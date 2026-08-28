/* gm-struct-tools.js — the structural tools for model3d.html.

     StructureTool   create / digitise / derive / drape / decluster planar
                     structural data, with the map trace -> dip pathway that
                     lets a district with no drilling still carry orientation
     StereonetTool   lower-hemisphere net: poles, great circles, Kamb /
                     exponential Kamb / Schmidt contouring, Bingham + Fisher
                     statistics, and selection linked both ways to the scene
     FormTool        form interpolant (gradient RBF) -> form surfaces and form
                     lines; structural trend field; global trend plane

   Panels render into #inspector and receive pointer events from the viewer
   while active, exactly like the tools in gm-tools.js.                       */

import * as GM from './gm-core.js';
import * as E from './gm-engine.js';
import * as S from './gm-structural.js';
import { h, clear, row, num, txt, sel, btn, range, note, kv, section, toast, modal, menu, colorInput, fmtNum } from './gm-ui.js';

const $ = id => document.getElementById(id);
const CAT_COLORS = [[104, 176, 255], [244, 162, 97], [138, 201, 38], [231, 111, 81], [187, 148, 255], [42, 196, 179], [255, 209, 102], [148, 163, 184], [214, 93, 177], [125, 211, 252]];

function structLayers(project) { return project ? project.objects.filter(o => S.isStructural(o) && o.role !== 'trend') : []; }
function traceLayers(project) { return project ? project.objects.filter(o => o.kind === 'lineset' && ['geology-outline', 'faults', 'lines'].includes(o.role) && o.parts.length) : []; }
function groundPick(R, e) { const p = R.pick(e.clientX, e.clientY, o => o.kind === 'grid2d' || o.kind === 'mesh' || o.kind === 'imageplane'); return p ? p.world : null; }
function toScreen(R, x, y, z) {
  const v = R.toScene(x, y, z); v.y *= R.ve; v.project(R.camera);
  const r = R.canvas.getBoundingClientRect();
  return [(v.x * 0.5 + 0.5) * r.width + r.left, (-v.y * 0.5 + 0.5) * r.height + r.top, v.z];
}
function bearing(a, b) { const d = Math.atan2(b[0] - a[0], b[1] - a[1]) * 180 / Math.PI; return (d % 360 + 360) % 360; }

/* ================================================== STRUCTURAL DATA === */

export class StructureTool {
  constructor(T) { this.T = T; this.layer = null; this.mode = null; this.pending = null; this.form = { dip: 30, dipaz: 90, polarity: 1, type: 'bedding', confidence: 'sketched', source: '', page: '' }; }
  get app() { return this.T.app; }
  onProject() { this.layer = null; this.mode = null; this.pending = null; }
  stop() { this.mode = null; this.pending = null; this.T.R.clearOverlay(); $('gl').style.cursor = ''; }

  panel(layer) {
    const P = this.app.project;
    if (layer) this.layer = layer;
    const list = structLayers(P);
    if (!this.layer || !P.get(this.layer.id)) this.layer = list[0] || null;
    const el = h('div', { class: 'tool' }, h('h2', {}, 'STRUCTURAL DATA'));

    el.appendChild(row('layer', sel([['', list.length ? '— pick —' : '— none yet —'], ...list.map(o => [o.id, `${o.name} (${o.n})`])], this.layer ? this.layer.id : '', {
      onchange: e => { this.layer = P.get(e.target.value) || null; this.repanel(); }
    }), btn('+', () => this.newLayer(), { title: 'new structural layer' })));

    /* ---------------- digitise ---------------- */
    const dig = section('DIGITISE ON THE MAP');
    dig.appendChild(note('Rotate so the mapped dip tick points to the top of the screen, then check the azimuth below matches the symbol — a reading 180° out means you are looking at the measurement backwards.'));
    dig.appendChild(h('div', { class: 'modes' },
      btn('POINT + DOWN-DIP (2 clicks)', () => this.startDigitise('two'), { class: 'b' + (this.mode === 'two' ? ' on' : '') }),
      btn('POINT ONLY (type azimuth)', () => this.startDigitise('one'), { class: 'b' + (this.mode === 'one' ? ' on' : '') })));
    dig.appendChild(row('dip °', num(this.form.dip, { min: 0, max: 90, onchange: e => { this.form.dip = Math.max(0, Math.min(90, +e.target.value)); } })));
    dig.appendChild(row('dip azimuth °', num(this.form.dipaz, { min: 0, max: 360, onchange: e => { this.form.dipaz = (+e.target.value % 360 + 360) % 360; } }), btn('FROM VIEW', () => { this.form.dipaz = this.viewAzimuth(); this.repanel(); }, { title: 'take the azimuth you are currently looking along' })));
    dig.appendChild(row('polarity', sel([[1, 'right way up'], [-1, 'overturned']], this.form.polarity, { onchange: e => { this.form.polarity = +e.target.value; } })));
    dig.appendChild(row('type', txt(this.form.type, { onchange: e => { this.form.type = e.target.value; } })));
    dig.appendChild(row('confidence', sel(['surveyed', 'sketched', 'inferred', 'described'], this.form.confidence, { onchange: e => { this.form.confidence = e.target.value; } })));
    dig.appendChild(row('source', txt(this.form.source, { placeholder: 'map / report', onchange: e => { this.form.source = e.target.value; } }), num(this.form.page, { placeholder: 'page', style: { maxWidth: '70px' }, onchange: e => { this.form.page = e.target.value; } })));
    if (this.mode) dig.appendChild(note(this.mode === 'two' ? 'Click the measurement location, then click a point in the DOWN-DIP direction. Esc cancels.' : 'Click the measurement location. Esc cancels.', 'note warn'));
    el.appendChild(dig);

    /* ---------------- derive from traces ---------------- */
    const der = section('DERIVE FROM MAP TRACES');
    const traces = traceLayers(P);
    if (!traces.length) der.appendChild(note('No mapped contact or fault traces in this project yet.'));
    else {
      der.appendChild(note('Fits a plane through each window of a contact or fault trace where it crosses terrain \u2014 the three-point problem, run along the whole line, growing the window until the plane is resolvable. Windows without relief, straight in map view, or with a poor fit are rejected, never guessed. A genuinely vertical structure also traces a straight line, so it comes back indeterminate rather than as 90\u00b0.'));
      this.derSel = this.derSel || traces[0].id;
      der.appendChild(row('trace layer', sel(traces.map(o => [o.id, `${o.name} (${o.parts.length})`]), this.derSel, { onchange: e => { this.derSel = e.target.value; } })));
      this.der = this.der || Object.assign({}, S.DERIVE_DEFAULTS);
      der.appendChild(row('window m', num(this.der.window, { onchange: e => { this.der.window = +e.target.value; } }), h('span', { class: 'mono' }, 'step'), num(this.der.step, { onchange: e => { this.der.step = +e.target.value; } })));
      der.appendChild(row('min relief m', num(this.der.min_relief, { onchange: e => { this.der.min_relief = +e.target.value; } })));
      der.appendChild(row('min spread m', num(this.der.min_spread, { onchange: e => { this.der.min_spread = +e.target.value; } })));
      der.appendChild(row('max fit RMS m', num(this.der.max_rms, { onchange: e => { this.der.max_rms = +e.target.value; } })));
      der.appendChild(btn('DERIVE ORIENTATIONS', () => this.derive(), { class: 'b wide' }));
      if (!list.length) der.appendChild(btn('\u2605 DERIVE FROM ALL ' + traces.length + ' TRACE LAYERS', () => this.deriveAll(), { class: 'b wide on' }));
      else der.appendChild(btn('DERIVE FROM ALL TRACE LAYERS', () => this.deriveAll(), { class: 'b wide' }));
      if (this.derStats) der.appendChild(kv(Object.entries(this.derStats).map(([k, v]) => [k.replace(/_/g, ' '), String(v)])));
    }
    el.appendChild(der);

    if (this.layer) {
      /* ---------------- clean up ---------------- */
      const cl = section('CLEAN UP');
      const topo = this.app.topoGrid ? this.app.topoGrid() : null;
      cl.appendChild(btn('SET ELEVATION FROM TOPOGRAPHY', () => this.setElevation(), { class: 'b wide' }));
      cl.appendChild(note('Measurements digitised off a flat map have no elevation, so they sit below the model and are silently ignored when a surface is built. Drape them first.'));
      this.dc = this.dc || Object.assign({}, S.DECLUSTER_DEFAULTS);
      const cols = Object.keys(this.layer.attributes);
      cl.appendChild(row('radius m', num(this.dc.radius, { onchange: e => { this.dc.radius = +e.target.value; } })));
      cl.appendChild(row('angular tol °', num(this.dc.angular_tolerance, { onchange: e => { this.dc.angular_tolerance = +e.target.value; } })));
      cl.appendChild(row('priority col', sel([['', '—'], ...cols.filter(c => this.layer.isNumeric(c))], this.dc.priority_column || '', { onchange: e => { this.dc.priority_column = e.target.value || null; } })));
      cl.appendChild(row('per category', sel([['', '—'], ...cols.filter(c => !this.layer.isNumeric(c))], this.dc.category_column || '', { onchange: e => { this.dc.category_column = e.target.value || null; } })));
      cl.appendChild(btn('DECLUSTER', () => this.decluster(), { class: 'b wide' }));
      el.appendChild(cl);

      /* ---------------- quick statistics ---------------- */
      const st = section('THIS LAYER');
      try {
        const R = S.readStructural(this.layer);
        const b = R.n >= 2 ? S.binghamStats(R.poles, R.n) : null;
        st.appendChild(kv([
          ['Measurements', `${R.n}${R.n < this.layer.n ? ` of ${this.layer.n} (rest have no dip)` : ''}`],
          ['Mean plane', b ? `${b.mean_plane.dip.toFixed(0)}° → ${b.mean_plane.dip_azimuth.toFixed(0)}°` : null],
          ['Fabric', b ? b.fabric : null],
          ['Fold hinge', b && b.fabric.startsWith('girdle') ? `${b.fold_hinge.plunge.toFixed(0)}° → ${b.fold_hinge.trend.toFixed(0)}°` : null],
        ]));
      } catch (e) { st.appendChild(note(e.message, 'note warn')); }
      st.appendChild(h('div', { class: 'frow' }, btn('OPEN STEREONET', () => this.T.open('stereonet', this.layer)), btn('FORM INTERPOLANT', () => this.T.open('form', this.layer))));
      el.appendChild(st);
    }
    return el;
  }
  repanel() { this.T.showPanel(this.panel()); }

  newLayer() {
    const name = prompt('New structural layer name', 'Field structure');
    if (name == null) return;
    const ps = S.newStructural(name);
    this.app.project.add(ps); this.layer = ps; this.repanel();
  }
  viewAzimuth() {
    const R = this.T.R, cam = R.camera.getWorldDirection(new (R.camera.position.constructor)());
    return (Math.atan2(cam.x, -cam.z) * 180 / Math.PI % 360 + 360) % 360;
  }
  startDigitise(mode) {
    if (!this.layer) { toast('make or pick a structural layer first', 'warn'); return; }
    this.mode = this.mode === mode ? null : mode; this.pending = null;
    $('gl').style.cursor = this.mode ? 'crosshair' : '';
    this.T.R.clearOverlay(); this.repanel();
  }
  onClick(e) {
    if (!this.mode) return false;
    const w = groundPick(this.T.R, e);
    if (!w) { toast('click on the ground or a surface', 'warn'); return true; }
    if (this.mode === 'one') { this.commit(w, this.form.dipaz); return true; }
    if (!this.pending) { this.pending = w; this.T.R.overlayMarker(w, 0x2dd4bf, 12); toast('now click a point in the DOWN-DIP direction', 'info', 2500); return true; }
    const az = bearing(this.pending, w);
    this.commit(this.pending, az); this.pending = null; this.T.R.clearOverlay();
    return true;
  }
  onKey(e) { if (this.mode && e.key === 'Escape') { this.stop(); this.repanel(); return true; } return false; }
  commit(w, az) {
    const f = this.form;
    S.addMeasurement(this.layer, w[0], w[1], w[2], f.dip, az, {
      polarity: f.polarity, type: f.type || null, confidence: f.confidence,
      source: f.source || null, page: f.page === '' ? null : f.page,
    });
    this.form.dipaz = az;
    this.app.refresh(this.layer);
    toast(`${f.dip.toFixed(0)}° → ${az.toFixed(0)}° added (${this.layer.n} total)`, 'ok', 1800);
    this.repanel();
  }

  /** Every mapped contact and fault trace in the project at once — the
      one-click path for a district that has a geological map and nothing
      else.  Results land in a single layer, tagged with the source line. */
  async deriveAll() {
    const traces = traceLayers(this.app.project);
    if (!traces.length) return toast('no mapped traces in this project', 'warn');
    const merged = S.newStructural('Derived structure (mapped geology)');
    merged.color = [140, 190, 120];
    const roll = { parts: 0, windows: 0, kept: 0, no_relief: 0, no_spread: 0, bad_fit: 0, short: 0 };
    this.app.status('deriving orientations from the mapped geology…');
    for (let i = 0; i < traces.length; i++) {
      const src = traces[i];
      this.app.status(`deriving from ${src.name} (${i + 1}/${traces.length})…`);
      try {
        const out = await this.app.engine.call('deriveStructure', Object.assign({ lineset: src }, this.der || S.DERIVE_DEFAULTS));
        for (const k of Object.keys(roll)) roll[k] += (out.metadata.derived || {})[k] || 0;
        const R = S.readStructural(out);
        for (let q = 0; q < R.n; q++) {
          const j = R.index[q], a = {};
          for (const c of Object.keys(out.attributes)) if (!['dip', 'dip_azimuth', 'polarity'].includes(c)) a[c] = out.attributes[c][j];
          a.polarity = R.polarity[q];
          S.addMeasurement(merged, R.x[q], R.y[q], R.z[q], R.dip[q], R.dipaz[q], a);
        }
      } catch (e) { console.warn(src.name, e); }
    }
    this.app.status('');
    merged.provenance = { method: 'least-squares plane through draped map traces (three-point problem)', layers: traces.map(t => t.name).join(', ') };
    merged.metadata.derived = roll;
    merged.metadata.howto = 'Every orientation here was read from where a mapped line crosses the DEM, so it inherits the accuracy of both. relief_m and fit_rms_m are on each point; confidence is "inferred". Digitise over the top of it where you have a real reading.';
    if (!merged.n) { merged.warn(`nothing derivable: ${roll.windows} windows tried, ${roll.no_relief} without relief, ${roll.no_spread} without spread, ${roll.bad_fit} poorly fitted.`); toast('no orientation could be derived from these traces — the ground is probably too flat', 'warn', 8000); this.repanel(); return; }
    this.app.project.add(merged); this.layer = merged; this.derStats = roll;
    toast(`${merged.n} orientations derived from ${traces.length} trace layer(s)`, 'ok', 6000);
    this.app.select(merged.id);
  }

  async derive() {
    const src = this.app.project.get(this.derSel);
    if (!src) return toast('pick a trace layer', 'warn');
    this.app.status(`deriving orientations from ${src.name}…`);
    try {
      const out = await this.app.engine.call('deriveStructure', Object.assign({ lineset: src }, this.der, { name: src.name + ' — derived structure' }));
      this.derStats = out.metadata.derived;
      if (!out.n) { toast('no orientation could be derived — see the warnings', 'warn', 7000); this.repanel(); return; }
      this.app.project.add(out); this.layer = out;
      toast(`${out.n} orientations derived from ${src.name}`, 'ok');
      this.app.select(out.id);
    } catch (err) { console.error(err); toast('derive failed: ' + err.message, 'err', 8000); }
    this.app.status('');
  }
  setElevation() {
    const topo = this.app.topoGrid();
    if (!topo) return toast('no topography in this project', 'warn');
    const r = S.setElevationFromGrid(this.layer, topo, { offset: 0 });
    this.app.refresh(this.layer);
    toast(`${r.moved} point(s) draped onto ${topo.name}${r.outside ? `, ${r.outside} outside it` : ''}`, r.outside ? 'warn' : 'ok');
  }
  async decluster() {
    this.app.status('declustering…');
    try {
      const r = await this.app.engine.call('declusterStructural', Object.assign({ points: this.layer }, this.dc, { name: this.layer.name + ' — declustered' }));
      if (!r.points.n) { toast('everything was dropped — loosen the angular tolerance', 'warn', 7000); }
      else { this.app.project.add(r.points); this.layer = r.points; toast(`kept ${r.kept}, removed ${r.removed}${r.noisy ? `, ${r.noisy} cluster(s) too noisy to average` : ''}`, r.noisy ? 'warn' : 'ok', 6000); this.app.select(r.points.id); }
    } catch (err) { console.error(err); toast('decluster failed: ' + err.message, 'err', 8000); }
    this.app.status('');
  }
}

/* ========================================================= STEREONET === */

export class StereonetTool {
  constructor(T) {
    this.T = T; this.sel = new Set(); this.canvas = null;
    this.opt = { net: 'equatorial', projection: 'equal_area', desample: 0.5, poles: true, planes: false, contours: true, method: 'kamb', sigma: 3, levels: 6, showMean: true, showGirdle: true, showCone: false, colorBy: '' };
    this.picking = null; this.poly = []; this.picked = new Set(); this.dg = null; this.stats = null; this.rect = null;
  }
  get app() { return this.T.app; }
  onProject() { this.sel.clear(); this.dg = null; this.stats = null; }
  stop() { this.picking = null; this.poly = []; this.removeRect(); $('gl').style.cursor = ''; }

  panel(layer) {
    const P = this.app.project, list = structLayers(P);
    if (layer && list.includes(layer)) this.sel = new Set([layer.id]);
    if (!this.sel.size && list.length) this.sel = new Set([list[0].id]);
    const el = h('div', { class: 'tool' }, h('h2', {}, 'STEREONET'));
    if (!list.length) { el.appendChild(note('No structural data yet. Use TOOLS ▸ Structural data to digitise or derive some.')); return el; }

    const ds = section('DATASETS');
    for (const o of list) ds.appendChild(h('label', { class: 'chk' },
      h('input', { type: 'checkbox', checked: this.sel.has(o.id), onchange: e => { e.target.checked ? this.sel.add(o.id) : this.sel.delete(o.id); this.redraw(); } }),
      `${o.name} (${o.n})`));
    el.appendChild(ds);

    this.canvas = h('canvas', { width: 632, height: 632, class: 'stereo', style: { width: '316px', height: '316px' } });
    this.canvas.addEventListener('click', ev => this.onNetClick(ev));
    el.appendChild(h('div', { class: 'imgbox', style: { background: '#0f1318' } }, this.canvas));
    this.readout = h('div', { class: 'mono' }, '');
    el.appendChild(this.readout);

    const po = section('PLOT');
    po.appendChild(row('net', sel(Object.entries(S.NET_TYPES), this.opt.net, { onchange: e => { this.opt.net = e.target.value; this.redraw(); } })));
    po.appendChild(row('projection', sel(Object.entries(S.PROJECTIONS), this.opt.projection, { onchange: e => { this.opt.projection = e.target.value; this.dg = null; this.redraw(); } })));
    po.appendChild(row('desample', range(this.opt.desample, 0, 1, 0.05, e => { this.opt.desample = +e.target.value; this.redraw(); }), h('span', { class: 'mono' }, String(this.opt.desample))));
    po.appendChild(note('Desampling thins the picture only — every measurement is still used for the statistics and the contours.'));
    po.appendChild(h('div', {},
      h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt.poles, onchange: e => { this.opt.poles = e.target.checked; this.redraw(); } }), 'poles'),
      h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt.planes, onchange: e => { this.opt.planes = e.target.checked; this.redraw(); } }), 'planes'),
      h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt.contours, onchange: e => { this.opt.contours = e.target.checked; this.dg = null; this.redraw(); } }), 'contours')));
    if (this.opt.contours) {
      po.appendChild(row('method', sel(Object.entries(S.CONTOUR_METHODS), this.opt.method, { onchange: e => { this.opt.method = e.target.value; this.dg = null; this.redraw(); } })));
      po.appendChild(row('levels', num(this.opt.levels, { min: 2, max: 12, onchange: e => { this.opt.levels = Math.max(2, Math.min(12, +e.target.value)); this.redraw(); } })));
      po.appendChild(note(this.opt.method === 'schmidt' ? 'Schmidt (1 % area) overfits below about 400 points — the guide recommends Kamb for most datasets.' : 'Kamb sizes its counting circle so the expected count is 3σ; contours are in units of σ.'));
    }
    const catCols = this.categoryColumns();
    po.appendChild(row('colour by', sel([['', 'per dataset'], ['dip', 'dip'], ['dip_azimuth', 'dip azimuth'], ['polarity', 'polarity'], ...catCols.map(c => [c, c])], this.opt.colorBy, { onchange: e => { this.opt.colorBy = e.target.value; this.redraw(); } })));
    el.appendChild(po);

    const stx = section('STATISTICS');
    stx.appendChild(h('div', {},
      h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt.showMean, onchange: e => { this.opt.showMean = e.target.checked; this.redraw(); } }), 'Bingham mean plane'),
      h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt.showGirdle, onchange: e => { this.opt.showGirdle = e.target.checked; this.redraw(); } }), 'best-fit girdle'),
      h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt.showCone, onchange: e => { this.opt.showCone = e.target.checked; this.redraw(); } }), 'Fisher α95')));
    this.statsHost = h('div', {});
    stx.appendChild(this.statsHost);
    el.appendChild(stx);

    const se = section('SELECT');
    se.appendChild(h('div', { class: 'modes' },
      btn('LASSO ON THE NET', () => this.startPick('poly'), { class: 'b' + (this.picking === 'poly' ? ' on' : '') }),
      btn('CLICK POINTS', () => this.startPick('point'), { class: 'b' + (this.picking === 'point' ? ' on' : '') }),
      btn('BOX IN THE SCENE', () => this.startPick('scene'), { class: 'b' + (this.picking === 'scene' ? ' on' : '') }),
      btn('CLEAR', () => { this.picked.clear(); this.redraw(); })));
    se.appendChild(note(`${this.picked.size} selected. A selection becomes a category column on the source layer, so it colours the 3-D scene straight away — and a category made in the scene filters the net the same way.`));
    if (this.picked.size) se.appendChild(h('div', { class: 'frow' }, btn('ASSIGN TO CATEGORY…', () => this.assign(), { class: 'b wide' })));
    el.appendChild(se);

    el.appendChild(h('div', { class: 'frow' }, btn('EXPORT SVG', () => this.exportSvg()), btn('EXPORT PNG', () => this.exportPng())));
    setTimeout(() => this.redraw(), 0);
    return el;
  }
  repanel() { this.T.showPanel(this.panel()); }
  categoryColumns() {
    const out = new Set();
    for (const id of this.sel) { const o = this.app.project.get(id); if (!o) continue; for (const c of Object.keys(o.attributes)) if (!o.isNumeric(c) && !['dip', 'dip_azimuth', 'polarity'].includes(c)) out.add(c); }
    return [...out];
  }
  gather() {
    const rows = [];
    for (const id of this.sel) {
      const o = this.app.project.get(id); if (!o) continue;
      const R = S.readStructural(o);
      for (let i = 0; i < R.n; i++) rows.push({ obj: o, row: R.index[i], dip: R.dip[i], az: R.dipaz[i], pol: R.polarity[i], pole: [R.poles[3 * i], R.poles[3 * i + 1], R.poles[3 * i + 2]] });
    }
    return rows;
  }

  redraw() {
    if (!this.canvas) return;
    const rows = this.gather();
    this.rows = rows;
    const poles = new Float64Array(rows.length * 3);
    rows.forEach((r, i) => { poles[3 * i] = r.pole[0]; poles[3 * i + 1] = r.pole[1]; poles[3 * i + 2] = r.pole[2]; });
    this.stats = rows.length >= 2 ? { bingham: S.binghamStats(poles, rows.length), fisher: S.fisherStats(poles, rows.length) } : null;
    if (this.opt.contours && rows.length >= 5 && !this.dg) this.dg = S.densityGrid(poles, rows.length, { method: this.opt.method, projection: this.opt.projection, size: rows.length > 3000 ? 72 : 96 });
    if (!this.opt.contours) this.dg = null;
    drawStereonet(this.canvas, { rows, opt: this.opt, dg: this.dg, stats: this.stats, picked: this.picked, poly: this.poly, colors: this.colorFor.bind(this) });
    if (this.statsHost) { clear(this.statsHost); this.statsHost.appendChild(this.statsTable()); }
    if (this.readout) this.readout.textContent = `${rows.length} measurement(s)` + (this.dg ? ` · peak ${this.dg.max.toFixed(1)} ${this.dg.unit_label}` : '');
  }
  colorFor(r, i) {
    const by = this.opt.colorBy;
    if (!by) { const k = [...this.sel].indexOf(r.obj.id); return CAT_COLORS[(k < 0 ? 0 : k) % CAT_COLORS.length]; }
    if (by === 'polarity') return r.pol >= 0 ? [104, 176, 255] : [217, 112, 45];
    if (by === 'dip') { const c = GM.colormap('turbo', r.dip / 90); return c; }
    if (by === 'dip_azimuth') { const c = GM.colormap('turbo', r.az / 360); return c; }
    const v = r.obj.attributes[by] ? r.obj.attributes[by][r.row] : null;
    if (v == null || v === '') return [130, 140, 150];
    if (!this._catmap) this._catmap = new Map();
    if (!this._catmap.has(String(v))) this._catmap.set(String(v), this._catmap.size);
    return CAT_COLORS[this._catmap.get(String(v)) % CAT_COLORS.length];
  }
  statsTable() {
    if (!this.stats || !this.stats.bingham) return note('at least 2 measurements are needed for statistics');
    const b = this.stats.bingham, f = this.stats.fisher;
    const box = h('div', {});
    box.appendChild(kv([
      ['n', String(b.n)],
      ['Bingham mean plane', `${b.mean_plane.dip.toFixed(1)}° → ${b.mean_plane.dip_azimuth.toFixed(1)}°`],
      ['e1 (pole to mean plane)', `${b.mean_pole.plunge.toFixed(1)}° → ${b.mean_pole.trend.toFixed(1)}°`],
      ['Best-fit girdle', `${b.best_fit_plane.dip.toFixed(1)}° → ${b.best_fit_plane.dip_azimuth.toFixed(1)}°`],
      ['e3 (fold hinge)', `${b.fold_hinge.plunge.toFixed(1)}° → ${b.fold_hinge.trend.toFixed(1)}°`],
      ['Eigenvalues', b.eigenvalues.map(v => v.toFixed(4)).join('  ')],
      ['Fabric (Woodcock K)', `${b.fabric} · K = ${b.shape.toFixed(2)}`],
      ['Fisher mean plane', f ? `${f.mean_plane.dip.toFixed(1)}° → ${f.mean_plane.dip_azimuth.toFixed(1)}°` : null],
      ['Fisher κ / R̄ / α95', f ? `${isFinite(f.kappa) ? f.kappa.toFixed(1) : '∞'} / ${f.Rbar.toFixed(3)} / ${f.alpha95 == null ? '—' : f.alpha95.toFixed(1) + '°'}` : null],
    ]));
    if (f && f.steep) box.appendChild(note('The Fisher mean loses robustness as planes steepen — with a mean dip of ' + f.mean_plane.dip.toFixed(0) + '° it is being pulled by the steepest measurements. Prefer the Bingham mean here.', 'note warn'));
    if (b.fabric.startsWith('girdle')) box.appendChild(note('A girdle means the poles lie on a great circle: the data is folded, e3 is the fold hinge, and the best-fit plane is the profile plane — the ideal section through the fold.'));
    return box;
  }

  /* ---- selection ---- */
  startPick(mode) {
    this.picking = this.picking === mode ? null : mode; this.poly = [];
    this.removeRect();
    if (this.picking === 'scene') { $('gl').style.cursor = 'crosshair'; this.installRect(); }
    else $('gl').style.cursor = '';
    this.repanel();
  }
  onNetClick(ev) {
    if (!this.picking || this.picking === 'scene') return;
    const r = this.canvas.getBoundingClientRect();
    const x = ((ev.clientX - r.left) / r.width) * 2 - 1, y = 1 - ((ev.clientY - r.top) / r.height) * 2;
    const R = 0.86;                                             // net radius inside the canvas
    const px = x / R, py = y / R;
    if (this.picking === 'point') {
      let best = -1, bd = 0.05;
      this.rows.forEach((row, i) => { const p = S.projectVec(row.pole, this.opt.projection); if (!p) return; const d = Math.hypot(p.x - px, p.y - py); if (d < bd) { bd = d; best = i; } });
      if (best >= 0) { this.picked.has(best) ? this.picked.delete(best) : this.picked.add(best); this.redraw(); this.repanel(); }
      return;
    }
    // lasso
    if (this.poly.length > 2 && Math.hypot(this.poly[0][0] - px, this.poly[0][1] - py) < 0.06) {
      this.rows.forEach((row, i) => { const p = S.projectVec(row.pole, this.opt.projection); if (p && pointInPoly(p.x, p.y, this.poly)) this.picked.add(i); });
      this.poly = []; this.redraw(); this.repanel(); return;
    }
    this.poly.push([px, py]); this.redraw();
  }
  installRect() {
    const vp = $('viewport'); if (!vp || this.rect) return;
    const box = h('div', { style: { position: 'absolute', border: '1px dashed #2dd4bf', background: 'rgba(45,212,191,.12)', display: 'none', pointerEvents: 'none', zIndex: 30 } });
    vp.appendChild(box);
    let start = null;
    const down = e => { if (e.button !== 0) return; start = [e.clientX, e.clientY]; box.style.display = 'block'; e.stopPropagation(); e.preventDefault(); };
    const move = e => { if (!start) return; const r = vp.getBoundingClientRect(); const x0 = Math.min(start[0], e.clientX) - r.left, y0 = Math.min(start[1], e.clientY) - r.top; box.style.left = x0 + 'px'; box.style.top = y0 + 'px'; box.style.width = Math.abs(e.clientX - start[0]) + 'px'; box.style.height = Math.abs(e.clientY - start[1]) + 'px'; };
    const up = e => {
      if (!start) return;
      const a = [Math.min(start[0], e.clientX), Math.min(start[1], e.clientY)], b = [Math.max(start[0], e.clientX), Math.max(start[1], e.clientY)];
      start = null; box.style.display = 'none';
      if (b[0] - a[0] < 4 && b[1] - a[1] < 4) return;
      const R = this.T.R; let added = 0;
      this.rows.forEach((row, i) => {
        const o = row.obj, j = row.row;
        const sc = toScreen(R, o.xyz[3 * j], o.xyz[3 * j + 1], o.xyz[3 * j + 2]);
        if (sc[2] > 1) return;
        if (sc[0] >= a[0] && sc[0] <= b[0] && sc[1] >= a[1] && sc[1] <= b[1]) { this.picked.add(i); added++; }
      });
      toast(`${added} measurement(s) selected in the scene`, added ? 'ok' : 'warn', 2500);
      this.redraw(); this.repanel();
    };
    const canvas = $('gl');
    canvas.addEventListener('pointerdown', down, true);
    window.addEventListener('pointermove', move, true);
    window.addEventListener('pointerup', up, true);
    this.rect = { box, down, move, up, canvas };
  }
  removeRect() {
    if (!this.rect) return;
    this.rect.canvas.removeEventListener('pointerdown', this.rect.down, true);
    window.removeEventListener('pointermove', this.rect.move, true);
    window.removeEventListener('pointerup', this.rect.up, true);
    this.rect.box.remove(); this.rect = null;
  }
  assign() {
    const cols = this.categoryColumns();
    const nameIn = txt('Set 1');
    const colSel = sel([['__new__', '— new column —'], ...cols], cols[0] || '__new__');
    const newCol = txt('structural_set');
    const body = h('div', {},
      h('p', {}, `${this.picked.size} measurement(s) will be tagged. The column appears immediately as a "colour by" option on the layer and as a filter on this net.`),
      row('column', colSel), row('new column name', newCol), row('category', nameIn));
    const m = modal('ASSIGN SELECTION TO A CATEGORY', h('div', {}, body,
      h('div', { class: 'frow' }, btn('ASSIGN', () => {
        const col = colSel.value === '__new__' ? (newCol.value || 'structural_set') : colSel.value;
        const val = nameIn.value || 'Set 1';
        const touched = new Set();
        for (const i of this.picked) {
          const r = this.rows[i]; const o = r.obj;
          if (!o.attributes[col]) o.attributes[col] = new Array(o.n).fill(null);
          while (o.attributes[col].length < o.n) o.attributes[col].push(null);
          o.attributes[col][r.row] = val; touched.add(o);
        }
        for (const o of touched) { const d = this.app.display.get(o.id) || {}; d.attribute = col; this.app.display.set(o.id, d); this.app.refresh(o); }
        this.picked.clear(); m.close(); this.redraw(); this.repanel();
        toast(`tagged as ${col} = ${val} on ${touched.size} layer(s)`, 'ok');
      }, { class: 'b wide' }))));
  }
  exportPng() {
    const url = this.canvas.toDataURL('image/png');
    const a = document.createElement('a'); a.href = url; a.download = 'stereonet.png'; a.click();
  }
  exportSvg() {
    const svg = stereonetSvg({ rows: this.rows, opt: this.opt, dg: this.dg, stats: this.stats, colors: this.colorFor.bind(this) });
    GM.downloadBlob(new Blob([svg], { type: 'image/svg+xml' }), 'stereonet.svg');
  }
}

function pointInPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-12) + xi) inside = !inside;
  }
  return inside;
}

/* ---------------------------------------------------- net rendering --- */

const NET_BG = '#0f1318', NET_LINE = '#2a3340', NET_TEXT = '#8a97a6';

function drawStereonet(canvas, st) {
  const ctx = canvas.getContext('2d'); const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.43;
  ctx.clearRect(0, 0, W, H); ctx.fillStyle = NET_BG; ctx.fillRect(0, 0, W, H);
  const X = px => cx + px * R, Y = py => cy - py * R;

  // density fill
  if (st.dg && st.dg.max > 0) {
    const dgm = st.dg, size = dgm.size, img = ctx.createImageData(Math.ceil(2 * R), Math.ceil(2 * R));
    const n = img.width;
    const levels = []; for (let i = 1; i <= (st.opt.levels || 6); i++) levels.push(dgm.max * i / (st.opt.levels || 6));
    for (let yy = 0; yy < img.height; yy++) for (let xx = 0; xx < n; xx++) {
      const px = (xx / (n - 1)) * 2 - 1, py = 1 - (yy / (img.height - 1)) * 2;
      const rr = Math.hypot(px, py); const o = (yy * n + xx) * 4;
      if (rr > 1) { img.data[o + 3] = 0; continue; }
      const gi = (px + 1) / 2 * (size - 1), gj = (py + 1) / 2 * (size - 1);
      const i0 = Math.min(size - 2, Math.max(0, Math.floor(gi))), j0 = Math.min(size - 2, Math.max(0, Math.floor(gj)));
      const fx = gi - i0, fy = gj - j0;
      const g = dgm.grid, a = g[j0 * size + i0], b = g[j0 * size + i0 + 1], c = g[(j0 + 1) * size + i0], d = g[(j0 + 1) * size + i0 + 1];
      let v = (a === a && b === b && c === c && d === d) ? a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy : (a === a ? a : 0);
      let band = 0; for (let k = 0; k < levels.length; k++) if (v >= levels[k]) band = k + 1;
      if (!band) { img.data[o + 3] = 0; continue; }
      const t = band / levels.length;
      const col = GM.colormap('magma', 0.15 + 0.8 * t);
      img.data[o] = col[0]; img.data[o + 1] = col[1]; img.data[o + 2] = col[2]; img.data[o + 3] = 205;
    }
    ctx.putImageData(img, Math.round(cx - R), Math.round(cy - R));
    ctx.save(); ctx.globalCompositeOperation = 'destination-in'; ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill(); ctx.restore();
    ctx.strokeStyle = 'rgba(255,255,255,.28)'; ctx.lineWidth = 1;
    for (const lv of levels) for (const seg of S.contourLines(st.dg, [lv])[0].segments) { ctx.beginPath(); ctx.moveTo(X(seg[0][0]), Y(seg[0][1])); ctx.lineTo(X(seg[1][0]), Y(seg[1][1])); ctx.stroke(); }
  }

  // grid
  ctx.strokeStyle = NET_LINE; ctx.lineWidth = 1;
  if (st.opt.net === 'polar') {
    for (let p = 10; p < 90; p += 10) { const rr = S.projectVec(S.lineVector(0, p), st.opt.projection).r; ctx.beginPath(); ctx.arc(cx, cy, rr * R, 0, Math.PI * 2); ctx.stroke(); }
    for (let a = 0; a < 360; a += 10) { const t = a * Math.PI / 180; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.sin(t) * R, cy - Math.cos(t) * R); ctx.stroke(); }
  } else {
    for (let d = 10; d < 90; d += 10) { for (const az of [90, 270]) { const parts = S.greatCircle(d, az, { projection: st.opt.projection }); for (const part of parts) { ctx.beginPath(); part.forEach(([x, y], i) => i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.stroke(); } } }
    for (let p = 10; p < 90; p += 10) { for (const sgn of [1, -1]) { const parts = S.smallCircle([sgn, 0, 0], 90 - p, { projection: st.opt.projection }); for (const part of parts) { ctx.beginPath(); part.forEach(([x, y], i) => i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.stroke(); } } }
  }
  ctx.strokeStyle = '#4a5666'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx, cy - 6); ctx.lineTo(cx, cy + 6); ctx.moveTo(cx - 6, cy); ctx.lineTo(cx + 6, cy); ctx.stroke();
  ctx.fillStyle = NET_TEXT; ctx.font = `${Math.round(R * 0.085)}px ui-monospace, monospace`; ctx.textAlign = 'center';
  ctx.fillText('N', cx, cy - R - 8); ctx.fillText('S', cx, cy + R + Math.round(R * 0.11));
  ctx.textAlign = 'left'; ctx.fillText('E', cx + R + 6, cy + 4);
  ctx.textAlign = 'right'; ctx.fillText('W', cx - R - 6, cy + 4);

  const rows = st.rows || [];
  const mask = st.opt.desample > 0 ? S.desampleMask(flatPoles(rows), rows.length, st.opt.desample, st.opt.projection) : null;

  // great circles
  if (st.opt.planes) {
    ctx.lineWidth = 1.2;
    rows.forEach((r, i) => {
      if (mask && !mask[i]) return;
      const c = st.colors(r, i); ctx.strokeStyle = `rgba(${c[0]},${c[1]},${c[2]},.55)`;
      for (const part of S.greatCircle(r.dip, r.az, { projection: st.opt.projection })) { ctx.beginPath(); part.forEach(([x, y], k) => k ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.stroke(); }
    });
  }
  // poles
  if (st.opt.poles) {
    rows.forEach((r, i) => {
      if (mask && !mask[i] && !st.picked.has(i)) return;
      const p = S.projectVec(r.pole, st.opt.projection); if (!p) return;
      const c = st.colors(r, i); const on = st.picked.has(i);
      ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), on ? R * 0.018 : R * 0.011, 0, Math.PI * 2);
      ctx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`; ctx.fill();
      if (on) { ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke(); }
    });
  }
  // statistics overlays
  const b = st.stats && st.stats.bingham, f = st.stats && st.stats.fisher;
  if (b && st.opt.showMean) {
    ctx.strokeStyle = '#2dd4bf'; ctx.lineWidth = 2;
    for (const part of S.greatCircle(b.mean_plane.dip, b.mean_plane.dip_azimuth, { projection: st.opt.projection })) { ctx.beginPath(); part.forEach(([x, y], k) => k ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.stroke(); }
    const p1 = S.projectVec(b.eigenvectors[0], st.opt.projection);
    if (p1) { ctx.fillStyle = '#2dd4bf'; ctx.beginPath(); ctx.arc(X(p1.x), Y(p1.y), R * 0.022, 0, Math.PI * 2); ctx.fill(); }
  }
  if (b && st.opt.showGirdle) {
    ctx.strokeStyle = '#f4a261'; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
    for (const part of S.greatCircle(b.best_fit_plane.dip, b.best_fit_plane.dip_azimuth, { projection: st.opt.projection })) { ctx.beginPath(); part.forEach(([x, y], k) => k ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.stroke(); }
    ctx.setLineDash([]);
    const p3 = S.projectVec(b.eigenvectors[2], st.opt.projection);
    if (p3) { ctx.fillStyle = '#f4a261'; ctx.beginPath(); ctx.moveTo(X(p3.x), Y(p3.y) - R * 0.03); ctx.lineTo(X(p3.x) + R * 0.026, Y(p3.y) + R * 0.02); ctx.lineTo(X(p3.x) - R * 0.026, Y(p3.y) + R * 0.02); ctx.closePath(); ctx.fill(); }
  }
  if (f && f.alpha95 != null && st.opt.showCone) {
    ctx.strokeStyle = '#e879f9'; ctx.lineWidth = 1.5;
    for (const part of S.smallCircle(f.mean_vector, f.alpha95, { projection: st.opt.projection })) { ctx.beginPath(); part.forEach(([x, y], k) => k ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.stroke(); }
  }
  // lasso in progress
  if (st.poly && st.poly.length) {
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
    ctx.beginPath(); st.poly.forEach(([x, y], i) => i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#fff'; for (const [x, y] of st.poly) { ctx.beginPath(); ctx.arc(X(x), Y(y), 3, 0, Math.PI * 2); ctx.fill(); }
  }
  // caption
  ctx.fillStyle = NET_TEXT; ctx.font = `${Math.round(R * 0.062)}px ui-monospace, monospace`; ctx.textAlign = 'left';
  ctx.fillText(`${S.PROJECTIONS[st.opt.projection]} · lower hemisphere · n=${rows.length}`, 8, H - 8);
}
function flatPoles(rows) { const a = new Float64Array(rows.length * 3); rows.forEach((r, i) => { a[3 * i] = r.pole[0]; a[3 * i + 1] = r.pole[1]; a[3 * i + 2] = r.pole[2]; }); return a; }

function stereonetSvg(st) {
  const R = 300, W = 700, H = 720, cx = W / 2, cy = 340;
  const X = px => (cx + px * R).toFixed(2), Y = py => (cy - py * R).toFixed(2);
  const out = [`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><rect width="${W}" height="${H}" fill="#ffffff"/>`];
  const path = parts => parts.map(p => 'M' + p.map(([x, y]) => `${X(x)},${Y(y)}`).join('L')).join(' ');
  for (let d = 10; d < 90; d += 10) for (const az of [90, 270]) out.push(`<path d="${path(S.greatCircle(d, az, { projection: st.opt.projection }))}" fill="none" stroke="#dde3e8" stroke-width="0.8"/>`);
  for (let p = 10; p < 90; p += 10) for (const sgn of [1, -1]) out.push(`<path d="${path(S.smallCircle([sgn, 0, 0], 90 - p, { projection: st.opt.projection }))}" fill="none" stroke="#dde3e8" stroke-width="0.8"/>`);
  out.push(`<circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="#7b8794" stroke-width="1.6"/>`);
  if (st.opt.planes) st.rows.forEach((r, i) => { const c = st.colors(r, i); out.push(`<path d="${path(S.greatCircle(r.dip, r.az, { projection: st.opt.projection }))}" fill="none" stroke="rgb(${c})" stroke-opacity="0.55" stroke-width="1"/>`); });
  if (st.opt.poles) st.rows.forEach((r, i) => { const p = S.projectVec(r.pole, st.opt.projection); if (!p) return; const c = st.colors(r, i); out.push(`<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="3.2" fill="rgb(${c})"/>`); });
  const b = st.stats && st.stats.bingham;
  if (b && st.opt.showMean) out.push(`<path d="${path(S.greatCircle(b.mean_plane.dip, b.mean_plane.dip_azimuth, { projection: st.opt.projection }))}" fill="none" stroke="#0f766e" stroke-width="2.4"/>`);
  if (b && st.opt.showGirdle) out.push(`<path d="${path(S.greatCircle(b.best_fit_plane.dip, b.best_fit_plane.dip_azimuth, { projection: st.opt.projection }))}" fill="none" stroke="#b45309" stroke-width="2.4" stroke-dasharray="8 5"/>`);
  out.push(`<text x="${cx}" y="${cy - R - 12}" text-anchor="middle" font-family="ui-monospace,monospace" font-size="16" fill="#334155">N</text>`);
  out.push(`<text x="16" y="${H - 40}" font-family="ui-monospace,monospace" font-size="13" fill="#475569">${S.PROJECTIONS[st.opt.projection]} · lower hemisphere · n=${st.rows.length}</text>`);
  if (b) out.push(`<text x="16" y="${H - 20}" font-family="ui-monospace,monospace" font-size="13" fill="#475569">Bingham mean ${b.mean_plane.dip.toFixed(0)}° → ${b.mean_plane.dip_azimuth.toFixed(0)}° · hinge ${b.fold_hinge.plunge.toFixed(0)}° → ${b.fold_hinge.trend.toFixed(0)}° · ${b.fabric}</text>`);
  out.push('</svg>');
  return out.join('\n');
}

/* ====================================== FORM INTERPOLANT / TRENDS ===== */

export class FormTool {
  constructor(T) {
    this.T = T; this.sel = new Set();
    this.opt = { smoothing: 1e-6, max_points: 250, thresholds: 5, resolution: 0, boundary: 'data', pad: 15, drape: true, filterCol: '', filterVal: '' };
    this.trend = { strength: 5, range: 100, sel: new Set() };
    this.global = { dip: 0, dipaz: 90, pitch: 0, ratios: [3, 3, 1] };
    this.last = null;
  }
  get app() { return this.T.app; }
  onProject() { this.sel.clear(); this.trend.sel.clear(); this.last = null; }
  stop() { }

  panel(layer) {
    const P = this.app.project, list = structLayers(P);
    if (layer && list.includes(layer)) this.sel = new Set([layer.id]);
    if (!this.sel.size && list.length) this.sel = new Set([list[0].id]);
    const el = h('div', { class: 'tool' }, h('h2', {}, 'FORM INTERPOLANT & TRENDS'));
    if (!list.length) { el.appendChild(note('No structural data yet — TOOLS ▸ Structural data first.')); return el; }

    const inp = section('FORM INTERPOLANT');
    inp.appendChild(note('An RBF whose gradient is controlled only by the planar structural data: its iso-surfaces are everywhere tangent to the measured planes, so they show the deformation fabric. It is blind to faults and truncating intrusions, and it is very sensitive to clustered data — decluster first.'));
    for (const o of list) inp.appendChild(h('label', { class: 'chk' },
      h('input', { type: 'checkbox', checked: this.sel.has(o.id), onchange: e => { e.target.checked ? this.sel.add(o.id) : this.sel.delete(o.id); this.repanel(); } }), `${o.name} (${o.n})`));
    const cols = this.filterColumns();
    inp.appendChild(row('filter', sel([['', 'all data'], ...cols.map(c => [c, c])], this.opt.filterCol, { onchange: e => { this.opt.filterCol = e.target.value; this.opt.filterVal = ''; this.repanel(); } })));
    if (this.opt.filterCol) inp.appendChild(row('value', sel(this.filterValues(), this.opt.filterVal, { onchange: e => { this.opt.filterVal = e.target.value; } })));
    inp.appendChild(row('max points', num(this.opt.max_points, { onchange: e => { this.opt.max_points = Math.max(3, +e.target.value); } })));
    inp.appendChild(note('The solve is O(n³) in the browser — 250 measurements is about a second, 500 is about ten. Declustering is the right way to get under the cap.'));
    inp.appendChild(row('smoothing', num(this.opt.smoothing, { step: 'any', onchange: e => { this.opt.smoothing = +e.target.value; } })));
    inp.appendChild(row('boundary', sel([['data', 'around the measurements'], ['model', 'whole model bounds']], this.opt.boundary, { onchange: e => { this.opt.boundary = e.target.value; } })));
    inp.appendChild(row('pad %', num(this.opt.pad, { onchange: e => { this.opt.pad = +e.target.value; } })));
    inp.appendChild(row('resolution m', num(this.opt.resolution || '', { placeholder: 'auto', onchange: e => { this.opt.resolution = e.target.value === '' ? 0 : +e.target.value; } })));
    inp.appendChild(row('form surfaces', num(this.opt.thresholds, { min: 1, max: 12, onchange: e => { this.opt.thresholds = Math.max(1, Math.min(12, +e.target.value)); } })));
    inp.appendChild(h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: this.opt.drape, onchange: e => { this.opt.drape = e.target.checked; } }), 'also evaluate onto topography (form lines in map view)'));
    inp.appendChild(btn('BUILD FORM SURFACES', () => this.build(), { class: 'b wide' }));
    if (this.last) inp.appendChild(kv([
      ['Measurements used', String(this.last.meta.n_measurements) + (this.last.meta.thinned ? ` (${this.last.meta.thinned} thinned out)` : '')],
      ['Max residual', this.last.meta.residual_max_deg + '° from the measured pole'],
      ['Mean residual', this.last.meta.residual_mean_deg + '°'],
      ['Surfaces', String(this.last.surfaces)],
      ['Thresholds', this.last.thresholds.map(t => t.toFixed(3)).join(', ')],
    ]));
    if (this.last) inp.appendChild(note('Threshold values are relative to the centre of the box and are not geologically meaningful — they label surfaces, they do not date them.'));
    el.appendChild(inp);

    const tr = section('STRUCTURAL TREND');
    tr.appendChild(note('A trend that varies through space: the anisotropy ratio starts at the strength on each input and halves every range — 5:5:1 on the surface, 2.5:2.5:1 at one range, 1.25:1.25:1 at two, practically isotropic by three.'));
    const meshes = P.objects.filter(o => o.kind === 'mesh' || (o.kind === 'lineset' && o.role === 'faults'));
    for (const o of [...list, ...meshes]) tr.appendChild(h('label', { class: 'chk' },
      h('input', { type: 'checkbox', checked: this.trend.sel.has(o.id), onchange: e => { e.target.checked ? this.trend.sel.add(o.id) : this.trend.sel.delete(o.id); } }), `${o.name}`));
    tr.appendChild(row('strength', num(this.trend.strength, { onchange: e => { this.trend.strength = +e.target.value; } })));
    tr.appendChild(row('range m', num(this.trend.range, { onchange: e => { this.trend.range = +e.target.value; } })));
    tr.appendChild(btn('BUILD TREND FIELD', () => this.buildTrend(), { class: 'b wide' }));
    el.appendChild(tr);

    const gl = section('GLOBAL TREND (one plane)');
    gl.appendChild(note('Sets the anisotropy ellipsoid used by the implicit-surface and kriging tools. Pitch is measured in the plane from strike — it is the direction of maximum continuity.'));
    gl.appendChild(row('dip °', num(this.global.dip, { onchange: e => { this.global.dip = +e.target.value; this.saveGlobal(); } })));
    gl.appendChild(row('dip azimuth °', num(this.global.dipaz, { onchange: e => { this.global.dipaz = +e.target.value; this.saveGlobal(); } })));
    gl.appendChild(row('pitch °', num(this.global.pitch, { onchange: e => { this.global.pitch = +e.target.value; this.saveGlobal(); } })));
    gl.appendChild(row('ratios', num(this.global.ratios[0], { onchange: e => { this.global.ratios[0] = +e.target.value; this.saveGlobal(); } }), num(this.global.ratios[1], { onchange: e => { this.global.ratios[1] = +e.target.value; this.saveGlobal(); } }), num(this.global.ratios[2], { onchange: e => { this.global.ratios[2] = +e.target.value; this.saveGlobal(); } })));
    gl.appendChild(btn('SET FROM THE BINGHAM MEAN PLANE', () => this.fromMean(), { class: 'b wide' }));
    el.appendChild(gl);
    return el;
  }
  repanel() { this.T.showPanel(this.panel()); }
  layers() { return [...this.sel].map(id => this.app.project.get(id)).filter(Boolean); }
  filterColumns() { const s = new Set(); for (const o of this.layers()) for (const c of Object.keys(o.attributes)) if (!o.isNumeric(c) && !['dip', 'dip_azimuth', 'polarity'].includes(c)) s.add(c); return [...s]; }
  filterValues() { const s = new Set(); for (const o of this.layers()) { const col = o.attributes[this.opt.filterCol]; if (!col) continue; for (const v of col) if (v != null && v !== '') s.add(String(v)); } return [...s]; }

  merged() {
    const layers = this.layers();
    if (!layers.length) throw new Error('pick at least one structural layer');
    const out = S.newStructural('form input');
    for (const o of layers) {
      const col = this.opt.filterCol ? o.attributes[this.opt.filterCol] : null;
      const R = S.readStructural(o, { filter: i => !col || String(col[i]) === this.opt.filterVal });
      for (let i = 0; i < R.n; i++) S.addMeasurement(out, R.x[i], R.y[i], R.z[i], R.dip[i], R.dipaz[i], { polarity: R.polarity[i] });
    }
    if (out.n < 3) throw new Error(`only ${out.n} measurement(s) after filtering — at least 3 are needed`);
    return out;
  }
  boundsFor(ps) {
    let b = this.opt.boundary === 'model' ? this.app.project.bounds() : ps.bounds();
    if (!b) throw new Error('no bounds');
    const pad = Math.max(this.opt.pad, 0) / 100;
    const sx = (b[3] - b[0]) * pad, sy = (b[4] - b[1]) * pad, sz = Math.max((b[5] - b[2]) * pad, 50);
    return [b[0] - sx, b[1] - sy, b[2] - sz, b[3] + sx, b[4] + sy, b[5] + sz];
  }
  async build() {
    let ps;
    try { ps = this.merged(); } catch (e) { return toast(e.message, 'warn', 6000); }
    const bounds = this.boundsFor(ps);
    const span = Math.max(bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]);
    const spacing = this.opt.resolution || Math.max(5, span / 48);
    const nodes = Math.ceil((bounds[3] - bounds[0]) / spacing) * Math.ceil((bounds[4] - bounds[1]) / spacing) * Math.ceil((bounds[5] - bounds[2]) / spacing);
    if (nodes > 8e6) return toast(`that resolution needs ${(nodes / 1e6).toFixed(1)} M nodes — increase it`, 'warn', 7000);
    this.app.status('fitting the form interpolant…');
    try {
      const r = await this.app.engine.call('formSurfaces', {
        points: ps, bounds, spacing, count: this.opt.thresholds,
        smoothing: this.opt.smoothing, max_points: this.opt.max_points,
        name: 'Form surface',
      }, (f, n) => this.app.status(n ? `${n} (${Math.round(f * 100)}%)` : `building… ${Math.round(f * 100)}%`));
      const prev = this.app.project.objects.filter(o => o.metadata && o.metadata.form_of === 'form');
      for (const o of prev) this.app.project.remove(o);
      for (const m of r.surfaces) { m.metadata.form_of = 'form'; m.opacity = 0.9; this.app.project.add(m); }
      this.last = { meta: r.meta, thresholds: r.thresholds, surfaces: r.surfaces.length };
      if (this.opt.drape) this.drapeForm(r.interpolant);
      toast(`${r.surfaces.length} form surface(s) — max residual ${r.meta.residual_max_deg}°`, 'ok', 6000);
      if (r.meta.residual_max_deg > 2) toast(`the fit is not honouring every measurement (max ${r.meta.residual_max_deg}°) — try less smoothing, or decluster`, 'warn', 8000);
    } catch (err) { console.error(err); toast('form interpolant failed: ' + err.message, 'err', 9000); }
    this.app.status(''); this.repanel();
  }
  drapeForm(json) {
    const topo = this.app.topoGrid(); if (!topo) return;
    const fi = S.FormInterpolant.fromJSON(json);
    const g = S.evaluateOnto(topo, (x, y, z) => fi.value(x, y, z), 'form');
    if (!g) return;
    g.name = 'Form lines (form interpolant on topography)'; g.group = 'Structure';
    const old = this.app.project.objects.find(o => o.name === g.name); if (old) this.app.project.remove(old);
    const d = this.app.display.get(g.id) || {}; d.mode = 'draped'; d.colormap = 'rdbu'; d.lift = 3; this.app.display.set(g.id, d);
    this.app.project.add(g);
  }
  async buildTrend() {
    const inputs = [...this.trend.sel].map(id => this.app.project.get(id)).filter(Boolean);
    if (!inputs.length) return toast('pick at least one trend input', 'warn');
    const b = this.app.project.bounds(); if (!b) return toast('no bounds', 'warn');
    this.app.status('building the trend field…');
    try {
      const r = await this.app.engine.call('trendGlyphs', { inputs, bounds: b, strength: this.trend.strength, range: this.trend.range, n: 9, name: `Trend S${this.trend.strength} R${this.trend.range}` });
      const old = this.app.project.objects.filter(o => o.role === 'trend'); for (const o of old) this.app.project.remove(o);
      this.app.project.add(r.points);
      toast(`${r.points.n} trend glyphs — ratio halves every ${this.trend.range} m`, 'ok', 5000);
      this.app.select(r.points.id);
    } catch (err) { console.error(err); toast('trend failed: ' + err.message, 'err', 8000); }
    this.app.status('');
  }
  fromMean() {
    try {
      const ps = this.merged(); const R = S.readStructural(ps);
      const b = S.binghamStats(R.poles, R.n);
      this.global.dip = +b.mean_plane.dip.toFixed(1); this.global.dipaz = +b.mean_plane.dip_azimuth.toFixed(1);
      if (b.fabric.startsWith('girdle')) { this.global.pitch = 0; toast(`mean plane ${this.global.dip}° → ${this.global.dipaz}°; the data is a girdle, so the fold hinge (${b.fold_hinge.plunge.toFixed(0)}° → ${b.fold_hinge.trend.toFixed(0)}°) is the real continuity direction — a structural trend will fit this better than one plane.`, 'warn', 9000); }
      else toast(`mean plane ${this.global.dip}° → ${this.global.dipaz}°`, 'ok');
      this.saveGlobal(); this.repanel();
    } catch (e) { toast(e.message, 'warn'); }
  }
  saveGlobal() {
    const P = this.app.project; if (!P) return;
    P.metadata.global_trend = { dip: this.global.dip, dip_azimuth: this.global.dipaz, pitch: this.global.pitch, ratios: this.global.ratios.slice() };
    this.app.markDirty();
  }
}

/** The anisotropy the other tools should use, if a global trend has been set. */
export function globalAnisotropy(project, baseRange) {
  const g = project && project.metadata && project.metadata.global_trend;
  if (!g) return null;
  return S.planeAnisotropy(g.dip, g.dip_azimuth, g.pitch, g.ratios, baseRange);
}
