"""geomodel.stratigraphy — the "pancake" layer-cake builder.

A stratigraphic model is an ordered list of units, youngest (top) first.  Each
unit is bounded below by a contact surface (a Grid2D heightfield on the model
lattice) and above by the base of the unit above it — or by topography for
the first unit.  The last unit (basement) has no base.

Contact rules follow Leapfrog's surface chronology, reduced to heightfields:

* ``deposit`` — a conformable contact.  Deposits do NOT cut older volumes:
  where the deposit's base would dip below an older surface it is lifted onto
  it (on-lap / pinch-out, zero thickness).
* ``erosion`` — an unconformity.  Erosion surfaces cut everything older:
  every older surface below is clipped up to it.
* everything is finally clipped below topography.

The result is monotonic (top >= base everywhere), so volumes, vertical
columns ("virtual drillholes"), block-model tagging and section ribbons are all
exact, and the model round-trips as one Grid2D per contact + a StratModel
object that records the order, colours and rules.
"""
from .model import Grid2D, Mesh, StratModel, PointSet, NAN, farray
from . import interp


DEFAULT_COLORS = [[222, 184, 135], [205, 133, 63], [160, 160, 200], [120, 170, 120],
                  [200, 120, 120], [190, 190, 100], [100, 150, 190], [170, 120, 170],
                  [140, 140, 140], [230, 200, 150]]


def surface_on_lattice(source, lattice, method='rbf', **params):
    """Return a Grid2D on ``lattice``'s nodes from ``source`` which may be a
    Grid2D (resampled bilinearly), a PointSet of contact points (interpolated
    with ``method``), or a constant elevation (float)."""
    g = lattice.copy_empty()
    if isinstance(source, (int, float)):
        g.values = farray([float(source)] * (g.nx * g.ny))
        g.metadata['source'] = 'constant'
        return g
    if isinstance(source, Grid2D):
        vals = farray()
        for j in range(g.ny):
            for i in range(g.nx):
                x, y = g.node_xy(i, j)
                vals.append(source.sample(x, y))
        g.values = vals
        g.metadata['source'] = 'grid:' + source.id
        g.name = source.name
        return g
    if isinstance(source, PointSet):
        pts = [source.point(i) for i in range(source.n)]
        zcol = params.pop('z_column', None)
        vals = list(source.numeric(zcol)) if zcol else [p[2] for p in pts]
        spec = (g.x0, g.y0, g.dx, g.dy, g.nx, g.ny)
        out = interp.grid_from_points(pts, vals, method=method, spec=spec, name=source.name, **params)
        out.metadata['source'] = 'points:' + source.id
        out.rotation = g.rotation
        return out
    raise TypeError('unsupported contact source %r' % type(source))


def build_stratigraphy(topography, units, lattice=None, method='rbf', **params):
    """Build the pancake stack.

    topography: Grid2D (defines the lattice unless ``lattice`` given).
    units: list (top/youngest first) of dicts:
        {'name': 'Alluvium', 'color': [r,g,b], 'lithology': 'gravel',
         'contact': 'deposit'|'erosion',      # rule for this unit's BASE
         'base': Grid2D | PointSet | float | None (None = basement)}
    Returns (StratModel, [Grid2D base surfaces in unit order], topo_on_lattice).
    """
    lattice = lattice or topography
    topo = surface_on_lattice(topography, lattice) if topography is not lattice else topography
    n = lattice.nx * lattice.ny
    bases = []
    rules = []
    for k, u in enumerate(units):
        src = u.get('base')
        if src is None:
            bases.append(None)
        else:
            g = surface_on_lattice(src, lattice, method=method, **dict(params))
            g.name = u.get('name', 'unit %d' % k) + ' base'
            g.role = 'contact'
            g.color = u.get('color') or DEFAULT_COLORS[k % len(DEFAULT_COLORS)]
            g.metadata['contact'] = u.get('contact', 'deposit')
            bases.append(g)
        rules.append(u.get('contact', 'deposit'))
    # chronology: apply from OLDEST surface up to the youngest
    real = [(k, g) for k, g in enumerate(bases) if g is not None]
    for pos in range(len(real) - 1, -1, -1):
        k, g = real[pos]
        older = [og for ok_, og in real[pos + 1:]]
        if rules[k] == 'erosion':
            for og in older:                       # cut everything below
                for idx in range(n):
                    a, b = og.values[idx], g.values[idx]
                    if a == a and b == b and a > b:
                        og.values[idx] = b
        else:                                      # deposit on-laps older surfaces
            for og in older:
                for idx in range(n):
                    a, b = og.values[idx], g.values[idx]
                    if a == a and b == b and b < a:
                        g.values[idx] = a
    # nothing above topography
    for _k, g in real:
        for idx in range(n):
            t, b = topo.values[idx], g.values[idx]
            if t == t and b == b and b > t:
                g.values[idx] = t
    sm = StratModel(name=params.get('name', 'stratigraphy'), topography=topo.id)
    for k, u in enumerate(units):
        sm.units.append({'name': u.get('name', 'unit %d' % k),
                         'color': u.get('color') or DEFAULT_COLORS[k % len(DEFAULT_COLORS)],
                         'lithology': u.get('lithology', ''),
                         'description': u.get('description', ''),
                         'contact': rules[k],
                         'base': bases[k].id if bases[k] is not None else None})
    return sm, bases, topo


def column_at(strat, grids, x, y, topo):
    """Vertical column at (x, y): list of {'name','top','base','thickness'}
    (basement gets base = None).  ``grids`` = {grid id: Grid2D}."""
    out = []
    top = topo.sample(x, y)
    for u in strat.units:
        if u['base'] is None:
            out.append({'name': u['name'], 'top': top, 'base': None,
                        'thickness': None, 'color': u['color']})
            break
        g = grids[u['base']]
        b = g.sample(x, y)
        if b != b or top != top:
            out.append({'name': u['name'], 'top': top, 'base': b, 'thickness': NAN, 'color': u['color']})
        else:
            out.append({'name': u['name'], 'top': top, 'base': b,
                        'thickness': max(top - b, 0.0), 'color': u['color']})
        top = b
    return out


def unit_at(strat, grids, x, y, z, topo):
    """Name of the unit containing (x, y, z) or None above topography."""
    col = column_at(strat, grids, x, y, topo)
    for c in col:
        if c['top'] != c['top']:
            continue
        if z > c['top'] + 1e-9:
            return None
        if c['base'] is None or z >= c['base']:
            return c['name']
    return None


def unit_volume_mesh(top, base, name, color, skirt=True):
    """Closed mesh of one unit between two heightfields on the same lattice:
    top surface (up-facing), base surface (down-facing) and a skirt around the
    lattice edge.  Zero-thickness cells are kept (degenerate but closed)."""
    if (top.nx, top.ny) != (base.nx, base.ny):
        raise ValueError('top and base grids must share a lattice')
    nx, ny = top.nx, top.ny
    verts = farray()
    idx_top, idx_base = {}, {}
    for j in range(ny):
        for i in range(nx):
            x, y = top.node_xy(i, j)
            zt, zb = top.get(i, j), base.get(i, j)
            if zt != zt or zb != zb:
                continue
            if zb > zt:
                zb = zt
            idx_top[(i, j)] = len(verts) // 3
            verts.extend((x, y, zt))
            idx_base[(i, j)] = len(verts) // 3
            verts.extend((x, y, zb))
    tris = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            q = [(i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)]
            if any(c not in idx_top for c in q):
                continue
            a, b, c, d = (idx_top[k] for k in q)
            tris.extend([a, b, c, a, c, d])                 # top: CCW from above
            a, b, c, d = (idx_base[k] for k in q)
            tris.extend([a, c, b, a, d, c])                 # base: CCW from below
    if skirt:
        def edge(cells):
            for (p, q) in cells:
                if p in idx_top and q in idx_top:
                    tp, tq, bp, bq = idx_top[p], idx_top[q], idx_base[p], idx_base[q]
                    tris.extend([tp, bp, bq, tp, bq, tq])
        south = [((i, 0), (i + 1, 0)) for i in range(nx - 1)]
        east = [((nx - 1, j), (nx - 1, j + 1)) for j in range(ny - 1)]
        north = [((i + 1, ny - 1), (i, ny - 1)) for i in range(nx - 1)]
        west = [((0, j + 1), (0, j)) for j in range(ny - 1)]
        for cells in (south, east, north, west):
            edge(cells)
    m = Mesh(verts, tris, name=name, color=color, role='unit')
    m.metadata['unit'] = name
    return m


def stratigraphy_volumes(strat, grids, topo):
    """One closed Mesh per unit (basement gets a flat floor 1 km below the
    lowest base so it is still closed)."""
    meshes = []
    top = topo
    lowest = min([g.zrange()[0] for g in grids.values() if g.zrange()[0] == g.zrange()[0]] or [topo.zrange()[0]])
    for u in strat.units:
        if u['base'] is None:
            floor = top.copy_empty(fill=lowest - 1000.0)
            meshes.append(unit_volume_mesh(top, floor, u['name'], u['color']))
            break
        base = grids[u['base']]
        meshes.append(unit_volume_mesh(top, base, u['name'], u['color']))
        top = base
    return meshes


def thickness_grid(top, base, name='thickness'):
    g = top.copy_empty()
    for idx in range(len(g.values)):
        t, b = top.values[idx], base.values[idx]
        g.values[idx] = NAN if (t != t or b != b) else max(t - b, 0.0)
    g.name = name
    g.role = 'property'
    g.units = 'm'
    return g


def tag_blockmodel(bm, strat, grids, topo, attribute='unit'):
    """Add a category attribute naming the unit each block centroid sits in
    ('' above topography)."""
    names = []
    nx, ny, nz = bm.count
    cache = {}
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                x, y, z = bm.centroid(i, j, k)
                col = cache.get((i, j))
                if col is None:
                    col = column_at(strat, grids, x, y, topo)
                    cache[(i, j)] = col
                name = ''
                for c in col:
                    if c['top'] != c['top']:
                        continue
                    if z > c['top']:
                        break
                    if c['base'] is None or z >= c['base']:
                        name = c['name']
                        break
                names.append(name)
    bm.add_attribute(attribute, names, kind='category')
    return bm
