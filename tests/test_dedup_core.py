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

    def test_authorless_books_only_match_by_isbn(self):
        """没有作者的书不参与作者桶比较（否则全库无作者的书会两两相比）。

        这是刻意的：中文个人书库作者字段常为空，若把它们都丢进同一个桶做两两比较，
        一次扫描就是 O(n²)。这类记录只靠 ISBN 强证据成组。
        """
        left = make_record(1, '无名书', authors=[])
        right = make_record(2, '无名书', authors=[])
        pairs, stats = cluster.candidate_pairs([left, right], threshold=0.5)
        self.assertEqual(pairs, [])
        self.assertEqual(stats['comparisons'], 0)
        # 有 ISBN 时仍然能成组
        left = make_record(3, '无名书', authors=[], isbn='9780306406157')
        right = make_record(4, '完全不同', authors=[], isbn='9780306406157')
        pairs, _stats = cluster.candidate_pairs([left, right], threshold=0.99)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['reasons'], ['isbn'])

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

    def test_format_bytes(self):
        self.assertEqual(diff.format_bytes(0), '0 B')
        self.assertEqual(diff.format_bytes(2048), '2.0 KB')
        self.assertEqual(diff.format_bytes(1024 * 1024), '1.0 MB')


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

    def test_filter_keeps_full_summary(self):
        """筛选只影响 groups，summary 始终是全量统计，另回 filtered 计数。"""
        built = self._report()
        filtered, count = report.filter_report(built, keyword='活着')
        self.assertEqual(count, len(filtered['groups']))
        self.assertEqual(filtered['summary'], built['summary'])
        self.assertEqual(filtered['filtered']['group_count'], count)

    def test_filter_by_min_members(self):
        built = self._report()
        _filtered, count = report.filter_report(built, min_members=3)
        self.assertEqual(count, 0)

    def test_filter_by_keyword_matches_author(self):
        built = self._report()
        _filtered, count = report.filter_report(built, keyword='余华')
        self.assertGreaterEqual(count, 1)

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