# -*- coding: utf-8 -*-
"""报告：形状、汇总（全部纯函数，可离线测）。"""
import time

from . import diff as diff_mod
from . import keeper as keeper_mod
# 报告里每条成员记录向外暴露的字段——刻意收窄，避免把整份数据字典塞进报告文件。
# （注意：简介只带 `comments_present` 这个布尔，正文不进报告——报告会被整份读进内存。）
_MEMBER_FIELDS = (
    'id', 'title', 'authors', 'formats', 'size', 'added', 'isbn', 'isbn13', 'isbn10',
    'comments_present', 'tags', 'series', 'series_index', 'publisher', 'pubdate',
    'languages', 'rating', 'has_cover', 'collector_id', 'collector_name', 'sole',
    'book_type', 'translators',
)

CONFIDENCE_LABELS = {
    'strong': '确定',
    'likely': '很可能',
    'weak': '存疑',
    'certain': '完全相同',
}

# 模糊命中里，"很可能"与"存疑"的分界：相似度 ≥ 这个值才算很可能。
# 以前 rank 1 一律叫"很可能"，而"存疑"在候选生成里根本不可能出现——于是
# 界面上的"存疑"筛选器永远筛不出东西、真书库 1584 组里 1582 组都显示"很可能"。
SIMILARITY_LIKELY = 0.95


def confidence_of(rank, max_similarity=None):
    """证据强度 → 对外三档（确定 / 很可能 / 存疑）。

    - rank ≥ 3：ISBN 相同这类强证据 → **确定**
    - rank == 2：元数据完全相同（当前候选生成未产出，占位）→ **很可能**
    - rank == 1：模糊标题命中 → 相似度 ≥ `SIMILARITY_LIKELY` 才叫**很可能**，否则**存疑**
    - rank == 0：仅标题相似 → **存疑**
    """
    if rank >= 3:
        return 'strong'
    if rank == 2:
        return 'likely'
    if rank == 1:
        similarity = 1.0 if max_similarity is None else max_similarity
        return 'likely' if similarity >= SIMILARITY_LIKELY else 'weak'
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
        confidence = confidence_of(group['confidence_rank'], group['max_similarity'])
        out_groups.append({
            'index': index,
            'members': [build_member(m) for m in members],
            'reasons': group['reasons'],
            'confidence': confidence,
            'confidence_label': CONFIDENCE_LABELS.get(confidence, ''),
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
    stats = stats or {}
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
            # 因为"只差一个卷号/期号"被排除的候选对（不静默吞掉，界面上如实说明）
            'serial_excluded': int(stats.get('skipped_serial') or 0),
            # 因为"一边是实体书、一边是电子书"被排除的候选对（用户口径：不跨类判断）
            'cross_type_excluded': int(stats.get('skipped_cross_type') or 0),
            # 本次因为忽略名单而没有成组的候选对，以及名单里已经失效的条目数
            'ignored_excluded': int(stats.get('ignored_excluded') or 0),
            'ignored_stale': int(stats.get('ignored_stale') or 0),
        },
        'groups': out_groups,
    }


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