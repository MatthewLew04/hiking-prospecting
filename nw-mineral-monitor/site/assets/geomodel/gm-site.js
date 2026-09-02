/* gm-site.js — build a model project in the browser around a map point: the
   JS twin of pipelines/geomodel/kit.py (build_site_model).  Terrain comes from
   the same AWS terrarium tiles the map's 3-D mode streams; imagery (satellite /
   USGS topo / Macrostrat geology) is stitched into a texture draped on the
   topography; AOI bundles (geology units, faults, targets, claims) and the
   national graded-mines table come from site/data; tiled layers the map had
   loaded arrive through the IndexedDB hand-off the OPEN 3D MODEL button writes. */
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

/** Draped geology + faults from an AOI bundle. */
export function geologyObjects(geo, crs, topo, box, opts = {}) {
  const out = []; const fwd = (lon, lat) => GM.utm.fwd(lon, lat, crs.zone, crs.north); const [minx, miny, maxx, maxy] = box;
  const cell = topo.dx; const elev = (x, y, lift = 0) => { const v = topo.sample(x, y); return (v === v ? v : 0) + lift; };
  const inbox = (x, y) => x >= minx && x <= maxx && y >= miny && y <= maxy;
  let nTri = 0; const budget = opts.maxTriangles || 150000; const maxEdge = Math.max(cell * 2, (maxx - minx) / 60);
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
      const m = new GM.Mesh({ vertices: v, triangles: tflat, name: u.nm || u.id || 'unit', color: E.unitColor(u), role: 'geology', group: 'Geology (draped)', opacity: 0.85 });
      Object.assign(m.metadata, { unit_id: u.id, age: u.age, lithology: u.li, description: u.de, source: u.src, t0_ma: u.t0, t1_ma: u.t1 });
      m.provenance = { bundle: `data/geology/${opts.aoi}.json`, unit: u.id, drape: `terrain +1.5 m, edges <= ${maxEdge.toFixed(0)} m` };
      out.push(m); nTri += t2.length;
    }
    const ol = new GM.LineSet({ name: (u.nm || 'unit') + ' outline', role: 'geology-outline', color: [40, 40, 40], group: 'Geology outlines' });
    for (const ring of polys) { const dense = E.densify(ring.concat([ring[0]]), cell); ol.addPolyline(dense.map(([x, y]) => [x, y, elev(x, y, 2)]), { unit: u.nm, unit_id: u.id, age: u.age }); }
    ol.provenance = { bundle: `data/geology/${opts.aoi}.json` }; out.push(ol);
  }
  const fl = new GM.LineSet({ name: 'Faults (mapped)', role: 'faults', color: [212, 165, 63], group: 'Structure' });
  for (const f of (geo.faults || []).filter(f => f.path)) { const path = f.path.map(([x, y]) => fwd(x, y)); if (!path.some(([x, y]) => inbox(x, y))) continue; const dense = E.densify(path, cell); fl.addPolyline(dense.map(([x, y]) => [x, y, elev(x, y, 3)]), { name: f.nm, type: f.ty, source: f.src }); }
  if (fl.parts.length) { fl.provenance = { bundle: `data/geology/${opts.aoi}.json`, drape: 'terrain +3 m' }; out.push(fl); }
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

export function pointsFromHandoff(h, crs, topo, box) {
  const out = []; const [minx, miny, maxx, maxy] = box; const colors = { mrds: [110, 140, 190], usmin: [140, 120, 170], stategeo: [90, 160, 120], ardf: [160, 130, 90], claimsA: [255, 80, 80], claimsC: [120, 120, 160] };
  const names = { mrds: 'USGS MRDS occurrences', usmin: 'USGS USMIN map features', stategeo: 'State-survey mine records', ardf: 'Alaska ARDF occurrences', claimsA: 'Claims active (BLM centroids)', claimsC: 'Claims closed (BLM centroids)' };
  for (const [layer, feats] of Object.entries(h.layers || {})) {
    const ps = new GM.PointSet({ name: names[layer] || layer, role: layer.startsWith('claims') ? 'claims' : 'points', color: colors[layer] || [150, 150, 150], group: layer.startsWith('claims') ? 'Claims' : 'Mines' });
    for (const f of feats) { const [e, n] = GM.utm.fwd(f.x, f.y, crs.zone, crs.north); if (e < minx || e > maxx || n < miny || n > maxy) continue; const z = topo.sample(e, n); const attrs = Object.assign({}, f.p || {}); if (layer === 'claimsA') attrs.status = 'ACTIVE'; if (layer === 'claimsC') attrs.status = 'CLOSED'; ps.add(e, n, z === z ? z : 0, attrs); }
    if (ps.n) { ps.provenance = { source: 'map hand-off (viewport snapshot of tiled layer ' + layer + ')', note: 'viewport snapshot, not an archive' }; out.push(ps); }
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
  if (aoi) {
    log(`loading ${aoi} geology…`);
    try { const geo = await jget(`${base}data/geology/${aoi}.json`); for (const o of geologyObjects(geo, crs, topo, box, { aoi })) proj.add(o); } catch (e) { proj.metadata.warnings = (proj.metadata.warnings || []).concat([`geology bundle unavailable: ${e.message}`]); }
    try { const tj = await jget(`${base}data/targets/${aoi}.json`); const ts = new GM.PointSet({ name: 'Geology targets (scored)', role: 'targets', color: [45, 212, 191], group: 'Mines' }); for (const t of tj.targets || []) { if (t.cx == null) continue; const [e, n] = GM.utm.fwd(t.cx, t.cy, zone, north); if (e < box[0] || e > box[2] || n < box[1] || n > box[3]) continue; const z = topo.sample(e, n); ts.add(e, n, z === z ? z : 0, { tier: t.tier, score: t.score, unit: t.nm, age: t.age, area_km2: t.area_km2, money: t.money ? 1 : 0, tier_name: t.tierName }); } if (ts.n) { ts.provenance = { bundle: `data/targets/${aoi}.json` }; proj.add(ts); } } catch (e) { /* optional */ }
    try { const cl = await jget(`${base}data/openground/${aoi}_claims.json`); for (const [status, color] of [['active', [255, 80, 80]], ['closed', [120, 120, 160]]]) { const cs = new GM.PointSet({ name: `Claims ${status} (BLM centroids)`, role: 'claims', color, group: 'Claims' }); for (const c of cl[status] || []) { if (c.x == null) continue; const [e, n] = GM.utm.fwd(c.x, c.y, zone, north); if (e < box[0] || e > box[2] || n < box[1] || n > box[3]) continue; const z = topo.sample(e, n); cs.add(e, n, z === z ? z : 0, { serial: c.ser, name: c.name, type: c.type, disposition: c.disp, acres: c.acres, status: status.toUpperCase() }); } if (cs.n) { cs.provenance = { bundle: `data/openground/${aoi}_claims.json`, note: 'MLRS centroids, not staked corners' }; proj.add(cs); } } } catch (e) { /* optional */ }
  }
  log('loading graded mines…');
  try { const gr = await jget(`${base}data/grades/grades.json`); const ps = minesFromGrades(gr, crs, topo, box, opts.gi); if (ps.n) proj.add(ps); } catch (e) { proj.metadata.warnings = (proj.metadata.warnings || []).concat([`grades table unavailable: ${e.message}`]); }
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
