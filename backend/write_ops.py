# -*- coding: utf-8 -*-
"""两处写操作（合并 / 删除）的执行与记账。

这是整个工具里**唯一会写书库**的模块，所以规则写得比别处死：

1. **成员来自本次扫描，不接受前端传 id 列表。** `/merge`、`/delete` 都只收一个分组序号，
   服务端自己回到那次扫描的索引里取成员——否则一个被篡改或过期的列表就能去删
   没被查出来的书。
   （0.1.5 的 `source_ids`——用户勾选要合并掉的那几本——**不是**这条规矩的例外：
   每个 id 仍要回到这一组当前的成员里核对，超出集合一律拒绝。）
2. **保留项必须在成员里**，且必须是本次扫描算出的成员之一。
3. **先预览再执行**：`build_plan()` 与 `execute()` 用同一份计划，预览里已经列明
   哪些格式会被复制、哪些同格式会被丢弃。
4. **执行后记账**：合并掉的 id 写进 `merged.json`、单独删掉的写进 `deleted.json`，
   列表与会话据此把它们摘掉——两种操作都是真的从书库删记录，留在列表里会引导用户再点一次。
   没勾选的那些也一并记进 `kept`：它们还在书库里，记账里必须留着，否则事后看台账会
   以为这一组已经处理干净。

关于"被删掉的那本书里的用户数据"：宿主侧的删除会**级联清理**关联数据（收藏/在读/
阅读进度/时长/评分/书评/共读记录/书单关联，见上游 PoxenStudio/mybooks#82 的修复
commit a33f0c26，新增的 `webserver/base/book_data_cascade.py`）。但那是**删除不是迁移**
——被删那一本上的进度不会搬到保留项上。界面文案必须说清这一点，不能让人以为合并会把
阅读进度一起带过来。

关于"删错了能不能捞回来"：**书本身能**。工具的删除走 `api.calibre.delete_book` →
`base_tool.delete_book_by_id` → `self.db.delete_book(book_id)`，而 calibre 的签名是
`delete_book(..., permanent=False, ...)`：文件被**搬进** `<书库>/.caltrash`，宿主的
「回收站」页（`/api/admin/trash/books/restore`）能按原 book_id 连元数据一起恢复，
保留窗口是 calibre 的 14 天过期时间。**捞不回来的是上面那段级联清掉的应用侧数据**。
所以文案写的应该是这个区别，而不是笼统的"删除不可逆"（0.1.4 那样写过，不准确）。
"""
import json
import logging
import os
import threading
import time

from . import driver
from .dedup import ignore as ignore_mod
from .dedup import keeper as keeper_mod

MERGED_FILENAME = 'merged.json'
DELETED_FILENAME = 'deleted.json'
IGNORED_FILENAME = 'ignored.json'

# 写操作串行化。**两个用途**：
#
# 1. 记账是"读→改→写"，并发两次（双击确认、两个标签页）会丢一条记录——
#    丢了记账意味着那本书仍留在列表里，再点合并就撞宿主报错。
# 2. 同一组的两次合并同时跑，两边都先通过"还剩 ≥2 本"的检查，然后各自去删源记录。
#
# 一把进程内的锁就够：工具是单进程的，写操作本身也只有这两处。
_WRITE_LOCK = threading.RLock()


def keep_rules():
    return list(keeper_mod.KEEP_RULES)


# 与 tool.BookDedupTool.info() 的 keep_rules 同源，供 scope 与前端下拉用
KEEP_RULES = keep_rules()


def merged_path(work_dir):
    return os.path.join(work_dir, MERGED_FILENAME)


def read_merged(work_dir):
    """读合并记账（列表：每次合并一条）。"""
    path = merged_path(work_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as err:
        logging.warning('[book_dedup] merged ledger unreadable: %s', err)
        return []
    return data if isinstance(data, list) else []


def read_merged_ids(work_dir):
    """被合并掉的 book_id（源记录已删除）。"""
    ids = []
    for entry in read_merged(work_dir):
        for book_id in entry.get('removed_ids') or []:
            ids.append(book_id)
    return ids


def read_removed_titles(work_dir):
    """被合并掉的书名，供界面在"已处理"区如实交代删了什么。"""
    titles = []
    for entry in read_merged(work_dir):
        for item in entry.get('removed') or []:
            titles.append({'id': item.get('id'), 'title': item.get('title') or '',
                           'into': entry.get('keeper_title') or '',
                           'at': entry.get('at') or ''})
    return titles


def read_failed_titles(work_dir):
    """**没做成**的那几步（合并失败/被中止），供界面如实交代。

    记账里一直有 `steps[].error`，但界面从不显示——5 本里失败 3 本时提示仍然是
    "已合并：复制 N 个格式，删除 M 条重复记录"，用户以为全成了。现在按记账列出来。
    """
    failed = []
    for entry in read_merged(work_dir):
        for step in entry.get('steps') or []:
            if not step.get('error'):
                continue
            failed.append({
                'id': step.get('source_id'),
                'title': step.get('source_title') or '',
                'error': step.get('error'),
                'message': step.get('message') or '',
                'into': entry.get('keeper_title') or '',
                'at': entry.get('at') or '',
            })
    return failed


def append_merged(work_dir, entry):
    with _WRITE_LOCK:
        entries = read_merged(work_dir)
        entries.append(entry)
        try:
            os.makedirs(work_dir, exist_ok=True)
            tmp = merged_path(work_dir) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(entries, handle, ensure_ascii=False)
            os.replace(tmp, merged_path(work_dir))
            return True
        except OSError as err:
            logging.error('[book_dedup] merged ledger not written: %s', err)
            return False


# --------------------------------------------------------------------------- 删除记账


def deleted_path(work_dir):
    return os.path.join(work_dir, DELETED_FILENAME)


def _read_ledger(path, what):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as err:
        logging.warning('[book_dedup] %s ledger unreadable: %s', what, err)
        return []
    return data if isinstance(data, list) else []


def read_deleted(work_dir):
    """读单独删除的记账（每次删除一条）。"""
    return _read_ledger(deleted_path(work_dir), 'deleted')


def read_deleted_ids(work_dir):
    return [entry.get('id') for entry in read_deleted(work_dir) if entry.get('id')]


def read_deleted_titles(work_dir):
    """被单独删掉的书名，供"已处理"区如实交代。"""
    return [{'id': entry.get('id'), 'title': entry.get('title') or '',
             'at': entry.get('at') or ''} for entry in read_deleted(work_dir)]


def append_deleted(work_dir, entry):
    with _WRITE_LOCK:
        entries = read_deleted(work_dir)
        entries.append(entry)
        try:
            os.makedirs(work_dir, exist_ok=True)
            tmp = deleted_path(work_dir) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(entries, handle, ensure_ascii=False)
            os.replace(tmp, deleted_path(work_dir))
            return True
        except OSError as err:
            logging.error('[book_dedup] deleted ledger not written: %s', err)
            return False


def gone_ids(work_dir):
    """本次结果里**已经不在书库**的成员（合并掉的 + 单独删掉的）。

    `/groups`、`/group`、`/merge` 三处都要用它：这些 id 已经不存在了，留在列表里会
    引导用户再点一次，拿它们去合并还会撞上"来源书籍不存在"。
    """
    return set(read_merged_ids(work_dir)) | set(read_deleted_ids(work_dir))


# --------------------------------------------------------------------------- 忽略名单
#
# "这几本不是重复"记在这里。**不是写书库**，所以不受 §写操作 那套守门约束；
# 但它是跨会话的持久状态（下次扫描要读），所以照样走同一把锁、同样的"读→改→写"原子替换。


def ignored_path(work_dir):
    return os.path.join(work_dir, IGNORED_FILENAME)


def read_ignored(work_dir):
    """读忽略名单（配对列表，每条带两侧指纹）。"""
    return _read_ledger(ignored_path(work_dir), 'ignored')


def _write_ignored(work_dir, entries):
    with _WRITE_LOCK:
        try:
            os.makedirs(work_dir, exist_ok=True)
            tmp = ignored_path(work_dir) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(entries, handle, ensure_ascii=False)
            os.replace(tmp, ignored_path(work_dir))
            return True
        except OSError as err:
            logging.error('[book_dedup] ignored list not written: %s', err)
            return False


def add_ignored(work_dir, members, now=None):
    """把这一组成员**两两**记为"不是重复"。

    :param members: 报告形状的成员（至少要两本）
    :return: 新增的配对条数
    """
    with _WRITE_LOCK:
        entries = read_ignored(work_dir)
        existing = ignore_mod.ignored_keys_from(entries)
        stamp = now or time.strftime('%Y-%m-%d %H:%M:%S')
        by_id = {m.get('id'): m for m in members if m.get('id') is not None}
        added = 0
        for left, right in ignore_mod.pairs_in(list(by_id)):
            if (left, right) in existing:
                continue
            # 指纹两侧都存：扫描时用它察觉"同一个 id 换了另一本书"（见 dedup/ignore.py）
            entries.append({
                'a': left,
                'b': right,
                'at': stamp,
                'titles': {str(left): (by_id[left].get('title') or ''),
                           str(right): (by_id[right].get('title') or '')},
                'sigs': {str(left): ignore_mod.signature(by_id[left]),
                         str(right): ignore_mod.signature(by_id[right])},
            })
            added += 1
        if added:
            _write_ignored(work_dir, entries)
        return added


def remove_ignored(work_dir, pairs=None, all_entries=False):
    """撤销忽略：给了 `pairs` 就只删这几对；`all_entries=True` 清空。

    :return: 删掉的条数
    """
    with _WRITE_LOCK:
        entries = read_ignored(work_dir)
        if all_entries:
            removed = len(entries)
            if removed:
                _write_ignored(work_dir, [])
            return removed
        wanted = {ignore_mod.pair_key(a, b) for a, b in (pairs or [])}
        if not wanted:
            return 0
        kept = [e for e in entries
                if ignore_mod.pair_key(e.get('a', 0), e.get('b', 0)) not in wanted]
        removed = len(entries) - len(kept)
        if removed:
            _write_ignored(work_dir, kept)
        return removed


def live_ignored(work_dir, records_by_id):
    """按当前书库核对忽略名单，**剔掉失效的**并回写（书删了 / id 换了主人）。

    :return: 仍然生效的条目列表
    """
    entries = read_ignored(work_dir)
    if not entries:
        return []
    live = [e for e in entries if ignore_mod.entry_is_live(e, records_by_id)]
    if len(live) != len(entries):
        _write_ignored(work_dir, live)
        logging.info('[book_dedup] ignored list pruned: %d -> %d',
                     len(entries), len(live))
    return live


def ignored_rows(work_dir):
    """忽略名单给界面看的形状（书名 + 配对的规范键）。"""
    rows = []
    for entry in read_ignored(work_dir):
        left, right = entry.get('a'), entry.get('b')
        if left is None or right is None:
            continue
        titles = entry.get('titles') or {}
        rows.append({
            'a': left,
            'b': right,
            'a_title': titles.get(str(left)) or '',
            'b_title': titles.get(str(right)) or '',
            'at': entry.get('at') or '',
        })
    rows.sort(key=lambda row: (row['at'], row['a'], row['b']), reverse=True)
    return rows


def execute_delete(api, work_dir, index_arg, book_id):
    """单独删除一本重复书（写操作，串行化）。

    守门与合并完全一致：只收分组序号 + 一个 book_id，且该 id 必须**是这一组当前的成员**
    ——不接受前端传任意 id，否则一个构造出来的请求就能删掉没被查出来的书。

    :return: ``{'err': 'ok', 'data': {...}}`` 或错误字典
    """
    with _WRITE_LOCK:
        return _execute_delete(api, work_dir, index_arg, book_id)


def _execute_delete(api, work_dir, index_arg, book_id):
    """`execute_delete` 的实现体（调用方持锁）。"""
    # 单删用 `group_members`（不要求至少两本）：把一组删到只剩一本或删光都合法
    members, error = group_members(work_dir, index_arg)
    if error:
        return error

    try:
        wanted = int(book_id)
    except (TypeError, ValueError):
        return {'err': 'params.invalid', 'msg': 'book_id 必须是整数'}

    target = None
    for member in members:
        if member.get('id') == wanted:
            target = member
            break
    if target is None:
        return {'err': 'book.not_in_group', 'msg': '要删除的书必须是这一组里的成员'}

    title = target.get('title') or ''
    # 报告可能是几天前扫的：书在别处被删掉时，别把宿主的"来源书籍不存在"丢给用户，
    # 直接说清楚要重扫。**读不到 ≠ 不存在**，两种要分开说。
    exists = driver.book_exists(api, wanted)
    if exists is None:
        return {'err': 'scope.failed', 'msg': '读取书库失败，请重试'}
    if not exists:
        return {'err': 'book.missing',
                'msg': '这本书已不在书库（可能已在别处被删除），请重新扫描'}
    try:
        driver.delete_book(api, wanted)
    except Exception as err:  # noqa: BLE001
        logging.error('[book_dedup] delete_book(%s) failed: %s', wanted, err)
        return {'err': 'delete.failed', 'msg': '删除失败：%s' % err}

    append_deleted(work_dir, {
        'id': wanted,
        'title': title,
        'at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'index': int(index_arg),
    })
    return {'err': 'ok', 'data': {'deleted_id': wanted, 'title': title}}


def report_signature(index):
    """这份结果的身份：生成时间 + 分组数。前端换了结果要能察觉。"""
    if not index:
        return ''
    return '%s|%s' % (index.get('generated_at') or '', len(index.get('groups') or []))


def recommend_for_members(members, rule=None):
    """对一组（报告形状的）成员重算推荐保留。

    报告里的 `recommendation` 是扫描时算的；一旦有成员被合并掉、或用户换了保留规则，
    就不能沿用旧结论，必须按当前成员重算。
    """
    if not members:
        return {'keeper_id': 0, 'reasons': [], 'protected': [], 'rule': rule or 'metadata'}
    # 报告里的成员字段名与引擎不同（`meta_score` vs `_meta_score`），补齐适配
    adapted = []
    for member in members:
        adapted.append({
            'id': member.get('id'),
            'formats': member.get('formats') or [],
            'size': member.get('size') or 0,
            'added': member.get('added') or '',
            'isbn': member.get('isbn') or '',
            '_meta_score': member.get('meta_score') or 0,
        })
    return keeper_mod.recommend(adapted, rule=rule or 'metadata')


def group_members(work_dir, index_arg):
    """取这一组的当前成员（报告形状），**已消失的（合并掉/单独删掉）不算**。

    不做数量判断——单删一组里的最后一本也是合法操作，只有合并才要求至少剩两本。
    公开给 `/ignore` 用（它也要按同一套守门核对 id 属于这一组）。
    """
    try:
        position = int(index_arg)
    except (TypeError, ValueError):
        return None, {'err': 'params.invalid', 'msg': 'index 必须是整数'}
    report = driver.read_report(work_dir)
    if not report:
        return None, {'err': 'report.not_found', 'msg': '查重结果文件缺失或损坏'}
    groups = report.get('groups') or []
    if position < 0 or position >= len(groups):
        return None, {'err': 'group.not_found', 'msg': '找不到该分组'}
    gone = gone_ids(work_dir)
    members = [m for m in (groups[position].get('members') or [])
               if m.get('id') not in gone]
    return members, None


def _current_members(work_dir, index_arg, gone=None):
    """取这一组的当前成员，并要求**至少两本**（合并的前提）。"""
    members, error = group_members(work_dir, index_arg)
    if error:
        return None, error
    if len(members) < 2:
        return None, {'err': 'group.already_merged',
                      'msg': '这一组已经处理过，只剩一本或没有可合并的成员'}
    return members, None


def _parse_ids(value):
    """解析"要合并哪几本"的勾选集。

    **`None` 与 `[]` 必须分开**：
    ``None`` = 前端没传这个字段（沿用 0.1.4 的行为：除保留项外全部）；
    ``[]``   = 一本都没勾（什么都不做）。把后者当成前者，前端一次状态丢失就会
    静悄悄把整组并掉——这是本函数存在的全部理由。

    :raises ValueError: 值不是整数 / 整数列表（含 `True` 这类 bool——它在 Python 里
        是 int，JSON 里的 `true` 会被当成 id=1）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('bool is not a book id')
    if isinstance(value, int):
        raw = [value]
    elif isinstance(value, str):
        raw = value.split(',')
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raise ValueError('unsupported source_ids type')

    ids = []
    for item in raw:
        if isinstance(item, bool):
            raise ValueError('bool is not a book id')
        if isinstance(item, int):
            ids.append(item)
            continue
        text = str(item).strip()
        if not text:
            continue
        ids.append(int(text))       # 非数字在这里抛 ValueError，由调用方转成 params.invalid
    return ids


def build_plan(api, work_dir, index_arg, keeper_id=None, keep_rule=None,
               task_id=None, source_ids=None):
    """合并预览：只算不写。返回 (plan, error)。

    :param source_ids: 用户勾选"要合并掉"的成员 id；``None`` = 除保留项外全部。
        **仍然只信"分组序号 + 本次扫描的成员"**：每个勾选的 id 都要回到这一组当前的
        成员里核对，不接受前端直接给一个 id 列表去删任意书（本模块开头那条规矩）。
    """
    members, error = _current_members(work_dir, index_arg)
    if error:
        return None, error

    try:
        selected = _parse_ids(source_ids)
    except ValueError:
        return None, {'err': 'params.invalid', 'msg': 'source_ids 必须是整数列表'}

    recommendation = recommend_for_members(members, keep_rule)
    resolved_keeper = recommendation['keeper_id']
    if keeper_id not in (None, '', 'null'):
        try:
            wanted = int(keeper_id)
        except (TypeError, ValueError):
            return None, {'err': 'params.invalid', 'msg': 'keeper_id 必须是整数'}
        if wanted not in [m.get('id') for m in members]:
            return None, {'err': 'keeper.not_in_group',
                          'msg': '保留项必须是这一组里的成员'}
        resolved_keeper = wanted
        recommendation = recommend_for_members(members, 'metadata')
        recommendation['keeper_id'] = wanted
        recommendation['reasons'] = [{'code': 'manual'}]
        recommendation['rule'] = 'manual'

    member_ids = [m.get('id') for m in members]
    if selected is not None:
        foreign = [book_id for book_id in selected if book_id not in member_ids]
        if foreign:
            return None, {'err': 'source.not_in_group',
                          'msg': '这些书不属于这一组：%s'
                                 % '、'.join(str(i) for i in foreign)}
        if resolved_keeper in selected:
            return None, {'err': 'source.is_keeper',
                          'msg': '保留项不能同时作为要合并掉的那一本'}
        selected = [book_id for book_id in selected if book_id != resolved_keeper]
        if not selected:
            return None, {'err': 'merge.nothing', 'msg': '没有勾选任何要合并的书'}

    # 保留项必须**现在**还在书库。报告是跨重启持久化的，可能几天前扫的；期间用户很可能
    # 正是在工具卡片上点"打开书籍页"把它删了/并了。这一步以前没有，后果是
    # `merge_formats` 抛"目标书籍不存在"被当成"无需合并"，然后把源记录删光。
    exists = driver.book_exists(api, resolved_keeper)
    if exists is None:
        # "读不到"≠"不存在"：别把宿主/数据库的问题说成"书没了"（re-review 时发现的）
        return None, {'err': 'scope.failed', 'msg': '读取书库失败，请重试'}
    if not exists:
        return None, {'err': 'keeper.missing',
                      'msg': '保留项已不在书库（可能已在别处被删除或合并），请重新扫描'}

    plan = driver.merge_plan(api, _to_engine_members(members), resolved_keeper,
                             source_ids=selected)
    if plan.get('error'):
        return None, {'err': plan['error'], 'msg': '保留项不在成员里'}
    plan['index'] = int(index_arg)
    plan['task_id'] = None if task_id in (None, '') else str(task_id)
    plan['recommendation'] = recommendation
    plan['members'] = members
    # 实际会合并掉的 id（勾选口径；缺省时就是"除保留项外全部"）。
    # 回给前端的是**将发生的事**，不是它送来的那一串——前端据此把计划与勾选态对齐。
    plan['source_ids'] = [step['source_id'] for step in plan['steps']]
    return plan, None


def _to_engine_members(members):
    """报告形状的成员 → 引擎形状（`driver.merge_plan` 与 keeper 都按引擎字段取数）。"""
    adapted = []
    for member in members:
        adapted.append({
            'id': member.get('id'),
            'title': member.get('title') or '',
            'formats': member.get('formats') or [],
            'size': member.get('size') or 0,
            'added': member.get('added') or '',
            'isbn': member.get('isbn') or '',
            '_meta_score': member.get('meta_score') or 0,
        })
    return adapted


def to_diff_members(members):
    """报告形状的成员 → 对照表（`diff.build_table`）要的形状。

    报告的成员字段几乎就是对照表的口径（它本来就由 `report.build_member` 按同一批字段裁的），
    **只差一个键名**：报告里叫 `meta_score`，对照表读 `_meta_score`。少了这一手，
    "元数据"整行会全变 0，被当成"两份相同"折叠掉。
    """
    return [dict(member, _meta_score=member.get('meta_score') or 0) for member in members]


def execute(api, work_dir, plan, delete_source=True):
    """按计划执行合并（写操作，串行化）。返回 ``{'err': 'ok', 'data': {...}}`` 或错误字典。"""
    with _WRITE_LOCK:
        return _execute(api, work_dir, plan, delete_source)


def _execute(api, work_dir, plan, delete_source=True):
    """`execute` 的实现体（调用方持锁）。"""
    keeper_id = plan.get('keeper_id')
    steps = plan.get('steps') or []
    if not steps:
        return {'err': 'merge.nothing', 'msg': '没有可合并的成员'}

    results = []
    removed = []
    for step in steps:
        source_id = step.get('source_id')
        try:
            outcome = driver.merge_group(api, source_id, keeper_id,
                                         delete_source=delete_source)
        except Exception as err:  # noqa: BLE001
            # 取数/核对阶段的意外异常（例如宿主 db 读失败）不能冒到 handler 变成 500：
            # 这一步记成失败、**不删任何东西**，界面与台账都如实显示。
            logging.error('[book_dedup] merge step %s failed: %s', source_id, err, exc_info=True)
            outcome = {'merged': [], 'deleted': False, 'notes': ['failed'],
                       'error': 'merge.failed', 'message': str(err)}
        entry = {
            'source_id': source_id,
            'source_title': step.get('source_title') or '',
            'moved_formats': outcome.get('merged') or [],
            'deleted': bool(outcome.get('deleted')),
            'notes': outcome.get('notes') or [],
        }
        if outcome.get('error') and not outcome.get('deleted'):
            entry['error'] = outcome['error']
            entry['message'] = outcome.get('message') or ''
        else:
            if outcome.get('deleted'):
                removed.append({'id': source_id, 'title': step.get('source_title') or ''})
        results.append(entry)

    entry = {
        'at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'index': plan.get('index'),
        'keeper_id': keeper_id,
        'keeper_title': plan.get('keeper_title') or '',
        'removed_ids': [item['id'] for item in removed],
        'removed': removed,
        'steps': results,
        'delete_source': bool(delete_source),
        # 用户**没勾**的那些（原样保留）。记账里也要有：否则事后只看这份台账，
        # 会以为这一组已经处理干净了
        'kept': plan.get('kept') or [],
    }
    append_merged(work_dir, entry)

    failed = [r for r in results if r.get('error')]
    kept = plan.get('kept') or []
    data = {
        'keeper_id': keeper_id,
        'keeper_title': plan.get('keeper_title') or '',
        'removed_ids': [item['id'] for item in removed],
        'steps': results,
        'moved_total': sum(len(r['moved_formats']) for r in results),
        'dropped_total': plan.get('dropped_total', 0),
        'kept': kept,
        'failed': len(failed),
        'warnings': ['source_records_not_migrated'],
    }
    if failed and len(failed) == len(results):
        return {'err': 'merge.failed', 'msg': '合并失败，请查看日志', 'data': data}
    return {'err': 'ok', 'data': data}