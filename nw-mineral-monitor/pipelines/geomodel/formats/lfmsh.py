"""Leapfrog binary mesh (.msh) -- the community-documented layout, stdlib only.

There is no public specification.  The layout below is what the open-source
converters (e.g. pemn's leapfrog-to-OBJ script) write and what Leapfrog Geo
accepts::

    %%ARANZ-1.0\\n\\n[index]\\nTri Integer 3 N;\\nLocation Double 3 M;\\n\\n[binary]\\n
    <12 bytes: struct.pack('<3i', 15732735, 1115938331, 1072939210)>
    <N*3 int32 little-endian triangle vertex indices, 0-based>
    <M*3 float64 little-endian vertex coordinates>

``read_msh`` parses the ``[index]`` lines generically (``name dtype shape...;``
with the shape reversed, so ``3 N`` = N rows of 3), skips the 12 bytes after
``[binary]`` (self-correcting by a few bytes if the payload size does not
match) and reads the arrays in index order.  ``Tri`` / ``Location`` become
the Mesh; any other array whose row count equals the vertex or triangle
count becomes an attribute (encoding unknown -- flagged in the warnings).
"""
import array
import io
import os
import re
import struct
import sys

from ..model import Mesh, farray, iarray

FORMAT_ID = 'lf_msh'
MAGIC = b'%%ARANZ-1.0'
_BINARY_PREFIX = struct.pack('<3i', 15732735, 1115938331, 1072939210)
_SPEC_WARNING = ('Leapfrog .msh is a reverse-engineered format (no public specification): the '
                 'header/array layout follows the community converters; the meaning of the 12 '
                 'bytes after [binary] and the encoding of any extra arrays are unknown')

_DTYPES = {'integer': ('i', 4), 'int': ('i', 4), 'int32': ('i', 4),
           'double': ('d', 8), 'float64': ('d', 8),
           'float': ('f', 4), 'single': ('f', 4), 'float32': ('f', 4),
           'long': ('q', 8), 'int64': ('q', 8),
           'short': ('h', 2), 'int16': ('h', 2),
           'byte': ('B', 1), 'uint8': ('B', 1), 'char': ('b', 1)}


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


def _le(arr):
    if sys.byteorder != 'little':
        arr = array.array(arr.typecode, arr)
        arr.byteswap()
    return arr


# ------------------------------------------------------------------- reader
def parse_index(header_text):
    """[(name, typecode, itemsize, [dims...])] from the [index] section.
    Dims are in file order, e.g. ['3', 'N'] -> [3, N] meaning N rows of 3."""
    entries = []
    m = re.search(r'\[index\](.*?)(?:\[binary\]|$)', header_text, re.S | re.I)
    body = m.group(1) if m else header_text
    for stmt in body.split(';'):
        stmt = stmt.strip()
        if not stmt:
            continue
        toks = stmt.split()
        if len(toks) < 3:
            continue
        name, dtype = toks[0], toks[1].lower()
        tc, size = _DTYPES.get(dtype, (None, None))
        try:
            dims = [int(t) for t in toks[2:]]
        except ValueError:
            continue
        entries.append((name, tc, size, dims, dtype))
    return entries


def read_msh(src):
    """Read a Leapfrog binary mesh into a Mesh (see module docstring)."""
    path = _src_path(src)
    data = _load_bytes(src)
    if not data.startswith(MAGIC[:7]):
        raise ValueError('not a Leapfrog mesh (missing %%ARANZ header)')
    bpos = data.find(b'[binary]')
    if bpos < 0:
        raise ValueError('Leapfrog mesh: no [binary] marker')
    header_text = data[:bpos + 8].decode('ascii', 'replace')
    entries = parse_index(header_text)
    warnings = [_SPEC_WARNING]
    if not entries:
        raise ValueError('Leapfrog mesh: empty [index] section')
    for name, tc, size, dims, dtype in entries:
        if tc is None:
            raise ValueError('Leapfrog mesh: unknown array type %r for %r' % (dtype, name))

    # payload offset: optional newline(s) after [binary], then 12 bytes
    off = bpos + 8
    while off < len(data) and data[off:off + 1] in (b'\n', b'\r'):
        off += 1
    off += 12
    expected = 0
    for name, tc, size, dims, dtype in entries:
        n = 1
        for d in dims:
            n *= d
        expected += n * size
    avail = len(data) - off
    if avail != expected:
        fixed = None
        for delta in (-1, -2, 1, 2, -3, 3, -4, 4, -12, 12):
            o2 = off + delta
            if 0 <= o2 <= len(data) and len(data) - o2 == expected:
                fixed = o2
                break
        if fixed is None:
            if avail < expected:
                raise ValueError('Leapfrog mesh: payload has %d bytes, index needs %d' % (avail, expected))
            warnings.append('%d trailing byte(s) after the indexed arrays ignored' % (avail - expected))
        else:
            warnings.append('binary payload found %+d byte(s) from the expected offset' % (fixed - off))
            off = fixed

    arrays = {}
    order = []
    for name, tc, size, dims, dtype in entries:
        n = 1
        for d in dims:
            n *= d
        arr = array.array(tc)
        arr.frombytes(data[off:off + n * size])
        off += n * size
        arrays[name] = (_le(arr), dims)
        order.append(name)

    def find(prefixes):
        for nm in order:
            if nm.lower() in prefixes:
                return nm
        for nm in order:
            if any(nm.lower().startswith(p) for p in prefixes):
                return nm
        return None

    tri_name = find(('tri', 'triangles', 'faces', 'indices'))
    loc_name = find(('location', 'locations', 'vertices', 'points', 'nodes'))
    if loc_name is None:
        raise ValueError('Leapfrog mesh: no Location array in index (%s)' % ', '.join(order))
    loc, ldims = arrays[loc_name]
    verts = farray(loc) if loc.typecode != 'd' else loc
    if ldims and ldims[0] != 3:
        warnings.append('Location rows have %d components, expected 3' % ldims[0])
    if tri_name is None:
        tris = iarray()
        warnings.append('no Tri array in index: vertices only')
    else:
        tri, tdims = arrays[tri_name]
        nv = len(verts) // 3
        if len(tri) and (min(tri) < 0 or max(tri) >= nv):
            if min(tri) >= 1 and max(tri) <= nv:
                warnings.append('triangle indices look 1-based; shifted to 0-based')
                tri = array.array('i', (t - 1 for t in tri))
            else:
                raise ValueError('Leapfrog mesh: triangle index out of range (%d..%d of %d vertices)'
                                 % (min(tri), max(tri), nv))
        tris = iarray(tri)
    name = os.path.splitext(os.path.basename(path))[0] if path else 'leapfrog mesh'
    mesh = Mesh(verts, tris, name=name, provenance={'format': FORMAT_ID, 'path': path})
    nv, nt = mesh.n_vertices, mesh.n_triangles
    extra = []
    for nm in order:
        if nm in (tri_name, loc_name):
            continue
        arr, dims = arrays[nm]
        rows = dims[-1] if dims else len(arr)
        width = 1
        for d in dims[:-1]:
            width *= d
        extra.append('%s(%s)' % (nm, 'x'.join(str(d) for d in dims)))
        loc_kind = 'vertices' if rows == nv else ('faces' if rows == nt else None)
        if loc_kind is None:
            warnings.append('array %r (%d rows) matches neither vertices nor triangles; kept in metadata' % (nm, rows))
            mesh.metadata.setdefault('extra_arrays', {})[nm] = list(arr)[:1000]
            continue
        if width == 1:
            mesh.attributes[nm] = {'location': loc_kind, 'values': farray(arr)}
        else:
            for c in range(width):
                mesh.attributes['%s_%d' % (nm, c + 1)] = {
                    'location': loc_kind, 'values': farray(arr[c::width])}
        warnings.append('array %r mapped to a per-%s attribute; its meaning/encoding is unknown' % (nm, loc_kind))
    mesh.metadata['index'] = [{'name': e[0], 'dtype': e[4], 'dims': e[3]} for e in entries]
    if extra:
        mesh.metadata['extra_arrays_listed'] = extra
    mesh.metadata['warnings'] = warnings
    return mesh


# ------------------------------------------------------------------- writer
def write_msh(mesh, dst):
    """Write a Mesh as a Leapfrog binary .msh (Tri + Location arrays only).
    Returns the path written (bytes for a BytesIO)."""
    nt, nv = mesh.n_triangles, mesh.n_vertices
    header = ('%%ARANZ-1.0\n\n[index]\nTri Integer 3 {0};\nLocation Double 3 {1};\n\n[binary]\n'
              .format(nt, nv)).encode('ascii')
    tri = _le(array.array('i', (int(t) for t in mesh.triangles)))
    loc = _le(array.array('d', (float(v) for v in mesh.vertices)))
    data = header + _BINARY_PREFIX + tri.tobytes() + loc.tobytes()
    return _emit(dst, data)
