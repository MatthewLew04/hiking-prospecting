"""geomodel.mapplate — the handoff for workings traced off a scanned plate.

Everything else in this feature reads *prose*, and prose can only ever give
``described`` geometry.  A level plan or a longitudinal section is different:
it was surveyed, and a working traced off a georeferenced one is entitled to
say so.  This module is the only path to ``surveyed`` confidence, and it is
deliberately narrow — you get it by supplying a georeference that can be
checked, not by asserting it.

The handoff has three parts, and an agent (or an operator with a tracing UI)
supplies all three:

  plate    the scan, its pixel size, and how pixels map to the ground:
           - plan:    >= 2 control points [px, py, lon, lat], or one anchor
                      plus a scale bar and a rotation
           - section: the map positions of the image's top-left and top-right
                      corners plus the elevations of its top and bottom edges
  where    the elevation the plan is drawn at — an absolute elevation, or the
           level label whose depth below the collar fixes it
  traces   pixel polylines, each with a workings type and a name

What comes back is spec elements carrying a world ``path`` rather than a
bearing and a length, at ``surveyed`` confidence, with the plate's citation
attached.  They merge into the same spec as the parsed prose, so one model can
mix a surveyed 300-level plan with a described adit and show the difference in
the drawing.

Missing information is a question, exactly as it is for prose: a plan with no
elevation and no level does not get draped at zero, it gets asked about.
Malformed input — a trace with one point, a type that is not a workings type —
is an error instead, because no answer would rescue it.
"""
import math
import os
import re
import sys

from .model import ImagePlane
from . import workings as wk

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINES = os.path.normpath(os.path.join(HERE, '..'))
if PIPELINES not in sys.path:
    sys.path.insert(0, PIPELINES)

from leapfrog_export import utm_zone, utm_fwd, utm_inv  # noqa: E402

PLATE_VERSION = 'nwmm-mapplate/1'

#: types a trace may claim; anything else is a mistake, not a question
TRACEABLE = tuple(sorted(k for k in wk.TYPES if k != 'stope'))

_SLUG = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


class PlateError(ValueError):
    """Malformed input.  No answer would rescue it, so it is not a gap."""


# ------------------------------------------------------------------ helpers
def _num(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlateError('%s must be a number, got %r' % (label, value))
    if value != value or value in (float('inf'), float('-inf')):
        raise PlateError('%s must be finite, got %r' % (label, value))
    return float(value)


def _lonlat(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PlateError('%s must be [lon, lat], got %r' % (label, value))
    lon, lat = _num(value[0], '%s lon' % label), _num(value[1], '%s lat' % label)
    if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        raise PlateError('%s is not a lon/lat pair: %r' % (label, value))
    return lon, lat


def _pixel(value, label, plate=None):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PlateError('%s must be [px, py], got %r' % (label, value))
    px, py = _num(value[0], '%s px' % label), _num(value[1], '%s py' % label)
    if plate is not None:
        # a generous margin: tracing slightly off the scan edge is normal,
        # tracing a screenful away from it is a units mistake
        if not -0.25 * plate['width'] <= px <= 1.25 * plate['width'] or \
                not -0.25 * plate['height'] <= py <= 1.25 * plate['height']:
            raise PlateError('%s %r is outside the %dx%d image; are these really pixels?'
                             % (label, value, plate['width'], plate['height']))
    return px, py


def _spread(points):
    x0, y0 = points[0]
    return max(math.hypot(px - x0, py - y0) for px, py in points)


def _collinear(points):
    """True when every point lies on one line — a georeference cannot be solved
    from those, because they fix no rotation."""
    if len(points) < 3:
        return False
    x0, y0 = points[0]
    span = _spread(points)
    if span <= 0.0:
        return True                               # all the same point
    far = max(points, key=lambda p: math.hypot(p[0] - x0, p[1] - y0))
    dx, dy = far[0] - x0, far[1] - y0
    for px, py in points:
        if abs((px - x0) * dy - (py - y0) * dx) > 1e-6 * span * span:
            return False
    return True


def _check_control(control):
    """Control points that no affine can be fitted to are a mistake to correct,
    not information that is missing, so they are an error rather than a gap."""
    pixels = [(c[0], c[1]) for c in control]
    world = [(c[2], c[3]) for c in control]
    if len(control) >= 2 and _spread(pixels) <= 0.0:
        raise PlateError('every control point is at the same pixel; they must be at '
                         'different places on the scan')
    if len(control) >= 2 and _spread(world) <= 0.0:
        raise PlateError('every control point maps to the same lon/lat; they must be at '
                         'different places on the ground')
    if _collinear(pixels) or _collinear(world):
        raise PlateError('the control points all lie on one line, so they fix scale but not '
                         'rotation. Move one of them well off that line, or use an anchor '
                         'plus a scale bar instead.')


def _identifier(value, label):
    if not isinstance(value, str) or not _SLUG.match(value):
        raise PlateError('%s must be a short identifier, got %r' % (label, value))
    return value


# --------------------------------------------------------------- validation
def validate_plate(plate):
    """Normalise a plate, or raise :class:`PlateError`.  What is *missing*
    rather than malformed is left for :func:`plate_gaps` to ask about."""
    if not isinstance(plate, dict):
        raise PlateError('a plate must be an object')
    out = {
        'plate_id': _identifier(plate.get('plate_id') or 'p1', 'plate_id'),
        'image': plate.get('image') or '',
        'width': int(_num(plate.get('width'), 'width')),
        'height': int(_num(plate.get('height'), 'height')),
        'plane': plate.get('plane') or 'plan',
        'source': dict(plate.get('source') or {}),
    }
    if out['plane'] not in ('plan', 'section'):
        raise PlateError('plane must be "plan" or "section", got %r' % (out['plane'],))
    if out['width'] <= 0 or out['height'] <= 0:
        raise PlateError('width and height are the scan\'s pixel size and must be positive')

    if out['plane'] == 'plan':
        control = plate.get('control')
        if control is not None:
            if not isinstance(control, (list, tuple)):
                raise PlateError('control must be a list of [px, py, lon, lat]')
            pts = []
            for i, c in enumerate(control):
                if not isinstance(c, (list, tuple)) or len(c) != 4:
                    raise PlateError('control[%d] must be [px, py, lon, lat], got %r' % (i, c))
                px, py = _pixel(c[:2], 'control[%d] pixel' % i, out)
                lon, lat = _lonlat(c[2:], 'control[%d] world' % i)
                pts.append([px, py, lon, lat])
            if len(pts) >= 2:
                _check_control(pts)
            out['control'] = pts
        anchor = plate.get('anchor')
        if anchor is not None:
            if not isinstance(anchor, dict):
                raise PlateError('anchor must be an object')
            out['anchor'] = {
                'px': list(_pixel(anchor.get('px'), 'anchor px', out)),
                'lonlat': list(_lonlat(anchor.get('lonlat'), 'anchor lonlat')),
                'scale_m_per_px': _num(anchor.get('scale_m_per_px'), 'anchor scale_m_per_px'),
                'rotation_deg': _num(anchor.get('rotation_deg', 0.0), 'anchor rotation_deg'),
            }
            if out['anchor']['scale_m_per_px'] <= 0:
                raise PlateError('anchor scale_m_per_px must be positive')
        if plate.get('elevation_m') is not None:
            out['elevation_m'] = _num(plate['elevation_m'], 'elevation_m')
        if plate.get('level'):
            out['level'] = str(plate['level'])
    else:
        for key in ('p1', 'p2'):
            if plate.get(key) is not None:
                out[key] = list(_lonlat(plate[key], key))
        if out.get('p1') and out.get('p2') and out['p1'] == out['p2']:
            raise PlateError('a section\'s top-left and top-right corners are the same '
                             'point, so the image covers no ground')
        for key in ('z_top', 'z_bottom'):
            if plate.get(key) is not None:
                out[key] = _num(plate[key], key)
        if 'z_top' in out and 'z_bottom' in out and out['z_top'] <= out['z_bottom']:
            raise PlateError('z_top (%s) must be above z_bottom (%s)'
                             % (out['z_top'], out['z_bottom']))
    return out


def validate_traces(plate, traces):
    """Normalise the traces, or raise :class:`PlateError`.

    A plate with no traces at all is valid: checking a georeference *before*
    tracing anything on it is the first thing anyone sensibly does."""
    if traces is None:
        traces = []
    if not isinstance(traces, (list, tuple)):
        raise PlateError('traces must be a list of {kind, points}, got %s'
                         % type(traces).__name__)
    seen, out = set(), []
    for i, t in enumerate(traces or ()):
        if not isinstance(t, dict):
            raise PlateError('traces[%d] must be an object' % i)
        tid = _identifier(t.get('id') or ('t%d' % (i + 1)), 'traces[%d].id' % i)
        if tid in seen:
            raise PlateError('duplicate trace id %r' % tid)
        seen.add(tid)
        kind = t.get('kind') or 'unknown'
        if kind not in TRACEABLE:
            raise PlateError('traces[%d].kind %r is not a workings type; choose from %s'
                             % (i, kind, ', '.join(TRACEABLE)))
        pts = t.get('points')
        if not isinstance(pts, (list, tuple)) or len(pts) < 2:
            raise PlateError('traces[%d] needs at least two points' % i)
        pixels = [list(_pixel(p, 'traces[%d].points[%d]' % (i, j), plate))
                  for j, p in enumerate(pts)]
        rec = {'id': tid, 'kind': kind, 'name': str(t.get('name') or ''), 'points': pixels}
        if t.get('level'):
            rec['level'] = str(t['level'])
        if t.get('elevation_m') is not None:
            rec['elevation_m'] = _num(t['elevation_m'], 'traces[%d].elevation_m' % i)
        out.append(rec)
    return out


def plate_gaps(plate, traces):
    """What the plate does not say, as questions in the same shape the parser
    and the builder use."""
    gaps = []
    pid = plate['plate_id']

    def gap(field, question, options=None, kind='plate'):
        gaps.append({'element': None, 'plate': pid, 'field': field, 'required': True,
                     'kind': kind, 'question': question,
                     'quote': citation(plate), 'span': None,
                     'options': (options or []) + [{'value': None,
                                                    'label': 'unknown — leave this plate out'}]})

    if plate['plane'] == 'plan':
        n = len(plate.get('control') or ())
        if n == 1 and not plate.get('anchor'):
            gap('control', 'Plate %s has one control point; a second is needed to fix scale '
                           'and rotation, or give an anchor with a scale bar instead.' % pid)
        elif n < 2 and not plate.get('anchor'):
            gap('control', 'Plate %s has no georeference. Give at least two control points '
                           '[px, py, lon, lat], or an anchor with a scale bar.' % pid)
        if plate.get('elevation_m') is None and not plate.get('level') and \
                not all(t.get('level') or t.get('elevation_m') is not None for t in traces):
            gap('elevation_m',
                'Plate %s is a plan, so it has no elevation of its own. Which level is it '
                'drawn at, or what elevation?' % pid,
                kind='plate_elevation')
    else:
        missing = [k for k in ('p1', 'p2', 'z_top', 'z_bottom') if plate.get(k) is None]
        if missing:
            gap(missing[0],
                'Plate %s is a section but is missing %s. Give the map positions of the '
                'image\'s top-left and top-right corners and the elevations of its top and '
                'bottom edges.' % (pid, ', '.join(missing)))
    if not traces:
        gaps.append({'element': None, 'plate': pid, 'field': 'traces', 'required': False,
                     'kind': 'plate', 'question': 'Plate %s is georeferenced but nothing has '
                                                  'been traced on it yet.' % pid,
                     'quote': citation(plate), 'span': None,
                     'options': [{'value': None, 'label': 'nothing to trace'}]})
    return gaps


def citation(plate):
    """A one-line provenance string; this is a traced element's ``quote``."""
    src = plate.get('source') or {}
    bits = [src.get('figure') or 'plate %s' % plate['plate_id']]
    if src.get('doc'):
        bits.append(src['doc'])
    if src.get('page'):
        bits.append('p. %s' % src['page'])
    return 'traced from ' + ', '.join(str(b) for b in bits)


# ----------------------------------------------------------- georeferencing
def _plan_control(plate):
    """The control points a plan is actually georeferenced from, or None.

    A single point fixes neither scale nor rotation, so it is not a
    georeference — which is why :func:`plate_gaps` stops asking as soon as an
    anchor is given and why its question offers the anchor as the answer.  An
    agent that takes that offer without deleting its lone point must get the
    anchor's scale, not a solve that cannot be done.  Everything that has to
    know which of the two was used asks here, so the zone, the solve and the
    provenance cannot disagree with the gap check or with each other."""
    control = plate.get('control') or ()
    return control if len(control) >= 2 else None


def _zone_for(plate):
    """The UTM zone the georeference is solved in.  Taken from the plate's own
    control geometry, so a plate is self-contained and does not depend on which
    mine it is later attached to."""
    if plate['plane'] == 'section':
        lon, lat = plate['p1']
    elif _plan_control(plate):
        lon, lat = plate['control'][0][2], plate['control'][0][3]
    else:
        lon, lat = plate['anchor']['lonlat']
    return utm_zone(lon, lat)


def image_plane(plate):
    """``(ImagePlane, zone, north)`` with the georeference solved in metres —
    a similarity fitted in degrees would be wrong, because a degree of
    longitude and a degree of latitude are not the same distance.

    Any failure in the affine solve is re-raised as a :class:`PlateError`, so a
    bad georeference reaches the caller as a correctable mistake rather than as
    an unhandled error."""
    try:
        return _image_plane(plate)
    except PlateError:
        raise
    except (ValueError, ZeroDivisionError, ArithmeticError) as exc:
        raise PlateError('the georeference for plate %s cannot be solved: %s'
                         % (plate.get('plate_id', '?'), exc))


def _image_plane(plate):
    control = _plan_control(plate)
    if plate['plane'] == 'plan' and control is None and not plate.get('anchor'):
        # plate_gaps asks about this rather than erroring, so it only reaches
        # here when a caller solves a plate it has not checked; say so instead
        # of failing later inside the affine with a bare ValueError.
        raise PlateError('plate %s has no georeference to solve: give at least two control '
                         'points, or an anchor with a scale bar' % plate['plate_id'])
    zone, north = _zone_for(plate)
    fwd = lambda lo, la: utm_fwd(lo, la, zone, north)
    name = plate.get('source', {}).get('figure') or 'plate %s' % plate['plate_id']

    if plate['plane'] == 'section':
        img = wk.section_image(plate['image'], plate['width'], plate['height'],
                               fwd(*plate['p1']), fwd(*plate['p2']),
                               plate['z_top'], plate['z_bottom'], name=name)
    else:
        img = ImagePlane(plate['image'], plate['width'], plate['height'], plane='plan',
                         name=name, elevation=plate.get('elevation_m'))
        if control:
            img.control = [[px, py] + list(fwd(lon, lat))
                           for px, py, lon, lat in control]
        else:
            a = plate['anchor']
            wk.georef_plan_from_scale(img, a['px'], fwd(*a['lonlat']),
                                      a['scale_m_per_px'], rotation_deg=a['rotation_deg'],
                                      elevation=plate.get('elevation_m'))
    img.provenance = dict(plate.get('source') or {})
    img.provenance['georeference'] = ('%d control points' % len(control)
                                      if control else 'anchor + scale bar')
    return img, zone, north


def scale_check(plate):
    """Metres per pixel implied by the georeference, and how far the control
    points disagree about it.  A plate whose points disagree badly was traced
    or tied wrongly, and that is worth saying out loud."""
    img, zone, north = image_plane(plate)
    if plate['plane'] == 'section':
        p1, p2 = utm_fwd(*plate['p1'], zone, north), utm_fwd(*plate['p2'], zone, north)
        return {'m_per_px': math.dist(p1, p2) / max(plate['width'], 1), 'residual_m': None}
    a, b, c, d, e, f = img.affine()
    m_per_px = math.sqrt(abs(a * e - b * d))
    residual = None
    if len(plate.get('control') or ()) >= 3:
        worst = 0.0
        for px, py, lon, lat in plate['control']:
            X, Y = utm_fwd(lon, lat, zone, north)
            gx, gy, _ = img.pixel_to_world(px, py)
            worst = max(worst, math.hypot(gx - X, gy - Y))
        residual = worst
    return {'m_per_px': m_per_px, 'residual_m': residual}


# ------------------------------------------------------------ spec elements
def element_id(plate_id, trace_id):
    """Traced ids are derived from the plate and trace they came from, not from
    a running count: re-attaching a plate must not renumber the prose elements
    that existing questions already refer to."""
    return 'e-%s-%s' % (plate_id, trace_id)


def traces_to_elements(plate, traces, first_index=1):
    """Traced polylines -> spec elements at ``surveyed`` confidence.

    Geometry is stored as lon/lat plus an elevation so the spec stays portable
    and independent of the mine it is attached to; the builder projects it into
    the model's own zone.  A plan trace whose elevation comes from a level
    label carries the label instead, and the builder resolves it against the
    collar like any other level."""
    img, zone, north = image_plane(plate)
    inv = lambda x, y: utm_inv(x, y, zone, north)
    quote = citation(plate)
    out = []
    for t in traces:
        level = t.get('level') or plate.get('level')
        elevation = t.get('elevation_m', plate.get('elevation_m'))
        path = []
        for px, py in t['points']:
            x, y, z = img.pixel_to_world(px, py)
            lon, lat = inv(x, y)
            if plate['plane'] == 'plan':
                z = elevation
            path.append([round(lon, 8), round(lat, 8),
                         None if z is None or z != z else round(z, 4)])
        el = {
            'id': element_id(plate['plate_id'], t['id']),
            'kind': t['kind'],
            'name': t['name'],
            'count': 1,
            'units_in': 'm',
            'defaults': [],
            'confidence': 'surveyed',
            'fields': {'path': 'surveyed'},
            'path': path,
            'plate': plate['plate_id'],
            'trace': t['id'],
            'quote': quote,
            'span': None,
            'source': dict(plate.get('source') or {}),
        }
        if level:
            el['level'] = level
            el['fields']['level'] = 'surveyed'
        if elevation is not None:
            el['elevation_m'] = elevation
        out.append(el)
    return out


def attach(spec, plates):
    """Fold validated plates and their traces into a parsed spec.

    Returns a new spec; the prose elements keep their ids and the traced ones
    are appended, so a model can mix a surveyed level plan with a described
    adit and the drawing can tell you which is which.
    """
    import json

    spec = json.loads(json.dumps(spec))
    validated = [(validate_plate(raw), raw) for raw in (plates or ())]
    incoming = set(p['plate_id'] for p, _ in validated)

    # Re-attaching a plate replaces it rather than adding it a second time, so
    # an agent that resends its plates while answering a question does not end
    # up with the workings traced twice.
    elements = [e for e in (spec.get('elements') or []) if e.get('plate') not in incoming]
    gaps = [g for g in (spec.get('gaps') or []) if g.get('plate') not in incoming]
    kept = [p for p in (spec.get('plates') or []) if p.get('plate_id') not in incoming]

    for plate, raw in validated:
        traces = validate_traces(plate, raw.get('traces'))
        pgaps = plate_gaps(plate, traces)
        gaps.extend(pgaps)
        if any(g['required'] for g in pgaps):
            kept.append(dict(plate, traces=traces, usable=False))
            continue
        elements.extend(traces_to_elements(plate, traces))
        kept.append(dict(plate, traces=traces, usable=True, scale=scale_check(plate)))
    kept.sort(key=lambda p: p['plate_id'])
    for i, g in enumerate(gaps, 1):
        g['id'] = 'g%d' % i
    spec['elements'] = elements
    spec['gaps'] = gaps
    spec['plates'] = kept
    spec['coverage']['elements'] = len(elements)
    spec['coverage']['plates'] = len(kept)
    spec['coverage']['traced_elements'] = sum(1 for e in elements if e.get('path'))
    spec['coverage']['unresolved'] = sum(1 for g in gaps if g['required'])
    spec['coverage']['questions'] = len(gaps)
    return spec
