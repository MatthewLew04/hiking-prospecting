"""ZMAP+ ASCII grid (Landmark ZMAP, Kingdom, Petrel "ZMAP+ grid" export).

Layout::

    ! comment lines
    @<name>, GRID, <nodes_per_line>
    <field_width>, <null_value>, <null_text>, <decimals>, <start_col>
    <nrows>, <ncols>, <xmin>, <xmax>, <ymin>, <ymax>
    0.0, 0.0, 0.0
    @
    values ...

Values are column-major: the first (western) column first, each column
running from ymax (north) DOWN to ymin; columns west to east.  xmin/xmax/
ymin/ymax are NODE coordinates (the grid is node-registered, like
``Grid2D``), so ``dx = (xmax - xmin) / (ncols - 1)``.  Fields are fixed
width (``field_width`` characters, right-justified) — GDAL's reader insists
on it — but free-format whitespace-separated values are accepted on read.
The null value is written with the same width/decimals.

Only the standard library is used.
"""
import array
import os

from ..model import Grid2D, NAN


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


def _csv(line):
    return [t.strip() for t in line.split(',')]


def _num(tok, what):
    try:
        return float(tok)
    except (TypeError, ValueError):
        raise ValueError('ZMAP: %s is not numeric: %r' % (what, tok))


def _line_tokens(s, width, null_text):
    """Tokens of one data line: fixed-width fields when the line length is a
    whole number of fields and every field parses, else whitespace split.
    Blank fixed-width fields (or fields equal to null_text) yield None."""
    s = s.rstrip('\r\n')
    if width > 0 and s and len(s) % width == 0:
        fields = [s[k:k + width] for k in range(0, len(s), width)]
        out = []
        ok = True
        for f in fields:
            t = f.strip()
            if not t or (null_text and t == null_text):
                out.append(None)
                continue
            try:
                out.append(float(t))
            except ValueError:
                ok = False
                break
        if ok:
            return out
    out = []
    for t in s.split():
        if null_text and t == null_text:
            out.append(None)
            continue
        try:
            out.append(float(t))
        except ValueError:
            raise ValueError('ZMAP: non-numeric grid value %r' % t)
    return out


def read_zmap(src):
    """Read a ZMAP+ grid -> Grid2D (node-registered, south row first)."""
    data, path = _load(src)
    text = data.decode('latin-1')
    lines = text.splitlines()
    warnings = []
    k = 0
    n = len(lines)
    # find the '@name, GRID, n' line
    while k < n and not lines[k].lstrip().startswith('@'):
        k += 1
    if k >= n:
        raise ValueError('ZMAP: no @ header line found')
    head = _csv(lines[k].lstrip()[1:])
    name = head[0] if head else ''
    kind = head[1].upper() if len(head) > 1 else 'GRID'
    if kind != 'GRID':
        warnings.append('header type %r is not GRID' % kind)
    nodes_per_line = None
    if len(head) > 2 and head[2]:
        try:
            nodes_per_line = int(float(head[2]))
        except ValueError:
            warnings.append('nodes-per-line %r not numeric' % head[2])
    k += 1
    hdr = []
    while k < n and len(hdr) < 3:
        s = lines[k].strip()
        k += 1
        if not s or s.startswith('!'):
            continue
        if s.startswith('@'):
            break
        hdr.append(_csv(s))
    if len(hdr) < 2:
        raise ValueError('ZMAP: header block incomplete (%d of 3 lines)' % len(hdr))
    f1 = hdr[0] + [''] * 5
    width = int(float(f1[0])) if f1[0] else 0
    null_value = _num(f1[1], 'null value') if f1[1] else None
    null_text = f1[2]
    decimals = int(float(f1[3])) if f1[3] else None
    f2 = hdr[1] + [''] * 6
    nrows = int(_num(f2[0], 'nrows'))
    ncols = int(_num(f2[1], 'ncols'))
    xmin, xmax = _num(f2[2], 'xmin'), _num(f2[3], 'xmax')
    ymin, ymax = _num(f2[4], 'ymin'), _num(f2[5], 'ymax')
    if nrows <= 0 or ncols <= 0:
        raise ValueError('ZMAP: grid size %d cols x %d rows invalid' % (ncols, nrows))
    # skip to the closing '@'
    while k < n and not lines[k].lstrip().startswith('@'):
        k += 1
    if k >= n:
        raise ValueError('ZMAP: header not closed with @')
    k += 1
    count = nrows * ncols
    flat = []
    while k < n and len(flat) < count:
        s = lines[k]
        k += 1
        if not s.strip() or s.lstrip().startswith('!'):
            continue
        flat.extend(_line_tokens(s, width, null_text))
    if len(flat) < count:
        raise ValueError('ZMAP: expected %d values, found %d' % (count, len(flat)))
    if len(flat) > count:
        warnings.append('%d trailing values ignored' % (len(flat) - count))
    tol = 1e-9 * max(1.0, abs(null_value)) if null_value is not None else 0.0
    values = array.array('d', [NAN]) * count
    nulls = 0
    nx, ny = ncols, nrows
    for i in range(nx):
        base = i * ny
        for r in range(ny):
            v = flat[base + r]
            j = ny - 1 - r
            if v is None or v != v or (null_value is not None and abs(v - null_value) <= tol):
                nulls += 1
                continue
            values[j * nx + i] = v
    dx = (xmax - xmin) / (nx - 1) if nx > 1 else 1.0
    dy = (ymax - ymin) / (ny - 1) if ny > 1 else 1.0
    if nx == 1 or ny == 1:
        warnings.append('single row/column: spacing undefined, set to 1.0')
    if dx <= 0 or dy <= 0:
        warnings.append('non-positive spacing dx=%r dy=%r' % (dx, dy))
    g = Grid2D(nx, ny, xmin, ymin, dx, dy, values, name=name.strip())
    if not g.name and path:
        g.name = os.path.splitext(os.path.basename(path))[0]
    g.provenance = {'format': 'zmap'}
    if path:
        g.provenance['path'] = path
    g.metadata['warnings'] = warnings
    g.metadata['zmap'] = {'field_width': width, 'null_value': null_value, 'null_text': null_text,
                          'decimals': decimals, 'nodes_per_line': nodes_per_line}
    g.metadata['null_nodes'] = nulls
    g.role = 'surface'
    return g


def _field(v, width, decimals):
    s = '%.*f' % (decimals, v)
    if len(s) > width:
        s = '%.*e' % (max(1, width - 8), v)
    if len(s) > width:
        raise ValueError('ZMAP: value %r does not fit in a %d-character field' % (v, width))
    return s.rjust(width)


def write_zmap(grid, dst, nodes_per_line=5, field_width=20, decimals=7, null=-9999.0):
    """Write a Grid2D as a ZMAP+ grid (fixed-width fields, column-major,
    north to south within each column).  ZMAP has no rotation."""
    if grid.rotation:
        raise ValueError('ZMAP grids cannot be rotated (grid.rotation=%r); resample to an '
                         'axis-aligned grid first' % grid.rotation)
    if grid.dx <= 0 or grid.dy <= 0:
        raise ValueError('grid spacing must be positive (dx=%r, dy=%r)' % (grid.dx, grid.dy))
    nodes_per_line = max(1, int(nodes_per_line))
    field_width = max(8, int(field_width))
    decimals = max(0, int(decimals))
    null = float(null)
    name = (grid.name or 'grid').replace(',', ' ').replace('\n', ' ').strip() or 'grid'
    out = ['! ZMAP+ grid written by nwmm geomodel',
           '! %s' % name,
           '@%s, GRID, %d' % (name, nodes_per_line),
           '%d, %s, , %d, 1' % (field_width, _field(null, field_width, decimals).strip(), decimals),
           '%d, %d, %s, %s, %s, %s' % (grid.ny, grid.nx, _field(grid.x0, field_width, decimals).strip(),
                                       _field(grid.xmax, field_width, decimals).strip(),
                                       _field(grid.y0, field_width, decimals).strip(),
                                       _field(grid.ymax, field_width, decimals).strip()),
           '0.0, 0.0, 0.0', '@']
    null_txt = _field(null, field_width, decimals)
    nx, ny = grid.nx, grid.ny
    for i in range(nx):
        col = []
        for j in range(ny - 1, -1, -1):
            v = grid.values[j * nx + i]
            col.append(null_txt if v != v else _field(v, field_width, decimals))
        for k in range(0, ny, nodes_per_line):
            out.append(''.join(col[k:k + nodes_per_line]))
    return _emit(dst, ('\n'.join(out) + '\n').encode('ascii'))


__all__ = ['read_zmap', 'write_zmap']
