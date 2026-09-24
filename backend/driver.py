# -*- coding: utf-8 -*-
"""取数与编排：把 CoreAPI 的数据搬进引擎，再把结果落成报告 / 执行合并。

引擎（`dedup/`）里没有一行宿主代码，所有"跟宿主打交道"的事都在这里：

- `load_records()`  —— 分批读全库（`get_data_as_dict` + `format_abspath` 算体积）
- `run_scan()`      —— 跑引擎、组装报告
- `merge_group()`   —— **唯一的写操作**：把重复项并进保留项，可选删掉源记录

字段口径全部按宿主实测（键名踩过的坑写在 `load_records` 的注释里）。
"""
import json
import logging
import os
import threading
import time

from .dedup import cluster, keeper, metadata, report

# `get_data_as_dict` 一次读多少本。整库一次读在 25k 本时是 MB 级内存 + 长阻塞，
# 分批读才能让进度条动起来、也才能响应取消。
BATCH_SIZE = 500

# 扫描上限：查重是逐本读元数据 + 两两比较，误点全库要能收住。
MAX_BOOKS = 60000

# 相似度阈值：本期按 85% 上线（BookOrbit 同值），原值会写进报告便于按真实误报标定。
DEFAULT_THRESHOLD = 0.85
MIN_THRESHOLD = 0.50
MAX_THRESHOLD = 1.00


def normalize_threshold(value):
    """阈值兜底到合法区间；非法输入回落默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    if number > 1:  # 允许前端传百分数
        number = number / 100.0
    return max(MIN_THRESHOLD, min(MAX_THRESHOLD, number))


def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(v) for v in value if v]
    except TypeError:
        return [str(value)]


def _added_at(book):
    """入库时间取 `timestamp`（加入书库的时间），不是 `pubdate`（出版日期）。"""
    value = book.get('timestamp')
    if value is None:
        return ''
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def load_records(api, book_ids, batch_size=BATCH_SIZE, on_progress=None, cancel=None):
    """把书库记录读成引擎要的形状。

    **键名口径（宿主实测，别改）**：

    - 格式列表键是 `available_formats`
    - 标题排序键是 `sort`，**不是 `title_sort`**
    - 未评分是数值 `0`，不是缺键
    - 封面看 `cover` 布尔键；取不到时回落 `CoreAPI.calibre.cover()`（慢，只在必要时）

    :return: (records, notes)；notes 记录跳过的书与原因，用于在报告里如实交代。
    """
    records = []
    notes = {'missing': 0, 'no_format_path': 0}
    ids = list(book_ids)
    total = len(ids)

    for start in range(0, total, batch_size):
        if cancel is not None and cancel.is_set():
            break
        chunk = ids[start:start + batch_size]
        books = api.calibre.get_data_as_dict(chunk) or []
        for book in books:
            book_id = book.get('id')
            if book_id is None:
                continue
            formats = [str(f).upper() for f in (book.get('available_formats') or [])]

            size = 0
            for fmt in formats:
                path = api.calibre.format_abspath(book_id, fmt)
                if not path:
                    continue
                try:
                    size += os.path.getsize(path)
                except OSError:
                    notes['no_format_path'] += 1

            record = {
                'id': book_id,
                'title': book.get('title') or '',
                'authors': _as_list(book.get('authors') or book.get('author')),
                'formats': formats,
                'size': size,
                'added': _added_at(book),
                'isbn': book.get('isbn') or '',
                'comments': book.get('comments') or '',
                'tags': _as_list(book.get('tags') or book.get('tag')),
                'series': book.get('series') or '',
                'series_index': book.get('series_index'),
                'publisher': book.get('publisher') or '',
                'pubdate': str(book.get('pubdate') or ''),
                'languages': _as_list(book.get('languages')),
                'translators': _as_list(book.get('translators')),
                'rating': book.get('rating') or 0,
                'cover': bool(book.get('cover')),
                'collector_id': book.get('collector_id'),
                'collector_name': book.get('collector') or '',
                'sole': bool(book.get('sole')),
                'book_type': book.get('book_type'),
            }
            cluster.prepare(record)
            record['_meta_score'] = metadata.score(record)
            _missing, _detail = metadata.missing_fields(record)
            record['_missing'] = _missing
            records.append(record)
        if on_progress is not None:
            on_progress(min(start + batch_size, total), total)

    notes['missing'] = max(0, total - len(records))
    return records, notes


def run_scan(api, book_ids=None, threshold=DEFAULT_THRESHOLD, scope_note='',
             on_progress=None, cancel=None):
    """跑一次完整查重：读数据 → 配对 → 分组 → 出报告。

    :param on_progress: ``callable(done, total, phase)``，phase 取 'load' / 'compare'
    """
    threshold = normalize_threshold(threshold)

    if book_ids is None:
        book_ids = api.calibre.all_book_ids()
    book_ids = list(book_ids)[:MAX_BOOKS]

    if on_progress is not None:
        on_progress(0, len(book_ids), 'load')
    records, notes = load_records(
        api, book_ids, on_progress=on_progress, cancel=cancel)
    if cancel is not None and cancel.is_set():
        return None

    if on_progress is not None:
        on_progress(len(records), len(records), 'compare')
    pairs, stats = cluster.candidate_pairs(records, threshold=threshold)
    groups = cluster.group_pairs(pairs, {r['id']: r for r in records})

    stats = dict(stats)
    stats.update(notes)
    stats['threshold'] = threshold
    stats['skipped_books_total'] = len(book_ids) - len(records)

    built = report.build_report(records, pairs, groups, threshold, stats,
                                scope_note=scope_note)
    return built


# --------------------------------------------------------------------------- 合并


def preview_merge(api, records, keeper_id):
    """合并预览：不写任何东西，只算"会发生什么"。

    这一步存在的理由是**同名格式不会被复制**：`merge_book_formats` 只复制目标书缺少的
    格式（`base_tool.py:265` 的 `new_fmts = src_fmts - tgt_fmts`），所以"重复格式的那一份"
    会被直接丢弃。用户必须在动手前看到这份清单，而不是事后才发现少了一个版本。
    """
    by_id = {r['id']: r for r in records}
    target = by_id.get(keeper_id)
    if target is None:
        return {'error': 'keeper.missing'}

    target_formats = set(f.upper() for f in (target.get('formats') or []))
    moved, dropped, sources = [], [], []
    for record in records:
        if record['id'] == keeper_id:
            continue
        source_formats = set(f.upper() for f in (record.get('formats') or []))
        sources.append({
            'id': record['id'],
            'title': record.get('title') or '',
            'formats': sorted(source_formats),
        })
        for fmt in sorted(source_formats - target_formats):
            moved.append({'source_id': record['id'], 'format': fmt})
        for fmt in sorted(source_formats & target_formats):
            dropped.append({'source_id': record['id'], 'format': fmt})

    return {
        'keeper_id': keeper_id,
        'keeper_title': target.get('title') or '',
        'keeper_formats': sorted(target_formats),
        'sources': sources,
        'moved': moved,
        'dropped': dropped,
        'reclaimable_bytes': keeper.reclaimable_bytes(records, keeper_id),
        'disk_waste_bytes': keeper.duplicate_disk_waste(records, keeper_id),
    }


def merge_group(api, source_id, target_id, delete_source=True):
    """把 `source_id` 并入 `target_id`。**本工具唯一的写操作。**

    两件事，分开记账：

    1. `merge_formats(source, target)` —— 复制源书target缺失的格式文件
    2. 可选 `delete_book(source)` —— 删掉源记录

    :return: ``{'merged': [...], 'deleted': bool, 'notes': [...]}``

    已知限制（写进返回值与日志，不假装没这回事）：

    - **同名格式不会复制**：两本都是 EPUB 时，源书那份会被丢弃（看到的是 target 的版本）
    - **工具箱的删除不清理关联数据**：`BaseTool.delete_book_by_id()` 只删 `Item`
      （上游 issue #82 未修），收藏/在读/进度/评分/书单会留下悬空行
    """
    notes = []
    merged = []
    if int(source_id) == int(target_id):
        return {'merged': [], 'deleted': False, 'notes': ['same_book'],
                'error': 'merge.same_book'}

    try:
        merged = api.calibre.merge_formats(source_id, target_id) or []
    except Exception as err:  # noqa: BLE001 —— 宿主抛 RuntimeError("没有可合并的格式")
        message = str(err)
        logging.info('[book_dedup] merge_formats(%s->%s): %s', source_id, target_id, message)
        notes.append('no_new_formats')

    deleted = False
    if delete_source:
        try:
            api.calibre.delete_book(source_id)
            deleted = True
        except Exception as err:  # noqa: BLE001
            logging.error('[book_dedup] delete_book(%s) failed: %s', source_id, err)
            return {'merged': merged, 'deleted': False, 'notes': notes,
                    'error': 'merge.delete_failed', 'message': str(err)}

    return {'merged': sorted(str(f).upper() for f in merged),
            'deleted': deleted, 'notes': notes}


def delete_book(api, book_id):
    """删除一本书（单独删除，不与任何书合并）。

    只做一件事，但单独封装的意义是：**写操作集中在 `write_ops`/`driver`**，
    `tests/test_fake_host.py::TestWritePathGuard` 就是靠这条边界守住"引擎层不许写"。

    宿主侧会连带清理关联数据——收藏 / 在读 / 阅读进度 / 时长 / 评分 / 书评 / 共读记录 /
    书单关联，见上游 PoxenStudio/mybooks#82 的修复（commit a33f0c26 新增
    `webserver/base/book_data_cascade.py`，工具箱路径与宿主删除路径共用它）。
    **但那是删除不是迁移**：被删这一本上的阅读进度不会搬到同组的其它书上。
    该修复目前只在 `develop` 线上，`v4.3.0` 尚未包含——所以界面文案写的是"会一并删除"，
    不承诺"已经清理干净"，在旧宿主上也不会说错。
    """
    api.calibre.delete_book(book_id)


# --------------------------------------------------------------------------- 落盘


REPORT_FILENAME = 'report.json'
INDEX_FILENAME = 'index.json'
# 指向"最近一次跑完的那份报告"，落在工具共享目录（`get_work_dir()` 无 key 那级），
# 不随 task_id 变——宿主重启后内存里的任务就没了，靠它把报告认回来。
LATEST_MARKER = 'latest.json'

# 列表每行预览几个书名（其余点开抽屉看全部）。用户定的值：2。
# 存进索引而不是让前端去读报告：`/groups` 只读索引，几 KB 的代价换"列表直接可读"。
PREVIEW_TITLES = 2

# 报告解析缓存。报告是 MB 级，而 `/group`（每点一行）与 `/groups` 的关键字筛选都要
# **整份解析**一遍：135 组点下来就是 135 次 `json.load`，非常明显。按
# `(路径, mtime_ns, 大小)` 认版本，只留最后一份，避免无界内存。
_REPORT_CACHE = {'key': None, 'report': None}
_REPORT_CACHE_LOCK = threading.Lock()


def report_path(work_dir):
    return os.path.join(work_dir, REPORT_FILENAME)


def index_path(work_dir):
    return os.path.join(work_dir, INDEX_FILENAME)


def write_report(work_dir, report):
    """报告落盘。**不放进 progress_data**——整库查重结果是 MB 级，而 progress_data 会进
    后台任务面板和每次 `/progress` 响应。"""
    os.makedirs(work_dir, exist_ok=True)
    tmp = report_path(work_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False)
    os.replace(tmp, report_path(work_dir))
    return report_path(work_dir)


def _load_report(path):
    """按 (路径, mtime, 大小) 缓存解析结果，避免每点一行都重解析整份报告。"""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (path, stat.st_mtime_ns, stat.st_size)
    with _REPORT_CACHE_LOCK:
        if _REPORT_CACHE['key'] == key:
            return _REPORT_CACHE['report']
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            report = json.load(handle)
    except (OSError, ValueError) as err:
        logging.warning('[book_dedup] report unreadable %s: %s', path, err)
        return None
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE['key'] = key
        _REPORT_CACHE['report'] = report
    return report


def read_report(work_dir, group_index=None):
    """读报告；给了 `group_index` 就只回那一组（组可能很大，别整份塞给前端）。"""
    path = report_path(work_dir)
    if not os.path.exists(path):
        return None
    report = _load_report(path)
    if report is None:
        return None
    if group_index is None:
        return report
    try:
        index = int(group_index)
    except (TypeError, ValueError):
        return None
    groups = report.get('groups') or []
    if index < 0 or index >= len(groups):
        return None
    return groups[index]


def write_index(work_dir, report):
    """写"组 → 成员 id / 推荐保留 / 可回收 / 成员预览"的轻量索引。

    存在的理由（两条）：

    1. `/groups` 要分页、`/merge` 再跑一次引擎就会得到**另一批 id**（`group_pairs` 的簇根
       依赖并查集遍历顺序）。所以按索引把这次的成员 id 固化下来，合并、展示、报告三处必须
       用同一批 id。
    2. **列表行要直接可读**。只给 `member_count` 的话每行都只能写"x 本可能是同一本书"，
       用户必须逐行点开才知道是哪些书（真机反馈原话：不直观、不合理）。所以顺带存
       `PREVIEW_TITLES` 个成员标题与作者——组索引只多几 KB，列表却一眼能读。
    """
    groups = report.get('groups') or []
    index = []
    for position, group in enumerate(groups):
        members = group['members']
        previews = members[:PREVIEW_TITLES]
        index.append({
            'index': position,
            'members': [m['id'] for m in members],
            # `titles`/`members` 必须**同序**，两边都取自同一份 `group['members']`——
            # 顺序错位会让界面把"保留项"标到别的书上
            'titles': [m.get('title') or '' for m in previews],
            'authors': [(m.get('authors') or [''])[0] for m in previews],
            'preview_truncated': len(members) > PREVIEW_TITLES,
            'keeper_id': (group.get('recommendation') or {}).get('keeper_id'),
            'confidence': group.get('confidence'),
            'member_count': group.get('member_count'),
            'reclaimable_bytes': group.get('reclaimable_bytes', 0),
            'disk_waste_bytes': group.get('disk_waste_bytes', 0),
        })
    payload = {
        'generated_at': report.get('generated_at'),
        'threshold': report.get('threshold'),
        'summary': report.get('summary'),
        'stats': report.get('stats'),
        'scanned_books': report.get('scanned_books'),
        'groups': index,
    }
    os.makedirs(work_dir, exist_ok=True)
    tmp = index_path(work_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, index_path(work_dir))
    return payload


def read_index(work_dir):
    path = index_path(work_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def latest_marker(shared_dir):
    return os.path.join(shared_dir, LATEST_MARKER)


def write_latest_marker(shared_dir, task_id, report):
    """记住"上次是哪一次跑出来的报告"。

    **按 task_id + generated_at 双校验**：task_id 是进程内自增、与其它工具共用计数器
    （`background_service.BackgroundTask._id_counter`），新进程里会重来，只按 id 找会把
    旧目录里的旧报告当成新结果。写失败只损失"下次自动打开"，报告本身已经落盘，所以
    best-effort，不让它把整个任务判失败。
    """
    payload = {
        'task_id': task_id,
        'generated_at': report.get('generated_at'),
        'summary': report.get('summary'),
        'threshold': report.get('threshold'),
        'scanned_books': report.get('scanned_books'),
    }
    try:
        os.makedirs(shared_dir, exist_ok=True)
        path = latest_marker(shared_dir)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return True
    except OSError as err:
        logging.warning('[book_dedup] latest marker not written: %s', err)
        return False


def read_latest_marker(shared_dir):
    path = latest_marker(shared_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def marker_matches(marker, task_id, report):
    """标记是否确实指向这份报告（双校验，见 `write_latest_marker`）。"""
    if not marker or not report:
        return False
    if marker.get('task_id') != task_id:
        return False
    return marker.get('generated_at') == report.get('generated_at')


def restored_progress(marker, index=None):
    """宿主重启后 `/progress` 的回退载荷：认回上次那份报告，而不是空白页。"""
    if not marker:
        return None
    payload = {
        'status': 'completed',
        'progress': 100,
        'restored': True,
        'task_id': marker.get('task_id'),
        'summary': marker.get('summary'),
        'threshold': marker.get('threshold'),
        'scanned_books': marker.get('scanned_books'),
    }
    if index:
        payload['summary'] = index.get('summary') or payload['summary']
    return payload


# --------------------------------------------------------------------------- 合并计划


def merge_plan(api, members, keeper_id):
    """把一次"合并这一组"拆成可执行的计划，并把**会丢什么**说清楚。

    这是写操作前最重要的一步。两件事必须在动手前摆给用户看：

    1. **同名格式不会被复制**：`merge_book_formats` 只复制目标书缺的格式
       （`base_tool.py:265` 的 `new_fmts = src_fmts - tgt_fmts`），所以两本都有 EPUB 时，
       源书那份会被丢弃，留下的是 keeper 的版本。
    2. **源记录的关联数据不会被迁移**：工具箱的删除只清 `Item`
       （上游 issue #82 未修），收藏/在读/进度/评分/书单会留下悬空行。
    """
    by_id = {r['id']: r for r in members}
    target = by_id.get(keeper_id)
    if target is None:
        return {'error': 'keeper.missing'}
    sources = [r for r in members if r['id'] != keeper_id]

    keeper_formats = set(f.upper() for f in (target.get('formats') or []))
    steps = []
    for record in sources:
        source_formats = set(f.upper() for f in (record.get('formats') or []))
        steps.append({
            'source_id': record['id'],
            'source_title': record.get('title') or '',
            'target_id': keeper_id,
            'moved_formats': sorted(source_formats - keeper_formats),
            'dropped_formats': sorted(source_formats & keeper_formats),
            'size': int(record.get('size') or 0),
        })

    return {
        'keeper_id': keeper_id,
        'keeper_title': target.get('title') or '',
        'keeper_formats': sorted(keeper_formats),
        'steps': steps,
        'moved_total': sum(len(s['moved_formats']) for s in steps),
        'dropped_total': sum(len(s['dropped_formats']) for s in steps),
        'reclaimable_bytes': keeper.reclaimable_bytes(members, keeper_id),
        'disk_waste_bytes': keeper.duplicate_disk_waste(members, keeper_id),
        'warnings': [
            'working_formats_dropped',
            'source_records_not_migrated',
        ],
    }