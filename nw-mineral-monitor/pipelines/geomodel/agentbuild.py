"""geomodel.agentbuild — a parsed ``WorkingsSpec`` plus a resolved mine become
a 3-D ``Project``.

No new geometry code lives here.  Every line and prism is made by the existing
primitives in :mod:`geomodel.workings`; this module's whole job is *placement* —
deciding where in the world each described working starts, and refusing to
place one when the text does not say.

Placement rules, in the order they are applied (each one is recorded on the
element it placed, so ``manifest.json`` can be read back as an explanation):

1. **Collar.**  The portal/collar sits at the resolved mine's coordinate, with
   Z sampled from the terrain tile.  With no coordinate, or no terrain, nothing
   is built — an unplaced mine is a gap, not a mine at sea level.
2. **Levels are named for their depth below the collar** — the universal
   convention on US mine plans — so the "300 level" is 300 ft below the shaft
   collar.  A level with no depth in its name ("No. 3 level", "adit level")
   takes the adit's elevation if there is an adit, and is otherwise a gap.
3. **Level stations.**  A drift or crosscut on a level starts where that level
   meets the shaft; with no shaft it starts at the end of the adit that reaches
   it; with neither it starts on the collar's vertical.
4. **Shafts and adits start at the collar**, unless the text gives them their
   own ``from``.
5. **Raises and winzes** join two level stations, or run from one for a stated
   distance.
6. **Stopes** are a prism along the level's drift bearing: the stated length,
   the type's default width, and the stated back height.

Anything that cannot be placed under those rules produces a ``placement`` gap
naming the element and what is missing.  Nothing is nudged into position.
"""
import hashlib
import json
import math
import os
import sys

from .model import Project, PointSet, utm_crs, sanitize
from . import workings as wk

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINES = os.path.normpath(os.path.join(HERE, '..'))
if PIPELINES not in sys.path:
    sys.path.insert(0, PIPELINES)

from leapfrog_export import utm_zone, utm_fwd, utm_inv  # noqa: E402

BUILDER_VERSION = 'nwmm-agentbuild/1'

FT = wk.FT

#: shafts define the level stations, so they are placed before anything that
#: sits on a level; stopes come last because they hang off a drift.
ORDER = {'portal': 0, 'shaft': 1, 'adit': 2, 'tunnel': 2, 'decline': 3,
         'drift': 4, 'crosscut': 4, 'trench': 4, 'pit': 4,
         'winze': 5, 'raise': 5, 'stope': 6}


class Unplaceable(Exception):
    """Raised inside a placement rule; becomes a gap, never a guess."""


def build(spec, site, context=False, radius_m=1200.0, zoom=13, offline=False, log=print):
    """``(spec, resolved mine) -> {'project', 'workings', 'summary', ...}``.

    ``site`` is what :func:`geomodel.resolve.site` returns, or any dict with
    ``lon``, ``lat``, ``elevation_m`` and ``name``.  With ``context=True`` the
    workings are dropped into a full :func:`geomodel.kit.build_site_model`
    project (terrain, draped geology, grade points); that path fetches tiles
    and is why the service builds asynchronously.
    """
    lon, lat = site.get('lon'), site.get('lat')
    if lon is None or lat is None:
        raise Unplaceable('the mine has no coordinate: %s' % (site.get('name') or site.get('mine_id')))
    z0 = site.get('elevation_m')
    if z0 is None:
        raise Unplaceable('no collar elevation: the terrain tile for %.5f, %.5f is not available'
                          % (lon, lat))

    zone, north = utm_zone(lon, lat)
    cx, cy = utm_fwd(lon, lat, zone, north)
    collar = (cx, cy, float(z0))

    name = site.get('name') or spec.get('mine_id') or 'mine'
    if context:
        from . import kit
        proj = kit.build_site_model(lon, lat, radius_m=radius_m, name=name, zoom=zoom,
                                    offline=offline, log=log)
        proj.objects = [o for o in proj.objects if getattr(o, 'role', '') != 'workings']
    else:
        proj = Project(name, utm_crs(zone, north),
                       origin=[round(cx, -2), round(cy, -2), 0.0],
                       site={'name': name, 'lon': lon, 'lat': lat,
                             'utm_zone': '%d%s' % (zone, 'N' if north else 'S')})

    ws = wk.new_workings(name='workings (from description)', mine=name)
    ws.metadata['builder'] = BUILDER_VERSION
    ws.metadata['spec_id'] = spec.get('spec_id')
    ws.metadata['source_text_sha256'] = spec.get('text_sha256')

    ctx = {'collar': collar, 'levels': {}, 'shaft': None, 'adit': None,
           'by_id': {}, 'level_bearings': {}, 'warnings': []}
    _seed_levels(ctx, spec, collar)

    placed, gaps, stopes = [], [], []
    for el in sorted(spec.get('elements', []), key=lambda e: (ORDER.get(e['kind'], 9), e['id'])):
        try:
            record = _place(ws, el, ctx, spec, site, stopes)
        except Unplaceable as exc:
            gaps.append({'id': 'p%d' % (len(gaps) + 1), 'element': el['id'], 'field': 'placement',
                         'required': True, 'kind': 'placement',
                         'question': 'The %s (%s) cannot be placed: %s What should it be attached to?'
                                     % (el['kind'], el['id'], exc),
                         'quote': el.get('quote', ''), 'span': el.get('span'),
                         'options': _placement_options(ctx)})
            continue
        placed.append(record)

    mine_objects = list(stopes) + [ws]
    portals = wk.portals_points(ws)
    if portals.n:
        mine_objects.append(portals)
    for obj in mine_objects:
        proj.add(obj)
    _stabilise(mine_objects, spec)

    proj.metadata['generator'] = 'geomodel.agentbuild.build'
    proj.metadata['builder_version'] = BUILDER_VERSION
    proj.metadata['notes'] = [
        'Workings digitised from a written description, not from a survey.',
        'Confidence is per element: "described" = read off the source text, '
        '"assumed" = supplied in answer to a question the text left open.',
        'Levels are placed at their named depth below the collar.',
        'Never enter adits or shafts.']

    return {
        'project': proj,
        'workings': ws,
        'placed': placed,
        'gaps': gaps,
        'warnings': ctx['warnings'],
        'levels': dict((k, round(v, 3)) for k, v in ctx['levels'].items()),
        'collar': {'lon': lon, 'lat': lat, 'x': cx, 'y': cy, 'z': collar[2],
                   'elevation_source': site.get('elevation_source', '')},
        'crs': proj.crs,
        'summary': wk.summary(ws),
        'confidence': _tally(spec.get('elements', []), placed),
    }


# ------------------------------------------------------------------ levels
def _seed_levels(ctx, spec, collar):
    """Level label -> elevation (m).  A named depth is a depth below collar."""
    for label, depth in (spec.get('levels') or {}).items():
        if depth is not None:
            ctx['levels'][label] = collar[2] - float(depth)
    for el in spec.get('elements', []):
        if el.get('level') and el.get('level_depth_m') is not None:
            ctx['levels'].setdefault(el['level'], collar[2] - float(el['level_depth_m']))


def _level_z(ctx, label):
    if label in ctx['levels']:
        return ctx['levels'][label]
    if label in ('adit', 'tunnel', 'surface', 'main haulage', 'haulage') and ctx['adit']:
        return ctx['adit']['end'][2]
    raise Unplaceable('the elevation of the "%s" level is not stated and no adit fixes it.' % label)


def _station(ctx, label):
    """Where a level meets the workings: the shaft first, then the adit."""
    z = _level_z(ctx, label)
    sh = ctx['shaft']
    if sh:
        dv = sh['z0'] - z
        if dv < -1e-6:
            ctx['warnings'].append('the "%s" level is above the shaft collar' % label)
        else:
            if dv - sh['vertical'] > 1.0:
                ctx['warnings'].append('the "%s" level lies %.0f m below the bottom of the %s'
                                       % (label, dv - sh['vertical'], sh['name'] or 'shaft'))
            h = dv / math.tan(math.radians(sh['dip'])) if sh['dip'] < 89.999 else 0.0
            a = math.radians(sh['azimuth'])
            return (sh['x'] + h * math.sin(a), sh['y'] + h * math.cos(a), z), \
                'the %s at the %s level' % (sh['name'] or 'shaft', label)
    if ctx['adit'] and abs(ctx['adit']['end'][2] - z) < 1e-6:
        return ctx['adit']['end'], 'the end of the %s' % (ctx['adit']['name'] or 'adit')
    return (ctx['collar'][0], ctx['collar'][1], z), 'the collar vertical at the %s level' % label


# ------------------------------------------------------------------ placing
def _place(ws, el, ctx, spec, site, stopes):
    kind = el['kind']
    attrs = _attrs(el, spec, site)
    if kind in ('adit', 'tunnel'):
        return _place_adit(ws, el, ctx, attrs)
    if kind == 'decline':
        return _place_decline(ws, el, ctx, attrs)
    if kind in ('shaft', 'winze'):
        return _place_shaft(ws, el, ctx, attrs)
    if kind in ('drift', 'crosscut', 'trench'):
        return _place_level_working(ws, el, ctx, attrs)
    if kind == 'raise':
        return _place_raise(ws, el, ctx, attrs)
    if kind == 'stope':
        return _place_stope(el, ctx, attrs, stopes)
    if kind in ('portal', 'pit'):
        return _place_point_feature(ws, el, ctx, attrs)
    raise Unplaceable('%r is not a kind this builder can place.' % kind)


def _need(el, field, what):
    v = el.get(field)
    if v is None:
        raise Unplaceable('%s is not stated.' % what)
    return v


def _origin(el, ctx):
    """Where an element that starts from something else begins."""
    frm = el.get('from') or {}
    ref = frm.get('ref')
    if ref == 'level':
        return _station(ctx, frm['level'])
    if ref == 'shaft_bottom':
        if not ctx['shaft']:
            raise Unplaceable('it starts at the bottom of a shaft, but no shaft is described.')
        return ctx['shaft']['bottom'], 'the bottom of the %s' % (ctx['shaft']['name'] or 'shaft')
    if ref == 'shaft' and ctx['shaft']:
        # "on the 300 level a drift was extended ... from the shaft" means from
        # the shaft *at that level*, not from its collar 300 feet above
        if el.get('level'):
            return _station(ctx, el['level'])
        return (ctx['shaft']['x'], ctx['shaft']['y'], ctx['shaft']['z0']), 'the shaft collar'
    if ref in ('adit', 'tunnel') and ctx['adit']:
        return ctx['adit']['end'], 'the end of the adit'
    if el.get('level'):
        return _station(ctx, el['level'])
    return ctx['collar'], 'the collar'


def _place_adit(ws, el, ctx, attrs):
    bearing = _need(el, 'bearing_deg', 'a bearing')
    length = _need(el, 'length_m', 'a length')
    # an adit is a surface entry by definition: it starts at the collar unless
    # the text explicitly drives it from somewhere else.  "cuts the vein on the
    # 300 level" says where it ends up, not where it begins.
    start, how = _origin(el, ctx) if el.get('from') else (ctx['collar'], 'the collar')
    k = wk.add_adit(ws, start, bearing, length, grade_pct=0.5, units_in='m',
                    name=_label(el), **attrs)
    _stamp(ws, k, el)
    end = ws.part_xyz(k)[-1]
    if ctx['adit'] is None:
        ctx['adit'] = {'end': end, 'name': el.get('name', '')}
    ctx['levels'].setdefault('adit', end[2])
    ctx['by_id'][el['id']] = {'start': start, 'end': end}
    return _record(el, k, how, start, end, note='grade +0.5 % for drainage (definitional)')


def _place_decline(ws, el, ctx, attrs):
    bearing = _need(el, 'bearing_deg', 'a bearing')
    length = _need(el, 'length_m', 'a length')
    grade = el.get('dip_deg')
    grade_pct = -100.0 * math.tan(math.radians(grade)) if grade else -15.0
    start, how = _origin(el, ctx)
    k = wk.add_decline(ws, start, [(bearing, length, grade_pct)], units_in='m',
                       name=_label(el), **attrs)
    _stamp(ws, k, el)
    end = ws.part_xyz(k)[-1]
    ctx['by_id'][el['id']] = {'start': start, 'end': end}
    return _record(el, k, how, start, end,
                   note=None if grade else 'no gradient stated; -15 % assumed for a ramp')


def _place_shaft(ws, el, ctx, attrs):
    depth = _need(el, 'depth_m', 'a depth')
    dip = el.get('dip_deg')
    if dip is None:
        raise Unplaceable('it is an incline with no stated angle.')
    if dip < 89.999 and el.get('bearing_deg') is None:
        raise Unplaceable('it is an incline with no stated direction.')
    azimuth = el.get('bearing_deg') or 0.0
    if el['kind'] == 'winze':
        start, how = _origin(el, ctx)
    else:
        start, how = ctx['collar'], 'the collar'
    k = wk.add_shaft(ws, start, depth, dip_deg=dip, azimuth_deg=azimuth, units_in='m',
                     kind=el['kind'], name=_label(el), level=el.get('level', ''),
                     level_z=start[2] if el['kind'] == 'winze' else None, **attrs)
    _stamp(ws, k, el)
    bottom = ws.part_xyz(k)[-1]
    if el['kind'] == 'shaft' and ctx['shaft'] is None:
        ctx['shaft'] = {'x': start[0], 'y': start[1], 'z0': start[2], 'dip': dip,
                        'azimuth': azimuth, 'vertical': depth * math.sin(math.radians(dip)),
                        'bottom': bottom, 'name': el.get('name', '')}
    ctx['by_id'][el['id']] = {'start': start, 'end': bottom}
    return _record(el, k, how, start, bottom)


def _place_level_working(ws, el, ctx, attrs):
    bearing = _need(el, 'bearing_deg', 'a bearing')
    length = _need(el, 'length_m', 'a length')
    if el['kind'] == 'trench':
        start, how = ctx['collar'], 'the collar'
    else:
        start, how = _origin(el, ctx)
    b = math.radians(bearing)
    end = (start[0] + length * math.sin(b), start[1] + length * math.cos(b), start[2])
    k = wk.add_level_working(ws, [start[:2], end[:2]], start[2], kind=el['kind'],
                             units_in=el.get('units_in', 'ft'), name=_label(el),
                             level=el.get('level', ''), bearing=bearing, length_m=length, **attrs)
    _stamp(ws, k, el)
    ctx['by_id'][el['id']] = {'start': start, 'end': end}
    if el.get('level'):
        ctx['level_bearings'].setdefault(el['level'], bearing)
    return _record(el, k, how, start, end)


def _place_raise(ws, el, ctx, attrs):
    conn = el.get('connects')
    if conn:
        lower, how_l = _station(ctx, conn[0])
        upper, how_u = _station(ctx, conn[1])
        if lower[2] > upper[2]:
            lower, upper = upper, lower
            how_l, how_u = how_u, how_l
        how = '%s up to %s' % (how_l, how_u)
    else:
        length = _need(el, 'length_m', 'a length')
        if not el.get('level'):
            raise Unplaceable('it has no level to start from.')
        lower, how_l = _station(ctx, el['level'])
        upper = (lower[0], lower[1], lower[2] + length)
        how = '%s, driven %0.1f m upward' % (how_l, length)
    k = wk.add_raise(ws, lower, upper, kind=el['kind'], name=_label(el),
                     level=el.get('level') or (conn[0] if conn else ''), level_z=lower[2],
                     units_in=el.get('units_in', 'ft'), **attrs)
    _stamp(ws, k, el)
    ctx['by_id'][el['id']] = {'start': lower, 'end': upper}
    return _record(el, k, how, lower, upper)


def _place_stope(el, ctx, attrs, stopes):
    length = _need(el, 'length_m', 'a length')
    if not el.get('level'):
        raise Unplaceable('it has no level, so its elevation is unknown.')
    start, how = _station(ctx, el['level'])
    bearing = el.get('bearing_deg')
    if bearing is None:
        bearing = _dominant_bearing(ctx, el['level'])
    if bearing is None:
        raise Unplaceable('no bearing is stated and no drift on that level gives one.')
    height = el.get('height_m')
    if height is None:
        raise Unplaceable('no back height is stated, so it has no vertical extent.')
    width = wk.TYPES['stope']['width_m']
    b = math.radians(bearing)
    ux, uy = math.sin(b), math.cos(b)
    px, py = math.cos(b), -math.sin(b)
    ring = [(start[0] + px * width / 2, start[1] + py * width / 2),
            (start[0] + ux * length + px * width / 2, start[1] + uy * length + py * width / 2),
            (start[0] + ux * length - px * width / 2, start[1] + uy * length - py * width / 2),
            (start[0] - px * width / 2, start[1] - py * width / 2)]
    mesh = wk.stope_prism(ring, start[2], start[2] + height, name=_label(el) or 'stope',
                          level=el.get('level', ''), units_in=el.get('units_in', 'ft'), **attrs)
    stopes.append(mesh)
    ctx['by_id'][el['id']] = {'start': start, 'end': (start[0] + ux * length, start[1] + uy * length,
                                                      start[2] + height)}
    return _record(el, None, '%s, %.0f m along %03.0f°' % (how, length, bearing),
                   start, ctx['by_id'][el['id']]['end'],
                   note='stope width %.1f m is the schema default, not a stated figure' % width)


def _place_point_feature(ws, el, ctx, attrs):
    length = el.get('length_m') or wk.TYPES[el['kind']]['width_m']
    start = ctx['collar']
    end = (start[0] + length, start[1], start[2])
    feat = {'type': el['kind'], 'name': _label(el), 'level': '', 'level_z': None, 'mine': '',
            'units_in': el.get('units_in', 'ft'),
            'width_m': wk.TYPES[el['kind']]['width_m'],
            'height_m': wk.TYPES[el['kind']]['height_m']}
    feat.update(attrs)                            # provenance wins over the defaults
    k = ws.add_polyline([start, end], feat)
    ctx['by_id'][el['id']] = {'start': start, 'end': end}
    return _record(el, k, 'the collar', start, end,
                   note='drawn as its stated size in plan; the text gives no outline')


def _dominant_bearing(ctx, level):
    """A stope with no bearing of its own follows the drift it was mined from,
    if one was described on the same level.  Otherwise it stays unplaced."""
    return ctx['level_bearings'].get(level)


# ------------------------------------------------------------------ records
def _stamp(ws, k, el):
    """Geometry is always metres; the feature records what the *source* used,
    which is what a reader needs in order to check the arithmetic."""
    f = ws.features[k]
    f['units_in'] = el.get('units_in', 'ft')
    # only a working that *sits on* a level gets a level elevation; an adit
    # spans elevations and merely reaches one.
    if el.get('level') and f.get('level_z') is None and el['kind'] in (
            'drift', 'crosscut', 'winze', 'raise', 'stope'):
        f['level_z'] = ws.part_xyz(k)[0][2]
    return k


def _label(el):
    return (el.get('name') or '').strip() or el['kind']


def _attrs(el, spec, site):
    """Provenance carried onto every feature: the quote, the span, the
    per-field confidence, and the citation of the mine it belongs to."""
    return {
        'confidence': el.get('confidence', 'described'),
        'element_id': el['id'],
        'quote': el.get('quote', ''),
        'span': el.get('span'),
        'fields': dict(el.get('fields') or {}),
        'defaults': list(el.get('defaults') or []),
        'source': {'doc': site.get('source', ''), 'url': site.get('source_url', ''),
                   'mine': site.get('name', ''), 'mine_id': site.get('mine_id', ''),
                   'spec_id': spec.get('spec_id', '')},
        'notes': 'digitised from a written description; not a survey',
    }


def _record(el, part, how, start, end, note=None):
    rec = {'element': el['id'], 'kind': el['kind'], 'name': el.get('name', ''),
           'part': part, 'placement': how, 'confidence': el.get('confidence', 'described'),
           'start': [round(v, 3) for v in start], 'end': [round(v, 3) for v in end]}
    if note:
        rec['note'] = note
    return rec


def _tally(elements, placed):
    out = {'surveyed': 0, 'described': 0, 'assumed': 0}
    ids = set(p['element'] for p in placed)
    for el in elements:
        if el['id'] in ids:
            out[el.get('confidence', 'described')] = out.get(el.get('confidence', 'described'), 0) + 1
    return out


def _placement_options(ctx):
    opts = [{'value': lv, 'label': 'the %s level' % lv} for lv in sorted(ctx['levels'])]
    if ctx['shaft']:
        opts.append({'value': 'shaft', 'label': 'the shaft collar'})
    if ctx['adit']:
        opts.append({'value': 'adit', 'label': 'the end of the adit'})
    opts.append({'value': None, 'label': 'unknown — omit this element'})
    return opts


def _stabilise(objects, spec):
    """Give this build's own objects content-derived ids.  ``ModelObject``
    normally stamps a counter and the wall clock into the id, which would make
    two builds of one description differ for no reason; the objects a *context*
    build inherits from ``kit`` keep their own ids, because ``StratModel``
    references them by string."""
    tag = spec.get('spec_id') or 'spec'
    for i, obj in enumerate(objects):
        obj.id = '%s-%s-%d' % (obj.kind, tag, i)
    return objects


def stable_bytes(proj):
    """The project's geometry payload with the wall-clock fields dropped: what
    content addressing and the "has anything actually changed?" check in
    ``publish`` are computed over."""
    d = sanitize(proj.to_json())
    d.pop('created', None)
    d.pop('modified', None)
    return json.dumps(d, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def content_sha256(proj):
    return hashlib.sha256(stable_bytes(proj)).hexdigest()


# ------------------------------------------------------------------ exports
def to_lonlat_fn(crs):
    zone, north = crs.get('zone'), crs.get('north', True)
    return lambda x, y: utm_inv(x, y, zone, north)


def write_exports(built, out_dir, formats=('json', 'omf2', 'dxf', 'geojson')):
    """Write the interchange files the tool hands back.  Returns a manifest
    ``{name: description}`` in a stable order."""
    from .formats import omf2, dxf
    import json as _json

    os.makedirs(out_dir, exist_ok=True)
    proj, ws = built['project'], built['workings']
    out = []
    if 'json' in formats:
        p = os.path.join(out_dir, 'model.geomodel.json')
        proj.save(p)
        out.append(('model.geomodel.json', 'model3d.html project — open with ?project=<url>'))
    if 'omf2' in formats:
        p = os.path.join(out_dir, 'model.omf')
        omf2.write_omf2(proj, p, name=proj.name,
                        description='NW Mineral Monitor — workings digitised from a description')
        out.append(('model.omf', 'OMF v2.0 — Leapfrog Geo 2025.1+, Seequent Evo'))
    if 'dxf' in formats:
        p = os.path.join(out_dir, 'workings.dxf')
        dxf.write_dxf(proj.by_kind('mesh') + proj.by_kind('lineset'), p)
        out.append(('workings.dxf', 'DXF R12 — 3-D polylines + stope solids'))
    if 'geojson' in formats:
        p = os.path.join(out_dir, 'workings.geojson')
        gj = wk.to_geojson(ws, proj.crs, to_lonlat=to_lonlat_fn(proj.crs))
        with open(p, 'w', encoding='utf-8') as fh:
            _json.dump(gj, fh, sort_keys=True, separators=(',', ':'))
        out.append(('workings.geojson', 'WGS84 footprint for the main map'))
    return out
