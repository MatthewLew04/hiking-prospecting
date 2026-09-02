"""geomodel.contours — contour lines from a Grid2D by marching squares (the
JS ``contourGrid`` / ``contourLevels`` / ``marchingSquares`` in gm-engine.js,
same cell order, same saddle rule, same chaining, so the two sides produce
identical LineSets).

A heightfield's lines sit at their own level; a property grid's (magnetics,
a form-interpolant evaluated onto topography...) can be draped on a surface
grid with a small lift, or put at a constant elevation.  Every part carries
``level``, ``units``, ``source`` and ``index`` (True on every Nth level).
"""
import math

from .model import LineSet
from .slicing import chain_segments


def marching_squares(nx, ny, at, xy, lv):
    """Marching squares on any lattice: ``at(i, j)`` is the node value (NaN =
    no data; cells touching one are skipped), ``xy(i, j)`` its position.
    Returns [((x, y), (x, y)), ...] for one level.  Saddle cells (cases 5 and
    10) are resolved by the cell mean."""
    segs = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            v = (at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1))
            if v[0] != v[0] or v[1] != v[1] or v[2] != v[2] or v[3] != v[3]:
                continue
            code = 0
            for k in range(4):
                if v[k] >= lv:
                    code |= (1 << k)
            if code == 0 or code == 15:
                continue
            P = (xy(i, j), xy(i + 1, j), xy(i + 1, j + 1), xy(i, j + 1))

            def pt(e):
                p, q = e, (e + 1) % 4
                t = (lv - v[p]) / (v[q] - v[p])
                return (P[p][0] + (P[q][0] - P[p][0]) * t, P[p][1] + (P[q][1] - P[p][1]) * t)

            if code == 5 or code == 10:
                centre_high = (v[0] + v[1] + v[2] + v[3]) / 4 >= lv
                if (code == 5) == centre_high:
                    segs.append((pt(0), pt(1)))
                    segs.append((pt(2), pt(3)))
                else:
                    segs.append((pt(3), pt(0)))
                    segs.append((pt(1), pt(2)))
                continue
            edges = [e for e in range(4) if (v[e] >= lv) != (v[(e + 1) % 4] >= lv)]
            segs.append((pt(edges[0]), pt(edges[1])))
    return segs


def nice_interval(span, target=8):
    """1 / 2 / 5 × 10ⁿ nearest to span / target."""
    raw = abs(span) / max(1, target)
    if not raw > 0:
        return 1.0
    p = 10.0 ** math.floor(math.log10(raw))
    f = raw / p
    return (1 if f < 1.5 else 2 if f < 3.5 else 5 if f < 7.5 else 10) * p


def contour_levels(grid, interval, base=0.0):
    """Level list base + m·interval covering the grid's value range."""
    interval = float(interval)
    base = float(base or 0)
    if not interval > 0:
        raise ValueError('contour interval must be > 0')
    zmin, zmax = grid.zrange()
    if zmin != zmin or zmax != zmax:
        return []
    m0 = math.ceil((zmin - base) / interval - 1e-9)
    m1 = math.floor((zmax - base) / interval + 1e-9)
    if m1 - m0 + 1 > 5000:
        raise ValueError('%d levels at an interval of %s — choose a coarser interval' % (m1 - m0 + 1, interval))
    return [base + m * interval for m in range(m0, m1 + 1)]


def contour_grid(grid, levels=None, interval=None, base=0.0, index=0, drape=None, lift=0.0, z=None,
                 name=None, color=None):
    """Contour a Grid2D -> LineSet (role 'contours'), one part per chained
    polyline, features {level, units, source, source_id, index}.
    ``index``: every Nth level is an index contour; ``drape``: a Grid2D to put
    a property grid's lines on (``lift`` metres above it); ``z``: a constant
    elevation when there is nothing to drape on."""
    base = float(base or 0)
    lv = contour_levels(grid, interval, base) if levels is None else [float(x) for x in levels]
    lift = float(lift or 0)
    N = int(index or 0)
    heightfield = grid.role != 'property'
    ls = LineSet(name=name or '%s contours' % grid.name, role='contours', color=color or [90, 70, 40])

    def at(i, j):
        return grid.values[j * grid.nx + i]

    def xy(i, j):
        return grid.node_xy(i, j)

    def z_of(x, y, level):
        if drape is not None:
            zt = drape.sample(x, y)
            if zt == zt:
                return zt + lift
        if z is not None:
            return float(z)
        return level + lift if heightfield else 0.0

    nseg = 0
    for k, level in enumerate(lv):
        segs = [((p[0], p[1], z_of(p[0], p[1], level)), (q[0], q[1], z_of(q[0], q[1], level)))
                for p, q in marching_squares(grid.nx, grid.ny, at, xy, level)]
        nseg += len(segs)
        if N > 0:
            is_index = (round((level - base) / interval) % N == 0) if interval else (k % N == 0)
        else:
            is_index = False
        for chain in chain_segments(segs):
            ls.add_polyline(chain, {'level': level, 'units': grid.units or 'm', 'source': grid.name,
                                    'source_id': grid.id, 'index': is_index})
    ls.metadata['contours'] = {'levels': lv, 'n_levels': len(lv), 'n_segments': nseg,
                               'interval': None if interval is None else float(interval), 'base': base,
                               'index_every': N or None, 'draped_on': drape.name if drape is not None else None,
                               'lift': lift}
    ls.provenance = {'method': 'marching squares over the grid nodes (contour_grid)', 'source_layer': grid.name,
                     'source_id': grid.id}
    return ls
