#!/usr/bin/env node
/* test_gm_formats.mjs — validates site/assets/geomodel/gm-formats.js against the Python
   reference implementation (pipelines/geomodel/formats) and the external reference
   readers available on this machine (omf-rust `omf2` wheel, pyarrow, omf 1.0.1, PIL).

     node tools/test_gm_formats.mjs            regenerate fixtures (python3) and run everything
     node tools/test_gm_formats.mjs --no-gen   reuse /tmp/gm_fixtures from a previous run
     node tools/test_gm_formats.mjs --filter=segy   only tests whose name contains 'segy'

   Direction 1: tools/gen_gm_fixtures.py writes every format with the Python writers and
   records what the Python readers see (expected.json); the JS readers must see the same
   (NaN == None, numeric tolerances, float32 mesh attributes at 1e-5).
   Direction 2: the JS writers write the objects back; the Python readers must see the same
   thing again, and — where the format is deterministic — the bytes must be identical to the
   Python-written fixture.
   Plus: omf-rust / pyarrow / omf 1.0.1 on the JS-written OMF files, PIL on encodePNG output,
   python zipfile on the ZIP writer, Python sniff() parity, repr()/printf parity.
   Exits 1 when anything fails. */

import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
import * as GM from '../site/assets/geomodel/gm-core.js';
import * as F from '../site/assets/geomodel/gm-formats.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const GEN = path.join(HERE, 'gen_gm_fixtures.py');
const OUT = '/tmp/gm_fixtures';
const JSOUT = path.join(OUT, 'js');
const NO_GEN = process.argv.includes('--no-gen');
const FILTER = (process.argv.find(a => a.startsWith('--filter=')) || '').slice(9);
const OMF1_PYTHON = '/tmp/omfenv/bin/python';

/* ------------------------------------------------------------------ python */
function pyRun(args, input) {
  const r = spawnSync('python3', args, { input, encoding: 'utf8', maxBuffer: 1 << 30 });
  if (r.status !== 0) throw new Error('python failed: ' + (r.stderr || '').slice(-2000));
  return r.stdout;
}
function pySummarize(format, file, opts = {}) {
  return JSON.parse(pyRun([GEN, 'summarize', format, file, JSON.stringify(opts)]));
}
const PY_PRELUDE = `
import sys, json, math, io, os
sys.path.insert(0, ${JSON.stringify(path.join(ROOT, 'pipelines'))})
def J(o):
    if isinstance(o, float): return None if (o != o or o in (math.inf, -math.inf)) else o
    if isinstance(o, dict): return {str(k): J(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [J(v) for v in o]
    if isinstance(o, bytes): return o.decode('latin-1')
    return o
out = None
`;
function py(code) {
  const r = spawnSync('python3', ['-c', PY_PRELUDE + code + '\nprint(json.dumps(J(out)))\n'], { encoding: 'utf8', maxBuffer: 1 << 30 });
  if (r.status !== 0) throw new Error('python failed:\n' + (r.stderr || '').slice(-3000));
  const lines = r.stdout.trim().split('\n');
  try { return JSON.parse(lines[lines.length - 1]); } catch (e) { throw new Error('bad python output: ' + r.stdout.slice(0, 400)); }
}

/* ---------------------------------------------------------------- compare */
const list = a => Array.from(a, v => (v === null || v === undefined || (typeof v === 'number' && v !== v)) ? null : (typeof v === 'bigint' ? Number(v) : v));
function fsumJs(values) { return F.fsum(Array.from(values).filter(v => v === v && v !== null)); }
function nnanJs(values) { let c = 0; for (const v of values) if (v !== v || v === null) c++; return c; }
function slice(a, n) { return Array.prototype.slice.call(a, 0, n); }
const JV = v => (typeof v === 'number' && !isFinite(v)) ? null : v;

/** JS twin of gen_gm_fixtures.summarize(). */
function summarize(obj) {
  if (obj instanceof GM.Project) {
    const md = {};
    for (const [k, v] of Object.entries(obj.metadata)) if (k !== 'warnings') md[k] = v;
    return { project: true, name: obj.name, crs: obj.crs, metadata: md, warnings: (obj.metadata.warnings || []).slice(), objects: obj.objects.map(summarize) };
  }
  if (Array.isArray(obj)) return obj.map(summarize);
  if (!obj.kind) return obj;
  const k = obj.kind;
  const base = { kind: k, name: obj.name, color: obj.color.slice(), role: obj.role === undefined ? null : obj.role, group: obj.group, opacity: obj.opacity,
    provenance_format: obj.provenance.format || null, warnings: (obj.metadata.warnings || []).slice() };
  const md = {};
  for (const [kk, v] of Object.entries(obj.metadata)) if (kk !== 'warnings' && kk !== 'extra_arrays' && !(v instanceof Uint8Array)) md[kk] = v;
  if ('property_of' in md) md.property_of = true;
  base.metadata = md;
  if (k === 'grid2d') {
    Object.assign(base, { nx: obj.nx, ny: obj.ny, x0: obj.x0, y0: obj.y0, dx: obj.dx, dy: obj.dy, rotation: obj.rotation, units: obj.units, values: list(obj.values), n_nan: nnanJs(obj.values) });
  } else if (k === 'mesh') {
    const v = obj.vertices, t = obj.triangles;
    const strided = o => { const out = []; for (let i = o; i < v.length; i += 3) out.push(v[i]); return out; };
    const attrs = {};
    for (const [n, a] of Object.entries(obj.attributes)) attrs[n] = { location: a.location || 'vertices', n: a.values.length, sum: fsumJs(a.values), n_nan: nnanJs(a.values), first: list(slice(a.values, 8)) };
    Object.assign(base, { nv: obj.nVertices, nt: obj.nTriangles, vsum: [fsumJs(strided(0)), fsumJs(strided(1)), fsumJs(strided(2))], tsum: Array.from(t).reduce((a, b) => a + b, 0),
      v_first: list(slice(v, 9)), t_first: slice(t, 9), bounds: obj.bounds() ? list(obj.bounds()) : null, attributes: attrs });
  } else if (k === 'lineset') {
    const v = obj.vertices;
    const strided = o => { const out = []; for (let i = o; i < v.length; i += 3) out.push(v[i]); return out; };
    const attrs = {};
    for (const [n, a] of Object.entries(obj.attributes || {})) attrs[n] = { location: a.location || 'vertices', n: a.values.length, sum: fsumJs(a.values), first: list(slice(a.values, 8)) };
    const feats = obj.features.filter(f => f && Object.keys(f).length);
    const explicit = feats.length > 0;
    Object.assign(base, { nv: obj.nVertices, nseg: obj.segments.length, vsum: [fsumJs(strided(0)), fsumJs(strided(1)), fsumJs(strided(2))], seg_first: slice(obj.segments, 12),
      parts_len: explicit ? obj.parts.map(p => p.length) : null, parts_first: explicit && obj.parts.length ? obj.parts[0].slice(0, 10) : [], features_first: feats.slice(0, 5), attributes: attrs });
  } else if (k === 'points') {
    const attrs = {};
    for (const [n, colRaw] of Object.entries(obj.attributes)) {
      const col = Array.from(colRaw);
      const isNum = v => typeof v === 'number';
      const numeric = col.every(v => v === null || v === undefined || isNum(v)) && col.some(isNum);
      const entry = { n: col.length, first: list(col.slice(0, 8)) };
      if (numeric) entry.sum = fsumJs(col.map(v => v === null || v === undefined ? NaN : v));
      attrs[n] = entry;
    }
    const xyz = obj.xyz;
    const strided = o => { const out = []; for (let i = o; i < xyz.length; i += 3) out.push(xyz[i]); return out; };
    Object.assign(base, { n: obj.n, xyz_first: list(slice(xyz, 9)), xyz_sum: [fsumJs(strided(0)), fsumJs(strided(1)), fsumJs(strided(2))], attributes: attrs });
  } else if (k === 'blockmodel') {
    const attrs = {};
    for (const [n, a] of Object.entries(obj.attributes)) {
      const vals = Array.from(a.values);
      const entry = { type: a.type, n: vals.length, first: list(vals.slice(0, 12)) };
      if (a.type === 'number') { entry.sum = fsumJs(vals); entry.n_nan = nnanJs(vals); }
      attrs[n] = entry;
    }
    Object.assign(base, { origin: obj.origin.slice(), block_size: obj.blockSize.slice(), count: obj.count.slice(), azimuth: obj.azimuth, attributes: attrs });
  } else if (k === 'drillholes') {
    Object.assign(base, { collars: obj.collars, surveys: obj.surveys, intervals: obj.intervals });
  }
  return JSON.parse(JSON.stringify(base, (kk, v) => JV(v)));
}
function segySummary(d) {
  return { n_traces: d.n_traces, ns: d.ns, dt: d.dt, format: d.format, endian: d.endian, revision: d.revision, text_encoding: d.text_encoding,
    text_first: d.text_header.split('\n').slice(0, 3), text_lines: d.text_header.split('\n').length, binary_header: d.binary_header, trace_headers: d.trace_headers.slice(0, 4),
    coords: d.coords.map(c => c.slice()), samples: d.samples.map(s => list(s)), warnings: d.warnings };
}
function lasSummary(d) {
  const data = {};
  for (const [k, v] of Object.entries(d.data)) data[k] = list(v);
  return { version: d.version, wrap: d.wrap, delimiter: d.delimiter, well: d.well, curves: d.curves, params: d.params, other: d.other, data, index_unit: d.index_unit,
    null: d.null, n_rows: d.n_rows, sections: d.sections, warnings: d.warnings };
}

/* Tolerances by summary path: float32 mesh / lineset attribute storage and SEG-Y samples get 1e-5. */
function tolFor(p) {
  if (/\/(attributes)\/[^/]+\/(sum|first)/.test(p) && /^\/?(objects\/\d+\/)?(mesh|lineset)?/.test(p)) return { rel: 1e-5, abs: 1e-5 };
  if (/samples/.test(p)) return { rel: 1e-5, abs: 1e-6 };
  return { rel: 1e-9, abs: 1e-9 };
}
function compare(js, py, p = '', out = [], ctx = {}) {
  const isNull = v => v === null || v === undefined || (typeof v === 'number' && v !== v);
  if (isNull(js) || isNull(py)) { if (isNull(js) !== isNull(py)) out.push(`${p}: js=${JSON.stringify(js)} py=${JSON.stringify(py)}`); return out; }
  if (typeof js === 'number' || typeof py === 'number') {
    if (typeof js !== 'number' || typeof py !== 'number') { out.push(`${p}: js=${JSON.stringify(js)} py=${JSON.stringify(py)}`); return out; }
    const t = ctx.tol || tolFor(p);
    if (Math.abs(js - py) > t.abs + t.rel * Math.max(Math.abs(js), Math.abs(py))) out.push(`${p}: js=${js} py=${py}`);
    return out;
  }
  if (typeof js === 'boolean' || typeof py === 'boolean' || typeof js === 'string' || typeof py === 'string') { if (js !== py) out.push(`${p}: js=${JSON.stringify(js)} py=${JSON.stringify(py)}`); return out; }
  if (Array.isArray(js) || Array.isArray(py)) {
    if (!Array.isArray(js) || !Array.isArray(py)) { out.push(`${p}: js=${JSON.stringify(js).slice(0, 80)} py=${JSON.stringify(py).slice(0, 80)}`); return out; }
    if (js.length !== py.length) { out.push(`${p}: length js=${js.length} py=${py.length}`); return out; }
    for (let i = 0; i < js.length && out.length < 25; i++) compare(js[i], py[i], `${p}/${i}`, out, ctx);
    return out;
  }
  const jk = Object.keys(js).sort(), pk = Object.keys(py).sort();
  if (jk.join('|') !== pk.join('|')) { out.push(`${p}: keys js=[${jk}] py=[${pk}]`); }
  for (const k of pk) if (k in js && out.length < 25) compare(js[k], py[k], `${p}/${k}`, out, ctx);
  return out;
}
function bytesEqual(a, b) {
  if (a.length !== b.length) return `length js=${a.length} py=${b.length}`;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return `byte ${i}: js=${a[i]} py=${b[i]} near ${JSON.stringify(new TextDecoder('latin1').decode(b.subarray(Math.max(0, i - 20), i + 20)))}`;
  return null;
}

/* ------------------------------------------------------------------ runner */
const results = [];
let current = null;
async function test(name, fn) {
  if (FILTER && !name.includes(FILTER)) return;
  current = name;
  const t0 = Date.now();
  try {
    const note = await fn();
    results.push({ name, ok: true, note: note || '', ms: Date.now() - t0 });
  } catch (e) {
    results.push({ name, ok: false, note: (e && e.stack ? e.message : String(e)).split('\n').slice(0, 6).join(' | '), ms: Date.now() - t0 });
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function assertSame(js, py, what) {
  const diffs = compare(js, py);
  if (diffs.length) throw new Error(`${what || 'mismatch'}: ${diffs.length} difference(s): ` + diffs.slice(0, 4).join(' ; '));
}
function writeOut(rel, bytes) {
  const p = path.join(JSOUT, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, bytes);
  return p;
}
const rd = rel => fs.readFileSync(path.isAbsolute(rel) ? rel : path.join(OUT, rel));
const bn = p => path.basename(p);

/* ------------------------------------------------------------- JS readers */
async function readFixture(rel, entry) {
  const fmt = entry.format, opts = Object.assign({}, entry.opts || {}, { file: bn(rel) });
  const dir = path.dirname(path.isAbsolute(rel) ? rel : path.join(OUT, rel));
  const bytes = rd(rel);
  switch (fmt) {
    case 'surfer_grd': return F.readSurferGrd(bytes, opts);
    case 'surfer_bln': return F.readBln(bytes, opts);
    case 'geosoft_grd': return F.readGeosoftGrd(bytes, opts);
    case 'gxf': return F.readGxf(bytes, opts);
    case 'geosoft_xyz': return F.readGeosoftXyz(bytes, opts);
    case 'arc_ascii': return F.readAsc(bytes, opts);
    case 'zmap': return F.readZmap(bytes, opts);
    case 'irap': return F.readIrap(bytes, opts);
    case 'cps3': return F.readCps3(bytes, opts);
    case 'ubc': { const models = {}; for (const [k, v] of Object.entries(opts.models || {})) models[k] = fs.readFileSync(path.join(dir, v)); return F.readUbc(bytes, Object.assign({}, opts, { models })); }
    case 'obj': return F.readObj(bytes, opts);
    case 'dxf': return F.readDxf(bytes, opts);
    case 'gocad_ts': return F.readGocad(bytes, opts);
    case 'lf_msh': return F.readLfMsh(bytes, opts);
    case 'csv_points': return F.readPointsCsv(bytes, opts);
    case 'csv_drillholes': {
      const intervals = {};
      for (const [k, v] of Object.entries(opts.intervals || {})) intervals[k] = fs.readFileSync(path.join(dir, v));
      return F.readDrillholes({ collar: bytes, survey: opts.survey ? fs.readFileSync(path.join(dir, opts.survey)) : undefined, intervals }, opts);
    }
    case 'csv_structural': return F.readStructuralCsv(bytes, opts);
    case 'csv_blockmodel': return F.readBlockmodelCsv(bytes, opts);
    case 'segy': return F.readSegy(bytes, opts);
    case 'las': return F.readLas(bytes, opts);
    case 'omf1': return F.readOmf1(bytes, opts);
    case 'omf2': return F.readOmf2(bytes, opts);
    default: throw new Error('no JS reader for ' + fmt);
  }
}
function summaryOf(fmt, obj) { return fmt === 'segy' ? segySummary(obj) : (fmt === 'las' ? lasSummary(obj) : summarize(obj)); }
function stripDate(s) { if (s && s.metadata) { const m = Object.assign({}, s.metadata); delete m.date; return Object.assign({}, s, { metadata: m }); } return s; }

/* ==================================================================== main */
async function main() {
  if (!NO_GEN || !fs.existsSync(path.join(OUT, 'expected.json'))) {
    const t0 = Date.now();
    console.log(pyRun([GEN, 'gen', OUT]).trim() + ` (${Date.now() - t0} ms)`);
  }
  fs.rmSync(JSOUT, { recursive: true, force: true });
  fs.mkdirSync(JSOUT, { recursive: true });
  const expected = JSON.parse(fs.readFileSync(path.join(OUT, 'expected.json'), 'utf8'));
  const FX = expected.fixtures;
  const readObjs = {};

  /* ---- 0. formatting helpers vs python repr / printf */
  await test('helpers: repr() / %g / %.10g / %.7f parity', () => {
    let bad = 0, n = 0;
    for (const [v, r, g6, g10, f7] of expected.repr_vectors) {
      n++;
      if (F.pyRepr(v) !== r) bad++;
      if (F.fmtG(v) !== g6) bad++;
      if (F.fmtG(v, 10) !== g10) bad++;
      if (f7 !== null && F.pyFixed(v, 7) !== f7) bad++;
    }
    assert(bad === 0, `${bad} formatting mismatches`);
    assert(F.pyRound(2.5) === 2 && F.pyRound(3.5) === 4 && F.pyRound(-2.5) === -2, 'round half even');
    return `${n} values x 4 formats`;
  });

  /* ---- 1. JS readers on python-written fixtures */
  for (const [rel, entry] of Object.entries(FX)) {
    if (!entry.summary || entry.format === 'project') continue;
    await test(`read ${entry.format}: ${path.isAbsolute(rel) ? bn(rel) : rel}`, async () => {
      const obj = await readFixture(rel, entry);
      readObjs[rel] = obj;
      const js = summaryOf(entry.format, obj);
      let exp = entry.summary;
      if (js && js.project) { assertSame(stripDate(js), stripDate(exp), 'project'); }
      else assertSame(js, exp);
      if (js && js.project) return `project: ${obj.objects.length} objects, ${obj.metadata.warnings.length} warnings`;
      return Array.isArray(obj) ? `${obj.length} objects` : (obj.kind || (entry.format === 'segy' ? `${obj.n_traces} traces` : `${obj.n_rows} rows`));
    });
  }

  /* SEG-Y section image + IBM floats + LAS intervals */
  await test('segy: section_image() parity', () => {
    const d = readObjs['seismic/a.sgy'];
    const exp = FX['seismic/a.sgy'].section_image;
    const img = F.segySectionImage(d);
    assertSame({ width: img.width, height: img.height, gray: Array.from(img.gray), p1: img.p1, p2: img.p2, z_top: img.z_top, z_bottom: img.z_bottom, clip: img.clip, warnings: img.warnings },
      { width: exp.width, height: exp.height, gray: exp.gray, p1: exp.p1, p2: exp.p2, z_top: exp.z_top, z_bottom: exp.z_bottom, clip: exp.clip, warnings: exp.warnings });
    const img2 = F.segySectionImage(d, { zTop: 1200.0, zBottom: 900.0, clipPct: 100 });
    assertSame({ z_top: img2.z_top, z_bottom: img2.z_bottom, warnings: img2.warnings, gray: Array.from(img2.gray) }, exp.custom);
    return `${img.width}x${img.height} grey`;
  });
  await test('segy: IBM float conversions', () => {
    for (const [v, w] of FX.ibm.words) assert(F.floatToIbm(v) === w, `float_to_ibm(${v}) = ${F.floatToIbm(v)} != ${w}`);
    for (const [w, v] of FX.ibm.floats) assert(F.ibmToFloat(w) === v, `ibm_to_float(${w})`);
    return `${FX.ibm.words.length + FX.ibm.floats.length} words`;
  });
  await test('las: las_to_intervals() parity', () => {
    const d = readObjs['las/wrap.las'];
    const exp = FX['las/wrap.las'].intervals;
    assertSame(JSON.parse(JSON.stringify(F.lasToIntervals(d, 'W1'), (k, v) => JV(v))), exp.default, 'default');
    assertSame(JSON.parse(JSON.stringify(F.lasToIntervals(d, 'W1', 2.0), (k, v) => JV(v))), exp.step2, 'step 2');
    assertSame(JSON.parse(JSON.stringify(F.lasToIntervals(d, 'W1', null, ['DT']), (k, v) => JV(v))), exp.dt_only, 'curves');
    return `${exp.default.length} rows`;
  });

  /* ---- 2. JS writers -> (a) byte-identical to the python fixture, (b) python readers agree */
  const roundTrip = async (rel, writer, outRel, extraPyOpts, srcRel) => {
    const entry = FX[rel];
    const src = readObjs[srcRel || rel];
    assert(src, 'source fixture not read: ' + (srcRel || rel));
    const bytes = await writer(src);
    const out = outRel || rel;
    const p = writeOut(out, bytes);
    const exact = bytesEqual(bytes, rd(rel));
    const pyOpts = Object.assign({}, entry.opts || {}, extraPyOpts || {});
    const pySum = pySummarize(entry.format, p, pyOpts);
    assertSame(pySum, entry.summary, 'python re-read of the JS file');
    if (exact) throw new Error('python reads it back identically but bytes differ: ' + exact);
    return `${bytes.length} bytes identical; python re-read ok`;
  };
  const W = [
    ['grids/dsaa.grd', g => F.writeSurferGrd(g, { fmt: 'dsaa' })],
    ['grids/dsbb.grd', g => F.writeSurferGrd(g, { fmt: 'dsbb' })],
    ['grids/dsrb.grd', g => F.writeSurferGrd(g, { fmt: 'dsrb' })],
    ['grids/blank.grd', g => F.writeSurferGrd(g, { fmt: 'dsaa' })],
    ['grids/geo_float.grd', g => F.writeGeosoftGrd(g, { dtype: 'float' })],
    ['grids/geo_short.grd', g => F.writeGeosoftGrd(g, { dtype: 'short' }), null, null, false, 'grids/geo_float.grd'],
    ['grids/geo_rot.grd', g => F.writeGeosoftGrd(g, { dtype: 'float' })],
    ['grids/g.gxf', g => F.writeGxf(g)],
    ['grids/g_rot.gxf', g => F.writeGxf(g)],
    ['grids/wide.gxf', g => F.writeGxf(g)],
    ['grids/sq.asc', g => F.writeAsc(g)],
    ['grids/g.zmap', g => F.writeZmap(g)],
    ['grids/g2.zmap', g => F.writeZmap(g, { nodesPerLine: 3, fieldWidth: 15, decimals: 3 })],
    ['grids/g.irap', g => F.writeIrap(g)],
    ['grids/g_rot.irap', g => F.writeIrap(g)],
    ['grids/p.xyz', p => F.writeGeosoftXyz(p)],
    ['grids/sample.xyz', p => F.writeGeosoftXyz(p), 'grids/sample_rewrite.xyz', null, true],
    ['grids/l.bln', l => F.writeBln(l)],
    ['meshes/a.obj', m => F.writeObj(m)],
    ['meshes/n.obj', m => F.writeObj(m, { normals: true })],
    ['meshes/a.dxf', o => F.writeDxf(o)],
    ['meshes/m.ts', o => F.writeGocad(o[0])],
    ['meshes/d.ts', o => F.writeGocad(o[0], { zpositive: 'Depth' })],
    ['meshes/l.pl', o => F.writeGocad(o[0])],
    ['meshes/p.vs', o => F.writeGocad(o[0])],
    ['meshes/lf.msh', m => F.writeLfMsh(m)],
    ['tables/pts.csv', p => F.writePointsCsv(p)],
    ['tables/pts_lf.csv', p => F.writePointsCsv(p, { leapfrog: true, columns: ['lith'] })],
    ['tables/st.csv', p => F.writeStructuralCsv(p)],
    ['tables/bm_nohdr.csv', b => F.writeBlockmodelCsv(b, { embeddedHeader: false, skipEmpty: false, sidecar: false }).csv],
    ['tables/rot.csv', b => F.writeBlockmodelCsv(b, { sidecar: false }).csv],
    ['seismic/a.sgy', d => F.writeSegy(d.samples, { dt_us: d.dt * 1e6, coords: d.coords })],
    ['seismic/f1.sgy', d => F.writeSegy(d.samples, { dt_us: 1000, format_code: 1 })],
    ['seismic/f2.sgy', d => F.writeSegy(d.samples, { dt_us: 1000, format_code: 2 })],
    ['seismic/f3.sgy', d => F.writeSegy(d.samples, { dt_us: 1000, format_code: 3 })],
    ['seismic/f5.sgy', d => F.writeSegy(d.samples, { dt_us: 1000, format_code: 5 })],
    ['seismic/f8.sgy', d => F.writeSegy(d.samples, { dt_us: 1000, format_code: 8 })],
    ['seismic/le.sgy', d => F.writeSegy(d.samples, { dt_us: 1000, endian: 'little' })],
    ['seismic/eb.sgy', d => F.writeSegy(d.samples, { dt_us: 500, text: d.text_header, text_encoding: 'ebcdic' })],
    ['las/out.las', d => F.writeLas(d), null, null, false, 'las/dup.las'],
    ['las/out.las', d => F.writeLas(d), 'las/out_again.las', null, true],
  ];
  for (const [rel, writer, outRel, pyOpts, noExact, srcRel] of W) {
    await test(`write ${FX[rel].format}: ${outRel || rel}${srcRel ? ' (from ' + srcRel + ')' : ''}`, async () => {
      if (!noExact) return roundTrip(rel, writer, outRel, pyOpts, srcRel);
      const src = readObjs[srcRel || rel];
      const bytes = await writer(src);
      const p = writeOut(outRel || rel, bytes);
      const pySum = pySummarize(FX[rel].format, p, FX[rel].opts || {});
      // the re-written file has the same content but not the same bytes (comment layout / lossy header re-read)
      const js = summaryOf(FX[rel].format, await readFixture(p, { format: FX[rel].format, opts: FX[rel].opts }));
      assertSame(pySum, JSON.parse(JSON.stringify(js)), 'python vs js read of the JS file');
      return `${bytes.length} bytes; python re-read ok`;
    });
  }
  await test('write ubc: mesh + model (identical bytes, python re-read)', async () => {
    const bm = readObjs['grids/ubc.msh'];
    const r = F.writeUbc(bm, 'density');
    const r2 = F.writeUbc(bm, 'sus');
    const pm = writeOut('grids/ubc.msh', r.msh);
    writeOut('grids/ubc_density.mod', r.mod);
    writeOut('grids/ubc_sus.mod', r2.mod);
    for (const [a, b] of [[r.msh, rd('grids/ubc.msh')], [r.mod, rd('grids/ubc_density.mod')], [r2.mod, rd('grids/ubc_sus.mod')]]) { const e = bytesEqual(a, b); if (e) throw new Error(e); }
    assertSame(pySummarize('ubc', pm, FX['grids/ubc.msh'].opts), FX['grids/ubc.msh'].summary);
    assert(bm.metadata.warnings.some(w => w.includes('NaN cells')), 'nodata warning recorded');
    return 'msh + 2 models identical';
  });
  await test('write ubc: exact UBC cell ordering', async () => {
    const bm = readObjs['grids/code.msh'];
    const r = F.writeUbc(bm, 'code');
    const vals = new TextDecoder().decode(r.mod).trim().split('\n').map(Number);
    const exp = []; for (let iy = 0; iy < 2; iy++) for (let ix = 0; ix < 3; ix++) for (let kz = 0; kz < 4; kz++) exp.push(kz + 10 * ix + 100 * iy);
    assert(JSON.stringify(vals) === JSON.stringify(exp), 'order');
    assert(new TextDecoder().decode(r.msh).startsWith('3 2 4\n1000.0 2000.0 500.0\n'), 'mesh header');
    return 'z fastest, then easting, then northing';
  });
  await test('write csv_drillholes: collar / survey / intervals (identical bytes, python re-read)', async () => {
    const dh = readObjs['tables/collar.csv'];
    const files = F.writeDrillholes(dh);
    const dir = path.join(JSOUT, 'tables', 'dh');
    fs.mkdirSync(dir, { recursive: true });
    for (const [k, v] of Object.entries(files)) fs.writeFileSync(path.join(dir, k), v);
    const pyOut = py(`
from geomodel.formats import tables
d = ${JSON.stringify(dir)}
dh = tables.write_drillholes(tables.read_drillholes(os.path.join(${JSON.stringify(OUT)}, 'tables', 'collar.csv'), os.path.join(${JSON.stringify(OUT)}, 'tables', 'survey.csv'),
     {'assay': os.path.join(${JSON.stringify(OUT)}, 'tables', 'assay.csv'), 'lith': os.path.join(${JSON.stringify(OUT)}, 'tables', 'lith.csv')}, negative_dip_down=True), '/tmp/gm_fixtures/pydh/')
out = {k: open(v, 'rb').read().decode('utf-8') for k, v in dh.items()}
back = tables.read_drillholes(os.path.join(d, 'collar.csv'), os.path.join(d, 'survey.csv'), {'assay': os.path.join(d, 'assay.csv'), 'lith': os.path.join(d, 'lith.csv')})
out['back'] = {'collars': back.collars, 'surveys': back.surveys, 'intervals': back.intervals, 'warnings': back.metadata['warnings']}
`);
    for (const k of ['collar', 'survey', 'assay', 'lith']) assert(new TextDecoder().decode(files[k + '.csv']) === pyOut[k], `${k}.csv differs from python's`);
    assertSame(pyOut.back, { collars: dh.collars, surveys: dh.surveys, intervals: dh.intervals, warnings: dh.metadata.warnings }, 'python re-read');
    return Object.keys(files).join(', ');
  });
  await test('write csv_blockmodel: embedded header + sidecar (identical bytes, python re-read)', async () => {
    const bm = readObjs['tables/bm.csv'];
    const r = F.writeBlockmodelCsv(bm);
    const p = writeOut('tables/bm.csv', r.csv);
    writeOut('tables/bm.csv.txt', r.txt);
    for (const [a, b] of [[r.csv, rd('tables/bm.csv')], [r.txt, rd('tables/bm.csv.txt')]]) { const e = bytesEqual(a, b); if (e) throw new Error(e); }
    assertSame(pySummarize('csv_blockmodel', p), FX['tables/bm.csv'].summary);
    return `${r.csv.length} + ${r.txt.length} bytes identical`;
  });

  /* ---- 3. OMF writers: python readers + omf-rust + pyarrow + omf 1.0.1 */
  const projSum = FX['omf/project_summary'].summary;
  let jsKit = null;
  await test('write omf2: JS project -> python read_omf2 matches the python-written kit', async () => {
    const prj = readObjs['omf/kit.omf'];
    jsKit = writeOut('omf/kit.omf', await F.writeOmf2(prj));
    const pySum = pySummarize('omf2', jsKit);
    assertSame(stripDate(pySum), stripDate(FX['omf/kit.omf'].summary), 'python read of JS omf2');
    const back = await F.readOmf2(fs.readFileSync(jsKit));
    assertSame(stripDate(summarize(back)), stripDate(FX['omf/kit.omf'].summary), 'JS read of JS omf2');
    assert(back.objects[0].id === prj.objects[0].id, 'ids restored through nwmm metadata');
    return `${fs.statSync(jsKit).size} bytes`;
  });
  await test('write omf2: compression none + prerelease "" + object list', async () => {
    const prj = readObjs['omf/kit.omf'];
    const p = writeOut('omf/kit_none.omf', await F.writeOmf2(prj, { compression: 'none' }));
    assertSame(stripDate(pySummarize('omf2', p)), stripDate(FX['omf/kit_none.omf'].summary));
    const two = await F.writeOmf2([prj.objects[0], prj.objects[2]], { name: 'two', prerelease: '' });
    const p2 = writeOut('omf/two.omf', two);
    const s = pySummarize('omf2', p2);
    assert(s.name === 'two' && s.metadata.omf_version === '2.0' && s.objects.length === 2, 'two-object file');
    const info = py(`
import zipfile, gzip
z = zipfile.ZipFile(${JSON.stringify(p)})
index = json.loads(gzip.decompress(z.read('index.json.gz')))
out = {'comment': z.comment.decode(), 'stored': all(i.compress_type == 0 for i in z.infolist()), 'last': z.infolist()[-1].filename,
       'names': [i.filename for i in z.infolist()][:3], 'elements': [e['name'] for e in index['elements']], 'types': [e['geometry']['type'] for e in index['elements']],
       'crs': index.get('coordinate_reference_system'), 'units': index.get('units'), 'color': index['elements'][0]['color'], 'alpha': index['elements'][2]['color'][3],
       'grid': index['elements'][3]['geometry']['grid'], 'att_types': {a['name']: a['data']['type'] for a in index['elements'][0]['attributes']},
       'comment2': zipfile.ZipFile(${JSON.stringify(p2)}).comment.decode(), 'testzip': z.testzip()}
`);
    assert(info.comment === 'Open Mining Format 2.0-beta.1' && info.comment2 === 'Open Mining Format 2.0', 'archive comments');
    assert(info.stored && info.last === 'index.json.gz' && info.testzip === null, 'stored members, index last, CRCs ok');
    assert(JSON.stringify(info.elements) === JSON.stringify(['samples', 'workings', 'surf', 'topo', 'mag', 'blocks']), 'element names');
    assert(JSON.stringify(info.types) === JSON.stringify(['PointSet', 'LineSet', 'Surface', 'GridSurface', 'GridSurface', 'BlockModel']), 'geometry types');
    assert(info.crs === 'EPSG:32612' && info.units === 'meters' && info.alpha === 128, 'crs / units / alpha');
    assert(JSON.stringify(info.grid) === JSON.stringify({ type: 'Regular', size: [10, 20], count: [2, 1] }), 'grid');
    assert(JSON.stringify(info.att_types) === JSON.stringify({ vec: 'Vector', Au_ppm: 'Number', lith: 'Category', ok: 'Boolean', when: 'Number', col: 'Color' }), 'attribute types');
    return 'zip layout + index.json verified with python zipfile';
  });
  await test('omf-rust (omf2 wheel) reads the JS-written OMF v2 with 0 problems', async () => {
    const r = py(`
import omf2
res = {}
for p in [${JSON.stringify(jsKit)}, ${JSON.stringify(path.join(JSOUT, 'omf/kit_none.omf'))}, ${JSON.stringify(path.join(JSOUT, 'omf/two.omf'))}]:
    reader = omf2.Reader(p)
    project, problems = reader.project()
    els = project.elements()
    d = {'n': len(els), 'problems': [str(x) for x in problems], 'names': [e.name for e in els]}
    if len(els) == 6:
        atts = {a.name: a for a in els[0].attributes()}
        vals, mask = reader.array_numbers(atts['Au_ppm'].get_data().values)
        cat = atts['lith'].get_data()
        b, bmask = reader.array_booleans(atts['ok'].get_data().values)
        when, wmask = reader.array_numbers(atts['when'].get_data().values)
        vec, vmask = reader.array_vectors(atts['vec'].get_data().values)
        col, cmask = reader.array_color(atts['col'].get_data().values)
        d.update({'v1': reader.array_vertices(els[0].geometry().vertices).tolist()[1], 'au': vals.tolist(), 'au_mask': mask.tolist(),
                  'names': d['names'], 'cat_names': reader.array_names(cat.names), 'cat_idx': reader.array_indices(cat.values)[0].tolist(), 'cat_grad': reader.array_gradient(cat.gradient).tolist(),
                  'ok': b.tolist(), 'ok_mask': bmask.tolist(), 'when0': str(when[0])[:19], 'when_mask': wmask.tolist(), 'vec0': vec.tolist()[0], 'vec_mask': vmask.tolist(),
                  'col2': col.tolist()[2], 'col_mask': cmask.tolist(), 'tris': reader.array_triangles(els[2].geometry().triangles).tolist(),
                  'segs': reader.array_segments(els[1].geometry().segments).tolist(), 'heights': reader.array_scalars(els[3].geometry().heights).tolist(),
                  'text': list(reader.array_text(els[5].attributes()[1].get_data().values))[:2]})
    res[os.path.basename(p)] = d
out = res
`);
    for (const [k, d] of Object.entries(r)) assert(d.problems.length === 0, `${k}: problems ${d.problems.join('; ')}`);
    const d = r['kit.omf'];
    assert(d.n === 6 && r['two.omf'].n === 2, 'element counts');
    assert(JSON.stringify(d.v1) === '[500010,4100000,1510]', 'vertices');
    assert(d.au[0] === 1.5 && JSON.stringify(d.au_mask) === '[false,true,false]', 'numbers + mask');
    assert(JSON.stringify(d.cat_names) === '["qtz","sch"]' && d.cat_idx[0] === 0 && JSON.stringify(d.cat_grad) === '[[255,0,0,255],[0,0,255,255]]', 'category');
    assert(JSON.stringify(d.ok.slice(0, 2)) === '[true,false]' && JSON.stringify(d.ok_mask) === '[false,false,true]', 'booleans');
    assert(d.when0 === '2020-01-01T00:00:00' && JSON.stringify(d.when_mask) === '[false,true,false]', 'timestamps');
    assert(JSON.stringify(d.vec0) === '[1,0,0]' && JSON.stringify(d.vec_mask) === '[false,false,true]', 'vectors');
    assert(JSON.stringify(d.col2) === '[10,20,30,40]' && JSON.stringify(d.col_mask) === '[false,true,false]', 'colors');
    assert(JSON.stringify(d.tris) === '[[0,1,2],[1,3,2]]' && JSON.stringify(d.segs) === '[[0,1],[1,2],[3,4]]', 'indices');
    assert(JSON.stringify(d.heights.slice(0, 4)) === '[1,2,3,4]' && d.heights[4] === null, 'grid heights');
    assert(JSON.stringify(d.text) === '["a","b"]', 'text');
    return 'kit.omf, kit_none.omf, two.omf: 0 problems, arrays verified';
  });
  await test('pyarrow opens every parquet member of the JS-written OMF v2 (schemas as parquet-rs)', async () => {
    const r = py(`
import zipfile, gzip, pyarrow as pa, pyarrow.parquet as pq
pa.set_cpu_count(1)
z = zipfile.ZipFile(${JSON.stringify(jsKit)})
index = json.loads(gzip.decompress(z.read('index.json.gz')))
schemas = {}
rows = {}
for info in z.infolist():
    if info.filename.endswith('.parquet'):
        data = z.read(info.filename)
        pf = pq.ParquetFile(io.BytesIO(data))
        schemas[info.filename] = str(pf.schema).split('\\n', 1)[1]
        rows[info.filename] = pf.metadata.num_rows
        pq.read_table(io.BytesIO(data), use_threads=False)
verts = index['elements'][0]['geometry']['vertices']['filename']
tris = index['elements'][2]['geometry']['triangles']['filename']
t = pq.read_table(io.BytesIO(z.read(verts)), use_threads=False).to_pydict()
out = {'n': len(schemas), 'verts_schema': schemas[verts], 'tris_schema': schemas[tris], 'x': t['x'], 'rows': rows, 'counts': {k: v['item_count'] for e in index['elements'] for k, v in e['geometry'].items() if isinstance(v, dict) and 'item_count' in v}}
`);
    assert(r.verts_schema === 'required group field_id=-1 schema {\n  required double field_id=-1 x;\n  required double field_id=-1 y;\n  required double field_id=-1 z;\n}\n', 'vertex schema: ' + r.verts_schema);
    assert(r.tris_schema.includes('required int32 field_id=-1 a (Int(bitWidth=32, isSigned=false));'), 'triangle schema');
    assert(JSON.stringify(r.x) === '[500000,500010,500020]', 'x column');
    return `${r.n} parquet members opened by pyarrow`;
  });
  await test('parquet: JS writer reproduces the omf-rust sample schemas + values (pyarrow + python reader)', async () => {
    const { Column, Group } = F;
    const CASES = {
      '18.parquet': [new Column('scalar', 'double', [1.0, 2.0])],
      '3.parquet': [new Column('number', 'double', [1.0, 2.0, 3.0, 4.0, null], true)],
      '2.parquet': [new Column('a', 'int32', [0, 1, 2, 3, 0, 0], false, 'uint32'), new Column('b', 'int32', [1, 2, 3, 0, 2, 3], false, 'uint32'), new Column('c', 'int32', [4, 4, 4, 4, 1, 2], false, 'uint32')],
      '16.parquet': [new Column('a', 'int32', [0, 1, 2, 3, 0, 1, 2, 3], false, 'uint32'), new Column('b', 'int32', [1, 2, 3, 0, 4, 4, 4, 4], false, 'uint32')],
      '9.parquet': [new Column('index', 'int32', [0, 0, 0, 0, 1], true, 'uint32')],
      '10.parquet': [new Column('name', 'byte_array', ['Base', 'Top'], false, 'string')],
      '17.parquet': [new Column('text', 'byte_array', [null, null, null, null, 'sw', 'se', 'ne', 'nw'], true, 'string')],
      '21.parquet': [new Column('bool', 'boolean', [false, false, false, false, false, false, false, true], true)],
      '12.parquet': [new Column('number', 'int64', [1, 2], true)],
      '5.parquet': [new Column('number', 'int64', [0, 1, 2, 3, 4].map(h => 946684800000000 + h * 3600000000), true, 'timestamp_micros')],
      '7.parquet': ['r', 'g', 'b', 'a'].map((c, i) => new Column(c, 'int32', [[0, 0, 255, 255], [0, 255, 0, 255], [255, 0, 0, 255], [255, 255, 255, 255]][i], false, 'uint8')),
      '4.parquet': [new Group('color', ['r', 'g', 'b', 'a'].map((c, i) => new Column(c, 'int32', [[255, 255, 0, 0, 255, 255], [0, 255, 255, 0, 255, 255], [0, 0, 0, 255, 255, 255], [255, 255, 255, 255, 255, 255]][i], false, 'uint8')))],
      '14.parquet': [new Group('vector', [new Column('x', 'float', [0, 0, 0, 0, 0]), new Column('y', 'float', [0, 0, 0, 0, 0]), new Column('z', 'float', [0, 0, 0, 0, 1])], true, [false, false, false, false, true])],
      '6.parquet': [new Column('value', 'int64', [946688400000000, 946692000000000, 946695600000000], false, 'timestamp_micros'), new Column('inclusive', 'boolean', [false, false, true])],
    };
    const dir = path.join(JSOUT, 'parquet');
    fs.mkdirSync(dir, { recursive: true });
    for (const [name, fields] of Object.entries(CASES)) {
      fs.writeFileSync(path.join(dir, 'gzip_' + name), await F.writeParquet(fields, { compression: 'gzip' }));
      fs.writeFileSync(path.join(dir, 'none_' + name), await F.writeParquet(fields, { compression: 'none' }));
    }
    const r = py(`
import zipfile, pyarrow as pa, pyarrow.parquet as pq
from geomodel.formats import parquet_lite as pl
pa.set_cpu_count(1)
z = zipfile.ZipFile(${JSON.stringify(expected.sample_v2)})
bad = []
n = 0
for name in ${JSON.stringify(Object.keys(CASES))}:
    ref = z.read(name)
    ref_schema = str(pq.ParquetFile(io.BytesIO(ref)).schema).split('\\n', 1)[1]
    ref_vals = pq.read_table(io.BytesIO(ref), use_threads=False).to_pydict()
    for comp in ('gzip', 'none'):
        ours = open(os.path.join(${JSON.stringify(dir)}, comp + '_' + name), 'rb').read()
        our_schema = str(pq.ParquetFile(io.BytesIO(ours)).schema).split('\\n', 1)[1]
        if our_schema != ref_schema: bad.append(name + ' schema ' + comp)
        if pq.read_table(io.BytesIO(ours), use_threads=False).to_pydict() != ref_vals: bad.append(name + ' values ' + comp)
        if pl.read_parquet(ours).columns != pl.read_parquet(ref).columns: bad.append(name + ' python reader ' + comp)
        n += 1
out = {'bad': bad, 'n': n}
`);
    assert(r.bad.length === 0, 'mismatches: ' + r.bad.join(', '));
    return `${r.n} files identical in schema + values`;
  });
  await test('parquet: JS reader == python reader on every omf-rust sample member', async () => {
    const zr = new F.ZipReader(fs.readFileSync(expected.sample_v2));
    const ours = {};
    for (const n of zr.names()) if (n.endsWith('.parquet')) { const pf = await F.readParquet(await zr.read(n)); const cols = {}; for (const [k, v] of Object.entries(pf.columns)) cols[k] = list(v).map(x => x instanceof Uint8Array ? Array.from(x) : x); ours[n] = { cols, schema: pf.schemaText(), rows: pf.numRows }; }
    const tmp = path.join(JSOUT, 'parquet', 'sample_cols.json');
    fs.writeFileSync(tmp, JSON.stringify(ours));
    const r = py(`
import zipfile
from geomodel.formats import parquet_lite as pl
js = json.load(open(${JSON.stringify(tmp)}))
z = zipfile.ZipFile(${JSON.stringify(expected.sample_v2)})
bad = []
for name, info in js.items():
    pf = pl.read_parquet(z.read(name))
    for k, v in pf.columns.items():
        pv = [(None if isinstance(x, float) and x != x else (list(x) if isinstance(x, bytes) else x)) for x in v]
        if pv != info['cols'].get(k): bad.append(name + '/' + k)
    if pf.schema_text() != info['schema'] or pf.num_rows != info['rows']: bad.append(name + ' schema')
out = {'bad': bad, 'n': len(js)}
`);
    assert(r.bad.length === 0, 'mismatches: ' + r.bad.join(', '));
    return `${r.n} members`;
  });
  await test('write omf1: JS project -> python read_omf1 + omf-rust converter + omf 1.0.1 reference reader', async () => {
    const prj = readObjs['omf/kit.omf'];
    const p = writeOut('omf/kit_v09.omf', await F.writeOmf1(prj));
    assertSame(stripDate(pySummarize('omf1', p)), stripDate(FX['omf/kit_v09.omf'].summary), 'python read of JS omf1');
    const back = await F.readOmf1(fs.readFileSync(p));
    assertSame(stripDate(summarize(back)), stripDate(FX['omf/kit_v09.omf'].summary), 'JS read of JS omf1');
    const r = py(`
import omf2, struct, uuid
p = ${JSON.stringify(p)}
raw = open(p, 'rb').read()
json_start = struct.unpack('<Q', raw[52:60])[0]
reg = json.loads(raw[json_start:].decode('utf-8'))
puid = str(uuid.UUID(bytes=raw[36:52]))
conv = ${JSON.stringify(path.join(JSOUT, 'omf/kit_v09_converted.omf'))}
problems = omf2.Omf1Converter().convert(p, conv)
project, problems2 = omf2.Reader(conv).project()
out = {'magic': list(raw[:4]), 'version': raw[4:36].rstrip(b'\\x00').decode(), 'project_class': reg[puid]['__class__'], 'n_el': len(reg[puid]['elements']),
       'classes': sorted({v['__class__'] for v in reg.values()}), 'detect': omf2.detect_omf1(p), 'conv_problems': [str(x) for x in problems],
       'conv_problems2': [str(x) for x in problems2], 'conv_names': [e.name for e in project.elements()],
       'blobs_ok': all(a['start'] >= 60 and a['start'] + a['length'] <= json_start and a['dtype'] in ('<f8', '<i8') for v in reg.values() for a in [v.get('array')] if isinstance(a, dict))}
`);
    assert(JSON.stringify(r.magic) === '[132,131,130,129]' && r.version === 'OMF-v0.9.0' && r.project_class === 'Project' && r.n_el === 6 && r.blobs_ok, 'header / registry');
    for (const c of ['PointSetElement', 'LineSetElement', 'SurfaceElement', 'VolumeElement', 'SurfaceGeometry', 'SurfaceGridGeometry', 'VolumeGridGeometry', 'ScalarData', 'StringData', 'MappedData', 'Vector3Data', 'ColorData', 'Legend', 'ScalarColormap', 'Vector3Array', 'Int3Array', 'Int2Array']) assert(r.classes.includes(c), 'missing class ' + c);
    assert(r.detect && r.conv_problems.length === 0 && r.conv_problems2.length === 0 && r.conv_names.length === 6, 'omf-rust converter: ' + r.conv_problems.join('; '));
    let note = 'python + omf-rust converter ok';
    if (fs.existsSync(OMF1_PYTHON)) {
      const ref = spawnSync(OMF1_PYTHON, ['-c', `
import json, sys
import omf, numpy as np
p = omf.OMFReader(sys.argv[1]).get_project()
out = {'valid': bool(p.validate()), 'elements': [[e.__class__.__name__, e.name, type(e.geometry).__name__, [[d.__class__.__name__, d.name, d.location, int(len(d.array))] for d in e.data]] for e in p.elements]}
pts = p.elements[0]
out['scalar'] = [None if np.isnan(v) else float(v) for v in pts.data[1].array.array]
out['mapped'] = [int(v) for v in pts.data[2].array.array]
out['legend'] = list(pts.data[2].legends[0].values.array)
out['grid_offsets'] = [None if np.isnan(v) else float(v) for v in p.elements[3].geometry.offset_w.array]
out['vol_axis_u'] = [float(v) for v in p.elements[5].geometry.axis_u]
print(json.dumps(out))
`, p], { encoding: 'utf8', timeout: 120000 });
      if (ref.status !== 0) throw new Error('omf 1.0.1 reference reader failed: ' + (ref.stderr || '').slice(-800));
      const o = JSON.parse(ref.stdout.trim().split('\n').pop());
      assert(o.valid, 'omf 1.0.1 validate()');
      assert(JSON.stringify(o.elements.map(e => e[0])) === JSON.stringify(['PointSetElement', 'LineSetElement', 'SurfaceElement', 'SurfaceElement', 'SurfaceElement', 'VolumeElement']), 'element classes');
      assert(JSON.stringify(o.elements[0][3][1]) === JSON.stringify(['ScalarData', 'Au_ppm', 'vertices', 3]), 'Au_ppm data');
      assert(JSON.stringify(o.scalar) === '[1.5,null,3.25]' && JSON.stringify(o.mapped) === '[0,-1,1]' && JSON.stringify(o.legend) === '["qtz","sch"]', 'arrays');
      assert(JSON.stringify(o.grid_offsets) === '[1,2,3,4,null,6]' && Math.abs(o.vol_axis_u[1] + Math.sin(Math.PI / 4)) < 1e-9, 'grid / volume');
      note += '; omf 1.0.1 validate() ok';
    } else note += '; omf 1.0.1 env not present (skipped)';
    return note;
  });
  await test('omf: convert test_v09.omf -> omf2 in JS, omf-rust reads it', async () => {
    const p9 = await F.readOmf1(fs.readFileSync(expected.sample_v09), { file: 'test_v09.omf' });
    const warns = [];
    const p = writeOut('omf/converted.omf', await F.writeOmf2(p9, { warnings: warns }));
    assert(warns.length === 0, 'warnings: ' + warns.join('; '));
    const back = await F.readOmf2(fs.readFileSync(p));
    assert(JSON.stringify(back.objects.map(o => [o.kind, o.name])) === JSON.stringify([['points', 'pts'], ['lineset', 'lines'], ['mesh', 'surf'], ['grid2d', 'gridsurf'], ['blockmodel', 'vol']]), 'kinds');
    assert(JSON.stringify(list(back.objects[0].xyz.subarray(0, 3))) === '[101,202,303]' && JSON.stringify(back.objects[0].attributes.mapped) === '["x","y",null]', 'points');
    assert(back.objects[0].attributes.dates[0] === '2020-01-01T00:00:00Z' && JSON.stringify(back.objects[4].origin) === '[110,220,330]', 'dates / origin');
    const r = py(`
import omf2
project, problems = omf2.Reader(${JSON.stringify(p)}).project()
out = {'problems': [str(x) for x in problems], 'n': len(project.elements())}
`);
    assert(r.problems.length === 0 && r.n === 5, 'omf-rust: ' + r.problems.join('; '));
    return '5 elements, 0 problems';
  });

  await test('omf2: hand-built left-handed grid surface, flat grid and flipped block model (JS == python)', async () => {
    const { Column } = F;
    const zw = new F.ZipWriter();
    zw.add('1.parquet', await F.writeParquet([new Column('scalar', 'double', [1, 2, 3, 4, 5, 6])]));
    zw.add('2.parquet', await F.writeParquet([new Column('number', 'double', Array.from({ length: 12 }, (_, i) => i * 1.5), true)]));
    const index = { name: 'lh', date: '2026-01-01T00:00:00Z', elements: [
      { name: 'g', geometry: { type: 'GridSurface', orient: { origin: [10.0, 20.0, 5.0], u: [1.0, 0.0, 0.0], v: [0.0, -1.0, 0.0] }, grid: { type: 'Regular', size: [1.0, 2.0], count: [2, 1] }, heights: { filename: '1.parquet', item_count: 6 } } },
      { name: 'flat', geometry: { type: 'GridSurface', orient: { origin: [0.0, 0.0, 0.0], u: [1.0, 0.0, 0.0], v: [0.0, 1.0, 0.0] }, grid: { type: 'Regular', size: [1.0, 1.0], count: [1, 1] } } },
      { name: 'bm', geometry: { type: 'BlockModel', orient: { origin: [0.0, 0.0, 100.0], u: [1.0, 0.0, 0.0], v: [0.0, -1.0, 0.0], w: [0.0, 0.0, -1.0] }, grid: { type: 'Regular', size: [1.0, 2.0, 5.0], count: [2, 3, 2] } },
        attributes: [{ name: 'v', location: 'Primitives', data: { type: 'Number', values: { filename: '2.parquet', item_count: 12 } } }] },
      { name: 'rot', geometry: { type: 'BlockModel', orient: { origin: [0.0, 0.0, 0.0], u: [Math.SQRT1_2, -Math.SQRT1_2, 0.0], v: [Math.SQRT1_2, Math.SQRT1_2, 0.0], w: [0.0, 0.0, 1.0] }, grid: { type: 'Regular', size: [1.0, 1.0, 1.0], count: [1, 1, 1] } } },
    ] };
    zw.add('index.json.gz', await F.gzip(new TextEncoder().encode(JSON.stringify(index))));
    const p = writeOut('omf/handbuilt.omf', zw.finish('Open Mining Format 2.0-beta.1'));
    const prj = await F.readOmf2(fs.readFileSync(p));
    const [g, flat, bm] = prj.objects;
    assert(g.nx === 3 && g.ny === 2 && g.x0 === 10 && g.y0 === 18, 'left-handed grid origin');
    assert(JSON.stringify(list(g.values)) === '[1,0,-1,4,3,2]', 'rows flipped, heights subtract: ' + list(g.values));
    assert(JSON.stringify(list(flat.values)) === '[0,0,0,0]', 'flat grid');
    assert(JSON.stringify(bm.origin) === '[0,-6,90]' && JSON.stringify(bm.count) === '[2,3,2]', 'flipped block model origin: ' + bm.origin);
    assertSame(stripDate(summarize(prj)), stripDate(pySummarize('omf2', p)), 'JS vs python read');
    return 'grid flipV, flat grid, block model flipV+flipW, 45-degree azimuth: JS == python';
  });
  await test('geosoft_grd: compressed block with an unreliable size field (lenient inflate) JS == python', async () => {
    const data = new Uint8Array(rd('grids/geo_comp.grd'));
    const dv = new DataView(data.buffer);
    const nb = dv.getInt32(520, true);
    // shrink the declared size of block 0 so the strict zlib read is truncated -> natural-end fallback
    const sizeOff = 528 + 8 * nb;
    dv.setInt32(sizeOff, dv.getInt32(sizeOff, true) - 4, true);
    const p = writeOut('grids/geo_comp_bad.grd', data);
    const g = await F.readGeosoftGrd(data, { file: 'geo_comp_bad.grd' });
    assert(g.metadata.warnings.some(w => w.includes('size field unreliable')), 'lenient warning: ' + g.metadata.warnings);
    assertSame(summarize(g), pySummarize('geosoft_grd', p), 'JS vs python');
    return g.metadata.warnings[0];
  });

  /* ---- 4. readAny / writeAs / sniff */
  await test('sniff(): parity with formats.sniff() on 29 heads', async () => {
    const cases = [['x.omf', [0x84, 0x83, 0x82, 0x81]], ['x.omf', 'PK\x03\x04'], ['x.grd', 'DSAA'], ['x.grd', 'DSBB 1'], ['x.grd', 'DSRB'], ['x.grd', [4, 0, 0, 0, 2, 0, 0, 0]], ['x.grd', [7, 0, 0, 0, 2, 0, 0, 0]],
      ['x.msh', '%%ARANZ-1.0'], ['x.msh', '3 2 4'], ['x.ts', '  GOCAD TSurf 1'], ['x.txt', '#TITLE'], ['x.txt', ' #POINTS'], ['x.asc', 'ncols 7'], ['x.asc', 'hello'], ['x.dat', '! c\n@g, GRID, 5'],
      ['x.zmp', 'nothing'], ['x.txt', '-996 5'], ['x.txt', 'FSASCI 0'], ['x.csv', ''], ['x.xyz', ''], ['a.dxf', ''], ['x.txt', '0\r\nSECTION'], ['x.las', ''], ['x.txt', '~VERSION'], ['x.sgy', ''], ['x.segy', ''], ['x.obj', ''], ['x.bin', 'junk'], ['x.gxf', '']];
    const js = cases.map(([n, h]) => F.sniff(n, typeof h === 'string' ? new TextEncoder().encode(h) : Uint8Array.from(h)));
    const pyr = py(`
from geomodel.formats import sniff
cases = ${JSON.stringify(cases.map(([n, h]) => [n, typeof h === 'string' ? Array.from(new TextEncoder().encode(h)) : h]))}
out = [sniff(path=n, head=bytes(h)) for n, h in cases]
`);
    const bad = cases.map((c, i) => js[i] === pyr[i] ? null : `${c[0]} ${JSON.stringify(c[1]).slice(0, 20)}: js=${js[i]} py=${pyr[i]}`).filter(Boolean);
    assert(!bad.length, bad.join('; '));
    return `${cases.length} heads agree`;
  });
  const SH = '/tmp/gm_silverhills';
  if (fs.existsSync(SH)) {
    await test('readAny(): every Silver Hills kit file (format detection, objects, warnings)', async () => {
      const notes = [];
      for (const f of fs.readdirSync(SH).sort()) {
        if (/\.(md|json)$/.test(f)) continue;
        const r = await F.readAny({ name: f, bytes: fs.readFileSync(path.join(SH, f)) }, { crs: GM.utm.crs(12, true) });
        assert(r.objects.length > 0, f + ': no objects');
        const expFmt = f.endsWith('-omf09.omf') ? 'omf1' : FX[path.join(SH, f)].format;
        assert(r.format === expFmt, `${f}: format ${r.format} != ${expFmt}`);
        assert(r.objects.every(o => o.provenance.file === f && o.provenance.format), f + ': provenance');
        const js = summaryOf(expFmt, r.project || (r.objects.length === 1 && !['dxf', 'gocad_ts'].includes(expFmt) ? r.objects[0] : r.objects));
        if (js && js.project) assertSame(stripDate(js), stripDate(FX[path.join(SH, f)].summary), f); else assertSame(js, FX[path.join(SH, f)].summary, f);
        notes.push(`${f.replace(/^silver-hills-mine|^sedimentary-rocks-/, '~')}:${r.objects.length}`);
      }
      return notes.join(' ');
    });
    await test('writeAs(): Silver Hills topography + points through every writer, python reads them back', async () => {
      const topo = (await F.readAny({ name: 'topography.grd', bytes: fs.readFileSync(path.join(SH, 'topography.grd')) })).objects[0];
      const pts = (await F.readAny({ name: 'claims-closed-blm-centroids.csv', bytes: fs.readFileSync(path.join(SH, 'claims-closed-blm-centroids.csv')) })).objects[0];
      const mesh = (await F.readAny({ name: 'sedimentary-rocks-associated-with-basin-and-range-extension.obj', bytes: fs.readFileSync(path.join(SH, 'sedimentary-rocks-associated-with-basin-and-range-extension.obj')) })).objects[0];
      const checks = [];
      const dir = path.join(JSOUT, 'writeas');
      fs.mkdirSync(dir, { recursive: true });
      for (const [fmt, obj, opts] of [['surfer_grd', topo, { fmt: 'dsrb' }], ['geosoft_grd', topo, {}], ['gxf', topo, {}], ['arc_ascii', topo, {}], ['zmap', topo, {}], ['irap', topo, {}],
        ['csv_points', pts, {}], ['geosoft_xyz', pts, {}], ['gocad_ts', pts, {}], ['obj', mesh, {}], ['lf_msh', mesh, {}], ['gocad_ts', mesh, {}], ['dxf', [mesh, pts], {}], ['omf2', [topo, pts, mesh], {}], ['omf1', [topo, pts, mesh], {}]]) {
        const files = await F.writeAs(fmt, obj, opts);
        for (const [name, data] of Object.entries(files)) {
          const p = path.join(dir, fmt + '_' + name);
          fs.writeFileSync(p, data);
          checks.push([fmt, p, Array.isArray(obj) ? obj.length : 1]);
        }
      }
      const r = py(`
from geomodel import formats
res = []
for fmt, p, n in ${JSON.stringify(checks)}:
    r = formats.reader(fmt)(p)
    if hasattr(r, 'objects'): res.append([fmt, len(r.objects), r.metadata['warnings']])
    elif isinstance(r, list): res.append([fmt, len(r), r[0].metadata['warnings']])
    else: res.append([fmt, 1, r.metadata['warnings'], getattr(r, 'nx', None), getattr(r, 'n', None), getattr(r, 'n_triangles', None)])
out = res
`);
      for (let i = 0; i < checks.length; i++) {
        const [fmt, , n] = checks[i];
        assert(r[i][1] === n, `${fmt}: python read ${r[i][1]} objects, expected ${n}`);
        if (r[i][3] !== undefined && r[i][3] !== null) assert(r[i][3] === 144, fmt + ' nx');
        if (r[i][5] !== undefined && r[i][5] !== null) assert(r[i][5] === 512, fmt + ' triangles');
      }
      return `${checks.length} files written and read back by python`;
    });
  }
  await test('readAny(): CSV lon/lat -> UTM conversion, table routing, SEG-Y -> ImagePlane + points, LAS -> Drillholes', async () => {
    const r = await F.readAny({ name: 'pts.csv', bytes: new TextEncoder().encode('lon,lat,au\n-114.5,38.2,1\n-114.6,38.3,2\n') }, { crs: GM.utm.crs(12, true) });
    const [e, n] = GM.utm.fwd(-114.5, 38.2, 12, true);
    assert(Math.abs(r.objects[0].point(0)[0] - e) < 1e-6 && Math.abs(r.objects[0].point(0)[1] - n) < 1e-6, 'utm conversion');
    assert(r.warnings.includes('converted lon/lat to UTM zone 12N'), 'conversion warning');
    const r0 = await F.readAny({ name: 'pts.csv', bytes: new TextEncoder().encode('x,y,z\n500000,4200000,1\n') }, { crs: GM.utm.crs(12, true) });
    assert(r0.objects[0].point(0)[0] === 500000 && !r0.warnings.some(w => w.includes('converted')), 'projected coordinates untouched');
    const r2 = await F.readAny({ name: 'st.csv', bytes: rd('tables/strike.csv') }, { table: 'structural' });
    assert(r2.format === 'csv_structural' && r2.objects[0].role === 'structural' && r2.objects[0].attributes.dip_azimuth[0] === 135, 'structural routing');
    const r3 = await F.readAny({ name: 'bm.csv', bytes: rd('tables/bm.csv') }, { table: 'blockmodel' });
    assert(r3.format === 'csv_blockmodel' && r3.objects[0].kind === 'blockmodel' && r3.objects[0].n === 12, 'blockmodel routing');
    const r4 = await F.readAny({ name: 'collar.csv', bytes: rd('tables/collar.csv') }, { table: 'drillholes', survey: rd('tables/survey.csv'), intervals: { assay: rd('tables/assay.csv') }, negativeDipDown: true });
    assert(r4.objects[0].kind === 'drillholes' && r4.objects[0].collars.length === 2 && r4.objects[0].surveys[0].dip === 60, 'drillholes routing');
    const sg = await F.readAny({ name: 'a.sgy', bytes: rd('seismic/a.sgy') });
    const [plane, tp] = sg.objects;
    assert(plane.kind === 'imageplane' && plane.plane === 'section' && plane.width === 7 && plane.height === 50 && plane.image.startsWith('data:image/png;base64,'), 'image plane');
    assert(JSON.stringify(plane.p1) === '[500000,4000000]' && JSON.stringify(plane.p2) === '[500060,4000030]' && plane.zTop === 0 && plane.zBottom === -98, 'section geometry');
    assert(tp.kind === 'points' && tp.n === 7 && tp.attributes.cdp[3] === 4, 'trace points');
    const png = GM.b64decode(plane.image.split(',')[1]);
    const pr = py(`
from PIL import Image
im = Image.open(io.BytesIO(bytes(${JSON.stringify(Array.from(png))})))
out = {'mode': im.mode, 'size': list(im.size), 'px': list(im.getdata())[:40]}
`);
    assert(pr.mode === 'L' && pr.size[0] === 7 && pr.size[1] === 50, 'PNG decoded by PIL');
    assert(JSON.stringify(pr.px) === JSON.stringify(FX['seismic/a.sgy'].section_image.gray.slice(0, 40)), 'PNG pixels == section_image grey');
    const la = await F.readAny({ name: 'wrap.las', bytes: rd('las/wrap.las') });
    const dh = la.objects[0];
    assert(dh.kind === 'drillholes' && dh.collars[0].hole === 'W1' && dh.intervals.las.length === 3 && dh.intervals.las[1].RHOB === 2450, 'las drillholes');
    const ubcr = await F.readAny({ name: 'ubc.msh', bytes: rd('grids/ubc.msh') }, { models: { density: rd('grids/ubc_density.mod') }, nodata: -99999 });
    assert(ubcr.format === 'ubc' && ubcr.objects[0].kind === 'blockmodel' && 'density' in ubcr.objects[0].attributes, 'ubc routing');
    return 'utm, structural, blockmodel, drillholes, segy(PNG via PIL), las, ubc';
  });
  await test('encodePNG(): RGBA + RGB + grey decoded by PIL', async () => {
    const w = 3, h = 2;
    const rgba = Uint8Array.from({ length: w * h * 4 }, (_, i) => (i * 37) & 255);
    const rgb = Uint8Array.from({ length: w * h * 3 }, (_, i) => (i * 11) & 255);
    const grey = Uint8Array.from({ length: w * h }, (_, i) => i * 40);
    const pngs = [await F.encodePNG(w, h, rgba, { channels: 4 }), await F.encodePNG(w, h, rgb, { channels: 3 }), await F.encodePNG(w, h, grey, { channels: 1 })];
    const r = py(`
from PIL import Image
out = []
for b in ${JSON.stringify(pngs.map(p => Array.from(p)))}:
    im = Image.open(io.BytesIO(bytes(b)))
    im.load()
    out.append({'mode': im.mode, 'size': list(im.size), 'data': [list(p) if isinstance(p, tuple) else p for p in im.getdata()]})
`);
    assert(r[0].mode === 'RGBA' && JSON.stringify(r[0].data.flat()) === JSON.stringify(Array.from(rgba)), 'rgba');
    assert(r[1].mode === 'RGB' && JSON.stringify(r[1].data.flat()) === JSON.stringify(Array.from(rgb)), 'rgb');
    assert(r[2].mode === 'L' && JSON.stringify(r[2].data) === JSON.stringify(Array.from(grey)), 'grey');
    return 'PIL decodes all three';
  });
  await test('zip: ZipWriter output verified by python zipfile; ZipReader reads python + omf-rust archives', async () => {
    const zw = new F.ZipWriter();
    zw.add('a.txt', 'hello');
    zw.add('b.bin', Uint8Array.from({ length: 5000 }, (_, i) => i & 255));
    const zb = zw.finish('Open Mining Format 2.0-beta.1');
    const p = writeOut('zip/t.zip', zb);
    const r = py(`
import zipfile
z = zipfile.ZipFile(${JSON.stringify(p)})
out = {'test': z.testzip(), 'comment': z.comment.decode(), 'names': z.namelist(), 'a': z.read('a.txt').decode(), 'b_ok': z.read('b.bin') == bytes(i & 255 for i in range(5000)), 'stored': all(i.compress_type == 0 for i in z.infolist())}
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zz:
    zz.writestr('x.txt', 'compressed text ' * 100)
    zz.comment = b'hello'
open(${JSON.stringify(path.join(JSOUT, 'zip/py.zip'))}, 'wb').write(buf.getvalue())
`);
    assert(r.test === null && r.comment === 'Open Mining Format 2.0-beta.1' && r.a === 'hello' && r.b_ok && r.stored, 'python zipfile view');
    const zr = new F.ZipReader(fs.readFileSync(path.join(JSOUT, 'zip/py.zip')));
    assert(zr.comment === 'hello' && new TextDecoder().decode(await zr.read('x.txt')) === 'compressed text '.repeat(100), 'deflate member from python');
    const omf = new F.ZipReader(fs.readFileSync(expected.sample_v2));
    assert(omf.names().length === 36 && (await omf.read('1.parquet')).length === 386, 'omf-rust zip64 extra fields');
    return 'stored writer + deflate/zip64 reader';
  });

  /* ---------------------------------------------------------------- report */
  const pad = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s.padEnd(n));
  console.log('\n' + pad('test', 96) + ' ' + pad('result', 6) + ' ' + 'note');
  console.log('-'.repeat(140));
  for (const r of results) console.log(pad(r.name, 96) + ' ' + pad(r.ok ? 'PASS' : 'FAIL', 6) + ' ' + r.note.slice(0, 160) + (r.ms > 500 ? ` (${r.ms} ms)` : ''));
  const nFail = results.filter(r => !r.ok).length;
  console.log('-'.repeat(140));
  console.log(`${results.length - nFail} passed, ${nFail} failed, ${results.length} total`);
  process.exit(nFail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(2); });
