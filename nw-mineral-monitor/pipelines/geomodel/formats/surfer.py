"""Golden Software Surfer grids (.grd) and blanking files (.bln).

Three grid flavours share the .grd extension and are told apart by their
first four bytes:

* ``DSAA`` — Surfer 6 ASCII: five header lines (``DSAA``, ``nx ny``,
  ``xlo xhi``, ``ylo yhi``, ``zlo zhi``) then free-format values, one grid
  row (south first, west to east) per block of lines.
* ``DSBB`` — Surfer 6 binary: ``DSBB``, int16 nx, int16 ny, six doubles
  (xlo, xhi, ylo, yhi, zlo, zhi) and float32 values in the same order.
* ``DSRB`` — Surfer 7 binary: tagged sections (int32 id, int32 size):
  header ``DSRB`` (version), ``GRID`` (72 bytes: int32 nRow, int32 nCol,
  doubles xLL, yLL, xSize, ySize, zMin, zMax, Rotation (unused), BlankValue),
  ``DATA`` (nRow*nCol doubles, south row first) and an optional ``FLTI``
  fault section which is skipped.

All three are node-registered (xlo/ylo are node coordinates) — exactly the
``Grid2D`` convention — so no half-cell shifts are involved.  The blank
(no-data) value is 1.70141e38 (float32 0x7EFFFFEE); anything >= 1.7e38 is
treated as no-data on read (Surfer 7 version 1 files define "blank" as
>= BlankValue, version 2 as == BlankValue; the writer emits exactly
BlankValue so both readings agree).  zlo/zhi exclude blanks; an all-blank
grid stores the blank value for both.

BLN files are comma-separated polylines: a ``count, flag[, "name"]`` header
per polyline followed by ``count`` ``x, y[, z]`` lines; flag 1 = blank
inside, 0 = blank outside.  A polyline whose first and last vertex coincide
is a closed polygon.  They map onto ``LineSet`` parts with ``features``
``{'flag': int, 'name': str, 'closed': bool}``.

Only the standard library is used.
"""
import array
import os
import re
import struct

from ..model import Grid2D, LineSet, NAN

BLANK = 1.70141e38
BLANK_F32 = 1.701410009187828e+38          # float32 0x7EFFFFEE, what Surfer stores
BLANK_THRESHOLD = 1.7e38
BLANK_TEXT = '1.70141e+38'

_TAG_HEADER = 0x42525344    # 'DSRB'
_TAG_GRID = 0x44495247      # 'GRID'
_TAG_DATA = 0x41544144      # 'DATA'
_TAG_FAULT = 0x49544c46     # 'FLTI'


# ----------------------------------------------------------------- helpers
def _load(src):
    """(bytes, path-or-None) from a path, bytes or a file object."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src), None
    if hasattr(src, 'read'):
        data = src.read()
        if isinstance(data, str):
            data = data.encode('latin-1')
        return data, getattr(src, 'name', None)
    with open(src, 'rb') as fh:
        return fh.read(), str(src)


def _emit(dst, data):
    """Write bytes to a path or binary file object.  Returns the path, or
    the bytes when ``dst`` is an in-memory buffer (BytesIO)."""
    if hasattr(dst, 'write'):
        dst.write(data)
        if hasattr(dst, 'getvalue'):
            return dst.getvalue()
        return getattr(dst, 'name', None)
    with open(dst, 'wb') as fh:
        fh.write(data)
    return dst


def _fmt(v):
    """Shortest round-trip decimal text for a float."""
    return repr(float(v))


def _provenance(fmt, path):
    p = {'format': fmt}
    if path:
        p['path'] = path
    return p


def _zrange(values):
    zs = [v for v in values if v == v]
    if not zs:
        return None, None
    return min(zs), max(zs)


def _spacing(lo, hi, n, axis, warnings):
    if n <= 1:
        warnings.append('%s: only one node, spacing undefined; set to 1.0' % axis)
        return 1.0
    d = (hi - lo) / (n - 1)
    if d <= 0:
        warnings.append('%s: non-positive spacing %r (hi %r <= lo %r)' % (axis, d, hi, lo))
    return d


# --------------------------------------------------------------- read_grd
def read_grd(src):
    """Read a Surfer grid (DSAA / DSBB / DSRB auto-detected) -> Grid2D."""
    data, path = _load(src)
    magic = data[:4]
    if magic == b'DSAA':
        return _read_dsaa(data, path)
    if magic == b'DSBB':
        return _read_dsbb(data, path)
    if magic == b'DSRB':
        return _read_dsrb(data, path)
    raise ValueError('not a Surfer grid (expected DSAA/DSBB/DSRB, found %r)' % magic)


def _finish(grid, fmt, path, warnings, extra=None):
    grid.provenance = _provenance(fmt, path)
    grid.metadata['warnings'] = warnings
    if extra:
        grid.metadata.update(extra)
    if path:
        grid.name = grid.name or os.path.splitext(os.path.basename(path))[0]
    grid.role = 'surface'
    return grid


def _read_dsaa(data, path):
    warnings = []
    text = data.decode('latin-1')
    tokens = text.split()
    if len(tokens) < 9:
        raise ValueError('DSAA header truncated')
    try:
        nx, ny = int(tokens[1]), int(tokens[2])
        xlo, xhi, ylo, yhi, zlo, zhi = [float(t) for t in tokens[3:9]]
    except ValueError as e:
        raise ValueError('DSAA header not numeric: %s' % e)
    if nx <= 0 or ny <= 0:
        raise ValueError('DSAA grid size %dx%d invalid' % (nx, ny))
    vals = tokens[9:]
    n = nx * ny
    if len(vals) < n:
        raise ValueError('DSAA: expected %d values, found %d' % (n, len(vals)))
    if len(vals) > n:
        warnings.append('DSAA: %d trailing tokens ignored' % (len(vals) - n))
    values = array.array('d')
    blanks = 0
    for t in vals[:n]:
        try:
            v = float(t)
        except ValueError:
            raise ValueError('DSAA: non-numeric value %r' % t)
        if v >= BLANK_THRESHOLD:
            values.append(NAN)
            blanks += 1
        else:
            values.append(v)
    dx = _spacing(xlo, xhi, nx, 'x', warnings)
    dy = _spacing(ylo, yhi, ny, 'y', warnings)
    g = Grid2D(nx, ny, xlo, ylo, dx, dy, values)
    return _finish(g, 'surfer_grd', path, warnings,
                   {'surfer_variant': 'DSAA', 'zlo': zlo, 'zhi': zhi, 'blank_nodes': blanks})


def _read_dsbb(data, path):
    warnings = []
    if len(data) < 56:
        raise ValueError('DSBB header truncated')
    nx, ny = struct.unpack('<hh', data[4:8])
    xlo, xhi, ylo, yhi, zlo, zhi = struct.unpack('<6d', data[8:56])
    if nx <= 0 or ny <= 0:
        raise ValueError('DSBB grid size %dx%d invalid' % (nx, ny))
    n = nx * ny
    need = 56 + 4 * n
    if len(data) < need:
        raise ValueError('DSBB: expected %d data bytes, found %d' % (4 * n, len(data) - 56))
    if len(data) > need:
        warnings.append('DSBB: %d trailing bytes ignored' % (len(data) - need))
    raw = array.array('f')
    raw.frombytes(data[56:need])
    if struct.pack('<f', 1.0) != struct.pack('=f', 1.0):
        raw.byteswap()
    values = array.array('d')
    blanks = 0
    for v in raw:
        if v >= BLANK_THRESHOLD or v != v:
            values.append(NAN)
            blanks += 1
        else:
            values.append(v)
    dx = _spacing(xlo, xhi, nx, 'x', warnings)
    dy = _spacing(ylo, yhi, ny, 'y', warnings)
    g = Grid2D(nx, ny, xlo, ylo, dx, dy, values)
    return _finish(g, 'surfer_grd', path, warnings,
                   {'surfer_variant': 'DSBB', 'zlo': zlo, 'zhi': zhi, 'blank_nodes': blanks})


def _read_dsrb(data, path):
    warnings = []
    pos = 0
    n = len(data)
    version = None
    header = None
    values = None
    faults = 0
    while pos + 8 <= n:
        tag, size = struct.unpack('<iI', data[pos:pos + 8])
        pos += 8
        if pos + size > n:
            raise ValueError('DSRB: section 0x%08x (size %d) runs past end of file' % (tag, size))
        body = data[pos:pos + size]
        if tag == _TAG_HEADER:
            if size >= 4:
                version = struct.unpack('<i', body[:4])[0]
            if version not in (1, 2):
                warnings.append('DSRB: unexpected version %r (expected 1 or 2)' % version)
        elif tag == _TAG_GRID:
            if size < 72:
                raise ValueError('DSRB: GRID section is %d bytes, expected 72' % size)
            header = struct.unpack('<ii8d', body[:72])
        elif tag == _TAG_DATA:
            if header is None:
                raise ValueError('DSRB: DATA section before GRID section')
            nrow, ncol = header[0], header[1]
            count = nrow * ncol
            if size < 8 * count:
                raise ValueError('DSRB: DATA section has %d bytes, expected %d' % (size, 8 * count))
            values = array.array('d')
            values.frombytes(body[:8 * count])
            if struct.pack('<d', 1.0) != struct.pack('=d', 1.0):
                values.byteswap()
        elif tag == _TAG_FAULT:
            faults += 1
        else:
            warnings.append('DSRB: unknown section 0x%08x (%d bytes) skipped' % (tag, size))
        pos += size
        if values is not None and pos >= n:
            break
    if header is None or values is None:
        raise ValueError('DSRB: missing GRID or DATA section')
    nrow, ncol, xll, yll, xsize, ysize, zmin, zmax, rot, blank = header
    if nrow <= 0 or ncol <= 0:
        raise ValueError('DSRB grid size %dx%d invalid' % (ncol, nrow))
    if rot:
        warnings.append('DSRB: rotation field %r ignored (unused by Surfer)' % rot)
    if faults:
        warnings.append('DSRB: %d fault section(s) skipped' % faults)
    if xsize <= 0 or ysize <= 0:
        warnings.append('DSRB: non-positive node spacing (%r, %r)' % (xsize, ysize))
    out = array.array('d')
    blanks = 0
    for v in values:
        if v != v or (v >= blank if version != 2 else v == blank) or v >= BLANK_THRESHOLD:
            out.append(NAN)
            blanks += 1
        else:
            out.append(v)
    g = Grid2D(ncol, nrow, xll, yll, xsize, ysize, out)
    return _finish(g, 'surfer_grd', path, warnings,
                   {'surfer_variant': 'DSRB', 'surfer_version': version, 'zlo': zmin,
                    'zhi': zmax, 'blank_value': blank, 'blank_nodes': blanks})


# -------------------------------------------------------------- write_grd
def write_grd(grid, dst, fmt='dsaa'):
    """Write a Grid2D as a Surfer grid.  ``fmt`` = 'dsaa' (ASCII), 'dsbb'
    (Surfer 6 binary, float32, nx/ny <= 32767) or 'dsrb' (Surfer 7 binary,
    doubles).  Surfer grids cannot carry a rotation."""
    fmt = (fmt or 'dsaa').lower()
    if grid.rotation:
        raise ValueError('Surfer grids cannot be rotated (grid.rotation=%r); '
                         'resample to an axis-aligned grid first' % grid.rotation)
    if grid.dx <= 0 or grid.dy <= 0:
        raise ValueError('grid spacing must be positive (dx=%r, dy=%r)' % (grid.dx, grid.dy))
    zlo, zhi = _zrange(grid.values)
    if zlo is None:
        zlo = zhi = BLANK
    if fmt == 'dsaa':
        return _emit(dst, _dsaa_bytes(grid, zlo, zhi))
    if fmt == 'dsbb':
        return _emit(dst, _dsbb_bytes(grid, zlo, zhi))
    if fmt == 'dsrb':
        return _emit(dst, _dsrb_bytes(grid, zlo, zhi))
    raise ValueError("fmt must be 'dsaa', 'dsbb' or 'dsrb', not %r" % fmt)


def _dsaa_bytes(grid, zlo, zhi):
    nx, ny = grid.nx, grid.ny
    lines = ['DSAA', '%d %d' % (nx, ny),
             '%s %s' % (_fmt(grid.x0), _fmt(grid.xmax)),
             '%s %s' % (_fmt(grid.y0), _fmt(grid.ymax)),
             '%s %s' % (_fmt(zlo), _fmt(zhi))]
    vals = grid.values
    for j in range(ny):
        row = vals[j * nx:(j + 1) * nx]
        txt = [BLANK_TEXT if v != v else _fmt(v) for v in row]
        for k in range(0, nx, 10):
            lines.append(' '.join(txt[k:k + 10]))
        lines.append('')
    return ('\n'.join(lines) + '\n').encode('ascii')


def _dsbb_bytes(grid, zlo, zhi):
    nx, ny = grid.nx, grid.ny
    if nx > 32767 or ny > 32767:
        raise ValueError('DSBB stores nx/ny as int16; %dx%d is too large — use fmt="dsrb"' % (nx, ny))
    head = b'DSBB' + struct.pack('<hh6d', nx, ny, grid.x0, grid.xmax, grid.y0, grid.ymax, zlo, zhi)
    vals = array.array('f', (BLANK_F32 if v != v else v for v in grid.values))
    if struct.pack('<f', 1.0) != struct.pack('=f', 1.0):
        vals.byteswap()
    return head + vals.tobytes()


def _dsrb_bytes(grid, zlo, zhi):
    nx, ny = grid.nx, grid.ny
    out = [struct.pack('<iIi', _TAG_HEADER, 4, 1),
           struct.pack('<iI', _TAG_GRID, 72),
           struct.pack('<ii8d', ny, nx, grid.x0, grid.y0, grid.dx, grid.dy,
                       zlo, zhi, 0.0, BLANK_F32),
           struct.pack('<iI', _TAG_DATA, 8 * nx * ny)]
    vals = array.array('d', (BLANK_F32 if v != v else v for v in grid.values))
    if struct.pack('<d', 1.0) != struct.pack('=d', 1.0):
        vals.byteswap()
    out.append(vals.tobytes())
    return b''.join(out)


# -------------------------------------------------------------------- BLN
def _split_bln(line):
    """Tokens of a BLN line: comma/space separated, double-quoted names kept."""
    out = []
    for m in re.finditer(r'"([^"]*)"|([^,\s"]+)', line):
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return out


def read_bln(src):
    """Read a Surfer blanking / breakline file -> LineSet (one part per
    polyline; ``features[k]`` = {'flag': int, 'name': str, 'closed': bool})."""
    data, path = _load(src)
    warnings = []
    text = data.decode('latin-1')
    lines = [ln.strip() for ln in text.splitlines()]
    ls = LineSet(role='lines')
    k = 0
    nlines = len(lines)
    while k < nlines:
        if not lines[k]:
            k += 1
            continue
        head = _split_bln(lines[k])
        k += 1
        try:
            count = int(float(head[0]))
        except (ValueError, IndexError):
            raise ValueError('BLN: bad polyline header %r at line %d' % (lines[k - 1], k))
        flag = 0
        name = ''
        if len(head) > 1:
            try:
                flag = int(float(head[1]))
            except ValueError:
                name = head[1]
        if len(head) > 2:
            name = head[2]
        pts = []
        while len(pts) < count and k < nlines:
            if not lines[k]:
                k += 1
                continue
            tok = _split_bln(lines[k])
            k += 1
            try:
                x, y = float(tok[0]), float(tok[1])
                z = float(tok[2]) if len(tok) > 2 else 0.0
            except (ValueError, IndexError):
                raise ValueError('BLN: bad vertex %r at line %d' % (lines[k - 1], k))
            pts.append((x, y, z))
        if len(pts) < count:
            warnings.append('BLN: polyline %r declares %d vertices, found %d' % (name, count, len(pts)))
        if not pts:
            continue
        closed = len(pts) > 2 and pts[0][0] == pts[-1][0] and pts[0][1] == pts[-1][1]
        ls.add_polyline(pts, {'flag': flag, 'name': name, 'closed': closed})
    ls.provenance = _provenance('surfer_bln', path)
    ls.metadata['warnings'] = warnings
    if path:
        ls.name = os.path.splitext(os.path.basename(path))[0]
    return ls


def write_bln(lineset, dst):
    """Write a LineSet's parts as a Surfer BLN file.  ``features`` may carry
    'flag' (default 1 = blank inside) and 'name'; Z is written as a third
    column when any vertex has a non-zero Z (breaklines need it)."""
    parts = lineset.parts or []
    with_z = any(v == v and v != 0.0 for v in lineset.vertices[2::3])
    lines = []
    for k, part in enumerate(parts):
        feat = lineset.features[k] if k < len(lineset.features) else {}
        flag = feat.get('flag', 1)
        try:
            flag = int(flag)
        except (TypeError, ValueError):
            flag = 1
        name = feat.get('name') or ''
        head = '%d,%d' % (len(part), flag)
        if name:
            head += ',"%s"' % str(name).replace('"', "'")
        lines.append(head)
        for idx in part:
            x, y, z = lineset.vertex(idx)
            if with_z:
                lines.append('%s,%s,%s' % (_fmt(x), _fmt(y), _fmt(0.0 if z != z else z)))
            else:
                lines.append('%s,%s' % (_fmt(x), _fmt(y)))
    return _emit(dst, ('\n'.join(lines) + '\n').encode('ascii'))


__all__ = ['read_grd', 'write_grd', 'read_bln', 'write_bln', 'BLANK']
