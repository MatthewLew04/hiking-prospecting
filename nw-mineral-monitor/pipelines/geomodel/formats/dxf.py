"""AutoCAD DXF (R12 / AC1009 writer, tolerant reader), stdlib only.

Writer (``write_dxf``): HEADER ($ACADVER AC1009, $INSUNITS 6 = metres,
$EXTMIN / $EXTMAX), a minimal TABLES section (LTYPE CONTINUOUS + one LAYER
per object) and ENTITIES:

* Mesh     -> one 3DFACE per triangle (4th corner repeats the 3rd)
* LineSet  -> one POLYLINE (66=1, 70=8 3-D polyline) + VERTEX (70=32)
              + SEQEND per part
* PointSet -> POINT

The layer is the sanitised object name (R12: <= 31 chars, [A-Za-z0-9_$-]).

Reader (``read_dxf``): walks the (code, value) tag pairs of the ENTITIES
section of any DXF release (R12 ... R2018 -- subclass markers, handles and
owner codes are simply ignored) and builds one object per (layer, kind):

* 3DFACE / SOLID                   -> Mesh per layer (vertices de-duplicated
                                      by exact coordinate; quads split)
* POLYLINE 70&64 polyface mesh     -> Mesh per layer (faces 71-74, 1-based,
                                      negative = invisible edge, quads split)
* POLYLINE 70&16 polygon mesh      -> Mesh per layer (M x N grid triangulated,
                                      closed in M / N honoured)
* POLYLINE (2-D / 3-D), LWPOLYLINE, LINE -> LineSet per layer, one part per
                                      entity (closed flag -> repeated first
                                      vertex; bulges tessellated)
* POINT                            -> PointSet per layer

Everything else (TEXT, INSERT, CIRCLE, ARC, SPLINE, MESH ...) is counted in
``metadata['skipped']`` of every returned object.
"""
import io
import math
import os
import re

from ..model import Mesh, LineSet, PointSet, farray, iarray

FORMAT_ID = 'dxf'
_EOL = '\r\n'

# AutoCAD Color Index -> RGB for the colours we can name; the rest are grey.
_ACI_RGB = {1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255),
            5: (0, 0, 255), 6: (255, 0, 255), 7: (255, 255, 255), 8: (128, 128, 128),
            9: (192, 192, 192), 250: (51, 51, 51), 251: (91, 91, 91), 252: (132, 132, 132),
            253: (173, 173, 173), 254: (214, 214, 214), 255: (255, 255, 255)}


# ------------------------------------------------------------------ helpers
def _load_bytes(src):
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src)
    if hasattr(src, 'read'):
        data = src.read()
        return data if isinstance(data, bytes) else data.encode('utf-8')
    with open(src, 'rb') as fh:
        return fh.read()


def _src_path(src):
    return os.fspath(src) if isinstance(src, (str, os.PathLike)) else None


def _emit(dst, data):
    if hasattr(dst, 'write'):
        dst.write(data)
        if isinstance(dst, io.BytesIO):
            return dst.getvalue()
        return getattr(dst, 'name', None)
    with open(dst, 'wb') as fh:
        fh.write(data)
    return os.fspath(dst)


def _decode(data):
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1', 'replace')


def _fmt(v):
    v = float(v)
    if v != v or v in (math.inf, -math.inf):
        return '0.0'
    r = repr(v)
    return r if ('.' in r or 'e' in r or 'n' in r) else r + '.0'


def sanitise_layer(name, fallback='0'):
    """R12 layer name: letters, digits, $ - _ ; at most 31 characters."""
    s = re.sub(r'[^A-Za-z0-9_$\-]+', '_', str(name or '').strip())
    s = s.strip('_')[:31]
    return s or fallback


def _aci_from_rgb(color):
    try:
        r, g, b = [float(c) for c in color[:3]]
    except (TypeError, ValueError):
        return 7
    best, bd = 7, None
    for aci, (cr, cg, cb) in _ACI_RGB.items():
        d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
        if bd is None or d < bd:
            best, bd = aci, d
    return best


# ------------------------------------------------------------------- writer
class _TagWriter(object):
    def __init__(self):
        self.parts = []

    def tag(self, code, value):
        self.parts.append('%3d%s%s%s' % (code, _EOL, value, _EOL))

    def point(self, x, y, z, base=10):
        self.tag(base, _fmt(x))
        self.tag(base + 10, _fmt(y))
        self.tag(base + 20, _fmt(z))

    def getvalue(self):
        return ''.join(self.parts)


def _layer_for(obj, k, layer_names, used):
    layer = None
    if isinstance(layer_names, dict):
        layer = layer_names.get(obj.name, layer_names.get(obj.id, layer_names.get(k)))
    elif isinstance(layer_names, str):
        layer = layer_names
    elif layer_names is not None:
        try:
            layer = layer_names[k]
        except (IndexError, TypeError):
            layer = None
    layer = sanitise_layer(layer if layer is not None else obj.name, fallback='OBJ_%d' % k)
    # keep layers unique per object unless the caller mapped them explicitly
    if layer_names is None:
        base, n = layer, 1
        while layer in used:
            n += 1
            suffix = '_%d' % n
            layer = base[:31 - len(suffix)] + suffix
    used.add(layer)
    return layer


def write_dxf(objects, dst, layer_names=None):
    """Write Mesh / LineSet / PointSet objects (one or a list) as DXF R12.

    ``layer_names`` may be a list parallel to ``objects``, a dict keyed by
    object name / id / index, or a single string.  Returns the path (bytes
    for a BytesIO).
    """
    if not isinstance(objects, (list, tuple)):
        objects = [objects]
    for obj in objects:
        if getattr(obj, 'kind', None) not in ('mesh', 'lineset', 'points'):
            raise TypeError('write_dxf: cannot write a %r object' % getattr(obj, 'kind', type(obj).__name__))
    used = set()
    layers = []
    for k, obj in enumerate(objects):
        layers.append((_layer_for(obj, k, layer_names, used), _aci_from_rgb(obj.color)))

    # extents
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    for obj in objects:
        b = obj.bounds()
        if not b:
            continue
        for a in range(3):
            mn[a] = min(mn[a], b[a])
            mx[a] = max(mx[a], b[a + 3])
    if mn[0] == math.inf:
        mn = [0.0, 0.0, 0.0]
        mx = [0.0, 0.0, 0.0]

    w = _TagWriter()
    w.tag(999, 'nw-mineral-monitor geomodel DXF R12 export')
    # HEADER
    w.tag(0, 'SECTION')
    w.tag(2, 'HEADER')
    w.tag(9, '$ACADVER')
    w.tag(1, 'AC1009')
    w.tag(9, '$INSUNITS')
    w.tag(70, 6)
    w.tag(9, '$EXTMIN')
    w.point(mn[0], mn[1], mn[2])
    w.tag(9, '$EXTMAX')
    w.point(mx[0], mx[1], mx[2])
    w.tag(0, 'ENDSEC')
    # TABLES: LTYPE + LAYER
    w.tag(0, 'SECTION')
    w.tag(2, 'TABLES')
    w.tag(0, 'TABLE')
    w.tag(2, 'LTYPE')
    w.tag(70, 1)
    w.tag(0, 'LTYPE')
    w.tag(2, 'CONTINUOUS')
    w.tag(70, 0)
    w.tag(3, 'Solid line')
    w.tag(72, 65)
    w.tag(73, 0)
    w.tag(40, '0.0')
    w.tag(0, 'ENDTAB')
    w.tag(0, 'TABLE')
    w.tag(2, 'LAYER')
    w.tag(70, len(layers) + 1)
    seen = set()
    for lname, aci in [('0', 7)] + layers:
        if lname in seen:
            continue
        seen.add(lname)
        w.tag(0, 'LAYER')
        w.tag(2, lname)
        w.tag(70, 0)
        w.tag(62, aci)
        w.tag(6, 'CONTINUOUS')
    w.tag(0, 'ENDTAB')
    w.tag(0, 'ENDSEC')
    # ENTITIES
    w.tag(0, 'SECTION')
    w.tag(2, 'ENTITIES')
    for obj, (layer, aci) in zip(objects, layers):
        kind = getattr(obj, 'kind', None)
        if kind == 'mesh':
            v, t = obj.vertices, obj.triangles
            for k in range(0, len(t) - 2, 3):
                a, b, c = t[k], t[k + 1], t[k + 2]
                w.tag(0, '3DFACE')
                w.tag(8, layer)
                w.point(v[3 * a], v[3 * a + 1], v[3 * a + 2], 10)
                w.point(v[3 * b], v[3 * b + 1], v[3 * b + 2], 11)
                w.point(v[3 * c], v[3 * c + 1], v[3 * c + 2], 12)
                w.point(v[3 * c], v[3 * c + 1], v[3 * c + 2], 13)
        elif kind == 'lineset':
            parts = obj.parts or []
            for p in parts:
                if len(p) < 2:
                    continue
                w.tag(0, 'POLYLINE')
                w.tag(8, layer)
                w.tag(66, 1)
                w.point(0.0, 0.0, 0.0)
                w.tag(70, 8)
                for i in p:
                    x, y, z = obj.vertex(i)
                    w.tag(0, 'VERTEX')
                    w.tag(8, layer)
                    w.point(x, y, z)
                    w.tag(70, 32)
                w.tag(0, 'SEQEND')
                w.tag(8, layer)
        elif kind == 'points':
            for i in range(obj.n):
                x, y, z = obj.point(i)
                w.tag(0, 'POINT')
                w.tag(8, layer)
                w.point(x, y, z)
        else:
            raise TypeError('write_dxf: cannot write a %r object' % kind)
    w.tag(0, 'ENDSEC')
    w.tag(0, 'EOF')
    return _emit(dst, w.getvalue().encode('utf-8'))


# ------------------------------------------------------------------- reader
def _tags(text):
    """Yield (code, value) pairs; tolerant of \\r\\n, \\n and stray blanks."""
    lines = re.split(r'\r\n|\n|\r', text)
    n = len(lines)
    k = 0
    while k + 1 < n:
        code_s = lines[k].strip()
        if not code_s:
            k += 1
            continue
        try:
            code = int(code_s)
        except ValueError:
            k += 1
            continue
        yield code, lines[k + 1].strip()
        k += 2


def _entities(text):
    """Group the ENTITIES section into [(type, [(code, value), ...]), ...];
    also returns the LAYER table colours {layer: aci} and the $ACADVER."""
    entities = []
    layer_colors = {}
    acadver = None
    section = None
    cur = None
    pending_var = None
    table_kind = None
    cur_layer = None
    for code, value in _tags(text):
        if code == 0:
            if value == 'SECTION':
                section = 'SECTION?'
                cur = None
                continue
            if value == 'ENDSEC':
                section = None
                cur = None
                continue
            if value == 'EOF':
                break
            if section == 'ENTITIES':
                cur = (value, [])
                entities.append(cur)
            elif section == 'TABLES':
                if value == 'TABLE':
                    table_kind = None
                    cur_layer = None
                elif value == 'LAYER' and table_kind == 'LAYER':
                    cur_layer = {}
                    cur = None
                elif value in ('ENDTAB',):
                    table_kind = None
                    cur_layer = None
                else:
                    cur_layer = None
            continue
        if section == 'SECTION?' and code == 2:
            section = value
            continue
        if section == 'HEADER':
            if code == 9:
                pending_var = value
            elif pending_var == '$ACADVER' and code == 1:
                acadver = value
            continue
        if section == 'TABLES':
            if code == 2 and table_kind is None and cur_layer is None:
                table_kind = value
            elif cur_layer is not None:
                if code == 2:
                    cur_layer['name'] = value
                elif code == 62:
                    try:
                        cur_layer['aci'] = int(value)
                    except ValueError:
                        pass
                if 'name' in cur_layer and 'aci' in cur_layer:
                    layer_colors[cur_layer['name']] = abs(cur_layer['aci'])
            continue
        if section == 'ENTITIES' and cur is not None:
            cur[1].append((code, value))
    return entities, layer_colors, acadver


def _first(tags, code, default=None, conv=float):
    for c, v in tags:
        if c == code:
            try:
                return conv(v)
            except ValueError:
                return default
    return default


def _all(tags, code, conv=float):
    out = []
    for c, v in tags:
        if c == code:
            try:
                out.append(conv(v))
            except ValueError:
                pass
    return out


def _xyz(tags, base=10, default_z=0.0):
    x = _first(tags, base)
    y = _first(tags, base + 10)
    z = _first(tags, base + 20)
    if x is None or y is None:
        return None
    return (x, y, default_z if z is None else z)


def _bulge_points(p0, p1, bulge, max_deg=15.0):
    """Intermediate points along a bulge arc from p0 to p1 (excluding both
    ends); z interpolated linearly."""
    if not bulge:
        return []
    (x0, y0, z0), (x1, y1, z1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    chord = math.hypot(dx, dy)
    if chord == 0:
        return []
    theta = 4.0 * math.atan(bulge)                     # included angle (signed)
    r = chord / (2.0 * math.sin(abs(theta) / 2.0))
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    # distance from chord midpoint to centre
    h = math.sqrt(max(r * r - (chord / 2.0) ** 2, 0.0))
    # unit normal to the chord (left of direction p0->p1); for a CCW arc
    # (bulge > 0) spanning less than 180 degrees the centre lies to the left
    nx, ny = -dy / chord, dx / chord
    sgn = 1.0 if bulge > 0 else -1.0
    if abs(theta) > math.pi:
        h = -h
    cx, cy = mx + sgn * nx * h, my + sgn * ny * h
    a0 = math.atan2(y0 - cy, x0 - cx)
    n = max(1, int(math.ceil(abs(math.degrees(theta)) / max_deg)))
    pts = []
    for k in range(1, n):
        a = a0 + theta * k / n
        t = k / float(n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a), z0 + (z1 - z0) * t))
    return pts


class _Builder(object):
    """Accumulates per-layer Meshes / LineSets / PointSets."""

    def __init__(self):
        self.meshes = {}     # layer -> (vert_index dict, verts, tris, order)
        self.lines = {}      # layer -> LineSet
        self.points = {}     # layer -> PointSet
        self.order = []      # (kind, layer) first appearance
        self.warnings = []
        self.skipped = {}
        self.bulge_arcs = 0

    def _mesh(self, layer):
        m = self.meshes.get(layer)
        if m is None:
            m = {'index': {}, 'verts': farray(), 'tris': iarray()}
            self.meshes[layer] = m
            self.order.append(('mesh', layer))
        return m

    def add_face(self, layer, pts):
        """pts: list of 3 or 4 (x, y, z); quads are split."""
        m = self._mesh(layer)
        idx = []
        for p in pts:
            key = (p[0], p[1], p[2])
            i = m['index'].get(key)
            if i is None:
                i = len(m['verts']) // 3
                m['index'][key] = i
                m['verts'].extend(key)
            idx.append(i)
        # drop repeated consecutive corners (triangles encoded as 3DFACE with p3 == p4)
        uniq = []
        for i in idx:
            if not uniq or uniq[-1] != i:
                uniq.append(i)
        if len(uniq) > 1 and uniq[0] == uniq[-1]:
            uniq.pop()
        if len(uniq) < 3:
            return 0
        n = 0
        for k in range(1, len(uniq) - 1):
            m['tris'].extend((uniq[0], uniq[k], uniq[k + 1]))
            n += 1
        return n

    def add_polyline(self, layer, pts, feature=None):
        ls = self.lines.get(layer)
        if ls is None:
            ls = LineSet(name=layer, role='lines')
            self.lines[layer] = ls
            self.order.append(('lineset', layer))
        if len(pts) >= 2:
            ls.add_polyline(pts, feature)
        elif len(pts) == 1:
            self.warnings.append('single-vertex polyline on layer %r ignored' % layer)

    def add_point(self, layer, p):
        ps = self.points.get(layer)
        if ps is None:
            ps = PointSet(name=layer, role='points')
            self.points[layer] = ps
            self.order.append(('points', layer))
        ps.add(p[0], p[1], p[2])

    def skip(self, etype):
        self.skipped[etype] = self.skipped.get(etype, 0) + 1


def _polyline_vertices(tags_list):
    """tags_list: list of VERTEX tag lists -> list of (flags, (x,y,z), bulge,
    (i1,i2,i3,i4))."""
    out = []
    for vt in tags_list:
        flags = _first(vt, 70, 0, int) or 0
        p = _xyz(vt, 10)
        bulge = _first(vt, 42, 0.0) or 0.0
        face = tuple(_first(vt, c, 0, int) or 0 for c in (71, 72, 73, 74))
        out.append((flags, p, bulge, face))
    return out


def _handle_polyline(b, layer, ptags, vtags):
    flags = _first(ptags, 70, 0, int) or 0
    verts = _polyline_vertices(vtags)
    if flags & 64:                                   # polyface mesh
        geom = []
        faces = []
        for vflags, p, _, face in verts:
            if vflags & 128 and not (vflags & 64):
                faces.append(face)
            elif p is not None:
                geom.append(p)
        n = 0
        for face in faces:
            idx = [abs(i) - 1 for i in face if i != 0]
            pts = []
            ok = True
            for i in idx:
                if i < 0 or i >= len(geom):
                    ok = False
                    break
                pts.append(geom[i])
            if ok and len(pts) >= 3:
                n += b.add_face(layer, pts)
            else:
                b.warnings.append('polyface face with bad vertex index on layer %r skipped' % layer)
        return n
    if flags & 16:                                   # polygon (M x N) mesh
        M = _first(ptags, 71, 0, int) or 0
        N = _first(ptags, 72, 0, int) or 0
        geom = [p for vflags, p, _, _ in verts if p is not None and not (vflags & 16 and not vflags & 64)]
        if M * N != len(geom) or M < 2 or N < 2:
            b.warnings.append('polygon mesh on layer %r: %d vertices but %dx%d declared' % (layer, len(geom), M, N))
            if M * N > len(geom) or M < 2 or N < 2:
                return 0
        closed_m = bool(flags & 1)
        closed_n = bool(flags & 32)
        n = 0
        for i in range(M if closed_m else M - 1):
            i2 = (i + 1) % M
            for j in range(N if closed_n else N - 1):
                j2 = (j + 1) % N
                p00 = geom[i * N + j]
                p01 = geom[i * N + j2]
                p10 = geom[i2 * N + j]
                p11 = geom[i2 * N + j2]
                n += b.add_face(layer, [p00, p01, p11, p10])
        return n
    # ordinary 2-D / 3-D polyline
    elev = _first(ptags, 30, 0.0) or 0.0
    pts = []
    bulges = []
    for vflags, p, bulge, _ in verts:
        if vflags & 16:                              # spline frame control point
            continue
        if p is None:
            continue
        if flags & 8:                                # 3-D polyline: WCS vertices
            pts.append(p)
        else:                                        # 2-D polyline: elevation on the POLYLINE
            pts.append((p[0], p[1], p[2] if p[2] else elev))
        bulges.append(bulge)
    closed = bool(flags & 1)
    out = []
    for k, p in enumerate(pts):
        out.append(p)
        if bulges[k] and (k + 1 < len(pts) or closed):
            nxt = pts[(k + 1) % len(pts)]
            out.extend(_bulge_points(p, nxt, bulges[k]))
            b.bulge_arcs += 1
    if closed and len(out) > 2 and out[0] != out[-1]:
        out.append(out[0])
    b.add_polyline(layer, out, {'closed': closed, 'entity': 'POLYLINE'})
    return 0


def _handle_lwpolyline(b, layer, tags):
    flags = _first(tags, 70, 0, int) or 0
    elev = _first(tags, 38, 0.0) or 0.0
    pts = []
    bulges = []
    x = None
    for c, v in tags:
        if c == 10:
            try:
                x = float(v)
            except ValueError:
                x = None
        elif c == 20 and x is not None:
            try:
                pts.append((x, float(v), elev))
                bulges.append(0.0)
            except ValueError:
                pass
            x = None
        elif c == 42 and pts:
            try:
                bulges[-1] = float(v)
            except ValueError:
                pass
    closed = bool(flags & 1)
    out = []
    for k, p in enumerate(pts):
        out.append(p)
        if bulges[k] and (k + 1 < len(pts) or closed):
            nxt = pts[(k + 1) % len(pts)]
            out.extend(_bulge_points(p, nxt, bulges[k]))
            b.bulge_arcs += 1
    if closed and len(out) > 2 and out[0] != out[-1]:
        out.append(out[0])
    b.add_polyline(layer, out, {'closed': closed, 'entity': 'LWPOLYLINE'})


def read_dxf(src):
    """Parse a DXF file (path / bytes / file object) into a list of Mesh,
    LineSet and PointSet objects, one per (layer, kind)."""
    path = _src_path(src)
    text = _decode(_load_bytes(src))
    entities, layer_colors, acadver = _entities(text)
    b = _Builder()
    k = 0
    n = len(entities)
    while k < n:
        etype, tags = entities[k]
        layer = _first(tags, 8, '0', str) or '0'
        if etype in ('3DFACE', 'SOLID', 'TRACE'):
            corners = [_xyz(tags, base) for base in (10, 11, 12, 13)]
            corners = [c for c in corners if c is not None]
            if etype in ('SOLID', 'TRACE') and len(corners) == 4:
                corners = [corners[0], corners[1], corners[3], corners[2]]
            if len(corners) >= 3:
                b.add_face(layer, corners)
            else:
                b.warnings.append('%s with %d corners skipped' % (etype, len(corners)))
        elif etype == 'POLYLINE':
            vtags = []
            j = k + 1
            while j < n and entities[j][0] == 'VERTEX':
                vtags.append(entities[j][1])
                j += 1
            if j < n and entities[j][0] == 'SEQEND':
                j += 1
            _handle_polyline(b, layer, tags, vtags)
            k = j
            continue
        elif etype == 'LWPOLYLINE':
            _handle_lwpolyline(b, layer, tags)
        elif etype == 'LINE':
            p0 = _xyz(tags, 10)
            p1 = _xyz(tags, 11)
            if p0 and p1:
                b.add_polyline(layer, [p0, p1], {'entity': 'LINE'})
        elif etype == 'POINT':
            p = _xyz(tags, 10)
            if p:
                b.add_point(layer, p)
        elif etype in ('VERTEX', 'SEQEND'):
            b.warnings.append('orphan %s entity ignored' % etype)
        else:
            b.skip(etype)
        k += 1

    if b.bulge_arcs:
        b.warnings.append('%d bulge arc(s) tessellated into straight segments' % b.bulge_arcs)
    if not entities:
        b.warnings.append('no ENTITIES section found')

    objects = []
    for kind, layer in b.order:
        if kind == 'mesh':
            m = b.meshes[layer]
            obj = Mesh(m['verts'], m['tris'], name=layer, role='surface')
        elif kind == 'lineset':
            obj = b.lines[layer]
        else:
            obj = b.points[layer]
        aci = layer_colors.get(layer)
        if aci in _ACI_RGB:
            obj.color = list(_ACI_RGB[aci])
        obj.provenance = {'format': FORMAT_ID, 'path': path}
        obj.metadata['layer'] = layer
        obj.metadata['acadver'] = acadver
        obj.metadata['skipped'] = dict(b.skipped)
        obj.metadata['warnings'] = list(b.warnings)
        objects.append(obj)
    return objects
