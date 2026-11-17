'use strict';
/**
 * 站点技能接入（需求 7）。
 *
 * 用同一个应用 Key GET 技能列表 → 按 sha256 下载校验技能包 → 加载技能说明 →
 * 实际使用后 POST /usage 上报（ids + 唯一 eventKey + engine）→ 授权变化时刷新。
 *
 * 安全：sha256 不匹配一律丢弃，绝不加载未校验的包。
 */

const crypto = require('crypto');
const fs = require('fs/promises');
const path = require('path');
const { timeoutFetch, describeUpstream } = require('./util');
const { unzip, looksLikeZip } = require('./zip');

const ENGINE = 'logistics-dashboard';
const MAX_PKG_BYTES = 32 * 1024 * 1024;

class SkillError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = 'SkillError';
    this.code = code;
    this.httpStatus = extra.httpStatus || 502;
    this.detail = extra.detail || '';
  }
}

const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

class Skills {
  constructor({ url, key, log, cacheDir }) {
    this.url = url;
    this.key = key;
    this.log = log;
    this.cacheDir = cacheDir;
    this.items = null;
    this.loadedAt = 0;
    this.instructions = new Map(); // id -> string
  }

  async #get(pathname = '', init = {}) {
    const res = await timeoutFetch(`${this.url}${pathname}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.key}`,
        Accept: 'application/json',
        ...(init.headers || {}),
      },
    }, 30000);
    const text = await res.text();
    return { res, text };
  }

  async list({ force = false, maxAgeMs = 10 * 60 * 1000 } = {}) {
    if (!force && this.items && Date.now() - this.loadedAt < maxAgeMs) return this.items;
    const { res, text } = await this.#get('');
    if (!res.ok) {
      const info = describeUpstream(res, text);
      if (res.status === 401 || res.status === 403) {
        throw new SkillError('SKILL_UNAUTHORIZED', '技能服务未授权。', { httpStatus: res.status, detail: info.detail });
      }
      throw new SkillError('SKILL_UPSTREAM', '获取技能列表失败。', { httpStatus: res.status, detail: info.detail });
    }
    let json = {};
    try {
      json = JSON.parse(text);
    } catch {
      throw new SkillError('SKILL_BAD_RESPONSE', '技能列表不是 JSON。', { detail: text.slice(0, 200) });
    }
    const arr = Array.isArray(json) ? json : (json.items || json.skills || json.data || []);
    this.items = arr.map(normalizeSkill).filter((s) => s.id);
    this.loadedAt = Date.now();
    return this.items;
  }

  /**
   * 下载 + 校验 + 落盘 + 读说明。
   * 说明来源优先级：列表里自带的 instructions/description > 包内 SKILL.md/README.md > 包内纯文本
   */
  async ensureLoaded(id, { force = false } = {}) {
    if (!force && this.instructions.has(id)) return this.instructions.get(id);

    const items = await this.list();
    const item = items.find((s) => s.id === id);
    if (!item) throw new SkillError('SKILL_NOT_FOUND', `未授权的技能：${id}`, { httpStatus: 404 });

    // 说明来源优先级：平台直接给的 instructions > 包内 SKILL.md > 列表里的 description
    let text = item.instructions || '';

    if (item.downloadPath) {
      const abs = new URL(item.downloadPath, this.url).toString();
      const res = await timeoutFetch(abs, {
        headers: { Authorization: `Bearer ${this.key}` },
      }, 60000);
      if (!res.ok) {
        const info = describeUpstream(res, await res.text());
        throw new SkillError('SKILL_DOWNLOAD', `下载技能包失败：${id}`, { httpStatus: res.status, detail: info.detail });
      }
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length > MAX_PKG_BYTES) throw new SkillError('SKILL_TOO_LARGE', '技能包体积超限。');

      // sha256 校验：不匹配直接丢弃
      if (item.sha256) {
        const got = sha256(buf);
        if (got.toLowerCase() !== String(item.sha256).toLowerCase()) {
          this.log.error(`skill ${id}: sha256 校验失败 expected=${item.sha256} got=${got}`);
          throw new SkillError('SKILL_HASH_MISMATCH', '技能包校验失败，已拒绝加载。');
        }
      }

      const dir = path.join(this.cacheDir, safeName(id));
      await fs.mkdir(dir, { recursive: true });
      await fs.writeFile(path.join(dir, 'package.bin'), buf);

      if (!text) text = await this.#readInstructionsFromPackage(buf, dir);
    }

    // 包里也没读到，才退回用列表里的描述，最后保底空串
    if (!text) text = item.description || '';

    this.instructions.set(id, text || '');
    return text || '';
  }

  async #readInstructionsFromPackage(buf, dir) {
    try {
      if (looksLikeZip(buf)) {
        const entries = unzip(buf);
        // 优先 SKILL.md，其次 README.md，再退到任意 .md
        const md = entries
          .map((e) => ({ ...e, base: path.basename(e.name).toLowerCase() }))
          .filter((e) => e.base.endsWith('.md'));
        const pick = md.find((e) => e.base === 'skill.md')
          || md.find((e) => e.base === 'readme.md')
          || md.find((e) => e.name.toLowerCase().includes('skill'))
          || md[0];
        if (!pick) return '';
        await fs.writeFile(path.join(dir, path.basename(pick.name) || 'SKILL.md'), pick.data);
        return pick.data.toString('utf8').slice(0, 200_000);
      }
      // 不是 zip：当纯文本处理
      const asText = buf.toString('utf8');
      if (/^[\x09\x0a\x0d\x20-\x7e\u4e00-\u9fa5]/.test(asText.slice(0, 200))) return asText.slice(0, 200_000);
      return '';
    } catch (e) {
      this.log.warn(`skill: 解析技能包失败 ${e.message}`);
      return '';
    }
  }

  /** 上报一次实际使用。eventKey 必须唯一（同一事件多次上报会被去重/拒绝） */
  async reportUsage(ids, { engine = ENGINE, eventKey } = {}) {
    if (!ids || !ids.length) return { skipped: true };
    const key = eventKey || `${engine}:${Date.now()}:${crypto.randomBytes(6).toString('hex')}`;
    const res = await timeoutFetch(`${this.url}/usage`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.key}`,
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify({ ids, eventKey: key, engine }),
    }, 20000);
    const text = await res.text();
    if (!res.ok) {
      const info = describeUpstream(res, text);
      this.log.warn(`skill usage 上报失败 HTTP ${res.status} ${info.detail}`);
      return { ok: false, status: res.status, detail: info.detail };
    }
    this.log.info(`skill usage 已上报 ids=${ids.join(',')} engine=${engine}`);
    return { ok: true, eventKey: key };
  }

  /** 授权变化后强制刷新，并丢掉已失效技能的缓存说明 */
  async refresh() {
    const items = await this.list({ force: true });
    const alive = new Set(items.map((s) => s.id));
    for (const id of [...this.instructions.keys()]) if (!alive.has(id)) this.instructions.delete(id);
    return items;
  }
}

function normalizeSkill(s) {
  if (typeof s === 'string') return { id: s, name: s };
  return {
    id: s.id || s.key || s.skill_id || s.name || '',
    name: s.name || s.title || s.id || '',
    description: s.description || s.summary || s.desc || '',
    instructions: s.instructions || s.content || '',
    downloadPath: s.downloadPath || s.download_path || s.package || '',
    sha256: s.sha256 || s.checksum || '',
    version: s.version || '',
  };
}

function safeName(id) {
  return String(id).replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 80) || 'skill';
}

module.exports = { Skills, SkillError, ENGINE, safeName };
