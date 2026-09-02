/* gm-geom-tools.js — three geometry tools for model3d.html, registered into
   the Tools registry (gm-tools.js) as extra entries of the TOOLS menu:

     extrude   Project a trace down dip: a fault / contact / working trace
               becomes a ribbon (or a capped prism for a closed outline) at a
               dip that is STATED in a document (described), DERIVED from the
               structural readings gm-structural.js produced along that very
               part (inferred), or a typed GUESS (assumed).  The depth is the
               user's projection distance, never a modelled fact, and the
               surface carries the weaker of the trace's confidence and the
               dip's.  Refusals from the engine are printed verbatim.
     contours  Contour any grid (topography, bases, property grids); property
               grids can be draped on the topography.
     plane     A finite plane through a structural measurement (attitude and
               provenance from its columns) or a typed attitude at a clicked
               point — a statement of attitude, not a modelled surface.

   Panel / arm / disarm / stop follow SectionTool.startDraw and
   WorkingsTool.start in gm-tools.js; the numerics live in gm-engine.js
   (extrudePolyline, contourGrid) and gm-structural.js (planeMesh) and run in
   the worker through app.engine where they are heavy enough to matter.
   The module self-installs when it is imported into a page whose
   window.gmApp.tools already exists; installGeomTools() is idempotent.       */
import * as GM from './gm-core.js';
import * as E from './gm-engine.js';
import * as S from './gm-structural.js';
import { h, row, num, txt, sel, btn, note, toast, kv, fmtNum } from './gm-ui.js';

const $ = id => document.getElementById(id);
export const TRACE_ROLES = ['faults', 'geology-outline', 'lines', 'workings'];
/** Confidence, strongest first — a surface is only as good as its weakest input. */
export const CONF_ORDER = ['surveyed', 'sketched', 'inferred', 'described', 'assumed'];
export function weakerConfidence(a, b) {
  const ia = CONF_ORDER.indexOf(a), ib = CONF_ORDER.indexOf(b);
  if (ia < 0) return ib < 0 ? (b || a || 'assumed') : b;
  if (ib < 0) return a;
  return CONF_ORDER[Math.max(ia, ib)];
}
function traceLayers(P) { return P ? P.objects.filter(o => o.kind === 'lineset' && TRACE_ROLES.includes(o.role) && o.parts.length) : []; }
function groundPick(R, e) { const p = R.pick(e.clientX, e.clientY, o => o.kind === 'grid2d' || o.kind === 'mesh' || o.kind === 'imageplane'); return p ? p.world : null; }
/** Part index of a lineset pick: hairlines carry segPart per segment, tubes a
    face -> part map (the same three routes gm-viewer.js reads). */
export function partIndexOf(p) {
  if (!p || !p.obj || p.obj.kind !== 'lineset' || !p.object) return null;
  const ud = p.object.userData || {};
  if (ud.segPart && p.index != null) return ud.segPart[Math.floor(p.index / 2)];
  if (ud.faceRanges && p.faceIndex != null) { for (const [a, b, part] of ud.faceRanges) if (p.faceIndex >= a && p.faceIndex < b) return part; }
  if (ud.faceToPart && p.faceIndex != null) return ud.faceToPart[p.faceIndex];
  if (ud.partIndex != null) return ud.partIndex;
  return null;
}
const featLabel = f => (f && (f.name || f.unit || f.type)) || '';
const az360 = a => ((a % 360) + 360) % 360;

/* ============================================== PROJECT A TRACE DOWN DIP */
export class ExtrudeTool {
  constructor(T) {
    this.T = T; this.layer = null; this.part = null; this.mode = null; this.src = 'stated';
    this.form = { dip: 60, dipaz: 90, depth: 200, role: 'fault', doc: '', page: '', name: '' };
    this.error = null; this.derived = null; this.busy = false; this.last = null;
  }
  get app() { return this.T.app; }
  onProject() { this.layer = null; this.part = null; this.mode = null; this.error = null; this.derived = null; this.last = null; }
  stop() { this.mode = null; this.T.R.clearOverlay(); $('gl').style.cursor = ''; }
  repanel() { this.T.showPanel(this.panel()); }
  partXYZ() { return this.layer && this.part != null && this.layer.parts[this.part] ? this.layer.partXYZ(this.part) : null; }
  feature() { return (this.layer && this.part != null && this.layer.features[this.part]) || {}; }
  hasElevation(xyz) { return xyz.some(p => p[2] === p[2] && p[2] !== 0); }
  isClosed(xyz) { return xyz.length > 2 && xyz[0][0] === xyz[xyz.length - 1][0] && xyz[0][1] === xyz[xyz.length - 1][1]; }
  /** The dip's own confidence: a document statement, a derived mean, or a guess. */
  dipConfidence(vertical = false) { return vertical || this.src === 'guess' ? 'assumed' : this.src === 'derived' ? 'inferred' : 'described'; }

  panel() {
    const P = this.app.project, layers = traceLayers(P), f = this.form;
    const box = h('div', { class: 'tool' });
    box.appendChild(note('Projects a mapped trace down dip into a surface. The DEPTH is your projection distance — how far you choose to draw it — never a modelled fact. The surface carries the weaker of the trace\'s confidence and the dip\'s, and says where the dip came from.'));
    if (!P || !layers.length) { box.appendChild(note('no fault / geology-outline / line / workings layer with parts in this project', 'note warn')); return box; }
    if (!this.layer || !layers.includes(this.layer)) { this.layer = layers[0]; this.part = null; this.derived = null; }
    box.appendChild(row('trace layer', sel(layers.map(l => [l.id, `${l.name} (${l.parts.length} part${l.parts.length === 1 ? '' : 's'}, ${l.role})`]), this.layer.id, { onchange: e => { this.layer = P.get(e.target.value); this.part = null; this.derived = null; this.error = null; this.repanel(); } })));
    const parts = this.layer.parts.map((_, k) => [String(k), `#${k} ${featLabel(this.layer.features[k])}`.trim()]);
    box.appendChild(row('part', sel([['', '— pick —'], ...parts], this.part == null ? '' : String(this.part), { onchange: e => { if (e.target.value === '') { this.part = null; } else this.setPart(+e.target.value); this.repanel(); } }),
      btn(this.mode === 'part' ? 'PICKING… (Esc)' : 'CLICK IT IN THE SCENE', () => this.pickPart(), { title: 'arm a pick: click the trace in the 3-D view' })));
    const xyz = this.partXYZ();
    if (xyz) {
      const feat = this.feature(), strike = E.polylineStrike(xyz), hasZ = this.hasElevation(xyz);
      const zs = xyz.map(p => p[2]).filter(z => z === z);
      box.appendChild(kv([
        ['vertices', `${xyz.length}${this.isClosed(xyz) ? ' — closed outline: it becomes a capped prism' : ' — open trace: it becomes a ribbon'}`],
        ['strike (plan)', `${strike.toFixed(1)}° / ${az360(strike + 180).toFixed(1)}° — a down-dip azimuth near ${az360(strike + 90).toFixed(0)}° or ${az360(strike + 270).toFixed(0)}° is perpendicular`],
        ['elevation', hasZ ? `${fmtNum(Math.min(...zs), 0)} – ${fmtNum(Math.max(...zs), 0)} m` : 'NONE — drape it on the topography first (Structural data ▸ SET ELEVATION), a plan-view trace cannot be projected'],
        ['trace confidence', feat.confidence || 'not stated — treated as sketched (a map digitisation)'],
      ]));
    }
    box.appendChild(row('dip from', sel([['stated', 'stated in a document (described)'], ['derived', 'derived along this part (Bingham mean, inferred)'], ['guess', 'typed guess (assumed)']], this.src, { onchange: e => { this.src = e.target.value; this.derived = null; this.error = null; this.repanel(); } })));
    if (this.src === 'stated') {
      box.appendChild(row('source doc', txt(f.doc, { placeholder: 'USGS Bull. 1234 …', oninput: e => { f.doc = e.target.value; } })));
      box.appendChild(row('page', txt(f.page, { placeholder: 'p. / plate', oninput: e => { f.page = e.target.value; } })));
    }
    if (this.src === 'derived') {
      box.appendChild(h('div', { class: 'frow' }, btn('DERIVE FROM THE READINGS ALONG THIS PART', () => this.derive(), { disabled: xyz == null, title: 'Bingham mean plane of the structural measurements gm-structural derived from this part' })));
      if (this.derived) box.appendChild(note(`Bingham mean of ${this.derived.n} reading${this.derived.n === 1 ? '' : 's'} along part ${this.part}: ${this.derived.dip.toFixed(1)}° → ${this.derived.dip_azimuth.toFixed(1)}° (${this.derived.fabric}) from ${this.derived.layers.join(', ')} — confidence inferred`, 'note ok'));
    }
    box.appendChild(row('dip °', num(f.dip, { min: 0, max: 90, step: 1, disabled: this.src === 'derived', onchange: e => { f.dip = +e.target.value; } })));
    box.appendChild(row('dip azimuth °', num(f.dipaz, { min: 0, max: 360, step: 1, disabled: this.src === 'derived', onchange: e => { f.dipaz = az360(+e.target.value); } }), h('span', { class: 'k' }, 'down-dip, clockwise from N')));
    box.appendChild(row('depth m', num(f.depth, { min: 1, step: 10, onchange: e => { f.depth = +e.target.value; } }), h('span', { class: 'k' }, 'your projection distance, not a fact')));
    box.appendChild(row('role', sel([['fault', 'fault'], ['vein', 'vein'], ['wall', 'wall']], f.role, { onchange: e => { f.role = e.target.value; } })));
    box.appendChild(row('name', txt(f.name, { placeholder: 'defaults to the part and the attitude', oninput: e => { f.name = e.target.value; } })));
    if (this.error) box.appendChild(note(this.error, 'note warn'));
    box.appendChild(h('div', { class: 'frow' },
      btn(this.busy ? 'BUILDING…' : 'BUILD', () => this.build(false), { disabled: this.busy || xyz == null }),
      btn('VERTICAL WALL', () => this.build(true), { disabled: this.busy || xyz == null, title: 'dip 90°, azimuth = strike + 90° — an assumption, tagged as one' })));
    box.appendChild(note('The engine refuses — and the reason is printed here verbatim — a dip azimuth within 20° of the trace\'s strike (the ribbon would collapse onto the trace), a trace without elevation, and a dip outside (0, 90].'));
    if (this.last) box.appendChild(note(`last built: ${this.last.name} — ${this.last.nTriangles} triangles, confidence ${this.last.metadata.confidence}`));
    return box;
  }
  setPart(k) { this.part = k; this.error = null; this.derived = null; if (this.layer) this.app.R.highlight(this.layer, k); }
  pickPart() {
    if (!this.layer) { toast('pick a trace layer first', 'warn'); return; }
    this.mode = 'part';
    this.T.arm(this, `PICK — click the trace to project in ${this.layer.name} · Esc cancels`, `${this.layer.name} → a ${this.form.role} surface`);
    this.repanel();
  }
  onClick(e) {
    if (this.mode !== 'part') return false;
    const id = this.layer.id;
    const p = this.app.R.pick(e.clientX, e.clientY, o => o.id === id);
    if (!p) { toast(`click on a line of ${this.layer.name}`, 'warn'); return true; }
    const k = partIndexOf(p);
    if (k == null || k < 0) { toast('could not tell which part that was — pick it from the list', 'warn'); return true; }
    this.setPart(k); this.mode = null; this.T.disarm(); this.repanel();
    return true;
  }
  onKey(e) { if (this.mode && e.key === 'Escape') { this.stop(); this.T.disarm(); this.repanel(); return true; } return false; }
  /** Structural readings derived from this very part.  gm-structural.js
      deriveFromTraces writes `source` (the trace layer's name) and `part`
      (its index) on every measurement it emits, and provenance.source_id on
      the layer; both routes are honoured. */
  readingsForPart() {
    const rows = [];
    if (!this.layer || this.part == null) return rows;
    for (const o of this.app.project.objects) {
      if (!S.isStructural(o)) continue;
      const src = o.attributes.source || [], part = o.attributes.part || [];
      const fromLayer = !!(o.provenance && o.provenance.source_id === this.layer.id);
      for (let i = 0; i < o.n; i++) {
        const sameLayer = fromLayer || src[i] === this.layer.name;
        if (sameLayer && part[i] != null && +part[i] === this.part) rows.push({ obj: o, row: i });
      }
    }
    return rows;
  }
  derive() {
    const rows = this.readingsForPart();
    if (!rows.length) {
      this.derived = null;
      this.error = `no structural readings were derived along part ${this.part} of ${this.layer ? this.layer.name : '?'} — run STRUCTURAL DATA ▸ DERIVE FROM ALL TRACE LAYERS first (it needs relief along the trace), or state the dip`;
      this.repanel(); return null;
    }
    const poles = new Float64Array(rows.length * 3);
    rows.forEach(({ obj, row }, i) => { const pl = S.poleFromDipAz(+obj.attributes.dip[row], +obj.attributes.dip_azimuth[row], 1); poles[3 * i] = pl[0]; poles[3 * i + 1] = pl[1]; poles[3 * i + 2] = pl[2]; });
    let mean;
    if (rows.length >= 2) { const b = S.binghamStats(poles, rows.length); mean = { dip: b.mean_plane.dip, dip_azimuth: b.mean_plane.dip_azimuth, fabric: b.fabric }; }
    else mean = { dip: +rows[0].obj.attributes.dip[rows[0].row], dip_azimuth: +rows[0].obj.attributes.dip_azimuth[rows[0].row], fabric: 'a single reading' };
    this.derived = Object.assign({ n: rows.length, layers: [...new Set(rows.map(r => r.obj.name))] }, mean);
    this.form.dip = +mean.dip.toFixed(1); this.form.dipaz = +mean.dip_azimuth.toFixed(1);
    this.error = null; this.repanel();
    return this.derived;
  }
  /** Build the surface.  vertical = the VERTICAL WALL shortcut (dip 90,
      azimuth = strike + 90, confidence assumed). */
  async build(vertical = false) {
    this.error = null;
    const xyz = this.partXYZ();
    if (!xyz) { this.error = 'pick a trace layer and one of its parts first'; this.repanel(); return null; }
    const f = this.form, feat = this.feature(), strike = E.polylineStrike(xyz);
    let dip = +f.dip, dipaz = az360(+f.dipaz), how;
    if (vertical) { dip = 90; dipaz = az360(strike + 90); how = 'vertical wall shortcut (dip 90°, azimuth = strike + 90°) — an assumption'; }
    else if (this.src === 'derived') {
      if (!this.derived && !this.derive()) return null;
      dip = this.derived.dip; dipaz = this.derived.dip_azimuth;
      how = `Bingham mean of ${this.derived.n} reading(s) derived along this part (${this.derived.layers.join(', ')})`;
    } else if (this.src === 'guess') how = 'typed guess';
    else how = 'stated' + (f.doc ? ` in ${f.doc}${f.page ? ', ' + f.page : ''}` : ' (no document given)');
    const dipConf = this.dipConfidence(vertical);
    const traceConf = feat.confidence || 'sketched';
    const conf = weakerConfidence(traceConf, dipConf);
    const source = { layer: this.layer.name, layer_id: this.layer.id, part: this.part, feature: featLabel(feat) || null, feature_source: feat.source || null, dip_from: how, trace_confidence: feat.confidence || null, dip_confidence: dipConf };
    const name = f.name || `${featLabel(feat) || this.layer.name} — ${f.role} projected ${dip.toFixed(0)}°→${dipaz.toFixed(0)}°`;
    const args = { xyz, dip, dipAzimuth: dipaz, depth: +f.depth, role: f.role, confidence: conf, name, source, metadata: f.doc ? { source_doc: { doc: f.doc, page: f.page } } : {} };
    this.busy = true; this.repanel();
    try {
      const mesh = await this.app.engine.call('extrudePolyline', args);
      mesh.group = 'Surfaces';
      mesh.metadata.derived_from = [this.layer.id];
      mesh.metadata.confidence = conf; mesh.metadata.dip_confidence = dipConf; mesh.metadata.trace_confidence = feat.confidence || null;
      mesh.provenance = Object.assign({}, mesh.provenance, { source_layer: this.layer.name, source_id: this.layer.id, part: this.part, dip_from: how, depth_m: +f.depth, note: 'the depth is the user\'s projection distance' });
      this.app.project.add(mesh); this.last = mesh;
      toast(`${mesh.name}: ${mesh.nTriangles} triangles, confidence ${conf} (${dipConf} dip on a ${traceConf} trace)`, conf === 'assumed' ? 'warn' : 'ok', 6000);
      return mesh;
    } catch (e) {
      this.error = String(e && e.message || e);
      toast(this.error, 'err', 9000);
      return null;
    } finally { this.busy = false; this.repanel(); }
  }
}

/* ======================================================= CONTOUR A GRID */
export class ContourTool {
  constructor(T) { this.T = T; this.grid = null; this.interval = ''; this.base = 0; this.index = 5; this.drape = true; this.lift = 2; this.busy = false; this.error = null; this.last = null; }
  get app() { return this.T.app; }
  onProject() { this.grid = null; this.interval = ''; this.error = null; this.last = null; }
  stop() { }
  repanel() { this.T.showPanel(this.panel()); }
  defaultInterval(g) { const zr = g.zrange(); return zr[0] === zr[0] ? E.niceInterval(zr[1] - zr[0], 8) : 1; }
  effectiveInterval() { return this.interval === '' || !(+this.interval > 0) ? this.defaultInterval(this.grid) : +this.interval; }
  panel() {
    const P = this.app.project, grids = P ? P.byKind('grid2d') : [];
    const box = h('div', { class: 'tool' });
    box.appendChild(note('Contour lines from any grid: topography, a stratigraphic base, a property grid (magnetics, a form interpolant on the ground…). Property grids are draped on the topography, lifted 2 m so they stay visible.'));
    if (!grids.length) { box.appendChild(note('no grid in this project', 'note warn')); return box; }
    if (!this.grid || !grids.includes(this.grid)) { this.grid = grids.find(g => g.role === 'topography') || grids[0]; this.interval = ''; }
    const g = this.grid, zr = g.zrange(), def = this.defaultInterval(g), interval = this.effectiveInterval();
    box.appendChild(row('grid', sel(grids.map(x => [x.id, `${x.name} (${x.role}${x.units ? ', ' + x.units : ''})`]), g.id, { onchange: e => { this.grid = P.get(e.target.value); this.interval = ''; this.error = null; this.repanel(); } })));
    let nLevels = null; try { nLevels = E.contourLevels(g, interval, +this.base || 0).length; } catch (e) { nLevels = e.message; }
    box.appendChild(kv([['value range', zr[0] === zr[0] ? `${fmtNum(zr[0], 1)} – ${fmtNum(zr[1], 1)} ${g.units || 'm'}` : 'no data'], ['levels', typeof nLevels === 'number' ? `${nLevels} at ${interval} ${g.units || 'm'}` : nLevels]]));
    box.appendChild(row(`interval ${g.units || 'm'}`, num(this.interval === '' ? def : this.interval, { min: 0, step: 'any', onchange: e => { this.interval = e.target.value === '' ? '' : +e.target.value; this.repanel(); } }), h('span', { class: 'k' }, `default ${def} (range / 8, rounded to 1-2-5)`)));
    box.appendChild(row('base', num(this.base, { step: 'any', onchange: e => { this.base = +e.target.value || 0; this.repanel(); } }), h('span', { class: 'k' }, 'levels are base + n × interval')));
    box.appendChild(row('index every', num(this.index, { min: 0, step: 1, onchange: e => { this.index = Math.max(0, e.target.value | 0); } }), h('span', { class: 'k' }, 'Nth level is marked index (0 = none)')));
    if (g.role === 'property') {
      const topo = this.app.topoGrid();
      box.appendChild(row('drape on topography', h('input', { type: 'checkbox', checked: this.drape, onchange: e => { this.drape = e.target.checked; this.repanel(); } }), topo ? h('span', { class: 'k' }, topo.name) : h('span', { class: 'k' }, 'no topography — the lines go at z = 0')));
      if (this.drape) box.appendChild(row('lift m', num(this.lift, { step: 'any', onchange: e => { this.lift = +e.target.value || 0; } })));
    } else box.appendChild(note('a heightfield\'s lines sit at their own level'));
    if (this.error) box.appendChild(note(this.error, 'note warn'));
    box.appendChild(h('div', { class: 'frow' }, btn(this.busy ? 'BUILDING…' : 'BUILD', () => this.build(), { disabled: this.busy })));
    if (this.last) box.appendChild(note(`last built: ${this.last.name} — ${this.last.parts.length} lines`));
    return box;
  }
  async build() {
    const g = this.grid; if (!g) return null;
    const interval = this.effectiveInterval(), base = +this.base || 0, topo = this.app.topoGrid();
    const args = { grid: g, interval, base, index: +this.index || 0, name: `${g.name} contours (${interval} ${g.units || 'm'})` };
    this.error = null;
    if (g.role === 'property') {
      if (this.drape && topo) { args.drape = topo; args.lift = +this.lift || 0; }
      else if (this.drape && !topo) this.error = 'no topography to drape on — the lines were put at z = 0';
    }
    this.busy = true; this.repanel();
    try {
      const ls = await this.app.engine.call('gridContours', args);
      ls.group = 'Surfaces';
      ls.metadata.derived_from = args.drape ? [g.id, topo.id] : [g.id];
      ls.metadata.interval = interval; ls.metadata.base = base; ls.metadata.index_every = +this.index || 0; ls.metadata.units = g.units || 'm';
      ls.provenance = Object.assign({}, ls.provenance, { source_layer: g.name, source_id: g.id, interval, draped_on: args.drape ? topo.name : null });
      if (!ls.parts.length) toast(`no contour of ${g.name} at ${interval} — try a finer interval`, 'warn', 6000);
      this.app.project.add(ls); this.last = ls;
      toast(`${ls.name}: ${ls.parts.length} lines, ${ls.metadata.contours.n_levels} levels`, 'ok');
      return ls;
    } catch (e) {
      this.error = String(e && e.message || e); toast(this.error, 'err', 8000); return null;
    } finally { this.busy = false; this.repanel(); }
  }
}

/* ============================================= PLANE FROM A MEASUREMENT */
export class PlaneTool {
  constructor(T) {
    this.T = T; this.mode = null; this.from = null; this.at = null;
    this.form = { dip: 45, dipaz: 90, halfStrike: 150, halfDip: 150, role: 'vein', confidence: 'described', doc: '', page: '', name: '' };
    this.error = null; this.last = null;
  }
  get app() { return this.T.app; }
  onProject() { this.from = null; this.at = null; this.mode = null; this.error = null; this.last = null; }
  stop() { this.mode = null; this.T.R.clearOverlay(); $('gl').style.cursor = ''; }
  repanel() { this.T.showPanel(this.panel()); }
  panel() {
    const f = this.form, box = h('div', { class: 'tool' });
    box.appendChild(note('A finite rectangle through a point with a stated attitude — a statement of attitude, not a modelled surface. Pick a structural measurement (it brings its dip, dip azimuth, source and confidence) or click the ground and type the attitude.'));
    box.appendChild(h('div', { class: 'frow' },
      btn(this.mode === 'measure' ? 'PICKING… (Esc)' : 'PICK A MEASUREMENT', () => this.arm('measure'), { title: 'click a structural glyph in the scene' }),
      btn(this.mode === 'ground' ? 'CLICKING… (Esc)' : 'CLICK THE GROUND', () => this.arm('ground'), { title: 'the plane goes through the clicked point' })));
    if (this.from) {
      const o = this.from.layer, i = this.from.row, A = o.attributes;
      box.appendChild(kv([['measurement', `${o.name} #${i}`], ['attitude', `${(+A.dip[i]).toFixed(0)}° → ${(+A.dip_azimuth[i]).toFixed(0)}°`], ['confidence', A.confidence && A.confidence[i] ? String(A.confidence[i]) : 'not stated'], ['source', A.source && A.source[i] ? String(A.source[i]) : '—'], ['location', this.at ? `E ${fmtNum(this.at[0], 0)} N ${fmtNum(this.at[1], 0)} Z ${fmtNum(this.at[2], 0)}` : '—']]));
    } else if (this.at) box.appendChild(kv([['location', `E ${fmtNum(this.at[0], 0)} N ${fmtNum(this.at[1], 0)} Z ${fmtNum(this.at[2], 0)} (typed attitude)`]]));
    else box.appendChild(note('nothing picked yet', 'note warn'));
    box.appendChild(row('dip °', num(f.dip, { min: 0, max: 90, step: 1, onchange: e => { f.dip = +e.target.value; } })));
    box.appendChild(row('dip azimuth °', num(f.dipaz, { min: 0, max: 360, step: 1, onchange: e => { f.dipaz = az360(+e.target.value); } }), h('span', { class: 'k' }, 'down-dip, clockwise from N')));
    box.appendChild(row('half along strike m', num(f.halfStrike, { min: 1, step: 10, onchange: e => { f.halfStrike = +e.target.value; } })));
    box.appendChild(row('half down dip m', num(f.halfDip, { min: 1, step: 10, onchange: e => { f.halfDip = +e.target.value; } })));
    box.appendChild(row('role', sel([['vein', 'vein'], ['fault', 'fault']], f.role, { onchange: e => { f.role = e.target.value; } })));
    if (!this.from) {
      box.appendChild(row('confidence', sel(CONF_ORDER.filter(c => c !== 'surveyed').map(c => [c, c]), f.confidence, { onchange: e => { f.confidence = e.target.value; } })));
      box.appendChild(row('source doc', txt(f.doc, { placeholder: 'where the attitude is stated', oninput: e => { f.doc = e.target.value; } })));
      box.appendChild(row('page', txt(f.page, { oninput: e => { f.page = e.target.value; } })));
    }
    box.appendChild(row('name', txt(f.name, { placeholder: 'defaults to the role and attitude', oninput: e => { f.name = e.target.value; } })));
    if (this.error) box.appendChild(note(this.error, 'note warn'));
    box.appendChild(h('div', { class: 'frow' }, btn('BUILD', () => this.build(), { disabled: !this.at })));
    box.appendChild(note('The extents are yours, not the deposit\'s: the plane is drawn at the size you give it, through the point, at the attitude — nothing here is interpolated.'));
    if (this.last) box.appendChild(note(`last built: ${this.last.name} (${this.last.metadata.confidence})`));
    return box;
  }
  arm(mode) {
    this.mode = this.mode === mode ? null : mode;
    if (!this.mode) { this.T.disarm(); this.repanel(); return; }
    this.T.arm(this, mode === 'measure' ? 'PLANE — click a structural measurement (a disc / triangle glyph) · Esc cancels' : 'PLANE — click the ground where the plane goes through · Esc cancels', mode === 'measure' ? 'attitude + provenance from the measurement' : 'typed attitude');
    this.repanel();
  }
  onClick(e) {
    if (!this.mode) return false;
    const R = this.app.R;
    if (this.mode === 'measure') {
      const p = R.pick(e.clientX, e.clientY, o => o.kind === 'points' && o.role === 'structural');
      if (!p || !p.obj) { toast('click a structural measurement', 'warn'); return true; }
      let row = p.index;
      if (row == null || row < 0 || row >= p.obj.n) {                 // a glyph mesh hit: take the nearest row
        let best = -1, bd = Infinity;
        for (let i = 0; i < p.obj.n; i++) { const d = Math.hypot(p.obj.xyz[3 * i] - p.world[0], p.obj.xyz[3 * i + 1] - p.world[1], p.obj.xyz[3 * i + 2] - p.world[2]); if (d < bd) { bd = d; best = i; } }
        row = best;
      }
      if (row < 0) { toast('no measurement there', 'warn'); return true; }
      this.setFrom(p.obj, row);
    } else {
      const w = groundPick(R, e);
      if (!w) { toast('click on the ground or a surface', 'warn'); return true; }
      this.at = w; this.from = null; this.error = null;
    }
    this.mode = null; this.T.disarm(); this.repanel();
    return true;
  }
  onKey(e) { if (this.mode && e.key === 'Escape') { this.stop(); this.T.disarm(); this.repanel(); return true; } return false; }
  setFrom(obj, row) {
    const A = obj.attributes;
    this.from = { layer: obj, row }; this.at = obj.point(row);
    this.form.dip = +A.dip[row]; this.form.dipaz = az360(+A.dip_azimuth[row]);
    this.form.confidence = A.confidence && A.confidence[row] ? String(A.confidence[row]) : 'described';
    this.error = null;
    this.app.R.highlight(obj, row);
  }
  build() {
    this.error = null;
    if (!this.at) { this.error = 'pick a measurement or click the ground first'; this.repanel(); return null; }
    const f = this.form, [x, y, z] = this.at;
    let fm = null, conf = f.confidence || 'described', source, polarity = 1;
    if (this.from) {
      const o = this.from.layer, i = this.from.row, A = o.attributes;
      const col = k => (A[k] && A[k][i] != null && A[k][i] !== '' ? A[k][i] : null);
      conf = col('confidence') ? String(col('confidence')) : 'described';
      polarity = col('polarity') == null ? 1 : (+col('polarity') < 0 ? -1 : 1);
      fm = { layer: o.name, layer_id: o.id, row: i, dip: +A.dip[i], dip_azimuth: +A.dip_azimuth[i], polarity, confidence: conf, source: col('source'), page: col('page'), type: col('type'), edited: Math.abs(+f.dip - +A.dip[i]) > 1e-9 || Math.abs(az360(+f.dipaz) - az360(+A.dip_azimuth[i])) > 1e-9 };
      source = col('source') || `${o.name} #${i}`;
    } else source = f.doc ? { doc: f.doc, page: f.page } : 'typed attitude';
    try {
      const mesh = S.planeMesh(x, y, z, +f.dip, az360(+f.dipaz), +f.halfStrike, +f.halfDip, { role: f.role, name: f.name || undefined, confidence: conf, from_measurement: fm, source, polarity });
      mesh.group = 'Surfaces';
      if (this.from) mesh.metadata.derived_from = [this.from.layer.id];
      mesh.provenance = Object.assign({}, mesh.provenance, { note: 'a statement of attitude, not a modelled surface', extents_m: [+f.halfStrike, +f.halfDip] });
      this.app.project.add(mesh); this.last = mesh;
      toast(`${mesh.name}: ${conf}${fm && fm.edited ? ' (attitude edited from the measurement)' : ''}`, 'ok');
      return mesh;
    } catch (e) {
      this.error = String(e && e.message || e); toast(this.error, 'err', 8000); return null;
    } finally { this.repanel(); }
  }
}

/* ============================================================= install */
/** Register the three tools (idempotent: a page that imports this module
    after gm-viewer.js has already installed it gets the same instances). */
export function installGeomTools(tools) {
  if (!tools || typeof tools.register !== 'function') return null;
  if (tools.all && tools.all.extrude && tools.all.contours && tools.all.plane) return { extrude: tools.all.extrude, contours: tools.all.contours, plane: tools.all.plane };
  const extrude = new ExtrudeTool(tools), contours = new ContourTool(tools), plane = new PlaneTool(tools);
  tools.register('extrude', extrude, { label: 'Project a trace down dip', title: 'Project a trace down dip', hint: 'fault / contact / working trace → surface at a stated, derived or guessed dip' });
  tools.register('contours', contours, { label: 'Contour a grid', title: 'Contour a grid', hint: 'topography, bases, property grids → contour lines' });
  tools.register('plane', plane, { label: 'Plane from a measurement', title: 'Plane from a measurement', hint: 'a finite plane through a structural measurement or a typed attitude' });
  tools.extrude = extrude; tools.contours = contours; tools.plane = plane;
  return { extrude, contours, plane };
}
if (typeof window !== 'undefined' && window.gmApp && window.gmApp.tools) {
  try { installGeomTools(window.gmApp.tools); } catch (e) { console.warn('gm-geom-tools: ' + (e && e.message)); }
}
