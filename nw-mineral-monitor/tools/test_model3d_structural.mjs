#!/usr/bin/env node
/* Headless acceptance test for the structural tools in site/model3d.html.

   Boots the viewer with no site parameters, injects a synthetic project
   (topography + a mapped contact trace that lies exactly in a known plane),
   then drives the new tools through the page API and checks state — never
   pixels, because swiftshader paints raster tiles black.

   Run: node tools/test_model3d_structural.mjs
        CHROME_PATH=/opt/pw-browsers/chromium node tools/test_model3d_structural.mjs   */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = 8990 + Math.floor(Math.random() * 300);
let failed = 0;
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); };

const server = spawn('python3', ['tools/range_server.py', String(PORT)], { cwd: ROOT, stdio: 'ignore' });
await new Promise(r => setTimeout(r, 900));
const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || '/opt/pw-browsers/chromium',
  args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e.message || e)));
// the harness aborts every external host, so tile fetch failures are the
// test's own doing and are not page errors
page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|ERR_FAILED|ERR_ABORTED/.test(m.text())) errors.push('console: ' + m.text()); });
await page.route(u => /^https?:\/\//.test(u.href) && !u.href.includes(`:${PORT}`), r => r.abort());

const DIP = 32, AZ = 118;                       // the plane the synthetic trace lies in
const base = `http://localhost:${PORT}/`;
try {
  await page.goto(`${base}model3d.html`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => !!window.gmApp && !!window.gmApp.R, null, { timeout: 60000 });
  await page.evaluate(() => { document.querySelectorAll('.modal').forEach(m => m.remove()); });

  /* ---------- inject a synthetic project ---------- */
  const built = await page.evaluate(async ({ DIP, AZ }) => {
    const GM = await import('./assets/geomodel/gm-core.js');
    const S = await import('./assets/geomodel/gm-structural.js');
    const p = new GM.Project({ name: 'structural test', crs: GM.utm.crs(12, true), origin: [0, 0, 0] });
    // topography: a broad ridge so Set Elevation has something to sample
    const nx = 81, ny = 81, dx = 50;
    const g = new GM.Grid2D({ name: 'Topography', role: 'topography', nx, ny, x0: -2000, y0: -2000, dx, dy: dx, values: new Float64Array(nx * ny) });
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) { const x = -2000 + i * dx, y = -2000 + j * dx; g.set(i, j, 900 + 180 * Math.cos(x / 900) + 60 * Math.sin(y / 700)); }
    p.add(g);
    // a mapped contact whose 3-D trace lies exactly in the plane DIP/AZ
    const s = S.strikeVector(AZ), d = S.dipVector(DIP, AZ);
    const ls = new GM.LineSet({ name: 'Contact A/B outline', role: 'geology-outline', color: [40, 40, 40], group: 'Geology outlines' });
    const path = [];
    for (let i = 0; i <= 80; i++) {
      const u = -1600 + i * 40, v = 320 * Math.sin(i / 8);
      path.push([u * s[0] + v * d[0], u * s[1] + v * d[1], 900 + u * s[2] + v * d[2]]);
    }
    ls.addPolyline(path, { unit: 'Unit A' });
    p.add(ls);
    window.gmApp.setProject(p, 'structural-test');
    return { objects: p.objects.length, layers: window.gmApp.R.layers.size };
  }, { DIP, AZ });
  check('boot: synthetic project loaded', built.objects === 2 && built.layers === 2, JSON.stringify(built));

  /* ---------- derive orientations from the mapped trace ---------- */
  await page.evaluate(() => window.gmApp.tools.open('structure'));
  const hasPanel = await page.evaluate(() => !!document.querySelector('#inspector .tool h2') && document.querySelector('#inspector .tool h2').textContent.includes('STRUCTURAL'));
  check('ui: structural tool panel opens', hasPanel);

  await page.evaluate(() => window.gmApp.tools.structure.deriveAll());
  await page.waitForFunction(() => window.gmApp.project.objects.some(o => o.role === 'structural'), null, { timeout: 60000 });
  const der = await page.evaluate(() => {
    const o = window.gmApp.project.objects.find(x => x.role === 'structural');
    const dips = o.attributes.dip, azs = o.attributes.dip_azimuth;
    let wd = 0, wa = 0;
    for (let i = 0; i < o.n; i++) { wd = Math.max(wd, Math.abs(dips[i] - 32)); wa = Math.max(wa, Math.abs(((azs[i] - 118) % 360 + 540) % 360 - 180)); }
    const L = window.gmApp.R.layers.get(o.id);
    return { id: o.id, n: o.n, wd, wa, conf: o.attributes.confidence && o.attributes.confidence[0], relief: !!o.attributes.relief_m, rms: !!o.attributes.fit_rms_m, group: window.gmApp.project.objects.find(x => x.id === o.id).group, children: L ? L.group.children.length : 0, drawn: L ? L.drawn : null, prov: o.provenance.method };
  });
  check('derive: orientations produced from the map trace', der.n >= 5, `${der.n} measurements`);
  check('derive: dip matches the source plane', der.wd < 0.05, `worst Δdip ${der.wd.toFixed(4)}°`);
  check('derive: azimuth matches the source plane', der.wa < 0.1, `worst Δaz ${der.wa.toFixed(4)}°`);
  check('derive: provenance and per-point quality recorded', der.conf === 'inferred' && der.relief && der.rms, `${der.conf} · ${der.prov}`);
  check('render: structural glyphs built', der.children >= 2 && der.drawn === der.n, `${der.children} children, ${der.drawn} glyphs`);
  check('ui: derived layer lands in the Structure group', der.group === 'Structure', der.group);

  /* ---------- set elevation, decluster ---------- */
  const elev = await page.evaluate(id => {
    const app = window.gmApp, o = app.project.get(id);
    const before = o.xyz[2];
    app.tools.structure.layer = o; app.tools.structure.setElevation();
    return { before, after: o.xyz[2], kept: !!o.attributes.z_original, topo: app.topoGrid().sample(o.xyz[0], o.xyz[1]) };
  }, der.id);
  check('set elevation: measurements draped onto topography', Math.abs(elev.after - elev.topo) < 1e-6 && elev.kept, `${elev.before.toFixed(1)} → ${elev.after.toFixed(1)}`);

  const dec = await page.evaluate(async id => {
    const app = window.gmApp; app.tools.structure.layer = app.project.get(id);
    app.tools.structure.dc = { radius: 400, angular_tolerance: 30 };
    await app.tools.structure.decluster();
    const o = app.project.objects.find(x => x.role === 'structural' && x.name.includes('declustered'));
    return o ? { n: o.n, meta: o.metadata.declustered } : null;
  }, der.id);
  check('decluster: runs through the worker and thins the set', !!dec && dec.n > 0 && dec.n < der.n, dec ? `${dec.meta.input} → ${dec.n}` : 'no output');

  /* ---------- stereonet ---------- */
  await page.evaluate(id => { const app = window.gmApp; app.tools.open('stereonet', app.project.get(id)); }, der.id);
  await page.waitForTimeout(700);
  const st = await page.evaluate(() => {
    const t = window.gmApp.tools.stereonet, c = t.canvas;
    const ctx = c.getContext('2d'); const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let lit = 0; for (let i = 0; i < d.length; i += 4 * 97) if (d[i] + d[i + 1] + d[i + 2] > 60) lit++;
    return { rows: t.rows.length, lit, bing: t.stats && t.stats.bingham ? { dip: t.stats.bingham.mean_plane.dip, az: t.stats.bingham.mean_plane.dip_azimuth, fabric: t.stats.bingham.fabric } : null, fisher: !!(t.stats && t.stats.fisher), dg: t.dg ? { max: t.dg.max, method: t.dg.method } : null };
  });
  check('stereonet: net drawn', st.lit > 100, `${st.lit} lit samples`);
  check('stereonet: Bingham mean recovers the plane', st.bing && Math.abs(st.bing.dip - DIP) < 1 && Math.abs(((st.bing.az - AZ) % 360 + 540) % 360 - 180) < 1, st.bing ? `${st.bing.dip.toFixed(1)}° → ${st.bing.az.toFixed(1)}°` : 'none');
  check('stereonet: a single plane reads as a cluster', st.bing && st.bing.fabric.startsWith('cluster'), st.bing && st.bing.fabric);
  check('stereonet: Fisher statistics computed', st.fisher);
  check('stereonet: Kamb contours computed', !!st.dg && st.dg.max > 0, st.dg ? `${st.dg.method} peak ${st.dg.max.toFixed(1)}` : 'none');

  const svg = await page.evaluate(() => {
    const t = window.gmApp.tools.stereonet;
    let captured = null; const orig = URL.createObjectURL;
    URL.createObjectURL = b => { captured = b; return orig.call(URL, b); };
    t.exportSvg(); URL.createObjectURL = orig;
    return captured ? captured.size : 0;
  });
  check('stereonet: SVG export produces a file', svg > 2000, svg + ' bytes');

  /* ---------- selection round-trip ---------- */
  const selr = await page.evaluate(() => {
    const t = window.gmApp.tools.stereonet;
    t.picked = new Set([0, 1, 2]);
    const rows = t.rows.slice(0, 3);
    for (const r of rows) { const o = r.obj; if (!o.attributes.structural_set) o.attributes.structural_set = new Array(o.n).fill(null); o.attributes.structural_set[r.row] = 'Set 1'; }
    const o = rows[0].obj;
    const d = window.gmApp.display.get(o.id) || {}; d.attribute = 'structural_set'; window.gmApp.display.set(o.id, d); window.gmApp.refresh(o);
    const L = window.gmApp.R.layers.get(o.id);
    return { tagged: o.attributes.structural_set.filter(Boolean).length, cats: L.categories, colors: !!L.categoryColors };
  });
  check('selection: a category tags the source layer and colours the scene', selr.tagged === 3 && selr.cats && selr.cats.includes('Set 1') && selr.colors, JSON.stringify(selr.cats));

  /* ---------- form interpolant ---------- */
  await page.evaluate(id => { const app = window.gmApp; app.tools.open('form', app.project.get(id)); }, der.id);
  const form = await page.evaluate(async id => {
    const app = window.gmApp, t = app.tools.form;
    t.sel = new Set([id]);
    t.opt.thresholds = 3; t.opt.resolution = 90; t.opt.max_points = 120; t.opt.drape = true; t.opt.boundary = 'data';
    await t.build();
    const surf = app.project.objects.filter(o => o.kind === 'mesh' && o.metadata.form_of === 'form');
    const lines = app.project.objects.find(o => o.name.startsWith('Form lines'));
    return { n: surf.length, tris: surf.map(m => m.nTriangles), last: t.last, drape: lines ? { role: lines.role, nx: lines.nx } : null };
  }, der.id);
  check('form interpolant: surfaces built', form.n === 3 && form.tris.every(t => t > 20), `${form.n} surfaces, ${form.tris.join('/')} triangles`);
  check('form interpolant: every measurement honoured', form.last && form.last.meta.residual_max_deg < 0.5, form.last ? `max residual ${form.last.meta.residual_max_deg}°` : 'no report');
  check('form interpolant: form lines evaluated onto topography', !!form.drape && form.drape.role === 'property', JSON.stringify(form.drape));

  /* ---------- structural trend ---------- */
  const trend = await page.evaluate(async id => {
    const app = window.gmApp, t = app.tools.form;
    t.trend.sel = new Set([id]); t.trend.strength = 5; t.trend.range = 150;
    await t.buildTrend();
    const g = app.project.objects.find(o => o.role === 'trend');
    if (!g) return null;
    const L = app.R.layers.get(g.id);
    return { n: g.n, min: Math.min(...g.attributes.strength), max: Math.max(...g.attributes.strength), range: g.metadata.trend.range, children: L ? L.group.children.length : 0 };
  }, der.id);
  check('structural trend: glyph field built', !!trend && trend.n > 0 && trend.children > 0, trend ? `${trend.n} glyphs, strength ${trend.min.toFixed(2)}–${trend.max.toFixed(2)}` : 'none');
  check('structural trend: strength decays away from the input', !!trend && trend.max <= 5.0001 && trend.min >= 1, trend ? `${trend.min}–${trend.max}` : '');

  /* ---------- global trend from the mean plane ---------- */
  const gt = await page.evaluate(() => {
    const t = window.gmApp.tools.form; t.fromMean();
    return window.gmApp.project.metadata.global_trend;
  });
  check('global trend: taken from the Bingham mean plane', gt && Math.abs(gt.dip - DIP) < 1 && Math.abs(((gt.dip_azimuth - AZ) % 360 + 540) % 360 - 180) < 1, JSON.stringify(gt));

  /* ---------- orthographic + legend + scale bar ---------- */
  const proj = await page.evaluate(() => {
    window.gmApp.setProjection('ortho');
    const a = { ortho: !!window.gmApp.R.camera.isOrthographicCamera, mpp: window.gmApp.R.metresPerPixel() };
    window.gmApp.renderLegend();
    a.legend = document.querySelectorAll('#legend > div').length;
    a.scale = (document.querySelector('#legend .lgd-scale span') || {}).textContent;
    window.gmApp.setProjection('persp');
    a.back = !window.gmApp.R.camera.isOrthographicCamera;
    return a;
  });
  check('camera: orthographic toggles both ways', proj.ortho && proj.back, `mpp ${proj.mpp && proj.mpp.toFixed(3)}`);
  check('legend: scale bar rendered with a real distance', proj.legend >= 1 && /\d/.test(proj.scale || ''), proj.scale);

  const legendKey = await page.evaluate(id => { window.gmApp.select(id); window.gmApp.renderLegend(); return { rows: document.querySelectorAll('#legend .lgd-key .sw-row').length, ttl: (document.querySelector('#legend .lgd-key .ttl') || {}).textContent }; }, der.id);
  check('legend: colour key for the selected layer', legendKey.rows >= 1, JSON.stringify(legendKey));

  /* ---------- display settings survive a save/load ---------- */
  const persist = await page.evaluate(async id => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js');
    const d = app.display.get(id) || {}; d.colormap = 'magma'; d.sides = 3; d.attribute = 'dip_azimuth'; app.display.set(id, d); app.syncObject(app.project.get(id));
    await app.saveProject(true);
    const reloaded = await GM.store.loadProject('structural-test');
    app.setProject(reloaded, 'structural-test');
    const back = app.display.get(id);
    return back ? { colormap: back.colormap, sides: back.sides, attribute: back.attribute } : null;
  }, der.id);
  check('persistence: display settings reload with the project', persist && persist.colormap === 'magma' && persist.sides === 3 && persist.attribute === 'dip_azimuth', JSON.stringify(persist));

  const roundtrip = await page.evaluate(id => {
    const o = window.gmApp.project.get(id);
    return o ? { role: o.role, n: o.n, dip: o.attributes.dip[0], az: o.attributes.dip_azimuth[0], cols: Object.keys(o.attributes).length } : null;
  }, der.id);
  check('persistence: structural columns survive the round-trip', roundtrip && roundtrip.role === 'structural' && Math.abs(roundtrip.dip - DIP) < 0.1 && roundtrip.cols >= 6, JSON.stringify(roundtrip));

  /* ---------- exports still work ---------- */
  const exp = await page.evaluate(async () => {
    const F = await import('./assets/geomodel/gm-formats.js');
    const app = window.gmApp, o = app.project.objects.find(x => x.role === 'structural');
    const out = await F.writeAs('csv_structural', [o], { crs: app.project.crs });
    const name = Object.keys(out)[0];
    const text = typeof out[name] === 'string' ? out[name] : new TextDecoder().decode(out[name]);
    return { name, head: text.split('\n')[0], rows: text.trim().split('\n').length - 1 };
  });
  check('export: structural CSV round-trips through gm-formats', /dip/i.test(exp.head) && exp.rows > 3, `${exp.name}: ${exp.head}`);

  check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '));
} catch (e) {
  check('test harness', false, String(e && e.message || e));
  if (errors.length) console.log('page errors:\n  ' + errors.slice(0, 6).join('\n  '));
} finally {
  await browser.close();
  server.kill();
}
console.log(`\nmodel3d structural: ${failed ? failed + ' FAILED' : 'all checks passed'}`);
process.exit(failed ? 1 : 0);
