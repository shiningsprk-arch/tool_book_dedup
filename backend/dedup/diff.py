# -*- coding: utf-8 -*-
"""对照：只展开"有差异"的字段。

思路取自 BookOrbit 的 `duplicate-diff.ts`（"copies of one book agree about most things,
so the comparison only earns its space when it drops the fields that are the same
everywhere"）。**本文件是 clean-room 重写**，字段集换成 MyBooks 口径。

这一层直接解决"两份元数据可能完全一致"：一致就不出对照行，界面自然没有噪音，
差异会落到入库时间与条目属性上，用户一眼就能定。
"""

# 参与对照的字段：字段名 → (中文名, 取值函数)。取值函数必须返回可比较的标量/元组。
FIELD_ACCESSORS = {
    'size': ('体积', lambda r: int(r.get('size') or 0)),
    'formats': ('格式', lambda r: tuple(sorted(f.upper() for f in (r.get('formats') or [])))),
    'isbn': ('ISBN', lambda r: r.get('isbn') or r.get('isbn13') or r.get('isbn10') or ''),
    'metadata': ('元数据', lambda r: r.get('_meta_score', 0)),
    'added': ('入库时间', lambda r: str(r.get('added') or '')),
    'rating': ('评分', lambda r: r.get('rating') or 0),
    'tags': ('标签', lambda r: tuple(sorted(r.get('tags') or []))),
    'series': ('系列', lambda r: r.get('series') or ''),
    'publisher': ('出版社', lambda r: r.get('publisher') or ''),
    'languages': ('语言', lambda r: tuple(sorted(r.get('languages') or []))),
    'translators': ('译者', lambda r: tuple(sorted(r.get('translators') or []))),
    'collector': ('收藏者', lambda r: r.get('collector_name') or r.get('collector_id') or ''),
    'sole': ('私有', lambda r: bool(r.get('sole'))),
    'book_type': ('书目类型', lambda r: r.get('book_type')),
}



def _signature(record, field):
    return FIELD_ACCESSORS[field][1](record)


def field_is_shared(records, field):
    """该字段在所有成员里取值是否完全一致（一致 → 折叠，不一致 → 展开）。"""
    values = set()
    for record in records:
        value = _signature(record, field)
        try:
            values.add(value)
        except TypeError:  # 不可哈希（理论上不会，取值函数已返回元组/标量）
            values.add(repr(value))
        if len(values) > 1:
            return False
    return True


def shared_fields(records):
    return [f for f in FIELD_ACCESSORS if field_is_shared(records, f)]


def differing_fields(records):
    return [f for f in FIELD_ACCESSORS if not field_is_shared(records, f)]


def summary_sentence(records):
    """把"全都一样"的字段折成一句人话，供界面在对照表上方显示。

    不逐项罗列——"体积相同、格式相同、ISBN 相同……" 铺满一行没人看。
    说清楚"除了这几项之外全部相同"就够，剩下的差异行本来就会展开给用户看。
    """
    shared = shared_fields(records)
    if not shared:
        return ''
    labels = [FIELD_ACCESSORS[f][0] for f in shared]
    if len(labels) > 4:
        return '除以下差异项外，其余 %d 项（%s 等）两份都相同' % (
            len(labels), '、'.join(labels[:3]))
    return '这些项两份都相同：' + '、'.join(labels)


def best_ids(records, field):
    """该字段表现最好的成员 id（用于在对照表里标记"这一项这本更好"）。

    只对"越大越好"的字段有意义（体积/元数据完整度/格式数/评分/标签数）；其余返回 None，
    不假装知道谁更好——那类差异该由用户判断。
    """
    if field == 'size':
        values = [(int(r.get('size') or 0), r['id']) for r in records]
    elif field == 'metadata':
        values = [(int(r.get('_meta_score') or 0), r['id']) for r in records]
    elif field == 'formats':
        values = [(len(r.get('formats') or []), r['id']) for r in records]
    elif field == 'rating':
        values = [(r.get('rating') or 0, r['id']) for r in records]
    elif field == 'tags':
        values = [(len(r.get('tags') or []), r['id']) for r in records]
    else:
        return None
    best = max(values)
    if sum(1 for value, _bid in values if value == best[0]) > 1:
        return None  # 并列 → 不算"谁更好"，避免误导
    return best[1]


def build_table(records):
    """生成对照表数据。

    :return: ``{'shared': [...], 'rows': [{'field','label','cells':[...],'best_id'}],
                'summary': str}``
    """
    rows = []
    for field in differing_fields(records):
        label = FIELD_ACCESSORS[field][0]
        rows.append({
            'field': field,
            'label': label,
            'cells': [{'book_id': r['id'], 'value': _display(r, field)} for r in records],
            'best_id': best_ids(records, field),
        })
    return {
        'shared': shared_fields(records),
        'rows': rows,
        'summary': summary_sentence(records),
    }


def _display(record, field):
    """把取值渲染成给用户看的短文本。"""
    if field == 'size':
        return format_bytes(int(record.get('size') or 0))
    if field == 'formats':
        return '、'.join(sorted(f.upper() for f in (record.get('formats') or []))) or '—'
    if field == 'metadata':
        return '%d 分' % int(record.get('_meta_score') or 0)
    if field == 'added':
        value = str(record.get('added') or '')
        return value[:19].replace('T', ' ') if value else '—'
    if field == 'rating':
        value = record.get('rating') or 0
        return '%d 星' % (value / 2) if value else '未评分'
    if field in ('tags', 'languages', 'translators'):
        values = record.get(field) or []
        return '、'.join(str(v) for v in values) if values else '—'
    if field in ('sole',):
        return '是' if record.get('sole') else '否'
    if field == 'book_type':
        return '实体书' if record.get('book_type') else '电子书'
    value = record.get(field)
    return str(value) if value not in (None, '', []) else '—'


def format_bytes(size):
    """字节 → 人类可读（1 位小数，够用且不啰嗦）。"""
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024.0
    return '%.1f TB' % size