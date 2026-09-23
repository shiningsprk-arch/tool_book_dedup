# -*- coding: utf-8 -*-
"""合并的执行与记账。

这是整个工具里**唯一会写书库**的地方，所以规则写得比别处死：

1. **成员来自本次扫描，不接受前端传 id 列表。** `/merge` 只收一个分组序号，
   服务端自己回到那次扫描的索引里取成员——否则一个被篡改或过期的列表就能去删
   没被查出来的书。
2. **保留项必须在成员里**，且必须是本次扫描算出的成员之一。
3. **先预览再执行**：`build_plan()` 与 `execute()` 用同一份计划，预览里已经列明
   哪些格式会被复制、哪些同格式会被丢弃。
4. **执行后记账**：被删掉的 id 写进 `merged.json`，列表与会话据此把它们摘掉——
   合并是真的删记录，留在列表里会引导用户再点一次。
"""
import json
import logging
import os
import time

from . import driver
from .dedup import keeper as keeper_mod

MERGED_FILENAME = 'merged.json'


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


def append_merged(work_dir, entry):
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


def _current_members(work_dir, index_arg, merged_ids):
    """取这一组的当前成员（报告形状），已合并掉的不算。"""
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
    group = groups[position]
    members = [m for m in (group.get('members') or [])
               if m.get('id') not in merged_ids]
    if len(members) < 2:
        return None, {'err': 'group.already_merged',
                      'msg': '这一组已经处理过，只剩一本或没有可合并的成员'}
    return members, None


def build_plan(api, work_dir, index_arg, keeper_id=None, keep_rule=None,
               task_id=None):
    """合并预览：只算不写。返回 (plan, error)。"""
    merged_ids = set(read_merged_ids(work_dir))
    members, error = _current_members(work_dir, index_arg, merged_ids)
    if error:
        return None, error

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

    plan = driver.merge_plan(api, _to_engine_members(members), resolved_keeper)
    if plan.get('error'):
        return None, {'err': plan['error'], 'msg': '保留项不在成员里'}
    plan['index'] = int(index_arg)
    plan['task_id'] = task_id or {}
    plan['recommendation'] = recommendation
    plan['members'] = members
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


def execute(api, work_dir, plan, delete_source=True):
    """按计划执行合并。返回 ``{'err': 'ok', 'data': {...}}`` 或错误字典。"""
    keeper_id = plan.get('keeper_id')
    steps = plan.get('steps') or []
    if not steps:
        return {'err': 'merge.nothing', 'msg': '没有可合并的成员'}

    results = []
    removed = []
    for step in steps:
        source_id = step.get('source_id')
        outcome = driver.merge_group(api, source_id, keeper_id,
                                     delete_source=delete_source)
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
    }
    append_merged(work_dir, entry)

    failed = [r for r in results if r.get('error')]
    data = {
        'keeper_id': keeper_id,
        'keeper_title': plan.get('keeper_title') or '',
        'removed_ids': [item['id'] for item in removed],
        'steps': results,
        'moved_total': sum(len(r['moved_formats']) for r in results),
        'dropped_total': plan.get('dropped_total', 0),
        'failed': len(failed),
        'warnings': ['source_records_not_migrated'],
    }
    if failed and len(failed) == len(results):
        return {'err': 'merge.failed', 'msg': '合并失败，请查看日志', 'data': data}
    return {'err': 'ok', 'data': data}