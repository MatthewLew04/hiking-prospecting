#!/usr/bin/env python3
"""gen_gm_fixtures.py — fixtures + reference expectations for tools/test_gm_formats.mjs.

    python3 tools/gen_gm_fixtures.py gen [outdir]            write every format with the
                                                             Python writers, read each file
                                                             back with the Python readers and
                                                             dump the result to expected.json
    python3 tools/gen_gm_fixtures.py summarize <format> <path> [json-opts]
                                                             read one file with the Python reader
                                                             and print its summary (used by the
                                                             node test to check JS-written files)

The summary schema is mirrored in the node test (summarize() there) so a JS object and a
Python object can be compared with NaN == None and numeric tolerances.
"""
import array
import io
import json
import math
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

from geomodel.model import Grid2D, Mesh, LineSet, PointSet, BlockModel, Project, farray, utm_crs, NAN  # noqa: E402
from geomodel import formats  # noqa: E402
from geomodel.formats import (surfer, geosoft, arcascii, zmap, irap, ubc, cps3, obj as objfmt, dxf as dxffmt,  # noqa: E402
                              gocad as gocadfmt, lfmsh, tables, segy, las)
from geomodel.formats.omf1 import read_omf1, write_omf1  # noqa: E402
from geomodel.formats.omf2 import read_omf2, write_omf2  # noqa: E402

REF = '/home/claude/ref'
SAMPLE_V2 = os.path.join(REF, 'omf-rust-git', 'tests', 'one_of_everything.omf')
if not os.path.exists(SAMPLE_V2):
    SAMPLE_V2 = os.path.join(REF, 'omfrust', 'one_of_everything.omf')
SAMPLE_V09 = os.path.join(REF, 'test_v09.omf')
TESTS = os.path.join(ROOT, 'tests')


# ------------------------------------------------------------------ summaries
def J(v):
    """JSON-safe value: NaN/inf -> None, arrays -> lists."""
    if isinstance(v, float):
        return None if (v != v or v in (math.inf, -math.inf)) else v
    if isinstance(v, (list, tuple, array.array)):
        return [J(x) for x in v]
    if isinstance(v, dict):
        return {str(k): J(x) for k, x in v.items()}
    if isinstance(v, bytes):
        return v.decode('latin-1')
    return v


def fsum(values):
    return math.fsum(v for v in values if v == v)


def nnan(values):
    return sum(1 for v in values if v != v)


def summarize(obj):
    if isinstance(obj, Project):
        return {'project': True, 'name': obj.name, 'crs': J(obj.crs), 'metadata': J({k: v for k, v in obj.metadata.items() if k != 'warnings'}),
                'warnings': list(obj.metadata.get('warnings', [])), 'objects': [summarize(o) for o in obj.objects]}
    if isinstance(obj, list):
        return [summarize(o) for o in obj]
    if isinstance(obj, dict):
        return J(obj)
    k = obj.kind
    base = {'kind': k, 'name': obj.name, 'color': list(obj.color), 'role': getattr(obj, 'role', None), 'group': obj.group, 'opacity': obj.opacity,
            'provenance_format': obj.provenance.get('format'), 'warnings': list(obj.metadata.get('warnings', []))}
    md = {kk: J(v) for kk, v in obj.metadata.items() if kk not in ('warnings', 'extra_arrays') and not isinstance(v, (bytes, bytearray))}
    if 'property_of' in md:
        md['property_of'] = True          # parent grid id: random per run
    base['metadata'] = md
    if k == 'grid2d':
        base.update({'nx': obj.nx, 'ny': obj.ny, 'x0': obj.x0, 'y0': obj.y0, 'dx': obj.dx, 'dy': obj.dy, 'rotation': obj.rotation, 'units': obj.units,
                     'values': J(list(obj.values)), 'n_nan': nnan(obj.values)})
    elif k == 'mesh':
        v, t = obj.vertices, obj.triangles
        base.update({'nv': obj.n_vertices, 'nt': obj.n_triangles, 'vsum': [fsum(v[0::3]), fsum(v[1::3]), fsum(v[2::3])], 'tsum': int(sum(t)),
                     'v_first': J(list(v[:9])), 't_first': list(t[:9]), 'bounds': J(obj.bounds()),
                     'attributes': {n: {'location': a.get('location', 'vertices'), 'n': len(a['values']), 'sum': fsum(a['values']), 'n_nan': nnan(a['values']),
                                        'first': J(list(a['values'])[:8])} for n, a in obj.attributes.items()}})
    elif k == 'lineset':
        v = obj.vertices
        # parts are only comparable when the file carries them explicitly (BLN / DXF / GOCAD -> non-empty
        # features); parts derived from bare segments differ between the python model and gm-core for loops
        feats = [f for f in obj.features if f]
        explicit = bool(feats)
        base.update({'nv': obj.n_vertices, 'nseg': len(obj.segments), 'vsum': [fsum(v[0::3]), fsum(v[1::3]), fsum(v[2::3])], 'seg_first': list(obj.segments[:12]),
                     'parts_len': [len(p) for p in obj.parts] if explicit else None, 'parts_first': list(obj.parts[0][:10]) if (explicit and obj.parts) else [], 'features_first': J(feats[:5]),
                     'attributes': {n: {'location': a.get('location', 'vertices'), 'n': len(a['values']), 'sum': fsum(a['values']), 'first': J(list(a['values'])[:8])}
                                    for n, a in (getattr(obj, 'attributes', None) or {}).items()}})
    elif k == 'points':
        attrs = {}
        for n, col in obj.attributes.items():
            col = list(col)
            numeric = all(v is None or isinstance(v, (int, float)) and not isinstance(v, bool) for v in col) and any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in col)
            entry = {'n': len(col), 'first': J(col[:8])}
            if numeric:
                entry['sum'] = fsum(NAN if v is None else float(v) for v in col)
            attrs[n] = entry
        base.update({'n': obj.n, 'xyz_first': J(list(obj.xyz[:9])), 'xyz_sum': [fsum(obj.xyz[0::3]), fsum(obj.xyz[1::3]), fsum(obj.xyz[2::3])], 'attributes': attrs})
    elif k == 'blockmodel':
        attrs = {}
        for n, a in obj.attributes.items():
            vals = list(a['values'])
            entry = {'type': a['type'], 'n': len(vals), 'first': J(vals[:12])}
            if a['type'] == 'number':
                entry['sum'] = fsum(vals)
                entry['n_nan'] = nnan(vals)
            attrs[n] = entry
        base.update({'origin': list(obj.origin), 'block_size': list(obj.block_size), 'count': list(obj.count), 'azimuth': obj.azimuth, 'attributes': attrs})
    elif k == 'drillholes':
        base.update({'collars': J(obj.collars), 'surveys': J(obj.surveys), 'intervals': J(obj.intervals)})
    return base


def segy_summary(d):
    return {'n_traces': d['n_traces'], 'ns': d['ns'], 'dt': d['dt'], 'format': d['format'], 'endian': d['endian'], 'revision': d['revision'],
            'text_encoding': d['text_encoding'], 'text_first': d['text_header'].split('\n')[:3], 'text_lines': len(d['text_header'].split('\n')),
            'binary_header': d['binary_header'], 'trace_headers': d['trace_headers'][:4], 'coords': [list(c) for c in d['coords']],
            'samples': [J(list(s)) for s in d['samples']], 'warnings': d['warnings']}


def las_summary(d):
    return {'version': d['version'], 'wrap': d['wrap'], 'delimiter': d['delimiter'], 'well': d['well'], 'curves': d['curves'], 'params': d['params'],
            'other': d['other'], 'data': {k: J(list(v)) for k, v in d['data'].items()}, 'index_unit': d['index_unit'], 'null': d['null'],
            'n_rows': d['n_rows'], 'sections': d['sections'], 'warnings': d['warnings']}


# ------------------------------------------------------------------- objects
def make_grid(dx=25.0, dy=10.0, rotation=0.0, name='test grid'):
    nx, ny = 7, 5
    vals = [NAN if (i == 2 and j == 3) else -50.0 + 3.25 * i + 100.0 * j for j in range(ny) for i in range(nx)]
    return Grid2D(nx, ny, 500000.0, 4500000.0, dx, dy, vals, rotation=rotation, name=name)


def pyramid(name='pyramid'):
    verts = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (5, 5, 8)]
    tris = [(0, 2, 1), (0, 3, 2), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    return Mesh(verts, tris, name=name, color=[255, 0, 0])


def sample_lines():
    ls = LineSet(name='Drift 300 level', color=[0, 255, 0])
    ls.add_polyline([(0, 0, 0), (10, 0, 1), (10, 10, 2)], {'type': 'drift'})
    ls.add_polyline([(5, 5, 5), (6, 6, 6)], {'type': 'raise'})
    return ls


def sample_points():
    ps = PointSet(name='collars', color=[0, 0, 255])
    ps.add(1, 2, 3, hole='A')
    ps.add(4, 5, 6, hole='B')
    return ps


def build_project():
    prj = Project(name='Test kit', crs=utm_crs(12, True))
    prj.metadata['description'] = 'kit description'
    ps = PointSet([[500000, 4100000, 1500], [500010, 4100000, 1510], [500020, 4100010, 1520]], name='samples', color=[255, 0, 0], role='samples',
                  attributes={'Au_ppm': [1.5, None, 3.25], 'lith': ['qtz', None, 'sch'], 'ok': [True, False, None],
                              'when': ['2020-01-01T00:00:00Z', None, '2021-06-15T12:30:00Z']})
    ps.metadata['datetime_attributes'] = ['when']
    ps.metadata['categories'] = {'lith': {'names': ['qtz', 'sch'], 'colors': [[255, 0, 0, 255], [0, 0, 255, 255]], 'index': False}}
    ps.metadata['vector_attributes'] = {'vec': ['vx', 'vy', 'vz']}
    ps.attributes['vx'] = [1.0, 0.0, NAN]
    ps.attributes['vy'] = [0.0, 1.0, NAN]
    ps.attributes['vz'] = [0.0, 0.0, NAN]
    ps.metadata['color_attributes'] = ['col']
    ps.attributes['col'] = [[1, 2, 3, 255], None, [10, 20, 30, 40]]
    ps.metadata['attribute_units'] = {'Au_ppm': 'ppm'}
    ps.metadata['colormaps'] = {'Au_ppm': {'type': 'continuous', 'range': [0.0, 5.0], 'gradient': [[0, 0, 0, 255], [255, 255, 255, 255]]}}
    prj.add(ps)
    ls = LineSet(name='workings', color=[0, 255, 0], role='workings')
    ls.add_polyline([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
    ls.add_polyline([(5, 5, 5), (6, 6, 6)])
    ls.attributes = {'seglen': {'location': 'segments', 'values': farray([1.0, 1.0, math.sqrt(3)])}}
    prj.add(ls)
    m = Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 1]], [[0, 1, 2], [1, 3, 2]], name='surf', color=[0, 0, 255], role='contact',
             attributes={'elev': {'location': 'vertices', 'values': farray([0, 0, 0, 1])}, 'face_id': {'location': 'faces', 'values': farray([7, 8])}})
    m.opacity = 0.5
    prj.add(m)
    prj.add(Grid2D(3, 2, 100.0, 200.0, 10.0, 20.0, [1, 2, 3, 4, NAN, 6], rotation=30.0, name='topo', role='topography'))
    prj.add(Grid2D(2, 2, 0.0, 0.0, 1.0, 1.0, [10, 11, 12, 13], name='mag', role='property', units='nT'))
    bm = BlockModel([1000, 2000, 300], [10, 10, 5], [2, 3, 2], azimuth=45.0, name='blocks', attributes={'grade': [float(i) if i % 5 else NAN for i in range(12)]})
    bm.add_attribute('rock', ['a', 'b'] * 6, kind='text')
    bm.add_attribute('flag', [True, False, None] * 4, kind='boolean')
    prj.add(bm)
    return prj


def build_geosoft(values, nx, ny, dx, dy, x0, y0, rot=0.0, kx=1, compressed=False, vectors_per_block=3):
    hdr = bytearray(512)
    raw = struct.pack('<%df' % len(values), *[-1e32 if v != v else v for v in values])
    ne, nv = (nx, ny) if kx == 1 else (ny, nx)
    struct.pack_into('<5i', hdr, 0, 4 + (1024 if compressed else 0), 2, ne, nv, kx)
    de, dv = (dx, dy) if kx == 1 else (dy, dx)
    struct.pack_into('<7d', hdr, 20, de, dv, x0, y0, rot, 0.0, 1.0)
    hdr[76:76 + 5] = b'built'
    if not compressed:
        return bytes(hdr) + raw
    per = vectors_per_block * ne * 4
    blocks = [b'\x00' * 16 + zlib.compress(raw[k:k + per]) for k in range(0, len(raw), per)]
    nb = len(blocks)
    pos = 512 + 16 + 8 * nb + 4 * nb
    offsets = []
    for b in blocks:
        offsets.append(pos)
        pos += len(b)
    table = struct.pack('<iiii', 0x1A2B3C4D, 1, nb, vectors_per_block)
    table += struct.pack('<%dq' % nb, *offsets)
    table += struct.pack('<%di' % nb, *[len(b) for b in blocks])
    return bytes(hdr) + table + b''.join(blocks)


def text_block(src_file, marker):
    """Module-level text constant from a unit-test module (keeps fixtures identical to the tests)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('gm_fixture_src_' + src_file.replace('.', '_'), os.path.join(TESTS, src_file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, marker)


def traces(ntr=7, ns=50):
    return [[math.sin(0.3 * i + 0.5 * k) * (k + 1) for i in range(ns)] for k in range(ntr)]


def coords(ntr=7):
    return [(500000 + 10 * k, 4000000 + 5 * k) for k in range(ntr)]


# ------------------------------------------------------------------- readers
def read_with(fmt, path, opts=None):
    """Python reader for a format id (matches formats.REGISTRY + the table/ubc variants)."""
    opts = dict(opts or {})
    if fmt == 'ubc':
        return ubc.read_ubc(path, models={k: os.path.join(os.path.dirname(path), v) if not os.path.isabs(v) else v for k, v in opts.get('models', {}).items()},
                            nodata=opts.get('nodata'))
    if fmt == 'csv_drillholes':
        d = os.path.dirname(path)
        svy = opts.get('survey')
        ivs = {k: os.path.join(d, v) for k, v in opts.get('intervals', {}).items()}
        return tables.read_drillholes(path, os.path.join(d, svy) if svy else None, ivs or None, negative_dip_down=opts.get('negativeDipDown', False))
    if fmt == 'csv_points':
        return tables.read_points_csv(path, x=opts.get('x'), y=opts.get('y'), z=opts.get('z'))
    if fmt == 'csv_blockmodel':
        return tables.read_blockmodel_csv(path, block_size=opts.get('blockSize'))
    if fmt == 'segy':
        return segy.read_segy(path, endian=opts.get('endian'))
    if fmt == 'las':
        return las.read_las(path)
    return formats.reader(fmt)(path)


def summary_for(fmt, path, opts=None):
    r = read_with(fmt, path, opts)
    if fmt == 'segy':
        return segy_summary(r)
    if fmt == 'las':
        return las_summary(r)
    return summarize(r)


# ----------------------------------------------------------------------- gen
def gen(out):
    os.makedirs(out, exist_ok=True)
    fixtures = {}

    def put(rel, data, fmt, opts=None, summary=True):
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if data is not None:
            with open(path, 'wb') as fh:
                fh.write(data if isinstance(data, bytes) else data.encode('utf-8'))
        entry = {'format': fmt, 'opts': opts or {}}
        if summary:
            entry['summary'] = summary_for(fmt, path, opts)
        fixtures[rel] = entry
        return path

    g = make_grid()
    put('grids/dsaa.grd', surfer.write_grd(g, io.BytesIO(), fmt='dsaa'), 'surfer_grd')
    put('grids/dsbb.grd', surfer.write_grd(g, io.BytesIO(), fmt='dsbb'), 'surfer_grd')
    put('grids/dsrb.grd', surfer.write_grd(g, io.BytesIO(), fmt='dsrb'), 'surfer_grd')
    put('grids/blank.grd', surfer.write_grd(Grid2D(3, 2, 0, 0, 1, 1, [NAN] * 6), io.BytesIO(), fmt='dsaa'), 'surfer_grd')
    put('grids/geo_float.grd', geosoft.write_grd(g, io.BytesIO(), dtype='float'), 'geosoft_grd')
    put('grids/geo_short.grd', geosoft.write_grd(g, io.BytesIO(), dtype='short'), 'geosoft_grd')
    put('grids/geo_rot.grd', geosoft.write_grd(make_grid(rotation=30.0), io.BytesIO()), 'geosoft_grd')
    put('grids/geo_comp.grd', build_geosoft(list(g.values), g.nx, g.ny, g.dx, g.dy, g.x0, g.y0, compressed=True), 'geosoft_grd')
    col_order = [g.get(i, j) for i in range(g.nx) for j in range(g.ny)]
    put('grids/geo_kx.grd', build_geosoft(col_order, g.nx, g.ny, g.dx, g.dy, g.x0, g.y0, kx=-1), 'geosoft_grd')
    hdr = bytearray(512)
    struct.pack_into('<5i', hdr, 0, 2, 1, 3, 2, 1)
    struct.pack_into('<7d', hdr, 20, 5.0, 5.0, 10.0, 20.0, 0.0, 100.0, 2.0)
    put('grids/geo_int16.grd', bytes(hdr) + struct.pack('<6h', 0, 2, -32767, 4, -6, 32767), 'geosoft_grd')
    put('grids/g.gxf', geosoft.write_gxf(g, io.BytesIO()), 'gxf')
    put('grids/g_rot.gxf', geosoft.write_gxf(make_grid(rotation=-35.0), io.BytesIO()), 'gxf')
    put('grids/wide.gxf', geosoft.write_gxf(Grid2D(40, 2, 0, 0, 1, 1, [k / 7.0 for k in range(80)]), io.BytesIO()), 'gxf')
    for nm in ('GXF_SPEC_PLAIN', 'GXF_SPEC_COMPRESSED', 'GXF_SPEC_REPEAT'):
        put('grids/%s.gxf' % nm.lower(), text_block('test_geomodel_grids.py', nm), 'gxf')
    cases = {1: (3, 2, '1 2 3\n4 5 6\n'), -1: (2, 3, '1 4\n2 5\n3 6\n'), 2: (2, 3, '4 1\n5 2\n6 3\n'), -2: (3, 2, '4 5 6\n1 2 3\n'),
             3: (3, 2, '6 5 4\n3 2 1\n'), -3: (2, 3, '6 3\n5 2\n4 1\n'), 4: (2, 3, '3 6\n2 5\n1 4\n'), -4: (3, 2, '3 2 1\n6 5 4\n')}
    for sense, (points, rows, body) in cases.items():
        horizontal = sense in (1, -2, 3, -4)
        pt, rw = (10, 20) if horizontal else (20, 10)
        txt = ('#POINTS\n%d\n#ROWS\n%d\n#PTSEPARATION\n%d\n#RWSEPARATION\n%d\n#XORIGIN\n100\n#YORIGIN\n200\n#SENSE\n%d\n#GRID\n%s' % (points, rows, pt, rw, sense, body))
        put('grids/sense_%s.gxf' % sense, txt, 'gxf')

    def enc(v):
        i = int(round((v + 10.0) / 0.5))
        return chr(37 + i // 90) + chr(37 + i % 90)

    def count(n):
        return chr(37 + n // 90) + chr(37 + n % 90)
    body = '""%s%s!!%s\n""%s!!%s\n' % (count(3), enc(1.0), enc(2.5), count(4), enc(-7.5))
    put('grids/hand.gxf', '#TITLE\n"hand built"\n#POINTS\n5\n#ROWS\n2\n#PTSEPARATION\n2\n#RWSEPARATION\n3\n#XORIGIN\n10\n#YORIGIN\n20\n#TRANSFORM\n0.5 -10\n#GTYPE\n2\n#GRID\n' + body, 'gxf')
    put('grids/defaults.gxf', 'comment line\n#POINTS 2\n#ROWS\n2\nthis comment is skipped\n#GRID\n1 -1e12\n-1e+12 4\n', 'gxf')
    put('grids/units.gxf', '#POINTS\n1\n#ROWS\n1\n#UNIT_LENGTH\nft,0.3048\n#GRID\n7\n', 'gxf')
    put('grids/sq.asc', arcascii.write_asc(make_grid(25.0, 25.0), io.BytesIO()), 'arc_ascii')
    put('grids/center.asc', 'NCOLS 2\nNROWS 2\nXLLCENTER 10\nYLLCENTER 20\nCELLSIZE 5\nNODATA_value -1\n3 4\n1 -1\n', 'arc_ascii')
    put('grids/g.zmap', zmap.write_zmap(g, io.BytesIO()), 'zmap')
    put('grids/g2.zmap', zmap.write_zmap(g, io.BytesIO(), nodes_per_line=3, field_width=15, decimals=3), 'zmap')
    put('grids/small.zmap', '! comment\n@t, GRID, 5\n20, -9999.0, , 7, 1\n2, 3, 100.0, 120.0, 200.0, 220.0\n0.0, 0.0, 0.0\n@\n4 1\n5 2\n6 3\n', 'zmap')
    put('grids/touch.zmap', '@t, GRID, 5\n6, -999.0, , 1, 1\n2, 3, 100.0, 120.0, 200.0, 220.0\n0.0, 0.0, 0.0\n@\n   4.0   1.0\n   5.0   2.0\n-999.0   3.0\n', 'zmap')
    put('grids/g.irap', irap.write_irap(g, io.BytesIO()), 'irap')
    put('grids/g_rot.irap', irap.write_irap(make_grid(rotation=22.5), io.BytesIO()), 'irap')
    put('grids/small.irap', '-996 2 10.0 20.0\n100.0 120.0 200.0 220.0\n3 0.0 100.0 200.0\n0 0 0 0 0 0 0\n1 2 3 4 5 6\n', 'irap')
    put('grids/small.cps3', 'FSASCI 0 1 "Computed" 0 1.0E+30\nFSATTR 0 0\nFSLIMI 100.0 120.0 200.0 220.0 1.0 6.0\nFSNROW 2 3\nFSXINC 10.0 20.0\n-> a comment\n4 1 5 2\n6 3\n', 'cps3')
    put('grids/nulls.cps3', 'FSASCI 0 1 "Computed" 0 1.0E+30\nFSATTR 0 0\nFSLIMI 100.0 120.0 200.0 220.0 1.0 6.0\nFSNROW 2 3\nFSXINC 10.0 20.0\n-> a comment\n4 1.0E+30 5 2\n6 3\n', 'cps3')
    bm = BlockModel([1000.0, 2000.0, 480.0], [10.0, 20.0, 5.0], [3, 2, 4])
    vals = [float(k) - 3.5 for k in range(bm.n)]
    vals[5] = NAN
    bm.add_attribute('density', vals)
    bm.add_attribute('sus', [v * 2 for v in vals])
    mesh_b, models = ubc.write_ubc(bm, io.BytesIO(), {'density': io.BytesIO(), 'sus': io.BytesIO()})
    put('grids/ubc_density.mod', models['density'], 'ubc', summary=False)
    put('grids/ubc_sus.mod', models['sus'], 'ubc', summary=False)
    put('grids/ubc.msh', mesh_b, 'ubc', {'models': {'density': 'ubc_density.mod', 'sus': 'ubc_sus.mod'}, 'nodata': -99999.0})
    ne, nn, nz = 3, 2, 4
    values = [kz + 10 * ix + 100 * iy for iy in range(nn) for ix in range(ne) for kz in range(nz)]
    put('grids/code.mod', '\n'.join(str(v) for v in values) + '\n', 'ubc', summary=False)
    put('grids/code.msh', '3 2 4\n1000 2000 500\n3*10\n2*20\n4*5\n', 'ubc', {'models': {'code': 'code.mod'}})
    put('grids/explicit.mod', '\n'.join(str(k) for k in range(8)) + '\n', 'ubc', summary=False)
    put('grids/explicit.msh', '2 2 2\n0 0 100\n10 10\n10 10\n50 50\n', 'ubc', {'models': {'density': 'explicit.mod'}})
    ps = PointSet(role='points')
    ps.add(1.0, 2.0, 3.0, mag=10.5, line='100', line_type='Line')
    ps.add(1.5, 2.0, NAN, mag=NAN, line='100', line_type='Line')
    ps.add(5.0, 6.0, 7.0, mag=-1.25, line='5', line_type='Tie')
    put('grids/p.xyz', geosoft.write_xyz(ps, io.BytesIO()), 'geosoft_xyz')
    put('grids/sample.xyz', text_block('test_geomodel_grids.py', 'XYZ_SAMPLE'), 'geosoft_xyz')
    put('grids/abbrev.xyz', '/X,Y,Z,K\nL10\n1,2,3,4\nT20\n5,6,7,*\nBase\n8,9,10,11\n', 'geosoft_xyz')
    put('grids/nohdr.xyz', 'Line 1\n1 2 3 4\n', 'geosoft_xyz')
    ls = LineSet(role='lines')
    ls.add_polyline([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 0, 0)], {'flag': 1, 'name': 'pit'})
    ls.add_polyline([(5, 5, 1.5), (6, 7, 2.5)], {'flag': 0, 'name': 'break line'})
    put('grids/l.bln', surfer.write_bln(ls, io.BytesIO()), 'surfer_bln')
    put('grids/classic.bln', '3, 1\n1, 2\n3, 4\n1, 2\n', 'surfer_bln')

    # ---- meshes
    m = pyramid('Test Surface')
    put('meshes/a.obj', objfmt.write_obj(m, io.BytesIO()), 'obj')
    put('meshes/n.obj', objfmt.write_obj(m, io.BytesIO(), normals=True), 'obj')
    put('meshes/forms.obj', ("# comment\nmtllib x.mtl\no thing\nv 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvn 0 0 1\nusemtl red\ng quad\n"
                             "f 1/1/1 2/1/1 3/1/1 4/1/1\ng neg\nf -1 -2 -3\nl 1 2 3\nf 1//1 2//1 \\\n 3//1\nf 1 2 9\n"), 'obj')
    put('meshes/a.dxf', dxffmt.write_dxf([m, sample_lines(), sample_points()], io.BytesIO()), 'dxf')
    put('meshes/layers.dxf', dxffmt.write_dxf([m, pyramid('ignored')], io.BytesIO()), 'dxf')

    def tags(*pairs):
        return ''.join('%d\n%s\n' % (c, v) for c, v in pairs)
    body = tags((0, 'SECTION'), (2, 'ENTITIES'))
    body += tags((0, 'POLYLINE'), (8, 'PF'), (66, 1), (70, 64), (71, 4), (72, 2))
    for x, y, z in [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 1)]:
        body += tags((0, 'VERTEX'), (8, 'PF'), (10, x), (20, y), (30, z), (70, 192))
    body += tags((0, 'VERTEX'), (8, 'PF'), (10, 0), (20, 0), (30, 0), (70, 128), (71, 1), (72, 2), (73, 3), (74, 4))
    body += tags((0, 'VERTEX'), (8, 'PF'), (10, 0), (20, 0), (30, 0), (70, 128), (71, -1), (72, 3), (73, 4))
    body += tags((0, 'SEQEND'))
    body += tags((0, 'POLYLINE'), (8, 'PM'), (66, 1), (70, 16), (71, 2), (72, 3))
    for i in range(2):
        for j in range(3):
            body += tags((0, 'VERTEX'), (8, 'PM'), (10, i), (20, j), (30, i * j), (70, 64))
    body += tags((0, 'SEQEND'))
    body += tags((0, 'LWPOLYLINE'), (5, '2A'), (330, '1F'), (100, 'AcDbEntity'), (8, 'LW'), (100, 'AcDbPolyline'), (90, 3), (70, 1), (38, 25.0),
                 (10, 0), (20, 0), (10, 4), (20, 0), (10, 4), (20, 3))
    body += tags((0, 'SOLID'), (8, 'S'), (10, 0), (20, 0), (30, 0), (11, 1), (21, 0), (31, 0), (12, 0), (22, 1), (32, 0), (13, 1), (23, 1), (33, 0))
    body += tags((0, 'LINE'), (8, 'L'), (10, 0), (20, 0), (30, 0), (11, 1), (21, 1), (31, 1))
    body += tags((0, 'POLYLINE'), (8, 'L'), (66, 1), (70, 0), (10, 0), (20, 0), (30, 7.5))
    body += tags((0, 'VERTEX'), (8, 'L'), (10, 0), (20, 0), (30, 0))
    body += tags((0, 'VERTEX'), (8, 'L'), (10, 2), (20, 0), (30, 0))
    body += tags((0, 'SEQEND'))
    body += tags((0, 'LWPOLYLINE'), (8, 'ARC'), (70, 0), (10, 0), (20, 0), (42, 1.0), (10, 2), (20, 0))
    body += tags((0, 'TEXT'), (8, 'T'), (1, 'hi'), (10, 0), (20, 0), (30, 0))
    body += tags((0, 'ENDSEC'), (0, 'EOF'))
    put('meshes/hand.dxf', body, 'dxf')
    m2 = pyramid('Surf One')
    m2.attributes['grade'] = {'location': 'vertices', 'values': [1.0, NAN, 3.0, 4.0, 5.0]}
    m2.attributes['zone'] = {'location': 'faces', 'values': [1, 1, 2, 2, 3, 3]}
    put('meshes/m.ts', gocadfmt.write_gocad(m2, io.BytesIO()), 'gocad_ts')
    put('meshes/d.ts', gocadfmt.write_gocad(m2, io.BytesIO(), zpositive='Depth'), 'gocad_ts')
    put('meshes/l.pl', gocadfmt.write_gocad(sample_lines(), io.BytesIO()), 'gocad_ts')
    psv = sample_points()
    psv.attributes['au'] = [0.5, 1.5]
    put('meshes/p.vs', gocadfmt.write_gocad(psv, io.BytesIO()), 'gocad_ts')
    concat = """GOCAD TSurf 1
HEADER {
name:depth surf
*solid*color:1 0 0 1
}
GOCAD_ORIGINAL_COORDINATE_SYSTEM
NAME Default
AXIS_NAME "X" "Y" "Z"
AXIS_UNIT "m" "m" "m"
ZPOSITIVE Depth
END_ORIGINAL_COORDINATE_SYSTEM
PROPERTIES grade vec
ESIZES 1 2
NO_DATA_VALUES -99999 -99999
TFACE
PVRTX 1 0 0 100 1.5 0 1
PVRTX 2 1 0 100 -99999 2 3
PVRTX 3 0 1 110 2.5 4 5
ATOM 4 1
TRGL 1 2 3
TRGL 2 4 3
BSTONE 1
BORDER 5 1 2
END
GOCAD VSet 1
HEADER {name:pts
*atoms*color:0 0 1 1
}
PROPERTIES au
SUBVSET
PVRTX 1 10 20 30 0.5
PVRTX 2 11 21 31 0.7
END
GOCAD PLine 1
HEADER {name:implicit}
ILINE
VRTX 1 0 0 0
VRTX 2 1 0 0
VRTX 3 2 0 0
ILINE
VRTX 4 5 5 5
VRTX 5 6 5 5
END
GOCAD PLine 1
HEADER {name:loop}
ILINE
VRTX 1 0 0 0
VRTX 2 1 0 0
VRTX 3 1 1 0
SEG 1 2
SEG 2 3
SEG 3 1
END
GOCAD Voxet 1
HEADER {name:vox}
END
"""
    put('meshes/concat.ts', concat, 'gocad_ts')
    put('meshes/lf.msh', lfmsh.write_msh(pyramid('lf'), io.BytesIO()), 'lf_msh')
    hdr2 = b'%%ARANZ-1.0\n\n[index]\nTri Integer 3 1;\nLocation Double 3 3;\nColour Integer 1 3;\nFaceTag Double 1 1;\n\n[binary]'
    payload = (struct.pack('<3i', 15732735, 1115938331, 1072939210) + struct.pack('<3i', 0, 1, 2) + struct.pack('<9d', 0, 0, 0, 1, 0, 0, 0, 1, 0)
               + struct.pack('<3i', 7, 8, 9) + struct.pack('<d', 2.5))
    put('meshes/lf_extra.msh', hdr2 + payload, 'lf_msh')
    put('meshes/lf_extra_nl.msh', hdr2 + b'\r\n' + payload, 'lf_msh')

    # ---- tables
    COLLAR = 'HOLE_ID,EAST,NORTH,ELEV,EOH,Prospect\nDH1,1000,2000,300,100,North\nDH2,1050,2000,305,50,North\n'
    SURVEY_NEG = 'holeid,depth,azimuth,dip\nDH1,0,90,-60\nDH1,50,95,-58\nDH1,100,100,-55\nDH2,0,0,-90\n'
    ASSAY = 'holeid,from,to,Au,Cu_pct,comment\nDH1,0,10,0.5,0.1,ok\nDH1,10,20,1.5,,high\nDH2,0,5,0.1,0.2,\n'
    LITH = 'BHID,Depth_From,Depth_To,Lith\nDH1,0,15,GRN\nDH1,15,100,SCH\nDH9,0,1,XX\n'
    put('tables/survey.csv', SURVEY_NEG, 'csv_drillholes', summary=False)
    put('tables/assay.csv', ASSAY, 'csv_drillholes', summary=False)
    put('tables/lith.csv', LITH, 'csv_drillholes', summary=False)
    put('tables/collar.csv', COLLAR, 'csv_drillholes', {'survey': 'survey.csv', 'intervals': {'assay': 'assay.csv', 'lith': 'lith.csv'}, 'negativeDipDown': True})
    put('tables/collar_noflag.csv', COLLAR, 'csv_drillholes', {'survey': 'survey.csv'})
    put('tables/collar_only.csv', COLLAR, 'csv_drillholes', {})
    pts = PointSet(name='pts', attributes={'au': [], 'lith': []})
    pts.add(1, 2, 3, au=0.5, lith='qtz')
    pts.add(4, 5, 6, au=None, lith='sch')
    put('tables/pts.csv', tables.write_points_csv(pts, io.BytesIO()), 'csv_points')
    put('tables/pts_lf.csv', tables.write_points_csv(pts, io.BytesIO(), leapfrog=True, columns=['lith']), 'csv_points')
    put('tables/syn.csv', 'Easting;Northing;RL;Au_ppm;Lith\n500000;4000000;1200.5;0.5;qtz\n500010;4000010;1201;;sch\n', 'csv_points')
    put('tables/noz.csv', 'X,Y,name\n1,2,a\n3,4,b\n', 'csv_points')
    put('tables/explicit.csv', 'lon,lat,h,v\n1,2,3,4\n', 'csv_points', {'x': 'lon', 'y': 'lat', 'z': 'h'})
    put('tables/quoted.csv', 'x,y,z,label\n1,2,3,"a, b"\n4,5,6,"say ""hi"""\n', 'csv_points')
    st = tables.read_structural_csv(b'x,y,z,strike,dip,polarity\n1,2,3,45,30,1\n4,5,6,300,80,\n')
    put('tables/st.csv', tables.write_structural_csv(st, io.BytesIO()), 'csv_structural')
    put('tables/strike.csv', 'x,y,z,strike,dip,polarity,station\n1,2,3,45,30,1,S1\n4,5,6,300,80,,S2\n', 'csv_structural')
    put('tables/dipdir.csv', 'x,y,z,dip_direction,strike,dip\n1,2,3,135,45,30\n', 'csv_structural')
    bm2 = BlockModel([1000, 2000, 100], [10, 10, 5], [3, 2, 2], name='bm')
    au = [float(i) for i in range(bm2.n)]
    au[0] = NAN
    au[bm2.n - 1] = NAN
    bm2.add_attribute('au', au)
    bm2.add_attribute('rock', ['ox' if i % 2 else '' for i in range(bm2.n)], kind='text')
    tables.write_blockmodel_csv(bm2, os.path.join(out, 'tables', 'bm.csv'))
    fixtures['tables/bm.csv'] = {'format': 'csv_blockmodel', 'opts': {}, 'summary': summary_for('csv_blockmodel', os.path.join(out, 'tables', 'bm.csv'))}
    fixtures['tables/bm.csv.txt'] = {'format': 'csv_blockmodel', 'opts': {}}
    put('tables/bm_nohdr.csv', tables.write_blockmodel_csv(bm2, io.BytesIO(), embedded_header=False, skip_empty=False), 'csv_blockmodel')
    rows = ['XC,YC,ZC,au']
    for i in range(bm2.n):
        if i == 4:
            continue
        x, y, z = bm2.centroid(*bm2.ijk(i))
        rows.append('%g,%g,%g,%d' % (x, y, z, i))
    put('tables/centroids.csv', '\n'.join(rows), 'csv_blockmodel')
    put('tables/centroids_arg.csv', '\n'.join(rows), 'csv_blockmodel', {'blockSize': [10, 10, 5]})
    put('tables/offlattice.csv', 'x,y,z,dx,dy,dz,au\n5,5,2.5,10,10,5,1\n15,5,2.5,10,10,5,2\n12,5,2.5,10,10,5,3\n15,5,2.5,10,10,5,9\n', 'csv_blockmodel')
    rot = BlockModel([1000, 2000, 100], [10, 10, 5], [3, 2, 1], azimuth=30, name='rot')
    rot.add_attribute('v', list(range(rot.n)))
    put('tables/rot.csv', tables.write_blockmodel_csv(rot, io.BytesIO(), sidecar=False), 'csv_blockmodel')
    put('tables/lf_hdr.csv', '# Block model exported from somewhere\nModel name: Demo\nx,y,z,dx,dy,dz,grade\n5,5,2.5,10,10,5,1.5\n15,5,2.5,10,10,5,2.5\n', 'csv_blockmodel')

    # ---- seismic / logs
    put('seismic/a.sgy', segy.write_segy(traces(), io.BytesIO(), 2000, coords=coords()), 'segy')
    tr = [[round(v * 100) for v in t] for t in traces(3, 20)]
    for fmt in (1, 2, 3, 5, 8):
        vals2 = [[max(-127, min(127, v)) for v in t] for t in tr] if fmt == 8 else tr
        put('seismic/f%d.sgy' % fmt, segy.write_segy(vals2, io.BytesIO(), 1000, format_code=fmt), 'segy')
    put('seismic/le.sgy', segy.write_segy(tr, io.BytesIO(), 1000, endian='little'), 'segy')
    bb = bytearray(segy.write_segy(tr, io.BytesIO(), 1000, endian='little'))
    struct.pack_into('<I', bb, 3200 + 96, 16909060)
    put('seismic/le_word.sgy', bytes(bb), 'segy')
    put('seismic/eb.sgy', segy.write_segy(traces(2, 10), io.BytesIO(), 500, text='HELLO\nWORLD', text_encoding='ebcdic'), 'segy')
    sc = bytearray(segy.write_segy(traces(2, 10), io.BytesIO(), 500, coords=[(123456, 654321), (1, 2)]))
    struct.pack_into('>h', sc, 3600 + 70, -100)
    struct.pack_into('>h', sc, 3600 + 68, 10)
    struct.pack_into('>i', sc, 3600 + 40, 55)
    put('seismic/scaled.sgy', bytes(sc), 'segy')
    base = bytearray(segy.write_segy(traces(2, 10), io.BytesIO(), 1000))
    struct.pack_into('>h', base, 3200 + 302, 0)
    short = bytearray(240)
    struct.pack_into('>H', short, 114, 4)
    struct.pack_into('>H', short, 116, 1000)
    put('seismic/var.sgy', bytes(base) + bytes(short) + struct.pack('>4f', 1, 2, 3, 4), 'segy')
    ext = bytearray(segy.write_segy(traces(2, 10), io.BytesIO(), 1000))
    struct.pack_into('>h', ext, 3200 + 304, 1)
    put('seismic/ext.sgy', bytes(ext[:3600]) + b'C 1 EXTENDED'.ljust(3200) + bytes(ext[3600:]), 'segy')
    d = segy.read_segy(os.path.join(out, 'seismic', 'a.sgy'))
    img = segy.section_image(d)
    img2 = segy.section_image(d, z_top=1200.0, z_bottom=900.0, clip_pct=100)
    fixtures['seismic/a.sgy']['section_image'] = {'width': img['width'], 'height': img['height'], 'gray': list(img['gray']), 'p1': img['p1'], 'p2': img['p2'],
                                                 'z_top': img['z_top'], 'z_bottom': img['z_bottom'], 'clip': img['clip'], 'warnings': img['warnings'],
                                                 'custom': {'z_top': img2['z_top'], 'z_bottom': img2['z_bottom'], 'warnings': img2['warnings'], 'gray': list(img2['gray'])}}
    fixtures['ibm'] = {'words': [(v, segy.float_to_ibm(v)) for v in (1.0, -118.625, 0.15625, 3.14159, 1e-7, 123456.789, -0.001, 7e20, 2.0 ** -70, 65536.0, 0.0)],
                       'floats': [(w, segy.ibm_to_float(w)) for w in (0x41100000, 0xC276A000, 0x40280000, 0)]}
    put('las/wrap.las', text_block('test_geomodel_seismic.py', 'WRAPPED'), 'las')
    put('las/las3.las', text_block('test_geomodel_seismic.py', 'LAS3'), 'las')
    dup = ("~V\nVERS. 2.0:\nWRAP. NO:\n~W\nSTRT.M 100:\nSTOP.M 101:\nSTEP.M 0.5:\nNULL. -9999:\nWELL. X-1: well\n~C\nDEPT.M:\nGR.API:\nGR.API: second gamma\n"
           "~P\nMUD. KCL: Mud\n~O\nfree text\nmore\n~A\n100 1 2\n100.5 -9999 3\n101 5\n")
    put('las/dup.las', dup, 'las')
    put('las/out.las', las.write_las(las.read_las(dup.encode()), io.BytesIO()), 'las')
    dw = las.read_las(os.path.join(out, 'las', 'wrap.las'))
    fixtures['las/wrap.las']['intervals'] = {'default': J(las.las_to_intervals(dw, 'W1')), 'step2': J(las.las_to_intervals(dw, 'W1', step=2.0)),
                                             'dt_only': J(las.las_to_intervals(dw, 'W1', curves=['DT']))}

    # ---- omf
    prj = build_project()
    put('omf/kit.omf', write_omf2(prj, io.BytesIO()), 'omf2')
    put('omf/kit_none.omf', write_omf2(prj, io.BytesIO(), compression='none'), 'omf2')
    put('omf/two.omf', write_omf2([prj.objects[0], prj.objects[2]], io.BytesIO(), name='two', prerelease=''), 'omf2')
    put('omf/kit_v09.omf', write_omf1(prj, io.BytesIO()), 'omf1')
    fixtures['omf/project_summary'] = {'format': 'project', 'summary': summarize(prj)}

    # ---- reference samples (absolute paths)
    refs = {SAMPLE_V2: 'omf2', SAMPLE_V09: 'omf1', os.path.join(REF, 'dxf', 'min_r12.dxf'): 'dxf',
            os.path.join(REF, 'omfrust', 'omf-python', 'tests', 'data', 'one_of_everything.omf'): 'omf2'}
    for nm in ('cfm.ts', 'input_3tri_all_props.ts', 'input_3tri_node_props.ts', 'input_small_TFACE.ts', 'pynoddy.pl'):
        refs[os.path.join(REF, 'gocad', nm)] = 'gocad_ts'
    sh = '/tmp/gm_silverhills'
    if os.path.isdir(sh):
        for nm in sorted(os.listdir(sh)):
            ext = os.path.splitext(nm)[1].lower()
            if ext in ('.md', '.json'):
                continue
            fmt = formats.sniff(os.path.join(sh, nm))
            if nm.endswith('-omf09.omf'):
                fmt = 'omf1'
            if fmt:
                refs[os.path.join(sh, nm)] = fmt
    for path, fmt in refs.items():
        if os.path.exists(path):
            fixtures[path] = {'format': fmt, 'opts': {}, 'summary': summary_for(fmt, path)}

    import random
    random.seed(1)
    reprs = [random.uniform(-1e6, 1e6) for _ in range(100)] + [random.random() * 10 ** random.randint(-30, 30) for _ in range(200)]
    reprs += [1e16, 1e15, 123.0, 0.1, 1 / 3, 2 ** -1074, 1.7976931348623157e308, -0.0, 5e-324, 1e-5, 9.5, 1e21, 1e22, 123456789.125]
    expected = {'fixtures': fixtures, 'repr_vectors': [(v, repr(v), '%g' % v, '%.10g' % v, '%.7f' % v if abs(v) < 1e20 else None) for v in reprs],
                'sample_v2': SAMPLE_V2, 'sample_v09': SAMPLE_V09}
    with open(os.path.join(out, 'expected.json'), 'w', encoding='utf-8') as fh:
        json.dump(expected, fh)
    print('wrote %d fixtures to %s' % (len(fixtures), out))


def main(argv):
    if len(argv) >= 2 and argv[1] == 'summarize':
        fmt, path = argv[2], argv[3]
        opts = json.loads(argv[4]) if len(argv) > 4 else {}
        print(json.dumps(summary_for(fmt, path, opts)))
        return 0
    out = argv[2] if len(argv) > 2 else '/tmp/gm_fixtures'
    gen(out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
