/* 履约追踪系统 · 前端 */
const $ = s => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt = n => (n == null || isNaN(n)) ? '—' : Math.round(n).toLocaleString('zh-CN');
const fmt1 = n => (n == null || isNaN(n)) ? '—' : (Math.round(n * 10) / 10).toFixed(1);
const dt = s => s ? String(s).replace('T', ' ').slice(0, 16) : '—';

let ME = null, META = null;
const charts = {};
let toastTimer;

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 2200);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}

/* ---------------- 导航 ---------------- */
const VIEW_META = { dash: '数据总览', orders: '履约单', alerts: '预警处理', track: '轨迹追踪' };
let VIEW = 'dash';

function showView(v) {
  VIEW = v;
  document.querySelectorAll('.view').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  $('#crumb').textContent = VIEW_META[v];
  history.replaceState(null, '', '#' + v);
  setTimeout(() => Object.values(charts).forEach(c => c && c.resize()), 30);
  if (v === 'dash') loadDash();
  if (v === 'orders') loadOrders(1);
  if (v === 'alerts') loadAlerts(1);
}

/* ---------------- 总览 ---------------- */
const KPI_DEF = [
  ['orders', '履约单数', n => fmt(n), '筛选范围内'],
  ['signRate', '签收覆盖率', n => fmt1(n) + '%', '已签收 / 总单'],
  ['rate7', '7 天达成率', n => fmt1(n) + '%', '已签收中 7 天内'],
  ['inboundHours', '仓内处理时长', n => fmt1(n) + ' 小时', '创建 → 签出'],
  ['transitDays', '物流运输天数', n => fmt1(n) + ' 天', '签出 → 签收'],
  ['alertN', '活跃预警', n => fmt(n), '待处理'],
];

async function loadDash() {
  const [s, h] = await Promise.all([api('/api/stats'), api('/api/health')]);
  const openAlerts = h.alerts;
  const vals = { ...s, alertN: openAlerts };
  $('#kpis').innerHTML = KPI_DEF.map(([k, lbl, f, sub]) =>
    `<div class="kpi"><div class="lbl">${lbl}</div>
     <div class="val" ${k === 'alertN' ? 'style="color:#c0392b"' : ''}>${f(vals[k])}</div>
     <div class="fl">${sub}</div></div>`).join('');
  $('#topMeta').textContent = `数据 ${h.range.start} ~ ${h.range.end} · ${fmt(h.orders)} 单 · 预警 ${fmt(h.alerts)} 条`;

  drawTrend(s.months || []);
  drawBars('cCarrier', (s.byCarrier || []).map(r => ({ name: r.k, n: r.n })), 8);
  drawBars('cWh', (s.byWarehouse || []).map(r => ({ name: r.k, n: r.n })), 10);
}

function initChart(id) {
  if (!charts[id]) charts[id] = echarts.init(document.getElementById(id), null, { renderer: 'canvas' });
  return charts[id];
}

function drawTrend(rows) {
  const c = initChart('cTrend');
  c.setOption({
    grid: { left: 8, right: 46, top: 30, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis' },
    legend: { right: 0, top: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: rows.map(r => r.m), axisTick: { show: false }, axisLine: { lineStyle: { color: '#e6ebf2' } }, axisLabel: { color: '#8b98ab', fontSize: 10.5 } },
    yAxis: [
      { type: 'value', splitLine: { lineStyle: { color: '#f0f3f8' } }, axisLabel: { color: '#8b98ab', fontSize: 10, formatter: v => v >= 10000 ? (v / 10000) + '万' : v } },
      { type: 'value', splitLine: { show: false }, axisLabel: { color: '#8b98ab', fontSize: 10, formatter: '{value} 天' } },
    ],
    series: [
      { name: '单量', type: 'bar', barMaxWidth: 34, data: rows.map(r => r.n), itemStyle: { color: '#4E79A7', borderRadius: [3, 3, 0, 0] } },
      { name: '物流天数', type: 'line', yAxisIndex: 1, data: rows.map(r => r.d && Math.round(r.d * 100) / 100), smooth: true, symbolSize: 5, itemStyle: { color: '#e8a33d' }, connectNulls: true },
    ],
  }, true);
}

function drawBars(id, rows, limit) {
  const c = initChart(id);
  const data = rows.slice(0, limit).reverse();
  c.setOption({
    grid: { left: 8, right: 62, top: 6, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: p => `${p[0].name}<br/>${fmt(p[0].value)} 单` },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: '#f0f3f8' } }, axisLabel: { color: '#8b98ab', fontSize: 10, formatter: v => v >= 10000 ? (v / 10000) + '万' : v } },
    yAxis: { type: 'category', data: data.map(r => r.name), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: '#5b6675', fontSize: 11 } },
    series: [{ type: 'bar', barMaxWidth: 15, data: data.map(r => r.n), itemStyle: { color: '#4E79A7', borderRadius: [0, 3, 3, 0] }, label: { show: true, position: 'right', fontSize: 10, color: '#8b98ab', formatter: p => fmt(p.value) } }],
  }, true);
}

/* ---------------- 通用筛选条 ---------------- */
function buildFilters(host, defs, onApply) {
  host.innerHTML = '';
  const make = (label, key, options, multi) => {
    if (multi) {
      const box = el('select');
      box.multiple = false;
      box.dataset.key = key;
      box.innerHTML = `<option value="">${label}：全部</option>` +
        options.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('');
      box.onchange = onApply;
      host.appendChild(box);
      return box;
    }
    const inp = el('input');
    inp.placeholder = label;
    inp.dataset.key = key;
    inp.oninput = () => { clearTimeout(inp._t); inp._t = setTimeout(onApply, 350); };
    host.appendChild(inp);
    return inp;
  };
  const refs = {};
  defs.forEach(d => {
    if (d.type === 'text') refs[d.key] = make(d.label, d.key, [], false);
    else if (d.type === 'select') refs[d.key] = make(d.label, d.key, d.options || [], true);
  });
  const btn = el('button', 'btn ghost sm', '重置');
  btn.onclick = () => { host.querySelectorAll('input').forEach(i => i.value = ''); host.querySelectorAll('select').forEach(s => s.value = ''); onApply(); };
  host.appendChild(btn);
  host.appendChild(el('span', 'grow'));
  const info = el('span', '', '');
  info.style.cssText = 'font-size:11.5px;color:#8b98ab';
  host.appendChild(info);
  refs._info = info;
  return refs;
}

function readFilters(refs) {
  const out = {};
  Object.entries(refs).forEach(([k, e]) => { if (k !== '_info' && e && e.value) out[k] = e.value; });
  return out;
}

function qs(obj, page, size) {
  const p = new URLSearchParams();
  Object.entries(obj).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); });
  if (page) p.set('page', page);
  if (size) p.set('size', size);
  return p.toString();
}

/* ---------------- 订单 ---------------- */
let OF = null, OPAGE = 1;

function renderOrderFilters(onApply) {
  if (OF) return;
  OF = buildFilters($('#orderFilters'), [
    { type: 'text', label: '搜索 履约单/运单/订单号', key: 'q' },
    { type: 'select', label: '仓库', key: 'wh', options: META.warehouses },
    { type: 'select', label: '物流商', key: 'cr', options: META.carriers },
    { type: 'select', label: '状态', key: 'status', options: META.orderStatuses },
    { type: 'select', label: '月份', key: 'month', options: META.months },
  ], () => onApply(1));
}

async function loadOrders(page = OPAGE) {
  renderOrderFilters(p => loadOrders(p));
  OPAGE = page;
  const f = readFilters(OF);
  const d = await api('/api/orders?' + qs(f, page, 50));
  OF._info.textContent = `共 ${fmt(d.total)} 单`;
  $('#orderBody').innerHTML = d.rows.length ? d.rows.map(r => `
    <tr class="clickable" data-no="${esc(r.履约单号)}">
      <td class="mono">${esc(r.履约单号)}</td>
      <td class="mono">${esc(r.快递单号) || '—'}</td>
      <td>${esc(r.平台)}</td><td>${esc(r.仓库)}</td><td>${esc(r.物流商)}</td>
      <td>${esc(r.目的国中文 || r.目的国)}</td>
      <td>${esc(r.状态)}</td>
      <td>${dt(r.创建时间)}</td><td>${dt(r.签出时间)}</td><td>${dt(r.签收时间)}</td>
      <td>${fmt(r.件数)}</td>
      <td>${r.有轨迹 ? '<span class="badge blue">有</span>' : '<span class="badge gray">无</span>'}</td>
    </tr>`).join('') : `<tr><td colspan="12" class="empty">没有匹配的履约单</td></tr>`;
  $('#orderBody').querySelectorAll('tr.clickable').forEach(tr => tr.onclick = () => openOrder(tr.dataset.no));
  pager($('#orderPager'), d.total, d.page, d.size, p => loadOrders(p));
}

/* ---------------- 预警 ---------------- */
let AF = null;

function stateBadge(s) {
  const m = { open: ['gray', '待处理'], claimed: ['blue', '已认领'], handling: ['purple', '处理中'],
              resolved: ['ok', '已解决'], archived: ['m', '已归档'] };
  const [cls, txt] = m[s] || ['gray', s];
  return `<span class="badge ${cls}">${txt}</span>`;
}
function levelBadge(l) {
  const m = { '紧急': 'e', '高': 'h', '中': 'm' };
  return `<span class="badge ${m[l] || 'm'}">${esc(l)}</span>`;
}

function renderAlertFilters(onApply) {
  if (AF) return;
  AF = buildFilters($('#alertFilters'), [
    { type: 'text', label: '搜索 履约单/运单号', key: 'q' },
    { type: 'select', label: '预警规则', key: 'rule_cn', options: META.rules.map(r => r.rule_cn) },
    { type: 'select', label: '级别', key: 'level', options: ['紧急', '高', '中'] },
    { type: 'select', label: '处理状态', key: 'state', options: ['open', 'claimed', 'handling', 'resolved', 'archived'] },
    { type: 'select', label: '物流商', key: 'cr', options: META.carriers },
  ], () => onApply(1));
}

async function loadAlerts(page = 1) {
  renderAlertFilters(p => loadAlerts(p));
  const f = readFilters(AF);
  // 前端用中文规则名，后端要规则 key
  if (f.rule_cn) {
    const hit = META.rules.find(r => r.rule_cn === f.rule_cn);
    delete f.rule_cn; if (hit) f.rule = hit.rule;
  }
  const d = await api('/api/alerts?' + qs(f, page, 50));
  AF._info.textContent = `共 ${fmt(d.total)} 条`;
  $('#alertBody').innerHTML = d.rows.length ? d.rows.map(r => `
    <tr class="clickable" data-no="${esc(r.order_no)}">
      <td class="mono">${esc(r.order_no)}</td>
      <td>${esc(r.rule_cn)}</td>
      <td>${levelBadge(r.level)}</td>
      <td>${fmt1(r.days)}</td>
      <td>${esc(r.carrier) || '—'}</td><td>${esc(r.country) || '—'}</td>
      <td style="color:#8b98ab">${esc(r.track_status) || '—'}</td>
      <td>${stateBadge(r.state)}</td>
      <td>${esc(r.assignee) || '—'}</td>
      <td><button class="btn ghost sm" data-act="claim" data-no="${esc(r.order_no)}" data-rule="${esc(r.rule)}">认领</button></td>
    </tr>`).join('') : `<tr><td colspan="10" class="empty">没有匹配的预警</td></tr>`;

  $('#alertBody').querySelectorAll('tr.clickable').forEach(tr => {
    tr.onclick = e => { if (e.target.closest('button')) return; openOrder(tr.dataset.no); };
  });
  $('#alertBody').querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api('/api/alerts/action', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ order_no: b.dataset.no, rule: b.dataset.rule, action: 'claim' }),
        });
        toast('已认领');
        loadAlerts(d.page);
      } catch (e) { toast(e.message); b.disabled = false; }
    };
  });
  pager($('#alertPager'), d.total, d.page, d.size, p => loadAlerts(p));
}

/* ---------------- 轨迹 ---------------- */
async function searchTrack() {
  const q = $('#trackQ').value.trim();
  if (!q) return;
  const d = await api('/api/orders?' + qs({ q }, 1, 30));
  $('#trackHint').textContent = `匹配 ${fmt(d.total)} 单`;
  $('#trackBody').innerHTML = d.rows.length ? d.rows.map(r => `
    <tr class="clickable" data-no="${esc(r.履约单号)}">
      <td class="mono">${esc(r.履约单号)}</td>
      <td class="mono">${esc(r.快递单号) || '—'}</td>
      <td>${esc(r.物流商)}</td>
      <td>${r.有轨迹 ? '<span class="badge blue">已同步轨迹</span>' : '<span class="badge gray">无轨迹数据</span>'}</td>
      <td colspan="3" style="color:#8b98ab">点击查看完整轨迹时间轴</td>
    </tr>`).join('') : `<tr><td colspan="7" class="empty">没有匹配结果</td></tr>`;
  $('#trackBody').querySelectorAll('tr.clickable').forEach(tr => tr.onclick = () => openOrder(tr.dataset.no));
}

/* ---------------- 分页 ---------------- */
function pager(host, total, page, size, go) {
  const pages = Math.max(1, Math.ceil(total / size));
  host.innerHTML = `<span>第 ${page} / ${pages} 页 · 共 ${fmt(total)} 条</span><span class="sp"></span>`;
  const mk = (txt, p, dis) => { const b = el('button', '', txt); b.disabled = dis; b.onclick = () => go(p); return b; };
  host.appendChild(mk('‹ 上一页', page - 1, page <= 1));
  host.appendChild(mk('下一页 ›', page + 1, page >= pages));
}

/* ---------------- 详情抽屉 ---------------- */
async function openOrder(no) {
  const d = await api('/api/orders/' + encodeURIComponent(no));
  const o = d.order, t = d.order && d.order.轨迹状态;
  $('#dTitle').textContent = o.履约单号;
  $('#dSub').textContent = `${o.平台} · ${o.仓库} · ${o.物流商} · ${o.目的国中文 || o.目的国 || ''}`;

  const kv = [
    ['出库单号', o.出库单号], ['快递单号', o.快递单号], ['状态', o.状态],
    ['创建', dt(o.创建时间)], ['签出', dt(o.签出时间)], ['签收', dt(o.签收时间)],
    ['件数', fmt(o.件数) + ' 件'], ['SKU 数', fmt(o.sku数)],
    ['仓内时长', o.仓内分钟 > 0 ? fmt1(o.仓内分钟 / 60) + ' 小时' : '—'],
    ['物流天数', o.物流分钟 > 0 ? fmt1(o.物流分钟 / 1440) + ' 天' : '—'],
    ['轨迹状态', o.轨迹状态中文 || o.轨迹状态 || '无轨迹'],
    ['承运商签收', dt(o.承运商签收时间)],
  ];

  const alerts = (d.alerts || []).map(a => `
    <div class="note" style="display:flex;gap:10px;align-items:center;justify-content:space-between">
      <div>${levelBadge(a.level)} <b>${esc(a.rule_cn)}</b>
        <span style="color:#8b98ab;margin-left:6px">${esc(a.evidence || '')} · ${fmt1(a.days)} 天</span></div>
      <div style="display:flex;gap:6px;align-items:center">${stateBadge(a.state)}</div>
    </div>`).join('') || '<div class="note">该单没有命中预警</div>';

  const acts = (d.alerts || []).map(a => `
    <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
      <span style="font-size:11px;color:#8b98ab;min-width:78px">${esc(a.rule_cn)}</span>
      ${['claim|认领', 'handling|处理中', 'resolve|已解决', 'archive|归档', 'reopen|重开'].map(s => {
        const [k, lbl] = s.split('|');
        return `<button class="btn ghost sm" data-no="${esc(o.履约单号)}" data-rule="${esc(a.rule)}" data-act="${k}">${lbl}</button>`;
      }).join('')}
    </div>`).join('');

  const ms = (d.milestones || []).map(m => `
    <div class="e"><div class="t">${dt(m.时间)}</div><div class="d">${esc(m.节点)}</div></div>`).join('')
    || '<div style="color:#8b98ab;font-size:12px">该单在 17track 中没有轨迹节点</div>';

  const skus = (d.skus || []).map(s => `
    <tr><td class="mono">${esc(s.sku)}</td><td>${esc(s.商品名称) || '—'}</td><td>${fmt(s.数量)}</td><td>${esc(s.类目) || '—'}</td></tr>`).join('');

  const notes = (d.notes || []).map(n => `
    <div class="note"><div style="font-size:11px;color:#8b98ab">${dt(n.ts)} · ${esc(n.actor)}</div>${esc(n.body)}</div>`).join('')
    || '<div style="color:#8b98ab;font-size:12px">暂无备注</div>';

  $('#dBody').innerHTML = `
    <div class="kv">${kv.map(([k, v]) => `<div class="i">${k}<b>${esc(v) || '—'}</b></div>`).join('')}</div>
    <div class="sect">预警</div>${alerts}${acts}
    <div class="sect">17track 轨迹</div><div class="tl">${ms}</div>
    <div class="sect">SKU 明细</div>
    <table class="tbl"><thead><tr><th>SKU</th><th>商品名称</th><th>数量</th><th>类目</th></tr></thead>
    <tbody>${skus || '<tr><td colspan="4" class="empty">无明细</td></tr>'}</tbody></table>
    <div class="sect">备注</div>${notes}
    <div style="display:flex;gap:8px;margin-top:8px">
      <input id="noteIn" placeholder="写一条备注…" style="flex:1;border:1px solid #e6ebf2;border-radius:8px;padding:8px 10px;outline:none">
      <button class="btn sm" id="noteSave">保存</button>
    </div>`;

  $('#noteSave').onclick = async () => {
    const v = $('#noteIn').value.trim();
    if (!v) return toast('备注不能为空');
    await api(`/api/orders/${encodeURIComponent(o.履约单号)}/notes`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ body: v }),
    });
    toast('已保存'); openOrder(o.履约单号);
  };
  $('#dBody').querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api('/api/alerts/action', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ order_no: b.dataset.no, rule: b.dataset.rule, action: b.dataset.act }),
        });
        toast('已更新'); openOrder(o.履约单号);
        if (VIEW === 'alerts') loadAlerts();
      } catch (e) { toast(e.message); b.disabled = false; }
    };
  });
  $('#mask').classList.add('on');
  $('#drawer').classList.add('on');
}

function closeDrawer() { $('#mask').classList.remove('on'); $('#drawer').classList.remove('on'); }

/* ---------------- 启动 ---------------- */
(async function boot() {
  try { ME = await api('/api/me'); } catch { return; }
  $('#who').textContent = `${ME.display_name} · ${ME.role_label}`;
  META = await api('/api/meta');
  document.querySelectorAll('#nav button').forEach(b => b.onclick = () => showView(b.dataset.v));
  $('#dClose').onclick = closeDrawer;
  $('#mask').onclick = closeDrawer;
  $('#trackGo').onclick = searchTrack;
  $('#trackQ').onkeydown = e => { if (e.key === 'Enter') searchTrack(); };
  $('#logout').onclick = async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; };
  const v = (location.hash || '').replace('#', '');
  showView(VIEW_META[v] ? v : 'dash');
  window.addEventListener('resize', () => Object.values(charts).forEach(c => c && c.resize()));
})();
