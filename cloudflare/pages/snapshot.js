const prefix = "preview/v1/";
const activeKey = prefix + "active.json";
const blockPrefix = prefix + "blocks/";
const slots = 1024;
const groupSize = 16;
const groups = slots / groupSize;
const directorySize = groupSize * 8;
const digestPattern = /^[0-9a-f]{64}$/;
const enc = new TextEncoder();
const mem = new WeakMap();
const dimensions = ["month", "week", "wh", "pf", "cr", "cn", "start", "end", "lastmile", "channel", "channel_group"];

function json(data, status = 200, headers = {}) {
  return Response.json(data, {status, headers: {"Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex", ...headers}});
}

async function hexDigest(bytes) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    byte => byte.toString(16).padStart(2, "0")).join("");
}

async function slot(key) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(key)));
  return ((digest[0] << 8) | digest[1]) % slots;
}

async function active(bucket) {
  const cached = mem.get(bucket);
  if (cached && cached.until > Date.now()) return cached.value;
  const record = await bucket.get(activeKey, {type: "json", cacheTtl: 30});
  if (!record) return null;
  mem.set(bucket, {until: Date.now() + 5_000, value: record});
  return record;
}

export async function snapshotStatus(bucket) {
  const record = bucket && await active(bucket);
  return {snapshotConfigured: Boolean(bucket), snapshotAt: record?.current?.at || null};
}

function content(bucket, hash) {
  return bucket.get(blockPrefix + hash, {type: "arrayBuffer", cacheTtl: 600});
}

async function chunk(bucket, hash, part, pending = new Map()) {
  if (!hash) return {};
  if (!pending.has(hash)) pending.set(hash, content(bucket, hash));
  const pack = await pending.get(hash);
  if (!pack) throw new Error("Incomplete snapshot");
  let bytes = pack;
  if (part !== undefined) {
    const view = new DataView(pack);
    const entry = (part % groupSize) * 8;
    const offset = view.getUint32(entry);
    const length = view.getUint32(entry + 4);
    if (!length) return {};
    if (offset < directorySize || offset + length > pack.byteLength) throw new Error("Invalid snapshot block");
    bytes = new Uint8Array(pack, offset, length);
  }
  return new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).json();
}

function lookupKeys(value) {
  const raw = value.normalize("NFKC").trim().toUpperCase();
  if (!/^[A-Z0-9#_.+ /-]{4,128}$/.test(raw)) return [];
  const n = raw.replace(/\s*\+\s*\d{1,3}\s*$/, "").replace(/[^A-Z0-9]/g, "");
  if (n.length < 4) return [];
  return ["r:" + raw, "n:" + n];
}

async function lookup(bucket, manifest, keys) {
  if (!keys.length) return [];
  const positions = await Promise.all(keys.map(slot));
  const pending = new Map();
  const blocks = await Promise.all(positions.map(index =>
    chunk(bucket, manifest.refs[Math.floor(index / groupSize)], index, pending)));
  // The export ranks matches by dispatch time. Append normalized-only matches.
  return [...new Set(keys.flatMap((key, i) => Object.hasOwn(blocks[i], key) ? blocks[i][key] : []))];
}

async function readOrders(bucket, manifest, ids) {
  const positions = await Promise.all(ids.map(slot));
  const unique = [...new Set(positions)];
  const blocks = new Map();
  const pending = new Map();
  for (let i = 0; i < unique.length; i += 4) {
    await Promise.all(unique.slice(i, i + 4).map(async index => blocks.set(index,
      await chunk(bucket, manifest.orders[Math.floor(index / groupSize)], index, pending))));
  }
  return ids.flatMap((id, i) => {
    const group = blocks.get(positions[i]);
    return Object.hasOwn(group, id) ? [group[id]] : [];
  });
}

const tableFields = new Set(["履约单号", "快递单号", "当前渠道", "物流商", "C端承运商", "仓库",
  "目的国中文", "平台", "轨迹状态", "轨迹状态中文", "轨迹子状态", "未注册", "回补状态", "已签收",
  "签出时间", "最新事件时间", "最新事件当地时间", "逐条事件数", "最新描述", "平台名称"]);

async function snapshotTrack(bucket, m, params) {
  const rawSize = Number(params.get("size") || 40);
  const page = Number(params.get("page") || 1);
  if (!Number.isSafeInteger(page) || page < 1 || !Number.isInteger(rawSize) || rawSize < 1 || rawSize > 200) {
    return json({detail: "无效分页参数"}, 422);
  }
  // Bound storage reads for references reused by several stores.
  const size = Math.min(40, rawSize);
  if (["1", "true", "on", "yes", "t", "y"].includes((params.get("resolved_only") || "").toLowerCase())) {
    return json({rows: [], total: 0, page, size, statusSummary: [], ruleSummary: [], snapshotAt: m.at,
      snapshotFiltersSkipped: true});
  }
  const offset = (page - 1) * size;
  const keys = lookupKeys(params.get("q") || "");
  const ids = await lookup(bucket, m, keys);
  const records = await readOrders(bucket, m, ids.slice(offset, offset + size));
  const rows = records.map(([order]) => Object.fromEntries(
    Object.entries(order).filter(([field]) => tableFields.has(field))));
  return json({rows, total: ids.length, page, size, statusSummary: [], ruleSummary: [], snapshotAt: m.at,
    snapshotFiltersSkipped: Boolean(params.get("week") || params.get("channel_group"))},
    200, !ids.length && keys.length ? {"X-Fulfillment-Snapshot-Miss": "1"} : {});
}

async function snapshotOrder(bucket, m, id, params) {
  const keys = lookupKeys(params.get("q") || "");
  const ids = await lookup(bucket, m, keys);
  if (!ids.includes(id)) return json({detail: "快照中没有匹配的履约单", snapshotAt: m.at}, 404,
    keys.length ? {"X-Fulfillment-Snapshot-Miss": "1"} : {});
  const [[order, nativeCarrier, archiveCarrier] = []] = await readOrders(bucket, m, [id]);
  if (!order) return json({detail: "快照中没有匹配的履约单", snapshotAt: m.at}, 404);
  const number = order.单号归一;
  const index = number ? await slot(number) : null;
  const eventBlock = number ? await chunk(bucket, m.events[Math.floor(index / groupSize)], index) : {};
  const grouped = Object.hasOwn(eventBlock, number) ? eventBlock[number] : {};
  const archived = grouped.a?.[archiveCarrier] || [];
  const native = [...(grouped.n?.[nativeCarrier || 0] || []), ...(grouped.n?.null || [])];
  const currentKeys = new Set(native.map(event => event[0]).filter(Boolean));
  const entries = [...archived.filter(event => !currentKeys.has(event[0])), ...native]
    .sort((a, b) => (a[1] || "~").localeCompare(b[1] || "~"));
  const events = entries.map(([, utc, local, provider, label]) =>
    ({事件时间: utc, 当地时间: local, 事件承运商: provider, 描述: label}));
  const anchor = entries.find(row => row[1] === order.承运商签收时间 && row[2]);
  if (!order.签收当地时间 && anchor) order.签收当地时间 = anchor[2];
  const recent = entries.find(row => row[1] === order.最新事件时间 && row[2]);
  if (!order.最新事件当地时间 && recent) order.最新事件当地时间 = recent[2];
  return json({order, events, milestones: grouped.m || [], snapshotAt: m.at,
    eventSource: archived.length ? (native.length ? "merged" : "archive") : (native.length ? "current" : null)});
}

export async function readSnapshot(bucket, url, fallback = false) {
  if (!bucket) return null;
  const path = url.pathname;
  const order = path.match(/^\/api\/orders\/([^/]+)$/);
  if (!(order || ["/api/track", "/api/meta", "/api/overview", "/api/channels"].includes(path))) return null;
  const filtered = ["/api/overview", "/api/channels"].includes(path) &&
    (dimensions.some(key => url.searchParams.get(key)) ||
      (url.searchParams.get("date_basis") || "shipped") !== "shipped");
  if (filtered && !fallback) return null;
  const record = await active(bucket);
  if (!record?.current) return null;
  try {
    return await readVersion(bucket, record.current, url, order, filtered);
  } catch (error) {
    if (!record.previous) throw error;
    return readVersion(bucket, record.previous, url, order, filtered);
  }
}

async function readVersion(bucket, m, url, order, filtered) {
  const path = url.pathname;
  if (path === "/api/track") return snapshotTrack(bucket, m, url.searchParams);
  if (order) return snapshotOrder(bucket, m, decodeURIComponent(order[1]), url.searchParams);
  const stats = await chunk(bucket, m.stats);
  if (path === "/api/meta") return json({...stats.meta, snapshotAt: m.at});
  const dateBasis = url.searchParams.get("date_basis") || "shipped";
  if (path === "/api/overview") {
    const report = stats.overview;
    return json({...report, snapshotAt: m.at, snapshotFiltersSkipped: filtered || dateBasis !== (report.dateBasis || "created")});
  }
  const timing = url.searchParams.get("basis") || "shipped";
  const basis = timing === "created" ? "paid" : timing;
  const dimension = url.searchParams.get("dimension") || "lastmile";
  const minimum = Number(url.searchParams.get("min_n") || 100);
  if (!["lastmile", "group"].includes(dimension) || !Number.isSafeInteger(minimum) || minimum < 1) {
    return json({detail: "无效渠道统计参数"}, 422);
  }
  const reports = dimension === "group" ? stats.channelGroups : stats.channels;
  if (!reports) return null;
  if (!Object.hasOwn(reports, basis)) return json({detail: "无效渠道统计参数"}, 422);
  const report = reports[basis];
  return json({...report, rows: report.rows.filter(row => row.orders >= minimum), snapshotAt: m.at,
    snapshotFiltersSkipped: filtered || dateBasis !== (report.dateBasis || "created")});
}

function validManifest(m) {
  return m && typeof m === "object" && Number.isFinite(Date.parse(m.at)) &&
    Number.isSafeInteger(m.ordersCount) && m.ordersCount > 0 &&
    digestPattern.test(m.stats) && ["refs", "orders", "events"].every(kind =>
      Array.isArray(m[kind]) && m[kind].length === groups &&
      m[kind].every(hash => hash === null || (typeof hash === "string" && digestPattern.test(hash))));
}

function blocksOf(m) {
  return m ? [m.stats, ...m.refs, ...m.orders, ...m.events].filter(Boolean) : [];
}

async function canPublish(request, key) {
  if (typeof key !== "string" || !/^[A-Za-z0-9_-]{43,128}$/.test(key)) return false;
  const supplied = request.headers.get("authorization") || "";
  if (!/^Bearer [0-9a-f]{64}$/.test(supplied)) return false;
  const expected = await hexDigest(enc.encode("fulfillment-snapshot-v1:" + key));
  let mismatch = 0;
  for (let i = 0; i < expected.length; i++) mismatch |= expected.charCodeAt(i) ^ supplied.charCodeAt(i + 7);
  return mismatch === 0;
}

export async function manageSnapshot(request, env, path) {
  if (!await canPublish(request, env.FULFILLMENT_EDGE_KEY)) return json({detail: "未授权"}, 403);
  const bucket = env.FULFILLMENT_SNAPSHOT;
  if (!bucket) return json({detail: "尚未绑定 Cloudflare Workers KV"}, 503);
  const manifest = path === "manifest";
  const block = path.match(/^blocks\/([0-9a-f]{64})$/);
  if (manifest && request.method === "GET") {
    const stored = await bucket.get(activeKey, {cacheTtl: 30});
    return stored ? json(JSON.parse(stored), 200, {ETag: '"' + await hexDigest(enc.encode(stored)) + '"'}) :
      json({current: null, previous: null});
  }
  if (path === "cleanup" && request.method === "POST") {
    const record = await bucket.get(activeKey, {type: "json", cacheTtl: 30});
    if (!record) return json({cursor: null, removed: 0});
    const {current, previous} = record;
    const live = new Set([...blocksOf(current), ...blocksOf(previous)].map(hash => blockPrefix + hash));
    const cursor = new URL(request.url).searchParams.get("cursor") || undefined;
    const listing = await bucket.list({prefix: blockPrefix, cursor, limit: 40});
    const before = Date.now() - 2 * 60 * 60 * 1000;
    const unused = listing.keys.filter(o => !live.has(o.name) && o.metadata?.created < before);
    for (let i = 0; i < unused.length; i += 5) {
      await Promise.all(unused.slice(i, i + 5).map(key => bucket.delete(key.name)));
    }
    return json({cursor: listing.list_complete ? null : listing.cursor, removed: unused.length});
  }
  if (request.method !== "PUT" || (!manifest && !block)) return json({detail: "未知的快照操作"}, 404);
  const length = Number(request.headers.get("content-length"));
  if (!Number.isInteger(length) || length < 1 || length > (manifest ? 50_000 : 6_000_000)) {
    return json({detail: "快照块超出大小限制"}, 413);
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength !== length) return json({detail: "快照数据长度不一致"}, 400);
  if (block) {
    if (await hexDigest(bytes) !== block[1]) return json({detail: "快照块校验失败"}, 422);
    await bucket.put(blockPrefix + block[1], bytes, {metadata: {created: Date.now()}});
    return json({ok: true}, 201);
  }
  let next;
  try { next = JSON.parse(new TextDecoder().decode(bytes)); }
  catch { return json({detail: "快照清单格式无效"}, 422); }
  if (!validManifest(next) || !await content(bucket, next.stats)) {
    return json({detail: "快照清单不完整"}, 422);
  }
  const prior = await bucket.get(activeKey, {cacheTtl: 30});
  const prev = prior ? JSON.parse(prior).current : null;
  const etag = prior ? '"' + await hexDigest(enc.encode(prior)) + '"' : null;
  if ((prior && request.headers.get("if-match") !== etag) ||
      (!prior && request.headers.get("if-none-match") !== "*") ||
      (prev && Date.parse(next.at) <= Date.parse(prev.at))) {
    return json({detail: "快照已由另一个同步任务更新"}, 409);
  }
  await bucket.put(activeKey, JSON.stringify({current: next, previous: prev}));
  mem.delete(bucket);
  return json({ok: true, snapshotAt: next.at}, 201);
}
