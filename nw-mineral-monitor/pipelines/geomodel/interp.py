"""geomodel.interp — spatial interpolation: inverse-distance weighting, radial
basis functions (the "implicit modelling" engine) and ordinary kriging with
variogram models, in 2-D or 3-D.

Everything works on plain Python sequences; numpy is used automatically when
importable (dense solves are ~100x faster) and every function has a pure
Python fallback so the pipelines keep their stdlib-only promise.

Vocabulary (Leapfrog's, so the two UIs read the same):
  * RBF kernels: 'linear' (|r|), 'cubic' (r^3), 'thin_plate' (r^2 log r),
    'gaussian' (exp(-(r/range)^2)), 'spheroidal' (spherical-covariance kernel
    with nugget / sill / range — the dual-kriging form of Leapfrog's
    spheroidal interpolant).  Drift: 'none' | 'constant' | 'linear'.
  * Variogram models for kriging: 'spherical' | 'exponential' | 'gaussian'
    | 'linear' | 'power' | 'nugget', each with nugget, sill (partial sill)
    and range; ranges may be anisotropic ([rx, ry, rz] + azimuth clockwise
    from north + dip + plunge, Leapfrog/Datamine style).
"""
import math

try:                                   # optional acceleration
    import numpy as _np
except Exception:                      # pragma: no cover - numpy absent
    _np = None

from .model import Grid2D, NAN, farray


# ------------------------------------------------------------------ helpers
def _pts(points):
    """Normalise point input to a list of (x, y, z) tuples.  Accepts a
    PointSet, a flat array, or a sequence of 2-/3-tuples."""
    if hasattr(points, 'xyz'):
        flat = points.xyz
        return [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat), 3)]
    points = list(points)
    if not points:
        return []
    if isinstance(points[0], (int, float)):
        return [(points[i], points[i + 1], points[i + 2]) for i in range(0, len(points), 3)]
    out = []
    for p in points:
        if len(p) == 2:
            out.append((float(p[0]), float(p[1]), 0.0))
        else:
            out.append((float(p[0]), float(p[1]), float(p[2])))
    return out


def _clean(points, values):
    pts, vals = [], []
    for p, v in zip(points, values):
        if v is None:
            continue
        v = float(v)
        if v != v or any(c != c for c in p):
            continue
        pts.append(p)
        vals.append(v)
    return pts, vals


class Anisotropy(object):
    """Ellipsoidal anisotropy: ranges along the major / semi-major / minor
    axes, oriented by azimuth (clockwise from north, degrees) of the major
    axis, dip of the major axis (positive down) and plunge of the semi-major
    axis about it (rotation in the plane normal to the major axis).  Distances
    are measured in the transformed isotropic space (range 1)."""

    def __init__(self, ranges, azimuth=0.0, dip=0.0, plunge=0.0, dim=3):
        if isinstance(ranges, (int, float)):
            ranges = [ranges] * 3
        ranges = list(ranges) + [ranges[-1]] * (3 - len(ranges))
        self.ranges = [float(max(r, 1e-12)) for r in ranges[:3]]
        self.azimuth, self.dip, self.plunge = float(azimuth), float(dip), float(plunge)
        self.dim = dim
        self.rot = self._matrix()

    def _matrix(self):
        az, dp, pl = (math.radians(self.azimuth), math.radians(self.dip), math.radians(self.plunge))
        # major axis direction from azimuth/dip; build orthonormal frame
        ca, sa, cd, sd = math.cos(az), math.sin(az), math.cos(dp), math.sin(dp)
        major = (sa * cd, ca * cd, -sd)            # east, north, up
        horiz = (ca, -sa, 0.0)                     # horizontal, perpendicular in plan
        minor0 = (-sa * sd, -ca * sd, -cd)         # completes right-handed frame
        cp, sp = math.cos(pl), math.sin(pl)
        semi = tuple(cp * horiz[i] + sp * minor0[i] for i in range(3))
        minor = tuple(-sp * horiz[i] + cp * minor0[i] for i in range(3))
        return (major, semi, minor)

    def transform(self, dx, dy, dz=0.0):
        out = []
        for axis, r in zip(self.rot, self.ranges):
            out.append((axis[0] * dx + axis[1] * dy + axis[2] * dz) / r)
        if self.dim == 2:
            return out[0], out[1], 0.0
        return tuple(out)

    def distance(self, a, b):
        u, v, w = self.transform(b[0] - a[0], b[1] - a[1], b[2] - a[2])
        return math.sqrt(u * u + v * v + w * w)


def _dist(a, b, dim=3):
    if dim == 2:
        return math.hypot(b[0] - a[0], b[1] - a[1])
    return math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2)


class _GridIndex(object):
    """Uniform-cell spatial hash for neighbour searches."""

    def __init__(self, pts, dim=3, cell=None):
        self.pts = pts
        self.dim = dim
        if not pts:
            self.cell = 1.0
            self.cells = {}
            return
        mins = [min(p[a] for p in pts) for a in range(3)]
        maxs = [max(p[a] for p in pts) for a in range(3)]
        span = max(maxs[a] - mins[a] for a in range(dim)) or 1.0
        self.cell = cell or max(span / max(1.0, len(pts) ** (1.0 / dim)) * 2.0, 1e-9)
        self.cells = {}
        keys = [self._key(p) for p in pts]
        for i, key in enumerate(keys):
            self.cells.setdefault(key, []).append(i)
        self.kmin = [min(k[a] for k in keys) for a in range(3)]
        self.kmax = [max(k[a] for k in keys) for a in range(3)]

    def _key(self, p):
        if self.dim == 2:
            return (int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell)), 0)
        return (int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell)),
                int(math.floor(p[2] / self.cell)))

    def nearest(self, q, k, radius=None, metric=None, metric_floor=1.0):
        """Up to k nearest (distance, index) pairs within ``radius`` (in metric
        units).  ``metric_floor`` is a lower bound on metric/euclidean distance
        (1/max range for anisotropic metrics) so ring expansion terminates."""
        if not self.pts:
            return []
        metric = metric or (lambda a, b: _dist(a, b, self.dim))
        kq = self._key(q)
        found = []
        max_ring = max(max(abs(kq[a] - self.kmin[a]), abs(kq[a] - self.kmax[a]))
                       for a in range(self.dim)) + 1
        if radius is not None:
            max_ring = min(max_ring, int(math.ceil(radius / metric_floor / self.cell)) + 1)
        R = 0
        while R <= max_ring:
            for _key, idxs in self._ring(kq, R):
                for i in idxs:
                    d = metric(q, self.pts[i])
                    if radius is None or d <= radius:
                        found.append((d, i))
            if len(found) >= k:
                found.sort()
                # every unscanned point is >= R*cell away (euclidean)
                if found[k - 1][0] <= R * self.cell * metric_floor:
                    return found[:k]
            R += 1
        found.sort()
        return found[:k]

    def _ring(self, kq, ring):
        out = []
        rng = range(-ring, ring + 1)
        zr = rng if self.dim == 3 else [0]
        for dx in rng:
            for dy in rng:
                for dz in zr:
                    if max(abs(dx), abs(dy), abs(dz)) != ring:
                        continue
                    key = (kq[0] + dx, kq[1] + dy, kq[2] + dz)
                    idxs = self.cells.get(key)
                    if idxs:
                        out.append((key, idxs))
        return out


# ---------------------------------------------------------------------- IDW
def idw(points, values, targets, power=2.0, max_points=16, radius=None, dim=3,
        anisotropy=None):
    """Inverse-distance-weighted estimates at ``targets``. Returns a list of
    floats (NaN where no neighbour is found)."""
    pts, vals = _clean(_pts(points), values)
    tg = _pts(targets)
    if not pts:
        return [NAN] * len(tg)
    metric = anisotropy.distance if anisotropy else None
    floor = 1.0 / max(anisotropy.ranges) if anisotropy else 1.0
    idx = _GridIndex(pts, dim)
    out = []
    for q in tg:
        nb = idx.nearest(q, max_points, radius, metric, floor)
        if not nb:
            out.append(NAN)
            continue
        if nb[0][0] < 1e-12:
            out.append(vals[nb[0][1]])
            continue
        wsum = vsum = 0.0
        for d, i in nb:
            w = 1.0 / d ** power
            wsum += w
            vsum += w * vals[i]
        out.append(vsum / wsum)
    return out


def nearest_neighbour(points, values, targets, dim=3, radius=None):
    pts, vals = _clean(_pts(points), values)
    tg = _pts(targets)
    idx = _GridIndex(pts, dim)
    out = []
    for q in tg:
        nb = idx.nearest(q, 1, radius)
        out.append(vals[nb[0][1]] if nb else NAN)
    return out


# ---------------------------------------------------------------- variograms
VARIOGRAM_MODELS = ('spherical', 'exponential', 'gaussian', 'linear', 'power', 'nugget')


class Variogram(object):
    """Nested variogram model: nugget + sum of structures.  Each structure is
    {'model': 'spherical', 'sill': c, 'range': a} (+ optional 'exponent'
    for 'power', + optional 'anisotropy': Anisotropy)."""

    def __init__(self, nugget=0.0, structures=None, model=None, sill=1.0, range_=1.0,
                 anisotropy=None):
        self.nugget = float(nugget)
        if structures is None:
            structures = [{'model': model or 'spherical', 'sill': float(sill),
                           'range': float(range_)}]
        self.structures = []
        for s in structures:
            s = dict(s)
            s.setdefault('model', 'spherical')
            s.setdefault('sill', 1.0)
            s.setdefault('range', 1.0)
            if s['model'] not in VARIOGRAM_MODELS:
                raise ValueError('unknown variogram model %r' % s['model'])
            self.structures.append(s)
        self.anisotropy = anisotropy

    @property
    def sill(self):
        return self.nugget + sum(s['sill'] for s in self.structures)

    @staticmethod
    def structure_gamma(model, h, sill, a, exponent=1.0):
        if h <= 0:
            return 0.0
        if model == 'nugget':
            return sill
        if model == 'spherical':
            if h >= a:
                return sill
            r = h / a
            return sill * (1.5 * r - 0.5 * r ** 3)
        if model == 'exponential':
            return sill * (1.0 - math.exp(-3.0 * h / a))
        if model == 'gaussian':
            return sill * (1.0 - math.exp(-3.0 * (h / a) ** 2))
        if model == 'linear':
            return sill * h / a
        if model == 'power':
            return sill * (h / a) ** exponent
        raise ValueError(model)

    def gamma(self, h):
        """Semivariance at isotropic lag h."""
        if h <= 0:
            return 0.0
        g = self.nugget
        for s in self.structures:
            g += self.structure_gamma(s['model'], h, s['sill'], s['range'], s.get('exponent', 1.0))
        return g

    def gamma_vec(self, a, b):
        """Semivariance between two points (honours anisotropy: the lag is
        measured in the ellipsoid's unit space and ranges are then 1)."""
        if self.anisotropy is not None:
            h = self.anisotropy.distance(a, b)
            if h <= 0:
                return 0.0
            g = self.nugget
            for s in self.structures:
                g += self.structure_gamma(s['model'], h, s['sill'], 1.0, s.get('exponent', 1.0))
            return g
        return self.gamma(_dist(a, b))

    def covariance(self, a, b):
        return self.sill - self.gamma_vec(a, b)

    def to_json(self):
        d = {'nugget': self.nugget, 'structures': [dict(s) for s in self.structures]}
        if self.anisotropy:
            d['anisotropy'] = {'ranges': self.anisotropy.ranges, 'azimuth': self.anisotropy.azimuth,
                               'dip': self.anisotropy.dip, 'plunge': self.anisotropy.plunge}
        for s in d['structures']:
            s.pop('anisotropy', None)
        return d

    @classmethod
    def from_json(cls, d):
        an = d.get('anisotropy')
        return cls(d.get('nugget', 0.0), d.get('structures'),
                   anisotropy=Anisotropy(an['ranges'], an.get('azimuth', 0), an.get('dip', 0),
                                         an.get('plunge', 0)) if an else None)


def empirical_variogram(points, values, n_lags=12, lag_size=None, azimuth=None,
                        tolerance=22.5, dim=3, max_pairs=2000000):
    """Experimental semivariogram: returns list of {'lag','gamma','pairs'}.
    ``azimuth`` (deg clockwise from north) restricts pairs to a direction
    band of ±tolerance in plan; None = omnidirectional."""
    pts, vals = _clean(_pts(points), values)
    n = len(pts)
    if n < 2:
        return []
    if lag_size is None:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
        lag_size = span / (2.0 * n_lags)
    sums = [0.0] * n_lags
    cnt = [0] * n_lags
    pairs = 0
    az = math.radians(azimuth) if azimuth is not None else None
    for i in range(n):
        for j in range(i + 1, n):
            pairs += 1
            if pairs > max_pairs:
                break
            h = _dist(pts[i], pts[j], dim)
            k = int(h / lag_size)
            if k >= n_lags or h <= 0:
                continue
            if az is not None:
                dx, dy = pts[j][0] - pts[i][0], pts[j][1] - pts[i][1]
                if dx == 0 and dy == 0:
                    continue
                ang = math.atan2(dx, dy)               # clockwise from north
                dang = abs((ang - az + math.pi / 2) % math.pi - math.pi / 2)
                if math.degrees(dang) > tolerance:
                    continue
            sums[k] += 0.5 * (vals[i] - vals[j]) ** 2
            cnt[k] += 1
    return [{'lag': (k + 0.5) * lag_size, 'gamma': sums[k] / cnt[k], 'pairs': cnt[k]}
            for k in range(n_lags) if cnt[k]]


def fit_variogram(experimental, model='spherical', nugget=None):
    """Cheap grid-search fit of a one-structure model to an experimental
    variogram (good enough to seed the UI's sliders)."""
    if not experimental:
        raise ValueError('empty experimental variogram')
    lags = [e['lag'] for e in experimental]
    gam = [e['gamma'] for e in experimental]
    wts = [e['pairs'] for e in experimental]
    gmax = max(gam) or 1.0
    best = None
    for rng_f in [0.2 + 0.05 * k for k in range(36)]:
        a = rng_f * max(lags) * 1.5
        for sill_f in [0.4 + 0.05 * k for k in range(25)]:
            c = sill_f * gmax
            for nug_f in ([0.0] if nugget is not None else [0.0, 0.05, 0.1, 0.2, 0.3, 0.4]):
                n0 = nugget if nugget is not None else nug_f * gmax
                err = 0.0
                for h, g, w in zip(lags, gam, wts):
                    m = n0 + Variogram.structure_gamma(model, h, max(c - n0, 1e-12), a)
                    err += w * (m - g) ** 2
                if best is None or err < best[0]:
                    best = (err, n0, c, a)
    _, n0, c, a = best
    return Variogram(nugget=n0, structures=[{'model': model, 'sill': max(c - n0, 1e-12), 'range': a}])


# -------------------------------------------------------------- linear algebra
def solve_dense(A, b):
    """Solve A x = b (lists of lists). numpy when available, else Gaussian
    elimination with partial pivoting.  Returns list of floats."""
    n = len(A)
    if _np is not None:
        try:
            x = _np.linalg.solve(_np.asarray(A, dtype=float), _np.asarray(b, dtype=float))
            return [float(v) for v in x]
        except Exception:
            x, _res, _rank, _sv = _np.linalg.lstsq(_np.asarray(A, dtype=float),
                                                   _np.asarray(b, dtype=float), rcond=None)
            return [float(v) for v in x]
    M = [list(map(float, row)) + [float(b[i])] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-14:
            M[c][c] += 1e-10          # regularise a singular pivot
            piv = c
        if piv != c:
            M[c], M[piv] = M[piv], M[c]
        pv = M[c][c]
        for r in range(c + 1, n):
            f = M[r][c] / pv
            if f == 0.0:
                continue
            rowc = M[c]
            rowr = M[r]
            for k in range(c, n + 1):
                rowr[k] -= f * rowc[k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n]
        for k in range(r + 1, n):
            s -= M[r][k] * x[k]
        x[r] = s / M[r][r]
    return x


# ------------------------------------------------------------- kriging (OK)
def ordinary_kriging(points, values, targets, variogram, max_points=24, radius=None,
                     min_points=2, dim=3, return_variance=True):
    """Ordinary kriging at ``targets`` with a moving neighbourhood.
    Returns (estimates, variances) — variances None when not requested."""
    pts, vals = _clean(_pts(points), values)
    tg = _pts(targets)
    est, var = [], []
    if len(pts) < min_points:
        return [NAN] * len(tg), ([NAN] * len(tg) if return_variance else None)
    metric = variogram.anisotropy.distance if variogram.anisotropy else None
    floor = 1.0 / max(variogram.anisotropy.ranges) if variogram.anisotropy else 1.0
    index = _GridIndex(pts, dim)
    cache = {}
    for q in tg:
        nb = index.nearest(q, max_points, radius, metric, floor)
        if len(nb) < min_points:
            est.append(NAN)
            var.append(NAN)
            continue
        if nb[0][0] < 1e-12:
            est.append(vals[nb[0][1]])
            var.append(0.0)
            continue
        ids = tuple(i for _d, i in nb)
        n = len(ids)
        key = ids
        A = cache.get(key)
        if A is None:
            A = [[0.0] * (n + 1) for _ in range(n + 1)]
            for r in range(n):
                pr = pts[ids[r]]
                for c in range(r, n):
                    g = variogram.gamma_vec(pr, pts[ids[c]]) if c != r else 0.0
                    A[r][c] = g
                    A[c][r] = g
                A[r][n] = 1.0
                A[n][r] = 1.0
            A[n][n] = 0.0
            if len(cache) < 512:
                cache[key] = A
        b = [variogram.gamma_vec(q, pts[i]) for i in ids] + [1.0]
        w = solve_dense(A, b)
        e = sum(w[r] * vals[ids[r]] for r in range(n))
        est.append(e)
        if return_variance:
            v = sum(w[r] * b[r] for r in range(n)) + w[n]
            var.append(max(v, 0.0) if v == v else NAN)
    return est, (var if return_variance else None)


def simple_kriging(points, values, targets, variogram, mean, max_points=24, radius=None, dim=3):
    """Simple kriging with a known mean (used for indicator / residual work)."""
    pts, vals = _clean(_pts(points), values)
    tg = _pts(targets)
    metric = variogram.anisotropy.distance if variogram.anisotropy else None
    floor = 1.0 / max(variogram.anisotropy.ranges) if variogram.anisotropy else 1.0
    index = _GridIndex(pts, dim)
    sill = variogram.sill
    out = []
    for q in tg:
        nb = index.nearest(q, max_points, radius, metric, floor)
        if not nb:
            out.append(NAN)
            continue
        ids = [i for _d, i in nb]
        n = len(ids)
        A = [[sill - variogram.gamma_vec(pts[ids[r]], pts[ids[c]]) for c in range(n)] for r in range(n)]
        b = [sill - variogram.gamma_vec(q, pts[i]) for i in ids]
        w = solve_dense(A, b)
        out.append(mean + sum(w[r] * (vals[ids[r]] - mean) for r in range(n)))
    return out


# --------------------------------------------------------------------- RBF
RBF_KERNELS = ('linear', 'cubic', 'thin_plate', 'gaussian', 'spheroidal', 'multiquadric')


def _kernel(kind, r, eps, params):
    if kind == 'linear':
        return r
    if kind == 'cubic':
        return r ** 3
    if kind == 'thin_plate':
        return 0.0 if r <= 0 else r * r * math.log(r)
    if kind == 'gaussian':
        return math.exp(-(r / eps) ** 2)
    if kind == 'multiquadric':
        return math.sqrt(r * r + eps * eps)
    if kind == 'spheroidal':
        # spherical covariance: sill - gamma (nugget handled on the diagonal)
        a = params.get('range', eps)
        c = params.get('sill', 1.0)
        if r >= a:
            return 0.0
        x = r / a
        return c * (1.0 - 1.5 * x + 0.5 * x ** 3)
    raise ValueError('unknown RBF kernel %r' % kind)


class RBF(object):
    """Radial-basis-function interpolant with optional polynomial drift and
    smoothing (Tikhonov / nugget on the diagonal).  Fit once, evaluate many.

    For implicit surfaces fit signed distances (0 on contact points, ±d on
    off-surface points) and iso-surface the evaluation at 0.
    """

    def __init__(self, kernel='thin_plate', drift='linear', smoothing=0.0, epsilon=None,
                 dim=3, anisotropy=None, **params):
        if kernel not in RBF_KERNELS:
            raise ValueError('unknown RBF kernel %r' % kernel)
        self.kernel = kernel
        self.drift = drift if drift in ('none', 'constant', 'linear') else 'linear'
        self.smoothing = float(smoothing)
        self.epsilon = epsilon
        self.dim = dim
        self.anisotropy = anisotropy
        self.params = dict(params)
        self.centers = []
        self.weights = []
        self.poly = []
        self.scale = 1.0
        self.offset = (0.0, 0.0, 0.0)

    def _ndrift(self):
        return {'none': 0, 'constant': 1, 'linear': 1 + self.dim}[self.drift]

    def _poly_terms(self, p):
        if self.drift == 'none':
            return []
        t = [1.0]
        if self.drift == 'linear':
            u = self._local(p)
            t.extend(u[:self.dim])
        return t

    def _local(self, p):
        return ((p[0] - self.offset[0]) / self.scale, (p[1] - self.offset[1]) / self.scale,
                (p[2] - self.offset[2]) / self.scale)

    def _r(self, a, b):
        if self.anisotropy is not None:
            return self.anisotropy.distance(a, b) * self.scale_aniso
        return _dist(self._local(a), self._local(b), self.dim)

    def fit(self, points, values):
        pts, vals = _clean(_pts(points), values)
        n = len(pts)
        if n == 0:
            raise ValueError('no valid points to fit')
        # normalise coordinates for conditioning
        mins = [min(p[a] for p in pts) for a in range(3)]
        maxs = [max(p[a] for p in pts) for a in range(3)]
        self.offset = tuple((mins[a] + maxs[a]) / 2.0 for a in range(3))
        span = max(maxs[a] - mins[a] for a in range(self.dim)) or 1.0
        self.scale = span
        # one length unit for both paths (G-44): the isotropic kernel sees
        # |d| / span, so the ellipsoid distance (|d| / range per axis) is
        # rescaled by the major range — a 1:1:1 anisotropy reproduces the
        # isotropic fit exactly and the normalised epsilon means the same thing
        self.scale_aniso = (max(self.anisotropy.ranges) / span) if self.anisotropy is not None else 1.0
        eps = self.epsilon
        if eps is None:
            eps = 0.25 if self.kernel in ('gaussian', 'multiquadric') else 1.0
        else:
            eps = eps / span
        if self.kernel == 'spheroidal':
            self.params.setdefault('range', span)
            self.params['range_local'] = self.params['range'] / span
        self._eps = eps
        nd = self._ndrift()
        N = n + nd
        A = [[0.0] * N for _ in range(N)]
        b = [0.0] * N
        kp = dict(self.params)
        if self.kernel == 'spheroidal':
            kp['range'] = self.params['range_local']
        for i in range(n):
            pi = pts[i]
            for j in range(i, n):
                v = _kernel(self.kernel, self._r(pi, pts[j]), eps, kp)
                A[i][j] = v
                A[j][i] = v
            A[i][i] += self.smoothing
            pt = self._poly_terms(pi)
            for k in range(nd):
                A[i][n + k] = pt[k]
                A[n + k][i] = pt[k]
            b[i] = vals[i]
        w = solve_dense(A, b)
        self.centers = pts
        self.values = vals
        self.weights = w[:n]
        self.poly = w[n:]
        self._kp = kp
        return self

    def predict(self, targets):
        tg = _pts(targets)
        out = []
        n = len(self.centers)
        for q in tg:
            s = 0.0
            for i in range(n):
                s += self.weights[i] * _kernel(self.kernel, self._r(q, self.centers[i]), self._eps, self._kp)
            pt = self._poly_terms(q)
            for k, c in enumerate(self.poly):
                s += c * pt[k]
            out.append(s)
        return out

    def predict_np(self, targets):
        """numpy-vectorised predict (falls back to predict())."""
        if _np is None or self.anisotropy is not None:
            return self.predict(targets)
        tg = _pts(targets)
        if not tg:
            return []
        T = _np.asarray(tg, dtype=float)
        C = _np.asarray(self.centers, dtype=float)
        off = _np.asarray(self.offset)
        Tl = (T - off) / self.scale
        Cl = (C - off) / self.scale
        if self.dim == 2:
            Tl = Tl[:, :2]
            Cl = Cl[:, :2]
        out = _np.zeros(len(tg))
        W = _np.asarray(self.weights)
        chunk = max(1, int(2e6 // max(1, len(C))))
        for s in range(0, len(tg), chunk):
            blk = Tl[s:s + chunk]
            d = _np.sqrt(((blk[:, None, :] - Cl[None, :, :]) ** 2).sum(axis=2))
            k = self.kernel
            eps = self._eps
            if k == 'linear':
                K = d
            elif k == 'cubic':
                K = d ** 3
            elif k == 'thin_plate':
                with _np.errstate(divide='ignore', invalid='ignore'):
                    K = _np.where(d > 0, d * d * _np.log(_np.where(d > 0, d, 1.0)), 0.0)
            elif k == 'gaussian':
                K = _np.exp(-(d / eps) ** 2)
            elif k == 'multiquadric':
                K = _np.sqrt(d * d + eps * eps)
            else:   # spheroidal
                a = self._kp.get('range', eps)
                c = self._kp.get('sill', 1.0)
                x = _np.minimum(d / a, 1.0)
                K = c * (1.0 - 1.5 * x + 0.5 * x ** 3)
            out[s:s + chunk] = K.dot(W)
        if self.poly:
            P = _np.ones((len(tg), 1))
            if self.drift == 'linear':
                P = _np.hstack([P, Tl[:, :self.dim]])
            out += P.dot(_np.asarray(self.poly))
        return [float(v) for v in out]


# ------------------------------------------------------------- elevation
class _MeshXYIndex(object):
    """Plan-view index of a mesh: a _GridIndex of triangle centroids plus the
    largest centroid-to-vertex reach (the JS ``meshXYIndex``)."""

    def __init__(self, mesh):
        self.mesh = mesh
        V = mesh.vertices
        nt = mesh.n_triangles
        C = []
        reach = 0.0
        for t in range(nt):
            a, b, c = mesh.triangle(t)
            cx = (V[3 * a] + V[3 * b] + V[3 * c]) / 3.0
            cy = (V[3 * a + 1] + V[3 * b + 1] + V[3 * c + 1]) / 3.0
            C.append((cx, cy, 0.0))
            for i in (a, b, c):
                reach = max(reach, math.sqrt((V[3 * i] - cx) ** 2 + (V[3 * i + 1] - cy) ** 2))
        self.index = _GridIndex(C, dim=2)
        self.reach = reach * (1 + 1e-9) + 1e-9
        self.k = min(max(nt, 1), 512)


def _in_tri(p, a, b, c):
    def s(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = s(p, a, b), s(p, b, c), s(p, c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def mesh_z_at(mesh, x, y, index=None):
    """z of a mesh under / over (x, y): the highest triangle covering the point
    in plan (a vertical ray); NaN where none does."""
    ix = index or _MeshXYIndex(mesh)
    m = ix.mesh
    V = m.vertices
    best = NAN
    for _d, t in ix.index.nearest((x, y, 0.0), ix.k, radius=ix.reach):
        a, b, c = m.triangle(t)
        ax, ay, bx, by, cx, cy = V[3 * a], V[3 * a + 1], V[3 * b], V[3 * b + 1], V[3 * c], V[3 * c + 1]
        if not _in_tri((x, y), (ax, ay), (bx, by), (cx, cy)):
            continue
        det = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        if abs(det) < 1e-300:
            continue
        l1 = ((bx - x) * (cy - y) - (cx - x) * (by - y)) / det
        l2 = ((cx - x) * (ay - y) - (ax - x) * (cy - y)) / det
        l3 = 1 - l1 - l2
        z = l1 * V[3 * a + 2] + l2 * V[3 * b + 2] + l3 * V[3 * c + 2]
        if best != best or z > best:
            best = z
    return best


ELEVATION_SCOPES = ('missing', 'all', 'not-surveyed')


def _surface_sampler(surface):
    if surface.kind == 'grid2d':
        return surface.sample
    if surface.kind == 'mesh':
        ix = _MeshXYIndex(surface)
        return lambda x, y: mesh_z_at(surface, x, y, ix)
    raise ValueError('cannot take elevations from a %s' % surface.kind)


def set_elevation_from(target, surface, offset=0.0, only='all'):
    """Leapfrog's Set Elevation, generalised (the JS ``setElevationFrom``).
    target: PointSet (xyz), LineSet (every vertex) or Drillholes (collars);
    surface: Grid2D (bilinear sample) or Mesh (vertical ray).  ``only``:
    'missing' (z NaN or 0) | 'all' | 'not-surveyed' (rows / parts / collars
    whose confidence is not 'surveyed').  The original z is kept (a
    ``z_original`` column, a per-feature list, a collar field) for
    ``restore_elevation``.  Returns {'moved', 'outside', 'skipped'}."""
    off = float(offset or 0)
    if only not in ELEVATION_SCOPES:
        raise ValueError('only must be one of %s' % ' | '.join(ELEVATION_SCOPES))
    z_at = _surface_sampler(surface)
    stats = {'moved': 0, 'outside': 0, 'skipped': 0}

    def missing(z):
        return z != z or z == 0

    label = surface.name + (' (+%g m)' % off if off else '')
    if target.kind == 'points':
        keep = list(target.attributes.get('z_original') or [])
        conf = target.attributes.get('confidence') or []
        while len(keep) < target.n:
            keep.append(None)
        for i in range(target.n):
            z0 = target.xyz[3 * i + 2]
            if (only == 'missing' and not missing(z0)) or (only == 'not-surveyed' and i < len(conf) and conf[i] == 'surveyed'):
                stats['skipped'] += 1
                continue
            z = z_at(target.xyz[3 * i], target.xyz[3 * i + 1])
            if z != z:
                stats['outside'] += 1
                continue
            if keep[i] is None:
                keep[i] = z0
            target.xyz[3 * i + 2] = z + off
            stats['moved'] += 1
        target.attributes['z_original'] = keep
    elif target.kind == 'lineset':
        while len(target.features) < len(target.parts):
            target.features.append({})
        for k, idx in enumerate(target.parts):
            f = target.features[k]
            if only == 'not-surveyed' and f.get('confidence') == 'surveyed':
                stats['skipped'] += len(idx)
                continue
            keep = list(f['z_original']) if isinstance(f.get('z_original'), list) else [None] * len(idx)
            touched = False
            for q, vi in enumerate(idx):
                z0 = target.vertices[3 * vi + 2]
                if only == 'missing' and not missing(z0):
                    stats['skipped'] += 1
                    continue
                z = z_at(target.vertices[3 * vi], target.vertices[3 * vi + 1])
                if z != z:
                    stats['outside'] += 1
                    continue
                if keep[q] is None:
                    keep[q] = z0
                target.vertices[3 * vi + 2] = z + off
                stats['moved'] += 1
                touched = True
            if touched:
                f['z_original'] = keep
    elif target.kind == 'drillholes':
        for c in target.collars:
            z0 = NAN if c.get('z') in (None, '') else float(c['z'])
            if (only == 'missing' and not missing(z0)) or (only == 'not-surveyed' and c.get('confidence') == 'surveyed'):
                stats['skipped'] += 1
                continue
            z = z_at(float(c['x']), float(c['y']))
            if z != z:
                stats['outside'] += 1
                continue
            if c.get('z_original') is None:
                c['z_original'] = z0 if z0 == z0 else None
            c['z'] = z + off
            stats['moved'] += 1
        target._traces = None
    else:
        raise ValueError('cannot set the elevation of a %s' % target.kind)
    target.metadata['elevation_from'] = label
    if stats['outside']:
        target.metadata.setdefault('warnings', []).append(
            '%d point(s) fell outside %s and kept their original elevation' % (stats['outside'], surface.name))
    return stats


def restore_elevation(target):
    """Undo set_elevation_from: put the kept original z back, drop the record."""
    restored = 0
    if target.kind == 'points':
        keep = target.attributes.pop('z_original', None)
        if keep:
            for i in range(min(target.n, len(keep))):
                if keep[i] is not None:
                    target.xyz[3 * i + 2] = float(keep[i])
                    restored += 1
    elif target.kind == 'lineset':
        for k, idx in enumerate(target.parts):
            f = target.features[k] if k < len(target.features) else None
            if not f or not isinstance(f.get('z_original'), list):
                continue
            for q, vi in enumerate(idx):
                if q < len(f['z_original']) and f['z_original'][q] is not None:
                    target.vertices[3 * vi + 2] = float(f['z_original'][q])
                    restored += 1
            del f['z_original']
    elif target.kind == 'drillholes':
        for c in target.collars:
            if 'z_original' in c:
                if c['z_original'] is not None:
                    c['z'] = float(c['z_original'])
                    restored += 1
                del c['z_original']
        target._traces = None
    else:
        raise ValueError('cannot restore the elevation of a %s' % target.kind)
    target.metadata.pop('elevation_from', None)
    return restored


# ------------------------------------------------------------ convenience
def grid_spec_from_points(points, cell=None, n=80, pad=0.05):
    """(x0, y0, dx, dy, nx, ny) covering the points with a margin."""
    pts = _pts(points)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = (x1 - x0) or 1.0, (y1 - y0) or 1.0
    x0 -= w * pad
    x1 += w * pad
    y0 -= h * pad
    y1 += h * pad
    if cell is None:
        cell = max(x1 - x0, y1 - y0) / float(n)
    nx = int(math.ceil((x1 - x0) / cell)) + 1
    ny = int(math.ceil((y1 - y0) / cell)) + 1
    return x0, y0, cell, cell, nx, ny


def grid_from_points(points, values, method='rbf', spec=None, cell=None, n=80,
                     name='surface', **params):
    """Interpolate scattered (x, y, value) samples onto a Grid2D.
    method: 'rbf' | 'idw' | 'ok' (ordinary kriging) | 'nn'.
    spec = (x0, y0, dx, dy, nx, ny) or None to auto-fit around the points."""
    pts = _pts(points)
    vals = list(values)
    if spec is None:
        spec = grid_spec_from_points(pts, cell=cell, n=n)
    x0, y0, dx, dy, nx, ny = spec
    g = Grid2D(nx, ny, x0, y0, dx, dy, name=name)
    targets = []
    for j in range(ny):
        for i in range(nx):
            x, y = g.node_xy(i, j)
            targets.append((x, y, 0.0))
    pts2 = [(p[0], p[1], 0.0) for p in pts]
    if method == 'rbf':
        rbf = RBF(kernel=params.pop('kernel', 'thin_plate'), drift=params.pop('drift', 'linear'),
                  smoothing=params.pop('smoothing', 0.0), dim=2, **params)
        rbf.fit(pts2, vals)
        est = rbf.predict_np(targets)
    elif method == 'idw':
        est = idw(pts2, vals, targets, power=params.get('power', 2.0),
                  max_points=params.get('max_points', 12), radius=params.get('radius'), dim=2)
    elif method == 'ok':
        vg = params.get('variogram')
        if vg is None:
            exp = empirical_variogram(pts2, vals, dim=2)
            vg = fit_variogram(exp) if exp else Variogram(model='spherical', sill=1.0, range_=max(dx * nx, dy * ny))
        est, _ = ordinary_kriging(pts2, vals, targets, vg, max_points=params.get('max_points', 16),
                                  radius=params.get('radius'), dim=2, return_variance=False)
    elif method == 'nn':
        est = nearest_neighbour(pts2, vals, targets, dim=2)
    else:
        raise ValueError('unknown method %r' % method)
    g.values = farray(est)
    g.metadata['interpolation'] = {'method': method, 'n_points': len(pts), 'params': _jsonable(params)}
    return g


def _jsonable(d):
    out = {}
    for k, v in d.items():
        if isinstance(v, Variogram):
            out[k] = v.to_json()
        elif isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out
