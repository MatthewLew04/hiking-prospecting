"""UBC-GIF 3-D mesh (.msh) + model files — the voxel interchange Geosoft
VOXI, Leapfrog, SimPEG and the UBC inversion codes share.

Mesh file::

    NE NN NZ                 cells along easting, northing, depth
    E0 N0 Z0                 SOUTH-WEST *TOP* corner
    <NE widths along E>      west -> east   ('n*w' repeats a width n times)
    <NN widths along N>      south -> north
    <NZ widths along Z>      TOP -> bottom

Model file: one value per cell, ordered with Z varying fastest (top to
bottom), then easting (west to east), then northing (south to north) — the
first value is the top-south-west cell.

``BlockModel`` is the opposite way up: origin = minimum corner
``(E0, N0, Z0 - sum(dz))``, attributes in i (X) fastest, then j (Y), then
k (Z) order with k = 0 at the BOTTOM.  Only uniform-width meshes map onto
a regular ``BlockModel``; variable widths raise ``ValueError``.  UBC files
have no no-data marker, so ``read_ubc(..., nodata=v)`` turns ``v`` into NaN
and the writer substitutes ``nodata`` (default -99999.0) for NaN and says so
in the model's metadata.

Only the standard library is used.
"""
import array
import os

from ..model import BlockModel, NAN


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


def _tokens(text):
    out = []
    for line in text.splitlines():
        for mark in ('!', '#'):
            cut = line.find(mark)
            if cut >= 0:
                line = line[:cut]
        out.extend(line.replace(',', ' ').split())
    return out


def _take_widths(tokens, pos, count, axis):
    """Expand ``count`` cell widths (with 'n*w' repeats) from tokens[pos:]."""
    widths = []
    while len(widths) < count:
        if pos >= len(tokens):
            raise ValueError('UBC mesh: ran out of %s widths (%d of %d)' % (axis, len(widths), count))
        t = tokens[pos]
        pos += 1
        if '*' in t:
            a, b = t.split('*', 1)
            try:
                rep, w = int(float(a)), float(b)
            except ValueError:
                raise ValueError('UBC mesh: bad width token %r' % t)
            widths.extend([w] * rep)
        else:
            try:
                widths.append(float(t))
            except ValueError:
                raise ValueError('UBC mesh: bad width token %r' % t)
    if len(widths) > count:
        raise ValueError('UBC mesh: %s widths expand to %d values, mesh declares %d'
                         % (axis, len(widths), count))
    return widths, pos


def _uniform(widths, axis):
    w0 = widths[0]
    for w in widths:
        if abs(w - w0) > 1e-9 * max(1.0, abs(w0)):
            raise ValueError('UBC mesh has variable %s cell widths (%s ...); only uniform meshes '
                             'map onto a regular BlockModel — resample or split the mesh first'
                             % (axis, ', '.join(_fmt(v) for v in widths[:6])))
    if w0 <= 0:
        raise ValueError('UBC mesh: non-positive %s cell width %r' % (axis, w0))
    return w0


def read_mesh(src):
    """Parse a UBC mesh -> dict(count=[ne, nn, nz], origin_top=[e0, n0, z0],
    widths=(we, wn, wz))."""
    data, path = _load(src)
    tokens = _tokens(data.decode('latin-1'))
    if len(tokens) < 6:
        raise ValueError('UBC mesh: header truncated')
    try:
        ne, nn, nz = int(float(tokens[0])), int(float(tokens[1])), int(float(tokens[2]))
        e0, n0, z0 = float(tokens[3]), float(tokens[4]), float(tokens[5])
    except ValueError as e:
        raise ValueError('UBC mesh: header not numeric: %s' % e)
    if ne <= 0 or nn <= 0 or nz <= 0:
        raise ValueError('UBC mesh: cell counts %d %d %d invalid' % (ne, nn, nz))
    pos = 6
    we, pos = _take_widths(tokens, pos, ne, 'easting')
    wn, pos = _take_widths(tokens, pos, nn, 'northing')
    wz, pos = _take_widths(tokens, pos, nz, 'Z')
    return {'count': [ne, nn, nz], 'origin_top': [e0, n0, z0], 'widths': (we, wn, wz),
            'path': path, 'trailing_tokens': len(tokens) - pos}


def _read_model(src, n):
    data, path = _load(src)
    tokens = _tokens(data.decode('latin-1'))
    if len(tokens) < n:
        raise ValueError('UBC model %s: expected %d values, found %d' % (path or '', n, len(tokens)))
    try:
        vals = [float(t) for t in tokens[:n]]
    except ValueError as e:
        raise ValueError('UBC model %s: non-numeric value (%s)' % (path or '', e))
    return vals, path, len(tokens) - n


def read_ubc(mesh_src, model_src=None, name='property', models=None, nodata=None):
    """Read a UBC mesh (+ model file(s)) -> BlockModel.  ``model_src`` is
    one model file stored as attribute ``name``; ``models`` = {attribute:
    path_or_bytes, ...} reads several.  ``nodata`` (float) is mapped to NaN."""
    mesh = read_mesh(mesh_src)
    ne, nn, nz = mesh['count']
    e0, n0, z0 = mesh['origin_top']
    warnings = []
    if mesh['trailing_tokens']:
        warnings.append('%d trailing tokens in the mesh file ignored' % mesh['trailing_tokens'])
    dx = _uniform(mesh['widths'][0], 'easting')
    dy = _uniform(mesh['widths'][1], 'northing')
    dz = _uniform(mesh['widths'][2], 'Z')
    bm = BlockModel([e0, n0, z0 - nz * dz], [dx, dy, dz], [ne, nn, nz])
    bm.provenance = {'format': 'ubc'}
    if mesh['path']:
        bm.provenance['path'] = mesh['path']
        bm.name = os.path.splitext(os.path.basename(mesh['path']))[0]
    sources = []
    if model_src is not None:
        sources.append((name, model_src))
    for k, v in (models or {}).items():
        sources.append((k, v))
    n = ne * nn * nz
    model_paths = {}
    for attr, src in sources:
        vals, path, extra = _read_model(src, n)
        if extra:
            warnings.append('model %s: %d trailing values ignored' % (attr, extra))
        out = array.array('d', [NAN]) * n
        tol = 1e-9 * max(1.0, abs(nodata)) if nodata is not None else 0.0
        p = 0
        for iy in range(nn):
            for ix in range(ne):
                for kz in range(nz):
                    v = vals[p]
                    p += 1
                    if nodata is not None and abs(v - nodata) <= tol:
                        continue
                    out[ix + ne * (iy + nn * (nz - 1 - kz))] = v
        bm.add_attribute(attr, out)
        if path:
            model_paths[attr] = path
    if model_paths:
        bm.provenance['model_paths'] = model_paths
    bm.metadata['warnings'] = warnings
    bm.metadata['ubc'] = {'origin_top': [e0, n0, z0], 'nodata': nodata}
    return bm


def write_ubc(blockmodel, mesh_dst, model_dst=None, attribute=None, nodata=-99999.0):
    """Write a BlockModel as a UBC mesh (+ model).  ``model_dst`` is a path /
    file object for ``attribute`` (default: the first numeric attribute), or
    a dict {attribute: dst} for several.  Returns (mesh_result,
    model_result) — model_result is a dict when several were written."""
    if blockmodel.azimuth:
        raise ValueError('UBC meshes are axis-aligned but the block model has azimuth %r; '
                         'rotate / resample it first' % blockmodel.azimuth)
    ne, nn, nz = blockmodel.count
    dx, dy, dz = blockmodel.block_size
    ox, oy, oz = blockmodel.origin
    mesh = ['%d %d %d' % (ne, nn, nz),
            '%s %s %s' % (_fmt(ox), _fmt(oy), _fmt(oz + nz * dz)),
            '%d*%s' % (ne, _fmt(dx)),
            '%d*%s' % (nn, _fmt(dy)),
            '%d*%s' % (nz, _fmt(dz))]
    mesh_out = _emit(mesh_dst, ('\n'.join(mesh) + '\n').encode('ascii'))
    if model_dst is None:
        return mesh_out, None
    if isinstance(model_dst, dict):
        targets = list(model_dst.items())
    else:
        if attribute is None:
            numeric = [k for k, a in blockmodel.attributes.items() if a.get('type') == 'number']
            if not numeric:
                raise ValueError('block model has no numeric attribute to write')
            attribute = numeric[0]
        targets = [(attribute, model_dst)]
    results = {}
    for attr, dst in targets:
        if attr not in blockmodel.attributes:
            raise ValueError('block model has no attribute %r (has %s)' % (attr, sorted(blockmodel.attributes)))
        vals = blockmodel.attributes[attr]['values']
        lines = []
        nan_count = 0
        for iy in range(nn):
            for ix in range(ne):
                for kz in range(nz):
                    v = vals[ix + ne * (iy + nn * (nz - 1 - kz))]
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        v = NAN
                    if v != v:
                        nan_count += 1
                        v = nodata
                    lines.append(_fmt(v))
        if nan_count:
            warn = blockmodel.metadata.setdefault('warnings', [])
            warn.append('ubc write: %d NaN cells of %r written as %s' % (nan_count, attr, _fmt(nodata)))
        results[attr] = _emit(dst, ('\n'.join(lines) + '\n').encode('ascii'))
    if isinstance(model_dst, dict):
        return mesh_out, results
    return mesh_out, results[targets[0][0]]


__all__ = ['read_ubc', 'write_ubc', 'read_mesh']
