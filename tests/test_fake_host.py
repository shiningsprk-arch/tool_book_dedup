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


class FakeAsyncService(object):
    """`AsyncService` 的替身，**关键是复刻"注入 db"这一步**。

    真实现里 `self.db` 不是 BaseTool 自带的：`AsyncService.setup()` 才给它赋值，而 setup 只在
    `register_function` / `register_service` 的包装里被调用。假宿主如果省掉这一步，
    「工具装上后一直显示共 0 本书」这类 bug 就**测不出来**——本工具就这么漏过一次
    （`ScopeHandler` 直接 `BookDedupTool().api.calibre.all_book_ids()` → `db=None`）。
    所以这里的 wrapper 必须真的 setup，而不是只把函数包一层。
    """

    _singleton = None

    def __init__(self, calibre_db=None):
        self.db = calibre_db

    def setup(self, calibre_db=None, scoped_session=None, need_check_db=False):
        self.db = calibre_db
        self.session = (scoped_session or (lambda: None))()

    def scoped_session(self):
        return lambda: None

    # 宿主 db：`main.py:398` 用真库调 `AsyncService().setup(book_db, ...)`，
    # 之后所有 `register_*` 包装都从这里取 db 注入。假宿主照做，否则注入的是 None。
    library = None

    @classmethod
    def instance(cls):
        if cls._singleton is None:
            cls._singleton = cls(cls.library)
        return cls._singleton

    @staticmethod
    def register_function(service_func):
        def wrapper(ins, *args, **kwargs):
            ins.setup(FakeAsyncService.instance().db)
            return service_func(ins, *args, **kwargs)
        wrapper.__name__ = getattr(service_func, '__name__', 'wrapped')
        return wrapper


REAL_CORE_API = {'cls': None}


def _load_real_core_api():
    """从 MyBooks clone 加载**真的** `webserver/toolbox/core_api.py`（找不到就留空）。

    为什么必须用真的：`api.calibre` 是**调用时**去读 `self._owner.db`（core_api.py 里
    `_NamespaceBase` 的注释写明了不能提前缓存）。「没经过 register_* 就没有 db」这条性质
    正是靠这个懒取才成立；用一个把整层替掉的 FakeApi 会让这类 bug 隐形——本工具就漏过一次
    （装到真宿主后"一直显示共 0 本书"）。
    """
    if REAL_CORE_API['cls'] is not None:
        return
    clone = os.path.abspath(os.path.join(ROOT, '..', 'mybooks源码', 'mybooks-v4.2.1'))
    path = os.path.join(clone, 'webserver', 'toolbox', 'core_api.py')
    if not os.path.exists(path):
        return
    spec = importlib.util.spec_from_file_location('real_core_api_under_test', path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as err:  # noqa: BLE001
        # 不静默：加载失败会让回归断言变成 skip，看起来像"本机没 clone"，
        # 实际上是桩不全 —— 那正好是这次要防的同类问题
        print('[harness] 真 CoreAPI 加载失败：%s: %s' % (type(err).__name__, err))
        return
    REAL_CORE_API['cls'] = getattr(module, 'CoreAPI', None)


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

    module('webserver', loader=types.SimpleNamespace(get_settings=lambda: {}))
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
            real = REAL_CORE_API['cls']
            # 有真 CoreAPI 就用真的（行为等同宿主）；没有（本机无 clone）才退回 FakeApi，
            # 此时相关回归断言会明确 skip，而不是假通过。
            self.api = real(self) if real is not None else library_api
            # 刻意置 None：真 BaseTool 也没有 db，它靠 register_* 里的 setup() 注入。
            # 不能省这一行——否则上一次用例残留的 db 会让"未初始化应当失败"的回归测试假通过。
            self.db = None
            self.session = None

        def setup(self, calibre_db=None, scoped_session=None, need_check_db=False):
            self.db = calibre_db
            self.session = (scoped_session or (lambda: None))()


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

        # --- 宿主 BaseTool 里被 CoreAPI 转发过来的那几个（真 core_api 会调它们）
        def get_all_book_ids(self):
            return self.db.new_api.all_book_ids()

        def get_book_metadata(self, book_id):
            return None

        def merge_book_formats(self, source_book_id, target_book_id):
            return self.db.merge_formats(source_book_id, target_book_id)

        def delete_book_by_id(self, book_id):
            self.db.delete_book(book_id)

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

    # 真 CoreAPI 的模块级依赖（本工具用不到那几个方法，但 import 会求值）
    module('webserver.base')
    module('webserver.base.formatter', SimpleBookFormatter=object)
    module('webserver.base.global_state', get_global_state=lambda: None)
    _load_real_core_api()
    module('webserver.services', AsyncService=FakeAsyncService)
    module('webserver.services.async_service', AsyncService=FakeAsyncService)
    module('webserver.services.background_service',
           BackgroundService=FakeBackgroundService, BackgroundTask=FakeTask)
    module('webserver.toolbox')
    FakeAsyncService.library = library_api
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
    """CoreAPI 的替身，同时站在**两个位置**上：

    - 测试代码用它：`api.calibre.books` / `api.calibre.calls`（现有用法）
    - 真 `CoreAPI` 用它当**宿主 db**：`self._owner.db.new_api.all_book_ids()`

    所以 `new_api` 指回自己 —— `FakeCalibre` 上已经实现了那些"库方法"。
    不这样做的话，`CalibreAPI.all_book_ids()`（转发 `owner.get_all_book_ids()`，
    后者读 `self.db.new_api`）就会 AttributeError，看起来像工具坏了。

    **库方法要在这里转一手**：真宿主里 `_owner.db` 是 calibre 的 LibraryDatabase，
    工具走 `api.calibre.*` 时真正被调用的是它（`core_api.py:172/330`）。
    这一类转发曾经是**漏的**——`get_data_as_dict` 只有 `FakeCalibre` 上有，
    于是"经 CoreAPI 读一本书"在替身里必然 AttributeError，而真宿主上是好的；
    0.1.3 那个"保留项已被删掉时把源记录删光"的 bug 就藏在这个缝里
    （工具把异常当成"无需合并"）。补上转发，替身才站在宿主的位置上。
    """

    def __init__(self, books):
        self.calibre = FakeCalibre(books)

    @property
    def new_api(self):
        return self.calibre

    def get_data_as_dict(self, ids):
        return self.calibre.get_data_as_dict(ids)

    def format_abspath(self, book_id, fmt, index_is_id=True):
        return self.calibre.format_abspath(book_id, fmt)


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
    write_ops = importlib.import_module(PKG_NAME + '.write_ops')
    if shared_root:
        tool.BookDedupTool.shared_work_dir = classmethod(
            lambda cls, _root=shared_root: _root)
    return tool, driver, write_ops


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
        self.tool, self.driver, self.write_ops = load_tool(self.tmp, self.api, self.shared)
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

    def test_calibre_access_requires_setup(self):
        """**回归**：直接 `BookDedupTool()` 再用 `api.calibre` 会拿 `db=None` 崩掉。

        工具装到真宿主后"一直显示共 0 本书、开始按钮点不动"就是这个——`self.db` 由
        `AsyncService.setup()` 注入，而 setup 只在 `register_*` 的包装里被调用。
        这条测试在假宿主**真的会注入 db**的前提下才有意义（假宿主若省掉 setup，
        它会跟着一起假通过，正是当初漏掉这个 bug 的原因）。
        """
        if REAL_CORE_API['cls'] is None:
            self.skipTest('本机没有 MyBooks clone，无法用真 CoreAPI 验证这条')
        raw = self.tool.BookDedupTool()
        # 没经过 register_function 就没有 db；真 CoreAPI 会在这里炸
        with self.assertRaises(Exception):
            raw.api.calibre.all_book_ids()
        # 走带装饰器的方法才行
        self.assertEqual(len(raw.all_book_ids()), len(self.api.calibre.all_book_ids()))

    def test_resolve_book_ids_expands_empty_to_whole_library(self):
        """空列表 = 全库（几万个 id 不该传到浏览器再传回来）。"""
        tool = self.tool.BookDedupTool()
        self.assertEqual(sorted(tool.resolve_book_ids([])),
                         sorted(self.api.calibre.all_book_ids()))
        self.assertEqual(tool.resolve_book_ids([3, 4]), [3, 4])

    def test_scope_and_start_handlers_get_real_ids(self):
        """扫一遍扫描参数装配：/start 传空列表时要能拿到全库 id，而不是空。"""
        tool = self.tool.BookDedupTool()
        ids = tool.resolve_book_ids([])
        self.assertTrue(ids, '全库展开拿不到任何 id')
        written = None

        class _Recorder:
            def __call__(self, *args, **kwargs):
                nonlocal written
                written = args

            def start(self):
                pass

        # 用真实 driver 跑一次，确认全库 id 能一路走到报告
        built = self.driver.run_scan(self.api, ids, threshold=0.85)
        self.assertEqual(built['scanned_books'], len(ids))

    def test_merge_path_uses_db_injected_api(self):
        """合并走的是写路径，同样必须先有 db——用 api_proxy() 而非裸 api。"""
        built, work_dir = scan(self.tool, self.driver, self.api)
        report = self.driver.read_report(work_dir)
        position = 0
        keeper = report['groups'][position]['members'][0]['id']
        proxy = self.tool.BookDedupTool().api_proxy()
        plan, error = self.write_ops.build_plan(
            proxy, work_dir, position, keeper_id=keeper)
        self.assertIsNone(error, error)
        self.assertTrue(plan['steps'])

    def test_index_carries_member_preview(self):
        """**回归**：列表行必须能直接读到成员书名，否则每行都只写"x 本可能是同一本书"。

        真机反馈：135 组每行都长一样，必须逐行点开才知道是哪些书。所以索引里要带
        前 `driver.PREVIEW_TITLES` 个成员的书名与作者。
        """
        _built, work_dir = scan(self.tool, self.driver, self.api)
        index = self.driver.read_index(work_dir)
        report = self.driver.read_report(work_dir)
        self.assertTrue(index['groups'])
        for entry in index['groups']:
            members = report['groups'][entry['index']]['members']
            self.assertEqual(entry['titles'],
                             [m['title'] for m in members[:2]])
            self.assertEqual(entry['authors'],
                             [(m.get('authors') or [''])[0] for m in members[:2]])
            # 顺序必须与 members 一致，否则界面会把"保留项"标到别的书上
            self.assertEqual(len(entry['titles']),
                             min(2, len(entry['members'])))
            self.assertEqual(entry['preview_truncated'],
                             len(entry['members']) > 2)

    def test_index_preview_caps_at_two(self):
        """超过 2 本的组：列表只给 2 个书名（其余点开抽屉看全部）。

        5 条记录的书名必须**完全一样**：写成「同一本书0..4」的话，它们只差尾部一个数字，
        属于"同系列不同卷"（`normalize.differs_only_by_serial`），本来就不该成组。
        """
        records = []
        for position in range(5):
            record = {
                'id': 100 + position,
                'title': '同一本书',
                'authors': ['同一作者'],
                'formats': ['EPUB'],
                'size': 100,
                'added': '2026-01-01T00:00:00',
                'isbn': '',
            }
            records.append(record)
        # 直接构造一份报告，走 write_index 的实际实现
        from importlib import import_module
        pkg = import_module(self.tool.__name__.rsplit('.', 1)[0])
        cluster = import_module(pkg.__name__ + '.dedup.cluster')
        for record in records:
            cluster.prepare(record)
        pairs, stats = cluster.candidate_pairs(records, threshold=0.5)
        groups = cluster.group_pairs(pairs, {r['id']: r for r in records})
        report_mod = import_module(pkg.__name__ + '.dedup.report')
        for record in records:
            record['_meta_score'] = 50
            record['_missing'] = []
        report = report_mod.build_report(records, pairs, groups, 0.5, stats)
        work_dir = self.tool.BookDedupTool.report_dir(99)
        self.driver.write_index(work_dir, report)
        index = self.driver.read_index(work_dir)
        self.assertEqual(len(index['groups']), 1)
        entry = index['groups'][0]
        self.assertEqual(entry['members'].__len__(), 5)
        self.assertEqual(len(entry['titles']), 2)
        self.assertTrue(entry['preview_truncated'])

    def test_host_cover_and_comments_convert_to_engine_names(self):
        """**回归（review P1）**：宿主给的是 `cover` / `comments`，引擎读的是
        `has_cover` / `comments_present`。

        两边曾经各写各的键名，后果是"封面"这一项永远被判缺失：一本字段齐全的书只有
        83 分（应为 100），而且每本书的 missing_fields 里都挂着"封面"。
        这条测试刻意**串起"宿主形状 → 评分结果"**——老测试之所以漏掉它，正是因为
        引擎测试只用 `has_cover`、假宿主只用 `cover`，各自都测不出这个名字错位。

        分数是钉死的：book 1 十二项全带 → 100；book 2 只有书名/作者/格式 → 8/21 = 38。
        """
        _built, work_dir = scan(self.tool, self.driver, self.api)
        report = self.driver.read_report(work_dir)
        members = {}
        for group in report['groups']:
            for member in group['members']:
                members[member['id']] = member

        rich = members[1]          # make_books(): cover=True, comments='简介', 其余字段齐全
        self.assertTrue(rich['has_cover'])
        self.assertTrue(rich['comments_present'])
        self.assertNotIn('has_cover', rich['missing_fields'])
        self.assertNotIn('comments', rich['missing_fields'])
        self.assertEqual(rich['meta_score'], 100)
        # 简介正文不进报告（报告是 MB 级的），只带"有没有"这个布尔
        self.assertNotIn('comments', rich)

        poor = members[2]          # cover=False, comments=''
        self.assertFalse(poor['has_cover'])
        self.assertFalse(poor['comments_present'])
        self.assertIn('has_cover', poor['missing_fields'])
        self.assertIn('comments', poor['missing_fields'])
        self.assertEqual(poor['meta_score'], 38)
        self.assertGreater(rich['meta_score'], poor['meta_score'])

    def test_report_read_is_cached(self):
        """报告是 MB 级：`/group` 每点一行都会读一次，必须有解析缓存。"""
        import time as _time
        _built, work_dir = scan(self.tool, self.driver, self.api)
        self.driver.read_report(work_dir)          # 预热
        started = _time.perf_counter()
        for _ in range(50):
            self.driver.read_report(work_dir, group_index=0)
        elapsed = _time.perf_counter() - started
        self.assertLess(elapsed, 0.25, '50 次读单组耗时 %.3fs，缓存没生效' % elapsed)

    def test_group_texts_builds_once(self):
        """关键字筛选用一次建好的文本表（原来每筛一组读一遍报告）。"""
        _built, work_dir = scan(self.tool, self.driver, self.api)
        texts = self.tool._group_texts(work_dir)
        report = self.driver.read_report(work_dir)
        self.assertEqual(len(texts), len(report['groups']))
        first = report['groups'][0]['members'][0]
        self.assertIn(str(first['title']).lower(), texts[0])

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
        self.tool, self.driver, self.write_ops = load_tool(self.tmp, self.api, self.shared)
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
        plan, error = self.write_ops.build_plan(
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
        _plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=5)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'keeper.not_in_group')

    def test_plan_rejects_keeper_that_left_the_library(self):
        """**回归（review P0）**：保留项已不在书库时，预览就该拒绝。

        报告是跨重启持久化的，可能几天前扫的；而工具卡片上就有「打开书籍页」，
        用户完全可能在期间把那本书删掉/并掉。
        """
        position, _group = self._group_index_of('三体')
        del self.api.calibre.books[1]          # 用户在别处删掉了保留项
        _plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'keeper.missing')

    def test_merge_aborts_when_keeper_left_the_library(self):
        """**回归（review P0）**：保留项不在了 → 一条记录都不许删。

        0.1.3 把 `merge_formats` 抛的"目标书籍不存在"当成"无需合并"，然后照样删源记录：
        结果是源记录被删光、什么都没复制。假宿主此前也没复刻这条抛出路径
        （`_owner.db.get_data_as_dict` 的转发是漏的），所以这个 bug 在测试里不可能发生。
        """
        position, group = self._group_index_of('三体')
        source = [m['id'] for m in group['members'] if m['id'] != 1][0]
        del self.api.calibre.books[1]

        # 绕过预览直接执行（模拟"预览是几分钟前生成的、期间保留项没了"）
        stale_plan = {
            'keeper_id': 1, 'keeper_title': '三体', 'index': position,
            'steps': [{'source_id': source, 'source_title': '三体（全集）', 'target_id': 1,
                       'moved_formats': ['PDF'], 'dropped_formats': ['EPUB']}],
            'moved_total': 1, 'dropped_total': 1,
            'reclaimable_bytes': 0, 'disk_waste_bytes': 0,
        }
        result = self.write_ops.execute(self.api, self.work_dir, stale_plan)
        self.assertNotEqual(result['err'], 'ok')
        self.assertNotIn(('delete_book', source), self.api.calibre.calls)
        self.assertIn(source, self.api.calibre.books)      # 源记录还在

    def test_library_read_failure_is_not_reported_as_missing_book(self):
        """**回归（re-review）**：宿主读不到 ≠ 书不存在。

        `book_exists` 以前把异常吞成 False，于是数据库/宿主的临时问题会被说成
        "保留项已不在书库"——用户会得到完全错误的结论。
        """

        def boom(ids):
            raise RuntimeError('数据库连接断了')

        self.api.calibre.get_data_as_dict = boom
        position, _group = self._group_index_of('三体')
        _plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'scope.failed')
        self.assertIn('读取书库失败', error['msg'])

    def test_unexpected_error_in_one_step_is_recorded_not_raised(self):
        """**回归（re-review）**：单步取数异常不许冒到 handler（会变 500），

        要记成这一步失败：不删任何东西，且能在「本次已处理」里看到。
        """
        position, group = self._group_index_of('三体')
        source = [m['id'] for m in group['members'] if m['id'] != 1][0]
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        original = self.api.calibre.get_data_as_dict
        calls = {'n': 0}

        def flaky(ids):
            # 只让"读源书"这一步失败，保留项的核对仍走正常路径
            if source in list(ids):
                raise RuntimeError('读这本书失败')
            return original(ids)

        self.api.calibre.get_data_as_dict = flaky
        result = self.write_ops.execute(self.api, self.work_dir, plan)
        self.assertEqual(result['err'], 'merge.failed')          # 全部失败 → 报错
        self.assertEqual(result['data']['failed'], 1)
        self.assertNotIn(('delete_book', source), self.api.calibre.calls)
        self.assertIn(source, self.api.calibre.books)            # 源书还在
        failed = self.write_ops.read_failed_titles(self.work_dir)
        self.assertEqual([f['id'] for f in failed], [source])

    def test_copy_failure_does_not_delete_source(self):
        """复制真的抛错时不许删源记录——只有"确实没有可复制的格式"才允许删。"""
        position, group = self._group_index_of('三体')
        source = [m['id'] for m in group['members'] if m['id'] != 1][0]
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.assertTrue(plan['steps'][0]['moved_formats'])   # 确实有该复制的格式

        def boom(source_id, target_id):
            raise RuntimeError('磁盘写入失败')

        self.api.calibre.merge_formats = boom
        result = self.write_ops.execute(self.api, self.work_dir, plan)
        self.assertNotEqual(result['err'], 'ok')
        self.assertNotIn(('delete_book', source), self.api.calibre.calls)
        self.assertIn(source, self.api.calibre.books)

    def test_identical_formats_still_merge(self):
        """两本格式完全一样（无可复制项）仍要能合并删除——这条老路不能被安全修复堵死。"""
        record = {'id': 6, 'title': '三体', 'authors': ['刘慈欣'],
                  'available_formats': ['EPUB', 'MOBI'], 'isbn': '',
                  'timestamp': '2026-06-01T00:00:00+00:00', '_paths': {}}
        self.api.calibre.books[6] = record
        _built, work_dir = scan(self.tool, self.driver, self.api)
        report = self.driver.read_report(work_dir)
        position = self._index_of_members(report, {1, 2, 6})
        plan, error = self.write_ops.build_plan(self.api, work_dir, position, keeper_id=1)
        self.assertIsNone(error)
        by_source = {step['source_id']: step for step in plan['steps']}
        self.assertEqual(by_source[6]['moved_formats'], [])   # 与 keeper 的格式完全一致
        self.assertEqual(by_source[2]['moved_formats'], ['PDF'])

        result = self.write_ops.execute(self.api, work_dir, plan)
        self.assertEqual(result['err'], 'ok')
        self.assertNotIn(6, self.api.calibre.books)           # 同格式那本照样被删
        self.assertIn(1, self.api.calibre.books)

    def test_partial_failure_is_recorded(self):
        """一组里有失败时：成功的照记，失败的要落进台账（界面据此如实显示）。"""
        self.api.calibre.books[6] = {
            'id': 6, 'title': '三体（全本）', 'authors': ['刘慈欣'],
            'available_formats': ['EPUB'], 'isbn': '',
            'timestamp': '2026-06-01T00:00:00+00:00', '_paths': {}}
        _built, work_dir = scan(self.tool, self.driver, self.api)
        report = self.driver.read_report(work_dir)
        position = self._index_of_members(report, {1, 2, 6})
        plan, error = self.write_ops.build_plan(self.api, work_dir, position, keeper_id=1)
        self.assertIsNone(error)
        self.assertEqual(len(plan['steps']), 2)

        del self.api.calibre.books[6]        # 其中一本在别处被删了
        result = self.write_ops.execute(self.api, work_dir, plan)
        self.assertEqual(result['err'], 'ok')                 # 部分成功仍是 ok
        self.assertEqual(result['data']['failed'], 1)         # 但失败数必须如实回
        self.assertEqual(result['data']['removed_ids'], [2])
        failed = self.write_ops.read_failed_titles(work_dir)
        self.assertEqual([f['id'] for f in failed], [6])
        self.assertEqual(failed[0]['error'], 'merge.source_missing')

    @staticmethod
    def _index_of_members(report, wanted):
        for index, group in enumerate(report['groups']):
            if {m['id'] for m in group['members']} == set(wanted):
                return index
        raise AssertionError('找不到成员为 %s 的分组：%s' % (
            wanted, [sorted(m['id'] for m in g['members']) for g in report['groups']]))

    def test_plan_rejects_bad_index(self):
        _plan, error = self.write_ops.build_plan(self.api, self.work_dir, 999)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'group.not_found')

    def test_execute_merges_formats_then_deletes_source(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        result = self.write_ops.execute(self.api, self.work_dir, plan)
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
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.write_ops.execute(self.api, self.work_dir, plan)

        merged_ids = self.write_ops.read_merged_ids(self.work_dir)
        self.assertEqual(merged_ids, [2])
        titles = self.write_ops.read_removed_titles(self.work_dir)
        self.assertEqual(titles[0]['title'], '三体（全集）')
        self.assertEqual(titles[0]['into'], '三体')

        index = self.driver.read_index(self.work_dir)
        remaining = [g for g in index['groups']
                     if not (set(g.get('members') or []) & set(merged_ids))]
        self.assertNotIn(position, [g['index'] for g in remaining])

    def test_double_merge_is_refused(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.write_ops.execute(self.api, self.work_dir, plan)
        _plan2, error = self.write_ops.build_plan(self.api, self.work_dir, position)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'group.already_merged')

    def test_execute_without_delete_keeps_source(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        result = self.write_ops.execute(self.api, self.work_dir, plan, delete_source=False)
        self.assertEqual(result['err'], 'ok')
        self.assertEqual(result['data']['removed_ids'], [])
        self.assertIn(2, self.api.calibre.books)          # 源记录还在
        self.assertNotIn(('delete_book', 2), self.api.calibre.calls)

    def test_plan_survives_no_new_formats(self):
        """两组格式完全相同时 merge_formats 会抛错，合并要照常继续（只是没复制什么）。"""
        position, _group = self._group_index_of('To Live')
        plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=3)
        self.assertIsNone(error)
        result = self.write_ops.execute(self.api, self.work_dir, plan)
        self.assertEqual(result['err'], 'ok')
        self.assertEqual(result['data']['moved_total'], 0)
        # 源书仍然被删（用户要的就是"消掉这一条"），并记账为"没有新格式"
        self.assertEqual(result['data']['removed_ids'], [4])
        self.assertIn('no_new_formats', result['data']['steps'][0]['notes'])

    def test_merge_plan_warns_about_limitations(self):
        position, _group = self._group_index_of('三体')
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, position, keeper_id=1)
        self.assertIn('working_formats_dropped', plan['warnings'])
        self.assertIn('source_records_not_migrated', plan['warnings'])


class TestDelete(unittest.TestCase):
    """单本书删除：守门、记账、以及"删完之后列表还显示吗"。"""

    def setUp(self):
        FakeBackgroundService._tasks = {}
        self.tmp = tempfile.mkdtemp(prefix='book_dedup_del_')
        self.shared = tempfile.mkdtemp(prefix='book_dedup_del_shared_')
        self.api = FakeApi(make_books())
        self.tool, self.driver, self.write_ops = load_tool(self.tmp, self.api, self.shared)
        self.tool_class = self.tool.BookDedupTool
        self.tool_class._last_task_id = None
        self.tool_class._accepted = False
        self.built, self.work_dir = scan(self.tool, self.driver, self.api)

    def _group_of(self, title):
        report = self.driver.read_report(self.work_dir)
        for position, group in enumerate(report['groups']):
            for member in group['members']:
                if member['title'] == title:
                    return position, group
        raise AssertionError('找不到分组：%s' % title)

    def test_delete_refuses_book_outside_group(self):
        """守门：只收分组序号 + 一个 book_id，且必须**是这一组的成员**。

        不校验的话，一个构造出来的请求就能删掉没被查出来的书。
        """
        position, _group = self._group_of('三体')
        result = self.write_ops.execute_delete(self.api, self.work_dir, position, 5)
        self.assertEqual(result['err'], 'book.not_in_group')
        self.assertIn(5, self.api.calibre.books)          # 那本书没被动

    def test_delete_rejects_bad_params(self):
        position, _group = self._group_of('三体')
        self.assertEqual(
            self.write_ops.execute_delete(self.api, self.work_dir, position, 'x')['err'],
            'params.invalid')
        self.assertEqual(
            self.write_ops.execute_delete(self.api, self.work_dir, 'x', 1)['err'],
            'params.invalid')
        self.assertEqual(
            self.write_ops.execute_delete(self.api, self.work_dir, 999, 1)['err'],
            'group.not_found')

    def test_delete_removes_book_and_records_ledger(self):
        position, _group = self._group_of('三体')
        result = self.write_ops.execute_delete(self.api, self.work_dir, position, 2)
        self.assertEqual(result['err'], 'ok')
        self.assertEqual(result['data']['deleted_id'], 2)
        self.assertNotIn(2, self.api.calibre.books)
        self.assertIn(('delete_book', 2), self.api.calibre.calls)

        self.assertEqual(self.write_ops.read_deleted_ids(self.work_dir), [2])
        titles = self.write_ops.read_deleted_titles(self.work_dir)
        self.assertEqual(titles[0]['title'], '三体（全集）')
        # 合并记账不该被污染
        self.assertEqual(self.write_ops.read_merged_ids(self.work_dir), [])

    def test_deleted_book_becomes_gone_for_other_operations(self):
        """删掉之后的 id 必须进 `gone_ids`，否则拿它去合并会撞"来源书籍不存在"。"""
        position, _group = self._group_of('三体')
        self.write_ops.execute_delete(self.api, self.work_dir, position, 2)
        self.assertIn(2, self.write_ops.gone_ids(self.work_dir))
        # 这一组只剩一本 → 不再提供合并预览
        _plan, error = self.write_ops.build_plan(self.api, self.work_dir, position)
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'group.already_merged')

    def test_group_with_fewer_than_two_is_dropped_from_list(self):
        """列表要按"剩下的够不够两本"摘除，而不是按"有没有被合并过"。"""
        position, _group = self._group_of('三体')
        index = self.driver.read_index(self.work_dir)
        gone = {2}
        remaining = [g for g in index['groups']
                     if len(set(g['members']) - gone) >= 2]
        self.assertNotIn(position, [g['index'] for g in remaining])
        # 「To Live / 活着」那一组（index 0）不受影响
        self.assertIn(0, [g['index'] for g in remaining])
        self.assertEqual(position, 1, '前提：#2 属于 index 1 那一组')

    def test_delete_works_when_group_has_more_than_two(self):
        """三本以上的组：删掉一本之后另外两本仍可合并（不该被"只剩一本"误拦）。"""
        books = {
            11: {'id': 11, 'title': '同一本书', 'authors': ['作者'], 'available_formats': ['EPUB'],
                 'isbn': '', 'timestamp': '2026-01-01T00:00:00+00:00', '_paths': {}},
            12: {'id': 12, 'title': '同一本书（全集）', 'authors': ['作者'], 'available_formats': ['PDF'],
                 'isbn': '', 'timestamp': '2026-02-01T00:00:00+00:00', '_paths': {}},
            13: {'id': 13, 'title': '同一本书（修订版）', 'authors': ['作者'], 'available_formats': ['MOBI'],
                 'isbn': '', 'timestamp': '2026-03-01T00:00:00+00:00', '_paths': {}},
        }
        api = FakeApi(books)
        tool, driver, write_ops = load_tool(self.tmp, api, self.shared)
        built = driver.run_scan(api, api.calibre.all_book_ids(), threshold=0.5)
        work_dir = tool.BookDedupTool.report_dir(88)
        driver.write_report(work_dir, built)
        driver.write_index(work_dir, built)
        self.assertEqual(built['summary']['group_count'], 1)

        result = write_ops.execute_delete(api, work_dir, 0, 12)
        self.assertEqual(result['err'], 'ok')
        plan, error = write_ops.build_plan(api, work_dir, 0, keeper_id=11)
        self.assertIsNone(error, error)
        self.assertEqual(sorted(plan['steps'][0]['source_id'] for _ in [0]), [13])

    def test_delete_does_not_touch_other_books(self):
        """删一本不影响同组其它书，也不影响别的组。"""
        position, _group = self._group_of('三体')
        before = sorted(self.api.calibre.books)
        self.write_ops.execute_delete(self.api, self.work_dir, position, 2)
        after = sorted(self.api.calibre.books)
        self.assertEqual(set(before) - set(after), {2})


class TestPartialMergeSelection(unittest.TestCase):
    """0.1.5：只合并用户勾选的那几本，没勾的原样保留。

    一组三本（1 / 2 / 6），保留项 1。勾选的才进 `steps`，没勾的进 `kept` 并且
    **不许被动**（记录还在、还在组里）。
    """

    def setUp(self):
        FakeBackgroundService._tasks = {}
        self.tmp = tempfile.mkdtemp(prefix='book_dedup_partial_')
        self.shared = tempfile.mkdtemp(prefix='book_dedup_partial_shared_')
        self.api = FakeApi(make_books())
        self.api.calibre.books[6] = {
            'id': 6, 'title': '三体（全本）', 'authors': ['刘慈欣'],
            'available_formats': ['AZW3'], 'isbn': '',
            'timestamp': '2026-06-01T00:00:00+00:00', '_paths': {}}
        # 造真的格式文件：`load_records` 的体积是按磁盘文件算的。假路径会让
        # "可回收空间"恒等于 0，而"数字按勾选口径"这条断言正是要盯它。
        blobs = os.path.join(self.tmp, 'blobs')
        os.makedirs(blobs, exist_ok=True)

        def blob(book_id, fmt, size):
            path = os.path.join(blobs, '%s.%s' % (book_id, fmt.lower()))
            with open(path, 'wb') as handle:
                handle.write(b'x' * size)
            return path

        self.api.calibre.books[1]['_paths'] = {
            'EPUB': blob(1, 'EPUB', 100), 'MOBI': blob(1, 'MOBI', 200)}
        self.api.calibre.books[2]['_paths'] = {
            'EPUB': blob(2, 'EPUB', 100), 'PDF': blob(2, 'PDF', 400)}
        self.api.calibre.books[6]['_paths'] = {'AZW3': blob(6, 'AZW3', 800)}
        self.tool, self.driver, self.write_ops = load_tool(self.tmp, self.api, self.shared)
        self.tool_class = self.tool.BookDedupTool
        self.tool_class._last_task_id = None
        self.tool_class._accepted = False
        self.built, self.work_dir = scan(self.tool, self.driver, self.api)
        report = self.driver.read_report(self.work_dir)
        self.position = None
        for index, group in enumerate(report['groups']):
            if {m['id'] for m in group['members']} == {1, 2, 6}:
                self.position = index
                break
        self.assertIsNotNone(self.position, '三本的那一组没成组')

    def test_plan_only_covers_selected_members(self):
        """勾了哪几本，预览里就只有哪几步；没勾的进 `kept` 一起回。"""
        plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[2])
        self.assertIsNone(error, error)
        self.assertEqual([step['source_id'] for step in plan['steps']], [2])
        self.assertEqual(plan['source_ids'], [2])
        self.assertEqual([item['id'] for item in plan['kept']], [6])
        self.assertEqual(plan['kept'][0]['title'], '三体（全本）')

    def test_subset_merge_leaves_unchecked_book_alone(self):
        """没勾的那本：记录还在、还能在组里继续处理。"""
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[2])
        result = self.write_ops.execute(self.api, self.work_dir, plan)
        self.assertEqual(result['err'], 'ok')
        self.assertEqual(result['data']['removed_ids'], [2])
        self.assertEqual([item['id'] for item in result['data']['kept']], [6])
        self.assertNotIn(2, self.api.calibre.books)
        self.assertIn(6, self.api.calibre.books)                  # 没勾的没被删
        self.assertNotIn(('delete_book', 6), self.api.calibre.calls)
        # 勾选的那本格式照旧并进保留项，没勾的那本独有的 AZW3 不该被带过来
        self.assertIn('PDF', self.api.calibre.books[1]['available_formats'])
        self.assertNotIn('AZW3', self.api.calibre.books[1]['available_formats'])

        # 记账里也要留下"哪几本没动"，否则事后看台账会以为这一组处理干净了
        ledger = self.write_ops.read_merged(self.work_dir)
        self.assertEqual([item['id'] for item in ledger[-1]['kept']], [6])

        # 这一组还剩 1 + 6 → 仍可继续处理
        plan2, error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[6])
        self.assertIsNone(error, error)
        self.assertEqual([step['source_id'] for step in plan2['steps']], [6])

    def test_plan_numbers_are_scoped_to_selection(self):
        """数字按勾选口径：沿用全组会虚报（用户是照这个数做决定的）。"""
        whole, _error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1)
        subset, _error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[2])
        # 全组：#2 的 PDF + #6 的 AZW3 都要复制；只勾 #2 时只剩 PDF
        self.assertEqual(whole['moved_total'], 2)
        self.assertEqual(subset['moved_total'], 1)
        self.assertLess(subset['reclaimable_bytes'], whole['reclaimable_bytes'])
        self.assertEqual(subset['kept'][0]['id'], 6)

    def test_foreign_id_is_refused_and_nothing_is_written(self):
        """守门：勾选集必须是这一组的成员——**勾选不是"传 id 就能删书"的口子**。"""
        _plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[5])
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'source.not_in_group')
        self.assertIn('5', error['msg'])
        self.assertEqual(self.api.calibre.calls, [])
        self.assertEqual(self.write_ops.read_merged(self.work_dir), [])

    def test_keeper_in_selection_is_refused(self):
        _plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[1, 2])
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'source.is_keeper')
        self.assertEqual(self.api.calibre.calls, [])

    def test_empty_selection_means_nothing_not_everything(self):
        """`[]` 与"没传这个字段"必须分开：前者一本都不并，后者才是全组。

        把空数组当成"缺省"，前端一次状态丢失就会静悄悄把整组并掉。
        """
        _plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[])
        self.assertIsNotNone(error)
        self.assertEqual(error['err'], 'merge.nothing')
        self.assertEqual(self.api.calibre.calls, [])

        # 缺省仍然是全组（0.1.4 的行为不退化）
        plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1)
        self.assertIsNone(error)
        self.assertEqual(sorted(step['source_id'] for step in plan['steps']), [2, 6])
        self.assertEqual(plan['kept'], [])

    def test_bad_selection_is_rejected(self):
        for bad in ('abc', [1, 'x'], {'a': 1}, True, [True]):
            _plan, error = self.write_ops.build_plan(
                self.api, self.work_dir, self.position, keeper_id=1, source_ids=bad)
            self.assertIsNotNone(error, bad)
            self.assertEqual(error['err'], 'params.invalid', bad)
        self.assertEqual(self.api.calibre.calls, [])

    def test_csv_selection_is_parsed(self):
        """`/merge_plan` 的 GET 参数是逗号串（与 `/merge` 的数组共用同一个解析）。"""
        plan, error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids='2, 6')
        self.assertIsNone(error, error)
        self.assertEqual(sorted(step['source_id'] for step in plan['steps']), [2, 6])

    def test_partial_merge_keeps_group_but_drops_deleted_titles(self):
        """只合并一部分之后：这一组还在列表里，但行里**不许**再出现已删的书名。"""
        plan, _error = self.write_ops.build_plan(
            self.api, self.work_dir, self.position, keeper_id=1, source_ids=[2])
        self.write_ops.execute(self.api, self.work_dir, plan)

        index = self.driver.read_index(self.work_dir)
        rows = self.tool._visible_groups(
            index['groups'], self.write_ops.gone_ids(self.work_dir))
        row = [item for item in rows if item['index'] == self.position][0]
        self.assertEqual(row['titles'], ['三体', '三体（全本）'])
        self.assertEqual(row['member_count'], 2)
        self.assertEqual(row['stale_count'], 1)
        self.assertFalse(row['preview_truncated'])
        for title in row['titles']:
            self.assertNotIn('全集', title)                       # 已被删掉的那本

    def test_visible_groups_falls_back_on_old_index(self):
        """0.1.4 写的索引没有全量标题：只标记 stale_count，不许炸。"""
        index = self.driver.read_index(self.work_dir)
        legacy = []
        for group in index['groups']:
            row = dict(group)
            row.pop('member_titles', None)
            row.pop('member_authors', None)
            legacy.append(row)
        rows = self.tool._visible_groups(legacy, {2})
        row = [item for item in rows if item['index'] == self.position][0]
        self.assertEqual(row['stale_count'], 1)
        self.assertTrue(row['titles'])                            # 旧字段原样保留

    def test_row_bytes_are_scoped_to_live_members(self):
        """行里的"可回收/同格式重占"必须按活着的成员重算，不把已删的书算进去。"""
        index = self.driver.read_index(self.work_dir)
        before = [item for item in index['groups']
                  if item['index'] == self.position][0]
        self.assertGreater(before['reclaimable_bytes'], 0, '前提：这一组有体积')

        self.write_ops.execute_delete(self.api, self.work_dir, self.position, 6)
        gone = self.write_ops.gone_ids(self.work_dir)
        rows = self.tool._visible_groups(index['groups'], gone)
        row = [item for item in rows if item['index'] == self.position][0]
        self.assertEqual(row['stale_count'], 1)
        # #6 的体积（800 字节的 AZW3）不该再算进去
        self.assertEqual(row['reclaimable_bytes'],
                         before['reclaimable_bytes'] - 800)

    def test_group_diff_is_rebuilt_for_live_members(self):
        """**回归（0.1.5 浏览器实测）**：滤掉已消失的成员之后，对照表要按活着的成员重建。

        表格是扫描时按当时那批成员算的；只裁成员不重算表格，表头 2 列而每行 3 格，
        用户看到的是"三个数字排在两个书名下面"（错位的信息，比缺一格更糟）。
        """
        self.write_ops.execute_delete(self.api, self.work_dir, self.position, 6)
        group = self.driver.read_report(self.work_dir, group_index=self.position)
        self.assertEqual(len(group['members']), 3)
        self.assertEqual(len(group['diff']['rows'][0]['cells']), 3)   # 扫描时的旧表格

        # 走 /group 的那条路（成员滤过之后重建）
        from importlib import import_module
        pkg = import_module(self.tool.__name__.rsplit('.', 1)[0])
        diff_mod = import_module(pkg.__name__ + '.dedup.diff')
        gone = self.write_ops.gone_ids(self.work_dir)
        members = [m for m in group['members'] if m['id'] not in gone]
        rebuilt = diff_mod.build_table(
            self.write_ops.to_diff_members(members))
        self.assertEqual(len(members), 2)
        for row in rebuilt['rows']:
            self.assertEqual([cell['book_id'] for cell in row['cells']],
                             [m['id'] for m in members])
        # `meta_score → _meta_score` 这一手必须在：否则"元数据"整行会变 0 而被折叠掉
        self.assertIn('metadata', [row['field'] for row in rebuilt['rows']])


class TestWritePathGuard(unittest.TestCase):
    """守门：整个工具只有 merge.py 允许调用删/改书库的方法。"""

    WRITE_CALLS = ('delete_book', 'merge_formats', 'remove_formats', 'set_metadata',
                   'set_cover', 'import_book', 'import_file', 'add_format')

    def test_write_operations_are_serialized(self):
        """**回归（review P3）**：写操作与记账都必须持同一把锁。

        记账是"读→改→写"，并发两次（双击确认、两个标签页）会丢一条记录——丢了记账，
        那本已经删掉的书还会留在列表里，再点合并就撞宿主报错。同一组并发合并更糟：
        两边都先通过"还剩 ≥2 本"的检查，然后各自去删源记录。
        """
        path = os.path.join(ROOT, 'backend', 'write_ops.py')
        with open(path, 'r', encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('_WRITE_LOCK', source)
        for name in ('def execute(', 'def execute_delete(',
                     'def append_merged(', 'def append_deleted('):
            index = source.index(name)
            following = source.find('\ndef ', index + 1)
            body = source[index:following if following > 0 else len(source)]
            self.assertIn('with _WRITE_LOCK', body, '%s 没有持锁' % name)

    def _py_files(self):
        for folder in ('backend', 'backend/dedup'):
            directory = os.path.join(ROOT, folder)
            for name in sorted(os.listdir(directory)):
                if name.endswith('.py'):
                    yield os.path.join(directory, name)

    def test_only_write_ops_and_driver_touch_write_calls(self):
        """写书库的动作只许出现在 `write_ops.py`（两处写操作）与 `driver.py`（取数/落盘）。

        这条边界是"引擎层不许写"的另一半。改名 `merge.py`→`write_ops.py` 时它立刻报红
        ——放行名单必须跟着实际文件名走，不能留着一个再也不匹配的旧名字。
        """
        allowed = {'write_ops.py', 'driver.py'}
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