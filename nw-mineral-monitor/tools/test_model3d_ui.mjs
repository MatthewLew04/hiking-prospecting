#!/usr/bin/env node
/* Headless acceptance test for the model3d.html shell (build 2026-09-02-ui2):
   the split right column (layer inspector + closable tool host), the one
   arming path and its mode strip, Esc semantics, the workflow-ordered TOOLS
   menu with readiness, the progress card, the banded layer tree with
   not-started groups, filter and badges, the PICKED card, delete with UNDO,
   the confidence key with a single class, opening on the workings with the
   ground thinned, saved scenes, the rendered image and the shared legend
   model.  State is checked, never pixels (swiftshader paints tiles black).
   Run: CHROME_PATH=… node tools/test_model3d_ui.mjs */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const F = await import(path.join(ROOT, 'site/assets/geomodel/gm-formats.js'));
const PORT = 9100 + Math.floor(Math.random() * 300);
let failed = 0;
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); };

async function terrariumTile() { const rgba = new Uint8Array(256 * 256 * 4); for (let y = 0; y < 256; y++) for (let x = 0; x < 256; x++) { const elev = 1500 + 0.8 * (x - 128) + 0.2 * (y - 128); const v = Math.round((elev + 32768) * 256); const o = (y * 256 + x) * 4; rgba[o] = (v >> 16) & 255; rgba[o + 1] = (v >> 8) & 255; rgba[o + 2] = v & 255; rgba[o + 3] = 255; } return F.encodePNG(256, 256, rgba, { channels: 4 }); }
async function flatTile(r, g, b) { const rgba = new Uint8Array(256 * 256 * 4); for (let i = 0; i < 256 * 256; i++) { rgba[4 * i] = r; rgba[4 * i + 1] = g; rgba[4 * i + 2] = b; rgba[4 * i + 3] = 255; } return F.encodePNG(256, 256, rgba, { channels: 4 }); }

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
try {
  await page.goto(`${base}model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills%20mine&gi=157&aoi=cassia&r=1500&fresh=1`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 3, null, { timeout: 120000 });
  await page.waitForTimeout(800);

  /* ---------- the shell ---------- */
  const shell = await page.evaluate(() => ({ toolhostHidden: document.getElementById('toolhost').hidden, card: document.querySelector('#inspector .steps') ? document.querySelectorAll('#inspector .step').length : 0, bands: [...document.querySelectorAll('#layers .lband')].map(b => b.textContent), notStarted: [...document.querySelectorAll('#layers .lgroup .ns')].map(s => s.parentElement.textContent.trim().slice(0, 40)), viewtools: document.querySelectorAll('#viewtools button').length, viewinfo: document.getElementById('viewinfo').textContent, saved: document.getElementById('savestat').textContent }));
  check('shell: tool host hidden, progress card lists the 9 steps', shell.toolhostHidden && shell.card === 9, JSON.stringify({ card: shell.card }));
  check('tree: banded INPUTS / MODELS / OUTPUTS with not-started step groups', shell.bands.join(',') === 'INPUTS,MODELS,OUTPUTS' && shell.notStarted.some(s => /Images.*step 1/.test(s)) && shell.notStarted.some(s => /Block models.*step 8/.test(s)), shell.notStarted.join(' | '));
  check('viewport: look-from / projection / fit buttons and a status bar naming the projection', shell.viewtools >= 11 && /perspective|orthographic/.test(shell.viewinfo), shell.viewinfo);

  /* ---------- sections start hidden ---------- */
  const secs = await page.evaluate(() => window.gmApp.project.byKind('section').map(s => [s.name, s.visible]));
  check('sections: the two presets start hidden', secs.length === 2 && secs.every(s => s[1] === false), JSON.stringify(secs));

  /* ---------- opening a tool leaves the inspector alone ---------- */
  await page.evaluate(() => { const m = window.gmApp.project.byKind('points').find(p => p.role === 'mines'); window.gmApp.select(m.id); window.gmApp.tools.open('workings'); });
  const open = await page.evaluate(() => ({ hidden: document.getElementById('toolhost').hidden, ttl: document.querySelector('#toolhost .ttl').textContent, insp: document.getElementById('inspector').textContent.includes('POINTS'), needs: document.querySelectorAll('#toolbody .need').length, selected: window.gmApp.selected }));
  check('tool host: opens with STEP n title and NEEDS strip, inspector keeps the selected layer', !open.hidden && /STEP 2\/9/.test(open.ttl) && /WORKINGS/.test(open.ttl) && open.insp && open.needs >= 1 && !!open.selected, open.ttl);
  const noLayer = await page.evaluate(() => window.gmApp.project.byKind('lineset').filter(l => l.role === 'workings').length);
  check('workings: opening the panel creates no layer', noLayer === 0, `${noLayer} workings layers`);

  /* ---------- arming, the mode strip, Esc semantics ---------- */
  await page.evaluate(() => window.gmApp.tools.workings.start('trace'));
  const armed = await page.evaluate(() => ({ armed: !!window.gmApp.tools.armed, bar: document.getElementById('modebar').style.display, text: document.getElementById('modebar').textContent, cursor: document.getElementById('gl').style.cursor, mode: document.querySelector('#toolhost .mode').textContent }));
  check('arm: mode strip says what the clicks do, where they go and at what confidence', armed.armed && armed.bar === 'flex' && /TRACE/.test(armed.text) && /sketched/.test(armed.text) && armed.cursor === 'crosshair' && /ARMED/.test(armed.mode), armed.text.slice(0, 80));
  await page.keyboard.press('Escape');
  const esc1 = await page.evaluate(() => ({ armed: !!window.gmApp.tools.armed, hidden: document.getElementById('toolhost').hidden, bar: document.getElementById('modebar').style.display }));
  check('Esc: first press disarms and keeps the tool open', !esc1.armed && !esc1.hidden && esc1.bar === 'none');
  await page.keyboard.press('Escape');
  const esc2 = await page.evaluate(() => ({ hidden: document.getElementById('toolhost').hidden, active: !!window.gmApp.tools.active, insp: document.getElementById('inspector').textContent.includes('POINTS') }));
  check('Esc: second press closes the tool and the inspector still shows the layer', esc2.hidden && !esc2.active && esc2.insp);
  await page.evaluate(() => { window.gmApp.tools.open('section'); document.getElementById('toolClose').click(); });
  const closed = await page.evaluate(() => document.getElementById('toolhost').hidden && !window.gmApp.tools.active);
  check('DONE ✕ closes the tool', closed);

  /* ---------- TOOLS menu order + readiness ---------- */
  await page.click('#btnTools');
  const menuState = await page.evaluate(() => { const m = document.querySelector('.ctxmenu'); return { heads: [...m.querySelectorAll('.hdr')].map(x => x.textContent), items: [...m.querySelectorAll('.mi')].map(x => x.textContent.trim().slice(0, 60)) }; });
  await page.keyboard.press('Escape');
  check('TOOLS ▾: grouped in workflow order, numbered, with readiness hints', menuState.heads.slice(0, 4).join('|') === 'FROM THE MAP|FROM THE GEOLOGY|VOLUMES|SEE IT' && /^1 +Georeference/.test(menuState.items[0]) && /needs structural data/.test(menuState.items.find(i => /Stereonet/.test(i)) || ''), menuState.items.slice(0, 4).join(' / '));
  const readiness = await page.evaluate(() => window.gmApp.tools.readiness());
  check('readiness: stereonet blocked without structural data, section ready', readiness.stereonet.state === 'blocked' && readiness.section.state === 'ready' && readiness.georef.state === 'ready', JSON.stringify(Object.fromEntries(Object.entries(readiness).map(([k, v]) => [k, v.state]))));

  /* ---------- workings: commit creates the layer; badges; legend key ---------- */
  const wk = await page.evaluate(async () => {
    const app = window.gmApp, E = await import('./assets/geomodel/gm-engine.js'); const topo = app.topoGrid(); const o = app.project.origin;
    const ws = app.tools.workings.ensureLayer();
    E.addAdit(ws, [o[0] - 200, o[1] - 100, 0], 45, 900, { gradePct: 0.5, unitsIn: 'ft', terrain: topo, name: 'No. 1 adit', confidence: 'described', source: { doc: 'USGS Bull 1', page: 12, quote: 'An adit driven N45E for 900 feet.' } });
    E.addShaft(ws, [o[0], o[1], 0], 300, { dipDeg: 90, unitsIn: 'ft', terrain: topo, name: 'Main shaft', confidence: 'described' });
    app.refresh(ws); app.renderLegend();
    return { layers: app.project.byKind('lineset').filter(l => l.role === 'workings').length, legend: document.getElementById('legend').innerText, rows: [...document.querySelectorAll('#layers .lrow')].map(r => r.textContent), tally: app.tools.readiness().workings };
  });
  check('workings: the layer exists once a feature is committed', wk.layers === 1);
  check('legend: confidence key shows with a single class present, working types named', /described · 2/.test(wk.legend) && /surveyed · 0/.test(wk.legend) && /working types/.test(wk.legend) && /adit/.test(wk.legend) && /shaft/.test(wk.legend), wk.legend.replace(/\n/g, ' | ').slice(0, 120));
  check('readiness: workings step is done with a HAS string', wk.tally.state === 'done' && /2 workings/.test(wk.tally.has), wk.tally.has);
  const badge = await page.evaluate(async () => { const app = window.gmApp, E = await import('./assets/geomodel/gm-engine.js'); const empty = E.newWorkings('empty test layer', app.project.name); empty.group = 'Workings'; app.project.add(empty); const r = [...document.querySelectorAll('#layers .lrow')].find(x => /empty test layer/.test(x.textContent)); const t = r ? r.querySelector('.st.empty') ? r.querySelector('.st.empty').title : 'no badge' : 'no row'; app.project.remove(empty); return t; });
  check('tree: an empty layer carries the ∅ badge with a plain-language title', /nothing digitised/.test(badge), badge.slice(0, 60));

  /* ---------- picking: card under the layer title, highlight, source sentence ---------- */
  const pick = await page.evaluate(async () => {
    const app = window.gmApp, V = await import('./assets/geomodel/gm-viewer.js');
    const ws = app.project.byKind('lineset').find(l => l.role === 'workings');
    const lines = V.describePick({ obj: ws, object: { userData: {} }, index: null, partIndex: 0 });
    app.picked = { obj: ws, index: 0, lines, quote: ws.features[0].source.quote, bounds: null }; app.R.highlight(ws, 0); app.select(ws.id);
    const insp = document.getElementById('inspector'); const kids = [...insp.querySelector('.insp').children].map(c => c.className);
    return { lines, pickIndex: kids.findIndex(c => /pick/.test(c)), badgesIndex: kids.findIndex(c => /badges/.test(c)), text: insp.textContent, hl: app.R.hl ? app.R.hl.children.length : 0 };
  });
  check('pick: readout names the confidence in words and the source', pick.lines.some(l => /confidence: described/.test(l)) && pick.lines.some(l => /USGS Bull 1/.test(l)), pick.lines.join(' | ').slice(0, 120));
  check('pick: the card sits under the badges, quotes the sentence, and the part is highlighted', pick.pickIndex > pick.badgesIndex && pick.pickIndex <= pick.badgesIndex + 2 && /An adit driven N45E/.test(pick.text) && pick.hl >= 1, `card at ${pick.pickIndex}, badges at ${pick.badgesIndex}, hl ${pick.hl}`);

  /* ---------- delete with UNDO ---------- */
  const und = await page.evaluate(() => { const app = window.gmApp; const ws = app.project.byKind('lineset').find(l => l.role === 'workings'); const id = ws.id; app.destructive(`deleted ${ws.name}`, () => app.project.remove(ws), () => app.project.add(ws)); const gone = !app.project.get(id); const toastHas = !!document.querySelector('#toasts .act'); app.undo(); return { gone, toastHas, back: !!app.project.get(id), n: app.history.length }; });
  check('undo: delete goes through the toast with UNDO and comes back', und.gone && und.toastHas && und.back && und.n === 0, JSON.stringify(und));

  /* ---------- filter, right-click menu ---------- */
  await page.fill('#lfilter', 'topo');
  const filt = await page.evaluate(() => document.querySelectorAll('#layers .lrow').length);
  await page.fill('#lfilter', '');
  check('tree: the filter narrows the rows', filt === 1, `${filt} rows for "topo"`);
  await page.evaluate(() => { const r = document.querySelector('#layers .lrow'); r.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 100, clientY: 100 })); });
  const ctx = await page.evaluate(() => { const m = document.querySelector('.ctxmenu'); const t = m ? [...m.querySelectorAll('.mi')].map(x => x.textContent) : []; if (m) m.remove(); return t; });
  check('tree: right-click opens the layer menu with Show only this and Delete', ctx.some(t => /Show only this/.test(t)) && ctx.some(t => /Delete/.test(t)), ctx.slice(0, 4).join(' / '));

  /* ---------- keys, views, projection ---------- */
  await page.click('#gl', { position: { x: 400, y: 300 } });
  await page.keyboard.press('o');
  const proj = await page.evaluate(() => ({ p: window.gmApp.R.projection, btn: document.getElementById('projBtn').textContent, info: document.getElementById('viewinfo').textContent }));
  check('keys: o switches to orthographic and the status bar says so', proj.p === 'ortho' && proj.btn === 'ORTHO' && /orthographic/.test(proj.info), proj.info);
  await page.keyboard.press('d');
  const top = await page.evaluate(() => { const c = window.gmApp.R.camera; return { up: c.up.toArray().map(v => +v.toFixed(2)), dir: c.getWorldDirection(new (c.position.constructor)()).toArray().map(v => +v.toFixed(2)) }; });
  check('keys: d gives a plan view with north up', top.up[2] === -1 && top.dir[1] === -1, JSON.stringify(top));
  await page.keyboard.press('u');
  const below = await page.evaluate(() => { const c = window.gmApp.R.camera; return c.getWorldDirection(new (c.position.constructor)()).y > 0.9; });
  check('keys: u looks from below', below);
  await page.keyboard.press('p');

  /* ---------- scenes ---------- */
  const scene = await page.evaluate(() => { const app = window.gmApp; const v0 = app.R.getView(); app.project.metadata.scenes = [{ id: 's1', name: 'test scene', created: new Date().toISOString(), view: v0, imagery: app.imagery, legend: true, seeThrough: false, visible: {}, opacity: {}, section: null, selected: null }]; app.R.viewFrom('north'); const moved = app.R.camera.position.distanceTo(new (app.R.camera.position.constructor)(...v0.position)) > 1; app.setSeeThrough(false); const before = JSON.stringify(app.R.getView().position); app.R.setView(v0); return { moved, restored: JSON.stringify(app.R.getView().position.map(v => +v.toFixed(1))) === JSON.stringify(v0.position.map(v => +v.toFixed(1))) }; });
  check('scenes: a saved view is restored exactly', scene.moved && scene.restored, JSON.stringify(scene));

  /* ---------- render image with overlays ---------- */
  const img = await page.evaluate(() => { const app = window.gmApp; const c = app.renderImage({ scale: 1 }); const M = app.legendModel(); const t = app.confidenceSentence({ surveyed: 0, described: 2, assumed: 0 }); return { w: c.width, h: c.height, scale: !!M.scale, conf: M.confidence.length, sentence: t }; });
  check('render image: a canvas of the viewport size, and the legend model carries scale + confidence', img.w > 200 && img.h > 200 && img.scale && img.conf === 3 && /NOT A SURVEY — 2 of 2 workings are digitised/.test(img.sentence), img.sentence.slice(0, 60));

  /* ---------- open on the workings ---------- */
  const zoom = await page.evaluate(() => { const app = window.gmApp; const d0 = app.R.camera.position.distanceTo(app.R.controls.target); const ok = app.zoomToWorkings(); const d1 = app.R.camera.position.distanceTo(app.R.controls.target); app.setSeeThrough(true); return { ok, closer: d1 < d0, topoOpacity: app.topoGrid().opacity }; });
  check('workings: Zoom to workings moves closer and see-through thins the ground', zoom.ok && zoom.closer && zoom.topoOpacity < 1, JSON.stringify(zoom));

  /* ---------- described-workings model opens on its workings ---------- */
  await page.goto(`${base}model3d.html?project=/models/tonopah-divide-mine-4c141151/model.geomodel.json`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.gmApp && window.gmApp.project && window.gmApp.project.objects.length > 0, null, { timeout: 120000 });
  await page.waitForTimeout(800);
  const desc = await page.evaluate(() => { const app = window.gmApp; const topo = app.topoGrid(); const b = app.project.bounds(); const d = app.R.camera.position.distanceTo(app.R.controls.target); const size = Math.max(b[3] - b[0], b[4] - b[1]); return { seeThrough: app.seeThrough, topoOpacity: topo.opacity, closeIn: d < size * 1.2, banner: document.getElementById('confbar').innerText.slice(0, 40), legend: document.getElementById('legend').innerText }; });
  check('described model: opens close to the workings with see-through ground and the banner up', desc.seeThrough && desc.topoOpacity < 1 && desc.closeIn && /NOT A SURVEY/.test(desc.banner) && /described · 4/.test(desc.legend), JSON.stringify({ s: desc.seeThrough, o: desc.topoOpacity, c: desc.closeIn }));

  check('no page errors', errors.length === 0, errors.slice(0, 3).join(' ; '));
} catch (e) { failed++; console.log('FAIL  harness error —', e.stack || e); }
finally { await browser.close(); server.kill(); }
console.log(failed ? `\n${failed} check(s) failed` : '\nmodel3d ui: all checks passed');
process.exit(failed ? 1 : 0);
