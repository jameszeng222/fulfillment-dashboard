'use strict';
/**
 * 极简 ZIP 读取器（只读，支持 store 与 deflate）。
 *
 * 为什么自己写：技能包是 zip，而 Node 没有内置解压。为了不给站点引入 npm 依赖，
 * 这里按 ZIP 规范解析中央目录，用 zlib 解压。只做"读出文件"这一件事：
 * 不支持加密、分卷、zip64（技能包不会这么大）。
 */

const zlib = require('zlib');

const EOCD_SIG = 0x06054b50;
const CEN_SIG = 0x02014b50;
const LOC_SIG = 0x04034b50;

function findEocd(buf) {
  // EOCD 至少 22 字节，注释最长 65535
  const min = Math.max(0, buf.length - 22 - 0xffff);
  for (let i = buf.length - 22; i >= min; i--) {
    if (buf.readUInt32LE(i) === EOCD_SIG) return i;
  }
  return -1;
}

/** 返回 [{ name, data, method, size }] */
function unzip(buffer, { maxEntries = 500, maxTotalBytes = 64 * 1024 * 1024 } = {}) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 22) throw new Error('不是有效的 zip');
  const eocd = findEocd(buffer);
  if (eocd < 0) throw new Error('找不到 zip 中央目录');

  const count = buffer.readUInt16LE(eocd + 10);
  const cenSize = buffer.readUInt32LE(eocd + 12);
  const cenOffset = buffer.readUInt32LE(eocd + 16);
  if (count === 0xffff || cenOffset === 0xffffffff) throw new Error('不支持 zip64');

  const out = [];
  let p = cenOffset;
  let total = 0;

  for (let i = 0; i < count && i < maxEntries; i++) {
    if (p + 46 > buffer.length || buffer.readUInt32LE(p) !== CEN_SIG) break;

    const method = buffer.readUInt16LE(p + 10);
    const compSize = buffer.readUInt32LE(p + 20);
    const uncompSize = buffer.readUInt32LE(p + 24);
    const nameLen = buffer.readUInt16LE(p + 28);
    const extraLen = buffer.readUInt16LE(p + 30);
    const commentLen = buffer.readUInt16LE(p + 32);
    const localOffset = buffer.readUInt32LE(p + 42);
    const name = buffer.slice(p + 46, p + 46 + nameLen).toString('utf8');

    p += 46 + nameLen + extraLen + commentLen;

    if (name.endsWith('/')) continue; // 目录项
    if (localOffset + 30 > buffer.length || buffer.readUInt32LE(localOffset) !== LOC_SIG) continue;

    const lNameLen = buffer.readUInt16LE(localOffset + 26);
    const lExtraLen = buffer.readUInt16LE(localOffset + 28);
    const dataStart = localOffset + 30 + lNameLen + lExtraLen;
    const dataEnd = dataStart + compSize;
    if (dataEnd > buffer.length) continue;

    const raw = buffer.slice(dataStart, dataEnd);
    let data;
    if (method === 0) {
      data = raw;
    } else if (method === 8) {
      try {
        data = zlib.inflateRawSync(raw, { maxOutputLength: maxTotalBytes });
      } catch {
        continue;
      }
    } else {
      continue; // 不支持的压缩方式（bzip2/lzma 等）
    }

    total += data.length;
    if (total > maxTotalBytes) throw new Error('解压后体积超限');

    out.push({ name, data, method, size: uncompSize });
  }

  return out;
}

/** 判断一段字节是不是 zip（本地文件头或空 zip 的 EOCD） */
function looksLikeZip(buf) {
  if (!Buffer.isBuffer(buf) || buf.length < 4) return false;
  return buf.readUInt32LE(0) === LOC_SIG || buf.readUInt32LE(0) === EOCD_SIG;
}

module.exports = { unzip, looksLikeZip };
