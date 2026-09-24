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
    signature: '',       // 当前这份报告的身份（生成时间+组数）；变了就丢掉按序号缓存的状态
    scan: null,          // 最近一次扫描的汇总
    page: 0,
    pageSize: 50,
    confidence: '',
    keyword: '',
    groups: [],
    filteredTotal: 0,
    removedTitles: [],
    deletedTitles: [],
    failedTitles: [],
    ignored: [],         // 已忽略的配对（不是重复）——可撤销
    active: null,        // 当前展开的分组 {index, group, keeperId, selected}
    groupCache: {},      // 组序号 → 详情（点过的行缓存下来，再点不请求）
    plan: null,          // 当前展开的合并预览（属于 active 那一组）
    confirmDelete: null, // 正在确认删除的那本书 id（行内确认条）
    pollTimer: null,
  };

  var el = {};

  function cache() {
    [
      'app', 'scope-hint', 'threshold', 'btn-scan', 'btn-cancel',
      'run-card', 'run-label', 'run-count', 'run-bar', 'run-detail',
      'summary-card', 'summary', 'filter-confidence', 'filter-keyword',
      'list-meta', 'groups', 'pager', 'pager-label', 'btn-prev', 'btn-next',
      'empty-state', 'handled-card', 'handled-list', 'toasts',
      'ignored-card', 'ignored-list', 'ignored-count', 'btn-unignore-all',
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
    // 列表行与行内抽屉都是拼出来的字符串，重画一次即跟上新语言
    if (state.groups && state.groups.length) {
      renderList({ generated_at: state.generatedAt });
    }
    renderHandled();
    renderIgnored();
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
    var detail = null;
    if (reason.code === 'metadata' && reason.score !== undefined) detail = reason.score;
    if (reason.code === 'formats' && reason.count !== undefined) detail = reason.count;
    if (reason.code === 'size' && reason.bytes !== undefined) detail = formatBytes(reason.bytes);
    if (detail === null) return text;
    // 括号由文案提供：中文用全角、英文用半角（原来写死全角，英文下会显示
    // "Most formats（2）"）
    return t('keep.withValue', '{text}（{value}）')
      .replace('{text}', text).replace('{value}', detail);
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
    // ESC 收起当前展开的那一行（没有浮层可关了）
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && state.active) collapseRow();
    });

    el['btn-unignore-all'].addEventListener('click', function () { unignore(null, true); });

    loadScope();
    loadIgnored();
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
    // "因卷册序号排除"只在真的排除了才显示——它解释"为什么这套书没被报出来"
    if (summary.serial_excluded) {
      items.push(['summary.serialExcluded', '已排除卷册序号', summary.serial_excluded]);
    }
    // 同理：实体书与电子书不互相比较，排除了多少对要说出来（不是静默收紧）
    if (summary.cross_type_excluded) {
      items.push(['summary.crossTypeExcluded', '已排除实体/电子书交叉',
                  summary.cross_type_excluded]);
    }
    // 以及被忽略名单挡掉的候选对（用户自己点的，更要说清楚）
    if (summary.ignored_excluded) {
      items.push(['summary.ignoredExcluded', '已忽略的配对', summary.ignored_excluded]);
    }
    el.summary.innerHTML = items.map(function (item) {
      return '<div class="bd-stat"><span class="bd-stat-value">' + escapeHtml(item[2]) +
        '</span><span class="bd-stat-label">' + escapeHtml(t(item[0], item[1])) +
        '</span></div>';
    }).join('');
  }

  // ---------------------------------------------------------------- 列表

  /** 结果换了（`signature` = 生成时间 + 组数）就丢掉与"这一份报告"绑定的状态。

  缓存的是**组序号 → 详情**，而序号只是报告里的位置：同一序号在新报告里是另一组。
  以前重扫之后点之前点过的行，命中的是旧缓存（`toggleRow` 直接渲染、不再请求），
  于是行标题是新的、抽屉内容是旧的。空 signature（旧报告/索引缺失）按"不知道"处理，
  不清缓存——宁可多留一次缓存，也不要因为字段缺失把用户正开着的那一行抖掉。
  */
  function dropStaleResult(signature) {
    if (!signature || signature === state.signature) return false;
    var changed = !!state.signature;
    state.signature = signature;
    if (!changed) return true;          // 第一次拿到结果：只记下来
    state.groupCache = {};
    state.active = null;
    state.plan = null;
    state.confirmDelete = null;
    return true;
  }

  function loadGroups() {
    var query = ['page=' + state.page, 'size=' + state.pageSize];
    if (state.confidence) query.push('confidence=' + encodeURIComponent(state.confidence));
    if (state.keyword) query.push('keyword=' + encodeURIComponent(state.keyword));
    return api('groups?' + query.join('&')).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        el['list-meta'].textContent = (resp && resp.msg) || t('list.failed', '读取列表失败');
        el.groups.innerHTML = '';
        return;
      }
      var data = resp.data;
      // **换了一份结果就必须把按"组序号"缓存的东西清掉**：组序号是报告里的位置，
      // 新报告里同一个序号是另一组。不清的话，重新查重之后点一个之前点过的行，
      // 行标题是新的、抽屉里的成员与对照表却来自上一次扫描（后端一直在回
      // `signature` 就是为了这个，只是以前没人读它）。
      dropStaleResult(data.signature);
      state.groups = data.groups || [];
      state.filteredTotal = data.filtered_total || 0;
      state.removedTitles = data.removed_titles || [];
      state.deletedTitles = data.deleted_titles || [];
      state.failedTitles = data.failed_titles || [];
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
    // 展开的抽屉就渲染在**对应的那一行之内**（原来是 `+ expandedHtml(active)` 追加到整页末尾，
    // 再叠一个居中浮层——真机反馈"报告总是在最中间显示"就是那个浮层）
    el.groups.innerHTML = state.groups.map(function (group) {
      return rowHtml(group, !!(active && active.index === group.index));
    }).join('');

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

    bindRows();
    bindDrawer();
  }

  function bindRows() {
    el.groups.querySelectorAll('[data-open]').forEach(function (node) {
      node.addEventListener('click', function () {
        toggleRow(Number(node.getAttribute('data-open')));
      });
    });
  }

  function rowHtml(group, open) {
    return '<article class="bd-group' + (open ? ' bd-group-open' : '') +
        '" data-group="' + group.index + '">' +
      '<button class="bd-group-head" data-open="' + group.index + '">' +
        '<span class="bd-chip bd-chip-' + escapeHtml(group.confidence) + '">' +
          escapeHtml(t(CONFIDENCE_KEYS[group.confidence], group.confidence)) + '</span>' +
        (group.stale_count
          ? '<span class="bd-chip bd-chip-stale">' +
            escapeHtml(t('list.staleHandled', '已处理 {n} 本')
              .replace('{n}', group.stale_count)) + '</span>'
          : '') +
        '<span class="bd-group-title">' + groupTitleHtml(group) + '</span>' +
        '<span class="bd-group-meta">' +
          escapeHtml(formatBytes(group.reclaimable_bytes)) + ' · ' +
          escapeHtml(t('list.sameFormat', '同格式 {v}').replace('{v}', formatBytes(group.disk_waste_bytes))) +
        '</span>' +
        '<span class="bd-group-caret" aria-hidden="true">' + (open ? '▾' : '▸') + '</span>' +
      '</button>' +
      (open ? drawerHtml(state.active, state.plan) : '') +
      '</article>';
  }

  /** 行标题：直接列出成员书名，而不是"x 本可能是同一本书"。 */
  function groupTitleHtml(group) {
    var titles = group.titles || [];
    if (!titles.length) {
      // 旧报告（写它的时候索引里还没有 titles）→ 回落旧文案，升级不该让列表变空
      return escapeHtml(t('list.groupTitle', '{n} 本可能是同一本书')
        .replace('{n}', group.member_count));
    }
    var separator = t('list.titleJoin', '、');
    var text = titles.map(function (title) { return '《' + title + '》'; }).join(separator);
    if (group.preview_truncated) {
      text += ' ' + t('list.membersMore', '等 {n} 本').replace('{n}', group.member_count);
    }
    var html = escapeHtml(text);
    // 作者只在预览的几本**同属一人**时才显示——否则一列两个作者反而更难读
    var authors = group.authors || [];
    var sameAuthor = authors.length > 0 && authors.every(function (name) {
      return name && name === authors[0];
    });
    if (sameAuthor) {
      html += '<span class="bd-group-author">' + escapeHtml(authors[0]) + '</span>';
    }
    return html;
  }

  /** 抽屉（详情）——就在这一行之内。 */
  function drawerHtml(active, plan) {
    if (!active || !active.group) return '';
    var inner = benchBodyHtml(active.group, active.keeperId);
    if (plan && plan.index === active.index) inner += planHtml(plan);
    return '<section class="bd-drawer">' + inner + '</section>';
  }

  // ---------------------------------------------------------------- 展开 / 收起

  function toggleRow(index) {
    if (state.active && state.active.index === index) {
      collapseRow();
      return;
    }
    state.plan = null;                       // 换了一组，上一组的合并预览/删除确认都作废
    state.confirmDelete = null;
    var cached = state.groupCache[index];
    if (cached) {
      setActive(index, cached);
      return;
    }
    setRowLoading(index, true);
    api('group?index=' + index).then(function (resp) {
      setRowLoading(index, false);
      if (!resp || resp.err !== 'ok') {
        notify((resp && resp.msg) || t('bench.failed', '读取分组失败'), 'error');
        return;
      }
      state.groupCache[index] = resp.data.group;
      setActive(index, resp.data.group);
    }).catch(function () {
      setRowLoading(index, false);
      notify(t('bench.failed', '读取分组失败'), 'error');
    });
  }

  function setActive(index, group) {
    var keeperId = (group.recommendation || {}).keeper_id;
    state.active = {
      index: index,
      group: group,
      keeperId: keeperId,
      selected: defaultSelection(group, keeperId),
    };
    refreshDrawer();
  }

  /** 默认勾选：除保留项外**全部**勾上（保留项是合并的目标，不是要被合并掉的那一本）。 */
  function defaultSelection(group, keeperId) {
    var selected = {};
    (group.members || []).forEach(function (member) {
      selected[member.id] = member.id !== keeperId;
    });
    return selected;
  }

  /** 当前勾选"要合并掉"的 id（不含保留项，且只取还在成员里的）。 */
  function selectedIds() {
    if (!state.active) return [];
    var keeperId = state.active.keeperId;
    var picked = state.active.selected || {};
    return (state.active.group.members || []).filter(function (member) {
      return member.id !== keeperId && picked[member.id];
    }).map(function (member) { return member.id; });
  }

  /** 勾选/保留项变了 → 已生成的预览作废（它的步骤与数字都是按旧选择算的）。 */
  function invalidatePlan() {
    state.plan = null;
    state.confirmDelete = null;
  }

  function toggleSelected(bookId, checked) {
    if (!state.active) return;
    state.active.selected[bookId] = !!checked;
    invalidatePlan();
    refreshDrawer();
  }

  function selectAll(checked) {
    if (!state.active) return;
    var keeperId = state.active.keeperId;
    (state.active.group.members || []).forEach(function (member) {
      state.active.selected[member.id] = member.id !== keeperId && !!checked;
    });
    invalidatePlan();
    refreshDrawer();
  }

  function collapseRow() {
    state.active = null;
    state.plan = null;
    state.confirmDelete = null;
    refreshDrawer();
  }

  function pickKeeper(bookId) {
    if (!state.active) return;
    var previous = state.active.keeperId;
    state.active.keeperId = bookId;
    // 保留项不是"要合并掉的那一本"：新保留项从勾选集里摘掉，被换下来的那一本
    // 重新变回候补就补勾——否则默认全勾的用户换一次保留项，会发现老保留项被留在外面
    state.active.selected[bookId] = false;
    if (previous && previous !== bookId) state.active.selected[previous] = true;
    invalidatePlan();
    refreshDrawer();
  }

  function setRowLoading(index, loading) {
    var row = el.groups.querySelector('.bd-group[data-group="' + index + '"]');
    if (row) row.classList.toggle('bd-group-loading', !!loading);
  }

  /** 只重画抽屉：改保留项、开关合并预览都走这里。
   *
   * **不重画整个列表**——全量重画会把滚动位置重置掉，正在看的那一行会跳走
   * （原来 `pickKeeper()` 里调 `loadGroups()` 就有这个毛病）。
   */
  function refreshDrawer() {
    if (!el.groups) return;
    var drawer = el.groups.querySelector('.bd-drawer');
    var html = drawerHtml(state.active, state.plan);
    if (drawer) {
      if (html) {
        drawer.outerHTML = html;             // 就地替换
      } else {
        drawer.remove();
      }
    } else if (html) {
      // 抽屉还没建出来（首次展开）→ 交给整表重画，它会在该行之内建好
      renderList({ generated_at: state.generatedAt });
      return;
    }
    // 行头的展开态跟着 state.active 走
    el.groups.querySelectorAll('.bd-group[data-group]').forEach(function (row) {
      var open = !!(state.active &&
        String(state.active.index) === row.getAttribute('data-group'));
      row.classList.toggle('bd-group-open', open);
      var caret = row.querySelector('.bd-group-caret');
      if (caret) caret.textContent = open ? '▾' : '▸';
    });
    bindDrawer();
  }

  function bindDrawer() {
    var drawer = el.groups.querySelector('.bd-drawer');
    if (!drawer) return;
    drawer.querySelectorAll('[data-pick]').forEach(function (node) {
      node.addEventListener('click', function () {
        pickKeeper(Number(node.getAttribute('data-pick')));
      });
    });
    drawer.querySelectorAll('[data-src]').forEach(function (node) {
      node.addEventListener('change', function () {
        toggleSelected(Number(node.getAttribute('data-src')), node.checked);
      });
    });
    var pickAll = drawer.querySelector('[data-select-all]');
    if (pickAll) pickAll.addEventListener('click', function () { selectAll(true); });
    var pickNone = drawer.querySelector('[data-select-none]');
    if (pickNone) pickNone.addEventListener('click', function () { selectAll(false); });
    drawer.querySelectorAll('[data-jump]').forEach(function (node) {
      node.addEventListener('click', function () {
        openBook(Number(node.getAttribute('data-jump')));
      });
    });
    var preview = drawer.querySelector('[data-preview]');
    if (preview) preview.addEventListener('click', openPlan);
    var collapse = drawer.querySelector('[data-collapse]');
    if (collapse) collapse.addEventListener('click', collapseRow);
    var apply = drawer.querySelector('[data-apply]');
    if (apply) apply.addEventListener('click', applyMerge);
    var ignore = drawer.querySelector('[data-ignore]');
    if (ignore) ignore.addEventListener('click', ignoreGroup);
    var cancel = drawer.querySelector('[data-plan-cancel]');
    if (cancel) cancel.addEventListener('click', function () {
      state.plan = null;
      refreshDrawer();
    });
    // 单本书删除：先就地展开确认条，再执行（不用弹层）
    drawer.querySelectorAll('[data-del]').forEach(function (node) {
      node.addEventListener('click', function () {
        var id = Number(node.getAttribute('data-del'));
        state.confirmDelete = (state.confirmDelete === id) ? null : id;
        refreshDrawer();
      });
    });
    drawer.querySelectorAll('[data-del-no]').forEach(function (node) {
      node.addEventListener('click', function () {
        state.confirmDelete = null;
        refreshDrawer();
      });
    });
    var delYes = drawer.querySelector('[data-del-yes]');
    if (delYes) {
      delYes.addEventListener('click', function () {
        deleteBook(Number(delYes.getAttribute('data-del-yes')));
      });
    }
  }

  /** 删除一本重复书（第二处写操作；后端仍会校验它是不是这一组的成员）。 */
  function deleteBook(bookId) {
    if (!state.active) return;
    var index = state.active.index;
    var button = el.groups.querySelector('[data-del-yes]');
    if (button) button.disabled = true;
    api('delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index: index, book_id: bookId }),
    }).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        if (button) button.disabled = false;
        notify((resp && resp.msg) || t('del.failed', '删除失败'), 'error');
        return;
      }
      var data = resp.data || {};
      notify(t('del.applied', '已删除《{title}》').replace('{title}', data.title || ''), 'success');
      // 这一组的成员组成变了（可能已不足两本）→ 丢掉缓存并整表刷新，由后端决定它还显不显示
      state.confirmDelete = null;
      state.active = null;
      state.plan = null;
      delete state.groupCache[index];
      loadGroups();
    }).catch(function () {
      if (button) button.disabled = false;
      notify(t('del.failed', '删除失败'), 'error');
    });
  }

  function benchBodyHtml(group, keeperId) {
    var members = group.members || [];
    var recommendation = group.recommendation || {};
    var keeper = keeperId || recommendation.keeper_id;
    // 理由只对"工具自动推荐的那本"成立。用户改选了别的书之后，原来那串理由
    // （"格式最多（2）"之类）其实是按另一本算的，照抄会变成假信息——改成如实说是手动选的。
    var manual = !!(keeperId && recommendation.keeper_id && keeperId !== recommendation.keeper_id);
    // 两个分支都写成字面量 `t('键', 兜底)`：用变量拼键会让"用到的键"静态对账不出来
    // （前端契约测试专门盯着这一点），也让这里看不出有哪几条文案。
    var label = manual
      ? t('bench.yourPick', '保留这本：{title}')
      : t('bench.recommend', '推荐保留：{title}');
    var reasonText = manual
      ? t('keep.manual', '你手动选择')
      : (recommendation.reasons || []).map(keeperReasonText).join('、');

    // 书名先替换进模板、整体再转义一次（与 plan.step / handled.item 一致）
    var head = '<p class="bd-hint">' +
      escapeHtml(label.replace('{title}', titleOf(members, keeper))) +
      (reasonText ? ' · ' + escapeHtml(reasonText) : '') + '</p>';

    var chosen = selectedIds().length;
    var pickRow = '<div class="bd-select-all">' +
      '<span class="bd-hint' + (chosen ? '' : ' bd-warn-hint') + '">' +
        escapeHtml(chosen
          ? t('bench.selectedCount', '已勾选 {n} 本将并入保留项（没勾的原样保留）')
              .replace('{n}', chosen)
          : t('bench.pickNoneHint', '还没勾选要合并的书：没勾的就原样保留，不会被动')) +
      '</span>' +
      '<button class="bd-btn bd-btn-small" data-select-all="1">' +
        escapeHtml(t('bench.selectAll', '全选')) + '</button>' +
      '<button class="bd-btn bd-btn-small" data-select-none="1">' +
        escapeHtml(t('bench.selectNone', '全不选')) + '</button>' +
    '</div>';

    var cards = members.map(function (member) {
      var isKeeper = member.id === keeper;
      var confirming = state.confirmDelete === member.id;
      var picked = !isKeeper && !!(state.active && state.active.selected[member.id]);
      // 保留项那张卡没有复选框：它是合并的目标，没法"被合并掉"
      var pick = isKeeper ? '' :
        '<label class="bd-copy-pick">' +
          '<input type="checkbox" data-src="' + member.id + '"' +
            (picked ? ' checked' : '') + '>' +
          '<span>' + escapeHtml(t('bench.pickMerge', '一并合并')) + '</span>' +
        '</label>';
      return '<div class="bd-copy' + (isKeeper ? ' bd-copy-keeper' : '') +
          (picked ? ' bd-copy-on' : '') + '">' +
        pick +
        '<div class="bd-copy-head">' +
          '<span class="bd-copy-title">' + escapeHtml(member.title) + '</span>' +
          (isKeeper ? '<span class="bd-chip bd-chip-keep">' + escapeHtml(t('bench.keep', '保留')) + '</span>' : '') +
        '</div>' +
        '<p class="bd-copy-authors">' + escapeHtml((member.authors || []).join('、') || '—') + '</p>' +
        '<p class="bd-copy-meta">' + escapeHtml(t('bench.id', 'ID {id}').replace('{id}', member.id)) +
          ' · ' + escapeHtml(formatBytes(member.size)) + '</p>' +
        '<p class="bd-copy-formats">' + escapeHtml((member.formats || []).join('、') || '—') + '</p>' +
        (confirming ? deleteConfirmHtml(member) : '') +
        '<div class="bd-copy-actions">' +
          '<button class="bd-btn bd-btn-small" data-jump="' + member.id + '">' +
            escapeHtml(t('bench.open', '打开书籍页')) + '</button>' +
          '<button class="bd-btn bd-btn-small' + (isKeeper ? ' bd-btn-primary' : '') +
            '" data-pick="' + member.id + '">' +
            escapeHtml(isKeeper ? t('bench.picked', '已选为保留') : t('bench.pick', '保留这本')) +
          '</button>' +
          '<button class="bd-btn bd-btn-small bd-btn-danger" data-del="' + member.id + '">' +
            escapeHtml(t('del.button', '删除')) + '</button>' +
        '</div>' +
      '</div>';
    }).join('');

    var table = diffTableHtml(group.diff, members, keeper);
    // 动作按钮与安全提示都搬进抽屉：没有浮层了，合并预览也在同一块里就地展开
    var actions =
      '<div class="bd-drawer-actions">' +
        '<button class="bd-btn bd-btn-primary" data-preview="1"' +
          (chosen ? '' : ' disabled') + '>' +
          escapeHtml(t('bench.preview', '先看合并预览')) + '</button>' +
        // 忽略是**可撤销**的（「已忽略」卡片里能撤），所以不加二次确认
        '<button class="bd-btn" data-ignore="1">' +
          escapeHtml(t('bench.ignore', '不是重复（忽略这组）')) + '</button>' +
        '<button class="bd-btn" data-collapse="1">' +
          escapeHtml(t('bench.collapse', '收起')) + '</button>' +
      '</div>' +
      '<p class="bd-hint">' + escapeHtml(t('bench.ignoreHint',
        '忽略只是"以后别再报出来"，不动书库、也不删记录；随时可以在「已忽略」里撤销。')) + '</p>' +
      (group.ignored_pairs
        ? '<p class="bd-hint bd-warn-hint">' +
          escapeHtml(t('bench.ignoredPart', '本组有 {n} 对已被忽略：重新查重后会拆开。')
            .replace('{n}', group.ignored_pairs)) + '</p>'
        : '') +
      '<p class="bd-hint bd-warn-hint">' + escapeHtml(t('bench.hint',
        '删除源记录会连带失去它的收藏/在读/阅读进度/评分/书单，请先确认要保留哪一本。')) + '</p>';
    return head + pickRow + '<div class="bd-copies">' + cards + '</div>' + table + actions;
  }

  function titleOf(members, bookId) {
    for (var i = 0; i < members.length; i += 1) {
      if (members[i].id === bookId) return members[i].title;
    }
    return '—';
  }

  /** 单本书的删除确认条（行内，不用弹层——与 0.1.2 的"详情不许居中浮层"一致）。 */
  function deleteConfirmHtml(member) {
    return '<div class="bd-confirm">' +
      '<p class="bd-confirm-title">' + escapeHtml(t('del.title', '删除《{title}》？')
        .replace('{title}', member.title)) +
        '<span class="bd-hint"> · ' + escapeHtml(t('bench.id', 'ID {id}').replace('{id}', member.id)) + '</span></p>' +
      '<p class="bd-hint bd-warn-hint">' + escapeHtml(t('del.warn',
        '会连带删除这本书记的收藏/在读/阅读进度/时长/评分/书评/书单归属——这些都**不会**搬到同组的其它书上。')) + '</p>' +
      '<p class="bd-hint">' + escapeHtml(t('plan.trashHint',
        '删掉的书会进书库回收站（后台 → 回收站 可以恢复）；但这本书记的收藏/在读/进度/评分/书评/书单归属会一起消失，恢复也带不回来。')) + '</p>' +
      '<div class="bd-confirm-actions">' +
        '<button class="bd-btn bd-btn-small bd-btn-danger" data-del-yes="' + member.id + '">' +
          escapeHtml(t('del.confirm', '确认删除')) + '</button>' +
        '<button class="bd-btn bd-btn-small" data-del-no="1">' +
          escapeHtml(t('del.cancel', '取消')) + '</button>' +
      '</div>' +
    '</div>';
  }

  function diffTableHtml(diff, members, keeper) {
    diff = diff || { rows: [], shared: [] };
    var head = '<tr><th>' + escapeHtml(t('diff.field', '对比项')) + '</th>' +
      members.map(function (member) {
        var mark = member.id === keeper ? ' class="bd-th-keep"' : '';
        return '<th' + mark + '>' + escapeHtml(member.title) + '</th>';
      }).join('') + '</tr>';
    var rows = (diff.rows || []).map(function (row) {
      // 逐列按 `book_id` 对齐当前成员：后端的表格与成员列表理论上同源，但一旦不同步
      // （旧报告 + 已被合并掉的成员），把某一本的数字排到另一本的书名下面是**错误信息**，
      // 比缺一格更糟。所以宁可显示 "—"，也不按位置硬塞。
      var byId = {};
      (row.cells || []).forEach(function (cell) { byId[cell.book_id] = cell; });
      return '<tr><td class="bd-td-field">' + escapeHtml(diffFieldLabel(row)) + '</td>' +
        members.map(function (member) {
          var cell = byId[member.id];
          if (!cell) return '<td>—</td>';
          var best = row.best_id && row.best_id === cell.book_id ? ' bd-td-best' : '';
          return '<td class="' + best.trim() + '">' +
            escapeHtml(diffCellText(cell)) + '</td>';
        }).join('') + '</tr>';
    }).join('');

    // 摘要按**当前语言**在本地拼：后端那句是中文（报告文件里给人看的），
    // en / zh-TW 下直接用它就是整句中文
    var summary = '';
    if ((diff.shared || []).length) {
      var sharedNames = diff.shared.map(function (field) {
        return diffFieldText(field, field);
      }).join(t('diff.listJoin', '、'));
      summary = '<p class="bd-hint">' + escapeHtml(t('diff.shared',
        '这些项两份都相同：{fields}').replace('{fields}', sharedNames)) + '</p>';
    }
    var body = rows
      ? '<table class="bd-diff"><thead>' + head + '</thead><tbody>' + rows + '</tbody></table>'
      : '<p class="bd-hint">' + escapeHtml(t('diff.allSame', '两份的元数据与条目属性完全相同')) + '</p>';
    return '<div class="bd-diff-wrap">' + summary + body + '</div>';
  }

  // 对照表的字段名：后端给稳定的 `field` 标识，这里映射到 i18n 键。
  // 刻意写成"字段 → 完整键名"的字面量表：拼键（'diff.field.' + field）会让契约测试
  // 静态对账不出"到底用了哪些键"（前端的键扫描靠字面量）。
  var DIFF_FIELD_KEYS = {
    size: 'diff.field.size',
    formats: 'diff.field.formats',
    isbn: 'diff.field.isbn',
    metadata: 'diff.field.metadata',
    cover: 'diff.field.cover',
    added: 'diff.field.added',
    rating: 'diff.field.rating',
    tags: 'diff.field.tags',
    series: 'diff.field.series',
    publisher: 'diff.field.publisher',
    languages: 'diff.field.languages',
    translators: 'diff.field.translators',
    collector: 'diff.field.collector',
    sole: 'diff.field.sole',
    book_type: 'diff.field.book_type',
  };

  function diffFieldText(field, fallback) {
    var key = DIFF_FIELD_KEYS[field];
    return key ? t(key, fallback || field) : (fallback || field);
  }

  function diffFieldLabel(row) {
    // 兜底用后端给的中文标注（那是报告文件里给人看的）
    return diffFieldText(row.field, row.label);
  }

  // 取值只看 `kind`（后端把与语言有关的字段都标了 kind），不需要再按字段名分支
  function diffCellText(cell) {
    var value = cell.value;
    if (cell.kind === 'bytes') return formatBytes(value);
    if (cell.kind === 'score') return t('diff.score', '{n} 分').replace('{n}', value);
    if (cell.kind === 'rating') {
      return value ? t('diff.stars', '{n} 星').replace('{n}', value / 2)
                   : t('diff.unrated', '未评分');
    }
    if (cell.kind === 'bool') return value ? t('diff.yes', '是') : t('diff.no', '否');
    if (cell.kind === 'enum') {
      return value ? t('diff.physical', '实体书') : t('diff.ebook', '电子书');
    }
    return value === undefined || value === null ? '—' : String(value);
  }

  function renderHandled() {
    var merged = state.removedTitles || [];
    var deleted = state.deletedTitles || [];
    var failed = state.failedTitles || [];
    var total = merged.length + deleted.length + failed.length;
    el['handled-card'].hidden = total === 0;
    if (!total) return;
    // 三类分开写：合并的、单独删掉的、**没做成的**——后果各不相同，
    // 尤其"没做成"必须显示出来（以前 5 本失败 3 本时提示仍是"已合并…"）
    var items = merged.map(function (item) {
      return '<li>' + escapeHtml(t('handled.item', '《{title}》已并入《{into}》')
        .replace('{title}', item.title).replace('{into}', item.into)) +
        '<span class="bd-hint"> · ' + escapeHtml(item.at || '') + '</span></li>';
    }).concat(deleted.map(function (item) {
      return '<li class="bd-handled-deleted">' +
        escapeHtml(t('handled.deleted', '《{title}》已删除').replace('{title}', item.title)) +
        '<span class="bd-hint"> · ' + escapeHtml(item.at || '') + '</span></li>';
    })).concat(failed.map(function (item) {
      return '<li class="bd-handled-failed">' +
        escapeHtml(t('handled.failed', '《{title}》未处理：{reason}')
          .replace('{title}', item.title)
          .replace('{reason}', failureReasonText(item))) +
        '<span class="bd-hint"> · ' + escapeHtml(item.at || '') + '</span></li>';
    }));
    el['handled-list'].innerHTML = items.join('');
  }

  // 失败原因是错误码（后端不送中文），在这里翻成人话
  var FAILURE_KEYS = {
    'merge.source_missing': 'reason.sourceMissing',
    'merge.target_missing': 'reason.targetMissing',
    'merge.copy_failed': 'reason.copyFailed',
    'merge.delete_failed': 'reason.deleteFailed',
    'merge.same_book': 'reason.sameBook',
  };

  function failureReasonText(item) {
    var key = FAILURE_KEYS[item.error];
    if (key) return t(key, item.error);
    return item.message || item.error || '';
  }

  // ---------------------------------------------------------------- 合并预览 / 执行

  function openPlan() {
    if (!state.active) return;
    var index = state.active.index;
    var ids = selectedIds();
    if (!ids.length) {
      notify(t('bench.pickNoneHint', '还没勾选要合并的书：没勾的就原样保留，不会被动'), 'warning');
      return;
    }
    api('merge_plan?index=' + index + '&keeper_id=' + (state.active.keeperId || '') +
        '&source_ids=' + ids.join(','))
      .then(function (resp) {
        if (!resp || resp.err !== 'ok') {
          notify((resp && resp.msg) || t('plan.failed', '生成合并预览失败'), 'error');
          return;
        }
        state.plan = resp.data || {};
        state.plan.index = index;      // 兜一层：预览归属必须与当前展开的组一致
        refreshDrawer();
        var node = el.groups.querySelector('.bd-plan');
        if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
      });
  }

  /** 勾选要删的成员里，有几本带着"会一起消失"的用户数据（评分/标签/简介）。
   *
   * 数据来自计划里的成员（报告形状本来就带 `rating` / `tags` / `comments_present`），
   * 不额外去读书库——预览里多这一句的代价是零查询。
   */
  function userDataWarnHtml(plan) {
    var byId = {};
    (plan.members || []).forEach(function (member) { byId[member.id] = member; });
    var affected = [];
    (plan.steps || []).forEach(function (step) {
      var member = byId[step.source_id];
      if (!member) return;
      var hasRating = (Number(member.rating) || 0) > 0;
      var hasTags = (member.tags || []).length > 0;
      if (hasRating || hasTags || member.comments_present) affected.push(member.title);
    });
    if (!affected.length) return '';
    return '<p class="bd-hint bd-warn-hint">' +
      escapeHtml(t('plan.userDataWarn', '勾选要删的这几本里，{n} 本带着你的评分/标签/简介：{titles}')
        .replace('{n}', affected.length)
        .replace('{titles}', affected.join(t('list.titleJoin', '、')))) + '</p>';
  }

  /** "未勾选、保持原样"那几本：让用户看清这次不动谁。 */
  function keptLineHtml(plan) {
    var kept = plan.kept || [];
    if (!kept.length) return '';
    return '<p class="bd-hint">' +
      escapeHtml(t('plan.kept', '未勾选、保持原样 {n} 本：{titles}')
        .replace('{n}', kept.length)
        .replace('{titles}', kept.map(function (item) {
          return '《' + item.title + '》';
        }).join(t('list.titleJoin', '、')))) + '</p>';
  }

  /** 合并预览块（渲染在抽屉里，不再是浮层）。 */
  function planHtml(plan) {
    var warnings = [
      t('plan.warnDrop', '同名格式不会被复制：源书的同名文件会被丢弃，留下的是保留项的那一份。'),
      t('plan.warnMigrate', '源记录的收藏/在读/阅读进度/评分/书单会随删除**一起消失**，不会迁移到保留项。'),
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
    var count = (plan.steps || []).length;

    return '<div class="bd-plan">' +
      '<h3 class="bd-plan-title">' + escapeHtml(t('plan.title', '合并预览')) + '</h3>' +
      '<p class="bd-plan-head">' + escapeHtml(t('plan.keep', '保留：{title}')
        .replace('{title}', plan.keeper_title)) + '</p>' +
      '<ul class="bd-plan-steps">' + steps + '</ul>' +
      keptLineHtml(plan) +
      '<div class="bd-plan-stats">' +
        '<span>' + escapeHtml(t('plan.movedTotal', '复制格式 {n} 个').replace('{n}', plan.moved_total)) + '</span>' +
        '<span>' + escapeHtml(t('plan.droppedTotal', '丢弃同格式 {n} 个').replace('{n}', plan.dropped_total)) + '</span>' +
        '<span>' + escapeHtml(t('plan.reclaim', '可回收 {v}').replace('{v}', formatBytes(plan.reclaimable_bytes))) + '</span>' +
      '</div>' +
      '<ul class="bd-warnings">' + warnings.map(function (text) {
        return '<li>' + escapeHtml(text) + '</li>';
      }).join('') + '</ul>' +
      userDataWarnHtml(plan) +
      '<label class="bd-check">' +
        // **默认勾上**：0.1.4 时"合并"意味着整组都并掉、这一击里没有"要删哪几本"的信息，
        // 所以默认值取保守的那个。0.1.5 起勾选本身就是同意——用户逐本勾出来的才是要
        // 合并掉的。不勾的后果也不是"什么都没做"：格式被复制过去、源记录还在，磁盘反而
        // 多占一份，重复记录下次扫描照样报出来。所以默认指向"合并掉"，并在文案里带上数量。
        '<input type="checkbox" data-delete-source checked>' +
        '<span>' + escapeHtml(t('plan.deleteSourceCount', '合并后删除勾选的 {n} 条记录')
          .replace('{n}', count)) + '</span>' +
      '</label>' +
      '<p class="bd-hint">' + escapeHtml(t('plan.deleteSourceOff',
        '不勾则只把格式并过去：两条记录都留着（同名格式那一份仍会被丢弃，磁盘占用不会下降）。')) + '</p>' +
      '<p class="bd-hint">' + escapeHtml(t('plan.trashHint',
        '删掉的书会进书库回收站（后台 → 回收站 可以恢复）；但这本书记的收藏/在读/进度/评分/书评/书单归属会一起消失，恢复也带不回来。')) + '</p>' +
      '<div class="bd-drawer-actions">' +
        '<button class="bd-btn bd-btn-primary" data-apply="1">' +
          escapeHtml(t('plan.applyCount', '确认合并（{n} 本）').replace('{n}', count)) + '</button>' +
        '<button class="bd-btn" data-plan-cancel="1">' +
          escapeHtml(t('plan.cancel', '取消')) + '</button>' +
      '</div>' +
    '</div>';
  }

  function applyMerge() {
    if (!state.plan) return;
    var checkbox = el.groups.querySelector('[data-delete-source]');
    var button = el.groups.querySelector('[data-apply]');
    var ids = state.plan.source_ids || selectedIds();
    if (button) button.disabled = true;
    api('merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        index: state.plan.index,
        keeper_id: state.plan.keeper_id,
        // 服务端会拿这一串回本次扫描的成员里逐个核对（勾选不是"传 id 就能删书"的口子）
        source_ids: ids,
        delete_source: !checkbox || checkbox.checked,
      }),
    }).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        if (button) button.disabled = false;
        notify((resp && resp.msg) || t('plan.applyFailed', '合并失败'), 'error');
        return;
      }
      var data = resp.data || {};
      // 部分失败必须说出来：后端在"部分成功"时仍回 err=ok，只把这几个数放在 data 里。
      // 以前这里只看 moved_total / removed_ids，5 本里失败 3 本也显示成完全成功。
      var moved = data.moved_total || 0;
      var removed = (data.removed_ids || []).length;
      var kept = (data.kept || []).length;
      if (data.failed) {
        notify(t('plan.appliedPartial',
          '已合并：复制 {m} 个格式，删除 {d} 条重复记录；{f} 条失败（详见「本次已处理」）')
          .replace('{m}', moved).replace('{d}', removed).replace('{f}', data.failed),
          'error');
      } else if (kept) {
        // 没勾的那几本还在书库里 → 组还在列表上，说清楚还剩几本
        notify(t('plan.appliedKept',
          '已合并：复制 {m} 个格式，删除 {d} 条重复记录；{k} 本未勾选、保持原样')
          .replace('{m}', moved).replace('{d}', removed).replace('{k}', kept),
          'success');
      } else {
        notify(t('plan.applied', '已合并：复制 {m} 个格式，删除 {d} 条重复记录')
          .replace('{m}', moved).replace('{d}', removed), 'success');
      }
      var done = state.active ? state.active.index : null;
      state.active = null;
      state.plan = null;
      if (done !== null) delete state.groupCache[done];
      // 该组已只剩一本 → 后端 /groups 会把它摘掉；只合并了一部分时它还在，
      // 那就把它重新展开，好接着处理剩下那几本（"一组做一半"是 0.1.5 的常态）
      loadGroups().then(function () {
        if (done !== null && kept) reopenRow(done);
      });
    }).catch(function () {
      if (button) button.disabled = false;
      notify(t('plan.applyFailed', '合并失败'), 'error');
    });
  }

  /** 部分合并之后重新展开同一组（行还在才展开——剩不足两本时列表已把它摘掉）。 */
  function reopenRow(index) {
    if (!el.groups.querySelector('.bd-group[data-group="' + index + '"]')) return;
    api('group?index=' + index).then(function (resp) {
      if (!resp || resp.err !== 'ok') return;      // 静默：列表本身已经刷新过了
      state.groupCache[index] = resp.data.group;
      setActive(index, resp.data.group);
    });
  }

  // ---------------------------------------------------------------- 忽略（不是重复）

  /** 记下"这几本不是重复"：不动书库，只让这一组以后不再报出来（可撤销）。 */
  function ignoreGroup() {
    if (!state.active) return;
    var index = state.active.index;
    var button = el.groups.querySelector('[data-ignore]');
    if (button) button.disabled = true;
    api('ignore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 不带 ids = 整组（服务端照样会回本次扫描的成员里核对一遍）
      body: JSON.stringify({ index: index }),
    }).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        if (button) button.disabled = false;
        notify((resp && resp.msg) || t('ignore.failed', '忽略失败'), 'error');
        return;
      }
      var titles = (resp.data || {}).titles || [];
      notify(t('ignore.done', '已忽略《{titles}》：以后查重不再报出来（可在「已忽略」里撤销）')
        .replace('{titles}', titles.join('》《')), 'success');
      state.active = null;                 // 这一组马上会从列表里消失
      state.plan = null;
      delete state.groupCache[index];
      loadGroups();
      loadIgnored();
    }).catch(function () {
      if (button) button.disabled = false;
      notify(t('ignore.failed', '忽略失败'), 'error');
    });
  }

  function loadIgnored() {
    api('ignored').then(function (resp) {
      if (!resp || resp.err !== 'ok') return;      // 读不到就不显示这张卡，不打扰
      state.ignored = (resp.data || {}).ignored || [];
      renderIgnored();
    }).catch(function () { /* 忽略名单读不到不该影响主流程 */ });
  }

  function renderIgnored() {
    var rows = state.ignored || [];
    el['ignored-card'].hidden = rows.length === 0;
    if (!rows.length) return;
    el['ignored-count'].textContent = t('ignore.count', '{n} 对').replace('{n}', rows.length);
    el['ignored-list'].innerHTML = rows.map(function (row) {
      return '<li>' +
        escapeHtml(t('ignore.item', '《{a}》 ↔ 《{b}》')
          .replace('{a}', row.a_title).replace('{b}', row.b_title)) +
        '<span class="bd-hint"> · ' + escapeHtml(row.at || '') + '</span>' +
        '<button class="bd-btn bd-btn-small" data-unignore="' + row.a + ',' + row.b + '">' +
          escapeHtml(t('ignore.undo', '撤销')) + '</button>' +
      '</li>';
    }).join('');
    el['ignored-list'].querySelectorAll('[data-unignore]').forEach(function (node) {
      node.addEventListener('click', function () {
        var parts = node.getAttribute('data-unignore').split(',');
        unignore([[Number(parts[0]), Number(parts[1])]]);
      });
    });
  }

  function unignore(pairs, all) {
    var button = el['btn-unignore-all'];
    if (button) button.disabled = true;
    api('unignore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(all ? { all: true } : { pairs: pairs }),
    }).then(function (resp) {
      if (button) button.disabled = false;
      if (!resp || resp.err !== 'ok') {
        notify((resp && resp.msg) || t('ignore.undoFailed', '撤销失败'), 'error');
        return;
      }
      notify(t('ignore.undone', '已撤销 {n} 对忽略')
        .replace('{n}', (resp.data || {}).removed || 0), 'success');
      loadIgnored();
      loadGroups();          // 撤销之后被藏起来的组要回来
    }).catch(function () {
      if (button) button.disabled = false;
      notify(t('ignore.undoFailed', '撤销失败'), 'error');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());