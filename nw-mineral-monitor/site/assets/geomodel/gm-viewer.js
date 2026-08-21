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
import { h, clear, row, num, txt, sel, btn, range, note, kv, section, toast, modal, menu, colorInput, fmtNum } from './gm-ui.js';
import { Tools } from './gm-tools.js';

const $ = id => document.getElementById(id);
const BUILD = '2026-08-21-gm1';
const GROUP_ORDER = ['Topography', 'Geology (draped)', 'Geology outlines', 'Structure', 'Mines', 'Claims', 'Workings', 'Stratigraphy', 'Block models', 'Surfaces', 'Sections', 'Images', 'Drillholes', 'Imports', 'Other'];

export const app = {
  project: null, R: null, engine: null, tools: null,
  display: new Map(),           // obj.id -> display options
  selected: null,               // obj id
  imagery: 'sat', imageryTex: null, topoId: null,
  dirty: false, saveTimer: null, key: null,
  hover: null,
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
  if (!q.get('fresh')) { const saved = await GM.store.loadProject(key).catch(() => null); if (saved) { setProject(saved, key); toast('restored your saved model for this site — use PROJECTS ▾ > Rebuild from map data to start over', 'info', 6000); return; } }
  const handoff = await GM.store.takeHandoff('site:' + key).catch(() => null);
  const { project } = await SITE.buildSiteProject(lon, lat, { radius, name, gi, aoi, zoom: +(q.get('zoom') || 13), handoff, onprog: status });
  setProject(project, key);
  markDirty();
}

async function loadProjectUrl(url) {
  status('loading project…'); const r = await fetch(url); if (!r.ok) throw new Error(`${url}: ${r.status}`);
  const name = url.split('/').pop(); const bytes = new Uint8Array(await r.arrayBuffer());
  await importBytes(name, bytes, { asProject: true });
}

async function showStart() {
  const list = await GM.store.listProjects().catch(() => []);
  const body = h('div', {},
    h('p', {}, 'Open this page from a mine card on the map (OPEN 3D MODEL), drop a project or data file here, or pick a saved model:'),
    h('div', { class: 'plist' }, ...(list.length ? list.map(p => h('div', { class: 'pitem', onclick: async () => { m.close(); const pr = await GM.store.loadProject(p.id); setProject(pr, p.id); } }, h('b', {}, p.name), h('span', {}, `${p.modified.slice(0, 16)} · ${(p.bytes / 1e6).toFixed(1)} MB`))) : [h('div', { class: 'note' }, 'no saved models yet')])),
    h('div', { class: 'frow' }, btn('NEW EMPTY PROJECT (UTM 12N)', () => { m.close(); setProject(new GM.Project({ name: 'new model', crs: GM.utm.crs(12, true), origin: [0, 0, 0] }), 'new-model'); }), btn('IMPORT FILES…', () => { m.close(); $('fileIn').click(); })),
    h('p', { class: 'note' }, 'Deep link: model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills&r=2500'));
  const m = modal('3D GEOLOGICAL MODEL', body, { sticky: false });
}

/* -------------------------------------------------------------- project */
export function setProject(p, key) {
  if (app.project) { for (const o of app.project.objects) app.R.remove(o.id); }
  app.project = p; app.key = key || (p.site && p.site.key) || GM.slug(p.name);
  if (p.site) p.site.key = app.key;
  p.ensureOrigin(); app.R.origin = p.origin || [0, 0, 0];
  app.display.clear();
  const topo = p.byKind('grid2d').find(g => g.role === 'topography'); app.topoId = topo ? topo.id : null;
  for (const o of p.objects) syncObject(o);
  p.on((type, obj) => { if (type === 'add') { syncObject(obj); } else if (type === 'remove') { app.R.remove(obj.id); app.display.delete(obj.id); if (app.selected === obj.id) select(null); } renderLayers(); markDirty(); });
  renderLayers(); renderHeader();
  const b = p.bounds(); if (b) app.R.fitTo(b);
  if (topo) applyImagery(app.imagery);
  status(`${p.name} — ${p.objects.length} objects · UTM ${p.crs.zone || ''}${p.crs.north === false ? 'S' : 'N'}`);
  app.tools.onProject();
}

export function syncObject(o) {
  const d = app.display.get(o.id) || defaultDisplay(o); app.display.set(o.id, d);
  if (o.kind === 'grid2d' && o.role === 'property' && !d.topo) d.topo = topoGrid();
  if (o.kind === 'grid2d' && o.role === 'topography') d.texture = app.imageryTex && app.imagery !== 'none' ? app.imageryTex : null;
  app.R.sync(o, d);
}
function defaultDisplay(o) {
  if (o.kind === 'grid2d') return o.role === 'property' ? { mode: 'draped', colormap: 'turbo', lift: 2 } : o.role === 'topography' ? { colorBy: 'elevation', colormap: 'terrain' } : { colorBy: 'flat' };
  if (o.kind === 'points') return { size: o.role === 'mines' ? 14 : 9, labels: o.role === 'mines' && o.n <= 60 };
  if (o.kind === 'blockmodel') return { colormap: 'turbo', shrink: 0.92 };
  if (o.kind === 'lineset') return { tubes: o.role === 'workings' || o.role === 'drillhole-traces' };
  return {};
}
export function topoGrid() { return app.topoId ? app.project.get(app.topoId) : app.project.byKind('grid2d').find(g => g.role === 'topography') || null; }
export function refresh(o) { syncObject(o); renderLayers(); markDirty(); }
export function markDirty() { app.dirty = true; clearTimeout(app.saveTimer); app.saveTimer = setTimeout(saveProject, 2500); $('saveBtn').classList.add('dirty'); }
export async function saveProject(explicit = false) {
  if (!app.project) return;
  try { app.project.site = app.project.site || {}; app.project.site.key = app.key; await GM.store.saveProject(app.project); app.dirty = false; $('saveBtn').classList.remove('dirty'); if (explicit) toast('saved in this browser (IndexedDB). Use EXPORT for files.', 'ok'); }
  catch (e) { console.warn(e); if (explicit) toast('save failed: ' + e.message, 'err'); }
}

/* -------------------------------------------------------------- imagery */
export async function applyImagery(kind) {
  app.imagery = kind; const topo = topoGrid(); if (!topo) return;
  if (kind === 'none') { app.imageryTex = null; syncObject(topo); return; }
  status(`stitching ${SITE.IMAGERY[kind].name}…`);
  try { const r = await SITE.buildImagery(topo, app.project.crs, kind, { maxPx: 2048 }); if (!r) throw new Error('no tiles'); app.imageryTex = canvasTexture(r.canvas); app.imageryTex.userData.shared = true; syncObject(topo); status(`${SITE.IMAGERY[kind].name} draped (z${r.zoom}, ${r.tiles} tiles)`); }
  catch (e) { app.imageryTex = null; syncObject(topo); status('imagery unavailable — elevation colours'); }
  app.R.invalidate();
}

/* --------------------------------------------------------------- layers */
function groupOf(o) {
  if (o.group) return o.group;
  if (o.kind === 'grid2d') return o.role === 'topography' ? 'Topography' : o.role === 'contact' ? 'Stratigraphy' : o.role === 'property' ? 'Surfaces' : 'Surfaces';
  if (o.kind === 'mesh') return o.role === 'unit' ? 'Stratigraphy' : o.role === 'geology' ? 'Geology (draped)' : o.role === 'stope' ? 'Workings' : 'Surfaces';
  if (o.kind === 'lineset') return o.role === 'workings' ? 'Workings' : o.role === 'faults' ? 'Structure' : o.role === 'geology-outline' ? 'Geology outlines' : o.role === 'section' ? 'Sections' : 'Imports';
  if (o.kind === 'points') return o.role === 'claims' ? 'Claims' : o.role === 'mines' || o.role === 'targets' ? 'Mines' : 'Imports';
  if (o.kind === 'blockmodel') return 'Block models';
  if (o.kind === 'drillholes') return 'Drillholes';
  if (o.kind === 'imageplane') return 'Images';
  if (o.kind === 'section') return 'Sections';
  if (o.kind === 'stratmodel') return 'Stratigraphy';
  return 'Other';
}
const collapsed = new Set(['Geology outlines', 'Claims']);
export function renderLayers() {
  const host = clear($('layers')); if (!app.project) return;
  const groups = new Map();
  for (const o of app.project.objects) { const g = groupOf(o); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(o); }
  const order = [...groups.keys()].sort((a, b) => (GROUP_ORDER.indexOf(a) + 1 || 99) - (GROUP_ORDER.indexOf(b) + 1 || 99));
  for (const g of order) {
    const objs = groups.get(g); const allOn = objs.every(o => o.visible !== false);
    const head = h('div', { class: 'lgroup' + (collapsed.has(g) ? ' closed' : '') },
      h('span', { class: 'tw', onclick: () => { collapsed.has(g) ? collapsed.delete(g) : collapsed.add(g); renderLayers(); } }, collapsed.has(g) ? '▸' : '▾'),
      h('input', { type: 'checkbox', checked: allOn, onchange: e => { for (const o of objs) { o.visible = e.target.checked; app.R.setVisible(o.id, o.visible); } renderLayers(); markDirty(); } }),
      h('span', { class: 'gname', onclick: () => { collapsed.has(g) ? collapsed.delete(g) : collapsed.add(g); renderLayers(); } }, g, h('span', { class: 'cnt' }, String(objs.length))));
    host.appendChild(head);
    if (collapsed.has(g)) continue;
    for (const o of objs) host.appendChild(layerRow(o));
  }
}
function layerRow(o) {
  const d = app.display.get(o.id) || {};
  const r = h('div', { class: 'lrow' + (app.selected === o.id ? ' sel' : ''), 'data-id': o.id, onclick: e => { if (e.target.tagName === 'INPUT' || e.target.classList.contains('mbtn')) return; select(o.id); } },
    h('input', { type: 'checkbox', checked: o.visible !== false, onchange: e => { o.visible = e.target.checked; app.R.setVisible(o.id, o.visible); markDirty(); } }),
    o.kind === 'section' || o.kind === 'stratmodel' ? h('span', { class: 'sw kind' }, o.kind === 'section' ? '§' : '≡') : colorInput(o.color, c => { o.color = c; syncObject(o); markDirty(); }),
    h('span', { class: 'lname', title: `${o.kind} · ${o.name}` }, o.name || '(unnamed)'),
    h('span', { class: 'ltag' }, tag(o)),
    h('button', { class: 'mbtn', onclick: e => { e.stopPropagation(); layerMenu(e.currentTarget, o); } }, '⋯'));
  return r;
}
function tag(o) {
  switch (o.kind) {
    case 'grid2d': return `${o.nx}×${o.ny}`;
    case 'mesh': return `${(o.nTriangles / 1000).toFixed(o.nTriangles > 9999 ? 0 : 1)}k△`;
    case 'lineset': return `${o.parts.length} ln`;
    case 'points': return `${o.n} pt`;
    case 'blockmodel': return `${o.count.join('×')}`;
    case 'drillholes': return `${o.collars.length} dh`;
    case 'imageplane': return o.plane;
    case 'stratmodel': return `${o.units.length} u`;
    default: return o.kind;
  }
}
function layerMenu(anchor, o) {
  const items = [
    { label: 'Zoom to', onclick: () => { const b = o.bounds(); if (b) app.R.fitTo(b); } },
    { label: 'Properties', onclick: () => select(o.id) },
    { label: 'Export…', onclick: () => exportDialog([o]) },
    '-',
    { label: 'Rename', onclick: () => { const n = prompt('Layer name', o.name); if (n != null) { o.name = n; renderLayers(); markDirty(); } } },
  ];
  if (o.kind === 'grid2d' && o.role !== 'topography') items.push({ label: 'Set as topography', onclick: () => { o.role = 'topography'; app.topoId = o.id; syncObject(o); applyImagery(app.imagery); markDirty(); } });
  if (o.kind === 'grid2d') items.push({ label: 'Convert to mesh', onclick: () => { const m = o.toMesh(1); m.name = o.name + ' (mesh)'; app.project.add(m); } });
  if (o.kind === 'lineset' && o.role === 'workings') items.push({ label: 'Send footprint to map (MY DATA)', onclick: () => app.tools.workings.sendToMap(o) });
  items.push('-', { label: 'Delete layer', onclick: () => { if (confirm(`Delete "${o.name}"?`)) app.project.remove(o); } });
  menu(anchor, items);
}

/* ------------------------------------------------------------ inspector */
export function select(id) {
  app.selected = id; renderLayers();
  const host = clear($('inspector'));
  if (!id) { host.appendChild(app.tools.panel || h('div', { class: 'note' }, 'select a layer or pick something in the scene')); return; }
  const o = app.project.get(id); if (!o) return;
  host.appendChild(inspectorFor(o));
}
function inspectorFor(o) {
  const d = app.display.get(o.id) || {};
  const b = o.bounds();
  const body = h('div', { class: 'insp' },
    h('h2', {}, o.name || '(unnamed)'),
    h('div', { class: 'badges' }, h('span', { class: 'badge' }, o.kind.toUpperCase()), o.role ? h('span', { class: 'badge dim' }, o.role) : null, o.group ? h('span', { class: 'badge dim' }, o.group) : null),
    kv([['Bounds E', b ? `${fmtNum(b[0], 0)} – ${fmtNum(b[3], 0)}` : null], ['Bounds N', b ? `${fmtNum(b[1], 0)} – ${fmtNum(b[4], 0)}` : null], ['Elevation', b && b[2] === b[2] ? `${fmtNum(b[2], 0)} – ${fmtNum(b[5], 0)} m` : null]]),
  );
  // display controls by kind
  const ctl = h('div', { class: 'psec' }, h('h3', {}, 'DISPLAY'));
  ctl.appendChild(row('opacity', range(o.opacity == null ? 1 : o.opacity, 0, 1, 0.05, e => { o.opacity = +e.target.value; app.R.setOpacity(o.id, o.opacity); markDirty(); })));
  if (o.kind === 'grid2d') {
    if (o.role === 'property') {
      ctl.appendChild(row('mode', sel([['draped', 'drape on topography'], ['flat', 'flat at elevation'], ['surface', 'as surface (Z = value)']], d.mode || 'draped', { onchange: e => { d.mode = e.target.value; syncObject(o); } })));
      ctl.appendChild(row('elevation', num(d.elevation == null ? '' : d.elevation, { placeholder: 'for flat mode', onchange: e => { d.elevation = e.target.value === '' ? null : +e.target.value; syncObject(o); } })));
    } else ctl.appendChild(row('colour by', sel([['flat', 'flat colour'], ['elevation', 'elevation']], d.colorBy || (o.role === 'topography' ? 'elevation' : 'flat'), { onchange: e => { d.colorBy = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), d.colormap || 'terrain', { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('wireframe', h('input', { type: 'checkbox', checked: !!d.wireframe, onchange: e => { d.wireframe = e.target.checked; syncObject(o); } })));
    const L = app.R.layers.get(o.id); if (L && L.range) ctl.appendChild(note(`range ${fmtNum(L.range[0])} – ${fmtNum(L.range[1])} ${o.units || ''}`));
    const zr = o.zrange(); ctl.appendChild(kv([['Cell', `${o.dx} × ${o.dy} m`], ['Rotation', o.rotation ? o.rotation + '°' : null], ['Values', `${fmtNum(zr[0])} – ${fmtNum(zr[1])}`], ['No-data', `${[...o.values].filter(v => v !== v).length} nodes`]]));
  }
  if (o.kind === 'mesh') {
    const attrs = Object.keys(o.attributes);
    if (attrs.length) ctl.appendChild(row('colour by', sel([['', 'flat colour'], ...attrs], d.attribute || '', { onchange: e => { d.attribute = e.target.value || null; syncObject(o); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), d.colormap || 'viridis', { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('wireframe', h('input', { type: 'checkbox', checked: !!d.wireframe, onchange: e => { d.wireframe = e.target.checked; syncObject(o); } })));
    ctl.appendChild(row('edges', h('input', { type: 'checkbox', checked: !!d.edges, onchange: e => { d.edges = e.target.checked; syncObject(o); } })));
    ctl.appendChild(kv([['Vertices', o.nVertices], ['Triangles', o.nTriangles]]));
  }
  if (o.kind === 'points') {
    const nums = Object.keys(o.attributes).filter(k => o.isNumeric(k)); const texts = Object.keys(o.attributes).filter(k => !o.isNumeric(k));
    ctl.appendChild(row('colour by', sel([['', 'layer colour'], ...nums], d.attribute || '', { onchange: e => { d.attribute = e.target.value || null; syncObject(o); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), d.colormap || 'viridis', { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(row('size', range(d.size || 10, 3, 30, 1, e => { d.size = +e.target.value; syncObject(o); })));
    ctl.appendChild(row('labels', h('input', { type: 'checkbox', checked: !!d.labels, onchange: e => { d.labels = e.target.checked; syncObject(o); } }), sel(['name', ...texts.filter(t => t !== 'name'), ...nums], d.labelField || 'name', { onchange: e => { d.labelField = e.target.value; syncObject(o); } })));
    const L = app.R.layers.get(o.id); if (L && L.range) ctl.appendChild(note(`range ${fmtNum(L.range[0])} – ${fmtNum(L.range[1])}`));
    ctl.appendChild(kv([['Points', o.n], ['Columns', Object.keys(o.attributes).join(', ') || '—']]));
  }
  if (o.kind === 'lineset') {
    ctl.appendChild(row('tubes', h('input', { type: 'checkbox', checked: !!d.tubes, onchange: e => { d.tubes = e.target.checked; syncObject(o); } }), range(d.tubeScale || 1, 0.2, 6, 0.1, e => { d.tubeScale = +e.target.value; syncObject(o); })));
    ctl.appendChild(kv([['Parts', o.parts.length], ['Length', fmtNum(o.length(), 0) + ' m']]));
    if (o.role === 'workings') { const s = E.workingsSummary(o); ctl.appendChild(kv(Object.entries(s.by_type).map(([k, v]) => [k, fmtNum(v, 0) + ' m']))); ctl.appendChild(btn('OPEN WORKINGS EDITOR', () => app.tools.open('workings', o))); }
  }
  if (o.kind === 'blockmodel') {
    const attrs = Object.keys(o.attributes); const L = app.R.layers.get(o.id);
    ctl.appendChild(row('attribute', sel(attrs, d.attribute || attrs[0], { onchange: e => { d.attribute = e.target.value; d.cutoff = null; d.cutoffHi = null; syncObject(o); select(o.id); } })));
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), d.colormap || 'turbo', { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
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
    ctl.appendChild(row('colormap', sel(Object.keys(GM.COLORMAPS), d.colormap || 'turbo', { onchange: e => { d.colormap = e.target.value; syncObject(o); } })));
    ctl.appendChild(kv([['Holes', o.collars.length], ['Surveys', o.surveys.length], ['Tables', tables.map(t => `${t} (${o.intervals[t].length})`).join(', ') || '—']]));
    ctl.appendChild(btn('SAMPLES → POINTS (for kriging)', () => { if (!d.table || !d.column) return toast('choose a table + column first', 'warn'); const ps = o.intervalPoints(d.table, d.column); ps.group = 'Imports'; ps.color = [255, 200, 80]; app.project.add(ps); select(ps.id); }));
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
  if (prov.length) body.appendChild(section('PROVENANCE', kv(prov)));
  const meta = Object.entries(o.metadata || {}).filter(([k, v]) => !['warnings', 'types', 'schema', 'howto'].includes(k) && v != null && typeof v !== 'object').map(([k, v]) => [k, String(v).slice(0, 300)]);
  if (meta.length) body.appendChild(section('METADATA', kv(meta)));
  if (o.metadata && o.metadata.howto) body.appendChild(note(o.metadata.howto));
  if (o.metadata && o.metadata.warnings && o.metadata.warnings.length) body.appendChild(section('WARNINGS', ...o.metadata.warnings.map(w => note(w, 'note warn'))));
  body.appendChild(h('div', { class: 'frow' }, btn('ZOOM TO', () => { const bb = o.bounds(); if (bb) app.R.fitTo(bb); }), btn('EXPORT…', () => exportDialog([o])), btn('DELETE', () => { if (confirm(`Delete "${o.name}"?`)) app.project.remove(o); }, { class: 'b danger' })));
  return body;
}

/* ------------------------------------------------------------- picking */
export function describePick(p) {
  if (!p || !p.obj) return null;
  const o = p.obj; const lines = [];
  if (o.kind === 'points' && p.index != null) { const i = p.index; lines.push(`${o.name} #${i}`); for (const [k, col] of Object.entries(o.attributes)) { const v = col[i]; if (v == null || v === '') continue; lines.push(`${k}: ${String(v).slice(0, 80)}`); if (lines.length > 14) break; } }
  else if (o.kind === 'lineset') { const sp = p.object.userData.segPart; const k = sp && p.index != null ? sp[Math.floor(p.index / 2)] : -1; const f = k >= 0 ? o.features[k] : null; lines.push(o.name + (k >= 0 ? ` · part ${k}` : '')); if (f) for (const [kk, v] of Object.entries(f)) if (v != null && v !== '' && typeof v !== 'object') lines.push(`${kk}: ${v}`); if (k >= 0) lines.push(`length: ${fmtNum(o.length(k), 1)} m`); }
  else if (o.kind === 'blockmodel' && p.instanceId != null) { const ids = p.object.userData.blockIds; const idx = ids ? ids[p.instanceId] : null; if (idx != null) { const [i, j, k] = o.ijk(idx); lines.push(`${o.name} block (${i},${j},${k})`); for (const [kk, a] of Object.entries(o.attributes)) { const v = a.values[idx]; if (v == null || v !== v) continue; lines.push(`${kk}: ${typeof v === 'number' ? fmtNum(v, 3) : v}`); } } }
  else if (o.kind === 'mesh') { lines.push(o.name); for (const k of ['unit', 'lithology', 'age', 'description']) if (o.metadata[k]) lines.push(`${k}: ${String(o.metadata[k]).slice(0, 120)}`); }
  else if (o.kind === 'grid2d') { const z = o.sample(p.world[0], p.world[1]); lines.push(o.name); lines.push(`value: ${fmtNum(z, 2)} ${o.units || ''}`); }
  else lines.push(o.name);
  return lines;
}

/* --------------------------------------------------------------- chrome */
function wireChrome() {
  const R = app.R; const canvas = $('gl'); const tip = $('tip');
  canvas.addEventListener('mousemove', e => {
    if (app.tools.active && app.tools.active.onMove && app.tools.active.onMove(e)) return;
    const p = R.pick(e.clientX, e.clientY);
    if (p) { const [x, y, z] = p.world; const lonlat = app.project && app.project.crs.kind === 'utm' ? GM.utm.inv(x, y, app.project.crs.zone, app.project.crs.north) : null; $('coords').textContent = `E ${fmtNum(x, 1)}  N ${fmtNum(y, 1)}  Z ${fmtNum(z, 1)} m` + (lonlat ? `   (${lonlat[1].toFixed(5)}, ${lonlat[0].toFixed(5)})` : ''); const lines = describePick(p); if (lines && !app.tools.active) { tip.style.display = 'block'; tip.style.left = (e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; tip.innerHTML = lines.map((l, i) => i ? GM.esc(l) : `<b>${GM.esc(l)}</b>`).join('<br>'); } else tip.style.display = 'none'; }
    else { tip.style.display = 'none'; }
  });
  canvas.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  let downAt = null;
  canvas.addEventListener('pointerdown', e => { downAt = [e.clientX, e.clientY, Date.now()]; });
  canvas.addEventListener('pointerup', e => {
    if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4 || Date.now() - downAt[2] > 600) return;
    if (app.tools.active && app.tools.active.onClick && app.tools.active.onClick(e)) return;
    const p = R.pick(e.clientX, e.clientY); if (p && p.obj) { select(p.obj.id); const lines = describePick(p); if (lines) { const host = $('inspector'); host.insertBefore(section('PICKED', ...lines.map(l => note(l))), host.firstChild.nextSibling); } }
  });
  canvas.addEventListener('dblclick', e => { if (app.tools.active && app.tools.active.onDblClick && app.tools.active.onDblClick(e)) return; const p = R.pick(e.clientX, e.clientY); if (p) { const v = R.toScene(p.world[0], p.world[1], p.world[2]); v.y *= R.ve; R.controls.target.copy(v); R.invalidate(); } });
  window.addEventListener('keydown', e => { if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return; if (app.tools.active && app.tools.active.onKey && app.tools.active.onKey(e)) return; if (e.key === 'Escape') { app.tools.stop(); select(null); } if (e.key === 'f') { const b = app.project && app.project.bounds(); if (b) R.fitTo(b); } if (e.key === 't') R.viewFrom('top'); if (e.key === 'n') R.viewFrom('north'); if (e.key === 'i') R.viewFrom('iso'); });
  // toolbar
  $('btnImport').onclick = () => $('fileIn').click();
  $('fileIn').onchange = async e => { for (const f of e.target.files) await importFile(f); e.target.value = ''; };
  document.addEventListener('dragover', e => { e.preventDefault(); $('drop').style.display = 'flex'; });
  $('drop').addEventListener('dragleave', () => { $('drop').style.display = 'none'; });
  document.addEventListener('drop', async e => { e.preventDefault(); $('drop').style.display = 'none'; for (const f of e.dataTransfer.files) await importFile(f); });
  $('btnExport').onclick = e => exportMenu(e.currentTarget);
  $('saveBtn').onclick = () => saveProject(true);
  $('btnProjects').onclick = e => projectsMenu(e.currentTarget);
  $('btnView').onclick = e => viewMenu(e.currentTarget);
  $('btnTools').onclick = e => app.tools.menu(e.currentTarget);
  $('btnHelp').onclick = () => helpModal();
  $('btnMap').onclick = () => { const s = app.project && app.project.site; location.href = s && s.lon != null ? `index.html#12/${s.lat}/${s.lon}` : 'index.html'; };
  $('veRange').oninput = e => { R.setVE(+e.target.value); $('veLbl').textContent = `VE ×${(+e.target.value).toFixed(1)}`; };
  R.onRender = () => { const az = R.northArrow(); $('north').style.transform = `rotate(${-az}rad)`; };
  window.addEventListener('beforeunload', () => { if (app.dirty) saveProject(); });
}
function renderHeader() { const p = app.project; $('siteName').textContent = p ? p.name : '—'; $('crsBadge').textContent = p && p.crs.kind === 'utm' ? `UTM ${p.crs.zone}${p.crs.north ? 'N' : 'S'} · EPSG:${p.crs.epsg}` : 'local XYZ'; document.title = `3D MODEL — ${p ? p.name : ''}`; }
export function status(t) { $('status').textContent = t || ''; }

function viewMenu(anchor) {
  menu(anchor, [
    { label: 'Fit all (f)', onclick: () => { const b = app.project.bounds(); if (b) app.R.fitTo(b); } },
    { label: 'Top (t)', onclick: () => app.R.viewFrom('top') }, { label: 'Look north (n)', onclick: () => app.R.viewFrom('north') }, { label: 'Look south', onclick: () => app.R.viewFrom('south') }, { label: 'Look east', onclick: () => app.R.viewFrom('east') }, { label: 'Look west', onclick: () => app.R.viewFrom('west') }, { label: 'Isometric (i)', onclick: () => app.R.viewFrom('iso') },
    '-',
    ...Object.entries(SITE.IMAGERY).map(([k, v]) => ({ label: (app.imagery === k ? '● ' : '○ ') + 'Drape: ' + v.name, onclick: () => applyImagery(k) })),
    '-',
    { label: 'Screenshot (PNG)', onclick: () => { const url = app.R.screenshot(); const a = document.createElement('a'); a.href = url; a.download = `${GM.slug(app.project.name)}-3d.png`; a.click(); } },
    { label: 'Toggle 2-D section panel', onclick: () => app.tools.section.togglePanel() },
  ]);
}
async function projectsMenu(anchor) {
  const list = await GM.store.listProjects().catch(() => []);
  menu(anchor, [
    { label: 'Save now', onclick: () => saveProject(true) },
    { label: 'Rename project', onclick: () => { const n = prompt('Project name', app.project.name); if (n) { app.project.name = n; renderHeader(); markDirty(); } } },
    { label: 'Rebuild from map data (discard edits)', onclick: async () => { const s = app.project.site; if (!s || s.lon == null) return toast('this project has no map site', 'warn'); if (!confirm('Rebuild this site from the map data? Your edits in this project will be lost.')) return; await GM.store.deleteProject(app.key); location.search = `?lat=${s.lat}&lon=${s.lon}&name=${encodeURIComponent(s.name || '')}&r=${s.radius_m || 2500}&aoi=${s.aoi || 'auto'}${s.grade_index != null ? '&gi=' + s.grade_index : ''}&fresh=1`; } },
    '-',
    ...list.map(p => ({ label: (p.id === app.key ? '● ' : '○ ') + p.name, hint: p.modified.slice(0, 10), onclick: async () => { const pr = await GM.store.loadProject(p.id); if (pr) setProject(pr, p.id); } })),
    '-',
    { label: 'Delete this project from browser storage', onclick: async () => { if (confirm('Delete saved copy of this project?')) { await GM.store.deleteProject(app.key); toast('deleted'); } } },
  ]);
}

/* --------------------------------------------------------------- import */
export async function importFile(file) {
  status(`reading ${file.name}…`);
  try { const bytes = new Uint8Array(await file.arrayBuffer()); await importBytes(file.name, bytes, {}); }
  catch (e) { console.error(e); toast(`${file.name}: ${e.message}`, 'err', 8000); status(`import failed: ${e.message}`); }
}
async function importBytes(name, bytes, opts) {
  const ext = name.split('.').pop().toLowerCase();
  if (ext === 'json' || ext === 'geojson') {
    const text = new TextDecoder().decode(bytes); const j = JSON.parse(text);
    if (j.schema === GM.SCHEMA) { const p = GM.Project.fromJSON(j); if (!app.project || opts.asProject || confirm('Replace the current project with this one? (Cancel = merge its layers into the current project)')) setProject(p, p.site && p.site.key); else for (const o of p.objects) app.project.add(o); toast(`loaded project ${p.name}`, 'ok'); return; }
    if (j.type === 'FeatureCollection' || j.type === 'Feature') { importGeoJSON(j, name); return; }
    throw new Error('unrecognised JSON (not a geomodel project or GeoJSON)');
  }
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tif', 'tiff'].includes(ext)) { await app.tools.georef.fromImageBytes(name, bytes); return; }
  if (ext === 'pdf') { await app.tools.georef.fromPdf(name, bytes); return; }
  if (ext === 'csv' || ext === 'txt' || ext === 'xyz' || ext === 'dat') { await importTableDialog(name, bytes); return; }
  const fmt = F.sniff(name, bytes);
  if (!fmt) throw new Error(`cannot detect the format of ${name}`);
  if (!app.project) setProject(new GM.Project({ name: name.replace(/\.[^.]+$/, ''), crs: GM.utm.crs(12, true) }), GM.slug(name));
  const res = await F.readAny({ name, bytes }, { crs: app.project.crs, format: fmt });
  await placeImported(res, name);
}
async function placeImported(res, name) {
  const objs = res.objects || [];
  if (!objs.length) throw new Error('nothing readable in ' + name);
  // grids: ask how to use them
  for (const o of objs) {
    if (o.kind === 'grid2d' && !o.role) o.role = 'surface';
    if (o.kind === 'grid2d') { const how = await askGridRole(o); if (how === 'cancel') continue; o.role = how; if (how === 'topography') { app.topoId = o.id; } }
    if (!o.group) o.group = 'Imports';
    if (!o.name) o.name = name;
    app.project.add(o);
  }
  if (res.project && res.project.metadata && res.project.metadata.warnings) for (const w of res.project.metadata.warnings) toast(w, 'warn', 6000);
  for (const w of (res.warnings || []).slice(0, 4)) toast(w, 'warn', 6000);
  const b = objs[0].bounds(); if (b && objs.length === 1) app.R.fitTo(b);
  status(`imported ${objs.length} object(s) from ${name} (${res.format})`);
  if (app.topoId && objs.some(o => o.role === 'topography')) applyImagery(app.imagery);
}
function askGridRole(g) {
  return new Promise(resolve => {
    const zr = g.zrange();
    const body = h('div', {}, h('p', {}, `${g.name || 'grid'}: ${g.nx}×${g.ny} nodes, cell ${fmtNum(g.dx, 2)}×${fmtNum(g.dy, 2)}, values ${fmtNum(zr[0])} – ${fmtNum(zr[1])}. How should it be used?`),
      h('div', { class: 'frow' }, btn('TOPOGRAPHY (elevation)', () => { m.close(); resolve('topography'); }), btn('SURFACE / CONTACT (Z = value)', () => { m.close(); resolve('contact'); }), btn('PROPERTY (geophysics, drape colours)', () => { m.close(); resolve('property'); }), btn('cancel', () => { m.close(); resolve('cancel'); })),
      note('Property grids (magnetics, gravity, radiometrics, thickness) are coloured by value and draped on the topography; surfaces are placed at Z = value (contacts, horizons, water tables).'));
    const m = modal('IMPORT GRID', body, { sticky: true });
  });
}
function importGeoJSON(j, name) {
  const feats = j.type === 'Feature' ? [j] : j.features; const crs = app.project.crs; const topo = topoGrid();
  const ps = new GM.PointSet({ name: name.replace(/\.[^.]+$/, ''), role: 'points', group: 'Imports', color: [255, 200, 80] }); const ls = new GM.LineSet({ name: name.replace(/\.[^.]+$/, ''), role: 'lines', group: 'Imports', color: [255, 200, 80] });
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
        else if (kind.value === 'collar') { res = await F.readAny({ name, bytes }, Object.assign(opts, { format: 'csv_drillholes' })); toast('collars loaded — import survey.csv and interval CSVs from the drillhole layer properties', 'info', 6000); }
        else res = await F.readAny({ name, bytes }, Object.assign(opts, { table: kind.value }));
        for (const o of res.objects) if (o.kind === 'points' && !zs.value) { const topo = topoGrid(); if (topo) for (let i = 0; i < o.n; i++) { if (o.xyz[3 * i + 2] === 0 || o.xyz[3 * i + 2] !== o.xyz[3 * i + 2]) { const t = topo.sample(o.xyz[3 * i], o.xyz[3 * i + 1]); if (t === t) o.xyz[3 * i + 2] = t + 2; } } }
        await placeImported(res, name);
      } catch (e) { toast(`${name}: ${e.message}`, 'err', 8000); }
    }), btn('cancel', () => m.close())));
  const m = modal('IMPORT TABLE', body, { sticky: true });
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
    { label: 'Whole project → OMF v2.0 (Leapfrog 2025+)', onclick: () => exportObjects('omf2', app.project.objects) },
    { label: 'Whole project → OMF v0.9 (Leapfrog ≤ 2024)', onclick: () => exportObjects('omf1', app.project.objects) },
    { label: 'Whole project → .geomodel.json', onclick: () => exportObjects('geomodel', app.project.objects) },
    { label: 'Whole project → DXF', onclick: () => exportObjects('dxf', app.project.objects) },
    '-',
    { label: 'Selected layer…', onclick: () => { const o = app.selected && app.project.get(app.selected); if (!o) return toast('select a layer first', 'warn'); exportDialog([o]); }, disabled: !app.selected },
    { label: 'Leapfrog kit (zip of everything, per-format)', onclick: () => exportKit() },
  ]);
}
export function exportDialog(objs) {
  const kinds = new Set(objs.map(o => o.kind)); const opts = EXPORTS.filter(([id, label, ks]) => ks.includes('*') || objs.some(o => ks.includes(o.kind)));
  const body = h('div', {}, h('p', {}, `${objs.length === 1 ? objs[0].name : objs.length + ' layers'} (${[...kinds].join(', ')})`),
    ...opts.map(([id, label]) => btn(label, () => { m.close(); exportObjects(id, objs); }, { class: 'b wide' })));
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
    const add = async (fmt, objs, label) => { try { const files = await F.writeAs(fmt, objs, { name: p.name, basename: base, crs: `EPSG:${p.crs.epsg}` }); for (const [n, v] of Object.entries(files)) { zip.add(n, typeof v === 'string' ? new TextEncoder().encode(v) : v); manifest.push(`- \`${n}\` — ${label}`); } } catch (e) { manifest.push(`- (${fmt} skipped: ${e.message})`); } };
    await add('omf2', p.objects, 'OMF v2.0 — Leapfrog Geo 2025.1+ (Leapfrog menu > OMF > Import)');
    await add('omf1', p.objects, 'OMF v0.9 — Leapfrog Geo ≤ 2024.1');
    zip.add(`${base}.geomodel.json`, new TextEncoder().encode(p.serialize())); manifest.push(`- \`${base}.geomodel.json\` — this viewer's project (drop on model3d.html)`);
    for (const g of p.byKind('grid2d')) { await add('surfer_grd', g, `Surfer 7 grid — ${g.name}`); await add('gxf', g, `Geosoft GXF — ${g.name}`); if (Math.abs(g.dx - g.dy) < 1e-9 && !g.rotation) await add('arc_ascii', g, `Arc/Info ASCII — ${g.name}`); }
    const ml = p.objects.filter(o => o.kind === 'mesh' || o.kind === 'lineset' || o.kind === 'points'); if (ml.length) await add('dxf', ml, 'DXF R12 — meshes as 3DFACE, lines as 3-D POLYLINE');
    for (const ps of p.byKind('points')) await add('csv_points', ps, `points CSV (East,North,Elev + columns) — ${ps.name}`);
    for (const bm of p.byKind('blockmodel')) { await add('csv_blockmodel', bm, `block model CSV — ${bm.name}`); await add('ubc', bm, `UBC mesh/model — ${bm.name}`); }
    for (const dh of p.byKind('drillholes')) await add('csv_drillholes', dh, `drillhole CSVs — ${dh.name}`);
    const readme = `# ${p.name} — 3-D model kit\n\nExported ${new Date().toISOString().slice(0, 10)} from the NW Mineral Monitor 3-D modeller (build ${BUILD}).\nCRS: **WGS84 / UTM zone ${p.crs.zone}${p.crs.north ? 'N' : 'S'} (EPSG:${p.crs.epsg}), metres, Z = elevation** — set exactly that when a package asks.\n\n## Files\n\n${manifest.join('\n')}\n\n## Import click-paths\n\n- Leapfrog Geo 2025.1+: Leapfrog menu > OMF > Import > \`${base}.omf\` (one shot; OMF objects cannot be reloaded). Older Leapfrog: \`${base}-omf09.omf\`.\n- Refreshable route: Topographies > New Topography > Import Elevation Grid (.grd/.asc); Meshes > Import Mesh (.dxf/.obj); Points > Import Points (.csv); Block Models > Import (.csv).\n- Oasis montaj / Target: Grid and Image > Import > .gxf / .grd; XYZ/CSV for points.\n- Surfer: File > Open > .grd (Surfer 7 binary).\n- Kingdom: grids via ZMAP+ (export a grid layer as ZMAP+ from the layer menu).\n\n## Honesty notes\n\n${(p.metadata.notes || []).map(n => '- ' + n).join('\n')}\n- Draped geology meshes follow terrain; they are map polygons, not modelled volumes. Workings digitised from historic maps carry their source and confidence in each feature.\n`;
    zip.add('README-GEOMODEL.md', new TextEncoder().encode(readme));
    GM.downloadBlob(new Blob([zip.finish()]), `${base}-geomodel-kit.zip`); status('kit exported');
  } catch (e) { console.error(e); toast('kit failed: ' + e.message, 'err'); }
}

function helpModal() {
  modal('3D MODEL — HOW TO', h('div', { class: 'help' },
    h('p', {}, h('b', {}, 'Navigate'), ' — left-drag orbit · right-drag pan · wheel zoom · double-click re-centres · f fit · t top · n north · i iso · Esc stops a tool. VE slider exaggerates elevation.'),
    h('p', {}, h('b', {}, 'Layers'), ' — tick to show/hide, swatch to recolour, ⋯ for zoom / export / delete. Click a layer or pick in the scene for its properties (colour by attribute, cut-offs, labels).'),
    h('p', {}, h('b', {}, 'TOOLS ▾ Section & slice'), ' — draw a section line (two clicks on the ground), or use the W–E / S–N presets; the plane clips the model, intersects every surface, fills the pancake units, samples block models and projects nearby workings; the 2-D panel shows the section the way you would draw it and exports PNG/DXF.'),
    h('p', {}, h('b', {}, 'TOOLS ▾ Workings'), ' — turn a paper map into 3-D: georeference a scanned level plan at its level elevation (or a longitudinal section between two surface points), trace drifts on it, add adits from portals (bearing + length), shafts from collars (depth, dip), raises between levels, stopes as extruded outlines. Feet convert to metres at the door. Send the footprint back to the map as a MY DATA layer.'),
    h('p', {}, h('b', {}, 'TOOLS ▾ Stratigraphy'), ' — the pancake model: add units youngest-first with a base from contact points (RBF / kriging / IDW), a surface grid, or a constant; deposit bases on-lap older units, erosion bases cut them; BUILD makes surfaces + volumes, a virtual drillhole reports the column anywhere.'),
    h('p', {}, h('b', {}, 'TOOLS ▾ Block model & kriging'), ' — lay a block grid, pick a sample layer (assay points, graded mines, imported XYZ) and a value, fit the experimental variogram (or set nugget / sill / range / model), choose the search, RUN ordinary kriging (or IDW / nearest) — optionally inside one stratigraphic unit. Cut-offs, grade–tonnage, export CSV/UBC/OMF.'),
    h('p', {}, h('b', {}, 'TOOLS ▾ Implicit surface'), ' — Leapfrog-style RBF of signed distances: contact points (0), hanging-wall (+) and foot-wall (−) points → iso-surface mesh (veins, intrusions, ore shells).'),
    h('p', {}, h('b', {}, 'Import'), ' — drop files: OMF v0.9/v2 · DXF · OBJ · GOCAD .ts/.pl/.vs · Leapfrog .msh · Surfer/Geosoft .grd · GXF · Arc ASCII · ZMAP+ · Irap · UBC · CSV (points, structural, collars, block models) · Geosoft XYZ · SEG-Y (as a section image) · LAS · GeoJSON · images/PDF pages for georeferencing.'),
    h('p', {}, h('b', {}, 'Export'), ' — OMF v2.0 for Leapfrog 2025+, OMF v0.9 for older, DXF/OBJ/GOCAD, Surfer/Geosoft/GXF/ZMAP/Irap grids, CSV/XYZ points, block-model CSV + UBC, the project JSON, or the whole kit as a zip with a README of click-paths.'),
    h('p', { class: 'note' }, 'Coordinates are WGS84/UTM metres, Z = elevation. Grades are cited historic figures, claims are BLM centroids, geology is map-scale, terrain is a ~30 m public composite. Never enter adits or shafts.')));
}

Object.assign(app, { renderLayers, refresh, select, status, topoGrid, exportObjects, exportDialog, syncObject, markDirty, saveProject, applyImagery, importFile, setProject });
boot();
