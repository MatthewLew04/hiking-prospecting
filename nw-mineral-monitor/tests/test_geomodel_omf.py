"""Tests for the Open Mining Format readers/writers (geomodel.formats.omf1 /
omf2) and the Parquet + Thrift layers underneath them.

External validators are used when present and skipped otherwise:

* pyarrow                 — cross-checks every Parquet file we write;
* the ``omf2`` wheel      — omf-rust's Python bindings, the reference OMF v2
                            reader (and its OMF v0.9 converter);
* omf 1.0.1               — the reference OMF v0.9 reader, run through the
                            interpreter tests/gm_ref.py points at when that
                            venv exists.

tools/fetch_gm_refs.py installs all of them; gm_ref is the only module that
knows where they landed.
"""
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gzip
import io
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import zipfile

from geomodel.model import (Grid2D, Mesh, LineSet, PointSet, BlockModel, Project,
                            farray, utm_crs)
from geomodel import formats
from geomodel.formats import thrift_compact as tc
from geomodel.formats import parquet_lite as pl
from geomodel.formats.parquet_lite import Column, Group
from geomodel.formats.omf1 import read_omf1, write_omf1
from geomodel.formats.omf2 import read_omf2, write_omf2, convert_omf1_to_omf2

from gm_ref import REF, SAMPLE_V2, SAMPLE_V09, omf1_python   # noqa: E402

OMF1_PYTHON = omf1_python()

NAN = float('nan')


def _pyarrow():
    """pyarrow.parquet or None (threads pinned to 1: the default pool can
    abort at interpreter exit in some sandboxes)."""
    try:
        import pyarrow as pa
        pa.set_cpu_count(1)
        pa.set_io_thread_count(1)
        import pyarrow.parquet as pq
        return pq
    except ImportError:
        return None


def _omf2_wheel():
    try:
        import omf2
        return omf2
    except ImportError:
        return None


_OMF1_OK = None


def _omf1_reference_available():
    global _OMF1_OK
    if _OMF1_OK is None:
        _OMF1_OK = False
        if OMF1_PYTHON.exists():
            try:
                r = subprocess.run([str(OMF1_PYTHON), '-c', 'import omf, numpy'], capture_output=True, timeout=60)
                _OMF1_OK = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                _OMF1_OK = False
    return _OMF1_OK


def _eq(a, b, tol=1e-9):
    """Sequence equality treating NaN == NaN and None == NaN."""
    a, b = list(a), list(b)
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        xn = x is None or (isinstance(x, float) and x != x)
        yn = y is None or (isinstance(y, float) and y != y)
        if xn or yn:
            if xn != yn:
                return False
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            if abs(x - y) > tol:
                return False
        elif x != y:
            return False
    return True


def build_project():
    """One of every supported object with every attribute type."""
    prj = Project(name='Test kit', crs=utm_crs(12, True))
    prj.metadata['description'] = 'kit description'
    ps = PointSet([[500000, 4100000, 1500], [500010, 4100000, 1510], [500020, 4100010, 1520]],
                  name='samples', color=[255, 0, 0], role='samples',
                  attributes={'Au_ppm': [1.5, None, 3.25], 'lith': ['qtz', None, 'sch'],
                              'ok': [True, False, None],
                              'when': ['2020-01-01T00:00:00Z', None, '2021-06-15T12:30:00Z']})
    ps.metadata['datetime_attributes'] = ['when']
    ps.metadata['categories'] = {'lith': {'names': ['qtz', 'sch'],
                                          'colors': [[255, 0, 0, 255], [0, 0, 255, 255]], 'index': False}}
    ps.metadata['vector_attributes'] = {'vec': ['vx', 'vy', 'vz']}
    ps.attributes['vx'] = [1.0, 0.0, NAN]
    ps.attributes['vy'] = [0.0, 1.0, NAN]
    ps.attributes['vz'] = [0.0, 0.0, NAN]
    ps.metadata['color_attributes'] = ['col']
    ps.attributes['col'] = [[1, 2, 3, 255], None, [10, 20, 30, 40]]
    ps.metadata['attribute_units'] = {'Au_ppm': 'ppm'}
    ps.metadata['colormaps'] = {'Au_ppm': {'type': 'continuous', 'range': [0.0, 5.0],
                                           'gradient': [[0, 0, 0, 255], [255, 255, 255, 255]]}}
    prj.add(ps)
    ls = LineSet(name='workings', color=[0, 255, 0], role='workings')
    ls.add_polyline([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
    ls.add_polyline([(5, 5, 5), (6, 6, 6)])
    ls.attributes = {'seglen': {'location': 'segments', 'values': farray([1.0, 1.0, math.sqrt(3)])}}
    prj.add(ls)
    m = Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 1]], [[0, 1, 2], [1, 3, 2]], name='surf',
             color=[0, 0, 255], role='contact',
             attributes={'elev': {'location': 'vertices', 'values': farray([0, 0, 0, 1])},
                         'face_id': {'location': 'faces', 'values': farray([7, 8])}})
    m.opacity = 0.5
    prj.add(m)
    prj.add(Grid2D(3, 2, 100.0, 200.0, 10.0, 20.0, [1, 2, 3, 4, NAN, 6], rotation=30.0,
                   name='topo', role='topography'))
    prj.add(Grid2D(2, 2, 0.0, 0.0, 1.0, 1.0, [10, 11, 12, 13], name='mag', role='property', units='nT'))
    bm = BlockModel([1000, 2000, 300], [10, 10, 5], [2, 3, 2], azimuth=45.0, name='blocks',
                    attributes={'grade': [float(i) if i % 5 else NAN for i in range(12)]})
    bm.add_attribute('rock', ['a', 'b'] * 6, kind='text')
    bm.add_attribute('flag', [True, False, None] * 4, kind='boolean')
    prj.add(bm)
    return prj


def check_roundtrip(test, src, back, alpha_kept=True, opacity_kept=True, units_kept=True):
    """Assert that ``back`` (re-read) matches ``src`` (built) object by object."""
    kinds = [o.kind for o in back.objects]
    test.assertEqual(kinds, ['points', 'lineset', 'mesh', 'grid2d', 'grid2d', 'blockmodel'])
    ps, ls, m, g, gp, bm = back.objects
    s_ps, s_ls, s_m, s_g, s_gp, s_bm = src.objects
    # points
    test.assertEqual(ps.name, 'samples')
    test.assertEqual(ps.color, [255, 0, 0])
    test.assertTrue(_eq(ps.xyz, s_ps.xyz))
    test.assertTrue(_eq(ps.attributes['Au_ppm'], [1.5, NAN, 3.25]))
    test.assertEqual(ps.attributes['lith'], ['qtz', None, 'sch'])
    test.assertEqual(ps.attributes['ok'], [True, False, None])
    test.assertEqual(ps.attributes['when'], ['2020-01-01T00:00:00Z', None, '2021-06-15T12:30:00Z'])
    test.assertTrue(_eq(ps.attributes['vec_x'], [1.0, 0.0, NAN]))
    test.assertTrue(_eq(ps.attributes['vec_z'], [0.0, 0.0, NAN]))
    test.assertEqual(ps.metadata['vector_attributes'], {'vec': ['vec_x', 'vec_y', 'vec_z']})
    test.assertEqual(ps.metadata['categories']['lith']['names'], ['qtz', 'sch'])
    test.assertEqual(ps.metadata['categories']['lith']['colors'], [[255, 0, 0, 255], [0, 0, 255, 255]])
    test.assertEqual(ps.metadata['boolean_attributes'], ['ok'])
    test.assertEqual(ps.metadata['datetime_attributes'], ['when'])
    test.assertEqual(ps.metadata['color_attributes'], ['col'])
    test.assertEqual(ps.attributes['col'][0], [1, 2, 3, 255])
    if alpha_kept:
        test.assertEqual(ps.attributes['col'], [[1, 2, 3, 255], None, [10, 20, 30, 40]])
    test.assertEqual(ps.metadata['colormaps']['Au_ppm']['range'], [0.0, 5.0])
    if units_kept:
        test.assertEqual(ps.metadata['attribute_units'], {'Au_ppm': 'ppm'})
    # lines
    test.assertTrue(_eq(ls.vertices, s_ls.vertices))
    test.assertEqual(list(ls.segments), list(s_ls.segments))
    test.assertEqual(len(ls.parts), 2)
    test.assertTrue(_eq(ls.attributes['seglen']['values'], [1.0, 1.0, math.sqrt(3)]))
    test.assertEqual(ls.attributes['seglen']['location'], 'segments')
    # mesh
    test.assertTrue(_eq(m.vertices, s_m.vertices))
    test.assertEqual(list(m.triangles), [0, 1, 2, 1, 3, 2])
    test.assertEqual(m.color, [0, 0, 255])
    if opacity_kept:
        test.assertAlmostEqual(m.opacity, 0.5, places=2)
    test.assertTrue(_eq(m.attributes['elev']['values'], [0, 0, 0, 1]))
    test.assertEqual(m.attributes['face_id']['location'], 'faces')
    test.assertTrue(_eq(m.attributes['face_id']['values'], [7, 8]))
    # grid
    test.assertEqual((g.nx, g.ny), (3, 2))
    test.assertAlmostEqual(g.x0, 100.0)
    test.assertAlmostEqual(g.y0, 200.0)
    test.assertAlmostEqual(g.dx, 10.0)
    test.assertAlmostEqual(g.dy, 20.0)
    test.assertAlmostEqual(g.rotation, 30.0, places=6)
    test.assertTrue(_eq(g.values, [1, 2, 3, 4, NAN, 6]))
    # property grid
    test.assertEqual(gp.role, 'property')
    test.assertEqual(gp.name, 'mag')
    test.assertTrue(_eq(gp.values, [10, 11, 12, 13]))
    if units_kept:
        test.assertEqual(gp.units, 'nT')
    # block model
    test.assertEqual(bm.origin, [1000.0, 2000.0, 300.0])
    test.assertEqual(bm.block_size, [10.0, 10.0, 5.0])
    test.assertEqual(bm.count, [2, 3, 2])
    test.assertAlmostEqual(bm.azimuth, 45.0, places=6)
    test.assertTrue(_eq(bm.attributes['grade']['values'], s_bm.attributes['grade']['values']))
    test.assertEqual(bm.attributes['rock'], {'type': 'text', 'values': ['a', 'b'] * 6})
    test.assertEqual(bm.attributes['flag'], {'type': 'boolean', 'values': [True, False, None] * 4})
    for o in back.objects:
        test.assertIn(o.provenance.get('format'), ('omf1', 'omf2'))
        test.assertEqual(o.provenance.get('element'), o.name if getattr(o, 'role', '') != 'property' else 'mag')


# ===================================================================== thrift
class ThriftCompactTests(unittest.TestCase):
    def test_varint_zigzag(self):
        for n in (0, 1, 127, 128, 300, 2 ** 31, 2 ** 40):
            v, pos = tc.decode_varint(tc.encode_varint(n), 0)
            self.assertEqual(v, n)
        for n in (0, -1, 1, -2, 2, 2 ** 31 - 1, -2 ** 31, 2 ** 62):
            self.assertEqual(tc.zigzag_decode(tc.zigzag_encode(n)), n)

    def test_struct_roundtrip(self):
        inner = [(1, 'i32', 5), (2, 'bool', True), (3, 'bool', False), (4, 'double', 2.5),
                 (5, 'binary', 'héllo'), (6, 'list:i64', [1, -2, 3]), (7, 'byte', -3),
                 (20, 'i16', 1000), (40, 'list:struct', [[(1, 'i32', 1)], [(1, 'i32', 2)]])]
        data = tc.encode_struct([(1, 'struct', inner), (99, 'i64', -7), (100, 'list:binary', [b'a', 'b'])])
        out, pos = tc.decode_struct(data, 0)
        self.assertEqual(pos, len(data))
        self.assertEqual(out[99], -7)
        self.assertEqual(out[100], [b'a', b'b'])
        s = out[1]
        self.assertEqual(s[1], 5)
        self.assertIs(s[2], True)
        self.assertIs(s[3], False)
        self.assertEqual(s[4], 2.5)
        self.assertEqual(s[5], 'héllo'.encode('utf-8'))
        self.assertEqual(s[6], [1, -2, 3])
        self.assertEqual(s[7], -3)
        self.assertEqual(s[20], 1000)
        self.assertEqual(s[40], [{1: 1}, {1: 2}])

    def test_long_list_and_empty_struct(self):
        data = tc.encode_struct([(1, 'list:i32', list(range(40))), (2, 'struct', [])])
        out, _ = tc.decode_struct(data, 0)
        self.assertEqual(out[1], list(range(40)))
        self.assertEqual(out[2], {})


# ==================================================================== parquet
class RleTests(unittest.TestCase):
    def test_hybrid_roundtrip(self):
        rnd = random.Random(7)
        for width in (1, 2, 3, 5, 8, 12):
            top = (1 << width) - 1
            for n in (0, 1, 7, 8, 9, 63, 64, 65, 1000):
                vals = [rnd.randint(0, top) for _ in range(n)]
                # inject runs
                for k in range(0, n, 37):
                    vals[k:k + 20] = [vals[k]] * len(vals[k:k + 20])
                enc = pl.rle_hybrid_encode(vals, width)
                dec, pos = pl.rle_hybrid_decode(enc, 0, len(enc), width, n)
                self.assertEqual(dec, vals, (width, n))
                self.assertEqual(pos, len(enc))

    def test_all_same_is_one_run(self):
        enc = pl.rle_hybrid_encode([1] * 100000, 1)
        self.assertLess(len(enc), 8)


class ParquetTests(unittest.TestCase):
    def fields(self, n=5):
        return [
            Column('d', 'double', [1.5, None, 3.0, NAN, 5.0][:n], optional=True),
            Column('f', 'float', [0.5, 1.5, 2.5, 3.5, 4.5][:n]),
            Column('i32', 'int32', [0, 1, 2, 2 ** 31 - 1, -5][:n]),
            Column('u32', 'int32', [0, 1, 2, 2 ** 32 - 1, 7][:n], logical='uint32'),
            Column('i64', 'int64', [None, 2 ** 40, -3, 4, 5][:n], optional=True),
            Column('s', 'byte_array', ['a', None, 'ccc', '', 'ünï'][:n], optional=True, logical='string'),
            Column('b', 'boolean', [True, None, False, True, True][:n], optional=True),
            Column('bb', 'boolean', [True, False, False, True, True][:n]),
            Group('vector', [Column('x', 'double', [1, 2, 3, 4, 5][:n]), Column('y', 'double', [6, 7, 8, 9, 10][:n])],
                  present=[True, False, True, True, False][:n]),
            Column('ts', 'int64', [0, None, 10 ** 12, 5, 6][:n], optional=True, logical='timestamp_micros'),
        ]

    def check(self, pf, n=5):
        self.assertEqual(pf.num_rows, n)
        self.assertTrue(_eq(pf.columns['d'], [1.5, None, 3.0, NAN, 5.0][:n]))
        self.assertEqual(pf.columns['f'], [0.5, 1.5, 2.5, 3.5, 4.5][:n])
        self.assertEqual(pf.columns['i32'], [0, 1, 2, 2 ** 31 - 1, -5][:n])
        self.assertEqual(pf.columns['u32'], [0, 1, 2, 2 ** 32 - 1, 7][:n])
        self.assertEqual(pf.columns['i64'], [None, 2 ** 40, -3, 4, 5][:n])
        self.assertEqual(pf.columns['s'], ['a', None, 'ccc', '', 'ünï'][:n])
        self.assertEqual(pf.columns['b'], [True, None, False, True, True][:n])
        self.assertEqual(pf.columns['bb'], [True, False, False, True, True][:n])
        self.assertEqual(pf.columns['vector.x'], [1, None, 3, 4, None][:n])
        self.assertEqual(pf.columns['vector.y'], [6, None, 8, 9, None][:n])
        self.assertEqual(pf.columns['ts'], [0, None, 10 ** 12, 5, 6][:n])
        self.assertEqual(pf.leaf('ts').logical, ('timestamp', 'micros', True))
        self.assertEqual(pf.leaf('u32').logical, ('integer', 32, False))
        self.assertEqual(pf.leaf('s').logical, ('string',))

    def test_write_read_roundtrip(self):
        for comp in ('gzip', 'none'):
            for rg in (1024 * 1024, 2, 1):
                data = pl.write_parquet(self.fields(), compression=comp, row_group_size=rg)
                self.assertTrue(data.startswith(b'PAR1') and data.endswith(b'PAR1'))
                self.check(pl.read_parquet(data))

    def test_empty_table(self):
        data = pl.write_parquet([Column('scalar', 'double', [])])
        pf = pl.read_parquet(data)
        self.assertEqual(pf.num_rows, 0)
        self.assertEqual(pf.columns['scalar'], [])

    def test_schema_text(self):
        pf = pl.read_parquet(pl.write_parquet([Column('scalar', 'double', [1.0])]))
        self.assertEqual(pf.schema_text(), 'message schema {\n  REQUIRED DOUBLE scalar;\n}')
        pf = pl.read_parquet(pl.write_parquet([Group('vector', [Column('x', 'float', [1.0]), Column('y', 'float', [2.0])])]))
        self.assertEqual(pf.schema_text(), 'message schema {\n  OPTIONAL group vector {\n    REQUIRED FLOAT x;\n    REQUIRED FLOAT y;\n  }\n}')

    def test_large_column_many_pages(self):
        n = 70000
        vals = [float(i) if i % 3 else None for i in range(n)]
        data = pl.write_parquet([Column('number', 'double', vals, optional=True)], row_group_size=20000)
        pf = pl.read_parquet(data)
        self.assertEqual(len(pf.row_groups), 4)
        self.assertEqual(pf.columns['number'], vals)

    def test_bad_input(self):
        with self.assertRaises(pl.ParquetError):
            pl.read_parquet(b'not a parquet file at all')
        with self.assertRaises(pl.ParquetError):
            pl.write_parquet([Column('a', 'double', [1.0]), Column('b', 'double', [1.0, 2.0])])

    # -- cross checks with pyarrow
    def test_pyarrow_reads_ours(self):
        pq = _pyarrow()
        if pq is None:
            self.skipTest('optional cross-validator unavailable: pyarrow not installed')
        for comp in ('gzip', 'none'):
            data = pl.write_parquet(self.fields(), compression=comp, row_group_size=2)
            t = pq.read_table(io.BytesIO(data), use_threads=False).to_pydict()
            self.assertTrue(_eq(t['d'], [1.5, None, 3.0, NAN, 5.0]))
            self.assertEqual(t['u32'], [0, 1, 2, 2 ** 32 - 1, 7])
            self.assertEqual(t['s'], ['a', None, 'ccc', '', 'ünï'])
            self.assertEqual(t['b'], [True, None, False, True, True])
            self.assertEqual(t['vector'], [{'x': 1.0, 'y': 6.0}, None, {'x': 3.0, 'y': 8.0}, {'x': 4.0, 'y': 9.0}, None])
            self.assertEqual(t['i64'], [None, 2 ** 40, -3, 4, 5])
            self.assertEqual(str(t['ts'][2]), '1970-01-12 13:46:40+00:00')
            md = pq.ParquetFile(io.BytesIO(data)).metadata
            self.assertEqual(md.num_row_groups, 3)
            self.assertEqual(md.row_group(0).column(0).compression, 'GZIP' if comp == 'gzip' else 'UNCOMPRESSED')

    def test_read_pyarrow_files(self):
        pq = _pyarrow()
        if pq is None:
            self.skipTest('optional cross-validator unavailable: pyarrow not installed')
        import pyarrow as pa
        t = pa.table({
            'number': pa.array([1.0, None, 3.0, 4.0, 5.0] * 30, pa.float64()),
            'text': pa.array(['a', None, 'b', 'a', 'c'] * 30, pa.string()),
            'bool': pa.array([True, None, False, True, True] * 30, pa.bool_()),
            'idx': pa.array([0, 1, None, 2, 0] * 30, pa.uint32()),
            'big': pa.array([2 ** 40, None, -1, 0, 7] * 30, pa.int64()),
            'vector': pa.array([{'x': 1.0, 'y': 2.0}, None, {'x': 3.0, 'y': 4.0}, {'x': 5.0, 'y': 6.0}, {'x': 7.0, 'y': 8.0}] * 30,
                               pa.struct([('x', pa.float64()), ('y', pa.float64())]))})
        variants = [dict(compression='NONE', data_page_version='1.0'),
                    dict(compression='GZIP', data_page_version='1.0', use_dictionary=False),
                    dict(compression='GZIP', data_page_version='2.0'),
                    dict(compression='NONE', data_page_version='2.0', use_dictionary=False, row_group_size=40),
                    dict(compression='GZIP', data_page_version='1.0', data_page_size=64)]
        for kw in variants:
            buf = io.BytesIO()
            pq.write_table(t, buf, **kw)
            pf = pl.read_parquet(buf.getvalue())
            self.assertEqual(pf.columns['number'], [1.0, None, 3.0, 4.0, 5.0] * 30, kw)
            self.assertEqual(pf.columns['text'], ['a', None, 'b', 'a', 'c'] * 30, kw)
            self.assertEqual(pf.columns['bool'], [True, None, False, True, True] * 30, kw)
            self.assertEqual(pf.columns['idx'], [0, 1, None, 2, 0] * 30, kw)
            self.assertEqual(pf.columns['big'], [2 ** 40, None, -1, 0, 7] * 30, kw)
            self.assertEqual(pf.columns['vector.x'], [1.0, None, 3.0, 5.0, 7.0] * 30, kw)
            self.assertEqual(pf.columns['vector.y'], [2.0, None, 4.0, 6.0, 8.0] * 30, kw)

    def test_snappy_without_codec_is_clear(self):
        pq = _pyarrow()
        if pq is None:
            self.skipTest('optional cross-validator unavailable: pyarrow not installed')
        try:
            import cramjam  # noqa: F401
            self.skipTest('optional cross-validator unavailable: cramjam present: snappy is supported')
        except ImportError:
            pass
        import pyarrow as pa
        buf = io.BytesIO()
        pq.write_table(pa.table({'a': pa.array([1.0, 2.0] * 100)}), buf, compression='SNAPPY')
        with self.assertRaises(pl.ParquetError) as cm:
            pl.read_parquet(buf.getvalue())
        self.assertIn('SNAPPY', str(cm.exception))


@unittest.skipUnless(SAMPLE_V2.exists(), 'optional cross-validator unavailable: omf-rust sample file not available')
class ParquetSchemaComplianceTests(unittest.TestCase):
    """Our arrays must print exactly like the parquet-rs written samples."""

    CASES = {
        '18.parquet': [Column('scalar', 'double', [1.0, 2.0])],
        '3.parquet': [Column('number', 'double', [1.0, 2.0, 3.0, 4.0, None], optional=True)],
        '2.parquet': [Column('a', 'int32', [0, 1, 2, 3, 0, 0], logical='uint32'),
                      Column('b', 'int32', [1, 2, 3, 0, 2, 3], logical='uint32'),
                      Column('c', 'int32', [4, 4, 4, 4, 1, 2], logical='uint32')],
        '16.parquet': [Column('a', 'int32', [0, 1, 2, 3, 0, 1, 2, 3], logical='uint32'),
                       Column('b', 'int32', [1, 2, 3, 0, 4, 4, 4, 4], logical='uint32')],
        '9.parquet': [Column('index', 'int32', [0, 0, 0, 0, 1], optional=True, logical='uint32')],
        '10.parquet': [Column('name', 'byte_array', ['Base', 'Top'], logical='string')],
        '17.parquet': [Column('text', 'byte_array', [None] * 4 + ['sw', 'se', 'ne', 'nw'], optional=True, logical='string')],
        '21.parquet': [Column('bool', 'boolean', [False] * 7 + [True], optional=True)],
        '12.parquet': [Column('number', 'int64', [1, 2], optional=True)],
        '5.parquet': [Column('number', 'int64', [946684800000000 + h * 3600000000 for h in range(5)],
                             optional=True, logical='timestamp_micros')],
        '7.parquet': [Column(c, 'int32', v, logical='uint8') for c, v in
                      zip('rgba', [[0, 0, 255, 255], [0, 255, 0, 255], [255, 0, 0, 255], [255] * 4])],
        '4.parquet': [Group('color', [Column(c, 'int32', v, logical='uint8') for c, v in
                                      zip('rgba', [[255, 255, 0, 0, 255, 255], [0, 255, 255, 0, 255, 255],
                                                   [0, 0, 0, 255, 255, 255], [255] * 6])])],
        '14.parquet': [Group('vector', [Column('x', 'float', [0, 0, 0, 0, 0.0]), Column('y', 'float', [0, 0, 0, 0, 0.0]),
                                        Column('z', 'float', [0, 0, 0, 0, 1.0])], present=[False] * 4 + [True])],
        '6.parquet': [Column('value', 'int64', [946688400000000, 946692000000000, 946695600000000], logical='timestamp_micros'),
                      Column('inclusive', 'boolean', [False, False, True])],
    }

    def test_schema_strings_match_samples(self):
        pq = _pyarrow()
        if pq is None:
            self.skipTest('optional cross-validator unavailable: pyarrow not installed')
        z = zipfile.ZipFile(str(SAMPLE_V2))
        for name, fields in self.CASES.items():
            ref_data = z.read(name)
            ref_schema = str(pq.ParquetFile(io.BytesIO(ref_data)).schema).split('\n', 1)[1]
            ref_vals = pq.read_table(io.BytesIO(ref_data), use_threads=False).to_pydict()
            for comp in ('gzip', 'none'):
                ours = pl.write_parquet(fields, compression=comp)
                our_schema = str(pq.ParquetFile(io.BytesIO(ours)).schema).split('\n', 1)[1]
                self.assertEqual(our_schema, ref_schema, name)
                self.assertEqual(pq.read_table(io.BytesIO(ours), use_threads=False).to_pydict(), ref_vals, name)
                # and our own reader sees the same thing as the sample
                self.assertEqual(pl.read_parquet(ours).columns, pl.read_parquet(ref_data).columns, name)

    def test_our_reader_matches_pyarrow_on_samples(self):
        pq = _pyarrow()
        if pq is None:
            self.skipTest('optional cross-validator unavailable: pyarrow not installed')
        z = zipfile.ZipFile(str(SAMPLE_V2))
        n = 0
        for info in z.infolist():
            if not info.filename.endswith('.parquet'):
                continue
            data = z.read(info.filename)
            ours = pl.read_parquet(data)
            t = pq.read_table(io.BytesIO(data), use_threads=False)
            for name, col in zip(t.column_names, t.columns):
                pyl = col.to_pylist()
                if pyl and any(isinstance(v, dict) for v in pyl if v is not None):
                    for key in sorted({k for v in pyl if v for k in v}):
                        self.assertEqual(ours.columns[name + '.' + key], [None if v is None else v[key] for v in pyl])
                elif pyl and any(hasattr(v, 'timestamp') for v in pyl if v is not None):
                    continue      # timestamps: checked through the schema test above
                else:
                    self.assertEqual(ours.columns[name], pyl, (info.filename, name))
            n += 1
        self.assertGreaterEqual(n, 30)


# ======================================================================= OMF 2
@unittest.skipUnless(SAMPLE_V2.exists(), 'optional cross-validator unavailable: omf-rust sample file not available')
class ReadSampleOmf2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = read_omf2(str(SAMPLE_V2))

    def test_project(self):
        p = self.project
        self.assertEqual(p.name, 'One of everything')
        self.assertEqual(p.metadata['omf_version'], '2.0-beta.1')
        self.assertEqual(p.metadata['author'], 'Tim Evans')
        self.assertEqual(p.metadata['date'], '1970-01-01T00:00:00Z')
        self.assertEqual(p.metadata['omf_metadata']['number'], 42)
        kinds = [(o.kind, o.name) for o in p.objects]
        self.assertEqual(kinds, [
            ('mesh', 'Pyramid surface'), ('points', 'Pyramid points'), ('lineset', 'Pyramid lines'),
            ('mesh', 'Pyramid grid surface'), ('blockmodel', 'Regular block model'),
            ('blockmodel', 'Sub-blocked block model, regular'), ('blockmodel', 'Sub-blocked block model, free-form'),
            ('mesh', 'Cube faces'), ('lineset', 'Cube edges'), ('mesh', 'Textured')])

    def test_surface(self):
        m = self.project.objects[0]
        self.assertEqual(m.n_vertices, 5)
        self.assertEqual(m.n_triangles, 6)
        self.assertEqual(m.color, [255, 128, 0])
        self.assertEqual(list(m.vertices[12:15]), [0.0, 0.0, 1.0])
        self.assertEqual(list(m.triangles[:3]), [0, 1, 4])
        self.assertTrue(_eq(m.attributes['Numbers']['values'], [1, 2, 3, 4, NAN]))
        self.assertEqual(m.attributes['Numbers']['location'], 'vertices')
        # colours per face and the date-time column live in text_attributes (numeric-only mesh attributes)
        self.assertEqual(m.metadata['text_attributes']['Colors']['location'], 'faces')
        self.assertEqual(m.metadata['text_attributes']['Colors']['values'][0], [255, 0, 0, 255])
        self.assertEqual(m.metadata['text_attributes']['Date-times']['values'][1], '2000-01-01T01:00:00Z')
        self.assertEqual(m.metadata['colormaps']['Date-times']['type'], 'discrete')
        self.assertEqual(m.provenance['format'], 'omf2')
        self.assertEqual(m.metadata['description'], 'A surface forming a pyramid')

    def test_points(self):
        ps = self.project.objects[1]
        self.assertEqual(ps.n, 5)
        self.assertEqual(ps.attributes['Categories'], ['Base', 'Base', 'Base', 'Base', 'Top'])
        cat = ps.metadata['categories']['Categories']
        self.assertEqual(cat['names'], ['Base', 'Top'])
        self.assertEqual(cat['colors'], [[255, 128, 0, 255], [0, 128, 255, 255]])
        self.assertEqual(cat['attributes'], {'Layer': [1.0, 2.0]})
        self.assertEqual(ps.metadata['attribute_units'], {'Categories': 'whatever'})
        self.assertTrue(_eq(ps.attributes['2D Vectors_x'], [1, 1, 0, 0, NAN]))
        self.assertTrue(_eq(ps.attributes['3D Vectors_z'], [NAN, NAN, NAN, NAN, 1.0]))
        self.assertEqual(ps.metadata['vector_attributes']['3D Vectors'], ['3D Vectors_x', '3D Vectors_y', '3D Vectors_z'])

    def test_lines_and_grid_and_blocks(self):
        ls = self.project.objects[2]
        self.assertEqual(ls.n_vertices, 5)
        self.assertEqual(len(ls.segments), 16)
        self.assertEqual(ls.metadata['text_attributes']['Strings']['values'], [None] * 4 + ['sw', 'se', 'ne', 'nw'])
        g = self.project.objects[3]          # tensor grid -> mesh
        self.assertEqual(g.kind, 'mesh')
        self.assertEqual(g.n_vertices, 9)
        self.assertEqual(g.n_triangles, 8)
        b = g.bounds()
        self.assertAlmostEqual(b[0], -1.5)
        self.assertAlmostEqual(b[5], 2.0)
        bm = self.project.objects[4]
        self.assertEqual(bm.origin, [-1.0, -1.0, -1.0])
        self.assertEqual(bm.count, [2, 2, 2])
        self.assertEqual(bm.attributes['Filter'], {'type': 'boolean', 'values': [False] * 7 + [True]})
        self.assertEqual(self.project.objects[7].group, 'Composite')
        self.assertEqual(self.project.objects[8].group, 'Composite')

    def test_warnings(self):
        w = '\n'.join(self.project.metadata['warnings'])
        self.assertIn('Tensor block model', w)
        self.assertIn('sub-blocks', w)
        self.assertIn('ProjectedTexture', w)
        self.assertIn('MappedTexture', w)
        self.assertIn('variable grid spacing', w)

    def test_bytes_source(self):
        with open(str(SAMPLE_V2), 'rb') as fh:
            p = read_omf2(fh.read())
        self.assertEqual(len(p.objects), 10)
        self.assertEqual(p.objects[0].provenance['path'], '<bytes>')


class WriteOmf2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='omf2test')
        cls.src = build_project()
        cls.warnings = []
        cls.path = os.path.join(cls.tmp, 'kit.omf')
        write_omf2(cls.src, cls.path, warnings=cls.warnings)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_container(self):
        self.assertEqual(self.warnings, [])
        z = zipfile.ZipFile(self.path)
        self.assertEqual(z.comment, b'Open Mining Format 2.0-beta.1')
        infos = z.infolist()
        self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in infos))
        self.assertEqual(infos[-1].filename, 'index.json.gz')
        names = [i.filename for i in infos[:-1]]
        self.assertEqual(names, ['%d.parquet' % (k + 1) for k in range(len(names))])
        index = json.loads(gzip.decompress(z.read('index.json.gz')))
        self.assertEqual(index['name'], 'Test kit')
        self.assertEqual(index['coordinate_reference_system'], 'EPSG:32612')
        self.assertEqual(index['units'], 'meters')
        self.assertRegex(index['date'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertEqual([e['name'] for e in index['elements']],
                         ['samples', 'workings', 'surf', 'topo', 'mag', 'blocks'])
        self.assertEqual([e['geometry']['type'] for e in index['elements']],
                         ['PointSet', 'LineSet', 'Surface', 'GridSurface', 'GridSurface', 'BlockModel'])
        samples = index['elements'][0]
        self.assertEqual(samples['color'], [255, 0, 0, 255])
        self.assertEqual({a['name']: a['data']['type'] for a in samples['attributes']},
                         {'vec': 'Vector', 'Au_ppm': 'Number', 'lith': 'Category', 'ok': 'Boolean',
                          'when': 'Number', 'col': 'Color'})
        self.assertEqual(index['elements'][2]['color'][3], 128)
        grid = index['elements'][3]['geometry']
        self.assertEqual(grid['grid'], {'type': 'Regular', 'size': [10.0, 20.0], 'count': [2, 1]})
        self.assertAlmostEqual(grid['orient']['u'][0], math.cos(math.radians(30)))
        self.assertNotIn('heights', index['elements'][4]['geometry'])
        self.assertEqual(index['elements'][4]['attributes'][0]['units'], 'nT')
        bm = index['elements'][5]['geometry']
        self.assertEqual(bm['grid'], {'type': 'Regular', 'size': [10.0, 10.0, 5.0], 'count': [2, 3, 2]})
        self.assertAlmostEqual(bm['orient']['u'][1], -math.sin(math.radians(45)))
        self.assertAlmostEqual(bm['orient']['v'][0], math.sin(math.radians(45)))
        self.assertEqual([a['location'] for a in index['elements'][5]['attributes']], ['Primitives'] * 3)
        # every parquet member is readable and declares the right length
        for el in index['elements']:
            for key in ('vertices', 'segments', 'triangles', 'heights'):
                ref = el['geometry'].get(key)
                if ref:
                    self.assertEqual(pl.read_parquet(z.read(ref['filename'])).num_rows, ref['item_count'])

    def test_roundtrip(self):
        back = read_omf2(self.path)
        self.assertEqual(back.metadata['warnings'], [])
        self.assertEqual(back.crs['kind'], 'utm')
        self.assertEqual(back.crs['zone'], 12)
        self.assertEqual(back.metadata['application'].split()[0], 'nw-mineral-monitor')
        check_roundtrip(self, self.src, back)
        ps, ls, m, g, gp, bm = back.objects
        self.assertEqual(ps.role, 'samples')
        self.assertEqual(ls.role, 'workings')
        self.assertEqual(m.role, 'contact')
        self.assertEqual(g.role, 'topography')
        self.assertEqual(ps.id, self.src.objects[0].id)

    def test_pyarrow_opens_every_array(self):
        pq = _pyarrow()
        if pq is None:
            self.skipTest('optional cross-validator unavailable: pyarrow not installed')
        z = zipfile.ZipFile(self.path)
        index = json.loads(gzip.decompress(z.read('index.json.gz')))
        schemas = {}
        for info in z.infolist():
            if info.filename.endswith('.parquet'):
                data = z.read(info.filename)
                pf = pq.ParquetFile(io.BytesIO(data))
                schemas[info.filename] = str(pf.schema).split('\n', 1)[1]
                self.assertEqual(pf.metadata.num_rows, pl.read_parquet(data).num_rows)
                pq.read_table(io.BytesIO(data), use_threads=False)
        verts = index['elements'][0]['geometry']['vertices']['filename']
        self.assertEqual(schemas[verts], 'required group field_id=-1 schema {\n  required double field_id=-1 x;\n'
                                         '  required double field_id=-1 y;\n  required double field_id=-1 z;\n}\n')
        tris = index['elements'][2]['geometry']['triangles']['filename']
        self.assertIn('required int32 field_id=-1 a (Int(bitWidth=32, isSigned=false));', schemas[tris])
        t = pq.read_table(io.BytesIO(z.read(verts)), use_threads=False).to_pydict()
        self.assertEqual(t['x'], [500000.0, 500010.0, 500020.0])

    def test_omf2_wheel_reads_ours(self):
        omf2 = _omf2_wheel()
        if omf2 is None:
            self.skipTest('optional cross-validator unavailable: omf2 wheel not installed')
        reader = omf2.Reader(self.path)
        project, problems = reader.project()
        self.assertEqual([str(p) for p in problems], [])
        els = project.elements()
        self.assertEqual([e.name for e in els], ['samples', 'workings', 'surf', 'topo', 'mag', 'blocks'])
        self.assertEqual(reader.array_vertices(els[0].geometry().vertices).tolist()[1], [500010.0, 4100000.0, 1510.0])
        atts = {a.name: a for a in els[0].attributes()}
        vals, mask = reader.array_numbers(atts['Au_ppm'].get_data().values)
        self.assertEqual(vals.tolist()[0], 1.5)
        self.assertEqual(mask.tolist(), [False, True, False])
        cat = atts['lith'].get_data()
        self.assertEqual(reader.array_names(cat.names), ['qtz', 'sch'])
        idx, imask = reader.array_indices(cat.values)
        self.assertEqual(idx.tolist()[0], 0)
        self.assertEqual(imask.tolist(), [False, True, False])
        self.assertEqual(reader.array_gradient(cat.gradient).tolist(), [[255, 0, 0, 255], [0, 0, 255, 255]])
        b, bmask = reader.array_booleans(atts['ok'].get_data().values)
        self.assertEqual(b.tolist()[:2], [True, False])
        self.assertEqual(bmask.tolist(), [False, False, True])
        when, wmask = reader.array_numbers(atts['when'].get_data().values)
        self.assertEqual(str(when[0])[:19], '2020-01-01T00:00:00')
        vec, vmask = reader.array_vectors(atts['vec'].get_data().values)
        self.assertEqual(vec.tolist()[0], [1.0, 0.0, 0.0])
        self.assertEqual(vmask.tolist(), [False, False, True])
        col, cmask = reader.array_color(atts['col'].get_data().values)
        self.assertEqual(col.tolist()[2], [10, 20, 30, 40])
        self.assertEqual(cmask.tolist(), [False, True, False])
        self.assertEqual(reader.array_triangles(els[2].geometry().triangles).tolist(), [[0, 1, 2], [1, 3, 2]])
        self.assertEqual(reader.array_segments(els[1].geometry().segments).tolist(), [[0, 1], [1, 2], [3, 4]])
        heights = reader.array_scalars(els[3].geometry().heights).tolist()
        self.assertEqual(heights[:4], [1.0, 2.0, 3.0, 4.0])
        self.assertTrue(math.isnan(heights[4]))
        text = reader.array_text(els[5].attributes()[1].get_data().values)
        self.assertEqual(text[:2], ['a', 'b'])

    def test_bytesio_and_object_list(self):
        buf = io.BytesIO()
        data = write_omf2([self.src.objects[0], self.src.objects[2]], buf, name='two', prerelease='')
        self.assertIsInstance(data, bytes)
        self.assertEqual(zipfile.ZipFile(io.BytesIO(data)).comment, b'Open Mining Format 2.0')
        back = read_omf2(data)
        self.assertEqual(back.name, 'two')
        self.assertEqual([o.kind for o in back.objects], ['points', 'mesh'])
        self.assertEqual(back.metadata['omf_version'], '2.0')

    def test_uncompressed_pages(self):
        path = os.path.join(self.tmp, 'plain.omf')
        write_omf2(self.src, path, compression='none')
        back = read_omf2(path)
        check_roundtrip(self, self.src, back)
        omf2 = _omf2_wheel()
        if omf2 is not None:
            _, problems = omf2.Reader(path).project()
            self.assertEqual(len(problems), 0)

    def test_duplicate_and_empty_names(self):
        a = PointSet([[0, 0, 0]], name='dup')
        b = PointSet([[1, 1, 1]], name='dup')
        c = PointSet([[2, 2, 2]], name='')
        data = write_omf2([a, b, c], io.BytesIO())
        back = read_omf2(data)
        self.assertEqual([o.name for o in back.objects], ['dup', 'dup (2)', 'element-3'])

    def test_rejects_non_omf_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('index.json.gz', gzip.compress(b'{}'))
        with self.assertRaises(ValueError):
            read_omf2(buf.getvalue())
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, 'w') as z:
            z.writestr('index.json.gz', gzip.compress(b'{"name": "x"}'))
            z.comment = b'Open Mining Format 3.0'
        with self.assertRaises(ValueError):
            read_omf2(buf2.getvalue())

    def test_left_handed_and_flat_grid_surfaces(self):
        # hand-built index: v points clockwise of u -> rows are flipped on read
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as z:
            z.writestr('1.parquet', pl.write_parquet([Column('scalar', 'double', [1, 2, 3, 4, 5, 6])]))
            index = {'name': 'lh', 'date': '2026-01-01T00:00:00Z', 'elements': [
                {'name': 'g', 'geometry': {'type': 'GridSurface',
                                           'orient': {'origin': [10.0, 20.0, 5.0], 'u': [1.0, 0.0, 0.0], 'v': [0.0, -1.0, 0.0]},
                                           'grid': {'type': 'Regular', 'size': [1.0, 2.0], 'count': [2, 1]},
                                           'heights': {'filename': '1.parquet', 'item_count': 6}}},
                {'name': 'flat', 'geometry': {'type': 'GridSurface',
                                              'orient': {'origin': [0.0, 0.0, 0.0], 'u': [1.0, 0.0, 0.0], 'v': [0.0, 1.0, 0.0]},
                                              'grid': {'type': 'Regular', 'size': [1.0, 1.0], 'count': [1, 1]}}}]}
            z.writestr('index.json.gz', gzip.compress(json.dumps(index).encode()))
            z.comment = b'Open Mining Format 2.0-beta.1'
        p = read_omf2(buf.getvalue())
        g, flat = p.objects
        self.assertEqual((g.nx, g.ny), (3, 2))
        self.assertEqual((g.x0, g.y0), (10.0, 18.0))
        # w = u x v points down: heights subtract; far row first
        self.assertEqual(list(g.values), [5 - 4, 5 - 5, 5 - 6, 5 - 1, 5 - 2, 5 - 3])
        self.assertEqual(g.node_xy(0, 1), (10.0, 20.0))
        self.assertEqual(list(flat.values), [0.0, 0.0, 0.0, 0.0])


# ===================================================================== OMF 0.9
@unittest.skipUnless(SAMPLE_V09.exists(), 'optional cross-validator unavailable: omf 1.0.1 reference file not available')
class ReadReferenceOmf1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = read_omf1(str(SAMPLE_V09))

    def test_project_and_elements(self):
        p = self.project
        self.assertEqual(p.name, 'Test Project')
        self.assertEqual(p.metadata['author'], 'me')
        self.assertEqual(p.metadata['revision'], 'r1')
        self.assertEqual(p.metadata['omf_version'], '0.9.0')
        self.assertEqual([(o.kind, o.name) for o in p.objects],
                         [('points', 'pts'), ('lineset', 'lines'), ('mesh', 'surf'), ('grid2d', 'gridsurf'), ('blockmodel', 'vol')])
        self.assertEqual(p.metadata['warnings'], ["element 'surf': 1 image texture(s) skipped"])

    def test_points(self):
        ps = self.project.objects[0]
        # project origin (100, 200, 300) + element origin (1, 2, 3) applied
        self.assertEqual(list(ps.xyz[:3]), [101.0, 202.0, 303.0])
        self.assertEqual(ps.color, [255, 0, 0])
        self.assertEqual(list(ps.attributes['scalar']), [1.5, 2.5, 3.5])
        self.assertEqual(ps.attributes['strings'], ['a', 'b', 'c'])
        self.assertEqual(list(ps.attributes['vec3_y']), [0.0, 1.0, 0.0])
        self.assertEqual(list(ps.attributes['vec2_x']), [1.0, 0.0, 0.0])
        self.assertEqual(ps.attributes['colors'], [[255, 0, 0, 255], [0, 255, 0, 255], [0, 0, 255, 255]])
        self.assertEqual(ps.attributes['dates'], ['2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z', '2020-01-03T00:00:00Z'])
        self.assertEqual(ps.attributes['mapped'], ['x', 'y', None])
        self.assertEqual(ps.metadata['categories']['mapped']['names'], ['x', 'y'])
        cm = ps.metadata['colormaps']['scalar']
        self.assertEqual(cm['range'], [0.0, 10.0])
        self.assertEqual(len(cm['gradient']), 128)
        self.assertEqual(ps.metadata['omf_subtype'], 'point')

    def test_others(self):
        ls, m, g, bm = self.project.objects[1:]
        self.assertEqual(ls.metadata['omf_subtype'], 'borehole')
        self.assertEqual(list(ls.segments), [0, 1, 1, 2])
        self.assertEqual(list(ls.attributes['segdata']['values']), [1.0, 2.0])
        self.assertEqual(m.attributes['facedata'], {'location': 'faces', 'values': farray([7.0])})
        self.assertEqual((g.nx, g.ny, g.dx, g.dy), (3, 3, 1.0, 2.0))
        self.assertEqual((g.x0, g.y0), (100.0, 200.0))
        self.assertEqual(list(g.values), [300.0] * 9)
        self.assertEqual(bm.origin, [110.0, 220.0, 330.0])
        self.assertEqual(bm.count, [2, 1, 1])
        self.assertEqual(list(bm.attributes['celldata']['values']), [1.0, 2.0])


class WriteOmf1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='omf1test')
        cls.src = build_project()
        cls.warnings = []
        cls.path = os.path.join(cls.tmp, 'kit_v09.omf')
        write_omf1(cls.src, cls.path, warnings=cls.warnings)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_header_and_registry(self):
        self.assertEqual(self.warnings, [])
        with open(self.path, 'rb') as fh:
            raw = fh.read()
        self.assertEqual(raw[:4], b'\x84\x83\x82\x81')
        self.assertEqual(raw[4:36], b'OMF-v0.9.0'.ljust(32, b'\x00'))
        import struct, uuid
        json_start = struct.unpack('<Q', raw[52:60])[0]
        reg = json.loads(raw[json_start:].decode('utf-8'))
        puid = str(uuid.UUID(bytes=raw[36:52]))
        self.assertEqual(reg[puid]['__class__'], 'Project')
        self.assertEqual(len(reg[puid]['elements']), 6)
        classes = sorted({v['__class__'] for v in reg.values()})
        for c in ('PointSetElement', 'LineSetElement', 'SurfaceElement', 'VolumeElement', 'SurfaceGeometry',
                  'SurfaceGridGeometry', 'VolumeGridGeometry', 'ScalarData', 'StringData', 'MappedData',
                  'Vector3Data', 'ColorData', 'Legend', 'ScalarColormap', 'Vector3Array', 'Int3Array', 'Int2Array'):
            self.assertIn(c, classes)
        for v in reg.values():
            self.assertIn('date_created', v)
            self.assertIn('date_modified', v)
            arr = v.get('array')
            if isinstance(arr, dict):
                self.assertIn(arr['dtype'], ('<f8', '<i8'))
                self.assertGreaterEqual(arr['start'], 60)
                self.assertLessEqual(arr['start'] + arr['length'], json_start)

    def test_roundtrip(self):
        back = read_omf1(self.path)
        self.assertEqual(back.metadata['warnings'], [])
        # v0.9 colours have no alpha and elements no opacity
        check_roundtrip(self, self.src, back, alpha_kept=False, opacity_kept=False, units_kept=False)
        self.assertEqual(back.objects[0].attributes['col'][1], [255, 255, 255, 255])

    def test_reference_reader_validates(self):
        if not _omf1_reference_available():
            self.skipTest('optional cross-validator unavailable: omf 1.0.1 reference environment (/tmp/omfenv) not available')
        script = r'''
import json, sys
import omf, numpy as np
p = omf.OMFReader(sys.argv[1]).get_project()
out = {'valid': bool(p.validate()), 'elements': []}
for e in p.elements:
    out['elements'].append({'class': e.__class__.__name__, 'name': e.name, 'geometry': type(e.geometry).__name__,
                            'data': [(d.__class__.__name__, d.name, d.location, int(len(d.array))) for d in e.data]})
pts = p.elements[0]
out['scalar'] = [None if np.isnan(v) else float(v) for v in pts.data[1].array.array]
out['mapped'] = [int(v) for v in pts.data[2].array.array]
out['legend'] = list(pts.data[2].legends[0].values.array)
out['grid_offsets'] = [None if np.isnan(v) else float(v) for v in p.elements[3].geometry.offset_w.array]
out['vol_axis_u'] = [float(v) for v in p.elements[5].geometry.axis_u]
print(json.dumps(out))
'''
        r = subprocess.run([str(OMF1_PYTHON), '-c', script, self.path], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(out['valid'])
        self.assertEqual([e['class'] for e in out['elements']],
                         ['PointSetElement', 'LineSetElement', 'SurfaceElement', 'SurfaceElement', 'SurfaceElement', 'VolumeElement'])
        self.assertEqual(out['elements'][3]['geometry'], 'SurfaceGridGeometry')
        self.assertEqual(out['elements'][0]['data'][1], ['ScalarData', 'Au_ppm', 'vertices', 3])
        self.assertEqual(out['scalar'], [1.5, None, 3.25])
        self.assertEqual(out['mapped'], [0, -1, 1])
        self.assertEqual(out['legend'], ['qtz', 'sch'])
        self.assertEqual(out['grid_offsets'], [1.0, 2.0, 3.0, 4.0, None, 6.0])
        self.assertAlmostEqual(out['vol_axis_u'][1], -math.sin(math.radians(45)))

    def test_omf_rust_converter_accepts_ours(self):
        omf2 = _omf2_wheel()
        if omf2 is None:
            self.skipTest('optional cross-validator unavailable: omf2 wheel not installed')
        self.assertTrue(omf2.detect_omf1(self.path))
        out = os.path.join(self.tmp, 'rust_converted.omf')
        problems = omf2.Omf1Converter().convert(self.path, out)
        self.assertEqual([str(p) for p in problems], [])
        project, problems = omf2.Reader(out).project()
        self.assertEqual(len(problems), 0)
        self.assertEqual([e.name for e in project.elements()], ['samples', 'workings', 'surf', 'topo', 'mag', 'blocks'])

    def test_bytesio_and_bad_input(self):
        data = write_omf1([self.src.objects[2]], io.BytesIO(), name='one')
        self.assertIsInstance(data, bytes)
        back = read_omf1(data)
        self.assertEqual(back.name, 'one')
        self.assertEqual(back.objects[0].kind, 'mesh')
        with self.assertRaises(ValueError):
            read_omf1(b'\x84\x83\x82\x81' + b'OMF-v1.0.0'.ljust(56, b'\x00'))
        with self.assertRaises(ValueError):
            read_omf1(b'PK\x03\x04' + b'\x00' * 80)


@unittest.skipUnless(SAMPLE_V09.exists(), 'optional cross-validator unavailable: omf 1.0.1 reference file not available')
class ConvertTests(unittest.TestCase):
    def test_convert_omf1_to_omf2(self):
        tmp = tempfile.mkdtemp(prefix='omfconv')
        try:
            out = os.path.join(tmp, 'converted.omf')
            warnings = []
            convert_omf1_to_omf2(str(SAMPLE_V09), out, warnings=warnings)
            self.assertEqual(warnings, [])
            p = read_omf2(out)
            self.assertEqual(p.name, 'Test Project')
            self.assertEqual([(o.kind, o.name) for o in p.objects],
                             [('points', 'pts'), ('lineset', 'lines'), ('mesh', 'surf'), ('grid2d', 'gridsurf'), ('blockmodel', 'vol')])
            ps = p.objects[0]
            self.assertEqual(list(ps.xyz[:3]), [101.0, 202.0, 303.0])
            self.assertEqual(ps.attributes['mapped'], ['x', 'y', None])
            self.assertEqual(ps.attributes['dates'][0], '2020-01-01T00:00:00Z')
            self.assertEqual(ps.metadata['datetime_attributes'], ['dates'])
            self.assertEqual(list(ps.attributes['vec3_z']), [0.0, 0.0, 1.0])
            self.assertEqual(ps.attributes['colors'][2], [0, 0, 255, 255])
            self.assertEqual(p.objects[4].origin, [110.0, 220.0, 330.0])
            omf2 = _omf2_wheel()
            if omf2 is not None:
                project, problems = omf2.Reader(out).project()
                self.assertEqual([str(x) for x in problems], [])
                self.assertEqual(len(project.elements()), 5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RegistryTests(unittest.TestCase):
    def test_registry_resolves(self):
        self.assertIs(formats.reader('omf1'), read_omf1)
        self.assertIs(formats.writer('omf1'), write_omf1)
        self.assertIs(formats.reader('omf2'), read_omf2)
        self.assertIs(formats.writer('omf2'), write_omf2)
        self.assertEqual(formats.sniff(head=b'\x84\x83\x82\x81' + b'\x00' * 60), 'omf1')
        self.assertEqual(formats.sniff(path='x.omf', head=b'PK\x03\x04'), 'omf2')

    def test_stdlib_only(self):
        import ast
        allowed_local = {'geomodel', 'parquet_lite', 'thrift_compact', 'omf1', 'omf2', 'model'}
        for mod in ('omf1', 'omf2', 'parquet_lite', 'thrift_compact'):
            src = (ROOT / 'pipelines' / 'geomodel' / 'formats' / (mod + '.py')).read_text(encoding='utf-8')
            for node in ast.walk(ast.parse(src)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for n in names:
                    top = n.split('.')[0]
                    if top in allowed_local:
                        continue
                    if top in ('cramjam', 'snappy', 'zstandard'):
                        continue    # optional codecs, imported lazily inside try/except
                    self.assertIn(top, sys.stdlib_module_names if hasattr(sys, 'stdlib_module_names') else {top},
                                  '%s imports non-stdlib module %s' % (mod, n))


if __name__ == '__main__':
    unittest.main()
