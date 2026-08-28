/* gm-tools.js — modelling tools for model3d.html:
   SectionTool (slice + 2-D section panel), WorkingsTool (maps -> 3-D workings),
   GeorefTool (scanned plans / sections as ImagePlanes), StratTool (pancake
   builder), BlocksTool (block model + variograms + kriging), ImplicitTool
   (RBF iso-surfaces).  Tools render their panel into #inspector and get
   pointer events from the viewer while active. */
import * as GM from './gm-core.js';
import * as E from './gm-engine.js';
import * as F from './gm-formats.js';
import { THREE, canvasTexture } from './gm-render.js';
import { h, clear, row, num, txt, sel, btn, range, note, kv, section, toast, modal, menu, colorInput, fmtNum, plotVariogram } from './gm-ui.js';
import { StructureTool, StereonetTool, FormTool } from './gm-struct-tools.js';

const $ = id => document.getElementById(id);

export class Tools {
  constructor(app) {
    this.app = app; this.active = null; this.panel = null;
    this.section = new SectionTool(this); this.workings = new WorkingsTool(this); this.georef = new GeorefTool(this);
    this.strat = new StratTool(this); this.blocks = new BlocksTool(this); this.implicit = new ImplicitTool(this);
    this.structure = new StructureTool(this); this.stereonet = new StereonetTool(this); this.form = new FormTool(this);
    this.all = { section: this.section, workings: this.workings, georef: this.georef, strat: this.strat, blocks: this.blocks, implicit: this.implicit, structure: this.structure, stereonet: this.stereonet, form: this.form };
  }
  get R() { return this.app.R; }
  get project() { return this.app.project; }
  onProject() { for (const t of Object.values(this.all)) t.onProject && t.onProject(); this.stop(); }
  menu(anchor) {
    menu(anchor, [
      { label: 'Structural data', hint: 'dip / dip azimuth', onclick: () => this.open('structure') },
      { label: 'Stereonet', hint: 'poles, contours, Bingham', onclick: () => this.open('stereonet') },
      { label: 'Form interpolant & trends', hint: 'deformation fabric', onclick: () => this.open('form') },
      '-',
      { label: 'Section & slice', hint: 'cut the model', onclick: () => this.open('section') },
      { label: 'Workings from maps', hint: 'adits, drifts, shafts, stopes', onclick: () => this.open('workings') },
      { label: 'Georeference an image', hint: 'level plan / section scan', onclick: () => this.open('georef') },
      { label: 'Stratigraphy (pancake)', hint: 'layered geologic model', onclick: () => this.open('strat') },
      { label: 'Block model & kriging', hint: 'grade estimation', onclick: () => this.open('blocks') },
      { label: 'Implicit surface (RBF)', hint: 'veins / contacts from points', onclick: () => this.open('implicit') },
      '-',
      { label: 'Virtual drillhole (click the ground)', onclick: () => this.strat.virtualHole() },
      { label: 'Derive structure from all mapped traces', hint: 'three-point problem', onclick: () => { this.open('structure'); this.structure.deriveAll(); } },
    ]);
  }
  open(name, ...args) { this.stop(); const t = this.all[name]; this.active = t; this.panel = t.panel(...args); this.app.select(null); showPanel(this.panel); }
  stop() { if (this.active && this.active.stop) this.active.stop(); this.active = null; this.R.clearOverlay(); $('gl').style.cursor = ''; }
  showPanel(p) { this.panel = p; showPanel(p); }
}
function showPanel(p) { const host = $('inspector'); clear(host); host.appendChild(p); }
function groundPick(R, e) { const p = R.pick(e.clientX, e.clientY, o => o.kind === 'grid2d' || o.kind === 'mesh' || o.kind === 'imageplane'); return p ? p.world : null; }

/* =========================================================== SECTION */
export class SectionTool {
  constructor(T) { this.T = T; this.sec = null; this.mode = null; this.pts = []; this.slice = false; this.side = 1; this.products = []; this.panelOpen = false; this.offset = 0; this.band = 25; }
  get app() { return this.T.app; }
  onProject() { this.sec = null; this.products = []; this.clear2d(); }
  panel(sec) {
    if (sec) this.sec = sec;
    if (!this.sec) this.sec = this.app.project.byKind('section')[0] || null;
    const P = h('div', { class: 'tool' }, h('h2', {}, 'SECTION & SLICE'));
    const list = this.app.project.byKind('section');
    P.appendChild(row('section', sel([['', '— pick —'], ...list.map(s => [s.id, s.name])], this.sec ? this.sec.id : '', { onchange: e => { this.sec = this.app.project.get(e.target.value); this.offset = 0; this.update(); this.T.showPanel(this.panel()); } })));
    P.appendChild(h('div', { class: 'frow' }, btn('DRAW LINE (2 clicks)', () => this.startDraw()), btn('W–E here', () => this.preset('we')), btn('S–N here', () => this.preset('sn'))));
    if (this.sec) {
      const s = this.sec; const L = Math.hypot(s.end[0] - s.start[0], s.end[1] - s.start[1]);
      P.appendChild(kv([['Name', s.name], ['Length', fmtNum(L, 0) + ' m'], ['Azimuth', fmtNum(Math.atan2(s.end[0] - s.start[0], s.end[1] - s.start[1]) * 180 / Math.PI % 360, 0) + '°'], ['Z range', `${fmtNum(s.zMin, 0)} – ${fmtNum(s.zMax, 0)}`]]));
      P.appendChild(row('offset', range(this.offset, -2000, 2000, 5, e => { this.offset = +e.target.value; this.update(); }, { style: { flex: 1 } }), h('span', { class: 'mono' }, ' m ⟂')));
      P.appendChild(row('clip model', sel([['0', 'no clipping'], ['1', 'hide front (look at the cut face)'], ['-1', 'hide back']], this.slice ? String(this.side) : '0', { onchange: e => { this.slice = e.target.value !== '0'; this.side = +e.target.value || 1; this.update(); } })));
      P.appendChild(row('band ± m', num(this.band, { onchange: e => { this.band = +e.target.value; this.update(); } })));
      P.appendChild(row('products', ...['lines', 'strat', 'blocks', 'near'].map(k => h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt(k), onchange: e => { this['show_' + k] = e.target.checked; this.update(); } }), { lines: 'surface cuts', strat: 'pancake fill', blocks: 'block slice', near: 'nearby workings' }[k]))));
      P.appendChild(h('div', { class: 'frow' }, btn('2-D SECTION PANEL', () => this.togglePanel()), btn('SAVE CUTS AS LAYERS', () => this.saveProducts()), btn('EXPORT DXF', () => this.exportDxf())));
      P.appendChild(h('div', { class: 'frow' }, btn('rename', () => { const n = prompt('Section name', s.name); if (n) { s.name = n; this.app.renderLayers(); } }), btn('delete section', () => { this.app.project.remove(s); this.sec = null; this.update(); this.T.showPanel(this.panel()); }, { class: 'b danger' })));
    }
    P.appendChild(note('The plane clips everything that is clippable, cuts every mesh and surface into lines, fills the stratigraphic units along the line, samples block models onto the plane and projects workings within the band. Sweep with the offset slider.'));
    this.update();
    return P;
  }
  opt(k) { return this['show_' + k] == null ? true : this['show_' + k]; }
  startDraw() { this.mode = 'draw'; this.pts = []; $('gl').style.cursor = 'crosshair'; this.app.status('click the start point of the section on the ground'); }
  preset(kind) {
    const topo = this.app.topoGrid(); const b = this.app.project.bounds(); if (!b) return; const cx = (b[0] + b[3]) / 2, cy = (b[1] + b[4]) / 2; const R = Math.max(b[3] - b[0], b[4] - b[1]) / 2;
    const t = this.app.R.controls.target; const w = this.app.R.fromScene(new THREE.Vector3(t.x, t.y, t.z)); const x = w[0], y = w[1];
    this.create(kind === 'we' ? [cx - R, y] : [x, cy - R], kind === 'we' ? [cx + R, y] : [x, cy + R], kind === 'we' ? 'Section W–E' : 'Section S–N');
  }
  create(start, end, name) {
    const b = this.app.project.bounds(); const zmin = b ? b[2] - 200 : -500, zmax = b ? b[5] + 50 : 500;
    const s = new GM.Section({ start, end, z_min: zmin, z_max: zmax, name: name || `Section ${this.app.project.byKind('section').length + 1}` });
    this.app.project.add(s); this.sec = s; this.offset = 0; this.update(); this.T.showPanel(this.panel());
  }
  onClick(e) {
    if (this.mode !== 'draw') return false;
    const w = groundPick(this.app.R, e); if (!w) return true;
    this.pts.push(w);
    if (this.pts.length === 1) { this.app.R.overlayMarker(w, 0x2dd4bf); this.app.status('click the end point'); }
    else { this.mode = null; $('gl').style.cursor = ''; this.app.R.clearOverlay(); this.create([this.pts[0][0], this.pts[0][1]], [this.pts[1][0], this.pts[1][1]]); }
    return true;
  }
  onMove(e) { if (this.mode !== 'draw' || this.pts.length !== 1) return false; const w = groundPick(this.app.R, e); if (!w) return true; this.app.R.clearOverlay(); this.app.R.overlayMarker(this.pts[0], 0x2dd4bf); this.app.R.overlayPolyline([this.pts[0], w], 0x2dd4bf, true); return true; }
  onKey(e) { if (e.key === 'Escape' && this.mode) { this.mode = null; this.app.R.clearOverlay(); $('gl').style.cursor = ''; return true; } return false; }
  stop() { this.mode = null; }
  plane() {
    if (!this.sec) return null; const s = this.sec; const { point, normal } = E.sectionPlane(s.start, s.end);
    const p = [point[0] + normal[0] * this.offset, point[1] + normal[1] * this.offset, 0];
    return { point: p, normal, start: [s.start[0] + normal[0] * this.offset, s.start[1] + normal[1] * this.offset], end: [s.end[0] + normal[0] * this.offset, s.end[1] + normal[1] * this.offset] };
  }
  async update() {
    const R = this.app.R; const pl = this.plane();
    // products group lives under the section layer
    for (const L of R.layers.values()) if (L.products) { for (const c of [...L.products.children]) { L.products.remove(c); if (c.geometry) c.geometry.dispose(); } }
    this.products = [];
    if (!pl) { R.setClip(null); this.draw2d(); return; }
    const s = this.sec; const Lsec = R.layers.get(s.id); if (Lsec) { Lsec.group.position.set(pl.normal[0] * this.offset, 0, -pl.normal[1] * this.offset); }
    R.setClip(pl.point, pl.normal, this.side, this.slice);
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
    }
    R.invalidate(); this.draw2d();
  }
  sampleTexture(pr) {
    const { smp, d } = pr; const w = smp.width, hgt = smp.height; if (!w || !hgt) return null; const c = document.createElement('canvas'); c.width = w; c.height = hgt; const ctx = c.getContext('2d'); const img = ctx.createImageData(w, hgt);
    const vals = smp.values; const cat = typeof vals[0] === 'string' || (Array.isArray(vals) && vals.some(v => typeof v === 'string'));
    let lo = Infinity, hi = -Infinity; if (!cat) { const L = this.app.R.layers.get(pr.bm.id); if (L && L.range) [lo, hi] = L.range; else for (const v of vals) { if (v !== v || v == null) continue; if (v < lo) lo = v; if (v > hi) hi = v; } }
    const cats = cat ? [...new Set(vals.filter(v => v))] : null;
    for (let i = 0; i < vals.length; i++) { const v = vals[i]; let col = null; if (cat) { if (v) col = GM.colormap(d.colormap || 'geology', cats.length > 1 ? cats.indexOf(v) / (cats.length - 1) : 0.5); } else if (v === v && v != null && (d.cutoff == null || v >= d.cutoff)) col = GM.colormap(d.colormap || 'turbo', hi > lo ? (v - lo) / (hi - lo) : 0.5); if (!col) continue; img.data[4 * i] = col[0]; img.data[4 * i + 1] = col[1]; img.data[4 * i + 2] = col[2]; img.data[4 * i + 3] = 235; }
    ctx.putImageData(img, 0, 0); pr.canvas = c; return canvasTexture(c);
  }
  saveProducts() { let n = 0; for (const pr of this.products) { if (pr.kind === 'line' || pr.kind === 'near') { pr.ls.name = `${this.sec.name} · ${pr.obj.name}`; pr.ls.group = 'Sections'; pr.ls.role = 'section'; this.app.project.add(pr.ls); n++; } if (pr.kind === 'ribbon') { pr.mesh.group = 'Sections'; pr.mesh.name = `${this.sec.name} · ${pr.mesh.name}`; this.app.project.add(pr.mesh); n++; } } toast(`${n} section layers saved`, 'ok'); }
  async exportDxf() { const objs = []; for (const pr of this.products) { if (pr.ls) objs.push(pr.ls); if (pr.mesh) objs.push(pr.mesh); } if (!objs.length) return toast('nothing to export', 'warn'); const files = await F.writeAs('dxf', objs, { basename: GM.slug(this.sec.name) }); for (const [n, v] of Object.entries(files)) GM.downloadBlob(new Blob([v]), n); }
  /* 2-D panel */
  togglePanel() { this.panelOpen = !this.panelOpen; $('sec2d').style.display = this.panelOpen ? 'flex' : 'none'; this.app.R.resize(); this.draw2d(); }
  clear2d() { const c = $('sec2dCanvas'); if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height); }
  draw2d() {
    if (!this.panelOpen) return; const c = $('sec2dCanvas'); const box = c.parentElement; c.width = box.clientWidth * (window.devicePixelRatio || 1); c.height = box.clientHeight * (window.devicePixelRatio || 1); const ctx = c.getContext('2d'); ctx.setTransform(window.devicePixelRatio || 1, 0, 0, window.devicePixelRatio || 1, 0, 0); const W = box.clientWidth, H = box.clientHeight; ctx.fillStyle = '#0b0e13'; ctx.fillRect(0, 0, W, H);
    const pl = this.plane(); if (!pl) { ctx.fillStyle = '#8a97a6'; ctx.fillText('no section', 20, 30); return; }
    const s = this.sec; const L = Math.hypot(pl.end[0] - pl.start[0], pl.end[1] - pl.start[1]); const u = [(pl.end[0] - pl.start[0]) / L, (pl.end[1] - pl.start[1]) / L];
    let zmin = s.zMin, zmax = s.zMax; const pad = { l: 56, r: 16, t: 18, b: 30 };
    const ve = this.app.R.ve; const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b; const scale = Math.min(pw / L, ph / ((zmax - zmin) * ve)); const X = d => pad.l + d * scale, Y = z => pad.t + ph - (z - zmin) * ve * scale;
    const along = p => (p[0] - pl.start[0]) * u[0] + (p[1] - pl.start[1]) * u[1];
    // grid + axes
    ctx.strokeStyle = '#1e2630'; ctx.lineWidth = 1; ctx.font = '10px ui-monospace, monospace'; ctx.fillStyle = '#8a97a6';
    const zstep = niceStep((zmax - zmin) / 6); for (let z = Math.ceil(zmin / zstep) * zstep; z <= zmax; z += zstep) { ctx.beginPath(); ctx.moveTo(pad.l, Y(z)); ctx.lineTo(X(L), Y(z)); ctx.stroke(); ctx.textAlign = 'right'; ctx.fillText(fmtNum(z, 0), pad.l - 6, Y(z) + 3); }
    const dstep = niceStep(L / 8); for (let d = 0; d <= L; d += dstep) { ctx.beginPath(); ctx.moveTo(X(d), pad.t); ctx.lineTo(X(d), Y(zmin)); ctx.stroke(); ctx.textAlign = 'center'; ctx.fillText(fmtNum(d, 0), X(d), Y(zmin) + 14); }
    ctx.textAlign = 'left'; ctx.fillStyle = '#c9d1d9'; ctx.fillText(`${s.name}${this.offset ? ` (offset ${this.offset} m)` : ''} — ${fmtNum(L, 0)} m, VE ×${ve.toFixed(1)}, looking ${lookDir(u)}`, pad.l + 6, 12);
    ctx.save(); ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
    for (const pr of this.products) {
      if (pr.kind === 'blocks' && pr.canvas) { const c4 = pr.smp.corners; const d0 = along(c4[0]), d1 = along(c4[1]); const z0 = c4[0][2], z1 = c4[3][2]; ctx.globalAlpha = 0.9; ctx.drawImage(pr.canvas, X(Math.min(d0, d1)), Y(Math.max(z0, z1)), Math.abs(X(d1) - X(d0)), Math.abs(Y(z1) - Y(z0))); ctx.globalAlpha = 1; }
    }
    for (const pr of this.products) if (pr.kind === 'ribbon') { const m = pr.mesh; ctx.fillStyle = rgba(m.color, 0.85); ctx.beginPath(); for (let t = 0; t < m.nTriangles; t++) { const [a, b, c2] = [m.triangles[3 * t], m.triangles[3 * t + 1], m.triangles[3 * t + 2]]; const pa = m.vertex(a), pb = m.vertex(b), pc = m.vertex(c2); ctx.moveTo(X(along(pa)), Y(pa[2])); ctx.lineTo(X(along(pb)), Y(pb[2])); ctx.lineTo(X(along(pc)), Y(pc[2])); ctx.closePath(); } ctx.fill(); }
    for (const pr of this.products) if (pr.kind === 'line' || pr.kind === 'near') { const ls = pr.ls; for (let k = 0; k < ls.parts.length; k++) { const f = ls.features[k] || {}; const col = pr.kind === 'near' && pr.obj.role === 'workings' ? (E.WORKING_TYPES[f.type] || E.WORKING_TYPES.unknown).color : (pr.color || pr.obj.color); ctx.strokeStyle = rgba(col, 1); ctx.lineWidth = pr.width || (pr.kind === 'near' ? 2.5 : 1.3); ctx.beginPath(); const pts = ls.partXYZ(k); pts.forEach((p, i) => { if (i) ctx.lineTo(X(along(p)), Y(p[2])); else ctx.moveTo(X(along(p)), Y(p[2])); }); ctx.stroke(); if (pr.kind === 'near' && f.name) { ctx.fillStyle = '#e6edf3'; ctx.fillText(f.name, X(along(pts[0])) + 4, Y(pts[0][2]) - 4); } } }
    // points near the plane
    for (const ps of this.app.project.byKind('points')) { if (ps.visible === false) continue; const nm = ps.attributes.name || []; for (let i = 0; i < ps.n; i++) { const p = ps.point(i); const dist = (p[0] - pl.point[0]) * pl.normal[0] + (p[1] - pl.point[1]) * pl.normal[1]; if (Math.abs(dist) > this.band * 4) continue; const d = along(p); if (d < 0 || d > L) continue; ctx.fillStyle = rgba(ps.color, 1); ctx.beginPath(); ctx.arc(X(d), Y(p[2]), 3.5, 0, Math.PI * 2); ctx.fill(); if (nm[i] && ps.role === 'mines') { ctx.fillStyle = '#ffd27a'; ctx.fillText(String(nm[i]).slice(0, 28), X(d) + 5, Y(p[2]) - 5); } } }
    ctx.restore();
  }
}
function niceStep(x) { const p = Math.pow(10, Math.floor(Math.log10(Math.max(x, 1e-9)))); const m = x / p; return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p; }
function lookDir(u) { const az = Math.atan2(u[0], u[1]) * 180 / Math.PI; const a = (az + 360) % 360; return `${['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(((a + 90) % 360) / 45) % 8]} (section runs ${['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(a / 45) % 8]}-ward)`; }
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

/* ========================================================== WORKINGS */
export class WorkingsTool {
  constructor(T) { this.T = T; this.ws = null; this.mode = null; this.pts = []; this.image = null; this.form = { type: 'drift', name: '', level: '', level_z: '', units_in: 'ft', confidence: 'sketched', doc: '', page: '', width_m: '', bearing: 0, length: 100, depth: 100, dip: 90, azimuth: 0, grade: 0.5, zTop: '', zBottom: '' }; }
  get app() { return this.T.app; }
  onProject() { this.ws = null; this.image = null; this.mode = null; }
  ensureLayer() { if (this.ws && this.app.project.get(this.ws.id)) return this.ws; this.ws = this.app.project.byKind('lineset').find(l => l.role === 'workings') || null; if (!this.ws) { this.ws = E.newWorkings('Workings (digitised)', this.app.project.name); this.ws.group = 'Workings'; this.app.project.add(this.ws); } return this.ws; }
  panel(ws, image) {
    if (ws) this.ws = ws; this.ensureLayer(); if (image) this.image = image;
    const f = this.form; const P = h('div', { class: 'tool' }, h('h2', {}, 'WORKINGS FROM MAPS'));
    const wsList = this.app.project.byKind('lineset').filter(l => l.role === 'workings'); const images = this.app.project.byKind('imageplane');
    P.appendChild(row('layer', sel(wsList.map(l => [l.id, l.name]), this.ws.id, { onchange: e => { this.ws = this.app.project.get(e.target.value); } }), btn('+', () => { const l = E.newWorkings(`Workings ${wsList.length + 1}`, this.app.project.name); l.group = 'Workings'; this.app.project.add(l); this.ws = l; this.T.showPanel(this.panel()); }, { title: 'new workings layer' })));
    P.appendChild(row('trace on', sel([['', 'ground / any surface'], ...images.map(i => [i.id, `${i.name} (${i.plane}${i.elevation != null ? ' @' + fmtNum(i.elevation, 0) : ''})`])], this.image ? this.image.id : '', { onchange: e => { this.image = e.target.value ? this.app.project.get(e.target.value) : null; if (this.image && this.image.plane === 'plan' && this.image.elevation != null) { f.level_z = this.image.elevation; this.T.showPanel(this.panel()); } } })));
    P.appendChild(note(this.image ? `tracing on "${this.image.name}" — clicks are read through its georeference${this.image.plane === 'plan' ? ' at the level elevation below' : ''}` : 'no image selected — clicks land on the topography / surfaces (use TOOLS > Georeference to place a scanned level plan first)'));
    const modes = [['trace', 'TRACE (drift / crosscut / level)'], ['adit', 'ADIT (portal + bearing + length)'], ['adit2', 'ADIT (click portal, click end)'], ['shaft', 'SHAFT / WINZE (collar + depth)'], ['raise', 'RAISE (click lower, click upper)'], ['stope', 'STOPE (outline + z range)']];
    P.appendChild(h('div', { class: 'modes' }, ...modes.map(([m, lbl]) => btn(lbl, () => this.start(m), { class: 'b' + (this.mode === m ? ' on' : '') }))));
    const frm = h('div', { class: 'psec' }, h('h3', {}, 'FEATURE'));
    frm.appendChild(row('type', sel(Object.keys(E.WORKING_TYPES).filter(t => t !== 'portal'), f.type, { onchange: e => { f.type = e.target.value; } })));
    frm.appendChild(row('name', txt(f.name, { placeholder: 'No. 2 adit / 300 level drift', oninput: e => { f.name = e.target.value; } })));
    frm.appendChild(row('level label', txt(f.level, { placeholder: '300', oninput: e => { f.level = e.target.value; } }), num(f.level_z, { placeholder: 'elev m', style: { width: '80px' }, oninput: e => { f.level_z = e.target.value; } })));
    frm.appendChild(row('map units', sel([['ft', 'feet'], ['m', 'metres']], f.units_in, { onchange: e => { f.units_in = e.target.value; } }), sel(E.CONFIDENCE, f.confidence, { onchange: e => { f.confidence = e.target.value; } })));
    frm.appendChild(row('source', txt(f.doc, { placeholder: 'document / plate', oninput: e => { f.doc = e.target.value; } }), txt(f.page, { placeholder: 'page', style: { width: '60px' }, oninput: e => { f.page = e.target.value; } })));
    frm.appendChild(row('opening w (m)', num(f.width_m, { placeholder: 'auto', oninput: e => { f.width_m = e.target.value; } })));
    const geo = h('div', { class: 'psec' }, h('h3', {}, 'GEOMETRY (for adit / shaft / stope)'));
    geo.appendChild(row('bearing °', num(f.bearing, { oninput: e => { f.bearing = +e.target.value; } }), h('span', { class: 'mono' }, 'length'), num(f.length, { oninput: e => { f.length = +e.target.value; } }), h('span', { class: 'mono' }, f.units_in)));
    geo.appendChild(row('grade %', num(f.grade, { oninput: e => { f.grade = +e.target.value; } }), h('span', { class: 'mono' }, 'depth'), num(f.depth, { oninput: e => { f.depth = +e.target.value; } })));
    geo.appendChild(row('dip °', num(f.dip, { oninput: e => { f.dip = +e.target.value; } }), h('span', { class: 'mono' }, 'azimuth'), num(f.azimuth, { oninput: e => { f.azimuth = +e.target.value; } })));
    geo.appendChild(row('stope z', num(f.zBottom, { placeholder: 'bottom', oninput: e => { f.zBottom = e.target.value; } }), num(f.zTop, { placeholder: 'top', oninput: e => { f.zTop = e.target.value; } })));
    P.appendChild(frm); P.appendChild(geo);
    // feature list
    const list = h('div', { class: 'psec' }, h('h3', {}, `FEATURES (${this.ws.parts.length}) · ${fmtNum(this.ws.length(), 0)} m`));
    this.ws.features.forEach((ft, k) => list.appendChild(h('div', { class: 'feat' }, h('span', { class: 'sw', style: { background: rgba((E.WORKING_TYPES[ft.type] || E.WORKING_TYPES.unknown).color, 1) } }), h('span', { class: 'fn' }, `${ft.type} ${ft.name || ''} ${ft.level ? '· L' + ft.level : ''}`), h('span', { class: 'fl' }, fmtNum(this.ws.length(k), 0) + ' m'), btn('✕', () => { this.ws.removePart(k); this.app.refresh(this.ws); this.T.showPanel(this.panel()); }, { class: 'x' }))));
    const stopes = this.app.project.byKind('mesh').filter(m => m.role === 'stope'); if (stopes.length) list.appendChild(note(`${stopes.length} stope volume(s) are separate mesh layers`));
    P.appendChild(list);
    P.appendChild(h('div', { class: 'frow' }, btn('UNDO LAST POINT', () => { this.pts.pop(); this.preview(); }), btn('FINISH (Enter)', () => this.finish()), btn('CANCEL (Esc)', () => this.cancel())));
    P.appendChild(h('div', { class: 'frow' }, btn('SEND FOOTPRINT TO MAP', () => this.sendToMap(this.ws)), btn('EXPORT DXF', () => this.app.exportObjects('dxf', [this.ws])), btn('EXPORT GEOJSON', () => this.app.exportObjects('geojson', [this.ws]))));
    P.appendChild(note('Workflow: georeference the level plan (TOOLS > Georeference) at its level elevation → TRACE drifts on it → ADIT from the portal on the surface (bearing/length from the map, or click portal + end) → SHAFT from its collar (depth, dip) → RAISEs between levels → STOPEs as outlines with a z range. Old US maps are in feet: keep "feet" and everything converts once.'));
    return P;
  }
  start(mode) { this.mode = mode; this.pts = []; $('gl').style.cursor = 'crosshair'; this.app.R.clearOverlay(); this.app.status({ trace: 'click along the working (double-click or Enter to finish)', adit: 'click the portal on the surface', adit2: 'click the portal, then the inner end', shaft: 'click the collar', raise: 'click the lower end, then the upper end', stope: 'click the outline corners (Enter to close)' }[mode]); this.T.showPanel(this.panel()); }
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
    const common = { name: f.name, level: f.level, level_z: f.level_z === '' ? null : +f.level_z, mine: this.app.project.name, source: f.doc ? { doc: f.doc, page: f.page } : {}, confidence: f.confidence, units_in: f.units_in, width_m: f.width_m === '' ? undefined : +f.width_m };
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
  cancel() { this.mode = null; this.pts = []; this.app.R.clearOverlay(); $('gl').style.cursor = ''; this.T.showPanel(this.panel()); }
  stop() { this.mode = null; this.pts = []; }
  async sendToMap(ws) {
    try { const fc = E.workingsToGeoJSON(ws, this.app.project.crs); if (!fc.features.length) return toast('no workings yet', 'warn'); const slug = 'workings-' + GM.slug(this.app.project.name); await GM.store.putUserLayer({ slug, name: `${this.app.project.name} workings (3-D model)`, added: new Date().toISOString().slice(0, 10), n: fc.features.length, misses: 0, visible: true, fc }); toast(`sent ${fc.features.length} workings to the map — reload the map and look under MY DATA`, 'ok', 7000); }
    catch (e) { toast('send failed: ' + e.message, 'err'); }
  }
}

/* ============================================================ GEOREF */
export class GeorefTool {
  constructor(T) { this.T = T; this.img = null; this.markers = []; this.mode = null; this.pending = null; this.plane = 'plan'; this.zTop = ''; this.zBottom = ''; this.elev = ''; this.name = ''; }
  get app() { return this.T.app; }
  onProject() { this.img = null; this.markers = []; }
  async fromImageBytes(name, bytes) { const blob = new Blob([bytes]); const url = URL.createObjectURL(blob); const bmp = await createImageBitmap(blob); const c = document.createElement('canvas'); const scale = Math.min(1, 4096 / Math.max(bmp.width, bmp.height)); c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale); c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height); URL.revokeObjectURL(url); this.setImage(name, c); }
  async fromPdf(name, bytes) {
    try { const pdfjs = await import('../pdfjs/pdf.min.mjs'); pdfjs.GlobalWorkerOptions.workerSrc = 'assets/pdfjs/pdf.worker.min.mjs'; const doc = await pdfjs.getDocument({ data: bytes }).promise; const pageNo = Math.max(1, Math.min(doc.numPages, +(prompt(`PDF has ${doc.numPages} pages — which page holds the map?`, '1') || 1))); const page = await doc.getPage(pageNo); const vp = page.getViewport({ scale: 1 }); const scale = Math.min(3, 3000 / Math.max(vp.width, vp.height)); const v2 = page.getViewport({ scale }); const c = document.createElement('canvas'); c.width = Math.round(v2.width); c.height = Math.round(v2.height); await page.render({ canvasContext: c.getContext('2d'), viewport: v2 }).promise; this.setImage(`${name} p${pageNo}`, c); }
    catch (e) { toast('PDF render failed (is assets/pdfjs present?): ' + e.message, 'err', 8000); }
  }
  setImage(name, canvas) { this.img = { name, canvas, dataUrl: canvas.toDataURL('image/jpeg', 0.85), width: canvas.width, height: canvas.height }; this.markers = []; this.name = name.replace(/\.[^.]+$/, ''); this.T.open('georef'); }
  panel(existing) {
    if (existing) { this.existing = existing; this.img = { name: existing.name, dataUrl: existing.image, width: existing.width, height: existing.height, canvas: null }; this.plane = existing.plane; this.elev = existing.elevation == null ? '' : existing.elevation; this.zTop = existing.zTop == null ? '' : existing.zTop; this.zBottom = existing.zBottom == null ? '' : existing.zBottom; this.name = existing.name; this.markers = existing.plane === 'plan' ? existing.control.map(c => ({ px: c[0], py: c[1], X: c[2], Y: c[3] })) : [{ px: 0, py: 0, X: existing.p1[0], Y: existing.p1[1] }, { px: existing.width, py: 0, X: existing.p2[0], Y: existing.p2[1] }]; }
    const P = h('div', { class: 'tool' }, h('h2', {}, 'GEOREFERENCE AN IMAGE'));
    if (!this.img) { P.appendChild(note('Drop a PNG/JPG/PDF of a level plan, longitudinal section or geologic map onto the page, or:')); P.appendChild(btn('CHOOSE IMAGE / PDF…', () => $('fileIn').click())); P.appendChild(note('A level plan becomes a horizontal plane at its level elevation; a section becomes a vertical plane between two surface points. Then trace workings on it (TOOLS > Workings).')); return P; }
    P.appendChild(row('name', txt(this.name, { oninput: e => { this.name = e.target.value; } })));
    P.appendChild(row('kind', sel([['plan', 'PLAN (horizontal: level plan, map)'], ['section', 'SECTION (vertical: long / cross section)']], this.plane, { onchange: e => { this.plane = e.target.value; this.markers = []; this.T.showPanel(this.panel()); } })));
    if (this.plane === 'plan') P.appendChild(row('elevation m', num(this.elev, { placeholder: 'level elevation (blank = drape on topography)', oninput: e => { this.elev = e.target.value; } })));
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
        btn('PICK', () => { this.mode = 'pick'; this.pending = m; $('gl').style.cursor = 'crosshair'; this.app.status(`click in the scene where marker ${i + 1} is`); }, { title: 'click the spot in the 3-D scene' }), btn('✕', () => { this.markers.splice(i, 1); this.T.showPanel(this.panel()); }, { class: 'x' })));
    });
    P.appendChild(h('div', { class: 'frow' }, btn(this.existing ? 'UPDATE IMAGE PLANE' : 'CREATE IMAGE PLANE', () => this.commit()), btn('SCALE BAR + ONE POINT…', () => this.scaleBar()), btn('cancel', () => { this.existing = null; this.T.stop(); this.app.select(null); })));
    return P;
  }
  onClick(e) { if (this.mode !== 'pick' || !this.pending) return false; const w = groundPick(this.app.R, e); if (!w) return true; this.pending.X = +w[0].toFixed(2); this.pending.Y = +w[1].toFixed(2); if (this.plane === 'section') { if (this.zTop === '') this.zTop = +w[2].toFixed(1); } else if (this.elev === '' && this.markers.indexOf(this.pending) === 0) { /* keep drape */ } this.mode = null; this.pending = null; $('gl').style.cursor = ''; this.T.showPanel(this.panel()); return true; }
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
      if (this.existing) { this.app.refresh(ip); toast('image plane updated', 'ok'); } else { this.app.project.add(ip); toast('image plane created — TOOLS > Workings to trace on it', 'ok', 5000); }
      const b = ip.bounds(); if (b) this.app.R.fitTo(b);
      this.existing = null; this.T.open('workings', null, ip);
    } catch (e) { toast(e.message, 'err'); }
  }
}

/* ============================================================= STRAT */
export class StratTool {
  constructor(T) { this.T = T; this.sm = null; this.busy = false; this.mode = null; }
  get app() { return this.T.app; }
  onProject() { this.sm = null; }
  ensure() { if (this.sm && this.app.project.get(this.sm.id)) return this.sm; this.sm = this.app.project.byKind('stratmodel')[0] || null; if (!this.sm) { const topo = this.app.topoGrid(); this.sm = new GM.StratModel({ name: 'Stratigraphy (pancake)', topography: topo ? topo.id : null }); this.app.project.add(this.sm); } return this.sm; }
  panel(sm) {
    if (sm) this.sm = sm; const S = this.ensure(); const P = h('div', { class: 'tool' }, h('h2', {}, 'STRATIGRAPHY — PANCAKE MODEL'));
    const topo = this.app.topoGrid(); if (!topo) { P.appendChild(note('needs a topography grid (import one, or open the model from a mine card)', 'note warn')); return P; }
    const pointLayers = this.app.project.byKind('points'), gridLayers = this.app.project.byKind('grid2d').filter(g => g.role !== 'topography' && g.role !== 'property');
    P.appendChild(note('Units are listed youngest (top) first. Each unit needs the surface at its BASE: contact points (interpolated), an existing surface grid, or a constant elevation. The last unit is basement (no base). Deposit bases on-lap older units; erosion bases cut them.'));
    const list = h('div', { class: 'units' });
    S.units.forEach((u, i) => {
      const src = u.source || { kind: u.base ? 'grid' : 'none', id: u.base || '', value: '' };
      u.source = src;
      const srcSel = sel([['none', 'basement (no base)'], ['points', 'contact points layer'], ['grid', 'surface grid'], ['const', 'constant elevation'], ['thickness', 'constant thickness below unit above']], src.kind, { onchange: e => { src.kind = e.target.value; this.T.showPanel(this.panel()); } });
      const idSel = src.kind === 'points' ? sel(pointLayers.map(l => [l.id, l.name]), src.id, { onchange: e => { src.id = e.target.value; } }) : src.kind === 'grid' ? sel(gridLayers.map(l => [l.id, l.name]), src.id, { onchange: e => { src.id = e.target.value; } }) : (src.kind === 'const' || src.kind === 'thickness') ? num(src.value, { placeholder: src.kind === 'const' ? 'elevation m' : 'thickness m', oninput: e => { src.value = e.target.value; } }) : null;
      list.appendChild(h('div', { class: 'unit' },
        h('div', { class: 'frow' }, colorInput(u.color || E.DEFAULT_COLORS[i % E.DEFAULT_COLORS.length], c => { u.color = c; }), txt(u.name, { placeholder: 'unit name', oninput: e => { u.name = e.target.value; } }), sel([['deposit', 'deposit'], ['erosion', 'erosion']], u.contact || 'deposit', { onchange: e => { u.contact = e.target.value; } }), btn('▲', () => { if (i > 0) { S.units.splice(i - 1, 2, S.units[i], S.units[i - 1]); this.T.showPanel(this.panel()); } }, { class: 'x' }), btn('▼', () => { if (i < S.units.length - 1) { S.units.splice(i, 2, S.units[i + 1], S.units[i]); this.T.showPanel(this.panel()); } }, { class: 'x' }), btn('✕', () => { S.units.splice(i, 1); this.T.showPanel(this.panel()); }, { class: 'x' })),
        h('div', { class: 'frow' }, srcSel, idSel, txt(u.lithology || '', { placeholder: 'lithology', oninput: e => { u.lithology = e.target.value; } }))));
    });
    P.appendChild(list);
    P.appendChild(h('div', { class: 'frow' }, btn('+ UNIT', () => { S.units.push({ name: `Unit ${S.units.length + 1}`, color: E.DEFAULT_COLORS[S.units.length % E.DEFAULT_COLORS.length], contact: 'deposit', source: { kind: S.units.length ? 'const' : 'points', id: pointLayers[0] ? pointLayers[0].id : '', value: '' } }); this.T.showPanel(this.panel()); }), btn('+ BASEMENT', () => { S.units.push({ name: 'Basement', color: [140, 140, 140], contact: 'deposit', source: { kind: 'none' } }); this.T.showPanel(this.panel()); }), btn('DIGITISE CONTACT POINTS', () => this.digitise())));
    const method = sel([['rbf', 'RBF (thin-plate, linear drift)'], ['ok', 'ordinary kriging (auto variogram)'], ['idw', 'inverse distance'], ['nn', 'nearest']], this.method || 'rbf', { onchange: e => { this.method = e.target.value; } });
    const res = num(this.res || 60, { style: { width: '70px' }, oninput: e => { this.res = +e.target.value; } });
    P.appendChild(row('interpolation', method)); P.appendChild(row('lattice nodes', res, h('span', { class: 'mono' }, '× per side')));
    P.appendChild(row('outputs', h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.volumes !== false, onchange: e => { this.volumes = e.target.checked; } }), 'unit volumes (closed meshes)')));
    P.appendChild(h('div', { class: 'frow' }, btn(this.busy ? 'BUILDING…' : 'BUILD STRATIGRAPHY', () => this.build(), { disabled: this.busy }), btn('VIRTUAL DRILLHOLE', () => this.virtualHole()), btn('TAG BLOCK MODEL', () => this.tagBlocks())));
    if (S.metadata.built) P.appendChild(note(`built ${S.metadata.built.at} · ${S.metadata.built.units} units · lattice ${S.metadata.built.lattice}`));
    return P;
  }
  digitise() { const ps = new GM.PointSet({ name: `contact points ${this.app.project.byKind('points').length + 1}`, role: 'contacts', group: 'Stratigraphy', color: [255, 120, 200] }); this.app.project.add(ps); this.mode = 'digitise'; this.target = ps; $('gl').style.cursor = 'crosshair'; this.app.status('click contact points on the terrain / section images / surfaces (Esc to stop); set the base of a unit to this layer'); }
  onClick(e) { if (this.mode !== 'digitise') return false; const w = groundPick(this.app.R, e); if (!w) return true; this.target.add(w[0], w[1], w[2], { n: this.target.n + 1 }); this.app.refresh(this.target); this.app.status(`${this.target.n} contact points`); return true; }
  onKey(e) { if (e.key === 'Escape' && this.mode) { this.mode = null; $('gl').style.cursor = ''; this.T.showPanel(this.panel()); return true; } return false; }
  stop() { this.mode = null; }
  async build() {
    const S = this.ensure(); const topo = this.app.topoGrid(); if (!topo) return; this.busy = true; this.T.showPanel(this.panel());
    try {
      // lattice: downsample topography to <= res nodes per side
      const res = this.res || 60; const stride = Math.max(1, Math.ceil(Math.max(topo.nx, topo.ny) / res));
      const lat = new GM.Grid2D({ nx: Math.ceil(topo.nx / stride), ny: Math.ceil(topo.ny / stride), x0: topo.x0, y0: topo.y0, dx: topo.dx * stride, dy: topo.dy * stride, rotation: topo.rotation, name: 'lattice' });
      for (let j = 0; j < lat.ny; j++) for (let i = 0; i < lat.nx; i++) { const [x, y] = lat.nodeXY(i, j); lat.values[j * lat.nx + i] = topo.sample(x, y); }
      const units = []; let prevBase = null;
      for (const u of S.units) {
        const src = u.source || {}; let base = null;
        if (src.kind === 'points') { const ps = this.app.project.get(src.id); if (!ps) throw new Error(`unit ${u.name}: points layer missing`); base = GM.packObject(ps); }
        else if (src.kind === 'grid') { const g = this.app.project.get(src.id); if (!g) throw new Error(`unit ${u.name}: grid missing`); base = GM.packObject(g); }
        else if (src.kind === 'const') base = +src.value;
        else if (src.kind === 'thickness') { const th = +src.value; const above = prevBase || GM.packObject(lat); const g = GM.unpackObject(above instanceof Object && above.kind ? above : GM.packObject(lat)); const gg = g.copyEmpty(); for (let k = 0; k < gg.values.length; k++) gg.values[k] = g.values[k] - th; base = GM.packObject(gg); }
        units.push({ name: u.name, color: u.color, lithology: u.lithology, contact: u.contact || 'deposit', base });
        if (base && typeof base === 'object') prevBase = base;
      }
      const r = await this.app.engine.call('buildStratigraphy', { topo: GM.packObject(lat), units, method: this.method || 'rbf' }, (f, n) => this.app.status(`building stratigraphy ${(f * 100) | 0}% ${n || ''}`));
      // remove old outputs of this model
      for (const o of this.app.project.objects.slice()) if (o.metadata && o.metadata.strat_of === S.id) this.app.project.remove(o);
      const bases = r.bases; const strat = r.strat; const grids = {};
      bases.forEach((g, i) => { if (!g) return; g.group = 'Stratigraphy'; g.metadata.strat_of = S.id; g.name = `${S.units[i].name} base`; g.color = S.units[i].color; this.app.project.add(g); grids[g.id] = g; });
      S.units.forEach((u, i) => { u.base = bases[i] ? bases[i].id : null; u.contact = strat.units[i].contact; });
      S.topography = topo.id; S.metadata.built = { at: new Date().toISOString().slice(0, 16), units: S.units.length, lattice: `${lat.nx}×${lat.ny}` }; S.metadata.lattice_id = null;
      if (this.volumes !== false) { const vols = await this.app.engine.call('stratigraphyVolumes', { strat: GM.packObject(S), grids: Object.fromEntries(Object.entries(grids).map(([k, g]) => [k, GM.packObject(g)])), topo: GM.packObject(r.topo || lat) }); vols.forEach((m, i) => { m.group = 'Stratigraphy'; m.metadata.strat_of = S.id; m.opacity = 0.9; if (i === vols.length - 1 && S.units[i] && !S.units[i].base) m.visible = false; this.app.project.add(m); }); }
      this.app.refresh(S); this.T.section.update(); toast('stratigraphy built — open a section to see the pancake fill', 'ok', 5000);
    } catch (e) { console.error(e); toast('build failed: ' + e.message, 'err', 8000); }
    this.busy = false; this.T.showPanel(this.panel());
  }
  virtualHole() { this.mode = 'vhole'; $('gl').style.cursor = 'crosshair'; this.app.status('click the ground for a virtual drillhole column'); this.T.active = this; const T = this; this.onClick = e => { if (T.mode !== 'vhole') return false; const w = groundPick(T.app.R, e); if (!w) return true; T.showColumn(w); return true; }; }
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
    // 2. samples + variogram
    const s = h('div', { class: 'psec' }, h('h3', {}, '2 · SAMPLES & VARIOGRAM'));
    const pl = this.app.project.byKind('points'); const ps = this.app.project.get(p.samples) || pl.find(l => l.role === 'samples') || pl[0]; if (ps) p.samples = ps.id;
    s.appendChild(row('samples', sel(pl.map(l => [l.id, `${l.name} (${l.n})`]), p.samples, { onchange: e => { p.samples = e.target.value; p.value = ''; this.exp = null; this.T.showPanel(this.panel()); } })));
    const cols = ps ? Object.keys(ps.attributes).filter(k => ps.isNumeric(k)) : []; if (ps && !p.value) p.value = cols.find(c => /^(au|ag|cu|pb|zn|grade|value|v|z)$/i.test(c)) || cols[0] || '';
    s.appendChild(row('value column', sel(cols, p.value, { onchange: e => { p.value = e.target.value; this.exp = null; this.T.showPanel(this.panel()); } })));
    s.appendChild(row('lags / size', num(p.nLags, { oninput: e => { p.nLags = +e.target.value; } }), num(p.lagSize, { placeholder: 'auto', oninput: e => { p.lagSize = e.target.value; } }), btn('COMPUTE', () => this.variogram())));
    const cv = document.createElement('canvas'); cv.width = 300; cv.height = 170; cv.className = 'vplot'; s.appendChild(cv); this.canvas = cv;
    s.appendChild(row('model', sel(E.VARIOGRAM_MODELS.filter(m => m !== 'nugget'), p.model, { onchange: e => { p.model = e.target.value; this.plot(); } }), btn('AUTO-FIT', () => this.fit())));
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
    est.appendChild(h('div', { class: 'frow' }, btn(this.busy ? 'RUNNING…' : 'RUN ESTIMATE', () => this.run(), { disabled: this.busy || !this.bm }), btn('GRADE–TONNAGE', () => this.gt(), { disabled: !this.bm })));
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
  samples() { const p = this.params; const ps = this.app.project.get(p.samples); if (!ps || !p.value) throw new Error('pick a sample layer and value column'); return ps; }
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
  constructor(T) { this.T = T; this.params = { kernel: 'thin_plate', drift: 'linear', smoothing: 0, spacing: '', valueCol: '', offset: 10 }; this.busy = false; this.mode = null; }
  get app() { return this.T.app; }
  panel() {
    const p = this.params; const P = h('div', { class: 'tool' }, h('h2', {}, 'IMPLICIT SURFACE (RBF)'));
    const pl = this.app.project.byKind('points'); const ps = this.app.project.get(p.points) || pl.find(l => l.role === 'contacts') || pl[0]; if (ps) p.points = ps.id;
    P.appendChild(note('Leapfrog-style implicit modelling: an RBF fits signed distances — 0 on the contact, positive on one side, negative on the other — and the zero iso-surface is the contact / vein / shell. Give a signed-distance column, or let the tool derive ±offset points from a "side" column (hw/fw, in/out, above/below, +/-).'));
    P.appendChild(row('points', sel(pl.map(l => [l.id, `${l.name} (${l.n})`]), p.points, { onchange: e => { p.points = e.target.value; this.T.showPanel(this.panel()); } })));
    const cols = ps ? Object.keys(ps.attributes) : [];
    P.appendChild(row('value column', sel([['', '— all points are on the contact (needs a side column or digitised ± points) —'], ...cols], p.valueCol, { onchange: e => { p.valueCol = e.target.value; } })));
    P.appendChild(row('side column', sel([['', '—'], ...cols], p.sideCol || '', { onchange: e => { p.sideCol = e.target.value; } }), num(p.offset, { title: 'offset distance for side points (m)', oninput: e => { p.offset = +e.target.value; } })));
    P.appendChild(row('kernel', sel(E.RBF_KERNELS, p.kernel, { onchange: e => { p.kernel = e.target.value; } }), sel(['none', 'constant', 'linear'], p.drift, { onchange: e => { p.drift = e.target.value; } })));
    P.appendChild(row('smoothing', num(p.smoothing, { oninput: e => { p.smoothing = +e.target.value; } }), h('span', { class: 'mono' }, 'grid m'), num(p.spacing, { placeholder: 'auto', oninput: e => { p.spacing = e.target.value; } })));
    P.appendChild(h('div', { class: 'frow' }, btn('DIGITISE ± POINTS', () => this.digitise()), btn(this.busy ? 'BUILDING…' : 'BUILD SURFACE', () => this.build(), { disabled: this.busy })));
    return P;
  }
  digitise() { const ps = new GM.PointSet({ name: `implicit points ${this.app.project.byKind('points').length + 1}`, role: 'contacts', group: 'Surfaces', color: [255, 120, 200] }); this.app.project.add(ps); this.params.points = ps.id; this.params.valueCol = 'sd'; this.mode = 'dig'; this.sign = 0; $('gl').style.cursor = 'crosshair'; this.app.status('click contact points (0); press + / - before a click for above/below points; Esc to stop'); this.T.showPanel(this.panel()); }
  onKey(e) { if (!this.mode) return false; if (e.key === '+' || e.key === '=') { this.sign = 1; this.app.status('next click: POSITIVE side'); return true; } if (e.key === '-') { this.sign = -1; this.app.status('next click: NEGATIVE side'); return true; } if (e.key === '0') { this.sign = 0; this.app.status('next click: on contact'); return true; } if (e.key === 'Escape') { this.mode = null; $('gl').style.cursor = ''; return true; } return false; }
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
      const mesh = await this.app.engine.call('implicitSurface', { points: Float64Array.from(xyz), values: Float64Array.from(vals), kernel: p.kernel, drift: p.drift, smoothing: p.smoothing, spacing: p.spacing === '' ? null : +p.spacing, name: `${ps.name} surface`, color: [220, 120, 60] }, (f, n) => this.app.status(`implicit ${(f * 100) | 0}% ${n || ''}`));
      mesh.group = 'Surfaces'; mesh.role = 'contact'; mesh.provenance = { method: 'RBF implicit', points: ps.name, kernel: p.kernel }; this.app.project.add(mesh); this.app.select(mesh.id); toast(`surface built: ${mesh.nTriangles} triangles`, 'ok');
    } catch (e) { console.error(e); toast('implicit build failed: ' + e.message, 'err', 8000); }
    this.busy = false; this.T.showPanel(this.panel());
  }
}
