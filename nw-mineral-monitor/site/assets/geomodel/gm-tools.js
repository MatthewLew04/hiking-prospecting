/* gm-tools.js — modelling tools for model3d.html:
   SectionTool (slice + 2-D section panel), WorkingsTool (maps -> 3-D workings),
   GeorefTool (scanned plans / sections as ImagePlanes), StratTool (pancake
   builder), BlocksTool (block model + variograms + kriging), ImplicitTool
   (RBF iso-surfaces).  Tools render their panel into the tool host and get
   pointer events from the viewer while active; MEASURE, NOTES and TRACE live
   in gm-more-tools.js and register themselves through Tools.register(). */
import * as GM from './gm-core.js';
import * as E from './gm-engine.js';
import * as F from './gm-formats.js';
import { THREE, canvasTexture } from './gm-render.js';
import { h, clear, row, num, txt, sel, btn, range, note, kv, section, toast, modal, menu, colorInput, fmtNum, plotVariogram, toolHead, confirmModal, promptModal, lineSample } from './gm-ui.js';
import { StructureTool, StereonetTool, FormTool, globalAnisotropy } from './gm-struct-tools.js';
import * as ST from './gm-structural.js';
import { CONF_CLASSES, confClass } from './gm-render.js';

const $ = id => document.getElementById(id);

/* The nine steps, in the order a district with no drillholes is modelled
   (GEOMODEL.md §1 and §7): from the map, from the geology, volumes, see it.
   Everything that presents the tools — the TOOLS menu, each panel's header,
   the progress card in the empty inspector, HELP — reads this one table, so
   the numbering and the wording cannot drift apart. `needs` and `has` are
   computed from the project alone. */
export const TOOL_STEPS = [
  { key: 'georef', step: 1, group: 'FROM THE MAP', title: 'Georeference a scan', menu: 'Georeference an image', purpose: 'Place a scanned level plan at its level elevation, or a longitudinal / cross section between two surface points, so it can be traced in 3-D.',
    needs: P => [{ label: 'an image or PDF (drop it on the page)', ok: true }],
    has: P => { const n = P.byKind('imageplane').length; return n ? `${n} georeferenced image${n > 1 ? 's' : ''}` : null; }, next: 'trace workings on it (step 2)' },
  { key: 'workings', step: 2, group: 'FROM THE MAP', title: 'Workings from maps', menu: 'Workings from maps', purpose: 'Turn a level plan, a section or a written description into adits, drifts, shafts, raises and stopes — every one typed, sourced and confidence-tagged.',
    needs: P => [{ label: 'topography', ok: !!topoOf(P), open: null, why: 'clicks land on the ground' }, { label: 'a georeferenced plan (optional)', ok: P.byKind('imageplane').length > 0, open: 'georef', optional: true }],
    has: P => { const ws = P.byKind('lineset').filter(l => l.role === 'workings' && l.parts.length); if (!ws.length) return null; const n = ws.reduce((a, l) => a + l.parts.length, 0), m = ws.reduce((a, l) => a + l.length(), 0); return `${n} working${n > 1 ? 's' : ''} · ${fmtNum(m, 0)} m`; }, next: 'cut a section through them (step 9)' },
  { key: 'structure', step: 3, group: 'FROM THE GEOLOGY', title: 'Structural data', menu: 'Structural data', purpose: 'Dip and dip azimuth: derived from where mapped contacts and faults cross the terrain (the three-point problem), digitised on the map, or imported.',
    needs: P => [{ label: 'mapped traces or a map to digitise on', ok: !!topoOf(P) || P.objects.some(o => o.kind === 'lineset' && ['geology-outline', 'faults', 'lines'].includes(o.role) && o.parts.length), why: 'contacts / faults draped on terrain' }],
    has: P => { const L = P.objects.filter(o => o.kind === 'points' && o.role === 'structural'); if (!L.length) return null; const n = L.reduce((a, l) => a + l.n, 0); return `${n} measurement${n > 1 ? 's' : ''} in ${L.length} layer${L.length > 1 ? 's' : ''}`; }, next: 'stereonet (step 4), form interpolant (step 5)' },
  { key: 'stereonet', step: 4, group: 'FROM THE GEOLOGY', title: 'Stereonet', menu: 'Stereonet', purpose: 'Lower-hemisphere net: poles, great circles, Kamb / Schmidt contours, Bingham and Fisher statistics, selection linked to the scene.',
    needs: P => [{ label: 'structural data', ok: P.objects.some(o => o.kind === 'points' && o.role === 'structural' && o.n > 0), open: 'structure' }],
    has: P => null, next: 'assign categories, then the form interpolant (step 5)' },
  { key: 'form', step: 5, group: 'FROM THE GEOLOGY', title: 'Form interpolant & trends', menu: 'Form interpolant & trends', purpose: 'A gradient-constrained RBF whose iso-surfaces lie parallel to the measured fabric; structural trend fields; the global trend plane.',
    needs: P => [{ label: '3+ structural measurements', ok: P.objects.some(o => o.kind === 'points' && o.role === 'structural' && o.n >= 3), open: 'structure' }],
    has: P => { const f = P.byKind('mesh').filter(m => m.metadata && m.metadata.form_of).length, t = P.objects.filter(o => o.role === 'trend').length; return f || t ? `${f} form surface${f === 1 ? '' : 's'}${t ? `, ${t} trend field` : ''}` : null; }, next: 'stratigraphy (step 6) or sections (step 9)' },
  { key: 'strat', step: 6, group: 'VOLUMES', title: 'Stratigraphy (pancake)', menu: 'Stratigraphy (pancake)', purpose: 'Units youngest-first, each with a base from contact points, a surface grid or a constant; deposit bases on-lap, erosion bases cut.',
    needs: P => [{ label: 'topography', ok: !!topoOf(P) }, { label: 'contact points or a surface grid', ok: P.byKind('points').some(p => p.role === 'contacts') || P.byKind('grid2d').some(g => g.role !== 'topography' && g.role !== 'property'), open: null, optional: true, why: 'constants and thicknesses work without them' }],
    has: P => { const s = P.byKind('stratmodel').find(m => m.units.length); if (!s) return null; return s.metadata.built ? `built · ${s.units.length} units` : `${s.units.length} unit${s.units.length > 1 ? 's' : ''}, not built`; }, next: 'virtual drillhole, block-model domains, section fill' },
  { key: 'implicit', step: 7, group: 'VOLUMES', title: 'Implicit surface (RBF)', menu: 'Implicit surface (RBF)', purpose: 'Signed-distance RBF through contact points (0), hanging-wall (+) and foot-wall (−) points → the iso-surface of a vein, intrusion or ore shell.',
    needs: P => [{ label: 'a points layer with sides or signed distances', ok: P.byKind('points').some(p => p.n >= 4), why: 'or digitise ± points in the panel' }],
    has: P => { const n = P.byKind('mesh').filter(m => m.provenance && /RBF implicit/.test(m.provenance.method || '')).length; return n ? `${n} surface${n > 1 ? 's' : ''}` : null; }, next: 'sections (step 9), export' },
  { key: 'blocks', step: 8, group: 'VOLUMES', title: 'Block model & kriging', menu: 'Block model & kriging', purpose: 'A block grid, a sample layer, an experimental variogram and its fit, then ordinary kriging / IDW / nearest — optionally inside one unit — with cut-offs and grade–tonnage.',
    needs: P => [{ label: 'a points layer with a numeric value', ok: P.byKind('points').some(p => p.role !== 'claims' && Object.keys(p.attributes).some(k => p.isNumeric(k))), why: 'assays, graded mines, imported XYZ' }],
    has: P => { const b = P.byKind('blockmodel'); if (!b.length) return null; const est = b.filter(x => x.metadata.estimates && x.metadata.estimates.length).length; return `${b.length} block model${b.length > 1 ? 's' : ''}${est ? `, ${est} estimated` : ''}`; }, next: 'grade–tonnage, section slice, export UBC / CSV' },
  { key: 'section', step: 9, group: 'SEE IT', title: 'Section & slice', menu: 'Section & slice', purpose: 'Cut the model: clip, intersect every surface, fill the pancake, sample block models, project nearby workings; the 2-D panel draws it the way it is drawn on paper.',
    needs: P => [{ label: 'anything to cut', ok: P.objects.length > 0 }],
    // hidden preset sections are scaffolding, not something the user cut
    has: P => { const s = P.byKind('section').filter(x => x.visible !== false).length; return s ? `${s} section${s > 1 ? 's' : ''}` : null; }, next: 'render image, export DXF / PNG' },
];
const topoOf = P => P.byKind('grid2d').find(g => g.role === 'topography') || null;
export const stepOf = key => TOOL_STEPS.find(s => s.key === key) || null;

export class Tools {
  constructor(app) {
    this.app = app; this.active = null; this.panel = null; this.armed = null; this.prevSelected = null;
    this.section = new SectionTool(this); this.workings = new WorkingsTool(this); this.georef = new GeorefTool(this);
    this.strat = new StratTool(this); this.blocks = new BlocksTool(this); this.implicit = new ImplicitTool(this);
    this.structure = new StructureTool(this); this.stereonet = new StereonetTool(this); this.form = new FormTool(this);
    this.all = { section: this.section, workings: this.workings, georef: this.georef, strat: this.strat, blocks: this.blocks, implicit: this.implicit, structure: this.structure, stereonet: this.stereonet, form: this.form };
    this.extra = [];                                   // [{ key, tool, label, hint }] registered by other modules
    const close = $('toolClose'); if (close) close.onclick = () => this.close();
  }
  get R() { return this.app.R; }
  get project() { return this.app.project; }
  /** Register a tool that lives outside this file (gm-more-tools.js). */
  register(key, tool, meta = {}) { this.all[key] = tool; this.extra.push(Object.assign({ key, tool }, meta)); }
  onProject() { for (const t of Object.values(this.all)) t.onProject && t.onProject(); this.close(); }

  /** Readiness of every step, from the project alone: blocked (a required
      input is missing), ready, or done (it has produced something). */
  readiness() {
    const P = this.project, out = {};
    for (const s of TOOL_STEPS) {
      if (!P) { out[s.key] = { state: 'blocked', why: 'no project', needs: [], has: null }; continue; }
      let needs = [], has = null;
      try { needs = s.needs(P); has = s.has(P); } catch (e) { /* a half-built object must not break the menu */ }
      const missing = needs.filter(n => !n.ok && !n.optional);
      out[s.key] = { state: has ? 'done' : missing.length ? 'blocked' : 'ready', why: missing.map(n => n.label).join(', '), needs, has };
    }
    return out;
  }
  menu(anchor) {
    const r = this.readiness(); const items = []; let group = null;
    for (const s of TOOL_STEPS) {
      if (s.group !== group) { group = s.group; items.push({ head: group }); }
      const st = r[s.key];
      items.push({ label: `${s.step}  ${s.menu}`, cls: st.state, hint: st.state === 'done' ? st.has : st.state === 'blocked' ? `needs ${st.why}` : 'ready', title: s.purpose, onclick: () => this.open(s.key) });
    }
    items.push('-', { head: 'SHORTCUTS' });
    const traces = this.project ? this.project.objects.filter(o => o.kind === 'lineset' && ['geology-outline', 'faults', 'lines'].includes(o.role) && o.parts.length).length : 0;
    items.push({ label: 'Derive structure from all mapped traces', hint: traces ? `${traces} trace layer${traces > 1 ? 's' : ''} · three-point problem` : 'no mapped traces', disabled: !traces, onclick: () => { this.open('structure'); this.structure.deriveAll(); } });
    const built = this.project && this.project.byKind('stratmodel').some(m => m.metadata.built);
    items.push({ label: 'Virtual drillhole (click the ground)', hint: built ? 'column through the pancake' : 'needs a built stratigraphy', cls: built ? '' : 'blocked', onclick: () => { this.open('strat'); if (built) this.strat.virtualHole(); } });
    for (const x of this.extra) items.push({ label: x.label || x.key, hint: x.hint || '', onclick: () => this.open(x.key) });
    menu(anchor, items);
  }
  /** Open a tool: the previous one is stopped, the layer inspector is left
      alone, and the panel lands in the tool host with a title bar. */
  open(name, ...args) {
    const t = this.all[name]; if (!t) return;
    if (this.active && this.active !== t && this.active.stop) this.active.stop();
    this.disarm(); this.active = t; this.current = name;
    this.panel = t.panel(...args); this.mount();
    const right = $('rightPane'); if (right && right.classList.contains('open') === false && window.innerWidth <= 1100) right.classList.add('open');
    return t;
  }
  mount() {
    const host = $('toolhost'), body = $('toolbody'); if (!host || !body) { const insp = $('inspector'); clear(insp); insp.appendChild(this.panel); return; }
    const meta = stepOf(this.current) || (this.extra.find(x => x.key === this.current) || {});
    host.hidden = false;
    host.querySelector('.ttl').textContent = ''; host.querySelector('.ttl').append(meta.step ? h('span', { class: 'step' }, `STEP ${meta.step}/${TOOL_STEPS.length} · `) : '', (meta.title || meta.label || this.current || '').toUpperCase());
    clear(body); body.appendChild(this.headFor()); body.appendChild(this.panel); body.scrollTop = 0;
    this.renderMode();
  }
  /** The NEEDS / HAS / NEXT strip every panel starts with, from TOOL_STEPS. */
  headFor() {
    const meta = stepOf(this.current); if (!meta || !this.project) return h('div');
    const st = this.readiness()[meta.key];
    return toolHead({ step: meta.step, total: TOOL_STEPS.length, title: meta.title, purpose: meta.purpose, needs: st.needs, has: st.has, next: meta.next, open: k => this.open(k) });
  }
  /** Re-render the current tool's panel in place (tools call this after any
      change of their own state). */
  showPanel(p) { this.panel = p; if (this.active) { const body = $('toolbody'); if (body && !$('toolhost').hidden) { const top = body.scrollTop; clear(body); body.appendChild(this.headFor()); body.appendChild(p); body.scrollTop = top; this.renderMode(); return; } } this.mount(); }
  /** Close the tool: stop it, empty the host, let the inspector stand alone. */
  close() {
    if (this.active && this.active.stop) this.active.stop();
    this.disarm(); this.active = null; this.current = null; this.panel = null;
    const host = $('toolhost'); if (host) { host.hidden = true; clear($('toolbody')); }
    this.R.clearOverlay(); $('gl').style.cursor = '';
    if (this.app.renderInspector) this.app.renderInspector();
  }
  /** stop() keeps the panel but cancels any armed mode — Esc while a mode is
      armed; a second Esc closes the tool. */
  stop() { if (this.active && this.active.stop) this.active.stop(); this.disarm(); this.R.clearOverlay(); $('gl').style.cursor = ''; if (this.active && this.active.repanel) this.active.repanel(); else if (this.active && this.active.panel && this.panel) this.showPanel(this.active.panel()); }
  /** One arming path for every click mode.  `text` says what the clicks do
      and how to get out; `target` names what will be written and at what
      confidence, so a trace off the bare ground can never pass as surveyed. */
  arm(tool, text, target = null) {
    if (tool && this.active !== tool) { this.active = tool; }
    this.armed = { text, target };
    $('gl').style.cursor = 'crosshair'; this.renderMode();
    this.app.status(text);
  }
  disarm() { if (!this.armed) return; this.armed = null; $('gl').style.cursor = ''; this.renderMode(); this.app.status(''); }
  renderMode() {
    const bar = $('modebar'), host = $('toolhost');
    if (host) { const m = host.querySelector('.mode'); if (m) m.textContent = this.armed ? '◎ ARMED' : ''; }
    if (!bar) return;
    clear(bar);
    if (!this.armed) { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    bar.appendChild(h('span', {}, h('b', {}, '◎ '), this.armed.text, this.armed.target ? h('span', { class: 'k' }, ' → ' + this.armed.target) : null));
    bar.appendChild(h('button', { class: 'x', title: 'cancel (Esc)', onclick: () => this.stop() }, '✕'));
  }
  /** The empty-inspector card: where things stand, step by step, and what to
      click next. */
  progressCard() {
    const P = this.project, r = this.readiness();
    const card = h('div', { class: 'insp' }, h('h2', {}, 'WHERE THINGS STAND'));
    if (!P) { card.appendChild(note('no project open')); return card; }
    const nextStep = TOOL_STEPS.find(s => r[s.key].state === 'ready') || null;
    const list = h('div', { class: 'steps' });
    for (const s of TOOL_STEPS) {
      const st = r[s.key];
      list.appendChild(h('button', { class: 'step ' + st.state + (nextStep && nextStep.key === s.key ? ' next' : ''), title: s.purpose + (st.state === 'blocked' ? ` — needs ${st.why}` : ''), onclick: () => this.open(s.key) },
        h('span', { class: 'n' }, String(s.step)), h('span', { class: 't' }, s.title),
        h('span', { class: 's' }, st.state === 'done' ? st.has : st.state === 'blocked' ? `needs ${st.why}` : 'ready')));
    }
    card.appendChild(list);
    if (nextStep) card.appendChild(note(`Start here → ${nextStep.step} ${nextStep.title}.`, 'note ok'));
    card.appendChild(note('Click a layer on the left for its properties, hover or click anything in the scene for what it is and where it came from, TOOLS ▾ for the steps.'));
    return card;
  }
}
function showPanel(p) { const host = $('inspector'); clear(host); host.appendChild(p); }
function groundPick(R, e) { const p = R.pick(e.clientX, e.clientY, o => o.kind === 'grid2d' || o.kind === 'mesh' || o.kind === 'imageplane'); return p ? p.world : null; }

/* =========================================================== SECTION */
export const NOT_A_SURVEY = 'NOT A SURVEY — described geometry drawn dashed, assumed dotted';
const r2 = v => Math.round(v * 100) / 100;
/** A 2-D drawing context that records SVG instead of painting pixels.  The
    section is drawn by ONE routine (SectionTool.paint) whether it lands on the
    panel canvas, the PNG export canvas or this recorder, so the three
    pictures cannot drift apart.  Only the subset of CanvasRenderingContext2D
    that paint() uses is implemented; save()/restore() open and close an SVG
    group so translate / scale / clip apply to what is drawn inside them. */
export class SvgCtx {
  constructor(W, H) {
    this.W = W; this.H = H; this.fillStyle = '#000'; this.strokeStyle = '#000'; this.lineWidth = 1; this.font = '10px monospace'; this.textAlign = 'left'; this.textBaseline = 'alphabetic'; this.globalAlpha = 1;
    this._dash = []; this._path = []; this._defs = []; this._nclip = 0;
    this._root = { tag: 'g', attrs: {}, children: [] }; this._stack = [this._root];
  }
  get _cur() { return this._stack[this._stack.length - 1]; }
  _add(tag, attrs, text = null) { this._cur.children.push({ tag, attrs, text }); }
  _alpha(a) { if (this.globalAlpha < 1) a.opacity = r2(this.globalAlpha); return a; }
  setLineDash(d) { this._dash = Array.from(d || []); }
  getLineDash() { return this._dash.slice(); }
  beginPath() { this._path = []; }
  moveTo(x, y) { this._path.push(`M${r2(x)} ${r2(y)}`); }
  lineTo(x, y) { this._path.push(`L${r2(x)} ${r2(y)}`); }
  closePath() { this._path.push('Z'); }
  rect(x, y, w, h) { this._path.push(`M${r2(x)} ${r2(y)}h${r2(w)}v${r2(h)}h${r2(-w)}Z`); }
  arc(x, y, r) { this._path.push(`M${r2(x + r)} ${r2(y)}A${r2(r)} ${r2(r)} 0 1 0 ${r2(x - r)} ${r2(y)}A${r2(r)} ${r2(r)} 0 1 0 ${r2(x + r)} ${r2(y)}`); }
  fill() { if (this._path.length) this._add('path', this._alpha({ d: this._path.join(''), fill: this.fillStyle })); }
  stroke() { if (this._path.length) this._add('path', this._alpha({ d: this._path.join(''), fill: 'none', stroke: this.strokeStyle, 'stroke-width': r2(this.lineWidth), 'stroke-dasharray': this._dash.length ? this._dash.map(r2).join(' ') : null })); }
  fillRect(x, y, w, h) { this._add('rect', this._alpha({ x: r2(x), y: r2(y), width: r2(w), height: r2(h), fill: this.fillStyle })); }
  strokeRect(x, y, w, h) { this._add('rect', this._alpha({ x: r2(x), y: r2(y), width: r2(w), height: r2(h), fill: 'none', stroke: this.strokeStyle, 'stroke-width': r2(this.lineWidth) })); }
  clearRect() { }
  _font() { const m = /(\d+(?:\.\d+)?)px\s*(.*)$/.exec(this.font) || []; return { size: m[1] ? +m[1] : 10, family: (m[2] || 'monospace').replace(/"/g, "'"), bold: /bold/.test(this.font) }; }
  fillText(t, x, y) { const f = this._font(); this._add('text', this._alpha({ x: r2(x), y: r2(y), fill: this.fillStyle, 'font-size': f.size, 'font-family': f.family, 'font-weight': f.bold ? 'bold' : null, 'text-anchor': this.textAlign === 'center' ? 'middle' : this.textAlign === 'right' ? 'end' : null }), String(t)); }
  measureText(t) { return { width: String(t).length * this._font().size * 0.6 }; }
  drawImage(img, x, y, w, h) { const href = img.toDataURL ? img.toDataURL('image/png') : (img.currentSrc || img.src || ''); if (!href) return; this._add('image', this._alpha({ href, x: r2(x), y: r2(y), width: r2(w), height: r2(h), preserveAspectRatio: 'none' })); }
  save() { const g = { tag: 'g', attrs: {}, children: [], state: { fillStyle: this.fillStyle, strokeStyle: this.strokeStyle, lineWidth: this.lineWidth, font: this.font, textAlign: this.textAlign, globalAlpha: this.globalAlpha, dash: this._dash.slice() } }; this._cur.children.push(g); this._stack.push(g); }
  restore() { if (this._stack.length < 2) return; const s = this._stack.pop().state; this.fillStyle = s.fillStyle; this.strokeStyle = s.strokeStyle; this.lineWidth = s.lineWidth; this.font = s.font; this.textAlign = s.textAlign; this.globalAlpha = s.globalAlpha; this._dash = s.dash; }
  _xform(t) { const c = this._cur; c.attrs.transform = (c.attrs.transform ? c.attrs.transform + ' ' : '') + t; }
  translate(tx, ty) { this._xform(`translate(${r2(tx)} ${r2(ty)})`); }
  scale(sx, sy) { this._xform(`scale(${r2(sx)} ${r2(sy)})`); }
  clip() { const id = 'clip' + (++this._nclip); this._defs.push(`<clipPath id="${id}"><path d="${this._path.join('')}"/></clipPath>`); this._cur.attrs['clip-path'] = `url(#${id})`; }
  toString() {
    const attr = a => Object.entries(a).filter(([, v]) => v != null).map(([k, v]) => ` ${k}="${GM.esc(String(v))}"`).join('');
    const ser = n => n.text != null ? `<${n.tag}${attr(n.attrs)}>${GM.esc(n.text)}</${n.tag}>` : n.children ? `<${n.tag}${attr(n.attrs)}>${n.children.map(ser).join('')}</${n.tag}>` : `<${n.tag}${attr(n.attrs)}/>`;
    return `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="${this.W}" height="${this.H}" viewBox="0 0 ${this.W} ${this.H}">${this._defs.length ? '<defs>' + this._defs.join('') + '</defs>' : ''}${this._root.children.map(ser).join('')}</svg>`;
  }
}

export class SectionTool {
  constructor(T) { this.T = T; this.sec = null; this.mode = null; this.pts = []; this.slice = false; this.thick = false; this.side = 1; this.products = []; this.panelOpen = false; this.offset = 0; this.band = 25; this._imgCache = new Map(); this._pending = false; }
  get app() { return this.T.app; }
  onProject() { this.sec = null; this.products = []; this._imgCache.clear(); this.clear2d(); }
  /** The offset slider sweeps ± half the longest horizontal extent of the
      project, in steps of 0.5 % of it — a 400 m prospect gets 1 m steps, a
      10 km district 50 m, never a hard-coded ±2000 m. */
  offsetRange() { const b = this.app.project.bounds(); const ext = b ? Math.max(b[3] - b[0], b[4] - b[1], 100) : 4000; const half = Math.ceil(ext / 2); const step = Math.max(0.5, +(ext * 0.005).toPrecision(2)); return { min: -half, max: half, step }; }
  sectionImageLayers() { return this.app.project.byKind('imageplane').filter(ip => ip.plane === 'section' && ip.p1 && ip.p2 && ip.zTop != null && ip.zBottom != null); }
  panel(sec) {
    if (sec) this.sec = sec;
    if (!this.sec) this.sec = this.app.project.byKind('section')[0] || null;
    const P = h('div', { class: 'tool' }, h('h2', {}, 'SECTION & SLICE'));
    const list = this.app.project.byKind('section');
    // Choosing a section shows it: the presets start hidden so a fresh model is
    // terrain, not glass walls, and the tool is where they become visible.
    P.appendChild(row('section', sel([['', '— pick —'], ...list.map(s => [s.id, s.name + (s.visible === false ? ' (hidden)' : '')])], this.sec ? this.sec.id : '', { onchange: e => { this.sec = this.app.project.get(e.target.value); this.offset = 0; if (this.sec && this.sec.visible === false) { this.sec.visible = true; this.app.R.setVisible(this.sec.id, true); this.app.renderLayers(); } this.update(); this.T.showPanel(this.panel()); } })));
    P.appendChild(h('div', { class: 'frow' }, btn('DRAW LINE (2 clicks)', () => this.startDraw()), btn('W–E here', () => this.preset('we')), btn('S–N here', () => this.preset('sn'))));
    const secImages = this.sectionImageLayers();
    if (secImages.length) P.appendChild(h('div', { class: 'frow' }, ...secImages.map(ip => btn(`SECTION ON THIS IMAGE: ${ip.name}`, () => this.sectionFromImage(ip), { title: 'a section line between the two top corners of this georeferenced section image, from its bottom to its top elevation — the image is then drawn under the 2-D section' }))));
    if (this.sec) {
      const s = this.sec; const L = Math.hypot(s.end[0] - s.start[0], s.end[1] - s.start[1]);
      P.appendChild(kv([['Name', s.name], ['Length', fmtNum(L, 0) + ' m'], ['Azimuth', fmtNum(azimuthOf(s), 0) + '°']]));
      const or = this.offsetRange(); const offLbl = h('span', { class: 'mono' }, `${fmtNum(this.offset, 0)} m ⟂`);
      // the slider and every control below call update() only — never a panel
      // rebuild — and the slider's calls are folded into one run per frame
      P.appendChild(row('offset', range(Math.max(or.min, Math.min(or.max, this.offset)), or.min, or.max, or.step, e => { this.offset = +e.target.value; offLbl.textContent = `${fmtNum(this.offset, 0)} m ⟂`; this.update(true); }, { style: { flex: 1 }, title: `±${or.max} m — half the model's longest extent — in ${or.step} m steps` }), offLbl));
      P.appendChild(row('z from / to', num(s.zMin, { title: 'bottom of the section, m (kept on the section object)', onchange: e => this.setZ(+e.target.value, s.zMax) }), num(s.zMax, { title: 'top of the section, m', onchange: e => this.setZ(s.zMin, +e.target.value) })));
      P.appendChild(row('clip model', sel([['0', 'no clipping'], ['1', 'hide front (look at the cut face)'], ['-1', 'hide back'], ['band', 'thick slice (band)']], this.clipMode(), { onchange: e => this.setClipMode(e.target.value) })));
      P.appendChild(row('band ± m', num(this.band, { onchange: e => { this.band = Math.max(0, +e.target.value); this.update(true); } }), h('span', { class: 'mono', title: 'the 2-D band and the 3-D thick slice are the same slab' }, 'half-width')));
      P.appendChild(row('products', ...['lines', 'strat', 'blocks', 'near'].map(k => h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt(k), onchange: e => { this['show_' + k] = e.target.checked; this.update(true); } }), { lines: 'surface cuts', strat: 'pancake fill', blocks: 'block slice', near: 'nearby workings' }[k]))));
      P.appendChild(h('div', { class: 'frow' }, btn('2-D SECTION PANEL', () => this.togglePanel()), btn('SAVE CUTS AS LAYERS', () => this.saveProducts()), btn('EXPORT DXF', () => this.exportDxf()), btn('EXPORT PNG', () => this.exportPng(), { title: 'the 2-D section at 2×, with title, scale bar, key, sources and the confidence sentence' }), btn('EXPORT SVG', () => this.exportSvg(), { title: 'the same drawing as vectors' })));
      P.appendChild(h('div', { class: 'frow' }, btn('rename', async () => { const n = await promptModal('RENAME SECTION', 'name', s.name); if (n) { s.name = n; this.app.renderLayers(); this.T.showPanel(this.panel()); } }), btn('delete section', () => { const gone = s; this.app.destructive(`deleted ${gone.name}`, () => { this.app.project.remove(gone); if (this.sec === gone) this.sec = null; this.update(); this.T.showPanel(this.panel()); }, () => { this.app.project.add(gone); this.sec = gone; this.update(); this.T.showPanel(this.panel()); }); }, { class: 'b danger' })));
    }
    P.appendChild(note('The plane clips everything that is clippable (or keeps a slab, in thick-slice mode), cuts every mesh and surface into lines, fills the stratigraphic units along the line, samples block models onto the plane and projects workings within the band. Sweep with the offset slider. Georeferenced section images that lie in the plane are drawn under the 2-D section.'));
    this.update();
    return P;
  }
  opt(k) { return this['show_' + k] == null ? true : this['show_' + k]; }
  clipMode() { return this.thick ? 'band' : this.slice ? String(this.side) : '0'; }
  setClipMode(v) { this.thick = v === 'band'; this.slice = v !== '0'; if (v !== 'band') this.side = +v || 1; this.update(); }
  /** z range of the section, edited in the panel and kept on the Section
      object (it serialises as z_min / z_max). */
  setZ(zmin, zmax) { const s = this.sec; if (!s || zmin !== zmin || zmax !== zmax) return; if (zmax <= zmin) { toast('z to must be above z from', 'warn'); return; } s.zMin = zmin; s.zMax = zmax; this.app.syncObject(s); this.app.markDirty(); this.update(); }
  /** A section line along the two top corners of a georeferenced section
      image, between its bottom and top elevations. */
  sectionFromImage(ip) {
    const s = new GM.Section({ start: [ip.p1[0], ip.p1[1]], end: [ip.p2[0], ip.p2[1]], z_min: Math.min(ip.zTop, ip.zBottom), z_max: Math.max(ip.zTop, ip.zBottom), name: `Section on ${ip.name}` });
    s.provenance = { method: 'section line from a georeferenced section image', source_layer: ip.name, source_id: ip.id }; s.metadata.derived_from = [ip.id];
    this.app.project.add(s); this.sec = s; this.offset = 0; this.update(); this.app.select(s.id); this.T.showPanel(this.panel());
    toast(`${s.name} — the image is drawn under the 2-D section`, 'ok', 5000); return s;
  }
  startDraw() { this.mode = 'draw'; this.pts = []; this.T.arm(this, 'SECTION — click the start point on the ground, then the end · Esc cancels', 'a new vertical section'); }
  preset(kind) {
    const topo = this.app.topoGrid(); const b = this.app.project.bounds(); if (!b) return; const cx = (b[0] + b[3]) / 2, cy = (b[1] + b[4]) / 2; const R = Math.max(b[3] - b[0], b[4] - b[1]) / 2;
    const t = this.app.R.controls.target; const w = this.app.R.fromScene(new THREE.Vector3(t.x, t.y, t.z)); const x = w[0], y = w[1];
    this.create(kind === 'we' ? [cx - R, y] : [x, cy - R], kind === 'we' ? [cx + R, y] : [x, cy + R], kind === 'we' ? 'Section W-E' : 'Section S-N');
  }
  create(start, end, name) {
    const b = this.app.project.bounds(); const zmin = b ? b[2] - 200 : -500, zmax = b ? b[5] + 50 : 500;
    // Serial names: a third W–E section is 'Section W-E [3]', never a second
    // row reading exactly like the first.
    const stem = name || 'Section'; const same = this.app.project.byKind('section').filter(x => x.name === stem || x.name.startsWith(stem + ' [')).length;
    const s = new GM.Section({ start, end, z_min: zmin, z_max: zmax, name: same ? `${stem} [${same + 1}]` : stem });
    this.app.project.add(s); this.sec = s; this.offset = 0; this.update(); this.T.showPanel(this.panel());
    toast(`${s.name} created — sweep it with the offset slider, clip with "clip model", 2-D SECTION PANEL draws it`, 'ok', 5000);
  }
  onClick(e) {
    if (this.mode !== 'draw') return false;
    const w = groundPick(this.app.R, e); if (!w) return true;
    this.pts.push(w);
    if (this.pts.length === 1) { this.app.R.overlayMarker(w, 0x2dd4bf); this.app.status('click the end point'); }
    else { this.mode = null; this.T.disarm(); this.app.R.clearOverlay(); this.create([this.pts[0][0], this.pts[0][1]], [this.pts[1][0], this.pts[1][1]]); }
    return true;
  }
  onMove(e) { if (this.mode !== 'draw' || this.pts.length !== 1) return false; const w = groundPick(this.app.R, e); if (!w) return true; this.app.R.clearOverlay(); this.app.R.overlayMarker(this.pts[0], 0x2dd4bf); this.app.R.overlayPolyline([this.pts[0], w], 0x2dd4bf, true); return true; }
  onKey(e) { if (e.key === 'Escape' && this.mode) { this.mode = null; this.app.R.clearOverlay(); this.T.disarm(); return true; } return false; }
  stop() { this.mode = null; this.pts = []; }
  plane() {
    if (!this.sec) return null; const s = this.sec; const { point, normal } = E.sectionPlane(s.start, s.end);
    const p = [point[0] + normal[0] * this.offset, point[1] + normal[1] * this.offset, 0];
    return { point: p, normal, start: [s.start[0] + normal[0] * this.offset, s.start[1] + normal[1] * this.offset], end: [s.end[0] + normal[0] * this.offset, s.end[1] + normal[1] * this.offset] };
  }
  /** Recompute the products.  A plain update() runs at once — callers (and
      the tests) read `products` and the clip straight after it.  The panel's
      slider and controls pass coalesce=true: those calls are folded into one
      run per animation frame, so dragging the offset slider never intersects
      every mesh more than once a frame. */
  update(coalesce = false) {
    if (!coalesce) { this._pending = false; this._update(); return; }
    if (this._pending) return;
    this._pending = true;
    requestAnimationFrame(() => { if (!this._pending) return; this._pending = false; this._update(); });
  }
  _update() {
    const R = this.app.R; const pl = this.plane();
    // products group lives under the section layer
    for (const L of R.layers.values()) if (L.products) { for (const c of [...L.products.children]) { L.products.remove(c); if (c.geometry) c.geometry.dispose(); } }
    this.products = [];
    if (!pl) { R.setClip(null); this.draw2d(); return; }
    const s = this.sec; const Lsec = R.layers.get(s.id); if (Lsec) { Lsec.group.position.set(pl.normal[0] * this.offset, 0, -pl.normal[1] * this.offset); }
    // thick slice: the renderer keeps a slab of the band's full width, so the
    // 3-D slab and the 2-D band are the same thing; the half-space modes are
    // the four-argument call they always were
    if (this.thick) R.setClip(pl.point, pl.normal, this.side, true, this.band * 2);
    else R.setClip(pl.point, pl.normal, this.side, this.slice);
    const P = this.app.project; const n = pl.normal, pt = pl.point;
    const prods = [];
    if (this.opt('lines')) {
      for (const o of P.objects) {
        if (o.visible === false) continue;
        if (o.kind === 'mesh' && o.role !== 'section') { const ls = E.meshPlaneIntersection(o, pt, n); if (ls.parts.length) prods.push({ kind: 'line', obj: o, ls, color: o.color }); }
        if (o.kind === 'grid2d') { const pls = E.profileLineSet(o, pl.start, pl.end, { n: 240, lift: 0.5 }); if (pls.parts.length) prods.push({ kind: 'line', obj: o, ls: pls, color: o.role === 'topography' ? [255, 255, 255] : o.color, width: o.role === 'topography' ? 2 : 1 }); }
      }
    }
    const strat = P.byKind('stratmodel').find(sm => sm.units.length && sm.units.some(u => u.base)); const topo = this.app.topoGrid();
    if (this.opt('strat') && strat && topo) {
      const grids = {}; for (const u of strat.units) if (u.base) { const g = P.get(u.base); if (g) grids[u.base] = g; }
      if (Object.keys(grids).length) { try { const ribbons = E.stratigraphySection(strat, grids, topo, pl.start, pl.end, 200); for (const m of ribbons) prods.push({ kind: 'ribbon', mesh: m }); } catch (e) { console.warn(e); } }
    }
    if (this.opt('blocks')) for (const bm of P.byKind('blockmodel')) { if (bm.visible === false) continue; const d = this.app.display.get(bm.id) || {}; const attr = d.attribute || Object.keys(bm.attributes)[0]; if (!attr) continue; const smp = E.blockmodelPlaneSample(bm, attr, [pt[0], pt[1], (s.zMin + s.zMax) / 2], n, { resolution: Math.min(...bm.blockSize) / 2 }); prods.push({ kind: 'blocks', bm, attr, smp, d }); }
    if (this.opt('near')) for (const o of P.objects) { if (o.kind !== 'lineset' || o.visible === false || !(o.role === 'workings' || o.role === 'drillhole-traces' || o.role === 'faults')) continue; const near = E.linesetNearPlane(o, pt, n, this.band, { project: true }); if (near.parts.length) prods.push({ kind: 'near', obj: o, ls: near }); }
    this.products = prods;
    // 3-D products
    if (Lsec && Lsec.products) {
      for (const pr of prods) {
        if (pr.kind === 'line' || pr.kind === 'near') { const seg = R.linesGeometry(pr.ls, pr.kind === 'near' && pr.obj.role === 'workings' ? null : (pr.color || pr.obj.color)); const m = new THREE.LineSegments(seg.geo, new THREE.LineBasicMaterial({ vertexColors: true, depthTest: true })); m.userData.clippable = false; m.position.set(-pl.normal[0] * this.offset, 0, pl.normal[1] * this.offset); Lsec.products.add(m); }
        if (pr.kind === 'ribbon') { const geo = R.geomFromMesh(pr.mesh); const m = new THREE.Mesh(geo, R.material(pr.mesh.color, { flat: true, transparent: true, opacity: 0.95 })); m.userData.clippable = false; m.position.set(-pl.normal[0] * this.offset, 0, pl.normal[1] * this.offset); Lsec.products.add(m); }
        if (pr.kind === 'blocks') { const tex = this.sampleTexture(pr); if (tex) { const c = pr.smp.corners; const pos = new Float32Array(12); c.forEach((p, i) => { const q = R.toSceneArr(p[0], p[1], p[2]); pos[3 * i] = q[0]; pos[3 * i + 1] = q[1]; pos[3 * i + 2] = q[2]; }); const geo = new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(pos, 3)); geo.setAttribute('uv', new THREE.Float32BufferAttribute([0, 1, 1, 1, 1, 0, 0, 0], 2)); geo.setIndex([0, 2, 1, 0, 3, 2]); const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: tex, side: THREE.DoubleSide, transparent: true, opacity: 0.95, depthWrite: false })); m.userData.clippable = false; m.position.set(-pl.normal[0] * this.offset, 0, pl.normal[1] * this.offset); Lsec.products.add(m); } }
      }
    } else for (const pr of prods) if (pr.kind === 'blocks') this.sampleTexture(pr);   // the 2-D panel still needs the raster
    R.invalidate(); this.draw2d();
  }
  sampleTexture(pr) {
    const { smp, d } = pr; const w = smp.width, hgt = smp.height; if (!w || !hgt) return null; const c = document.createElement('canvas'); c.width = w; c.height = hgt; const ctx = c.getContext('2d'); const img = ctx.createImageData(w, hgt);
    const vals = smp.values; const cat = typeof vals[0] === 'string' || (Array.isArray(vals) && vals.some(v => typeof v === 'string'));
    let lo = Infinity, hi = -Infinity; if (!cat) { const L = this.app.R.layers.get(pr.bm.id); if (L && L.range) [lo, hi] = L.range; else for (const v of vals) { if (v !== v || v == null) continue; if (v < lo) lo = v; if (v > hi) hi = v; } }
    const cats = cat ? [...new Set(vals.filter(v => v))] : null;
    pr.range = cat ? null : [lo, hi]; pr.cats = cats; pr.cmap = d.colormap || (cat ? 'geology' : 'turbo');
    for (let i = 0; i < vals.length; i++) { const v = vals[i]; let col = null; if (cat) { if (v) col = GM.colormap(d.colormap || 'geology', cats.length > 1 ? cats.indexOf(v) / (cats.length - 1) : 0.5); } else if (v === v && v != null && (d.cutoff == null || v >= d.cutoff)) col = GM.colormap(d.colormap || 'turbo', hi > lo ? (v - lo) / (hi - lo) : 0.5); if (!col) continue; img.data[4 * i] = col[0]; img.data[4 * i + 1] = col[1]; img.data[4 * i + 2] = col[2]; img.data[4 * i + 3] = 235; }
    ctx.putImageData(img, 0, 0); pr.canvas = c; return canvasTexture(c);
  }
  saveProducts() { let n = 0; for (const pr of this.products) { if (pr.kind === 'line' || pr.kind === 'near') { pr.ls.name = `${this.sec.name} · ${pr.obj.name}`; pr.ls.group = 'Sections'; pr.ls.role = 'section'; this.app.project.add(pr.ls); n++; } if (pr.kind === 'ribbon') { pr.mesh.group = 'Sections'; pr.mesh.name = `${this.sec.name} · ${pr.mesh.name}`; this.app.project.add(pr.mesh); n++; } } toast(`${n} section layers saved`, 'ok'); }
  async exportDxf() { const objs = []; for (const pr of this.products) { if (pr.ls) objs.push(pr.ls); if (pr.mesh) objs.push(pr.mesh); } if (!objs.length) return toast('nothing to export', 'warn'); const files = await F.writeAs('dxf', objs, { basename: GM.slug(this.sec.name) }); for (const [n, v] of Object.entries(files)) GM.downloadBlob(new Blob([v]), n); }
  /* ---------------------------------------------------------- 2-D panel */
  togglePanel() { this.setPanel(!this.panelOpen); }
  // The panel's own ✕ used to hide the element directly, which left panelOpen
  // true; the next click on 2-D SECTION PANEL then toggled it back to false and
  // hid an already-hidden panel, so the button looked dead until pressed twice.
  closePanel() { this.setPanel(false); }
  setPanel(open) { this.panelOpen = !!open; $('sec2d').style.display = this.panelOpen ? 'flex' : 'none'; this.app.R.resize(); this.draw2d(); }
  clear2d() { const c = $('sec2dCanvas'); if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height); }
  draw2d() {
    if (!this.panelOpen) return; const c = $('sec2dCanvas'); if (!c) return; const box = c.parentElement; const dpr = window.devicePixelRatio || 1; const W = box.clientWidth, H = box.clientHeight;
    c.width = W * dpr; c.height = H * dpr; const ctx = c.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.paint(ctx, W, H);
  }
  /** The distance / elevation frame of the drawing: everything that maps
      metres to pixels, shared by the panel, the exports and the scale bar. */
  frame(pl, W, H, pad) {
    const s = this.sec; const L = Math.hypot(pl.end[0] - pl.start[0], pl.end[1] - pl.start[1]) || 1; const u = [(pl.end[0] - pl.start[0]) / L, (pl.end[1] - pl.start[1]) / L];
    const zmin = s.zMin == null ? -500 : s.zMin, zmax = s.zMax == null ? 2000 : s.zMax; const ve = this.app.R.ve;
    const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b; const scale = Math.min(pw / L, ph / (Math.max(zmax - zmin, 1) * ve));
    return { L, u, zmin, zmax, ve, pw, ph, scale, pad, X: d => pad.l + d * scale, Y: z => pad.t + ph - (z - zmin) * ve * scale, along: p => (p[0] - pl.start[0]) * u[0] + (p[1] - pl.start[1]) * u[1] };
  }
  /** Georeferenced section images sorted against the current plane: `on` —
      both top corners within the band and the strike within ~10° of the
      section's, so the image can be drawn under the products; `oblique` —
      near the plane but not in it, which the title line says out loud. */
  sectionImages(pl, F) {
    const out = { on: [], oblique: [] }; const n = pl.normal; const sd = p => (p[0] - pl.point[0]) * n[0] + (p[1] - pl.point[1]) * n[1];
    for (const ip of this.sectionImageLayers()) {
      if (ip.visible === false) continue;
      const d1 = sd(ip.p1), d2 = sd(ip.p2); const L2 = Math.hypot(ip.p2[0] - ip.p1[0], ip.p2[1] - ip.p1[1]) || 1; const v = [(ip.p2[0] - ip.p1[0]) / L2, (ip.p2[1] - ip.p1[1]) / L2];
      const ang = Math.acos(Math.min(1, Math.abs(F.u[0] * v[0] + F.u[1] * v[1]))) * 180 / Math.PI;
      const inBand = Math.abs(d1) <= this.band && Math.abs(d2) <= this.band;
      if (inBand && ang <= 10) out.on.push(ip);
      else if (Math.min(Math.abs(d1), Math.abs(d2)) <= this.band || d1 * d2 < 0) out.oblique.push(ip);
    }
    return out;
  }
  /** Decode an image plane's raster once; the first draw after decoding
      repaints the panel. */
  imageFor(ip) {
    let im = this._imgCache.get(ip.id);
    if (!im || im._src !== ip.image) { im = new Image(); im._src = ip.image; im.onload = () => this.draw2d(); im.src = ip.image; this._imgCache.set(ip.id, im); }
    return im.complete && im.naturalWidth ? im : null;
  }
  imagesReady() { return Promise.all([...this._imgCache.values()].map(im => im.complete ? null : new Promise(r => { im.addEventListener('load', r, { once: true }); im.addEventListener('error', r, { once: true }); }))); }
  titleLine(F, imgs) { const s = this.sec; return `${s.name}${this.offset ? ` (offset ${fmtNum(this.offset, 0)} m)` : ''} — ${fmtNum(F.L, 0)} m, VE ×${F.ve.toFixed(1)}, looking ${lookDir(F.u)}${imgs.oblique.length ? ' · image plane oblique to this section — shown in 3-D only' : ''}`; }
  /** Draw the section into [0,0]–[W,H] of ANY 2-D context: the panel canvas,
      the export canvas, or an SvgCtx.  Distance along the line vs elevation,
      vertical exaggeration from the viewer; section images underneath, then
      block slices, unit ribbons, surface cuts and nearby workings (described
      dashed, assumed dotted, surveyed solid), then points within 4 bands. */
  paint(ctx, W, H, o = {}) {
    ctx.fillStyle = '#0b0e13'; ctx.fillRect(0, 0, W, H);
    const pl = this.plane(); if (!pl) { ctx.fillStyle = '#8a97a6'; ctx.font = '11px ui-monospace, monospace'; ctx.textAlign = 'left'; ctx.fillText('no section', 20, 30); return null; }
    const pad = o.pad || { l: 56, r: 16, t: 18, b: 30 }; const F = this.frame(pl, W, H, pad); const { L, zmin, zmax, ve, pw, ph, scale, X, Y, along } = F;
    const imgs = this.sectionImages(pl, F);
    // grid + axes
    ctx.strokeStyle = '#1e2630'; ctx.lineWidth = 1; ctx.font = '10px ui-monospace, monospace'; ctx.fillStyle = '#8a97a6'; ctx.setLineDash([]);
    // Tick spacing has to come from the pixels available, not from the data
    // extent: a section that is tall relative to its length is fitted on its
    // height, so its plotted width can be a fraction of the panel and eight
    // distance labels then overprint each other into a smear.
    const zticks = Math.max(2, Math.min(6, Math.floor(ph / 34)));
    const dticks = Math.max(2, Math.min(8, Math.floor((L * scale) / 54)));
    const zstep = niceStep((zmax - zmin) / zticks); for (let z = Math.ceil(zmin / zstep) * zstep; z <= zmax; z += zstep) { ctx.beginPath(); ctx.moveTo(pad.l, Y(z)); ctx.lineTo(X(L), Y(z)); ctx.stroke(); ctx.textAlign = 'right'; ctx.fillText(fmtNum(z, 0), pad.l - 6, Y(z) + 3); }
    const dstep = niceStep(L / dticks); for (let d = 0; d <= L; d += dstep) { ctx.beginPath(); ctx.moveTo(X(d), pad.t); ctx.lineTo(X(d), Y(zmin)); ctx.stroke(); ctx.textAlign = 'center'; ctx.fillText(fmtNum(d, 0), X(d), Y(zmin) + 14); }
    ctx.textAlign = 'left';
    if (o.title !== false) { ctx.fillStyle = '#c9d1d9'; ctx.fillText(this.titleLine(F, imgs), pad.l + 6, 12); }
    ctx.save(); ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
    // section images in the plane, under everything else
    for (const ip of imgs.on) {
      const im = this.imageFor(ip); if (!im) continue;
      const d0 = along(ip.p1), d1 = along(ip.p2); const x0 = X(Math.min(d0, d1)), w = Math.abs(X(d1) - X(d0)); const yT = Y(Math.max(ip.zTop, ip.zBottom)), hgt = Math.abs(Y(ip.zTop) - Y(ip.zBottom));
      ctx.save(); ctx.globalAlpha = ip.opacity == null ? 0.9 : ip.opacity;
      ctx.translate(d0 > d1 ? x0 + w : x0, ip.zTop < ip.zBottom ? yT + hgt : yT); ctx.scale(d0 > d1 ? -1 : 1, ip.zTop < ip.zBottom ? -1 : 1); ctx.drawImage(im, 0, 0, w, hgt);
      ctx.restore();
    }
    for (const pr of this.products) {
      // Column 0 of the sampled raster belongs at corners[0] — that is how the
      // 3-D plane is textured (uv 0 at c4[0]).  When corners[0] is the far end of
      // the section the bitmap has to be mirrored to land the same way round;
      // drawing it unflipped put the grades at the wrong distance along the line.
      if (pr.kind === 'blocks' && pr.canvas) {
        const c4 = pr.smp.corners; const d0 = along(c4[0]), d1 = along(c4[1]); const z0 = c4[0][2], z1 = c4[3][2];
        const x0 = X(Math.min(d0, d1)), yTop = Y(Math.max(z0, z1));
        const w = Math.abs(X(d1) - X(d0)), hgt = Math.abs(Y(z1) - Y(z0));
        ctx.save(); ctx.globalAlpha = 0.9;
        if (d0 > d1) { ctx.translate(x0 + w, 0); ctx.scale(-1, 1); ctx.drawImage(pr.canvas, 0, yTop, w, hgt); }
        else ctx.drawImage(pr.canvas, x0, yTop, w, hgt);
        ctx.restore();
      }
    }
    for (const pr of this.products) if (pr.kind === 'ribbon') { const m = pr.mesh; ctx.fillStyle = rgba(m.color, 0.85); ctx.beginPath(); for (let t = 0; t < m.nTriangles; t++) { const [a, b, c2] = [m.triangles[3 * t], m.triangles[3 * t + 1], m.triangles[3 * t + 2]]; const pa = m.vertex(a), pb = m.vertex(b), pc = m.vertex(c2); ctx.moveTo(X(along(pa)), Y(pa[2])); ctx.lineTo(X(along(pb)), Y(pb[2])); ctx.lineTo(X(along(pc)), Y(pc[2])); ctx.closePath(); } ctx.fill(); }
    for (const pr of this.products) if (pr.kind === 'line' || pr.kind === 'near') {
      const ls = pr.ls; const isWk = pr.kind === 'near' && pr.obj.role === 'workings';
      for (let k = 0; k < ls.parts.length; k++) {
        const f = ls.features[k] || {}; const col = isWk ? (E.WORKING_TYPES[f.type] || E.WORKING_TYPES.unknown).color : (pr.color || pr.obj.color);
        const cc = isWk ? CONF_CLASSES.find(c => c.key === confClass(f)) : null;
        ctx.strokeStyle = rgba(col, 1); ctx.lineWidth = pr.width || (pr.kind === 'near' ? 2.5 : 1.3); ctx.setLineDash(cc && cc.dash ? cc.dash : []);
        ctx.beginPath(); const pts = ls.partXYZ(k); pts.forEach((p, i) => { if (i) ctx.lineTo(X(along(p)), Y(p[2])); else ctx.moveTo(X(along(p)), Y(p[2])); }); ctx.stroke(); ctx.setLineDash([]);
        if (pr.kind === 'near' && f.name) { ctx.fillStyle = '#e6edf3'; ctx.fillText(f.name, X(along(pts[0])) + 4, Y(pts[0][2]) - 4); }
      }
    }
    // points near the plane
    for (const ps of this.app.project.byKind('points')) { if (ps.visible === false) continue; const nm = ps.attributes.name || []; for (let i = 0; i < ps.n; i++) { const p = ps.point(i); const dist = (p[0] - pl.point[0]) * pl.normal[0] + (p[1] - pl.point[1]) * pl.normal[1]; if (Math.abs(dist) > this.band * 4) continue; const d = along(p); if (d < 0 || d > L) continue; ctx.fillStyle = rgba(ps.color, 1); ctx.beginPath(); ctx.arc(X(d), Y(p[2]), 3.5, 0, Math.PI * 2); ctx.fill(); if (nm[i] && ps.role === 'mines') { ctx.fillStyle = '#ffd27a'; ctx.fillText(String(nm[i]).slice(0, 28), X(d) + 5, Y(p[2]) - 5); } } }
    ctx.restore();
    return F;
  }
  /* ------------------------------------------------------------ export */
  /** What the key has to say, from the products alone, so the export can be
      laid out before anything is painted: the unit fills present, the
      working types and their confidence counts, block colour ramps, and the
      documents the workings on this section were read from. */
  keyModel() {
    const K = { units: [], types: [], conf: { surveyed: 0, described: 0, assumed: 0 }, blocks: [], sources: [], notSurvey: false, images: [], oblique: [] };
    const seenU = new Set(), seenT = new Map(), seenS = new Set();
    for (const pr of this.products) {
      if (pr.kind === 'ribbon' && !seenU.has(pr.mesh.name)) { seenU.add(pr.mesh.name); K.units.push({ name: pr.mesh.name, color: pr.mesh.color }); }
      if (pr.kind === 'near' && pr.obj.role === 'workings') for (const f of pr.ls.features) { const t = f.type in E.WORKING_TYPES ? f.type : 'unknown'; seenT.set(t, (seenT.get(t) || 0) + 1); K.conf[confClass(f)]++; const src = f.source || {}; const doc = typeof src === 'string' ? src : (src.doc || src.document); if (doc) seenS.add(doc + (src.page ? ' p. ' + src.page : '')); }
      if (pr.kind === 'blocks') K.blocks.push({ name: pr.bm.name, attr: pr.attr, cmap: pr.cmap || 'turbo', lo: pr.range ? pr.range[0] : NaN, hi: pr.range ? pr.range[1] : NaN, cats: pr.cats, cutoff: pr.d && pr.d.cutoff != null ? pr.d.cutoff : null });
    }
    K.types = [...seenT].map(([t, n]) => ({ type: t, n, color: E.WORKING_TYPES[t].color }));
    K.sources = [...seenS];
    K.notSurvey = K.conf.described + K.conf.assumed > 0;
    const pl = this.plane(); if (pl) { const imgs = this.sectionImages(pl, this.frame(pl, 100, 100, { l: 0, r: 0, t: 0, b: 0 })); K.images = imgs.on.map(i => i.name); K.oblique = imgs.oblique.map(i => i.name); }
    return K;
  }
  /** Draw the key block below the drawing — or, with measure=true, only
      measure it — and return the y below it.  Text widths always come from
      lay.mc (a real canvas context), so the SVG lays out exactly as the PNG. */
  drawKey(ctx, K, lay, measure) {
    const x0 = lay.pad.l, W = lay.W - lay.pad.l - lay.pad.r, lh = 18, col = x0 + 104; let y = lay.headH + lay.secH + 8;
    const tw = t => lay.mc.measureText(t).width;
    ctx.font = '11px ui-monospace, monospace'; ctx.textAlign = 'left';
    const head = t => { if (!measure) { ctx.fillStyle = '#8a97a6'; ctx.fillText(t, x0, y + 12); } };
    const flow = (list, draw, gap) => { let x = col; for (const it of list) { const w = tw(it.text) + gap; if (x + w > x0 + W && x > col) { x = col; y += lh; } if (!measure) draw(it, x, y); x += w; } y += lh; };
    const swatch = (it, x, yy) => { ctx.fillStyle = rgba(it.color, 1); ctx.fillRect(x, yy + 3, 12, 10); ctx.fillStyle = '#e6edf3'; ctx.fillText(it.text, x + 16, yy + 12); };
    const line = (it, x, yy) => { ctx.strokeStyle = '#e6edf3'; ctx.lineWidth = 2; ctx.setLineDash(it.dash || []); ctx.beginPath(); ctx.moveTo(x, yy + 8); ctx.lineTo(x + 28, yy + 8); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = '#e6edf3'; ctx.fillText(it.text, x + 34, yy + 12); };
    const text = (it, x, yy) => { ctx.fillStyle = '#e6edf3'; ctx.fillText(it.text, x, yy + 12); };
    // scale bar: a nice length in metres, its width from the frame's own scale
    head('SCALE'); const bar = niceStep(lay.F.L / 5); const bw = bar * lay.F.scale;
    if (!measure) { ctx.fillStyle = '#e6edf3'; ctx.fillRect(col, y + 6, bw, 3); ctx.fillRect(col, y + 2, 1, 11); ctx.fillRect(col + bw - 1, y + 2, 1, 11); ctx.fillText(`${fmtNum(bar, 0)} m · ${fmtNum(bar / E.FT, 0)} ft   (VE ×${lay.F.ve.toFixed(1)}: vertical scale ×${lay.F.ve.toFixed(1)})`, col + bw + 8, y + 12); }
    y += lh;
    if (K.units.length) { head('UNITS'); flow(K.units.map(u => ({ text: u.name, color: u.color })), swatch, 30); }
    if (K.types.length) { head('WORKINGS'); flow(K.types.map(t => ({ text: `${t.type} (${t.n})`, color: t.color })), swatch, 30); head('CONFIDENCE'); flow(CONF_CLASSES.map(c => ({ text: `${c.label} ${K.conf[c.key]}`, dash: c.dash })), line, 48); }
    for (const b of K.blocks) {
      head('BLOCKS');
      if (b.cats) flow(b.cats.map((c, i) => ({ text: c, color: GM.colormap(b.cmap, b.cats.length > 1 ? i / (b.cats.length - 1) : 0.5) })).concat([{ text: `${b.attr} · ${b.name}`, color: [0, 0, 0] }]), swatch, 30);
      else { if (!measure) { for (let i = 0; i < 40; i++) { ctx.fillStyle = rgba(GM.colormap(b.cmap, i / 39), 1); ctx.fillRect(col + i * 3, y + 3, 3.2, 10); } ctx.fillStyle = '#e6edf3'; ctx.fillText(`${fmtNum(b.lo, 3)} – ${fmtNum(b.hi, 3)}  ${b.attr} · ${b.name} · ${b.cmap}${b.cutoff != null ? ` · cut-off ≥ ${fmtNum(b.cutoff, 3)}` : ''}`, col + 128, y + 12); } y += lh; }
    }
    if (K.images.length) { head('IMAGES'); flow(K.images.map(n => ({ text: n + ' (drawn under the products)' })), text, 20); }
    if (K.sources.length) { head('SOURCES'); flow(K.sources.map(s => ({ text: s })), text, 20); }
    if (K.notSurvey) { if (!measure) { ctx.fillStyle = '#ffd27a'; ctx.font = 'bold 12px ui-monospace, monospace'; ctx.fillText(NOT_A_SURVEY, x0, y + 13); ctx.font = '11px ui-monospace, monospace'; } y += lh + 2; }
    if (!measure) { ctx.fillStyle = '#5c6b7a'; ctx.fillText(`${this.app.project.name} · NW Mineral Monitor 3-D modeller · ${new Date().toISOString().slice(0, 10)} · described geometry inherits the accuracy of the text it was read from`, x0, y + 12); }
    y += lh;
    return y + 6;
  }
  /** Header + drawing + key, sized from the key's contents. */
  exportLayout(mc, K, W = 1200) {
    const pl = this.plane(); if (!pl) throw new Error('no section to export');
    const pad = { l: 64, r: 20, t: 14, b: 34 }; const headH = 48; const secH = Math.round(W * 0.42);
    const F = this.frame(pl, W, secH, pad); const s = this.sec;
    const lay = { W, headH, secH, pad, F, mc, H: 0 };
    lay.title = `${s.name}${this.offset ? ` — offset ${fmtNum(this.offset, 0)} m ⟂` : ''}`;
    lay.subtitle = `${fmtNum(F.L, 0)} m long · azimuth ${fmtNum(azimuthOf(s), 0)}° · looking ${lookDir(F.u)} · VE ×${F.ve.toFixed(1)} · z ${fmtNum(F.zmin, 0)} – ${fmtNum(F.zmax, 0)} m · band ±${this.band} m${K.oblique.length ? ' · image plane oblique to this section — shown in 3-D only' : ''}`;
    mc.font = '11px ui-monospace, monospace';
    lay.H = Math.ceil(this.drawKey(mc, K, lay, true));
    return lay;
  }
  compose(ctx, lay, K) {
    ctx.fillStyle = '#0b0e13'; ctx.fillRect(0, 0, lay.W, lay.H);
    ctx.textAlign = 'left'; ctx.fillStyle = '#e6edf3'; ctx.font = 'bold 14px ui-monospace, monospace'; ctx.fillText(lay.title, 16, 22);
    ctx.fillStyle = '#8a97a6'; ctx.font = '11px ui-monospace, monospace'; ctx.fillText(lay.subtitle, 16, 40);
    ctx.save(); ctx.translate(0, lay.headH); this.paint(ctx, lay.W, lay.secH, { title: false, pad: lay.pad }); ctx.restore();
    this.drawKey(ctx, K, lay, false);
  }
  async prepareExport(W) {
    if (!this.sec) throw new Error('no section — pick or draw one first');
    const pl = this.plane(); const K = this.keyModel();
    for (const ip of this.sectionImages(pl, this.frame(pl, 100, 100, { l: 0, r: 0, t: 0, b: 0 })).on) this.imageFor(ip);
    await this.imagesReady();
    const mc = document.createElement('canvas').getContext('2d');
    return { K, lay: this.exportLayout(mc, K, W) };
  }
  /** PNG at 2×: the drawing, a title line, scale bar, key, sources and — when
      any working on the section was described or assumed — the sentence
      that says it is not a survey.  Returns the canvas. */
  async exportPng(o = {}) {
    try {
      const scale = o.scale || 2; const { K, lay } = await this.prepareExport(o.width || 1200);
      const c = document.createElement('canvas'); c.width = lay.W * scale; c.height = lay.H * scale; const ctx = c.getContext('2d'); ctx.scale(scale, scale);
      this.compose(ctx, lay, K);
      if (o.download !== false) { c.toBlob(b => GM.downloadBlob(b, `${GM.slug(this.sec.name)}-section.png`), 'image/png'); toast(`PNG ${c.width}×${c.height} exported`, 'ok'); }
      return c;
    } catch (e) { toast('PNG export failed: ' + e.message, 'err', 6000); if (o.download === false) throw e; return null; }
  }
  /** The same drawing as vectors (polylines, ribbon polygons, text; rasters
      only for block slices and section images). Returns the SVG string. */
  async exportSvg(o = {}) {
    try {
      const { K, lay } = await this.prepareExport(o.width || 1200);
      const ctx = new SvgCtx(lay.W, lay.H); this.compose(ctx, lay, K); const svg = ctx.toString();
      if (o.download !== false) { GM.downloadBlob(new Blob([svg], { type: 'image/svg+xml' }), `${GM.slug(this.sec.name)}-section.svg`); toast('SVG exported', 'ok'); }
      return svg;
    } catch (e) { toast('SVG export failed: ' + e.message, 'err', 6000); if (o.download === false) throw e; return null; }
  }
}
function niceStep(x) { const p = Math.pow(10, Math.floor(Math.log10(Math.max(x, 1e-9)))); const m = x / p; return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p; }
function lookDir(u) { const az = Math.atan2(u[0], u[1]) * 180 / Math.PI; const a = (az + 360) % 360; return `${['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(((a + 90) % 360) / 45) % 8]} (section runs ${['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(a / 45) % 8]}-ward)`; }
function azimuthOf(s) { return ((Math.atan2(s.end[0] - s.start[0], s.end[1] - s.start[1]) * 180 / Math.PI) % 360 + 360) % 360; }
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

/* ========================================================== WORKINGS */
export class WorkingsTool {
  constructor(T) { this.T = T; this.ws = null; this.mode = null; this.pts = []; this.image = null; this.form = { type: 'drift', name: '', level: '', level_z: '', units_in: 'ft', confidence: 'sketched', doc: '', page: '', width_m: '', bearing: 0, length: 100, depth: 100, dip: 90, azimuth: 0, grade: 0.5, zTop: '', zBottom: '' }; }
  get app() { return this.T.app; }
  onProject() { this.ws = null; this.image = null; this.mode = null; }
  /* Two phases: find() is what the panel uses and never creates anything;
     ensureLayer() creates the layer on the first committed feature.  Opening
     the panel used to add an empty 'Workings (digitised)' row, mark the
     project dirty and autosave it — a layer nobody asked for. */
  find() { if (this.ws && this.app.project.get(this.ws.id)) return this.ws; this.ws = this.app.project.byKind('lineset').find(l => l.role === 'workings') || null; return this.ws; }
  ensureLayer() { if (this.find()) return this.ws; this.ws = E.newWorkings('Workings (digitised)', this.app.project.name); this.ws.group = 'Workings'; this.app.project.add(this.ws); return this.ws; }
  panel(wsArg, image) {
    if (wsArg) this.ws = wsArg; this.find(); if (image) this.image = image;
    const f = this.form; const P = h('div', { class: 'tool' }, h('h2', {}, 'WORKINGS FROM MAPS'));
    const wsList = this.app.project.byKind('lineset').filter(l => l.role === 'workings'); const images = this.app.project.byKind('imageplane');
    if (!this.image && images.length && !this._imageAsked) { this.image = images[images.length - 1]; if (this.image.plane === 'plan' && this.image.elevation != null) f.level_z = this.image.elevation; }
    P.appendChild(row('layer', sel([...(this.ws ? [] : [['', '— new layer on first feature —']]), ...wsList.map(l => [l.id, l.name])], this.ws ? this.ws.id : '', { onchange: e => { this.ws = this.app.project.get(e.target.value) || null; } }), btn('+', () => { const l = E.newWorkings(`Workings ${wsList.length + 1}`, this.app.project.name); l.group = 'Workings'; this.app.project.add(l); this.ws = l; this.T.showPanel(this.panel()); }, { title: 'new workings layer' })));
    P.appendChild(row('trace on', sel([['', 'ground / any surface'], ...images.map(i => [i.id, `${i.name} (${i.plane}${i.elevation != null ? ' @' + fmtNum(i.elevation, 0) : ''})`])], this.image ? this.image.id : '', { onchange: e => { this._imageAsked = true; this.image = e.target.value ? this.app.project.get(e.target.value) : null; if (this.image && this.image.plane === 'plan' && this.image.elevation != null) { f.level_z = this.image.elevation; } this.T.showPanel(this.panel()); } })));
    if (this.image) P.appendChild(note(`tracing on "${this.image.name}" — clicks are read through its georeference${this.image.plane === 'plan' ? ' at the level elevation below' : ''}`));
    else P.appendChild(h('div', { class: 'note' }, 'No plan selected: clicks land on the topography / surfaces, so a trace here is a sketch, never a survey. ', btn('GEOREFERENCE A PLAN…', () => this.T.open('georef'), { class: 'x' }), ' or draw straight on the ground with the buttons below.'));
    const modes = [['trace', 'TRACE (drift / crosscut / level)'], ['adit', 'ADIT (portal + bearing + length)'], ['adit2', 'ADIT (click portal, click end)'], ['shaft', 'SHAFT / WINZE (collar + depth)'], ['raise', 'RAISE (click lower, click upper)'], ['stope', 'STOPE (outline + z range)']];
    P.appendChild(h('div', { class: 'modes' }, ...modes.map(([m, lbl]) => btn(lbl, () => this.start(m), { class: this.mode === m ? 'on' : '' }))));
    const frm = h('div', { class: 'psec' }, h('h3', {}, 'FEATURE'));
    frm.appendChild(row('type', sel(Object.keys(E.WORKING_TYPES).filter(t => t !== 'portal'), f.type, { onchange: e => { f.type = e.target.value; } })));
    frm.appendChild(row('name', txt(f.name, { placeholder: 'No. 2 adit / 300 level drift', oninput: e => { f.name = e.target.value; } })));
    frm.appendChild(row('level label', txt(f.level, { placeholder: '300', oninput: e => { f.level = e.target.value; } }), num(f.level_z, { placeholder: 'elev m', style: { width: '80px' }, oninput: e => { f.level_z = e.target.value; } })));
    frm.appendChild(row('map units', sel([['ft', 'feet'], ['m', 'metres']], f.units_in, { title: 'Old US maps are in feet: keep feet and every typed length converts once, at the door.', onchange: e => { f.units_in = e.target.value; } }), sel(E.CONFIDENCE.map(c => [c, c + (c === 'surveyed' ? ' — off a georeferenced plan' : c === 'sketched' ? ' — drawn by hand' : c === 'inferred' ? ' — deduced' : ' — read off a text')]), f.confidence, { title: 'how sure the geometry is: surveyed draws solid, sketched dotted, described dashed', onchange: e => { f.confidence = e.target.value; if (f.confidence === 'surveyed' && !this.image) toast('"surveyed" means traced off a georeferenced plan — pick a plan under "trace on", or keep sketched', 'warn', 6000); } })));
    frm.appendChild(row('source', txt(f.doc, { placeholder: 'document / plate', oninput: e => { f.doc = e.target.value; } }), txt(f.page, { placeholder: 'page', style: { width: '60px' }, oninput: e => { f.page = e.target.value; } })));
    frm.appendChild(row('opening w (m)', num(f.width_m, { placeholder: 'auto', oninput: e => { f.width_m = e.target.value; } })));
    const geo = h('div', { class: 'psec' }, h('h3', {}, 'GEOMETRY (for adit / shaft / stope)'));
    geo.appendChild(row('bearing °', num(f.bearing, { oninput: e => { f.bearing = +e.target.value; } }), h('span', { class: 'mono' }, 'length'), num(f.length, { oninput: e => { f.length = +e.target.value; } }), h('span', { class: 'mono' }, f.units_in)));
    geo.appendChild(row('grade %', num(f.grade, { oninput: e => { f.grade = +e.target.value; } }), h('span', { class: 'mono' }, 'depth'), num(f.depth, { oninput: e => { f.depth = +e.target.value; } })));
    geo.appendChild(row('dip °', num(f.dip, { oninput: e => { f.dip = +e.target.value; } }), h('span', { class: 'mono' }, 'azimuth'), num(f.azimuth, { oninput: e => { f.azimuth = +e.target.value; } })));
    geo.appendChild(row('stope z', num(f.zBottom, { placeholder: 'bottom', oninput: e => { f.zBottom = e.target.value; } }), num(f.zTop, { placeholder: 'top', oninput: e => { f.zTop = e.target.value; } })));
    P.appendChild(frm); P.appendChild(geo);
    if (this.mode) P.appendChild(h('div', { class: 'frow' }, btn('UNDO LAST POINT (Backspace)', () => { this.pts.pop(); this.preview(); }), btn('FINISH (Enter / double-click)', () => this.finish(), { class: 'primary' }), btn('CANCEL (Esc)', () => this.cancel())));
    // feature list: name, level, confidence sample, length; hover highlights
    // the part in the scene, click zooms to it, ✕ deletes with UNDO
    const ws = this.ws;
    const list = h('div', { class: 'psec' }, h('h3', {}, ws ? `FEATURES (${ws.parts.length}) · ${fmtNum(ws.length(), 0)} m` : 'FEATURES (none yet)'));
    if (ws) {
      const tally = { surveyed: 0, described: 0, assumed: 0 }; ws.features.forEach(ft => { tally[confClass(ft)]++; });
      if (ws.features.length) list.appendChild(h('div', { class: 'keyrow' }, ...CONF_CLASSES.filter(c => tally[c.key]).map(c => h('span', { title: c.hint }, lineSample(c.dash), ` ${c.label} ${tally[c.key]}  `))));
      ws.features.forEach((ft, k) => {
        const cc = CONF_CLASSES.find(c => c.key === confClass(ft));
        list.appendChild(h('div', { class: 'feat', title: `${ft.type}${ft.level ? ' · level ' + ft.level : ''} · ${cc.label} — ${cc.hint}${ft.source && (ft.source.doc || ft.source.document) ? ' · ' + (ft.source.doc || ft.source.document) + (ft.source.page ? ' p. ' + ft.source.page : '') : ''}`,
          onmouseenter: () => this.app.R.highlight && this.app.R.highlight(ws, k), onmouseleave: () => this.app.R.highlight && this.app.R.highlight(null),
          onclick: () => { const pts = ws.partXYZ(k); const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]), zs = pts.map(p => p[2]); this.app.R.fitTo([Math.min(...xs) - 30, Math.min(...ys) - 30, Math.min(...zs) - 30, Math.max(...xs) + 30, Math.max(...ys) + 30, Math.max(...zs) + 30]); } },
          h('span', { class: 'sw', style: { background: rgba((E.WORKING_TYPES[ft.type] || E.WORKING_TYPES.unknown).color, 1) } }),
          h('span', { class: 'fn' }, ft.name || ft.type, ft.name && ft.name !== ft.type ? h('span', { class: 'fl' }, ' ' + ft.type) : null, ft.level ? h('span', { class: 'fl' }, ' · L' + ft.level) : null),
          lineSample(cc.dash), h('span', { class: 'fl' }, fmtNum(ws.length(k), 0) + ' m'),
          btn('✕', e => { e.stopPropagation(); const pts = ws.partXYZ(k), feat = ws.features[k]; this.app.destructive(`removed ${feat.name || feat.type}`, () => { ws.removePart(k); this.app.refresh(ws); this.T.showPanel(this.panel()); }, () => { ws.addPolyline(pts, feat); this.app.refresh(ws); this.T.showPanel(this.panel()); }); }, { class: 'x', title: 'remove this working (undo from the toast)' })));
      });
      const stopes = this.app.project.byKind('mesh').filter(m => m.role === 'stope'); if (stopes.length) list.appendChild(note(`${stopes.length} stope volume${stopes.length > 1 ? 's are' : ' is'} listed under WORKINGS in the layer tree as mesh layers`));
    } else list.appendChild(note('Nothing digitised yet. The layer is created with the first feature you commit.'));
    P.appendChild(list);
    if (ws && ws.parts.length) P.appendChild(h('div', { class: 'frow' }, btn('SEND FOOTPRINT TO MAP', () => this.sendToMap(ws)), btn('EXPORT DXF', () => this.app.exportObjects('dxf', [ws])), btn('EXPORT GEOJSON', () => this.app.exportObjects('geojson', [ws]))));
    return P;
  }
  start(mode) {
    this.mode = mode; this.pts = []; this.app.R.clearOverlay();
    const what = { trace: 'TRACE — click along the working · Enter or double-click finishes · Backspace undoes a point · Esc cancels', adit: 'ADIT — click the portal on the surface (bearing + length from the form) · Esc cancels', adit2: 'ADIT — click the portal, then the inner end · Esc cancels', shaft: 'SHAFT — click the collar (depth + dip from the form) · Esc cancels', raise: 'RAISE — click the lower end, then the upper end · Esc cancels', stope: 'STOPE — click the outline corners · Enter closes · Esc cancels' }[mode];
    const f = this.form; const target = `${this.ws ? this.ws.name : 'new workings layer'} as ${f.confidence}${this.image ? ` on ${this.image.name}` : ' on the ground'}`;
    this.T.arm(this, what, target); this.T.showPanel(this.panel());
  }
  pickPoint(e) {
    const R = this.app.R;
    if (this.image) { // intersect the ray with the image plane
      const ip = this.image; const c = ip.corners(); const P0 = R.toScene(c[0][0], c[0][1], c[0][2]); P0.y *= R.ve; const P1 = R.toScene(c[1][0], c[1][1], c[1][2]); P1.y *= R.ve; const P2 = R.toScene(c[3][0], c[3][1], c[3][2]); P2.y *= R.ve;
      const nrm = new THREE.Vector3().subVectors(P1, P0).cross(new THREE.Vector3().subVectors(P2, P0)).normalize(); const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(nrm, P0);
      const w = R.pickPlane(e.clientX, e.clientY, plane); if (!w) return null;
      if (ip.plane === 'plan') { const z = this.form.level_z !== '' ? +this.form.level_z : (ip.elevation != null ? ip.elevation : w[2]); return [w[0], w[1], z]; }
      return w;
    }
    return groundPick(R, e);
  }
  onClick(e) {
    if (!this.mode) return false; const w = this.pickPoint(e); if (!w) return true;
    const f = this.form; const ws = this.ensureLayer(); const topo = this.app.topoGrid();
    // `name` deliberately does NOT live in `common`: every branch below passes
    // `common` as the *source* of Object.assign, so a common.name of '' (the
    // form default until the user types) overwrote each branch's `|| 'adit'`
    // fallback and every unnamed working was stored nameless.
    const common = { level: f.level, level_z: f.level_z === '' ? null : +f.level_z, mine: this.app.project.name, source: f.doc ? { doc: f.doc, page: f.page } : {}, confidence: f.confidence, units_in: f.units_in, width_m: f.width_m === '' ? undefined : +f.width_m };
    if (this.mode === 'adit') { E.addAdit(ws, w, f.bearing, f.length, Object.assign({ gradePct: f.grade, unitsIn: f.units_in, terrain: topo, name: f.name || 'adit' }, common)); this.done(); return true; }
    if (this.mode === 'shaft') { E.addShaft(ws, w, f.depth, Object.assign({ dipDeg: f.dip, azimuthDeg: f.azimuth, unitsIn: f.units_in, terrain: f.type === 'winze' ? null : topo, name: f.name || (f.type === 'winze' ? 'winze' : 'shaft'), kind: f.type === 'winze' ? 'winze' : 'shaft' }, common)); this.done(); return true; }
    this.pts.push(w); this.preview();
    if (this.mode === 'adit2' && this.pts.length === 2) { const [a, b] = this.pts; const zA = topo ? (topo.sample(a[0], a[1]) || a[2]) : a[2]; ws.addPolyline([[a[0], a[1], zA], [b[0], b[1], zA + Math.hypot(b[0] - a[0], b[1] - a[1]) * f.grade / 100]], E.workingFeature('adit', f.name || 'adit', Object.assign({}, common, { units_in: 'm' }))); this.done(); return true; }
    if (this.mode === 'raise' && this.pts.length === 2) { E.addRaise(ws, this.pts[0], this.pts[1], Object.assign({ name: f.name || 'raise', kind: f.type === 'winze' ? 'winze' : 'raise' }, common)); this.done(); return true; }
    return true;
  }
  onDblClick(e) { if (this.mode === 'trace' || this.mode === 'stope') { this.finish(); return true; } return false; }
  onKey(e) { if (!this.mode) return false; if (e.key === 'Enter') { this.finish(); return true; } if (e.key === 'Escape') { this.cancel(); return true; } if (e.key === 'Backspace') { this.pts.pop(); this.preview(); return true; } return false; }
  onMove(e) { if (!this.mode || !this.pts.length) return false; const w = this.pickPoint(e); if (!w) return true; this.preview(w); return true; }
  preview(cursor) { const R = this.app.R; R.clearOverlay(); const col = (E.WORKING_TYPES[this.form.type] || E.WORKING_TYPES.unknown).color; const hex = (col[0] << 16) | (col[1] << 8) | col[2]; for (const p of this.pts) R.overlayMarker(p, hex, 8); const pts = cursor ? this.pts.concat([cursor]) : this.pts; if (pts.length > 1) R.overlayPolyline(pts, hex, !!cursor); if (this.mode === 'stope' && pts.length > 2) R.overlayPolyline([pts[pts.length - 1], pts[0]], hex, true); }
  finish() {
    const f = this.form; const ws = this.ensureLayer();
    if (this.mode === 'trace' && this.pts.length >= 2) { const z = f.level_z !== '' ? +f.level_z : null; const pts = z != null && this.image && this.image.plane === 'plan' ? this.pts.map(p => [p[0], p[1], z]) : this.pts; ws.addPolyline(pts, E.workingFeature(f.type, f.name, { level: f.level, level_z: z, mine: this.app.project.name, source: f.doc ? { doc: f.doc, page: f.page } : {}, confidence: f.confidence, units_in: f.units_in, width_m: f.width_m === '' ? undefined : +f.width_m })); }
    else if (this.mode === 'stope' && this.pts.length >= 3) { const zb = f.zBottom !== '' ? +f.zBottom : Math.min(...this.pts.map(p => p[2])) - 10, zt = f.zTop !== '' ? +f.zTop : Math.max(...this.pts.map(p => p[2])); try { const m = E.stopePrism(this.pts.map(p => [p[0], p[1]]), zb, zt, { name: f.name || `stope ${this.app.project.byKind('mesh').filter(x => x.role === 'stope').length + 1}`, level: f.level, mine: this.app.project.name, source: f.doc ? { doc: f.doc, page: f.page } : {}, confidence: f.confidence }); m.group = 'Workings'; this.app.project.add(m); } catch (e) { toast(e.message, 'err'); } }
    this.done();
  }
  done() { this.pts = []; this.app.R.clearOverlay(); this.app.refresh(this.ws); this.T.showPanel(this.panel()); this.app.status(`${this.ws.parts.length} workings, ${fmtNum(this.ws.length(), 0)} m — ${this.mode ? 'again, or Esc' : ''}`); }
  cancel() { this.mode = null; this.pts = []; this.app.R.clearOverlay(); this.T.disarm(); this.T.showPanel(this.panel()); }
  stop() { this.mode = null; this.pts = []; }
  async sendToMap(ws) {
    try { const fc = E.workingsToGeoJSON(ws, this.app.project.crs); if (!fc.features.length) return toast('no workings yet', 'warn'); const slug = 'workings-' + GM.slug(this.app.project.name); await GM.store.putUserLayer({ slug, name: `${this.app.project.name} workings (3-D model)`, added: new Date().toISOString().slice(0, 10), n: fc.features.length, misses: 0, visible: true, fc }); toast(`sent ${fc.features.length} workings to the map — reload the map and look under MY DATA`, 'ok', 7000); }
    catch (e) { toast('send failed: ' + e.message, 'err'); }
  }
}

/* ============================================================ GEOREF */
export class GeorefTool {
  constructor(T) { this.T = T; this.img = null; this.markers = []; this.mode = null; this.pending = null; this.plane = 'plan'; this.zTop = ''; this.zBottom = ''; this.elev = ''; this.name = ''; this.existing = null; }
  get app() { return this.T.app; }
  onProject() { this.img = null; this.markers = []; this.existing = null; }
  async fromImageBytes(name, bytes) { const blob = new Blob([bytes]); const url = URL.createObjectURL(blob); const bmp = await createImageBitmap(blob); const c = document.createElement('canvas'); const scale = Math.min(1, 4096 / Math.max(bmp.width, bmp.height)); c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale); c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height); URL.revokeObjectURL(url); this.setImage(name, c); }
  async fromPdf(name, bytes) {
    try { const pdfjs = await import('../pdfjs/pdf.min.mjs'); pdfjs.GlobalWorkerOptions.workerSrc = 'assets/pdfjs/pdf.worker.min.mjs'; const doc = await pdfjs.getDocument({ data: bytes }).promise; const pageNo = Math.max(1, Math.min(doc.numPages, +(prompt(`PDF has ${doc.numPages} pages — which page holds the map?`, '1') || 1))); const page = await doc.getPage(pageNo); const vp = page.getViewport({ scale: 1 }); const scale = Math.min(3, 3000 / Math.max(vp.width, vp.height)); const v2 = page.getViewport({ scale }); const c = document.createElement('canvas'); c.width = Math.round(v2.width); c.height = Math.round(v2.height); await page.render({ canvasContext: c.getContext('2d'), viewport: v2 }).promise; this.setImage(`${name} p${pageNo}`, c); }
    catch (e) { toast('PDF render failed (is assets/pdfjs present?): ' + e.message, 'err', 8000); }
  }
  // `existing` binds the panel to a plane opened for editing, and only commit()
  // or cancel clears it: panel() is re-entered with no argument on every
  // re-render, so it cannot do the clearing.  A newly loaded image is always a
  // *new* plane, so it has to be released here — otherwise commit() rewrites the
  // previously opened plane with this plate's control points while keeping its
  // old raster and pixel size, and this image is never added at all.
  setImage(name, canvas) { this.img = { name, canvas, dataUrl: canvas.toDataURL('image/jpeg', 0.85), width: canvas.width, height: canvas.height }; this.markers = []; this.existing = null; this.name = name.replace(/\.[^.]+$/, ''); this.T.open('georef'); }
  panel(existing) {
    if (existing) { this.existing = existing; this.img = { name: existing.name, dataUrl: existing.image, width: existing.width, height: existing.height, canvas: null }; this.plane = existing.plane; this.elev = existing.elevation == null ? '' : existing.elevation; this.zTop = existing.zTop == null ? '' : existing.zTop; this.zBottom = existing.zBottom == null ? '' : existing.zBottom; this.name = existing.name; this.markers = existing.plane === 'plan' ? existing.control.map(c => ({ px: c[0], py: c[1], X: c[2], Y: c[3] })) : [{ px: 0, py: 0, X: existing.p1[0], Y: existing.p1[1] }, { px: existing.width, py: 0, X: existing.p2[0], Y: existing.p2[1] }]; }
    const P = h('div', { class: 'tool' }, h('h2', {}, 'GEOREFERENCE AN IMAGE'));
    if (!this.img) { P.appendChild(note('Drop a PNG/JPG/PDF of a level plan, longitudinal section or geologic map onto the page, or:')); P.appendChild(btn('CHOOSE IMAGE / PDF…', () => $('fileIn').click())); P.appendChild(note('A level plan becomes a horizontal plane at its level elevation; a section becomes a vertical plane between two surface points. Then trace workings on it (TOOLS > Workings).')); return P; }
    P.appendChild(row('name', txt(this.name, { oninput: e => { this.name = e.target.value; } })));
    P.appendChild(row('kind', sel([['plan', 'PLAN (horizontal: level plan, map)'], ['section', 'SECTION (vertical: long / cross section)']], this.plane, { onchange: e => { this.plane = e.target.value; this.markers = []; this.T.showPanel(this.panel()); } })));
    if (this.plane === 'plan') P.appendChild(row('elevation m', num(this.elev, { placeholder: 'level elevation (blank = just above the terrain)', title: 'a level plan belongs at its level elevation; left blank it is placed 5 m above the highest corner of the terrain, flat', oninput: e => { this.elev = e.target.value; } })));
    else { P.appendChild(row('z top / bottom', num(this.zTop, { placeholder: 'top edge m', oninput: e => { this.zTop = e.target.value; } }), num(this.zBottom, { placeholder: 'bottom edge m', oninput: e => { this.zBottom = e.target.value; } }))); }
    // image canvas with markers
    const box = h('div', { class: 'imgbox' }); const view = document.createElement('canvas'); const W = 300, scale = W / this.img.width; view.width = W; view.height = Math.round(this.img.height * scale); box.appendChild(view);
    const draw = () => { const ctx = view.getContext('2d'); ctx.clearRect(0, 0, view.width, view.height); const im = new Image(); im.onload = () => { ctx.drawImage(im, 0, 0, view.width, view.height); this.markers.forEach((m, i) => { ctx.fillStyle = ['#ff5252', '#ffd740', '#69f0ae'][i % 3]; ctx.beginPath(); ctx.arc(m.px * scale, m.py * scale, 6, 0, Math.PI * 2); ctx.fill(); ctx.fillStyle = '#000'; ctx.font = 'bold 10px monospace'; ctx.fillText(String(i + 1), m.px * scale - 3, m.py * scale + 4); }); }; im.src = this.img.dataUrl; };
    view.onclick = e => { if (this.plane === 'section' && this.markers.length >= 2) return; const r = view.getBoundingClientRect(); const px = (e.clientX - r.left) / scale, py = (e.clientY - r.top) / scale; this.markers.push({ px, py, X: '', Y: '' }); draw(); this.T.showPanel(this.panel()); };
    draw(); P.appendChild(box);
    P.appendChild(note(this.plane === 'plan' ? 'Click 2+ points on the image (a known corner, a shaft collar, a section line crossing, a lat/long tick) and type their world coordinates — or PICK them in the 3-D scene.' : 'Click the TOP-LEFT and TOP-RIGHT ends of the section on the image and give their map coordinates; the top/bottom elevations stretch it vertically.'));
    this.markers.forEach((m, i) => {
      const fill = (X, Y) => { m.X = X; m.Y = Y; this.T.showPanel(this.panel()); };
      P.appendChild(h('div', { class: 'mk' }, h('span', { class: 'mkn', style: { background: ['#ff5252', '#ffd740', '#69f0ae'][i % 3] } }, String(i + 1)), h('span', { class: 'mono' }, `px ${m.px.toFixed(0)},${m.py.toFixed(0)}`),
        num(m.X, { placeholder: 'E (or lon)', oninput: e => { m.X = e.target.value; } }), num(m.Y, { placeholder: 'N (or lat)', oninput: e => { m.Y = e.target.value; } }),
        btn('PICK', () => { this.mode = 'pick'; this.pending = m; this.T.arm(this, `PICK marker ${i + 1} — click the spot in the scene where it is · Esc cancels`, this.name || 'image'); }, { title: 'click the spot in the 3-D scene' }), btn('✕', () => { this.markers.splice(i, 1); this.T.showPanel(this.panel()); }, { class: 'x' })));
    });
    if (this.plane === 'plan' && this.markers.filter(m => m.X !== '' && m.Y !== '').length >= 3) { const q = this.quality(); if (q) P.appendChild(note(`fit: ${fmtNum(q.mpp, 3)} m / pixel · control points disagree by up to ${fmtNum(q.maxResidual, 1)} m (RMS ${fmtNum(q.rms, 1)} m)${q.maxResidual > 20 ? ' — a marker is probably tied wrongly' : ''}`, q.maxResidual > 20 ? 'note warn' : 'note ok')); }
    P.appendChild(h('div', { class: 'frow' }, btn(this.existing ? 'UPDATE IMAGE PLANE' : 'CREATE IMAGE PLANE', () => this.commit(), { class: 'primary' }), btn('SCALE BAR + ONE POINT…', () => this.scaleBar()), btn('cancel', () => { this.existing = null; this.T.close(); })));
    return P;
  }
  /** With 3+ control points a plan georeference is over-determined: fit the
      affine transform by least squares and report how far the points disagree
      (the Python mapplate does the same), so a mistyped coordinate is visible
      before workings land in the wrong place. */
  quality() {
    const ms = this.markers.filter(m => m.X !== '' && m.Y !== '').map(m => { const [X, Y] = this.toUTM(m.X, m.Y); return [m.px, m.py, X, Y]; });
    if (ms.length < 3) return null;
    const A = ms.map(([px, py]) => [px, py, 1]); const solve = rhs => { const AtA = [[0, 0, 0], [0, 0, 0], [0, 0, 0]], Atb = [0, 0, 0]; for (let i = 0; i < A.length; i++) for (let r = 0; r < 3; r++) { Atb[r] += A[i][r] * rhs[i]; for (let c = 0; c < 3; c++) AtA[r][c] += A[i][r] * A[i][c]; } return E.solveDense(AtA, Atb); };
    let ax, ay; try { ax = solve(ms.map(m => m[2])); ay = solve(ms.map(m => m[3])); } catch (e) { return null; }
    let sum = 0, max = 0; for (const [px, py, X, Y] of ms) { const dx = ax[0] * px + ax[1] * py + ax[2] - X, dy = ay[0] * px + ay[1] * py + ay[2] - Y; const d = Math.hypot(dx, dy); sum += d * d; max = Math.max(max, d); }
    return { rms: Math.sqrt(sum / ms.length), maxResidual: max, mpp: Math.hypot(ax[0], ay[0]) };
  }
  onClick(e) { if (this.mode !== 'pick' || !this.pending) return false; const w = groundPick(this.app.R, e); if (!w) return true; this.pending.X = +w[0].toFixed(2); this.pending.Y = +w[1].toFixed(2); if (this.plane === 'section') { if (this.zTop === '') this.zTop = +w[2].toFixed(1); } else if (this.elev === '' && this.markers.indexOf(this.pending) === 0) { /* keep drape */ } this.mode = null; this.pending = null; this.T.disarm(); this.T.showPanel(this.panel()); return true; }
  onKey(e) { if (e.key === 'Escape' && this.mode) { this.mode = null; this.pending = null; this.T.disarm(); this.T.showPanel(this.panel()); return true; } return false; }
  stop() { this.mode = null; this.pending = null; }
  toUTM(X, Y) { X = +X; Y = +Y; if (GM.utm.looksLonLat(X, Y) && this.app.project.crs.kind === 'utm' && Math.abs(X) > 1) return GM.utm.fwd(X, Y, this.app.project.crs.zone, this.app.project.crs.north); return [X, Y]; }
  scaleBar() {
    const body = h('div', {}, note('When the map only has a scale bar: click one point on the image that you can locate (marker 1 with E/N filled), then give metres per pixel (measure the scale bar: bar length ÷ its pixel length) and the rotation of the image "up" direction clockwise from north.'));
    const mpp = num('', { placeholder: 'm per pixel' }), rot = num(0, { placeholder: 'rotation °' });
    body.appendChild(row('m / pixel', mpp)); body.appendChild(row('rotation °', rot));
    body.appendChild(btn('APPLY', () => { const m0 = this.markers[0]; if (!m0 || m0.X === '' || m0.Y === '') return toast('marker 1 needs coordinates', 'warn'); const [X, Y] = this.toUTM(m0.X, m0.Y); const ip = new GM.ImagePlane({ image: this.img.dataUrl, width: this.img.width, height: this.img.height, plane: 'plan', name: this.name }); E.georefPlanFromScale(ip, [m0.px, m0.py], [X, Y], +mpp.value, { rotationDeg: +rot.value, elevation: this.elev === '' ? null : +this.elev }); this.markers = ip.control.map(c => ({ px: c[0], py: c[1], X: c[2], Y: c[3] })); mdl.close(); this.T.showPanel(this.panel()); }));
    const mdl = modal('SCALE-BAR GEOREFERENCE', body);
  }
  commit() {
    try {
      const ms = this.markers.filter(m => m.X !== '' && m.Y !== '');
      let ip;
      if (this.plane === 'plan') { if (ms.length < 2) throw new Error('need at least 2 markers with coordinates'); const control = ms.map(m => { const [X, Y] = this.toUTM(m.X, m.Y); return [m.px, m.py, X, Y]; }); ip = this.existing || new GM.ImagePlane({ image: this.img.dataUrl, width: this.img.width, height: this.img.height, plane: 'plan', name: this.name }); ip.plane = 'plan'; ip.control = control; ip.elevation = this.elev === '' ? null : +this.elev; if (ip.elevation == null) { const topo = this.app.topoGrid(); if (topo) { const c = ip.corners(); const zs = c.map(q => topo.sample(q[0], q[1])).filter(z => z === z); ip.elevation = zs.length ? Math.max(...zs) + 5 : 0; ip.warn('no level elevation given: placed just above the terrain'); } else ip.elevation = 0; } }
      else { if (ms.length < 2) throw new Error('need the two top corners with coordinates'); if (this.zTop === '' || this.zBottom === '') throw new Error('need top and bottom elevations'); const [X1, Y1] = this.toUTM(ms[0].X, ms[0].Y), [X2, Y2] = this.toUTM(ms[1].X, ms[1].Y); ip = this.existing || new GM.ImagePlane({ image: this.img.dataUrl, width: this.img.width, height: this.img.height, plane: 'section', name: this.name }); ip.plane = 'section'; ip.p1 = [X1, Y1]; ip.p2 = [X2, Y2]; ip.zTop = +this.zTop; ip.zBottom = +this.zBottom; }
      ip.name = this.name || ip.name; ip.group = 'Images'; ip.opacity = ip.opacity == null ? 0.9 : ip.opacity;
      if (this.existing) { this.app.refresh(ip); toast('image plane updated', 'ok'); } else { this.app.project.add(ip); toast(`image plane created${ip.plane === 'plan' && this.elev === '' ? ' — no level elevation given, so it sits 5 m above the terrain; set one to place it at its level' : ''} — now trace workings on it (step 2)`, ip.plane === 'plan' && this.elev === '' ? 'warn' : 'ok', 7000); }
      const b = ip.bounds(); if (b) this.app.R.fitTo(b);
      this.existing = null; this.T.open('workings', null, ip);
    } catch (e) { toast(e.message, 'err'); }
  }
}

/* ============================================================= STRAT */
/** Is (x, y) inside a Grid2D's footprint (rotation honoured, as sample() does)? */
function inGrid(g, x, y) {
  let u, v; if (g.rotation) { const r = -g.rotation * Math.PI / 180, c = Math.cos(r), s = Math.sin(r), px = x - g.x0, py = y - g.y0; u = px * c - py * s; v = px * s + py * c; } else { u = x - g.x0; v = y - g.y0; }
  const fi = u / g.dx, fj = v / g.dy; return fi >= -1e-9 && fj >= -1e-9 && fi <= g.nx - 1 + 1e-9 && fj <= g.ny - 1 + 1e-9;
}
/** Bucket contact inputs before a surface is built through them (GEOMODEL.md
    §4 and the Model From Map course both warn about it).  A point with no
    elevation — z NaN, or exactly 0 on ground that is nowhere near sea level —
    is a map click that was never draped; a point above the ground is a
    section digitised in the air; a point far below the lowest ground is
    almost always a depth, or a feet value, typed as an elevation.  Nothing is
    repaired here: the counts are shown, DRAPE is offered for the first kind,
    and a unit with too little left is refused rather than given a default.
    Returns counts plus the usable row indices, their distinct-XY count and
    their XY span in lattice cells. */
export function classifyInputs(xyz, lattice, topo, opts = {}) {
  const n = Math.floor(xyz.length / 3); const tr = topo ? topo.zrange() : [NaN, NaN]; const tmin = tr[0];
  const floor = opts.floor != null ? opts.floor : (tmin === tmin ? tmin - 1000 : -Infinity);
  const out = { n, no_elevation: 0, outside_lattice: 0, above_topography: 0, below_floor: 0, usable: 0, distinct: 0, span_cells: 0, usable_index: [], floor };
  const seen = new Set(); let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (let i = 0; i < n; i++) {
    const x = xyz[3 * i], y = xyz[3 * i + 1], z = xyz[3 * i + 2];
    if (z !== z || (z === 0 && tmin === tmin && tmin > 0)) { out.no_elevation++; continue; }
    if (lattice && !inGrid(lattice, x, y)) { out.outside_lattice++; continue; }
    const t = topo ? topo.sample(x, y) : NaN;
    if (t === t && z > t + 5) { out.above_topography++; continue; }
    if (z < floor) { out.below_floor++; continue; }
    out.usable++; out.usable_index.push(i); seen.add(`${Math.round(x * 100)},${Math.round(y * 100)}`);
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  out.distinct = seen.size; out.span_cells = out.usable && lattice ? Math.max(x1 - x0, y1 - y0) / Math.max(lattice.dx, 1e-9) : 0;
  return out;
}
function subsetPoints(ps, idx) { const out = new GM.PointSet({ name: ps.name, role: ps.role, color: ps.color, provenance: ps.provenance, metadata: ps.metadata }); for (const i of idx) { const a = {}; for (const [k, c] of Object.entries(ps.attributes)) a[k] = c[i]; out.add(ps.xyz[3 * i], ps.xyz[3 * i + 1], ps.xyz[3 * i + 2], a); } return out; }
/** Points every `step` metres along a polyline (by arc length), ends included. */
function resampleByLength(xy, step) { const out = []; if (!xy.length) return out; let acc = 0, next = 0; out.push([xy[0][0], xy[0][1]]); for (let k = 0; k + 1 < xy.length; k++) { const a = xy[k], b = xy[k + 1]; const L = Math.hypot(b[0] - a[0], b[1] - a[1]); if (!L) continue; while (next + step <= acc + L) { next += step; const t = (next - acc) / L; out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]); } acc += L; } const last = xy[xy.length - 1]; const o = out[out.length - 1]; if (Math.hypot(last[0] - o[0], last[1] - o[1]) > step * 0.25) out.push([last[0], last[1]]); return out; }
const cls_text = c => `${c.n} points · ${c.usable} usable (${c.distinct} distinct XY, ${c.span_cells.toFixed(1)} lattice cells) · ${c.no_elevation} no elevation · ${c.outside_lattice} outside lattice · ${c.above_topography} above ground · ${c.below_floor} below floor`;

export class StratTool {
  constructor(T) { this.T = T; this.sm = null; this.busy = false; this.mode = null; this.keepPrev = false; }
  get app() { return this.T.app; }
  onProject() { this.sm = null; }
  /* find() never creates; ensure() creates the model on the first unit. */
  find() { if (this.sm && this.app.project.get(this.sm.id)) return this.sm; this.sm = this.app.project.byKind('stratmodel')[0] || null; return this.sm; }
  ensure() { if (this.find()) return this.sm; const topo = this.app.topoGrid(); this.sm = new GM.StratModel({ name: 'Stratigraphy (pancake)', topography: topo ? topo.id : null }); this.app.project.add(this.sm); return this.sm; }
  /** Mapped unit boundaries that can stand in for contact points.  Faults are
      left out on purpose: a fault is a discontinuity, not a contact. */
  traceLayers() { return this.app.project.objects.filter(o => o.kind === 'lineset' && (o.role === 'geology-outline' || o.role === 'lines') && o.parts.length); }
  /** The lattice the build interpolates on: the topography thinned to <= res
      nodes per side.  spec-only (no values) is enough to classify inputs. */
  lattice(topo, fill = true) {
    const res = this.res || 60; const stride = Math.max(1, Math.ceil(Math.max(topo.nx, topo.ny) / res));
    const lat = new GM.Grid2D({ nx: Math.ceil(topo.nx / stride), ny: Math.ceil(topo.ny / stride), x0: topo.x0, y0: topo.y0, dx: topo.dx * stride, dy: topo.dy * stride, rotation: topo.rotation, name: 'lattice' });
    if (fill) for (let j = 0; j < lat.ny; j++) for (let i = 0; i < lat.nx; i++) { const [x, y] = lat.nodeXY(i, j); lat.values[j * lat.nx + i] = topo.sample(x, y); }
    return lat;
  }
  /** A mapped unit boundary as contact points: resampled every lattice cell
      along the trace, z re-sampled from the topography (outlines are lifted
      +2 m for display), and vertices on the model box edge dropped — those
      are clipRingRect artefacts, not contacts. */
  traceToContacts(ls, lat, topo) {
    const ps = new GM.PointSet({ name: `${ls.name} (trace contacts)`, role: 'contacts', color: ls.color });
    const edge = { x0: topo.x0 + topo.dx, x1: topo.xmax - topo.dx, y0: topo.y0 + topo.dy, y1: topo.ymax - topo.dy }; let dropped = 0;
    ls.parts.forEach((part, k) => {
      const xy = part.map(i => { const v = ls.vertex(i); return [v[0], v[1]]; });
      for (const [x, y] of resampleByLength(xy, Math.max(lat.dx, 1))) { if (x <= edge.x0 || x >= edge.x1 || y <= edge.y0 || y >= edge.y1) { dropped++; continue; } const z = topo.sample(x, y); if (z !== z) { dropped++; continue; } ps.add(x, y, z, { part: k }); }
    });
    ps.metadata.dropped_edge = dropped; ps.metadata.derived_from = [ls.id, topo.id]; return ps;
  }
  /** The classification of one unit's contact input (points or trace), or
      null when it has none. */
  classify(u, topo, lat) {
    const src = u.source || {}; if (!(src.kind === 'points' || src.kind === 'trace') || !src.id) return null;
    const o = this.app.project.get(src.id); if (!o) return null;
    if (src.kind === 'trace') { if (o.kind !== 'lineset') return null; const ps = this.traceToContacts(o, lat, topo); const c = classifyInputs(ps.xyz, lat, topo); c.dropped_edge = ps.metadata.dropped_edge; return c; }
    if (o.kind !== 'points') return null; return classifyInputs(o.xyz, lat, topo);
  }
  panel(sm) {
    if (sm) this.sm = sm; const S = this.find() || new GM.StratModel({ name: 'Stratigraphy (pancake)' }); const P = h('div', { class: 'tool' }, h('h2', {}, 'STRATIGRAPHY — PANCAKE MODEL'));
    const topo = this.app.topoGrid(); if (!topo) { P.appendChild(note('needs a topography grid (import one, or open the model from a mine card)', 'note warn')); return P; }
    const pointLayers = this.app.project.byKind('points'), gridLayers = this.app.project.byKind('grid2d').filter(g => g.role !== 'topography' && g.role !== 'property'), traceLayers = this.traceLayers();
    const lat = this.lattice(topo, false);
    P.appendChild(note('Units are listed youngest (top) first. Each unit needs the surface at its BASE: contact points (interpolated), a mapped unit boundary (a heightfield through the draped trace), an existing surface grid, or a constant elevation. The last unit is basement (no base). Deposit bases on-lap older units; erosion bases cut them.'));
    const list = h('div', { class: 'units' });
    if (!S.units.length) list.appendChild(note('No units yet — + UNIT starts the model. Contact points come from DIGITISE CONTACT POINTS, an imported points layer, a mapped outline, or a surface grid.'));
    S.units.forEach((u, i) => {
      const src = u.source || { kind: u.base ? 'grid' : 'none', id: u.base || '', value: '' };
      u.source = src;
      const srcSel = sel([['none', 'basement (no base)'], ['points', 'contact points layer'], ['trace', 'contact from a mapped trace (outline / line)'], ['grid', 'surface grid'], ['const', 'constant elevation'], ['thickness', 'constant thickness below unit above']], src.kind, { onchange: e => { src.kind = e.target.value; if (src.kind === 'trace' && !traceLayers.some(t => t.id === src.id)) src.id = traceLayers[0] ? traceLayers[0].id : ''; if (src.kind === 'points' && !pointLayers.some(t => t.id === src.id)) src.id = pointLayers[0] ? pointLayers[0].id : ''; this.T.showPanel(this.panel()); } });
      const idSel = src.kind === 'points' ? sel(pointLayers.map(l => [l.id, l.name]), src.id, { onchange: e => { src.id = e.target.value; this.T.showPanel(this.panel()); } })
        : src.kind === 'trace' ? sel(traceLayers.length ? traceLayers.map(l => [l.id, `${l.name} (${l.parts.length})`]) : [['', '— no mapped outlines or lines —']], src.id, { onchange: e => { src.id = e.target.value; this.T.showPanel(this.panel()); } })
        : src.kind === 'grid' ? sel(gridLayers.map(l => [l.id, l.name]), src.id, { onchange: e => { src.id = e.target.value; } })
        : (src.kind === 'const' || src.kind === 'thickness') ? num(src.value, { placeholder: src.kind === 'const' ? 'elevation m' : 'thickness m', oninput: e => { src.value = e.target.value; } }) : null;
      const box = h('div', { class: 'unit' },
        h('div', { class: 'frow' }, colorInput(u.color || E.DEFAULT_COLORS[i % E.DEFAULT_COLORS.length], c => { u.color = c; }), txt(u.name, { placeholder: 'unit name', oninput: e => { u.name = e.target.value; } }), sel([['deposit', 'deposit'], ['erosion', 'erosion']], u.contact || 'deposit', { onchange: e => { u.contact = e.target.value; } }), btn('▲', () => { if (i > 0) { S.units.splice(i - 1, 2, S.units[i], S.units[i - 1]); this.T.showPanel(this.panel()); } }, { class: 'x' }), btn('▼', () => { if (i < S.units.length - 1) { S.units.splice(i, 2, S.units[i + 1], S.units[i]); this.T.showPanel(this.panel()); } }, { class: 'x' }), btn('✕', () => { S.units.splice(i, 1); this.T.showPanel(this.panel()); }, { class: 'x' })),
        h('div', { class: 'frow' }, srcSel, idSel, txt(u.lithology || '', { placeholder: 'lithology', oninput: e => { u.lithology = e.target.value; } })));
      if (src.kind === 'trace') box.appendChild(note('A single draped outcrop trace fixes the contact where the line is and nothing else: the surface through it carries no dip information away from the line. Fault layers are not offered — a fault is a discontinuity, not a contact.', 'note'));
      // what the build will actually see, before it is asked to
      let c = null; try { c = this.classify(u, topo, lat); } catch (e) { c = null; }
      if (c) {
        const refused = c.distinct < 3 || c.span_cells < 2; const bad = c.n - c.usable;
        const nt = note(cls_text(c) + (c.dropped_edge ? ` · ${c.dropped_edge} on the box edge dropped` : '') + (refused ? ' — too little to build a surface through: the unit will be refused' : bad ? ' — the rest are left out; no default elevation is substituted' : ''), refused ? 'note warn' : bad ? 'note warn' : 'note ok');
        if (c.no_elevation && src.kind === 'points') nt.appendChild(btn('DRAPE', () => this.drape(u), { class: 'x', title: 'set the elevation of the points that have none from the topography (z_original is kept; the others are untouched)' }));
        box.appendChild(nt);
      }
      list.appendChild(box);
    });
    P.appendChild(list);
    P.appendChild(h('div', { class: 'frow' }, btn('+ UNIT', () => { const M = this.ensure(); M.units.push({ name: `Unit ${M.units.length + 1}`, color: E.DEFAULT_COLORS[M.units.length % E.DEFAULT_COLORS.length], contact: 'deposit', source: { kind: M.units.length ? 'const' : 'points', id: pointLayers[0] ? pointLayers[0].id : '', value: '' } }); this.T.showPanel(this.panel()); }), btn('+ BASEMENT', () => { const M = this.ensure(); M.units.push({ name: 'Basement', color: [140, 140, 140], contact: 'deposit', source: { kind: 'none' } }); this.T.showPanel(this.panel()); }), btn('DIGITISE CONTACT POINTS', () => this.digitise())));
    const method = sel([['rbf', 'RBF (thin-plate, linear drift)'], ['ok', 'ordinary kriging (auto variogram)'], ['idw', 'inverse distance'], ['nn', 'nearest']], this.method || 'rbf', { onchange: e => { this.method = e.target.value; } });
    const res = num(this.res || 60, { style: { width: '70px' }, onchange: e => { this.res = +e.target.value; this.T.showPanel(this.panel()); } });
    P.appendChild(row('interpolation', method)); P.appendChild(row('lattice nodes', res, h('span', { class: 'mono' }, `× per side · cell ${fmtNum(lat.dx, 0)} m`)));
    P.appendChild(row('outputs', h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.volumes !== false, onchange: e => { this.volumes = e.target.checked; } }), 'unit volumes (closed meshes)'), h('label', { class: 'chk', title: 'the previous bases and volumes are kept, hidden, renamed "(previous)" and tagged superseded instead of removed' }, h('input', { type: 'checkbox', checked: !!this.keepPrev, onchange: e => { this.keepPrev = e.target.checked; } }), 'keep previous outputs')));
    P.appendChild(h('div', { class: 'frow' }, btn(this.busy ? 'BUILDING…' : 'BUILD STRATIGRAPHY', () => this.build(), { disabled: this.busy || !S.units.length, class: 'primary', title: S.units.length ? '' : 'add a unit first' }), btn('VIRTUAL DRILLHOLE', () => this.virtualHole(), { disabled: !S.metadata.built, title: S.metadata.built ? 'click the ground for the column' : 'build the stratigraphy first' }), btn('TAG BLOCK MODEL', () => this.tagBlocks(), { disabled: !S.metadata.built })));
    const outs = this.app.project.objects.filter(o => o.metadata && o.metadata.strat_of === S.id); const cur = outs.filter(o => !o.metadata.superseded), prev = outs.filter(o => o.metadata.superseded);
    if (S.metadata.built) P.appendChild(note(`current set: built ${S.metadata.built.at} · ${S.metadata.built.units} units · lattice ${S.metadata.built.lattice} · ${cur.filter(o => o.kind === 'grid2d').length} base surfaces, ${cur.filter(o => o.kind === 'mesh').length} volumes${prev.length ? ` · ${prev.length} previous output${prev.length > 1 ? 's' : ''} kept hidden, tagged superseded` : ''} — open a section (step 9) to see the pancake fill; VIRTUAL DRILLHOLE reports the column anywhere`, 'note ok'));
    if (this.last && this.last.warnings && this.last.warnings.length) this.last.warnings.forEach(w => P.appendChild(note(w, 'note warn')));
    return P;
  }
  /** Leapfrog's Set Elevation, for the points that have none: their z comes
      from the topography (z_original kept); points that already had an
      elevation are not touched.  Undoable. */
  drape(u) {
    const ps = this.app.project.get(u.source.id); const topo = this.app.topoGrid(); if (!ps || !topo) return;
    const tmin = topo.zrange()[0]; const noZ = new Set(); for (let i = 0; i < ps.n; i++) { const z = ps.xyz[3 * i + 2]; if (z !== z || (z === 0 && tmin === tmin && tmin > 0)) noZ.add(i); }
    if (!noZ.size) return toast('every point already has an elevation', 'info');
    const before = Float64Array.from(ps.xyz); const keep = (ps.attributes.z_original || []).slice(); let moved = 0;
    this.app.destructive(`draped ${noZ.size} point(s) of ${ps.name} onto ${topo.name}`, () => {
      ST.setElevationFromGrid(ps, topo); moved = 0;
      for (let i = 0; i < ps.n; i++) { if (noZ.has(i)) { if (ps.xyz[3 * i + 2] !== before[3 * i + 2]) moved++; continue; } ps.xyz[3 * i + 2] = before[3 * i + 2]; ps.attributes.z_original[i] = keep[i] == null ? null : keep[i]; }
      this.app.refresh(ps); this.T.showPanel(this.panel());
      toast(`${moved} point(s) draped; ${noZ.size - moved} outside the grid still have no elevation`, moved ? 'ok' : 'warn');
    }, () => { ps.xyz = Float64Array.from(before); if (keep.length) ps.attributes.z_original = keep; else delete ps.attributes.z_original; this.app.refresh(ps); this.T.showPanel(this.panel()); });
  }
  digitise() {
    // append to the existing contact layer rather than minting a new one per
    // press; a new layer only when there is none yet
    let ps = this.target && this.app.project.get(this.target.id) ? this.target : this.app.project.byKind('points').filter(p => p.role === 'contacts').pop() || null;
    if (!ps) { ps = new GM.PointSet({ name: 'contact points', role: 'contacts', group: 'Stratigraphy', color: [255, 120, 200] }); this.app.project.add(ps); }
    this.mode = 'digitise'; this.target = ps; this.T.arm(this, 'CONTACT POINTS — click contacts on the terrain, section images or surfaces · Esc stops', `${ps.name} (${ps.n} so far)`); this.T.showPanel(this.panel());
  }
  // Both of this tool's click modes live here on the prototype.  virtualHole()
  // used to install its own `onClick` on the instance, which shadowed this one
  // for good — after one virtual drillhole, digitising contacts was dead.
  onClick(e) {
    if (this.mode === 'vhole') { const w = groundPick(this.app.R, e); if (!w) return true; this.showColumn(w); return true; }
    if (this.mode !== 'digitise') return false;
    const w = groundPick(this.app.R, e); if (!w) return true;
    this.target.add(w[0], w[1], w[2], { n: this.target.n + 1 }); this.app.refresh(this.target); this.app.status(`${this.target.n} contact points in ${this.target.name}`); return true;
  }
  onKey(e) { if (e.key === 'Escape' && this.mode) { this.mode = null; this.T.disarm(); this.T.showPanel(this.panel()); return true; } return false; }
  stop() { this.mode = null; }
  async build() {
    const S = this.ensure(); const topo = this.app.topoGrid(); if (!topo) return; this.busy = true; this.T.showPanel(this.panel());
    try {
      // lattice: downsample topography to <= res nodes per side
      const lat = this.lattice(topo);
      const units = []; let prevBase = null; const inputs = [];   // per unit: what fed it, what to record on its outputs
      for (const u of S.units) {
        const src = u.source || {}; let base = null; const info = { ids: [topo.id], provenance: null, warnings: [], cls: null };
        if (src.kind === 'points' || src.kind === 'trace') {
          const o = this.app.project.get(src.id); if (!o) throw new Error(`unit ${u.name}: ${src.kind === 'trace' ? 'trace' : 'points'} layer missing`);
          info.ids.push(o.id); let ps = o;
          if (src.kind === 'trace') {
            if (o.kind !== 'lineset') throw new Error(`unit ${u.name}: ${o.name} is not a lineset`);
            if (o.role === 'faults') throw new Error(`unit ${u.name}: ${o.name} is a fault layer — a fault is a discontinuity, not a contact`);
            ps = this.traceToContacts(o, lat, topo);
            info.provenance = { method: 'contact from map trace', source_layer: o.name, source_id: o.id, confidence: 'inferred' };
            info.warnings.push('a heightfield through a single draped outcrop trace carries no dip information away from the line');
          } else if (o.kind !== 'points') throw new Error(`unit ${u.name}: ${o.name} is not a points layer`);
          const cls = classifyInputs(ps.xyz, lat, topo); info.cls = cls;
          if (cls.distinct < 3 || cls.span_cells < 2) throw new Error(`unit ${u.name}: ${cls.distinct} usable contact point${cls.distinct === 1 ? '' : 's'} spanning ${cls.span_cells.toFixed(1)} lattice cells (${cls.no_elevation} without elevation, ${cls.outside_lattice} outside the lattice, ${cls.above_topography} above the ground, ${cls.below_floor} below the floor) — drape or add points; no default elevation is substituted`);
          const left = cls.n - cls.usable;
          if (left) { info.warnings.push(`${left} of ${cls.n} input points were left out (${cls.no_elevation} without elevation, ${cls.outside_lattice} outside the lattice, ${cls.above_topography} above the ground, ${cls.below_floor} below the floor) — no default elevation was substituted`); ps = subsetPoints(ps, cls.usable_index); }
          base = GM.packObject(ps);
        }
        else if (src.kind === 'grid') { const g = this.app.project.get(src.id); if (!g) throw new Error(`unit ${u.name}: grid missing`); info.ids.push(g.id); base = GM.packObject(g); }
        else if (src.kind === 'const') base = +src.value;
        else if (src.kind === 'thickness') { const th = +src.value; const above = prevBase || GM.packObject(lat); const g = GM.unpackObject(above instanceof Object && above.kind ? above : GM.packObject(lat)); const gg = g.copyEmpty(); for (let k = 0; k < gg.values.length; k++) gg.values[k] = g.values[k] - th; base = GM.packObject(gg); }
        units.push({ name: u.name, color: u.color, lithology: u.lithology, contact: u.contact || 'deposit', base }); inputs.push(info);
        if (base && typeof base === 'object') prevBase = base;
      }
      const r = await this.app.engine.call('buildStratigraphy', { topo: GM.packObject(lat), units, method: this.method || 'rbf' }, (f, n) => this.app.status(`building stratigraphy ${(f * 100) | 0}% ${n || ''}`));
      // previous outputs of this model: removed, or kept hidden and marked
      // superseded so a rebuild never silently overwrites what was compared
      const old = this.app.project.objects.filter(o => o.metadata && o.metadata.strat_of === S.id);
      if (this.keepPrev) { for (const o of old) { if (!o.metadata.superseded) { o.metadata.superseded = true; o.metadata.superseded_at = new Date().toISOString().slice(0, 16); o.name += ' (previous)'; } o.visible = false; this.app.R.setVisible(o.id, false); } }
      else for (const o of old) this.app.project.remove(o);
      const stamp = new Date().toISOString().slice(0, 16);
      const bases = r.bases; const strat = r.strat; const grids = {};
      bases.forEach((g, i) => {
        if (!g) return; const info = inputs[i] || { ids: [topo.id], warnings: [] };
        g.group = 'Stratigraphy'; g.metadata.strat_of = S.id; g.name = `${S.units[i].name} base`; g.color = S.units[i].color;
        g.metadata.derived_from = info.ids.slice(); g.metadata.built_at = stamp;
        if (info.provenance) g.provenance = Object.assign({}, g.provenance, info.provenance);
        if (info.cls) { const c = info.cls; g.metadata.inputs = { n: c.n, usable: c.usable, distinct: c.distinct, span_cells: +c.span_cells.toFixed(2), no_elevation: c.no_elevation, outside_lattice: c.outside_lattice, above_topography: c.above_topography, below_floor: c.below_floor }; }
        for (const w of info.warnings) g.warn(w);
        this.app.project.add(g); grids[g.id] = g;
      });
      S.units.forEach((u, i) => { u.base = bases[i] ? bases[i].id : null; u.contact = strat.units[i].contact; });
      S.topography = topo.id; S.metadata.built = { at: stamp, units: S.units.length, lattice: `${lat.nx}×${lat.ny}` }; S.metadata.lattice_id = null;
      if (this.volumes !== false) { const vols = await this.app.engine.call('stratigraphyVolumes', { strat: GM.packObject(S), grids: Object.fromEntries(Object.entries(grids).map(([k, g]) => [k, GM.packObject(g)])), topo: GM.packObject(r.topo || lat) }); vols.forEach((m, i) => { m.group = 'Stratigraphy'; m.metadata.strat_of = S.id; m.opacity = 0.9; m.metadata.built_at = stamp; m.metadata.derived_from = [topo.id, ...(i > 0 && S.units[i - 1].base ? [S.units[i - 1].base] : []), ...(S.units[i] && S.units[i].base ? [S.units[i].base] : [])]; if (i === vols.length - 1 && S.units[i] && !S.units[i].base) m.visible = false; this.app.project.add(m); }); }
      this.last = { warnings: inputs.flatMap(x => x.warnings) };
      this.app.refresh(S); this.T.section.update(); toast('stratigraphy built — open a section to see the pancake fill', 'ok', 5000);
    } catch (e) { console.error(e); toast('build failed: ' + e.message, 'err', 8000); }
    this.busy = false; this.T.showPanel(this.panel());
  }
  virtualHole() { if (this.T.active !== this) this.T.open('strat'); this.mode = 'vhole'; this.T.arm(this, 'VIRTUAL DRILLHOLE — click the ground for the column · Esc stops', 'a report, no geometry'); }
  showColumn(w) {
    const S = this.ensure(); const topo = this.app.topoGrid(); const grids = {}; for (const u of S.units) if (u.base) { const g = this.app.project.get(u.base); if (g) grids[u.base] = g; }
    const col = Object.keys(grids).length ? E.columnAt(S, grids, w[0], w[1], topo) : [];
    const body = h('div', {}, kv([['E / N', `${fmtNum(w[0], 1)} / ${fmtNum(w[1], 1)}`], ['Surface', fmtNum(topo ? topo.sample(w[0], w[1]) : w[2], 1) + ' m']]),
      col.length ? h('table', { class: 'tbl' }, h('tr', {}, h('th', {}, 'unit'), h('th', {}, 'top'), h('th', {}, 'base'), h('th', {}, 'thickness')), ...col.map(c => h('tr', {}, h('td', {}, h('span', { class: 'sw', style: { background: rgba(c.color, 1) } }), ' ' + c.name), h('td', {}, fmtNum(c.top, 1)), h('td', {}, c.base == null ? '—' : fmtNum(c.base, 1)), h('td', {}, c.thickness == null ? 'basement' : fmtNum(c.thickness, 1))))) : note('no built stratigraphy yet'));
    // block model values at the column
    for (const bm of this.app.project.byKind('blockmodel')) { const rows = []; for (let k = bm.count[2] - 1; k >= 0; k--) { const c = bm.centroid(0, 0, k); const i = Math.floor((w[0] - bm.origin[0]) / bm.blockSize[0]), j = Math.floor((w[1] - bm.origin[1]) / bm.blockSize[1]); if (i < 0 || j < 0 || i >= bm.count[0] || j >= bm.count[1]) break; const idx = bm.index(i, j, k); const vals = Object.entries(bm.attributes).map(([n, a]) => `${n}=${typeof a.values[idx] === 'number' ? fmtNum(a.values[idx], 3) : a.values[idx]}`).join(' '); rows.push(`${fmtNum(c[2], 0)} m: ${vals}`); } if (rows.length) body.appendChild(section(bm.name, ...rows.slice(0, 40).map(r => note(r)))); }
    const ls = new GM.LineSet({ name: 'virtual drillhole', role: 'lines', color: [255, 255, 255] }); ls.addPolyline([[w[0], w[1], (topo ? topo.sample(w[0], w[1]) : w[2]) + 20], [w[0], w[1], (col.length && col[col.length - 1].top) ? col[col.length - 1].top - 100 : w[2] - 300]]); this.app.R.clearOverlay(); this.app.R.overlayPolyline(ls.partXYZ(0), 0xffffff);
    modal('VIRTUAL DRILLHOLE', body);
  }
  async tagBlocks() { const S = this.ensure(); const bms = this.app.project.byKind('blockmodel'); if (!bms.length) return toast('no block model yet (TOOLS > Block model)', 'warn'); const topo = this.app.topoGrid(); const grids = {}; for (const u of S.units) if (u.base) { const g = this.app.project.get(u.base); if (g) grids[u.base] = GM.packObject(g); } if (!Object.keys(grids).length) return toast('build the stratigraphy first', 'warn'); for (const bm of bms) { const r = await this.app.engine.call('tagBlockModel', { bm: GM.packObject(bm), strat: GM.packObject(S), grids, topo: GM.packObject(topo) }, f => this.app.status(`tagging ${(f * 100) | 0}%`)); bm.addAttribute('unit', r.attributes.unit.values, 'category'); this.app.refresh(bm); } toast('blocks tagged with units — use it as a domain in kriging', 'ok'); }
}

/* ============================================================ BLOCKS */
const GRADE_RE = /^(au|ag|cu|pb|zn|grade|value)$/i;
const SIDE_RE = /^(sd|side|hw|fw|pos|neg)$/i;
/** Points layers that can honestly feed an estimate: at least one numeric
    column, never claims (a centroid list carries no value).  Assay-style
    layers first, then mines with a cited grade column, then the rest. */
export function sampleLayers(P) {
  const rank = p => p.role === 'samples' ? 0 : p.role === 'mines' && Object.keys(p.attributes).some(k => GRADE_RE.test(k) && p.isNumeric(k)) ? 1 : 2;
  return P.byKind('points').filter(p => p.role !== 'claims' && p.n > 0 && Object.keys(p.attributes).some(k => p.isNumeric(k))).sort((a, b) => rank(a) - rank(b));
}
/** Points layers an implicit surface can be fitted through: a numeric
    (signed-distance) column, a side-like column, or contact points. */
export function implicitLayers(P) { return P.byKind('points').filter(p => p.role !== 'claims' && p.n > 0 && (p.role === 'contacts' || Object.keys(p.attributes).some(k => p.isNumeric(k) || SIDE_RE.test(k)))); }
const NO_SAMPLES = '✗ samples with a numeric value — import an assay CSV or open the model from a mine card with cited grades';

export class BlocksTool {
  constructor(T) { this.T = T; this.bm = null; this.params = { size: 25, zsize: 10, method: 'ok', maxPoints: 16, radius: '', minPoints: 2, power: 2, nugget: 0, sill: 1, range: 300, model: 'spherical', nLags: 12, lagSize: '', value: '', samples: '', domain: '', domainValue: '' }; this.exp = null; this.vg = null; this.busy = false; }
  get app() { return this.T.app; }
  onProject() { this.bm = null; this.exp = null; this.vg = null; }
  panel(bm) {
    if (bm) this.bm = bm; const p = this.params; const P = h('div', { class: 'tool' }, h('h2', {}, 'BLOCK MODEL & KRIGING'));
    const bms = this.app.project.byKind('blockmodel'); if (!this.bm) this.bm = bms[0] || null;
    // 1. grid
    const g = h('div', { class: 'psec' }, h('h3', {}, '1 · BLOCK GRID'));
    g.appendChild(row('model', sel([['', '— new —'], ...bms.map(b => [b.id, b.name])], this.bm ? this.bm.id : '', { onchange: e => { this.bm = e.target.value ? this.app.project.get(e.target.value) : null; this.T.showPanel(this.panel()); } })));
    g.appendChild(row('block xy / z (m)', num(p.size, { oninput: e => { p.size = +e.target.value; } }), num(p.zsize, { oninput: e => { p.zsize = +e.target.value; } })));
    g.appendChild(row('extent', sel([['view', 'around the site (half radius, 400 m deep)'], ['all', 'whole model bounds'], ['samples', 'around the sample layer (+10%)']], this.extent || 'view', { onchange: e => { this.extent = e.target.value; } })));
    g.appendChild(h('div', { class: 'frow' }, btn('CREATE BLOCK MODEL', () => this.create())));
    if (this.bm) g.appendChild(kv([['Blocks', `${this.bm.count.join(' × ')} = ${this.bm.n.toLocaleString()}`], ['Attributes', Object.keys(this.bm.attributes).join(', ') || '—']]));
    P.appendChild(g);
    // 2. samples + variogram — only layers that carry a value are offered
    const s = h('div', { class: 'psec' }, h('h3', {}, '2 · SAMPLES & VARIOGRAM'));
    const pl = sampleLayers(this.app.project); const ps = pl.find(l => l.id === p.samples) || pl[0] || null; p.samples = ps ? ps.id : '';
    const why = pl.length ? '' : 'no points layer with a numeric value — import assays or cited grades first';
    if (!pl.length) s.appendChild(note(NO_SAMPLES, 'note warn'));
    s.appendChild(row('samples', sel(pl.length ? pl.map(l => [l.id, `${l.name} (${l.n}${l.role === 'samples' ? ', assays' : l.role === 'mines' ? ', cited grades' : ''})`]) : [['', '— none —']], p.samples, { disabled: !pl.length, title: why, onchange: e => { p.samples = e.target.value; p.value = ''; this.exp = null; this.T.showPanel(this.panel()); } })));
    const cols = ps ? Object.keys(ps.attributes).filter(k => ps.isNumeric(k)) : []; if (ps && !p.value) p.value = cols.find(c => GRADE_RE.test(c) || /^(v|z)$/i.test(c)) || cols[0] || '';
    s.appendChild(row('value column', sel(cols.length ? cols : [['', '—']], p.value, { disabled: !cols.length, onchange: e => { p.value = e.target.value; this.exp = null; this.T.showPanel(this.panel()); } })));
    s.appendChild(row('lags / size', num(p.nLags, { oninput: e => { p.nLags = +e.target.value; } }), num(p.lagSize, { placeholder: 'auto', oninput: e => { p.lagSize = e.target.value; } }), btn('COMPUTE', () => this.variogram(), { disabled: !pl.length, title: why })));
    const cv = document.createElement('canvas'); cv.width = 300; cv.height = 170; cv.className = 'vplot'; s.appendChild(cv); this.canvas = cv;
    s.appendChild(row('model', sel(E.VARIOGRAM_MODELS.filter(m => m !== 'nugget'), p.model, { onchange: e => { p.model = e.target.value; this.plot(); } }), btn('AUTO-FIT', () => this.fit(), { disabled: !pl.length, title: why || (this.exp ? '' : 'compute the experimental variogram first') })));
    s.appendChild(row('nugget', num(p.nugget, { oninput: e => { p.nugget = +e.target.value; this.plot(); } }), h('span', { class: 'mono' }, 'sill'), num(p.sill, { oninput: e => { p.sill = +e.target.value; this.plot(); } }), h('span', { class: 'mono' }, 'range'), num(p.range, { oninput: e => { p.range = +e.target.value; this.plot(); } })));
    s.appendChild(row('anisotropy', num(p.aniso || '', { placeholder: 'major/minor ratio', oninput: e => { p.aniso = e.target.value; } }), num(p.anisoAz || 0, { placeholder: 'azimuth °', oninput: e => { p.anisoAz = +e.target.value; } }), num(p.anisoZ || '', { placeholder: 'vert ratio', oninput: e => { p.anisoZ = e.target.value; } })));
    P.appendChild(s);
    // 3. estimate
    const est = h('div', { class: 'psec' }, h('h3', {}, '3 · ESTIMATE'));
    est.appendChild(row('method', sel([['ok', 'ordinary kriging'], ['idw', 'inverse distance'], ['nn', 'nearest neighbour']], p.method, { onchange: e => { p.method = e.target.value; } })));
    est.appendChild(row('search', num(p.maxPoints, { title: 'max points', oninput: e => { p.maxPoints = +e.target.value; } }), h('span', { class: 'mono' }, 'pts'), num(p.radius, { placeholder: 'radius m', oninput: e => { p.radius = e.target.value; } }), num(p.minPoints, { title: 'min points', oninput: e => { p.minPoints = +e.target.value; } }), h('span', { class: 'mono' }, 'min')));
    est.appendChild(row('IDW power', num(p.power, { oninput: e => { p.power = +e.target.value; } })));
    const cats = this.bm ? Object.entries(this.bm.attributes).filter(([k, a]) => a.type !== 'number') : [];
    if (cats.length) { const catVals = p.domain && this.bm.attributes[p.domain] ? [...new Set(this.bm.attributes[p.domain].values.filter(v => v))] : []; est.appendChild(row('domain', sel([['', 'all blocks'], ...cats.map(([k]) => k)], p.domain, { onchange: e => { p.domain = e.target.value; this.T.showPanel(this.panel()); } }), catVals.length ? sel(catVals, p.domainValue || catVals[0], { onchange: e => { p.domainValue = e.target.value; } }) : null)); }
    est.appendChild(h('div', { class: 'frow' }, btn(this.busy ? 'RUNNING…' : 'RUN ESTIMATE', () => this.run(), { disabled: this.busy || !this.bm || !pl.length, title: why || (this.bm ? '' : 'create a block model first') }), btn('GRADE–TONNAGE', () => this.gt(), { disabled: !this.bm })));
    P.appendChild(est);
    P.appendChild(note('Ordinary kriging with a moving neighbourhood; the variogram is fitted by weighted grid search (or set it by hand). Estimates go to <value>_est, kriging variance to <value>_var. Restrict to one stratigraphic unit with a domain (tag the block model from the Stratigraphy tool).'));
    if (this.exp) this.plot();
    return P;
  }
  create() {
    const p = this.params; let b;
    if (this.extent === 'all' || !this.app.project.site || this.app.project.site.lon == null) b = this.app.project.bounds();
    else if (this.extent === 'samples') { const ps = this.app.project.get(p.samples); b = ps && ps.bounds(); if (b) { const pad = Math.max(b[3] - b[0], b[4] - b[1]) * 0.1; b = [b[0] - pad, b[1] - pad, b[2] - pad, b[3] + pad, b[4] + pad, b[5] + pad]; } }
    if (!b || this.extent === 'view' || this.extent == null) { const site = this.app.project.site; const topo = this.app.topoGrid(); const crs = this.app.project.crs; let cx, cy; if (site && site.lon != null) [cx, cy] = GM.utm.fwd(site.lon, site.lat, crs.zone, crs.north); else { const bb = this.app.project.bounds(); cx = (bb[0] + bb[3]) / 2; cy = (bb[1] + bb[4]) / 2; } const R = (site && site.radius_m ? site.radius_m : 2500) / 2; const zs = topo ? topo.sample(cx, cy) : 0; b = [cx - R, cy - R, (zs === zs ? zs : 0) - 400, cx + R, cy + R, (zs === zs ? zs : 0) + 20]; }
    if (!b) return toast('no extent', 'warn');
    const n = Math.ceil((b[3] - b[0]) / p.size) * Math.ceil((b[4] - b[1]) / p.size) * Math.ceil((b[5] - b[2]) / p.zsize); if (n > 2e6) return toast(`that would be ${n.toLocaleString()} blocks — use larger blocks (limit 2M)`, 'warn', 6000);
    const bm = E.createBlockModel(b, [p.size, p.size, p.zsize], { name: `Block model ${this.app.project.byKind('blockmodel').length + 1}` }); bm.group = 'Block models'; this.app.project.add(bm); this.bm = bm; this.T.showPanel(this.panel());
  }
  samples() { const p = this.params; const ps = this.app.project.get(p.samples); if (!ps || !p.value) throw new Error('pick a sample layer and value column'); if (ps.role === 'claims' || !ps.isNumeric(p.value)) throw new Error(`${ps.name} has no numeric ${p.value || 'value'} column — claims centroids and name lists cannot be estimated from`); return ps; }
  async variogram() {
    try { const ps = this.samples(); const p = this.params; const vals = ps.numeric(p.value); const r = await this.app.engine.call('empiricalVariogram', { points: GM.packObject(ps), values: vals, nLags: p.nLags, lagSize: p.lagSize === '' ? null : +p.lagSize, dim: 3 }); this.exp = r; if (!r.length) return toast('no pairs (need ≥ 2 valid samples)', 'warn'); await this.fit(); }
    catch (e) { toast(e.message, 'err'); }
  }
  async fit() { if (!this.exp) return; try { const j = await this.app.engine.call('fitVariogram', { experimental: this.exp, model: this.params.model }); const s = j.structures[0]; this.params.nugget = +j.nugget.toPrecision(4); this.params.sill = +s.sill.toPrecision(4); this.params.range = +s.range.toPrecision(4); this.T.showPanel(this.panel()); } catch (e) { toast(e.message, 'err'); } }
  variogramModel() { const p = this.params; let an = null; if (p.aniso && +p.aniso > 1) { const ratio = +p.aniso, zr = p.anisoZ ? +p.anisoZ : ratio; an = new E.Anisotropy([p.range, p.range / ratio, p.range / zr], p.anisoAz || 0, 0, 0); } const vg = new E.Variogram({ nugget: p.nugget, structures: [{ model: p.model, sill: p.sill, range: an ? 1 : p.range }], anisotropy: an }); return vg; }
  plot() { if (!this.canvas) return; try { plotVariogram(this.canvas, this.exp || [], this.exp ? this.variogramModel() : null, { title: this.exp ? `${this.params.value}: ${this.exp.reduce((a, e) => a + e.pairs, 0)} pairs` : 'compute the experimental variogram' }); } catch (e) { /* ignore */ } }
  async run() {
    const p = this.params; if (!this.bm) return; this.busy = true; this.T.showPanel(this.panel());
    try {
      const ps = this.samples(); const vg = p.method === 'ok' ? this.variogramModel().toJSON() : null;
      const r = await this.app.engine.call('estimate', { bm: GM.packObject(this.bm), samples: GM.packObject(ps), value: p.value, method: p.method, variogram: vg, maxPoints: p.maxPoints, radius: p.radius === '' ? null : +p.radius, minPoints: p.minPoints, power: p.power, domain: p.domain || null, domainValue: p.domainValue || null }, (f, n) => this.app.status(`estimating ${(f * 100) | 0}% ${n || ''}`));
      for (const [k, a] of Object.entries(r.attributes)) this.bm.addAttribute(k, a.values, a.type || 'number'); this.bm.metadata.estimates = r.metadata.estimates;
      this.bm.metadata.derived_from = [...new Set([...(this.bm.metadata.derived_from || []), ps.id])];
      const d = this.app.display.get(this.bm.id) || {}; d.attribute = p.value + '_est'; d.cutoff = null; this.app.display.set(this.bm.id, d); this.app.refresh(this.bm); this.T.section.update(); this.app.status('estimate done'); toast(`${p.value}_est written to ${this.bm.name}`, 'ok');
    } catch (e) { console.error(e); toast('estimate failed: ' + e.message, 'err', 8000); }
    this.busy = false; this.T.showPanel(this.panel());
  }
  gt() {
    const bm = this.bm; const attrs = Object.keys(bm.attributes).filter(k => bm.attributes[k].type === 'number'); if (!attrs.length) return toast('no numeric attribute', 'warn');
    const attr = (this.app.display.get(bm.id) || {}).attribute || attrs[0]; const vals = bm.attributes[attr].values; let lo = Infinity, hi = -Infinity; for (const v of vals) { if (v !== v) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
    const density = num(2.7), cut = txt([0, 0.25, 0.5, 0.75].map(f => +(lo + (hi - lo) * f).toPrecision(3)).join(', ')); const out = h('div', {});
    const calc = () => { const cutoffs = cut.value.split(/[,\s]+/).filter(Boolean).map(Number); const rows = E.gradeTonnage(bm, attr, cutoffs, { density: +density.value }); clear(out).appendChild(h('table', { class: 'tbl' }, h('tr', {}, h('th', {}, 'cut-off'), h('th', {}, 'blocks'), h('th', {}, 'volume m³'), h('th', {}, 'tonnes'), h('th', {}, 'mean grade')), ...rows.map(r => h('tr', {}, h('td', {}, fmtNum(r.cutoff, 3)), h('td', {}, r.blocks.toLocaleString()), h('td', {}, Math.round(r.volume_m3).toLocaleString()), h('td', {}, Math.round(r.tonnes).toLocaleString()), h('td', {}, fmtNum(r.mean_grade, 3)))))); };
    calc(); modal('GRADE–TONNAGE', h('div', {}, row('attribute', h('b', {}, attr)), row('density t/m³', density), row('cut-offs', cut), btn('RECALCULATE', calc), out, note('Whole blocks only (no partial blocks, no dilution, no recovery). A research-grade view of an interpolation of cited/imported numbers — not a resource estimate.')));
  }
}

/* ========================================================== IMPLICIT */
export class ImplicitTool {
  constructor(T) { this.T = T; this.params = { kernel: 'thin_plate', drift: 'linear', smoothing: 0, spacing: '', valueCol: '', offset: 10, zFrom: '', zTo: '', iso: 0, clipTopo: true, trend: 'none' }; this.busy = false; this.mode = null; }
  get app() { return this.T.app; }
  /** Default field box: the points' XY extent padded 10 %, from 20 m above
      the highest ground down to 400 m below the lowest — a surface traced at
      the surface is projected to depth, and the box says how far. */
  defaults(ps) {
    const topo = this.app.topoGrid(); const [tmin, tmax] = topo ? topo.zrange() : [NaN, NaN]; const b = ps ? ps.bounds() : null; let xy = null;
    if (b) { const pad = Math.max(b[3] - b[0], b[4] - b[1], 1) * 0.1; xy = [b[0] - pad, b[1] - pad, b[3] + pad, b[4] + pad]; }
    return { zFrom: tmin === tmin ? tmin - 400 : (b ? b[2] - 50 : null), zTo: tmax === tmax ? tmax + 20 : (b ? b[5] + 50 : null), xy, tmin, tmax };
  }
  bounds(ps) { const d = this.defaults(ps); const p = this.params; const zFrom = p.zFrom === '' ? d.zFrom : +p.zFrom, zTo = p.zTo === '' ? d.zTo : +p.zTo; if (!d.xy || zFrom == null || zTo == null || zFrom !== zFrom || zTo !== zTo) return null; return [d.xy[0], d.xy[1], Math.min(zFrom, zTo), d.xy[2], d.xy[3], Math.max(zFrom, zTo)]; }
  globalTrend() { const P = this.app.project; return P && P.metadata && P.metadata.global_trend ? P.metadata.global_trend : null; }
  /** The anisotropy handed to the RBF, and the sentence that says which trend it is. */
  trend() {
    const g = this.globalTrend();
    if (this.params.trend !== 'global' || !g) return { anisotropy: null, text: 'trend: none' };
    const an = globalAnisotropy(this.app.project, 1);
    return { anisotropy: an ? an.toJSON() : null, text: `trend: project global trend ${fmtNum(g.dip, 0)}°→${String(Math.round(g.dip_azimuth || 0)).padStart(3, '0')}, pitch ${fmtNum(g.pitch || 0, 0)}, ratios ${(g.ratios || [3, 3, 1]).join(',')}` };
  }
  panel() {
    const p = this.params; const P = h('div', { class: 'tool' }, h('h2', {}, 'IMPLICIT SURFACE (RBF)'));
    const pl = implicitLayers(this.app.project); const ps = pl.find(l => l.id === p.points) || pl.find(l => l.role === 'contacts') || pl[0] || null; p.points = ps ? ps.id : '';
    P.appendChild(note('Leapfrog-style implicit modelling: an RBF fits signed distances — 0 on the contact, positive on one side, negative on the other — and the zero iso-surface is the contact / vein / shell. Give a signed-distance column, or let the tool derive ±offset points from a "side" column (hw/fw, in/out, above/below, +/-).'));
    if (!pl.length) P.appendChild(note('✗ a points layer with a signed-distance or side column, or contact points — import one, or DIGITISE ± POINTS below', 'note warn'));
    P.appendChild(row('points', sel(pl.length ? pl.map(l => [l.id, `${l.name} (${l.n})`]) : [['', '— none —']], p.points, { disabled: !pl.length, onchange: e => { p.points = e.target.value; this.T.showPanel(this.panel()); } })));
    const cols = ps ? Object.keys(ps.attributes) : [];
    P.appendChild(row('value column', sel([['', '— all points are on the contact (needs a side column or digitised ± points) —'], ...cols], p.valueCol, { onchange: e => { p.valueCol = e.target.value; } })));
    P.appendChild(row('side column', sel([['', '—'], ...cols], p.sideCol || '', { onchange: e => { p.sideCol = e.target.value; } }), num(p.offset, { title: 'offset distance for side points (m)', oninput: e => { p.offset = +e.target.value; } })));
    P.appendChild(row('kernel', sel(E.RBF_KERNELS, p.kernel, { onchange: e => { p.kernel = e.target.value; } }), sel(['none', 'constant', 'linear'], p.drift, { onchange: e => { p.drift = e.target.value; } })));
    P.appendChild(row('smoothing', num(p.smoothing, { oninput: e => { p.smoothing = +e.target.value; } }), h('span', { class: 'mono' }, 'grid m'), num(p.spacing, { placeholder: 'auto', oninput: e => { p.spacing = e.target.value; } })));
    const d = this.defaults(ps);
    P.appendChild(row('z from / to', num(p.zFrom, { placeholder: d.zFrom == null ? 'bottom m' : fmtNum(d.zFrom, 0), title: 'bottom of the field box (default: lowest ground − 400 m)', oninput: e => { p.zFrom = e.target.value; } }), num(p.zTo, { placeholder: d.zTo == null ? 'top m' : fmtNum(d.zTo, 0), title: 'top of the field box (default: highest ground + 20 m)', oninput: e => { p.zTo = e.target.value; } })));
    P.appendChild(note(`field box: the points' XY extent padded 10 %${d.xy ? ` (${fmtNum(d.xy[2] - d.xy[0], 0)} × ${fmtNum(d.xy[3] - d.xy[1], 0)} m)` : ''}, z ${p.zFrom === '' ? (d.zFrom == null ? '?' : fmtNum(d.zFrom, 0)) : p.zFrom} – ${p.zTo === '' ? (d.zTo == null ? '?' : fmtNum(d.zTo, 0)) : p.zTo} m — the surface is projected that far, on the RBF's extrapolation alone`));
    P.appendChild(row('iso value', num(p.iso, { oninput: e => { p.iso = +e.target.value; } }), h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: !!p.clipTopo, onchange: e => { p.clipTopo = e.target.checked; } }), 'clip below topography')));
    const g = this.globalTrend();
    P.appendChild(row('trend', sel([['none', 'none'], ['global', g ? 'project global trend' : 'project global trend (none set — step 5 sets it)']], p.trend, { onchange: e => { p.trend = e.target.value; this.T.showPanel(this.panel()); } })));
    P.appendChild(note(this.trend().text + (p.trend === 'global' && !g ? ' — no global trend on this project, so none is applied' : '')));
    P.appendChild(h('div', { class: 'frow' }, btn('DIGITISE ± POINTS', () => this.digitise()), btn(this.busy ? 'BUILDING…' : 'BUILD SURFACE', () => this.build(), { disabled: this.busy || !ps, title: ps ? '' : 'no points layer to fit through' })));
    return P;
  }
  digitise() { const ps = new GM.PointSet({ name: `implicit points ${this.app.project.byKind('points').length + 1}`, role: 'contacts', group: 'Surfaces', color: [255, 120, 200] }); this.app.project.add(ps); this.params.points = ps.id; this.params.valueCol = 'sd'; this.mode = 'dig'; this.sign = 0; this.armText(); this.T.showPanel(this.panel()); }
  armText() { const side = this.sign > 0 ? 'POSITIVE side (hanging wall)' : this.sign < 0 ? 'NEGATIVE side (foot wall)' : 'ON the contact'; this.T.arm(this, `± POINTS — clicks place ${side} points · press + / − / 0 to switch side (it stays switched) · Esc stops`, `${this.app.project.get(this.params.points).name}`); }
  onKey(e) { if (!this.mode) return false; if (e.key === '+' || e.key === '=') { this.sign = 1; this.armText(); return true; } if (e.key === '-') { this.sign = -1; this.armText(); return true; } if (e.key === '0') { this.sign = 0; this.armText(); return true; } if (e.key === 'Escape') { this.mode = null; this.T.disarm(); return true; } return false; }
  onClick(e) { if (this.mode !== 'dig') return false; const w = groundPick(this.app.R, e); if (!w) return true; const ps = this.app.project.get(this.params.points); ps.add(w[0], w[1], w[2], { sd: this.sign * (this.params.offset || 10), side: this.sign > 0 ? 'pos' : this.sign < 0 ? 'neg' : 'on' }); this.app.refresh(ps); return true; }
  stop() { this.mode = null; }
  async build() {
    const p = this.params; const ps = this.app.project.get(p.points); if (!ps) return; this.busy = true; this.T.showPanel(this.panel());
    try {
      const xyz = []; const vals = [];
      for (let i = 0; i < ps.n; i++) {
        const [x, y, z] = ps.point(i); let v = 0;
        if (p.valueCol && ps.isNumeric(p.valueCol)) v = ps.numeric(p.valueCol)[i];
        xyz.push(x, y, z); vals.push(v === v ? v : 0);
        if (p.sideCol && ps.attributes[p.sideCol]) { const s = String(ps.attributes[p.sideCol][i] || '').toLowerCase(); const sg = /^(hw|hang|up|above|in|pos|\+|1|top)/.test(s) ? 1 : /^(fw|foot|down|below|out|neg|-|bottom)/.test(s) ? -1 : 0; if (sg) { xyz.push(x, y, z + sg * p.offset); vals.push(sg * p.offset); } }
      }
      if (vals.every(v => v === 0)) throw new Error('all values are 0 — add positive/negative side points (or a signed-distance column) so the surface has an inside and an outside');
      const topo = this.app.topoGrid(); const bnd = this.bounds(ps); const tr = this.trend(); const iso = +p.iso || 0;
      const progress = (f, n) => this.app.status(`implicit ${(f * 100) | 0}% ${n || ''}`);
      const name = `${ps.name} surface${bnd ? ` · projected to ${fmtNum(bnd[2], 0)} m` : ''}`; const color = [220, 120, 60];
      const common = { kernel: p.kernel, drift: p.drift, smoothing: p.smoothing, anisotropy: tr.anisotropy };
      const spacing = p.spacing === '' ? (bnd ? Math.max(bnd[3] - bnd[0], bnd[4] - bnd[1], bnd[5] - bnd[2]) / 50 : null) : +p.spacing;
      let mesh, clipped = 0;
      if (p.clipTopo && topo && bnd) {
        // the engine's implicitSurface op evaluates the field and runs the
        // iso-surface in one go with no hook between them, so the clipped
        // path is its three parts — fit, field, iso-surface — with the mask
        // applied in the middle: every node above the ground, or off the
        // topography grid, becomes NaN and no triangle is made there
        const count = [0, 1, 2].map(k => Math.ceil((bnd[3 + k] - bnd[k]) / spacing) + 1); const nn = count[0] * count[1] * count[2];
        if (nn > 200 ** 3) throw new Error(`${count.join('×')} = ${nn.toLocaleString()} field nodes — coarsen the grid spacing or shrink the z range (limit 200³)`);
        const rbf = await this.app.engine.call('rbfFit', Object.assign({ points: Float64Array.from(xyz), values: Float64Array.from(vals), dim: 3 }, common), progress);
        const sf = await this.app.engine.call('scalarFieldFromRBF', { rbf, bounds: bnd, spacing }, (f, n) => progress(0.05 + 0.7 * f, n || 'evaluating field'));
        const [nx, ny, nz] = sf.count; const [ox, oy, oz] = sf.origin; const [dx, dy, dz] = sf.spacing;
        for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) { const t = topo.sample(ox + i * dx, oy + j * dy); for (let k = 0; k < nz; k++) { if (t !== t || oz + k * dz > t) { sf.field[i + nx * (j + ny * k)] = NaN; clipped++; } } }
        mesh = await this.app.engine.call('isosurface', { field: sf.field, count: sf.count, origin: sf.origin, spacing: sf.spacing, iso, name, color }, (f, n) => progress(0.8 + 0.2 * f, n || 'iso-surface'));
        mesh.metadata.implicit = { kernel: p.kernel, drift: p.drift, smoothing: p.smoothing, n_points: vals.length, bounds: bnd, spacing: sf.spacing, count: sf.count, iso, clipped_nodes: clipped };
      } else {
        mesh = await this.app.engine.call('implicitSurface', Object.assign({ points: Float64Array.from(xyz), values: Float64Array.from(vals), spacing, bounds: bnd, iso, name, color }, common), progress);
      }
      if (!mesh.nTriangles) throw new Error(`the iso-surface ${iso} is empty inside the field box${clipped ? ' below the topography' : ''} — widen z from / to, change the iso value, or add side points`);
      mesh.group = 'Surfaces'; mesh.role = 'contact';
      mesh.provenance = { method: 'RBF implicit', points: ps.name, kernel: p.kernel, confidence: 'inferred', trend: tr.anisotropy ? 'project global trend' : 'none', clipped_below_topography: !!(p.clipTopo && topo && bnd) };
      mesh.metadata.derived_from = [ps.id, ...(topo && p.clipTopo ? [topo.id] : [])];
      mesh.metadata.boundary = bnd ? { z_from: bnd[2], z_to: bnd[5], xy: [bnd[0], bnd[1], bnd[3], bnd[4]], spacing, clip_below_topography: !!(p.clipTopo && topo), relative_to_topo_min: topo ? +(bnd[2] - topo.zrange()[0]).toFixed(1) : null } : { clip_below_topography: false };
      mesh.metadata.trend = tr.text;
      this.app.project.add(mesh); this.app.select(mesh.id); toast(`surface built: ${mesh.nTriangles} triangles · ${tr.text}${clipped ? ' · clipped below the topography' : ''}`, 'ok', 5000);
    } catch (e) { console.error(e); toast('implicit build failed: ' + e.message, 'err', 8000); }
    this.busy = false; this.T.showPanel(this.panel());
  }
}
