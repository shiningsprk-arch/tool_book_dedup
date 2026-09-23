# -*- coding: utf-8 -*-
"""元数据完整度评分。

字段集按 MyBooks / Calibre 实际口径重做（BookOrbit 那 24 项里有 9 项是它自己的
provider id，MyBooks 侧没有对应概念）。取值规则只有三类：非空串 / 正数 / 计数>0。

**两个宿主陷阱，写在这里免得再踩**（`tool_quality_check` 踩过同款）：

- `get_data_as_dict()` 的标题排序字段键叫 **`sort`**，不是 `title_sort`
- **未评分是数值 `0`，不是缺键**——用"键存在"当"有值"会把全库 rating 判成满分
- 封面没有独立键可读（`SimpleBookFormatter` 无条件生成 img URL），所以本模块只接受
  调用方传来的 `has_cover` 布尔值，不自己猜
"""

# (字段名, 权重, 中文说明) —— 权重体现"这条信息对认出一本书有多重要"
FIELD_WEIGHTS = (
    ('title', 3.0, '书名'),
    ('authors', 3.0, '作者'),
    ('comments', 2.5, '简介'),
    ('has_cover', 2.0, '封面'),
    ('formats', 2.0, '格式文件'),
    ('isbn', 1.5, 'ISBN'),
    ('publisher', 1.5, '出版社'),
    ('pubdate', 1.5, '出版日期'),
    ('tags', 1.0, '标签'),
    ('series', 1.0, '系列'),
    ('languages', 1.0, '语言'),
    ('rating', 1.0, '评分'),
)

UNKNOWN_AUTHOR_MARKERS = ('未知作者', '佚名', 'unknown', 'n/a')


def _has_text(value):
    return bool(str(value).strip()) if value is not None else False


def _has_positive(value):
    """正数才算有值——**未评分是 0**，不是缺键。"""
    if value is None:
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def is_unknown_author(authors):
    """作者是否是"未知/佚名"这类占位——占位不算填了作者。"""
    if not authors:
        return True
    names = [authors] if isinstance(authors, str) else list(authors)
    for name in names:
        text = str(name).strip().lower()
        if text and not any(marker in text for marker in UNKNOWN_AUTHOR_MARKERS):
            return False
    return True


def missing_fields(record):
    """逐字段判断是否"缺"，返回 (缺失字段名列表, 每字段得分明细)。"""
    detail = {}
    for field, weight, _label in FIELD_WEIGHTS:
        if field == 'authors':
            present = not is_unknown_author(record.get('authors'))
        elif field == 'has_cover':
            present = bool(record.get('has_cover'))
        elif field == 'formats':
            present = bool(record.get('formats'))
        elif field == 'rating':
            present = _has_positive(record.get('rating'))
        elif field == 'series':
            present = _has_text(record.get('series'))
        elif field == 'isbn':
            present = _has_text(record.get('isbn')) or bool(
                record.get('isbn13') or record.get('isbn10'))
        elif field == 'tags':
            present = bool(record.get('tags'))
        elif field == 'languages':
            present = bool(record.get('languages'))
        else:
            present = _has_text(record.get(field))
        detail[field] = {'present': present, 'weight': weight}
    missing = [f for f, _w, _l in FIELD_WEIGHTS if not detail[f]['present']]
    return missing, detail


def score(record):
    """0~100 的完整度分。分母只算权重>0 的字段（本表全部>0）。"""
    _missing, detail = missing_fields(record)
    total = sum(item['weight'] for item in detail.values())
    if not total:
        return 0
    earned = sum(item['weight'] for item in detail.values() if item['present'])
    return int(earned / total * 100)


def score_all(records):
    """批量评分，返回 {book_id: 分数}。"""
    return {record['id']: score(record) for record in records}