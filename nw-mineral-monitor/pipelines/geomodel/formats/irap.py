"""Irap classic ASCII grid (RMS / Petrel "Irap classic" surface export).

Layout (free-format, whitespace separated)::

    -996  nrow  xinc  yinc
    xori  xmax  yori  ymax
    ncol  rotation  xori  yori
    0 0 0 0 0 0 0
    values, up to 6 per line

``nrow`` is the number of rows (along Y), ``ncol`` the number of columns
(along X).  Values are in Fortran order: the first value is node (i=0, j=0)
at (xori, yori) — the south-west corner — with the column index (X)
varying fastest, then rows northwards.  Undefined nodes are 9999900.0.
``rotation`` is degrees anticlockwise about (xori, yori), the same
convention as ``Grid2D.rotation``; xmax/ymax are the unrotated extents.

Only the standard library is used.
"""
import array
import os

from ..model import Grid2D, NAN

UNDEF = 9999900.0
_MAGIC = -996


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


def read_irap(src):
    """Read an Irap classic ASCII grid -> Grid2D."""
    data, path = _load(src)
    text = data.decode('latin-1')
    tokens = text.split()
    if len(tokens) < 19:
        raise ValueError('Irap: header truncated (%d tokens)' % len(tokens))
    warnings = []
    try:
        head = [float(t) for t in tokens[:19]]
    except ValueError as e:
        raise ValueError('Irap: header not numeric: %s' % e)
    magic = int(head[0])
    if magic != _MAGIC:
        raise ValueError('Irap: first value %d is not -996 (not an Irap classic ASCII grid)' % magic)
    nrow = int(head[1])
    xinc, yinc = head[2], head[3]
    xori, xmax, yori, ymax = head[4:8]
    ncol = int(head[8])
    rotation = head[9]
    rot_x, rot_y = head[10], head[11]
    flags = head[12:19]
    if any(flags):
        warnings.append('non-zero fourth header line %r' % flags)
    if ncol <= 0 or nrow <= 0:
        raise ValueError('Irap: grid size %d x %d invalid' % (ncol, nrow))
    if xinc <= 0 or yinc <= 0:
        raise ValueError('Irap: increments must be positive (xinc=%r yinc=%r)' % (xinc, yinc))
    if (rot_x, rot_y) != (xori, yori):
        warnings.append('rotation origin (%r, %r) differs from xori/yori (%r, %r); '
                        'rotation applied about xori/yori' % (rot_x, rot_y, xori, yori))
    exp_xmax = xori + (ncol - 1) * xinc
    exp_ymax = yori + (nrow - 1) * yinc
    if abs(exp_xmax - xmax) > 1e-6 * max(1.0, abs(xmax)) or abs(exp_ymax - ymax) > 1e-6 * max(1.0, abs(ymax)):
        warnings.append('xmax/ymax (%r, %r) inconsistent with origin + (n-1)*inc (%r, %r); '
                        'increments trusted' % (xmax, ymax, exp_xmax, exp_ymax))
    count = ncol * nrow
    vals = tokens[19:]
    if len(vals) < count:
        raise ValueError('Irap: expected %d values, found %d' % (count, len(vals)))
    if len(vals) > count:
        warnings.append('%d trailing tokens ignored' % (len(vals) - count))
    values = array.array('d')
    undef = 0
    for t in vals[:count]:
        try:
            v = float(t)
        except ValueError:
            raise ValueError('Irap: non-numeric value %r' % t)
        if v >= UNDEF - 1e-3 or v != v:
            values.append(NAN)
            undef += 1
        else:
            values.append(v)
    g = Grid2D(ncol, nrow, xori, yori, xinc, yinc, values, rotation=rotation)
    if path:
        g.name = os.path.splitext(os.path.basename(path))[0]
    g.provenance = {'format': 'irap'}
    if path:
        g.provenance['path'] = path
    g.metadata['warnings'] = warnings
    g.metadata['irap'] = {'xmax': xmax, 'ymax': ymax, 'rotation_origin': [rot_x, rot_y]}
    g.metadata['undefined_nodes'] = undef
    g.role = 'surface'
    return g


def write_irap(grid, dst, per_line=6):
    """Write a Grid2D as an Irap classic ASCII grid (rotation honoured)."""
    if grid.dx <= 0 or grid.dy <= 0:
        raise ValueError('grid spacing must be positive (dx=%r, dy=%r)' % (grid.dx, grid.dy))
    per_line = max(1, int(per_line))
    out = ['%d %d %s %s' % (_MAGIC, grid.ny, _fmt(grid.dx), _fmt(grid.dy)),
           '%s %s %s %s' % (_fmt(grid.x0), _fmt(grid.xmax), _fmt(grid.y0), _fmt(grid.ymax)),
           '%d %s %s %s' % (grid.nx, _fmt(grid.rotation), _fmt(grid.x0), _fmt(grid.y0)),
           '0 0 0 0 0 0 0']
    undef = _fmt(UNDEF)
    vals = grid.values
    n = len(vals)
    for k in range(0, n, per_line):
        out.append(' '.join(undef if v != v else _fmt(v) for v in vals[k:k + per_line]))
    return _emit(dst, ('\n'.join(out) + '\n').encode('ascii'))


__all__ = ['read_irap', 'write_irap', 'UNDEF']
