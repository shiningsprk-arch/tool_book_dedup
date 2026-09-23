# -*- coding: utf-8 -*-
"""查重合并：MyBooks 外部工具（Toolbox 工具包）后端。

manifest.json 的 `entry_backend` 指向 `BookDedupTool`，`api_routes` 指向本模块的若干
Handler，宿主 toolbox_manager 把它们挂到：

    GET  /api/toolbox/tool/book_dedup/scope      范围与阈值选项
    POST /api/toolbox/tool/book_dedup/start      开始扫描
    GET  /api/toolbox/tool/book_dedup/progress   任务进度（可回退到上次结果）
    GET  /api/toolbox/tool/book_dedup/groups     分组列表（分页/筛选）
    GET  /api/toolbox/tool/book_dedup/group      单个分组的完整对照
    GET  /api/toolbox/tool/book_dedup/merge_plan 合并预览（只算不写）
    POST /api/toolbox/tool/book_dedup/merge      执行合并（**唯一的写操作**）
    POST /api/toolbox/tool/book_dedup/cancel     取消扫描

两个外部工具特有的注意点（与其它工具包一致）：

1. 宿主挂 `api_routes` 时只额外包一层"工具被禁用就 404"的 `prepare()`，**不注入任何
   鉴权装饰器**，所以这里必须显式写 `@js @is_admin`。
2. `api_routes[].path` 是正则片段，前缀 `/api/toolbox/tool/<tool_id>/` 由宿主拼。

**写操作只有一处**：`/merge`。它做的是"先复制格式、再删源记录"，两件事分开记账，
并且 `/merge_plan` 会在动手前把「哪些格式会被复制、哪些同格式会被丢弃」摆清楚——
同名格式不会被复制（`merge_book_formats` 只补目标书缺的格式），用户必须在动手前看到。

已知限制（刻意写在代码里，不假装没这回事）：工具箱的删除只清 `Item`，收藏/在读/进度/
评分/书单会留下悬空行（上游 issue #82 未修）。前端在确认弹层里必须写明这一点。
"""
import json
import logging
import os
import threading
from typing import Optional

from webserver.handlers.base import BaseHandler, is_admin, js
from webserver.i18n import _
from webserver.services import AsyncService
from webserver.services.background_service import BackgroundService, BackgroundTask
from webserver.toolbox.base_tool import BaseTool

from . import driver, merge as merge_mod
from .dedup import report as report_mod

# 单次扫描最多覆盖的书本数（与 driver 的上限一致）
MAX_BOOKS = driver.MAX_BOOKS

_STATUS_RUNNING = BackgroundTask.STATUS_RUNNING
_STATUS_COMPLETED = BackgroundTask.STATUS_COMPLETED
_STATUS_FAILED = BackgroundTask.STATUS_FAILED


class BookDedupTool(BaseTool):
    """书籍查重与合并（外置工具）。"""

    service_item_name = '查重合并'

    # 同一时刻只允许一个扫描任务。`_accepted` 覆盖"已受理、但后台线程还没开跑"这段窗口：
    # 只看任务状态的话，两次几乎同时到达的 /start 都能通过，而且取消会打到旧的事件上。
    _state_lock = threading.Lock()
    _accepted = False
    _last_task_id: Optional[int] = None
    _cancel_event = threading.Event()
    # 本次扫描的记录（内存里留一份，供分组明细用）。扫描结果 MB 级，只留最近一次。
    _records = None
    _records_lock = threading.Lock()

    # 与仓库根 manifest.json 的对应字段保持一致
    @staticmethod
    def info() -> dict:
        return {
            'tool_id': 'book_dedup',
            'name': '查重合并',
            'description': '按 ISBN/标题/作者找出重复书籍，可逐组对照并合并：'
                           '格式并入保留项，重复记录删除。合并前会列出同名格式的取舍',
            'revision': '0.1.1',
            'author': '黏菌',
            'publish_date': '2026-09-23',
            'repo_url': 'https://github.com/shiningsprk-arch/tool_book_dedup',
        }

    # ---------------------------------------------------------------- 任务状态

    @classmethod
    def _task_status(cls) -> str:
        if cls._last_task_id is None:
            return ''
        try:
            task = BackgroundService().get_task(cls._last_task_id)
        except Exception:  # noqa: BLE001
            return ''
        return (task or {}).get('status') or ''

    @classmethod
    def get_last_task(cls) -> Optional[dict]:
        if cls._last_task_id is None:
            return None
        return BackgroundService().get_task(cls._last_task_id)

    @classmethod
    def is_running(cls) -> bool:
        if cls._accepted:
            return True
        _lock = getattr(BackgroundService, 'lock', None)
        if _lock is not None:
            with _lock:
                return cls._task_status() == _STATUS_RUNNING
        return cls._task_status() == _STATUS_RUNNING

    @classmethod
    def begin_task(cls) -> bool:
        """原子占位：把"已受理"和"启动"之间的窗口关掉。"""
        with cls._state_lock:
            if cls._accepted:
                return False
            if cls._task_status() == _STATUS_RUNNING:
                return False
            cls._accepted = True
            cls._cancel_event = threading.Event()
            return True

    @classmethod
    def request_cancel(cls) -> None:
        cls._cancel_event.set()

    @classmethod
    def release_task(cls) -> None:
        with cls._state_lock:
            cls._accepted = False

    # ---------------------------------------------------------------- 记录暂存

    @classmethod
    def set_records(cls, records):
        with cls._records_lock:
            cls._records = records

    @classmethod
    def get_records(cls):
        with cls._records_lock:
            return cls._records

    # ---------------------------------------------------------------- 取数

    # **必须带 `@AsyncService.register_function`**：`self.db` 是 `AsyncService.setup()` 注入的，
    # 而 `setup` 只在 `register_function` / `register_service` 的包装里被调用；`register_service`
    # 又是**异步**的（生产环境 `async_mode()` 恒真，丢队列后返回 None），handler 同步拿不到值。
    # 直接 `BookDedupTool()` 再调 `self.api.calibre.*` 会拿到 `db=None` → AttributeError。
    # 这个坑的代价是"工具装上后一直显示共 0 本书、开始按钮点不动"。
    @AsyncService.register_function
    def all_book_ids(self):
        """全库 book_id（handler 侧的入口）。"""
        return list(self.api.calibre.all_book_ids())

    @AsyncService.register_function
    def api_proxy(self):
        """返回一个**已注入 db** 的 `CoreAPI`，供需要写书库的 handler 使用。

        合并（`/merge`）与预览（`/merge_plan`）都要读格式列表、调 `merge_formats`、
        删记录——这些全在 `self.api.calibre` 上，而 `api` 本身不会触发 `setup()`。
        所以写操作也必须经由带 `register_function` 的方法，让宿主先把 db 装上。
        只读、且不需要返回值的方法（如后台线程里的扫描）可以直接用 `AsyncService().db`。
        """
        return self.api

    @AsyncService.register_function
    def resolve_book_ids(self, book_ids):
        """把请求里的 id 列表解析成要扫描的列表；空列表 = 整个书库。

        空列表展开放在**这里**而不是前端：几万个 id 不该传到浏览器再传回来。
        """
        ids = list(book_ids or [])
        if not ids:
            ids = list(self.api.calibre.all_book_ids())
        return ids

    @classmethod
    def shared_dir(cls) -> str:
        """工具共享目录（无 key 那级）：`latest.json` 落在这里，路径不随 task_id 变。"""
        tool = cls()
        shared = getattr(tool, 'shared_work_dir', None)
        if callable(shared):
            return shared()
        return tool.get_work_dir()

    @classmethod
    def report_dir(cls, task_id) -> str:
        return cls().get_work_dir('task-%s' % task_id)

    # ---------------------------------------------------------------- 扫描主体

    @classmethod
    def run_scan(cls, task_id: int) -> None:
        """后台线程主体：跑一遍 driver.run_scan，落盘报告与指针，最后关任务。"""
        tool = cls()
        work_dir = cls.report_dir(task_id)
        # 后台线程不走 `register_*` 的包装，所以 `tool.db` 不会被注入。而
        # `CoreAPI.calibre` 是**调用时**去读 `self._owner.db`（core_api.py:40-50 的注释），
        # 因此这里把单例上的 db 挂到实例上就够了——否则整库扫描会拿 `db=None` 崩掉。
        try:
            tool.db = AsyncService().db
        except Exception as err:  # noqa: BLE001
            logging.error('[book_dedup] 取宿主 db 失败，扫描无法进行: %s', err)
            tool.complete_task(task_id, error_message=_('无法访问书库'))
            cls.release_task()
            return

        def on_progress(done, total, phase=''):
            try:
                percent = int(done * 100 / total) if total else 100
                # compare 阶段做完就没有下一段了，封顶 95 等落盘结束再补到 100
                percent = min(95, percent) if phase == 'compare' else min(90, percent)
                tool.update_task_progress(
                    task_id, percent,
                    progress_data={'phase': phase, 'done': done, 'total': total})
            except Exception as err:  # noqa: BLE001
                logging.warning('[book_dedup] progress update failed: %s', err)

        try:
            built = driver.run_scan(
                tool.api,
                book_ids=cls._scan_ids,
                threshold=cls._scan_threshold,
                scope_note=cls._scan_scope_note,
                on_progress=on_progress,
                cancel=cls._cancel_event,
            )
            if built is None:
                tool.complete_task(task_id)  # 被取消：正常收尾，不算失败
                return

            driver.write_report(work_dir, built)
            driver.write_index(work_dir, built)
            driver.write_latest_marker(cls.shared_dir(), task_id, built)
            cls.set_records(cls._records_snapshot)
            summary = built.get('summary') or {}
            tool.update_task_progress(
                task_id, 100,
                progress_data={
                    'phase': 'done',
                    'summary': summary,
                    'threshold': built.get('threshold'),
                    'scanned_books': built.get('scanned_books'),
                })
            tool.complete_task(task_id)
        except Exception as err:  # noqa: BLE001
            logging.error('[book_dedup] scan failed: %s', err, exc_info=True)
            tool.complete_task(task_id, error_message=str(err))
        finally:
            cls._scan_ids = None
            cls._records_snapshot = None
            cls.release_task()


def _parse_body(handler) -> Optional[dict]:
    """读 JSON 请求体；不是合法 JSON 时返回 None。"""
    try:
        payload = json.loads(handler.request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None



def _report_dir_for(tool, task_id=None):
    """解析本次要读的工作目录：显式 task_id 优先，否则回退到 latest 标记。"""
    if task_id:
        return tool.report_dir(task_id), task_id
    marker = driver.read_latest_marker(tool.shared_dir())
    if not marker:
        return None, None
    return tool.report_dir(marker.get('task_id')), marker.get('task_id')


class ScopeHandler(BaseHandler):
    """GET /scope —— 范围与阈值选项。"""

    @js
    @is_admin
    async def get(self):
        tool = BookDedupTool()
        try:
            total = len(tool.all_book_ids())
        except Exception as err:  # noqa: BLE001
            logging.error('[book_dedup] 读取全库 id 失败: %s', err, exc_info=True)
            return {'err': 'scope.failed', 'msg': _('读取书库失败')}
        return {
            'err': 'ok',
            'data': {
                'total_books': total,
                'max_books': MAX_BOOKS,
                'threshold': driver.DEFAULT_THRESHOLD,
                'threshold_range': [driver.MIN_THRESHOLD, driver.MAX_THRESHOLD],
                'confidence': ['strong', 'likely', 'weak'],
                'keep_rules': list(merge_mod.KEEP_RULES),
            },
        }


class StartHandler(BaseHandler):
    """POST /start —— 开始扫描（立即返回，进度走 /progress）。"""

    @js
    @is_admin
    async def post(self):
        if BookDedupTool.is_running():
            return {'err': 'task.running', 'msg': _('已有查重任务在运行')}

        payload = _parse_body(self)
        if payload is None:
            return {'err': 'params.invalid', 'msg': _('请求体不是合法 JSON')}

        book_ids = payload.get('book_ids') or []
        if not isinstance(book_ids, list) or any(not isinstance(i, int) for i in book_ids):
            return {'err': 'params.invalid', 'msg': _('book_ids 必须是整数数组')}
        # 空列表 = 整个书库。展开走带装饰器的方法（前端不该把几万个 id 传一圈回来）。
        try:
            book_ids = BookDedupTool().resolve_book_ids(book_ids)
        except Exception as err:  # noqa: BLE001
            logging.error('[book_dedup] 读取全库 id 失败: %s', err, exc_info=True)
            return {'err': 'scope.failed', 'msg': _('读取书库失败')}
        if not book_ids:
            return {'err': 'scope.empty', 'msg': _('书库为空')}
        if len(book_ids) > MAX_BOOKS:
            return {'err': 'scope.too_large',
                    'msg': _('单次最多扫描 %d 本书') % MAX_BOOKS}

        threshold = driver.normalize_threshold(payload.get('threshold'))
        if not BookDedupTool.begin_task():
            return {'err': 'task.running', 'msg': _('已有查重任务在运行')}

        tool = BookDedupTool()
        task_id = tool.create_task(
            progress_data={'phase': 'queued', 'added': len(book_ids)})
        BookDedupTool._last_task_id = task_id

        # 扫描参数挂在类上给后台线程用：run_scan 是 classmethod，拿不到请求上下文
        BookDedupTool._scan_ids = book_ids
        BookDedupTool._scan_threshold = threshold
        BookDedupTool._scan_scope_note = str(payload.get('scope_note') or '')
        BookDedupTool._records_snapshot = None

        worker = threading.Thread(
            target=BookDedupTool.run_scan, args=(task_id,),
            name='book-dedup-scan', daemon=True)
        worker.start()
        BookDedupTool._worker = worker
        return {'err': 'ok', 'data': {'task_id': task_id, 'threshold': threshold}}


class ProgressHandler(BaseHandler):
    """GET /progress —— 任务进度；内存里没有任务时回退到上次结果。

    宿主只把后台任务放在内存里，`_last_task_id` 也只在进程内有效——安装/更新工具或重启
    MyBooks 之后这里就认不出上次那次扫描了，而报告文件其实还在磁盘上。所以无任务时回退读
    `latest.json`，重开工具页依然能看到上次的结果，不必重跑。
    """

    @js
    @is_admin
    async def get(self):
        tool = BookDedupTool()
        task = BookDedupTool.get_last_task()
        if task:
            return {'err': 'ok', 'data': {
                'status': task.get('status'),
                'progress': task.get('progress', 0),
                'progress_data': task.get('progress_data') or {},
                'error': task.get('error_message'),
                'task_id': task.get('id'),
                'restored': False,
            }}

        if BookDedupTool.is_running():
            return {'err': 'ok', 'data': {'status': 'running', 'progress': 0,
                                          'progress_data': {}, 'restored': False}}

        marker = driver.read_latest_marker(tool.shared_dir())
        restored = driver.restored_progress(marker)
        if not restored:
            return {'err': 'task.not_found', 'msg': _('没有查重任务记录')}
        restored['progress_data'] = {
            'phase': 'done',
            'summary': restored.get('summary'),
            'threshold': restored.get('threshold'),
            'scanned_books': restored.get('scanned_books'),
        }
        return {'err': 'ok', 'data': restored}


class GroupsHandler(BaseHandler):
    """GET /groups —— 分组列表（分页/筛选）。

    只回索引级的字段（成员 id、可回收体积、推荐保留 id），完整对照走 `/group`——
    一组的完整对照可能很大，列表页不需要。
    """

    @js
    @is_admin
    async def get(self):
        tool = BookDedupTool()
        task_id = self.get_argument('task_id', None)
        work_dir, resolved = _report_dir_for(tool, task_id)
        if not work_dir:
            return {'err': 'report.not_found', 'msg': _('没有可读取的查重结果')}

        index = driver.read_index(work_dir)
        if not index:
            return {'err': 'report.not_found', 'msg': _('查重结果文件缺失或损坏')}

        groups = list(index.get('groups') or [])
        merged_ids = set(merge_mod.read_merged_ids(work_dir))
        # 已合并的成员不再出现：合并会真的删掉源记录，留在列表里会引导用户再次点它
        groups = [g for g in groups
                  if not (set(g.get('members') or []) & merged_ids)]
        for group in groups:
            members = group.get('members') or []
            group['merged_count'] = len([m for m in members if m in merged_ids])

        confidence = self.get_argument('confidence', None)
        if confidence:
            wanted = set(confidence.split(','))
            groups = [g for g in groups if g.get('confidence') in wanted]
        keyword = (self.get_argument('keyword', '') or '').strip().lower()
        if keyword:
            needle = keyword
            groups = [g for g in groups if needle in _group_text(work_dir, g)]

        signature = merge_mod.report_signature(index)
        removed = merge_mod.read_removed_titles(work_dir)
        page = self.get_argument('page', '0')
        size = self.get_argument('size', '50')
        sliced, total = report_mod.paginate(groups, page=page, size=size)
        return {
            'err': 'ok',
            'data': {
                'task_id': resolved,
                'signature': signature,
                'generated_at': index.get('generated_at'),
                'threshold': index.get('threshold'),
                'summary': index.get('summary'),
                'stats': index.get('stats'),
                'scanned_books': index.get('scanned_books'),
                'filtered_total': total,
                'page': int(page) if str(page).isdigit() else 0,
                'groups': sliced,
                'merged_ids': sorted(merged_ids),
                'removed_titles': removed,
            },
        }


def _group_text(work_dir, group):
    """把一组里所有成员的书名/作者拍成一段小写文本，供关键字筛选。"""
    report = driver.read_report(work_dir)
    if not report:
        return ''
    members = report.get('groups') or []
    index = group.get('index')
    if index is None or index >= len(members):
        return ''
    parts = []
    for member in members[index].get('members') or []:
        parts.append(str(member.get('title') or ''))
        parts.extend(str(a) for a in (member.get('authors') or []))
    return ' '.join(parts).lower()


class GroupHandler(BaseHandler):
    """GET /group —— 单个分组的完整对照（成员、差异表、推荐保留）。"""

    @js
    @is_admin
    async def get(self):
        tool = BookDedupTool()
        index_arg = self.get_argument('index', None)
        if index_arg is None:
            return {'err': 'params.invalid', 'msg': _('缺少 index 参数')}
        task_id = self.get_argument('task_id', None)
        work_dir, resolved = _report_dir_for(tool, task_id)
        if not work_dir:
            return {'err': 'report.not_found', 'msg': _('没有可读取的查重结果')}

        group = driver.read_report(work_dir, group_index=index_arg)
        if not group:
            return {'err': 'group.not_found', 'msg': _('找不到该分组')}

        merged_ids = set(merge_mod.read_merged_ids(work_dir))
        members = [m for m in (group.get('members') or [])
                   if m.get('id') not in merged_ids]
        keep_rule = self.get_argument('keep_rule', None)
        if keep_rule or len(members) != len(group.get('members') or []):
            # 成员变过（有成员被合并掉）或用户换了保留规则 → 重算推荐，不能沿用旧结论
            group = dict(group)
            group['members'] = members
            group['recommendation'] = merge_mod.recommend_for_members(members, keep_rule)
        return {'err': 'ok', 'data': {
            'task_id': resolved, 'group': group, 'merged_ids': sorted(merged_ids)}}


class MergePlanHandler(BaseHandler):
    """GET /merge_plan —— 合并预览：只算不写，把"会丢什么"摆清楚。"""

    @js
    @is_admin
    async def get(self):
        tool = BookDedupTool()
        index_arg = self.get_argument('index', None)
        if index_arg is None:
            return {'err': 'params.invalid', 'msg': _('缺少 index 参数')}
        work_dir, _resolved = _report_dir_for(tool, self.get_argument('task_id', None))
        if not work_dir:
            return {'err': 'report.not_found', 'msg': _('没有可读取的查重结果')}

        plan, error = merge_mod.build_plan(
            tool.api_proxy(), work_dir, index_arg,
            keeper_id=self.get_argument('keeper_id', None),
            keep_rule=self.get_argument('keep_rule', None),
            task_id=self.get_argument('task_id', None))
        if error:
            return error
        return {'err': 'ok', 'data': plan}


class MergeHandler(BaseHandler):
    """POST /merge —— 执行合并。**本工具唯一的写操作。**

    请求体：``{"index": 3, "keeper_id": 12, "task_id": 7, "delete_source": true}``

    `index` 是本次扫描里的分组序号；服务端会用它把成员 id 与那次扫描**绑定**，
    不接受前端直接传成员列表——否则一个被篡改/过期的列表会去删没被查出来的书。
    """

    @js
    @is_admin
    async def post(self):
        payload = _parse_body(self)
        if payload is None:
            return {'err': 'params.invalid', 'msg': _('请求体不是合法 JSON')}
        index_arg = payload.get('index')
        if index_arg is None:
            return {'err': 'params.invalid', 'msg': _('缺少 index 参数')}
        delete_source = bool(payload.get('delete_source', True))

        tool = BookDedupTool()
        work_dir, _resolved = _report_dir_for(tool, payload.get('task_id'))
        if not work_dir:
            return {'err': 'report.not_found', 'msg': _('没有可读取的查重结果')}

        plan, error = merge_mod.build_plan(
            tool.api_proxy(), work_dir, index_arg,
            keeper_id=payload.get('keeper_id'),
            keep_rule=payload.get('keep_rule'),
            task_id=payload.get('task_id'))
        if error:
            return error

        result = merge_mod.execute(tool.api_proxy(), work_dir, plan, delete_source=delete_source)
        if result.get('err') != 'ok':
            return result
        return {'err': 'ok', 'data': result['data']}


class CancelHandler(BaseHandler):
    """POST /cancel —— 取消扫描。"""

    @js
    @is_admin
    async def post(self):
        if not BookDedupTool.is_running():
            return {'err': 'task.not_found', 'msg': _('没有正在运行的查重任务')}
        BookDedupTool.request_cancel()
        return {'err': 'ok', 'msg': _('已请求取消')}


ROUTES = (
    (r'scope', ScopeHandler),
    (r'start', StartHandler),
    (r'progress', ProgressHandler),
    (r'groups', GroupsHandler),
    (r'group', GroupHandler),
    (r'merge_plan', MergePlanHandler),
    (r'merge', MergeHandler),
    (r'cancel', CancelHandler),
)

# 后台线程用的扫描参数（classmethod 拿不到请求上下文，所以挂在类上）
BookDedupTool._scan_ids = None
BookDedupTool._scan_threshold = driver.DEFAULT_THRESHOLD
BookDedupTool._scan_scope_note = ''
BookDedupTool._records_snapshot = None
BookDedupTool._worker = None