/* tools/test_gm_structural.mjs — checks for site/assets/geomodel/gm-structural.js
   Run: node tools/test_gm_structural.mjs                                     */
import * as S from '../site/assets/geomodel/gm-structural.js';
import * as GM from '../site/assets/geomodel/gm-core.js';
import * as E from '../site/assets/geomodel/gm-engine.js';

let pass = 0, fail = 0; const fails = [];
const ok = (name, cond, extra = '') => { if (cond) { pass++; } else { fail++; fails.push(name + (extra ? ' — ' + extra : '')); } };
const close = (name, a, b, tol = 1e-9) => ok(name, Math.abs(a - b) <= tol, `got ${a}, want ${b} (tol ${tol})`);
const angClose = (name, a, b, tol) => { let d = Math.abs(((a - b) % 360 + 540) % 360 - 180); ok(name, d <= tol, `got ${a}, want ${b} (Δ${d.toFixed(4)}°)`); };

/* ---------------------------------------------------- 1. conventions */
{
  const p = S.poleFromDipAz(0, 0);
  ok('horizontal plane pole is up', Math.abs(p[0]) < 1e-12 && Math.abs(p[1]) < 1e-12 && Math.abs(p[2] - 1) < 1e-12);
  const q = S.poleFromDipAz(30, 0);
  // a plane dipping north faces north: its upward normal tilts north too
  close('dip 30 az 000 pole x', q[0], 0, 1e-12); close('dip 30 az 000 pole y', q[1], 0.5, 1e-12); close('dip 30 az 000 pole z', q[2], Math.cos(30 * Math.PI / 180), 1e-12);
  const e = S.poleFromDipAz(90, 90);
  close('vertical N-S plane dipping east has an east-pointing pole', e[0], 1, 1e-12);
  const zplane = S.poleFromDipAz(45, 0);
  ok('the pole is the gradient of the plane', Math.abs(zplane[1] - Math.SQRT1_2) < 1e-12 && Math.abs(zplane[2] - Math.SQRT1_2) < 1e-12);
  let worst = 0;
  for (let d = 0; d <= 90; d += 5) for (let a = 0; a < 360; a += 15) {
    const r = S.dipAzFromPole(S.poleFromDipAz(d, a));
    let da = Math.abs(r.dip - d); worst = Math.max(worst, da);
    if (d > 0.5 && d < 89.5) { let dd = Math.abs(((r.dip_azimuth - a) % 360 + 540) % 360 - 180); worst = Math.max(worst, dd); }
  }
  ok('pole <-> dip/dipaz round-trip', worst < 1e-8, 'worst ' + worst);
  const dv = S.dipVector(45, 90);
  close('dip vector points east and down', dv[0], Math.cos(45 * Math.PI / 180), 1e-12);
  close('dip vector z', dv[2], -Math.sin(45 * Math.PI / 180), 1e-12);
  ok('strike ⟂ dip', Math.abs(S.dot(S.strikeVector(90), S.dipVector(45, 90))) < 1e-12);
  ok('pole ⟂ strike', Math.abs(S.dot(S.poleFromDipAz(37, 213), S.strikeVector(213))) < 1e-12);
  ok('pole ⟂ dip vector', Math.abs(S.dot(S.poleFromDipAz(37, 213), S.dipVector(37, 213))) < 1e-12);
  const tp = S.trendPlunge(S.lineVector(120, 40));
  angClose('lineVector/trendPlunge trend', tp.trend, 120, 1e-8); close('lineVector/trendPlunge plunge', tp.plunge, 40, 1e-8);
}

/* ------------------------------------------------------ 2. fitPlane */
{
  const dip = 28, az = 115, n = S.poleFromDipAz(dip, az);
  const pts = []; const s = S.strikeVector(az), d = S.dipVector(dip, az);
  for (let i = 0; i < 40; i++) { const u = (Math.random() - 0.5) * 600, v = (Math.random() - 0.5) * 600; pts.push([1000 + u * s[0] + v * d[0], 2000 + u * s[1] + v * d[1], 500 + u * s[2] + v * d[2]]); }
  const f = S.fitPlane(pts);
  const r = S.dipAzFromPole(f.normal);
  close('fitPlane dip', r.dip, dip, 1e-6); angClose('fitPlane azimuth', r.dip_azimuth, az, 1e-5);
  ok('fitPlane rms ~ 0', f.rms < 1e-8, 'rms ' + f.rms);
  ok('fitPlane l1 >= l2 >= l3', f.l1 >= f.l2 && f.l2 >= f.l3);
}

/* ----------------------------------------- 3. derive from a trace */
{
  // a contact dipping 35 -> 070 cutting a synthetic ridge: the trace is the
  // set of points that lie in BOTH the plane and the topography.
  const dip = 35, az = 70, n = S.poleFromDipAz(dip, az);
  const ls = new GM.LineSet({ name: 'test contact', role: 'geology-outline' });
  // walk along strike, and let the plane's own z define elevation (trace lies in the plane)
  const s = S.strikeVector(az), d = S.dipVector(dip, az);
  const path = [];
  for (let i = 0; i <= 60; i++) {
    const u = -1200 + i * 40;                                  // along strike
    const v = 300 * Math.sin(i / 9);                            // wander down-dip: gives spread AND relief
    path.push([u * s[0] + v * d[0], u * s[1] + v * d[1], 800 + u * s[2] + v * d[2]]);
  }
  ls.addPolyline(path, { unit: 'Test unit' });
  const out = S.deriveFromTraces(ls, { window: 700, step: 350, min_relief: 20, min_spread: 25, max_rms: 5 });
  ok('derive produced measurements', out.n > 0, JSON.stringify(out.metadata.derived));
  let wd = 0, wa = 0;
  for (let i = 0; i < out.n; i++) { wd = Math.max(wd, Math.abs(out.attributes.dip[i] - dip)); wa = Math.max(wa, Math.abs(((out.attributes.dip_azimuth[i] - az) % 360 + 540) % 360 - 180)); }
  ok('derived dip matches the plane', wd < 0.01, 'worst Δdip ' + wd);
  ok('derived azimuth matches the plane', wa < 0.05, 'worst Δaz ' + wa);
  ok('derived points carry relief + rms', out.attributes.relief_m && out.attributes.fit_rms_m);
  ok('derived confidence is inferred', out.attributes.confidence[0] === 'inferred');
  ok('derived points carry the plan spread that made them resolvable', out.attributes.plan_spread_m && out.attributes.plan_spread_m.every(v => v >= 25));
  // a trace that is STRAIGHT IN MAP VIEW leaves the plane free to rotate about
  // it however much elevation it gains: the least-squares answer would be a
  // meaningless near-vertical plane, so it must be rejected instead
  const straight = new GM.LineSet({ name: 'straight in plan', role: 'geology-outline' });
  straight.addPolyline(Array.from({ length: 40 }, (_, i) => [i * 60, 0, 800 + i * 18]), {});
  const ns = S.deriveFromTraces(straight, { window: 500, step: 250, max_window: 2000 });
  ok('a trace straight in plan derives nothing', ns.n === 0, `got ${ns.n}, stats ${JSON.stringify(ns.metadata.derived)}`);
  ok('the rejection reason is spread, not relief', ns.metadata.derived.no_spread > 0 && ns.metadata.derived.no_relief === 0, JSON.stringify(ns.metadata.derived));
  // the same line with map-view wander is resolvable again
  const wandering = new GM.LineSet({ name: 'wandering', role: 'geology-outline' });
  const dp = 22, azp = 300, sp2 = S.strikeVector(azp), dp2 = S.dipVector(dp, azp);
  wandering.addPolyline(Array.from({ length: 60 }, (_, i) => { const u = i * 50, v = 260 * Math.sin(i / 7); return [u * sp2[0] + v * dp2[0], u * sp2[1] + v * dp2[1], 800 + u * sp2[2] + v * dp2[2]]; }), {});
  const nw = S.deriveFromTraces(wandering, { window: 500, step: 250, max_window: 2000 });
  ok('a wandering trace on the same plane resolves it', nw.n > 0 && Math.abs(nw.attributes.dip[0] - dp) < 0.05, `n=${nw.n} dip=${nw.n ? nw.attributes.dip[0] : '-'}`);

  // flat ground must yield nothing at all
  const flat = new GM.LineSet({ name: 'flat trace', role: 'geology-outline' });
  flat.addPolyline(Array.from({ length: 40 }, (_, i) => [i * 50, 200 * Math.sin(i / 6), 1000]), {});
  const nf = S.deriveFromTraces(flat, { window: 700, step: 350 });
  ok('flat ground derives nothing', nf.n === 0, 'got ' + nf.n);
  ok('flat ground warns', (nf.metadata.warnings || []).length > 0);
}

/* --------------------------------------------- 4. Set Elevation */
{
  const g = new GM.Grid2D({ name: 'topo', role: 'topography', nx: 11, ny: 11, x0: 0, y0: 0, dx: 100, dy: 100, values: new Float64Array(121).fill(0) });
  for (let j = 0; j < 11; j++) for (let i = 0; i < 11; i++) g.set(i, j, 500 + i * 10);
  const ps = S.newStructural('m');
  S.addMeasurement(ps, 250, 250, 0, 40, 90); S.addMeasurement(ps, 5000, 5000, 0, 20, 10);
  const r = S.setElevationFromGrid(ps, g);
  ok('set elevation moved the inside point', Math.abs(ps.xyz[2] - 525) < 1e-6, 'z=' + ps.xyz[2]);
  ok('set elevation reports the outside point', r.outside === 1 && r.moved === 1);
  ok('original z is kept', ps.attributes.z_original[0] === 0);
}

/* ---------------------------------------------- 5. stereonet math */
{
  for (const proj of ['equal_area', 'equal_angle']) {
    let worst = 0;
    for (let t = 0; t < 360; t += 17) for (let p = 1; p < 90; p += 7) {
      const v = S.lineVector(t, p), pr = S.projectVec(v, proj), back = S.unprojectDisc(pr.x, pr.y, proj);
      worst = Math.max(worst, S.axialAngle(v, back));
    }
    ok('stereonet round-trip ' + proj, worst < 1e-4, 'worst ' + worst + '°');
  }
  const c = S.projectVec([0, 0, -1]); close('vertical plots at the centre', Math.hypot(c.x, c.y), 0, 1e-12);
  const rim = S.projectVec([0, 1, 0]); close('horizontal plots on the rim', Math.hypot(rim.x, rim.y), 1, 1e-12);
  const north = S.projectVec(S.lineVector(0, 30)); ok('trend 000 plots north (+y)', north.y > 0 && Math.abs(north.x) < 1e-12);
  const east = S.projectVec(S.lineVector(90, 30)); ok('trend 090 plots east (+x)', east.x > 0 && Math.abs(east.y) < 1e-12);
  // great circle points must lie in the plane
  const gc = S.greatCircle(52, 137);
  let worstDot = 0; const pole = S.poleFromDipAz(52, 137);
  for (const part of gc) for (const [x, y] of part) { const v = S.unprojectDisc(x, y); worstDot = Math.max(worstDot, Math.abs(S.dot(v, pole))); }
  ok('great circle lies in its plane', worstDot < 1e-8, 'worst |n·v| ' + worstDot);
  const vert = S.greatCircle(90, 90);
  ok('vertical plane projects to a straight line', vert.flat().every(([x, y]) => Math.abs(x) < 1e-9));
  const horiz = S.greatCircle(0, 0);
  ok('horizontal plane projects to the rim circle', horiz.flat().every(([x, y]) => Math.abs(Math.hypot(x, y) - 1) < 1e-9));
  const sc = S.smallCircle([0, 0, -1], 30);
  ok('small circle about vertical is a concentric ring', sc.flat().every(([x, y]) => Math.abs(Math.hypot(x, y) - Math.hypot(sc[0][0][0], sc[0][0][1])) < 1e-9));
}

/* ------------------------------------------------- 6. statistics */
{
  // tight cluster of poles about a known plane
  const dip = 44, az = 310, truth = S.poleFromDipAz(dip, az);
  const N = 300, poles = new Float64Array(N * 3);
  let seed = 12345; const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  for (let i = 0; i < N; i++) { const d = dip + (rnd() - 0.5) * 6, a = az + (rnd() - 0.5) * 8; const p = S.poleFromDipAz(d, a); poles[3 * i] = p[0]; poles[3 * i + 1] = p[1]; poles[3 * i + 2] = p[2]; }
  const b = S.binghamStats(poles, N);
  close('Bingham mean plane dip', b.mean_plane.dip, dip, 0.6); angClose('Bingham mean plane azimuth', b.mean_plane.dip_azimuth, az, 0.6);
  ok('Bingham eigenvalues sum to 1', Math.abs(b.eigenvalues.reduce((a, c) => a + c, 0) - 1) < 1e-9);
  ok('Bingham calls a tight set a cluster', b.fabric.startsWith('cluster'), b.fabric);
  const f = S.fisherStats(poles, N);
  close('Fisher mean plane dip', f.mean_plane.dip, dip, 0.6);
  ok('Fisher kappa is large for a tight set', f.kappa > 100, 'kappa ' + f.kappa);
  ok('Fisher alpha95 is small', f.alpha95 < 2, 'a95 ' + f.alpha95);

  // girdle: poles of planes that all contain a common axis (a cylindrical fold)
  const hinge = S.lineVector(35, 12);
  const M = 200, gp = new Float64Array(M * 3);
  for (let i = 0; i < M; i++) {
    const t = (i / M) * Math.PI;
    const tmp = Math.abs(hinge[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
    const u = S.unit(S.cross(hinge, tmp)), w = S.cross(hinge, u);
    const v = S.unit([Math.cos(t) * u[0] + Math.sin(t) * w[0], Math.cos(t) * u[1] + Math.sin(t) * w[1], Math.cos(t) * u[2] + Math.sin(t) * w[2]]);
    gp[3 * i] = v[0]; gp[3 * i + 1] = v[1]; gp[3 * i + 2] = v[2];
  }
  const bg = S.binghamStats(gp, M);
  ok('girdle e3 recovers the fold hinge', S.axialAngle(bg.eigenvectors[2], hinge) < 0.5, S.axialAngle(bg.eigenvectors[2], hinge) + '°');
  angClose('fold hinge trend', bg.fold_hinge.trend, 35, 0.6); close('fold hinge plunge', bg.fold_hinge.plunge, 12, 0.6);
  ok('girdle is reported as a girdle', bg.fabric.startsWith('girdle'), bg.fabric);
}

/* ------------------------------------------------- 7. contouring */
{
  const N = 400, poles = new Float64Array(N * 3);
  let seed = 777; const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  for (let i = 0; i < N; i++) { const p = S.poleFromDipAz(20 + rnd() * 4, 100 + rnd() * 6); poles[3 * i] = p[0]; poles[3 * i + 1] = p[1]; poles[3 * i + 2] = p[2]; }
  for (const method of ['kamb', 'exp_kamb', 'schmidt']) {
    const dg = S.densityGrid(poles, N, { method, size: 96 });
    ok('density ' + method + ' has a peak', dg.max > 3, 'max ' + dg.max);
    // the peak must sit where the cluster is
    let bi = -1, bv = -1; for (let k = 0; k < dg.grid.length; k++) if (dg.grid[k] > bv) { bv = dg.grid[k]; bi = k; }
    const i = bi % dg.size, j = (bi / dg.size) | 0;
    const px = (i / (dg.size - 1)) * 2 - 1, py = (j / (dg.size - 1)) * 2 - 1;
    const v = S.unprojectDisc(px, py); const want = S.poleFromDipAz(22, 103);
    // the Kamb counting circle for n=400, sigma=3 is itself ~12 deg in radius,
    // so "within 15 deg of the cluster centre" is as tight as this can be
    ok('density ' + method + ' peaks on the cluster', S.axialAngle(v, want) < 15, S.axialAngle(v, want) + '°');
    const cl = S.contourLines(dg, [dg.max * 0.5]);
    ok('contourLines produces segments for ' + method, cl[0].segments.length > 4, cl[0].segments.length + ' segments');
  }
  const mask = S.desampleMask(poles, N, 0.5);
  ok('desample thins a tight cluster', mask.reduce((a, c) => a + c, 0) < N);
}

/* ----------------------------------------------- 8. declustering */
{
  const ps = S.newStructural('clustered');
  for (let i = 0; i < 12; i++) S.addMeasurement(ps, 100 + i * 0.5, 100, 100, 30 + (i % 3), 90 + (i % 3), { conf: i });
  for (let i = 0; i < 5; i++) S.addMeasurement(ps, 900 + i * 0.5, 900, 100, 60, 200);
  S.addMeasurement(ps, 5000, 5000, 100, 10, 10);
  const r = S.decluster(ps, { radius: 50, angular_tolerance: 30 });
  ok('decluster keeps one per cluster', r.ps.n === 3, 'kept ' + r.ps.n);
  ok('decluster reports removals', r.removed === 15, 'removed ' + r.removed);
  ok('declustered output is structural', r.ps.role === 'structural' && r.ps.attributes.dip.length === 3);
  // a cluster with no consistent orientation must be dropped entirely
  const noisy = S.newStructural('noisy');
  for (const [d, a] of [[10, 0], [80, 90], [45, 180], [70, 300], [20, 45], [88, 12]]) S.addMeasurement(noisy, 10, 10, 10, d, a);
  const rn = S.decluster(noisy, { radius: 100, angular_tolerance: 15 });
  ok('an inconsistent cluster is dropped whole', rn.ps.n === 0 && rn.noisy === 1, `n=${rn.ps.n} noisy=${rn.noisy}`);
  ok('dropping a noisy cluster is warned about', (rn.ps.metadata.warnings || []).length > 0);
}

/* ------------------------------------------- 9. form interpolant */
{
  // (a) flat-lying beds: every pole is up, so f must be a function of z alone
  const n = 20, pts = new Float64Array(n * 3), poles = new Float64Array(n * 3);
  let seed = 99; const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  for (let i = 0; i < n; i++) { pts[3 * i] = rnd() * 2000; pts[3 * i + 1] = rnd() * 2000; pts[3 * i + 2] = 500 + rnd() * 200; poles[3 * i + 2] = 1; }
  const fi = new S.FormInterpolant().fit(pts, poles);
  let worst = 0;
  for (let i = 0; i < n; i++) { const g = S.unit(fi.gradient(pts[3 * i], pts[3 * i + 1], pts[3 * i + 2])); worst = Math.max(worst, S.axialAngle(g, [0, 0, 1])); }
  ok('flat beds: gradient reproduced', worst < 1e-6, 'worst ' + worst + '°');
  const a = fi.value(0, 0, 100), b2 = fi.value(1500, 1900, 100);
  ok('flat beds: f is constant on a horizontal plane', Math.abs(a - b2) < 1e-6, `${a} vs ${b2}`);
  ok('flat beds: f increases upward', fi.value(0, 0, 200) > fi.value(0, 0, 100));

  // (b) a uniformly dipping set: level sets are the dipping planes
  const dip = 40, az = 215, want = S.poleFromDipAz(dip, az);
  const p2 = new Float64Array(n * 3), q2 = new Float64Array(n * 3);
  for (let i = 0; i < n; i++) { p2[3 * i] = rnd() * 3000; p2[3 * i + 1] = rnd() * 3000; p2[3 * i + 2] = rnd() * 800; q2[3 * i] = want[0]; q2[3 * i + 1] = want[1]; q2[3 * i + 2] = want[2]; }
  const fi2 = new S.FormInterpolant().fit(p2, q2);
  let w2 = 0; for (let i = 0; i < 40; i++) { const g = S.unit(fi2.gradient(rnd() * 3000, rnd() * 3000, rnd() * 800)); w2 = Math.max(w2, S.axialAngle(g, want)); }
  ok('uniform dip: gradient is constant everywhere', w2 < 1e-5, 'worst ' + w2 + '°');

  // (c) a cylindrical fold from varying poles: the fit must honour every datum
  const F = 60, p3 = new Float64Array(F * 3), q3 = new Float64Array(F * 3);
  for (let i = 0; i < F; i++) {
    const x = -1500 + (i % 12) * 250, y = -1000 + Math.floor(i / 12) * 500;
    const d = 45 * Math.sin(x / 900);                       // limbs dip alternately
    const pole = S.poleFromDipAz(Math.abs(d), d >= 0 ? 90 : 270);
    p3[3 * i] = x; p3[3 * i + 1] = y; p3[3 * i + 2] = 600 + 200 * Math.cos(x / 900);
    q3[3 * i] = pole[0]; q3[3 * i + 1] = pole[1]; q3[3 * i + 2] = pole[2];
  }
  const fi3 = new S.FormInterpolant().fit(p3, q3, { smoothing: 1e-8 });
  let w3 = 0; for (let i = 0; i < F; i++) { const g = S.unit(fi3.gradient(p3[3 * i], p3[3 * i + 1], p3[3 * i + 2])); w3 = Math.max(w3, S.axialAngle(g, [q3[3 * i], q3[3 * i + 1], q3[3 * i + 2]])); }
  ok('folded set: every measurement is honoured', w3 < 1e-4, 'worst ' + w3 + '°');
  ok('form interpolant serialises', S.FormInterpolant.fromJSON(JSON.parse(JSON.stringify(fi3.toJSON()))).value(0, 0, 600).toFixed(6) === fi3.value(0, 0, 600).toFixed(6));

  // (d) field + isosurface integration
  const bounds = [-1600, -1100, 300, 1400, 1100, 900];
  const ff = S.formField(fi3, bounds, 120);
  ok('formField centre is zero', Math.abs(ff.field[((ff.count[2] >> 1) * ff.count[1] * ff.count[0]) + ((ff.count[1] >> 1) * ff.count[0]) + (ff.count[0] >> 1)]) < Math.max(...ff.field.map(Math.abs)) * 0.35);
  const th = S.defaultThresholds(ff.field, 3);
  ok('defaultThresholds returns 3 ascending values', th.length === 3 && th[0] < th[1] && th[1] < th[2]);
  const mesh = E.isosurface(ff.field, ff.count, ff.origin, ff.spacing, { iso: th[1], name: 'form surface' });
  ok('form surface iso-surfaces', mesh.nTriangles > 50, mesh.nTriangles + ' triangles');
}

/* ---------------------------------------- 10. trends + anisotropy */
{
  const dip = 60, az = 120;
  const an = S.planeAnisotropy(dip, az, 0, [5, 5, 1], 100);
  const minor = [an._m[6], an._m[7], an._m[8]];
  ok('planeAnisotropy minor axis is the plane normal', S.axialAngle(minor, S.poleFromDipAz(dip, az)) < 1e-6, S.axialAngle(minor, S.poleFromDipAz(dip, az)) + '°');
  const major = [an._m[0], an._m[1], an._m[2]];
  ok('pitch 0 puts the major axis along strike', S.axialAngle(major, S.strikeVector(az)) < 1e-6, S.axialAngle(major, S.strikeVector(az)) + '°');
  const an2 = S.planeAnisotropy(dip, az, 90, [5, 5, 1], 100);
  const major2 = [an2._m[0], an2._m[1], an2._m[2]];
  ok('pitch 90 puts the major axis down dip', S.axialAngle(major2, S.dipVector(dip, az)) < 1e-6, S.axialAngle(major2, S.dipVector(dip, az)) + '°');
  const minor2 = [an2._m[6], an2._m[7], an2._m[8]];
  ok('pitch 90 keeps the minor axis normal to the plane', S.axialAngle(minor2, S.poleFromDipAz(dip, az)) < 1e-6);
  ok('ranges follow the ratios', Math.abs(an.ranges[0] - 500) < 1e-9 && Math.abs(an.ranges[2] - 100) < 1e-9, an.ranges.join(','));

  const ps = S.newStructural('trend input');
  S.addMeasurement(ps, 0, 0, 0, 30, 90); S.addMeasurement(ps, 1000, 0, 0, 30, 90);
  const tf = S.buildTrendField([ps], { strength: 5, range: 100 });
  const t0 = tf.at(0, 0, 0), t1 = tf.at(0, 0, -100), t2 = tf.at(0, 0, -200), t3 = tf.at(0, 0, -300);
  close('trend ratio on the input', t0.ratio, 5, 1e-9);
  close('trend ratio halves at one range', t1.ratio, 2.5, 1e-9);
  close('trend ratio halves again at two ranges', t2.ratio, 1.25, 1e-9);
  close('trend is isotropic by 3 ranges (floored at 1:1:1)', t3.ratio, 1, 1e-12);
  ok('trend is still anisotropic just inside 2 ranges', tf.at(0, 0, -190).ratio > 1.2, tf.at(0, 0, -190).ratio);
  close('trend plane matches the measurement', t0.dip, 30, 1e-9);
  const gl = S.trendGlyphs(tf, [-200, -200, -200, 1200, 200, 200], { n: 5 });
  ok('trend glyphs are produced', gl.n > 0, gl.n + ' glyphs');
  ok('trend glyphs carry strength', gl.attributes.strength.every(v => v >= 1));
  ok('trend field serialises', S.TrendField.fromJSON(JSON.parse(JSON.stringify(tf.toJSON()))).at(0, 0, 0).ratio === 5);
}

/* --------------------------------- 11. normalise + evaluate onto */
{
  const ps = new GM.PointSet({ name: 'raw', xyz: [0, 0, 0, 10, 10, 10] });
  ps.attributes.DIP = [30, 100]; ps.attributes.strike = [0, 90]; ps.attributes.younging = ['1', 'overturned'];
  S.normaliseStructural(ps);
  ok('normalise renames dip', ps.attributes.dip[0] === 30);
  ok('normalise derives dip_azimuth from strike', ps.attributes.dip_azimuth[0] === 90);
  ok('normalise folds an out-of-range dip', ps.attributes.dip[1] === 80 && ps.attributes.dip_azimuth[1] === 0, `${ps.attributes.dip[1]} / ${ps.attributes.dip_azimuth[1]}`);
  ok('normalise reads polarity words', ps.attributes.polarity[1] === -1);
  ok('normalise sets role + warns', ps.role === 'structural' && (ps.metadata.warnings || []).length > 0);

  const m = new GM.Mesh({ name: 'm', vertices: [0, 0, 0, 100, 0, 0, 0, 100, 0], triangles: [0, 1, 2] });
  S.evaluateOnto(m, (x, y, z) => x + y, 'test');
  ok('evaluateOnto writes mesh vertex attributes', m.attributes.test.values[1] === 100 && m.attributes.test.location === 'vertices');
  const pp = new GM.PointSet({ name: 'p', xyz: [1, 2, 3] });
  S.evaluateOnto(pp, (x, y, z) => x * y * z, 'v');
  ok('evaluateOnto writes point columns', pp.attributes.v[0] === 6);
}

/* --------------------------------------- 13. row removal (PointSet) */
{
  const ps = S.newStructural('rm');
  for (let i = 0; i < 5; i++) S.addMeasurement(ps, i, i * 10, i * 100, 10 + i, 100 + i, { confidence: i === 2 ? 'surveyed' : 'sketched', tag: 't' + i });
  ps.attributes.z_original = [1, 2];                       // a short column, as set-elevation can leave behind
  ok('removeRow drops one row', ps.removeRow(2) === 1 && ps.n === 4);
  ok('removeRow keeps xyz in step', ps.xyz[6] === 3 && ps.xyz[7] === 30 && ps.xyz[8] === 300);
  ok('removeRow shifts every column', ps.attributes.dip.join() === '10,11,13,14' && ps.attributes.tag.join() === 't0,t1,t3,t4' && ps.attributes.confidence.every(c => c === 'sketched'));
  ok('removeRow pads short columns to n first', Object.values(ps.attributes).every(c => c.length === ps.n) && ps.attributes.z_original.join() === '1,2,,');
  ok('removeRows drops several at once and ignores bad indices', ps.removeRows([0, 3, 99, -1, 0]) === 2 && ps.n === 2 && ps.attributes.dip.join() === '11,13');
  ok('removeRow ignores an out-of-range index', ps.removeRow(7) === 0 && ps.n === 2);
  ok('a removed row survives the JSON round-trip', GM.PointSet.fromJSON(JSON.parse(JSON.stringify(ps.toJSON()))).attributes.tag.join() === 't1,t3');
}

console.log(`\ngm-structural: ${pass} passed, ${fail} failed`);
if (fail) { console.log('\nFAILURES:'); for (const f of fails) console.log('  ✗ ' + f); process.exit(1); }
