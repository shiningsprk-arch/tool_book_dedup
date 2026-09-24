# -*- coding: utf-8 -*-
"""归一化：把"同一本书的两种写法"压成同一个 key。

设计来源是 BookOrbit 的 `book-duplicate-normalize.ts`（AGPL），**本文件是 clean-room
重写**：只借它"ISBN 互换 + 媒体家族"这两个判定思路，实现、命名与全部判定细节按 MyBooks
自己的数据形态重做（中文标点、全角字符、Calibre 字段口径）。

为什么 ISBN 要归一到 13 位：同一本书的新老书号（ISBN-10 与 ISBN-13）是不同的字符串，
不换算就永远对不上，而换算之后它们是同一个 key。
"""
import re
import unicodedata

try:
    from webserver.i18n import _
except ImportError:  # 离线测试（引擎不应依赖宿主）
    def _(s):
        return s

# 媒体家族：只有同一家族内才互为重复（同一本书的 epub 与 mp3 不是重复）。
# MyBooks 的入库格式见 BaseTool.SUPPORTED_FORMATS，有声书/漫画目前不入库，
# 但保留这两组是为了将来接上时不必改判定结构。
_AUDIOBOOK_FORMATS = frozenset(['m4b', 'mp3', 'm4a', 'opus', 'ogg', 'flac'])
_COMIC_FORMATS = frozenset(['cbz', 'cbr', 'cb7', 'cbx'])
_EBOOK_FORMATS = frozenset(['epub', 'pdf', 'mobi', 'azw', 'azw3', 'fb2', 'kepub', 'txt', 'docx'])

# 标题归一化时要抹掉的字符：所有标点（含中文标点）与空白。
# 用 unicodedata 分类判断，不写死字符表——中文书名里的《》、·、—、：都得抹掉。
_PUNCT_CATEGORIES = frozenset(['Po', 'Pd', 'Ps', 'Pe', 'Pi', 'Pf', 'Pc', 'Zs', 'Zl', 'Zp'])

_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]')
_ISBN_DIGITS_RE = re.compile(r'[^0-9Xx]')
# 书名尾部的括号注记：（全集）/ (全本) / [修订版] —— 中英文括号都认
_TAIL_ANNOTATION_RE = re.compile(r'[（(\[][^）)\]]*[）)\]]\s*$')

# --------------------------------------------------------------------------- 序列标记
#
# "序列标记" = 卷/册/辑/期/部次的序号（`第一部`/`12`/`全13册`/`上下`/`20161029`/`vol.10`）。
# 两侧书名**只差一个序列标记**时，它们是同一套书的不同卷/不同期，**不是**同一本书的两种写法——
# 实测真书库（24,835 本）上，不排除这类对的后果是 87% 的分组都是"同系列不同卷"，
# 而界面上一键"合并这一组"会把整套书删到只剩一本。
#
# 注意判据是"**整段**残留都得由序列标记构成"：`我是猫（企鹅经典丛书第二辑：上海文艺精装版）`
# 的注记里虽然有"第二辑"，但它整段不是序列标记，仍然按"同一本书的另一种版本"处理。
_SERIAL_BODY = (
    r'第[0-9〇零一二三四五六七八九十百千两壹贰叁肆伍陆柒捌玖拾]+[部卷册辑篇集回期章]'
    r'|[0-9〇零一二三四五六七八九十百千两壹贰叁肆伍陆柒捌玖拾]+[部卷册辑篇集回期章]?'
    r'|[上下中][册卷部]?'
    r'|全|共|套装'
    r'|[a-z]{0,4}[0-9]{1,4}[a-z]{0,4}'
)
_SERIAL_ONLY_RE = re.compile(r'^(?:%s)+$' % _SERIAL_BODY)
# 序列标记最长就这么长（`第1卷上下`、`20161029`）；更长的残留一律不算标记
_SERIAL_MAX_LEN = 16


def is_serial_marker(text):
    """`text` 是否**整段**由序列标记构成。空串按"没有标记"处理，返回 False。"""
    if not text:
        return False
    if len(text) > _SERIAL_MAX_LEN:
        return False
    return bool(_SERIAL_ONLY_RE.match(text))


# 尾部**注记**（`title_core` 剥掉的那一截）里出现数字，或**以卷号开头**时，同样不当它是
# "另一种写法"。判据比 `is_serial_marker` 松，因为注记常是"卷号 + 卷副标题"的花样写法：
#   `（第一部 发配牛背驼）` / `（至24卷26章）` / `（1-9）` / `（全20卷）`
#   `[Thu, 16 Jun 2016]` / `[周五, 31 一月 2014]` / `（20161029）`
# 而"另一种版本"的注记照旧放行：`（全本）`/`（修订版）`/`（果麦经典）`/`（译文名著精选）`
# 都没有数字、也不以卷号开头。
# 卷号判定**锚在开头**是关键：`（第一部 发配牛背驼）` 是卷册，而
# `（企鹅经典丛书第二辑：上海文艺精装版）` 是丛书名（同一本书的另一个版本），不能一起毙掉。
# 代价：`（2005最新修订版）` 这种带年份的版本注记会被一并排除（宁可漏，不可误删整套书）。
_SERIAL_ANNOTATION_RE = re.compile(
    r'[0-9]'
    r'|^第[0-9〇零一二三四五六七八九十百千两壹贰叁肆伍陆柒捌玖拾]+[部卷册辑篇集回期章]'
    # 整段注记就是一个序数/方位词（`（三）`/`（一）`/`（上）`/`（中册）`）→ 卷册
    r'|^[〇零一二三四五六七八九十百千两壹贰叁肆伍陆柒捌玖拾上下中]{1,3}$'
    # 带单位的方位词出现在注记里（`（套装上下册）`）→ 卷册
    r'|[上下中][册卷部]')


def annotation_is_serial(text):
    """书名的尾部注记（`title_core` 剥掉的那截）是否像卷次/期次/册数标记。"""
    if not text:
        return False
    return bool(_SERIAL_ANNOTATION_RE.search(text))


def differs_only_by_serial(a_key, b_key):
    """两条归一化书名是否**只差一个序列标记**（门牌号不同，不是同一本书）。

    判据：两条书名有 ≥2 字的公共前缀（防止 "12"/"13" 这种本来就不是同一本书的短串相撞），
    且去掉公共前缀之后，两边剩下的那截**各自**都是序列标记（允许一边为空）。

    例（`True` = 判定为不同卷，不成组）：

    - ``德川家康（第一部）`` / ``德川家康（第十二部）`` → 前缀 ``德川家康第``，残留 ``一部``/``十二部``
    - ``苗疆蛊事`` / ``苗疆蛊事1`` → 残留 ``空``/``1``
    - ``全金属狂潮 10`` / ``全金属狂潮 19`` → 前缀 ``全金属狂潮1``，残留 ``0``/``9``
    - ``The Economist [Thu, 16 Jun]`` / ``[Thu, 23 Jun]`` → 残留 ``16jun``/``23jun``

    例（`False` = 保留原有判定）：

    - ``孙子兵法`` / ``孙子兵法(全本)`` → 残留 ``空``/``全本``（``全本`` 不是序列标记）
    - ``我是猫`` / ``我是猫(果麦经典)`` → 残留 ``果麦经典``
    """
    if not a_key or not b_key or a_key == b_key:
        return False
    if len(a_key) < 2 or len(b_key) < 2:
        return False
    head = 0
    limit = min(len(a_key), len(b_key))
    while head < limit and a_key[head] == b_key[head]:
        head += 1
    if head < 2:
        return False
    a_rest, b_rest = a_key[head:], b_key[head:]
    if not a_rest and not b_rest:
        return False
    if a_rest and not is_serial_marker(a_rest):
        return False
    if b_rest and not is_serial_marker(b_rest):
        return False
    return True


def _is_cjk(ch):
    return bool(_CJK_RE.match(ch))


def has_cjk(text):
    """字符串里是否含有 CJK 字符（用来决定中文分词策略）。"""
    if not text:
        return False
    return any(_is_cjk(ch) for ch in text)


def cjk_ratio(text):
    """CJK 字符占非空白字符的比例，0.0~1.0。"""
    if not text:
        return 0.0
    body = [ch for ch in text if not ch.isspace()]
    if not body:
        return 0.0
    return sum(1 for ch in body if _is_cjk(ch)) / float(len(body))


# --------------------------------------------------------------------------- ISBN


def normalize_isbn(value):
    """去掉所有非 [0-9X] 字符并大写；空值返回 None。"""
    if not value:
        return None
    text = _ISBN_DIGITS_RE.sub('', str(value)).upper()
    return text or None


def is_valid_isbn10(value):
    """ISBN-10 校验：按 10..1 加权求和，需能被 11 整除（末位 X 记 10）。"""
    if not re.fullmatch(r'\d{9}[\dX]', value or ''):
        return False
    total = 0
    for index, ch in enumerate(value):
        digit = 10 if (index == 9 and ch == 'X') else int(ch)
        total += digit * (10 - index)
    return total % 11 == 0


def is_valid_isbn13(value):
    """ISBN-13 校验：1/3 交替加权求和，需能被 10 整除；且必须以 978/979 开头。"""
    if not re.fullmatch(r'\d{13}', value or ''):
        return False
    if not (value.startswith('978') or value.startswith('979')):
        return False
    total = 0
    for index, ch in enumerate(value):
        total += int(ch) * (1 if index % 2 == 0 else 3)
    return total % 10 == 0


def isbn10_to_isbn13(value):
    """ISBN-10 → ISBN-13：前缀 978 + 前 9 位，再补 1/3 加权算出的校验位。"""
    body = '978' + value[:9]
    total = 0
    for index, ch in enumerate(body):
        total += int(ch) * (1 if index % 2 == 0 else 3)
    return body + str((10 - total % 10) % 10)


def isbn_key(isbn10=None, isbn13=None, isbn=None):
    """把任意形式的书号归一成同一个 13 位 key；无效或缺失返回 None。

    优先用合法的 ISBN-13；否则把合法的 ISBN-10 换算过来。校验位不对的一律丢弃——
    书库里脏数据不少（扫描录入、手工敲错），按"看起来像"就分组会造出整片误报。
    """
    for raw in (isbn13, isbn):
        candidate = normalize_isbn(raw)
        if candidate and is_valid_isbn13(candidate):
            return candidate
        # 有的记录把 ISBN-13 填进了 isbn10 字段，反之亦然，所以每个槽位都两种都试
        if candidate and is_valid_isbn10(candidate):
            return isbn10_to_isbn13(candidate)

    candidate = normalize_isbn(isbn10)
    if candidate and is_valid_isbn10(candidate):
        return isbn10_to_isbn13(candidate)
    if candidate and is_valid_isbn13(candidate):
        return candidate
    return None


# --------------------------------------------------------------------------- 文本


def title_key(title):
    """标题归一化：NFKC 折全角 → 去标点与空白 → 小写。

    中文书名普遍很短（"活着"），所以这里不做"去掉 the/a"这类英文停用词处理——
    那会误伤中文里恰好同名的字，而且对中文毫无收益。

    **注意这里不剥离尾部注记**（"（全集）"），剥离后单独放 `title_core`：
    两者都要用——注记相同与否本身是"这是不是同一本书"的证据，直接丢掉会丢信息。
    """
    if not title:
        return ''
    text = unicodedata.normalize('NFKC', str(title))
    kept = []
    for ch in text:
        if ch.isspace() or unicodedata.category(ch) in _PUNCT_CATEGORIES:
            continue
        kept.append(ch)
    return ''.join(kept).lower()


def title_core(title):
    """剥掉尾部括号注记后的主书名原文，如 `孙子兵法(全本)` → `孙子兵法`。

    中文书库里"同一本书"最常见的形态就是主标题完全相同 + 尾部注记不同
    （（全集）/（全本）/（修订版）/ (插图版)）。不剥离的话，注记会被归一化成几个
    额外的字，把短书名的 Dice 拉下阈值——**真实的重复反而查不到**。

    只剥尾部、只剥括号里的，不动中间的字：`罪与罚（上）` 能剥成 `罪与罚`，
    而 `A（B）C` 这种中间带括号的不会被误剥。
    """
    if not title:
        return ''
    text = unicodedata.normalize('NFKC', str(title)).strip()
    while True:
        stripped = _TAIL_ANNOTATION_RE.sub('', text).strip()
        if len(stripped) == len(text):
            break
        text = stripped
    return text


def author_key(name):
    """单个作者名归一化。"""
    return title_key(name)


def author_keys(authors):
    """作者集合归一化：返回 key 的集合（多作者书任一对上即可）。

    两条容错，都是为了"同一批作者的不同写法能收敛到同一个集合"：

    - 合著符号：OPF 里常有 `A & B` / `A、B` / `A, B` 这种单槽多作者写法，
      与 `['A', 'B']` 两槽的写法归一后必须相等，所以在槽内再切一次。
    - 去重：重复出现的作者只算一次。
    """
    if not authors:
        return frozenset()
    if isinstance(authors, str):
        authors = [authors]
    keys = set()
    for name in authors:
        for part in re.split(r'[&,;、，；/]| and ', str(name)):
            key = author_key(part)
            if key:
                keys.add(key)
    return frozenset(keys)


def media_family(fmt):
    """格式 → 媒体家族。

    未入库的格式归 'unknown'：两个 unknown 之间**算同一家族**（都认不出来，就按同类处理，
    免得把显然是同一本书的两份漏掉）；但 unknown 与已知家族不重叠（`families_of` 取交集）。
    """
    if not fmt:
        return 'unknown'
    normalized = str(fmt).strip().lower().lstrip('.')
    if normalized in _AUDIOBOOK_FORMATS:
        return 'audiobook'
    if normalized in _COMIC_FORMATS:
        return 'comic'
    if normalized in _EBOOK_FORMATS:
        return 'ebook'
    return 'unknown'


def families_of(formats):
    """一组格式涉及的媒体家族集合。"""
    return frozenset(media_family(f) for f in (formats or []))