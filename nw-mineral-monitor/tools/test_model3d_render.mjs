#!/usr/bin/env node
/* Headless acceptance test for the renderer behind site/model3d.html
   (site/assets/geomodel/gm-render.js): tube picking, pick fidelity through
   see-through context, confidence-preserving opacity, screen-space dashes,
   lineset / mesh / section labels, label decluttering, build errors, thick
   slices and the idle render loop.
   Same harness as tools/test_model3d.mjs: serves site/ with
   tools/range_server.py, stubs every external tile host with synthetic
   fixtures, boots the Silver Hills site fresh, adds workings through the
   engine, then checks state — never pixels (swiftshader paints black).
   Run: node tools/test_model3d_render.mjs   (exit code != 0 on failure)
        CHROME_PATH=... to point at a Chromium that is not /opt/pw-browsers/chromium */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const F = await import(path.join(ROOT, 'site/assets/geomodel/gm-formats.js'));
const PORT = 9300 + Math.floor(Math.random() * 300);
const results = []; let failed = 0;
function check(name, ok, detail = '') { results.push([ok ? 'PASS' : 'FAIL', name, detail]); if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); }
const near = (a, b, tol) => Math.abs(a - b) <= tol;

async function terrariumTile() {
  const rgba = new Uint8Array(256 * 256 * 4);
  for (let y = 0; y < 256; y++) for (let x = 0; x < 256; x++) { const elev = 1500 + 0.8 * (x - 128) + 0.2 * (y - 128); const v = Math.round((elev + 32768) * 256); const o = (y * 256 + x) * 4; rgba[o] = (v >> 16) & 255; rgba[o + 1] = (v >> 8) & 255; rgba[o + 2] = v & 255; rgba[o + 3] = 255; }
  return F.encodePNG(256, 256, rgba, { channels: 4 });
}
async function flatTile(r, g, b) { const rgba = new Uint8Array(256 * 256 * 4); for (let i = 0; i < 256 * 256; i++) { rgba[4 * i] = r; rgba[4 * i + 1] = g; rgba[4 * i + 2] = b; rgba[4 * i + 3] = 255; } return F.encodePNG(256, 256, rgba, { channels: 4 }); }

const server = spawn('python3', ['tools/range_server.py', String(PORT)], { cwd: ROOT, stdio: 'ignore' });
await new Promise(r => setTimeout(r, 900));
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/opt/pw-browsers/chromium', args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = []; page.on('pageerror', e => errors.push(String(e.message || e))); page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
await page.route(u => /^https?:\/\//.test(u.href) && !u.href.startsWith(`http://localhost:${PORT}`) && !u.href.startsWith(`http://127.0.0.1:${PORT}`), r => r.abort());
const terr = await terrariumTile(), sat = await flatTile(60, 90, 50), topo = await flatTile(230, 220, 200), geo = await flatTile(200, 120, 80);
await page.route(/elevation-tiles-prod/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(terr), headers: { 'Access-Control-Allow-Origin': '*' } }));
await page.route(/arcgisonline/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(sat), headers: { 'Access-Control-Allow-Origin': '*' } }));
await page.route(/nationalmap\.gov/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(topo), headers: { 'Access-Control-Allow-Origin': '*' } }));
await page.route(/macrostrat/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(geo), headers: { 'Access-Control-Allow-Origin': '*' } }));

const base = `http://localhost:${PORT}/`;
try {
  await page.goto(`${base}model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills%20mine&gi=157&aoi=cassia&r=1500&fresh=1`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 3, null, { timeout: 120000 });
  await page.waitForTimeout(1500);

  // workings with every confidence class, plus two drifts sharing a midpoint
  // so the declutter pass has a deterministic collision to resolve
  const wk = await page.evaluate(async () => {
    const app = window.gmApp, E = await import('./assets/geomodel/gm-engine.js');
    const ws = app.project.byKind('lineset').find(l => l.role === 'workings') || app.tools.workings.ensureLayer(); const topo = app.topoGrid(); const o = app.project.origin;
    E.addAdit(ws, [o[0] - 200, o[1] - 100, 0], 45, 900, { gradePct: 0.5, unitsIn: 'ft', terrain: topo, name: 'No. 1 adit', confidence: 'described', source: { doc: 'USGS Bull 1', page: 12 } });
    E.addShaft(ws, [o[0], o[1], 0], 300, { dipDeg: 90, unitsIn: 'ft', terrain: topo, name: 'Main shaft' });
    E.addLevelWorking(ws, [[o[0] - 50, o[1]], [o[0] + 120, o[1] + 30], [o[0] + 200, o[1] - 40]], 1450, { kind: 'drift', name: '100 level', level: '100' });
    E.addLevelWorking(ws, [[o[0] + 300, o[1] + 200], [o[0] + 400, o[1] + 200]], 1450, { kind: 'drift', name: 'dup A', level: '100' });
    E.addLevelWorking(ws, [[o[0] + 350, o[1] + 150], [o[0] + 350, o[1] + 250]], 1450, { kind: 'drift', name: 'dup B', level: '100' });   // crosses dup A at its midpoint
    ws.features[0].confidence = 'described'; ws.features[1].confidence = 'surveyed'; ws.features[2].confidence = 'sketched'; ws.features[3].confidence = 'described'; ws.features[4].confidence = 'described';
    app.refresh(ws);
    return { parts: ws.parts.length, layer: app.R.layers.has(ws.id) };
  });
  check('setup: five workings drawn', wk.parts === 5 && wk.layer, `${wk.parts} parts`);

  /* ---------- 1. tube picking resolves to the feature ---------- */
  const tube = await page.evaluate(async () => {
    const app = window.gmApp, R = app.R, GMR = await import('./assets/geomodel/gm-render.js'), V = await import('./assets/geomodel/gm-viewer.js'); const THREE = GMR.THREE;
    const ws = app.project.byKind('lineset').find(l => l.role === 'workings'); const L = R.layers.get(ws.id);
    const meshes = []; L.group.traverse(n => { if (n.isMesh && n.userData.faceRanges) meshes.push(n); });
    const ranges = meshes.map(m => ({ conf: m.userData.confidence, faces: m.geometry.index.count / 3, ranges: m.userData.faceRanges.map(r => r.slice()) }));
    // every face of every batched mesh belongs to exactly one contiguous part range
    const contiguous = ranges.every(r => r.ranges.length && r.ranges[0][0] === 0 && r.ranges[r.ranges.length - 1][1] === r.faces && r.ranges.every((q, i) => i === 0 || q[0] === r.ranges[i - 1][1]));
    const partOf = (obj, face) => { for (const [a, b, k] of obj.userData.faceRanges) if (face >= a && face < b) return k; return -1; };
    // a ray through the centreline of every part (a quarter of the way along
    // its first segment, clear of the dup A / dup B crossing) must land on a
    // face of that part
    const resolved = [], described = [];
    for (let k = 0; k < ws.parts.length; k++) {
      const pts = ws.partXYZ(k); const m = [0, 1, 2].map(i => pts[0][i] + 0.25 * (pts[1][i] - pts[0][i]));
      const v = R.toScene(m[0], m[1], m[2]); v.y *= R.ve; const ndc = v.clone().project(R.camera);
      R.raycaster.setFromCamera(new THREE.Vector2(ndc.x, ndc.y), R.camera);
      const hits = R.raycaster.intersectObjects(meshes, false).filter(h => h.point.distanceTo(v) < 6);
      const h = hits[0]; resolved.push(h ? partOf(h.object, h.faceIndex) : null);
      described.push(h ? (V.describePick({ obj: ws, object: h.object, faceIndex: h.faceIndex, index: null }) || [])[0] : null);
    }
    return { n: meshes.length, ranges, contiguous, resolved, described };
  });
  check('tubes: batched meshes carry userData.faceRanges covering every face', tube.n >= 2 && tube.contiguous, JSON.stringify(tube.ranges.map(r => [r.conf, r.faces, r.ranges.length])));
  check('tubes: a raycast through each part resolves to that part', tube.resolved.every((k, i) => k === i), JSON.stringify(tube.resolved));
  check('tubes: describePick names the part from the face hit', tube.described.every((l, i) => l && l.endsWith(`part ${i}`)), JSON.stringify(tube.described));

  /* ---------- 2. pick fidelity: see-through context, pixel thresholds ---------- */
  const pk = await page.evaluate(async () => {
    const app = window.gmApp, R = app.R, V = await import('./assets/geomodel/gm-viewer.js');
    const ws = app.project.byKind('lineset').find(l => l.role === 'workings');
    const pts = ws.partXYZ(1); const m = [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2, (pts[0][2] + pts[1][2]) / 2];   // the shaft, 45 m under the collar
    const v = R.toScene(m[0], m[1], m[2]); v.y *= R.ve; const ndc = v.clone().project(R.camera);
    const r = R.canvas.getBoundingClientRect(); const cx = r.left + (ndc.x + 1) / 2 * r.width, cy = r.top + (1 - ndc.y) / 2 * r.height;
    const desc = p => p && p.obj ? { kind: p.obj.kind, role: p.obj.role, first: (V.describePick(p) || [])[0] } : null;
    const before = desc(R.pick(cx, cy));
    V.setSeeThrough(true); const after = desc(R.pick(cx, cy)); const opac = { topo: app.topoGrid().opacity }; V.setSeeThrough(false);
    const restored = desc(R.pick(cx, cy));
    const mpp = R.metresPerPixel();
    return { before, after, restored, opac, thr: [R.raycaster.params.Points.threshold, R.raycaster.params.Line.threshold], mpp };
  });
  check('pick: opaque terrain hides the shaft under it', pk.before && (pk.before.kind === 'grid2d' || pk.before.kind === 'mesh'), JSON.stringify(pk.before));
  check('pick: see-through terrain (0.55) and geology (0.4) yield to the shaft behind them', pk.after && pk.after.kind === 'lineset' && /part 1$/.test(pk.after.first || ''), JSON.stringify(pk.after));
  check('pick: opaque again after see-through is switched off', pk.restored && pk.restored.kind !== 'lineset', JSON.stringify(pk.restored));
  check('pick: raycaster thresholds are 6 px in metres at the target', near(pk.thr[0], 6 * pk.mpp, 1e-9) && near(pk.thr[1], 6 * pk.mpp, 1e-9), `threshold ${pk.thr[0].toFixed(3)} m, mpp ${pk.mpp.toFixed(3)}`);

  /* ---------- 3. opacity keeps confidence styling ---------- */
  const op = await page.evaluate(() => {
    const app = window.gmApp, R = app.R; const ws = app.project.byKind('lineset').find(l => l.role === 'workings'); const L = R.layers.get(ws.id);
    const tubes = {}; const dashes = []; L.group.traverse(n => { if (n.isMesh && n.userData.faceRanges) tubes[n.userData.confidence] = n.material; if (n.material && n.material.isLineDashedMaterial && n.userData.confidence) dashes.push({ conf: n.userData.confidence, depthTest: n.material.depthTest, base: n.material.userData.baseOpacity }); });
    const grab = () => Object.fromEntries(Object.entries(tubes).map(([k, m]) => [k, +m.opacity.toFixed(4)]));
    const base = Object.fromEntries(Object.entries(tubes).map(([k, m]) => [k, m.userData.baseOpacity]));
    const full0 = grab(); R.setOpacity(ws.id, 0.5); const half = grab(); R.setOpacity(ws.id, 1); const full = grab();
    return { base, full0, half, full, dashes };
  });
  check('opacity: tubes store baseOpacity (surveyed 1, described 0.72, assumed 0.5)', op.base.surveyed === 1 && op.base.described === 0.72 && op.base.assumed === 0.5, JSON.stringify(op.base));
  check('opacity: the slider multiplies the base instead of overwriting it', near(op.half.described, 0.36, 1e-6) && near(op.half.assumed, 0.25, 1e-6) && near(op.half.surveyed, 0.5, 1e-6) && near(op.full.described, 0.72, 1e-6), JSON.stringify(op.half) + ' → ' + JSON.stringify(op.full));
  check('opacity: dashed centrelines ignore depth so the dash shows through the tube', op.dashes.length >= 2 && op.dashes.every(d => d.depthTest === false), JSON.stringify(op.dashes));

  /* ---------- 4. dash patterns in screen space ---------- */
  const dash = await page.evaluate(async () => {
    const app = window.gmApp, R = app.R, GMR = await import('./assets/geomodel/gm-render.js'); const o = app.project.origin;
    const ws = app.project.byKind('lineset').find(l => l.role === 'workings'); const L = R.layers.get(ws.id);
    const dm = []; L.group.traverse(n => { if (n.material && n.material.isLineDashedMaterial && n.userData.confidence) dm.push(n); });
    const px = Object.fromEntries(GMR.CONF_CLASSES.map(c => [c.key, c.dash]));
    // the render loop rescales on its next frame; under swiftshader a frame
    // that recompiles shaders can take longer than any fixed sleep, so wait
    // for frames rather than milliseconds
    const frames = n => new Promise(r => { const f = () => (--n <= 0 ? r() : requestAnimationFrame(f)); requestAnimationFrame(f); });
    const mpp0 = R.metresPerPixel(); const d0 = dm.map(n => [n.userData.confidence, n.material.dashSize, n.material.gapSize]);
    R.fitTo([o[0] - 60, o[1] - 60, 1400, o[0] + 60, o[1] + 60, 1500]);
    await frames(3);
    const mpp1 = R.metresPerPixel(); const d1 = dm.map(n => [n.userData.confidence, n.material.dashSize, n.material.gapSize]);
    R.fitTo(app.project.bounds());
    await frames(3);
    const mpp2 = R.metresPerPixel(); const d2 = dm.map(n => [n.userData.confidence, n.material.dashSize, n.material.gapSize]);
    return { px, classes: GMR.CONF_CLASSES.map(c => [c.key, c.dash]), mpp0, d0, mpp1, d1, mpp2, d2 };
  });
  check('dash: CONF_CLASSES dash arrays are pixels the viewer still classifies (described > 5 → dashed, assumed dotted)', dash.px.described[0] > 5 && dash.px.assumed[0] <= 5 && dash.px.surveyed == null, JSON.stringify(dash.classes));
  check('dash: sizes are px × metres-per-pixel at build', dash.d0.length >= 2 && dash.d0.every(([c, ds, gs]) => near(ds / dash.mpp0, dash.px[c][0], 0.11 * dash.px[c][0]) && near(gs / dash.mpp0, dash.px[c][1], 0.11 * dash.px[c][1])), JSON.stringify(dash.d0.map(d => [d[0], +d[1].toFixed(2)])) + ` @ mpp ${dash.mpp0.toFixed(3)}`);
  check('dash: zooming in shrinks the metre dash to keep the pixel pattern', dash.mpp1 < dash.mpp0 / 3 && dash.d1.every(([c, ds, gs]) => near(ds, dash.px[c][0] * dash.mpp1, 0.02 * ds) && near(gs, dash.px[c][1] * dash.mpp1, 0.02 * gs)), JSON.stringify(dash.d1.map(d => [d[0], +d[1].toFixed(3)])) + ` @ mpp ${dash.mpp1.toFixed(3)}`);
  check('dash: zooming back out grows it again', dash.d2.every(([c, ds]) => near(ds, dash.px[c][0] * dash.mpp2, 0.02 * ds)), JSON.stringify(dash.d2.map(d => [d[0], +d[1].toFixed(2)])) + ` @ mpp ${dash.mpp2.toFixed(3)}`);

  /* ---------- 5 + 6. labels and decluttering ---------- */
  const lb = await page.evaluate(() => {
    const app = window.gmApp, R = app.R; const ws = app.project.byKind('lineset').find(l => l.role === 'workings');
    const sprites = L => { const out = []; L.group.traverse(n => { if (n.isSprite && n.userData.label) out.push({ text: n.userData.text, pr: n.userData.priority, part: n.userData.partIndex, visible: n.visible }); }); return out; };
    const d = app.display.get(ws.id) || {}; d.labels = true; d.labelField = 'name'; app.display.set(ws.id, d); app.syncObject(ws);
    let L = R.layers.get(ws.id); const byName = sprites(L); const counts = { labelled: L.labelled, total: L.labelTotal };
    d.labelField = 'confidence'; app.syncObject(ws); L = R.layers.get(ws.id); const byConf = sprites(L).map(s => s.text);
    d.labelField = 'name'; app.syncObject(ws); L = R.layers.get(ws.id);
    R.declutterLabels(true);
    const after = sprites(L); const dups = after.filter(s => /^dup /.test(s.text));
    let all = 0; for (const LL of R.layers.values()) if (LL.group.visible) LL.group.traverse(n => { if (n.isSprite && n.userData.label) all++; });
    const mines = app.project.byKind('points').find(p => p.role === 'mines'); const ML = mines && R.layers.get(mines.id); const mineSprites = ML ? sprites(ML) : [];
    const siteCol = mines && mines.attributes.is_site ? Array.from(mines.attributes.is_site).map(Number) : null;
    // one label on a mesh at its centre; a section carries its name at the top of the quad
    const mesh = app.project.byKind('mesh')[0]; const md = app.display.get(mesh.id) || {}; md.labels = true; app.display.set(mesh.id, md); app.syncObject(mesh); const meshL = sprites(R.layers.get(mesh.id));
    app.tools.open('section'); app.tools.section.preset('we'); const sec = app.tools.section.sec; const secL = sec ? sprites(R.layers.get(sec.id)) : [];
    return { byName, counts, byConf, dups, all, decluttered: R.labelsDecluttered, mineSprites: mineSprites.slice(0, 6), minePr: [...new Set(mineSprites.map(s => s.pr))], siteLabelled: siteCol ? mineSprites.some(s => s.pr === 3) : null, meshL, meshName: mesh.name, secL, secName: sec && sec.name };
  });
  check('labels: one per named part, at the midpoint, with the confidence spelled out', lb.byName.length === 5 && lb.counts.labelled === 5 && lb.counts.total === 5 && lb.byName.some(s => s.text === 'No. 1 adit (described)') && lb.byName.some(s => s.text === 'Main shaft') && lb.byName.some(s => s.text === '100 level (assumed)'), JSON.stringify(lb.byName.map(s => s.text)));
  check('labels: labelField=confidence shows the class itself', lb.byConf.includes('described') && lb.byConf.includes('surveyed') && lb.byConf.includes('assumed'), JSON.stringify(lb.byConf));
  check('labels: workings labels carry declutter priority 2', lb.byName.every(s => s.pr === 2), JSON.stringify(lb.byName.map(s => s.pr)));
  check('declutter: two labels at the same point → exactly one visible', lb.decluttered && lb.dups.length === 2 && lb.dups.filter(s => s.visible).length === 1, JSON.stringify(lb.dups) + ` (${lb.all} label sprites)`);
  check('declutter: the site label outranks the rest (is_site → 3)', lb.siteLabelled === null || lb.siteLabelled === true, lb.siteLabelled === null ? 'no is_site column on this site — skipped' : JSON.stringify(lb.minePr));
  check('labels: a mesh gets one label at its centre', lb.meshL.length === 1 && lb.meshL[0].text === lb.meshName.slice(0, 48), JSON.stringify(lb.meshL));
  check('labels: a section carries its name on the quad', lb.secL.length === 1 && lb.secL[0].text === String(lb.secName).slice(0, 48), JSON.stringify(lb.secL));

  /* ---------- 8. thick slice ---------- */
  const cl = await page.evaluate(async () => {
    const app = window.gmApp, R = app.R, GMR = await import('./assets/geomodel/gm-render.js'); const THREE = GMR.THREE;
    const pl = app.tools.section.plane(); if (!pl) return null;
    const scenePt = (p, s) => { const v = R.toScene(p[0] + pl.normal[0] * s, p[1] + pl.normal[1] * s, p[2] + pl.normal[2] * s); v.y *= R.ve; return v; };
    R.setClip(pl.point, pl.normal, 1, true, 40);
    const slab = R.clip.planes.map(p => p.clone());
    const inside = slab.every(p => p.distanceToPoint(scenePt(pl.point, 0)) >= -1e-6);
    const outside = slab.some(p => p.distanceToPoint(scenePt(pl.point, 30)) < 0) && slab.some(p => p.distanceToPoint(scenePt(pl.point, -30)) < 0);
    const near15 = slab.every(p => p.distanceToPoint(scenePt(pl.point, 15)) >= -1e-6) && slab.every(p => p.distanceToPoint(scenePt(pl.point, -15)) >= -1e-6);
    const dot = slab.length === 2 ? slab[0].normal.dot(slab[1].normal) : null;
    let pushed = 0; R.layers.get(app.project.byKind('lineset').find(l => l.role === 'workings').id).group.traverse(n => { if (n.material && n.userData.clippable !== false && n.material.clippingPlanes && n.material.clippingPlanes.length === 2) pushed++; });
    R.setClip(pl.point, pl.normal, 1, true); const single = R.clip.planes.length;
    R.setClip(pl.point, pl.normal, -1, true); const singleNeg = R.clip.planes.length;
    R.setClip(null); const cleared = R.clip.planes.length;
    return { n: slab.length, dot, inside, outside, near15, pushed, single, singleNeg, cleared, active: R.clip.active };
  });
  check('slice: a thickness makes two opposed clipping planes', cl && cl.n === 2 && near(cl.dot, -1, 1e-6), cl ? `${cl.n} planes, dot ${cl.dot}` : 'no section plane');
  check('slice: the 40 m slab keeps ±15 m and cuts ±30 m', cl && cl.inside && cl.near15 && cl.outside, JSON.stringify(cl && { inside: cl.inside, near15: cl.near15, outside: cl.outside }));
  check('slice: both planes reach the materials; single-plane callers unchanged', cl && cl.pushed > 0 && cl.single === 1 && cl.singleNeg === 1 && cl.cleared === 0 && !cl.active, JSON.stringify(cl && { pushed: cl.pushed, single: cl.single, cleared: cl.cleared }));

  /* ---------- 7. build errors surface ---------- */
  const be = await page.evaluate(() => {
    const app = window.gmApp, R = app.R; const mesh = app.project.byKind('mesh')[0];
    const orig = R.buildMesh; R.buildMesh = () => { throw new Error('synthetic build failure'); };
    app.syncObject(mesh); const L = R.layers.get(mesh.id); const broken = { err: L.buildError, empty: L.empty, children: L.group.children.length };
    R.buildMesh = orig; app.syncObject(mesh); const L2 = R.layers.get(mesh.id);
    return { broken, fixed: { err: L2.buildError, empty: L2.empty, children: L2.group.children.length } };
  });
  check('build error: a throwing builder leaves buildError + empty on the layer', be.broken.err === 'synthetic build failure' && be.broken.empty === true && be.broken.children === 0, JSON.stringify(be.broken));
  check('build error: cleared on the next successful build', be.fixed.err === null && be.fixed.empty === false && be.fixed.children > 0, JSON.stringify(be.fixed));

  /* ---------- 9. idle render loop ---------- */
  const idle = await page.evaluate(async () => {
    const R = window.gmApp.R; const prev = R.onRender; let n = 0; R.onRender = () => { n++; if (prev) prev(); };
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    await sleep(700); n = 0; await sleep(700); const quiet = n;
    R.invalidate(); await sleep(150); const afterInvalidate = n;
    R.controls.target.x += 5; R.invalidate(); await sleep(400); const afterMove = n;
    R.onRender = prev;
    return { quiet, afterInvalidate, afterMove };
  });
  check('loop: an untouched scene stops re-rendering', idle.quiet === 0, `${idle.quiet} renders in 700 ms idle`);
  check('loop: invalidate() renders once and onRender fires', idle.afterInvalidate >= 1 && idle.afterInvalidate <= 3, `${idle.afterInvalidate} renders`);
  check('loop: a camera move renders and settles', idle.afterMove >= idle.afterInvalidate + 1, `${idle.afterMove} renders total`);

  const pageErrors = errors.filter(e => !/WebGL|GPU|swiftshader|GL_|Automatic fallback|THREE\.WebGLRenderer|favicon|net::ERR_FAILED|Failed to load resource/i.test(e));
  check('no page errors', pageErrors.length === 0, pageErrors.slice(0, 5).join(' || '));
} catch (e) {
  check('run', false, String(e && e.stack || e));
} finally {
  await browser.close(); server.kill();
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
