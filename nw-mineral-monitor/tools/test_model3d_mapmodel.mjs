#!/usr/bin/env node
/* Headless acceptance test for 'Model the rock from the map' in site/model3d.html
   (site/assets/geomodel/gm-map-model.js over the gm-engine.js map-model ops).
   - serves site/ with tools/range_server.py and stubs every external tile
     host with synthetic fixtures, exactly as tools/test_model3d.mjs does
   - boots the viewer on the Silver Hills site (aoi=cassia: real draped unit
     outlines from data/geology/cassia.json), derives orientations from the
     traces, opens the tool, BUILDs, adds a water level — and checks state,
     not pixels: provenance, confidence, the RESULT block, the warnings.
   Run: CHROME_PATH=... node tools/test_model3d_mapmodel.mjs   (exit code != 0 on failure) */
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
  await page.goto(`${base}model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills%20mine&gi=157&aoi=cassia&r=2500&fresh=1`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 3, null, { timeout: 120000 });
  await page.waitForTimeout(800);

  /* ---------- registration + inputs ---------- */
  const reg = await page.evaluate(async () => {
    const app = window.gmApp, M = await import('./assets/geomodel/gm-map-model.js');
    const x = app.tools.extra.find(e => e.key === 'mapmodel');
    // the site builder drops a unit's draped mesh past its triangle budget; the ages then come from the bundle the meshes were read from
    const ages = await app.tools.mapmodel.loadAges();
    const units = M.unitsFromProject(app.project, ages);
    return { agesLoaded: !!ages && ages.size > 100, agesFrom: units.map(u => u.age_from), installed: !!app.tools.all.mapmodel && app.tools.mapmodel === app.tools.all.mapmodel, label: x && x.label, hint: x && x.hint, units: units.map(u => [u.name, u.t0, u.t1, u.polygons, u.outline.nVertices]), outlines: app.project.objects.filter(o => o.kind === 'lineset' && o.role === 'geology-outline').length, structural: app.project.objects.filter(o => o.role === 'structural').length, card: document.getElementById('inspector').textContent };
  });
  check('install: the tool is registered as mapmodel with the label and hint', reg.installed && reg.label === 'Model the rock from the map' && reg.hint === 'contacts + derived dips → pancake', `${reg.label} · ${reg.hint}`);
  check('inputs: the Cassia outlines become aged map units (t0/t1 from the draped mesh, else from the geology bundle)', reg.agesLoaded && reg.units.length >= 3 && reg.units.every(u => u[1] != null && u[2] != null && u[4] > 10) && reg.agesFrom.every(Boolean), JSON.stringify(reg.units) + ' ages from ' + reg.agesFrom.join('/'));
  check('progress card: the one-click button is offered with mapped geology in the model', /MODEL THE ROCK FROM THE MAP/.test(reg.card), reg.card.slice(0, 80));

  /* ---------- panel before any orientation: DERIVE FIRST ---------- */
  await page.evaluate(() => window.gmApp.tools.open('mapmodel'));
  const p0 = await page.evaluate(() => { const b = document.getElementById('toolbody'); const btns = [...b.querySelectorAll('button')].map(x => x.textContent.trim()); return { ttl: document.querySelector('#toolhost .ttl').textContent, text: b.textContent, btns, hidden: document.getElementById('toolhost').hidden }; });
  check('panel: opens in the tool host under its own title, with the inferred-from-the-map sentence first', !p0.hidden && /MODEL THE ROCK FROM THE MAP/.test(p0.ttl) && p0.text.indexOf('Inferred from the map: unit bases are surfaces through the mapped contacts, bent down dip where an orientation was derived nearby. Not a survey of what is underground.') >= 0 && p0.text.indexOf('Inferred from the map') < p0.text.indexOf('NEEDS'), p0.ttl);
  check('panel: NEEDS names outlines, topography and (optional) readings; DERIVE FIRST offered while there are none', /geology outlines with unit ids/.test(p0.text) && /✓ topography/.test(p0.text) && /structural readings \(optional\)/.test(p0.text) && p0.btns.includes('DERIVE FIRST') && p0.btns.includes('BUILD'), p0.btns.join(' / '));
  check('panel: the options are the stated defaults (tol = topo cell, radius 300, offset 100, nodes 60, rbf)', reg.structural === 0 && (await page.evaluate(() => { const o = window.gmApp.tools.mapmodel.opts; return o.radius === 300 && o.offset === 100 && o.nodes === 60 && o.method === 'rbf' && o.tol === ''; })));

  /* ---------- DERIVE FIRST → step 3 on every trace, back to this panel ---------- */
  await page.evaluate(() => window.gmApp.tools.mapmodel.deriveFirst());
  await page.waitForFunction(() => window.gmApp.project.objects.some(o => o.role === 'structural') && !window.gmApp.tools.mapmodel.deriving, null, { timeout: 120000 });
  const p1 = await page.evaluate(() => { const b = document.getElementById('toolbody'); const btns = [...b.querySelectorAll('button')].map(x => x.textContent.trim()); const s = window.gmApp.project.objects.filter(o => o.role === 'structural'); return { text: b.textContent, btns, n: s.reduce((a, l) => a + l.n, 0), active: window.gmApp.tools.active === window.gmApp.tools.mapmodel, ttl: document.querySelector('#toolhost .ttl').textContent }; });
  check('derive first: orientations derived from the mapped traces, the panel comes back to this tool', p1.n > 0 && p1.active && /MODEL THE ROCK/.test(p1.ttl) && !p1.btns.includes('DERIVE FIRST') && /✓ structural readings/.test(p1.text), `${p1.n} readings`);

  /* ---------- BUILD ---------- */
  const built = await page.evaluate(async () => {
    const app = window.gmApp, T = app.tools.mapmodel;
    const before = app.project.objects.length;
    const sm = await T.build();
    const P = app.project;
    const bases = P.byKind('grid2d').filter(g => g.metadata && g.metadata.strat_of === (sm && sm.id));
    const vols = P.byKind('mesh').filter(m => m.role === 'unit' && m.metadata && m.metadata.strat_of === (sm && sm.id));
    const contacts = P.byKind('points').filter(p => p.metadata && p.metadata.strat_of === (sm && sm.id));
    const body = document.getElementById('toolbody');
    const res = body.querySelector('.mm-result');
    const prov = o => ({ method: o.provenance && o.provenance.method, conf: o.provenance && o.provenance.confidence, df: (o.metadata.derived_from || []).length, group: o.group, name: o.name, warnings: o.metadata.warnings || [] });
    const resultText = res ? res.textContent : null;
    // a W–E section through the model: the fill must see the new pancake (this shows the section panel; the next build restores ours)
    app.tools.section.preset('we');
    return {
      sm: sm ? { name: sm.name, units: sm.units.map(u => [u.name, !!u.base, u.source && u.source.kind]), built: sm.metadata.built, map_model: sm.metadata.map_model, prov: prov(sm), group: sm.group, topo: sm.topography === app.topoGrid().id } : null,
      bases: bases.map(prov), vols: vols.map(prov), contacts: contacts.map(o => Object.assign(prov(o), { n: o.n, visible: o.visible, role: o.role })),
      added: P.objects.length - before, result: T.result && { stats: T.result.stats, built: T.result.built, warnings: T.result.warnings },
      resultText, error: T.error, readiness: app.tools.readiness().strat, ribbons: app.tools.section.products.filter(p => p.kind === 'ribbon').length,
      rows: [...document.querySelectorAll('#layers .lrow')].map(r => r.textContent).filter(t => /base|Rock from the map|contacts \+ dip/.test(t)).length,
    };
  });
  check('build: a StratModel named "Rock from the map (inferred)" in group Stratigraphy, built, with its units and sources', !!built.sm && built.sm.name === 'Rock from the map (inferred)' && built.sm.map_model === true && !!built.sm.built && built.sm.topo && built.sm.units.filter(u => u[1]).length >= 2 && built.sm.units[built.sm.units.length - 1][1] === false && built.sm.units.filter(u => u[1]).every(u => u[2] === 'points') && built.sm.prov.method === 'model from map' && built.sm.prov.conf === 'inferred', built.sm ? JSON.stringify(built.sm.units) : `error: ${built.error}`);
  check('build: ≥ 2 base grids in group Stratigraphy, provenance.method "model from map", confidence inferred, derived_from set', built.bases.length >= 2 && built.bases.every(b => b.method === 'model from map' && b.conf === 'inferred' && b.df >= 2 && b.group === 'Stratigraphy'), built.bases.map(b => `${b.name} (${b.method}/${b.conf}, ${b.df})`).join(' | '));
  check('build: unit volumes (one per unit) with the same provenance, in group Stratigraphy', built.vols.length === (built.sm ? built.sm.units.length : -1) && built.vols.every(v => v.method === 'model from map' && v.conf === 'inferred' && v.group === 'Stratigraphy'), built.vols.map(v => v.name).join(' | '));
  check('build: the contact + offset points are kept as hidden contact layers a rebuild can use', built.contacts.length >= 2 && built.contacts.length === built.bases.length && built.contacts.every(c => c.role === 'contacts' && c.visible === false && c.n >= 3 && c.method === 'model from map'), built.contacts.map(c => `${c.name}: ${c.n}`).join(' | '));
  check('build: the RESULT block stays in the panel with units modelled, skipped, contacts per unit, dips used', !!built.resultText && /units modelled/.test(built.resultText) && /skipped/.test(built.resultText) && /contacts per unit/.test(built.resultText) && /dips used/.test(built.resultText) && /units without dip/.test(built.resultText), (built.resultText || '').replace(/\s+/g, ' ').slice(0, 200));
  check('build: the stats say which unit is basement and how many dips were used (readings existed)', !!built.result && built.result.stats.basement && built.result.stats.readings > 0 && built.result.stats.dips_used > 0 && built.result.stats.no_dip === false, built.result ? `basement ${built.result.stats.basement}, ${built.result.stats.dips_used} dips from ${built.result.stats.readings} readings, contacts ${JSON.stringify(built.result.stats.contacts_per_unit)}` : 'no result');
  check('build: step 6 reads as done and the section fill sees the pancake', built.readiness.state === 'done' && /built/.test(built.readiness.has) && built.ribbons >= 2, `${built.readiness.has} · ${built.ribbons} ribbons`);

  /* ---------- rebuild replaces this model's outputs, not another's ---------- */
  const re = await page.evaluate(async () => {
    const app = window.gmApp, T = app.tools.mapmodel; const sm = T.find(); const n0 = app.project.objects.length; const ids0 = app.project.objects.filter(o => o.metadata && o.metadata.strat_of === sm.id).map(o => o.id);
    T.opts.nodes = 40; await T.build();
    const ids1 = app.project.objects.filter(o => o.metadata && o.metadata.strat_of === sm.id).map(o => o.id);
    return { same: app.project.objects.length === n0, models: app.project.byKind('stratmodel').filter(m => m.metadata.map_model).length, replaced: ids1.length === ids0.length && !ids1.some(i => ids0.includes(i)), lattice: sm.metadata.built.lattice };
  });
  check('rebuild: one map model, its previous outputs replaced in place', re.same && re.models === 1 && re.replaced, `lattice ${re.lattice}`);

  /* ---------- water level: stated → described; no source → assumed ---------- */
  const water = await page.evaluate(() => {
    const app = window.gmApp, T = app.tools.mapmodel, topo = app.topoGrid(), b = topo.bounds();
    T.water = { mode: 'elev', value: '1450', source: 'USGS Bull. 1234', page: 'p. 12' };
    const m = T.addWater();
    T.water = { mode: 'collar', value: '80', source: '', page: '' };
    const m2 = T.addWater();
    const collar = T.collar();
    const z = i => m.vertices[3 * i + 2];
    return { m: m && { name: m.name, role: m.role, color: m.color, opacity: m.opacity, group: m.group, conf: m.metadata.confidence, note: m.metadata.note, src: m.metadata.source, flat: [0, 1, 2, 3].every(i => z(i) === 1450), box: m.vertices[0] === b[0] && m.vertices[1] === b[1] && m.vertices[3] === b[3] && m.vertices[7] === b[4], tris: m.nTriangles, drawn: app.R.layers.has(m.id) },
             m2: m2 && { conf: m2.metadata.confidence, z: m2.vertices[2], collar: collar && collar.z, warn: (m2.metadata.warnings || [])[0] || '', stated: m2.metadata.stated_as },
             panel: document.getElementById('toolbody').textContent };
  });
  check('water: a stated level with a source is a horizontal plane, role water, [60,120,220] @ 0.35, group Surfaces, "described"', !!water.m && water.m.role === 'water' && water.m.conf === 'described' && water.m.color.join() === '60,120,220' && water.m.opacity === 0.35 && water.m.group === 'Surfaces' && water.m.flat && water.m.box && water.m.tris === 2 && water.m.drawn && water.m.note === 'water level as stated; not a modelled head' && water.m.src.doc === 'USGS Bull. 1234', water.m ? `${water.m.name} · ${water.m.conf}` : 'no mesh');
  check('water: below the collar without a source is "assumed" and warned, collar from the topography at the site', !!water.m2 && water.m2.conf === 'assumed' && water.m2.collar != null && Math.abs(water.m2.z - (water.m2.collar - 80)) < 1e-6 && /assumption/.test(water.m2.warn), water.m2 ? `${water.m2.stated} → ${water.m2.z.toFixed(1)}` : 'no mesh');
  check('water: the panel says what was added last and at what confidence', /last added: Water level \d+ m \(assumed\) — assumed \(no source given\)/.test(water.panel), (water.panel.match(/last added:[^\n]{0,80}/) || [''])[0]);

  /* ---------- refusal in the page: a unit that touches nothing older is skipped and named ---------- */
  const refuse = await page.evaluate(async () => {
    const app = window.gmApp, GM = await import('./assets/geomodel/gm-core.js'), topo = app.topoGrid(), o = app.project.origin;
    const ls = new GM.LineSet({ name: 'Island outline', role: 'geology-outline', group: 'Geology outlines' });
    const ring = []; for (let k = 0; k <= 24; k++) { const a = k / 24 * Math.PI * 2; const x = o[0] + 120 * Math.cos(a), y = o[1] + 120 * Math.sin(a); ring.push([x, y, topo.sample(x, y) + 2]); }
    ls.addPolyline(ring, { unit: 'Island tuff', unit_id: 'island-1', age: 'Holocene', t0: 0.01, t1: 0 });
    app.project.add(ls);
    const T = app.tools.mapmodel; await T.build();
    const res = document.querySelector('#toolbody .mm-result');
    app.project.remove(ls);
    return { skipped: T.result && T.result.stats.rejected.no_contacts, warn: T.result && T.result.warnings.find(w => /Island tuff/.test(w)), text: res ? res.textContent : '' };
  });
  check('refusal: a unit whose outline touches no older unit is skipped, named in the warnings and in the panel', refuse.skipped && refuse.skipped.includes('Island tuff') && /touches no older unit/.test(refuse.warn || '') && /Island tuff — touches no older unit/.test(refuse.text), (refuse.warn || '').slice(0, 120));

  const pageErrors = errors.filter(e => !/WebGL|GPU|swiftshader|GL_|Automatic fallback|THREE\.WebGLRenderer|favicon|net::ERR_FAILED|Failed to load resource/i.test(e));
  check('no page errors', pageErrors.length === 0, pageErrors.slice(0, 5).join(' || '));
} catch (e) {
  check('run', false, String(e && e.stack || e));
} finally {
  await browser.close(); server.kill();
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
