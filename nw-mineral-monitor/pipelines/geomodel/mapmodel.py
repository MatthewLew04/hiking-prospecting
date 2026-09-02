"""geomodel.mapmodel — Model From Map, reduced to what a geological map
draped on a DEM can honestly say (GEOMODEL.md §7).  The Python twin of the
``unitOrder`` / ``sharedContacts`` / ``dipOffsets`` / ``buildFromMap``
block of site/assets/geomodel/gm-engine.js, cross-checked in
tools/test_gm_engine.mjs.

* ``unit_order``      the stack from the units' ages (youngest first);
                      unaged units go last, in map order, and are reported;
                      units with the same ages are reported as ties
* ``shared_contacts`` where a unit's draped outline meets an older unit's
                      outline (distance to the older outline's segments) —
                      that is the younger unit's base cropping out.
                      Vertices within one topo cell of the model box edge
                      are clipping artefacts and are skipped
* ``dip_offsets``     one extra point a fixed distance down dip of each
                      contact, from the nearest orientation derived from
                      the traces themselves, recording which reading it was
* ``build_from_map``  a→c into the unit list ``stratigraphy.build_stratigraphy``
                      takes: each unit's base is a PointSet of its contacts
                      plus offsets, contact 'deposit', the oldest unit is
                      basement with no base

Nothing is defaulted: a unit that touches nothing older, or has fewer than
three contacts, is skipped and named; a map with no derivable dip yields
heightfields through the contacts, labelled ``NO_DIP_WARNING``; readings
derived along FAULT traces are never used as bedding dips.  Everything
produced carries provenance {method: 'model from map', confidence:
'inferred'}.
"""
import math

from .model import PointSet, NAN
from .interp import _GridIndex

MAPMODEL_METHOD = 'model from map'
MAPMODEL_DEFAULTS = {'tol': None, 'radius': 300.0, 'offset': 100.0, 'min_contacts': 3}
NO_DIP_WARNING = 'no dip information — bases follow the mapped contacts at the surface'
DEFAULT_COLORS = [[222, 184, 135], [205, 133, 63], [160, 160, 200], [120, 170, 120],
                  [200, 120, 120], [190, 190, 100], [100, 150, 190], [170, 120, 170],
                  [140, 140, 140], [230, 200, 150]]


def _finite_or_none(v):
    if v is None or v == '':
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dip_vector(dip, dip_azimuth):
    """Unit vector down the dip line (the JS dipVector)."""
    d, a = math.radians(dip), math.radians(dip_azimuth)
    cd = math.cos(d)
    return (math.sin(a) * cd, math.cos(a) * cd, -math.sin(d))


def _label(u, i):
    name = u.get('name') if isinstance(u, dict) else None
    if name is not None and name != '':
        return str(name)
    uid = u.get('id') if isinstance(u, dict) else None
    return str(uid) if uid is not None else 'unit %d' % i


def _age_key(u):
    t0, t1 = _finite_or_none(u.get('t0')), _finite_or_none(u.get('t1'))
    if t0 is None and t1 is None:
        return None
    return (t0 if t1 is None else t1, t1 if t0 is None else t0)


def _round_key(x, y):
    return (math.floor(x * 1000 + 0.5), math.floor(y * 1000 + 0.5))


def _py_round(v, nd):
    return round(float(v), nd)


# ---------------------------------------------------------------- order
def unit_order(units):
    """Order units youngest first by t1 (younger Ma) then t0 (older Ma).
    Returns {'order', 'aged', 'unaged', 'unaged_names', 'ties', 'keys',
    'warnings'} — indices into ``units``; ties are groups of names."""
    units = list(units or [])
    keys = [_age_key(u) for u in units]
    aged = [i for i, k in enumerate(keys) if k is not None]
    unaged = [i for i, k in enumerate(keys) if k is None]
    aged.sort(key=lambda i: (keys[i][0], keys[i][1], i))
    ties = []
    p = 0
    while p < len(aged):
        q = p + 1
        while q < len(aged) and keys[aged[q]] == keys[aged[p]]:
            q += 1
        if q - p > 1:
            ties.append([_label(units[i], i) for i in aged[p:q]])
        p = q
    warnings = []
    unaged_names = [_label(units[i], i) for i in unaged]
    if unaged:
        n = len(unaged)
        warnings.append('%d unit%s without an age (%s) — placed last, in map order, and not modelled: nothing says where %s in the sequence'
                        % (n, 's' if n > 1 else '', ', '.join(unaged_names), 'they sit' if n > 1 else 'it sits'))
    for t in ties:
        warnings.append('same ages, order between them unknown: %s — neither is treated as older than the other' % ' / '.join(t))
    return {'order': aged + unaged, 'aged': aged, 'unaged': unaged, 'unaged_names': unaged_names,
            'ties': ties, 'keys': keys, 'warnings': warnings}


# ------------------------------------------------------------- contacts
def _seg_dist2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = ((px - ax) * dx + (py - ay) * dy) / l2 if l2 > 0 else 0.0
    if t < 0:
        t = 0.0
    elif t > 1:
        t = 1.0
    qx, qy = ax + t * dx - px, ay + t * dy - py
    return qx * qx + qy * qy


class _SegmentHash(object):
    """Spatial hash of a LineSet's plan segments in cells of ``cell`` m."""

    def __init__(self, ls, cell):
        self.cell = float(cell)
        self.segs = []
        self.cells = {}
        for part in ls.parts:
            for k in range(len(part) - 1):
                a, b = ls.vertex(part[k]), ls.vertex(part[k + 1])
                if any(v != v for v in (a[0], a[1], b[0], b[1])):
                    continue
                s = len(self.segs)
                self.segs.append((a[0], a[1], b[0], b[1]))
                ix0, ix1 = int(math.floor(min(a[0], b[0]) / cell)), int(math.floor(max(a[0], b[0]) / cell))
                iy0, iy1 = int(math.floor(min(a[1], b[1]) / cell)), int(math.floor(max(a[1], b[1]) / cell))
                for ix in range(ix0, ix1 + 1):
                    for iy in range(iy0, iy1 + 1):
                        self.cells.setdefault((ix, iy), []).append(s)

    def near(self, px, py, tol):
        ix, iy = int(math.floor(px / self.cell)), int(math.floor(py / self.cell))
        best = math.inf
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for s in self.cells.get((ix + dx, iy + dy), ()):
                    g = self.segs[s]
                    d2 = _seg_dist2(px, py, g[0], g[1], g[2], g[3])
                    if d2 < best:
                        best = d2
        return math.sqrt(best) if best <= tol * tol else None


def _inner_box(topo, box):
    if box is not None and len(box) >= 4:
        return [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
    if topo is None:
        return None
    return [topo.x0 + topo.dx, topo.y0 + topo.dy, topo.xmax - topo.dx, topo.ymax - topo.dy]


def shared_contacts(outline_a, outline_b, tol, topo=None, box=None, against=None, name=None, color=None):
    """The vertices of outline A within ``tol`` of outline B's line (distance
    to B's segments).  Vertices within one topo cell of the model box edge
    are skipped; z is resampled from ``topo`` (vertices over no-data are
    skipped and counted).  Returns {'points', 'count', 'edge_skipped',
    'nodata', 'tol'}."""
    tol = float(tol)
    if not tol > 0:
        raise ValueError('shared_contacts: tol must be > 0')
    if outline_a.kind != 'lineset' or outline_b.kind != 'lineset':
        raise TypeError('shared_contacts: two LineSets are needed')
    ibox = _inner_box(topo, box)
    h = _SegmentHash(outline_b, tol)
    against = str(against) if against is not None else (outline_b.name or '')
    points = PointSet(name=name or '%s / %s contacts' % (outline_a.name or 'unit', outline_b.name or 'older unit'),
                      role='contacts', color=color or outline_a.color)
    seen = set()
    edge = nodata = 0
    for k, part in enumerate(outline_a.parts):
        feat = outline_a.features[k] if k < len(outline_a.features) else {}
        if feat.get('unit') is not None:
            unit = str(feat['unit'])
        elif feat.get('unit_id') is not None:
            unit = str(feat['unit_id'])
        else:
            unit = outline_a.name or ''
        for i in part:
            v = outline_a.vertex(i)
            x, y = v[0], v[1]
            if x != x or y != y:
                continue
            if ibox is not None and (x <= ibox[0] or x >= ibox[2] or y <= ibox[1] or y >= ibox[3]):
                edge += 1
                continue
            d = h.near(x, y, tol)
            if d is None:
                continue
            key = _round_key(x, y)
            if key in seen:
                continue
            z = v[2]
            if topo is not None:
                z = topo.sample(x, y)
                if z != z:
                    nodata += 1
                    continue
            seen.add(key)
            points.add(x, y, z, kind='contact', unit=unit, against=against, part=k, distance_m=_py_round(d, 3))
    points.metadata['contact'] = {'tol': tol, 'against': against, 'edge_skipped': edge, 'nodata': nodata}
    points.metadata['derived_from'] = [outline_a.id, outline_b.id] + ([topo.id] if topo is not None else [])
    points.provenance = {'method': MAPMODEL_METHOD, 'confidence': 'inferred',
                         'inputs': [outline_a.name, outline_b.name] + ([topo.name] if topo is not None else []),
                         'step': 'shared contacts', 'tol_m': tol}
    return {'points': points, 'count': points.n, 'edge_skipped': edge, 'nodata': nodata, 'tol': tol}


# ---------------------------------------------------------------- dips
def _readings_of(structural, exclude_sources=None):
    if structural is None:
        layers = []
    elif isinstance(structural, (list, tuple)):
        layers = list(structural)
    else:
        layers = [structural]
    layers = [o for o in layers if o is not None and getattr(o, 'kind', None) == 'points' and o.n > 0]
    ex = set(str(s) for s in (exclude_sources or []))
    R = {'pts': [], 'dip': [], 'az': [], 'src': [], 'part': [], 'layer': [], 'row': [], 'layers': [o.id for o in layers], 'excluded': 0}
    for ps in layers:
        D, Az = ps.numeric('dip'), ps.numeric('dip_azimuth')
        S, Pt = ps.attributes.get('source', []), ps.attributes.get('part', [])
        for i in range(ps.n):
            d, a = D[i], Az[i]
            x, y, z = ps.point(i)
            if d != d or a != a or x != x or y != y or z != z:
                continue
            src = S[i] if i < len(S) else None
            if src is not None and str(src) in ex:
                R['excluded'] += 1
                continue
            R['pts'].append((x, y, z))
            R['dip'].append(d)
            R['az'].append(a)
            R['src'].append(None if src is None else str(src))
            R['part'].append(Pt[i] if i < len(Pt) else None)
            R['layer'].append(ps.name or ps.id)
            R['row'].append(i)
    R['n'] = len(R['dip'])
    return R


def dip_offsets(contact_pts, structural, radius=None, offset=None, exclude_sources=None):
    """For each contact point find the nearest structural reading within
    ``radius`` (default 300 m) and add one point ``offset`` (default 100 m)
    down its dip vector, recording which reading it came from.  Returns
    {'points' (the offsets only), 'used', 'unmatched', 'readings', 'excluded'}."""
    radius = float(MAPMODEL_DEFAULTS['radius'] if radius is None else radius)
    offset = float(MAPMODEL_DEFAULTS['offset'] if offset is None else offset)
    if not radius > 0 or not offset > 0:
        raise ValueError('dip_offsets: radius and offset must be > 0')
    R = _readings_of(structural, exclude_sources)
    out = PointSet(name=(contact_pts.name or 'contacts') + ' — dip offsets', role='contacts', color=contact_pts.color)
    # cells no smaller than the search radius: a single reading would otherwise give a 2 m cell and a radius search that walks hundreds of rings
    index = _GridIndex(R['pts'], dim=3, cell=radius) if R['n'] else None
    used = unmatched = 0
    for i in range(contact_pts.n):
        x, y, z = contact_pts.point(i)
        if x != x or y != y or z != z:
            unmatched += 1
            continue
        found = index.nearest((x, y, z), 1, radius=radius) if index is not None else []
        if not found:
            unmatched += 1
            continue
        d, j = found[0]
        v = _dip_vector(R['dip'][j], R['az'][j])
        out.add(x + offset * v[0], y + offset * v[1], z + offset * v[2],
                kind='offset', contact=i, dip=R['dip'][j], dip_azimuth=R['az'][j], reading_layer=R['layer'][j],
                reading_row=R['row'][j], reading_source=R['src'][j], reading_part=R['part'][j],
                reading_distance_m=_py_round(d, 2), offset_m=offset)
        used += 1
    out.metadata['dip_offsets'] = {'radius_m': radius, 'offset_m': offset, 'used': used, 'unmatched': unmatched,
                                   'readings': R['n'], 'readings_excluded': R['excluded']}
    out.metadata['derived_from'] = [contact_pts.id] + R['layers']
    out.provenance = {'method': MAPMODEL_METHOD, 'confidence': 'inferred', 'inputs': [contact_pts.name] + R['layers'],
                      'step': 'dip offsets', 'radius_m': radius, 'offset_m': offset}
    return {'points': out, 'used': used, 'unmatched': unmatched, 'readings': R['n'], 'excluded': R['excluded']}


def _append_points(dst, src, seen=None):
    added = 0
    for i in range(src.n):
        x, y, z = src.point(i)
        if seen is not None:
            key = _round_key(x, y)
            if key in seen:
                continue
            seen.add(key)
        attrs = {}
        for k, col in src.attributes.items():
            attrs[k] = col[i] if i < len(col) else None
        dst.add(x, y, z, **attrs)
        added += 1
    return added


# --------------------------------------------------------------- build
def build_from_map(topo, units, faults=None, structural=None, opts=None, on_progress=None):
    """unit_order → shared_contacts → dip_offsets into the unit list
    ``stratigraphy.build_stratigraphy`` takes.

    units: [{'id', 'name', 't0', 't1', 'color', 'lithology', 'outline': LineSet}]
    faults: LineSet or list (recorded and warned about, never honoured —
            readings derived along them are excluded from the dip search)
    structural: PointSet or list with dip / dip_azimuth (+ source / part)
    opts: {'tol', 'radius', 'offset', 'min_contacts'}
    Returns {'units', 'stats', 'warnings'}: each unit carries base (a
    PointSet of contacts + offsets, or None for the basement), provenance,
    derived_from and its own warnings."""
    if topo is None or getattr(topo, 'kind', None) != 'grid2d':
        raise ValueError('build_from_map: a topography grid is required (contacts take their elevation from it)')
    o = dict(MAPMODEL_DEFAULTS)
    o.update(opts or {})
    tol = float(o['tol']) if o.get('tol') is not None and float(o['tol']) > 0 else max(topo.dx, topo.dy)
    radius, offset = float(o['radius']), float(o['offset'])
    min_contacts = max(1, int(o['min_contacts']))
    units = [dict(u) for u in (units or [])]
    for i, u in enumerate(units):
        ol = u.get('outline')
        if ol is None or getattr(ol, 'kind', None) != 'lineset':
            raise ValueError('build_from_map: unit %s has no outline LineSet' % _label(u, i))
    if faults is None:
        faults = []
    elif not isinstance(faults, (list, tuple)):
        faults = [faults]
    faults = [f for f in faults if f is not None and getattr(f, 'kind', None) == 'lineset']
    fault_names = [f.name for f in faults if f.name]
    if structural is None:
        structural = []
    elif not isinstance(structural, (list, tuple)):
        structural = [structural]
    structural = [s for s in structural if s is not None and getattr(s, 'kind', None) == 'points']
    R = _readings_of(structural, fault_names)
    ord_ = unit_order(units)
    warnings = list(ord_['warnings'])
    aged = ord_['aged']
    stats = {
        'ordered': [_label(units[i], i) for i in aged], 'unaged': ord_['unaged_names'], 'ties': ord_['ties'],
        'contacts_per_unit': {}, 'offsets_per_unit': {}, 'dips_used': 0, 'units_without_dip': [], 'units_modelled': [], 'basement': None,
        'rejected': {'no_age': list(ord_['unaged_names']), 'no_contacts': [], 'few_contacts': [], 'edge_vertices': 0, 'nodata': 0,
                     'readings_excluded': R['excluded']},
        'readings': R['n'], 'no_dip': False, 'faults_ignored': len(faults), 'tol': tol, 'radius': radius, 'offset': offset,
        'min_contacts': min_contacts,
    }
    if faults:
        nf, ne = len(faults), R['excluded']
        warnings.append('%d mapped fault trace%s not honoured: the bases are continuous across %s (fault blocks are not modelled), and %d reading%s derived along fault traces %s left out of the dip search'
                        % (nf, 's are' if nf > 1 else ' is', 'them' if nf > 1 else 'it', ne, '' if ne == 1 else 's', 'was' if ne == 1 else 'were'))
    if not R['n']:
        stats['no_dip'] = True
        warnings.append(NO_DIP_WARNING)
    out = []
    for p, ui in enumerate(aged):
        u = units[ui]
        name = _label(u, ui)
        key = ord_['keys'][ui]
        base = {'id': u.get('id'), 'name': name, 'color': u.get('color') or DEFAULT_COLORS[p % len(DEFAULT_COLORS)],
                'lithology': u.get('lithology') or '', 'description': u.get('description') or '',
                't0': _finite_or_none(u.get('t0')), 't1': _finite_or_none(u.get('t1')), 'contact': 'deposit', 'base': None,
                'n_contacts': 0, 'n_offsets': 0, 'against': [], 'warnings': [], 'derived_from': [u['outline'].id, topo.id],
                'provenance': {'method': MAPMODEL_METHOD, 'confidence': 'inferred',
                               'inputs': [u['outline'].name or name, topo.name or 'topography'],
                               'tol_m': tol, 'radius_m': radius, 'offset_m': offset}}
        if p == len(aged) - 1:
            base['provenance']['role'] = 'basement'
            base['provenance']['note'] = 'the oldest aged unit: no base is modelled'
            stats['basement'] = name
            out.append(base)
            if on_progress:
                on_progress((p + 1) / float(len(aged)))
            continue
        older = [oj for oj in aged[p + 1:] if ord_['keys'][oj] != key]
        contacts = PointSet(name='%s — map contacts' % name, role='contacts', color=base['color'])
        seen = set()
        for oj in older:
            ou = units[oj]
            r = shared_contacts(u['outline'], ou['outline'], tol, topo=topo, against=_label(ou, oj), name=contacts.name)
            stats['rejected']['edge_vertices'] += r['edge_skipped']
            stats['rejected']['nodata'] += r['nodata']
            if r['count'] and _append_points(contacts, r['points'], seen):
                base['against'].append(_label(ou, oj))
                base['derived_from'].append(ou['outline'].id)
                base['provenance']['inputs'].append(ou['outline'].name or _label(ou, oj))
        stats['contacts_per_unit'][name] = contacts.n
        if not contacts.n:
            stats['rejected']['no_contacts'].append(name)
            warnings.append('%s: its outline touches no older unit inside the model (%s) — skipped; nothing on the map says where its base is'
                            % (name, ', '.join(_label(units[oj], oj) for oj in older) if older else 'no older unit with an age'))
            if on_progress:
                on_progress((p + 1) / float(len(aged)))
            continue
        if contacts.n < min_contacts:
            stats['rejected']['few_contacts'].append(name)
            warnings.append('%s: only %d contact point%s with older units (%s) — fewer than %d, too little to fit a surface through; skipped'
                            % (name, contacts.n, '' if contacts.n == 1 else 's', ', '.join(base['against']), min_contacts))
            if on_progress:
                on_progress((p + 1) / float(len(aged)))
            continue
        d = dip_offsets(contacts, structural, radius=radius, offset=offset, exclude_sources=fault_names)
        stats['offsets_per_unit'][name] = d['used']
        stats['dips_used'] += d['used']
        merged = PointSet(name='%s — map contacts + dip offsets' % name, role='contacts', color=base['color'])
        _append_points(merged, contacts)
        _append_points(merged, d['points'])
        merged.metadata['map_model'] = {'unit': name, 'against': list(base['against']), 'contacts': contacts.n, 'offsets': d['used'],
                                        'unmatched': d['unmatched'], 'tol_m': tol, 'radius_m': radius, 'offset_m': offset}
        merged.metadata['derived_from'] = base['derived_from'] + R['layers']
        merged.provenance = dict(base['provenance'])
        merged.provenance['inputs'] = base['provenance']['inputs'] + [s.name or s.id for s in structural]
        merged.provenance['step'] = 'contacts + dip offsets'
        base['base'] = merged
        base['n_contacts'] = contacts.n
        base['n_offsets'] = d['used']
        base['derived_from'] = list(merged.metadata['derived_from'])
        base['provenance'] = merged.provenance
        if not d['used']:
            stats['units_without_dip'].append(name)
            base['warnings'].append(('no orientation within %g m of its contacts — the base is a heightfield through the contacts only, with no dip away from the line' % radius)
                                    if R['n'] else NO_DIP_WARNING)
        stats['units_modelled'].append(name)
        out.append(base)
        if on_progress:
            on_progress((p + 1) / float(len(aged)))
    if stats['no_dip']:
        for u in out:
            if u['base'] is not None and NO_DIP_WARNING not in u['warnings']:
                u['warnings'].append(NO_DIP_WARNING)
    if not aged:
        warnings.append('no unit carries an age — nothing can be ordered, nothing is modelled')
    elif len(out) == 1:
        warnings.append('only %s can be placed: no younger unit has enough contacts with an older one — no base surface to build' % stats['basement'])
    return {'units': out, 'stats': stats, 'warnings': warnings}


def strat_units(result):
    """The ``units`` list ``stratigraphy.build_stratigraphy`` takes, from a
    ``build_from_map`` result."""
    return [{'name': u['name'], 'color': u['color'], 'lithology': u['lithology'], 'description': u['description'],
             'contact': u['contact'], 'base': u['base']} for u in result['units']]
