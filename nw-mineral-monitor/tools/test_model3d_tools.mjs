#!/usr/bin/env node
/* Headless acceptance test for the tool improvements in site/model3d.html:
   section PNG / SVG export, thick slice, section images, the pancake
   builder's trace source + input classification, the implicit surface's
   bounds / clipping / trend, the truthful sample pickers, and the three
   tools of gm-more-tools.js (measure, notes, trace).
   Same harness as tools/test_model3d.mjs: serves site/ with range_server.py,
   stubs every external tile host, boots the Silver Hills site with &fresh=1,
   drives the tools through window.gmApp and checks state, not pixels.
   Run: node tools/test_model3d_tools.mjs  (exit code != 0 on failure) */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const F = await import(path.join(ROOT, 'site/assets/geomodel/gm-formats.js'));
const PORT = 8600 + Math.floor(Math.random() * 150);
const results = []; let failed = 0;
function check(name, ok, detail = '') { results.push([ok ? 'PASS' : 'FAIL', name, detail]); if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); }

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
const NOT_A_SURVEY = 'NOT A SURVEY — described geometry drawn dashed, assumed dotted';
try {
  await page.goto(`${base}model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills%20mine&gi=157&aoi=cassia&r=1500&fresh=1`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 3, null, { timeout: 120000 });
  await page.waitForTimeout(1200);
  const boot = await page.evaluate(async () => {
    const M = await import('./assets/geomodel/gm-more-tools.js');   // self-installs into window.gmApp.tools
    const t = window.gmApp.tools; return { n: window.gmApp.project.objects.length, tools: ['measure', 'notes', 'polyline'].map(k => !!t.all[k] && typeof t.all[k].panel === 'function' && typeof t.all[k].purpose === 'string'), extra: t.extra.map(x => x.key), exported: typeof M.installMoreTools === 'function' };
  });
  check('boot: site built and gm-more-tools self-installed three tools', boot.n >= 6 && boot.tools.every(Boolean) && boot.exported, `${boot.n} objects · extra tools ${boot.extra.join(',')}`);

  /* ---------------- section: a described working, PNG + SVG export ---------------- */
  const sec = await page.evaluate(async () => {
    const app = window.gmApp, E = await import('./assets/geomodel/gm-engine.js'), R = await import('./assets/geomodel/gm-render.js'); const o = app.project.origin;
    const dash = R.CONF_CLASSES.find(c => c.key === 'described').dash.join(' ');   // the export reads the dash from CONF_CLASSES, so the check does too
    const ws = app.tools.workings.ensureLayer();
    E.addLevelWorking(ws, [[o[0] - 120, o[1] - 60], [o[0], o[1]], [o[0] + 120, o[1] + 60]], 1450, { kind: 'drift', name: 'No. 2 level drift', level: '200', confidence: 'described', source: { doc: 'USGS Bull 1', page: 12 } });
    const k = ws.parts.length - 1; ws.features[k].confidence = 'described'; ws.features[k].source = { doc: 'USGS Bull 1', page: 12 }; app.refresh(ws);
    app.tools.open('section'); const t = app.tools.section; t.create([o[0] - 1500, o[1]], [o[0] + 1500, o[1]], 'Test W-E'); t.band = 25; t.update();
    t.setPanel(true); t.draw2d();
    const png = await t.exportPng({ download: false }); const svg = await t.exportSvg({ download: false });
    const K = t.keyModel(); const or = t.offsetRange(); const b = app.project.bounds();
    return { near: t.products.filter(p => p.kind === 'near').length, pngW: png.width, pngH: png.height, svgLen: svg.length, svgHasName: svg.includes('Test W-E'), svgDash: svg.includes(`stroke-dasharray="${dash}"`), dash, svgIsSvg: svg.startsWith('<?xml') && svg.includes('<svg') && svg.trimEnd().endsWith('</svg>'), key: { conf: K.conf, types: K.types.map(t => t.type), sources: K.sources, notSurvey: K.notSurvey }, range: or, ext: Math.max(b[3] - b[0], b[4] - b[1]), sentence: svg.includes('NOT A SURVEY — described geometry drawn dashed, assumed dotted'), panel: (() => { const c = document.getElementById('sec2dCanvas'); return c.width > 100 && c.height > 50; })() };
  });
  check('section: the described drift is projected onto the section', sec.near >= 1 && sec.key.conf.described >= 1 && sec.key.types.includes('drift'), JSON.stringify(sec.key));
  check('section: PNG export is a 2× canvas of the laid-out size', sec.pngW === 2400 && sec.pngH >= 1200 && sec.pngH % 2 === 0, `${sec.pngW}×${sec.pngH}`);
  check('section: SVG export names the section and carries the NOT A SURVEY sentence', sec.svgIsSvg && sec.svgHasName && sec.sentence, `${sec.svgLen} chars`);
  check('section: described working drawn dashed in the vector export, source listed in the key', sec.svgDash && sec.key.sources.some(s => /USGS Bull 1/.test(s)) && sec.key.notSurvey, `dash "${sec.dash}" · ${sec.key.sources.join(' | ')}`);
  check('section: offset slider range comes from the project extent (±half, 0.5 % steps)', Math.abs(sec.range.max - Math.ceil(sec.ext / 2)) <= 1 && sec.range.min === -sec.range.max && Math.abs(sec.range.step - +(sec.ext * 0.005).toPrecision(2)) < 1e-9, JSON.stringify(sec.range));
  check('section: 2-D panel still paints through the shared routine', sec.panel);

  /* ---------------- thick slice: setClip called with the slab thickness ---------------- */
  const thick = await page.evaluate(() => {
    const app = window.gmApp, t = app.tools.section, R = app.R; const calls = []; const orig = R.setClip;
    R.setClip = function (...a) { calls.push(a.map(v => Array.isArray(v) ? v.length : v)); return orig.apply(this, a); };
    try { t.band = 30; t.setClipMode('band'); const band = calls[calls.length - 1]; t.setClipMode('1'); const front = calls[calls.length - 1]; t.setClipMode('0'); const off = calls[calls.length - 1]; return { band, front, off, mode: t.clipMode() }; }
    finally { R.setClip = orig; }
  });
  check('section: thick slice calls setClip with 5 arguments, thickness = 2 × band', thick.band.length === 5 && thick.band[4] === 60 && thick.band[3] === true, JSON.stringify(thick.band));
  check('section: hide-front / no-clip keep the four-argument call', thick.front.length === 4 && thick.front[3] === true && thick.off.length === 4 && thick.off[3] === false, `${thick.front.length} / ${thick.off.length} args`);

  /* ---------------- z range editable and persisted; slider coalesced ---------------- */
  const zr = await page.evaluate(async () => {
    const app = window.gmApp, t = app.tools.section; const s = t.sec; t.setZ(1200, 1650);
    let runs = 0; const orig = t._update; t._update = function () { runs++; return orig.apply(this, arguments); };
    for (let i = 0; i < 12; i++) { t.offset = i; t.update(true); }
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const coalesced = runs; t.update(); const sync = runs - coalesced; t._update = orig; t.offset = 0; t.update();
    return { zMin: s.zMin, zMax: s.zMax, json: s.toJSON().z_min, coalesced, sync };
  });
  check('section: z from / to edited in the panel persist on the Section object', zr.zMin === 1200 && zr.zMax === 1650 && zr.json === 1200, JSON.stringify(zr));
  check('section: twelve slider ticks in one frame run the intersections once; a direct update() runs at once', zr.coalesced === 1 && zr.sync === 1, `${zr.coalesced} coalesced run(s), ${zr.sync} sync`);

  /* ---------------- section image planes: drawn under the section, oblique ones called out ---------------- */
  const img = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'); const o = app.project.origin; const t = app.tools.section;
    const c = document.createElement('canvas'); c.width = 200; c.height = 100; const x = c.getContext('2d'); x.fillStyle = '#c08040'; x.fillRect(0, 0, 200, 100);
    const on = new GM.ImagePlane({ image: c.toDataURL('image/png'), width: 200, height: 100, plane: 'section', p1: [o[0] - 400, o[1] + 5], p2: [o[0] + 400, o[1] - 5], z_top: 1600, z_bottom: 1300, name: 'Long section plate 3' }); on.group = 'Images'; app.project.add(on);
    const obl = new GM.ImagePlane({ image: c.toDataURL('image/png'), width: 200, height: 100, plane: 'section', p1: [o[0] - 300, o[1] - 200], p2: [o[0] + 300, o[1] + 200], z_top: 1600, z_bottom: 1300, name: 'Cross section plate 4' }); obl.group = 'Images'; app.project.add(obl);
    t.update(); const K = t.keyModel();
    const svg = await t.exportSvg({ download: false });
    const s2 = t.sectionFromImage(obl);
    return { on: K.images, oblique: K.oblique, svgImage: svg.includes('<image ') && svg.includes('data:image/png'), svgOblique: svg.includes('image plane oblique to this section'), made: s2.name, start: s2.start, zMin: s2.zMin, zMax: s2.zMax, selected: t.sec === s2, listed: [...document.querySelectorAll('#toolbody button')].some(b => /SECTION ON THIS IMAGE/.test(b.textContent)), cacheKeys: [...t._imgCache.keys()].length };
  });
  check('section: an in-plane section image is drawn under the products (once decoded, cached by id)', img.on.length === 1 && img.on[0] === 'Long section plate 3' && img.svgImage && img.cacheKeys >= 1, JSON.stringify(img.on));
  check('section: an oblique image is called out in the title line, not drawn', img.oblique.length === 1 && img.svgOblique, JSON.stringify(img.oblique));
  check('section: SECTION ON THIS IMAGE makes a section between the plate corners and selects it', img.made === 'Section on Cross section plate 4' && img.zMin === 1300 && img.zMax === 1600 && img.selected && img.listed, JSON.stringify([img.start, img.zMin, img.zMax]));

  /* ---------------- stratigraphy: input classification + trace source ---------------- */
  const cls = await page.evaluate(async () => {
    const app = window.gmApp, T = await import('./assets/geomodel/gm-tools.js'); const o = app.project.origin; const topo = app.topoGrid(); const lat = app.tools.strat.lattice(topo, false);
    const xyz = Float64Array.from([o[0], o[1], 0, o[0] + 10, o[1], 1500, o[0] + 20, o[1], 1500, o[0] + 40, o[1] + 40, 1500, o[0] + 9000, o[1], 1500, o[0], o[1] + 30, 1900, o[0] + 60, o[1], -200]);
    return T.classifyInputs(xyz, lat, topo);
  });
  check('strat: classifyInputs buckets z=0 as no elevation, plus outside / above / below', cls.no_elevation === 1 && cls.outside_lattice === 1 && cls.above_topography === 1 && cls.below_floor === 1 && cls.usable === 3, JSON.stringify(cls));

  const trace = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'), E = await import('./assets/geomodel/gm-engine.js'); const o = app.project.origin; const topo = app.topoGrid(); const ST = app.tools.strat; const lat = ST.lattice(topo);
    // the fixture's one mapped unit covers the whole box, so its clipped
    // outline IS the box edge: every vertex is a clipRingRect artefact and
    // must yield nothing — a surface through the box edge would be invented
    const boxEdge = app.project.byKind('lineset').filter(l => l.role === 'geology-outline' && l.parts.length).map(l => ST.traceToContacts(l, lat, topo).n);
    // a real outcrop trace draped the way gm-site drapes outlines (+2 m)
    const path = []; for (let i = 0; i <= 40; i++) { const x = o[0] - 1000 + i * 50, y = o[1] - 500 + 300 * Math.sin(i / 6); path.push([x, y]); }
    const ol = new GM.LineSet({ name: 'Tv outline (synthetic)', role: 'geology-outline', color: [40, 40, 40], group: 'Geology outlines' });
    ol.addPolyline(E.densify(path, topo.dx).map(([x, y]) => [x, y, (topo.sample(x, y) || 0) + 2]), { unit: 'Tv' }); app.project.add(ol);
    const best = { l: ol, n: ST.traceToContacts(ol, lat, topo).n, boxEdge };
    const S = ST.ensure(); S.units = [{ name: 'Traced unit', color: [200, 160, 120], contact: 'deposit', source: { kind: 'trace', id: best.l.id } }, { name: 'Basement', color: [140, 140, 140], contact: 'deposit', source: { kind: 'none' } }];
    ST.res = 40; ST.method = 'rbf'; ST.keepPrev = false; await ST.build();
    const g = app.project.byKind('grid2d').find(x => x.metadata && x.metadata.strat_of === S.id);
    const vols = app.project.byKind('mesh').filter(m => m.metadata && m.metadata.strat_of === S.id);
    const offered = ST.traceLayers().map(l => l.role);
    return { outline: best.l.name, n: best.n, boxEdge: best.boxEdge, built: !!S.metadata.built, prov: g && g.provenance, derived: g && g.metadata.derived_from, warnings: g && g.metadata.warnings, inputs: g && g.metadata.inputs, volDerived: vols.map(v => v.metadata.derived_from), offered, topo: topo.id, outlineId: best.l.id };
  });
  check('strat: an outline that is only the clipped box edge yields no contacts (nothing invented)', trace.boxEdge.length >= 1 && trace.boxEdge.every(n => n === 0), `${trace.boxEdge.join(',')} contacts from ${trace.boxEdge.length} box-edge outline(s)`);
  {
    check('strat: a mapped outline builds a base grid with provenance "contact from map trace"', trace.built && trace.prov && trace.prov.method === 'contact from map trace' && trace.prov.confidence === 'inferred' && trace.prov.source_id === trace.outlineId, `${trace.outline}: ${trace.n} trace contacts · ${JSON.stringify(trace.prov)}`);
    check('strat: the base grid warns that a single trace carries no dip away from the line', Array.isArray(trace.warnings) && trace.warnings.some(w => /no dip information/.test(w)), (trace.warnings || []).join(' | ').slice(0, 120));
    check('strat: derived_from lists the topography and the outline on bases and volumes', trace.derived && trace.derived.includes(trace.topo) && trace.derived.includes(trace.outlineId) && trace.volDerived.length >= 1 && trace.volDerived.every(d => d && d.includes(trace.topo)), JSON.stringify(trace.derived));
    check('strat: fault layers are not offered as contacts', !trace.offered.includes('faults') && trace.offered.length >= 1, trace.offered.join(','));
  }
  const refuse = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'); const o = app.project.origin; const ST = app.tools.strat;
    const ps = new GM.PointSet({ name: 'flat clicks', role: 'contacts' }); for (let i = 0; i < 12; i++) ps.add(o[0] + i * 40, o[1] + i * 25, 0, { n: i }); app.project.add(ps);
    const S = ST.ensure(); const keep = S.units; S.units = [{ name: 'Undraped', color: [1, 2, 3], contact: 'deposit', source: { kind: 'points', id: ps.id } }];
    const before = app.project.objects.length; let msg = null; const toasts = []; const obs = new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) if (n.textContent) toasts.push(n.textContent); }); obs.observe(document.body, { childList: true, subtree: true });
    ST.keepPrev = true; await ST.build(); obs.disconnect(); msg = toasts.find(t => /build failed/.test(t)) || null;
    S.units = keep; const z0 = ps.point(0)[2];
    // DRAPE only the points without elevation, keep z_original, undo restores
    ST.drape({ source: { id: ps.id } }); const z1 = ps.point(0)[2]; const zo = ps.attributes.z_original && ps.attributes.z_original[0]; app.undo(); const z2 = ps.point(0)[2];
    return { msg, added: app.project.objects.length - before, z0, z1, zo, z2, topoAt: app.topoGrid().sample(o[0], o[1]) };
  });
  check('strat: a unit whose points all lack elevation is refused, not given a default', /Undraped/.test(refuse.msg || '') && /no default elevation/.test(refuse.msg || '') && refuse.added === 0, (refuse.msg || 'no failure toast').slice(0, 140));
  check('strat: DRAPE sets z from the topography (z_original kept) and UNDO puts it back', refuse.z0 === 0 && Math.abs(refuse.z1 - refuse.topoAt) < 1e-6 && refuse.zo === 0 && refuse.z2 === 0, `z ${refuse.z0} → ${refuse.z1.toFixed(1)} → ${refuse.z2}`);

  const keep = await page.evaluate(async () => {
    const app = window.gmApp; const ST = app.tools.strat; const S = ST.ensure(); ST.keepPrev = true; await ST.build();
    const outs = app.project.objects.filter(o => o.metadata && o.metadata.strat_of === S.id);
    const prev = outs.filter(o => o.metadata.superseded), cur = outs.filter(o => !o.metadata.superseded);
    app.tools.open('strat'); const text = document.getElementById('toolbody').textContent;
    return { prev: prev.length, cur: cur.length, prevHidden: prev.every(o => o.visible === false && / \(previous\)$/.test(o.name)), says: /current set/.test(text) && /previous output/.test(text) };
  });
  check('strat: "keep previous outputs" hides and tags the earlier set instead of removing it', keep.prev >= 2 && keep.cur >= 2 && keep.prevHidden && keep.says, `${keep.cur} current, ${keep.prev} previous`);

  /* ---------------- implicit surface: bounds, clip below topography, provenance ---------------- */
  const imp = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'); const o = app.project.origin; const topo = app.topoGrid(); const zc = topo.sample(o[0], o[1]);
    const ps = new GM.PointSet({ name: 'shell pts', role: 'contacts' }); for (let i = 0; i < 120; i++) { const th = Math.random() * 6.283, ph = Math.acos(2 * Math.random() - 1); for (const [r, s] of [[80, 0], [100, 20], [60, -20]]) ps.add(o[0] + r * Math.sin(ph) * Math.cos(th), o[1] + r * Math.sin(ph) * Math.sin(th), zc + r * Math.cos(ph), { sd: s }); } app.project.add(ps);
    const T = app.tools.implicit; T.params.points = ps.id; T.params.valueCol = 'sd'; T.params.kernel = 'linear'; T.params.spacing = 8; T.params.clipTopo = true; T.params.zFrom = ''; T.params.zTo = ''; T.params.trend = 'none';
    app.tools.open('implicit'); const panelText = document.getElementById('toolbody').textContent;
    await T.build();
    const m = app.project.byKind('mesh').filter(x => x.name.startsWith('shell pts')).pop(); if (!m) return { none: true, panelText };
    let maxZ = -Infinity, above = 0; for (let i = 0; i < m.nVertices; i++) { const v = m.vertex(i); if (v[2] > maxZ) maxZ = v[2]; const t = topo.sample(v[0], v[1]); if (t === t && v[2] > t + 8) above++; }
    const tz = topo.zrange(); const bnd = m.metadata.boundary;
    return { tris: m.nTriangles, maxZ, above, topoMax: tz[1], topoMin: tz[0], name: m.name, prov: m.provenance, derived: m.metadata.derived_from, bnd, clipped: m.metadata.implicit && m.metadata.implicit.clipped_nodes, sphereTop: zc + 80, trendText: m.metadata.trend, panelSaysTrend: /trend: none/.test(panelText), ps: ps.id, topo: topo.id };
  });
  check('implicit: clip below topography — no vertex above the ground, max z ≤ topo max', !imp.none && imp.tris > 200 && imp.maxZ <= imp.topoMax && imp.above === 0 && imp.clipped > 0, imp.none ? 'no mesh' : `${imp.tris} tris · max z ${imp.maxZ.toFixed(1)} vs topo max ${imp.topoMax.toFixed(1)} · sphere top ${imp.sphereTop.toFixed(1)} · ${imp.clipped} nodes clipped`);
  check('implicit: default box runs from topo min − 400 to topo max + 20 and the depth is in the name', !imp.none && imp.bnd && Math.abs(imp.bnd.z_from - (imp.topoMin - 400)) < 1e-6 && Math.abs(imp.bnd.z_to - (imp.topoMax + 20)) < 1e-6 && /projected to/.test(imp.name), imp.none ? '' : `${imp.name} · z ${imp.bnd.z_from.toFixed(0)}–${imp.bnd.z_to.toFixed(0)}`);
  check('implicit: provenance inferred, derived_from lists the points and the topography, trend stated', !imp.none && imp.prov.confidence === 'inferred' && imp.derived.includes(imp.ps) && imp.derived.includes(imp.topo) && imp.trendText === 'trend: none' && imp.panelSaysTrend, imp.none ? '' : JSON.stringify(imp.prov));

  const trend = await page.evaluate(async () => {
    const app = window.gmApp; app.project.metadata.global_trend = { dip: 30, dip_azimuth: 90, pitch: 0, ratios: [3, 3, 1] };
    const T = app.tools.implicit; T.params.trend = 'global'; const tr = T.trend();
    const calls = []; const orig = app.engine.call; app.engine.call = function (op, args, ...rest) { calls.push([op, args && args.anisotropy]); return orig.call(this, op, args, ...rest); };
    try { T.params.clipTopo = false; await T.build(); } finally { app.engine.call = orig; }
    const m = app.project.byKind('mesh').filter(x => x.name.startsWith('shell pts')).pop();
    app.tools.open('implicit'); const panelText = document.getElementById('toolbody').textContent; delete app.project.metadata.global_trend; T.params.trend = 'none';
    return { text: tr.text, an: tr.anisotropy, passed: calls.find(c => c[0] === 'implicitSurface'), prov: m && m.provenance.trend, panel: /project global trend 30°→090, pitch 0, ratios 3,3,1/.test(panelText) };
  });
  check('implicit: the project global trend becomes the RBF anisotropy and the panel says which', trend.an && trend.an.ranges.length === 3 && trend.passed && trend.passed[1] && trend.passed[1].ranges && trend.prov === 'project global trend' && trend.panel, `${trend.text} · ranges ${trend.an && trend.an.ranges.map(v => v.toFixed(1)).join(',')}`);

  /* ---------------- truthful sample pickers ---------------- */
  const pick = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'), T = await import('./assets/geomodel/gm-tools.js'); const o = app.project.origin;
    const names = new GM.PointSet({ name: 'names only', role: 'points' }); names.add(o[0], o[1], 1500, { name: 'A' }); app.project.add(names);
    const claims = new GM.PointSet({ name: 'claims test', role: 'claims' }); claims.add(o[0], o[1], 1500, { acres: 20 }); app.project.add(claims);
    const assay = new GM.PointSet({ name: 'assays test', role: 'samples' }); assay.add(o[0], o[1], 1500, { au: 1.2 }); app.project.add(assay);
    const L = T.sampleLayers(app.project); const I = T.implicitLayers(app.project);
    // an empty project of samples: hide everything with a value and read the panel
    const hidden = L.slice(); for (const p of hidden) app.project.objects.splice(app.project.objects.indexOf(p), 1);
    app.tools.open('blocks'); const body = document.getElementById('toolbody'); const text = body.textContent; const disabled = [...body.querySelectorAll('button')].filter(b => /COMPUTE|AUTO-FIT|RUN ESTIMATE/.test(b.textContent)).map(b => [b.textContent, b.disabled, b.title]);
    for (const p of hidden) app.project.objects.push(p);
    app.tools.open('blocks');
    return { first: L[0] && L[0].role, names: L.map(l => l.name), roles: L.map(l => l.role), impNames: I.map(l => l.name), text: /✗ samples with a numeric value/.test(text), disabled };
  });
  check('blocks: sample picker lists only valued points layers, assays first, never claims or name lists', pick.first === 'samples' && !pick.roles.includes('claims') && !pick.names.includes('names only') && pick.names.includes('assays test'), pick.names.join(' | '));
  check('blocks: with no valued layer the panel says what is missing and disables COMPUTE / AUTO-FIT / RUN ESTIMATE', pick.text && pick.disabled.length === 3 && pick.disabled.every(d => d[1] && /numeric value/.test(d[2])), JSON.stringify(pick.disabled.map(d => d[0])));
  check('implicit: points list excludes name-only and claims layers, keeps contacts and side/sd columns', pick.impNames.includes('shell pts') && !pick.impNames.includes('names only') && !pick.impNames.includes('claims test'), pick.impNames.join(' | '));

  /* ---------------- MEASURE ---------------- */
  const meas = await page.evaluate(() => {
    const app = window.gmApp; const o = app.project.origin; app.tools.open('measure'); const T = app.tools.all.measure;
    T.start(); const armed = !!app.tools.armed && /MEASURE/.test(app.tools.armed.text);
    T.addPoint([o[0], o[1], 1500], 'Topography'); const m = T.addPoint([o[0] + 300, o[1] + 400, 1600], 'ground');
    const status = document.getElementById('status').textContent; const overlay = app.R.overlay.children.length; const panel = document.getElementById('toolbody').textContent;
    const ls = T.keep(); const f = ls.features[ls.features.length - 1];
    return { armed, len: m.len3d, plan: m.plan, dz: m.dz, bearing: m.bearing, plunge: m.plunge, status, overlay, panel: /3-D length/.test(panel) && /ft/.test(panel), role: ls.role, group: ls.group, feat: f, cleared: T.pts.length === 0 && !app.tools.armed };
  });
  check('measure: two programmatic points give the right 3-D length, plan length, Δz, bearing and plunge', Math.abs(meas.len - Math.hypot(500, 100)) < 1e-6 && Math.abs(meas.plan - 500) < 1e-6 && Math.abs(meas.dz - 100) < 1e-6 && Math.abs(meas.bearing - 36.8699) < 1e-3 && Math.abs(meas.plunge + 11.3099) < 1e-3, `${meas.len.toFixed(2)} m · plan ${meas.plan.toFixed(1)} · Δz ${meas.dz} · ${meas.bearing.toFixed(2)}° / ${meas.plunge.toFixed(2)}°`);
  check('measure: readout in metres and feet in the status line and the panel, rubber band in the overlay', /m \//.test(meas.status) && /ft/.test(meas.status) && meas.panel && meas.overlay >= 3 && meas.armed, meas.status.slice(0, 100));
  check('measure: KEEP writes an annotation lineset in Notes (never workings) with the numbers as fields', meas.role === 'annotation' && meas.group === 'Notes' && Math.abs(meas.feat.length_m - Math.hypot(500, 100)) < 0.01 && Math.abs(meas.feat.length_ft - Math.hypot(500, 100) / 0.3048) < 0.1 && meas.feat.bearing_deg === 36.9 && meas.cleared, JSON.stringify(meas.feat).slice(0, 160));

  /* ---------------- NOTES ---------------- */
  const notes = await page.evaluate(async () => {
    const app = window.gmApp, R = await import('./assets/geomodel/gm-render.js'); const o = app.project.origin; const before = R.confidenceTally(app.project);
    app.tools.open('notes'); const T = app.tools.all.notes; T.start(); const armed = !!app.tools.armed;
    T.commit([o[0] + 50, o[1] - 20, 1490], { text: 'quartz float on the slope', source: 'USGS Bull 1', page: '12', url: 'https://example.org/bull1', author: 'ml' });
    T.commit([o[0] - 50, o[1] + 20, 1495], { text: 'second note' });
    const ps = app.project.byKind('points').filter(p => p.role === 'notes'); const p = ps[0]; const d = app.display.get(p.id); const after = R.confidenceTally(app.project);
    const L = app.R.layers.get(p.id); let sprites = 0; L.group.traverse(n => { if (n.isSprite) sprites++; });
    const panel = document.getElementById('toolbody').textContent;
    T.remove(p, 0); const n1 = p.n; app.undo(); const n2 = p.n;
    return { layers: ps.length, n: p.n, group: p.group, name: p.name, cols: Object.keys(p.attributes), row: Object.fromEntries(Object.entries(p.attributes).map(([k, c]) => [k, c[0]])), labels: d && d.labels, field: d && d.labelField, sprites, same: JSON.stringify(before) === JSON.stringify(after), armed, panel: /quartz float/.test(panel), n1, n2 };
  });
  check('notes: each note appends a row to the one "notes" PointSet in Notes with the six columns', notes.layers === 1 && notes.n === 2 && notes.group === 'Notes' && notes.name === 'notes (interpretation)' && ['text', 'source', 'page', 'url', 'author', 'date'].every(c => notes.cols.includes(c)) && notes.row.text === 'quartz float on the slope' && notes.row.page === '12', JSON.stringify(notes.row));
  check('notes: labels forced on (text), pins never alter the confidence tally', notes.labels === true && notes.field === 'text' && notes.sprites >= 2 && notes.same && notes.panel && notes.armed, `${notes.sprites} label sprites`);
  check('notes: ✕ removes a note with UNDO', notes.n1 === 1 && notes.n2 === 2, `${notes.n1} → ${notes.n2}`);

  /* ---------------- TRACE a fault → Structure tool lists it ---------------- */
  const poly = await page.evaluate(() => {
    const app = window.gmApp; const o = app.project.origin; const topo = app.topoGrid(); app.tools.open('polyline'); const T = app.tools.all.polyline;
    T.role = 'faults'; T.target = ''; T.form.layerName = 'Traced fault'; T.form.name = 'Silver Hills fault'; T.form.type = 'fault'; T.form.unit_a = 'Tv'; T.form.unit_b = 'Pz'; T.form.doc = 'Plate 2'; T.form.page = '2';
    T.start(); const armed = app.tools.armed && app.tools.armed.text; const target = app.tools.armed && app.tools.armed.target;
    for (const [dx, dy] of [[-300, -200], [0, 0], [250, 300]]) T.addPoint(T.onGround([o[0] + dx, o[1] + dy, 0]));
    const ls = T.finish(); const f = ls.features[0]; const z = ls.partXYZ(0)[1][2]; const t = topo.sample(o[0], o[1]);
    app.tools.open('structure'); const text = document.getElementById('toolbody').textContent; const opts = [...document.querySelectorAll('#toolbody select option')].map(x => x.textContent);
    app.tools.open('polyline'); const listed = /Silver Hills fault/.test(document.getElementById('toolbody').textContent);
    T.removePart(ls, 0); const n1 = ls.parts.length; app.undo(); const n2 = ls.parts.length;
    return { armed, target, role: ls.role, group: ls.group, name: ls.name, feat: f, z, t, prov: ls.provenance, inDerive: /DERIVE FROM MAP TRACES/.test(text) && opts.some(x => /Traced fault \(1\)/.test(x)), listed, n1, n2 };
  });
  check('trace: the polyline commits to a new "faults" lineset in Structure, tagged sketched, z = topography + 3 m', poly.role === 'faults' && poly.group === 'Structure' && poly.name === 'Traced fault' && poly.feat.confidence === 'sketched' && poly.feat.name === 'Silver Hills fault' && poly.feat.unit_a === 'Tv' && poly.feat.source === 'Plate 2' && poly.feat.page === '2' && Math.abs(poly.z - (poly.t + 3)) < 1e-6 && poly.prov.confidence === 'sketched', JSON.stringify(poly.feat));
  check('trace: the mode line names the target and the confidence', /TRACE FAULT/.test(poly.armed || '') && /Traced fault \(new\) as sketched/.test(poly.target || ''), poly.target);
  check('trace: the Structure tool lists the new fault in DERIVE FROM MAP TRACES', poly.inDerive, poly.inDerive ? 'Traced fault (1) offered' : 'not offered');
  check('trace: the panel lists the feature and ✕ removes it with UNDO', poly.listed && poly.n1 === 0 && poly.n2 === 1, `${poly.n1} → ${poly.n2}`);

  /* ---------------- project switch cleans the new tools up ---------------- */
  const sw = await page.evaluate(() => { const app = window.gmApp; const T = app.tools.all; T.measure.pts = [[0, 0, 0]]; T.polyline.pts = [[0, 0, 0]]; T.polyline.target = 'x'; app.tools.onProject(); return { m: T.measure.pts.length, p: T.polyline.pts.length, t: T.polyline.target, closed: app.tools.active === null }; });
  check('shell: onProject resets the extra tools and closes the host', sw.m === 0 && sw.p === 0 && sw.t === '' && sw.closed, JSON.stringify(sw));

  // a refused build is logged by the tool (console.error + toast) before the
  // toast — the two refusals above are exercised on purpose, so they are not
  // page errors here
  const pageErrors = errors.filter(e => !/WebGL|GPU|swiftshader|GL_|Automatic fallback|THREE\.WebGLRenderer|favicon|net::ERR_FAILED|Failed to load resource|no default elevation is substituted/i.test(e));
  check('no page errors', pageErrors.length === 0, pageErrors.slice(0, 5).join(' || '));
} catch (e) {
  check('run', false, String(e && e.stack || e));
} finally {
  await browser.close(); server.kill();
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
