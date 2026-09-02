#!/usr/bin/env node
/* Headless acceptance test for the map hand-off into site/model3d.html:
   the viewer builds rock, faults and mine features for every site from what
   the map wrote into IndexedDB 'nwmm-geomodel' (store 'handoff').
   - serves site/ with tools/range_server.py, stubs every external tile host
   - seeds the hand-off the way index.html's open3D() writes it (same DB
     schema, key 'site:<slug>-<lat>_<lon>') before each boot
   - boot A: Silver Hills (inside the Cassia bundle, aoi=cassia) — the bundle
     is preferred, the hand-off geology is skipped with a note, the USMIN
     rows become a 'features' layer drawn as type glyphs
   - boot B: a site outside every bundle (aoi=auto) — the hand-off geology is
     draped, ages read into t0/t1 (table, numbers, or warned), faults draped
   - boot C: the same site with nothing handed over — the model says it has
     no mapped geology
   State is checked, never pixels (swiftshader paints black).
   Run: CHROME_PATH=… node tools/test_model3d_site.mjs  (exit != 0 on failure) */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const F = await import(path.join(ROOT, 'site/assets/geomodel/gm-formats.js'));
const SITE = await import(path.join(ROOT, 'site/assets/geomodel/gm-site.js'));
const PORT = 9500 + Math.floor(Math.random() * 300);
const results = []; let failed = 0;
function check(name, ok, detail = '') { results.push([ok ? 'PASS' : 'FAIL', name, detail]); if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); }

/* ---------- the age reader, checked in node before any browser ---------- */
{
  const P = SITE.parseAgeText;
  const eq = (t, want) => { const r = P(t); const ok = want == null ? r == null : !!r && Math.abs(r.t0 - want[0]) < 1e-9 && Math.abs(r.t1 - want[1]) < 1e-9; check(`age text: ${JSON.stringify(t)} → ${want ? want.join('/') : 'null'}`, ok, r ? `${r.t0}/${r.t1} (${r.how})` : 'null'); };
  eq('Cretaceous', [145, 66]); eq('Late Jurassic to Early Cretaceous', [173, 105.5]); eq('Miocene (17-14 Ma)', [17, 14]);
  eq('Pennsylvanian and Permian', [323, 252]); eq('Neoproterozoic', [1000, 539]); eq('Precambrian', [4600, 539]); eq('Gronkian', null); eq('', null);
  check('hexRGB: #AABBCC read, #abc and null refused', JSON.stringify(SITE.hexRGB('#AABBCC')) === '[170,187,204]' && SITE.hexRGB('#abc') == null && SITE.hexRGB(null) == null);
}

/* ---------- synthetic tiles (as test_model3d.mjs) ---------- */
async function terrariumTile() { const rgba = new Uint8Array(256 * 256 * 4); for (let y = 0; y < 256; y++) for (let x = 0; x < 256; x++) { const elev = 1500 + 0.8 * (x - 128) + 0.2 * (y - 128); const v = Math.round((elev + 32768) * 256); const o = (y * 256 + x) * 4; rgba[o] = (v >> 16) & 255; rgba[o + 1] = (v >> 8) & 255; rgba[o + 2] = v & 255; rgba[o + 3] = 255; } return F.encodePNG(256, 256, rgba, { channels: 4 }); }
async function flatTile(r, g, b) { const rgba = new Uint8Array(256 * 256 * 4); for (let i = 0; i < 256 * 256; i++) { rgba[4 * i] = r; rgba[4 * i + 1] = g; rgba[4 * i + 2] = b; rgba[4 * i + 3] = 255; } return F.encodePNG(256, 256, rgba, { channels: 4 }); }

/* ---------- the hand-offs, shaped exactly as index.html open3D() writes them ---------- */
const box = (lon0, lat0, lon1, lat1) => [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]];   // one polygon = [outer ring]
const SH = { lon: -113.125, lat: 42.147, name: 'Silver Hills mine' };
const OUT = { lon: -115.3, lat: 43.6, name: 'Outside site' };
const keyOf = (name, lat, lon) => name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') + '-' + lat.toFixed(3) + '_' + lon.toFixed(3);
const handoffA = {
  layers: { usmin: [
    { x: -113.128, y: 42.145, p: { typ: 'Mine Shaft', nm: 'Test shaft', quad: 'Almo', yr: 1972, scale: 24000, st: 'ID' } },
    { x: -113.121, y: 42.15, p: { typ: 'Adit', az: 45, nm: 'Test adit', quad: 'Almo', yr: 1972, scale: 24000, st: 'ID' } },
  ] },
  geology: {
    units: [
      { id: 'a1', nm: 'Handoff unit west', label: 'Kw', li: 'sandstone', age: 'Cretaceous', de: 'test', color: '#336699', src: 'SGMC', url: 'https://example.invalid/sgmc', scale: '1:500,000', polys: [box(-113.14, 42.135, -113.125, 42.16)] },
      { id: 'a2', nm: 'Handoff unit east', label: 'Tv', li: 'tuff', age: 'Miocene', age_min: 12, age_max: 20, color: '#AABBCC', src: 'SGMC', url: 'https://example.invalid/sgmc', scale: '1:500,000', polys: [box(-113.125, 42.135, -113.11, 42.16)] },
    ],
    faults: [{ id: 'f1', nm: 'Test fault', ty: 'normal', src: 'USGS Quaternary faults', path: [[-113.14, 42.14], [-113.11, 42.155]] }],
    sources: ['SGMC', 'USGS Quaternary faults'], note: 'synthetic',
  },
  total: 2, at: new Date().toISOString(), note: 'viewport snapshot of loaded tiles, not an archive',
};
const handoffB = {
  layers: { usmin: [
    { x: -115.31, y: 43.595, p: { typ: 'Tunnel', nm: 'No-azimuth tunnel', quad: 'Test', yr: 1950, scale: 62500, st: 'ID' } },
    { x: -115.305, y: 43.605, p: { typ: 'Open Pit Mine', nm: 'Pit', quad: 'Test', yr: 1950, scale: 62500, st: 'ID' } },
    { x: -115.3, y: 43.595, p: { typ: 'Placer', nm: 'Bar', quad: 'Test', yr: 1950, scale: 62500, st: 'ID' } },
    { x: -115.295, y: 43.605, p: { typ: 'Prospect Pit', nm: null, quad: 'Test', yr: 1950, scale: 62500, st: 'ID' } },
    { x: -115.29, y: 43.595, p: { typ: 'Mine Dump', nm: 'Dump', quad: 'Test', yr: 1950, scale: 62500, st: 'ID' } },
    { x: -115.31, y: 43.605, p: { typ: 'Mill', nm: 'Stamp mill', quad: 'Test', yr: 1950, scale: 62500, st: 'ID' } },
    { x: -115.29, y: 43.605, p: { typ: 'Diggings', nm: 'Diggings', quad: 'Test', yr: 1950, scale: 62500, st: 'ID' } },
  ] },
  geology: {
    units: [
      { id: 101, nm: 'Cretaceous granodiorite', label: 'Kgd', li: 'granodiorite', age: 'Cretaceous', de: 'Idaho batholith', src: 'SGMC', url: 'https://example.invalid/sgmc', scale: '1:500,000', polys: [box(-115.33, 43.58, -115.3, 43.62)] },
      { id: 102, nm: 'Numeric-age volcanics', label: 'Tv', li: 'rhyolite', age: 'Miocene', age_min: 12, age_max: 20, de: 'test', color: '#AABBCC', src: 'SGMC', url: 'https://example.invalid/sgmc', scale: '1:500,000', polys: [box(-115.3, 43.58, -115.27, 43.62)] },
      { id: 103, nm: 'Gronk unit', label: 'Gk', li: 'unknown', age: 'Gronkian', de: 'an age the table cannot read', color: 'not-a-colour', src: 'SGMC', polys: [box(-115.305, 43.597, -115.295, 43.603)] },
    ],
    faults: [{ id: 'f9', nm: 'Outside fault', ty: 'normal', age: 'Quaternary', slip: 'normal', src: 'USGS Quaternary faults', path: [[-115.32, 43.585], [-115.28, 43.615]] }],
    sources: ['SGMC', 'USGS Quaternary faults'], note: 'synthetic',
  },
  total: 7, at: new Date().toISOString(), note: 'viewport snapshot of loaded tiles, not an archive',
};

const server = spawn('python3', ['tools/range_server.py', String(PORT)], { cwd: ROOT, stdio: 'ignore' });
await new Promise(r => setTimeout(r, 900));
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/opt/pw-browsers/chromium', args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = []; page.on('pageerror', e => errors.push(String(e.message || e))); page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|ERR_FAILED|ERR_ABORTED/.test(m.text())) errors.push('console: ' + m.text()); });
await page.route(u => /^https?:\/\//.test(u.href) && !u.href.includes(`:${PORT}`), r => r.abort());
const terr = await terrariumTile(), sat = await flatTile(60, 90, 50);
await page.route(/elevation-tiles-prod/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(terr), headers: { 'Access-Control-Allow-Origin': '*' } }));
await page.route(/arcgisonline|nationalmap\.gov|macrostrat/, r => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(sat), headers: { 'Access-Control-Allow-Origin': '*' } }));
const base = `http://localhost:${PORT}/`;

/** Seed the hand-off in page context exactly as index.html g3dDb() / open3D() do. */
async function seedHandoff(key, data) {
  await page.goto(`${base}data/aoi.json`, { waitUntil: 'domcontentloaded' });   // any document of the origin
  await page.evaluate(async ({ key, data }) => {
    const db = await new Promise((res, rej) => { const rq = indexedDB.open('nwmm-geomodel', 1); rq.onupgradeneeded = () => { const d = rq.result; if (!d.objectStoreNames.contains('projects')) d.createObjectStore('projects', { keyPath: 'id' }); if (!d.objectStoreNames.contains('handoff')) d.createObjectStore('handoff', { keyPath: 'key' }); }; rq.onsuccess = () => res(rq.result); rq.onerror = () => rej(rq.error); });
    await new Promise((res, rej) => { const tx = db.transaction('handoff', 'readwrite'); tx.objectStore('handoff').put({ key: 'site:' + key, data, at: data.at }); tx.oncomplete = res; tx.onerror = () => rej(tx.error); });
    db.close();
  }, { key, data });
}
async function boot(site, aoi) {
  await page.goto(`${base}model3d.html?lat=${site.lat}&lon=${site.lon}&name=${encodeURIComponent(site.name)}&aoi=${aoi}&r=1500&fresh=1`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.site && window.gmApp.project.objects.length > 2, null, { timeout: 120000 });
  await page.waitForTimeout(600);
}
/** Everything a check wants to know about the project, read in one evaluate. */
const summarise = () => page.evaluate(() => {
  const p = window.gmApp.project, R = window.gmApp.R;
  const feats = p.byKind('points').find(o => o.role === 'features') || null;
  const glyphs = []; let azSprites = 0, pickCloud = 0;
  if (feats) { const L = R.layers.get(feats.id); azSprites = (L.azSprites || []).length; L.group.traverse(o => { if (o.userData.kind === 'features') glyphs.push({ sprite: !!o.isSprite, shape: o.userData.shape, key: o.userData.featureKey, rows: o.userData.rows || null, rowIndex: o.userData.rowIndex, az: o.userData.az, mapShape: o.material.map && o.material.map.userData.shape }); if (o.isPoints && o.userData.pickOnly) pickCloud = o.geometry.attributes.position.count; }); }
  return {
    aoi: p.site.aoi, warnings: p.metadata.warnings || [], notes: p.metadata.notes || [], geology_source: p.metadata.geology_source || null,
    meshes: p.byKind('mesh').filter(m => m.role === 'geology').map(m => ({ name: m.name, color: m.color, prov: m.provenance, meta: m.metadata, tri: m.nTriangles })),
    outlines: p.byKind('lineset').filter(l => l.role === 'geology-outline').length,
    faults: p.byKind('lineset').filter(l => l.role === 'faults').map(l => ({ parts: l.parts.length, features: l.features, prov: l.provenance })),
    features: feats ? { id: feats.id, name: feats.name, role: feats.role, group: feats.group, n: feats.n, cols: Object.keys(feats.attributes), typ: feats.attributes.typ, az: feats.attributes.az, nm: feats.attributes.nm, prov: feats.provenance, howto: feats.metadata.howto, featureKeys: R.layers.get(feats.id).featureKeys, counts: R.layers.get(feats.id).featureCounts, glyphs, azSprites, pickCloud } : null,
  };
});

try {
  /* ================= boot A: inside the Cassia bundle ================= */
  const keyA = keyOf(SH.name, SH.lat, SH.lon);
  check('key: the seeded hand-off key matches the viewer\'s', keyA === 'silver-hills-mine-42.147_-113.125', keyA);
  await seedHandoff(keyA, handoffA);
  await boot(SH, 'cassia');
  const A = await summarise();
  const fa = A.features;
  check('A features: role features, 2 rows, named and grouped under Mines', !!fa && fa.role === 'features' && fa.n === 2 && fa.name === 'Mine features (USMIN topo maps)' && fa.group === 'Mines', fa ? `${fa.role} ${fa.n} ${fa.name} / ${fa.group}` : 'no features layer');
  check('A features: typ / az / nm / quad / yr / scale / st columns kept', !!fa && ['typ', 'az', 'nm', 'quad', 'yr', 'scale', 'st'].every(c => fa.cols.includes(c)) && fa.typ[0] === 'Mine Shaft' && fa.az[1] === 45 && fa.az[0] == null, fa ? fa.cols.join(',') : '');
  check('A features: provenance says USMIN, surface locations only; howto explains the symbols', !!fa && /USGS USMIN/.test(fa.prov.source) && /surface locations only, no depth or extent/.test(fa.prov.source) && /square = shaft/.test(fa.howto || '') && /apex/.test(fa.howto || ''), fa ? fa.prov.source : '');
  check('A geology: the Cassia bundle is preferred (meshes cite the bundle, none the hand-off)', A.meshes.length >= 1 && A.meshes.every(m => m.prov.bundle === 'data/geology/cassia.json') && A.geology_source && A.geology_source.kind === 'bundle', `${A.meshes.length} meshes: ${[...new Set(A.meshes.map(m => m.prov.bundle))].join(' | ')}`);
  check('A geology: hand-off units skipped with a note naming the bundle and the counts', A.notes.some(n => /hand-off geology \(2 unit\(s\), 1 fault\(s\)\) not used: the cassia bundle covers this site/.test(n)) && A.geology_source.handoff_skipped && A.geology_source.handoff_skipped.units === 2, A.notes.find(n => /hand-off/.test(n)) || 'no note');
  check('A geology: no "no mapped geology" warning when the bundle drew', !A.warnings.some(w => /no mapped geology/.test(w)), A.warnings.join(' | '));
  check('A glyphs: featureKeys = [shaft, adit] in table order, with counts', !!fa && fa.featureKeys.join(',') === 'shaft,adit' && fa.counts.shaft === 1 && fa.counts.adit === 1, fa ? JSON.stringify(fa.featureKeys) : '');
  const sq = fa && fa.glyphs.find(g => !g.sprite && g.shape === 'square'), tri = fa && fa.glyphs.find(g => g.sprite && g.shape === 'triangle');
  check('A glyphs: the shaft is a square point sprite (row 0), the adit a rotated triangle sprite (row 1, az 45)', !!sq && sq.mapShape === 'square' && JSON.stringify(sq.rows) === '[0]' && sq.key === 'shaft' && !!tri && tri.mapShape === 'triangle' && tri.rowIndex === 1 && tri.az === 45 && fa.azSprites === 1, JSON.stringify(fa ? fa.glyphs : null));
  check('A glyphs: an invisible pick cloud holds every row', !!fa && fa.pickCloud === 2, fa ? String(fa.pickCloud) : '');

  // picking: project each row to the screen and pick it back — the index is the row
  const picks = await page.evaluate(async id => {
    const { THREE } = await import('./assets/geomodel/gm-render.js'); const R = window.gmApp.R, o = window.gmApp.project.get(id); const rect = R.canvas.getBoundingClientRect(); R.root.updateMatrixWorld(true); const out = [];
    for (let i = 0; i < o.n; i++) { const p = o.point(i); const v = new THREE.Vector3(...R.toSceneArr(p[0], p[1], p[2] + 1)); R.root.localToWorld(v); v.project(R.camera); const cx = rect.left + (v.x + 1) / 2 * rect.width, cy = rect.top + (1 - v.y) / 2 * rect.height; const hit = R.pick(cx, cy, ob => ob.id === o.id); out.push(hit ? { index: hit.index, obj: hit.obj && hit.obj.id === o.id } : null); }
    return out;
  }, fa ? fa.id : null);
  check('A pick: clicking each glyph returns its row index', picks.length === 2 && picks.every((h, i) => h && h.index === i && h.obj), JSON.stringify(picks));

  // The adit's apex follows the azimuth on the ground.  Plan view, north up:
  // az 45 is 45° clockwise from screen-up.  viewFrom('north') puts the camera
  // NORTH of the site looking south (8.5° down): east is then screen-left, so a
  // NE apex points left with a slight downward tilt; viewFrom('east') looks
  // west with north on the right, so the apex points right.  The apex on
  // screen is (−sin rot, cos rot).
  const rot = await page.evaluate(id => { const R = window.gmApp.R, sp = R.layers.get(id).azSprites[0]; const apex = () => { R.camera.updateMatrixWorld(); R.updateGlyphs(); const r = sp.material.rotation; return { deg: r * 180 / Math.PI, x: -Math.sin(r), y: Math.cos(r) }; }; R.viewFrom('top'); const plan = apex(); R.viewFrom('north'); const north = apex(); R.viewFrom('east'); const east = apex(); R.viewFrom('iso'); return { plan, north, east, scale: sp.scale.toArray(), ve: R.ve }; }, fa ? fa.id : null);
  check('A glyphs: the adit apex turns with the camera (plan −45°; from the north it points left; from the east, right)', Math.abs(rot.plan.deg + 45) < 0.5 && rot.north.x < -0.9 && Math.abs(rot.north.y) < 0.35 && rot.east.x > 0.9 && Math.abs(rot.east.y) < 0.35, JSON.stringify(rot));
  check('A glyphs: the sprite is fitted to 16 px and un-stretched under the vertical exaggeration', Math.abs(rot.scale[0] / rot.scale[1] - rot.ve) < 1e-6 && rot.scale[0] > 0.01 && rot.scale[0] < 0.03, JSON.stringify(rot.scale));

  // labels by the map name
  const labels = await page.evaluate(id => { const app = window.gmApp, o = app.project.get(id); const d = app.display.get(o.id) || {}; d.labels = true; app.display.set(o.id, d); app.syncObject(o); const L = app.R.layers.get(o.id); const t = []; L.group.traverse(s => { if (s.isSprite && s.userData.label) t.push(s.userData.text); }); d.labels = false; app.syncObject(o); return t.sort(); }, fa ? fa.id : null);
  check('A labels: switched on, the glyphs are labelled by nm', labels.join(',') === 'Test adit,Test shaft', labels.join(','));

  // the feature-type classifier, in page (the renderer imports three)
  const cls = await page.evaluate(async () => { const M = await import('./assets/geomodel/gm-render.js'); const t = ['Mine Shaft', 'Air Shaft', 'Adit', 'Tunnel', 'Open Pit Mine', 'Quarry', 'Strip Mine', 'Prospect Pit', 'Prospect', 'Gravel Pit', 'Placer', 'Mine Dump', 'Tailings', 'Mill', 'Smelter', 'Leach Pond', '', null]; return { keys: t.map(x => M.featureSymbol(x).key), shapes: M.FEATURE_SYMBOLS.map(s => s.shape), fields: M.FEATURE_SYMBOLS.every(s => s.match instanceof RegExp && s.key && s.label && s.shape && Array.isArray(s.color) && s.color.length === 3) }; });
  check('FEATURE_SYMBOLS: every row has match / key / label / shape / color and the eight shapes', cls.fields && cls.shapes.join(',') === 'square,triangle,diamond,triangle-down,circle,hexagon,cross,ring', cls.shapes.join(','));
  check('featureSymbol: shaft·shaft·adit·adit·openpit·openpit·openpit·prospect·prospect·placer·placer·dump·dump·mill·mill·other·other·other', cls.keys.join('·') === 'shaft·shaft·adit·adit·openpit·openpit·openpit·prospect·prospect·placer·placer·dump·dump·mill·mill·other·other·other', cls.keys.join('·'));

  // the triangle budget, exercised directly on the same topography
  const budget = await page.evaluate(async hg => { const S = await import('./assets/geomodel/gm-site.js'); const app = window.gmApp, p = app.project, topo = app.topoGrid(); const b = topo.bounds(); const conv = S.geologyFromHandoff(hg); const objs = S.geologyObjects(conv.geo, p.crs, topo, [b[0], b[1], b[3], b[4]], { aoi: 'map hand-off', bundle: conv.label, maxTriangles: 1 }); return { stats: objs.stats, meshes: objs.filter(o => o.kind === 'mesh').length, outlines: objs.filter(o => o.role === 'geology-outline').length, label: conv.label }; }, handoffA.geology);
  check('geologyObjects: when the triangle budget bites, later units are outlines only and stats say so', budget.stats.units === 1 && budget.stats.units_over_budget === 1 && budget.meshes === 1 && budget.outlines === 2 && budget.label === 'USGS geology via map hand-off (SGMC, USGS Quaternary faults)', JSON.stringify(budget));

  /* ================= boot B: outside every bundle ================= */
  const keyB = keyOf(OUT.name, OUT.lat, OUT.lon);
  await seedHandoff(keyB, handoffB);
  await boot(OUT, 'auto');
  const B = await summarise();
  check('B site: aoi=auto resolves to no bundle outside every AOI', B.aoi === null, String(B.aoi));
  const byName = Object.fromEntries(B.meshes.map(m => [m.name, m]));
  const K = byName['Cretaceous granodiorite'], N = byName['Numeric-age volcanics'], G = byName['Gronk unit'];
  check('B geology: three hand-off units draped as role geology meshes with outlines', B.meshes.length === 3 && B.outlines === 3 && B.meshes.every(m => m.tri > 0), `${B.meshes.length} meshes, ${B.outlines} outlines`);
  check('B geology: provenance names the hand-off and its sources; geology_source says handoff', B.meshes.every(m => m.prov.bundle === 'USGS geology via map hand-off (SGMC, USGS Quaternary faults)' && /viewport snapshot/.test(m.prov.handoff)) && B.geology_source && B.geology_source.kind === 'handoff' && B.geology_source.units === 3 && B.geology_source.faults === 1, B.meshes[0] ? B.meshes[0].prov.bundle : '');
  check('B ages: Cretaceous → t0 145 / t1 66 from the table', !!K && K.meta.t0_ma === 145 && K.meta.t1_ma === 66 && /table/.test(K.meta.age_basis), K ? `${K.meta.t0_ma}/${K.meta.t1_ma} ${K.meta.age_basis}` : 'missing');
  check('B ages: numeric age_max / age_min → t0 20 / t1 12, url and scale carried, #AABBCC kept', !!N && N.meta.t0_ma === 20 && N.meta.t1_ma === 12 && N.meta.source_url === 'https://example.invalid/sgmc' && N.meta.source_scale === '1:500,000' && JSON.stringify(N.color) === '[170,187,204]', N ? JSON.stringify([N.meta.t0_ma, N.meta.t1_ma, N.meta.source_url, N.meta.source_scale, N.color]) : 'missing');
  check('B ages: an unreadable age leaves t0/t1 null and warns on the unit and the project', !!G && G.meta.t0_ma === null && G.meta.t1_ma === null && (G.meta.warnings || []).some(w => /age not read for unit 'Gronk unit'.*Gronkian/.test(w)) && B.warnings.some(w => /age text not read for 1 hand-off unit\(s\) \(Gronk unit\)/.test(w)), G ? (G.meta.warnings || []).join(' | ') : 'missing');
  check('B colour: a unit without a valid hex gets the stable hash colour, not a default', !!K && !!G && K.color.every(c => c >= 128) && G.color.every(c => c >= 128) && JSON.stringify(K.color) !== JSON.stringify(G.color), `${JSON.stringify(K && K.color)} ${JSON.stringify(G && G.color)}`);
  check('B faults: one draped part with name / type / age / slip, provenance naming the hand-off', B.faults.length === 1 && B.faults[0].parts === 1 && B.faults[0].features[0].name === 'Outside fault' && B.faults[0].features[0].type === 'normal' && B.faults[0].features[0].age === 'Quaternary' && B.faults[0].features[0].slip_sense === 'normal' && /map hand-off/.test(B.faults[0].prov.bundle), JSON.stringify(B.faults));
  check('B warnings: no "no mapped geology" warning when the hand-off drew', !B.warnings.some(w => /no mapped geology/.test(w)), B.warnings.join(' | '));
  const fb = B.features;
  check('B features: 7 rows; featureKeys in table order', !!fb && fb.n === 7 && fb.featureKeys.join(',') === 'adit,openpit,placer,prospect,dump,mill,other', fb ? fb.featureKeys.join(',') : 'none');
  const shapesB = fb ? fb.glyphs.filter(g => !g.sprite).map(g => g.shape).sort().join(',') : '';
  check('B glyphs: one point sprite per shape (a tunnel without an azimuth is an unrotated triangle), no rotated sprites', shapesB === 'circle,cross,diamond,hexagon,ring,triangle,triangle-down' && fb.azSprites === 0 && fb.glyphs.every(g => g.sprite || g.mapShape === g.shape), shapesB);
  check('B glyphs: every glyph carries its rows, and the rows cover every feature once', !!fb && fb.glyphs.filter(g => !g.sprite).flatMap(g => g.rows).sort((a, b) => a - b).join(',') === '0,1,2,3,4,5,6', fb ? JSON.stringify(fb.glyphs.map(g => g.rows)) : '');

  /* ================= boot C: nothing handed over, outside every bundle ================= */
  await boot({ lon: OUT.lon, lat: OUT.lat, name: 'Bare site' }, 'auto');
  const C = await summarise();
  check('C: with neither a bundle nor a hand-off the project warns that it has no mapped geology', C.warnings.includes('no mapped geology in this model — open it from the map with USGS GEOLOGY on and the site in view, or drop a geology file') && C.meshes.length === 0 && C.faults.length === 0 && C.geology_source && C.geology_source.kind === 'none' && C.features === null, C.warnings.join(' | '));

  const pageErrors = errors.filter(e => !/WebGL|GPU|swiftshader|GL_|Automatic fallback|THREE\.WebGLRenderer|favicon/i.test(e));
  check('no page errors', pageErrors.length === 0, pageErrors.slice(0, 5).join(' || '));
} catch (e) {
  check('run', false, String(e && e.stack || e));
} finally {
  await browser.close(); server.kill();
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
