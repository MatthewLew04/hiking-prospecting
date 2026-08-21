"""CPS-3 ASCII grid (Schlumberger CPS-3 / Petrel "CPS-3 grid" export).

Header keywords (one per line, whitespace separated)::

    FSASCI 0 1 "Computed" 0 1.0E+30      last token = null value
    FSATTR 0 0
    FSLIMI xmin xmax ymin ymax zmin zmax  grid limits (node coordinates)
    FSNROW nrows ncols
    FSXINC xinc yinc
    -> free comment lines

followed by nrows*ncols values.  Values are column-major: the first value is
the top-left (north-west) node, each column runs DOWN (north to south) and
columns advance west to east — the layout Petrel writes.  The within-column
direction has not been verified against a CPS-3 specification, only against
exports, so every grid read here carries that caveat in
``metadata['warnings']``.  Increments come from FSXINC; FSLIMI is checked
against them and a mismatch is reported.

Read-only; only the standard library is used.
"""
import array
import os

from ..model import Grid2D, NAN

ORDER_WARNING = ('CPS-3: values assumed column-major from the north-west node, each column '
                 'running north to south (unverified assumption — check against a known surface)')


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


def _floats(parts, key, n):
    try:
        vals = [float(t) for t in parts[1:1 + n]]
    except ValueError as e:
        raise ValueError('CPS-3: %s not numeric: %s' % (key, e))
    if len(vals) < n:
        raise ValueError('CPS-3: %s needs %d values, found %d' % (key, n, len(vals)))
    return vals


def read_cps3(src):
    """Read a CPS-3 ASCII grid -> Grid2D (node-registered, south row first)."""
    data, path = _load(src)
    text = data.decode('latin-1')
    lines = text.splitlines()
    warnings = [ORDER_WARNING]
    null = 1e30
    limits = None
    nrows = ncols = None
    xinc = yinc = None
    attr = None
    comments = []
    k = 0
    n = len(lines)
    while k < n:
        s = lines[k].strip()
        if not s:
            k += 1
            continue
        parts = s.split()
        key = parts[0].upper()
        if key.startswith('->'):
            comments.append(s[2:].strip())
            k += 1
            continue
        if key.startswith('FS'):
            if key == 'FSASCI':
                if len(parts) > 1:
                    try:
                        null = float(parts[-1])
                    except ValueError:
                        warnings.append('FSASCI null token %r not numeric; 1e30 assumed' % parts[-1])
            elif key == 'FSATTR':
                attr = parts[1:]
            elif key == 'FSLIMI':
                limits = _floats(parts, key, 6)
            elif key == 'FSNROW':
                nrows, ncols = [int(v) for v in _floats(parts, key, 2)]
            elif key == 'FSXINC':
                xinc, yinc = _floats(parts, key, 2)
            else:
                warnings.append('unknown keyword %s ignored' % key)
            k += 1
            continue
        break
    if nrows is None or ncols is None:
        raise ValueError('CPS-3: FSNROW missing')
    if limits is None:
        raise ValueError('CPS-3: FSLIMI missing')
    if nrows <= 0 or ncols <= 0:
        raise ValueError('CPS-3: grid size %d rows x %d cols invalid' % (nrows, ncols))
    xmin, xmax, ymin, ymax, zmin, zmax = limits
    if xinc is None or yinc is None:
        xinc = (xmax - xmin) / (ncols - 1) if ncols > 1 else 1.0
        yinc = (ymax - ymin) / (nrows - 1) if nrows > 1 else 1.0
        warnings.append('FSXINC missing; increments derived from FSLIMI')
    if xinc <= 0 or yinc <= 0:
        raise ValueError('CPS-3: increments must be positive (%r, %r)' % (xinc, yinc))
    if ncols > 1 and abs((xmax - xmin) - (ncols - 1) * xinc) > 1e-6 * max(1.0, abs(xmax - xmin)):
        warnings.append('FSLIMI x extent %r != (ncols-1)*xinc %r; xinc trusted, xmin taken as the '
                        'west node' % (xmax - xmin, (ncols - 1) * xinc))
    if nrows > 1 and abs((ymax - ymin) - (nrows - 1) * yinc) > 1e-6 * max(1.0, abs(ymax - ymin)):
        warnings.append('FSLIMI y extent %r != (nrows-1)*yinc %r; yinc trusted, ymin taken as the '
                        'south node' % (ymax - ymin, (nrows - 1) * yinc))
    tokens = []
    while k < n:
        s = lines[k].strip()
        k += 1
        if not s or s.startswith('->'):
            continue
        tokens.extend(s.split())
    count = nrows * ncols
    if len(tokens) < count:
        raise ValueError('CPS-3: expected %d values, found %d' % (count, len(tokens)))
    if len(tokens) > count:
        warnings.append('%d trailing tokens ignored' % (len(tokens) - count))
    values = array.array('d', [NAN]) * count
    nulls = 0
    tol = 1e-6 * max(1.0, abs(null))
    nx, ny = ncols, nrows
    for i in range(nx):
        base = i * ny
        for r in range(ny):
            t = tokens[base + r]
            try:
                v = float(t)
            except ValueError:
                raise ValueError('CPS-3: non-numeric value %r' % t)
            if v != v or abs(v - null) <= tol or v >= abs(null):
                nulls += 1
                continue
            values[(ny - 1 - r) * nx + i] = v
    g = Grid2D(nx, ny, xmin, ymin, xinc, yinc, values)
    if path:
        g.name = os.path.splitext(os.path.basename(path))[0]
    g.provenance = {'format': 'cps3'}
    if path:
        g.provenance['path'] = path
    g.metadata['warnings'] = warnings
    g.metadata['cps3'] = {'null': null, 'limits': limits, 'attr': attr, 'comments': comments,
                          'zmin': zmin, 'zmax': zmax}
    g.metadata['null_nodes'] = nulls
    g.role = 'surface'
    return g


__all__ = ['read_cps3', 'ORDER_WARNING']
