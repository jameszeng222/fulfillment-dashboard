import assert from "node:assert/strict";
import test from "node:test";
import worker from "../cloudflare/pages/_worker.js";

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
    FULFILLMENT_EDGE_KEY: shared,
    FULFILLMENT_VPC_SERVICE: {
      async fetch(target, options) {
        assert.equal(target, "http://localhost:8770/api/me");
        assert.equal(options.headers.get("x-fulfillment-edge-key"), shared);
        return Response.json({ username: "test-operator" });
      },
    },
  };
  const pages = { FULFILLMENT_PRIVATE_WORKER: { fetch: request => worker.fetch(request, origin) } };
  const response = await worker.fetch(new Request(site + "/api/me"), pages);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).username, "test-operator");
});
