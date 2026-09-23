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
    for position, (source_id, clone_id) in enumerate(injected):
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
    index = {
        'generated_at': built['generated_at'],
        'threshold': built['threshold'],
        'summary': built['summary'],
        'stats': built['stats'],
        'scanned_books': built['scanned_books'],
        'groups': [
            {
                'index': position,
                'members': [m['id'] for m in group['members']],
                'keeper_id': (group.get('recommendation') or {}).get('keeper_id'),
                'confidence': group.get('confidence'),
                'member_count': group.get('member_count'),
                'reclaimable_bytes': group.get('reclaimable_bytes', 0),
                'disk_waste_bytes': group.get('disk_waste_bytes', 0),
            }
            for position, group in enumerate(built['groups'])
        ],
    }
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
  var DATA = { report: null, index: null, merged: [] };
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

  window.__previewApi = function (name, params) {
    params = params || {};
    if (!DATA.report) {
      return ready.then(function () { return window.__previewApi(name, params); });
    }
    var mergedIds = DATA.merged.map(function (m) { return m.id; });

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
      var groups = DATA.index.groups.filter(function (g) {
        return !(g.members || []).some(function (id) { return mergedIds.indexOf(id) >= 0; });
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
        merged_ids: mergedIds,
        removed_titles: DATA.merged.map(function (m) {
          return { id: m.id, title: m.title, into: m.into, at: m.at };
        }),
      } });
    }
    if (name === 'group') {
      var position = parseInt(params.index, 10);
      var group = DATA.report.groups[position];
      if (!group) return Promise.resolve({ err: 'group.not_found', msg: '找不到该分组' });
      return Promise.resolve({ err: 'ok', data: { task_id: 7, group: group, merged_ids: mergedIds } });
    }
    if (name === 'merge_plan') {
      var pos = parseInt(params.index, 10);
      var target = DATA.report.groups[pos];
      if (!target) return Promise.resolve({ err: 'group.not_found', msg: '找不到该分组' });
      var keeperId = parseInt(params.keeper_id, 10) || target.recommendation.keeper_id;
      var keeper = null;
      target.members.forEach(function (m) { if (m.id === keeperId) keeper = m; });
      var keeperFormats = (keeper.formats || []).map(function (f) { return f.toUpperCase(); });
      var steps = target.members.filter(function (m) { return m.id !== keeperId; }).map(function (m) {
        var formats = (m.formats || []).map(function (f) { return f.toUpperCase(); });
        return {
          source_id: m.id, source_title: m.title, target_id: keeperId,
          moved_formats: formats.filter(function (f) { return keeperFormats.indexOf(f) < 0; }),
          dropped_formats: formats.filter(function (f) { return keeperFormats.indexOf(f) >= 0; }),
          size: m.size,
        };
      });
      return Promise.resolve({ err: 'ok', data: {
        index: pos, keeper_id: keeperId, keeper_title: keeper.title,
        keeper_formats: keeperFormats, steps: steps,
        moved_total: steps.reduce(function (n, s) { return n + s.moved_formats.length; }, 0),
        dropped_total: steps.reduce(function (n, s) { return n + s.dropped_formats.length; }, 0),
        reclaimable_bytes: target.reclaimable_bytes,
        disk_waste_bytes: target.disk_waste_bytes,
        warnings: ['working_formats_dropped', 'source_records_not_migrated'],
        members: target.members, recommendation: target.recommendation,
      } });
    }
    if (name === 'merge') {
      // 回的形状必须与 tool.MergeHandler 一致（含 moved_total / removed_ids）——
      // 少了字段界面上会显示 undefined，那会把真问题掩盖掉
      var target = DATA.report.groups[parseInt(params.index, 10)];
      var keeperId = parseInt(params.keeper_id, 10) || (target && target.recommendation.keeper_id);
      var sources = target ? target.members.filter(function (m) { return m.id !== keeperId; }) : [];
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