"""Open Mining Format v0.9 (``OMF-v0.9.0``; the ``omf`` 1.x Python package,
Leapfrog Geo <= 2024.1 "OMF" import/export) reader and writer.

File layout (verified against omf 1.0.1 and hex dumps of its output):

* bytes 0-3 magic ``84 83 82 81``; 4-35 ``'OMF-v0.9.0'`` NUL-padded; 36-51
  project UUID (RFC-4122 big-endian field order = ``uuid.bytes``); 52-59
  little-endian uint64 offset of the JSON registry;
* binary blobs from offset 60: each array is ``zlib.compress`` of the C-order
  little-endian ``<f8`` (floats) or ``<i8`` (ints) bytes; images are zlib'd
  PNGs;
* from the JSON offset to EOF: one JSON dict keyed by UUID strings; every
  object has ``__class__``, ``date_created``, ``date_modified`` and references
  other objects by UUID string; array blobs are ``{"start", "length",
  "dtype"}`` dicts.

Mapping to the geomodel objects
-------------------------------
PointSetElement <-> PointSet, LineSetElement <-> LineSet, SurfaceElement with
SurfaceGeometry <-> Mesh, SurfaceElement with SurfaceGridGeometry <-> Grid2D
(uniform tensors, horizontal axes; anything else becomes a Mesh),
VolumeElement <-> BlockModel (uniform tensors, vertical W axis).  Element and
project origins are added so model coordinates are absolute.

Data arrays become attribute "records" (see ``_apply_records``): ScalarData ->
numeric, StringData -> text, MappedData -> category, Vector2/3Data -> per
component numeric columns, ColorData -> colour, DateTimeData -> ISO text.
Hints needed to write them back (vector components, categories, colour and
date columns, colormaps) live in ``obj.metadata`` so a read/write cycle is
faithful.  Textures are skipped with a warning in ``project.metadata['warnings']``.

This module also holds the helpers shared with ``omf2.py`` (the attribute
record mapping, grid/axis conversions, source/destination handling).
"""
import array
import io
import json
import math
import struct
import sys
import time
import uuid
import zlib

from .. import __version__ as _GEOMODEL_VERSION
from ..model import (Grid2D, Mesh, LineSet, PointSet, BlockModel, Project,
                     farray, iarray, NAN, _all_numeric)

OMF1_MAGIC = b'\x84\x83\x82\x81'
OMF1_VERSION = b'OMF-v0.9.0'
APPLICATION = 'nw-mineral-monitor geomodel %s' % _GEOMODEL_VERSION

# record locations (v0.9 vocabulary, used by both modules)
LOC_VERTICES, LOC_SEGMENTS, LOC_FACES, LOC_CELLS = 'vertices', 'segments', 'faces', 'cells'
# v0.9 has no boolean data and no nullable date-times: these description
# suffixes mark the substitutes so a read restores the original type
MARK_BOOLEAN = '[boolean]'
MARK_DATETIME = '[datetime]'


class OmfError(ValueError):
    """Unreadable OMF data."""


# ==================================================================== helpers
def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _read_source(src):
    """path | bytes | file object -> (bytes, label)."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src), '<bytes>'
    if hasattr(src, 'read'):
        return src.read(), getattr(src, 'name', '<stream>')
    with open(src, 'rb') as fh:
        return fh.read(), src


def _write_dest(data, dst):
    """Write bytes to a path or binary file object; returns path or bytes."""
    if dst is None:
        return data
    if hasattr(dst, 'write'):
        dst.write(data)
        if isinstance(dst, io.BytesIO):
            return dst.getvalue()
        return getattr(dst, 'name', None) or data
    with open(dst, 'wb') as fh:
        fh.write(data)
    return dst


def _objects_of(project_or_objects):
    """Accept a Project or a list / single model object -> (Project|None, [objects])."""
    if isinstance(project_or_objects, Project):
        return project_or_objects, list(project_or_objects.objects)
    if hasattr(project_or_objects, 'kind'):
        return None, [project_or_objects]
    return None, list(project_or_objects or [])


def _unique_names(names, fallback):
    """Make names unique and non-empty (OMF v2 warns on duplicates)."""
    out = []
    used = set()
    for k, n in enumerate(names):
        n = (n or '').strip() or ('%s-%d' % (fallback, k + 1))
        cand, c = n, 1
        while cand in used:
            c += 1
            cand = '%s (%d)' % (n, c)
        used.add(cand)
        out.append(cand)
    return out


def _isnan(v):
    return v is None or (isinstance(v, float) and v != v)


def _vec_norm(v):
    return math.sqrt(sum(c * c for c in v))


def _vec_cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _vec_dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _uniform(sizes, tol=1e-9):
    """(is_uniform, size) for a tensor spacing list."""
    sizes = [float(s) for s in sizes]
    if not sizes:
        return False, 0.0
    s0 = sizes[0]
    scale = max(abs(s0), 1e-12)
    return all(abs(s - s0) <= tol * scale for s in sizes), s0


def _iso_from_epoch_micros(us):
    if us is None:
        return None
    secs, frac = divmod(int(us), 1000000)
    t = time.gmtime(secs)
    base = time.strftime('%Y-%m-%dT%H:%M:%S', t)
    if frac:
        base += ('.%06d' % frac).rstrip('0')
    return base + 'Z'


def _iso_from_epoch_days(days):
    if days is None:
        return None
    return time.strftime('%Y-%m-%d', time.gmtime(int(days) * 86400))


def _epoch_micros_from_iso(s):
    """ISO-8601 (date or date-time, 'Z' or offset) -> microseconds since epoch
    or None when unparseable."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        import calendar
        txt = s.replace('T', ' ')
        offset = 0
        if txt.endswith('Z'):
            txt = txt[:-1]
        elif len(txt) > 6 and txt[-6] in '+-' and txt[-3] == ':':
            sign = 1 if txt[-6] == '+' else -1
            offset = sign * (int(txt[-5:-3]) * 3600 + int(txt[-2:]) * 60)
            txt = txt[:-6]
        frac = 0
        if '.' in txt:
            txt, f = txt.split('.', 1)
            frac = int((f + '000000')[:6])
        fmt = '%Y-%m-%d %H:%M:%S' if ' ' in txt else '%Y-%m-%d'
        tm = time.strptime(txt, fmt)
        return (calendar.timegm(tm) - offset) * 1000000 + frac
    except (ValueError, OverflowError):
        return None


def _num(v):
    """Value -> float (NaN for missing / non-numeric)."""
    if v is None or v == '':
        return NAN
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return NAN


def _is_bool_column(col):
    seen = False
    for v in col:
        if v is None:
            continue
        if not isinstance(v, bool):
            return False
        seen = True
    return seen


def _color4(c):
    """Any colour spec -> [r, g, b, a] ints or None."""
    if c is None:
        return None
    if isinstance(c, str):
        s = c.lstrip('#')
        if len(s) in (6, 8):
            try:
                vals = [int(s[k:k + 2], 16) for k in range(0, len(s), 2)]
                return vals + [255] * (4 - len(vals))
            except ValueError:
                return None
        return None
    try:
        vals = [max(0, min(255, int(round(float(v))))) for v in c]
    except (TypeError, ValueError):
        return None
    if len(vals) == 3:
        vals.append(255)
    return vals[:4] if len(vals) >= 4 else None


# ============================================================ axis handling
def _horizontal_rotation(u, v, warn, label):
    """Unit axes u, v of a grid -> (rotation_deg_ccw, flip_v) or None when the
    grid is not horizontal.  ``flip_v`` = v points clockwise of u (left-handed
    grid) so rows must be reversed to fit Grid2D's convention."""
    if abs(u[2]) > 1e-6 or abs(v[2]) > 1e-6:
        warn('%s: grid axes are not horizontal; converted to a triangulated mesh' % label)
        return None
    rot = round(math.degrees(math.atan2(u[1], u[0])), 9) + 0.0   # (+0.0 turns -0.0 into 0.0)
    perp = [-u[1], u[0], 0.0]
    d = _vec_dot(perp, v)
    if abs(abs(d) - 1.0) > 1e-4:
        warn('%s: grid axes are not orthogonal unit vectors (|u.v_perp| = %.4f); mesh conversion used' % (label, abs(d)))
        return None
    return rot, d < 0


def _rotation_axes(rotation):
    r = math.radians(rotation)
    c, s = math.cos(r), math.sin(r)
    return [c, s, 0.0], [-s, c, 0.0]


def _azimuth_axes(azimuth):
    """Block-model axes for an azimuth clockwise from north (model convention)."""
    r = math.radians(azimuth)
    c, s = math.cos(r), math.sin(r)
    return [c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]


def _grid_from_tensor_surface(name, origin, u, v, tensor_u, tensor_v, heights, warn, **kw):
    """OMF grid surface (node offsets along u x v) -> Grid2D when regular and
    horizontal, else a Mesh.  ``heights`` may be None (flat) and may hold
    NaN.  Returns (object, is_grid, tri_cells) where ``tri_cells`` (mesh case
    only) lists the source cell index of every triangle so per-cell
    attributes can be re-aligned."""
    nu, nv = len(tensor_u), len(tensor_v)
    nx, ny = nu + 1, nv + 1
    n = nx * ny
    if heights is None:
        heights = [0.0] * n
    if len(heights) != n:
        raise OmfError('%s: %d heights for a %d x %d node grid' % (name, len(heights), nx, ny))
    w = _vec_cross(u, v)
    wn = _vec_norm(w) or 1.0
    w = [c / wn for c in w]
    ok_u, du = _uniform(tensor_u)
    ok_v, dv = _uniform(tensor_v)
    rot = _horizontal_rotation(u, v, warn, name) if (ok_u and ok_v) else None
    if not (ok_u and ok_v):
        warn('%s: variable grid spacing; converted to a triangulated mesh' % name)
    if rot is not None and nu >= 1 and nv >= 1:
        rotation, flip_v = rot
        vals = array.array('d', [NAN]) * n
        if not flip_v:
            x0, y0 = origin[0], origin[1]
            for k in range(n):
                h = heights[k]
                vals[k] = NAN if _isnan(h) else origin[2] + h * w[2]
        else:
            # left-handed grid: start from the far row and reverse j
            x0 = origin[0] + nv * dv * v[0]
            y0 = origin[1] + nv * dv * v[1]
            for j in range(ny):
                src_j = nv - j
                for i in range(nx):
                    h = heights[src_j * nx + i]
                    vals[j * nx + i] = NAN if _isnan(h) else origin[2] + h * w[2]
        return Grid2D(nx, ny, x0, y0, du, dv, vals, rotation=rotation, name=name, **kw), True, None
    # general case: triangulate
    cu = [0.0]
    for s in tensor_u:
        cu.append(cu[-1] + float(s))
    cv = [0.0]
    for s in tensor_v:
        cv.append(cv[-1] + float(s))
    verts = array.array('d')
    valid = [False] * n
    for j in range(ny):
        for i in range(nx):
            k = j * nx + i
            h = heights[k]
            hh = 0.0 if _isnan(h) else h
            valid[k] = not _isnan(h)
            verts.extend((origin[0] + cu[i] * u[0] + cv[j] * v[0] + hh * w[0],
                          origin[1] + cu[i] * u[1] + cv[j] * v[1] + hh * w[1],
                          origin[2] + cu[i] * u[2] + cv[j] * v[2] + hh * w[2]))
    tris = array.array('I')
    tri_cells = []
    for j in range(nv):
        for i in range(nu):
            p, q = j * nx + i, j * nx + i + 1
            r, s = (j + 1) * nx + i, (j + 1) * nx + i + 1
            cell = j * nu + i
            if valid[p] and valid[q] and valid[s]:
                tris.extend((p, q, s))
                tri_cells.append(cell)
            if valid[p] and valid[s] and valid[r]:
                tris.extend((p, s, r))
                tri_cells.append(cell)
    mesh = Mesh(verts, tris, name=name, **kw)
    mesh.metadata['from_grid_surface'] = True
    return mesh, False, tri_cells


def _blockmodel_from_grid(name, origin, u, v, w, tu, tv, tw, warn, **kw):
    """OMF 3-D grid -> (BlockModel, index_remap) or (None, None).
    ``index_remap`` maps a model cell index to the source cell index (axis
    flips), or None when the ordering is identical."""
    ok_u, du = _uniform(tu)
    ok_v, dv = _uniform(tv)
    ok_w, dw = _uniform(tw)
    nu, nv, nw = len(tu), len(tv), len(tw)
    if not (ok_u and ok_v and ok_w):
        warn('%s: tensor block model with variable block sizes is not supported; skipped' % name)
        return None, None
    if not (nu and nv and nw):
        warn('%s: empty block model; skipped' % name)
        return None, None
    if abs(u[2]) > 1e-6 or abs(v[2]) > 1e-6 or abs(abs(w[2]) - 1.0) > 1e-6:
        warn('%s: block model axes are not (horizontal u, v; vertical w); skipped' % name)
        return None, None
    azimuth = round(math.degrees(math.atan2(-u[1], u[0])), 9) + 0.0
    _, v_expect, _ = _azimuth_axes(azimuth)
    d = _vec_dot(v, v_expect)
    if abs(abs(d) - 1.0) > 1e-4:
        warn('%s: block model u and v axes are not orthogonal; skipped' % name)
        return None, None
    flip_v = d < 0
    flip_w = w[2] < 0
    ox = origin[0] + (nv * dv * v[0] if flip_v else 0.0)
    oy = origin[1] + (nv * dv * v[1] if flip_v else 0.0)
    oz = origin[2] + (nw * dw * w[2] if flip_w else 0.0)
    bm = BlockModel([ox, oy, oz], [du, dv, dw], [nu, nv, nw], azimuth=azimuth, name=name, **kw)
    remap = None
    if flip_v or flip_w:
        remap = []
        for k in range(nw):
            sk = nw - 1 - k if flip_w else k
            for j in range(nv):
                sj = nv - 1 - j if flip_v else j
                base = nu * (sj + nv * sk)
                remap.extend(range(base, base + nu))
    return bm, remap


def _remap(values, remap):
    if remap is None:
        return values
    return [values[i] for i in remap]


# ======================================================= attribute records
# A record is a dict: {'name', 'location', 'type', 'values', ...} where type is
# one of number | text | category | boolean | color | vector | datetime.
# Optional keys: 'names' + 'colors' + 'sub' (category), 'dim' (vector),
# 'units', 'description', 'colormap'.

def _record(name, location, rtype, values, **extra):
    d = {'name': name, 'location': location, 'type': rtype, 'values': values}
    d.update(extra)
    return d


def _meta_list(obj, key, value):
    lst = obj.metadata.setdefault(key, [])
    if value not in lst:
        lst.append(value)


def _apply_records(obj, records, warn):
    """Attach attribute records read from a file to a model object.

    PointSet columns and BlockModel attributes take every type directly
    (numbers as float arrays with NaN for null, text / booleans / colours /
    ISO date-times as lists, categories as their names).  Mesh and LineSet
    ``attributes`` are numeric only (categories become their indices,
    booleans 1/0/NaN); their text-like attributes are kept in
    ``metadata['text_attributes']``.  Vectors are split into ``<name>_x`` ..
    columns.  The hints needed to write everything back
    (``vector_attributes``, ``categories``, ``boolean_attributes``,
    ``color_attributes``, ``datetime_attributes``, ``attribute_units``,
    ``attribute_descriptions``, ``colormaps``) go in ``obj.metadata``.
    """
    kind = obj.kind
    allowed = {'points': (LOC_VERTICES,), 'mesh': (LOC_VERTICES, LOC_FACES),
               'lineset': (LOC_VERTICES, LOC_SEGMENTS), 'blockmodel': (LOC_CELLS,)}.get(kind, ())
    columnar = kind in ('points', 'blockmodel')
    for rec in records:
        name, loc, rtype = rec['name'], rec['location'], rec['type']
        label = '%s/%s' % (obj.name, name)
        if loc not in allowed:
            warn('%s: attribute location %r is not supported on a %s; skipped' % (label, loc, kind))
            continue
        vals = rec['values']
        if rtype == 'vector':
            dim = rec.get('dim') or 3
            comps = ['%s_%s' % (name, c) for c in 'xyz'[:dim]]
            obj.metadata.setdefault('vector_attributes', {})[name] = comps
            for ci, cname in enumerate(comps):
                _set_numeric(obj, cname, loc, farray(NAN if v is None else v[ci] for v in vals))
        elif rtype == 'category':
            names = list(rec.get('names') or [])
            entry = {'names': names, 'index': not columnar}
            if rec.get('colors'):
                entry['colors'] = rec['colors']
            if rec.get('sub'):
                entry['attributes'] = rec['sub']
            obj.metadata.setdefault('categories', {})[name] = entry
            if columnar:
                _set_text(obj, name, loc, [None if (i is None or i < 0 or i >= len(names)) else names[i] for i in vals])
            else:
                _set_numeric(obj, name, loc, farray(NAN if (i is None or i < 0) else i for i in vals))
        elif rtype == 'number':
            _set_numeric(obj, name, loc, farray(vals))
        elif rtype == 'boolean':
            _meta_list(obj, 'boolean_attributes', name)
            if columnar:
                _set_text(obj, name, loc, [None if v is None else bool(v) for v in vals], 'boolean')
            else:
                _set_numeric(obj, name, loc, farray(NAN if v is None else (1.0 if v else 0.0) for v in vals))
        elif rtype == 'text':
            _set_text(obj, name, loc, [None if v is None else str(v) for v in vals])
        elif rtype == 'datetime':
            _meta_list(obj, 'datetime_attributes', name)
            _set_text(obj, name, loc, [None if v is None else str(v) for v in vals], 'datetime')
        elif rtype == 'color':
            _meta_list(obj, 'color_attributes', name)
            _set_text(obj, name, loc, [_color4(v) for v in vals], 'color')
        else:
            warn('%s: attribute type %r is not supported; skipped' % (label, rtype))
            continue
        if rec.get('units'):
            obj.metadata.setdefault('attribute_units', {})[name] = rec['units']
        if rec.get('description'):
            obj.metadata.setdefault('attribute_descriptions', {})[name] = rec['description']
        if rec.get('colormap'):
            obj.metadata.setdefault('colormaps', {})[name] = rec['colormap']


def _set_numeric(obj, name, loc, col):
    if obj.kind == 'points':
        obj.attributes[name] = col
    elif obj.kind == 'blockmodel':
        obj.add_attribute(name, col)
    else:
        if not hasattr(obj, 'attributes'):
            obj.attributes = {}
        obj.attributes[name] = {'location': loc, 'values': col}


def _set_text(obj, name, loc, col, kind_hint='text'):
    if obj.kind == 'points':
        obj.attributes[name] = list(col)
    elif obj.kind == 'blockmodel':
        obj.add_attribute(name, list(col), kind=kind_hint)
    else:
        obj.metadata.setdefault('text_attributes', {})[name] = {
            'location': loc, 'type': kind_hint, 'values': list(col)}


def _collect_records(obj, warn):
    """Model object -> attribute records for writing (inverse of
    ``_apply_records``; honours the metadata hints)."""
    meta = obj.metadata or {}
    vec_hints = dict(meta.get('vector_attributes') or {})
    cat_hints = dict(meta.get('categories') or {})
    bool_hints = set(meta.get('boolean_attributes') or [])
    color_hints = set(meta.get('color_attributes') or [])
    date_hints = set(meta.get('datetime_attributes') or [])
    units = meta.get('attribute_units') or {}
    descs = meta.get('attribute_descriptions') or {}
    cmaps = meta.get('colormaps') or {}
    out = []
    kind = obj.kind

    def finish(rec):
        rec['units'] = units.get(rec['name'], '')
        rec['description'] = descs.get(rec['name'], '')
        if rec['name'] in cmaps:
            rec['colormap'] = cmaps[rec['name']]
        out.append(rec)

    if kind == 'points':
        cols = dict((k, (LOC_VERTICES, v, None)) for k, v in obj.attributes.items())
        n = obj.n
    elif kind == 'blockmodel':
        cols = dict((k, (LOC_CELLS, a['values'], a.get('type', 'number'))) for k, a in obj.attributes.items())
        n = obj.n
    elif kind in ('mesh', 'lineset'):
        cols = {}
        for k, a in (getattr(obj, 'attributes', None) or {}).items():
            loc = a.get('location', LOC_VERTICES)
            if loc == 'cells':
                loc = LOC_FACES if kind == 'mesh' else LOC_SEGMENTS
            cols[k] = (loc, a['values'], a.get('type', 'number'))
        for k, a in (meta.get('text_attributes') or {}).items():
            if k not in cols:
                cols[k] = (a.get('location', LOC_VERTICES), a['values'], a.get('type', 'text'))
        n = None
    else:
        return out
    consumed = set()
    for vname, comps in vec_hints.items():
        if all(c in cols for c in comps) and comps:
            loc = cols[comps[0]][0]
            arrays = [list(cols[c][1]) for c in comps]
            vals = []
            for k in range(len(arrays[0])):
                row = [_num(a[k]) for a in arrays]
                vals.append(None if any(x != x for x in row) else row)
            consumed.update(comps)
            finish(_record(vname, loc, 'vector', vals, dim=len(comps)))
    for name, (loc, col, ctype) in cols.items():
        if name in consumed:
            continue
        col = list(col)
        if n is not None and len(col) != n:
            warn('%s/%s: %d values for %d items; skipped' % (obj.name, name, len(col), n))
            continue
        if name in cat_hints:
            hint = cat_hints[name]
            names = list(hint.get('names') or [])
            if hint.get('index') or ctype == 'number' and not any(isinstance(v, str) for v in col):
                idx = []
                for v in col:
                    f = _num(v)
                    idx.append(None if (f != f or f < 0) else int(f))
            else:
                lookup = dict((nm, i) for i, nm in enumerate(names))
                idx = []
                for v in col:
                    if v is None or v == '':
                        idx.append(None)
                        continue
                    v = str(v)
                    if v not in lookup:
                        lookup[v] = len(names)
                        names.append(v)
                    idx.append(lookup[v])
            top = max([i for i in idx if i is not None] + [-1])
            while len(names) <= top:
                names.append('category %d' % len(names))
            finish(_record(name, loc, 'category', idx, names=names, colors=hint.get('colors'),
                           sub=hint.get('attributes')))
            continue
        if name in color_hints or ctype == 'color':
            finish(_record(name, loc, 'color', [_color4(v) for v in col]))
            continue
        if name in date_hints or ctype == 'datetime':
            finish(_record(name, loc, 'datetime', [None if v in (None, '') else str(v) for v in col]))
            continue
        if name in bool_hints or ctype == 'boolean' or _is_bool_column(col):
            vals = []
            for v in col:
                if v is None or v == '':
                    vals.append(None)
                elif isinstance(v, str):
                    vals.append(v.strip().lower() in ('1', 'true', 't', 'yes', 'y'))
                else:
                    f = _num(v)
                    vals.append(None if f != f else bool(f))
            finish(_record(name, loc, 'boolean', vals))
            continue
        if ctype == 'number' or _all_numeric(col):
            finish(_record(name, loc, 'number', [None if _num(v) != _num(v) else _num(v) for v in col]))
            continue
        finish(_record(name, loc, 'text', [None if v is None else str(v) for v in col]))
    return out


# ===================================================================== v0.9 IO
def read_omf1(src):
    """Read an OMF v0.9 file (path or bytes) -> Project."""
    data, label = _read_source(src)
    if len(data) < 60 or data[:4] != OMF1_MAGIC:
        raise OmfError('not an OMF v0.9 file (bad magic)')
    version = struct.unpack('<32s', data[4:36])[0]
    if version[:10] != OMF1_VERSION:
        raise OmfError('unsupported OMF version %r' % version.rstrip(b'\x00').decode('ascii', 'replace'))
    project_uid = str(uuid.UUID(bytes=data[36:52]))
    json_start = struct.unpack('<Q', data[52:60])[0]
    if json_start < 60 or json_start > len(data):
        raise OmfError('bad JSON offset %d' % json_start)
    try:
        registry = json.loads(data[json_start:].decode('utf-8'))
    except ValueError as e:
        raise OmfError('bad JSON registry: %s' % e)
    pj = registry.get(project_uid)
    if pj is None or pj.get('__class__') != 'Project':
        pj = None
        for v in registry.values():
            if isinstance(v, dict) and v.get('__class__') == 'Project':
                pj = v
                break
        if pj is None:
            raise OmfError('no Project object in registry')
    reader = _Omf1Reader(data, registry, label)
    project = Project(name=pj.get('name') or 'model')
    project.metadata.update({'omf_version': '0.9.0', 'author': pj.get('author', ''),
                             'description': pj.get('description', ''),
                             'revision': pj.get('revision', ''), 'units': pj.get('units', ''),
                             'date': pj.get('date') or pj.get('date_created', ''),
                             'warnings': reader.warnings})
    if pj.get('units'):
        project.crs['units'] = pj['units']
    porigin = [float(c) for c in (pj.get('origin') or [0.0, 0.0, 0.0])]
    for uid in pj.get('elements') or []:
        el = registry.get(uid)
        if el is None:
            reader.warn('element %s missing from registry' % uid)
            continue
        try:
            objs = reader.element(el, porigin)
        except OmfError as e:
            reader.warn('element %r: %s' % (el.get('name'), e))
            continue
        for obj in objs:
            obj.provenance = {'format': 'omf1', 'path': label, 'element': el.get('name', '')}
            project.add(obj)
    return project


class _Omf1Reader(object):
    def __init__(self, data, registry, label):
        self.data = data
        self.registry = registry
        self.label = label
        self.warnings = []

    def warn(self, msg):
        self.warnings.append(msg)

    # -- registry access
    def obj(self, uid, expect=None):
        if isinstance(uid, dict):
            return uid
        o = self.registry.get(uid)
        if o is None:
            raise OmfError('missing object %s' % uid)
        if expect and o.get('__class__') not in expect:
            raise OmfError('object %s is a %s, expected %s' % (uid, o.get('__class__'), '/'.join(expect)))
        return o

    def blob(self, index):
        start, length = int(index['start']), int(index['length'])
        dtype = index.get('dtype', '<f8')
        if start < 0 or start + length > len(self.data):
            raise OmfError('array blob out of range')
        raw = zlib.decompress(self.data[start:start + length])
        if dtype == '<f8':
            a = array.array('d')
        elif dtype == '<i8':
            a = array.array('q')
        elif dtype == 'image/png':
            return raw
        else:
            raise OmfError('unknown array dtype %r' % dtype)
        a.frombytes(raw[:len(raw) - len(raw) % a.itemsize])
        if sys.byteorder != 'little':
            a.byteswap()
        return a

    def array_obj(self, uid):
        """ScalarArray / Vector3Array / Int2Array ... -> flat array or list."""
        o = self.obj(uid)
        arr = o.get('array')
        if isinstance(arr, dict):
            return self.blob(arr)
        return arr if arr is not None else []

    # -- elements
    def element(self, el, porigin):
        cls = el.get('__class__')
        name = el.get('name') or ''
        color = el.get('color')
        kw = {}
        if color and len(color) >= 3:
            kw['color'] = [int(c) for c in color[:3]]
        if el.get('description'):
            kw['metadata'] = {'description': el['description']}
        geom = self.obj(el['geometry'])
        gcls = geom.get('__class__')
        gorigin = [float(c) for c in (geom.get('origin') or [0.0, 0.0, 0.0])]
        origin = [porigin[k] + gorigin[k] for k in range(3)]
        objs = []
        if cls == 'PointSetElement' and gcls == 'PointSetGeometry':
            xyz = self._shift(self.array_obj(geom['vertices']), origin)
            obj = PointSet(xyz, name=name, **kw)
            obj.metadata['omf_subtype'] = el.get('subtype', 'point')
            _apply_records(obj, self.records(el), self.warn)
            objs.append(obj)
        elif cls == 'LineSetElement' and gcls == 'LineSetGeometry':
            xyz = self._shift(self.array_obj(geom['vertices']), origin)
            segs = iarray(self.array_obj(geom['segments']))
            obj = LineSet(xyz, segs, name=name, **kw)
            obj.metadata['omf_subtype'] = el.get('subtype', 'line')
            _apply_records(obj, self.records(el), self.warn)
            objs.append(obj)
        elif cls == 'SurfaceElement' and gcls == 'SurfaceGeometry':
            xyz = self._shift(self.array_obj(geom['vertices']), origin)
            tris = iarray(self.array_obj(geom['triangles']))
            obj = Mesh(xyz, tris, name=name, **kw)
            _apply_records(obj, self.records(el), self.warn)
            objs.append(obj)
        elif cls == 'SurfaceElement' and gcls == 'SurfaceGridGeometry':
            heights = None
            if geom.get('offset_w'):
                heights = self.array_obj(geom['offset_w'])
            obj, is_grid, tri_cells = _grid_from_tensor_surface(
                name, origin, geom.get('axis_u') or [1, 0, 0], geom.get('axis_v') or [0, 1, 0],
                geom.get('tensor_u') or [], geom.get('tensor_v') or [], heights, self.warn, **kw)
            recs = self.records(el)
            if is_grid:
                props = _property_grids(obj, recs, self.warn)
                if heights is not None or not props:
                    objs.append(obj)      # a flat grid carrying data is just its property grids
                objs.extend(props)
            else:
                _apply_records(obj, _realign_face_records(recs, tri_cells), self.warn)
                objs.append(obj)
        elif cls == 'VolumeElement' and gcls == 'VolumeGridGeometry':
            bm, remap = _blockmodel_from_grid(
                name, origin, geom.get('axis_u') or [1, 0, 0], geom.get('axis_v') or [0, 1, 0],
                geom.get('axis_w') or [0, 0, 1], geom.get('tensor_u') or [], geom.get('tensor_v') or [],
                geom.get('tensor_w') or [], self.warn, **kw)
            if bm is not None:
                recs = self.records(el)
                for r in recs:
                    if r['location'] == LOC_CELLS:
                        r['values'] = _remap(list(r['values']), remap)
                _apply_records(bm, recs, self.warn)
                objs.append(bm)
        else:
            self.warn('element %r: unsupported class %s / %s; skipped' % (name, cls, gcls))
        if el.get('textures'):
            self.warn('element %r: %d image texture(s) skipped' % (name, len(el['textures'])))
        return objs

    def _shift(self, flat, origin):
        a = farray(flat)
        if any(origin):
            for k in range(0, len(a) - 2, 3):
                a[k] += origin[0]
                a[k + 1] += origin[1]
                a[k + 2] += origin[2]
        return a

    # -- data
    def records(self, el):
        out = []
        for uid in el.get('data') or []:
            try:
                d = self.obj(uid)
                rec = self.record(d)
            except OmfError as e:
                self.warn('%s: data %s unreadable: %s' % (el.get('name'), uid, e))
                continue
            if rec is not None:
                out.append(rec)
        return out

    def record(self, d):
        cls = d.get('__class__')
        name = d.get('name') or ''
        loc = d.get('location') or LOC_VERTICES
        desc = d.get('description') or ''
        marker = None
        for mk in (MARK_BOOLEAN, MARK_DATETIME):
            if desc.endswith(mk):
                marker = mk
                desc = desc[:-len(mk)].rstrip()
        if cls == 'ScalarData':
            vals = [None if v != v else float(v) for v in self.array_obj(d['array'])]
            if marker == MARK_BOOLEAN:
                return _record(name, loc, 'boolean', [None if v is None else bool(v) for v in vals], description=desc)
            rec = _record(name, loc, 'number', vals, description=desc)
            if d.get('colormap'):
                try:
                    cm = self.obj(d['colormap'])
                    grad = [_color4(c) for c in self.array_obj(cm['gradient'])]
                    rec['colormap'] = {'type': 'continuous', 'range': [float(x) for x in cm['limits']],
                                       'gradient': grad}
                except (OmfError, KeyError, TypeError, ValueError):
                    self.warn('%s: colormap unreadable' % name)
            return rec
        if cls == 'StringData':
            vals = [None if v is None else str(v) for v in self.array_obj(d['array'])]
            if marker == MARK_DATETIME:
                return _record(name, loc, 'datetime', [v or None for v in vals], description=desc)
            return _record(name, loc, 'text', vals, description=desc)
        if cls == 'DateTimeData':
            return _record(name, loc, 'datetime', list(self.array_obj(d['array'])), description=desc)
        if cls in ('Vector2Data', 'Vector3Data'):
            dim = 2 if cls == 'Vector2Data' else 3
            flat = list(self.array_obj(d['array']))
            vals = []
            for k in range(0, len(flat) - dim + 1, dim):
                row = flat[k:k + dim]
                vals.append(None if any(x != x for x in row) else [float(x) for x in row])
            return _record(name, loc, 'vector', vals, dim=dim, description=desc)
        if cls == 'ColorData':
            arr = self.obj(d['array'])
            raw = arr.get('array')
            if isinstance(raw, dict):
                flat = list(self.blob(raw))
                vals = [_color4(flat[k:k + 3]) for k in range(0, len(flat) - 2, 3)]
            else:
                vals = [_color4(c) for c in (raw or [])]
            return _record(name, loc, 'color', vals, description=desc)
        if cls == 'MappedData':
            idx = [None if (v < 0) else int(v) for v in self.array_obj(d['array'])]
            names = None
            colors = None
            sub = {}
            for luid in d.get('legends') or []:
                leg = self.obj(luid)
                vals_obj = self.obj(leg['values'])
                vcls = vals_obj.get('__class__')
                vals = vals_obj.get('array')
                if isinstance(vals, dict):
                    vals = list(self.blob(vals))
                if vcls == 'StringArray' and names is None:
                    names = [str(v) for v in vals]
                elif vcls == 'ColorArray' and colors is None:
                    colors = [_color4(c) for c in vals]
                else:
                    sub[leg.get('name') or vcls] = list(vals)
            if names is None:
                top = max([i for i in idx if i is not None] + [-1])
                names = ['%d' % i for i in range(top + 1)]
            return _record(name, loc, 'category', idx, names=names, colors=colors, sub=sub or None, description=desc)
        self.warn('data %r: unsupported class %s; skipped' % (name, cls))
        return None


def _realign_face_records(records, tri_cells):
    """Per-cell records of a grid that was triangulated -> per-triangle."""
    out = []
    for rec in records:
        if rec['location'] == LOC_FACES and tri_cells is not None:
            vals = rec['values']
            rec = dict(rec)
            rec['values'] = [vals[c] if c < len(vals) else None for c in tri_cells]
        out.append(rec)
    return out


def _property_grids(grid, records, warn):
    """Per-node / per-cell numeric attributes of a grid surface -> extra
    Grid2D objects with role 'property'."""
    out = []
    for rec in records:
        name = rec['name']
        rtype = rec['type']
        if rtype == 'vector':
            warn('%s/%s: vector attribute on a grid surface skipped' % (grid.name, name))
            continue
        if rtype in ('text', 'datetime', 'color'):
            warn('%s/%s: %s attribute on a grid surface skipped' % (grid.name, name, rtype))
            continue
        if rtype == 'category':
            vals = [NAN if (i is None or i < 0) else float(i) for i in rec['values']]
        elif rtype == 'boolean':
            vals = [NAN if v is None else (1.0 if v else 0.0) for v in rec['values']]
        else:
            vals = rec['values']
        gname = grid.name if name == grid.name else '%s/%s' % (grid.name, name)
        if rec['location'] == LOC_VERTICES:
            if len(vals) != grid.nx * grid.ny:
                warn('%s/%s: %d values for %d nodes; skipped' % (grid.name, name, len(vals), grid.nx * grid.ny))
                continue
            g = Grid2D(grid.nx, grid.ny, grid.x0, grid.y0, grid.dx, grid.dy, farray(vals),
                       rotation=grid.rotation, role='property', name=gname, color=grid.color)
        elif rec['location'] == LOC_FACES:
            nx, ny = grid.nx - 1, grid.ny - 1
            if len(vals) != nx * ny or nx < 1 or ny < 1:
                warn('%s/%s: %d values for %d cells; skipped' % (grid.name, name, len(vals), nx * ny))
                continue
            x0, y0 = grid.node_xy(0.5, 0.5)
            g = Grid2D(nx, ny, x0, y0, grid.dx, grid.dy, farray(vals), rotation=grid.rotation,
                       role='property', name=gname, color=grid.color)
            g.metadata['cell_centred'] = True
        else:
            warn('%s/%s: location %r not supported on a grid surface' % (grid.name, name, rec['location']))
            continue
        g.metadata['property_of'] = grid.id
        if rec.get('units'):
            g.units = rec['units']
        if rtype == 'category':
            g.metadata['categories'] = {name: {'names': rec.get('names'), 'colors': rec.get('colors'), 'index': True}}
        if rec.get('colormap'):
            g.metadata['colormaps'] = {name: rec['colormap']}
        out.append(g)
    return out


# --------------------------------------------------------------------- writer
class _Omf1Writer(object):
    def __init__(self, warn):
        self.buf = io.BytesIO()
        self.buf.write(b'\x00' * 60)
        self.reg = {}
        self.now = _now()
        self.warn = warn

    def add(self, cls, props):
        uid = str(uuid.uuid4())
        d = {'__class__': cls, 'date_created': self.now, 'date_modified': self.now}
        d.update(props)
        self.reg[uid] = d
        return uid

    def blob(self, typecode, values):
        a = values if (isinstance(values, array.array) and values.typecode == typecode) else array.array(typecode, values)
        if sys.byteorder != 'little':
            a = array.array(typecode, a)
            a.byteswap()
        start = self.buf.tell()
        self.buf.write(zlib.compress(a.tobytes()))
        return {'start': start, 'length': self.buf.tell() - start,
                'dtype': '<f8' if typecode == 'd' else '<i8'}

    def f8(self, cls, values):
        return self.add(cls, {'array': self.blob('d', farray(values) if not isinstance(values, array.array) else values)})

    def i8(self, cls, values):
        return self.add(cls, {'array': self.blob('q', [int(v) for v in values])})

    # -- data
    def data(self, records, allowed, label):
        uids = []
        for rec in records:
            loc = rec['location']
            if loc not in allowed:
                self.warn('%s/%s: location %r cannot be written to this element' % (label, rec['name'], loc))
                continue
            props = {'name': rec['name'], 'description': rec.get('description') or '', 'location': loc}
            rtype = rec['type']
            vals = rec['values']
            if rtype == 'number':
                props['array'] = self.f8('ScalarArray', [NAN if v is None else float(v) for v in vals])
                cm = rec.get('colormap')
                if cm and cm.get('type') == 'continuous' and cm.get('gradient'):
                    grad = _resample_gradient(cm['gradient'], 128)
                    gid = self.add('ColorArray', {'array': [g[:3] for g in grad]})
                    rng = cm.get('range') or [0.0, 1.0]
                    props['colormap'] = self.add('ScalarColormap', {'name': '', 'description': '', 'gradient': gid,
                                                                    'limits': [float(rng[0]), float(rng[1])]})
                uids.append(self.add('ScalarData', props))
            elif rtype == 'text':
                props['array'] = self.add('StringArray', {'array': ['' if v is None else str(v) for v in vals]})
                uids.append(self.add('StringData', props))
            elif rtype == 'datetime':
                if all(v is not None for v in vals):
                    props['array'] = self.add('DateTimeArray', {'array': [str(v) for v in vals]})
                    uids.append(self.add('DateTimeData', props))
                else:
                    # omf 1.x DateTimeArray cannot hold nulls: ISO text instead
                    props['array'] = self.add('StringArray', {'array': ['' if v is None else str(v) for v in vals]})
                    props['description'] = (props['description'] + ' ' + MARK_DATETIME).strip()
                    uids.append(self.add('StringData', props))
            elif rtype == 'boolean':
                # v0.9 has no boolean data: 1 / 0 / NaN scalar
                props['array'] = self.f8('ScalarArray', [NAN if v is None else (1.0 if v else 0.0) for v in vals])
                props['description'] = (props['description'] + ' ' + MARK_BOOLEAN).strip()
                uids.append(self.add('ScalarData', props))
            elif rtype == 'vector':
                dim = rec.get('dim') or 3
                flat = []
                for v in vals:
                    flat.extend([NAN] * dim if v is None else [float(c) for c in v][:dim])
                cls = 'Vector2Array' if dim == 2 else 'Vector3Array'
                props['array'] = self.f8(cls, flat)
                uids.append(self.add('Vector2Data' if dim == 2 else 'Vector3Data', props))
            elif rtype == 'color':
                flat = []
                for v in vals:
                    c = _color4(v) or [255, 255, 255, 255]
                    flat.extend(c[:3])
                props['array'] = self.i8('Int3Array', flat)
                uids.append(self.add('ColorData', props))
            elif rtype == 'category':
                props['array'] = self.i8('ScalarArray', [-1 if i is None else int(i) for i in vals])
                legends = [self.add('Legend', {'name': rec['name'], 'description': '',
                                               'values': self.add('StringArray', {'array': list(rec.get('names') or [])})})]
                if rec.get('colors'):
                    cols = [(_color4(c) or [128, 128, 128, 255])[:3] for c in rec['colors']]
                    legends.append(self.add('Legend', {'name': rec['name'] + ' colors', 'description': '',
                                                       'values': self.add('ColorArray', {'array': cols})}))
                for sname, svals in (rec.get('sub') or {}).items():
                    svals = list(svals)
                    if _all_numeric(svals):
                        vid = self.f8('ScalarArray', [_num(v) for v in svals])
                    else:
                        vid = self.add('StringArray', {'array': ['' if v is None else str(v) for v in svals]})
                    legends.append(self.add('Legend', {'name': sname, 'description': '', 'values': vid}))
                props['legends'] = legends
                uids.append(self.add('MappedData', props))
            else:
                self.warn('%s/%s: attribute type %r not written' % (label, rec['name'], rtype))
        return uids

    # -- elements
    def element(self, obj, name=None):
        name = name or obj.name or obj.id
        color = [int(c) for c in (obj.color or [160, 160, 160])[:3]]
        desc = str(obj.metadata.get('description', '')) if obj.metadata else ''
        recs = _collect_records(obj, self.warn)
        if obj.kind == 'points':
            geom = self.add('PointSetGeometry', {'origin': [0.0, 0.0, 0.0], 'vertices': self.f8('Vector3Array', obj.xyz)})
            return self.add('PointSetElement', {'name': name, 'description': desc, 'color': color,
                                                'subtype': 'point', 'geometry': geom, 'textures': [],
                                                'data': self.data(recs, (LOC_VERTICES,), name)})
        if obj.kind == 'lineset':
            geom = self.add('LineSetGeometry', {'origin': [0.0, 0.0, 0.0], 'vertices': self.f8('Vector3Array', obj.vertices),
                                                'segments': self.i8('Int2Array', obj.segments)})
            return self.add('LineSetElement', {'name': name, 'description': desc, 'color': color,
                                               'subtype': 'line', 'geometry': geom,
                                               'data': self.data(recs, (LOC_VERTICES, LOC_SEGMENTS), name)})
        if obj.kind == 'mesh':
            geom = self.add('SurfaceGeometry', {'origin': [0.0, 0.0, 0.0], 'vertices': self.f8('Vector3Array', obj.vertices),
                                                'triangles': self.i8('Int3Array', obj.triangles)})
            return self.add('SurfaceElement', {'name': name, 'description': desc, 'color': color,
                                               'subtype': 'surface', 'geometry': geom, 'textures': [],
                                               'data': self.data(recs, (LOC_VERTICES, LOC_FACES), name)})
        if obj.kind == 'grid2d':
            if obj.nx < 2 or obj.ny < 2:
                self.warn('%s: grids need at least 2 x 2 nodes for OMF; skipped' % name)
                return None
            au, av = _rotation_axes(obj.rotation)
            props = {'origin': [obj.x0, obj.y0, 0.0], 'tensor_u': [obj.dx] * (obj.nx - 1),
                     'tensor_v': [obj.dy] * (obj.ny - 1), 'axis_u': au, 'axis_v': av}
            data = []
            if obj.role == 'property':
                data = self.data([_record(name, LOC_VERTICES, 'number', list(obj.values), units=obj.units)], (LOC_VERTICES,), name)
            else:
                props['offset_w'] = self.f8('ScalarArray', obj.values)
            geom = self.add('SurfaceGridGeometry', props)
            return self.add('SurfaceElement', {'name': name, 'description': desc, 'color': color,
                                               'subtype': 'surface', 'geometry': geom, 'textures': [], 'data': data})
        if obj.kind == 'blockmodel':
            au, av, aw = _azimuth_axes(obj.azimuth)
            geom = self.add('VolumeGridGeometry', {
                'origin': list(obj.origin), 'tensor_u': [obj.block_size[0]] * obj.count[0],
                'tensor_v': [obj.block_size[1]] * obj.count[1], 'tensor_w': [obj.block_size[2]] * obj.count[2],
                'axis_u': au, 'axis_v': av, 'axis_w': aw})
            return self.add('VolumeElement', {'name': name, 'description': desc, 'color': color,
                                              'subtype': 'volume', 'geometry': geom,
                                              'data': self.data(recs, (LOC_CELLS,), name)})
        if obj.kind == 'drillholes':
            return self.element(obj.traces_lineset(), name + ' traces')
        self.warn('%s: object kind %r has no OMF v0.9 equivalent; skipped' % (name, obj.kind))
        return None


def _resample_gradient(grad, n):
    grad = [(_color4(c) or [0, 0, 0, 255]) for c in grad] or [[0, 0, 0, 255]]
    if len(grad) == n:
        return grad
    out = []
    for k in range(n):
        t = k / float(n - 1) if n > 1 else 0.0
        pos = t * (len(grad) - 1)
        i0 = int(math.floor(pos))
        i1 = min(i0 + 1, len(grad) - 1)
        f = pos - i0
        out.append([int(round(grad[i0][c] * (1 - f) + grad[i1][c] * f)) for c in range(4)])
    return out


def write_omf1(project_or_objects, dst, name='', description='', author='', revision='',
               units='m', warnings=None):
    """Write a Project or a list of model objects as OMF v0.9.  ``dst`` is a
    path or a binary file object; returns the path (or the bytes when a
    BytesIO was given).  Problems are appended to ``warnings`` (a list) and to
    ``project.metadata['warnings']`` when a Project was given."""
    project, objects = _objects_of(project_or_objects)
    warns = warnings if warnings is not None else []
    if project is not None:
        name = name or project.name
        description = description or str(project.metadata.get('description', ''))
        author = author or str(project.metadata.get('author', ''))
        units = project.crs.get('units', units) or units
    w = _Omf1Writer(warns.append)
    names = _unique_names([o.name for o in objects], 'element')
    elements = []
    for obj, nm in zip(objects, names):
        uid = w.element(obj, nm)
        if uid:
            elements.append(uid)
    puid = str(uuid.uuid4())
    w.reg[puid] = {'__class__': 'Project', 'date_created': w.now, 'date_modified': w.now,
                   'name': name or 'model', 'description': description or '',
                   'author': author or APPLICATION, 'revision': revision or '',
                   'units': units or '', 'origin': [0.0, 0.0, 0.0], 'elements': elements}
    json_start = w.buf.tell()
    w.buf.write(json.dumps(w.reg).encode('utf-8'))
    w.buf.seek(0)
    w.buf.write(OMF1_MAGIC)
    w.buf.write(struct.pack('<32s', OMF1_VERSION.ljust(32, b'\x00')))
    w.buf.write(uuid.UUID(puid).bytes)
    w.buf.write(struct.pack('<Q', json_start))
    _merge_warnings(project, warns)
    return _write_dest(w.buf.getvalue(), dst)


def _merge_warnings(project, warns):
    if project is None or not warns:
        return
    existing = project.metadata.setdefault('warnings', [])
    for msg in warns:
        if msg not in existing:
            existing.append(msg)


def convert_omf1_to_omf2(src, dst, **kw):
    """OMF v0.9 file -> OMF v2.0 file (see ``omf2.write_omf2`` for options)."""
    from .omf2 import write_omf2
    project = read_omf1(src)
    return write_omf2(project, dst, **kw)


def convert_omf2_to_omf1(src, dst, **kw):
    """OMF v2.0 file -> OMF v0.9 file."""
    from .omf2 import read_omf2
    project = read_omf2(src)
    return write_omf1(project, dst, **kw)
