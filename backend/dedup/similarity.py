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

from .normalize import (annotation_is_serial, cjk_ratio, differs_only_by_serial,
                        has_cjk)


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


def _grams_for(text, n):
    """`_ngrams` 的取用口，形状与缓存一致（见 `variant_score` 的 `gram_of`）。"""
    return _ngrams(text, n)


def shared_ngram_size(a_key, b_key):
    """一次比较里**两侧共用**的 n：取各自语言的较小者。

    这一点是必须的：`ngram_size_for` 是**逐串**判断语言的，而"中英混排"的两个串可能
    各选一个 n（`哈利波特 Harry Potter` 的 CJK 占比 0.27 → n=3，`哈利波特` → n=2），
    两套 n-gram 之间交集恒为空 → 相似度恒为 0.000，真正的重复永远查不出来。
    共用一个 n 之后，分数才是"这两个书名差多少"的有意义回答。
    """
    return min(ngram_size_for(a_key), ngram_size_for(b_key))


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
    """一次扫描里的两级缓存：n-gram 切分结果 + 两两比较结果。

    标题要与同一个作者桶里的其它书比较很多次，切分和比较都只该做一遍。
    25k 本的场景下这是主要的 CPU 节省点（真书库实测 438,740 次比较）。
    """

    __slots__ = ('_cache', '_pairs')

    def __init__(self):
        # (文本, n) → n-gram 多重集
        self._cache = {}
        # 同一对标题在一次扫描里可能被比较多次（多个共同作者），比较结果也缓存
        self._pairs = {}

    def grams(self, text, n):
        """带缓存的 `_ngrams`——**必须连 n 一起做键**，否则两种 n 会互相污染。"""
        key = (text, n)
        cached = self._cache.get(key)
        if cached is None:
            cached = _ngrams(text, n)
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
            cached = variant_score(key_a, key_b, core_a, core_b, gram_of=self.grams)
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


def _bigrams(text):
    """去叠字之后的 bigram 集合（用于包含度，见 `variant_score`）。"""
    if not text:
        return set()
    squeezed = _squeeze(text)
    if len(squeezed) < 2:
        return {squeezed}
    return set(squeezed[i:i + 2] for i in range(len(squeezed) - 1))


def _containment(a_bigrams, b_bigrams):
    """较小集合被较大集合包住的比例。"""
    if not a_bigrams or not b_bigrams:
        return 0.0
    smaller, bigger = ((a_bigrams, b_bigrams) if len(a_bigrams) <= len(b_bigrams)
                       else (b_bigrams, a_bigrams))
    return len(smaller & bigger) / float(len(smaller))


def _annotation_of(key, core):
    """`title_core` 剥掉的那一截（尾部注记的归一化文本）；没剥过则返回 ''。"""
    if not key or not core or not key.startswith(core):
        return ''
    return key[len(core):]


def variant_score(a_key, b_key, a_core=None, b_core=None, gram_of=None):
    """标题相似度：取「完整标题」与「剥离尾部注记后的主书名」两个视角里的**较大**者。

    两个视角各自负责一件事：

    - **完整标题（Dice + 包含度取小）负责挡误报**：`"三体"` vs `"三体2"` 的包含度是 1.0
      （短串被完全包住），只看包含度会把**同系列的卷册**判成重复；Dice 是 0.50，能分清。
    - **主书名负责放行真正的重复**：`"孙子兵法"` vs `"孙子兵法(全本)"` 的完整标题 Dice
      只有 0.75（注记被归一化成额外的字，索引对不齐），而剥离注记后两边的主书名
      完全相同 → 1.0。中文书库里"主标题相同 + 尾部注记不同"是最常见的重复形态，
      不单独看这个视角就会漏掉一批真重复。

    **这道放行有两道闸**（不设的话真书库上 87% 的分组是"同系列不同卷"）：

    1. `differs_only_by_serial`（外层）：两侧完整标题只差一个卷号/期号 → 直接 0
    2. 注记里有数字/卷号（`annotation_is_serial`），或两个主书名之间也只差一个卷号
       （`differs_only_by_serial(a_core, b_core)`）→ **不给主书名视角放行**

    第 2 条是必须的：`侯海洋基层风云（第一部 发配牛背驼）` 与 `（第二部 巴山城管）`
    的主书名都是"侯海洋基层风云"，只靠主书名比较必然给 1.0；
    `苗疆蛊事（全集）`（主书名"苗疆蛊事"）与 `苗疆蛊事1` 之间也只差一个卷号——
    少了这条，并查集就会顺着这种"中介书"把整套卷册串成一组。

    :param gram_of: 可选 `(text, n) → Counter` 取用口，扫描时传缓存里的那份（见 `SimilarityCache`）
    """
    if not a_key or not b_key:
        return 0.0
    if a_key == b_key:
        return 1.0
    if differs_only_by_serial(a_key, b_key):
        return 0.0

    gram_of = gram_of or _grams_for
    n = shared_ngram_size(a_key, b_key)
    score = dice_from_grams(gram_of(a_key, n), gram_of(b_key, n))
    # 包含度只在两侧同处"中文 bigram 空间"时才有意义；n=3（西文/混排）时
    # 逐字 bigram 的包含关系不再成立，硬套会得出 0
    if n == 2:
        score = min(score, _containment(_bigrams(a_key), _bigrams(b_key)))

    # 主书名视角：任一边带尾注（core 与 key 不同）时才需要比较；两边都没注记时
    # core == key，这一路等于重算完整标题的分数，纯属浪费。
    # 注意条件写"任一边不同"而不是"两边都不同"——被比较的那一本通常正是没有注记的那一本。
    if a_core and b_core and (a_core != a_key or b_core != b_key):
        annotation_a = _annotation_of(a_key, a_core)
        annotation_b = _annotation_of(b_key, b_core)
        if (annotation_is_serial(annotation_a) or annotation_is_serial(annotation_b)
                or differs_only_by_serial(a_core, b_core)):
            return score                      # 注记本身是卷号/期次 → 不放行
        core_n = shared_ngram_size(a_core, b_core)
        core_dice = dice_from_grams(gram_of(a_core, core_n), gram_of(b_core, core_n))
        if core_n == 2:
            core_dice = min(core_dice, _containment(_bigrams(a_core), _bigrams(b_core)))
        if a_core == b_core:
            core_dice = 1.0
        score = max(score, core_dice)

    return score