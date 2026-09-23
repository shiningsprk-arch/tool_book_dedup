/* 查重合并 —— 自包含前端（无构建步骤）。
 *
 * 后端的判定结果已经算好一切（分组、差异表、推荐保留、合并计划），这里只负责展示与
 * 把用户的选择回传。所以本文件里**不做任何查重逻辑**——中文分词、相似度、去重规则
 * 全在 backend/dedup/ 里，那边有单测。
 */
(function () {
  'use strict';

  var TOOL_ID = 'book_dedup';
  var LOCALE = 'zh';
  var DICT = {};
  var FALLBACK = 'zh';
  // 语言包加载完之前渲染出来的**动态**内容（分组行、对照表、合并预览）用的是兜底文案，
  // 之后不会自己重画——所以要在语言包就绪时重渲染一次。静态的 data-i18n 由 applyI18n()
  // 直接改文本，不需要重画。
  var rerender = function () {};

  // ---------------------------------------------------------------- 桥

  // 宿主注入的桥：`window.MyBooksToolBridge`（注意不是 toolboxBridge）。
  // `bridge.theme` / `bridge.locale` 是**字符串**（getter，随宿主变化实时更新），
  // 变化订阅走 `onThemeChange` / `onLocaleChange`。
  var bridge = window.MyBooksToolBridge || null;

  function api(path, options) {
    if (bridge && typeof bridge.fetch === 'function') {
      return bridge.fetch(path, options);
    }
    // 本地预览（stub bridge 不在时）直接打同源接口
    var opts = options || {};
    return fetch('/api/toolbox/tool/' + TOOL_ID + '/' + path, opts).then(function (resp) {
      return resp.json();
    });
  }

  function notify(message, level) {
    if (bridge && typeof bridge.notify === 'function') {
      bridge.notify(message, level || 'info');
      return;
    }
    toast(message, level);
  }

  function applyTheme(theme) {
    document.body.setAttribute('data-theme', theme || 'light');
  }

  function t(key, fallback) {
    if (DICT[key] !== undefined && DICT[key] !== null) return DICT[key];
    if (fallback !== undefined) return fallback;
    return key;
  }

  function loadLocale(code) {
    code = code || LOCALE;
    var candidates = [code];
    if (code.indexOf('-') > 0) candidates.push(code.split('-')[0]);
    if (candidates.indexOf(FALLBACK) < 0) candidates.push(FALLBACK);

    function tryNext() {
      if (!candidates.length) return Promise.resolve();
      var next = candidates.shift();
      return fetch('locales/' + next + '.json').then(function (resp) {
        if (!resp.ok) throw new Error('missing locale ' + next);
        return resp.json();
      }).then(function (data) {
        DICT = flatten(data);
        applyI18n();
        // 动态内容（分组行/对照表/弹层/汇总）跟着重画一次，否则会一直留着兜底文案
        try { rerender(); } catch (err) { /* 重画失败不该影响语言切换本身 */ }
      }).catch(tryNext);
    }
    return tryNext();
  }

  function flatten(source, prefix, out) {
    out = out || {};
    prefix = prefix || '';
    Object.keys(source || {}).forEach(function (key) {
      var value = source[key];
      var full = prefix ? prefix + '.' + key : key;
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        flatten(value, full, out);
      } else {
        out[full] = value;
      }
    });
    return out;
  }

  function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(function (node) {
      var key = node.getAttribute('data-i18n');
      var text = t(key, null);
      if (text !== null) node.textContent = text;
    });
    document.querySelectorAll('[data-i18n-attr]').forEach(function (node) {
      var spec = node.getAttribute('data-i18n-attr').split(':');
      var text = t(spec[1], null);
      if (text !== null) node.setAttribute(spec[0], text);
    });
    document.title = t('app.title', '查重合并');
  }

  // ---------------------------------------------------------------- 状态

  var state = {
    totalBooks: 0,
    threshold: 85,
    scopeData: null,     // 书库规模等（供语言切换后重算提示）
    scopeMessage: '',    // 读取失败/恢复失败时的提示文案
    generatedAt: '',     // 报告生成时间（语言切换后重画列表要用）
    scan: null,          // 最近一次扫描的汇总
    page: 0,
    pageSize: 50,
    confidence: '',
    keyword: '',
    groups: [],
    filteredTotal: 0,
    mergedIds: [],
    removedTitles: [],
    active: null,        // 当前打开的分组
    keeperId: null,
    plan: null,
    pollTimer: null,
  };

  var el = {};

  function cache() {
    [
      'app', 'scope-hint', 'threshold', 'btn-scan', 'btn-cancel',
      'run-card', 'run-label', 'run-count', 'run-bar', 'run-detail',
      'summary-card', 'summary', 'filter-confidence', 'filter-keyword',
      'list-meta', 'groups', 'pager', 'pager-label', 'btn-prev', 'btn-next',
      'empty-state', 'handled-card', 'handled-list',
      'bench-overlay', 'bench-body', 'bench-title', 'bench-close', 'bench-hint',
      'btn-merge', 'btn-preview',
      'plan-overlay', 'plan-body', 'plan-title', 'plan-close', 'plan-hint',
      'btn-apply', 'plan-cancel', 'delete-source', 'toasts',
    ].forEach(function (id) {
      el[id] = document.getElementById(id);
    });
  }

  function toast(message, level) {
    if (!el.toasts) return;
    var node = document.createElement('div');
    node.className = 'bd-toast bd-toast-' + (level || 'info');
    node.textContent = message;
    el.toasts.appendChild(node);
    setTimeout(function () { node.remove(); }, 4200);
  }

  // ---------------------------------------------------------------- 工具函数

  // 语言包就绪 / 切换时重画动态内容（静态的 data-i18n 由 applyI18n 处理）
  rerender = function () {
    if (!el || !el['scope-hint']) return;
    el['scope-hint'].textContent = scopeHintText();
    if (state.scan) renderSummary(state.scan);
    if (state.groups && state.groups.length) {
      renderList({ generated_at: state.generatedAt });
    }
    if (state.active && el['bench-overlay'] && !el['bench-overlay'].hidden) {
      el['bench-body'].innerHTML = benchBodyHtml(state.active.group, state.active.keeperId);
      bindBench();
    }
    if (state.plan && el['plan-overlay'] && !el['plan-overlay'].hidden) {
      renderPlan(state.plan);
    }
    renderHandled();
  };

  function formatBytes(size) {
    size = Number(size) || 0;
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return (index === 0 ? Math.round(size) : size.toFixed(1)) + ' ' + units[index];
  }

  var REASON_KEYS = {
    isbn: 'reason.isbn',
    fuzzy_metadata: 'reason.fuzzy',
    weak_title_only: 'reason.weak',
    exact_metadata: 'reason.exact',
    file_hash: 'reason.hash',
  };
  var CONFIDENCE_KEYS = {
    strong: 'confidence.strong',
    likely: 'confidence.likely',
    weak: 'confidence.weak',
    certain: 'confidence.certain',
  };
  var KEEPER_REASON_KEYS = {
    metadata: 'keep.metadata',
    formats: 'keep.formats',
    size: 'keep.size',
    isbn: 'keep.isbn',
    oldest: 'keep.oldest',
    newest: 'keep.newest',
    added: 'keep.added',
    manual: 'keep.manual',
    protected: 'keep.protected',
    multi_protected: 'keep.multiProtected',
  };

  function keeperReasonText(reason) {
    var key = KEEPER_REASON_KEYS[reason.code] || 'keep.added';
    var text = t(key, reason.code);
    if (reason.code === 'metadata' && reason.score !== undefined) {
      return text + '（' + reason.score + '）';
    }
    if (reason.code === 'formats' && reason.count !== undefined) {
      return text + '（' + reason.count + '）';
    }
    if (reason.code === 'size' && reason.bytes !== undefined) {
      return text + '（' + formatBytes(reason.bytes) + '）';
    }
    return text;
  }

  function escapeHtml(text) {
    return String(text === undefined || text === null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function openBook(bookId) {
    // 宿主 iframe 里没有导航通道（桥只有 resize/notify），所以新开标签页。
    // 从查重场景看这本来就是更好的交互：要对照着看"是不是同一本"。
    window.open('/book/' + bookId, '_blank', 'noopener,noreferrer');
  }

  // ---------------------------------------------------------------- 首屏

  function init() {
    cache();
    applyTheme((bridge && bridge.theme) || 'light');
    if (bridge && bridge.onThemeChange) bridge.onThemeChange(applyTheme);
    if (bridge && bridge.onLocaleChange) {
      bridge.onLocaleChange(function (next) { if (next) loadLocale(next); });
    }
    loadLocale((bridge && bridge.locale) || LOCALE);

    el['btn-scan'].addEventListener('click', startScan);
    el['btn-cancel'].addEventListener('click', cancelScan);
    el['filter-confidence'].addEventListener('change', function () {
      state.confidence = this.value; state.page = 0; loadGroups();
    });
    var keywordTimer = null;
    el['filter-keyword'].addEventListener('input', function () {
      var value = this.value;
      clearTimeout(keywordTimer);
      keywordTimer = setTimeout(function () {
        state.keyword = value.trim(); state.page = 0; loadGroups();
      }, 300);
    });
    el['btn-prev'].addEventListener('click', function () {
      if (state.page > 0) { state.page -= 1; loadGroups(); }
    });
    el['btn-next'].addEventListener('click', function () {
      var maxPage = Math.ceil(state.filteredTotal / state.pageSize) - 1;
      if (state.page < maxPage) { state.page += 1; loadGroups(); }
    });
    el['bench-close'].addEventListener('click', closeBench);
    el['btn-preview'].addEventListener('click', function () { openPlan(false); });
    el['btn-merge'].addEventListener('click', function () { openPlan(true); });
    el['plan-close'].addEventListener('click', closePlan);
    el['plan-cancel'].addEventListener('click', closePlan);
    el['btn-apply'].addEventListener('click', applyMerge);
    el['bench-overlay'].addEventListener('click', function (event) {
      if (event.target === el['bench-overlay']) closeBench();
    });
    el['plan-overlay'].addEventListener('click', function (event) {
      if (event.target === el['plan-overlay']) closePlan();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (!el['plan-overlay'].hidden) closePlan();
      else if (!el['bench-overlay'].hidden) closeBench();
    });

    loadScope();
    restore();
  }

  function scopeHintText() {
    if (!state.scopeData) return state.scopeMessage || '';
    return t('scope.total', '共 {n} 本书').replace('{n}', state.scopeData.total_books);
  }

  function renderScopeHint(data) {
    state.scopeData = data;
    state.scopeMessage = '';
    el['scope-hint'].textContent = scopeHintText();
  }

  function setScopeMessage(text) {
    state.scopeData = null;
    state.scopeMessage = text;
    el['scope-hint'].textContent = text;
  }

  function loadScope() {
    api('scope').then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        setScopeMessage(t('scope.failed', '读取书库信息失败'));
        return;
      }
      state.totalBooks = resp.data.total_books;
      state.threshold = Math.round((resp.data.threshold || 0.85) * 100);
      el.threshold.value = state.threshold;
      renderScopeHint(resp.data);
      el['btn-scan'].disabled = resp.data.total_books === 0;
    }).catch(function () {
      setScopeMessage(t('scope.failed', '读取书库信息失败'));
    });
  }

  function restore() {
    // 进页面问一次进度：宿主重启后内存任务没了，后端会回退到上次那份报告
    api('progress').then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        // task.not_found 是正常空态（从没跑过）；其它错误要明说，不然和"没跑过"长得一样
        if (resp && resp.err && resp.err !== 'task.not_found') {
          setScopeMessage(t('scope.restoreFailed', '读取上次结果失败')
            + '：' + (resp.msg || resp.err));
        }
        return;
      }
      var data = resp.data || {};
      if (data.status === 'running') {
        showRun(t('run.scanning', '正在查重…'));
        poll();
        return;
      }
      if (data.status === 'completed') {
        state.scan = data.progress_data && data.progress_data.summary;
        showRun(t('run.restored', '查重完成 · 上次查重结果'), true);
        finishRun(data.progress_data || {});
      }
    }).catch(function () { /* 静默：首屏失败不该弹错 */ });
  }

  // ---------------------------------------------------------------- 扫描

  function startScan() {
    var threshold = Number(el.threshold.value) || 85;
    if (threshold < 50 || threshold > 100) {
      notify(t('scope.thresholdRange', '阈值应在 50~100 之间'), 'warning');
      return;
    }
    el['btn-scan'].disabled = true;
    api('start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_ids: state.allIds || [], threshold: threshold / 100 }),
    }).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify((resp && resp.msg) || t('scope.startFailed', '启动失败'), 'error');
        el['btn-scan'].disabled = false;
        return;
      }
      state.threshold = threshold;
      showRun(t('run.scanning', '正在查重…'));
      poll();
    }).catch(function () {
      notify(t('scope.startFailed', '启动失败'), 'error');
      el['btn-scan'].disabled = false;
    });
  }

  function allIds() {
    // 空数组会让后端展开成全库（`StartHandler` 把空列表解释为"整个书库"）——
    // 前端不该也不需要把几万个 id 传到客户端再传回去。
    return state.allIds || [];
  }

  function cancelScan() {
    api('cancel', { method: 'POST' }).then(function (resp) {
      notify((resp && resp.msg) || t('run.cancelled', '已请求取消'),
             resp && resp.err === 'ok' ? 'info' : 'warning');
    });
  }

  var PHASE_KEYS = {
    load: 'phase.load',
    compare: 'phase.compare',
    done: 'phase.done',
    queued: 'phase.queued',
  };

  function showRun(label, restored) {
    el['run-card'].hidden = false;
    el['run-label'].textContent = label;
    el['btn-cancel'].hidden = !!restored;
    el['run-bar'].style.width = restored ? '100%' : '0%';
    if (restored) el['run-card'].classList.add('bd-run-done');
  }

  function poll() {
    clearTimeout(state.pollTimer);
    api('progress').then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        if (resp && resp.err === 'task.not_found') {
          el['btn-scan'].disabled = false;
          el['btn-cancel'].hidden = true;
          el['run-label'].textContent = t('run.idle', '没有正在进行的任务');
          return;
        }
        state.pollTimer = setTimeout(poll, 2000);
        return;
      }
      var data = resp.data || {};
      var pd = data.progress_data || {};
      el['run-bar'].style.width = (data.progress || 0) + '%';
      if (pd.total) {
        el['run-count'].textContent = pd.done + ' / ' + pd.total;
      }
      var phase = PHASE_KEYS[pd.phase];
      el['run-detail'].textContent = phase ? t(phase, pd.phase) : '';

      if (data.status === 'running') {
        state.pollTimer = setTimeout(poll, 700);
        return;
      }
      el['btn-cancel'].hidden = true;
      el['btn-scan'].disabled = false;
      if (data.status === 'failed') {
        el['run-label'].textContent = t('run.failed', '查重失败');
        el['run-detail'].textContent = data.error || '';
        notify(data.error || t('run.failed', '查重失败'), 'error');
        return;
      }
      finishRun(pd);
    }).catch(function () {
      state.pollTimer = setTimeout(poll, 2000);
    });
  }

  function finishRun(progressData) {
    state.scan = (progressData && progressData.summary) || null;
    el['run-card'].classList.add('bd-run-done');
    el['run-label'].textContent = t('run.restored', '查重完成 · 上次查重结果');
    if (state.scan) renderSummary(state.scan);
    el['summary-card'].hidden = false;
    loadGroups();
  }

  function renderSummary(summary) {
    var items = [
      ['summary.groups', '重复分组', summary.group_count],
      ['summary.extra', '多余副本', summary.extra_copies],
      ['summary.reclaimable', '可回收空间', formatBytes(summary.reclaimable_bytes)],
      ['summary.waste', '同格式重占', formatBytes(summary.disk_waste_bytes)],
    ];
    el.summary.innerHTML = items.map(function (item) {
      return '<div class="bd-stat"><span class="bd-stat-value">' + escapeHtml(item[2]) +
        '</span><span class="bd-stat-label">' + escapeHtml(t(item[0], item[1])) +
        '</span></div>';
    }).join('');
  }

  // ---------------------------------------------------------------- 列表

  function loadGroups() {
    var query = ['page=' + state.page, 'size=' + state.pageSize];
    if (state.confidence) query.push('confidence=' + encodeURIComponent(state.confidence));
    if (state.keyword) query.push('keyword=' + encodeURIComponent(state.keyword));
    api('groups?' + query.join('&')).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        el['list-meta'].textContent = (resp && resp.msg) || t('list.failed', '读取列表失败');
        el.groups.innerHTML = '';
        return;
      }
      var data = resp.data;
      state.groups = data.groups || [];
      state.filteredTotal = data.filtered_total || 0;
      state.mergedIds = data.merged_ids || [];
      state.removedTitles = data.removed_titles || [];
      if (data.summary) renderSummary(data.summary);
      renderList(data);
      renderHandled();
    }).catch(function () {
      el['list-meta'].textContent = t('list.failed', '读取列表失败');
    });
  }

  function renderList(data) {
    if (data && data.generated_at) state.generatedAt = data.generated_at;
    el['list-meta'].textContent = t('list.meta', '显示 {shown} / {total} 组 · 生成于 {at}')
      .replace('{shown}', state.groups.length)
      .replace('{total}', state.filteredTotal)
      .replace('{at}', data.generated_at || '—');

    var active = state.active;
    el.groups.innerHTML = state.groups.map(function (group) {
      var expanded = active && active.index === group.index;
      return rowHtml(group, expanded);
    }).join('') + (expandedHtml(active));

    el['pager'].hidden = state.filteredTotal <= state.pageSize;
    var maxPage = Math.max(0, Math.ceil(state.filteredTotal / state.pageSize) - 1);
    el['pager-label'].textContent = (state.page + 1) + ' / ' + (maxPage + 1);
    el['btn-prev'].disabled = state.page === 0;
    el['btn-next'].disabled = state.page >= maxPage;

    var empty = state.filteredTotal === 0;
    el['empty-state'].hidden = !empty;
    if (empty) {
      el['empty-state'].textContent = (state.confidence || state.keyword)
        ? t('list.emptyFiltered', '当前筛选条件下没有结果')
        : t('list.emptyClean', '没有发现重复书籍');
    }

    el.groups.querySelectorAll('[data-open]').forEach(function (node) {
      node.addEventListener('click', function () { openBench(Number(node.getAttribute('data-open'))); });
    });
    el.groups.querySelectorAll('[data-pick]').forEach(function (node) {
      node.addEventListener('click', function (event) {
        event.stopPropagation();
        pickKeeper(Number(node.getAttribute('data-pick')));
      });
    });
    el.groups.querySelectorAll('[data-jump]').forEach(function (node) {
      node.addEventListener('click', function (event) {
        event.stopPropagation();
        openBook(Number(node.getAttribute('data-jump')));
      });
    });
  }

  function rowHtml(group, expanded) {
    var members = group.members_preview;
    var title = expanded && state.active && state.active.group
      ? state.active.group.members.map(function (m) { return m.title; }).join(' | ')
      : t('list.groupTitle', '{n} 本可能是同一本书').replace('{n}', group.member_count);
    return '<article class="bd-group' + (expanded ? ' bd-group-open' : '') + '">' +
      '<button class="bd-group-head" data-open="' + group.index + '">' +
        '<span class="bd-chip bd-chip-' + escapeHtml(group.confidence) + '">' +
          escapeHtml(t(CONFIDENCE_KEYS[group.confidence], group.confidence)) + '</span>' +
        '<span class="bd-group-title">' + escapeHtml(title) + '</span>' +
        '<span class="bd-group-meta">' +
          escapeHtml(formatBytes(group.reclaimable_bytes)) + ' · ' +
          escapeHtml(t('list.sameFormat', '同格式 {v}').replace('{v}', formatBytes(group.disk_waste_bytes))) +
        '</span>' +
      '</button>' +
      (expanded ? '' : '<div class="bd-group-preview" hidden></div>') +
      '</article>';
  }

  function expandedHtml(active) {
    if (!active || !active.group) return '';
    return '<section class="bd-bench-inline">' + benchBodyHtml(active.group, active.keeperId) + '</section>';
  }

  function benchBodyHtml(group, keeperId) {
    var members = group.members || [];
    var recommendation = group.recommendation || {};
    var keeper = keeperId || recommendation.keeper_id;
    var reasons = (recommendation.reasons || []).map(keeperReasonText).join('、');

    var head = '<p class="bd-hint">' + escapeHtml(t('bench.recommend', '推荐保留：{title}'))
      .replace('{title}', titleOf(members, keeper)) +
      (reasons ? ' · ' + escapeHtml(reasons) : '') + '</p>';

    var cards = members.map(function (member) {
      var isKeeper = member.id === keeper;
      return '<div class="bd-copy' + (isKeeper ? ' bd-copy-keeper' : '') + '">' +
        '<div class="bd-copy-head">' +
          '<span class="bd-copy-title">' + escapeHtml(member.title) + '</span>' +
          (isKeeper ? '<span class="bd-chip bd-chip-keep">' + escapeHtml(t('bench.keep', '保留')) + '</span>' : '') +
        '</div>' +
        '<p class="bd-copy-authors">' + escapeHtml((member.authors || []).join('、') || '—') + '</p>' +
        '<p class="bd-copy-meta">' + escapeHtml(t('bench.id', 'ID {id}').replace('{id}', member.id)) +
          ' · ' + escapeHtml(formatBytes(member.size)) + '</p>' +
        '<p class="bd-copy-formats">' + escapeHtml((member.formats || []).join('、') || '—') + '</p>' +
        '<div class="bd-copy-actions">' +
          '<button class="bd-btn bd-btn-small" data-jump="' + member.id + '">' +
            escapeHtml(t('bench.open', '打开书籍页')) + '</button>' +
          '<button class="bd-btn bd-btn-small' + (isKeeper ? ' bd-btn-primary' : '') +
            '" data-pick="' + member.id + '">' +
            escapeHtml(isKeeper ? t('bench.picked', '已选为保留') : t('bench.pick', '保留这本')) +
          '</button>' +
        '</div>' +
      '</div>';
    }).join('');

    var table = diffTableHtml(group.diff, members, keeper);
    return head + '<div class="bd-copies">' + cards + '</div>' + table;
  }

  function titleOf(members, bookId) {
    for (var i = 0; i < members.length; i += 1) {
      if (members[i].id === bookId) return members[i].title;
    }
    return '—';
  }

  function diffTableHtml(diff, members, keeper) {
    diff = diff || { rows: [], summary: '' };
    var head = '<tr><th>' + escapeHtml(t('diff.field', '对比项')) + '</th>' +
      members.map(function (member) {
        var mark = member.id === keeper ? ' class="bd-th-keep"' : '';
        return '<th' + mark + '>' + escapeHtml(member.title) + '</th>';
      }).join('') + '</tr>';
    var rows = (diff.rows || []).map(function (row) {
      return '<tr><td class="bd-td-field">' + escapeHtml(row.label) + '</td>' +
        (row.cells || []).map(function (cell) {
          var best = row.best_id && row.best_id === cell.book_id ? ' bd-td-best' : '';
          return '<td class="' + best.trim() + '">' + escapeHtml(cell.value) + '</td>';
        }).join('') + '</tr>';
    }).join('');

    var summary = diff.summary
      ? '<p class="bd-hint">' + escapeHtml(diff.summary) + '</p>'
      : '';
    var body = rows
      ? '<table class="bd-diff"><thead>' + head + '</thead><tbody>' + rows + '</tbody></table>'
      : '<p class="bd-hint">' + escapeHtml(t('diff.allSame', '两份的元数据与条目属性完全相同')) + '</p>';
    return '<div class="bd-diff-wrap">' + summary + body + '</div>';
  }

  function renderHandled() {
    var removed = state.removedTitles || [];
    el['handled-card'].hidden = removed.length === 0;
    if (!removed.length) return;
    el['handled-list'].innerHTML = removed.map(function (item) {
      return '<li>' + escapeHtml(t('handled.item', '《{title}》已并入《{into}》')
        .replace('{title}', item.title).replace('{into}', item.into)) +
        '<span class="bd-hint"> · ' + escapeHtml(item.at || '') + '</span></li>';
    }).join('');
  }

  // ---------------------------------------------------------------- 对照台

  function openBench(index) {
    api('group?index=' + index).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify((resp && resp.msg) || t('bench.failed', '读取分组失败'), 'error');
        return;
      }
      var group = resp.data.group;
      state.active = {
        index: index,
        group: group,
        keeperId: (group.recommendation || {}).keeper_id,
      };
      el['bench-title'].textContent = t('bench.title', '重复项对照');
      el['bench-body'].innerHTML = benchBodyHtml(group, state.active.keeperId);
      el['bench-hint'].textContent = t('bench.hint',
        '删除源记录会连带丢失它的收藏/在读/进度/评分/书单，请先确认要保留哪一本。');
      el['bench-overlay'].hidden = false;
      bindBench();
      // 同时在列表里展开，关闭后位置不跳
      state.page = Math.floor(index / state.pageSize);
      loadGroups();
    });
  }

  function bindBench() {
    var root = el['bench-body'];
    root.querySelectorAll('[data-pick]').forEach(function (node) {
      node.addEventListener('click', function () {
        state.active.keeperId = Number(node.getAttribute('data-pick'));
        el['bench-body'].innerHTML = benchBodyHtml(state.active.group, state.active.keeperId);
        bindBench();
      });
    });
    root.querySelectorAll('[data-jump]').forEach(function (node) {
      node.addEventListener('click', function () {
        openBook(Number(node.getAttribute('data-jump')));
      });
    });
  }

  function pickKeeper(bookId) {
    if (!state.active) return;
    state.active.keeperId = bookId;
    var root = el['bench-body'];
    if (root) { root.innerHTML = benchBodyHtml(state.active.group, bookId); bindBench(); }
    loadGroups();
  }

  function closeBench() {
    el['bench-overlay'].hidden = true;
    state.active = null;
    loadGroups();
  }

  // ---------------------------------------------------------------- 合并

  function openPlan(execute) {
    if (!state.active) return;
    var payload = {
      index: state.active.index,
      keeper_id: state.active.keeperId,
      keep_rule: 'metadata',
    };
    api('merge_plan?index=' + payload.index +
        '&keeper_id=' + payload.keeper_id).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify((resp && resp.msg) || t('plan.failed', '生成合并预览失败'), 'error');
        return;
      }
      state.plan = resp.data;
      renderPlan(state.plan);
      el['plan-overlay'].hidden = false;
      if (execute) notify(t('plan.hint', '请确认后再执行'), 'info');
    });
  }

  function renderPlan(plan) {
    var warnings = [
      t('plan.warnDrop', '同名格式不会被复制：源书的同名文件会被丢弃，留下的是保留项的那一份。'),
      t('plan.warnMigrate', '源记录的收藏/在读/阅读进度/评分/书单不会被迁移（工具箱删除不清理关联数据）。'),
    ];
    var steps = (plan.steps || []).map(function (step) {
      return '<li>' +
        escapeHtml(t('plan.step', '《{src}》(#{id}) → 并入《{dst}》')
          .replace('{src}', step.source_title)
          .replace('{id}', step.source_id)
          .replace('{dst}', plan.keeper_title)) +
        '<ul class="bd-plan-formats">' +
          '<li>' + escapeHtml(t('plan.moved', '会复制过去：')) +
            escapeHtml((step.moved_formats || []).join('、') || t('plan.none', '无')) + '</li>' +
          '<li class="bd-plan-drop">' + escapeHtml(t('plan.dropped', '会被丢弃：')) +
            escapeHtml((step.dropped_formats || []).join('、') || t('plan.none', '无')) + '</li>' +
        '</ul></li>';
    }).join('');

    el['plan-title'].textContent = t('plan.title', '合并预览');
    el['plan-body'].innerHTML =
      '<p class="bd-plan-head">' + escapeHtml(t('plan.keep', '保留：{title}')
        .replace('{title}', plan.keeper_title)) + '</p>' +
      '<ul class="bd-plan-steps">' + steps + '</ul>' +
      '<div class="bd-plan-stats">' +
        '<span>' + escapeHtml(t('plan.movedTotal', '复制格式 {n} 个').replace('{n}', plan.moved_total)) + '</span>' +
        '<span>' + escapeHtml(t('plan.droppedTotal', '丢弃同格式 {n} 个').replace('{n}', plan.dropped_total)) + '</span>' +
        '<span>' + escapeHtml(t('plan.reclaim', '可回收 {v}').replace('{v}', formatBytes(plan.reclaimable_bytes))) + '</span>' +
      '</div>' +
      '<ul class="bd-warnings">' + warnings.map(function (text) {
        return '<li>' + escapeHtml(text) + '</li>';
      }).join('') + '</ul>';
    el['plan-hint'].textContent = '';
  }

  function closePlan() {
    el['plan-overlay'].hidden = true;
    el['btn-apply'].disabled = false;
  }

  function applyMerge() {
    if (!state.plan) return;
    el['btn-apply'].disabled = true;
    api('merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        index: state.plan.index,
        keeper_id: state.plan.keeper_id,
        delete_source: el['delete-source'].checked,
      }),
    }).then(function (resp) {
      el['btn-apply'].disabled = false;
      if (!resp || resp.err !== 'ok') {
        notify((resp && resp.msg) || t('plan.applyFailed', '合并失败'), 'error');
        return;
      }
      var data = resp.data || {};
      // 兜底：字段缺失时显示 0，而不是把 undefined 甩到用户脸上
      notify(t('plan.applied', '已合并：复制 {m} 个格式，删除 {d} 条重复记录')
        .replace('{m}', data.moved_total || 0)
        .replace('{d}', (data.removed_ids || []).length),
        'success');
      closePlan();
      closeBench();
    }).catch(function () {
      el['btn-apply'].disabled = false;
      notify(t('plan.applyFailed', '合并失败'), 'error');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());