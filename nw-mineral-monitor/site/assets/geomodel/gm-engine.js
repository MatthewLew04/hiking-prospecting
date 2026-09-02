/* gm-engine.js — the numeric engine of the browser geomodeller: a line-for-line
   port of pipelines/geomodel/{interp,stratigraphy,blockmodel,slicing,workings}.py
   (+ the geometry helpers of kit.py) to ES-module JavaScript.

   Pure functions, no DOM, no three.js — importable from browsers (main thread
   or a module Worker) and from node >= 18.  Objects are the gm-core classes
   (Grid2D, Mesh, LineSet, PointSet, BlockModel, StratModel ...).

   Conventions kept from the Python reference:
   - points are PointSets, flat xyz Float64Arrays or arrays of [x, y(, z)];
     values are arrays / typed arrays (null / NaN = missing, dropped);
   - neighbour searches return the k nearest within ``radius`` sorted by
     (distance, index) — exactly the Python ``_GridIndex.nearest`` order, so
     kriging / IDW weights match the reference to rounding;
   - dense systems are solved by Gaussian elimination with partial pivoting
     on a flat row-major Float64Array (the reference's pure-Python fallback,
     numpy's LAPACK solve differs only in rounding); a singular pivot is
     regularised by +1e-10;
   - Python keyword arguments become an ``opts`` object (snake_case keys as
     in Python; the common ones also accept camelCase).

   Deliberate deviations (see test_gm_engine.mjs / the report):
   - ``ordinaryKriging`` / ``simpleKriging`` return ``{est, variance}``
     objects (Python returns a tuple); ``blockCentroids`` returns a flat
     Float64Array; ``sectionPlane`` / ``planeBasis`` return objects.
   - ``blockmodelPlaneSample`` also samples category attributes (Array of
     strings) — the Python version only takes numeric ones.
   - ``buildStratigraphy`` strips ``name`` from the interpolation params it
     forwards (the Python version would raise on PointSet contacts).
   - the worker ops expose progress callbacks; the Python code has none.

   Worker / client: ``runOp(op, args, onProgress)`` is the op table used by
   gm-worker.js; ``EngineClient`` is the promise wrapper the viewer uses (it
   packs gm-core objects with GM.packObject on the way in and unpacks results
   on the way out, and runs the engine on the calling thread when Workers are
   unavailable). */

import * as GM from './gm-core.js';

/* =========================================================== helpers */
const DEG = Math.PI / 180;           // CPython math.radians: x * (pi / 180)
const RAD2DEG = 180 / Math.PI;       // CPython math.degrees: x * (180 / pi)
const INF = Infinity;

export const RBF_KERNELS = ['linear', 'cubic', 'thin_plate', 'gaussian', 'spheroidal', 'multiquadric'];
export const VARIOGRAM_MODELS = ['spherical', 'exponential', 'gaussian', 'linear', 'power', 'nugget'];
export const DEFAULT_COLORS = [[222, 184, 135], [205, 133, 63], [160, 160, 200], [120, 170, 120],
  [200, 120, 120], [190, 190, 100], [100, 150, 190], [170, 120, 170],
  [140, 140, 140], [230, 200, 150]];

function opt(o, a, b, def) {
  if (o && o[a] !== undefined && o[a] !== null) return o[a];
  if (o && b && o[b] !== undefined && o[b] !== null) return o[b];
  return def;
}
function pymod(x, m) { const r = x % m; return (r !== 0 && (r < 0) !== (m < 0)) ? r + m : r; }

/** Python's round(x, nd): correctly rounded, exact ties to even. */
export function pyRound(x, nd = 0) {
  x = +x;
  if (!isFinite(x) || Math.abs(x) >= 1e21) return x;
  nd = nd | 0;
  if (nd < 0) { const p = Math.pow(10, -nd); return pyRound(x / p, 0) * p; }
  if (nd > 100) return x;
  const s = x.toFixed(nd);
  let r = Number(s);
  const full = x.toFixed(100);
  const dot = full.indexOf('.');
  const frac = full.slice(dot + 1);
  if (frac[nd] === '5' && /^0*$/.test(frac.slice(nd + 1))) {
    // exact tie: toFixed rounded away from zero — use the truncation when the kept digit is even
    const truncStr = nd > 0 ? full.slice(0, dot + 1 + nd) : full.slice(0, dot);
    const last = +truncStr[truncStr.length - 1];
    if (last % 2 === 0) r = Number(truncStr);
  }
  return r === 0 ? 0 : r;
}

function isModelObject(v) { return v instanceof GM.ModelObject; }
function asObject(v) {   // accept a packed (plain) gm-core object anywhere a live one is expected
  if (v && typeof v === 'object' && !isModelObject(v) && typeof v.kind === 'string' && GM.KINDS[v.kind]) return unpackModel(v);
  return v;
}

/** Normalise point input to a flat Float64Array xyz (the Python ``_pts``). */
export function toXYZ(points) {
  if (points == null) return new Float64Array(0);
  points = asObject(points);
  if (points.xyz && ArrayBuffer.isView(points.xyz)) return points.xyz;               // PointSet
  if (ArrayBuffer.isView(points)) return points instanceof Float64Array ? points : Float64Array.from(points);
  if (!Array.isArray(points)) throw new TypeError('points: expected a PointSet, a flat xyz array or an array of [x, y, z]');
  if (!points.length) return new Float64Array(0);
  if (typeof points[0] === 'number') return Float64Array.from(points);
  const out = new Float64Array(points.length * 3);
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    out[3 * i] = +p[0]; out[3 * i + 1] = +p[1]; out[3 * i + 2] = p.length > 2 ? +p[2] : 0;
  }
  return out;
}

/** Drop samples with a missing value or a NaN coordinate (the Python ``_clean``). */
export function cleanPoints(points, values) {
  const xyz = toXYZ(points);
  const n = Math.floor(xyz.length / 3);
  const ox = new Float64Array(n * 3), ov = new Float64Array(n);
  let m = 0;
  for (let i = 0; i < n; i++) {
    let v = values == null ? NaN : values[i];
    if (v == null || v === '') continue;
    v = +v;
    if (v !== v) continue;
    const x = xyz[3 * i], y = xyz[3 * i + 1], z = xyz[3 * i + 2];
    if (x !== x || y !== y || z !== z) continue;
    ox[3 * m] = x; ox[3 * m + 1] = y; ox[3 * m + 2] = z; ov[m] = v; m++;
  }
  return { xyz: ox.subarray(0, 3 * m), vals: ov.subarray(0, m), n: m };
}

function dist3(ax, ay, az, bx, by, bz) { const dx = bx - ax, dy = by - ay, dz = bz - az; return Math.sqrt(dx * dx + dy * dy + dz * dz); }
function dist2(ax, ay, bx, by) { const dx = bx - ax, dy = by - ay; return Math.sqrt(dx * dx + dy * dy); }

function jsonable(d) {
  const out = {};
  for (const [k, v] of Object.entries(d || {})) {
    if (v instanceof Variogram || v instanceof Anisotropy) out[k] = v.toJSON();
    else if (v == null || typeof v === 'number' || typeof v === 'string' || typeof v === 'boolean') out[k] = v;
    else if (typeof v === 'function') continue;
    else out[k] = String(v);
  }
  return out;
}

/* ======================================================== anisotropy */
export class Anisotropy {
  /** ranges along major / semi-major / minor axes; azimuth (deg clockwise from
      north) of the major axis, dip (positive down), plunge about it. */
  constructor(ranges, azimuth = 0, dip = 0, plunge = 0, dim = 3) {
    if (typeof ranges === 'number') ranges = [ranges, ranges, ranges];
    ranges = Array.from(ranges, Number);
    if (!ranges.length) throw new Error('anisotropy needs at least one range');
    while (ranges.length < 3) ranges.push(ranges[ranges.length - 1]);
    this.ranges = ranges.slice(0, 3).map(r => Math.max(r, 1e-12));
    this.azimuth = +azimuth; this.dip = +dip; this.plunge = +plunge; this.dim = dim;
    this.rot = this._matrix();
    this._m = Float64Array.from([...this.rot[0], ...this.rot[1], ...this.rot[2]]);
  }
  _matrix() {
    const az = this.azimuth * DEG, dp = this.dip * DEG, pl = this.plunge * DEG;
    const ca = Math.cos(az), sa = Math.sin(az), cd = Math.cos(dp), sd = Math.sin(dp);
    const major = [sa * cd, ca * cd, -sd];
    const horiz = [ca, -sa, 0];
    const minor0 = [-sa * sd, -ca * sd, -cd];
    const cp = Math.cos(pl), sp = Math.sin(pl);
    const semi = [0, 1, 2].map(i => cp * horiz[i] + sp * minor0[i]);
    const minor = [0, 1, 2].map(i => -sp * horiz[i] + cp * minor0[i]);
    return [major, semi, minor];
  }
  transform(dx, dy, dz = 0) {
    const m = this._m, r = this.ranges;
    const u = (m[0] * dx + m[1] * dy + m[2] * dz) / r[0];
    const v = (m[3] * dx + m[4] * dy + m[5] * dz) / r[1];
    if (this.dim === 2) return [u, v, 0];
    return [u, v, (m[6] * dx + m[7] * dy + m[8] * dz) / r[2]];
  }
  dist6(ax, ay, az, bx, by, bz) {
    const dx = bx - ax, dy = by - ay, dz = bz - az, m = this._m, r = this.ranges;
    const u = (m[0] * dx + m[1] * dy + m[2] * dz) / r[0];
    const v = (m[3] * dx + m[4] * dy + m[5] * dz) / r[1];
    const w = this.dim === 2 ? 0 : (m[6] * dx + m[7] * dy + m[8] * dz) / r[2];
    return Math.sqrt(u * u + v * v + w * w);
  }
  distance(a, b) { return this.dist6(a[0], a[1], a[2] || 0, b[0], b[1], b[2] || 0); }
  toJSON() { return { ranges: this.ranges.slice(), azimuth: this.azimuth, dip: this.dip, plunge: this.plunge }; }
  static fromJSON(d) {
    if (d instanceof Anisotropy) return d;
    if (typeof d === 'number' || Array.isArray(d)) return new Anisotropy(d);
    return new Anisotropy(d.ranges, d.azimuth || 0, d.dip || 0, d.plunge || 0, d.dim || 3);
  }
}

/* ======================================================= spatial hash */
/** Uniform-cell spatial hash (the Python ``_GridIndex``).  ``nearest`` fills
    ``resD`` / ``resI`` with up to k (distance, index) pairs sorted by
    (distance, index) and returns how many were found. */
export class GridIndex {
  constructor(xyz, dim = 3, cell = null) {
    this.xyz = xyz; this.dim = dim;
    const n = Math.floor(xyz.length / 3);
    this.n = n;
    this.cells = new Map();
    this.resD = new Float64Array(64); this.resI = new Int32Array(64);
    this._fd = []; this._fi = []; this._ord = [];
    if (!n) { this.cell = 1; this.kmin = [0, 0, 0]; this.kmax = [0, 0, 0]; return; }
    const mn = [INF, INF, INF], mx = [-INF, -INF, -INF];
    for (let i = 0; i < n; i++) for (let a = 0; a < 3; a++) { const v = xyz[3 * i + a]; if (v < mn[a]) mn[a] = v; if (v > mx[a]) mx[a] = v; }
    let span = -INF;
    for (let a = 0; a < dim; a++) span = Math.max(span, mx[a] - mn[a]);
    if (!span) span = 1;
    this.cell = cell || Math.max(span / Math.max(1, Math.pow(n, 1 / dim)) * 2, 1e-9);
    const c = this.cell;
    const kx = new Float64Array(n), ky = new Float64Array(n), kz = new Float64Array(n);
    const kmin = [INF, INF, INF], kmax = [-INF, -INF, -INF];
    for (let i = 0; i < n; i++) {
      kx[i] = Math.floor(xyz[3 * i] / c); ky[i] = Math.floor(xyz[3 * i + 1] / c); kz[i] = dim === 2 ? 0 : Math.floor(xyz[3 * i + 2] / c);
      if (kx[i] < kmin[0]) kmin[0] = kx[i]; if (kx[i] > kmax[0]) kmax[0] = kx[i];
      if (ky[i] < kmin[1]) kmin[1] = ky[i]; if (ky[i] > kmax[1]) kmax[1] = ky[i];
      if (kz[i] < kmin[2]) kmin[2] = kz[i]; if (kz[i] > kmax[2]) kmax[2] = kz[i];
    }
    this.kmin = kmin; this.kmax = kmax;
    this.KX = kmax[0] - kmin[0] + 1; this.KY = kmax[1] - kmin[1] + 1;
    for (let i = 0; i < n; i++) {
      const key = (kx[i] - kmin[0]) + this.KX * ((ky[i] - kmin[1]) + this.KY * (kz[i] - kmin[2]));
      let lst = this.cells.get(key);
      if (!lst) { lst = []; this.cells.set(key, lst); }
      lst.push(i);
    }
  }
  _visit(ix, iy, iz, qx, qy, qz, radius, metric) {
    const kmin = this.kmin, kmax = this.kmax;
    if (ix < kmin[0] || ix > kmax[0] || iy < kmin[1] || iy > kmax[1] || iz < kmin[2] || iz > kmax[2]) return;
    const lst = this.cells.get((ix - kmin[0]) + this.KX * ((iy - kmin[1]) + this.KY * (iz - kmin[2])));
    if (!lst) return;
    const xyz = this.xyz, fd = this._fd, fi = this._fi, d2 = this.dim === 2;
    for (let q = 0; q < lst.length; q++) {
      const i = lst[q], px = xyz[3 * i], py = xyz[3 * i + 1], pz = xyz[3 * i + 2];
      let d;
      if (metric) d = metric(qx, qy, qz, px, py, pz);
      else if (d2) d = dist2(qx, qy, px, py);
      else d = dist3(qx, qy, qz, px, py, pz);
      if (radius == null || d <= radius) { fd.push(d); fi.push(i); }
    }
  }
  _sorted(k) {
    const fd = this._fd, fi = this._fi, ord = this._ord;
    ord.length = fd.length;
    for (let i = 0; i < fd.length; i++) ord[i] = i;
    ord.sort((a, b) => (fd[a] - fd[b]) || (fi[a] - fi[b]));
    const m = Math.min(k, fd.length);
    if (this.resD.length < m) { this.resD = new Float64Array(m); this.resI = new Int32Array(m); }
    for (let i = 0; i < m; i++) { this.resD[i] = fd[ord[i]]; this.resI[i] = fi[ord[i]]; }
    return m;
  }
  /** k nearest within ``radius`` (metric units); ``metricFloor`` = lower bound of
      metric / euclidean distance so the ring expansion terminates. */
  nearest(qx, qy, qz, k, radius = null, metric = null, metricFloor = 1) {
    if (!this.n || !(k > 0)) return 0;
    const cell = this.cell, dim = this.dim;
    const kqx = Math.floor(qx / cell), kqy = Math.floor(qy / cell), kqz = dim === 2 ? 0 : Math.floor(qz / cell);
    if (kqx !== kqx || kqy !== kqy || kqz !== kqz) return 0;
    const kq = [kqx, kqy, kqz];
    let maxRing = 0;
    for (let a = 0; a < dim; a++) maxRing = Math.max(maxRing, Math.max(Math.abs(kq[a] - this.kmin[a]), Math.abs(kq[a] - this.kmax[a])));
    maxRing += 1;
    if (radius != null) maxRing = Math.min(maxRing, Math.ceil(radius / metricFloor / cell) + 1);
    const fd = this._fd, fi = this._fi;
    fd.length = 0; fi.length = 0;
    for (let R = 0; R <= maxRing; R++) {
      for (let dx = -R; dx <= R; dx++) {
        const ax = Math.abs(dx) === R;
        for (let dy = -R; dy <= R; dy++) {
          const face = ax || Math.abs(dy) === R;
          if (dim === 2) { if (face) this._visit(kqx + dx, kqy + dy, 0, qx, qy, qz, radius, metric); continue; }
          if (face) { for (let dz = -R; dz <= R; dz++) this._visit(kqx + dx, kqy + dy, kqz + dz, qx, qy, qz, radius, metric); }
          else { this._visit(kqx + dx, kqy + dy, kqz - R, qx, qy, qz, radius, metric); this._visit(kqx + dx, kqy + dy, kqz + R, qx, qy, qz, radius, metric); }
        }
      }
      if (fd.length >= k) {
        const m = this._sorted(k);
        // every unscanned point is >= R*cell away (euclidean)
        if (this.resD[k - 1] <= R * cell * metricFloor) return m;
      }
    }
    return this._sorted(k);
  }
}

function metricOf(an) {
  if (!an) return { metric: null, floor: 1 };
  return { metric: (ax, ay, az, bx, by, bz) => an.dist6(ax, ay, az, bx, by, bz), floor: 1 / Math.max(...an.ranges) };
}

/* ================================================================ IDW */
/** Inverse-distance-weighted estimates at ``targets`` -> Float64Array (NaN where no neighbour). */
export function idw(points, values, targets, opts = {}) {
  const power = opt(opts, 'power', null, 2), maxPoints = opt(opts, 'max_points', 'maxPoints', 16);
  const radius = opt(opts, 'radius', null, null), dim = opt(opts, 'dim', null, 3), onProgress = opts.onProgress;
  const an = opts.anisotropy ? Anisotropy.fromJSON(opts.anisotropy) : null;
  const { xyz, vals, n } = cleanPoints(points, values);
  const tg = toXYZ(targets), nt = Math.floor(tg.length / 3);
  const out = new Float64Array(nt).fill(NaN);
  if (!n) return out;
  const { metric, floor } = metricOf(an);
  const idx = new GridIndex(xyz, dim);
  const p2 = power === 2, p1 = power === 1;
  for (let t = 0; t < nt; t++) {
    const m = idx.nearest(tg[3 * t], tg[3 * t + 1], tg[3 * t + 2], maxPoints, radius, metric, floor);
    if (!m) continue;
    if (idx.resD[0] < 1e-12) { out[t] = vals[idx.resI[0]]; continue; }
    let ws = 0, vs = 0;
    for (let q = 0; q < m; q++) {
      const d = idx.resD[q];
      const w = 1 / (p2 ? d * d : p1 ? d : Math.pow(d, power));
      ws += w; vs += w * vals[idx.resI[q]];
    }
    out[t] = vs / ws;
    if (onProgress && (t & 255) === 255) onProgress(t / nt);
  }
  if (onProgress) onProgress(1);
  return out;
}

export function nearestNeighbour(points, values, targets, opts = {}) {
  const dim = opt(opts, 'dim', null, 3), radius = opt(opts, 'radius', null, null);
  const { xyz, vals } = cleanPoints(points, values);
  const tg = toXYZ(targets), nt = Math.floor(tg.length / 3);
  const idx = new GridIndex(xyz, dim);
  const out = new Float64Array(nt).fill(NaN);
  for (let t = 0; t < nt; t++) {
    const m = idx.nearest(tg[3 * t], tg[3 * t + 1], tg[3 * t + 2], 1, radius);
    if (m) out[t] = vals[idx.resI[0]];
  }
  return out;
}

/* ========================================================= variograms */
export class Variogram {
  /** new Variogram({nugget, structures:[{model, sill, range, exponent?}], anisotropy})
      or new Variogram({model, sill, range, nugget}) for one structure. */
  constructor(o = {}) {
    if (typeof o === 'number') o = { nugget: o };
    this.nugget = +(o.nugget || 0);
    let structures = o.structures;
    if (structures == null) structures = [{ model: o.model || 'spherical', sill: +(o.sill ?? 1), range: +(o.range ?? o.range_ ?? 1) }];
    this.structures = structures.map(s => {
      s = Object.assign({ model: 'spherical', sill: 1, range: 1 }, s);
      if (s.model == null) s.model = 'spherical';
      if (s.sill == null) s.sill = 1;
      if (s.range == null) s.range = 1;
      if (!VARIOGRAM_MODELS.includes(s.model)) throw new Error(`unknown variogram model ${s.model}`);
      s.sill = +s.sill; s.range = +s.range;
      if (s.exponent != null) s.exponent = +s.exponent;
      return s;
    });
    this.anisotropy = o.anisotropy ? Anisotropy.fromJSON(o.anisotropy) : null;
  }
  get sill() { let s = this.nugget; for (const st of this.structures) s += st.sill; return s; }
  static structureGamma(model, h, sill, a, exponent = 1) {
    if (h <= 0) return 0;
    switch (model) {
      case 'nugget': return sill;
      case 'spherical': { if (h >= a) return sill; const r = h / a; return sill * (1.5 * r - 0.5 * r * r * r); }
      case 'exponential': return sill * (1 - Math.exp(-3 * h / a));
      case 'gaussian': { const r = h / a; return sill * (1 - Math.exp(-3 * r * r)); }
      case 'linear': return sill * h / a;
      case 'power': return sill * Math.pow(h / a, exponent);
    }
    throw new Error(`unknown variogram model ${model}`);
  }
  /** Semivariance at isotropic lag h. */
  gamma(h) {
    if (h <= 0) return 0;
    let g = this.nugget;
    for (const s of this.structures) g += Variogram.structureGamma(s.model, h, s.sill, s.range, s.exponent ?? 1);
    return g;
  }
  /** Semivariance between two points (anisotropy: lag in ellipsoid unit space, ranges 1). */
  gamma6(ax, ay, az, bx, by, bz) {
    if (this.anisotropy) {
      const h = this.anisotropy.dist6(ax, ay, az, bx, by, bz);
      if (h <= 0) return 0;
      let g = this.nugget;
      for (const s of this.structures) g += Variogram.structureGamma(s.model, h, s.sill, 1, s.exponent ?? 1);
      return g;
    }
    return this.gamma(dist3(ax, ay, az, bx, by, bz));
  }
  gammaVec(a, b) { return this.gamma6(a[0], a[1], a[2] || 0, b[0], b[1], b[2] || 0); }
  covariance(a, b) { return this.sill - this.gammaVec(a, b); }
  toJSON() {
    const d = { nugget: this.nugget, structures: this.structures.map(s => { const c = Object.assign({}, s); delete c.anisotropy; return c; }) };
    if (this.anisotropy) d.anisotropy = this.anisotropy.toJSON();
    return d;
  }
  /** Accepts the Python ``Variogram.to_json()`` output (or a Variogram). */
  static fromJSON(d) {
    if (d instanceof Variogram) return d;
    if (typeof d === 'string') d = JSON.parse(d);
    const an = d.anisotropy;
    return new Variogram({
      nugget: d.nugget || 0, structures: d.structures,
      model: d.model, sill: d.sill, range: d.range,
      anisotropy: an ? new Anisotropy(an.ranges, an.azimuth || 0, an.dip || 0, an.plunge || 0) : null,
    });
  }
}

/** Experimental semivariogram -> [{lag, gamma, pairs}] (azimuth: deg clockwise from north, ±tolerance in plan). */
export function empiricalVariogram(points, values, opts = {}) {
  const nLags = opt(opts, 'n_lags', 'nLags', 12);
  let lagSize = opt(opts, 'lag_size', 'lagSize', null);
  const azimuth = opt(opts, 'azimuth', null, null), tolerance = opt(opts, 'tolerance', null, 22.5);
  const dim = opt(opts, 'dim', null, 3), maxPairs = opt(opts, 'max_pairs', 'maxPairs', 2000000);
  const { xyz, vals, n } = cleanPoints(points, values);
  if (n < 2) return [];
  if (lagSize == null) {
    let xmn = INF, xmx = -INF, ymn = INF, ymx = -INF;
    for (let i = 0; i < n; i++) { const x = xyz[3 * i], y = xyz[3 * i + 1]; if (x < xmn) xmn = x; if (x > xmx) xmx = x; if (y < ymn) ymn = y; if (y > ymx) ymx = y; }
    const span = Math.max(xmx - xmn, ymx - ymn) || 1;
    lagSize = span / (2 * nLags);
  }
  const sums = new Float64Array(nLags), cnt = new Float64Array(nLags);
  let pairs = 0;
  const az = azimuth != null ? azimuth * DEG : null;
  for (let i = 0; i < n; i++) {
    const xi = xyz[3 * i], yi = xyz[3 * i + 1], zi = xyz[3 * i + 2], vi = vals[i];
    for (let j = i + 1; j < n; j++) {
      pairs += 1;
      if (pairs > maxPairs) break;
      const h = dim === 2 ? dist2(xi, yi, xyz[3 * j], xyz[3 * j + 1]) : dist3(xi, yi, zi, xyz[3 * j], xyz[3 * j + 1], xyz[3 * j + 2]);
      const k = Math.floor(h / lagSize);
      if (k >= nLags || h <= 0) continue;
      if (az != null) {
        const dx = xyz[3 * j] - xi, dy = xyz[3 * j + 1] - yi;
        if (dx === 0 && dy === 0) continue;
        const ang = Math.atan2(dx, dy);
        const dang = Math.abs(pymod(ang - az + Math.PI / 2, Math.PI) - Math.PI / 2);
        if (dang * RAD2DEG > tolerance) continue;
      }
      const dv = vi - vals[j];
      sums[k] += 0.5 * dv * dv;
      cnt[k] += 1;
    }
  }
  const out = [];
  for (let k = 0; k < nLags; k++) if (cnt[k]) out.push({ lag: (k + 0.5) * lagSize, gamma: sums[k] / cnt[k], pairs: cnt[k] });
  return out;
}

/** Grid-search fit of a one-structure model (same search order as the Python). */
export function fitVariogram(experimental, opts = {}) {
  const model = opt(opts, 'model', null, 'spherical'), nugget = opt(opts, 'nugget', null, null);
  if (!experimental || !experimental.length) throw new Error('empty experimental variogram');
  const lags = experimental.map(e => e.lag), gam = experimental.map(e => e.gamma), wts = experimental.map(e => e.pairs);
  const gmax = Math.max(...gam) || 1, lmax = Math.max(...lags);
  let best = null;
  const nugs = nugget != null ? [0] : [0.0, 0.05, 0.1, 0.2, 0.3, 0.4];
  for (let kr = 0; kr < 36; kr++) {
    const a = (0.2 + 0.05 * kr) * lmax * 1.5;
    for (let ks = 0; ks < 25; ks++) {
      const c = (0.4 + 0.05 * ks) * gmax;
      for (const nf of nugs) {
        const n0 = nugget != null ? nugget : nf * gmax;
        let err = 0;
        for (let q = 0; q < lags.length; q++) {
          const m = n0 + Variogram.structureGamma(model, lags[q], Math.max(c - n0, 1e-12), a);
          const d = m - gam[q];
          err += wts[q] * d * d;
        }
        if (best === null || err < best[0]) best = [err, n0, c, a];
      }
    }
  }
  const [, n0, c, a] = best;
  return new Variogram({ nugget: n0, structures: [{ model, sill: Math.max(c - n0, 1e-12), range: a }] });
}

/* ===================================================== linear algebra */
/** In-place Gaussian elimination with partial pivoting on a flat row-major
    n x n Float64Array; the solution overwrites b.  Singular pivots (< 1e-14)
    are regularised with +1e-10 like the Python fallback. */
export function gaussSolveInPlace(M, n, b) {
  for (let c = 0; c < n; c++) {
    let piv = c, best = Math.abs(M[c * n + c]);
    for (let r = c + 1; r < n; r++) { const v = Math.abs(M[r * n + c]); if (v > best) { best = v; piv = r; } }
    if (best < 1e-14) { M[c * n + c] += 1e-10; piv = c; }
    const cc = c * n;
    if (piv !== c) {
      const pc = piv * n;
      for (let k = c; k < n; k++) { const t = M[cc + k]; M[cc + k] = M[pc + k]; M[pc + k] = t; }
      const t = b[c]; b[c] = b[piv]; b[piv] = t;
    }
    const pv = M[cc + c];
    for (let r = c + 1; r < n; r++) {
      const rc = r * n, f = M[rc + c] / pv;
      if (f === 0) continue;
      for (let k = c + 1; k < n; k++) M[rc + k] -= f * M[cc + k];
      b[r] -= f * b[c];
    }
  }
  for (let r = n - 1; r >= 0; r--) {
    const rc = r * n;
    let s = b[r];
    for (let k = r + 1; k < n; k++) s -= M[rc + k] * b[k];
    b[r] = s / M[rc + r];
  }
  return b;
}

/** Solve A x = b.  A: array of rows or a flat Float64Array (n*n); returns Float64Array. */
export function solveDense(A, b) {
  const n = b.length;
  let M;
  if (ArrayBuffer.isView(A)) { if (A.length !== n * n) throw new Error('solveDense: flat matrix size mismatch'); M = Float64Array.from(A); }
  else { M = new Float64Array(n * n); for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) M[r * n + c] = +A[r][c]; }
  const x = Float64Array.from(b, v => +v);
  return gaussSolveInPlace(M, n, x);
}

/* =========================================================== kriging */
/** Ordinary kriging with a moving neighbourhood -> {est, variance} (Float64Arrays; variance null when not requested). */
export function ordinaryKriging(points, values, targets, variogram, opts = {}) {
  const maxPoints = opt(opts, 'max_points', 'maxPoints', 24), radius = opt(opts, 'radius', null, null);
  const minPoints = opt(opts, 'min_points', 'minPoints', 2), dim = opt(opts, 'dim', null, 3);
  const returnVariance = opt(opts, 'return_variance', 'returnVariance', true), onProgress = opts.onProgress;
  const vg = Variogram.fromJSON(variogram);
  const { xyz, vals, n } = cleanPoints(points, values);
  const tg = toXYZ(targets), nt = Math.floor(tg.length / 3);
  const est = new Float64Array(nt).fill(NaN), vari = returnVariance ? new Float64Array(nt).fill(NaN) : null;
  if (n < minPoints) return { est, variance: vari };
  const { metric, floor } = metricOf(vg.anisotropy);
  const index = new GridIndex(xyz, dim);
  const K = maxPoints + 1;
  const A = new Float64Array(K * K), b = new Float64Array(K), w = new Float64Array(K);
  for (let t = 0; t < nt; t++) {
    const qx = tg[3 * t], qy = tg[3 * t + 1], qz = tg[3 * t + 2];
    const m = index.nearest(qx, qy, qz, maxPoints, radius, metric, floor);
    if (m < minPoints) continue;
    if (index.resD[0] < 1e-12) { est[t] = vals[index.resI[0]]; if (vari) vari[t] = 0; continue; }
    const ids = index.resI, N = m + 1;
    for (let r = 0; r < m; r++) {
      const pr = ids[r], px = xyz[3 * pr], py = xyz[3 * pr + 1], pz = xyz[3 * pr + 2];
      A[r * N + r] = 0;
      for (let c = r + 1; c < m; c++) {
        const pc = ids[c];
        const g = vg.gamma6(px, py, pz, xyz[3 * pc], xyz[3 * pc + 1], xyz[3 * pc + 2]);
        A[r * N + c] = g; A[c * N + r] = g;
      }
      A[r * N + m] = 1; A[m * N + r] = 1;
      b[r] = vg.gamma6(qx, qy, qz, px, py, pz);
    }
    A[m * N + m] = 0; b[m] = 1;
    for (let r = 0; r < N; r++) w[r] = b[r];
    gaussSolveInPlace(A, N, w);
    let e = 0;
    for (let r = 0; r < m; r++) e += w[r] * vals[ids[r]];
    est[t] = e;
    if (vari) {
      let v = 0;
      for (let r = 0; r < m; r++) v += w[r] * b[r];
      v += w[m];
      vari[t] = v === v ? Math.max(v, 0) : NaN;
    }
    if (onProgress && (t & 127) === 127) onProgress(t / nt);
  }
  if (onProgress) onProgress(1);
  return { est, variance: vari };
}

/** Simple kriging with a known mean -> {est}. */
export function simpleKriging(points, values, targets, variogram, mean, opts = {}) {
  const maxPoints = opt(opts, 'max_points', 'maxPoints', 24), radius = opt(opts, 'radius', null, null), dim = opt(opts, 'dim', null, 3);
  const vg = Variogram.fromJSON(variogram);
  const { xyz, vals } = cleanPoints(points, values);
  const tg = toXYZ(targets), nt = Math.floor(tg.length / 3);
  const { metric, floor } = metricOf(vg.anisotropy);
  const index = new GridIndex(xyz, dim);
  const sill = vg.sill;
  const est = new Float64Array(nt).fill(NaN);
  const A = new Float64Array(maxPoints * maxPoints), w = new Float64Array(maxPoints);
  for (let t = 0; t < nt; t++) {
    const qx = tg[3 * t], qy = tg[3 * t + 1], qz = tg[3 * t + 2];
    const m = index.nearest(qx, qy, qz, maxPoints, radius, metric, floor);
    if (!m) continue;
    const ids = index.resI;
    for (let r = 0; r < m; r++) {
      const pr = ids[r], px = xyz[3 * pr], py = xyz[3 * pr + 1], pz = xyz[3 * pr + 2];
      for (let c = 0; c < m; c++) { const pc = ids[c]; A[r * m + c] = sill - vg.gamma6(px, py, pz, xyz[3 * pc], xyz[3 * pc + 1], xyz[3 * pc + 2]); }
      w[r] = sill - vg.gamma6(qx, qy, qz, px, py, pz);
    }
    gaussSolveInPlace(A, m, w);
    let e = 0;
    for (let r = 0; r < m; r++) e += w[r] * (vals[ids[r]] - mean);
    est[t] = mean + e;
  }
  return { est };
}

/* =============================================================== RBF */
function kernelFn(kind, eps, kp) {
  switch (kind) {
    case 'linear': return r => r;
    case 'cubic': return r => r * r * r;
    case 'thin_plate': return r => (r <= 0 ? 0 : r * r * Math.log(r));
    case 'gaussian': return r => { const q = r / eps; return Math.exp(-(q * q)); };
    case 'multiquadric': return r => Math.sqrt(r * r + eps * eps);
    case 'spheroidal': {
      const a = kp.range != null ? kp.range : eps, c = kp.sill != null ? kp.sill : 1;
      return r => { if (r >= a) return 0; const x = r / a; return c * (1 - 1.5 * x + 0.5 * x * x * x); };
    }
  }
  throw new Error(`unknown RBF kernel ${kind}`);
}

/** Radial-basis-function interpolant with polynomial drift and smoothing (the
    implicit-modelling engine).  new RBF({kernel, drift, smoothing, epsilon, dim,
    anisotropy, range, sill}); fit(points, values); predict(targets, onProgress). */
export class RBF {
  constructor(o = {}) {
    const { kernel = 'thin_plate', drift = 'linear', smoothing = 0, epsilon = null, dim = 3, anisotropy = null, ...params } = o;
    if (!RBF_KERNELS.includes(kernel)) throw new Error(`unknown RBF kernel ${kernel}`);
    this.kernel = kernel;
    this.drift = ['none', 'constant', 'linear'].includes(drift) ? drift : 'linear';
    this.smoothing = +smoothing;
    this.epsilon = epsilon == null ? null : +epsilon;
    this.dim = dim;
    this.anisotropy = anisotropy ? Anisotropy.fromJSON(anisotropy) : null;
    this.params = {};
    for (const [k, v] of Object.entries(params)) if (typeof v !== 'function' && v !== undefined) this.params[k] = v;
    this.centers = new Float64Array(0); this.values = new Float64Array(0);
    this.weights = new Float64Array(0); this.poly = new Float64Array(0);
    this.scale = 1; this.offset = [0, 0, 0]; this.scaleAniso = 1;
    this._eps = null; this._kp = null; this._local = null;
  }
  _ndrift() { return { none: 0, constant: 1, linear: 1 + this.dim }[this.drift]; }
  get n() { return this.centers.length / 3; }
  fit(points, values) {
    const { xyz, vals, n } = cleanPoints(points, values);
    if (!n) throw new Error('no valid points to fit');
    const mn = [INF, INF, INF], mx = [-INF, -INF, -INF];
    for (let i = 0; i < n; i++) for (let a = 0; a < 3; a++) { const v = xyz[3 * i + a]; if (v < mn[a]) mn[a] = v; if (v > mx[a]) mx[a] = v; }
    this.offset = [0, 1, 2].map(a => (mn[a] + mx[a]) / 2);
    let span = -INF;
    for (let a = 0; a < this.dim; a++) span = Math.max(span, mx[a] - mn[a]);
    if (!span) span = 1;
    this.scale = span;
    // One length unit for both paths (G-44): the isotropic kernel sees
    // |Δ| / span, so the ellipsoid distance (|Δ| / range along each axis) is
    // rescaled by the major range so that a 1:1:1 anisotropy reproduces the
    // isotropic fit exactly and the normalised epsilon / spheroidal range mean
    // the same thing whichever path is taken.
    this.scaleAniso = this.anisotropy ? Math.max(...this.anisotropy.ranges) / span : 1;
    let eps = this.epsilon;
    if (eps == null) eps = (this.kernel === 'gaussian' || this.kernel === 'multiquadric') ? 0.25 : 1;
    else eps = eps / span;
    if (this.kernel === 'spheroidal') {
      if (this.params.range == null) this.params.range = span;
      this.params.range_local = this.params.range / span;
    }
    this._eps = eps;
    const kp = Object.assign({}, this.params);
    if (this.kernel === 'spheroidal') kp.range = this.params.range_local;
    this._kp = kp;
    const nd = this._ndrift(), N = n + nd, dim = this.dim;
    const L = new Float64Array(n * 3);
    const [ox, oy, oz] = this.offset, s = this.scale;
    for (let i = 0; i < n; i++) { L[3 * i] = (xyz[3 * i] - ox) / s; L[3 * i + 1] = (xyz[3 * i + 1] - oy) / s; L[3 * i + 2] = (xyz[3 * i + 2] - oz) / s; }
    const kf = kernelFn(this.kernel, eps, kp);
    const an = this.anisotropy, sa = this.scaleAniso;
    const A = new Float64Array(N * N), b = new Float64Array(N);
    for (let i = 0; i < n; i++) {
      const iN = i * N;
      for (let j = i; j < n; j++) {
        let r;
        if (an) r = an.dist6(xyz[3 * i], xyz[3 * i + 1], xyz[3 * i + 2], xyz[3 * j], xyz[3 * j + 1], xyz[3 * j + 2]) * sa;
        else if (dim === 2) r = dist2(L[3 * i], L[3 * i + 1], L[3 * j], L[3 * j + 1]);
        else r = dist3(L[3 * i], L[3 * i + 1], L[3 * i + 2], L[3 * j], L[3 * j + 1], L[3 * j + 2]);
        const v = kf(r);
        A[iN + j] = v; A[j * N + i] = v;
      }
      A[iN + i] += this.smoothing;
      if (nd) {
        A[iN + n] = 1; A[n * N + i] = 1;
        for (let k = 1; k < nd; k++) { const u = L[3 * i + (k - 1)]; A[iN + n + k] = u; A[(n + k) * N + i] = u; }
      }
      b[i] = vals[i];
    }
    gaussSolveInPlace(A, N, b);
    this.centers = Float64Array.from(xyz); this.values = Float64Array.from(vals);
    this.weights = b.slice(0, n); this.poly = b.slice(n);
    this._local = L;
    return this;
  }
  predict(targets, onProgress = null) {
    const tg = toXYZ(targets), nt = Math.floor(tg.length / 3), n = this.n;
    const out = new Float64Array(nt);
    if (!n) { out.fill(NaN); return out; }
    const kf = kernelFn(this.kernel, this._eps, this._kp);
    const W = this.weights, P = this.poly, nd = P.length, dim = this.dim;
    const [ox, oy, oz] = this.offset, s = this.scale;
    const an = this.anisotropy, sa = this.scaleAniso, C = this.centers, L = this._local;
    const step = Math.max(256, Math.floor(2e6 / Math.max(1, n)));
    for (let t = 0; t < nt; t++) {
      const x = tg[3 * t], y = tg[3 * t + 1], z = tg[3 * t + 2];
      const qx = (x - ox) / s, qy = (y - oy) / s, qz = (z - oz) / s;
      let acc = 0;
      if (an) {
        for (let i = 0; i < n; i++) acc += W[i] * kf(an.dist6(x, y, z, C[3 * i], C[3 * i + 1], C[3 * i + 2]) * sa);
      } else if (dim === 2) {
        for (let i = 0; i < n; i++) { const dx = L[3 * i] - qx, dy = L[3 * i + 1] - qy; acc += W[i] * kf(Math.sqrt(dx * dx + dy * dy)); }
      } else {
        for (let i = 0; i < n; i++) { const dx = L[3 * i] - qx, dy = L[3 * i + 1] - qy, dz = L[3 * i + 2] - qz; acc += W[i] * kf(Math.sqrt(dx * dx + dy * dy + dz * dz)); }
      }
      if (nd) {
        acc += P[0];
        if (nd > 1) { acc += P[1] * qx; acc += P[2] * qy; if (nd > 3) acc += P[3] * qz; }
      }
      out[t] = acc;
      if (onProgress && t % step === step - 1) onProgress(t / nt);
    }
    if (onProgress) onProgress(1);
    return out;
  }
  toJSON() {
    return {
      kind: 'rbf', kernel: this.kernel, drift: this.drift, smoothing: this.smoothing, epsilon: this.epsilon, dim: this.dim,
      anisotropy: this.anisotropy ? this.anisotropy.toJSON() : null, params: this.params,
      centers: this.centers, values: this.values, weights: this.weights, poly: this.poly,
      scale: this.scale, offset: this.offset.slice(), scaleAniso: this.scaleAniso, eps: this._eps, kp: this._kp,
    };
  }
  static fromJSON(d) {
    if (d instanceof RBF) return d;
    const r = new RBF(Object.assign({ kernel: d.kernel, drift: d.drift, smoothing: d.smoothing, epsilon: d.epsilon, dim: d.dim, anisotropy: d.anisotropy }, d.params || {}));
    r.centers = GM.f64(d.centers || []); r.values = GM.f64(d.values || []);
    r.weights = GM.f64(d.weights || []); r.poly = GM.f64(d.poly || []);
    r.scale = d.scale; r.offset = (d.offset || [0, 0, 0]).slice(); r.scaleAniso = d.scaleAniso == null ? 1 : d.scaleAniso;
    r._eps = d.eps; r._kp = d.kp || {};
    const n = r.n, L = new Float64Array(n * 3);
    for (let i = 0; i < n; i++) for (let a = 0; a < 3; a++) L[3 * i + a] = (r.centers[3 * i + a] - r.offset[a]) / r.scale;
    r._local = L;
    return r;
  }
}

/* ======================================================= convenience */
/** [x0, y0, dx, dy, nx, ny] covering the points with a margin. */
export function gridSpecFromPoints(points, opts = {}) {
  const cellIn = opt(opts, 'cell', null, null), n = opt(opts, 'n', null, 80), pad = opt(opts, 'pad', null, 0.05);
  const xyz = toXYZ(points), m = Math.floor(xyz.length / 3);
  if (!m) throw new Error('gridSpecFromPoints: no points');
  let x0 = INF, x1 = -INF, y0 = INF, y1 = -INF;
  for (let i = 0; i < m; i++) { const x = xyz[3 * i], y = xyz[3 * i + 1]; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  const w = (x1 - x0) || 1, h = (y1 - y0) || 1;
  x0 -= w * pad; x1 += w * pad; y0 -= h * pad; y1 += h * pad;
  const cell = cellIn == null ? Math.max(x1 - x0, y1 - y0) / n : cellIn;
  const nx = Math.ceil((x1 - x0) / cell) + 1, ny = Math.ceil((y1 - y0) / cell) + 1;
  return [x0, y0, cell, cell, nx, ny];
}

function specArray(spec) {
  if (Array.isArray(spec) || ArrayBuffer.isView(spec)) return Array.from(spec, Number);
  return [+spec.x0, +spec.y0, +spec.dx, +spec.dy, spec.nx | 0, spec.ny | 0];
}

const GRID_KEYS = new Set(['method', 'spec', 'cell', 'n', 'name', 'onProgress', 'params']);

/** Interpolate scattered (x, y, value) samples onto a Grid2D.
    opts: {method:'rbf'|'idw'|'ok'|'nn', spec, cell, n, name, onProgress, ...interpolation params}. */
export function gridFromPoints(points, values, opts = {}) {
  const o = Object.assign({}, opts, opts.params || {});
  const method = o.method || 'rbf', name = o.name || 'surface', onProgress = o.onProgress;
  const params = {};
  for (const [k, v] of Object.entries(o)) if (!GRID_KEYS.has(k) && v !== undefined) params[k] = v;
  const pts = toXYZ(points), np = Math.floor(pts.length / 3);
  const vals = values;
  const spec = o.spec ? specArray(o.spec) : gridSpecFromPoints(pts, { cell: o.cell, n: o.n == null ? 80 : o.n });
  const [x0, y0, dx, dy, nx, ny] = spec;
  const g = new GM.Grid2D({ nx, ny, x0, y0, dx, dy, name });
  const targets = new Float64Array(nx * ny * 3);
  let q = 0;
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) { const xy = g.nodeXY(i, j); targets[q++] = xy[0]; targets[q++] = xy[1]; targets[q++] = 0; }
  const pts2 = new Float64Array(np * 3);
  for (let i = 0; i < np; i++) { pts2[3 * i] = pts[3 * i]; pts2[3 * i + 1] = pts[3 * i + 1]; pts2[3 * i + 2] = 0; }
  let est;
  if (method === 'rbf') {
    const { kernel = 'thin_plate', drift = 'linear', smoothing = 0, ...rest } = params;
    delete params.kernel; delete params.drift; delete params.smoothing;     // popped, like the Python
    const rbf = new RBF(Object.assign({ kernel, drift, smoothing, dim: 2 }, rest));
    rbf.fit(pts2, vals);
    est = rbf.predict(targets, onProgress);
  } else if (method === 'idw') {
    est = idw(pts2, vals, targets, { power: opt(params, 'power', null, 2), max_points: opt(params, 'max_points', 'maxPoints', 12), radius: params.radius, dim: 2, onProgress });
  } else if (method === 'ok') {
    let vg = params.variogram;
    if (vg == null) {
      const exp = empiricalVariogram(pts2, vals, { dim: 2 });
      vg = exp.length ? fitVariogram(exp) : new Variogram({ model: 'spherical', sill: 1, range: Math.max(dx * nx, dy * ny) });
    } else vg = Variogram.fromJSON(vg);
    params.variogram = vg;
    est = ordinaryKriging(pts2, vals, targets, vg, { max_points: opt(params, 'max_points', 'maxPoints', 16), radius: params.radius, dim: 2, return_variance: false, onProgress }).est;
  } else if (method === 'nn') {
    est = nearestNeighbour(pts2, vals, targets, { dim: 2 });
  } else throw new Error(`unknown method ${method}`);
  g.values = est;
  g.metadata.interpolation = { method, n_points: np, params: jsonable(params) };
  return g;
}

/* ====================================================== stratigraphy */
function gridLookup(grids) {
  if (!grids) return { get: () => undefined, all: () => [] };
  if (typeof grids === 'function') return { get: grids, all: () => [] };
  if (grids instanceof Map) return { get: id => asObject(grids.get(id)), all: () => [...grids.values()].map(asObject) };
  if (Array.isArray(grids)) { const m = new Map(); for (const g0 of grids) { const g = asObject(g0); if (g) m.set(g.id, g); } return { get: id => m.get(id), all: () => [...m.values()] }; }
  if (typeof grids.byKind === 'function') return { get: id => grids.get(id), all: () => grids.byKind('grid2d') };   // Project
  return { get: id => asObject(grids[id]), all: () => Object.values(grids).map(asObject) };
}
function needGrid(look, id) { const g = look.get(id); if (!g) throw new Error(`grid ${id} not found`); return g; }

/** Grid2D on ``lattice``'s nodes from a Grid2D (bilinear resample), a PointSet
    (interpolated with opts.method) or a constant elevation. */
export function surfaceOnLattice(source, lattice, opts = {}) {
  source = asObject(source); lattice = asObject(lattice);
  const g = lattice.copyEmpty();
  if (typeof source === 'number') {
    g.values.fill(+source);
    g.metadata.source = 'constant';
    return g;
  }
  if (source instanceof GM.Grid2D) {
    for (let j = 0; j < g.ny; j++) for (let i = 0; i < g.nx; i++) { const xy = g.nodeXY(i, j); g.values[j * g.nx + i] = source.sample(xy[0], xy[1]); }
    g.metadata.source = 'grid:' + source.id;
    g.name = source.name;
    return g;
  }
  if (source instanceof GM.PointSet) {
    const { method = 'rbf', z_column, zColumn, ...params } = opts;
    const zcol = z_column || zColumn;
    const vals = zcol ? source.numeric(zcol) : Float64Array.from({ length: source.n }, (_, i) => source.xyz[3 * i + 2]);
    const out = gridFromPoints(source.xyz, vals, Object.assign({}, params, { method, spec: [g.x0, g.y0, g.dx, g.dy, g.nx, g.ny], name: source.name }));
    out.metadata.source = 'points:' + source.id;
    out.rotation = g.rotation;
    return out;
  }
  throw new TypeError('unsupported contact source ' + (source && source.kind ? source.kind : typeof source));
}

/** Build the pancake stack.  units (top/youngest first): [{name, color, lithology,
    description, contact:'deposit'|'erosion', base: Grid2D|PointSet|number|null}].
    Returns {strat: StratModel, bases: [Grid2D|null], topo: Grid2D}. */
export function buildStratigraphy(topography, units, opts = {}) {
  topography = asObject(topography);
  const { lattice: lat0 = null, method = 'rbf', onProgress = null, name: modelName, ...params } = opts;
  const lattice = asObject(lat0) || topography;
  const topo = topography !== lattice ? surfaceOnLattice(topography, lattice) : topography;
  const n = lattice.nx * lattice.ny;
  const bases = [], rules = [];
  units.forEach((u, k) => {
    const src = u.base;
    if (src == null) bases.push(null);
    else {
      const g = surfaceOnLattice(src, lattice, Object.assign({ method }, params));
      g.name = (u.name != null ? u.name : `unit ${k}`) + ' base';
      g.role = 'contact';
      g.color = u.color || DEFAULT_COLORS[k % DEFAULT_COLORS.length];
      g.metadata.contact = u.contact || 'deposit';
      bases.push(g);
    }
    rules.push(u.contact || 'deposit');
    if (onProgress) onProgress((k + 1) / (units.length + 1));
  });
  // chronology: apply from the OLDEST surface up to the youngest
  const real = [];
  bases.forEach((g, k) => { if (g) real.push([k, g]); });
  for (let pos = real.length - 1; pos >= 0; pos--) {
    const [k, g] = real[pos];
    const older = real.slice(pos + 1).map(e => e[1]);
    const gv = g.values;
    if (rules[k] === 'erosion') {
      for (const og of older) { const ov = og.values; for (let idx = 0; idx < n; idx++) { const a = ov[idx], b = gv[idx]; if (a === a && b === b && a > b) ov[idx] = b; } }
    } else {
      for (const og of older) { const ov = og.values; for (let idx = 0; idx < n; idx++) { const a = ov[idx], b = gv[idx]; if (a === a && b === b && b < a) gv[idx] = a; } }
    }
  }
  const tv = topo.values;
  for (const [, g] of real) { const gv = g.values; for (let idx = 0; idx < n; idx++) { const t = tv[idx], b = gv[idx]; if (t === t && b === b && b > t) gv[idx] = t; } }
  const sm = new GM.StratModel({ name: modelName || 'stratigraphy', topography: topo.id });
  units.forEach((u, k) => {
    sm.units.push({
      name: u.name != null ? u.name : `unit ${k}`,
      color: u.color || DEFAULT_COLORS[k % DEFAULT_COLORS.length],
      lithology: u.lithology || '', description: u.description || '',
      contact: rules[k], base: bases[k] ? bases[k].id : null,
    });
  });
  if (onProgress) onProgress(1);
  return { strat: sm, bases, topo };
}

/** Vertical column at (x, y): [{name, top, base, thickness, color}] (basement: base null). */
export function columnAt(strat, grids, x, y, topo) {
  strat = asObject(strat); topo = asObject(topo);
  const look = gridLookup(grids);
  const out = [];
  let top = topo.sample(x, y);
  for (const u of strat.units) {
    if (u.base == null) { out.push({ name: u.name, top, base: null, thickness: null, color: u.color }); break; }
    const b = needGrid(look, u.base).sample(x, y);
    if (b !== b || top !== top) out.push({ name: u.name, top, base: b, thickness: NaN, color: u.color });
    else out.push({ name: u.name, top, base: b, thickness: Math.max(top - b, 0), color: u.color });
    top = b;
  }
  return out;
}

/** Name of the unit containing (x, y, z), or null above topography. */
export function unitAt(strat, grids, x, y, z, topo) {
  for (const c of columnAt(strat, grids, x, y, topo)) {
    if (c.top !== c.top) continue;
    if (z > c.top + 1e-9) return null;
    if (c.base == null || z >= c.base) return c.name;
  }
  return null;
}

/** Closed mesh of one unit between two heightfields on the same lattice. */
export function unitVolumeMesh(top, base, name, color, skirt = true) {
  top = asObject(top); base = asObject(base);
  if (top.nx !== base.nx || top.ny !== base.ny) throw new Error('top and base grids must share a lattice');
  const nx = top.nx, ny = top.ny;
  const verts = new Float64Array(nx * ny * 6);
  let nv = 0;
  const idxTop = new Int32Array(nx * ny).fill(-1), idxBase = new Int32Array(nx * ny).fill(-1);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const zt = top.values[j * nx + i];
    let zb = base.values[j * nx + i];
    if (zt !== zt || zb !== zb) continue;
    if (zb > zt) zb = zt;
    const xy = top.nodeXY(i, j);
    idxTop[j * nx + i] = nv; verts[3 * nv] = xy[0]; verts[3 * nv + 1] = xy[1]; verts[3 * nv + 2] = zt; nv++;
    idxBase[j * nx + i] = nv; verts[3 * nv] = xy[0]; verts[3 * nv + 1] = xy[1]; verts[3 * nv + 2] = zb; nv++;
  }
  const tris = [];
  for (let j = 0; j < ny - 1; j++) for (let i = 0; i < nx - 1; i++) {
    const q0 = j * nx + i, q1 = j * nx + i + 1, q2 = (j + 1) * nx + i + 1, q3 = (j + 1) * nx + i;
    if (idxTop[q0] < 0 || idxTop[q1] < 0 || idxTop[q2] < 0 || idxTop[q3] < 0) continue;
    let a = idxTop[q0], b = idxTop[q1], c = idxTop[q2], d = idxTop[q3];
    tris.push(a, b, c, a, c, d);                 // top: CCW from above
    a = idxBase[q0]; b = idxBase[q1]; c = idxBase[q2]; d = idxBase[q3];
    tris.push(a, c, b, a, d, c);                 // base: CCW from below
  }
  if (skirt) {
    const edge = (p, q) => {
      if (idxTop[p] >= 0 && idxTop[q] >= 0) { const tp = idxTop[p], tq = idxTop[q], bp = idxBase[p], bq = idxBase[q]; tris.push(tp, bp, bq, tp, bq, tq); }
    };
    for (let i = 0; i < nx - 1; i++) edge(i, i + 1);                                                   // south
    for (let j = 0; j < ny - 1; j++) edge(j * nx + nx - 1, (j + 1) * nx + nx - 1);                     // east
    for (let i = 0; i < nx - 1; i++) edge((ny - 1) * nx + i + 1, (ny - 1) * nx + i);                   // north
    for (let j = 0; j < ny - 1; j++) edge((j + 1) * nx, j * nx);                                       // west
  }
  const m = new GM.Mesh({ vertices: verts.slice(0, 3 * nv), triangles: Uint32Array.from(tris), name, color, role: 'unit' });
  m.metadata.unit = name;
  return m;
}

/** One closed Mesh per unit (basement gets a flat floor 1 km below the lowest base). */
export function stratigraphyVolumes(strat, grids, topo) {
  strat = asObject(strat); topo = asObject(topo);
  const look = gridLookup(grids);
  const meshes = [];
  let top = topo;
  const lows = look.all().map(g => g.zrange()[0]).filter(v => v === v);
  const lowest = lows.length ? Math.min(...lows) : topo.zrange()[0];
  for (const u of strat.units) {
    if (u.base == null) {
      const floor = top.copyEmpty(lowest - 1000);
      meshes.push(unitVolumeMesh(top, floor, u.name, u.color));
      break;
    }
    const base = needGrid(look, u.base);
    meshes.push(unitVolumeMesh(top, base, u.name, u.color));
    top = base;
  }
  return meshes;
}

export function thicknessGrid(top, base, name = 'thickness') {
  top = asObject(top); base = asObject(base);
  const g = top.copyEmpty();
  for (let idx = 0; idx < g.values.length; idx++) {
    const t = top.values[idx], b = base.values[idx];
    g.values[idx] = (t !== t || b !== b) ? NaN : Math.max(t - b, 0);
  }
  g.name = name; g.role = 'property'; g.units = 'm';
  return g;
}

/** Add a category attribute naming the unit each block centroid sits in ('' above topography). */
export function tagBlockModel(bm, strat, grids, topo, attribute = 'unit', onProgress = null) {
  bm = asObject(bm); strat = asObject(strat); topo = asObject(topo);
  const [nx, ny, nz] = bm.count;
  const names = new Array(bm.n);
  const cache = new Array(nx * ny);
  let pos = 0;
  for (let k = 0; k < nz; k++) {
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
      const [x, y, z] = bm.centroid(i, j, k);
      let col = cache[j * nx + i];
      if (col === undefined) { col = columnAt(strat, grids, x, y, topo); cache[j * nx + i] = col; }
      let name = '';
      for (const c of col) {
        if (c.top !== c.top) continue;
        if (z > c.top) break;
        if (c.base == null || z >= c.base) { name = c.name; break; }
      }
      names[pos++] = name;
    }
    if (onProgress) onProgress((k + 1) / nz);
  }
  bm.addAttribute(attribute, names, 'category');
  return bm;
}

/* ======================================================== block model */
/** bounds = [minx, miny, minz, maxx, maxy, maxz]; blockSize = [dx, dy, dz] or a scalar. */
export function createBlockModel(bounds, blockSize, opts = {}) {
  const name = opt(opts, 'name', null, 'block model'), azimuth = opt(opts, 'azimuth', null, 0), snap = opt(opts, 'snap', null, true);
  if (typeof blockSize === 'number') blockSize = [blockSize, blockSize, blockSize];
  const [dx, dy, dz] = Array.from(blockSize, Number);
  let [minx, miny, minz] = bounds;
  const [, , , maxx, maxy, maxz] = bounds;
  if (snap) { minx = Math.floor(minx / dx) * dx; miny = Math.floor(miny / dy) * dy; minz = Math.floor(minz / dz) * dz; }
  const nx = Math.max(1, Math.ceil((maxx - minx) / dx)), ny = Math.max(1, Math.ceil((maxy - miny) / dy)), nz = Math.max(1, Math.ceil((maxz - minz) / dz));
  const bm = new GM.BlockModel({ origin: [minx, miny, minz], blockSize: [dx, dy, dz], count: [nx, ny, nz], name, azimuth });
  bm.metadata.created_from = { bounds: Array.from(bounds) };
  return bm;
}

/** Flat xyz Float64Array of block centroids (only where mask[idx] is truthy when given). */
export function blockCentroids(bm, mask = null) {
  bm = asObject(bm);
  const [nx, ny, nz] = bm.count;
  let count = 0;
  if (mask) { for (let i = 0; i < bm.n; i++) if (mask[i]) count++; } else count = bm.n;
  const out = new Float64Array(count * 3);
  let idx = 0, q = 0;
  for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    if (!mask || mask[idx]) { const c = bm.centroid(i, j, k); out[q++] = c[0]; out[q++] = c[1]; out[q++] = c[2]; }
    idx++;
  }
  return out;
}

/** Length-weighted compositing of interval samples per hole -> new PointSet (no-op without targetLength). */
export function composite(points, value, opts = {}) {
  points = asObject(points);
  const targetLength = opt(opts, 'target_length', 'targetLength', null);
  if (!targetLength) return points;
  const n = points.n;
  const holeCol = points.attributes.hole || new Array(n).fill(null);
  const holes = new Map();
  for (let i = 0; i < n; i++) { const h = holeCol[i] === undefined ? null : holeCol[i]; if (!holes.has(h)) holes.set(h, []); holes.get(h).push(i); }
  const out = new GM.PointSet({ name: points.name + ' composited', role: 'samples' });
  const fr = points.numeric('from'), to = points.numeric('to'), val = points.numeric(value);
  for (const [h, ids] of holes) {
    ids.sort((a, b) => fr[a] - fr[b]);
    if (!ids.length || fr[ids[0]] !== fr[ids[0]]) {
      for (const i of ids) { const p = points.point(i); const attrs = { hole: h }; attrs[value] = val[i] !== val[i] ? null : val[i]; out.add(p[0], p[1], p[2], attrs); }
      continue;
    }
    const start = fr[ids[0]], end = to[ids[ids.length - 1]];
    let d = start;
    while (d < end - 1e-9) {
      const d1 = Math.min(d + targetLength, end);
      let wsum = 0, vsum = 0, xs = 0, ys = 0, zs = 0;
      for (const i of ids) {
        const a = Math.max(fr[i], d), b = Math.min(to[i], d1);
        if (b <= a || val[i] !== val[i]) continue;
        const w = b - a;
        wsum += w; vsum += w * val[i];
        const p = points.point(i);
        xs += w * p[0]; ys += w * p[1]; zs += w * p[2];
      }
      if (wsum > 0) { const attrs = { hole: h, from: d, to: d1, length: wsum }; attrs[value] = vsum / wsum; out.add(xs / wsum, ys / wsum, zs / wsum, attrs); }
      d = d1;
    }
  }
  return out;
}

/** Estimate ``value`` into every block (or the domain's blocks) by 'ok' | 'idw' | 'nn'.
    opts: {method, variogram, max_points, radius, min_points, power, domain, domain_value,
    out_name, sample_domain, onProgress}.  Adds <out>_est (+ <out>_var for ok). */
export function estimate(bm, samples, value, opts = {}) {
  bm = asObject(bm); samples = asObject(samples);
  const method = opt(opts, 'method', null, 'ok'), maxPoints = opt(opts, 'max_points', 'maxPoints', 16), radius = opt(opts, 'radius', null, null);
  const minPoints = opt(opts, 'min_points', 'minPoints', 2), power = opt(opts, 'power', null, 2);
  const domain = opt(opts, 'domain', null, null), domainValue = opt(opts, 'domain_value', 'domainValue', null);
  const out = opt(opts, 'out_name', 'outName', value), sampleDomain = opt(opts, 'sample_domain', 'sampleDomain', null), onProgress = opts.onProgress;
  let variogram = opts.variogram ? Variogram.fromJSON(opts.variogram) : null;
  let pts = samples.xyz, vals = samples.numeric(value);
  if (sampleDomain) {
    const keep = [];
    for (let i = 0; i < samples.n; i++) if (sampleDomain[i]) keep.push(i);
    const p2 = new Float64Array(keep.length * 3), v2 = new Float64Array(keep.length);
    keep.forEach((i, q) => { p2[3 * q] = pts[3 * i]; p2[3 * q + 1] = pts[3 * i + 1]; p2[3 * q + 2] = pts[3 * i + 2]; v2[q] = vals[i]; });
    pts = p2; vals = v2;
  }
  const npts = vals.length;
  let mask = null;
  if (domain) {
    const attr = bm.attributes[domain];
    if (!attr) throw new Error(`block model has no attribute ${domain}`);
    const cat = attr.values;
    mask = new Uint8Array(bm.n);
    for (let i = 0; i < bm.n; i++) mask[i] = cat[i] === domainValue ? 1 : 0;
  }
  const targets = blockCentroids(bm, mask);
  const n = bm.n;
  const estAll = new Float64Array(n).fill(NaN), varAll = new Float64Array(n).fill(NaN);
  let est, vari = null;
  if (method === 'ok') {
    if (!variogram) {
      const exp = empiricalVariogram(pts, vals);
      variogram = exp.length ? fitVariogram(exp) : new Variogram({ model: 'spherical', sill: 1, range: Math.max(bm.count[0] * bm.blockSize[0], bm.count[1] * bm.blockSize[1], bm.count[2] * bm.blockSize[2]) / 2 });
    }
    const r = ordinaryKriging(pts, vals, targets, variogram, { max_points: maxPoints, radius, min_points: minPoints, onProgress });
    est = r.est; vari = r.variance;
  } else if (method === 'idw') {
    est = idw(pts, vals, targets, { power, max_points: maxPoints, radius, onProgress });
  } else if (method === 'nn') {
    est = nearestNeighbour(pts, vals, targets, { radius });
  } else throw new Error(`unknown method ${method}`);
  let pos = 0;
  for (let idx = 0; idx < n; idx++) {
    if (!mask || mask[idx]) { estAll[idx] = est[pos]; if (vari) varAll[idx] = vari[pos]; pos++; }
  }
  bm.addAttribute(out + '_est', estAll);
  if (vari) bm.addAttribute(out + '_var', varAll);
  const rec = { attribute: out, method, samples: npts, max_points: maxPoints, radius, domain, domain_value: domainValue };
  if (variogram) rec.variogram = variogram.toJSON();
  (bm.metadata.estimates = bm.metadata.estimates || []).push(rec);
  return bm;
}

/** Tonnes and mean grade above each cut-off -> [{cutoff, blocks, volume_m3, tonnes, mean_grade}]. */
export function gradeTonnage(bm, attribute, cutoffs, opts = {}) {
  bm = asObject(bm);
  const density = opt(opts, 'density', null, 2.7), domain = opt(opts, 'domain', null, null), domainValue = opt(opts, 'domain_value', 'domainValue', null);
  const vals = bm.attributes[attribute].values;
  const vol = bm.blockSize[0] * bm.blockSize[1] * bm.blockSize[2];
  let mask = null;
  if (domain) { const cat = bm.attributes[domain].values; mask = new Uint8Array(bm.n); for (let i = 0; i < bm.n; i++) mask[i] = cat[i] === domainValue ? 1 : 0; }
  const rows = [];
  for (const c of cutoffs) {
    let n = 0, s = 0;
    for (let idx = 0; idx < vals.length; idx++) {
      if (mask && !mask[idx]) continue;
      const v = vals[idx];
      if (v === v && v >= c) { n++; s += v; }
    }
    rows.push({ cutoff: c, blocks: n, volume_m3: n * vol, tonnes: n * vol * density, mean_grade: n ? s / n : NaN });
  }
  return rows;
}

/** Block centroids with a value as a PointSet (NaN / null blocks skipped). */
export function blockmodelToPoints(bm, attribute) {
  bm = asObject(bm);
  const ps = new GM.PointSet({ name: `${bm.name} ${attribute}`, role: 'points' });
  const vals = bm.attributes[attribute].values;
  const [nx, ny, nz] = bm.count;
  const xyz = [], col = [];
  let idx = 0;
  for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const v = vals[idx++];
    if (v != null && v === v) { const c = bm.centroid(i, j, k); xyz.push(c[0], c[1], c[2]); col.push(v); }
  }
  ps.xyz = Float64Array.from(xyz);
  ps.attributes[attribute] = col;
  return ps;
}

/* ============================================================ slicing */
/** {point, normal, u, length} for a vertical section start -> end. */
export function sectionPlane(start, end) {
  const x0 = start[0], y0 = start[1], x1 = end[0], y1 = end[1];
  const dx = x1 - x0, dy = y1 - y0, ln = Math.hypot(dx, dy);
  if (ln === 0) throw new Error('zero-length section');
  const u = [dx / ln, dy / ln, 0];
  return { point: [x0, y0, 0], normal: [-u[1], u[0], 0], u, length: ln };
}

/** Orthonormal {n, u, v} in the plane; v as vertical as possible. */
export function planeBasis(point, normal) {
  let [nx, ny, nz] = normal;
  const ln = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
  const n = [nx / ln, ny / ln, nz / ln];
  let up = [0, 0, 1];
  if (Math.abs(n[2]) > 0.999) up = [0, 1, 0];
  let u = [up[1] * n[2] - up[2] * n[1], up[2] * n[0] - up[0] * n[2], up[0] * n[1] - up[1] * n[0]];
  const lu = Math.sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]) || 1;
  u = u.map(c => c / lu);
  const v = [n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0]];
  return { n, u, v };
}

export function toPlaneCoords(p, point, u, v) {
  const d = [p[0] - point[0], p[1] - point[1], p[2] - point[2]];
  return [d[0] * u[0] + d[1] * u[1] + d[2] * u[2], d[0] * v[0] + d[1] * v[1] + d[2] * v[2]];
}

/** Intersection polylines of a triangle mesh with a plane -> LineSet. */
export function meshPlaneIntersection(mesh, point, normal, name = null) {
  mesh = asObject(mesh);
  const { n } = planeBasis(point, normal);
  const [px, py, pz] = point;
  const V = mesh.vertices, nv = mesh.nVertices;
  const dist = new Float64Array(nv);
  const bnd = mesh.bounds();
  const scale = bnd ? Math.max(...bnd.map(Math.abs)) : 1;
  const tiny = 1e-12 * (scale || 1);
  for (let i = 0; i < nv; i++) {
    const d = (V[3 * i] - px) * n[0] + (V[3 * i + 1] - py) * n[1] + (V[3 * i + 2] - pz) * n[2];
    dist[i] = d !== 0 ? d : tiny;      // zeros count as positive (simulation of simplicity)
  }
  const segs = [];
  const T = mesh.triangles;
  const thr = (1e-9 * (scale || 1)) ** 2;
  const pts = [];
  const edge = (i, j) => {
    const di = dist[i], dj = dist[j];
    if ((di < 0) !== (dj < 0)) {
      const lo = i < j ? i : j, hi = i < j ? j : i;
      const dlo = dist[lo], dhi = dist[hi], tt = dlo / (dlo - dhi);
      pts.push([V[3 * lo] + (V[3 * hi] - V[3 * lo]) * tt, V[3 * lo + 1] + (V[3 * hi + 1] - V[3 * lo + 1]) * tt, V[3 * lo + 2] + (V[3 * hi + 2] - V[3 * lo + 2]) * tt]);
    }
  };
  for (let t = 0; t < T.length; t += 3) {
    const a = T[t], b = T[t + 1], c = T[t + 2];
    pts.length = 0;
    edge(a, b); edge(b, c); edge(c, a);
    if (pts.length === 2) {
      const p = pts[0], q = pts[1];
      if ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2 > thr) segs.push([p, q]);
    }
  }
  const ls = new GM.LineSet({ name: name || (mesh.name + ' section'), role: 'section', color: mesh.color });
  for (const chain of chainSegments(segs)) ls.addPolyline(chain, { source: mesh.id });
  ls.metadata.plane = { point: Array.from(point), normal: n.slice() };
  return ls;
}

/** Join segments sharing endpoints into polylines (greedy; closed loops snapped exactly). */
export function chainSegments(segs, eps = 1e-6) {
  if (!segs.length) return [];
  const key = p => pyRound(p[0] / eps) + ',' + pyRound(p[1] / eps) + ',' + pyRound(p[2] / eps);
  const ends = new Map();
  const push = (k, si) => { let l = ends.get(k); if (!l) { l = []; ends.set(k, l); } l.push(si); };
  segs.forEach(([a, b], si) => { push(key(a), si); push(key(b), si); });
  const used = new Uint8Array(segs.length);
  const chains = [];
  for (let s0 = 0; s0 < segs.length; s0++) {
    if (used[s0]) continue;
    used[s0] = 1;
    const chain = [segs[s0][0], segs[s0][1]];
    for (const direction of [1, -1]) {
      for (;;) {
        const tip = direction === 1 ? chain[chain.length - 1] : chain[0];
        const ktip = key(tip);
        const cand = (ends.get(ktip) || []).filter(si => !used[si]);
        if (!cand.length) break;
        const si = cand[0];
        used[si] = 1;
        const [p, q] = segs[si];
        const nxt = key(p) === ktip ? q : p;
        if (direction === 1) chain.push(nxt); else chain.unshift(nxt);
      }
    }
    if (chain.length > 2 && key(chain[0]) === key(chain[chain.length - 1])) chain[chain.length - 1] = chain[0];
    chains.push(chain);
  }
  return chains;
}

/** Sample a Grid2D along start -> end: [[distance, x, y, z], ...] (z NaN where no data). */
export function gridProfile(grid, start, end, n = 200) {
  grid = asObject(grid);
  const { point, u, length } = sectionPlane(start, end);
  const out = [];
  for (let k = 0; k <= n; k++) {
    const d = length * k / n;
    const x = point[0] + u[0] * d, y = point[1] + u[1] * d;
    out.push([d, x, y, grid.sample(x, y)]);
  }
  return out;
}

export function profileLineSet(grid, start, end, opts = {}) {
  grid = asObject(grid);
  const n = opt(opts, 'n', null, 200), lift = opt(opts, 'lift', null, 0);
  const ls = new GM.LineSet({ name: opts.name || grid.name + ' profile', role: 'section', color: grid.color });
  let run = [];
  for (const [, x, y, z] of gridProfile(grid, start, end, n)) {
    if (z !== z) { if (run.length > 1) ls.addPolyline(run, { source: grid.id }); run = []; continue; }
    run.push([x, y, z + lift]);
  }
  if (run.length > 1) ls.addPolyline(run, { source: grid.id });
  return ls;
}

/** Coloured double-sided ribbons (one Mesh per unit) along a vertical section. */
export function stratigraphySection(strat, grids, topo, start, end, n = 200) {
  strat = asObject(strat); topo = asObject(topo);
  const look = gridLookup(grids);
  const profiles = [gridProfile(topo, start, end, n)];
  const names = [], colors = [];
  for (const u of strat.units) {
    names.push(u.name); colors.push(u.color);
    if (u.base == null) {
      let zmin = INF;
      for (const prof of profiles) for (const p of prof) if (p[3] === p[3] && p[3] < zmin) zmin = p[3];
      if (zmin === INF) zmin = 0;
      const floor = zmin - Math.max(50, 0.1 * sectionPlane(start, end).length);
      profiles.push(profiles[0].map(p => [p[0], p[1], p[2], floor]));
      break;
    }
    profiles.push(gridProfile(needGrid(look, u.base), start, end, n));
  }
  const meshes = [];
  for (let k = 0; k < profiles.length - 1; k++) {
    const top = profiles[k], base = profiles[k + 1];
    const verts = [], tris = [];
    const idx = new Int32Array(top.length).fill(-1);
    for (let s = 0; s < top.length; s++) {
      const zt = top[s][3];
      let zb = base[s][3];
      if (zt !== zt || zb !== zb) continue;
      if (zb > zt) zb = zt;
      idx[s] = verts.length / 3;
      verts.push(top[s][1], top[s][2], zt, base[s][1], base[s][2], zb);
    }
    for (let s = 0; s < top.length - 1; s++) {
      if (idx[s] >= 0 && idx[s + 1] >= 0) {
        const a = idx[s], b = idx[s + 1];
        tris.push(a, a + 1, b + 1, a, b + 1, b);
        tris.push(a, b + 1, a + 1, a, b, b + 1);     // double-sided
      }
    }
    const m = new GM.Mesh({ vertices: Float64Array.from(verts), triangles: Uint32Array.from(tris), name: names[k] + ' (section)', color: colors[k], role: 'section' });
    m.metadata.unit = names[k];
    meshes.push(m);
  }
  return meshes;
}

/** Sample a block attribute onto a plane -> {width, height, values (row-major from the
    top-left of the patch), corners[4], u, v, du, dv, extent}.  Nearest-block lookup. */
export function blockmodelPlaneSample(bm, attribute, point, normal, opts = {}) {
  bm = asObject(bm);
  let extent = opt(opts, 'extent', null, null);
  const resolution = opt(opts, 'resolution', null, null);
  const { u, v } = planeBasis(point, normal);
  const attr = bm.attributes[attribute];
  if (!attr) throw new Error(`block model has no attribute ${attribute}`);
  const vals = attr.values, numeric = attr.type === 'number';
  if (extent == null) {
    const b = bm.bounds();
    const diag = Math.sqrt((b[3] - b[0]) ** 2 + (b[4] - b[1]) ** 2 + (b[5] - b[2]) ** 2);
    extent = [-diag / 2, diag / 2, -diag / 2, diag / 2];
  }
  const [umin, umax, vmin, vmax] = extent;
  const res = resolution || Math.min(...bm.blockSize) / 2;
  const w = Math.max(1, Math.ceil((umax - umin) / res)), h = Math.max(1, Math.ceil((vmax - vmin) / res));
  const out = numeric ? new Float64Array(w * h) : new Array(w * h).fill(null);
  const [nx, ny, nz] = bm.count, [ox, oy, oz] = bm.origin, [dx, dy, dz] = bm.blockSize;
  const az = bm.azimuth * DEG, ca = Math.cos(az), sa = Math.sin(az);
  let q = 0;
  for (let r = 0; r < h; r++) {
    const vv = vmax - (r + 0.5) * res;
    for (let c = 0; c < w; c++) {
      const uu = umin + (c + 0.5) * res;
      const x = point[0] + u[0] * uu + v[0] * vv, y = point[1] + u[1] * uu + v[1] * vv, z = point[2] + u[2] * uu + v[2] * vv;
      let lx = x - ox, ly = y - oy;
      if (bm.azimuth) { const tx = lx * ca - ly * sa, ty = lx * sa + ly * ca; lx = tx; ly = ty; }
      const i = Math.floor(lx / dx), j = Math.floor(ly / dy), k = Math.floor((z - oz) / dz);
      if (i >= 0 && i < nx && j >= 0 && j < ny && k >= 0 && k < nz) {
        const val = vals[bm.index(i, j, k)];
        out[q++] = numeric ? (val == null ? NaN : val) : (val == null ? null : val);
      } else out[q++] = numeric ? NaN : null;
    }
  }
  const corners = [[umin, vmax], [umax, vmax], [umax, vmin], [umin, vmin]].map(([uu, vv]) =>
    [point[0] + u[0] * uu + v[0] * vv, point[1] + u[1] * uu + v[1] * vv, point[2] + u[2] * uu + v[2] * vv]);
  return { width: w, height: h, values: out, corners, u, v, du: res, dv: res, extent };
}

function clipToBand(a, b, sd, hw) {
  const da = sd(a), db = sd(b);
  const target = Math.max(da, db) > hw ? hw : -hw;
  if (db === da) return b;
  let t = (target - da) / (db - da);
  t = Math.max(0, Math.min(1, t));
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

/** Parts of a LineSet within ``halfWidth`` of the plane (clipped at the band), projected onto it. */
export function linesetNearPlane(ls, point, normal, halfWidth, opts = {}) {
  ls = asObject(ls);
  const project = opt(opts, 'project', null, true);
  const { n } = planeBasis(point, normal);
  const out = new GM.LineSet({ name: opts.name || ls.name + ' near section', role: ls.role, color: ls.color });
  const sd = p => (p[0] - point[0]) * n[0] + (p[1] - point[1]) * n[1] + (p[2] - point[2]) * n[2];
  const proj = p => { if (!project) return p; const d = sd(p); return [p[0] - d * n[0], p[1] - d * n[1], p[2] - d * n[2]]; };
  ls.parts.forEach((part, k) => {
    const pts = part.map(i => ls.vertex(i));
    const feat = k < ls.features.length ? ls.features[k] : {};
    let run = [];
    for (let a = 0; a < pts.length; a++) {
      const p = pts[a];
      if (Math.abs(sd(p)) <= halfWidth) {
        if (!run.length && a > 0) run.push(proj(clipToBand(pts[a - 1], p, sd, halfWidth)));
        run.push(proj(p));
      } else if (run.length) {
        run.push(proj(clipToBand(pts[a - 1], p, sd, halfWidth)));
        if (run.length > 1) out.addPolyline(run, feat);
        run = [];
      }
    }
    if (run.length > 1) out.addPolyline(run, feat);
  });
  return out;
}

/* -------------------------------------------------------- iso-surface */
const TET_CUBE = [[0, 5, 1, 6], [0, 1, 2, 6], [0, 2, 3, 6], [0, 3, 7, 6], [0, 7, 4, 6], [0, 4, 5, 6]];
const CUBE_OFF = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]];
const OFFX = Int32Array.from(CUBE_OFF.map(o => o[0])), OFFY = Int32Array.from(CUBE_OFF.map(o => o[1])), OFFZ = Int32Array.from(CUBE_OFF.map(o => o[2]));
// edge slot (0..6) of the pair of corners (a, b): direction from the lower corner; slot 7 = the node itself
const EDGE_SLOT = new Int8Array(64).fill(-1);
const EDGE_LO = new Int8Array(64).fill(-1);
for (let a = 0; a < 8; a++) for (let b = 0; b < 8; b++) {
  if (a === b) continue;
  const ia = OFFX[a] + 2 * OFFY[a] + 4 * OFFZ[a], ib = OFFX[b] + 2 * OFFY[b] + 4 * OFFZ[b];
  const lo = ia < ib ? a : b, hi = ia < ib ? b : a;
  const di = OFFX[hi] - OFFX[lo], dj = OFFY[hi] - OFFY[lo], dk = OFFZ[hi] - OFFZ[lo];
  if (di < 0 || dj < 0 || dk < 0) continue;
  EDGE_SLOT[a * 8 + b] = di + 2 * dj + 4 * dk - 1;
  EDGE_LO[a * 8 + b] = lo;
}

/** Marching tetrahedra on a regular NODE grid.  field[i + nx*(j + ny*k)], NaN =
    missing (cubes touching NaN are skipped).  Triangles are oriented with the
    field gradient (negative -> positive) and share vertices along edges. */
export function isosurface(field, count, origin, spacing, opts = {}) {
  const iso = opt(opts, 'iso', null, 0), name = opts.name || 'isosurface', color = opts.color || [200, 120, 60], onProgress = opts.onProgress;
  const nx = count[0] | 0, ny = count[1] | 0, nz = count[2] | 0;
  const dx = +spacing[0], dy = +spacing[1], dz = +spacing[2];
  const x0 = +origin[0], y0 = +origin[1], z0 = +origin[2];
  const nxy = nx * ny;
  let vcap = Math.max(1024, 4 * nxy), verts = new Float64Array(3 * vcap), nvert = 0;
  let tcap = Math.max(1024, 8 * nxy), tris = new Uint32Array(3 * tcap), ntri = 0;
  const SLOTS = 8;
  let cacheA = new Int32Array(SLOTS * nxy).fill(-1), cacheB = new Int32Array(SLOTS * nxy).fill(-1);
  const ids = new Int32Array(8), vals = new Float64Array(8), tn = new Int32Array(4), tv = new Float64Array(4), inside = new Uint8Array(4);
  const pv = new Int32Array(3);
  let ci = 0, cj = 0, ck = 0;   // current cube

  const interpVertex = (ca, cb) => {
    const va = vals[ca], vb = vals[cb];
    let t = vb !== va ? (iso - va) / (vb - va) : 0.5;
    let lo, slot, from, to;
    if (t <= 1e-9) { lo = ca; slot = 7; from = ca; to = ca; t = 0; }
    else if (t >= 1 - 1e-9) { lo = cb; slot = 7; from = cb; to = cb; t = 0; }
    else { const e = ca * 8 + cb; lo = EDGE_LO[e]; slot = EDGE_SLOT[e]; from = ca; to = cb; }
    const cache = OFFZ[lo] === 0 ? cacheA : cacheB;
    const ckey = slot * nxy + ((ci + OFFX[lo]) + nx * (cj + OFFY[lo]));
    const hit = cache[ckey];
    if (hit >= 0) return hit;
    const ai = ci + OFFX[from], aj = cj + OFFY[from], ak = ck + OFFZ[from];
    const bi = ci + OFFX[to], bj = cj + OFFY[to], bk = ck + OFFZ[to];
    if (nvert >= vcap) { vcap *= 2; const nvts = new Float64Array(3 * vcap); nvts.set(verts); verts = nvts; }
    verts[3 * nvert] = x0 + (ai + (bi - ai) * t) * dx;
    verts[3 * nvert + 1] = y0 + (aj + (bj - aj) * t) * dy;
    verts[3 * nvert + 2] = z0 + (ak + (bk - ak) * t) * dz;
    cache[ckey] = nvert;
    return nvert++;
  };
  const emit = (a, b, c) => {
    if (a !== b && b !== c && a !== c) {
      if (ntri >= tcap) { tcap *= 2; const nt = new Uint32Array(3 * tcap); nt.set(tris); tris = nt; }
      tris[3 * ntri] = a; tris[3 * ntri + 1] = b; tris[3 * ntri + 2] = c; ntri++;
    }
  };

  for (let k = 0; k < nz - 1; k++) {
    ck = k;
    for (let j = 0; j < ny - 1; j++) {
      cj = j;
      const base0 = nx * (j + ny * k), base1 = nx * (j + 1 + ny * k), base2 = nx * (j + ny * (k + 1)), base3 = nx * (j + 1 + ny * (k + 1));
      for (let i = 0; i < nx - 1; i++) {
        ci = i;
        const v0 = field[base0 + i], v1 = field[base0 + i + 1], v2 = field[base1 + i + 1], v3 = field[base1 + i];
        const v4 = field[base2 + i], v5 = field[base2 + i + 1], v6 = field[base3 + i + 1], v7 = field[base3 + i];
        if (v0 !== v0 || v1 !== v1 || v2 !== v2 || v3 !== v3 || v4 !== v4 || v5 !== v5 || v6 !== v6 || v7 !== v7) continue;
        const allBelow = v0 < iso && v1 < iso && v2 < iso && v3 < iso && v4 < iso && v5 < iso && v6 < iso && v7 < iso;
        if (allBelow) continue;
        const allAbove = v0 >= iso && v1 >= iso && v2 >= iso && v3 >= iso && v4 >= iso && v5 >= iso && v6 >= iso && v7 >= iso;
        if (allAbove) continue;
        vals[0] = v0; vals[1] = v1; vals[2] = v2; vals[3] = v3; vals[4] = v4; vals[5] = v5; vals[6] = v6; vals[7] = v7;
        for (let tt = 0; tt < 6; tt++) {
          const tet = TET_CUBE[tt];
          let cnt = 0;
          for (let q = 0; q < 4; q++) { tn[q] = tet[q]; tv[q] = vals[tet[q]]; inside[q] = tv[q] >= iso ? 1 : 0; cnt += inside[q]; }
          if (cnt === 0 || cnt === 4) continue;
          if (cnt === 1 || cnt === 3) {
            const flag = cnt === 1 ? 1 : 0;
            let a = -1;
            for (let q = 0; q < 4; q++) if (inside[q] === flag) { a = q; break; }
            let m = 0;
            for (let q = 0; q < 4; q++) if (q !== a) pv[m++] = interpVertex(tn[a], tn[q]);
            if (flag) emit(pv[0], pv[2], pv[1]); else emit(pv[0], pv[1], pv[2]);
          } else {
            let a = -1, b = -1, c = -1, d = -1;
            for (let q = 0; q < 4; q++) { if (inside[q]) { if (a < 0) a = q; else b = q; } else { if (c < 0) c = q; else d = q; } }
            const pac = interpVertex(tn[a], tn[c]), pad = interpVertex(tn[a], tn[d]);
            const pbc = interpVertex(tn[b], tn[c]), pbd = interpVertex(tn[b], tn[d]);
            emit(pac, pbc, pbd);
            emit(pac, pbd, pad);
          }
        }
      }
    }
    const tmp = cacheA; cacheA = cacheB; cacheB = tmp.fill(-1);
    if (onProgress && (k & 7) === 7) onProgress((k + 1) / Math.max(1, nz - 1));
  }
  const m = new GM.Mesh({ vertices: verts.slice(0, 3 * nvert), triangles: tris.slice(0, 3 * ntri), name, color, role: 'surface' });
  orientConsistently(m, field, count, origin, spacing);
  m.metadata.iso = iso;
  if (onProgress) onProgress(1);
  return m;
}

/** Flip triangles whose normal points against the field gradient (the Python ``_orient_consistently``). */
export function orientConsistently(mesh, field, count, origin, spacing) {
  const nx = count[0] | 0, ny = count[1] | 0, nz = count[2] | 0;
  const dx = +spacing[0], dy = +spacing[1], dz = +spacing[2];
  const x0 = +origin[0], y0 = +origin[1], z0 = +origin[2];
  const V = mesh.vertices, T = mesh.triangles;
  for (let t = 0; t < T.length; t += 3) {
    const a = T[t], b = T[t + 1], c = T[t + 2];
    const ax = V[3 * a], ay = V[3 * a + 1], az = V[3 * a + 2];
    const bx = V[3 * b], by = V[3 * b + 1], bz = V[3 * b + 2];
    const cx0 = V[3 * c], cy0 = V[3 * c + 1], cz0 = V[3 * c + 2];
    const ux = bx - ax, uy = by - ay, uz = bz - az, wx = cx0 - ax, wy = cy0 - ay, wz = cz0 - az;
    const nxv = uy * wz - uz * wy, nyv = uz * wx - ux * wz, nzv = ux * wy - uy * wx;
    const cx = (ax + bx + cx0) / 3, cy = (ay + by + cy0) / 3, cz = (az + bz + cz0) / 3;
    const i = Math.min(Math.max(Math.trunc((cx - x0) / dx), 0), nx - 2);
    const j = Math.min(Math.max(Math.trunc((cy - y0) / dy), 0), ny - 2);
    const k = Math.min(Math.max(Math.trunc((cz - z0) / dz), 0), nz - 2);
    const f0 = field[i + nx * (j + ny * k)];
    const gx = (field[i + 1 + nx * (j + ny * k)] - f0) / dx;
    const gy = (field[i + nx * (j + 1 + ny * k)] - f0) / dy;
    const gz = (field[i + nx * (j + ny * (k + 1))] - f0) / dz;
    if (nxv * gx + nyv * gy + nzv * gz < 0) { T[t + 1] = c; T[t + 2] = b; }
  }
  return mesh;
}

/** Evaluate an RBF on a node grid -> {field, count, origin, spacing}. */
export function scalarFieldFromRBF(rbf, bounds, spacing, onProgress = null) {
  rbf = rbf instanceof RBF ? rbf : RBF.fromJSON(rbf);
  const [minx, miny, minz, maxx, maxy, maxz] = bounds;
  const [dx, dy, dz] = typeof spacing === 'number' ? [spacing, spacing, spacing] : spacing;
  const nx = Math.ceil((maxx - minx) / dx) + 1, ny = Math.ceil((maxy - miny) / dy) + 1, nz = Math.ceil((maxz - minz) / dz) + 1;
  const targets = new Float64Array(nx * ny * nz * 3);
  let q = 0;
  for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) { targets[q++] = minx + i * dx; targets[q++] = miny + j * dy; targets[q++] = minz + k * dz; }
  const field = rbf.predict(targets, onProgress);
  return { field, count: [nx, ny, nz], origin: [minx, miny, minz], spacing: [dx, dy, dz] };
}

export function gridToPointsOnLine(grid, start, end, n = 100) {
  return gridProfile(grid, start, end, n).filter(p => p[3] === p[3]).map(p => [p[1], p[2], p[3]]);
}

/* =========================================================== workings */
export const FT = 0.3048;
export const WORKING_TYPES = {
  adit: { color: [255, 170, 40], width_m: 2.0, height_m: 2.2, desc: 'horizontal entry from surface' },
  tunnel: { color: [255, 170, 40], width_m: 2.4, height_m: 2.4, desc: 'horizontal through-opening' },
  drift: { color: [255, 210, 90], width_m: 1.8, height_m: 2.1, desc: 'horizontal working along the vein' },
  crosscut: { color: [255, 230, 140], width_m: 1.8, height_m: 2.1, desc: 'horizontal working across the vein' },
  shaft: { color: [255, 80, 80], width_m: 2.5, height_m: 2.5, desc: 'vertical/inclined opening from surface' },
  winze: { color: [255, 120, 120], width_m: 1.8, height_m: 1.8, desc: 'internal shaft sunk from a level' },
  raise: { color: [255, 140, 200], width_m: 1.8, height_m: 1.8, desc: 'internal opening driven upward' },
  decline: { color: [240, 140, 60], width_m: 4.0, height_m: 4.0, desc: 'inclined ramp' },
  stope: { color: [120, 200, 255], width_m: 2.0, height_m: 2.0, desc: 'mined-out ore volume' },
  portal: { color: [255, 255, 255], width_m: 2.0, height_m: 2.0, desc: 'adit entrance' },
  level: { color: [200, 200, 200], width_m: 1.8, height_m: 2.1, desc: 'level outline / datum' },
  trench: { color: [190, 160, 120], width_m: 1.5, height_m: 1.0, desc: 'surface trench' },
  pit: { color: [190, 160, 120], width_m: 3.0, height_m: 2.0, desc: 'prospect pit / open cut' },
  unknown: { color: [180, 180, 180], width_m: 1.8, height_m: 2.0, desc: 'unclassified working' },
};
export const CONFIDENCE = ['surveyed', 'sketched', 'inferred', 'described'];

export function newWorkings(name = 'workings', mine = '') {
  const ls = new GM.LineSet({ name, role: 'workings', color: [255, 170, 40] });
  ls.metadata.schema = 'nwmm-workings/1';
  ls.metadata.mine = mine;
  ls.metadata.types = Object.keys(WORKING_TYPES).sort();
  return ls;
}

/** Feature attribute dict with the schema defaults (the Python ``_feat``). */
export function workingFeature(kind, name = '', kw = {}) {
  kind = kind in WORKING_TYPES ? kind : 'unknown';
  const k = Object.assign({}, kw);
  const take = (key, def) => { const v = k[key]; delete k[key]; return v === undefined ? def : v; };
  const f = {
    type: kind, name, level: take('level', ''), level_z: take('level_z', null), mine: take('mine', ''),
    width_m: take('width_m', WORKING_TYPES[kind].width_m), height_m: take('height_m', WORKING_TYPES[kind].height_m),
    source: take('source', {}), confidence: take('confidence', 'sketched'), units_in: take('units_in', 'm'), notes: take('notes', ''),
  };
  for (const [key, v] of Object.entries(k)) if (v !== undefined) f[key] = v;
  return f;
}
const toM = (v, unitsIn) => (unitsIn === 'ft' ? v * FT : v);
function splitOpts(opts, names) {   // pull known keyword arguments (snake_case or camelCase), return [values, rest]
  const rest = Object.assign({}, opts), got = {};
  for (const [snake, camel, def] of names) {
    let v = def;
    if (rest[snake] !== undefined) { v = rest[snake]; delete rest[snake]; }
    if (camel && rest[camel] !== undefined) { if (v === def) v = rest[camel]; delete rest[camel]; }
    got[snake] = v;
  }
  return [got, rest];
}

/** Adit from a portal: bearing clockwise from north, length along the drive, grade_pct rise per 100.
    opts: {grade_pct, units_in:'m'|'ft', terrain: Grid2D, name, ...feature attrs}. */
export function addAdit(ws, portalXYZ, bearingDeg, length, opts = {}) {
  const [o, attrs] = splitOpts(opts, [['grade_pct', 'gradePct', 0], ['units_in', 'unitsIn', 'm'], ['terrain', null, null], ['name', null, 'adit']]);
  const L = toM(length, o.units_in);
  let [x, y, z] = portalXYZ;
  if (o.terrain) { const zt = asObject(o.terrain).sample(x, y); if (zt === zt) z = zt; }
  const b = bearingDeg * DEG;
  const end = [x + L * Math.sin(b), y + L * Math.cos(b), z + L * o.grade_pct / 100];
  return ws.addPolyline([[x, y, z], end], workingFeature('adit', o.name, Object.assign({ units_in: o.units_in, bearing: bearingDeg, length_m: L }, attrs)));
}

/** Shaft (or winze) from a collar: dip_deg positive DOWN (90 = vertical), azimuth_deg of the incline. */
export function addShaft(ws, collarXYZ, depth, opts = {}) {
  const [o, attrs] = splitOpts(opts, [['dip_deg', 'dipDeg', 90], ['azimuth_deg', 'azimuthDeg', 0], ['units_in', 'unitsIn', 'm'], ['terrain', null, null], ['name', null, 'shaft'], ['kind', null, 'shaft']]);
  const D = toM(depth, o.units_in);
  let [x, y, z] = collarXYZ;
  if (o.terrain && o.kind === 'shaft') { const zt = asObject(o.terrain).sample(x, y); if (zt === zt) z = zt; }
  const d = o.dip_deg * DEG, a = o.azimuth_deg * DEG;
  const h = D * Math.cos(d);
  const bottom = [x + h * Math.sin(a), y + h * Math.cos(a), z - D * Math.sin(d)];
  return ws.addPolyline([[x, y, z], bottom], workingFeature(o.kind, o.name, Object.assign({ units_in: o.units_in, depth_m: D, dip: o.dip_deg, azimuth: o.azimuth_deg }, attrs)));
}

/** Horizontal working traced in plan (world metres) at a level elevation. opts: {kind:'drift', units_in, name, ...attrs}. */
export function addLevelWorking(ws, xyPoints, levelZ, opts = {}) {
  const [o, attrs] = splitOpts(opts, [['kind', null, 'drift'], ['units_in', 'unitsIn', 'm'], ['name', null, '']]);
  const pts = xyPoints.map(p => [p[0], p[1], levelZ]);
  return ws.addPolyline(pts, workingFeature(o.kind, o.name, Object.assign({ level_z: levelZ, units_in: o.units_in }, attrs)));
}

export function addRaise(ws, lowerXYZ, upperXYZ, opts = {}) {
  const [o, attrs] = splitOpts(opts, [['name', null, 'raise'], ['kind', null, 'raise']]);
  return ws.addPolyline([Array.from(lowerXYZ), Array.from(upperXYZ)], workingFeature(o.kind, o.name, attrs));
}

/** segments: [[bearing_deg, length, grade_pct], ...] legs from start. */
export function addDecline(ws, startXYZ, segments, opts = {}) {
  const [o, attrs] = splitOpts(opts, [['units_in', 'unitsIn', 'm'], ['name', null, 'decline']]);
  const pts = [Array.from(startXYZ)];
  for (const [bearing, length, grade] of segments) {
    const L = toM(length, o.units_in), b = bearing * DEG;
    const [x, y, z] = pts[pts.length - 1];
    pts.push([x + L * Math.sin(b), y + L * Math.cos(b), z + L * grade / 100]);
  }
  return ws.addPolyline(pts, workingFeature('decline', o.name, Object.assign({ units_in: o.units_in }, attrs)));
}

/** Mined-out volume: a closed plan outline extruded between two elevations (closed Mesh, role 'stope'). */
export function stopePrism(outlineXY, zBottom, zTop, opts = {}) {
  const [o, attrs] = splitOpts(opts, [['name', null, 'stope'], ['color', null, null]]);
  let ring = outlineXY.map(p => [+p[0], +p[1]]);
  if (ring.length > 2 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]) ring = ring.slice(0, -1);
  if (ring.length < 3) throw new Error('stope outline needs >= 3 points');
  if (signedArea(ring) < 0) ring.reverse();
  const n = ring.length;
  const verts = new Float64Array(n * 6);
  ring.forEach(([x, y], i) => { verts[3 * i] = x; verts[3 * i + 1] = y; verts[3 * i + 2] = zBottom; verts[3 * (n + i)] = x; verts[3 * (n + i) + 1] = y; verts[3 * (n + i) + 2] = zTop; });
  const tris = [];
  for (const [a, b, c] of earClip(ring)) { tris.push(a + n, b + n, c + n); tris.push(a, c, b); }
  for (let i = 0; i < n; i++) { const j = (i + 1) % n; tris.push(i, j, j + n, i, j + n, i + n); }
  const m = new GM.Mesh({ vertices: verts, triangles: Uint32Array.from(tris), name: o.name, color: o.color || WORKING_TYPES.stope.color, role: 'stope' });
  Object.assign(m.metadata, workingFeature('stope', o.name, attrs));
  m.metadata.z_bottom = zBottom; m.metadata.z_top = zTop;
  return m;
}

export function signedArea(ring) {
  let s = 0;
  for (let i = 0; i < ring.length; i++) { const [x0, y0] = ring[i], [x1, y1] = ring[(i + 1) % ring.length]; s += x0 * y1 - x1 * y0; }
  return s / 2;
}

export function inTri(p, a, b, c) {
  const s = (p1, p2, p3) => (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1]);
  const d1 = s(p, a, b), d2 = s(p, b, c), d3 = s(p, c, a);
  const neg = d1 < 0 || d2 < 0 || d3 < 0, pos = d1 > 0 || d2 > 0 || d3 > 0;
  return !(neg && pos);
}

/** Triangulate a simple CCW polygon -> [[a, b, c], ...] index triples (ear clipping, fan fallback). */
export function earClip(ring) {
  const idx = ring.map((_, i) => i);
  const tris = [];
  let guard = 0;
  while (idx.length > 3 && guard < 10000) {
    guard++;
    let found = false;
    for (let k = 0; k < idx.length; k++) {
      const a = idx[(k - 1 + idx.length) % idx.length], b = idx[k], c = idx[(k + 1) % idx.length];
      const pa = ring[a], pb = ring[b], pc = ring[c];
      const cross = (pb[0] - pa[0]) * (pc[1] - pa[1]) - (pb[1] - pa[1]) * (pc[0] - pa[0]);
      if (cross <= 0) continue;                     // reflex vertex
      let blocked = false;
      for (const q of idx) { if (q === a || q === b || q === c) continue; if (inTri(ring[q], pa, pb, pc)) { blocked = true; break; } }
      if (blocked) continue;
      tris.push([a, b, c]);
      idx.splice(k, 1);
      found = true;
      break;
    }
    if (!found) break;                               // degenerate: fan the rest
  }
  if (idx.length >= 3) for (let k = 1; k < idx.length - 1; k++) tris.push([idx[0], idx[k], idx[k + 1]]);
  return tris;
}

/* ================================================= geometry: projection */
/** Strike of a polyline in plan: azimuth (degrees clockwise from north, folded
    into 0..180) of its principal direction — the major eigenvector of the xy
    covariance, so a wiggly trace gets its overall trend, not its last leg. */
export function polylineStrike(xyz) {
  const n = xyz.length;
  if (n < 2) return NaN;
  let cx = 0, cy = 0;
  for (const p of xyz) { cx += p[0]; cy += p[1]; }
  cx /= n; cy /= n;
  let xx = 0, xy = 0, yy = 0;
  for (const p of xyz) { const dx = p[0] - cx, dy = p[1] - cy; xx += dx * dx; xy += dx * dy; yy += dy * dy; }
  const theta = 0.5 * Math.atan2(2 * xy, xx - yy);      // major axis, from +E toward +N
  return pymod(Math.atan2(Math.cos(theta), Math.sin(theta)) * RAD2DEG, 180);
}
const deg1 = v => String(Math.round(v * 10) / 10);

export const EXTRUDE_SCHEMA = 'nwmm-extrude/1';
export const EXTRUDE_COLORS = { vein: [120, 255, 190], fault: [230, 90, 90], wall: [200, 200, 200] };
/** Project a polyline down dip: every vertex is pushed t·dipVector(dip, dipAz)
    (t = depth / sin dip, or per vertex (z − zBottom) / sin dip so the bottom
    ring lies on a level) and the two rings are stitched into a ribbon; a
    closed outline is capped top and bottom so a vertical extrusion of a flat
    outline reproduces stopePrism's vertex and triangle order exactly.
      opts: { dip (deg below horizontal), dipAzimuth / dip_azimuth (deg clockwise
      from north, DOWN-dip — the project's structural contract), depth (vertical
      m) | zBottom, closed, name, color, role: 'vein'|'fault'|'wall', confidence,
      source (the part this came from), metadata }
    Refuses (throws) a dip outside (0, 90], an open trace's dip azimuth within
    20° of its strike (the ribbon would collapse onto the trace; the message
    prints the strike — a closed outline has no single strike and its prism
    cannot collapse, so it is not gated), fewer than 2 vertices, and a trace whose z are all 0 or
    NaN — a plan-view trace has to be draped before it can be projected. */
export function extrudePolyline(xyz, opts = {}) {
  const [o] = splitOpts(opts, [['dip', null, null], ['dip_azimuth', 'dipAzimuth', null], ['depth', null, null], ['z_bottom', 'zBottom', null], ['closed', null, false], ['name', null, null], ['color', null, null], ['role', null, 'wall'], ['confidence', null, 'described'], ['metadata', null, null], ['source', null, null]]);
  const dip = +o.dip, dipAz = o.dip_azimuth == null ? NaN : +o.dip_azimuth;
  if (!(dip > 0) || dip > 90) throw new Error(`dip must be > 0 and <= 90 degrees below horizontal (got ${o.dip})`);
  if (dipAz !== dipAz) throw new Error('a dip azimuth is required (degrees clockwise from north, in the down-dip direction)');
  let pts = Array.from(xyz || [], p => [+p[0], +p[1], p.length > 2 && p[2] != null ? +p[2] : NaN]);
  let closed = !!o.closed;
  if (pts.length > 2 && pts[0][0] === pts[pts.length - 1][0] && pts[0][1] === pts[pts.length - 1][1]) { pts = pts.slice(0, -1); closed = true; }
  if (pts.length < 2) throw new Error('a trace needs at least 2 vertices');
  if (closed && pts.length < 3) throw new Error('a closed outline needs at least 3 vertices');
  if (!pts.some(p => p[2] === p[2] && p[2] !== 0)) throw new Error('the trace has no elevation — drape it on the topography first');
  const noZ = pts.filter(p => p[2] !== p[2]).length;
  if (noZ) throw new Error(`${noZ} vertex(es) of the trace have no elevation — drape it on the topography first`);
  if (o.depth == null && o.z_bottom == null) throw new Error('give a depth (vertical metres) or a bottom elevation (zBottom)');
  const strike = polylineStrike(pts);
  if (dip < 90 && !closed) {                 // a closed outline has no single strike and its prism cannot collapse
    const off = Math.abs(pymod(dipAz - strike + 90, 180) - 90);   // acute angle between the dip direction and the strike axis
    if (off < 20) throw new Error(`dip azimuth ${deg1(dipAz)}° is within 20° of the trace's strike (${deg1(strike)}° / ${deg1(strike + 180)}°) — the ribbon would collapse onto the trace; a down-dip azimuth near ${deg1(pymod(strike + 90, 360))}° or ${deg1(pymod(strike + 270, 360))}° is perpendicular to it`);
  }
  if (closed && signedArea(pts) < 0) pts.reverse();
  const n = pts.length, role = ['vein', 'fault', 'wall'].includes(o.role) ? o.role : 'wall';
  const d = dip * DEG, a = dipAz * DEG, sd = Math.sin(d);
  const dv = dip === 90 ? [0, 0, -1] : [Math.sin(a) * Math.cos(d), Math.cos(a) * Math.cos(d), -sd];
  const verts = new Float64Array(n * 6);
  let zsum = 0;
  for (let i = 0; i < n; i++) {
    const [x, y, z] = pts[i]; zsum += z;
    const t = o.depth != null ? +o.depth / sd : (z - +o.z_bottom) / sd;
    verts[3 * (n + i)] = x; verts[3 * (n + i) + 1] = y; verts[3 * (n + i) + 2] = z;                   // the trace (top ring)
    verts[3 * i] = x + t * dv[0]; verts[3 * i + 1] = y + t * dv[1]; verts[3 * i + 2] = z + t * dv[2];   // projected (bottom ring)
  }
  const tris = [];
  if (closed) for (const [p, q, r] of earClip(pts.map(v => [v[0], v[1]]))) { tris.push(p + n, q + n, r + n); tris.push(p, r, q); }
  const nseg = closed ? n : n - 1;
  for (let i = 0; i < nseg; i++) { const j = (i + 1) % n; tris.push(i, j, j + n, i, j + n, i + n); }
  const m = new GM.Mesh({ vertices: verts, triangles: Uint32Array.from(tris), name: o.name || `${role} projected ${deg1(dip)}° → ${deg1(dipAz)}°`, color: o.color || EXTRUDE_COLORS[role], role });
  Object.assign(m.metadata, o.metadata || {}, {
    schema: EXTRUDE_SCHEMA, dip, dip_azimuth: dipAz,
    depth_m: o.depth != null ? +o.depth : zsum / n - +o.z_bottom, z_bottom: o.z_bottom == null ? null : +o.z_bottom,
    strike_deg: strike, closed, n_trace: n, source: o.source == null ? null : o.source, confidence: o.confidence,
    note: 'a mapped trace projected down dip by a distance the user chose — the depth is a projection distance, not a modelled fact',
  });
  m.provenance = { method: 'polyline projected down dip (extrudePolyline)', dip, dip_azimuth: dipAz };
  return m;
}

/* =================================================== geometry: contours */
/** Marching squares on any lattice: at(i, j) is the node value (NaN = no data,
    cells touching one are skipped), xy(i, j) its position; returns the
    [[x, y], [x, y]] segments of one level.  Saddle cells (cases 5 and 10) are
    resolved by the cell mean, so both languages and every caller (the
    stereonet density contours included) split them the same way. */
export function marchingSquares(nx, ny, at, xy, lv) {
  const segs = [];
  for (let j = 0; j < ny - 1; j++) for (let i = 0; i < nx - 1; i++) {
    const v = [at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)];
    if (v[0] !== v[0] || v[1] !== v[1] || v[2] !== v[2] || v[3] !== v[3]) continue;
    let code = 0;
    for (let k = 0; k < 4; k++) if (v[k] >= lv) code |= (1 << k);
    if (code === 0 || code === 15) continue;
    const P = [xy(i, j), xy(i + 1, j), xy(i + 1, j + 1), xy(i, j + 1)];
    const pt = e => { const p = e, q = (e + 1) % 4; const t = (lv - v[p]) / (v[q] - v[p]); return [P[p][0] + (P[q][0] - P[p][0]) * t, P[p][1] + (P[q][1] - P[p][1]) * t]; };
    if (code === 5 || code === 10) {
      const centreHigh = (v[0] + v[1] + v[2] + v[3]) / 4 >= lv;
      if ((code === 5) === centreHigh) { segs.push([pt(0), pt(1)]); segs.push([pt(2), pt(3)]); }   // wrap corners 1 and 3
      else { segs.push([pt(3), pt(0)]); segs.push([pt(1), pt(2)]); }                                // wrap corners 0 and 2
      continue;
    }
    const edges = [];
    for (let e = 0; e < 4; e++) if ((v[e] >= lv) !== (v[(e + 1) % 4] >= lv)) edges.push(e);
    segs.push([pt(edges[0]), pt(edges[1])]);
  }
  return segs;
}

/** 1 / 2 / 5 × 10ⁿ nearest to span / target (a contour interval a map would use). */
export function niceInterval(span, target = 8) {
  const raw = Math.abs(span) / Math.max(1, target);
  if (!(raw > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}

/** Level list base + m·interval covering the grid's value range. */
export function contourLevels(grid, interval, base = 0) {
  const g = asObject(grid);
  interval = +interval; base = base == null ? 0 : +base;
  if (!(interval > 0)) throw new Error('contour interval must be > 0');
  const [zmin, zmax] = g.zrange();
  if (zmin !== zmin || zmax !== zmax) return [];
  const m0 = Math.ceil((zmin - base) / interval - 1e-9), m1 = Math.floor((zmax - base) / interval + 1e-9);
  if (m1 - m0 + 1 > 5000) throw new Error(`${m1 - m0 + 1} levels at an interval of ${interval} — choose a coarser interval`);
  const out = [];
  for (let m = m0; m <= m1; m++) out.push(base + m * interval);
  return out;
}

/** Contour a Grid2D -> LineSet (role 'contours'), one part per chained
    polyline, features { level, units, source, source_id, index }.
      opts: { interval, base (used when levels is null, and for the index
      rhythm), index: N (every Nth level is an index contour), drape: Grid2D
      (property grids: put the lines on this surface), lift (m above it),
      z (constant elevation when there is nothing to drape on), name, color }
    A heightfield's lines sit at their own level; a property grid's at the
    draped surface, else at opts.z (default 0). */
export function contourGrid(grid, levels, opts = {}) {
  const g = asObject(grid);
  const interval = opt(opts, 'interval', null, null), base = opt(opts, 'base', null, 0) || 0;
  const lv = levels == null ? contourLevels(g, interval, base) : Array.from(levels, Number);
  const drape = opts.drape ? asObject(opts.drape) : null, lift = opts.lift == null ? 0 : +opts.lift;
  const N = opt(opts, 'index', null, 0) | 0, heightfield = g.role !== 'property';
  const ls = new GM.LineSet({ name: opts.name || `${g.name} contours`, role: 'contours', color: opts.color || [90, 70, 40] });
  const at = (i, j) => g.values[j * g.nx + i], xy = (i, j) => g.nodeXY(i, j);
  const zOf = (x, y, level) => {
    if (drape) { const zt = drape.sample(x, y); if (zt === zt) return zt + lift; }
    if (opts.z != null) return +opts.z;
    return heightfield ? level + lift : 0;
  };
  let nseg = 0;
  lv.forEach((level, k) => {
    const segs = marchingSquares(g.nx, g.ny, at, xy, level).map(([p, q]) => [[p[0], p[1], zOf(p[0], p[1], level)], [q[0], q[1], zOf(q[0], q[1], level)]]);
    nseg += segs.length;
    const isIndex = N > 0 && (interval ? pymod(pyRound((level - base) / interval), N) === 0 : k % N === 0);
    for (const chain of chainSegments(segs)) ls.addPolyline(chain, { level, units: g.units || 'm', source: g.name, source_id: g.id, index: isIndex });
  });
  ls.metadata.contours = { levels: lv, n_levels: lv.length, n_segments: nseg, interval: interval == null ? null : +interval, base, index_every: N || null, draped_on: drape ? drape.name : null, lift };
  ls.provenance = { method: 'marching squares over the grid nodes (gridContours)', source_layer: g.name, source_id: g.id };
  return ls;
}

/* ================================================== geometry: elevation */
/** Plan-view index of a mesh: a GridIndex of triangle centroids plus the
    largest centroid-to-vertex reach, enough to find every triangle that can
    cover a point. */
export function meshXYIndex(mesh) {
  const m = asObject(mesh), nt = m.nTriangles, V = m.vertices, C = new Float64Array(nt * 3);
  let reach = 0;
  for (let t = 0; t < nt; t++) {
    const a = m.triangles[3 * t], b = m.triangles[3 * t + 1], c = m.triangles[3 * t + 2];
    const cx = (V[3 * a] + V[3 * b] + V[3 * c]) / 3, cy = (V[3 * a + 1] + V[3 * b + 1] + V[3 * c + 1]) / 3;
    C[3 * t] = cx; C[3 * t + 1] = cy;
    for (const i of [a, b, c]) reach = Math.max(reach, dist2(cx, cy, V[3 * i], V[3 * i + 1]));
  }
  return { mesh: m, index: new GridIndex(C, 2), reach: reach * (1 + 1e-9) + 1e-9, k: Math.min(Math.max(nt, 1), 512) };
}
/** z of a mesh under / over (x, y) — the highest triangle covering the point
    in plan (vertical ray); NaN where nothing does. */
export function meshZAt(mesh, x, y, index = null) {
  const ix = index || meshXYIndex(mesh), m = ix.mesh, V = m.vertices;
  const found = ix.index.nearest(x, y, 0, ix.k, ix.reach);
  let best = NaN;
  for (let q = 0; q < found; q++) {
    const t = ix.index.resI[q];
    const a = m.triangles[3 * t], b = m.triangles[3 * t + 1], c = m.triangles[3 * t + 2];
    const ax = V[3 * a], ay = V[3 * a + 1], bx = V[3 * b], by = V[3 * b + 1], cx = V[3 * c], cy = V[3 * c + 1];
    if (!inTri([x, y], [ax, ay], [bx, by], [cx, cy])) continue;
    const det = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay);
    if (Math.abs(det) < 1e-300) continue;
    const l1 = ((bx - x) * (cy - y) - (cx - x) * (by - y)) / det, l2 = ((cx - x) * (ay - y) - (ax - x) * (cy - y)) / det, l3 = 1 - l1 - l2;
    const z = l1 * V[3 * a + 2] + l2 * V[3 * b + 2] + l3 * V[3 * c + 2];
    if (best !== best || z > best) best = z;
  }
  return best;
}
export const ELEVATION_SCOPES = ['missing', 'all', 'not-surveyed'];
/** Leapfrog's Set Elevation, generalised.  target: PointSet (xyz), LineSet
    (every vertex) or Drillholes (collars); surface: Grid2D (bilinear sample)
    or Mesh (vertical ray).  opts: { offset (m, default 0), only: 'missing'
    (z NaN or 0) | 'all' | 'not-surveyed' (rows / parts / collars whose
    confidence is not 'surveyed') }.  The original z is kept (a z_original
    column, a per-feature z_original list, a collar z_original) so
    restoreElevation() can put it back.  Returns { moved, outside, skipped }. */
export function setElevationFrom(target, surface, opts = {}) {
  const T = asObject(target), Sf = asObject(surface);
  const off = opts.offset == null ? 0 : +opts.offset, only = opts.only || 'all';
  if (!ELEVATION_SCOPES.includes(only)) throw new Error(`only must be one of ${ELEVATION_SCOPES.join(' | ')}`);
  let zAt;
  if (Sf.kind === 'grid2d') zAt = (x, y) => Sf.sample(x, y);
  else if (Sf.kind === 'mesh') { const ix = meshXYIndex(Sf); zAt = (x, y) => meshZAt(Sf, x, y, ix); }
  else throw new Error(`cannot take elevations from a ${Sf.kind}`);
  const stats = { moved: 0, outside: 0, skipped: 0 };
  const missing = z => z !== z || z === 0;
  const label = Sf.name + (off ? ` (+${off} m)` : '');
  if (T.kind === 'points') {
    const keep = T.attributes.z_original || [], conf = T.attributes.confidence || [];
    for (let i = 0; i < T.n; i++) {
      const z0 = T.xyz[3 * i + 2];
      if ((only === 'missing' && !missing(z0)) || (only === 'not-surveyed' && conf[i] === 'surveyed')) { stats.skipped++; continue; }
      const z = zAt(T.xyz[3 * i], T.xyz[3 * i + 1]);
      if (z !== z) { stats.outside++; continue; }
      if (keep[i] == null) keep[i] = z0;
      T.xyz[3 * i + 2] = z + off; stats.moved++;
    }
    for (let i = 0; i < T.n; i++) if (keep[i] === undefined) keep[i] = null;
    T.attributes.z_original = keep;
  } else if (T.kind === 'lineset') {
    for (let k = 0; k < T.parts.length; k++) {
      const f = T.features[k] || (T.features[k] = {}), idx = T.parts[k];
      if (only === 'not-surveyed' && f.confidence === 'surveyed') { stats.skipped += idx.length; continue; }
      const keep = Array.isArray(f.z_original) ? f.z_original.slice() : new Array(idx.length).fill(null);
      let touched = false;
      idx.forEach((vi, q) => {
        const z0 = T.vertices[3 * vi + 2];
        if (only === 'missing' && !missing(z0)) { stats.skipped++; return; }
        const z = zAt(T.vertices[3 * vi], T.vertices[3 * vi + 1]);
        if (z !== z) { stats.outside++; return; }
        if (keep[q] == null) keep[q] = z0;
        T.vertices[3 * vi + 2] = z + off; stats.moved++; touched = true;
      });
      if (touched) f.z_original = keep;
    }
  } else if (T.kind === 'drillholes') {
    for (const c of T.collars) {
      const z0 = c.z == null || c.z === '' ? NaN : +c.z;
      if ((only === 'missing' && !missing(z0)) || (only === 'not-surveyed' && c.confidence === 'surveyed')) { stats.skipped++; continue; }
      const z = zAt(+c.x, +c.y);
      if (z !== z) { stats.outside++; continue; }
      if (c.z_original == null) c.z_original = z0 === z0 ? z0 : null;
      c.z = z + off; stats.moved++;
    }
    T._traces = null;
  } else throw new Error(`cannot set the elevation of a ${T.kind}`);
  T.metadata.elevation_from = label;
  if (stats.outside) T.warn(`${stats.outside} point(s) fell outside ${Sf.name} and kept their original elevation`);
  return stats;
}
/** Undo setElevationFrom: put the kept original z back and drop the record. */
export function restoreElevation(target) {
  const T = asObject(target);
  let restored = 0;
  if (T.kind === 'points') {
    const keep = T.attributes.z_original;
    if (keep) { for (let i = 0; i < T.n; i++) if (keep[i] != null) { T.xyz[3 * i + 2] = +keep[i]; restored++; } delete T.attributes.z_original; }
  } else if (T.kind === 'lineset') {
    T.parts.forEach((idx, k) => { const f = T.features[k]; if (!f || !Array.isArray(f.z_original)) return; idx.forEach((vi, q) => { if (f.z_original[q] != null) { T.vertices[3 * vi + 2] = +f.z_original[q]; restored++; } }); delete f.z_original; });
  } else if (T.kind === 'drillholes') {
    for (const c of T.collars) if (c.z_original !== undefined) { if (c.z_original != null) { c.z = +c.z_original; restored++; } delete c.z_original; }
    T._traces = null;
  } else throw new Error(`cannot restore the elevation of a ${T.kind}`);
  delete T.metadata.elevation_from;
  return restored;
}

/* ============================================ geometry: clip to ground */
function clipCore(mesh, topo, eps) {
  const m = asObject(mesh), g = asObject(topo);
  let zAt;
  if (g.kind === 'grid2d') zAt = (x, y) => g.sample(x, y);
  else if (g.kind === 'mesh') { const ix = meshXYIndex(g); zAt = (x, y) => meshZAt(g, x, y, ix); }
  else throw new Error(`the ground must be a Grid2D or a Mesh, not a ${g.kind}`);
  const nv = m.nVertices, V = m.vertices, depth = new Float64Array(nv);
  for (let i = 0; i < nv; i++) { const zg = zAt(V[3 * i], V[3 * i + 1]); depth[i] = zg === zg ? V[3 * i + 2] - zg : NaN; }
  const vKeys = [], fKeys = [];
  for (const [k, a] of Object.entries(m.attributes)) (a.location === 'faces' ? fKeys : vKeys).push(k);
  const outV = [], outT = [], outVA = {}, outFA = {}, segments = [];
  for (const k of vKeys) outVA[k] = [];
  for (const k of fKeys) outFA[k] = [];
  const vmap = new Int32Array(nv).fill(-1), edge = new Map();
  const useVertex = i => {
    if (vmap[i] < 0) { vmap[i] = outV.length / 3; outV.push(V[3 * i], V[3 * i + 1], V[3 * i + 2]); for (const k of vKeys) outVA[k].push(m.attributes[k].values[i]); }
    return vmap[i];
  };
  const crossing = (p, q) => {              // where the edge p-q meets the ground (computed low index -> high index so shared edges share the vertex)
    const lo = Math.min(p, q), hi = Math.max(p, q), key = lo + ',' + hi;
    let id = edge.get(key);
    if (id != null) return id;
    const t = depth[lo] / (depth[lo] - depth[hi]);
    id = outV.length / 3;
    for (let a = 0; a < 3; a++) outV.push(V[3 * lo + a] + (V[3 * hi + a] - V[3 * lo + a]) * t);
    for (const k of vKeys) { const va = m.attributes[k].values; outVA[k].push(va[lo] + (va[hi] - va[lo]) * t); }
    edge.set(key, id);
    return id;
  };
  const at = id => [outV[3 * id], outV[3 * id + 1], outV[3 * id + 2]];
  const stats = { kept: 0, dropped: 0, split: 0, unknown_ground: 0 };
  for (let t = 0; t < m.nTriangles; t++) {
    const I = [m.triangles[3 * t], m.triangles[3 * t + 1], m.triangles[3 * t + 2]];
    const D = I.map(i => depth[i]);
    const emitWhole = () => { outT.push(useVertex(I[0]), useVertex(I[1]), useVertex(I[2])); for (const k of fKeys) outFA[k].push(m.attributes[k].values[t]); };
    if (D[0] !== D[0] || D[1] !== D[1] || D[2] !== D[2]) { stats.unknown_ground++; emitWhole(); continue; }
    const below = D.map(d => d <= eps), nb = below.filter(Boolean).length;
    if (nb === 3) { stats.kept++; emitWhole(); continue; }
    if (nb === 0) { stats.dropped++; continue; }
    stats.split++;
    // rotate so vertex 0 is the odd one out (the single below, or the single above)
    let s = 0;
    for (let q = 0; q < 3; q++) if (below[q] === (nb === 1)) { s = q; break; }
    const p = I[s], q = I[(s + 1) % 3], r = I[(s + 2) % 3];
    if (nb === 1) {                           // p below: keep its corner
      const cq = crossing(p, q), cr = crossing(p, r);
      outT.push(useVertex(p), cq, cr);
      for (const k of fKeys) outFA[k].push(m.attributes[k].values[t]);
      segments.push([at(cq), at(cr)]);
    } else {                                  // p above: keep the quad q, r, r-p crossing, p-q crossing
      const cpq = crossing(p, q), crp = crossing(r, p);
      outT.push(useVertex(q), useVertex(r), crp);
      outT.push(useVertex(q), crp, cpq);
      for (const k of fKeys) { outFA[k].push(m.attributes[k].values[t]); outFA[k].push(m.attributes[k].values[t]); }
      segments.push([at(cpq), at(crp)]);
    }
  }
  const attributes = {};
  for (const k of vKeys) attributes[k] = { location: 'vertices', values: Float32Array.from(outVA[k]) };
  for (const k of fKeys) attributes[k] = { location: 'faces', values: Float32Array.from(outFA[k]) };
  return { mesh: m, ground: g, vertices: Float64Array.from(outV), triangles: Uint32Array.from(outT), attributes, segments, stats };
}
/** Keep the part of a mesh that lies below the ground: triangles wholly above
    it are dropped, wholly below kept, mixed ones split where their edges
    cross the ground (linear interpolation).  Returns a new Mesh with the
    same attributes; opts: { eps (m, default 1e-6), name }. */
export function clipMeshToTopography(mesh, topo, opts = {}) {
  const c = clipCore(mesh, topo, opts.eps == null ? 1e-6 : +opts.eps), m = c.mesh;
  const out = new GM.Mesh({ name: opts.name || `${m.name} (below ${c.ground.name})`, color: m.color, role: m.role, opacity: m.opacity, group: m.group, vertices: c.vertices, triangles: c.triangles, attributes: c.attributes, provenance: Object.assign({}, m.provenance), metadata: Object.assign({}, m.metadata) });
  out.metadata.clipped_to = c.ground.name;
  out.metadata.clip = Object.assign({ source_id: m.id, ground_id: c.ground.id }, c.stats);
  return out;
}
/** Where a surface (Mesh or Grid2D) meets the ground: a LineSet (role
    'interpretation') named '<name> daylight (computed)'. */
export function daylightTrace(source, topo, opts = {}) {
  const s = asObject(source), g = asObject(topo);
  let segs;
  if (s.kind === 'mesh') segs = clipCore(s, g, opts.eps == null ? 1e-6 : +opts.eps).segments;
  else if (s.kind === 'grid2d') {
    if (g.kind !== 'grid2d') throw new Error('a grid surface needs a grid topography');
    const at = (i, j) => { const v = s.values[j * s.nx + i]; if (v !== v) return NaN; const [x, y] = s.nodeXY(i, j); const zg = g.sample(x, y); return zg === zg ? v - zg : NaN; };
    const zOn = p => { const z = g.sample(p[0], p[1]); return z === z ? z : s.sample(p[0], p[1]); };
    segs = marchingSquares(s.nx, s.ny, at, (i, j) => s.nodeXY(i, j), 0).map(([p, q]) => [[p[0], p[1], zOn(p)], [q[0], q[1], zOn(q)]]);
  } else throw new Error(`cannot daylight a ${s.kind}`);
  const ls = new GM.LineSet({ name: `${s.name} daylight (computed)`, role: 'interpretation', color: opts.color || [255, 200, 60] });
  for (const chain of chainSegments(segs)) ls.addPolyline(chain, { source: s.name, source_id: s.id, ground: g.name, method: 'surface ∩ topography', confidence: 'inferred' });
  ls.metadata.derived_from = [s.id, g.id];
  ls.metadata.note = 'computed intersection of a modelled surface with the topography — it inherits every error of both';
  ls.provenance = { method: 'surface / topography intersection (daylightTrace)', source_layer: s.name, ground: g.name };
  return ls;
}

/** Map traced pixel coordinates through an ImagePlane's georeference -> [[x, y, z], ...]. */
export function traceToWorld(image, pixelPoints, levelZ = null) {
  image = asObject(image);
  const out = [];
  for (const [px, py] of pixelPoints) {
    let [x, y, z] = image.pixelToWorld(px, py);
    if (image.plane === 'plan') z = levelZ != null ? levelZ : (image.elevation != null ? image.elevation : 0);
    out.push([x, y, z]);
  }
  return out;
}

/** Georeference a plan from one anchor pixel -> world, metres per pixel and an optional rotation (deg clockwise from north). */
export function georefPlanFromScale(image, anchorPx, anchorWorld, scaleMPerPx, opts = {}) {
  image = asObject(image);
  const rotationDeg = opt(opts, 'rotation_deg', 'rotationDeg', 0), elevation = opt(opts, 'elevation', null, null);
  const [px, py] = anchorPx, [X, Y] = anchorWorld;
  const r = -rotationDeg * DEG, c = Math.cos(r), s = Math.sin(r);
  const dx = 100 * scaleMPerPx * c, dy = -100 * scaleMPerPx * s;
  image.control = [[px, py, X, Y], [px + 100, py, X + dx, Y + dy]];
  if (elevation != null) image.elevation = elevation;
  return image;
}

export function sectionImage(imageUri, width, height, p1, p2, zTop, zBottom, opts = {}) {
  return new GM.ImagePlane(Object.assign({ name: 'section' }, opts, { image: imageUri, width, height, plane: 'section', p1, p2, z_top: zTop, z_bottom: zBottom }));
}
export function levelFromSection(image, pxX, pxY) { return asObject(image).pixelToWorld(pxX, pxY); }

/** Lengths by type and by level. */
export function workingsSummary(ws) {
  ws = asObject(ws);
  const byType = {}, byLevel = {};
  ws.features.forEach((f, k) => {
    const L = ws.length(k), ty = f.type || 'unknown';
    byType[ty] = (byType[ty] || 0) + L;
    let lv = f.level;
    if (!lv) {
      if (f.level_z != null) { const r = pyRound(f.level_z, 0); lv = ((r === 0 && (f.level_z < 0 || Object.is(f.level_z, -0))) ? '-0' : String(r)) + ' m'; }
      else lv = 'unassigned';
    }
    byLevel[lv] = (byLevel[lv] || 0) + L;
  });
  return { n_features: ws.parts.length, total_m: ws.length(), by_type: byType, by_level: byLevel };
}

/** 2-D footprint of the workings as GeoJSON (WGS84); toLonLat(x, y) defaults to the UTM inverse of ``crs``. */
export function workingsToGeoJSON(ws, crs, toLonLat = null) {
  ws = asObject(ws);
  if (!toLonLat) {
    if (!crs || crs.zone == null) throw new Error('workingsToGeoJSON: need a UTM crs {zone, north} or a toLonLat converter');
    toLonLat = (x, y) => GM.utm.inv(x, y, crs.zone, crs.north !== false);
  }
  const feats = ws.parts.map((part, k) => {
    const coords = part.map(i => { const [x, y, z] = ws.vertex(i); const [lon, lat] = toLonLat(x, y); return [pyRound(lon, 7), pyRound(lat, 7), pyRound(z, 2)]; });
    const props = Object.assign({}, k < ws.features.length ? ws.features[k] : {});
    props.length_m = pyRound(ws.length(k), 1);
    props.layer = 'workings';
    return { type: 'Feature', properties: props, geometry: { type: 'LineString', coordinates: coords } };
  });
  return { type: 'FeatureCollection', features: feats, properties: { schema: 'nwmm-workings/1', mine: ws.metadata.mine || '' } };
}

/** Surface entries (first vertex of adits / shafts / declines) as a PointSet. */
export function portalsPoints(ws) {
  ws = asObject(ws);
  const ps = new GM.PointSet({ name: 'portals & collars', role: 'points', color: [255, 255, 255] });
  ws.features.forEach((f, k) => {
    if (['adit', 'tunnel', 'shaft', 'decline', 'portal'].includes(f.type)) {
      const [x, y, z] = ws.partXYZ(k)[0];
      ps.add(x, y, z, { type: f.type, name: f.name === undefined ? '' : f.name });
    }
  });
  return ps;
}

/* ================================================================ kit */
/** Sutherland–Hodgman clip of a ring ([[x, y], ...]) to a rectangle. */
export function clipRingRect(ring, minx, miny, maxx, maxy) {
  const clip = (poly, inside, intersect) => {
    const out = [];
    if (!poly.length) return out;
    let prev = poly[poly.length - 1];
    for (const cur of poly) {
      if (inside(cur)) { if (!inside(prev)) out.push(intersect(prev, cur)); out.push(cur); }
      else if (inside(prev)) out.push(intersect(prev, cur));
      prev = cur;
    }
    return out;
  };
  const ix = (a, b, x) => { const t = (x - a[0]) / (b[0] - a[0]); return [x, a[1] + (b[1] - a[1]) * t]; };
  const iy = (a, b, y) => { const t = (y - a[1]) / (b[1] - a[1]); return [a[0] + (b[0] - a[0]) * t, y]; };
  let poly = ring.map(p => [+p[0], +p[1]]);
  if (poly.length && poly[0][0] === poly[poly.length - 1][0] && poly[0][1] === poly[poly.length - 1][1]) poly = poly.slice(0, -1);
  poly = clip(poly, p => p[0] >= minx, (a, b) => ix(a, b, minx));
  poly = clip(poly, p => p[0] <= maxx, (a, b) => ix(a, b, maxx));
  poly = clip(poly, p => p[1] >= miny, (a, b) => iy(a, b, miny));
  poly = clip(poly, p => p[1] <= maxy, (a, b) => iy(a, b, maxy));
  const clean = [];
  for (const p of poly) {
    const l = clean[clean.length - 1];
    if (!l || Math.abs(p[0] - l[0]) > 1e-9 || Math.abs(p[1] - l[1]) > 1e-9) clean.push(p);
  }
  if (clean.length > 1 && Math.abs(clean[0][0] - clean[clean.length - 1][0]) < 1e-9 && Math.abs(clean[0][1] - clean[clean.length - 1][1]) < 1e-9) clean.pop();
  return clean;
}

/** Midpoint-subdivide (x, y) triangles until every edge <= maxEdge -> {points, triangles} (shared midpoints). */
export function subdivideTriangles(verts2d, tris, maxEdge) {
  const pts = verts2d.map(p => [p[0], p[1]]);
  const mid = new Map();
  const midpoint = (a, b) => {
    const key = a < b ? a + ':' + b : b + ':' + a;
    let m = mid.get(key);
    if (m === undefined) { m = pts.length; pts.push([(pts[a][0] + pts[b][0]) / 2, (pts[a][1] + pts[b][1]) / 2]); mid.set(key, m); }
    return m;
  };
  const elen = (a, b) => Math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]);
  const out = [];
  const stack = tris.map(t => [t[0], t[1], t[2]]);
  let guard = 0;
  while (stack.length && guard < 2000000) {
    guard++;
    const [a, b, c] = stack.pop();
    if (Math.max(elen(a, b), elen(b, c), elen(c, a)) <= maxEdge) { out.push([a, b, c]); continue; }
    const ab = midpoint(a, b), bc = midpoint(b, c), ca = midpoint(c, a);
    stack.push([a, ab, ca], [ab, b, bc], [ca, bc, c], [ab, bc, ca]);
  }
  return { points: pts, triangles: out };
}

/** Insert points along a 2-D polyline so no segment exceeds ``step``. */
export function densify(path, step) {
  const out = [];
  for (let k = 0; k < path.length - 1; k++) {
    const x0 = path[k][0], y0 = path[k][1], x1 = path[k + 1][0], y1 = path[k + 1][1];
    const L = Math.hypot(x1 - x0, y1 - y0);
    const n = Math.max(1, Math.ceil(L / step));
    for (let q = 0; q < n; q++) { const t = q / n; out.push([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t]); }
  }
  if (path.length) out.push([path[path.length - 1][0], path[path.length - 1][1]]);
  return out;
}

/** Stable pastel colour from a geology unit's id / name. */
export function unitColor(u) {
  const s = String((u && (u.id || u.nm)) || 'u');
  let h = 0;
  for (const ch of s) h = (h * 31 + ch.codePointAt(0)) & 0xffffff;
  const r = (h >> 16) & 255, g = (h >> 8) & 255, b = h & 255;
  return [128 + (r >> 1), 128 + (g >> 1), 128 + (b >> 1)];
}

/* ================================================= model from the map */
/* Leapfrog's Model From Map, reduced to what a geological map draped on a
   DEM can honestly say (GEOMODEL.md §7):

     unitOrder       the stack from the units' ages (youngest first);
                     unaged units go last and are reported, ties are reported
     sharedContacts  where a unit's draped outline meets an older unit's
                     outline — that is the younger unit's base cropping out
     dipOffsets      one extra point a fixed distance down dip of each
                     contact, from the nearest orientation DERIVED from the
                     traces themselves (gm-structural.js deriveFromTraces)
     buildFromMap    a→c into the unit list buildStratigraphy takes

   Nothing is defaulted: a unit that touches nothing older, or has fewer
   than three contacts, is skipped and named; a map with no derivable dip
   yields heightfields through the contacts, labelled as such; readings
   derived from FAULT traces are never used as bedding dips.  Everything
   produced carries provenance {method: 'model from map', confidence:
   'inferred'}.  Python twin: pipelines/geomodel/mapmodel.py. */

export const MAPMODEL_METHOD = 'model from map';
export const MAPMODEL_DEFAULTS = { tol: null, radius: 300, offset: 100, min_contacts: 3 };
export const NO_DIP_WARNING = 'no dip information — bases follow the mapped contacts at the surface';

const finiteOrNull = v => (v == null || v === '') ? null : (isFinite(+v) ? +v : null);
/** Unit vector down the dip line — the same formula as gm-structural.js
    dipVector (that module imports this one, so it cannot be imported here). */
function dipVec(dip, dipAz) { const d = dip * DEG, a = dipAz * DEG, cd = Math.cos(d); return [Math.sin(a) * cd, Math.cos(a) * cd, -Math.sin(d)]; }
const unitLabel = (u, i) => (u && u.name != null && u.name !== '') ? String(u.name) : (u && u.id != null ? String(u.id) : `unit ${i}`);
function ageKey(u) {   // [t1, t0] — youngest first sorts ascending; a single age stands for both
  const t0 = finiteOrNull(u.t0), t1 = finiteOrNull(u.t1);
  if (t0 == null && t1 == null) return null;
  return [t1 == null ? t0 : t1, t0 == null ? t1 : t0];
}

/** Order units youngest first by t1 (younger Ma) then t0 (older Ma).  Units
    with no ages go last in map order and are reported; units with the same
    ages are reported as ties (their mutual order is not knowledge). */
export function unitOrder(units) {
  const list = Array.isArray(units) ? units : [];
  const aged = [], unaged = [];
  list.forEach((u, i) => { if (ageKey(u)) aged.push(i); else unaged.push(i); });
  aged.sort((a, b) => { const ka = ageKey(list[a]), kb = ageKey(list[b]); return (ka[0] - kb[0]) || (ka[1] - kb[1]) || (a - b); });
  const ties = [];
  for (let p = 0; p < aged.length;) {
    let q = p + 1;
    while (q < aged.length && ageKey(list[aged[q]])[0] === ageKey(list[aged[p]])[0] && ageKey(list[aged[q]])[1] === ageKey(list[aged[p]])[1]) q++;
    if (q - p > 1) ties.push(aged.slice(p, q).map(i => unitLabel(list[i], i)));
    p = q;
  }
  const warnings = [];
  if (unaged.length) warnings.push(`${unaged.length} unit${unaged.length > 1 ? 's' : ''} without an age (${unaged.map(i => unitLabel(list[i], i)).join(', ')}) — placed last, in map order, and not modelled: nothing says where ${unaged.length > 1 ? 'they sit' : 'it sits'} in the sequence`);
  for (const t of ties) warnings.push(`same ages, order between them unknown: ${t.join(' / ')} — neither is treated as older than the other`);
  return { order: aged.concat(unaged), aged, unaged, unaged_names: unaged.map(i => unitLabel(list[i], i)), ties, keys: list.map(u => ageKey(u)), warnings };
}

/** Squared distance from (px, py) to the segment a–b in plan. */
function segDist2(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
  let t = L2 > 0 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  if (t < 0) t = 0; else if (t > 1) t = 1;
  const qx = ax + t * dx - px, qy = ay + t * dy - py;
  return qx * qx + qy * qy;
}
/** Spatial hash of a LineSet's plan segments in cells of ``cell`` metres. */
function segmentHash(ls, cell) {
  const segs = [], cells = new Map();
  for (const part of ls.parts) {
    for (let k = 0; k + 1 < part.length; k++) {
      const a = ls.vertex(part[k]), b = ls.vertex(part[k + 1]);
      if (a[0] !== a[0] || a[1] !== a[1] || b[0] !== b[0] || b[1] !== b[1]) continue;
      const s = segs.length; segs.push([a[0], a[1], b[0], b[1]]);
      const ix0 = Math.floor(Math.min(a[0], b[0]) / cell), ix1 = Math.floor(Math.max(a[0], b[0]) / cell), iy0 = Math.floor(Math.min(a[1], b[1]) / cell), iy1 = Math.floor(Math.max(a[1], b[1]) / cell);
      for (let ix = ix0; ix <= ix1; ix++) for (let iy = iy0; iy <= iy1; iy++) { const key = ix + ',' + iy; let l = cells.get(key); if (!l) { l = []; cells.set(key, l); } l.push(s); }
    }
  }
  return { segs, cells, cell, near(px, py, tol) {
    const ix = Math.floor(px / cell), iy = Math.floor(py / cell), t2 = tol * tol; let best = INF;
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      const l = cells.get((ix + dx) + ',' + (iy + dy)); if (!l) continue;
      for (const s of l) { const g = segs[s]; const d2 = segDist2(px, py, g[0], g[1], g[2], g[3]); if (d2 < best) best = d2; }
    }
    return best <= t2 ? Math.sqrt(best) : null;
  } };
}
/** One topo cell inside the model box: vertices on or beyond it are
    clipping artefacts (clipRingRect), not contacts. */
function innerBox(topo, box) {
  if (box && box.length >= 4) return [+box[0], +box[1], +box[2], +box[3]];
  if (!topo) return null;
  return [topo.x0 + topo.dx, topo.y0 + topo.dy, topo.xmax - topo.dx, topo.ymax - topo.dy];
}

/** The vertices of outline A that lie within ``tol`` of outline B's line
    (distance to B's segments, not just its vertices) — the contact between
    the two units.  Vertices within one topo cell of the model box edge are
    skipped; z is resampled from opts.topo (vertices over no-data are
    skipped and counted).  Returns {points, count, edge_skipped, nodata, tol}. */
export function sharedContacts(outlineA, outlineB, tol, opts = {}) {
  const A = asObject(outlineA), B = asObject(outlineB);
  const topo = opts.topo ? asObject(opts.topo) : null;
  tol = +tol; if (!(tol > 0)) throw new Error('sharedContacts: tol must be > 0');
  if (!A || A.kind !== 'lineset' || !B || B.kind !== 'lineset') throw new Error('sharedContacts: two LineSets are needed');
  const box = innerBox(topo, opts.box);
  const hash = segmentHash(B, tol);
  const against = opts.against != null ? String(opts.against) : (B.name || '');
  const points = new GM.PointSet({ name: opts.name || `${A.name || 'unit'} / ${B.name || 'older unit'} contacts`, role: 'contacts', color: opts.color || A.color });
  const seen = new Set();
  let edge = 0, nodata = 0;
  A.parts.forEach((part, k) => {
    const feat = A.features[k] || {};
    const unit = feat.unit != null ? String(feat.unit) : (feat.unit_id != null ? String(feat.unit_id) : (A.name || ''));
    for (const i of part) {
      const v = A.vertex(i); const x = v[0], y = v[1];
      if (x !== x || y !== y) continue;
      if (box && (x <= box[0] || x >= box[2] || y <= box[1] || y >= box[3])) { edge++; continue; }
      const d = hash.near(x, y, tol); if (d == null) continue;
      const key = Math.floor(x * 1000 + 0.5) + ',' + Math.floor(y * 1000 + 0.5);
      if (seen.has(key)) continue;
      let z = v[2];
      if (topo) { z = topo.sample(x, y); if (z !== z) { nodata++; continue; } }
      seen.add(key);
      points.add(x, y, z, { kind: 'contact', unit, against, part: k, distance_m: pyRound(d, 3) });
    }
  });
  points.metadata.contact = { tol, against, edge_skipped: edge, nodata };
  points.metadata.derived_from = [A.id, B.id].concat(topo ? [topo.id] : []);
  points.provenance = { method: MAPMODEL_METHOD, confidence: 'inferred', inputs: [A.name, B.name].concat(topo ? [topo.name] : []), step: 'shared contacts', tol_m: tol };
  return { points, count: points.n, edge_skipped: edge, nodata, tol };
}

/** Flatten one or several structural PointSets (live or packed) into typed
    columns, dropping rows without a dip / azimuth and rows whose ``source``
    is in opts.exclude_sources (readings derived from fault traces are not
    bedding dips). */
function readingsOf(structural, excludeSources) {
  const layers = (structural == null ? [] : Array.isArray(structural) ? structural : [structural]).map(asObject).filter(o => o && o.kind === 'points' && o.n > 0);
  const ex = new Set((excludeSources || []).map(String));
  const xyz = [], dip = [], az = [], src = [], part = [], layer = [], row = [];
  let excluded = 0;
  for (const ps of layers) {
    const D = ps.numeric('dip'), Az = ps.numeric('dip_azimuth'), S = ps.attributes.source || [], Pt = ps.attributes.part || [];
    for (let i = 0; i < ps.n; i++) {
      const d = D[i], a = Az[i], x = ps.xyz[3 * i], y = ps.xyz[3 * i + 1], z = ps.xyz[3 * i + 2];
      if (d !== d || a !== a || x !== x || y !== y || z !== z) continue;
      if (S[i] != null && ex.has(String(S[i]))) { excluded++; continue; }
      xyz.push(x, y, z); dip.push(d); az.push(a); src.push(S[i] == null ? null : String(S[i])); part.push(Pt[i] == null ? null : Pt[i]); layer.push(ps.name || ps.id); row.push(i);
    }
  }
  return { n: dip.length, xyz: Float64Array.from(xyz), dip, az, src, part, layer, row, layers: layers.map(l => l.id), excluded };
}

/** For each contact point find the nearest structural reading within
    opts.radius (default 300 m) and add one point opts.offset (default
    100 m) down its dip vector, recording which reading it came from.
    Returns {points (the offsets only), used, unmatched, readings, excluded}. */
export function dipOffsets(contactPts, structural, opts = {}) {
  const pts = asObject(contactPts);
  const radius = +opt(opts, 'radius', null, MAPMODEL_DEFAULTS.radius), offset = +opt(opts, 'offset', null, MAPMODEL_DEFAULTS.offset);
  if (!(radius > 0) || !(offset > 0)) throw new Error('dipOffsets: radius and offset must be > 0');
  const R = readingsOf(structural, opts.exclude_sources || opts.excludeSources);
  const out = new GM.PointSet({ name: (pts.name || 'contacts') + ' — dip offsets', role: 'contacts', color: pts.color });
  // cells no smaller than the search radius: a single reading would otherwise give a 2 m cell and a radius search that walks hundreds of rings
  const index = R.n ? new GridIndex(R.xyz, 3, radius) : null;
  let used = 0, unmatched = 0;
  for (let i = 0; i < pts.n; i++) {
    const x = pts.xyz[3 * i], y = pts.xyz[3 * i + 1], z = pts.xyz[3 * i + 2];
    if (x !== x || y !== y || z !== z) { unmatched++; continue; }
    const m = index ? index.nearest(x, y, z, 1, radius) : 0;
    if (!m) { unmatched++; continue; }
    const j = index.resI[0], d = index.resD[0];
    const v = dipVec(R.dip[j], R.az[j]);
    out.add(x + offset * v[0], y + offset * v[1], z + offset * v[2], { kind: 'offset', contact: i, dip: R.dip[j], dip_azimuth: R.az[j], reading_layer: R.layer[j], reading_row: R.row[j], reading_source: R.src[j], reading_part: R.part[j], reading_distance_m: pyRound(d, 2), offset_m: offset });
    used++;
  }
  out.metadata.dip_offsets = { radius_m: radius, offset_m: offset, used, unmatched, readings: R.n, readings_excluded: R.excluded };
  out.metadata.derived_from = [pts.id].concat(R.layers);
  out.provenance = { method: MAPMODEL_METHOD, confidence: 'inferred', inputs: [pts.name].concat(R.layers), step: 'dip offsets', radius_m: radius, offset_m: offset };
  return { points: out, used, unmatched, readings: R.n, excluded: R.excluded };
}

/** Append src's rows to dst, skipping rows whose plan position (to 1 mm)
    is already in ``seen`` — a vertex at a triple junction is one contact,
    not one per older unit. */
function appendPoints(dst, src, seen = null) {
  let added = 0;
  for (let i = 0; i < src.n; i++) {
    const x = src.xyz[3 * i], y = src.xyz[3 * i + 1];
    if (seen) { const key = Math.floor(x * 1000 + 0.5) + ',' + Math.floor(y * 1000 + 0.5); if (seen.has(key)) continue; seen.add(key); }
    const a = {}; for (const [k, col] of Object.entries(src.attributes)) a[k] = col[i];
    dst.add(x, y, src.xyz[3 * i + 2], a); added++;
  }
  return added;
}

/** Orchestrate unitOrder → sharedContacts → dipOffsets into the unit list
    buildStratigraphy takes.  args: {topo, units: [{id, name, t0, t1, color,
    lithology, outline: LineSet}], faults: [LineSet], structural: PointSet |
    [PointSet], opts: {tol, radius, offset, min_contacts}, onProgress}.
    Returns {units, stats, warnings}: each unit has base = a PointSet of its
    contacts + offsets (contact 'deposit'; the oldest unit is basement with
    no base), provenance, derived_from and its own warnings; stats counts
    everything that was used, skipped or refused. */
export function buildFromMap(args = {}) {
  const topo = asObject(args.topo || args.topography);
  if (!topo || topo.kind !== 'grid2d') throw new Error('buildFromMap: a topography grid is required (contacts take their elevation from it)');
  const o = Object.assign({}, MAPMODEL_DEFAULTS, args.opts || {});
  const onProgress = typeof args.onProgress === 'function' ? args.onProgress : null;
  const tol = o.tol > 0 ? +o.tol : Math.max(topo.dx, topo.dy);
  const radius = +o.radius, offset = +o.offset, minContacts = Math.max(1, o.min_contacts | 0);
  const units = (args.units || []).map((u, i) => Object.assign({}, u, { outline: asObject(u.outline), _i: i }));
  for (const u of units) if (!u.outline || u.outline.kind !== 'lineset') throw new Error(`buildFromMap: unit ${unitLabel(u, u._i)} has no outline LineSet`);
  const faults = (args.faults == null ? [] : Array.isArray(args.faults) ? args.faults : [args.faults]).map(asObject).filter(f => f && f.kind === 'lineset');
  const faultNames = faults.map(f => f.name).filter(Boolean);
  const structural = args.structural == null ? [] : (Array.isArray(args.structural) ? args.structural : [args.structural]).map(asObject).filter(s => s && s.kind === 'points');
  const R = readingsOf(structural, faultNames);
  const ord = unitOrder(units);
  const warnings = ord.warnings.slice();
  const stats = {
    ordered: ord.aged.map(i => unitLabel(units[i], i)), unaged: ord.unaged_names, ties: ord.ties,
    contacts_per_unit: {}, offsets_per_unit: {}, dips_used: 0, units_without_dip: [], units_modelled: [], basement: null,
    rejected: { no_age: ord.unaged_names.slice(), no_contacts: [], few_contacts: [], edge_vertices: 0, nodata: 0, readings_excluded: R.excluded },
    readings: R.n, no_dip: false, faults_ignored: faults.length, tol, radius, offset, min_contacts: minContacts,
  };
  if (faults.length) warnings.push(`${faults.length} mapped fault trace${faults.length > 1 ? 's are' : ' is'} not honoured: the bases are continuous across ${faults.length > 1 ? 'them' : 'it'} (fault blocks are not modelled), and ${R.excluded} reading${R.excluded === 1 ? '' : 's'} derived along fault traces ${R.excluded === 1 ? 'was' : 'were'} left out of the dip search`);
  if (!R.n) { stats.no_dip = true; warnings.push(NO_DIP_WARNING); }
  const out = [];
  const aged = ord.aged;
  aged.forEach((ui, p) => {
    const u = units[ui], name = unitLabel(u, ui), key = ord.keys[ui];
    const base = { id: u.id != null ? u.id : null, name, color: u.color || DEFAULT_COLORS[p % DEFAULT_COLORS.length], lithology: u.lithology || '', description: u.description || '', t0: finiteOrNull(u.t0), t1: finiteOrNull(u.t1), contact: 'deposit', base: null, n_contacts: 0, n_offsets: 0, against: [], warnings: [], derived_from: [u.outline.id, topo.id], provenance: { method: MAPMODEL_METHOD, confidence: 'inferred', inputs: [u.outline.name || name, topo.name || 'topography'], tol_m: tol, radius_m: radius, offset_m: offset } };
    if (p === aged.length - 1) {
      base.provenance.role = 'basement'; base.provenance.note = 'the oldest aged unit: no base is modelled';
      stats.basement = name; out.push(base);
      if (onProgress) onProgress((p + 1) / aged.length);
      return;
    }
    const older = aged.slice(p + 1).filter(oj => { const k2 = ord.keys[oj]; return !(k2[0] === key[0] && k2[1] === key[1]); });
    const contacts = new GM.PointSet({ name: `${name} — map contacts`, role: 'contacts', color: base.color });
    const seen = new Set();
    for (const oj of older) {
      const ou = units[oj];
      const r = sharedContacts(u.outline, ou.outline, tol, { topo, against: unitLabel(ou, oj), name: contacts.name });
      stats.rejected.edge_vertices += r.edge_skipped; stats.rejected.nodata += r.nodata;
      if (r.count && appendPoints(contacts, r.points, seen)) { base.against.push(unitLabel(ou, oj)); base.derived_from.push(ou.outline.id); base.provenance.inputs.push(ou.outline.name || unitLabel(ou, oj)); }
    }
    stats.contacts_per_unit[name] = contacts.n;
    if (!contacts.n) {
      stats.rejected.no_contacts.push(name);
      warnings.push(`${name}: its outline touches no older unit inside the model (${older.length ? older.map(oj => unitLabel(units[oj], oj)).join(', ') : 'no older unit with an age'}) — skipped; nothing on the map says where its base is`);
      if (onProgress) onProgress((p + 1) / aged.length);
      return;
    }
    if (contacts.n < minContacts) {
      stats.rejected.few_contacts.push(name);
      warnings.push(`${name}: only ${contacts.n} contact point${contacts.n === 1 ? '' : 's'} with older units (${base.against.join(', ')}) — fewer than ${minContacts}, too little to fit a surface through; skipped`);
      if (onProgress) onProgress((p + 1) / aged.length);
      return;
    }
    const d = dipOffsets(contacts, structural, { radius, offset, exclude_sources: faultNames });
    stats.offsets_per_unit[name] = d.used; stats.dips_used += d.used;
    const merged = new GM.PointSet({ name: `${name} — map contacts + dip offsets`, role: 'contacts', color: base.color });
    appendPoints(merged, contacts); appendPoints(merged, d.points);
    merged.metadata.map_model = { unit: name, against: base.against.slice(), contacts: contacts.n, offsets: d.used, unmatched: d.unmatched, tol_m: tol, radius_m: radius, offset_m: offset };
    merged.metadata.derived_from = base.derived_from.concat(R.layers);
    merged.provenance = Object.assign({}, base.provenance, { inputs: base.provenance.inputs.concat(structural.map(s => s.name || s.id)), step: 'contacts + dip offsets' });
    base.base = merged; base.n_contacts = contacts.n; base.n_offsets = d.used; base.derived_from = merged.metadata.derived_from.slice(); base.provenance = merged.provenance;
    if (!d.used) {
      stats.units_without_dip.push(name);
      base.warnings.push(R.n ? `no orientation within ${radius} m of its contacts — the base is a heightfield through the contacts only, with no dip away from the line` : NO_DIP_WARNING);
    }
    stats.units_modelled.push(name);
    out.push(base);
    if (onProgress) onProgress((p + 1) / aged.length);
  });
  if (stats.no_dip) for (const u of out) if (u.base && !u.warnings.includes(NO_DIP_WARNING)) u.warnings.push(NO_DIP_WARNING);
  if (!aged.length) warnings.push('no unit carries an age — nothing can be ordered, nothing is modelled');
  else if (out.length === 1) warnings.push(`only ${stats.basement} can be placed: no younger unit has enough contacts with an older one — no base surface to build`);
  return { units: out, stats, warnings };
}

/* ======================================================= worker ops */
function undef(o) { const out = {}; for (const [k, v] of Object.entries(o)) if (v !== undefined) out[k] = v; return out; }

/** Op table shared by gm-worker.js and the main-thread fallback of EngineClient.
    Every op takes (args, onProgress) with live gm-core objects and returns live objects. */
export const OPS = {
  ping: () => 'pong',
  gridFromPoints: (a, p) => gridFromPoints(a.points, a.values, Object.assign({}, a.params || {}, undef({ method: a.method, spec: a.spec, cell: a.cell, n: a.n, name: a.name }), { onProgress: p })),
  buildStratigraphy: (a, p) => buildStratigraphy(a.topo || a.topography, a.units || [], Object.assign({}, a.params || {}, undef({ method: a.method, lattice: a.lattice, name: a.name }), { onProgress: p })),
  stratigraphyVolumes: a => stratigraphyVolumes(a.strat, a.grids || a.bases, a.topo),
  stratigraphySection: a => stratigraphySection(a.strat, a.grids || a.bases, a.topo, a.start, a.end, a.n == null ? 200 : a.n),
  columnAt: a => columnAt(a.strat, a.grids || a.bases, a.x, a.y, a.topo),
  unitAt: a => unitAt(a.strat, a.grids || a.bases, a.x, a.y, a.z, a.topo),
  thicknessGrid: a => thicknessGrid(a.top, a.base, a.name),
  tagBlockModel: (a, p) => tagBlockModel(a.bm, a.strat, a.grids || a.bases, a.topo, a.attribute || 'unit', p),
  empiricalVariogram: a => empiricalVariogram(a.points, a.values, a),
  fitVariogram: a => fitVariogram(a.experimental, a).toJSON(),
  estimate: (a, p) => estimate(a.bm, a.samples, a.value, Object.assign({}, a, { onProgress: p })),
  gradeTonnage: a => gradeTonnage(a.bm, a.attribute, a.cutoffs, a),
  createBlockModel: a => createBlockModel(a.bounds, a.block_size || a.blockSize, a),
  blockmodelToPoints: a => blockmodelToPoints(a.bm, a.attribute),
  composite: a => composite(a.points, a.value, a),
  idw: (a, p) => idw(a.points, a.values, a.targets, Object.assign({}, a, { onProgress: p })),
  nearestNeighbour: a => nearestNeighbour(a.points, a.values, a.targets, a),
  ordinaryKriging: (a, p) => ordinaryKriging(a.points, a.values, a.targets, a.variogram, Object.assign({}, a, { onProgress: p })),
  rbfFit: a => { const { points, values, ...rest } = a; return new RBF(rest).fit(points, values).toJSON(); },
  rbfPredict: (a, p) => RBF.fromJSON(a.rbf).predict(a.targets, p),
  isosurface: (a, p) => isosurface(a.field, a.count, a.origin, a.spacing, { iso: a.iso, name: a.name, color: a.color, onProgress: p }),
  implicitSurface: (a, p) => {
    const { points, values, bounds, spacing, iso = 0, name, color, params = {}, kernel = 'thin_plate', drift = 'linear', smoothing = 0, epsilon = null, anisotropy = null } = a;
    const rbf = new RBF(Object.assign({ kernel, drift, smoothing, epsilon, dim: 3, anisotropy }, params));
    rbf.fit(points, values);
    p(0.05, 'rbf fitted');
    let bnd = bounds;
    const xyz = rbf.centers;
    if (!bnd) {
      const mn = [INF, INF, INF], mx = [-INF, -INF, -INF];
      for (let i = 0; i < xyz.length; i += 3) for (let q = 0; q < 3; q++) { if (xyz[i + q] < mn[q]) mn[q] = xyz[i + q]; if (xyz[i + q] > mx[q]) mx[q] = xyz[i + q]; }
      const pad = Math.max(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2], 1) * 0.1;
      bnd = [mn[0] - pad, mn[1] - pad, mn[2] - pad, mx[0] + pad, mx[1] + pad, mx[2] + pad];
    }
    const sp = spacing || Math.max(bnd[3] - bnd[0], bnd[4] - bnd[1], bnd[5] - bnd[2]) / 50;
    const sf = scalarFieldFromRBF(rbf, bnd, sp, f => p(0.05 + 0.75 * f, 'evaluating field'));
    const mesh = isosurface(sf.field, sf.count, sf.origin, sf.spacing, { iso, name: name || 'implicit surface', color, onProgress: f => p(0.8 + 0.2 * f, 'iso-surface') });
    mesh.metadata.implicit = { kernel, drift, smoothing, n_points: rbf.n, bounds: bnd, spacing: sf.spacing, count: sf.count, iso };
    return mesh;
  },
  scalarFieldFromRBF: (a, p) => scalarFieldFromRBF(a.rbf, a.bounds, a.spacing, p),
  meshPlaneIntersection: a => meshPlaneIntersection(a.mesh, a.point, a.normal, a.name),
  gridProfile: a => gridProfile(a.grid, a.start, a.end, a.n == null ? 200 : a.n),
  profileLineSet: a => profileLineSet(a.grid, a.start, a.end, a),
  blockmodelPlaneSample: a => blockmodelPlaneSample(a.bm, a.attribute, a.point, a.normal, a),
  linesetNearPlane: a => linesetNearPlane(a.lineset || a.ls, a.point, a.normal, opt(a, 'half_width', 'halfWidth', 10), a),
  desurvey: a => asObject(a.drillholes).desurvey(a.step),
  workingsToGeoJSON: a => workingsToGeoJSON(a.workings || a.ws, a.crs),
  workingsSummary: a => workingsSummary(a.workings || a.ws),
  portalsPoints: a => portalsPoints(a.workings || a.ws),
  // geometry: projection / contours / elevation / ground clip
  extrudePolyline: a => extrudePolyline(a.xyz || a.points || a.polyline, a),
  gridContours: a => contourGrid(a.grid, a.levels == null ? null : a.levels, a),
  contourLevels: a => contourLevels(a.grid, a.interval, a.base),
  setElevationFrom: a => { const t = asObject(a.target); const stats = setElevationFrom(t, a.surface || a.grid || a.topo, a); return { target: t, stats }; },
  restoreElevation: a => { const t = asObject(a.target); const restored = restoreElevation(t); return { target: t, restored }; },
  clipMeshToTopography: a => clipMeshToTopography(a.mesh, a.topo || a.topography || a.ground, a),
  daylightTrace: a => daylightTrace(a.source || a.mesh || a.grid, a.topo || a.topography || a.ground, a),
  // model from the map: a→d of GEOMODEL.md §7 into buildStratigraphy's unit list
  mapModelInputs: (a, p) => buildFromMap(Object.assign({}, a, { onProgress: p })),
};

/** Run one op on live objects (used by the worker and by the main-thread fallback). */
export function runOp(op, args, onProgress) {
  const fn = OPS[op];
  if (!fn) throw new Error(`unknown op ${op}`);
  return fn(args || {}, typeof onProgress === 'function' ? onProgress : () => {});
}

/* ---------------------------------------------------- transport pack */
const PACK_DEPTH = 6;
/** GM.packObject, but keeping the in-memory Float64Arrays of property grids and
    block-model attributes (toJSON downcasts them to f32 for the JSON file format —
    fine on disk, lossy for an in-memory hop). */
function packModel(v) {
  const d = GM.packObject(v);
  if (v instanceof GM.Grid2D) d.values = v.values;
  else if (v instanceof GM.BlockModel) for (const [k, a] of Object.entries(v.attributes)) if (a.type === 'number' && d.attributes[k]) d.attributes[k].values = a.values;
  return d;
}
function unpackModel(d) {
  if (d.kind === 'blockmodel' && d.attributes && Object.values(d.attributes).every(a => a.type !== 'number' || ArrayBuffer.isView(a.values))) return new GM.BlockModel(d);   // keeps f64
  return GM.unpackObject(d);
}
/** gm-core objects -> plain packed objects (raw typed arrays), recursively through arrays / objects / Maps. */
export function packValue(v, depth = 0) {
  if (v == null || typeof v !== 'object') return v;
  if (isModelObject(v)) return packModel(v);
  if (v instanceof Variogram || v instanceof RBF || v instanceof Anisotropy) return v.toJSON();
  if (ArrayBuffer.isView(v) || v instanceof ArrayBuffer) return v;
  if (depth >= PACK_DEPTH) return v;
  if (Array.isArray(v)) return v.map(x => packValue(x, depth + 1));
  if (v instanceof Map) { const m = new Map(); for (const [k, x] of v) m.set(k, packValue(x, depth + 1)); return m; }
  if (Object.getPrototypeOf(v) !== Object.prototype && Object.getPrototypeOf(v) !== null) return v;   // leave other class instances alone
  const out = {};
  for (const [k, x] of Object.entries(v)) out[k] = packValue(x, depth + 1);
  return out;
}
/** Inverse of packValue: anything with a known ``kind`` becomes a gm-core object. */
export function unpackValue(v, depth = 0) {
  if (v == null || typeof v !== 'object') return v;
  if (ArrayBuffer.isView(v) || v instanceof ArrayBuffer || isModelObject(v)) return v;
  if (typeof v.kind === 'string' && GM.KINDS[v.kind]) return unpackModel(v);
  if (depth >= PACK_DEPTH) return v;
  if (Array.isArray(v)) return v.map(x => unpackValue(x, depth + 1));
  if (v instanceof Map) { const m = new Map(); for (const [k, x] of v) m.set(k, unpackValue(x, depth + 1)); return m; }
  if (Object.getPrototypeOf(v) !== Object.prototype && Object.getPrototypeOf(v) !== null) return v;
  const out = {};
  for (const [k, x] of Object.entries(v)) out[k] = unpackValue(x, depth + 1);
  return out;
}
/** ArrayBuffers (deduplicated) of the large typed arrays inside a packed value, for postMessage transfer lists. */
export function collectTransferables(v, minBytes = 16384) {
  const set = new Set();
  const walk = (x, depth) => {
    if (x == null || typeof x !== 'object' || depth > PACK_DEPTH + 2) return;
    if (ArrayBuffer.isView(x)) { if (x.byteLength >= minBytes && x.buffer instanceof ArrayBuffer) set.add(x.buffer); return; }
    if (x instanceof ArrayBuffer) { if (x.byteLength >= minBytes) set.add(x); return; }
    if (Array.isArray(x)) { for (const y of x) walk(y, depth + 1); return; }
    if (x instanceof Map) { for (const y of x.values()) walk(y, depth + 1); return; }
    for (const y of Object.values(x)) walk(y, depth + 1);
  };
  walk(v, 0);
  return [...set];
}

/* ======================================================= EngineClient */
/** Promise client for gm-worker.js.  new EngineClient(workerUrl | Worker | null);
    call(op, args, onProgress) -> Promise<result with live gm-core objects>;
    terminate().  Without Worker support (or with null) the ops run on the calling thread. */
export class EngineClient {
  constructor(workerUrl = null, opts = {}) {
    this._next = 1;
    this._pending = new Map();
    this.worker = null;
    this._everOk = false;
    this._ready = Promise.resolve();
    this.forceLocal = !!opts.forceLocal;
    if (workerUrl && !this.forceLocal) {
      if (typeof workerUrl === 'object' && typeof workerUrl.postMessage === 'function') this._attach(workerUrl);
      else if (typeof globalThis.Worker === 'function') {
        try { this._attach(new globalThis.Worker(workerUrl, { type: 'module', name: opts.name || 'gm-engine' })); }
        catch (e) { this.worker = null; }
      } else if (typeof process !== 'undefined' && process.versions && process.versions.node) {
        this._ready = import('node:worker_threads').then(wt => { this._attach(new wt.Worker(workerUrl)); }).catch(() => { this.worker = null; });
      }
    }
  }
  get usingWorker() { return !!this.worker; }
  _attach(w) {
    this.worker = w;
    const onMsg = m => this._onMessage(m);
    const onErr = e => this._onError(e);
    if (typeof w.on === 'function') { w.on('message', onMsg); w.on('error', onErr); }
    else { w.onmessage = e => onMsg(e.data); w.onerror = onErr; w.onmessageerror = onErr; }
  }
  _onMessage(m) {
    if (!m || m.id == null) return;
    const p = this._pending.get(m.id);
    if (!p) return;
    if (m.progress !== undefined && m.ok === undefined) { if (p.onProgress) { try { p.onProgress(m.progress, m.note); } catch (e) { /* ignore */ } } return; }
    this._pending.delete(m.id);
    if (m.ok) { this._everOk = true; p.resolve(unpackValue(m.result)); }
    else { const err = new Error(m.error || 'engine error'); if (m.stack) err.workerStack = m.stack; p.reject(err); }
  }
  _onError(e) {
    const err = e instanceof Error ? e : new Error((e && e.message) || 'worker error');
    const pending = [...this._pending.values()];
    this._pending.clear();
    if (!this._everOk) {                         // the worker never worked (e.g. failed to load): run locally
      try { this.worker && this.worker.terminate && this.worker.terminate(); } catch (x) { /* ignore */ }
      this.worker = null;
      for (const p of pending) this._local(p.op, p.args, p.onProgress).then(p.resolve, p.reject);
    } else for (const p of pending) p.reject(err);
  }
  async _local(op, args, onProgress) {
    await Promise.resolve();
    return runOp(op, args, onProgress);
  }
  async call(op, args = {}, onProgress = null) {
    await this._ready;
    if (!this.worker) return this._local(op, args, onProgress);
    const id = this._next++;
    return new Promise((resolve, reject) => {
      this._pending.set(id, { resolve, reject, onProgress, op, args });
      try { this.worker.postMessage({ id, op, args: packValue(args) }); }
      catch (e) { this._pending.delete(id); reject(e); }
    });
  }
  terminate() {
    const pending = [...this._pending.values()];
    this._pending.clear();
    for (const p of pending) p.reject(new Error('engine terminated'));
    if (this.worker) { try { this.worker.terminate(); } catch (e) { /* ignore */ } }
    this.worker = null;
  }
}

export { GM };
