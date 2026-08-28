/* gm-structural.js — structural geology engine for model3d.html.
   Pure numerics + object helpers; no DOM.  Everything here works from
   surface-mapped data, because the districts this app models rarely have
   drilling:

     * planar structural measurements as a first-class PointSet contract
       (dip, dip_azimuth, polarity + free columns)
     * derivation of dip/dip-azimuth from a mapped contact or fault trace
       draped on terrain (least-squares plane through the 3-D trace: the
       three-point problem, with relief / spread / RMS gates so a flat-ground
       trace never produces a confident-looking number)
     * declustering (spatial radius, angular tolerance, priority weighting)
     * stereonet projection, Kamb / exponential-Kamb / Schmidt contouring,
       Bingham and Fisher statistics
     * a gradient-only ("form") interpolant after Lajaunie et al. 1997:
       an RBF whose GRADIENT is constrained by the poles, so its level sets
       are everywhere tangent to the measured planes
     * a structural trend field: local anisotropy ellipsoids that follow the
       input geometry and decay by halving every `range`

   Conventions (Leapfrog's, and stated in the course guides):
     dip          0..90    degrees below horizontal
     dip azimuth  0..360   degrees clockwise from north, in the DOWN-dip direction
     polarity     +1 right way up, -1 overturned  (0/'1'/'0' accepted on read)
     pole         unit normal, +Z up for polarity +1
   Coordinates are (E, N, elevation) in project units.                        */

import * as GM from './gm-core.js';
import * as E from './gm-engine.js';

export const DEG = Math.PI / 180, RAD = 180 / Math.PI;
const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
const norm360 = a => ((a % 360) + 360) % 360;

/* ===================================================== conventions ===== */

/** Unit normal (pole) of a plane, pointing up for polarity +1. */
export function poleFromDipAz(dip, dipAz, polarity = 1) {
  const d = dip * DEG, a = dipAz * DEG, sd = Math.sin(d), s = polarity < 0 ? -1 : 1;
  return [Math.sin(a) * sd * s, Math.cos(a) * sd * s, Math.cos(d) * s];
}
/** Inverse: a unit vector -> {dip, dip_azimuth} of the plane it is normal to.
    The vector is flipped to point up first, so the answer is always 0..90. */
export function dipAzFromPole(n) {
  let [x, y, z] = n; const L = Math.hypot(x, y, z) || 1; x /= L; y /= L; z /= L;
  if (z < 0) { x = -x; y = -y; z = -z; }
  const dip = Math.acos(clamp(z, -1, 1)) * RAD;
  const dipAz = (Math.abs(x) < 1e-12 && Math.abs(y) < 1e-12) ? 0 : norm360(Math.atan2(x, y) * RAD);
  return { dip, dip_azimuth: dipAz };
}
/** Unit vector down the dip line. */
export function dipVector(dip, dipAz) {
  const d = dip * DEG, a = dipAz * DEG, cd = Math.cos(d);
  return [Math.sin(a) * cd, Math.cos(a) * cd, -Math.sin(d)];
}
/** Unit vector along strike (dip azimuth - 90). */
export function strikeVector(dipAz) { const a = dipAz * DEG; return [-Math.cos(a), Math.sin(a), 0]; }
/** Unit vector of a line given trend + plunge (plunge positive down). */
export function lineVector(trend, plunge) {
  const t = trend * DEG, p = plunge * DEG, cp = Math.cos(p);
  return [Math.sin(t) * cp, Math.cos(t) * cp, -Math.sin(p)];
}
/** Inverse of lineVector; the vector is flipped downward first. */
export function trendPlunge(v) {
  let [x, y, z] = v; const L = Math.hypot(x, y, z) || 1; x /= L; y /= L; z /= L;
  if (z > 0) { x = -x; y = -y; z = -z; }
  return { trend: norm360(Math.atan2(x, y) * RAD), plunge: Math.asin(clamp(-z, -1, 1)) * RAD };
}
/** Acute angle in degrees between two axes (sign-insensitive). */
export function axialAngle(a, b) {
  const d = Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2]);
  return Math.acos(clamp(d, -1, 1)) * RAD;
}
export const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
export const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export function unit(v) { const L = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / L, v[1] / L, v[2] / L]; }

/* ============================================ the structural contract == */

export const DIP_SYN = ['dip', 'dip_deg', 'dipangle', 'dip_angle'];
export const DIPAZ_SYN = ['dip_azimuth', 'dipazimuth', 'dipaz', 'dip_dir', 'dipdirection', 'dip_direction', 'azimuth', 'azi', 'dd'];
export const STRIKE_SYN = ['strike', 'strike_deg', 'strike_azimuth'];
export const POL_SYN = ['polarity', 'pol', 'younging', 'facing'];

const findCol = (ps, syn) => Object.keys(ps.attributes).find(k => syn.includes(k.toLowerCase().replace(/[\s-]+/g, '_')));

/** Coerce any PointSet that carries orientation columns into the canonical
    `dip` / `dip_azimuth` / `polarity` contract and mark it role='structural'.
    Accepts strike (right-hand rule: dip azimuth = strike + 90).  Returns the
    same object; records what it renamed in metadata. */
export function normaliseStructural(ps) {
  const notes = [];
  const dipCol = findCol(ps, DIP_SYN);
  let azCol = findCol(ps, DIPAZ_SYN);
  const strCol = findCol(ps, STRIKE_SYN);
  const polCol = findCol(ps, POL_SYN);
  if (!dipCol) throw new Error('no dip column (looked for ' + DIP_SYN.join(', ') + ')');
  if (dipCol !== 'dip') { ps.attributes.dip = ps.attributes[dipCol]; delete ps.attributes[dipCol]; notes.push(`${dipCol} -> dip`); }
  if (!azCol && strCol) {
    ps.attributes.dip_azimuth = ps.attributes[strCol].map(v => v == null || v === '' ? null : norm360(+v + 90));
    notes.push(`${strCol} + 90 -> dip_azimuth (right-hand rule)`);
  } else if (azCol && azCol !== 'dip_azimuth') { ps.attributes.dip_azimuth = ps.attributes[azCol]; delete ps.attributes[azCol]; notes.push(`${azCol} -> dip_azimuth`); }
  if (!ps.attributes.dip_azimuth) throw new Error('no dip azimuth or strike column');
  if (polCol && polCol !== 'polarity') { ps.attributes.polarity = ps.attributes[polCol]; delete ps.attributes[polCol]; notes.push(`${polCol} -> polarity`); }
  const n = ps.n;
  // normalise the values themselves
  const dips = ps.attributes.dip, azs = ps.attributes.dip_azimuth, pols = ps.attributes.polarity || [];
  let flipped = 0, outOfRange = 0;
  for (let i = 0; i < n; i++) {
    let d = +dips[i], a = +azs[i];
    if (d !== d || a !== a) continue;
    if (d < 0) { d = -d; a = a + 180; outOfRange++; }
    if (d > 90) { d = 180 - d; a = a + 180; flipped++; }
    dips[i] = clamp(d, 0, 90); azs[i] = norm360(a);
  }
  ps.attributes.polarity = Array.from({ length: n }, (_, i) => {
    const v = pols[i];
    if (v == null || v === '') return 1;
    const s = String(v).trim().toLowerCase();
    if (s === '0' || s === 'overturned' || s === 'inverted' || s === 'down' || s === 'false' || s === '-1') return -1;
    const f = +v; return f < 0 ? -1 : 1;
  });
  ps.role = 'structural';
  if (!ps.group) ps.group = 'Structure';
  if (notes.length) ps.metadata.column_mapping = notes.join('; ');
  if (flipped || outOfRange) ps.warn(`${flipped + outOfRange} measurement(s) had a dip outside 0-90 and were folded into range (azimuth rotated 180)`);
  return ps;
}

export function newStructural(name = 'Structural data', opts = {}) {
  const ps = new GM.PointSet(Object.assign({ name, role: 'structural', group: 'Structure', color: [104, 176, 255] }, opts));
  ps.attributes.dip = []; ps.attributes.dip_azimuth = []; ps.attributes.polarity = [];
  ps.metadata.schema = 'nwmm-structural/1';
  return ps;
}

export function addMeasurement(ps, x, y, z, dip, dipAz, attrs = {}) {
  return ps.add(x, y, z, Object.assign({ dip: clamp(+dip, 0, 90), dip_azimuth: norm360(+dipAz), polarity: attrs.polarity == null ? 1 : attrs.polarity }, attrs));
}

/** Pull a structural PointSet into typed arrays.  `opts.filter` is an optional
    predicate(i) — used for query filters and category selections. */
export function readStructural(ps, opts = {}) {
  const n = ps.n, dips = ps.attributes.dip || [], azs = ps.attributes.dip_azimuth || [], pols = ps.attributes.polarity || [];
  const idx = [], X = [], Y = [], Z = [], D = [], A = [], P = [];
  for (let i = 0; i < n; i++) {
    if (opts.filter && !opts.filter(i)) continue;
    const d = +dips[i], a = +azs[i];
    if (d !== d || a !== a) continue;
    idx.push(i); X.push(ps.xyz[3 * i]); Y.push(ps.xyz[3 * i + 1]); Z.push(ps.xyz[3 * i + 2]);
    D.push(d); A.push(a); P.push(pols[i] == null ? 1 : (+pols[i] < 0 ? -1 : 1));
  }
  const m = idx.length, poles = new Float64Array(m * 3);
  for (let k = 0; k < m; k++) { const p = poleFromDipAz(D[k], A[k], P[k]); poles[3 * k] = p[0]; poles[3 * k + 1] = p[1]; poles[3 * k + 2] = p[2]; }
  return { n: m, index: idx, x: X, y: Y, z: Z, dip: D, dipaz: A, polarity: P, poles };
}

export function isStructural(o) { return o && o.kind === 'points' && (o.role === 'structural' || (o.attributes && o.attributes.dip && o.attributes.dip_azimuth)); }

/* ================================== derive orientation from a trace ==== */

/** Least-squares plane through a set of 3-D points.
    Returns {normal, centroid, rms, l1, l2, l3} with l1>=l2>=l3 = sqrt of the
    covariance eigenvalues (i.e. extents in metres along the principal axes). */
export function fitPlane(pts) {
  const n = pts.length;
  if (n < 3) return null;
  let cx = 0, cy = 0, cz = 0;
  for (const p of pts) { cx += p[0]; cy += p[1]; cz += p[2]; }
  cx /= n; cy /= n; cz /= n;
  let xx = 0, xy = 0, xz = 0, yy = 0, yz = 0, zz = 0;
  for (const p of pts) { const dx = p[0] - cx, dy = p[1] - cy, dz = p[2] - cz; xx += dx * dx; xy += dx * dy; xz += dx * dz; yy += dy * dy; yz += dy * dz; zz += dz * dz; }
  const C = [[xx / n, xy / n, xz / n], [xy / n, yy / n, yz / n], [xz / n, yz / n, zz / n]];
  const { values, vectors } = jacobiEigen3(C);            // descending
  const normal = vectors[2];
  let ss = 0; for (const p of pts) { const d = (p[0] - cx) * normal[0] + (p[1] - cy) * normal[1] + (p[2] - cz) * normal[2]; ss += d * d; }
  // plan-view spread: a trace that is straight in MAP VIEW leaves the plane
  // free to rotate about that line, however much elevation it gains, and the
  // least-squares answer in that case is a meaningless near-vertical plane
  const P2 = jacobiEigen3([[xx / n, xy / n, 0], [xy / n, yy / n, 0], [0, 0, 0]]);
  const planMinor = Math.sqrt(Math.max(P2.values[1], 0));
  return { normal: unit(normal), centroid: [cx, cy, cz], rms: Math.sqrt(ss / n), l1: Math.sqrt(Math.max(values[0], 0)), l2: Math.sqrt(Math.max(values[1], 0)), l3: Math.sqrt(Math.max(values[2], 0)), plan_minor: planMinor };
}

export const DERIVE_DEFAULTS = { window: 500, step: 250, max_window: 2500, min_relief: 20, min_spread: 25, max_rms: 15, min_points: 6, confidence: 'inferred' };

/** Derive planar structural measurements from a LineSet whose vertices are
    draped on terrain — a mapped geological contact or fault trace.
    A sliding window along each part is fitted with a plane; windows that lack
    relief, that are straight in MAP VIEW (which leaves the plane free to
    rotate about the trace, so the least-squares answer is a meaningless
    near-vertical plane), or that fit badly are rejected and counted, never
    guessed.  This is the three-point problem run continuously along the trace.
    A consequence worth stating: a genuinely vertical structure also traces a
    straight line in plan, so it is reported as indeterminate rather than as
    90 degrees — the trace cannot tell the two apart. */
export function deriveFromTraces(lineset, opts = {}) {
  const o = Object.assign({}, DERIVE_DEFAULTS, opts);
  const out = newStructural(o.name || (lineset.name + ' — derived structure'));
  out.color = [140, 190, 120];
  const stats = { parts: 0, windows: 0, kept: 0, grown: 0, no_relief: 0, no_spread: 0, bad_fit: 0, short: 0 };
  for (let k = 0; k < lineset.parts.length; k++) {
    const idxs = lineset.parts[k]; if (!idxs || idxs.length < o.min_points) { stats.short++; continue; }
    stats.parts++;
    const P = idxs.map(i => [lineset.vertices[3 * i], lineset.vertices[3 * i + 1], lineset.vertices[3 * i + 2]]);
    // cumulative plan distance so the window is a real ground distance
    const cum = [0]; for (let i = 1; i < P.length; i++) cum.push(cum[i - 1] + Math.hypot(P[i][0] - P[i - 1][0], P[i][1] - P[i - 1][1]));
    const total = cum[cum.length - 1]; if (total < o.window * 0.5) { stats.short++; continue; }
    const feat = lineset.features[k] || {};
    const maxWin = Math.max(o.window, o.max_window || o.window);
    for (let s = 0; s + o.window <= total + 1e-6; s += o.step) {
      stats.windows++;
      // grow the window until the trace has enough relief and enough spread to
      // determine a plane; a smooth trace on gentle ground needs a longer look
      let best = null, why = 'short';
      for (let w = o.window; w <= maxWin; w += o.window * 0.5) {
        const lo = s, hi = s + w;
        if (hi > total + 1e-6 && w > o.window) break;
        const win = []; for (let i = 0; i < P.length; i++) if (cum[i] >= lo && cum[i] <= Math.min(hi, total)) win.push(P[i]);
        if (win.length < o.min_points) { why = 'short'; continue; }
        let zmin = Infinity, zmax = -Infinity; for (const q of win) { if (q[2] < zmin) zmin = q[2]; if (q[2] > zmax) zmax = q[2]; }
        const relief = zmax - zmin;
        const fit = fitPlane(win);
        if (!fit) { why = 'bad_fit'; continue; }
        best = { win, fit, relief, w };
        if (!(relief >= o.min_relief)) { why = 'no_relief'; continue; }
        if (!(fit.plan_minor >= o.min_spread)) { why = 'no_spread'; continue; }
        if (!(fit.rms <= o.max_rms)) { why = 'bad_fit'; continue; }
        why = null; break;
      }
      if (why || !best) { stats[why === 'short' ? 'short' : why === 'no_relief' ? 'no_relief' : why === 'no_spread' ? 'no_spread' : 'bad_fit']++; continue; }
      const { win, fit, relief, w } = best;
      if (w > o.window) stats.grown++;
      const { dip, dip_azimuth } = dipAzFromPole(fit.normal);
      addMeasurement(out, fit.centroid[0], fit.centroid[1], fit.centroid[2], dip, dip_azimuth, {
        polarity: 1, confidence: o.confidence, source: lineset.name,
        feature: feat.unit || feat.name || feat.type || '', part: k,
        relief_m: +relief.toFixed(1), fit_rms_m: +fit.rms.toFixed(2), span_m: +fit.l1.toFixed(0), plan_spread_m: +fit.plan_minor.toFixed(0),
        window_m: Math.round(w), n_pts: win.length,
      });
      stats.kept++;
    }
  }
  out.provenance = { method: 'least-squares plane through a draped map trace (three-point problem)', source_layer: lineset.name, source_id: lineset.id, window_m: o.window, step_m: o.step };
  out.metadata.derived = stats;
  out.metadata.howto = 'Dip read from where the mapped trace crosses terrain (window_m records how far along the line each reading had to look) — it is only as good as the map and the DEM. Windows without relief, without spread, or with a poor plane fit were rejected rather than guessed; the counts are in DERIVED below. Every point carries relief_m and fit_rms_m so a weak reading stays visible.';
  if (!stats.kept) out.warn(`no orientation could be derived: ${stats.windows} windows tried — ${stats.no_relief} lacked relief, ${stats.no_spread} lacked spread, ${stats.bad_fit} fitted poorly. Flat ground carries no dip information.`);
  else if (stats.kept < stats.windows * 0.25) out.warn(`only ${stats.kept} of ${stats.windows} windows produced a usable plane — treat the coverage as sparse.`);
  return out;
}

/** Drape a PointSet onto a Grid2D (Leapfrog's `Set Elevation`).  Keeps the
    original z in `z_original` so it can be switched back. */
export function setElevationFromGrid(ps, grid, opts = {}) {
  const off = opts.offset == null ? 0 : +opts.offset;
  const keep = ps.attributes.z_original || [];
  let moved = 0, outside = 0;
  for (let i = 0; i < ps.n; i++) {
    const x = ps.xyz[3 * i], y = ps.xyz[3 * i + 1];
    const z = grid.sample(x, y);
    if (z !== z) { outside++; continue; }
    if (keep[i] == null) keep[i] = ps.xyz[3 * i + 2];
    ps.xyz[3 * i + 2] = z + off; moved++;
  }
  ps.attributes.z_original = keep;
  ps.metadata.elevation_from = grid.name + (off ? ` (+${off} m)` : '');
  if (outside) ps.warn(`${outside} point(s) fell outside ${grid.name} and kept their original elevation`);
  return { moved, outside };
}

/* ============================================================ eigen ==== */

/** Jacobi eigen-decomposition of a symmetric 3x3.  Returns eigenvalues in
    DESCENDING order with matching unit eigenvectors. */
export function jacobiEigen3(Min) {
  const a = [Min[0].slice(), Min[1].slice(), Min[2].slice()];
  let v = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  for (let sweep = 0; sweep < 64; sweep++) {
    let off = 0; for (let p = 0; p < 3; p++) for (let q = p + 1; q < 3; q++) off += a[p][q] * a[p][q];
    if (off < 1e-30) break;
    for (let p = 0; p < 2; p++) for (let q = p + 1; q < 3; q++) {
      if (Math.abs(a[p][q]) < 1e-300) continue;
      const theta = (a[q][q] - a[p][p]) / (2 * a[p][q]);
      const t = Math.sign(theta || 1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
      const c = 1 / Math.sqrt(t * t + 1), s = t * c;
      for (let k = 0; k < 3; k++) { const akp = a[k][p], akq = a[k][q]; a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq; }
      for (let k = 0; k < 3; k++) { const apk = a[p][k], aqk = a[q][k]; a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk; }
      for (let k = 0; k < 3; k++) { const vkp = v[k][p], vkq = v[k][q]; v[k][p] = c * vkp - s * vkq; v[k][q] = s * vkp + c * vkq; }
    }
  }
  const order = [0, 1, 2].sort((i, j) => a[j][j] - a[i][i]);
  return { values: order.map(i => a[i][i]), vectors: order.map(i => unit([v[0][i], v[1][i], v[2][i]])) };
}

/** Orientation tensor of a set of axes (sign-insensitive), normalised by n. */
export function orientationTensor(vecs, n = null) {
  const m = n == null ? vecs.length / 3 : n;
  const T = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let i = 0; i < m; i++) { const x = vecs[3 * i], y = vecs[3 * i + 1], z = vecs[3 * i + 2]; T[0][0] += x * x; T[0][1] += x * y; T[0][2] += x * z; T[1][1] += y * y; T[1][2] += y * z; T[2][2] += z * z; }
  T[1][0] = T[0][1]; T[2][0] = T[0][2]; T[2][1] = T[1][2];
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) T[i][j] /= (m || 1);
  return T;
}

/* ======================================================== statistics === */

/** Bingham analysis of poles to planes.  e1 = pole to the mean plane;
    e3 = pole to the best-fit great circle, which for folded bedding IS the
    fold hinge (and the best-fit plane IS the profile plane). */
export function binghamStats(poles, n = null) {
  const m = n == null ? poles.length / 3 : n;
  if (m < 2) return null;
  const { values, vectors } = jacobiEigen3(orientationTensor(poles, m));
  const [e1, e2, e3] = vectors;
  const meanPlane = dipAzFromPole(e1);
  const bestFit = dipAzFromPole(e3);
  const hinge = trendPlunge(e3);
  const K = values[0] > 0 ? Math.log(values[0] / Math.max(values[2], 1e-12)) : 0;
  const shape = Math.log(values[0] / Math.max(values[1], 1e-12)) / Math.max(Math.log(values[1] / Math.max(values[2], 1e-12)), 1e-12);   // Woodcock K
  return {
    n: m, eigenvalues: values, eigenvectors: [e1, e2, e3],
    mean_plane: meanPlane, mean_pole: trendPlunge(e1),
    best_fit_plane: bestFit, fold_hinge: hinge,
    strength: K, shape,
    fabric: shape > 1 ? 'cluster (point maximum)' : shape < 0.6 ? 'girdle (folded / planar fabric)' : 'transitional',
  };
}

/** Fisher statistics of a set of vectors.  Vectors are folded onto a common
    hemisphere first (axial data has no true mean otherwise). */
export function fisherStats(vecs, n = null) {
  const m = n == null ? vecs.length / 3 : n;
  if (m < 2) return null;
  // reference = Bingham e1, so folding is stable for steeply dipping sets
  const ref = jacobiEigen3(orientationTensor(vecs, m)).vectors[0];
  let sx = 0, sy = 0, sz = 0;
  for (let i = 0; i < m; i++) {
    let x = vecs[3 * i], y = vecs[3 * i + 1], z = vecs[3 * i + 2];
    if (x * ref[0] + y * ref[1] + z * ref[2] < 0) { x = -x; y = -y; z = -z; }
    sx += x; sy += y; sz += z;
  }
  const R = Math.hypot(sx, sy, sz), mean = unit([sx, sy, sz]), Rbar = R / m;
  const kappa = m > R ? (m - 1) / (m - R) : Infinity;
  let a95 = null;
  if (m > 1 && R > 0 && R < m) {
    const c = 1 - ((m - R) / R) * (Math.pow(1 / 0.05, 1 / (m - 1)) - 1);
    a95 = c > -1 && c < 1 ? Math.acos(c) * RAD : null;
  }
  const plane = dipAzFromPole(mean);
  return { n: m, R, Rbar, kappa, alpha95: a95, mean_vector: mean, mean_plane: plane, mean_pole: trendPlunge(mean), steep: plane.dip > 50 };
}

/* ======================================================== stereonet ==== */

export const PROJECTIONS = { equal_area: 'equal area (Schmidt)', equal_angle: 'equal angle (Wulff)' };
export const NET_TYPES = { equatorial: 'equatorial', polar: 'polar' };
export const CONTOUR_METHODS = { kamb: 'Kamb', exp_kamb: 'exponential Kamb', schmidt: 'Schmidt (1% area)' };

/** Project a direction onto the lower-hemisphere net.  Returns {x, y} on the
    unit disc, or null if the vector is degenerate. */
export function projectVec(v, projection = 'equal_area') {
  let [x, y, z] = v; const L = Math.hypot(x, y, z); if (!L) return null;
  x /= L; y /= L; z /= L;
  if (z > 0) { x = -x; y = -y; z = -z; }           // lower hemisphere
  const plunge = Math.asin(clamp(-z, -1, 1));       // 0 at the rim, pi/2 at the centre
  const half = (Math.PI / 2 - plunge) / 2;
  const r = projection === 'equal_angle' ? Math.tan(half) : Math.SQRT2 * Math.sin(half);
  const t = Math.atan2(x, y);
  return { x: r * Math.sin(t), y: r * Math.cos(t), r };
}
/** Inverse of projectVec: a point on the unit disc -> a downward unit vector. */
export function unprojectDisc(px, py, projection = 'equal_area') {
  const r = Math.hypot(px, py); if (r > 1.0000001) return null;
  const half = projection === 'equal_angle' ? Math.atan(r) : Math.asin(clamp(r / Math.SQRT2, -1, 1));
  const plunge = Math.PI / 2 - 2 * half;
  const t = Math.atan2(px, py), cp = Math.cos(plunge);
  return [Math.sin(t) * cp, Math.cos(t) * cp, -Math.sin(plunge)];
}
/** Great circle of a plane, as one or more polylines on the unit disc. */
export function greatCircle(dip, dipAz, opts = {}) {
  const proj = opts.projection || 'equal_area', steps = opts.steps || 361;
  const s = strikeVector(dipAz), d = dipVector(dip, dipAz);
  const parts = []; let cur = [];
  let prev = null;
  for (let i = 0; i < steps; i++) {
    const t = (i / (steps - 1)) * Math.PI;           // half turn covers the circle for axial data
    const v = [Math.cos(t) * s[0] + Math.sin(t) * d[0], Math.cos(t) * s[1] + Math.sin(t) * d[1], Math.cos(t) * s[2] + Math.sin(t) * d[2]];
    const p = projectVec(v, proj); if (!p) continue;
    if (prev && Math.hypot(p.x - prev.x, p.y - prev.y) > 0.5) { if (cur.length > 1) parts.push(cur); cur = []; }
    cur.push([p.x, p.y]); prev = p;
  }
  if (cur.length > 1) parts.push(cur);
  return parts;
}
/** Small circle (cone) about an axis, for confidence cones. */
export function smallCircle(axis, angleDeg, opts = {}) {
  const proj = opts.projection || 'equal_area', steps = opts.steps || 181;
  const a = unit(axis); const tmp = Math.abs(a[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
  const u = unit(cross(a, tmp)), w = cross(a, u);
  const ca = Math.cos(angleDeg * DEG), sa = Math.sin(angleDeg * DEG);
  const parts = []; let cur = [], prev = null;
  for (let i = 0; i < steps; i++) {
    const t = (i / (steps - 1)) * 2 * Math.PI;
    const v = [ca * a[0] + sa * (Math.cos(t) * u[0] + Math.sin(t) * w[0]), ca * a[1] + sa * (Math.cos(t) * u[1] + Math.sin(t) * w[1]), ca * a[2] + sa * (Math.cos(t) * u[2] + Math.sin(t) * w[2])];
    const p = projectVec(v, proj); if (!p) continue;
    if (prev && Math.hypot(p.x - prev.x, p.y - prev.y) > 0.5) { if (cur.length > 1) parts.push(cur); cur = []; }
    cur.push([p.x, p.y]); prev = p;
  }
  if (cur.length > 1) parts.push(cur);
  return parts;
}

/** Deterministic desampling for DISPLAY ONLY (Leapfrog's `desample rate`,
    0..1, default 0.5).  Never used for statistics — the guide is explicit
    that all data is always used in the calculations. */
export function desampleMask(poles, n, rate = 0.5, projection = 'equal_area') {
  if (!(rate > 0)) return null;
  const cells = Math.max(8, Math.round(20 + (1 - rate) * 260));
  const seen = new Set(), keep = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const p = projectVec([poles[3 * i], poles[3 * i + 1], poles[3 * i + 2]], projection); if (!p) continue;
    const k = Math.round((p.x + 1) / 2 * cells) * (cells + 1) + Math.round((p.y + 1) / 2 * cells);
    if (seen.has(k)) continue; seen.add(k); keep[i] = 1;
  }
  return keep;
}

/** Density on a square lattice covering the unit disc.
    Methods follow the standard formulations (as in mplstereonet):
      kamb      count of poles inside a counting circle sized so the expected
                count is sigma^2; contoured in units of sigma
      exp_kamb  Vollmer's exponentially weighted variant
      schmidt   count inside a 1% area circle, contoured in % per 1% area   */
export function densityGrid(poles, n, opts = {}) {
  const size = opts.size || 96, proj = opts.projection || 'equal_area';
  const method = opts.method || 'kamb', sigma = opts.sigma || 3;
  const N = Math.max(1, n);
  let radius, units, expF = 0;
  if (method === 'schmidt') { radius = 1 - 0.01; units = 0.01 * N; }
  else if (method === 'exp_kamb') { expF = 2 * (1 + N / (sigma * sigma)); units = Math.sqrt(N * (expF / 2 - 1) / (expF * expF)); radius = 0; }
  else { const a = (sigma * sigma) / (N + sigma * sigma); radius = 1 - a; units = Math.sqrt(N * a * (1 - a)); }
  const grid = new Float64Array(size * size).fill(NaN);
  const dirs = new Float64Array(size * size * 3); const active = [];
  for (let j = 0; j < size; j++) for (let i = 0; i < size; i++) {
    const px = (i / (size - 1)) * 2 - 1, py = (j / (size - 1)) * 2 - 1;
    if (px * px + py * py > 1.0) continue;
    const v = unprojectDisc(px, py, proj); if (!v) continue;
    const k = j * size + i; dirs[3 * k] = v[0]; dirs[3 * k + 1] = v[1]; dirs[3 * k + 2] = v[2]; active.push(k); grid[k] = 0;
  }
  for (const k of active) {
    const nx = dirs[3 * k], ny = dirs[3 * k + 1], nz = dirs[3 * k + 2];
    let c = 0;
    if (method === 'exp_kamb') { for (let i = 0; i < n; i++) { const cd = Math.abs(nx * poles[3 * i] + ny * poles[3 * i + 1] + nz * poles[3 * i + 2]); c += Math.exp(expF * (cd - 1)); } }
    else { for (let i = 0; i < n; i++) { const cd = Math.abs(nx * poles[3 * i] + ny * poles[3 * i + 1] + nz * poles[3 * i + 2]); if (cd >= radius) c++; } }
    grid[k] = c / (units || 1);
  }
  let max = 0; for (const k of active) if (grid[k] > max) max = grid[k];
  return { grid, size, max, method, sigma, units, projection: proj, unit_label: method === 'schmidt' ? '% per 1% area' : 'σ' };
}

/** Marching squares on the density lattice -> contour polylines on the disc. */
export function contourLines(dg, levels) {
  const { grid, size } = dg, out = [];
  const at = (i, j) => grid[j * size + i];
  const px = i => (i / (size - 1)) * 2 - 1;
  for (const lv of levels) {
    const segs = [];
    for (let j = 0; j < size - 1; j++) for (let i = 0; i < size - 1; i++) {
      const v = [at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)];
      if (v.some(x => x !== x)) continue;
      let code = 0; for (let k = 0; k < 4; k++) if (v[k] >= lv) code |= (1 << k);
      if (code === 0 || code === 15) continue;
      const X = [px(i), px(i + 1), px(i + 1), px(i)], Y = [px(j), px(j), px(j + 1), px(j + 1)];
      const pt = e => { const a = e, b = (e + 1) % 4; const t = (lv - v[a]) / ((v[b] - v[a]) || 1e-12); return [X[a] + (X[b] - X[a]) * t, Y[a] + (Y[b] - Y[a]) * t]; };
      const edges = []; for (let e = 0; e < 4; e++) { const a = e, b = (e + 1) % 4; if ((v[a] >= lv) !== (v[b] >= lv)) edges.push(e); }
      for (let e = 0; e + 1 < edges.length; e += 2) segs.push([pt(edges[e]), pt(edges[e + 1])]);
    }
    out.push({ level: lv, segments: segs });
  }
  return out;
}

/* ======================================================= declustering == */

export const DECLUSTER_DEFAULTS = { radius: 25, angular_tolerance: 30, min_keep_fraction: 0.5 };

/** Spatial declustering after Leapfrog: cluster by a search radius, drop
    outliers beyond an angular tolerance from the cluster mean, keep the one
    measurement closest to that mean (optionally weighted by a priority
    column).  A cluster too inconsistent to have a mean is dropped whole —
    which the guide warns about explicitly, so it is counted and surfaced. */
export function decluster(ps, opts = {}) {
  const o = Object.assign({}, DECLUSTER_DEFAULTS, opts);
  const S = readStructural(ps);
  if (!S.n) throw new Error('no valid measurements');
  const cat = o.category_column && ps.attributes[o.category_column] ? ps.attributes[o.category_column] : null;
  const prio = o.priority_column && ps.attributes[o.priority_column] ? ps.numeric(o.priority_column) : null;
  const cell = Math.max(o.radius, 1e-6);
  const buckets = new Map();
  for (let k = 0; k < S.n; k++) {
    const i = S.index[k];
    const key = [Math.floor(S.x[k] / cell), Math.floor(S.y[k] / cell), Math.floor(S.z[k] / cell), cat ? String(cat[i]) : ''].join('|');
    if (!buckets.has(key)) buckets.set(key, []); buckets.get(key).push(k);
  }
  const keep = [], dropped = [];
  let noisy = 0;
  for (const members of buckets.values()) {
    if (members.length === 1) { keep.push(members[0]); continue; }
    const sub = new Float64Array(members.length * 3);
    members.forEach((k, q) => { sub[3 * q] = S.poles[3 * k]; sub[3 * q + 1] = S.poles[3 * k + 1]; sub[3 * q + 2] = S.poles[3 * k + 2]; });
    let mean = jacobiEigen3(orientationTensor(sub, members.length)).vectors[0];
    // discard outliers beyond the angular tolerance, then recompute
    let inl = members.filter(k => axialAngle([S.poles[3 * k], S.poles[3 * k + 1], S.poles[3 * k + 2]], mean) <= o.angular_tolerance);
    if (inl.length < Math.max(1, Math.ceil(members.length * o.min_keep_fraction))) { noisy++; members.forEach(k => dropped.push(k)); continue; }
    const sub2 = new Float64Array(inl.length * 3);
    inl.forEach((k, q) => { sub2[3 * q] = S.poles[3 * k]; sub2[3 * q + 1] = S.poles[3 * k + 1]; sub2[3 * q + 2] = S.poles[3 * k + 2]; });
    mean = jacobiEigen3(orientationTensor(sub2, inl.length)).vectors[0];
    let best = null, bestScore = Infinity;
    for (const k of inl) {
      const ang = axialAngle([S.poles[3 * k], S.poles[3 * k + 1], S.poles[3 * k + 2]], mean);
      const w = prio ? (prio[S.index[k]] === prio[S.index[k]] ? prio[S.index[k]] : 0) : 0;
      const score = ang - w * (o.priority_weight == null ? 5 : o.priority_weight);
      if (score < bestScore) { bestScore = score; best = k; }
    }
    keep.push(best);
    for (const k of members) if (k !== best) dropped.push(k);
  }
  keep.sort((a, b) => a - b);
  const out = newStructural(o.name || (ps.name + ' — declustered'));
  out.color = ps.color.slice();
  const extra = Object.keys(ps.attributes).filter(k => !['dip', 'dip_azimuth', 'polarity'].includes(k));
  for (const k of keep) {
    const i = S.index[k], attrs = { polarity: S.polarity[k] };
    for (const c of extra) attrs[c] = ps.attributes[c][i];
    addMeasurement(out, S.x[k], S.y[k], S.z[k], S.dip[k], S.dipaz[k], attrs);
  }
  out.provenance = { method: 'spatial declustering', source_layer: ps.name, source_id: ps.id, radius_m: o.radius, angular_tolerance_deg: o.angular_tolerance, priority_column: o.priority_column || null, category_column: o.category_column || null };
  out.metadata.declustered = { input: S.n, kept: keep.length, removed: dropped.length, clusters_dropped_as_noisy: noisy };
  if (noisy) out.warn(`${noisy} cluster(s) were too inconsistently oriented to pick a representative measurement and were dropped entirely — check for subvertical or mixed-set data at those locations.`);
  return { ps: out, kept: keep.length, removed: dropped.length, noisy };
}

/* ================================================= form interpolant ==== */

function solveDenseLocal(A, b, N) {
  const M = A, x = new Float64Array(N);
  for (let c = 0; c < N; c++) {
    let piv = c, best = Math.abs(M[c * N + c]);
    for (let r = c + 1; r < N; r++) { const v = Math.abs(M[r * N + c]); if (v > best) { best = v; piv = r; } }
    if (best < 1e-14) { M[c * N + c] += 1e-10; }
    if (piv !== c) { for (let k = c; k < N; k++) { const t = M[c * N + k]; M[c * N + k] = M[piv * N + k]; M[piv * N + k] = t; } const t = b[c]; b[c] = b[piv]; b[piv] = t; }
    const d = M[c * N + c];
    for (let r = c + 1; r < N; r++) {
      const f = M[r * N + c] / d; if (!f) continue;
      for (let k = c; k < N; k++) M[r * N + k] -= f * M[c * N + k];
      b[r] -= f * b[c];
    }
  }
  for (let r = N - 1; r >= 0; r--) { let s = b[r]; for (let k = r + 1; k < N; k++) s -= M[r * N + k] * x[k]; x[r] = s / (M[r * N + r] || 1e-14); }
  return x;
}

export const FORM_DEFAULTS = { smoothing: 1e-6, max_points: 400 };

/** Gradient-only RBF ("form interpolant", Lajaunie et al. 1997).
    f is built so that grad f(p_i) = pole_i at every measurement, using the
    cubic kernel K(r)=r^3 whose Hessian H = 3(r I + d d^T / r).  The absolute
    value of f is meaningless — only its level sets are, which is exactly what
    the Leapfrog guide says about form-interpolant thresholds. */
export class FormInterpolant {
  constructor(o = {}) {
    this.points = o.points ? Float64Array.from(o.points) : new Float64Array(0);
    this.coef = o.coef ? Float64Array.from(o.coef) : new Float64Array(0);
    this.drift = o.drift ? Array.from(o.drift) : [0, 0, 0];
    this.scale = o.scale || 1; this.offset = o.offset ? Array.from(o.offset) : [0, 0, 0];
    this.smoothing = o.smoothing == null ? FORM_DEFAULTS.smoothing : o.smoothing;
    this.centreValue = o.centre_value || 0;
    this.meta = o.meta || {};
  }
  get n() { return this.points.length / 3; }
  /** Solve for the coefficients so that grad f(p_i) = pole_i.
      Unknowns: one 3-vector per measurement plus a constant drift gradient;
      the extra 3 rows impose sum(c) = 0, the side condition the cubic kernel
      needs to be conditionally positive definite. */
  fit(points, poles, opts = {}) {
    const n = points.length / 3;
    if (n < 3) throw new Error('a form interpolant needs at least 3 measurements');
    const mn = [Infinity, Infinity, Infinity], mx = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < n; i++) for (let a = 0; a < 3; a++) { const v = points[3 * i + a]; if (v < mn[a]) mn[a] = v; if (v > mx[a]) mx[a] = v; }
    this.offset = [0, 1, 2].map(a => (mn[a] + mx[a]) / 2);
    this.scale = Math.max(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2], 1);
    const P = new Float64Array(n * 3);
    for (let i = 0; i < n; i++) for (let a = 0; a < 3; a++) P[3 * i + a] = (points[3 * i + a] - this.offset[a]) / this.scale;
    const N = 3 * n + 3, A = new Float64Array(N * N), b = new Float64Array(N);
    const lam = (opts.smoothing == null ? this.smoothing : opts.smoothing);
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        const dx = P[3 * i] - P[3 * j], dy = P[3 * i + 1] - P[3 * j + 1], dz = P[3 * i + 2] - P[3 * j + 2];
        const r = Math.hypot(dx, dy, dz);
        if (r < 1e-12) continue;                   // H(0) = 0 for the cubic kernel
        const d = [dx, dy, dz];
        for (let a = 0; a < 3; a++) for (let c = 0; c < 3; c++) A[(3 * i + a) * N + 3 * j + c] = 3 * ((a === c ? r : 0) + d[a] * d[c] / r);
      }
      for (let a = 0; a < 3; a++) {
        A[(3 * i + a) * N + 3 * i + a] += lam;
        A[(3 * i + a) * N + 3 * n + a] = 1;        // constant drift gradient
        A[(3 * n + a) * N + 3 * i + a] = 1;        // sum(c) = 0
        b[3 * i + a] = poles[3 * i + a];
      }
    }
    const sol = solveDenseLocal(A, b, N);
    this.points = P; this.coef = sol.slice(0, 3 * n); this.drift = [sol[3 * n], sol[3 * n + 1], sol[3 * n + 2]];
    this.smoothing = lam;
    this.meta = Object.assign({ method: 'gradient RBF (cubic kernel, constant drift) — Lajaunie potential field', n_measurements: n, smoothing: lam }, opts.meta || {});
    return this;
  }
  /** f at a single point, in project coordinates. */
  value(x, y, z) {
    const n = this.n, s = this.scale, o = this.offset;
    const X = (x - o[0]) / s, Y = (y - o[1]) / s, Z = (z - o[2]) / s;
    let f = this.drift[0] * X + this.drift[1] * Y + this.drift[2] * Z;
    const P = this.points, C = this.coef;
    for (let j = 0; j < n; j++) {
      const dx = X - P[3 * j], dy = Y - P[3 * j + 1], dz = Z - P[3 * j + 2];
      const r = Math.hypot(dx, dy, dz); if (r < 1e-12) continue;
      f += 3 * r * (dx * C[3 * j] + dy * C[3 * j + 1] + dz * C[3 * j + 2]);
    }
    return f - this.centreValue;
  }
  /** grad f at a point (used to verify the fit and to drive trend glyphs). */
  gradient(x, y, z) {
    const n = this.n, s = this.scale, o = this.offset;
    const X = (x - o[0]) / s, Y = (y - o[1]) / s, Z = (z - o[2]) / s;
    const g = [this.drift[0], this.drift[1], this.drift[2]];
    const P = this.points, C = this.coef;
    for (let j = 0; j < n; j++) {
      const d = [X - P[3 * j], Y - P[3 * j + 1], Z - P[3 * j + 2]];
      const r = Math.hypot(d[0], d[1], d[2]); if (r < 1e-12) continue;
      for (let a = 0; a < 3; a++) for (let c = 0; c < 3; c++) g[a] += 3 * ((a === c ? r : 0) + d[a] * d[c] / r) * C[3 * j + c];
    }
    return g;
  }
  toJSON() { return { points: Array.from(this.points), coef: Array.from(this.coef), drift: this.drift, scale: this.scale, offset: this.offset, smoothing: this.smoothing, centre_value: this.centreValue, meta: this.meta }; }
  static fromJSON(d) { return new FormInterpolant(d); }
}

/** Fit a form interpolant from a structural PointSet.  Returns the fitted
    object plus a residual report (max angle between the reproduced gradient
    and the measured pole) so the fit can be trusted or rejected. */
export function fitFormInterpolant(ps, opts = {}) {
  const S = readStructural(ps, opts);
  if (S.n < 3) throw new Error('need at least 3 measurements');
  const cap = opts.max_points || FORM_DEFAULTS.max_points;
  let use = S;
  let thinned = 0;
  if (S.n > cap) {
    const stride = S.n / cap, pick = [];
    for (let i = 0; i < cap; i++) pick.push(Math.min(S.n - 1, Math.floor(i * stride)));
    const poles = new Float64Array(pick.length * 3);
    pick.forEach((k, q) => { poles[3 * q] = S.poles[3 * k]; poles[3 * q + 1] = S.poles[3 * k + 1]; poles[3 * q + 2] = S.poles[3 * k + 2]; });
    use = { n: pick.length, x: pick.map(k => S.x[k]), y: pick.map(k => S.y[k]), z: pick.map(k => S.z[k]), poles };
    thinned = S.n - pick.length;
  }
  const pts = new Float64Array(use.n * 3);
  for (let i = 0; i < use.n; i++) { pts[3 * i] = use.x[i]; pts[3 * i + 1] = use.y[i]; pts[3 * i + 2] = use.z[i]; }
  const fi = new FormInterpolant({ smoothing: opts.smoothing });
  fi.fit(pts, use.poles, opts);
  // residuals
  let worst = 0, sum = 0;
  for (let i = 0; i < use.n; i++) {
    const g = unit(fi.gradient(pts[3 * i], pts[3 * i + 1], pts[3 * i + 2]));
    const ang = axialAngle(g, [use.poles[3 * i], use.poles[3 * i + 1], use.poles[3 * i + 2]]);
    sum += ang; if (ang > worst) worst = ang;
  }
  fi.meta.residual_mean_deg = +(sum / use.n).toFixed(3);
  fi.meta.residual_max_deg = +worst.toFixed(3);
  fi.meta.thinned = thinned;
  fi.meta.source = ps.name;
  return fi;
}

/** Sample a form interpolant onto a lattice.  Values are shifted so that the
    centre of the box is 0 — the guide's "surfaces are defined relative to the
    centre of that cube". */
export function formField(fi, bounds, spacing, onProgress = null) {
  const [x0, y0, z0, x1, y1, z1] = bounds;
  const sp = typeof spacing === 'number' ? [spacing, spacing, spacing] : spacing;
  const nx = Math.max(2, Math.ceil((x1 - x0) / sp[0]) + 1), ny = Math.max(2, Math.ceil((y1 - y0) / sp[1]) + 1), nz = Math.max(2, Math.ceil((z1 - z0) / sp[2]) + 1);
  const field = new Float64Array(nx * ny * nz);
  const centre = fi.value((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
  let q = 0;
  for (let k = 0; k < nz; k++) {
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) field[q++] = fi.value(x0 + i * sp[0], y0 + j * sp[1], z0 + k * sp[2]) - centre;
    if (onProgress) onProgress((k + 1) / nz);
  }
  return { field, count: [nx, ny, nz], origin: [x0, y0, z0], spacing: sp, centre_value: centre };
}

/** Default thresholds: evenly spaced through the field's range, excluding the
    extremes.  Leapfrog derives them from the bounding box and says plainly
    that the values are not geologically meaningful. */
export function defaultThresholds(field, count = 5) {
  let lo = Infinity, hi = -Infinity;
  for (const v of field) { if (v !== v) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!(hi > lo)) return [0];
  const out = []; for (let i = 1; i <= count; i++) out.push(+(lo + (hi - lo) * i / (count + 1)).toFixed(6));
  return out;
}

/* ==================================================== structural trend = */

/** Build an anisotropy whose major axis lies in a plane at a given pitch.
    dip / dipAz define the plane; pitch is measured in the plane from strike
    (Leapfrog's green centre arrow, movable through 180 degrees); ratios are
    major : semi-major : minor as in Leapfrog's `Ellipsoid Ratios` (3,3,1). */
export function planeAnisotropy(dip, dipAz, pitch, ratios = [3, 3, 1], baseRange = 1) {
  const s = strikeVector(dipAz), d = dipVector(dip, dipAz);
  const cp = Math.cos(pitch * DEG), sp = Math.sin(pitch * DEG);
  const major = unit([cp * s[0] + sp * d[0], cp * s[1] + sp * d[1], cp * s[2] + sp * d[2]]);
  const nrm = poleFromDipAz(dip, dipAz, 1);
  const semi = unit(cross(nrm, major));
  const tp = trendPlunge(major);
  // trendPlunge flips downward; keep the frame consistent by using the flipped axis
  const majorDown = tp.plunge >= 0 ? unit(lineVector(tp.trend, tp.plunge)) : major;
  const zero = new E.Anisotropy([1, 1, 1], tp.trend, tp.plunge, 0);
  const semi0 = zero.rot[1];
  const sgn = dot(cross(semi0, semi), majorDown);
  const roll = Math.atan2(sgn, clamp(dot(semi0, semi), -1, 1)) * RAD;
  const r = ratios.map(v => Math.max(1e-6, +v));
  return new E.Anisotropy([baseRange * r[0], baseRange * r[1], baseRange * r[2]], tp.trend, tp.plunge, roll);
}

export const TREND_DEFAULTS = { strength: 5, range: 100 };

/** A spatially varying anisotropy field, after Leapfrog's structural trend.
    Inputs are structural measurements and/or meshes.  At a query point the
    nearest input supplies the local plane; the anisotropy ratio starts at
    `strength` on the input and HALVES every `range` — 5:5:1 at the surface,
    2.5:2.5:1 at one range, 1.25:1.25:1 at two, approaching but never reaching
    isotropic, exactly as the course guide states. */
export class TrendField {
  constructor(o = {}) {
    this.strength = o.strength == null ? TREND_DEFAULTS.strength : +o.strength;
    this.range = o.range == null ? TREND_DEFAULTS.range : +o.range;
    this.type = o.type || 'strongest along inputs';
    this.sources = o.sources || [];
    this.xyz = o.xyz ? Float64Array.from(o.xyz) : new Float64Array(0);
    this.normals = o.normals ? Float64Array.from(o.normals) : new Float64Array(0);
    this._index = null;
  }
  get n() { return this.xyz.length / 3; }
  index() { if (!this._index && this.n) this._index = new E.GridIndex(this.xyz, 3); return this._index; }
  /** Local trend at a point: the plane, the distance to its control, and the
      decayed anisotropy ratio. */
  at(x, y, z) {
    const n = this.n; if (!n) return null;
    let best = -1, bestD = Infinity;
    const ix = this.index();
    if (ix && ix.nearest(x, y, z, 1)) { best = ix.resI[0]; bestD = ix.resD[0]; }
    if (best < 0) for (let i = 0; i < n; i++) { const d = Math.hypot(x - this.xyz[3 * i], y - this.xyz[3 * i + 1], z - this.xyz[3 * i + 2]); if (d < bestD) { bestD = d; best = i; } }
    const nrm = [this.normals[3 * best], this.normals[3 * best + 1], this.normals[3 * best + 2]];
    const { dip, dip_azimuth } = dipAzFromPole(nrm);
    const ratio = Math.max(1, this.strength * Math.pow(2, -bestD / Math.max(this.range, 1e-6)));
    return { dip, dip_azimuth, distance: bestD, ratio, normal: nrm };
  }
  anisotropyAt(x, y, z, baseRange = 1) {
    const t = this.at(x, y, z); if (!t) return null;
    return planeAnisotropy(t.dip, t.dip_azimuth, 0, [t.ratio, t.ratio, 1], baseRange);
  }
  toJSON() { return { strength: this.strength, range: this.range, type: this.type, sources: this.sources, xyz: Array.from(this.xyz), normals: Array.from(this.normals) }; }
  static fromJSON(d) { return new TrendField(d); }
}

/** Collect trend control points from structural PointSets and/or meshes. */
export function buildTrendField(inputs, opts = {}) {
  const X = [], N = [], sources = [];
  for (const o of inputs) {
    if (isStructural(o)) {
      const S = readStructural(o);
      for (let i = 0; i < S.n; i++) { X.push(S.x[i], S.y[i], S.z[i]); N.push(S.poles[3 * i], S.poles[3 * i + 1], S.poles[3 * i + 2]); }
      sources.push({ id: o.id, name: o.name, kind: 'structural', n: S.n });
    } else if (o.kind === 'mesh') {
      const V = o.vertices, T = o.triangles, nt = T.length / 3;
      const stride = Math.max(1, Math.floor(nt / (opts.mesh_samples || 4000)));
      let used = 0;
      for (let t = 0; t < nt; t += stride) {
        const a = T[3 * t], b = T[3 * t + 1], c = T[3 * t + 2];
        const p = [V[3 * a], V[3 * a + 1], V[3 * a + 2]], q = [V[3 * b], V[3 * b + 1], V[3 * b + 2]], r = [V[3 * c], V[3 * c + 1], V[3 * c + 2]];
        const u = [q[0] - p[0], q[1] - p[1], q[2] - p[2]], v = [r[0] - p[0], r[1] - p[1], r[2] - p[2]];
        const nn = unit(cross(u, v)); if (!isFinite(nn[0])) continue;
        X.push((p[0] + q[0] + r[0]) / 3, (p[1] + q[1] + r[1]) / 3, (p[2] + q[2] + r[2]) / 3);
        N.push(nn[0], nn[1], nn[2]); used++;
      }
      sources.push({ id: o.id, name: o.name, kind: 'mesh', n: used });
    } else if (o.kind === 'lineset') {
      // a draped trace: use the local tangent + vertical as the plane (a
      // vertical structure along the trace) — honest for mapped faults with
      // no dip data, and flagged as such.
      for (let k = 0; k < o.parts.length; k++) {
        const idx = o.parts[k];
        for (let i = 1; i < idx.length; i++) {
          const a = idx[i - 1], b = idx[i];
          const t = unit([o.vertices[3 * b] - o.vertices[3 * a], o.vertices[3 * b + 1] - o.vertices[3 * a + 1], 0]);
          if (!isFinite(t[0])) continue;
          const nn = unit(cross(t, [0, 0, 1]));
          X.push((o.vertices[3 * a] + o.vertices[3 * b]) / 2, (o.vertices[3 * a + 1] + o.vertices[3 * b + 1]) / 2, (o.vertices[3 * a + 2] + o.vertices[3 * b + 2]) / 2);
          N.push(nn[0], nn[1], nn[2]);
        }
      }
      sources.push({ id: o.id, name: o.name, kind: 'lineset (assumed vertical)', n: 0 });
    }
  }
  if (!X.length) throw new Error('no usable trend inputs');
  return new TrendField({ strength: opts.strength, range: opts.range, sources, xyz: X, normals: N });
}

/** Ellipsoid glyphs for a trend field, as a structural PointSet whose
    `strength` column drives glyph size — Leapfrog draws a smaller ellipsoid
    where the trend is weaker, so the extent of the trend is visible. */
export function trendGlyphs(field, bounds, opts = {}) {
  const n = opts.n || 9;
  const ps = newStructural(opts.name || 'Structural trend');
  ps.role = 'trend'; ps.color = [214, 145, 72]; ps.group = 'Structure';
  ps.attributes.strength = []; ps.attributes.distance = [];
  const [x0, y0, z0, x1, y1, z1] = bounds;
  const nz = Math.max(2, Math.round(n / 2));
  for (let k = 0; k < nz; k++) for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    const x = x0 + (x1 - x0) * (i + 0.5) / n, y = y0 + (y1 - y0) * (j + 0.5) / n, z = z0 + (z1 - z0) * (k + 0.5) / nz;
    const t = field.at(x, y, z); if (!t) continue;
    if (t.ratio < 1.02) continue;                    // effectively isotropic: draw nothing
    addMeasurement(ps, x, y, z, t.dip, t.dip_azimuth, { strength: +t.ratio.toFixed(3), distance: +t.distance.toFixed(1) });
  }
  ps.provenance = { method: 'structural trend (strongest along inputs)', strength: field.strength, range_m: field.range, inputs: field.sources.map(s => s.name).join(', ') };
  ps.metadata.trend = field.toJSON();
  ps.metadata.howto = 'Anisotropy ratio halves every range: ' + field.strength + ':' + field.strength + ':1 on the input, ' + (field.strength / 2).toFixed(2) + ' at ' + field.range + ' m, ' + (field.strength / 4).toFixed(2) + ' at ' + (2 * field.range) + ' m. Glyphs are omitted where the trend has decayed to isotropic.';
  return ps;
}

/* ============================================== evaluate onto objects == */

/** Sample a scalar function onto a grid, mesh vertices or a point set —
    Leapfrog's "evaluate on surface".  Returns the attribute name written. */
export function evaluateOnto(target, fn, name = 'value') {
  if (target.kind === 'mesh') {
    const n = target.nVertices, vals = new Float32Array(n);
    for (let i = 0; i < n; i++) vals[i] = fn(target.vertices[3 * i], target.vertices[3 * i + 1], target.vertices[3 * i + 2]);
    target.attributes[name] = { location: 'vertices', values: vals };
  } else if (target.kind === 'points') {
    const col = []; for (let i = 0; i < target.n; i++) col.push(fn(target.xyz[3 * i], target.xyz[3 * i + 1], target.xyz[3 * i + 2]));
    target.attributes[name] = col;
  } else if (target.kind === 'grid2d') {
    const g = target.copyEmpty ? target.copyEmpty() : null;
    if (!g) throw new Error('grid target cannot be copied');
    for (let j = 0; j < target.ny; j++) for (let i = 0; i < target.nx; i++) { const [x, y] = target.nodeXY(i, j); const z = target.get(i, j); g.set(i, j, fn(x, y, z === z ? z : 0)); }
    g.name = target.name + ' — ' + name; g.role = 'property'; g.units = '';
    return g;
  } else throw new Error('cannot evaluate onto a ' + target.kind);
  return name;
}

/* ============================================== worker registration ==== */

/** Ops added to the shared engine registry so they run in gm-worker.js (and,
    when the worker cannot load, on the main thread through the same path). */
export const STRUCT_OPS = {
  deriveStructure: (a) => deriveFromTraces(a.lineset, a),
  declusterStructural: (a) => { const r = decluster(a.points || a.ps, a); return { points: r.ps, kept: r.kept, removed: r.removed, noisy: r.noisy }; },
  stereonetDensity: (a) => densityGrid(a.poles, a.n, a),
  /** Fit a form interpolant, evaluate it on a lattice and extract the form
      surfaces in one round trip. */
  formSurfaces: (a, p) => {
    const fi = fitFormInterpolant(a.points || a.ps, a);
    if (p) p(0.35, `fitted ${fi.meta.n_measurements} measurements (max residual ${fi.meta.residual_max_deg}°)`);
    const ff = formField(fi, a.bounds, a.spacing, f => p && p(0.35 + 0.45 * f, 'evaluating the field'));
    const thresholds = (a.thresholds && a.thresholds.length) ? a.thresholds.slice() : defaultThresholds(ff.field, a.count || 5);
    const meshes = [];
    thresholds.forEach((t, i) => {
      const m = E.isosurface(ff.field, ff.count, ff.origin, ff.spacing, { iso: t, name: `${a.name || 'Form'} ${i + 1}`, color: a.colors && a.colors[i] ? a.colors[i] : [110 + i * 22, 150, 210 - i * 18] });
      if (m.nTriangles) { m.role = 'surface'; m.group = 'Structure'; m.metadata.form_threshold = t; m.metadata.form = fi.meta; meshes.push(m); }
      if (p) p(0.8 + 0.2 * (i + 1) / thresholds.length, `form surface ${i + 1}/${thresholds.length}`);
    });
    return { surfaces: meshes, interpolant: fi.toJSON(), thresholds, meta: fi.meta, count: ff.count, spacing: ff.spacing };
  },
  trendGlyphs: (a) => { const f = buildTrendField(a.inputs, a); return { points: trendGlyphs(f, a.bounds, a), field: f.toJSON() }; },
};
try { Object.assign(E.OPS, STRUCT_OPS); } catch (e) { /* engine without a registry */ }
