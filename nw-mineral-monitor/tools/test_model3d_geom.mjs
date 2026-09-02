#!/usr/bin/env node
/* Headless acceptance test for the geometry tools of site/model3d.html
   (site/assets/geomodel/gm-geom-tools.js: project a trace down dip, contour a
   grid, plane from a measurement).
   - serves site/ with tools/range_server.py and stubs every external tile
     host with synthetic fixtures, exactly as tools/test_model3d.mjs does
   - boots the viewer on the Silver Hills site (&fresh=1), then drives the
     tools through window.gmApp and checks state, not pixels
   Run: CHROME_PATH=... node tools/test_model3d_geom.mjs   (exit code != 0 on failure) */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const F = await import(path.join(ROOT, 'site/assets/geomodel/gm-formats.js'));
const PORT = 8765 + Math.floor(Math.random() * 200);
const results = []; let failed = 0;
function check(name, ok, detail = '') { results.push([ok ? 'PASS' : 'FAIL', name, detail]); if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); }

// synthetic terrarium tile: a gentle slope around 1500 m (R*256+G+B/256-32768)
async function terrariumTile() {
  const rgba = new Uint8Array(256 * 256 * 4);
  for (let y = 0; y < 256; y++) for (let x = 0; x < 256; x++) { const elev = 1500 + 0.8 * (x - 128) + 0.2 * (y - 128); const v = Math.round((elev + 32768) * 256); const o = (y * 256 + x) * 4; rgba[o] = (v >> 16) & 255; rgba[o + 1] = (v >> 8) & 255; rgba[o + 2] = v & 255; rgba[o + 3] = 255; }
  return F.encodePNG(256, 256, rgba, { channels: 4 });
}
async function flatTile(r, g, b) { const rgba = new Uint8Array(256 * 256 * 4); for (let i = 0; i < 256 * 256; i++) { rgba[4 * i] = r; rgba[4 * i + 1] = g; rgba[4 * i + 2] = b; rgba[4 * i + 3] = 255; } return F.encodePNG(256, 256, rgba, { channels: 4 }); }

const server = spawn('python3', ['tools/range_server.py', String(PORT)], { cwd: ROOT, stdio: 'ignore' });
await new Promise(r => setTimeout(r, 900));
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/opt/pw-browsers/chromium', args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = []; page.on('pageerror', e => errors.push(String(e.message || e))); page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
await page.route(u => /^https?:\/\//.test(u.href) && !u.href.startsWith(`http://localhost:${PORT}`) && !u.href.startsWith(`http://127.0.0.1:${PORT}`), r => r.abort());
const terr = await terrariumTile(), sat = await flatTile(60, 90, 50), topoT = await flatTile(230, 220, 200), geo = await flatTile(200, 120, 80);
await page.route(/elevation-tiles-prod/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(terr), headers: { 'Access-Control-Allow-Origin': '*' } }));
await page.route(/arcgisonline/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(sat), headers: { 'Access-Control-Allow-Origin': '*' } }));
await page.route(/nationalmap\.gov/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(topoT), headers: { 'Access-Control-Allow-Origin': '*' } }));
await page.route(/macrostrat/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(geo), headers: { 'Access-Control-Allow-Origin': '*' } }));

const base = `http://localhost:${PORT}/`;
try {
  await page.goto(`${base}model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills%20mine&gi=157&aoi=cassia&r=1500&fresh=1`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 3, null, { timeout: 120000 });
  await page.waitForFunction(() => window.gmApp.tools && window.gmApp.tools.all && window.gmApp.tools.all.extrude, null, { timeout: 30000 });

  /* ---------- registration ---------- */
  const reg = await page.evaluate(() => { const T = window.gmApp.tools; return { keys: T.extra.map(x => x.key), labels: T.extra.map(x => x.label), all: ['extrude', 'contours', 'plane'].every(k => !!T.all[k]), once: T.extra.filter(x => x.key === 'extrude').length, same: T.all.extrude === T.extrude }; });
  check('install: extrude / contours / plane registered once each, with menu labels', reg.all && reg.once === 1 && reg.same && reg.labels.includes('Project a trace down dip') && reg.labels.includes('Plane from a measurement') && reg.keys.includes('contours'), reg.keys.join(','));

  /* ---------- a draped geology-outline trace to project ---------- */
  // The site's own outlines are closed rings (a capped prism, no strike gate),
  // so the open trace the strike refusal needs is synthesised here, draped on
  // the stubbed terrain and running roughly N-S; a site outline is projected
  // too, further down, when the boot produced one.
  const src = await page.evaluate(async () => {
    const app = window.gmApp, P = app.project, topo = app.topoGrid(), o = P.origin;
    const GM = await import('./assets/geomodel/gm-core.js'), E = await import('./assets/geomodel/gm-engine.js');
    const ls = new GM.LineSet({ name: 'Contact outline (synthetic, draped)', role: 'geology-outline', color: [40, 40, 40], group: 'Geology outlines' });
    const path = [];
    for (let i = 0; i <= 30; i++) { const x = o[0] + 120 * Math.sin(i / 5), y = o[1] - 600 + i * 40; const z = topo.sample(x, y); path.push([x, y, z === z ? z : 1500]); }
    ls.addPolyline(path, { unit: 'Synthetic unit', confidence: 'sketched' });
    P.add(ls);
    const xyz = ls.partXYZ(0);
    return { id: ls.id, name: ls.name, part: 0, n: xyz.length, strike: E.polylineStrike(xyz), zmin: Math.min(...xyz.map(p => p[2])), zmax: Math.max(...xyz.map(p => p[2])), rendered: !!app.R.layers.get(ls.id) };
  });
  check('fixture: an open geology-outline trace draped on the stubbed terrain', src.rendered && src.n === 31 && src.zmax > src.zmin && src.zmin > 1000, `strike ${src.strike.toFixed(1)}°, z ${src.zmin.toFixed(0)}–${src.zmax.toFixed(0)}`);

  /* ---------- ExtrudeTool ---------- */
  await page.evaluate(() => window.gmApp.tools.open('extrude'));
  const panel = await page.evaluate(() => { const host = document.getElementById('toolhost'); const ttl = host && host.querySelector('.ttl'); return { open: !!host && !host.hidden, title: ttl && ttl.textContent, body: document.getElementById('toolbody').textContent }; });
  check('extrude: panel opens in the tool host and says the depth is a projection distance, not a fact', panel.open && /PROJECT A TRACE DOWN DIP/.test(panel.title || '') && /projection distance/.test(panel.body) && /never a modelled fact/.test(panel.body), panel.title);

  const ex = await page.evaluate(async ({ id, part, strike }) => {
    const app = window.gmApp, t = app.tools.extrude;
    t.layer = app.project.get(id); t.setPart(part); t.src = 'guess'; t.form.dip = 60; t.form.dipaz = (strike + 90) % 360; t.form.depth = 200; t.form.role = 'fault'; t.form.name = '';
    const before = app.project.objects.length;
    const m = await t.build();
    if (!m) return { error: t.error };
    const L = app.R.layers.get(m.id);
    return { added: app.project.objects.length - before, name: m.name, role: m.role, group: m.group, tris: m.nTriangles, verts: m.nVertices, rendered: !!L, error: t.error,
             meta: { dip: m.metadata.dip, az: m.metadata.dip_azimuth, depth: m.metadata.depth_m, conf: m.metadata.confidence, dipConf: m.metadata.dip_confidence, traceConf: m.metadata.trace_confidence, schema: m.metadata.schema, derived_from: m.metadata.derived_from, note: m.metadata.note, source: m.metadata.source },
             bottomBelow: (() => { let ok = true; for (let i = 0; i < m.nVertices / 2; i++) if (!(m.vertices[3 * i + 2] < m.vertices[3 * (i + m.nVertices / 2) + 2])) ok = false; return ok; })(),
             panel: document.getElementById('toolbody').textContent };
  }, src);
  check('extrude: a typed guess builds a ribbon from the draped outline part', !ex.error && ex.added === 1 && ex.tris === 2 * (src.n - 1) && ex.verts === 2 * src.n && ex.rendered && ex.bottomBelow, ex.error || `${ex.name}: ${ex.tris} tris`);
  check('extrude: metadata carries dip / dip_azimuth / depth, confidence "assumed" for a typed guess', !!ex.meta && ex.meta.dip === 60 && Math.abs(ex.meta.az - (src.strike + 90) % 360) < 1e-9 && ex.meta.depth === 200 && ex.meta.conf === 'assumed' && ex.meta.dipConf === 'assumed' && ex.meta.schema === 'nwmm-extrude/1', JSON.stringify(ex.meta && { dip: ex.meta.dip, az: ex.meta.az, depth: ex.meta.depth, conf: ex.meta.conf }));
  check('extrude: group Surfaces, derived_from = [trace layer], source part + dip origin recorded, honesty note', !!ex.meta && ex.group === 'Surfaces' && ex.meta.derived_from && ex.meta.derived_from[0] === src.id && ex.meta.source.part === src.part && ex.meta.source.layer_id === src.id && /typed guess/.test(ex.meta.source.dip_from) && /projection distance/.test(ex.meta.note), ex.meta && ex.meta.source.dip_from);
  check('extrude: the panel reports the last build and its confidence', /last built/.test(ex.panel || '') && /assumed/.test(ex.panel || ''));

  const st = await page.evaluate(async () => { const t = window.gmApp.tools.extrude; t.src = 'stated'; t.form.doc = 'USGS Bull. 1'; t.form.page = 'p. 12'; t.form.dip = 45; const m = await t.build(); return m ? { conf: m.metadata.confidence, dipConf: m.metadata.dip_confidence, doc: m.metadata.source_doc, how: m.metadata.source.dip_from, trace: m.metadata.trace_confidence } : { error: t.error }; });
  check('extrude: a stated dip is "described" (weaker than the sketched trace) and cites its document', st.conf === 'described' && st.dipConf === 'described' && st.trace === 'sketched' && st.doc && st.doc.doc === 'USGS Bull. 1' && /stated in USGS Bull\. 1, p\. 12/.test(st.how || ''), JSON.stringify(st));

  const bad = await page.evaluate(async ({ strike }) => { const app = window.gmApp, t = app.tools.extrude; t.src = 'guess'; t.form.dipaz = strike; t.form.dip = 60; const before = app.project.objects.length; const m = await t.build(); return { m: !!m, error: t.error, added: app.project.objects.length - before, panel: document.getElementById('toolbody').textContent }; }, src);
  check('extrude: a dip azimuth along the trace\'s strike is refused and nothing is added', !bad.m && bad.added === 0 && /within 20°/.test(bad.error || ''), bad.error);
  check('extrude: the refusal prints the strike and is shown verbatim in the panel', !!bad.error && bad.error.includes(src.strike.toFixed(1)) && bad.panel.includes(bad.error), `strike ${src.strike.toFixed(1)}°`);

  const flat = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'); const o = app.project.origin;
    const ls = new GM.LineSet({ name: 'Plan-view trace (no elevation)', role: 'lines', group: 'Geology outlines' }); ls.addPolyline([[o[0], o[1], 0], [o[0] + 200, o[1] + 50, 0], [o[0] + 400, o[1] + 30, 0]], {}); app.project.add(ls);
    const t = app.tools.extrude; t.layer = ls; t.setPart(0); t.src = 'guess'; t.form.dip = 60; t.form.dipaz = 0; const m = await t.build();
    return { m: !!m, error: t.error, panel: document.getElementById('toolbody').textContent };
  });
  check('extrude: a trace without elevation is refused with the drape hint, verbatim', !flat.m && flat.error === 'the trace has no elevation — drape it on the topography first' && flat.panel.includes(flat.error), flat.error);

  const wall = await page.evaluate(async ({ id, part, strike }) => { const app = window.gmApp, t = app.tools.extrude; t.layer = app.project.get(id); t.setPart(part); t.form.depth = 150; const m = await t.build(true); return m ? { dip: m.metadata.dip, az: m.metadata.dip_azimuth, conf: m.metadata.confidence, how: m.metadata.source.dip_from, vertical: (() => { const n = m.nVertices / 2; for (let i = 0; i < n; i++) if (Math.abs(m.vertices[3 * i] - m.vertices[3 * (i + n)]) > 1e-9 || Math.abs(m.vertices[3 * i + 2] - (m.vertices[3 * (i + n) + 2] - 150)) > 1e-9) return false; return true; })() } : { error: t.error }; }, src);
  check('extrude: VERTICAL WALL = dip 90, azimuth strike + 90, confidence assumed, straight down 150 m', wall.dip === 90 && Math.abs(wall.az - (src.strike + 90) % 360) < 1e-9 && wall.conf === 'assumed' && wall.vertical && /vertical wall/.test(wall.how || ''), wall.error || JSON.stringify(wall));

  const nd = await page.evaluate(() => { const t = window.gmApp.tools.extrude; t.src = 'derived'; t.derived = null; const r = t.derive(); return { r, error: t.error, panel: document.getElementById('toolbody').textContent }; });
  check('extrude: "derived along this part" with no readings refuses with a message, in the panel', nd.r === null && /no structural readings were derived along part 0/.test(nd.error || '') && nd.panel.includes(nd.error), nd.error);

  const dv = await page.evaluate(async ({ id, part }) => {
    const app = window.gmApp, S = await import('./assets/geomodel/gm-structural.js'); const ls = app.project.get(id);
    const out = S.deriveFromTraces(ls, { window: 200, step: 100, max_window: 800, min_relief: 3, min_spread: 10, max_rms: 60, min_points: 4 });
    if (!out.n) return { n: 0, stats: out.metadata.derived };
    out.group = 'Structure'; app.project.add(out);
    const t = app.tools.extrude; t.layer = ls; t.setPart(part); t.src = 'derived'; t.derived = null; const d = t.derive();
    const m = d ? await t.build() : null;
    return { n: out.n, d: d && { n: d.n, dip: d.dip, az: d.dip_azimuth }, conf: m && m.metadata.confidence, dipConf: m && m.metadata.dip_confidence, how: m && m.metadata.source.dip_from, mdip: m && m.metadata.dip, error: t.error, sourceCol: out.attributes.source && out.attributes.source[0], partCol: out.attributes.part && out.attributes.part[0] };
  }, src);
  check('extrude: readings derived along the part (source + part columns) give a Bingham-mean dip at confidence "inferred"', dv.n > 0 && dv.d && dv.d.n === dv.n && dv.conf === 'inferred' && dv.dipConf === 'inferred' && /Bingham mean/.test(dv.how || '') && Math.abs(dv.mdip - dv.d.dip) < 1e-9 && dv.sourceCol === src.name && dv.partCol === 0, dv.n ? JSON.stringify({ readings: dv.n, dip: dv.d && +dv.d.dip.toFixed(1), az: dv.d && +dv.d.az.toFixed(1), err: dv.error }) : 'no window derivable: ' + JSON.stringify(dv.stats));

  // picking through the renderer: the tool's own pick filter + segPart mapping
  await page.evaluate(({ id }) => { const app = window.gmApp, ls = app.project.get(id), t = app.tools.extrude; t.layer = ls; t.part = null; t.pickPart(); app.R.fitTo(ls.bounds()); }, src);
  await page.waitForTimeout(400);                       // a frame, so the camera matrices are current
  const pick = await page.evaluate(({ id, part }) => {
    const app = window.gmApp, R = app.R, ls = app.project.get(id), t = app.tools.extrude;
    const armed = !!app.tools.armed && app.tools.armed.text.includes('PICK');
    const xyz = ls.partXYZ(part); const q = xyz[15];
    const v = R.toScene(q[0], q[1], q[2]); v.y *= R.ve; v.project(R.camera);
    const r = R.canvas.getBoundingClientRect();
    const cx = (v.x * 0.5 + 0.5) * r.width + r.left, cy = (-v.y * 0.5 + 0.5) * r.height + r.top;
    const hit = R.pick(cx, cy, o => o.id === id);
    const handled = t.onClick({ clientX: cx, clientY: cy });
    return { armed, hit: !!hit, handled, part: t.part, disarmed: !app.tools.armed, mode: t.mode };
  }, src);
  check('extrude: PICK arms the tool and a click on the trace in the scene selects its part and disarms', pick.armed && pick.hit && pick.handled && pick.part === src.part && pick.disarmed && pick.mode === null, JSON.stringify(pick));

  const site = await page.evaluate(async () => {
    const app = window.gmApp, ls = app.project.byKind('lineset').find(l => l.role === 'geology-outline' && !/synthetic/i.test(l.name) && l.parts.some(p => p.length >= 4));
    if (!ls) return null;
    const k = ls.parts.findIndex(p => p.length >= 4); const xyz = ls.partXYZ(k);
    if (!xyz.some(p => p[2] === p[2] && p[2] !== 0)) return { name: ls.name, noZ: true };
    const t = app.tools.extrude; t.layer = ls; t.setPart(k); t.src = 'guess'; t.form.dip = 50; t.form.dipaz = 0; t.form.depth = 100; const m = await t.build();
    return { name: ls.name, k, n: xyz.length, closed: xyz[0][0] === xyz[xyz.length - 1][0] && xyz[0][1] === xyz[xyz.length - 1][1], built: !!m, tris: m && m.nTriangles, capped: m && m.metadata.closed, error: t.error };
  });
  check('extrude: a site geology outline from the boot projects too (closed rings become capped prisms)', site == null || (site.built && site.tris > 0 && (!site.closed || site.capped)), site ? (site.error || `${site.name} part ${site.k}: ${site.n} vertices, ${site.tris} tris${site.capped ? ', capped' : ''}`) : 'no site outline in this box — skipped');

  /* ---------- ContourTool ---------- */
  const ct = await page.evaluate(async () => {
    const app = window.gmApp; app.tools.open('contours'); const t = app.tools.contours; const topo = app.topoGrid();
    const def = t.defaultInterval(topo);
    t.grid = topo; t.interval = 20; t.index = 5; t.base = 0;
    const before = app.project.objects.length;
    const ls = await t.build();
    if (!ls) return { error: t.error };
    const zr = topo.zrange(); const expected = []; for (let m = Math.ceil(zr[0] / 20); m * 20 <= zr[1] + 1e-9; m++) expected.push(m * 20);
    const levels = [...new Set(ls.features.map(f => f.level))].sort((a, b) => a - b);
    return { added: app.project.objects.length - before, parts: ls.parts.length, role: ls.role, group: ls.group, levels, drawn: expected.filter(l => l > zr[0]), zr, def, name: ls.name, units: ls.features[0] && ls.features[0].units, source: ls.features[0] && ls.features[0].source,
             derived_from: ls.metadata.derived_from, topoId: topo.id, topoName: topo.name, nIndex: ls.features.filter(f => f.index).length, nLevels: ls.metadata.contours.n_levels,
             zOk: ls.parts.every((p, k) => p.every(i => Math.abs(ls.vertices[3 * i + 2] - ls.features[k].level) < 1e-9)), rendered: !!app.R.layers.get(ls.id), panel: document.getElementById('toolbody').textContent };
  });
  check('contours: the topography contoured at 20 m yields parts at the requested levels (all multiples of 20 inside the range)', !ct.error && ct.added === 1 && ct.parts > 0 && ct.role === 'contours' && ct.levels.length > 0 && ct.levels.every(l => Math.abs(l / 20 - Math.round(l / 20)) < 1e-9 && l >= ct.zr[0] && l <= ct.zr[1]) && ct.drawn.every(l => ct.levels.includes(l)), ct.error || `${ct.parts} parts, ${ct.levels.length} levels ${ct.levels[0]}…${ct.levels[ct.levels.length - 1]} in ${ct.zr.map(v => v.toFixed(0)).join('–')}`);
  check('contours: lines sit at their level, every 5th is an index contour, features carry units + source', ct.zOk && ct.nIndex > 0 && ct.nIndex < ct.parts && ct.units === 'm' && ct.source === ct.topoName, `${ct.nIndex} index parts of ${ct.parts}`);
  check('contours: group Surfaces, derived_from the grid, rendered, panel reports it', ct.group === 'Surfaces' && ct.derived_from && ct.derived_from[0] === ct.topoId && ct.rendered && /last built/.test(ct.panel), ct.name);
  check('contours: the default interval is a 1-2-5 step near range / 8', /^[125](0*)(\.0*)?$/.test(String(ct.def)) && ct.def >= (ct.zr[1] - ct.zr[0]) / 16 && ct.def <= (ct.zr[1] - ct.zr[0]) / 4, `default ${ct.def} for a ${(ct.zr[1] - ct.zr[0]).toFixed(0)} m range`);

  const cp = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'); const topo = app.topoGrid();
    const mag = new GM.Grid2D({ nx: 30, ny: 30, x0: topo.x0, y0: topo.y0, dx: topo.dx * (topo.nx - 1) / 29, dy: topo.dy * (topo.ny - 1) / 29, name: 'mag', role: 'property', units: 'nT', group: 'Imports' });
    for (let j = 0; j < 30; j++) for (let i = 0; i < 30; i++) mag.values[j * 30 + i] = 100 * Math.sin(i / 4) + 3 * j;
    app.project.add(mag);
    const t = app.tools.contours; t.grid = mag; t.interval = 25; t.drape = true; t.lift = 2; t.index = 0;
    const ls = await t.build();
    if (!ls) return { error: t.error };
    let worst = 0; for (let i = 0; i < ls.nVertices; i++) { const g = topo.sample(ls.vertices[3 * i], ls.vertices[3 * i + 1]); worst = Math.max(worst, Math.abs(ls.vertices[3 * i + 2] - (g + 2))); }
    return { parts: ls.parts.length, worst, units: ls.features[0] && ls.features[0].units, derived_from: ls.metadata.derived_from, ids: [mag.id, topo.id], levels: [...new Set(ls.features.map(f => f.level))].length, noIndex: ls.features.every(f => f.index === false) };
  });
  check('contours: a property grid is draped on the topography 2 m up, units nT, derived_from grid + topography', !cp.error && cp.parts > 0 && cp.worst < 1e-6 && cp.units === 'nT' && cp.derived_from[0] === cp.ids[0] && cp.derived_from[1] === cp.ids[1] && cp.noIndex, cp.error || `${cp.parts} parts, ${cp.levels} levels, worst lift error ${cp.worst.toExponential(1)}`);

  /* ---------- PlaneTool ---------- */
  const pl = await page.evaluate(async () => {
    const app = window.gmApp, S = await import('./assets/geomodel/gm-structural.js'); app.tools.open('plane'); const t = app.tools.plane;
    const o = app.project.origin, topo = app.topoGrid(); const z = topo.sample(o[0], o[1]);
    const ps = S.newStructural('Field readings'); S.addMeasurement(ps, o[0] + 50, o[1] - 30, z, 42, 135, { polarity: 1, confidence: 'sketched', source: 'field notebook p.3', type: 'vein' }); ps.group = 'Structure'; app.project.add(ps);
    const panel0 = document.getElementById('toolbody').textContent;
    t.setFrom(ps, 0); t.form.halfStrike = 150; t.form.halfDip = 100; t.form.role = 'vein'; t.form.name = '';
    const before = app.project.objects.length;
    const m = t.build();
    if (!m) return { error: t.error };
    const added = app.project.objects.length - before;
    const v = m.vertices; const e1 = [v[3] - v[0], v[4] - v[1], v[5] - v[2]], e2 = [v[9] - v[0], v[10] - v[1], v[11] - v[2]];
    const ang = S.axialAngle(S.unit(S.cross(e1, e2)), S.poleFromDipAz(42, 135));
    const centre = [(v[0] + v[6]) / 2, (v[1] + v[7]) / 2, (v[2] + v[8]) / 2], p0 = ps.point(0);
    const halfS = Math.hypot(v[3] - v[0], v[4] - v[1], v[5] - v[2]) / 2, halfD = Math.hypot(v[9] - v[0], v[10] - v[1], v[11] - v[2]) / 2;
    t.from = null; t.at = [o[0] - 200, o[1] + 100, z]; t.form.dip = 70; t.form.dipaz = 20; t.form.role = 'fault'; t.form.confidence = 'assumed'; t.form.doc = 'a guess'; const m2 = t.build();
    return { added, tris: m.nTriangles, verts: m.nVertices, role: m.role, group: m.group, opacity: m.opacity, ang, halfS, halfD, centreErr: Math.hypot(centre[0] - p0[0], centre[1] - p0[1], centre[2] - p0[2]), rendered: !!app.R.layers.get(m.id),
             meta: { dip: m.metadata.dip, az: m.metadata.dip_azimuth, conf: m.metadata.confidence, schema: m.metadata.schema, note: m.metadata.note, fm: m.metadata.from_measurement, source: m.metadata.source, derived_from: m.metadata.derived_from }, psId: ps.id,
             m2: m2 ? { tris: m2.nTriangles, role: m2.role, conf: m2.metadata.confidence, fm: m2.metadata.from_measurement, source: m2.metadata.source } : { error: t.error }, panel0, panel: document.getElementById('toolbody').textContent };
  });
  check('plane: a 2-triangle mesh with role vein, opacity 0.35, in Surfaces, rendered', !pl.error && pl.added === 1 && pl.tris === 2 && pl.verts === 4 && pl.role === 'vein' && pl.opacity === 0.35 && pl.group === 'Surfaces' && pl.rendered, pl.error || `${pl.tris} tris`);
  check('plane: through the measurement, at its attitude, 150 m along strike and 100 m down dip', pl.ang < 1e-6 && pl.centreErr < 1e-6 && Math.abs(pl.halfS - 150) < 1e-6 && Math.abs(pl.halfD - 100) < 1e-6, `pole off by ${pl.ang && pl.ang.toExponential(1)}°`);
  check('plane: provenance from the measurement\'s columns (source, confidence), derived_from the layer', pl.meta && pl.meta.dip === 42 && pl.meta.az === 135 && pl.meta.conf === 'sketched' && pl.meta.source === 'field notebook p.3' && pl.meta.fm && pl.meta.fm.row === 0 && pl.meta.fm.layer_id === pl.psId && pl.meta.fm.edited === false && pl.meta.derived_from[0] === pl.psId && pl.meta.schema === 'nwmm-assay-vein/1', JSON.stringify(pl.meta && { conf: pl.meta.conf, source: pl.meta.source }));
  check('plane: the panel and the metadata say "a statement of attitude, not a modelled surface"', /statement of attitude, not a modelled surface/.test(pl.panel0) && pl.meta.note === 'statement of attitude, not a modelled surface');
  check('plane: a typed attitude at a ground point builds a fault plane at the typed confidence with its document', pl.m2 && pl.m2.tris === 2 && pl.m2.role === 'fault' && pl.m2.conf === 'assumed' && pl.m2.fm === null && pl.m2.source && pl.m2.source.doc === 'a guess', JSON.stringify(pl.m2));

  await page.evaluate(({ psId }) => { const app = window.gmApp, ps = app.project.get(psId), t = app.tools.plane; t.from = null; t.at = null; t.arm('measure'); app.R.fitTo([ps.xyz[0] - 80, ps.xyz[1] - 80, ps.xyz[2] - 80, ps.xyz[0] + 80, ps.xyz[1] + 80, ps.xyz[2] + 80]); }, { psId: pl.psId });
  await page.waitForTimeout(400);
  const pickM = await page.evaluate(({ psId }) => {
    const app = window.gmApp, R = app.R, ps = app.project.get(psId), t = app.tools.plane;
    const armed = !!app.tools.armed;
    const v = R.toScene(ps.xyz[0], ps.xyz[1], ps.xyz[2]); v.y *= R.ve; v.project(R.camera);
    const r = R.canvas.getBoundingClientRect();
    const cx = (v.x * 0.5 + 0.5) * r.width + r.left, cy = (-v.y * 0.5 + 0.5) * r.height + r.top;
    const hit = R.pick(cx, cy, o => o.kind === 'points' && o.role === 'structural');
    const handled = t.onClick({ clientX: cx, clientY: cy });
    return { armed, hit: !!hit, handled, from: t.from ? { id: t.from.layer.id, row: t.from.row } : null, dip: t.form.dip, disarmed: !app.tools.armed };
  }, { psId: pl.psId });
  check('plane: PICK A MEASUREMENT arms, a click on the glyph in the scene takes the row, its attitude, and disarms', pickM.armed && pickM.hit && pickM.handled && pickM.from && pickM.from.id === pl.psId && pickM.from.row === 0 && pickM.dip === 42 && pickM.disarmed, JSON.stringify(pickM));

  /* ---------- the products survive a save + reload ---------- */
  await page.evaluate(() => window.gmApp.saveProject(true));
  await page.waitForTimeout(500);
  const key = await page.evaluate(() => window.gmApp.key);
  await page.goto(`${base}model3d.html?key=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 10, null, { timeout: 60000 });
  const back = await page.evaluate(() => { const P = window.gmApp.project; const ex = P.byKind('mesh').filter(m => m.metadata.schema === 'nwmm-extrude/1'), pl = P.byKind('mesh').filter(m => m.metadata.schema === 'nwmm-assay-vein/1'), ct = P.byKind('lineset').filter(l => l.role === 'contours'); return { ex: ex.length, exConf: ex.map(m => m.metadata.confidence), pl: pl.length, ct: ct.length, ctLevels: ct[0] && ct[0].features.filter(f => f.level != null).length, tools: !!(window.gmApp.tools.all.extrude && window.gmApp.tools.all.contours && window.gmApp.tools.all.plane) }; });
  check('persistence: projected surfaces, planes and contours reload with their metadata; tools reinstalled', back.ex >= 4 && back.exConf.includes('assumed') && back.exConf.includes('described') && back.pl === 2 && back.ct === 2 && back.ctLevels > 0 && back.tools, JSON.stringify(back));

  const pageErrors = errors.filter(e => !/WebGL|GPU|swiftshader|GL_|Automatic fallback|THREE\.WebGLRenderer|favicon|net::ERR_FAILED|Failed to load resource|ERR_ABORTED/i.test(e));
  check('no page errors', pageErrors.length === 0, pageErrors.slice(0, 5).join(' || '));
} catch (e) {
  check('run', false, String(e && e.stack || e));
  if (errors.length) console.log('page errors:\n  ' + errors.slice(0, 6).join('\n  '));
} finally {
  await browser.close(); server.kill();
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
