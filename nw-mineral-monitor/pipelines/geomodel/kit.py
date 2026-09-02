"""geomodel.kit — build a 3-D model project for a mine (or any point) from the
repo's research bundles, and export it in every format the modelling packages
read.

``build_site_model`` is the Python twin of what site/model3d.html does in the
browser when a user clicks "OPEN 3D MODEL" on a mine card:

  * topography  — Grid2D in WGS84/UTM sampled from the same AWS terrarium
                  tiles as the map's 3-D mode (cached under pipelines/cache/)
  * geology     — AOI harmonised map units as draped, terrain-following meshes
                  (clipped to the site box) + draped outlines; faults as
                  draped polylines
  * mines       — graded mines inside the radius (all commodity columns),
                  scored targets, BLM claim centroids when the AOI has them
  * scaffolding — an empty workings layer (schema in metadata), an empty
                  stratigraphy (topography only), two default sections

Everything is honest about provenance: each object carries where it came
from, the terrain zoom, and — when tiles were unreachable — that Z is 0.
"""
import math
import os
import re
import sys

from .model import (Project, Grid2D, Mesh, LineSet, PointSet, Section, StratModel,
                    utm_crs, farray, iarray, NAN)
from . import workings as wk

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINES = os.path.normpath(os.path.join(HERE, '..'))
ROOT = os.path.normpath(os.path.join(PIPELINES, '..'))
SITE = os.path.join(ROOT, 'site')
if PIPELINES not in sys.path:
    sys.path.insert(0, PIPELINES)

from leapfrog_export import (utm_zone, utm_fwd, utm_inv, utm_epsg, Terrain,  # noqa: E402
                             load_json)


def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or 'site').lower()).strip('-')
    return s or 'site'


def aoi_for_point(lon, lat, config=None):
    """AOI key whose bbox contains the point (smallest bbox wins), or None."""
    cfg = config or load_json(os.path.join(PIPELINES, 'config', 'aoi.json')) or {}
    best = None
    for key, a in (cfg.get('aois') or {}).items():
        b = a.get('bbox')
        if not b or not (b[0] <= lon <= b[2] and b[1] <= lat <= b[3]):
            continue
        area = (b[2] - b[0]) * (b[3] - b[1])
        if best is None or area < best[0]:
            best = (area, key)
    return best[1] if best else None


# ---------------------------------------------------------------- geometry
def clip_ring_rect(ring, minx, miny, maxx, maxy):
    """Sutherland–Hodgman clip of a ring (list of (x,y)) to a rectangle."""
    def clip(poly, inside, intersect):
        out = []
        if not poly:
            return out
        prev = poly[-1]
        for cur in poly:
            if inside(cur):
                if not inside(prev):
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(intersect(prev, cur))
            prev = cur
        return out

    def ix(a, b, x):
        t = (x - a[0]) / (b[0] - a[0])
        return (x, a[1] + (b[1] - a[1]) * t)

    def iy(a, b, y):
        t = (y - a[1]) / (b[1] - a[1])
        return (a[0] + (b[0] - a[0]) * t, y)
    poly = [tuple(p[:2]) for p in ring]
    if poly and poly[0] == poly[-1]:
        poly = poly[:-1]
    poly = clip(poly, lambda p: p[0] >= minx, lambda a, b: ix(a, b, minx))
    poly = clip(poly, lambda p: p[0] <= maxx, lambda a, b: ix(a, b, maxx))
    poly = clip(poly, lambda p: p[1] >= miny, lambda a, b: iy(a, b, miny))
    poly = clip(poly, lambda p: p[1] <= maxy, lambda a, b: iy(a, b, maxy))
    # drop consecutive duplicates
    clean = []
    for p in poly:
        if not clean or (abs(p[0] - clean[-1][0]) > 1e-9 or abs(p[1] - clean[-1][1]) > 1e-9):
            clean.append(p)
    if len(clean) > 1 and abs(clean[0][0] - clean[-1][0]) < 1e-9 and abs(clean[0][1] - clean[-1][1]) < 1e-9:
        clean.pop()
    return clean


def subdivide_triangles(verts2d, tris, max_edge):
    """Midpoint-subdivide (x, y) triangles until every edge <= max_edge.
    Returns (points, triangles) with shared midpoints."""
    pts = [tuple(p) for p in verts2d]
    tris = [tuple(t) for t in tris]
    mid = {}

    def midpoint(a, b):
        key = (a, b) if a < b else (b, a)
        m = mid.get(key)
        if m is None:
            m = len(pts)
            pts.append(((pts[a][0] + pts[b][0]) / 2.0, (pts[a][1] + pts[b][1]) / 2.0))
            mid[key] = m
        return m

    def elen(a, b):
        return math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1])
    out = []
    stack = list(tris)
    guard = 0
    while stack and guard < 2000000:
        guard += 1
        a, b, c = stack.pop()
        if max(elen(a, b), elen(b, c), elen(c, a)) <= max_edge:
            out.append((a, b, c))
            continue
        ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
        stack.extend([(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)])
    return pts, out


def densify(path, step):
    """Insert points along a 2-D polyline so no segment exceeds ``step``."""
    out = []
    for k in range(len(path) - 1):
        x0, y0 = path[k][0], path[k][1]
        x1, y1 = path[k + 1][0], path[k + 1][1]
        L = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(math.ceil(L / step)))
        for q in range(n):
            t = q / float(n)
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    if path:
        out.append((path[-1][0], path[-1][1]))
    return out


# ----------------------------------------------------------------- builder
def build_site_model(lon, lat, radius_m=2500.0, name=None, aoi='auto', grade_index=None,
                     zoom=13, cell=None, offline=False, max_unit_triangles=120000, log=print):
    """Build a Project around (lon, lat).  Returns the Project; nothing is
    written.  ``aoi='auto'`` picks the AOI bundle containing the point."""
    d = os.path.join(SITE, 'data')
    grades = load_json(os.path.join(d, 'grades', 'grades.json'))
    if grade_index is not None and grades:
        gi = int(grade_index)
        lon, lat = grades['x'][gi], grades['y'][gi]
        name = name or grades['name'][gi]
    name = name or ('site %.4f %.4f' % (lat, lon))
    zone, north = utm_zone(lon, lat)
    fwd = lambda lo, la: utm_fwd(lo, la, zone, north)
    inv = lambda e, n_: utm_inv(e, n_, zone, north)
    cx, cy = fwd(lon, lat)
    R = float(radius_m)
    minx, miny, maxx, maxy = cx - R, cy - R, cx + R, cy + R
    if aoi == 'auto':
        aoi = aoi_for_point(lon, lat)
    proj = Project(name, utm_crs(zone, north), origin=[round(cx, -2), round(cy, -2), 0.0],
                   site={'name': name, 'lon': lon, 'lat': lat, 'radius_m': R, 'aoi': aoi,
                         'grade_index': grade_index, 'utm_zone': '%d%s' % (zone, 'N' if north else 'S')})
    proj.metadata['generator'] = 'geomodel.kit.build_site_model'
    proj.metadata['notes'] = [
        'Coordinates: WGS84 / UTM zone %d%s (EPSG:%d), metres, Z = elevation.' % (zone, 'N' if north else 'S', utm_epsg(zone, north)),
        'Research context only: claim points are BLM centroids, grades are cited historic figures, geology is map-scale.',
        'Never enter adits or shafts.']

    # ---- topography
    terr = Terrain(zoom, offline=offline)
    probe = terr.sample(lon, lat)
    have_elev = probe is not None
    native = 156543.03 * math.cos(math.radians(lat)) / 2 ** zoom
    if cell is None:
        cell = max(round(native), 5.0)
        while (2 * R / cell) ** 2 > 1.2e6:
            cell *= 2
    nx = int(math.ceil(2 * R / cell)) + 1
    ny = nx
    topo = Grid2D(nx, ny, minx, miny, cell, cell, name='Topography', role='topography',
                  color=[150, 150, 150])
    vals = farray()
    for j in range(ny):
        for i in range(nx):
            x, y = topo.node_xy(i, j)
            lo, la = inv(x, y)
            v = terr.sample(lo, la) if have_elev else None
            vals.append(NAN if v is None else v)
    topo.values = vals
    topo.provenance = {'source': 'AWS Terrain Tiles (Mapzen terrarium: 3DEP/SRTM composite)',
                       'zoom': zoom, 'cell_m': cell, 'reachable': have_elev}
    if not have_elev:
        topo.metadata.setdefault('warnings', []).append(
            'terrain tiles unreachable and not cached: elevations are NaN (treat as unknown)')
        log('WARNING: terrain unreachable — topography has no data')
    proj.add(topo)

    def elev(x, y, lift=0.0):
        v = topo.sample(x, y)
        return (0.0 if v != v else v) + lift

    # ---- AOI bundles
    geology = load_json(os.path.join(d, 'geology', '%s.json' % aoi)) if aoi else None
    targets = load_json(os.path.join(d, 'targets', '%s.json' % aoi)) if aoi else None
    claims = load_json(os.path.join(d, 'openground', '%s_claims.json' % aoi)) if aoi else None
    inbox = lambda x, y: minx <= x <= maxx and miny <= y <= maxy

    n_tri_total = 0
    if geology:
        units = [u for u in geology.get('units', []) if u.get('g')]
        budget_edge = max(cell * 2, 2 * R / 60.0)
        for u in units:
            polys_xy = []
            for poly in u['g']:
                outer = [fwd(x, y) for x, y in poly[0]]
                xs = [p[0] for p in outer]
                ys = [p[1] for p in outer]
                if max(xs) < minx or min(xs) > maxx or max(ys) < miny or min(ys) > maxy:
                    continue
                clipped = clip_ring_rect(outer, minx, miny, maxx, maxy)
                if len(clipped) >= 3:
                    polys_xy.append(clipped)
            if not polys_xy:
                continue
            verts2d, tris = [], []
            for ring in polys_xy:
                if abs(wk._signed_area(ring)) < 1.0:
                    continue
                if wk._signed_area(ring) < 0:
                    ring = list(reversed(ring))
                base = len(verts2d)
                verts2d.extend(ring)
                for a, b, c in wk._ear_clip(ring):
                    tris.append((base + a, base + b, base + c))
            if not tris:
                continue
            pts, tris = subdivide_triangles(verts2d, tris, budget_edge)
            if n_tri_total + len(tris) > max_unit_triangles:
                log('note: geology triangle budget reached; %s kept as outline only' % u.get('nm'))
                tris = []
            if tris:
                vflat = farray()
                for x, y in pts:
                    vflat.extend((x, y, elev(x, y, 1.5)))
                tflat = iarray()
                for t in tris:
                    tflat.extend(t)
                color = _unit_color(u)
                m = Mesh(vflat, tflat, name=u.get('nm') or u.get('id') or 'unit', color=color,
                         role='geology', group='Geology (draped)', opacity=0.85)
                m.metadata.update({'unit_id': u.get('id'), 'age': u.get('age'), 'lithology': u.get('li'),
                                   'description': u.get('de'), 'source': u.get('src'),
                                   't0_ma': u.get('t0'), 't1_ma': u.get('t1'), 'aoi': aoi})
                m.provenance = {'bundle': 'site/data/geology/%s.json' % aoi, 'unit': u.get('id'),
                                'drape': 'terrain +1.5 m, edges <= %.0f m' % budget_edge}
                proj.add(m)
                n_tri_total += len(tris)
            ol = LineSet(name=(u.get('nm') or 'unit') + ' outline', role='geology-outline',
                         color=[40, 40, 40], group='Geology outlines')
            for ring in polys_xy:
                dense = densify(ring + [ring[0]], cell)
                ol.add_polyline([(x, y, elev(x, y, 2.0)) for x, y in dense],
                                {'unit': u.get('nm'), 'unit_id': u.get('id'), 'age': u.get('age')})
            ol.provenance = {'bundle': 'site/data/geology/%s.json' % aoi}
            proj.add(ol)
        faults = [f for f in geology.get('faults', []) if f.get('path')]
        fl = LineSet(name='Faults (mapped)', role='faults', color=[212, 165, 63], group='Structure')
        for f in faults:
            path = [fwd(x, y) for x, y in f['path']]
            if not any(inbox(x, y) for x, y in path):
                continue
            dense = densify(path, cell)
            fl.add_polyline([(x, y, elev(x, y, 3.0)) for x, y in dense],
                            {'name': f.get('nm'), 'type': f.get('ty'), 'source': f.get('src')})
        if fl.parts:
            fl.provenance = {'bundle': 'site/data/geology/%s.json' % aoi, 'drape': 'terrain +3 m'}
            proj.add(fl)

    # ---- graded mines
    if grades:
        ps = PointSet(name='Mines (cited grades)', role='mines', color=[201, 133, 0], group='Mines')
        cols = [c for c in ('name', 'st', 'cnty', 'dist', 'au', 'ag', 'pb', 'zn', 'cu', 'sb', 'wo3',
                            'hgf', 'usd', 'ton', 'yd3', 'plc', 'open', 'com', 'src', 'url', 'basis',
                            'yrs', 'quote') if c in grades]
        for i in range(grades['n']):
            x, y = grades['x'][i], grades['y'][i]
            if x is None or y is None:
                continue
            e, n_ = fwd(x, y)
            if not inbox(e, n_):
                continue
            attrs = {c: grades[c][i] for c in cols}
            attrs['grade_index'] = i
            attrs['is_site'] = 1 if (grade_index is not None and i == int(grade_index)) else 0
            ps.add(e, n_, elev(e, n_), **attrs)
        if ps.n:
            ps.provenance = {'bundle': 'site/data/grades/grades.json', 'note': 'best cited grade per mine; units per column'}
            proj.add(ps)
    if targets:
        ts = PointSet(name='Geology targets (scored)', role='targets', color=[45, 212, 191], group='Mines')
        for t in targets.get('targets', []):
            if t.get('cx') is None:
                continue
            e, n_ = fwd(t['cx'], t['cy'])
            if inbox(e, n_):
                ts.add(e, n_, elev(e, n_), tier=t.get('tier'), score=t.get('score'), unit=t.get('nm'),
                       age=t.get('age'), area_km2=t.get('area_km2'), money=1 if t.get('money') else 0,
                       tier_name=t.get('tierName'))
        if ts.n:
            ts.provenance = {'bundle': 'site/data/targets/%s.json' % aoi}
            proj.add(ts)
    if claims:
        for status, color in (('active', [255, 80, 80]), ('closed', [120, 120, 160])):
            cs = PointSet(name='Claims %s (BLM centroids)' % status, role='claims', color=color, group='Claims')
            for c in claims.get(status, []) or []:
                if c.get('x') is None:
                    continue
                e, n_ = fwd(c['x'], c['y'])
                if inbox(e, n_):
                    cs.add(e, n_, elev(e, n_), serial=c.get('ser'), name=c.get('name'), type=c.get('type'),
                           disposition=c.get('disp'), acres=c.get('acres'), status=status.upper())
            if cs.n:
                cs.provenance = {'bundle': 'site/data/openground/%s_claims.json' % aoi,
                                 'note': 'MLRS centroids, not staked corners'}
                proj.add(cs)

    # ---- scaffolding: workings, stratigraphy, sections
    ws = wk.new_workings('Workings (digitised)', mine=name)
    ws.group = 'Workings'
    ws.metadata['howto'] = ('Georeference a level plan as an ImagePlane at its level elevation, trace '
                            'drifts; add adits from portals, shafts from collars (see geomodel.workings).')
    proj.add(ws)
    sm = StratModel(name='Stratigraphy (pancake)', topography=topo.id)
    sm.metadata['howto'] = 'Add units top-down with contact points or surfaces; build with geomodel.stratigraphy.'
    proj.add(sm)
    zr = topo.zrange()
    zmin = (zr[0] - 400.0) if zr[0] == zr[0] else -500.0
    zmax = (zr[1] + 50.0) if zr[1] == zr[1] else 500.0
    # Preset sections are scaffolding: they start hidden so a fresh model shows
    # terrain rather than two glass walls (the viewer's Section tool ticks one
    # visible when it is chosen).
    proj.add(Section(start=[cx - R, cy], end=[cx + R, cy], z_min=zmin, z_max=zmax, name='Section W-E', visible=False))
    proj.add(Section(start=[cx, cy - R], end=[cx, cy + R], z_min=zmin, z_max=zmax, name='Section S-N', visible=False))
    proj.metadata['summary'] = {o.kind + ':' + o.name: (o.bounds() or None) for o in proj.objects}
    return proj


def _unit_color(u):
    # stable pastel from the unit id/name
    s = str(u.get('id') or u.get('nm') or 'u')
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xffffff
    r, g, b = (h >> 16) & 255, (h >> 8) & 255, h & 255
    return [128 + r // 2, 128 + g // 2, 128 + b // 2]


# ------------------------------------------------------------------ export
def export_project(proj, out_dir, formats=('json', 'omf2', 'omf1', 'surfer', 'asc', 'gxf', 'dxf', 'csv', 'obj'),
                   log=print):
    """Write the project in interchange formats.  Returns a manifest list."""
    from .formats import omf1, omf2, surfer, arcascii, geosoft, dxf, tables, obj
    os.makedirs(out_dir, exist_ok=True)
    slug = slugify(proj.name)
    manifest = []
    if 'json' in formats:
        p = os.path.join(out_dir, slug + '.geomodel.json')
        proj.save(p)
        manifest.append((os.path.basename(p), 'model3d.html project (drop onto the 3-D viewer)'))
    if 'omf2' in formats:
        p = os.path.join(out_dir, slug + '.omf')
        omf2.write_omf2(proj, p, name=proj.name, description='NW Mineral Monitor geomodel export')
        manifest.append((os.path.basename(p), 'OMF v2.0 — Leapfrog Geo 2025.1+ (Leapfrog menu > OMF > Import), Seequent Evo'))
    if 'omf1' in formats:
        p = os.path.join(out_dir, slug + '-omf09.omf')
        omf1.write_omf1(proj, p, name=proj.name, description='NW Mineral Monitor geomodel export (OMF v0.9)')
        manifest.append((os.path.basename(p), 'OMF v0.9 — Leapfrog Geo <= 2024.1'))
    for g in proj.by_kind('grid2d'):
        base = os.path.join(out_dir, slugify(g.name))
        if 'surfer' in formats:
            surfer.write_grd(g, base + '.grd', fmt='dsrb')
            manifest.append((os.path.basename(base) + '.grd', 'Surfer 7 binary grid — Surfer, Leapfrog elevation/2-D grid import, Oasis montaj'))
        if 'asc' in formats and abs(g.dx - g.dy) < 1e-9 and not g.rotation:
            arcascii.write_asc(g, base + '.asc')
            manifest.append((os.path.basename(base) + '.asc', 'Arc/Info ASCII grid — Leapfrog Import Elevation Grid, QGIS'))
        if 'gxf' in formats:
            geosoft.write_gxf(g, base + '.gxf')
            manifest.append((os.path.basename(base) + '.gxf', 'Geosoft GXF — Oasis montaj grid import, Leapfrog'))
    meshes = proj.by_kind('mesh')
    lines = proj.by_kind('lineset')
    if 'dxf' in formats and (meshes or lines):
        p = os.path.join(out_dir, slug + '.dxf')
        dxf.write_dxf(meshes + lines, p)
        manifest.append((os.path.basename(p), 'DXF R12 — 3DFACE meshes + 3-D polylines (Leapfrog, AutoCAD, Surpac, Vulcan)'))
    if 'obj' in formats:
        for m in meshes:
            p = os.path.join(out_dir, slugify(m.name) + '.obj')
            obj.write_obj(m, p)
            manifest.append((os.path.basename(p), 'OBJ mesh'))
    if 'csv' in formats:
        for ps in proj.by_kind('points'):
            p = os.path.join(out_dir, slugify(ps.name) + '.csv')
            tables.write_points_csv(ps, p, leapfrog=True)
            manifest.append((os.path.basename(p), 'points CSV (East,North,Elev + columns) — Leapfrog Points import'))
    with open(os.path.join(out_dir, 'README-GEOMODEL.md'), 'w', encoding='utf-8') as fh:
        fh.write(readme(proj, manifest))
    manifest.append(('README-GEOMODEL.md', 'what is here, CRS, import click-paths'))
    for f, desc in manifest:
        log('  %-40s %s' % (f, desc))
    return manifest


def readme(proj, manifest):
    crs = proj.crs
    lines = ['# %s — 3-D model kit' % proj.name, '',
             'Generated by nw-mineral-monitor `geomodel` (%s).' % proj.modified,
             'CRS: **WGS84 / UTM zone %s (EPSG:%s), metres, Z = elevation** — set exactly that when a package asks.' %
             (('%d%s' % (crs.get('zone', 0), 'N' if crs.get('north', True) else 'S')) if crs.get('kind') == 'utm' else 'local', crs.get('epsg', 'n/a')),
             '', '## Files', '']
    for f, d in manifest:
        lines.append('- `%s` — %s' % (f, d))
    lines += ['', '## Import click-paths', '',
              '- **Leapfrog Geo 2025.1+**: Leapfrog menu > OMF > Import > `%s.omf` (one shot; OMF objects cannot be reloaded).' % slugify(proj.name),
              '  Older Leapfrog (<= 2024.1): use `%s-omf09.omf`.' % slugify(proj.name),
              '  Refreshable route: Topographies > New Topography > Import Elevation Grid (`topography.grd` / `.asc`), '
              'Meshes > Import Mesh (`.obj`/`.dxf`), Points > Import Points (`.csv`).',
              '- **Oasis montaj / Target**: Grid and Image > Import > `topography.gxf` or `.grd`; XYZ/CSV for points.',
              '- **Surfer**: File > Open > `topography.grd` (Surfer 7 binary); Map > New > 3D Surface.',
              '- **Kingdom**: grids via XYZ/ZMAP+ export (`geomodel_kit.py convert topography.grd topography.zmap`).',
              '- **Browser**: drop `%s.geomodel.json` onto site/model3d.html (or open it from the mine card).' % slugify(proj.name),
              '', '## Honesty notes', '']
    for n in proj.metadata.get('notes', []):
        lines.append('- ' + n)
    topo = next((g for g in proj.by_kind('grid2d') if g.role == 'topography'), None)
    if topo:
        lines.append('- Topography: %s, zoom %s, %s m cells%s.' % (
            topo.provenance.get('source'), topo.provenance.get('zoom'), topo.provenance.get('cell_m'),
            '' if topo.provenance.get('reachable', True) else ' — TILES UNREACHABLE: no elevations'))
    lines.append('- Draped geology meshes follow terrain at +1.5 m; they are map polygons, not modelled volumes.')
    lines.append('- The workings and stratigraphy objects are empty scaffolds until you digitise maps / add contacts.')
    return '\n'.join(lines) + '\n'


def convert(src, dst, in_format=None, out_format=None, log=print):
    """Generic file conversion through the registry (object lists)."""
    from . import formats as F
    in_format = in_format or F.sniff(src)
    if in_format is None:
        raise ValueError('cannot detect the input format of %s — pass --in-format' % src)
    ext = os.path.splitext(dst)[1].lower()
    if out_format is None:
        cands = F.formats_for_extension(ext)
        if not cands:
            raise ValueError('no writer for %s — pass --out-format' % ext)
        out_format = cands[0]
    objs = _read_any(in_format, src)
    writer = F.writer(out_format)
    if writer is None:
        raise ValueError('%s is read-only' % out_format)
    written = _write_any(out_format, writer, objs, dst)
    log('%s (%s) -> %s (%s): %s' % (src, in_format, dst, out_format, written))
    return written


def _read_any(fmt, src):
    from . import formats as F
    reader = F.reader(fmt)
    if fmt == 'ubc':
        base, ext = os.path.splitext(src)
        models = {}
        for cand in ('.mod', '.den', '.sus', '.mag', '.gra', '.nev'):
            if os.path.exists(base + cand):
                models[cand[1:]] = base + cand
        obj = reader(src, models=models or None)
    elif fmt == 'csv_drillholes':
        obj = reader(src)
    else:
        obj = reader(src)
    if isinstance(obj, Project):
        return list(obj.objects)
    if isinstance(obj, dict):                     # segy / las dicts
        return [obj]
    return obj if isinstance(obj, list) else [obj]


def _write_any(fmt, writer, objs, dst):
    if fmt in ('omf1', 'omf2'):
        writer(objs, dst)
        return '%d objects' % len(objs)
    if fmt == 'dxf':
        writer([o for o in objs if o.kind in ('mesh', 'lineset', 'points')], dst)
        return '%d objects' % len(objs)
    # single-object writers: pick the first compatible object
    want = {'surfer_grd': ('grid2d',), 'geosoft_grd': ('grid2d',), 'gxf': ('grid2d',), 'arc_ascii': ('grid2d',),
            'zmap': ('grid2d',), 'irap': ('grid2d',), 'obj': ('mesh',), 'gocad_ts': ('mesh', 'lineset', 'points'),
            'lf_msh': ('mesh',), 'csv_points': ('points',), 'csv_structural': ('points',),
            'csv_blockmodel': ('blockmodel',), 'ubc': ('blockmodel',), 'geosoft_xyz': ('points',),
            'surfer_bln': ('lineset',), 'csv_drillholes': ('drillholes',)}.get(fmt)
    for o in objs:
        if want is None or getattr(o, 'kind', None) in want:
            if fmt == 'ubc':
                base = os.path.splitext(dst)[0]
                attr = next(iter(o.attributes), None)
                writer(o, dst, base + '.mod', attr)
            else:
                writer(o, dst)
            return getattr(o, 'name', '?')
    raise ValueError('no object of kind %s to write as %s' % (want, fmt))


def describe(src, fmt=None):
    """Human summary of a file's contents."""
    from . import formats as F
    fmt = fmt or F.sniff(src)
    objs = _read_any(fmt, src)
    out = ['%s: %s' % (src, fmt)]
    for o in objs:
        if isinstance(o, dict):
            out.append('  dict with keys %s' % ', '.join(sorted(o)[:12]))
            continue
        b = o.bounds()
        extra = ''
        if o.kind == 'grid2d':
            extra = ' %dx%d cell %.3gx%.3g' % (o.nx, o.ny, o.dx, o.dy)
        elif o.kind == 'mesh':
            extra = ' %d verts %d tris' % (o.n_vertices, o.n_triangles)
        elif o.kind == 'lineset':
            extra = ' %d parts' % len(o.parts)
        elif o.kind == 'points':
            extra = ' %d points cols=%s' % (o.n, ','.join(list(o.attributes)[:8]))
        elif o.kind == 'blockmodel':
            extra = ' %s blocks attrs=%s' % ('x'.join(map(str, o.count)), ','.join(o.attributes))
        out.append('  [%s] %s%s bounds=%s' % (o.kind, o.name, extra,
                   None if not b else tuple(round(v, 2) for v in b)))
        for w in (o.metadata.get('warnings') or []):
            out.append('    warning: %s' % w)
    return '\n'.join(out)
