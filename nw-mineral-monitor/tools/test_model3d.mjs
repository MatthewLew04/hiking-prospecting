#!/usr/bin/env node
/* Headless acceptance test for site/model3d.html (the 3-D geological modeller).
   - serves site/ with tools/range_server.py
   - stubs every external tile host with synthetic fixtures (the sandbox cannot
     reach them from page context; real browsers can): terrarium tiles encode a
     gentle slope around 1500 m, imagery tiles are flat colours
   - boots the viewer on a Cassia site, then drives the tools through the page
     API (window.gmApp) and checks state, not pixels (swiftshader paints black)
   Run: node tools/test_model3d.mjs  (exit code != 0 on failure) */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const F = await import(path.join(ROOT, 'site/assets/geomodel/gm-formats.js'));
const GM = await import(path.join(ROOT, 'site/assets/geomodel/gm-core.js'));
const PORT = 8765 + Math.floor(Math.random() * 200);
const results = []; let failed = 0;
function check(name, ok, detail = '') { results.push([ok ? 'PASS' : 'FAIL', name, detail]); if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); }

// synthetic terrarium tile: elevation = 1500 + 0.8*(px-128) per tile column (a slope), decoded as R*256+G+B/256-32768
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
// catch-all abort for external hosts FIRST (playwright runs handlers in reverse registration order)
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
  const summary = await page.evaluate(() => { const p = window.gmApp.project; return { name: p.name, crs: p.crs, n: p.objects.length, kinds: p.objects.map(o => o.kind + ':' + o.name), topo: (() => { const g = p.byKind('grid2d')[0]; return { nx: g.nx, ny: g.ny, z: g.zrange(), center: g.sample(p.origin[0], p.origin[1]) }; })(), layers: window.gmApp.R.layers.size, status: document.getElementById('status').textContent }; });
  check('boot: project built from URL params', summary.n >= 6, `${summary.n} objects, crs ${summary.crs.epsg}`);
  check('boot: topography sampled from terrarium fixture', summary.topo.z[0] > 1300 && summary.topo.z[1] < 1700 && summary.topo.nx > 50, `z ${summary.topo.z.map(v => v.toFixed(0))} nx ${summary.topo.nx}`);
  check('boot: geology meshes + mines present (faults only if mapped inside the box)', summary.kinds.some(k => k.startsWith('mesh:')) && summary.kinds.some(k => k.includes('Mines')), summary.kinds.slice(0, 8).join(' | '));
  check('render: one three.js group per object', summary.layers === summary.n, `${summary.layers} layers`);
  await page.waitForTimeout(1500);
  const imagery = await page.evaluate(() => window.gmApp.imageryTex ? 'sat texture' : 'none');
  check('imagery: satellite texture stitched from stubbed tiles', imagery === 'sat texture', imagery);

  // layer tree + inspector
  const tree = await page.evaluate(() => ({ rows: document.querySelectorAll('#layers .lrow').length, groups: document.querySelectorAll('#layers .lgroup').length }));
  check('ui: layer tree rendered', tree.rows >= 5 && tree.groups >= 4, JSON.stringify(tree));
  await page.evaluate(() => { const m = window.gmApp.project.byKind('points').find(p => p.role === 'mines'); window.gmApp.select(m.id); });
  const insp = await page.evaluate(() => document.getElementById('inspector').textContent.includes('POINTS'));
  check('ui: inspector shows selected layer', insp);

  // section tool
  await page.evaluate(() => { window.gmApp.tools.open('section'); window.gmApp.tools.section.preset('we'); });
  await page.waitForTimeout(400);
  const sec = await page.evaluate(() => { const t = window.gmApp.tools.section; t.slice = true; t.side = 1; t.update(); return { n: window.gmApp.project.byKind('section').length, products: t.products.map(p => p.kind), clip: window.gmApp.R.clip.active, planes: window.gmApp.R.clip.planes.length }; });
  check('section: W–E preset creates a section + products', sec.n >= 3 && sec.products.includes('line'), `${sec.products.length} products: ${[...new Set(sec.products)].join(',')}`);
  check('section: clipping plane active', sec.clip && sec.planes === 1);
  await page.evaluate(() => window.gmApp.tools.section.togglePanel());
  await page.waitForTimeout(300);
  const sec2d = await page.evaluate(() => { const c = document.getElementById('sec2dCanvas'); return { display: document.getElementById('sec2d').style.display, w: c.width, h: c.height }; });
  check('section: 2-D panel drawn', sec2d.display === 'flex' && sec2d.w > 100 && sec2d.h > 50, JSON.stringify(sec2d));

  // workings: programmatic adit + shaft + trace + stope, then summary + geojson
  const wk = await page.evaluate(async () => {
    const app = window.gmApp, E = await import('./assets/geomodel/gm-engine.js');
    // the workings layer is created on the first committed feature, never by
    // the site builder or by opening the panel
    const ws = app.tools.workings.ensureLayer(); const topo = app.topoGrid(); const o = app.project.origin;
    E.addAdit(ws, [o[0] - 200, o[1] - 100, 0], 45, 900, { gradePct: 0.5, unitsIn: 'ft', terrain: topo, name: 'No. 1 adit', confidence: 'sketched', source: { doc: 'USGS Bull 1', page: 12 } });
    E.addShaft(ws, [o[0], o[1], 0], 300, { dipDeg: 90, unitsIn: 'ft', terrain: topo, name: 'Main shaft' });
    E.addLevelWorking(ws, [[o[0] - 50, o[1]], [o[0] + 120, o[1] + 30], [o[0] + 200, o[1] - 40]], 1450, { kind: 'drift', name: '100 level', level: '100' });
    E.addRaise(ws, [o[0] + 120, o[1] + 30, 1450], [o[0] + 120, o[1] + 30, 1500], { name: 'raise 1' });
    const st = E.stopePrism([[o[0] + 100, o[1]], [o[0] + 160, o[1]], [o[0] + 160, o[1] + 40], [o[0] + 100, o[1] + 40]], 1450, 1480, { name: 'stope A' }); st.group = 'Workings'; app.project.add(st);
    app.refresh(ws);
    const s = E.workingsSummary(ws); const gj = E.workingsToGeoJSON(ws, app.project.crs);
    return { parts: ws.parts.length, total: s.total_m, types: Object.keys(s.by_type), gj: gj.features.length, lon: gj.features[0].geometry.coordinates[0][0], aditLen: ws.length(0), shaftDz: ws.partXYZ(1)[0][2] - ws.partXYZ(1)[1][2], stopeTris: st.nTriangles };
  });
  check('workings: adit/shaft/drift/raise/stope constructed', wk.parts === 4 && wk.stopeTris === 12, `${wk.parts} parts, ${wk.types.join('/')}, ${wk.total.toFixed(0)} m`);
  check('workings: feet converted to metres (900 ft adit ≈ 274 m, 300 ft shaft ≈ 91 m)', Math.abs(wk.aditLen - 274.3) < 1 && Math.abs(wk.shaftDz - 91.44) < 0.01, `adit ${wk.aditLen.toFixed(1)} shaft ${wk.shaftDz.toFixed(2)}`);
  check('workings: GeoJSON footprint in WGS84', wk.gj === 4 && wk.lon < -113 && wk.lon > -114, `lon ${wk.lon}`);

  // confidence has to be legible: a working read off a sentence must not be
  // drawn, counted or described the way a surveyed one is
  const conf = await page.evaluate(async () => {
    const app = window.gmApp, R = await import('./assets/geomodel/gm-render.js'), V = await import('./assets/geomodel/gm-viewer.js');
    const ws = app.project.byKind('lineset').find(l => l.role === 'workings');
    ws.features[1].confidence = 'surveyed';           // the Main shaft, off a plan
    ws.features[0].confidence = 'described';          // the adit, off a sentence
    app.refresh(ws); app.renderConfidence(); app.showLegend = true; app.select(ws.id); app.renderLegend();
    const L = app.R.layers.get(ws.id); const mats = [];
    L.group.traverse(n => { if (n.material) mats.push({ t: n.material.type, conf: n.userData.confidence || null, dash: n.material.dashSize || null, ld: !!(n.geometry.getAttribute && n.geometry.getAttribute('lineDistance')) }); });
    let pick = null; L.group.traverse(n => { if (!pick && n.type === 'LineSegments' && n.userData.segPart) pick = n; });
    return { tally: R.confidenceTally(app.project), mats,
             banner: document.getElementById('confbar').innerText,
             legend: document.getElementById('legend').innerText,
             pick: V.describePick({ obj: ws, object: pick, index: 0 }) };
  });
  check('confidence: tally counts described, surveyed and the assumed stope separately',
        conf.tally.surveyed === 1 && conf.tally.described > 0,
        JSON.stringify(conf.tally));
  check('confidence: a described working is drawn dashed, not solid',
        conf.mats.some(m => m.t === 'LineDashedMaterial' && m.conf === 'described' && m.dash > 0 && m.ld),
        JSON.stringify(conf.mats.filter(m => m.conf)));
  check('confidence: the viewport says the model is not a survey',
        /NOT A SURVEY/.test(conf.banner) && /digitised/.test(conf.banner), conf.banner.slice(0, 90));
  check('confidence: the legend decodes the line styles',
        /surveyed/.test(conf.legend) && /described/.test(conf.legend), conf.legend.replace(/\n/g, ' | ').slice(0, 90));
  check('provenance: picking a working shows the document it was read out of',
        conf.pick.some(l => l.startsWith('source:') && /USGS Bull 1/.test(l)),
        (conf.pick.find(l => l.startsWith('source:')) || conf.pick.join(' | ')).slice(0, 90));

  // image plane (plan) + trace through its georeference
  const ip = await page.evaluate(() => {
    const app = window.gmApp; const o = app.project.origin;
    const c = document.createElement('canvas'); c.width = 400; c.height = 300; const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0, 0, 400, 300); x.strokeStyle = '#000'; x.strokeRect(20, 20, 360, 260);
    const img = new (app.project.constructor.KINDS ? app.project.constructor.KINDS.imageplane : Object)({}); return null;
  }).catch(() => null);
  const ip2 = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'), E = await import('./assets/geomodel/gm-engine.js'); const o = app.project.origin;
    const c = document.createElement('canvas'); c.width = 400; c.height = 300; const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0, 0, 400, 300);
    const plan = new GM.ImagePlane({ image: c.toDataURL('image/png'), width: 400, height: 300, plane: 'plan', control: [[0, 0, o[0] - 200, o[1] + 150], [400, 0, o[0] + 200, o[1] + 150]], elevation: 1420, name: '200 level plan' }); plan.group = 'Images'; app.project.add(plan);
    const ws = app.project.byKind('lineset').find(l => l.role === 'workings'); const world = E.traceToWorld(plan, [[20, 20], [380, 20], [380, 280]], 1420); ws.addPolyline(world, E.workingFeature('drift', '200 level drift', { level: '200', level_z: 1420 })); app.refresh(ws);
    const corners = plan.corners(); return { corners: corners.map(q => q.map(v => +v.toFixed(1))), world: world.map(q => q.map(v => +v.toFixed(1))), layers: app.R.layers.has(plan.id) };
  });
  check('georef: plan image placed at level elevation, pixels map to world', ip2.layers && ip2.corners[0][2] === 1420 && Math.abs(ip2.world[1][0] - ip2.world[0][0] - 360) < 0.01, JSON.stringify(ip2.world));

  // stratigraphy: three units (erosion from digitised points, constant, basement)
  const strat = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'); const o = app.project.origin; const topo = app.topoGrid();
    const ps = new GM.PointSet({ name: 'contacts A', role: 'contacts', group: 'Stratigraphy' }); for (let i = 0; i < 40; i++) { const x = o[0] + (Math.random() - 0.5) * 2800, y = o[1] + (Math.random() - 0.5) * 2800; const t = topo.sample(x, y); ps.add(x, y, (t === t ? t : 1500) - 30 - 0.02 * (x - o[0]), { n: i }); } app.project.add(ps);
    const S = app.tools.strat.ensure(); S.units = [{ name: 'Alluvium', color: [222, 184, 135], contact: 'erosion', source: { kind: 'points', id: ps.id } }, { name: 'Tuff', color: [205, 133, 63], contact: 'deposit', source: { kind: 'const', value: 1380 } }, { name: 'Basement', color: [140, 140, 140], contact: 'deposit', source: { kind: 'none' } }];
    app.tools.strat.res = 40; app.tools.strat.method = 'rbf'; await app.tools.strat.build();
    const bases = app.project.byKind('grid2d').filter(g => g.role === 'contact' || (g.metadata && g.metadata.strat_of)); const vols = app.project.byKind('mesh').filter(m => m.role === 'unit');
    const E = await import('./assets/geomodel/gm-engine.js'); const grids = {}; for (const u of S.units) if (u.base) grids[u.base] = app.project.get(u.base); const col = E.columnAt(S, grids, o[0], o[1], topo);
    return { bases: bases.length, vols: vols.length, col: col.map(c => [c.name, c.top && +c.top.toFixed(1), c.base == null ? null : +c.base.toFixed(1)]), built: S.metadata.built };
  });
  check('stratigraphy: built 2 base surfaces + 3 unit volumes via worker', strat.bases === 2 && strat.vols === 3, JSON.stringify(strat.col));
  check('stratigraphy: column is monotonic (top ≥ base)', strat.col.every(c => c[2] == null || c[1] >= c[2] - 1e-6));
  await page.evaluate(() => window.gmApp.tools.section.update());
  const ribbons = await page.evaluate(() => window.gmApp.tools.section.products.filter(p => p.kind === 'ribbon').length);
  check('section: pancake ribbons appear in the section', ribbons === 3, `${ribbons} ribbons`);

  // block model + IDW + OK through the worker, tagging, grade-tonnage
  const bm = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'), E = await import('./assets/geomodel/gm-engine.js'); const o = app.project.origin;
    const samples = new GM.PointSet({ name: 'assays', role: 'samples', group: 'Imports' }); for (let i = 0; i < 150; i++) { const x = o[0] + (Math.random() - 0.5) * 1200, y = o[1] + (Math.random() - 0.5) * 1200, z = 1300 + Math.random() * 250; samples.add(x, y, z, { au: Math.max(0, 1 + 0.002 * (x - o[0]) + Math.random() * 0.4) }); } app.project.add(samples);
    const T = app.tools.blocks; T.params.samples = samples.id; T.params.value = 'au'; T.params.size = 50; T.params.zsize = 25; T.extent = 'samples'; T.create();
    await T.variogram();
    T.params.method = 'ok'; await T.run();
    const b = T.bm; const est = b.attributes.au_est.values; let n = 0, mn = Infinity, mx = -Infinity; for (const v of est) { if (v !== v) continue; n++; if (v < mn) mn = v; if (v > mx) mx = v; }
    await app.tools.strat.tagBlocks();
    const gt = E.gradeTonnage(b, 'au_est', [0.5, 1.5]);
    const L = app.R.layers.get(b.id);
    return { count: b.count, n, mn, mx, hasVar: !!b.attributes.au_var, vg: T.params, unit: !!b.attributes.unit, units: [...new Set(b.attributes.unit.values)].length, gt: gt.map(r => [r.cutoff, r.blocks]), shown: L && L.shownBlocks, meta: b.metadata.estimates && b.metadata.estimates[0].method };
  });
  check('blocks: block model created around samples + OK estimate written', bm.n > 100 && bm.hasVar && bm.meta === 'ok', `${bm.count.join('x')} blocks, ${bm.n} estimated, range ${bm.mn.toFixed(2)}–${bm.mx.toFixed(2)}`);
  check('blocks: auto-fitted variogram sensible', bm.vg.sill > 0 && bm.vg.range > 0, `nugget ${bm.vg.nugget} sill ${bm.vg.sill} range ${bm.vg.range}`);
  check('blocks: tagged with stratigraphic units + grade–tonnage', bm.unit && bm.units >= 2 && bm.gt[0][1] >= bm.gt[1][1], JSON.stringify(bm.gt));
  check('blocks: instanced rendering', bm.shown > 0, `${bm.shown} instances`);
  await page.evaluate(() => window.gmApp.tools.section.update());
  const bslice = await page.evaluate(() => window.gmApp.tools.section.products.filter(p => p.kind === 'blocks').length);
  check('section: block model sliced onto the plane', bslice >= 1);

  // implicit surface from signed-distance points (sphere)
  const imp = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'); const o = app.project.origin;
    const ps = new GM.PointSet({ name: 'shell pts', role: 'contacts' }); for (let i = 0; i < 120; i++) { const th = Math.random() * 6.283, ph = Math.acos(2 * Math.random() - 1); for (const [r, s] of [[80, 0], [100, 20], [60, -20]]) ps.add(o[0] + r * Math.sin(ph) * Math.cos(th), o[1] + r * Math.sin(ph) * Math.sin(th), 1400 + r * Math.cos(ph), { sd: s }); } app.project.add(ps);
    const T = app.tools.implicit; T.params.points = ps.id; T.params.valueCol = 'sd'; T.params.kernel = 'linear'; T.params.spacing = 8; await T.build();
    const m = app.project.byKind('mesh').find(x => x.name.includes('shell pts')); if (!m) return null; let r = 0; for (let i = 0; i < m.nVertices; i++) { const v = m.vertex(i); r += Math.hypot(v[0] - o[0], v[1] - o[1], v[2] - 1400); } return { tris: m.nTriangles, meanR: r / m.nVertices };
  });
  check('implicit: RBF iso-surface of a sphere (mean radius ≈ 80)', imp && imp.tris > 500 && Math.abs(imp.meanR - 80) < 4, imp ? `${imp.tris} tris, r̄ ${imp.meanR.toFixed(1)}` : 'no mesh');

  // export OMF v2 / v0.9 / project JSON / DXF in the page, validate with the JS readers here
  const exp = await page.evaluate(async () => {
    const app = window.gmApp, F = await import('./assets/geomodel/gm-formats.js');
    const o2 = await F.writeAs('omf2', app.project.objects, { name: app.project.name }); const o1 = await F.writeAs('omf1', app.project.objects, { name: app.project.name }); const dx = await F.writeAs('dxf', app.project.objects.filter(o => ['mesh', 'lineset', 'points'].includes(o.kind)), { basename: 't' });
    const b2 = Object.values(o2)[0], b1 = Object.values(o1)[0], bd = Object.values(dx)[0];
    const back = await F.readOmf2(b2); const back1 = await F.readOmf1(b1);
    return { omf2: b2.length, omf1: b1.length, dxf: bd.length, kinds2: back.objects.map(o => o.kind), kinds1: back1.objects.map(o => o.kind), json: app.project.serialize().length };
  });
  check('export: OMF v2 + v0.9 + DXF written in-page and re-read', exp.omf2 > 10000 && exp.omf1 > 10000 && exp.dxf > 10000 && exp.kinds2.includes('blockmodel') && exp.kinds1.includes('points'), `omf2 ${exp.omf2} B (${exp.kinds2.length} el), omf1 ${exp.omf1} B, dxf ${exp.dxf} B, json ${exp.json} B`);

  // import: a Surfer grid (property) + a CSV with lon/lat through the page importer
  const topoG = await page.evaluate(() => { const g = window.gmApp.topoGrid(); return { nx: g.nx, ny: g.ny, x0: g.x0, y0: g.y0, dx: g.dx }; });
  const mag = new GM.Grid2D({ nx: 40, ny: 40, x0: topoG.x0, y0: topoG.y0, dx: topoG.dx * topoG.nx / 40, dy: topoG.dx * topoG.ny / 40, name: 'mag' }); for (let j = 0; j < 40; j++) for (let i = 0; i < 40; i++) mag.values[j * 40 + i] = Math.sin(i / 5) * 100 + j;
  const grd = F.writeSurferGrd(mag, { fmt: 'dsrb' });
  fs.writeFileSync('/tmp/gm_test_mag.grd', grd);
  const csv = 'name,lon,lat,au_ozt\nTest A,-113.126,42.146,0.5\nTest B,-113.124,42.148,1.2\n'; fs.writeFileSync('/tmp/gm_test_pts.csv', csv);
  page.once('dialog', d => d.accept());
  const before = await page.evaluate(() => window.gmApp.project.objects.length);
  // grid import asks for the role through a modal -> click PROPERTY
  const [fileChooser] = await Promise.all([page.waitForEvent('filechooser'), page.click('#btnImport')]);
  await fileChooser.setFiles('/tmp/gm_test_mag.grd');
  await page.waitForSelector('.modal .b', { timeout: 15000 });
  await page.click('text=PROPERTY (geophysics, drape colours)');
  await page.waitForFunction(n => window.gmApp.project.objects.length > n, before, { timeout: 15000 });
  const gridImp = await page.evaluate(() => { const g = window.gmApp.project.byKind('grid2d').find(x => x.role === 'property'); const d = window.gmApp.display.get(g.id); return { name: g.name, role: g.role, mode: d.mode, layer: window.gmApp.R.layers.has(g.id) }; });
  check('import: Surfer 7 grid imported as draped property layer', gridImp.role === 'property' && gridImp.mode === 'draped' && gridImp.layer, JSON.stringify(gridImp));
  const [fc2] = await Promise.all([page.waitForEvent('filechooser'), page.click('#btnImport')]);
  await fc2.setFiles('/tmp/gm_test_pts.csv');
  await page.waitForSelector('.modal', { timeout: 15000 });
  await page.click('.modal-body button.b:has-text("IMPORT")');
  await page.waitForFunction(() => window.gmApp.project.byKind('points').some(p => p.name === 'gm_test_pts'), null, { timeout: 15000 });
  const csvImp = await page.evaluate(() => { const p = window.gmApp.project.byKind('points').find(x => x.name === 'gm_test_pts'); const o = window.gmApp.project.origin; return { n: p.n, e: p.xyz[0], dist: Math.hypot(p.xyz[0] - o[0], p.xyz[1] - o[1]), z: p.xyz[2], au: p.attributes.au_ozt }; });
  check('import: CSV lon/lat converted to UTM and draped on topography', csvImp.n === 2 && csvImp.dist < 500 && csvImp.z > 1300 && +csvImp.au[1] === 1.2, JSON.stringify(csvImp));

  // save + reload from IndexedDB
  await page.evaluate(() => window.gmApp.saveProject(true));
  await page.waitForTimeout(500);
  const key = await page.evaluate(() => window.gmApp.key);
  await page.goto(`${base}model3d.html?key=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 10, null, { timeout: 60000 });
  const reloaded = await page.evaluate(() => ({ n: window.gmApp.project.objects.length, ws: window.gmApp.project.byKind('lineset').find(l => l.role === 'workings').parts.length, bm: window.gmApp.project.byKind('blockmodel').length }));
  check('persistence: project restored from IndexedDB with workings + block model', reloaded.ws === 5 && reloaded.bm === 1, JSON.stringify(reloaded));

  // screenshot for the record
  await page.waitForTimeout(800);
  await page.screenshot({ path: '/tmp/model3d-test.png' });
  const pageErrors = errors.filter(e => !/WebGL|GPU|swiftshader|GL_|Automatic fallback|THREE\.WebGLRenderer|favicon|net::ERR_FAILED|Failed to load resource/i.test(e));
  check('no page errors', pageErrors.length === 0, pageErrors.slice(0, 5).join(' || '));
} catch (e) {
  check('run', false, String(e && e.stack || e));
} finally {
  await browser.close(); server.kill();
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
