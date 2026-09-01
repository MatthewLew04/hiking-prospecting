/* gm-formats.js — browser / node port of pipelines/geomodel/formats/*.py
   (the interchange readers and writers of the geomodel toolkit).

   Same byte layouts, same edge cases and (where practical) the same warning
   texts as the Python reference, which is validated against GDAL, pyarrow,
   omf-rust, ezdxf, segyio and lasio.  Objects are the gm-core classes
   (Grid2D node-registered / south row first, Mesh, LineSet, PointSet,
   BlockModel i-fastest, Drillholes dip-positive-down, ImagePlane, Project).

   Sections (search for "=====" banners):
     helpers      bytes, Python-compatible text/number parsing + formatting
     compression  gzip / zlib via (De)CompressionStream, CRC-32, PNG encoder
     zip          STORED-only writer with archive comment, zip64-aware reader
     thrift       compact protocol (Parquet footers / page headers)
     parquet      single-table Parquet reader + writer (omf-rust schemas)
     surfer       DSAA / DSBB / DSRB grids, BLN polylines
     geosoft      binary GRD (v2, compressed read), GXF (base-90 read), XYZ
     arcascii     Arc/Info ASCII grid
     zmap         ZMAP+ grid
     irap         Irap classic ASCII grid
     cps3         CPS-3 ASCII grid (read only)
     ubc          UBC-GIF mesh + model
     obj          Wavefront OBJ
     dxf          DXF R12 writer, tolerant reader
     gocad        GOCAD TSurf / PLine / VSet
     lfmsh        Leapfrog binary mesh
     tables       RFC-4180 CSV + points / drillholes / structural / block model
     segy         SEG-Y rev 0/1/2 + EBCDIC + IBM floats + section image
     las          CWLS LAS 1.2 / 2.0 (3.0 best effort)
     omf          OMF v0.9 and v2.0 (shared record mapping)
     registry     FORMATS, sniff(), readAny(), writeAs()

   Runtime: modern browsers (Chrome / Firefox / Safari 17+) and node >= 22.
   Only web-standard APIs are used (TypedArrays, DataView, TextEncoder/Decoder,
   CompressionStream 'gzip' / 'deflate' / 'deflate-raw', crypto.randomUUID).
   No dependencies: the OMF v2 ZIP container is handled by the small built-in
   STORED writer / zip64-aware reader below, so JSZip is not required.

   Conventions: readers take bytes (Uint8Array / ArrayBuffer) or text and an
   opts object ({file: 'name.ext'} names the object like the Python path stem);
   writers return Uint8Array.  Readers that inflate something (Geosoft GRD,
   OMF) are async; readAny() / writeAs() are always async.  PointSet numeric
   columns use null (not NaN) for missing values because gm-core's
   PointSet.isNumeric() treats NaN as text; Mesh / LineSet attribute values are
   Float32Array as in gm-core. */

import * as GM from './gm-core.js';

export const VERSION = '1.0.0';
export const APPLICATION = 'nw-mineral-monitor geomodel ' + GM.VERSION;

/* ========================================================================
   helpers
   ======================================================================== */
const ENC = new TextEncoder();
const NAN = NaN;

/** Anything byte-like -> Uint8Array (string = UTF-8). */
export function toU8(src) {
  if (src instanceof Uint8Array) return src;
  if (src instanceof ArrayBuffer) return new Uint8Array(src);
  if (ArrayBuffer.isView(src)) return new Uint8Array(src.buffer, src.byteOffset, src.byteLength);
  if (typeof src === 'string') return ENC.encode(src);
  if (src && typeof src.length === 'number') return Uint8Array.from(src);
  throw new TypeError('expected Uint8Array, ArrayBuffer or string');
}

export function concatU8(chunks) {
  let total = 0;
  for (const c of chunks) total += c.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

function bytesEq(a, off, str) {
  if (off + str.length > a.length) return false;
  for (let i = 0; i < str.length; i++) if (a[off + i] !== str.charCodeAt(i)) return false;
  return true;
}

function findBytes(hay, needle, from = 0) {
  const n = needle.length;
  outer: for (let i = from; i + n <= hay.length; i++) {
    for (let k = 0; k < n; k++) if (hay[i + k] !== needle[k]) continue outer;
    return i;
  }
  return -1;
}

/** Growable little/big-endian byte buffer. */
class ByteWriter {
  constructor(cap = 4096) { this.buf = new Uint8Array(cap); this.dv = new DataView(this.buf.buffer); this.pos = 0; }
  ensure(n) {
    if (this.pos + n <= this.buf.length) return;
    let c = this.buf.length * 2;
    while (c < this.pos + n) c *= 2;
    const nb = new Uint8Array(c);
    nb.set(this.buf.subarray(0, this.pos));
    this.buf = nb; this.dv = new DataView(nb.buffer);
  }
  get length() { return this.pos; }
  u8(v) { this.ensure(1); this.buf[this.pos++] = v & 0xff; return this; }
  i8(v) { this.ensure(1); this.dv.setInt8(this.pos, v); this.pos += 1; return this; }
  u16(v, le = true) { this.ensure(2); this.dv.setUint16(this.pos, v, le); this.pos += 2; return this; }
  i16(v, le = true) { this.ensure(2); this.dv.setInt16(this.pos, v, le); this.pos += 2; return this; }
  u32(v, le = true) { this.ensure(4); this.dv.setUint32(this.pos, v >>> 0, le); this.pos += 4; return this; }
  i32(v, le = true) { this.ensure(4); this.dv.setInt32(this.pos, v, le); this.pos += 4; return this; }
  u64(v, le = true) { this.ensure(8); this.dv.setBigUint64(this.pos, BigInt.asUintN(64, BigInt(v)), le); this.pos += 8; return this; }
  i64(v, le = true) { this.ensure(8); this.dv.setBigInt64(this.pos, BigInt.asIntN(64, BigInt(v)), le); this.pos += 8; return this; }
  f32(v, le = true) { this.ensure(4); this.dv.setFloat32(this.pos, v, le); this.pos += 4; return this; }
  f64(v, le = true) { this.ensure(8); this.dv.setFloat64(this.pos, v, le); this.pos += 8; return this; }
  bytes(u8) { this.ensure(u8.length); this.buf.set(u8, this.pos); this.pos += u8.length; return this; }
  zeros(n) { this.ensure(n); this.buf.fill(0, this.pos, this.pos + n); this.pos += n; return this; }
  /** Write at an absolute position without moving the cursor. */
  patchU32(at, v, le = true) { this.dv.setUint32(at, v >>> 0, le); }
  result() { return this.buf.slice(0, this.pos); }
}

/* ---------------------------------------------------------- text decoding */
/** Exact ISO-8859-1 (Python 'latin-1'): byte n -> U+00nn. */
export function decodeLatin1(bytes) {
  bytes = toU8(bytes);
  let out = '';
  const CH = 8192;
  for (let i = 0; i < bytes.length; i += CH) out += String.fromCharCode.apply(null, bytes.subarray(i, Math.min(bytes.length, i + CH)));
  return out;
}

/** Python ``_decode``: utf-8, then cp1252, then latin-1 (with replacement). */
export function decodeText(bytes, stripBom = false) {
  bytes = toU8(bytes);
  if (stripBom && bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) bytes = bytes.subarray(3);
  try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch (e) { /* not utf-8 */ }
  try { return new TextDecoder('windows-1252', { fatal: true }).decode(bytes); } catch (e) { /* fall through */ }
  return decodeLatin1(bytes);
}

function encodeAscii(text, replacement = '?') {
  const out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) { const c = text.charCodeAt(i); out[i] = c < 128 ? c : replacement.charCodeAt(0); }
  return out;
}
function encodeLatin1(text, replacement = '?') {
  const out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) { const c = text.charCodeAt(i); out[i] = c < 256 ? c : replacement.charCodeAt(0); }
  return out;
}
function utf8(text) { return ENC.encode(text); }

/* ------------------------------------------------ Python string semantics */
// str.split() / str.strip() whitespace (ASCII + the C1 / Unicode spaces Python counts)
const WS_CLASS = '[\\s\\x1c-\\x1f\\x85]';
const RE_WS_RUN = new RegExp(WS_CLASS + '+', 'g');
const RE_STRIP = new RegExp('^' + WS_CLASS + '+|' + WS_CLASS + '+$', 'g');
const RE_LSTRIP = new RegExp('^' + WS_CLASS + '+');
const RE_RSTRIP = new RegExp(WS_CLASS + '+$');
const RE_LINEBREAK = /\r\n|\r|\n|\x0b|\x0c|\x1c|\x1d|\x1e|\x85|\u2028|\u2029/;

export function pyStrip(s) {
  const n = s.length;
  if (!n) return s;
  const a = s.charCodeAt(0), b = s.charCodeAt(n - 1);
  if (a > 32 && b > 32 && a !== 0x85 && b !== 0x85 && a !== 0xa0 && b !== 0xa0 && a < 0x1680 && b < 0x1680) return s;   // fast path: nothing to strip
  return s.replace(RE_STRIP, '');
}
function pyLstrip(s) { return s.replace(RE_LSTRIP, ''); }
function pyRstrip(s) { return s.replace(RE_RSTRIP, ''); }
/** str.split() with no arguments. */
export function pySplit(s) { const t = pyStrip(s); return t ? t.split(RE_WS_RUN) : []; }
/** str.splitlines() (no trailing empty element). */
export function pySplitlines(text) {
  if (!text) return [];
  const parts = text.split(RE_LINEBREAK);
  if (parts.length && parts[parts.length - 1] === '' ) parts.pop();
  return parts;
}

/* ------------------------------------------------------- number parsing */
const RE_FLOAT = /^[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|inf(?:inity)?|nan)$/i;
const RE_FLOAT_US = /^[+-]?(?:\d+(?:_\d+)*)?(?:\.(?:\d+(?:_\d+)*)?)?(?:[eE][+-]?\d+(?:_\d+)*)?$/;
/** Python float(str): number, or undefined where Python raises ValueError. */
export function pyFloat(s) {
  if (typeof s === 'number') return s;
  if (typeof s === 'boolean') return s ? 1 : 0;
  if (s == null) return undefined;
  let t = pyStrip(String(s));
  // fast path: plain decimal / exponent text (digits . + - e E only) parses the same way in JS and Python
  let simple = t.length > 0, digit = false;
  for (let i = 0; i < t.length; i++) {
    const c = t.charCodeAt(i);
    if (c >= 48 && c <= 57) digit = true;
    else if (!(c === 46 || c === 43 || c === 45 || c === 101 || c === 69)) { simple = false; break; }
  }
  if (simple && digit) { const v = Number(t); return v !== v ? undefined : v; }
  if (t.indexOf('_') >= 0) {
    if (!RE_FLOAT_US.test(t)) return undefined;
    t = t.replace(/_/g, '');
  }
  if (!RE_FLOAT.test(t)) return undefined;
  const low = t.toLowerCase().replace(/^[+-]/, '');
  if (low === 'nan') return NAN;
  if (low.charCodeAt(0) === 105) return t.charCodeAt(0) === 45 ? -Infinity : Infinity;   // 'inf'
  return parseFloat(t);
}
/** Python int(str): integer, or undefined where Python raises ValueError. */
export function pyInt(s) {
  if (typeof s === 'number') return Math.trunc(s);
  if (s == null) return undefined;
  const t = pyStrip(String(s)).replace(/_/g, '');
  if (!/^[+-]?\d+$/.test(t)) return undefined;
  return parseInt(t, 10);
}
/** Python round(): half to even. */
export function pyRound(v) {
  if (!isFinite(v)) return v;
  const f = Math.floor(v), d = v - f;
  if (d === 0.5) return (f % 2 === 0) ? f : f + 1;
  return Math.round(v);
}
/** math.fsum: exactly rounded sum (Shewchuk partials). */
export function fsum(values) {
  const partials = [];
  for (let x of values) {
    if (x !== x) return NAN;
    let i = 0;
    for (let y of partials) {
      if (Math.abs(x) < Math.abs(y)) { const t = x; x = y; y = t; }
      const hi = x + y, lo = y - (hi - x);
      if (lo) partials[i++] = lo;
      x = hi;
    }
    partials.length = i;
    partials.push(x);
  }
  let total = 0;
  for (const p of partials) total += p;
  return total;
}

/* ----------------------------------------------------- number formatting */
/** Python repr(float): shortest round-trip digits, fixed when -4 < decpt <= 16. */
export function pyRepr(v) {
  v = +v;
  if (v !== v) return 'nan';
  if (v === Infinity) return 'inf';
  if (v === -Infinity) return '-inf';
  if (v === 0) return (1 / v < 0) ? '-0.0' : '0.0';
  const sign = v < 0 ? '-' : '';
  const av = Math.abs(v);
  const s = String(av);                             // shortest round-trip in JS too
  if (av >= 1e-4 && av < 1e16 && s.indexOf('e') < 0) return sign + (s.indexOf('.') < 0 ? s + '.0' : s);   // fast path: same fixed layout
  const m = /^(\d+)(?:\.(\d+))?(?:e([+-]\d+))?$/.exec(s);
  if (!m) return s;
  const intPart = m[1], frac = m[2] || '', exp = m[3] ? parseInt(m[3], 10) : 0;
  let digits = intPart + frac;
  let decpt = intPart.length + exp;                 // value = 0.digits * 10^decpt
  const lead = digits.length - digits.replace(/^0+/, '').length;
  digits = digits.slice(lead);
  decpt -= lead;
  digits = digits.replace(/0+$/, '') || '0';
  if (decpt <= -4 || decpt > 16) {
    const e = decpt - 1;
    const mant = digits[0] + (digits.length > 1 ? '.' + digits.slice(1) : '');
    return sign + mant + 'e' + (e < 0 ? '-' : '+') + String(Math.abs(e)).padStart(2, '0');
  }
  if (decpt <= 0) return sign + '0.' + '0'.repeat(-decpt) + digits;
  if (decpt >= digits.length) return sign + digits + '0'.repeat(decpt - digits.length) + '.0';
  return sign + digits.slice(0, decpt) + '.' + digits.slice(decpt);
}
/** Python repr(str): single quotes unless the text holds a single quote and no double quote. */
export function pyReprStr(s) {
  s = String(s);
  const q = (s.indexOf("'") >= 0 && s.indexOf('"') < 0) ? '"' : "'";
  let out = '';
  for (const ch of s) {
    if (ch === '\\') out += '\\\\';
    else if (ch === q) out += '\\' + q;
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else { const c = ch.codePointAt(0); out += (c < 32 || c === 127) ? '\\x' + c.toString(16).padStart(2, '0') : ch; }
  }
  return q + out + q;
}
/** repr(float) with a trailing '.0' removed (the obj / gocad / csv style). */
function reprShort(v) { const r = pyRepr(v); return r.endsWith('.0') ? r.slice(0, -2) : r; }
/** C printf %.<prec>g */
export function fmtG(v, prec = 6) {
  v = +v;
  if (v !== v) return 'nan';
  if (v === Infinity) return 'inf';
  if (v === -Infinity) return '-inf';
  if (prec === 0) prec = 1;
  if (v === 0) return (1 / v < 0) ? '-0' : '0';
  const e = v.toExponential(prec - 1);
  const m = /^(-?)(\d)(?:\.(\d+))?e([+-]\d+)$/.exec(e);
  const X = parseInt(m[4], 10);
  if (X < -4 || X >= prec) {
    let mant = m[2] + (m[3] ? '.' + m[3].replace(/0+$/, '') : '');
    if (mant.endsWith('.')) mant = mant.slice(0, -1);
    return m[1] + mant + 'e' + (X < 0 ? '-' : '+') + String(Math.abs(X)).padStart(2, '0');
  }
  let f = v.toFixed(Math.max(0, prec - 1 - X));
  if (f.indexOf('.') >= 0) f = f.replace(/0+$/, '').replace(/\.$/, '');
  return f;
}
/** C printf %.<d>f with round-half-even at exact binary ties (Python behaviour). */
export function pyFixed(v, d) {
  v = +v;
  if (!isFinite(v)) return v !== v ? 'nan' : (v < 0 ? '-inf' : 'inf');
  const neg = v < 0 || (v === 0 && 1 / v < 0);
  const a = Math.abs(v);
  let s;
  if (a >= 1e21) s = BigInt(a).toString() + (d > 0 ? '.' + '0'.repeat(d) : '');
  else if (d > 60) s = a.toFixed(d);
  else {
    const exact = a.toFixed(100);                   // exact expansion for |v| >= ~1e-23
    const dot = exact.indexOf('.');
    const frac = exact.slice(dot + 1);
    if (frac.length > d && frac[d] === '5' && /^0*$/.test(frac.slice(d + 1))) {
      let kept = exact.slice(0, dot) + frac.slice(0, d);
      if ((kept.charCodeAt(kept.length - 1) - 48) % 2 === 1) kept = incDecimal(kept);
      s = d > 0 ? kept.slice(0, kept.length - d) + '.' + kept.slice(kept.length - d) : kept;
    } else s = a.toFixed(d);
  }
  return (neg ? '-' : '') + s;
}
function incDecimal(digits) {
  const arr = digits.split('');
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] === '9') arr[i] = '0';
    else { arr[i] = String.fromCharCode(arr[i].charCodeAt(0) + 1); return arr.join(''); }
  }
  return '1' + arr.join('');
}
/** C printf %.<d>e (two-digit exponent minimum). */
export function pyExp(v, d) {
  v = +v;
  if (!isFinite(v)) return v !== v ? 'nan' : (v < 0 ? '-inf' : 'inf');
  const neg = v < 0 || (v === 0 && 1 / v < 0);
  const s = Math.abs(v).toExponential(d).replace(/e([+-])(\d)$/, 'e$10$2');
  return (neg ? '-' : '') + s;
}
function padLeft(s, w) { s = String(s); return s.length >= w ? s : ' '.repeat(w - s.length) + s; }
function padRight(s, w) { s = String(s); return s.length >= w ? s : s + ' '.repeat(w - s.length); }

/* ------------------------------------------------------------ misc model */
function isNaNv(v) { return v === null || v === undefined || (typeof v === 'number' && v !== v); }
function nanToNull(arr) { const out = new Array(arr.length); for (let i = 0; i < arr.length; i++) { const v = arr[i]; out[i] = (v == null || v !== v) ? null : v; } return out; }
function stem(name, fallback) {
  if (!name) return fallback;
  const base = String(name).split(/[\\/]/).pop();
  const k = base.lastIndexOf('.');
  return (k > 0 ? base.slice(0, k) : base) || fallback;
}
function setProvenance(obj, format, file, extra) {
  obj.provenance = Object.assign({ format }, extra || {});
  if (file) obj.provenance.file = file;
  return obj;
}
function finishWarnings(obj, warnings) { obj.metadata.warnings = warnings; return obj; }
function uuid4() {
  const c = globalThis.crypto;
  if (c && c.randomUUID) return c.randomUUID();
  const b = new Uint8Array(16);
  if (c && c.getRandomValues) c.getRandomValues(b); else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
function uuidBytes(s) { const h = s.replace(/-/g, ''); const b = new Uint8Array(16); for (let i = 0; i < 16; i++) b[i] = parseInt(h.slice(2 * i, 2 * i + 2), 16); return b; }
function uuidFromBytes(b) { const h = Array.from(b, x => x.toString(16).padStart(2, '0')).join(''); return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`; }

export class FormatError extends Error {}

/* ========================================================================
   compression, CRC-32, PNG
   ======================================================================== */
/* Pump bytes through a (De)CompressionStream; the read loop runs before the
   write so back-pressure cannot dead-lock.  In lenient mode whatever was
   decoded before an error (trailing junk after a zlib stream) is returned. */
async function pump(stream, u8, lenient = false) {
  const writer = stream.writable.getWriter();
  const reader = stream.readable.getReader();
  const chunks = [];
  let readErr = null, writeErr = null;
  const reading = (async () => {
    try { for (;;) { const { value, done } = await reader.read(); if (done) break; chunks.push(value); } }
    catch (e) { readErr = e; }
  })();
  try {
    const STEP = 1 << 20;
    for (let i = 0; i < u8.length; i += STEP) await writer.write(u8.subarray(i, Math.min(u8.length, i + STEP)));
    await writer.close();
  } catch (e) { writeErr = e; }
  await reading;
  const err = readErr || writeErr;
  if (err && !(lenient && chunks.length)) throw err;
  return { data: concatU8(chunks), complete: !err };
}
export async function gzip(u8) {
  const out = (await pump(new CompressionStream('gzip'), toU8(u8))).data;
  if (out.length > 9) { out[9] = 255; }          // OS = unknown, like python's gzip.compress
  return out;
}
export async function gunzip(u8) { return (await pump(new DecompressionStream('gzip'), toU8(u8))).data; }
/** zlib-wrapped deflate (RFC 1950) = python zlib.compress */
export async function deflate(u8) { return (await pump(new CompressionStream('deflate'), toU8(u8))).data; }
export async function inflate(u8) { return (await pump(new DecompressionStream('deflate'), toU8(u8))).data; }
async function inflateLenient(u8) { return pump(new DecompressionStream('deflate'), toU8(u8), true); }
async function inflateRaw(u8) { return (await pump(new DecompressionStream('deflate-raw'), toU8(u8))).data; }

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return t;
})();
export function crc32(bytes, crc = 0) {
  crc = (crc ^ 0xffffffff) >>> 0;
  for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/** Minimal PNG encoder: 8-bit, channels 1 (grey) / 2 (grey+alpha) / 3 (RGB) / 4 (RGBA), filter 0. */
export async function encodePNG(width, height, data, opts = {}) {
  const channels = opts.channels || (data.length === width * height ? 1 : (data.length === width * height * 3 ? 3 : 4));
  const colorType = { 1: 0, 2: 4, 3: 2, 4: 6 }[channels];
  if (colorType === undefined) throw new FormatError('PNG: channels must be 1..4');
  const stride = width * channels;
  if (data.length < stride * height) throw new FormatError('PNG: pixel buffer too small');
  const raw = new Uint8Array((stride + 1) * height);
  for (let y = 0; y < height; y++) { raw[y * (stride + 1)] = 0; raw.set(data.subarray(y * stride, (y + 1) * stride), y * (stride + 1) + 1); }
  const idat = await deflate(raw);
  const w = new ByteWriter(idat.length + 64);
  w.bytes(Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a));
  const chunk = (type, body) => {
    const tb = encodeAscii(type);
    w.u32(body.length, false); w.bytes(tb); w.bytes(body);
    w.u32(crc32(body, crc32(tb)), false);
  };
  const ihdr = new ByteWriter(13);
  ihdr.u32(width, false).u32(height, false).u8(8).u8(colorType).u8(0).u8(0).u8(0);
  chunk('IHDR', ihdr.result());
  chunk('IDAT', idat);
  chunk('IEND', new Uint8Array(0));
  return w.result();
}
export function pngDataUrl(pngBytes) { return 'data:image/png;base64,' + GM.b64encode(pngBytes); }

/* ========================================================================
   zip (OMF v2 container).  Writer: STORED members + archive comment.
   Reader: central directory with zip64 extras, STORED and DEFLATE members.
   ======================================================================== */
const ZIP_LOCAL = 0x04034b50, ZIP_CENTRAL = 0x02014b50, ZIP_EOCD = 0x06054b50, ZIP64_EOCD = 0x06064b50, ZIP64_LOC = 0x07064b50;

export class ZipWriter {
  constructor() { this.w = new ByteWriter(1 << 16); this.entries = []; }
  /** Add a STORED member (DOS date 1980-01-01, unix mode 0644). */
  add(name, data) {
    data = toU8(data);
    const nameB = utf8(name);
    if (data.length > 0xfffffffe) throw new FormatError('zip: member too large for a zip32 archive');
    const crc = crc32(data);
    const offset = this.w.length;
    const w = this.w;
    w.u32(ZIP_LOCAL).u16(20).u16(0).u16(0).u16(0).u16(0x21).u32(crc).u32(data.length).u32(data.length).u16(nameB.length).u16(0);
    w.bytes(nameB).bytes(data);
    this.entries.push({ nameB, crc, size: data.length, offset });
    return name;
  }
  finish(comment = '') {
    const w = this.w;
    const cdStart = w.length;
    for (const e of this.entries) {
      w.u32(ZIP_CENTRAL).u16(0x0314).u16(20).u16(0).u16(0).u16(0).u16(0x21).u32(e.crc).u32(e.size).u32(e.size)
        .u16(e.nameB.length).u16(0).u16(0).u16(0).u16(0).u32(0x81a40000).u32(e.offset).bytes(e.nameB);
    }
    const cdSize = w.length - cdStart;
    const cb = utf8(comment);
    w.u32(ZIP_EOCD).u16(0).u16(0).u16(this.entries.length).u16(this.entries.length).u32(cdSize).u32(cdStart).u16(cb.length).bytes(cb);
    return w.result();
  }
}

export class ZipReader {
  constructor(bytes) {
    this.data = toU8(bytes);
    this.entries = new Map();
    this.comment = '';
    this._parse();
  }
  _parse() {
    const d = this.data, dv = new DataView(d.buffer, d.byteOffset, d.byteLength);
    if (d.length < 22) throw new FormatError('zip: file too short');
    let eocd = -1;
    for (let i = d.length - 22; i >= Math.max(0, d.length - 22 - 65535); i--) {
      if (dv.getUint32(i, true) === ZIP_EOCD && i + 22 + dv.getUint16(i + 20, true) <= d.length) { eocd = i; break; }
    }
    if (eocd < 0) throw new FormatError('zip: end of central directory not found');
    let nEntries = dv.getUint16(eocd + 10, true);
    let cdSize = dv.getUint32(eocd + 12, true);
    let cdOff = dv.getUint32(eocd + 16, true);
    const cl = dv.getUint16(eocd + 20, true);
    this.comment = decodeText(d.subarray(eocd + 22, eocd + 22 + cl));
    this.commentBytes = d.slice(eocd + 22, eocd + 22 + cl);
    if ((nEntries === 0xffff || cdSize === 0xffffffff || cdOff === 0xffffffff) && eocd >= 20 && dv.getUint32(eocd - 20, true) === ZIP64_LOC) {
      const z64 = Number(dv.getBigUint64(eocd - 20 + 8, true));
      if (z64 + 56 <= d.length && dv.getUint32(z64, true) === ZIP64_EOCD) {
        nEntries = Number(dv.getBigUint64(z64 + 32, true));
        cdSize = Number(dv.getBigUint64(z64 + 40, true));
        cdOff = Number(dv.getBigUint64(z64 + 48, true));
      }
    }
    let p = cdOff;
    for (let k = 0; k < nEntries; k++) {
      if (p + 46 > d.length || dv.getUint32(p, true) !== ZIP_CENTRAL) throw new FormatError('zip: bad central directory entry');
      const method = dv.getUint16(p + 10, true);
      const crc = dv.getUint32(p + 16, true);
      let csize = dv.getUint32(p + 20, true), usize = dv.getUint32(p + 24, true);
      const nl = dv.getUint16(p + 28, true), el = dv.getUint16(p + 30, true), cl2 = dv.getUint16(p + 32, true);
      let off = dv.getUint32(p + 42, true);
      const name = decodeText(d.subarray(p + 46, p + 46 + nl));
      // zip64 extended information extra field (id 1): only the 0xffffffff fields are present, in order
      let q = p + 46 + nl;
      const qEnd = q + el;
      while (q + 4 <= qEnd) {
        const id = dv.getUint16(q, true), sz = dv.getUint16(q + 2, true);
        if (id === 1) {
          let r = q + 4;
          if (usize === 0xffffffff && r + 8 <= q + 4 + sz) { usize = Number(dv.getBigUint64(r, true)); r += 8; }
          if (csize === 0xffffffff && r + 8 <= q + 4 + sz) { csize = Number(dv.getBigUint64(r, true)); r += 8; }
          if (off === 0xffffffff && r + 8 <= q + 4 + sz) { off = Number(dv.getBigUint64(r, true)); r += 8; }
        }
        q += 4 + sz;
      }
      this.entries.set(name, { name, method, crc, csize, usize, offset: off });
      p = p + 46 + nl + el + cl2;
    }
  }
  names() { return Array.from(this.entries.keys()); }
  has(name) { return this.entries.has(name); }
  /** Member bytes (STORED sliced, DEFLATE inflated). */
  async read(name) {
    const e = this.entries.get(name);
    if (!e) throw new FormatError('zip: no member ' + name);
    const d = this.data, dv = new DataView(d.buffer, d.byteOffset, d.byteLength);
    if (e.offset + 30 > d.length || dv.getUint32(e.offset, true) !== ZIP_LOCAL) throw new FormatError('zip: bad local header for ' + name);
    const nl = dv.getUint16(e.offset + 26, true), el = dv.getUint16(e.offset + 28, true);
    const start = e.offset + 30 + nl + el;
    if (start + e.csize > d.length) throw new FormatError('zip: member ' + name + ' runs past end of file');
    const body = d.subarray(start, start + e.csize);
    if (e.method === 0) return body;
    if (e.method === 8) return inflateRaw(body);
    throw new FormatError('zip: unsupported compression method ' + e.method + ' for ' + name);
  }
}

/* ========================================================================
   thrift compact protocol (the subset Parquet uses)
   ======================================================================== */
const T_BOOL_TRUE = 1, T_BOOL_FALSE = 2, T_BYTE = 3, T_I16 = 4, T_I32 = 5, T_I64 = 6, T_DOUBLE = 7, T_BINARY = 8, T_LIST = 9, T_SET = 10, T_MAP = 11, T_STRUCT = 12;
const KIND_TO_TYPE = { bool: T_BOOL_TRUE, byte: T_BYTE, i16: T_I16, i32: T_I32, i64: T_I64, double: T_DOUBLE, binary: T_BINARY, struct: T_STRUCT, list: T_LIST };

export class ThriftError extends FormatError {}

/** Unsigned LEB128 varint (Numbers; exact below 2^53). */
export function encodeVarint(n) {
  if (n < 0) throw new RangeError('varint must be unsigned');
  const out = [];
  for (;;) {
    const b = n % 128;
    n = Math.floor(n / 128);
    if (n) out.push(b | 0x80); else { out.push(b); return Uint8Array.from(out); }
  }
}
export function decodeVarint(buf, pos) {
  let out = 0, mul = 1, shift = 0;
  for (;;) {
    if (pos >= buf.length) throw new ThriftError('truncated varint');
    const b = buf[pos++];
    out += (b & 0x7f) * mul;
    if (!(b & 0x80)) return [out, pos];
    mul *= 128; shift += 7;
    if (shift > 70) throw new ThriftError('varint too long');
  }
}
export function zigzagEncode(n) { return n < 0 ? -n * 2 - 1 : n * 2; }
export function zigzagDecode(n) { return n % 2 ? -(n + 1) / 2 : n / 2; }

/** Schema-less decode: struct -> {fieldId: value}; binary stays Uint8Array. */
export function decodeStruct(buf, pos = 0) {
  const out = {};
  let lastId = 0;
  for (;;) {
    if (pos >= buf.length) throw new ThriftError('truncated struct');
    const header = buf[pos++];
    if (header === 0) return [out, pos];
    const delta = header >> 4, wtype = header & 0x0f;
    let fid;
    if (delta) fid = lastId + delta;
    else { const [z, p] = decodeVarint(buf, pos); fid = zigzagDecode(z); pos = p; }
    lastId = fid;
    const [value, p2] = decodeThriftValue(buf, pos, wtype);
    out[fid] = value;
    pos = p2;
  }
}
function decodeThriftValue(buf, pos, wtype) {
  switch (wtype) {
    case T_BOOL_TRUE: return [true, pos];
    case T_BOOL_FALSE: return [false, pos];
    case T_BYTE: { if (pos >= buf.length) throw new ThriftError('truncated byte'); const b = buf[pos]; return [b > 127 ? b - 256 : b, pos + 1]; }
    case T_I16: case T_I32: case T_I64: { const [z, p] = decodeVarint(buf, pos); return [zigzagDecode(z), p]; }
    case T_DOUBLE: { if (pos + 8 > buf.length) throw new ThriftError('truncated double'); return [new DataView(buf.buffer, buf.byteOffset + pos, 8).getFloat64(0, true), pos + 8]; }
    case T_BINARY: { const [n, p] = decodeVarint(buf, pos); if (p + n > buf.length) throw new ThriftError('truncated binary'); return [buf.slice(p, p + n), p + n]; }
    case T_LIST: case T_SET: {
      if (pos >= buf.length) throw new ThriftError('truncated list header');
      const header = buf[pos++];
      let n = header >> 4;
      const etype = header & 0x0f;
      if (n === 15) { const [m, p] = decodeVarint(buf, pos); n = m; pos = p; }
      const items = [];
      for (let k = 0; k < n; k++) {
        if (etype === T_BOOL_TRUE || etype === T_BOOL_FALSE) { items.push(buf[pos] === 1); pos += 1; }
        else { const [v, p] = decodeThriftValue(buf, pos, etype); items.push(v); pos = p; }
      }
      return [items, pos];
    }
    case T_MAP: {
      if (pos >= buf.length) throw new ThriftError('truncated map header');
      const [n, p] = decodeVarint(buf, pos);
      pos = p;
      if (n === 0) return [{}, pos];
      const kv = buf[pos++];
      const ktype = kv >> 4, vtype = kv & 0x0f;
      const out = {};
      for (let k = 0; k < n; k++) {
        const [key, p1] = decodeThriftValue(buf, pos, ktype);
        const [val, p2] = decodeThriftValue(buf, p1, vtype);
        out[key instanceof Uint8Array ? new TextDecoder().decode(key) : key] = val;
        pos = p2;
      }
      return [out, pos];
    }
    case T_STRUCT: return decodeStruct(buf, pos);
    default: throw new ThriftError('unknown compact type ' + wtype);
  }
}

/** encodeStruct([[fieldId, kind, value], ...]); null/undefined values are skipped.
    Kinds: bool byte i16 i32 i64 double binary struct list:<kind>. */
export function encodeStruct(fields) {
  const parts = [];
  let lastId = 0;
  const sorted = fields.slice().sort((a, b) => a[0] - b[0]);
  for (const [fid, kind, value] of sorted) {
    if (value === null || value === undefined) continue;
    const base = kind.split(':', 1)[0];
    const wtype = base === 'bool' ? (value ? T_BOOL_TRUE : T_BOOL_FALSE) : KIND_TO_TYPE[base];
    const delta = fid - lastId;
    if (delta > 0 && delta < 16) parts.push(Uint8Array.of((delta << 4) | wtype));
    else { parts.push(Uint8Array.of(wtype)); parts.push(encodeVarint(zigzagEncode(fid))); }
    lastId = fid;
    if (base !== 'bool') parts.push(encodeThriftValue(kind, value));
  }
  parts.push(Uint8Array.of(0));
  return concatU8(parts);
}
function encodeThriftValue(kind, value) {
  const i = kind.indexOf(':');
  const base = i < 0 ? kind : kind.slice(0, i), sub = i < 0 ? '' : kind.slice(i + 1);
  switch (base) {
    case 'byte': return Uint8Array.of(value & 0xff);
    case 'i16': case 'i32': case 'i64': return encodeVarint(zigzagEncode(Number(value)));
    case 'double': { const b = new Uint8Array(8); new DataView(b.buffer).setFloat64(0, value, true); return b; }
    case 'binary': { const v = typeof value === 'string' ? utf8(value) : toU8(value); return concatU8([encodeVarint(v.length), v]); }
    case 'struct': return encodeStruct(value);
    case 'list': {
      if (!sub) throw new Error('list kind needs an element kind, e.g. list:i32');
      const esub = sub.split(':', 1)[0];
      const etype = KIND_TO_TYPE[esub];
      const n = value.length;
      const parts = [];
      if (n < 15) parts.push(Uint8Array.of((n << 4) | etype));
      else { parts.push(Uint8Array.of(0xf0 | etype)); parts.push(encodeVarint(n)); }
      for (const item of value) parts.push(esub === 'bool' ? Uint8Array.of(item ? 1 : 2) : encodeThriftValue(sub, item));
      return concatU8(parts);
    }
    default: throw new Error('unknown thrift kind ' + kind);
  }
}

/* ========================================================================
   parquet (single table, PLAIN + RLE levels, GZIP / none) — port of
   parquet_lite.py.  Writer output matches parquet-rs schemas (root element
   without repetition) so omf-rust accepts the files; reader handles data
   pages v1/v2, dictionaries, RLE/BIT_PACKED levels, nested optional groups.
   ======================================================================== */
const PARQUET_MAGIC = 'PAR1';
const BOOLEAN = 0, INT32 = 1, INT64 = 2, INT96 = 3, FLOAT = 4, DOUBLE = 5, BYTE_ARRAY = 6, FIXED_LEN_BYTE_ARRAY = 7;
const PHYSICAL_NAMES = { boolean: BOOLEAN, int32: INT32, int64: INT64, int96: INT96, float: FLOAT, double: DOUBLE, byte_array: BYTE_ARRAY, fixed_len_byte_array: FIXED_LEN_BYTE_ARRAY };
const PHYSICAL_BY_CODE = Object.fromEntries(Object.entries(PHYSICAL_NAMES).map(([k, v]) => [v, k]));
const ENC_PLAIN = 0, ENC_PLAIN_DICTIONARY = 2, ENC_RLE = 3, ENC_BIT_PACKED = 4, ENC_RLE_DICTIONARY = 8;
const CODEC_UNCOMPRESSED = 0, CODEC_SNAPPY = 1, CODEC_GZIP = 2, CODEC_LZO = 3, CODEC_BROTLI = 4, CODEC_LZ4 = 5, CODEC_ZSTD = 6, CODEC_LZ4_RAW = 7;
const CODEC_NAMES = { none: CODEC_UNCOMPRESSED, uncompressed: CODEC_UNCOMPRESSED, gzip: CODEC_GZIP, snappy: CODEC_SNAPPY, zstd: CODEC_ZSTD };
const CODEC_LABEL = { [CODEC_SNAPPY]: 'SNAPPY', [CODEC_LZO]: 'LZO', [CODEC_BROTLI]: 'BROTLI', [CODEC_LZ4]: 'LZ4', [CODEC_ZSTD]: 'ZSTD', [CODEC_LZ4_RAW]: 'LZ4_RAW' };
const REQUIRED = 0, OPTIONAL = 1, REPEATED = 2;
const PAGE_DATA = 0, PAGE_INDEX = 1, PAGE_DICTIONARY = 2, PAGE_DATA_V2 = 3;
// ConvertedType: UTF8 0, DATE 6, TIMESTAMP_MILLIS 9, TIMESTAMP_MICROS 10, UINT_8..64 = 11..14, INT_8..64 = 15..18
const CT_UTF8 = 0, CT_DATE = 6, CT_TIMESTAMP_MILLIS = 9, CT_TIMESTAMP_MICROS = 10;
const CT_UINT = { 8: 11, 16: 12, 32: 13, 64: 14 };
const CT_INT = { 8: 15, 16: 16, 32: 17, 64: 18 };
const LT_STRING = 1, LT_DATE = 6, LT_TIMESTAMP = 8, LT_INTEGER = 10;
const TU_MILLIS = 1, TU_MICROS = 2, TU_NANOS = 3;
export const DEFAULT_ROW_GROUP_SIZE = 1024 * 1024;
export const PARQUET_CREATED_BY = 'nwmm geomodel parquet_lite';

export class ParquetError extends FormatError {}

/** Leaf column for writeParquet. ptype 'boolean'|'int32'|'int64'|'float'|'double'|'byte_array';
    logical null|'string'|'uint8'..'uint64'|'int8'..'int64'|'date'|'timestamp_micros'|'timestamp_millis'|'timestamp_nanos';
    values: array / typed array (null = missing in optional columns). */
export class Column {
  constructor(name, ptype, values, optional = false, logical = null) {
    this.name = name;
    this.ptype = typeof ptype === 'number' ? ptype : PHYSICAL_NAMES[ptype];
    if (this.ptype === undefined) throw new ParquetError('unknown physical type ' + ptype);
    this.values = values;
    this.optional = !!optional;
    this.logical = logical || null;
  }
  get length() { return this.values.length; }
}
/** Optional group of leaves; present = per-row booleans (null = all present). */
export class Group {
  constructor(name, children, optional = true, present = null) {
    this.name = name; this.children = Array.from(children); this.optional = !!optional; this.present = present;
  }
  get length() { return this.children.length ? this.children[0].length : 0; }
}
/** Reader-side schema node. */
class Node {
  constructor(name, o = {}) {
    this.name = name;
    this.repetition = o.repetition === undefined ? null : o.repetition;
    this.ptype = o.ptype === undefined ? null : o.ptype;
    this.typeLength = o.typeLength === undefined ? null : o.typeLength;
    this.converted = o.converted === undefined ? null : o.converted;
    this.logical = o.logical || null;   // ['string'] | ['integer', bits, signed] | ['date'] | ['timestamp', unit, utc] | ['raw', {...}]
    this.fieldId = o.fieldId === undefined ? null : o.fieldId;
    this.children = o.children || [];
    this.path = []; this.maxDef = 0; this.maxRep = 0;
  }
  get isLeaf() { return this.ptype !== null; }
  describe(indent = 0) {
    const pad = '  '.repeat(indent);
    const rep = { [REQUIRED]: 'REQUIRED', [OPTIONAL]: 'OPTIONAL', [REPEATED]: 'REPEATED' }[this.repetition];
    if (this.isLeaf) {
      const ann = this.logical ? ' (' + logicalText(this.logical) + ')' : '';
      return `${pad}${rep || 'REQUIRED'} ${PHYSICAL_BY_CODE[this.ptype].toUpperCase()} ${this.name}${ann};`;
    }
    const lines = [indent === 0 ? `${pad}message ${this.name} {` : `${pad}${rep ? rep + ' ' : ''}group ${this.name} {`];
    for (const c of this.children) lines.push(c.describe(indent + 1));
    lines.push(pad + '}');
    return lines.join('\n');
  }
}
function logicalText(lt) {
  if (lt[0] === 'string') return 'STRING';
  if (lt[0] === 'integer') return `INTEGER(${lt[1]},${lt[2] ? 'true' : 'false'})`;
  if (lt[0] === 'date') return 'DATE';
  if (lt[0] === 'timestamp') return `TIMESTAMP(${lt[1].toUpperCase()},${lt[2] ? 'true' : 'false'})`;
  return String(lt);
}
function parseLogical(raw, converted) {
  if (raw) {
    if (raw[LT_STRING] !== undefined) return ['string'];
    if (raw[LT_INTEGER] !== undefined) { const d = raw[LT_INTEGER]; return ['integer', d[1] === undefined ? 32 : d[1], d[2] === undefined ? true : !!d[2]]; }
    if (raw[LT_DATE] !== undefined) return ['date'];
    if (raw[LT_TIMESTAMP] !== undefined) {
      const d = raw[LT_TIMESTAMP], unit = d[2] || {};
      const name = unit[TU_MICROS] !== undefined ? 'micros' : unit[TU_MILLIS] !== undefined ? 'millis' : unit[TU_NANOS] !== undefined ? 'nanos' : 'micros';
      return ['timestamp', name, d[1] === undefined ? true : !!d[1]];
    }
    return ['raw', raw];
  }
  if (converted === null || converted === undefined) return null;
  if (converted === CT_UTF8) return ['string'];
  if (converted === CT_DATE) return ['date'];
  if (converted === CT_TIMESTAMP_MILLIS) return ['timestamp', 'millis', true];
  if (converted === CT_TIMESTAMP_MICROS) return ['timestamp', 'micros', true];
  for (const [bits, code] of Object.entries(CT_UINT)) if (code === converted) return ['integer', +bits, false];
  for (const [bits, code] of Object.entries(CT_INT)) if (code === converted) return ['integer', +bits, true];
  return ['converted', converted];
}

/* ---------------------------------------------------------- RLE / bit-pack */
function bitWidth(maxValue) { let w = 0; while (maxValue > 0 && 2 ** w <= maxValue) w++; return w; }

/** RLE / bit-packed hybrid (no length prefix): literal groups of 8 values LSB first, runs <varint run<<1><value>. */
export function rleHybridEncode(values, width) {
  const n = values.length;
  if (width === 0 || n === 0) return new Uint8Array(0);
  const parts = [];
  const nbytes = (width + 7) >> 3;
  let literal = [];
  const flush = (final) => {
    if (!literal.length) return;
    if (final) while (literal.length % 8) literal.push(0);
    const groups = literal.length / 8;
    parts.push(encodeVarint(groups * 2 + 1));
    const packed = new Uint8Array(groups * width);
    let bitpos = 0;
    for (const v of literal) for (let b = 0; b < width; b++, bitpos++) if ((v >>> b) & 1) packed[bitpos >> 3] |= 1 << (bitpos & 7);
    parts.push(packed);
    literal = [];
  };
  let i = 0;
  while (i < n) {
    const v = values[i];
    let j = i + 1;
    while (j < n && values[j] === v) j++;
    let run = j - i;
    if (run >= 8) {
      const pad = (8 - literal.length % 8) % 8;
      if (pad) {
        if (run - pad >= 8) { for (let k = 0; k < pad; k++) literal.push(v); i += pad; run -= pad; }
        else { for (let k = i; k < j; k++) literal.push(values[k]); i = j; continue; }
      }
      flush(false);
      parts.push(encodeVarint(run * 2));
      const vb = new Uint8Array(nbytes);
      for (let b = 0; b < nbytes; b++) vb[b] = (v >>> (8 * b)) & 0xff;
      parts.push(vb);
      i = j;
    } else {
      for (let k = i; k < j; k++) literal.push(values[k]);
      i = j;
    }
  }
  flush(true);
  return concatU8(parts);
}
/** Decode `count` values from buf[pos:end] -> [array, newPos]. */
export function rleHybridDecode(buf, pos, end, width, count) {
  const out = new Array(count);
  let got = 0;
  if (width === 0) { out.fill(0); return [out, pos]; }
  const nbytes = (width + 7) >> 3;
  while (got < count && pos < end) {
    const [header, p] = decodeVarint(buf, pos);
    pos = p;
    if (header & 1) {
      const groups = header >>> 1;
      const total = groups * width;
      if (pos + total > end) throw new ParquetError('truncated bit-packed run');
      const nvals = Math.min(groups * 8, count - got);
      let bp = pos * 8;
      for (let k = 0; k < nvals; k++) {
        let v = 0, remaining = width, shift = 0, b = bp;
        while (remaining > 0) {
          const byte = buf[b >> 3], off = b & 7, take = Math.min(8 - off, remaining);
          v += ((byte >> off) & ((1 << take) - 1)) * 2 ** shift;
          shift += take; remaining -= take; b += take;
        }
        out[got++] = v;
        bp += width;
      }
      pos += total;
    } else {
      const run = header >>> 1;
      if (pos + nbytes > end) throw new ParquetError('truncated RLE run');
      let v = 0;
      for (let b = 0; b < nbytes; b++) v += buf[pos + b] * 2 ** (8 * b);
      if (width < 32) v = v % (2 ** width);
      pos += nbytes;
      const take = Math.min(run, count - got);
      for (let k = 0; k < take; k++) out[got++] = v;
    }
  }
  if (got < count) throw new ParquetError(`not enough level/index data (${got} of ${count})`);
  return [out, pos];
}
/** Deprecated BIT_PACKED level encoding (MSB first). */
function bitPackedDeprecatedDecode(buf, pos, width, count) {
  const total = Math.ceil(width * count / 8);
  if (pos + total > buf.length) throw new ParquetError('truncated BIT_PACKED levels');
  const out = new Array(count);
  for (let k = 0; k < count; k++) {
    let v = 0;
    for (let b = 0; b < width; b++) { const bit = k * width + b; v = v * 2 + ((buf[pos + (bit >> 3)] >> (7 - (bit & 7))) & 1); }
    out[k] = v;
  }
  return [out, pos + total];
}

/* --------------------------------------------------------------- PLAIN */
function plainEncode(ptype, values, logical) {
  const n = values.length;
  if (ptype === BOOLEAN) {
    const out = new Uint8Array((n + 7) >> 3);
    for (let k = 0; k < n; k++) if (values[k]) out[k >> 3] |= 1 << (k & 7);
    return out;
  }
  if (ptype === INT32 || ptype === INT64 || ptype === FLOAT || ptype === DOUBLE) {
    const size = ptype === INT32 || ptype === FLOAT ? 4 : 8;
    const out = new Uint8Array(n * size);
    const dv = new DataView(out.buffer);
    const unsigned = logical === 'uint32' || logical === 'uint8' || logical === 'uint16' || logical === 'uint64';
    for (let k = 0; k < n; k++) {
      const v = values[k];
      if (ptype === DOUBLE) dv.setFloat64(k * 8, Number(v), true);
      else if (ptype === FLOAT) dv.setFloat32(k * 4, Number(v), true);
      else if (ptype === INT32) { if (unsigned) dv.setUint32(k * 4, Number(v) >>> 0, true); else dv.setInt32(k * 4, Math.trunc(Number(v)), true); }
      else { const b = typeof v === 'bigint' ? v : BigInt(Math.trunc(Number(v))); if (unsigned) dv.setBigUint64(k * 8, BigInt.asUintN(64, b), true); else dv.setBigInt64(k * 8, BigInt.asIntN(64, b), true); }
    }
    return out;
  }
  if (ptype === BYTE_ARRAY) {
    const parts = [];
    for (const v of values) {
      const b = typeof v === 'string' ? utf8(v) : toU8(v);
      const len = new Uint8Array(4);
      new DataView(len.buffer).setUint32(0, b.length, true);
      parts.push(len, b);
    }
    return concatU8(parts);
  }
  throw new ParquetError('cannot PLAIN-encode physical type ' + ptype);
}
/* -> [values, newPos]; int64 comes back as Number (exact below 2^53). */
function plainDecode(ptype, buf, pos, count, typeLength, asText) {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  if (ptype === BOOLEAN) {
    const nb = (count + 7) >> 3;
    if (pos + nb > buf.length) throw new ParquetError('truncated boolean data');
    const out = new Array(count);
    for (let k = 0; k < count; k++) out[k] = !!((buf[pos + (k >> 3)] >> (k & 7)) & 1);
    return [out, pos + nb];
  }
  if (ptype === INT32 || ptype === INT64 || ptype === FLOAT || ptype === DOUBLE) {
    const size = ptype === INT32 || ptype === FLOAT ? 4 : 8;
    const nb = count * size;
    if (pos + nb > buf.length) throw new ParquetError('truncated numeric data');
    let out;
    if (ptype === DOUBLE) { out = new Float64Array(count); for (let k = 0; k < count; k++) out[k] = dv.getFloat64(pos + 8 * k, true); }
    else if (ptype === FLOAT) { out = new Float64Array(count); for (let k = 0; k < count; k++) out[k] = dv.getFloat32(pos + 4 * k, true); }
    else if (ptype === INT32) { out = new Array(count); for (let k = 0; k < count; k++) out[k] = dv.getInt32(pos + 4 * k, true); }
    else { out = new Array(count); for (let k = 0; k < count; k++) out[k] = Number(dv.getBigInt64(pos + 8 * k, true)); }
    return [out, pos + nb];
  }
  if (ptype === INT96) { const out = new Array(count); for (let k = 0; k < count; k++) out[k] = buf.slice(pos + 12 * k, pos + 12 * k + 12); return [out, pos + 12 * count]; }
  if (ptype === FIXED_LEN_BYTE_ARRAY) { const tl = typeLength || 0; const out = new Array(count); for (let k = 0; k < count; k++) out[k] = buf.slice(pos + tl * k, pos + tl * (k + 1)); return [out, pos + count * tl]; }
  if (ptype === BYTE_ARRAY) {
    const out = new Array(count);
    const dec = asText ? new TextDecoder() : null;
    for (let k = 0; k < count; k++) {
      if (pos + 4 > buf.length) throw new ParquetError('truncated byte array');
      const n = dv.getUint32(pos, true);
      pos += 4;
      const v = buf.slice(pos, pos + n);
      pos += n;
      out[k] = asText ? dec.decode(v) : v;
    }
    return [out, pos];
  }
  throw new ParquetError('unsupported physical type ' + ptype);
}

/* --------------------------------------------------------------- writer */
class PqLeaf {
  constructor(column, path, chain) { this.column = column; this.path = path; this.chain = chain; this.maxDef = chain.filter(g => g.optional).length + (column.optional ? 1 : 0); }
}
function logicalFields(logical, ptype) {
  if (!logical) return [null, null];
  if (logical === 'string') {
    if (ptype !== BYTE_ARRAY) throw new ParquetError('string logical type needs byte_array');
    return [CT_UTF8, [[LT_STRING, 'struct', []]]];
  }
  if (logical.startsWith('uint') || logical.startsWith('int')) {
    const signed = !logical.startsWith('u');
    const bits = parseInt(logical.replace(/^u?int/, ''), 10);
    const ct = (signed ? CT_INT : CT_UINT)[bits];
    if (ct === undefined) throw new ParquetError('unknown logical type ' + logical);
    return [ct, [[LT_INTEGER, 'struct', [[1, 'byte', bits], [2, 'bool', signed]]]]];
  }
  if (logical === 'date') return [CT_DATE, [[LT_DATE, 'struct', []]]];
  if (logical.startsWith('timestamp')) {
    const unit = logical.includes('_') ? logical.split('_', 2)[1] : 'micros';
    const tu = { millis: TU_MILLIS, micros: TU_MICROS, nanos: TU_NANOS }[unit];
    if (!tu) throw new ParquetError('unknown logical type ' + logical);
    const ct = { millis: CT_TIMESTAMP_MILLIS, micros: CT_TIMESTAMP_MICROS }[unit];
    return [ct === undefined ? null : ct, [[LT_TIMESTAMP, 'struct', [[1, 'bool', true], [2, 'struct', [[tu, 'struct', []]]]]]]];
  }
  throw new ParquetError('unknown logical type ' + logical);
}
function flattenColumns(fields) {
  const leaves = [], elements = [];
  const visit = (field, chain) => {
    if (field instanceof Group) {
      elements.push([[3, 'i32', field.optional ? OPTIONAL : REQUIRED], [4, 'binary', field.name], [5, 'i32', field.children.length]]);
      for (const c of field.children) visit(c, chain.concat([field]));
    } else {
      const [ct, lt] = logicalFields(field.logical, field.ptype);
      elements.push([[1, 'i32', field.ptype], [3, 'i32', field.optional ? OPTIONAL : REQUIRED], [4, 'binary', field.name], [6, 'i32', ct], [10, 'struct', lt]]);
      leaves.push(new PqLeaf(field, chain.map(g => g.name).concat([field.name]), chain.slice()));
    }
  };
  for (const f of fields) visit(f, []);
  return [leaves, elements];
}
/* Definition levels + present values for rows [start, stop). */
function defLevels(leaf, start, stop) {
  const col = leaf.column, vals = col.values;
  const optGroups = leaf.chain.filter(g => g.optional);
  if (!optGroups.length && !col.optional) return [null, ArrayBuffer.isView(vals) ? vals.subarray(start, stop) : vals.slice(start, stop)];
  const levels = [], present = [];
  if (!optGroups.length) {
    for (let i = start; i < stop; i++) { const v = vals[i]; if (v === null || v === undefined) levels.push(0); else { levels.push(1); present.push(v); } }
    return [levels, present];
  }
  for (let i = start; i < stop; i++) {
    let d = 0, absent = false;
    for (const g of optGroups) { if (g.present === null || g.present === undefined || g.present[i]) d += 1; else { absent = true; break; } }
    if (!absent) {
      const v = vals[i];
      if (col.optional) { if (v === null || v === undefined) absent = true; else d += 1; }
      if (!absent) present.push(v);
    }
    levels.push(d);
  }
  return [levels, present];
}
function u32le(n) { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, n, true); return b; }
function i32le(n) { const b = new Uint8Array(4); new DataView(b.buffer).setInt32(0, n, true); return b; }

/** Write a Parquet file from Column / Group specs -> Promise<Uint8Array>.
    opts: compression 'gzip' (default) | 'none', rowGroupSize, createdBy. */
export async function writeParquet(fields, opts = {}) {
  fields = Array.from(fields);
  const [leaves, schemaElements] = flattenColumns(fields);
  if (!leaves.length) throw new ParquetError('no columns');
  const nrows = leaves[0].column.values.length;
  for (const lf of leaves) {
    if (lf.column.values.length !== nrows) throw new ParquetError(`column ${lf.path.join('.')} has ${lf.column.values.length} rows, expected ${nrows}`);
    for (const g of lf.chain) if (g.present && g.present.length !== nrows) throw new ParquetError(`group ${g.name} presence mask has the wrong length`);
  }
  const codec = CODEC_NAMES[(opts.compression || 'gzip').toLowerCase()];
  if (codec === undefined) throw new ParquetError('unknown compression ' + opts.compression);
  if (codec !== CODEC_UNCOMPRESSED && codec !== CODEC_GZIP) throw new ParquetError('writer supports gzip / none only');
  const createdBy = opts.createdBy || PARQUET_CREATED_BY;
  const parts = [utf8(PARQUET_MAGIC)];
  let tell = 4;
  const push = (u8) => { parts.push(u8); tell += u8.length; };
  const rowGroupSize = Math.max(1, (opts.rowGroupSize || DEFAULT_ROW_GROUP_SIZE) | 0);
  const starts = [];
  for (let s = 0; s < nrows; s += rowGroupSize) starts.push(s);
  if (!starts.length) starts.push(0);
  const rowGroups = [];
  for (const rgStart of starts) {
    const rgStop = Math.min(nrows, rgStart + rowGroupSize);
    const rgRows = rgStop - rgStart;
    const chunks = [];
    let rgUnc = 0, rgComp = 0;
    const rgFileOffset = tell;
    for (const lf of leaves) {
      const col = lf.column;
      const [levels, present] = defLevels(lf, rgStart, rgStop);
      const bodyParts = [];
      if (levels !== null) { const enc = rleHybridEncode(levels, bitWidth(lf.maxDef)); bodyParts.push(u32le(enc.length), enc); }
      bodyParts.push(plainEncode(col.ptype, present, col.logical));
      const body = concatU8(bodyParts);
      const payload = codec === CODEC_GZIP ? await gzip(body) : body;
      const header = encodeStruct([[1, 'i32', PAGE_DATA], [2, 'i32', body.length], [3, 'i32', payload.length],
        [5, 'struct', [[1, 'i32', rgRows], [2, 'i32', ENC_PLAIN], [3, 'i32', ENC_RLE], [4, 'i32', ENC_RLE]]]]);
      const pageOffset = tell;
      push(header); push(payload);
      const unc = header.length + body.length, comp = header.length + payload.length;
      rgUnc += unc; rgComp += comp;
      const meta = [[1, 'i32', col.ptype], [2, 'list:i32', [ENC_PLAIN, ENC_RLE]], [3, 'list:binary', lf.path.slice()],
        [4, 'i32', codec], [5, 'i64', rgRows], [6, 'i64', unc], [7, 'i64', comp], [9, 'i64', pageOffset]];
      chunks.push([[2, 'i64', 0], [3, 'struct', meta]]);
    }
    rowGroups.push([[1, 'list:struct', chunks], [2, 'i64', rgUnc], [3, 'i64', rgRows], [5, 'i64', rgFileOffset], [6, 'i64', rgComp], [7, 'i16', rowGroups.length]]);
  }
  const root = [[4, 'binary', 'schema'], [5, 'i32', fields.length]];
  const footer = encodeStruct([[1, 'i32', 2], [2, 'list:struct', [root].concat(schemaElements)], [3, 'i64', nrows],
    [4, 'list:struct', rowGroups], [6, 'binary', createdBy], [7, 'list:struct', leaves.map(() => [[1, 'struct', []]])]]);
  push(footer); push(i32le(footer.length)); push(utf8(PARQUET_MAGIC));
  return concatU8(parts);
}

/* --------------------------------------------------------------- reader */
function thriftText(b) { return b instanceof Uint8Array ? new TextDecoder().decode(b) : b; }
async function pqDecompress(codec, data) {
  if (codec === CODEC_UNCOMPRESSED) return data;
  if (codec === CODEC_GZIP) { try { return await gunzip(data); } catch (e) { return await inflate(data); } }
  throw new ParquetError(`${CODEC_LABEL[codec] || codec}-compressed parquet is not supported in the browser reader (re-write the file with GZIP or no compression)`);
}

/** Decoded Parquet file: schema (root Node), leaves, numRows, createdBy, keyValue, columns {'a.b': values}. */
export class ParquetFile {
  constructor(data) { this.data = toU8(data); this.columns = {}; this._parseFooter(); }
  _parseFooter() {
    const data = this.data, n = data.length;
    if (n < 12 || !bytesEq(data, 0, PARQUET_MAGIC) || !bytesEq(data, n - 4, PARQUET_MAGIC)) throw new ParquetError('not a parquet file (missing PAR1 magic)');
    const flen = new DataView(data.buffer, data.byteOffset + n - 8, 4).getInt32(0, true);
    if (flen <= 0 || flen + 12 > n) throw new ParquetError('bad parquet footer length ' + flen);
    const [meta] = decodeStruct(data.subarray(n - 8 - flen, n - 8), 0);
    this.version = meta[1];
    this.numRows = meta[3] || 0;
    this.createdBy = thriftText(meta[6] || '');
    this.keyValue = {};
    for (const kv of meta[5] || []) this.keyValue[thriftText(kv[1] || '')] = kv[2] === undefined ? null : thriftText(kv[2]);
    const elements = meta[2] || [];
    if (!elements.length) throw new ParquetError('parquet file has no schema');
    const [schema, nxt] = this._buildNode(elements, 0);
    if (nxt !== elements.length) throw new ParquetError('schema tree does not consume all elements');
    this.schema = schema;
    this.leaves = [];
    this._assignPaths(this.schema, [], 0, 0, true);
    this.rowGroups = meta[4] || [];
  }
  _buildNode(elements, idx) {
    const e = elements[idx];
    const node = new Node(thriftText(e[4] || ''), { repetition: e[3] === undefined ? null : e[3], fieldId: e[9] === undefined ? null : e[9] });
    const nchildren = e[5];
    if (nchildren) {
      idx += 1;
      for (let k = 0; k < nchildren; k++) { const [child, i2] = this._buildNode(elements, idx); node.children.push(child); idx = i2; }
      return [node, idx];
    }
    if (e[1] === undefined) return [node, idx + 1];
    node.ptype = e[1];
    node.typeLength = e[2] === undefined ? null : e[2];
    node.converted = e[6] === undefined ? null : e[6];
    node.logical = parseLogical(e[10], node.converted);
    return [node, idx + 1];
  }
  _assignPaths(node, path, maxDef, maxRep, root = false) {
    if (!root) {
      path = path.concat([node.name]);
      if (node.repetition === OPTIONAL) maxDef += 1;
      else if (node.repetition === REPEATED) { maxDef += 1; maxRep += 1; }
    }
    node.path = path; node.maxDef = maxDef; node.maxRep = maxRep;
    if (node.isLeaf) this.leaves.push(node);
    for (const c of node.children) this._assignPaths(c, path, maxDef, maxRep);
  }
  schemaText() { return this.schema.describe(); }
  leaf(path) {
    const p = typeof path === 'string' ? path.split('.') : Array.from(path);
    for (const lf of this.leaves) if (lf.path.length === p.length && lf.path.every((s, i) => s === p[i])) return lf;
    return null;
  }
  column(path) {
    const key = Array.isArray(path) ? path.join('.') : path;
    if (!(key in this.columns)) throw new ParquetError('no column ' + key);
    return this.columns[key];
  }
  async readAll() {
    const cols = this.leaves.map(() => []);
    for (const rg of this.rowGroups) {
      const chunks = rg[1] || [];
      if (chunks.length !== this.leaves.length) throw new ParquetError(`row group has ${chunks.length} column chunks, schema has ${this.leaves.length} leaves`);
      const nrows = rg[3] || 0;
      for (let k = 0; k < this.leaves.length; k++) cols[k].push(await this._readChunk(this.leaves[k], chunks[k], nrows));
    }
    this.columns = {};
    this.leaves.forEach((lf, k) => { this.columns[lf.path.join('.')] = joinColumn(cols[k], lf); });
    return this.columns;
  }
  async _readChunk(leaf, chunk, nrows) {
    if (chunk[1]) throw new ParquetError('external column chunk files are not supported');
    const cm = chunk[3];
    if (!cm) throw new ParquetError('column chunk without metadata');
    if (leaf.maxRep) throw new ParquetError('repeated fields (lists/maps) are not supported: ' + leaf.path.join('.'));
    const codec = cm[4] === undefined ? CODEC_UNCOMPRESSED : cm[4];
    const numValues = cm[5] || 0;
    const dataOff = cm[9], dictOff = cm[11];
    const start = dictOff === undefined ? dataOff : Math.min(dataOff, dictOff);
    const end = start + (cm[7] || 0);
    if (start === undefined || end > this.data.length) throw new ParquetError('column chunk offsets out of range');
    const asText = !!(leaf.logical && leaf.logical[0] === 'string');
    const unsignedBits = leaf.logical && leaf.logical[0] === 'integer' && !leaf.logical[2] ? leaf.logical[1] : 0;
    let pos = start, dictionary = null, seen = 0;
    const pages = [];
    const data = this.data;
    while (pos < end && seen < numValues) {
      const [header, hpos] = decodeStruct(data, pos);
      const ptypePage = header[1];
      const compSize = header[3] || 0;
      const body = data.subarray(hpos, hpos + compSize);
      pos = hpos + compSize;
      if (ptypePage === PAGE_DICTIONARY) {
        const dh = header[7] || {};
        const raw = await pqDecompress(codec, body);
        [dictionary] = plainDecode(leaf.ptype, raw, 0, dh[1] || 0, leaf.typeLength, asText);
      } else if (ptypePage === PAGE_DATA) {
        const dh = header[5] || {};
        const n = dh[1] || 0;
        const raw = await pqDecompress(codec, body);
        let p = 0, levels = null;
        if (leaf.maxDef) [levels, p] = decodeLevels(raw, p, leaf.maxDef, n, dh[3] === undefined ? ENC_RLE : dh[3], true);
        const nvals = levels === null ? n : countEq(levels, leaf.maxDef);
        const vals = decodeValues(leaf, raw, p, nvals, dh[2] === undefined ? ENC_PLAIN : dh[2], dictionary, asText);
        pages.push([levels, n, vals]);
        seen += n;
      } else if (ptypePage === PAGE_DATA_V2) {
        const dh = header[8] || {};
        const n = dh[1] || 0, nnulls = dh[2] || 0, rl = dh[6] || 0, dl = dh[5] || 0;
        if (rl || leaf.maxRep) throw new ParquetError('repeated fields are not supported');
        const isComp = dh[7] === undefined ? true : dh[7];
        let levels = null;
        if (leaf.maxDef) [levels] = rleHybridDecode(body, 0, dl, bitWidth(leaf.maxDef), n);
        let vbytes = body.subarray(dl);
        if (isComp && codec !== CODEC_UNCOMPRESSED) vbytes = await pqDecompress(codec, vbytes);
        const nvals = levels === null ? n - nnulls : countEq(levels, leaf.maxDef);
        const vals = decodeValues(leaf, vbytes, 0, nvals, dh[4] === undefined ? ENC_PLAIN : dh[4], dictionary, asText);
        pages.push([levels, n, vals]);
        seen += n;
      } else if (ptypePage === PAGE_INDEX) {
        continue;
      } else throw new ParquetError('unknown page type ' + ptypePage);
    }
    let out = [];
    for (const [levels, n, vals] of pages) {
      if (levels === null) { for (let k = 0; k < vals.length; k++) out.push(vals[k]); }
      else { let it = 0; const md = leaf.maxDef; for (let k = 0; k < n; k++) out.push(levels[k] === md ? vals[it++] : null); }
    }
    if (unsignedBits && out.length) { const wrap = 2 ** unsignedBits; out = out.map(v => (v === null || v >= 0) ? v : v + wrap); }
    if (out.length !== numValues) throw new ParquetError(`column ${leaf.path.join('.')}: decoded ${out.length} values, metadata says ${numValues}`);
    return out;
  }
}
function countEq(levels, md) { let c = 0; for (let k = 0; k < levels.length; k++) if (levels[k] === md) c++; return c; }
/* Required DOUBLE / FLOAT columns come back as Float64Array; everything else as Array. */
function joinColumn(parts, leaf) {
  const all = parts.length === 1 ? parts[0] : [].concat(...parts);
  if (leaf.maxDef === 0 && (leaf.ptype === DOUBLE || leaf.ptype === FLOAT)) return Float64Array.from(all);
  return all;
}
function decodeLevels(raw, pos, maxDef, count, encoding, v1) {
  const width = bitWidth(maxDef);
  if (encoding === ENC_RLE) {
    if (v1) {
      if (pos + 4 > raw.length) throw new ParquetError('truncated level length');
      const n = new DataView(raw.buffer, raw.byteOffset + pos, 4).getUint32(0, true);
      pos += 4;
      const [levels] = rleHybridDecode(raw, pos, pos + n, width, count);
      return [levels, pos + n];
    }
    return rleHybridDecode(raw, pos, raw.length, width, count);
  }
  if (encoding === ENC_BIT_PACKED) return bitPackedDeprecatedDecode(raw, pos, width, count);
  throw new ParquetError('unsupported level encoding ' + encoding);
}
function decodeValues(leaf, raw, pos, nvals, encoding, dictionary, asText) {
  if (nvals === 0) return [];
  if (encoding === ENC_PLAIN) return plainDecode(leaf.ptype, raw, pos, nvals, leaf.typeLength, asText)[0];
  if (encoding === ENC_RLE_DICTIONARY || encoding === ENC_PLAIN_DICTIONARY) {
    if (!dictionary) throw new ParquetError('dictionary-encoded page without a dictionary page');
    if (pos >= raw.length) throw new ParquetError('truncated dictionary indices');
    const width = raw[pos];
    const [idx] = rleHybridDecode(raw, pos + 1, raw.length, width, nvals);
    const out = new Array(nvals);
    for (let k = 0; k < nvals; k++) { if (idx[k] >= dictionary.length) throw new ParquetError('dictionary index out of range'); out[k] = dictionary[idx[k]]; }
    return out;
  }
  if (encoding === ENC_RLE && leaf.ptype === BOOLEAN) {
    if (pos + 4 > raw.length) throw new ParquetError('truncated RLE boolean length');
    const n = new DataView(raw.buffer, raw.byteOffset + pos, 4).getUint32(0, true);
    const [bits] = rleHybridDecode(raw, pos + 4, pos + 4 + n, 1, nvals);
    return bits.map(b => !!b);
  }
  throw new ParquetError(`unsupported value encoding ${encoding} for column ${leaf.path.join('.')}`);
}
/** bytes -> Promise<ParquetFile> with every column decoded into .columns */
export async function readParquet(src) {
  const pf = new ParquetFile(src);
  await pf.readAll();
  return pf;
}

/* ========================================================================
   grid helpers shared by the grid formats
   ======================================================================== */
function gridZRange(values) {
  let mn = Infinity, mx = -Infinity, any = false;
  for (let i = 0; i < values.length; i++) { const v = values[i]; if (v !== v) continue; any = true; if (v < mn) mn = v; if (v > mx) mx = v; }
  return any ? [mn, mx] : [null, null];
}
function gridSpacing(lo, hi, n, axis, warnings) {
  if (n <= 1) { warnings.push(`${axis}: only one node, spacing undefined; set to 1.0`); return 1.0; }
  const d = (hi - lo) / (n - 1);
  if (d <= 0) warnings.push(`${axis}: non-positive spacing ${pyRepr(d)} (hi ${pyRepr(hi)} <= lo ${pyRepr(lo)})`);
  return d;
}
function finishGrid(grid, fmt, opts, warnings, extra) {
  setProvenance(grid, fmt, opts.file);
  grid.metadata.warnings = warnings;
  if (extra) Object.assign(grid.metadata, extra);
  if (!grid.name) grid.name = opts.name || stem(opts.file, '');
  grid.role = 'surface';
  return grid;
}
function asText(src) { return typeof src === 'string' ? src : decodeLatin1(toU8(src)); }

/* ========================================================================
   surfer — Golden Software grids (.grd: DSAA / DSBB / DSRB) and BLN files
   DSAA: 5 header lines then free-format values, south row first.
   DSBB: 'DSBB' int16 nx ny, 6 doubles xlo xhi ylo yhi zlo zhi, float32 values.
   DSRB: tagged sections (int32 id, uint32 size): DSRB(version) GRID(72 bytes:
         int32 nRow nCol, doubles xLL yLL xSize ySize zMin zMax Rotation Blank)
         DATA(doubles) [FLTI skipped].  Blank = 1.70141e38 (>= 1.7e38 on read).
   ======================================================================== */
export const SURFER_BLANK = 1.70141e38;
const BLANK_F32 = 1.701410009187828e+38;     // float32 0x7EFFFFEE, what Surfer stores
const BLANK_THRESHOLD = 1.7e38;
const BLANK_TEXT = '1.70141e+38';
const TAG_HEADER = 0x42525344, TAG_GRID = 0x44495247, TAG_DATA = 0x41544144, TAG_FAULT = 0x49544c46;

export function readSurferGrd(src, opts = {}) {
  const data = toU8(src);
  if (bytesEq(data, 0, 'DSAA')) return readDsaa(data, opts);
  if (bytesEq(data, 0, 'DSBB')) return readDsbb(data, opts);
  if (bytesEq(data, 0, 'DSRB')) return readDsrb(data, opts);
  throw new FormatError(`not a Surfer grid (expected DSAA/DSBB/DSRB, found ${pyReprStr(decodeLatin1(data.subarray(0, 4)))})`);
}
function readDsaa(data, opts) {
  const warnings = [];
  const tokens = pySplit(decodeLatin1(data));
  if (tokens.length < 9) throw new FormatError('DSAA header truncated');
  const nx = pyInt(tokens[1]), ny = pyInt(tokens[2]);
  const head = tokens.slice(3, 9).map(pyFloat);
  if (nx === undefined || ny === undefined || head.some(v => v === undefined)) throw new FormatError('DSAA header not numeric');
  const [xlo, xhi, ylo, yhi, zlo, zhi] = head;
  if (nx <= 0 || ny <= 0) throw new FormatError(`DSAA grid size ${nx}x${ny} invalid`);
  const n = nx * ny;
  const vals = tokens.length - 9;
  if (vals < n) throw new FormatError(`DSAA: expected ${n} values, found ${vals}`);
  if (vals > n) warnings.push(`DSAA: ${vals - n} trailing tokens ignored`);
  const values = new Float64Array(n);
  let blanks = 0;
  for (let k = 0; k < n; k++) {
    const v = pyFloat(tokens[9 + k]);
    if (v === undefined) throw new FormatError(`DSAA: non-numeric value ${pyReprStr(tokens[9 + k])}`);
    if (v >= BLANK_THRESHOLD) { values[k] = NAN; blanks++; } else values[k] = v;
  }
  const dx = gridSpacing(xlo, xhi, nx, 'x', warnings), dy = gridSpacing(ylo, yhi, ny, 'y', warnings);
  const g = new GM.Grid2D({ nx, ny, x0: xlo, y0: ylo, dx, dy, values });
  return finishGrid(g, 'surfer_grd', opts, warnings, { surfer_variant: 'DSAA', zlo, zhi, blank_nodes: blanks });
}
function readDsbb(data, opts) {
  const warnings = [];
  if (data.length < 56) throw new FormatError('DSBB header truncated');
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const nx = dv.getInt16(4, true), ny = dv.getInt16(6, true);
  const xlo = dv.getFloat64(8, true), xhi = dv.getFloat64(16, true), ylo = dv.getFloat64(24, true), yhi = dv.getFloat64(32, true), zlo = dv.getFloat64(40, true), zhi = dv.getFloat64(48, true);
  if (nx <= 0 || ny <= 0) throw new FormatError(`DSBB grid size ${nx}x${ny} invalid`);
  const n = nx * ny, need = 56 + 4 * n;
  if (data.length < need) throw new FormatError(`DSBB: expected ${4 * n} data bytes, found ${data.length - 56}`);
  if (data.length > need) warnings.push(`DSBB: ${data.length - need} trailing bytes ignored`);
  const values = new Float64Array(n);
  let blanks = 0;
  for (let k = 0; k < n; k++) {
    const v = dv.getFloat32(56 + 4 * k, true);
    if (v >= BLANK_THRESHOLD || v !== v) { values[k] = NAN; blanks++; } else values[k] = v;
  }
  const dx = gridSpacing(xlo, xhi, nx, 'x', warnings), dy = gridSpacing(ylo, yhi, ny, 'y', warnings);
  const g = new GM.Grid2D({ nx, ny, x0: xlo, y0: ylo, dx, dy, values });
  return finishGrid(g, 'surfer_grd', opts, warnings, { surfer_variant: 'DSBB', zlo, zhi, blank_nodes: blanks });
}
function readDsrb(data, opts) {
  const warnings = [];
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const n = data.length;
  let pos = 0, version = null, header = null, values = null, faults = 0;
  while (pos + 8 <= n) {
    const tag = dv.getInt32(pos, true), size = dv.getUint32(pos + 4, true);
    pos += 8;
    if (pos + size > n) throw new FormatError(`DSRB: section 0x${(tag >>> 0).toString(16).padStart(8, '0')} (size ${size}) runs past end of file`);
    if (tag === TAG_HEADER) {
      if (size >= 4) version = dv.getInt32(pos, true);
      if (version !== 1 && version !== 2) warnings.push(`DSRB: unexpected version ${version === null ? 'None' : version} (expected 1 or 2)`);
    } else if (tag === TAG_GRID) {
      if (size < 72) throw new FormatError(`DSRB: GRID section is ${size} bytes, expected 72`);
      header = [dv.getInt32(pos, true), dv.getInt32(pos + 4, true)];
      for (let k = 0; k < 8; k++) header.push(dv.getFloat64(pos + 8 + 8 * k, true));
    } else if (tag === TAG_DATA) {
      if (header === null) throw new FormatError('DSRB: DATA section before GRID section');
      const count = header[0] * header[1];
      if (size < 8 * count) throw new FormatError(`DSRB: DATA section has ${size} bytes, expected ${8 * count}`);
      values = new Float64Array(count);
      for (let k = 0; k < count; k++) values[k] = dv.getFloat64(pos + 8 * k, true);
    } else if (tag === TAG_FAULT) faults++;
    else warnings.push(`DSRB: unknown section 0x${(tag >>> 0).toString(16).padStart(8, '0')} (${size} bytes) skipped`);
    pos += size;
    if (values !== null && pos >= n) break;
  }
  if (header === null || values === null) throw new FormatError('DSRB: missing GRID or DATA section');
  const [nrow, ncol, xll, yll, xsize, ysize, zmin, zmax, rot, blank] = header;
  if (nrow <= 0 || ncol <= 0) throw new FormatError(`DSRB grid size ${ncol}x${nrow} invalid`);
  if (rot) warnings.push(`DSRB: rotation field ${pyRepr(rot)} ignored (unused by Surfer)`);
  if (faults) warnings.push(`DSRB: ${faults} fault section(s) skipped`);
  if (xsize <= 0 || ysize <= 0) warnings.push(`DSRB: non-positive node spacing (${pyRepr(xsize)}, ${pyRepr(ysize)})`);
  const out = new Float64Array(values.length);
  let blanks = 0;
  for (let k = 0; k < values.length; k++) {
    const v = values[k];
    if (v !== v || (version !== 2 ? v >= blank : v === blank) || v >= BLANK_THRESHOLD) { out[k] = NAN; blanks++; } else out[k] = v;
  }
  const g = new GM.Grid2D({ nx: ncol, ny: nrow, x0: xll, y0: yll, dx: xsize, dy: ysize, values: out });
  return finishGrid(g, 'surfer_grd', opts, warnings, { surfer_variant: 'DSRB', surfer_version: version, zlo: zmin, zhi: zmax, blank_value: blank, blank_nodes: blanks });
}

/** Grid2D -> Surfer grid bytes. opts.fmt 'dsaa' (default) | 'dsbb' | 'dsrb'. */
export function writeSurferGrd(grid, opts = {}) {
  const fmt = (typeof opts === 'string' ? opts : (opts.fmt || 'dsaa')).toLowerCase();
  if (grid.rotation) throw new FormatError(`Surfer grids cannot be rotated (grid.rotation=${pyRepr(grid.rotation)}); resample to an axis-aligned grid first`);
  if (grid.dx <= 0 || grid.dy <= 0) throw new FormatError(`grid spacing must be positive (dx=${pyRepr(grid.dx)}, dy=${pyRepr(grid.dy)})`);
  let [zlo, zhi] = gridZRange(grid.values);
  if (zlo === null) zlo = zhi = SURFER_BLANK;
  if (fmt === 'dsaa') {
    const nx = grid.nx, ny = grid.ny, vals = grid.values;
    const lines = ['DSAA', `${nx} ${ny}`, `${pyRepr(grid.x0)} ${pyRepr(grid.xmax)}`, `${pyRepr(grid.y0)} ${pyRepr(grid.ymax)}`, `${pyRepr(zlo)} ${pyRepr(zhi)}`];
    for (let j = 0; j < ny; j++) {
      const txt = [];
      for (let i = 0; i < nx; i++) { const v = vals[j * nx + i]; txt.push(v !== v ? BLANK_TEXT : pyRepr(v)); }
      for (let k = 0; k < nx; k += 10) lines.push(txt.slice(k, k + 10).join(' '));
      lines.push('');
    }
    return encodeAscii(lines.join('\n') + '\n');
  }
  if (fmt === 'dsbb') {
    const nx = grid.nx, ny = grid.ny;
    if (nx > 32767 || ny > 32767) throw new FormatError(`DSBB stores nx/ny as int16; ${nx}x${ny} is too large — use fmt="dsrb"`);
    const w = new ByteWriter(56 + 4 * nx * ny);
    w.bytes(encodeAscii('DSBB')).i16(nx).i16(ny).f64(grid.x0).f64(grid.xmax).f64(grid.y0).f64(grid.ymax).f64(zlo).f64(zhi);
    for (let k = 0; k < nx * ny; k++) { const v = grid.values[k]; w.f32(v !== v ? BLANK_F32 : v); }
    return w.result();
  }
  if (fmt === 'dsrb') {
    const nx = grid.nx, ny = grid.ny;
    const w = new ByteWriter(100 + 8 * nx * ny);
    w.i32(TAG_HEADER).u32(4).i32(1);
    w.i32(TAG_GRID).u32(72).i32(ny).i32(nx).f64(grid.x0).f64(grid.y0).f64(grid.dx).f64(grid.dy).f64(zlo).f64(zhi).f64(0.0).f64(BLANK_F32);
    w.i32(TAG_DATA).u32(8 * nx * ny);
    for (let k = 0; k < nx * ny; k++) { const v = grid.values[k]; w.f64(v !== v ? BLANK_F32 : v); }
    return w.result();
  }
  throw new FormatError(`fmt must be 'dsaa', 'dsbb' or 'dsrb', not ${pyReprStr(fmt)}`);
}

/* -- BLN: 'count, flag[, "name"]' header per polyline + count 'x, y[, z]' lines */
function splitBln(line) {
  const out = [];
  const re = /"([^"]*)"|([^,\s"]+)/g;
  let m;
  while ((m = re.exec(line)) !== null) out.push(m[1] !== undefined ? m[1] : m[2]);
  return out;
}
export function readBln(src, opts = {}) {
  const warnings = [];
  const lines = pySplitlines(asText(src)).map(pyStrip);
  const ls = new GM.LineSet({ role: 'lines' });
  let k = 0;
  const nlines = lines.length;
  while (k < nlines) {
    if (!lines[k]) { k++; continue; }
    const head = splitBln(lines[k]);
    k++;
    const c = head.length ? pyFloat(head[0]) : undefined;
    if (c === undefined || c !== c) throw new FormatError(`BLN: bad polyline header ${pyReprStr(lines[k - 1])} at line ${k}`);
    const count = Math.trunc(c);
    let flag = 0, name = '';
    if (head.length > 1) { const f = pyFloat(head[1]); if (f === undefined || f !== f) name = head[1]; else flag = Math.trunc(f); }
    if (head.length > 2) name = head[2];
    const pts = [];
    while (pts.length < count && k < nlines) {
      if (!lines[k]) { k++; continue; }
      const tok = splitBln(lines[k]);
      k++;
      const x = pyFloat(tok[0]), y = pyFloat(tok[1]);
      const z = tok.length > 2 ? pyFloat(tok[2]) : 0.0;
      if (x === undefined || y === undefined || z === undefined) throw new FormatError(`BLN: bad vertex ${pyReprStr(lines[k - 1])} at line ${k}`);
      pts.push([x, y, z]);
    }
    if (pts.length < count) warnings.push(`BLN: polyline ${pyReprStr(name)} declares ${count} vertices, found ${pts.length}`);
    if (!pts.length) continue;
    const closed = pts.length > 2 && pts[0][0] === pts[pts.length - 1][0] && pts[0][1] === pts[pts.length - 1][1];
    ls.addPolyline(pts, { flag, name, closed });
  }
  setProvenance(ls, 'surfer_bln', opts.file);
  ls.metadata.warnings = warnings;
  ls.name = opts.name || stem(opts.file, '');
  return ls;
}
export function writeBln(lineset) {
  const parts = lineset.parts || [];
  let withZ = false;
  for (let k = 2; k < lineset.vertices.length; k += 3) { const v = lineset.vertices[k]; if (v === v && v !== 0) { withZ = true; break; } }
  const lines = [];
  parts.forEach((part, k) => {
    const feat = k < lineset.features.length ? lineset.features[k] : {};
    let flag = feat.flag === undefined ? 1 : feat.flag;
    if (typeof flag === 'number') flag = Math.trunc(flag);
    else if (typeof flag === 'boolean') flag = flag ? 1 : 0;
    else { const f = pyInt(flag); flag = f === undefined ? 1 : f; }
    const name = feat.name || '';
    let head = `${part.length},${flag}`;
    if (name) head += `,"${String(name).replace(/"/g, "'")}"`;
    lines.push(head);
    for (const idx of part) {
      const [x, y, z] = lineset.vertex(idx);
      lines.push(withZ ? `${pyRepr(x)},${pyRepr(y)},${pyRepr(z !== z ? 0.0 : z)}` : `${pyRepr(x)},${pyRepr(y)}`);
    }
  });
  return encodeAscii(lines.join('\n') + '\n');
}

/* ========================================================================
   geosoft — Oasis montaj binary grid (.grd), GXF (.gxf), XYZ (.xyz)
   GRD v2 header (512 bytes, LE): int32 ES SF NE NV KX @0; double DE DV X0 Y0
   ROT ZBASE ZMULT @20; char LABEL[48] @76, MAPNO[16] @124; int32 PROJ UNITX
   UNITY UNITZ NVPTS @140; float IZMIN IZMAX IZMED IZMEA @160; double ZVAR
   @176; int32 PRCS @184.  ES > 1024 = compressed (zlib blocks, table @512).
   ======================================================================== */
export const GEOSOFT_DUMMY = -1e32;
const GXF_DEFAULT_DUMMY = -1e12;
const GEOSOFT_HEADER = 512;

function geosoftType(es, sf) {
  if (es === 1) return { 0: 'B', 1: 'b' }[sf] || null;
  if (es === 2) return { 0: 'H', 1: 'h' }[sf] || null;
  if (es === 4) return { 0: 'I', 1: 'i', 2: 'f' }[sf] || null;
  if (es === 8) return 'd';
  return null;
}
const INT_DUMMY = { b: -127, B: 255, h: -32767, H: 65535, i: -2147483647, I: 4294967295 };

async function geosoftInflate(data, warnings) {
  if (data.length < GEOSOFT_HEADER + 16) throw new FormatError('compressed Geosoft grid: block table truncated');
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const signature = dv.getInt32(512, true), compType = dv.getInt32(516, true), nBlocks = dv.getInt32(520, true), vpb = dv.getInt32(524, true);
  if (nBlocks <= 0) throw new FormatError(`compressed Geosoft grid: ${nBlocks} blocks`);
  let off = 528;
  const offsets = [], sizes = [];
  for (let k = 0; k < nBlocks; k++) offsets.push(Number(dv.getBigInt64(off + 8 * k, true)));
  off += 8 * nBlocks;
  for (let k = 0; k < nBlocks; k++) sizes.push(dv.getInt32(off + 4 * k, true));
  const out = [];
  for (let k = 0; k < nBlocks; k++) {
    const start = offsets[k], size = sizes[k];
    if (start < 0 || start + size > data.length || size < 16) throw new FormatError(`compressed Geosoft grid: block ${k} (offset ${start}, size ${size}) outside the file`);
    try { out.push(await inflate(data.subarray(start + 16, start + size))); }
    catch (e) {
      // tolerate a size that excludes the 16-byte block header
      try {
        const r = await inflateLenient(data.subarray(start + 16));
        out.push(r.data);
        warnings.push(`compressed block ${k}: size field unreliable, stream read to a natural end`);
      } catch (e2) { throw new FormatError(`compressed Geosoft grid: block ${k} does not inflate (${e2.message})`); }
    }
  }
  return [concatU8(out), { signature, compression_type: compType, n_blocks: nBlocks, vectors_per_block: vpb }];
}

/** Geosoft binary grid (v2, KX = +-1, uncompressed or compressed) -> Promise<Grid2D>. */
export async function readGeosoftGrd(src, opts = {}) {
  const data = toU8(src);
  if (data.length < GEOSOFT_HEADER) throw new FormatError('Geosoft grid: file shorter than the 512-byte header');
  const warnings = [];
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const es = dv.getInt32(0, true), sf = dv.getInt32(4, true), ne = dv.getInt32(8, true), nv = dv.getInt32(12, true), kx = dv.getInt32(16, true);
  const de = dv.getFloat64(20, true), dvv = dv.getFloat64(28, true), x0 = dv.getFloat64(36, true), y0 = dv.getFloat64(44, true), rot = dv.getFloat64(52, true);
  const zbase = dv.getFloat64(60, true);
  let zmult = dv.getFloat64(68, true);
  const cstr = (a, b) => { let end = a; while (end < b && data[end] !== 0) end++; return pyStrip(decodeLatin1(data.subarray(a, end))); };
  const label = cstr(76, 124), mapno = cstr(124, 140);
  const proj = dv.getInt32(140, true), unitx = dv.getInt32(144, true), unity = dv.getInt32(148, true), unitz = dv.getInt32(152, true), nvpts = dv.getInt32(156, true);
  const izmin = dv.getFloat32(160, true), izmax = dv.getFloat32(164, true), izmed = dv.getFloat32(168, true), izmea = dv.getFloat32(172, true);
  const zvar = dv.getFloat64(176, true), prcs = dv.getInt32(184, true);
  const compressed = es > 1024;
  const esPlain = compressed ? es - 1024 : es;
  const tc = geosoftType(esPlain, sf);
  if (tc === null) {
    if (sf === 3) throw new FormatError('Geosoft colour grids (SF=3) are not supported');
    throw new FormatError(`Geosoft grid: unsupported element size / sign flag ES=${es} SF=${sf}`);
  }
  if (kx !== 1 && kx !== -1) throw new FormatError(`Geosoft grid: KX=${kx} not supported (only 1 and -1)`);
  if (ne <= 0 || nv <= 0) throw new FormatError(`Geosoft grid: NE=${ne} NV=${nv} invalid`);
  if (zmult === 0) { warnings.push('ZMULT is 0; treated as 1'); zmult = 1.0; }
  const extra = {};
  let body;
  if (compressed) { const [b, info] = await geosoftInflate(data, warnings); body = b; extra.compression = info; }
  else body = data.subarray(GEOSOFT_HEADER);
  const count = ne * nv, need = count * esPlain;
  if (body.length < need) throw new FormatError(`Geosoft grid: expected ${need} data bytes, found ${body.length}`);
  if (body.length > need) warnings.push(`${body.length - need} trailing bytes after the grid data ignored`);
  const bdv = new DataView(body.buffer, body.byteOffset, body.byteLength);
  const get = { B: o => bdv.getUint8(o), b: o => bdv.getInt8(o), H: o => bdv.getUint16(o, true), h: o => bdv.getInt16(o, true), I: o => bdv.getUint32(o, true), i: o => bdv.getInt32(o, true), f: o => bdv.getFloat32(o, true), d: o => bdv.getFloat64(o, true) }[tc];
  const flat = new Float64Array(count).fill(NAN);
  let dummies = 0;
  if (tc === 'f' || tc === 'd') {
    for (let k = 0; k < count; k++) { const v = get(k * esPlain); if (v <= GEOSOFT_DUMMY || v !== v) dummies++; else flat[k] = v / zmult + zbase; }
  } else {
    const dummy = INT_DUMMY[tc];
    for (let k = 0; k < count; k++) { const v = get(k * esPlain); if (v === dummy) dummies++; else flat[k] = v / zmult + zbase; }
  }
  let nx, ny, dx, dy, values;
  if (kx === 1) { nx = ne; ny = nv; dx = de; dy = dvv; values = flat; }
  else {
    nx = nv; ny = ne; dx = dvv; dy = de;
    values = new Float64Array(count).fill(NAN);
    for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) values[j * nx + i] = flat[i * ne + j];
    if (rot) warnings.push(`KX=-1 with rotation ${pyRepr(rot)}: rotation applied CCW about (X0, Y0) as for KX=1 (unverified for Y-oriented grids)`);
  }
  if (dx <= 0 || dy <= 0) warnings.push(`non-positive spacing DE=${pyRepr(de)} DV=${pyRepr(dvv)}`);
  const g = new GM.Grid2D({ nx, ny, x0, y0, dx, dy, values, rotation: rot, name: label });
  if (!g.name) g.name = opts.name || stem(opts.file, '');
  setProvenance(g, 'geosoft_grd', opts.file);
  g.metadata.warnings = warnings;
  g.metadata.geosoft = { ES: es, SF: sf, NE: ne, NV: nv, KX: kx, DE: de, DV: dvv, X0: x0, Y0: y0, ROT: rot, ZBASE: zbase, ZMULT: zmult, LABEL: label, MAPNO: mapno,
    PROJ: proj, UNITX: unitx, UNITY: unity, UNITZ: unitz, NVPTS: nvpts, IZMIN: izmin, IZMAX: izmax, IZMED: izmed, IZMEA: izmea, ZVAR: zvar, PRCS: prcs, compressed };
  g.metadata.dummy_nodes = dummies;
  Object.assign(g.metadata, extra);
  g.role = 'surface';
  return g;
}

function geosoftStats(values) {
  const zs = [];
  for (let i = 0; i < values.length; i++) if (values[i] === values[i]) zs.push(values[i]);
  zs.sort((a, b) => a - b);
  const n = zs.length;
  if (!n) return [0, 0.0, 0.0, 0.0, 0.0, 0.0];
  const mean = fsum(zs) / n;
  const sq = new Float64Array(n);
  for (let i = 0; i < n; i++) sq[i] = (zs[i] - mean) * (zs[i] - mean);
  const variance = fsum(sq) / n;
  const med = n % 2 ? zs[(n - 1) / 2] : 0.5 * (zs[n / 2 - 1] + zs[n / 2]);
  return [n, zs[0], zs[n - 1], med, mean, variance];
}

/** Grid2D -> uncompressed Geosoft grid. opts.dtype 'float' (ES=4 SF=2) | 'short' (int16 + ZBASE/ZMULT). */
export function writeGeosoftGrd(grid, opts = {}) {
  const dtype = (typeof opts === 'string' ? opts : (opts.dtype || 'float')).toLowerCase();
  if (dtype !== 'float' && dtype !== 'short') throw new FormatError(`dtype must be 'float' or 'short', not ${pyReprStr(dtype)}`);
  const [nValid, zmin, zmax, zmed, zmean, zvar] = geosoftStats(grid.values);
  const n = grid.nx * grid.ny;
  const w = new ByteWriter(GEOSOFT_HEADER + 4 * n + 16);
  let es, sf, zbase, zmult;
  const raw = new ByteWriter(4 * n + 16);
  if (dtype === 'float') {
    es = 4; sf = 2; zbase = 0.0; zmult = 1.0;
    for (let k = 0; k < n; k++) { const v = grid.values[k]; raw.f32(v !== v ? GEOSOFT_DUMMY : v); }
  } else {
    es = 2; sf = 1;
    const span = zmax - zmin;
    zbase = nValid ? zmin + span / 2.0 : 0.0;
    zmult = span > 0 ? (2 * 32765.0) / span : 1.0;
    for (let k = 0; k < n; k++) {
      const v = grid.values[k];
      if (v !== v) raw.i16(-32767);
      else { const s = pyRound((v - zbase) * zmult); raw.i16(Math.max(-32766, Math.min(32767, s))); }
    }
  }
  w.i32(es).i32(sf).i32(grid.nx).i32(grid.ny).i32(1);
  w.f64(grid.dx).f64(grid.dy).f64(grid.x0).f64(grid.y0).f64(grid.rotation).f64(zbase).f64(zmult);
  const label = encodeLatin1(grid.name || '').subarray(0, 48);
  w.bytes(label).zeros(48 - label.length).zeros(16);
  w.i32(0).i32(0).i32(0).i32(0).i32(nValid);
  w.f32(zmin).f32(zmax).f32(zmed).f32(zmean);
  w.f64(zvar).i32(0);
  w.zeros(GEOSOFT_HEADER - w.length);
  w.bytes(raw.result());
  const msg = 'geosoft write_grd: no .gi sidecar written (projection / colour table absent)';
  const warn = grid.metadata.warnings = grid.metadata.warnings || [];
  if (!warn.includes(msg)) warn.push(msg);
  return w.result();
}

/* -------------------------------------------------------------------- GXF */
const GXF_LABEL_RE = /^#\s*([A-Za-z_]+)\s*(.*)$/;
// (horizontal rows, flip x, flip y) per #SENSE
const GXF_SENSE = { 1: [true, false, false], '-1': [false, false, false], 2: [false, false, true], '-2': [true, false, true],
  3: [true, true, true], '-3': [false, true, true], 4: [false, true, false], '-4': [true, true, false] };

function gxfHeader(lines) {
  const hdr = new Map();
  let k = 0, gridAt = null;
  const n = lines.length;
  while (k < n) {
    const m = GXF_LABEL_RE.exec(lines[k]);
    if (!m) { k++; continue; }
    const label = m[1].toUpperCase(), rest = pyStrip(m[2]);
    k++;
    if (label.startsWith('GRID')) { gridAt = k; break; }
    const vals = [];
    if (rest) vals.push(rest);
    let cont = false;
    while (k < n && !lines[k].startsWith('#')) {
      const s = pyRstrip(lines[k]);
      k++;
      if (cont) vals[vals.length - 1] = pyRstrip(vals[vals.length - 1].slice(0, -1)) + pyStrip(s);
      else if (pyStrip(s)) vals.push(pyStrip(s));
      else continue;
      cont = vals[vals.length - 1].endsWith('\\');
    }
    if (!hdr.has(label)) hdr.set(label, vals);
  }
  return [hdr, gridAt];
}
function gxfValue(hdr, ...names) {
  for (const nm of names) for (const [k, v] of hdr) if (k.startsWith(nm) && v.length) return v[0];
  return null;
}
function gxfFields(text) {
  const out = [];
  const re = /"([^"]*)"|([^,\s"]+)/g;
  let m;
  while ((m = re.exec(text || '')) !== null) out.push(m[1] !== undefined ? m[1] : m[2]);
  return out;
}
export function gxfBase90(chunk) {
  let v = 0;
  for (let i = 0; i < chunk.length; i++) {
    const d = chunk.charCodeAt(i) - 37;
    if (d < 0 || d > 89) throw new FormatError(`GXF: ${pyReprStr(chunk[i])} is not a base-90 digit`);
    v = v * 90 + d;
  }
  return v;
}
function gxfValuesCompressed(lines, gtype, count, scale, offset, warnings) {
  let stream = '';
  for (const ln of lines) if (!ln.startsWith('$')) stream += pyStrip(ln);
  const out = new Float64Array(count);
  let len = 0, pos = 0, dummies = 0;
  const total = stream.length;
  while (len < count) {
    if (pos + gtype > total) throw new FormatError(`GXF: compressed data ends after ${len} of ${count} values`);
    const chunk = stream.slice(pos, pos + gtype);
    pos += gtype;
    if (chunk[0] === '!') { out[len++] = NAN; dummies++; }
    else if (chunk[0] === '"') {
      if (pos + 2 * gtype > total) throw new FormatError(`GXF: truncated repeat code at offset ${pos}`);
      let rep = gxfBase90(stream.slice(pos, pos + gtype));
      pos += gtype;
      const vchunk = stream.slice(pos, pos + gtype);
      pos += gtype;
      let val;
      if (vchunk[0] === '!') { val = NAN; dummies += rep; } else val = gxfBase90(vchunk) * scale + offset;
      if (len + rep > count) { warnings.push(`GXF: repeat run of ${rep} overflows the grid; clipped`); rep = count - len; }
      for (let r = 0; r < rep; r++) out[len++] = val;
    } else out[len++] = gxfBase90(chunk) * scale + offset;
  }
  if (pos < total) warnings.push(`GXF: ${total - pos} trailing compressed characters ignored`);
  return [out, dummies];
}
function gxfValuesPlain(lines, count, dummyText, dummyValue, scale, offset, warnings) {
  const out = new Float64Array(count);
  let len = 0, dummies = 0;
  const tol = 1e-9 * Math.max(1.0, Math.abs(dummyValue));
  const apply = scale !== 1.0 || offset !== 0.0;
  for (const ln of lines) {
    if (len >= count) break;
    for (const tok of pySplit(ln)) {
      if (len >= count) break;
      if (tok === dummyText) { out[len++] = NAN; dummies++; continue; }
      const v = pyFloat(tok);
      if (v === undefined) throw new FormatError(`GXF: non-numeric grid value ${pyReprStr(tok)}`);
      if (Math.abs(v - dummyValue) <= tol) { out[len++] = NAN; dummies++; }
      else out[len++] = apply ? v * scale + offset : v;
    }
  }
  if (len < count) throw new FormatError(`GXF: expected ${count} grid values, found ${len}`);
  return [out, dummies];
}
/** GXF (uncompressed or base-90 compressed, any #SENSE) -> Grid2D with (x0, y0) at the bottom-left corner. */
export function readGxf(src, opts = {}) {
  const lines = pySplitlines(asText(src));
  const [hdr, gridAt] = gxfHeader(lines);
  if (gridAt === null) throw new FormatError('GXF: no #GRID object found');
  const warnings = [];
  const num = (label, dflt, ...alts) => {
    const v = gxfValue(hdr, label, ...alts);
    if (v === null) return dflt;
    const f = gxfFields(v);
    const x = f.length ? pyFloat(f[0]) : undefined;
    if (x === undefined) { warnings.push(`GXF: ${label} value ${pyReprStr(v)} not numeric; default ${pyRepr(dflt)} used`); return dflt; }
    return x;
  };
  const points = Math.trunc(num('POINTS', 0)), rows = Math.trunc(num('ROWS', 0));
  if (points <= 0 || rows <= 0) throw new FormatError(`GXF: #POINTS (${points}) and #ROWS (${rows}) are required`);
  const ptsep = num('PTSEPARATION', 1.0, 'PTSE'), rwsep = num('RWSEPARATION', 1.0, 'RWSE');
  const xorigin = num('XORIGIN', 0.0, 'XORI'), yorigin = num('YORIGIN', 0.0, 'YORI');
  const rotation = num('ROTATION', 0.0, 'ROTA');
  const sense = Math.trunc(num('SENSE', 1, 'SENS'));
  const gtype = Math.trunc(num('GTYPE', 0));
  let title = gxfValue(hdr, 'TITLE', 'TITL') || '';
  if (title.startsWith('"') && title.endsWith('"') && title.length >= 2) title = title.slice(1, -1);
  let scale = 1.0, offset = 0.0, tname = '';
  const tv = gxfValue(hdr, 'TRANSFORM', 'TRAN');
  if (tv !== null) {
    const f = gxfFields(tv);
    const s = f.length ? pyFloat(f[0]) : undefined;
    const o = f.length > 1 ? pyFloat(f[1]) : 0.0;
    if (s === undefined || o === undefined) { warnings.push(`GXF: #TRANSFORM ${pyReprStr(tv)} not understood; identity used`); scale = 1.0; offset = 0.0; }
    else { scale = s; offset = o; }
    if (f.length > 2) tname = f[2];
  }
  let dummyText = gxfValue(hdr, 'DUMMY', 'DUMM');
  let dummyValue;
  if (dummyText === null) { dummyValue = GXF_DEFAULT_DUMMY; dummyText = ''; }
  else {
    const f = gxfFields(dummyText);
    dummyText = f.length ? f[0] : dummyText;
    const dvl = pyFloat(dummyText);
    if (dvl === undefined) { warnings.push(`GXF: #DUMMY ${pyReprStr(dummyText)} not numeric; matched as text only`); dummyValue = Infinity; }
    else dummyValue = dvl;
  }
  let units = 'm', unitScale = 1.0;
  const uv = gxfValue(hdr, 'UNIT_LENGTH', 'UNIT');
  if (uv !== null) {
    const f = gxfFields(uv);
    if (f.length) units = f[0];
    if (f.length > 1) { const us = pyFloat(f[1]); if (us !== undefined) unitScale = us; }
  }
  const senseSpec = GXF_SENSE[String(sense)];
  if (!senseSpec) throw new FormatError(`GXF: #SENSE ${sense} is not one of +-1..+-4`);
  const count = points * rows;
  const body = lines.slice(gridAt);
  let flat, dummies;
  if (gtype > 0) [flat, dummies] = gxfValuesCompressed(body, gtype, count, scale, offset, warnings);
  else {
    [flat, dummies] = gxfValuesPlain(body, count, dummyText, dummyValue, scale, offset, warnings);
    if (scale !== 1.0 || offset !== 0.0) warnings.push(`GXF: #TRANSFORM ${pyRepr(scale)},${pyRepr(offset)} applied to uncompressed values per the spec (GDAL ignores it)`);
  }
  const [horizontal, flipX, flipY] = senseSpec;
  let nx, ny, dx, dy;
  if (horizontal) { nx = points; ny = rows; dx = ptsep; dy = rwsep; } else { nx = rows; ny = points; dx = rwsep; dy = ptsep; }
  const values = new Float64Array(count).fill(NAN);
  for (let r = 0; r < rows; r++) {
    const base = r * points;
    for (let p = 0; p < points; p++) {
      let i, j;
      if (horizontal) { i = p; j = r; } else { i = r; j = p; }
      if (flipX) i = nx - 1 - i;
      if (flipY) j = ny - 1 - j;
      values[j * nx + i] = flat[base + p];
    }
  }
  if (sense !== 1) warnings.push(`GXF: #SENSE ${sense} re-ordered to south-west-first; #XORIGIN/#YORIGIN taken as the bottom-left corner per the GXF spec (GDAL would take them as the first stored point)`);
  const g = new GM.Grid2D({ nx, ny, x0: xorigin, y0: yorigin, dx, dy, values, rotation, units, name: title });
  if (!g.name) g.name = opts.name || stem(opts.file, '');
  setProvenance(g, 'gxf', opts.file);
  g.metadata.warnings = warnings;
  g.metadata.gxf = { sense, gtype, points, rows, transform: [scale, offset, tname], dummy: dummyText || null, unit_length: [units, unitScale],
    map_projection: hdr.get('MAP_PROJECTION') || null, map_datum_transform: hdr.get('MAP_DATUM_TRANSFORM') || null };
  g.metadata.dummy_nodes = dummies;
  g.role = 'surface';
  return g;
}
/** Grid2D -> uncompressed GXF (SENSE 1, #DUMMY -1e+32, lines <= 80 chars). */
export function writeGxf(grid) {
  if (grid.dx <= 0 || grid.dy <= 0) throw new FormatError(`grid spacing must be positive (dx=${pyRepr(grid.dx)}, dy=${pyRepr(grid.dy)})`);
  const unit = grid.units || 'm';
  const unitScale = { m: 1.0, ft: 0.3048, ftUS: 0.3048006096012, km: 1000.0 }[unit] || 1.0;
  const out = ['#TITLE', (grid.name || 'grid').slice(0, 78), '#POINTS', String(grid.nx), '#ROWS', String(grid.ny),
    '#PTSEPARATION', pyRepr(grid.dx), '#RWSEPARATION', pyRepr(grid.dy), '#XORIGIN', pyRepr(grid.x0), '#YORIGIN', pyRepr(grid.y0),
    '#ROTATION', pyRepr(grid.rotation), '#SENSE', '1', '#UNIT_LENGTH', `${unit},${pyRepr(unitScale)}`, '#DUMMY', '-1e+32', '#GRID'];
  const nx = grid.nx, vals = grid.values;
  for (let j = 0; j < grid.ny; j++) {
    let line = '';
    for (let i = 0; i < nx; i++) {
      const v = vals[j * nx + i];
      const tok = v !== v ? '-1e+32' : pyRepr(v);
      if (line && line.length + 1 + tok.length > 80) { out.push(line); line = tok; }
      else line = line ? line + ' ' + tok : tok;
    }
    out.push(line);
  }
  return encodeAscii(out.join('\n') + '\n');
}

/* ------------------------------------------------------------ Geosoft XYZ */
const LINE_ABBREV = { l: 'Line', t: 'Tie', b: 'Base', r: 'Random', s: 'Special', d: 'Trend' };
const LINE_HEADER_RE = /^([A-Za-z]+)\s*(.*)$/;
function splitChannels(text) {
  const s = pyStrip(text);
  if (s.indexOf(',') >= 0) return s.split(',').map(pyStrip).filter(t => t);
  return pySplit(s);
}
function lineHeader(s) {
  const m = LINE_HEADER_RE.exec(s);
  if (!m) return null;
  let kind = m[1];
  const rest = pyStrip(m[2]);
  if (kind.length === 1 && rest && /\d/.test(rest[0])) kind = LINE_ABBREV[kind.toLowerCase()] || kind;
  kind = kind[0].toUpperCase() + kind.slice(1).toLowerCase();
  return [kind, rest ? rest : kind];
}
function numericTokens(toks) { for (const t of toks) { if (t === '*') continue; if (pyFloat(t) === undefined) return false; } return true; }

/** Geosoft XYZ export -> PointSet. opts.x/y/z name the coordinate channels (default X/Y/Z). */
export function readGeosoftXyz(src, opts = {}) {
  const xName = opts.x || 'X', yName = opts.y || 'Y', zName = opts.z || 'Z';
  const text = asText(src);
  const warnings = [];
  const candidates = [];
  const rows = [];
  let lineName = '', lineType = '', flight = null, date = null, seenData = false;
  for (const raw of pySplitlines(text)) {
    const s = pyStrip(raw);
    if (!s) continue;
    if (s.startsWith('//')) {
      const body = pyStrip(s.slice(2));
      const low = body.toLowerCase();
      if (low.startsWith('flight')) flight = pyStrip(pyStrip(body.slice(6)).replace(/^:+/, '')) || null;
      else if (low.startsWith('date')) date = pyStrip(pyStrip(body.slice(4)).replace(/^:+/, '')) || null;
      continue;
    }
    if (s.startsWith('/')) {
      const body = pyStrip(s.slice(1));
      if (!body || /^[=\-_ ]*$/.test(body)) continue;
      const toks = splitChannels(body);
      if (!seenData && toks.length >= 2 && !numericTokens(toks)) candidates.push(toks);
      continue;
    }
    const header = lineHeader(s);
    if (header !== null) { lineType = header[0]; lineName = header[1]; flight = null; date = null; continue; }
    rows.push([lineName, lineType, flight, date, pySplit(s.replace(/,/g, ' '))]);
    seenData = true;
  }
  if (!rows.length) throw new FormatError('Geosoft XYZ: no data rows found');
  let ncol = 0;
  for (const r of rows) if (r[4].length > ncol) ncol = r[4].length;
  let channels = null;
  for (let c = candidates.length - 1; c >= 0; c--) {
    const up = candidates[c].map(t => t.toUpperCase());
    if (up.includes(xName.toUpperCase()) && up.includes(yName.toUpperCase())) { channels = candidates[c].slice(); break; }
  }
  if (channels === null && candidates.length) { channels = candidates[candidates.length - 1].slice(); warnings.push(`channel header guessed from comment ${pyReprStr(channels.join(' '))}`); }
  if (channels === null) {
    channels = ['X', 'Y', 'Z'].slice(0, ncol);
    for (let k = 4; k <= ncol; k++) channels.push('ch' + k);
    warnings.push(`no channel header line; columns named ${channels.join(', ')}`);
  }
  if (channels.length !== ncol) {
    warnings.push(`channel header lists ${channels.length} names but rows have up to ${ncol} columns`);
    while (channels.length < ncol) channels.push('ch' + (channels.length + 1));
  }
  const upper = channels.map(c => c.toUpperCase());
  const col = name => { const i = name ? upper.indexOf(name.toUpperCase()) : -1; return i < 0 ? null : i; };
  const ix = col(xName), iy = col(yName), iz = col(zName);
  if (ix === null || iy === null) throw new FormatError(`Geosoft XYZ: coordinate channels ${pyReprStr(xName)}/${pyReprStr(yName)} not in [${channels.join(', ')}]`);
  if (iz === null) warnings.push(`no ${pyReprStr(zName)} channel; z set to 0`);
  const attrs = {};
  channels.forEach((c, k) => { if (k !== ix && k !== iy && k !== iz) attrs[c] = []; });
  attrs.line = []; attrs.line_type = [];
  const hasFlight = rows.some(r => r[2] !== null), hasDate = rows.some(r => r[3] !== null);
  if (hasFlight) attrs.flight = [];
  if (hasDate) attrs.date = [];
  const xyz = new Float64Array(3 * rows.length);
  let dummies = 0, short = 0;
  rows.forEach((row, ri) => {
    const [ln, lt, fl, dt, toks] = row;
    const vals = new Array(ncol);
    for (let k = 0; k < ncol; k++) {
      const t = k < toks.length ? toks[k] : '*';
      if (t === '*' || t === '') { vals[k] = NAN; dummies++; }
      else { const f = pyFloat(t); if (f === undefined) { vals[k] = NAN; dummies++; } else vals[k] = f; }
    }
    if (toks.length < ncol) short++;
    xyz[3 * ri] = vals[ix]; xyz[3 * ri + 1] = vals[iy]; xyz[3 * ri + 2] = iz !== null ? vals[iz] : 0.0;
    channels.forEach((c, k) => { if (k === ix || k === iy || k === iz) return; const v = vals[k]; attrs[c].push(v !== v ? null : v); });
    attrs.line.push(ln); attrs.line_type.push(lt);
    if (hasFlight) attrs.flight.push(fl);
    if (hasDate) attrs.date.push(dt);
  });
  if (short) warnings.push(`${short} rows shorter than ${ncol} columns (padded with dummies)`);
  const ps = new GM.PointSet({ xyz, attributes: attrs, role: 'points' });
  setProvenance(ps, 'geosoft_xyz', opts.file);
  ps.metadata.warnings = warnings;
  ps.metadata.channels = channels;
  ps.metadata.dummies = dummies;
  ps.name = opts.name || stem(opts.file, '');
  return ps;
}
function xyzNum(v) {
  if (v === null || v === undefined || v === '') return '*';
  if (typeof v === 'string') { const f = pyFloat(v); if (f === undefined) return v.replace(/ /g, '_'); v = f; }
  if (typeof v === 'boolean') v = v ? 1 : 0;
  if (v !== v) return '*';
  return pyRepr(v);
}
/** PointSet -> Geosoft XYZ. opts: lineCol ('line'), typeCol ('line_type'), columns. */
export function writeGeosoftXyz(points, opts = {}) {
  const lineCol = opts.lineCol || 'line', typeCol = opts.typeCol || 'line_type';
  const n = points.n;
  const columns = opts.columns ? Array.from(opts.columns) : Object.keys(points.attributes).filter(c => c !== lineCol && c !== typeCol && c !== 'flight' && c !== 'date');
  const cols = columns.map(c => { const col = points.attributes[c] || []; const out = Array.from(col); while (out.length < n) out.push(null); return out; });
  const pad = k => { const out = Array.from(points.attributes[k] || []); while (out.length < n) out.push(null); return out; };
  const linesAttr = pad(lineCol), typesAttr = pad(typeCol), flights = pad('flight'), dates = pad('date');
  const out = ['/ ' + ['X', 'Y', 'Z'].concat(columns.map(c => String(c).replace(/ /g, '_'))).join(' '), '/' + '='.repeat(60)];
  let cur = null, curFl = {}, curDt = {};
  const blank = v => v === null || v === undefined || v === '';
  for (let k = 0; k < n; k++) {
    const key = '\x00KEY' + String(linesAttr[k]) + '\x01' + String(typesAttr[k]);
    if (key !== cur) {
      cur = key;
      const name = linesAttr[k];
      let kind = typesAttr[k];
      kind = blank(kind) ? 'Line' : (pyStrip(String(kind)) || 'Line');
      kind = kind[0].toUpperCase() + kind.slice(1).toLowerCase();
      if (blank(name)) out.push(kind);
      else { const nm = String(name); out.push(nm.toLowerCase() === kind.toLowerCase() ? kind : `${kind} ${nm}`); }
      curFl = {}; curDt = {};
    }
    if (flights[k] !== curFl && !blank(flights[k])) out.push(`//Flight ${flights[k]}`);
    curFl = flights[k];
    if (dates[k] !== curDt && !blank(dates[k])) out.push(`//Date ${dates[k]}`);
    curDt = dates[k];
    const [px, py, pz] = points.point(k);
    const fields = [xyzNum(px), xyzNum(py), xyzNum(pz)];
    for (const col of cols) fields.push(xyzNum(col[k]));
    out.push(fields.join(' '));
  }
  return encodeLatin1(out.join('\n') + '\n');
}

/* ========================================================================
   arcascii — Arc/Info ASCII grid (ncols nrows xll{corner|center} yll..
   cellsize [dx dy] NODATA_value; north row first; cells are areas so node
   x0 = xllcorner + cellsize/2)
   ======================================================================== */
const ASC_KEYS = new Set(['ncols', 'nrows', 'xllcorner', 'xllcenter', 'yllcorner', 'yllcenter', 'cellsize', 'dx', 'dy', 'nodata_value']);
export function readAsc(src, opts = {}) {
  const lines = pySplitlines(asText(src));
  const warnings = [];
  const hdr = {};
  let k = 0;
  while (k < lines.length) {
    const s = pyStrip(lines[k]);
    if (!s) { k++; continue; }
    const parts = pySplit(s);
    const key = parts[0].toLowerCase();
    if (ASC_KEYS.has(key) && parts.length >= 2) {
      const v = pyFloat(parts[1]);
      if (v === undefined) throw new FormatError(`Arc ASCII: header ${parts[0]} has non-numeric value ${pyReprStr(parts[1])}`);
      hdr[key] = v;
      k++;
      continue;
    }
    break;
  }
  for (const need of ['ncols', 'nrows']) if (!(need in hdr)) throw new FormatError(`Arc ASCII: header lacks ${need}`);
  const nx = Math.trunc(hdr.ncols), ny = Math.trunc(hdr.nrows);
  if (nx <= 0 || ny <= 0) throw new FormatError(`Arc ASCII: grid size ${nx}x${ny} invalid`);
  let dx, dy;
  if ('cellsize' in hdr) { dx = dy = hdr.cellsize; if ('dx' in hdr || 'dy' in hdr) warnings.push('both cellsize and dx/dy given; cellsize used'); }
  else if ('dx' in hdr && 'dy' in hdr) { dx = hdr.dx; dy = hdr.dy; }
  else throw new FormatError('Arc ASCII: header lacks cellsize (or dx/dy)');
  if (dx <= 0 || dy <= 0) throw new FormatError(`Arc ASCII: cellsize must be positive (dx=${pyRepr(dx)} dy=${pyRepr(dy)})`);
  let x0, y0;
  if ('xllcenter' in hdr) x0 = hdr.xllcenter;
  else if ('xllcorner' in hdr) x0 = hdr.xllcorner + dx / 2.0;
  else { x0 = dx / 2.0; warnings.push('no xllcorner/xllcenter; x origin assumed 0'); }
  if ('yllcenter' in hdr) y0 = hdr.yllcenter;
  else if ('yllcorner' in hdr) y0 = hdr.yllcorner + dy / 2.0;
  else { y0 = dy / 2.0; warnings.push('no yllcorner/yllcenter; y origin assumed 0'); }
  const nodata = 'nodata_value' in hdr ? hdr.nodata_value : null;
  if (nodata === null) warnings.push('no NODATA_value in header; none applied');
  const tokens = pySplit(lines.slice(k).join(' '));
  const n = nx * ny;
  if (tokens.length < n) throw new FormatError(`Arc ASCII: expected ${n} values, found ${tokens.length}`);
  if (tokens.length > n) warnings.push(`${tokens.length - n} trailing tokens ignored`);
  const values = new Float64Array(n).fill(NAN);
  let blanks = 0;
  const tol = nodata !== null ? 1e-9 * Math.max(1.0, Math.abs(nodata)) : 0.0;
  for (let r = 0; r < ny; r++) {
    const j = ny - 1 - r, base = r * nx;
    for (let i = 0; i < nx; i++) {
      const t = tokens[base + i];
      const v = pyFloat(t);
      if (v === undefined) throw new FormatError(`Arc ASCII: non-numeric value ${pyReprStr(t)}`);
      if ((nodata !== null && Math.abs(v - nodata) <= tol) || v !== v) { blanks++; continue; }
      values[j * nx + i] = v;
    }
  }
  const g = new GM.Grid2D({ nx, ny, x0, y0, dx, dy, values });
  g.name = opts.name || stem(opts.file, '');
  setProvenance(g, 'arc_ascii', opts.file);
  g.metadata.warnings = warnings;
  g.metadata.nodata_value = nodata;
  g.metadata.nodata_nodes = blanks;
  g.role = 'surface';
  return g;
}
/** Grid2D -> Arc/Info ASCII grid (xllcorner form, north row first). Square cells, no rotation. */
export function writeAsc(grid, opts = {}) {
  let nodata = opts.nodata === undefined ? -9999.0 : +opts.nodata;
  if (grid.rotation) throw new FormatError(`Arc ASCII grids cannot be rotated (grid.rotation=${pyRepr(grid.rotation)}); resample to an axis-aligned grid first`);
  if (Math.abs(grid.dx - grid.dy) > 1e-9 * Math.max(Math.abs(grid.dx), Math.abs(grid.dy), 1.0)) throw new FormatError(`Arc ASCII needs square cells but dx=${pyRepr(grid.dx)} != dy=${pyRepr(grid.dy)}; resample the grid to a common cellsize (e.g. min(dx, dy)) before writing`);
  if (grid.dx <= 0) throw new FormatError(`cellsize must be positive (dx=${pyRepr(grid.dx)})`);
  const valid = new Set();
  for (let i = 0; i < grid.values.length; i++) if (grid.values[i] === grid.values[i]) valid.add(grid.values[i]);
  if (valid.has(nodata)) for (const cand of [-99999.0, -999999.0, -1e32]) if (!valid.has(cand)) { nodata = cand; break; }
  const cell = grid.dx;
  const out = [`ncols        ${grid.nx}`, `nrows        ${grid.ny}`, `xllcorner    ${pyRepr(grid.x0 - cell / 2.0)}`, `yllcorner    ${pyRepr(grid.y0 - cell / 2.0)}`,
    `cellsize     ${pyRepr(cell)}`, `NODATA_value ${pyRepr(nodata)}`];
  const nd = pyRepr(nodata), nx = grid.nx, vals = grid.values;
  for (let j = grid.ny - 1; j >= 0; j--) {
    const row = [];
    for (let i = 0; i < nx; i++) { const v = vals[j * nx + i]; row.push(v !== v ? nd : pyRepr(v)); }
    out.push(row.join(' '));
  }
  return encodeAscii(out.join('\n') + '\n');
}

/* ========================================================================
   zmap — ZMAP+ ASCII grid (column-major, each column north -> south)
   ======================================================================== */
function zmapCsv(line) { return line.split(',').map(pyStrip); }
function zmapNum(tok, what) { const v = pyFloat(tok); if (v === undefined) throw new FormatError(`ZMAP: ${what} is not numeric: ${pyReprStr(tok)}`); return v; }
function zmapLineTokens(s, width, nullText) {
  if (width > 0 && s && s.length % width === 0) {
    const out = [];
    let ok = true;
    for (let k = 0; k < s.length; k += width) {
      const t = pyStrip(s.slice(k, k + width));
      if (!t || (nullText && t === nullText)) { out.push(null); continue; }
      const v = pyFloat(t);
      if (v === undefined) { ok = false; break; }
      out.push(v);
    }
    if (ok) return out;
  }
  const out = [];
  for (const t of pySplit(s)) {
    if (nullText && t === nullText) { out.push(null); continue; }
    const v = pyFloat(t);
    if (v === undefined) throw new FormatError(`ZMAP: non-numeric grid value ${pyReprStr(t)}`);
    out.push(v);
  }
  return out;
}
export function readZmap(src, opts = {}) {
  const lines = pySplitlines(asText(src));
  const warnings = [];
  let k = 0;
  const n = lines.length;
  while (k < n && !pyLstrip(lines[k]).startsWith('@')) k++;
  if (k >= n) throw new FormatError('ZMAP: no @ header line found');
  const head = zmapCsv(pyLstrip(lines[k]).slice(1));
  const name = head.length ? head[0] : '';
  const kind = head.length > 1 ? head[1].toUpperCase() : 'GRID';
  if (kind !== 'GRID') warnings.push(`header type ${pyReprStr(kind)} is not GRID`);
  let nodesPerLine = null;
  if (head.length > 2 && head[2]) { const v = pyFloat(head[2]); if (v === undefined) warnings.push(`nodes-per-line ${pyReprStr(head[2])} not numeric`); else nodesPerLine = Math.trunc(v); }
  k++;
  const hdr = [];
  while (k < n && hdr.length < 3) {
    const s = pyStrip(lines[k]);
    k++;
    if (!s || s.startsWith('!')) continue;
    if (s.startsWith('@')) break;
    hdr.push(zmapCsv(s));
  }
  if (hdr.length < 2) throw new FormatError(`ZMAP: header block incomplete (${hdr.length} of 3 lines)`);
  const f1 = hdr[0].concat(['', '', '', '', '']);
  const width = f1[0] ? Math.trunc(zmapNum(f1[0], 'field width')) : 0;
  const nullValue = f1[1] ? zmapNum(f1[1], 'null value') : null;
  const nullText = f1[2];
  const decimals = f1[3] ? Math.trunc(zmapNum(f1[3], 'decimals')) : null;
  const f2 = hdr[1].concat(['', '', '', '', '', '']);
  const nrows = Math.trunc(zmapNum(f2[0], 'nrows')), ncols = Math.trunc(zmapNum(f2[1], 'ncols'));
  const xmin = zmapNum(f2[2], 'xmin'), xmax = zmapNum(f2[3], 'xmax'), ymin = zmapNum(f2[4], 'ymin'), ymax = zmapNum(f2[5], 'ymax');
  if (nrows <= 0 || ncols <= 0) throw new FormatError(`ZMAP: grid size ${ncols} cols x ${nrows} rows invalid`);
  while (k < n && !pyLstrip(lines[k]).startsWith('@')) k++;
  if (k >= n) throw new FormatError('ZMAP: header not closed with @');
  k++;
  const count = nrows * ncols;
  const flat = [];
  while (k < n && flat.length < count) {
    const s = lines[k];
    k++;
    if (!pyStrip(s) || pyLstrip(s).startsWith('!')) continue;
    for (const v of zmapLineTokens(s, width, nullText)) flat.push(v);
  }
  if (flat.length < count) throw new FormatError(`ZMAP: expected ${count} values, found ${flat.length}`);
  if (flat.length > count) warnings.push(`${flat.length - count} trailing values ignored`);
  const tol = nullValue !== null ? 1e-9 * Math.max(1.0, Math.abs(nullValue)) : 0.0;
  const values = new Float64Array(count).fill(NAN);
  let nulls = 0;
  const nx = ncols, ny = nrows;
  for (let i = 0; i < nx; i++) {
    const base = i * ny;
    for (let r = 0; r < ny; r++) {
      const v = flat[base + r], j = ny - 1 - r;
      if (v === null || v !== v || (nullValue !== null && Math.abs(v - nullValue) <= tol)) { nulls++; continue; }
      values[j * nx + i] = v;
    }
  }
  const dx = nx > 1 ? (xmax - xmin) / (nx - 1) : 1.0, dy = ny > 1 ? (ymax - ymin) / (ny - 1) : 1.0;
  if (nx === 1 || ny === 1) warnings.push('single row/column: spacing undefined, set to 1.0');
  if (dx <= 0 || dy <= 0) warnings.push(`non-positive spacing dx=${pyRepr(dx)} dy=${pyRepr(dy)}`);
  const g = new GM.Grid2D({ nx, ny, x0: xmin, y0: ymin, dx, dy, values, name: pyStrip(name) });
  if (!g.name) g.name = opts.name || stem(opts.file, '');
  setProvenance(g, 'zmap', opts.file);
  g.metadata.warnings = warnings;
  g.metadata.zmap = { field_width: width, null_value: nullValue, null_text: nullText, decimals, nodes_per_line: nodesPerLine };
  g.metadata.null_nodes = nulls;
  g.role = 'surface';
  return g;
}
function zmapField(v, width, decimals) {
  let s = pyFixed(v, decimals);
  if (s.length > width) s = pyExp(v, Math.max(1, width - 8));
  if (s.length > width) throw new FormatError(`ZMAP: value ${pyRepr(v)} does not fit in a ${width}-character field`);
  return padLeft(s, width);
}
/** Grid2D -> ZMAP+ grid. opts: nodesPerLine 5, fieldWidth 20, decimals 7, nullValue -9999. */
export function writeZmap(grid, opts = {}) {
  if (grid.rotation) throw new FormatError(`ZMAP grids cannot be rotated (grid.rotation=${pyRepr(grid.rotation)}); resample to an axis-aligned grid first`);
  if (grid.dx <= 0 || grid.dy <= 0) throw new FormatError(`grid spacing must be positive (dx=${pyRepr(grid.dx)}, dy=${pyRepr(grid.dy)})`);
  const nodesPerLine = Math.max(1, Math.trunc(opts.nodesPerLine === undefined ? 5 : opts.nodesPerLine));
  const fieldWidth = Math.max(8, Math.trunc(opts.fieldWidth === undefined ? 20 : opts.fieldWidth));
  const decimals = Math.max(0, Math.trunc(opts.decimals === undefined ? 7 : opts.decimals));
  const nul = +(opts.nullValue === undefined ? -9999.0 : opts.nullValue);
  const name = pyStrip((grid.name || 'grid').replace(/,/g, ' ').replace(/\n/g, ' ')) || 'grid';
  const out = ['! ZMAP+ grid written by nwmm geomodel', `! ${name}`, `@${name}, GRID, ${nodesPerLine}`,
    `${fieldWidth}, ${pyStrip(zmapField(nul, fieldWidth, decimals))}, , ${decimals}, 1`,
    `${grid.ny}, ${grid.nx}, ${pyStrip(zmapField(grid.x0, fieldWidth, decimals))}, ${pyStrip(zmapField(grid.xmax, fieldWidth, decimals))}, ${pyStrip(zmapField(grid.y0, fieldWidth, decimals))}, ${pyStrip(zmapField(grid.ymax, fieldWidth, decimals))}`,
    '0.0, 0.0, 0.0', '@'];
  const nullTxt = zmapField(nul, fieldWidth, decimals);
  const nx = grid.nx, ny = grid.ny;
  for (let i = 0; i < nx; i++) {
    const col = [];
    for (let j = ny - 1; j >= 0; j--) { const v = grid.values[j * nx + i]; col.push(v !== v ? nullTxt : zmapField(v, fieldWidth, decimals)); }
    for (let k = 0; k < ny; k += nodesPerLine) out.push(col.slice(k, k + nodesPerLine).join(''));
  }
  return encodeAscii(out.join('\n') + '\n');
}

/* ========================================================================
   irap — Irap classic ASCII grid (-996 header, row-major south first)
   ======================================================================== */
export const IRAP_UNDEF = 9999900.0;
export function readIrap(src, opts = {}) {
  const tokens = pySplit(asText(src));
  if (tokens.length < 19) throw new FormatError(`Irap: header truncated (${tokens.length} tokens)`);
  const warnings = [];
  const head = tokens.slice(0, 19).map(pyFloat);
  if (head.some(v => v === undefined)) throw new FormatError('Irap: header not numeric');
  const magic = Math.trunc(head[0]);
  if (magic !== -996) throw new FormatError(`Irap: first value ${magic} is not -996 (not an Irap classic ASCII grid)`);
  const nrow = Math.trunc(head[1]);
  const xinc = head[2], yinc = head[3];
  const [xori, xmax, yori, ymax] = head.slice(4, 8);
  const ncol = Math.trunc(head[8]);
  const rotation = head[9];
  const rotX = head[10], rotY = head[11];
  const flags = head.slice(12, 19);
  if (flags.some(f => f)) warnings.push(`non-zero fourth header line [${flags.map(pyRepr).join(', ')}]`);
  if (ncol <= 0 || nrow <= 0) throw new FormatError(`Irap: grid size ${ncol} x ${nrow} invalid`);
  if (xinc <= 0 || yinc <= 0) throw new FormatError(`Irap: increments must be positive (xinc=${pyRepr(xinc)} yinc=${pyRepr(yinc)})`);
  if (rotX !== xori || rotY !== yori) warnings.push(`rotation origin (${pyRepr(rotX)}, ${pyRepr(rotY)}) differs from xori/yori (${pyRepr(xori)}, ${pyRepr(yori)}); rotation applied about xori/yori`);
  const expXmax = xori + (ncol - 1) * xinc, expYmax = yori + (nrow - 1) * yinc;
  if (Math.abs(expXmax - xmax) > 1e-6 * Math.max(1.0, Math.abs(xmax)) || Math.abs(expYmax - ymax) > 1e-6 * Math.max(1.0, Math.abs(ymax)))
    warnings.push(`xmax/ymax (${pyRepr(xmax)}, ${pyRepr(ymax)}) inconsistent with origin + (n-1)*inc (${pyRepr(expXmax)}, ${pyRepr(expYmax)}); increments trusted`);
  const count = ncol * nrow;
  const nvals = tokens.length - 19;
  if (nvals < count) throw new FormatError(`Irap: expected ${count} values, found ${nvals}`);
  if (nvals > count) warnings.push(`${nvals - count} trailing tokens ignored`);
  const values = new Float64Array(count);
  let undef = 0;
  for (let k = 0; k < count; k++) {
    const v = pyFloat(tokens[19 + k]);
    if (v === undefined) throw new FormatError(`Irap: non-numeric value ${pyReprStr(tokens[19 + k])}`);
    if (v >= IRAP_UNDEF - 1e-3 || v !== v) { values[k] = NAN; undef++; } else values[k] = v;
  }
  const g = new GM.Grid2D({ nx: ncol, ny: nrow, x0: xori, y0: yori, dx: xinc, dy: yinc, values, rotation });
  g.name = opts.name || stem(opts.file, '');
  setProvenance(g, 'irap', opts.file);
  g.metadata.warnings = warnings;
  g.metadata.irap = { xmax, ymax, rotation_origin: [rotX, rotY] };
  g.metadata.undefined_nodes = undef;
  g.role = 'surface';
  return g;
}
export function writeIrap(grid, opts = {}) {
  if (grid.dx <= 0 || grid.dy <= 0) throw new FormatError(`grid spacing must be positive (dx=${pyRepr(grid.dx)}, dy=${pyRepr(grid.dy)})`);
  const perLine = Math.max(1, Math.trunc(opts.perLine === undefined ? 6 : opts.perLine));
  const out = [`-996 ${grid.ny} ${pyRepr(grid.dx)} ${pyRepr(grid.dy)}`, `${pyRepr(grid.x0)} ${pyRepr(grid.xmax)} ${pyRepr(grid.y0)} ${pyRepr(grid.ymax)}`,
    `${grid.nx} ${pyRepr(grid.rotation)} ${pyRepr(grid.x0)} ${pyRepr(grid.y0)}`, '0 0 0 0 0 0 0'];
  const undef = pyRepr(IRAP_UNDEF), vals = grid.values;
  for (let k = 0; k < vals.length; k += perLine) {
    const row = [];
    for (let i = k; i < Math.min(vals.length, k + perLine); i++) { const v = vals[i]; row.push(v !== v ? undef : pyRepr(v)); }
    out.push(row.join(' '));
  }
  return encodeAscii(out.join('\n') + '\n');
}

/* ========================================================================
   cps3 — CPS-3 ASCII grid (read only; column-major from the NW node)
   ======================================================================== */
export const CPS3_ORDER_WARNING = 'CPS-3: values assumed column-major from the north-west node, each column running north to south (unverified assumption — check against a known surface)';
function cps3Floats(parts, key, n) {
  const vals = parts.slice(1, 1 + n).map(pyFloat);
  if (vals.some(v => v === undefined)) throw new FormatError(`CPS-3: ${key} not numeric`);
  if (vals.length < n) throw new FormatError(`CPS-3: ${key} needs ${n} values, found ${vals.length}`);
  return vals;
}
export function readCps3(src, opts = {}) {
  const lines = pySplitlines(asText(src));
  const warnings = [CPS3_ORDER_WARNING];
  let nul = 1e30, limits = null, nrows = null, ncols = null, xinc = null, yinc = null, attr = null;
  const comments = [];
  let k = 0;
  const n = lines.length;
  while (k < n) {
    const s = pyStrip(lines[k]);
    if (!s) { k++; continue; }
    const parts = pySplit(s);
    const key = parts[0].toUpperCase();
    if (key.startsWith('->')) { comments.push(pyStrip(s.slice(2))); k++; continue; }
    if (key.startsWith('FS')) {
      if (key === 'FSASCI') {
        if (parts.length > 1) { const v = pyFloat(parts[parts.length - 1]); if (v === undefined) warnings.push(`FSASCI null token ${pyReprStr(parts[parts.length - 1])} not numeric; 1e30 assumed`); else nul = v; }
      } else if (key === 'FSATTR') attr = parts.slice(1);
      else if (key === 'FSLIMI') limits = cps3Floats(parts, key, 6);
      else if (key === 'FSNROW') { const v = cps3Floats(parts, key, 2); nrows = Math.trunc(v[0]); ncols = Math.trunc(v[1]); }
      else if (key === 'FSXINC') { const v = cps3Floats(parts, key, 2); xinc = v[0]; yinc = v[1]; }
      else warnings.push(`unknown keyword ${key} ignored`);
      k++;
      continue;
    }
    break;
  }
  if (nrows === null || ncols === null) throw new FormatError('CPS-3: FSNROW missing');
  if (limits === null) throw new FormatError('CPS-3: FSLIMI missing');
  if (nrows <= 0 || ncols <= 0) throw new FormatError(`CPS-3: grid size ${nrows} rows x ${ncols} cols invalid`);
  const [xmin, xmax, ymin, ymax, zmin, zmax] = limits;
  if (xinc === null || yinc === null) {
    xinc = ncols > 1 ? (xmax - xmin) / (ncols - 1) : 1.0;
    yinc = nrows > 1 ? (ymax - ymin) / (nrows - 1) : 1.0;
    warnings.push('FSXINC missing; increments derived from FSLIMI');
  }
  if (xinc <= 0 || yinc <= 0) throw new FormatError(`CPS-3: increments must be positive (${pyRepr(xinc)}, ${pyRepr(yinc)})`);
  if (ncols > 1 && Math.abs((xmax - xmin) - (ncols - 1) * xinc) > 1e-6 * Math.max(1.0, Math.abs(xmax - xmin)))
    warnings.push(`FSLIMI x extent ${pyRepr(xmax - xmin)} != (ncols-1)*xinc ${pyRepr((ncols - 1) * xinc)}; xinc trusted, xmin taken as the west node`);
  if (nrows > 1 && Math.abs((ymax - ymin) - (nrows - 1) * yinc) > 1e-6 * Math.max(1.0, Math.abs(ymax - ymin)))
    warnings.push(`FSLIMI y extent ${pyRepr(ymax - ymin)} != (nrows-1)*yinc ${pyRepr((nrows - 1) * yinc)}; yinc trusted, ymin taken as the south node`);
  const tokens = [];
  while (k < n) {
    const s = pyStrip(lines[k]);
    k++;
    if (!s || s.startsWith('->')) continue;
    for (const t of pySplit(s)) tokens.push(t);
  }
  const count = nrows * ncols;
  if (tokens.length < count) throw new FormatError(`CPS-3: expected ${count} values, found ${tokens.length}`);
  if (tokens.length > count) warnings.push(`${tokens.length - count} trailing tokens ignored`);
  const values = new Float64Array(count).fill(NAN);
  let nulls = 0;
  const tol = 1e-6 * Math.max(1.0, Math.abs(nul));
  const nx = ncols, ny = nrows;
  for (let i = 0; i < nx; i++) {
    const base = i * ny;
    for (let r = 0; r < ny; r++) {
      const t = tokens[base + r];
      const v = pyFloat(t);
      if (v === undefined) throw new FormatError(`CPS-3: non-numeric value ${pyReprStr(t)}`);
      if (v !== v || Math.abs(v - nul) <= tol || v >= Math.abs(nul)) { nulls++; continue; }
      values[(ny - 1 - r) * nx + i] = v;
    }
  }
  const g = new GM.Grid2D({ nx, ny, x0: xmin, y0: ymin, dx: xinc, dy: yinc, values });
  g.name = opts.name || stem(opts.file, '');
  setProvenance(g, 'cps3', opts.file);
  g.metadata.warnings = warnings;
  g.metadata.cps3 = { null: nul, limits, attr, comments, zmin, zmax };
  g.metadata.null_nodes = nulls;
  g.role = 'surface';
  return g;
}

/* ========================================================================
   ubc — UBC-GIF 3-D mesh (.msh) + model files.  Mesh: NE NN NZ / E0 N0 Z0
   (south-west TOP corner) / widths along E, N, Z ('n*w' repeats).  Model:
   one value per cell, Z fastest (top -> bottom), then E, then N.
   ======================================================================== */
function ubcTokens(text) {
  const out = [];
  for (let line of pySplitlines(text)) {
    for (const mark of ['!', '#']) { const cut = line.indexOf(mark); if (cut >= 0) line = line.slice(0, cut); }
    for (const t of pySplit(line.replace(/,/g, ' '))) out.push(t);
  }
  return out;
}
function ubcTakeWidths(tokens, pos, count, axis) {
  const widths = [];
  while (widths.length < count) {
    if (pos >= tokens.length) throw new FormatError(`UBC mesh: ran out of ${axis} widths (${widths.length} of ${count})`);
    const t = tokens[pos++];
    if (t.indexOf('*') >= 0) {
      const k = t.indexOf('*');
      const rep = pyFloat(t.slice(0, k)), w = pyFloat(t.slice(k + 1));
      if (rep === undefined || w === undefined) throw new FormatError(`UBC mesh: bad width token ${pyReprStr(t)}`);
      for (let r = 0; r < Math.trunc(rep); r++) widths.push(w);
    } else {
      const w = pyFloat(t);
      if (w === undefined) throw new FormatError(`UBC mesh: bad width token ${pyReprStr(t)}`);
      widths.push(w);
    }
  }
  if (widths.length > count) throw new FormatError(`UBC mesh: ${axis} widths expand to ${widths.length} values, mesh declares ${count}`);
  return [widths, pos];
}
function ubcUniform(widths, axis) {
  const w0 = widths[0];
  for (const w of widths) if (Math.abs(w - w0) > 1e-9 * Math.max(1.0, Math.abs(w0)))
    throw new FormatError(`UBC mesh has variable ${axis} cell widths (${widths.slice(0, 6).map(pyRepr).join(', ')} ...); only uniform meshes map onto a regular BlockModel — resample or split the mesh first`);
  if (w0 <= 0) throw new FormatError(`UBC mesh: non-positive ${axis} cell width ${pyRepr(w0)}`);
  return w0;
}
/** Parse a UBC mesh -> {count, origin_top, widths, trailing_tokens}. */
export function readUbcMesh(src) {
  const tokens = ubcTokens(asText(src));
  if (tokens.length < 6) throw new FormatError('UBC mesh: header truncated');
  const h = tokens.slice(0, 6).map(pyFloat);
  if (h.some(v => v === undefined)) throw new FormatError('UBC mesh: header not numeric');
  const ne = Math.trunc(h[0]), nn = Math.trunc(h[1]), nz = Math.trunc(h[2]);
  if (ne <= 0 || nn <= 0 || nz <= 0) throw new FormatError(`UBC mesh: cell counts ${ne} ${nn} ${nz} invalid`);
  let pos = 6, we, wn, wz;
  [we, pos] = ubcTakeWidths(tokens, pos, ne, 'easting');
  [wn, pos] = ubcTakeWidths(tokens, pos, nn, 'northing');
  [wz, pos] = ubcTakeWidths(tokens, pos, nz, 'Z');
  return { count: [ne, nn, nz], origin_top: [h[3], h[4], h[5]], widths: [we, wn, wz], trailing_tokens: tokens.length - pos };
}
function ubcReadModel(src, n, label) {
  const tokens = ubcTokens(asText(src));
  if (tokens.length < n) throw new FormatError(`UBC model ${label}: expected ${n} values, found ${tokens.length}`);
  const vals = new Float64Array(n);
  for (let k = 0; k < n; k++) { const v = pyFloat(tokens[k]); if (v === undefined) throw new FormatError(`UBC model ${label}: non-numeric value (${pyReprStr(tokens[k])})`); vals[k] = v; }
  return [vals, tokens.length - n];
}
/** UBC mesh (+ model files) -> BlockModel.  opts.models = {attribute: bytes}; or opts.model (bytes) +
    opts.attribute (default 'property') for one; opts.nodata -> NaN; opts.name / opts.file name the object. */
export function readUbc(meshSrc, opts = {}) {
  const mesh = readUbcMesh(meshSrc);
  const [ne, nn, nz] = mesh.count;
  const [e0, n0, z0] = mesh.origin_top;
  const warnings = [];
  if (mesh.trailing_tokens) warnings.push(`${mesh.trailing_tokens} trailing tokens in the mesh file ignored`);
  const dx = ubcUniform(mesh.widths[0], 'easting'), dy = ubcUniform(mesh.widths[1], 'northing'), dz = ubcUniform(mesh.widths[2], 'Z');
  const bm = new GM.BlockModel({ origin: [e0, n0, z0 - nz * dz], blockSize: [dx, dy, dz], count: [ne, nn, nz] });
  setProvenance(bm, 'ubc', opts.file);
  bm.name = opts.name || stem(opts.file, '');
  const sources = [];
  if (opts.model !== undefined && opts.model !== null) sources.push([opts.attribute || 'property', opts.model]);
  for (const [k, v] of Object.entries(opts.models || {})) sources.push([k, v]);
  const n = ne * nn * nz;
  const nodata = opts.nodata === undefined ? null : +opts.nodata;
  for (const [attr, src] of sources) {
    const [vals, extra] = ubcReadModel(src, n, attr);
    if (extra) warnings.push(`model ${attr}: ${extra} trailing values ignored`);
    const out = new Float64Array(n).fill(NAN);
    const tol = nodata !== null ? 1e-9 * Math.max(1.0, Math.abs(nodata)) : 0.0;
    let p = 0;
    for (let iy = 0; iy < nn; iy++) for (let ix = 0; ix < ne; ix++) for (let kz = 0; kz < nz; kz++) {
      const v = vals[p++];
      if (nodata !== null && Math.abs(v - nodata) <= tol) continue;
      out[ix + ne * (iy + nn * (nz - 1 - kz))] = v;
    }
    bm.addAttribute(attr, out, 'number');
  }
  bm.metadata.warnings = warnings;
  bm.metadata.ubc = { origin_top: [e0, n0, z0], nodata };
  return bm;
}
/** BlockModel -> {msh, mod}: UBC mesh + the model file of `attribute` (default: first numeric attribute). */
export function writeUbc(blockmodel, attribute, opts = {}) {
  if (attribute && typeof attribute === 'object') { opts = attribute; attribute = opts.attribute; }
  const nodata = opts.nodata === undefined ? -99999.0 : +opts.nodata;
  if (blockmodel.azimuth) throw new FormatError(`UBC meshes are axis-aligned but the block model has azimuth ${pyRepr(blockmodel.azimuth)}; rotate / resample it first`);
  const [ne, nn, nz] = blockmodel.count;
  const [dx, dy, dz] = blockmodel.blockSize;
  const [ox, oy, oz] = blockmodel.origin;
  const mesh = [`${ne} ${nn} ${nz}`, `${pyRepr(ox)} ${pyRepr(oy)} ${pyRepr(oz + nz * dz)}`, `${ne}*${pyRepr(dx)}`, `${nn}*${pyRepr(dy)}`, `${nz}*${pyRepr(dz)}`];
  const result = { msh: encodeAscii(mesh.join('\n') + '\n'), mod: null };
  if (attribute === undefined || attribute === null) {
    const numeric = Object.entries(blockmodel.attributes).filter(([, a]) => a.type === 'number').map(([k]) => k);
    if (!numeric.length) { if (opts.meshOnly) return result; throw new FormatError('block model has no numeric attribute to write'); }
    attribute = numeric[0];
  }
  if (!(attribute in blockmodel.attributes)) throw new FormatError(`block model has no attribute ${pyReprStr(attribute)} (has ${Object.keys(blockmodel.attributes).sort().join(', ')})`);
  const vals = blockmodel.attributes[attribute].values;
  const lines = [];
  let nanCount = 0;
  for (let iy = 0; iy < nn; iy++) for (let ix = 0; ix < ne; ix++) for (let kz = 0; kz < nz; kz++) {
    let v = vals[ix + ne * (iy + nn * (nz - 1 - kz))];
    v = typeof v === 'number' ? v : (v === null || v === undefined ? NAN : (pyFloat(v) ?? NAN));
    if (v !== v) { nanCount++; v = nodata; }
    lines.push(pyRepr(v));
  }
  if (nanCount) blockmodel.warn(`ubc write: ${nanCount} NaN cells of ${pyReprStr(attribute)} written as ${pyRepr(nodata)}`);
  result.mod = encodeAscii(lines.join('\n') + '\n');
  result.attribute = attribute;
  return result;
}

/* ========================================================================
   obj — Wavefront OBJ triangle meshes
   ======================================================================== */
function objFmt(v) { v = +v; if (!isFinite(v)) return '0'; return reprShort(v); }
function* logicalLines(text) {
  let buf = null, start = 0, n = 0;
  for (const raw of pySplitlines(text)) {
    n++;
    const line = pyRstrip(raw);
    if (buf === null) { start = n; buf = ''; }
    if (line.endsWith('\\')) { buf += line.slice(0, -1) + ' '; continue; }
    buf += line;
    yield [start, buf];
    buf = null;
  }
  if (buf) yield [start, buf];
}
/** Wavefront OBJ -> Mesh (polygons fan-triangulated, groups merged; per-face 'group' attribute when > 1 group). */
export function readObj(src, opts = {}) {
  const text = typeof src === 'string' ? src : decodeText(toU8(src));
  const verts = [], tris = [], warnings = [], groups = [], faceGroup = [], materials = [], mtllibs = [];
  let curGroup = null, nLines = 0, nVt = 0, nVn = 0, nFaces = 0, nQuads = 0, badFaces = 0, objName = null;
  const groupIndex = (g) => { let i = groups.indexOf(g); if (i < 0) { groups.push(g); i = groups.length - 1; } return i; };
  for (const [lineno, line] of logicalLines(text)) {
    const s = pyStrip(line);
    if (!s || s[0] === '#') continue;
    const parts = pySplit(s);
    const key = parts[0];
    if (key === 'v') {
      if (parts.length < 4) { warnings.push(`line ${lineno}: vertex with < 3 coordinates skipped`); verts.push(0, 0, 0); continue; }
      const x = pyFloat(parts[1]), y = pyFloat(parts[2]), z = pyFloat(parts[3]);
      if (x === undefined || y === undefined || z === undefined) { warnings.push(`line ${lineno}: unparsable vertex ${pyReprStr(s.slice(0, 40))}`); verts.push(0, 0, 0); }
      else verts.push(x, y, z);
    } else if (key === 'f') {
      const nv = verts.length / 3;
      const idx = [];
      let ok = true;
      for (const tok of parts.slice(1)) {
        const vi = tok.split('/')[0];
        let i = pyInt(vi);
        if (i === undefined) { ok = false; break; }
        i = i < 0 ? nv + i : i - 1;
        if (i < 0 || i >= nv) { ok = false; break; }
        idx.push(i);
      }
      if (!ok || idx.length < 3) { badFaces++; continue; }
      nFaces++;
      if (idx.length > 3) nQuads++;
      const gi = curGroup !== null ? groupIndex(curGroup) : null;
      for (let k = 1; k < idx.length - 1; k++) { tris.push(idx[0], idx[k], idx[k + 1]); faceGroup.push(gi); }
    } else if (key === 'vt') nVt++;
    else if (key === 'vn') nVn++;
    else if (key === 'l') nLines++;
    else if (key === 'o' || key === 'g') {
      const gname = parts.length > 1 ? parts.slice(1).join(' ') : '';
      if (key === 'o' && objName === null && gname) objName = gname;
      curGroup = gname;
      groupIndex(gname);
    } else if (key === 'usemtl') { const m = parts.slice(1).join(' '); if (!materials.includes(m)) materials.push(m); }
    else if (key === 'mtllib') mtllibs.push(parts.slice(1).join(' '));
  }
  if (badFaces) warnings.push(`${badFaces} face(s) with invalid or out-of-range vertex indices were skipped`);
  if (nLines) warnings.push(`${nLines} polyline (l) record(s) ignored (counted in metadata["lines"])`);
  if (!tris.length && verts.length) warnings.push(`no faces: file holds ${verts.length / 3} vertices only`);
  let meshName = opts.name || objName || (groups.length === 1 && groups[0] ? groups[0] : null);
  if (!meshName) meshName = opts.file ? stem(opts.file, 'obj mesh') : 'obj mesh';
  const mesh = new GM.Mesh({ vertices: Float64Array.from(verts), triangles: Uint32Array.from(tris), name: meshName });
  setProvenance(mesh, 'obj', opts.file);
  if (groups.length > 1 && faceGroup.some(g => g !== null)) mesh.attributes.group = { location: 'faces', values: Float32Array.from(faceGroup, g => g === null ? -1 : g) };
  const md = mesh.metadata;
  md.groups = groups.slice(); md.lines = nLines; md.faces = nFaces; md.polygons_triangulated = nQuads; md.texcoords = nVt; md.normals = nVn;
  if (materials.length) md.materials = materials;
  if (mtllibs.length) md.mtllib = mtllibs;
  md.warnings = warnings;
  return mesh;
}
function vertexNormals(mesh) {
  const n = mesh.nVertices, v = mesh.vertices, t = mesh.triangles;
  const nx = new Float64Array(n), ny = new Float64Array(n), nz = new Float64Array(n);
  for (let k = 0; k + 2 < t.length; k += 3) {
    const a = t[k], b = t[k + 1], c = t[k + 2];
    const ax = v[3 * a], ay = v[3 * a + 1], az = v[3 * a + 2];
    const ux = v[3 * b] - ax, uy = v[3 * b + 1] - ay, uz = v[3 * b + 2] - az;
    const wx = v[3 * c] - ax, wy = v[3 * c + 1] - ay, wz = v[3 * c + 2] - az;
    const cx = uy * wz - uz * wy, cy = uz * wx - ux * wz, cz = ux * wy - uy * wx;
    for (const i of [a, b, c]) { nx[i] += cx; ny[i] += cy; nz[i] += cz; }
  }
  const out = [];
  for (let i = 0; i < n; i++) { const ln = Math.sqrt(nx[i] ** 2 + ny[i] ** 2 + nz[i] ** 2); out.push(ln > 0 ? [nx[i] / ln, ny[i] / ln, nz[i] / ln] : [0, 0, 1]); }
  return out;
}
/** Mesh -> OBJ bytes. opts: name, normals (bool), comment. */
export function writeObj(mesh, opts = {}) {
  let oname = opts.name || mesh.name || 'mesh';
  oname = pyStrip(oname).replace(/\s+/g, '_') || 'mesh';
  const out = ['# Wavefront OBJ written by nw-mineral-monitor geomodel'];
  if (opts.comment) for (const c of pySplitlines(String(opts.comment))) out.push('# ' + c);
  out.push(`# ${mesh.nVertices} vertices, ${mesh.nTriangles} triangles`, 'o ' + oname);
  const v = mesh.vertices;
  for (let i = 0; i + 2 < v.length; i += 3) out.push(`v ${objFmt(v[i])} ${objFmt(v[i + 1])} ${objFmt(v[i + 2])}`);
  if (opts.normals) for (const [a, b, c] of vertexNormals(mesh)) out.push(`vn ${objFmt(a)} ${objFmt(b)} ${objFmt(c)}`);
  const t = mesh.triangles;
  if (opts.normals) for (let k = 0; k + 2 < t.length; k += 3) { const a = t[k] + 1, b = t[k + 1] + 1, c = t[k + 2] + 1; out.push(`f ${a}//${a} ${b}//${b} ${c}//${c}`); }
  else for (let k = 0; k + 2 < t.length; k += 3) out.push(`f ${t[k] + 1} ${t[k + 1] + 1} ${t[k + 2] + 1}`);
  return utf8(out.join('\n') + '\n');
}

/* ========================================================================
   dxf — AutoCAD DXF R12 writer (3DFACE / POLYLINE+VERTEX / POINT) and a
   tolerant reader for any release (3DFACE SOLID TRACE, POLYLINE incl.
   polyface + polygon meshes, LWPOLYLINE with bulges, LINE, POINT).
   ======================================================================== */
const DXF_EOL = '\r\n';
const ACI_RGB = [[1, [255, 0, 0]], [2, [255, 255, 0]], [3, [0, 255, 0]], [4, [0, 255, 255]], [5, [0, 0, 255]], [6, [255, 0, 255]], [7, [255, 255, 255]], [8, [128, 128, 128]],
  [9, [192, 192, 192]], [250, [51, 51, 51]], [251, [91, 91, 91]], [252, [132, 132, 132]], [253, [173, 173, 173]], [254, [214, 214, 214]], [255, [255, 255, 255]]];
const ACI_MAP = new Map(ACI_RGB);
function dxfFmt(v) { v = +v; if (!isFinite(v)) return '0.0'; return pyRepr(v); }
export function sanitiseLayer(name, fallback = '0') {
  let s = pyStrip(String(name == null ? '' : name)).replace(/[^A-Za-z0-9_$\-]+/g, '_');
  s = s.replace(/^_+|_+$/g, '').slice(0, 31);
  return s || fallback;
}
function aciFromRgb(color) {
  if (!color || color.length < 3) return 7;
  const r = +color[0], g = +color[1], b = +color[2];
  if (r !== r || g !== g || b !== b) return 7;
  let best = 7, bd = null;
  for (const [aci, [cr, cg, cb]] of ACI_RGB) { const d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2; if (bd === null || d < bd) { best = aci; bd = d; } }
  return best;
}
class DxfTagWriter {
  constructor() { this.parts = []; }
  tag(code, value) { this.parts.push(padLeft(code, 3) + DXF_EOL + value + DXF_EOL); }
  point(x, y, z, base = 10) { this.tag(base, dxfFmt(x)); this.tag(base + 10, dxfFmt(y)); this.tag(base + 20, dxfFmt(z)); }
  text() { return this.parts.join(''); }
}
function dxfLayerFor(obj, k, layerNames, used) {
  let layer = null;
  if (typeof layerNames === 'string') layer = layerNames;
  else if (Array.isArray(layerNames)) layer = k < layerNames.length ? layerNames[k] : null;
  else if (layerNames && typeof layerNames === 'object') layer = layerNames[obj.name] ?? layerNames[obj.id] ?? layerNames[k] ?? null;
  layer = sanitiseLayer(layer !== null && layer !== undefined ? layer : obj.name, `OBJ_${k}`);
  if (layerNames === null || layerNames === undefined) {
    const base = layer;
    let n = 1;
    while (used.has(layer)) { n++; const suffix = `_${n}`; layer = base.slice(0, 31 - suffix.length) + suffix; }
  }
  used.add(layer);
  return layer;
}
/** Mesh / LineSet / PointSet objects (one or a list) -> DXF R12 bytes. opts.layerNames: list | {name|id|index: layer} | string. */
export function writeDxf(objects, opts = {}) {
  if (!Array.isArray(objects)) objects = [objects];
  const layerNames = opts.layerNames === undefined ? null : opts.layerNames;
  for (const obj of objects) if (!obj || !['mesh', 'lineset', 'points'].includes(obj.kind)) throw new TypeError(`write_dxf: cannot write a ${pyReprStr(obj && obj.kind ? obj.kind : typeof obj)} object`);
  const used = new Set();
  const layers = objects.map((obj, k) => [dxfLayerFor(obj, k, layerNames, used), aciFromRgb(obj.color)]);
  let mn = [Infinity, Infinity, Infinity], mx = [-Infinity, -Infinity, -Infinity];
  for (const obj of objects) {
    const b = obj.bounds();
    if (!b) continue;
    for (let a = 0; a < 3; a++) { mn[a] = Math.min(mn[a], b[a]); mx[a] = Math.max(mx[a], b[a + 3]); }
  }
  if (mn[0] === Infinity) { mn = [0, 0, 0]; mx = [0, 0, 0]; }
  const w = new DxfTagWriter();
  w.tag(999, 'nw-mineral-monitor geomodel DXF R12 export');
  w.tag(0, 'SECTION'); w.tag(2, 'HEADER');
  w.tag(9, '$ACADVER'); w.tag(1, 'AC1009');
  w.tag(9, '$INSUNITS'); w.tag(70, 6);
  w.tag(9, '$EXTMIN'); w.point(mn[0], mn[1], mn[2]);
  w.tag(9, '$EXTMAX'); w.point(mx[0], mx[1], mx[2]);
  w.tag(0, 'ENDSEC');
  w.tag(0, 'SECTION'); w.tag(2, 'TABLES');
  w.tag(0, 'TABLE'); w.tag(2, 'LTYPE'); w.tag(70, 1);
  w.tag(0, 'LTYPE'); w.tag(2, 'CONTINUOUS'); w.tag(70, 0); w.tag(3, 'Solid line'); w.tag(72, 65); w.tag(73, 0); w.tag(40, '0.0');
  w.tag(0, 'ENDTAB');
  w.tag(0, 'TABLE'); w.tag(2, 'LAYER'); w.tag(70, layers.length + 1);
  const seen = new Set();
  for (const [lname, aci] of [['0', 7]].concat(layers)) {
    if (seen.has(lname)) continue;
    seen.add(lname);
    w.tag(0, 'LAYER'); w.tag(2, lname); w.tag(70, 0); w.tag(62, aci); w.tag(6, 'CONTINUOUS');
  }
  w.tag(0, 'ENDTAB'); w.tag(0, 'ENDSEC');
  w.tag(0, 'SECTION'); w.tag(2, 'ENTITIES');
  objects.forEach((obj, k) => {
    const [layer] = layers[k];
    if (obj.kind === 'mesh') {
      const v = obj.vertices, t = obj.triangles;
      for (let q = 0; q + 2 < t.length; q += 3) {
        const a = t[q], b = t[q + 1], c = t[q + 2];
        w.tag(0, '3DFACE'); w.tag(8, layer);
        w.point(v[3 * a], v[3 * a + 1], v[3 * a + 2], 10);
        w.point(v[3 * b], v[3 * b + 1], v[3 * b + 2], 11);
        w.point(v[3 * c], v[3 * c + 1], v[3 * c + 2], 12);
        w.point(v[3 * c], v[3 * c + 1], v[3 * c + 2], 13);
      }
    } else if (obj.kind === 'lineset') {
      for (const p of obj.parts || []) {
        if (p.length < 2) continue;
        w.tag(0, 'POLYLINE'); w.tag(8, layer); w.tag(66, 1); w.point(0.0, 0.0, 0.0); w.tag(70, 8);
        for (const i of p) { const [x, y, z] = obj.vertex(i); w.tag(0, 'VERTEX'); w.tag(8, layer); w.point(x, y, z); w.tag(70, 32); }
        w.tag(0, 'SEQEND'); w.tag(8, layer);
      }
    } else {
      for (let i = 0; i < obj.n; i++) { const [x, y, z] = obj.point(i); w.tag(0, 'POINT'); w.tag(8, layer); w.point(x, y, z); }
    }
  });
  w.tag(0, 'ENDSEC'); w.tag(0, 'EOF');
  return utf8(w.text());
}

/* ---- reader */
function* dxfTags(text) {
  const lines = text.split(/\r\n|\n|\r/);
  const n = lines.length;
  let k = 0;
  while (k + 1 < n) {
    const codeS = pyStrip(lines[k]);
    if (!codeS) { k++; continue; }
    const code = pyInt(codeS);
    if (code === undefined) { k++; continue; }
    yield [code, pyStrip(lines[k + 1])];
    k += 2;
  }
}
function dxfEntities(text) {
  const entities = [], layerColors = {};
  let acadver = null, section = null, cur = null, pendingVar = null, tableKind = null, curLayer = null;
  for (const [code, value] of dxfTags(text)) {
    if (code === 0) {
      if (value === 'SECTION') { section = 'SECTION?'; cur = null; continue; }
      if (value === 'ENDSEC') { section = null; cur = null; continue; }
      if (value === 'EOF') break;
      if (section === 'ENTITIES') { cur = [value, []]; entities.push(cur); }
      else if (section === 'TABLES') {
        if (value === 'TABLE') { tableKind = null; curLayer = null; }
        else if (value === 'LAYER' && tableKind === 'LAYER') { curLayer = {}; cur = null; }
        else if (value === 'ENDTAB') { tableKind = null; curLayer = null; }
        else curLayer = null;
      }
      continue;
    }
    if (section === 'SECTION?' && code === 2) { section = value; continue; }
    if (section === 'HEADER') { if (code === 9) pendingVar = value; else if (pendingVar === '$ACADVER' && code === 1) acadver = value; continue; }
    if (section === 'TABLES') {
      if (code === 2 && tableKind === null && curLayer === null) tableKind = value;
      else if (curLayer !== null) {
        if (code === 2) curLayer.name = value;
        else if (code === 62) { const a = pyInt(value); if (a !== undefined) curLayer.aci = a; }
        if ('name' in curLayer && 'aci' in curLayer) layerColors[curLayer.name] = Math.abs(curLayer.aci);
      }
      continue;
    }
    if (section === 'ENTITIES' && cur !== null) cur[1].push([code, value]);
  }
  return [entities, layerColors, acadver];
}
function dxfFirst(tags, code, dflt = null, conv = 'float') {
  for (const [c, v] of tags) {
    if (c !== code) continue;
    if (conv === 'str') return v;
    const r = conv === 'int' ? pyInt(v) : pyFloat(v);
    return r === undefined ? dflt : r;
  }
  return dflt;
}
function dxfXyz(tags, base = 10, defaultZ = 0.0) {
  const x = dxfFirst(tags, base), y = dxfFirst(tags, base + 10), z = dxfFirst(tags, base + 20);
  if (x === null || y === null) return null;
  return [x, y, z === null ? defaultZ : z];
}
export function bulgePoints(p0, p1, bulge, maxDeg = 15.0) {
  if (!bulge) return [];
  const [x0, y0, z0] = p0, [x1, y1, z1] = p1;
  const dx = x1 - x0, dy = y1 - y0;
  const chord = Math.hypot(dx, dy);
  if (chord === 0) return [];
  const theta = 4.0 * Math.atan(bulge);
  const r = chord / (2.0 * Math.sin(Math.abs(theta) / 2.0));
  const mx = (x0 + x1) / 2.0, my = (y0 + y1) / 2.0;
  let h = Math.sqrt(Math.max(r * r - (chord / 2.0) ** 2, 0.0));
  const nx = -dy / chord, ny = dx / chord;
  const sgn = bulge > 0 ? 1.0 : -1.0;
  if (Math.abs(theta) > Math.PI) h = -h;
  const cx = mx + sgn * nx * h, cy = my + sgn * ny * h;
  const a0 = Math.atan2(y0 - cy, x0 - cx);
  const n = Math.max(1, Math.ceil(Math.abs(theta * 180 / Math.PI) / maxDeg));
  const pts = [];
  for (let k = 1; k < n; k++) { const a = a0 + theta * k / n, t = k / n; pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a), z0 + (z1 - z0) * t]); }
  return pts;
}
class DxfBuilder {
  constructor() { this.meshes = new Map(); this.lines = new Map(); this.points = new Map(); this.order = []; this.warnings = []; this.skipped = {}; this.bulgeArcs = 0; }
  _mesh(layer) {
    let m = this.meshes.get(layer);
    if (!m) { m = { index: new Map(), verts: [], tris: [] }; this.meshes.set(layer, m); this.order.push(['mesh', layer]); }
    return m;
  }
  addFace(layer, pts) {
    const m = this._mesh(layer);
    const idx = [];
    for (const p of pts) {
      const key = p[0] + ',' + p[1] + ',' + p[2];
      let i = m.index.get(key);
      if (i === undefined) { i = m.verts.length / 3; m.index.set(key, i); m.verts.push(p[0], p[1], p[2]); }
      idx.push(i);
    }
    const uniq = [];
    for (const i of idx) if (!uniq.length || uniq[uniq.length - 1] !== i) uniq.push(i);
    if (uniq.length > 1 && uniq[0] === uniq[uniq.length - 1]) uniq.pop();
    if (uniq.length < 3) return 0;
    let n = 0;
    for (let k = 1; k < uniq.length - 1; k++) { m.tris.push(uniq[0], uniq[k], uniq[k + 1]); n++; }
    return n;
  }
  addPolyline(layer, pts, feature) {
    let ls = this.lines.get(layer);
    if (!ls) { ls = new GM.LineSet({ name: layer, role: 'lines' }); this.lines.set(layer, ls); this.order.push(['lineset', layer]); }
    if (pts.length >= 2) ls.addPolyline(pts, feature);
    else if (pts.length === 1) this.warnings.push(`single-vertex polyline on layer ${pyReprStr(layer)} ignored`);
  }
  addPoint(layer, p) {
    let ps = this.points.get(layer);
    if (!ps) { ps = new GM.PointSet({ name: layer, role: 'points' }); this.points.set(layer, ps); this.order.push(['points', layer]); }
    ps.add(p[0], p[1], p[2]);
  }
  skip(etype) { this.skipped[etype] = (this.skipped[etype] || 0) + 1; }
}
function dxfPolylineVertices(tagsList) {
  return tagsList.map(vt => {
    const flags = dxfFirst(vt, 70, 0, 'int') || 0;
    const p = dxfXyz(vt, 10);
    const bulge = dxfFirst(vt, 42, 0.0) || 0.0;
    const face = [71, 72, 73, 74].map(c => dxfFirst(vt, c, 0, 'int') || 0);
    return [flags, p, bulge, face];
  });
}
function ptEq(a, b) { return a[0] === b[0] && a[1] === b[1] && a[2] === b[2]; }
function dxfHandlePolyline(b, layer, ptags, vtags) {
  const flags = dxfFirst(ptags, 70, 0, 'int') || 0;
  const verts = dxfPolylineVertices(vtags);
  if (flags & 64) {                                   // polyface mesh
    const geom = [], faces = [];
    for (const [vflags, p, , face] of verts) { if ((vflags & 128) && !(vflags & 64)) faces.push(face); else if (p !== null) geom.push(p); }
    let n = 0;
    for (const face of faces) {
      const idx = face.filter(i => i !== 0).map(i => Math.abs(i) - 1);
      const pts = [];
      let ok = true;
      for (const i of idx) { if (i < 0 || i >= geom.length) { ok = false; break; } pts.push(geom[i]); }
      if (ok && pts.length >= 3) n += b.addFace(layer, pts);
      else b.warnings.push(`polyface face with bad vertex index on layer ${pyReprStr(layer)} skipped`);
    }
    return n;
  }
  if (flags & 16) {                                   // polygon (M x N) mesh
    const M = dxfFirst(ptags, 71, 0, 'int') || 0, N = dxfFirst(ptags, 72, 0, 'int') || 0;
    const geom = verts.filter(([vflags, p]) => p !== null && !((vflags & 16) && !(vflags & 64))).map(v => v[1]);
    if (M * N !== geom.length || M < 2 || N < 2) {
      b.warnings.push(`polygon mesh on layer ${pyReprStr(layer)}: ${geom.length} vertices but ${M}x${N} declared`);
      if (M * N > geom.length || M < 2 || N < 2) return 0;
    }
    const closedM = !!(flags & 1), closedN = !!(flags & 32);
    let n = 0;
    for (let i = 0; i < (closedM ? M : M - 1); i++) {
      const i2 = (i + 1) % M;
      for (let j = 0; j < (closedN ? N : N - 1); j++) {
        const j2 = (j + 1) % N;
        n += b.addFace(layer, [geom[i * N + j], geom[i * N + j2], geom[i2 * N + j2], geom[i2 * N + j]]);
      }
    }
    return n;
  }
  const elev = dxfFirst(ptags, 30, 0.0) || 0.0;
  const pts = [], bulges = [];
  for (const [vflags, p, bulge] of verts) {
    if (vflags & 16) continue;
    if (p === null) continue;
    if (flags & 8) pts.push(p); else pts.push([p[0], p[1], p[2] ? p[2] : elev]);
    bulges.push(bulge);
  }
  const closed = !!(flags & 1);
  const out = [];
  pts.forEach((p, k) => {
    out.push(p);
    if (bulges[k] && (k + 1 < pts.length || closed)) { const nxt = pts[(k + 1) % pts.length]; for (const q of bulgePoints(p, nxt, bulges[k])) out.push(q); b.bulgeArcs++; }
  });
  if (closed && out.length > 2 && !ptEq(out[0], out[out.length - 1])) out.push(out[0]);
  b.addPolyline(layer, out, { closed, entity: 'POLYLINE' });
  return 0;
}
function dxfHandleLwpolyline(b, layer, tags) {
  const flags = dxfFirst(tags, 70, 0, 'int') || 0;
  const elev = dxfFirst(tags, 38, 0.0) || 0.0;
  const pts = [], bulges = [];
  let x = null;
  for (const [c, v] of tags) {
    if (c === 10) { const f = pyFloat(v); x = f === undefined ? null : f; }
    else if (c === 20 && x !== null) { const f = pyFloat(v); if (f !== undefined) { pts.push([x, f, elev]); bulges.push(0.0); } x = null; }
    else if (c === 42 && pts.length) { const f = pyFloat(v); if (f !== undefined) bulges[bulges.length - 1] = f; }
  }
  const closed = !!(flags & 1);
  const out = [];
  pts.forEach((p, k) => {
    out.push(p);
    if (bulges[k] && (k + 1 < pts.length || closed)) { const nxt = pts[(k + 1) % pts.length]; for (const q of bulgePoints(p, nxt, bulges[k])) out.push(q); b.bulgeArcs++; }
  });
  if (closed && out.length > 2 && !ptEq(out[0], out[out.length - 1])) out.push(out[0]);
  b.addPolyline(layer, out, { closed, entity: 'LWPOLYLINE' });
}
/** DXF -> [Mesh | LineSet | PointSet, ...], one object per (layer, kind). */
export function readDxf(src, opts = {}) {
  const text = typeof src === 'string' ? src : decodeText(toU8(src));
  const [entities, layerColors, acadver] = dxfEntities(text);
  const b = new DxfBuilder();
  let k = 0;
  const n = entities.length;
  while (k < n) {
    const [etype, tags] = entities[k];
    const layer = dxfFirst(tags, 8, '0', 'str') || '0';
    if (etype === '3DFACE' || etype === 'SOLID' || etype === 'TRACE') {
      let corners = [10, 11, 12, 13].map(base => dxfXyz(tags, base)).filter(c => c !== null);
      if ((etype === 'SOLID' || etype === 'TRACE') && corners.length === 4) corners = [corners[0], corners[1], corners[3], corners[2]];
      if (corners.length >= 3) b.addFace(layer, corners);
      else b.warnings.push(`${etype} with ${corners.length} corners skipped`);
    } else if (etype === 'POLYLINE') {
      const vtags = [];
      let j = k + 1;
      while (j < n && entities[j][0] === 'VERTEX') { vtags.push(entities[j][1]); j++; }
      if (j < n && entities[j][0] === 'SEQEND') j++;
      dxfHandlePolyline(b, layer, tags, vtags);
      k = j;
      continue;
    } else if (etype === 'LWPOLYLINE') dxfHandleLwpolyline(b, layer, tags);
    else if (etype === 'LINE') { const p0 = dxfXyz(tags, 10), p1 = dxfXyz(tags, 11); if (p0 && p1) b.addPolyline(layer, [p0, p1], { entity: 'LINE' }); }
    else if (etype === 'POINT') { const p = dxfXyz(tags, 10); if (p) b.addPoint(layer, p); }
    else if (etype === 'VERTEX' || etype === 'SEQEND') b.warnings.push(`orphan ${etype} entity ignored`);
    else b.skip(etype);
    k++;
  }
  if (b.bulgeArcs) b.warnings.push(`${b.bulgeArcs} bulge arc(s) tessellated into straight segments`);
  if (!entities.length) b.warnings.push('no ENTITIES section found');
  const objects = [];
  for (const [kind, layer] of b.order) {
    let obj;
    if (kind === 'mesh') { const m = b.meshes.get(layer); obj = new GM.Mesh({ vertices: Float64Array.from(m.verts), triangles: Uint32Array.from(m.tris), name: layer, role: 'surface' }); }
    else if (kind === 'lineset') obj = b.lines.get(layer);
    else obj = b.points.get(layer);
    const aci = layerColors[layer];
    if (ACI_MAP.has(aci)) obj.color = ACI_MAP.get(aci).slice();
    setProvenance(obj, 'dxf', opts.file);
    obj.metadata.layer = layer;
    obj.metadata.acadver = acadver;
    obj.metadata.skipped = Object.assign({}, b.skipped);
    obj.metadata.warnings = b.warnings.slice();
    objects.push(obj);
  }
  return objects;
}

/* ========================================================================
   gocad — GOCAD ASCII TSurf (Mesh) / PLine (LineSet) / VSet (PointSet)
   ======================================================================== */
const GOCAD_NO_DATA = -99999.0;
const GOCAD_SECTION_KEYWORDS = new Set(['GOCAD_ORIGINAL_COORDINATE_SYSTEM', 'PROPERTIES', 'PROPERTY_CLASSES', 'TFACE', 'ILINE', 'SUBVSET', 'TVOLUME', 'VRTX', 'PVRTX', 'ATOM', 'PATOM',
  'TRGL', 'SEG', 'TETRA', 'GEOLOGICAL_TYPE', 'GEOLOGICAL_FEATURE', 'STRATIGRAPHIC_POSITION', 'ESIZES', 'NO_DATA_VALUES', 'UNITS', 'TRGL_PROPERTIES', 'BSTONE', 'BORDER', 'END']);
function gocadFmt(v) { v = +v; if (!isFinite(v)) return pyRepr(GOCAD_NO_DATA); return reprShort(v); }
function gocadFloat(tok) {
  const v = pyFloat(tok);
  if (v !== undefined) return v;
  const t = tok.toLowerCase();
  if (t === 'nan' || t === 'na' || t === 'none' || t === 'null') return NAN;
  return undefined;
}
export function parseGocadColor(value) {
  value = pyStrip(value);
  if (!value) return null;
  if (value.startsWith('#')) {
    const h = value.slice(1);
    if (h.length >= 6) { const c = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); return c.some(v => v !== v) ? null : c; }
    return null;
  }
  const toks = pySplit(value.replace(/,/g, ' '));
  const vals = [];
  for (const t of toks.slice(0, 3)) { const f = pyFloat(t); if (f === undefined) return null; vals.push(f); }
  if (vals.length < 3) return null;
  if (Math.max(...vals) <= 1.0) return vals.map(v => pyRound(v * 255));
  return vals.map(v => pyRound(v));
}
function quotedTokens(line) { const out = []; const re = /"[^"]*"|\S+/g; let m; while ((m = re.exec(line)) !== null) out.push(m[0].replace(/^"+|"+$/g, '')); return out; }
class GocadObject {
  constructor(otype, version) {
    this.otype = otype; this.version = version; this.header = {}; this.coordsys = {}; this.props = []; this.esizes = []; this.nodata = []; this.units = [];
    this.tprops = []; this.tnodata = []; this.geologicalType = null; this.ids = new Map(); this.verts = []; this.pvals = []; this.tris = []; this.tvals = [];
    this.segs = []; this.parts = []; this.partHasSeg = []; this.warnings = []; this.nAtoms = 0; this.warned = new Set();
  }
  newPart() { this.parts.push([]); this.partHasSeg.push(false); }
  addVertex(fid, x, y, z, pvals) {
    const gi = this.verts.length / 3;
    this.verts.push(x, y, z); this.pvals.push(pvals); this.ids.set(fid, gi);
    if (!this.parts.length) this.newPart();
    this.parts[this.parts.length - 1].push(gi);
    return gi;
  }
  lookup(fid) { const v = this.ids.get(fid); return v === undefined ? null : v; }
}
function gocadSplitProps(obj, values, names, esizes, nodata, warnings, what) {
  const vals = values.map(t => { const v = gocadFloat(t); return v === undefined ? NAN : v; });
  let sizes = (esizes && esizes.length === names.length) ? esizes.slice() : names.map(() => 1);
  if (sizes.reduce((a, b) => a + b, 0) !== vals.length) {
    if (vals.length === names.length) sizes = names.map(() => 1);
    else {
      const key = 'count:' + what;
      if (!obj.warned.has(key)) { obj.warned.add(key); warnings.push(`${what}: ${vals.length} property values for ${names.length} declared properties (${names.join(' ')})`); }
    }
  }
  const out = {};
  let pos = 0;
  names.forEach((name, k) => {
    const sz = k < sizes.length ? sizes[k] : 1;
    const nd = k < nodata.length ? nodata[k] : null;
    for (let c = 0; c < sz; c++) {
      const col = sz === 1 ? name : `${name}_${c + 1}`;
      let v;
      if (pos < vals.length) {
        v = vals[pos];
        if (nd !== null && v === nd) v = NAN;
        else if (v === v && Math.abs(v) >= 1e38) {
          v = NAN;
          if (!obj.warned.has('nodata1e38')) { obj.warned.add('nodata1e38'); warnings.push('property values >= 1e38 treated as no-data (GOCAD convention)'); }
        }
      } else v = NAN;
      out[col] = v;
      pos++;
    }
  });
  let extra = 0;
  while (pos < vals.length) { extra++; out[`${what}_extra_${extra}`] = vals[pos]; pos++; }
  return out;
}
function gocadParse(text) {
  const objects = [];
  let cur = null, inHeader = false, braceDepth = 0, inCoordsys = false, skipBlock = false;
  const count = (s, ch) => s.split(ch).length - 1;
  for (const raw of pySplitlines(text)) {
    const line = pyStrip(raw);
    if (!line) continue;
    const up = line.toUpperCase();
    const first = up ? pySplit(up)[0] : '';
    if (cur === null) {
      if (up.startsWith('GOCAD ')) {
        const toks = pySplit(line);
        cur = new GocadObject(toks.length > 1 ? toks[1] : '?', toks.length > 2 ? toks[2] : '');
        inHeader = false; braceDepth = 0; inCoordsys = false; skipBlock = false;
      }
      continue;
    }
    if (inHeader || skipBlock) {
      if (GOCAD_SECTION_KEYWORDS.has(first.replace(/\{+$/, '')) || up.startsWith('GOCAD ')) {
        if (inHeader) cur.warnings.push('HEADER block not closed with "}"');
        inHeader = skipBlock = false; braceDepth = 0;
      } else {
        braceDepth += count(line, '{') - count(line, '}');
        if (inHeader && line.indexOf(':') >= 0 && !line.startsWith('#')) { const i = line.indexOf(':'); cur.header[pyStrip(line.slice(0, i))] = pyStrip(line.slice(i + 1)); }
        if (braceDepth <= 0 && (line.endsWith('}') || braceDepth < 0)) { inHeader = skipBlock = false; braceDepth = 0; }
        continue;
      }
    }
    if (first.startsWith('HEADER')) {
      inHeader = true;
      braceDepth = count(line, '{') - count(line, '}');
      let rest = line.indexOf('{') >= 0 ? line.slice(line.indexOf('{') + 1) : '';
      rest = pyStrip(rest.replace(/\}+$/, ''));
      if (rest.indexOf(':') >= 0) { const i = rest.indexOf(':'); cur.header[pyStrip(rest.slice(0, i))] = pyStrip(rest.slice(i + 1)); }
      if (braceDepth <= 0 && line.indexOf('}') >= 0) inHeader = false;
      continue;
    }
    if (first === 'HDR') {
      const rest = pyStrip(line.slice(3));
      if (rest.indexOf(':') >= 0) { const i = rest.indexOf(':'); cur.header[pyStrip(rest.slice(0, i))] = pyStrip(rest.slice(i + 1)); }
      continue;
    }
    if (up.indexOf('PROPERTY_CLASS_HEADER') >= 0 && (up.startsWith('PROPERTY_CLASS_HEADER') || up.startsWith('TRGL_PROPERTY_CLASS_HEADER'))) {
      const depth = count(line, '{') - count(line, '}');
      if (depth > 0 || line.indexOf('{') < 0) { skipBlock = true; braceDepth = Math.max(depth, 1); }
      continue;
    }
    if (inCoordsys) {
      if (first === 'END_ORIGINAL_COORDINATE_SYSTEM') inCoordsys = false;
      else { const toks = quotedTokens(line); if (toks.length) cur.coordsys[toks[0].toUpperCase()] = toks.length > 2 ? toks.slice(1) : (toks.length > 1 ? toks[1] : ''); }
      continue;
    }
    if (first === 'GOCAD_ORIGINAL_COORDINATE_SYSTEM') { inCoordsys = true; continue; }
    const toks = pySplit(line);
    const key = toks[0].toUpperCase();
    if (key === 'END') { objects.push(cur); cur = null; continue; }
    if (key === 'PROPERTIES' || key === 'FIELDS') cur.props = toks.slice(1);
    else if (key === 'ESIZES') cur.esizes = toks.slice(1).map(t => Math.trunc(pyFloat(t)));
    else if (key === 'NO_DATA_VALUES') cur.nodata = toks.slice(1).map(t => pyFloat(t));
    else if (key === 'UNITS') cur.units = toks.slice(1);
    else if (key === 'TRGL_PROPERTIES') cur.tprops = toks.slice(1);
    else if (key === 'TRGL_NO_DATA_VALUES') cur.tnodata = toks.slice(1).map(t => pyFloat(t));
    else if (key === 'GEOLOGICAL_TYPE') cur.geologicalType = toks.slice(1).join(' ');
    else if (key === 'TFACE' || key === 'ILINE' || key === 'SUBVSET' || key === 'TVOLUME') cur.newPart();
    else if (key === 'VRTX' || key === 'PVRTX') {
      if (toks.length < 5) { cur.warnings.push(`short ${key} record: ${pyReprStr(line.slice(0, 60))}`); continue; }
      const fid = pyInt(toks[1]), x = pyFloat(toks[2]), y = pyFloat(toks[3]), z = pyFloat(toks[4]);
      if (fid === undefined || x === undefined || y === undefined || z === undefined) { cur.warnings.push(`unparsable ${key} record: ${pyReprStr(line.slice(0, 60))}`); continue; }
      let rest = toks.slice(5);
      if (rest.length && rest[0].toUpperCase().startsWith('CN')) rest = rest.slice(1);
      let pv = null;
      if (cur.props.length || key === 'PVRTX') pv = gocadSplitProps(cur, rest, cur.props, cur.esizes, cur.nodata, cur.warnings, key);
      cur.addVertex(fid, x, y, z, pv);
    } else if (key === 'ATOM' || key === 'PATOM') {
      if (toks.length < 3) continue;
      const fid = pyInt(toks[1]), ref = pyInt(toks[2]);
      if (fid === undefined || ref === undefined) continue;
      const gi = cur.lookup(ref);
      if (gi === null) { cur.warnings.push(`${key} ${fid} refers to unknown vertex ${ref}`); continue; }
      const x = cur.verts[3 * gi], y = cur.verts[3 * gi + 1], z = cur.verts[3 * gi + 2];
      let pv = cur.pvals[gi];
      if (key === 'PATOM' && cur.props.length) pv = gocadSplitProps(cur, toks.slice(3), cur.props, cur.esizes, cur.nodata, cur.warnings, key);
      cur.addVertex(fid, x, y, z, pv);
      cur.nAtoms++;
    } else if (key === 'TRGL') {
      if (toks.length < 4) continue;
      const ids = [pyInt(toks[1]), pyInt(toks[2]), pyInt(toks[3])];
      if (ids.some(i => i === undefined)) { cur.warnings.push(`unparsable TRGL: ${pyReprStr(line.slice(0, 60))}`); continue; }
      const g = ids.map(i => cur.lookup(i));
      if (g.some(v => v === null)) { cur.warnings.push(`TRGL ${toks.slice(1, 4).join(' ')} references an undefined vertex; skipped`); continue; }
      cur.tris.push(g[0], g[1], g[2]);
      if (cur.tprops.length || toks.length > 4) {
        const names = cur.tprops.length ? cur.tprops : toks.slice(4).map((_, k) => `trgl_prop_${k + 1}`);
        cur.tvals.push(gocadSplitProps(cur, toks.slice(4), names, [], cur.tnodata, cur.warnings, 'TRGL'));
      } else cur.tvals.push(null);
    } else if (key === 'SEG') {
      if (toks.length < 3) continue;
      const ia = pyInt(toks[1]), ib = pyInt(toks[2]);
      if (ia === undefined || ib === undefined) continue;
      const a = cur.lookup(ia), bb = cur.lookup(ib);
      if (a === null || bb === null) { cur.warnings.push(`SEG ${toks.slice(1, 3).join(' ')} references an undefined vertex; skipped`); continue; }
      cur.segs.push([a, bb]);
      if (cur.partHasSeg.length) cur.partHasSeg[cur.partHasSeg.length - 1] = true;
    }
  }
  if (cur !== null) { cur.warnings.push('file ended without END keyword'); objects.push(cur); }
  return objects;
}
function chainsFromSegments(segs) {
  const nxt = new Map(), hasPrev = new Set();
  for (const [a, b] of segs) { if (!nxt.has(a)) nxt.set(a, []); nxt.get(a).push(b); hasPrev.add(b); }
  const walk = (start) => {
    const chain = [start];
    let cur = start;
    while (nxt.get(cur) && nxt.get(cur).length) { cur = nxt.get(cur).shift(); chain.push(cur); if (cur === start) break; }
    return chain;
  };
  const chains = [];
  for (const a of Array.from(nxt.keys())) if (!hasPrev.has(a)) while (nxt.get(a) && nxt.get(a).length) chains.push(walk(a));
  for (const a of Array.from(nxt.keys())) while (nxt.get(a) && nxt.get(a).length) chains.push(walk(a));
  return chains;
}
function gocadVertexAttributes(g) {
  const cols = [];
  for (const pv of g.pvals) if (pv) for (const k of Object.keys(pv)) if (!cols.includes(k)) cols.push(k);
  const out = {};
  const n = g.verts.length / 3;
  for (const c of cols) {
    const arr = new Float64Array(n);
    for (let i = 0; i < n; i++) { const pv = g.pvals[i]; arr[i] = pv && c in pv ? pv[c] : NAN; }
    out[c] = { location: 'vertices', values: arr };
  }
  return out;
}
function gocadFaceAttributes(g) {
  const cols = [];
  for (const tv of g.tvals) if (tv) for (const k of Object.keys(tv)) if (!cols.includes(k)) cols.push(k);
  const out = {};
  for (const c of cols) out[c] = { location: 'faces', values: Float64Array.from(g.tvals, tv => tv && c in tv ? tv[c] : NAN) };
  return out;
}
function gocadPickColor(header, prefs) {
  for (const key of prefs) if (key in header) { const c = parseGocadColor(header[key]); if (c) return c; }
  for (const [key, val] of Object.entries(header)) if (key.toLowerCase().endsWith('color')) { const c = parseGocadColor(val); if (c) return c; }
  return null;
}
function gocadFinish(obj, g, file, depthFlipped) {
  setProvenance(obj, 'gocad_ts', file, { gocad_type: g.otype });
  const md = obj.metadata;
  md.gocad_header = Object.assign({}, g.header);
  md.coordinate_system = Object.assign({}, g.coordsys);
  if (g.geologicalType) md.geological_type = g.geologicalType;
  if (g.props.length) { md.properties = g.props.slice(); if (g.units.length) { md.property_units = {}; g.props.forEach((p, i) => { if (i < g.units.length) md.property_units[p] = g.units[i]; }); } }
  if (g.tprops.length) md.triangle_properties = g.tprops.slice();
  if (depthFlipped) g.warnings.push('ZPOSITIVE Depth: Z negated to elevation');
  if (g.nAtoms) md.atoms = g.nAtoms;
  md.warnings = g.warnings.slice();
  return obj;
}
function gocadRole(gtype) {
  const t = (gtype || '').toLowerCase();
  if (t.indexOf('fault') >= 0) return 'fault';
  if (t.indexOf('topo') >= 0) return 'topography';
  if (['top', 'boundary', 'unconformity', 'horizon'].includes(t)) return 'contact';
  return 'surface';
}
/** GOCAD ASCII (one or more concatenated objects) -> [Mesh | LineSet | PointSet, ...]. */
export function readGocad(src, opts = {}) {
  const text = typeof src === 'string' ? src : decodeText(toU8(src));
  const parsed = gocadParse(text);
  const out = [], skipped = [];
  for (const g of parsed) {
    const name = g.header.name || g.header.NAME || g.otype;
    let zpos = g.coordsys.ZPOSITIVE === undefined ? '' : g.coordsys.ZPOSITIVE;
    zpos = typeof zpos === 'string' ? zpos : zpos.join(' ');
    const depth = pyStrip(zpos).toLowerCase() === 'depth';
    if (depth) for (let i = 2; i < g.verts.length; i += 3) g.verts[i] = -g.verts[i];
    const otype = g.otype.toLowerCase();
    const verts = Float64Array.from(g.verts);
    if (otype === 'tsurf') {
      const attributes = Object.assign({}, gocadVertexAttributes(g), gocadFaceAttributes(g));
      const mesh = new GM.Mesh({ vertices: verts, triangles: Uint32Array.from(g.tris), name, role: gocadRole(g.geologicalType), attributes });
      const c = gocadPickColor(g.header, ['*solid*color', 'solid*color', 'color']);
      if (c) mesh.color = c;
      if (g.parts.length > 1) mesh.metadata.tfaces = g.parts.length;
      if (!g.tris.length) g.warnings.push('TSurf has no TRGL records');
      out.push(gocadFinish(mesh, g, opts.file, depth));
    } else if (otype === 'pline') {
      const partOf = new Map();
      g.parts.forEach((p, pk) => { for (const gi of p) partOf.set(gi, pk); });
      const segByPart = new Map();
      for (const [a, b] of g.segs) { const pk = partOf.has(a) ? partOf.get(a) : 0; if (!segByPart.has(pk)) segByPart.set(pk, []); segByPart.get(pk).push([a, b]); }
      const segs = [], parts = [];
      g.parts.forEach((p, pk) => {
        let partSegs;
        if (pk < g.partHasSeg.length && g.partHasSeg[pk]) partSegs = segByPart.get(pk) || [];
        else if (p.length > 1) { partSegs = []; for (let k = 0; k < p.length - 1; k++) partSegs.push([p[k], p[k + 1]]); }
        else partSegs = [];
        for (const [a, b] of partSegs) segs.push(a, b);
        for (const ch of chainsFromSegments(partSegs)) parts.push(ch);
      });
      const ls = new GM.LineSet({ vertices: verts, segments: Uint32Array.from(segs), parts, features: parts.map((_, k) => ({ iline: k })), name, role: 'lines' });
      if (g.props.length) { const attrs = gocadVertexAttributes(g); ls.metadata.vertex_properties = {}; for (const [k, v] of Object.entries(attrs)) ls.metadata.vertex_properties[k] = Array.from(v.values); }
      const c = gocadPickColor(g.header, ['*line*color', 'line*color', 'color', '*solid*color']);
      if (c) ls.color = c;
      if (!segs.length) g.warnings.push('PLine has no segments');
      out.push(gocadFinish(ls, g, opts.file, depth));
    } else if (otype === 'vset') {
      const attrs = {};
      for (const [k, v] of Object.entries(gocadVertexAttributes(g))) attrs[k] = nanToNull(v.values);
      const ps = new GM.PointSet({ xyz: verts, attributes: attrs, name, role: 'points' });
      const c = gocadPickColor(g.header, ['*atoms*color', 'atoms*color', 'color', '*solid*color']);
      if (c) ps.color = c;
      out.push(gocadFinish(ps, g, opts.file, depth));
    } else skipped.push(`${g.otype} ${pyReprStr(name)}`);
  }
  if (skipped.length) {
    if (!out.length) throw new FormatError(`no TSurf / PLine / VSet objects in file (found: ${skipped.join(', ')})`);
    for (const o of out) (o.metadata.warnings = o.metadata.warnings || []).push(`skipped unsupported object(s): ${skipped.join(', ')}`);
  }
  if (!out.length && !parsed.length) throw new FormatError('not a GOCAD ASCII file (no "GOCAD <Type>" line)');
  return out;
}
function gocadCoordsys(zpositive) {
  return ['GOCAD_ORIGINAL_COORDINATE_SYSTEM', 'NAME Default', 'AXIS_NAME "X" "Y" "Z"', 'AXIS_UNIT "m" "m" "m"', `ZPOSITIVE ${zpositive}`, 'END_ORIGINAL_COORDINATE_SYSTEM'];
}
function gocadColorLine(key, color) {
  let r = 0.5, g = 0.5, b = 0.5;
  if (color && color.length >= 3 && color.slice(0, 3).every(c => typeof c === 'number' && c === c)) [r, g, b] = color.slice(0, 3).map(c => Math.max(0.0, Math.min(1.0, c / 255.0)));
  return `${key}:${gocadFmt(r)} ${gocadFmt(g)} ${gocadFmt(b)} 1`;
}
function gocadNumericColumns(attributes, location, n) {
  const cols = [];
  for (const [name, spec] of Object.entries(attributes || {})) {
    let vals;
    if (spec && typeof spec === 'object' && !Array.isArray(spec) && !ArrayBuffer.isView(spec)) {
      if ((spec.location || 'vertices') !== location) continue;
      vals = spec.values;
    } else vals = spec;
    if (vals == null || vals.length !== n) continue;
    let ok = true;
    const fl = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const v = vals[i];
      if (v === null || v === undefined || v === '') { fl[i] = NAN; continue; }
      if (typeof v === 'boolean') { ok = false; break; }
      const f = typeof v === 'number' ? v : pyFloat(v);
      if (f === undefined) { ok = false; break; }
      fl[i] = f;
    }
    if (ok) cols.push([String(name).replace(/\s+/g, '_'), fl]);
  }
  return cols;
}
function gocadPropertyHeader(w, cols) {
  w('PROPERTIES ' + cols.map(c => c[0]).join(' '));
  w('PROP_LEGAL_RANGES ' + cols.map(() => '**none** **none**').join(' '));
  w('NO_DATA_VALUES ' + cols.map(() => gocadFmt(GOCAD_NO_DATA)).join(' '));
  w('PROPERTY_CLASSES ' + cols.map(c => c[0].toLowerCase()).join(' '));
  w('PROPERTY_KINDS ' + cols.map(() => '"Real Number"').join(' '));
  w('PROPERTY_SUBCLASSES ' + cols.map(() => 'QUANTITY Float').join(' '));
  w('ESIZES ' + cols.map(() => '1').join(' '));
  w('UNITS ' + cols.map(() => 'unitless').join(' '));
}
/** Mesh -> TSurf, LineSet -> PLine, PointSet -> VSet. opts: zpositive 'Elevation' | 'Depth', name. */
export function writeGocad(obj, opts = {}) {
  const zpositive = opts.zpositive || 'Elevation';
  if (zpositive !== 'Elevation' && zpositive !== 'Depth') throw new FormatError("zpositive must be 'Elevation' or 'Depth'");
  const zsign = zpositive === 'Depth' ? -1.0 : 1.0;
  const oname = pyStrip(opts.name || obj.name || obj.kind);
  const L = [];
  const w = s => L.push(s);
  if (obj.kind === 'mesh') {
    const n = obj.nVertices;
    const cols = gocadNumericColumns(obj.attributes, 'vertices', n);
    const fcols = gocadNumericColumns(obj.attributes, 'faces', obj.nTriangles);
    w('GOCAD TSurf 1'); w('HEADER {'); w('name:' + oname); w(gocadColorLine('*solid*color', obj.color)); w('}');
    for (const s of gocadCoordsys(zpositive)) w(s);
    if (obj.role === 'fault' || obj.role === 'topography') w(`GEOLOGICAL_TYPE ${obj.role === 'fault' ? 'fault' : 'topographic'}`);
    if (cols.length) gocadPropertyHeader(w, cols);
    if (fcols.length) {
      w('TRGL_PROPERTIES ' + fcols.map(c => c[0]).join(' '));
      w('TRGL_NO_DATA_VALUES ' + fcols.map(() => gocadFmt(GOCAD_NO_DATA)).join(' '));
      w('TRGL_ESIZES ' + fcols.map(() => '1').join(' '));
    }
    w('TFACE');
    const v = obj.vertices;
    for (let i = 0; i < n; i++) {
      const x = v[3 * i], y = v[3 * i + 1], z = zsign * v[3 * i + 2];
      if (cols.length) w(`PVRTX ${i + 1} ${gocadFmt(x)} ${gocadFmt(y)} ${gocadFmt(z)} ${cols.map(c => gocadFmt(c[1][i])).join(' ')}`);
      else w(`VRTX ${i + 1} ${gocadFmt(x)} ${gocadFmt(y)} ${gocadFmt(z)}`);
    }
    const t = obj.triangles;
    for (let k = 0; k + 2 < t.length; k += 3) {
      let line = `TRGL ${t[k] + 1} ${t[k + 1] + 1} ${t[k + 2] + 1}`;
      if (fcols.length) line += ' ' + fcols.map(c => gocadFmt(c[1][k / 3])).join(' ');
      w(line);
    }
    w('END');
  } else if (obj.kind === 'lineset') {
    w('GOCAD PLine 1'); w('HEADER {'); w('name:' + oname); w(gocadColorLine('*line*color', obj.color)); w('}');
    for (const s of gocadCoordsys(zpositive)) w(s);
    let vid = 0;
    for (const p of obj.parts || []) {
      if (p.length < 2) continue;
      w('ILINE');
      const ids = [];
      for (const i of p) { const [x, y, z] = obj.vertex(i); vid++; ids.push(vid); w(`VRTX ${vid} ${gocadFmt(x)} ${gocadFmt(y)} ${gocadFmt(zsign * z)}`); }
      for (let k = 0; k < ids.length - 1; k++) w(`SEG ${ids[k]} ${ids[k + 1]}`);
    }
    w('END');
  } else if (obj.kind === 'points') {
    const n = obj.n;
    const cols = gocadNumericColumns(obj.attributes, 'vertices', n);
    w('GOCAD VSet 1'); w('HEADER {'); w('name:' + oname); w(gocadColorLine('*atoms*color', obj.color)); w('}');
    for (const s of gocadCoordsys(zpositive)) w(s);
    if (cols.length) gocadPropertyHeader(w, cols);
    w('SUBVSET');
    for (let i = 0; i < n; i++) {
      const [x, y, z] = obj.point(i);
      if (cols.length) w(`PVRTX ${i + 1} ${gocadFmt(x)} ${gocadFmt(y)} ${gocadFmt(zsign * z)} ${cols.map(c => gocadFmt(c[1][i])).join(' ')}`);
      else w(`VRTX ${i + 1} ${gocadFmt(x)} ${gocadFmt(y)} ${gocadFmt(zsign * z)}`);
    }
    w('END');
  } else throw new TypeError(`write_gocad: cannot write a ${pyReprStr(obj.kind)} object`);
  return utf8(L.join('\n') + '\n');
}

/* ========================================================================
   lfmsh — Leapfrog binary mesh (.msh): '%%ARANZ-1.0' text header with an
   [index] of 'Name Type dims...;' entries, '[binary]', 12 mystery bytes,
   then the arrays in index order (LE).
   ======================================================================== */
const LFMSH_MAGIC = '%%ARANZ-1.0';
const LFMSH_PREFIX = [15732735, 1115938331, 1072939210];
const LFMSH_SPEC_WARNING = 'Leapfrog .msh is a reverse-engineered format (no public specification): the header/array layout follows the community converters; the meaning of the 12 bytes after [binary] and the encoding of any extra arrays are unknown';
const LFMSH_DTYPES = { integer: ['i', 4], int: ['i', 4], int32: ['i', 4], double: ['d', 8], float64: ['d', 8], float: ['f', 4], single: ['f', 4], float32: ['f', 4],
  long: ['q', 8], int64: ['q', 8], short: ['h', 2], int16: ['h', 2], byte: ['B', 1], uint8: ['B', 1], char: ['b', 1] };
/** [index] section -> [{name, tc, size, dims, dtype}] (dims in file order: [3, N] = N rows of 3). */
export function parseLfMshIndex(headerText) {
  const entries = [];
  const m = /\[index\]([\s\S]*?)(?:\[binary\]|$)/i.exec(headerText);
  const body = m ? m[1] : headerText;
  for (let stmt of body.split(';')) {
    stmt = pyStrip(stmt);
    if (!stmt) continue;
    const toks = pySplit(stmt);
    if (toks.length < 3) continue;
    const name = toks[0], dtype = toks[1].toLowerCase();
    const [tc, size] = LFMSH_DTYPES[dtype] || [null, null];
    const dims = toks.slice(2).map(pyInt);
    if (dims.some(d => d === undefined)) continue;
    entries.push({ name, tc, size, dims, dtype });
  }
  return entries;
}
export function readLfMsh(src, opts = {}) {
  const data = toU8(src);
  if (!bytesEq(data, 0, LFMSH_MAGIC.slice(0, 7))) throw new FormatError('not a Leapfrog mesh (missing %%ARANZ header)');
  const bpos = findBytes(data, encodeAscii('[binary]'));
  if (bpos < 0) throw new FormatError('Leapfrog mesh: no [binary] marker');
  const headerText = decodeLatin1(data.subarray(0, bpos + 8));
  const entries = parseLfMshIndex(headerText);
  const warnings = [LFMSH_SPEC_WARNING];
  if (!entries.length) throw new FormatError('Leapfrog mesh: empty [index] section');
  for (const e of entries) if (e.tc === null) throw new FormatError(`Leapfrog mesh: unknown array type ${pyReprStr(e.dtype)} for ${pyReprStr(e.name)}`);
  let off = bpos + 8;
  while (off < data.length && (data[off] === 10 || data[off] === 13)) off++;
  off += 12;
  let expected = 0;
  for (const e of entries) expected += e.dims.reduce((a, b) => a * b, 1) * e.size;
  const avail = data.length - off;
  if (avail !== expected) {
    let fixed = null;
    for (const delta of [-1, -2, 1, 2, -3, 3, -4, 4, -12, 12]) { const o2 = off + delta; if (o2 >= 0 && o2 <= data.length && data.length - o2 === expected) { fixed = o2; break; } }
    if (fixed === null) {
      if (avail < expected) throw new FormatError(`Leapfrog mesh: payload has ${avail} bytes, index needs ${expected}`);
      warnings.push(`${avail - expected} trailing byte(s) after the indexed arrays ignored`);
    } else { warnings.push(`binary payload found ${fixed - off >= 0 ? '+' : ''}${fixed - off} byte(s) from the expected offset`); off = fixed; }
  }
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const readers = { i: o => dv.getInt32(o, true), d: o => dv.getFloat64(o, true), f: o => dv.getFloat32(o, true), q: o => Number(dv.getBigInt64(o, true)), h: o => dv.getInt16(o, true), B: o => dv.getUint8(o), b: o => dv.getInt8(o) };
  const arrays = new Map(), order = [];
  for (const e of entries) {
    const n = e.dims.reduce((a, b) => a * b, 1);
    const arr = new Float64Array(n);
    const rd = readers[e.tc];
    for (let k = 0; k < n; k++) arr[k] = rd(off + k * e.size);
    off += n * e.size;
    arrays.set(e.name, [arr, e.dims]);
    order.push(e.name);
  }
  const find = (prefixes) => {
    for (const nm of order) if (prefixes.includes(nm.toLowerCase())) return nm;
    for (const nm of order) if (prefixes.some(p => nm.toLowerCase().startsWith(p))) return nm;
    return null;
  };
  const triName = find(['tri', 'triangles', 'faces', 'indices']);
  const locName = find(['location', 'locations', 'vertices', 'points', 'nodes']);
  if (locName === null) throw new FormatError(`Leapfrog mesh: no Location array in index (${order.join(', ')})`);
  const [verts, ldims] = arrays.get(locName);
  if (ldims.length && ldims[0] !== 3) warnings.push(`Location rows have ${ldims[0]} components, expected 3`);
  let tris;
  if (triName === null) { tris = new Uint32Array(0); warnings.push('no Tri array in index: vertices only'); }
  else {
    let [tri] = arrays.get(triName);
    const nv = verts.length / 3;
    if (tri.length) {
      let mn = Infinity, mx = -Infinity;
      for (const t of tri) { if (t < mn) mn = t; if (t > mx) mx = t; }
      if (mn < 0 || mx >= nv) {
        if (mn >= 1 && mx <= nv) { warnings.push('triangle indices look 1-based; shifted to 0-based'); tri = tri.map(t => t - 1); }
        else throw new FormatError(`Leapfrog mesh: triangle index out of range (${mn}..${mx} of ${nv} vertices)`);
      }
    }
    tris = Uint32Array.from(tri);
  }
  const name = opts.name || (opts.file ? stem(opts.file, 'leapfrog mesh') : 'leapfrog mesh');
  const mesh = new GM.Mesh({ vertices: verts, triangles: tris, name });
  setProvenance(mesh, 'lf_msh', opts.file);
  const nv = mesh.nVertices, nt = mesh.nTriangles;
  const extra = [];
  for (const nm of order) {
    if (nm === triName || nm === locName) continue;
    const [arr, dims] = arrays.get(nm);
    const rows = dims.length ? dims[dims.length - 1] : arr.length;
    let width = 1;
    for (const d of dims.slice(0, -1)) width *= d;
    extra.push(`${nm}(${dims.join('x')})`);
    const locKind = rows === nv ? 'vertices' : (rows === nt ? 'faces' : null);
    if (locKind === null) {
      warnings.push(`array ${pyReprStr(nm)} (${rows} rows) matches neither vertices nor triangles; kept in metadata`);
      (mesh.metadata.extra_arrays = mesh.metadata.extra_arrays || {})[nm] = Array.from(arr.subarray(0, 1000));
      continue;
    }
    if (width === 1) mesh.attributes[nm] = { location: locKind, values: Float32Array.from(arr) };
    else for (let c = 0; c < width; c++) { const col = new Float32Array(rows); for (let r = 0; r < rows; r++) col[r] = arr[r * width + c]; mesh.attributes[`${nm}_${c + 1}`] = { location: locKind, values: col }; }
    warnings.push(`array ${pyReprStr(nm)} mapped to a per-${locKind} attribute; its meaning/encoding is unknown`);
  }
  mesh.metadata.index = entries.map(e => ({ name: e.name, dtype: e.dtype, dims: e.dims }));
  if (extra.length) mesh.metadata.extra_arrays_listed = extra;
  mesh.metadata.warnings = warnings;
  return mesh;
}
export function writeLfMsh(mesh) {
  const nt = mesh.nTriangles, nv = mesh.nVertices;
  const header = encodeAscii(`%%ARANZ-1.0\n\n[index]\nTri Integer 3 ${nt};\nLocation Double 3 ${nv};\n\n[binary]\n`);
  const w = new ByteWriter(header.length + 12 + 12 * nt + 24 * nv);
  w.bytes(header);
  for (const v of LFMSH_PREFIX) w.i32(v);
  for (let k = 0; k < 3 * nt; k++) w.i32(mesh.triangles[k]);
  for (let k = 0; k < 3 * nv; k++) w.f64(mesh.vertices[k]);
  return w.result();
}

/* ========================================================================
   tables — RFC-4180 CSV parser with delimiter sniffing + points /
   drillholes / structural / block-model tables (Leapfrog column synonyms)
   ======================================================================== */
export const X_SYN = ['x', 'east', 'easting', 'xcoord', 'coordx', 'xcollar', 'collarx', 'xutm', 'utmx', 'utme', 'xm', 'eastm', 'lon', 'longitude', 'e'];
export const Y_SYN = ['y', 'north', 'northing', 'ycoord', 'coordy', 'ycollar', 'collary', 'yutm', 'utmy', 'utmn', 'ym', 'northm', 'lat', 'latitude', 'n'];
export const Z_SYN = ['z', 'elev', 'elevation', 'rl', 'alt', 'altitude', 'zcoord', 'coordz', 'zcollar', 'collarz', 'zm', 'elevm', 'height'];
const HOLE_SYN = ['holeid', 'hole', 'bhid', 'dhid', 'ddh', 'drillhole', 'hole_no', 'holeno', 'holename', 'id', 'name', 'well', 'wellid'];
const DEPTH_SYN = ['depth', 'maxdepth', 'eoh', 'totaldepth', 'td', 'length', 'finaldepth', 'enddepth', 'holedepth', 'depthm'];
const SVY_DEPTH_SYN = ['depth', 'at', 'distance', 'md', 'dist', 'surveydepth', 'measureddepth', 'station'];
const AZI_SYN = ['azimuth', 'azi', 'az', 'bearing', 'brg', 'azim', 'trend', 'direction'];
const DIP_SYN = ['dip', 'inclination', 'incl', 'inc', 'plunge', 'dipangle'];
const FROM_SYN = ['from', 'depthfrom', 'start', 'fromm', 'fromdepth', 'top', 'startdepth', 'depfrom'];
const TO_SYN = ['to', 'depthto', 'end', 'tom', 'todepth', 'bottom', 'enddepth', 'depto'];
const DIPAZ_SYN = ['dipazimuth', 'dipdir', 'dipdirection', 'dipazi', 'azimuth', 'azi', 'dipaz', 'ddr'];
const STRIKE_SYN = ['strike', 'strikeazimuth', 'strikedir'];
const POLARITY_SYN = ['polarity', 'pol', 'younging', 'facing', 'overturned'];
const BM_X_SYN = ['x', 'xc', 'xcentre', 'xcenter', 'centroidx', 'xcentroid', 'centrex', 'centerx', 'xworld', 'east', 'easting', 'xmid', 'midx'];
const BM_Y_SYN = ['y', 'yc', 'ycentre', 'ycenter', 'centroidy', 'ycentroid', 'centrey', 'centery', 'yworld', 'north', 'northing', 'ymid', 'midy'];
const BM_Z_SYN = ['z', 'zc', 'zcentre', 'zcenter', 'centroidz', 'zcentroid', 'centrez', 'centerz', 'zworld', 'elev', 'elevation', 'rl', 'zmid', 'midz'];
const DX_SYN = ['dx', 'xinc', 'xsize', 'sizex', 'xlength', 'xdim', 'dimx', 'blocksizex', 'xblocksize', 'lengthx', 'xlen'];
const DY_SYN = ['dy', 'yinc', 'ysize', 'sizey', 'ylength', 'ydim', 'dimy', 'blocksizey', 'yblocksize', 'lengthy', 'ylen'];
const DZ_SYN = ['dz', 'zinc', 'zsize', 'sizez', 'zlength', 'zdim', 'dimz', 'blocksizez', 'zblocksize', 'lengthz', 'zlen'];

/** Header normalisation: lower-case letters and digits only. */
export function normHeader(name) { return String(name == null ? '' : name).toLowerCase().replace(/[^a-z0-9]/g, ''); }

/** Cell -> number or null (blank / na / nan / null / none / - / #n/a; thousands separators tolerated). */
function cellNum(s) {
  if (s === null || s === undefined) return null;
  if (typeof s === 'number') return s;
  const t = pyStrip(String(s));
  if (!t || ['na', 'n/a', 'nan', 'null', 'none', '-', '#n/a'].includes(t.toLowerCase())) return null;
  let v = pyFloat(t);
  if (v === undefined) v = pyFloat(t.replace(/,/g, ''));
  return v === undefined ? null : v;
}
function isNumericColumn(values) {
  let seen = false;
  for (const v of values) {
    const t = v === null || v === undefined ? '' : pyStrip(String(v));
    if (!t) continue;
    if (cellNum(t) === null) return false;
    seen = true;
  }
  return seen;
}
/** Python str() of a cell value for CSV output ('' for null / NaN, repr-short for floats). */
function csvFmt(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') return isFinite(v) ? reprShort(v) : '';
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  if (Array.isArray(v)) return '[' + v.map(x => typeof x === 'string' ? pyReprStr(x) : csvFmt(x)).join(', ') + ']';
  return String(v);
}
function countOutsideQuotes(line, d) {
  let n = 0, inQ = false;
  for (const ch of line) { if (ch === '"') inQ = !inQ; else if (ch === d && !inQ) n++; }
  return n;
}
/** Delimiter sniffing over , ; tab | (consistency across lines, then first-line counts, then whitespace). */
export function sniffDialect(text) {
  const sample = text.slice(0, 8192);
  const lines = pySplitlines(sample).filter(l => pyStrip(l));
  const first = lines.length ? lines[0] : '';
  const pref = [',', '\t', ';', '|'];
  let best = null, bestScore = 0;
  for (const d of [',', ';', '\t', '|']) {
    const freq = new Map();
    for (const l of lines) { const c = countOutsideQuotes(l, d); freq.set(c, (freq.get(c) || 0) + 1); }
    let mode = -1, mf = 0;
    for (const [c, f] of freq) if (c > 0 && (f > mf || (f === mf && c > mode))) { mode = c; mf = f; }
    if (mode < 1) continue;
    const score = mf / lines.length;
    if (score >= 0.9 && (score > bestScore || (score === bestScore && pref.indexOf(d) < pref.indexOf(best)))) { best = d; bestScore = score; }
  }
  if (best !== null && first.split(best).length >= 2) {
    // csv.Sniffer: skipinitialspace when every delimiter of the first line is followed by a space
    const c1 = first.split(best).length - 1, c2 = first.split(best + ' ').length - 1;
    return { delimiter: best, skipInitialSpace: c1 === c2 };
  }
  const counts = [',', ';', '\t', '|'].map(d => [d, first.split(d).length - 1]);
  let top = counts[0];
  for (const c of counts) if (c[1] > top[1]) top = c;
  if (top[1] > 0) return { delimiter: top[0], skipInitialSpace: false };
  if (pySplit(first).length >= 2) return { delimiter: ' ', skipInitialSpace: true };
  return { delimiter: ',', skipInitialSpace: false };
}
/** RFC-4180 rows (quotes, doubled quotes, embedded newlines); delimiter ' ' collapses runs of spaces. */
export function parseCsvRows(text, delim = ',', skipInitial = false) {
  const rows = [];
  let row = [], field = '', i = 0, inQ = false, quoted = false, start = true;
  const n = text.length;
  const ws = delim === ' ';
  while (i < n) {
    const ch = text[i];
    if (inQ) {
      if (ch === '"') { if (text[i + 1] === '"') { field += '"'; i += 2; } else { inQ = false; i++; } }
      else { field += ch; i++; }
      continue;
    }
    if (start && (skipInitial || ws) && ch === ' ') { i++; continue; }
    if (ch === '"' && start) { inQ = true; quoted = true; start = false; i++; continue; }
    if (ch === delim || (ws && ch === '\t')) {
      row.push(field); field = ''; start = true; quoted = false; i++;
      if (ws) while (i < n && (text[i] === ' ' || text[i] === '\t')) i++;
      continue;
    }
    if (ch === '\r' || ch === '\n') {
      if (!(ws && start && row.length && field === '')) row.push(field);
      rows.push(row); row = []; field = ''; start = true; quoted = false; i++;
      if (ch === '\r' && text[i] === '\n') i++;
      continue;
    }
    field += ch; start = false; i++;
  }
  if (field !== '' || row.length || quoted) { if (!(ws && start && row.length && field === '')) row.push(field); rows.push(row); }
  return rows;
}
function csvQuote(s, delim) {
  let need = false;
  for (const ch of s) if (ch === delim || ch === '"' || ch === '\n' || ch === '\r') { need = true; break; }
  return need ? '"' + s.replace(/"/g, '""') + '"' : s;
}
/** Rows -> CSV bytes (python csv.writer QUOTE_MINIMAL, '\n' line ends). */
function csvText(header, rows, delim = ',') {
  const out = [];
  const line = (cells) => { const f = cells.map(c => csvQuote(String(c), delim)); out.push(f.length === 1 && f[0] === '' ? '""' : f.join(delim)); };
  line(header);
  for (const r of rows) line(r.map(csvFmt));
  return utf8(out.join('\n') + '\n');
}

const KV_RE = /^\s*#?\s*([A-Za-z_][\w .\-/()]*?)\s*:\s*(.*)$/;
/** Parsed delimited table: headers, columns {header: [cells]}, n, preamble, warnings, delimiter. */
export class Table {
  constructor(headers, columns, preamble, warnings, delimiter) {
    this.headers = headers; this.columns = columns; this.n = headers.length ? columns[headers[0]].length : 0;
    this.preamble = preamble; this.warnings = warnings; this.delimiter = delimiter;
  }
  find(synonyms, explicit = null, exclude = []) {
    if (explicit !== null && explicit !== undefined) {
      if (explicit in this.columns) return explicit;
      for (const h of this.headers) if (normHeader(h) === normHeader(explicit)) return h;
      throw new FormatError(`column ${pyReprStr(explicit)} not in [${this.headers.map(pyReprStr).join(', ')}]`);
    }
    const normed = this.headers.filter(h => !exclude.includes(h)).map(h => [normHeader(h), h]);
    for (const s of synonyms) { const ns = normHeader(s); for (const [nh, h] of normed) if (nh === ns) return h; }
    return null;
  }
  numeric(header) { return this.columns[header].map(cellNum); }
  isNumeric(header) { return isNumericColumn(this.columns[header]); }
  typed(header) { return this.isNumeric(header) ? this.numeric(header) : this.columns[header].map(v => v == null ? '' : pyStrip(String(v))); }
}
/** Delimited text -> Table; leading '#' lines and 'key: value' lines go to preamble. */
export function parseTable(src, opts = {}) {
  const text = typeof src === 'string' ? src : decodeText(toU8(src), true);
  const lines = pySplitlines(text);
  const pre = {}, preLines = [], warnings = [];
  let start = 0;
  if (opts.preamble !== false) {
    while (start < lines.length) {
      const s = pyStrip(lines[start]);
      if (!s) { start++; continue; }
      if (s.startsWith('#')) {
        preLines.push(s);
        const m = KV_RE.exec(s);
        if (m) pre[pyStrip(m[1])] = pyStrip(m[2]);
        start++;
        continue;
      }
      const m = KV_RE.exec(s);
      if (m && ![',', ';', '\t', '|'].some(d => m[1].indexOf(d) >= 0) && s.indexOf(':') >= 0) { preLines.push(s); pre[pyStrip(m[1])] = pyStrip(m[2]); start++; continue; }
      break;
    }
  }
  const body = lines.slice(start).join('\n');
  if (!pyStrip(body)) throw new FormatError('empty table');
  const dialect = sniffDialect(body);
  let rows = parseCsvRows(body, dialect.delimiter, dialect.skipInitialSpace);
  rows = rows.filter(r => r.some(c => pyStrip(c)));
  if (!rows.length) throw new FormatError('table has no rows');
  let headers = rows[0].map(pyStrip);
  const seen = new Map();
  headers = headers.map((h, k) => {
    if (!h) h = `col${k + 1}`;
    if (seen.has(h)) { seen.set(h, seen.get(h) + 1); h = `${h}_${seen.get(h)}`; } else seen.set(h, 1);
    return h;
  });
  const ncol = headers.length;
  const columns = {};
  for (const h of headers) columns[h] = [];
  let ragged = 0;
  for (let i = 1; i < rows.length; i++) {
    let r = rows[i];
    if (r.length !== ncol) { ragged++; r = r.concat(new Array(Math.max(0, ncol - r.length)).fill('')).slice(0, ncol); }
    for (let k = 0; k < ncol; k++) columns[headers[k]].push(pyStrip(r[k]));
  }
  if (ragged) warnings.push(`${ragged} row(s) had a different number of cells than the header (padded / truncated)`);
  const t = new Table(headers, columns, pre, warnings, dialect.delimiter);
  t.preambleLines = preLines;
  return t;
}
function tableProv(fmt, file) { return { format: fmt, file: file || null }; }

/* ------------------------------------------------------------------ points */
/** Point table -> PointSet. opts: x/y/z explicit column names, name, role, file. */
export function readPointsCsv(src, opts = {}) {
  const t = parseTable(src);
  const warnings = t.warnings.slice();
  const xc = t.find(X_SYN, opts.x), yc = t.find(Y_SYN, opts.y, [xc]);
  if (xc === null || yc === null) throw new FormatError(`no X / Y columns found in [${t.headers.map(pyReprStr).join(', ')}]`);
  const zc = t.find(Z_SYN, opts.z, [xc, yc]);
  if (zc === null) warnings.push(`no Z column (${Z_SYN.slice(0, 4).join(', ')}): Z set to 0`);
  const xs = t.numeric(xc), ys = t.numeric(yc);
  const zs = zc ? t.numeric(zc) : new Array(t.n).fill(0.0);
  const attrCols = t.headers.filter(h => h !== xc && h !== yc && h !== zc);
  const typed = {};
  for (const h of attrCols) typed[h] = t.typed(h);
  const xyz = [], attrs = {};
  for (const h of attrCols) attrs[h] = [];
  let skipped = 0;
  for (let i = 0; i < t.n; i++) {
    if (xs[i] === null || ys[i] === null) { skipped++; continue; }
    xyz.push(xs[i], ys[i], zs[i] === null ? NAN : zs[i]);
    for (const h of attrCols) attrs[h].push(typed[h][i]);
  }
  const ps = new GM.PointSet({ name: opts.name || stem(opts.file, 'points'), role: opts.role || 'points', xyz: Float64Array.from(xyz), attributes: attrs });
  ps.provenance = tableProv('csv_points', opts.file);
  if (skipped) warnings.push(`${skipped} row(s) without numeric X/Y skipped`);
  ps.metadata.columns = { x: xc, y: yc, z: zc };
  ps.metadata.warnings = warnings;
  return ps;
}
/** PointSet -> 'x,y,z,<attrs>' CSV (opts.leapfrog -> 'East,North,Elev'; opts.columns limits / orders attributes). */
export function writePointsCsv(points, opts = {}) {
  const cols = opts.columns ? Array.from(opts.columns) : Object.keys(points.attributes);
  const header = (opts.leapfrog ? ['East', 'North', 'Elev'] : ['x', 'y', 'z']).concat(cols);
  const rows = [];
  for (let i = 0; i < points.n; i++) {
    const [x, y, z] = points.point(i);
    const row = [x, y, z];
    for (const c of cols) { const col = points.attributes[c] || []; row.push(i < col.length ? col[i] : null); }
    rows.push(row);
  }
  return csvText(header, rows);
}

/* -------------------------------------------------------------- drillholes */
function holeId(v) { let s = v == null ? '' : pyStrip(String(v)); if (/^-?\d+\.0$/.test(s)) s = s.slice(0, -2); return s; }
/** {collar, survey?, intervals?: {table: bytes}} -> Drillholes (dips stored positive down).
    opts.negativeDipDown negates survey dips; opts.name; opts.file (collar filename). */
export function readDrillholes(sources, opts = {}) {
  if (sources instanceof Uint8Array || sources instanceof ArrayBuffer || typeof sources === 'string') sources = { collar: sources };
  const warnings = [];
  const t = parseTable(sources.collar);
  for (const w of t.warnings) warnings.push('collar: ' + w);
  const hc = t.find(HOLE_SYN);
  if (hc === null) throw new FormatError(`collar table: no hole id column in [${t.headers.map(pyReprStr).join(', ')}]`);
  const xc = t.find(X_SYN, null, [hc]), yc = t.find(Y_SYN, null, [hc, xc]), zc = t.find(Z_SYN, null, [hc, xc, yc]);
  if (xc === null || yc === null) throw new FormatError(`collar table: no X / Y columns in [${t.headers.map(pyReprStr).join(', ')}]`);
  if (zc === null) warnings.push('collar: no Z column, collar elevations set to 0');
  const dc = t.find(DEPTH_SYN, null, [hc, xc, yc, zc]);
  if (dc === null) warnings.push('collar: no depth column (depth / max_depth / eoh)');
  const extra = t.headers.filter(h => ![hc, xc, yc, zc, dc].includes(h));
  const typed = {};
  for (const h of extra) typed[h] = t.typed(h);
  const xs = t.numeric(xc), ys = t.numeric(yc);
  const zs = zc ? t.numeric(zc) : new Array(t.n).fill(0.0);
  const ds = dc ? t.numeric(dc) : new Array(t.n).fill(null);
  const collars = [];
  const seen = new Set();
  for (let i = 0; i < t.n; i++) {
    const hole = holeId(t.columns[hc][i]);
    if (!hole || xs[i] === null || ys[i] === null) { warnings.push(`collar row ${i + 2} skipped (missing hole id or coordinates)`); continue; }
    if (seen.has(hole)) warnings.push(`duplicate collar ${pyReprStr(hole)} (kept both)`);
    seen.add(hole);
    const rec = { hole, x: xs[i], y: ys[i], z: zs[i] === null ? 0.0 : zs[i], depth: ds[i] };
    for (const h of extra) rec[h] = typed[h][i];
    collars.push(rec);
  }
  const surveys = [];
  const negativeDipDown = !!opts.negativeDipDown;
  if (sources.survey !== undefined && sources.survey !== null) {
    const s = parseTable(sources.survey);
    for (const w of s.warnings) warnings.push('survey: ' + w);
    const shc = s.find(HOLE_SYN), sdc = s.find(SVY_DEPTH_SYN, null, [shc]), sac = s.find(AZI_SYN, null, [shc, sdc]), sic = s.find(DIP_SYN, null, [shc, sdc, sac]);
    if ([shc, sdc, sac, sic].includes(null)) throw new FormatError(`survey table: need hole / depth / azimuth / dip columns, got [${s.headers.map(pyReprStr).join(', ')}]`);
    const sd = s.numeric(sdc), sa = s.numeric(sac), si = s.numeric(sic);
    const sextra = s.headers.filter(h => ![shc, sdc, sac, sic].includes(h));
    const styped = {};
    for (const h of sextra) styped[h] = s.typed(h);
    let neg = 0, pos = 0;
    for (let i = 0; i < s.n; i++) {
      const hole = holeId(s.columns[shc][i]);
      if (!hole || sd[i] === null || si[i] === null) { warnings.push(`survey row ${i + 2} skipped (missing hole / depth / dip)`); continue; }
      const dip = negativeDipDown ? -si[i] : si[i];
      if (dip < 0) neg++; else if (dip > 0) pos++;
      const rec = { hole, depth: sd[i], azimuth: sa[i] === null ? 0.0 : sa[i], dip };
      for (const h of sextra) rec[h] = styped[h][i];
      surveys.push(rec);
    }
    if (neg && !pos) warnings.push(`survey: all dips are negative after conversion -- if the file uses negative-down dips pass negative_dip_down=${negativeDipDown ? 'False' : 'True'}`);
    if (!negativeDipDown && neg && pos) warnings.push(`survey: mixed dip signs (${neg} negative, ${pos} positive)`);
    const holes = new Set(collars.map(c => c.hole));
    const orphans = Array.from(new Set(surveys.filter(r => !holes.has(r.hole)).map(r => r.hole))).sort();
    if (orphans.length) warnings.push(`survey: ${orphans.length} hole id(s) not in collar table (e.g. ${orphans.slice(0, 5).join(', ')})`);
  } else warnings.push('no survey table: holes are treated as vertical');
  const intervals = {};
  for (const [table, isrc] of Object.entries(sources.intervals || {})) {
    const it = parseTable(isrc);
    for (const w of it.warnings) warnings.push(`${table}: ${w}`);
    const ihc = it.find(HOLE_SYN), ifc = it.find(FROM_SYN, null, [ihc]), itc = it.find(TO_SYN, null, [ihc, ifc]);
    if ([ihc, ifc, itc].includes(null)) throw new FormatError(`${table} table: need hole / from / to columns, got [${it.headers.map(pyReprStr).join(', ')}]`);
    const fr = it.numeric(ifc), to = it.numeric(itc);
    const iextra = it.headers.filter(h => ![ihc, ifc, itc].includes(h));
    const ityped = {};
    for (const h of iextra) ityped[h] = it.typed(h);
    const rows = [];
    let bad = 0;
    for (let i = 0; i < it.n; i++) {
      const hole = holeId(it.columns[ihc][i]);
      if (!hole || fr[i] === null || to[i] === null) { bad++; continue; }
      if (to[i] < fr[i]) warnings.push(`${table}: hole ${hole} interval ${fmtG(fr[i])}-${fmtG(to[i])} reversed`);
      const rec = { hole, from: fr[i], to: to[i] };
      for (const h of iextra) rec[h] = ityped[h][i];
      rows.push(rec);
    }
    if (bad) warnings.push(`${table}: ${bad} row(s) without hole / from / to skipped`);
    const holes = new Set(collars.map(c => c.hole));
    const orphans = Array.from(new Set(rows.filter(r => !holes.has(r.hole)).map(r => r.hole))).sort();
    if (orphans.length) warnings.push(`${table}: ${orphans.length} hole id(s) not in collar table (e.g. ${orphans.slice(0, 5).join(', ')})`);
    intervals[table] = rows;
  }
  const dh = new GM.Drillholes({ collars, surveys, intervals, name: opts.name || stem(opts.file, 'drillholes') });
  dh.provenance = { format: 'csv_drillholes', file: opts.file || null, survey: opts.surveyFile || null, intervals: opts.intervalFiles || {} };
  dh.metadata.columns = { hole: hc, x: xc, y: yc, z: zc, depth: dc };
  dh.metadata.dip_convention = 'positive down';
  dh.metadata.warnings = warnings;
  return dh;
}
/** Drillholes -> {'collar.csv', 'survey.csv', '<table>.csv'} bytes. */
export function writeDrillholes(dh) {
  const out = {};
  const extra = [];
  for (const c of dh.collars) for (const k of Object.keys(c)) if (!['hole', 'x', 'y', 'z', 'depth'].includes(k) && !extra.includes(k)) extra.push(k);
  out['collar.csv'] = csvText(['holeid', 'x', 'y', 'z', 'max_depth'].concat(extra), dh.collars.map(c => [c.hole, c.x, c.y, c.z, c.depth].concat(extra.map(k => c[k]))));
  const sextra = [];
  for (const s of dh.surveys) for (const k of Object.keys(s)) if (!['hole', 'depth', 'azimuth', 'dip'].includes(k) && !sextra.includes(k)) sextra.push(k);
  out['survey.csv'] = csvText(['holeid', 'depth', 'azimuth', 'dip'].concat(sextra), dh.surveys.map(s => [s.hole, s.depth, s.azimuth, s.dip].concat(sextra.map(k => s[k]))));
  for (const [table, recs] of Object.entries(dh.intervals)) {
    const cols = [];
    for (const r of recs) for (const k of Object.keys(r)) if (!['hole', 'from', 'to'].includes(k) && !cols.includes(k)) cols.push(k);
    const safe = String(table).replace(/[^\w\-]+/g, '_') || 'intervals';
    out[`${safe}.csv`] = csvText(['holeid', 'from', 'to'].concat(cols), recs.map(r => [r.hole, r.from, r.to].concat(cols.map(k => r[k]))));
  }
  return out;
}

/* -------------------------------------------------------------- structural */
/** Planar structural CSV -> PointSet(role 'structural') with dip / dip_azimuth (+ polarity) attributes. */
export function readStructuralCsv(src, opts = {}) {
  const t = parseTable(src);
  const warnings = t.warnings.slice();
  const xc = t.find(X_SYN), yc = t.find(Y_SYN, null, [xc]), zc = t.find(Z_SYN, null, [xc, yc]);
  if (xc === null || yc === null) throw new FormatError(`structural table: no X / Y columns in [${t.headers.map(pyReprStr).join(', ')}]`);
  if (zc === null) warnings.push('no Z column: Z set to 0');
  const dipc = t.find(DIP_SYN, null, [xc, yc, zc]);
  const dazc = t.find(DIPAZ_SYN, null, [xc, yc, zc, dipc]);
  const strc = t.find(STRIKE_SYN, null, [xc, yc, zc, dipc, dazc]);
  const polc = t.find(POLARITY_SYN, null, [xc, yc, zc, dipc, dazc, strc]);
  if (dipc === null && dazc === null && strc === null) throw new FormatError(`structural table: no dip / dip_azimuth / strike columns in [${t.headers.map(pyReprStr).join(', ')}]`);
  if (dipc === null) warnings.push('no dip column: dip set to NaN');
  const xs = t.numeric(xc), ys = t.numeric(yc);
  const zs = zc ? t.numeric(zc) : new Array(t.n).fill(0.0);
  const dips = dipc ? t.numeric(dipc) : new Array(t.n).fill(null);
  let dazs;
  if (dazc !== null) {
    dazs = t.numeric(dazc);
    if (strc !== null) warnings.push(`both ${pyReprStr(dazc)} and ${pyReprStr(strc)} present: dip azimuth taken from ${pyReprStr(dazc)}`);
  } else if (strc !== null) {
    dazs = t.numeric(strc).map(s => s === null ? null : (((s + 90.0) % 360.0) + 360.0) % 360.0);
    warnings.push(`dip_azimuth derived from ${pyReprStr(strc)} with the right-hand rule (strike + 90)`);
  } else { dazs = new Array(t.n).fill(null); warnings.push('no dip azimuth / strike column: dip_azimuth set to NaN'); }
  const used = [xc, yc, zc, dipc, dazc, strc, polc];
  const extra = t.headers.filter(h => !used.includes(h));
  const typed = {};
  for (const h of extra) typed[h] = t.typed(h);
  const pols = polc ? t.typed(polc) : null;
  const order = ['dip', 'dip_azimuth'].concat(pols !== null ? ['polarity'] : []).concat(extra);
  const attrs = {};
  for (const h of order) attrs[h] = [];
  const xyz = [];
  let skipped = 0;
  for (let i = 0; i < t.n; i++) {
    if (xs[i] === null || ys[i] === null) { skipped++; continue; }
    xyz.push(xs[i], ys[i], zs[i] === null ? NAN : zs[i]);
    attrs.dip.push(dips[i]);
    attrs.dip_azimuth.push(dazs[i]);
    if (pols !== null) attrs.polarity.push(pols[i]);
    for (const h of extra) attrs[h].push(typed[h][i]);
  }
  const ps = new GM.PointSet({ name: opts.name || stem(opts.file, 'structural'), role: 'structural', xyz: Float64Array.from(xyz), attributes: attrs });
  ps.provenance = tableProv('csv_structural', opts.file);
  if (skipped) warnings.push(`${skipped} row(s) without numeric X/Y skipped`);
  ps.metadata.columns = { x: xc, y: yc, z: zc, dip: dipc, dip_azimuth: dazc, strike: strc, polarity: polc };
  ps.metadata.warnings = warnings;
  return ps;
}
export function writeStructuralCsv(points) {
  const base = ['dip', 'dip_azimuth', 'polarity'];
  const extra = Object.keys(points.attributes).filter(k => !base.includes(k));
  const header = ['x', 'y', 'z'].concat(base, extra);
  const rows = [];
  for (let i = 0; i < points.n; i++) {
    const [x, y, z] = points.point(i);
    const row = [x, y, z];
    for (const c of base.concat(extra)) { const col = points.attributes[c] || []; row.push(i < col.length ? col[i] : null); }
    rows.push(row);
  }
  return csvText(header, rows);
}

/* ------------------------------------------------------------- block model */
function inferSpacing(values) {
  const u = Array.from(new Set(values.filter(v => v !== null))).sort((a, b) => a - b);
  let best = null;
  for (let i = 0; i + 1 < u.length; i++) { const d = u[i + 1] - u[i]; if (d > 1e-9 && (best === null || d < best)) best = d; }
  return best;
}
function modeOf(values) {
  const counts = new Map();
  for (const v of values) { if (v === null) continue; counts.set(v, (counts.get(v) || 0) + 1); }
  let best = null, bc = -1;
  for (const [v, c] of counts) if (c > bc) { best = v; bc = c; }
  return best;
}
function headerFloats(value) {
  const parts = String(value).trim().split(/[,\s;x]+/).filter(v => v !== '');
  const out = parts.map(pyFloat);
  return out.some(v => v === undefined) ? null : out;
}
function headerGet(pre, ...keys) {
  const normed = {};
  for (const [k, v] of Object.entries(pre)) normed[normHeader(k)] = v;
  for (const k of keys) if (normHeader(k) in normed) return normed[normHeader(k)];
  return null;
}
/** Block-model CSV (centroids + optional sizes + attributes) -> BlockModel on a regular lattice.
    opts.blockSize (number | [dx,dy,dz]) overrides the embedded header / size columns / inferred spacing. */
export function readBlockmodelCsv(src, opts = {}) {
  const t = parseTable(src);
  const warnings = t.warnings.slice();
  const pre = Object.assign({}, t.preamble);
  const xc = t.find(BM_X_SYN), yc = t.find(BM_Y_SYN, null, [xc]), zc = t.find(BM_Z_SYN, null, [xc, yc]);
  if ([xc, yc, zc].includes(null)) throw new FormatError(`block model table: need centroid x / y / z columns, got [${t.headers.map(pyReprStr).join(', ')}]`);
  const dxc = t.find(DX_SYN, null, [xc, yc, zc]), dyc = t.find(DY_SYN, null, [xc, yc, zc, dxc]), dzc = t.find(DZ_SYN, null, [xc, yc, zc, dxc, dyc]);
  const xs = t.numeric(xc), ys = t.numeric(yc), zs = t.numeric(zc);
  let size = null, sizeSource = null;
  if (opts.blockSize !== undefined && opts.blockSize !== null) {
    size = typeof opts.blockSize === 'number' ? [opts.blockSize, opts.blockSize, opts.blockSize] : Array.from(opts.blockSize, Number);
    sizeSource = 'argument';
  }
  if (size === null) {
    const hv = headerGet(pre, 'block_size', 'blocksize', 'block size', 'size', 'cell_size', 'cellsize');
    const fl = hv !== null ? headerFloats(hv) : null;
    if (fl && fl.length) { size = fl.length < 3 ? fl.concat(fl, fl).slice(0, 3) : fl.slice(0, 3); sizeSource = 'embedded header'; }
  }
  if (size === null && dxc !== null && dyc !== null && dzc !== null) {
    const cols = [t.numeric(dxc), t.numeric(dyc), t.numeric(dzc)];
    size = cols.map(modeOf);
    if (size.includes(null)) size = null;
    else {
      sizeSource = 'size columns';
      cols.forEach((c, a) => {
        const distinct = Array.from(new Set(c.filter(v => v !== null))).sort((p, q) => p - q);
        if (distinct.length > 1) warnings.push(`column ${pyReprStr([dxc, dyc, dzc][a])} has ${distinct.length} distinct sizes (${distinct.slice(0, 6).map(d => fmtG(d)).join(', ')}): sub-blocked model regularised to the most common size`);
      });
    }
  }
  if (size === null) {
    size = [inferSpacing(xs), inferSpacing(ys), inferSpacing(zs)];
    sizeSource = 'inferred from centroid spacing';
    ['x', 'y', 'z'].forEach((nm, a) => { if (size[a] === null) { size[a] = 1.0; warnings.push(`cannot infer ${nm} block size (single layer): using 1.0`); } });
  }
  size = size.map(Number);
  if (Math.min(...size) <= 0) throw new FormatError(`block size must be positive: [${size.map(pyRepr).join(', ')}]`);
  let azimuth = 0.0;
  let hv = headerGet(pre, 'azimuth', 'rotation', 'bearing');
  if (hv !== null) { const fl = headerFloats(hv); if (fl && fl.length) azimuth = fl[0]; }
  const valid = [];
  for (let i = 0; i < t.n; i++) if (xs[i] !== null && ys[i] !== null && zs[i] !== null) valid.push(i);
  if (!valid.length) throw new FormatError('block model table: no rows with numeric centroids');
  let origin = null, count = null;
  hv = headerGet(pre, 'base_point', 'basepoint', 'origin', 'base point', 'min_corner', 'minimum');
  let fl = hv !== null ? headerFloats(hv) : null;
  if (fl && fl.length >= 3) origin = fl.slice(0, 3);
  hv = headerGet(pre, 'count', 'counts', 'blocks', 'n_blocks', 'dimensions', 'nx_ny_nz', 'size_in_blocks');
  fl = hv !== null ? headerFloats(hv) : null;
  if (fl && fl.length >= 3 && fl.slice(0, 3).every(v => v >= 1 && Number.isInteger(v))) count = fl.slice(0, 3).map(v => Math.trunc(v));
  if (azimuth && origin === null) { warnings.push(`header azimuth ${fmtG(azimuth)} ignored: no origin / base point in header`); azimuth = 0.0; }
  let lx, ly, lz, localOrigin;
  if (azimuth) {
    const r = azimuth * Math.PI / 180, c = Math.cos(r), s = Math.sin(r);
    const ox = origin[0], oy = origin[1];
    lx = valid.map(i => (xs[i] - ox) * c - (ys[i] - oy) * s);
    ly = valid.map(i => (xs[i] - ox) * s + (ys[i] - oy) * c);
    lz = valid.map(i => zs[i] - origin[2]);
    localOrigin = [0.0, 0.0, 0.0];
  } else { lx = valid.map(i => xs[i]); ly = valid.map(i => ys[i]); lz = valid.map(i => zs[i]); localOrigin = origin; }
  if (localOrigin === null) {
    localOrigin = [Math.min(...lx) - size[0] / 2.0, Math.min(...ly) - size[1] / 2.0, Math.min(...lz) - size[2] / 2.0];
    origin = localOrigin.slice();
  }
  if (count === null) {
    count = [[lx, localOrigin[0], size[0]], [ly, localOrigin[1], size[1]], [lz, localOrigin[2], size[2]]].map(([v, o, sz]) => Math.max(1, pyRound((Math.max(...v) - o - sz / 2.0) / sz) + 1));
  }
  const bm = new GM.BlockModel({ origin, blockSize: size, count, azimuth, name: opts.name || stem(opts.file, 'blockmodel') });
  bm.provenance = tableProv('csv_blockmodel', opts.file);
  const n = bm.n;
  if (n > 50000000) throw new FormatError(`block model too large: ${n} blocks`);
  const attrCols = t.headers.filter(h => ![xc, yc, zc, dxc, dyc, dzc].includes(h));
  const typed = {}, isNum = {}, store = {};
  for (const h of attrCols) { typed[h] = t.typed(h); isNum[h] = t.isNumeric(h); store[h] = isNum[h] ? new Float64Array(n).fill(NAN) : new Array(n).fill(null); }
  const tol = size.map(sz => 1e-6 * sz);
  let offLattice = 0, outOfRange = 0, dup = 0;
  const filled = new Set();
  valid.forEach((i, k) => {
    const ijk = [];
    let ok = true;
    const axes = [[lx[k], localOrigin[0], size[0]], [ly[k], localOrigin[1], size[1]], [lz[k], localOrigin[2], size[2]]];
    for (let a = 0; a < 3; a++) {
      const [v, o, sz] = axes[a];
      const f = (v - o) / sz - 0.5;
      const idx = pyRound(f);
      if (Math.abs(f - idx) * sz > tol[a]) { ok = false; break; }
      ijk.push(idx);
    }
    if (!ok) { offLattice++; return; }
    if (ijk.some((v, a) => v < 0 || v >= count[a])) { outOfRange++; return; }
    const idx = bm.index(ijk[0], ijk[1], ijk[2]);
    if (filled.has(idx)) dup++;
    filled.add(idx);
    for (const h of attrCols) { const v = typed[h][i]; store[h][idx] = isNum[h] ? (v === null ? NAN : v) : v; }
  });
  for (const h of attrCols) bm.addAttribute(h, store[h], isNum[h] ? 'number' : 'text');
  if (offLattice) warnings.push(`${offLattice} row(s) do not land on the ${fmtG(size[0])}x${fmtG(size[1])}x${fmtG(size[2])} lattice and were skipped`);
  if (outOfRange) warnings.push(`${outOfRange} row(s) fall outside the header-defined grid and were skipped`);
  if (dup) warnings.push(`${dup} duplicate block position(s): last row wins`);
  const missing = n - filled.size;
  if (missing) warnings.push(`${missing} of ${n} blocks have no row (NaN)`);
  bm.metadata.header = pre;
  bm.metadata.block_size_source = sizeSource;
  bm.metadata.columns = { x: xc, y: yc, z: zc, dx: dxc, dy: dyc, dz: dzc };
  bm.metadata.rows = t.n;
  bm.metadata.warnings = warnings;
  return bm;
}
/** Ordered [key, value] pairs describing the lattice (embedded header / sidecar). */
export function blockmodelDefinition(bm, rows = null) {
  const items = [['name', bm.name], ['base_point', bm.origin.map(csvFmt).join(', ')], ['block_size', bm.blockSize.map(csvFmt).join(', ')],
    ['count', bm.count.join(', ')], ['azimuth', csvFmt(bm.azimuth)], ['blocks', String(bm.n)]];
  if (rows !== null) items.push(['rows', String(rows)]);
  items.push(['generator', 'nw-mineral-monitor geomodel']);
  return items;
}
/** BlockModel -> {csv, txt}: 'x,y,z,dx,dy,dz,<attrs>' centroid table (+ sidecar definition).
    opts: attributes, embeddedHeader (true), sidecar (true), skipEmpty (true). */
export function writeBlockmodelCsv(bm, opts = {}) {
  const names = opts.attributes ? Array.from(opts.attributes) : Object.keys(bm.attributes);
  const cols = names.map(nm => [nm, bm.attributes[nm].values]);
  const header = ['x', 'y', 'z', 'dx', 'dy', 'dz'].concat(names);
  const [dx, dy, dz] = bm.blockSize;
  const rows = [];
  const [nx, ny, nz] = bm.count;
  const skipEmpty = opts.skipEmpty !== false;
  for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const idx = bm.index(i, j, k);
    const vals = cols.map(c => { const v = c[1][idx]; return v === undefined ? null : v; });
    if (skipEmpty && cols.length && vals.every(v => v === null || (typeof v === 'number' && v !== v) || v === '')) continue;
    const [x, y, z] = bm.centroid(i, j, k);
    rows.push([x, y, z, dx, dy, dz].concat(vals));
  }
  const definition = blockmodelDefinition(bm, rows.length);
  let body = csvText(header, rows);
  if (opts.embeddedHeader !== false) body = concatU8([utf8(definition.map(([k, v]) => `# ${k}: ${v}\n`).join('')), body]);
  const txt = opts.sidecar !== false ? utf8(definition.map(([k, v]) => `${k}: ${v}\n`).join('')) : null;
  return { csv: body, txt };
}

/* ========================================================================
   segy — SEG-Y rev 0 / 1 / 2 seismic (GPR / resistivity) sections.
   3200-byte textual header (EBCDIC cp037 or ASCII), 400-byte binary header,
   240-byte trace headers; all byte positions below follow the standard's
   1-based numbering minus one.  Sample formats 1 IBM, 2 i32, 3 i16, 5 f32,
   6 f64, 8 i8, 9 i64, 10 u32, 11 u16, 12 u64, 16 u8.
   ======================================================================== */
const SEGY_TEXT_LEN = 3200, SEGY_BIN_LEN = 400, SEGY_TRACE_HDR = 240;
const SEGY_FORMAT_SIZE = { 1: 4, 2: 4, 3: 2, 5: 4, 6: 8, 8: 1, 9: 8, 10: 4, 11: 2, 12: 8, 16: 1 };
const SEGY_FORMAT_NAME = { 1: 'IBM float32', 2: 'int32', 3: 'int16', 4: 'fixed-point with gain (unsupported)', 5: 'IEEE float32', 6: 'IEEE float64', 7: 'int24 (unsupported)',
  8: 'int8', 9: 'int64', 10: 'uint32', 11: 'uint16', 12: 'uint64', 15: 'uint24 (unsupported)', 16: 'uint8' };
const IBM_POW16 = Array.from({ length: 128 }, (_, e) => Math.pow(16, e - 64));
const SEGY_BYTE_ORDER_BIG = 16909060, SEGY_BYTE_ORDER_LITTLE = 67305985;
/** cp037 (EBCDIC) -> Unicode, 256 entries. */
export const CP037 = '\u0000\u0001\u0002\u0003\u009c\u0009\u0086\u007f\u0097\u008d\u008e\u000b\u000c\u000d\u000e\u000f\u0010\u0011\u0012\u0013\u009d\u0085\u0008\u0087\u0018\u0019\u0092\u008f\u001c\u001d\u001e\u001f\u0080\u0081\u0082\u0083\u0084\u000a\u0017\u001b\u0088\u0089\u008a\u008b\u008c\u0005\u0006\u0007\u0090\u0091\u0016\u0093\u0094\u0095\u0096\u0004\u0098\u0099\u009a\u009b\u0014\u0015\u009e\u001a \u00a0\u00e2\u00e4\u00e0\u00e1\u00e3\u00e5\u00e7\u00f1\u00a2.<(+|&\u00e9\u00ea\u00eb\u00e8\u00ed\u00ee\u00ef\u00ec\u00df!$*);\u00ac-/\u00c2\u00c4\u00c0\u00c1\u00c3\u00c5\u00c7\u00d1\u00a6,%_>?\u00f8\u00c9\u00ca\u00cb\u00c8\u00cd\u00ce\u00cf\u00cc`:#@\'="\u00d8abcdefghi\u00ab\u00bb\u00f0\u00fd\u00fe\u00b1\u00b0jklmnopqr\u00aa\u00ba\u00e6\u00b8\u00c6\u00a4\u00b5~stuvwxyz\u00a1\u00bf\u00d0\u00dd\u00de\u00ae^\u00a3\u00a5\u00b7\u00a9\u00a7\u00b6\u00bc\u00bd\u00be[]\u00af\u00a8\u00b4\u00d7{ABCDEFGHI\u00ad\u00f4\u00f6\u00f2\u00f3\u00f5}JKLMNOPQR\u00b9\u00fb\u00fc\u00f9\u00fa\u00ff\\\u00f7STUVWXYZ\u00b2\u00d4\u00d6\u00d2\u00d3\u00d50123456789\u00b3\u00db\u00dc\u00d9\u00da\u009f';
const CP037_REVERSE = (() => { const m = new Map(); for (let i = 0; i < 256; i++) m.set(CP037.charCodeAt(i), i); return m; })();
export function decodeEbcdic(bytes) { let out = ''; for (let i = 0; i < bytes.length; i++) out += CP037[bytes[i]]; return out; }
export function encodeEbcdic(text) { const out = new Uint8Array(text.length); for (let i = 0; i < text.length; i++) { const b = CP037_REVERSE.get(text.charCodeAt(i)); out[i] = b === undefined ? 0x6f : b; } return out; }

/** IBM System/360 single precision word -> float. */
export function ibmToFloat(word) {
  word = word >>> 0;
  if (word === 0) return 0.0;
  const s = word >>> 31, e = (word >>> 24) & 0x7f, f = word & 0xffffff;
  const v = (f / 16777216.0) * IBM_POW16[e];
  return s ? -v : v;
}
function frexp(value) {
  if (value === 0 || !isFinite(value) || value !== value) return [value, 0];
  const abs = Math.abs(value);
  let exp = Math.max(-1023, Math.floor(Math.log2(abs)) + 1);
  let x = abs * Math.pow(2, -exp);
  while (x < 0.5) { x *= 2; exp--; }
  while (x >= 1) { x *= 0.5; exp++; }
  return [value < 0 ? -x : x, exp];
}
/** float -> IBM single precision word (round to nearest, overflow clamps). */
export function floatToIbm(v) {
  v = +v;
  if (v === 0.0 || v !== v) return 0;
  const s = v < 0 ? 0x80000000 : 0;
  v = Math.abs(v);
  const [m, x] = frexp(v);
  const x4 = Math.ceil(x / 4);
  const f = m * Math.pow(2.0, x - 4 * x4);
  let e = x4 + 64;
  let frac = pyRound(f * 16777216.0);
  if (frac >= 16777216) { frac = Math.floor(frac / 16); e += 1; }
  while (frac && e < 0) { frac = Math.floor(frac / 16); e += 1; }
  if (e > 127) { e = 127; frac = 0xffffff; }
  return (s | (e << 24) | (frac & 0xffffff)) >>> 0;
}
function segyDecodeText(raw) {
  let txt, kind;
  if (raw[0] === 0x43) { txt = decodeLatin1(raw).replace(/[\x80-\xff]/g, '�'); kind = 'ascii'; }
  else {
    let high = false, blank = true;
    for (let i = 0; i < raw.length; i++) { const b = raw[i]; if (b >= 0x80) high = true; if (b !== 0x40 && b !== 0x00) blank = false; }
    if (!high && !blank) { txt = decodeLatin1(raw); kind = 'ascii'; }
    else { txt = decodeEbcdic(raw); kind = 'ebcdic'; }
  }
  const lines = [];
  for (let i = 0; i < txt.length; i += 80) lines.push(pyRstrip(txt.slice(i, i + 80)));
  return [lines.join('\n'), kind];
}
function segyScaled(v, scalar) { if (scalar > 0) return v * scalar; if (scalar < 0) return v / -scalar; return v; }
function segyDecodeSamples(dv, start, fmt, ns, le) {
  const out = new Float32Array(ns);
  switch (fmt) {
    case 1: for (let k = 0; k < ns; k++) out[k] = ibmToFloat(dv.getUint32(start + 4 * k, le)); break;
    case 2: for (let k = 0; k < ns; k++) out[k] = dv.getInt32(start + 4 * k, le); break;
    case 3: for (let k = 0; k < ns; k++) out[k] = dv.getInt16(start + 2 * k, le); break;
    case 5: for (let k = 0; k < ns; k++) out[k] = dv.getFloat32(start + 4 * k, le); break;
    case 6: for (let k = 0; k < ns; k++) out[k] = dv.getFloat64(start + 8 * k, le); break;
    case 8: for (let k = 0; k < ns; k++) out[k] = dv.getInt8(start + k); break;
    case 9: for (let k = 0; k < ns; k++) out[k] = Number(dv.getBigInt64(start + 8 * k, le)); break;
    case 10: for (let k = 0; k < ns; k++) out[k] = dv.getUint32(start + 4 * k, le); break;
    case 11: for (let k = 0; k < ns; k++) out[k] = dv.getUint16(start + 2 * k, le); break;
    case 12: for (let k = 0; k < ns; k++) out[k] = Number(dv.getBigUint64(start + 8 * k, le)); break;
    case 16: for (let k = 0; k < ns; k++) out[k] = dv.getUint8(start + k); break;
    default: throw new FormatError('SEG-Y: unsupported format ' + fmt);
  }
  return out;
}
/** SEG-Y bytes -> {text_header, text_encoding, binary_header, n_traces, samples: [Float32Array...], dt (s),
    trace_headers, coords [[x, y]...], ns, format, endian, revision, warnings}.
    opts: ilineByte 189, xlineByte 193, cdpxByte 181, cdpyByte 185, spByte 197, endian 'big'|'little', maxTraces. */
export function readSegy(src, opts = {}) {
  const data = toU8(src);
  const ilineByte = opts.ilineByte || 189, xlineByte = opts.xlineByte || 193, cdpxByte = opts.cdpxByte || 181, cdpyByte = opts.cdpyByte || 185, spByte = opts.spByte || 197;
  const warnings = [];
  if (data.length < SEGY_TEXT_LEN + SEGY_BIN_LEN) throw new FormatError('SEG-Y: file shorter than the 3600-byte file header');
  const [text, textKind] = segyDecodeText(data.subarray(0, SEGY_TEXT_LEN));
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const B = SEGY_TEXT_LEN;
  let le;
  if (opts.endian === 'big' || opts.endian === '>') le = false;
  else if (opts.endian === 'little' || opts.endian === '<') le = true;
  else {
    const word = dv.getUint32(B + 96, false);
    if (word === SEGY_BYTE_ORDER_BIG) le = false;
    else if (word === SEGY_BYTE_ORDER_LITTLE) le = true;
    else {
      const fmtBig = dv.getInt16(B + 24, false), fmtLittle = dv.getInt16(B + 24, true);
      if (fmtBig in SEGY_FORMAT_SIZE) le = false;
      else if (fmtLittle in SEGY_FORMAT_SIZE) { le = true; warnings.push('format code only valid as little-endian: reading the file little-endian'); }
      else { le = false; warnings.push(`format code ${fmtBig} is not valid in either byte order`); }
    }
  }
  const i16 = o => dv.getInt16(o, le), u16 = o => dv.getUint16(o, le), i32 = o => dv.getInt32(o, le), u64 = o => Number(dv.getBigUint64(o, le));
  const fmt = i16(B + 24), nsHdr = u16(B + 20);
  let dtUs = u16(B + 16);
  const revRaw = u16(B + 300);
  const revision = revRaw ? (revRaw >> 8) + (revRaw & 0xff) / 100.0 : 0.0;
  const binaryHeader = {
    job_id: i32(B), line_number: i32(B + 4), reel_number: i32(B + 8), traces_per_ensemble: i16(B + 12), aux_traces_per_ensemble: i16(B + 14),
    sample_interval_us: dtUs, sample_interval_orig_us: u16(B + 18), samples_per_trace: nsHdr, samples_per_trace_orig: u16(B + 22),
    format_code: fmt, format: SEGY_FORMAT_NAME[fmt] || `unknown (${fmt})`, ensemble_fold: i16(B + 26), trace_sorting: i16(B + 28), measurement_system: i16(B + 54),
    revision, revision_raw: revRaw, fixed_length: i16(B + 302), n_ext_text: i16(B + 304),
  };
  if (revRaw >= 0x0200) { binaryHeader.max_extra_trace_headers = i32(B + 306); binaryHeader.n_traces_in_file = u64(B + 312); binaryHeader.first_trace_offset = u64(B + 320); }
  if (!(fmt in SEGY_FORMAT_SIZE)) throw new FormatError(`SEG-Y: unsupported data sample format code ${fmt} (${SEGY_FORMAT_NAME[fmt] || '?'})`);
  const size = SEGY_FORMAT_SIZE[fmt];
  let off = SEGY_TEXT_LEN + SEGY_BIN_LEN;
  const nExt = binaryHeader.n_ext_text;
  if (nExt > 0) { off += nExt * SEGY_TEXT_LEN; warnings.push(`${nExt} extended textual header(s) skipped`); }
  else if (nExt < 0) {
    const endA = encodeAscii('((SEG: EndText))'), endE = encodeEbcdic('((SEG: EndText))');
    let count = 0;
    while (off + SEGY_TEXT_LEN <= data.length) {
      const blk = data.subarray(off, off + SEGY_TEXT_LEN);
      off += SEGY_TEXT_LEN; count++;
      if (findBytes(blk, endA) >= 0 || findBytes(blk, endE) >= 0) break;
    }
    warnings.push(`${count} extended textual header(s) (variable count) skipped`);
  }
  const firstOff = binaryHeader.first_trace_offset || 0;
  if (firstOff && firstOff > off && firstOff < data.length) off = firstOff;
  if (binaryHeader.fixed_length === 0 && revision >= 1) warnings.push('fixed-length trace flag is 0: per-trace sample counts honoured');
  const samples = [], headers = [], coords = [];
  let nBad = 0, ntr = 0;
  const extraHdrs = binaryHeader.max_extra_trace_headers || 0;
  const maxTraces = opts.maxTraces === undefined ? null : opts.maxTraces;
  while (off + SEGY_TRACE_HDR <= data.length) {
    if (maxTraces !== null && ntr >= maxTraces) break;
    const h = off;
    let ns = u16(h + 114);
    if (binaryHeader.fixed_length === 1 && nsHdr) { if (ns !== nsHdr && ns) nBad++; ns = nsHdr; }
    else if (ns === 0) ns = nsHdr;
    const tdt = u16(h + 116);
    let hdrBytes = SEGY_TRACE_HDR;
    if (extraHdrs && revRaw >= 0x0200) hdrBytes += SEGY_TRACE_HDR * Math.max(0, i16(h + 156));
    const start = off + hdrBytes, end = start + ns * size;
    if (end > data.length) { warnings.push(`trace ${ntr + 1} truncated (needs ${ns * size} bytes, ${data.length - start} left): stopped`); break; }
    const sc = i16(h + 70), se = i16(h + 68);
    const th = {
      seq: i32(h), seq_file: i32(h + 4), ffid: i32(h + 8), trace_in_ffid: i32(h + 12), energy_source_point: i32(h + 16), cdp: i32(h + 20), trace_in_cdp: i32(h + 24),
      trace_id: i16(h + 28), offset: i32(h + 36), rec_elev: segyScaled(i32(h + 40), se), src_elev: segyScaled(i32(h + 44), se), src_depth: segyScaled(i32(h + 48), se),
      scalar_elev: se, scalar_coord: sc, sx: segyScaled(i32(h + 72), sc), sy: segyScaled(i32(h + 76), sc), gx: segyScaled(i32(h + 80), sc), gy: segyScaled(i32(h + 84), sc),
      coord_units: i16(h + 88), delay_ms: i16(h + 108), ns, dt_us: tdt,
      cdpx: segyScaled(i32(h + cdpxByte - 1), sc), cdpy: segyScaled(i32(h + cdpyByte - 1), sc), inline: i32(h + ilineByte - 1), xline: i32(h + xlineByte - 1), sp: i32(h + spByte - 1),
    };
    headers.push(th);
    samples.push(segyDecodeSamples(dv, start, fmt, ns, le));
    coords.push(th.cdpx || th.cdpy ? [th.cdpx, th.cdpy] : [th.sx, th.sy]);
    off = end;
    ntr++;
  }
  if (nBad) warnings.push(`${nBad} trace header(s) disagree with the binary-header sample count (binary header used)`);
  if (off < data.length && !(maxTraces !== null && ntr >= maxTraces)) warnings.push(`${data.length - off} trailing byte(s) after the last trace ignored`);
  if (!samples.length) warnings.push('no traces');
  if (dtUs === 0 && headers.length) { dtUs = headers[0].dt_us; warnings.push(`binary-header sample interval is 0: using the first trace header (${dtUs} us)`); }
  if (dtUs === 0) warnings.push('sample interval unknown (0)');
  const nsAll = samples.length ? Array.from(new Set(samples.map(s => s.length))).sort((a, b) => a - b) : [nsHdr];
  if (nsAll.length > 1) warnings.push(`variable trace lengths: ${nsAll.slice(0, 6).join(', ')} samples`);
  if (coords.every(c => c[0] === 0 && c[1] === 0)) warnings.push('no trace coordinates (CDP X/Y and source X/Y all zero)');
  return { text_header: text, text_encoding: textKind, binary_header: binaryHeader, n_traces: samples.length, samples, dt: dtUs * 1e-6, trace_headers: headers, coords,
    ns: nsAll.length ? Math.max(...nsAll) : 0, format: fmt, endian: le ? 'little' : 'big', revision, warnings, file: opts.file || null };
}
/** Amplitude section -> {width, height, gray: Uint8Array (row 0 = time 0), p1, p2, z_top, z_bottom, clip, dt, warnings}.
    opts: zTop, zBottom (default 0 / -(ns-1)*dt*1000, i.e. TWT ms as pseudo-depth), clipPct (98). */
export function segySectionImage(d, opts = {}) {
  const samples = d.samples;
  const width = samples.length;
  let height = 0;
  for (const s of samples) if (s.length > height) height = s.length;
  const warnings = [];
  const dt = d.dt || 0.0;
  let zTop = opts.zTop === undefined || opts.zTop === null ? null : +opts.zTop;
  let zBottom = opts.zBottom === undefined || opts.zBottom === null ? null : +opts.zBottom;
  if (zTop === null) zTop = 0.0;
  if (zBottom === null) { zBottom = -Math.max(height - 1, 0) * dt * 1000.0; warnings.push('no depth conversion: z is two-way time in ms (negative down) as pseudo-depth'); }
  const mags = [];
  let total = 0;
  for (const s of samples) total += s.length;
  const step = Math.max(1, Math.floor(total / 200000));
  let k = 0;
  for (const s of samples) for (let i = 0; i < s.length; i++, k++) { const v = s[i]; if (k % step === 0 && v === v) mags.push(Math.abs(v)); }
  mags.sort((a, b) => a - b);
  let clip = 0.0;
  if (mags.length) {
    const pct = Math.min(Math.max(+(opts.clipPct === undefined ? 98.0 : opts.clipPct), 0.0), 100.0);
    const idx = Math.min(mags.length - 1, pyRound(pct / 100.0 * (mags.length - 1)));
    clip = mags[idx];
  }
  if (clip <= 0) {
    const mx = mags.length ? mags[mags.length - 1] : 0;
    clip = mx > 0 ? mx : 1.0;
    if (!mags.some(m => m)) warnings.push('all amplitudes are zero');
  }
  const gray = new Uint8Array(width * height).fill(0x80);
  const scale = 127.0 / clip;
  samples.forEach((s, col) => {
    for (let row = 0; row < s.length; row++) {
      const v = s[row];
      if (v !== v) continue;
      const g = 128 + Math.trunc(v * scale);
      gray[row * width + col] = g < 0 ? 0 : (g > 255 ? 255 : g);
    }
  });
  const coords = d.coords || [];
  const p1 = coords.length ? coords[0].slice() : null, p2 = coords.length ? coords[coords.length - 1].slice() : null;
  if (p1 !== null && p1[0] === p2[0] && p1[1] === p2[1] && width > 1) warnings.push('first and last trace share the same coordinates');
  return { width, height, gray, p1, p2, z_top: zTop, z_bottom: zBottom, clip, dt, warnings };
}
function segyTextHeader(text, encoding) {
  const lines = text ? pySplitlines(String(text)).map(pyRstrip) : [];
  let blob = '';
  for (let k = 0; k < 40; k++) {
    let body = k < lines.length ? lines[k] : '';
    if (!body.startsWith('C')) body = pyRstrip(`C${padLeft(k + 1, 2)} ${body}`);
    blob += padRight(body.slice(0, 80), 80);
  }
  return encoding === 'ebcdic' ? encodeEbcdic(blob) : encodeAscii(blob);
}
/** Traces (array of equal-length sample arrays) -> minimal SEG-Y rev 1.
    opts: dt_us (required), coords [[x, y]...], format_code 5 (1 2 3 5 8), text, inlines, xlines, delay_ms 0, endian 'big', text_encoding 'ascii'|'ebcdic'. */
export function writeSegy(samples, opts = {}) {
  const le = (opts.endian || 'big') !== 'big';
  const textEncoding = opts.text_encoding || opts.textEncoding || 'ascii';
  if (textEncoding !== 'ascii' && textEncoding !== 'ebcdic') throw new FormatError("text_encoding must be 'ascii' or 'ebcdic'");
  const traces = Array.from(samples, t => Array.from(t));
  const ntr = traces.length;
  const ns = ntr ? traces[0].length : 0;
  if (traces.some(t => t.length !== ns)) throw new FormatError('write_segy: all traces must have the same number of samples');
  const formatCode = opts.format_code === undefined ? (opts.formatCode === undefined ? 5 : opts.formatCode) : opts.format_code;
  if (![1, 2, 3, 5, 8].includes(formatCode)) throw new FormatError('write_segy: format_code must be 1, 2, 3, 5 or 8');
  if (ns > 65535) throw new FormatError('write_segy: more than 65535 samples per trace');
  const rawDt = opts.dt_us === undefined ? opts.dtUs : opts.dt_us;
  if (rawDt === undefined || rawDt === null) throw new FormatError('write_segy: dt_us is required');
  const dtUs = pyRound(+rawDt);
  let head = opts.text;
  if (head === undefined || head === null) head = `nw-mineral-monitor geomodel SEG-Y rev 1 export\nTRACES ${ntr}  SAMPLES ${ns}  SAMPLE INTERVAL ${dtUs} US\nBYTES 181-184 CDP X, 185-188 CDP Y (SCALAR 1), 189-192 INLINE, 193-196 CROSSLINE\nBYTES 197-200 SHOTPOINT, 73-76 SOURCE X, 77-80 SOURCE Y\nDATA FORMAT ${formatCode}\nEND TEXTUAL HEADER`;
  const sizes = { 1: 4, 2: 4, 3: 2, 5: 4, 8: 1 };
  const w = new ByteWriter(SEGY_TEXT_LEN + SEGY_BIN_LEN + ntr * (SEGY_TRACE_HDR + ns * sizes[formatCode]) + 16);
  w.bytes(segyTextHeader(head, textEncoding));
  const b0 = w.length;
  w.zeros(SEGY_BIN_LEN);
  const dv = w.dv;
  dv.setInt32(b0, 1, le); dv.setInt32(b0 + 4, 1, le); dv.setInt32(b0 + 8, 1, le); dv.setInt16(b0 + 12, 1, le);
  dv.setUint16(b0 + 16, dtUs, le); dv.setUint16(b0 + 18, dtUs, le); dv.setUint16(b0 + 20, ns, le); dv.setUint16(b0 + 22, ns, le);
  dv.setInt16(b0 + 24, formatCode, le); dv.setInt16(b0 + 26, 1, le); dv.setInt16(b0 + 28, 4, le); dv.setInt16(b0 + 54, 1, le);
  dv.setUint16(b0 + 300, 0x0100, le); dv.setInt16(b0 + 302, 1, le); dv.setInt16(b0 + 304, 0, le);
  const coords = opts.coords || null, inlines = opts.inlines || null, xlines = opts.xlines || null;
  const delayMs = Math.trunc(opts.delay_ms === undefined ? (opts.delayMs || 0) : opts.delay_ms);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, pyRound(v)));
  traces.forEach((t, k) => {
    const h0 = w.length;
    w.zeros(SEGY_TRACE_HDR);
    const hv = w.dv;
    const [x, y] = coords !== null && k < coords.length ? coords[k] : [0.0, 0.0];
    const xi = pyRound(x), yi = pyRound(y);
    const il = inlines !== null ? Math.trunc(inlines[k]) : 1;
    const xl = xlines !== null ? Math.trunc(xlines[k]) : k + 1;
    hv.setInt32(h0, k + 1, le); hv.setInt32(h0 + 4, k + 1, le); hv.setInt32(h0 + 8, k + 1, le); hv.setInt32(h0 + 12, 1, le); hv.setInt32(h0 + 16, k + 1, le);
    hv.setInt32(h0 + 20, k + 1, le); hv.setInt32(h0 + 24, 1, le); hv.setInt16(h0 + 28, 1, le); hv.setInt16(h0 + 68, 1, le); hv.setInt16(h0 + 70, 1, le);
    hv.setInt32(h0 + 72, xi, le); hv.setInt32(h0 + 76, yi, le); hv.setInt32(h0 + 80, xi, le); hv.setInt32(h0 + 84, yi, le); hv.setInt16(h0 + 88, 1, le);
    hv.setInt16(h0 + 108, delayMs, le); hv.setUint16(h0 + 114, ns, le); hv.setUint16(h0 + 116, dtUs, le);
    hv.setInt32(h0 + 180, xi, le); hv.setInt32(h0 + 184, yi, le); hv.setInt32(h0 + 188, il, le); hv.setInt32(h0 + 192, xl, le); hv.setInt32(h0 + 196, k + 1, le); hv.setInt16(h0 + 200, 1, le);
    if (formatCode === 1) for (const v of t) w.u32(floatToIbm(v), le);
    else if (formatCode === 5) for (const v of t) w.f32(+v, le);
    else if (formatCode === 2) for (const v of t) w.i32(clamp(v, -2147483648, 2147483647), le);
    else if (formatCode === 3) for (const v of t) w.i16(clamp(v, -32768, 32767), le);
    else for (const v of t) w.i8(clamp(v, -128, 127));
  });
  return w.result();
}

/* ========================================================================
   las — CWLS LAS 1.2 / 2.0 (3.0 best effort) well logs
   ======================================================================== */
export const LAS_DEFAULT_NULL = -999.25;
/** 'MNEM.UNIT  VALUE : DESCR' -> [mnem, unit, value, descr] (value ends at the LAST colon). */
export function parseLasHeaderLine(line) {
  const s = pyStrip(line);
  const firstColon = s.indexOf(':');
  const beforeColon = firstColon < 0 ? s : s.slice(0, firstColon);
  if (beforeColon.indexOf('.') < 0) {
    if (firstColon >= 0) return [pyStrip(s.slice(0, firstColon)), '', pyStrip(s.slice(firstColon + 1)), ''];
    return [s, '', '', ''];
  }
  const dot = s.indexOf('.');
  const name = pyStrip(s.slice(0, dot));
  let rest = s.slice(dot + 1);
  const m = /^([^\s:]*)([\s\S]*)$/.exec(rest);
  const unit = m[1];
  rest = m[2];
  let value, descr;
  if (rest.indexOf(':') >= 0) { const k = rest.lastIndexOf(':'); value = rest.slice(0, k); descr = rest.slice(k + 1); }
  else { value = rest; descr = ''; }
  return [name, pyStrip(unit), pyStrip(value), pyStrip(descr)];
}
function lasSectionKind(title) {
  let t = pyStrip(title.replace(/^~+/, '')).toLowerCase();
  if (t.startsWith('log ')) t = t.replace(' ', '_');
  const m = /^[a-z_]+/.exec(t);
  const word = m ? m[0] : '';
  if (!word) return 'unknown';
  if (word === 'v' || word.startsWith('version')) return 'version';
  if (word === 'w' || word.startsWith('well')) return 'well';
  if (word === 'c' || word.startsWith('curve') || word.startsWith('log_definition')) return 'curve';
  if (word === 'p' || word.startsWith('param') || word.startsWith('log_parameter')) return 'param';
  if (word === 'o' || word.startsWith('other')) return 'other';
  if (word === 'a' || word.startsWith('ascii') || word.startsWith('log_data')) return 'data';
  if (word.startsWith('log')) return 'log_other';
  return 'unknown';
}
function lasNum(tok, nul) {
  const v = pyFloat(tok);
  if (v === undefined) return NAN;
  if (nul !== null && Math.abs(v - nul) <= 1e-9 * Math.max(1.0, Math.abs(nul))) return NAN;
  return v;
}
function lasSplit(line, delimiter) {
  if (delimiter === ',') return line.split(',').map(pyStrip);
  if (delimiter === '\t') return line.split('\t').map(pyStrip);
  return pySplit(line);
}
/** LAS text / bytes -> {version, wrap, delimiter, well, curves, params, other, data: {mnem: Float64Array}, index_unit, null, n_rows, sections, warnings}. */
export function readLas(src, opts = {}) {
  const text = typeof src === 'string' ? src : decodeText(toU8(src), true);
  const warnings = [];
  let version = null, wrap = false, delimiter = ' ';
  const well = {}, params = {}, other = [], dataLines = [], seenSections = [];
  let curves = [];
  let section = null, dataSections = 0;
  for (const raw of pySplitlines(text)) {
    const line = raw.replace(/[\r\n]+$/, '');
    const s = pyStrip(line);
    if (!s) continue;
    if (s.startsWith('~')) {
      const kind = lasSectionKind(s);
      seenSections.push(pySplit(s)[0]);
      if (kind === 'data') {
        dataSections++;
        if (dataSections > 1) { warnings.push(`${dataSections} data sections: only the first was read`); section = 'skip'; continue; }
      } else if (kind === 'curve' && curves.length) { warnings.push(`additional curve-definition section ${pyReprStr(s)} ignored`); section = 'skip'; continue; }
      else if (kind === 'unknown' || kind === 'log_other') { warnings.push(`section ${pyReprStr(pySplit(s)[0])} not understood; skipped`); section = 'skip'; continue; }
      section = kind;
      continue;
    }
    if (section === 'data') { if (s.startsWith('#')) continue; dataLines.push(s); continue; }
    if (s.startsWith('#')) continue;
    if (section === 'skip' || section === null) continue;
    if (section === 'other') { other.push(pyRstrip(line)); continue; }
    const [name, unit, value, descr] = parseLasHeaderLine(s);
    if (section === 'version') {
      const u = name.toUpperCase();
      if (u === 'VERS') { const v = pyFloat(value); if (v === undefined) { version = null; warnings.push(`unparsable VERS ${pyReprStr(value)}`); } else version = v; }
      else if (u === 'WRAP') wrap = pyStrip(value).toUpperCase().startsWith('Y');
      else if (u === 'DLM') {
        const dvv = pyStrip(value).toUpperCase();
        delimiter = { COMMA: ',', TAB: '\t', SPACE: ' ' }[dvv] || ' ';
        if (!['COMMA', 'TAB', 'SPACE', ''].includes(dvv)) warnings.push(`unknown DLM ${pyReprStr(value)}: whitespace assumed`);
      } else (params._version = params._version || {})[name] = { unit, value, descr };
    } else if (section === 'well') well[name.toUpperCase()] = { unit, value, descr };
    else if (section === 'curve') curves.push({ mnem: name, unit, descr, value });
    else if (section === 'param') params[name] = { unit, value, descr };
  }
  if (version === null) warnings.push('no ~Version section / VERS line');
  if (version !== null && version >= 3) warnings.push('LAS 3.0 read best-effort (first ~Log_Data set, DLM honoured)');
  if (wrap && version !== null && version >= 3) warnings.push('WRAP YES is not legal in LAS 3.0; honoured anyway');
  if (!curves.length) warnings.push('no ~Curve section: data columns unnamed');
  let nul = LAS_DEFAULT_NULL;
  const nv = well.NULL ? well.NULL.value : null;
  if (nv !== null && nv !== undefined && nv !== '') { const v = pyFloat(nv); if (v === undefined) warnings.push(`unparsable NULL ${pyReprStr(nv)}: ${fmtG(LAS_DEFAULT_NULL)} assumed`); else nul = v; }
  let names = [];
  const counts = {};
  for (const c of curves) {
    const m = c.mnem;
    if (m in counts) {
      counts[m]++;
      const m2 = `${m}:${counts[m]}`;
      if (counts[m] === 2) { const first = names.indexOf(m); names[first] = `${m}:1`; }
      names.push(m2);
    } else { counts[m] = 1; names.push(m); }
  }
  if (Object.values(counts).some(c => c > 1)) warnings.push('duplicate curve mnemonics renamed with :1, :2 suffixes');
  let ncur = names.length;
  const records = [];
  if (wrap && ncur) {
    let buf = [];
    for (const ln of dataLines) {
      for (const t of lasSplit(ln, delimiter)) buf.push(t);
      while (buf.length >= ncur) { records.push(buf.slice(0, ncur)); buf = buf.slice(ncur); }
    }
    if (buf.length) warnings.push(`${buf.length} leftover value(s) at the end of the wrapped data section`);
  } else {
    let short = 0, long = 0;
    for (const ln of dataLines) {
      let toks = lasSplit(ln, delimiter);
      if (ncur === 0) { ncur = toks.length; names = toks.map((_, k) => `COL${k + 1}`); curves = names.map(n => ({ mnem: n, unit: '', descr: '' })); }
      if (toks.length < ncur) { short++; toks = toks.concat(new Array(ncur - toks.length).fill('')); }
      else if (toks.length > ncur) { long++; toks = toks.slice(0, ncur); }
      records.push(toks);
    }
    if (short) warnings.push(`${short} data line(s) with fewer values than curves (padded with NULL)`);
    if (long) warnings.push(`${long} data line(s) with more values than curves (truncated)`);
  }
  const data = {};
  names.forEach((n, k) => { const col = new Float64Array(records.length); for (let r = 0; r < records.length; r++) { const tok = records[r][k]; col[r] = tok !== '' ? lasNum(tok, nul) : NAN; } data[n] = col; });
  if (!records.length) warnings.push('no data rows');
  const indexUnit = curves.length ? curves[0].unit : '';
  return { version, wrap, delimiter, well, curves: names.map((n, k) => ({ mnem: n, unit: curves[k].unit || '', descr: curves[k].descr || '' })), params, other: other.join('\n'), data,
    index_unit: indexUnit, null: nul, n_rows: records.length, sections: seenSections, warnings, file: opts.file || null };
}
/** Log samples -> Drillholes interval rows [{hole, from, to, <curve>: value|null}]; step defaults to ~Well STEP or the median increment. */
export function lasToIntervals(d, hole, step = null, curves = null) {
  const names = d.curves.map(c => c.mnem);
  if (!names.length) return [];
  const index = d.data[names[0]];
  const n = index.length;
  if (step === null || step === undefined) {
    const sv = d.well.STEP ? d.well.STEP.value : null;
    const f = sv !== null && sv !== undefined && sv !== '' ? pyFloat(sv) : 0.0;
    step = f === undefined ? 0.0 : Math.abs(f);
    if (!step) {
      const diffs = [];
      for (let k = 0; k < n - 1; k++) if (index[k] === index[k] && index[k + 1] === index[k + 1]) diffs.push(Math.abs(index[k + 1] - index[k]));
      diffs.sort((a, b) => a - b);
      step = diffs.length ? diffs[Math.floor(diffs.length / 2)] : 1.0;
    }
  }
  step = Math.abs(+step);
  const wanted = names.slice(1).filter(c => curves === null || curves === undefined || curves.includes(c));
  const rows = [];
  for (let k = 0; k < n; k++) {
    const depth = index[k];
    if (depth !== depth) continue;
    const r = { hole, from: depth - step / 2.0, to: depth + step / 2.0 };
    for (const c of wanted) { const v = d.data[c][k]; r[c] = v !== v ? null : v; }
    rows.push(r);
  }
  return rows;
}
function lasFmt(v, width = 0) {
  let r;
  if (v === null || v === undefined || (typeof v === 'number' && v !== v)) r = '';
  else if (typeof v === 'number') { r = fmtG(v, 10); if (r === '' || r === '-' || r === '-0') r = '0'; }
  else r = String(v);
  return width ? padLeft(r, width) : r;
}
/** Minimal LAS 2.0 (WRAP NO) writer for a dict shaped like readLas output (needs curves + data). */
export function writeLas(d) {
  const curves = d.curves || [];
  const data = d.data || {};
  if (!curves.length) throw new FormatError('write_las: no curves');
  const names = curves.map(c => c.mnem);
  for (const n of names) if (!(n in data)) throw new FormatError(`write_las: no data for curve ${pyReprStr(n)}`);
  const n = data[names[0]].length;
  let nul = d.null === undefined || d.null === null ? LAS_DEFAULT_NULL : pyFloat(d.null);
  if (nul === undefined || nul !== nul) nul = LAS_DEFAULT_NULL;
  const index = data[names[0]];
  const well = Object.assign({}, d.well || {});
  const idxUnit = curves[0].unit || (well.STRT && well.STRT.unit) || '';
  const strt = n ? index[0] : 0.0, stop = n ? index[n - 1] : 0.0;
  let step = n > 1 ? index[1] - index[0] : 0.0;
  if (n > 2) {
    let mn = Infinity, mx = -Infinity;
    for (let k = 0; k < n - 1; k++) { const dd = index[k + 1] - index[k]; if (dd < mn) mn = dd; if (dd > mx) mx = dd; }
    if (mx - mn > 1e-6 * Math.max(1.0, Math.abs(step))) step = 0.0;
  }
  const L = [];
  const w = s => L.push(s);
  const item = (mnem, unit, value, descr) => w(` ${padRight(mnem, 5)}.${padRight(unit || '', 7)} ${padLeft(lasFmt(value), 22)} : ${descr || ''}`);
  w('~Version Information');
  w(' VERS.                 2.0 : CWLS LOG ASCII STANDARD - VERSION 2.0');
  w(' WRAP.                  NO : ONE LINE PER DEPTH STEP');
  w('~Well Information');
  w('#MNEM.UNIT       DATA                    DESCRIPTION');
  w('#---- -----      ----------------------  ---------------------------');
  item('STRT', idxUnit, strt, 'START DEPTH'); item('STOP', idxUnit, stop, 'STOP DEPTH'); item('STEP', idxUnit, step, 'STEP'); item('NULL', '', nul, 'NULL VALUE');
  const std = ['COMP', 'WELL', 'FLD', 'LOC', 'PROV', 'CNTY', 'STAT', 'CTRY', 'SRVC', 'DATE', 'UWI', 'API'];
  const dflt = { COMP: 'COMPANY', WELL: 'WELL', FLD: 'FIELD', LOC: 'LOCATION', SRVC: 'SERVICE COMPANY', DATE: 'LOG DATE', UWI: 'UNIQUE WELL ID' };
  for (const mnem of std) {
    if (mnem in well) item(mnem, well[mnem].unit || '', well[mnem].value === undefined ? '' : well[mnem].value, well[mnem].descr || '');
    else if (mnem in dflt) item(mnem, '', '', dflt[mnem]);
  }
  for (const [mnem, it] of Object.entries(well)) { if (['STRT', 'STOP', 'STEP', 'NULL'].includes(mnem) || std.includes(mnem)) continue; item(mnem, it.unit || '', it.value === undefined ? '' : it.value, it.descr || ''); }
  w('~Curve Information');
  w('#MNEM.UNIT       API CODE                DESCRIPTION');
  w('#---- -----      ----------------------  ---------------------------');
  for (const c of curves) item(c.mnem, c.unit || '', c.value === undefined ? '' : c.value, c.descr || '');
  const params = Object.entries(d.params || {}).filter(([k]) => !k.startsWith('_'));
  if (params.length) { w('~Parameter Information'); for (const [mnem, it] of params) item(mnem, it.unit || '', it.value === undefined ? '' : it.value, it.descr || ''); }
  if (d.other) { w('~Other'); for (const ln of pySplitlines(String(d.other))) w(ln); }
  w('~ASCII Log Data');
  const cols = names.map(nm => data[nm]);
  for (let k = 0; k < n; k++) {
    const cells = cols.map(col => { let v = k < col.length ? col[k] : NAN; if (v === null || v === undefined || v !== v) v = nul; return lasFmt(+v, 12); });
    w(cells.join(' '));
  }
  return encodeAscii(L.join('\n') + '\n');
}

/* ========================================================================
   omf — shared helpers for Open Mining Format v0.9 (omf1) and v2.0 (omf2):
   element <-> model object mapping, attribute "records" and the metadata
   hints (vector_attributes, categories, boolean/color/datetime_attributes,
   attribute_units, attribute_descriptions, colormaps) that make a read /
   write cycle faithful.
   ======================================================================== */
export class OmfError extends FormatError {}
const LOC_VERTICES = 'vertices', LOC_SEGMENTS = 'segments', LOC_FACES = 'faces', LOC_CELLS = 'cells';
const MARK_BOOLEAN = '[boolean]', MARK_DATETIME = '[datetime]';

function objectsOf(projectOrObjects) {
  if (projectOrObjects instanceof GM.Project) return [projectOrObjects, projectOrObjects.objects.slice()];
  if (projectOrObjects && projectOrObjects.kind) return [null, [projectOrObjects]];
  return [null, Array.from(projectOrObjects || [])];
}
function uniqueNames(names, fallback) {
  const out = [], used = new Set();
  names.forEach((n, k) => {
    n = pyStrip(n || '') || `${fallback}-${k + 1}`;
    let cand = n, c = 1;
    while (used.has(cand)) { c++; cand = `${n} (${c})`; }
    used.add(cand); out.push(cand);
  });
  return out;
}
function isUniform(sizes, tol = 1e-9) {
  sizes = Array.from(sizes, Number);
  if (!sizes.length) return [false, 0.0];
  const s0 = sizes[0], scale = Math.max(Math.abs(s0), 1e-12);
  return [sizes.every(s => Math.abs(s - s0) <= tol * scale), s0];
}
function pyRoundN(x, n) { return parseFloat(pyFixed(x, n)); }
function radians(d) { return d * (Math.PI / 180); }
function degrees(r) { return r * (180 / Math.PI); }
const pad2 = n => String(n).padStart(2, '0');
export function isoFromEpochMicros(us) {
  if (us === null || us === undefined) return null;
  us = Math.trunc(Number(us));
  const secs = Math.floor(us / 1000000), frac = us - secs * 1000000;
  const d = new Date(secs * 1000);
  let base = `${String(d.getUTCFullYear()).padStart(4, '0')}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}T${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
  if (frac) base += ('.' + String(frac).padStart(6, '0')).replace(/0+$/, '');
  return base + 'Z';
}
export function isoFromEpochDays(days) {
  if (days === null || days === undefined) return null;
  const d = new Date(Math.trunc(Number(days)) * 86400000);
  return `${String(d.getUTCFullYear()).padStart(4, '0')}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
}
/** ISO-8601 date / date-time ('Z' or +hh:mm offset) -> microseconds since the epoch, or null. */
export function epochMicrosFromIso(s) {
  if (s === null || s === undefined) return null;
  let txt = pyStrip(String(s));
  if (!txt) return null;
  txt = txt.replace('T', ' ');
  let offset = 0;
  if (txt.endsWith('Z')) txt = txt.slice(0, -1);
  else if (txt.length > 6 && (txt[txt.length - 6] === '+' || txt[txt.length - 6] === '-') && txt[txt.length - 3] === ':') {
    const sign = txt[txt.length - 6] === '+' ? 1 : -1;
    offset = sign * (parseInt(txt.slice(-5, -3), 10) * 3600 + parseInt(txt.slice(-2), 10) * 60);
    txt = txt.slice(0, -6);
  }
  let frac = 0;
  if (txt.indexOf('.') >= 0) { const k = txt.indexOf('.'); const f = txt.slice(k + 1); txt = txt.slice(0, k); if (!/^\d*$/.test(f)) return null; frac = parseInt((f + '000000').slice(0, 6), 10); }
  const m = /^(\d{1,4})-(\d{1,2})-(\d{1,2})(?: (\d{1,2}):(\d{1,2}):(\d{1,2}))?$/.exec(txt);
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3], H = m[4] === undefined ? 0 : +m[4], M = m[5] === undefined ? 0 : +m[5], S = m[6] === undefined ? 0 : +m[6];
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || H > 23 || M > 59 || S > 61) return null;
  const ms = Date.UTC(y, mo - 1, d, H, M, S);
  if (ms !== ms) return null;
  return (Math.round(ms / 1000) - offset) * 1000000 + frac;
}
function numOf(v) {
  if (v === null || v === undefined || v === '') return NAN;
  if (typeof v === 'boolean') return v ? 1.0 : 0.0;
  if (typeof v === 'number') return v;
  if (typeof v === 'string') { const f = pyFloat(v); return f === undefined ? NAN : f; }
  return NAN;
}
function allNumeric(col) {
  let seen = false;
  for (const v of col) {
    if (v === null || v === undefined || v === '') continue;
    if (typeof v === 'boolean') return false;
    if (typeof v === 'number') { seen = true; continue; }
    if (typeof v === 'string' && pyFloat(v) !== undefined) { seen = true; continue; }
    return false;
  }
  return seen;
}
function isBoolColumn(col) {
  let seen = false;
  for (const v of col) { if (v === null || v === undefined) continue; if (typeof v !== 'boolean') return false; seen = true; }
  return seen;
}
/** Any colour spec -> [r, g, b, a] ints or null. */
export function color4(c) {
  if (c === null || c === undefined) return null;
  if (typeof c === 'string') {
    const s = c.replace(/^#+/, '');
    if (s.length === 6 || s.length === 8) {
      const vals = [];
      for (let k = 0; k < s.length; k += 2) { const v = parseInt(s.slice(k, k + 2), 16); if (v !== v) return null; vals.push(v); }
      while (vals.length < 4) vals.push(255);
      return vals;
    }
    return null;
  }
  if (typeof c !== 'object' || typeof c.length !== 'number') return null;
  const vals = [];
  for (const v of Array.from(c)) { const f = typeof v === 'number' ? v : pyFloat(v); if (f === undefined || f !== f) return null; vals.push(Math.max(0, Math.min(255, pyRound(f)))); }
  if (vals.length === 3) vals.push(255);
  return vals.length >= 4 ? vals.slice(0, 4) : null;
}
function vecNorm(v) { return Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]); }
function vecCross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
function vecDot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

/* ------------------------------------------------------------ axis handling */
function horizontalRotation(u, v, warn, label) {
  if (Math.abs(u[2]) > 1e-6 || Math.abs(v[2]) > 1e-6) { warn(`${label}: grid axes are not horizontal; converted to a triangulated mesh`); return null; }
  const rot = pyRoundN(degrees(Math.atan2(u[1], u[0])), 9) + 0.0;
  const perp = [-u[1], u[0], 0.0];
  const d = vecDot(perp, v);
  if (Math.abs(Math.abs(d) - 1.0) > 1e-4) { warn(`${label}: grid axes are not orthogonal unit vectors (|u.v_perp| = ${Math.abs(d).toFixed(4)}); mesh conversion used`); return null; }
  return [rot === 0 ? 0 : rot, d < 0];
}
function rotationAxes(rotation) { const r = radians(rotation), c = Math.cos(r), s = Math.sin(r); return [[c, s, 0.0], [-s, c, 0.0]]; }
function azimuthAxes(azimuth) { const r = radians(azimuth), c = Math.cos(r), s = Math.sin(r); return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]; }

/** OMF grid surface -> [Grid2D, true, null] when regular + horizontal, else [Mesh, false, triCells]. */
function gridFromTensorSurface(name, origin, u, v, tensorU, tensorV, heights, warn, kw = {}) {
  const nu = tensorU.length, nv = tensorV.length;
  const nx = nu + 1, ny = nv + 1, n = nx * ny;
  if (heights === null || heights === undefined) heights = new Float64Array(n);
  if (heights.length !== n) throw new OmfError(`${name}: ${heights.length} heights for a ${nx} x ${ny} node grid`);
  let w = vecCross(u, v);
  const wn = vecNorm(w) || 1.0;
  w = w.map(c => c / wn);
  const [okU, du] = isUniform(tensorU), [okV, dv] = isUniform(tensorV);
  const rot = (okU && okV) ? horizontalRotation(u, v, warn, name) : null;
  if (!(okU && okV)) warn(`${name}: variable grid spacing; converted to a triangulated mesh`);
  if (rot !== null && nu >= 1 && nv >= 1) {
    const [rotation, flipV] = rot;
    const vals = new Float64Array(n).fill(NAN);
    let x0, y0;
    if (!flipV) {
      x0 = origin[0]; y0 = origin[1];
      for (let k = 0; k < n; k++) { const h = heights[k]; vals[k] = isNaNv(h) ? NAN : origin[2] + h * w[2]; }
    } else {
      x0 = origin[0] + nv * dv * v[0]; y0 = origin[1] + nv * dv * v[1];
      for (let j = 0; j < ny; j++) { const srcJ = nv - j; for (let i = 0; i < nx; i++) { const h = heights[srcJ * nx + i]; vals[j * nx + i] = isNaNv(h) ? NAN : origin[2] + h * w[2]; } }
    }
    return [new GM.Grid2D(Object.assign({}, kw, { nx, ny, x0, y0, dx: du, dy: dv, values: vals, rotation, name })), true, null];
  }
  const cu = [0.0], cv = [0.0];
  for (const s of tensorU) cu.push(cu[cu.length - 1] + +s);
  for (const s of tensorV) cv.push(cv[cv.length - 1] + +s);
  const verts = new Float64Array(3 * n);
  const valid = new Array(n).fill(false);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const k = j * nx + i, h = heights[k];
    const hh = isNaNv(h) ? 0.0 : h;
    valid[k] = !isNaNv(h);
    verts[3 * k] = origin[0] + cu[i] * u[0] + cv[j] * v[0] + hh * w[0];
    verts[3 * k + 1] = origin[1] + cu[i] * u[1] + cv[j] * v[1] + hh * w[1];
    verts[3 * k + 2] = origin[2] + cu[i] * u[2] + cv[j] * v[2] + hh * w[2];
  }
  const tris = [], triCells = [];
  for (let j = 0; j < nv; j++) for (let i = 0; i < nu; i++) {
    const p = j * nx + i, q = j * nx + i + 1, r = (j + 1) * nx + i, s = (j + 1) * nx + i + 1;
    const cell = j * nu + i;
    if (valid[p] && valid[q] && valid[s]) { tris.push(p, q, s); triCells.push(cell); }
    if (valid[p] && valid[s] && valid[r]) { tris.push(p, s, r); triCells.push(cell); }
  }
  const mesh = new GM.Mesh(Object.assign({}, kw, { vertices: verts, triangles: Uint32Array.from(tris), name }));
  mesh.metadata.from_grid_surface = true;
  return [mesh, false, triCells];
}
/** OMF 3-D grid -> [BlockModel, remap | null] or [null, null]. */
function blockmodelFromGrid(name, origin, u, v, w, tu, tv, tw, warn, kw = {}) {
  const [okU, du] = isUniform(tu), [okV, dv] = isUniform(tv), [okW, dw] = isUniform(tw);
  const nu = tu.length, nv = tv.length, nw = tw.length;
  if (!(okU && okV && okW)) { warn(`${name}: tensor block model with variable block sizes is not supported; skipped`); return [null, null]; }
  if (!(nu && nv && nw)) { warn(`${name}: empty block model; skipped`); return [null, null]; }
  if (Math.abs(u[2]) > 1e-6 || Math.abs(v[2]) > 1e-6 || Math.abs(Math.abs(w[2]) - 1.0) > 1e-6) { warn(`${name}: block model axes are not (horizontal u, v; vertical w); skipped`); return [null, null]; }
  let azimuth = pyRoundN(degrees(Math.atan2(-u[1], u[0])), 9) + 0.0;
  if (azimuth === 0) azimuth = 0;
  const [, vExpect] = azimuthAxes(azimuth);
  const d = vecDot(v, vExpect);
  if (Math.abs(Math.abs(d) - 1.0) > 1e-4) { warn(`${name}: block model u and v axes are not orthogonal; skipped`); return [null, null]; }
  const flipV = d < 0, flipW = w[2] < 0;
  const ox = origin[0] + (flipV ? nv * dv * v[0] : 0.0), oy = origin[1] + (flipV ? nv * dv * v[1] : 0.0), oz = origin[2] + (flipW ? nw * dw * w[2] : 0.0);
  const bm = new GM.BlockModel(Object.assign({}, kw, { origin: [ox, oy, oz], blockSize: [du, dv, dw], count: [nu, nv, nw], azimuth, name }));
  let remap = null;
  if (flipV || flipW) {
    remap = [];
    for (let k = 0; k < nw; k++) { const sk = flipW ? nw - 1 - k : k; for (let j = 0; j < nv; j++) { const sj = flipV ? nv - 1 - j : j; const base = nu * (sj + nv * sk); for (let i = 0; i < nu; i++) remap.push(base + i); } }
  }
  return [bm, remap];
}
function remapValues(values, remap) { if (remap === null) return values; return remap.map(i => values[i]); }

/* ---------------------------------------------------------- attribute records
   {name, location, type: number|text|category|boolean|color|vector|datetime, values, names?, colors?, sub?, dim?, units?, description?, colormap?} */
function record(name, location, rtype, values, extra = {}) { return Object.assign({ name, location, type: rtype, values }, extra); }
function metaList(obj, key, value) { const lst = obj.metadata[key] = obj.metadata[key] || []; if (!lst.includes(value)) lst.push(value); }
function setNumeric(obj, name, loc, col) {
  if (obj.kind === 'points') obj.attributes[name] = nanToNull(col);
  else if (obj.kind === 'blockmodel') obj.addAttribute(name, col instanceof Float64Array ? col : Float64Array.from(col), 'number');
  else { if (!obj.attributes) obj.attributes = {}; obj.attributes[name] = { location: loc, values: Float32Array.from(col) }; }
}
function setText(obj, name, loc, col, kindHint = 'text') {
  if (obj.kind === 'points') obj.attributes[name] = Array.from(col);
  else if (obj.kind === 'blockmodel') obj.addAttribute(name, Array.from(col), kindHint);
  else (obj.metadata.text_attributes = obj.metadata.text_attributes || {})[name] = { location: loc, type: kindHint, values: Array.from(col) };
}
/** Attach attribute records read from a file to a model object (see omf1.py _apply_records). */
function applyRecords(obj, records, warn) {
  const kind = obj.kind;
  const allowed = { points: [LOC_VERTICES], mesh: [LOC_VERTICES, LOC_FACES], lineset: [LOC_VERTICES, LOC_SEGMENTS], blockmodel: [LOC_CELLS] }[kind] || [];
  const columnar = kind === 'points' || kind === 'blockmodel';
  for (const rec of records) {
    const { name, location: loc, type: rtype } = rec;
    const label = `${obj.name}/${name}`;
    if (!allowed.includes(loc)) { warn(`${label}: attribute location ${pyReprStr(loc)} is not supported on a ${kind}; skipped`); continue; }
    const vals = rec.values;
    if (rtype === 'vector') {
      const dim = rec.dim || 3;
      const comps = 'xyz'.slice(0, dim).split('').map(c => `${name}_${c}`);
      (obj.metadata.vector_attributes = obj.metadata.vector_attributes || {})[name] = comps;
      comps.forEach((cname, ci) => setNumeric(obj, cname, loc, Float64Array.from(vals, v => v === null || v === undefined ? NAN : v[ci])));
    } else if (rtype === 'category') {
      const names = Array.from(rec.names || []);
      const entry = { names, index: !columnar };
      if (rec.colors) entry.colors = rec.colors;
      if (rec.sub) entry.attributes = rec.sub;
      (obj.metadata.categories = obj.metadata.categories || {})[name] = entry;
      if (columnar) setText(obj, name, loc, Array.from(vals, i => (i === null || i === undefined || i < 0 || i >= names.length) ? null : names[i]));
      else setNumeric(obj, name, loc, Float64Array.from(vals, i => (i === null || i === undefined || i < 0) ? NAN : i));
    } else if (rtype === 'number') setNumeric(obj, name, loc, Float64Array.from(vals, v => v === null || v === undefined ? NAN : v));
    else if (rtype === 'boolean') {
      metaList(obj, 'boolean_attributes', name);
      if (columnar) setText(obj, name, loc, Array.from(vals, v => v === null || v === undefined ? null : !!v), 'boolean');
      else setNumeric(obj, name, loc, Float64Array.from(vals, v => v === null || v === undefined ? NAN : (v ? 1.0 : 0.0)));
    } else if (rtype === 'text') setText(obj, name, loc, Array.from(vals, v => v === null || v === undefined ? null : String(v)));
    else if (rtype === 'datetime') { metaList(obj, 'datetime_attributes', name); setText(obj, name, loc, Array.from(vals, v => v === null || v === undefined ? null : String(v)), 'datetime'); }
    else if (rtype === 'color') { metaList(obj, 'color_attributes', name); setText(obj, name, loc, Array.from(vals, color4), 'color'); }
    else { warn(`${label}: attribute type ${pyReprStr(rtype)} is not supported; skipped`); continue; }
    if (rec.units) (obj.metadata.attribute_units = obj.metadata.attribute_units || {})[name] = rec.units;
    if (rec.description) (obj.metadata.attribute_descriptions = obj.metadata.attribute_descriptions || {})[name] = rec.description;
    if (rec.colormap) (obj.metadata.colormaps = obj.metadata.colormaps || {})[name] = rec.colormap;
  }
}
/** Model object -> attribute records for writing (inverse of applyRecords; honours the metadata hints). */
function collectRecords(obj, warn) {
  const meta = obj.metadata || {};
  const vecHints = Object.assign({}, meta.vector_attributes || {});
  const catHints = Object.assign({}, meta.categories || {});
  const boolHints = new Set(meta.boolean_attributes || []);
  const colorHints = new Set(meta.color_attributes || []);
  const dateHints = new Set(meta.datetime_attributes || []);
  const units = meta.attribute_units || {}, descs = meta.attribute_descriptions || {}, cmaps = meta.colormaps || {};
  const out = [];
  const kind = obj.kind;
  const finish = (rec) => {
    rec.units = units[rec.name] || '';
    rec.description = descs[rec.name] || '';
    if (rec.name in cmaps) rec.colormap = cmaps[rec.name];
    out.push(rec);
  };
  const cols = new Map();
  let n = null;
  if (kind === 'points') { for (const [k, v] of Object.entries(obj.attributes)) cols.set(k, [LOC_VERTICES, v, null]); n = obj.n; }
  else if (kind === 'blockmodel') { for (const [k, a] of Object.entries(obj.attributes)) cols.set(k, [LOC_CELLS, a.values, a.type || 'number']); n = obj.n; }
  else if (kind === 'mesh' || kind === 'lineset') {
    for (const [k, a] of Object.entries(obj.attributes || {})) {
      let loc = a.location || LOC_VERTICES;
      if (loc === 'cells') loc = kind === 'mesh' ? LOC_FACES : LOC_SEGMENTS;
      cols.set(k, [loc, a.values, a.type || 'number']);
    }
    for (const [k, a] of Object.entries(meta.text_attributes || {})) if (!cols.has(k)) cols.set(k, [a.location || LOC_VERTICES, a.values, a.type || 'text']);
  } else return out;
  const consumed = new Set();
  for (const [vname, comps] of Object.entries(vecHints)) {
    if (comps.length && comps.every(c => cols.has(c))) {
      const loc = cols.get(comps[0])[0];
      const arrays = comps.map(c => Array.from(cols.get(c)[1]));
      const vals = [];
      for (let k = 0; k < arrays[0].length; k++) { const row = arrays.map(a => numOf(a[k])); vals.push(row.some(x => x !== x) ? null : row); }
      for (const c of comps) consumed.add(c);
      finish(record(vname, loc, 'vector', vals, { dim: comps.length }));
    }
  }
  for (const [name, [loc, rawCol, ctype]] of cols) {
    if (consumed.has(name)) continue;
    const col = Array.from(rawCol);
    if (n !== null && col.length !== n) { warn(`${obj.name}/${name}: ${col.length} values for ${n} items; skipped`); continue; }
    if (name in catHints) {
      const hint = catHints[name];
      let names = Array.from(hint.names || []);
      let idx;
      if (hint.index || (ctype === 'number' && !col.some(v => typeof v === 'string'))) {
        idx = col.map(v => { const f = numOf(v); return (f !== f || f < 0) ? null : Math.trunc(f); });
      } else {
        const lookup = new Map(names.map((nm, i) => [nm, i]));
        idx = col.map(v => {
          if (v === null || v === undefined || v === '') return null;
          v = String(v);
          if (!lookup.has(v)) { lookup.set(v, names.length); names.push(v); }
          return lookup.get(v);
        });
      }
      const top = Math.max(-1, ...idx.filter(i => i !== null));
      while (names.length <= top) names.push(`category ${names.length}`);
      finish(record(name, loc, 'category', idx, { names, colors: hint.colors || null, sub: hint.attributes || null }));
      continue;
    }
    if (colorHints.has(name) || ctype === 'color') { finish(record(name, loc, 'color', col.map(color4))); continue; }
    if (dateHints.has(name) || ctype === 'datetime') { finish(record(name, loc, 'datetime', col.map(v => (v === null || v === undefined || v === '') ? null : String(v)))); continue; }
    if (boolHints.has(name) || ctype === 'boolean' || isBoolColumn(col)) {
      const vals = col.map(v => {
        if (v === null || v === undefined || v === '') return null;
        if (typeof v === 'string') return ['1', 'true', 't', 'yes', 'y'].includes(pyStrip(v).toLowerCase());
        const f = numOf(v);
        return f !== f ? null : !!f;
      });
      finish(record(name, loc, 'boolean', vals));
      continue;
    }
    if (ctype === 'number' || allNumeric(col)) { finish(record(name, loc, 'number', col.map(v => { const f = numOf(v); return f !== f ? null : f; }))); continue; }
    finish(record(name, loc, 'text', col.map(v => v === null || v === undefined ? null : String(v))));
  }
  return out;
}
function realignFaceRecords(records, triCells) {
  return records.map(rec => {
    if (rec.location === LOC_FACES && triCells !== null) { const vals = rec.values; rec = Object.assign({}, rec); rec.values = triCells.map(c => c < vals.length ? vals[c] : null); }
    return rec;
  });
}
/** Per-node / per-cell numeric attributes of a grid surface -> extra Grid2D objects (role 'property'). */
function propertyGrids(grid, records, warn) {
  const out = [];
  for (const rec of records) {
    const name = rec.name, rtype = rec.type;
    if (rtype === 'vector') { warn(`${grid.name}/${name}: vector attribute on a grid surface skipped`); continue; }
    if (rtype === 'text' || rtype === 'datetime' || rtype === 'color') { warn(`${grid.name}/${name}: ${rtype} attribute on a grid surface skipped`); continue; }
    let vals;
    if (rtype === 'category') vals = Float64Array.from(rec.values, i => (i === null || i === undefined || i < 0) ? NAN : i);
    else if (rtype === 'boolean') vals = Float64Array.from(rec.values, v => (v === null || v === undefined) ? NAN : (v ? 1.0 : 0.0));
    else vals = Float64Array.from(rec.values, v => (v === null || v === undefined) ? NAN : v);
    const gname = name === grid.name ? grid.name : `${grid.name}/${name}`;
    let g;
    if (rec.location === LOC_VERTICES) {
      if (vals.length !== grid.nx * grid.ny) { warn(`${grid.name}/${name}: ${vals.length} values for ${grid.nx * grid.ny} nodes; skipped`); continue; }
      g = new GM.Grid2D({ nx: grid.nx, ny: grid.ny, x0: grid.x0, y0: grid.y0, dx: grid.dx, dy: grid.dy, values: vals, rotation: grid.rotation, role: 'property', name: gname, color: grid.color });
    } else if (rec.location === LOC_FACES) {
      const nx = grid.nx - 1, ny = grid.ny - 1;
      if (vals.length !== nx * ny || nx < 1 || ny < 1) { warn(`${grid.name}/${name}: ${vals.length} values for ${nx * ny} cells; skipped`); continue; }
      const [x0, y0] = grid.nodeXY(0.5, 0.5);
      g = new GM.Grid2D({ nx, ny, x0, y0, dx: grid.dx, dy: grid.dy, values: vals, rotation: grid.rotation, role: 'property', name: gname, color: grid.color });
      g.metadata.cell_centred = true;
    } else { warn(`${grid.name}/${name}: location ${pyReprStr(rec.location)} not supported on a grid surface`); continue; }
    g.metadata.property_of = grid.id;
    if (rec.units) g.units = rec.units;
    if (rtype === 'category') g.metadata.categories = { [name]: { names: rec.names || null, colors: rec.colors || null, index: true } };
    if (rec.colormap) g.metadata.colormaps = { [name]: rec.colormap };
    out.push(g);
  }
  return out;
}
function resampleGradient(grad, n) {
  grad = (grad || []).map(c => color4(c) || [0, 0, 0, 255]);
  if (!grad.length) grad = [[0, 0, 0, 255]];
  if (grad.length === n) return grad;
  const out = [];
  for (let k = 0; k < n; k++) {
    const t = n > 1 ? k / (n - 1) : 0.0;
    const pos = t * (grad.length - 1);
    const i0 = Math.floor(pos), i1 = Math.min(i0 + 1, grad.length - 1), f = pos - i0;
    out.push([0, 1, 2, 3].map(c => pyRound(grad[i0][c] * (1 - f) + grad[i1][c] * f)));
  }
  return out;
}
function mergeWarnings(project, warns) {
  if (project === null || !warns.length) return;
  const existing = project.metadata.warnings = project.metadata.warnings || [];
  for (const msg of warns) if (!existing.includes(msg)) existing.push(msg);
}

/* ========================================================================
   omf1 — Open Mining Format v0.9 ('OMF-v0.9.0', the omf 1.x python
   package, Leapfrog Geo <= 2024.1).  Layout: magic 84 83 82 81; version
   (32 bytes, NUL padded); project UUID (16); uint64 LE JSON offset; zlib'd
   <f8 / <i8 array blobs from offset 60; JSON registry keyed by UUID.
   ======================================================================== */
const OMF1_MAGIC = [0x84, 0x83, 0x82, 0x81];
const OMF1_VERSION = 'OMF-v0.9.0';

class Omf1Reader {
  constructor(data, registry, label) { this.data = data; this.registry = registry; this.label = label; this.warnings = []; }
  warn(msg) { this.warnings.push(msg); }
  obj(uid, expect = null) {
    if (uid && typeof uid === 'object') return uid;
    const o = this.registry[uid];
    if (!o) throw new OmfError(`missing object ${uid}`);
    if (expect && !expect.includes(o.__class__)) throw new OmfError(`object ${uid} is a ${o.__class__}, expected ${expect.join('/')}`);
    return o;
  }
  async blob(index) {
    const start = Math.trunc(index.start), length = Math.trunc(index.length);
    const dtype = index.dtype || '<f8';
    if (start < 0 || start + length > this.data.length) throw new OmfError('array blob out of range');
    const raw = await inflate(this.data.subarray(start, start + length));
    if (dtype === 'image/png') return raw;
    if (dtype === '<f8') { const n = Math.floor(raw.length / 8); return new Float64Array(raw.buffer.slice(raw.byteOffset, raw.byteOffset + 8 * n)); }
    if (dtype === '<i8') { const n = Math.floor(raw.length / 8); const big = new BigInt64Array(raw.buffer.slice(raw.byteOffset, raw.byteOffset + 8 * n)); return Array.from(big, Number); }
    throw new OmfError(`unknown array dtype ${pyReprStr(dtype)}`);
  }
  async arrayObj(uid) {
    const o = this.obj(uid);
    const arr = o.array;
    if (arr && typeof arr === 'object' && !Array.isArray(arr)) return this.blob(arr);
    return arr === null || arr === undefined ? [] : arr;
  }
  shift(flat, origin) {
    const a = flat instanceof Float64Array ? Float64Array.from(flat) : Float64Array.from(flat, v => v === null ? NAN : +v);
    if (origin.some(c => c)) for (let k = 0; k + 2 < a.length; k += 3) { a[k] += origin[0]; a[k + 1] += origin[1]; a[k + 2] += origin[2]; }
    return a;
  }
  async element(el, porigin) {
    const cls = el.__class__;
    const name = el.name || '';
    const color = el.color;
    const kw = {};
    if (color && color.length >= 3) kw.color = color.slice(0, 3).map(c => Math.trunc(c));
    if (el.description) kw.metadata = { description: el.description };
    const geom = this.obj(el.geometry);
    const gcls = geom.__class__;
    const gorigin = (geom.origin || [0.0, 0.0, 0.0]).map(Number);
    const origin = [0, 1, 2].map(k => porigin[k] + gorigin[k]);
    const objs = [];
    if (cls === 'PointSetElement' && gcls === 'PointSetGeometry') {
      const xyz = this.shift(await this.arrayObj(geom.vertices), origin);
      const obj = new GM.PointSet(Object.assign({}, kw, { xyz, name }));
      obj.metadata.omf_subtype = el.subtype === undefined ? 'point' : el.subtype;
      applyRecords(obj, await this.records(el), m => this.warn(m));
      objs.push(obj);
    } else if (cls === 'LineSetElement' && gcls === 'LineSetGeometry') {
      const xyz = this.shift(await this.arrayObj(geom.vertices), origin);
      const segs = Uint32Array.from(await this.arrayObj(geom.segments));
      const obj = new GM.LineSet(Object.assign({}, kw, { vertices: xyz, segments: segs, name }));
      obj.metadata.omf_subtype = el.subtype === undefined ? 'line' : el.subtype;
      applyRecords(obj, await this.records(el), m => this.warn(m));
      objs.push(obj);
    } else if (cls === 'SurfaceElement' && gcls === 'SurfaceGeometry') {
      const xyz = this.shift(await this.arrayObj(geom.vertices), origin);
      const tris = Uint32Array.from(await this.arrayObj(geom.triangles));
      const obj = new GM.Mesh(Object.assign({}, kw, { vertices: xyz, triangles: tris, name }));
      applyRecords(obj, await this.records(el), m => this.warn(m));
      objs.push(obj);
    } else if (cls === 'SurfaceElement' && gcls === 'SurfaceGridGeometry') {
      let heights = null;
      if (geom.offset_w) heights = await this.arrayObj(geom.offset_w);
      const [obj, isGrid, triCells] = gridFromTensorSurface(name, origin, geom.axis_u || [1, 0, 0], geom.axis_v || [0, 1, 0], geom.tensor_u || [], geom.tensor_v || [], heights, m => this.warn(m), kw);
      const recs = await this.records(el);
      if (isGrid) {
        const props = propertyGrids(obj, recs, m => this.warn(m));
        if (heights !== null || !props.length) objs.push(obj);
        for (const p of props) objs.push(p);
      } else { applyRecords(obj, realignFaceRecords(recs, triCells), m => this.warn(m)); objs.push(obj); }
    } else if (cls === 'VolumeElement' && gcls === 'VolumeGridGeometry') {
      const [bm, remap] = blockmodelFromGrid(name, origin, geom.axis_u || [1, 0, 0], geom.axis_v || [0, 1, 0], geom.axis_w || [0, 0, 1], geom.tensor_u || [], geom.tensor_v || [], geom.tensor_w || [], m => this.warn(m), kw);
      if (bm !== null) {
        const recs = await this.records(el);
        for (const r of recs) if (r.location === LOC_CELLS) r.values = remapValues(Array.from(r.values), remap);
        applyRecords(bm, recs, m => this.warn(m));
        objs.push(bm);
      }
    } else this.warn(`element ${pyReprStr(name)}: unsupported class ${cls} / ${gcls}; skipped`);
    if (el.textures && el.textures.length) this.warn(`element ${pyReprStr(name)}: ${el.textures.length} image texture(s) skipped`);
    return objs;
  }
  async records(el) {
    const out = [];
    for (const uid of el.data || []) {
      let rec;
      try { rec = await this.record(this.obj(uid)); }
      catch (e) { if (!(e instanceof OmfError)) throw e; this.warn(`${el.name}: data ${uid} unreadable: ${e.message}`); continue; }
      if (rec !== null) out.push(rec);
    }
    return out;
  }
  async record(d) {
    const cls = d.__class__;
    const name = d.name || '';
    const loc = d.location || LOC_VERTICES;
    let desc = d.description || '';
    let marker = null;
    for (const mk of [MARK_BOOLEAN, MARK_DATETIME]) if (desc.endsWith(mk)) { marker = mk; desc = pyRstrip(desc.slice(0, -mk.length)); }
    if (cls === 'ScalarData') {
      const vals = Array.from(await this.arrayObj(d.array), v => v !== v ? null : +v);
      if (marker === MARK_BOOLEAN) return record(name, loc, 'boolean', vals.map(v => v === null ? null : !!v), { description: desc });
      const rec = record(name, loc, 'number', vals, { description: desc });
      if (d.colormap) {
        try {
          const cm = this.obj(d.colormap);
          const grad = Array.from(await this.arrayObj(cm.gradient), color4);
          rec.colormap = { type: 'continuous', range: cm.limits.map(Number), gradient: grad };
        } catch (e) { this.warn(`${name}: colormap unreadable`); }
      }
      return rec;
    }
    if (cls === 'StringData') {
      const vals = Array.from(await this.arrayObj(d.array), v => v === null || v === undefined ? null : String(v));
      if (marker === MARK_DATETIME) return record(name, loc, 'datetime', vals.map(v => v || null), { description: desc });
      return record(name, loc, 'text', vals, { description: desc });
    }
    if (cls === 'DateTimeData') return record(name, loc, 'datetime', Array.from(await this.arrayObj(d.array)), { description: desc });
    if (cls === 'Vector2Data' || cls === 'Vector3Data') {
      const dim = cls === 'Vector2Data' ? 2 : 3;
      const flat = Array.from(await this.arrayObj(d.array));
      const vals = [];
      for (let k = 0; k + dim <= flat.length; k += dim) { const row = flat.slice(k, k + dim).map(Number); vals.push(row.some(x => x !== x) ? null : row); }
      return record(name, loc, 'vector', vals, { dim, description: desc });
    }
    if (cls === 'ColorData') {
      const arr = this.obj(d.array);
      const raw = arr.array;
      let vals;
      if (raw && typeof raw === 'object' && !Array.isArray(raw)) { const flat = Array.from(await this.blob(raw)); vals = []; for (let k = 0; k + 2 < flat.length; k += 3) vals.push(color4(flat.slice(k, k + 3))); }
      else vals = (raw || []).map(color4);
      return record(name, loc, 'color', vals, { description: desc });
    }
    if (cls === 'MappedData') {
      const idx = Array.from(await this.arrayObj(d.array), v => v < 0 ? null : Math.trunc(v));
      let names = null, colors = null;
      const sub = {};
      for (const luid of d.legends || []) {
        const leg = this.obj(luid);
        const valsObj = this.obj(leg.values);
        const vcls = valsObj.__class__;
        let vals = valsObj.array;
        if (vals && typeof vals === 'object' && !Array.isArray(vals)) vals = Array.from(await this.blob(vals));
        if (vcls === 'StringArray' && names === null) names = vals.map(String);
        else if (vcls === 'ColorArray' && colors === null) colors = vals.map(color4);
        else sub[leg.name || vcls] = Array.from(vals);
      }
      if (names === null) { const top = Math.max(-1, ...idx.filter(i => i !== null)); names = []; for (let i = 0; i <= top; i++) names.push(String(i)); }
      return record(name, loc, 'category', idx, { names, colors, sub: Object.keys(sub).length ? sub : null, description: desc });
    }
    this.warn(`data ${pyReprStr(name)}: unsupported class ${cls}; skipped`);
    return null;
  }
}
/** OMF v0.9 bytes -> Promise<Project>. */
export async function readOmf1(src, opts = {}) {
  const data = toU8(src);
  const label = opts.file || '<bytes>';
  if (data.length < 60 || !OMF1_MAGIC.every((b, i) => data[i] === b)) throw new OmfError('not an OMF v0.9 file (bad magic)');
  if (!bytesEq(data, 4, OMF1_VERSION)) {
    let end = 4; while (end < 36 && data[end] !== 0) end++;
    throw new OmfError(`unsupported OMF version ${pyReprStr(decodeLatin1(data.subarray(4, end)))}`);
  }
  const projectUid = uuidFromBytes(data.subarray(36, 52));
  const jsonStart = Number(new DataView(data.buffer, data.byteOffset, data.byteLength).getBigUint64(52, true));
  if (jsonStart < 60 || jsonStart > data.length) throw new OmfError(`bad JSON offset ${jsonStart}`);
  let registry;
  try { registry = JSON.parse(new TextDecoder().decode(data.subarray(jsonStart))); }
  catch (e) { throw new OmfError(`bad JSON registry: ${e.message}`); }
  let pj = registry[projectUid];
  if (!pj || pj.__class__ !== 'Project') {
    pj = null;
    for (const v of Object.values(registry)) if (v && typeof v === 'object' && v.__class__ === 'Project') { pj = v; break; }
    if (pj === null) throw new OmfError('no Project object in registry');
  }
  const reader = new Omf1Reader(data, registry, label);
  const project = new GM.Project({ name: pj.name || 'model' });
  Object.assign(project.metadata, { omf_version: '0.9.0', author: pj.author || '', description: pj.description || '', revision: pj.revision || '', units: pj.units || '',
    date: pj.date || pj.date_created || '', warnings: reader.warnings });
  if (pj.units) project.crs.units = pj.units;
  const porigin = (pj.origin || [0.0, 0.0, 0.0]).map(Number);
  for (const uid of pj.elements || []) {
    const el = registry[uid];
    if (!el) { reader.warn(`element ${uid} missing from registry`); continue; }
    let objs;
    try { objs = await reader.element(el, porigin); }
    catch (e) { if (!(e instanceof OmfError)) throw e; reader.warn(`element ${pyReprStr(el.name)}: ${e.message}`); continue; }
    for (const obj of objs) { obj.provenance = { format: 'omf1', file: label, element: el.name || '' }; project.add(obj); }
  }
  return project;
}

class Omf1Writer {
  constructor(warn) { this.buf = new ByteWriter(1 << 16); this.buf.zeros(60); this.reg = {}; this.now = GM.nowISO(); this.warn = warn; }
  add(cls, props) { const uid = uuid4(); this.reg[uid] = Object.assign({ __class__: cls, date_created: this.now, date_modified: this.now }, props); return uid; }
  async blob(typecode, values) {
    let raw;
    if (typecode === 'd') { const a = values instanceof Float64Array ? values : Float64Array.from(values, v => v === null || v === undefined ? NAN : +v); raw = new Uint8Array(a.buffer, a.byteOffset, a.byteLength); }
    else { const a = new BigInt64Array(values.length); for (let i = 0; i < values.length; i++) a[i] = BigInt(Math.trunc(values[i])); raw = new Uint8Array(a.buffer); }
    const start = this.buf.length;
    this.buf.bytes(await deflate(raw));
    return { start, length: this.buf.length - start, dtype: typecode === 'd' ? '<f8' : '<i8' };
  }
  async f8(cls, values) { return this.add(cls, { array: await this.blob('d', values) }); }
  async i8(cls, values) { return this.add(cls, { array: await this.blob('q', Array.from(values, v => Math.trunc(v))) }); }
  async data(records, allowed, label) {
    const uids = [];
    for (const rec of records) {
      const loc = rec.location;
      if (!allowed.includes(loc)) { this.warn(`${label}/${rec.name}: location ${pyReprStr(loc)} cannot be written to this element`); continue; }
      const props = { name: rec.name, description: rec.description || '', location: loc };
      const rtype = rec.type, vals = rec.values;
      if (rtype === 'number') {
        props.array = await this.f8('ScalarArray', vals.map(v => v === null || v === undefined ? NAN : +v));
        const cm = rec.colormap;
        if (cm && cm.type === 'continuous' && cm.gradient) {
          const grad = resampleGradient(cm.gradient, 128);
          const gid = this.add('ColorArray', { array: grad.map(g => g.slice(0, 3)) });
          const rng = cm.range || [0.0, 1.0];
          props.colormap = this.add('ScalarColormap', { name: '', description: '', gradient: gid, limits: [+rng[0], +rng[1]] });
        }
        uids.push(this.add('ScalarData', props));
      } else if (rtype === 'text') {
        props.array = this.add('StringArray', { array: vals.map(v => v === null || v === undefined ? '' : String(v)) });
        uids.push(this.add('StringData', props));
      } else if (rtype === 'datetime') {
        if (vals.every(v => v !== null && v !== undefined)) { props.array = this.add('DateTimeArray', { array: vals.map(String) }); uids.push(this.add('DateTimeData', props)); }
        else {
          props.array = this.add('StringArray', { array: vals.map(v => v === null || v === undefined ? '' : String(v)) });
          props.description = pyStrip(props.description + ' ' + MARK_DATETIME);
          uids.push(this.add('StringData', props));
        }
      } else if (rtype === 'boolean') {
        props.array = await this.f8('ScalarArray', vals.map(v => v === null || v === undefined ? NAN : (v ? 1.0 : 0.0)));
        props.description = pyStrip(props.description + ' ' + MARK_BOOLEAN);
        uids.push(this.add('ScalarData', props));
      } else if (rtype === 'vector') {
        const dim = rec.dim || 3;
        const flat = [];
        for (const v of vals) { if (v === null || v === undefined) for (let k = 0; k < dim; k++) flat.push(NAN); else for (let k = 0; k < dim; k++) flat.push(+v[k]); }
        props.array = await this.f8(dim === 2 ? 'Vector2Array' : 'Vector3Array', flat);
        uids.push(this.add(dim === 2 ? 'Vector2Data' : 'Vector3Data', props));
      } else if (rtype === 'color') {
        const flat = [];
        for (const v of vals) { const c = color4(v) || [255, 255, 255, 255]; flat.push(c[0], c[1], c[2]); }
        props.array = await this.i8('Int3Array', flat);
        uids.push(this.add('ColorData', props));
      } else if (rtype === 'category') {
        props.array = await this.i8('ScalarArray', vals.map(i => i === null || i === undefined ? -1 : Math.trunc(i)));
        const legends = [this.add('Legend', { name: rec.name, description: '', values: this.add('StringArray', { array: Array.from(rec.names || []) }) })];
        if (rec.colors) {
          const cols = rec.colors.map(c => (color4(c) || [128, 128, 128, 255]).slice(0, 3));
          legends.push(this.add('Legend', { name: rec.name + ' colors', description: '', values: this.add('ColorArray', { array: cols }) }));
        }
        for (const [sname, svalsRaw] of Object.entries(rec.sub || {})) {
          const svals = Array.from(svalsRaw);
          const vid = allNumeric(svals) ? await this.f8('ScalarArray', svals.map(numOf)) : this.add('StringArray', { array: svals.map(v => v === null || v === undefined ? '' : String(v)) });
          legends.push(this.add('Legend', { name: sname, description: '', values: vid }));
        }
        props.legends = legends;
        uids.push(this.add('MappedData', props));
      } else this.warn(`${label}/${rec.name}: attribute type ${pyReprStr(rtype)} not written`);
    }
    return uids;
  }
  async element(obj, name) {
    name = name || obj.name || obj.id;
    const color = (obj.color || [160, 160, 160]).slice(0, 3).map(c => Math.trunc(c));
    const desc = obj.metadata && obj.metadata.description !== undefined ? String(obj.metadata.description) : '';
    const recs = collectRecords(obj, this.warn);
    if (obj.kind === 'points') {
      const geom = this.add('PointSetGeometry', { origin: [0.0, 0.0, 0.0], vertices: await this.f8('Vector3Array', obj.xyz) });
      return this.add('PointSetElement', { name, description: desc, color, subtype: 'point', geometry: geom, textures: [], data: await this.data(recs, [LOC_VERTICES], name) });
    }
    if (obj.kind === 'lineset') {
      const geom = this.add('LineSetGeometry', { origin: [0.0, 0.0, 0.0], vertices: await this.f8('Vector3Array', obj.vertices), segments: await this.i8('Int2Array', obj.segments) });
      return this.add('LineSetElement', { name, description: desc, color, subtype: 'line', geometry: geom, data: await this.data(recs, [LOC_VERTICES, LOC_SEGMENTS], name) });
    }
    if (obj.kind === 'mesh') {
      const geom = this.add('SurfaceGeometry', { origin: [0.0, 0.0, 0.0], vertices: await this.f8('Vector3Array', obj.vertices), triangles: await this.i8('Int3Array', obj.triangles) });
      return this.add('SurfaceElement', { name, description: desc, color, subtype: 'surface', geometry: geom, textures: [], data: await this.data(recs, [LOC_VERTICES, LOC_FACES], name) });
    }
    if (obj.kind === 'grid2d') {
      if (obj.nx < 2 || obj.ny < 2) { this.warn(`${name}: grids need at least 2 x 2 nodes for OMF; skipped`); return null; }
      const [au, av] = rotationAxes(obj.rotation);
      const props = { origin: [obj.x0, obj.y0, 0.0], tensor_u: new Array(obj.nx - 1).fill(obj.dx), tensor_v: new Array(obj.ny - 1).fill(obj.dy), axis_u: au, axis_v: av };
      let data = [];
      if (obj.role === 'property') data = await this.data([record(name, LOC_VERTICES, 'number', Array.from(obj.values), { units: obj.units })], [LOC_VERTICES], name);
      else props.offset_w = await this.f8('ScalarArray', obj.values);
      const geom = this.add('SurfaceGridGeometry', props);
      return this.add('SurfaceElement', { name, description: desc, color, subtype: 'surface', geometry: geom, textures: [], data });
    }
    if (obj.kind === 'blockmodel') {
      const [au, av, aw] = azimuthAxes(obj.azimuth);
      const geom = this.add('VolumeGridGeometry', { origin: obj.origin.slice(), tensor_u: new Array(obj.count[0]).fill(obj.blockSize[0]), tensor_v: new Array(obj.count[1]).fill(obj.blockSize[1]),
        tensor_w: new Array(obj.count[2]).fill(obj.blockSize[2]), axis_u: au, axis_v: av, axis_w: aw });
      return this.add('VolumeElement', { name, description: desc, color, subtype: 'volume', geometry: geom, data: await this.data(recs, [LOC_CELLS], name) });
    }
    if (obj.kind === 'drillholes') return this.element(obj.tracesLineSet(), name + ' traces');
    this.warn(`${name}: object kind ${pyReprStr(obj.kind)} has no OMF v0.9 equivalent; skipped`);
    return null;
  }
}
/** Project | objects -> OMF v0.9 bytes. opts: name, description, author, revision, units ('m'), warnings (array to append to). */
export async function writeOmf1(projectOrObjects, opts = {}) {
  const [project, objects] = objectsOf(projectOrObjects);
  const warns = opts.warnings || [];
  let { name = '', description = '', author = '', revision = '', units = 'm' } = opts;
  if (project !== null) {
    name = name || project.name;
    description = description || String(project.metadata.description || '');
    author = author || String(project.metadata.author || '');
    units = (project.crs && project.crs.units) || units;
  }
  const w = new Omf1Writer(m => warns.push(m));
  const names = uniqueNames(objects.map(o => o.name), 'element');
  const elements = [];
  for (let k = 0; k < objects.length; k++) { const uid = await w.element(objects[k], names[k]); if (uid) elements.push(uid); }
  const puid = uuid4();
  w.reg[puid] = { __class__: 'Project', date_created: w.now, date_modified: w.now, name: name || 'model', description: description || '', author: author || APPLICATION,
    revision: revision || '', units: units || '', origin: [0.0, 0.0, 0.0], elements };
  const jsonStart = w.buf.length;
  w.buf.bytes(utf8(JSON.stringify(w.reg)));
  const out = w.buf.result();
  out.set(OMF1_MAGIC, 0);
  out.set(encodeAscii(OMF1_VERSION), 4);
  out.set(uuidBytes(puid), 36);
  new DataView(out.buffer, out.byteOffset, out.byteLength).setBigUint64(52, BigInt(jsonStart), true);
  mergeWarnings(project, warns);
  return out;
}

/* ========================================================================
   omf2 — Open Mining Format v2.0 (omf-rust / Leapfrog Geo 2025.1+).
   ZIP of STORED members (comment 'Open Mining Format 2.0[-pre]'),
   index.json.gz (written last), arrays as <n>.parquet with the exact
   parquet schemas omf-rust accepts.
   ======================================================================== */
const OMF2_FORMAT_NAME = 'Open Mining Format';
const OMF2_MAJOR = 2, OMF2_MINOR = 0;
export const OMF2_DEFAULT_PRERELEASE = 'beta.1';
const OMF2_INDEX = 'index.json.gz';
const NWMM_KEY = 'nwmm';

function omf2ParseComment(comment) {
  const m = /^Open Mining Format (\d+)\.(\d+)(?:-([^\s]+))?$/.exec(pyStrip(comment || ''));
  if (!m) return null;
  return [[parseInt(m[1], 10), parseInt(m[2], 10)], m[3] === undefined ? null : m[3]];
}
export function omf2FormatComment(prerelease = OMF2_DEFAULT_PRERELEASE) {
  let s = `${OMF2_FORMAT_NAME} ${OMF2_MAJOR}.${OMF2_MINOR}`;
  if (prerelease) s += '-' + prerelease;
  return s;
}
function crsFromString(crs, units) {
  units = pyStrip(units || '').toLowerCase();
  const unit = ['feet', 'foot', 'ft', 'us survey feet', 'international feet'].includes(units) ? 'ft' : 'm';
  crs = pyStrip(crs || '');
  const m = /^EPSG:(\d+)$/i.exec(crs);
  if (m) {
    const code = parseInt(m[1], 10);
    let d;
    if (code >= 32601 && code <= 32660) d = GM.utm.crs(code - 32600, true);
    else if (code >= 32701 && code <= 32760) d = GM.utm.crs(code - 32700, false);
    else d = { kind: 'local', epsg: code, units: unit };
    d.crs_string = crs;
    return d;
  }
  const d = { kind: 'local', units: unit };
  if (crs) d.crs_string = crs;
  return d;
}
function addOrigin(origin, offset) { if (!offset) return origin.slice(); return [0, 1, 2].map(k => origin[k] + +offset[k]); }
function nwmmMetadata(obj) {
  const d = { kind: obj.kind, id: obj.id };
  for (const key of ['role', 'units', 'group']) { const v = obj[key]; if (v) d[key] = v; }
  if (obj.visible === false) d.visible = false;
  return d;
}
function restoreNwmm(obj, d) {
  if (d.role && 'role' in obj) obj.role = d.role;
  if (d.units && 'units' in obj) obj.units = d.units;
  if (d.group && !obj.group) obj.group = d.group;
  if (d.id) obj.id = d.id;
  if (d.visible === false) obj.visible = false;
}

class Omf2Reader {
  constructor(zip, index, label) { this.zip = zip; this.index = index; this.label = label; this.warnings = []; this.cache = new Map(); }
  warn(msg) { this.warnings.push(msg); }
  async table(ref) {
    if (!ref || typeof ref !== 'object' || !('filename' in ref)) throw new OmfError(`bad array reference ${JSON.stringify(ref)}`);
    const fn = ref.filename;
    if (!this.cache.has(fn)) {
      if (!this.zip.has(fn)) throw new OmfError(`missing array file ${fn}`);
      let pf;
      try { pf = await readParquet(await this.zip.read(fn)); }
      catch (e) { if (e instanceof ParquetError || e instanceof ThriftError) throw new OmfError(`${fn}: ${e.message}`); throw e; }
      this.cache.set(fn, pf);
    }
    const pf = this.cache.get(fn);
    const n = ref.item_count === undefined ? pf.numRows : Math.trunc(ref.item_count);
    if (n !== pf.numRows) this.warn(`${fn}: item_count ${n} differs from the parquet row count ${pf.numRows}`);
    return pf;
  }
  async scalars(ref) { return (await this.table(ref)).column('scalar'); }
  async vertices(ref, origin) {
    const pf = await this.table(ref);
    const xs = pf.column('x'), ys = pf.column('y'), zs = pf.column('z');
    const out = new Float64Array(3 * xs.length);
    for (let k = 0; k < xs.length; k++) { out[3 * k] = xs[k] + origin[0]; out[3 * k + 1] = ys[k] + origin[1]; out[3 * k + 2] = zs[k] + origin[2]; }
    return out;
  }
  async indices(ref, cols) {
    const pf = await this.table(ref);
    const columns = cols.map(c => pf.column(c));
    const n = columns[0].length, w = cols.length;
    const out = new Uint32Array(n * w);
    for (let k = 0; k < n; k++) for (let c = 0; c < w; c++) out[k * w + c] = columns[c][k];
    return out;
  }
  async element(el, porigin, group) {
    const name = el.name || '';
    const geom = el.geometry || {};
    const gtype = geom.type;
    const kw = {};
    const color = color4(el.color);
    if (color) { kw.color = color.slice(0, 3); if (color[3] < 255) kw.opacity = color[3] / 255.0; }
    if (group) kw.group = group;
    const meta = {};
    if (el.description) meta.description = el.description;
    const elMeta = Object.assign({}, el.metadata || {});
    const nwmm = elMeta[NWMM_KEY];
    delete elMeta[NWMM_KEY];
    if (Object.keys(elMeta).length) meta.omf_metadata = elMeta;
    if (Object.keys(meta).length) kw.metadata = meta;
    const objs = [];
    const warn = m => this.warn(m);
    try {
      if (gtype === 'Composite') {
        for (const child of geom.elements || []) for (const o of await this.element(child, porigin, name)) objs.push(o);
        if (el.attributes && el.attributes.length) this.warn(`${name}: attributes on a composite element skipped`);
        return objs;
      }
      if (gtype === 'PointSet') {
        const origin = addOrigin(porigin, geom.origin);
        const obj = new GM.PointSet(Object.assign({}, kw, { xyz: await this.vertices(geom.vertices, origin), name }));
        applyRecords(obj, await this.records(el, gtype), warn);
        objs.push(obj);
      } else if (gtype === 'LineSet') {
        const origin = addOrigin(porigin, geom.origin);
        const obj = new GM.LineSet(Object.assign({}, kw, { vertices: await this.vertices(geom.vertices, origin), segments: await this.indices(geom.segments, ['a', 'b']), name }));
        applyRecords(obj, await this.records(el, gtype), warn);
        objs.push(obj);
      } else if (gtype === 'Surface') {
        const origin = addOrigin(porigin, geom.origin);
        const obj = new GM.Mesh(Object.assign({}, kw, { vertices: await this.vertices(geom.vertices, origin), triangles: await this.indices(geom.triangles, ['a', 'b', 'c']), name }));
        applyRecords(obj, await this.records(el, gtype), warn);
        objs.push(obj);
      } else if (gtype === 'GridSurface') {
        const orient = geom.orient || {};
        const origin = addOrigin(porigin, orient.origin);
        const u = orient.u || [1.0, 0.0, 0.0], v = orient.v || [0.0, 1.0, 0.0];
        const [tu, tv] = await this.grid2(geom.grid || {});
        const heights = geom.heights ? await this.scalars(geom.heights) : null;
        const [obj, isGrid, triCells] = gridFromTensorSurface(name, origin, u, v, tu, tv, heights, warn, kw);
        const recs = await this.records(el, gtype);
        if (isGrid) {
          if (heights === null && recs.length) for (const g of propertyGrids(obj, recs, warn)) objs.push(g);
          else { objs.push(obj); for (const g of propertyGrids(obj, recs, warn)) objs.push(g); }
        } else { applyRecords(obj, realignFaceRecords(recs, triCells), warn); objs.push(obj); }
      } else if (gtype === 'BlockModel') {
        const orient = geom.orient || {};
        const origin = addOrigin(porigin, orient.origin);
        const u = orient.u || [1.0, 0.0, 0.0], v = orient.v || [0.0, 1.0, 0.0], w = orient.w || [0.0, 0.0, 1.0];
        const [tu, tv, tw] = await this.grid3(geom.grid || {});
        if (geom.subblocks) this.warn(`${name}: sub-blocks are not supported; parent blocks only`);
        const [bm, remap] = blockmodelFromGrid(name, origin, u, v, w, tu, tv, tw, warn, kw);
        if (bm !== null) {
          const recs = await this.records(el, gtype);
          for (const r of recs) if (r.location === LOC_CELLS) r.values = remapValues(Array.from(r.values), remap);
          applyRecords(bm, recs, warn);
          objs.push(bm);
        }
      } else this.warn(`${name}: unsupported geometry type ${pyReprStr(gtype)}; skipped`);
    } catch (e) {
      if (!(e instanceof FormatError || e instanceof TypeError || e instanceof RangeError)) throw e;
      this.warn(`${name}: unreadable (${e.constructor.name}: ${e.message})`);
      return [];
    }
    for (const obj of objs) {
      obj.provenance = { format: 'omf2', file: this.label, element: name };
      if (nwmm && typeof nwmm === 'object' && obj.name === name) restoreNwmm(obj, nwmm);
    }
    return objs;
  }
  async grid2(grid) {
    if (grid.type === 'Regular') { const size = grid.size, count = grid.count; return [new Array(Math.trunc(count[0])).fill(+size[0]), new Array(Math.trunc(count[1])).fill(+size[1])]; }
    if (grid.type === 'Tensor') return [Array.from(await this.scalars(grid.u)), Array.from(await this.scalars(grid.v))];
    throw new OmfError(`unknown grid type ${pyReprStr(grid.type)}`);
  }
  async grid3(grid) {
    if (grid.type === 'Regular') { const size = grid.size, count = grid.count; return [0, 1, 2].map(k => new Array(Math.trunc(count[k])).fill(+size[k])); }
    if (grid.type === 'Tensor') return [Array.from(await this.scalars(grid.u)), Array.from(await this.scalars(grid.v)), Array.from(await this.scalars(grid.w))];
    throw new OmfError(`unknown grid type ${pyReprStr(grid.type)}`);
  }
  async records(el, gtype) {
    const out = [];
    for (const att of el.attributes || []) {
      let rec;
      try { rec = await this.record(att, gtype, el.name || ''); }
      catch (e) { if (!(e instanceof FormatError || e instanceof TypeError || e instanceof RangeError)) throw e; this.warn(`${el.name}/${att.name}: unreadable attribute (${e.message})`); continue; }
      if (rec !== null) out.push(rec);
    }
    return out;
  }
  async record(att, gtype, ename) {
    const name = att.name || '';
    const loc = att.location;
    const data = att.data || {};
    const dtype = data.type;
    let location;
    if (loc === 'Vertices') location = LOC_VERTICES;
    else if (loc === 'Primitives') location = { LineSet: LOC_SEGMENTS, Surface: LOC_FACES, GridSurface: LOC_FACES, BlockModel: LOC_CELLS }[gtype] || 'primitives';
    else if (loc === 'Categories' && gtype === 'Categories') location = loc;
    else {
      if (dtype !== 'MappedTexture' && dtype !== 'ProjectedTexture') { this.warn(`${ename}/${name}: attribute location ${pyReprStr(loc)} skipped`); return null; }
      location = loc;
    }
    const extra = { units: att.units || '', description: att.description || '' };
    if (dtype === 'Number') {
      const pf = await this.table(data.values);
      const leaf = pf.leaf('number');
      const vals = pf.column('number');
      const rec = this.numberRecord(name, location, vals, leaf, extra);
      if (data.colormap) rec.colormap = await this.colormap(data.colormap, leaf);
      return rec;
    }
    if (dtype === 'Text') return record(name, location, 'text', Array.from((await this.table(data.values)).column('text')), extra);
    if (dtype === 'Boolean') return record(name, location, 'boolean', Array.from((await this.table(data.values)).column('bool')), extra);
    if (dtype === 'Category') {
      const idx = Array.from((await this.table(data.values)).column('index'));
      const names = Array.from((await this.table(data.names)).column('name'));
      const colors = data.gradient ? await this.gradient(data.gradient) : null;
      const sub = {};
      for (const satt of data.attributes || []) { const srec = await this.record(satt, 'Categories', ename + '/' + name); if (srec !== null) sub[satt.name || ''] = Array.from(srec.values); }
      return record(name, location, 'category', idx, Object.assign({ names, colors, sub: Object.keys(sub).length ? sub : null }, extra));
    }
    if (dtype === 'Vector') {
      const pf = await this.table(data.values);
      const xs = pf.column('vector.x'), ys = pf.column('vector.y');
      const zs = pf.leaf('vector.z') ? pf.column('vector.z') : null;
      const vals = [];
      for (let k = 0; k < xs.length; k++) vals.push(xs[k] === null ? null : (zs === null ? [xs[k], ys[k]] : [xs[k], ys[k], zs[k]]));
      return record(name, location, 'vector', vals, Object.assign({ dim: zs === null ? 2 : 3 }, extra));
    }
    if (dtype === 'Color') {
      const pf = await this.table(data.values);
      const cols = ['r', 'g', 'b', 'a'].map(c => pf.column('color.' + c));
      const vals = [];
      for (let k = 0; k < cols[0].length; k++) vals.push(cols[0][k] === null ? null : [cols[0][k], cols[1][k], cols[2][k], cols[3][k]]);
      return record(name, location, 'color', vals, extra);
    }
    if (dtype === 'MappedTexture' || dtype === 'ProjectedTexture') { this.warn(`${ename}/${name}: ${dtype} skipped (image ${(data.image || {}).filename})`); return null; }
    this.warn(`${ename}/${name}: unsupported attribute data type ${pyReprStr(dtype)}; skipped`);
    return null;
  }
  numberRecord(name, location, vals, leaf, extra) {
    const lt = leaf ? leaf.logical : null;
    if (lt && lt[0] === 'date') return record(name, location, 'datetime', Array.from(vals, v => v === null ? null : isoFromEpochDays(v)), extra);
    if (lt && lt[0] === 'timestamp') {
      const scale = { millis: 1000, micros: 1, nanos: 0.001 }[lt[1]];
      return record(name, location, 'datetime', Array.from(vals, v => v === null ? null : isoFromEpochMicros(Math.trunc(v * scale))), extra);
    }
    return record(name, location, 'number', Array.from(vals, v => v === null ? null : +v), extra);
  }
  async gradient(ref) {
    const pf = await this.table(ref);
    const cols = ['r', 'g', 'b', 'a'].map(c => pf.column(c));
    const out = [];
    for (let k = 0; k < cols[0].length; k++) out.push([cols[0][k], cols[1][k], cols[2][k], cols[3][k]]);
    return out;
  }
  async colormap(cm, leaf) {
    try {
      if (cm.type === 'Continuous') { const rng = cm.range || {}; return { type: 'continuous', range: [rng.min === undefined ? null : rng.min, rng.max === undefined ? null : rng.max], gradient: await this.gradient(cm.gradient) }; }
      if (cm.type === 'Discrete') {
        const pf = await this.table(cm.boundaries);
        const vals = pf.column('value'), inc = pf.column('inclusive');
        const bounds = [];
        for (let k = 0; k < vals.length; k++) bounds.push([vals[k], !!inc[k]]);
        return { type: 'discrete', boundaries: bounds, gradient: await this.gradient(cm.gradient) };
      }
    } catch (e) { if (!(e instanceof FormatError || e instanceof TypeError)) throw e; this.warn(`colormap unreadable: ${e.message}`); }
    return null;
  }
}
/** OMF v2 bytes -> Promise<Project>. */
export async function readOmf2(src, opts = {}) {
  const data = toU8(src);
  const label = opts.file || '<bytes>';
  let zip;
  try { zip = new ZipReader(data); }
  catch (e) { throw new OmfError(`not an OMF v2 (zip) file: ${e.message}`); }
  const ver = omf2ParseComment(zip.comment);
  if (ver === null) throw new OmfError(`zip archive comment ${pyReprStr(zip.comment.slice(0, 60))} is not an Open Mining Format version tag`);
  const [[major, minor], pre] = ver;
  if (major !== OMF2_MAJOR) throw new OmfError(`unsupported OMF major version ${major}.${minor}`);
  if (!zip.has(OMF2_INDEX)) throw new OmfError(`missing ${OMF2_INDEX}`);
  let index;
  try { index = JSON.parse(new TextDecoder().decode(await gunzip(await zip.read(OMF2_INDEX)))); }
  catch (e) { throw new OmfError(`bad index.json.gz: ${e.message}`); }
  const rd = new Omf2Reader(zip, index, label);
  const project = new GM.Project({ name: index.name || 'model' });
  const version = `${major}.${minor}` + (pre ? '-' + pre : '');
  Object.assign(project.metadata, { omf_version: version, author: index.author || '', description: index.description || '', date: index.date || '', application: index.application || '',
    units: index.units || '', coordinate_reference_system: index.coordinate_reference_system || '', warnings: rd.warnings });
  if (index.metadata) project.metadata.omf_metadata = index.metadata;
  project.crs = crsFromString(index.coordinate_reference_system || '', index.units || '');
  const porigin = (index.origin || [0.0, 0.0, 0.0]).map(Number);
  for (const el of index.elements || []) for (const obj of await rd.element(el, porigin, '')) project.add(obj);
  return project;
}

class Omf2Writer {
  constructor(zip, warn, compression = 'gzip') { this.zip = zip; this.warn = warn; this.compression = compression; this.counter = 0; }
  store(data, ext) { this.counter++; const name = `${this.counter}${ext}`; this.zip.add(name, data); return name; }
  async table(fields, n) { const data = await writeParquet(fields, { compression: this.compression }); return { filename: this.store(data, '.parquet'), item_count: n }; }
  async scalars(values) { const vals = Float64Array.from(values, v => +v); return this.table([new Column('scalar', 'double', vals)], vals.length); }
  async vertices(flat) {
    const n = Math.floor(flat.length / 3);
    const xs = new Float64Array(n), ys = new Float64Array(n), zs = new Float64Array(n);
    for (let k = 0; k < n; k++) { xs[k] = flat[3 * k]; ys[k] = flat[3 * k + 1]; zs[k] = flat[3 * k + 2]; }
    return this.table([new Column('x', 'double', xs), new Column('y', 'double', ys), new Column('z', 'double', zs)], n);
  }
  async indexTuples(flat, names) {
    const w = names.length, n = Math.floor(flat.length / w);
    const cols = names.map((nm, k) => { const col = new Array(n); for (let i = 0; i < n; i++) col[i] = Math.trunc(flat[i * w + k]); return new Column(nm, 'int32', col, false, 'uint32'); });
    return this.table(cols, n);
  }
  async gradient(colors) {
    const rows = colors.map(c => color4(c) || [128, 128, 128, 255]);
    const cols = ['r', 'g', 'b', 'a'].map((ch, k) => new Column(ch, 'int32', rows.map(r => r[k]), false, 'uint8'));
    return this.table(cols, rows.length);
  }
  async attribute(rec, gtype, label) {
    const name = rec.name, loc = rec.location;
    let location;
    if (loc === LOC_VERTICES) location = 'Vertices';
    else if (loc === LOC_SEGMENTS || loc === LOC_FACES || loc === LOC_CELLS) location = 'Primitives';
    else if (loc === 'Categories') location = 'Categories';
    else { this.warn(`${label}/${name}: location ${pyReprStr(loc)} cannot be written`); return null; }
    const rtype = rec.type, vals = rec.values, n = vals.length;
    let data = null;
    if (rtype === 'number') {
      const nums = vals.map(v => (v === null || v === undefined || (typeof v === 'number' && v !== v)) ? null : +v);
      data = { type: 'Number', values: await this.table([new Column('number', 'double', nums, true)], n) };
      const cm = await this.colormap(rec.colormap);
      if (cm) data.colormap = cm;
    } else if (rtype === 'text') {
      data = { type: 'Text', values: await this.table([new Column('text', 'byte_array', vals.map(v => v === null || v === undefined ? null : String(v)), true, 'string')], n) };
    } else if (rtype === 'boolean') {
      data = { type: 'Boolean', values: await this.table([new Column('bool', 'boolean', vals.map(v => v === null || v === undefined ? null : !!v), true)], n) };
    } else if (rtype === 'datetime') {
      const micros = vals.map(epochMicrosFromIso);
      data = { type: 'Number', values: await this.table([new Column('number', 'int64', micros, true, 'timestamp_micros')], n) };
    } else if (rtype === 'vector') {
      const dim = rec.dim || 3;
      const present = vals.map(v => v !== null && v !== undefined);
      const comps = 'xyz'.slice(0, dim).split('').map((ch, k) => new Column(ch, 'double', vals.map(v => v === null || v === undefined ? 0.0 : +v[k])));
      data = { type: 'Vector', values: await this.table([new Group('vector', comps, true, present)], n) };
    } else if (rtype === 'color') {
      const rows = vals.map(color4);
      const present = rows.map(r => r !== null);
      const comps = ['r', 'g', 'b', 'a'].map((ch, k) => new Column(ch, 'int32', rows.map(r => r === null ? 0 : r[k]), false, 'uint8'));
      data = { type: 'Color', values: await this.table([new Group('color', comps, true, present)], n) };
    } else if (rtype === 'category') {
      const names = Array.from(rec.names || [], String);
      const idx = vals.map(i => i === null || i === undefined ? null : Math.trunc(i));
      const top = Math.max(-1, ...idx.filter(i => i !== null));
      while (names.length <= top) names.push(`category ${names.length}`);
      data = { type: 'Category', values: await this.table([new Column('index', 'int32', idx, true, 'uint32')], n), names: await this.table([new Column('name', 'byte_array', names, false, 'string')], names.length) };
      const colors = rec.colors;
      if (colors && colors.length === names.length) data.gradient = await this.gradient(colors);
      const subs = [];
      for (const [sname, svalsRaw] of Object.entries(rec.sub || {})) {
        const svals = Array.from(svalsRaw);
        if (svals.length !== names.length) continue;
        const numeric = svals.every(v => v === null || v === undefined || typeof v === 'number');
        const srec = record(sname, 'Categories', numeric ? 'number' : 'text', svals.map(v => numeric ? numOf(v) : (v === null || v === undefined ? null : String(v))));
        if (numeric) srec.values = srec.values.map(v => v !== v ? null : v);
        const sa = await this.attribute(srec, 'Categories', label + '/' + name);
        if (sa) subs.push(sa);
      }
      if (subs.length) data.attributes = subs;
    } else { this.warn(`${label}/${name}: attribute type ${pyReprStr(rtype)} not written`); return null; }
    const att = { name, location, data };
    if (rec.description) att.description = rec.description;
    if (rec.units) att.units = rec.units;
    return att;
  }
  async colormap(cm) {
    if (!cm || !cm.gradient) return null;
    try {
      if (cm.type === 'continuous') {
        const [lo, hi] = cm.range || [null, null];
        if (lo === null || lo === undefined || hi === null || hi === undefined) return null;
        return { type: 'Continuous', range: { min: +lo, max: +hi }, gradient: await this.gradient(cm.gradient) };
      }
      if (cm.type === 'discrete') {
        const bounds = cm.boundaries || [];
        if (cm.gradient.length !== bounds.length + 1) return null;
        const tbl = await this.table([new Column('value', 'double', bounds.map(b => +b[0])), new Column('inclusive', 'boolean', bounds.map(b => !!b[1]))], bounds.length);
        return { type: 'Discrete', boundaries: tbl, gradient: await this.gradient(cm.gradient) };
      }
    } catch (e) { if (!(e instanceof TypeError || e instanceof RangeError || e instanceof FormatError)) throw e; return null; }
    return null;
  }
  async element(obj, name) {
    const opacity = obj.opacity === undefined || obj.opacity === null ? 1.0 : +obj.opacity;
    const color = color4((obj.color || [160, 160, 160]).slice(0, 3).concat([pyRound(255 * (opacity || 1.0))]));
    const el = { name, color };
    const desc = obj.metadata && obj.metadata.description !== undefined ? String(obj.metadata.description) : '';
    if (desc) el.description = desc;
    const elMeta = {};
    if (obj.metadata && obj.metadata.omf_metadata && typeof obj.metadata.omf_metadata === 'object') Object.assign(elMeta, obj.metadata.omf_metadata);
    elMeta[NWMM_KEY] = nwmmMetadata(obj);
    el.metadata = elMeta;
    let recs = collectRecords(obj, this.warn);
    const kind = obj.kind;
    let gtype;
    if (kind === 'points') { el.geometry = { type: 'PointSet', vertices: await this.vertices(obj.xyz) }; gtype = 'PointSet'; }
    else if (kind === 'lineset') { el.geometry = { type: 'LineSet', vertices: await this.vertices(obj.vertices), segments: await this.indexTuples(obj.segments, ['a', 'b']) }; gtype = 'LineSet'; }
    else if (kind === 'mesh') { el.geometry = { type: 'Surface', vertices: await this.vertices(obj.vertices), triangles: await this.indexTuples(obj.triangles, ['a', 'b', 'c']) }; gtype = 'Surface'; }
    else if (kind === 'grid2d') {
      if (obj.nx < 2 || obj.ny < 2) { this.warn(`${name}: grids need at least 2 x 2 nodes for OMF; skipped`); return null; }
      const [au, av] = rotationAxes(obj.rotation);
      const geom = { type: 'GridSurface', orient: { origin: [obj.x0, obj.y0, 0.0], u: au, v: av }, grid: { type: 'Regular', size: [obj.dx, obj.dy], count: [obj.nx - 1, obj.ny - 1] } };
      if (obj.role === 'property') recs = [record(name, LOC_VERTICES, 'number', Array.from(obj.values), { units: obj.units })];
      else geom.heights = await this.scalars(obj.values);
      el.geometry = geom;
      gtype = 'GridSurface';
    } else if (kind === 'blockmodel') {
      const [au, av, aw] = azimuthAxes(obj.azimuth);
      el.geometry = { type: 'BlockModel', orient: { origin: obj.origin.slice(), u: au, v: av, w: aw }, grid: { type: 'Regular', size: obj.blockSize.slice(), count: obj.count.slice() } };
      gtype = 'BlockModel';
    } else if (kind === 'drillholes') return this.element(obj.tracesLineSet(), name + ' traces');
    else { this.warn(`${name}: object kind ${pyReprStr(kind)} has no OMF v2 equivalent; skipped`); return null; }
    const allowed = { PointSet: [LOC_VERTICES], LineSet: [LOC_VERTICES, LOC_SEGMENTS], Surface: [LOC_VERTICES, LOC_FACES], GridSurface: [LOC_VERTICES, LOC_FACES], BlockModel: [LOC_CELLS] }[gtype];
    const atts = [];
    const anames = uniqueNames(recs.map(r => r.name), 'attribute');
    for (let k = 0; k < recs.length; k++) {
      const rec = recs[k];
      if (!allowed.includes(rec.location)) { this.warn(`${name}/${rec.name}: location ${pyReprStr(rec.location)} is not valid on a ${gtype}; skipped`); continue; }
      const att = await this.attribute(Object.assign({}, rec, { name: anames[k] }), gtype, name);
      if (att) atts.push(att);
    }
    if (atts.length) el.attributes = atts;
    return el;
  }
}
function crsString(project, crs) {
  if (crs) return crs;
  if (project === null) return '';
  const c = project.crs || {};
  if (c.crs_string) return c.crs_string;
  if (c.epsg) return `EPSG:${Math.trunc(c.epsg)}`;
  return '';
}
function dateString(project) {
  let d = null;
  if (project !== null) d = project.metadata.date || project.created;
  d = d || GM.nowISO();
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.test(String(d))) return String(d);
  const us = epochMicrosFromIso(d);
  return us !== null ? isoFromEpochMicros(us) : GM.nowISO();
}
/** Project | objects -> OMF v2.0 bytes. opts: name, description, crs ('EPSG:32612'), prerelease ('beta.1'; '' for the final tag),
    compression 'gzip' | 'none' (parquet pages), author, units, warnings (array to append to). */
export async function writeOmf2(projectOrObjects, opts = {}) {
  const [project, objects] = objectsOf(projectOrObjects);
  const warns = opts.warnings || [];
  let { name = '', description = '', crs = '', author = '', units = '' } = opts;
  const prerelease = opts.prerelease === undefined ? OMF2_DEFAULT_PRERELEASE : opts.prerelease;
  if (project !== null) {
    name = name || project.name;
    description = description || String(project.metadata.description || '');
    author = author || String(project.metadata.author || '');
    units = units || String(project.metadata.units || '');
    if (!units) { const u = (project.crs || {}).units || ''; units = { m: 'meters', ft: 'feet' }[u] || u || ''; }
  }
  const zip = new ZipWriter();
  const w = new Omf2Writer(zip, m => warns.push(m), opts.compression || 'gzip');
  const names = uniqueNames(objects.map(o => o.name), 'element');
  const elements = [];
  for (let k = 0; k < objects.length; k++) { const el = await w.element(objects[k], names[k]); if (el) elements.push(el); }
  const index = { name: name || 'model' };
  if (description) index.description = description;
  const crsS = crsString(project, crs);
  if (crsS) index.coordinate_reference_system = crsS;
  if (units) index.units = units;
  if (author) index.author = author;
  index.application = APPLICATION;
  index.date = dateString(project);
  if (project !== null && project.metadata.omf_metadata && typeof project.metadata.omf_metadata === 'object') index.metadata = project.metadata.omf_metadata;
  index.elements = elements;
  zip.add(OMF2_INDEX, await gzip(utf8(JSON.stringify(index))));
  const out = zip.finish(omf2FormatComment(prerelease));
  mergeWarnings(project, warns);
  return out;
}

/* ========================================================================
   registry — FORMATS, sniff(), readAny(), writeAs()
   ======================================================================== */
function pick(objects, kinds, what) {
  const list = objects instanceof GM.Project ? objects.objects : (Array.isArray(objects) ? objects : [objects]);
  for (const o of list) if (o && kinds.includes(o.kind)) return o;
  throw new FormatError(`${what}: needs a ${kinds.join(' / ')} object`);
}
function listOf(objects) { return objects instanceof GM.Project ? objects.objects.slice() : (Array.isArray(objects) ? objects.slice() : [objects]); }
function baseName(objects, opts, fallback) {
  if (opts && opts.basename) return opts.basename;
  if (objects instanceof GM.Project) return GM.slug(objects.name || fallback);
  const list = listOf(objects);
  return GM.slug(list.length && list[0] && list[0].name ? list[0].name : fallback);
}

/** id -> {name, exts, kinds, read(bytes, opts) -> object | [objects] | dict, write(objects, opts) -> bytes | {filename: bytes}} */
export const FORMATS = {
  surfer_grd: { name: 'Golden Software Surfer grid (DSAA ascii / DSBB Surfer 6 / DSRB Surfer 7)', exts: ['.grd'], kinds: ['grid2d'],
    read: async (b, o) => readSurferGrd(b, o), write: async (x, o) => writeSurferGrd(pick(x, ['grid2d'], 'surfer_grd'), o) },
  surfer_bln: { name: 'Surfer blanking / breakline polylines', exts: ['.bln'], kinds: ['lineset'],
    read: async (b, o) => readBln(b, o), write: async (x) => writeBln(pick(x, ['lineset'], 'surfer_bln')) },
  geosoft_grd: { name: 'Geosoft Oasis montaj binary grid (v2, uncompressed + compressed read)', exts: ['.grd'], kinds: ['grid2d'],
    read: (b, o) => readGeosoftGrd(b, o), write: async (x, o) => writeGeosoftGrd(pick(x, ['grid2d'], 'geosoft_grd'), o) },
  gxf: { name: 'Geosoft Grid eXchange File (ASCII, incl. base-90 compressed read)', exts: ['.gxf'], kinds: ['grid2d'],
    read: async (b, o) => readGxf(b, o), write: async (x) => writeGxf(pick(x, ['grid2d'], 'gxf')) },
  geosoft_xyz: { name: 'Geosoft XYZ line database export (channels + Line/Tie headers)', exts: ['.xyz'], kinds: ['points'],
    read: async (b, o) => readGeosoftXyz(b, o), write: async (x, o) => writeGeosoftXyz(pick(x, ['points'], 'geosoft_xyz'), o) },
  arc_ascii: { name: 'Arc/Info ASCII grid', exts: ['.asc', '.txt'], kinds: ['grid2d'],
    read: async (b, o) => readAsc(b, o), write: async (x, o) => writeAsc(pick(x, ['grid2d'], 'arc_ascii'), o) },
  zmap: { name: 'ZMAP+ ASCII grid (Kingdom / Petrel / Landmark)', exts: ['.zmap', '.dat', '.zmp'], kinds: ['grid2d'],
    read: async (b, o) => readZmap(b, o), write: async (x, o) => writeZmap(pick(x, ['grid2d'], 'zmap'), o) },
  irap: { name: 'Irap classic ASCII grid (RMS / Petrel)', exts: ['.irap', '.gri'], kinds: ['grid2d'],
    read: async (b, o) => readIrap(b, o), write: async (x, o) => writeIrap(pick(x, ['grid2d'], 'irap'), o) },
  cps3: { name: 'CPS-3 ASCII grid (read; column direction flagged)', exts: ['.cps3', '.cps'], kinds: ['grid2d'], read: async (b, o) => readCps3(b, o) },
  ubc: { name: 'UBC-GIF 3-D mesh + model files (Geosoft / Leapfrog voxel interchange)', exts: ['.msh'], kinds: ['blockmodel'],
    read: async (b, o) => readUbc(b, o),
    write: async (x, o = {}) => {
      const bm = pick(x, ['blockmodel'], 'ubc');
      const base = baseName(bm, o, 'blockmodel');
      const attrs = o.attributes || (o.attribute ? [o.attribute] : Object.entries(bm.attributes).filter(([, a]) => a.type === 'number').map(([k]) => k));
      const out = {};
      if (!attrs.length) { out[`${base}.msh`] = writeUbc(bm, null, { meshOnly: true, nodata: o.nodata }).msh; return out; }
      for (const a of attrs) { const r = writeUbc(bm, a, o); out[`${base}.msh`] = r.msh; out[attrs.length === 1 ? `${base}.mod` : `${base}_${GM.slug(a)}.mod`] = r.mod; }
      return out;
    } },
  omf1: { name: 'Open Mining Format v0.9 (Leapfrog Geo <= 2024.1)', exts: ['.omf'], kinds: ['points', 'lineset', 'mesh', 'grid2d', 'blockmodel', 'drillholes'],
    read: (b, o) => readOmf1(b, o), write: (x, o) => writeOmf1(x, o) },
  omf2: { name: 'Open Mining Format v2.0 (Leapfrog Geo 2025.1+, Seequent Evo)', exts: ['.omf'], kinds: ['points', 'lineset', 'mesh', 'grid2d', 'blockmodel', 'drillholes'],
    read: (b, o) => readOmf2(b, o), write: (x, o) => writeOmf2(x, o) },
  obj: { name: 'Wavefront OBJ mesh', exts: ['.obj'], kinds: ['mesh'], read: async (b, o) => readObj(b, o), write: async (x, o) => writeObj(pick(x, ['mesh'], 'obj'), o) },
  dxf: { name: 'AutoCAD DXF R12 (3DFACE meshes, POLYLINE 3-D polylines, POINT)', exts: ['.dxf'], kinds: ['mesh', 'lineset', 'points'],
    read: async (b, o) => readDxf(b, o), write: async (x, o) => writeDxf(listOf(x).filter(ob => ['mesh', 'lineset', 'points'].includes(ob.kind)), o) },
  gocad_ts: { name: 'GOCAD TSurf / PLine / VSet ASCII', exts: ['.ts', '.pl', '.vs'], kinds: ['mesh', 'lineset', 'points'],
    read: async (b, o) => readGocad(b, o),
    write: async (x, o = {}) => {
      const list = listOf(x).filter(ob => ['mesh', 'lineset', 'points'].includes(ob.kind));
      if (!list.length) throw new FormatError('gocad_ts: needs a mesh / lineset / points object');
      if (list.length === 1) return writeGocad(list[0], o);
      const out = {}, used = new Set();
      for (const ob of list) {
        let nm = `${GM.slug(ob.name)}.${{ mesh: 'ts', lineset: 'pl', points: 'vs' }[ob.kind]}`;
        let k = 1; while (used.has(nm)) { k++; nm = nm.replace(/(\.\w+)$/, `-${k}$1`); }
        used.add(nm); out[nm] = writeGocad(ob, o);
      }
      return out;
    } },
  lf_msh: { name: 'Leapfrog binary mesh (.msh, community-documented layout)', exts: ['.msh'], kinds: ['mesh'], read: async (b, o) => readLfMsh(b, o), write: async (x) => writeLfMsh(pick(x, ['mesh'], 'lf_msh')) },
  csv_points: { name: 'Point table CSV (x,y,z + columns; Leapfrog Points import)', exts: ['.csv'], kinds: ['points'],
    read: async (b, o) => readPointsCsv(b, o), write: async (x, o) => writePointsCsv(pick(x, ['points'], 'csv_points'), o) },
  csv_drillholes: { name: 'Drillhole collar / survey / interval CSV set', exts: ['.csv'], kinds: ['drillholes'],
    read: async (b, o = {}) => readDrillholes({ collar: b, survey: o.survey, intervals: o.intervals }, o),
    write: async (x, o = {}) => { const dh = pick(x, ['drillholes'], 'csv_drillholes'); const base = baseName(dh, o, 'drillholes'); const files = writeDrillholes(dh); const out = {}; for (const [k, v] of Object.entries(files)) out[`${base}_${k}`] = v; return out; } },
  csv_structural: { name: 'Planar structural data CSV (x,y,z,dip,dip_azimuth,polarity)', exts: ['.csv'], kinds: ['points'],
    read: async (b, o) => readStructuralCsv(b, o), write: async (x) => writeStructuralCsv(pick(x, ['points'], 'csv_structural')) },
  csv_blockmodel: { name: 'Block-model CSV (centroids + sizes; Leapfrog import/export style)', exts: ['.csv'], kinds: ['blockmodel'],
    read: async (b, o) => readBlockmodelCsv(b, o),
    write: async (x, o = {}) => { const bm = pick(x, ['blockmodel'], 'csv_blockmodel'); const base = baseName(bm, o, 'blockmodel'); const r = writeBlockmodelCsv(bm, o); const out = { [`${base}.csv`]: r.csv }; if (r.txt) out[`${base}.csv.txt`] = r.txt; return out; } },
  segy: { name: 'SEG-Y rev 0/1 seismic / GPR / resistivity section', exts: ['.sgy', '.segy'], kinds: ['imageplane', 'points'],
    read: async (b, o) => readSegy(b, o), write: async (x, o = {}) => { const d = x && x.samples ? x : (listOf(x).find(ob => ob && ob.samples) || null); if (!d) throw new FormatError('segy: needs {samples, dt_us, coords}'); return writeSegy(d.samples, Object.assign({ dt_us: d.dt !== undefined ? d.dt * 1e6 : undefined, coords: d.coords }, o)); } },
  las: { name: 'CWLS LAS 2.0 well log', exts: ['.las'], kinds: ['drillholes'],
    read: async (b, o) => readLas(b, o), write: async (x) => { const d = x && x.curves ? x : (listOf(x).find(ob => ob && ob.curves) || null); if (!d) throw new FormatError('las: needs a readLas-style dict (curves + data)'); return writeLas(d); } },
};
const FORMAT_EXT = { surfer_grd: '.grd', surfer_bln: '.bln', geosoft_grd: '.grd', gxf: '.gxf', geosoft_xyz: '.xyz', arc_ascii: '.asc', zmap: '.zmap', irap: '.irap', cps3: '.cps3', omf1: '.omf', omf2: '.omf',
  obj: '.obj', dxf: '.dxf', lf_msh: '.msh', csv_points: '.csv', csv_structural: '.csv', segy: '.sgy', las: '.las' };

export function formatsForExtension(ext) { ext = String(ext || '').toLowerCase(); return Object.keys(FORMATS).filter(k => FORMATS[k].exts.includes(ext)); }
function extOf(name) { const base = String(name || '').split(/[\\/]/).pop(); const k = base.lastIndexOf('.'); return k > 0 ? base.slice(k).toLowerCase() : ''; }
function lstripBytes(head) { let i = 0; while (i < head.length && (head[i] === 32 || head[i] === 9 || head[i] === 10 || head[i] === 13 || head[i] === 11 || head[i] === 12)) i++; return head.subarray(i); }

/** Best-effort format id from magic bytes / extension (same rules as formats/__init__.py sniff()). */
export function sniff(filename, bytes) {
  const head = bytes ? toU8(bytes).subarray(0, 1024) : new Uint8Array(0);
  const ext = extOf(filename);
  if (head.length >= 4 && head[0] === 0x84 && head[1] === 0x83 && head[2] === 0x82 && head[3] === 0x81) return 'omf1';
  if (bytesEq(head, 0, 'PK') && ext === '.omf') return 'omf2';
  if (bytesEq(head, 0, 'DSAA') || bytesEq(head, 0, 'DSBB') || bytesEq(head, 0, 'DSRB')) return 'surfer_grd';
  if (bytesEq(head, 0, '%%ARANZ')) return 'lf_msh';
  const ls = lstripBytes(head);
  if (bytesEq(ls, 0, 'GOCAD')) return 'gocad_ts';
  if (ext === '.gxf' || bytesEq(ls, 0, '#TITLE') || bytesEq(ls, 0, '#POINTS')) return 'gxf';
  if (ext === '.sgy' || ext === '.segy') return 'segy';
  if (ext === '.las' || bytesEq(ls, 0, '~V')) return 'las';
  if (ext === '.dxf' || bytesEq(ls, 0, '0\r\nSECTIO') || bytesEq(ls, 0, '0\nSECTION')) return 'dxf';
  if (ext === '.obj') return 'obj';
  if (ext === '.grd') {
    if (head.length >= 8) { const es = new DataView(head.buffer, head.byteOffset, 4).getInt32(0, true); if ([1, 2, 4, 8, 1025, 1026, 1028, 1032].includes(es)) return 'geosoft_grd'; }
    return 'surfer_grd';
  }
  if (ext === '.asc') {
    const txt = decodeLatin1(ls.subarray(0, 16)).toLowerCase();
    if (txt.startsWith('ncols') || txt.startsWith('nrows') || txt.startsWith('xllcorner')) return 'arc_ascii';
  }
  if (ext === '.zmap' || ext === '.zmp' || ls[0] === 0x21 || ls[0] === 0x40) { if (head.indexOf(0x40) >= 0) return 'zmap'; }
  if (bytesEq(ls, 0, '-996')) return 'irap';
  if (bytesEq(ls, 0, 'FSASCI')) return 'cps3';
  if (ext === '.msh') return 'ubc';
  if (ext === '.xyz') return 'geosoft_xyz';
  if (ext === '.csv') return 'csv_points';
  return null;
}

async function fileBytes(file) {
  if (file instanceof Uint8Array) return file;
  if (file instanceof ArrayBuffer) return new Uint8Array(file);
  if (file && typeof file.arrayBuffer === 'function') return new Uint8Array(await file.arrayBuffer());   // File / Blob
  if (file && file.bytes !== undefined && typeof file.bytes !== 'function') return toU8(file.bytes);  // {name, bytes}
  return toU8(file);
}
function looksLonLat(xyz) {
  let any = false;
  for (let k = 0; k + 1 < xyz.length; k += 3) { const x = xyz[k], y = xyz[k + 1]; if (x !== x || y !== y) continue; any = true; if (!GM.utm.looksLonLat(x, y)) return false; }
  return any;
}
/* Convert lon/lat-looking PointSet / Drillholes coordinates to UTM when opts.crs is a UTM crs. */
function lonLatToUtm(objects, opts, warnings) {
  const crs = opts.crs;
  if (!crs || crs.kind !== 'utm' || opts.assumeLonLat === false) return;
  const label = `converted lon/lat to UTM zone ${crs.zone}${crs.north === false ? 'S' : 'N'}`;
  for (const obj of objects) {
    if (obj.kind === 'points') {
      if (!(opts.assumeLonLat || looksLonLat(obj.xyz))) continue;
      for (let k = 0; k + 1 < obj.xyz.length; k += 3) { const [e, n] = GM.utm.fwd(obj.xyz[k], obj.xyz[k + 1], crs.zone, crs.north !== false); obj.xyz[k] = e; obj.xyz[k + 1] = n; }
      obj.warn(label); warnings.push(label);
    } else if (obj.kind === 'drillholes') {
      const flat = []; for (const c of obj.collars) flat.push(+c.x, +c.y, 0);
      if (!(opts.assumeLonLat || looksLonLat(flat))) continue;
      for (const c of obj.collars) { const [e, n] = GM.utm.fwd(+c.x, +c.y, crs.zone, crs.north !== false); c.x = e; c.y = n; }
      obj.warn(label); warnings.push(label);
    }
  }
}
const LAS_X_KEYS = ['X', 'XCOORD', 'XWELL', 'EASTING', 'EAST', 'LONG', 'LON', 'LONGI', 'SLON', 'XLOC'];
const LAS_Y_KEYS = ['Y', 'YCOORD', 'YWELL', 'NORTHING', 'NORTH', 'LATI', 'LAT', 'SLAT', 'YLOC'];
const LAS_Z_KEYS = ['ELEV', 'EKB', 'EGL', 'KB', 'GL', 'ELEVATION', 'RL', 'EDF', 'APD', 'SELEV', 'ZLOC'];
function lasWellValue(well, keys) {
  for (const k of keys) if (well[k] && well[k].value !== undefined) { const v = pyFloat(well[k].value); if (v !== undefined && v === v) return v; }
  return null;
}
/** LAS dict -> Drillholes with one collar (x/y/z from the ~Well section when present) and intervals.las rows. */
export function lasToDrillholes(d, opts = {}) {
  const warnings = d.warnings.slice();
  const hole = (d.well.WELL && pyStrip(d.well.WELL.value)) || opts.name || stem(opts.file, 'well');
  const x = lasWellValue(d.well, LAS_X_KEYS), y = lasWellValue(d.well, LAS_Y_KEYS), z = lasWellValue(d.well, LAS_Z_KEYS);
  if (x === null || y === null) warnings.push('no collar coordinates in the ~Well section; collar placed at (0, 0)');
  const rows = lasToIntervals(d, hole, opts.step === undefined ? null : opts.step, opts.curves === undefined ? null : opts.curves);
  let depth = null;
  for (const r of rows) if (depth === null || r.to > depth) depth = r.to;
  const dh = new GM.Drillholes({ name: opts.name || stem(opts.file, hole), collars: [{ hole, x: x === null ? 0.0 : x, y: y === null ? 0.0 : y, z: z === null ? 0.0 : z, depth }], surveys: [], intervals: { las: rows } });
  dh.metadata.las = { version: d.version, curves: d.curves, well: d.well, null: d.null, n_rows: d.n_rows, index_unit: d.index_unit };
  dh.metadata.dip_convention = 'positive down';
  dh.metadata.warnings = warnings;
  return dh;
}
/** SEG-Y dict -> [ImagePlane (section image, PNG data URL), PointSet (trace positions)]. */
export async function segyToObjects(d, opts = {}) {
  const img = segySectionImage(d, opts);
  const warnings = d.warnings.concat(img.warnings);
  const name = opts.name || stem(opts.file, 'section');
  const out = [];
  if (img.width && img.height) {
    const png = await encodePNG(img.width, img.height, img.gray, { channels: 1 });
    // degenerate coordinates (no / identical trace positions): lay the section out eastwards, one unit per trace
    const p1 = img.p1 || [0, 0], p2 = (img.p2 && !(img.p1[0] === img.p2[0] && img.p1[1] === img.p2[1])) ? img.p2 : [p1[0] + img.width, p1[1]];
    const plane = new GM.ImagePlane({ image: pngDataUrl(png), width: img.width, height: img.height, plane: 'section', p1: p1.slice(0, 2), p2: p2.slice(0, 2), z_top: img.z_top, z_bottom: img.z_bottom, name });
    plane.metadata.segy = { n_traces: d.n_traces, ns: d.ns, dt: d.dt, format: d.format, endian: d.endian, revision: d.revision, text_encoding: d.text_encoding, text_header: d.text_header, binary_header: d.binary_header, clip: img.clip };
    plane.metadata.warnings = warnings;
    out.push(plane);
  }
  if (d.coords.some(c => c[0] || c[1])) {
    const xyz = new Float64Array(3 * d.n_traces);
    const attrs = { trace: [], cdp: [], inline: [], xline: [], sp: [] };
    d.trace_headers.forEach((th, k) => { xyz[3 * k] = d.coords[k][0]; xyz[3 * k + 1] = d.coords[k][1]; xyz[3 * k + 2] = img.z_top; attrs.trace.push(k + 1); attrs.cdp.push(th.cdp); attrs.inline.push(th.inline); attrs.xline.push(th.xline); attrs.sp.push(th.sp); });
    const ps = new GM.PointSet({ name: `${name} traces`, role: 'points', xyz, attributes: attrs });
    ps.metadata.warnings = warnings.slice();
    out.push(ps);
  }
  return out;
}

const TABLE_FORMATS = { points: 'csv_points', drillholes: 'csv_drillholes', structural: 'csv_structural', blockmodel: 'csv_blockmodel' };
/** Read any supported file -> {format, objects, warnings, project?}.  `file` is a File or {name, bytes}.
    opts: format (override sniffing), table 'points'|'drillholes'|'structural'|'blockmodel' for any delimited table, x/y/z column overrides,
    survey / intervals (drillhole side tables), models (UBC), crs {kind:'utm', zone, north} + assumeLonLat, zTop/zBottom/clipPct (SEG-Y). */
export async function readAny(file, opts = {}) {
  const name = (file && file.name) || opts.name || '';
  const bytes = await fileBytes(file);
  // opts.table is the table kind the import dialog was *told* by the user, so it
  // outranks a sniff() result that came only from the extension: sniff returns null
  // for a delimited .txt/.dat and 'geosoft_xyz' for every .xyz, and gating the
  // override on the guess threw those files out before they were read.
  // It must NOT outrank a sniff() that recognised the *content*.  Surfer, GOCAD,
  // GXF, Irap, CPS3, LAS and DXF are all identified by their leading bytes whatever
  // they are named, and the dialog's kind selector defaults to 'points' — so a
  // Surfer grid called topo.dat would otherwise be forced through the CSV reader and
  // die on "no X / Y columns found in ['DSAA']" when it used to import cleanly.
  // An explicit opts.format still beats both: it names a reader outright.
  const sniffed = sniff(name, bytes);
  const guessedFromExtensionOnly = sniffed === null || sniffed === 'csv_points' || sniffed === 'geosoft_xyz';
  let format = opts.format || (guessedFromExtensionOnly && opts.table && TABLE_FORMATS[opts.table]) || sniffed;
  if (!format) throw new FormatError(`cannot determine the format of ${name || 'the file'}`);
  const ro = Object.assign({}, opts, { file: name });
  let objects = [], project = null;
  const warnings = [];
  if (format === 'omf1' || format === 'omf2') {
    project = format === 'omf1' ? await readOmf1(bytes, ro) : await readOmf2(bytes, ro);
    objects = project.objects.slice();
    for (const w of project.metadata.warnings || []) warnings.push(w);
  } else if (format === 'segy') {
    objects = await segyToObjects(readSegy(bytes, ro), ro);
  } else if (format === 'las') {
    objects = [lasToDrillholes(readLas(bytes, ro), ro)];
  } else if (format === 'ubc') {
    objects = [readUbc(bytes, ro)];
  } else if (format === 'csv_drillholes') {
    objects = [readDrillholes({ collar: bytes, survey: opts.survey, intervals: opts.intervals }, ro)];
  } else {
    const fmt = FORMATS[format];
    if (!fmt || !fmt.read) throw new FormatError(`no reader for format ${format}`);
    const r = await fmt.read(bytes, ro);
    objects = Array.isArray(r) ? r : [r];
  }
  for (const obj of objects) {
    if (!obj.provenance) obj.provenance = {};
    if (!obj.provenance.format) obj.provenance.format = format;
    if (name && !obj.provenance.file) obj.provenance.file = name;
  }
  if (['csv_points', 'csv_drillholes', 'csv_structural', 'geosoft_xyz', 'las'].includes(format)) lonLatToUtm(objects, opts, warnings);
  for (const obj of objects) for (const w of (obj.metadata && obj.metadata.warnings) || []) if (!warnings.includes(w)) warnings.push(w);
  const out = { format, objects, warnings };
  if (project) out.project = project;
  return out;
}

/** Write objects (object | array | Project) in a format -> {filename: bytes}. */
export async function writeAs(formatId, objects, opts = {}) {
  const fmt = FORMATS[formatId];
  if (!fmt || !fmt.write) throw new FormatError(`no writer for format ${formatId}`);
  const result = await fmt.write(objects, opts);
  if (result instanceof Uint8Array || typeof result === 'string') {
    let base;
    if (formatId === 'omf1' || formatId === 'omf2' || formatId === 'dxf') base = baseName(objects, opts, 'model');
    else if (formatId === 'gocad_ts') base = baseName(listOf(objects).filter(o => ['mesh', 'lineset', 'points'].includes(o.kind))[0] || objects, opts, 'model');
    else if (formatId === 'segy') base = opts.basename || 'section';
    else if (formatId === 'las') base = opts.basename || 'log';
    else base = baseName(pick(objects, fmt.kinds, formatId), opts, formatId);
    let ext = FORMAT_EXT[formatId] || '';
    if (formatId === 'gocad_ts') { const o = listOf(objects).find(ob => ['mesh', 'lineset', 'points'].includes(ob.kind)); ext = '.' + ({ mesh: 'ts', lineset: 'pl', points: 'vs' }[o ? o.kind : 'mesh']); }
    return { [base + ext]: result };
  }
  return result;
}
