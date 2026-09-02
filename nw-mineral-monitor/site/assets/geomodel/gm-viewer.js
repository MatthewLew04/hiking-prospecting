/* gm-viewer.js — the 3-D model app (site/model3d.html).
   Opens from a mine card (?lat&lon&name&gi&aoi&r) or a project (?project=url |
   ?key=saved id | drag-drop .geomodel.json / .omf / any supported file).
   Wires: gm-core (objects), gm-site (bootstrap), gm-render (three.js),
   gm-formats (I/O), gm-engine via a Web Worker (modelling), gm-tools (tools). */
import * as GM from './gm-core.js';
import * as F from './gm-formats.js';
import * as E from './gm-engine.js';
import * as SITE from './gm-site.js';
import { Renderer, canvasTexture, THREE } from './gm-render.js';
import * as GMR from './gm-render.js';
import { h, clear, row, num, txt, sel, btn, range, note, kv, section, toast, modal, menu, colorInput, fmtNum, confirmModal, promptModal, lineSample } from './gm-ui.js';
import { Tools, TOOL_STEPS, stepOf } from './gm-tools.js';
import * as ST from './gm-structural.js';

const $ = id => document.getElementById(id);
const BUILD = '2026-09-02-ui2';
/* The tree is a workflow: inputs first, then what the steps produce, then the
   outputs.  Groups that a step fills are always shown, empty, with the step
   to open — a first-time user sees where the workings will go before there
   are any. */
const BANDS = [
  { name: 'INPUTS', groups: ['Topography', 'Images', 'Geology (draped)', 'Geology outlines', 'Structure', 'Drillholes', 'Mines', 'Claims', 'Imports'] },
  { name: 'MODELS', groups: ['Workings', 'Stratigraphy', 'Surfaces', 'Block models'] },
  { name: 'OUTPUTS', groups: ['Sections', 'Notes', 'Other'] },
];
const GROUP_ORDER = BANDS.flatMap(b => b.groups);
const STEP_GROUPS = { Images: 'georef', Workings: 'workings', Structure: 'structure', Stratigraphy: 'strat', 'Block models': 'blocks', Sections: 'section' };

export const app = {
  project: null, R: null, engine: null, tools: null,
  display: new Map(),           // obj.id -> display options
  selected: null,               // obj id
  picked: null,                 // the last scene pick, shown as a card in the inspector
  imagery: 'sat', imageryTex: null, topoId: null, showLegend: true,
  dirty: false, saveTimer: null, key: null, savedAt: null,
  hover: null, history: [], layerFilter: '', seeThrough: false,
};
window.gmApp = app;             // for headless tests / console

/* ----------------------------------------------------------------- boot */
async function boot() {
  app.engine = new E.EngineClient(new URL('./gm-worker.js', import.meta.url).href);
  app.R = new Renderer($('gl'), { origin: [0, 0, 0], ve: 1.5 });
  app.tools = new Tools(app);
  wireChrome();
  const q = new URLSearchParams(location.search);
  status('starting…');
  try {
    if (q.get('project')) { await loadProjectUrl(q.get('project')); }
    else if (q.get('key')) { const p = await GM.store.loadProject(q.get('key')); if (p) setProject(p, q.get('key')); else throw new Error('no saved project ' + q.get('key')); }
    else if (q.get('lat') && q.get('lon')) { await buildFromParams(q); }
    else { await showStart(); }
  } catch (e) { console.error(e); toast('start failed: ' + e.message, 'err', 8000); status('start failed: ' + e.message); await showStart(); }
}

async function buildFromParams(q) {
  const lat = +q.get('lat'), lon = +q.get('lon'); const name = q.get('name') || ''; const radius = +(q.get('r') || 2500);
  const gi = q.get('gi'); const aoi = q.get('aoi') || 'auto';
  const key = GM.slug(name || 'site') + '-' + lat.toFixed(3) + '_' + lon.toFixed(3);
  if (!q.get('fresh')) { const saved = await GM.store.loadProject(key).catch(() => null); if (saved) { setProject(saved, key); toast('restored your saved model for this site — PROJECTS ▾ > Rebuild from map data starts over', 'info', 6000); return; } }
  const handoff = await GM.store.takeHandoff('site:' + key).catch(() => null);
  const { project } = await SITE.buildSiteProject(lon, lat, { radius, name, gi, aoi, zoom: +(q.get('zoom') || 13), handoff, onprog: status });
  setProject(project, key);
  // the site builder's warnings (grades table unavailable, tiles unreachable)
  // used to be written into metadata and never shown
  for (const w of (project.metadata.warnings || []).slice(0, 3)) toast(w, 'warn', 7000);
  markDirty();
}

async function loadProjectUrl(url) {
  status('loading project…'); const r = await fetch(url); if (!r.ok) throw new Error(`${url}: ${r.status}`);
  const name = url.split('/').pop(); const bytes = new Uint8Array(await r.arrayBuffer());
  await importBytes(name, bytes, { asProject: true });
}

/* The start screen can be dismissed and has no reopen path, so the header
   buttons have to cope with there being no project rather than throwing on a
   null dereference — a menu that silently does nothing reads as a dead button. */
function needProject() {
  if (app.project) return true;
  showStart();
  return false;
}

async function showStart() {
  const list = await GM.store.listProjects().catch(() => []);
  const zoneIn = num(12, { min: 1, max: 60, step: 1, style: { maxWidth: '70px' } }); const hemi = sel([['n', 'north'], ['s', 'south']], 'n');
  const body = h('div', {},
    h('p', {}, 'Open this page from a mine card on the map (OPEN 3D MODEL), drop a project or data file here, or pick a saved model:'),
    h('div', { class: 'plist' }, ...(list.length ? list.map(p => h('div', { class: 'pitem', onclick: async () => { m.close(); const pr = await GM.store.loadProject(p.id); setProject(pr, p.id); } }, h('b', {}, p.name), h('span', {}, `${p.modified.slice(0, 16)} · ${(p.bytes / 1e6).toFixed(1)} MB`))) : [h('div', { class: 'note' }, 'no saved models yet')])),
    h('div', { class: 'frow' }, h('span', { class: 'mono' }, 'UTM zone'), zoneIn, hemi, btn('NEW EMPTY PROJECT', () => { m.close(); const z = Math.max(1, Math.min(60, +zoneIn.value || 12)); const nm = `new model (UTM ${z}${hemi.value === 's' ? 'S' : 'N'})`; setProject(new GM.Project({ name: nm, crs: GM.utm.crs(z, hemi.value !== 's'), origin: [0, 0, 0] }), GM.slug(nm) + '-' + Date.now().toString(36)); }), btn('IMPORT FILES…', () => { m.close(); $('fileIn').click(); })),
    h('p', { class: 'note' }, 'Deep link: model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills&r=2500'));
  const m = modal('3D GEOLOGICAL MODEL', body, { sticky: false });
}

/* -------------------------------------------------------------- project */
export function setProject(p, key) {
  if (app.project) { for (const o of app.project.objects) app.R.remove(o.id); }
  app.project = p; app.key = key || (p.site && p.site.key) || GM.slug(p.name);
  if (p.site) p.site.key = app.key;
  p.ensureOrigin(); app.R.origin = p.origin || [0, 0, 0];
  app.display.clear(); app.history = []; app.picked = null; app.selected = null; app.confBarDismissed = false; app.seeThrough = false;
  const savedDisplay = (p.metadata && p.metadata.display) || null;
  if (savedDisplay) for (const [k, v] of Object.entries(savedDisplay)) if (v && typeof v === 'object') app.display.set(k, Object.assign({}, v));
  const topo = p.byKind('grid2d').find(g => g.role === 'topography'); app.topoId = topo ? topo.id : null;
  for (const o of p.objects) syncObject(o);
  p.on((type, obj) => { if (type === 'add') { syncObject(obj); } else if (type === 'remove') { app.R.remove(obj.id); app.display.delete(obj.id); if (app.selected === obj.id) select(null); } renderLayers(); renderConfidence(); markDirty(); });
  renderLayers(); renderHeader(); renderConfidence();
  const b = p.bounds(); if (b) app.R.fitTo(b);
  if (topo) applyImagery(app.imagery);
  status(`${p.name} — ${p.objects.length} objects · UTM ${p.crs.zone || ''}${p.crs.north === false ? 'S' : 'N'}`);
  app.tools.onProject();
  renderInspector(); renderViewInfo();
  openOnWorkings();
}

/* A described-workings model used to open on the whole terrain box with the
   ground opaque, so the four dashed workings it exists to show were not on
   screen at all.  Fit the camera to the workings and thin the ground. */
function workingsBounds() {
  if (!app.project) return null;
  let b = null;
  for (const o of app.project.objects) {
    if (!((o.kind === 'lineset' && o.role === 'workings' && o.parts.length) || (o.kind === 'mesh' && o.role === 'stope'))) continue;
    const ob = o.bounds(); if (!ob) continue;
    b = b ? [Math.min(b[0], ob[0]), Math.min(b[1], ob[1]), Math.min(b[2], ob[2]), Math.max(b[3], ob[3]), Math.max(b[4], ob[4]), Math.max(b[5], ob[5])] : ob.slice();
  }
  if (!b) return null;
  const pad = Math.max(150, 0.25 * Math.max(b[3] - b[0], b[4] - b[1], b[5] - b[2]));
  return [b[0] - pad, b[1] - pad, b[2] - pad, b[3] + pad, b[4] + pad, b[5] + pad];
}
export function zoomToWorkings() { const b = workingsBounds(); if (!b) { toast('no workings in this model yet — TOOLS ▾ > 2 Workings from maps', 'warn'); return false; } app.R.fitTo(b, 1.1); return true; }
export function setSeeThrough(on) {
  const topo = topoGrid(); if (!topo) return;
  app.seeThrough = !!on; topo.opacity = on ? 0.55 : 1; app.R.setOpacity(topo.id, topo.opacity);
  for (const m of app.project.byKind('mesh')) if (m.role === 'geology') { m.opacity = on ? 0.4 : 1; app.R.setOpacity(m.id, m.opacity); }
  markDirty();
}
function openOnWorkings() {
  const t = GMR.confidenceTally(app.project); const n = t.surveyed + t.described + t.assumed;
  if (!n || !zoomToWorkings()) return;
  setSeeThrough(true);
  toast(`${n} working${n > 1 ? 's are' : ' is'} below the surface — ground shown at 55 % so they read. VIEW ▾ > Solid ground restores it.`, 'info', 7000);
}

export function syncObject(o) {
  const d = app.display.get(o.id) || defaultDisplay(o); app.display.set(o.id, d);
  if (o.kind === 'grid2d' && o.role === 'property' && !d.topo) d.topo = topoGrid();
  if (o.kind === 'grid2d' && o.role === 'topography') d.texture = app.imageryTex && app.imagery !== 'none' ? app.imageryTex : null;
  app.R.sync(o, d);
}
function defaultDisplay(o) {
  if (o.kind === 'grid2d') return o.role === 'property' ? { mode: 'draped', colormap: 'turbo', lift: 2 } : o.role === 'topography' ? { colorBy: 'elevation', colormap: 'terrain' } : { colorBy: 'flat' };
  if (o.kind === 'points') return o.role === 'structural' ? { sides: 16, tick: true, attribute: 'dip', colormap: 'turbo' } : o.role === 'trend' ? { sides: 20, tick: false } : { size: o.role === 'mines' ? 14 : 9, labels: o.role === 'mines' && o.n <= 60 };
  if (o.kind === 'blockmodel') return { colormap: 'turbo', shrink: 0.92 };
  if (o.kind === 'lineset') return { tubes: o.role === 'workings' || o.role === 'drillhole-traces' };
  return {};
}
export function topoGrid() { return app.topoId ? app.project.get(app.topoId) : app.project.byKind('grid2d').find(g => g.role === 'topography') || null; }
// renderConfidence() belongs here, not only on add/remove: the workings tools
// mutate an existing lineset in place and then call refresh(), so a working
// digitised by hand fires no project event at all.  Without this the "not a
// survey" banner never appears for the very workings the user drew.
export function refresh(o) { syncObject(o); renderLayers(); renderConfidence(); markDirty(); }
export function markDirty() { app.dirty = true; clearTimeout(app.saveTimer); app.saveTimer = setTimeout(saveProject, 2500); $('saveBtn').classList.add('dirty'); renderSaveStat('unsaved changes'); }
function renderSaveStat(text, err = false) { const el = $('savestat'); if (!el) return; el.textContent = text || ''; el.classList.toggle('err', !!err); el.title = err ? text : (app.savedAt ? `last saved ${app.savedAt} in this browser (IndexedDB). Use EXPORT for files.` : ''); }
export async function saveProject(explicit = false) {
  if (!app.project) return;
  try {
    app.project.site = app.project.site || {}; app.project.site.key = app.key;
    // display settings (colormap, attribute, cut-offs, glyph size, labels...)
    // are part of the project, so a reload looks like what you left
    const disp = {};
    for (const [id, d] of app.display) {
      if (!app.project.get(id)) continue;
      const keep = {};
      for (const [k, v] of Object.entries(d)) { if (v == null) continue; if (typeof v === 'object' && !Array.isArray(v)) continue; if (k === 'range' || k === 'topo' || k === 'texture') continue; keep[k] = v; }
      if (Object.keys(keep).length) disp[id] = keep;
    }
    app.project.metadata.display = disp;
    await GM.store.saveProject(app.project); app.dirty = false; $('saveBtn').classList.remove('dirty');
    app.savedAt = new Date().toTimeString().slice(0, 5); renderSaveStat(`saved ${app.savedAt}`);
    if (explicit) toast('saved in this browser (IndexedDB). Use EXPORT for files.', 'ok'); }
  catch (e) { console.warn(e); renderSaveStat('save failed: ' + e.message, true); toast('save failed: ' + e.message + ' — export the project JSON to keep your work', 'err', 9000); }
}

/* ------------------------------------------------------------- history */
/** Every destructive edit goes through here: it runs, the inverse is kept,
    and the toast offers UNDO.  Ctrl/Cmd+Z undoes the last one.  Inverses are
    closures over the removed object, not project snapshots — a project with a
    DEM grid and draped images serialises to tens of MB. */
export function destructive(label, doIt, undoIt) {
  doIt();
  app.history.push({ label, undo: undoIt }); if (app.history.length > 30) app.history.shift();
  toast(label, 'warn', 8000, { action: { label: 'UNDO', onclick: () => undo() } });
}
export function undo() {
  const e = app.history.pop(); if (!e) { toast('nothing to undo', 'info', 2000); return false; }
  try { e.undo(); toast('undid: ' + e.label, 'ok', 2500); } catch (err) { console.error(err); toast('undo failed: ' + err.message, 'err'); }
  return true;
}

/* -------------------------------------------------------------- imagery */
export async function applyImagery(kind) {
  app.imagery = kind; const topo = topoGrid(); if (!topo) return;
  if (kind === 'none') { app.imageryTex = null; syncObject(topo); return; }
  status(`stitching ${SITE.IMAGERY[kind].name}…`);
  try { const r = await SITE.buildImagery(topo, app.project.crs, kind, { maxPx: 2048 }); if (!r) throw new Error('no tiles'); app.imageryTex = canvasTexture(r.canvas); app.imageryTex.userData.shared = true; syncObject(topo); status(`${SITE.IMAGERY[kind].name} draped (z${r.zoom}, ${r.tiles} tiles)`); }
  catch (e) { app.imageryTex = null; syncObject(topo); status('imagery unavailable — elevation colours'); }
  if (app.seeThrough) app.R.setOpacity(topo.id, topo.opacity);
  app.R.invalidate();
}

/* --------------------------------------------------------------- layers */
function groupOf(o) {
  if (o.group) return o.group;
  if (o.kind === 'grid2d') return o.role === 'topography' ? 'Topography' : o.role === 'contact' ? 'Stratigraphy' : o.role === 'property' ? 'Surfaces' : 'Surfaces';
  if (o.kind === 'mesh') return o.role === 'unit' ? 'Stratigraphy' : o.role === 'geology' ? 'Geology (draped)' : o.role === 'stope' ? 'Workings' : 'Surfaces';
  if (o.kind === 'lineset') return o.role === 'workings' ? 'Workings' : o.role === 'faults' ? 'Structure' : o.role === 'geology-outline' ? 'Geology outlines' : o.role === 'section' ? 'Sections' : 'Imports';
  if (o.kind === 'points') return o.role === 'structural' || o.role === 'trend' ? 'Structure' : o.role === 'claims' ? 'Claims' : o.role === 'mines' || o.role === 'targets' ? 'Mines' : o.role === 'notes' ? 'Notes' : 'Imports';
  if (o.kind === 'blockmodel') return 'Block models';
  if (o.kind === 'drillholes') return 'Drillholes';
  if (o.kind === 'imageplane') return 'Images';
  if (o.kind === 'section') return 'Sections';
  if (o.kind === 'stratmodel') return 'Stratigraphy';
  return 'Other';
}
const collapsed = new Set(['Geology outlines', 'Claims']);
function isEmpty(o) {
  if (o.kind === 'lineset') return !o.parts.length;
  if (o.kind === 'points') return !o.n;
  if (o.kind === 'stratmodel') return !o.units.length;
  if (o.kind === 'mesh') return !o.nTriangles;
  return false;
}
const plainTag = o => {
  switch (o.kind) {
    case 'grid2d': return `elevation / value grid, ${o.nx} × ${o.ny} cells of ${fmtNum(o.dx, 0)} m`;
    case 'mesh': return `${o.nTriangles.toLocaleString()} triangles`;
    case 'lineset': return o.parts.length ? `${o.parts.length} line${o.parts.length > 1 ? 's' : ''}, ${fmtNum(o.length(), 0)} m` : 'no lines yet';
    case 'points': return o.n ? `${o.n} point${o.n > 1 ? 's' : ''}` : 'no points yet';
    case 'blockmodel': return `${o.count.join(' × ')} blocks`;
    case 'drillholes': return `${o.collars.length} drillholes`;
    case 'imageplane': return o.plane === 'plan' ? 'georeferenced plan (horizontal image)' : 'georeferenced section (vertical image)';
    case 'stratmodel': return o.units.length ? `${o.units.length} units` : 'no units defined yet';
    case 'section': return 'a section line: pick it in the Section tool';
    default: return o.kind;
  }
};
export function renderLayers() {
  const host = clear($('layers')); if (!app.project) return;
  const filter = (app.layerFilter || '').trim().toLowerCase();
  const groups = new Map();
  for (const o of app.project.objects) { if (filter && !`${o.name} ${o.kind} ${o.role || ''} ${groupOf(o)}`.toLowerCase().includes(filter)) continue; const g = groupOf(o); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(o); }
  const extra = [...groups.keys()].filter(g => !GROUP_ORDER.includes(g)).sort();
  for (const band of BANDS) {
    const names = band.groups.concat(band.name === 'OUTPUTS' ? extra : []);
    const shown = names.filter(g => groups.has(g) || (!filter && STEP_GROUPS[g]));
    if (!shown.length) continue;
    host.appendChild(h('div', { class: 'lband' }, band.name));
    for (const g of shown) {
      const objs = groups.get(g) || []; const allOn = objs.every(o => o.visible !== false); const step = STEP_GROUPS[g] ? stepOf(STEP_GROUPS[g]) : null;
      const closed = collapsed.has(g) && !filter;
      const head = h('div', { class: 'lgroup' + (closed ? ' closed' : ''), oncontextmenu: e => { e.preventDefault(); groupMenu({ x: e.clientX, y: e.clientY }, g, objs); } },
        h('span', { class: 'tw', onclick: () => { collapsed.has(g) ? collapsed.delete(g) : collapsed.add(g); renderLayers(); } }, objs.length ? (closed ? '▸' : '▾') : '·'),
        objs.length ? h('input', { type: 'checkbox', checked: allOn, title: 'show / hide the whole group', onchange: e => { for (const o of objs) { o.visible = e.target.checked; app.R.setVisible(o.id, o.visible); } renderLayers(); markDirty(); } }) : h('span', { style: { width: '13px' } }),
        h('span', { class: 'gname', title: step ? `${step.title} (step ${step.step})` : '', onclick: () => { if (objs.length) { collapsed.has(g) ? collapsed.delete(g) : collapsed.add(g); renderLayers(); } else if (step) app.tools.open(step.key); } }, g, objs.length ? h('span', { class: 'cnt' }, String(objs.length)) : h('span', { class: 'ns' }, step ? `— not started · step ${step.step}` : '— empty')),
        h('button', { class: 'mbtn', title: 'group menu', onclick: e => { e.stopPropagation(); groupMenu(e.currentTarget, g, objs); } }, '⋯'));
      host.appendChild(head);
      if (closed) continue;
      for (const o of objs) host.appendChild(layerRow(o));
    }
  }
  if (filter && !groups.size) host.appendChild(h('div', { class: 'note', style: { padding: '8px 12px' } }, `no layer matches "${filter}"`));
}
function layerRow(o) {
  const d = app.display.get(o.id) || {}; const L = app.R.layers.get(o.id);
  const empty = isEmpty(o); const warns = (o.metadata && o.metadata.warnings) || [];
  const r = h('div', { class: 'lrow' + (app.selected === o.id ? ' sel' : '') + (empty ? ' empty' : ''), 'data-id': o.id,
    onclick: e => { if (e.target.tagName === 'INPUT' || e.target.classList.contains('mbtn')) return; select(o.id); },
    oncontextmenu: e => { e.preventDefault(); layerMenu({ x: e.clientX, y: e.clientY }, o); },
    ondblclick: () => { const b = o.bounds(); if (b) app.R.fitTo(b); } },
    h('input', { type: 'checkbox', checked: o.visible !== false, title: 'show / hide', onchange: e => { o.visible = e.target.checked; app.R.setVisible(o.id, o.visible); markDirty(); } }),
    o.kind === 'section' || o.kind === 'stratmodel' ? h('span', { class: 'sw kind', title: o.kind === 'section' ? 'section line' : 'stratigraphic model' }, o.kind === 'section' ? '§' : '≡') : colorInput(o.color, c => { o.color = c; syncObject(o); markDirty(); }),
    h('span', { class: 'lname', title: `${o.name} — ${plainTag(o)}${o.role ? ' · role ' + o.role : ''} (double-click zooms; right-click for the menu)` }, o.name || '(unnamed)'),
    L && L.buildError ? h('span', { class: 'st fail', title: 'failed to draw: ' + L.buildError }, '✕') : null,
    warns.length ? h('span', { class: 'st warn', title: warns.join('\n') }, `⚠${warns.length > 1 ? warns.length : ''}`) : null,
    empty ? h('span', { class: 'st empty', title: (o.metadata && o.metadata.howto) || 'nothing digitised or built yet' }, '∅') : h('span', { class: 'ltag', title: plainTag(o) }, tag(o)),
    h('button', { class: 'mbtn', title: 'layer menu (or right-click the row)', onclick: e => { e.stopPropagation(); layerMenu(e.currentTarget, o); } }, '⋯'));
  return r;
}
function tag(o) {
  switch (o.kind) {
    case 'grid2d': return `${o.nx}×${o.ny}`;
    case 'mesh': return `${(o.nTriangles / 1000).toFixed(o.nTriangles > 9999 ? 0 : 1)}k△`;
    case 'lineset': return `${o.parts.length} ln`;
    case 'points': return o.role === 'structural' ? `${o.n} ▱` : o.role === 'trend' ? `${o.n} ⬭` : `${o.n} pt`;
    case 'blockmodel': return `${o.count.join('×')}`;
    case 'drillholes': return `${o.collars.length} dh`;
    case 'imageplane': return o.plane;
    case 'stratmodel': return `${o.units.length} u`;
    default: return o.kind;
  }
}
function dependentsOf(o) {
  const out = [];
  for (const x of app.project.objects) {
    if (x === o) continue;
    const m = x.metadata || {};
    if (m.strat_of === o.id || m.form_of === o.id || (m.derived_from && m.derived_from.includes(o.id)) || (x.kind === 'stratmodel' && x.units.some(u => u.base === o.id || (u.source && u.source.id === o.id)))) out.push(x);
    if (o.kind === 'grid2d' && o.role === 'topography' && (x.kind === 'grid2d' && x.role === 'property' || x.kind === 'mesh' && x.role === 'geology')) out.push(x);
  }
  return out;
}
export async function deleteLayer(o) {
  const deps = dependentsOf(o);
  const body = h('div', {}, h('p', {}, `Delete "${o.name}"?`), deps.length ? note(`${deps.length} layer${deps.length > 1 ? 's' : ''} depend${deps.length > 1 ? '' : 's'} on it and will be kept as they are: ${deps.map(d => d.name).join(', ')}.`, 'note warn') : null, o.role === 'topography' ? note('This is the ground: draped imagery, property grids and orientations derived from traces all sit on it.', 'note warn') : null, note('UNDO is offered in the toast for a few seconds; the browser copy autosaves 2.5 s after that.'));
  if (!(await confirmModal('DELETE LAYER', body, { ok: 'DELETE', danger: true }))) return;
  const disp = app.display.get(o.id);
  destructive(`deleted ${o.name}`, () => { app.project.remove(o); }, () => { if (disp) app.display.set(o.id, disp); app.project.add(o); select(o.id); });
}
function layerMenu(anchor, o) {
  const items = [
    { head: o.name.slice(0, 40) },
    { label: 'Zoom to', hint: 'double-click', onclick: () => { const b = o.bounds(); if (b) app.R.fitTo(b); } },
    { label: 'Properties', onclick: () => select(o.id) },
    { label: 'Show only this', onclick: () => { for (const x of app.project.objects) { x.visible = x === o || (x.kind === 'grid2d' && x.role === 'topography'); app.R.setVisible(x.id, x.visible); } renderLayers(); markDirty(); } },
    { label: 'Export…', onclick: () => exportDialog([o]) },
    '-',
    { label: 'Rename', onclick: async () => { const n = await promptModal('RENAME LAYER', 'name', o.name); if (n != null && n.trim()) { o.name = n.trim(); renderLayers(); renderInspector(); markDirty(); } } },
  ];
  if (o.kind === 'grid2d' && o.role !== 'topography') items.push({ label: 'Set as topography', onclick: () => { for (const g of app.project.byKind('grid2d')) if (g.role === 'topography' && g !== o) { g.role = 'surface'; syncObject(g); } o.role = 'topography'; app.topoId = o.id; syncObject(o); applyImagery(app.imagery); renderLayers(); markDirty(); } });
  if (o.kind === 'grid2d') items.push({ label: 'Convert to mesh', onclick: () => { const m = o.toMesh(1); m.name = o.name + ' (mesh)'; m.metadata.derived_from = [o.id]; app.project.add(m); } });
  if (o.kind === 'lineset' && o.role === 'workings') items.push({ label: 'Send footprint to map (MY DATA)', onclick: () => app.tools.workings.sendToMap(o) });
  if (o.kind === 'lineset' && o.role === 'workings') items.push({ label: 'Edit in the Workings tool', onclick: () => app.tools.open('workings', o) });
  if (o.kind === 'points' && o.role === 'structural') items.push({ label: 'Stereonet', onclick: () => app.tools.open('stereonet', o) });
  if (o.kind === 'imageplane') items.push({ label: 'Trace workings on this image', onclick: () => app.tools.open('workings', null, o) });
  if (o.kind === 'section') items.push({ label: 'Open in the Section tool', onclick: () => app.tools.open('section', o) });
  items.push('-', { label: 'Delete layer…', onclick: () => deleteLayer(o) });
  menu(anchor, items);
}
function groupMenu(anchor, g, objs) {
  const step = STEP_GROUPS[g] ? stepOf(STEP_GROUPS[g]) : null;
  const vis = v => { for (const o of objs) { o.visible = v; app.R.setVisible(o.id, v); } renderLayers(); markDirty(); };
  menu(anchor, [
    { head: g },
    step ? { label: `Open ${step.title} (step ${step.step})`, onclick: () => app.tools.open(step.key) } : null,
    objs.length ? { label: 'Show only this group', onclick: () => { for (const o of app.project.objects) { const on = objs.includes(o) || (o.kind === 'grid2d' && o.role === 'topography'); o.visible = on; app.R.setVisible(o.id, on); } renderLayers(); markDirty(); } } : null,
    objs.length ? { label: 'Show all', onclick: () => vis(true) } : null,
    objs.length ? { label: 'Hide all', onclick: () => vis(false) } : null,
    objs.length ? { label: 'Zoom to group', onclick: () => { let b = null; for (const o of objs) { const ob = o.bounds(); if (!ob) continue; b = b ? [Math.min(b[0], ob[0]), Math.min(b[1], ob[1]), Math.min(b[2], ob[2]), Math.max(b[3], ob[3]), Math.max(b[4], ob[4]), Math.max(b[5], ob[5])] : ob.slice(); } if (b) app.R.fitTo(b); } } : null,
    '-',
    { label: 'Collapse all groups', onclick: () => { for (const k of GROUP_ORDER) collapsed.add(k); renderLayers(); } },
    { label: 'Expand all groups', onclick: () => { collapsed.clear(); renderLayers(); } },
    '-',
    { label: 'Import into this group…', onclick: () => { app.importGroup = g; $('fileIn').click(); } },
    objs.length ? { label: 'Export group…', onclick: () => exportDialog(objs) } : null,
  ]);
}

/* ------------------------------------------------------------ inspector */
export function select(id) {
  app.selected = id; app.picked = app.picked && app.picked.obj && app.picked.obj.id === id ? app.picked : null; renderLayers();
  const row = document.querySelector('.lrow.sel'); if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
  if (!id) app.R.highlight(null);
  renderInspector();
}
/** The layer inspector stands on its own: it never hosts a tool panel, and
    with nothing selected it shows where the model stands, step by step. */
export function renderInspector() {
  const host = clear($('inspector'));
  if (!app.project) { host.appendChild(h('div', { class: 'note' }, 'no project — open one from a mine card, drop a file, or PROJECTS ▾')); return; }
  const o = app.selected ? app.project.get(app.selected) : null;
  if (!o) { host.appendChild(app.tools.progressCard()); return; }
  host.appendChild(inspectorFor(o));
}
/* One line that says where a layer came from, in words. */
function sourceSummary(o) {
  const p = o.provenance || {};
  const bits = [];
  if (p.source) bits.push(String(p.source));
  if (p.bundle) bits.push(String(p.bundle));
  if (p.method) bits.push(String(p.method));
  if (p.builder) bits.push(`built by ${p.builder}`);
  if (o.kind === 'grid2d' && o.role === 'topography' && p.zoom) bits.push(`zoom ${p.zoom} · ${fmtNum(p.cell_m || o.dx, 0)} m cells`);
  if (o.kind === 'lineset' && o.role === 'workings' && o.features.length) { const t = { surveyed: 0, described: 0, assumed: 0 }; o.features.forEach(f => t[GMR.confClass(f)]++); bits.push(CONF_WORDS(t)); }
  return bits.join(' · ');
}
const CONF_WORDS = t => GMR.CONF_CLASSES.filter(c => t[c.key]).map(c => `${t[c.key]} ${c.label}`).join(', ');
function pickCard() {
  const p = app.picked; if (!p || !p.lines) return null;
  const o = p.obj;
  const card = h('div', { class: 'psec pick' }, h('h3', {}, 'PICKED'));
  p.lines.forEach((l, i) => { const m = /^(source|url|page):\s*(.*)$/i.exec(l); if (m && m[1].toLowerCase() === 'url') card.appendChild(h('div', { class: 'note' }, 'url: ', h('a', { href: m[2], target: '_blank', rel: 'noopener', style: { color: 'var(--accent)' } }, m[2].slice(0, 60)))); else card.appendChild(i ? note(l) : h('div', { class: 'note', style: { color: 'var(--ink)', fontWeight: 600 } }, l)); });
  if (p.quote) card.appendChild(h('div', { class: 'note' }, h('blockquote', {}, '“' + String(p.quote).slice(0, 400) + '”')));
  const acts = h('div', { class: 'frow' });
  acts.appendChild(btn('ZOOM TO FEATURE', () => { const b = p.bounds || (o.bounds && o.bounds()); if (b) app.R.fitTo(b, 1.3); }, { class: 'x' }));
  if (o.kind === 'lineset' && o.role === 'workings') acts.appendChild(btn('EDIT', () => app.tools.open('workings', o), { class: 'x' }));
  if (o.kind === 'points' && o.role === 'structural') acts.appendChild(btn('STEREONET', () => app.tools.open('stereonet', o), { class: 'x' }));
  acts.appendChild(btn('CLEAR', () => { app.picked = null; app.R.highlight(null); renderInspector(); }, { class: 'x' }));
  card.appendChild(acts);
  return card;
}
function inspectorFor(o) {
  const d = app.display.get(o.id) || {};
  const b = o.bounds();
  const body = h('div', { class: 'insp' },
    h('h2', {}, o.name || '(unnamed)'),
    h('div', { class: 'badges' }, h('span', { class: 'badge', title: plainTag(o) }, o.kind.toUpperCase()), o.role ? h('span', { class: 'badge dim' }, o.role) : null, o.group ? h('span', { class: 'badge dim' }, o.group) : null),
  );
  const src = sourceSummary(o); if (src) body.appendChild(h('div', { class: 'note', style: { color: 'var(--ink2)', marginTop: '-4px' } }, src));
  const pc = pickCard(); if (pc) body.appendChild(pc);
  body.appendChild(kv([['Bounds E', b ? `${fmtNum(b[0], 0)} – ${fmtNum(b[3], 0)}` : null], ['Bounds N', b ? `${fmtNum(b[1], 0)} – ${fmtNum(b[4], 0)}` : null], ['Elevation', b && b[2] === b[2] ? `${fmtNum(b[2], 0)} – ${fmtNum(b[5], 0)} m` : null]]));
  // display controls by kind
  const ctl = h('div', { class: 'psec' }, h('h3', {}, 'DISPLAY'));
  ctl.appendChild(row('opacity', range(o.opacity == null ? 1 : o.opacity, 0, 1, 0.05, e => { o.opacity = +e.target.value; app.R.setOpacity(o.id, o.opacity); markDirty(); })));
  if (o.kind === 'grid2d') {
    if (o.role === 'property') {
      // the mode decides which colormap a grid falls back to, so redraw the
      // panel — otherwise the dropdown keeps naming the previous mode's ramp
      ctl.appendChild(row('mode', sel([['draped', 'drape on topography'], ['flat', 'flat at elevation'], ['surface', 'as surface (Z = value)']], d.mode || 'draped', { onchange: e => { d.mode = e.target.value; syncObject(o); select(o.id); } })));
      ctl.appendChild(row('elevation', num(d.elevation == null ? '' : d.elevation, { placeholder: 'for flat mode', onchange: e => { d.elevation = e.target.value === '' ? null : +e.target.value; syncObject(o); } })));
    } else ctl.appendChild(row('colour by', sel([['flat', 'flat colour'], ['elevation', 'elevation']], d.colorBy || (o.role === 'topography' ? 'elevation' : 'flat'), { onchange: e => { d.colorBy = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), GMR.defaultColormap(o, d), { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('wireframe', h('input', { type: 'checkbox', checked: !!d.wireframe, onchange: e => { d.wireframe = e.target.checked; syncObject(o); } })));
    const L = app.R.layers.get(o.id); if (L && L.range) ctl.appendChild(note(`range ${fmtNum(L.range[0])} – ${fmtNum(L.range[1])} ${o.units || ''}`));
    const zr = o.zrange(); ctl.appendChild(kv([['Cell', `${o.dx} × ${o.dy} m`], ['Rotation', o.rotation ? o.rotation + '°' : null], ['Values', `${fmtNum(zr[0])} – ${fmtNum(zr[1])}`], ['No-data', `${[...o.values].filter(v => v !== v).length} nodes`]]));
    if (o.role === 'topography') ctl.appendChild(h('div', { class: 'frow' }, btn(app.seeThrough ? 'SOLID GROUND' : 'SEE-THROUGH GROUND', () => { setSeeThrough(!app.seeThrough); select(o.id); }, { class: 'x', title: 'thin the ground so workings below it read' })));
  }
  if (o.kind === 'mesh') {
    const attrs = Object.keys(o.attributes);
    if (attrs.length) ctl.appendChild(row('colour by', sel([['', 'flat colour'], ...attrs], d.attribute || '', { onchange: e => { d.attribute = e.target.value || null; syncObject(o); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), GMR.defaultColormap(o, d), { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('wireframe', h('input', { type: 'checkbox', checked: !!d.wireframe, onchange: e => { d.wireframe = e.target.checked; syncObject(o); } })));
    ctl.appendChild(row('edges', h('input', { type: 'checkbox', checked: !!d.edges, onchange: e => { d.edges = e.target.checked; syncObject(o); } })));
    ctl.appendChild(kv([['Vertices', o.nVertices], ['Triangles', o.nTriangles]]));
  }
  if (o.kind === 'points' && (o.role === 'structural' || o.role === 'trend')) {
    const cols = Object.keys(o.attributes).filter(k => !['dip', 'dip_azimuth', 'polarity', 'z_original'].includes(k));
    ctl.appendChild(row('colour by', sel([['', 'layer colour'], ['dip', 'dip'], ['dip_azimuth', 'dip azimuth'], ['polarity', 'polarity'], ...cols], d.attribute || '', { onchange: e => { d.attribute = e.target.value || null; syncObject(o); select(o.id); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), GMR.defaultColormap(o, d), { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('disc size m', num(d.radius == null ? '' : d.radius, { placeholder: 'auto', onchange: e => { d.radius = e.target.value === '' ? null : +e.target.value; syncObject(o); } })));
    ctl.appendChild(row('disc sides', sel([[3, '3 — triangle (apex down dip)'], [6, '6'], [16, '16 — disc'], [32, '32']], d.sides || 16, { onchange: e => { d.sides = +e.target.value; syncObject(o); } })));
    if (o.role !== 'trend') ctl.appendChild(row('down-dip ticks', h('input', { type: 'checkbox', checked: d.tick !== false, onchange: e => { d.tick = e.target.checked; syncObject(o); } })));
    ctl.appendChild(row('labels', h('input', { type: 'checkbox', checked: !!d.labels, onchange: e => { d.labels = e.target.checked; syncObject(o); } }), sel(['dip', 'dip_azimuth', ...cols], d.labelField || 'dip', { onchange: e => { d.labelField = e.target.value; syncObject(o); } })));
    const L = app.R.layers.get(o.id);
    if (L && L.range) ctl.appendChild(note(`range ${fmtNum(L.range[0])} – ${fmtNum(L.range[1])}`));
    if (L && L.drawn != null && L.drawn < L.totalGlyphs) ctl.appendChild(note(`drawing ${L.drawn} of ${L.totalGlyphs} glyphs (decimated for speed)`, 'note warn'));
    try {
      const RS = ST.readStructural(o); const bg = RS.n >= 2 ? ST.binghamStats(RS.poles, RS.n) : null;
      ctl.appendChild(kv([['Measurements', `${RS.n}${RS.n < o.n ? ` of ${o.n}` : ''}`],
        ['Mean plane', bg ? `${bg.mean_plane.dip.toFixed(0)}° → ${bg.mean_plane.dip_azimuth.toFixed(0)}°` : null],
        ['Fabric', bg ? bg.fabric : null],
        ['Columns', Object.keys(o.attributes).join(', ')]]));
    } catch (e) { ctl.appendChild(note(e.message, 'note warn')); }
    ctl.appendChild(h('div', { class: 'frow' }, btn('STEREONET', () => app.tools.open('stereonet', o)), btn('EDIT / DERIVE', () => app.tools.open('structure', o)), btn('FORM INTERPOLANT', () => app.tools.open('form', o))));
  }
  else if (o.kind === 'points') {
    const nums = Object.keys(o.attributes).filter(k => o.isNumeric(k)); const texts = Object.keys(o.attributes).filter(k => !o.isNumeric(k));
    ctl.appendChild(row('colour by', sel([['', 'layer colour'], ...nums], d.attribute || '', { onchange: e => { d.attribute = e.target.value || null; syncObject(o); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), GMR.defaultColormap(o, d), { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('size', range(d.size || 10, 3, 30, 1, e => { d.size = +e.target.value; syncObject(o); })));
    ctl.appendChild(row('labels', h('input', { type: 'checkbox', checked: !!d.labels, onchange: e => { d.labels = e.target.checked; syncObject(o); } }), sel(['name', ...texts.filter(t => t !== 'name'), ...nums], d.labelField || 'name', { onchange: e => { d.labelField = e.target.value; syncObject(o); } })));
    const L = app.R.layers.get(o.id); if (L && L.range) ctl.appendChild(note(`range ${fmtNum(L.range[0])} – ${fmtNum(L.range[1])}`));
    ctl.appendChild(kv([['Points', o.n], ['Columns', Object.keys(o.attributes).join(', ') || '—']]));
    if (o.n) ctl.appendChild(btn('TABLE…', () => tableModal(o), { class: 'x', title: 'every row, sortable; click a row to zoom to it' }));
  }
  if (o.kind === 'lineset') {
    ctl.appendChild(row('tubes', h('input', { type: 'checkbox', checked: !!d.tubes, onchange: e => { d.tubes = e.target.checked; syncObject(o); } }), range(d.tubeScale || 1, 0.2, 6, 0.1, e => { d.tubeScale = +e.target.value; syncObject(o); })));
    ctl.appendChild(row('labels', h('input', { type: 'checkbox', checked: !!d.labels, onchange: e => { d.labels = e.target.checked; syncObject(o); } }), sel([['name', 'name'], ['type', 'type'], ['level', 'level'], ['confidence', 'confidence']], d.labelField || 'name', { onchange: e => { d.labelField = e.target.value; syncObject(o); } })));
    ctl.appendChild(kv([['Parts', o.parts.length], ['Length', fmtNum(o.length(), 0) + ' m']]));
    if (o.role === 'workings') {
      const s = E.workingsSummary(o); ctl.appendChild(kv(Object.entries(s.by_type).map(([k, v]) => [k, fmtNum(v, 0) + ' m'])));
      // confidence, per layer, in the same words as the banner
      const t = { surveyed: 0, described: 0, assumed: 0 }; o.features.forEach(f => t[GMR.confClass(f)]++);
      const conf = h('div', { class: 'psec' }, h('h3', {}, 'CONFIDENCE'));
      for (const c of GMR.CONF_CLASSES) if (t[c.key]) conf.appendChild(h('div', { class: 'keyrow', title: c.hint }, lineSample(c.dash), `${c.label} · ${t[c.key]} — ${c.hint}`));
      if (t.described + t.assumed) conf.appendChild(note('A described working is a digitising bridge, not new evidence: every element carries the sentence it came from.'));
      body.appendChild(conf);
      const list = h('div', { class: 'psec' }, h('h3', {}, `WORKINGS (${o.parts.length})`));
      o.features.forEach((f, k) => {
        const cc = GMR.CONF_CLASSES.find(c => c.key === GMR.confClass(f)); const src = f.source || {};
        const rowEl = h('div', { class: 'feat', title: `${cc.label} — ${cc.hint}`, onmouseenter: () => app.R.highlight(o, k), onmouseleave: () => app.R.highlight(null), onclick: () => { const pts = o.partXYZ(k); const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]), zs = pts.map(p => p[2]); app.picked = { obj: o, index: k, lines: describePick({ obj: o, object: { userData: { segPart: null } }, index: null, partIndex: k }), quote: f.quote || src.quote || null, bounds: [Math.min(...xs) - 30, Math.min(...ys) - 30, Math.min(...zs) - 30, Math.max(...xs) + 30, Math.max(...ys) + 30, Math.max(...zs) + 30] }; app.R.highlight(o, k); renderInspector(); } },
          h('span', { class: 'sw', style: { background: `rgb(${(E.WORKING_TYPES[f.type] || E.WORKING_TYPES.unknown).color.join(',')})` } }),
          h('span', { class: 'fn' }, f.name || f.type, f.name && f.name !== f.type ? h('span', { class: 'fl' }, ' ' + f.type) : null, f.level ? h('span', { class: 'fl' }, ' · L' + f.level) : null),
          lineSample(cc.dash), h('span', { class: 'fl' }, fmtNum(o.length(k), 0) + ' m'), src.page ? h('span', { class: 'fl' }, 'p.' + src.page) : null);
        list.appendChild(rowEl);
      });
      body.appendChild(list);
      ctl.appendChild(btn('OPEN WORKINGS EDITOR', () => app.tools.open('workings', o)));
    }
    if (o.parts.length) ctl.appendChild(btn('TABLE…', () => tableModal(o), { class: 'x' }));
  }
  if (o.kind === 'blockmodel') {
    const attrs = Object.keys(o.attributes); const L = app.R.layers.get(o.id);
    ctl.appendChild(row('attribute', sel(attrs, d.attribute || attrs[0], { onchange: e => { d.attribute = e.target.value; d.cutoff = null; d.cutoffHi = null; syncObject(o); select(o.id); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), GMR.defaultColormap(o, d, !!(L && L.categories)), { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    if (L && L.range) { const [lo, hi] = L.range; const cutoffIn = num(d.cutoff == null ? lo : d.cutoff, { onchange: e => { d.cutoff = +e.target.value; syncObject(o); select(o.id); } }); const hiIn = num(d.cutoffHi == null ? hi : d.cutoffHi, { onchange: e => { d.cutoffHi = +e.target.value; syncObject(o); select(o.id); } }); ctl.appendChild(row('cut-off ≥', cutoffIn)); ctl.appendChild(row('≤', hiIn)); ctl.appendChild(note(`range ${fmtNum(lo)} – ${fmtNum(hi)} · showing ${L.shownBlocks} of ${L.totalBlocks} blocks${L.shownBlocks < L.totalBlocks ? ' (decimated)' : ''}`)); }
    if (L && L.categories) ctl.appendChild(row('category', sel([['', 'all'], ...L.categories], d.category || '', { onchange: e => { d.category = e.target.value || null; syncObject(o); } })));
    ctl.appendChild(row('block shrink', range(d.shrink || 0.92, 0.3, 1, 0.02, e => { d.shrink = +e.target.value; syncObject(o); })));
    ctl.appendChild(kv([['Origin', o.origin.map(v => fmtNum(v, 1)).join(', ')], ['Block', o.blockSize.join(' × ') + ' m'], ['Count', o.count.join(' × ') + ` = ${o.n.toLocaleString()}`]]));
    if (o.metadata.estimates) for (const e of o.metadata.estimates) ctl.appendChild(note(`${e.attribute}: ${e.method.toUpperCase()} from ${e.samples} samples${e.variogram ? ' · variogram ' + e.variogram.structures.map(s => `${s.model} sill ${fmtNum(s.sill, 3)} range ${fmtNum(s.range, 0)}`).join(' + ') + ' nugget ' + fmtNum(e.variogram.nugget, 3) : ''}`));
    ctl.appendChild(btn('GRADE–TONNAGE / ESTIMATE…', () => app.tools.open('blocks', o)));
  }
  if (o.kind === 'drillholes') {
    const tables = Object.keys(o.intervals); const cols = d.table && o.intervals[d.table] && o.intervals[d.table][0] ? Object.keys(o.intervals[d.table][0]).filter(k => !['hole', 'from', 'to'].includes(k)) : [];
    ctl.appendChild(row('table', sel([['', '—'], ...tables], d.table || '', { onchange: e => { d.table = e.target.value || null; d.column = null; syncObject(o); select(o.id); } })));
    if (cols.length) ctl.appendChild(row('colour by', sel([['', '—'], ...cols], d.column || '', { onchange: e => { d.column = e.target.value || null; syncObject(o); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), GMR.defaultColormap(o, d), { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(kv([['Holes', o.collars.length], ['Surveys', o.surveys.length], ['Tables', tables.map(t => `${t} (${o.intervals[t].length})`).join(', ') || '—']]));
    ctl.appendChild(btn('ADD SURVEY / INTERVAL CSV…', () => addDrillholeTable(o)));
    ctl.appendChild(btn('SAMPLES → POINTS (for kriging)', () => { if (!d.table || !d.column) return toast('choose a table + column first', 'warn'); const ps = o.intervalPoints(d.table, d.column); ps.group = 'Imports'; ps.color = [255, 200, 80]; ps.metadata.derived_from = [o.id]; app.project.add(ps); select(ps.id); }));
  }
  if (o.kind === 'imageplane') {
    ctl.appendChild(kv([['Plane', o.plane], ['Size', `${o.width} × ${o.height} px`], ['Elevation', o.elevation], ['z top / bottom', o.plane === 'section' ? `${o.zTop} / ${o.zBottom}` : null], ['Control pts', o.control.length || null]]));
    ctl.appendChild(btn('GEOREFERENCE…', () => app.tools.open('georef', o)));
    ctl.appendChild(btn('TRACE WORKINGS ON THIS IMAGE', () => app.tools.open('workings', null, o)));
  }
  if (o.kind === 'section') { ctl.appendChild(btn('OPEN IN SECTION TOOL', () => app.tools.open('section', o))); }
  if (o.kind === 'stratmodel') { ctl.appendChild(kv(o.units.map(u => [u.name, `${u.contact || 'deposit'}${u.base ? '' : ' (basement)'}`]))); ctl.appendChild(btn('OPEN STRATIGRAPHY BUILDER', () => app.tools.open('strat', o))); }
  body.appendChild(ctl);
  // provenance + metadata
  const prov = Object.entries(o.provenance || {}).filter(([k, v]) => v != null && typeof v !== 'object').map(([k, v]) => [k, String(v)]);
  for (const [k, v] of Object.entries(o.provenance || {})) if (v && typeof v === 'object' && !Array.isArray(v)) for (const [kk, vv] of Object.entries(v)) if (vv != null && typeof vv !== 'object') prov.push([`${k}.${kk}`, String(vv)]);
  if (prov.length) body.appendChild(section('PROVENANCE', kv(prov.map(([k, v]) => [k, /^https?:\/\//.test(v) ? h('a', { href: v, target: '_blank', rel: 'noopener', style: { color: 'var(--accent)' } }, v.slice(0, 80)) : v]))));
  const meta = Object.entries(o.metadata || {}).filter(([k, v]) => !['warnings', 'types', 'schema', 'howto', 'notes', 'display'].includes(k) && v != null && typeof v !== 'object').map(([k, v]) => [k, String(v).slice(0, 300)]);
  if (meta.length) { const det = h('details', {}, h('summary', { class: 'mono', style: { cursor: 'pointer', margin: '8px 0 2px' } }, `METADATA (${meta.length})`), kv(meta)); body.appendChild(det); }
  if (o.metadata && o.metadata.howto) body.appendChild(note(o.metadata.howto));
  if (o.metadata && o.metadata.warnings && o.metadata.warnings.length) body.appendChild(section('WARNINGS', ...o.metadata.warnings.map(w => note(w, 'note warn'))));
  // notes travel with the project and into the OMF description
  const ta = h('textarea', { class: 'notes', placeholder: 'notes on this layer (interpretation, sources, doubts) — saved with the project', onchange: e => { o.metadata.notes = e.target.value; markDirty(); } }); ta.value = (o.metadata && o.metadata.notes) || '';
  body.appendChild(section('NOTES', ta));
  body.appendChild(h('div', { class: 'frow' }, btn('ZOOM TO', () => { const bb = o.bounds(); if (bb) app.R.fitTo(bb); }), btn('EXPORT…', () => exportDialog([o])), btn('DELETE…', () => deleteLayer(o), { class: 'danger' })));
  return body;
}
/** A plain table of every row of a points layer or every feature of a
    lineset: sortable by header, filterable, click = zoom + highlight. */
function tableModal(o) {
  const rows = []; let cols = [];
  if (o.kind === 'points') { cols = ['#', 'E', 'N', 'Z', ...Object.keys(o.attributes)]; for (let i = 0; i < o.n; i++) { const p = o.point(i); rows.push([i, +p[0].toFixed(1), +p[1].toFixed(1), +p[2].toFixed(1), ...Object.values(o.attributes).map(c => c[i])]); } }
  else { const keys = [...new Set(o.features.flatMap(f => Object.keys(f).filter(k => typeof f[k] !== 'object')))]; cols = ['#', 'length m', ...keys]; o.features.forEach((f, k) => rows.push([k, +o.length(k).toFixed(1), ...keys.map(kk => f[kk])])); }
  let sortCol = 0, asc = true, filter = '';
  const box = h('div', { style: { maxHeight: '60vh', overflow: 'auto' } });
  const draw = () => {
    clear(box); const f = filter.toLowerCase();
    const shown = rows.filter(r => !f || r.some(v => String(v == null ? '' : v).toLowerCase().includes(f))).sort((a, b) => { const x = a[sortCol], y = b[sortCol]; const c = typeof x === 'number' && typeof y === 'number' ? x - y : String(x == null ? '' : x).localeCompare(String(y == null ? '' : y)); return asc ? c : -c; });
    const t = h('table', { class: 'tbl' }, h('tr', {}, ...cols.map((c, i) => h('th', { style: { cursor: 'pointer' }, onclick: () => { if (sortCol === i) asc = !asc; else { sortCol = i; asc = true; } draw(); } }, c + (sortCol === i ? (asc ? ' ▲' : ' ▼') : '')))));
    for (const r of shown.slice(0, 1000)) t.appendChild(h('tr', { style: { cursor: 'pointer' }, onclick: () => { const i = r[0]; if (o.kind === 'points') { const p = o.point(i); app.R.fitTo([p[0] - 60, p[1] - 60, p[2] - 60, p[0] + 60, p[1] + 60, p[2] + 60]); app.R.highlight(o, i); } else { const pts = o.partXYZ(i); const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]), zs = pts.map(p => p[2]); app.R.fitTo([Math.min(...xs) - 30, Math.min(...ys) - 30, Math.min(...zs) - 30, Math.max(...xs) + 30, Math.max(...ys) + 30, Math.max(...zs) + 30]); app.R.highlight(o, i); } } }, ...r.map(v => h('td', {}, v == null ? '' : typeof v === 'number' ? fmtNum(v, 3) : String(v).slice(0, 60)))));
    box.appendChild(t); if (shown.length > 1000) box.appendChild(note(`showing 1000 of ${shown.length} rows — filter to narrow`));
    cnt.textContent = `${shown.length} of ${rows.length} rows`;
  };
  const cnt = h('span', { class: 'mono' }); const fin = txt('', { placeholder: 'filter…', oninput: e => { filter = e.target.value; draw(); } });
  draw();
  modal(`TABLE — ${o.name}`, h('div', {}, h('div', { class: 'frow' }, fin, cnt, btn('EXPORT CSV', () => exportObjects(o.kind === 'points' ? (o.role === 'structural' ? 'csv_structural' : 'csv_points') : 'geojson', [o]), { class: 'x' })), box), { width: 'min(900px,94vw)' });
}

/* ------------------------------------------------------------- picking */
/* A working's citation lives in a nested `source` object, and the key/value
   loop below skips objects — so the document a working was read out of never
   reached the panel that shows the working.  Provenance the model carries but
   never displays is provenance the reader cannot check. */
function sourceLines(src) {
  if (!src || typeof src !== 'object') return [];
  const out = [];
  const doc = src.doc || src.document || src.title;
  if (doc) out.push(`source: ${String(doc).slice(0, 160)}${src.page ? `, p. ${src.page}` : ''}`);
  else if (src.page) out.push(`page: ${src.page}`);
  if (src.url) out.push(`url: ${String(src.url).slice(0, 160)}`);
  return out;
}

export function describePick(p) {
  if (!p || !p.obj) return null;
  const o = p.obj; const lines = [];
  if (o.kind === 'points' && o.role === 'structural' && p.index != null) {
    const i = p.index; const dip = o.attributes.dip ? o.attributes.dip[i] : null, az = o.attributes.dip_azimuth ? o.attributes.dip_azimuth[i] : null;
    lines.push(`${o.name} #${i}`);
    if (dip != null && az != null) lines.push(`${(+dip).toFixed(0)}° → ${(+az).toFixed(0)}°  (dip / dip azimuth)`);
    const pol = o.attributes.polarity ? +o.attributes.polarity[i] : 1; if (pol < 0) lines.push('overturned');
    for (const [k, col] of Object.entries(o.attributes)) { if (['dip', 'dip_azimuth', 'polarity', 'z_original'].includes(k)) continue; const v = col[i]; if (v == null || v === '') continue; lines.push(`${k}: ${String(v).slice(0, 60)}`); if (lines.length > 12) break; }
  }
  else if (o.kind === 'points' && p.index != null) { const i = p.index; lines.push(`${o.name} #${i}`); for (const [k, col] of Object.entries(o.attributes)) { const v = col[i]; if (v == null || v === '') continue; lines.push(`${k}: ${String(v).slice(0, 80)}`); if (lines.length > 14) break; } }
  else if (o.kind === 'lineset') {
    // a part index can come from the segment map (hairlines), from the
    // renderer's face→part map (tubes), or straight from a feature row
    let k = p.partIndex != null ? p.partIndex : -1;
    if (k < 0 && p.object && p.object.userData) { const ud = p.object.userData; if (ud.segPart && p.index != null) k = ud.segPart[Math.floor(p.index / 2)]; else if (ud.faceRanges && p.faceIndex != null) { for (const [a, b, part] of ud.faceRanges) if (p.faceIndex >= a && p.faceIndex < b) { k = part; break; } } else if (ud.faceToPart && p.faceIndex != null) k = ud.faceToPart[p.faceIndex]; }
    const f = k >= 0 ? o.features[k] : null; lines.push(o.name + (k >= 0 ? ` · part ${k}` : ''));
    if (f) { const cc = GMR.CONF_CLASSES.find(c => c.key === GMR.confClass(f)); for (const [kk, v] of Object.entries(f)) { if (kk === 'confidence' || kk === 'quote' || kk === 'span') continue; if (v != null && v !== '' && typeof v !== 'object') lines.push(`${kk}: ${v}`); } if (o.role === 'workings') lines.push(`confidence: ${cc.label} — ${cc.hint}`); }
    if (k >= 0) lines.push(`length${f && GMR.confClass(f) !== 'surveyed' ? ' (as drawn)' : ''}: ${fmtNum(o.length(k), 1)} m`);
    if (f) lines.push(...sourceLines(f.source));
  }
  else if (o.kind === 'blockmodel' && p.instanceId != null) { const ids = p.object.userData.blockIds; const idx = ids ? ids[p.instanceId] : null; if (idx != null) { const [i, j, k] = o.ijk(idx); lines.push(`${o.name} block (${i},${j},${k})`); for (const [kk, a] of Object.entries(o.attributes)) { const v = a.values[idx]; if (v == null || v !== v) continue; lines.push(`${kk}: ${typeof v === 'number' ? fmtNum(v, 3) : v}`); } } }
  else if (o.kind === 'mesh') { lines.push(o.name); for (const k of ['unit', 'lithology', 'age', 'description']) if (o.metadata[k]) lines.push(`${k}: ${String(o.metadata[k]).slice(0, 120)}`); if (o.metadata.confidence) lines.push(`confidence: ${o.metadata.confidence}`); }
  else if (o.kind === 'grid2d') { const z = o.sample(p.world[0], p.world[1]); lines.push(o.name); lines.push(`value: ${fmtNum(z, 2)} ${o.units || ''}`); }
  else lines.push(o.name);
  return lines;
}
function partIndexOf(p) {
  const o = p.obj; if (!o || o.kind !== 'lineset' || !p.object) return null;
  const ud = p.object.userData || {};
  if (ud.segPart && p.index != null) return ud.segPart[Math.floor(p.index / 2)];
  if (ud.faceRanges && p.faceIndex != null) { for (const [a, b, part] of ud.faceRanges) if (p.faceIndex >= a && p.faceIndex < b) return part; }
  if (ud.faceToPart && p.faceIndex != null) return ud.faceToPart[p.faceIndex];
  return null;
}

/* --------------------------------------------------------------- chrome */
function wireChrome() {
  const R = app.R; const canvas = $('gl'); const tip = $('tip');
  canvas.addEventListener('mousemove', e => {
    if (app.tools.active && app.tools.active.onMove && app.tools.active.onMove(e)) return;
    const p = R.pick(e.clientX, e.clientY);
    if (p) { const [x, y, z] = p.world; const lonlat = app.project && app.project.crs.kind === 'utm' ? GM.utm.inv(x, y, app.project.crs.zone, app.project.crs.north) : null; $('coords').textContent = `E ${fmtNum(x, 1)}  N ${fmtNum(y, 1)}  Z ${fmtNum(z, 1)} m` + (lonlat ? `   (${lonlat[1].toFixed(5)}, ${lonlat[0].toFixed(5)})` : ''); const lines = describePick(p);
      // the readout stays on while a tool panel is merely open; only an armed
      // click mode owns the pointer
      if (lines && !app.tools.armed) { tip.style.display = 'block'; tip.style.left = Math.min(window.innerWidth - 330, e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; tip.innerHTML = lines.map((l, i) => i ? GM.esc(l) : `<b>${GM.esc(l)}</b>`).join('<br>'); } else tip.style.display = 'none'; }
    else { tip.style.display = 'none'; }
  });
  canvas.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  let downAt = null;
  canvas.addEventListener('pointerdown', e => { downAt = [e.clientX, e.clientY, Date.now(), e.button]; });
  canvas.addEventListener('pointerup', e => {
    if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4 || Date.now() - downAt[2] > 600 || e.button !== 0) return;
    if (app.tools.active && app.tools.active.onClick && app.tools.active.onClick(e)) return;
    const p = R.pick(e.clientX, e.clientY);
    if (p && p.obj) {
      const lines = describePick(p); const k = partIndexOf(p);
      let bounds = null, quote = null;
      if (p.obj.kind === 'lineset' && k != null && k >= 0) { const pts = p.obj.partXYZ(k); const xs = pts.map(q => q[0]), ys = pts.map(q => q[1]), zs = pts.map(q => q[2]); bounds = [Math.min(...xs) - 30, Math.min(...ys) - 30, Math.min(...zs) - 30, Math.max(...xs) + 30, Math.max(...ys) + 30, Math.max(...zs) + 30]; const f = p.obj.features[k] || {}; quote = f.quote || (f.source && f.source.quote) || null; }
      else if (p.obj.kind === 'points' && p.index != null) { const q = p.obj.point(p.index); bounds = [q[0] - 60, q[1] - 60, q[2] - 60, q[0] + 60, q[1] + 60, q[2] + 60]; }
      app.picked = { obj: p.obj, index: p.obj.kind === 'lineset' ? k : p.index, lines, bounds, quote };
      if (p.obj.kind === 'lineset' && k != null && k >= 0) R.highlight(p.obj, k); else if (p.obj.kind === 'points' && p.index != null) R.highlight(p.obj, p.index); else R.highlight(null);
      app.selected = p.obj.id; renderLayers(); renderInspector();
      const rowEl = document.querySelector('.lrow.sel'); if (rowEl && rowEl.scrollIntoView) rowEl.scrollIntoView({ block: 'nearest' });
    }
  });
  canvas.addEventListener('contextmenu', e => {
    e.preventDefault();
    const p = R.pick(e.clientX, e.clientY); const w = p ? p.world : null;
    const items = [];
    if (p && p.obj) { items.push({ head: p.obj.name.slice(0, 40) }, { label: 'Properties', onclick: () => select(p.obj.id) }, { label: 'Zoom to layer', onclick: () => { const b = p.obj.bounds(); if (b) R.fitTo(b); } }, { label: `Hide ${p.obj.name.slice(0, 24)}`, onclick: () => { p.obj.visible = false; R.setVisible(p.obj.id, false); renderLayers(); markDirty(); } }, '-'); }
    if (w) { const v = R.toScene(w[0], w[1], w[2]); v.y *= R.ve; items.push({ label: 'Centre the view here', hint: 'double-click', onclick: () => { R.controls.target.copy(v); R.invalidate(); } }); }
    if (w && app.project) items.push('-', { label: 'Section W–E through here', onclick: () => { app.tools.open('section'); const b = app.project.bounds(); const Rr = Math.max(b[3] - b[0], b[4] - b[1]) / 2; app.tools.section.create([w[0] - Rr, w[1]], [w[0] + Rr, w[1]], 'Section W-E'); } }, { label: 'Section S–N through here', onclick: () => { app.tools.open('section'); const b = app.project.bounds(); const Rr = Math.max(b[3] - b[0], b[4] - b[1]) / 2; app.tools.section.create([w[0], w[1] - Rr], [w[0], w[1] + Rr], 'Section S-N'); } }, { label: 'Draw a section line…', onclick: () => { app.tools.open('section'); app.tools.section.startDraw(); } }, '-', { label: 'Add a structural measurement here…', onclick: () => { app.tools.open('structure'); app.tools.structure.startDigitise && app.tools.structure.startDigitise('one'); } });
    if (items.length) menu({ x: e.clientX, y: e.clientY }, items);
  });
  canvas.addEventListener('dblclick', e => { if (app.tools.active && app.tools.active.onDblClick && app.tools.active.onDblClick(e)) return; const p = R.pick(e.clientX, e.clientY); if (p) { const v = R.toScene(p.world[0], p.world[1], p.world[2]); v.y *= R.ve; R.controls.target.copy(v); R.invalidate(); } });
  window.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
    if ((e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z')) { if (!e.shiftKey) { e.preventDefault(); undo(); } return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (app.tools.active && app.tools.active.onKey && app.tools.active.onKey(e)) return;
    if (e.key === 'Escape') {
      // one Esc per layer of state: open menu / modal, then an armed mode,
      // then the tool panel, then the selection
      if (document.querySelector('.ctxmenu')) return;
      if (app.tools.armed) { app.tools.stop(); return; }
      if (app.tools.active) { app.tools.close(); return; }
      if (app.picked) { app.picked = null; R.highlight(null); renderInspector(); return; }
      select(null); return;
    }
    const k = e.key;
    if (k === 'f' || k === 'Home') { const b = app.project && app.project.bounds(); if (b) R.fitTo(b); }
    else if (k === 't' || k === 'd') R.viewFrom('top');
    else if (k === 'n') R.viewFrom('north'); else if (k === 's') R.viewFrom('south'); else if (k === 'e') R.viewFrom('east'); else if (k === 'w') R.viewFrom('west'); else if (k === 'u') R.viewFrom('below'); else if (k === 'i') R.viewFrom('iso');
    else if (k === 'o' || k === 'p') setProjection(k === 'o' ? 'ortho' : 'persp');
    else if (k === 'l' || k === 'L') lookAtSection(e.shiftKey);
    else if (k === '?') helpModal();
    else return;
    renderViewInfo();
  });
  // toolbar
  $('btnImport').onclick = () => $('fileIn').click();
  $('fileIn').onchange = async e => { for (const f of e.target.files) await importFile(f); e.target.value = ''; app.importGroup = null; };
  document.addEventListener('dragover', e => { e.preventDefault(); $('drop').style.display = 'flex'; });
  // #drop is pointer-events:none, so a dragleave listener on it can never fire
  // and the overlay stayed up over the viewport after any drag that did not end
  // in a drop.  Watch the document and hide when the pointer leaves the window.
  document.addEventListener('dragleave', e => { if (!e.relatedTarget) $('drop').style.display = 'none'; });
  document.addEventListener('dragend', () => { $('drop').style.display = 'none'; });
  document.addEventListener('drop', async e => { e.preventDefault(); $('drop').style.display = 'none'; for (const f of e.dataTransfer.files) await importFile(f); });
  $('btnExport').onclick = e => { if (needProject()) exportMenu(e.currentTarget); };
  $('saveBtn').onclick = () => { if (needProject()) saveProject(true); };
  $('btnProjects').onclick = e => projectsMenu(e.currentTarget);
  $('btnView').onclick = e => { if (needProject()) viewMenu(e.currentTarget); };
  $('btnTools').onclick = e => { if (needProject()) app.tools.menu(e.currentTarget); };
  $('btnHelp').onclick = () => helpModal();
  $('brand').onclick = () => showStart();
  $('siteName').onclick = async () => { if (!needProject()) return; const n = await promptModal('RENAME PROJECT', 'name', app.project.name); if (n && n.trim()) { app.project.name = n.trim(); renderHeader(); markDirty(); } };
  // the map tab is usually still open behind this one: go back to it rather
  // than loading the whole map again in this tab
  $('btnMap').onclick = () => { const s = app.project && app.project.site; if (window.opener && !window.opener.closed) { try { window.opener.focus(); window.close(); return; } catch (e) { /* fall through */ } } location.href = s && s.lon != null ? `index.html#12/${s.lat}/${s.lon}` : 'index.html'; };
  $('veRange').oninput = e => { R.setVE(+e.target.value); $('veLbl').textContent = `VE ×${(+e.target.value).toFixed(1)}`; renderViewInfo(); if (Math.abs(+e.target.value - 1) < 1e-6) status('VE ×1.0 — true vertical scale'); };
  $('north').onclick = () => { R.viewFrom('north'); renderViewInfo(); };
  for (const b of document.querySelectorAll('#viewtools button')) {
    const v = b.dataset.view, a = b.dataset.act;
    b.onclick = () => { if (!app.project) return; if (v) R.viewFrom(v); else if (a === 'fit') { const bb = app.project.bounds(); if (bb) R.fitTo(bb); } else if (a === 'proj') setProjection(R.projection === 'ortho' ? 'persp' : 'ortho'); else if (a === 'section') app.tools.open('section'); else if (a === 'legend') { app.showLegend = !app.showLegend; renderLegend(); } renderViewInfo(); };
  }
  $('lfilter').oninput = e => { app.layerFilter = e.target.value; renderLayers(); };
  $('lfilter').onkeydown = e => { if (e.key === 'Escape') { e.target.value = ''; app.layerFilter = ''; renderLayers(); e.target.blur(); } };
  $('btnLayers').onclick = () => { $('leftPane').classList.toggle('open'); $('rightPane').classList.remove('open'); };
  $('btnPanel').onclick = () => { $('rightPane').classList.toggle('open'); $('leftPane').classList.remove('open'); };
  // the 2-D section strip is resizable by its top edge
  const grip = document.querySelector('#sec2d .grip'); if (grip) { let y0 = 0, h0 = 0; grip.onpointerdown = e => { y0 = e.clientY; h0 = $('sec2d').offsetHeight; grip.setPointerCapture(e.pointerId); }; grip.onpointermove = e => { if (!grip.hasPointerCapture(e.pointerId)) return; const hgt = Math.max(140, Math.min(window.innerHeight * 0.6, h0 - (e.clientY - y0))); $('sec2d').style.height = hgt + 'px'; R.resize(); app.tools.section.draw2d(); }; }
  let legendTick = 0;
  R.onRender = () => { const az = R.northArrow(); $('north').style.transform = `rotate(${-az}rad)`; const now = performance.now(); if (now - legendTick > 250) { legendTick = now; renderLegend(); renderViewInfo(); } };
  window.addEventListener('beforeunload', () => { if (app.dirty) saveProject(); });
}
function lookAtSection(flip) {
  const t = app.tools.section; const pl = t.plane && t.plane(); if (!pl) { toast('no section to look at — TOOLS ▾ > 9 Section & slice', 'info'); return; }
  const n = pl.normal; const side = (t.side || 1) * (flip ? -1 : 1);
  const R = app.R; const tg = R.controls.target.clone(), d = R.camera.position.distanceTo(tg);
  const v = new THREE.Vector3(n[0] * side, 0.05, -n[1] * side).normalize();
  R.camera.position.copy(tg).addScaledVector(v, d); R.camera.up.set(0, 1, 0); R.controls.update(); R.invalidate();
}
function renderHeader() { const p = app.project; $('siteName').textContent = p ? p.name : '—'; $('crsBadge').textContent = p && p.crs.kind === 'utm' ? `UTM ${p.crs.zone}${p.crs.north ? 'N' : 'S'} · EPSG:${p.crs.epsg}` : 'local XYZ'; document.title = `3D MODEL — ${p ? p.name : ''}`; }
export function status(t) { $('status').textContent = t || ''; }
function renderViewInfo() {
  const el = $('viewinfo'); if (!el || !app.R) return;
  const R = app.R; const vp = R.viewAzPlunge ? R.viewAzPlunge() : null;
  el.textContent = `${R.projection === 'ortho' ? 'orthographic · scale exact' : 'perspective · scale nominal'} · VE ×${R.ve.toFixed(1)}${vp ? ` · looking ${vp.az.toFixed(0)}° / ${vp.plunge.toFixed(0)}° down` : ''}`;
  const pb = $('projBtn'); if (pb) { pb.textContent = R.projection === 'ortho' ? 'ORTHO' : 'PERSP'; pb.classList.toggle('on', R.projection === 'ortho'); }
  const kb = document.querySelector('#viewtools [data-act=legend]'); if (kb) kb.classList.toggle('on', !!app.showLegend);
}

/* ------------------------------------------------ projection + legend */
export function setProjection(kind) {
  const k = app.R.setProjection(kind);
  status(k === 'ortho' ? 'orthographic — the scale bar is exact in this mode' : 'perspective — the scale bar is only nominal');
  renderLegend(); renderViewInfo();
}

function niceLength(v) {
  const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
  return (m >= 5 ? 5 : m >= 2 ? 2 : 1) * p;
}
/** One sentence, shared by the banner and the rendered image, so the two can
    never say different things. */
export function confidenceSentence(t) {
  const soft = t.described + t.assumed; if (!soft) return '';
  const parts = [];
  if (t.described) parts.push(`${t.described} read off a written description`);
  if (t.assumed) parts.push(`${t.assumed} supplied in answer to a gap`);
  if (t.surveyed) parts.push(`${t.surveyed} traced off a georeferenced plan`);
  return `NOT A SURVEY — ${soft} of ${soft + t.surveyed} workings ${soft === 1 ? 'is' : 'are'} digitised, not surveyed: ${parts.join('; ')}. Dashed is described, dotted is assumed. Never enter old workings.`;
}
/* The honesty banner.  A model built from old prose is a digitising bridge,
   not new evidence, and the failure mode to design against is a hand-drawn
   adit being read as a survey.  So whenever a project carries any working that
   was not surveyed, the viewport says so in words — the dashed linework alone
   is too easy to miss. */
export function renderConfidence() {
  const host = $('confbar'); if (!host) return;
  clear(host);
  const t = app.project ? GMR.confidenceTally(app.project) : { surveyed: 0, described: 0, assumed: 0 };
  const soft = t.described + t.assumed;
  const vp = $('viewport');
  if (!soft || app.confBarDismissed) { host.style.display = 'none'; if (vp) vp.style.setProperty('--bannerH', '0px'); return; }
  const s = confidenceSentence(t); const i = s.indexOf(' — ');
  host.style.display = 'flex';
  host.appendChild(h('span', {}, h('b', {}, s.slice(0, i + 3)), s.slice(i + 3)));
  host.appendChild(h('button', { class: 'x', title: 'hide until the next model (VIEW ▾ brings it back)', onclick: () => { app.confBarDismissed = true; renderConfidence(); } }, '✕'));
  if (vp) vp.style.setProperty('--bannerH', host.offsetHeight + 'px');
}

/** The pieces of the legend as data, so the DOM legend and the rendered
    image draw the same thing. */
export function legendModel() {
  const R = app.R; const out = { scale: null, confidence: [], keys: [] };
  if (!app.project) return out;
  const mpp = R.metresPerPixel(); const target = niceLength(mpp * 110); const px = Math.max(24, Math.min(220, target / mpp));
  out.scale = { px, label: (target >= 1000 ? `${(target / 1000).toFixed(target % 1000 ? 1 : 0)} km` : `${Math.round(target)} m`) + (R.projection === 'ortho' ? '' : ' (nominal)'), nominal: R.projection !== 'ortho', mpp };
  const tally = GMR.confidenceTally(app.project);
  const anyWorkings = app.project.objects.some(o => o.visible !== false && ((o.kind === 'lineset' && o.role === 'workings' && o.parts.length) || (o.kind === 'mesh' && o.role === 'stope')));
  if (anyWorkings) {
    out.confidence = GMR.CONF_CLASSES.map(c => ({ label: c.label, hint: c.hint, dash: c.dash, count: tally[c.key] }));
    const types = new Map();
    for (const o of app.project.objects) { if (o.visible === false) continue; if (o.kind === 'lineset' && o.role === 'workings') for (const f of o.features) types.set(f.type || 'unknown', (E.WORKING_TYPES[f.type] || E.WORKING_TYPES.unknown).color); else if (o.kind === 'mesh' && o.role === 'stope') types.set('stope', (E.WORKING_TYPES.stope || E.WORKING_TYPES.unknown).color); }
    if (types.size) out.keys.push({ title: 'working types', rows: [...types].map(([t, c]) => ({ label: t, color: c })) });
  }
  const claims = app.project.byKind('points').filter(p => p.role === 'claims' && p.visible !== false);
  if (claims.length) out.keys.push({ title: 'claims (BLM centroids)', rows: claims.map(c => ({ label: c.name.replace(/\s*\(BLM centroids\)/, ''), color: c.color })) });
  const o = app.selected ? app.project.get(app.selected) : null; const L = o ? R.layers.get(o.id) : null;
  if (L && (L.range || L.categories)) {
    const d = app.display.get(o.id) || {};
    if (L.categories) { const cols = L.categoryColors || []; out.keys.push({ title: o.name, rows: L.categories.slice(0, 12).map((c, i) => ({ label: String(c), color: cols[i] || GM.colormap(GMR.defaultColormap(o, d, true), L.categories.length > 1 ? i / (L.categories.length - 1) : 0) })), more: Math.max(0, L.categories.length - 12) }); }
    else out.keys.push({ title: o.name, ramp: GMR.defaultColormap(o, d), range: L.range, attribute: d.attribute || 'value' });
  }
  return out;
}
/** Legend for the selected layer plus a scale bar.  Leapfrog only draws a
    scale bar in orthographic because perspective foreshortens it; we say so
    rather than drawing a number that is quietly wrong. */
export function renderLegend() {
  const host = $('legend'); if (!host) return;
  clear(host);
  if (!app.showLegend || !app.project) { host.style.display = 'none'; return; }
  host.style.display = 'flex';
  const M = legendModel();
  if (M.scale) host.appendChild(h('div', { class: 'lgd-scale', title: M.scale.nominal ? 'perspective foreshortens: the bar is exact only at the orbit centre — press o for orthographic' : 'orthographic: the bar is exact everywhere' }, h('div', { class: 'bar', style: { width: M.scale.px.toFixed(0) + 'px' } }), h('span', {}, M.scale.label)));
  // what the line styles mean, whenever any working is on screen — a model
  // whose workings are all described still needs the reader to know that
  // dashed means described
  if (M.confidence.length) {
    const box = h('div', { class: 'lgd-key' }, h('div', { class: 'ttl' }, 'workings'));
    for (const c of M.confidence) box.appendChild(h('div', { class: 'sw-row' + (c.count ? '' : ' dim'), title: c.hint }, lineSample(c.dash), `${c.label} · ${c.count}`));
    host.appendChild(box);
  }
  for (const k of M.keys) {
    const box = h('div', { class: 'lgd-key' }, h('div', { class: 'ttl' }, String(k.title || '').slice(0, 34)));
    if (k.rows) { for (const r of k.rows) box.appendChild(h('div', { class: 'sw-row' }, h('i', { style: { background: `rgb(${r.color[0] | 0},${r.color[1] | 0},${r.color[2] | 0})` } }), r.label.slice(0, 24))); if (k.more) box.appendChild(h('div', { class: 'sw-row more' }, `+${k.more} more`)); }
    else { const cv = h('canvas', { width: 140, height: 10, class: 'ramp' }); const cx = cv.getContext('2d'); for (let i = 0; i < 140; i++) { const c = GM.colormap(k.ramp, i / 139); cx.fillStyle = `rgb(${c[0] | 0},${c[1] | 0},${c[2] | 0})`; cx.fillRect(i, 0, 1, 10); } box.appendChild(cv); box.appendChild(h('div', { class: 'sw-row rng' }, h('span', {}, fmtNum(k.range[0])), h('span', {}, k.attribute), h('span', {}, fmtNum(k.range[1])))); }
    host.appendChild(box);
  }
}

/* ------------------------------------------------------- render image */
/** The exported picture carries what the screen carries: scale bar, the
    confidence key and sentence, north, the view direction and a footer with
    project, CRS, VE and date — a shared image must not lose the honesty cues
    that live in HTML overlays. */
export function renderImage(opts = {}) {
  const R = app.R; const src = R.canvas; const scale = opts.scale || 1;
  R.renderer.render(R.scene, R.camera);
  const W = Math.round(src.width * scale), H = Math.round(src.height * scale);
  const out = document.createElement('canvas'); out.width = W; out.height = H; const ctx = out.getContext('2d');
  ctx.drawImage(src, 0, 0, W, H);
  const M = legendModel(); const t = GMR.confidenceTally(app.project); const sent = confidenceSentence(t);
  const k = W / src.clientWidth * scale / scale; // css px -> image px
  const f = Math.max(10, Math.round(11 * (W / Math.max(1, src.clientWidth))));
  ctx.font = `${f}px ui-monospace, Menlo, monospace`; ctx.textBaseline = 'middle';
  const pad = f;
  // scale bar
  if (M.scale) { const bw = M.scale.px * (W / Math.max(1, src.clientWidth)); const x = pad, y = H - pad * 3.2; ctx.fillStyle = 'rgba(11,14,19,.8)'; ctx.fillRect(x - 6, y - f, bw + 12 + ctx.measureText(M.scale.label).width + 10, f * 2.4); ctx.fillStyle = '#e6edf3'; ctx.fillRect(x, y, bw / 2, 5); ctx.strokeStyle = '#e6edf3'; ctx.strokeRect(x, y, bw, 5); ctx.fillText(M.scale.label, x + bw + 8, y + 2); }
  // confidence key
  let y = H - pad * 5.5;
  if (M.confidence.length) { for (const c of [...M.confidence].reverse()) { if (!c.count) continue; ctx.fillStyle = 'rgba(11,14,19,.8)'; ctx.fillRect(pad - 6, y - f * 0.8, f * 14, f * 1.6); ctx.strokeStyle = '#e6edf3'; ctx.lineWidth = 2; ctx.setLineDash(c.dash ? (c.dash[0] > 5 ? [8, 5] : [2, 4]) : []); ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(pad + f * 2.2, y); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = '#e6edf3'; ctx.fillText(`${c.label} · ${c.count}`, pad + f * 2.8, y); y -= f * 1.7; } }
  // banner
  if (sent) { ctx.font = `bold ${f}px system-ui, sans-serif`; const words = sent.split(' '); const lines = []; let line = ''; for (const w of words) { const cand = line ? line + ' ' + w : w; if (ctx.measureText(cand).width > W * 0.7) { lines.push(line); line = w; } else line = cand; } if (line) lines.push(line); const bh = lines.length * f * 1.4 + f; ctx.fillStyle = 'rgba(28,20,8,.92)'; ctx.fillRect(W * 0.15 - 8, pad - 4, W * 0.7 + 16, bh); ctx.strokeStyle = '#c98500'; ctx.lineWidth = 2; ctx.strokeRect(W * 0.15 - 8, pad - 4, W * 0.7 + 16, bh); ctx.fillStyle = '#f1c27b'; lines.forEach((l, i) => ctx.fillText(l, W * 0.15, pad + f * 0.7 + i * f * 1.4)); }
  // north + view direction
  const az = R.northArrow(); const vp = R.viewAzPlunge ? R.viewAzPlunge() : null; const cx = W - pad * 3, cy = pad * 3; ctx.save(); ctx.translate(cx, cy); ctx.fillStyle = 'rgba(11,14,19,.7)'; ctx.beginPath(); ctx.arc(0, 0, f * 2, 0, Math.PI * 2); ctx.fill(); ctx.rotate(-az); ctx.fillStyle = '#2dd4bf'; ctx.beginPath(); ctx.moveTo(0, -f * 1.6); ctx.lineTo(f * 0.6, f * 0.4); ctx.lineTo(0, 0); ctx.lineTo(-f * 0.6, f * 0.4); ctx.closePath(); ctx.fill(); ctx.restore(); ctx.font = `${f}px ui-monospace, monospace`; ctx.fillStyle = '#c9d1d9'; ctx.textAlign = 'right'; if (vp) ctx.fillText(`looking ${vp.az.toFixed(0)}° · ${vp.plunge.toFixed(0)}° down`, W - pad, cy + f * 2.8); ctx.textAlign = 'left';
  // footer
  const p = app.project; const foot = `${p.name} · ${p.crs.kind === 'utm' ? `WGS84 / UTM ${p.crs.zone}${p.crs.north ? 'N' : 'S'} (EPSG:${p.crs.epsg})` : 'local XYZ'} · VE ×${R.ve.toFixed(1)}${R.ve !== 1 ? ' — vertical not to scale' : ''} · ${R.projection === 'ortho' ? 'orthographic' : 'perspective (scale nominal)'} · ${new Date().toISOString().slice(0, 10)} · NW Mineral Monitor 3-D model (${BUILD})`;
  ctx.fillStyle = 'rgba(11,14,19,.85)'; ctx.fillRect(0, H - f * 1.8, W, f * 1.8); ctx.fillStyle = '#8a97a6'; ctx.fillText(foot, pad, H - f * 0.9);
  return out;
}
function renderImageDialog() {
  const scale = sel([[1, 'screen size'], [2, '2× (print)'], [3, '3×']], 2);
  const body = h('div', {}, note('The image carries the scale bar, the line-style key, the NOT A SURVEY sentence when any working is not surveyed, north, the viewing direction and a footer with the project, CRS, vertical exaggeration and date. Those are not optional: a picture without them reads as a survey.'),
    row('size', scale), h('div', { class: 'frow' }, btn('RENDER + DOWNLOAD', () => { m.close(); try { const c = renderImage({ scale: +scale.value }); c.toBlob(b => GM.downloadBlob(b, `${GM.slug(app.project.name)}-3d.png`), 'image/png'); status('image rendered'); } catch (e) { toast('render failed: ' + e.message, 'err'); } }, { class: 'primary' }), btn('COPY TO CLIPBOARD', async () => { try { const c = renderImage({ scale: +scale.value }); const b = await new Promise(r => c.toBlob(r, 'image/png')); await navigator.clipboard.write([new ClipboardItem({ 'image/png': b })]); toast('copied', 'ok'); m.close(); } catch (e) { toast('clipboard refused: ' + e.message, 'warn'); } })));
  const m = modal('RENDER IMAGE', body);
}

/* --------------------------------------------------------------- menus */
function viewMenu(anchor) {
  const R = app.R; const scenes = (app.project.metadata.scenes || []);
  const chk = on => (on ? '☑ ' : '☐ ');
  menu(anchor, [
    { head: 'LOOK FROM' },
    { label: 'Fit all', hint: 'f · Home', onclick: () => { const b = app.project.bounds(); if (b) R.fitTo(b); } },
    { label: 'Zoom to workings', hint: workingsBounds() ? 'the underground' : 'none yet', disabled: !workingsBounds(), onclick: () => zoomToWorkings() },
    { label: 'Plan (north up)', hint: 'd', onclick: () => R.viewFrom('top') }, { label: 'Look north', hint: 'n', onclick: () => R.viewFrom('north') }, { label: 'Look south', hint: 's', onclick: () => R.viewFrom('south') }, { label: 'Look east', hint: 'e', onclick: () => R.viewFrom('east') }, { label: 'Look west', hint: 'w', onclick: () => R.viewFrom('west') }, { label: 'From below', hint: 'u', onclick: () => R.viewFrom('below') }, { label: 'Isometric', hint: 'i', onclick: () => R.viewFrom('iso') },
    { label: 'Look at the section', hint: 'l · Shift+l flips', onclick: () => lookAtSection(false) },
    '-', { head: 'PROJECTION' },
    { label: (R.projection === 'ortho' ? '● ' : '○ ') + 'Orthographic', hint: 'o · scale exact', onclick: () => setProjection('ortho') },
    { label: (R.projection !== 'ortho' ? '● ' : '○ ') + 'Perspective', hint: 'p · scale nominal', onclick: () => setProjection('persp') },
    '-', { head: 'SHOW' },
    { label: chk(app.showLegend) + 'Legend + scale bar', onclick: () => { app.showLegend = !app.showLegend; renderLegend(); renderViewInfo(); } },
    { label: chk(!app.confBarDismissed) + 'NOT A SURVEY banner', hint: GMR.confidenceTally(app.project).described + GMR.confidenceTally(app.project).assumed ? '' : 'nothing digitised', onclick: () => { app.confBarDismissed = !app.confBarDismissed; renderConfidence(); } },
    { label: chk(app.seeThrough) + 'See-through ground', hint: 'workings below read', onclick: () => setSeeThrough(!app.seeThrough) },
    { label: chk(app.tools.section.panelOpen) + '2-D section panel', onclick: () => app.tools.section.togglePanel() },
    '-', { head: 'DRAPE' },
    ...Object.entries(SITE.IMAGERY).map(([k, v]) => ({ label: (app.imagery === k ? '● ' : '○ ') + v.name, onclick: () => applyImagery(k) })),
    '-', { head: 'SCENES' },
    { label: 'Save scene…', hint: 'camera + visibility', onclick: () => saveScene() },
    ...scenes.map(s => ({ label: s.name, hint: s.created.slice(0, 10), onclick: () => restoreScene(s) })),
    scenes.length ? { label: 'Delete a scene…', onclick: () => deleteSceneDialog() } : null,
    '-',
    { label: 'Render image (PNG with overlays)…', onclick: () => renderImageDialog() },
  ]);
}
async function saveScene() {
  const name = await promptModal('SAVE SCENE', 'name', `Scene ${(app.project.metadata.scenes || []).length + 1}`, { note: 'A scene remembers the camera, projection, vertical exaggeration, which layers are on and their opacity, the drape and the active section — the data underneath stays live.' });
  if (!name) return;
  const sec = app.tools.section;
  const s = { id: 'scene-' + Date.now().toString(36), name, created: new Date().toISOString(), view: app.R.getView(), imagery: app.imagery, legend: app.showLegend, seeThrough: app.seeThrough, visible: Object.fromEntries(app.project.objects.map(o => [o.id, o.visible !== false])), opacity: Object.fromEntries(app.project.objects.map(o => [o.id, o.opacity == null ? 1 : o.opacity])), section: sec.sec ? { id: sec.sec.id, offset: sec.offset, slice: sec.slice, side: sec.side, band: sec.band, panel: sec.panelOpen } : null, selected: app.selected };
  (app.project.metadata.scenes = app.project.metadata.scenes || []).push(s); markDirty(); renderLayers(); toast(`scene "${name}" saved — VIEW ▾ > SCENES`, 'ok');
}
function restoreScene(s) {
  for (const o of app.project.objects) { if (s.visible && s.visible[o.id] != null) { o.visible = s.visible[o.id]; app.R.setVisible(o.id, o.visible); } if (s.opacity && s.opacity[o.id] != null) { o.opacity = s.opacity[o.id]; app.R.setOpacity(o.id, o.opacity); } }
  if (s.imagery && s.imagery !== app.imagery) applyImagery(s.imagery);
  app.showLegend = s.legend !== false; app.seeThrough = !!s.seeThrough;
  const sec = app.tools.section; if (s.section) { const so = app.project.get(s.section.id); if (so) { sec.sec = so; sec.offset = s.section.offset || 0; sec.slice = !!s.section.slice; sec.side = s.section.side || 1; sec.band = s.section.band || 25; sec.update(); if (s.section.panel) sec.setPanel(true); } } else { sec.sec = null; sec.update(); }
  app.R.setView(s.view); $('veRange').value = app.R.ve; $('veLbl').textContent = `VE ×${app.R.ve.toFixed(1)}`;
  if (s.selected && app.project.get(s.selected)) select(s.selected); else select(null);
  renderLayers(); renderLegend(); renderViewInfo(); toast(`scene "${s.name}"`, 'info', 2000);
}
function deleteSceneDialog() {
  const scenes = app.project.metadata.scenes || [];
  const body = h('div', {}, ...scenes.map(s => h('div', { class: 'frow' }, h('span', { style: { flex: 1 } }, s.name), btn('DELETE', () => { const i = scenes.indexOf(s); if (i >= 0) scenes.splice(i, 1); markDirty(); m.close(); toast(`scene "${s.name}" deleted`, 'warn', 6000, { action: { label: 'UNDO', onclick: () => { scenes.splice(Math.min(i, scenes.length), 0, s); markDirty(); } } }); }, { class: 'danger x' }))));
  const m = modal('DELETE A SCENE', body);
}
async function projectsMenu(anchor) {
  const list = await GM.store.listProjects().catch(() => []);
  const has = !!app.project;
  menu(anchor, [
    { label: 'Save now', disabled: !has, onclick: () => saveProject(true) },
    { label: 'Rename project', disabled: !has, onclick: async () => { const n = await promptModal('RENAME PROJECT', 'name', app.project.name); if (n && n.trim()) { app.project.name = n.trim(); renderHeader(); markDirty(); } } },
    { label: 'Save a copy as…', disabled: !has, onclick: async () => { const n = await promptModal('SAVE A COPY', 'name', app.project.name + ' (copy)'); if (!n) return; const j = JSON.parse(app.project.serialize()); const p = GM.Project.fromJSON(j); p.name = n; p.site = Object.assign({}, p.site || {}); const key = GM.slug(n) + '-' + Date.now().toString(36); p.site.key = key; await GM.store.saveProject(p); toast(`saved "${n}" — PROJECTS ▾ lists it`, 'ok'); } },
    { label: 'Rebuild from map data (discard edits)', disabled: !(has && app.project.site && app.project.site.lon != null), onclick: async () => { const s = app.project.site; if (!(await confirmModal('REBUILD FROM MAP DATA', 'Rebuild this site from the map data? Every edit in this project is lost. Export the project JSON first if you want to keep it.', { ok: 'REBUILD', danger: true }))) return; await GM.store.deleteProject(app.key); location.search = `?lat=${s.lat}&lon=${s.lon}&name=${encodeURIComponent(s.name || '')}&r=${s.radius_m || 2500}&aoi=${s.aoi || 'auto'}${s.grade_index != null ? '&gi=' + s.grade_index : ''}&fresh=1`; } },
    '-', { head: 'SAVED IN THIS BROWSER' },
    ...(list.length ? list.map(p => ({ label: (p.id === app.key ? '● ' : '○ ') + p.name, hint: p.modified.slice(0, 10), onclick: async () => { const pr = await GM.store.loadProject(p.id); if (pr) setProject(pr, p.id); } })) : [{ label: 'no saved models yet', disabled: true }]),
    '-',
    { label: 'Open a model / start a new one…', onclick: () => showStart() },
    { label: 'Delete this project from browser storage…', disabled: !has, onclick: async () => { if (!(await confirmModal('DELETE SAVED PROJECT', `Delete the saved copy of "${app.project.name}" from this browser? The page closes the project; export the JSON first to keep it.`, { ok: 'DELETE', danger: true }))) return; await GM.store.deleteProject(app.key); for (const o of app.project.objects) app.R.remove(o.id); app.project = null; app.key = null; clearTimeout(app.saveTimer); app.dirty = false; $('saveBtn').classList.remove('dirty'); renderSaveStat(''); renderHeader(); renderLayers(); renderInspector(); renderConfidence(); renderLegend(); app.tools.close(); toast('deleted'); showStart(); } },
  ]);
}

/* --------------------------------------------------------------- import */
export async function importFile(file) {
  status(`reading ${file.name}…`);
  try { const bytes = new Uint8Array(await file.arrayBuffer()); await importBytes(file.name, bytes, {}); }
  catch (e) { console.error(e); toast(`${file.name}: ${e.message}`, 'err', 8000); status(`import failed: ${e.message}`); }
}
/* A drop onto the opening screen has no project to land in.  Make one, in the
   UTM zone the data itself sits in when the file says — an empty project forced
   to zone 12 would misproject anything from a neighbouring state. */
function ensureProject(name, lonlat) {
  if (app.project) return;
  const z = lonlat ? GM.utm.zone(lonlat[0], lonlat[1]) : { zone: 12, north: true };
  setProject(new GM.Project({ name: name.replace(/\.[^.]+$/, ''), crs: GM.utm.crs(z.zone, z.north) }), GM.slug(name));
}
function firstLonLat(j) {
  const feats = j.type === 'Feature' ? [j] : (j.features || []);
  for (const f of feats) {
    let c = f && f.geometry && f.geometry.coordinates;
    while (Array.isArray(c) && Array.isArray(c[0])) c = c[0];
    if (Array.isArray(c) && GM.utm.looksLonLat(c[0], c[1])) return [c[0], c[1]];
  }
  return null;
}
function replaceOrMerge(p) {
  return new Promise(resolve => {
    const done = v => { m.close(); resolve(v); };
    const m = modal('LOAD PROJECT', h('div', {}, h('p', {}, `"${p.name}" has ${p.objects.length} layers. Replace the open project "${app.project.name}", or merge its layers into it?`), note('Merge adds every layer of the file to the open project and keeps both copies of anything with the same id.'), h('div', { class: 'frow', style: { justifyContent: 'flex-end' } }, btn('CANCEL', () => done('cancel')), btn('MERGE', () => done('merge')), btn('REPLACE', () => done('replace'), { class: 'danger' }))), { sticky: true });
  });
}
async function importBytes(name, bytes, opts) {
  const ext = name.split('.').pop().toLowerCase();
  if (ext === 'json' || ext === 'geojson') {
    const text = new TextDecoder().decode(bytes); const j = JSON.parse(text);
    if (j.schema === GM.SCHEMA) {
      const p = GM.Project.fromJSON(j);
      let how = 'replace';
      if (app.project && !opts.asProject) how = await replaceOrMerge(p);
      if (how === 'cancel') { status('import cancelled'); return; }
      if (how === 'replace') setProject(p, p.site && p.site.key); else for (const o of p.objects) { if (app.importGroup) o.group = app.importGroup; app.project.add(o); }
      toast(`loaded project ${p.name}`, 'ok'); return;
    }
    if (j.type === 'FeatureCollection' || j.type === 'Feature') { ensureProject(name, firstLonLat(j)); importGeoJSON(j, name); return; }
    throw new Error('unrecognised JSON (not a geomodel project or GeoJSON)');
  }
  // Everything below needs somewhere to put what it reads.  This has to sit
  // *after* the geomodel-project branch above, which installs its own project —
  // creating an empty one first would turn a first load into a merge prompt.
  ensureProject(name);
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tif', 'tiff'].includes(ext)) { await app.tools.georef.fromImageBytes(name, bytes); return; }
  if (ext === 'pdf') { await app.tools.georef.fromPdf(name, bytes); return; }
  if (ext === 'csv' || ext === 'txt' || ext === 'xyz' || ext === 'dat') { await importTableDialog(name, bytes); return; }
  const fmt = F.sniff(name, bytes);
  if (!fmt) throw new Error(`cannot detect the format of ${name}`);
  const res = await F.readAny({ name, bytes }, { crs: app.project.crs, format: fmt });
  await placeImported(res, name);
}
async function placeImported(res, name) {
  const objs = res.objects || [];
  if (!objs.length) throw new Error('nothing readable in ' + name);
  // grids: ask how to use them
  for (const o of objs) {
    if (o.kind === 'grid2d' && !o.role) o.role = 'surface';
    if (o.kind === 'grid2d') { const how = await askGridRole(o); if (how === 'cancel') continue; o.role = how; if (how === 'topography') { for (const g of app.project.byKind('grid2d')) if (g.role === 'topography') { g.role = 'surface'; syncObject(g); } app.topoId = o.id; } }
    if (!o.group) o.group = app.importGroup || 'Imports';
    if (!o.name) o.name = name;
    app.project.add(o);
  }
  if (res.project && res.project.metadata && res.project.metadata.warnings) for (const w of res.project.metadata.warnings) toast(w, 'warn', 6000);
  for (const w of (res.warnings || []).slice(0, 4)) toast(w, 'warn', 6000);
  const b = objs[0].bounds(); if (b && objs.length === 1) app.R.fitTo(b);
  status(`imported ${objs.length} object(s) from ${name} (${res.format})`);
  if (objs.length === 1) select(objs[0].id);
  if (app.topoId && objs.some(o => o.role === 'topography')) applyImagery(app.imagery);
}
function askGridRole(g) {
  return new Promise(resolve => {
    const zr = g.zrange();
    const body = h('div', {}, h('p', {}, `${g.name || 'grid'}: ${g.nx}×${g.ny} nodes, cell ${fmtNum(g.dx, 2)}×${fmtNum(g.dy, 2)}, values ${fmtNum(zr[0])} – ${fmtNum(zr[1])}. How should it be used?`),
      note(`The project is ${app.project.crs.kind === 'utm' ? `WGS84 / UTM ${app.project.crs.zone}${app.project.crs.north ? 'N' : 'S'} in metres` : 'local XYZ'}; nothing is reprojected, so the grid's coordinates must already be in that system. Its extent E ${fmtNum(g.x0, 0)} – ${fmtNum(g.x0 + g.dx * (g.nx - 1), 0)}, N ${fmtNum(g.y0, 0)} – ${fmtNum(g.y0 + g.dy * (g.ny - 1), 0)}${Math.abs(g.x0) <= 180 && Math.abs(g.y0) <= 90 ? ' looks like degrees, not metres' : ''}.`, Math.abs(g.x0) <= 180 && Math.abs(g.y0) <= 90 ? 'note warn' : 'note'),
      h('div', { class: 'frow' }, btn('TOPOGRAPHY (elevation)', () => { m.close(); resolve('topography'); }), btn('SURFACE / CONTACT (Z = value)', () => { m.close(); resolve('contact'); }), btn('PROPERTY (geophysics, drape colours)', () => { m.close(); resolve('property'); }), btn('cancel', () => { m.close(); resolve('cancel'); })),
      note('Property grids (magnetics, gravity, radiometrics, thickness) are coloured by value and draped on the topography; surfaces are placed at Z = value (contacts, horizons, water tables).'));
    const m = modal('IMPORT GRID', body, { sticky: true });
  });
}
function importGeoJSON(j, name) {
  const feats = j.type === 'Feature' ? [j] : j.features; const crs = app.project.crs; const topo = topoGrid();
  const ps = new GM.PointSet({ name: name.replace(/\.[^.]+$/, ''), role: 'points', group: app.importGroup || 'Imports', color: [255, 200, 80] }); const ls = new GM.LineSet({ name: name.replace(/\.[^.]+$/, ''), role: 'lines', group: app.importGroup || 'Imports', color: [255, 200, 80] });
  const toXYZ = c => { let [x, y, z] = c; if (GM.utm.looksLonLat(x, y) && crs.kind === 'utm') [x, y] = GM.utm.fwd(x, y, crs.zone, crs.north); if (z == null || z !== z) { const t = topo ? topo.sample(x, y) : NaN; z = t === t ? t + 2 : 0; } return [x, y, z]; };
  for (const f of feats) { const g = f.geometry; if (!g) continue; const props = f.properties || {}; if (g.type === 'Point') { const [x, y, z] = toXYZ(g.coordinates); ps.add(x, y, z, props); } else if (g.type === 'MultiPoint') for (const c of g.coordinates) { const [x, y, z] = toXYZ(c); ps.add(x, y, z, props); } else if (g.type === 'LineString') ls.addPolyline(g.coordinates.map(toXYZ), props); else if (g.type === 'MultiLineString') for (const l of g.coordinates) ls.addPolyline(l.map(toXYZ), props); else if (g.type === 'Polygon') for (const r of g.coordinates) ls.addPolyline(r.map(toXYZ), props); else if (g.type === 'MultiPolygon') for (const poly of g.coordinates) for (const r of poly) ls.addPolyline(r.map(toXYZ), props); }
  if (ps.n) app.project.add(ps); if (ls.parts.length) { if (feats.some(f => (f.properties || {}).layer === 'workings' || (f.properties || {}).type in E.WORKING_TYPES)) ls.role = 'workings'; app.project.add(ls); }
  toast(`GeoJSON: ${ps.n} points, ${ls.parts.length} lines`, 'ok');
}
async function importTableDialog(name, bytes) {
  const text = new TextDecoder().decode(bytes.subarray(0, 200000));
  const isGeosoftXyz = /^\s*\/|^Line\s+\d|^Tie\s+\d/m.test(text) && name.toLowerCase().endsWith('.xyz');
  let head = null; try { head = F.parseTable(text); } catch (e) { head = null; } const cols = head ? head.headers || [] : [];
  const kind = sel([['points', 'points (x, y, z + columns)'], ['structural', 'planar structural data (dip / dip azimuth)'], ['collar', 'drillhole collars (then add survey / intervals)'], ['blockmodel', 'block model (centroids + sizes)'], ['xyz', 'Geosoft XYZ line database']], isGeosoftXyz ? 'xyz' : 'points');
  const pick = (label, guess) => sel([['', '— auto —'], ...cols], cols.find(c => guess.test(c)) || '', {});
  const xs = pick('x', /^(x|east|easting|e|lon|longitude)$/i), ys = pick('y', /^(y|north|northing|n|lat|latitude)$/i), zs = pick('z', /^(z|elev|elevation|rl|alt)$/i);
  const guessCol = head && (xs.value || cols[0]) ? head.columns[xs.value || cols[0]] : null;
  const lonlat = h('input', { type: 'checkbox', checked: !!(guessCol && guessCol.length && guessCol.slice(0, 200).every(v => Math.abs(+v) <= 180)) });
  const body = h('div', {}, h('p', {}, `${name}: columns ${cols.join(', ') || '(none detected)'}`),
    row('table type', kind), row('X column', xs), row('Y column', ys), row('Z column', zs), row('lon/lat → UTM', lonlat),
    note('Z blank = draped on topography (+2 m). Lon/lat is converted to the project UTM zone.'),
    h('div', { class: 'frow' }, btn('IMPORT', async () => {
      m.close();
      try {
        const opts = { crs: app.project.crs, assumeLonLat: lonlat.checked, x: xs.value || undefined, y: ys.value || undefined, z: zs.value || undefined };
        let res;
        if (kind.value === 'xyz') res = await F.readAny({ name, bytes }, Object.assign(opts, { format: 'geosoft_xyz' }));
        else if (kind.value === 'collar') { res = await F.readAny({ name, bytes }, Object.assign(opts, { format: 'csv_drillholes' })); toast('collars loaded — the holes are vertical until a survey is added: select the layer and use ADD SURVEY / INTERVAL CSV…', 'info', 6000); }
        else res = await F.readAny({ name, bytes }, Object.assign(opts, { table: kind.value }));
        if (kind.value === 'structural') for (const o of res.objects) if (o.kind === 'points') { try { ST.normaliseStructural(o); } catch (err) { toast('structural columns: ' + err.message, 'warn', 7000); } }
        for (const o of res.objects) if (o.kind === 'points' && !zs.value) { const topo = topoGrid(); if (topo) for (let i = 0; i < o.n; i++) { if (o.xyz[3 * i + 2] === 0 || o.xyz[3 * i + 2] !== o.xyz[3 * i + 2]) { const t = topo.sample(o.xyz[3 * i], o.xyz[3 * i + 1]); if (t === t) o.xyz[3 * i + 2] = t + 2; } } }
        await placeImported(res, name);
      } catch (e) { toast(`${name}: ${e.message}`, 'err', 8000); }
    }, { class: 'primary' }), btn('cancel', () => m.close())));
  const m = modal('IMPORT TABLE', body, { sticky: true });
}

/* A collar CSV imports on its own, so the survey and the assay / lithology
   tables have to be attachable afterwards — this is the control the collar
   import sends the user to.  Each table is read back through readDrillholes
   against the collars we already hold, so it gets exactly the column-synonym
   matching and orphan-hole checking a whole CSV set would have got. */
function addDrillholeTable(dh) {
  const fileIn = h('input', { type: 'file', accept: '.csv,.txt,.tsv' });
  const kind = sel([['intervals', 'interval table (hole, from, to, assay / lithology…)'], ['survey', 'survey (hole, depth, azimuth, dip)']], 'intervals');
  const nameIn = txt('', { placeholder: 'from the file name' });
  const body = h('div', {},
    h('p', {}, `${dh.name}: ${dh.collars.length} collars, ${dh.surveys.length} survey rows, ${Object.keys(dh.intervals).length} interval table(s).`),
    row('CSV file', fileIn), row('table type', kind), row('table name', nameIn),
    note('The survey deviates the holes; an interval table becomes an entry in the layer’s "table" dropdown and can be sampled to points for kriging. Hole ids have to match the collar table — any that do not are reported.'),
    h('div', { class: 'frow' }, btn('ADD', async () => {
      const f = fileIn.files && fileIn.files[0];
      if (!f) return toast('choose a CSV first', 'warn');
      const table = (nameIn.value || f.name.replace(/\.[^.]+$/, '')).trim() || 'intervals';
      m.close();
      try {
        const bytes = new Uint8Array(await f.arrayBuffer());
        // only the collars go back out to CSV: re-serialising the intervals we
        // already hold would cost more than reading the new table
        const collar = F.writeDrillholes(new GM.Drillholes({ collars: dh.collars }))['collar.csv'];
        const read = F.readDrillholes(kind.value === 'survey' ? { collar, survey: bytes } : { collar, intervals: { [table]: bytes } }, { name: dh.name });
        const prefix = kind.value === 'survey' ? 'survey:' : table + ':';
        for (const w of (read.metadata.warnings || []).filter(w => w.startsWith(prefix)).slice(0, 4)) toast(w, 'warn', 7000);
        if (kind.value === 'survey') {
          dh.surveys = read.surveys;
          // the collar-only import stamped "holes are treated as vertical"; it
          // stops being true the moment a survey lands, so it must not persist
          dh.metadata.warnings = (dh.metadata.warnings || []).filter(w => !/no survey table/.test(w));
          toast(`${f.name}: ${read.surveys.length} survey rows — holes desurveyed`, 'ok');
        } else {
          dh.intervals[table] = read.intervals[table];
          const d = app.display.get(dh.id) || {}; if (!d.table) { d.table = table; app.display.set(dh.id, d); }
          toast(`${f.name}: ${read.intervals[table].length} rows as "${table}"`, 'ok');
        }
        refresh(dh); select(dh.id);
      } catch (e) { toast(`${f.name}: ${e.message}`, 'err', 8000); }
    }, { class: 'primary' }), btn('cancel', () => m.close())));
  const m = modal('ADD DRILLHOLE TABLE', body, { sticky: true });
}

/* --------------------------------------------------------------- export */
const EXPORTS = [
  ['omf2', 'OMF v2.0 — Leapfrog Geo 2025+, Evo', ['*']], ['omf1', 'OMF v0.9 — Leapfrog Geo ≤ 2024.1', ['*']], ['geomodel', 'Project JSON (.geomodel.json)', ['*']],
  ['dxf', 'DXF R12 (meshes, lines, points)', ['mesh', 'lineset', 'points']], ['obj', 'OBJ mesh', ['mesh']], ['gocad_ts', 'GOCAD TSurf / PLine / VSet', ['mesh', 'lineset', 'points']], ['lf_msh', 'Leapfrog .msh mesh', ['mesh']],
  ['surfer_grd', 'Surfer 7 grid (.grd)', ['grid2d']], ['geosoft_grd', 'Geosoft grid (.grd)', ['grid2d']], ['gxf', 'Geosoft GXF', ['grid2d']], ['arc_ascii', 'Arc/Info ASCII grid', ['grid2d']], ['zmap', 'ZMAP+ grid (Kingdom/Petrel)', ['grid2d']], ['irap', 'Irap classic grid', ['grid2d']],
  ['csv_points', 'Points CSV (East,North,Elev,…)', ['points']], ['csv_structural', 'Structural CSV', ['points']], ['geosoft_xyz', 'Geosoft XYZ', ['points']], ['csv_blockmodel', 'Block model CSV (+ header)', ['blockmodel']], ['ubc', 'UBC mesh + model', ['blockmodel']], ['csv_drillholes', 'Drillhole CSV set', ['drillholes']], ['geojson', 'GeoJSON (WGS84 footprint)', ['lineset', 'points']],
];
function exportMenu(anchor) {
  menu(anchor, [
    { head: 'WHOLE PROJECT' },
    { label: 'OMF v2.0 (Leapfrog 2025+)', onclick: () => exportObjects('omf2', app.project.objects) },
    { label: 'OMF v0.9 (Leapfrog ≤ 2024)', onclick: () => exportObjects('omf1', app.project.objects) },
    { label: '.geomodel.json (this viewer)', onclick: () => exportObjects('geomodel', app.project.objects) },
    { label: 'DXF', onclick: () => exportObjects('dxf', app.project.objects) },
    { label: 'Leapfrog kit (zip of everything, per-format)', onclick: () => exportKit() },
    '-',
    { label: 'Selected layer…', onclick: () => { const o = app.selected && app.project.get(app.selected); if (!o) return toast('select a layer first', 'warn'); exportDialog([o]); }, disabled: !app.selected },
    { label: 'Render image (PNG with overlays)…', onclick: () => renderImageDialog() },
  ]);
}
export function exportDialog(objs) {
  const kinds = new Set(objs.map(o => o.kind)); const opts = EXPORTS.filter(([id, label, ks]) => ks.includes('*') || objs.some(o => ks.includes(o.kind)));
  const body = h('div', {}, h('p', {}, `${objs.length === 1 ? objs[0].name : objs.length + ' layers'} (${[...kinds].join(', ')})`),
    ...opts.map(([id, label]) => btn(label, () => { m.close(); exportObjects(id, objs); }, { class: 'wide' })));
  const m = modal('EXPORT', body);
}
export async function exportObjects(fmt, objs) {
  try {
    status(`exporting ${fmt}…`); const base = GM.slug(objs.length === 1 ? objs[0].name : app.project.name);
    if (fmt === 'geomodel') { const p = objs === app.project.objects ? app.project : Object.assign(new GM.Project({ name: app.project.name, crs: app.project.crs, origin: app.project.origin, site: app.project.site, metadata: app.project.metadata }), { objects: objs }); GM.downloadBlob(new Blob([p.serialize()], { type: 'application/json' }), `${base}.geomodel.json`); status('exported project JSON'); return; }
    if (fmt === 'geojson') { const crs = app.project.crs; const fc = { type: 'FeatureCollection', features: [] }; for (const o of objs) { if (o.kind === 'lineset') fc.features.push(...E.workingsToGeoJSON(o, crs).features); if (o.kind === 'points') for (let i = 0; i < o.n; i++) { const [x, y, z] = o.point(i); const [lon, lat] = GM.utm.inv(x, y, crs.zone, crs.north); const props = {}; for (const [k, c] of Object.entries(o.attributes)) props[k] = c[i]; fc.features.push({ type: 'Feature', properties: props, geometry: { type: 'Point', coordinates: [+lon.toFixed(7), +lat.toFixed(7), +z.toFixed(2)] } }); } } GM.downloadBlob(new Blob([JSON.stringify(fc)], { type: 'application/geo+json' }), `${base}.geojson`); status('exported GeoJSON'); return; }
    const files = await F.writeAs(fmt, objs.length === 1 ? objs[0] : objs, { name: app.project.name, description: `NW Mineral Monitor model3d export (${BUILD})`, crs: app.project.crs.kind === 'utm' ? `EPSG:${app.project.crs.epsg}` : '', basename: base });
    const names = Object.keys(files);
    if (names.length === 1) { const v = files[names[0]]; GM.downloadBlob(new Blob([v]), names[0]); }
    else { const zip = new F.ZipWriter(); for (const [n, v] of Object.entries(files)) zip.add(n, typeof v === 'string' ? new TextEncoder().encode(v) : v); GM.downloadBlob(new Blob([zip.finish()]), `${base}-${fmt}.zip`); }
    status(`exported ${names.join(', ')}`);
  } catch (e) { console.error(e); toast(`export failed: ${e.message}`, 'err', 8000); status('export failed'); }
}
async function exportKit() {
  try {
    status('building kit…'); const p = app.project; const zip = new F.ZipWriter(); const base = GM.slug(p.name); const manifest = [];
    // Every per-layer writer was handed the *project* basename, so all the grids
    // landed on one `<project>.grd` and only the last survived the zip.  Each
    // single-object export gets its own basename, and any residual collision is
    // suffixed rather than silently overwritten.
    const used = new Set();
    const uniq = n => { if (!used.has(n)) { used.add(n); return n; } const dot = n.indexOf('.', n.lastIndexOf('/') + 1); const stem = dot < 0 ? n : n.slice(0, dot), ext = dot < 0 ? '' : n.slice(dot); for (let i = 2; ; i++) { const c = `${stem}-${i}${ext}`; if (!used.has(c)) { used.add(c); return c; } } };
    const add = async (fmt, objs, label, basename) => { try { const files = await F.writeAs(fmt, objs, { name: p.name, basename: basename || base, crs: `EPSG:${p.crs.epsg}` }); for (const [n, v] of Object.entries(files)) { const key = uniq(n); zip.add(key, typeof v === 'string' ? new TextEncoder().encode(v) : v); manifest.push(`- \`${key}\` — ${label}`); } } catch (e) { manifest.push(`- (${fmt} skipped: ${e.message})`); } };
    const layerBase = o => `${base}-${GM.slug(o.name || o.kind)}`;
    await add('omf2', p.objects, 'OMF v2.0 — Leapfrog Geo 2025.1+ (Leapfrog menu > OMF > Import)');
    await add('omf1', p.objects, 'OMF v0.9 — Leapfrog Geo ≤ 2024.1');
    zip.add(uniq(`${base}.geomodel.json`), new TextEncoder().encode(p.serialize())); manifest.push(`- \`${base}.geomodel.json\` — this viewer's project (drop on model3d.html)`);
    for (const g of p.byKind('grid2d')) { await add('surfer_grd', g, `Surfer 7 grid — ${g.name}`, layerBase(g)); await add('gxf', g, `Geosoft GXF — ${g.name}`, layerBase(g)); if (Math.abs(g.dx - g.dy) < 1e-9 && !g.rotation) await add('arc_ascii', g, `Arc/Info ASCII — ${g.name}`, layerBase(g)); }
    const ml = p.objects.filter(o => o.kind === 'mesh' || o.kind === 'lineset' || o.kind === 'points'); if (ml.length) await add('dxf', ml, 'DXF R12 — meshes as 3DFACE, lines as 3-D POLYLINE');
    for (const ps of p.byKind('points')) await add('csv_points', ps, `points CSV (East,North,Elev + columns) — ${ps.name}`, layerBase(ps));
    for (const bm of p.byKind('blockmodel')) { await add('csv_blockmodel', bm, `block model CSV — ${bm.name}`, layerBase(bm)); await add('ubc', bm, `UBC mesh/model — ${bm.name}`, layerBase(bm)); }
    for (const dh of p.byKind('drillholes')) await add('csv_drillholes', dh, `drillhole CSVs — ${dh.name}`, layerBase(dh));
    try { const img = renderImage({ scale: 2 }); const blob = await new Promise(r => img.toBlob(r, 'image/png')); zip.add(uniq(`${base}-3d.png`), new Uint8Array(await blob.arrayBuffer())); manifest.push(`- \`${base}-3d.png\` — the scene as rendered, with scale bar, confidence key and banner`); } catch (e) { manifest.push(`- (render image skipped: ${e.message})`); }
    const t = GMR.confidenceTally(p); const sent = confidenceSentence(t);
    const notes = p.objects.filter(o => o.metadata && o.metadata.notes).map(o => `- **${o.name}**: ${o.metadata.notes}`);
    const readme = `# ${p.name} — 3-D model kit\n\nExported ${new Date().toISOString().slice(0, 10)} from the NW Mineral Monitor 3-D modeller (build ${BUILD}).\nCRS: **WGS84 / UTM zone ${p.crs.zone}${p.crs.north ? 'N' : 'S'} (EPSG:${p.crs.epsg}), metres, Z = elevation** — set exactly that when a package asks.\n${sent ? `\n> **${sent}**\n` : ''}\n## Files\n\n${manifest.join('\n')}\n\n## Import click-paths\n\n- Leapfrog Geo 2025.1+: Leapfrog menu > OMF > Import > \`${base}.omf\` (one shot; OMF objects cannot be reloaded). Older Leapfrog: \`${base}-omf09.omf\`.\n- Refreshable route: Topographies > New Topography > Import Elevation Grid (.grd/.asc); Meshes > Import Mesh (.dxf/.obj); Points > Import Points (.csv); Block Models > Import (.csv).\n- Oasis montaj / Target: Grid and Image > Import > .gxf / .grd; XYZ/CSV for points.\n- Surfer: File > Open > .grd (Surfer 7 binary).\n- Kingdom: grids via ZMAP+ (export a grid layer as ZMAP+ from the layer menu).\n\n## Honesty notes\n\n${(p.metadata.notes || []).map(n => '- ' + n).join('\n')}\n- Draped geology meshes follow terrain; they are map polygons, not modelled volumes. Workings digitised from historic maps carry their source and confidence in each feature.\n${notes.length ? `\n## Layer notes\n\n${notes.join('\n')}\n` : ''}`;
    zip.add(uniq('README-GEOMODEL.md'), new TextEncoder().encode(readme));
    GM.downloadBlob(new Blob([zip.finish()]), `${base}-geomodel-kit.zip`); status('kit exported');
  } catch (e) { console.error(e); toast('kit failed: ' + e.message, 'err'); }
}

/* ----------------------------------------------------------------- help */
function helpModal() {
  const tabs = {
    'START HERE': () => h('div', {},
      h('p', {}, 'The page shows one historic mine site in 3-D. What you are looking at:'),
      h('div', { class: 'keyrow' }, h('span', { class: 'sw', style: { background: '#8a97a6' } }), 'the ground — real terrain (a public ~30 m composite), stretched by the VE slider so relief reads'),
      h('div', { class: 'keyrow' }, h('span', { class: 'sw', style: { background: '#5a7d5a' } }), 'the drape — satellite, USGS topo or the Macrostrat geology map (VIEW ▾ > DRAPE)'),
      h('div', { class: 'keyrow' }, h('span', { class: 'sw', style: { background: '#c98500' } }), 'mines — cited historic grades; claims — BLM centroids'),
      ...GMR.CONF_CLASSES.map(c => h('div', { class: 'keyrow' }, lineSample(c.dash), `workings drawn ${c.dash ? (c.dash[0] > 5 ? 'dashed' : 'dotted') : 'solid'} are ${c.label}: ${c.hint}`)),
      h('div', { class: 'keyrow' }, h('span', { class: 'sw', style: { background: 'rgba(45,212,191,.3)', border: '1px solid #2dd4bf' } }), 'the cyan panes are section lines you can cut along (hidden until you pick one)'),
      h('div', { class: 'keyrow' }, h('span', { class: 'sw', style: { background: '#2a1c08', border: '1px solid #c98500' } }), 'the gold banner appears whenever any working was not surveyed — it is the point of the page, not a nag'),
      h('p', {}, h('b', {}, 'First three clicks: ')),
      h('div', { class: 'frow' }, btn('SHOW THE WORKINGS', () => { m.close(); zoomToWorkings() && setSeeThrough(true); }), btn('CUT A SECTION', () => { m.close(); app.tools.open('section'); app.tools.section.preset('we'); }), btn('READ A LAYER', () => { m.close(); const o = app.project && (app.project.byKind('lineset').find(l => l.role === 'workings' && l.parts.length) || app.project.byKind('points').find(p => p.role === 'mines')); if (o) select(o.id); })),
      h('p', { class: 'note' }, 'Everything autosaves in this browser; EXPORT makes files (Leapfrog OMF, DXF, grids, CSV, the project JSON). Nothing leaves the browser.')),
    'THE ORDER': () => { const r = app.project ? app.tools.readiness() : null; return h('div', {}, h('p', {}, 'The nine tools are steps. A district with no drillholes is modelled from the map, the terrain, the old plans and the prose, in this order:'), h('table', { class: 'tbl' }, h('tr', {}, h('th', {}, '#'), h('th', {}, 'tool'), h('th', {}, 'needs'), h('th', {}, 'produces'), h('th', {}, '')), ...TOOL_STEPS.map(s => h('tr', {}, h('td', {}, String(s.step)), h('td', {}, h('b', {}, s.title), h('div', { class: 'note', style: { margin: '2px 0 0' } }, s.purpose)), h('td', {}, r ? r[s.key].needs.map(n => (n.ok ? '✓ ' : '✗ ') + n.label).join(', ') : ''), h('td', {}, r && r[s.key].has ? r[s.key].has : s.next), h('td', {}, btn('OPEN', () => { m.close(); app.tools.open(s.key); }, { class: 'x', disabled: !app.project })))))); },
    'NAVIGATE': () => h('div', {}, h('p', {}, h('b', {}, 'Mouse'), ' — left-drag orbits · right-drag pans · wheel zooms · double-click re-centres on what you clicked · right-click for a menu (sections through here, hide, properties) · hover reads out what anything is and where it came from.'), h('p', {}, h('b', {}, 'Keys'), ' — d plan (north up) · n s e w · u from below · i isometric · f / Home fit everything · o orthographic / p perspective · l look at the section (Shift+l flips) · Esc leaves a mode, then closes the tool, then clears the selection · Ctrl/Cmd+Z undoes the last delete.'), h('p', {}, h('b', {}, 'Right-hand buttons'), ' — the same views, FIT, ORTHO/PERSP, SLICE (the section tool) and KEY (legend + scale bar). The status bar says which projection you are in: the scale bar is exact only in orthographic.'), h('p', {}, h('b', {}, 'VE'), ' — vertical exaggeration; ×1.0 is true scale. The rendered image says which was used.')),
    'LAYERS': () => h('div', {}, h('p', {}, 'The tree is a workflow: INPUTS (what came from the map and the terrain), MODELS (what the steps produce), OUTPUTS (sections, notes). Groups a step fills are listed even when empty, with the step to open.'), h('p', {}, 'Tick to show / hide · swatch to recolour · ⋯ or right-click for zoom, show-only, rename, export, delete (with UNDO) · double-click zooms · the filter box narrows the list · badges: ⚠ warnings, ✕ failed to draw, ∅ nothing digitised or built yet.'), h('p', {}, 'Click a layer for its properties on the right: colour by attribute, cut-offs, labels, the table of its rows, provenance, notes that travel with the project. Pick something in the scene and the PICKED card shows the feature, its confidence and the sentence it was read from.')),
    'TOOLS': () => h('div', {}, ...TOOL_STEPS.map(s => h('p', {}, h('b', {}, `${s.step} ${s.title}`), ' — ' + s.purpose)), h('p', {}, 'A tool opens in the lower right; DONE ✕ or Esc closes it. While a click mode is armed a strip at the top-left says what the clicks do and how to get out; the layer inspector stays available above the tool.')),
    'HONESTY': () => h('div', {}, h('p', {}, 'Nothing is invented. A working read off a sentence draws dashed, one supplied in answer to a gap draws dotted, and only one traced off a georeferenced plan draws solid. The banner counts them; the legend decodes the line styles; the rendered image and the kit README carry the same sentence.'), h('p', {}, 'Orientations derived from a mapped trace carry the relief, spread and fit error they came from and are tagged inferred; flat ground yields nothing rather than a dip of zero. Kriging and the RBF are documented research tools, not a resource estimate. Grades are cited historic figures; claims are BLM centroids; terrain is a ~30 m composite; geology is map-scale.'), h('p', { class: 'note' }, 'Never enter adits or shafts.')),
    'IMPORT / EXPORT': () => h('div', {}, h('p', {}, h('b', {}, 'Import'), ' — drop files: OMF v0.9/v2 · DXF · OBJ · GOCAD .ts/.pl/.vs · Leapfrog .msh · Surfer/Geosoft .grd · GXF · Arc ASCII · ZMAP+ · Irap · UBC · CSV (points, structural, collars, block models) · Geosoft XYZ · SEG-Y (as a section image) · LAS · GeoJSON · images/PDF pages for georeferencing. Nothing is reprojected: the project is WGS84 / UTM in metres.'), h('p', {}, h('b', {}, 'Export'), ' — OMF v2.0 for Leapfrog 2025+, OMF v0.9 for older, DXF/OBJ/GOCAD, Surfer/Geosoft/GXF/ZMAP/Irap grids, CSV/XYZ points, block-model CSV + UBC, the project JSON, a rendered image with its overlays, or the whole kit as a zip with a README of click-paths. OMF objects cannot be reloaded in Leapfrog — re-import after a refresh, or use the grid / CSV route for layers you expect to update.')),
  };
  const body = h('div', { class: 'help' }); const bar = h('div', { class: 'tabs' }); const page = h('div');
  const show = k => { clear(page); page.appendChild(tabs[k]()); for (const b of bar.children) b.classList.toggle('on', b.textContent === k); };
  for (const k of Object.keys(tabs)) bar.appendChild(btn(k, () => show(k), { class: 'x' }));
  body.appendChild(bar); body.appendChild(page); show('START HERE');
  const m = modal('3D MODEL — HOW TO', body, { width: 'min(760px,94vw)' });
}

Object.assign(app, { renderLayers, refresh, select, status, topoGrid, exportObjects, exportDialog, syncObject, markDirty, saveProject, applyImagery, importFile, setProject, setProjection, renderLegend, renderConfidence, renderInspector, destructive, undo, deleteLayer, zoomToWorkings, setSeeThrough, renderImage, legendModel, confidenceSentence, helpModal });
boot();
