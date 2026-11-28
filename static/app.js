/* 履约监控系统 · 前端 */
const $ = s => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const icon = name => `<svg class="ui-icon" aria-hidden="true" focusable="false"><use href="#i-${name}"></use></svg>`;
const sectionIcon = (name, tone) => `<span class="section-icon tone-${tone}">${icon(name)}</span>`;
const metricCard = (label, value, detail, name, tone) =>
  `<div class="kpi metric-card tone-${tone}"><div class="kpi-label"><span class="lbl">${label}</span>${sectionIcon(name,tone)}</div><div class="val">${value}</div><div class="fl sr-only">${detail}</div></div>`;
const fmt = n => (n == null || isNaN(n)) ? '—' : Math.round(n).toLocaleString('zh-CN');
const fmt1 = n => (n == null || isNaN(n)) ? '—' : (Math.round(n * 10) / 10).toFixed(1);
const dt = s => s ? String(s).replace('T', ' ').slice(0, 16) : '—';
const trackTime = (local, utc) => local ||
  (utc && utc !== 'NaT' ? `${String(utc).replace('T', ' ').slice(0, 19)} UTC` : '时间未提供');
const measure = (value, unit = '') => value == null ? '—' : fmt1(value) + unit;
const DELIVERY_WINDOWS = [2, 3, 5, 6, 7];
const deliveryRate = (row, days) => row.deliveryRates?.[days];
const durationText = value => {
  if (value == null || !Number.isFinite(value) || value < 0) return '—';
  const minutes = Math.floor(value);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} 小时 ${minutes % 60} 分钟` :
    `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
};
const platformName = code => META?.platformNames?.[code] || code || '—';
const levelTone = level => level === '紧急' ? 'e' : level === '高' ? 'h' : 'm';
const states = { open: ['gray', '待处理'], claimed: ['blue', '已认领'], handling: ['purple', '处理中'],
  resolved: ['ok', '已解决'], recovered: ['ok', '已不再命中'], archived: ['m', '已归档'] };
const actions = { resolve: '标记解决', archive: '归档' };
const allowedActions = () => Object.entries(actions).filter(([key]) =>
  ME.can_edit && (key !== 'archive' || ['admin', 'logistics'].includes(ME.role)));
const stateBadge = state => {
  const [tone, label] = states[state] || ['gray', state];
  return `<span class="badge ${tone}">${esc(label)}</span>`;
};
const jsonPost = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

let ME = null, META = null, RULES = null;
let lastImportAt = null;
let snapshotAt = null;
let metaFetch = null, metaFetchRevision = '';
let chartsFetch = null;
const charts = {};
const pendingRequests = new Map();
const pageResponses = new Map();
const cachedPages = new Set(['activity', 'dash', 'health', 'track', 'trackSummary', 'drawer', 'channels', 'us', 'regionSegments', 'rules', 'trackExceptions']);
let readGeneration = 0;
let dataRevision = '', revisionCheckedAt = -Infinity;
const reportMemory = new Map();
const reportInflight = new Map();
const reportCacheBase = 'fulfillment-report-v2:';
const reportTtl = 120_000;
const metaPath = '/api/meta';
const reportTimers = new Map();
let resizeFrame = 0;
let toastTimer;
const chartTheme = {
  ink: '#181818', paper: '#fff', text: '#181818', muted: '#6a6a6a',
  grid: '#e4e4e4',
  series: ['#b8b8b8', '#757575', '#435f50', '#181818'],
  font: 'Geist, "Microsoft YaHei", "PingFang SC", sans-serif',
};
const chartMotion = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const chartTip = { backgroundColor: chartTheme.ink, borderWidth: 0, confine: true,
  padding: [8, 12], textStyle: { color: chartTheme.paper, fontFamily: chartTheme.font, fontSize: 14 } };
const compactCount = value => value >= 10000 ? `${Number((value / 10000).toFixed(1))} 万` : fmt(value);

function setTopMeta(createdThrough) {
  const parts = [createdThrough ? `建单截止 ${createdThrough}` : '履约数据'];
  const imported = lastImportAt ? `最近导入 ${dt(lastImportAt)}` : '';
  const snapshot = snapshotAt ? new Date(snapshotAt).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '';
  const banner = $('#dashboardSource');
  banner.hidden = !imported && !snapshot;
  banner.classList.toggle('is-snapshot', Boolean(snapshot));
  $('#dashboardSourceLabel').textContent = snapshot ? '云端快照' : 'ERP / WMS 履约数据';
  $('#dashboardSourceTime').textContent = snapshot ? `${snapshot}（北京时间）` : imported;
  if (VIEW !== 'dash') {
    if (imported) parts.push(imported);
    if (snapshot) parts.push(`云端快照 ${snapshot}`);
  }
  $('#topMeta').textContent = parts.join(' · ');
}

function toast(msg, undo) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('on');
  if (undo) {
    const button = el('button', 'toast-undo', '撤销');
    button.type = 'button';
    button.onclick = () => {
      button.disabled = true;
      run(undo);
    };
    t.appendChild(button);
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    t.classList.remove('on');
    t.replaceChildren();
  }, undo ? 10000 : 2200);
}

async function api(path, opts = {}) {
  const headers = new Headers(opts.headers);
  if (ME?.csrf && !['GET', 'HEAD'].includes((opts.method || 'GET').toUpperCase())) {
    headers.set('X-CSRF-Token', ME.csrf);
  }
  const r = await fetch(path, { credentials: 'same-origin', ...opts, headers });
  if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(Array.isArray(d.detail) ? d.detail.map(item => item.msg).join('；') :
    d.detail || d.message || ('HTTP ' + r.status));
  if (d.snapshotAt && d.snapshotAt !== snapshotAt) {
    snapshotAt = d.snapshotAt;
    setTopMeta(META?.range.end || d.asof);
  }
  return d;
}

function reportPath(path) {
  const url = new URL(path, location.origin);
  for (const [key, value] of url.searchParams) {
    if (value.startsWith('[')) {
      try { const parsed = JSON.parse(value); if (Array.isArray(parsed)) url.searchParams.set(key, JSON.stringify([...parsed].sort())); }
      catch { /* Keep an unrecognized parameter unchanged. */ }
    }
  }
  url.searchParams.sort();
  return url.pathname + url.search;
}

function reportStorageKey(path) {
  const version = /^\/api\/channel-members(?:\?|$)/.test(path) ? 'member-lastmile:' :
    /^\/api\/channels(?:\?|$)/.test(path) ? 'channel-members:' :
    /^\/api\/overview(?:\?|$)/.test(path) ? 'country-timing-p90:' :
      /^\/api\/regional-analysis(?:\?|$)/.test(path) ? 'regional-geography:' : '';
  return reportCacheBase + encodeURIComponent(`${ME?.username || 'preview'}:${ME?.role || 'preview'}:${ME?.preview ? 'preview' : 'full'}`) + ':' + version + path;
}

function reportRecord(path) {
  if (reportMemory.has(path)) return reportMemory.get(path);
  try {
    const raw = localStorage.getItem(reportStorageKey(path));
    if (raw) {
      const record = JSON.parse(raw);
      if (record.data && Number.isFinite(record.at)) { reportMemory.set(path, record); return record; }
    }
  } catch { /* The memory cache remains available when storage is disabled. */ }
  return null;
}

function persistReport(path, record) {
  reportMemory.set(path, record);
  if (reportMemory.size > 40) reportMemory.delete(reportMemory.keys().next().value);
  const save = () => {
    try {
      const key = reportStorageKey(path);
      const prefix = reportCacheBase;
      const keys = Object.keys(localStorage).filter(item => item.startsWith(prefix) && item !== key);
      // Keep the most recent aggregate reports within the browser's storage budget.
      const sorted = keys.map(item => [item, (() => { try { return JSON.parse(localStorage.getItem(item)).at || 0; } catch { return 0; } })()])
        .sort((a,b) => a[1] - b[1]);
      while (sorted.length >= 24) localStorage.removeItem(sorted.shift()[0]);
      const value = JSON.stringify(record);
      while (true) {
        try { localStorage.setItem(key, value); break; }
        catch { if (!sorted.length) break; localStorage.removeItem(sorted.shift()[0]); }
      }
    } catch { /* No storage: use in-memory data. */ }
  };
  if ('requestIdleCallback' in window) requestIdleCallback(save, {timeout: 1000});
  else setTimeout(save, 0);
}

function reportFresh(record, maxAge = reportTtl) {
  return record && dataRevision && record.revision === dataRevision &&
    performance.now() - revisionCheckedAt < 40_000 && Date.now() - record.at < maxAge;
}

function fetchReport(path, options = {}, force = false) {
  const saved = reportRecord(path);
  if (!force && reportFresh(saved, path === metaPath ? Infinity : reportTtl)) return Promise.resolve(saved);
  const revision = dataRevision;
  const pending = reportInflight.get(path);
  if (pending?.revision === revision) return pending.promise;
  const promise = api(path, {signal: AbortSignal.timeout(22_000), ...options}).then(data => {
    // Preserve chart and table identity when the calculated data did not change.
    const previous = reportRecord(path);
    if (previous && JSON.stringify(previous.data) === JSON.stringify(data)) data = previous.data;
    const record = {data, at: Date.now(), revision};
    if (reportInflight.get(path)?.promise === promise) persistReport(path, record);
    return record;
  }).finally(() => { if (reportInflight.get(path)?.promise === promise) reportInflight.delete(path); });
  reportInflight.set(path, {revision, promise});
  return promise;
}

const cacheKeyView = {dash: 'dash', channels: 'channels', us: 'us', health: 'rules', rules: 'rules',
  trackSync: 'rules', channelGroups: 'rules', trackExceptions: 'track'};
function reportStatus(key, text) {
  if (cacheKeyView[key] !== VIEW) return;
  if (key === 'trackExceptions' && trackMode !== 'special-tracking') return;
  if (VIEW === 'rules') {
    const pane = {health:'data', trackSync:'sync', channelGroups:'channel-groups'}[key];
    if ((pane && $('#rules-' + pane).hidden) || (key === 'rules' && $('#rules-sla').hidden && $('#rules-business').hidden)) return;
  }
  const status = $('#reportCacheStatus');
  status.hidden = !text;
  status.textContent = text || '';
}

function cacheClock(at) {
  return new Date(at).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
}

async function presentReport(key, path, render, options = {}, {force = false, oldVisible = false} = {}) {
  path = reportPath(path);
  const id = Symbol(), generation = readGeneration;
  pendingRequests.set(key, id);
  const current = () => pendingRequests.get(key) === id && generation === readGeneration;
  const cached = reportRecord(path);
  if (cached) {
    render(cached.data);
    reportStatus(key, reportFresh(cached) && !force ? `更新于 ${cacheClock(cached.at)}` : `更新于 ${cacheClock(cached.at)} · 正在同步`);
  } else reportStatus(key, oldVisible ? '正在更新筛选 · 下方为上次结果' : '正在读取数据');
  try {
    const record = await fetchReport(path, options, force);
    if (!current()) return null;
    if (!cached || cached.data !== record.data) render(record.data);
    reportStatus(key, `更新于 ${cacheClock(record.at)}`);
    return record.data;
  } catch (error) {
    if (!current()) return null;
    if (!cached) {
      reportStatus(key, oldVisible ? '上次结果 · 更新失败' : '数据读取失败');
      throw error;
    }
    reportStatus(key, `缓存 ${cacheClock(cached.at)} · 暂未更新`);
    if (key === 'regionSegments' && VIEW === 'us') $('#regionalSegmentStatus').textContent += ' · 缓存结果，暂未更新';
    return cached.data;
  }
}

function scheduleReport(key, refresh) {
  pendingRequests.delete(key);
  clearTimeout(reportTimers.get(key));
  reportTimers.set(key, setTimeout(() => {
    if (cacheKeyView[key] === VIEW) run(refresh);
  }, 180));
}

function applyMeta(data) {
  if (META === data) return data;
  META = data;
  setTopMeta(META.range.end);
  const alerts = Object.values(alertFiltersByMode);
  const regions = Object.values(regionFiltersByMarket);
  for (const filters of [DF, TF, CF, ...alerts, ...regions]) {
    if (!filters) continue;
    for (const [key, options] of Object.entries(META.filterOptions)) {
      if (!filters[key]?.setOptions || ['month','week'].includes(key)) continue;
      const values = regions.includes(filters) && key === 'cn' ? options.filter(option =>
        !['us','usa','united states','united states of america'].includes(option.v.toLowerCase().trim())) :
        alerts.includes(filters) && key === 'channel' ? (META.alertChannels || []) : options;
      filters[key].setOptions(values, values.reduce((sum,row) => sum + row.n, 0),
        key !== 'channel_group' || filterPresetWidgets.has(filters));
    }
    if (filters.date_basis) updateDateOptions(filters, filters.date_basis.value);
  }
  slaGroupFilter?.setOptions(META.filterOptions.channel_group || [], META.range.orders);
  return data;
}

function revalidateMeta(force = false) {
  if (!metaFetch || metaFetchRevision !== dataRevision) {
    const revision = dataRevision;
    const request = fetchReport(metaPath, {signal: AbortSignal.timeout(12_000)}, force)
      .then(record => metaFetch === request ? applyMeta(record.data) : META || record.data)
      .finally(() => { if (metaFetch === request) metaFetch = null; });
    metaFetch = request;
    metaFetchRevision = revision;
  }
  return metaFetch;
}

function loadMeta() {
  const saved = reportRecord(metaPath);
  if (saved) {
    applyMeta(saved.data);
    if (!reportFresh(saved, Infinity)) revalidateMeta().catch(() => {});
    return Promise.resolve(META);
  }
  return revalidateMeta();
}

function loadChartLibrary() {
  if (window.echarts) return Promise.resolve();
  if (!chartsFetch) chartsFetch = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/static/echarts.min.js?v=1';
    const fail = () => {
      clearTimeout(deadline);
      chartsFetch = null;
      script.remove();
      reject(new Error('图表组件加载失败，请稍后重试'));
    };
    const deadline = setTimeout(fail, 10_000);
    script.onload = () => {
      clearTimeout(deadline);
      resolve();
    };
    script.onerror = fail;
    document.head.append(script);
  });
  return chartsFetch;
}

function readPage(path, options) {
  const current = pageResponses.get(path);
  const now = performance.now();
  const revisionFresh = dataRevision && now - revisionCheckedAt < 40_000;
  if (current && (!current.until || (current.until > now &&
      (now - current.receivedAt < 25_000 || (revisionFresh && current.revision === dataRevision))))) return current.result;
  const entry = { until: 0 };
  entry.result = api(path, options).then(data => {
    entry.receivedAt = performance.now();
    entry.revision = dataRevision;
    entry.until = entry.receivedAt + (revisionFresh ? 120_000 : 25_000);
    return data;
  }, error => {
    if (pageResponses.get(path) === entry) pageResponses.delete(path);
    throw error;
  });
  pageResponses.delete(path);
  pageResponses.set(path, entry);
  if (pageResponses.size > 32) pageResponses.delete(pageResponses.keys().next().value);
  return entry.result;
}

async function refreshDataRevision() {
  try {
    const {revision} = await api('/api/data-revision', {cache: 'no-store', signal: AbortSignal.timeout(10_000)});
    if (!revision) return;
    const changed = Boolean(dataRevision && revision !== dataRevision);
    if (changed) {
      pageResponses.clear();
      trackSummaryUntil = 0;
    }
    dataRevision = revision;
    revisionCheckedAt = performance.now();
    if (changed && !document.querySelector('dialog[open]')) {
      if (usesFilterMeta()) revalidateMeta().catch(() => {});
      const refresh = {dash: loadDash, channels: loadChannels,
        track: trackMode === 'special-tracking' ? loadTrackExceptions : null,
        us: loadUs, rules: loadRules}[VIEW];
      if (refresh) run(refresh);
      if (VIEW === 'dash' || VIEW === 'track') run(loadActivity);
      if (VIEW === 'us') loadVisibleRegionSegments();
    }
  } catch {
    revisionCheckedAt = -Infinity;
  }
}

function invalidatePages() {
  pageResponses.clear();
  reportInflight.clear();
  metaFetch = null;
  revisionCheckedAt = -Infinity;
  readGeneration++;
}

async function latest(key, path, options) {
  const id = Symbol();
  const generation = readGeneration;
  pendingRequests.set(key, id);
  const cached = cachedPages.has(key);
  const stillCurrent = () => pendingRequests.get(key) === id && (!cached || generation === readGeneration);
  try {
    const result = await (cached ? readPage(path, options) : api(path, options));
    return stillCurrent() ? result : null;
  } catch (error) {
    if (stillCurrent()) throw error;
    return null;
  }
}

function run(work) {
  return Promise.resolve().then(work).catch(error => toast(error.message));
}

function exportCsv(name, fields, rows) {
  if (!rows.length) return toast('当前没有可导出的数据');
  const csvCell = value => {
    let text = String(value ?? '');
    if (/^\s*[=+@\-]/.test(text) || /^\d{15,}$/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  const content = [fields.map(([label]) => label), ...rows.map(row => fields.map(([, get]) => get(row)))]
    .map(row => row.map(csvCell).join(',')).join('\r\n');
  downloadBlob(new Blob(['\ufeff', content], { type: 'text/csv;charset=utf-8' }), name + '.csv');
}

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const VIEW_META = { dash: '数据总览', track: '轨迹监控', channels: '渠道时效', us: '区域分析', rules: '数据与规则' };
let VIEW = 'dash';
let trackMode = 'tracking';

function routeView(route) {
  if (['alerts', 'archive', 'detailed', 'special-tracking', 'channel-exceptions'].includes(route)) return 'track';
  if (['regions', 'regions-other'].includes(route)) return 'us';
  if (['channels-lastmile','channels-group'].includes(route)) return 'channels';
  return Object.hasOwn(VIEW_META, route) ? route : 'dash';
}

function usesFilterMeta() {
  return ['dash','track','us','channels'].includes(VIEW) ||
    (VIEW === 'rules' && !$('#rules-sla').hidden);
}

function displayView(v) {
  if (VIEW === 'us' && v !== 'us') segmentPath = '';
  VIEW = v;
  $('#reportCacheStatus').hidden = true;
  document.querySelectorAll('.view').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  document.querySelectorAll('#nav button').forEach(b => {
    b.classList.toggle('on', b.dataset.v === v);
    if (b.dataset.v === v) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  $('#crumb').textContent = VIEW_META[v];
  setTopMeta(META?.range.end);
}

function showView(v) {
  if (ME.preview && ['rules', 'us'].includes(v)) v = 'dash';
  displayView(v);
  const route = v === 'track' && trackMode !== 'tracking' ? trackMode :
      v === 'channels' ? 'channels-' + channelDimension :
      v === 'us' ? (usView.market === 'us' ? 'regions' : 'regions-other') : v;
  history.replaceState(null, '', '#' + route);
  window.scrollTo({ top: 0 });
  resizeVisibleCharts();
  if (META && usesFilterMeta()) loadMeta().catch(() => {});
  const tracking = {tracking: loadTrack, alerts: loadAlerts, archive: loadAlerts, 'special-tracking': loadTrackExceptions};
  run({dash: loadDash, track: tracking[trackMode], channels: loadChannels, us: loadUs, rules: loadRules}[v]);
  if (!ME.preview && (v === 'dash' || v === 'track')) run(loadActivity);
}

function initChart(id) {
  if (!charts[id]) {
    if (!Object.keys(charts).length) echarts.registerTheme('workspace', {textStyle:{fontFamily:chartTheme.font}});
    charts[id] = echarts.init(document.getElementById(id), 'workspace', { renderer: 'canvas' });
  }
  return charts[id];
}

function resizeVisibleCharts() {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    for (const chart of Object.values(charts)) {
      const host = chart.getDom();
      if (host.clientWidth && host.clientHeight &&
          (host.clientWidth !== chart.getWidth() || host.clientHeight !== chart.getHeight())) {
        chart.resize();
        if (chart === charts.usMap) drawUsMap();
      }
    }
  });
}

/* ---------------- 筛选条 ---------------- */
const FILTER_VISUALS = {
  q:['search','blue'], date_basis:['clock','blue'], month:['calendar','blue'], week:['week','blue'],
  wh:['warehouse','sand'], pf:['store','violet'], cr:['truck','cyan'], channel:['route','cyan'],
  channel_group:['layers','green'], lastmile:['box','green'], cn:['globe','blue'],
  tstat:['scan','cyan'], sub:['list','cyan'], rule:['bell','warn'], level:['alert','warn'],
  state:['check','green'], status:['check','green'], resolved_only:['check','green'],
  coverage:['layers','violet'], sort:['sort','blue'], main_only:['sliders','warn'], min_n:['chart','violet'],
};
const filterVisual = key => FILTER_VISUALS[key] || ['sliders','blue'];
function filterGlyph(key) {
  return el('span','filter-icon',icon(filterVisual(key)[0]));
}

function positionFilterPopover(root, panel) {
  document.querySelectorAll('.multi-filter[open]').forEach(other => {
    if (other !== root) other.open = false;
  });
  panel.style.left = '0';
  const overflow = panel.getBoundingClientRect().right - window.innerWidth + 12;
  if (overflow > 0) panel.style.left = `-${overflow}px`;
}

function multiFilter(def, onChange) {
  const root = el('details', 'multi-filter tone-' + filterVisual(def.key)[1]);
  root.dataset.filter = def.key;
  const trigger = el('summary', 'multi-trigger');
  const name = el('strong', 'filter-name');
  name.textContent = def.label;
  const badge = el('span', 'filter-value');
  trigger.append(filterGlyph(def.key), name, badge, el('span', 'multi-caret'));
  const panel = el('div', 'multi-popover');
  const search = el('input');
  search.type = 'search';
  search.placeholder = `搜索${def.label}…`;
  search.setAttribute('aria-label', search.placeholder);
  const all = el('input');
  all.type = 'checkbox';
  const allLabel = el('label', 'multi-row multi-all', '<span>全部</span>');
  allLabel.prepend(all);
  const total = el('small', '', fmt(def.total ?? META.range.orders));
  allLabel.append(total);
  const list = el('div', 'multi-options');
  panel.append(search, allLabel, list);
  root.append(trigger, panel);
  const selected = new Set();
  let optionsSource = def.options;

  function update() {
    badge.textContent = selected.size ? `已选 ${selected.size}` : '全部';
    root.classList.toggle('has-selection', selected.size > 0);
    all.checked = !selected.size;
  }

  function renderOptions() {
    const query = search.value.trim().toLocaleLowerCase();
    list.replaceChildren(...def.options.filter(option =>
      `${option.t} ${option.v}`.toLocaleLowerCase().includes(query)
    ).map(option => {
      const row = el('label', 'multi-row');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = selected.has(option.v);
      box.onchange = () => {
        if (box.checked) selected.add(option.v);
        else selected.delete(option.v);
        update();
        onChange(1);
      };
      const text = el('span');
      text.textContent = option.t;
      row.append(box, text, el('small', '', fmt(option.n)));
      return row;
    }));
    if (!list.children.length) list.append(el('p', 'empty', '没有匹配的选项'));
  }

  all.onchange = () => {
    selected.clear();
    update();
    renderOptions();
    onChange(1);
  };
  search.oninput = renderOptions;
  root.addEventListener('toggle', () => {
    if (!root.open) return;
    positionFilterPopover(root, panel);
    search.focus();
  });
  update();
  renderOptions();
  return {
    root,
    setOptions(options, count, keepMissing = true) {
      if (optionsSource === options) return;
      optionsSource = options;
      const seen = new Set(options.map(option => option.v));
      const missing = def.options.filter(option => selected.has(option.v) && !seen.has(option.v));
      if (!keepMissing) missing.forEach(option => selected.delete(option.v));
      def.options = [...options, ...(keepMissing ? missing.map(option => ({...option, n: 0})) : [])]
        .sort((a, b) => ['month', 'week'].includes(def.key)
        ? a.v.localeCompare(b.v, undefined, {numeric: true}) : 0);
      total.textContent = fmt(count);
      update();
      renderOptions();
    },
    labelFor(code) { return def.options.find(option => option.v === code)?.t; },
    get value() { return [...selected]; },
    set value(value) { this.setValue(value); },
    setValue(value, preserveMissing = false) {
      selected.clear();
      for (const code of Array.isArray(value) ? value : [value]) {
        if (typeof code !== 'string' || !code) continue;
        if (!def.options.some(option => option.v === code)) {
          if (!preserveMissing) continue;
          def.options = [...def.options, {v:code, t:code, n:0}];
        }
        selected.add(code);
      }
      update();
      renderOptions();
    },
  };
}

function makeTimeRange(onChange) {
  const root = el('div', 'filter-time-range tone-blue');
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', '时间范围（北京时间）');
  const fields = {};
  const inputs = [];
  const error = el('span', 'filter-time-error');
  error.setAttribute('role', 'status');
  error.hidden = true;
  for (const [key, label, defaultTime] of [['start', '开始', '00:00'], ['end', '结束', '23:59']]) {
    const date = el('input');
    date.type = 'date';
    date.setAttribute('aria-label', `${label}日期（北京时间）`);
    const time = el('input');
    time.type = 'time';
    time.step = '60';
    time.disabled = true;
    time.setAttribute('aria-label', `${label}时分（北京时间）`);
    time.title = `未填写时默认 ${defaultTime}`;
    const updateTime = () => {
      time.disabled = !date.value;
      time.value = date.value ? time.value || defaultTime : '';
      field.classList.toggle('has-date', Boolean(date.value));
    };
    const changed = () => { updateTime(); onChange(1); };
    date.onchange = changed;
    time.onchange = changed;
    const field = el('div', 'date-filter');
    const dayLabel = el('label', 'date-filter-label');
    dayLabel.append(el('span', '', label), date);
    field.append(dayLabel, time);
    root.appendChild(field);
    inputs.push(date, time);
    fields[key] = {
      get value() {
        if (!date.value || !time.value || time.value === defaultTime) return date.value;
        return `${date.value}T${time.value}`;
      },
      set value(value) {
        const parts = (value || '').split(/[T ]/);
        date.value = parts[0];
        time.value = parts[1] || '';
        updateTime();
      },
    };
  }
  root.append(el('span', 'filter-timezone', icon('clock') + '<span>北京时间</span>'), error);
  return {root, fields, validate() {
    const {start, end} = fields;
    const invalidInput = inputs.some(input => !input.validity.valid);
    const minute = (value, fallback) => value.length === 10 ? `${value}T${fallback}` : value;
    const reversed = Boolean(start.value && end.value && minute(start.value, '00:00') > minute(end.value, '23:59'));
    error.hidden = !invalidInput && !reversed;
    error.textContent = invalidInput ? '请填写有效的日期和分钟' : reversed ? '开始时间不能晚于结束时间' : '';
    return error.hidden;
  }};
}

function makeFilters(host, defs, onChange) {
  host.innerHTML = '';
  const refs = {};
  const defaults = new Map();
  const cancelSearches = [];
  let timeRange;
  const change = (...args) => {
    if (!timeRange || timeRange.validate()) {
      onChange(...args);
      syncFilterPreset(refs);
    }
  };
  defs.forEach(d => {
    if (d.type === 'text') {
      const i = el('input'); i.placeholder = d.label; i.dataset.key = d.key; i.style.width = (d.w || 210) + 'px';
      i.setAttribute('aria-label', d.label);
      let timer;
      cancelSearches.push(() => clearTimeout(timer));
      const submit = () => { clearTimeout(timer); change(1, 'search'); };
      const schedule = () => { clearTimeout(timer); timer = setTimeout(submit, 350); };
      if (d.auto !== false) {
        i.oninput = event => { if (!event.isComposing) schedule(); };
        i.oncompositionend = schedule;
      }
      i.onkeydown = event => {
        if (event.key === 'Enter' && !event.isComposing) {
          event.preventDefault();
          submit();
        }
      };
      if (d.submit) {
        const group = el('div', 'filter-search');
        const button = el('button', 'btn ghost sm', '搜索');
        button.type = 'button';
        button.onclick = submit;
        const searchField = el('span','filter-search-field tone-' + filterVisual(d.key)[1]);
        searchField.append(filterGlyph(d.key),i);
        group.append(searchField, button);
        host.appendChild(group);
      } else {
        const searchField = el('span','filter-search-field tone-' + filterVisual(d.key)[1]);
        searchField.append(filterGlyph(d.key),i);
        host.appendChild(searchField);
      }
      refs[d.key] = i;
    } else if (d.type === 'time-range') {
      timeRange = makeTimeRange(change);
      Object.assign(refs, timeRange.fields);
      host.appendChild(timeRange.root);
    } else if (d.type === 'multi') {
      const control = multiFilter(d, change);
      host.appendChild(control.root);
      refs[d.key] = control;
    } else {
      const s = el('select'); s.dataset.key = d.key;
      s.setAttribute('aria-label', d.label);
      s.innerHTML = (d.defaultValue == null ? `<option value="">${d.defaultLabel || d.label + '：全部'}</option>` : '') +
        (d.options || []).filter(o => o != null && o !== '').map(o => {
          const v = (o && typeof o === 'object') ? o.v : o;
          const t = (o && typeof o === 'object') ? (o.t != null ? o.t : o.v) : o;
          return `<option value="${esc(v)}">${esc(t)}</option>`;
        }).join('');
      s.onchange = () => change(1);
      if (d.defaultValue != null) {
        s.value = d.defaultValue;
        defaults.set(d.key, d.defaultValue);
      }
      const control = el('span','filter-select tone-' + filterVisual(d.key)[1]);
      control.dataset.filterKey = d.key;
      control.append(filterGlyph(d.key),s);
      host.appendChild(control); refs[d.key] = s;
    }
  });
  const btn = el('button', 'btn ghost sm filter-reset', icon('sync') + '重置');
  btn.type = 'button';
  btn.onclick = () => {
    cancelSearches.forEach(cancel => cancel());
    Object.entries(refs).forEach(([key, control]) => {
      if ('value' in control) control.value = defaults.get(key) ?? '';
    });
    change(1);
  };
  host.appendChild(btn);
  const sp = el('span', 'grow'); sp.style.flex = '1';
  host.appendChild(sp);
  const info = el('span', 'tip', '');
  host.appendChild(info);
  refs._info = info;
  Object.defineProperties(refs, {
    validate: {value: () => !timeRange || timeRange.validate()},
    cancelSearch: {value: () => cancelSearches.forEach(cancel => cancel())},
  });
  return refs;
}

function readF(refs) {
  const o = {};
  for (const [key, control] of Object.entries(refs)) {
    const value = control?.value;
    if (key !== '_info' && (Array.isArray(value) ? value.length > 0 : value)) o[key] = value;
  }
  return o;
}

function qs(o, page, size) {
  const p = new URLSearchParams();
  Object.entries(o).forEach(([k, v]) => {
    if (Array.isArray(v)) {
      if (v.length) p.set(k, JSON.stringify(v));
    } else if (v !== '' && v != null) p.set(k, v);
  });
  if (page) p.set('page', page);
  if (size) p.set('size', size);
  return p.toString();
}

function pager(host, total, page, size, go, {sizes, changeSize} = {}) {
  const pages = Math.max(1, Math.ceil(total / size));
  host.innerHTML = `<span>第 ${page} / ${pages} 页 · 共 ${fmt(total)} 条</span><span class="sp"></span>`;
  const mk = (t, p, dis) => { const b = el('button', '', t); b.type = 'button'; b.disabled = dis; b.onclick = () => go(p); return b; };
  if (sizes) {
    const perPage = el('label', 'pager-field', '每页');
    const select = el('select');
    select.setAttribute('aria-label', '每页显示数量');
    select.replaceChildren(...sizes.map(n => new Option(`${n} 条`, n)));
    select.value = String(size);
    select.onchange = () => changeSize(Number(select.value));
    perPage.append(select);
    host.append(perPage);
    host.append(mk('首页', 1, page <= 1));
  }
  host.appendChild(mk('‹ 上一页', page - 1, page <= 1));
  host.appendChild(mk('下一页 ›', page + 1, page >= pages));
  if (sizes) {
    host.append(mk('末页', pages, page >= pages));
    const jump = el('form', 'pager-field');
    const field = el('input');
    field.type = 'number';
    field.min = 1;
    field.max = pages;
    field.required = true;
    field.placeholder = '页码';
    field.setAttribute('aria-label', '跳转页码');
    field.disabled = pages === 1;
    const submit = el('button', '', '跳转');
    submit.type = 'submit';
    submit.disabled = field.disabled;
    jump.append(field, submit);
    jump.onsubmit = event => {
      event.preventDefault();
      if (field.valueAsNumber !== page) go(field.valueAsNumber);
    };
    host.append(jump);
  }
}

/* ================= 数据总览 ================= */
let DF = null;
let renderedDash = null;
let paintedOverview = null;
const DEFAULT_DATE_BASIS = 'shipped';
const dateBasisLabel = {paid: '支付时间', created: '创建时间', shipped: '出库时间'};
const commonFilterLabels = {month:'月份', week:'周次', wh:'仓库', pf:'平台', cr:'后台物流商',
  channel:'当前渠道', channel_group:'自定义渠道', lastmile:'C 端渠道', cn:'国家'};
const trend = { created: [], shipped: [], paid: [], filters: {}, span: 45, basis: DEFAULT_DATE_BASIS };
const channelGroupPath = '/api/overview/channel-groups';
let channelGroups;
let chosenChannels = new Set();

async function loadChannelGroups() {
  return presentReport('channelGroups', channelGroupPath, renderChannelGroups);
}

function renderChannelGroups(data) {
  const host = $('#channelGroupRulesBody');
  host.innerHTML = data.groups.map(group => `<tr><td><b>${esc(group.name)}</b></td>
    <td><ul class="channel-group-members">${group.channels.map(channel => `<li>${esc(channel)}</li>`).join('')}</ul></td>
    ${ME.can_edit ? `<td><button type="button" class="btn ghost sm" data-edit-group="${esc(group.name)}">编辑</button></td>` : ''}</tr>`)
    .join('') || blankRow(ME.can_edit ? 3 : 2, '暂无自定义渠道分组');
}

async function openChannelGroups(button, name = '') {
  button.disabled = true;
  try {
    const [settings] = await Promise.all([api(channelGroupPath), loadMeta()]);
    channelGroups = settings;
    const choice = $('#channelGroupChoice');
    choice.replaceChildren(new Option('新增分组', ''), ...settings.groups.map(group => new Option(group.name, group.name)));
    choice.value = name;
    if (choice.selectedIndex < 0) choice.value = '';
    editChannelGroup();
    $('#channelGroupsDialog').showModal();
  } finally {
    button.disabled = false;
  }
}

function editChannelGroup() {
  const name = $('#channelGroupChoice').value;
  const group = channelGroups.groups.find(item => item.name === name);
  chosenChannels = new Set(group?.channels || []);
  $('#channelGroupName').value = name;
  $('#channelGroupSearch').value = '';
  $('#channelGroupError').textContent = '';
  $('#channelGroupRemove').hidden = !group;
  renderChannelGroupOptions();
}

function renderChannelGroupOptions() {
  const active = $('#channelGroupChoice').value;
  const query = $('#channelGroupSearch').value.trim().toLocaleLowerCase();
  const channels = new Map((META.filterOptions.channel || []).map(channel => [channel.v, channel.n]));
  const otherGroups = new Map(channelGroups.groups.filter(item => item.name !== active)
    .flatMap(item => item.channels.map(key => [key, item.name])));
  chosenChannels.forEach(key => { if (!channels.has(key)) channels.set(key, 0); });
  const options = [...channels].filter(([key]) => key.toLocaleLowerCase().includes(query));
  options.sort(([a, n], [b, m]) => Number(chosenChannels.has(b)) - Number(chosenChannels.has(a)) || m - n);
  const host = $('#channelGroupOptions');
  const rows = options.map(([key, count]) => {
    const label = el('label', 'multi-row');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = chosenChannels.has(key);
    box.disabled = otherGroups.has(key);
    box.onchange = () => {
      if (box.checked) chosenChannels.add(key);
      else chosenChannels.delete(key);
      $('#channelGroupSelected').textContent = `已选 ${chosenChannels.size} 个当前渠道`;
    };
    label.append(box, el('span', '', key), el('small', '', otherGroups.get(key) || fmt(count)));
    return label;
  });
  host.replaceChildren(...(rows.length ? rows : [el('p', 'empty', '没有匹配的当前渠道')]));
  $('#channelGroupSelected').textContent = `已选 ${chosenChannels.size} 个当前渠道`;
}

async function saveChannelGroup(remove) {
  const active = $('#channelGroupChoice').value;
  const name = $('#channelGroupName').value.trim();
  const error = $('#channelGroupError');
  if (!remove && (!name || !chosenChannels.size)) {
    error.textContent = '请填写分组名称，并勾选至少一个当前渠道。';
    return;
  }
  const groups = channelGroups.groups.filter(group => group.name !== active);
  if (!remove) groups.push({name, channels: [...chosenChannels]});
  const controls = ['channelGroupSave', 'channelGroupRemove', 'channelGroupsClose', 'channelGroupChoice',
    'channelGroupName', 'channelGroupSearch'].map(id => document.getElementById(id));
  controls.forEach(control => { control.disabled = true; });
  error.textContent = '';
  try {
    channelGroups = await api(channelGroupPath, {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({revision: channelGroups.revision, groups})});
  } catch (failure) {
    error.textContent = failure.message;
    return;
  } finally {
    controls.forEach(control => { control.disabled = false; });
  }
  $('#channelGroupsDialog').close();
  invalidatePages();
  metaFetch = null;
  toast(remove ? '已删除分组' : '已保存渠道分组');
  await Promise.all([revalidateMeta(true), loadChannelGroups()]);
}
function drawWeekCalendar(weeks) {
  const body = $('#weekCalendarBody');
  if (body.rows.length) return;
  body.innerHTML = weeks.map(week => {
    const sunday = Date.parse(week.start + 'T00:00:00Z');
    const days = Array.from({length: 7}, (_, offset) =>
      new Date(sunday + offset * 86_400_000).toISOString().slice(0, 10));
    return `<tr><th scope="row">${esc(week.v)}</th>
      ${[week.start, week.end, ...days].map(day =>
        `<td class="${day.startsWith('2026-') ? '' : 'cross-year'}"><time datetime="${esc(day)}">${esc(day)}</time></td>`).join('')}
    </tr>`;
  }).join('') || blankRow(10, '暂时无法读取周次表');
}

function dateBasisFilter() {
  return {type: 'select', label: '筛选时间', key: 'date_basis',
    defaultValue: DEFAULT_DATE_BASIS,
    options: Object.entries(dateBasisLabel).map(([v, t]) => ({v, t}))};
}

function updateDateOptions(filters, basis) {
  const options = META.dateOptions?.[basis];
  for (const key of ['month', 'week']) {
    filters[key]?.setOptions(options?.[key] || META.filterOptions[key] || [],
      options?.total ?? META.range.orders);
  }
}

function commonDateFilters() {
  return [
    dateBasisFilter(),
    { type: 'time-range' },
    ...Object.entries(commonFilterLabels).map(([key, label]) =>
      ({ type: 'multi', key, label, options: META.filterOptions[key] || [] })),
  ];
}

async function loadDash() {
  const initial = !DF && !META && !defaultFilterPreset('overview') ?
    loadOverview({date_basis: DEFAULT_DATE_BASIS}) : null;
  initial?.catch(() => {});
  loadChartLibrary().catch(() => {});
  try {
    await loadMeta();
  } catch (error) {
    if (!initial) throw error;
    if (VIEW === 'dash') {
      const retry = el('button', 'btn ghost sm', '重试筛选项');
      retry.type = 'button';
      retry.onclick = () => run(loadDash);
      $('#dashFilters').replaceChildren(retry);
    }
    return initial;
  }
  if (VIEW !== 'dash') return;
  if (!DF) {
    const host = $('#dashFilters');
    DF = makeFilters(host, commonDateFilters(), () => scheduleReport('dash',loadDash));
    attachFilterPresets('overview', '数据总览', host, DF, {onApply:() => scheduleReport('dash',loadDash)});
  }
  const f = readF(DF);
  updateDateOptions(DF, f.date_basis);
  if (initial) {
    if (renderedDash) syncOverviewFilterNote(renderedDash);
    return initial;
  }
  return loadOverview(f);
}

function loadOverview(f) {
  return presentReport('dash', '/api/overview?' + qs(f), d => renderDash(d, f), {}, {oldVisible: Boolean(renderedDash)});
}

function syncOverviewFilterNote(d) {
  if (!DF) return;
  DF._info.hidden = !d.snapshotFiltersSkipped;
  DF._info.textContent = d.snapshotFiltersSkipped ? '实时筛选暂不可用，图表显示全量快照' : '';
}

function renderDash(d, f) {
  if (VIEW !== 'dash') return;
  syncOverviewFilterNote(d);
  if (renderedDash === d) {
    void loadOverviewCharts(d);
    return;
  }
  renderedDash = d;
  const dateBasis = d.dateBasis || (d.snapshotAt ? 'created' : f.date_basis);
  $('#monthlyScope').textContent = `按${dateBasisLabel[dateBasis].replace('时间', '')}月份统计 · 出库 → 妥投`;
  setTopMeta(d.asof);

  const k = d.kpi;
  const defs = [
    ['履约单数', fmt(k.orders), '筛选范围', 'box', 'blue'],
    ['总件数', fmt(k.items), `单均 ${k.orders ? fmt1(k.items / k.orders) : '—'} 件`, 'layers', 'blue'],
    ['妥投覆盖率', measure(k.signRate, '%'), `含 WMS 补算 ${fmt(k.fallbackSigned)} 单`, 'check', 'green'],
    ['仓内处理', k.inboundHours == null ? '—' : `${fmt1(k.inboundHours)} <small>小时</small>`, '履约单创建 → 出库', 'warehouse', 'violet'],
    ['物流运输', k.transitDays == null ? '—' : `${fmt1(k.transitDays)} <small>天</small>`, `出库 → 妥投 · ${fmt(k.transitSamples)} 单`, 'truck', 'cyan'],
    ['6 天内妥投率', measure(deliveryRate(k, 6), '%'),
      k.deliveryRates ? `出库起算 · 2 天内 ${measure(deliveryRate(k, 2), '%')}` : '妥投率待同步', 'clock', 'green'],
  ];
  $('#dashKpi').innerHTML = defs.map(row => metricCard(...row)).join('');

  drawOverviewMixes(d);

  if (trend.globalBasis !== dateBasis) {
    trend.globalBasis = trend.basis = dateBasis;
    document.querySelectorAll('#dailyBasis button').forEach(button =>
      button.setAttribute('aria-pressed', String(button.dataset.dateBasis === dateBasis)));
  }
  trend.created = d.daily;
  trend.shipped = d.dailyShipped;
  trend.paid = d.dailyPaid;
  trend.filters = f;
  void loadOverviewCharts(d);

  drawRank('cCarrier', d.byCarrier, 10, k.orders,
    ME.preview ? null : value => openTracking({ ...f, cr: value }));
  drawRank('cLastMile', d.byLastMile.map(r => ({ ...r, k: r.k || '未填写 C 端渠道', filter: r.k || '__empty__' })), 10, k.orders,
    ME.preview ? null : value => openTracking({ ...f, lastmile: value }));
  drawRank('cTrackingStatus', d.byTrackingStatus, 10, k.orders,
    ME.preview ? null : value => filterByTrackStatus(value, f));
  drawRank('cWarehouse', d.byWarehouse, 10, k.orders,
    ME.preview ? null : value => openTracking({ ...f, wh: value }));
  drawRank('cCountry', d.byCountry, 15, k.orders);
  drawCountryTiming(d.countryTiming, f);
  drawRank('cCurrentChannel', d.byCurrentChannel || [], 10, k.orders,
    ME.preview ? null : value => openTracking({ ...f, channel: value }));
  drawRank('cChannelGroup', (d.byChannelGroup || []).map(row => ({...row, filter: row.group || false})),
    Number.POSITIVE_INFINITY, k.orders, ME.preview ? null : group => openTracking({...f, channel_group: group}));
  drawDispatchStatus(d.byStatus);
}

function drawCountryTiming(rows, filters) {
  const host = $('#countryTiming');
  const sort = $('#countryTimingSort');
  sort.hidden = !rows?.length;
  if (!rows?.length) {
    host.innerHTML = `<p class="rank-empty tip">${rows ? '当前筛选无目的国家数据' : '当前快照暂无各国时效数据'}</p>`;
    return;
  }
  const metric = sort.value;
  const data = metric === 'orders' ? rows :
    [...rows].sort((left, right) => (right[metric] ?? -1) - (left[metric] ?? -1));
  const max = Math.max(1, ...rows.map(row => row.days || 0));
  const days = value => value == null ? '—' : `${value.toFixed(2)} 天`;
  host.innerHTML = `<div class="tblwrap country-timing-table"><table class="tbl">
    <thead><tr><th>目的国家</th><th>平均时效</th><th title="约 90% 的有效样本在此时长内妥投">P90 时效</th><th>有效时效样本</th><th>履约单量</th></tr></thead>
    <tbody>${data.map((row, index) => {
      const name = esc(row.name || row.code);
      return `<tr><td>${ME.preview ? `<b>${name}</b>` :
        `<button type="button" class="order-link" data-country-timing="${index}">${name}</button>`}</td>
        <td><span class="timing-value"><span class="rank-track" aria-hidden="true">
          <span style="width:${(row.days || 0) * 100 / max}%"></span></span>
          <strong>${days(row.days)}</strong></span></td>
        <td><strong>${days(row.p90)}</strong></td><td>${fmt(row.samples)}</td><td>${fmt(row.orders)}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  host.onclick = event => {
    const button = event.target.closest('[data-country-timing]');
    if (button) openCountryRegion(data[Number(button.dataset.countryTiming)].code, filters);
  };
  sort.onchange = () => drawCountryTiming(rows, filters);
}

async function loadOverviewCharts(d) {
  if (paintedOverview === d) return;
  const status = $('#overviewChartStatus');
  try {
    await loadChartLibrary();
  } catch {
    if (VIEW !== 'dash' || renderedDash !== d) return;
    const retry = el('button', 'btn ghost sm', '重试图表');
    retry.type = 'button';
    retry.onclick = () => loadOverviewCharts(d);
    status.replaceChildren('趋势图暂时无法加载。 ', retry);
    status.hidden = false;
    return;
  }
  if (VIEW !== 'dash' || renderedDash !== d || paintedOverview === d) return;
  status.hidden = true;
  $('#cMonthly').style.minWidth = `${d.monthly.length * 65 + 100}px`;
  const chart = initChart('cMonthly');
  chart.resize();
  chart.setOption({
    animation: chartMotion, animationDuration: 230, animationDurationUpdate: 140, animationEasing: 'quarticOut',
    grid: { left: 6, right: 10, top: 52, bottom: 6, containLabel: true },
    tooltip: { ...chartTip, trigger: 'axis', valueFormatter: fmt },
    legend: { right: 4, top: 0, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11, color: chartTheme.text } },
    xAxis: { type: 'category', data: d.monthly.map(r => r.m), axisTick: { show: false },
      axisLine: { lineStyle: { color: chartTheme.grid } }, axisLabel: { color: chartTheme.muted, hideOverlap: true } },
    yAxis: [{ type: 'value', splitLine: { lineStyle: { color: chartTheme.grid } },
      axisLabel: { color: chartTheme.muted, formatter: compactCount } },
    { type: 'value', min: 0, splitLine: { show: false },
      axisLabel: { color: chartTheme.muted, formatter: '{value} 天' } }],
    series: [
      { name: '单量', type: 'bar', barMaxWidth: 48, data: d.monthly.map(r => r.n),
        itemStyle: { color: chartTheme.series[0], borderRadius: [3, 3, 0, 0] },
        label: { show: true, position: 'insideBottom', distance: 7, color: chartTheme.ink, fontSize: 11,
          formatter: point => fmt(point.value) },
        labelLayout: { hideOverlap: true },
        tooltip: { valueFormatter: value => `${fmt(value)} 单` } },
      { name: '平均物流天数', type: 'line', smooth: 0.35, yAxisIndex: 1, symbolSize: 6, data: d.monthly.map(r => r.d),
        itemStyle: { color: chartTheme.ink }, lineStyle: { width: 1.5 },
        label: { show: true, position: 'top', distance: 22, fontSize: 11, color: chartTheme.ink,
          formatter: point => measure(point.value, ' 天') },
        labelLayout: { hideOverlap: true },
        tooltip: { valueFormatter: value => measure(value, ' 天') } },
    ],
  });
  drawDailyVolume();
  paintedOverview = d;
}

function drawOverviewMixes(data) {
  const shipping = data.warehouseShipping;
  const orders = data.orderSplit;
  if (shipping) {
    drawMix('warehouseShippingMix', shipping.overseas, shipping.total,
      `已出库合计 ${fmt(shipping.total)} 单`,
      [
        ['海外仓', shipping.overseas, 'primary'], ['国内仓', shipping.domestic, 'secondary'],
        ...(shipping.unknown ? [['仓库未归类', shipping.unknown, 'unknown']] : []),
      ], shipping.total, { donut: true });
  } else {
    $('#warehouseShippingMix').textContent = '等待快照同步';
  }
  if (orders) {
    drawMix('orderSplitMix', orders.split, orders.total,
      `${fmt(orders.split)} / ${fmt(orders.total)} 个订单拆单`,
      [
        ['同仓拆单', orders.same, 'teal'], ['异仓拆单', orders.cross, 'primary'],
        ...(orders.unknown ? [['仓库待确认', orders.unknown, 'unknown']] : []),
      ], orders.split, { note: '构成占比以拆单订单为分母' +
        (orders.missingFulfillments ? ` · ${fmt(orders.missingFulfillments)} 个履约单缺订单号` : '') });
  } else {
    $('#orderSplitMix').textContent = '等待快照同步';
  }
}

function drawMix(id, numerator, denominator, summary, rows, groupTotal, {donut = false, note = ''} = {}) {
  const percentage = (n, total) => measure(total ? 100 * n / total : null, '%');
  const segments = rows.filter(([, n]) => n > 0);
  const rate = percentage(numerator, denominator);
  const list = `<ul class="mix-list">${rows.map(([name, n, tone]) => `<li>
      <span class="mix-key mix-${tone}" aria-hidden="true"></span><span>${esc(name)}</span><strong>${fmt(n)} 单</strong>
      <span class="mix-percentage">${percentage(n, groupTotal)}</span></li>`).join('')}</ul>`;
  if (donut) {
    let start = 0;
    const arcs = groupTotal ? segments.map(([, n, tone]) => {
      const length = 100 * n / groupTotal;
      const offset = -start;
      start += length;
      return `<circle class="mix-ring-${tone}" cx="50" cy="50" r="44" pathLength="100"
        stroke-dasharray="${length} 100" stroke-dashoffset="${offset}"/>`;
    }).join('') : '';
    $(`#${id}`).innerHTML = `<div class="mix-donut-layout">
      <div class="mix-donut" role="img" aria-label="海外仓发货占比 ${rate}">
        <svg class="mix-ring" viewBox="0 0 100 100" fill="none" stroke-width="8" aria-hidden="true">
          <g transform="rotate(-90 50 50)"><circle class="mix-ring-track" cx="50" cy="50" r="44"/>${arcs}</g>
        </svg><strong>${rate}</strong></div>
      <div class="mix-details">${list}<p class="tip mix-note">${esc(summary)}</p></div>
    </div>`;
    return;
  }
  const bar = segments.map(([name, n, tone]) =>
    `<span class="mix-${tone}" style="flex:${n} 0 0" title="${esc(name)} ${percentage(n, groupTotal)}"></span>`).join('');
  $(`#${id}`).innerHTML = `
    <div class="mix-summary"><strong>${rate}</strong><span class="tip">${esc(summary)}</span></div>
    <div class="mix-track" aria-hidden="true">${bar}</div>
    ${list}
    ${note ? `<p class="tip mix-note">${esc(note)}</p>` : ''}`;
}

function drawDispatchStatus(rows) {
  drawRank('cStatus', rows, rows.length, rows.reduce((total, row) => total + row.n, 0));
  const toggle = $('#statusMore');
  const rest = [...document.querySelectorAll('#cStatus li')].filter((_, i) => i >= 5 && rows[i].k !== '已取消');
  toggle.hidden = !rest.length;
  const show = expanded => {
    rest.forEach(row => { row.hidden = !expanded; });
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.textContent = expanded ? '收起状态' : `展开其余 ${rest.length} 项`;
  };
  show(toggle.getAttribute('aria-expanded') === 'true');
  toggle.onclick = () => show(toggle.getAttribute('aria-expanded') !== 'true');
}

function drawDailyVolume() {
  if (!renderedDash || !window.echarts) return;
  const clock = day => Date.parse(day + 'T00:00:00Z');
  const dayMs = 86_400_000;
  const series = trend[trend.basis] || [];
  const label = dateBasisLabel[trend.basis];
  const last = series.at(-1);
  const latest = last ? clock(last.day) : 0;
  const days = trend.span ? series.filter(row => clock(row.day) >= latest - (trend.span - 1) * dayMs) : series;
  $('#dailyNote').hidden = ME.preview || !days.length;
  $('#dailyNote').textContent = `点击按${label}查看当天的履约单`;
  $('#cDaily').hidden = !days.length;
  $('#dailyRange').textContent = days.length ?
    `${days[0].day} 至 ${days.at(-1).day} · 按${label}统计（北京时间）` :
    !trend[trend.basis] ? `当前快照暂无${label}日单量` : '当前筛选无履约单';
  if (!days.length) {
    $('#dailyFocus').textContent = '';
    return;
  }
  const chart = initChart('cDaily');
  resizeVisibleCharts();
  const peak = days.reduce((max, row) => !max || row.n > max.n ? row : max, null);
  const mean = days.reduce((sum, row) => sum + row.n, 0) / days.length;
  $('#dailyFocus').innerHTML = `有单日均 <b>${fmt(mean)}</b> 单 · 峰值 <b>${fmt(peak.n)}</b> 单`;
  const values = days.map(row => [clock(row.day), row.n]);
  chart.setOption({
    useUTC: true, animation: chartMotion, animationDuration: 230, animationDurationUpdate: 140,
    animationEasing: 'quarticOut',
    grid: { top: 20, right: 18, bottom: 3, left: 6, containLabel: true },
    tooltip: { ...chartTip, trigger: 'axis', formatter: points => {
      const point = points.find(row => row.seriesIndex === 0);
      return point ? `${new Date(point.value[0]).toISOString().slice(0, 10)}<br><b>${fmt(point.value[1])}</b> 单` : '';
    } },
    xAxis: { type: 'time', splitNumber: 6, axisLine: { lineStyle: { color: chartTheme.grid } },
      axisTick: { show: false }, splitLine: { show: false },
      axisLabel: { color: chartTheme.muted, formatter: '{MM}/{dd}', hideOverlap: true },
      min: days.length === 1 ? latest - dayMs / 2 : null,
      max: days.length === 1 ? latest + dayMs / 2 : null },
    yAxis: { type: 'value', min: 0, max: value => Math.max(1, Math.ceil(value.max * 1.10)),
      splitNumber: 4, splitLine: { lineStyle: { color: chartTheme.grid } },
      axisLabel: { color: chartTheme.muted, formatter: compactCount } },
    series: [
      { type: 'bar', name: '日单量', data: values, barWidth: days.length > 90 ? 2 : '62%', barMaxWidth: 28,
        itemStyle: { borderRadius: [3, 3, 0, 0],
          color: point => point.value[0] === clock(peak.day) ? chartTheme.series[2] : chartTheme.series[0] },
        emphasis: { itemStyle: { color: chartTheme.series[1] } },
        markPoint: {
          symbol: 'circle', symbolSize: 8, silent: true,
          itemStyle: { color: chartTheme.ink, borderColor: chartTheme.paper, borderWidth: 1.5 },
          label: { show: true, position: 'top', color: chartTheme.ink, fontWeight: 700,
            formatter: () => fmt(peak.n) },
          data: [{ coord: [clock(peak.day), peak.n] }],
        } },
    ],
  });
  const pointer = chart.getZr();
  pointer.off('click');
  if (ME.preview) return;
  pointer.on('click', event => {
    const point = [event.offsetX, event.offsetY];
    if (!chart.containPixel('grid', point)) return;
    const date = chart.convertFromPixel({ xAxisIndex: 0 }, point[0]);
    const row = days.find(day => Math.abs(clock(day.day) - date) <= dayMs / 2);
    if (row) {
      const {start, end} = trend.filters;
      openTracking({ ...trend.filters, date_basis: trend.basis,
        start: start?.startsWith(row.day) ? start : row.day,
        end: end?.startsWith(row.day) ? end : row.day });
    }
  });
}

function drawRank(id, rows, limit, total, onClick) {
  const host = document.getElementById(id);
  const data = rows.slice(0, limit);
  if (!data.length) {
    host.innerHTML = '<p class="rank-empty tip">当前筛选无数据</p>';
    return;
  }
  host.innerHTML = `<ol class="rank-list">${data.map((row, index) => {
    const name = esc(row.k || '未填写');
    const share = total ? 100 * row.n / total : 0;
    const percent = share > 0 && share < 0.1 ? '&lt;0.1%' : `${fmt1(share)}%`;
    const content = `<span class="rank-name" title="${name}">${name}</span><span class="rank-track" aria-hidden="true">
      <span style="width:${share}%"></span></span><strong class="rank-value">${fmt(row.n)}</strong>
      <span class="rank-share">${percent}</span>`;
    return onClick && row.k && row.filter !== false ?
      `<li><button type="button" class="rank-row" data-rank="${index}">${content}</button></li>` :
      `<li class="rank-row">${content}</li>`;
  }).join('')}</ol>`;
  if (onClick) host.querySelectorAll('button[data-rank]').forEach(button => {
    const row = data[Number(button.dataset.rank)];
    button.onclick = () => onClick(row.filter || row.k);
  });
}

/* ================= 轨迹监控 ================= */
let TF = null;
let AF = null;
const alertFiltersByMode = {};
let trackRows = [];
let trackPage = 1;
let trackPageSize = 40;
const fulfillmentPageSizes = [20, 40, 100, 200];
let renderedTrack = null;
let alertRows = [];
let alertPage = 1;
const alertPageSize = {alerts: 40, archive: 40};
const trackingGroups = () => [...new Map(META.trackStatuses.map(row => [row.status, row.label]))]
  .map(([v, t]) => ({ v, t }));
const ruleOptions = () => META.ruleOptions || META.rules.map(row => ({ v: row.rule, t: row.rule_cn }));
const blankRow = (cols, message) => `<tr><td colspan="${cols}" class="empty">${message}</td></tr>`;
const orderButton = (no, tracking) => `<button type="button" class="order-link mono" data-order="${esc(no)}" aria-haspopup="dialog" aria-controls="drawer">${esc(no)}</button>
  <small class="cell-sub mono">${esc(tracking || '—')}</small>`;
const trackStyles = {
  Delivered: ['green', 'check'], InTransit: ['cyan', 'truck'], OutForDelivery: ['blue', 'pin'],
  AvailableForPickup: ['cyan', 'box'], DeliveryFailure: ['danger', 'alert'], Exception: ['warn', 'alert'],
  Expired: ['danger', 'clock'], InfoReceived: ['violet', 'scan'], NotFound: ['blue', 'scan'],
};
const statusPill = (label, [tone, name]) =>
  `<span class="status-pill tone-${tone}">${icon(name)}${esc(label)}</span>`;
const trackBadge = (status, label, sub, missing, fallback = '其他履约单 · 无本地结果') => status
  ? `${statusPill(label || status, Object.hasOwn(trackStyles, status) ? trackStyles[status] : ['blue', 'route'])}
     ${sub ? `<small class="cell-sub mono">${esc(sub)}</small>` : ''}`
  : `<span class="badge gray">${missing ? 'API 账号登记未匹配' : esc(fallback)}</span>`;
const coverageOptions = [
  ['detailed', '有逐条承运商事件'],
  ['tracked', '有 17TRACK 查询记录'],
  ['unmatched', 'API 账号登记未匹配'],
  ['queried_empty', '已查询 · 未返回轨迹'],
  ['signed_unstored', 'WMS 已签收 · 待核实'],
  ['signed_time_invalid', 'WMS 签收时间异常'],
  ['carrier_conflict', '承运商待核对'],
  ['shipped_unstored', '已出库 · 待同步轨迹'],
  ['other_unstored', '其他履约单 · 无本地结果'],
];

function setTrackMode(mode) {
  if (ME?.preview) mode = 'tracking';
  if (trackMode !== mode && mode !== 'tracking') {
    $('#alertBody').replaceChildren();
    $('#alertChips').replaceChildren();
    $('#alertPager').replaceChildren();
    alertRows = [];
  }
  trackMode = mode;
  if (mode !== 'tracking') AF = alertFiltersByMode[mode] || null;
  $('#trackingPane').hidden = mode !== 'tracking';
  $('#alertsPane').hidden = mode === 'tracking' || mode === 'special-tracking';
  $('#specialTrackingPane').hidden = mode !== 'special-tracking';
  const archived = mode === 'archive';
  $('#alertFilters').hidden = archived;
  $('#archiveFilters').hidden = !archived;
  $('#alertListTitle').textContent = archived ? '归档列表' : '预警处理清单';
  $('#alertListNote').textContent = archived ? '按归档时间从近到远' :
    '按出库时间从近到远 · Postnet 预警暂缓显示';
  $('#alertBatch').hidden = $('#alertSelectColumn').hidden = archived;
  $('#alertHandlerColumn').hidden = !archived;
  document.querySelectorAll('#trackTabs button').forEach(btn =>
    btn.setAttribute('aria-pressed', String(btn.dataset.mode === mode)));
}

function updateSubstates(filters) {
  const current = filters.sub.value;
  const entries = META.trackStatuses.filter(row => !filters.tstat.value || filters.tstat.value === row.status);
  filters.sub.replaceChildren(new Option('子状态：全部', ''),
    ...entries.map(row => new Option(` ${row.sub_label || row.sub} · ${row.sub}`, row.sub)));
  if (entries.some(row => row.sub === current)) filters.sub.value = current;
}

const trackingLookup = () => ({type: 'text', label: '履约单号 / 运单号 / 订单号 / 平台订单号',
  key: 'q', w: 275, submit: true, auto: !ME.preview});

function monitoringFilterDefs(alerts = false) {
  const common = commonDateFilters();
  if (alerts) {
    const channels = META.alertChannels || [];
    Object.assign(common.find(def => def.key === 'channel'), {
      label: '当前物流渠道', options: channels, total: channels.reduce((sum, channel) => sum + channel.n, 0),
    });
  }
  return [
    trackingLookup(),
    ...common,
    { type: 'select', label: '17TRACK 状态', key: 'tstat', options: trackingGroups() },
    { type: 'select', label: '子状态', key: 'sub' },
  ];
}

let trackFilterTimer;
function prepareTrack() {
  if (TF) return;
  if (ME.preview) {
    TF = makeFilters($('#trackFilters'), [trackingLookup()], () => run(loadTrack));
    TF._info.setAttribute('role', 'status');
    return;
  }
  TF = makeFilters($('#trackFilters'), [
    ...monitoringFilterDefs(),
    { type: 'select', label: '主预警', key: 'rule', options: ruleOptions() },
    { type: 'select', label: '处理结果', key: 'resolved_only', options: [{v: 'true', t: '已标记解决'}] },
    { type: 'select', label: '轨迹覆盖', key: 'coverage',
      options: coverageOptions.map(([v, t]) => ({ v, t })) },
    { type: 'select', label: '排序', key: 'sort', options: ['创建时间', '签出时间', '停滞天数', '逐条事件数'] },
  ], (_, action) => {
    updateDateOptions(TF, TF.date_basis.value);
    updateSubstates(TF);
    clearTimeout(trackFilterTimer);
    if (action === 'search') run(() => loadTrack(1, true));
    else trackFilterTimer = setTimeout(() => {
      if (VIEW === 'track' && trackMode === 'tracking') run(() => loadTrack(1));
    }, 250);
  });
  updateDateOptions(TF, TF.date_basis.value);
  updateSubstates(TF);
  attachFilterPresets('tracking', '运单轨迹', $('#trackFilters'), TF, {onApply:() => {
    clearTimeout(trackFilterTimer);
    run(loadTrack);
  }});
  TF._info.setAttribute('role', 'status');
}

function openTracking(values = {}) {
  if (!ME.preview && !META) return run(async () => { await loadMeta(); openTracking(values); });
  prepareTrack();
  if (!ME.preview) {
    TF.date_basis.value = values.date_basis || DEFAULT_DATE_BASIS;
    updateDateOptions(TF, TF.date_basis.value);
  }
  Object.entries(TF).forEach(([key, control]) => {
    if (key !== '_info' && key !== 'date_basis') control.value = values[key] || '';
  });
  if (!ME.preview) updateSubstates(TF);
  syncFilterPreset(TF);
  setTrackMode('tracking');
  showView('track');
}

function filterByTrackStatus(label, scope = {}) {
  if (!META) return run(async () => { await loadMeta(); filterByTrackStatus(label, scope); });
  const state = trackingGroups().find(row => row.t === label);
  const coverage = coverageOptions.find(([, name]) => name === label)?.[0];
  if (state || coverage) openTracking({ ...scope, tstat: state?.v, coverage });
}

let lastTrackingQuery = '';
let trackSummaryPath = '';
let trackSummaryUntil = 0;
let trackSummaryGeneration = -1;

async function loadTrackFilters() {
  if (TF) return;
  const host = $('#trackFilters');
  if (!ME.preview && !META) {
    host.textContent = '正在载入筛选项…';
    try {
      await loadMeta();
    } catch {
      const retry = el('button', 'btn ghost sm', '重试加载筛选项');
      retry.type = 'button';
      retry.onclick = () => run(loadTrackFilters);
      host.replaceChildren(retry);
      return;
    }
  }
  prepareTrack();
  $('#trackBody').querySelectorAll('[data-platform]').forEach(node => {
    node.textContent = platformName(node.dataset.platform);
  });
}

function trackRowsOnly(filters) {
  return !filters.tstat && !filters.sub && !filters.rule && !filters.coverage &&
    ['签出时间', '创建时间', '履约单号', '解决时间'].includes(filters.sort);
}

function trackRowsPath(filters, page, size) {
  return '/api/track?' + qs({...filters, part: 'rows'}, page, size);
}

let trackAnalysisRequest;
let trackAnalysisScope;

function renderTrackAnalysis(report) {
  const stats = report.summary;
  const stamp = value => value?.replace('T', ' ').slice(0, 16) || '—';
  const day = value => value.slice(0, 10);
  const range = stats.first_ship ? `${day(stats.first_ship)} 至 ${day(stats.last_ship)} 出库` : '无出库时间';
  const percent = (n, total) => total ? `${fmt1(100 * n / total)}%` : '—';
  const details = {
    scan: row => `${fmt(row.affected)} / ${fmt(row.total)} 单 · ${percent(row.affected, row.total)}` +
      (row.hours == null ? '' : ` · 已有扫描的中位数 ${fmt1(row.hours)} 小时`) +
      (row.missing ? ` · ${fmt(row.missing)} 单仍无实物扫描` : '') + ` · ${fmt(row.open)} 单未确认妥投`,
    stale: row => `${fmt(row.affected)} 单未确认妥投 · 最短断更 ${fmt1(row.hours)} 小时 · 最后扫描 ${stamp(row.first_seen)} 至 ${stamp(row.last_seen).slice(-5)}`,
    station: row => `超 72 小时 ${fmt(row.affected)} / ${fmt(row.total)} 单 · ${percent(row.affected, row.total)}` +
      `（此前 8 天 ${percent(row.baseline_affected, row.baseline_total)}） · 中位数 ${fmt1(row.hours)} 小时`,
    exception: row => `${fmt(row.affected)} 单曾出现 · ${fmt(row.open)} 单未确认妥投`,
  };
  const groups = ['stale', 'scan', 'station', 'exception'].map(key => report.groups.find(group => group.name === key));
  const metrics = [['匹配履约单', stats.total], ['有逐条轨迹', stats.tracked],
    ['17TRACK 已妥投', stats.confirmed], ['未妥投且断更 ≥72 小时', stats.stale]];
  $('#trackAnalysisStatus').textContent = report.aiAvailable ? '' : '当前显示本机统计。智能中枢 AI 解读暂不可用。';
  $('#trackAnalysisResult').innerHTML = `
    <div class="track-analysis-metrics">${metrics.map(([label, value]) =>
      `<div><span>${esc(label)}</span><strong>${fmt(value)}</strong></div>`).join('')}</div>
    <p class="tip track-analysis-window">${esc(range)} · 最近轨迹同步 ${esc(stamp(stats.checked))} · 分析于 ${esc(stamp(report.asOf))}（北京时间）</p>
    ${stats.tracked ? groups.map(group => `
      <section class="track-analysis-group"><h3>${esc(group.label)}</h3>
        ${group.rows.length ? group.rows.map(row => {
          const period = group.name === 'scan' ? stamp(row.first_shipped) +
            (stamp(row.first_shipped) === stamp(row.last_shipped) ? '' : ` 至 ${stamp(row.last_shipped).slice(-5)}`) : day(row.period);
          const origin = group.name === 'scan' || group.name === 'station' ? '出库' : '轨迹';
          const parts = [`${period} ${origin}`, row.carrier_name, row.warehouse, row.channel, row.place, row.stage, row.issue];
          return `<div class="track-analysis-row">
            <strong>${esc(parts.filter(Boolean).join(' · '))}</strong>
            <span>${esc(details[group.name](row))}</span>
            <div class="track-analysis-examples" aria-label="样单">${row.examples.map(no =>
              `<button type="button" class="order-link mono" data-track-analysis-order="${esc(no)}">${esc(no)}</button>`).join('')}</div>
          </div>`;
        }).join('') : '<p class="tip">当前筛选下未发现达到批次判定条件的记录。</p>'}
      </section>`).join('') : '<p class="tip">筛选范围内没有可分析的逐条轨迹。</p>'}
    ${stats.tracked ? '<p class="tip track-analysis-window">首条实物扫描排除面单创建和待接收通知。各类异常可能重叠。</p>' : ''}`;
  $('#interpretTrackAnalysis').hidden = !report.aiAvailable || !stats.tracked;
}

async function analyzeTrack({interpret = false} = {}) {
  trackAnalysisRequest?.abort();
  const controller = new AbortController();
  trackAnalysisRequest = controller;
  const status = $('#trackAnalysisStatus');
  const reading = $('#trackAnalysisReading');
  const text = $('#trackAnalysisText');
  const refresh = $('#refreshTrackAnalysis');
  const ai = $('#interpretTrackAnalysis');
  refresh.disabled = ai.disabled = true;
  if (interpret) {
    reading.hidden = false;
    text.textContent = '正在生成 AI 解读…';
    reading.scrollIntoView({block: 'nearest'});
  } else {
    reading.hidden = ai.hidden = true;
    $('#trackAnalysisResult').replaceChildren();
    status.textContent = '正在归并当前筛选范围的逐条轨迹…';
  }
  try {
    const report = await api('/api/track-analysis' + (interpret ? '?interpret=true' : ''), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(trackAnalysisScope), signal: controller.signal,
    });
    if (trackAnalysisRequest !== controller) return;
    renderTrackAnalysis(report);
    if (interpret) text.textContent = report.interpretation || '当前范围没有可解读的轨迹。';
  } catch (error) {
    if (controller.signal.aborted) return;
    (interpret ? text : status).textContent = error.message;
  } finally {
    if (trackAnalysisRequest === controller) refresh.disabled = ai.disabled = false;
  }
}

function openTrackAnalysis() {
  const {sort, ...selection} = readF(TF || {});
  trackAnalysisScope = Object.fromEntries(new URLSearchParams(qs(selection)));
  const scopeItems = [
    ...(selection.pf || []).map(platformName),
    ...(selection.lastmile || []),
    ...(selection.month || []),
    ...(selection.week || []),
    [selection.start, selection.end].filter(Boolean).join(' 至 '),
  ].filter(Boolean);
  $('#trackAnalysisScope').textContent = ['当前筛选 · 全部匹配订单', ...scopeItems].join(' · ');
  $('#trackAnalysisDialog').showModal();
  run(analyzeTrack);
}

async function loadTrackSummary(filters, available = null) {
  const {sort, ...scope} = filters;
  const path = '/api/track?' + qs({...scope, part: 'summary'});
  const host = $('#trackChips');
  if (trackSummaryPath !== path) {
    host.replaceChildren();
    trackSummaryUntil = 0;
  }
  trackSummaryPath = path;
  const generation = readGeneration;
  if (!available && trackSummaryGeneration === generation && trackSummaryUntil > performance.now()) return;
  try {
    if (available) pendingRequests.delete('trackSummary');
    const data = available || await latest('trackSummary', path, {signal: AbortSignal.timeout(18_000)});
    if (!data || path !== trackSummaryPath || VIEW !== 'track' || trackMode !== 'tracking') return;
    trackSummaryUntil = performance.now() + 25_000;
    trackSummaryGeneration = generation;
    host.innerHTML = (data.statusSummary || []).slice(0, 7).map(row =>
      `<button type="button" class="chipstat" data-track-status="${esc(row.状态)}">${esc(row.状态)} <b>${fmt(row.n)}</b></button>`).join('');
  } catch {
    if (path === trackSummaryPath && VIEW === 'track' && trackMode === 'tracking') {
      const retry = el('button', 'btn ghost sm', '重试加载状态统计');
      retry.type = 'button';
      retry.onclick = () => run(() => loadTrackSummary(filters));
      host.replaceChildren(retry);
    }
  }
}

async function loadTrack(page = 1, fromSearch = false, size = trackPageSize) {
  if (!TF && !ME.preview && defaultFilterPreset('tracking')) {
    await loadTrackFilters();
    if (!TF || VIEW !== 'track' || trackMode !== 'tracking') return;
  } else if (!TF) run(loadTrackFilters);
  const filters = readF(TF || {});
  let f = {sort: filters.resolved_only ? '解决时间' : '签出时间', ...filters};
  const query = f.q?.trim();
  const {q, sort, ...scope} = f;
  const status = $('#trackListStatus');
  const retry = $('#retryTrack');
  retry.hidden = true;
  retry.onclick = () => run(() => loadTrack(page, fromSearch, size));
  status.textContent = page > 1 ? `正在加载第 ${page} 页…` : query ? '正在搜索…' : ME.preview ? '' : '正在读取履约单…';
  const pageLabel = $('#trackPager > span');
  if (pageLabel && page > 1) pageLabel.textContent = status.textContent;
  const fetchRows = (filters, number) => latest('track', trackRowsPath(filters, number, size),
    {signal: AbortSignal.timeout(18_000)});
  let d, clearedFilters = false;
  try {
    if (ME.preview && !query) {
      pendingRequests.delete('track');
      d = {rows: [], total: 0, page: 1, size};
    } else {
      d = await fetchRows(f, page);
    }
    if (!d || VIEW !== 'track' || trackMode !== 'tracking') return;
    if (fromSearch && query && !d.total && !scope.channel_group?.length && !scope.resolved_only &&
        Object.keys(scope).some(key => key !== 'date_basis')) {
      const broad = await fetchRows({q, sort}, 1);
      if (!broad || VIEW !== 'track' || trackMode !== 'tracking') return;
      if (broad.total) {
        for (const key of Object.keys(scope)) TF[key].value = key === 'date_basis' ? DEFAULT_DATE_BASIS : '';
        updateDateOptions(TF, DEFAULT_DATE_BASIS);
        updateSubstates(TF);
        syncFilterPreset(TF);
        clearedFilters = true;
        f = {q, sort};
        d = broad;
      }
    }
  } catch (error) {
    if (VIEW === 'track' && trackMode === 'tracking') {
      status.textContent = error.name === 'TimeoutError' ? '列表加载超时' : '列表加载失败';
      if (pageLabel && page > 1) pageLabel.textContent = status.textContent;
      retry.hidden = false;
    }
    return;
  }
  const lastPage = Math.max(1, Math.ceil(d.total / d.size));
  if (d.page > lastPage) {
    return loadTrack(lastPage, false, d.size);
  }
  trackRows = d.rows;
  trackPage = d.page;
  trackPageSize = d.size;
  lastTrackingQuery = query || '';
  status.textContent = ME.preview && !query ? '' :
    `${d.snapshotFiltersSkipped ? '离线快照暂不可应用当前筛选 · ' :
      clearedFilters ? '已切换至全部履约单搜索 · ' : ''}共 ${fmt(d.total)} 单`;
  if (!ME.preview) run(() => loadTrackSummary(f, !d.total ? d : null));
  pager($('#trackPager'), d.total, d.page, d.size, p => run(() => loadTrack(p)), {
    sizes:fulfillmentPageSizes, changeSize:n => run(() => loadTrack(1, false, n)),
  });
  if (renderedTrack === d) return;
  renderedTrack = d;

  $('#trackBody').innerHTML = d.rows.length ? d.rows.map(r => `
    <tr><td>${orderButton(r.履约单号, r.快递单号)}</td>
      <td>${esc(r.当前渠道 || '—')}<small class="cell-sub">${esc(r.物流商 || '—')} / ${esc(r.C端承运商 || '—')}</small></td>
      <td>${esc(r.仓库 || '—')}<small class="cell-sub">${esc(r.目的国中文 || '—')} · <span data-platform="${esc(r.平台 || '')}">${esc(r.平台名称 || platformName(r.平台))}</span></small></td>
      <td>${trackBadge(r.轨迹状态, r.轨迹状态中文, r.轨迹子状态, r.未注册 && !r.状态标签,
        r.状态标签 || (
        r.回补状态 === 'empty' ? '已查询 · 未返回轨迹' :
        r.已签收 ? 'WMS 已签收 · 待核实' :
        r.签出时间 ? '已出库 · 待同步轨迹' : undefined))}</td>
      ${ME.preview ? '' : `<td>${trackingAlerts(r)}</td>`}
      <td class="mono">${dt(r.签出时间)}</td>
      <td>${durationText(r.运输分钟)}</td>
      <td><span class="cell-truncate" title="${esc(r.最新描述 || '')}">${esc(r.最新描述 || '—')}</span>
        <small class="cell-sub mono">${r.最新事件当地时间 || r.最新事件时间 ? esc(trackTime(r.最新事件当地时间, r.最新事件时间)) : '—'}${r.逐条事件数 ? ` · ${fmt(r.逐条事件数)} 条事件` : ''}</small></td></tr>`).join('') :
    blankRow(ME.preview ? 7 : 8, ME.preview && !query ? '输入完整单号查询物流轨迹' :
      f.resolved_only && !d.snapshotFiltersSkipped ? '没有符合条件的已标记解决履约单' : '没有匹配的履约单');
  $('#trackBody').closest('.tblwrap').scrollTop = 0;
  if (d.rows.length && (query ? d.total === 1 : d.page === 1)) {
    const selection = pendingRequests.get('track');
    const generation = readGeneration;
    setTimeout(() => {
      if (VIEW === 'track' && trackMode === 'tracking' && selection === pendingRequests.get('track') &&
          generation === readGeneration && !drawerOrder) {
        preloadOrder(d.rows[0].履约单号, 'low');
      }
    }, query ? 0 : 250);
  }
  if (!ME.preview && !query && trackRowsOnly(f) && d.page * d.size < d.total) {
    const selection = pendingRequests.get('track');
    const generation = readGeneration;
    const next = trackRowsPath(f, d.page + 1, d.size);
    setTimeout(() => {
      if (VIEW === 'track' && trackMode === 'tracking' && !document.hidden &&
          selection === pendingRequests.get('track') && generation === readGeneration) {
        readPage(next, {signal: AbortSignal.timeout(18_000), priority: 'low'}).catch(() => {});
      }
    }, 100);
  }
}

function prepareAlerts() {
  AF = alertFiltersByMode[trackMode] || null;
  if (AF) return;
  const archived = trackMode === 'archive';
  const host = $(archived ? '#archiveFilters' : '#alertFilters');
  const refs = makeFilters(host, [
    ...monitoringFilterDefs(true),
    { type: 'select', label: '预警规则', key: 'rule', options: ruleOptions() },
    { type: 'select', label: '级别', key: 'level', options: ['紧急', '高', '中'] },
    { type: 'select', label: '命中范围', key: 'main_only', options: [{ v: 'true', t: '仅主预警' }] },
  ], () => { updateDateOptions(refs, refs.date_basis.value); updateSubstates(refs); run(loadAlerts); });
  AF = alertFiltersByMode[trackMode] = refs;
  updateDateOptions(refs, refs.date_basis.value);
  updateSubstates(refs);
  attachFilterPresets(archived ? 'archive' : 'alerts', archived ? '归档列表' : '预警处理', host, refs, {onApply:() => run(loadAlerts)});
}

function refreshSelection() {
  const selected = $('#alertBody').querySelectorAll('[data-alert-index]:checked');
  $('#selectedAlerts').textContent = selected.length ? `已选 ${selected.length} 条` : '';
  $('#selectPage').checked = Boolean(alertRows.length && alertRows.length === selected.length);
  $('#selectPage').indeterminate = selected.length > 0 && selected.length < alertRows.length;
  $('#selectPage').disabled = !ME.can_edit || !alertRows.length;
  $('#applyBatch').disabled = !ME.can_edit || !selected.length || !$('#batchAction').value;
}

const alertStyles = {
  signout_timeout: ['violet', 'warehouse'], not_online: ['warn', 'scan'],
  transport_timeout: ['danger', 'clock'], no_update: ['violet', 'history'],
  stagnation: ['warn', 'history'], customs_hold: ['violet', 'globe'],
  delivery_failure: ['danger', 'pin'], returning: ['danger', 'return'],
  carrier_exception: ['warn', 'alert'],
};
const alertStyle = rule => Object.hasOwn(alertStyles, rule) ? alertStyles[rule] : ['blue', 'alert'];

function trackingAlerts(row) {
  const current = row.主预警名称 ? `<span class="badge ${levelTone(row.主预警级别)}">${esc(row.主预警名称)}</span>
    ${row.预警数 > 1 ? `<small class="cell-sub">另有 ${fmt(row.预警数 - 1)} 条待处理</small>` : ''}` : '';
  const past = row.历史预警;
  const previous = past ? `<small class="cell-sub"><span class="badge ok">${past.state === 'resolved' ? '已解决' : '已不再命中'}</span>
    ${esc(past.rule_cn || past.rule)}${past.total > 1 ? ` · 共 ${fmt(past.total)} 条` : ''}
    ${past.handled_by ? ` · ${esc(past.handled_by)}` : ''}${past.handle_at ? ` · ${esc(syncTime(past.handle_at))}` : ''}</small>` : '';
  return current + previous || '—';
}

async function loadAlerts(page = 1) {
  const mode = trackMode;
  const archived = mode === 'archive';
  await loadMeta();
  if (VIEW !== 'track' || trackMode !== mode) return;
  prepareAlerts();
  const params = qs({ ...readF(AF), view: archived ? 'archive' : 'pending' }, page, alertPageSize[mode]);
  const d = await latest('alerts', '/api/alerts?' + params);
  if (!d || VIEW !== 'track' || trackMode !== mode) return;
  if (!d.rows.length && d.total && page > 1) return loadAlerts(Math.ceil(d.total / d.size));
  alertRows = d.rows;
  alertPage = d.page;
  AF._info.textContent = `共 ${fmt(d.total)} 条${archived ? '归档' : '待处理'}`;
  const categories = $('#alertChips');
  const restoreFocus = categories.contains(document.activeElement) ? document.activeElement.dataset.alertRule : '';
  categories.innerHTML = d.summary.slice(0, 7).map(r => {
    const [tone, name] = alertStyle(r.rule);
    return `<button type="button" class="chipstat alert-category tone-${tone}" data-alert-rule="${esc(r.rule)}" aria-pressed="${AF.rule.value === r.rule}">
      ${sectionIcon(name, tone)}<span>${esc(r.rule_cn)}<b>${fmt(r.n)}</b></span></button>`;
  }).join('');
  if (restoreFocus) {
    const button = [...categories.querySelectorAll('button')].find(item => item.dataset.alertRule === restoreFocus);
    (button || AF.rule).focus({preventScroll: true});
  }
  $('#alertBody').innerHTML = d.rows.length ? d.rows.map((r, index) => `
    <tr>${archived ? '' : `<td class="check"><input type="checkbox" data-alert-index="${index}" aria-label="选择这条预警" ${ME.can_edit ? '' : 'disabled'}></td>`}
      <td>${orderButton(r.order_no, r.tracking_no)}</td>
      <td class="mono">${esc(r.shipped_at || r.shipped_date || '—')}</td>
      <td>${durationText(r.transport_minutes)}</td>
      <td><span class="badge ${levelTone(r.level)}">${esc(r.level)}</span> ${statusPill(r.rule_cn, alertStyle(r.rule))}<small class="cell-sub">${r.is_main ? '主预警' : '次预警'} · ${esc(r.category || '—')}</small></td>
      <td>${esc(r.current_channel || '—')}</td>
      <td>${esc(r.carrier || '—')}<small class="cell-sub">${esc(r.country || '—')}</small></td>
      <td>${trackBadge(r.track_status, trackingGroups().find(row => row.v === r.track_status)?.t, r.track_sub)}</td>
      <td>${measure(r.days, ' 天')}</td>
      ${archived ? `<td>${esc(r.handled_by || '—')}<small class="cell-sub">${esc(syncTime(r.handle_at))}</small>
        ${allowedActions().some(([action]) => action === 'archive') ? `<button class="btn ghost sm" type="button" data-undo-archive="${index}">撤销归档</button>` : ''}</td>` : ''}
    </tr>`).join('') : blankRow(9, archived ? '没有符合条件的归档记录' : '没有符合条件的待处理预警');
  pager($('#alertPager'), d.total, d.page, d.size, p => run(() => loadAlerts(p)), {
    sizes: fulfillmentPageSizes, changeSize: size => {
      alertPageSize[mode] = size;
      run(() => loadAlerts(1));
    },
  });
  $('#alertBody').closest('.tblwrap').scrollTop = 0;
   $('#alertBody').querySelectorAll('[data-undo-archive]').forEach(button => {
     button.onclick = () => run(() => {
       button.disabled = true;
       return changeAlerts('undo_archive', [alertTarget(d.rows[Number(button.dataset.undoArchive)])])
         .finally(() => { button.disabled = false; });
     });
   });
  refreshSelection();
}

async function exportAlertDetails(scope, button) {
  const mode = trackMode;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '正在导出…';
  try {
    await loadMeta();
    if (VIEW !== 'track' || trackMode !== mode || mode === 'tracking') return;
    prepareAlerts();
    const params = new URLSearchParams(qs({...readF(AF), view: mode === 'archive' ? 'archive' : 'pending'}));
    params.set('export', scope);
    const response = await fetch('/api/alerts?' + params, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === 'string' ? error.detail : error.message || `导出失败（${response.status}）`);
    }
    const encoded = response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    downloadBlob(await response.blob(), encoded ? decodeURIComponent(encoded) :
      scope === 'tracks' ? '预警明细与轨迹.zip' : '预警明细.csv');
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function alertTarget({order_no, rule, first_seen_at, history_id, handle_at}) {
  return {order_no, rule, first_seen_at, history_id, handle_at};
}

async function changeAlerts(action, items) {
  const result = await jsonPost('/api/alerts/batch', {action, items});
  if (action.startsWith('undo_')) {
    const detail = result.restored === result.updated ? '已回到待处理' :
      result.restored === 0 ? '保留在历史预警' : `${result.restored} 条回到待处理，其余留在历史`;
    toast(`已撤销 · ${detail}`);
  } else {
    const undo = () => changeAlerts('undo_' + action, items.map(item => ({...item, handle_at: result.handle_at})));
    toast(`已${actions[action]} ${result.updated} 条预警`, undo);
  }
  invalidatePages();
  await Promise.all([loadActivity(), drawerOrder ? refreshOrder(drawerOrder) : null,
    VIEW === 'track' && trackMode !== 'special-tracking' ?
      (trackMode === 'tracking' ? loadTrack(trackPage) : loadAlerts(alertPage)) : null]);
}

async function applyBatch() {
  const action = $('#batchAction').value;
  const items = [...$('#alertBody').querySelectorAll('[data-alert-index]:checked')]
    .map(input => alertTarget(alertRows[Number(input.dataset.alertIndex)]));
  if (!action || !items.length) return;
  $('#applyBatch').disabled = true;
  try {
    await changeAlerts(action, items);
  } finally {
    refreshSelection();
  }
}

/* ================= 渠道时效 ================= */
const TIMING_BASIS = {
  paid: { label: '支付到签收', note: '支付 → 妥投' },
  shipped: { label: '出库到签收', note: '签出 → 妥投' },
  online: { label: '上网到签收', note: '上网 → 妥投' },
};
let CF = null;
let channelRows = [];
let channelBasis = 'shipped';
let channelDimension = 'lastmile';
let channelTabOverride = null;
let channelDayMode = 'calendar';
const CHANNEL_DAY_LABELS = {calendar:'自然日', workday:'工作日'};
const channelDeliveryRates = row => channelDayMode === 'workday' ? row.workdayDeliveryRates : row.deliveryRates;
const channelDeliveryRate = (row, days) => channelDeliveryRates(row)?.[days];
let channelComparisonExpanded = false;
const CHANNEL_DIMENSIONS = {lastmile:'C 端渠道',group:'自定义渠道'};
const CHANNEL_BASE_CRITERIA = {filters:{date_basis:DEFAULT_DATE_BASIS}, basis:'shipped', dimension:'lastmile'};
let renderedChannels = null;
let renderedChannelFilters = {};
let channelTrendPeriod = 'month';
let channelTrendVisible = false;
let channelTrendData = null;
let channelTrendPath = '';
let channelTrendRequest = 0;
let channelMemberRequest = 0;
let renderedChannelMembers = null;
let channelMemberDimension = 'lastmile';
const channelMemberSelection = {key:'', channel:''};
let channelMemberPicker;
/* ---------------- 组合筛选 ---------------- */
const filterPresetWidgets = new WeakMap();

function filterPresetStorageKey(scope) {
  const prefix = scope === 'channels' ? 'fulfillment-channel-presets-v1:' : 'fulfillment-filter-presets-v1:' + scope + ':';
  return prefix + encodeURIComponent(ME.username || 'preview');
}

function readFilterPresets(scope) {
  try {
    const saved = JSON.parse(localStorage.getItem(filterPresetStorageKey(scope)));
    if (Array.isArray(saved?.items)) {
      const items = saved.items.filter(item => typeof item?.id === 'string' && typeof item.name === 'string' &&
        item.criteria?.filters && typeof item.criteria.filters === 'object');
      return {items, defaultId:items.some(item => item.id === saved.defaultId) ? saved.defaultId : ''};
    }
  } catch { /* A blocked or invalid preference store has no saved combinations. */ }
  return {items:[], defaultId:''};
}

function defaultFilterPreset(scope) {
  const {items, defaultId} = readFilterPresets(scope);
  return items.find(item => item.id === defaultId) || null;
}

function applySavedFilters(refs, values, base) {
  refs.cancelSearch?.();
  if (refs.date_basis) {
    const basis = values.date_basis;
    refs.date_basis.value = Object.hasOwn(dateBasisLabel, basis) ? basis : base.date_basis;
    updateDateOptions(refs, refs.date_basis.value);
  }
  if (refs.tstat) {
    refs.tstat.value = values.tstat || '';
    updateSubstates(refs);
  }
  for (const [key, control] of Object.entries(refs)) {
    if (['_info','date_basis','tstat'].includes(key)) continue;
    const value = values[key] ?? base[key] ?? '';
    if (control.setValue) control.setValue(value, true);
    else if (typeof value === 'string') {
      control.value = value;
      if (control instanceof HTMLSelectElement && control.selectedIndex < 0) control.value = base[key] || '';
    }
  }
  refs.validate?.();
}

function syncFilterPreset(refs) {
  filterPresetWidgets.get(refs)?.sync();
}

function attachFilterPresets(scope, label, host, refs, {
  onApply, readExtra = () => ({}), writeExtra = () => {}, describeExtra = () => [],
  before = host.querySelector('.filter-reset'),
} = {}) {
  let preferences = readFilterPresets(scope);
  let applied = '';
  const read = () => ({filters:readF(refs), ...readExtra()});
  const base = read();
  const signature = ({filters, ...extra}) => reportPath('/?' + qs({...filters, ...extra}));
  const root = el('details','multi-filter preset-filter');
  root.dataset.presetScope = scope;
  const trigger = el('summary','multi-trigger preset-trigger', icon('layers') + '<span>组合筛选</span><span class="multi-caret"></span>');
  const name = el('strong','preset-current');
  trigger.insertBefore(name, trigger.lastElementChild);
  const panel = el('div','multi-popover preset-popover');
  const list = el('div','preset-options');
  const actions = el('div','preset-actions');
  const save = el('button','btn ghost sm','保存当前筛选');
  const manage = el('button','btn ghost sm','管理组合');
  save.type = manage.type = 'button';
  actions.append(save, manage);
  panel.append(list, actions);
  root.append(trigger, panel);

  const active = () => preferences.items.find(item => item.id === applied && signature(item.criteria) === signature(read()));
  const sync = () => {
    const item = active();
    name.hidden = !item;
    name.textContent = item?.name || '';
    const detail = item ? item.name + (item.id === preferences.defaultId ? ' · 默认' : '') : '';
    trigger.title = detail || '保存或应用本页筛选组合';
    trigger.setAttribute('aria-label', detail ? '组合筛选 · ' + detail : '组合筛选');
    root.classList.toggle('has-selection', Boolean(item));
  };
  const apply = (item, notify = true) => {
    const criteria = item?.criteria || base;
    applied = item?.id || '';
    applySavedFilters(refs, criteria.filters, base.filters);
    writeExtra(criteria, base);
    sync();
    if (notify) {
      root.open = false;
      trigger.focus({preventScroll:true});
      onApply?.();
    }
  };
  const refresh = () => {
    preferences = readFilterPresets(scope);
    sync();
  };
  const renderMenu = () => {
    const item = active();
    const option = (text, entry, selected) => {
      const button = el('button','preset-option');
      button.type = 'button';
      button.append(el('span'));
      button.firstChild.textContent = text;
      if (entry?.id === preferences.defaultId) button.append(el('small','','默认'));
      button.setAttribute('aria-pressed', String(selected));
      button.onclick = () => apply(entry);
      return button;
    };
    list.replaceChildren(option('站点默认', null, !item && signature(base) === signature(read())),
      ...preferences.items.map(entry => option(entry.name, entry, entry === item)));
    manage.disabled = !preferences.items.length;
  };
  const write = (state, selectedId = applied) => {
    localStorage.setItem(filterPresetStorageKey(scope), JSON.stringify(state));
    preferences = state;
    applied = selectedId;
    sync();
    if (root.open) renderMenu();
  };
  const describe = criteria => {
    const {filters} = criteria;
    const items = describeExtra(criteria);
    if (filters.date_basis) items.push('按' + dateBasisLabel[filters.date_basis]);
    if (filters.start) items.push('开始 ' + filters.start.replace('T',' '));
    if (filters.end) items.push('结束 ' + filters.end.replace('T',' '));
    for (const [key, control] of Object.entries(refs)) {
      const value = filters[key];
      if (!value?.length || ['date_basis','start','end','_info'].includes(key)) continue;
      const title = commonFilterLabels[key] || control.getAttribute?.('aria-label') || key;
      const values = Array.isArray(value) ? value : [value];
      const display = values.slice(0, 2).map(code => control.labelFor?.(code) ||
        [...(control.options || [])].find(option => option.value === code)?.text.trim() || code);
      items.push(title + ' ' + display.join('、') + (values.length > 2 ? ' 等 ' + values.length + ' 项' : ''));
    }
    return items.join(' · ') || '全部';
  };
  const editor = create => {
    if (create && refs.validate && !refs.validate()) { root.open = false; return; }
    root.open = false;
    openFilterPresetEditor({scope, label, create, read, write, describe, activeId:active()?.id,
      validate:refs.validate, trigger});
  };
  save.onclick = () => editor(true);
  manage.onclick = () => editor(false);
  root.ontoggle = () => {
    if (!root.open) return;
    refresh();
    renderMenu();
    positionFilterPopover(root, panel);
  };
  window.addEventListener('storage', event => {
    if (event.key !== null && event.key !== filterPresetStorageKey(scope)) return;
    refresh();
    if (root.open) renderMenu();
  });
  filterPresetWidgets.set(refs, {sync});
  host.insertBefore(root, before);
  const initial = preferences.items.find(item => item.id === preferences.defaultId);
  if (initial) apply(initial, false);
  else sync();
}

function openFilterPresetEditor({scope, label, create, read, write, describe, activeId, validate, trigger}) {
  const state = readFilterPresets(scope);
  if (!create && !state.items.length) return;
  const dialog = $('#filterPresetDialog'), form = $('#filterPresetForm');
  const edit = $('#filterPresetEdit'), name = $('#filterPresetName');
  const replace = $('#filterPresetReplace'), makeDefault = $('#filterPresetDefault');
  const error = $('#filterPresetError');
  const selected = () => state.items.find(item => item.id === edit.value);
  const showSummary = () => {
    const item = selected();
    $('#filterPresetSummary').textContent = describe(item && !replace.checked ? item.criteria : read());
  };
  const fill = () => {
    const item = selected();
    name.value = item?.name || '';
    replace.checked = false;
    makeDefault.checked = item?.id === state.defaultId;
    $('#filterPresetReplaceLabel').hidden = !item;
    $('#filterPresetDelete').hidden = !item;
    error.textContent = '';
    showSummary();
  };
  $('#filterPresetTitle').textContent = label + (create ? ' · 保存当前筛选' : ' · 管理组合');
  $('#filterPresetEditLabel').hidden = create;
  edit.replaceChildren(new Option('', ''), ...state.items.map(item => new Option(item.name, item.id)));
  edit.value = create ? '' : activeId || state.defaultId || state.items[0].id;
  fill();
  $('#filterPresetClose').onclick = () => dialog.close();
  dialog.onclose = () => trigger.focus({preventScroll:true});
  edit.onchange = fill;
  replace.onchange = showSummary;
  form.onsubmit = event => {
    event.preventDefault();
    const saved = readFilterPresets(scope);
    const previous = saved.items.find(item => item.id === edit.value);
    const label = name.value.trim();
    if (!label) { error.textContent = '请输入组合名称。'; return; }
    if (edit.value && !previous) {
      error.textContent = '此组合已在其他标签页删除，请重新打开管理。'; return;
    }
    if (saved.items.some(item => item.id !== previous?.id && item.name.toLocaleLowerCase() === label.toLocaleLowerCase())) {
      error.textContent = '已有同名组合，请换个名称。'; return;
    }
    const capture = !previous || replace.checked;
    if (capture && validate && !validate()) {
      error.textContent = '请先修正页面上的筛选时间。'; return;
    }
    const id = previous?.id || crypto.getRandomValues(new Uint32Array(4)).join('-');
    const item = {id, name:label, criteria:capture ? read() : previous.criteria};
    const items = previous ? saved.items.map(entry => entry.id === id ? item : entry) : [...saved.items, item];
    const defaultId = makeDefault.checked ? id : saved.defaultId === id ? '' : saved.defaultId;
    try {
      write({items, defaultId}, capture ? id : undefined);
    } catch { error.textContent = '浏览器无法保存组合，请检查站点存储设置。'; return; }
    dialog.close();
    toast(makeDefault.checked ? '已保存并设为默认组合' : '已保存筛选组合');
  };
  $('#filterPresetDelete').onclick = () => {
    const item = selected();
    if (!item || !window.confirm(`删除筛选组合「${item.name}」？`)) return;
    const saved = readFilterPresets(scope);
    try {
      write({items:saved.items.filter(entry => entry.id !== item.id),
        defaultId:saved.defaultId === item.id ? '' : saved.defaultId});
    } catch { error.textContent = '浏览器无法保存这一更改。'; return; }
    dialog.close();
    toast('已删除筛选组合');
  };
  dialog.showModal();
  (create ? name : edit).focus();
}

function channelQuery({filters, basis, dimension}) {
  return reportPath('/api/channels?' + qs({...filters, basis, ...(dimension === 'group' ? {dimension} : {})}));
}

function syncChannelTimingControls() {
  $('#channelBasisNote').textContent = TIMING_BASIS[channelBasis].note;
  $('#channelBasis').querySelectorAll('button').forEach(button =>
    button.setAttribute('aria-pressed', String(button.dataset.basis === channelBasis)));
  $('#channelDimension').querySelectorAll('button').forEach(button =>
    button.setAttribute('aria-pressed', String(button.dataset.dimension === channelDimension)));
  $('#channelDayMode').querySelectorAll('button').forEach(button =>
    button.setAttribute('aria-pressed', String(button.dataset.dayMode === channelDayMode)));
}

function setChannelDimension(dimension) {
  if (channelDimension !== dimension) {
    channelMemberRequest++;
    renderedChannelMembers = null;
    channelComparisonExpanded = false;
  }
  channelDimension = dimension;
  if (VIEW === 'channels') history.replaceState(null, '', '#channels-' + dimension);
  syncChannelTimingControls();
}

async function loadChannelFilters() {
  if (CF) return;
  const host = $('#channelFilters');
  host.textContent = '正在读取筛选项…';
  try {
    await loadMeta();
  } catch {
    const retry = el('button', 'btn ghost sm', '重试加载筛选项');
    retry.onclick = () => run(loadChannelFilters);
    host.replaceChildren(retry);
    return;
  }
  if (CF) return;
  CF = makeFilters(host, [
    ...commonDateFilters(),
    { type: 'select', label: '最少单量（默认 100）', key: 'min_n', defaultLabel:'单量 ≥ 100（默认）',
      options: [1, 10, 30, 100, 500].map(value => ({v:value, t:'单量 ≥ ' + value})) },
  ], () => {
    updateDateOptions(CF, CF.date_basis.value);
    scheduleReport('channels',loadChannels);
  });
  updateDateOptions(CF, CF.date_basis.value);
  attachFilterPresets('channels', '渠道时效', host, CF, {
    onApply:() => scheduleReport('channels',loadChannels),
    readExtra:() => ({basis:channelBasis, dimension:channelDimension,
      ...(channelDayMode === 'workday' ? {dayMode:channelDayMode} : {})}),
    writeExtra:({basis, dimension, dayMode}, base) => {
      channelBasis = Object.hasOwn(TIMING_BASIS, basis) ? basis : base.basis;
      channelDayMode = dayMode === 'workday' ? 'workday' : 'calendar';
      channelComparisonExpanded = false;
      setChannelDimension(channelTabOverride || (Object.hasOwn(CHANNEL_DIMENSIONS, dimension) ? dimension : base.dimension));
      if (renderedChannels?.dimension === channelDimension) renderChannelRates(renderedChannels, channelRows);
      renderChannelMembers();
    },
    describeExtra:({basis, dimension, dayMode}) =>
      [CHANNEL_DIMENSIONS[dimension], TIMING_BASIS[basis]?.label, CHANNEL_DAY_LABELS[dayMode || 'calendar']].filter(Boolean),
  });
  channelTabOverride = null;
  if (renderedChannels) showChannelDataDate(renderedChannels);
}

function showChannelDataDate(data) {
  if (!CF) return;
  const date = data.snapshotFiltersSkipped ? '实时筛选暂不可用，表格显示全量快照' :
    `${data.snapshotAt ? '离线快照 · ' : ''}数据截止 ${data.asof}`;
  const missing = [...(data.rows || []), ...(data.byCountry || [])].some(row => !channelDeliveryRates(row));
  CF._info.textContent = date + (missing ? ` · ${CHANNEL_DAY_LABELS[channelDayMode]}妥投率待同步` : '');
}

function renderChannelPeriod(data, filters) {
  const period = [];
  if (data.snapshotFiltersSkipped) {
    period.push('全量离线快照 · 实时筛选未应用');
  } else {
    const {week, start, end, month} = filters;
    period.push(`按${dateBasisLabel[data.dateBasis] || '快照日期口径'}筛选`);
    if (week?.length) {
      const options = new Map((META?.filterOptions.week || []).map(option => [option.v, option.t]));
      const weeks = [...week].sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
      period.push(`周次 ${weeks.map(code => options.get(code) || code).join('、')}`);
    }
    if (month?.length) period.push(`月份 ${[...month].sort().join('、')}`);
    if (start || end) {
      const show = date => date.replace('T', ' ');
      const range = start && end ? `${show(start)} 至 ${show(end)}` :
        start ? `${show(start)} 起` : `截至 ${show(end)}`;
      period.push(`日期 ${range}（北京时间）`);
    }
    if (!week?.length && !month?.length && !start && !end) {
      period.push(data.dateAsof ? `全部日期 · 截至 ${data.dateAsof}` : '全部日期');
    }
    if (data.snapshotAt) period.push('离线快照');
  }
  for (const id of ['channelComparisonPeriod', 'channelTablePeriod', 'channelCountryPeriod']) {
    const host = document.getElementById(id);
    host.classList.toggle('is-snapshot', Boolean(data.snapshotAt));
    host.replaceChildren(...period.map(value => {
      const label = el('span');
      label.textContent = value;
      return label;
    }));
  }
}

function channelLink(row) {
  return ME.preview ? esc(row.channel) :
    `<button type="button" class="order-link" data-channel-track="${esc(row.key)}">${esc(row.channel)}</button>`;
}

function channelMeasure(label, value, unit, tone, meter = false) {
  const bar = meter && value != null ?
    `<span class="compare-bar" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, value))}%"></i></span>` : '';
  return `<div class="compare-metric compare-${tone}">
    <span class="compare-metric-label">${label}</span><b>${measure(value, unit)}</b>
    ${bar}
  </div>`;
}

function renderChannelComparison(data, rows) {
  const isGroup = data.dimension === 'group';
  const visible = isGroup && channelComparisonExpanded ? rows : rows.slice(0, 8);
  const expand = isGroup && rows.length > 8;
  $('#channelCompareExpand').hidden = !expand;
  $('#channelCompareToggle').textContent = channelComparisonExpanded ? '收起其余分组' : `展开其余 ${rows.length - 8} 个分组`;
  $('#channelCompareToggle').setAttribute('aria-expanded', String(isGroup && channelComparisonExpanded));
  $('#channelCompareHead').hidden = !visible.length;
  $('#channelChartEmpty').hidden = Boolean(visible.length);
  const label = TIMING_BASIS[data.basis].label;
  $('#channelChartScope').textContent = visible.length ?
    `${label} · ${visible.length === rows.length ? '共' : '单量前'} ${visible.length} 个${isGroup ? '分组' : '渠道'} · ${CHANNEL_DAY_LABELS[channelDayMode]}妥投率按全部有效单` : label;
  $('#channelComparisonRows').innerHTML = visible.map(row => `<li class="compare-grid compare-row">
    <div class="compare-name">${channelLink(row)}</div>
    <div class="compare-samples"><span class="compare-metric-label">有效样本 / 单量</span>
      <b>${fmt(row.samples)}</b><small>/ ${fmt(row.orders)}</small>
    </div>
    ${channelMeasure('平均用时', row.avgDays, ' 天', 'average')}
    ${channelMeasure('P90 用时', row.p90Days, ' 天', 'p90')}
    ${DELIVERY_WINDOWS.map(days => channelMeasure(`${days} 天内妥投`, channelDeliveryRate(row, days), '%', days === 6 ? 'rate-primary' : 'rate', days === 6)).join('')}
  </li>`).join('');
}

async function loadChannels() {
  if (!CF) {
    if (!defaultFilterPreset('channels')) fetchReport(channelQuery({...CHANNEL_BASE_CRITERIA, dimension:channelDimension})).catch(() => {});
    await loadChannelFilters();
  }
  if (!CF || VIEW !== 'channels' || !CF.validate()) return;
  const criteria = {filters:readF(CF), basis:channelBasis, dimension:channelDimension};
  const {dimension, filters} = criteria;
  const loading = $('#channelLoading');
  const results = $('#channelResults');
  const currentResult = renderedChannels?.dimension === dimension;
  results.hidden = !currentResult;
  loading.hidden = currentResult;
  loading.textContent = '正在读取渠道时效…';
  results.setAttribute('aria-busy', 'true');
  try {
    await presentReport('channels', channelQuery(criteria),
      d => { if (dimension === channelDimension && (d.dimension || 'lastmile') === dimension) renderChannels(d, filters); },
      {signal: AbortSignal.timeout(18_000)}, {oldVisible: currentResult});
  } catch (error) {
    if (VIEW === 'channels' && dimension === channelDimension) {
      loading.hidden = false;
      loading.textContent = error.name === 'TimeoutError' ? '渠道时效读取超时。' : '渠道时效读取失败。' + error.message;
      if (currentResult) loading.append(` 下方为上次的「${TIMING_BASIS[renderedChannels.basis].label}」结果。`);
      const retry = el('button', 'btn ghost sm', '重试');
      retry.onclick = () => run(loadChannels);
      loading.append(' ', retry);
      results.setAttribute('aria-busy', 'false');
    }
    return;
  }
}

function renderChannels(d, filters) {
  if (VIEW !== 'channels') return;
  const loading = $('#channelLoading'), results = $('#channelResults');
  loading.hidden = true;
  results.hidden = false;
  results.inert = false;
  results.setAttribute('aria-busy', 'false');
  if (renderedChannels === d) {
    if (d.dimension === 'group' && !renderedChannelMembers) refreshChannelMemberGroups(d);
    return;
  }
  renderedChannels = d;
  const dimension = d.dimension || 'lastmile';
  const dimensionLabel = CHANNEL_DIMENSIONS[dimension];
  $('#channelComparisonName').textContent = dimensionLabel + '时效对比';
  $('#channelCompareLabel').textContent = dimensionLabel;
  $('#channelPerformanceTitle').textContent = dimensionLabel + '表现对比';
  $('#channelTableLabel').textContent = dimension === 'group' ? '自定义渠道分组' : 'C 端显示物流渠道';
  $('#channelCountryTitle').textContent = dimensionLabel + ' × 国家';
  $('#channelCountryLabel').textContent = dimensionLabel;
  renderedChannelFilters = d.snapshotFiltersSkipped ? {} : {...filters, date_basis: d.dateBasis || filters.date_basis};
  renderChannelPeriod(d, renderedChannelFilters);

  const rows = d.rows || [];
  channelRows = rows;
  const totalOrders = rows.reduce((a, r) => a + r.orders, 0);
  const signed = rows.reduce((a, r) => a + r.signed, 0);
  const samples = rows.reduce((a, r) => a + r.samples, 0);
  const fallback = rows.reduce((a, r) => a + (r.fallbackSamples || 0), 0);
  const overAll = rows.reduce((a, r) => a + (r.overdue || 0), 0);
  const inAll = rows.reduce((a, r) => a + r.slaInTransit, 0);
  $('#channelKpi').innerHTML = [
    [dimension === 'group' ? '对比分组数' : 'C 端渠道数', fmt(rows.length), `单量 ≥${filters.min_n || 100}`, dimension === 'group' ? 'layers' : 'route','violet'],
    ['有效签收样本', fmt(samples), signed ? `含 WMS 补算 ${fmt(fallback)} 单` : '当前范围无可用的妥投时间','check','green'],
    ['覆盖单量', fmt(totalOrders), '参与统计','box','blue'],
    ['SLA 在途超时', fmt(overAll), inAll ? `占可计时在途 ${fmt1(100 * overAll / inAll)}%` : '当前范围无可计时的在途单','clock',overAll ? 'warn' : 'green'],
  ].map(row => metricCard(...row)).join('');

  renderChannelRates(d, rows);
  refreshChannelMemberGroups(d);
  const selector = $('#channelTrendKey');
  const key = selector.value;
  selector.replaceChildren(...rows.map(row => new Option(row.channel, row.key)));
  if (rows.some(row => row.key === key)) selector.value = key;
  selector.disabled = !rows.length;
  $('#channelTrendScope').textContent = `按${dateBasisLabel[d.dateBasis || DEFAULT_DATE_BASIS]}分组 · ${TIMING_BASIS[d.basis].label}`;
  if (!rows.length) {
    channelTrendRequest++;
    channelTrendData = null;
    $('#channelTrendStatus').textContent = '当前筛选没有可比较的渠道。';
    $('#channelTrendData').hidden = true;
  } else if (channelTrendVisible) {
    run(loadChannelTrend);
  }
}

function setupChannelMemberPicker() {
  const host = $('#channelMemberPicker');
  const trigger = $('#channelMemberTrigger');
  const menu = $('#channelMemberMenu');
  const groups = $('#channelMemberGroups');
  const branches = $('#channelMemberBranches');
  const pointer = matchMedia('(hover: hover) and (pointer: fine)');
  const stacked = matchMedia('(max-width: 760px)');
  let items = [], mapping = null, mappingRequest;
  let active = null, pinned = false, lastPoint = null, departure = null;
  let openTimer, closeTimer, branchTimer;

  function label() {
    const {key, channel} = channelMemberSelection;
    $('#channelMemberSelection').textContent = [key, channel].filter(Boolean).join(' / ') || '选择分组';
    trigger.setAttribute('aria-label', key ? `自定义渠道分组：${key}${channel ? ' / ' + channel : ''}` : '自定义渠道分组');
    trigger.disabled = !items.length;
  }

  function align() {
    const box = host.getBoundingClientRect();
    const available = document.documentElement.clientWidth;
    const width = stacked.matches ? Math.min(248, available - 32) : 504;
    const reverse = !stacked.matches && box.left + width > available - 16;
    const edge = reverse ? box.right - width : box.left;
    const left = Math.max(16, Math.min(edge, available - width - 16));
    menu.style.left = `${left - box.left}px`;
    menu.dataset.side = reverse ? 'left' : 'right';
    menu.classList.toggle('member-menu-up', window.innerHeight - box.bottom < 260 && box.top > window.innerHeight - box.bottom);
  }

  function renderBranches() {
    if (!active || menu.hidden) return;
    const members = mapping?.get(active) ||
      (renderedChannelMembers?.group === active ? renderedChannelMembers.rows.map(row => row.channel) : []);
    const names = ['', ...members];
    const rows = names.map(channel => {
      const checked = active === channelMemberSelection.key && channel === channelMemberSelection.channel;
      const button = el('button', '', '');
      button.type = 'button';
      button.dataset.memberBranch = channel;
      button.setAttribute('aria-pressed', String(checked));
      button.append(document.createTextNode(channel || '全部当前渠道'));
      if (checked) button.insertAdjacentHTML('beforeend', icon('check'));
      return button;
    });
    if (mappingRequest && !members.length) rows.push(el('p', 'tip', '正在读取当前渠道…'));
    const focused = branches.contains(document.activeElement) ? document.activeElement.dataset.memberBranch : undefined;
    branches.replaceChildren(...rows);
    if (focused !== undefined) rows.find(row => row.dataset.memberBranch === focused)?.focus({preventScroll:true});
  }

  function activate(button) {
    clearTimeout(branchTimer);
    active = button.dataset.memberGroup;
    lastPoint = departure = null;
    groups.querySelectorAll('button').forEach(item => item.setAttribute('aria-expanded', String(item === button)));
    renderBranches();
    branches.hidden = false;
  }

  function open() {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    if (!menu.hidden || !items.length) return;
    const drill = channelMemberDimension === 'lastmile';
    groups.replaceChildren(...items.map(({key}) => {
      const button = el('button', '', '');
      button.type = 'button';
      button.dataset.memberGroup = key;
      button.setAttribute('aria-pressed', String(key === channelMemberSelection.key));
      if (drill) button.setAttribute('aria-expanded', 'false');
      button.append(document.createTextNode(key));
      if (drill) button.insertAdjacentHTML('beforeend', icon('right'));
      return button;
    }));
    branches.hidden = true;
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    align();
    if (drill && !mappingRequest) {
      mappingRequest = fetchReport(channelGroupPath).then(({data}) => {
        mapping = new Map(data.groups.map(group => [group.name, group.channels]));
      }).catch(() => {}).finally(() => {
        mappingRequest = null;
        renderBranches();
      });
    }
  }

  function close(focus = false) {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    clearTimeout(branchTimer);
    if (menu.hidden) return;
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    active = lastPoint = departure = null;
    pinned = false;
    if (focus) trigger.focus({preventScroll:true});
  }

  function select(key, channel = '') {
    const changed = channelMemberSelection.key !== key;
    Object.assign(channelMemberSelection, {key, channel});
    close(true);
    label();
    if (changed) run(loadChannelMembers);
    else renderChannelMembers();
  }

  function towardBranch(point) {
    if (!departure || branches.hidden || stacked.matches) return false;
    const box = branches.getBoundingClientRect();
    const reverse = menu.dataset.side === 'left';
    const edge = reverse ? box.right : box.left;
    if (reverse ? point.x > departure.x : point.x < departure.x) return false;
    const upper = {x:edge, y:box.top};
    const lower = {x:edge, y:box.bottom};
    const cross = (a, b, c) => (b.x-a.x)*(c.y-a.y) - (b.y-a.y)*(c.x-a.x);
    const sides = [cross(departure, upper, point), cross(upper, lower, point), cross(lower, departure, point)];
    return sides.every(value => value >= 0) || sides.every(value => value <= 0);
  }

  trigger.addEventListener('pointerenter', () => {
    if (pointer.matches && trigger.disabled === false && menu.hidden) openTimer = setTimeout(open, 160);
  });
  host.addEventListener('pointerenter', () => clearTimeout(closeTimer));
  host.addEventListener('pointerleave', () => {
    clearTimeout(openTimer);
    clearTimeout(branchTimer);
    if (!pinned) closeTimer = setTimeout(close, 260);
  });
  trigger.onclick = event => {
    if (pinned) return close();
    open();
    pinned = true;
    if (event.detail === 0) groups.querySelector('button')?.focus();
  };
  menu.addEventListener('pointermove', event => {
    if (!pointer.matches || channelMemberDimension !== 'lastmile') return;
    const button = event.target.closest('[data-member-group]');
    const point = {x:event.clientX, y:event.clientY};
    clearTimeout(branchTimer);
    if (!button) return;
    if (button.dataset.memberGroup === active) {
      lastPoint = point;
      departure = null;
      return;
    }
    departure ||= lastPoint;
    if (!towardBranch(point)) branchTimer = setTimeout(() => activate(button), 140);
  });
  branches.addEventListener('pointerenter', () => clearTimeout(branchTimer));
  menu.onclick = event => {
    const group = event.target.closest('[data-member-group]');
    const branch = event.target.closest('[data-member-branch]');
    if (branch) select(active, branch.dataset.memberBranch);
    else if (group && channelMemberDimension === 'current') select(group.dataset.memberGroup);
    else if (group) {
      activate(group);
      if (event.detail === 0) branches.querySelector('button')?.focus();
    }
  };
  host.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !menu.hidden) {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === 'ArrowDown' && event.target === trigger) {
      event.preventDefault();
      open();
      pinned = true;
      groups.querySelector('button')?.focus();
    } else if (!menu.hidden && event.key === (menu.dataset.side === 'left' ? 'ArrowLeft' : 'ArrowRight') &&
               groups.contains(event.target) && channelMemberDimension === 'lastmile') {
      event.preventDefault();
      activate(event.target);
      branches.querySelector('button')?.focus();
    } else if (!menu.hidden && event.key === (menu.dataset.side === 'left' ? 'ArrowRight' : 'ArrowLeft') &&
               branches.contains(event.target)) {
      event.preventDefault();
      const previous = [...groups.querySelectorAll('button')].find(button => button.dataset.memberGroup === active);
      previous?.setAttribute('aria-expanded', 'false');
      previous?.focus();
      branches.hidden = true;
      active = lastPoint = departure = null;
    }
  });
  host.addEventListener('focusout', event => {
    if (event.relatedTarget && !host.contains(event.relatedTarget)) close();
  });
  document.addEventListener('pointerdown', event => { if (!host.contains(event.target)) close(); });
  window.addEventListener('resize', () => { if (!menu.hidden) align(); });
  return {
    close,
    label,
    setGroups(next) {
      const changed = next.length !== items.length || next.some((item, index) => item.key !== items[index]?.key);
      if (changed) close();
      items = next;
      label();
    }
  };
}

function refreshChannelMemberGroups(report) {
  $('#channelMembers').hidden = report.dimension !== 'group';
  if (report.dimension !== 'group') return;
  const groups = report.memberGroups || [];
  if (!groups.some(item => item.key === channelMemberSelection.key)) {
    Object.assign(channelMemberSelection, {key:groups[0]?.key || '', channel:''});
  }
  channelMemberPicker.setGroups(groups);
  if (groups.length) run(loadChannelMembers);
  else {
    channelMemberRequest++;
    renderedChannelMembers = null;
    $('#channelMemberContent').hidden = true;
    $('#channelMembersScope').textContent = '';
    $('#channelMemberStatus').textContent = report.snapshotAt ?
      '快照中暂无子渠道明细。' : '当前筛选范围内没有已配置的自定义渠道。';
  }
}

async function loadChannelMembers() {
  const {key} = channelMemberSelection;
  if (VIEW !== 'channels' || channelDimension !== 'group' || renderedChannels?.dimension !== 'group' || !key) return;
  const {min_n, ...filters} = renderedChannelFilters;
  const path = reportPath('/api/channel-members?' + qs({...filters, key, basis:renderedChannels.basis}));
  const request = ++channelMemberRequest;
  const status = $('#channelMemberStatus');
  renderedChannelMembers = reportRecord(path)?.data || null;
  if (renderedChannelMembers) renderChannelMembers();
  else {
    $('#channelMemberContent').hidden = true;
    $('#channelMembersScope').textContent = '';
    status.textContent = '正在计算子渠道占比和时效…';
  }
  try {
    const record = await fetchReport(path);
    if (request !== channelMemberRequest || VIEW !== 'channels') return;
    renderedChannelMembers = record.data;
    renderChannelMembers();
  } catch {
    if (request !== channelMemberRequest || VIEW !== 'channels') return;
    const retry = el('button', 'btn ghost sm', '重试');
    retry.type = 'button';
    retry.onclick = () => run(loadChannelMembers);
    status.replaceChildren('暂时无法更新子渠道数据。 ', retry);
  }
}

function renderChannelMembers() {
  if (VIEW !== 'channels' || channelDimension !== 'group' || !renderedChannelMembers) return;
  const report = renderedChannelMembers;
  const {group} = report;
  const lastmile = channelMemberDimension === 'lastmile';
  const parent = report.rows.find(row => row.channel === channelMemberSelection.channel);
  if (!parent) channelMemberSelection.channel = '';
  channelMemberPicker.label();
  const scope = lastmile && parent ? parent : report;
  const orders = scope.orders;
  const rows = lastmile ? scope.lastmileRows || [] : report.rows;
  const label = lastmile ? 'C 端渠道' : '当前物流渠道';
  const openMember = key => openTracking({...renderedChannelFilters, channel_group:group,
    ...(lastmile ? {lastmile:key, ...(parent ? {channel:parent.channel} : {})} : {channel:key})});
  $('#channelMemberStatus').textContent = lastmile && !scope.lastmileRows ? '暂时无法读取 C 端渠道明细。' : '';
  $('#channelMemberContent').hidden = false;
  $('#channelMembersScope').textContent = [group, scope.channel, `${fmt(orders)} 单`,
    TIMING_BASIS[renderedChannels.basis].label, CHANNEL_DAY_LABELS[channelDayMode]].filter(Boolean).join(' · ');
  $('#channelMembersTitle').textContent = label + '对比';
  $('#channelMemberLabel').textContent = label;
  const share = value => orders ? (value > 0 && value / orders < 0.001 ? '&lt;0.1%' : measure(100 * value / orders, '%')) : '—';
  const days = value => value == null ? '—' : `${value.toFixed(2)} 天`;
  $('#channelMemberBody').innerHTML = rows.map(row => `<tr>
    <td>${ME.preview || !row.orders ? esc(row.channel) :
      `<button type="button" class="order-link" data-member-channel="${esc(row.key || row.channel)}">${esc(row.channel)}</button>`}</td>
    <td><b>${fmt(row.orders)}</b></td>
    <td class="member-share-cell"><div class="member-share">
      <span class="rank-track" aria-hidden="true"><span style="width:${orders ? 100 * row.orders / orders : 0}%"></span></span>
      <span>${share(row.orders)}</span></div></td><td>${fmt(row.samples)}</td>
    <td>${days(row.avgDays)}</td><td>${days(row.p90Days)}</td>
    ${[5, 6, 7].map(day => `<td>${measure(channelDeliveryRate(row, day), '%')}</td>`).join('')}</tr>`).join('')
    || '<tr><td colspan="9" class="tip">暂无可比较的渠道</td></tr>';
  $('#channelMemberBody').onclick = event => {
    const target = event.target.closest('[data-member-channel]');
    if (target) openMember(target.dataset.memberChannel);
  };
}

async function loadChannelTrend() {
  const key = $('#channelTrendKey').value;
  if (VIEW !== 'channels' || !channelTrendVisible || !renderedChannels || !key) return;
  const {min_n, ...filters} = renderedChannelFilters;
  const d = renderedChannels;
  const path = reportPath('/api/channel-trends?' + qs({...filters, key, basis:d.basis, dimension:d.dimension}));
  const request = ++channelTrendRequest;
  const status = $('#channelTrendStatus');
  const saved = reportRecord(path);
  if (path !== channelTrendPath || !channelTrendData) {
    channelTrendPath = path;
    channelTrendData = saved?.data || null;
    if (channelTrendData) renderChannelTrend();
    else {
      $('#channelTrendData').hidden = true;
      status.textContent = '正在计算渠道各期时效…';
    }
  }
  try {
    const record = await fetchReport(path);
    if (request !== channelTrendRequest || VIEW !== 'channels') return;
    channelTrendData = record.data;
    renderChannelTrend();
  } catch {
    if (request !== channelTrendRequest || VIEW !== 'channels') return;
    const retry = el('button', 'btn ghost sm', '重试');
    retry.type = 'button';
    retry.onclick = () => run(loadChannelTrend);
    status.replaceChildren(channelTrendData ? '更新失败，显示已有趋势。 ' : '暂时无法读取渠道趋势。 ', retry);
  }
}

function renderChannelTrend() {
  if (!channelTrendData || VIEW !== 'channels') return;
  const data = channelTrendData[channelTrendPeriod] || [];
  const host = $('#channelTrendData');
  const status = $('#channelTrendStatus');
  host.hidden = !data.length;
  status.textContent = data.length ? '' : '当前渠道在筛选范围内没有数据。';
  if (!data.length) return;
  const month = channelTrendPeriod === 'month';
  const against = month ? '较上月' : '较上周';
  const rate = channelDayMode === 'workday' ? 'workdayRate6' : 'rate6';
  const rateTitle = `6 天内妥投率（${CHANNEL_DAY_LABELS[channelDayMode]}）`;
  const label = row => month ? row.start.slice(0,7) : row.start + ' 起';
  const days = value => value == null ? '—' : `${value.toFixed(2)} 天`;
  const delta = (next, previous, unit, decimals) => {
    if (!Number.isFinite(next) || !Number.isFinite(previous)) return '';
    const diff = Number((next - previous).toFixed(decimals));
    return `<small class="cell-sub">${against} ${diff > 0 ? '+' : ''}${diff.toFixed(decimals)}${unit}</small>`;
  };
  $('#channelTrendBody').innerHTML = data.map((row, index) => {
    const prior = data[index - 1];
    const previous = !row.partial && !prior?.partial && prior?.end === row.start ? prior : null;
    const volumeChange = previous?.orders ? delta(100 * row.orders / previous.orders, 100, '%', 1) : '';
    return `<tr><td><b>${esc(label(row))}</b>${row.partial ? '<span class="badge gray">部分周期</span>' : ''}</td>
      <td>${fmt(row.orders)}${volumeChange}</td><td>${fmt(row.samples)}</td>
      <td>${days(row.avg)}${delta(row.avg, previous?.avg, ' 天', 2)}</td>
      <td>${days(row.p90)}${delta(row.p90, previous?.p90, ' 天', 2)}</td>
      <td>${measure(row[rate], '%')}${delta(row[rate], previous?.[rate], ' 个百分点', 1)}</td></tr>`;
  }).reverse().join('');
  $('#channelTrendNote').textContent = `${month ? '按自然月' : '周日为每周起点（北京时间）'} · 相邻完整周期显示环比 · ${rateTitle}按全部有效履约单计算`;
  void drawChannelTrendCharts(data, month, rate, rateTitle);
}

async function drawChannelTrendCharts(data, month, rate, rateTitle) {
  try { await loadChartLibrary(); } catch { return; }
  if (VIEW !== 'channels' || data !== channelTrendData?.[channelTrendPeriod] ||
      rate !== (channelDayMode === 'workday' ? 'workdayRate6' : 'rate6')) return;
  const periods = [];
  for (const row of data) {
    if (periods.length && periods.at(-1).end !== row.start) periods.push({start:periods.at(-1).end, missing:true});
    periods.push(row);
  }
  const xLabels = periods.map(row => (month ? row.start.slice(0,7) : row.start.slice(5)) + (row.partial ? '*' : ''));
  const common = {
    animation: chartMotion,
    grid: {left:5, right:8, top:34, bottom:5, containLabel:true},
    tooltip: {...chartTip, trigger:'axis'},
    legend: {top:0, right:5, textStyle:{color:chartTheme.muted, fontSize:11}},
    xAxis: {type:'category', data:xLabels, axisTick:{show:false}, axisLabel:{color:chartTheme.muted, hideOverlap:true}},
  };
  const axis = formatter => ({type:'value', min:0, axisLabel:{color:chartTheme.muted, formatter},
    splitLine:{lineStyle:{color:chartTheme.grid}}});
  const line = (name, field, color, unit, yAxisIndex = 0) => ({name,type:'line',smooth:0.25,showSymbol:periods.length <= 22,
    yAxisIndex, data:periods.map(row => row[field] ?? null), itemStyle:{color},
    tooltip:{valueFormatter:value => measure(value,unit)}});
  initChart('channelTrendDuration').setOption({...common, yAxis:axis('{value} 天'), series:[
    line('平均时效','avg',chartTheme.series[2],' 天'),
    line('P90','p90',chartTheme.ink,' 天'),
  ]}, {notMerge:true});
  initChart('channelTrendVolume').setOption({...common, yAxis:[axis(compactCount),
    {...axis('{value}%'),max:100,splitLine:{show:false}}], series:[
    {name:'履约单量',type:'bar',barMaxWidth:29,data:periods.map(row => row.orders ?? null),
      itemStyle:{color:chartTheme.series[0],borderRadius:[3,3,0,0]},tooltip:{valueFormatter:fmt}},
    line(rateTitle,rate,chartTheme.series[2],'%',1),
  ]}, {notMerge:true});
  resizeVisibleCharts();
}

function renderChannelRates(d, rows) {
  showChannelDataDate(d);
  renderChannelComparison(d, rows);
  const dayLabel = CHANNEL_DAY_LABELS[channelDayMode];
  $('#channelDetailedRateScope').textContent =
    `${dayLabel}妥投率按全部有效单 · SLA 按当前渠道配置及有效签收样本 · 超时占比按可计时在途单`;
  $('#channelCountryRateScope').textContent = `单量 ≥ 200 的组合 · ${dayLabel}妥投率`;
  $('#channelBody').innerHTML = rows.length ? rows.map(r => {
    return `<tr>
      <td>${channelLink(r)}</td>
      <td>${fmt(r.orders)}</td><td>${fmt(r.signed)}</td><td>${fmt(r.samples)}</td>
      <td>${measure(r.avgDays, ' 天')}</td><td>${measure(r.p90Days, ' 天')}</td>
      ${DELIVERY_WINDOWS.map(days => `<td>${measure(channelDeliveryRate(r, days), '%')}</td>`).join('')}
      <td title="SLA 有效签收样本 ${fmt(r.sla_samples)} 单"><b>${measure(r.slaRate, '%')}</b></td>
      <td>${fmt(r.inTransit)}</td>
      <td class="${r.overRate > 20 ? 'neg' : ''}">${fmt(r.overdue)}${r.overRate != null ? ` (${fmt1(r.overRate)}%)` : ''}</td>
    </tr>`;
  }).join('') : blankRow(14, '没有满足条件的渠道');

  $('#chanCountryBody').innerHTML = (d.byCountry || []).map(r => `
    <tr><td><b>${esc(r.ch)}</b></td><td>${esc(r.ctry || '—')}</td><td>${fmt(r.n)}</td><td>${fmt(r.samples)}</td>
    <td>${measure(r.avgDays, ' 天')}</td><td>${measure(channelDeliveryRate(r, 2), '%')}</td><td>${measure(channelDeliveryRate(r, 6), '%')}</td></tr>`).join('')
    || blankRow(7, '当前筛选无达到样本量的渠道组合');

}

/* ================= 特殊轨迹监控 ================= */

let trajectorySettings;
let editingTrackRule = null;
let exceptionFilters;
let exceptionPageSize = 40;
let exceptionSearchTimer;
const trajectoryPath = '/api/channel-track-rules';
const trajectoryWeekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const readTrackWords = value => value.split(/\r?\n/).map(word => word.trim()).filter(Boolean);
const eventWallClock = value => value ? value.replace('T', ' ').slice(0, 16) + ' ' + value.slice(-6) : '—';

function renderTrackRuleOptions() {
  const select = $('#trackExceptionRule');
  const current = select.value;
  select.replaceChildren(new Option('全部规则', ''), ...trajectorySettings.rules.filter(rule => rule.enabled)
    .map(rule => new Option(rule.name, rule.key)));
  select.value = current;
  if (select.selectedIndex < 0) select.value = '';
}

function renderTrackRuleCards(counts) {
  const byRule = new Map(counts.map(summary => [summary.rule, summary]));
  $('#trackRuleCards').innerHTML = trajectorySettings.rules.map(rule => {
    const stat = byRule.get(rule.key);
    const start = rule.anchor === 'shipped' ? '签出' : rule.start.join(' / ');
    return `<article class="trajectory-rule">
      <div class="ct"><h3>${esc(rule.name)}</h3><span class="badge ${rule.enabled ? 'ok' : 'gray'}">
        ${rule.enabled ? '已启用' : '已停用'}</span></div>
      <p><b>${esc(rule.channels.join('、'))}</b></p>
      <p class="tip">${esc(start)} → ${esc(rule.end.join(' / '))}</p>
      <p class="tip">${esc(rule.note)}</p>
      ${stat ? `<div class="trajectory-counts">
        <span><b>${fmt(stat.late)}</b> 节点超时</span><span><b>${fmt(stat.missing)}</b> 超时未见节点</span>
        <span><b>${fmt(stat.unverified)}</b> 待轨迹核对</span>
        ${stat.unknown_clock ? `<span><b>${fmt(stat.unknown_clock)}</b> 时区待确认</span>` : ''}</div>` : ''}
      ${ME.can_edit ? `<button type="button" class="btn ghost sm" data-edit-track-rule="${esc(rule.key)}">编辑规则</button>` : ''}
    </article>`;
  }).join('') || '<p class="tip">暂无规则</p>';
}

async function loadTrackExceptions(page = 1, size = exceptionPageSize) {
  const status = $('#trackExceptionStatus');
  status.textContent = '正在核对轨迹…';
  let data;
  try {
    if (!trajectorySettings) {
      trajectorySettings = await api(trajectoryPath);
      renderTrackRuleOptions();
    }
    if (!exceptionFilters) {
      exceptionFilters = {days:$('#trackExceptionDays'), rule:$('#trackExceptionRule'),
        state:$('#trackExceptionState'), q:$('#trackExceptionSearch')};
    }
    const filters = {...readF(exceptionFilters), q:exceptionFilters.q.value.trim()};
    data = await latest('trackExceptions', '/api/channel-track-exceptions?' + qs(filters, page, size),
      {signal: AbortSignal.timeout(20_000)});
  } catch (error) {
    if (VIEW === 'track' && trackMode === 'special-tracking') status.textContent = error.message;
    return;
  }
  if (!data || VIEW !== 'track' || trackMode !== 'special-tracking') return;
  const lastPage = Math.max(1, Math.ceil(data.total / data.size));
  if (data.page > lastPage) return loadTrackExceptions(lastPage, data.size);
  exceptionPageSize = data.size;
  renderTrackRuleCards(data.counts);
  status.textContent = `出库自 ${data.from} 起 · 截至 ${syncTime(data.asof)} · 共 ${fmt(data.total)} 条`;
  $('#trackExceptionBody').innerHTML = data.rows.map(row => `<tr>
    <td>${orderButton(row.order_no, row.waybill)}</td>
    <td><b>${esc(row.name)}</b><small class="cell-sub"><span class="badge ${row.state === 'late' ? 'h' : 'purple'}">
      ${row.state === 'late' ? '节点超时' : '超时未见节点'}</span></small></td>
    <td>${esc([row.warehouse, row.site].filter(Boolean).join(' · ') || '—')}
      <small class="cell-sub">${esc(row.channel || row.lastmile || '—')}</small></td>
    <td class="mono">${esc(eventWallClock(row.anchor_at))}<small class="cell-sub">${esc(row.anchor_text)}</small></td>
    <td class="mono">${esc(row.due)} 日终<small class="cell-sub">${esc(row.deadline.slice(-6))}</small></td>
    <td class="mono">${esc(eventWallClock(row.event_at))}<small class="cell-sub">${row.event_text ? esc(row.event_text) :
      `最近核对 ${esc(syncTime(row.checked_at))}`}</small></td></tr>`).join('') || blankRow(6, '没有符合条件的轨迹异常');
  pager($('#trackExceptionPager'), data.total, data.page, data.size, next => run(() => loadTrackExceptions(next)), {
    sizes:[20, 40, 100], changeSize:n => run(() => loadTrackExceptions(1, n)),
  });
}

function addTrackRuleSite(site = {name: '', warehouses: [], cutoff: '14:30'}) {
  const row = el('div', 'track-rule-site', `
    <label>地点<input data-site="name" value="${esc(site.name)}" maxlength="70" required></label>
    <label>仓库代码<input data-site="warehouses" value="${esc(site.warehouses.join(', '))}" placeholder="用逗号分隔"></label>
    <label>截单时间<input type="time" data-site="cutoff" value="${esc(site.cutoff)}" required></label>
    <button type="button" class="btn ghost sm" aria-label="移除地点">移除</button>`);
  row.lastElementChild.onclick = () => row.remove();
  $('#trackRuleSites').append(row);
}

function refreshTrackRuleForm() {
  const form = $('#trackRuleForm');
  const event = form.elements.anchor.value === 'event';
  $('#trackRuleStart').hidden = !event;
  form.elements.start.required = event;
  $('#trackRuleSiteFields').hidden = event;
  $('#trackRuleSites').querySelectorAll('input').forEach(input => { input.disabled = event; });
  $('#trackRuleBeforeTitle').textContent = event ? '起始后第几天' : '截单前';
  $('#trackRuleAfterTitle').hidden = event;
  $('#trackRuleSchedule').querySelectorAll('[data-after]').forEach(cell => { cell.hidden = event; });
  const options = META.filterOptions[{lastmile: 'lastmile', current: 'channel', group: 'channel_group'}[form.elements.scope.value]] || [];
  $('#trackRuleChannelPick').replaceChildren(new Option('选择渠道…', ''), ...options.map(row => new Option(row.t, row.v)));
}

async function openTrackRule(key) {
  await loadMeta();
  if (!trajectorySettings) trajectorySettings = await api(trajectoryPath);
  const rule = trajectorySettings.rules.find(row => row.key === key);
  editingTrackRule = rule?.key || null;
  const form = $('#trackRuleForm');
  form.reset();
  $('#trackRuleError').textContent = '';
  $('#trackRuleFormTitle').textContent = rule ? `编辑规则 · ${rule.name}` : '新增轨迹异常规则';
  $('#trackRuleRemove').hidden = !rule;
  if (rule) {
    for (const name of ['name', 'scope', 'anchor', 'carrier', 'note']) form.elements[name].value = rule[name] ?? '';
    for (const name of ['channels', 'start', 'end']) form.elements[name].value = rule[name].join('\n');
    form.elements.enabled.checked = rule.enabled;
  }
  $('#trackRuleSites').replaceChildren();
  (rule?.sites?.length ? rule.sites : [undefined]).forEach(addTrackRuleSite);
  $('#trackRuleSchedule').innerHTML = trajectoryWeekdays.map((day, index) => `<tr>
    <th scope="row">${day}</th><td><input type="number" min="0" max="14" value="${rule?.before[index] ?? ''}"
      data-slot="before" aria-label="${day} 截单前或起始后"></td>
    <td data-after><input type="number" min="0" max="14" value="${rule?.after?.[index] ?? ''}"
      data-slot="after" aria-label="${day} 截单时及之后"></td></tr>`).join('');
  refreshTrackRuleForm();
  $('#trackRuleDialog').showModal();
}

async function saveTrackRule(remove) {
  const form = $('#trackRuleForm');
  const {elements} = form;
  let items = trajectorySettings.rules.filter(rule => rule.key !== editingTrackRule);
  if (!remove) {
    const shipped = elements.anchor.value === 'shipped';
    const value = name => elements[name].value.trim();
    const schedule = slot => [...$('#trackRuleSchedule').querySelectorAll(`[data-slot="${slot}"]`)]
      .map(input => input.value === '' ? null : Number(input.value));
    const sites = [...$('#trackRuleSites').children].map(row => ({
      name: row.querySelector('[data-site=name]').value.trim(),
      warehouses: row.querySelector('[data-site=warehouses]').value.split(/[,，\s]+/).filter(Boolean),
      cutoff: row.querySelector('[data-site=cutoff]').value,
    }));
    const rule = {key: editingTrackRule || crypto.randomUUID(), name: value('name'), enabled: elements.enabled.checked,
      scope: value('scope'), channels: readTrackWords(value('channels')), carrier: Number(value('carrier')) || null,
      anchor: value('anchor'), start: shipped ? [] : readTrackWords(value('start')), end: readTrackWords(value('end')),
      before: schedule('before'), after: shipped ? schedule('after') : null, sites: shipped ? sites : [], note: value('note')};
    items = editingTrackRule ? trajectorySettings.rules.map(item => item.key === editingTrackRule ? rule : item) : [...items, rule];
  }
  const save = $('#trackRuleSave');
  const error = $('#trackRuleError');
  save.disabled = $('#trackRuleRemove').disabled = true;
  error.textContent = '';
  try {
    trajectorySettings = await api(trajectoryPath, {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({revision: trajectorySettings.revision, rules: items})});
    $('#trackRuleDialog').close();
    invalidatePages();
    renderTrackRuleOptions();
    toast(remove ? '已删除规则' : '已保存规则');
    await loadTrackExceptions();
  } catch (failure) {
    error.textContent = failure.message;
  } finally {
    save.disabled = $('#trackRuleRemove').disabled = false;
  }
}

function setupTrackExceptionControls() {
  $('#addTrackRule').hidden = !ME.can_edit;
  $('#addTrackRule').onclick = () => run(() => openTrackRule(null));
  $('#trackRuleCards').onclick = event => {
    const target = event.target.closest('[data-edit-track-rule]');
    if (target) run(() => openTrackRule(target.dataset.editTrackRule));
  };
  for (const id of ['trackExceptionDays', 'trackExceptionRule', 'trackExceptionState']) {
    document.getElementById(id).onchange = () => {
      run(loadTrackExceptions);
    };
  }
  $('#trackExceptionSearch').oninput = () => {
    clearTimeout(exceptionSearchTimer);
    exceptionSearchTimer = setTimeout(() => run(loadTrackExceptions), 250);
  };
  $('#refreshTrackExceptions').onclick = () => run(async () => {
    trajectorySettings = null;
    invalidatePages();
    await loadTrackExceptions();
  });
  const form = $('#trackRuleForm');
  form.elements.anchor.onchange = form.elements.scope.onchange = refreshTrackRuleForm;
  $('#trackRuleChannelPick').onchange = event => {
    const value = event.target.value;
    if (!value) return;
    form.elements.channels.value = [...new Set([...readTrackWords(form.elements.channels.value), value])].join('\n');
    event.target.value = '';
  };
  $('#addTrackRuleSite').onclick = () => addTrackRuleSite();
  $('#trackRuleClose').onclick = () => $('#trackRuleDialog').close();
  $('#trackRuleRemove').onclick = () => run(() => saveTrackRule(true));
  form.onsubmit = event => { event.preventDefault(); run(() => saveTrackRule(false)); };
}

/* ================= 区域分析 ================= */

let UF = null;
const regionFiltersByMarket = {};
const regionTimingByMarket = {};
let regionDrilldown = null;
let usReport = null;
const usMapLoads = new Map();
let usMapPlaces = new Map();
let usSharedEdges = [];
let regionalFilters = {};
let segmentPath = '';
let segmentGeneration = -1;
const usView = {market: 'us', basis: 'paid', dimension: 'lastmile', level: 'states', metric: 'p90', place: null, channel: ''};
const usMetricLabel = {orders: '单量', avg: '平均时效', p90: 'P90 时效', p95: 'P95 时效'};
const usTimingLabel = {paid: '支付到签收', created: '创建到签收', shipped: '出库到签收',
  paid_create: '支付到创建', create_ship: '创建到出库', ship_pickup: '出库到上网', pickup_delivered: '上网到签收'};
const earlySegments = ['paid_create', 'create_ship', 'ship_pickup'];
const lateSegments = ['pickup_delivered', 'shipped', 'created', 'paid'];
const usPalette = ['#d9dedb', '#b2bdb6', '#899c90', '#627e6c', '#435f50'];
const usBlank = '#f3f3f3';
const usRegionLabels = new Set(['CA', 'CO', 'IA', 'TX', 'GA', 'VA', 'NY', 'PR']);
const usStateLabels = new Set(['WA', 'CA', 'CO', 'TX', 'IL', 'FL', 'NY']);
const regionalDuration = (minutes, basis) => Number.isFinite(minutes) ?
  earlySegments.includes(basis) ? (minutes / 60).toFixed(1) + ' 小时' : (minutes / 1440).toFixed(2) + ' 天' : '—';
const usDay = value => Number.isFinite(value) ? regionalDuration(value * 1440, usReport.basis) : '—';
const usPercent = value => value > 0 && value < 0.1 ? '&lt;0.1%' : measure(value, '%');
const usShare = (part, total) => total > 0 ? 100 * part / total : null;
const isUsCountry = code => ['us', 'usa', 'united states', 'united states of america'].includes(code.trim().toLowerCase());

function openCountryRegion(country, filters) {
  const market = isUsCountry(country) ? 'us' : 'other';
  setRegionMarket(market);
  usView.place = null;
  usView.channel = '';
  $('#usSearch').value = '';
  $('#usResults').hidden = true;
  $('#usStatus').textContent = '正在读取区域履约数据…';
  regionDrilldown = {market, filters: {...filters, cn: country}};
  showView('us');
}

async function loadUs() {
  const market = usView.market;
  const scope = 'region-' + market;
  if (!UF && !regionDrilldown && !defaultFilterPreset(scope)) {
    fetchReport(reportPath('/api/regional-analysis?' + qs({date_basis:DEFAULT_DATE_BASIS, market,
      basis:usView.basis, dimension:usView.dimension}))).catch(() => {});
  }
  await loadMeta();
  if (VIEW !== 'us' || usView.market !== market) return;
  if (!UF) {
    const isUS = market === 'us';
    const host = $(isUS ? '#usFilters' : '#otherRegionFilters');
    const defs = commonDateFilters().filter(def => !isUS || def.key !== 'cn');
    UF = regionFiltersByMarket[market] = makeFilters(host, defs, () => scheduleReport('us',loadUs));
    if (!isUS) {
      const others = (META.filterOptions.cn || []).filter(option => !isUsCountry(option.v));
      UF.cn.setOptions(others, others.reduce((sum, option) => sum + option.n, 0), false);
    }
    updateDateOptions(UF, UF.date_basis.value);
    attachFilterPresets(scope, isUS ? '区域分析 · 美国' : '区域分析 · 其他国家', host, UF, {
      onApply:() => scheduleReport('us',loadUs),
      readExtra:() => usView.market === market ? {basis:usView.basis, dimension:usView.dimension} : regionTimingByMarket[market],
      writeExtra:({basis, dimension}, base) => {
        usView.basis = $('#usBasis').value = Object.hasOwn(usTimingLabel, basis) ? basis : base.basis;
        usView.dimension = $('#usDimension').value = ['lastmile','current','group'].includes(dimension) ? dimension : base.dimension;
        usView.channel = '';
      },
      describeExtra:({basis, dimension}) => [usTimingLabel[basis], {current:'当前渠道', ...CHANNEL_DIMENSIONS}[dimension]].filter(Boolean),
    });
  }
  if (regionDrilldown?.market === market) {
    applySavedFilters(UF, regionDrilldown.filters, {date_basis: DEFAULT_DATE_BASIS});
    regionDrilldown = null;
    usView.basis = $('#usBasis').value = 'shipped';
    syncFilterPreset(UF);
  }
  const filters = readF(UF);
  updateDateOptions(UF, filters.date_basis);
  const status = $('#usStatus');
  const path = '/api/regional-analysis?' + qs({...filters, market: usView.market, basis: usView.basis, dimension: usView.dimension});
  const saved = reportRecord(reportPath(path));
  if (!saved) status.textContent = usReport ? '正在更新…' : '正在读取区域履约数据…';
  if (!saved && usReport && usReport.market !== usView.market) $('#usResults').hidden = true;
  try {
    return await presentReport('us', path, report => renderRegionalData(report, filters),
      {signal: AbortSignal.timeout(18_000)}, {oldVisible: Boolean(usReport && usReport.market === usView.market)});
  } catch (error) {
    if (VIEW !== 'us') return;
    const retry = el('button', 'btn ghost sm', '重试');
    retry.type = 'button';
    retry.onclick = () => run(loadUs);
    status.replaceChildren('区域履约数据暂时无法加载。 ', retry);
    return;
  }
}

function renderRegionalData(report, filters) {
  if (!report || VIEW !== 'us' || report.market !== usView.market ||
      report.basis !== usView.basis || report.dimension !== usView.dimension) return;
  const prior = usReport;
  usReport = report;
  regionalFilters = filters;
  $('#usStatus').textContent = `按${dateBasisLabel[report.dateBasis]}筛选（北京时间） · ${usTimingLabel[report.basis]}`;
  $('#usResults').hidden = false;
  if (prior === report) {
    loadVisibleRegionSegments();
    if (!charts.usMap) void loadUsMap(report.market);
    return;
  }
  const selector = $('#usMapChannel');
  selector.replaceChildren(new Option('全部渠道', ''), ...report.channels.map(row => new Option(row.label, row.label)));
  selector.value = usView.channel;
  if (selector.selectedIndex < 0) selector.value = '';
  usView.channel = selector.value;
  renderUsReport();
  void loadUsMap(report.market);
}

function usPlaceLabel(key) {
  if (usReport.market === 'other') return usReport.geography[key]?.label || key;
  if (usView.level === 'regions') return key;
  const state = usReport.geography[key];
  return state ? `${state.label}（${key}）` : '未识别州';
}

function renderUsReport() {
  if (!usReport || usReport.market !== usView.market) return;
  const {national, states, regions, channels} = usReport;
  const other = usReport.market === 'other';
  const isRegion = !other && usView.level === 'regions';
  const places = isRegion ? regions : states;
  const selected = usView.place === null ? national : places[usView.place];
  const country = other && regionalFilters.cn?.length === 1 ? regionalFilters.cn[0] : null;
  const countryLabel = country && (usReport.geography[country]?.label || UF?.cn?.labelFor(country) || country);
  const whole = other ? countryLabel || '其他国家合计' : '全美';
  const scope = usView.place === null ? whole : usPlaceLabel(usView.place);
  const mix = usView.place === null ? channels : selected?.channels || [];
  const selectedChannel = usView.channel;
  const tail = usView.metric === 'p95' ? 'p95' : 'p90';
  $('#usKpi').innerHTML = [
    [other ? `${countryLabel || '其他国家'}履约单` : '美国履约单', fmt(national.orders), '单量含在途单','globe','blue'],
    ['有效时效样本', fmt(national.samples), earlySegments.includes(usReport.basis) ?
      `有效覆盖 ${usPercent(usShare(national.samples, national.orders))}` : `含 WMS 补算 ${fmt(national.fallback)} 单`,'check','green'],
    ['平均时效', usDay(national.avg), usTimingLabel[usReport.basis],'clock','cyan'],
    [usMetricLabel[tail], usDay(national[tail]), `约 ${tail.slice(1)}% 的有效样本在此时长内完成`,'history','violet'],
  ].map(row => metricCard(...row)).join('');

  $('#usMixTitle').textContent = scope + '渠道构成';
  $('#usMixScope').textContent = `${fmt(selected?.orders ?? 0)} 单 · ${usTimingLabel[usReport.basis]}`;
  $('#usClear').hidden = usView.place === null;
  $('#usClear').textContent = '返回' + whole;
  $('#regionMapTitle').textContent = other ? '其他国家履约地图' : '美国履约地图';
  $('#regionMapSource').href = 'https://github.com/apache/echarts-examples/blob/gh-pages/public/data/asset/geo/' +
    (other ? 'world.json' : 'USA.json');
  $('#usLevel').hidden = other;
  $('#usMixBody').innerHTML = mix.map(row => {
    const share = usShare(row.orders, selected.orders);
    return `<tr><td><button class="order-link" type="button" data-us-channel="${esc(row.label)}"
      aria-label="在地图查看 ${esc(row.label)}">${esc(row.label)}</button></td><td>${fmt(row.orders)}</td>
      <td>${usPercent(share)}<div class="us-share-meter" aria-hidden="true"><i style="width:${share ?? 0}%"></i></div></td>
      <td>${usDay(row.avg)}</td><td>${usDay(row.p90)}</td><td>${usDay(row.p95)}</td><td>${fmt(row.samples)}</td></tr>`;
  }).join('') || blankRow(7, '当前范围无渠道数据');

  const countNote = selectedChannel ? '渠道单量 ÷ 当地总单量' : other ? '当地单量 ÷ 其他国家总单量' : '当地单量 ÷ 全美总单量';
  const source = selectedChannel || '全部渠道';
  const geographyLabel = other ? '按国家' : isRegion ? '按业务区域' : '按州';
  $('#usMapHint').textContent = `${geographyLabel} · ${source} · ${usMetricLabel[usView.metric]} · ${
    usView.metric === 'orders' ? '按履约单计' : usTimingLabel[usReport.basis]}`;
  $('#usDetailTitle').textContent = other ? '国家明细' : isRegion ? '业务区域明细' : '州级明细';
  $('#usDetailScope').textContent = `${source} · 占比为${countNote} · 点击地域查看渠道构成`;
  $('#usPlaceColumn').textContent = other ? '目的国家' : isRegion ? '业务区域' : '目的州';
  $('#usSearch').placeholder = other ? '搜索国家名称' : '搜索州名、代码或区域';
  $('#usChannelColumn').textContent = selectedChannel ? '所选渠道' : '单量最高的渠道';
  const metrics = (key, place) => {
    const data = selectedChannel ? place.channels?.find(row => row.label === selectedChannel) : place;
    const orders = data?.orders ?? 0;
    const share = usShare(orders, selectedChannel ? place.orders : national.orders);
    return {key, label: usPlaceLabel(key), region: isRegion ? key : usReport.geography[key]?.region || '未识别州',
      orders, share, avg: data?.avg, p90: data?.p90, p95: data?.p95, samples: data?.samples ?? 0,
      channel: selectedChannel || place.channels?.[0]?.label || '—',
      leadingShare: !selectedChannel && place.channels?.[0] ? usShare(place.channels[0].orders, place.orders) : null};
  };
  const placeMetrics = Object.entries(places).map(([key, place]) => metrics(key, place));
  const byPlace = new Map(placeMetrics.map(row => [row.key, row]));
  const query = $('#usSearch').value.trim().toLocaleLowerCase();
  const metric = usView.metric;
  const details = placeMetrics.filter(row => `${row.label} ${row.region} ${usReport.geography[row.key]?.name || ''}`
    .toLocaleLowerCase().includes(query)).sort((a,b) => (b[metric] ?? -1) - (a[metric] ?? -1) || a.label.localeCompare(b.label));
  $('#usDetailsBody').innerHTML = details.map(row => `<tr class="${row.key === usView.place ? 'chosen' : ''}">
    <td><button type="button" class="order-link" data-us-place="${esc(row.key)}"
      aria-pressed="${row.key === usView.place}">${esc(row.label)}</button>${other || isRegion ? '' : `<small class="cell-sub">${esc(row.region)}</small>`}</td>
    <td>${esc(row.channel)}${row.leadingShare != null ? `<small class="cell-sub">占当地 ${usPercent(row.leadingShare)}</small>` : ''}</td>
    <td>${fmt(row.orders)}</td><td>${usPercent(row.share)}</td>
    <td>${usDay(row.avg)}</td><td>${usDay(row.p90)}</td><td>${usDay(row.p95)}</td><td>${fmt(row.samples)}</td></tr>`).join('') ||
    blankRow(8, other ? '没有符合条件的国家' : '没有符合条件的州或区域');

  const mapEntries = Object.entries(usReport.geography).map(([code, state]) => [
    state.name, {key: isRegion ? state.region : code, state: state.label, code, region: state.region,
      row: byPlace.get(isRegion ? state.region : code)},
  ]);
  const names = new Set(), duplicateNames = new Set();
  for (const [name] of mapEntries) {
    if (names.has(name)) duplicateNames.add(name);
    names.add(name);
  }
  usMapPlaces = new Map(mapEntries.filter(([name]) => !duplicateNames.has(name)));
  drawUsMap();
  currentSegmentScope = scope;
  loadVisibleRegionSegments();
}

function loadVisibleRegionSegments() {
  if (regionSegmentsVisible && VIEW === 'us' && currentSegmentScope && usReport?.market === usView.market &&
      usReport.basis === usView.basis && usReport.dimension === usView.dimension) {
    void loadRegionSegments(currentSegmentScope);
  }
}

async function loadRegionSegments(scope) {
  const filters = {...regionalFilters, market: usReport.market, dimension: usReport.dimension, detail: 'segments',
    level: usView.place === null ? 'all' : usView.level, place: usView.place || '', focus: usView.channel};
  const path = '/api/regional-analysis?' + qs(filters);
  const generation = readGeneration;
  if (segmentPath === path && segmentGeneration === generation && reportFresh(reportRecord(reportPath(path)))) return;
  segmentPath = path;
  segmentGeneration = generation;
  const status = $('#regionalSegmentStatus');
  const selection = scope + ' · ' + (usView.channel || '全部渠道');
  if (!reportRecord(reportPath(path))) status.textContent = selection + ' · 正在统计各阶段…' +
    ($('#regionalSegmentContent').children.length ? '（下方为上次范围）' : '');
  try {
    return await presentReport('regionSegments', path, report => renderRegionSegments(report, path, selection),
      {signal: AbortSignal.timeout(25_000)});
  } catch (error) {
    if (segmentPath !== path || VIEW !== 'us') return;
    segmentPath = '';
    const retry = el('button', 'btn ghost sm', '重试');
    retry.type = 'button';
    retry.onclick = () => loadRegionSegments(scope);
    status.replaceChildren(selection + ' · 分段数据暂时无法加载。 ', retry);
  }
}

let renderedSegments = null, currentSegmentScope = '', regionSegmentsVisible = false;
function renderRegionSegments(report, path, selection) {
    if (!report || segmentPath !== path || VIEW !== 'us' || usView.market !== report.market) return;
    $('#regionalSegmentStatus').textContent = selection + ' · ' + fmt(report.total) + ' 单';
    if (renderedSegments === report) return;
    renderedSegments = report;
    const byKey = new Map(report.segments.map(row => [row.key, row]));
    $('#regionalSegmentContent').innerHTML = [
      ['接单与物流交接', earlySegments], ['运输与全链路', lateSegments],
    ].map(([heading, keys]) => {
      const rows = keys.map(key => byKey.get(key));
      const max = Math.max(1, ...rows.flatMap(row => [row.avgMinutes || 0, row.p90Minutes || 0]));
      return `<section class="regional-segment-group"><h3>${heading}</h3>
        <div class="regional-segment-heading"><span>阶段</span><span class="regional-segment-bar-label">平均 / P90</span>
          <span>平均</span><span>P90</span><span class="regional-segment-samples">有效样本</span></div>
        ${rows.map(row => `<div class="regional-segment-row">
          <button type="button" class="order-link" data-segment="${row.key}">${usTimingLabel[row.key]}</button>
          <div class="regional-segment-bars" aria-hidden="true">${[row.avgMinutes, row.p90Minutes].map(value =>
            `<span><i style="width:${100 * (value || 0) / max}%"></i></span>`).join('')}</div>
          <span>${regionalDuration(row.avgMinutes, row.key)}</span><span>${regionalDuration(row.p90Minutes, row.key)}</span>
          <span class="regional-segment-samples">${fmt(row.samples)}
            <small class="cell-sub">${usPercent(usShare(row.samples, report.total))}</small></span></div>`).join('')}
      </section>`;
    }).join('');
}

function regionMapName(market) {
  return market === 'us' ? 'fulfillment-us' : 'fulfillment-world';
}

function sharedStateEdges(features) {
  const edges = new Map();
  const shared = [];
  for (const {properties, geometry} of features) {
    const name = properties.name;
    const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
    for (const rings of polygons) for (const ring of rings) {
      for (let i = 1; i < ring.length; i++) {
        const a = ring[i - 1], b = ring[i];
        const start = a.join(','), end = b.join(',');
        const key = start < end ? start + '|' + end : end + '|' + start;
        const neighbor = edges.get(key);
        if (neighbor && neighbor !== name) shared.push({a, b, left: neighbor, right: name});
        else edges.set(key, name);
      }
    }
  }
  return shared;
}

function drawUsRegionBorders(chart) {
  const children = [];
  for (const {a, b, left, right} of usSharedEdges) {
    if (usMapPlaces.get(left)?.region === usMapPlaces.get(right)?.region) continue;
    const [x1, y1] = chart.convertToPixel({seriesIndex: 0}, a) || [];
    const [x2, y2] = chart.convertToPixel({seriesIndex: 0}, b) || [];
    if (![x1, y1, x2, y2].every(Number.isFinite)) continue;
    children.push({type:'line', silent:true, shape:{x1, y1, x2, y2}, z:3,
      style:{stroke:chartTheme.series[2], lineWidth:1.8, lineCap:'round'}});
  }
  if (children.length) chart.setOption({graphic: [{type:'group', silent:true, children}]});
}

function drawUsMap() {
  const chart = charts.usMap;
  if (!chart || !usReport || VIEW !== 'us' || !echarts.getMap(regionMapName(usReport.market))) return;
  const metric = usView.metric;
  const other = usReport.market === 'other';
  const byRegion = !other && usView.level === 'regions';
  const compact = chart.getWidth() < 560;
  const hourly = earlySegments.includes(usReport.basis);
  const format = metric === 'orders' ? n => fmt(n) + ' 单' : usDay;
  const mapValue = row => {
    const value = row?.[metric];
    if (!Number.isFinite(value) || value <= 0) return NaN;
    if (metric === 'orders') return value;
    return hourly ? Number((value * 24).toFixed(1)) / 24 : Number(value.toFixed(2));
  };
  const unique = [...new Set([...usMapPlaces.values()].map(place => mapValue(place.row)).filter(Number.isFinite))]
    .sort((a, b) => a - b);
  const bands = Math.min(usPalette.length, unique.length);
  const cuts = Array.from({length: Math.max(0, bands - 1)}, (_, index) =>
    unique[Math.ceil((index + 1) * unique.length / bands) - 1]);
  const pieces = Array.from({length: bands}, (_, index) => {
    const color = usPalette[bands === 1 ? 2 : Math.round(index * (usPalette.length - 1) / (bands - 1))];
    const min = cuts[index - 1], max = cuts[index];
    if (bands === 1) return {value: unique[0], color, label: format(unique[0])};
    return {...(min == null ? {} : {gt: min}), ...(max == null ? {} : {lte: max}), color,
      label: min == null ? '≤ ' + format(max) : max == null ? '> ' + format(min) : `> ${format(min)} 至 ${format(max)}`};
  });
  $('#usLegend').innerHTML = [...pieces, {color: usBlank, label: '无数据或样本'}]
    .map(piece => `<span><i style="background:${piece.color}"></i>${piece.label}</span>`).join('');
  chart.setOption({
    animation: false,
    tooltip: {...chartTip, trigger: 'item', formatter: item => {
      const place = usMapPlaces.get(item.name);
      if (!place) return esc(item.name);
      const row = place.row;
      const title = byRegion ? place.region : other ? place.state : `${place.state}（${place.code}） · ${place.region}`;
      return `<strong>${esc(title)}</strong><br>${
        row ? `单量 ${fmt(row.orders)} · 占比 ${usPercent(row.share)}<br>平均 ${usDay(row.avg)} · P90 ${usDay(row.p90)} · P95 ${usDay(row.p95)}<br>有效样本 ${fmt(row.samples)} 单` : '当前范围无记录'}`;
    }},
    visualMap: [{type: 'piecewise', show: false, pieces, outOfRange: {color: usBlank}}],
    series: [{type: 'map', map: regionMapName(usReport.market), roam: false, layoutCenter: ['50%', '50%'], layoutSize: '140%',
      itemStyle: {areaColor: usBlank, borderColor: chartTheme.paper, borderWidth: byRegion ? 0.6 : 1},
      emphasis: {label: {show: false}, itemStyle: {borderColor: chartTheme.ink, borderWidth: 1.6}},
      data: [...usMapPlaces].map(([name, place]) => {
        const value = mapValue(place.row);
        const selected = place.key === usView.place;
        const anchor = byRegion ? usRegionLabels.has(place.code) : !other && usStateLabels.has(place.code) || selected;
        const caption = byRegion ? place.region : other || !compact ? place.state : place.code;
        const label = anchor && Number.isFinite(value) ? {show:true,
          formatter:`${caption}\n${format(value)}`, color:chartTheme.ink, fontSize:compact ? 10 : 12,
          fontWeight:700, lineHeight:compact ? 12 : 16, textBorderColor:chartTheme.paper, textBorderWidth:3} : undefined;
        return {name, value, label, emphasis:label ? {label} : undefined,
          itemStyle:selected ? {borderColor:chartTheme.ink, borderWidth:2} : undefined};
      }),
    }],
  }, {notMerge: true});
  if (byRegion) drawUsRegionBorders(chart);
  resizeVisibleCharts();
}

function prepareUsMap(market) {
  const mapName = regionMapName(market);
  if (!usMapLoads.has(market)) {
    const asset = market === 'us' ? '/static/us-states.geojson' : '/static/world-countries.geojson';
    usMapLoads.set(market, Promise.all([
      loadChartLibrary(),
      fetch(asset + '?v=1', {signal: AbortSignal.timeout(12_000), priority:'low'}).then(response => {
        if (!response.ok) throw new Error('地图读取失败');
        return response.json();
      }),
    ]).then(([, geo]) => {
      if (market === 'us') usSharedEdges = sharedStateEdges(geo.features);
      echarts.registerMap(mapName, geo, market === 'us' ? {
        Alaska: {left: -131, top: 25, width: 15}, Hawaii: {left: -110, top: 28, width: 5},
        'Puerto Rico': {left: -76, top: 26, width: 2},
      } : undefined);
    }).catch(error => { usMapLoads.delete(market); throw error; }));
  }
  return usMapLoads.get(market);
}

async function loadUsMap(market = usReport?.market) {
  if (!market) return;
  const mapName = regionMapName(market);
  const ready = prepareUsMap(market);
  if (!window.echarts || !echarts.getMap(mapName)) {
    charts.usMap?.clear();
    $('#usLegend').replaceChildren();
    $('#usMapStatus').hidden = false;
    $('#usMapStatus').textContent = '正在加载地图…';
  }
  try {
    await ready;
  } catch {
    if (usView.market !== market) return;
    const retry = el('button', 'btn ghost sm', '重试地图');
    retry.type = 'button';
    retry.onclick = () => loadUsMap(market);
    $('#usMapStatus').replaceChildren('地图暂时无法加载。 ', retry);
    return;
  }
  if (usReport?.market !== market || VIEW !== 'us') return;
  if (!charts.usMap) {
    const chart = initChart('usMap');
    chart.on('click', event => {
      const place = usMapPlaces.get(event.name);
      if (place) selectUsPlace(place.key);
    });
  }
  $('#usMapStatus').hidden = true;
  drawUsMap();
}

function selectUsPlace(key) {
  usView.place = usView.place === key ? null : key;
  renderUsReport();
}

function setupUsControls() {
  for (const [id, field] of [['usLevel', 'level'], ['usMetric', 'metric']]) {
    const container = document.getElementById(id);
    container.onclick = event => {
      const button = event.target.closest('button[data-value]');
      if (!button || usView[field] === button.dataset.value) return;
      usView[field] = button.dataset.value;
      container.querySelectorAll('button').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      if (field === 'level') usView.place = null;
      renderUsReport();
    };
  }
  $('#usBasis').onchange = event => {
    usView.basis = event.target.value;
    syncFilterPreset(UF);
    run(loadUs);
  };
  $('#regionalSegmentContent').onclick = event => {
    const target = event.target.closest('[data-segment]');
    if (!target) return;
    usView.basis = $('#usBasis').value = target.dataset.segment;
    syncFilterPreset(UF);
    if (usView.metric === 'orders') {
      usView.metric = 'p90';
      $('#usMetric').querySelectorAll('button').forEach(button =>
        button.setAttribute('aria-pressed', String(button.dataset.value === 'p90')));
    }
    run(loadUs);
  };
  $('#regionMarkets').onclick = event => {
    const button = event.target.closest('button[data-value]');
    if (!button || usView.market === button.dataset.value) return;
    setRegionMarket(button.dataset.value);
    showView('us');
  };
  $('#usDimension').onchange = event => {
    usView.dimension = event.target.value;
    usView.channel = '';
    syncFilterPreset(UF);
    run(loadUs);
  };
  $('#usMapChannel').onchange = event => {
    usView.channel = event.target.value;
    renderUsReport();
  };
  $('#usMixBody').onclick = event => {
    const button = event.target.closest('[data-us-channel]');
    if (!button) return;
    usView.channel = $('#usMapChannel').value = button.dataset.usChannel;
    renderUsReport();
  };
  $('#usDetailsBody').onclick = event => {
    const button = event.target.closest('[data-us-place]');
    if (!button) return;
    const key = button.dataset.usPlace;
    selectUsPlace(key);
    [...$('#usDetailsBody').querySelectorAll('[data-us-place]')].find(item => item.dataset.usPlace === key)?.focus({preventScroll: true});
  };
  $('#usSearch').oninput = renderUsReport;
  $('#usClear').onclick = () => selectUsPlace(null);
}

function setRegionMarket(market) {
  if (market === usView.market) return;
  regionDrilldown = null;
  regionTimingByMarket[usView.market] = {basis:usView.basis, dimension:usView.dimension};
  usView.market = market;
  UF = regionFiltersByMarket[market] || null;
  const timing = regionTimingByMarket[market] || {basis:'paid',dimension:'lastmile'};
  usView.basis = $('#usBasis').value = timing.basis;
  usView.dimension = $('#usDimension').value = timing.dimension;
  $('#usFilters').hidden = market !== 'us';
  $('#otherRegionFilters').hidden = market === 'us';
  usView.level = 'states';
  usView.place = null;
  usView.channel = '';
  $('#usSearch').value = '';
  $('#regionMarkets').querySelectorAll('button').forEach(button =>
    button.setAttribute('aria-pressed', String(button.dataset.value === market)));
  $('#usLevel').querySelectorAll('button').forEach(button =>
    button.setAttribute('aria-pressed', String(button.dataset.value === 'states')));
}
/* ================= 数据与规则 ================= */
let activeConfiguration = null;
let slaGroupFilter;
let slaFilters;

const syncRunStates = {
  running: ['blue', '同步中'], ok: ['ok', '同步成功'], idle: ['m', '无待查运单'],
  failed: ['e', '同步失败'],
};
const pushStates = { queued: ['blue', '待入库'], applied: ['ok', '已入库'], ignored: ['m', '已核对'] };
const syncReasons = {
  credentials: '请检查本机 17TRACK 凭据', prepare: '查询或准备数据失败', publish: '数据库切换失败',
  interrupted: '上次任务中断', unmatched: '待匹配履约单', postnet: '按规则跳过 Postnet',
  not_in_scope: '不在当前处理范围',
  outside_tracking_window: '出库超过五个月，已停止更新',
  different_carrier: '与已匹配的承运商代码不同', older: '本地已有更新的轨迹',
  notification_only: '通知已记录',
};
const verificationStatus = {
  verified: '已核对逐条轨迹', unmatched: 'API 账号登记未匹配', empty: '接口未返回可核对的逐条轨迹',
  read_error: '接口读取失败 · 待重试', carrier_conflict: '承运商不匹配 · 待核对',
  older: '接口结果较旧或缺少事件时间 · 保留已有轨迹',
};
const syncBadge = (tone, label) => `<span class="badge ${tone}">${esc(label)}</span>`;
const syncClock = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
});
const syncTime = value => value && Number.isFinite(Date.parse(value)) ? syncClock.format(new Date(value)) : '—';
const sourceRows = rows => rows.map(([label, value]) =>
  `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');

const syncStatusPath = '/api/17track/sync-status';

async function loadSyncStatus() {
  return presentReport('trackSync', syncStatusPath, renderSyncStatus,
    {cache: 'no-store'}, {force: true, oldVisible: Boolean($('#pollSummary').children.length)});
}

let fullPullStarting = false;
let fullPullState = null;
let fullPullAvailable = false;

function renderFullPull(job, available) {
  fullPullState = job;
  fullPullAvailable = available;
  const button = $('#startFullPull');
  const progress = $('#fullPullProgress');
  const detail = job?.detail || {};
  const active = ['queued', 'running'].includes(job?.status);
  const resume = job?.status === 'failed' && detail.total != null;
  button.disabled = !ME.can_edit || !available || active || fullPullStarting;
  button.textContent = active ? '全量拉取中' : resume ? '继续拉取轨迹' : '全量拉取轨迹';
  progress.hidden = !detail.total;
  if (detail.total) {
    progress.max = detail.total;
    progress.value = detail.processed;
  }
  const reason = {credentials:'本机 17TRACK 凭据不可用', launch:'本机任务启动失败', batch:'批次处理失败'};
  const label = !job ? '暂无全量拉取任务' : job.status === 'queued' ? '已排队，等待本机运行' :
    job.status === 'running' ? '正在分批查询与入库' : job.status === 'ok' ? '本轮已完成' :
      reason[detail.reason] || '任务失败';
  const counts = detail.total == null ? '' :
    ` · 已处理 ${fmt(detail.processed)} / ${fmt(detail.total)} 个运单 · 取回逐条轨迹 ${fmt(detail.with_events)}`;
  const outcomes = [['账号未登记', detail.unmatched], ['承运商不匹配', detail.carrier_conflict],
    ['未返回轨迹', detail.empty], ['查询失败', detail.read_error], ['本地轨迹较新', detail.older],
    ['已不符合查询范围', detail.skipped]].filter(([, count]) => count)
    .map(([name, count]) => `${name} ${fmt(count)}`).join(' · ');
  const importSummary = detail.lastImportWaybills ? ` · ${detail.trigger === 'import' ? '入库自动触发' : '已并入入库更新'}` +
    ` · 最近导入有 ${fmt(detail.lastImportWaybills)} 个运单进入追踪范围` : '';
  $('#fullPullStatus').textContent = label + counts + importSummary + (outcomes ? ' · ' + outcomes : '') +
    (job?.finished_at ? ' · ' + syncTime(job.finished_at) : '');
}

async function startFullPull() {
  if (fullPullStarting || $('#startFullPull').disabled) return;
  fullPullStarting = true;
  $('#startFullPull').disabled = true;
  try {
    const job = await api('/api/17track/full-pull', {method:'POST', signal:AbortSignal.timeout(12_000)});
    renderFullPull(job, true);
    const previous = reportRecord(syncStatusPath);
    if (previous) persistReport(syncStatusPath, {...previous, data:{...previous.data, fullPull:job}});
    reportInflight.delete(syncStatusPath);
    await loadSyncStatus();
  } finally {
    fullPullStarting = false;
    renderFullPull(fullPullState, fullPullAvailable);
  }
}

function renderSyncStatus(state) {
  const { poll, webhook } = state;
  renderFullPull(state.fullPull || null, Boolean(state.fullPullAvailable));
  const run = poll.runs[0];
  let pollBadge = run ? syncRunStates[run.status] : ['gray', '等待首次同步'];
  if (poll.schedule?.state === 'Running' && run?.status !== 'running') pollBadge = syncRunStates.running;
  if (poll.schedule?.state === 'Ready' && poll.schedule.result && poll.schedule.result !== 267011) {
    pollBadge = ['e', '上次计划任务异常'];
  }
  if (poll.schedule?.state === 'Ready' && !poll.schedule.result && run &&
      new Date(state.now) - new Date(run.started_at) > 135 * 60 * 1000) {
    pollBadge = ['h', '已超过更新间隔'];
  }
  if (poll.schedule?.state === 'Disabled') pollBadge = ['h', '任务已停用'];
  if (run?.status === 'running' && new Date(state.now) - new Date(run.started_at) > 20 * 60 * 1000) {
    pollBadge = ['e', '任务长时间未完成'];
  }
  const [pollTone, pollLabel] = pollBadge || ['gray', '状态待确认'];
  $('#pollStatus').className = 'badge ' + pollTone;
  $('#pollStatus').textContent = pollLabel;
  $('#pollSummary').innerHTML = sourceRows([
    ['查询范围', '近 3 个月出库的合规运单，以及出库 3–5 个月且未签收的合规运单'],
    ['运行计划', poll.schedule ? '每 2 小时检查 · 单批最多 4,000 运单' : '计划任务状态暂不可读'],
    ...(poll.coverage ? [
      ['待持续核对范围', `${fmt(poll.coverage.orders)} 单 · 出库 3–5 个月 ${fmt(poll.coverage.olderOrders)} 单`],
      ['逐单核对进度', `近 24 小时成功 ${fmt(poll.coverage.verified24h)} 单 · 待首次核对 ${fmt(poll.coverage.uncheckedOrders)} 单 · 最近一次异常 ${fmt(poll.coverage.exceptionOrders)} 单`],
      ['出库 3–5 个月的未签收单', `近 24 小时成功 ${fmt(poll.coverage.olderVerified24h)} 单 · 待首次核对 ${fmt(poll.coverage.olderUnchecked)} 单`],
    ] : []),
    ['最近成功', syncTime(poll.lastSuccess)],
    ['最近一批', run?.detail?.queried != null ?
      `查询 ${fmt(run.detail.queried)} 个运单 · 取回 ${fmt(run.detail.with_events)} 个运单的逐条轨迹` :
      syncReasons[run?.detail?.reason] || syncRunStates[run?.status]?.[1] || '—'],
    ['下次计划', syncTime(poll.schedule?.next)],
  ]);
  const [pushTone, pushLabel] = !webhook.ready ? ['h', '待配置验签'] :
    !webhook.url ? ['h', '待接入公网'] :
      webhook.lastReceived ? ['ok', '已收到推送'] : ['blue', '等待首次推送'];
  $('#webhookStatus').className = 'badge ' + pushTone;
  $('#webhookStatus').textContent = pushLabel;
  $('#webhookSummary').innerHTML = sourceRows([
    ['公网回调地址', webhook.url || '尚未配置'],
    ['最近收到', syncTime(webhook.lastReceived)],
    ['最近入库', syncTime(webhook.lastApplied)],
    ['推送处理', `待入库 ${fmt(webhook.queued)} 条 · 已入库 ${fmt(webhook.applied)} 条`],
  ]);
  $('#webhookHelp').textContent = !webhook.url ?
    '需要将公网 HTTPS 回调地址配置到 17TRACK 后台。' : '验签通过后入队，本机每 5 分钟检查待入库的推送。';
  $('#syncObserved').textContent = `状态读取于 ${syncTime(state.now)} · 此页每 30 秒刷新 · 北京时间`;
  $('#carrierConflicts').innerHTML = (poll.carrierConflicts || []).map(row =>
    `<tr><td>${esc(row.provider || '—')}</td><td>${esc(row.channel || '—')}</td><td>${esc(String(row.expectedCarrier ?? '—'))}</td><td>${esc(row.registeredCodes === '' ? '账号无登记' : row.registeredCodes ?? '尚未记录')}</td><td>${fmt(row.orders)}</td></tr>`
  ).join('') || blankRow(5, '暂无承运商匹配异常');
  $('#openCarrierConflicts').onclick = () => openTracking({coverage: 'carrier_conflict'});
  $('#pollHistory').innerHTML = poll.runs.map(row => {
    const [tone, label] = syncRunStates[row.status] || ['gray', '状态待确认'];
    return `<tr><td>${syncTime(row.started_at)}</td><td>${syncTime(row.finished_at)}</td>
      <td>${syncBadge(tone, label)}${row.detail?.reason ? `<small class="cell-sub">${esc(syncReasons[row.detail.reason] || '')}</small>` : ''}</td>
      <td>${fmt(row.detail?.queried)}</td><td>${fmt(row.detail?.with_events)}</td><td>${fmt(row.detail?.unmatched)}</td></tr>`;
  }).join('') || blankRow(6, '暂无同步记录');
  $('#webhookHistory').innerHTML = webhook.pushes.map(row => {
    const [tone, label] = pushStates[row.state] || ['gray', '状态待确认'];
    const reason = row.reason === 'unmatched' && row.state === 'ignored' ? '未匹配履约单' : syncReasons[row.reason];
    return `<tr><td>${syncTime(row.received_at)}</td><td>${esc(row.event)}</td>
      <td>${syncBadge(tone, label)}${reason ? `<small class="cell-sub">${esc(reason)}</small>` : ''}</td><td>${syncTime(row.processed_at)}</td></tr>`;
  }).join('') || blankRow(4, '暂无收到的推送');
}

function refreshSyncStatus() {
  loadSyncStatus().catch(() => {
    $('#syncObserved').textContent = '暂时读不到同步状态，稍后会自动重试。';
  });
}

function updateActivity(pending, importedAt, through) {
  $('#alertTabCount').textContent = fmt(pending);
  $('#pendingCount').textContent = `${fmt(pending)} 单`;
  lastImportAt = importedAt;
  setTopMeta(through);
}

async function loadActivity() {
  return presentReport('activity', '/api/activity', d => updateActivity(d.pendingOrders, d.lastImportAt, META?.range.end));
}

let renderedHealth = null;
async function loadHealth() {
  return presentReport('health', '/api/health', renderHealth, {}, {oldVisible: Boolean($('#dataKpi').children.length)});
}

function renderHealth(d) {
  if (renderedHealth === d) return;
  renderedHealth = d;
  updateActivity(d.pendingOrders, d.recentImports[0]?.ingested_at, d.range.end);
  const t = d.tracking;
  $('#dataKpi').innerHTML = [
    ['ERP 履约单', fmt(d.orders), `${d.range.start} 至 ${d.range.end}`,'box','blue'],
    ['17TRACK 查询记录', fmt(t.records), '按运单号统计','route','cyan'],
    ['已有轨迹事件', fmt(t.withEvent), '有最新事件时间','check','green'],
    ['登记未匹配', fmt(t.unmatched), '历史详情查询返回未匹配','alert',t.unmatched ? 'warn' : 'green'],
    ['可查看逐条轨迹的运单', fmt(t.detailedNumbers), `${fmt(t.detailedEvents)} 条承运商事件`,'list','violet'],
  ].map(row => metricCard(...row)).join('');
  $('#sourceHealth').innerHTML = [
    ['ERP / WMS', `建单截止 ${d.range.end} · 最近导入 ${dt(lastImportAt)}`],
    ['17TRACK', `最近查询 ${dt(t.lastCheck)} · 历史回补 ${fmt(t.archiveNumbers)} 个运单 · 已查询无轨迹 ${fmt(t.empty)} 个运单`],
    ['业务预警', `${fmt(d.alerts)} 条规则命中 · ${fmt(d.pendingOrders)} 张单待跟进`],
    ['最近后台任务', d.lastJob ? `${d.lastJob.job} · ${dt(d.lastJob.started_at)} · ${d.lastJob.status}` : '暂无运行日志'],
  ].map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('');
  $('#importBody').innerHTML = d.recentImports.map(row =>
    `<tr><td>${esc(row.filename)}</td><td class="mono">${dt(row.ingested_at)}</td></tr>`).join('') ||
    blankRow(2, '暂无导入记录');
}

function setRulePane(pane) {
  document.querySelectorAll('#ruleTabs [data-rule-pane]').forEach(button => {
    const selected = button.dataset.rulePane === pane;
    button.setAttribute('aria-pressed', String(selected));
    $('#rules-' + button.dataset.rulePane).hidden = !selected;
  });
  $('#refreshRules').hidden = pane === 'definitions';
  $('#reportCacheStatus').hidden = true;
  run(loadRules);
}

async function loadRuleReference() {
  const path = '/api/reference?v=1';
  const record = reportRecord(path) || await fetchReport(path);
  return record.data;
}

async function loadRules() {
  if (!$('#rules-definitions').hidden) {
    if (!$('#weekCalendarBody').rows.length) drawWeekCalendar((await loadRuleReference()).weeks);
    return;
  }
  if (!$('#rules-data').hidden) return loadHealth();
  if (!$('#rules-sync').hidden) return refreshSyncStatus();
  if (!$('#rules-channel-groups').hidden) return loadChannelGroups();
  if (!$('#rules-dictionary').hidden) {
    if ($('#dictionaryBody').rows.length) return;
    const {trackStatuses} = await loadRuleReference();
    $('#dictionaryBody').innerHTML = trackStatuses.map(row =>
      `<tr><td>${esc(row.label)}</td><td class="mono">${esc(row.status)}</td><td>${esc(row.sub_label)}</td>
        <td class="mono">${esc(row.sub)}</td></tr>`).join('') || blankRow(4, '暂无状态字典');
    return;
  }
  if (!$('#rules-sla').hidden && !slaGroupFilter) {
    await loadMeta();
    if (!slaGroupFilter) {
      slaGroupFilter = multiFilter({
        key: 'channel_group', label: '自定义渠道', options: META.filterOptions.channel_group || [],
      }, () => { syncFilterPreset(slaFilters); renderSLATable(); });
      const host = $('#slaGroupFilter');
      host.append(slaGroupFilter.root);
      slaFilters = {q:$('#slaSearch'), channel_group:slaGroupFilter};
      attachFilterPresets('sla', '渠道 SLA', host, slaFilters, {onApply:renderSLATable});
      renderSLATable();
    }
  }
  return presentReport('rules', '/api/rules', renderRules, {}, {oldVisible: Boolean(RULES)});
}

function renderRules(d) {
  if (RULES === d) return;
  RULES = d;
  $('#ruleAsof').textContent = `数据截止 ${d.asof} · 工作日：${d.workdayRule}`;
  $('#ruleList').innerHTML = (d.rules || []).map(r => `
    <div class="rule">
      <div class="rh">
      <span class="badge ${levelTone(r.level)}">${esc(r.level)}</span>
        <b>${esc(r.name)}</b>
        <span class="cat">${esc(r.category)}</span>
        <span class="badge ${r.enabled ? 'ok' : 'gray'}">${r.enabled ? '已启用' : '已停用'}</span>
        <span class="sp"></span>
        ${ME.role === 'admin' ? `<button type="button" class="btn ghost sm" data-config-rule="${esc(r.key)}">配置</button>` : ''}
        <button type="button" class="btn ghost sm" data-rule-track="${esc(r.key)}">查看命中</button>
        <span class="hit">主预警 ${fmt(r.hits || 0)} 单</span>
      </div>
      <dl>
        <dt>触发条件</dt><dd>${esc(r.trigger)}</dd>
        <dt>判定依据</dt><dd>${esc(r.basis)}</dd>
        <dt>排除条件</dt><dd>${esc(r.exclude)}</dd>
        <dt>恢复方式</dt><dd>${esc(r.recovery)}</dd>
      </dl>
    </div>`).join('');

  renderSLATable();

  $('#notImpl').innerHTML = (d.notImplemented || []).map(x =>
    `<div class="note"><b>${esc(x.name)}</b><span> · 需要 ${esc(x.need)}</span></div>`).join('');

}

function renderSLATable() {
  const query = $('#slaSearch').value.trim().toLocaleLowerCase();
  const groups = slaGroupFilter?.value || [];
  $('#slaBody').innerHTML = (RULES?.channels || []).filter(row =>
    row.ch.toLocaleLowerCase().includes(query) && (!groups.length || groups.includes(row.channelGroup)))
    .map(r => `<tr>
      <td><b>${esc(r.ch)}</b></td><td>${fmt(r.n)}</td>
      <td>${TIMING_BASIS[r.basis].label}</td><td>${r.target} 天</td><td>${fmt(r.samples)} / ${fmt(r.signed)}</td>
      ${[3, 5, 6].map(day => {
        const target = r.targets[`d${day}`];
        const actual = r[`rate${day}`];
        const tone = actual == null ? 'gray' : actual >= target ? 'ok' : 'h';
        return `<td>${measure(target, '%')} / <span class="badge ${tone}">${measure(actual, '%')}</span></td>`;
      }).join('')}
      <td>${measure(r.avgDays, ' 天')}</td>
      <td><span class="badge ${r.customized ? 'blue' : 'gray'}">${r.customized ? '独立配置' : '沿用初始值'}</span>
        ${ME.role === 'admin' ? `<button type="button" class="btn ghost sm" data-config-sla="${esc(r.ch)}">编辑</button>` : ''}</td>
    </tr>`).join('') || blankRow(10, '没有符合条件的当前渠道');
}

async function openConfiguration(kind, key) {
  if (ME.role !== 'admin') return;
  if (!reportFresh(reportRecord('/api/rules'))) {
    const record = await fetchReport('/api/rules', {}, true);
    renderRules(record.data);
  }
  const isRule = kind === 'rule';
  const item = isRule ? RULES.rules.find(row => row.key === key) : RULES.channels.find(row => row.ch === key);
  if (!item || ME.role !== 'admin') return;
  const numberField = (name, label, value, min, max, step = 1) =>
    `<label><span>${label}</span><input name="${name}" type="number" value="${value}" min="${min}" max="${max}" step="${step}" required></label>`;
  activeConfiguration = { isRule, key, hasThreshold: item.threshold != null };
  $('#settingsTitle').textContent = isRule ? item.name : item.ch;
  $('#settingsError').textContent = '';
  $('#settingsFields').innerHTML = isRule ? `
    <label class="settings-check"><input name="enabled" type="checkbox" ${item.enabled ? 'checked' : ''}>启用这条规则</label>
    <label><span>预警级别</span><select name="level">${['紧急', '高', '中'].map(level =>
      `<option${level === item.level ? ' selected' : ''}>${level}</option>`).join('')}</select></label>
    ${item.threshold != null ? numberField('threshold',
      `时效阈值（${esc(item.thresholdUnit)}）`, item.threshold, 0, 3650) : ''}
    <p class="muted settings-hint">${esc(item.basis)}</p>` : `
    <label><span>SLA 计时口径</span><select name="basis" aria-describedby="slaBasisNote">${Object.entries(TIMING_BASIS)
      .map(([value, option]) => `<option value="${value}"${value === item.basis ? ' selected' : ''}>${option.label}</option>`).join('')}</select></label>
    <p class="muted settings-hint" id="slaBasisNote">${TIMING_BASIS[item.basis].note}</p>
    ${numberField('days', '承诺时效（天）', item.target, 1, 3650)}
    ${[3, 5, 6].map(day => numberField(`d${day}`, `${day} 天达成率目标（%）`, item.targets[`d${day}`], 0, 100, 'any')).join('')}
    <p class="muted settings-hint">适用于履约单的当前渠道，保存后更新 SLA 对比和运输超时预警。</p>`;
  const basis = $('#settingsFields select[name="basis"]');
  if (basis) basis.onchange = () => $('#slaBasisNote').textContent = TIMING_BASIS[basis.value].note;
  $('#settingsDialog').showModal();
}

async function saveConfiguration(event) {
  event.preventDefault();
  const { isRule, key, hasThreshold } = activeConfiguration;
  const fields = new FormData(event.currentTarget);
  const body = isRule ? { enabled: fields.has('enabled'), level: fields.get('level'),
    threshold: hasThreshold ? Number(fields.get('threshold')) : null } :
    { channel: key, basis: fields.get('basis'), days: Number(fields.get('days')), targets: Object.fromEntries(
      ['d3', 'd5', 'd6'].map(day => [day, Number(fields.get(day))])
    ) };
  const button = $('#settingsSave');
  const cancel = $('#settingsCancel');
  const close = $('#settingsClose');
  [button, cancel, close].forEach(element => element.disabled = true);
  button.textContent = '正在重算…';
  $('#settingsError').textContent = '';
  try {
    await api(isRule ? `/api/rules/business/${encodeURIComponent(key)}` : '/api/rules/channel-sla', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    $('#settingsDialog').close();
    toast('配置已保存，预警已重算');
    await reloadRules();
  } catch (error) {
    if ($('#settingsDialog').open) $('#settingsError').textContent = error.message;
    else toast(error.message);
  } finally {
    [button, cancel, close].forEach(element => element.disabled = false);
    button.textContent = '保存并重算';
  }
}

async function reloadRules() {
  invalidatePages();
  await Promise.all([loadRules(), loadActivity()]);
}

/* ================= 详情抽屉 ================= */
let drawerTrigger = null;

const carrierEventLabels = {
  delivered: ['妥投', 'delivered', 'check'],
  dispatch: ['外出派送', 'dispatch', 'pin'],
  pickup: ['承运商已提取', 'pickup', 'box'],
  collection: ['待自提', 'pickup', 'pin'],
  customsHold: ['疑似卡关', 'attention', 'alert'],
  customsOpen: ['清关中', 'customs', 'globe'],
  customsReleased: ['清关放行', 'customs', 'globe'],
  failure: ['派送失败', 'attention', 'alert'],
};

function carrierEventLabel(stage, sub, description) {
  if (sub === 'InTransit_CustomsRequiringInformation' ||
      /clearance delay|customs (?:delay|hold)|held by customs|held in customs|清关延误|海关扣留|清关需补充材料|等待清关资料/i.test(description)) {
    return carrierEventLabels.customsHold;
  }
  if (stage === 'DeliveryFailure' || sub.startsWith('DeliveryFailure_') ||
      /attempted delivery|delivery attempt (?:failed|unsuccessful|made)|delivery failed|not delivered|派送失败|投递失败|未妥投/i.test(description)) {
    return carrierEventLabels.failure;
  }
  if (stage === 'AvailableForPickup' || sub.startsWith('AvailableForPickup_') ||
      /available for (?:pickup|collection)|ready for (?:pickup|collection)|待自提|已到达取件点/i.test(description)) {
    return carrierEventLabels.collection;
  }
  if (stage === 'Delivered' || sub.startsWith('Delivered_') || /^delivered\b|^已签收|^已妥投/i.test(description)) {
    return carrierEventLabels.delivered;
  }
  if (stage === 'OutForDelivery' || sub.startsWith('OutForDelivery_') ||
      /out for delivery|外出派送|正在派送|^派送中/i.test(description)) return carrierEventLabels.dispatch;
  if (stage === 'PickedUp' || sub === 'InTransit_PickedUp' ||
      /^(?:shipment )?picked up\b|^shipment pickup\b|^已揽收|^承运商已提取/i.test(description)) {
    return carrierEventLabels.pickup;
  }
  if (sub === 'InTransit_CustomsReleased' ||
      /shipment release|cleared customs|customs clearance completed|清关完成|海关放行/i.test(description)) {
    return carrierEventLabels.customsReleased;
  }
  if (sub === 'InTransit_CustomsProcessing' || /清关中/i.test(description)) return carrierEventLabels.customsOpen;
  return null;
}

function carrierEventText(stage, sub, description) {
  const label = carrierEventLabel(stage, sub || '', description || '');
  const badge = label ? `<span class="event-label ${label[1]}">${icon(label[2])}${label[0]}</span>` : '';
  const text = description || (label ? '' : '承运商未提供事件描述');
  return `<div class="d carrier-event-text">${badge}${text === label?.[0] ? '' : esc(text)}</div>`;
}

let drawerOrder = null;
let drawerPrefetch = null;
let drawerIntentTimer;
const orderDetailsPath = no => '/api/orders/' + encodeURIComponent(no) +
  (ME.preview ? '?q=' + encodeURIComponent(lastTrackingQuery) : '');

function preloadOrder(no, priority = 'auto') {
  if (drawerPrefetch || drawerOrder || VIEW !== 'track' || document.hidden || navigator.connection?.saveData) return;
  const path = orderDetailsPath(no);
  const cached = pageResponses.get(path);
  if (cached && (!cached.until || cached.until > performance.now())) return;
  drawerPrefetch = readPage(path, {signal: AbortSignal.timeout(12_000), priority})
    .catch(() => {}).finally(() => { drawerPrefetch = null; });
}

function watchOrderIntent(event) {
  const button = event.target.closest('[data-order]');
  if (!button || (event.relatedTarget instanceof Node && button.contains(event.relatedTarget))) return;
  clearTimeout(drawerIntentTimer);
  drawerIntentTimer = setTimeout(() => {
    if (button.isConnected && button.matches(':hover, :focus-visible')) preloadOrder(button.dataset.order);
  }, 120);
}

function orderFields(fields) {
  return `<div class="kv">${fields.map(([label, value]) =>
    `<div class="i">${esc(label)}<b>${esc(value) || '—'}</b></div>`).join('')}</div>`;
}

function orderTrackStatus(order) {
  return order.状态标签 || order.轨迹状态中文 || (order.核对结果 === 'carrier_conflict' ? '承运商待核对' :
    order.WMS签收异常 ? 'WMS 签收时间异常' : order.未注册 ? 'API 账号登记未匹配' :
    order.回补状态 === 'empty' ? '已查询 · 未返回轨迹' :
    order.已签收 ? 'WMS 已签收 · 待核实' : order.签出时间 ? '已出库 · 待同步轨迹' : '无本地查询结果');
}

function setDrawerOrderLink(no) {
  const link = $('#dWmsOrder');
  link.href = 'https://bit.htwig.com/wms/outboard/#/perform/detail?performOrderNo=' + encodeURIComponent(no);
  link.setAttribute('aria-label', `在 WMS 查看履约单 ${no}（新标签页）`);
  link.title = '在 WMS 查看（新标签页）';
  $('#dOrderNo').textContent = no;
}

function showOrderSummary(no, trigger) {
  const alert = trackMode !== 'tracking' ? alertRows.find(row => row.order_no === no) : null;
  const order = trackMode === 'tracking' ? trackRows.find(row => row.履约单号 === no) : null;
  const channel = order?.当前渠道 || alert?.current_channel || order?.物流商 || alert?.carrier;
  const country = order?.目的国中文 || alert?.country;
  const shipped = order?.签出时间 || alert?.shipped_at || alert?.shipped_date;
  const platform = order && (order.平台名称 || (order.平台 && platformName(order.平台)));
  const officialStatus = order ? orderTrackStatus(order) :
    trackingGroups().find(row => row.v === alert?.track_status)?.t || alert?.track_status;
  setDrawerOrderLink(no);
  $('#dSub').textContent = [platform, order?.仓库 || alert?.warehouse, channel, country].filter(Boolean).join(' · ');
  const fields = [
    ['运单号', order?.快递单号 || alert?.tracking_no], ['当前物流渠道', channel], ['收货国家', country],
    ['出库（北京时间）', shipped && dt(shipped)], ['17TRACK 主状态', officialStatus],
    ['C 端承运商', order?.C端承运商],
    ...(!ME.preview && order?.订单号 ? [['订单号', order.订单号]] : []),
  ].filter(([, value]) => value);
  const recent = order?.最新描述 ? `<section class="timeline-panel carrier-timeline" aria-label="最新物流轨迹">
    <h4>${icon('route')}最新物流轨迹 · 当地时间</h4>
    <div class="t">${esc(trackTime(order.最新事件当地时间, order.最新事件时间))}</div>
    ${carrierEventText(order.轨迹状态, order.轨迹子状态, order.最新描述)}
  </section>` : '';
  const body = $('#dBody');
  body.innerHTML = `${fields.length ? orderFields(fields) : ''}${recent}
    <div class="note" id="drawerProgress" role="status">正在加载完整轨迹和履约明细…</div>`;
  body.scrollTop = 0;
  drawerOrder = no;
  drawerTrigger = trigger;
  $('#mask').classList.add('on');
  $('#drawer').classList.add('on');
  $('#drawer').inert = false;
  $('.app').inert = true;
  $('#dClose').focus({preventScroll: true});
}

async function openOrder(no, trigger) {
  clearTimeout(drawerIntentTimer);
  showOrderSummary(no, trigger);
  await loadOrderDetail(no, orderDetailsPath(no));
}

async function refreshOrder(no) {
  const path = orderDetailsPath(no);
  pageResponses.delete(path);
  if (drawerOrder === no) await loadOrderDetail(no, path, true);
}

async function loadOrderDetail(no, path, refresh = false) {
  const body = $('#dBody');
  let d;
  try {
    d = await latest('drawer', path, {signal: AbortSignal.timeout(12_000), priority: 'high'});
    if (!d) return;
    if (!d.order) throw new Error('详情数据不完整');
  } catch (error) {
    pageResponses.delete(path);
    if (refresh) throw error;
    const progress = $('#drawerProgress');
    progress.textContent = error.name === 'TimeoutError' ? '完整详情加载超时。' : '完整详情加载失败。' + error.message;
    const retry = el('button', 'btn ghost sm', '重试加载详情');
    retry.type = 'button';
    retry.onclick = () => {
      progress.textContent = '正在加载完整轨迹和履约明细…';
      $('#dClose').focus({preventScroll: true});
      run(() => loadOrderDetail(no, path));
    };
    progress.append(' ', retry);
    return;
  }
  const oldScroll = body.scrollTop;
  const o = d.order;
  setDrawerOrderLink(o.履约单号);
  $('#dSub').textContent = [o.平台名称 || platformName(o.平台), o.仓库, o.当前渠道 || o.物流商, o.目的国中文 || o.目的国].filter(Boolean).join(' · ');

  const kv = [
    ...(!ME.preview ? [['订单号', o.订单号], ['平台订单号', o.平台订单号]] : []),
    ['运单号', o.快递单号],
    ...(!ME.preview ? [
      ['履约单创建人', o.履约单创建人], ['履约单创建原因', o.履约单创建原因],
      ['签出重', o['签出重/g'] == null ? '—' : `${o['签出重/g'].toLocaleString('zh-CN')} g`],
    ] : []),
    ['收货国家', o.目的国中文 || o.目的国],
    ...(!ME.preview ? [['州 / 省', o.目的州省], ['城市', o.目的城市], ['邮编', o.目的邮编]] : []),
    ['当前库内节点', o.签出时间 ? '已出库' : o.状态],
    ['ERP 出库单状态', o.状态],
    ...(!ME.preview ? [[`ERP / WMS 妥投（${o.WMS时间口径 || '原始值'}）`, dt(o.WMS妥投时间 || o.签收时间)]] : []),
    ...(!ME.preview && o.WMS签收异常 ? [['WMS 签收校验', '时间异常 · 不计入时效']] : []),
    ['17TRACK 妥投', o.签收当地时间 || o.承运商签收时间 ?
      trackTime(o.签收当地时间, o.承运商签收时间) : '—'],
    ['C 端承运商', o.C端承运商],
    ['17TRACK 主状态', orderTrackStatus(o)],
    ['17TRACK 子状态', o.轨迹子状态],
    ...(d.verification ? [
      ['最近核对（北京时间）', dt(d.verification.lastAttempt)],
      ['最近成功核对（北京时间）', dt(d.verification.lastVerified)],
      ['核对结果', verificationStatus[d.verification.outcome] || d.verification.outcome],
    ] : [['最近查询', dt(o.查询时间)]]),
  ];

  const isPending = a => a.is_current && ['open', 'claimed', 'handling'].includes(a.state);
  const orderAlerts = d.alerts || [];
  const drawerActions = allowedActions();
  const alertButtons = (a, pending) => {
    const buttons = pending ? drawerActions : drawerActions.filter(([key]) =>
      a.state === (key === 'archive' ? 'archived' : 'resolved'))
      .map(([key]) => ['undo_' + key, key === 'archive' ? '撤销归档' : '撤销标记解决']);
    return buttons.length ? `<div class="actrow">${buttons.map(([key, label]) =>
      `<button type="button" class="btn ghost sm" data-alert-row="${orderAlerts.indexOf(a)}" data-act="${key}">${label}</button>`
    ).join('')}</div>` : '';
  };
  const alertCard = (a, pending) => `
    <div class="note">
      <div class="alert-detail">
        ${a.level ? `<span class="badge ${levelTone(a.level)}">${esc(a.level)}</span>` : ''}
        ${statusPill(a.rule_cn || a.rule, alertStyle(a.rule))}
        <small>${a.is_main == null ? '' : a.is_main ? '主预警' : '次预警'}${a.days == null ? '' : ` · ${measure(a.days, ' 天')}`}</small>
        ${pending ? '' : stateBadge(a.state)}
      </div>
      ${a.evidence ? `<p>${esc(a.evidence)}</p>` : ''}
      ${a.handled_by ? `<p>处理人 ${esc(a.handled_by)} · ${esc(syncTime(a.handle_at))}</p>` : ''}
      ${!pending && a.first_seen_at ? `<p>首次留痕 · ${esc(dt(a.first_seen_at))}</p>` : ''}
      ${a.cleared_at ? `<p>不再命中 · ${esc(syncTime(a.cleared_at))}</p>` : ''}
      ${a.handle_note ? `<p>${esc(a.handle_note)}</p>` : ''}
      ${alertButtons(a, pending)}
    </div>`;
  const activeAlerts = orderAlerts.filter(isPending).map(a => alertCard(a, true)).join('');
  const pastAlerts = orderAlerts.filter(a => !isPending(a)).map(a => alertCard(a, false)).join('');

  const milestoneLabels = { InfoReceived: '收到运单信息', Departure: '起运', Arrival: '到达',
    Returning: '退运中', Returned: '已退回' };
  const events = d.events || [];
  const ms = (d.milestones || []).map(m =>
    `<div class="e"><div class="t">${esc(trackTime(m.当地时间, m.时间))}</div>${carrierEventText(m.节点, '', milestoneLabels[m.节点])}</div>`).join('');
  const timeline = events.length ? events.map(event => `
    <div class="e">
      <div class="t">${esc(trackTime(event.当地时间, event.事件时间))}
        ${event.事件承运商 ? ` · ${esc(event.事件承运商)}` : ''}</div>
      ${carrierEventText(event.主状态, event.子状态, event.描述 || milestoneLabels[event.主状态])}
      ${event.地点 ? `<div class="cell-sub">${esc(event.地点)}</div>` : ''}
    </div>`).join('') : ms;
  const timelineLabel = events.length ? `${d.eventSource === 'archive' ? '17TRACK 历史回补' : '承运商逐条事件'} · ${fmt(events.length)} 条 · 当地时间` :
    ms ? '已入库关键节点 · 当地时间' : '暂无已入库的 17TRACK 事件';
  const fulfillmentTimeline = [
    ['客户下单（支付）', o.支付时间], ['创建履约单', o.创建时间],
    ['库内分配', o.分配时间], ['打包', o.打包时间], ['出库（签出）', o.签出时间],
  ].filter(([, time]) => time).sort((a, b) => String(a[1]).localeCompare(String(b[1])))
    .map(([label, time]) => `<li><b>${label}</b><time datetime="${esc(time)}">${dt(time)}</time></li>`).join('');

  const skus = (d.skus || []).map(s =>
    `<tr><td class="mono">${esc(s.sku)}</td><td>${esc(s.商品名称) || '—'}</td><td>${fmt(s.数量)}</td><td>${esc(s.类目) || '—'}</td></tr>`).join('');

  const notes = (d.notes || []).map(n =>
    `<div class="note"><div class="tip">${dt(n.ts)} · ${esc(n.actor)}</div>${esc(n.body)}</div>`).join('')
    || '<div class="tip">暂无备注</div>';

  $('#dBody').innerHTML = `
    ${orderFields(kv)}
    ${ME.preview ? '' : `<div class="sect">待处理预警</div>${activeAlerts || '<p class="note">当前没有待处理预警</p>'}
      ${pastAlerts ? `<div class="sect">历史预警</div>${pastAlerts}` : ''}`}
    <div class="sect drawer-section">
      <span>物流时间线</span>
      ${o.单号归一 ? `<a href="https://t.17track.net/zh-cn#nums=${encodeURIComponent(o.单号归一)}" target="_blank" rel="noopener noreferrer">在 17TRACK 查看 ↗</a>` : ''}
    </div>
    ${fulfillmentTimeline ? `<section class="timeline-panel fulfillment-timeline" aria-label="履约节点">
      <h4>${icon('warehouse')}履约节点 · 北京时间</h4><ol>${fulfillmentTimeline}</ol>
    </section>` : ''}
    <section class="timeline-panel carrier-timeline" aria-label="承运商轨迹">
      <h4>${icon('truck')}${timelineLabel}</h4>
      ${!events.length && o.最新描述 ? `<div class="note"><b>最新轨迹</b> · ${esc(trackTime(o.最新事件当地时间, o.最新事件时间))}
        ${carrierEventText(o.轨迹状态, o.轨迹子状态, o.最新描述)}${o.地点 ? `<div class="cell-sub">${esc(o.地点)}</div>` : ''}</div>` : ''}
      ${timeline ? `<div class="tl">${timeline}</div>` : ''}
    </section>
    ${ME.preview ? '' : `<div class="sect">SKU 明细</div>
    <div class="tblwrap drawer-table"><table class="tbl"><thead><tr><th>SKU</th><th>商品名称</th><th>数量</th><th>类目</th></tr></thead>
    <tbody>${skus || blankRow(4, '暂无 SKU 明细')}</tbody></table></div>
    <div class="sect">备注</div>${notes}`}
    ${ME.can_edit ? `<form class="note-form" id="noteForm">
      <input id="noteIn" aria-label="履约备注" placeholder="写一条备注…" maxlength="2000" required>
      <button class="btn sm" type="submit" id="noteSave">保存备注</button>
    </form>` : ''}`;

  const form = $('#noteForm');
  if (form) form.onsubmit = event => {
    event.preventDefault();
    run(async () => {
      const v = $('#noteIn').value.trim();
      if (!v) return toast('备注不能为空');
      const save = $('#noteSave');
      save.disabled = true;
      try {
        await jsonPost(`/api/orders/${encodeURIComponent(no)}/notes`, { body: v });
        toast('已保存备注');
        await refreshOrder(no);
      } finally { save.disabled = false; }
    });
  };
  $('#dBody').querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = () => run(async () => {
      b.disabled = true;
      try {
        await changeAlerts(b.dataset.act, [alertTarget(orderAlerts[Number(b.dataset.alertRow)])]);
      } finally { b.disabled = false; }
    });
  });
  body.scrollTop = oldScroll;
}

function closeDrawer() {
  drawerOrder = null;
  pendingRequests.delete('drawer');
  $('#mask').classList.remove('on');
  $('#drawer').classList.remove('on');
  $('#drawer').inert = true;
  $('.app').inert = false;
  if (drawerTrigger?.isConnected) drawerTrigger.focus({preventScroll: true});
}

/* ================= 启动 ================= */
run(async function boot() {
  const v = location.hash.slice(1);
  if (['alerts','archive','special-tracking','channel-exceptions'].includes(v))
    setTrackMode(v === 'channel-exceptions' ? 'special-tracking' : v);
  if (v === 'regions-other') setRegionMarket('other');
  displayView(routeView(v));
  if (['channels-lastmile','channels-group'].includes(v)) {
    channelTabOverride = v.slice('channels-'.length);
    setChannelDimension(channelTabOverride);
  }
  ME = await api('/api/me');
  if (ME.dataRevision) {
    dataRevision = ME.dataRevision;
    revisionCheckedAt = performance.now();
  }
  if (['alerts','archive','special-tracking','channel-exceptions'].includes(v))
    setTrackMode(v === 'channel-exceptions' ? 'special-tracking' : v);
  $('#opsAssistant').hidden = !ME.opsAssistant;
  $('#openChannelGroups').hidden = !ME.can_edit;
  $('#channelGroupAction').hidden = !ME.can_edit;
  $('#cloudSharing').hidden = !ME.cloudSharing;
  $('#logout').hidden = ME.preview || ME.openTest;
  if (!ME.preview) $('#currentChannelTip').textContent += ' · 点击渠道查看履约单';
  if (ME.preview) {
    for (const selector of ['[data-v="rules"]', '[data-v="us"]', '#openPending',
      '#trackTabs', '#showDetailed', '#exportTrack', '#trackAlertHeading']) {
      $(selector).hidden = true;
    }
    $('#trackIntro').textContent = '输入完整单号查看物流状态和时间线';
  }
  $('#who').textContent = ME.preview || ME.openTest ? ME.display_name : `${ME.display_name} · ${ME.role_label}`;
  $('#batchAction').append(...allowedActions().map(([key, label]) => new Option(label, key)));
  $('#batchAction').hidden = !ME.can_edit;
  $('#applyBatch').hidden = !ME.can_edit;
  document.querySelectorAll('#nav button').forEach(b => b.onclick = () => showView(b.dataset.v));
  setupUsControls();
  new IntersectionObserver(([entry]) => {
    regionSegmentsVisible = entry.isIntersecting;
    if (regionSegmentsVisible) loadVisibleRegionSegments();
  }, {rootMargin:'120px 0px'}).observe($('#regionSegmentsPanel'));
  setupTrackExceptionControls();
  new IntersectionObserver(([entry]) => {
    channelTrendVisible = entry.isIntersecting;
    if (channelTrendVisible) run(loadChannelTrend);
  }, {rootMargin:'100px 0px'}).observe($('#channelTrend'));
  $('#channelTrendKey').onchange = () => run(loadChannelTrend);
  channelMemberPicker = setupChannelMemberPicker();
  $('#channelMemberDimension').onclick = event => {
    const button = event.target.closest('[data-member-dimension]');
    if (!button || channelMemberDimension === button.dataset.memberDimension) return;
    channelMemberDimension = button.dataset.memberDimension;
    $('#channelMemberDimension').querySelectorAll('button').forEach(item =>
      item.setAttribute('aria-pressed', String(item === button)));
    channelMemberPicker.close();
    if (channelMemberDimension === 'current') channelMemberSelection.channel = '';
    renderChannelMembers();
  };
  $('#channelTrendPeriod').onclick = event => {
    const selected = event.target.closest('[data-period]');
    if (!selected || selected.dataset.period === channelTrendPeriod) return;
    channelTrendPeriod = selected.dataset.period;
    $('#channelTrendPeriod').querySelectorAll('button').forEach(button =>
      button.setAttribute('aria-pressed', String(button === selected)));
    renderChannelTrend();
  };
  const periods = [...document.querySelectorAll('#dailyPeriod button')];
  periods.forEach(button => button.onclick = () => {
    if (trend.span === Number(button.dataset.days)) return;
    trend.span = Number(button.dataset.days);
    periods.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    drawDailyVolume();
  });
  const dailyBases = [...document.querySelectorAll('#dailyBasis button')];
  dailyBases.forEach(button => button.onclick = () => {
    const basis = button.dataset.dateBasis;
    if (trend.basis === basis) return;
    trend.basis = basis;
    dailyBases.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    drawDailyVolume();
  });
  document.querySelectorAll('#trackTabs [data-mode]').forEach(btn => btn.onclick = () => {
    setTrackMode(btn.dataset.mode);
    showView('track');
  });
  document.querySelectorAll('#ruleTabs [data-rule-pane]').forEach(btn => btn.onclick = () => setRulePane(btn.dataset.rulePane));
  $('#startFullPull').onclick = () => run(startFullPull);
  setInterval(() => {
    if (VIEW === 'rules' && !$('#rules-sync').hidden && !document.hidden) refreshSyncStatus();
  }, 30_000);
  setInterval(() => { if (!ME.preview && !document.hidden) refreshDataRevision(); }, 15_000);
  if (!ME.preview && !dataRevision) refreshDataRevision();
  $('#openPending').onclick = () => run(async () => {
    await loadMeta();
    setTrackMode('alerts');
    prepareAlerts();
    showView('track');
  });
  $('#trackChips').onclick = event => {
    const target = event.target.closest('[data-track-status]');
    if (target && TF) filterByTrackStatus(target.dataset.trackStatus, readF(TF));
  };
  $('#alertChips').onclick = event => {
    const target = event.target.closest('[data-alert-rule]');
    if (target) {
      AF.rule.value = AF.rule.value === target.dataset.alertRule ? '' : target.dataset.alertRule;
      syncFilterPreset(AF);
      run(loadAlerts);
    }
  };
  const orderClick = event => {
    const button = event.target.closest('[data-order]');
    if (button) openOrder(button.dataset.order, button).catch(error => toast(error.message));
  };
  for (const list of [$('#trackBody'), $('#alertBody'), $('#trackExceptionBody')]) {
    list.onclick = orderClick;
    list.addEventListener('pointerover', watchOrderIntent);
    list.addEventListener('focusin', watchOrderIntent);
  }
  $('#showDetailed').onclick = () => openTracking({ coverage: 'detailed', sort: '逐条事件数' });
  $('#openTrackAnalysis').hidden = ME.preview;
  $('#openTrackAnalysis').onclick = () => run(async () => {
    await loadTrackFilters();
    if (TF && VIEW === 'track' && trackMode === 'tracking') openTrackAnalysis();
  });
  $('#refreshTrackAnalysis').onclick = () => run(analyzeTrack);
  $('#interpretTrackAnalysis').onclick = () => run(() => analyzeTrack({interpret: true}));
  $('#trackAnalysisDialog').addEventListener('close', () => trackAnalysisRequest?.abort());
  $('#trackAnalysisResult').onclick = event => {
    const button = event.target.closest('[data-track-analysis-order]');
    if (!button) return;
    $('#trackAnalysisDialog').close();
    run(() => openOrder(button.dataset.trackAnalysisOrder, $('#openTrackAnalysis')));
  };
  $('#alertBody').onchange = refreshSelection;
  $('#batchAction').onchange = refreshSelection;
  $('#applyBatch').onclick = () => run(applyBatch);
  $('#selectPage').onchange = event => {
    $('#alertBody').querySelectorAll('[data-alert-index]').forEach(box => box.checked = event.target.checked);
    refreshSelection();
  };
  $('#channelResults').onclick = event => {
    const button = event.target.closest('[data-channel-track]');
    if (button) {
      const dimension = renderedChannels?.dimension === 'group' ? 'channel_group' : 'lastmile';
      openTracking({...renderedChannelFilters, [dimension]:button.dataset.channelTrack});
    }
  };
  $('#channelDimension').onclick = event => {
    const button = event.target.closest('button[data-dimension]');
    if (!button || channelDimension === button.dataset.dimension) return;
    if (!CF) channelTabOverride = button.dataset.dimension;
    setChannelDimension(button.dataset.dimension);
    if (CF) syncFilterPreset(CF);
    showView('channels');
  };
  $('#channelCompareToggle').onclick = () => {
    if (renderedChannels?.dimension !== 'group') return;
    channelComparisonExpanded = !channelComparisonExpanded;
    renderChannelComparison(renderedChannels,channelRows);
  };
  $('#channelDayMode').onclick = event => {
    const button = event.target.closest('button[data-day-mode]');
    if (!button || channelDayMode === button.dataset.dayMode) return;
    channelDayMode = button.dataset.dayMode;
    syncChannelTimingControls();
    syncFilterPreset(CF);
    if (renderedChannels) renderChannelRates(renderedChannels, channelRows);
    renderChannelTrend();
    renderChannelMembers();
  };
  $('#channelBasis').onclick = event => {
    const button = event.target.closest('button[data-basis]');
    if (!CF || !button || channelBasis === button.dataset.basis) return;
    channelBasis = button.dataset.basis;
    syncChannelTimingControls();
    syncFilterPreset(CF);
    scheduleReport('channels', loadChannels);
  };
  $('#ruleList').onclick = event => {
    const config = event.target.closest('[data-config-rule]');
    if (config) return run(() => openConfiguration('rule', config.dataset.configRule));
    const button = event.target.closest('[data-rule-track]');
    if (button) run(async () => {
      await loadMeta();
      setTrackMode('alerts');
      prepareAlerts();
      AF.rule.value = button.dataset.ruleTrack;
      syncFilterPreset(AF);
      showView('track');
    });
  };
  $('#slaSearch').oninput = () => { syncFilterPreset(slaFilters); renderSLATable(); };
  $('#slaBody').onclick = event => {
    const config = event.target.closest('[data-config-sla]');
    if (config) run(() => openConfiguration('sla', config.dataset.configSla));
  };
  $('#settingsForm').onsubmit = saveConfiguration;
  $('#openChannelGroups').onclick = event => {
    const button = event.currentTarget;
    run(() => openChannelGroups(button));
  };
  $('#exportChannelGroups').onclick = event => run(async () => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const config = await api(channelGroupPath);
      const exportedAt = new Date();
      const payload = {...config,exportedAt:exportedAt.toISOString()};
      const name = exportedAt.toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'medium'}).replace(/[: ]/g,'-');
      const href = URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)+'\n'],{type:'application/json;charset=utf-8'}));
      const link = document.createElement('a');
      link.href = href;
      link.download = `自定义渠道_${name}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(href),1000);
    } finally { button.disabled = false; }
  });
  $('#channelGroupRulesBody').onclick = event => {
    const button = event.target.closest('[data-edit-group]');
    if (button) run(() => openChannelGroups(button, button.dataset.editGroup));
  };
  $('#channelGroupsClose').onclick = () => $('#channelGroupsDialog').close();
  $('#channelGroupChoice').onchange = editChannelGroup;
  $('#channelGroupSearch').oninput = renderChannelGroupOptions;
  $('#channelGroupsForm').onsubmit = event => { event.preventDefault(); run(() => saveChannelGroup(false)); };
  $('#channelGroupRemove').onclick = () => run(() => saveChannelGroup(true));
  $('#channelGroupsDialog').oncancel = event => { if ($('#channelGroupSave').disabled) event.preventDefault(); };
  $('#settingsCancel').onclick = $('#settingsClose').onclick = () => $('#settingsDialog').close();
  $('#settingsDialog').addEventListener('cancel', event => {
    if ($('#settingsSave').disabled) event.preventDefault();
  });
  $('#refreshRules').onclick = () => run(async () => {
    await reloadRules();
    toast('已刷新数据与规则');
  });
  $('#exportTrack').onclick = () => exportCsv('履约轨迹-本页', [
    '履约单号', '快递单号', '物流商', '当前渠道', 'C端承运商', '仓库', '目的国中文', '轨迹状态中文',
    '轨迹子状态', '运输商代码', '主预警名称', '逐条事件数',
  ].map(key => [key, row => row[key]]).concat([
    ['平台名称', row => platformName(row.平台)], ['客户下单（北京时间）', row => row.支付时间],
    ['履约单创建（北京时间）', row => row.创建时间], ['出库（北京时间）', row => row.签出时间],
    ['运输时间', row => durationText(row.运输分钟)],
    ['历史预警', row => row.历史预警?.rule_cn], ['处理人', row => row.历史预警?.handled_by],
    ['处理时间（北京时间）', row => row.历史预警?.handle_at ? syncTime(row.历史预警.handle_at) : ''],
    ['ERP / WMS 妥投（原始值）', row => row.签收时间], ['WMS 时间口径', row => row.WMS时间口径],
    ['妥投统计来源', row => row.妥投统计来源],
    ['17TRACK 妥投（当地时间或标注 UTC）', row => row.签收当地时间 || row.承运商签收时间 ?
      trackTime(row.签收当地时间, row.承运商签收时间) : ''],
    ['最新轨迹（当地时间或标注 UTC）', row => row.最新事件当地时间 || row.最新事件时间 ?
      trackTime(row.最新事件当地时间, row.最新事件时间) : ''],
  ]), trackRows);
  $('#exportAllAlerts').onclick = () => run(() => exportAlertDetails('all', $('#exportAllAlerts')));
  $('#exportAlertsWithTracks').onclick = () => run(() => exportAlertDetails('tracks', $('#exportAlertsWithTracks')));
  $('#exportChannels').onclick = () => {
    if (!renderedChannels) return;
    const basis = TIMING_BASIS[renderedChannels.basis].label;
    const dimension = CHANNEL_DIMENSIONS[renderedChannels.dimension || 'lastmile'];
    const dayLabel = CHANNEL_DAY_LABELS[channelDayMode];
    exportCsv(`渠道时效-${dimension}-${basis}-${dayLabel}`, [
    ['时效口径', () => basis],
    ['妥投率计时', () => dayLabel],
    [dimension, row => row.channel], ['单量', row => row.orders], ['有妥投时间', row => row.signed],
    ['有效签收样本', row => row.samples], ['WMS 补算样本', row => row.fallbackSamples],
    ['平均时效（天）', row => row.avgDays], ['P90（天）', row => row.p90Days],
    ...DELIVERY_WINDOWS.map(days => [`${days}天内妥投率（${dayLabel}，%，按总单量）`, row => channelDeliveryRate(row, days)]),
    ['SLA 在途超时', row => row.overdue], ['SLA 有效签收样本', row => row.sla_samples], ['SLA达成率（%）', row => row.slaRate],
    ], channelRows);
  };
  $('#dClose').onclick = closeDrawer;
  $('#mask').onclick = closeDrawer;
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if ($('#drawer').classList.contains('on')) closeDrawer();
    for (const filter of document.querySelectorAll('.multi-filter[open]')) {
      filter.open = false;
      filter.querySelector('summary').focus();
    }
  });
  document.addEventListener('click', event => {
    for (const filter of document.querySelectorAll('.multi-filter[open]')) {
      if (!filter.contains(event.target)) filter.open = false;
    }
  });
  $('#logout').onclick = () => run(async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; });
  if (v === 'detailed') openTracking({ coverage: 'detailed', sort: '逐条事件数' });
  else showView(routeView(v));
  window.addEventListener('resize', resizeVisibleCharts);
});
