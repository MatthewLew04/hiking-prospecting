"""Open Mining Format v2.0 (omf-rust / Leapfrog Geo 2025.1+ / Seequent Evo)
reader and writer, pure Python.

Container (verified against gmggroup/omf-rust ``src/file``):

* a ZIP archive whose members are all STORED (the reference reader rejects
  compressed members); the archive comment is ``'Open Mining Format 2.0'``
  optionally followed by ``-<prerelease>`` (the current omf-rust release
  writes and only accepts ``2.0-beta.1``; pass ``prerelease=''`` for the
  final tag once it exists);
* ``index.json.gz`` — gzip of the JSON project (written last);
* arrays as ``<n>.parquet`` (``n`` counts from 1 in write order) and images
  as ``<n>.png`` / ``<n>.jpg``.

Arrays use the exact Parquet schemas omf-rust accepts
(``src/file/parquet/schemas.rs``): e.g. ``required double scalar``,
``required double x, y, z`` for vertices, ``required int32 a, b, c
(integer(32,false))`` for triangles, ``optional double number``,
``optional byte_array text (string)``, ``optional group vector {required
double x, y[, z]}`` and ``optional group color {required int32 r,g,b,a
(integer(8,false))}``.  ``parquet_lite`` writes them with PLAIN encoding and
GZIP (default) or no compression — the two codecs the omf-rust reader
decodes.

Element mapping is shared with ``omf1.py`` (see its docstring): PointSet,
LineSet, Surface (Mesh), GridSurface (Grid2D; tensor / tilted grids become a
Mesh; extra numeric attributes become property grids), BlockModel (regular;
sub-blocks are ignored with a warning), Composite (flattened, children keep
the composite name as ``group``).  Attribute types Number / Text / Category /
Boolean / Color / Vector round-trip through the metadata hints; textures are
skipped with a warning in ``project.metadata['warnings']``.
"""
import array
import gzip
import io
import json
import re
import zipfile

from ..model import Mesh, LineSet, PointSet, Project, utm_crs
from . import parquet_lite as pq
from .parquet_lite import Column, Group
from .omf1 import (APPLICATION, OmfError, LOC_VERTICES, LOC_SEGMENTS, LOC_FACES, LOC_CELLS,
                   _read_source, _write_dest, _objects_of, _unique_names, _apply_records,
                   _collect_records, _property_grids, _realign_face_records,
                   _grid_from_tensor_surface, _blockmodel_from_grid, _remap, _rotation_axes,
                   _azimuth_axes, _color4, _iso_from_epoch_micros, _iso_from_epoch_days,
                   _epoch_micros_from_iso, _merge_warnings, _now, _num, _record)

FORMAT_NAME = 'Open Mining Format'
FORMAT_MAJOR, FORMAT_MINOR = 2, 0
DEFAULT_PRERELEASE = 'beta.1'
INDEX_NAME = 'index.json.gz'
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _parse_comment(comment):
    """b'Open Mining Format 2.0-beta.1' -> ((2, 0), 'beta.1') or None."""
    try:
        text = comment.decode('utf-8')
    except (UnicodeDecodeError, AttributeError):
        return None
    m = re.match(r'^' + re.escape(FORMAT_NAME) + r' (\d+)\.(\d+)(?:-([^\s]+))?$', text.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2))), m.group(3)


def format_comment(prerelease=DEFAULT_PRERELEASE):
    s = '%s %d.%d' % (FORMAT_NAME, FORMAT_MAJOR, FORMAT_MINOR)
    if prerelease:
        s += '-' + prerelease
    return s


# ==================================================================== reader
def read_omf2(src):
    """Read an OMF v2 file (path or bytes) -> Project."""
    data, label = _read_source(src)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise OmfError('not an OMF v2 (zip) file: %s' % e)
    ver = _parse_comment(zf.comment)
    if ver is None:
        raise OmfError('zip archive comment %r is not an Open Mining Format version tag' % zf.comment[:60])
    (major, minor), pre = ver
    if major != FORMAT_MAJOR:
        raise OmfError('unsupported OMF major version %d.%d' % (major, minor))
    names = set(zf.namelist())
    if INDEX_NAME not in names:
        raise OmfError('missing %s' % INDEX_NAME)
    try:
        index = json.loads(gzip.decompress(zf.read(INDEX_NAME)).decode('utf-8'))
    except (OSError, ValueError) as e:
        raise OmfError('bad index.json.gz: %s' % e)
    rd = _Omf2Reader(zf, index, label)
    project = Project(name=index.get('name') or 'model')
    version = '%d.%d' % (major, minor) + ('-' + pre if pre else '')
    project.metadata.update({
        'omf_version': version, 'author': index.get('author', ''),
        'description': index.get('description', ''), 'date': index.get('date', ''),
        'application': index.get('application', ''), 'units': index.get('units', ''),
        'coordinate_reference_system': index.get('coordinate_reference_system', ''),
        'warnings': rd.warnings})
    if index.get('metadata'):
        project.metadata['omf_metadata'] = index['metadata']
    project.crs = _crs_from_string(index.get('coordinate_reference_system', ''), index.get('units', ''))
    porigin = [float(c) for c in (index.get('origin') or [0.0, 0.0, 0.0])]
    for el in index.get('elements') or []:
        for obj in rd.element(el, porigin, group=''):
            project.add(obj)
    return project


def _crs_from_string(crs, units):
    units = (units or '').strip().lower()
    unit = 'ft' if units in ('feet', 'foot', 'ft', 'us survey feet', 'international feet') else 'm'
    crs = (crs or '').strip()
    m = re.match(r'^EPSG:(\d+)$', crs, re.I)
    if m:
        code = int(m.group(1))
        if 32601 <= code <= 32660:
            d = utm_crs(code - 32600, True)
        elif 32701 <= code <= 32760:
            d = utm_crs(code - 32700, False)
        else:
            # not a UTM zone: keep the model's 'local' kind but remember the code
            d = {'kind': 'local', 'epsg': code, 'units': unit}
        d['crs_string'] = crs
        return d
    d = {'kind': 'local', 'units': unit}
    if crs:
        d['crs_string'] = crs
    return d


class _Omf2Reader(object):
    def __init__(self, zf, index, label):
        self.zf = zf
        self.index = index
        self.label = label
        self.warnings = []
        self._cache = {}

    def warn(self, msg):
        self.warnings.append(msg)

    # -- arrays
    def table(self, ref):
        """Array reference {'filename', 'item_count'} -> ParquetFile."""
        if not isinstance(ref, dict) or 'filename' not in ref:
            raise OmfError('bad array reference %r' % (ref,))
        fn = ref['filename']
        if fn not in self._cache:
            try:
                data = self.zf.read(fn)
            except KeyError:
                raise OmfError('missing array file %s' % fn)
            try:
                self._cache[fn] = pq.read_parquet(data)
            except pq.ParquetError as e:
                raise OmfError('%s: %s' % (fn, e))
        pf = self._cache[fn]
        n = int(ref.get('item_count', pf.num_rows))
        if n != pf.num_rows:
            self.warn('%s: item_count %d differs from the parquet row count %d' % (fn, n, pf.num_rows))
        return pf

    def scalars(self, ref):
        return self.table(ref).column('scalar')

    def vertices(self, ref, origin):
        pf = self.table(ref)
        xs, ys, zs = pf.column('x'), pf.column('y'), pf.column('z')
        out = array.array('d', [0.0]) * (3 * len(xs))
        ox, oy, oz = origin
        for k in range(len(xs)):
            out[3 * k] = xs[k] + ox
            out[3 * k + 1] = ys[k] + oy
            out[3 * k + 2] = zs[k] + oz
        return out

    def indices(self, ref, cols):
        pf = self.table(ref)
        columns = [pf.column(c) for c in cols]
        n = len(columns[0])
        out = array.array('I', [0]) * (n * len(cols))
        w = len(cols)
        for k in range(n):
            for c in range(w):
                out[k * w + c] = columns[c][k]
        return out

    # -- elements
    def element(self, el, porigin, group):
        name = el.get('name') or ''
        geom = el.get('geometry') or {}
        gtype = geom.get('type')
        kw = {}
        color = _color4(el.get('color'))
        if color:
            kw['color'] = color[:3]
            if color[3] < 255:
                kw['opacity'] = color[3] / 255.0
        if group:
            kw['group'] = group
        meta = {}
        if el.get('description'):
            meta['description'] = el['description']
        el_meta = dict(el.get('metadata') or {})
        nwmm = el_meta.pop(NWMM_KEY, None)
        if el_meta:
            meta['omf_metadata'] = el_meta
        if meta:
            kw['metadata'] = meta
        objs = []
        try:
            if gtype == 'Composite':
                for child in geom.get('elements') or []:
                    objs.extend(self.element(child, porigin, group=name))
                if el.get('attributes'):
                    self.warn('%s: attributes on a composite element skipped' % name)
                return objs
            if gtype == 'PointSet':
                origin = _add(porigin, geom.get('origin'))
                obj = PointSet(self.vertices(geom['vertices'], origin), name=name, **kw)
                _apply_records(obj, self.records(el, gtype), self.warn)
                objs.append(obj)
            elif gtype == 'LineSet':
                origin = _add(porigin, geom.get('origin'))
                obj = LineSet(self.vertices(geom['vertices'], origin),
                              self.indices(geom['segments'], ('a', 'b')), name=name, **kw)
                _apply_records(obj, self.records(el, gtype), self.warn)
                objs.append(obj)
            elif gtype == 'Surface':
                origin = _add(porigin, geom.get('origin'))
                obj = Mesh(self.vertices(geom['vertices'], origin),
                           self.indices(geom['triangles'], ('a', 'b', 'c')), name=name, **kw)
                _apply_records(obj, self.records(el, gtype), self.warn)
                objs.append(obj)
            elif gtype == 'GridSurface':
                orient = geom.get('orient') or {}
                origin = _add(porigin, orient.get('origin'))
                u = orient.get('u') or [1.0, 0.0, 0.0]
                v = orient.get('v') or [0.0, 1.0, 0.0]
                tu, tv = self._grid2(geom.get('grid') or {})
                heights = self.scalars(geom['heights']) if geom.get('heights') else None
                obj, is_grid, tri_cells = _grid_from_tensor_surface(name, origin, u, v, tu, tv, heights, self.warn, **kw)
                recs = self.records(el, gtype)
                if is_grid:
                    if heights is None and recs:
                        # flat grid carrying data: the property grids are the content
                        objs.extend(_property_grids(obj, recs, self.warn))
                    else:
                        objs.append(obj)
                        objs.extend(_property_grids(obj, recs, self.warn))
                else:
                    _apply_records(obj, _realign_face_records(recs, tri_cells), self.warn)
                    objs.append(obj)
            elif gtype == 'BlockModel':
                orient = geom.get('orient') or {}
                origin = _add(porigin, orient.get('origin'))
                u = orient.get('u') or [1.0, 0.0, 0.0]
                v = orient.get('v') or [0.0, 1.0, 0.0]
                w = orient.get('w') or [0.0, 0.0, 1.0]
                tu, tv, tw = self._grid3(geom.get('grid') or {})
                if geom.get('subblocks'):
                    self.warn('%s: sub-blocks are not supported; parent blocks only' % name)
                bm, remap = _blockmodel_from_grid(name, origin, u, v, w, tu, tv, tw, self.warn, **kw)
                if bm is not None:
                    recs = self.records(el, gtype)
                    for r in recs:
                        if r['location'] == LOC_CELLS:
                            r['values'] = _remap(list(r['values']), remap)
                    _apply_records(bm, recs, self.warn)
                    objs.append(bm)
            else:
                self.warn('%s: unsupported geometry type %r; skipped' % (name, gtype))
        except (OmfError, KeyError, ValueError, TypeError) as e:
            self.warn('%s: unreadable (%s: %s)' % (name, type(e).__name__, e))
            return []
        for obj in objs:
            obj.provenance = {'format': 'omf2', 'path': self.label, 'element': name}
            if isinstance(nwmm, dict) and obj.name == name:
                _restore_nwmm(obj, nwmm)
        return objs

    def _grid2(self, grid):
        if grid.get('type') == 'Regular':
            size, count = grid['size'], grid['count']
            return [float(size[0])] * int(count[0]), [float(size[1])] * int(count[1])
        if grid.get('type') == 'Tensor':
            return list(self.scalars(grid['u'])), list(self.scalars(grid['v']))
        raise OmfError('unknown grid type %r' % grid.get('type'))

    def _grid3(self, grid):
        if grid.get('type') == 'Regular':
            size, count = grid['size'], grid['count']
            return ([float(size[0])] * int(count[0]), [float(size[1])] * int(count[1]),
                    [float(size[2])] * int(count[2]))
        if grid.get('type') == 'Tensor':
            return list(self.scalars(grid['u'])), list(self.scalars(grid['v'])), list(self.scalars(grid['w']))
        raise OmfError('unknown grid type %r' % grid.get('type'))

    # -- attributes
    def records(self, el, gtype):
        out = []
        for att in el.get('attributes') or []:
            try:
                rec = self.record(att, gtype, el.get('name', ''))
            except (OmfError, KeyError, ValueError, TypeError) as e:
                self.warn('%s/%s: unreadable attribute (%s)' % (el.get('name'), att.get('name'), e))
                continue
            if rec is not None:
                out.append(rec)
        return out

    def record(self, att, gtype, ename):
        name = att.get('name') or ''
        loc = att.get('location')
        data = att.get('data') or {}
        dtype = data.get('type')
        if loc == 'Vertices':
            location = LOC_VERTICES
        elif loc == 'Primitives':
            location = {'LineSet': LOC_SEGMENTS, 'Surface': LOC_FACES, 'GridSurface': LOC_FACES,
                        'BlockModel': LOC_CELLS}.get(gtype, 'primitives')
        elif loc == 'Categories' and gtype == 'Categories':
            location = loc
        else:
            if dtype not in ('MappedTexture', 'ProjectedTexture'):
                self.warn('%s/%s: attribute location %r skipped' % (ename, name, loc))
                return None
            location = loc
        extra = {'units': att.get('units', ''), 'description': att.get('description', '')}
        if dtype == 'Number':
            pf = self.table(data['values'])
            leaf = pf.leaf('number')
            vals = pf.column('number')
            rec = self._number_record(name, location, vals, leaf, extra)
            if data.get('colormap'):
                rec['colormap'] = self._colormap(data['colormap'], leaf)
            return rec
        if dtype == 'Text':
            return _record(name, location, 'text', self.table(data['values']).column('text'), **extra)
        if dtype == 'Boolean':
            return _record(name, location, 'boolean', self.table(data['values']).column('bool'), **extra)
        if dtype == 'Category':
            idx = self.table(data['values']).column('index')
            names = self.table(data['names']).column('name')
            colors = None
            if data.get('gradient'):
                colors = self._gradient(data['gradient'])
            sub = {}
            for satt in data.get('attributes') or []:
                srec = self.record(satt, 'Categories', ename + '/' + name)
                if srec is not None:
                    sub[satt.get('name', '')] = list(srec['values'])
            return _record(name, location, 'category', idx, names=names, colors=colors, sub=sub or None, **extra)
        if dtype == 'Vector':
            pf = self.table(data['values'])
            xs, ys = pf.column('vector.x'), pf.column('vector.y')
            zs = pf.column('vector.z') if pf.leaf('vector.z') else None
            vals = []
            for k in range(len(xs)):
                if xs[k] is None:
                    vals.append(None)
                elif zs is None:
                    vals.append([xs[k], ys[k]])
                else:
                    vals.append([xs[k], ys[k], zs[k]])
            return _record(name, location, 'vector', vals, dim=2 if zs is None else 3, **extra)
        if dtype == 'Color':
            pf = self.table(data['values'])
            cols = [pf.column('color.' + c) for c in 'rgba']
            vals = [None if cols[0][k] is None else [cols[c][k] for c in range(4)] for k in range(len(cols[0]))]
            return _record(name, location, 'color', vals, **extra)
        if dtype in ('MappedTexture', 'ProjectedTexture'):
            self.warn('%s/%s: %s skipped (image %s)' % (ename, name, dtype, (data.get('image') or {}).get('filename')))
            return None
        self.warn('%s/%s: unsupported attribute data type %r; skipped' % (ename, name, dtype))
        return None

    def _number_record(self, name, location, vals, leaf, extra):
        lt = leaf.logical if leaf is not None else None
        if lt and lt[0] == 'date':
            return _record(name, location, 'datetime', [_iso_from_epoch_days(v) for v in vals], **extra)
        if lt and lt[0] == 'timestamp':
            unit = lt[1]
            scale = {'millis': 1000, 'micros': 1, 'nanos': 0.001}[unit]
            out = []
            for v in vals:
                out.append(None if v is None else _iso_from_epoch_micros(int(v * scale)))
            return _record(name, location, 'datetime', out, **extra)
        return _record(name, location, 'number', [None if v is None else float(v) for v in vals], **extra)

    def _gradient(self, ref):
        pf = self.table(ref)
        cols = [pf.column(c) for c in 'rgba']
        return [[cols[c][k] for c in range(4)] for k in range(len(cols[0]))]

    def _colormap(self, cm, leaf):
        try:
            if cm.get('type') == 'Continuous':
                rng = cm.get('range') or {}
                return {'type': 'continuous', 'range': [rng.get('min'), rng.get('max')],
                        'gradient': self._gradient(cm['gradient'])}
            if cm.get('type') == 'Discrete':
                pf = self.table(cm['boundaries'])
                bounds = list(zip(pf.column('value'), pf.column('inclusive')))
                return {'type': 'discrete', 'boundaries': [[v, bool(i)] for v, i in bounds],
                        'gradient': self._gradient(cm['gradient'])}
        except (OmfError, KeyError) as e:
            self.warn('colormap unreadable: %s' % e)
        return None


def _add(origin, offset):
    if not offset:
        return list(origin)
    return [origin[k] + float(offset[k]) for k in range(3)]


# application-specific element metadata (OMF asks for a prefix per application)
NWMM_KEY = 'nwmm'


def _nwmm_metadata(obj):
    d = {'kind': obj.kind, 'id': obj.id}
    for key in ('role', 'units', 'group'):
        v = getattr(obj, key, None)
        if v:
            d[key] = v
    if not getattr(obj, 'visible', True):
        d['visible'] = False
    return d


def _restore_nwmm(obj, d):
    if d.get('role') and hasattr(obj, 'role'):
        obj.role = d['role']
    if d.get('units') and hasattr(obj, 'units'):
        obj.units = d['units']
    if d.get('group') and not obj.group:
        obj.group = d['group']
    if d.get('id'):
        obj.id = d['id']
    if d.get('visible') is False:
        obj.visible = False


# ==================================================================== writer
class _Omf2Writer(object):
    def __init__(self, zf, warn, compression='gzip'):
        self.zf = zf
        self.warn = warn
        self.compression = compression
        self.counter = 0
        self.elements = []

    # -- arrays
    def _store(self, data, ext):
        self.counter += 1
        name = '%d%s' % (self.counter, ext)
        info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        self.zf.writestr(info, data)
        return name

    def table(self, fields, n):
        data = pq.write_parquet(fields, compression=self.compression)
        return {'filename': self._store(data, '.parquet'), 'item_count': n}

    def scalars(self, values):
        vals = [float(v) for v in values]
        return self.table([Column('scalar', 'double', vals)], len(vals))

    def vertices(self, flat):
        n = len(flat) // 3
        return self.table([Column('x', 'double', flat[0::3]), Column('y', 'double', flat[1::3]),
                           Column('z', 'double', flat[2::3])], n)

    def index_tuples(self, flat, names):
        w = len(names)
        n = len(flat) // w
        cols = [Column(nm, 'int32', [int(v) for v in flat[k::w]], logical='uint32') for k, nm in enumerate(names)]
        return self.table(cols, n)

    def gradient(self, colors):
        rows = [(_color4(c) or [128, 128, 128, 255]) for c in colors]
        cols = [Column(ch, 'int32', [r[k] for r in rows], logical='uint8') for k, ch in enumerate('rgba')]
        return self.table(cols, len(rows))

    # -- attributes
    def attribute(self, rec, gtype, label):
        name = rec['name']
        loc = rec['location']
        if loc == LOC_VERTICES:
            location = 'Vertices'
        elif loc in (LOC_SEGMENTS, LOC_FACES, LOC_CELLS):
            location = 'Primitives'
        elif loc == 'Categories':
            location = 'Categories'
        else:
            self.warn('%s/%s: location %r cannot be written' % (label, name, loc))
            return None
        rtype = rec['type']
        vals = rec['values']
        n = len(vals)
        data = None
        if rtype == 'number':
            nums = [None if (v is None or (isinstance(v, float) and v != v)) else float(v) for v in vals]
            data = {'type': 'Number', 'values': self.table([Column('number', 'double', nums, optional=True)], n)}
            cm = self.colormap(rec.get('colormap'))
            if cm:
                data['colormap'] = cm
        elif rtype == 'text':
            data = {'type': 'Text', 'values': self.table(
                [Column('text', 'byte_array', [None if v is None else str(v) for v in vals], optional=True, logical='string')], n)}
        elif rtype == 'boolean':
            data = {'type': 'Boolean', 'values': self.table(
                [Column('bool', 'boolean', [None if v is None else bool(v) for v in vals], optional=True)], n)}
        elif rtype == 'datetime':
            micros = [_epoch_micros_from_iso(v) for v in vals]
            data = {'type': 'Number', 'values': self.table(
                [Column('number', 'int64', micros, optional=True, logical='timestamp_micros')], n)}
        elif rtype == 'vector':
            dim = rec.get('dim') or 3
            present = [v is not None for v in vals]
            comps = []
            for k, ch in enumerate('xyz'[:dim]):
                comps.append(Column(ch, 'double', [0.0 if v is None else float(v[k]) for v in vals]))
            data = {'type': 'Vector', 'values': self.table([Group('vector', comps, present=present)], n)}
        elif rtype == 'color':
            rows = [_color4(v) for v in vals]
            present = [r is not None for r in rows]
            comps = [Column(ch, 'int32', [0 if r is None else r[k] for r in rows], logical='uint8')
                     for k, ch in enumerate('rgba')]
            data = {'type': 'Color', 'values': self.table([Group('color', comps, present=present)], n)}
        elif rtype == 'category':
            names = [str(s) for s in (rec.get('names') or [])]
            idx = [None if (i is None) else int(i) for i in vals]
            top = max([i for i in idx if i is not None] + [-1])
            while len(names) <= top:
                names.append('category %d' % len(names))
            data = {'type': 'Category',
                    'values': self.table([Column('index', 'int32', idx, optional=True, logical='uint32')], n),
                    'names': self.table([Column('name', 'byte_array', names, logical='string')], len(names))}
            colors = rec.get('colors')
            if colors and len(colors) == len(names):
                data['gradient'] = self.gradient(colors)
            subs = []
            for sname, svals in (rec.get('sub') or {}).items():
                svals = list(svals)
                if len(svals) != len(names):
                    continue
                numeric = all(v is None or isinstance(v, (int, float)) for v in svals)
                srec = _record(sname, 'Categories', 'number' if numeric else 'text',
                               [_num(v) if numeric else (None if v is None else str(v)) for v in svals])
                if numeric:
                    srec['values'] = [None if v != v else v for v in srec['values']]
                sa = self.attribute(srec, 'Categories', label + '/' + name)
                if sa:
                    subs.append(sa)
            if subs:
                data['attributes'] = subs
        else:
            self.warn('%s/%s: attribute type %r not written' % (label, name, rtype))
            return None
        att = {'name': name, 'location': location, 'data': data}
        if rec.get('description'):
            att['description'] = rec['description']
        if rec.get('units'):
            att['units'] = rec['units']
        return att

    def colormap(self, cm):
        if not cm or not cm.get('gradient'):
            return None
        try:
            if cm.get('type') == 'continuous':
                lo, hi = cm.get('range') or [None, None]
                if lo is None or hi is None:
                    return None
                return {'type': 'Continuous', 'range': {'min': float(lo), 'max': float(hi)},
                        'gradient': self.gradient(cm['gradient'])}
            if cm.get('type') == 'discrete':
                bounds = cm.get('boundaries') or []
                if len(cm['gradient']) != len(bounds) + 1:
                    return None
                tbl = self.table([Column('value', 'double', [float(b[0]) for b in bounds]),
                                  Column('inclusive', 'boolean', [bool(b[1]) for b in bounds])], len(bounds))
                return {'type': 'Discrete', 'boundaries': tbl, 'gradient': self.gradient(cm['gradient'])}
        except (TypeError, ValueError, IndexError):
            return None
        return None

    # -- elements
    def element(self, obj, name):
        color = _color4(list(obj.color or [160, 160, 160])[:3] + [int(round(255 * float(getattr(obj, 'opacity', 1.0) or 1.0)))])
        el = {'name': name, 'color': color}
        desc = str(obj.metadata.get('description', '')) if obj.metadata else ''
        if desc:
            el['description'] = desc
        el_meta = {}
        if obj.metadata and isinstance(obj.metadata.get('omf_metadata'), dict):
            el_meta.update(obj.metadata['omf_metadata'])
        el_meta[NWMM_KEY] = _nwmm_metadata(obj)
        el['metadata'] = el_meta
        recs = _collect_records(obj, self.warn)
        kind = obj.kind
        if kind == 'points':
            el['geometry'] = {'type': 'PointSet', 'vertices': self.vertices(obj.xyz)}
            gtype = 'PointSet'
        elif kind == 'lineset':
            el['geometry'] = {'type': 'LineSet', 'vertices': self.vertices(obj.vertices),
                              'segments': self.index_tuples(obj.segments, ('a', 'b'))}
            gtype = 'LineSet'
        elif kind == 'mesh':
            el['geometry'] = {'type': 'Surface', 'vertices': self.vertices(obj.vertices),
                              'triangles': self.index_tuples(obj.triangles, ('a', 'b', 'c'))}
            gtype = 'Surface'
        elif kind == 'grid2d':
            if obj.nx < 2 or obj.ny < 2:
                self.warn('%s: grids need at least 2 x 2 nodes for OMF; skipped' % name)
                return None
            au, av = _rotation_axes(obj.rotation)
            geom = {'type': 'GridSurface',
                    'orient': {'origin': [obj.x0, obj.y0, 0.0], 'u': au, 'v': av},
                    'grid': {'type': 'Regular', 'size': [obj.dx, obj.dy], 'count': [obj.nx - 1, obj.ny - 1]}}
            if obj.role == 'property':
                recs = [_record(name, LOC_VERTICES, 'number', list(obj.values), units=obj.units)]
            else:
                geom['heights'] = self.scalars(obj.values)
            el['geometry'] = geom
            gtype = 'GridSurface'
        elif kind == 'blockmodel':
            au, av, aw = _azimuth_axes(obj.azimuth)
            el['geometry'] = {'type': 'BlockModel',
                              'orient': {'origin': list(obj.origin), 'u': au, 'v': av, 'w': aw},
                              'grid': {'type': 'Regular', 'size': list(obj.block_size), 'count': list(obj.count)}}
            gtype = 'BlockModel'
        elif kind == 'drillholes':
            return self.element(obj.traces_lineset(), name + ' traces')
        else:
            self.warn('%s: object kind %r has no OMF v2 equivalent; skipped' % (name, kind))
            return None
        allowed = {'PointSet': (LOC_VERTICES,), 'LineSet': (LOC_VERTICES, LOC_SEGMENTS),
                   'Surface': (LOC_VERTICES, LOC_FACES), 'GridSurface': (LOC_VERTICES, LOC_FACES),
                   'BlockModel': (LOC_CELLS,)}[gtype]
        atts = []
        anames = _unique_names([r['name'] for r in recs], 'attribute')
        for rec, an in zip(recs, anames):
            if rec['location'] not in allowed:
                self.warn('%s/%s: location %r is not valid on a %s; skipped' % (name, rec['name'], rec['location'], gtype))
                continue
            rec = dict(rec, name=an)
            att = self.attribute(rec, gtype, name)
            if att:
                atts.append(att)
        if atts:
            el['attributes'] = atts
        return el


def _crs_string(project, crs):
    if crs:
        return crs
    if project is None:
        return ''
    c = project.crs or {}
    if c.get('crs_string'):
        return c['crs_string']
    if c.get('epsg'):
        return 'EPSG:%d' % int(c['epsg'])
    return ''


def _date_string(project):
    d = None
    if project is not None:
        d = project.metadata.get('date') or project.created
    d = d or _now()
    if re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$', str(d)):
        return str(d)
    us = _epoch_micros_from_iso(d)
    return _iso_from_epoch_micros(us) if us is not None else _now()


def write_omf2(project_or_objects, dst, name='', description='', crs='', prerelease=DEFAULT_PRERELEASE,
               compression='gzip', author='', units='', warnings=None):
    """Write a Project or a list of model objects as OMF v2.0.

    ``dst``: path or binary file object; returns the path (or bytes for a
    BytesIO).  ``prerelease``: version tag suffix (omf-rust 0.2.0-beta.1
    accepts only 'beta.1'; use '' for the final 2.0 tag).  ``compression``:
    'gzip' (default) or 'none' for the parquet pages.  Problems go to
    ``warnings`` (a list) and to ``project.metadata['warnings']``.
    """
    project, objects = _objects_of(project_or_objects)
    warns = warnings if warnings is not None else []
    if project is not None:
        name = name or project.name
        description = description or str(project.metadata.get('description', ''))
        author = author or str(project.metadata.get('author', ''))
        units = units or str(project.metadata.get('units', '') or '')
        if not units:
            u = (project.crs or {}).get('units', '')
            units = {'m': 'meters', 'ft': 'feet'}.get(u, u or '')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED, allowZip64=True) as zf:
        w = _Omf2Writer(zf, warns.append, compression=compression)
        names = _unique_names([o.name for o in objects], 'element')
        elements = []
        for obj, nm in zip(objects, names):
            el = w.element(obj, nm)
            if el:
                elements.append(el)
        index = {'name': name or 'model'}
        if description:
            index['description'] = description
        crs_s = _crs_string(project, crs)
        if crs_s:
            index['coordinate_reference_system'] = crs_s
        if units:
            index['units'] = units
        if author:
            index['author'] = author
        index['application'] = APPLICATION
        index['date'] = _date_string(project)
        if project is not None and isinstance(project.metadata.get('omf_metadata'), dict):
            index['metadata'] = project.metadata['omf_metadata']
        index['elements'] = elements
        payload = gzip.compress(json.dumps(index, separators=(',', ':'), allow_nan=False).encode('utf-8'),
                                compresslevel=6, mtime=0)
        info = zipfile.ZipInfo(INDEX_NAME, date_time=_ZIP_DATE)
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        zf.writestr(info, payload)
        zf.comment = format_comment(prerelease).encode('utf-8')
    _merge_warnings(project, warns)
    return _write_dest(buf.getvalue(), dst)


def convert_omf1_to_omf2(src, dst, **kw):
    """OMF v0.9 file -> OMF v2.0 file."""
    from .omf1 import read_omf1
    return write_omf2(read_omf1(src), dst, **kw)


def convert_omf2_to_omf1(src, dst, **kw):
    """OMF v2.0 file -> OMF v0.9 file."""
    from .omf1 import write_omf1
    return write_omf1(read_omf2(src), dst, **kw)
