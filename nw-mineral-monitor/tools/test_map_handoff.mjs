#!/usr/bin/env node
/* Headless acceptance test for the map -> 3-D modeller hand-off (g3dHandoff in
   site/index.html): the rock units, fault traces and mine features the map has
   loaded around a site reach IndexedDB 'nwmm-geomodel' for model3d.html.
   - serves site/ with tools/range_server.py (PMTiles need byte ranges) and
     reads the real national geology / faults / USMIN / MRDS archives from
     site/data/tiles (no fixtures: the check is against the tiles that ship)
   - stubs every external host the way tools/test_model3d.mjs does (basemap
     rasters answer a 1-px PNG, BLM queries answer empty, the rest is aborted)
   - boots index.html, turns on the USGS geology + faults layers, jumps to
     Tonopah NV (inside the national tiles), waits for the tiles, and checks
     the hand-off's state, not pixels
   Run: CHROME_PATH=... node tools/test_map_handoff.mjs  (exit code != 0 on failure) */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = 9100 + Math.floor(Math.random() * 200);
const BASE = `http://127.0.0.1:${PORT}`;
const SITE = { lon: -117.2, lat: 38.05, zoom: 12, radius: 2500, name: 'Tonopah test site' };
const results = []; let failed = 0;
function check(name, ok, detail = '') { results.push([ok ? 'PASS' : 'FAIL', name, detail]); if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); }
const DARK_PX = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');

async function waitForServer(child) {
  let last;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (child.exitCode !== null) throw new Error(`range server exited ${child.exitCode}`);
    try {
      const r = await fetch(`${BASE}/data/manifest.json`, { headers: { Range: 'bytes=0-31' } });
      if (r.status === 206 && r.headers.get('accept-ranges') === 'bytes') return;
      last = new Error(`unexpected range response ${r.status}`);
    } catch (e) { last = e; }
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error(`range server did not become ready: ${last}`);
}
const lonlatPair = c => Array.isArray(c) && c.length >= 2 && Number.isFinite(c[0]) && Number.isFinite(c[1]) && Math.abs(c[0]) <= 180 && Math.abs(c[1]) <= 90;

const server = spawn('python3', ['tools/range_server.py', String(PORT)], { cwd: ROOT, stdio: 'ignore' });
const browser = await (async () => {
  await waitForServer(server);
  return chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || undefined, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--disable-dev-shm-usage'] });
})().catch(e => { server.kill(); throw e; });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = []; page.on('pageerror', e => errors.push(String(e.message || e))); page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
await page.route('**/*', route => {
  const url = new URL(route.request().url());
  if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') return route.continue();
  if (/(?:basemaps\.cartocdn\.com|server\.arcgisonline\.com|basemap\.nationalmap\.gov|tiles\.macrostrat\.org)$/.test(url.hostname))
    return route.fulfill({ status: 200, contentType: 'image/png', body: DARK_PX });
  if (url.hostname === 'gis.blm.gov')
    return route.fulfill({ status: 200, contentType: 'application/geo+json', body: JSON.stringify({ type: 'FeatureCollection', features: [] }) });
  return route.abort('blockedbyclient');
});

const t0 = Date.now();
try {
  // boot straight onto the site (hash view) so the national-zoom tiles are never fetched
  await page.goto(`${BASE}/?debug=1#${SITE.zoom}/${SITE.lat}/${SITE.lon}`, { waitUntil: 'domcontentloaded', timeout: 90_000 });
  await page.waitForFunction(() => { try { return typeof DBG_BOOT_SETTLED !== 'undefined' && DBG_BOOT_SETTLED && map.loaded(); } catch (_) { return false; } }, null, { timeout: 120_000 });
  check('boot: map settled', true, `${((Date.now() - t0) / 1000).toFixed(1)} s`);

  // the user turns USGS GEOLOGY + FAULTS on and is looking at the site
  await page.evaluate(({ lon, lat, zoom }) => {
    for (const id of ['nationalGeoTgl', 'nationalFaultTgl']) { const el = document.getElementById(id); if (el) el.checked = true; }
    S.layers.natGeo = true; S.layers.natFault = true; applyFilters();
    map.jumpTo({ center: [lon, lat], zoom });
  }, SITE);
  await page.waitForFunction(() => {
    try {
      return map.loaded() && map.areTilesLoaded() &&
        map.querySourceFeatures('national-geology', { sourceLayer: 'geology' }).length > 0 &&
        map.querySourceFeatures('national-usmin', { sourceLayer: 'usmin' }).length > 0;
    } catch (_) { return false; }
  }, null, { timeout: 120_000 });
  await page.waitForTimeout(600);
  const layersOn = await page.evaluate(() => ({ geo: map.getLayoutProperty('national-geology-fill', 'visibility'), fault: map.getLayoutProperty('national-fault-line', 'visibility'), zoom: map.getZoom() }));
  check('setup: geology + fault layers visible at the site', layersOn.geo === 'visible' && layersOn.fault === 'visible' && Math.abs(layersOn.zoom - SITE.zoom) < 0.01, JSON.stringify(layersOn));

  // ---- the hand-off itself ----
  const h = await page.evaluate(({ lon, lat, radius }) => {
    const h = g3dHandoff(lon, lat, radius);
    // serialisable summary + the full geology block (the rings are plain arrays)
    return { total: h.total, layers: Object.fromEntries(Object.entries(h.layers).map(([k, v]) => [k, { n: v.length, sample: v[0] }])), geology: h.geology, note: h.note, at: h.at };
  }, SITE);
  const G = h.geology || {};
  const units = G.units || [], faults = G.faults || [];
  check('handoff: rock units present', units.length > 0, `${units.length} units from ${(G.sources || []).join(', ')}`);
  check('handoff: every unit carries a name or label, a lithology and rings', units.length > 0 && units.every(u => (u.nm || u.label) && u.li && Array.isArray(u.rings) && u.rings.length > 0 && Array.isArray(u.polys) && u.polys.length > 0),
    units.slice(0, 3).map(u => `${u.label || u.nm} · ${u.li} · ${u.polys.length} poly / ${u.rings.length} rings`).join(' | '));
  check('handoff: rings are [lon,lat] pairs in GeoJSON order (polys = [outer, holes...])', units.every(u => u.polys.every(poly => poly.length >= 1 && poly.every(r => r.length >= 3 && r.every(lonlatPair))) && u.rings.length === u.polys.reduce((n, poly) => n + poly.length, 0)));
  const ids = units.map(u => u.id);
  check('handoff: no two units share an id (tile repeats merged by fid)', new Set(ids).size === ids.length, `${ids.length} ids, ${new Set(ids).size} unique; ${units.filter(u => u.tiles > 1).length} units seen in more than one tile`);
  check('handoff: units carry provenance (fid, source, scale, url or dataset)', units.every(u => u.fid != null && u.src && (u.scale || u.url)), JSON.stringify(units[0] && { id: units[0].id, fid: units[0].fid, src: units[0].src, scale: units[0].scale, url: units[0].url, age: units[0].age, age_min: units[0].age_min, age_max: units[0].age_max, color: units[0].color }));
  const [W, S_, E, N] = G.box || [];
  const bboxOf = pts => pts.reduce((b, c) => [Math.min(b[0], c[0]), Math.min(b[1], c[1]), Math.max(b[2], c[0]), Math.max(b[3], c[1])], [Infinity, Infinity, -Infinity, -Infinity]);
  const hits = b => b[0] <= E && b[2] >= W && b[1] <= N && b[3] >= S_;
  check('handoff: every kept polygon touches the 1.3×radius box (whole features kept, the viewer clips)', Array.isArray(G.box) && units.every(u => u.polys.every(poly => hits(bboxOf(poly[0])))), `box ${(G.box || []).map(v => v.toFixed(3)).join(',')}`);
  check('handoff: faults array present with [lon,lat] paths, one entry per LineString', Array.isArray(faults) && faults.every(f => Array.isArray(f.path) && f.path.length >= 2 && f.path.every(lonlatPair) && f.id && Number.isInteger(f.part)),
    `${faults.length} fault traces; ${faults.slice(0, 2).map(f => `${f.nm || f.ty || '(unnamed)'} · ${f.age || 'age unstated'} · ${f.src}`).join(' | ')}`);
  check('handoff: sources name the tile layers used', Array.isArray(G.sources) && G.sources.includes('national-geology/geology') && (faults.length === 0 || G.sources.includes('national-faults/faults')), (G.sources || []).join(', '));
  check('handoff: geology totals match', G.total && G.total.units === units.length && G.total.faults === faults.length, JSON.stringify(G.total));
  check('handoff: no note when geology was on and loaded at the site', G.note == null && G.zoom === SITE.zoom, `note ${String(G.note)}, zoom ${G.zoom}`);
  check('handoff: the point layers are still handed over (USMIN + MRDS around Tonopah)', h.total > 0 && h.layers.usmin && h.layers.usmin.n > 0 && h.layers.mrds && h.layers.mrds.n > 0,
    `total ${h.total}: ${Object.entries(h.layers).map(([k, v]) => `${k} ${v.n}`).join(', ')}; usmin sample ${h.layers.usmin && JSON.stringify(h.layers.usmin.sample.p).slice(0, 80)}`);
  check('handoff: point layer records keep {x,y,p}', Object.values(h.layers).every(v => v.sample && Number.isFinite(v.sample.x) && Number.isFinite(v.sample.y) && v.sample.p && typeof v.sample.p === 'object'));
  check('handoff: the snapshot is labelled as a snapshot', /snapshot/.test(h.note) && !!h.at);
  check('handoff: caps respected', units.length <= 3000 && faults.length <= 2000);

  // ---- open3D writes it to IndexedDB for model3d.html (window.open stubbed) ----
  const stored = await page.evaluate(async ({ lon, lat, radius, name }) => {
    const opened = []; const realOpen = window.open; window.open = u => { opened.push(u); return null; };
    try { await open3D(lon, lat, name, { radius }); } finally { window.open = realOpen; }
    const key = 'site:' + g3dSlug(name) + '-' + lat.toFixed(3) + '_' + lon.toFixed(3);
    const db = await g3dDb();
    const rec = await new Promise((res, rej) => { const tx = db.transaction('handoff', 'readonly'); const rq = tx.objectStore('handoff').get(key); rq.onsuccess = () => res(rq.result); rq.onerror = () => rej(rq.error); });
    return { opened, key, has: !!rec, units: rec && rec.data.geology ? rec.data.geology.units.length : -1, faults: rec && rec.data.geology ? rec.data.geology.faults.length : -1, points: rec ? rec.data.total : -1 };
  }, SITE);
  check('open3D: the hand-off with geology lands in IndexedDB nwmm-geomodel/handoff', stored.has && stored.units > 0 && stored.points > 0, JSON.stringify(stored));
  check('open3D: opens model3d.html with the site params', stored.opened.length === 1 && /^model3d\.html\?/.test(stored.opened[0]) && /lat=38\.05/.test(stored.opened[0]) && /r=2500/.test(stored.opened[0]), stored.opened[0]);

  // ---- geology hidden at click time: the source is still asked ----
  const hidden = await page.evaluate(async ({ lon, lat, radius }) => {
    const el = document.getElementById('nationalGeoTgl'); if (el) el.checked = false;
    S.layers.natGeo = false; applyFilters();
    await new Promise(r => { let done = false; map.once('idle', () => { done = true; r(); }); setTimeout(() => { if (!done) r(); }, 4000); });
    const h = g3dHandoff(lon, lat, radius);
    return { units: h.geology.units.length, faults: h.geology.faults.length, note: h.geology.note, vis: map.getLayoutProperty('national-geology-fill', 'visibility') };
  }, SITE);
  check('hidden: with USGS GEOLOGY off the hand-off says so',
    hidden.vis === 'none' && typeof hidden.note === 'string' && (hidden.units > 0 ? /hidden on the map/.test(hidden.note) : /USGS GEOLOGY on/.test(hidden.note)),
    `${hidden.units} units, ${hidden.faults} faults, note: ${hidden.note}`);
  check('hidden: faults (still on) are still handed over', hidden.faults === faults.length, `${hidden.faults} vs ${faults.length}`);
  await page.evaluate(() => { const el = document.getElementById('nationalGeoTgl'); if (el) el.checked = true; S.layers.natGeo = true; applyFilters(); });

  // ---- zoomed out: the note and the units must agree ----
  const far = await page.evaluate(async ({ lon, lat, radius }) => {
    map.jumpTo({ center: [lon, lat], zoom: 6 });
    await new Promise(r => { let done = false; map.once('idle', () => { done = true; r(); }); setTimeout(() => { if (!done) r(); }, 8000); });
    const h = g3dHandoff(lon, lat, radius);
    return { units: h.geology.units.length, note: h.geology.note, zoom: map.getZoom(), gzoom: h.geology.zoom };
  }, SITE);
  check('zoomed out: either no rock units and a note to zoom in with USGS GEOLOGY on, or coarse units and a note that their outlines are simplified',
    typeof far.note === 'string' && /USGS GEOLOGY/.test(far.note) && (far.units === 0 ? /zoom to the site/.test(far.note) : /simplified/.test(far.note)) && far.gzoom === 6,
    `z${far.zoom.toFixed(1)}: ${far.units} units, note: ${far.note}`);

  // ---- a state-survey geology / fault archive that is on joins the hand-off ----
  const ss = await page.evaluate(async ({ lon, lat, zoom, radius }) => {
    map.jumpTo({ center: [lon, lat], zoom });
    const d = STATE_SURVEY_LAYERS.find(r => r.id === 'nv_usgs_ds249');
    if (!d) return { missing: true };
    const el = document.getElementById(d.toggle_id); if (el) el.checked = true;
    S.stateSurvey[d.id] = true; applyFilters();
    const t0 = Date.now();
    while (Date.now() - t0 < 60000) {
      await new Promise(r => setTimeout(r, 300));
      try { if (map.loaded() && map.areTilesLoaded() && map.getLayer('nv-ds249-geology') && map.querySourceFeatures(d.source_id, { sourceLayer: 'nv_ds249_geology' }).length > 0 && map.querySourceFeatures('national-geology', { sourceLayer: 'geology' }).length > 0) break; } catch (_) {}
    }
    const h = g3dHandoff(lon, lat, radius);
    const nv = h.geology.units.filter(u => u.layer === 'nv-ds249/nv_ds249_geology'), nvf = h.geology.faults.filter(f => f.layer === 'nv-ds249/nv_ds249_faults');
    const ids = h.geology.units.map(u => u.id);
    el.checked = false; S.stateSurvey[d.id] = false; applyFilters();
    return { sources: h.geology.sources, units: h.geology.units.length, nv: nv.length, nvf: nvf.length, sample: nv[0] && { id: nv[0].id, nm: nv[0].nm, label: nv[0].label, li: nv[0].li, src: nv[0].src, scale: nv[0].scale, polys: nv[0].polys.length }, unique: new Set(ids).size === ids.length, note: h.geology.note };
  }, SITE);
  check('state survey: NEVADA DS 249 geology + faults join the hand-off beside the national rows, ids still unique',
    !ss.missing && ss.sources.includes('nv-ds249/nv_ds249_geology') && ss.sources.includes('national-geology/geology') && ss.nv > 0 && ss.nvf > 0 && ss.unique && ss.sample && (ss.sample.nm || ss.sample.label) && ss.sample.src && ss.sample.polys > 0,
    JSON.stringify({ sources: ss.sources, units: ss.units, nv: ss.nv, nvf: ss.nvf, sample: ss.sample, note: ss.note }).slice(0, 300));

  // ---- the card copy ----
  const card = await page.evaluate(({ lon, lat }) => {
    const btn = g3dButton(lon, lat, "O'Brien mine");
    const feat = (lid, source, props) => ({ layer: { id: lid }, source, properties: props, geometry: { type: 'Point', coordinates: [lon, lat] } });
    showFeature(feat('national-usmin-c', 'national-usmin', { typ: 'Adit', nm: 'Test adit', st: 'NV', quad: 'Tonopah', yr: 1905, scale: 62500, az: 120 }));
    const usmin = document.getElementById('detailInner').innerHTML;
    showFeature(feat('national-mrds-c', 'national-mrds', { nm: 'Test mine', st: 'NV', id: 10000001, status: 'P', g: 1, commodities: 'AU AG' }));
    const mrds = document.getElementById('detailInner').innerHTML;
    showFeature(feat('national-ardf-c', 'national-ardf', { nm: 'Test ardf', id: 'AA001' }));
    const ardf = document.getElementById('detailInner').innerHTML;
    closeDetail();
    return { btn, usmin, mrds, ardf };
  }, SITE);
  const HINT = 'The 3-D model shows what is mapped here; underground workings appear only where a document describes them.';
  check('card: the button says what the model gets', /title="open a 3-D model around this site: terrain, the USGS geology and faults the map has loaded here, and the mine features around it \(new tab\)"/.test(card.btn) && /OPEN 3D MODEL/.test(card.btn), card.btn.slice(0, 80));
  check('card: an apostrophe in the name survives the onclick', /open3D\(-117\.200000,38\.050000,'O\\'Brien mine'/.test(card.btn));
  check('card: USMIN card carries the button and the one-line honesty note', card.usmin.includes('OPEN 3D MODEL') && card.usmin.includes(HINT) && card.usmin.indexOf('OPEN 3D MODEL') < card.usmin.indexOf(HINT));
  check('card: MRDS card carries the button and the one-line honesty note', card.mrds.includes('OPEN 3D MODEL') && card.mrds.includes(HINT) && card.mrds.indexOf('OPEN 3D MODEL') < card.mrds.indexOf(HINT));
  check('card: the note is one line under the button, once per card', (card.usmin.match(/g3dhint/g) || []).length === 1 && (card.mrds.match(/g3dhint/g) || []).length === 1 && !card.ardf.includes(HINT));

  const pageErrors = errors.filter(e => !/WebGL|GPU|swiftshader|GL_|favicon|net::ERR_FAILED|net::ERR_BLOCKED_BY_CLIENT|Failed to load resource|blockedbyclient|ERR_ABORTED/i.test(e));
  check('no page errors', pageErrors.length === 0, pageErrors.slice(0, 5).join(' || '));
} catch (e) {
  check('run', false, String(e && e.stack || e));
} finally {
  await browser.close(); server.kill();
}
console.log(`\n${results.length - failed}/${results.length} passed in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
process.exit(failed ? 1 : 0);
