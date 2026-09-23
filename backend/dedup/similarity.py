# -*- coding: utf-8 -*-
"""相似度：字符 n-gram 的 Dice 系数。

`2 * |A ∩ B| / (|A| + |B|)`，也就是 pg_trgm 用的那个公式（BookOrbit 走的是 PostgreSQL 的
`similarity()`，宿主是 SQLite 且 FTS 被显式关掉，所以这里自己实现）。

**中文为什么用 bigram 而不是分词**：模糊召回要对付的是"同一本书的两种写法"，
分词结果依赖分词器对两种写法切得一致，一旦切分不同就召回不到；字符 bigram 不做这种判断。
jieba 宿主已有（`requirements.txt` 里就有），但它是"按词典切词"，适合当辅助特征，
不适合当召回主信号。

**为什么用多重集（Counter）而不是集合**：集合会让"活着"和"活着活着"的内部重叠只算一次，
多重集更贴近真实重合度。
"""
from collections import Counter
import re

from .normalize import cjk_ratio, has_cjk


def _ngrams(text, n):
    """字符 n-gram 多重集。太短的串不做补齐，直接返回自身（保证仍有可比性）。"""
    if not text:
        return Counter()
    if len(text) < n:
        return Counter({text: 1})
    return Counter(text[i:i + n] for i in range(len(text) - n + 1))


def ngram_size_for(text):
    """按文本选 n：含 CJK 用 2，其余用 3。

    中文书名普遍 2–6 个字，取 3 会让"活着"这种短标题退化成一个 gram；
    西文书名较长，取 3 更稳（同 pg_trgm 的默认）。
    """
    if has_cjk(text) and cjk_ratio(text) >= 0.3:
        return 2
    return 3


def grams(text, n=None):
    """取文本的 n-gram 多重集；`n=None` 时按语言自动选（见 `ngram_size_for`）。"""
    if not text:
        return Counter()
    if n is None:
        n = ngram_size_for(text)
    return _ngrams(text, n)


def dice_from_grams(a_grams, b_grams):
    """两个 n-gram 多重集的 Dice 系数，0.0~1.0。任一为空返回 0.0。"""
    if not a_grams or not b_grams:
        return 0.0
    intersection = sum((a_grams & b_grams).values())
    total = sum(a_grams.values()) + sum(b_grams.values())
    if not total:
        return 0.0
    return 2.0 * intersection / float(total)


class SimilarityCache(object):
    """按 key 缓存 n-gram，避免同一个串被反复切。

    扫描时每本书的标题要与同作者桶里的其它书比较多次，切一次缓存起来即可；
    25k 本的场景下这是主要的 CPU 节省点。
    """

    __slots__ = ('_cache', '_pairs')

    def __init__(self):
        self._cache = {}
        # 同一对标题在一次扫描里可能被比较多次（多个共同作者），比较结果也缓存
        self._pairs = {}

    def grams(self, key):
        cached = self._cache.get(key)
        if cached is None:
            cached = grams(key)
            self._cache[key] = cached
        return cached

    def dice(self, key_a, key_b, core_a=None, core_b=None):
        """两个标题之间的相似度（走变体判定，见 `variant_score`）。"""
        if not key_a or not key_b:
            return 0.0
        if key_a == key_b:
            return 1.0
        cache_key = (key_a, key_b, core_a or '', core_b or '')
        cached = self._pairs.get(cache_key)
        if cached is None:
            cached = variant_score(key_a, key_b, core_a, core_b)
            self._pairs[cache_key] = cached
            self._pairs[(key_b, key_a, core_b or '', core_a or '')] = cached
        return cached

    def __len__(self):
        """返回已缓存的比较对数（n-gram 缓存是中间产物，不计入对外长度）。"""
        return len(self._pairs)


def similarity(a, b):
    """无缓存的单次比较（测试与零星调用用），走与扫描相同的变体判定。"""
    return variant_score(a, b)


# --------------------------------------------------------------------------- 变体判定


def _squeeze(text):
    """去掉连续重复字符（"鳄鱼怕怕" → "鳄鱼怕"）。

    用来消掉 A 型弹注/叠字造成的表层差异——这类重复在中文书名里很常见
    （"怕怕"、"说说"），逐字比较会以为是两本书。
    """
    if not text:
        return ''
    out = [text[0]]
    for ch in text[1:]:
        if ch != out[-1]:
            out.append(ch)
    return ''.join(out)


def _tokens(text):
    """切成"有意义的单元"：含 CJK 用 bigram，否则按标点／空白切词。

    token 是**允许直接比较相等**的单元，与 `grams()` 的多重集不同——后者用于算连续
    相似度，前者用于回答"一个标题是不是另一个的变体"。
    """
    if not text:
        return set()
    if has_cjk(text) and cjk_ratio(text) >= 0.3:
        if len(text) < 2:
            return {text}
        return set(text[i:i + 2] for i in range(len(text) - 1))
    return set(part for part in re.split(r'\s+', text) if part)


def _token_similarity(a_tokens, b_tokens):
    """两个 token 集合的 Dice。"""
    if not a_tokens or not b_tokens:
        return 0.0
    return 2.0 * len(a_tokens & b_tokens) / float(len(a_tokens) + len(b_tokens))


def _containment(a_tokens, b_tokens):
    """较小集合被较大集合包住的比例。"""
    if not a_tokens or not b_tokens:
        return 0.0
    smaller, bigger = (a_tokens, b_tokens) if len(a_tokens) <= len(b_tokens) else (b_tokens, a_tokens)
    return len(smaller & bigger) / float(len(smaller))


def variant_score(a_key, b_key, a_core=None, b_core=None):
    """标题相似度：取「完整标题」与「剥离尾部注记后的主书名」两个视角里的**较大**者。

    两个视角各自负责一件事：

    - **完整标题（Dice + 包含度取小）负责挡误报**：`"三体"` vs `"三体2"` 的包含度是 1.0
      （短串被完全包住），只看包含度会把**同系列的卷册**判成重复；Dice 是 0.50，能分清。
    - **主书名负责放行真正的重复**：`"孙子兵法"` vs `"孙子兵法(全本)"` 的完整标题 Dice
      只有 0.75（注记被归一化成额外的字，索引对不齐），而剥离注记后两边的主书名
      完全相同 → 1.0。中文书库里"主标题相同 + 尾部注记不同"是最常见的重复形态，
      不单独看这个视角就会漏掉一批真重复。

    剥离注记是有意的：如果剥离后的两本书其实不同（例如 `某书` 与 `某书(续集)` 都剥成
    `某书`），那属于"看起来像同一本"，查重工具本来就该把它摆给用户判断——这不是
    判定的终点，界面上的对照表与"保留哪本"才是。
    """
    if not a_key or not b_key:
        return 0.0
    if a_key == b_key:
        return 1.0

    score = dice_from_grams(grams(a_key), grams(b_key))
    if has_cjk(a_key) or has_cjk(b_key):
        score = min(score, _containment(_tokens(_squeeze(a_key)), _tokens(_squeeze(b_key))))

    # 主书名视角：任一边带尾注（core 与 key 不同）时才需要比较；两边都没注记时
    # core == key，这一路等于重算完整标题的分数，纯属浪费。
    # 注意条件写"任一边不同"而不是"两边都不同"——被比较的那一本通常正是没有注记的那一本。
    if a_core and b_core and (a_core != a_key or b_core != b_key):
        core_dice = dice_from_grams(grams(a_core), grams(b_core))
        if has_cjk(a_core) or has_cjk(b_core):
            core_dice = min(core_dice, _containment(
                _tokens(_squeeze(a_core)), _tokens(_squeeze(b_core))))
        if a_core == b_core:
            core_dice = 1.0
        score = max(score, core_dice)

    return score