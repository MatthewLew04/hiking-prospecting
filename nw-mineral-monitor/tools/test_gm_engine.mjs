#!/usr/bin/env node
/* test_gm_engine.mjs — cross-checks site/assets/geomodel/gm-engine.js against
   the Python reference (pipelines/geomodel/*.py) by spawning python3 with small
   scripts that read a JSON payload on stdin and print JSON.

   node tools/test_gm_engine.mjs            (no dependencies; exits 1 on failure)
   node tools/test_gm_engine.mjs --no-bench (skip the timing section)

   Relative error = |js - py| / max(|py|, 1e-3 * max|py|)  (NaN == NaN == null). */

import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { Worker } from 'node:worker_threads';
import * as GM from '../site/assets/geomodel/gm-core.js';
import * as E from '../site/assets/geomodel/gm-engine.js';
import * as S from '../site/assets/geomodel/gm-structural.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const PIPELINES = path.join(ROOT, 'pipelines');
const WORKER_URL = new URL('../site/assets/geomodel/gm-worker.js', import.meta.url);
const NO_BENCH = process.argv.includes('--no-bench');

/* ------------------------------------------------------------------ python */
const PY_PRELUDE = `
import sys, json, math, array
sys.path.insert(0, ${JSON.stringify(PIPELINES)})
from geomodel import interp, stratigraphy, blockmodel, slicing, workings, kit
from geomodel.model import Grid2D, Mesh, LineSet, PointSet, BlockModel, StratModel, ImagePlane, NAN, farray
D = json.load(sys.stdin)
def J(o):
    if isinstance(o, float): return None if (o != o or o in (math.inf, -math.inf)) else o
    if isinstance(o, dict): return {str(k): J(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, array.array)): return [J(v) for v in o]
    return o
def grid(d):
    return Grid2D(d['nx'], d['ny'], d['x0'], d['y0'], d['dx'], d['dy'], d['values'], rotation=d.get('rotation', 0.0),
                  oid=d.get('id'), name=d.get('name', ''), role=d.get('role', 'surface'))
def pset(d):
    return PointSet(d['xyz'], attributes=d.get('attributes') or {}, oid=d.get('id'), name=d.get('name', ''))
def mesh(d):
    return Mesh(d['vertices'], d['triangles'], oid=d.get('id'), name=d.get('name', ''), color=d.get('color'))
def lineset(d):
    return LineSet(d['vertices'], d['segments'], parts=d.get('parts'), features=d.get('features'), oid=d.get('id'), name=d.get('name', ''), role=d.get('role', 'lines'), color=d.get('color'))
def nanlist(a):
    return [NAN if v is None else v for v in a]
out = None
`;
function py(code, data) {
  const r = spawnSync('python3', ['-c', PY_PRELUDE + code + '\nprint(json.dumps(J(out)))\n'],
    { input: JSON.stringify(data, (k, v) => (typeof v === 'number' && !isFinite(v)) ? null : v), encoding: 'utf8', maxBuffer: 1 << 30 });
  if (r.status !== 0) throw new Error('python failed:\n' + r.stderr);
  try { return JSON.parse(r.stdout); } catch (e) { throw new Error('bad python output: ' + r.stdout.slice(0, 400) + '\n' + r.stderr); }
}
const list = a => Array.from(a, v => (v == null || v !== v) ? null : v);
const gridJSON = g => ({ kind: 'grid2d', id: g.id, name: g.name, nx: g.nx, ny: g.ny, x0: g.x0, y0: g.y0, dx: g.dx, dy: g.dy, rotation: g.rotation, role: g.role, values: list(g.values) });
const psetJSON = p => ({ kind: 'points', id: p.id, name: p.name, xyz: list(p.xyz), attributes: Object.fromEntries(Object.entries(p.attributes).map(([k, v]) => [k, v.map(x => (typeof x === 'number' && x !== x) ? null : x)])) });
const meshJSON = m => ({ kind: 'mesh', id: m.id, name: m.name, color: m.color, vertices: list(m.vertices), triangles: Array.from(m.triangles) });
const lsJSON = l => ({ kind: 'lineset', id: l.id, name: l.name, role: l.role, color: l.color, vertices: list(l.vertices), segments: Array.from(l.segments), parts: l.parts, features: l.features });

/* ------------------------------------------------------------------ utils */
function rng(seed) {   // mulberry32
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
const results = [];
let failures = 0;
function record(name, ok, detail = '', ms = null) { results.push({ name, ok, detail, ms }); if (!ok) failures++; }
function isNum(v) { return typeof v === 'number'; }
function maxAbs(b) { let m = 0; const walk = x => { if (x == null) return; if (isNum(x)) { if (isFinite(x)) m = Math.max(m, Math.abs(x)); } else if (typeof x === 'object') { for (const y of (Array.isArray(x) || ArrayBuffer.isView(x)) ? x : Object.values(x)) walk(y); } }; walk(b); return m; }
/** deep compare JS value (may hold typed arrays / NaN) with the Python JSON value (null = NaN). */
function deepCompare(a, b, tol, scale = null) {
  const state = { maxErr: 0, bad: null };
  if (scale == null) scale = maxAbs(b);
  const floorAbs = 1e-3 * scale;
  const fail = (p, msg) => { if (!state.bad) state.bad = `${p}: ${msg}`; };
  const walk = (x, y, p) => {
    if (state.bad) return;
    const xn = x == null || (isNum(x) && x !== x), yn = y == null || (isNum(y) && y !== y);
    if (xn || yn) { if (!(xn && yn)) fail(p, `js=${fmtv(x)} py=${fmtv(y)}`); return; }
    if (isNum(y) || isNum(x)) {
      if (!isNum(x) || !isNum(y)) { fail(p, `type js=${typeof x} py=${typeof y}`); return; }
      const err = Math.abs(x - y) / Math.max(Math.abs(y), floorAbs, 1e-300);
      if (err > state.maxErr) state.maxErr = err;
      if (!(err <= tol)) fail(p, `js=${x} py=${y} rel=${err.toExponential(2)}`);
      return;
    }
    if (typeof y === 'string' || typeof y === 'boolean') { if (x !== y) fail(p, `js=${fmtv(x)} py=${fmtv(y)}`); return; }
    const xa = Array.isArray(x) || ArrayBuffer.isView(x), ya = Array.isArray(y);
    if (xa || ya) {
      if (!(xa && ya)) { fail(p, `array mismatch js=${typeof x} py=${typeof y}`); return; }
      if (x.length !== y.length) { fail(p, `length js=${x.length} py=${y.length}`); return; }
      for (let i = 0; i < y.length; i++) walk(x[i], y[i], `${p}[${i}]`);
      return;
    }
    if (typeof y === 'object') {
      if (typeof x !== 'object') { fail(p, `object mismatch js=${typeof x}`); return; }
      const kx = Object.keys(x).filter(k => x[k] !== undefined).sort(), ky = Object.keys(y).sort();
      if (kx.join(',') !== ky.join(',')) { fail(p, `keys js=[${kx}] py=[${ky}]`); return; }
      for (const k of ky) walk(x[k], y[k], `${p}.${k}`);
      return;
    }
    fail(p, `unhandled js=${fmtv(x)} py=${fmtv(y)}`);
  };
  walk(a, b, '$');
  return state;
}
const fmtv = v => { try { return JSON.stringify(v) ?? String(v); } catch (e) { return String(v); } };
function cmp(name, a, b, tol, extra = '') {
  const s = deepCompare(a, b, tol);
  record(name, !s.bad, (s.bad ? s.bad + ' ' : '') + `max rel err ${s.maxErr.toExponential(2)}${extra ? ' ' + extra : ''}`);
  return !s.bad;
}
function section(title) { console.log(`\n== ${title}`); }
async function timed(fn) { const t0 = performance.now(); const r = await fn(); return [r, performance.now() - t0]; }

/* ============================================================ 1. interp */
async function testInterp() {
  section('1. interpolation (idw / rbf / kriging / variograms / grids)');
  const R = rng(12345);
  const n = 70, pts = [], vals = [];
  for (let i = 0; i < n; i++) {
    const x = R() * 1000, y = R() * 800, z = R() * 300;
    pts.push([x, y, z]);
    vals.push(Math.sin(x / 200) + Math.cos(y / 150) + z / 300 + 0.2 * (R() - 0.5));
  }
  vals[5] = null;                           // missing value (dropped by both)
  const tg = [];
  for (let i = 0; i < 40; i++) tg.push([R() * 1100 - 50, R() * 900 - 50, R() * 340 - 20]);
  tg.push(pts[3].slice(), pts[17].slice());  // exact hits
  const pts2 = pts.map(p => [p[0], p[1]]), vals2 = vals.map((v, i) => v == null ? null : v + 0.1 * i);
  const vgJ = { nugget: 0.1, structures: [{ model: 'spherical', sill: 0.6, range: 300 }, { model: 'exponential', sill: 0.4, range: 600 }] };
  const vg2J = { nugget: 0.05, structures: [{ model: 'gaussian', sill: 1.0, range: 1.0 }], anisotropy: { ranges: [400, 200, 80], azimuth: 120, dip: 20, plunge: 0 } };
  const vg3J = { nugget: 0.2, structures: [{ model: 'power', sill: 0.5, range: 400, exponent: 1.5 }, { model: 'linear', sill: 0.3, range: 800 }, { model: 'nugget', sill: 0.1 }] };
  const anJ = { ranges: [300, 150, 60], azimuth: 35, dip: 10, plunge: 5 };
  const mean = vals.filter(v => v != null).reduce((s, v) => s + v, 0) / vals.filter(v => v != null).length;
  const [P, pyMs] = await timed(() => py(`
pts, vals, tg = D['pts'], D['vals'], D['tg']
an = interp.Anisotropy(**D['an'])
out = {}
out['idw'] = interp.idw(pts, vals, tg, power=2.0, max_points=16)
out['idw_r'] = interp.idw(pts, vals, tg, power=1.5, max_points=8, radius=250.0)
out['idw_an'] = interp.idw(pts, vals, tg, power=2.0, max_points=12, anisotropy=an)
out['nn'] = interp.nearest_neighbour(pts, vals, tg)
out['nn_r'] = interp.nearest_neighbour(pts, vals, tg, radius=40.0)
out['rbf'] = {}
for k in interp.RBF_KERNELS:
    out['rbf'][k] = interp.RBF(kernel=k, drift='linear').fit(pts, vals).predict(tg)
out['rbf_np'] = interp.RBF(kernel='thin_plate').fit(pts, vals).predict_np(tg)
out['rbf_none'] = interp.RBF(kernel='cubic', drift='none').fit(pts, vals).predict(tg)
out['rbf_const_smooth'] = interp.RBF(kernel='thin_plate', drift='constant', smoothing=0.5).fit(pts, vals).predict(tg)
out['rbf_gauss_eps'] = interp.RBF(kernel='gaussian', epsilon=400.0).fit(pts, vals).predict(tg)
out['rbf_mq_eps'] = interp.RBF(kernel='multiquadric', epsilon=150.0, drift='constant').fit(pts, vals).predict(tg)
out['rbf_sph'] = interp.RBF(kernel='spheroidal', range=500.0, sill=2.0, smoothing=0.01).fit(pts, vals).predict(tg)
out['rbf_an'] = interp.RBF(kernel='thin_plate', anisotropy=an).fit(pts, vals).predict(tg)
vg = interp.Variogram.from_json(D['vg']); vg2 = interp.Variogram.from_json(D['vg2']); vg3 = interp.Variogram.from_json(D['vg3'])
out['vg_json'] = vg.to_json(); out['vg2_json'] = vg2.to_json(); out['vg3_json'] = vg3.to_json()
out['gamma'] = [vg.gamma(h) for h in (0.0, 10.0, 150.0, 300.0, 900.0)] + [vg3.gamma(h) for h in (0.0, 50.0, 400.0, 1200.0)] + [vg2.gamma_vec(pts[0], pts[1]), vg2.covariance(pts[2], pts[3]), vg.sill, vg2.sill]
e, v = interp.ordinary_kriging(pts, vals, tg, vg, max_points=16); out['ok'] = e; out['ok_var'] = v
e, v = interp.ordinary_kriging(pts, vals, tg, vg2, max_points=12, radius=2.0); out['ok2'] = e; out['ok2_var'] = v
e, v = interp.ordinary_kriging(pts, vals, tg, vg3, max_points=6, min_points=3, radius=220.0); out['ok3'] = e; out['ok3_var'] = v
e, v = interp.ordinary_kriging(pts, vals, tg, vg, max_points=10, return_variance=False); out['ok_nv'] = e; out['ok_nv_var'] = v
out['sk'] = interp.simple_kriging(pts, vals, tg, vg, D['mean'], max_points=10)
out['emp'] = interp.empirical_variogram(pts, vals)
out['emp_dir'] = interp.empirical_variogram(pts, vals, n_lags=10, lag_size=60.0, azimuth=30.0, tolerance=20.0)
out['emp_dir2'] = interp.empirical_variogram(pts, vals, n_lags=8, azimuth=170.0, tolerance=40.0, dim=2)
out['emp2d'] = interp.empirical_variogram(pts, vals, dim=2)
out['emp_cap'] = interp.empirical_variogram(pts, vals, max_pairs=500)
out['fit'] = interp.fit_variogram(out['emp']).to_json()
out['fit_exp'] = interp.fit_variogram(out['emp'], model='exponential', nugget=0.05).to_json()
out['fit_gauss'] = interp.fit_variogram(out['emp_dir']).to_json()
p2, v2 = D['pts2'], D['vals2']
out['grid_spec'] = interp.grid_spec_from_points(p2, cell=37.0)
out['grid_spec_n'] = interp.grid_spec_from_points(p2, n=15, pad=0.1)
out['grids'] = {}
for m, kw in (('rbf', {}), ('rbf_lin', {'kernel': 'linear', 'drift': 'constant', 'smoothing': 0.2}), ('idw', {'power': 3.0, 'max_points': 5}), ('ok', {}), ('ok_vg', {'variogram': vg, 'max_points': 8, 'radius': 300.0}), ('nn', {})):
    g = interp.grid_from_points(p2, v2, method=m.split('_')[0], n=12, name='t', **kw)
    out['grids'][m] = {'spec': [g.x0, g.y0, g.dx, g.dy, g.nx, g.ny], 'values': list(g.values), 'meta': g.metadata['interpolation']}
g = interp.grid_from_points(p2, v2, method='idw', spec=(100.0, 50.0, 80.0, 60.0, 9, 7))
out['grid_spec_given'] = list(g.values)
`, { pts, vals, tg, an: anJ, vg: vgJ, vg2: vg2J, vg3: vg3J, mean, pts2, vals2 }));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  const an = new E.Anisotropy(anJ.ranges, anJ.azimuth, anJ.dip, anJ.plunge);
  cmp('idw power 2 / 16 pts', E.idw(pts, vals, tg, { power: 2, maxPoints: 16 }), P.idw, 1e-6);
  cmp('idw power 1.5 / radius 250', E.idw(pts, vals, tg, { power: 1.5, max_points: 8, radius: 250 }), P.idw_r, 1e-6);
  cmp('idw anisotropic', E.idw(pts, vals, tg, { power: 2, maxPoints: 12, anisotropy: an }), P.idw_an, 1e-6);
  cmp('nearest neighbour', E.nearestNeighbour(pts, vals, tg), P.nn, 1e-12);
  cmp('nearest neighbour radius 40', E.nearestNeighbour(pts, vals, tg, { radius: 40 }), P.nn_r, 1e-12);
  for (const k of E.RBF_KERNELS) cmp(`rbf ${k} (drift linear)`, new E.RBF({ kernel: k, drift: 'linear' }).fit(pts, vals).predict(tg), P.rbf[k], 1e-6);
  cmp('rbf thin_plate vs predict_np', new E.RBF({ kernel: 'thin_plate' }).fit(pts, vals).predict(tg), P.rbf_np, 1e-6);
  cmp('rbf cubic drift none', new E.RBF({ kernel: 'cubic', drift: 'none' }).fit(pts, vals).predict(tg), P.rbf_none, 1e-6);
  cmp('rbf thin_plate constant drift + smoothing', new E.RBF({ kernel: 'thin_plate', drift: 'constant', smoothing: 0.5 }).fit(pts, vals).predict(tg), P.rbf_const_smooth, 1e-6);
  cmp('rbf gaussian epsilon 400', new E.RBF({ kernel: 'gaussian', epsilon: 400 }).fit(pts, vals).predict(tg), P.rbf_gauss_eps, 1e-6);
  cmp('rbf multiquadric epsilon 150', new E.RBF({ kernel: 'multiquadric', epsilon: 150, drift: 'constant' }).fit(pts, vals).predict(tg), P.rbf_mq_eps, 1e-6);
  cmp('rbf spheroidal range/sill/smoothing', new E.RBF({ kernel: 'spheroidal', range: 500, sill: 2, smoothing: 0.01 }).fit(pts, vals).predict(tg), P.rbf_sph, 1e-6);
  cmp('rbf thin_plate anisotropic', new E.RBF({ kernel: 'thin_plate', anisotropy: an }).fit(pts, vals).predict(tg), P.rbf_an, 1e-6);
  const rbfJ = E.RBF.fromJSON(JSON.parse(JSON.stringify(new E.RBF({ kernel: 'thin_plate' }).fit(pts, vals).toJSON(), (k, v) => ArrayBuffer.isView(v) ? Array.from(v) : v)));
  cmp('rbf toJSON/fromJSON round trip', rbfJ.predict(tg), P.rbf.thin_plate, 1e-9);
  const vg = E.Variogram.fromJSON(vgJ), vg2 = E.Variogram.fromJSON(vg2J), vg3 = E.Variogram.fromJSON(vg3J);
  cmp('Variogram.fromJSON(to_json()) round trip', [vg.toJSON(), vg2.toJSON(), vg3.toJSON()], [P.vg_json, P.vg2_json, P.vg3_json], 1e-12);
  cmp('variogram gamma / gamma_vec / covariance / sill', [0, 10, 150, 300, 900].map(h => vg.gamma(h)).concat([0, 50, 400, 1200].map(h => vg3.gamma(h)), [vg2.gammaVec(pts[0], pts[1]), vg2.covariance(pts[2], pts[3]), vg.sill, vg2.sill]), P.gamma, 1e-12);
  let r = E.ordinaryKriging(pts, vals, tg, vg, { maxPoints: 16 });
  cmp('ordinary kriging nested spherical+exponential: estimates', r.est, P.ok, 1e-6);
  cmp('ordinary kriging: variances', r.variance, P.ok_var, 1e-6);
  r = E.ordinaryKriging(pts, vals, tg, vg2J, { max_points: 12, radius: 2.0 });
  cmp('ordinary kriging anisotropic gaussian, radius: estimates', r.est, P.ok2, 1e-6);
  cmp('ordinary kriging anisotropic gaussian, radius: variances', r.variance, P.ok2_var, 1e-6);
  r = E.ordinaryKriging(pts, vals, tg, vg3, { max_points: 6, min_points: 3, radius: 220 });
  cmp('ordinary kriging power+linear+nugget, min_points: estimates', r.est, P.ok3, 1e-6);
  cmp('ordinary kriging power+linear+nugget: variances', r.variance, P.ok3_var, 1e-6);
  r = E.ordinaryKriging(pts, vals, tg, vg, { max_points: 10, return_variance: false });
  cmp('ordinary kriging without variance', [r.est, r.variance], [P.ok_nv, P.ok_nv_var], 1e-6);
  cmp('simple kriging', E.simpleKriging(pts, vals, tg, vg, mean, { max_points: 10 }).est, P.sk, 1e-6);
  cmp('empirical variogram (omni, 3-D)', E.empiricalVariogram(pts, vals), P.emp, 1e-9);
  cmp('empirical variogram (azimuth 30 ±20, lag 60)', E.empiricalVariogram(pts, vals, { n_lags: 10, lag_size: 60, azimuth: 30, tolerance: 20 }), P.emp_dir, 1e-9);
  cmp('empirical variogram (azimuth 170 ±40, 2-D)', E.empiricalVariogram(pts, vals, { nLags: 8, azimuth: 170, tolerance: 40, dim: 2 }), P.emp_dir2, 1e-9);
  cmp('empirical variogram (2-D)', E.empiricalVariogram(pts, vals, { dim: 2 }), P.emp2d, 1e-9);
  cmp('empirical variogram (max_pairs cap)', E.empiricalVariogram(pts, vals, { max_pairs: 500 }), P.emp_cap, 1e-9);
  cmp('fit_variogram spherical (same grid search)', E.fitVariogram(E.empiricalVariogram(pts, vals)).toJSON(), P.fit, 1e-9);
  cmp('fit_variogram exponential, fixed nugget', E.fitVariogram(E.empiricalVariogram(pts, vals), { model: 'exponential', nugget: 0.05 }).toJSON(), P.fit_exp, 1e-9);
  cmp('fit_variogram on directional', E.fitVariogram(E.empiricalVariogram(pts, vals, { n_lags: 10, lag_size: 60, azimuth: 30, tolerance: 20 })).toJSON(), P.fit_gauss, 1e-9);
  cmp('grid_spec_from_points (cell)', E.gridSpecFromPoints(pts2, { cell: 37 }), P.grid_spec, 1e-12);
  cmp('grid_spec_from_points (n, pad)', E.gridSpecFromPoints(pts2, { n: 15, pad: 0.1 }), P.grid_spec_n, 1e-12);
  const gridOpts = { rbf: {}, rbf_lin: { kernel: 'linear', drift: 'constant', smoothing: 0.2 }, idw: { power: 3, max_points: 5 }, ok: {}, ok_vg: { variogram: vgJ, max_points: 8, radius: 300 }, nn: {} };
  for (const [m, kw] of Object.entries(gridOpts)) {
    const g = E.gridFromPoints(pts2, vals2, Object.assign({ method: m.split('_')[0], n: 12, name: 't' }, kw));
    const meta = Object.assign({}, g.metadata.interpolation);
    if (m === 'ok') delete meta.params.variogram;    // JS records the auto-fitted variogram (deliberate extension)
    cmp(`grid_from_points ${m}`, { spec: [g.x0, g.y0, g.dx, g.dy, g.nx, g.ny], values: g.values, meta }, P.grids[m], m === 'nn' ? 1e-12 : 1e-6);
  }
  cmp('grid_from_points with explicit spec', E.gridFromPoints(pts2, vals2, { method: 'idw', spec: [100, 50, 80, 60, 9, 7] }).values, P.grid_spec_given, 1e-6);
}

/* ======================================================= 2. stratigraphy */
function makeStratData() {
  const R = rng(777);
  const nx = 21, ny = 16, x0 = 1000, y0 = 2000, d = 25;
  const topo = new GM.Grid2D({ nx, ny, x0, y0, dx: d, dy: d, name: 'Topography', role: 'topography' });
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) { const [x, y] = topo.nodeXY(i, j); topo.values[j * nx + i] = 1200 + 30 * Math.sin(x / 150) + 20 * Math.cos(y / 100) + 2 * (R() - 0.5); }
  topo.values[5 * nx + 7] = NaN; topo.values[12 * nx + 18] = NaN;     // no-data holes
  const contacts = new GM.PointSet({ name: 'Alluvium contacts', role: 'contacts' });
  for (let i = 0; i < 30; i++) { const x = x0 + 10 + R() * (500 - 20), y = y0 + 10 + R() * (375 - 20); contacts.add(x, y, 1150 + 40 * Math.sin(x / 120) + 25 * Math.cos(y / 90) + 3 * (R() - 0.5), { src: 'map' }); }
  const sand = new GM.Grid2D({ nx: 11, ny: 9, x0: 990, y0: 1990, dx: 55, dy: 55, name: 'Sandstone base (coarse)' });
  for (let j = 0; j < 9; j++) for (let i = 0; i < 11; i++) { const [x, y] = sand.nodeXY(i, j); sand.values[j * 11 + i] = 1120 + 30 * Math.cos(x / 100) - 20 * Math.sin(y / 80); }
  sand.values[3 * 11 + 4] = NaN;
  const units = [
    { name: 'Alluvium', color: [222, 184, 135], lithology: 'gravel', contact: 'erosion', base: contacts },
    { name: 'Sandstone', color: [205, 133, 63], lithology: 'sandstone', contact: 'deposit', base: sand },
    { name: 'Shale', lithology: 'shale', contact: 'deposit', base: 1100 },
    { name: 'Basement', color: [140, 140, 140], lithology: 'granite', base: null },
  ];
  return { topo, contacts, sand, units };
}
async function testStratigraphy() {
  section('2. stratigraphy (21x16 lattice, erosion / deposit / constant / basement)');
  const { topo, contacts, sand, units } = makeStratData();
  const probe = [[1123.4, 2041.7], [1310.2, 2222.9], [1477.0, 2360.5]];
  const probe3 = [[1123.4, 2041.7, 1180.0], [1310.2, 2222.9, 1105.0], [1477.0, 2360.5, 1000.0], [1123.4, 2041.7, 1300.0], [1200.0, 2100.0, 1149.0]];
  const bmBounds = [1000, 2000, 1000, 1500, 2375, 1260], bmSize = [50, 50, 20];
  const sec = [[1005, 2010], [1490, 2370]];
  const payload = {
    topo: gridJSON(topo), probe, probe3, bmBounds, bmSize, sec,
    units: units.map(u => ({ name: u.name, color: u.color || null, lithology: u.lithology, contact: u.contact || null, base: u.base == null ? null : typeof u.base === 'number' ? u.base : u.base.kind === 'grid2d' ? gridJSON(u.base) : psetJSON(u.base) })),
  };
  const [P, pyMs] = await timed(() => py(`
from collections import Counter
topo = grid(D['topo'])
def units_for():
    us = []
    for u in D['units']:
        b = u['base']
        src = None if b is None else (float(b) if isinstance(b, (int, float)) else (grid(b) if b['kind'] == 'grid2d' else pset(b)))
        d = {'name': u['name'], 'lithology': u['lithology'], 'base': src}
        if u['color'] is not None: d['color'] = u['color']
        if u['contact'] is not None: d['contact'] = u['contact']
        us.append(d)
    return us
out = {}
for method, kw in (('rbf', {}), ('idw', {'max_points': 6, 'power': 2.5}), ('ok', {'max_points': 10})):
    sm, bases, topo2 = stratigraphy.build_stratigraphy(topo, units_for(), method=method, **kw)
    grids = {g.id: g for g in bases if g is not None}
    r = {'bases': [None if g is None else list(g.values) for g in bases],
         'names': [None if g is None else g.name for g in bases],
         'roles': [None if g is None else [g.role, g.color, g.metadata.get('contact'), g.metadata.get('source', '')[:6]] for g in bases],
         'units': [{k: v for k, v in u.items() if k != 'base'} for u in sm.units],
         'has_base': [u['base'] is not None for u in sm.units], 'topo_same': topo2 is topo}
    r['columns'] = [stratigraphy.column_at(sm, grids, x, y, topo2) for x, y in D['probe']]
    r['units_at'] = [stratigraphy.unit_at(sm, grids, x, y, z, topo2) for x, y, z in D['probe3']]
    vols = stratigraphy.stratigraphy_volumes(sm, grids, topo2)
    r['vol_counts'] = [[m.n_vertices, m.n_triangles] for m in vols]
    r['vol_sums'] = [[sum(m.vertices), sum(m.triangles)] for m in vols]
    r['vol_meta'] = [[m.name, m.role, m.metadata['unit']] for m in vols]
    bm = blockmodel.create_blockmodel(D['bmBounds'], D['bmSize'])
    stratigraphy.tag_blockmodel(bm, sm, grids, topo2)
    r['tag_counts'] = dict(Counter(bm.attributes['unit']['values']))
    r['tags'] = bm.attributes['unit']['values']
    r['thick'] = list(stratigraphy.thickness_grid(topo2, bases[0]).values)
    r['thick2'] = list(stratigraphy.thickness_grid(bases[0], bases[1], name='ss').values)
    secs = slicing.stratigraphy_section(sm, grids, topo2, D['sec'][0], D['sec'][1], n=60)
    r['sec_counts'] = [[m.n_vertices, m.n_triangles] for m in secs]
    r['sec_verts'] = [list(m.vertices) for m in secs]
    r['sec_tris'] = [list(m.triangles) for m in secs]
    out[method] = r
# lattice different from topography
lat = Grid2D(11, 9, 1000.0, 2000.0, 50.0, 46.875)
sm, bases, topo2 = stratigraphy.build_stratigraphy(topo, units_for(), lattice=lat)
out['lattice'] = {'topo': list(topo2.values), 'bases': [None if g is None else list(g.values) for g in bases], 'topo_same': topo2 is topo, 'nx': topo2.nx}
`, payload));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  for (const [method, kw] of [['rbf', {}], ['idw', { max_points: 6, power: 2.5 }], ['ok', { max_points: 10 }]]) {
    const built = E.buildStratigraphy(topo, units, Object.assign({ method }, kw));
    const { strat, bases, topo: topo2 } = built;
    const Pm = P[method];
    cmp(`build_stratigraphy [${method}]: base surface values`, bases.map(g => g && g.values), Pm.bases, 1e-9);
    cmp(`build_stratigraphy [${method}]: names / roles / colours / contacts`, {
      names: bases.map(g => g && g.name), roles: bases.map(g => g && [g.role, g.color, g.metadata.contact, (g.metadata.source || '').slice(0, 6)]),
      units: strat.units.map(u => { const c = Object.assign({}, u); delete c.base; return c; }), has_base: strat.units.map(u => u.base != null), topo_same: topo2 === topo,
    }, { names: Pm.names, roles: Pm.roles, units: Pm.units, has_base: Pm.has_base, topo_same: Pm.topo_same }, 1e-12);
    const okIds = strat.units.every((u, k) => (u.base == null) === (bases[k] == null) && (u.base == null || u.base === bases[k].id));
    record(`build_stratigraphy [${method}]: unit.base ids match grids`, okIds);
    const grids = bases.filter(Boolean);
    cmp(`column_at [${method}] at 3 points`, probe.map(([x, y]) => E.columnAt(strat, grids, x, y, topo2)), Pm.columns, 1e-9);
    cmp(`unit_at [${method}]`, probe3.map(([x, y, z]) => E.unitAt(strat, grids, x, y, z, topo2)), Pm.units_at, 1e-12);
    const vols = E.stratigraphyVolumes(strat, grids, topo2);
    cmp(`stratigraphy_volumes [${method}]: vertex / triangle counts`, vols.map(m => [m.nVertices, m.nTriangles]), Pm.vol_counts, 0);
    cmp(`stratigraphy_volumes [${method}]: coordinate / index checksums`, vols.map(m => [m.vertices.reduce((s, v) => s + v, 0), m.triangles.reduce((s, v) => s + v, 0)]), Pm.vol_sums, 1e-9);
    cmp(`stratigraphy_volumes [${method}]: names / roles`, vols.map(m => [m.name, m.role, m.metadata.unit]), Pm.vol_meta, 0);
    const bm = E.createBlockModel(bmBounds, bmSize);
    E.tagBlockModel(bm, strat, grids, topo2);
    const counts = {};
    for (const t of bm.attributes.unit.values) counts[t] = (counts[t] || 0) + 1;
    cmp(`tag_blockmodel [${method}]: category counts`, counts, Pm.tag_counts, 0);
    cmp(`tag_blockmodel [${method}]: every block tag`, Array.from(bm.attributes.unit.values), Pm.tags, 0);
    cmp(`thickness_grid [${method}]`, [E.thicknessGrid(topo2, bases[0]).values, E.thicknessGrid(bases[0], bases[1], 'ss').values], [Pm.thick, Pm.thick2], 1e-9);
    const secs = E.stratigraphySection(strat, grids, topo2, sec[0], sec[1], 60);
    cmp(`stratigraphy_section [${method}]: ribbon counts`, secs.map(m => [m.nVertices, m.nTriangles]), Pm.sec_counts, 0);
    cmp(`stratigraphy_section [${method}]: ribbon vertices`, secs.map(m => m.vertices), Pm.sec_verts, 1e-9);
    cmp(`stratigraphy_section [${method}]: ribbon triangles`, secs.map(m => m.triangles), Pm.sec_tris, 0);
  }
  const lat = new GM.Grid2D({ nx: 11, ny: 9, x0: 1000, y0: 2000, dx: 50, dy: 46.875 });
  const b2 = E.buildStratigraphy(topo, units, { lattice: lat });
  cmp('build_stratigraphy on a separate lattice (topo resampled)', { topo: b2.topo.values, bases: b2.bases.map(g => g && g.values), topo_same: b2.topo === topo, nx: b2.topo.nx }, P.lattice, 1e-9);
  return { topo, units };
}

/* ========================================================= 3. estimate */
async function testEstimate() {
  section('3. block-model estimation (20x15x12, OK / IDW / NN / domain)');
  const R = rng(4242);
  const bounds = [0, 0, 0, 200, 150, 60], size = [10, 10, 5];
  const samples = new GM.PointSet({ name: 'assays', role: 'samples' });
  for (let i = 0; i < 120; i++) {
    const x = R() * 220 - 10, y = R() * 170 - 10, z = R() * 70 - 5;
    const v = Math.exp(0.8 * (R() - 0.5) + 0.4 * Math.sin(x / 40) + 0.3 * Math.cos(y / 30) - z / 100);
    samples.add(x, y, z, { au: i % 17 === 0 ? null : v, hole: 'H' + (i % 9) });
  }
  samples.add(45, 35, 12.5, { au: 2.5, hole: 'H0' });       // exactly on a block centroid
  const vgJ = { nugget: 0.2, structures: [{ model: 'spherical', sill: 0.8, range: 60 }] };
  const comp = new GM.PointSet({ name: 'intervals', role: 'samples' });
  let k = 0;
  for (const hole of ['A', 'B', 'C']) {
    let from = 0;
    for (let q = 0; q < 7; q++) { const len = 1 + R() * 3, to = from + len; comp.add(10 * k, 5 * k, -(from + to) / 2, { hole, from, to, length: len, au: q === 3 && hole === 'B' ? null : R() * 3 }); from = to; k++; }
  }
  comp.add(99, 99, 1, { hole: 'D', au: 7.0 });                // no from/to -> passed through
  const [P, pyMs] = await timed(() => py(`
bm = blockmodel.create_blockmodel(D['bounds'], D['size'])
samples = pset(D['samples'])
vg = interp.Variogram.from_json(D['vg'])
blockmodel.estimate(bm, samples, 'au', method='ok', variogram=vg, max_points=12, radius=45.0, out_name='ok')
blockmodel.estimate(bm, samples, 'au', method='ok', variogram=vg, max_points=16, out_name='ok_all')
blockmodel.estimate(bm, samples, 'au', method='idw', max_points=8, power=2.0, out_name='idw')
blockmodel.estimate(bm, samples, 'au', method='nn', out_name='nn')
zone = ['A' if bm.ijk(i)[2] < 6 else 'B' for i in range(bm.n)]
bm.add_attribute('zone', zone, kind='category')
blockmodel.estimate(bm, samples, 'au', method='idw', max_points=6, domain='zone', domain_value='A', out_name='dom')
sd = [i % 3 != 0 for i in range(samples.n)]
blockmodel.estimate(bm, samples, 'au', method='ok', max_points=10, sample_domain=sd, out_name='auto')
out = {'origin': bm.origin, 'count': bm.count, 'n': bm.n,
       'attrs': {k: list(bm.attributes[k]['values']) for k in ('ok_est', 'ok_var', 'ok_all_est', 'ok_all_var', 'idw_est', 'nn_est', 'dom_est', 'auto_est', 'auto_var')},
       'has': {k: (k in bm.attributes) for k in ('idw_var', 'nn_var', 'dom_var')},
       'estimates': bm.metadata['estimates'],
       'gt': blockmodel.grade_tonnage(bm, 'ok_all_est', [0.0, 0.5, 1.0, 1.5, 3.0], density=2.65),
       'gt_dom': blockmodel.grade_tonnage(bm, 'idw_est', [0.5, 1.2], domain='zone', domain_value='B'),
       'centroids': blockmodel.block_centroids(bm)[:9] + blockmodel.block_centroids(bm, [i % 7 == 0 for i in range(bm.n)])[:6]}
ps = blockmodel.blockmodel_to_points(bm, 'dom_est')
out['pts'] = {'n': ps.n, 'xyz': list(ps.xyz)[:30], 'vals': list(ps.attributes['dom_est'])[:10]}
snap = blockmodel.create_blockmodel((3.2, -7.9, 1.1, 57.0, 22.0, 30.0), 5.0, azimuth=30.0, snap=False)
out['bm2'] = {'origin': snap.origin, 'count': snap.count, 'c': snap.centroid(2, 3, 1), 'b': snap.bounds()}
cp = blockmodel.composite(pset(D['comp']), 'au', target_length=3.0)
out['comp'] = {'xyz': list(cp.xyz), 'attrs': cp.attributes, 'name': cp.name}
`, { bounds, size, samples: psetJSON(samples), vg: vgJ, comp: psetJSON(comp) }));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  const bm = E.createBlockModel(bounds, size);
  cmp('create_blockmodel origin / count', { origin: bm.origin, count: bm.count, n: bm.n }, { origin: P.origin, count: P.count, n: P.n }, 0);
  const vg = E.Variogram.fromJSON(vgJ);
  let progressCalls = 0;
  const [, msOk] = await timed(() => E.estimate(bm, samples, 'au', { method: 'ok', variogram: vg, max_points: 12, radius: 45, out_name: 'ok', onProgress: () => progressCalls++ }));
  E.estimate(bm, samples, 'au', { method: 'ok', variogram: vgJ, maxPoints: 16, outName: 'ok_all' });
  E.estimate(bm, samples, 'au', { method: 'idw', max_points: 8, power: 2, out_name: 'idw' });
  E.estimate(bm, samples, 'au', { method: 'nn', out_name: 'nn' });
  const zone = []; for (let i = 0; i < bm.n; i++) zone.push(bm.ijk(i)[2] < 6 ? 'A' : 'B');
  bm.addAttribute('zone', zone, 'category');
  E.estimate(bm, samples, 'au', { method: 'idw', max_points: 6, domain: 'zone', domain_value: 'A', out_name: 'dom' });
  const sd = []; for (let i = 0; i < samples.n; i++) sd.push(i % 3 !== 0);
  E.estimate(bm, samples, 'au', { method: 'ok', max_points: 10, sample_domain: sd, out_name: 'auto' });
  cmp('estimate ok (radius 45): block estimates', bm.attributes.ok_est.values, P.attrs.ok_est, 1e-6, `(${msOk.toFixed(0)} ms, ${progressCalls} progress calls)`);
  cmp('estimate ok (radius 45): block variances', bm.attributes.ok_var.values, P.attrs.ok_var, 1e-6);
  cmp('estimate ok (16 pts, no radius): estimates', bm.attributes.ok_all_est.values, P.attrs.ok_all_est, 1e-6);
  cmp('estimate ok (16 pts, no radius): variances', bm.attributes.ok_all_var.values, P.attrs.ok_all_var, 1e-6);
  cmp('estimate idw (8 pts, power 2)', bm.attributes.idw_est.values, P.attrs.idw_est, 1e-6);
  cmp('estimate nn', bm.attributes.nn_est.values, P.attrs.nn_est, 1e-12);
  cmp('estimate idw in domain zone=A', bm.attributes.dom_est.values, P.attrs.dom_est, 1e-6);
  cmp('estimate ok auto-fitted variogram + sample_domain: estimates', bm.attributes.auto_est.values, P.attrs.auto_est, 1e-6);
  cmp('estimate ok auto-fitted variogram: variances', bm.attributes.auto_var.values, P.attrs.auto_var, 1e-6);
  cmp('estimate: no variance attribute for idw / nn', { idw_var: 'idw_var' in bm.attributes, nn_var: 'nn_var' in bm.attributes, dom_var: 'dom_var' in bm.attributes }, P.has, 0);
  cmp('estimate: metadata.estimates records (incl. fitted variogram)', bm.metadata.estimates, P.estimates, 1e-9);
  cmp('grade_tonnage', E.gradeTonnage(bm, 'ok_all_est', [0, 0.5, 1, 1.5, 3], { density: 2.65 }), P.gt, 1e-9);
  cmp('grade_tonnage with domain', E.gradeTonnage(bm, 'idw_est', [0.5, 1.2], { domain: 'zone', domain_value: 'B' }), P.gt_dom, 1e-9);
  const mask = []; for (let i = 0; i < bm.n; i++) mask.push(i % 7 === 0);
  const c1 = E.blockCentroids(bm), c2 = E.blockCentroids(bm, mask);
  const tri = (a, m) => { const o = []; for (let i = 0; i < m; i++) o.push([a[3 * i], a[3 * i + 1], a[3 * i + 2]]); return o; };
  cmp('block_centroids (all / masked)', tri(c1, 9).concat(tri(c2, 6)), P.centroids, 1e-12);
  const ps = E.blockmodelToPoints(bm, 'dom_est');
  cmp('blockmodel_to_points', { n: ps.n, xyz: Array.from(ps.xyz.slice(0, 30)), vals: ps.attributes.dom_est.slice(0, 10) }, P.pts, 1e-6);
  const bm2 = E.createBlockModel([3.2, -7.9, 1.1, 57, 22, 30], 5, { azimuth: 30, snap: false });
  cmp('create_blockmodel (scalar size, azimuth, no snap) + centroid + bounds', { origin: bm2.origin, count: bm2.count, c: bm2.centroid(2, 3, 1), b: bm2.bounds() }, P.bm2, 1e-12);
  const cp = E.composite(comp, 'au', { target_length: 3 });
  cmp('composite (length-weighted, per hole)', { xyz: cp.xyz, attrs: cp.attributes, name: cp.name }, P.comp, 1e-9);
}

/* ======================================================= 4. isosurface */
function sphereField(n, r) {
  const f = new Float64Array(n * n * n), c = (n - 1) / 2;
  for (let k = 0; k < n; k++) for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) { const x = i - c, y = j - c, z = k - c; f[i + n * (j + n * k)] = Math.sqrt(x * x + y * y + z * z) - r; }
  return f;
}
async function testIsosurface() {
  section('4. iso-surface (marching tetrahedra) + mesh/plane intersection');
  const n = 21, field = sphereField(n, 7.3), origin = [-10, -10, -10], spacing = [1, 1, 1];
  // second field: tilted plane through nodes (t == 0 / 1 reuse), NaN hole, anisotropic spacing, iso != 0
  const c2 = [15, 12, 10], sp2 = [2, 3, 1.5], o2 = [100, 200, 50];
  const f2 = new Float64Array(c2[0] * c2[1] * c2[2]);
  for (let k = 0; k < c2[2]; k++) for (let j = 0; j < c2[1]; j++) for (let i = 0; i < c2[0]; i++) {
    const z = o2[2] + k * sp2[2], x = o2[0] + i * sp2[0];
    f2[i + c2[0] * (j + c2[1] * k)] = (z - 54.5) + 0.25 * Math.sin(x / 7) + 0.25;
    if (i >= 6 && i <= 8 && j >= 4 && j <= 6) f2[i + c2[0] * (j + c2[1] * k)] = NaN;
  }
  const plane = { point: [0.3, 0, 0], normal: [1, 0, 0] };
  const plane2 = { point: [-1.2, 0.7, 0.4], normal: [0.3, -0.5, 0.81] };
  const [P, pyMs] = await timed(() => py(`
m = slicing.isosurface(nanlist(D['field']), D['count'], D['origin'], D['spacing'], iso=0.0, name='sphere')
out = {'nv': m.n_vertices, 'nt': m.n_triangles, 'verts': list(m.vertices), 'tris': list(m.triangles), 'iso': m.metadata['iso'], 'role': m.role}
ls = slicing.mesh_plane_intersection(m, D['plane']['point'], D['plane']['normal'])
out['sec'] = {'parts': len(ls.parts), 'length': ls.length(), 'nv': ls.n_vertices, 'closed': [list(ls.vertex(p[0])) == list(ls.vertex(p[-1])) for p in ls.parts], 'plane': ls.metadata['plane'], 'name': ls.name, 'feat_is_mesh_id': ls.features[0]['source'] == m.id}
ls2 = slicing.mesh_plane_intersection(m, D['plane2']['point'], D['plane2']['normal'], name='oblique')
out['sec2'] = {'parts': len(ls2.parts), 'length': ls2.length(), 'nv': ls2.n_vertices, 'verts': list(ls2.vertices), 'parts_list': ls2.parts}
m2 = slicing.isosurface(nanlist(D['f2']), D['c2'], D['o2'], D['sp2'], iso=0.25, color=[1, 2, 3])
out['m2'] = {'nv': m2.n_vertices, 'nt': m2.n_triangles, 'verts': list(m2.vertices), 'tris': list(m2.triangles), 'color': m2.color}
segs = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)), ((2.0, 0.0, 0.0), (1.0, 0.0, 0.0)), ((5.0, 5.0, 0.0), (5.0, 6.0, 0.0)), ((2.0, 0.0, 0.0), (2.0, 1.0, 0.0)), ((2.0, 1.0, 0.0), (0.0, 0.0, 1e-9))]
out['chains'] = slicing.chain_segments(segs)
out['basis'] = [slicing.plane_basis((0, 0, 0), nrm) for nrm in ((0, 0, 1), (0, 0, -3), (1, 1, 0), (0.2, -0.4, 0.7))]
out['section_plane'] = slicing.section_plane((10.0, 20.0), (40.0, 60.0))
out['tpc'] = slicing.to_plane_coords((3.0, 4.0, 5.0), (1.0, 1.0, 1.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
`, { field: list(field), count: [n, n, n], origin, spacing, plane, plane2, f2: list(f2), c2, o2, sp2 }));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  let calls = 0;
  const [m, ms] = await timed(() => E.isosurface(field, [n, n, n], origin, spacing, { iso: 0, name: 'sphere', onProgress: () => calls++ }));
  cmp('isosurface sphere 21^3: vertex / triangle counts', [m.nVertices, m.nTriangles], [P.nv, P.nt], 0, `(${ms.toFixed(1)} ms, ${calls} progress calls)`);
  cmp('isosurface sphere: vertex coordinates identical', m.vertices, P.verts, 1e-12);
  cmp('isosurface sphere: triangle indices identical', m.triangles, P.tris, 0);
  cmp('isosurface sphere: metadata', [m.metadata.iso, m.role], [P.iso, P.role], 0);
  let outward = 0;
  for (let t = 0; t < m.triangles.length; t += 3) {
    const a = m.triangles[t], b = m.triangles[t + 1], c = m.triangles[t + 2], V = m.vertices;
    const ux = V[3 * b] - V[3 * a], uy = V[3 * b + 1] - V[3 * a + 1], uz = V[3 * b + 2] - V[3 * a + 2];
    const wx = V[3 * c] - V[3 * a], wy = V[3 * c + 1] - V[3 * a + 1], wz = V[3 * c + 2] - V[3 * a + 2];
    const nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
    const cx = (V[3 * a] + V[3 * b] + V[3 * c]) / 3, cy = (V[3 * a + 1] + V[3 * b + 1] + V[3 * c + 1]) / 3, cz = (V[3 * a + 2] + V[3 * b + 2] + V[3 * c + 2]) / 3;
    if (nx * cx + ny * cy + nz * cz > 0) outward++;
  }
  record('isosurface sphere: all triangles outward', outward === m.nTriangles, `${outward}/${m.nTriangles}`);
  const ls = E.meshPlaneIntersection(m, plane.point, plane.normal);
  const expected = 2 * Math.PI * Math.sqrt(7.3 ** 2 - 0.3 ** 2);
  record('mesh_plane_intersection: one closed loop', ls.parts.length === 1 && P.sec.parts === 1 && ls.parts.every(p => { const a = ls.vertex(p[0]), b = ls.vertex(p[p.length - 1]); return a[0] === b[0] && a[1] === b[1] && a[2] === b[2]; }), `parts js=${ls.parts.length} py=${P.sec.parts} closed=${P.sec.closed}`);
  record('mesh_plane_intersection: length within 0.1% of Python', Math.abs(ls.length() - P.sec.length) <= 1e-3 * P.sec.length, `js=${ls.length().toFixed(6)} py=${P.sec.length.toFixed(6)} analytic=${expected.toFixed(3)}`);
  cmp('mesh_plane_intersection: vertices / metadata', { nv: ls.nVertices, plane: ls.metadata.plane, name: ls.name, feat_is_mesh_id: ls.features[0].source === m.id }, { nv: P.sec.nv, plane: P.sec.plane, name: P.sec.name, feat_is_mesh_id: P.sec.feat_is_mesh_id }, 1e-12);
  const ls2 = E.meshPlaneIntersection(m, plane2.point, plane2.normal, 'oblique');
  cmp('mesh_plane_intersection oblique plane: chains identical', { parts: ls2.parts.length, length: ls2.length(), nv: ls2.nVertices, verts: ls2.vertices, parts_list: ls2.parts }, P.sec2, 1e-9);
  const m2 = E.isosurface(f2, c2, o2, sp2, { iso: 0.25, color: [1, 2, 3] });
  cmp('isosurface through nodes (t=0/1 reuse), NaN hole, anisotropic spacing, iso 0.25', { nv: m2.nVertices, nt: m2.nTriangles, verts: m2.vertices, tris: m2.triangles, color: m2.color }, P.m2, 1e-12);
  const segs = [[[0, 0, 0], [1, 0, 0]], [[2, 0, 0], [1, 0, 0]], [[5, 5, 0], [5, 6, 0]], [[2, 0, 0], [2, 1, 0]], [[2, 1, 0], [0, 0, 1e-9]]];
  cmp('chain_segments (greedy, loop snap)', E.chainSegments(segs), P.chains, 1e-12);
  cmp('plane_basis / section_plane / to_plane_coords', {
    basis: [[0, 0, 1], [0, 0, -3], [1, 1, 0], [0.2, -0.4, 0.7]].map(nrm => { const b = E.planeBasis([0, 0, 0], nrm); return [b.n, b.u, b.v]; }),
    sp: (() => { const s = E.sectionPlane([10, 20], [40, 60]); return [s.point, s.normal, s.u, s.length]; })(),
    tpc: E.toPlaneCoords([3, 4, 5], [1, 1, 1], [1, 0, 0], [0, 0, 1]),
  }, { basis: P.basis, sp: P.section_plane, tpc: P.tpc }, 1e-12);
  return m;
}

/* ========================================================== 5. workings */
async function testWorkings() {
  section('5. workings (adit / shaft / drift / raise / decline / stope / GeoJSON)');
  const terrain = new GM.Grid2D({ nx: 12, ny: 10, x0: 512000, y0: 4912000, dx: 40, dy: 40, name: 'terrain' });
  for (let j = 0; j < 10; j++) for (let i = 0; i < 12; i++) terrain.values[j * 12 + i] = 1500 + 3 * i - 2.5 * j + Math.sin(i * j);
  const outline = [[512120, 4912140], [512180, 4912150], [512200, 4912200], [512170, 4912180], [512150, 4912230], [512110, 4912190], [512120, 4912140]];
  const build = (ws, W) => {
    W.addAdit(ws, [512050.5, 4912060.25, 1490], 47.5, 850, { grade_pct: 0.5, units_in: 'ft', terrain, name: 'No. 2 adit', level: '2', confidence: 'surveyed', source: { doc: 'USBM 1942', page: 3 } });
    W.addShaft(ws, [512200, 4912200, 1480], 300, { dip_deg: 70, azimuth_deg: 135, units_in: 'ft', terrain, name: 'Main shaft', mine: 'Lucky Boy' });
    W.addShaft(ws, [512210.5, 4912150.5, 1420], 40, { name: 'Winze 1', kind: 'winze', level_z: 1420, terrain });
    W.addLevelWorking(ws, [[512100, 4912100], [512140, 4912130], [512190, 4912135], [512260, 4912180]], 1420, { kind: 'drift', name: '300 level drift', level: '300', width_m: 2.2 });
    W.addLevelWorking(ws, [[512140, 4912130], [512150, 4912090]], 1420, { kind: 'crosscut', units_in: 'ft' });
    W.addRaise(ws, [512190, 4912135, 1420], [512192, 4912137, 1465], { name: 'Raise A', confidence: 'inferred' });
    W.addDecline(ws, [512000, 4912000, 1500], [[200, 120, -12], [290, 80, -10], [20, 50, -8.5]], { name: 'Decline', units_in: 'ft' });
    W.addRaise(ws, [512000, 4912000, 1400], [512000, 4912000, 1430], { kind: 'bogus-type' });
  };
  const wsJ = E.newWorkings('Test workings', 'Lucky Boy');
  build(wsJ, E);
  const image = new GM.ImagePlane({ image: 'plan.png', width: 800, height: 600, plane: 'plan', name: 'level plan' });
  E.georefPlanFromScale(image, [120, 340], [512080, 4912120], 0.75, { rotation_deg: 12, elevation: 1420 });
  const pixels = [[120, 340], [400, 300], [650, 120]];
  const sec = E.sectionImage('sec.png', 1000, 500, [512000, 4912000], [512400, 4912300], 1550, 1300, { name: 'long section' });
  const [P, pyMs] = await timed(() => py(`
terrain = grid(D['terrain'])
ws = workings.new_workings('Test workings', mine='Lucky Boy')
workings.add_adit(ws, (512050.5, 4912060.25, 1490.0), 47.5, 850.0, grade_pct=0.5, units_in='ft', terrain=terrain, name='No. 2 adit', level='2', confidence='surveyed', source={'doc': 'USBM 1942', 'page': 3})
workings.add_shaft(ws, (512200.0, 4912200.0, 1480.0), 300.0, dip_deg=70.0, azimuth_deg=135.0, units_in='ft', terrain=terrain, name='Main shaft', mine='Lucky Boy')
workings.add_shaft(ws, (512210.5, 4912150.5, 1420.0), 40.0, name='Winze 1', kind='winze', level_z=1420.0, terrain=terrain)
workings.add_level_working(ws, [(512100.0, 4912100.0), (512140.0, 4912130.0), (512190.0, 4912135.0), (512260.0, 4912180.0)], 1420.0, kind='drift', name='300 level drift', level='300', width_m=2.2)
workings.add_level_working(ws, [(512140.0, 4912130.0), (512150.0, 4912090.0)], 1420.0, kind='crosscut', units_in='ft')
workings.add_raise(ws, (512190.0, 4912135.0, 1420.0), (512192.0, 4912137.0, 1465.0), name='Raise A', confidence='inferred')
workings.add_decline(ws, (512000.0, 4912000.0, 1500.0), [(200.0, 120.0, -12.0), (290.0, 80.0, -10.0), (20.0, 50.0, -8.5)], name='Decline', units_in='ft')
workings.add_raise(ws, (512000.0, 4912000.0, 1400.0), (512000.0, 4912000.0, 1430.0), kind='bogus-type')
out = {'verts': list(ws.vertices), 'segments': list(ws.segments), 'parts': ws.parts, 'features': ws.features, 'meta': ws.metadata, 'role': ws.role, 'color': ws.color, 'name': ws.name}
sp = workings.stope_prism(D['outline'], 1400.0, 1425.0, name='Stope 3', level='300', confidence='inferred')
out['stope'] = {'verts': list(sp.vertices), 'tris': list(sp.triangles), 'meta': sp.metadata, 'color': sp.color, 'role': sp.role, 'name': sp.name}
sq = workings.stope_prism([(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)], 5.0, 8.0, color=[9, 9, 9])   # clockwise -> reversed
out['stope_cw'] = {'verts': list(sq.vertices), 'tris': list(sq.triangles), 'color': sq.color}
out['ear'] = [workings._ear_clip([tuple(p) for p in ring]) for ring in D['rings']]
out['area'] = [workings._signed_area([tuple(p) for p in ring]) for ring in D['rings']]
out['summary'] = workings.summary(ws)
out['geojson'] = workings.to_geojson(ws, {'zone': 11, 'north': True})
out['geojson_s'] = workings.to_geojson(ws, {'zone': 33, 'north': False})
pp = workings.portals_points(ws)
out['portals'] = {'xyz': list(pp.xyz), 'attrs': pp.attributes, 'name': pp.name, 'color': pp.color}
img = ImagePlane('plan.png', 800, 600, plane='plan', name='level plan')
workings.georef_plan_from_scale(img, (120, 340), (512080.0, 4912120.0), 0.75, rotation_deg=12.0, elevation=1420.0)
out['control'] = img.control
out['trace'] = workings.trace_to_world(img, D['pixels'])
out['trace_z'] = workings.trace_to_world(img, D['pixels'], level_z=1333.0)
sec = workings.section_image('sec.png', 1000, 500, (512000.0, 4912000.0), (512400.0, 4912300.0), 1550.0, 1300.0, name='long section')
out['trace_sec'] = workings.trace_to_world(sec, D['pixels'])
out['level_from_sec'] = workings.level_from_section(sec, 250, 100)
`, { terrain: gridJSON(terrain), outline, pixels, rings: [outline.slice(0, -1), [[0, 0], [4, 0], [4, 1], [1, 1], [1, 3], [4, 3], [4, 4], [0, 4]], [[0, 0], [1, 0], [2, 0], [2, 2]]] }));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  cmp('add_adit / add_shaft / add_level_working / add_raise / add_decline geometry', wsJ.vertices, P.verts, 1e-9);
  cmp('workings segments / parts', { segments: wsJ.segments, parts: wsJ.parts }, { segments: P.segments, parts: P.parts }, 0);
  cmp('workings features (schema attributes)', wsJ.features, P.features, 1e-12);
  cmp('new_workings metadata / role / colour', { meta: wsJ.metadata, role: wsJ.role, color: wsJ.color, name: wsJ.name }, { meta: P.meta, role: P.role, color: P.color, name: P.name }, 0);
  const sp = E.stopePrism(outline, 1400, 1425, { name: 'Stope 3', level: '300', confidence: 'inferred' });
  cmp('stope_prism (concave outline, ear clipping): geometry', { verts: sp.vertices, tris: sp.triangles }, { verts: P.stope.verts, tris: P.stope.tris }, 1e-9);
  cmp('stope_prism: metadata / colour / role', { meta: sp.metadata, color: sp.color, role: sp.role, name: sp.name }, { meta: P.stope.meta, color: P.stope.color, role: P.stope.role, name: P.stope.name }, 1e-12);
  const sq = E.stopePrism([[0, 0], [0, 10], [10, 10], [10, 0]], 5, 8, { color: [9, 9, 9] });
  cmp('stope_prism (clockwise outline reversed)', { verts: sq.vertices, tris: sq.triangles, color: sq.color }, P.stope_cw, 1e-12);
  const rings = [outline.slice(0, -1), [[0, 0], [4, 0], [4, 1], [1, 1], [1, 3], [4, 3], [4, 4], [0, 4]], [[0, 0], [1, 0], [2, 0], [2, 2]]];
  cmp('_ear_clip / _signed_area', { ear: rings.map(r => E.earClip(r)), area: rings.map(r => E.signedArea(r)) }, { ear: P.ear, area: P.area }, 1e-12);
  cmp('workings summary (by type / by level)', E.workingsSummary(wsJ), P.summary, 1e-9);
  const gj = E.workingsToGeoJSON(wsJ, { zone: 11, north: true });
  cmp('to_geojson (UTM 11N -> lon/lat, 1e-7)', gj, P.geojson, 1e-7);
  const lonlat = gj.features.flatMap(f => f.geometry.coordinates.map(c => c.slice(0, 2)));
  const lonlatPy = P.geojson.features.flatMap(f => f.geometry.coordinates.map(c => c.slice(0, 2)));
  let maxd = 0;
  lonlat.forEach((c, i) => { maxd = Math.max(maxd, Math.abs(c[0] - lonlatPy[i][0]), Math.abs(c[1] - lonlatPy[i][1])); });
  record('to_geojson lon/lat absolute difference <= 1e-7', maxd <= 1e-7, `max |dlon,dlat| = ${maxd.toExponential(2)}`);
  cmp('to_geojson (southern hemisphere zone 33S)', E.workingsToGeoJSON(wsJ, { zone: 33, north: false }), P.geojson_s, 1e-7);
  const pp = E.portalsPoints(wsJ);
  cmp('portals_points', { xyz: pp.xyz, attrs: pp.attributes, name: pp.name, color: pp.color }, P.portals, 1e-12);
  cmp('georef_plan_from_scale control points', image.control, P.control, 1e-12);
  cmp('trace_to_world (plan, image elevation / level_z / section)', { a: E.traceToWorld(image, pixels), b: E.traceToWorld(image, pixels, 1333), c: E.traceToWorld(sec, pixels), d: E.levelFromSection(sec, 250, 100) }, { a: P.trace, b: P.trace_z, c: P.trace_sec, d: P.level_from_sec }, 1e-12);
}

/* ============================================================ 6. worker */
async function testWorker(stratInput) {
  section('6. worker + EngineClient (node worker_threads, same file as the browser worker)');
  const R = rng(99);
  const ps = new GM.PointSet({ name: 'pts', role: 'points' });
  for (let i = 0; i < 60; i++) ps.add(R() * 500, R() * 400, 0, { val: Math.sin(i / 5) + R() * 0.1 });
  const values = ps.numeric('val');
  const direct = E.gridFromPoints(ps, values, { method: 'idw', n: 20, params: { max_points: 6 } });
  const directRbf = E.gridFromPoints(ps, values, { method: 'rbf', n: 20, kernel: 'thin_plate' });
  // (a) EngineClient from a URL (node path: dynamic import of worker_threads)
  const client = new E.EngineClient(WORKER_URL);
  try {
    let progress = [];
    const [g, ms] = await timed(() => client.call('gridFromPoints', { points: ps, values, method: 'idw', n: 20, params: { max_points: 6 } }, f => progress.push(f)));
    record('EngineClient(url) uses a worker thread', client.usingWorker, `usingWorker=${client.usingWorker}`);
    record('worker gridFromPoints round-trips a Grid2D', g instanceof GM.Grid2D && g.nx === direct.nx && g.ny === direct.ny && g.values.length === direct.values.length, `${g && g.constructor.name} ${g && g.nx}x${g && g.ny} (${ms.toFixed(0)} ms)`);
    let maxd = 0; for (let i = 0; i < direct.values.length; i++) maxd = Math.max(maxd, Math.abs(g.values[i] - direct.values[i]));
    record('worker result identical to direct call', maxd === 0 && g.metadata.interpolation.method === 'idw', `max |diff| = ${maxd}`);
    record('worker sends progress 0..1', progress.length > 0 && progress[progress.length - 1] === 1 && progress.every(f => f >= 0 && f <= 1), `${progress.length} events`);
    const g2 = await client.call('gridFromPoints', { points: GM.packObject(ps), values: Array.from(values), method: 'rbf', n: 20, params: { kernel: 'thin_plate' } });
    maxd = 0; for (let i = 0; i < directRbf.values.length; i++) maxd = Math.max(maxd, Math.abs(g2.values[i] - directRbf.values[i]));
    record('worker accepts pre-packed objects and plain arrays (rbf)', maxd === 0, `max |diff| = ${maxd}`);
    // nested packed objects one level deep (units[].base) + result object of objects
    const { topo, units } = stratInput;
    const d = E.buildStratigraphy(topo, units, { method: 'idw', max_points: 6 });
    const w = await client.call('buildStratigraphy', { topo, units, method: 'idw', params: { max_points: 6 } });
    const same = w.strat instanceof GM.StratModel && w.topo instanceof GM.Grid2D && w.bases.length === d.bases.length && w.bases.every((g, k) => (g == null) === (d.bases[k] == null) && (g == null || g.values.every((v, i) => v === d.bases[k].values[i] || (v !== v && d.bases[k].values[i] !== d.bases[k].values[i]))));
    record('worker buildStratigraphy (nested packed units, packed result tree)', same, `${w.bases.filter(Boolean).length} bases, units=${w.strat.units.length}`);
    const field = sphereField(31, 11.2);
    const mw = await client.call('isosurface', { field, count: [31, 31, 31], origin: [-15, -15, -15], spacing: [1, 1, 1], iso: 0 });
    const md = E.isosurface(field, [31, 31, 31], [-15, -15, -15], [1, 1, 1]);
    record('worker isosurface (typed arrays transferred back)', mw instanceof GM.Mesh && mw.nVertices === md.nVertices && mw.nTriangles === md.nTriangles && mw.vertices.every((v, i) => v === md.vertices[i]), `${mw.nVertices} verts`);
    const vgw = await client.call('fitVariogram', { experimental: await client.call('empiricalVariogram', { points: ps, values, dim: 2 }) });
    record('worker empiricalVariogram -> fitVariogram (plain JSON results)', !!(vgw && vgw.structures && vgw.structures[0].range > 0), JSON.stringify(vgw));
    let err = null;
    try { await client.call('noSuchOp', {}); } catch (e) { err = e; }
    record('worker rejects unknown op with an Error', err instanceof Error && /unknown op/.test(err.message), err && err.message);
    err = null;
    try { await client.call('gridFromPoints', { points: [], values: [], method: 'rbf' }); } catch (e) { err = e; }
    record('worker forwards engine exceptions', err instanceof Error && err.message.length > 0, err && err.message);
  } finally { client.terminate(); }
  // (b) an explicitly constructed worker_threads Worker (the call site the spec describes)
  const worker = new Worker(WORKER_URL, { type: 'module' });
  const client2 = new E.EngineClient(worker);
  try {
    const g = await client2.call('gridFromPoints', { points: ps, values, method: 'nn', n: 10 });
    const dn = E.gridFromPoints(ps, values, { method: 'nn', n: 10 });
    record('EngineClient(new Worker(url, {type:module})) round trip (nn grid)', g instanceof GM.Grid2D && g.values.every((v, i) => v === dn.values[i]), `${g.nx}x${g.ny}`);
    const bm = E.createBlockModel([0, 0, 0, 50, 40, 30], 10);
    const s = new GM.PointSet({ name: 's' });
    for (let i = 0; i < 25; i++) s.add(R() * 50, R() * 40, R() * 30, { au: R() });
    const bmw = await client2.call('estimate', { bm, samples: s, value: 'au', method: 'idw', max_points: 5 });
    record('worker estimate returns the BlockModel with new attributes', bmw instanceof GM.BlockModel && 'au_est' in bmw.attributes && bmw.metadata.estimates.length === 1, Object.keys(bmw.attributes).join(','));
    const bmd = E.estimate(E.createBlockModel([0, 0, 0, 50, 40, 30], 10), s, 'au', { method: 'idw', max_points: 5 });
    record('worker keeps f64 block attributes (no f32 downcast in transit)', bmw.attributes.au_est.values instanceof Float64Array && bmw.attributes.au_est.values.every((v, i) => v === bmd.attributes.au_est.values[i] || (v !== v && bmd.attributes.au_est.values[i] !== bmd.attributes.au_est.values[i])), bmw.attributes.au_est.values.constructor.name);
    const tg = await client2.call('thicknessGrid', { top: stratInput.topo, base: E.buildStratigraphy(stratInput.topo, stratInput.units, { method: 'idw' }).bases[1] });
    record('worker keeps f64 values of property grids', tg instanceof GM.Grid2D && tg.role === 'property' && tg.values instanceof Float64Array, `${tg.role} ${tg.values.constructor.name}`);
    const tr = await client2.call('desurvey', { drillholes: new GM.Drillholes({ name: 'dh', collars: [{ hole: 'A', x: 0, y: 0, z: 100, depth: 50 }], surveys: [{ hole: 'A', depth: 0, azimuth: 45, dip: 60 }, { hole: 'A', depth: 50, azimuth: 50, dip: 55 }] }), step: 10 });
    record('worker desurvey (Drillholes -> traces)', tr && tr.A && tr.A.length === 6 && Math.abs(tr.A[5][0] - 50) < 1e-9, `${tr && tr.A && tr.A.length} stations`);
  } finally { client2.terminate(); }
  // (c) main-thread fallback
  const local = new E.EngineClient(null);
  const gl = await local.call('gridFromPoints', { points: ps, values, method: 'idw', n: 20, params: { max_points: 6 } });
  record('EngineClient(null) main-thread fallback gives the same grid', !local.usingWorker && gl.values.every((v, i) => v === direct.values[i]));
  local.terminate();
}

/* ================================================================ 7. kit */
async function testKitAndSlicing() {
  section('7. kit helpers + block / lineset slicing');
  const ring = [[-20, 10], [50, -15], [130, 40], [90, 120], [20, 95], [-30, 60], [-20, 10]];
  const tri = { verts: [[0, 0], [100, 0], [60, 80], [0, 70]], tris: [[0, 1, 2], [0, 2, 3]] };
  const path = [[0, 0], [10, 0], [10, 25.5], [3, 30]];
  const bm = E.createBlockModel([0, 0, 0, 100, 80, 40], [10, 10, 5], { azimuth: 0 });
  const vals = new Float64Array(bm.n); for (let i = 0; i < bm.n; i++) vals[i] = (i % 11) / 10;
  vals[7] = NaN;
  bm.addAttribute('g', vals);
  bm.addAttribute('cat', Array.from({ length: bm.n }, (_, i) => (i % 3 ? 'ore' : 'waste')), 'category');
  const bmr = E.createBlockModel([0, 0, 0, 100, 80, 40], [10, 10, 5], { azimuth: 25 });
  bmr.addAttribute('g', vals);
  const ls = new GM.LineSet({ name: 'lines', role: 'workings', color: [1, 2, 3] });
  ls.addPolyline([[0, -30, 0], [10, -5, 5], [20, 4, 10], [30, 40, 12], [40, -2, 14], [50, -40, 16]], { name: 'a' });
  ls.addPolyline([[0, 100, 0], [5, 120, 0]], { name: 'far' });
  ls.addPolyline([[0, 3, 0], [100, 3, 0]], { name: 'inside' });
  ls.addPolyline([[0, 50, 0], [0, -50, 0]], { name: 'through' });
  const grid = new GM.Grid2D({ nx: 8, ny: 6, x0: 0, y0: 0, dx: 10, dy: 10, name: 'g' });
  for (let i = 0; i < grid.values.length; i++) grid.values[i] = i % 9 === 4 ? NaN : 100 + i;
  const [P, pyMs] = await timed(() => py(`
out = {}
out['clip'] = [kit.clip_ring_rect(D['ring'], *rect) for rect in D['rects']]
pts, tris = kit.subdivide_triangles(D['tri']['verts'], D['tri']['tris'], 30.0)
out['subdiv'] = {'pts': pts, 'tris': tris}
out['dens'] = kit.densify(D['path'], 4.0)
out['dens1'] = kit.densify(D['path'][:1], 4.0)
out['colors'] = [kit._unit_color(u) for u in ({'id': 'Qal'}, {'nm': 'Belt Supergroup'}, {'id': 'Tv', 'nm': 'x'}, {}, {'id': 'Δ-unit'})]
bm = BlockModel(D['bm']['origin'], D['bm']['size'], D['bm']['count'], azimuth=D['bm']['az'])
bm.add_attribute('g', D['bm']['g'])
bmr = BlockModel(D['bm']['origin'], D['bm']['size'], D['bm']['count'], azimuth=25.0)
bmr.add_attribute('g', D['bm']['g'])
s = slicing.blockmodel_plane_sample(bm, 'g', (50.0, 40.0, 20.0), (1.0, 0.0, 0.0))
out['sample'] = {k: s[k] for k in ('width', 'height', 'values', 'corners', 'u', 'v', 'du', 'dv', 'extent')}
s2 = slicing.blockmodel_plane_sample(bm, 'g', (50.0, 40.0, 20.0), (0.3, 0.6, 0.2), extent=(-60.0, 60.0, -25.0, 25.0), resolution=4.0)
out['sample2'] = {k: s2[k] for k in ('width', 'height', 'values', 'corners', 'u', 'v', 'du', 'dv', 'extent')}
s3 = slicing.blockmodel_plane_sample(bmr, 'g', (50.0, 40.0, 20.0), (0.0, 1.0, 0.0), resolution=3.0)
out['sample3'] = {k: s3[k] for k in ('width', 'height', 'values')}
ls = lineset(D['ls'])
for name, hw, proj in (('near', 5.0, True), ('near_noproj', 5.0, False), ('wide', 45.0, True)):
    n = slicing.lineset_near_plane(ls, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), hw, project=proj)
    out[name] = {'verts': list(n.vertices), 'parts': n.parts, 'features': n.features, 'role': n.role, 'color': n.color, 'name': n.name}
g = grid(D['grid'])
out['profile'] = slicing.grid_profile(g, (-5.0, 3.0), (75.0, 52.0), n=37)
pl = slicing.profile_lineset(g, (-5.0, 3.0), (75.0, 52.0), n=37, lift=2.0)
out['profile_ls'] = {'verts': list(pl.vertices), 'parts': pl.parts, 'features': pl.features, 'name': pl.name}
out['on_line'] = slicing.grid_to_points_on_line(g, (-5.0, 3.0), (75.0, 52.0), n=12)
`, { ring, rects: [[0, 0, 100, 100], [-100, -100, 200, 200], [60, 60, 70, 70], [0, 0, 10, 5]], tri, path, bm: { origin: bm.origin, size: bm.blockSize, count: bm.count, az: bm.azimuth, g: list(vals) }, ls: lsJSON(ls), grid: gridJSON(grid) }));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  cmp('clip_ring_rect (Sutherland-Hodgman)', [[0, 0, 100, 100], [-100, -100, 200, 200], [60, 60, 70, 70], [0, 0, 10, 5]].map(r => E.clipRingRect(ring, ...r)), P.clip, 1e-12);
  const sd = E.subdivideTriangles(tri.verts, tri.tris, 30);
  cmp('subdivide_triangles (shared midpoints, same order)', { pts: sd.points, tris: sd.triangles }, P.subdiv, 1e-12);
  cmp('densify', [E.densify(path, 4), E.densify(path.slice(0, 1), 4)], [P.dens, P.dens1], 1e-12);
  cmp('_unit_color (stable hash)', [{ id: 'Qal' }, { nm: 'Belt Supergroup' }, { id: 'Tv', nm: 'x' }, {}, { id: 'Δ-unit' }].map(E.unitColor), P.colors, 0);
  const s = E.blockmodelPlaneSample(bm, 'g', [50, 40, 20], [1, 0, 0]);
  cmp('blockmodel_plane_sample (auto extent)', { width: s.width, height: s.height, values: s.values, corners: s.corners, u: s.u, v: s.v, du: s.du, dv: s.dv, extent: s.extent }, P.sample, 1e-12);
  const s2 = E.blockmodelPlaneSample(bm, 'g', [50, 40, 20], [0.3, 0.6, 0.2], { extent: [-60, 60, -25, 25], resolution: 4 });
  cmp('blockmodel_plane_sample (oblique, extent, resolution)', { width: s2.width, height: s2.height, values: s2.values, corners: s2.corners, u: s2.u, v: s2.v, du: s2.du, dv: s2.dv, extent: s2.extent }, P.sample2, 1e-12);
  const s3 = E.blockmodelPlaneSample(bmr, 'g', [50, 40, 20], [0, 1, 0], { resolution: 3 });
  cmp('blockmodel_plane_sample (rotated block model)', { width: s3.width, height: s3.height, values: s3.values }, P.sample3, 1e-12);
  const sc = E.blockmodelPlaneSample(bm, 'cat', [50, 40, 20], [1, 0, 0]);
  record('blockmodel_plane_sample on a category attribute (JS extension)', Array.isArray(sc.values) && sc.values.some(v => v === 'ore') && sc.values.some(v => v === null));
  for (const [name, hw, proj] of [['near', 5, true], ['near_noproj', 5, false], ['wide', 45, true]]) {
    const nr = E.linesetNearPlane(ls, [0, 0, 0], [0, 1, 0], hw, { project: proj });
    cmp(`lineset_near_plane (half width ${hw}, project=${proj})`, { verts: nr.vertices, parts: nr.parts, features: nr.features, role: nr.role, color: nr.color, name: nr.name }, P[name], 1e-12);
  }
  cmp('grid_profile', E.gridProfile(grid, [-5, 3], [75, 52], 37), P.profile, 1e-12);
  const pl = E.profileLineSet(grid, [-5, 3], [75, 52], { n: 37, lift: 2 });
  cmp('profile_lineset (runs split at no-data)', { verts: pl.vertices, parts: pl.parts, features: pl.features, name: pl.name }, P.profile_ls, 1e-12);
  cmp('grid_to_points_on_line', E.gridToPointsOnLine(grid, [-5, 3], [75, 52], 12), P.on_line, 1e-12);
  // solver sanity (pure JS)
  const A = [[4, 1, 2], [1, 5, 3], [2, 3, 6]], b = [1, 2, 3];
  const x = E.solveDense(A, b);
  const res = A.map((row, i) => row.reduce((s, v, j) => s + v * x[j], 0) - b[i]);
  record('solveDense residual', Math.max(...res.map(Math.abs)) < 1e-12, `max |Ax-b| = ${Math.max(...res.map(Math.abs)).toExponential(2)}`);
  record('pyRound ties-to-even / decimals', E.pyRound(2.5) === 2 && E.pyRound(3.5) === 4 && E.pyRound(-2.5) === -2 && E.pyRound(0.125, 2) === 0.12 && E.pyRound(0.375, 2) === 0.38 && E.pyRound(1234.5678, 2) === 1234.57 && E.pyRound(-0.4) === 0);
}

/* ============================================================ 8. bench */
async function bench() {
  section('8. timings');
  const R = rng(2024);
  // RBF fit n = 1500 (3-D thin-plate, linear drift) + predict on 20^3
  const n = 1500, pts = new Float64Array(n * 3), vals = new Float64Array(n);
  for (let i = 0; i < n; i++) { const x = R() * 1000, y = R() * 800, z = R() * 300; pts[3 * i] = x; pts[3 * i + 1] = y; pts[3 * i + 2] = z; vals[i] = Math.sin(x / 150) * Math.cos(y / 120) + z / 300; }
  const [rbf, msFit] = await timed(() => new E.RBF({ kernel: 'thin_plate', drift: 'linear' }).fit(pts, vals));
  const [fieldR, msPred] = await timed(() => E.scalarFieldFromRBF(rbf, [0, 0, 0, 1000, 800, 300], [1000 / 19, 800 / 19, 300 / 19]));
  let maxResid = 0;
  const back = rbf.predict(pts.subarray(0, 3 * 50));
  for (let i = 0; i < 50; i++) maxResid = Math.max(maxResid, Math.abs(back[i] - vals[i]));
  record(`RBF fit n=1500 (dense LU 1504^2): ${msFit.toFixed(0)} ms`, msFit < 15000, `interpolation residual at centres ${maxResid.toExponential(2)}; predict 20^3 nodes ${msPred.toFixed(0)} ms`, msFit);
  // kriging of 20k blocks
  const bm = E.createBlockModel([0, 0, 0, 400, 250, 100], [10, 10, 5]);      // 40 x 25 x 20 = 20000
  const samples = new GM.PointSet({ name: 's' });
  for (let i = 0; i < 600; i++) samples.add(R() * 400, R() * 250, R() * 100, { au: Math.exp(R() - 0.5) });
  const vg = new E.Variogram({ nugget: 0.1, structures: [{ model: 'spherical', sill: 0.9, range: 80 }] });
  const [, msOk] = await timed(() => E.estimate(bm, samples, 'au', { method: 'ok', variogram: vg, max_points: 16, radius: 120 }));
  const [, msOk24] = await timed(() => E.estimate(bm, samples, 'au', { method: 'ok', variogram: vg, max_points: 24, out_name: 'au24' }));
  const [, msIdw] = await timed(() => E.estimate(bm, samples, 'au', { method: 'idw', max_points: 16, out_name: 'idw' }));
  let nEst = 0; for (const v of bm.attributes.au_est.values) if (v === v) nEst++;
  record(`ordinary kriging ${bm.n} blocks, 16 nb, radius 120: ${msOk.toFixed(0)} ms`, msOk < 20000, `${nEst} blocks estimated; 24 nb no radius ${msOk24.toFixed(0)} ms; idw 16 nb ${msIdw.toFixed(0)} ms`, msOk);
  // isosurface 100^3
  const N = 100, field = sphereField(N, 38.5);
  E.isosurface(field, [N, N, N], [0, 0, 0], [1, 1, 1]);     // warm up the JIT once
  const [mesh, msIso] = await timed(() => E.isosurface(field, [N, N, N], [0, 0, 0], [1, 1, 1]));
  record(`isosurface 100^3 nodes: ${msIso.toFixed(0)} ms (< 2000 required)`, msIso < 2000, `${mesh.nVertices} vertices, ${mesh.nTriangles} triangles`, msIso);
  const [, msSec] = await timed(() => E.meshPlaneIntersection(mesh, [50.2, 49.7, 50.1], [0.2, 0.3, 0.93]));
  record(`mesh_plane_intersection of that mesh: ${msSec.toFixed(0)} ms`, true, '', msSec);
}

/* ============================================================ 9. geometry */
async function testGeometry() {
  section('9. geometry (extrude / contours / plane / set elevation / clip to ground / anisotropy unit)');
  const trace = [[0, 0, 1500], [100, 20, 1510], [200, 10, 1520], [300, 40, 1530], [400, 15, 1545]];
  const outline = [[0, 0, 1425], [50, 0, 1425], [50, 40, 1425], [20, 20, 1425], [0, 40, 1425]];
  const sheared = outline.map((p, i) => [p[0], p[1], 1420 + 3 * i]);
  const grid = new GM.Grid2D({ nx: 11, ny: 9, x0: 1000, y0: 2000, dx: 10, dy: 10, name: 'plane', role: 'surface' });
  for (let j = 0; j < 9; j++) for (let i = 0; i < 11; i++) grid.set(i, j, 100 + 2 * i + j + 0.7 * Math.sin(i * 1.3 + j * 0.7));
  const hole = new GM.Grid2D({ nx: 11, ny: 9, x0: 1000, y0: 2000, dx: 10, dy: 10, name: 'holed', role: 'surface', values: grid.values.slice() });
  hole.set(5, 4, NaN); hole.set(6, 4, NaN); hole.set(5, 5, NaN);
  const prop = new GM.Grid2D({ nx: 11, ny: 9, x0: 1000, y0: 2000, dx: 10, dy: 10, name: 'mag', role: 'property', units: 'nT', values: grid.values.map(v => (v - 100) * 3) });
  const saddle = new GM.Grid2D({ nx: 2, ny: 2, x0: 0, y0: 0, dx: 1, dy: 1, name: 'saddle', values: [1, 0, 0, 1] });   // nodes (0,0) and (1,1) high -> case 5
  const topo = new GM.Grid2D({ nx: 5, ny: 5, x0: 950, y0: 1950, dx: 50, dy: 50, name: 'ground', role: 'topography', values: new Float64Array(25).fill(100) });
  const tilt = new GM.Mesh({ name: 'tilt', vertices: [1000, 2000, 80, 1100, 2000, 120, 1100, 2100, 120, 1000, 2100, 80, 1000, 2000, 60], triangles: [0, 1, 2, 0, 2, 3, 3, 0, 4], attributes: { v: { location: 'vertices', values: [1, 2, 3, 4, 5] }, f: { location: 'faces', values: [10, 20, 30] } } });
  const ls = new GM.LineSet({ name: 'traces' });
  ls.addPolyline([[1005, 2005, 0], [1015, 2015, NaN], [1025, 2025, 777]], { confidence: 'sketched' });
  ls.addPolyline([[1035, 2035, 0], [1045, 2045, 0]], { confidence: 'surveyed' });
  ls.addPolyline([[1055, 2055, 0], [10, 10, 0]], { confidence: 'inferred' });          // second vertex outside the grid
  const ps = new GM.PointSet({ name: 'pts' });
  ps.add(1005, 2005, 0, { confidence: 'sketched' }); ps.add(1015, 2015, NaN, { confidence: 'surveyed' }); ps.add(1025, 2025, 500, { confidence: 'surveyed' }); ps.add(1035, 2035, 12, { confidence: 'described' });
  const dh = new GM.Drillholes({ name: 'dh', collars: [{ hole: 'A', x: 1005, y: 2005, z: 0, depth: 50 }, { hole: 'B', x: 1050, y: 2050, z: 300, depth: 50, confidence: 'surveyed' }] });
  const [P, pyMs] = await timed(() => py(`
from geomodel import contours, assay
out = {}
def meshj(m):
    return {'verts': list(m.vertices), 'tris': list(m.triangles), 'role': m.role, 'color': m.color, 'name': m.name, 'meta': m.metadata, 'opacity': m.opacity}
def lsj(l):
    return {'verts': list(l.vertices), 'parts': l.parts, 'features': l.features, 'role': l.role, 'name': l.name}
tr, ol, sh = D['trace'], D['outline'], D['sheared']
out['open'] = meshj(workings.extrude_polyline(tr, 60.0, 175.0, depth=100.0, role='fault', confidence='inferred', name='rib', source={'layer': 'L', 'part': 2}))
out['open_zb'] = meshj(workings.extrude_polyline(tr, 35.0, 350.0, z_bottom=1400.0, role='vein', name='rib2'))
out['sheared'] = meshj(workings.extrude_polyline(sh, 60.0, 90.0, depth=30.0, closed=True, name='prism'))
out['vertical'] = meshj(workings.extrude_polyline(ol, 90.0, 0.0, depth=25.0, closed=True, name='wall'))
out['stope'] = meshj(workings.stope_prism([p[:2] for p in ol], 1400.0, 1425.0, name='wall'))
out['strike'] = workings._polyline_strike(tr)
def err(fn):
    try:
        fn(); return None
    except ValueError as e:
        return str(e)
out['err_az'] = err(lambda: workings.extrude_polyline(tr, 60.0, 84.0, depth=100.0))
out['err_flat'] = err(lambda: workings.extrude_polyline([[p[0], p[1], 0.0] for p in tr], 60.0, 175.0, depth=100.0))
out['err_nan'] = err(lambda: workings.extrude_polyline([[p[0], p[1]] for p in tr], 60.0, 175.0, depth=100.0))
out['err_dip'] = err(lambda: workings.extrude_polyline(tr, 0.0, 175.0, depth=100.0))
out['err_few'] = err(lambda: workings.extrude_polyline(tr[:1], 60.0, 175.0, depth=100.0))
g, h, pg, sd, tp = grid(D['grid']), grid(D['hole']), grid(D['prop']), grid(D['saddle']), grid(D['topo'])
pg.units = 'nT'
out['levels'] = contours.contour_levels(g, 5.0)
out['levels_b'] = contours.contour_levels(g, 4.0, base=1.5)
out['nice'] = [contours.nice_interval(v) for v in (210.0, 37.0, 0.9, 1e4)]
out['contours'] = lsj(contours.contour_grid(g, interval=5.0, index=2, name='c'))
out['contours'].update({'meta': contours.contour_grid(g, interval=5.0, index=2, name='c').metadata['contours']})
out['contours_hole'] = lsj(contours.contour_grid(h, levels=out['levels'], interval=5.0, index=2, name='ch'))
out['contours_prop'] = lsj(contours.contour_grid(pg, levels=[0.0, 30.0, 60.0], drape=tp, lift=2.0, name='cp'))
out['contours_prop_z'] = lsj(contours.contour_grid(pg, levels=[0.0, 30.0], z=1234.0, name='cz'))
out['saddle'] = contours.marching_squares(2, 2, lambda i, j: sd.values[j * 2 + i], sd.node_xy, 0.5)
cx, cy, cz, half, dip, az = 5000.0, 6000.0, 1400.0, 120.0, 55.0, 300.0
vein = {'strike_deg': az - 90.0, 'dip_deg': dip, 'dip_direction_deg': az, 'dip_direction_assumed': False, 'confidence': 'described', 'quote': ''}
placed = [{'start': (cx - 10.0, cy - 5.0, cz - 2.0), 'end': (cx + 10.0, cy + 5.0, cz + 2.0)}]
out['vein'] = list(assay.vein_surface(vein, placed, None, extent=half).vertices)
out['plane'] = meshj(assay.plane_mesh(cx, cy, cz, dip, az, half, 80.0, role='vein', name='pl', from_measurement={'layer': 'S', 'row': 3}))
l = lineset(D['ls'])
st = interp.set_elevation_from(l, g, only='missing')
out['ls_missing'] = {'stats': st, 'verts': list(l.vertices), 'features': [dict(f) for f in l.features], 'meta': dict(l.metadata)}
out['ls_restore'] = {'n': interp.restore_elevation(l), 'verts': list(l.vertices), 'features': [dict(f) for f in l.features]}
l2 = lineset(D['ls'])
out['ls_ns'] = {'stats': interp.set_elevation_from(l2, g, offset=2.0, only='not-surveyed'), 'verts': list(l2.vertices)}
p = pset(D['ps'])
out['ps_ns'] = {'stats': interp.set_elevation_from(p, g, only='not-surveyed'), 'xyz': list(p.xyz), 'zo': p.attributes['z_original'], 'meta': p.metadata}
p2 = pset(D['ps'])
out['ps_all'] = {'stats': interp.set_elevation_from(p2, g), 'xyz': list(p2.xyz), 'zo': p2.attributes['z_original']}
tm = Mesh(D['tilt']['vertices'], D['tilt']['triangles'], attributes={'v': {'location': 'vertices', 'values': farray(D['tilt']['v'])}, 'f': {'location': 'faces', 'values': farray(D['tilt']['f'])}}, name='tilt', oid=D['tilt']['id'])
gm = g.to_mesh()
out['mesh_z'] = [interp.mesh_z_at(gm, x, y) for x, y in ((1012.5, 2007.5), (1000.0, 2000.0), (1099.9, 2079.9), (900.0, 2000.0))]
out['mesh_z_grid'] = [g.sample(x, y) for x, y in ((1012.5, 2007.5), (1000.0, 2000.0), (1099.9, 2079.9))]
p3 = pset(D['ps'])
out['ps_mesh'] = {'stats': interp.set_elevation_from(p3, gm), 'xyz': list(p3.xyz)}
from geomodel.model import Drillholes
d = Drillholes(D['dh']['collars'], [], {}, name='dh')
out['dh'] = {'stats': interp.set_elevation_from(d, g, only='not-surveyed'), 'collars': [dict(c) for c in d.collars]}
out['dh_restore'] = {'n': interp.restore_elevation(d), 'collars': [dict(c) for c in d.collars]}
c = slicing.clip_mesh_to_topography(tm, tp, name='clip')
out['clip'] = {'verts': list(c.vertices), 'tris': list(c.triangles), 'v': list(c.attributes['v']['values']), 'f': list(c.attributes['f']['values']), 'clip': c.metadata['clip'], 'role': c.role}
out['daylight'] = lsj(slicing.daylight_trace(tm, tp))
hi = g.copy_empty(); hi.values = farray(v + 5.0 for v in g.values); hi.name = 'hi'
out['daylight_grid'] = lsj(slicing.daylight_trace(g, hi))
pts, vals, tg = D['rbf']['pts'], D['rbf']['vals'], D['rbf']['tg']
out['rbf_iso'] = {}
for k in interp.RBF_KERNELS:
    a = interp.RBF(kernel=k, epsilon=200.0 if k in ('gaussian', 'multiquadric') else None).fit(pts, vals).predict(tg)
    b = interp.RBF(kernel=k, epsilon=200.0 if k in ('gaussian', 'multiquadric') else None, anisotropy=interp.Anisotropy([1, 1, 1])).fit(pts, vals).predict(tg)
    out['rbf_iso'][k] = max(abs(x - y) for x, y in zip(a, b))
`, { trace, outline, sheared, grid: gridJSON(grid), hole: gridJSON(hole), prop: gridJSON(prop), saddle: gridJSON(saddle), topo: gridJSON(topo), tilt: { id: tilt.id, vertices: Array.from(tilt.vertices), triangles: Array.from(tilt.triangles), v: [1, 2, 3, 4, 5], f: [10, 20, 30] }, ls: lsJSON(ls), ps: psetJSON(ps), dh: { collars: dh.collars }, rbf: rbfData() }));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  const meshJ = m => ({ verts: m.vertices, tris: m.triangles, role: m.role, color: m.color, name: m.name, meta: m.metadata, opacity: m.opacity });
  const lsJ = l => ({ verts: l.vertices, parts: l.parts, features: l.features, role: l.role, name: l.name });
  // -- extrude
  const rib = E.extrudePolyline(trace, { dip: 60, dipAzimuth: 175, depth: 100, role: 'fault', confidence: 'inferred', name: 'rib', source: { layer: 'L', part: 2 } });
  cmp('extrude_polyline open ribbon: vertices / triangles / metadata', meshJ(rib), P.open, 1e-9);
  record('extrude_polyline: schema + honesty note + strike recorded', rib.metadata.schema === 'nwmm-extrude/1' && /projection distance/.test(rib.metadata.note) && Math.abs(rib.metadata.strike_deg - P.strike) < 1e-9, `strike ${rib.metadata.strike_deg.toFixed(2)}°`);
  record('extrude_polyline: bottom ring is trace + (depth / sin dip)·dipVector', (() => { const t = 100 / Math.sin(60 * Math.PI / 180); const dv = [Math.sin(175 * Math.PI / 180) * Math.cos(60 * Math.PI / 180), Math.cos(175 * Math.PI / 180) * Math.cos(60 * Math.PI / 180), -Math.sin(60 * Math.PI / 180)]; let worst = 0; for (let i = 0; i < 5; i++) for (let a = 0; a < 3; a++) worst = Math.max(worst, Math.abs(rib.vertices[3 * i + a] - (trace[i][a] + t * dv[a]))); return worst < 1e-9; })());
  cmp('extrude_polyline with zBottom (bottom ring on a level)', meshJ(E.extrudePolyline(trace, { dip: 35, dipAzimuth: 350, zBottom: 1400, role: 'vein', name: 'rib2' })), P.open_zb, 1e-9);
  cmp('extrude_polyline closed sheared prism (caps + sides)', meshJ(E.extrudePolyline(sheared, { dip: 60, dipAzimuth: 90, depth: 30, closed: true, name: 'prism' })), P.sheared, 1e-9);
  const wall = E.extrudePolyline(outline, { dip: 90, dipAzimuth: 0, depth: 25, closed: true, name: 'wall' });
  cmp('extrude_polyline dip 90 closed: vertical wall matches Python', meshJ(wall), P.vertical, 1e-9);
  const stope = E.stopePrism(outline.map(p => [p[0], p[1]]), 1400, 1425, { name: 'wall' });
  record('extrude_polyline dip 90 closed reproduces stopePrism vertex + triangle order exactly', stope.nVertices === wall.nVertices && stope.nTriangles === wall.nTriangles && stope.vertices.every((v, i) => v === wall.vertices[i]) && stope.triangles.every((v, i) => v === wall.triangles[i]), `${wall.nVertices} verts, ${wall.nTriangles} tris`);
  const thrown = fn => { try { fn(); return null; } catch (e) { return e.message; } };
  const eAz = thrown(() => E.extrudePolyline(trace, { dip: 60, dipAzimuth: 84, depth: 100 }));
  record('extrude_polyline refuses a dip azimuth along strike and prints the strike', !!eAz && /within 20°/.test(eAz) && eAz.includes(P.strike.toFixed(1)) && P.err_az && P.err_az.includes(P.strike.toFixed(1)), eAz);
  const eFlat = thrown(() => E.extrudePolyline(trace.map(p => [p[0], p[1], 0]), { dip: 60, dipAzimuth: 175, depth: 100 }));
  record('extrude_polyline refuses a trace with no elevation (all z = 0)', eFlat === 'the trace has no elevation — drape it on the topography first' && P.err_flat === eFlat, eFlat);
  const eNan = thrown(() => E.extrudePolyline(trace.map(p => [p[0], p[1]]), { dip: 60, dipAzimuth: 175, depth: 100 }));
  record('extrude_polyline refuses a trace with no elevation (all z NaN)', eNan === P.err_flat, eNan);
  record('extrude_polyline refuses dip 0 and a single vertex (both languages)', /dip must be/.test(thrown(() => E.extrudePolyline(trace, { dip: 0, dipAzimuth: 175, depth: 100 })) || '') && /dip must be/.test(P.err_dip || '') && /at least 2/.test(thrown(() => E.extrudePolyline(trace.slice(0, 1), { dip: 60, dipAzimuth: 175, depth: 100 })) || '') && /at least 2/.test(P.err_few || ''));
  // -- contours
  const lv = E.contourLevels(grid, 5);
  cmp('contour_levels (interval, and with a base)', [lv, E.contourLevels(grid, 4, 1.5)], [P.levels, P.levels_b], 0);
  cmp('nice_interval (1/2/5 ladder)', [210, 37, 0.9, 1e4].map(v => E.niceInterval(v)), P.nice, 1e-12);
  const cs = E.contourGrid(grid, null, { interval: 5, index: 2, name: 'c' });
  cmp('contour_grid on a synthetic plane: vertices / parts / features / metadata', Object.assign(lsJ(cs), { meta: cs.metadata.contours }), P.contours, 1e-9);
  const drawn = lv.filter(l => l > grid.zrange()[0]);                 // a level at the minimum has nothing below it
  record('contour_grid: every requested level present, parts carry level / units / source / index', drawn.every(l => cs.features.some(f => f.level === l)) && cs.features.every(f => f.units === 'm' && f.source === 'plane' && typeof f.index === 'boolean') && cs.features.some(f => f.index) && cs.features.some(f => !f.index), `${cs.parts.length} parts for ${lv.length} levels (${drawn.length} above the minimum)`);
  record('contour_grid: a plane gives one open part per level with z = level', cs.parts.length === drawn.length && cs.parts.every((p, k) => p.every(i => cs.vertices[3 * i + 2] === cs.features[k].level)), `${cs.parts.length} parts`);
  const ch = E.contourGrid(hole, lv, { interval: 5, index: 2, name: 'ch' });
  cmp('contour_grid with a NaN hole', lsJ(ch), P.contours_hole, 1e-9);
  record('contour_grid: the hole breaks lines and shortens them, no vertex inside the hole', ch.metadata.contours.n_segments < cs.metadata.contours.n_segments && ch.parts.length >= cs.parts.length && !Array.from({ length: ch.nVertices }, (_, i) => ch.vertex(i)).some(v => v[0] > 1050 && v[0] < 1060 && v[1] > 2040 && v[1] < 2050), `${cs.metadata.contours.n_segments} -> ${ch.metadata.contours.n_segments} segments, ${cs.parts.length} -> ${ch.parts.length} parts`);
  const cp = E.contourGrid(prop, [0, 30, 60], { drape: topo, lift: 2, name: 'cp' });
  cmp('contour_grid of a property grid draped on topography (+2 m)', lsJ(cp), P.contours_prop, 1e-9);
  record('contour_grid: draped property contours sit 2 m above the ground, units from the grid', cp.nVertices > 0 && Array.from({ length: cp.nVertices }, (_, i) => cp.vertices[3 * i + 2]).every(z => Math.abs(z - 102) < 1e-9) && cp.features[0].units === 'nT');
  cmp('contour_grid of a property grid at a constant z', lsJ(E.contourGrid(prop, [0, 30], { z: 1234, name: 'cz' })), P.contours_prop_z, 1e-9);
  cmp('marching squares saddle (case 5) split by the cell mean', E.marchingSquares(2, 2, (i, j) => saddle.values[j * 2 + i], (i, j) => saddle.nodeXY(i, j), 0.5), P.saddle, 1e-12);
  // -- plane from a measurement
  const S = await import('../site/assets/geomodel/gm-structural.js');
  const pl = S.planeMesh(5000, 6000, 1400, 55, 300, 120, 80, { name: 'pl', from_measurement: { layer: 'S', row: 3 } });
  cmp('plane_mesh: vertices / triangles / metadata match Python', meshJ(pl), P.plane, 1e-9);
  cmp('planeMesh corners equal assay.vein_surface (strike = dip azimuth - 90, same half extent)', S.planeMesh(5000, 6000, 1400, 55, 300, 120, 120).vertices, P.vein, 1e-9);
  record('planeMesh: two triangles, role vein, opacity 0.35, schema + honesty note', pl.nTriangles === 2 && pl.role === 'vein' && pl.opacity === 0.35 && pl.metadata.schema === 'nwmm-assay-vein/1' && pl.metadata.note === 'statement of attitude, not a modelled surface' && pl.metadata.confidence === 'described' && pl.metadata.from_measurement.row === 3);
  record('planeMesh: the plane contains its centre and has the stated pole', (() => { const v = pl.vertices; const e1 = [v[3] - v[0], v[4] - v[1], v[5] - v[2]], e2 = [v[9] - v[0], v[10] - v[1], v[11] - v[2]]; const n = S.unit(S.cross(e1, e2)); const pole = S.poleFromDipAz(55, 300); return S.axialAngle(n, pole) < 1e-9 && Math.abs((v[0] + v[6]) / 2 - 5000) < 1e-9; })());
  record('planeMesh registered as a worker op', typeof E.OPS.planeMesh === 'function' && E.runOp('planeMesh', { x: 1, y: 2, z: 3, dip: 30, dipAzimuth: 90 }).nTriangles === 2);
  // -- set elevation
  // a deep clone the way the worker makes one, with its own typed arrays
  const cloneObj = o => { const c = GM.unpackObject(GM.packObject(o)); for (const k of ['vertices', 'xyz']) if (c[k]) c[k] = Float64Array.from(c[k]); return c; };
  const l1 = cloneObj(ls);
  const st = E.setElevationFrom(l1, grid, { only: 'missing' });
  cmp('set_elevation_from lineset (missing scope): stats / vertices / z_original / metadata', { stats: st, verts: l1.vertices, features: l1.features, meta: l1.metadata }, P.ls_missing, 1e-9);
  record('set_elevation_from lineset (missing): a real z is left alone, 0 and NaN are draped, outside counted', l1.vertices[8] === 777 && l1.vertices[2] !== 0 && l1.vertices[5] === l1.vertices[5] && st.skipped === 1 && st.outside === 1 && st.moved === 5 && Array.isArray(l1.features[0].z_original) && l1.features[0].z_original[2] === null, JSON.stringify(st));
  const back = E.restoreElevation(l1);
  cmp('restore_elevation lineset', { n: back, verts: l1.vertices, features: l1.features }, P.ls_restore, 1e-9);
  const l2 = cloneObj(ls);
  cmp('set_elevation_from lineset (not-surveyed, +2 m)', { stats: E.setElevationFrom(l2, grid, { offset: 2, only: 'not-surveyed' }), verts: l2.vertices }, P.ls_ns, 1e-9);
  record('set_elevation_from: a surveyed part keeps its z', l2.vertices[3 * 3 + 2] === 0 && l2.vertices[3 * 4 + 2] === 0);
  const p1 = cloneObj(ps);
  cmp('set_elevation_from points (not-surveyed)', { stats: E.setElevationFrom(p1, grid, { only: 'not-surveyed' }), xyz: p1.xyz, zo: p1.attributes.z_original, meta: p1.metadata }, P.ps_ns, 1e-9);
  const p2 = cloneObj(ps);
  cmp('set_elevation_from points (all) == setElevationFromGrid', { stats: (r => ({ moved: r.moved, outside: r.outside, skipped: r.skipped }))(S.setElevationFromGrid(p2, grid)), xyz: p2.xyz, zo: p2.attributes.z_original }, P.ps_all, 1e-9);
  const gm = grid.toMesh();
  cmp('mesh_z_at (vertical ray over centroid index) == grid sample', [[1012.5, 2007.5], [1000, 2000], [1099.9, 2079.9], [900, 2000]].map(([x, y]) => E.meshZAt(gm, x, y)), P.mesh_z, 1e-9);
  record('mesh_z_at equals the grid at a node, tracks it inside a cell, and is NaN off the mesh', Math.abs(P.mesh_z[1] - P.mesh_z_grid[1]) < 1e-9 && P.mesh_z.slice(0, 3).every((z, i) => Math.abs(z - P.mesh_z_grid[i]) < 1) && P.mesh_z[3] == null && E.meshZAt(gm, 900, 2000) !== E.meshZAt(gm, 900, 2000));
  const p3 = cloneObj(ps);
  cmp('set_elevation_from points onto a Mesh surface', { stats: E.setElevationFrom(p3, gm), xyz: p3.xyz }, P.ps_mesh, 1e-9);
  const d1 = new GM.Drillholes({ name: 'dh', collars: dh.collars.map(c => Object.assign({}, c)) });
  cmp('set_elevation_from drillhole collars (not-surveyed)', { stats: E.setElevationFrom(d1, grid, { only: 'not-surveyed' }), collars: d1.collars }, P.dh, 1e-9);
  cmp('restore_elevation drillholes', { n: E.restoreElevation(d1), collars: d1.collars }, P.dh_restore, 1e-9);
  // -- clip to ground
  const clip = E.clipMeshToTopography(tilt, topo, { name: 'clip' });
  cmp('clip_mesh_to_topography: vertices / triangles / attributes / stats', { verts: clip.vertices, tris: clip.triangles, v: clip.attributes.v.values, f: clip.attributes.f.values, clip: clip.metadata.clip, role: clip.role }, P.clip, 1e-6);
  record('clip_mesh_to_topography: every vertex at or below the ground (+1e-6), mixed triangles split, whole ones kept', Array.from({ length: clip.nVertices }, (_, i) => clip.vertices[3 * i + 2] <= topo.sample(clip.vertices[3 * i], clip.vertices[3 * i + 1]) + 1e-6).every(Boolean) && clip.metadata.clip.split === 2 && clip.metadata.clip.kept === 1 && clip.nTriangles === 4, JSON.stringify(clip.metadata.clip));
  record('clip_mesh_to_topography: the cut is where z = ground (x = 50 % across the 80 -> 120 tilt)', Array.from({ length: clip.nVertices }, (_, i) => clip.vertex(i)).filter(v => Math.abs(v[2] - 100) < 1e-9).every(v => Math.abs(v[0] - 1050) < 1e-9));
  const dl = E.daylightTrace(tilt, topo);
  cmp('daylight_trace of a mesh (chained crossing segments)', lsJ(dl), P.daylight, 1e-9);
  record('daylight_trace: role interpretation, named "<name> daylight (computed)", one chain along x = 1050', dl.role === 'interpretation' && dl.name === 'tilt daylight (computed)' && dl.parts.length === 1 && Array.from({ length: dl.nVertices }, (_, i) => dl.vertices[3 * i]).every(x => Math.abs(x - 1050) < 1e-9), `${dl.parts.length} part(s)`);
  const hi = grid.copyEmpty(); hi.values = grid.values.map(v => v + 5); hi.name = 'hi';
  cmp('daylight_trace of a grid against a grid (zero contour of the difference)', lsJ(E.daylightTrace(grid, hi)), P.daylight_grid, 1e-9);
  // -- one length unit for anisotropic and isotropic RBFs (G-44)
  const { pts, vals, tg } = rbfData();
  for (const k of E.RBF_KERNELS) {
    const eps = (k === 'gaussian' || k === 'multiquadric') ? 200 : null;
    const a = new E.RBF({ kernel: k, epsilon: eps }).fit(pts, vals).predict(tg), b = new E.RBF({ kernel: k, epsilon: eps, anisotropy: { ranges: [1, 1, 1] } }).fit(pts, vals).predict(tg);
    let worst = 0; for (let i = 0; i < tg.length; i++) worst = Math.max(worst, Math.abs(a[i] - b[i]));
    record(`RBF ${k}: a 1:1:1 anisotropy reproduces the isotropic fit (JS ${worst.toExponential(1)}, py ${P.rbf_iso[k].toExponential(1)})`, worst < 1e-6 && P.rbf_iso[k] < 1e-6);
  }
  const rIso = new E.RBF({ kernel: 'spheroidal', range: 300 }).fit(pts, vals), rAn = new E.RBF({ kernel: 'spheroidal', range: 300, anisotropy: { ranges: [200, 200, 200] } }).fit(pts, vals);
  record('RBF scaleAniso = major range / span, so the ellipsoid distance and the normalised epsilon share a unit', Math.abs(rAn.scaleAniso - 200 / rIso.scale) < 1e-12 && rIso.scaleAniso === 1 && Math.abs(E.RBF.fromJSON(rAn.toJSON()).scaleAniso - rAn.scaleAniso) === 0, `scaleAniso ${rAn.scaleAniso.toExponential(3)} span ${rIso.scale.toFixed(1)}`);
  // -- the new ops through the worker and the main-thread fallback
  const client = new E.EngineClient(WORKER_URL);
  try {
    const wm = await client.call('extrudePolyline', { xyz: trace, dip: 60, dipAzimuth: 175, depth: 100, role: 'fault', name: 'rib' });
    record('worker extrudePolyline round-trips a Mesh with its metadata', wm instanceof GM.Mesh && wm.vertices.every((v, i) => v === rib.vertices[i]) && wm.metadata.schema === 'nwmm-extrude/1' && wm.metadata.dip === 60, `${wm && wm.nTriangles} tris`);
    const wc = await client.call('gridContours', { grid, interval: 5, index: 2, name: 'c' });
    record('worker gridContours round-trips the LineSet', wc instanceof GM.LineSet && wc.role === 'contours' && wc.parts.length === cs.parts.length && wc.vertices.every((v, i) => v === cs.vertices[i]) && wc.features[1].level === cs.features[1].level, `${wc && wc.parts.length} parts`);
    let err = null; try { await client.call('extrudePolyline', { xyz: trace, dip: 60, dipAzimuth: 84, depth: 100 }); } catch (e) { err = e; }
    record('worker forwards the strike refusal verbatim', err instanceof Error && err.message.includes(P.strike.toFixed(1)), err && err.message);
    const we = await client.call('setElevationFrom', { target: cloneObj(ls), surface: grid, only: 'missing' });
    record('worker setElevationFrom returns the moved target + stats', we && we.target instanceof GM.LineSet && we.stats.moved === 5 && we.target.vertices[2] === P.ls_missing.verts[2], JSON.stringify(we && we.stats));
    const wcl = await client.call('clipMeshToTopography', { mesh: tilt, topo });
    record('worker clipMeshToTopography / daylightTrace', wcl instanceof GM.Mesh && wcl.nTriangles === 4 && (await client.call('daylightTrace', { source: tilt, topo })).parts.length === 1);
  } finally { client.terminate(); }
}
function rbfData() {
  const R = rng(777), pts = [], vals = [];
  for (let i = 0; i < 40; i++) { const x = R() * 500, y = R() * 300, z = R() * 100; pts.push([x, y, z]); vals.push(Math.sin(x / 100) + y / 300 + z / 50); }
  return { pts, vals, tg: [[100, 100, 20], [250, 150, 50], [400, 20, 90], [10, 290, 5]] };
}

/* ======================================================= 10. model from map */
/** The synthetic three-unit map both sides read: a 1 km square, topography a
    plane rising east; Basement (Paleozoic) west, Middle (Neogene) centre,
    Young (Quaternary) east, an Island younger than everything that touches
    nothing older, and an Unaged unit.  Two derived readings, one of them
    too far from the northern contacts, plus one derived along a fault. */
function makeMapData() {
  const topo = new GM.Grid2D({ nx: 41, ny: 41, x0: 0, y0: 0, dx: 25, dy: 25, name: 'Topography', role: 'topography' });
  for (let j = 0; j < 41; j++) for (let i = 0; i < 41; i++) { const [x, y] = topo.nodeXY(i, j); topo.values[j * 41 + i] = 1200 + 0.1 * x + 0.02 * y; }
  const rect = (name, x0, y0, x1, y1, step, feat) => {
    const ls = new GM.LineSet({ name: name + ' outline', role: 'geology-outline' });
    ls.addPolyline(E.densify([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], step).map(([x, y]) => [x, y, topo.sample(x, y) + 2]), feat);
    return ls;
  };
  const units = [
    { id: 'y', name: 'Young', t0: 2.6, t1: 0, outline: rect('Young', 700, 0, 1000, 1000, 25, { unit: 'Young', unit_id: 'y' }) },
    { id: 'm', name: 'Middle', t0: 8.7, t1: 2.6, outline: rect('Middle', 400, 0, 700, 1000, 20, { unit: 'Middle', unit_id: 'm' }) },
    { id: 'b', name: 'Basement', t0: 320, t1: 286, outline: rect('Basement', 0, 0, 400, 1000, 25, { unit: 'Basement', unit_id: 'b' }) },
    { id: 'i', name: 'Island', t0: 0.1, t1: 0, outline: rect('Island', 800, 400, 900, 500, 10, { unit: 'Island', unit_id: 'i' }) },
    { id: 'u', name: 'Unaged', outline: rect('Unaged', 100, 100, 200, 200, 10, { unit: 'Unaged', unit_id: 'u' }) },
  ];
  const struct = new GM.PointSet({ name: 'Derived structure', role: 'structural' });
  struct.add(700, 500, topo.sample(700, 500), { dip: 30, dip_azimuth: 90, source: 'Young outline', part: 0 });
  struct.add(400, 500, topo.sample(400, 500), { dip: 20, dip_azimuth: 90, source: 'Middle outline', part: 0 });   // both bases dip east, under their own unit
  struct.add(410, 600, topo.sample(410, 600), { dip: 80, dip_azimuth: 0, source: 'Faults (mapped)', part: 0 });
  const faults = new GM.LineSet({ name: 'Faults (mapped)', role: 'faults' }); faults.addPolyline([[410, 550, 1240], [410, 650, 1240]], { name: 'f1' });
  const lattice = new GM.Grid2D({ nx: 11, ny: 11, x0: 0, y0: 0, dx: 100, dy: 100, name: 'lattice' });
  return { topo, units, struct, faults, lattice };
}
const unitJSON = u => ({ id: u.id, name: u.name, t0: u.t0 === undefined ? null : u.t0, t1: u.t1 === undefined ? null : u.t1, outline: lsJSON(u.outline) });
const contactsJSON = ps => ({ xyz: list(ps.xyz), n: ps.n, attrs: Object.fromEntries(Object.entries(ps.attributes).map(([k, v]) => [k, v.map(x => (typeof x === 'number' && x !== x) ? null : x)])), role: ps.role, prov: ps.provenance, derived_from_n: (ps.metadata.derived_from || []).length });
const unitOut = u => ({ name: u.name, contact: u.contact, has_base: !!u.base, n: u.base ? u.base.n : null, n_contacts: u.n_contacts, n_offsets: u.n_offsets, against: u.against, warnings: u.warnings, t0: u.t0, t1: u.t1, method: u.provenance.method, confidence: u.provenance.confidence, inputs: u.provenance.inputs, derived_from_n: u.derived_from.length, base: u.base ? contactsJSON(u.base) : null });
async function testMapModel() {
  section('10. model from map (unit order / shared contacts / dip offsets / buildFromMap, worker round trip)');
  const { topo, units, struct, faults, lattice } = makeMapData();
  const tied = [{ id: 'a', name: 'A', t0: 5, t1: 1 }, { id: 'b', name: 'B', t0: 5, t1: 1 }, { id: 'c', name: 'C', t0: 9, t1: 5 }, { id: 'd', name: 'D' }, { id: 'e', name: 'E', t0: 1, t1: 0 }, { id: 'f', name: 'F', t1: 0.5 }];
  const payload = { topo: gridJSON(topo), lattice: gridJSON(lattice), units: units.map(unitJSON), tied, struct: psetJSON(struct), faults: lsJSON(faults) };
  const [P, pyMs] = await timed(() => py(`
from geomodel import mapmodel
topo, lat = grid(D['topo']), grid(D['lattice'])
def unit(d):
    u = {'id': d['id'], 'name': d['name'], 'outline': lineset(d['outline'])}
    if d['t0'] is not None: u['t0'] = d['t0']
    if d['t1'] is not None: u['t1'] = d['t1']
    return u
units = [unit(d) for d in D['units']]
struct = pset(D['struct']); struct.role = 'structural'
faults = lineset(D['faults'])
def cj(ps):
    return {'xyz': list(ps.xyz), 'n': ps.n, 'attrs': ps.attributes, 'role': ps.role, 'prov': ps.provenance, 'derived_from_n': len(ps.metadata.get('derived_from', []))}
def uo(u):
    return {'name': u['name'], 'contact': u['contact'], 'has_base': u['base'] is not None, 'n': u['base'].n if u['base'] else None,
            'n_contacts': u['n_contacts'], 'n_offsets': u['n_offsets'], 'against': u['against'], 'warnings': u['warnings'], 't0': u['t0'], 't1': u['t1'],
            'method': u['provenance']['method'], 'confidence': u['provenance']['confidence'], 'inputs': u['provenance']['inputs'],
            'derived_from_n': len(u['derived_from']), 'base': cj(u['base']) if u['base'] else None}
out = {}
o = mapmodel.unit_order(units)
out['order'] = {'order': o['order'], 'aged': o['aged'], 'unaged': o['unaged'], 'unaged_names': o['unaged_names'], 'ties': o['ties'], 'warnings': o['warnings']}
o2 = mapmodel.unit_order(D['tied'])
out['tied'] = {'order': o2['order'], 'ties': o2['ties'], 'unaged_names': o2['unaged_names'], 'warnings': o2['warnings']}
sc = mapmodel.shared_contacts(units[0]['outline'], units[1]['outline'], 25.0, topo=topo)
out['sc'] = {'count': sc['count'], 'edge_skipped': sc['edge_skipped'], 'nodata': sc['nodata'], 'tol': sc['tol'], 'points': cj(sc['points'])}
sc0 = mapmodel.shared_contacts(units[0]['outline'], units[2]['outline'], 25.0, topo=topo)
out['sc_none'] = sc0['count']
scw = mapmodel.shared_contacts(units[0]['outline'], units[1]['outline'], 25.0)
out['sc_noz'] = {'count': scw['count'], 'z': list(scw['points'].xyz)[2::3][:5]}
do = mapmodel.dip_offsets(sc['points'], struct, radius=300.0, offset=100.0, exclude_sources=['Faults (mapped)'])
out['do'] = {'used': do['used'], 'unmatched': do['unmatched'], 'readings': do['readings'], 'excluded': do['excluded'], 'points': cj(do['points'])}
do2 = mapmodel.dip_offsets(sc['points'], struct, radius=300.0, offset=100.0)
out['do_all'] = {'used': do2['used'], 'unmatched': do2['unmatched'], 'readings': do2['readings'], 'excluded': do2['excluded']}
r = mapmodel.build_from_map(topo, units, faults=faults, structural=struct, opts={'radius': 300.0, 'offset': 100.0})
out['bfm'] = {'units': [uo(u) for u in r['units']], 'stats': r['stats'], 'warnings': r['warnings']}
sm, bases, topo2 = stratigraphy.build_stratigraphy(topo, mapmodel.strat_units(r), lattice=lat, method='rbf')
out['strat'] = {'bases': [None if g is None else list(g.values) for g in bases], 'names': [None if g is None else g.name for g in bases], 'units': [u['name'] for u in sm.units], 'has_base': [u['base'] is not None for u in sm.units]}
r2 = mapmodel.build_from_map(topo, units[:3])
out['nodip'] = {'units': [uo(u) for u in r2['units']], 'stats': r2['stats'], 'warnings': r2['warnings']}
few = [dict(units[0], outline=None), units[1], units[2]]
# a Young outline with a single short shared stretch: 2 contact vertices only
ls = LineSet(name='Young outline', role='geology-outline')
ls.add_polyline([(700.0, 480.0, 1270.0), (700.0, 500.0, 1272.0), (900.0, 500.0, 1292.0), (900.0, 480.0, 1290.0), (700.0, 480.0, 1270.0)], {'unit': 'Young', 'unit_id': 'y'})
few[0]['outline'] = ls
r3 = mapmodel.build_from_map(topo, few, structural=struct)
out['few'] = {'units': [uo(u) for u in r3['units']], 'rejected': r3['stats']['rejected'], 'warnings': r3['warnings']}
def err(fn):
    try:
        fn(); return None
    except (ValueError, TypeError) as e:
        return str(e)
out['err_topo'] = err(lambda: mapmodel.build_from_map(None, units))
out['err_tol'] = err(lambda: mapmodel.shared_contacts(units[0]['outline'], units[1]['outline'], 0.0))
out['err_outline'] = err(lambda: mapmodel.build_from_map(topo, [{'id': 'x', 'name': 'X', 't0': 1, 't1': 0}]))
`, payload));
  console.log(`   python reference: ${pyMs.toFixed(0)} ms`);
  // a. order
  const o = E.unitOrder(units);
  cmp('unit_order: youngest first by t1 then t0, unaged last, reported', { order: o.order, aged: o.aged, unaged: o.unaged, unaged_names: o.unaged_names, ties: o.ties, warnings: o.warnings }, P.order, 0);
  record('unit_order: Island (0.1–0) before Young (2.6–0), Unaged last', o.order.join(',') === '3,0,1,2,4' && o.unaged_names[0] === 'Unaged', o.order.join(','));
  const o2 = E.unitOrder(tied);
  cmp('unit_order: ties (same ages) and a single age reported', { order: o2.order, ties: o2.ties, unaged_names: o2.unaged_names, warnings: o2.warnings }, P.tied, 0);
  record('unit_order: A / B tie is reported, D unaged, F (t1 only) placed', o2.ties.length === 1 && o2.ties[0].join('/') === 'A/B' && o2.unaged_names.join() === 'D' && o2.order[0] === 4 && o2.order[1] === 5, JSON.stringify(o2.order) + ' ties ' + JSON.stringify(o2.ties));
  // b. shared contacts
  const sc = E.sharedContacts(units[0].outline, units[1].outline, 25, { topo });
  cmp('shared_contacts: Young / Middle share x = 700 — points, attributes, edge skips', { count: sc.count, edge_skipped: sc.edge_skipped, nodata: sc.nodata, tol: sc.tol, points: contactsJSON(sc.points) }, P.sc, 1e-12);
  record('shared_contacts: every contact lies on x = 700, off the box edge, z from the topography', sc.count === 37 && [...Array(sc.count).keys()].every(i => sc.points.xyz[3 * i] === 700 && sc.points.xyz[3 * i + 1] > 25 && sc.points.xyz[3 * i + 1] < 975 && Math.abs(sc.points.xyz[3 * i + 2] - topo.sample(700, sc.points.xyz[3 * i + 1])) < 1e-9), `${sc.count} contacts, ${sc.edge_skipped} edge vertices skipped`);
  cmp('shared_contacts: Young / Basement do not touch', E.sharedContacts(units[0].outline, units[2].outline, 25, { topo }).count, P.sc_none, 0);
  const scw = E.sharedContacts(units[0].outline, units[1].outline, 25);
  cmp('shared_contacts: without a topography the outline z is kept (and no edge skip)', { count: scw.count, z: Array.from(scw.points.xyz).filter((_, k) => k % 3 === 2).slice(0, 5) }, P.sc_noz, 1e-12);
  // c. dip offsets
  const d = E.dipOffsets(sc.points, struct, { radius: 300, offset: 100, exclude_sources: ['Faults (mapped)'] });
  cmp('dip_offsets: nearest reading within 300 m, one point 100 m down dip, fault reading excluded', { used: d.used, unmatched: d.unmatched, readings: d.readings, excluded: d.excluded, points: contactsJSON(d.points) }, P.do, 1e-12);
  let geomOk = d.used > 0;
  for (let i = 0; i < d.points.n && geomOk; i++) {
    const c = d.points.attributes.contact[i], v = S.dipVector(+d.points.attributes.dip[i], +d.points.attributes.dip_azimuth[i]);
    for (let a = 0; a < 3; a++) if (Math.abs(d.points.xyz[3 * i + a] - (sc.points.xyz[3 * c + a] + 100 * v[a])) > 1e-9) geomOk = false;
    if (+d.points.attributes.dip[i] !== 30 || +d.points.attributes.dip_azimuth[i] !== 90) geomOk = false;   // never the 80° fault reading
  }
  record('dip_offsets: offset = contact + 100 m × S.dipVector(dip, dip azimuth), from the bedding reading', geomOk && d.used === 23 && d.unmatched === 14, `${d.used} offsets, ${d.unmatched} contacts beyond 300 m of a reading`);
  const dAll = E.dipOffsets(sc.points, struct, { radius: 300, offset: 100 });
  cmp('dip_offsets: without the exclusion the fault reading is a candidate', { used: dAll.used, unmatched: dAll.unmatched, readings: dAll.readings, excluded: dAll.excluded }, P.do_all, 0);
  // d. buildFromMap
  const r = E.buildFromMap({ topo, units, faults, structural: struct, opts: { radius: 300, offset: 100 } });
  cmp('build_from_map: units (bases, counts, provenance, warnings) / stats / warnings', { units: r.units.map(unitOut), stats: r.stats, warnings: r.warnings }, P.bfm, 1e-12);
  record('build_from_map: Young + Middle get bases, Basement none, Island skipped and named, Unaged reported', r.units.map(u => u.name).join() === 'Young,Middle,Basement' && r.units[0].base && r.units[1].base && !r.units[2].base && r.stats.rejected.no_contacts.join() === 'Island' && r.stats.unaged.join() === 'Unaged' && r.warnings.some(w => /Island: its outline touches no older unit/.test(w)), r.warnings.join(' | ').slice(0, 160));
  record('build_from_map: every base carries method "model from map", confidence inferred, derived_from', r.units.filter(u => u.base).every(u => u.base.provenance.method === 'model from map' && u.base.provenance.confidence === 'inferred' && u.base.metadata.derived_from.length >= 3 && u.provenance.method === 'model from map'));
  const built = E.buildStratigraphy(topo, r.units.map(u => ({ name: u.name, color: u.color, lithology: u.lithology, description: u.description, contact: u.contact, base: u.base })), { lattice, method: 'rbf' });
  cmp('build_from_map → build_stratigraphy: 2 base grids + basement', { bases: built.bases.map(g => g && g.values), names: built.bases.map(g => g && g.name), units: built.strat.units.map(u => u.name), has_base: built.strat.units.map(u => u.base != null) }, P.strat, 1e-9);
  // refusals
  const r2 = E.buildFromMap({ topo, units: units.slice(0, 3) });
  cmp('build_from_map: no readings anywhere → heightfields, NO_DIP_WARNING on every base', { units: r2.units.map(unitOut), stats: r2.stats, warnings: r2.warnings }, P.nodip, 1e-12);
  record('build_from_map: the no-dip warning is the stated sentence, on the result and on each base', r2.stats.no_dip && r2.warnings.includes(E.NO_DIP_WARNING) && r2.units.filter(u => u.base).every(u => u.warnings.includes(E.NO_DIP_WARNING)) && r2.stats.dips_used === 0, E.NO_DIP_WARNING);
  const fewLs = new GM.LineSet({ name: 'Young outline', role: 'geology-outline' });
  fewLs.addPolyline([[700, 480, 1270], [700, 500, 1272], [900, 500, 1292], [900, 480, 1290], [700, 480, 1270]], { unit: 'Young', unit_id: 'y' });
  const r3 = E.buildFromMap({ topo, units: [Object.assign({}, units[0], { outline: fewLs }), units[1], units[2]], structural: struct });
  cmp('build_from_map: fewer than 3 contacts → skipped and named', { units: r3.units.map(unitOut), rejected: r3.stats.rejected, warnings: r3.warnings }, P.few, 1e-12);
  record('build_from_map: the 2-contact unit is refused with its count, the rest still builds', r3.stats.rejected.few_contacts.join() === 'Young' && r3.units.map(u => u.name).join() === 'Middle,Basement' && r3.warnings.some(w => /only 2 contact points/.test(w)), r3.warnings.find(w => /contact point/.test(w)) || '');
  const err = fn => { try { fn(); return null; } catch (e) { return e.message; } };
  cmp('build_from_map / shared_contacts refusals (no topography, tol 0, no outline)', [err(() => E.buildFromMap({ units })), err(() => E.sharedContacts(units[0].outline, units[1].outline, 0)), err(() => E.buildFromMap({ topo, units: [{ id: 'x', name: 'X', t0: 1, t1: 0 }] }))].map(m => m == null ? null : m.replace(/^sharedContacts/, 'shared_contacts').replace(/^buildFromMap/, 'build_from_map')), [P.err_topo, P.err_tol, P.err_outline], 0);
  // the worker path the tool takes: mapModelInputs → buildStratigraphy → stratigraphyVolumes
  const client = new E.EngineClient(WORKER_URL);
  try {
    const w = await client.call('mapModelInputs', { topo, units: units.map(u => ({ id: u.id, name: u.name, t0: u.t0, t1: u.t1, outline: u.outline })), faults, structural: struct, opts: { radius: 300, offset: 100 } });
    record('worker mapModelInputs: same units and stats back through the worker', client.usingWorker && w.units.length === 3 && w.units[0].base instanceof GM.PointSet && w.units[0].base.n === r.units[0].base.n && JSON.stringify(w.stats) === JSON.stringify(r.stats), `${w.units.map(u => u.name + ':' + (u.base ? u.base.n : '-')).join(' ')}`);
    const b = await client.call('buildStratigraphy', { topo, lattice, units: w.units.map(u => ({ name: u.name, color: u.color, contact: u.contact, base: u.base })), method: 'rbf', name: 'Rock from the map (inferred)' });
    const vols = await client.call('stratigraphyVolumes', { strat: b.strat, grids: b.bases.filter(Boolean), topo: b.topo });
    record('worker buildStratigraphy + stratigraphyVolumes on the map-model units: 2 bases, 3 volumes', b.bases.filter(Boolean).length === 2 && b.strat.name === 'Rock from the map (inferred)' && vols.length === 3 && vols.every(m => m instanceof GM.Mesh && m.nTriangles > 0), `${vols.map(m => m.nTriangles).join('/')} triangles`);
  } finally { client.terminate(); }
}

/* ================================================================= main */
async function main() {
  const t0 = performance.now();
  const exported = Object.keys(E).filter(k => k !== 'GM').sort();
  console.log(`gm-engine.js exports ${exported.length} names: ${exported.join(', ')}`);
  let stratInput = null;
  const steps = [
    ['interp', testInterp],
    ['stratigraphy', async () => { stratInput = await testStratigraphy(); }],
    ['estimate', testEstimate],
    ['isosurface', testIsosurface],
    ['workings', testWorkings],
    ['worker', () => testWorker(stratInput || makeStratData())],
    ['kit', testKitAndSlicing],
    ['geometry', testGeometry],
    ['mapmodel', testMapModel],
  ];
  if (!NO_BENCH) steps.push(['bench', bench]);
  for (const [name, fn] of steps) {
    try { await fn(); }
    catch (e) { record(`${name}: test group crashed`, false, String(e && e.stack || e)); }
  }
  const total = performance.now() - t0;
  console.log('\n' + '='.repeat(100));
  console.log(`${'RESULT'.padEnd(6)} ${'TEST'.padEnd(70)} DETAIL`);
  console.log('-'.repeat(100));
  for (const r of results) {
    const tag = r.ok ? 'PASS' : 'FAIL';
    console.log(`${tag.padEnd(6)} ${r.name.slice(0, 70).padEnd(70)} ${r.detail}`);
  }
  console.log('-'.repeat(100));
  console.log(`${results.length - failures}/${results.length} passed, ${failures} failed  (${(total / 1000).toFixed(1)} s total)`);
  process.exit(failures ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(2); });
