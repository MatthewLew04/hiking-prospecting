/* gm-map-model.js — 'Model the rock from the map' for model3d.html:
   Leapfrog's Model From Map made one click, labelled inferred, refusing
   where the ground cannot say (GEOMODEL.md §7).

     mapmodel  contacts + derived dips → pancake.  The mapped unit outlines
               (the 'Geology outlines' group) are ordered by their ages;
               where a unit's outline meets an older unit's outline that is
               the younger unit's base cropping out; where an orientation
               was DERIVED nearby from the trace itself (step 3) one point
               is added a fixed distance down dip; the base surfaces go
               through those points and the existing buildStratigraphy /
               stratigraphyVolumes ops stack them.  Every output carries
               provenance {method: 'model from map', confidence: 'inferred'}
               and metadata.derived_from; a unit that touches nothing older
               is skipped and named; a map with no derivable dip yields
               heightfields through the contacts and says so on every base.
     water     a horizontal plane at a STATED water level (an elevation or
               a depth below the collar, with a source) — never a computed
               head: 'described' when a source is given, 'assumed' otherwise.

   The numerics are gm-engine.js unitOrder / sharedContacts / dipOffsets /
   buildFromMap (worker op 'mapModelInputs'); the panel follows the
   ExtrudeTool pattern in gm-geom-tools.js.  The module self-installs when
   imported into a page whose window.gmApp.tools exists;
   installMapModelTools() is idempotent.                                     */
import * as GM from './gm-core.js';
import * as E from './gm-engine.js';
import * as S from './gm-structural.js';
import { h, row, num, txt, sel, btn, note, toast, kv, section, fmtNum } from './gm-ui.js';

export const MAPMODEL_SENTENCE = 'Inferred from the map: unit bases are surfaces through the mapped contacts, bent down dip where an orientation was derived nearby. Not a survey of what is underground.';
export const MAPMODEL_NAME = 'Rock from the map (inferred)';
export const WATER_NOTE = 'water level as stated; not a modelled head';
export const WATER_COLOR = [60, 120, 220];

const finite = v => (v == null || v === '') ? null : (isFinite(+v) ? +v : null);

/** The mapped units of a project: one entry per map unit (same name and
    age text → the same unit, however many polygons it has), with a merged
    outline LineSet, its ages from the outline features, the draped geology
    mesh of the same unit_id, or — when the site builder dropped that mesh
    (triangle budget, a ring ear-clip could not take) — the AOI bundle the
    meshes were read from (``ages``: unit id → {t0, t1, ...}, see
    MapModelTool.loadAges); its colour comes from the mesh when there is one. */
export function unitsFromProject(P, ages = null) {
  const out = new Map();
  if (!P) return [];
  const outlines = P.objects.filter(o => o.kind === 'lineset' && o.role === 'geology-outline' && o.parts.length);
  const meshes = P.byKind('mesh').filter(m => m.role === 'geology');
  for (const ls of outlines) {
    ls.parts.forEach((part, k) => {
      const f = ls.features[k] || {};
      const uid = f.unit_id != null ? String(f.unit_id) : null;
      const mesh = uid ? meshes.find(m => m.metadata && m.metadata.unit_id != null && String(m.metadata.unit_id) === uid) : null;
      const name = f.unit != null && f.unit !== '' ? String(f.unit) : (mesh ? mesh.name : ls.name.replace(/ outline$/i, ''));
      const age = f.age != null ? String(f.age) : (mesh && mesh.metadata.age != null ? String(mesh.metadata.age) : '');
      const key = name + '|' + age;
      let u = out.get(key);
      if (!u) {
        u = { id: uid || GM.slug(name), name, age, t0: null, t1: null, age_from: null, color: mesh ? mesh.color.slice() : E.unitColor({ id: uid || name }), lithology: mesh && mesh.metadata.lithology ? String(mesh.metadata.lithology) : '', description: mesh && mesh.metadata.description ? String(mesh.metadata.description) : '', outline: new GM.LineSet({ name: name + ' outline', role: 'geology-outline', color: ls.color }), layer_ids: [], unit_ids: [], mesh_ids: [], polygons: 0, age_conflicts: 0 };
        out.set(key, u);
      }
      let t0 = f.t0 != null ? finite(f.t0) : (f.t0_ma != null ? finite(f.t0_ma) : (mesh && mesh.metadata.t0_ma != null ? finite(mesh.metadata.t0_ma) : null));
      let t1 = f.t1 != null ? finite(f.t1) : (f.t1_ma != null ? finite(f.t1_ma) : (mesh && mesh.metadata.t1_ma != null ? finite(mesh.metadata.t1_ma) : null));
      let from = t0 != null || t1 != null ? (f.t0 != null || f.t1 != null || f.t0_ma != null ? 'outline feature' : 'draped geology mesh') : null;
      if (t0 == null && t1 == null && uid && ages && ages.get(uid)) { const b = ages.get(uid); t0 = finite(b.t0); t1 = finite(b.t1); if (t0 != null || t1 != null) from = 'geology bundle'; if (!u.lithology && b.lithology) u.lithology = String(b.lithology); if (!u.description && b.description) u.description = String(b.description); if (!u.age && b.age) u.age = String(b.age); }
      if (u.t0 == null && u.t1 == null) { u.t0 = t0; u.t1 = t1; u.age_from = from; }
      else if ((t0 != null || t1 != null) && (t0 !== u.t0 || t1 !== u.t1)) u.age_conflicts++;
      u.outline.addPolyline(ls.partXYZ(k), f);
      u.polygons++;
      if (!u.layer_ids.includes(ls.id)) u.layer_ids.push(ls.id);
      if (uid && !u.unit_ids.includes(uid)) u.unit_ids.push(uid);
      if (mesh && !u.mesh_ids.includes(mesh.id)) u.mesh_ids.push(mesh.id);
    });
  }
  return [...out.values()];
}
export function structuralLayers(P) { return P ? P.objects.filter(o => S.isStructural(o) && o.n > 0) : []; }
export function faultLayers(P) { return P ? P.objects.filter(o => o.kind === 'lineset' && o.role === 'faults' && o.parts.length) : []; }
/** The lattice the bases are interpolated on: the topography thinned to
    <= res nodes per side (StratTool.lattice, kept local). */
export function latticeOf(topo, res = 60) {
  const stride = Math.max(1, Math.ceil(Math.max(topo.nx, topo.ny) / Math.max(4, res | 0)));
  const lat = new GM.Grid2D({ nx: Math.ceil(topo.nx / stride), ny: Math.ceil(topo.ny / stride), x0: topo.x0, y0: topo.y0, dx: topo.dx * stride, dy: topo.dy * stride, rotation: topo.rotation, name: 'lattice' });
  for (let j = 0; j < lat.ny; j++) for (let i = 0; i < lat.nx; i++) { const [x, y] = lat.nodeXY(i, j); lat.values[j * lat.nx + i] = topo.sample(x, y); }
  return lat;
}

/* ============================================ MODEL THE ROCK FROM THE MAP */
export class MapModelTool {
  constructor(T) {
    this.T = T; this.busy = false; this.error = null; this.result = null; this.deriving = false;
    this.opts = { tol: '', radius: 300, offset: 100, nodes: 60, method: 'rbf' };
    this.water = { mode: 'elev', value: '', source: '', page: '' }; this.lastWater = null; this.waterError = null;
    this.ages = null; this.agesFor = null; this.agesLoading = null; this.agesError = null;
  }
  get app() { return this.T.app; }
  onProject() { this.result = null; this.error = null; this.lastWater = null; this.waterError = null; this.ages = null; this.agesFor = null; this.agesLoading = null; this.agesError = null; }
  stop() { }
  repanel() { this.T.showPanel(this.panel()); }
  /** The ages of the site's AOI bundle (data/geology/<aoi>.json — the file
      the draped meshes and outlines were read from), by unit id, for units
      whose mesh the site builder dropped.  Cached per AOI; a bundle that
      cannot be fetched leaves those units unaged, and says so. */
  async loadAges() {
    const P = this.app.project, aoi = P && P.site && P.site.aoi ? String(P.site.aoi) : null;
    if (!aoi) return null;
    if (this.ages && this.agesFor === aoi) return this.ages;
    if (this.agesLoading) return this.agesLoading;
    this.agesLoading = (async () => {
      try {
        const r = await fetch(`data/geology/${encodeURIComponent(aoi)}.json`); if (!r.ok) throw new Error(`${r.status}`);
        const geo = await r.json();
        this.ages = new Map((geo.units || []).map(u => [String(u.id), { t0: u.t0, t1: u.t1, age: u.age, name: u.nm, lithology: u.li, description: u.de }]));
        this.agesError = null;
      } catch (e) { this.ages = new Map(); this.agesError = `geology bundle data/geology/${aoi}.json unavailable (${e && e.message || e}) — units without a draped mesh stay unaged`; }
      this.agesFor = aoi; this.agesLoading = null;
      return this.ages;
    })();
    return this.agesLoading;
  }
  units() { return unitsFromProject(this.app.project, this.ages); }
  /** The existing map model, if any (never created here: the build does). */
  find() { const P = this.app.project; return P ? P.byKind('stratmodel').find(m => m.metadata && m.metadata.map_model) || null : null; }
  /** Elevation of the collar: the topography at the site point (or at the
      project origin when the project has no site). */
  collar() {
    const P = this.app.project, topo = this.app.topoGrid(); if (!P || !topo) return null;
    let x, y, from;
    if (P.site && P.site.lon != null && P.site.lat != null && P.crs && P.crs.zone) { [x, y] = GM.utm.fwd(+P.site.lon, +P.site.lat, P.crs.zone, P.crs.north !== false); from = `topography at the site point (${P.site.name || 'site'})`; }
    else if (P.origin) { x = P.origin[0]; y = P.origin[1]; from = 'topography at the project origin (no site point)'; }
    else return null;
    const z = topo.sample(x, y); return z === z ? { z, x, y, from } : null;
  }

  panel() {
    const P = this.app.project, box = h('div', { class: 'tool' }, h('h2', {}, 'MODEL THE ROCK FROM THE MAP'));
    box.appendChild(note(MAPMODEL_SENTENCE, 'note warn'));
    // the bundle ages arrive asynchronously: the panel re-renders once when they do
    if (P && P.site && P.site.aoi && !(this.ages && this.agesFor === String(P.site.aoi)) && !this.agesLoading) this.loadAges().then(() => { if (this.T.active === this && !this.busy) this.repanel(); });
    const topo = P ? this.app.topoGrid() : null, units = this.units(), struct = structuralLayers(P), faults = faultLayers(P);
    const aged = units.filter(u => u.t0 != null || u.t1 != null), nRead = struct.reduce((a, l) => a + l.n, 0);
    /* NEEDS */
    const needs = h('div', { class: 'needs' },
      h('span', { class: 'need ' + (units.length ? 'ok' : 'no'), title: 'the Geology outlines group: one draped outline per mapped unit, with unit ids and ages' }, `${units.length ? '✓' : '✗'} geology outlines with unit ids — ${units.length ? `${units.length} unit${units.length > 1 ? 's' : ''} (${aged.length} with ages)` : 'none: open the model from the map with USGS GEOLOGY on'}`),
      h('span', { class: 'need ' + (topo ? 'ok' : 'no') }, `${topo ? '✓' : '✗'} topography${topo ? ` · cell ${fmtNum(topo.dx, 0)} m` : ''}`),
      h('span', { class: 'need ' + (nRead ? 'ok' : 'opt'), title: 'optional: without them the bases are heightfields through the contacts and say so' }, `${nRead ? '✓' : '○'} structural readings (optional) — ${nRead ? `${nRead} in ${struct.length} layer${struct.length > 1 ? 's' : ''}` : 'none yet'}`),
      faults.length ? h('span', { class: 'need opt', title: 'fault blocks are not modelled: the bases run continuously across the faults, and readings derived along fault traces are not used as bedding dips' }, `○ ${faults.length} fault layer${faults.length > 1 ? 's' : ''} — not honoured (stated in the result)`) : null);
    const needBox = section('NEEDS', needs);
    if (!nRead && units.length && topo) {
      needBox.appendChild(note('No orientations yet. DERIVE FIRST runs step 3 on every mapped trace (the three-point problem where each contact crosses the terrain) so the bases can bend down dip; without it they follow the mapped contacts at the surface only — and are labelled that way.', 'note'));
      needBox.appendChild(h('div', { class: 'frow' }, btn(this.deriving ? 'DERIVING…' : 'DERIVE FIRST', () => this.deriveFirst(), { disabled: this.deriving || this.busy, class: 'primary', title: 'TOOLS ▸ Structural data ▸ DERIVE FROM ALL TRACE LAYERS, then come back here' })));
    }
    box.appendChild(needBox);
    if (units.length) {
      const list = h('div', { class: 'units' });
      const ord = E.unitOrder(units);
      for (const i of ord.order) { const u = units[i]; list.appendChild(h('div', { class: 'frow', style: { alignItems: 'center' } }, h('span', { class: 'sw', style: { background: `rgb(${u.color.join(',')})`, display: 'inline-block', width: '12px', height: '12px', marginRight: '6px' } }), h('span', {}, `${u.name}`), h('span', { class: 'k', title: u.age_from ? `ages from the ${u.age_from}` : '' }, u.t0 != null || u.t1 != null ? ` ${fmtNum(u.t0, 2)} – ${fmtNum(u.t1, 2)} Ma${u.age ? ` (${u.age})` : ''}` : this.agesLoading ? ' ages loading…' : ' no age — not placed'), h('span', { class: 'k' }, ` · ${u.polygons} polygon${u.polygons > 1 ? 's' : ''}, ${u.outline.nVertices} vertices`))); }
      const ordBox = section('ORDER (youngest first, from the map ages)', list);
      for (const w of ord.warnings) ordBox.appendChild(note(w, 'note warn'));
      if (this.agesError) ordBox.appendChild(note(this.agesError, 'note warn'));
      if (units.some(u => u.age_from === 'geology bundle')) ordBox.appendChild(note(`ages of ${units.filter(u => u.age_from === 'geology bundle').map(u => u.name).join(', ')} read from the geology bundle (their draped mesh was not built at this radius)`));
      box.appendChild(ordBox);
    }
    /* OPTIONS */
    const o = this.opts;
    const opt = section('OPTIONS',
      row('contact tolerance m', num(o.tol, { min: 0, step: 'any', placeholder: topo ? `${fmtNum(topo.dx, 0)} (topo cell)` : 'topo cell', onchange: e => { o.tol = e.target.value === '' ? '' : +e.target.value; } }), h('span', { class: 'k' }, 'a vertex of one outline this close to an older outline is a contact')),
      row('dip search radius m', num(o.radius, { min: 1, step: 10, onchange: e => { o.radius = +e.target.value; } }), h('span', { class: 'k' }, 'nearest derived reading used for a contact')),
      row('offset m', num(o.offset, { min: 1, step: 10, onchange: e => { o.offset = +e.target.value; } }), h('span', { class: 'k' }, 'one point this far down dip of each contact')),
      row('lattice nodes', num(o.nodes, { min: 8, step: 4, onchange: e => { o.nodes = +e.target.value; } }), h('span', { class: 'k' }, `× per side${topo ? ` · cell ${fmtNum(latticeOf(topo, o.nodes).dx, 0)} m` : ''}`)),
      row('interpolation', sel([['rbf', 'RBF (thin-plate, linear drift)'], ['ok', 'ordinary kriging (auto variogram)'], ['idw', 'inverse distance'], ['nn', 'nearest']], o.method, { onchange: e => { o.method = e.target.value; } })));
    box.appendChild(opt);
    if (this.error) box.appendChild(note(this.error, 'note warn'));
    const can = !!topo && aged.length >= 2 && !this.busy;
    box.appendChild(h('div', { class: 'frow' }, btn(this.busy ? 'BUILDING…' : 'BUILD', () => this.build(), { disabled: !can, class: 'primary', title: can ? 'contacts → dip offsets → base surfaces → unit volumes' : !topo ? 'needs a topography grid' : aged.length < 2 ? 'needs at least two mapped units with ages' : 'building' })));
    box.appendChild(note('Refused, and said so in the RESULT: a unit whose outline touches no older unit (nothing says where its base is), a unit with fewer than 3 contact points, units without an age. Units with the same ages are not treated as older or younger than each other. Faults are not honoured — the bases run across them.'));
    /* RESULT */
    if (this.result) box.appendChild(this.resultBlock(this.result));
    /* WATER LEVEL */
    box.appendChild(this.waterBlock());
    return box;
  }
  resultBlock(r) {
    const st = r.stats || {}, blk = h('div', { class: 'psec mm-result' }, h('h3', {}, 'RESULT'));
    const rows = [['built', r.built ? `${r.built.bases} base surface${r.built.bases === 1 ? '' : 's'}, ${r.built.volumes} unit volume${r.built.volumes === 1 ? '' : 's'} · lattice ${r.built.lattice} · ${r.built.at}` : 'nothing built']];
    rows.push(['units modelled', (st.units_modelled || []).length ? (st.units_modelled || []).map(n => `${n} (${st.contacts_per_unit[n]} contacts + ${st.offsets_per_unit[n] || 0} offsets)`).join('; ') : 'none']);
    if (st.basement) rows.push(['basement', `${st.basement} (oldest aged unit; no base)`]);
    const skipped = [];
    for (const n of (st.rejected && st.rejected.no_contacts) || []) skipped.push(`${n} — touches no older unit`);
    for (const n of (st.rejected && st.rejected.few_contacts) || []) skipped.push(`${n} — fewer than ${st.min_contacts} contacts (${st.contacts_per_unit[n]})`);
    for (const n of (st.rejected && st.rejected.no_age) || []) skipped.push(`${n} — no age`);
    rows.push(['skipped', skipped.length ? skipped.join('; ') : 'none']);
    rows.push(['contacts per unit', Object.keys(st.contacts_per_unit || {}).length ? Object.entries(st.contacts_per_unit).map(([n, c]) => `${n}: ${c}`).join(', ') : '—']);
    rows.push(['dips used', `${st.dips_used || 0} offset point${st.dips_used === 1 ? '' : 's'} from ${st.readings || 0} reading${st.readings === 1 ? '' : 's'}${st.rejected && st.rejected.readings_excluded ? ` (${st.rejected.readings_excluded} along faults left out)` : ''}`]);
    rows.push(['units without dip', (st.units_without_dip || []).length ? st.units_without_dip.join(', ') : st.no_dip ? 'all — no readings anywhere' : 'none']);
    if (st.ties && st.ties.length) rows.push(['same ages', st.ties.map(t => t.join(' / ')).join('; ')]);
    rows.push(['dropped', `${(st.rejected && st.rejected.edge_vertices) || 0} outline vertices on the model box edge (clipping), ${(st.rejected && st.rejected.nodata) || 0} over no-data terrain`]);
    blk.appendChild(kv(rows));
    for (const w of r.warnings || []) blk.appendChild(note(w, 'note warn'));
    blk.appendChild(note('Every base, volume and contact layer carries provenance method "model from map", confidence "inferred", and the layers it was derived from. Open a section (step 9) to see the fill; the Stratigraphy tool (step 6) can rebuild from the same contact layers.'));
    return blk;
  }
  waterBlock() {
    const w = this.water, collar = this.collar();
    const blk = section('WATER LEVEL',
      note('A stated water table — from a report, a shaft log, a well — drawn as a horizontal plane across the model box. It is what the source says, not a modelled head.'),
      row('given as', sel([['elev', 'elevation (m)'], ['collar', 'below the collar (m)']], w.mode, { onchange: e => { w.mode = e.target.value; this.repanel(); } })),
      row(w.mode === 'collar' ? 'depth below collar m' : 'elevation m', num(w.value, { step: 'any', placeholder: w.mode === 'collar' ? 'e.g. 120' : 'e.g. 1450', onchange: e => { w.value = e.target.value; } }), w.mode === 'collar' ? h('span', { class: 'k' }, collar ? `collar ${fmtNum(collar.z, 0)} m — ${collar.from}` : 'no collar: no site point or topography') : null),
      row('source', txt(w.source, { placeholder: 'USGS Bull. 1234 …  (empty = assumed)', oninput: e => { w.source = e.target.value; } })),
      row('page', txt(w.page, { placeholder: 'p. / plate', oninput: e => { w.page = e.target.value; } })));
    if (this.waterError) blk.appendChild(note(this.waterError, 'note warn'));
    blk.appendChild(h('div', { class: 'frow' }, btn('ADD WATER LEVEL', () => this.addWater(), { disabled: !this.app.topoGrid() })));
    if (this.lastWater) blk.appendChild(note(`last added: ${this.lastWater.name} — ${this.lastWater.metadata.confidence}${this.lastWater.metadata.source ? ` (${this.lastWater.metadata.source.doc}${this.lastWater.metadata.source.page ? ', ' + this.lastWater.metadata.source.page : ''})` : ' (no source given)'}`, this.lastWater.metadata.confidence === 'assumed' ? 'note warn' : 'note ok'));
    return blk;
  }

  /** Step 3 on every mapped trace, then back to this panel. */
  async deriveFirst() {
    const st = this.app.tools && this.app.tools.structure;
    if (!st || typeof st.deriveAll !== 'function') { this.error = 'the Structural data tool is not available'; this.repanel(); return; }
    this.deriving = true; this.error = null; this.repanel();
    try { await st.deriveAll(); }
    catch (e) { this.error = 'derive failed: ' + (e && e.message || e); }
    finally { this.deriving = false; if (this.T.active === this) this.repanel(); }
  }

  /** contacts → dip offsets → base surfaces → unit volumes, through the
      worker, into group 'Stratigraphy' under one StratModel. */
  async build() {
    const P = this.app.project, topo = this.app.topoGrid();
    this.error = null;
    if (!P || !topo) { this.error = 'needs a topography grid'; this.repanel(); return null; }
    await this.loadAges();
    const units = this.units(), struct = structuralLayers(P), faults = faultLayers(P);
    if (units.filter(u => u.t0 != null || u.t1 != null).length < 2) { this.error = 'needs at least two mapped units with ages'; this.repanel(); return null; }
    const o = this.opts, tol = finite(o.tol) > 0 ? +o.tol : Math.max(topo.dx, topo.dy);
    this.busy = true; this.repanel();
    const status = (f, n) => this.app.status(`model from the map ${(f * 100) | 0}% ${n || ''}`);
    try {
      const lat = latticeOf(topo, o.nodes);
      status(0, 'contacts');
      const r = await this.app.engine.call('mapModelInputs', {
        topo: GM.packObject(topo),
        units: units.map(u => ({ id: u.id, name: u.name, t0: u.t0, t1: u.t1, color: u.color, lithology: u.lithology, description: u.description, outline: GM.packObject(u.outline) })),
        faults: faults.map(f => GM.packObject(f)), structural: struct.map(s => GM.packObject(s)),
        opts: { tol, radius: +o.radius, offset: +o.offset },
      }, (f, n) => status(0.4 * f, n || 'contacts'));
      const withBase = r.units.filter(u => u.base);
      const result = { stats: r.stats, warnings: r.warnings.slice(), units: r.units.map(u => ({ name: u.name, n_contacts: u.n_contacts, n_offsets: u.n_offsets, against: u.against, warnings: u.warnings })), built: null };
      if (!withBase.length) {
        this.result = result; this.error = 'nothing to build: ' + (r.warnings[r.warnings.length - 1] || 'no unit has enough contacts');
        toast(this.error, 'warn', 8000); return null;
      }
      const stamp = new Date().toISOString().slice(0, 16);
      status(0.4, 'base surfaces');
      const built = await this.app.engine.call('buildStratigraphy', { topo: GM.packObject(lat), units: r.units.map(u => ({ name: u.name, color: u.color, lithology: u.lithology, description: u.description, contact: 'deposit', base: u.base ? GM.packObject(u.base) : null })), method: o.method || 'rbf', name: MAPMODEL_NAME }, (f, n) => status(0.4 + 0.4 * f, n || 'base surfaces'));
      // one model, rebuilt in place: the previous outputs of THIS model go
      let sm = this.find();
      if (!sm) { sm = new GM.StratModel({ name: MAPMODEL_NAME, topography: topo.id, group: 'Stratigraphy' }); sm.metadata.map_model = true; P.add(sm); }
      for (const old of P.objects.filter(x => x.metadata && x.metadata.strat_of === sm.id)) P.remove(old);
      const inputIds = [topo.id, ...units.flatMap(u => u.layer_ids), ...struct.map(s => s.id)];
      const inputNames = [topo.name, ...units.map(u => u.outline.name), ...struct.map(s => s.name)];
      const stamp_prov = { method: E.MAPMODEL_METHOD, confidence: 'inferred', inputs: inputNames, tol_m: tol, radius_m: +o.radius, offset_m: +o.offset, lattice: `${lat.nx}×${lat.ny}`, interpolation: o.method || 'rbf', built_at: stamp, ages_from: Object.fromEntries(units.filter(u => u.age_from).map(u => [u.name, u.age_from])) };
      const grids = {}, contactLayers = [];
      r.units.forEach((u, i) => {
        if (!u.base) return;
        // the contact + offset points the surface went through, kept (hidden) so the fit can be inspected and rebuilt in step 6
        const ps = u.base; ps.name = `${u.name} — map contacts + dip offsets`; ps.role = 'contacts'; ps.group = 'Stratigraphy'; ps.color = u.color; ps.visible = false;
        ps.metadata.strat_of = sm.id; ps.metadata.built_at = stamp; ps.metadata.derived_from = u.derived_from.slice();
        ps.provenance = Object.assign({}, u.provenance, { method: E.MAPMODEL_METHOD, confidence: 'inferred' });
        for (const w of u.warnings) ps.warn(w);
        P.add(ps); contactLayers.push(ps);
        const g = built.bases[i]; if (!g) return;
        g.group = 'Stratigraphy'; g.name = `${u.name} base`; g.color = u.color; g.role = 'contact';
        g.metadata.strat_of = sm.id; g.metadata.built_at = stamp; g.metadata.derived_from = [ps.id, ...u.derived_from]; g.metadata.contact = 'deposit';
        g.metadata.inputs = { contacts: u.n_contacts, offsets: u.n_offsets, against: u.against.slice() };
        g.provenance = Object.assign({}, g.provenance, u.provenance, { method: E.MAPMODEL_METHOD, confidence: 'inferred', inputs: u.provenance.inputs, interpolation: o.method || 'rbf', lattice: `${lat.nx}×${lat.ny}` });
        for (const w of u.warnings) g.warn(w);
        P.add(g); grids[g.id] = g;
      });
      sm.units = r.units.map((u, i) => ({ name: u.name, color: u.color, lithology: u.lithology || '', description: u.description || '', contact: 'deposit', base: built.bases[i] ? built.bases[i].id : null, source: built.bases[i] ? { kind: 'points', id: u.base.id, value: '' } : { kind: 'none' }, map_unit: { id: u.id, t0: u.t0, t1: u.t1, contacts: u.n_contacts, offsets: u.n_offsets, against: u.against.slice() } }));
      sm.topography = topo.id; sm.metadata.built = { at: stamp, units: sm.units.length, lattice: `${lat.nx}×${lat.ny}` }; sm.metadata.map_model = true; sm.metadata.map_model_stats = r.stats;
      sm.metadata.derived_from = inputIds.slice(); sm.metadata.warnings = r.warnings.slice(); sm.provenance = stamp_prov;
      status(0.8, 'unit volumes');
      const vols = await this.app.engine.call('stratigraphyVolumes', { strat: GM.packObject(sm), grids: Object.fromEntries(Object.entries(grids).map(([k, g]) => [k, GM.packObject(g)])), topo: GM.packObject(built.topo || lat) });
      vols.forEach((m, i) => {
        m.group = 'Stratigraphy'; m.opacity = 0.9; m.metadata.strat_of = sm.id; m.metadata.built_at = stamp;
        const u = r.units[i] || {};
        m.metadata.derived_from = [topo.id, ...(i > 0 && sm.units[i - 1].base ? [sm.units[i - 1].base] : []), ...(sm.units[i] && sm.units[i].base ? [sm.units[i].base] : [])];
        m.provenance = { method: E.MAPMODEL_METHOD, confidence: 'inferred', inputs: (u.provenance && u.provenance.inputs) || inputNames, note: i === vols.length - 1 && !sm.units[i].base ? 'basement: the floor is 1 km below the lowest base, a drawing limit not a fact' : 'closed volume between the base above and this unit\'s base' };
        if (i === vols.length - 1 && sm.units[i] && !sm.units[i].base) m.visible = false;
        P.add(m);
      });
      result.built = { bases: Object.keys(grids).length, volumes: vols.length, lattice: `${lat.nx}×${lat.ny}`, at: stamp, contact_layers: contactLayers.length };
      this.result = result;
      this.app.refresh(sm); if (this.T.section && this.T.section.update) this.T.section.update();
      this.app.status('');
      toast(`rock from the map: ${result.built.bases} base surface${result.built.bases === 1 ? '' : 's'}, ${result.built.volumes} volumes — inferred; ${r.stats.dips_used ? `${r.stats.dips_used} dip offsets used` : 'no dip information'}${r.stats.rejected.no_contacts.length ? `; skipped ${r.stats.rejected.no_contacts.join(', ')}` : ''}`, r.stats.no_dip || r.stats.rejected.no_contacts.length ? 'warn' : 'ok', 8000);
      return sm;
    } catch (e) {
      console.error(e); this.error = 'build failed: ' + (e && e.message || e); toast(this.error, 'err', 9000); return null;
    } finally { this.busy = false; this.app.status(''); this.repanel(); }
  }

  /** A horizontal plane at the stated level, clipped to the model box. */
  addWater() {
    const P = this.app.project, topo = this.app.topoGrid(); this.waterError = null;
    if (!P || !topo) { this.waterError = 'needs a topography grid (the plane spans the model box)'; this.repanel(); return null; }
    const w = this.water, v = finite(w.value);
    if (v == null) { this.waterError = 'type the water level first: an elevation, or a depth below the collar'; this.repanel(); return null; }
    let z, statedAs, collar = null;
    if (w.mode === 'collar') {
      collar = this.collar();
      if (!collar) { this.waterError = 'no collar elevation: the project has no site point on the topography — give the level as an elevation instead'; this.repanel(); return null; }
      z = collar.z - v; statedAs = `${fmtNum(v, 1)} m below the collar (collar ${fmtNum(collar.z, 1)} m, ${collar.from})`;
    } else { z = v; statedAs = `${fmtNum(v, 1)} m elevation`; }
    const source = w.source && w.source.trim() ? { doc: w.source.trim(), page: w.page && w.page.trim() ? w.page.trim() : null } : null;
    const confidence = source ? 'described' : 'assumed';
    const b = topo.bounds(); const x0 = b[0], y0 = b[1], x1 = b[3], y1 = b[4];
    const m = new GM.Mesh({ name: `Water level ${Math.round(z)} m (${source ? 'stated' : 'assumed'})`, vertices: [x0, y0, z, x1, y0, z, x1, y1, z, x0, y1, z], triangles: [0, 1, 2, 0, 2, 3], role: 'water', color: WATER_COLOR.slice(), group: 'Surfaces', opacity: 0.35 });
    m.metadata = { confidence, note: WATER_NOTE, elevation_m: z, stated_as: statedAs, source, collar_z: collar ? collar.z : null, derived_from: [topo.id], clipped_to: 'model box' };
    m.provenance = { method: 'horizontal plane at a stated water level', confidence, source: source ? `${source.doc}${source.page ? ', ' + source.page : ''}` : 'not stated — assumed', note: WATER_NOTE, box: [x0, y0, x1, y1] };
    if (!source) m.warn('no source given for this water level — it is an assumption, drawn as one');
    P.add(m); this.lastWater = m;
    toast(`${m.name}: ${confidence}${source ? ` — ${source.doc}` : ' (no source: assumed)'}`, source ? 'ok' : 'warn', 6000);
    this.repanel();
    return m;
  }
}

/* ============================================================= install */
/** Register the tool (idempotent: a page that imports this module after
    gm-viewer.js has already installed it gets the same instance). */
export function installMapModelTools(tools) {
  if (!tools || typeof tools.register !== 'function') return null;
  if (tools.all && tools.all.mapmodel) return { mapmodel: tools.all.mapmodel };
  const mapmodel = new MapModelTool(tools);
  tools.register('mapmodel', mapmodel, { label: 'Model the rock from the map', title: 'Model the rock from the map', hint: 'contacts + derived dips → pancake' });
  tools.mapmodel = mapmodel;
  return { mapmodel };
}
if (typeof window !== 'undefined' && window.gmApp && window.gmApp.tools) {
  try { installMapModelTools(window.gmApp.tools); } catch (e) { console.warn('gm-map-model: ' + (e && e.message)); }
}
