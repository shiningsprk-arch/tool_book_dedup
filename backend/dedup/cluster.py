# -*- coding: utf-8 -*-
"""候选择配对与分组。

两档证据，强度分级（BookOrbit 的 REASON_RANK 思路）：

    file_hash(4) > isbn(3) > exact_metadata(2) > fuzzy_metadata(1)

本期的实际取值是 `isbn`(3) / `fuzzy_metadata`(1) / `weak_title_only`(0)；
`exact_metadata` 与 `file_hash` 在常量里占位，等按需精确核对那一期接上。

**性能约束是这个文件存在的主要理由**：宿主仓库自带 `tests/cases/big-metadata.db`
是 24,835 本，两两比较是 3 亿次。所以候选生成不两两比，而是走"短路链"：

    作者桶（倒排，O(n)）→ 桶内两两 → 卷册序号排除 → 标题 Dice 过阈值 → ISBN 弱命中可独立成对

作者不同的一对**根本不会进入标题比较**。中文个人书库作者字段常常为空，那类书会全部
落进同一个"无作者"桶（`''`），桶内仍是两两——所以对超大桶设上限并记录被跳过量，宁可不比，
也不让一次扫描把服务拖住。

**卷册序号排除**（`differs_only_by_serial`）是第二道闸：`德川家康（第一部）` 与
`德川家康（第十二部）` 的主书名完全相同，不论阈值多高都会被算成 1.0，靠阈值是拦不住的，
必须在配对前就把"只差一个卷号"的对剔掉。剔掉多少对会记进 `stats['skipped_serial']`，
在报告里如实交代（不静默吞掉）。
"""
from .normalize import (families_of, isbn_key, title_core, title_key, author_keys,
                        differs_only_by_serial)
from .similarity import SimilarityCache

# 匹配理由 → 强度等级（数字越大越强）
REASON_RANK = {
    'file_hash': 4,
    'isbn': 3,
    'exact_metadata': 2,
    'fuzzy_metadata': 1,
    'weak_title_only': 0,
}
RANK_REASON = {v: k for k, v in REASON_RANK.items()}

# 一个作者桶内最多做多少次两两比较；超过就跳过并记账（见模块 docstring）。
DEFAULT_MAX_BUCKET = 3000


def prepare(record):
    """给一条书籍记录补上归一化 key（`_title_key` / `_title_core` / `_author_keys` / `_isbn_key`）。

    只做一次，后面的配对、展示、报告都复用这几个 key——避免同一个书名被反复归一化。
    `_title_core` 是剥掉尾部括号注记的主书名，扫描时与完整标题一起参与判定。
    """
    record['_title_key'] = title_key(record.get('title'))
    record['_title_core'] = title_key(title_core(record.get('title')))
    record['_author_keys'] = author_keys(record.get('authors'))
    record['_isbn_key'] = isbn_key(
        record.get('isbn10'), record.get('isbn13'), record.get('isbn'))
    return record


def _make_pair(a, b, reasons, similarity=None):
    """规范化成对的 Pair（小的 id 在前，保证一对只有一个表示）。"""
    if a['id'] > b['id']:
        a, b = b, a
    return {
        'a': a['id'],
        'b': b['id'],
        'reasons': list(reasons),
        'similarity': similarity,
    }


def _strongest(reasons):
    if not reasons:
        return 0
    return max(REASON_RANK.get(r, 0) for r in reasons)


def candidate_pairs(records, threshold=0.85, max_bucket=DEFAULT_MAX_BUCKET):
    """从全量记录里找出候选重复对。

    :param records:   已经 `prepare()` 过的记录列表
    :param threshold: 标题相似度阈值（0~1），原值会写进报告便于标定
    :param max_bucket: 单个作者桶的规模上限，超过则跳过该桶并记账
    :return: `(pairs, stats)`；stats 含比较次数与被跳过的桶，用于诚实交代扫描代价
    """
    pairs = {}
    stats = {
        'comparisons': 0,
        'skipped_buckets': 0,
        'skipped_books': 0,
        'skipped_serial': 0,
        'max_bucket': max_bucket,
    }
    cache = SimilarityCache()

    # --- 第一档：ISBN key 相同且媒体家族重叠 → 直接成对（强证据，与标题无关）
    by_isbn = {}
    for record in records:
        key = record.get('_isbn_key')
        if key:
            by_isbn.setdefault(key, []).append(record)
    for group in by_isbn.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left, right = group[i], group[j]
                if not (families_of(left['formats']) & families_of(right['formats'])):
                    continue  # 同一本书的 epub 与 mp3 不是重复
                pair = _make_pair(left, right, ['isbn'], 1.0)
                _merge_pair(pairs, pair)

    # --- 第二档：作者桶内比较标题
    buckets = {}
    for record in records:
        # 没有作者（或作者字段是空串）的书也要进桶：都落进同一个 "" 桶里比较，
        # 而不是被排除在标题比较之外。这类书在个人书库里不少，漏掉就是整批漏掉。
        # 桶上限（`max_bucket`）照样守着最坏情况：一个几千本的"无作者"桶会被跳过并记账。
        for key in (record.get('_author_keys') or ('',)):
            buckets.setdefault(key, []).append(record)

    for members in buckets.values():
        if len(members) < 2:
            continue
        size = len(members)
        if size > max_bucket:
            stats['skipped_buckets'] += 1
            stats['skipped_books'] += size
            continue
        for i in range(size):
            left = members[i]
            left_key = left.get('_title_key')
            if not left_key:
                continue
            for j in range(i + 1, size):
                right = members[j]
                right_key = right.get('_title_key')
                if not right_key:
                    continue
                if differs_only_by_serial(left_key, right_key):
                    # 只差卷号/期号 → 同一套书的不同卷，不是重复。记账，别静默吞掉
                    stats['skipped_serial'] += 1
                    continue
                stats['comparisons'] += 1
                score = cache.dice(left_key, right_key,
                                   left.get('_title_core'), right.get('_title_core'))
                if score < threshold:
                    continue
                if left.get('_isbn_key') and right.get('_isbn_key') \
                        and left['_isbn_key'] != right['_isbn_key']:
                    # 两本书号都有效但不同 → 是不同版本，降级而不是直接丢：
                    # 仍然成对，但理由只剩模糊匹配，让用户自己判断
                    reasons = ['fuzzy_metadata']
                else:
                    reasons = ['fuzzy_metadata', 'weak_title_only']
                _merge_pair(pairs, _make_pair(left, right, reasons, score))

    return list(pairs.values()), stats


def _merge_pair(pairs, pair):
    """把一对合并进结果：同一对出现多次时保留最强理由与最高相似度。"""
    key = (pair['a'], pair['b'])
    existing = pairs.get(key)
    if existing is None:
        pairs[key] = pair
        return
    merged = set(existing['reasons']) | set(pair['reasons'])
    # 只有所有理由都相同才算同一档；否则取更强的那一档
    if _strongest(pair['reasons']) > _strongest(existing['reasons']):
        existing['reasons'] = pair['reasons']
    elif _strongest(pair['reasons']) == _strongest(existing['reasons']):
        existing['reasons'] = sorted(merged)
    if pair.get('similarity') is not None:
        current = existing.get('similarity')
        if current is None or pair['similarity'] > current:
            existing['similarity'] = pair['similarity']


class _UnionFind(object):
    """并查集：把成对关系聚成组。"""

    def __init__(self):
        self._parent = {}

    def find(self, item):
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        # 路径压缩
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left, right):
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self._parent[root_right] = root_left

    def groups(self):
        result = {}
        for item in list(self._parent):
            result.setdefault(self.find(item), []).append(item)
        return result


def group_pairs(pairs, by_id):
    """把成对关系聚成组，返回按"证据强度 → 成员数 → 相似度"排序的组列表。

    :param by_id: {book_id: record}，用来取成员与算可回收体积
    """
    union = _UnionFind()
    for pair in pairs:
        union.union(pair['a'], pair['b'])

    grouped = union.groups()
    member_pairs = {}
    for pair in pairs:
        member_pairs.setdefault(union.find(pair['a']), []).append(pair)

    groups = []
    for root, member_ids in grouped.items():
        if len(member_ids) < 2:
            continue
        related = member_pairs.get(root, [])
        reasons = set()
        for pair in related:
            reasons |= set(pair['reasons'])
        strongest = _strongest(reasons)
        best_similarity = max((p.get('similarity') or 0.0) for p in related) if related else 0.0

        members = [by_id[bid] for bid in sorted(member_ids) if bid in by_id]
        sizes = [int(m.get('size') or 0) for m in members]
        groups.append({
            'members': [m['id'] for m in members],
            'reasons': sorted(reasons, key=lambda r: -REASON_RANK.get(r, 0)),
            'confidence': RANK_REASON.get(strongest, 'weak_title_only'),
            'confidence_rank': strongest,
            'max_similarity': round(best_similarity, 4),
            'member_count': len(members),
            'bytes_total': sum(sizes),
            'bytes_max': max(sizes) if sizes else 0,
            'reclaimable_bytes': sum(sizes) - (max(sizes) if sizes else 0),
        })

    groups.sort(key=lambda g: (-g['confidence_rank'], -g['member_count'], -g['max_similarity']))
    return groups