"""Geosoft Oasis montaj interchange: binary grids (.grd), Grid eXchange
Files (.gxf) and XYZ line-database exports (.xyz).

Geosoft binary grid (version 2)
-------------------------------
512-byte header, little-endian::

    off  type    name   meaning
      0  int32   ES     element size 1/2/4/8 bytes; +1024 = compressed
      4  int32   SF     0 unsigned int, 1 signed int, 2 float, 3 colour
      8  int32   NE     elements per vector
     12  int32   NV     number of vectors
     16  int32   KX     1 vectors run along X (first vector = south row)
                        -1 vectors run along Y (first vector = west column)
     20  double  DE     element separation      28 double DV vector separation
     36  double  X0     origin (first point)    44 double Y0
     52  double  ROT    rotation, degrees CCW about (X0, Y0)
     60  double  ZBASE  68 double ZMULT        Z = stored / ZMULT + ZBASE
     76  char48  LABEL  124 char16 MAPNO
    140  int32   PROJ, UNITX, UNITY, UNITZ, NVPTS
    160  float   IZMIN, IZMAX, IZMED, IZMEA   176 double ZVAR   184 int32 PRCS

Dummies: int8 -127, uint8 255, int16 -32767, uint16 65535, int32
-2147483647, uint32 4294967295, float/double <= -1e32.  Compressed grids
(ES > 1024) follow the header with int32 signature @512, int32 compression
type @516, int32 n_blocks @520, int32 vectors_per_block @524, int64 absolute
block offsets and int32 compressed block sizes; each block is 16 bytes of
unexplained header then a zlib stream — the inflated blocks concatenate into
the plain element stream (the layout harmonica / Loop3D use).

Only the grid itself is read or written; the ``.grd.gi`` / ``.grd.xml``
sidecars Oasis writes (projection, colour table) are not — the writer says
so in ``grid.metadata['warnings']``.

GXF
---
ASCII ``#LABEL`` objects followed by data lines; ``#GRID`` is last.  Rows are
stored in the order given by ``#SENSE`` (+-1 bottom-left, +-2 upper-left,
+-3 upper-right, +-4 bottom-right; positive = right-handed, i.e. standing
at the first point looking into the grid the first row runs to your right;
default 1 = bottom-left, rows left to right).  ``#XORIGIN``/``#YORIGIN`` are
always the bottom-left corner of the grid regardless of sense (GXF spec
section 6; GDAL treats them as the first stored point instead).  Base-90
compression (``#GTYPE`` > 0): each value is GTYPE ASCII digits 37..126 MSB
first, ``!``*GTYPE is a dummy and ``"``*GTYPE + count + value repeats a value.
Values are ``I90 * scale + offset`` with ``#TRANSFORM scale, offset``.
When ``#DUMMY`` is absent -1e12 is used (GDAL's default).

Geosoft XYZ
-----------
``/`` comment lines (the last one before the data that names the channels
is the header, e.g. ``/ X Y Z MAG`` or ``/X,Y,Z,MAG``), ``Line 10`` /
``Tie 5`` / ``Base`` / ``Flight`` group headers, ``//Flight n`` and
``//Date`` annotations, and ``*`` for dummies.  Points get the channels as
attributes plus ``line`` (text) and ``line_type``.

Only the standard library is used.
"""
import array
import math
import os
import re
import struct
import zlib

from ..model import Grid2D, PointSet, NAN

GEOSOFT_DUMMY = -1e32
GXF_DEFAULT_DUMMY = -1e12
HEADER_SIZE = 512

_INT_DUMMY = {'b': -127, 'B': 255, 'h': -32767, 'H': 65535,
              'i': -2147483647, 'I': 4294967295}


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
    return repr(float(v))


def _provenance(fmt, path):
    p = {'format': fmt}
    if path:
        p['path'] = path
    return p


def _little():
    return struct.pack('=d', 1.0) == struct.pack('<d', 1.0)


def _stats(values):
    """(n_valid, zmin, zmax, median, mean, variance) ignoring NaN."""
    zs = sorted(v for v in values if v == v)
    n = len(zs)
    if not n:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0
    mean = math.fsum(zs) / n
    var = math.fsum((z - mean) ** 2 for z in zs) / n
    med = zs[n // 2] if n % 2 else 0.5 * (zs[n // 2 - 1] + zs[n // 2])
    return n, zs[0], zs[-1], med, mean, var


# ------------------------------------------------------------ Geosoft GRD
def _typecode(es, sf):
    if es == 1:
        return {0: 'B', 1: 'b'}.get(sf)
    if es == 2:
        return {0: 'H', 1: 'h'}.get(sf)
    if es == 4:
        return {0: 'I', 1: 'i', 2: 'f'}.get(sf)
    if es == 8:
        return 'd'
    return None


def _inflate(data, warnings):
    """Concatenate the zlib blocks of a compressed Geosoft grid."""
    if len(data) < HEADER_SIZE + 16:
        raise ValueError('compressed Geosoft grid: block table truncated')
    signature, comp_type, n_blocks, vectors_per_block = struct.unpack('<iiii', data[512:528])
    if n_blocks <= 0:
        raise ValueError('compressed Geosoft grid: %d blocks' % n_blocks)
    off = 528
    offsets = struct.unpack('<%dq' % n_blocks, data[off:off + 8 * n_blocks])
    off += 8 * n_blocks
    sizes = struct.unpack('<%di' % n_blocks, data[off:off + 4 * n_blocks])
    out = []
    for k in range(n_blocks):
        start, size = offsets[k], sizes[k]
        if start < 0 or start + size > len(data) or size < 16:
            raise ValueError('compressed Geosoft grid: block %d (offset %d, size %d) '
                             'outside the file' % (k, start, size))
        try:
            out.append(zlib.decompress(data[start + 16:start + size]))
        except zlib.error:
            # tolerate a size that excludes the 16-byte block header
            try:
                out.append(zlib.decompressobj().decompress(data[start + 16:]))
                warnings.append('compressed block %d: size field unreliable, stream read to a '
                                'natural end' % k)
            except zlib.error as e:
                raise ValueError('compressed Geosoft grid: block %d does not inflate (%s)' % (k, e))
    return b''.join(out), {'signature': signature, 'compression_type': comp_type,
                           'n_blocks': n_blocks, 'vectors_per_block': vectors_per_block}


def read_grd(src):
    """Read a Geosoft binary grid (version 2, KX = +-1, uncompressed or
    compressed) -> Grid2D with (x0, y0) at the south-west node."""
    data, path = _load(src)
    if len(data) < HEADER_SIZE:
        raise ValueError('Geosoft grid: file shorter than the 512-byte header')
    warnings = []
    es, sf, ne, nv, kx = struct.unpack('<5i', data[0:20])
    de, dv, x0, y0, rot, zbase, zmult = struct.unpack('<7d', data[20:76])
    label = data[76:124].split(b'\0')[0].decode('latin-1', 'replace').strip()
    mapno = data[124:140].split(b'\0')[0].decode('latin-1', 'replace').strip()
    proj, unitx, unity, unitz, nvpts = struct.unpack('<5i', data[140:160])
    izmin, izmax, izmed, izmea = struct.unpack('<4f', data[160:176])
    zvar, = struct.unpack('<d', data[176:184])
    prcs, = struct.unpack('<i', data[184:188])

    compressed = es > 1024
    es_plain = es - 1024 if compressed else es
    tc = _typecode(es_plain, sf)
    if tc is None:
        if sf == 3:
            raise ValueError('Geosoft colour grids (SF=3) are not supported')
        raise ValueError('Geosoft grid: unsupported element size / sign flag ES=%d SF=%d' % (es, sf))
    if kx not in (1, -1):
        raise ValueError('Geosoft grid: KX=%d not supported (only 1 and -1)' % kx)
    if ne <= 0 or nv <= 0:
        raise ValueError('Geosoft grid: NE=%d NV=%d invalid' % (ne, nv))
    if zmult == 0:
        warnings.append('ZMULT is 0; treated as 1')
        zmult = 1.0

    extra = {}
    if compressed:
        body, info = _inflate(data, warnings)
        extra['compression'] = info
    else:
        body = data[HEADER_SIZE:]
    count = ne * nv
    need = count * es_plain
    if len(body) < need:
        raise ValueError('Geosoft grid: expected %d data bytes, found %d' % (need, len(body)))
    if len(body) > need:
        warnings.append('%d trailing bytes after the grid data ignored' % (len(body) - need))
    raw = array.array(tc)
    raw.frombytes(body[:need])
    if not _little():
        raw.byteswap()

    # decode dummies and scaling into a flat list in file order
    flat = array.array('d', [NAN]) * count
    dummies = 0
    if tc in ('f', 'd'):
        for k, v in enumerate(raw):
            if v <= GEOSOFT_DUMMY or v != v:
                dummies += 1
            else:
                flat[k] = v / zmult + zbase
    else:
        dummy = _INT_DUMMY[tc]
        for k, v in enumerate(raw):
            if v == dummy:
                dummies += 1
            else:
                flat[k] = v / zmult + zbase

    if kx == 1:
        nx, ny, dx, dy = ne, nv, de, dv
        values = flat
    else:
        nx, ny, dx, dy = nv, ne, dv, de
        values = array.array('d', [NAN]) * count
        for i in range(nx):
            col = flat[i * ne:(i + 1) * ne]
            for j in range(ny):
                values[j * nx + i] = col[j]
        if rot:
            warnings.append('KX=-1 with rotation %r: rotation applied CCW about (X0, Y0) '
                            'as for KX=1 (unverified for Y-oriented grids)' % rot)
    if dx <= 0 or dy <= 0:
        warnings.append('non-positive spacing DE=%r DV=%r' % (de, dv))

    g = Grid2D(nx, ny, x0, y0, dx, dy, values, rotation=rot, name=label)
    if not g.name and path:
        g.name = os.path.splitext(os.path.basename(path))[0]
    g.provenance = _provenance('geosoft_grd', path)
    g.metadata['warnings'] = warnings
    g.metadata.update({
        'geosoft': {'ES': es, 'SF': sf, 'NE': ne, 'NV': nv, 'KX': kx, 'DE': de, 'DV': dv,
                    'X0': x0, 'Y0': y0, 'ROT': rot, 'ZBASE': zbase, 'ZMULT': zmult,
                    'LABEL': label, 'MAPNO': mapno, 'PROJ': proj, 'UNITX': unitx,
                    'UNITY': unity, 'UNITZ': unitz, 'NVPTS': nvpts, 'IZMIN': izmin,
                    'IZMAX': izmax, 'IZMED': izmed, 'IZMEA': izmea, 'ZVAR': zvar,
                    'PRCS': prcs, 'compressed': compressed},
        'dummy_nodes': dummies})
    g.metadata.update(extra)
    g.role = 'surface'
    return g


def write_grd(grid, dst, dtype='float'):
    """Write a Grid2D as an uncompressed Geosoft grid.  ``dtype`` 'float'
    -> ES=4 SF=2 float32; 'short' -> ES=2 SF=1 int16 with ZBASE/ZMULT
    chosen to span the data range.  KX=1 (rows along X, south row first),
    ROT from ``grid.rotation``, LABEL from ``grid.name``.  No ``.gi``
    sidecar is written (a warning is appended to grid.metadata)."""
    dtype = (dtype or 'float').lower()
    if dtype not in ('float', 'short'):
        raise ValueError("dtype must be 'float' or 'short', not %r" % dtype)
    n_valid, zmin, zmax, zmed, zmean, zvar = _stats(grid.values)
    if dtype == 'float':
        es, sf, zbase, zmult = 4, 2, 0.0, 1.0
        raw = array.array('f', (GEOSOFT_DUMMY if v != v else v for v in grid.values))
    else:
        es, sf = 2, 1
        span = zmax - zmin
        zbase = zmin + span / 2.0 if n_valid else 0.0
        zmult = (2 * 32765.0) / span if span > 0 else 1.0
        raw = array.array('h')
        for v in grid.values:
            if v != v:
                raw.append(-32767)
            else:
                s = int(round((v - zbase) * zmult))
                raw.append(max(-32766, min(32767, s)))
    if not _little():
        raw.byteswap()
    label = (grid.name or '').encode('latin-1', 'replace')[:48]
    head = struct.pack('<5i', es, sf, grid.nx, grid.ny, 1)
    head += struct.pack('<7d', grid.dx, grid.dy, grid.x0, grid.y0, grid.rotation, zbase, zmult)
    head += label.ljust(48, b'\0') + b'\0' * 16
    head += struct.pack('<5i', 0, 0, 0, 0, n_valid)
    head += struct.pack('<4f', zmin, zmax, zmed, zmean)
    head += struct.pack('<d', zvar)
    head += struct.pack('<i', 0)
    head = head.ljust(HEADER_SIZE, b'\0')
    warn = grid.metadata.setdefault('warnings', [])
    msg = 'geosoft write_grd: no .gi sidecar written (projection / colour table absent)'
    if msg not in warn:
        warn.append(msg)
    return _emit(dst, head + raw.tobytes())


# -------------------------------------------------------------------- GXF
_LABEL_RE = re.compile(r'^#\s*([A-Za-z_]+)\s*(.*)$')

# (horizontal rows, flip x, flip y) per SENSE: how stored (row r, point p)
# maps to grid (i, j).  horizontal: rows run along X.  flip_*: the stored
# direction is opposite to +X / +Y.
_SENSE = {
    1: (True, False, False),    # bottom-left, rows run right, rows stack up
    -1: (False, False, False),  # bottom-left, rows run up, rows stack right
    2: (False, False, True),    # upper-left, rows run down, rows stack right
    -2: (True, False, True),    # upper-left, rows run right, rows stack down
    3: (True, True, True),      # upper-right, rows run left, rows stack down
    -3: (False, True, True),    # upper-right, rows run down, rows stack left
    4: (False, True, False),    # bottom-right, rows run up, rows stack left
    -4: (True, True, False),    # bottom-right, rows run left, rows stack up
}


def _gxf_header(lines):
    """Parse label objects up to #GRID.  Returns (dict label -> [data lines],
    index of first grid data line)."""
    hdr = {}
    k = 0
    n = len(lines)
    grid_at = None
    while k < n:
        m = _LABEL_RE.match(lines[k])
        if not m:
            k += 1
            continue
        label = m.group(1).upper()
        rest = m.group(2).strip()
        k += 1
        if label.startswith('GRID'):
            grid_at = k
            break
        vals = []
        if rest:
            vals.append(rest)
        cont = False
        while k < n and not lines[k].startswith('#'):
            s = lines[k].rstrip()
            k += 1
            if cont:
                vals[-1] = vals[-1][:-1].rstrip() + s.strip()
            elif s.strip():
                vals.append(s.strip())
            else:
                continue
            cont = vals[-1].endswith('\\')
        if label not in hdr:
            hdr[label] = vals
    return hdr, grid_at


def _gxf_value(hdr, *names):
    for nm in names:
        for k, v in hdr.items():
            if k.startswith(nm) and v:
                return v[0]
    return None


def _gxf_fields(text):
    """Split a GXF data line into fields (space or comma separated; quoted
    strings kept whole)."""
    out = []
    for m in re.finditer(r'"([^"]*)"|([^,\s"]+)', text or ''):
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return out


def _b90(chunk):
    v = 0
    for ch in chunk:
        d = ord(ch) - 37
        if d < 0 or d > 89:
            raise ValueError('GXF: %r is not a base-90 digit' % ch)
        v = v * 90 + d
    return v


def _gxf_values_compressed(lines, gtype, count, scale, offset, warnings):
    stream = ''.join(ln.rstrip('\r\n').strip() for ln in lines if not ln.startswith('$'))
    out = array.array('d')
    pos = 0
    total = len(stream)
    dummies = 0
    while len(out) < count:
        if pos + gtype > total:
            raise ValueError('GXF: compressed data ends after %d of %d values' % (len(out), count))
        chunk = stream[pos:pos + gtype]
        pos += gtype
        if chunk[0] == '!':
            out.append(NAN)
            dummies += 1
        elif chunk[0] == '"':
            if pos + 2 * gtype > total:
                raise ValueError('GXF: truncated repeat code at offset %d' % pos)
            rep = _b90(stream[pos:pos + gtype])
            pos += gtype
            vchunk = stream[pos:pos + gtype]
            pos += gtype
            if vchunk[0] == '!':
                val = NAN
                dummies += rep
            else:
                val = _b90(vchunk) * scale + offset
            if len(out) + rep > count:
                warnings.append('GXF: repeat run of %d overflows the grid; clipped' % rep)
                rep = count - len(out)
            out.extend([val] * rep)
        else:
            out.append(_b90(chunk) * scale + offset)
    if pos < total:
        warnings.append('GXF: %d trailing compressed characters ignored' % (total - pos))
    return out, dummies


def _gxf_values_plain(lines, count, dummy_text, dummy_value, scale, offset, warnings):
    out = array.array('d')
    dummies = 0
    tol = 1e-9 * max(1.0, abs(dummy_value))
    apply = (scale != 1.0 or offset != 0.0)
    for ln in lines:
        if len(out) >= count:
            break
        for tok in ln.split():
            if len(out) >= count:
                break
            if tok == dummy_text:
                out.append(NAN)
                dummies += 1
                continue
            try:
                v = float(tok)
            except ValueError:
                raise ValueError('GXF: non-numeric grid value %r' % tok)
            if abs(v - dummy_value) <= tol:
                out.append(NAN)
                dummies += 1
            elif apply:
                out.append(v * scale + offset)
            else:
                out.append(v)
    if len(out) < count:
        raise ValueError('GXF: expected %d grid values, found %d' % (count, len(out)))
    return out, dummies


def read_gxf(src):
    """Read a Geosoft GXF (uncompressed or base-90 compressed, any #SENSE)
    -> Grid2D with (x0, y0) at the bottom-left corner."""
    data, path = _load(src)
    text = data.decode('latin-1')
    lines = text.splitlines()
    hdr, grid_at = _gxf_header(lines)
    if grid_at is None:
        raise ValueError('GXF: no #GRID object found')
    warnings = []

    def num(label, default, *alts):
        v = _gxf_value(hdr, label, *alts)
        if v is None:
            return default
        f = _gxf_fields(v)
        try:
            return float(f[0])
        except (IndexError, ValueError):
            warnings.append('GXF: %s value %r not numeric; default %r used' % (label, v, default))
            return default

    points = int(num('POINTS', 0))
    rows = int(num('ROWS', 0))
    if points <= 0 or rows <= 0:
        raise ValueError('GXF: #POINTS (%d) and #ROWS (%d) are required' % (points, rows))
    ptsep = num('PTSEPARATION', 1.0, 'PTSE')
    rwsep = num('RWSEPARATION', 1.0, 'RWSE')
    xorigin = num('XORIGIN', 0.0, 'XORI')
    yorigin = num('YORIGIN', 0.0, 'YORI')
    rotation = num('ROTATION', 0.0, 'ROTA')
    sense = int(num('SENSE', 1, 'SENS'))
    gtype = int(num('GTYPE', 0))
    title = _gxf_value(hdr, 'TITLE', 'TITL') or ''
    if title.startswith('"') and title.endswith('"') and len(title) >= 2:
        title = title[1:-1]
    scale, offset, tname = 1.0, 0.0, ''
    tv = _gxf_value(hdr, 'TRANSFORM', 'TRAN')
    if tv is not None:
        f = _gxf_fields(tv)
        try:
            scale = float(f[0])
            offset = float(f[1]) if len(f) > 1 else 0.0
        except (IndexError, ValueError):
            warnings.append('GXF: #TRANSFORM %r not understood; identity used' % tv)
            scale, offset = 1.0, 0.0
        if len(f) > 2:
            tname = f[2]
    dummy_text = _gxf_value(hdr, 'DUMMY', 'DUMM')
    if dummy_text is None:
        dummy_value = GXF_DEFAULT_DUMMY
        dummy_text = ''
    else:
        dummy_text = _gxf_fields(dummy_text)[0] if _gxf_fields(dummy_text) else dummy_text
        try:
            dummy_value = float(dummy_text)
        except ValueError:
            warnings.append('GXF: #DUMMY %r not numeric; matched as text only' % dummy_text)
            dummy_value = float('inf')
    units = 'm'
    unit_scale = 1.0
    uv = _gxf_value(hdr, 'UNIT_LENGTH', 'UNIT')
    if uv is not None:
        f = _gxf_fields(uv)
        if f:
            units = f[0]
        if len(f) > 1:
            try:
                unit_scale = float(f[1])
            except ValueError:
                pass
    if sense not in _SENSE:
        raise ValueError('GXF: #SENSE %d is not one of +-1..+-4' % sense)

    count = points * rows
    body = lines[grid_at:]
    if gtype > 0:
        flat, dummies = _gxf_values_compressed(body, gtype, count, scale, offset, warnings)
    else:
        flat, dummies = _gxf_values_plain(body, count, dummy_text, dummy_value, scale, offset, warnings)
        if scale != 1.0 or offset != 0.0:
            warnings.append('GXF: #TRANSFORM %r,%r applied to uncompressed values per the spec '
                            '(GDAL ignores it)' % (scale, offset))

    horizontal, flip_x, flip_y = _SENSE[sense]
    if horizontal:
        nx, ny, dx, dy = points, rows, ptsep, rwsep
    else:
        nx, ny, dx, dy = rows, points, rwsep, ptsep
    values = array.array('d', [NAN]) * count
    for r in range(rows):
        base = r * points
        for p in range(points):
            if horizontal:
                i, j = p, r
            else:
                i, j = r, p
            if flip_x:
                i = nx - 1 - i
            if flip_y:
                j = ny - 1 - j
            values[j * nx + i] = flat[base + p]
    if sense != 1:
        warnings.append('GXF: #SENSE %d re-ordered to south-west-first; #XORIGIN/#YORIGIN taken '
                        'as the bottom-left corner per the GXF spec (GDAL would take them as '
                        'the first stored point)' % sense)

    g = Grid2D(nx, ny, xorigin, yorigin, dx, dy, values, rotation=rotation, units=units,
               name=title)
    if not g.name and path:
        g.name = os.path.splitext(os.path.basename(path))[0]
    g.provenance = _provenance('gxf', path)
    g.metadata['warnings'] = warnings
    g.metadata['gxf'] = {'sense': sense, 'gtype': gtype, 'points': points, 'rows': rows,
                         'transform': [scale, offset, tname], 'dummy': dummy_text or None,
                         'unit_length': [units, unit_scale],
                         'map_projection': hdr.get('MAP_PROJECTION'),
                         'map_datum_transform': hdr.get('MAP_DATUM_TRANSFORM')}
    g.metadata['dummy_nodes'] = dummies
    g.role = 'surface'
    return g


def write_gxf(grid, dst):
    """Write a Grid2D as an uncompressed GXF (SENSE 1, #DUMMY -1e+32, lines
    <= 80 characters).  Rotation is written as #ROTATION (degrees CCW)."""
    if grid.dx <= 0 or grid.dy <= 0:
        raise ValueError('grid spacing must be positive (dx=%r, dy=%r)' % (grid.dx, grid.dy))
    unit = grid.units or 'm'
    unit_scale = {'m': 1.0, 'ft': 0.3048, 'ftUS': 0.3048006096012, 'km': 1000.0}.get(unit, 1.0)
    out = ['#TITLE', (grid.name or 'grid')[:78], '#POINTS', '%d' % grid.nx, '#ROWS', '%d' % grid.ny,
           '#PTSEPARATION', _fmt(grid.dx), '#RWSEPARATION', _fmt(grid.dy),
           '#XORIGIN', _fmt(grid.x0), '#YORIGIN', _fmt(grid.y0),
           '#ROTATION', _fmt(grid.rotation), '#SENSE', '1',
           '#UNIT_LENGTH', '%s,%s' % (unit, _fmt(unit_scale)),
           '#DUMMY', '-1e+32', '#GRID']
    nx = grid.nx
    vals = grid.values
    for j in range(grid.ny):
        row = vals[j * nx:(j + 1) * nx]
        line = ''
        for v in row:
            tok = '-1e+32' if v != v else _fmt(v)
            if line and len(line) + 1 + len(tok) > 80:
                out.append(line)
                line = tok
            else:
                line = tok if not line else line + ' ' + tok
        out.append(line)
    return _emit(dst, ('\n'.join(out) + '\n').encode('ascii'))


# ------------------------------------------------------------ Geosoft XYZ
_LINE_ABBREV = {'l': 'Line', 't': 'Tie', 'b': 'Base', 'r': 'Random', 's': 'Special',
                'd': 'Trend'}
_LINE_HEADER_RE = re.compile(r'^([A-Za-z]+)\s*(.*)$')


def _split_channels(text):
    s = text.strip()
    if ',' in s:
        return [t.strip() for t in s.split(',') if t.strip()]
    return s.split()


def _line_header(s):
    """('Line', '10') for 'Line 10' / 'L10' / 'Tie 5' / 'Base'; None if the
    text is not a line header (data rows start with a digit, sign, '.' or
    '*')."""
    m = _LINE_HEADER_RE.match(s)
    if not m:
        return None
    kind, rest = m.group(1), m.group(2).strip()
    if len(kind) == 1 and rest and rest[0].isdigit():
        kind = _LINE_ABBREV.get(kind.lower(), kind)
    kind = kind[0].upper() + kind[1:].lower()
    return kind, (rest if rest else kind)


def read_xyz(src, x='X', y='Y', z='Z'):
    """Read a Geosoft XYZ export -> PointSet.  Channels named by the last
    ``/`` header line before the data that lists them (``/ X Y Z MAG``);
    ``x``/``y``/``z`` pick the coordinate channels (case-insensitive; a
    missing Z gives z = 0 with a warning).  Attributes: every other channel
    (numeric, NaN for ``*``), ``line`` (text), ``line_type`` ('Line', 'Tie',
    ...), plus ``flight`` / ``date`` when ``//Flight`` / ``//Date`` appear."""
    data, path = _load(src)
    text = data.decode('latin-1')
    warnings = []
    candidates = []    # comment lines that could be the channel header
    rows = []          # (line_name, line_type, flight, date, tokens)
    line_name, line_type, flight, date = '', '', None, None
    seen_data = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith('//'):
            body = s[2:].strip()
            low = body.lower()
            if low.startswith('flight'):
                flight = body[6:].strip().lstrip(':').strip() or None
            elif low.startswith('date'):
                date = body[4:].strip().lstrip(':').strip() or None
            continue
        if s.startswith('/'):
            body = s[1:].strip()
            if not body or set(body) <= set('=-_ '):
                continue
            toks = _split_channels(body)
            if not seen_data and len(toks) >= 2 and not _numeric_tokens(toks):
                candidates.append(toks)
            continue
        header = _line_header(s)
        if header is not None:
            line_type, line_name = header
            flight = None
            date = None
            continue
        toks = s.replace(',', ' ').split()
        rows.append((line_name, line_type, flight, date, toks))
        seen_data = True
    if not rows:
        raise ValueError('Geosoft XYZ: no data rows found')
    ncol = max(len(r[4]) for r in rows)
    channels = None
    for toks in reversed(candidates):
        up = [t.upper() for t in toks]
        if x.upper() in up and y.upper() in up:
            channels = toks
            break
    if channels is None and candidates:
        channels = candidates[-1]
        warnings.append('channel header guessed from comment %r' % ' '.join(channels))
    if channels is None:
        channels = ['X', 'Y', 'Z'][:ncol] + ['ch%d' % k for k in range(4, ncol + 1)]
        warnings.append('no channel header line; columns named %s' % ', '.join(channels))
    if len(channels) != ncol:
        warnings.append('channel header lists %d names but rows have up to %d columns'
                        % (len(channels), ncol))
        while len(channels) < ncol:
            channels.append('ch%d' % (len(channels) + 1))
    upper = [c.upper() for c in channels]

    def col(name):
        return upper.index(name.upper()) if name and name.upper() in upper else None

    ix, iy, iz = col(x), col(y), col(z)
    if ix is None or iy is None:
        raise ValueError('Geosoft XYZ: coordinate channels %r/%r not in %s' % (x, y, channels))
    if iz is None:
        warnings.append('no %r channel; z set to 0' % z)
    attrs = {c: [] for k, c in enumerate(channels) if k not in (ix, iy, iz)}
    attrs['line'] = []
    attrs['line_type'] = []
    has_flight = any(r[2] is not None for r in rows)
    has_date = any(r[3] is not None for r in rows)
    if has_flight:
        attrs['flight'] = []
    if has_date:
        attrs['date'] = []
    ps = PointSet(role='points')
    dummies = 0
    short = 0
    for line_name, line_type, fl, dt, toks in rows:
        vals = []
        for k in range(ncol):
            t = toks[k] if k < len(toks) else '*'
            if t == '*' or t == '':
                vals.append(NAN)
                dummies += 1
            else:
                try:
                    vals.append(float(t))
                except ValueError:
                    vals.append(NAN)
                    dummies += 1
        if len(toks) < ncol:
            short += 1
        px, py = vals[ix], vals[iy]
        pz = vals[iz] if iz is not None else 0.0
        ps.xyz.extend((px, py, pz))
        for k, c in enumerate(channels):
            if k in (ix, iy, iz):
                continue
            attrs[c].append(vals[k])
        attrs['line'].append(line_name)
        attrs['line_type'].append(line_type)
        if has_flight:
            attrs['flight'].append(fl)
        if has_date:
            attrs['date'].append(dt)
    if short:
        warnings.append('%d rows shorter than %d columns (padded with dummies)' % (short, ncol))
    ps.attributes = attrs
    ps.provenance = _provenance('geosoft_xyz', path)
    ps.metadata['warnings'] = warnings
    ps.metadata['channels'] = channels
    ps.metadata['dummies'] = dummies
    if path:
        ps.name = os.path.splitext(os.path.basename(path))[0]
    return ps


def _numeric_tokens(toks):
    for t in toks:
        if t == '*':
            continue
        try:
            float(t)
        except ValueError:
            return False
    return True


def write_xyz(points, dst, line_col='line', type_col='line_type', columns=None):
    """Write a PointSet as a Geosoft XYZ file: ``/ X Y Z <channels>`` header,
    ``Line <name>`` / ``Tie <name>`` group headers from ``line_col`` /
    ``type_col`` (consecutive runs), ``*`` for NaN / missing."""
    n = points.n
    if columns is None:
        columns = [c for c in points.attributes
                   if c not in (line_col, type_col, 'flight', 'date')]
    cols = []
    for c in columns:
        col = points.attributes.get(c, [])
        cols.append(list(col) + [None] * (n - len(col)))
    lines_attr = list(points.attributes.get(line_col, [])) + [None] * n
    types_attr = list(points.attributes.get(type_col, [])) + [None] * n
    flights = list(points.attributes.get('flight', [])) + [None] * n
    dates = list(points.attributes.get('date', [])) + [None] * n
    out = ['/ ' + ' '.join(['X', 'Y', 'Z'] + [str(c).replace(' ', '_') for c in columns]),
           '/' + '=' * 60]
    cur = object()
    cur_fl = object()
    cur_dt = object()
    for k in range(n):
        key = (lines_attr[k], types_attr[k])
        if key != cur:
            cur = key
            name, kind = lines_attr[k], types_attr[k]
            kind = (str(kind).strip() or 'Line') if kind not in (None, '') else 'Line'
            kind = kind[0].upper() + kind[1:].lower()
            if name in (None, ''):
                out.append(kind)
            else:
                name = str(name)
                out.append(kind if name.lower() == kind.lower() else '%s %s' % (kind, name))
            cur_fl = object()
            cur_dt = object()
        if flights[k] != cur_fl and flights[k] not in (None, ''):
            out.append('//Flight %s' % flights[k])
        cur_fl = flights[k]
        if dates[k] != cur_dt and dates[k] not in (None, ''):
            out.append('//Date %s' % dates[k])
        cur_dt = dates[k]
        px, py, pz = points.point(k)
        fields = [_xyz_num(px), _xyz_num(py), _xyz_num(pz)]
        for col in cols:
            fields.append(_xyz_num(col[k]))
        out.append(' '.join(fields))
    return _emit(dst, ('\n'.join(out) + '\n').encode('latin-1', 'replace'))


def _xyz_num(v):
    if v is None or v == '':
        return '*'
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return v.replace(' ', '_')
    if isinstance(v, float) and v != v:
        return '*'
    return _fmt(v)


__all__ = ['read_grd', 'write_grd', 'read_gxf', 'write_gxf', 'read_xyz', 'write_xyz']
