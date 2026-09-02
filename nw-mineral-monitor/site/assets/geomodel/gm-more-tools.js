/* gm-more-tools.js — three small tools for model3d.html that live outside
   gm-tools.js: MEASURE (distances, bearings, plunges in metres and feet),
   NOTES (interpretation notes pinned with their source) and TRACE (a fault /
   contact / vein polyline of a chosen role, sketched by hand).
   installMoreTools(tools) registers them with the tool shell; the module also
   installs itself when window.gmApp.tools already exists at import time, so
   it works whether or not the viewer wires the call. */
import * as GM from './gm-core.js';
import * as E from './gm-engine.js';
import { THREE, CONF_CLASSES, confClass } from './gm-render.js';
import { h, row, num, txt, sel, btn, note, kv, section, toast, modal, fmtNum, lineSample } from './gm-ui.js';

const $ = id => document.getElementById(id);
const FT = E.FT;
const today = () => new Date().toISOString().slice(0, 10);
const mft = m => `${fmtNum(m, 1)} m / ${fmtNum(m / FT, 0)} ft`;
function groundPick(R, e) { const p = R.pick(e.clientX, e.clientY, o => o.kind === 'grid2d' || o.kind === 'mesh' || o.kind === 'imageplane'); return p ? p.world : null; }
function bearingOf(a, b) { const d = Math.atan2(b[0] - a[0], b[1] - a[1]) * 180 / Math.PI; return (d % 360 + 360) % 360; }
const dashOf = f => (CONF_CLASSES.find(c => c.key === confClass(f)) || {}).dash || null;   // the dash a confidence draws with, from the one table
function hexOf(c) { return (c[0] << 16) | (c[1] << 8) | c[2]; }

/** Measurements of a chain of points: length along the chain in 3-D and in
    plan, Δz first → last, the bearing (clockwise from north) and plunge
    (positive down) of first → last, and the same per leg. */
export function measureChain(pts) {
  let len3d = 0, plan = 0; const legs = [];
  for (let i = 1; i < pts.length; i++) { const a = pts[i - 1], b = pts[i]; const dp = Math.hypot(b[0] - a[0], b[1] - a[1]); const dz = b[2] - a[2]; const d3 = Math.hypot(dp, dz); len3d += d3; plan += dp; legs.push({ len3d: d3, plan: dp, dz, bearing: bearingOf(a, b), plunge: Math.atan2(-dz, dp) * 180 / Math.PI }); }
  const a = pts[0], b = pts[pts.length - 1]; const dz = pts.length > 1 ? b[2] - a[2] : 0; const dp = pts.length > 1 ? Math.hypot(b[0] - a[0], b[1] - a[1]) : 0;
  return { n: pts.length, len3d, plan, dz, straight: Math.hypot(dp, dz), bearing: pts.length > 1 ? bearingOf(a, b) : null, plunge: pts.length > 1 ? Math.atan2(-dz, dp) * 180 / Math.PI : null, legs };
}

/* ============================================================ MEASURE */
export class MeasureTool {
  constructor(T) { this.T = T; this.pts = []; this.snaps = []; this.mode = null; this.purpose = 'Distances, bearings and plunges between clicked points — on any object or the ground — in metres and feet (the plates are in feet). A readout, not geometry, unless you keep it as an annotation line.'; }
  get app() { return this.T.app; }
  onProject() { this.pts = []; this.snaps = []; this.mode = null; }
  stop() { this.mode = null; this.pts = []; this.snaps = []; }
  panel() {
    const P = h('div', { class: 'tool' }, h('h2', {}, 'MEASURE'));
    P.appendChild(note(this.purpose));
    P.appendChild(h('div', { class: 'frow' }, btn(this.mode ? 'MEASURING… (click points)' : 'START (click points)', () => this.start(), { class: this.mode ? 'on' : 'primary' }), btn('CLEAR (Esc)', () => this.clear()), btn('KEEP AS ANNOTATION LINE', () => this.keep(), { disabled: this.pts.length < 2, title: this.pts.length < 2 ? 'measure two points first' : 'write the line and its numbers into a Notes layer (role annotation — never workings)' })));
    if (this.pts.length) { const m = measureChain(this.pts); P.appendChild(kv(this.rows(m))); P.appendChild(note(this.snaps.map((s, i) => `${i + 1} on ${s}`).join(' · '))); }
    else P.appendChild(note('Nothing measured yet. The first click snaps to whatever is under the cursor (a working, a surface, the ground) and the mode line says which.'));
    return P;
  }
  rows(m) {
    const r = [['Points', String(m.n)]]; if (m.n < 2) return r;
    r.push(['3-D length', mft(m.len3d)], ['Plan length', mft(m.plan)], ['Δz (first → last)', mft(m.dz)], ['Bearing', `${fmtNum(m.bearing, 1)}° clockwise from north`], ['Plunge', `${fmtNum(m.plunge, 1)}° (+ down)`]);
    if (m.n > 2) { r.push(['Straight first → last', mft(m.straight)]); const L = m.legs[m.legs.length - 1]; r.push(['Last leg', `${mft(L.len3d)} · ${fmtNum(L.bearing, 0)}° / ${fmtNum(L.plunge, 0)}°`]); }
    return r;
  }
  start() { this.mode = 'measure'; this.T.arm(this, 'MEASURE — click the first point (any object, or the ground), then the second · more clicks chain · Esc clears', 'a readout, no geometry'); this.T.showPanel(this.panel()); }
  clear() { this.pts = []; this.snaps = []; this.app.R.clearOverlay(); this.mode = null; this.T.disarm(); this.app.status(''); this.T.showPanel(this.panel()); }
  pick(e) { const p = this.app.R.pick(e.clientX, e.clientY); return p ? { w: p.world, on: p.obj ? p.obj.name : 'ground' } : null; }
  onClick(e) { if (!this.mode) return false; const p = this.pick(e); if (!p) { this.app.status('nothing under the cursor — click an object or the ground'); return true; } this.addPoint(p.w, p.on); return true; }
  onMove(e) { if (!this.mode || !this.pts.length) return false; const p = this.pick(e); if (!p) return true; this.preview(p.w); return true; }
  onKey(e) { if (!this.mode && !this.pts.length) return false; if (e.key === 'Escape') { this.clear(); return true; } if (e.key === 'Enter' && this.pts.length >= 2) { this.keep(); return true; } return false; }
  /** Add a measured point (world xyz) and say what it snapped to. */
  addPoint(w, on = 'ground') {
    this.pts.push([w[0], w[1], w[2]]); this.snaps.push(on); this.preview();
    const m = measureChain(this.pts);
    this.app.status(this.pts.length < 2 ? `point 1 on ${on} — click the second point` : `${mft(m.len3d)} · plan ${mft(m.plan)} · Δz ${mft(m.dz)} · bearing ${fmtNum(m.bearing, 1)}° · plunge ${fmtNum(m.plunge, 1)}° (${this.pts.length} points, last on ${on})`);
    if (this.T.active === this) this.T.showPanel(this.panel());
    return m;
  }
  preview(cursor) { const R = this.app.R; R.clearOverlay(); for (const p of this.pts) R.overlayMarker(p, 0x7dd3fc, 9); if (this.pts.length > 1) R.overlayPolyline(this.pts, 0x7dd3fc); if (cursor && this.pts.length) R.overlayPolyline([this.pts[this.pts.length - 1], cursor], 0x7dd3fc, true); }
  /** Write the chain as a feature of the 'measurements' annotation lineset
      (group Notes, role annotation — it can never be mistaken for a working). */
  keep() {
    if (this.pts.length < 2) { toast('measure two points first', 'warn'); return null; }
    const m = measureChain(this.pts);
    let ls = this.app.project.byKind('lineset').find(l => l.role === 'annotation' && l.name === 'measurements'); const fresh = !ls;
    if (fresh) { ls = new GM.LineSet({ name: 'measurements', role: 'annotation', color: [125, 211, 252], group: 'Notes' }); ls.provenance = { method: 'measured by hand in the 3-D modeller', confidence: 'sketched' }; ls.metadata.schema = 'nwmm-annotation/1'; }
    const feat = { name: `${fmtNum(m.len3d, 1)} m (${fmtNum(m.len3d / FT, 0)} ft)`, kind: 'measurement', length_m: +m.len3d.toFixed(2), plan_m: +m.plan.toFixed(2), dz_m: +m.dz.toFixed(2), bearing_deg: +m.bearing.toFixed(1), plunge_deg: +m.plunge.toFixed(1), length_ft: +(m.len3d / FT).toFixed(1), plan_ft: +(m.plan / FT).toFixed(1), dz_ft: +(m.dz / FT).toFixed(1), snapped_to: this.snaps.join(' → '), confidence: 'sketched', date: today() };
    ls.addPolyline(this.pts.map(p => p.slice()), feat);
    if (fresh) this.app.project.add(ls); else this.app.refresh(ls);
    toast(`kept as an annotation line in "${ls.name}" (Notes)`, 'ok');
    this.clear(); return ls;
  }
}

/* ============================================================== NOTES */
const NOTE_COLS = ['text', 'source', 'page', 'url', 'author', 'date'];
function rowOf(ps, i) { const attrs = {}; for (const [k, c] of Object.entries(ps.attributes)) attrs[k] = c[i]; return { xyz: ps.point(i), attrs }; }
function removeRow(ps, i) { const xyz = new Float64Array(ps.xyz.length - 3); xyz.set(ps.xyz.subarray(0, 3 * i)); xyz.set(ps.xyz.subarray(3 * i + 3), 3 * i); ps.xyz = xyz; for (const k of Object.keys(ps.attributes)) ps.attributes[k].splice(i, 1); }
function insertRow(ps, i, r) { const xyz = new Float64Array(ps.xyz.length + 3); xyz.set(ps.xyz.subarray(0, 3 * i)); xyz.set(r.xyz, 3 * i); xyz.set(ps.xyz.subarray(3 * i), 3 * i + 3); ps.xyz = xyz; for (const k of Object.keys(ps.attributes)) ps.attributes[k].splice(i, 0, r.attrs[k] == null ? null : r.attrs[k]); }

export class NoteTool {
  constructor(T) { this.T = T; this.mode = null; this.author = ''; this.purpose = 'Pin an interpretation note to a spot — on the ground or on any object — with the document, page or URL it rests on. Notes are a points layer of their own: they never count as workings and never change the confidence tally.'; }
  get app() { return this.T.app; }
  onProject() { this.mode = null; }
  stop() { this.mode = null; }
  find() { return this.app.project.byKind('points').find(p => p.role === 'notes') || null; }
  /** One notes layer per project, labels forced on (the text is the label). */
  ensureLayer() {
    let ps = this.find();
    if (!ps) { ps = new GM.PointSet({ name: 'notes (interpretation)', role: 'notes', color: [255, 210, 90], group: 'Notes' }); for (const c of NOTE_COLS) ps.attributes[c] = []; ps.provenance = { method: 'typed by hand in the 3-D modeller', kind: 'interpretation' }; this.app.project.add(ps); }
    const d = this.app.display.get(ps.id) || {}; d.labels = true; d.labelField = 'text'; this.app.display.set(ps.id, d);
    return ps;
  }
  panel() {
    const P = h('div', { class: 'tool' }, h('h2', {}, 'NOTES'));
    P.appendChild(note(this.purpose));
    P.appendChild(h('div', { class: 'frow' }, btn(this.mode ? 'CLICK WHERE THE NOTE BELONGS…' : 'PIN A NOTE (click the scene)', () => this.start(), { class: this.mode ? 'on' : 'primary' }), this.mode ? btn('CANCEL (Esc)', () => this.cancel()) : null));
    const ps = this.find();
    const list = section(ps ? `NOTES (${ps.n})` : 'NOTES (none yet)');
    if (ps && ps.n) {
      for (let i = 0; i < ps.n; i++) {
        const a = ps.attributes; const src = [a.source && a.source[i], a.page && a.page[i] ? 'p. ' + a.page[i] : ''].filter(Boolean).join(' ');
        list.appendChild(h('div', { class: 'feat', title: `${a.text[i]}${src ? ' — ' + src : ''}${a.url && a.url[i] ? ' · ' + a.url[i] : ''}`, onmouseenter: () => this.app.R.highlight && this.app.R.highlight(ps, i), onmouseleave: () => this.app.R.highlight && this.app.R.highlight(null), onclick: () => { const q = ps.point(i); this.app.R.fitTo([q[0] - 80, q[1] - 80, q[2] - 80, q[0] + 80, q[1] + 80, q[2] + 80]); } },
          h('span', { class: 'fn' }, String(a.text[i] || '').slice(0, 60), src ? h('span', { class: 'fl' }, ' · ' + src) : null, a.date && a.date[i] ? h('span', { class: 'fl' }, ' · ' + a.date[i]) : null),
          btn('✕', e => { e.stopPropagation(); this.remove(ps, i); }, { class: 'x', title: 'remove this note (undo from the toast)' })));
      }
    } else list.appendChild(note('Nothing pinned yet. The layer "notes (interpretation)" is created with the first note, under Notes, with its labels on.'));
    P.appendChild(list);
    return P;
  }
  start() { this.mode = 'place'; this.T.arm(this, 'NOTE — click where the note belongs (ground or any object) · Esc stops', 'notes (interpretation) — a note, never a working'); this.T.showPanel(this.panel()); }
  cancel() { this.mode = null; this.T.disarm(); this.T.showPanel(this.panel()); }
  onClick(e) { if (!this.mode) return false; const p = this.app.R.pick(e.clientX, e.clientY); if (!p) { this.app.status('nothing under the cursor — click the ground or an object'); return true; } this.ask(p.world, p.obj ? p.obj.name : 'ground'); return true; }
  onKey(e) { if (e.key === 'Escape' && this.mode) { this.cancel(); return true; } return false; }
  ask(w, on) {
    const text = h('textarea', { rows: 3, placeholder: 'what you read, saw or infer here', style: { width: '100%' } });
    const doc = txt('', { placeholder: 'document / plate' }), page = txt('', { placeholder: 'page', style: { width: '60px' } }), url = txt('', { placeholder: 'https://…' }), author = txt(this.author, { placeholder: 'initials' });
    const body = h('div', {}, kv([['At', `E ${fmtNum(w[0], 1)}  N ${fmtNum(w[1], 1)}  z ${fmtNum(w[2], 1)} m · on ${on}`]]), row('note', text), row('source', doc, page), row('url', url), row('author', author),
      h('div', { class: 'frow', style: { justifyContent: 'flex-end', marginTop: '10px' } }, btn('CANCEL', () => m.close()), btn('PIN NOTE', () => { if (!text.value.trim()) return toast('the note is empty', 'warn'); this.author = author.value; m.close(); this.commit(w, { text: text.value.trim(), source: doc.value, page: page.value, url: url.value, author: author.value }); }, { class: 'primary' })));
    const m = modal('PIN A NOTE', body, { sticky: true }); setTimeout(() => text.focus(), 0);
  }
  /** Append one note row (world xyz + the six columns) to the notes layer. */
  commit(w, f) {
    const ps = this.ensureLayer();
    const i = ps.add(w[0], w[1], w[2], { text: f.text, source: f.source || '', page: f.page || '', url: f.url || '', author: f.author || '', date: f.date || today() });
    this.app.refresh(ps); this.app.status(`note ${ps.n} pinned — ${String(f.text).slice(0, 60)}`);
    if (this.T.active === this) this.T.showPanel(this.panel());
    toast('note pinned — listed under Notes; it never counts as a working', 'ok');
    return i;
  }
  remove(ps, i) { const r = rowOf(ps, i); this.app.destructive(`removed note "${String(r.attrs.text || '').slice(0, 30)}"`, () => { removeRow(ps, i); this.app.refresh(ps); this.T.showPanel(this.panel()); }, () => { insertRow(ps, i, r); this.app.refresh(ps); this.T.showPanel(this.panel()); }); }
}

/* =========================================================== POLYLINE */
const ROLES = {
  faults: { label: 'fault', group: 'Structure', layer: 'Faults (traced)', color: [212, 165, 63], type: 'fault' },
  'geology-outline': { label: 'contact', group: 'Geology outlines', layer: 'Contacts (traced)', color: [40, 40, 40], type: 'contact' },
  lines: { label: 'line', group: 'Imports', layer: 'Lines (traced)', color: [200, 200, 220], type: 'vein' },
};
export class PolylineTool {
  constructor(T) { this.T = T; this.role = 'faults'; this.target = ''; this.image = null; this.pts = []; this.mode = null; this.form = { name: '', type: 'fault', unit_a: '', unit_b: '', doc: '', page: '', layerName: '' }; this.purpose = 'Trace a fault, a unit contact or a vein by clicking along it on the draped map, the ground or a georeferenced plate. The polyline lands in a lineset of the role you choose, tagged sketched, so the Structure tool can derive orientations from it and the pancake builder can use a contact as a unit base.'; }
  get app() { return this.T.app; }
  onProject() { this.target = ''; this.image = null; this.pts = []; this.mode = null; }
  stop() { this.mode = null; this.pts = []; }
  layers() { return this.app.project.byKind('lineset').filter(l => l.role === this.role); }
  panel() {
    const f = this.form; const meta = ROLES[this.role]; const P = h('div', { class: 'tool' }, h('h2', {}, 'TRACE A FAULT / CONTACT / VEIN'));
    P.appendChild(note(this.purpose));
    P.appendChild(row('role', sel([['faults', 'fault → Structure'], ['geology-outline', 'contact / unit boundary → Geology outlines'], ['lines', 'line — vein, dyke, anything → Imports']], this.role, { onchange: e => { this.role = e.target.value; this.target = ''; f.type = ROLES[this.role].type; this.T.showPanel(this.panel()); } })));
    const layers = this.layers(); if (this.target && !layers.some(l => l.id === this.target)) this.target = '';
    P.appendChild(row('layer', sel([['', '— new layer —'], ...layers.map(l => [l.id, `${l.name} (${l.parts.length})`])], this.target, { onchange: e => { this.target = e.target.value; this.T.showPanel(this.panel()); } })));
    if (!this.target) P.appendChild(row('new layer name', txt(f.layerName, { placeholder: meta.layer, oninput: e => { f.layerName = e.target.value; } })));
    const images = this.app.project.byKind('imageplane');
    P.appendChild(row('trace on', sel([['', 'ground / draped map (z = topography + 3 m)'], ...images.map(i => [i.id, `${i.name} (${i.plane}${i.elevation != null ? ' @' + fmtNum(i.elevation, 0) : ''})`])], this.image ? this.image.id : '', { onchange: e => { this.image = e.target.value ? this.app.project.get(e.target.value) : null; this.T.showPanel(this.panel()); } })));
    if (this.image) P.appendChild(note(`tracing on "${this.image.name}" — clicks are read through its georeference${this.image.plane === 'plan' && this.image.elevation != null ? ` at ${fmtNum(this.image.elevation, 0)} m` : ''}`));
    const frm = section('FEATURE');
    frm.appendChild(row('name', txt(f.name, { placeholder: 'Silver Hills fault / Tv–Pz contact', oninput: e => { f.name = e.target.value; } })));
    frm.appendChild(row('type', sel(['fault', 'contact', 'vein', 'dyke', 'shear', 'fold axis', 'other'], f.type, { onchange: e => { f.type = e.target.value; } })));
    frm.appendChild(row('units', txt(f.unit_a, { placeholder: 'unit A (hanging wall / above)', oninput: e => { f.unit_a = e.target.value; } }), txt(f.unit_b, { placeholder: 'unit B', oninput: e => { f.unit_b = e.target.value; } })));
    frm.appendChild(row('source', txt(f.doc, { placeholder: 'map / plate / report', oninput: e => { f.doc = e.target.value; } }), txt(f.page, { placeholder: 'page', style: { width: '60px' }, oninput: e => { f.page = e.target.value; } })));
    frm.appendChild(row('confidence', lineSample(dashOf({ confidence: 'sketched' })), h('span', { class: 'mono' }, ' sketched — drawn by hand, dotted')));
    P.appendChild(frm);
    P.appendChild(h('div', { class: 'modes' }, btn(this.mode ? 'TRACING… (click along it)' : `START TRACE (${meta.label})`, () => this.start(), { class: this.mode ? 'on' : 'primary' })));
    if (this.mode) P.appendChild(h('div', { class: 'frow' }, btn('UNDO LAST POINT (Backspace)', () => { this.pts.pop(); this.preview(); }), btn('FINISH (Enter / double-click)', () => this.finish(), { class: 'primary' }), btn('CANCEL (Esc)', () => this.cancel())));
    const ls = this.target ? this.app.project.get(this.target) : null;
    const list = section(ls ? `FEATURES IN ${ls.name.toUpperCase()} (${ls.parts.length})` : 'FEATURES');
    if (ls) ls.features.forEach((ft, k) => list.appendChild(h('div', { class: 'feat', title: `${ft.type || ''}${ft.unit_a || ft.unit_b ? ` · ${ft.unit_a || '?'} / ${ft.unit_b || '?'}` : ''}${ft.source ? ' · ' + ft.source + (ft.page ? ' p. ' + ft.page : '') : ''} · ${ft.confidence || 'unknown'}`, onmouseenter: () => this.app.R.highlight && this.app.R.highlight(ls, k), onmouseleave: () => this.app.R.highlight && this.app.R.highlight(null), onclick: () => { const pts = ls.partXYZ(k); const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]), zs = pts.map(p => p[2]); this.app.R.fitTo([Math.min(...xs) - 30, Math.min(...ys) - 30, Math.min(...zs) - 30, Math.max(...xs) + 30, Math.max(...ys) + 30, Math.max(...zs) + 30]); } },
      h('span', { class: 'fn' }, ft.name || ft.type || `part ${k}`, ft.type && ft.name ? h('span', { class: 'fl' }, ' ' + ft.type) : null), lineSample(dashOf(ft)), h('span', { class: 'fl' }, fmtNum(ls.length(k), 0) + ' m'),
      btn('✕', e => { e.stopPropagation(); this.removePart(ls, k); }, { class: 'x', title: 'remove this trace (undo from the toast)' }))));
    else list.appendChild(note('A new layer is created with the first trace you finish.'));
    P.appendChild(list);
    return P;
  }
  start() {
    this.mode = 'trace'; this.pts = []; this.app.R.clearOverlay();
    const meta = ROLES[this.role]; const tgt = this.target && this.app.project.get(this.target) ? this.app.project.get(this.target).name : `${this.form.layerName || meta.layer} (new)`;
    this.T.arm(this, `TRACE ${meta.label.toUpperCase()} — click along it · Enter or double-click finishes · Backspace undoes a point · Esc cancels`, `${tgt} as sketched${this.image ? ` on ${this.image.name}` : ' on the ground'}`);
    this.T.showPanel(this.panel());
  }
  /** Ground clicks land on the topography + 3 m (the draped-line convention);
      on a plate the click is read through its georeference. */
  pickPoint(e) {
    const R = this.app.R;
    if (this.image) {
      const ip = this.image; const c = ip.corners(); const P0 = R.toScene(c[0][0], c[0][1], c[0][2]); P0.y *= R.ve; const P1 = R.toScene(c[1][0], c[1][1], c[1][2]); P1.y *= R.ve; const P2 = R.toScene(c[3][0], c[3][1], c[3][2]); P2.y *= R.ve;
      const nrm = new THREE.Vector3().subVectors(P1, P0).cross(new THREE.Vector3().subVectors(P2, P0)).normalize(); const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(nrm, P0);
      const w = R.pickPlane(e.clientX, e.clientY, plane); if (!w) return null;
      if (ip.plane === 'plan') return [w[0], w[1], ip.elevation != null ? ip.elevation : w[2]];
      return w;
    }
    const w = groundPick(R, e); if (!w) return null;
    return this.onGround(w);
  }
  onGround(w) { const topo = this.app.topoGrid(); const t = topo ? topo.sample(w[0], w[1]) : NaN; return [w[0], w[1], t === t ? t + 3 : w[2]]; }
  onClick(e) { if (!this.mode) return false; const w = this.pickPoint(e); if (!w) return true; this.addPoint(w); return true; }
  addPoint(w) { this.pts.push([w[0], w[1], w[2]]); this.preview(); this.app.status(`${this.pts.length} point${this.pts.length > 1 ? 's' : ''} — Enter or double-click finishes`); return this.pts.length; }
  onDblClick() { if (this.mode) { this.finish(); return true; } return false; }
  onKey(e) { if (!this.mode) return false; if (e.key === 'Enter') { this.finish(); return true; } if (e.key === 'Escape') { this.cancel(); return true; } if (e.key === 'Backspace') { this.pts.pop(); this.preview(); return true; } return false; }
  onMove(e) { if (!this.mode || !this.pts.length) return false; const w = this.pickPoint(e); if (!w) return true; this.preview(w); return true; }
  preview(cursor) { const R = this.app.R; R.clearOverlay(); const hex = hexOf(ROLES[this.role].color.map(v => Math.max(v, 90))); for (const p of this.pts) R.overlayMarker(p, hex, 8); const pts = cursor ? this.pts.concat([cursor]) : this.pts; if (pts.length > 1) R.overlayPolyline(pts, hex, !!cursor); }
  /** Commit the points as one feature of the chosen role: an existing layer
      of that role, or a new one named by the user (or by the role). */
  finish() {
    if (this.pts.length < 2) { toast('a trace needs at least two points', 'warn'); return null; }
    const f = this.form; const meta = ROLES[this.role]; let ls = this.target ? this.app.project.get(this.target) : null; const fresh = !ls;
    if (fresh) { ls = new GM.LineSet({ name: f.layerName || meta.layer, role: this.role, color: meta.color, group: meta.group }); ls.provenance = { method: 'traced by hand in the 3-D modeller', confidence: 'sketched', on: this.image ? this.image.name : 'ground (topography + 3 m)' }; }
    const feat = { name: f.name || `${f.type} ${ls.parts.length + 1}`, type: f.type, unit_a: f.unit_a, unit_b: f.unit_b, source: f.doc, page: f.page, confidence: 'sketched', traced_on: this.image ? this.image.name : 'ground', date: today() };
    ls.addPolyline(this.pts.map(p => p.slice()), feat);
    if (fresh) { this.app.project.add(ls); this.target = ls.id; } else this.app.refresh(ls);
    this.pts = []; this.app.R.clearOverlay(); this.app.status(`${ls.parts.length} trace${ls.parts.length > 1 ? 's' : ''} in ${ls.name} — again, or Esc`);
    if (this.T.active === this) this.T.showPanel(this.panel());
    toast(`${feat.name} traced into ${ls.name} (${meta.group}) as sketched${this.role !== 'lines' ? ' — the Structure tool can now derive orientations from it' : ''}`, 'ok', 5000);
    return ls;
  }
  cancel() { this.mode = null; this.pts = []; this.app.R.clearOverlay(); this.T.disarm(); this.T.showPanel(this.panel()); }
  removePart(ls, k) { const pts = ls.partXYZ(k), feat = ls.features[k]; this.app.destructive(`removed ${feat.name || feat.type || 'trace'} from ${ls.name}`, () => { ls.removePart(k); this.app.refresh(ls); this.T.showPanel(this.panel()); }, () => { ls.addPolyline(pts, feat); this.app.refresh(ls); this.T.showPanel(this.panel()); }); }
}

/* ============================================================ install */
/** Register the three tools with the tool shell (idempotent: a key that is
    already registered is left alone).  Returns { measure, notes, polyline }. */
export function installMoreTools(tools) {
  const out = {};
  const add = (key, Cls, meta) => { if (tools.all[key]) { out[key] = tools.all[key]; return; } const t = new Cls(tools); tools.register(key, t, meta); out[key] = t; };
  add('measure', MeasureTool, { label: 'Measure', hint: 'distances, bearings, plunges · m and ft' });
  add('notes', NoteTool, { label: 'Pin a note', hint: 'interpretation notes with their source' });
  add('polyline', PolylineTool, { label: 'Trace a fault / contact / vein', hint: 'a sketched polyline of the chosen role' });
  return out;
}
if (typeof window !== 'undefined' && window.gmApp && window.gmApp.tools && typeof window.gmApp.tools.register === 'function') installMoreTools(window.gmApp.tools);
