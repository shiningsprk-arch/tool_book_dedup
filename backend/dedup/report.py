# -*- coding: utf-8 -*-
"""报告：形状、汇总、筛选（全部纯函数，可离线测）。"""
import time

from . import diff as diff_mod
from . import keeper as keeper_mod
from .cluster import REASON_RANK
from .metadata import score as meta_score

# 报告里每条成员记录向外暴露的字段——刻意收窄，避免把整份数据字典塞进报告文件。
_MEMBER_FIELDS = (
    'id', 'title', 'authors', 'formats', 'size', 'added', 'isbn', 'isbn13', 'isbn10',
    'comments_present', 'tags', 'series', 'series_index', 'publisher', 'pubdate',
    'languages', 'rating', 'has_cover', 'collector_id', 'collector_name', 'sole',
    'book_type', 'translators',
)

REASON_LABELS = {
    'file_hash': '文件完全相同',
    'isbn': 'ISBN 相同',
    'exact_metadata': '元数据完全相同',
    'fuzzy_metadata': '标题相似',
    'weak_title_only': '仅标题相似',
}

CONFIDENCE_LABELS = {
    'strong': '确定',
    'likely': '很可能',
    'weak': '存疑',
    'certain': '完全相同',
}

# 对外用的三档（强度 3/2 都叫"确定"，0 叫"存疑"）
def confidence_of(rank):
    if rank >= 3:
        return 'strong'
    if rank == 2:
        return 'likely'
    if rank == 1:
        return 'likely'
    return 'weak'


def build_member(record):
    """把引擎内部的成员记录裁成报告里的形状。"""
    member = {}
    for field in _MEMBER_FIELDS:
        if field in record:
            member[field] = record[field]
    member['meta_score'] = int(record.get('_meta_score') or 0)
    member['missing_fields'] = list(record.get('_missing') or [])
    member.setdefault('formats', [])
    member.setdefault('size', 0)
    return member


def build_report(records, pairs, groups, threshold, stats, scope_note=''):
    """组装完整报告。

    :param records: 已经 `prepare()` + 评过分的记录
    :param pairs:   `cluster.candidate_pairs` 的输出
    :param groups:  `cluster.group_pairs` 的输出
    """
    by_id = {record['id']: record for record in records}
    out_groups = []
    for index, group in enumerate(groups):
        members = [by_id[bid] for bid in group['members'] if bid in by_id]
        if len(members) < 2:
            continue
        recommendation = keeper_mod.recommend(members)
        reclaimable = keeper_mod.reclaimable_bytes(members, recommendation['keeper_id'])
        out_groups.append({
            'index': index,
            'members': [build_member(m) for m in members],
            'reasons': group['reasons'],
            'reason_labels': [REASON_LABELS.get(r, r) for r in group['reasons']],
            'confidence': confidence_of(group['confidence_rank']),
            'confidence_label': CONFIDENCE_LABELS.get(
                confidence_of(group['confidence_rank']), ''),
            'max_similarity': group['max_similarity'],
            'member_count': group['member_count'],
            'bytes_total': group['bytes_total'],
            'bytes_max': group['bytes_max'],
            'reclaimable_bytes': reclaimable,
            'disk_waste_bytes': keeper_mod.duplicate_disk_waste(
                members, recommendation['keeper_id']),
            'recommendation': recommendation,
            'diff': diff_mod.build_table(members),
        })

    total_extra = sum(g['member_count'] - 1 for g in out_groups)
    return {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'threshold': threshold,
        'scope_note': scope_note,
        'scanned_books': len(records),
        'stats': stats,
        'summary': {
            'group_count': len(out_groups),
            'extra_copies': total_extra,
            'reclaimable_bytes': sum(g['reclaimable_bytes'] for g in out_groups),
            'disk_waste_bytes': sum(g['disk_waste_bytes'] for g in out_groups),
            'strong_groups': sum(1 for g in out_groups if g['confidence'] == 'strong'),
            'weak_groups': sum(1 for g in out_groups if g['confidence'] == 'weak'),
        },
        'groups': out_groups,
    }


def filter_report(report, confidence=None, min_members=None, keyword=None):
    """筛选报告里的组，返回 (新报告, 命中组数)。

    与原报告的 `summary` 分开：`summary` 始终是**全量**统计，筛选只影响 `groups`，
    同时额外回一个 `filtered` 计数——否则界面上的数字会互相矛盾。
    """
    groups = report.get('groups') or []
    result = list(groups)
    if confidence:
        wanted = set(confidence if isinstance(confidence, (list, tuple, set)) else [confidence])
        result = [g for g in result if g['confidence'] in wanted]
    if min_members:
        try:
            minimum = int(min_members)
        except (TypeError, ValueError):
            minimum = 0
        if minimum > 1:
            result = [g for g in result if g['member_count'] >= minimum]
    if keyword:
        needle = str(keyword).strip().lower()
        if needle:
            def _hit(group):
                for member in group['members']:
                    if needle in str(member.get('title') or '').lower():
                        return True
                    for author in member.get('authors') or []:
                        if needle in str(author).lower():
                            return True
                return False
            result = [g for g in result if _hit(g)]

    filtered = dict(report)
    filtered['groups'] = result
    filtered['filtered'] = {
        'group_count': len(result),
        'extra_copies': sum(g['member_count'] - 1 for g in result),
        'reclaimable_bytes': sum(g['reclaimable_bytes'] for g in result),
    }
    return filtered, len(result)


def summarise_for_toolbar(report):
    """给进度/汇总条用的小载荷（不含成员明细，避免进 progress_data）。"""
    summary = dict(report.get('summary') or {})
    summary['scanned_books'] = report.get('scanned_books', 0)
    summary['threshold'] = report.get('threshold')
    return summary


def paginate(groups, page=0, size=50):
    """分页，返回 (切片, 总组数)。"""
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0
    try:
        size = min(500, max(1, int(size)))
    except (TypeError, ValueError):
        size = 50
    start = page * size
    return groups[start:start + size], len(groups)