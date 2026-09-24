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
    POST /api/toolbox/tool/book_dedup/merge      执行合并（写操作）
    POST /api/toolbox/tool/book_dedup/delete     删除一本重复书（写操作）
    POST /api/toolbox/tool/book_dedup/ignore     记下"这一组不是重复"（不改书库）
    POST /api/toolbox/tool/book_dedup/unignore   撤销忽略
    GET  /api/toolbox/tool/book_dedup/ignored    忽略名单
    GET  /api/toolbox/tool/book_dedup/cancel     取消扫描

两个外部工具特有的注意点（与其它工具包一致）：

1. 宿主挂 `api_routes` 时只额外包一层"工具被禁用就 404"的 `prepare()`，**不注入任何
   鉴权装饰器**，所以这里必须显式写 `@js @is_admin`。
2. `api_routes[].path` 是正则片段，前缀 `/api/toolbox/tool/<tool_id>/` 由宿主拼。

**写操作只有两处**：`/merge`（先复制格式、再删源记录）与 `/delete`（单删一本）。
两者都只接受"分组序号 + book_id"，成员由服务端回**本次扫描的索引**核对，不接受前端
传成员列表；执行前还会确认保留项现在还在书库（报告是跨重启持久化的，可能是几天前扫的）。
0.1.5 起 `/merge` 与 `/merge_plan` 多一个 `source_ids`（用户勾选"要合并掉"的那几本），
它同样要逐个回到这一组当前的成员里核对——**勾选不是"传 id 列表就能删书"的口子**。

已知限制（刻意写在代码里，不假装没这回事）：同名格式不会被复制；被删掉的那本书上的
用户数据会被宿主的级联清理一并删除（上游 issue #82 的修复），**不是迁移到保留项上**。
前端在合并预览与删除确认里都必须写明这两点。删掉的书**本身**可以在宿主的「回收站」里
恢复（calibre 删除默认进 `<书库>/.caltrash`，不是永久删除），但上面那些应用侧数据
恢复不回来——文案要分开说，别写成笼统的"不可逆"。
"""
import json
import logging
import threading
from typing import Optional

from webserver.handlers.base import BaseHandler, is_admin, js
from webserver.i18n import _
from webserver.services import AsyncService
from webserver.services.background_service import BackgroundService, BackgroundTask
from webserver.toolbox.base_tool import BaseTool

from . import driver, write_ops
from .dedup import diff as diff_mod
from .dedup import ignore as ignore_mod
from .dedup import keeper as keeper_mod
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

    # 与仓库根 manifest.json 的对应字段保持一致
    @staticmethod
    def info() -> dict:
        return {
            'tool_id': 'book_dedup',
            'name': '查重合并',
            'description': '按 ISBN/标题/作者找出重复书籍，可逐组对照并合并：'
                           '格式并入保留项，重复记录删除。合并前会列出同名格式的取舍',
            'revision': '0.1.8',
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
            # 忽略名单：读一次，扫描时按配对剔除；失效条目（书删了 / id 换了主人）
            # 由 write_ops 核对后回写，报告里记数交代（不静默丢弃用户设过的白名单）
            built = driver.run_scan(
                tool.api,
                book_ids=cls._scan_ids,
                threshold=cls._scan_threshold,
                scope_note=cls._scan_scope_note,
                on_progress=on_progress,
                cancel=cls._cancel_event,
                ignored_entries=write_ops.read_ignored(cls.shared_dir()),
                prune_ignored=lambda records_by_id: write_ops.live_ignored(
                    cls.shared_dir(), records_by_id),
            )
            if built is None:
                tool.complete_task(task_id)  # 被取消：正常收尾，不算失败
                return

            driver.write_report(work_dir, built)
            driver.write_index(work_dir, built)
            driver.write_latest_marker(cls.shared_dir(), task_id, built)
            # 报告目录保留策略：只留最近 `driver.KEEP_REPORTS` 份（默认 5）。**放在最后**——
            # 这时本次报告已经在盘上，它一定是最新的那份、不会被自己删掉；再把 work_dir
            # 显式列为 protect 兜一层。清理失败只记日志，绝不影响这次扫描的结果。
            try:
                driver.prune_reports(cls.shared_dir(), protect=work_dir)
            except Exception as err:  # noqa: BLE001
                logging.warning('[book_dedup] prune_reports failed: %s', err)
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
                'keep_rules': list(write_ops.KEEP_RULES),
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
        # 已经不在书库的成员（合并掉的 + 单独删掉的）：留在列表里会引导用户再点一次，
        # 拿它们去合并还会撞上"来源书籍不存在"。一组不足两本也就不必再处理。
        # （`gone` 下面还要回给前端做 `gone_ids`，所以先取出来、别塞回调用里）
        gone = write_ops.gone_ids(work_dir)
        groups = _visible_groups(groups, gone, load_ignored_keys(tool))

        confidence = self.get_argument('confidence', None)
        if confidence:
            wanted = set(confidence.split(','))
            groups = [g for g in groups if g.get('confidence') in wanted]
        keyword = (self.get_argument('keyword', '') or '').strip().lower()
        if keyword:
            # 一次读报告、建好"每组的可搜文本"，再筛。原来是每筛一组就调一次
            # `_group_text()`（各自读一遍报告）——135 组就是 135 次整份解析。
            texts = _group_texts(work_dir)
            groups = [g for g in groups if keyword in texts.get(g.get('index'), '')]

        signature = write_ops.report_signature(index)
        removed = write_ops.read_removed_titles(work_dir)
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
                'gone_ids': sorted(gone),
                'removed_titles': removed,
                'deleted_titles': write_ops.read_deleted_titles(work_dir),
                # 没做成的那几步也要摆出来：界面"已处理"区据此如实显示失败
                'failed_titles': write_ops.read_failed_titles(work_dir),
            },
        }


def _visible_groups(groups, gone, ignored_keys=None):
    """列表要显示的行：摘掉"剩下的不够两本"的组与"整组已被忽略"的组，
    并按存活成员重算行标题。

    :param groups: 索引里的组（`driver.read_index()` 的 `groups`）
    :param gone:   已经不在书库的成员 id（`write_ops.gone_ids()`）
    :param ignored_keys: 忽略名单的配对键集合。**整组都被忽略**的行直接不显示——
        那正是"重新查重之后这一组不会再出现"的同一条件（分组是成对关系的连通分量，
        组内两两都被剔掉，这一组就散了）；只忽略了其中几对的行照旧显示，
        但带上 `ignored_pairs` 让界面如实提示"本组有 N 对已被忽略"。
    """
    ignored_keys = ignored_keys or set()
    rows = []
    for group in groups:
        member_ids = list(group.get('members') or [])
        live_ids = [book_id for book_id in member_ids if book_id not in gone]
        if len(live_ids) < 2:
            continue
        counts = ignore_mod.counts_for_group(live_ids, ignored_keys)
        if counts['pairs'] and counts['ignored'] >= counts['pairs']:
            continue                      # 整组都被忽略：这一组不再报出来
        row = dict(group)
        stale = len(set(member_ids) & gone)
        row['stale_count'] = stale
        row['ignored_pairs'] = counts['ignored']
        if stale:
            # 行标题是扫描时写进索引的：只合并了一部分成员之后，它还会显示已经被
            # 删掉的书名。0.1.5 起"一组做一半"是常态，必须扣掉重算。
            row.update(_live_row_fields(group, gone))
        rows.append(row)
    return rows


def _live_row_fields(group, gone):
    """按"还在书库的成员"重算这一行的标题预览、成员数与两个字节数；旧索引原样返回空。

    行标题取自扫描时写死的索引（`driver.write_index` 的 `titles`），只有前
    `PREVIEW_TITLES` 个、也不含存活信息。0.1.5 允许只勾选其中几本合并，于是
    "一组做一半"成为常态——不重算的话，这一行会继续写着已经删掉的书名，
    "可回收/同格式重占"也会把已经删掉的那几本算进去。
    """
    member_ids = list(group.get('members') or [])
    titles = group.get('member_titles') or []
    authors = group.get('member_authors') or []
    sizes = group.get('member_sizes') or []
    formats = group.get('member_formats') or []
    if not titles or len(titles) != len(member_ids):
        return {}
    live_titles, live_authors, live = [], [], []
    for position, book_id in enumerate(member_ids):
        if book_id in gone:
            continue
        live_titles.append(titles[position])
        live_authors.append(authors[position] if position < len(authors) else '')
        live.append({
            'id': book_id,
            'title': titles[position],
            'size': sizes[position] if position < len(sizes) else 0,
            'formats': formats[position] if position < len(formats) else [],
        })
    fields = {
        'titles': live_titles[:driver.PREVIEW_TITLES],
        'authors': live_authors[:driver.PREVIEW_TITLES],
        'member_count': len(live_titles),
        'preview_truncated': len(live_titles) > driver.PREVIEW_TITLES,
    }
    keeper_id = group.get('keeper_id')
    if keeper_id in [item['id'] for item in live]:
        # 保留项还在 → 两个字节数按活着的成员重算（定义见 keeper.py）
        fields['reclaimable_bytes'] = keeper_mod.reclaimable_bytes(live, keeper_id)
        fields['disk_waste_bytes'] = keeper_mod.duplicate_disk_waste(live, keeper_id)
    return fields


def _group_texts(work_dir):
    """`{组序号: "成员书名与作者拼成的小写文本"}`，供 `/groups` 的关键字筛选。

    只读一次报告：这类筛选要拿**全部成员**的书名/作者去匹配（索引里只有前 2 个预览，
    用它筛会漏），所以必须回到报告；但一次建好即可，不必每组读一遍。
    """
    report = driver.read_report(work_dir)
    if not report:
        return {}
    texts = {}
    for position, group in enumerate(report.get('groups') or []):
        parts = []
        for member in group.get('members') or []:
            parts.append(str(member.get('title') or ''))
            parts.extend(str(a) for a in (member.get('authors') or []))
        texts[position] = ' '.join(parts).lower()
    return texts


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

        gone = write_ops.gone_ids(work_dir)
        members = [m for m in (group.get('members') or [])
                   if m.get('id') not in gone]
        # 这一组有几对已被忽略（0.1.6）：抽屉里要如实提示"重新查重后会拆开"
        group = dict(group)
        group['ignored_pairs'] = ignore_mod.counts_for_group(
            [m.get('id') for m in members], load_ignored_keys(tool))['ignored']
        keep_rule = self.get_argument('keep_rule', None)
        if keep_rule or len(members) != len(group.get('members') or []):
            # 成员变过（有成员被合并或删掉）或用户换了保留规则 → 重算推荐，不能沿用旧结论
            group['members'] = members
            group['member_count'] = len(members)
            group['recommendation'] = write_ops.recommend_for_members(members, keep_rule)
            # **对照表也要按活着的成员重算**：它是扫描时按当时那批成员算的，
            # 滤掉成员之后表头会比每行的单元格少一格——用户看到的是
            # "三个数字排在两个书名下面"（0.1.5 起"只合并一部分"是常态，很容易撞上）。
            group['diff'] = diff_mod.build_table(write_ops.to_diff_members(members))
        return {'err': 'ok', 'data': {
            'task_id': resolved, 'group': group, 'gone_ids': sorted(gone)}}


class MergePlanHandler(BaseHandler):
    """GET /merge_plan —— 合并预览：只算不写，把"会丢什么"摆清楚。

    参数：`index`（必填）、`keeper_id`、`keep_rule`、`source_ids`（逗号分隔的 id，
    用户勾选"要合并掉"的那几本；不传 = 除保留项外全部）、`task_id`。
    """

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

        plan, error = write_ops.build_plan(
            tool.api_proxy(), work_dir, index_arg,
            keeper_id=self.get_argument('keeper_id', None),
            keep_rule=self.get_argument('keep_rule', None),
            task_id=self.get_argument('task_id', None),
            source_ids=self.get_argument('source_ids', None))
        if error:
            return error
        return {'err': 'ok', 'data': plan}


class MergeHandler(BaseHandler):
    """POST /merge —— 执行合并（写操作之一）。

    请求体：``{"index": 3, "keeper_id": 12, "task_id": 7, "delete_source": true,
    "source_ids": [14, 15]}``

    `index` 是本次扫描里的分组序号；服务端会用它把成员 id 与那次扫描**绑定**，
    不接受前端直接传成员列表——否则一个被篡改/过期的列表会去删没被查出来的书。
    `source_ids` 是用户勾选"要合并掉"的那几本，同样要逐个回到这一组当前的成员里核对：
    **勾选不是"传 id 就能删书"的口子**。字段缺省 = 除保留项外全部（0.1.4 的行为），
    显式空数组 = 一本都不并（错误），不会被当成"全部"。

    部分失败也是"成功"返回（`err=ok`），但 `data.failed` 会如实给出失败条数，
    失败明细落进记账（`/groups` 回 `failed_titles`）——界面必须显示出来。
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

        plan, error = write_ops.build_plan(
            tool.api_proxy(), work_dir, index_arg,
            keeper_id=payload.get('keeper_id'),
            keep_rule=payload.get('keep_rule'),
            task_id=payload.get('task_id'),
            source_ids=payload.get('source_ids'))
        if error:
            return error

        result = write_ops.execute(tool.api_proxy(), work_dir, plan, delete_source=delete_source)
        if result.get('err') != 'ok':
            return result
        return {'err': 'ok', 'data': result['data']}


class DeleteHandler(BaseHandler):
    """POST /delete —— 单独删除一本重复书。**本工具的第二处写操作。**

    请求体：``{"index": 3, "book_id": 14, "task_id": 7}``

    与 `/merge` 同样的守门：只收分组序号 + 一个 book_id，且该 id 必须**是这一组当前的
    成员**（服务端自己回索引里核对）。不接受前端传任意 id——否则一个构造出来的请求
    就能删掉没被查出来的书。

    删除的后果（界面文案必须与这里一致）：宿主侧会级联清理这本书记关联数据
    （收藏/在读/进度/时长/评分/书评/共读记录/书单关联），**但那是删除不是迁移**——
    它的阅读进度不会搬到同组的其它书上。
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
        if payload.get('book_id') is None:
            return {'err': 'params.invalid', 'msg': _('缺少 book_id 参数')}

        tool = BookDedupTool()
        work_dir, _resolved = _report_dir_for(tool, payload.get('task_id'))
        if not work_dir:
            return {'err': 'report.not_found', 'msg': _('没有可读取的查重结果')}

        result = write_ops.execute_delete(
            tool.api_proxy(), work_dir, index_arg, payload.get('book_id'))
        if result.get('err') != 'ok':
            return result
        return {'err': 'ok', 'data': result['data']}


def load_ignored_keys(tool):
    """忽略名单的配对键集合（读一次，供 `/groups`、`/group` 用）。

    文件很小（一条配对一行），每个请求读一次即可；读取失败由 `write_ops` 记日志并
    按空名单处理——忽略名单读不到不该让整个列表打不开。
    """
    return ignore_mod.ignored_keys_from(
        write_ops.read_ignored(tool.shared_dir()))


class IgnoreHandler(BaseHandler):
    """POST /ignore —— 记下"这一组不是重复"，下次查重不再报出来。

    请求体：``{"index": 3, "task_id": 7, "ids": [12, 15]}``（`ids` 可省，默认整组）。

    守门与写操作同款：只收分组序号 + 成员 id，且**每个 id 都必须是这一组当前的成员**。
    这里不改书库，但一样不接受任意 id——否则一个构造出来的请求就能把任意两本书写进白名单，
    以后它们永远不再被查出来（白名单是"少报"，同样是用户看不见的损失）。

    记的是**配对**不是整本（见 `dedup/ignore.py` 的理由）：记整本会连真重复一起藏掉，
    而且书 id 会被 calibre 复用（新书拿 `max(id)+1`），只存 id 的白名单会误伤新书。
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

        tool = BookDedupTool()
        work_dir, _resolved = _report_dir_for(tool, payload.get('task_id'))
        if not work_dir:
            return {'err': 'report.not_found', 'msg': _('没有可读取的查重结果')}

        members, error = write_ops.group_members(work_dir, index_arg)
        if error:
            return error
        wanted = payload.get('ids')
        if wanted in (None, '', []):
            chosen = members
        else:
            if not isinstance(wanted, (list, tuple)):
                return {'err': 'params.invalid', 'msg': _('ids 必须是数组')}
            try:
                picked = {int(book_id) for book_id in wanted}
            except (TypeError, ValueError):
                return {'err': 'params.invalid', 'msg': _('ids 必须是整数数组')}
            known = {m.get('id') for m in members}
            foreign = sorted(picked - known)
            if foreign:
                return {'err': 'book.not_in_group',
                        'msg': _('这些书不属于这一组：%s')
                               % '、'.join(str(i) for i in foreign)}
            chosen = [m for m in members if m.get('id') in picked]
        if len(chosen) < 2:
            return {'err': 'params.invalid', 'msg': _('至少要选两本才算"不是重复"')}

        added = write_ops.add_ignored(tool.shared_dir(), chosen)
        titles = [m.get('title') or '' for m in chosen]
        logging.info('[book_dedup] ignored group %s: %s', index_arg, titles)
        return {'err': 'ok', 'data': {
            'added': added,
            'titles': titles,
            'pairs': [list(pair) for pair in ignore_mod.pairs_in(
                [m.get('id') for m in chosen])],
        }}


class UnignoreHandler(BaseHandler):
    """POST /unignore —— 撤销忽略。

    请求体：``{"pairs": [[12, 15]]}`` 撤销指定配对；``{"all": true}`` 清空名单。
    """

    @js
    @is_admin
    async def post(self):
        payload = _parse_body(self)
        if payload is None:
            return {'err': 'params.invalid', 'msg': _('请求体不是合法 JSON')}
        tool = BookDedupTool()
        if payload.get('all'):
            removed = write_ops.remove_ignored(tool.shared_dir(), all_entries=True)
            return {'err': 'ok', 'data': {'removed': removed}}
        pairs = payload.get('pairs')
        if not isinstance(pairs, (list, tuple)) or not pairs:
            return {'err': 'params.invalid', 'msg': _('缺少 pairs 参数')}
        try:
            parsed = [(int(pair[0]), int(pair[1])) for pair in pairs]
        except (TypeError, ValueError, IndexError):
            return {'err': 'params.invalid', 'msg': _('pairs 必须是 [[a, b], ...]')}
        removed = write_ops.remove_ignored(tool.shared_dir(), parsed)
        return {'err': 'ok', 'data': {'removed': removed}}


class IgnoredHandler(BaseHandler):
    """GET /ignored —— 忽略名单（书名 + 配对），供界面列出与撤销。"""

    @js
    @is_admin
    async def get(self):
        tool = BookDedupTool()
        return {'err': 'ok', 'data': {
            'ignored': write_ops.ignored_rows(tool.shared_dir())}}


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
    (r'delete', DeleteHandler),
    (r'ignore', IgnoreHandler),
    (r'unignore', UnignoreHandler),
    (r'ignored', IgnoredHandler),
    (r'cancel', CancelHandler),
)

# 后台线程用的扫描参数（classmethod 拿不到请求上下文，所以挂在类上）
BookDedupTool._scan_ids = None
BookDedupTool._scan_threshold = driver.DEFAULT_THRESHOLD
BookDedupTool._scan_scope_note = ''
BookDedupTool._worker = None