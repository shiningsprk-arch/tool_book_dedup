# -*- coding: utf-8 -*-
"""离线 smoke：拿真 calibre 书库跑完整查重流程，不需要 MyBooks、不需要 calibre。

做法与 `tool_quality_check/scripts/smoke_offline.py` 一致：直接用 sqlite3 读
`metadata.db`，把 CoreAPI 用替身顶上，于是 driver + 引擎都能在打包前跑通。

用法：
    python scripts/smoke_offline.py [path/to/library] [--threshold 0.85]
    python scripts/smoke_offline.py --perf        # 25k 合成库的扫描耗时

默认库是 MyBooks clone 里的测试库。
"""
import argparse
import importlib.util
import os
import random
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, '..', 'backend'))
DEFAULT_LIBRARY = os.path.join(
    HERE, '..', '..', 'mybooks源码', 'mybooks-v4.2.1', 'tests', 'library')


def load_package():
    """按宿主 toolbox_manager 的方式加载 backend/。"""
    name = 'mybooks_tool_book_dedup_smoke'
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(BACKEND, '__init__.py'),
        submodule_search_locations=[BACKEND])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return package


class FakeCalibre(object):
    """CoreAPI.calibre 里本工具用到的那一小片，背后是 calibre 的 metadata.db。

    键名刻意照抄宿主 `get_data_as_dict()` 的真实形状——替身是唯一站在宿主位置上的东西，
    自己编一个宿主没有的键只会把 bug 藏起来而不是抓出来。
    """

    def __init__(self, library):
        self.library = library
        self.conn = sqlite3.connect(
            os.path.join(library, 'metadata.db'), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._books = self._load_books()

    def _rows(self, sql, args=()):
        try:
            return self.conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []

    def _load_books(self):
        books = {}
        for row in self._rows('SELECT * FROM books'):
            keys = set(row.keys())
            book_id = row['id']
            record = {
                'id': book_id,
                'title': row['title'],
                'sort': row['sort'],           # 宿主的数据字典里排序键就是 sort
                'author_sort': row['author_sort'],
                'path': row['path'],
                'series_index': row['series_index'],
                'pubdate': row['pubdate'],
                'timestamp': row['timestamp'],
                'cover': row['has_cover'] if 'has_cover' in keys else 0,
                'isbn': row['isbn'] if 'isbn' in keys else '',
            }
            record['authors'] = [r['name'] for r in self._rows(
                'SELECT a.name FROM authors a JOIN books_authors_link l ON l.author=a.id '
                'WHERE l.book=? ORDER BY l.id', (book_id,))]
            record['author'] = ' & '.join(record['authors'])
            series = self._rows(
                'SELECT s.name FROM series s JOIN books_series_link l ON l.series=s.id '
                'WHERE l.book=?', (book_id,))
            if series:
                record['series'] = series[0]['name']
            record['tags'] = [r['name'] for r in self._rows(
                'SELECT t.name FROM tags t JOIN books_tags_link l ON l.tag=t.id '
                'WHERE l.book=?', (book_id,))]
            langs = [r['lang_code'] for r in self._rows(
                'SELECT lang_code FROM languages l JOIN books_languages_link k '
                'ON k.lang_code=l.id WHERE k.book=?', (book_id,))]
            if langs:
                record['languages'] = langs
            publishers = self._rows(
                'SELECT p.name FROM publishers p JOIN books_publishers_link l '
                'ON l.publisher=p.id WHERE l.book=?', (book_id,))
            if publishers:
                record['publisher'] = publishers[0]['name']
            comments = self._rows('SELECT text FROM comments WHERE book=?', (book_id,))
            if comments:
                record['comments'] = comments[0]['text']
            ratings = self._rows(
                'SELECT r.rating FROM ratings r JOIN books_ratings_link l '
                'ON l.rating=r.id WHERE l.book=?', (book_id,))
            # 未评分是数值 0（不是缺键）——照宿主口径
            record['rating'] = ratings[0]['rating'] if ratings else 0
            record['translators'] = []

            # calibre 的 metadata.db 里 data.name **不带扩展名**（格式在 data.format 列），
            # 磁盘上的真实文件名是 `<name>.<小写格式>` —— 拼错就会 os.path.getsize 失败、
            # 体积一律记 0（真书库也一样，不只是替身的问题）
            fmt_rows = self._rows('SELECT format, name FROM data WHERE book=?', (book_id,))
            record['available_formats'] = [r['format'].upper() for r in fmt_rows]
            record['_format_files'] = {}
            for row in fmt_rows:
                fmt = row['format'].upper()
                path = os.path.join(self.library, record['path'],
                                    '%s.%s' % (row['name'], row['format'].lower()))
                if not os.path.exists(path):
                    continue
                record['_format_files'][fmt] = path
            books[book_id] = record
        return books

    # --- CoreAPI.calibre 的那几个方法
    def all_book_ids(self):
        return sorted(self._books)

    def get_data_as_dict(self, ids):
        return [self._books[i] for i in ids if i in self._books]

    def format_abspath(self, book_id, fmt):
        book = self._books.get(book_id)
        if not book:
            return None
        key = str(fmt).upper()
        # `_paths` 是预览脚本给注入副本挂的键，`_format_files` 是这里的规范键
        return (book.get('_format_files') or book.get('_paths') or {}).get(key)


class FakeApi(object):
    """整个 CoreAPI 的替身（工具只用到 calibre 这一片）。"""

    def __init__(self, library):
        self.calibre = FakeCalibre(library)


def section(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


def run_library(library, threshold, inject=0):
    """跑真书库。

    `inject` 会基于真记录派生出 N 组"同一本书的另一种写法"，用来验证引擎**查得到**；
    不注入时的那 13 本真书是验证**不误报**的对照（同一个阈值下必须 0 组）。
    """
    package = load_package()
    driver = importlib.import_module(package.__name__ + '.driver')
    cluster = importlib.import_module(package.__name__ + '.dedup.cluster')
    api = FakeApi(library)

    section('查重扫描：%s' % library)
    book_ids = api.calibre.all_book_ids()
    print('书库规模: %d 本' % len(book_ids))
    if inject:
        _inject_duplicates(api, inject)
        book_ids = sorted(api.calibre._books)
        print('注入重复: %d 组 → 书库 %d 本' % (inject, len(book_ids)))

    started = time.time()
    phases = []

    def on_progress(done, total, phase=''):
        phases.append(phase)

    report = driver.run_scan(api, book_ids, threshold=threshold,
                             on_progress=on_progress)
    elapsed = time.time() - started

    summary = report['summary']
    print('耗时      : %.2fs' % elapsed)
    print('扫描本数  : %d' % report['scanned_books'])
    print('比较次数  : %d' % report['stats']['comparisons'])
    print('阈        : %.2f' % report['threshold'])
    print('分组      : %d 组 / 多余 %d 本' % (
        summary['group_count'], summary['extra_copies']))
    print('可回收    : %.1f MB' % (summary['reclaimable_bytes'] / 1024.0 / 1024.0))
    print('其中同格式: %.1f MB（真会省下的）' % (
        summary['disk_waste_bytes'] / 1024.0 / 1024.0))
    print('强证据组  : %d，存疑组: %d' % (
        summary['strong_groups'], summary['weak_groups']))
    print('封面键可用: %s' % ('是' if any(
        b.get('cover') is not None for b in api.calibre._books.values()) else '否'))

    for group in report['groups'][:8]:
        titles = ' | '.join(m['title'][:18] for m in group['members'])
        print('  · [%s] %s  相似度=%.3f' % (
            group['confidence_label'], titles, group['max_similarity']))

    assert report['scanned_books'] == len(book_ids), '扫描本数与书库不符'
    assert set(phases) & {'load', 'compare'}, '进度阶段没有回调'
    if inject:
        assert summary['group_count'] >= inject, (
            '注入了 %d 组重复，只查到 %d 组' % (inject, summary['group_count']))
        # 至少要有多余副本，否则"可回收"永远是 0，界面看不出价值
        assert summary['extra_copies'] >= inject, '多余副本数不足'
        print('注入的 %d 组全部命中 ✓' % inject)
    else:
        assert summary['group_count'] == 0, (
            '干净书库上出现 %d 组误报' % summary['group_count'])
        print('干净书库 0 误报 ✓')
    return report


def _inject_duplicates(api, count):
    """基于真记录派生重复项：同作者、标题加后缀（模拟"另一个来源的同名书"）。

    刻意让"重复项"的格式与体积都不同——真实的重复就是这样（一个来源只有 EPUB、
    另一个还带 PDF），只测"标题一模一样"的情况会漏掉最该验的合并预览。

    :return: `[(source_id, clone_id), ...]`，调用方可以进一步摆布这些副本
             （预览脚本就靠它给副本挂上真实存在、体积不同的格式文件）。
    """
    books = api.calibre._books
    originals = sorted(books)[:count]
    next_id = max(books) + 1
    formats_pool = [['EPUB'], ['EPUB', 'PDF'], ['EPUB', 'MOBI'], ['PDF']]
    created = []
    for offset, source_id in enumerate(originals):
        source = dict(books[source_id])
        clone = dict(source)
        clone['id'] = next_id + offset
        clone['title'] = '%s（全集）' % source['title']      # 标题差异：走模糊匹配
        clone['available_formats'] = formats_pool[offset % len(formats_pool)]
        clone['_format_files'] = dict(source.get('_format_files') or {})
        # 不给 `_paths`：格式文件路径留空，体积会因文件缺失记为 0，
        # 正好覆盖"体积读不到"这个分支；调用方需要真实体积时自己补 `_paths`
        clone.pop('_paths', None)
        books[clone['id']] = clone
        created.append((source_id, clone['id']))
    return created


def run_perf(count):
    """合成库性能：验证"作者桶内比较"在 25k 本规模上真的收得住。

    作者池刻意取到 200 人 —— 只放 7 个作者的话每个桶都是几千本，测出来的是**最坏情况**
    （实际书库作者分散得多）。最坏情况跑得过，真实分布就有余量。
    """
    package = load_package()
    driver = importlib.import_module(package.__name__ + '.driver')
    cluster = importlib.import_module(package.__name__ + '.dedup.cluster')

    section('性能实测：%d 本合成库（作者池 200 人，随机构造）' % count)
    random.seed(20260923)
    surnames = ['刘', '余', '莫', '王', '李', '张', '陈', '赵', '孙', '周',
                '吴', '郑', '冯', '蒋', '沈', '韩', '杨', '朱', '秦', '许']
    given = ['慈欣', '华', '言', '小波', '敖', '爱玲', '忠实', '平凹', '连科',
             '晓声', '一梅', '黎', '岭', '岩', '宏伟', '长江', '蕾', '冬', '三', '四']
    nouns = ['三体', '活着', '红高粱', '黄金时代', '动物凶猛', '倾城之恋', '白鹿原',
             '长恨歌', '平凡的世界', '围城', '骆驼祥子', '呐喊', '边城', '雷雨']
    authors = ['%s%s' % (surnames[i // len(given)], given[i % len(given)])
               for i in range(200)]

    records = []
    for index in range(count):
        author = random.choice(authors)
        title = '%s%d' % (random.choice(nouns), random.randint(1, 3))
        record = {
            'id': index + 1,
            'title': title,
            'authors': [author],
            'formats': ['EPUB'],
            'size': random.randint(500_000, 5_000_000),
            'added': '2026-01-01T00:00:00',
            'isbn': '',
        }
        cluster.prepare(record)
        records.append(record)

    started = time.time()
    pairs, stats = cluster.candidate_pairs(records, threshold=0.85)
    elapsed = time.time() - started
    print('配对耗时    : %.2fs' % elapsed)
    print('比较次数    : %d' % stats['comparisons'])
    print('被跳过桶    : %d（涉及 %d 本）' % (
        stats['skipped_buckets'], stats['skipped_books']))
    print('候选对      : %d' % len(pairs))
    budget = 60.0
    if elapsed > budget:
        print('!! 超出 %.0fs 预算，需要进一步降复杂度' % budget)
        return False
    print('在 %.0fs 预算内 ✓' % budget)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('library', nargs='?', default=DEFAULT_LIBRARY)
    parser.add_argument('--threshold', type=float, default=0.85)
    parser.add_argument('--inject', type=int, default=0,
                        help='基于真记录派生 N 组重复，验证"查得到"')
    parser.add_argument('--perf', action='store_true', help='跑合成库性能实测')
    parser.add_argument('--perf-count', type=int, default=25000)
    parser.add_argument('--all', action='store_true',
                        help='干净库 + 注入库 + 性能，一次跑完')
    args = parser.parse_args()

    ok = True
    if args.all:
        library = os.path.abspath(args.library)
        if not os.path.exists(os.path.join(library, 'metadata.db')):
            print('找不到 metadata.db: %s' % library)
            return 2
        try:
            run_library(library, args.threshold, inject=0)
            run_library(library, args.threshold, inject=8)
        except AssertionError as err:
            print('断言失败: %s' % err)
            ok = False
        ok = run_perf(args.perf_count) and ok
    elif args.perf:
        ok = run_perf(args.perf_count)
    else:
        library = os.path.abspath(args.library)
        if not os.path.exists(os.path.join(library, 'metadata.db')):
            print('找不到 metadata.db: %s' % library)
            return 2
        try:
            run_library(library, args.threshold, inject=args.inject)
        except AssertionError as err:
            print('断言失败: %s' % err)
            ok = False

    section('结论')
    print('通过 ✓' if ok else '失败 ✗')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())