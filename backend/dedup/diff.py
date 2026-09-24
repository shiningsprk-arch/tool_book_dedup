# -*- coding: utf-8 -*-
"""对照：只展开"有差异"的字段。

思路取自 BookOrbit 的 `duplicate-diff.ts`（"copies of one book agree about most things,
so the comparison only earns its space when it drops the fields that are the same
everywhere"）。**本文件是 clean-room 重写**，字段集换成 MyBooks 口径。

这一层直接解决"两份元数据可能完全一致"：一致就不出对照行，界面自然没有噪音，
差异会落到入库时间与条目属性上，用户一眼就能定。

**值分两类**（`_cell`）：
- `kind='text'`：已经渲染好的短文本（格式列表、标签、日期…），与语言无关
- 其余 kind（`bytes`/`score`/`rating`/`bool`/`enum`）：只给**原始值**，由前端按当前语言渲染
  （"79.9 KB"、"9 星"/"未评分"、"是"/"否"、"实体书"/"电子书"）

之所以要区分：这些文案以前是后端拼好的中文字符串，前端原样渲染——en / zh-TW 下
整张对照表都是中文。字段名同理：后端给 `field` 这个稳定标识，前端用 `diff.field.<field>`
取本地化名字（`label` 只作为回退，也是报告文件里给人看的中文标注）。
"""

# 参与对照的字段：字段名 → (中文名, 取值函数)。取值函数必须返回可比较的标量/元组。
# 中文名只用于报告文件（给人看）与前端取不到本地化键时的回退。
FIELD_ACCESSORS = {
    'size': ('体积', lambda r: int(r.get('size') or 0)),
    'formats': ('格式', lambda r: tuple(sorted(f.upper() for f in (r.get('formats') or [])))),
    'isbn': ('ISBN', lambda r: r.get('isbn') or r.get('isbn13') or r.get('isbn10') or ''),
    'metadata': ('元数据', lambda r: r.get('_meta_score', 0)),
    'cover': ('封面', lambda r: bool(r.get('has_cover'))),
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
    """把"全都一样"的字段折成一句人话。

    **只写进报告文件给人看**（界面不再用它——中文字符串在 en / zh-TW 下是错的语言，
    前端现在用 `shared` 里的字段名自己按当前语言拼）。

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
    elif field == 'cover':
        values = [(1 if r.get('has_cover') else 0, r['id']) for r in records]
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

    **成员变过就要重算**（`/group` 会滤掉已合并/已删的成员）：表格是扫描时按当时那批
    成员算的，滤掉一个成员之后表头会比每行的单元格少一格——用户看到的是"三个数字排在
    两个书名下面"。所以调用方拿活着的成员**重建**，而不是裁剪旧表的单元格。
    """
    rows = []
    for field in differing_fields(records):
        label = FIELD_ACCESSORS[field][0]
        rows.append({
            'field': field,
            'label': label,
            'cells': [_cell(r, field) for r in records],
            'best_id': best_ids(records, field),
        })
    return {
        'shared': shared_fields(records),
        'rows': rows,
        'summary': summary_sentence(records),
    }


# 字段 → 值类型。`text` 之外的类型由前端按当前语言渲染（见模块 docstring）。
_VALUE_KINDS = {
    'size': 'bytes',
    'metadata': 'score',
    'rating': 'rating',
    'cover': 'bool',
    'sole': 'bool',
    'book_type': 'enum',
}


def _cell(record, field):
    """对照表里的一格：`{'book_id', 'kind', 'value'}`。"""
    kind = _VALUE_KINDS.get(field, 'text')
    value = _display(record, field)
    return {'book_id': record['id'], 'kind': kind, 'value': value}


def _display(record, field):
    """把取值渲染成短文本。

    **语言无关**的在这里渲染（格式列表、标签、日期、原始数字）；
    与语言有关的几种只送原始值（kind != 'text'），文案交给前端。
    """
    if field == 'size':
        return int(record.get('size') or 0)
    if field == 'metadata':
        return int(record.get('_meta_score') or 0)
    if field == 'rating':
        return int(record.get('rating') or 0)
    if field == 'cover':
        return bool(record.get('has_cover'))
    if field == 'sole':
        return bool(record.get('sole'))
    if field == 'book_type':
        return 1 if record.get('book_type') else 0
    if field == 'formats':
        return '、'.join(sorted(f.upper() for f in (record.get('formats') or []))) or '—'
    if field == 'added':
        value = str(record.get('added') or '')
        return value[:19].replace('T', ' ') if value else '—'
    if field in ('tags', 'languages', 'translators'):
        values = record.get(field) or []
        return '、'.join(str(v) for v in values) if values else '—'
    value = record.get(field)
    return str(value) if value not in (None, '', []) else '—'