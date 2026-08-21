"""Arc/Info ASCII grid (.asc) — the ESRI "AAIGrid" raster.

Header keywords (case-insensitive, any order, first token of each line)::

    ncols        7
    nrows        5
    xllcorner    100.0      (or xllcenter: the centre of the lower-left cell)
    yllcorner    200.0      (or yllcenter)
    cellsize     25.0       (GDAL also writes dx / dy for non-square cells)
    NODATA_value -9999      (optional; GDAL default -9999)

followed by nrows rows of ncols values, the NORTH row first.  Cells are
areas; ``Grid2D`` is node-registered, so the node of the lower-left cell is
at ``xllcorner + cellsize/2`` (``xllcenter`` is already the node) and rows
are flipped into our south-first order.  The writer emits the
xllcorner/yllcorner form and needs square cells (dx == dy) and no rotation —
the format has neither.

Only the standard library is used.
"""
import array
import os

from ..model import Grid2D, NAN

_HEADER_KEYS = ('ncols', 'nrows', 'xllcorner', 'xllcenter', 'yllcorner', 'yllcenter',
                'cellsize', 'dx', 'dy', 'nodata_value')


def _load(src):
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


def read_asc(src):
    """Read an Arc/Info ASCII grid -> Grid2D (node-registered, south row
    first, NaN for NODATA)."""
    data, path = _load(src)
    text = data.decode('latin-1')
    lines = text.splitlines()
    warnings = []
    hdr = {}
    k = 0
    while k < len(lines):
        s = lines[k].strip()
        if not s:
            k += 1
            continue
        parts = s.split()
        key = parts[0].lower()
        if key in _HEADER_KEYS and len(parts) >= 2:
            try:
                hdr[key] = float(parts[1])
            except ValueError:
                raise ValueError('Arc ASCII: header %s has non-numeric value %r' % (parts[0], parts[1]))
            k += 1
            continue
        break
    for need in ('ncols', 'nrows'):
        if need not in hdr:
            raise ValueError('Arc ASCII: header lacks %s' % need)
    nx, ny = int(hdr['ncols']), int(hdr['nrows'])
    if nx <= 0 or ny <= 0:
        raise ValueError('Arc ASCII: grid size %dx%d invalid' % (nx, ny))
    if 'cellsize' in hdr:
        dx = dy = hdr['cellsize']
        if 'dx' in hdr or 'dy' in hdr:
            warnings.append('both cellsize and dx/dy given; cellsize used')
    elif 'dx' in hdr and 'dy' in hdr:
        dx, dy = hdr['dx'], hdr['dy']
    else:
        raise ValueError('Arc ASCII: header lacks cellsize (or dx/dy)')
    if dx <= 0 or dy <= 0:
        raise ValueError('Arc ASCII: cellsize must be positive (dx=%r dy=%r)' % (dx, dy))
    if 'xllcenter' in hdr:
        x0 = hdr['xllcenter']
    elif 'xllcorner' in hdr:
        x0 = hdr['xllcorner'] + dx / 2.0
    else:
        x0 = dx / 2.0
        warnings.append('no xllcorner/xllcenter; x origin assumed 0')
    if 'yllcenter' in hdr:
        y0 = hdr['yllcenter']
    elif 'yllcorner' in hdr:
        y0 = hdr['yllcorner'] + dy / 2.0
    else:
        y0 = dy / 2.0
        warnings.append('no yllcorner/yllcenter; y origin assumed 0')
    nodata = hdr.get('nodata_value')
    if nodata is None:
        warnings.append('no NODATA_value in header; none applied')
    tokens = ' '.join(lines[k:]).split()
    n = nx * ny
    if len(tokens) < n:
        raise ValueError('Arc ASCII: expected %d values, found %d' % (n, len(tokens)))
    if len(tokens) > n:
        warnings.append('%d trailing tokens ignored' % (len(tokens) - n))
    values = array.array('d', [NAN]) * n
    blanks = 0
    tol = 1e-9 * max(1.0, abs(nodata)) if nodata is not None else 0.0
    for r in range(ny):
        j = ny - 1 - r
        base = r * nx
        for i in range(nx):
            t = tokens[base + i]
            try:
                v = float(t)
            except ValueError:
                raise ValueError('Arc ASCII: non-numeric value %r' % t)
            if (nodata is not None and abs(v - nodata) <= tol) or v != v:
                blanks += 1
                continue
            values[j * nx + i] = v
    g = Grid2D(nx, ny, x0, y0, dx, dy, values)
    if path:
        g.name = os.path.splitext(os.path.basename(path))[0]
    g.provenance = {'format': 'arc_ascii'}
    if path:
        g.provenance['path'] = path
    g.metadata['warnings'] = warnings
    g.metadata['nodata_value'] = nodata
    g.metadata['nodata_nodes'] = blanks
    g.role = 'surface'
    return g


def write_asc(grid, dst, nodata=-9999.0):
    """Write a Grid2D as an Arc/Info ASCII grid (xllcorner/yllcorner form,
    north row first).  Requires square cells and no rotation."""
    if grid.rotation:
        raise ValueError('Arc ASCII grids cannot be rotated (grid.rotation=%r); resample to an '
                         'axis-aligned grid first' % grid.rotation)
    if abs(grid.dx - grid.dy) > 1e-9 * max(abs(grid.dx), abs(grid.dy), 1.0):
        raise ValueError('Arc ASCII needs square cells but dx=%r != dy=%r; resample the grid to a '
                         'common cellsize (e.g. min(dx, dy)) before writing' % (grid.dx, grid.dy))
    if grid.dx <= 0:
        raise ValueError('cellsize must be positive (dx=%r)' % grid.dx)
    nodata = float(nodata)
    valid = [v for v in grid.values if v == v]
    if nodata in valid:
        for cand in (-99999.0, -999999.0, -1e32):
            if cand not in valid:
                nodata = cand
                break
    cell = grid.dx
    out = ['ncols        %d' % grid.nx,
           'nrows        %d' % grid.ny,
           'xllcorner    %s' % _fmt(grid.x0 - cell / 2.0),
           'yllcorner    %s' % _fmt(grid.y0 - cell / 2.0),
           'cellsize     %s' % _fmt(cell),
           'NODATA_value %s' % _fmt(nodata)]
    nd = _fmt(nodata)
    nx = grid.nx
    vals = grid.values
    for j in range(grid.ny - 1, -1, -1):
        row = vals[j * nx:(j + 1) * nx]
        out.append(' '.join(nd if v != v else _fmt(v) for v in row))
    return _emit(dst, ('\n'.join(out) + '\n').encode('ascii'))


__all__ = ['read_asc', 'write_asc']
