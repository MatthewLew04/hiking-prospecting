"""geomodel.workings — underground workings: the schema, constructors, and the
bridge from "maps that are 3-D in a 2-D format" (level plans, longitudinal
sections, old USBM/USGS mine sketches) to 3-D geometry.

Schema.  Workings live in a ``LineSet`` with ``role='workings'``; each part is
one feature and ``features[k]`` holds its attributes:

    type      adit | tunnel | drift | crosscut | shaft | winze | raise |
              decline | stope | portal | level | trench | pit | unknown
    name      'No. 2 adit', '300 level', 'Main shaft'...
    level     level label as written on the map ('300', '2nd')
    level_z   elevation (m) of the level if known
    mine      mine name
    width_m / height_m   typical opening size (for tubes / stope prisms)
    source    {'doc': title, 'page': n, 'url': ..., 'figure': 'Plate 3'}
    confidence  'surveyed' | 'sketched' | 'inferred' | 'described' | 'assumed'
              ('assumed' is a value an operator or an agent supplied in answer
              to a question the source text left open — see geomodel.narrative)
    units_in    'ft' | 'm'   (what the map used; geometry is always metres)
    notes     free text

Stopes are extruded outlines (``stope_prism``) stored as closed Meshes with
role 'stope' and the same attribute keys in ``metadata``.

How a paper map becomes 3-D:
  1. georeference the scan as an ``ImagePlane`` (plan at a level elevation, or a
     vertical section between two surface points);
  2. trace the workings on the image in pixel coordinates;
  3. ``trace_to_world`` maps pixels through the image's georeference, and the
     constructors below fill in what the plan cannot show (a drift sits at its
     level elevation; an adit runs in from its portal at the surface; a shaft
     drops from its collar; a raise joins two levels).
Lengths on historic US maps are almost always in FEET; pass ``units_in='ft'``
and everything is converted to metres once, at the door.
"""
import math

from .model import LineSet, Mesh, PointSet, ImagePlane, farray, iarray

FT = 0.3048

TYPES = {
    'adit':      {'color': [255, 170, 40],  'width_m': 2.0, 'height_m': 2.2, 'desc': 'horizontal entry from surface'},
    'tunnel':    {'color': [255, 170, 40],  'width_m': 2.4, 'height_m': 2.4, 'desc': 'horizontal through-opening'},
    'drift':     {'color': [255, 210, 90],  'width_m': 1.8, 'height_m': 2.1, 'desc': 'horizontal working along the vein'},
    'crosscut':  {'color': [255, 230, 140], 'width_m': 1.8, 'height_m': 2.1, 'desc': 'horizontal working across the vein'},
    'shaft':     {'color': [255, 80, 80],   'width_m': 2.5, 'height_m': 2.5, 'desc': 'vertical/inclined opening from surface'},
    'winze':     {'color': [255, 120, 120], 'width_m': 1.8, 'height_m': 1.8, 'desc': 'internal shaft sunk from a level'},
    'raise':     {'color': [255, 140, 200], 'width_m': 1.8, 'height_m': 1.8, 'desc': 'internal opening driven upward'},
    'decline':   {'color': [240, 140, 60],  'width_m': 4.0, 'height_m': 4.0, 'desc': 'inclined ramp'},
    'stope':     {'color': [120, 200, 255], 'width_m': 2.0, 'height_m': 2.0, 'desc': 'mined-out ore volume'},
    'portal':    {'color': [255, 255, 255], 'width_m': 2.0, 'height_m': 2.0, 'desc': 'adit entrance'},
    'level':     {'color': [200, 200, 200], 'width_m': 1.8, 'height_m': 2.1, 'desc': 'level outline / datum'},
    'trench':    {'color': [190, 160, 120], 'width_m': 1.5, 'height_m': 1.0, 'desc': 'surface trench'},
    'pit':       {'color': [190, 160, 120], 'width_m': 3.0, 'height_m': 2.0, 'desc': 'prospect pit / open cut'},
    'unknown':   {'color': [180, 180, 180], 'width_m': 1.8, 'height_m': 2.0, 'desc': 'unclassified working'},
}

CONFIDENCE = ('surveyed', 'sketched', 'inferred', 'described', 'assumed')


def new_workings(name='workings', mine=''):
    ls = LineSet(name=name, role='workings', color=[255, 170, 40])
    ls.metadata['schema'] = 'nwmm-workings/1'
    ls.metadata['mine'] = mine
    ls.metadata['types'] = sorted(TYPES)
    return ls


def _feat(kind, name='', **kw):
    kind = kind if kind in TYPES else 'unknown'
    f = {'type': kind, 'name': name, 'level': kw.pop('level', ''), 'level_z': kw.pop('level_z', None),
         'mine': kw.pop('mine', ''), 'width_m': kw.pop('width_m', TYPES[kind]['width_m']),
         'height_m': kw.pop('height_m', TYPES[kind]['height_m']),
         'source': kw.pop('source', {}), 'confidence': kw.pop('confidence', 'sketched'),
         'units_in': kw.pop('units_in', 'm'), 'notes': kw.pop('notes', '')}
    f.update(kw)
    return f


def _m(v, units_in):
    return v * FT if units_in == 'ft' else v


# --------------------------------------------------------------- constructors
def add_adit(ws, portal_xyz, bearing_deg, length, grade_pct=0.0, units_in='m',
             terrain=None, name='adit', **attrs):
    """Adit driven from a portal: bearing clockwise from north, ``length``
    along the drive, ``grade_pct`` rise per 100 (adits usually +0.5 % for
    drainage).  With ``terrain`` (Grid2D) the portal Z snaps to ground."""
    L = _m(length, units_in)
    x, y, z = portal_xyz
    if terrain is not None:
        zt = terrain.sample(x, y)
        if zt == zt:
            z = zt
    b = math.radians(bearing_deg)
    end = (x + L * math.sin(b), y + L * math.cos(b), z + L * grade_pct / 100.0)
    k = ws.add_polyline([(x, y, z), end], _feat('adit', name, units_in=units_in, bearing=bearing_deg,
                                               length_m=L, **attrs))
    return k


def add_shaft(ws, collar_xyz, depth, dip_deg=90.0, azimuth_deg=0.0, units_in='m',
              terrain=None, name='shaft', kind='shaft', **attrs):
    """Shaft (or winze) sunk from a collar: ``dip_deg`` positive DOWN from
    horizontal (90 = vertical), ``azimuth_deg`` of the incline direction."""
    D = _m(depth, units_in)
    x, y, z = collar_xyz
    if terrain is not None and kind == 'shaft':
        zt = terrain.sample(x, y)
        if zt == zt:
            z = zt
    d, a = math.radians(dip_deg), math.radians(azimuth_deg)
    h = D * math.cos(d)
    bottom = (x + h * math.sin(a), y + h * math.cos(a), z - D * math.sin(d))
    return ws.add_polyline([(x, y, z), bottom], _feat(kind, name, units_in=units_in, depth_m=D,
                                                   dip=dip_deg, azimuth=azimuth_deg, **attrs))


def add_level_working(ws, xy_points, level_z, kind='drift', units_in='m', name='', **attrs):
    """Horizontal working traced in plan at a known level elevation.
    ``xy_points`` already in world metres (use trace_to_world first)."""
    pts = [(p[0], p[1], level_z) for p in xy_points]
    return ws.add_polyline(pts, _feat(kind, name, level_z=level_z, units_in=units_in, **attrs))


def add_raise(ws, lower_xyz, upper_xyz, name='raise', kind='raise', **attrs):
    return ws.add_polyline([tuple(lower_xyz), tuple(upper_xyz)], _feat(kind, name, **attrs))


def add_decline(ws, start_xyz, segments, units_in='m', name='decline', **attrs):
    """segments: list of (bearing_deg, length, grade_pct) legs."""
    pts = [tuple(start_xyz)]
    for bearing, length, grade in segments:
        L = _m(length, units_in)
        b = math.radians(bearing)
        x, y, z = pts[-1]
        pts.append((x + L * math.sin(b), y + L * math.cos(b), z + L * grade / 100.0))
    return ws.add_polyline(pts, _feat('decline', name, units_in=units_in, **attrs))


def stope_prism(outline_xy, z_bottom, z_top, name='stope', color=None, **attrs):
    """Mined-out volume: a closed outline (in plan, metres) extruded between
    two elevations.  Triangulated with an ear-clipping fan for simple polygons."""
    ring = [tuple(p[:2]) for p in outline_xy]
    if len(ring) > 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        raise ValueError('stope outline needs >= 3 points')
    if _signed_area(ring) < 0:
        ring.reverse()
    n = len(ring)
    verts = farray()
    for x, y in ring:
        verts.extend((x, y, z_bottom))
    for x, y in ring:
        verts.extend((x, y, z_top))
    tris = iarray()
    cap = _ear_clip(ring)
    for a, b, c in cap:
        tris.extend((a + n, b + n, c + n))         # top (CCW from above)
        tris.extend((a, c, b))                     # bottom (CCW from below)
    for i in range(n):
        j = (i + 1) % n
        tris.extend((i, j, j + n, i, j + n, i + n))
    m = Mesh(verts, tris, name=name, color=color or TYPES['stope']['color'], role='stope')
    m.metadata.update(_feat('stope', name, **attrs))
    m.metadata['z_bottom'] = z_bottom
    m.metadata['z_top'] = z_top
    return m


def _signed_area(ring):
    s = 0.0
    for i in range(len(ring)):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % len(ring)]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def _ear_clip(ring):
    """Triangulate a simple CCW polygon (list of (x, y)) -> index triples."""
    idx = list(range(len(ring)))
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 10000:
        guard += 1
        found = False
        for k in range(len(idx)):
            a, b, c = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            pa, pb, pc = ring[a], ring[b], ring[c]
            cross = (pb[0] - pa[0]) * (pc[1] - pa[1]) - (pb[1] - pa[1]) * (pc[0] - pa[0])
            if cross <= 0:
                continue                       # reflex vertex
            if any(_in_tri(ring[q], pa, pb, pc) for q in idx if q not in (a, b, c)):
                continue
            tris.append((a, b, c))
            idx.pop(k)
            found = True
            break
        if not found:                          # degenerate: fan the rest
            break
    if len(idx) >= 3:
        for k in range(1, len(idx) - 1):
            tris.append((idx[0], idx[k], idx[k + 1]))
    return tris


def _in_tri(p, a, b, c):
    def s(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = s(p, a, b), s(p, b, c), s(p, c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


# ----------------------------------------------------------- map -> world
def trace_to_world(image, pixel_points, level_z=None):
    """Map traced pixel coordinates through an ImagePlane's georeference.
    Plan images return (x, y, level_z or image.elevation); section images
    return (x, y, z) on the section plane."""
    out = []
    for px, py in pixel_points:
        x, y, z = image.pixel_to_world(px, py)
        if image.plane == 'plan':
            z = level_z if level_z is not None else (image.elevation if image.elevation is not None else 0.0)
        out.append((x, y, z))
    return out


def georef_plan_from_scale(image, anchor_px, anchor_world, scale_m_per_px, north_up=True,
                           rotation_deg=0.0, elevation=None):
    """Georeference a plan when only a scale bar and one known point are
    available: anchor pixel -> world, metres per pixel, optional rotation of
    the image's up direction clockwise from north."""
    px, py = anchor_px
    X, Y = anchor_world
    r = math.radians(-rotation_deg)
    c, s = math.cos(r), math.sin(r)
    # second control point 100 px to the right of the anchor
    dx, dy = 100.0 * scale_m_per_px * c, -100.0 * scale_m_per_px * s
    image.control = [[px, py, X, Y], [px + 100.0, py, X + dx, Y + dy]]
    if elevation is not None:
        image.elevation = elevation
    return image


def section_image(image_uri, width, height, p1, p2, z_top, z_bottom, name='section', **kw):
    """Convenience constructor for a vertical section scan."""
    return ImagePlane(image_uri, width, height, plane='section', p1=p1, p2=p2,
                      z_top=z_top, z_bottom=z_bottom, name=name, **kw)


def level_from_section(image, px_x, px_y):
    """Read the elevation of a level drawn on a section image at pixel row
    px_y (px_x only fixes the along-section position)."""
    return image.pixel_to_world(px_x, px_y)


# ------------------------------------------------------------- reporting
def summary(ws):
    """Lengths by type and by level for a workings LineSet."""
    by_type, by_level = {}, {}
    for k, f in enumerate(ws.features):
        L = ws.length(k)
        by_type[f.get('type', 'unknown')] = by_type.get(f.get('type', 'unknown'), 0.0) + L
        lv = f.get('level') or ('%.0f m' % f['level_z'] if f.get('level_z') is not None else 'unassigned')
        by_level[lv] = by_level.get(lv, 0.0) + L
    return {'n_features': len(ws.parts), 'total_m': ws.length(), 'by_type': by_type, 'by_level': by_level}


def to_geojson(ws, crs, to_lonlat=None):
    """2-D footprint of the workings as GeoJSON (WGS84) for the main map:
    each part -> LineString with its attributes; z kept as the 3rd ordinate.
    ``to_lonlat(x, y)`` converts projected -> (lon, lat); default uses the
    UTM CRS dict."""
    if to_lonlat is None:
        from leapfrog_export import utm_inv          # sibling pipeline module

        def to_lonlat(x, y):
            return utm_inv(x, y, crs['zone'], crs.get('north', True))
    feats = []
    for k, part in enumerate(ws.parts):
        coords = []
        for i in part:
            x, y, z = ws.vertex(i)
            lon, lat = to_lonlat(x, y)
            coords.append([round(lon, 7), round(lat, 7), round(z, 2)])
        props = dict(ws.features[k] if k < len(ws.features) else {})
        props['length_m'] = round(ws.length(k), 1)
        props['layer'] = 'workings'
        feats.append({'type': 'Feature', 'properties': props,
                      'geometry': {'type': 'LineString', 'coordinates': coords}})
    return {'type': 'FeatureCollection', 'features': feats,
            'properties': {'schema': 'nwmm-workings/1', 'mine': ws.metadata.get('mine', '')}}


def portals_points(ws):
    """Surface entries (first vertex of adits/shafts/declines) as a PointSet."""
    ps = PointSet(name='portals & collars', role='points', color=[255, 255, 255])
    for k, f in enumerate(ws.features):
        if f.get('type') in ('adit', 'tunnel', 'shaft', 'decline', 'portal'):
            x, y, z = ws.part_xyz(k)[0]
            ps.add(x, y, z, type=f['type'], name=f.get('name', ''))
    return ps
