import assert from "node:assert/strict";
import test from "node:test";
import {createHash} from "node:crypto";
import {gzipSync} from "node:zlib";
import worker from "../cloudflare/pages/_worker.js";
import relay from "../cloudflare/private-worker/index.js";

const site = "https://fulfillment-monitor.pages.dev";
const upstream = "https://fulfillment-origin.example";
const hub = "https://hub.example/oauth/authorize";
const shared = "test_".repeat(10);
const settings = { FULFILLMENT_ORIGIN_URL: upstream, FULFILLMENT_EDGE_KEY: shared, OPS_AUTHORIZE_URL: hub };

test("Pages health is distinct from application readiness", async () => {
  const health = await worker.fetch(new Request(site + "/__edge/health"), {});
  assert.deepEqual(await health.json(), { status: "edge_ready", originConfigured: false });
  assert.equal((await worker.fetch(new Request(site + "/"), {})).status, 503);
  assert.equal((await worker.fetch(new Request(site + "/preview"), settings)).status, 404);
  assert.equal((await worker.fetch(new Request(site + "/api/docs"), settings)).status, 404);
  const recursive = { ...settings, FULFILLMENT_ORIGIN_URL: site };
  assert.equal((await worker.fetch(new Request(site + "/"), recursive)).status, 503);
});

test("The proxy authenticates to a fixed origin and preserves site sessions", async (t) => {
  t.mock.method(globalThis, "fetch", async (url, request) => {
    assert.equal(String(url), upstream + "/api/logout");
    assert.equal(request.headers.get("user-agent"), "test");
    assert.equal(request.headers.get("x-fulfillment-edge-key"), shared);
    assert.equal(request.headers.get("cookie"), "test-session=opaque");
    assert.equal(request.headers.get("x-csrf-token"), "test-csrf");
    assert.equal(request.headers.get("x-forwarded-host"), null);
    assert.equal(request.redirect, "manual");
    return new Response(null, {status: 303, headers: {
      Location: upstream + "/login",
      "Set-Cookie": "test-session=; Path=/; HttpOnly; Secure",
    }});
  });
  const request = new Request(site + "/api/logout", {method: "POST", body: "{}", headers: {
    "x-fulfillment-edge-key": "forged", "x-forwarded-host": "outside.example", "user-agent": "test",
    cookie: "test-session=opaque", "x-csrf-token": "test-csrf",
  }});
  const response = await worker.fetch(request, settings);
  assert.equal(response.headers.get("Location"), "/login");
  assert.match(response.headers.get("Set-Cookie"), /Secure/);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
});

test("The OAuth endpoint permits only the configured identity provider", async (t) => {
  const proxy = t.mock.method(globalThis, "fetch", async () =>
    new Response(null, {status: 303, headers: {Location: hub + "?state=random"}}));
  const login = new Request(site + "/ops/auth/login");
  assert.equal((await worker.fetch(login, settings)).headers.get("Location"), hub + "?state=random");
  proxy.mock.mockImplementation(async () =>
    new Response(null, {status: 303, headers: {Location: "https://outside.example/"}}));
  assert.equal((await worker.fetch(login, settings)).status, 502);
});

test("Pages reaches the local port only via its private Worker binding", async () => {
  const origin = {
    FULFILLMENT_VPC_SERVICE: {
      async fetch(request) {
        assert.equal(request.url, "http://localhost:8770/api/me");
        assert.equal(request.headers.get("x-fulfillment-edge-key"), shared);
        return Response.json({ username: "test-operator" });
      },
    },
  };
  const pages = { FULFILLMENT_EDGE_KEY: shared,
    FULFILLMENT_PRIVATE_WORKER: { fetch: (url, options) => relay.fetch(new Request(url, options), origin) } };
  const response = await worker.fetch(new Request(site + "/api/me"), pages);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).username, "test-operator");
});

test("a visitor can open Pages and look up a complete reference without the tunnel", async () => {
  const now = "2026-11-20T08:00:00+00:00";
  const id = "P10001";
  const number = "A123456789";
  const values = new Map();
  const store = {
    async get(key, options) {
      const value = values.get(key);
      if (value === undefined) return null;
      return options?.type === "json" ? JSON.parse(value) : value;
    },
  };
  const current = {at: now, stats: "reports", refs: Array(64).fill(null), orders: Array(64).fill(null),
    events: Array(64).fill(null)};
  const indexFor = key => createHash("sha256").update(key).digest().readUInt16BE() % 1024;
  const pack = (kind, key, value) => {
    const index = indexFor(key);
    const part = index % 16;
    const head = Buffer.alloc(128);
    const data = gzipSync(JSON.stringify({[key]: value}));
    head.writeUInt32BE(128, 8 * part);
    head.writeUInt32BE(data.length, 8 * part + 4);
    const packed = Uint8Array.from(Buffer.concat([head, data])).buffer;
    current[kind][Math.floor(index / 16)] = kind;
    values.set("preview/v1/blocks/" + kind, packed);
  };
  pack("refs", "n:" + number, [id]);
  pack("orders", id, [{履约单号: id, 快递单号: number, 单号归一: number, 轨迹状态: "InTransit"}, 12, null]);
  pack("events", number, {n: {12: [["known-event", now, "2026-11-20 00:00:00 -08:00", "Carrier", "已揽收"]]}});
  values.set("preview/v1/blocks/reports", Uint8Array.from(gzipSync(JSON.stringify({
    meta: {range: {orders: 1}}, overview: {kpi: {orders: 1}},
    channels: {shipped: {rows: [{orders: 240}, {orders: 9}]}},
  }))).buffer);
  values.set("preview/v1/active.json", JSON.stringify({current, previous: null}));
  const pages = {
    FULFILLMENT_EDGE_PREVIEW: "true", FULFILLMENT_SNAPSHOT: store,
    ASSETS: {fetch: async () => new Response("<title>履约监控系统</title>")},
  };
  const get = (path, options) => worker.fetch(new Request(site + path, options), pages);
  assert.equal((await get("/")).status, 200);
  assert.equal((await (await get("/api/me")).json()).preview, true);
  assert.equal((await (await get("/api/meta")).json()).range.orders, 1);
  const filtered = await (await get("/api/overview?wh=TEST")).json();
  assert.equal(filtered.kpi.orders, 1);
  assert.equal(filtered.snapshotFiltersSkipped, true);
  assert.equal((await (await get("/api/channels")).json()).rows.length, 1);
  assert.equal((await (await get("/api/track")).json()).rows.length, 0);
  const query = "?q=" + number;
  const found = await (await get("/api/track" + query)).json();
  assert.equal(found.rows[0].履约单号, id);
  assert.equal(found.snapshotAt, now);
  const detail = await (await get("/api/orders/" + id + query)).json();
  assert.equal(detail.events[0].描述, "已揽收");
  assert.equal((await get("/api/orders/" + id)).status, 404);
  assert.equal((await get("/api/track" + query, {headers: {Cookie: "fd_session=admin"}})).status, 503);
  assert.equal((await get("/__edge/snapshot/manifest")).status, 403);
  assert.equal((await get("/__edge/snapshot/blocks/" + "f".repeat(64), {method: "PUT", body: "{}"})).status, 403);
  assert.equal((await get("/api/orders")).status, 503);
});
