/* gm-site.js — build a model project in the browser around a map point: the
   JS twin of pipelines/geomodel/kit.py (build_site_model).  Terrain comes from
   the same AWS terrarium tiles the map's 3-D mode streams; imagery (satellite /
   USGS topo / Macrostrat geology) is stitched into a texture draped on the
   topography; AOI bundles (geology units, faults, targets, claims) and the
   national graded-mines table come from site/data; tiled layers the map had
   loaded — USMIN mine features, MRDS, claims — and the USGS geology it had
   decoded in the viewport arrive through the IndexedDB hand-off the OPEN 3D
   MODEL button writes, so a site outside every AOI bundle still gets its
   rock and its faults. */
import * as GM from './gm-core.js';
import * as E from './gm-engine.js';

export const TERRAIN_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png';
export const IMAGERY = {
  sat: { name: 'Satellite (Esri World Imagery)', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', max: 18 },
  topo: { name: 'USGS Topo', url: 'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}', max: 16 },
  geology: { name: 'Geology (Macrostrat)', url: 'https://tiles.macrostrat.org/carto/{z}/{x}/{y}.png', max: 14 },
  none: { name: 'None (elevation colours)', url: null },
};

function mercXY(lon, lat, z) { const n = 2 ** z, s = Math.sin(Math.max(-85.051, Math.min(85.051, lat)) * Math.PI / 180); return [(lon + 180) / 360 * n, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n]; }

async function fetchImage(url, timeoutMs = 20000) {
  const opts = { mode: 'cors' }; if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) opts.signal = AbortSignal.timeout(timeoutMs);
  const r = await fetch(url, opts); if (!r.ok) throw new Error(`${r.status} ${url}`);
  return createImageBitmap(await r.blob());
}

/** Terrarium mosaic sampler (bilinear) at one zoom — same math as the map's exporter. */
export class TerrainSampler {
  constructor(zoom) { this.z = zoom; this.n = 2 ** zoom; this.tiles = new Map(); this.canvas = document.createElement('canvas'); this.canvas.width = this.canvas.height = 256; this.ctx = this.canvas.getContext('2d', { willReadFrequently: true }); this.failed = 0; }
  async fetchTile(tx, ty) {
    const key = tx + '_' + ty; if (this.tiles.has(key)) return;
    if (tx < 0 || ty < 0 || tx >= this.n || ty >= this.n) { this.tiles.set(key, null); return; }
    try { const bm = await fetchImage(TERRAIN_URL.replace('{z}', this.z).replace('{x}', tx).replace('{y}', ty)); this.ctx.clearRect(0, 0, 256, 256); this.ctx.drawImage(bm, 0, 0); this.tiles.set(key, this.ctx.getImageData(0, 0, 256, 256).data); }
    catch (e) { this.failed++; this.tiles.set(key, null); }
  }
  async prefetch(w, s, e, n, onprog) {
    const [x0, y0] = mercXY(w, n, this.z), [x1, y1] = mercXY(e, s, this.z); const jobs = [];
    for (let ty = Math.floor(y0) - 1; ty <= Math.floor(y1) + 1; ty++) for (let tx = Math.floor(x0) - 1; tx <= Math.floor(x1) + 1; tx++) jobs.push([tx, ty]);
    for (let i = 0; i < jobs.length; i += 8) { await Promise.all(jobs.slice(i, i + 8).map(([tx, ty]) => this.fetchTile(tx, ty))); if (onprog) onprog(Math.min(jobs.length, i + 8), jobs.length); }
    return [...this.tiles.values()].some(v => v !== null);
  }
  px(gx, gy) { const tx = Math.floor(gx / 256), ty = Math.floor(gy / 256), t = this.tiles.get(tx + '_' + ty); if (!t) return null; const o = ((Math.floor(gy) - ty * 256) * 256 + (Math.floor(gx) - tx * 256)) * 4; return t[o] * 256 + t[o + 1] + t[o + 2] / 256 - 32768; }
  sample(lon, lat) {
    const [mx, my] = mercXY(lon, lat, this.z); const gx = mx * 256 - 0.5, gy = my * 256 - 0.5; const x0 = Math.floor(gx), y0 = Math.floor(gy), fx = gx - x0, fy = gy - y0;
    const v = [this.px(x0, y0), this.px(x0 + 1, y0), this.px(x0, y0 + 1), this.px(x0 + 1, y0 + 1)];
    if (v.some(x => x == null)) { const g = v.filter(x => x != null); return g.length ? g[0] : NaN; }
    return v[0] * (1 - fx) * (1 - fy) + v[1] * fx * (1 - fy) + v[2] * (1 - fx) * fy + v[3] * fx * fy;
  }
}

/** Build a topography Grid2D (UTM lattice) around lon/lat. */
export async function buildTopography(lon, lat, radius, crs, opts = {}) {
  const zone = crs.zone, north = crs.north; const [cx, cy] = GM.utm.fwd(lon, lat, zone, north);
  const R = radius; let zoom = opts.zoom || 13;
  // cap tiles to ~150 at the chosen zoom
  for (; zoom > 8; zoom--) { const degW = (2 * R / 111320 / Math.cos(lat * Math.PI / 180)), degH = 2 * R / 110540; const tx = Math.ceil(degW / (360 / 2 ** zoom)) + 2, ty = Math.ceil(degH / (360 / 2 ** zoom) / Math.cos(lat * Math.PI / 180)) + 2; if (tx * ty <= 150) break; }
  const terr = new TerrainSampler(zoom);
  const [wlon, slat] = GM.utm.inv(cx - R, cy - R, zone, north), [elon, nlat] = GM.utm.inv(cx + R, cy + R, zone, north);
  const have = await terr.prefetch(wlon - 0.002, slat - 0.002, elon + 0.002, nlat + 0.002, opts.onprog);
  const native = 156543.03 * Math.cos(lat * Math.PI / 180) / 2 ** zoom;
  let cell = opts.cell || Math.max(Math.round(native), 5); while ((2 * R / cell) ** 2 > 6e5) cell *= 2;
  const nx = Math.ceil(2 * R / cell) + 1, ny = nx;
  const g = new GM.Grid2D({ nx, ny, x0: cx - R, y0: cy - R, dx: cell, dy: cell, name: 'Topography', role: 'topography', color: [150, 150, 150] });
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) { const [x, y] = g.nodeXY(i, j); const [lo, la] = GM.utm.inv(x, y, zone, north); g.values[j * nx + i] = have ? terr.sample(lo, la) : NaN; }
  g.provenance = { source: 'AWS Terrain Tiles (Mapzen terrarium: 3DEP/SRTM composite)', zoom, cell_m: cell, reachable: have, tiles_failed: terr.failed };
  if (!have) g.warn('terrain tiles unreachable: topography has no data');
  return { grid: g, sampler: terr, zoom, cell, have };
}

/** Stitch basemap tiles covering the grid's bbox into a canvas (<= maxPx). */
export async function buildImagery(grid, crs, kind = 'sat', opts = {}) {
  const src = IMAGERY[kind]; if (!src || !src.url) return null;
  const b = grid.bounds(); const corners = [[b[0], b[1]], [b[3], b[1]], [b[3], b[4]], [b[0], b[4]]].map(([x, y]) => GM.utm.inv(x, y, crs.zone, crs.north));
  const w = Math.min(...corners.map(c => c[0])), e = Math.max(...corners.map(c => c[0])), s = Math.min(...corners.map(c => c[1])), n = Math.max(...corners.map(c => c[1]));
  const maxPx = opts.maxPx || 2048; const lat = (s + n) / 2;
  let z = Math.min(src.max, 18);
  for (; z > 8; z--) { const [x0] = mercXY(w, n, z), [x1] = mercXY(e, s, z); if ((x1 - x0) * 256 <= maxPx) break; }
  const [x0, y0] = mercXY(w, n, z), [x1, y1] = mercXY(e, s, z);
  const tx0 = Math.floor(x0), ty0 = Math.floor(y0), tx1 = Math.floor(x1), ty1 = Math.floor(y1);
  const cw = (tx1 - tx0 + 1) * 256, ch = (ty1 - ty0 + 1) * 256;
  const canvas = document.createElement('canvas'); canvas.width = cw; canvas.height = ch; const ctx = canvas.getContext('2d'); ctx.fillStyle = '#2a3038'; ctx.fillRect(0, 0, cw, ch);
  const jobs = []; let ok = 0;
  for (let ty = ty0; ty <= ty1; ty++) for (let tx = tx0; tx <= tx1; tx++) jobs.push([tx, ty]);
  for (let i = 0; i < jobs.length; i += 8) await Promise.all(jobs.slice(i, i + 8).map(async ([tx, ty]) => { try { const bm = await fetchImage(src.url.replace('{z}', z).replace('{x}', tx).replace('{y}', ty)); ctx.drawImage(bm, (tx - tx0) * 256, (ty - ty0) * 256); ok++; } catch (e) { /* leave grey */ } }));
  if (!ok) return null;
  // Re-project: the texture is applied with planar UVs over the UTM bbox. Resample the mercator
  // mosaic onto a UTM-aligned canvas so north is up and the bbox maps exactly.
  const out = document.createElement('canvas'); const ow = Math.min(maxPx, Math.round(cw)), oh = Math.min(maxPx, Math.round(ch)); out.width = ow; out.height = oh; const octx = out.getContext('2d');
  const srcData = ctx.getImageData(0, 0, cw, ch).data, dst = octx.createImageData(ow, oh), dd = dst.data;
  for (let py = 0; py < oh; py++) {
    const Y = b[4] - (py + 0.5) / oh * (b[4] - b[1]);
    for (let px = 0; px < ow; px++) {
      const X = b[0] + (px + 0.5) / ow * (b[3] - b[0]); const [lo, la] = GM.utm.inv(X, Y, crs.zone, crs.north); const [mx, my] = mercXY(lo, la, z);
      const sx = Math.min(cw - 1, Math.max(0, Math.round((mx - tx0) * 256))), sy = Math.min(ch - 1, Math.max(0, Math.round((my - ty0) * 256)));
      const si = (sy * cw + sx) * 4, di = (py * ow + px) * 4; dd[di] = srcData[si]; dd[di + 1] = srcData[si + 1]; dd[di + 2] = srcData[si + 2]; dd[di + 3] = 255;
    }
  }
  octx.putImageData(dst, 0, 0);
  return { canvas: out, zoom: z, tiles: ok, source: src.name };
}

async function jget(u) { const r = await fetch(u); if (!r.ok) throw new Error(`${u} ${r.status}`); return r.json(); }

export async function aoiForPoint(lon, lat, base = '') {
  try { const cfg = await jget(base + 'data/aoi.json'); let best = null; for (const [k, a] of Object.entries(cfg.aois || {})) { const b = a.bbox; if (!b || !(b[0] <= lon && lon <= b[2] && b[1] <= lat && lat <= b[3])) continue; const area = (b[2] - b[0]) * (b[3] - b[1]); if (!best || area < best[0]) best = [area, k]; } return best ? best[1] : null; }
  catch (e) { /* no aoi config shipped with the site: probe the known bundles */ }
  for (const k of ['cassia', 'clearlake', 'delamar24k']) { try { const r = await fetch(`${base}data/targets/${k}.json`, { method: 'HEAD' }); if (r.ok) { const t = await jget(`${base}data/targets/${k}.json`); const bb = t.bbox || null; if (!bb) return k; if (bb[0] <= lon && lon <= bb[2] && bb[1] <= lat && lat <= bb[3]) return k; } } catch (e) { /* skip */ } }
  return null;
}

/* Geologic time in Ma, [older, younger] — ICS boundaries rounded the way map
   legends round them.  Used only for a hand-off unit whose age arrives as
   legend text without numeric age_min / age_max: reading "Cretaceous" as
   145–66 is transcription, not invention, and anything the table cannot read
   stays null and is warned about rather than defaulted (GEOMODEL.md §4).
   Sub-epochs precede their parents so the longest-name-first regex below
   never reads "Neoproterozoic" as "Proterozoic". */
export const GEO_TIME = [
  ['holocene', 0.0117, 0], ['pleistocene', 2.58, 0.0117], ['quaternary', 2.58, 0],
  ['pliocene', 5.33, 2.58], ['miocene', 23, 5.33], ['neogene', 23, 2.58],
  ['oligocene', 33.9, 23], ['eocene', 56, 33.9], ['paleocene', 66, 56], ['palaeocene', 66, 56], ['paleogene', 66, 23], ['palaeogene', 66, 23],
  ['tertiary', 66, 2.58], ['cenozoic', 66, 0],
  ['cretaceous', 145, 66], ['jurassic', 201, 145], ['triassic', 252, 201], ['mesozoic', 252, 66],
  ['permian', 299, 252], ['pennsylvanian', 323, 299], ['mississippian', 359, 323], ['carboniferous', 359, 299],
  ['devonian', 419, 359], ['silurian', 444, 419], ['ordovician', 485, 444], ['cambrian', 539, 485],
  ['paleozoic', 539, 252], ['palaeozoic', 539, 252], ['phanerozoic', 539, 0],
  ['neoproterozoic', 1000, 539], ['mesoproterozoic', 1600, 1000], ['paleoproterozoic', 2500, 1600], ['palaeoproterozoic', 2500, 1600],
  ['proterozoic', 2500, 539], ['archean', 4000, 2500], ['archaean', 4000, 2500], ['hadean', 4600, 4000], ['precambrian', 4600, 539],
];
const GEO_NAMES = GEO_TIME.map(t => t[0]).sort((a, b) => b.length - a.length);
const AGE_RE = new RegExp(`\\b(?:(early|lower|middle|mid|late|upper)\\b[\\s-]*(?:(?:to|and|or|[-–])\\s*)?(?:(early|lower|middle|mid|late|upper)\\b[\\s-]*)?)?(${GEO_NAMES.join('|')})\\b`, 'gi');
const MA_RANGE_RE = /(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:ma|m\.?y\.?a?|million)\b/i;
const MA_ONE_RE = /(?:^|[^\d.])(\d+(?:\.\d+)?)\s*(?:ma|m\.?y\.?a?)\b/i;
function halfOf(t0, t1, mod) {
  const mid = (t0 + t1) / 2, q = (t0 - t1) / 4;
  switch (String(mod || '').toLowerCase()) {
    case 'early': case 'lower': return [t0, mid];
    case 'late': case 'upper': return [mid, t1];
    case 'middle': case 'mid': return [t0 - q, t1 + q];
    default: return [t0, t1];
  }
}
/** Read a legend age ("Cretaceous", "Late Jurassic to Early Cretaceous",
    "Miocene (17-14 Ma)") into { t0, t1, how } in Ma, or null when nothing in
    the text is a known period or a stated Ma range.  A compound spans every
    period named; a numeric range in the text wins over the period name. */
export function parseAgeText(text) {
  const s = String(text == null ? '' : text).trim(); if (!s) return null;
  const mr = s.match(MA_RANGE_RE); if (mr) { const a = +mr[1], b = +mr[2]; return { t0: Math.max(a, b), t1: Math.min(a, b), how: 'stated Ma range' }; }
  const m1 = s.match(MA_ONE_RE); if (m1) { const a = +m1[1]; return { t0: a, t1: a, how: 'stated age' }; }
  let t0 = -Infinity, t1 = Infinity, hits = 0; AGE_RE.lastIndex = 0; let m;
  while ((m = AGE_RE.exec(s))) {
    const row = GEO_TIME.find(t => t[0] === m[3].toLowerCase()); if (!row) continue;
    // "Early to Middle Jurassic": the union of the named sub-ranges
    const parts = [m[1], m[2]].filter(Boolean).map(mod => halfOf(row[1], row[2], mod));
    const lo = parts.length ? Math.max(...parts.map(q => q[0])) : row[1], hi = parts.length ? Math.min(...parts.map(q => q[1])) : row[2];
    t0 = Math.max(t0, lo); t1 = Math.min(t1, hi); hits++;
  }
  return hits ? { t0, t1, how: 'geologic-time table' } : null;
}
export function hexRGB(s) { const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(s || '').trim()); return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null; }
const isPolys = g => Array.isArray(g) && g.length && g.every(poly => Array.isArray(poly) && poly.length && Array.isArray(poly[0]) && poly[0].length >= 3 && Array.isArray(poly[0][0]));

/** The map's hand-off geology (index.html open3D writes h.geology = { units,
    faults, sources, note } decoded from the national USGS PMTiles in the
    viewport) in the shape geologyObjects() takes — the same shape the AOI
    bundles have, so draped units, outlines and faults are identical in kind.
    Returns { geo, label, unparsed: [{ id, nm, age }], sources }. */
export function geologyFromHandoff(hg) {
  const units = [], faults = [], unparsed = [];
  (hg.units || []).forEach((u, i) => {
    if (!u || !isPolys(u.polys)) return;
    const id = u.id != null ? u.id : `handoff-unit-${i}`, nm = u.nm || u.label || 'unit';
    let t0 = GM.isNum(+u.age_max) && u.age_max !== null && u.age_max !== '' ? +u.age_max : null, t1 = GM.isNum(+u.age_min) && u.age_min !== null && u.age_min !== '' ? +u.age_min : null;
    let age_how = t0 != null && t1 != null ? 'age_min / age_max from the map data' : null;
    if (t0 == null || t1 == null) {
      const r = parseAgeText(u.age);
      if (r) { t0 = r.t0; t1 = r.t1; age_how = r.how; }
      else { t0 = t1 = null; unparsed.push({ id, nm, age: u.age == null || u.age === '' ? null : String(u.age) }); }
    }
    const rgb = hexRGB(u.color);
    units.push({ id, nm, label: u.label, li: u.li, age: u.age, de: u.de, src: u.src, url: u.url, scale: u.scale, t0, t1, age_how, rgb: rgb || undefined, g: u.polys });
  });
  (hg.faults || []).forEach((f, i) => {
    if (!f || !Array.isArray(f.path) || f.path.length < 2 || !Array.isArray(f.path[0])) return;
    faults.push({ id: f.id != null ? f.id : `handoff-fault-${i}`, nm: f.nm, ty: f.ty, age: f.age, slip: f.slip, src: f.src, path: f.path });
  });
  const named = (hg.sources || []).map(s => typeof s === 'string' ? s : (s && (s.name || s.ref || s.id || s.src)) || '').map(s => String(s).trim()).filter(Boolean);
  const sources = named.length ? [...new Set(named)] : [...new Set(units.map(u => u.src).concat(faults.map(f => f.src)).filter(Boolean).map(String))];
  const label = 'USGS geology via map hand-off' + (sources.length ? ` (${sources.join(', ')})` : '');
  return { geo: { units, faults }, label, unparsed, sources };
}

/** The pieces of a polyline inside a rectangle (Liang–Barsky per segment,
    consecutive inside segments chained).  A mapped fault is a long sparse
    trace: it seldom has a vertex inside a 3 km box, and the part of it beyond
    the terrain grid must not be draped onto z = 0. */
export function clipPolylineRect(path, minx, miny, maxx, maxy) {
  const out = []; let cur = null;
  for (let i = 0; i + 1 < path.length; i++) {
    const a = path[i], b = path[i + 1]; const dx = b[0] - a[0], dy = b[1] - a[1];
    let t0 = 0, t1 = 1, ok = true; const p = [-dx, dx, -dy, dy], q = [a[0] - minx, maxx - a[0], a[1] - miny, maxy - a[1]];
    for (let k = 0; k < 4 && ok; k++) {
      if (p[k] === 0) { if (q[k] < 0) ok = false; continue; }
      const r = q[k] / p[k];
      if (p[k] < 0) { if (r > t1) ok = false; else if (r > t0) t0 = r; } else { if (r < t0) ok = false; else if (r < t1) t1 = r; }
    }
    if (!ok) { cur = null; continue; }
    const A = [a[0] + dx * t0, a[1] + dy * t0], B = [a[0] + dx * t1, a[1] + dy * t1];
    if (cur && t0 === 0) cur.push(B); else { cur = [A, B]; out.push(cur); }
    if (t1 < 1) cur = null;
  }
  return out.filter(piece => piece.some((v, i) => i && Math.hypot(v[0] - piece[i - 1][0], v[1] - piece[i - 1][1]) > 1e-6));
}

/** Draped geology + faults from an AOI bundle (or the same shape from the
    map hand-off: opts.bundle names the source in every provenance).  The
    returned array carries `.stats` — triangles drawn, units draped, units
    left as outlines once the triangle budget bit — so the caller can say so. */
export function geologyObjects(geo, crs, topo, box, opts = {}) {
  const out = []; const fwd = (lon, lat) => GM.utm.fwd(lon, lat, crs.zone, crs.north); const [minx, miny, maxx, maxy] = box;
  const cell = topo.dx; const elev = (x, y, lift = 0) => { const v = topo.sample(x, y); return (v === v ? v : 0) + lift; };
  let nTri = 0; const budget = opts.maxTriangles || 150000; const maxEdge = Math.max(cell * 2, (maxx - minx) / 60);
  const bundle = opts.bundle || `data/geology/${opts.aoi}.json`; const extra = opts.provenance || {};
  const defined = o => { const r = {}; for (const [k, v] of Object.entries(o)) if (v !== undefined) r[k] = v; return r; };
  const stats = { triangles: 0, units: 0, units_over_budget: 0, faults: 0, budget };
  for (const u of (geo.units || []).filter(u => u.g)) {
    const polys = [];
    for (const poly of u.g) { const outer = poly[0].map(([x, y]) => fwd(x, y)); const xs = outer.map(p => p[0]), ys = outer.map(p => p[1]); if (Math.max(...xs) < minx || Math.min(...xs) > maxx || Math.max(...ys) < miny || Math.min(...ys) > maxy) continue; const c = E.clipRingRect(outer, minx, miny, maxx, maxy); if (c.length >= 3) polys.push(c); }
    if (!polys.length) continue;
    const verts2d = [], tris = [];
    for (let ring of polys) { if (Math.abs(E.signedArea(ring)) < 1) continue; if (E.signedArea(ring) < 0) ring = ring.slice().reverse(); const base = verts2d.length; verts2d.push(...ring); for (const [a, b, c] of E.earClip(ring)) tris.push([base + a, base + b, base + c]); }
    if (tris.length && nTri < budget) {
      const { points: pts, triangles: t2 } = E.subdivideTriangles(verts2d, tris, maxEdge);
      const v = new Float64Array(pts.length * 3); pts.forEach((p, i) => { v[3 * i] = p[0]; v[3 * i + 1] = p[1]; v[3 * i + 2] = elev(p[0], p[1], 1.5); });
      const tflat = new Uint32Array(t2.length * 3); t2.forEach((t, i) => { tflat[3 * i] = t[0]; tflat[3 * i + 1] = t[1]; tflat[3 * i + 2] = t[2]; });
      // a unit that came with its map colour keeps it (the hand-off carries the
      // PMTiles fill); otherwise the stable hash colour every bundle unit gets
      const m = new GM.Mesh({ vertices: v, triangles: tflat, name: u.nm || u.id || 'unit', color: Array.isArray(u.rgb) && u.rgb.length === 3 ? u.rgb : E.unitColor(u), role: 'geology', group: 'Geology (draped)', opacity: 0.85 });
      Object.assign(m.metadata, { unit_id: u.id, age: u.age, lithology: u.li, description: u.de, source: u.src, t0_ma: u.t0 == null ? null : u.t0, t1_ma: u.t1 == null ? null : u.t1 }, defined({ age_basis: u.age_how, source_url: u.url, source_scale: u.scale, unit_label: u.label && u.label !== u.nm ? u.label : undefined }));
      m.provenance = Object.assign({ bundle, unit: u.id, drape: `terrain +1.5 m, edges <= ${maxEdge.toFixed(0)} m` }, extra);
      out.push(m); nTri += t2.length; stats.units++;
    } else if (tris.length) stats.units_over_budget++;
    const ol = new GM.LineSet({ name: (u.nm || 'unit') + ' outline', role: 'geology-outline', color: [40, 40, 40], group: 'Geology outlines' });
    for (const ring of polys) { const dense = E.densify(ring.concat([ring[0]]), cell); ol.addPolyline(dense.map(([x, y]) => [x, y, elev(x, y, 2)]), { unit: u.nm, unit_id: u.id, age: u.age, t0: u.t0 != null && u.t0 === u.t0 ? +u.t0 : undefined, t1: u.t1 != null && u.t1 === u.t1 ? +u.t1 : undefined }); }
    ol.provenance = Object.assign({ bundle }, extra); out.push(ol);
  }
  const fl = new GM.LineSet({ name: 'Faults (mapped)', role: 'faults', color: [212, 165, 63], group: 'Structure' });
  for (const f of (geo.faults || []).filter(f => Array.isArray(f.path) && f.path.length >= 2)) {
    const path = f.path.map(([x, y]) => fwd(x, y)); const feature = Object.assign({ name: f.nm, type: f.ty, source: f.src }, defined({ age: f.age, slip_sense: f.slip, fault_id: f.id }));
    // a trace that crosses the box more than once becomes one part per crossing
    for (const piece of clipPolylineRect(path, minx, miny, maxx, maxy)) { const dense = E.densify(piece, cell); fl.addPolyline(dense.map(([x, y]) => [x, y, elev(x, y, 3)]), feature); }
  }
  if (fl.parts.length) { fl.provenance = Object.assign({ bundle, drape: 'terrain +3 m' }, extra); out.push(fl); stats.faults = fl.parts.length; }
  stats.triangles = nTri; out.stats = stats;
  return out;
}

export function minesFromGrades(gr, crs, topo, box, siteIndex = null) {
  const ps = new GM.PointSet({ name: 'Mines (cited grades)', role: 'mines', color: [201, 133, 0], group: 'Mines' });
  const cols = ['name', 'st', 'cnty', 'dist', 'au', 'ag', 'pb', 'zn', 'cu', 'sb', 'wo3', 'hgf', 'usd', 'ton', 'yd3', 'plc', 'open', 'com', 'src', 'url', 'basis', 'yrs', 'quote'].filter(c => gr[c]);
  const [minx, miny, maxx, maxy] = box;
  for (let i = 0; i < gr.n; i++) { const x = gr.x[i], y = gr.y[i]; if (x == null || y == null) continue; const [e, n] = GM.utm.fwd(x, y, crs.zone, crs.north); if (e < minx || e > maxx || n < miny || n > maxy) continue; const attrs = {}; for (const c of cols) attrs[c] = gr[c][i]; attrs.grade_index = i; attrs.is_site = siteIndex != null && i === +siteIndex ? 1 : 0; const z = topo.sample(e, n); ps.add(e, n, z === z ? z : 0, attrs); }
  ps.provenance = { bundle: 'data/grades/grades.json', note: 'best cited grade per mine; units per column' };
  return ps;
}

/* USMIN rows keep these columns even when a row lacks one, so the inspector
   table, the pick card and the glyph builder always find them. */
export const USMIN_COLUMNS = ['typ', 'az', 'nm', 'quad', 'yr', 'scale', 'st'];
export const USMIN_HOWTO = 'Symbols by feature type: square = shaft · triangle = adit / tunnel (the apex points along the mapped azimuth, into the hill, when the map gave one) · circle = prospect or pit · diamond = open pit / quarry / strip mine · hexagon = dump / tailings · inverted triangle = placer / gravel · cross = mill / smelter / plant · ring = anything else. Each symbol is where a historical topographic map showed the feature; it says nothing about depth, length or whether it is open. Never enter adits or shafts.';

export function pointsFromHandoff(h, crs, topo, box) {
  const out = []; const [minx, miny, maxx, maxy] = box; const colors = { mrds: [110, 140, 190], usmin: [140, 120, 170], stategeo: [90, 160, 120], ardf: [160, 130, 90], claimsA: [255, 80, 80], claimsC: [120, 120, 160] };
  const names = { mrds: 'USGS MRDS occurrences', usmin: 'Mine features (USMIN topo maps)', stategeo: 'State-survey mine records', ardf: 'Alaska ARDF occurrences', claimsA: 'Claims active (BLM centroids)', claimsC: 'Claims closed (BLM centroids)' };
  const roles = { usmin: 'features' };
  for (const [layer, feats] of Object.entries(h.layers || {})) {
    if (!Array.isArray(feats)) continue;
    const ps = new GM.PointSet({ name: names[layer] || layer, role: layer.startsWith('claims') ? 'claims' : (roles[layer] || 'points'), color: colors[layer] || [150, 150, 150], group: layer.startsWith('claims') ? 'Claims' : 'Mines' });
    if (layer === 'usmin') for (const c of USMIN_COLUMNS) ps.attributes[c] = [];
    for (const f of feats) { if (!f || !GM.isNum(+f.x) || !GM.isNum(+f.y)) continue; const [e, n] = GM.utm.fwd(+f.x, +f.y, crs.zone, crs.north); if (e < minx || e > maxx || n < miny || n > maxy) continue; const z = topo.sample(e, n); const attrs = Object.assign({}, f.p || {}); if (layer === 'claimsA') attrs.status = 'ACTIVE'; if (layer === 'claimsC') attrs.status = 'CLOSED'; ps.add(e, n, z === z ? z : 0, attrs); }
    if (!ps.n) continue;
    ps.provenance = { source: 'map hand-off (viewport snapshot of tiled layer ' + layer + ')', note: 'viewport snapshot, not an archive' };
    if (layer === 'usmin') {
      ps.provenance = { source: 'USGS USMIN: features digitised from historical topographic maps — surface locations only, no depth or extent', handoff: 'map hand-off (viewport snapshot of tiled layer usmin)', note: 'viewport snapshot, not an archive' };
      ps.metadata.howto = USMIN_HOWTO;
      ps.metadata.columns = { typ: 'feature type as the map symbol was classified', az: 'adit azimuth in degrees from the map symbol (absent when the map gave none)', nm: 'name printed on the map', quad: 'topographic quadrangle', yr: 'map year', scale: 'map scale denominator', st: 'state' };
    }
    out.push(ps);
  }
  return out;
}

/** Build the whole project for a site. opts: {radius, name, gi, aoi, zoom, imagery, onprog, base} */
export async function buildSiteProject(lon, lat, opts = {}) {
  const log = opts.onprog || (() => { });
  const radius = opts.radius || 2500; const { zone, north } = GM.utm.zone(lon, lat); const crs = GM.utm.crs(zone, north);
  const [cx, cy] = GM.utm.fwd(lon, lat, zone, north); const box = [cx - radius, cy - radius, cx + radius, cy + radius];
  const name = opts.name || `site ${lat.toFixed(4)} ${lon.toFixed(4)}`;
  const proj = new GM.Project({ name, crs, origin: [Math.round(cx / 100) * 100, Math.round(cy / 100) * 100, 0], site: { name, lon, lat, radius_m: radius, aoi: opts.aoi || null, grade_index: opts.gi == null ? null : +opts.gi, utm_zone: `${zone}${north ? 'N' : 'S'}`, key: GM.slug(name) + '-' + lat.toFixed(3) + '_' + lon.toFixed(3) } });
  proj.metadata.generator = 'model3d.html (browser)';
  proj.metadata.notes = [`Coordinates: WGS84 / UTM zone ${zone}${north ? 'N' : 'S'} (EPSG:${crs.epsg}), metres, Z = elevation.`, 'Research context only: claim points are BLM centroids, grades are cited historic figures, geology is map-scale.', 'Never enter adits or shafts.'];
  log('fetching terrain tiles…');
  const topoRes = await buildTopography(lon, lat, radius, crs, { zoom: opts.zoom || 13, onprog: (d, t) => log(`terrain tiles ${d}/${t}`) });
  const topo = topoRes.grid; proj.add(topo);
  proj.metadata.topography = { zoom: topoRes.zoom, cell: topoRes.cell, reachable: topoRes.have };
  const base = opts.base || '';
  let aoi = opts.aoi; if (!aoi || aoi === 'auto') { aoi = await aoiForPoint(lon, lat, base); proj.site.aoi = aoi; }
  const warn = msg => { proj.metadata.warnings = (proj.metadata.warnings || []).concat([msg]); };
  const budgetNote = (stats, what) => { if (stats && stats.units_over_budget) warn(`geology triangle budget (${stats.budget.toLocaleString()}) reached in the ${what}: ${stats.units_over_budget} unit(s) drawn as outlines only — a smaller radius (r=) drapes them`); };
  let bundleGeo = 0;   // geology objects the AOI bundle produced inside the box
  if (aoi) {
    log(`loading ${aoi} geology…`);
    try { const geo = await jget(`${base}data/geology/${aoi}.json`); const objs = geologyObjects(geo, crs, topo, box, { aoi }); for (const o of objs) proj.add(o); bundleGeo = objs.length; budgetNote(objs.stats, `${aoi} bundle`); if (bundleGeo) proj.metadata.geology_source = { kind: 'bundle', bundle: `data/geology/${aoi}.json`, units: objs.stats.units, faults: objs.stats.faults }; } catch (e) { warn(`geology bundle unavailable: ${e.message}`); }
    try { const tj = await jget(`${base}data/targets/${aoi}.json`); const ts = new GM.PointSet({ name: 'Geology targets (scored)', role: 'targets', color: [45, 212, 191], group: 'Mines' }); for (const t of tj.targets || []) { if (t.cx == null) continue; const [e, n] = GM.utm.fwd(t.cx, t.cy, zone, north); if (e < box[0] || e > box[2] || n < box[1] || n > box[3]) continue; const z = topo.sample(e, n); ts.add(e, n, z === z ? z : 0, { tier: t.tier, score: t.score, unit: t.nm, age: t.age, area_km2: t.area_km2, money: t.money ? 1 : 0, tier_name: t.tierName }); } if (ts.n) { ts.provenance = { bundle: `data/targets/${aoi}.json` }; proj.add(ts); } } catch (e) { /* optional */ }
    try { const cl = await jget(`${base}data/openground/${aoi}_claims.json`); for (const [status, color] of [['active', [255, 80, 80]], ['closed', [120, 120, 160]]]) { const cs = new GM.PointSet({ name: `Claims ${status} (BLM centroids)`, role: 'claims', color, group: 'Claims' }); for (const c of cl[status] || []) { if (c.x == null) continue; const [e, n] = GM.utm.fwd(c.x, c.y, zone, north); if (e < box[0] || e > box[2] || n < box[1] || n > box[3]) continue; const z = topo.sample(e, n); cs.add(e, n, z === z ? z : 0, { serial: c.ser, name: c.name, type: c.type, disposition: c.disp, acres: c.acres, status: status.toUpperCase() }); } if (cs.n) { cs.provenance = { bundle: `data/openground/${aoi}_claims.json`, note: 'MLRS centroids, not staked corners' }; proj.add(cs); } } } catch (e) { /* optional */ }
  }
  // Geology the map handed over (decoded from the national USGS PMTiles in
  // the viewport).  One source per area: where the AOI bundle drew anything
  // the hand-off units are left out and the project says so, because the same
  // contact drawn twice from two compilations reads as two contacts.
  const hg = opts.handoff && opts.handoff.geology && typeof opts.handoff.geology === 'object' ? opts.handoff.geology : null;
  const hgUnits = hg && Array.isArray(hg.units) ? hg.units.length : 0, hgFaults = hg && Array.isArray(hg.faults) ? hg.faults.length : 0;
  if (hgUnits + hgFaults > 0) {
    if (bundleGeo) {
      const note = `map hand-off geology (${hgUnits} unit(s), ${hgFaults} fault(s)) not used: the ${aoi} bundle covers this site, and one source per area keeps a contact from being drawn twice`;
      proj.metadata.notes.push(note); proj.metadata.geology_source.handoff_skipped = { units: hgUnits, faults: hgFaults, note };
    } else {
      log('draping the map\'s geology…');
      const conv = geologyFromHandoff(hg);
      const objs = geologyObjects(conv.geo, crs, topo, box, { aoi: 'map hand-off', bundle: conv.label, provenance: Object.assign({ handoff: 'viewport snapshot of the map\'s loaded geology tiles, not an archive' }, opts.handoff.at ? { snapshot_at: opts.handoff.at } : {}) });
      for (const o of objs) proj.add(o);
      budgetNote(objs.stats, 'map hand-off');
      const bad = new Map(conv.unparsed.map(u => [String(u.id), u]));
      for (const o of objs) if (o.kind === 'mesh' && o.role === 'geology' && bad.has(String(o.metadata.unit_id))) { const u = bad.get(String(o.metadata.unit_id)); o.warn(`age not read for unit '${u.nm}': ${u.age == null ? 'no age text' : `"${u.age}"`} is not in the geologic-time table — t0 / t1 left unset rather than guessed`); }
      if (conv.unparsed.length) warn(`age text not read for ${conv.unparsed.length} hand-off unit(s) (${conv.unparsed.map(u => u.nm).slice(0, 4).join(', ')}${conv.unparsed.length > 4 ? ', …' : ''}) — t0 / t1 left unset rather than guessed`);
      proj.metadata.geology_source = { kind: 'handoff', bundle: conv.label, sources: conv.sources, units: objs.stats.units, faults: objs.stats.faults, note: hg.note || null };
    }
  }
  if (!proj.objects.some(o => (o.kind === 'mesh' && o.role === 'geology') || (o.kind === 'lineset' && o.role === 'faults'))) {
    if (!proj.metadata.geology_source) proj.metadata.geology_source = { kind: 'none' };
    warn('no mapped geology in this model — open it from the map with USGS GEOLOGY on and the site in view, or drop a geology file');
  }
  log('loading graded mines…');
  try { const gr = await jget(`${base}data/grades/grades.json`); const ps = minesFromGrades(gr, crs, topo, box, opts.gi); if (ps.n) proj.add(ps); } catch (e) { warn(`grades table unavailable: ${e.message}`); }
  if (opts.handoff) { const existing = new Set(proj.objects.map(o => o.name)); for (const ps of pointsFromHandoff(opts.handoff, crs, topo, box)) if (!existing.has(ps.name) || !ps.role.startsWith('claims')) proj.add(ps); }
  // No scaffold layers: an empty 'Workings (digitised)' row and a 0-unit
  // stratigraphy used to be added here so the tree showed where they would
  // go.  The tree now lists those groups itself as '— not started · step n'
  // with the tool to open, and the tools create the layer on the first
  // committed feature, so nothing empty is autosaved on the user's behalf.
  const zr = topo.zrange(); const zmin = zr[0] === zr[0] ? zr[0] - 400 : -500, zmax = zr[1] === zr[1] ? zr[1] + 50 : 500;
  // The two preset sections are scaffolding, not data: they start hidden so a
  // fresh model shows terrain rather than two 3 km glass walls, and the Section
  // tool ticks one visible when it is chosen.
  proj.add(new GM.Section({ start: [cx - radius, cy], end: [cx + radius, cy], z_min: zmin, z_max: zmax, name: 'Section W-E', visible: false }));
  proj.add(new GM.Section({ start: [cx, cy - radius], end: [cx, cy + radius], z_min: zmin, z_max: zmax, name: 'Section S-N', visible: false }));
  return { project: proj, topoRes };
}
