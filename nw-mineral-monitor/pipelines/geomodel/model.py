"""geomodel.model — in-memory geological-model objects and the JSON project
container shared by the Python pipelines and site/model3d.html.

Conventions (every format module honours these so objects round-trip):

* Coordinates are projected (WGS84 / UTM, metres) unless ``Project.crs``
  says otherwise; Z is elevation (positive up).
* ``Grid2D`` is NODE-registered: node (i, j) sits at
  ``x0 + i*dx`` , ``y0 + j*dy`` (then rotated ``rotation`` degrees CCW about
  (x0, y0)); values are row-major with j (south -> north) OUTER and i
  (west -> east) INNER, i.e. ``values[j*nx + i]``.  The first value is the
  south-west node — the Surfer / Geosoft(KX=1) / GXF(SENSE=1) order.
  ``float('nan')`` marks no-data.
* ``Mesh`` = flat XYZ vertex list + flat triangle index triples (0-based,
  CCW seen from the outward normal).
* ``LineSet`` = flat XYZ vertices + flat segment pairs; ``parts`` optionally
  groups vertices into ordered polylines (workings, fault traces...).
* ``BlockModel`` is a regular grid of cells; attribute i + nx*(j + ny*k) is
  block (i, j, k) with i along X fastest — the OMF "U fastest" order.
* Arrays inside the JSON project are ``{"@f64": "<base64>"}`` /
  ``{"@f32": ...}`` / ``{"@u32": ...}`` little-endian typed-array blobs so
  the browser can wrap them with ``Float64Array`` etc. without parsing.
"""
import array
import base64
import json
import math
import struct
import sys
import time

from . import SCHEMA

NAN = float('nan')


def isnan(v):
    return v is None or (isinstance(v, float) and v != v)


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# ------------------------------------------------------------------ arrays
def farray(values=None):
    """Double array (array('d')) from any iterable; NaN for None."""
    a = array.array('d')
    if values is not None:
        a.extend(NAN if v is None else float(v) for v in values)
    return a


def iarray(values=None):
    a = array.array('I')
    if values is not None:
        a.extend(int(v) for v in values)
    return a


def _b64(arr, typecode):
    a = array.array(typecode, arr) if not (isinstance(arr, array.array) and arr.typecode == typecode) else arr
    if sys.byteorder != 'little':
        a = array.array(typecode, a)
        a.byteswap()
    return base64.b64encode(a.tobytes()).decode('ascii')


def encode_array(arr, kind='f64'):
    """Encode a numeric sequence as a typed-array JSON blob."""
    if kind == 'f64':
        return {'@f64': _b64(arr, 'd')}
    if kind == 'f32':
        return {'@f32': _b64(arr, 'f')}
    if kind == 'u32':
        return {'@u32': _b64(arr, 'I')}
    if kind == 'i32':
        return {'@i32': _b64(arr, 'i')}
    raise ValueError(kind)


def decode_array(obj):
    """Inverse of encode_array; also accepts plain JSON lists."""
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        raise ValueError('not an array blob')
    for key, tc in (('@f64', 'd'), ('@f32', 'f'), ('@u32', 'I'), ('@i32', 'i')):
        if key in obj:
            a = array.array(tc)
            a.frombytes(base64.b64decode(obj[key]))
            if sys.byteorder != 'little':
                a.byteswap()
            return a
    raise ValueError('unknown array blob %r' % list(obj)[:3])


# ------------------------------------------------------------------- base
class ModelObject(object):
    """Common fields: id, name, color [r,g,b], visible, opacity, group,
    provenance (free dict: source, url, page, retrieved...), metadata."""
    kind = 'object'
    _seq = 0

    def __init__(self, name='', color=None, oid=None, provenance=None,
                 metadata=None, visible=True, opacity=1.0, group=''):
        ModelObject._seq += 1
        self.id = oid or ('%s-%d-%x' % (self.kind, ModelObject._seq, int(time.time() * 1000) & 0xffffff))
        self.name = name
        self.color = list(color) if color else [160, 160, 160]
        self.visible = visible
        self.opacity = opacity
        self.group = group
        self.provenance = dict(provenance or {})
        self.metadata = dict(metadata or {})

    def _head(self):
        return {'id': self.id, 'kind': self.kind, 'name': self.name,
                'color': self.color, 'visible': self.visible,
                'opacity': self.opacity, 'group': self.group,
                'provenance': self.provenance, 'metadata': self.metadata}

    def _load_head(self, d):
        self.id = d.get('id', self.id)
        self.name = d.get('name', '')
        self.color = d.get('color', self.color)
        self.visible = d.get('visible', True)
        self.opacity = d.get('opacity', 1.0)
        self.group = d.get('group', '')
        self.provenance = dict(d.get('provenance') or {})
        self.metadata = dict(d.get('metadata') or {})
        return self

    def bounds(self):
        """(minx, miny, minz, maxx, maxy, maxz) or None."""
        return None


def _xyz_bounds(flat):
    if not flat:
        return None
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    for k in range(0, len(flat) - 2, 3):
        for a in range(3):
            v = flat[k + a]
            if v != v:
                continue
            if v < mn[a]:
                mn[a] = v
            if v > mx[a]:
                mx[a] = v
    if mn[0] == math.inf:
        return None
    return (mn[0], mn[1], mn[2], mx[0], mx[1], mx[2])


# ------------------------------------------------------------------ Grid2D
class Grid2D(ModelObject):
    """Node-registered regular 2-D grid: a heightfield (topography, contact
    surface, horizon) or a 2-D property grid (magnetics, gravity...)."""
    kind = 'grid2d'

    def __init__(self, nx, ny, x0, y0, dx, dy, values=None, rotation=0.0,
                 units='m', role='surface', **kw):
        ModelObject.__init__(self, **kw)
        self.nx, self.ny = int(nx), int(ny)
        self.x0, self.y0 = float(x0), float(y0)
        self.dx, self.dy = float(dx), float(dy)
        self.rotation = float(rotation)
        self.units = units
        self.role = role          # 'topography' | 'surface' | 'property'
        if values is None:
            self.values = array.array('d', [NAN]) * (self.nx * self.ny)
        else:
            self.values = values if isinstance(values, array.array) and values.typecode == 'd' else farray(values)
        if len(self.values) != self.nx * self.ny:
            raise ValueError('Grid2D values length %d != nx*ny %d' % (len(self.values), self.nx * self.ny))

    # -- geometry
    def node_xy(self, i, j):
        if self.rotation:
            r = math.radians(self.rotation)
            c, s = math.cos(r), math.sin(r)
            u, v = i * self.dx, j * self.dy
            return self.x0 + u * c - v * s, self.y0 + u * s + v * c
        return self.x0 + i * self.dx, self.y0 + j * self.dy

    def get(self, i, j):
        return self.values[j * self.nx + i]

    def set(self, i, j, v):
        self.values[j * self.nx + i] = NAN if v is None else v

    @property
    def xmax(self):
        return self.x0 + (self.nx - 1) * self.dx

    @property
    def ymax(self):
        return self.y0 + (self.ny - 1) * self.dy

    def bounds(self):
        zs = [v for v in self.values if v == v]
        corners = [self.node_xy(0, 0), self.node_xy(self.nx - 1, 0),
                   self.node_xy(0, self.ny - 1), self.node_xy(self.nx - 1, self.ny - 1)]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return (min(xs), min(ys), min(zs) if zs else NAN, max(xs), max(ys), max(zs) if zs else NAN)

    def zrange(self):
        zs = [v for v in self.values if v == v]
        return (min(zs), max(zs)) if zs else (NAN, NAN)

    def sample(self, x, y):
        """Bilinear value at (x, y); NaN outside or over no-data nodes
        (falls back to the nearest valid corner of the cell)."""
        if self.rotation:
            r = math.radians(-self.rotation)
            c, s = math.cos(r), math.sin(r)
            px, py = x - self.x0, y - self.y0
            u, v = px * c - py * s, px * s + py * c
        else:
            u, v = x - self.x0, y - self.y0
        fi, fj = u / self.dx, v / self.dy
        if fi < -1e-9 or fj < -1e-9 or fi > self.nx - 1 + 1e-9 or fj > self.ny - 1 + 1e-9:
            return NAN
        i0 = min(max(int(math.floor(fi)), 0), self.nx - 2) if self.nx > 1 else 0
        j0 = min(max(int(math.floor(fj)), 0), self.ny - 2) if self.ny > 1 else 0
        tx = min(max(fi - i0, 0.0), 1.0) if self.nx > 1 else 0.0
        ty = min(max(fj - j0, 0.0), 1.0) if self.ny > 1 else 0.0
        i1 = min(i0 + 1, self.nx - 1)
        j1 = min(j0 + 1, self.ny - 1)
        v00, v10 = self.get(i0, j0), self.get(i1, j0)
        v01, v11 = self.get(i0, j1), self.get(i1, j1)
        vals = [v00, v10, v01, v11]
        if any(v != v for v in vals):
            good = [(abs(tx - a) + abs(ty - b), v) for (a, b), v in
                    zip(((0, 0), (1, 0), (0, 1), (1, 1)), vals) if v == v]
            if not good:
                return NAN
            good.sort()
            return good[0][1]
        return (v00 * (1 - tx) * (1 - ty) + v10 * tx * (1 - ty)
                + v01 * (1 - tx) * ty + v11 * tx * ty)

    def to_mesh(self, stride=1, name=None, skip_nodata=True):
        """Triangulate the heightfield into a Mesh (Z = value)."""
        stride = max(1, int(stride))
        idx = {}
        verts = array.array('d')
        for j in range(0, self.ny, stride):
            for i in range(0, self.nx, stride):
                z = self.get(i, j)
                if skip_nodata and z != z:
                    continue
                x, y = self.node_xy(i, j)
                idx[(i, j)] = len(verts) // 3
                verts.extend((x, y, 0.0 if z != z else z))
        tris = array.array('I')
        js = list(range(0, self.ny, stride))
        iis = list(range(0, self.nx, stride))
        for a in range(len(js) - 1):
            for b in range(len(iis) - 1):
                p = idx.get((iis[b], js[a]))
                q = idx.get((iis[b + 1], js[a]))
                r = idx.get((iis[b], js[a + 1]))
                s = idx.get((iis[b + 1], js[a + 1]))
                if p is None or q is None or r is None or s is None:
                    continue
                tris.extend((p, q, s))
                tris.extend((p, s, r))
        m = Mesh(verts, tris, name=name or self.name, color=self.color,
                 provenance=dict(self.provenance))
        m.metadata['from_grid'] = self.id
        return m

    def copy_empty(self, fill=NAN):
        g = Grid2D(self.nx, self.ny, self.x0, self.y0, self.dx, self.dy,
                   array.array('d', [fill]) * (self.nx * self.ny),
                   rotation=self.rotation, units=self.units, role=self.role,
                   name=self.name, color=self.color)
        return g

    def to_json(self):
        d = self._head()
        d.update({'nx': self.nx, 'ny': self.ny, 'x0': self.x0, 'y0': self.y0,
                  'dx': self.dx, 'dy': self.dy, 'rotation': self.rotation,
                  'units': self.units, 'role': self.role,
                  'values': encode_array(self.values, 'f32' if self.role == 'property' else 'f64')})
        return d

    @classmethod
    def from_json(cls, d):
        g = cls(d['nx'], d['ny'], d['x0'], d['y0'], d['dx'], d['dy'],
                farray(decode_array(d['values'])), rotation=d.get('rotation', 0.0),
                units=d.get('units', 'm'), role=d.get('role', 'surface'))
        return g._load_head(d)


# -------------------------------------------------------------------- Mesh
class Mesh(ModelObject):
    """Triangle mesh. attributes: {name: {'location': 'vertices'|'faces',
    'values': array}} for numeric per-vertex / per-face data."""
    kind = 'mesh'

    def __init__(self, vertices=None, triangles=None, attributes=None,
                 role='surface', **kw):
        ModelObject.__init__(self, **kw)
        self.vertices = vertices if isinstance(vertices, array.array) else farray(_flat(vertices))
        self.triangles = triangles if isinstance(triangles, array.array) else iarray(_flat(triangles))
        self.attributes = dict(attributes or {})
        self.role = role   # 'surface' | 'topography' | 'contact' | 'vein' | 'fault' | 'volume' | 'unit'

    @property
    def n_vertices(self):
        return len(self.vertices) // 3

    @property
    def n_triangles(self):
        return len(self.triangles) // 3

    def vertex(self, i):
        return (self.vertices[3 * i], self.vertices[3 * i + 1], self.vertices[3 * i + 2])

    def triangle(self, t):
        return (self.triangles[3 * t], self.triangles[3 * t + 1], self.triangles[3 * t + 2])

    def bounds(self):
        return _xyz_bounds(self.vertices)

    def validate(self):
        n = self.n_vertices
        for k in range(len(self.triangles)):
            if self.triangles[k] >= n:
                raise ValueError('triangle index %d >= %d vertices' % (self.triangles[k], n))
        if len(self.vertices) % 3 or len(self.triangles) % 3:
            raise ValueError('ragged mesh arrays')
        return True

    def to_json(self):
        d = self._head()
        d.update({'role': self.role,
                  'vertices': encode_array(self.vertices, 'f64'),
                  'triangles': encode_array(self.triangles, 'u32'),
                  'attributes': {k: {'location': v.get('location', 'vertices'),
                                     'values': encode_array(v['values'], 'f32')}
                                 for k, v in self.attributes.items()}})
        return d

    @classmethod
    def from_json(cls, d):
        attrs = {k: {'location': v.get('location', 'vertices'),
                     'values': farray(decode_array(v['values']))}
                 for k, v in (d.get('attributes') or {}).items()}
        m = cls(farray(decode_array(d['vertices'])), iarray(decode_array(d['triangles'])),
                attributes=attrs, role=d.get('role', 'surface'))
        return m._load_head(d)


def _flat(seq):
    if seq is None:
        return []
    seq = list(seq)
    if seq and isinstance(seq[0], (list, tuple, array.array)):
        out = []
        for s in seq:
            out.extend(s)
        return out
    return seq


# ----------------------------------------------------------------- LineSet
class LineSet(ModelObject):
    """Polylines: flat vertices + flat segments; ``parts`` = list of vertex
    index lists (ordered polylines); ``features`` = per-part attribute dicts
    (used by workings: type, level, name, source...)."""
    kind = 'lineset'

    def __init__(self, vertices=None, segments=None, parts=None, features=None,
                 role='lines', **kw):
        ModelObject.__init__(self, **kw)
        self.vertices = vertices if isinstance(vertices, array.array) else farray(_flat(vertices))
        self.segments = segments if isinstance(segments, array.array) else iarray(_flat(segments))
        self.parts = [list(p) for p in (parts or [])]
        self.features = [dict(f) for f in (features or [])]
        self.role = role   # 'lines' | 'faults' | 'workings' | 'drillhole-traces' | 'contours' | 'section'
        if not self.parts and len(self.segments):
            self.parts = self._parts_from_segments()
        if not len(self.segments) and self.parts:
            self.segments = self._segments_from_parts()

    def _segments_from_parts(self):
        s = array.array('I')
        for p in self.parts:
            for k in range(len(p) - 1):
                s.extend((p[k], p[k + 1]))
        return s

    def _parts_from_segments(self):
        nxt = {}
        starts = []
        has_prev = set()
        for k in range(0, len(self.segments) - 1, 2):
            a, b = self.segments[k], self.segments[k + 1]
            nxt.setdefault(a, []).append(b)
            has_prev.add(b)
        for a in nxt:
            if a not in has_prev:
                starts.append(a)
        parts, seen = [], set()
        for a in starts + [k for k in nxt if k not in has_prev]:
            if a in seen:
                continue
            chain = [a]
            seen.add(a)
            cur = a
            while nxt.get(cur) and nxt[cur][0] not in seen:
                cur = nxt[cur].pop(0)
                chain.append(cur)
                seen.add(cur)
            parts.append(chain)
        return parts

    def add_polyline(self, xyz, feature=None):
        """Append an ordered polyline (list of (x,y,z)); returns part index."""
        base = self.n_vertices
        for p in xyz:
            self.vertices.extend((float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
        idx = list(range(base, base + len(xyz)))
        for k in range(len(idx) - 1):
            self.segments.extend((idx[k], idx[k + 1]))
        self.parts.append(idx)
        self.features.append(dict(feature or {}))
        return len(self.parts) - 1

    def part_xyz(self, k):
        return [self.vertex(i) for i in self.parts[k]]

    @property
    def n_vertices(self):
        return len(self.vertices) // 3

    def vertex(self, i):
        return (self.vertices[3 * i], self.vertices[3 * i + 1], self.vertices[3 * i + 2])

    def bounds(self):
        return _xyz_bounds(self.vertices)

    def length(self, k=None):
        parts = [self.parts[k]] if k is not None else self.parts
        tot = 0.0
        for p in parts:
            for a in range(len(p) - 1):
                x0, y0, z0 = self.vertex(p[a])
                x1, y1, z1 = self.vertex(p[a + 1])
                tot += math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)
        return tot

    def to_json(self):
        d = self._head()
        d.update({'role': self.role,
                  'vertices': encode_array(self.vertices, 'f64'),
                  'segments': encode_array(self.segments, 'u32'),
                  'parts': self.parts, 'features': self.features})
        return d

    @classmethod
    def from_json(cls, d):
        ls = cls(farray(decode_array(d['vertices'])), iarray(decode_array(d['segments'])),
                 parts=d.get('parts'), features=d.get('features'), role=d.get('role', 'lines'))
        return ls._load_head(d)


# ---------------------------------------------------------------- PointSet
class PointSet(ModelObject):
    """Points with attribute columns. attributes: {name: list} — numeric
    columns are arrays (NaN = missing), text columns are lists of str."""
    kind = 'points'

    def __init__(self, xyz=None, attributes=None, role='points', **kw):
        ModelObject.__init__(self, **kw)
        self.xyz = xyz if isinstance(xyz, array.array) else farray(_flat(xyz))
        self.attributes = {}
        for k, v in (attributes or {}).items():
            self.attributes[k] = v
        self.role = role   # 'points' | 'mines' | 'samples' | 'contacts' | 'structural' | 'collars' | 'targets' | 'claims'

    @property
    def n(self):
        return len(self.xyz) // 3

    def point(self, i):
        return (self.xyz[3 * i], self.xyz[3 * i + 1], self.xyz[3 * i + 2])

    def add(self, x, y, z, **attrs):
        self.xyz.extend((float(x), float(y), float(z)))
        n = self.n
        keys = list(self.attributes) + [k for k in attrs if k not in self.attributes]
        for k in keys:
            col = self.attributes.setdefault(k, [])
            while len(col) < n - 1:
                col.append(None)
            col.append(attrs.get(k))
        return n - 1

    def numeric(self, name):
        """Column as floats (NaN where missing / non-numeric)."""
        out = array.array('d')
        for v in self.attributes.get(name, []):
            try:
                out.append(float(v) if v not in (None, '') else NAN)
            except (TypeError, ValueError):
                out.append(NAN)
        while len(out) < self.n:
            out.append(NAN)
        return out

    def bounds(self):
        return _xyz_bounds(self.xyz)

    def to_json(self):
        d = self._head()
        attrs = {}
        for k, col in self.attributes.items():
            if _all_numeric(col):
                attrs[k] = {'type': 'number', 'values': encode_array(
                    farray(None if v in (None, '') else v for v in col), 'f64')}
            else:
                attrs[k] = {'type': 'text', 'values': [None if v is None else str(v) for v in col]}
        d.update({'role': self.role, 'xyz': encode_array(self.xyz, 'f64'), 'attributes': attrs})
        return d

    @classmethod
    def from_json(cls, d):
        attrs = {}
        for k, v in (d.get('attributes') or {}).items():
            vals = decode_array(v['values']) if v.get('type') == 'number' else v['values']
            attrs[k] = list(vals)
        p = cls(farray(decode_array(d['xyz'])), attributes=attrs, role=d.get('role', 'points'))
        return p._load_head(d)


def _all_numeric(col):
    seen = False
    for v in col:
        if v is None or v == '':
            continue
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)):
            seen = True
            continue
        try:
            float(v)
            seen = True
        except (TypeError, ValueError):
            return False
    return seen


# -------------------------------------------------------------- BlockModel
class BlockModel(ModelObject):
    """Regular block model. origin = minimum corner; block (i,j,k) centroid is
    origin + ((i+.5)*dx, (j+.5)*dy, (k+.5)*dz) rotated ``azimuth`` degrees
    clockwise from north about the origin (0 = axis-aligned).
    attributes: {name: array of length nx*ny*nz in i-fastest order}."""
    kind = 'blockmodel'

    def __init__(self, origin, block_size, count, attributes=None, azimuth=0.0, **kw):
        ModelObject.__init__(self, **kw)
        self.origin = [float(v) for v in origin]
        self.block_size = [float(v) for v in block_size]
        self.count = [int(v) for v in count]
        self.azimuth = float(azimuth)
        self.attributes = {}
        for k, v in (attributes or {}).items():
            self.add_attribute(k, v)

    @property
    def n(self):
        return self.count[0] * self.count[1] * self.count[2]

    def add_attribute(self, name, values, kind='number'):
        if kind == 'number':
            vals = values if isinstance(values, array.array) and values.typecode == 'd' else farray(values)
        else:
            vals = list(values)
        if len(vals) != self.n:
            raise ValueError('attribute %s has %d values, model has %d blocks' % (name, len(vals), self.n))
        self.attributes[name] = {'type': kind, 'values': vals}

    def index(self, i, j, k):
        return i + self.count[0] * (j + self.count[1] * k)

    def ijk(self, idx):
        nx, ny = self.count[0], self.count[1]
        return idx % nx, (idx // nx) % ny, idx // (nx * ny)

    def centroid(self, i, j, k):
        ox, oy, oz = self.origin
        u = (i + 0.5) * self.block_size[0]
        v = (j + 0.5) * self.block_size[1]
        z = oz + (k + 0.5) * self.block_size[2]
        if self.azimuth:
            r = math.radians(self.azimuth)
            c, s = math.cos(r), math.sin(r)
            # azimuth clockwise from north: local u -> east rotated by az
            return ox + u * c + v * s, oy - u * s + v * c, z
        return ox + u, oy + v, z

    def bounds(self):
        xs, ys = [], []
        for i in (0, self.count[0]):
            for j in (0, self.count[1]):
                x, y, _ = self.centroid(i - 0.5, j - 0.5, 0)
                xs.append(x)
                ys.append(y)
        return (min(xs), min(ys), self.origin[2], max(xs), max(ys),
                self.origin[2] + self.count[2] * self.block_size[2])

    def to_json(self):
        d = self._head()
        attrs = {}
        for k, a in self.attributes.items():
            if a['type'] == 'number':
                attrs[k] = {'type': 'number', 'values': encode_array(a['values'], 'f32')}
            else:
                attrs[k] = {'type': a['type'], 'values': list(a['values'])}
        d.update({'origin': self.origin, 'block_size': self.block_size, 'count': self.count,
                  'azimuth': self.azimuth, 'attributes': attrs})
        return d

    @classmethod
    def from_json(cls, d):
        bm = cls(d['origin'], d['block_size'], d['count'], azimuth=d.get('azimuth', 0.0))
        for k, a in (d.get('attributes') or {}).items():
            if a.get('type', 'number') == 'number':
                bm.add_attribute(k, farray(decode_array(a['values'])))
            else:
                bm.add_attribute(k, a['values'], kind=a['type'])
        return bm._load_head(d)


# -------------------------------------------------------------- Drillholes
class Drillholes(ModelObject):
    """Drillhole database: collars / surveys / interval tables, in the column
    conventions Leapfrog's importer expects (positive dip = down)."""
    kind = 'drillholes'

    def __init__(self, collars=None, surveys=None, intervals=None, **kw):
        ModelObject.__init__(self, **kw)
        # collars: [{'hole': str, 'x':, 'y':, 'z':, 'depth': float|None, ...}]
        self.collars = [dict(c) for c in (collars or [])]
        # surveys: [{'hole':, 'depth':, 'azimuth':, 'dip':}]  (dip +ve down)
        self.surveys = [dict(s) for s in (surveys or [])]
        # intervals: {'assay': [{'hole':, 'from':, 'to':, 'Au_ppm': ..}], 'lith': [...]}
        self.intervals = {k: [dict(r) for r in v] for k, v in (intervals or {}).items()}
        self._traces = None

    def holes(self):
        return [c['hole'] for c in self.collars]

    def collar(self, hole):
        for c in self.collars:
            if c['hole'] == hole:
                return c
        return None

    def desurvey(self, step=None):
        """Minimum-curvature desurvey -> {hole: [(depth, x, y, z), ...]}.
        Stations are the survey depths (plus the collar at 0 and the hole's
        max depth); ``step`` subdivides long intervals for smooth curves."""
        traces = {}
        for c in self.collars:
            hole = c['hole']
            svy = sorted((s for s in self.surveys if s['hole'] == hole), key=lambda s: float(s['depth']))
            depth_max = c.get('depth')
            if depth_max in (None, ''):
                ds = [float(r['to']) for t in self.intervals.values() for r in t if r['hole'] == hole]
                depth_max = max(ds) if ds else (float(svy[-1]['depth']) if svy else 0.0)
            depth_max = float(depth_max)
            if not svy:
                svy = [{'depth': 0.0, 'azimuth': 0.0, 'dip': 90.0}]
            if float(svy[0]['depth']) > 0:
                svy = [dict(svy[0], depth=0.0)] + svy
            if float(svy[-1]['depth']) < depth_max:
                svy = svy + [dict(svy[-1], depth=depth_max)]
            pts = [(0.0, float(c['x']), float(c['y']), float(c['z']))]
            x, y, z = float(c['x']), float(c['y']), float(c['z'])
            for a in range(len(svy) - 1):
                d0, d1 = float(svy[a]['depth']), float(svy[a + 1]['depth'])
                if d1 <= d0:
                    continue
                az0, dp0 = math.radians(float(svy[a]['azimuth'])), math.radians(float(svy[a]['dip']))
                az1, dp1 = math.radians(float(svy[a + 1]['azimuth'])), math.radians(float(svy[a + 1]['dip']))
                nsub = 1
                if step:
                    nsub = max(1, int(math.ceil((d1 - d0) / float(step))))
                for q in range(nsub):
                    ta, tb = q / nsub, (q + 1) / nsub
                    # interpolate direction linearly in (azimuth, dip) for sub-steps
                    azA = _lerp_angle(az0, az1, ta)
                    azB = _lerp_angle(az0, az1, tb)
                    dpA = dp0 + (dp1 - dp0) * ta
                    dpB = dp0 + (dp1 - dp0) * tb
                    seg = (d1 - d0) / nsub
                    dx, dy, dz = _min_curvature(seg, azA, dpA, azB, dpB)
                    x, y, z = x + dx, y + dy, z + dz
                    pts.append((d0 + seg * (q + 1), x, y, z))
            traces[hole] = pts
        self._traces = traces
        return traces

    def locate(self, hole, depth, traces=None):
        """XYZ at a downhole depth (linear along the desurveyed trace)."""
        traces = traces or self._traces or self.desurvey()
        tr = traces.get(hole)
        if not tr:
            return None
        if depth <= tr[0][0]:
            return tr[0][1:]
        for a in range(len(tr) - 1):
            d0, d1 = tr[a][0], tr[a + 1][0]
            if d0 <= depth <= d1:
                t = 0.0 if d1 == d0 else (depth - d0) / (d1 - d0)
                return tuple(tr[a][b + 1] + (tr[a + 1][b + 1] - tr[a][b + 1]) * t for b in range(3))
        return tr[-1][1:]

    def interval_points(self, table, column, traces=None):
        """Interval midpoints as a PointSet with the named value column and
        interval length (for compositing / kriging)."""
        traces = traces or self.desurvey()
        ps = PointSet(name='%s %s' % (table, column), role='samples')
        for r in self.intervals.get(table, []):
            try:
                f, t = float(r['from']), float(r['to'])
                v = float(r[column])
            except (KeyError, TypeError, ValueError):
                continue
            p = self.locate(r['hole'], (f + t) / 2, traces)
            if p is None:
                continue
            ps.add(p[0], p[1], p[2], **{'hole': r['hole'], 'from': f, 'to': t,
                                        'length': t - f, column: v})
        return ps

    def traces_lineset(self, step=None):
        traces = self.desurvey(step)
        ls = LineSet(name=self.name + ' traces', role='drillhole-traces', color=self.color)
        for hole, pts in traces.items():
            ls.add_polyline([(p[1], p[2], p[3]) for p in pts], {'hole': hole})
        return ls

    def bounds(self):
        tr = self.desurvey()
        flat = array.array('d')
        for pts in tr.values():
            for p in pts:
                flat.extend(p[1:])
        return _xyz_bounds(flat)

    def to_json(self):
        d = self._head()
        d.update({'collars': self.collars, 'surveys': self.surveys, 'intervals': self.intervals})
        return d

    @classmethod
    def from_json(cls, d):
        return cls(d.get('collars'), d.get('surveys'), d.get('intervals'))._load_head(d)


def _lerp_angle(a, b, t):
    d = (b - a + math.pi) % (2 * math.pi) - math.pi
    return a + d * t


def _min_curvature(length, az0, dip0, az1, dip1):
    """Minimum-curvature displacement (dx east, dy north, dz up) for a segment.
    Dips are positive DOWN (Leapfrog convention)."""
    # direction cosines: inclination from vertical-down = 90 - dip
    i0, i1 = math.pi / 2 - dip0, math.pi / 2 - dip1
    cos_dl = math.cos(i1 - i0) - math.sin(i0) * math.sin(i1) * (1 - math.cos(az1 - az0))
    cos_dl = max(-1.0, min(1.0, cos_dl))
    dl = math.acos(cos_dl)
    rf = 1.0 if dl < 1e-9 else 2.0 / dl * math.tan(dl / 2.0)
    dn = length / 2.0 * (math.sin(i0) * math.cos(az0) + math.sin(i1) * math.cos(az1)) * rf
    de = length / 2.0 * (math.sin(i0) * math.sin(az0) + math.sin(i1) * math.sin(az1)) * rf
    dv = length / 2.0 * (math.cos(i0) + math.cos(i1)) * rf     # positive down
    return de, dn, -dv


# -------------------------------------------------------------- ImagePlane
class ImagePlane(ModelObject):
    """A scanned map placed in 3-D — the bridge for "maps that are 3-D in a
    2-D format":

    * kind 'section': a vertical cross / longitudinal section.  ``p1`` and
      ``p2`` are the map (x, y) of the image's top-left and top-right corners
      (Oasis-montaj "Georeference Section Image" convention); ``z_top`` /
      ``z_bottom`` are the elevations of the top and bottom image edges.
    * kind 'plan': a level plan or surface map.  ``control`` = list of
      [px, py, X, Y] tie points (>= 2; 2 = similarity, >= 3 = least-squares
      affine); ``elevation`` = the level it is drawn at (e.g. the 300-ft
      level), or None to drape on topography.
    ``image`` = data URI (PNG/JPEG) or a relative path; ``width``/``height``
    in pixels.  Pixel (0,0) is the image's top-left corner.
    """
    kind = 'imageplane'

    def __init__(self, image, width, height, plane='plan', p1=None, p2=None,
                 z_top=None, z_bottom=None, control=None, elevation=None, **kw):
        ModelObject.__init__(self, **kw)
        self.image = image
        self.width, self.height = int(width), int(height)
        self.plane = plane
        self.p1, self.p2 = (list(p1) if p1 else None), (list(p2) if p2 else None)
        self.z_top, self.z_bottom = z_top, z_bottom
        self.control = [list(c) for c in (control or [])]
        self.elevation = elevation

    def affine(self):
        """Plan: pixel -> world affine (a, b, c, d, e, f): X = a*px + b*py + c,
        Y = d*px + e*py + f. Least squares for >=3 points; similarity for 2."""
        cp = self.control
        if len(cp) < 2:
            raise ValueError('need >= 2 control points')
        if len(cp) == 2:
            (px0, py0, X0, Y0), (px1, py1, X1, Y1) = cp[0], cp[1]
            dpx, dpy = px1 - px0, py1 - py0
            dX, dY = X1 - X0, Y1 - Y0
            den = dpx * dpx + dpy * dpy
            if den == 0:
                raise ValueError('coincident control points')
            # similarity with a vertical flip (pixel y down, world y up)
            ca = (dX * dpx - dY * dpy) / den
            cb = (dY * dpx + dX * dpy) / den
            a, b = ca, cb
            d, e = cb, -ca
            c = X0 - a * px0 - b * py0
            f = Y0 - d * px0 - e * py0
            return (a, b, c, d, e, f)
        # least squares for a,b,c and d,e,f separately
        return _lsq_affine(cp)

    def pixel_to_world(self, px, py):
        if self.plane == 'section':
            u = px / float(self.width)
            v = py / float(self.height)
            x = self.p1[0] + (self.p2[0] - self.p1[0]) * u
            y = self.p1[1] + (self.p2[1] - self.p1[1]) * u
            z = self.z_top + (self.z_bottom - self.z_top) * v
            return (x, y, z)
        a, b, c, d, e, f = self.affine()
        return (a * px + b * py + c, d * px + e * py + f,
                self.elevation if self.elevation is not None else NAN)

    def corners(self):
        """World XYZ of the four image corners (TL, TR, BR, BL)."""
        w, h = self.width, self.height
        return [self.pixel_to_world(0, 0), self.pixel_to_world(w, 0),
                self.pixel_to_world(w, h), self.pixel_to_world(0, h)]

    def bounds(self):
        flat = array.array('d')
        for c in self.corners():
            flat.extend(c)
        return _xyz_bounds(flat)

    def to_json(self):
        d = self._head()
        d.update({'image': self.image, 'width': self.width, 'height': self.height,
                  'plane': self.plane, 'p1': self.p1, 'p2': self.p2,
                  'z_top': self.z_top, 'z_bottom': self.z_bottom,
                  'control': self.control, 'elevation': self.elevation})
        return d

    @classmethod
    def from_json(cls, d):
        return cls(d['image'], d['width'], d['height'], plane=d.get('plane', 'plan'),
                   p1=d.get('p1'), p2=d.get('p2'), z_top=d.get('z_top'),
                   z_bottom=d.get('z_bottom'), control=d.get('control'),
                   elevation=d.get('elevation'))._load_head(d)


def _lsq_affine(cp):
    # normal equations for [px py 1] -> X and -> Y
    sxx = syy = sxy = sx = sy = n = 0.0
    bx = [0.0, 0.0, 0.0]
    by = [0.0, 0.0, 0.0]
    for px, py, X, Y in cp:
        sxx += px * px
        syy += py * py
        sxy += px * py
        sx += px
        sy += py
        n += 1
        bx[0] += px * X
        bx[1] += py * X
        bx[2] += X
        by[0] += px * Y
        by[1] += py * Y
        by[2] += Y
    M = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]]
    a, b, c = solve3(M, bx)
    d, e, f = solve3(M, by)
    return (a, b, c, d, e, f)


def solve3(M, v):
    """Solve a 3x3 system by Cramer's rule."""
    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    D = det(M)
    if abs(D) < 1e-12:
        raise ValueError('singular control-point system')
    out = []
    for col in range(3):
        m = [row[:] for row in M]
        for r in range(3):
            m[r][col] = v[r]
        out.append(det(m) / D)
    return out


# -------------------------------------------------------------- StratModel
class StratModel(ModelObject):
    """The pancake stack: ordered units (top/youngest first), each bounded by
    the contact surface (Grid2D id) at its BASE; the top of the first unit is
    the topography.  ``units``: [{'name', 'color', 'lithology', 'base': grid
    id or None (basement), 'contact': 'deposit'|'erosion', 'description'}]."""
    kind = 'stratmodel'

    def __init__(self, units=None, topography=None, **kw):
        ModelObject.__init__(self, **kw)
        self.units = [dict(u) for u in (units or [])]
        self.topography = topography   # Grid2D id

    def to_json(self):
        d = self._head()
        d.update({'units': self.units, 'topography': self.topography})
        return d

    @classmethod
    def from_json(cls, d):
        return cls(d.get('units'), d.get('topography'))._load_head(d)


# ----------------------------------------------------------------- Section
class Section(ModelObject):
    """A saved slice: vertical section through (x0,y0)-(x1,y1) between z_min
    and z_max, or a general plane (point + normal).  Products (intersection
    lines, sampled grids) are recomputed on load, not stored."""
    kind = 'section'

    def __init__(self, start=None, end=None, z_min=None, z_max=None,
                 point=None, normal=None, **kw):
        ModelObject.__init__(self, **kw)
        self.start, self.end = (list(start) if start else None), (list(end) if end else None)
        self.z_min, self.z_max = z_min, z_max
        self.point, self.normal = (list(point) if point else None), (list(normal) if normal else None)

    def plane(self):
        """(point, unit normal) of the section plane."""
        if self.point and self.normal:
            n = self.normal
            ln = math.sqrt(sum(c * c for c in n)) or 1.0
            return list(self.point), [c / ln for c in n]
        (x0, y0), (x1, y1) = self.start[:2], self.end[:2]
        dx, dy = x1 - x0, y1 - y0
        ln = math.hypot(dx, dy) or 1.0
        return [x0, y0, 0.0], [-dy / ln, dx / ln, 0.0]

    def to_json(self):
        d = self._head()
        d.update({'start': self.start, 'end': self.end, 'z_min': self.z_min,
                  'z_max': self.z_max, 'point': self.point, 'normal': self.normal})
        return d

    @classmethod
    def from_json(cls, d):
        return cls(d.get('start'), d.get('end'), d.get('z_min'), d.get('z_max'),
                   d.get('point'), d.get('normal'))._load_head(d)


KINDS = {c.kind: c for c in (Grid2D, Mesh, LineSet, PointSet, BlockModel,
                             Drillholes, ImagePlane, StratModel, Section)}


# ----------------------------------------------------------------- Project
class Project(object):
    """A model project: CRS + a list of objects.  ``crs`` is
    {'kind': 'utm', 'zone': 12, 'north': True, 'epsg': 32612, 'units': 'm'}
    (or {'kind': 'local'}); ``origin`` is an [x, y, z] offset the browser
    subtracts for float32 precision (stored coordinates are absolute)."""

    def __init__(self, name='model', crs=None, origin=None, site=None, metadata=None):
        self.schema = SCHEMA
        self.name = name
        self.crs = dict(crs or {'kind': 'local', 'units': 'm'})
        self.origin = list(origin) if origin else None
        self.site = dict(site or {})       # {'name','lon','lat','source','id'}
        self.metadata = dict(metadata or {})
        self.created = _now()
        self.modified = self.created
        self.objects = []

    def add(self, obj):
        self.objects.append(obj)
        self.modified = _now()
        return obj

    def get(self, oid):
        for o in self.objects:
            if o.id == oid:
                return o
        return None

    def by_kind(self, kind):
        return [o for o in self.objects if o.kind == kind]

    def bounds(self):
        bs = [o.bounds() for o in self.objects]
        bs = [b for b in bs if b and all(v == v for v in b)]
        if not bs:
            return None
        return (min(b[0] for b in bs), min(b[1] for b in bs), min(b[2] for b in bs),
                max(b[3] for b in bs), max(b[4] for b in bs), max(b[5] for b in bs))

    def ensure_origin(self):
        if self.origin is None:
            b = self.bounds()
            if b:
                self.origin = [round((b[0] + b[3]) / 2, -2), round((b[1] + b[4]) / 2, -2), 0.0]
        return self.origin

    def to_json(self):
        self.ensure_origin()
        return {'schema': self.schema, 'name': self.name, 'crs': self.crs,
                'origin': self.origin, 'site': self.site, 'metadata': self.metadata,
                'created': self.created, 'modified': self.modified,
                'generator': 'nw-mineral-monitor geomodel',
                'objects': [o.to_json() for o in self.objects]}

    def dumps(self, indent=None):
        return json.dumps(sanitize(self.to_json()), indent=indent, allow_nan=False,
                          default=_json_default)

    def save(self, path, indent=None):
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(self.dumps(indent))

    @classmethod
    def from_json(cls, d):
        if d.get('schema') != SCHEMA:
            raise ValueError('not a %s project (schema=%r)' % (SCHEMA, d.get('schema')))
        p = cls(d.get('name', 'model'), d.get('crs'), d.get('origin'), d.get('site'), d.get('metadata'))
        p.created = d.get('created', p.created)
        p.modified = d.get('modified', p.modified)
        for od in d.get('objects', []):
            klass = KINDS.get(od.get('kind'))
            if klass is None:
                continue
            p.objects.append(klass.from_json(od))
        return p

    @classmethod
    def load(cls, path):
        with open(path, 'r', encoding='utf-8') as fh:
            return cls.from_json(json.load(fh))


def sanitize(o):
    """Replace NaN/inf floats with None and arrays with lists so the output
    is strict JSON (browsers reject bare NaN tokens)."""
    if isinstance(o, float):
        return None if (o != o or o in (math.inf, -math.inf)) else o
    if isinstance(o, dict):
        return {k: sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [sanitize(v) for v in o]
    if isinstance(o, array.array):
        return [sanitize(v) for v in o]
    return o


def _json_default(o):
    if isinstance(o, array.array):
        return list(o)
    if isinstance(o, float) and o != o:
        return None
    raise TypeError('not JSON serializable: %r' % type(o))


def utm_crs(zone, north=True):
    return {'kind': 'utm', 'zone': int(zone), 'north': bool(north),
            'epsg': (32600 if north else 32700) + int(zone), 'units': 'm'}
