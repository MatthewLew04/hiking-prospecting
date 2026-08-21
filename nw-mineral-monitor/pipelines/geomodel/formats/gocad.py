"""GOCAD ASCII objects: TSurf (.ts) -> Mesh, PLine (.pl) -> LineSet,
VSet (.vs) -> PointSet; stdlib only.

Reader (``read_gocad``) handles several objects concatenated in one file,
``VRTX`` / ``PVRTX`` / ``ATOM`` / ``PATOM`` records, ``TRGL`` (with optional
``TRGL_PROPERTIES`` values after the three ids), ``SEG`` (or the implicit
sequential segments of a PLine without SEG records), ``TFACE`` / ``ILINE`` /
``SUBVSET`` parts (merged; vertex ids may be global or restart per part),
``PROPERTIES`` + ``ESIZES`` + ``NO_DATA_VALUES`` (-> NaN), ``ZPOSITIVE
Depth`` (Z negated to elevation), HEADER ``name:`` and ``*solid*color:`` /
``*line*color:`` / ``*atoms*color:`` in ``r g b [a]`` (0-1 floats) or
``#rrggbb`` form.  ``BSTONE`` / ``BORDER`` and the many display keywords are
ignored; files with an unclosed HEADER block (they exist) still parse.

Writer (``write_gocad``) emits a TSurf (one TFACE, 1-based VRTX / PVRTX,
TRGL), a PLine (one ILINE per part) or a VSet (numeric attributes as PVRTX
PROPERTIES) with the GOCAD_ORIGINAL_COORDINATE_SYSTEM block.
"""
import io
import math
import os
import re

from ..model import Mesh, LineSet, PointSet, farray, iarray, NAN

FORMAT_ID = 'gocad_ts'
NO_DATA_DEFAULT = -99999.0

_SECTION_KEYWORDS = ('GOCAD_ORIGINAL_COORDINATE_SYSTEM', 'PROPERTIES', 'PROPERTY_CLASSES',
                     'TFACE', 'ILINE', 'SUBVSET', 'TVOLUME', 'VRTX', 'PVRTX', 'ATOM', 'PATOM',
                     'TRGL', 'SEG', 'TETRA', 'GEOLOGICAL_TYPE', 'GEOLOGICAL_FEATURE',
                     'STRATIGRAPHIC_POSITION', 'ESIZES', 'NO_DATA_VALUES', 'UNITS',
                     'TRGL_PROPERTIES', 'BSTONE', 'BORDER', 'END')


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
        return repr(NO_DATA_DEFAULT)
    r = repr(v)
    return r[:-2] if r.endswith('.0') else r


def _float(tok):
    try:
        return float(tok)
    except ValueError:
        t = tok.lower()
        if t in ('nan', 'na', 'none', 'null'):
            return NAN
        raise


def _parse_color(value):
    """'r g b [a]' floats in 0-1 (or 0-255), or '#rrggbb' -> [r, g, b] ints."""
    value = value.strip()
    if not value:
        return None
    if value.startswith('#'):
        h = value[1:]
        if len(h) >= 6:
            try:
                return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]
            except ValueError:
                return None
        return None
    toks = value.replace(',', ' ').split()
    vals = []
    for t in toks[:3]:
        try:
            vals.append(float(t))
        except ValueError:
            return None
    if len(vals) < 3:
        return None
    if max(vals) <= 1.0:
        return [int(round(v * 255)) for v in vals]
    return [int(round(v)) for v in vals]


def _quoted_tokens(line):
    return [t.strip('"') for t in re.findall(r'"[^"]*"|\S+', line)]


# ------------------------------------------------------------------- parser
class _GocadObject(object):
    def __init__(self, otype, version):
        self.otype = otype
        self.version = version
        self.header = {}
        self.coordsys = {}
        self.props = []          # vertex property names
        self.esizes = []
        self.nodata = []
        self.units = []
        self.tprops = []         # triangle property names
        self.tnodata = []
        self.geological_type = None
        self.ids = {}            # file id -> global vertex index (latest definition wins)
        self.verts = farray()
        self.pvals = []          # per vertex list of property values (floats) or None
        self.tris = iarray()
        self.tvals = []
        self.segs = []           # (a, b) global indices
        self.parts = []          # list of [global vertex indices] per ILINE / TFACE / SUBVSET
        self.part_has_seg = []
        self.warnings = []
        self.n_atoms = 0
        self.warned = set()

    def new_part(self):
        self.parts.append([])
        self.part_has_seg.append(False)

    def add_vertex(self, fid, x, y, z, pvals):
        gi = len(self.verts) // 3
        self.verts.extend((x, y, z))
        self.pvals.append(pvals)
        self.ids[fid] = gi
        if not self.parts:
            self.new_part()
        self.parts[-1].append(gi)
        return gi

    def lookup(self, fid):
        return self.ids.get(fid)


def _split_props(obj, values, names, esizes, nodata, warnings, what):
    """Map the raw property tokens of one record onto {column: float}."""
    vals = []
    for t in values:
        try:
            vals.append(_float(t))
        except ValueError:
            vals.append(NAN)
    sizes = list(esizes) if esizes and len(esizes) == len(names) else [1] * len(names)
    if sum(sizes) != len(vals):
        if len(vals) == len(names):
            sizes = [1] * len(names)
        else:
            key = 'count:' + what
            if key not in obj.warned:
                obj.warned.add(key)
                warnings.append('%s: %d property values for %d declared properties (%s)'
                                % (what, len(vals), len(names), ' '.join(names)))
    out = {}
    pos = 0
    for k, name in enumerate(names):
        sz = sizes[k] if k < len(sizes) else 1
        nd = nodata[k] if k < len(nodata) else None
        for c in range(sz):
            col = name if sz == 1 else '%s_%d' % (name, c + 1)
            if pos < len(vals):
                v = vals[pos]
                if nd is not None and v == nd:
                    v = NAN
                elif v == v and abs(v) >= 1e38:
                    v = NAN
                    if 'nodata1e38' not in obj.warned:
                        obj.warned.add('nodata1e38')
                        warnings.append('property values >= 1e38 treated as no-data (GOCAD convention)')
            else:
                v = NAN
            out[col] = v
            pos += 1
    # undeclared trailing values
    extra = 0
    while pos < len(vals):
        extra += 1
        out['%s_extra_%d' % (what, extra)] = vals[pos]
        pos += 1
    return out


def _parse_objects(text):
    objects = []
    cur = None
    in_header = False
    brace_depth = 0
    in_coordsys = False
    skip_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()
        first = up.split(None, 1)[0] if up else ''
        if cur is None:
            if up.startswith('GOCAD '):
                toks = line.split()
                cur = _GocadObject(toks[1] if len(toks) > 1 else '?',
                                   toks[2] if len(toks) > 2 else '')
                in_header = False
                brace_depth = 0
                in_coordsys = False
                skip_block = False
            continue
        # ---- HEADER { ... } and PROPERTY_CLASS_HEADER ... { ... } blocks
        if in_header or skip_block:
            if first.rstrip('{') in _SECTION_KEYWORDS or up.startswith('GOCAD '):
                # unclosed block: fall through to normal processing
                if in_header:
                    cur.warnings.append('HEADER block not closed with "}"')
                in_header = skip_block = False
                brace_depth = 0
            else:
                brace_depth += line.count('{') - line.count('}')
                if in_header and ':' in line and not line.startswith('#'):
                    key, val = line.split(':', 1)
                    cur.header[key.strip()] = val.strip()
                if brace_depth <= 0 and (line.endswith('}') or brace_depth < 0):
                    in_header = skip_block = False
                    brace_depth = 0
                continue
        if first.startswith('HEADER'):
            in_header = True
            brace_depth = line.count('{') - line.count('}')
            rest = line.split('{', 1)[1] if '{' in line else ''
            rest = rest.rstrip('}').strip()
            if ':' in rest:
                key, val = rest.split(':', 1)
                cur.header[key.strip()] = val.strip()
            if brace_depth <= 0 and '}' in line:
                in_header = False
            continue
        if first == 'HDR':
            rest = line[3:].strip()
            if ':' in rest:
                key, val = rest.split(':', 1)
                cur.header[key.strip()] = val.strip()
            continue
        if 'PROPERTY_CLASS_HEADER' in up and up.startswith(('PROPERTY_CLASS_HEADER', 'TRGL_PROPERTY_CLASS_HEADER')):
            depth = line.count('{') - line.count('}')
            if depth > 0 or '{' not in line:
                skip_block = True
                brace_depth = max(depth, 1)
            continue
        if in_coordsys:
            if first == 'END_ORIGINAL_COORDINATE_SYSTEM':
                in_coordsys = False
            else:
                toks = _quoted_tokens(line)
                if toks:
                    cur.coordsys[toks[0].upper()] = toks[1:] if len(toks) > 2 else (toks[1] if len(toks) > 1 else '')
            continue
        if first == 'GOCAD_ORIGINAL_COORDINATE_SYSTEM':
            in_coordsys = True
            continue
        toks = line.split()
        key = toks[0].upper()
        if key == 'END':
            objects.append(cur)
            cur = None
            continue
        if key in ('PROPERTIES', 'FIELDS'):
            cur.props = toks[1:]
        elif key == 'ESIZES':
            cur.esizes = [int(float(t)) for t in toks[1:]]
        elif key == 'NO_DATA_VALUES':
            cur.nodata = [float(t) for t in toks[1:]]
        elif key == 'UNITS':
            cur.units = toks[1:]
        elif key == 'TRGL_PROPERTIES':
            cur.tprops = toks[1:]
        elif key == 'TRGL_NO_DATA_VALUES':
            cur.tnodata = [float(t) for t in toks[1:]]
        elif key == 'GEOLOGICAL_TYPE':
            cur.geological_type = ' '.join(toks[1:])
        elif key in ('TFACE', 'ILINE', 'SUBVSET', 'TVOLUME'):
            cur.new_part()
        elif key in ('VRTX', 'PVRTX'):
            if len(toks) < 5:
                cur.warnings.append('short %s record: %r' % (key, line[:60]))
                continue
            try:
                fid = int(toks[1])
                x, y, z = float(toks[2]), float(toks[3]), float(toks[4])
            except ValueError:
                cur.warnings.append('unparsable %s record: %r' % (key, line[:60]))
                continue
            rest = toks[5:]
            if rest and rest[0].upper().startswith('CN'):   # control-node flag
                rest = rest[1:]
            pv = None
            if cur.props or key == 'PVRTX':
                pv = _split_props(cur, rest, cur.props, cur.esizes, cur.nodata, cur.warnings, key)
            cur.add_vertex(fid, x, y, z, pv)
        elif key in ('ATOM', 'PATOM'):
            if len(toks) < 3:
                continue
            try:
                fid, ref = int(toks[1]), int(toks[2])
            except ValueError:
                continue
            gi = cur.lookup(ref)
            if gi is None:
                cur.warnings.append('%s %d refers to unknown vertex %d' % (key, fid, ref))
                continue
            x, y, z = cur.verts[3 * gi], cur.verts[3 * gi + 1], cur.verts[3 * gi + 2]
            pv = cur.pvals[gi]
            if key == 'PATOM' and cur.props:
                pv = _split_props(cur, toks[3:], cur.props, cur.esizes, cur.nodata, cur.warnings, key)
            cur.add_vertex(fid, x, y, z, pv)
            cur.n_atoms += 1
        elif key == 'TRGL':
            if len(toks) < 4:
                continue
            try:
                ids = [int(toks[1]), int(toks[2]), int(toks[3])]
            except ValueError:
                cur.warnings.append('unparsable TRGL: %r' % line[:60])
                continue
            g = [cur.lookup(i) for i in ids]
            if any(v is None for v in g):
                cur.warnings.append('TRGL %s references an undefined vertex; skipped' % ' '.join(toks[1:4]))
                continue
            cur.tris.extend(g)
            if cur.tprops or len(toks) > 4:
                names = cur.tprops or ['trgl_prop_%d' % (k + 1) for k in range(len(toks) - 4)]
                cur.tvals.append(_split_props(cur, toks[4:], names, [], cur.tnodata, cur.warnings, 'TRGL'))
            else:
                cur.tvals.append(None)
        elif key == 'SEG':
            if len(toks) < 3:
                continue
            try:
                a, b = cur.lookup(int(toks[1])), cur.lookup(int(toks[2]))
            except ValueError:
                continue
            if a is None or b is None:
                cur.warnings.append('SEG %s references an undefined vertex; skipped' % ' '.join(toks[1:3]))
                continue
            cur.segs.append((a, b))
            if cur.part_has_seg:
                cur.part_has_seg[-1] = True
        # BSTONE, BORDER, PROPERTY_CLASSES, PROP_LEGAL_RANGES, ... ignored
    if cur is not None:
        cur.warnings.append('file ended without END keyword')
        objects.append(cur)
    return objects


def _chains_from_segments(segs):
    """Ordered vertex chains from (a, b) segments; open chains first (from
    vertices without a predecessor), then whatever is left (closed loops)."""
    nxt = {}
    has_prev = set()
    for a, b in segs:
        nxt.setdefault(a, []).append(b)
        has_prev.add(b)

    def walk(start):
        chain = [start]
        cur = start
        while nxt.get(cur):
            cur = nxt[cur].pop(0)
            chain.append(cur)
            if cur == start:
                break
        return chain

    chains = []
    for a in list(nxt):
        if a not in has_prev:
            while nxt.get(a):
                chains.append(walk(a))
    for a in list(nxt):
        while nxt.get(a):
            chains.append(walk(a))
    return chains


def _vertex_attributes(g):
    """Per-vertex property columns -> {name: {'location': 'vertices', 'values': array}}."""
    cols = []
    for pv in g.pvals:
        if pv:
            for k in pv:
                if k not in cols:
                    cols.append(k)
    out = {}
    n = len(g.verts) // 3
    for c in cols:
        arr = farray()
        for i in range(n):
            pv = g.pvals[i]
            arr.append(pv.get(c, NAN) if pv else NAN)
        out[c] = {'location': 'vertices', 'values': arr}
    return out


def _face_attributes(g):
    cols = []
    for tv in g.tvals:
        if tv:
            for k in tv:
                if k not in cols:
                    cols.append(k)
    out = {}
    for c in cols:
        arr = farray()
        for tv in g.tvals:
            arr.append(tv.get(c, NAN) if tv else NAN)
        out[c] = {'location': 'faces', 'values': arr}
    return out


def _pick_color(header, prefs):
    for key in prefs:
        if key in header:
            c = _parse_color(header[key])
            if c:
                return c
    for key, val in header.items():
        if key.lower().endswith('color'):
            c = _parse_color(val)
            if c:
                return c
    return None


def _finish(obj, g, path, depth_flipped):
    obj.provenance = {'format': FORMAT_ID, 'path': path, 'gocad_type': g.otype}
    md = obj.metadata
    md['gocad_header'] = dict(g.header)
    md['coordinate_system'] = dict(g.coordsys)
    if g.geological_type:
        md['geological_type'] = g.geological_type
    if g.props:
        md['properties'] = list(g.props)
        if g.units:
            md['property_units'] = dict(zip(g.props, g.units))
    if g.tprops:
        md['triangle_properties'] = list(g.tprops)
    if depth_flipped:
        g.warnings.append('ZPOSITIVE Depth: Z negated to elevation')
    if g.n_atoms:
        md['atoms'] = g.n_atoms
    md['warnings'] = list(g.warnings)
    return obj


def _role_for(gtype):
    t = (gtype or '').lower()
    if 'fault' in t:
        return 'fault'
    if 'topo' in t:
        return 'topography'
    if t in ('top', 'boundary', 'unconformity', 'horizon'):
        return 'contact'
    return 'surface'


def read_gocad(src):
    """Parse one or more concatenated GOCAD ASCII objects.  Returns a list:
    TSurf -> Mesh, PLine -> LineSet, VSet -> PointSet.  Other object types
    (Well, Voxet, SGrid, TSolid ...) are skipped with a warning on the
    objects that were read (or raise ValueError if nothing else is there)."""
    path = _src_path(src)
    text = _decode(_load_bytes(src))
    parsed = _parse_objects(text)
    out = []
    skipped = []
    for g in parsed:
        name = g.header.get('name') or g.header.get('NAME') or g.otype
        zpos = g.coordsys.get('ZPOSITIVE', '')
        zpos = zpos if isinstance(zpos, str) else ' '.join(zpos)
        depth = zpos.strip().lower() == 'depth'
        if depth:
            for i in range(2, len(g.verts), 3):
                g.verts[i] = -g.verts[i]
        otype = g.otype.lower()
        if otype == 'tsurf':
            mesh = Mesh(g.verts, g.tris, name=name, role=_role_for(g.geological_type))
            mesh.attributes.update(_vertex_attributes(g))
            mesh.attributes.update(_face_attributes(g))
            c = _pick_color(g.header, ('*solid*color', 'solid*color', 'color'))
            if c:
                mesh.color = c
            if len(g.parts) > 1:
                mesh.metadata['tfaces'] = len(g.parts)
            if not len(g.tris):
                g.warnings.append('TSurf has no TRGL records')
            out.append(_finish(mesh, g, path, depth))
        elif otype == 'pline':
            ls = LineSet(name=name, role='lines')
            # segments: explicit SEG records per part, otherwise the implicit
            # sequential chain of the part's vertices
            seg_by_part = {}
            part_of = {}
            for pk, p in enumerate(g.parts):
                for gi in p:
                    part_of[gi] = pk
            for a, b in g.segs:
                seg_by_part.setdefault(part_of.get(a, 0), []).append((a, b))
            ls.vertices = farray(g.verts)
            segs = iarray()
            parts = []
            for pk, p in enumerate(g.parts):
                if pk < len(g.part_has_seg) and g.part_has_seg[pk]:
                    part_segs = seg_by_part.get(pk, [])
                elif len(p) > 1:
                    part_segs = [(p[k], p[k + 1]) for k in range(len(p) - 1)]
                else:
                    part_segs = []
                for a, b in part_segs:
                    segs.extend((a, b))
                parts.extend(_chains_from_segments(part_segs))
            ls.segments = segs
            ls.parts = parts
            ls.features = [{'iline': k} for k in range(len(parts))]
            if g.props:
                attrs = _vertex_attributes(g)
                ls.metadata['vertex_properties'] = {k: list(v['values']) for k, v in attrs.items()}
            c = _pick_color(g.header, ('*line*color', 'line*color', 'color', '*solid*color'))
            if c:
                ls.color = c
            if not len(segs):
                g.warnings.append('PLine has no segments')
            out.append(_finish(ls, g, path, depth))
        elif otype == 'vset':
            attrs = {k: list(v['values']) for k, v in _vertex_attributes(g).items()}
            ps = PointSet(g.verts, attributes=attrs, name=name, role='points')
            c = _pick_color(g.header, ('*atoms*color', 'atoms*color', 'color', '*solid*color'))
            if c:
                ps.color = c
            out.append(_finish(ps, g, path, depth))
        else:
            skipped.append('%s %r' % (g.otype, name))
    if skipped:
        if not out:
            raise ValueError('no TSurf / PLine / VSet objects in file (found: %s)' % ', '.join(skipped))
        for o in out:
            o.metadata.setdefault('warnings', []).append('skipped unsupported object(s): %s' % ', '.join(skipped))
    if not out and not parsed:
        raise ValueError('not a GOCAD ASCII file (no "GOCAD <Type>" line)')
    return out


# ------------------------------------------------------------------- writer
def _coordsys_lines(zpositive):
    return ['GOCAD_ORIGINAL_COORDINATE_SYSTEM',
            'NAME Default',
            'AXIS_NAME "X" "Y" "Z"',
            'AXIS_UNIT "m" "m" "m"',
            'ZPOSITIVE %s' % zpositive,
            'END_ORIGINAL_COORDINATE_SYSTEM']


def _color_line(key, color):
    try:
        r, g, b = [max(0.0, min(1.0, float(c) / 255.0)) for c in color[:3]]
    except (TypeError, ValueError):
        r, g, b = 0.5, 0.5, 0.5
    return '%s:%s %s %s 1' % (key, _fmt(r), _fmt(g), _fmt(b))


def _numeric_columns(attributes, location, n):
    """[(name, values)] of per-vertex numeric attributes of length n."""
    cols = []
    for name, spec in attributes.items():
        if isinstance(spec, dict):
            if spec.get('location', 'vertices') != location:
                continue
            vals = spec.get('values')
        else:
            vals = spec
        if vals is None or len(vals) != n:
            continue
        ok = True
        fl = []
        for v in vals:
            if v is None or v == '':
                fl.append(NAN)
                continue
            if isinstance(v, bool):
                ok = False
                break
            try:
                fl.append(float(v))
            except (TypeError, ValueError):
                ok = False
                break
        if ok:
            cols.append((re.sub(r'\s+', '_', str(name)), fl))
    return cols


def write_gocad(obj, dst, zpositive='Elevation', name=None):
    """Write a Mesh as TSurf, a LineSet as PLine or a PointSet as VSet.
    ``zpositive`` 'Elevation' (default) or 'Depth' (Z negated on output).
    Returns the path written (bytes for a BytesIO)."""
    if zpositive not in ('Elevation', 'Depth'):
        raise ValueError("zpositive must be 'Elevation' or 'Depth'")
    zsign = -1.0 if zpositive == 'Depth' else 1.0
    oname = (name or obj.name or obj.kind).strip()
    kind = obj.kind
    L = []
    w = L.append
    if kind == 'mesh':
        n = obj.n_vertices
        cols = _numeric_columns(obj.attributes, 'vertices', n)
        fcols = _numeric_columns(obj.attributes, 'faces', obj.n_triangles)
        w('GOCAD TSurf 1')
        w('HEADER {')
        w('name:' + oname)
        w(_color_line('*solid*color', obj.color))
        w('}')
        L.extend(_coordsys_lines(zpositive))
        if obj.role in ('fault', 'topography'):
            w('GEOLOGICAL_TYPE %s' % ('fault' if obj.role == 'fault' else 'topographic'))
        if cols:
            w('PROPERTIES ' + ' '.join(c[0] for c in cols))
            w('PROP_LEGAL_RANGES ' + ' '.join('**none** **none**' for _ in cols))
            w('NO_DATA_VALUES ' + ' '.join(_fmt(NO_DATA_DEFAULT) for _ in cols))
            w('PROPERTY_CLASSES ' + ' '.join(c[0].lower() for c in cols))
            w('PROPERTY_KINDS ' + ' '.join('"Real Number"' for _ in cols))
            w('PROPERTY_SUBCLASSES ' + ' '.join('QUANTITY Float' for _ in cols))
            w('ESIZES ' + ' '.join('1' for _ in cols))
            w('UNITS ' + ' '.join('unitless' for _ in cols))
        if fcols:
            w('TRGL_PROPERTIES ' + ' '.join(c[0] for c in fcols))
            w('TRGL_NO_DATA_VALUES ' + ' '.join(_fmt(NO_DATA_DEFAULT) for _ in fcols))
            w('TRGL_ESIZES ' + ' '.join('1' for _ in fcols))
        w('TFACE')
        v = obj.vertices
        for i in range(n):
            x, y, z = v[3 * i], v[3 * i + 1], zsign * v[3 * i + 2]
            if cols:
                w('PVRTX %d %s %s %s %s' % (i + 1, _fmt(x), _fmt(y), _fmt(z),
                                           ' '.join(_fmt(c[1][i]) for c in cols)))
            else:
                w('VRTX %d %s %s %s' % (i + 1, _fmt(x), _fmt(y), _fmt(z)))
        t = obj.triangles
        for k in range(0, len(t) - 2, 3):
            line = 'TRGL %d %d %d' % (t[k] + 1, t[k + 1] + 1, t[k + 2] + 1)
            if fcols:
                line += ' ' + ' '.join(_fmt(c[1][k // 3]) for c in fcols)
            w(line)
        w('END')
    elif kind == 'lineset':
        w('GOCAD PLine 1')
        w('HEADER {')
        w('name:' + oname)
        w(_color_line('*line*color', obj.color))
        w('}')
        L.extend(_coordsys_lines(zpositive))
        parts = obj.parts or []
        vid = 0
        for p in parts:
            if len(p) < 2:
                continue
            w('ILINE')
            ids = []
            for i in p:
                x, y, z = obj.vertex(i)
                vid += 1
                ids.append(vid)
                w('VRTX %d %s %s %s' % (vid, _fmt(x), _fmt(y), _fmt(zsign * z)))
            for k in range(len(ids) - 1):
                w('SEG %d %d' % (ids[k], ids[k + 1]))
        w('END')
    elif kind == 'points':
        n = obj.n
        cols = _numeric_columns(obj.attributes, 'vertices', n)
        w('GOCAD VSet 1')
        w('HEADER {')
        w('name:' + oname)
        w(_color_line('*atoms*color', obj.color))
        w('}')
        L.extend(_coordsys_lines(zpositive))
        if cols:
            w('PROPERTIES ' + ' '.join(c[0] for c in cols))
            w('PROP_LEGAL_RANGES ' + ' '.join('**none** **none**' for _ in cols))
            w('NO_DATA_VALUES ' + ' '.join(_fmt(NO_DATA_DEFAULT) for _ in cols))
            w('PROPERTY_CLASSES ' + ' '.join(c[0].lower() for c in cols))
            w('PROPERTY_KINDS ' + ' '.join('"Real Number"' for _ in cols))
            w('PROPERTY_SUBCLASSES ' + ' '.join('QUANTITY Float' for _ in cols))
            w('ESIZES ' + ' '.join('1' for _ in cols))
            w('UNITS ' + ' '.join('unitless' for _ in cols))
        w('SUBVSET')
        for i in range(n):
            x, y, z = obj.point(i)
            if cols:
                w('PVRTX %d %s %s %s %s' % (i + 1, _fmt(x), _fmt(y), _fmt(zsign * z),
                                           ' '.join(_fmt(c[1][i]) for c in cols)))
            else:
                w('VRTX %d %s %s %s' % (i + 1, _fmt(x), _fmt(y), _fmt(zsign * z)))
        w('END')
    else:
        raise TypeError('write_gocad: cannot write a %r object' % kind)
    return _emit(dst, ('\n'.join(L) + '\n').encode('utf-8'))
