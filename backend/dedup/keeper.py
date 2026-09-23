# -*- coding: utf-8 -*-
"""保留判断：推荐留哪一本，以及**为什么**。

形态照 BookOrbit 的 `duplicate-keeper.ts`：规则是元组比较（主键相同才看次键，所以不存在
"分数打平怎么办"），理由是**数据不是句子**（`{'code': 'metadata', 'score': 82}`），
前端按 i18n 渲染。原注释那句话是这套设计最好的说明——"a rule is only trustworthy when it
can say what decided it"。**本文件 clean-room 重写，字段与权重按 MyBooks 口径。**

`protected`（带阅读进度/书单的副本不许悄悄删）在本期是**空实现**——工具箱读不到用户数据
（`AppDBAPI` 只有 4 个方法）。函数与常量先留在位，等宿主开放只读接口再接上，而不是到时候
再改结构。
"""

KEEP_RULES = ('metadata', 'formats', 'size', 'oldest', 'newest')

RULE_LABELS = {
    'metadata': '元数据最全',
    'formats': '格式最多',
    'size': '体积最大',
    'oldest': '最早入库',
    'newest': '最新入库',
}


def _format_count(record):
    return len(record.get('formats') or [])


def _size(record):
    return int(record.get('size') or 0)


def _added(record):
    """入库时间转成可比较的数字；缺失按 0 处理（视作最老）。"""
    value = record.get('added')
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        import datetime
        text = str(value).replace('Z', '+00:00')
        parsed = datetime.datetime.fromisoformat(text)
        return parsed.timestamp()
    except Exception:
        return 0.0


def rule_vector(record, rule):
    """按规则给出比较向量：越大越好，故"最早入库"用负数。"""
    score = int(record.get('_meta_score') or 0)
    if rule == 'size':
        return (_size(record), score, _format_count(record))
    if rule == 'formats':
        return (_format_count(record), score, _size(record))
    if rule == 'oldest':
        return (-_added(record), score)
    if rule == 'newest':
        return (_added(record), score)
    # metadata（默认）
    return (score, _format_count(record), _size(record), -_added(record))


def protected_members(records):
    """带用户数据、不应被悄悄丢弃的副本。

    **本期恒为空**：工具箱读不到收藏/在读/阅读进度（`CoreAPI.db` 只有
    `get_item_by_book_id`/`create_item`/`delete_item_by_book_id`/`get_reader`）。
    留这个函数是让"保护"这条规则只有一个入口，将来宿主开放只读接口时改这里。
    """
    return []


def recommend(records, rule='metadata', manual_id=None):
    """推荐保留哪一本。

    :param rule:      `KEEP_RULES` 之一
    :param manual_id: 用户手动点选的 id（优先于规则）
    :return: ``{'keeper_id': int, 'reasons': [...], 'protected': [...], 'rule': str}``
    """
    if not records:
        return {'keeper_id': 0, 'reasons': [], 'protected': [], 'rule': rule}

    if manual_id is not None and any(r['id'] == manual_id for r in records):
        return {
            'keeper_id': manual_id,
            'reasons': [{'code': 'manual'}],
            'protected': [],
            'rule': 'manual',
        }

    if rule not in KEEP_RULES:
        rule = 'metadata'

    protected = protected_members(records)
    reasons = []

    # 只有一个副本带用户数据 → 它直接胜出，规则不再参与
    # （"freeing bytes never justifies deleting the copy someone is part way through"）
    if len(protected) == 1:
        keeper = protected[0]
        reasons.append({'code': 'protected'})
    else:
        keeper = max(records, key=lambda r: rule_vector(r, rule))
        reasons.extend(_explain(records, keeper, rule))
        if len(protected) > 1:
            # 多个副本都带用户数据 → 规则无法安全二选一，把警告留给界面
            reasons.append({'code': 'multi_protected'})

    if not reasons:
        reasons.append({'code': 'added', 'at': str(keeper.get('added') or '')})

    return {
        'keeper_id': keeper['id'],
        'reasons': reasons,
        'protected': [r['id'] for r in protected],
        'rule': rule,
    }


def _explain(records, keeper, rule):
    """给出人话理由。只陈述"确实优于其它成员"的那些项，不硬凑。"""
    reasons = []
    others = [r for r in records if r['id'] != keeper['id']]
    score = int(keeper.get('_meta_score') or 0)
    if score and any(int(r.get('_meta_score') or 0) < score for r in others):
        reasons.append({'code': 'metadata', 'score': score})

    count = _format_count(keeper)
    if count and any(_format_count(r) < count for r in others):
        reasons.append({'code': 'formats', 'count': count})

    size = _size(keeper)
    if size and any(_size(r) < size for r in others):
        reasons.append({'code': 'size', 'bytes': size})

    if _isbn_of(keeper) and any(not _isbn_of(r) for r in others):
        reasons.append({'code': 'isbn'})

    added = _added(keeper)
    if added:
        if rule == 'newest':
            reasons.insert(0, {'code': 'newest', 'at': str(keeper.get('added') or '')})
        elif rule == 'oldest':
            reasons.insert(0, {'code': 'oldest', 'at': str(keeper.get('added') or '')})
    return reasons


def _isbn_of(record):
    return record.get('isbn') or record.get('isbn13') or record.get('isbn10')


def reclaimable_bytes(records, keeper_id):
    """保留 keeper 之后可回收的字节数（其余成员的总体积）。"""
    return sum(_size(r) for r in records if r['id'] != keeper_id)


def discard_members(records, keeper_id):
    """会被丢弃的成员。"""
    return [r for r in records if r['id'] != keeper_id]


def duplicate_disk_waste(records, keeper_id):
    """"重复占用"：被丢弃成员中，与 keeper **同格式**那部分的体积。

    这一数字比"可回收字节"诚实：合并会把 keeper 缺的格式搬过去（旧记录仍占着它的文件），
    所以真正会因为合并而消失的磁盘占用，只是两边同名格式的那一份。
    """
    keeper_formats = set(f.upper() for f in _formats_of(records, keeper_id))
    waste = 0
    for record in discard_members(records, keeper_id):
        formats = set(f.upper() for f in (record.get('formats') or []))
        if formats and formats <= keeper_formats:
            waste += _size(record)
    return waste


def _formats_of(records, book_id):
    for record in records:
        if record['id'] == book_id:
            return record.get('formats') or []
    return []