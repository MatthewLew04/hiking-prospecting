"""geomodel.slicing — sections and iso-surfaces.

* ``mesh_plane_intersection`` — polylines where a Mesh meets a plane.
* ``grid_profile`` — a heightfield sampled along a section line.
* ``stratigraphy_section`` — the coloured "pancake" ribbons between stacked
  surfaces along a vertical section (what a geologist draws on a cross-section).
* ``blockmodel_plane_sample`` — a block-model attribute sampled onto a plane as
  a 2-D raster (for section images / heat maps).
* ``lineset_near_plane`` — workings / drillhole traces within a band of a
  section, projected onto it.
* ``isosurface`` — marching-tetrahedra iso-surface of a scalar field on a
  regular grid (how an RBF implicit model becomes a vein / contact mesh).

Planes are (point, unit normal).  Vertical sections are defined by a start and
end (x, y) and are the common case in mining.
"""
import math

from .model import Mesh, LineSet, NAN, farray, iarray


def section_plane(start, end):
    """(point, normal, u_axis, length) for a vertical section start->end."""
    x0, y0 = start[0], start[1]
    x1, y1 = end[0], end[1]
    dx, dy = x1 - x0, y1 - y0
    ln = math.hypot(dx, dy)
    if ln == 0:
        raise ValueError('zero-length section')
    u = (dx / ln, dy / ln, 0.0)
    n = (-u[1], u[0], 0.0)
    return (x0, y0, 0.0), n, u, ln


def plane_basis(point, normal):
    """Orthonormal (u, v) in the plane; v is as vertical as possible."""
    nx, ny, nz = normal
    ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    n = (nx / ln, ny / ln, nz / ln)
    up = (0.0, 0.0, 1.0)
    if abs(n[2]) > 0.999:
        up = (0.0, 1.0, 0.0)
    # u = up x n (horizontal-ish), v = n x u
    u = (up[1] * n[2] - up[2] * n[1], up[2] * n[0] - up[0] * n[2], up[0] * n[1] - up[1] * n[0])
    lu = math.sqrt(sum(c * c for c in u)) or 1.0
    u = tuple(c / lu for c in u)
    v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
    return n, u, v


def to_plane_coords(p, point, u, v):
    d = (p[0] - point[0], p[1] - point[1], p[2] - point[2])
    return (d[0] * u[0] + d[1] * u[1] + d[2] * u[2], d[0] * v[0] + d[1] * v[1] + d[2] * v[2])


def mesh_plane_intersection(mesh, point, normal, name=None):
    """Intersection polylines of a triangle mesh with a plane -> LineSet.
    Segments are chained into polylines by shared endpoints."""
    n, u, v = plane_basis(point, normal)
    px, py, pz = point
    V = mesh.vertices
    nv = mesh.n_vertices
    dist = [0.0] * nv
    b = mesh.bounds()
    scale = max(abs(v) for v in b) if b else 1.0
    tiny = 1e-12 * (scale or 1.0)
    for i in range(nv):
        d = ((V[3 * i] - px) * n[0] + (V[3 * i + 1] - py) * n[1] + (V[3 * i + 2] - pz) * n[2])
        dist[i] = d if d != 0 else tiny     # zeros count as positive (simulation of simplicity)
    segs = []
    T = mesh.triangles
    for t in range(0, len(T), 3):
        a, b, c = T[t], T[t + 1], T[t + 2]
        da, db, dc = dist[a], dist[b], dist[c]
        pts = []
        for (i, j, di, dj) in ((a, b, da, db), (b, c, db, dc), (c, a, dc, da)):
            if (di < 0) != (dj < 0):
                lo, hi = (i, j) if i < j else (j, i)      # evaluate edge in one direction
                dlo, dhi = dist[lo], dist[hi]
                tt = dlo / (dlo - dhi)
                pts.append((V[3 * lo] + (V[3 * hi] - V[3 * lo]) * tt,
                            V[3 * lo + 1] + (V[3 * hi + 1] - V[3 * lo + 1]) * tt,
                            V[3 * lo + 2] + (V[3 * hi + 2] - V[3 * lo + 2]) * tt))
        if len(pts) == 2:
            p, q = pts
            if (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2 > (1e-9 * (scale or 1.0)) ** 2:
                segs.append((p, q))          # drop zero-length segments (vertex on plane)
    ls = LineSet(name=name or (mesh.name + ' section'), role='section', color=mesh.color)
    for chain in chain_segments(segs):
        ls.add_polyline(chain, {'source': mesh.id})
    ls.metadata['plane'] = {'point': list(point), 'normal': list(n)}
    return ls


def chain_segments(segs, eps=1e-6):
    """Join segments sharing endpoints into polylines (greedy)."""
    if not segs:
        return []
    key = lambda p: (round(p[0] / eps), round(p[1] / eps), round(p[2] / eps))
    ends = {}
    for si, (a, b) in enumerate(segs):
        ends.setdefault(key(a), []).append(si)
        ends.setdefault(key(b), []).append(si)
    used = [False] * len(segs)
    chains = []
    for s0 in range(len(segs)):
        if used[s0]:
            continue
        used[s0] = True
        a, b = segs[s0]
        chain = [a, b]
        # extend forward from b, then backward from a
        for direction in (1, -1):
            while True:
                tip = chain[-1] if direction == 1 else chain[0]
                cand = [si for si in ends.get(key(tip), []) if not used[si]]
                if not cand:
                    break
                si = cand[0]
                used[si] = True
                p, q = segs[si]
                nxt = q if key(p) == key(tip) else p
                if direction == 1:
                    chain.append(nxt)
                else:
                    chain.insert(0, nxt)
        if len(chain) > 2 and key(chain[0]) == key(chain[-1]):
            chain[-1] = chain[0]                 # snap closed loops exactly
        chains.append(chain)
    return chains


def grid_profile(grid, start, end, n=200):
    """Sample a Grid2D along start->end: list of (distance, x, y, z) (z NaN
    where no data)."""
    (x0, y0, _), _n, u, ln = section_plane(start, end)
    out = []
    for k in range(n + 1):
        d = ln * k / float(n)
        x, y = x0 + u[0] * d, y0 + u[1] * d
        out.append((d, x, y, grid.sample(x, y)))
    return out


def profile_lineset(grid, start, end, n=200, name=None, lift=0.0):
    ls = LineSet(name=name or grid.name + ' profile', role='section', color=grid.color)
    run = []
    for d, x, y, z in grid_profile(grid, start, end, n):
        if z != z:
            if len(run) > 1:
                ls.add_polyline(run, {'source': grid.id})
            run = []
            continue
        run.append((x, y, z + lift))
    if len(run) > 1:
        ls.add_polyline(run, {'source': grid.id})
    return ls


def stratigraphy_section(strat, grids, topo, start, end, n=200):
    """Coloured ribbons (one Mesh per unit) filling a vertical section between
    successive surfaces of a pancake model.  The basement ribbon extends to
    z_floor = lowest base - 10% of the section length."""
    profiles = [grid_profile(topo, start, end, n)]
    names, colors = [], []
    for u in strat.units:
        names.append(u['name'])
        colors.append(u['color'])
        if u['base'] is None:
            zmin = min([p[3] for prof in profiles for p in prof if p[3] == p[3]] or [0.0])
            floor = zmin - max(50.0, 0.1 * section_plane(start, end)[3])
            profiles.append([(d, x, y, floor) for d, x, y, _z in profiles[0]])
            break
        profiles.append(grid_profile(grids[u['base']], start, end, n))
    meshes = []
    for k in range(len(profiles) - 1):
        top, base = profiles[k], profiles[k + 1]
        verts = farray()
        tris = iarray()
        idx = {}
        for s in range(len(top)):
            zt, zb = top[s][3], base[s][3]
            if zt != zt or zb != zb:
                continue
            if zb > zt:
                zb = zt
            idx[s] = len(verts) // 3
            verts.extend((top[s][1], top[s][2], zt))
            verts.extend((base[s][1], base[s][2], zb))
        for s in range(len(top) - 1):
            if s in idx and s + 1 in idx:
                a, b = idx[s], idx[s + 1]
                tris.extend((a, a + 1, b + 1, a, b + 1, b))
                tris.extend((a, b + 1, a + 1, a, b, b + 1))     # double-sided
        m = Mesh(verts, tris, name=names[k] + ' (section)', color=colors[k], role='section')
        m.metadata['unit'] = names[k]
        meshes.append(m)
    return meshes


def blockmodel_plane_sample(bm, attribute, point, normal, extent=None, resolution=None):
    """Sample a block attribute onto a plane -> dict(width, height, values
    (row-major from the top-left of the plane patch), corners[4] world xyz,
    u, v basis, du, dv).  Nearest-block lookup (blocks are piecewise constant)."""
    n, u, v = plane_basis(point, normal)
    vals = bm.attributes[attribute]['values']
    if extent is None:
        b = bm.bounds()
        diag = math.sqrt((b[3] - b[0]) ** 2 + (b[4] - b[1]) ** 2 + (b[5] - b[2]) ** 2)
        extent = (-diag / 2, diag / 2, -diag / 2, diag / 2)
    umin, umax, vmin, vmax = extent
    res = resolution or min(bm.block_size) / 2.0
    w = max(1, int(math.ceil((umax - umin) / res)))
    h = max(1, int(math.ceil((vmax - vmin) / res)))
    out = farray()
    nx, ny, nz = bm.count
    ox, oy, oz = bm.origin
    dx, dy, dz = bm.block_size
    az = math.radians(bm.azimuth)
    ca, sa = math.cos(az), math.sin(az)
    for r in range(h):
        vv = vmax - (r + 0.5) * res
        for c in range(w):
            uu = umin + (c + 0.5) * res
            x = point[0] + u[0] * uu + v[0] * vv
            y = point[1] + u[1] * uu + v[1] * vv
            z = point[2] + u[2] * uu + v[2] * vv
            lx, ly = x - ox, y - oy
            if bm.azimuth:
                lx, ly = lx * ca - ly * sa, lx * sa + ly * ca
            i, j, k = int(math.floor(lx / dx)), int(math.floor(ly / dy)), int(math.floor((z - oz) / dz))
            if 0 <= i < nx and 0 <= j < ny and 0 <= k < nz:
                val = vals[bm.index(i, j, k)]
                out.append(NAN if val is None else val)
            else:
                out.append(NAN)
    corners = []
    for (uu, vv) in ((umin, vmax), (umax, vmax), (umax, vmin), (umin, vmin)):
        corners.append((point[0] + u[0] * uu + v[0] * vv, point[1] + u[1] * uu + v[1] * vv,
                        point[2] + u[2] * uu + v[2] * vv))
    return {'width': w, 'height': h, 'values': out, 'corners': corners, 'u': u, 'v': v,
            'du': res, 'dv': res, 'extent': extent}


def lineset_near_plane(ls, point, normal, half_width, project=True, name=None):
    """Parts of a LineSet within ``half_width`` of the plane (clipped at the
    band), optionally projected onto the plane."""
    n, u, v = plane_basis(point, normal)
    out = LineSet(name=name or ls.name + ' near section', role=ls.role, color=ls.color)

    def sd(p):
        return (p[0] - point[0]) * n[0] + (p[1] - point[1]) * n[1] + (p[2] - point[2]) * n[2]

    def proj(p):
        if not project:
            return p
        d = sd(p)
        return (p[0] - d * n[0], p[1] - d * n[1], p[2] - d * n[2])

    for k, part in enumerate(ls.parts):
        pts = [ls.vertex(i) for i in part]
        run = []
        for a in range(len(pts)):
            p = pts[a]
            inside = abs(sd(p)) <= half_width
            if inside:
                if not run and a > 0:      # entering: add the clipped entry point
                    q = pts[a - 1]
                    run.append(proj(_clip_to_band(q, p, sd, half_width)))
                run.append(proj(p))
            else:
                if run:
                    run.append(proj(_clip_to_band(pts[a - 1], p, sd, half_width)))
                    if len(run) > 1:
                        out.add_polyline(run, ls.features[k] if k < len(ls.features) else {})
                    run = []
        if len(run) > 1:
            out.add_polyline(run, ls.features[k] if k < len(ls.features) else {})
    return out


def _clip_to_band(a, b, sd, hw):
    da, db = sd(a), sd(b)
    target = hw if max(da, db) > hw else -hw
    if db == da:
        return b
    t = (target - da) / (db - da)
    t = max(0.0, min(1.0, t))
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


# ------------------------------------------------------------- iso-surface
_TET_CUBE = ((0, 5, 1, 6), (0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6), (0, 7, 4, 6), (0, 4, 5, 6))
_CUBE_OFF = ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))


def isosurface(field, count, origin, spacing, iso=0.0, name='isosurface', color=None):
    """Marching tetrahedra on a regular NODE grid.

    field: flat list/array of values at nodes, index i + nx*(j + ny*k)
           (i fastest), NaN = missing (cubes touching NaN are skipped).
    count: (nx, ny, nz) nodes; origin: (x0, y0, z0) of node (0,0,0);
    spacing: (dx, dy, dz).
    Returns a Mesh whose triangles are oriented with the gradient (from
    negative to positive), with shared vertices along edges.
    """
    nx, ny, nz = count
    dx, dy, dz = spacing
    x0, y0, z0 = origin
    verts = farray()
    tris = iarray()
    edge_cache = {}

    def node(i, j, k):
        return i + nx * (j + ny * k)

    def interp_vertex(ia, ib, va, vb):
        t = (iso - va) / (vb - va) if vb != va else 0.5
        if t <= 1e-9:                 # iso passes through node a: reuse its vertex
            key, t = (ia, ia), 0.0
        elif t >= 1 - 1e-9:
            key, t, ia, va = (ib, ib), 0.0, ib, vb
        else:
            key = (ia, ib) if ia < ib else (ib, ia)
        vi = edge_cache.get(key)
        if vi is not None:
            return vi
        ai, aj, ak = ia % nx, (ia // nx) % ny, ia // (nx * ny)
        bi, bj, bk = ib % nx, (ib // nx) % ny, ib // (nx * ny)
        vi = len(verts) // 3
        verts.extend((x0 + (ai + (bi - ai) * t) * dx, y0 + (aj + (bj - aj) * t) * dy,
                      z0 + (ak + (bk - ak) * t) * dz))
        edge_cache[key] = vi
        return vi

    def emit(a, b, c):
        if a != b and b != c and a != c:      # drop degenerate triangles
            tris.extend((a, b, c))

    for k in range(nz - 1):
        for j in range(ny - 1):
            for i in range(nx - 1):
                ids = [node(i + o[0], j + o[1], k + o[2]) for o in _CUBE_OFF]
                vals = [field[n] for n in ids]
                if any(v != v for v in vals):
                    continue
                if all(v < iso for v in vals) or all(v >= iso for v in vals):
                    continue
                for tet in _TET_CUBE:
                    tn = [ids[t] for t in tet]
                    tv = [vals[t] for t in tet]
                    inside = [v >= iso for v in tv]
                    cnt = sum(inside)
                    if cnt == 0 or cnt == 4:
                        continue
                    if cnt == 1 or cnt == 3:
                        # one vertex apart from the other three
                        flag = (cnt == 1)
                        a = [q for q in range(4) if inside[q] == flag][0]
                        others = [q for q in range(4) if q != a]
                        p = [interp_vertex(tn[a], tn[o], tv[a], tv[o]) for o in others]
                        # orientation: triangle normal towards positive side
                        if flag:        # a is the positive vertex -> normal points to a
                            emit(p[0], p[2], p[1])
                        else:
                            emit(p[0], p[1], p[2])
                    else:
                        pos = [q for q in range(4) if inside[q]]
                        neg = [q for q in range(4) if not inside[q]]
                        a, b = pos
                        c, d = neg
                        pac = interp_vertex(tn[a], tn[c], tv[a], tv[c])
                        pad = interp_vertex(tn[a], tn[d], tv[a], tv[d])
                        pbc = interp_vertex(tn[b], tn[c], tv[b], tv[c])
                        pbd = interp_vertex(tn[b], tn[d], tv[b], tv[d])
                        emit(pac, pbc, pbd)
                        emit(pac, pbd, pad)
    m = Mesh(verts, tris, name=name, color=color or [200, 120, 60], role='surface')
    _orient_consistently(m, field, count, origin, spacing, iso)
    m.metadata['iso'] = iso
    return m


def _orient_consistently(mesh, field, count, origin, spacing, iso):
    """Flip triangles whose normal points against the field gradient."""
    nx, ny, nz = count
    dx, dy, dz = spacing
    x0, y0, z0 = origin
    V, T = mesh.vertices, mesh.triangles

    def grad(x, y, z):
        i = min(max(int((x - x0) / dx), 0), nx - 2)
        j = min(max(int((y - y0) / dy), 0), ny - 2)
        k = min(max(int((z - z0) / dz), 0), nz - 2)

        def f(ii, jj, kk):
            return field[ii + nx * (jj + ny * kk)]
        gx = f(i + 1, j, k) - f(i, j, k)
        gy = f(i, j + 1, k) - f(i, j, k)
        gz = f(i, j, k + 1) - f(i, j, k)
        return gx / dx, gy / dy, gz / dz

    for t in range(0, len(T), 3):
        a, b, c = T[t], T[t + 1], T[t + 2]
        ax, ay, az = V[3 * a], V[3 * a + 1], V[3 * a + 2]
        ux, uy, uz = V[3 * b] - ax, V[3 * b + 1] - ay, V[3 * b + 2] - az
        wx, wy, wz = V[3 * c] - ax, V[3 * c + 1] - ay, V[3 * c + 2] - az
        nxv, nyv, nzv = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
        cx, cy, cz = (ax + V[3 * b] + V[3 * c]) / 3, (ay + V[3 * b + 1] + V[3 * c + 1]) / 3, (az + V[3 * b + 2] + V[3 * c + 2]) / 3
        g = grad(cx, cy, cz)
        if nxv * g[0] + nyv * g[1] + nzv * g[2] < 0:
            T[t + 1], T[t + 2] = c, b


def scalar_field_from_rbf(rbf, bounds, spacing):
    """Evaluate an interp.RBF on a node grid -> (field, count, origin)."""
    minx, miny, minz, maxx, maxy, maxz = bounds
    dx, dy, dz = spacing if not isinstance(spacing, (int, float)) else (spacing,) * 3
    nx = int(math.ceil((maxx - minx) / dx)) + 1
    ny = int(math.ceil((maxy - miny) / dy)) + 1
    nz = int(math.ceil((maxz - minz) / dz)) + 1
    targets = []
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                targets.append((minx + i * dx, miny + j * dy, minz + k * dz))
    vals = rbf.predict_np(targets)
    return farray(vals), (nx, ny, nz), (minx, miny, minz), (dx, dy, dz)


def grid_to_points_on_line(grid, start, end, n=100):
    """Convenience: (x, y, z) along a section where the grid has data."""
    return [(x, y, z) for _d, x, y, z in grid_profile(grid, start, end, n) if z == z]


# ---------------------------------------------------------- ground clip
def _clip_core(mesh, topo, eps):
    from .interp import _surface_sampler
    z_at = _surface_sampler(topo)
    nv = mesh.n_vertices
    V = mesh.vertices
    depth = [NAN] * nv
    for i in range(nv):
        zg = z_at(V[3 * i], V[3 * i + 1])
        depth[i] = V[3 * i + 2] - zg if zg == zg else NAN
    v_keys = [k for k, a in mesh.attributes.items() if a.get('location', 'vertices') != 'faces']
    f_keys = [k for k, a in mesh.attributes.items() if a.get('location', 'vertices') == 'faces']
    out_v, out_t, segments = [], [], []
    out_va = dict((k, []) for k in v_keys)
    out_fa = dict((k, []) for k in f_keys)
    vmap = [-1] * nv
    edge = {}

    def use_vertex(i):
        if vmap[i] < 0:
            vmap[i] = len(out_v) // 3
            out_v.extend((V[3 * i], V[3 * i + 1], V[3 * i + 2]))
            for k in v_keys:
                out_va[k].append(mesh.attributes[k]['values'][i])
        return vmap[i]

    def crossing(p, q):
        lo, hi = min(p, q), max(p, q)
        key = (lo, hi)
        if key in edge:
            return edge[key]
        t = depth[lo] / (depth[lo] - depth[hi])
        vid = len(out_v) // 3
        for a in range(3):
            out_v.append(V[3 * lo + a] + (V[3 * hi + a] - V[3 * lo + a]) * t)
        for k in v_keys:
            va = mesh.attributes[k]['values']
            out_va[k].append(va[lo] + (va[hi] - va[lo]) * t)
        edge[key] = vid
        return vid

    def at(vid):
        return (out_v[3 * vid], out_v[3 * vid + 1], out_v[3 * vid + 2])

    stats = {'kept': 0, 'dropped': 0, 'split': 0, 'unknown_ground': 0}
    for t in range(mesh.n_triangles):
        I = mesh.triangle(t)
        D = [depth[i] for i in I]

        def emit_whole():
            out_t.extend((use_vertex(I[0]), use_vertex(I[1]), use_vertex(I[2])))
            for k in f_keys:
                out_fa[k].append(mesh.attributes[k]['values'][t])

        if D[0] != D[0] or D[1] != D[1] or D[2] != D[2]:
            stats['unknown_ground'] += 1
            emit_whole()
            continue
        below = [d <= eps for d in D]
        nb = sum(1 for b in below if b)
        if nb == 3:
            stats['kept'] += 1
            emit_whole()
            continue
        if nb == 0:
            stats['dropped'] += 1
            continue
        stats['split'] += 1
        s = 0
        for q in range(3):
            if below[q] == (nb == 1):
                s = q
                break
        p, q, r = I[s], I[(s + 1) % 3], I[(s + 2) % 3]
        if nb == 1:
            cq, cr = crossing(p, q), crossing(p, r)
            out_t.extend((use_vertex(p), cq, cr))
            for k in f_keys:
                out_fa[k].append(mesh.attributes[k]['values'][t])
            segments.append((at(cq), at(cr)))
        else:
            cpq, crp = crossing(p, q), crossing(r, p)
            out_t.extend((use_vertex(q), use_vertex(r), crp))
            out_t.extend((use_vertex(q), crp, cpq))
            for k in f_keys:
                out_fa[k].append(mesh.attributes[k]['values'][t])
                out_fa[k].append(mesh.attributes[k]['values'][t])
            segments.append((at(cpq), at(crp)))
    attributes = {}
    for k in v_keys:
        attributes[k] = {'location': 'vertices', 'values': farray(out_va[k])}
    for k in f_keys:
        attributes[k] = {'location': 'faces', 'values': farray(out_fa[k])}
    return {'vertices': farray(out_v), 'triangles': iarray(out_t), 'attributes': attributes,
            'segments': segments, 'stats': stats}


def clip_mesh_to_topography(mesh, topo, eps=1e-6, name=None):
    """Keep the part of a mesh below the ground (a Grid2D or a Mesh): triangles
    wholly above are dropped, wholly below kept, mixed ones split where their
    edges cross the ground (linear interpolation).  The JS
    ``clipMeshToTopography``: same vertex and triangle order."""
    c = _clip_core(mesh, topo, float(eps))
    out = Mesh(c['vertices'], c['triangles'], attributes=c['attributes'], role=mesh.role,
               name=name or '%s (below %s)' % (mesh.name, topo.name), color=mesh.color, opacity=mesh.opacity,
               group=mesh.group, provenance=dict(mesh.provenance), metadata=dict(mesh.metadata))
    out.metadata['clipped_to'] = topo.name
    clip = {'source_id': mesh.id, 'ground_id': topo.id}
    clip.update(c['stats'])
    out.metadata['clip'] = clip
    return out


def daylight_trace(source, topo, eps=1e-6, color=None):
    """Where a surface (Mesh or Grid2D) meets the ground: a LineSet (role
    'interpretation') named '<name> daylight (computed)'."""
    if source.kind == 'mesh':
        segs = _clip_core(source, topo, float(eps))['segments']
    elif source.kind == 'grid2d':
        from .contours import marching_squares
        if topo.kind != 'grid2d':
            raise ValueError('a grid surface needs a grid topography')

        def at(i, j):
            v = source.values[j * source.nx + i]
            if v != v:
                return NAN
            x, y = source.node_xy(i, j)
            zg = topo.sample(x, y)
            return v - zg if zg == zg else NAN

        def z_on(p):
            z = topo.sample(p[0], p[1])
            return z if z == z else source.sample(p[0], p[1])

        segs = [((p[0], p[1], z_on(p)), (q[0], q[1], z_on(q)))
                for p, q in marching_squares(source.nx, source.ny, at, source.node_xy, 0.0)]
    else:
        raise ValueError('cannot daylight a %s' % source.kind)
    ls = LineSet(name='%s daylight (computed)' % source.name, role='interpretation', color=color or [255, 200, 60])
    for chain in chain_segments(segs):
        ls.add_polyline(chain, {'source': source.name, 'source_id': source.id, 'ground': topo.name,
                                'method': 'surface ∩ topography', 'confidence': 'inferred'})
    ls.metadata['derived_from'] = [source.id, topo.id]
    ls.metadata['note'] = 'computed intersection of a modelled surface with the topography — it inherits every error of both'
    ls.provenance = {'method': 'surface / topography intersection (daylight_trace)', 'source_layer': source.name,
                     'ground': topo.name}
    return ls
