# -*- coding: utf-8 -*-
"""假宿主 harness：验证 tool.py 的接线与合并的守门。

`tool.py` 依赖 `webserver.*`（宿主），离线导不进来，所以这里把 `webserver.*` 用替身塞进
`sys.modules`，再导入**真的** `tool.py` 与 `merge.py` —— 与 `quality_check` 第六轮同一套
做法。这样能在没有 MyBooks 的机器上验证：

- 报告落盘 → `/groups` 能读、`/progress` 重启后能回退认回
- 分页/筛选计数
- **合并**：预览列出"哪些格式会被复制、哪些同格式会被丢弃"；执行后记账、已合并的组消失
- 守门：保留项必须是组内成员；`/merge` 不接受前端传成员列表

运行：python tests/test_fake_host.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
BACKEND = os.path.join(ROOT, 'backend')

PKG_NAME = 'book_dedup_under_test'


# --------------------------------------------------------------------------- 替身


class FakeTask(object):
    # 真 BackgroundTask 的常量（tool.py 在模块级读它们）
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'

    def __init__(self, task_id):
        self.task_id = task_id
        self.status = self.STATUS_RUNNING
        self.progress = 0
        self.progress_data = {}
        self.error_message = None
        self.id = task_id


class FakeBackgroundService(object):
    _tasks = {}
    lock = __import__('threading').RLock()
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'

    def get_task(self, task_id):
        return FakeBackgroundService._tasks.get(task_id)


def _install_fake_webserver(tmp_root, library_api):
    """把 webserver.* 换成一堆刚好够用的替身。"""
    modules = {}

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        modules[name] = mod
        sys.modules[name] = mod
        return mod

    def _(text):
        return text

    module('webserver')
    module('webserver.i18n', _=_)

    handlers_mod = types.ModuleType('webserver.handlers')
    sys.modules['webserver.handlers'] = handlers_mod
    module('webserver.handlers.base', BaseHandler=object, is_admin=lambda fn: fn,
           js=lambda fn: fn)

    class FakeTool(object):
        """BaseTool 的替身：工作目录、任务、API 都换掉，其余照真实现用。"""
        service_item_name = ''
        TOOL_DATA_ROOT = tmp_root
        _counters = {'task': 100}

        def __init__(self):
            self.api = library_api

        @classmethod
        def tool_id(cls):
            return cls.info()['tool_id']

        def get_work_dir(self, unique_key=''):
            tool_dir = os.path.join(self.TOOL_DATA_ROOT, self.tool_id())
            if unique_key:
                import hashlib
                tool_dir = os.path.join(
                    tool_dir, hashlib.md5(unique_key.encode()).hexdigest()[:16])
            os.makedirs(tool_dir, exist_ok=True)
            return tool_dir

        def cleanup_work_dir(self, work_dir):
            pass

        def create_task(self, progress_data=None):
            FakeTool._counters['task'] += 1
            task_id = FakeTool._counters['task']
            task = FakeTask(task_id)
            task.progress_data = progress_data or {}
            FakeBackgroundService._tasks[task_id] = task
            return task_id

        def update_task_progress(self, task_id, progress, progress_data=None):
            task = FakeBackgroundService._tasks.get(task_id)
            if task:
                task.progress = progress
                if progress_data:
                    task.progress_data = dict(progress_data)

        def complete_task(self, task_id, error_message=None):
            task = FakeBackgroundService._tasks.get(task_id)
            if task:
                task.status = 'failed' if error_message else 'completed'
                task.error_message = error_message

    module('webserver.services')
    module('webserver.services.background_service',
           BackgroundService=FakeBackgroundService, BackgroundTask=FakeTask)
    module('webserver.toolbox')
    module('webserver.toolbox.base_tool', BaseTool=FakeTool)
    return modules, FakeTool


class FakeCalibre(object):
    """CoreAPI.calibre 替身：内存书库 + 记录每次写操作。"""

    def __init__(self, books):
        self.books = books        # {id: {title, authors, available_formats, size, isbn, ...}}
        self.calls = []

    def all_book_ids(self):
        return sorted(self.books)

    def get_data_as_dict(self, ids):
        return [self.books[i] for i in ids if i in self.books]

    def format_abspath(self, book_id, fmt):
        book = self.books.get(book_id)
        if not book:
            return None
        return book.get('_paths', {}).get(str(fmt).upper())

    def merge_formats(self, source_id, target_id):
        """复刻宿主行为：只复制目标缺的格式；没有新格式时抛 RuntimeError。"""
        self.calls.append(('merge_formats', source_id, target_id))
        source = self.books[source_id]
        target = self.books[target_id]
        source_formats = set(f.upper() for f in source['available_formats'])
        target_formats = set(f.upper() for f in target['available_formats'])
        new_formats = sorted(source_formats - target_formats)
        if not new_formats:
            raise RuntimeError('来源书籍没有目标书籍中缺少的格式，无需合并')
        target['available_formats'] = sorted(target_formats | source_formats)
        return new_formats

    def delete_book(self, book_id):
        self.calls.append(('delete_book', book_id))
        self.books.pop(book_id, None)


class FakeApi(object):
    def __init__(self, books):
        self.calibre = FakeCalibre(books)


def load_tool(tmp_root, api, shared_root=None):
    """加载真 tool.py / merge.py / driver.py。

    每个用例给一个独立的 `shared_root`：`latest.json` 与 `merged.json` 都落在共享目录
    （`get_work_dir()` 无 key 那级），不隔离的话上一个用例的合并记账会污染下一个
    （症状是"这一组已经处理过"）。
    """
    modules, _fake_tool = _install_fake_webserver(tmp_root, api)
    # 同一个包名被反复加载：不清缓存的话第二次会拿到**上一个用例的模块**，
    # 里面的 TOOL_DATA_ROOT 还指向旧的临时目录（症状是"这一组已经处理过"这种串味）。
    for name in [key for key in list(sys.modules)
                 if key == PKG_NAME or key.startswith(PKG_NAME + '.')]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PKG_NAME, os.path.join(BACKEND, '__init__.py'),
        submodule_search_locations=[BACKEND])
    package = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = package
    spec.loader.exec_module(package)
    driver = importlib.import_module(PKG_NAME + '.driver')
    tool = importlib.import_module(PKG_NAME + '.tool')
    merge = importlib.import_module(PKG_NAME + '.merge')
    if shared_root:
        tool.BookDedupTool.shared_work_dir = classmethod(
            lambda cls, _root=shared_root: _root)
    return tool, driver, merge


def make_books():
    """两组重复 + 一本无关书。

    A 组：同一本书两条记录，格式重叠（都有 EPUB）+ 各有独有格式（MOBI / PDF）
    B 组：靠 ISBN 命中的同一本书（标题写法不同）
    """
    return {
        1: {'id': 1, 'title': '三体', 'authors': ['刘慈欣'], 'available_formats': ['EPUB', 'MOBI'],
            'isbn': '9787536692930', 'rating': 8, 'tags': ['科幻'], 'languages': ['zh'],
            'comments': '简介', 'cover': True, 'publisher': '重庆出版社',
            'pubdate': '2008-01-01', 'series': '三体', 'series_index': 1,
            'timestamp': '2026-01-01T00:00:00+00:00', '_paths': {}},
        2: {'id': 2, 'title': '三体（全集）', 'authors': ['刘慈欣'],
            'available_formats': ['EPUB', 'PDF'],
            'isbn': '', 'rating': 0, 'tags': [], 'languages': [],
            'comments': '', 'cover': False,
            'timestamp': '2026-05-01T00:00:00+00:00', '_paths': {}},
        3: {'id': 3, 'title': 'To Live', 'authors': ['余华'],
            'available_formats': ['EPUB'], 'isbn': '9787506365437',
            'timestamp': '2026-02-01T00:00:00+00:00', '_paths': {}},
        4: {'id': 4, 'title': '活着', 'authors': ['余华'],
            'available_formats': ['EPUB'], 'isbn': '978-7-5063-6543-7',
            'timestamp': '2026-03-01T00:00:00+00:00', '_paths': {}},
        5: {'id': 5, 'title': '完全无关的书', 'authors': ['另一个人'],
            'available_formats': ['EPUB'], 'isbn': '',
            'timestamp': '2026-04-01T00:00:00+00:00', '_paths': {}},
    }


def scan(tool, driver, api, threshold=0.85):
    """直接跑一次 driver 扫描并落盘，模拟"扫完了"的状态。"""
    built = driver.run_scan(api, api.calibre.all_book_ids(), threshold=threshold)
    work_dir = tool.BookDedupTool.report_dir(7)
    driver.write_report(work_dir, built)
    driver.write_index(work_dir, built)
    driver.write_latest_marker(tool.BookDedupTool.shared_dir(), 7, built)
    return built, work_dir


# --------------------------------------------------------------------------- 用例


class TestToolWiring(unittest.TestCase):
    def setUp(self):
        # 每个用例一个干净的 FakeBackgroundService / 临时目录
        FakeBackgroundService._tasks = {}
        self.tmp = tempfile.mkdtemp(prefix='book_dedup_test_')
        self.shared = tempfile.mkdtemp(prefix='book_dedup_shared_')
        self.api = FakeApi(make_books())
        self.tool, self.driver, self.merge = load_tool(self.tmp, self.api, self.shared)
        self.tool.BookDedupTool._last_task_id = None
        self.tool.BookDedupTool._accepted = False

    def test_info_matches_manifest(self):
        manifest_path = os.path.join(ROOT, 'manifest.json')
        with open(manifest_path, 'r', encoding='utf-8') as handle:
            manifest = json.load(handle)
        info = self.tool.BookDedupTool.info()
        self.assertEqual(info['tool_id'], manifest['tool_id'])
        self.assertEqual(info['revision'], manifest['revision'])
        self.assertEqual(info['name'], manifest['name'])
        self.assertEqual(manifest['core_api_version'], '1.3.0')

    def test_routes_match_manifest(self):
        manifest_path = os.path.join(ROOT, 'manifest.json')
        with open(manifest_path, 'r', encoding='utf-8') as handle:
            manifest = json.load(handle)
        declared = sorted(route['path'] for route in manifest['api_routes'])
        implemented = sorted(path for path, _handler in self.tool.ROUTES)
        self.assertEqual(declared, implemented)

    def test_scan_writes_report_and_index(self):
        built, work_dir = scan(self.tool, self.driver, self.api)
        self.assertTrue(os.path.exists(self.driver.report_path(work_dir)))
        index = self.driver.read_index(work_dir)
        self.assertIsNotNone(index)
        self.assertEqual(index['summary']['group_count'], built['summary']['group_count'])

    def test_restored_progress_after_restart(self):
        """宿主重启后内存任务没了，/progress 要能认回上次那份报告。"""
        _built, work_dir = scan(self.tool, self.driver, self.api)
        tool_class = self.tool.BookDedupTool
        tool_class._last_task_id = None          # 模拟进程重启
        marker = self.driver.read_latest_marker(tool_class.shared_dir())
        self.assertIsNotNone(marker)
        restored = self.driver.restored_progress(marker)
        self.assertTrue(restored['restored'])
        self.assertEqual(restored['status'], 'completed')
        self.assertIn('summary', restored)

    def test_marker_double_check_rejects_stale_task_id(self):
        """task_id 是进程内自增、会重来，只按 id 找会把旧报告当成新结果。"""
        built, work_dir = scan(self.tool, self.driver, self.api)
        marker = self.driver.read_latest_marker(self.tool.BookDedupTool.shared_dir())
        self.assertTrue(self.driver.marker_matches(marker, 7, built))
        self.assertFalse(self.driver.marker_matches(marker, 8, built))
        other = dict(built)
        other['generated_at'] = '1999-01-01 00:00:00'
        self.assertFalse(self.driver.marker_matches(marker, 7, other))

    def test_groups_filters_by_confidence(self):
        built, work_dir = scan(self.tool, self.driver, self.api)
        index = self.driver.read_index(work_dir)
        groups = index['groups']
        strong = [g for g in groups if g['confidence'] == 'strong']
        likely = [g for g in groups if g['confidence'] == 'likely']
        self.assertTrue(strong, '应有 ISBN 命中的强证据组')
        self.assertTrue(likely, '应有标题模糊命中的组')
        self.assertEqual(index['summary']['group_count'], len(groups))


class TestMerge(unittest.TestCase):
    def setUp(self):
        FakeBackgroundService._tasks = {}
        self.tmp = tempfile.mkdtemp(prefix='book_dedup_merge_')
        self.shared = tempfile.mkdtemp(prefix='book_dedup_merge_shared_')
        self.api = FakeApi(make_books())
        self.tool, self.driver, self.merge = load_tool(self.tmp, self.api, self.shared)
        self.tool_class = self.tool.BookDedupTool
        self.tool_class._last_task_id = None
        self.tool_class._accepted = False
        self.built, self.work_dir = scan(self.tool, self.driver, self.api)

    def _group_index_of(self, title):
        report = self.driver.read_report(self.work_dir)
        for position, group in enumerate(report['groups']):
            for member in group['members']:
                if member['title'] == title:
                    return position, group
        raise AssertionError('找不到分组：%s' % title)

    def test_plan_lists_moved_and_dropped_formats(self):
        """预览必须分清"会被复制过去的格式"与"同格式会被丢弃的那一份"。"""
        position, group = self._group_index_of('三体')
        plan, error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.assertIsNone(error)
        self.assertEqual(plan['keeper_id'], 1)
        self.assertEqual(plan['moved_total'], 1)      # 源书的 PDF 进 keeper
        self.assertEqual(plan['dropped_total'], 1)    # 两边都有 EPUB → 源书那份丢弃
        step = plan['steps'][0]
        self.assertEqual(step['moved_formats'], ['PDF'])
        self.assertEqual(step['dropped_formats'], ['EPUB'])

    def test_plan_rejects_keeper_outside_group(self):
        position, _group = self._group_index_of('三体')
        _plan, error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=5)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'keeper.not_in_group')

    def test_plan_rejects_bad_index(self):
        _plan, error = self.merge.build_plan(self.api, self.work_dir, 999)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'group.not_found')

    def test_execute_merges_formats_then_deletes_source(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        result = self.merge.execute(self.api, self.work_dir, plan)
        self.assertEqual(result['err'], 'ok')
        data = result['data']
        self.assertEqual(data['removed_ids'], [2])
        self.assertIn('PDF', data['steps'][0]['moved_formats'])
        # keeper 拿到了源书独有的格式
        self.assertIn('PDF', self.api.calibre.books[1]['available_formats'])
        # 源记录真的被删了
        self.assertNotIn(2, self.api.calibre.books)
        self.assertIn(('delete_book', 2), self.api.calibre.calls)

    def test_execute_records_ledger_and_hides_group(self):
        """合并后这一组只剩一本 → 列表要把它摘掉，不能引导用户再点一次。"""
        position, _group = self._group_index_of('三体')
        plan, _error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.merge.execute(self.api, self.work_dir, plan)

        merged_ids = self.merge.read_merged_ids(self.work_dir)
        self.assertEqual(merged_ids, [2])
        titles = self.merge.read_removed_titles(self.work_dir)
        self.assertEqual(titles[0]['title'], '三体（全集）')
        self.assertEqual(titles[0]['into'], '三体')

        index = self.driver.read_index(self.work_dir)
        remaining = [g for g in index['groups']
                     if not (set(g.get('members') or []) & set(merged_ids))]
        self.assertNotIn(position, [g['index'] for g in remaining])

    def test_double_merge_is_refused(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.merge.execute(self.api, self.work_dir, plan)
        _plan2, error = self.merge.build_plan(self.api, self.work_dir, position)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'group.already_merged')

    def test_execute_without_delete_keeps_source(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        result = self.merge.execute(self.api, self.work_dir, plan, delete_source=False)
        self.assertEqual(result['err'], 'ok')
        self.assertEqual(result['data']['removed_ids'], [])
        self.assertIn(2, self.api.calibre.books)          # 源记录还在
        self.assertNotIn(('delete_book', 2), self.api.calibre.calls)

    def test_plan_survives_no_new_formats(self):
        """两组格式完全相同时 merge_formats 会抛错，合并要照常继续（只是没复制什么）。"""
        position, _group = self._group_index_of('To Live')
        plan, error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=3)
        self.assertIsNone(error)
        result = self.merge.execute(self.api, self.work_dir, plan)
        self.assertEqual(result['err'], 'ok')
        self.assertEqual(result['data']['moved_total'], 0)
        # 源书仍然被删（用户要的就是"消掉这一条"），并记账为"没有新格式"
        self.assertEqual(result['data']['removed_ids'], [4])
        self.assertIn('no_new_formats', result['data']['steps'][0]['notes'])

    def test_merge_plan_warns_about_limitations(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.merge.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.assertIn('working_formats_dropped', plan['warnings'])
        self.assertIn('source_records_not_migrated', plan['warnings'])


class TestWritePathGuard(unittest.TestCase):
    """守门：整个工具只有 merge.py 允许调用删/改书库的方法。"""

    WRITE_CALLS = ('delete_book', 'merge_formats', 'remove_formats', 'set_metadata',
                   'set_cover', 'import_book', 'import_file', 'add_format')

    def _py_files(self):
        for folder in ('backend', 'backend/dedup'):
            directory = os.path.join(ROOT, folder)
            for name in sorted(os.listdir(directory)):
                if name.endswith('.py'):
                    yield os.path.join(directory, name)

    def test_only_driver_and_merge_touch_write_calls(self):
        allowed = {'merge.py', 'driver.py'}
        offenders = {}
        for path in self._py_files():
            with open(path, 'r', encoding='utf-8') as handle:
                text = handle.read()
            for call in self.WRITE_CALLS:
                if '.%s(' % call in text or "'%s'" % call in text:
                    if os.path.basename(path) not in allowed:
                        offenders.setdefault(os.path.basename(path), []).append(call)
        self.assertEqual(offenders, {}, '这些文件不该直接调用写操作：%s' % offenders)

    def test_engine_has_no_host_write_calls(self):
        """引擎（dedup/）必须完全不碰书库写操作——它是纯函数层。"""
        directory = os.path.join(ROOT, 'backend', 'dedup')
        for name in sorted(os.listdir(directory)):
            if not name.endswith('.py'):
                continue
            with open(os.path.join(directory, name), 'r', encoding='utf-8') as handle:
                text = handle.read()
            for call in self.WRITE_CALLS:
                self.assertNotIn('.%s(' % call, text,
                                 '引擎里出现了写操作：%s -> %s' % (name, call))


if __name__ == '__main__':
    unittest.main(verbosity=2)