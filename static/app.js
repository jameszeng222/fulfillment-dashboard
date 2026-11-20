/* 履约监控系统 · 前端（四页：总览 / 轨迹监控 / 渠道时效 / 数据与规则） */
const $ = s => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt = n => (n == null || isNaN(n)) ? '—' : Math.round(n).toLocaleString('zh-CN');
const fmt1 = n => (n == null || isNaN(n)) ? '—' : (Math.round(n * 10) / 10).toFixed(1);
const dt = s => s ? String(s).replace('T', ' ').slice(0, 16) : '—';
const trackTime = (local, utc) => local ||
  (utc && utc !== 'NaT' ? `${String(utc).replace('T', ' ').slice(0, 19)} UTC` : '时间未提供');
const measure = (value, unit = '') => value == null ? '—' : fmt1(value) + unit;
const platformName = code => META.platformNames[code] || code || '—';
const levelTone = level => level === '紧急' ? 'e' : level === '高' ? 'h' : 'm';
const states = { open: ['gray', '待处理'], claimed: ['blue', '已认领'], handling: ['purple', '处理中'],
  resolved: ['ok', '已解决'], archived: ['m', '已归档'] };
const actions = { claim: '认领', handling: '开始处理', resolve: '标记解决', archive: '归档', reopen: '重开' };
const allowedActions = () => Object.entries(actions).filter(([key]) =>
  ME.can_edit && (!['archive', 'reopen'].includes(key) || ['admin', 'logistics'].includes(ME.role)));
const stateBadge = state => {
  const [tone, label] = states[state] || ['gray', state];
  return `<span class="badge ${tone}">${esc(label)}</span>`;
};
const jsonPost = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

let ME = null, META = null, RULES = null;
let lastImportAt = null;
const charts = {};
const pendingRequests = new Map();
const pageResponses = new Map();
const cachedPages = new Set(['activity', 'dash', 'health', 'track', 'channels', 'rules']);
let readGeneration = 0;
let resizeFrame = 0;
let toastTimer;
const chartTheme = {
  ink: '#081f5c', paper: '#ffffff', text: '#243657', muted: '#536782',
  grid: '#e1e7f0',
  series: ['#adc4e0', '#7096d1', '#334eac', '#081f5c'],
};
const chartMotion = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const chartTip = { backgroundColor: chartTheme.ink, borderWidth: 0, confine: true,
  padding: [9, 12], textStyle: { color: chartTheme.paper, fontSize: 12 } };
const compactCount = value => value >= 10000 ? `${Number((value / 10000).toFixed(1))} 万` : fmt(value);

function setTopMeta(createdThrough) {
  const parts = [`建单截止 ${createdThrough}`];
  if (lastImportAt) parts.push(`最近导入 ${dt(lastImportAt)}`);
  $('#topMeta').textContent = parts.join(' · ');
}

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 2200);
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
    d.detail || ('HTTP ' + r.status));
  return d;
}

function readPage(path) {
  const current = pageResponses.get(path);
  if (current && (!current.until || current.until > performance.now())) return current.result;
  const entry = { until: 0 };
  entry.result = api(path).then(data => {
    entry.until = performance.now() + 25_000;
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

function invalidatePages() {
  pageResponses.clear();
  readGeneration++;
}

async function latest(key, path, options) {
  const id = Symbol();
  const generation = readGeneration;
  pendingRequests.set(key, id);
  const cached = cachedPages.has(key);
  const stillCurrent = () => pendingRequests.get(key) === id && (!cached || generation === readGeneration);
  try {
    const result = await (cached ? readPage(path) : api(path, options));
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
  const url = URL.createObjectURL(new Blob(['\ufeff', content], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name + '.csv';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const VIEW_META = { dash: '数据总览', track: '轨迹监控', channels: '渠道时效', rules: '数据与规则' };
let VIEW = 'dash';
let trackMode = 'tracking';

function showView(v) {
  if (ME.preview && v === 'rules') v = 'dash';
  VIEW = v;
  document.querySelectorAll('.view').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  document.querySelectorAll('#nav button').forEach(b => {
    b.classList.toggle('on', b.dataset.v === v);
    if (b.dataset.v === v) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  $('#crumb').textContent = VIEW_META[v];
  history.replaceState(null, '', '#' + (v === 'track' && trackMode === 'alerts' ? 'alerts' : v));
  window.scrollTo({ top: 0 });
  resizeVisibleCharts();
  run({ dash: loadDash, track: trackMode === 'alerts' ? loadAlerts : loadTrack,
    channels: loadChannels, rules: loadRules }[v]);
}

function initChart(id) {
  if (!charts[id]) charts[id] = echarts.init(document.getElementById(id), null, { renderer: 'canvas' });
  return charts[id];
}

function resizeVisibleCharts() {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    for (const chart of Object.values(charts)) {
      const host = chart.getDom();
      if (host.clientWidth && host.clientHeight &&
          (host.clientWidth !== chart.getWidth() || host.clientHeight !== chart.getHeight())) chart.resize();
    }
  });
}

/* ---------------- 筛选条 ---------------- */
function multiFilter(def, onChange) {
  const root = el('details', 'multi-filter');
  const trigger = el('summary', 'multi-trigger');
  const name = el('span');
  name.textContent = def.label;
  const badge = el('strong');
  trigger.append(name, badge, el('span', 'multi-caret'));
  const panel = el('div', 'multi-popover');
  const search = el('input');
  search.type = 'search';
  search.placeholder = `搜索${def.label}…`;
  search.setAttribute('aria-label', search.placeholder);
  const all = el('input');
  all.type = 'checkbox';
  const allLabel = el('label', 'multi-row multi-all', '<span>全部</span>');
  allLabel.prepend(all);
  allLabel.append(el('small', '', fmt(META.range.orders)));
  const list = el('div', 'multi-options');
  panel.append(search, allLabel, list);
  root.append(trigger, panel);
  const selected = new Set();

  function update() {
    badge.textContent = selected.size ? `已选 ${selected.size}` : '全部';
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
    document.querySelectorAll('.multi-filter[open]').forEach(other => {
      if (other !== root) other.open = false;
    });
    panel.style.left = '0';
    const overflow = panel.getBoundingClientRect().right - window.innerWidth + 12;
    if (overflow > 0) panel.style.left = `-${overflow}px`;
    search.focus();
  });
  update();
  renderOptions();
  return {
    root,
    get value() { return [...selected]; },
    set value(value) {
      selected.clear();
      for (const code of Array.isArray(value) ? value : [value]) {
        if (def.options.some(option => option.v === code)) selected.add(code);
      }
      update();
      renderOptions();
    },
  };
}

function makeFilters(host, defs, onChange) {
  host.innerHTML = '';
  const refs = {};
  defs.forEach(d => {
    if (d.type === 'text') {
      const i = el('input'); i.placeholder = d.label; i.dataset.key = d.key; i.style.width = (d.w || 210) + 'px';
      i.setAttribute('aria-label', d.label);
      let timer;
      const submit = () => { clearTimeout(timer); onChange(1, 'search'); };
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
        group.append(i, button);
        host.appendChild(group);
      } else {
        host.appendChild(i);
      }
      refs[d.key] = i;
    } else if (d.type === 'date') {
      const i = el('input'); i.type = 'date'; i.dataset.key = d.key; i.title = d.label;
      i.setAttribute('aria-label', d.label);
      const label = el('label', 'date-filter');
      label.append(el('span', '', d.label), i);
      i.onchange = () => onChange(1);
      host.appendChild(label); refs[d.key] = i;
    } else if (d.type === 'multi') {
      const control = multiFilter(d, onChange);
      host.appendChild(control.root);
      refs[d.key] = control;
    } else {
      const s = el('select'); s.dataset.key = d.key;
      s.setAttribute('aria-label', d.label);
      s.innerHTML = `<option value="">${d.label}：全部</option>` +
        (d.options || []).filter(o => o != null && o !== '').map(o => {
          const v = (o && typeof o === 'object') ? o.v : o;
          const t = (o && typeof o === 'object') ? (o.t != null ? o.t : o.v) : o;
          return `<option value="${esc(v)}">${esc(t)}</option>`;
        }).join('');
      s.onchange = () => onChange(1);
      host.appendChild(s); refs[d.key] = s;
    }
  });
  const btn = el('button', 'btn ghost sm', '重置');
  btn.onclick = () => {
    Object.values(refs).forEach(control => { if ('value' in control) control.value = ''; });
    onChange(1);
  };
  host.appendChild(btn);
  const sp = el('span', 'grow'); sp.style.flex = '1';
  host.appendChild(sp);
  const info = el('span', '', ''); info.style.cssText = 'font-size:11.5px;color:#8b98ab';
  host.appendChild(info);
  refs._info = info;
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

function pager(host, total, page, size, go) {
  const pages = Math.max(1, Math.ceil(total / size));
  host.innerHTML = `<span>第 ${page} / ${pages} 页 · 共 ${fmt(total)} 条</span><span class="sp"></span>`;
  const mk = (t, p, dis) => { const b = el('button', '', t); b.disabled = dis; b.onclick = () => go(p); return b; };
  host.appendChild(mk('‹ 上一页', page - 1, page <= 1));
  host.appendChild(mk('下一页 ›', page + 1, page >= pages));
}

/* ================= 数据总览 ================= */
let DF = null;
let renderedDash = null;
const trend = { days: [], filters: {}, span: 45 };

function dashFilterDefs() {
  return [
    { type: 'date', label: '开始日期', key: 'start' },
    { type: 'date', label: '结束日期', key: 'end' },
    ...Object.entries({ month: '月份', wh: '仓库', pf: '平台', cr: '后台物流商',
      channel: '当前渠道', lastmile: 'C 端渠道', cn: '国家' }).map(([key, label]) =>
      ({ type: 'multi', key, label, options: META.filterOptions[key] })),
  ];
}

async function loadDash() {
  if (!DF) DF = makeFilters($('#dashFilters'), dashFilterDefs(), () => run(loadDash));
  const f = readF(DF);
  const d = await latest('dash', '/api/overview?' + qs(f));
  if (!d || VIEW !== 'dash' || renderedDash === d) return;
  renderedDash = d;
  DF._info.hidden = true;
  setTopMeta(d.asof);

  const k = d.kpi;
  const defs = [
    ['履约单数', fmt(k.orders), '筛选范围'],
    ['总件数', fmt(k.items), `单均 ${k.orders ? fmt1(k.items / k.orders) : '—'} 件`],
    ['妥投覆盖率', measure(k.signRate, '%'), `含 WMS 补算 ${fmt(k.fallbackSigned)} 单`],
    ['仓内处理', measure(k.inboundHours, ' 小时'), '履约单创建 → 出库'],
    ['物流运输', measure(k.transitDays, ' 天'), `出库 → 妥投 · ${fmt(k.transitSamples)} 单`],
    ['6 天达成率', measure(k.rate6, '%'), '有效时效样本中 ≤6 天'],
  ];
  $('#dashKpi').innerHTML = defs.map(([l, v, s]) =>
    `<div class="kpi"><div class="lbl">${l}</div><div class="val">${v}</div><div class="fl">${s}</div></div>`).join('');

  const c = initChart('cMonthly');
  c.setOption({
    animation: chartMotion, animationDuration: 230, animationDurationUpdate: 140, animationEasing: 'quarticOut',
    grid: { left: 6, right: 10, top: 32, bottom: 5, containLabel: true },
    tooltip: { ...chartTip, trigger: 'axis', valueFormatter: fmt },
    legend: { right: 4, top: 0, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11, color: chartTheme.text } },
    xAxis: { type: 'category', data: d.monthly.map(r => r.m), axisTick: { show: false },
      axisLine: { lineStyle: { color: chartTheme.grid } }, axisLabel: { color: chartTheme.muted, hideOverlap: true } },
    yAxis: [{ type: 'value', splitLine: { lineStyle: { color: chartTheme.grid } },
      axisLabel: { color: chartTheme.muted, formatter: compactCount } },
    { type: 'value', min: 0, splitLine: { show: false },
      axisLabel: { color: chartTheme.muted, formatter: '{value} 天' } }],
    series: [
      { name: '单量', type: 'bar', barMaxWidth: 34, data: d.monthly.map(r => r.n),
        itemStyle: { color: chartTheme.series[0], borderRadius: [3, 3, 0, 0] },
        tooltip: { valueFormatter: value => `${fmt(value)} 单` } },
      { name: '平均物流天数', type: 'line', yAxisIndex: 1, symbolSize: 6, data: d.monthly.map(r => r.d),
        itemStyle: { color: chartTheme.ink }, lineStyle: { width: 1.5 },
        tooltip: { valueFormatter: value => measure(value, ' 天') } },
    ],
  });

  trend.days = d.daily;
  trend.filters = f;
  drawDailyVolume();

  drawRank('cCarrier', d.byCarrier, 10,
    ME.preview ? null : value => openTracking({ ...f, cr: value }));
  drawRank('cLastMile', d.byLastMile.map(r => ({ ...r, k: r.k || '未填写 C 端渠道', filter: r.k || '__empty__' })), 10,
    ME.preview ? null : value => openTracking({ ...f, lastmile: value }));
  drawRank('cTrackingStatus', d.byTrackingStatus, 10,
    ME.preview ? null : value => filterByTrackStatus(value, f));
  drawRank('cWarehouse', d.byWarehouse, 10,
    ME.preview ? null : value => openTracking({ ...f, wh: value }));
  drawRank('cCountry', d.byCountry, 15);
  drawRank('cCurrentChannel', d.byCurrentChannel || [], 10,
    ME.preview ? null : value => openTracking({ ...f, channel: value }));
  drawDispatchStatus(d.byStatus);
}

function drawDispatchStatus(rows) {
  drawRank('cStatus', rows, rows.length);
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

// Lieflat Charts F3 Hairline Area: one baseline stem per day and a thin contour.
function drawDailyVolume() {
  const clock = day => Date.parse(day + 'T00:00:00Z');
  const dayMs = 86_400_000;
  const last = trend.days.at(-1);
  const latest = last ? clock(last.day) : 0;
  const days = trend.span ? trend.days.filter(row => clock(row.day) >= latest - (trend.span - 1) * dayMs) : trend.days;
  $('#dailyNote').hidden = ME.preview || !days.length;
  $('#cDaily').hidden = !days.length;
  $('#dailyRange').textContent = days.length ?
    `${days[0].day} 至 ${days.at(-1).day} · 按履约单创建日` : '当前筛选无履约单';
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
  const contour = [];
  for (const point of values) {
    const before = contour.at(-1);
    if (before && point[0] - before[0] > dayMs) contour.push([before[0] + dayMs, null]);
    contour.push(point);
  }
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
      { type: 'bar', name: '日单量', data: values, barWidth: days.length > 90 ? 1.4 : 2.1,
        itemStyle: { color: chartTheme.series[0] }, emphasis: { itemStyle: { color: chartTheme.series[1] } } },
      { type: 'line', data: contour, showSymbol: false, lineStyle: { color: chartTheme.series[2], width: 1.9 },
        itemStyle: { color: chartTheme.series[2] },
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
    if (row) openTracking({ ...trend.filters, start: row.day, end: row.day });
  });
}

function drawRank(id, rows, limit, onClick) {
  const host = document.getElementById(id);
  const data = rows.slice(0, limit);
  if (!data.length) {
    host.innerHTML = '<p class="rank-empty tip">当前筛选无数据</p>';
    return;
  }
  const max = Math.max(1, ...data.map(row => row.n));
  host.innerHTML = `<ol class="rank-list">${data.map((row, index) => {
    const name = esc(row.k || '未填写');
    const content = `<span class="rank-name" title="${name}">${name}</span><span class="rank-track" aria-hidden="true">
      <span style="width:${100 * row.n / max}%"></span></span><strong class="rank-value">${fmt(row.n)}</strong>`;
    return onClick && row.k ?
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
let trackRows = [];
let renderedTrack = null;
let alertRows = [];
let alertPage = 1;
const pendingStates = 'open,claimed,handling';
const trackingGroups = () => [...new Map(META.trackStatuses.map(row => [row.status, row.label]))]
  .map(([v, t]) => ({ v, t }));
const ruleOptions = () => META.ruleOptions || META.rules.map(row => ({ v: row.rule, t: row.rule_cn }));
const blankRow = (cols, message) => `<tr><td colspan="${cols}" class="empty">${message}</td></tr>`;
const orderButton = (no, tracking) => `<button type="button" class="order-link mono" data-order="${esc(no)}">${esc(no)}</button>
  <small class="cell-sub mono">${esc(tracking || '—')}</small>`;
const trackBadge = (status, label, sub, missing, fallback = '其他履约单 · 无本地结果') => status
  ? `<span class="badge ${status === 'Delivered' ? 'ok' : status === 'Exception' ? 'h' : 'blue'}">${esc(label || status)}</span>
     ${sub ? `<small class="cell-sub mono">${esc(sub)}</small>` : ''}`
  : `<span class="badge gray">${missing ? 'API 账号登记未匹配' : esc(fallback)}</span>`;
const coverageOptions = [
  ['detailed', '有逐条承运商事件'],
  ['tracked', '有 17TRACK 查询记录'],
  ['unmatched', 'API 账号登记未匹配'],
  ['queried_empty', '已查询 · 未返回轨迹'],
  ['signed_unstored', '已签收 · 轨迹未入库'],
  ['shipped_unstored', '已出库 · 待同步轨迹'],
  ['other_unstored', '其他履约单 · 无本地结果'],
];

function setTrackMode(mode) {
  if (ME.preview) mode = 'tracking';
  trackMode = mode;
  $('#trackingPane').hidden = mode !== 'tracking';
  $('#alertsPane').hidden = mode !== 'alerts';
  document.querySelectorAll('#trackTabs button').forEach(btn =>
    btn.setAttribute('aria-pressed', String(btn.dataset.mode === mode)));
}

function updateSubstates() {
  const current = TF.sub.value;
  const entries = META.trackStatuses.filter(row => !TF.tstat.value || TF.tstat.value === row.status);
  TF.sub.replaceChildren(new Option('子状态：全部', ''),
    ...entries.map(row => new Option(` ${row.sub_label || row.sub} · ${row.sub}`, row.sub)));
  if (entries.some(row => row.sub === current)) TF.sub.value = current;
}

function prepareTrack() {
  if (TF) return;
  const lookup = { type: 'text', label: '履约单号 / 运单号 / 订单号 / 平台订单号',
    key: 'q', w: 275, submit: true, auto: !ME.preview };
  if (ME.preview) {
    TF = makeFilters($('#trackFilters'), [lookup], () => run(loadTrack));
    TF._info.setAttribute('role', 'status');
    return;
  }
  TF = makeFilters($('#trackFilters'), [
    lookup,
    ...dashFilterDefs(),
    { type: 'select', label: '17TRACK 状态', key: 'tstat', options: trackingGroups() },
    { type: 'select', label: '子状态', key: 'sub' },
    { type: 'select', label: '主预警', key: 'rule', options: ruleOptions() },
    { type: 'select', label: '轨迹覆盖', key: 'coverage',
      options: coverageOptions.map(([v, t]) => ({ v, t })) },
    { type: 'select', label: '排序', key: 'sort', options: ['创建时间', '签出时间', '停滞天数', '逐条事件数'] },
  ], (_, action) => { updateSubstates(); run(() => loadTrack(1, action === 'search')); });
  updateSubstates();
  TF._info.setAttribute('role', 'status');
}

function openTracking(values = {}) {
  prepareTrack();
  Object.entries(TF).forEach(([key, control]) => { if (key !== '_info') control.value = values[key] || ''; });
  if (!ME.preview) updateSubstates();
  setTrackMode('tracking');
  showView('track');
}

function filterByTrackStatus(label, scope = {}) {
  const state = trackingGroups().find(row => row.t === label);
  const coverage = coverageOptions.find(([, name]) => name === label)?.[0];
  if (state || coverage) openTracking({ ...scope, tstat: state?.v, coverage });
}

let lastTrackingQuery = '';

async function loadTrack(page = 1, fromSearch = false) {
  prepareTrack();
  const f = { sort: '签出时间', ...readF(TF) };
  const query = f.q?.trim();
  const {q, sort, ...scope} = f;
  TF._info.textContent = query ? '正在搜索…' : ME.preview ? '' : '正在读取履约单…';
  let d = await latest('track', '/api/track?' + qs(f, page, 40));
  if (!d || VIEW !== 'track' || trackMode !== 'tracking') return;
  let clearedFilters = false;
  if (fromSearch && query && !d.total && Object.keys(scope).length) {
    const broad = await latest('track', '/api/track?' + qs({q, sort}, 1, 40));
    if (!broad || VIEW !== 'track' || trackMode !== 'tracking') return;
    if (broad.total) {
      for (const key of Object.keys(scope)) TF[key].value = '';
      updateSubstates();
      clearedFilters = true;
      d = broad;
    }
  }
  trackRows = d.rows;
  lastTrackingQuery = query || '';
  TF._info.textContent = ME.preview && !query ? '' :
    `${clearedFilters ? '已切换至全部履约单搜索 · ' : ''}共 ${fmt(d.total)} 单`;
  if (renderedTrack === d) return;
  renderedTrack = d;
  $('#trackChips').innerHTML = ME.preview ? '' : (d.statusSummary || []).slice(0, 7).map(row =>
    `<button type="button" class="chipstat" data-track-status="${esc(row.状态)}">${esc(row.状态)} <b>${fmt(row.n)}</b></button>`).join('');

  $('#trackBody').innerHTML = d.rows.length ? d.rows.map(r => `
    <tr><td>${orderButton(r.履约单号, r.快递单号)}</td>
      <td>${esc(r.当前渠道 || '—')}<small class="cell-sub">${esc(r.物流商 || '—')} / ${esc(r.C端承运商 || '—')}</small></td>
      <td>${esc(r.仓库 || '—')}<small class="cell-sub">${esc(r.目的国中文 || '—')} · ${esc(platformName(r.平台))}</small></td>
      <td>${trackBadge(r.轨迹状态, r.轨迹状态中文, r.轨迹子状态, r.未注册,
        r.回补状态 === 'empty' ? '已查询 · 未返回轨迹' :
        r.已签收 ? '已签收 · 轨迹未入库' :
        r.签出时间 ? '已出库 · 待同步轨迹' : undefined)}</td>
      ${ME.preview ? '' : `<td>${r.主预警名称 ? `<span class="badge ${levelTone(r.主预警级别)}">${esc(r.主预警名称)}</span>
      ${r.预警数 > 1 ? `<small class="cell-sub">另有 ${fmt(r.预警数 - 1)} 条</small>` : ''}` : '—'}</td>`}
      <td class="mono">${dt(r.签出时间)}</td>
      <td><span class="cell-truncate" title="${esc(r.最新描述 || '')}">${esc(r.最新描述 || '—')}</span>
        <small class="cell-sub mono">${r.最新事件当地时间 || r.最新事件时间 ? esc(trackTime(r.最新事件当地时间, r.最新事件时间)) : '—'}${r.逐条事件数 ? ` · ${fmt(r.逐条事件数)} 条事件` : ''}</small></td></tr>`).join('') :
    blankRow(ME.preview ? 6 : 7, ME.preview && !query ? '输入完整单号查询物流轨迹' : '没有匹配的履约单');
  pager($('#trackPager'), d.total, d.page, d.size, p => run(() => loadTrack(p)));
}

function prepareAlerts() {
  if (AF) return;
  AF = makeFilters($('#alertFilters'), [
    { type: 'text', label: '履约单号 / 运单号', key: 'q', w: 222 },
    ...dashFilterDefs().filter(def => !['pf', 'lastmile', 'channel'].includes(def.key)),
    { type: 'select', label: '处理状态', key: 'state', options: [
      { v: pendingStates, t: '待跟进（待处理、已认领、处理中）' },
      ...Object.entries(states).map(([v, [, t]]) => ({ v, t }))] },
    { type: 'select', label: '预警规则', key: 'rule', options: ruleOptions() },
    { type: 'select', label: '级别', key: 'level', options: ['紧急', '高', '中'] },
    { type: 'select', label: '命中范围', key: 'main_only', options: [{ v: 'true', t: '仅主预警' }] },
  ], () => run(loadAlerts));
  AF.state.value = pendingStates;
}

function refreshSelection() {
  const selected = $('#alertBody').querySelectorAll('[data-alert-index]:checked');
  $('#selectedAlerts').textContent = selected.length ? `已选 ${selected.length} 条` : '';
  $('#selectPage').checked = Boolean(alertRows.length && alertRows.length === selected.length);
  $('#selectPage').indeterminate = selected.length > 0 && selected.length < alertRows.length;
  $('#selectPage').disabled = !ME.can_edit || !alertRows.length;
  $('#applyBatch').disabled = !ME.can_edit || !selected.length || !$('#batchAction').value;
}

async function loadAlerts(page = 1) {
  prepareAlerts();
  const d = await latest('alerts', '/api/alerts?' + qs({ sort: 'level', ...readF(AF) }, page, 40));
  if (!d) return;
  if (!d.rows.length && d.total && page > 1) return loadAlerts(Math.ceil(d.total / d.size));
  alertRows = d.rows;
  alertPage = d.page;
  AF._info.textContent = `共 ${fmt(d.total)} 条命中`;
  $('#alertChips').innerHTML = d.summary.slice(0, 7).map(r =>
    `<button type="button" class="chipstat" data-alert-rule="${esc(r.rule)}">${esc(r.rule_cn)} <b>${fmt(r.n)}</b></button>`).join('');
  $('#alertBody').innerHTML = d.rows.length ? d.rows.map((r, index) => `
    <tr><td class="check"><input type="checkbox" data-alert-index="${index}" aria-label="选择这条预警" ${ME.can_edit ? '' : 'disabled'}></td>
      <td>${orderButton(r.order_no, r.tracking_no)}</td>
      <td><span class="badge ${levelTone(r.level)}">${esc(r.level)}</span> ${esc(r.rule_cn)}<small class="cell-sub">${r.is_main ? '主预警' : '次预警'} · ${esc(r.category || '—')}</small></td>
      <td>${esc(r.carrier || '—')}<small class="cell-sub">${esc(r.country || '—')}</small></td>
      <td>${trackBadge(r.track_status, trackingGroups().find(row => row.v === r.track_status)?.t, r.track_sub)}</td>
      <td>${measure(r.days, ' 天')}</td><td>${stateBadge(r.state)}</td><td>${esc(r.assignee || '—')}</td>
    </tr>`).join('') : blankRow(8, '没有符合条件的预警');
  pager($('#alertPager'), d.total, d.page, d.size, p => run(() => loadAlerts(p)));
  refreshSelection();
}

async function applyBatch() {
  const action = $('#batchAction').value;
  const items = [...$('#alertBody').querySelectorAll('[data-alert-index]:checked')].map(input => {
    const { order_no, rule } = alertRows[Number(input.dataset.alertIndex)];
    return { order_no, rule };
  });
  if (!action || !items.length) return;
  $('#applyBatch').disabled = true;
  try {
    const result = await jsonPost('/api/alerts/batch', { action, items });
    toast(`已更新 ${result.updated} 条预警 · ${actions[action]}`);
    invalidatePages();
    await Promise.all([loadAlerts(alertPage), loadActivity()]);
  } finally {
    refreshSelection();
  }
}

/* ================= 渠道时效 ================= */
const TIMING_BASIS = {
  paid: { label: '下单到签收', note: '从支付时间（北京时间）到妥投。优先 17TRACK，缺失时按仓库时间口径用 WMS 补算。' },
  shipped: { label: '出库到签收', note: '从签出时间（北京时间）到妥投。优先 17TRACK，缺失时按仓库时间口径用 WMS 补算。' },
  online: { label: '上网到签收', note: '从 17TRACK 首条有效揽收或运输扫描到妥投。妥投时间优先 17TRACK，缺失时用 WMS 补算。' },
};
let CF = null;
let channelRows = [];
let channelBasis = 'shipped';
let renderedChannels = null;

async function loadChannels() {
  if (!CF) CF = makeFilters($('#channelFilters'), [
    ...dashFilterDefs(),
    { type: 'select', label: '最少单量（默认 100）', key: 'min_n', options: [1, 10, 30, 100, 500] },
  ], () => run(loadChannels));

  const basis = channelBasis;
  const loading = $('#channelLoading');
  const results = $('#channelResults');
  const currentResult = renderedChannels !== null;
  loading.hidden = false;
  loading.classList.toggle('channel-refresh', currentResult);
  loading.textContent = currentResult ?
    `正在更新「${TIMING_BASIS[basis].label}」 · 下方为上次结果` : '正在读取渠道时效…';
  results.inert = currentResult;
  results.setAttribute('aria-busy', 'true');
  const filters = readF(CF);
  const d = await latest('channels', '/api/channels?' + qs({ ...filters, basis })).catch(error => {
    if (basis === channelBasis) loading.textContent = '渠道时效读取失败。' + error.message;
    return null;
  });
  if (!d || VIEW !== 'channels') return;
  loading.hidden = true;
  results.hidden = false;
  results.inert = false;
  results.setAttribute('aria-busy', 'false');
  if (renderedChannels === d) return;
  renderedChannels = d;
  CF._info.textContent = `数据截止 ${d.asof}`;

  const rows = d.rows || [];
  channelRows = rows;
  const totalOrders = rows.reduce((a, r) => a + r.orders, 0);
  const signed = rows.reduce((a, r) => a + r.signed, 0);
  const samples = rows.reduce((a, r) => a + r.samples, 0);
  const fallback = rows.reduce((a, r) => a + (r.fallbackSamples || 0), 0);
  const overAll = rows.reduce((a, r) => a + (r.overdue || 0), 0);
  const inAll = rows.reduce((a, r) => a + r.slaInTransit, 0);
  $('#channelKpi').innerHTML = [
    ['C 端渠道数', fmt(rows.length), `单量 ≥${CF.min_n.value || 100}`],
    ['有效签收样本', fmt(samples), signed ? `含 WMS 补算 ${fmt(fallback)} 单` : '当前范围无可用的妥投时间'],
    ['覆盖单量', fmt(totalOrders), '参与统计'],
    ['SLA 在途超时', fmt(overAll), inAll ? `占可计时在途 ${fmt1(100 * overAll / inAll)}%` : '当前范围无可计时的在途单'],
  ].map(([l, v, s]) => `<div class="kpi"><div class="lbl">${l}</div><div class="val">${v}</div><div class="fl">${s}</div></div>`).join('');

  $('#channelBody').innerHTML = rows.length ? rows.map(r => {
    return `<tr>
      <td>${ME.preview ? esc(r.channel) :
        `<button type="button" class="order-link" data-channel-track="${esc(r.key)}">${esc(r.channel)}</button>`}</td>
      <td>${fmt(r.orders)}</td><td>${fmt(r.signed)}</td><td>${fmt(r.samples)}</td>
      <td>${measure(r.avgDays, ' 天')}</td><td>${measure(r.p90Days, ' 天')}</td>
      <td>${measure(r.rate3, '%')}</td><td>${measure(r.rate5, '%')}</td>
      <td><b>${measure(r.rate6, '%')}</b></td><td>${measure(r.rate7, '%')}</td>
      <td title="SLA 有效签收样本 ${fmt(r.sla_samples)} 单"><b>${measure(r.slaRate, '%')}</b></td>
      <td>${fmt(r.inTransit)}</td>
      <td class="${r.overRate > 20 ? 'neg' : ''}">${fmt(r.overdue)}${r.overRate != null ? ` (${fmt1(r.overRate)}%)` : ''}</td>
    </tr>`;
  }).join('') : blankRow(13, '没有满足条件的渠道');

  $('#chanCountryBody').innerHTML = (d.byCountry || []).map(r => `
    <tr><td><b>${esc(r.ch)}</b></td><td>${esc(r.ctry || '—')}</td><td>${fmt(r.n)}</td><td>${fmt(r.samples)}</td>
    <td>${measure(r.avgDays, ' 天')}</td><td>${measure(r.rate6, '%')}</td></tr>`).join('')
    || blankRow(6, '当前筛选无达到样本量的渠道组合');

  const chartRows = rows.filter(row => row.samples > 0).slice(0, 8).reverse();
  $('#channelCharts').hidden = !chartRows.length;
  $('#channelChartEmpty').hidden = Boolean(chartRows.length);
  if (!chartRows.length) return;
  const label = TIMING_BASIS[basis].label;
  $('#channelChartScope').textContent = `${label} · 单量靠前且有有效样本的 ${chartRows.length} 个渠道 · P90 为 90% 样本在该天数内签收`;
  drawChannelBars('channelDurationChart', chartRows, [
    ['平均时效', 'avgDays', chartTheme.series[1]], ['P90', 'p90Days', chartTheme.ink],
  ], ' 天', label, filters);
  drawChannelBars('channelRateChart', chartRows, [
    ['3 天', 'rate3', chartTheme.series[0]], ['5 天', 'rate5', chartTheme.series[1]],
    ['6 天', 'rate6', chartTheme.series[2]], ['7 天', 'rate7', chartTheme.series[3]],
  ], '%', label, filters);
}

function drawChannelBars(id, rows, metrics, unit, basisLabel, filters) {
  const chart = initChart(id);
  resizeVisibleCharts();
  chart.setOption({
    animation: chartMotion, animationDuration: 230, animationDurationUpdate: 140, animationEasing: 'quarticOut',
    grid: { top: 35, left: 4, right: 25, bottom: 8, containLabel: true },
    legend: { top: 2, itemWidth: 13, itemHeight: 8, textStyle: { color: chartTheme.text } },
    tooltip: {
      ...chartTip, trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: points => {
        const row = rows[points[0].dataIndex];
        return `<b>${esc(row.channel)}</b> · ${basisLabel}<br>` +
          points.map(point => `${point.marker}${point.seriesName} ${measure(point.value, unit)}`).join('<br>') +
          `<br>有效样本 ${fmt(row.samples)} / ${fmt(row.signed)} 单`;
      },
    },
    xAxis: { type: 'value', min: 0, max: unit === '%' ? 100 : undefined,
      splitLine: { lineStyle: { color: chartTheme.grid } },
      axisLabel: { color: chartTheme.muted, formatter: value => value + unit } },
    yAxis: { type: 'category', data: rows.map(row => row.channel),
      axisTick: { show: false }, axisLine: { show: false },
      axisLabel: { color: chartTheme.text, width: 104, overflow: 'truncate' } },
    series: metrics.map(([name, key, color]) => ({
      name, type: 'bar', barMaxWidth: unit === '%' ? 6 : 12,
      data: rows.map(row => row[key]), itemStyle: { color, borderRadius: [0, 3, 3, 0] },
    })),
  });
  chart.off('click');
  if (!ME.preview) chart.on('click', point => openTracking({ ...filters, lastmile: rows[point.dataIndex].key }));
}

/* ================= 数据与规则 ================= */
let activeConfiguration = null;

const syncRunStates = {
  running: ['blue', '同步中'], ok: ['ok', '同步成功'], idle: ['m', '无待查运单'],
  failed: ['e', '同步失败'],
};
const pushStates = { queued: ['blue', '待入库'], applied: ['ok', '已入库'], ignored: ['m', '已核对'] };
const syncReasons = {
  credentials: '请检查本机 17TRACK 凭据', prepare: '查询或准备数据失败', publish: '数据库切换失败',
  interrupted: '上次任务中断', unmatched: '待匹配履约单', postnet: '按规则跳过 Postnet',
  not_in_scope: '不在当前处理范围',
  different_carrier: '与已匹配的承运商代码不同', older: '本地已有更新的轨迹',
  notification_only: '通知已记录',
};
const syncBadge = (tone, label) => `<span class="badge ${tone}">${esc(label)}</span>`;
const syncClock = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
});
const syncTime = value => value && Number.isFinite(Date.parse(value)) ? syncClock.format(new Date(value)) : '—';
const sourceRows = rows => rows.map(([label, value]) =>
  `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');

async function loadSyncStatus() {
  const state = await latest('trackSync', '/api/17track/sync-status', { cache: 'no-store' });
  if (!state) return;
  const { poll, webhook } = state;
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
    ['运行计划', poll.schedule ? '每 2 小时检查近期运单' : '计划任务状态暂不可读'],
    ['最近成功', syncTime(poll.lastSuccess)],
    ['最近一批', run?.detail?.queried != null ?
      `查询 ${fmt(run.detail.queried)} 单 · 取回 ${fmt(run.detail.with_events)} 单逐条轨迹` :
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
  $('#openPending').textContent = `待跟进预警 · ${fmt(pending)} 单`;
  lastImportAt = importedAt;
  setTopMeta(through);
}

async function loadActivity() {
  const d = await latest('activity', '/api/activity');
  if (d) updateActivity(d.pendingOrders, d.lastImportAt, META.range.end);
}

async function loadHealth() {
  const d = await latest('health', '/api/health');
  if (!d) return;
  updateActivity(d.pendingOrders, d.recentImports[0]?.ingested_at, d.range.end);
  const t = d.tracking;
  $('#dataKpi').innerHTML = [
    ['ERP 履约单', fmt(d.orders), `${d.range.start} 至 ${d.range.end}`],
    ['17TRACK 查询记录', fmt(t.records), '按运单号统计'],
    ['已有轨迹事件', fmt(t.withEvent), '有最新事件时间'],
    ['登记未匹配', fmt(t.unmatched), '历史详情查询返回未匹配'],
    ['可查看逐条轨迹的运单', fmt(t.detailedNumbers), `${fmt(t.detailedEvents)} 条承运商事件`],
  ].map(([l, v, s]) => `<div class="kpi"><div class="lbl">${l}</div><div class="val">${v}</div><div class="fl">${s}</div></div>`).join('');
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
  run(loadRules);
}

async function loadRules() {
  if (!$('#rules-data').hidden) return loadHealth();
  if (!$('#rules-sync').hidden) return refreshSyncStatus();
  if (!$('#rules-dictionary').hidden) {
    $('#dictionaryBody').innerHTML = META.trackStatuses.map(row =>
      `<tr><td>${esc(row.label)}</td><td class="mono">${esc(row.status)}</td><td>${esc(row.sub_label)}</td>
        <td class="mono">${esc(row.sub)}</td></tr>`).join('') || blankRow(4, '暂无状态字典');
    return;
  }
  const d = await latest('rules', '/api/rules');
  if (!d || RULES === d) return;
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
  $('#slaBody').innerHTML = (RULES?.channels || []).filter(row => row.ch.toLocaleLowerCase().includes(query))
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

function openConfiguration(kind, key) {
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

async function openOrder(no, refresh = false) {
  if (!refresh) drawerTrigger = document.activeElement;
  const oldScroll = refresh ? $('#dBody').scrollTop : 0;
  const lookup = ME.preview ? '?q=' + encodeURIComponent(lastTrackingQuery) : '';
  const d = await latest('drawer', '/api/orders/' + encodeURIComponent(no) + lookup);
  if (!d) return;
  const o = d.order;
  $('#dTitle').textContent = o.履约单号;
  $('#dSub').textContent = [platformName(o.平台), o.仓库, o.当前渠道 || o.物流商, o.目的国中文 || o.目的国].filter(Boolean).join(' · ');

  const kv = [
    ...(!ME.preview ? [['订单号', o.订单号], ['平台订单号', o.平台订单号]] : []),
    ['运单号', o.快递单号], ['收货国家', o.目的国中文 || o.目的国],
    ...(!ME.preview ? [['州 / 省', o.目的州省], ['城市', o.目的城市], ['邮编', o.目的邮编]] : []),
    ['当前库内节点', o.签出时间 ? '已出库' : o.状态],
    ['ERP 出库单状态', o.状态],
    ...(!ME.preview ? [[`ERP / WMS 妥投（${o.WMS时间口径 || '原始值'}）`, dt(o.WMS妥投时间 || o.签收时间)]] : []),
    ['17TRACK 妥投', o.签收当地时间 || o.承运商签收时间 ?
      trackTime(o.签收当地时间, o.承运商签收时间) : '—'],
    ['妥投统计来源', o.妥投统计来源 === 'WMS' ? 'WMS 补算' : o.妥投统计来源 || '—'],
    ['C 端承运商', o.C端承运商],
    ['17TRACK 主状态', o.轨迹状态中文 || (o.未注册 ? 'API 账号登记未匹配' :
      o.回补状态 === 'empty' ? '已查询 · 未返回轨迹' :
      o.已签收 ? '已签收 · 轨迹未入库' : o.签出时间 ? '已出库 · 待同步轨迹' : '无本地查询结果')],
    ['17TRACK 子状态', o.轨迹子状态], ['最近查询', dt(o.查询时间)],
  ];

  const alerts = (d.alerts || []).map(a => `
    <div class="note">
      <div class="alert-detail">
        <span class="badge ${levelTone(a.level)}">${esc(a.level)}</span><b>${esc(a.rule_cn)}</b>
        <small>${a.is_main ? '主预警' : '次预警'} · ${measure(a.days, ' 天')}</small>
        ${stateBadge(a.state)}
      </div>
      <p>${esc(a.evidence || '暂无附加证据')}${a.assignee ? ` · 负责人 ${esc(a.assignee)}` : ''}</p>
      ${a.handle_note ? `<p>${esc(a.handle_note)}</p>` : ''}
      <div class="actrow">${allowedActions().map(([key, label]) =>
        `<button type="button" class="btn ghost sm" data-rule="${esc(a.rule)}" data-act="${key}">${label}</button>`).join('')}</div>
    </div>`).join('') || '<p class="note">该单没有命中预警</p>';

  const milestoneLabels = { InfoReceived: '收到运单信息', PickedUp: '已揽收',
    Departure: '起运', Arrival: '到达', AvailableForPickup: '等待自提',
    OutForDelivery: '正在派送', Delivered: '签收', Returning: '退运中', Returned: '已退回' };
  const events = d.events || [];
  const ms = (d.milestones || []).map(m =>
    `<div class="e"><div class="t">${esc(trackTime(m.当地时间, m.时间))}</div><div class="d">${esc(milestoneLabels[m.节点] || m.节点)}</div></div>`).join('');
  const timeline = events.length ? events.map(event => `
    <div class="e">
      <div class="t">${esc(trackTime(event.当地时间, event.事件时间))}
        ${event.事件承运商 ? ` · ${esc(event.事件承运商)}` : ''}</div>
      <div class="d">${esc(event.描述 || milestoneLabels[event.主状态] || '承运商未提供事件描述')}</div>
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
    `<div class="note"><div style="font-size:11px;color:#8b98ab">${dt(n.ts)} · ${esc(n.actor)}</div>${esc(n.body)}</div>`).join('')
    || '<div style="color:#8b98ab;font-size:12px">暂无备注</div>';

  $('#dBody').innerHTML = `
    <div class="kv">${kv.map(([k, v]) => `<div class="i">${k}<b>${esc(v) || '—'}</b></div>`).join('')}</div>
    ${ME.preview ? '' : `<div class="sect">业务预警</div>${alerts}`}
    <div class="sect drawer-section">
      <span>物流时间线</span>
      ${o.单号归一 ? `<a href="https://t.17track.net/zh-cn#nums=${encodeURIComponent(o.单号归一)}" target="_blank" rel="noopener noreferrer">在 17TRACK 查看 ↗</a>` : ''}
    </div>
    ${fulfillmentTimeline ? `<section class="timeline-panel fulfillment-timeline" aria-label="履约节点">
      <h4>履约节点 · 北京时间</h4><ol>${fulfillmentTimeline}</ol>
    </section>` : ''}
    <section class="timeline-panel carrier-timeline" aria-label="承运商轨迹">
      <h4>${timelineLabel}</h4>
      ${!events.length && o.最新描述 ? `<div class="note"><b>最新轨迹</b> · ${esc(trackTime(o.最新事件当地时间, o.最新事件时间))}<br>${esc(o.最新描述)}${o.地点 ? ` · ${esc(o.地点)}` : ''}</div>` : ''}
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
        if ($('#drawer').classList.contains('on')) await openOrder(no, true);
      } finally { save.disabled = false; }
    });
  };
  $('#dBody').querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = () => run(async () => {
      b.disabled = true;
      try {
        await jsonPost('/api/alerts/action', { order_no: no, rule: b.dataset.rule, action: b.dataset.act });
        toast(`处理状态已更新 · ${actions[b.dataset.act]}`);
        invalidatePages();
        if ($('#drawer').classList.contains('on')) await openOrder(no, true);
        await Promise.all([loadActivity(), trackMode === 'alerts' ? loadAlerts(alertPage) : null]);
      } finally { b.disabled = false; }
    });
  });
  $('#mask').classList.add('on');
  $('#drawer').classList.add('on');
  $('#drawer').inert = false;
  $('.app').inert = true;
  $('#dBody').scrollTop = oldScroll;
  if (!refresh) $('#dClose').focus();
}

function closeDrawer() {
  pendingRequests.delete('drawer');
  $('#mask').classList.remove('on');
  $('#drawer').classList.remove('on');
  $('#drawer').inert = true;
  $('.app').inert = false;
  if (drawerTrigger?.isConnected) drawerTrigger.focus();
}

/* ================= 启动 ================= */
run(async function boot() {
  [ME, META] = await Promise.all([api('/api/me'), api('/api/meta')]);
  $('#opsAssistant').hidden = !ME.opsAssistant;
  $('#cloudSharing').hidden = !ME.cloudSharing;
  if (ME.preview) {
    for (const selector of ['[data-v="rules"]', '#logout', '#openPending',
      '#trackTabs', '#showDetailed', '#exportTrack', '#trackAlertHeading']) {
      $(selector).hidden = true;
    }
    $('#trackIntro').textContent = '输入完整的履约单号、运单号、订单号或平台订单号，查看物流状态和时间线。';
  }
  setTopMeta(META.range.end);
  $('#who').textContent = ME.preview ? ME.display_name : `${ME.display_name} · ${ME.role_label}`;
  $('#batchAction').append(...allowedActions().map(([key, label]) => new Option(label, key)));
  $('#batchAction').hidden = !ME.can_edit;
  $('#applyBatch').hidden = !ME.can_edit;
  document.querySelectorAll('#nav button').forEach(b => b.onclick = () => showView(b.dataset.v));
  const periods = [...document.querySelectorAll('#dailyPeriod button')];
  periods.forEach(button => button.onclick = () => {
    if (trend.span === Number(button.dataset.days)) return;
    trend.span = Number(button.dataset.days);
    periods.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    drawDailyVolume();
  });
  document.querySelectorAll('#trackTabs [data-mode]').forEach(btn => btn.onclick = () => {
    setTrackMode(btn.dataset.mode);
    showView('track');
  });
  document.querySelectorAll('#ruleTabs [data-rule-pane]').forEach(btn => btn.onclick = () => setRulePane(btn.dataset.rulePane));
  setInterval(() => {
    if (VIEW === 'rules' && !$('#rules-sync').hidden && !document.hidden) refreshSyncStatus();
  }, 30_000);
  $('#openPending').onclick = () => {
    prepareAlerts();
    AF.state.value = pendingStates;
    setTrackMode('alerts');
    showView('track');
  };
  $('#trackChips').onclick = event => {
    const target = event.target.closest('[data-track-status]');
    if (target) filterByTrackStatus(target.dataset.trackStatus, readF(TF));
  };
  $('#alertChips').onclick = event => {
    const target = event.target.closest('[data-alert-rule]');
    if (target) {
      AF.rule.value = target.dataset.alertRule;
      run(loadAlerts);
    }
  };
  const orderClick = event => {
    const button = event.target.closest('[data-order]');
    if (button) run(() => openOrder(button.dataset.order));
  };
  $('#trackBody').onclick = orderClick;
  $('#showDetailed').onclick = () => openTracking({ coverage: 'detailed', sort: '逐条事件数' });
  $('#alertBody').onclick = orderClick;
  $('#alertBody').onchange = refreshSelection;
  $('#batchAction').onchange = refreshSelection;
  $('#applyBatch').onclick = () => run(applyBatch);
  $('#selectPage').onchange = event => {
    $('#alertBody').querySelectorAll('[data-alert-index]').forEach(box => box.checked = event.target.checked);
    refreshSelection();
  };
  $('#channelBody').onclick = event => {
    const button = event.target.closest('[data-channel-track]');
    if (button) openTracking({ ...readF(CF), lastmile: button.dataset.channelTrack });
  };
  $('#channelBasis').onclick = event => {
    const button = event.target.closest('button[data-basis]');
    if (!button || channelBasis === button.dataset.basis) return;
    channelBasis = button.dataset.basis;
    $('#channelBasisNote').textContent = TIMING_BASIS[channelBasis].note;
    for (const item of $('#channelBasis').querySelectorAll('button')) {
      item.setAttribute('aria-pressed', String(item === button));
    }
    run(loadChannels);
  };
  $('#ruleList').onclick = event => {
    const config = event.target.closest('[data-config-rule]');
    if (config) return openConfiguration('rule', config.dataset.configRule);
    const button = event.target.closest('[data-rule-track]');
    if (button) {
      prepareAlerts();
      AF.rule.value = button.dataset.ruleTrack;
      AF.state.value = '';
      setTrackMode('alerts');
      showView('track');
    }
  };
  $('#slaSearch').oninput = renderSLATable;
  $('#slaBody').onclick = event => {
    const config = event.target.closest('[data-config-sla]');
    if (config) openConfiguration('sla', config.dataset.configSla);
  };
  $('#settingsForm').onsubmit = saveConfiguration;
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
    ['ERP / WMS 妥投（原始值）', row => row.签收时间], ['WMS 时间口径', row => row.WMS时间口径],
    ['妥投统计来源', row => row.妥投统计来源],
    ['17TRACK 妥投（当地时间或标注 UTC）', row => row.签收当地时间 || row.承运商签收时间 ?
      trackTime(row.签收当地时间, row.承运商签收时间) : ''],
    ['最新轨迹（当地时间或标注 UTC）', row => row.最新事件当地时间 || row.最新事件时间 ?
      trackTime(row.最新事件当地时间, row.最新事件时间) : ''],
  ]), trackRows);
  $('#exportAlerts').onclick = () => exportCsv('预警处理-本页', [
    ['履约单号', row => row.order_no], ['运单号', row => row.tracking_no], ['预警', row => row.rule_cn],
    ['级别', row => row.level], ['处理状态', row => states[row.state]?.[1]], ['负责人', row => row.assignee],
    ['后台物流商', row => row.carrier], ['异常持续天数', row => row.days],
  ], alertRows);
  $('#exportChannels').onclick = () => exportCsv('渠道时效-' + TIMING_BASIS[channelBasis].label, [
    ['时效口径', () => TIMING_BASIS[channelBasis].label],
    ['C 端显示物流渠道', row => row.channel], ['单量', row => row.orders], ['有妥投时间', row => row.signed],
    ['有效签收样本', row => row.samples], ['WMS 补算样本', row => row.fallbackSamples],
    ['平均时效（天）', row => row.avgDays], ['P90（天）', row => row.p90Days],
    ['3天达成率（%）', row => row.rate3], ['5天达成率（%）', row => row.rate5],
    ['6天达成率（%）', row => row.rate6], ['7天达成率（%）', row => row.rate7],
    ['SLA 在途超时', row => row.overdue], ['SLA 有效签收样本', row => row.sla_samples], ['SLA达成率（%）', row => row.slaRate],
  ], channelRows);
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
  const v = (location.hash || '').replace('#', '');
  if (v === 'alerts') setTrackMode('alerts');
  if (v === 'detailed') openTracking({ coverage: 'detailed', sort: '逐条事件数' });
  else showView(v === 'alerts' ? 'track' : VIEW_META[v] ? v : 'dash');
  if (!ME.preview && v !== 'rules') run(loadActivity);
  window.addEventListener('resize', resizeVisibleCharts);
});
