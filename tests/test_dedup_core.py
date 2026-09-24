# -*- coding: utf-8 -*-
"""查重合并引擎单测：不需要 MyBooks、不需要 calibre。

运行：
    python tests/test_dedup_core.py
或
    python -m pytest tests/test_dedup_core.py -q
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, '..', 'backend'))


def _load_engine():
    """按宿主 toolbox_manager 的方式加载 backend/，再取 dedup 子包。"""
    name = 'mybooks_tool_book_dedup_test'
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(BACKEND, '__init__.py'),
        submodule_search_locations=[BACKEND])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return importlib.import_module(name + '.dedup')


dedup = _load_engine()
normalize = dedup.normalize
similarity = dedup.similarity
cluster = dedup.cluster
metadata = dedup.metadata
diff = dedup.diff
keeper = dedup.keeper
report = dedup.report


def score_titles(left, right):
    """按**扫描时的口径**比较两个原始书名：完整标题与主书名两个视角都带上。

    只传 `title_key()` 会丢掉主书名视角，`孙子兵法` / `孙子兵法(全本)` 就会只有 0.75——
    那是测试自己漏了参数，不是引擎的行为。
    """
    return similarity.variant_score(
        normalize.title_key(left), normalize.title_key(right),
        normalize.title_key(normalize.title_core(left)),
        normalize.title_key(normalize.title_core(right)))


def make_record(book_id, title, authors=('某作者',), formats=('EPUB',), size=1000,
                isbn=None, added='2026-01-01T00:00:00', **extra):
    record = {
        'id': book_id,
        'title': title,
        'authors': list(authors),
        'formats': list(formats),
        'size': size,
        'isbn': isbn,
        'added': added,
    }
    record.update(extra)
    return cluster.prepare(record)


class TestNormalizeIsbn(unittest.TestCase):
    def test_normalize_strips_separators(self):
        self.assertEqual(normalize.normalize_isbn('978-7-02-000220-9'), '9787020002209')
        self.assertEqual(normalize.normalize_isbn('0-306-40615-2'), '0306406152')
        self.assertEqual(normalize.normalize_isbn('  '), None)
        self.assertEqual(normalize.normalize_isbn(None), None)

    def test_isbn10_checksum(self):
        self.assertTrue(normalize.is_valid_isbn10('0306406152'))
        self.assertTrue(normalize.is_valid_isbn10('097522980X'))
        self.assertFalse(normalize.is_valid_isbn10('0306406153'))
        self.assertFalse(normalize.is_valid_isbn10('030640615'))

    def test_isbn13_checksum(self):
        self.assertTrue(normalize.is_valid_isbn13('9787020002207'))
        self.assertTrue(normalize.is_valid_isbn13('9780306406157'))
        self.assertFalse(normalize.is_valid_isbn13('9787020002208'))
        # 前缀必须是 978/979
        self.assertFalse(normalize.is_valid_isbn13('1234567890128'))

    def test_isbn10_converts_to_isbn13(self):
        self.assertEqual(normalize.isbn10_to_isbn13('0306406152'), '9780306406157')
        self.assertEqual(normalize.isbn10_to_isbn13('097522980X'), '9780975229804')

    def test_isbn_key_unifies_old_and_new_numbers(self):
        """同一本书的新老书号必须收敛到同一个 key —— 这是查重能对上号的关键。"""
        from_10 = normalize.isbn_key(isbn10='0-306-40615-2')
        from_13 = normalize.isbn_key(isbn13='978-0-306-40615-7')
        self.assertEqual(from_10, from_13)
        self.assertEqual(from_10, '9780306406157')

    def test_isbn_key_rejects_bad_checksum(self):
        """校验位不对的一律丢弃，避免"看起来像"就分组造成的整片误报。"""
        self.assertIsNone(normalize.isbn_key(isbn13='9787020002208'))
        self.assertIsNone(normalize.isbn_key(isbn10='0306406153'))
        self.assertIsNone(normalize.isbn_key())

    def test_isbn_key_accepts_either_slot(self):
        """有的记录把 ISBN-13 填进了 isbn10 字段，反之亦然。"""
        self.assertEqual(normalize.isbn_key(isbn10='9780306406157'), '9780306406157')
        self.assertEqual(normalize.isbn_key(isbn13='0306406152'), '9780306406157')


class TestNormalizeText(unittest.TestCase):
    def test_title_key_strips_cjk_punctuation(self):
        self.assertEqual(normalize.title_key('《活着》'), '活着')
        self.assertEqual(normalize.title_key('三体·全集'), '三体全集')
        self.assertEqual(normalize.title_key('书名：副标题'), '书名副标题')
        self.assertEqual(normalize.title_key('A—B'), 'ab')

    def test_title_key_folds_width_and_case(self):
        self.assertEqual(normalize.title_key('Ｔｈｅ　Ｈｏｂｂｉｔ'), 'thehobbit')
        self.assertEqual(normalize.title_key('THE HOBBIT'), 'thehobbit')
        self.assertEqual(normalize.title_key(None), '')

    def test_author_keys_is_a_set(self):
        """两种写法（"甲 & 乙" vs ['甲','乙']）归一后应能对上。"""
        keys_joined = normalize.author_keys(['甲 & 乙'])
        keys_split = normalize.author_keys(['甲', '乙'])
        self.assertEqual(keys_joined, keys_split)
        self.assertEqual(normalize.author_keys(None), frozenset())

    def test_media_family(self):
        self.assertEqual(normalize.media_family('epub'), 'ebook')
        self.assertEqual(normalize.media_family('.AZW3'), 'ebook')
        self.assertEqual(normalize.media_family('mp3'), 'audiobook')
        self.assertEqual(normalize.media_family('cbz'), 'comic')
        self.assertEqual(normalize.media_family('xyz'), 'unknown')
        self.assertEqual(normalize.media_family(None), 'unknown')

    def test_has_cjk(self):
        self.assertTrue(normalize.has_cjk('活着'))
        self.assertFalse(normalize.has_cjk('Hobbit'))
        self.assertGreater(normalize.cjk_ratio('活着活着'), 0.9)
        self.assertLess(normalize.cjk_ratio('The Hobbit'), 0.1)


class TestSerialMarker(unittest.TestCase):
    """序列标记：卷/册/辑/期/部次的序号（判定层最重要的一道闸，见 cluster.py 注释）。"""

    def test_is_serial_marker(self):
        for text in ('1', '12', '第一部', '第十二部', '全13册', '上', '中', '下册',
                     'vol.10', '16jun', '20161029', '第1卷上下'):
            marker = normalize.title_key(text)
            self.assertTrue(normalize.is_serial_marker(marker), marker)
        # "另一种版本"的注记不是序列标记
        for text in ('全本', '全集', '修订版', '果麦经典', '经典版', '插图版',
                     '企鹅经典丛书第二辑：上海文艺精装版'):
            marker = normalize.title_key(text)
            self.assertFalse(normalize.is_serial_marker(marker), marker)
        self.assertFalse(normalize.is_serial_marker(''))

    def test_annotation_is_serial(self):
        """注记判定：带数字/以卷号开头/整段是序数的算卷册；丛书名、版本名不算。"""
        for text in ('三', '一', '上', '中册', '第一部 发配牛背驼', '第一部', '至24卷26章',
                     '1-9', '全20卷', '20161029', 'Thu, 16 Jun 2016', '周五, 31 一月 2014',
                     '套装上下册', '典藏版套装共8册'):
            self.assertTrue(normalize.annotation_is_serial(text), text)
        for text in ('全本', '全集', '修订版', '经典版', '果麦经典', '译文名著精选',
                     '图文增补版', '别册', '企鹅经典丛书第二辑：上海文艺精装版',
                     '读客熊猫君出品，余华不吃不喝不睡，疯了般读完了这本书'):
            self.assertFalse(normalize.annotation_is_serial(text), text)

    def test_differs_only_by_serial(self):
        self.assertTrue(normalize.differs_only_by_serial('德川家康第一部', '德川家康第十二部'))
        self.assertTrue(normalize.differs_only_by_serial('苗疆蛊事', '苗疆蛊事1'))
        self.assertTrue(normalize.differs_only_by_serial('全金属狂潮10', '全金属狂潮19'))
        self.assertFalse(normalize.differs_only_by_serial('孙子兵法', '孙子兵法全本'))
        self.assertFalse(normalize.differs_only_by_serial('活着', '三体'))
        # 完全同名（真重复）与空值都不算"只差序号"
        self.assertFalse(normalize.differs_only_by_serial('三体', '三体'))
        self.assertFalse(normalize.differs_only_by_serial('', '三体'))
        self.assertFalse(normalize.differs_only_by_serial('三体', ''))
        # 公共前缀太短（<2 字）不算——短串本来就不可信
        self.assertFalse(normalize.differs_only_by_serial('12', '13'))
        # 对称性
        self.assertTrue(normalize.differs_only_by_serial('德川家康第十二部', '德川家康第一部'))


class TestSimilarity(unittest.TestCase):
    def test_ngram_size_by_language(self):
        self.assertEqual(similarity.ngram_size_for('活着'), 2)
        self.assertEqual(similarity.ngram_size_for('The Hobbit'), 3)

    def test_dice_identical_is_one(self):
        self.assertAlmostEqual(similarity.similarity('活着', '活着'), 1.0)

    def test_dice_empty_is_zero(self):
        self.assertEqual(similarity.similarity('', '活着'), 0.0)
        self.assertEqual(similarity.similarity('活着', ''), 0.0)

    def test_chinese_near_miss_scores_high(self):
        """中文书名差一个字，应该仍然明显高于无关书名（bigram 的作用）。

        "活着"与"活着的意义"是**不同作品**，0.4 这个值是有意的：低于默认阈值 0.85，
        不该被判成重复——它只用来体现"关系比无关书名近得多"。
        """
        near = similarity.similarity('活着', '活着的意义')
        far = similarity.similarity('活着', '三体')
        self.assertGreater(near, far)
        self.assertGreater(near, 0.3)
        self.assertLess(near, 1.0)
        self.assertLess(near, 0.85)

    def test_annotation_suffix_uses_core_title(self):
        """带"（全本）"注记的同一本书必须判为完全相同（走主书名视角）。"""
        norm = normalize
        for title in ('孙子兵法', '红楼梦', 'The Hobbit'):
            base_key = norm.title_key(title)
            base_core = norm.title_key(norm.title_core(title))
            for suffix in ('（全本）', '(全集)', '[修订版]'):
                annotated_key = norm.title_key(title + suffix)
                annotated_core = norm.title_key(norm.title_core(title + suffix))
                self.assertAlmostEqual(
                    similarity.variant_score(
                        base_key, annotated_key, base_core, annotated_core), 1.0,
                    '%s vs %s' % (title, title + suffix))

    def test_title_core_only_strips_trailing_brackets(self):
        self.assertEqual(normalize.title_core('孙子兵法(全本)'), '孙子兵法')
        self.assertEqual(normalize.title_core('罪与罚（上）'), '罪与罚')
        # NFKC 会把全角括号折成半角（归一化的目的），但中间带括号的**不该被剥**
        self.assertEqual(normalize.title_core('A（B）C'), 'A(B)C')
        self.assertEqual(normalize.title_core('没有括号'), '没有括号')
        self.assertEqual(normalize.title_core(None), '')

    def test_suffix_variant_matches(self):
        """加"（全集）"这类后缀的真重复必须查得到——这是中文书库最常见的重复形态。"""
        self.assertGreaterEqual(
            similarity.similarity('孙子兵法', '孙子兵法全集'), 0.75)

    def test_series_volumes_are_not_duplicates(self):
        """卷册号只差一位时不能判成重复——包含度会误判，Dice 挡得住。"""
        self.assertLess(similarity.similarity('三体', '三体2'), 0.85)
        self.assertLess(similarity.similarity('三体', '三体3'), 0.85)
        # 但两本内容真的相同（一字不差）仍然是 1.0
        self.assertAlmostEqual(similarity.similarity('三体', '三体'), 1.0)

    def test_serial_only_difference_is_excluded(self):
        """**只差一个卷号/期号**的对必须排除——这类对的主书名相同，光靠阈值拦不住。

        真书库（24,835 本）实测：不排除它们的话，87% 的分组都是"同系列不同卷"，
        而界面上一键"合并这一组"会把整套书删到只剩一本。这些用例是那次 review 的原文。
        """
        cases = [
            ('德川家康（第一部）', '德川家康（第十二部）'),
            ('天龙八部（一）', '天龙八部（二）'),
            ('笑傲江湖（一）', '笑傲江湖（四）'),
            ('罗马人的故事4：凯撒时代（上）', '罗马人的故事4：凯撒时代（下）'),
            ('苗疆蛊事', '苗疆蛊事1'),
            ('苗疆蛊事1', '苗疆蛊事2'),
            ('明朝那些事儿', '明朝那些事儿·壹'),
            ('全金属狂潮 10', '全金属狂潮 19'),
            ('The Economist [Thu, 16 Jun]', 'The Economist [Thu, 23 Jun]'),
            ('The Economist (20161029)', 'The Economist (20161105)'),
            ('德川家康（全13册）', '德川家康（第十二部）'),
            ('李自成（全10卷）', '李自成'),
        ]
        for left, right in cases:
            score = score_titles(left, right)
            self.assertLess(score, 0.85, '%s vs %s 被判成了重复' % (left, right))

    def test_edition_annotation_still_matches(self):
        """"另一种版本"的注记（不是序号）必须照旧放行——这才是要查的真重复。"""
        cases = [
            ('孙子兵法', '孙子兵法(全本)'),
            ('孙子兵法', '孙子兵法[修订版]'),
            ('我是猫', '我是猫(果麦经典)'),
            ('我是猫', '我是猫（企鹅经典丛书第二辑：上海文艺精装版）'),
            ('万历十五年', '万历十五年（经典版）'),
            ('斗破苍穹', '斗破苍穹（全本）'),
            ('焚舟纪（别册）', '焚舟纪别册'),
        ]
        for left, right in cases:
            score = score_titles(left, right)
            self.assertGreaterEqual(score, 0.85, '%s vs %s 被误排除' % (left, right))

    def test_annotation_with_volume_marker_is_not_a_variant(self):
        """**回归（review P1 二轮）**：注记里带卷号/期次时，主书名视角不许放行。

        光排除"两侧书名只差一个卷号"是不够的：真书库里误差是通过**中间人**串起来的。
        `侯海洋基层风云（第一部 发配牛背驼）` 与 `（第二部 巴山城管）` 的主书名都是
        "侯海洋基层风云"；`苗疆蛊事（全集）`（主书名"苗疆蛊事"）与 `苗疆蛊事1` 之间也
        只差一个卷号。只要有一条边活下来，并查集就会把整套卷册连成一组——
        实测（24,835 本真书库）这几种形态让最大分组达到 19 本。
        """
        cases = [
            ('侯海洋基层风云（第一部 发配牛背驼）', '侯海洋基层风云（第二部 天堂到地狱）'),
            ('苗疆蛊事（全集）', '苗疆蛊事1'),
            ('苗疆蛊事（至24卷26章）', '苗疆蛊事2'),
            ('鹿鼎记（新修版）', '鹿鼎记（三）'),
            ('The Economist [Thu, 16 Jun 2016]', 'The Economist [周五, 31 一月 2014]'),
            ('The Economist (20161029)', 'The Economist [周五, 31 一月 2014]'),
            ('鲁迅全集（全20卷）', '鲁迅全集'),
            ('蒋勋说红楼梦(典藏版)(套装共8册)', '蒋勋说红楼梦'),
            ('基督山伯爵(套装上下册)', '基督山伯爵'),
        ]
        for left, right in cases:
            score = score_titles(left, right)
            self.assertLess(score, 0.85, '%s vs %s 被判成了重复' % (left, right))

    def test_publisher_annotation_still_matches(self):
        """丛书名/版本名的注记不能被上面的规则误伤。

        判据锚在注记**开头**就是为了这条：`（企鹅经典丛书第二辑：上海文艺精装版）`
        虽然含"第二辑"，但它是丛书名（同一本书的另一个版本），不是卷号。
        """
        cases = [
            ('我是猫', '我是猫（企鹅经典丛书第二辑：上海文艺精装版）'),
            ('我是猫', '我是猫(果麦经典)'),
            ('万历十五年', '万历十五年（经典版）'),
            ('明朝那些事儿', '明朝那些事儿（图文增补版）'),
        ]
        for left, right in cases:
            score = score_titles(left, right)
            self.assertGreaterEqual(score, 0.85, '%s vs %s 被误排除' % (left, right))

    def test_identical_serial_titles_stay_matched(self):
        """同名同卷号的**真重复**仍要成组（"罗马人的故事4：凯撒时代（上）" x6 那种）。"""
        title = '罗马人的故事4：凯撒时代（上）'
        self.assertAlmostEqual(score_titles(title, title), 1.0)
        self.assertAlmostEqual(score_titles('天龙八部（一）', '天龙八部（一）'), 1.0)

    def test_mixed_script_titles_do_not_collapse_to_zero(self):
        """中英混排的标题不能再恒为 0：n 必须是两侧共用的。

        原来 `ngram_size_for` 是逐串判断的，`哈利波特 Harry Potter`（CJK 占比 0.27）
        取 3-gram 而 `哈利波特` 取 2-gram，两套 n-gram 交集恒空 → 相似度 0.000，
        同一本书带英文副题就永远查不出来。现在分数是有意义的（虽然仍低于阈值）。
        """
        for left, right in (('哈利波特 Harry Potter', '哈利波特'),
                            ('三体 Three Body', '三体')):
            score = similarity.similarity(normalize.title_key(left),
                                          normalize.title_key(right))
            self.assertGreater(score, 0.0, '%s vs %s 仍是 0' % (left, right))

    def test_shared_ngram_size_is_the_smaller_one(self):
        self.assertEqual(similarity.shared_ngram_size('哈利波特', 'thehobbit'), 2)
        self.assertEqual(similarity.shared_ngram_size('thehobbit', 'lordofrings'), 3)

    def test_unrelated_titles_score_low(self):
        self.assertLess(similarity.similarity('活着', '三体'), 0.25)

    def test_english_similar_titles(self):
        """加后缀的长书名会被拉低——所以阈值不能定得过高（这点在报告里透出原值便于标定）。"""
        self.assertGreater(
            similarity.similarity('thehobbit', 'thehobbitillustrated'), 0.5)

    def test_symmetry(self):
        left = similarity.similarity('三体', '三体全集')
        right = similarity.similarity('三体全集', '三体')
        self.assertAlmostEqual(left, right)

    def test_cache_matches_uncached(self):
        cache = similarity.SimilarityCache()
        self.assertAlmostEqual(
            cache.dice('活着', '活着的意义'),
            similarity.similarity('活着', '活着的意义'))
        self.assertGreater(len(cache), 0)


class TestCluster(unittest.TestCase):
    def test_isbn_pair_matches_without_title_similarity(self):
        """同一本书号、标题写法完全不同（一中文一英文）也应该成组。"""
        left = make_record(1, '活着', isbn='9787506365437')
        right = make_record(2, 'To Live', isbn='978-7-5063-6543-7'.replace('-', ''))
        right['_isbn_key'] = left['_isbn_key']
        pairs, _stats = cluster.candidate_pairs([left, right], threshold=0.99)
        self.assertEqual(len(pairs), 1)
        self.assertIn('isbn', pairs[0]['reasons'])

    def test_isbn_pair_requires_same_media_family(self):
        """同一本书的 epub 与 mp3 不是重复。"""
        audio = {'id': 2, 'title': 'X', 'authors': ['A'], 'formats': ['MP3'],
                 'size': 1, 'isbn': '9780306406157'}
        book = make_record(1, 'X', formats=['EPUB'], isbn='9780306406157')
        pairs, _stats = cluster.candidate_pairs([book, cluster.prepare(audio)],
                                                threshold=0.99)
        self.assertEqual(pairs, [])

    def test_same_author_similar_title_pairs(self):
        left = make_record(1, '三体', authors=['刘慈欣'])
        right = make_record(2, '三体（全集）', authors=['刘慈欣'])
        pairs, stats = cluster.candidate_pairs([left, right], threshold=0.5)
        self.assertEqual(len(pairs), 1)
        self.assertGreater(stats['comparisons'], 0)

    def test_different_author_never_compared(self):
        """作者不同的一对根本不进入标题比较——这是扫描不爆的关键。"""
        left = make_record(1, '活着', authors=['余华'])
        right = make_record(2, '活着', authors=['另一个作者'])
        pairs, stats = cluster.candidate_pairs([left, right], threshold=0.5)
        self.assertEqual(pairs, [])
        self.assertEqual(stats['comparisons'], 0)

    def test_bucket_cap_skips_and_accounts(self):
        """同一作者的书过多时跳过该桶并记账，不让一次扫描把服务拖住。"""
        records = [cluster.prepare({
            'id': i, 'title': '无名书%d' % i, 'authors': ['高产作者'],
            'formats': ['EPUB'], 'size': 1}) for i in range(20)]
        pairs, stats = cluster.candidate_pairs(records, threshold=0.5, max_bucket=5)
        self.assertEqual(pairs, [])
        self.assertEqual(stats['skipped_buckets'], 1)
        self.assertEqual(stats['skipped_books'], 20)
        self.assertEqual(stats['comparisons'], 0)

    def test_authorless_books_share_one_bucket(self):
        """没有作者的书**仍要**比较标题：它们全落进同一个 "" 桶。

        这里曾经断言"不参与比较"，与 `cluster.py` 的模块注释（"那类书会全部落进同一个
        『无作者』桶"）正好相反 —— 后果是同一本无作者的书在标题上永远查不出来。
        防 O(n²) 的是桶上限（`max_bucket`），见下一条。
        """
        left = make_record(1, '无名书', authors=[])
        right = make_record(2, '无名书', authors=[])
        pairs, stats = cluster.candidate_pairs([left, right], threshold=0.5)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(stats['comparisons'], 1)
        # 有 ISBN 时仍然能成组（走强证据那一档）
        left = make_record(3, '无名书', authors=[], isbn='9780306406157')
        right = make_record(4, '完全不同', authors=[], isbn='9780306406157')
        pairs, _stats = cluster.candidate_pairs([left, right], threshold=0.99)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['reasons'], ['isbn'])

    def test_authorless_bucket_respects_cap(self):
        """无作者桶再大也受 `max_bucket` 约束（跳过大桶并记账，不拖住扫描）。"""
        records = [make_record(i + 1, '无名书%d' % i, authors=[]) for i in range(30)]
        pairs, stats = cluster.candidate_pairs(records, threshold=0.5, max_bucket=4)
        self.assertEqual(pairs, [])
        self.assertEqual(stats['skipped_buckets'], 1)
        self.assertEqual(stats['skipped_books'], 30)

    def test_union_find_groups_chain(self):
        """A~B、B~C 应该并成一个三人组（并查集的传递性）。"""
        a = make_record(1, '三体', authors=['刘慈欣'])
        b = make_record(2, '三体全集', authors=['刘慈欣'])
        c = make_record(3, '三体全集上', authors=['刘慈欣'])
        pairs, _stats = cluster.candidate_pairs([a, b, c], threshold=0.3)
        groups = cluster.group_pairs(pairs, {r['id']: r for r in (a, b, c)})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['member_count'], 3)

    def test_group_reclaimable_keeps_largest(self):
        a = make_record(1, '三体', authors=['刘慈欣'], size=100)
        b = make_record(2, '三体全集', authors=['刘慈欣'], size=300)
        pairs, _stats = cluster.candidate_pairs([a, b], threshold=0.3)
        groups = cluster.group_pairs(pairs, {r['id']: r for r in (a, b)})
        self.assertEqual(groups[0]['bytes_max'], 300)
        self.assertEqual(groups[0]['reclaimable_bytes'], 100)

    def test_pairs_are_canonically_ordered(self):
        a = make_record(9, '三体', authors=['刘慈欣'])
        b = make_record(2, '三体全集', authors=['刘慈欣'])
        pairs, _stats = cluster.candidate_pairs([a, b], threshold=0.3)
        self.assertEqual(pairs[0]['a'], 2)
        self.assertEqual(pairs[0]['b'], 9)


class TestMetadataScore(unittest.TestCase):
    def test_unrated_is_zero_not_present(self):
        """宿主未评分是数值 0，不是缺键——按"键存在"判有值会让全库满分。"""
        record = {'id': 1, 'title': 'X', 'authors': ['A'], 'rating': 0}
        _missing, detail = metadata.missing_fields(record)
        self.assertFalse(detail['rating']['present'])
        zero = metadata.score(record)
        record['rating'] = 8
        self.assertGreater(metadata.score(record), zero)

    def test_unknown_author_is_not_filled(self):
        for placeholder in ('未知作者', '佚名', 'Unknown'):
            record = {'id': 1, 'title': 'X', 'authors': [placeholder]}
            _missing, detail = metadata.missing_fields(record)
            self.assertFalse(detail['authors']['present'], placeholder)

    def test_empty_beats_full(self):
        bare = {'id': 1, 'title': 'X', 'authors': ['未知作者']}
        full = {
            'id': 2, 'title': 'X', 'authors': ['A'], 'comments': '简介',
            'has_cover': True, 'formats': ['EPUB'], 'isbn': '9780306406157',
            'publisher': 'P', 'pubdate': '2020', 'tags': ['t'], 'series': 's',
            'languages': ['zh'], 'rating': 8,
        }
        self.assertEqual(metadata.score(full), 100)
        self.assertLess(metadata.score(bare), 50)

    def test_score_bounds(self):
        self.assertEqual(metadata.score({'id': 1}), 0)


class TestDiff(unittest.TestCase):
    def test_identical_metadata_collapses(self):
        """两份元数据完全一致 → 元数据行不出现，避免界面噪音。"""
        left = make_record(1, '三体', formats=['EPUB'], size=100, added='2026-01-01')
        right = make_record(2, '三体', formats=['EPUB'], size=100, added='2026-01-01')
        for record in (left, right):
            record['_meta_score'] = 50
        self.assertIn('metadata', diff.shared_fields([left, right]))
        self.assertIn('formats', diff.shared_fields([left, right]))
        rows = diff.build_table([left, right])['rows']
        self.assertEqual([r['field'] for r in rows], [])

    def test_differing_field_is_expanded(self):
        left = make_record(1, '三体', formats=['EPUB'], size=100)
        right = make_record(2, '三体', formats=['EPUB', 'PDF'], size=300)
        for record in (left, right):
            record['_meta_score'] = 50
        rows = diff.build_table([left, right])['rows']
        fields = [r['field'] for r in rows]
        self.assertIn('size', fields)
        self.assertIn('formats', fields)

    def test_best_id_only_for_monotone_fields(self):
        left = make_record(1, '三体', size=100)
        right = make_record(2, '三体', size=300)
        left['_meta_score'] = right['_meta_score'] = 50
        table = diff.build_table([left, right])
        size_row = [r for r in table['rows'] if r['field'] == 'size'][0]
        self.assertEqual(size_row['best_id'], 2)
        # 入库时间不是"越大越好"，不该给推荐
        added_row = [r for r in table['rows'] if r['field'] == 'added']
        if added_row:
            self.assertIsNone(added_row[0]['best_id'])

    def test_cells_carry_kind_for_frontend_i18n(self):
        """**回归（review P2）**：与语言有关的取值只送原始值，文案交给前端。

        以前 `_display` 直接返回中文（"2.0 KB"/"9 星"/"实体书"），前端原样渲染，
        于是 en / zh-TW 的对照表整张都是中文。
        """
        left = make_record(1, '三体', formats=['EPUB'], size=100)
        right = make_record(2, '三体', formats=['EPUB', 'PDF'], size=300)
        left['_meta_score'] = right['_meta_score'] = 50
        left['rating'] = 8
        right['rating'] = 0
        left['has_cover'] = True
        right['has_cover'] = False
        rows = {r['field']: r for r in diff.build_table([left, right])['rows']}
        self.assertEqual(rows['size']['cells'][0]['kind'], 'bytes')
        self.assertEqual(rows['size']['cells'][1]['value'], 300)          # 原始字节
        self.assertEqual(rows['rating']['cells'][0]['kind'], 'rating')
        self.assertEqual(rows['rating']['cells'][1]['value'], 0)          # 0 = 未评分
        self.assertEqual(rows['cover']['cells'][0]['kind'], 'bool')
        self.assertTrue(rows['cover']['cells'][0]['value'])
        # 语言无关的字段照旧给文本
        self.assertEqual(rows['formats']['cells'][1]['kind'], 'text')
        self.assertEqual(rows['formats']['cells'][1]['value'], 'EPUB、PDF')

    def test_cover_is_compared_and_recommended(self):
        """封面差异要出现在对照表里，并且"有封面"那本被标为更好。"""
        left = make_record(1, '三体')
        right = make_record(2, '三体')
        left['has_cover'] = False
        right['has_cover'] = True
        rows = {r['field']: r for r in diff.build_table([left, right])['rows']}
        self.assertIn('cover', rows)
        self.assertEqual(rows['cover']['best_id'], 2)
        self.assertEqual(rows['cover']['label'], '封面')


class TestKeeper(unittest.TestCase):
    def _pair(self, **left_extra):
        left = make_record(1, '三体', formats=['EPUB'], size=100, added='2026-01-01')
        right = make_record(2, '三体', formats=['EPUB', 'PDF'], size=300,
                            added='2026-02-01')
        left['_meta_score'] = left_extra.get('left_score', 50)
        right['_meta_score'] = left_extra.get('right_score', 50)
        return [left, right]

    def test_metadata_rule_wins_on_score(self):
        records = self._pair(left_score=90, right_score=10)
        result = keeper.recommend(records, rule='metadata')
        self.assertEqual(result['keeper_id'], 1)
        self.assertTrue(any(r['code'] == 'metadata' for r in result['reasons']))

    def test_formats_rule(self):
        records = self._pair()
        result = keeper.recommend(records, rule='formats')
        self.assertEqual(result['keeper_id'], 2)

    def test_size_rule(self):
        records = self._pair()
        result = keeper.recommend(records, rule='size')
        self.assertEqual(result['keeper_id'], 2)

    def test_oldest_and_newest(self):
        records = self._pair()
        self.assertEqual(keeper.recommend(records, rule='oldest')['keeper_id'], 1)
        self.assertEqual(keeper.recommend(records, rule='newest')['keeper_id'], 2)

    def test_manual_override_beats_rule(self):
        records = self._pair(left_score=90, right_score=10)
        result = keeper.recommend(records, rule='metadata', manual_id=2)
        self.assertEqual(result['keeper_id'], 2)
        self.assertEqual(result['reasons'][0]['code'], 'manual')

    def test_tie_break_is_total(self):
        """完全相同的两份：靠“最早入库”定，不出现"打平怎么办"。"""
        left = make_record(1, '三体', size=100, added='2026-01-01')
        right = make_record(2, '三体', size=100, added='2026-03-01')
        left['_meta_score'] = right['_meta_score'] = 50
        self.assertEqual(keeper.recommend([left, right], rule='metadata')['keeper_id'], 1)

    def test_reclaimable_and_disk_waste(self):
        left = make_record(1, '三体', formats=['EPUB'], size=100)
        right = make_record(2, '三体', formats=['EPUB'], size=300)
        for record in (left, right):
            record['_meta_score'] = 50
        self.assertEqual(keeper.reclaimable_bytes([left, right], 2), 100)
        # 同格式重叠的体积才是真省下来的
        self.assertEqual(keeper.duplicate_disk_waste([left, right], 2), 100)

    def test_disk_waste_zero_when_formats_disjoint(self):
        """keeper 缺的格式会被搬过去，旧记录仍占着文件——那部分不算已省。"""
        left = make_record(1, '三体', formats=['PDF'], size=100)
        right = make_record(2, '三体', formats=['EPUB'], size=300)
        for record in (left, right):
            record['_meta_score'] = 50
        self.assertEqual(keeper.duplicate_disk_waste([left, right], 2), 0)

    def test_protected_hook_is_empty_for_now(self):
        """本期读不到用户数据，protected 必须恒为空，不能让界面以为有保护。"""
        records = self._pair()
        self.assertEqual(keeper.protected_members(records), [])
        self.assertEqual(keeper.recommend(records, )['protected'], [])

    def test_unknown_rule_falls_back_to_metadata(self):
        records = self._pair(left_score=90, right_score=10)
        self.assertEqual(keeper.recommend(records, rule='nonsense')['keeper_id'], 1)


class TestReport(unittest.TestCase):
    def _report(self):
        a = make_record(1, '三体', authors=['刘慈欣'], formats=['EPUB'], size=100)
        b = make_record(2, '三体全集', authors=['刘慈欣'], formats=['EPUB'], size=300)
        c = make_record(3, '活着', authors=['余华'], formats=['EPUB'], size=200)
        d = make_record(4, '活着', authors=['余华'], formats=['EPUB'], size=250)
        records = [a, b, c, d]
        for record in records:
            record['_meta_score'] = metadata.score(record)
        pairs, stats = cluster.candidate_pairs(records, threshold=0.3)
        groups = cluster.group_pairs(pairs, {r['id']: r for r in records})
        return report.build_report(records, pairs, groups, 0.85, stats)

    def test_summary_counts(self):
        built = self._report()
        self.assertEqual(built['scanned_books'], 4)
        self.assertGreaterEqual(built['summary']['group_count'], 2)
        self.assertGreater(built['summary']['extra_copies'], 0)

    def test_groups_are_sorted_by_confidence_then_size(self):
        built = self._report()
        ranks = [g['confidence'] for g in built['groups']]
        self.assertTrue(ranks)

    def test_confidence_tier_splits_fuzzy_by_similarity(self):
        """**回归（review P2）**：模糊命中要按相似度分"很可能 / 存疑"。

        以前 rank 1 一律叫"很可能"，而"存疑"在候选生成里根本不可能出现——
        界面上的"存疑"筛选器永远筛不出东西，真书库 1584 组里 1582 组都显示"很可能"。
        """
        self.assertEqual(report.confidence_of(3), 'strong')                    # ISBN 命中
        self.assertEqual(report.confidence_of(1, 1.0), 'likely')
        self.assertEqual(report.confidence_of(1, report.SIMILARITY_LIKELY), 'likely')
        self.assertEqual(report.confidence_of(1, 0.9), 'weak')
        self.assertEqual(report.confidence_of(0), 'weak')
        self.assertEqual(report.confidence_of(2), 'likely')

    def test_summary_reports_serial_excluded(self):
        """因卷册序号被排除的候选对要进 summary（不静默吞掉）。"""
        left = make_record(1, '德川家康（第一部）')
        right = make_record(2, '德川家康（第十二部）')
        pairs, stats = cluster.candidate_pairs([left, right], threshold=0.85)
        self.assertEqual(pairs, [])
        self.assertEqual(stats['skipped_serial'], 1)
        built = report.build_report([left, right], pairs, [], 0.85, stats)
        self.assertEqual(built['summary']['serial_excluded'], 1)

    def test_paginate_clamps(self):
        groups = list(range(120))
        page, total = report.paginate(groups, page=0, size=50)
        self.assertEqual(len(page), 50)
        self.assertEqual(total, 120)
        page, _total = report.paginate(groups, page=99, size=50)
        self.assertEqual(page, [])

    def test_member_shape_is_narrow(self):
        """报告里每条成员只带白名单字段——别把整份数据字典写进报告文件。"""
        built = self._report()
        member = built['groups'][0]['members'][0]
        self.assertIn('meta_score', member)
        self.assertIn('missing_fields', member)
        self.assertNotIn('_title_key', member)
        self.assertNotIn('_author_keys', member)

    def test_disk_waste_surfaced_per_group(self):
        built = self._report()
        for group in built['groups']:
            self.assertIn('disk_waste_bytes', group)
            self.assertLessEqual(group['disk_waste_bytes'], group['reclaimable_bytes'])


if __name__ == '__main__':
    unittest.main(verbosity=2)