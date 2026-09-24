# -*- coding: utf-8 -*-
"""生成一个能真跑的本地预览：把 frontend/ 拷到 dev/preview 下，配一个 stub 桥。

为什么需要它：`tool.py` 依赖宿主（`webserver.*`），本机跑不起来；但界面必须用真浏览器
点过才算验证。所以这里用**真实报告数据**（跑一遍离线引擎生成 `data.json`）+ 一个 stub
桥（复刻 `window.MyBooksToolBridge` 的接口）把整个前端驱动起来。

用法：
    python scripts/make_preview.py          # 生成 dev/preview/
    python -m http.server 8765 -d dev/preview
    打开 http://127.0.0.1:8765/index.html

**预览用的 data.json 是真实跑出来的**：脚本会加载真书库、注入几组重复、跑引擎，
所以界面上看到的分组/差异表/推荐保留都是真数据，不是手写的假货。
"""
import importlib.util
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
FRONTEND = os.path.join(ROOT, 'frontend')
PREVIEW = os.path.join(ROOT, 'dev', 'preview')
BACKEND = os.path.join(ROOT, 'backend')

sys.path.insert(0, HERE)
import smoke_offline  # noqa: E402  （同目录的离线 harness，复用它的真书库替身）


def build_data():
    """跑一遍真引擎，产出预览要的报告与索引。

    为了让**体积类数字不是全 0**（否则"可回收空间/同格式重占"两栏看不出意义，合并预览里
    "哪些格式会被丢弃"也演示不了），给注入的重复项造几个**真实存在的小文件**当格式文件：
    引擎与 driver 都是照常调 `os.path.getsize`，不含任何预览专用分支。
    """
    package = smoke_offline.load_package()
    driver = importlib.import_module(package.__name__ + '.driver')

    library = os.path.abspath(smoke_offline.DEFAULT_LIBRARY)
    api = smoke_offline.FakeApi(library)

    # 预览用的假文件目录（每次重建，量很小）
    blob_dir = os.path.join(PREVIEW, '_blobs')
    if os.path.exists(blob_dir):
        shutil.rmtree(blob_dir)
    os.makedirs(blob_dir)

    injected = smoke_offline._inject_duplicates(api, 10)
    # 再给第一本挂**第三个**副本：0.1.5 的核心场景是"只合并其中两本、第三本保持原样"，
    # 全是两本一组的话，预览里演练不出"没勾的那本原样保留"（2 本里不勾 1 本 = 无事可做）。
    extra_source = injected[0][0]
    extra = dict(api.calibre._books[extra_source])
    extra['id'] = max(api.calibre._books) + 1
    extra['title'] = '%s（修订版）' % extra['title']
    extra['available_formats'] = ['EPUB', 'AZW3']
    extra.pop('_paths', None)
    api.calibre._books[extra['id']] = extra

    for position, clone_id in enumerate([cid for _src, cid in injected] + [extra['id']]):
        # 注意要拿**书库里那份**对象：`_inject_duplicates` 存的是浅拷贝，
        # 改它返回的中间变量不会影响 `all_book_ids()` 读到的记录
        clone = api.calibre._books[clone_id]
        paths = {}
        for index, fmt in enumerate(clone['available_formats']):
            # 同一本书的两个副本体积接近但不相同（真实书库就是这样）
            size = 380_000 + position * 91_000 + index * 137_000
            path = os.path.join(blob_dir, 'book%d.%s' % (clone_id, fmt.lower()))
            with open(path, 'wb') as handle:
                handle.write(b'\0' * size)
            paths[fmt] = path
        clone['_format_files'] = paths

    book_ids = sorted(api.calibre._books)
    built = driver.run_scan(api, book_ids, threshold=0.85,
                            scope_note='预览数据（真实引擎产出）')
    # 索引**走真的 `driver.write_index()`**，不在这里手搓一份。
    # 手搓过一版，结果后端加了 `titles`/`authors` 预览字段之后预览里看不到——
    # 两份形状各自演化，正是"预览与真机不一致"的来源。
    index = driver.write_index(os.path.join(PREVIEW, '_index'), built)
    return built, index


STUB_BRIDGE = """/* 预览用的 stub 桥：复刻 window.MyBooksToolBridge 的公开接口。
 * 只有 fetch / notify / theme / locale 四项，宿主那份还多两个订阅回调，
 * 这里也照实现——前端对两者的用法必须完全一样。
 */
(function (window) {
  'use strict';
  var state = { theme: 'light', locale: 'zh' };
  var listeners = { locale: [], theme: [] };

  function json(payload) {
    return Promise.resolve(payload);
  }

  function route(path, options) {
    var clean = String(path || '').replace(/^\\//, '');
    var name = clean.split('?')[0];
    var query = clean.indexOf('?') >= 0 ? clean.slice(clean.indexOf('?') + 1) : '';
    var params = {};
    query.split('&').forEach(function (pair) {
      if (!pair) return;
      var bits = pair.split('=');
      params[decodeURIComponent(bits[0])] = decodeURIComponent(bits.slice(1).join('='));
    });
    // POST 的请求体要解出来给假后端用（`delete_source` 就在里面）——
    // 丢掉它会让"删除源记录"这个开关读不到值，静默变成"不删"
    var body = {};
    if (options && options.body) {
      try { body = JSON.parse(options.body) || {}; } catch (e) { body = {}; }
    }
    Object.keys(body).forEach(function (key) { params[key] = body[key]; });
    return window.__previewApi(name, params);
  }

  var bridge = {
    toolId: 'book_dedup',
    fetch: function (path, options) { return route(path, options); },
    notify: function (message, level) {
      window.__previewNotify(message, level || 'info');
    },
    onLocaleChange: function (fn) { listeners.locale.push(fn); },
    onThemeChange: function (fn) { listeners.theme.push(fn); },
  };
  Object.defineProperty(bridge, 'theme', { get: function () { return state.theme; }, enumerable: true });
  Object.defineProperty(bridge, 'locale', { get: function () { return state.locale; }, enumerable: true });

  window.MyBooksToolBridge = bridge;
  // 供预览面板切换深浅色与语言，验证前端对宿主推送的处理
  window.__previewSetTheme = function (theme) {
    state.theme = theme;
    listeners.theme.forEach(function (fn) { fn(theme); });
  };
  window.__previewSetLocale = function (locale) {
    state.locale = locale;
    listeners.locale.forEach(function (fn) { fn(locale); });
  };
}(window));
"""

STUB_API = """/* 预览用的假后端：直接读 data.json 里那份**真报告**，并按 tool.py 的响应形状包装。
 * 这里没有判定逻辑——判定在后端引擎里，预览只为验证界面。
 */
(function (window) {
  'use strict';
  var DATA = { report: null, index: null, merged: [], deleted: [], ignored: [] };
  var failing = [];

  // 数据没就绪时的调用**挂起等待**，而不是立刻回错。
  // 直接回错会让首屏真实地走向 catch 分支（界面上显示"读取书库信息失败"）——
  // 那是预览脚手架自己造出来的假故障，不是工具的缺陷，会掩盖真问题。
  var ready = fetch('data.json').then(function (r) { return r.json(); }).then(function (out) {
    DATA.report = out.report;
    DATA.index = out.index;
  }).catch(function (err) {
    failing.push(String(err));
    throw err;
  });

  function summary() { return DATA.index ? DATA.index.summary : {}; }

  /** 已经不在书库的成员（合并掉的 + 单独删掉的）——与 write_ops.gone_ids 同义。 */
  function goneIds() {
    return DATA.merged.map(function (m) { return m.id; })
      .concat(DATA.deleted.map(function (d) { return d.id; }));
  }

  /** 忽略名单的配对键（与 dedup/ignore.pair_key 同规范：小的在前）。 */
  function ignoredKeys() {
    return DATA.ignored.map(function (entry) { return entry[0] + ',' + entry[1]; });
  }

  function pairKey(a, b) { return a <= b ? a + ',' + b : b + ',' + a; }

  function pairsIn(ids) {
    var unique = ids.slice().sort(function (a, b) { return a - b; });
    var out = [];
    for (var i = 0; i < unique.length; i += 1) {
      for (var j = i + 1; j < unique.length; j += 1) {
        out.push([unique[i], unique[j]]);
      }
    }
    return out;
  }

  function ignoredCount(ids) {
    var keys = ignoredKeys();
    return pairsIn(ids).filter(function (pair) {
      return keys.indexOf(pair[0] + ',' + pair[1]) >= 0;
    }).length;
  }

  function remainingMembers(group) {
    var gone = goneIds();
    return (group.members || []).filter(function (id) { return gone.indexOf(id) < 0; });
  }

  /** 与 driver 的 `member_titles` 同序：扣掉已消失的成员重算行标题与两个字节数
   * （旧索引里没有这些字段就跳过）。真后端在 `tool._live_row_fields` 里做同一件事。 */
  function applyLiveRow(row, group, gone) {
    var titles = group.member_titles || [];
    var authors = group.member_authors || [];
    var sizes = group.member_sizes || [];
    var formats = group.member_formats || [];
    if (!titles.length || titles.length !== (group.members || []).length) return;
    var liveTitles = [], liveAuthors = [], live = [];
    (group.members || []).forEach(function (id, position) {
      if (gone.indexOf(id) >= 0) return;
      liveTitles.push(titles[position]);
      liveAuthors.push(authors[position] || '');
      live.push({ id: id, size: sizes[position] || 0, formats: formats[position] || [] });
    });
    row.titles = liveTitles.slice(0, 2);
    row.authors = liveAuthors.slice(0, 2);
    row.member_count = liveTitles.length;
    row.preview_truncated = liveTitles.length > 2;
    var keeperId = group.keeper_id;
    var keeperLive = live.some(function (m) { return m.id === keeperId; });
    if (keeperLive) {
      var keeperFormats = (formats[(group.members || []).indexOf(keeperId)] || []);
      row.reclaimable_bytes = live.reduce(function (sum, m) {
        return sum + (m.id === keeperId ? 0 : m.size);
      }, 0);
      row.disk_waste_bytes = live.reduce(function (sum, m) {
        if (m.id === keeperId || !m.formats.length) return sum;
        var subset = m.formats.every(function (f) { return keeperFormats.indexOf(f) >= 0; });
        return sum + (subset ? m.size : 0);
      }, 0);
    }
  }

  /** 对照表的裁剪版：真后端拿活着的成员**重建**（`diff.build_table`），预览这边没有引擎，
   * 只按 id 扣掉已消失成员那一格。形状一致；差别只是"重算后变相同的行会被折进 shared"，
   * 预览里可能仍留着一行空的差异——不影响要验的界面接线。 */
  function filterDiff(table, live) {
    if (!table) return table;
    var keep = live.map(function (m) { return m.id; });
    var rows = [];
    (table.rows || []).forEach(function (row) {
      var cells = (row.cells || []).filter(function (c) { return keep.indexOf(c.book_id) >= 0; });
      if (cells.length < 2) return;
      var copy = {};
      Object.keys(row).forEach(function (key) { copy[key] = row[key]; });
      copy.cells = cells;
      if (keep.indexOf(copy.best_id) < 0) copy.best_id = null;
      rows.push(copy);
    });
    var out = {};
    Object.keys(table).forEach(function (key) { out[key] = table[key]; });
    out.rows = rows;
    return out;
  }

  window.__previewApi = function (name, params) {
    params = params || {};
    if (!DATA.report) {
      return ready.then(function () { return window.__previewApi(name, params); });
    }
    var mergedIds = goneIds();

    if (name === 'scope') {
      return Promise.resolve({ err: 'ok', data: {
        total_books: DATA.index.scanned_books,
        max_books: 60000,
        threshold: DATA.index.threshold,
        threshold_range: [0.5, 1.0],
        confidence: ['strong', 'likely', 'weak'],
        keep_rules: ['metadata', 'formats', 'size', 'oldest', 'newest'],
      } });
    }
    if (name === 'progress') {
      return Promise.resolve({ err: 'ok', data: {
        status: 'completed', progress: 100, restored: true, task_id: 7,
        progress_data: { phase: 'done', summary: summary(),
                         threshold: DATA.index.threshold,
                         scanned_books: DATA.index.scanned_books },
      } });
    }
    if (name === 'groups') {
      // 与 tool.GroupsHandler 一致：按**剩余成员**够不够两本摘组，并扣掉已消失的成员
      // 重算行标题（预览里也得这样，否则"只合并了一部分"之后这一行的书名是假的）
      var groups = DATA.index.groups.filter(function (g) {
        var live = remainingMembers(g);
        if (live.length < 2) return false;
        // 整组的两两配对都被忽略 → 这一组不再报出来（与后端 _visible_groups 同规则）
        return ignoredCount(live) < pairsIn(live).length;
      }).map(function (g) {
        var row = {};
        Object.keys(g).forEach(function (key) { row[key] = g[key]; });
        var gone = (g.members || []).filter(function (id) { return mergedIds.indexOf(id) >= 0; });
        row.stale_count = gone.length;
        row.ignored_pairs = ignoredCount(remainingMembers(g));
        if (gone.length) applyLiveRow(row, g, gone);
        return row;
      });
      if (params.confidence) {
        var wanted = params.confidence.split(',');
        groups = groups.filter(function (g) { return wanted.indexOf(g.confidence) >= 0; });
      }
      var size = parseInt(params.size || '50', 10);
      var page = parseInt(params.page || '0', 10);
      return Promise.resolve({ err: 'ok', data: {
        task_id: 7, generated_at: DATA.index.generated_at,
        threshold: DATA.index.threshold, summary: summary(), stats: DATA.index.stats,
        scanned_books: DATA.index.scanned_books,
        filtered_total: groups.length, page: page,
        groups: groups.slice(page * size, page * size + size),
        gone_ids: mergedIds,
        removed_titles: DATA.merged.map(function (m) {
          return { id: m.id, title: m.title, into: m.into, at: m.at };
        }),
        deleted_titles: DATA.deleted.map(function (d) {
          return { id: d.id, title: d.title, at: d.at };
        }),
        failed_titles: [],
      } });
    }
    if (name === 'group') {
      var position = parseInt(params.index, 10);
      var group = DATA.report.groups[position];
      if (!group) return Promise.resolve({ err: 'group.not_found', msg: '找不到该分组' });
      var live = [];
      (group.members || []).forEach(function (m) {
        if (mergedIds.indexOf(m.id) < 0) live.push(m);
      });
      if (live.length !== (group.members || []).length) {
        // 成员变过 → 换成存活成员（推荐保留项若已被删掉，回落到第一本），
        // 并**一起裁对照表**：否则表头 2 列、每行 3 格
        var copy = {};
        Object.keys(group).forEach(function (key) { copy[key] = group[key]; });
        copy.members = live;
        copy.member_count = live.length;
        copy.diff = filterDiff(group.diff, live);
        var keeper = group.recommendation && group.recommendation.keeper_id;
        var stillThere = live.some(function (m) { return m.id === keeper; });
        copy.recommendation = stillThere ? group.recommendation : {
          keeper_id: live.length ? live[0].id : 0,
          reasons: [{ code: 'manual' }], protected: [], rule: 'manual',
        };
        group = copy;
      }
      group = Object.assign({}, group, {
        ignored_pairs: ignoredCount(live.map(function (m) { return m.id; })),
      });
      return Promise.resolve({ err: 'ok', data: { task_id: 7, group: group, gone_ids: mergedIds } });
    }
    if (name === 'merge_plan') {
      var pos = parseInt(params.index, 10);
      var target = DATA.report.groups[pos];
      if (!target) return Promise.resolve({ err: 'group.not_found', msg: '找不到该分组' });
      var keeperId = parseInt(params.keeper_id, 10) || target.recommendation.keeper_id;
      var keeper = null;
      target.members.forEach(function (m) { if (m.id === keeperId) keeper = m; });
      // 勾选集：缺省 = 除保留项外全部；显式给了就以给的为准（与 write_ops._parse_ids 同语义）
      var picked = params.source_ids === undefined || params.source_ids === null
        ? null
        : String(params.source_ids).split(',').map(function (text) { return parseInt(text, 10); })
            .filter(function (id) { return !isNaN(id); });
      var keeperFormats = (keeper.formats || []).map(function (f) { return f.toUpperCase(); });
      var chosen = target.members.filter(function (m) {
        if (m.id === keeperId) return false;
        return picked === null || picked.indexOf(m.id) >= 0;
      });
      var keptMembers = target.members.filter(function (m) {
        return m.id !== keeperId && chosen.indexOf(m) < 0;
      });
      var steps = chosen.map(function (m) {
        var formats = (m.formats || []).map(function (f) { return f.toUpperCase(); });
        return {
          source_id: m.id, source_title: m.title, target_id: keeperId,
          moved_formats: formats.filter(function (f) { return keeperFormats.indexOf(f) < 0; }),
          dropped_formats: formats.filter(function (f) { return keeperFormats.indexOf(f) >= 0; }),
          size: m.size,
        };
      });
      var movingBytes = chosen.reduce(function (n, m) { return n + (m.size || 0); }, 0);
      var wasteBytes = chosen.reduce(function (n, m) {
        var formats = (m.formats || []).map(function (f) { return f.toUpperCase(); });
        var subset = formats.every(function (f) { return keeperFormats.indexOf(f) >= 0; });
        return n + (subset && formats.length ? (m.size || 0) : 0);
      }, 0);
      return Promise.resolve({ err: 'ok', data: {
        index: pos, keeper_id: keeperId, keeper_title: keeper.title,
        keeper_formats: keeperFormats, steps: steps,
        source_ids: steps.map(function (s) { return s.source_id; }),
        kept: keptMembers.map(function (m) {
          return { id: m.id, title: m.title, formats: m.formats || [], size: m.size || 0 };
        }),
        moved_total: steps.reduce(function (n, s) { return n + s.moved_formats.length; }, 0),
        dropped_total: steps.reduce(function (n, s) { return n + s.dropped_formats.length; }, 0),
        reclaimable_bytes: movingBytes,
        disk_waste_bytes: wasteBytes,
        warnings: ['working_formats_dropped', 'source_records_not_migrated'],
        members: target.members, recommendation: target.recommendation,
      } });
    }
    if (name === 'delete') {
      // 与 tool.DeleteHandler 同形状：只收 index + book_id，且必须**是这一组的成员**
      var pos = parseInt(params.index, 10);
      var target = DATA.report.groups[pos];
      if (!target) return Promise.resolve({ err: 'group.not_found', msg: '找不到该分组' });
      var want = parseInt(params.book_id, 10);
      var found = null;
      target.members.forEach(function (m) { if (m.id === want) found = m; });
      if (!found) {
        return Promise.resolve({ err: 'book.not_in_group', msg: '要删除的书必须是这一组里的成员' });
      }
      DATA.deleted.push({
        id: want, title: found.title,
        at: new Date().toISOString().slice(0, 19).replace('T', ' '),
      });
      return Promise.resolve({ err: 'ok', data: { deleted_id: want, title: found.title } });
    }
    if (name === 'ignored') {
      return Promise.resolve({ err: 'ok', data: {
        ignored: DATA.ignored.map(function (pair) {
          var titles = {};
          (DATA.report.groups || []).forEach(function (group) {
            (group.members || []).forEach(function (m) { titles[m.id] = m.title; });
          });
          return {
            a: pair[0], b: pair[1],
            a_title: titles[pair[0]] || ('#' + pair[0]),
            b_title: titles[pair[1]] || ('#' + pair[1]),
            at: pair[2] || '',
          };
        }),
      } });
    }
    if (name === 'ignore') {
      // 与 tool.IgnoreHandler 同守门：id 必须是**这一组的成员**
      var pos = parseInt(params.index, 10);
      var target = DATA.report.groups[pos];
      if (!target) return Promise.resolve({ err: 'group.not_found', msg: '找不到该分组' });
      var ids = (target.members || []).map(function (m) { return m.id; });
      if (ids.length < 2) return Promise.resolve({ err: 'params.invalid', msg: '至少要选两本' });
      var stamp = new Date().toISOString().slice(0, 19).replace('T', ' ');
      var added = 0;
      pairsIn(ids).forEach(function (pair) {
        if (ignoredKeys().indexOf(pair[0] + ',' + pair[1]) >= 0) return;
        DATA.ignored.push([pair[0], pair[1], stamp]);
        added += 1;
      });
      return Promise.resolve({ err: 'ok', data: {
        added: added,
        titles: (target.members || []).map(function (m) { return m.title; }),
      } });
    }
    if (name === 'unignore') {
      if (params.all) {
        var removedAll = DATA.ignored.length;
        DATA.ignored = [];
        return Promise.resolve({ err: 'ok', data: { removed: removedAll } });
      }
      var wanted = (params.pairs || []).map(function (pair) { return pairKey(pair[0], pair[1]); });
      var before = DATA.ignored.length;
      DATA.ignored = DATA.ignored.filter(function (pair) {
        return wanted.indexOf(pairKey(pair[0], pair[1])) < 0;
      });
      return Promise.resolve({ err: 'ok', data: { removed: before - DATA.ignored.length } });
    }
    if (name === 'merge') {
      // 回的形状必须与 tool.MergeHandler 一致（含 moved_total / removed_ids / kept）——
      // 少了字段界面上会显示 undefined，那会把真问题掩盖掉
      var target = DATA.report.groups[parseInt(params.index, 10)];
      var keeperId = parseInt(params.keeper_id, 10) || (target && target.recommendation.keeper_id);
      var all = target ? target.members.filter(function (m) { return m.id !== keeperId; }) : [];
      var picked = params.source_ids === undefined || params.source_ids === null
        ? null
        : params.source_ids.map(function (id) { return parseInt(id, 10); });
      var sources = picked === null
        ? all
        : all.filter(function (m) { return picked.indexOf(m.id) >= 0; });
      var kept = all.filter(function (m) { return sources.indexOf(m) < 0; })
        .map(function (m) { return { id: m.id, title: m.title, formats: m.formats || [], size: m.size || 0 }; });
      var keeper = null;
      if (target) target.members.forEach(function (k) { if (k.id === keeperId) keeper = k; });
      var keeperFormats = keeper ? (keeper.formats || []).map(function (f) { return f.toUpperCase(); }) : [];
      var moved = 0;
      sources.forEach(function (m) {
        (m.formats || []).forEach(function (f) {
          if (keeperFormats.indexOf(f.toUpperCase()) < 0) moved += 1;
        });
      });
      var doDelete = params.delete_source === undefined ? true : !!params.delete_source;
      if (doDelete) {
        sources.forEach(function (m) {
          DATA.merged.push({
            id: m.id, title: m.title,
            into: keeper ? keeper.title : '',
            at: new Date().toISOString().slice(0, 19).replace('T', ' '),
          });
        });
      }
      return Promise.resolve({ err: 'ok', data: {
        keeper_id: keeperId,
        keeper_title: keeper ? keeper.title : '',
        moved_total: moved,
        dropped_total: 0,
        removed_ids: doDelete ? sources.map(function (m) { return m.id; }) : [],
        kept: kept,
        steps: [], failed: 0,
        warnings: ['source_records_not_migrated'],
      } });
    }
    return Promise.resolve({ err: 'not_found', msg: '预览未实现：' + name });
  };

  window.__previewNotify = function (message, level) {
    var node = document.createElement('div');
    node.className = 'bd-toast bd-toast-' + level;
    node.textContent = '[notify] ' + message;
    document.getElementById('toasts').appendChild(node);
    setTimeout(function () { node.remove(); }, 4000);
  };
}(window));
"""

PREVIEW_TOOLBAR = """/* 预览面板：切换深浅色/语言、模拟"已合并"、以及拦掉 window.open。
 * 这些都不是工具的功能，只是为了在本地把宿主侧的行为补上。
 */
(function (window) {
  'use strict';
  window.__previewMerged = [];
  var opened = [];

  // 预览里 window.open 会真的跳走，拦下来记录即可
  var realOpen = window.open;
  window.open = function (url) {
    opened.push(url);
    window.__previewNotify('打开书籍页：' + url, 'info');
    return null;
  };

  window.addEventListener('DOMContentLoaded', function () {
    var bar = document.createElement('div');
    bar.className = 'bd-preview-bar';
    bar.innerHTML =
      '<strong>预览</strong>' +
      '<button id="pv-theme">切换深浅色</button>' +
      '<button id="pv-locale">切到 English</button>' +
      '<button id="pv-open">已打开：0 个书籍页</button>';
    document.body.insertBefore(bar, document.body.firstChild);

    document.getElementById('pv-theme').addEventListener('click', function () {
      var next = document.body.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      window.__previewSetTheme(next);
    });
    document.getElementById('pv-locale').addEventListener('click', function () {
      var current = window.MyBooksToolBridge.locale;
      window.__previewSetLocale(current === 'en' ? 'zh' : 'en');
    });
    document.getElementById('pv-open').addEventListener('click', function () {
      this.textContent = '已打开：' + opened.length + ' 个书籍页';
    });
  });
}(window));
"""

PREVIEW_CSS = """.bd-preview-bar {
  position: sticky;
  top: 0;
  z-index: 40;
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 8px 16px;
  background: #fffbe6;
  border-bottom: 1px solid #e6d9a8;
  font: 13px/1.5 system-ui, sans-serif;
  color: #4a3b00;
}
.bd-preview-bar button {
  font: inherit;
  cursor: pointer;
  border: 1px solid #c9b871;
  background: #fff;
  border-radius: 4px;
  padding: 3px 10px;
}
"""


def main():
    if os.path.exists(PREVIEW):
        shutil.rmtree(PREVIEW)
    os.makedirs(PREVIEW)

    # 1) 拷前端（预览与线上跑的是同一份文件，不做任何改写）
    for item in os.listdir(FRONTEND):
        source = os.path.join(FRONTEND, item)
        target = os.path.join(PREVIEW, item)
        if os.path.isdir(source):
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)

    # 2) 真报告数据
    report, index = build_data()
    with open(os.path.join(PREVIEW, 'data.json'), 'w', encoding='utf-8') as handle:
        json.dump({'report': report, 'index': index}, handle, ensure_ascii=False)
    print('预览数据：%d 组 / 扫描 %d 本' % (
        index['summary']['group_count'], index['scanned_books']))

    # 3) stub 桥 / 假后端 / 预览面板
    os.makedirs(os.path.join(PREVIEW, 'static'), exist_ok=True)
    with open(os.path.join(PREVIEW, 'static', 'toolbox-bridge.js'), 'w',
              encoding='utf-8') as handle:
        handle.write(STUB_BRIDGE)
    with open(os.path.join(PREVIEW, 'preview-api.js'), 'w', encoding='utf-8') as handle:
        handle.write(STUB_API)
    with open(os.path.join(PREVIEW, 'preview-toolbar.js'), 'w', encoding='utf-8') as handle:
        handle.write(PREVIEW_TOOLBAR)
    with open(os.path.join(PREVIEW, 'preview.css'), 'w', encoding='utf-8') as handle:
        handle.write(PREVIEW_CSS)

    # 4) 在 index.html 里挂上预览脚本（**只改预览副本**）
    index_path = os.path.join(PREVIEW, 'index.html')
    with open(index_path, 'r', encoding='utf-8') as handle:
        html = handle.read()
    html = html.replace(
        '<link rel="stylesheet" href="lib/theme.css',
        '<link rel="stylesheet" href="preview.css">\n  <link rel="stylesheet" href="lib/theme.css')
    html = html.replace(
        '<script src="app.js',
        '<script src="preview-api.js"></script>\n  <script src="preview-toolbar.js"></script>\n  <script src="app.js')
    with open(index_path, 'w', encoding='utf-8') as handle:
        handle.write(html)

    print('预览已生成：%s' % PREVIEW)
    print('  python -m http.server 8765 -d dev/preview')


if __name__ == '__main__':
    main()