'use strict';
/**
 * 会话与一次性 state。
 *
 * state: 随机、单次使用、有时限，绑定当前浏览器会话(sid)。
 * session: 服务端存储，浏览器只拿一个不可猜的 sid。
 *
 * 说明：默认内存存储，进程重启即失效（用户需重新登录）。
 * 单实例站点足够；要持久化/多实例时把这两个 Map 换成 Redis 即可，接口不变。
 */

const crypto = require('crypto');

const now = () => Date.now();

function token(bytes = 32) {
  return crypto.randomBytes(bytes).toString('base64url');
}

/** 定长随机的恒定时间比较，避免时序侧信道 */
function safeEqual(a, b) {
  const ab = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

class Store {
  constructor(ttlSec) {
    this.ttl = ttlSec * 1000;
    this.map = new Map();
    this.timer = setInterval(() => this.sweep(), 60_000);
    this.timer.unref?.();
  }

  set(key, value) {
    this.map.set(key, { value, exp: now() + this.ttl });
    return key;
  }

  get(key) {
    const rec = this.map.get(key);
    if (!rec) return null;
    if (rec.exp <= now()) {
      this.map.delete(key);
      return null;
    }
    return rec.value;
  }

  take(key) {
    const v = this.get(key);
    if (v !== null) this.map.delete(key);
    return v;
  }

  delete(key) {
    this.map.delete(key);
  }

  sweep() {
    const t = now();
    for (const [k, rec] of this.map) if (rec.exp <= t) this.map.delete(k);
  }

  get size() {
    return this.map.size;
  }
}

class Sessions {
  constructor(cfg) {
    this.cfg = cfg;
    this.sessions = new Store(cfg.sessionTtlSec);
    this.states = new Store(cfg.stateTtlSec);
  }

  /** 发起登录：生成 state 并记下它属于哪个浏览器会话、登录后回哪里 */
  issueState({ sid, redirectTo }) {
    const state = token(32);
    this.states.set(state, { sid, redirectTo, issuedAt: now() });
    return state;
  }

  /**
   * 消费 state。返回值说明：
   *   null            -> 不存在或已过期（拒绝）
   *   {mismatch:true} -> state 存在但不属于当前浏览器（拒绝）
   */
  consumeState(state, sid) {
    if (!state) return null;
    const rec = this.states.take(state); // 单次使用：取走即删
    if (!rec) return null;
    if (rec.sid !== sid) return { mismatch: true };
    return rec;
  }

  createSession(user) {
    const sid = token(32);
    const csrf = token(24);
    this.sessions.set(sid, { user, csrf, createdAt: now(), lastSeen: now() });
    return { sid, csrf };
  }

  get(sid) {
    if (!sid) return null;
    const s = this.sessions.get(sid);
    if (s) s.lastSeen = now();
    return s;
  }

  destroy(sid) {
    if (sid) this.sessions.delete(sid);
  }

  get stats() {
    return { sessions: this.sessions.size, states: this.states.size };
  }
}

module.exports = { Sessions, Store, token, safeEqual };
