"""geomodel.blockmodel — block-model creation and grade estimation.

``create_blockmodel`` lays a regular block grid over a bounding box;
``estimate`` fills an attribute by ordinary kriging / IDW / nearest neighbour
from a PointSet of samples (assay interval midpoints, surface samples, graded
mines...) with an optional domain restriction (only blocks whose category
attribute equals a value — e.g. the unit tagged by stratigraphy.tag_blockmodel
— and only samples inside it).  ``grade_tonnage`` reports tonnes / grade above
cut-offs.  All numbers carry their assumptions in ``bm.metadata``.
"""
import math

from .model import BlockModel, PointSet, NAN, farray
from . import interp


def create_blockmodel(bounds, block_size, name='block model', azimuth=0.0, snap=True):
    """bounds = (minx, miny, minz, maxx, maxy, maxz); block_size = (dx, dy, dz)
    or a scalar.  The origin snaps down to a block multiple when ``snap``."""
    if isinstance(block_size, (int, float)):
        block_size = (block_size, block_size, block_size)
    dx, dy, dz = (float(v) for v in block_size)
    minx, miny, minz, maxx, maxy, maxz = bounds
    if snap:
        minx, miny, minz = (math.floor(minx / dx) * dx, math.floor(miny / dy) * dy,
                            math.floor(minz / dz) * dz)
    nx = max(1, int(math.ceil((maxx - minx) / dx)))
    ny = max(1, int(math.ceil((maxy - miny) / dy)))
    nz = max(1, int(math.ceil((maxz - minz) / dz)))
    bm = BlockModel([minx, miny, minz], [dx, dy, dz], [nx, ny, nz], name=name, azimuth=azimuth)
    bm.metadata['created_from'] = {'bounds': list(bounds)}
    return bm


def block_centroids(bm, mask=None):
    """List of (x, y, z) centroids (optionally only where mask[idx] is True)."""
    out = []
    nx, ny, nz = bm.count
    idx = 0
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                if mask is None or mask[idx]:
                    out.append(bm.centroid(i, j, k))
                idx += 1
    return out


def composite(points, value, length='length', target_length=None):
    """Length-weighted compositing of interval samples per hole: returns a new
    PointSet with one sample per ``target_length`` run (None = no-op)."""
    if not target_length:
        return points
    holes = {}
    for i in range(points.n):
        h = points.attributes.get('hole', [None] * points.n)[i]
        holes.setdefault(h, []).append(i)
    out = PointSet(name=points.name + ' composited', role='samples')
    fr = points.numeric('from')
    to = points.numeric('to')
    val = points.numeric(value)
    for h, ids in holes.items():
        ids.sort(key=lambda i: fr[i])
        if not ids or fr[ids[0]] != fr[ids[0]]:
            for i in ids:
                p = points.point(i)
                out.add(p[0], p[1], p[2], hole=h, **{value: val[i]})
            continue
        start = fr[ids[0]]
        end = to[ids[-1]]
        d = start
        while d < end - 1e-9:
            d1 = min(d + target_length, end)
            wsum = vsum = 0.0
            xs = ys = zs = 0.0
            for i in ids:
                a, b = max(fr[i], d), min(to[i], d1)
                if b <= a or val[i] != val[i]:
                    continue
                w = b - a
                wsum += w
                vsum += w * val[i]
                p = points.point(i)
                xs += w * p[0]
                ys += w * p[1]
                zs += w * p[2]
            if wsum > 0:
                out.add(xs / wsum, ys / wsum, zs / wsum, hole=h, **{'from': d, 'to': d1,
                        'length': wsum, value: vsum / wsum})
            d = d1
    return out


def estimate(bm, samples, value, method='ok', variogram=None, max_points=16, radius=None,
             min_points=2, power=2.0, domain=None, domain_value=None, out_name=None,
             sample_domain=None, progress=None):
    """Estimate ``value`` into every block (or the domain's blocks).

    samples: PointSet with a numeric column ``value``.
    method: 'ok' (ordinary kriging; needs ``variogram`` or one is fitted from
            the samples), 'idw', 'nn'.
    domain / domain_value: name of a category attribute on ``bm`` and the
            value that selects the blocks to estimate (others stay NaN).
    sample_domain: optional list of booleans (per sample) to restrict samples.
    Adds attributes <out>_est, <out>_var (ok), <out>_n (neighbours used) and
    records the parameters in bm.metadata['estimates'].
    """
    out = out_name or value
    pts = [samples.point(i) for i in range(samples.n)]
    vals = list(samples.numeric(value))
    if sample_domain is not None:
        pts = [p for p, keep in zip(pts, sample_domain) if keep]
        vals = [v for v, keep in zip(vals, sample_domain) if keep]
    mask = None
    if domain:
        cat = bm.attributes[domain]['values']
        mask = [c == domain_value for c in cat]
    targets = block_centroids(bm, mask)
    n = bm.n
    est_all = farray([NAN] * n)
    var_all = farray([NAN] * n)
    if method == 'ok':
        if variogram is None:
            exp = interp.empirical_variogram(pts, vals)
            variogram = interp.fit_variogram(exp) if exp else interp.Variogram(
                model='spherical', sill=1.0, range_=max(bm.count[a] * bm.block_size[a] for a in range(3)) / 2.0)
        est, var = interp.ordinary_kriging(pts, vals, targets, variogram, max_points=max_points,
                                           radius=radius, min_points=min_points)
    elif method == 'idw':
        est = interp.idw(pts, vals, targets, power=power, max_points=max_points, radius=radius)
        var = None
    elif method == 'nn':
        est = interp.nearest_neighbour(pts, vals, targets, radius=radius)
        var = None
    else:
        raise ValueError('unknown method %r' % method)
    pos = 0
    for idx in range(n):
        if mask is None or mask[idx]:
            est_all[idx] = est[pos]
            if var is not None:
                var_all[idx] = var[pos]
            pos += 1
    bm.add_attribute(out + '_est', est_all)
    if var is not None:
        bm.add_attribute(out + '_var', var_all)
    rec = {'attribute': out, 'method': method, 'samples': len(pts), 'max_points': max_points,
           'radius': radius, 'domain': domain, 'domain_value': domain_value}
    if variogram is not None:
        rec['variogram'] = variogram.to_json()
    bm.metadata.setdefault('estimates', []).append(rec)
    return bm


def grade_tonnage(bm, attribute, cutoffs, density=2.7, domain=None, domain_value=None):
    """Tonnes and mean grade above each cut-off (density t/m^3).  Block volume
    is assumed fully inside the domain (no partial blocks)."""
    vals = bm.attributes[attribute]['values']
    vol = bm.block_size[0] * bm.block_size[1] * bm.block_size[2]
    mask = None
    if domain:
        cat = bm.attributes[domain]['values']
        mask = [c == domain_value for c in cat]
    rows = []
    for c in cutoffs:
        n = 0
        s = 0.0
        for idx, v in enumerate(vals):
            if mask is not None and not mask[idx]:
                continue
            if v == v and v >= c:
                n += 1
                s += v
        rows.append({'cutoff': c, 'blocks': n, 'volume_m3': n * vol,
                     'tonnes': n * vol * density, 'mean_grade': (s / n) if n else NAN})
    return rows


def blockmodel_to_points(bm, attribute):
    """Block centroids with a value as a PointSet (for export / display)."""
    ps = PointSet(name='%s %s' % (bm.name, attribute), role='points')
    vals = bm.attributes[attribute]['values']
    nx, ny, nz = bm.count
    idx = 0
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                v = vals[idx]
                if v == v and v is not None:
                    x, y, z = bm.centroid(i, j, k)
                    ps.add(x, y, z, **{attribute: v})
                idx += 1
    return ps
