/* 履约追踪系统 · 前端（四页：总览 / 轨迹监控 / 渠道时效 / 数据与规则） */
const $ = s => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt = n => (n == null || isNaN(n)) ? '—' : Math.round(n).toLocaleString('zh-CN');
const fmt1 = n => (n == null || isNaN(n)) ? '—' : (Math.round(n * 10) / 10).toFixed(1);
const dt = s => s ? String(s).replace('T', ' ').slice(0, 16) : '—';

let ME = null, META = null, RULES = null;
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

const VIEW_META = { dash: '数据总览', track: '轨迹监控', channels: '渠道时效', rules: '数据与规则' };
let VIEW = 'dash';

function showView(v) {
  VIEW = v;
  document.querySelectorAll('.view').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  $('#crumb').textContent = VIEW_META[v];
  history.replaceState(null, '', '#' + v);
  setTimeout(() => Object.values(charts).forEach(c => c && c.resize()), 30);
  if (v === 'dash') loadDash();
  if (v === 'track') loadTrack(1);
  if (v === 'channels') loadChannels();
  if (v === 'rules') loadRules();
}

function initChart(id) {
  if (!charts[id]) charts[id] = echarts.init(document.getElementById(id), null, { renderer: 'canvas' });
  return charts[id];
}

/* ---------------- 筛选条 ---------------- */
function makeFilters(host, defs, onChange) {
  host.innerHTML = '';
  const refs = {};
  defs.forEach(d => {
    if (d.type === 'text') {
      const i = el('input'); i.placeholder = d.label; i.dataset.key = d.key; i.style.width = (d.w || 210) + 'px';
      i.oninput = () => { clearTimeout(i._t); i._t = setTimeout(() => onChange(1), 350); };
      host.appendChild(i); refs[d.key] = i;
    } else if (d.type === 'date') {
      const i = el('input'); i.type = 'date'; i.dataset.key = d.key; i.title = d.label;
      i.onchange = () => onChange(1);
      host.appendChild(i); refs[d.key] = i;
    } else {
      const s = el('select'); s.dataset.key = d.key;
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
  btn.onclick = () => { host.querySelectorAll('input,select').forEach(x => x.value = ''); onChange(1); };
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
  Object.entries(refs).forEach(([k, e]) => { if (k !== '_info' && e && e.value) o[k] = e.value; });
  return o;
}

function qs(o, page, size) {
  const p = new URLSearchParams();
  Object.entries(o).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); });
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
let DF = null, DFOPTS = null;

function dashFilterDefs() {
  return [
    { type: 'date', label: '开始日期', key: 'start' },
    { type: 'date', label: '结束日期', key: 'end' },
    { type: 'select', label: '月份', key: 'month', options: META.months },
    { type: 'select', label: '仓库', key: 'wh', options: META.warehouses },
    { type: 'select', label: '平台', key: 'pf', options: META.platforms },
    { type: 'select', label: '物流商', key: 'cr', options: META.carriers },
  ];
}

async function loadDash() {
  if (!DF) DF = makeFilters($('#dashFilters'), dashFilterDefs(), () => loadDash());
  const f = readF(DF);
  const d = await api('/api/overview?' + qs(f));
  DF._info.textContent = `数据截止 ${d.asof}`;
  $('#topMeta').textContent = `数据截止 ${d.asof}`;

  const k = d.kpi;
  const defs = [
    ['履约单数', fmt(k.orders), '筛选范围'],
    ['总件数', fmt(k.items), `单均 ${k.orders ? fmt1(k.items / k.orders) : '—'} 件`],
    ['签收覆盖率', fmt1(k.signRate) + '%', `已签收 ${fmt(k.signed)}`],
    ['仓内处理', fmt1(k.inboundHours) + ' 小时', '创建 → 签出'],
    ['物流运输', fmt1(k.transitDays) + ' 天', '签出 → 签收'],
    ['6 天达成率', fmt1(k.rate6) + '%', '已签收中 ≤6 天'],
  ];
  $('#dashKpi').innerHTML = defs.map(([l, v, s]) =>
    `<div class="kpi"><div class="lbl">${l}</div><div class="val">${v}</div><div class="fl">${s}</div></div>`).join('');

  const c = initChart('cMonthly');
  c.setOption({
    grid: { left: 8, right: 46, top: 30, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis' },
    legend: { right: 0, top: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: d.monthly.map(r => r.m), axisTick: { show: false }, axisLine: { lineStyle: { color: '#e6ebf2' } }, axisLabel: { color: '#8b98ab', fontSize: 10.5 } },
    yAxis: [{ type: 'value', splitLine: { lineStyle: { color: '#f0f3f8' } }, axisLabel: { color: '#8b98ab', fontSize: 10, formatter: v => v >= 10000 ? (v / 10000) + '万' : v } },
            { type: 'value', splitLine: { show: false }, axisLabel: { color: '#8b98ab', fontSize: 10, formatter: '{value} 天' } }],
    series: [
      { name: '单量', type: 'bar', barMaxWidth: 34, data: d.monthly.map(r => r.n), itemStyle: { color: '#4E79A7', borderRadius: [3, 3, 0, 0] } },
      { name: '物流天数', type: 'line', yAxisIndex: 1, smooth: true, symbolSize: 5, connectNulls: true, itemStyle: { color: '#e8a33d' }, data: d.monthly.map(r => r.d && Math.round(r.d * 100) / 100) },
    ],
  }, true);

  drawHBar('cCarrier', d.byCarrier.map(r => ({ name: r.k, n: r.n })), 10);
  drawHBar('cWarehouse', d.byWarehouse.map(r => ({ name: r.k, n: r.n })), 10);
  drawHBar('cCountry', d.byCountry.map(r => ({ name: r.k, n: r.n })), 12);
  drawHBar('cStatus', d.byStatus.map(r => ({ name: r.k, n: r.n })), 10);
}

function drawHBar(id, rows, limit) {
  const c = initChart(id);
  const data = rows.slice(0, limit).reverse();
  c.setOption({
    grid: { left: 8, right: 66, top: 6, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: p => `${p[0].name}<br/>${fmt(p[0].value)} 单` },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: '#f0f3f8' } }, axisLabel: { color: '#8b98ab', fontSize: 10, formatter: v => v >= 10000 ? (v / 10000) + '万' : v } },
    yAxis: { type: 'category', data: data.map(r => r.name || '—'), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: '#5b6675', fontSize: 11, width: 120, overflow: 'truncate' } },
    series: [{ type: 'bar', barMaxWidth: 15, data: data.map(r => r.n), itemStyle: { color: '#4E79A7', borderRadius: [0, 3, 3, 0] }, label: { show: true, position: 'right', fontSize: 10, color: '#8b98ab', formatter: p => fmt(p.value) } }],
  }, true);
}

/* ================= 轨迹监控 ================= */
let TF = null;

async function loadTrack(page = 1) {
  if (!TF) {
    if (!RULES) RULES = await api('/api/rules');
    TF = makeFilters($('#trackFilters'), [
      { type: 'text', label: '搜索 履约单/运单号', key: 'q', w: 230 },
      { type: 'select', label: '月份', key: 'month', options: META.months },
      { type: 'select', label: '仓库', key: 'wh', options: META.warehouses },
      { type: 'select', label: '物流商', key: 'cr', options: META.carriers },
      { type: 'select', label: '17track 官方状态', key: 'tstat',
        options: ['Delivered|已签收', 'InTransit|运输中', 'OutForDelivery|派送中', 'AvailableForPickup|待自提',
                  'DeliveryFailure|投递失败', 'Exception|异常', 'Expired|长时间未更新', 'InfoReceived|已收到信息',
                  'NotFound|查无此单'].map(s => { const [v, t] = s.split('|'); return { v, t }; }) },
      { type: 'select', label: '自定义物流异常', key: 'rule',
        options: (RULES.rules || []).map(r => ({ v: r.key, t: r.name })) },
    ], () => loadTrack(1));
  }
  const f = readF(TF);
  const d = await api('/api/track?' + qs(f, page, 50));
  TF._info.textContent = `共 ${fmt(d.total)} 单`;

  $('#trackChips').innerHTML =
    '<span class="chipstat">官方状态：' + (d.statusSummary || []).slice(0, 6)
      .map(x => `${esc(x.状态)} <b>${fmt(x.n)}</b>`).join('　') + '</span>' +
    '<span class="chipstat">自定义异常：' + (d.ruleSummary || []).slice(0, 6)
      .map(x => `${esc(x.异常)} <b>${fmt(x.n)}</b>`).join('　') + '</span>';

  $('#trackBody').innerHTML = d.rows.length ? d.rows.map(r => `
    <tr class="clickable" data-no="${esc(r.履约单号)}">
      <td class="mono">${esc(r.履约单号)}</td>
      <td class="mono">${esc(r.快递单号) || '—'}</td>
      <td>${esc(r.物流商)}</td>
      <td>${esc(r.目的国中文 || '—')}</td>
      <td>${r.轨迹状态 ? `<span class="badge blue">${esc(r.轨迹状态中文)}</span><div style="font-size:10px;color:#9aa5b4">${esc(r.轨迹子状态 || '')}</div>`
                        : (r.未注册 ? '<span class="badge gray">未注册</span>' : '<span class="badge gray">无轨迹</span>')}</td>
      <td>${r.主预警名称 ? `<span class="badge ${r.主预警级别 === '紧急' ? 'e' : r.主预警级别 === '高' ? 'h' : 'm'}">${esc(r.主预警名称)}</span>` : '—'}</td>
      <td style="color:#8b98ab;max-width:260px;overflow:hidden;text-overflow:ellipsis">${dt(r.最新事件时间)} ${esc(r.最新描述 || '')}</td>
      <td>${r.停滞天数 == null ? '—' : fmt1(r.停滞天数) + ' 天'}</td>
    </tr>`).join('') : `<tr><td colspan="8" class="empty">没有匹配的单</td></tr>`;

  $('#trackBody').querySelectorAll('tr.clickable').forEach(tr => tr.onclick = () => openOrder(tr.dataset.no));
  pager($('#trackPager'), d.total, d.page, d.size, p => loadTrack(p));
}

/* ================= 渠道时效 ================= */
let CF = null;

async function loadChannels() {
  if (!CF) CF = makeFilters($('#channelFilters'), [
    { type: 'select', label: '月份', key: 'month', options: META.months },
    { type: 'select', label: '仓库', key: 'wh', options: META.warehouses },
    { type: 'select', label: '平台', key: 'pf', options: META.platforms },
    { type: 'select', label: '国家', key: 'cn', options: META.countries },
  ], () => loadChannels());

  const d = await api('/api/channels?' + qs(readF(CF)));
  CF._info.textContent = `数据截止 ${d.asof}`;

  const rows = d.rows || [];
  const okN = rows.filter(r => r.verdict === '达标').length;
  const totalOrders = rows.reduce((a, r) => a + r.orders, 0);
  const overAll = rows.reduce((a, r) => a + (r.overdue || 0), 0);
  const inAll = rows.reduce((a, r) => a + (r.inTransit || 0), 0);
  $('#channelKpi').innerHTML = [
    ['渠道数', fmt(rows.length), '单量 ≥100'],
    ['达标渠道', `${okN} / ${rows.length}`, '6天达成率 + 平均时效同时满足'],
    ['覆盖单量', fmt(totalOrders), '参与统计'],
    ['在途超时', fmt(overAll), inAll ? `占在途 ${fmt1(100 * overAll / inAll)}%` : '—'],
  ].map(([l, v, s]) => `<div class="kpi"><div class="lbl">${l}</div><div class="val">${v}</div><div class="fl">${s}</div></div>`).join('');

  $('#channelBody').innerHTML = rows.length ? rows.map(r => {
    const cls = r.verdict === '达标' ? 'ok' : 'e';
    return `<tr>
      <td><b>${esc(r.channel)}</b></td>
      <td>${fmt(r.orders)}</td><td>${fmt(r.signed)}</td>
      <td>${fmt1(r.avgDays)} 天</td><td>${fmt1(r.p90Days)} 天</td>
      <td>${fmt1(r.rate3)}%</td><td>${fmt1(r.rate5)}%</td>
      <td><b>${fmt1(r.rate6)}%</b></td>
      <td>${r.target} 天 / ${fmt1(r.targets.d6)}%</td>
      <td>${fmt(r.inTransit)}</td>
      <td class="${r.overRate > 20 ? 'neg' : ''}">${fmt(r.overdue)}${r.overRate != null ? ` (${fmt1(r.overRate)}%)` : ''}</td>
      <td><span class="badge ${cls}">${r.verdict}</span></td>
    </tr>`;
  }).join('') : `<tr><td colspan="12" class="empty">没有满足条件的渠道</td></tr>`;

  $('#chanCountryBody').innerHTML = (d.byCountry || []).map(r => `
    <tr><td><b>${esc(r.ch)}</b></td><td>${esc(r.ctry || '—')}</td><td>${fmt(r.n)}</td>
    <td>${fmt1(r.avgDays)} 天</td><td>${fmt1(r.rate6)}%</td></tr>`).join('')
    || `<tr><td colspan="5" class="empty">无数据</td></tr>`;
}

/* ================= 数据与规则 ================= */
let loadedRules = false;

async function loadRules() {
  if (loadedRules) return;
  const d = await api('/api/rules');
  $('#ruleAsof').textContent = `数据截止 ${d.asof} · 工作日：${d.workdayRule}`;
  $('#ruleList').innerHTML = (d.rules || []).map(r => `
    <div class="rule">
      <div class="rh">
        <span class="badge ${r.level === '紧急' ? 'e' : r.level === '高' ? 'h' : 'm'}">${esc(r.level)}</span>
        <b>${esc(r.name)}</b>
        <span class="cat">${esc(r.category)}</span>
        <span class="sp"></span>
        <span class="hit">命中 ${fmt(r.hits)} 单${r.avgDays != null ? ` · 平均 ${fmt1(r.avgDays)} 天` : ''}</span>
      </div>
      <dl>
        <dt>触发条件</dt><dd>${esc(r.trigger)}</dd>
        <dt>判定依据</dt><dd>${esc(r.basis)}</dd>
        <dt>排除条件</dt><dd>${esc(r.exclude)}</dd>
        <dt>恢复方式</dt><dd>${esc(r.recovery)}</dd>
      </dl>
    </div>`).join('');

  $('#slaBody').innerHTML = (d.channels || []).map(r => {
    const ok = r.rate6 != null && r.rate6 >= r.targets.d6;
    return `<tr>
      <td><b>${esc(r.ch)}</b></td><td>${fmt(r.n)}</td>
      <td>${r.target} 天</td>
      <td>${fmt1(r.targets.d3)}%</td><td>${fmt1(r.targets.d5)}%</td><td>${fmt1(r.targets.d6)}%</td>
      <td><b>${fmt1(r.rate6)}%</b></td><td>${fmt1(r.avgDays)} 天</td>
      <td><span class="badge ${ok ? 'ok' : 'e'}">${ok ? '达标' : '未达标'}</span></td>
    </tr>`;
  }).join('') || `<tr><td colspan="9" class="empty">无数据</td></tr>`;

  $('#notImpl').innerHTML = (d.notImplemented || []).map(x =>
    `<div class="note" style="margin-top:6px"><b>${esc(x.name)}</b> — 需要 ${esc(x.need)}</div>`).join('');

  loadedRules = true;
}

/* ================= 详情抽屉 ================= */
async function openOrder(no) {
  const d = await api('/api/orders/' + encodeURIComponent(no));
  const o = d.order;
  $('#dTitle').textContent = o.履约单号;
  $('#dSub').textContent = `${o.平台} · ${o.仓库} · ${o.物流商} · ${o.目的国中文 || o.目的国 || ''}`;

  const kv = [
    ['出库单号', o.出库单号], ['快递单号', o.快递单号], ['状态', o.状态],
    ['创建', dt(o.创建时间)], ['签出', dt(o.签出时间)], ['签收', dt(o.签收时间)],
    ['件数', fmt(o.件数) + ' 件'], ['SKU 数', fmt(o.sku数)],
    ['仓内时长', o.仓内分钟 > 0 ? fmt1(o.仓内分钟 / 60) + ' 小时' : '—'],
    ['物流天数', o.物流分钟 > 0 ? fmt1(o.物流分钟 / 1440) + ' 天' : '—'],
    ['17track', o.轨迹状态中文 || (o.未注册 ? '未注册' : '无轨迹')],
    ['承运商签收', dt(o.承运商签收时间)],
  ];
  const lvl = l => l === '紧急' ? 'e' : l === '高' ? 'h' : 'm';
  const stBadge = s => {
    const m = { open: ['gray', '待处理'], claimed: ['blue', '已认领'], handling: ['purple', '处理中'], resolved: ['ok', '已解决'], archived: ['m', '已归档'] };
    const [c, t] = m[s] || ['gray', s]; return `<span class="badge ${c}">${t}</span>`;
  };

  const alerts = (d.alerts || []).map(a => `
    <div class="note" style="display:flex;gap:10px;align-items:center">
      <span class="badge ${lvl(a.level)}">${esc(a.level)}</span><b>${esc(a.rule_cn)}</b>
      <span style="color:#8b98ab">${esc(a.evidence || '')} · ${fmt1(a.days)} 天</span>
      <span style="margin-left:auto">${stBadge(a.state)}</span>
    </div>`).join('') || '<div class="note">该单没有命中预警</div>';

  const acts = (d.alerts || []).map(a => `
    <div class="actrow"><span>${esc(a.rule_cn)}</span>
      ${['claim|认领', 'handling|处理中', 'resolve|已解决', 'archive|归档', 'reopen|重开'].map(s => {
        const [k, lbl] = s.split('|');
        return `<button class="btn ghost sm" data-no="${esc(o.履约单号)}" data-rule="${esc(a.rule)}" data-act="${k}">${lbl}</button>`;
      }).join('')}</div>`).join('');

  const ms = (d.milestones || []).map(m =>
    `<div class="e"><div class="t">${dt(m.时间)}</div><div class="d">${esc(m.节点)}</div></div>`).join('')
    || '<div style="color:#8b98ab;font-size:12px">该单在 17track 中没有轨迹节点</div>';

  const skus = (d.skus || []).map(s =>
    `<tr><td class="mono">${esc(s.sku)}</td><td>${esc(s.商品名称) || '—'}</td><td>${fmt(s.数量)}</td><td>${esc(s.类目) || '—'}</td></tr>`).join('');

  const notes = (d.notes || []).map(n =>
    `<div class="note"><div style="font-size:11px;color:#8b98ab">${dt(n.ts)} · ${esc(n.actor)}</div>${esc(n.body)}</div>`).join('')
    || '<div style="color:#8b98ab;font-size:12px">暂无备注</div>';

  $('#dBody').innerHTML = `
    <div class="kv">${kv.map(([k, v]) => `<div class="i">${k}<b>${esc(v) || '—'}</b></div>`).join('')}</div>
    <div class="sect">业务预警</div>${alerts}${acts}
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
      } catch (e) { toast(e.message); b.disabled = false; }
    };
  });
  $('#mask').classList.add('on');
  $('#drawer').classList.add('on');
}

function closeDrawer() { $('#mask').classList.remove('on'); $('#drawer').classList.remove('on'); }

/* ================= 启动 ================= */
(async function boot() {
  try { ME = await api('/api/me'); } catch { return; }
  $('#who').textContent = `${ME.display_name} · ${ME.role_label}`;
  META = await api('/api/meta');
  RULES = await api('/api/rules');
  document.querySelectorAll('#nav button').forEach(b => b.onclick = () => showView(b.dataset.v));
  $('#dClose').onclick = closeDrawer;
  $('#mask').onclick = closeDrawer;
  $('#logout').onclick = async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; };
  const v = (location.hash || '').replace('#', '');
  if (v === 'rules') loadedRules = false;
  showView(VIEW_META[v] ? v : 'dash');
  window.addEventListener('resize', () => Object.values(charts).forEach(c => c && c.resize()));
})();
