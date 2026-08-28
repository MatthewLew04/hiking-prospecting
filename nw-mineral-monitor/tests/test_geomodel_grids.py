"""Grid / voxel format readers and writers: Surfer, Geosoft GRD/GXF/XYZ,
Arc ASCII, ZMAP+, Irap, CPS-3 and UBC.

Round trips, orientation (which value lands on which node), hand-built
compressed streams, and independent validation against GDAL (via rasterio)
and harmonica's Geosoft reader when those are installed.

    python3 -m unittest tests.test_geomodel_grids -v
"""
import importlib.util
import io
import math
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from geomodel.model import Grid2D, PointSet, BlockModel, LineSet, NAN   # noqa: E402
from geomodel.formats import surfer, geosoft, arcascii, zmap, irap, ubc, cps3   # noqa: E402

from gm_ref import HARMONICA_REF as _HARMONICA_REF   # noqa: E402

HARMONICA_REF = str(_HARMONICA_REF)


# ----------------------------------------------------------------- helpers
def make_grid(dx=25.0, dy=10.0, rotation=0.0, name='test grid'):
    """7 x 5 grid, dx != dy, negative values, one NaN hole at (i=2, j=3).
    value(i, j) = -50 + 3.25*i + 100*j so every node is distinct."""
    nx, ny = 7, 5
    vals = []
    for j in range(ny):
        for i in range(nx):
            vals.append(NAN if (i == 2 and j == 3) else -50.0 + 3.25 * i + 100.0 * j)
    return Grid2D(nx, ny, 500000.0, 4500000.0, dx, dy, vals, rotation=rotation, name=name)


class GridAssertions(unittest.TestCase):
    def assertGridEqual(self, a, b, tol=1e-9, value_tol=None):
        self.assertEqual((a.nx, a.ny), (b.nx, b.ny))
        for attr in ('x0', 'y0', 'dx', 'dy', 'rotation'):
            self.assertAlmostEqual(getattr(a, attr), getattr(b, attr), delta=tol, msg=attr)
        vt = tol if value_tol is None else value_tol
        self.assertEqual(len(a.values), len(b.values))
        for k, (u, v) in enumerate(zip(a.values, b.values)):
            if u != u:
                self.assertTrue(v != v, 'node %d: expected NaN, got %r' % (k, v))
            else:
                self.assertAlmostEqual(u, v, delta=vt, msg='node %d' % k)

    def assertProvenance(self, obj, fmt, path=None):
        self.assertEqual(obj.provenance.get('format'), fmt)
        self.assertIsInstance(obj.metadata.get('warnings'), list)
        if path:
            self.assertEqual(obj.provenance.get('path'), path)


# ------------------------------------------------------------- round trips
class RoundTrips(GridAssertions):
    def test_surfer_dsaa(self):
        g = make_grid()
        data = surfer.write_grd(g, io.BytesIO(), fmt='dsaa')
        self.assertTrue(data.startswith(b'DSAA'))
        r = surfer.read_grd(data)
        self.assertGridEqual(g, r)
        self.assertProvenance(r, 'surfer_grd')
        self.assertEqual(r.metadata['surfer_variant'], 'DSAA')
        self.assertEqual(r.metadata['zlo'], -50.0)
        self.assertEqual(r.metadata['zhi'], 369.5)

    def test_surfer_dsbb(self):
        g = make_grid()
        data = surfer.write_grd(g, io.BytesIO(), fmt='dsbb')
        self.assertTrue(data.startswith(b'DSBB'))
        self.assertEqual(len(data), 56 + 4 * 35)
        r = surfer.read_grd(data)
        self.assertGridEqual(g, r, value_tol=1e-4)     # float32 storage
        self.assertEqual(r.metadata['surfer_variant'], 'DSBB')

    def test_surfer_dsrb(self):
        g = make_grid()
        data = surfer.write_grd(g, io.BytesIO(), fmt='dsrb')
        self.assertTrue(data.startswith(b'DSRB'))
        r = surfer.read_grd(data)
        self.assertGridEqual(g, r)
        self.assertEqual(r.metadata['surfer_variant'], 'DSRB')
        self.assertEqual(r.metadata['blank_nodes'], 1)

    def test_surfer_path_io_and_name(self):
        g = make_grid()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'hill.grd')
            self.assertEqual(surfer.write_grd(g, p, fmt='dsrb'), p)
            r = surfer.read_grd(p)
            self.assertGridEqual(g, r)
            self.assertProvenance(r, 'surfer_grd', p)
            self.assertEqual(r.name, 'hill')
            with open(p, 'rb') as fh:
                r2 = surfer.read_grd(fh)
            self.assertGridEqual(g, r2)

    def test_surfer_all_blank_zrange(self):
        g = Grid2D(3, 2, 0, 0, 1, 1, [NAN] * 6)
        data = surfer.write_grd(g, io.BytesIO(), fmt='dsaa').decode()
        self.assertIn('1.70141e+38 1.70141e+38', data.splitlines()[4])
        r = surfer.read_grd(data.encode())
        self.assertTrue(all(v != v for v in r.values))

    def test_surfer_rejects_rotation_and_unknown_fmt(self):
        with self.assertRaises(ValueError):
            surfer.write_grd(make_grid(rotation=15.0), io.BytesIO())
        with self.assertRaises(ValueError):
            surfer.write_grd(make_grid(), io.BytesIO(), fmt='dsxx')
        with self.assertRaises(ValueError):
            surfer.read_grd(b'NOPE' + b'\0' * 100)

    def test_surfer_bln(self):
        ls = LineSet(role='lines')
        ls.add_polyline([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 0, 0)], {'flag': 1, 'name': 'pit'})
        ls.add_polyline([(5, 5, 1.5), (6, 7, 2.5)], {'flag': 0, 'name': 'break line'})
        data = surfer.write_bln(ls, io.BytesIO())
        text = data.decode()
        self.assertTrue(text.startswith('4,1,"pit"\n0.0,0.0,0.0\n'))
        r = surfer.read_bln(data)
        self.assertEqual(len(r.parts), 2)
        self.assertEqual(r.features[0]['flag'], 1)
        self.assertEqual(r.features[0]['name'], 'pit')
        self.assertTrue(r.features[0]['closed'])
        self.assertFalse(r.features[1]['closed'])
        self.assertEqual(r.features[1]['name'], 'break line')
        self.assertEqual(r.part_xyz(1), [(5.0, 5.0, 1.5), (6.0, 7.0, 2.5)])
        self.assertProvenance(r, 'surfer_bln')
        # classic 2-column blanking file without names
        r2 = surfer.read_bln(b'3, 1\n1, 2\n3, 4\n1, 2\n')
        self.assertEqual(r2.part_xyz(0), [(1.0, 2.0, 0.0), (3.0, 4.0, 0.0), (1.0, 2.0, 0.0)])
        self.assertEqual(r2.features[0], {'flag': 1, 'name': '', 'closed': True})

    def test_geosoft_grd_float(self):
        g = make_grid()
        data = geosoft.write_grd(g, io.BytesIO(), dtype='float')
        self.assertEqual(struct.unpack('<5i', data[:20]), (4, 2, 7, 5, 1))
        self.assertEqual(len(data), 512 + 4 * 35)
        r = geosoft.read_grd(data)
        self.assertGridEqual(g, r, value_tol=1e-4)
        self.assertProvenance(r, 'geosoft_grd')
        self.assertEqual(r.name, 'test grid')
        self.assertEqual(r.metadata['geosoft']['NVPTS'], 34)
        self.assertEqual(r.metadata['dummy_nodes'], 1)
        self.assertTrue(any('.gi' in w for w in g.metadata['warnings']))

    def test_geosoft_grd_short(self):
        g = make_grid()
        data = geosoft.write_grd(g, io.BytesIO(), dtype='short')
        self.assertEqual(struct.unpack('<5i', data[:20]), (2, 1, 7, 5, 1))
        r = geosoft.read_grd(data)
        span = 369.5 + 50.0
        self.assertGridEqual(g, r, value_tol=span / 65000.0)
        self.assertEqual(r.metadata['geosoft']['SF'], 1)

    def test_geosoft_grd_rotation(self):
        g = make_grid(rotation=30.0)
        r = geosoft.read_grd(geosoft.write_grd(g, io.BytesIO()))
        self.assertGridEqual(g, r, value_tol=1e-4)
        self.assertAlmostEqual(r.rotation, 30.0)

    def test_gxf(self):
        g = make_grid()
        data = geosoft.write_gxf(g, io.BytesIO())
        text = data.decode()
        self.assertTrue(all(len(ln) <= 80 for ln in text.splitlines()))
        self.assertIn('#SENSE\n1\n', text)
        self.assertIn('#DUMMY\n-1e+32\n', text)
        r = geosoft.read_gxf(data)
        self.assertGridEqual(g, r)
        self.assertProvenance(r, 'gxf')
        self.assertEqual(r.name, 'test grid')
        self.assertEqual(r.metadata['gxf']['sense'], 1)

    def test_gxf_rotation_and_long_rows(self):
        g = make_grid(rotation=-35.0)
        r = geosoft.read_gxf(geosoft.write_gxf(g, io.BytesIO()))
        self.assertGridEqual(g, r)
        wide = Grid2D(40, 2, 0, 0, 1, 1, [k / 7.0 for k in range(80)])
        data = geosoft.write_gxf(wide, io.BytesIO())
        self.assertTrue(all(len(ln) <= 80 for ln in data.decode().splitlines()))
        self.assertGridEqual(wide, geosoft.read_gxf(data))

    def test_arc_ascii(self):
        g = make_grid(25.0, 25.0)
        data = arcascii.write_asc(g, io.BytesIO())
        text = data.decode()
        self.assertTrue(text.startswith('ncols        7\nnrows        5\nxllcorner    499987.5\n'
                                        'yllcorner    4499987.5\ncellsize     25.0\nNODATA_value -9999.0\n'))
        # north row first
        self.assertTrue(text.splitlines()[6].startswith('350.0 353.25'))
        r = arcascii.read_asc(data)
        self.assertGridEqual(g, r)
        self.assertProvenance(r, 'arc_ascii')

    def test_arc_ascii_center_form_and_rejections(self):
        r = arcascii.read_asc(b'NCOLS 2\nNROWS 2\nXLLCENTER 10\nYLLCENTER 20\nCELLSIZE 5\n'
                              b'NODATA_value -1\n3 4\n1 -1\n')
        self.assertEqual((r.x0, r.y0, r.dx), (10.0, 20.0, 5.0))
        self.assertEqual(list(r.values[:1]), [1.0])
        self.assertTrue(r.values[1] != r.values[1])
        self.assertEqual(list(r.values[2:]), [3.0, 4.0])
        with self.assertRaises(ValueError) as cm:
            arcascii.write_asc(make_grid(25.0, 10.0), io.BytesIO())
        self.assertIn('square', str(cm.exception))
        with self.assertRaises(ValueError):
            arcascii.write_asc(make_grid(25.0, 25.0, rotation=5.0), io.BytesIO())

    def test_zmap(self):
        g = make_grid()
        data = zmap.write_zmap(g, io.BytesIO())
        lines = data.decode().splitlines()
        self.assertEqual(lines[2], '@test grid, GRID, 5')
        self.assertEqual(lines[3], '20, -9999.0000000, , 7, 1')
        self.assertEqual(lines[4], '5, 7, 500000.0000000, 500150.0000000, 4500000.0000000, 4500040.0000000')
        self.assertEqual(lines[6], '@')
        self.assertTrue(all(len(ln) % 20 == 0 for ln in lines[7:]))
        r = zmap.read_zmap(data)
        self.assertGridEqual(g, r)
        self.assertProvenance(r, 'zmap')
        self.assertEqual(r.name, 'test grid')
        r2 = zmap.read_zmap(zmap.write_zmap(g, io.BytesIO(), nodes_per_line=3, field_width=15, decimals=3))
        self.assertGridEqual(g, r2, value_tol=1e-3)
        with self.assertRaises(ValueError):
            zmap.write_zmap(make_grid(rotation=1.0), io.BytesIO())

    def test_irap(self):
        g = make_grid()
        data = irap.write_irap(g, io.BytesIO())
        lines = data.decode().splitlines()
        self.assertEqual(lines[0], '-996 5 25.0 10.0')
        self.assertEqual(lines[1], '500000.0 500150.0 4500000.0 4500040.0')
        self.assertEqual(lines[2], '7 0.0 500000.0 4500000.0')
        self.assertEqual(lines[3], '0 0 0 0 0 0 0')
        self.assertEqual(len(lines[4].split()), 6)
        r = irap.read_irap(data)
        self.assertGridEqual(g, r)
        self.assertProvenance(r, 'irap')
        self.assertEqual(r.metadata['undefined_nodes'], 1)

    def test_irap_rotation(self):
        g = make_grid(rotation=22.5)
        r = irap.read_irap(irap.write_irap(g, io.BytesIO()))
        self.assertGridEqual(g, r)
        self.assertAlmostEqual(r.rotation, 22.5)
        with self.assertRaises(ValueError):
            irap.read_irap(b'-995 1 1 1\n0 0 0 0\n1 0 0 0\n0 0 0 0 0 0 0\n1\n')

    def test_ubc(self):
        bm = BlockModel([1000.0, 2000.0, 480.0], [10.0, 20.0, 5.0], [3, 2, 4])
        vals = [float(k) - 3.5 for k in range(bm.n)]
        vals[5] = NAN
        bm.add_attribute('density', vals)
        bm.add_attribute('sus', [v * 2 for v in vals])
        mesh, model = ubc.write_ubc(bm, io.BytesIO(), io.BytesIO(), 'density', nodata=-99999.0)
        self.assertEqual(mesh.decode(), '3 2 4\n1000.0 2000.0 500.0\n3*10.0\n2*20.0\n4*5.0\n')
        r = ubc.read_ubc(mesh, model, name='density', nodata=-99999.0)
        self.assertEqual(r.count, [3, 2, 4])
        self.assertEqual(r.origin, [1000.0, 2000.0, 480.0])
        self.assertEqual(r.block_size, [10.0, 20.0, 5.0])
        got = r.attributes['density']['values']
        for u, v in zip(vals, got):
            if u != u:
                self.assertTrue(v != v)
            else:
                self.assertEqual(u, v)
        self.assertProvenance(r, 'ubc')
        # several models at once, via dict targets
        mesh2, models = ubc.write_ubc(bm, io.BytesIO(), {'density': io.BytesIO(), 'sus': io.BytesIO()})
        r2 = ubc.read_ubc(mesh2, models={'density': models['density'], 'sus': models['sus']}, nodata=-99999.0)
        self.assertEqual(sorted(r2.attributes), ['density', 'sus'])
        self.assertEqual(r2.attributes['sus']['values'][0], vals[0] * 2)

    def test_xyz(self):
        ps = PointSet(role='points')
        ps.add(1.0, 2.0, 3.0, mag=10.5, line='100', line_type='Line')
        ps.add(1.5, 2.0, NAN, mag=NAN, line='100', line_type='Line')
        ps.add(5.0, 6.0, 7.0, mag=-1.25, line='5', line_type='Tie')
        data = geosoft.write_xyz(ps, io.BytesIO())
        text = data.decode()
        self.assertIn('/ X Y Z mag\n', text)
        self.assertIn('\nLine 100\n', text)
        self.assertIn('\nTie 5\n', text)
        self.assertIn('1.5 2.0 * *\n', text)
        r = geosoft.read_xyz(data)
        self.assertEqual(r.n, 3)
        self.assertEqual(r.attributes['line'], ['100', '100', '5'])
        self.assertEqual(r.attributes['line_type'], ['Line', 'Line', 'Tie'])
        self.assertEqual(r.attributes['mag'][0], 10.5)
        self.assertTrue(r.attributes['mag'][1] != r.attributes['mag'][1])
        self.assertEqual(r.point(2), (5.0, 6.0, 7.0))
        self.assertProvenance(r, 'geosoft_xyz')


# --------------------------------------------------------- rasterio / GDAL
def _rasterio():
    if importlib.util.find_spec('numpy') is None or importlib.util.find_spec('rasterio') is None:
        return None
    try:
        import rasterio
        import rasterio.shutil              # noqa: F401  (attribute used via rasterio.shutil)
    except ImportError:
        return None
    return rasterio


class GdalCrossCheck(GridAssertions):
    """Independent validation: GDAL (through rasterio) reads what we write
    and we read what GDAL writes.  GDAL returns rows north-first and a
    pixel-is-area geotransform, so node x0 = c + a/2 and the south node
    y0 = f + e*(ny - 0.5)."""

    def setUp(self):
        self.rio = _rasterio()
        if self.rio is None:
            self.skipTest('optional cross-validator unavailable: rasterio not installed')
        import numpy as np
        self.np = np
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        if getattr(self, 'tmp', None):
            self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def north_first(self, g):
        return self.np.array(g.values, dtype='f8').reshape(g.ny, g.nx)[::-1]

    def check_gdal_reads(self, path, g, env=None):
        np = self.np
        with self.rio.Env(**(env or {})):
            with self.rio.open(path) as ds:
                self.assertEqual((ds.width, ds.height), (g.nx, g.ny))
                arr = ds.read(1, masked=True)
                exp = self.north_first(g)
                mask = np.isnan(exp)
                self.assertTrue(np.array_equal(np.ma.getmaskarray(arr), mask), 'nodata mask differs')
                self.assertTrue(np.allclose(arr.filled(0)[~mask], exp[~mask], atol=1e-4))
                t = ds.transform
                self.assertAlmostEqual(t.c + t.a / 2.0, g.x0, places=6)
                self.assertAlmostEqual(t.f + t.e * (g.ny - 0.5), g.y0, places=6)
                self.assertAlmostEqual(t.a, g.dx, places=9)
                self.assertAlmostEqual(-t.e, g.dy, places=9)
                return ds.driver

    def gdal_write(self, g, path, driver, env=None):
        """Write ``g`` with GDAL's ``driver`` (through a GeoTIFF + CreateCopy)."""
        np = self.np
        from rasterio.transform import from_origin
        exp = self.north_first(g)
        arr = np.where(np.isnan(exp), -9999.0, exp)
        tif = self.path('src.tif')
        tr = from_origin(g.x0 - g.dx / 2.0, g.ymax + g.dy / 2.0, g.dx, g.dy)
        with self.rio.open(tif, 'w', driver='GTiff', width=g.nx, height=g.ny, count=1,
                           dtype='float64', transform=tr, nodata=-9999.0) as dst:
            dst.write(arr, 1)
        with self.rio.Env(**(env or {})):
            self.rio.shutil.copy(tif, path, driver=driver)
        return path

    # (a) our writers -> GDAL
    def test_gdal_reads_our_dsaa(self):
        g = make_grid()
        p = self.path('w.grd')
        surfer.write_grd(g, p, fmt='dsaa')
        self.assertEqual(self.check_gdal_reads(p, g), 'GSAG')

    def test_gdal_reads_our_dsbb(self):
        g = make_grid()
        p = self.path('w.grd')
        surfer.write_grd(g, p, fmt='dsbb')
        self.assertEqual(self.check_gdal_reads(p, g), 'GSBG')

    def test_gdal_reads_our_dsrb(self):
        g = make_grid()
        p = self.path('w.grd')
        surfer.write_grd(g, p, fmt='dsrb')
        self.assertEqual(self.check_gdal_reads(p, g), 'GS7BG')

    def test_gdal_reads_our_aaigrid(self):
        g = make_grid(25.0, 25.0)
        p = self.path('w.asc')
        arcascii.write_asc(g, p)
        self.assertEqual(self.check_gdal_reads(p, g), 'AAIGrid')

    def test_gdal_reads_our_gxf(self):
        g = make_grid()
        p = self.path('w.gxf')
        geosoft.write_gxf(g, p)
        self.assertEqual(self.check_gdal_reads(p, g), 'GXF')

    def test_gdal_reads_our_zmap(self):
        g = make_grid()
        p = self.path('w.zmap')
        zmap.write_zmap(g, p)
        # GDAL treats ZMAP limits as cell edges unless told the nodes are points
        self.assertEqual(self.check_gdal_reads(p, g, {'ZMAP_PIXEL_IS_POINT': 'TRUE'}), 'ZMap')

    # (b) GDAL writers -> our readers
    def test_we_read_gdal_gsag(self):
        g = make_grid()
        r = surfer.read_grd(self.gdal_write(g, self.path('g.grd'), 'GSAG'))
        self.assertEqual(r.metadata['surfer_variant'], 'DSAA')
        self.assertGridEqual(g, r, value_tol=1e-6)

    def test_we_read_gdal_gsbg(self):
        g = make_grid()
        r = surfer.read_grd(self.gdal_write(g, self.path('g.grd'), 'GSBG'))
        self.assertEqual(r.metadata['surfer_variant'], 'DSBB')
        self.assertGridEqual(g, r, value_tol=1e-4)

    def test_we_read_gdal_gs7bg(self):
        g = make_grid()
        r = surfer.read_grd(self.gdal_write(g, self.path('g.grd'), 'GS7BG'))
        self.assertEqual(r.metadata['surfer_variant'], 'DSRB')
        self.assertGridEqual(g, r)

    def test_we_read_gdal_aaigrid(self):
        g = make_grid(25.0, 25.0)
        r = arcascii.read_asc(self.gdal_write(g, self.path('g.asc'), 'AAIGrid'))
        self.assertGridEqual(g, r, value_tol=1e-6)

    def test_we_read_gdal_zmap(self):
        # GDAL can CreateCopy ZMap (bonus beyond the read-only GXF); with
        # ZMAP_PIXEL_IS_POINT it writes node coordinates like Petrel does.
        g = make_grid()
        p = self.gdal_write(g, self.path('g.zmap'), 'ZMap', {'ZMAP_PIXEL_IS_POINT': 'TRUE'})
        r = zmap.read_zmap(p)
        self.assertGridEqual(g, r, value_tol=1e-6)


# ----------------------------------------------------------- Geosoft GRD
def _harmonica():
    if not os.path.exists(HARMONICA_REF):
        return None
    if importlib.util.find_spec('numpy') is None or importlib.util.find_spec('xarray') is None:
        return None
    spec = importlib.util.spec_from_file_location('oasis_montaj_grd', HARMONICA_REF)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError:
        return None
    return mod


def build_geosoft(values, nx, ny, dx, dy, x0, y0, rot=0.0, kx=1, compressed=False,
                  vectors_per_block=3, es=4, sf=2, zbase=0.0, zmult=1.0):
    """Hand-build a Geosoft v2 grid.  ``values`` are in file order (one
    vector after another)."""
    hdr = bytearray(512)
    if es == 4 and sf == 2:
        raw = struct.pack('<%df' % len(values), *[-1e32 if v != v else v for v in values])
        ne, nv = (nx, ny) if kx == 1 else (ny, nx)
    else:
        raise ValueError('test helper only builds float grids')
    struct.pack_into('<5i', hdr, 0, es + (1024 if compressed else 0), sf, ne, nv, kx)
    de, dv = (dx, dy) if kx == 1 else (dy, dx)
    struct.pack_into('<7d', hdr, 20, de, dv, x0, y0, rot, zbase, zmult)
    hdr[76:76 + 5] = b'built'
    if not compressed:
        return bytes(hdr) + raw
    per = vectors_per_block * ne * es
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


class GeosoftGrd(GridAssertions):
    def test_compressed_hand_built(self):
        g = make_grid()
        data = build_geosoft(list(g.values), g.nx, g.ny, g.dx, g.dy, g.x0, g.y0, compressed=True)
        self.assertEqual(struct.unpack('<i', data[:4])[0], 1028)
        r = geosoft.read_grd(data)
        self.assertGridEqual(g, r, value_tol=1e-4)
        self.assertEqual(r.metadata['compression']['n_blocks'], 2)
        self.assertEqual(r.metadata['compression']['vectors_per_block'], 3)
        self.assertTrue(r.metadata['geosoft']['compressed'])
        self.assertEqual(r.name, 'built')

    def test_kx_minus_one(self):
        # vectors along Y: file order is column after column, south to north
        g = make_grid()
        col_order = [g.get(i, j) for i in range(g.nx) for j in range(g.ny)]
        data = build_geosoft(col_order, g.nx, g.ny, g.dx, g.dy, g.x0, g.y0, kx=-1)
        r = geosoft.read_grd(data)
        self.assertGridEqual(g, r, value_tol=1e-4)
        self.assertEqual(r.metadata['geosoft']['KX'], -1)

    def test_scaling_and_int_dummies(self):
        # int16 grid: Z = stored / ZMULT + ZBASE, -32767 = dummy
        hdr = bytearray(512)
        struct.pack_into('<5i', hdr, 0, 2, 1, 3, 2, 1)
        struct.pack_into('<7d', hdr, 20, 5.0, 5.0, 10.0, 20.0, 0.0, 100.0, 2.0)
        raw = struct.pack('<6h', 0, 2, -32767, 4, -6, 32767)
        r = geosoft.read_grd(bytes(hdr) + raw)
        self.assertEqual(list(r.values[:2]), [100.0, 101.0])
        self.assertTrue(r.values[2] != r.values[2])
        self.assertEqual(list(r.values[3:]), [102.0, 97.0, 100.0 + 32767 / 2.0])
        with self.assertRaises(ValueError):
            geosoft.read_grd(bytes(hdr[:512])[:4] + struct.pack('<i', 3) + bytes(hdr[8:]) + raw)

    def test_against_harmonica(self):
        oasis = _harmonica()
        if oasis is None:
            self.skipTest('optional cross-validator unavailable: harmonica reference reader (numpy + xarray) not available')
        import numpy as np
        g = make_grid(rotation=30.0)
        with tempfile.TemporaryDirectory() as d:
            for dtype, tol in (('float', 1e-4), ('short', (369.5 + 50.0) / 65000.0)):
                p = os.path.join(d, 'ours_%s.grd' % dtype)
                geosoft.write_grd(g, p, dtype=dtype)
                da = oasis.load_oasis_montaj_grid(p)
                self.assertEqual(da.shape, (g.ny, g.nx))
                exp = np.array(g.values).reshape(g.ny, g.nx)
                self.assertTrue(np.array_equal(np.isnan(da.values), np.isnan(exp)))
                self.assertTrue(np.allclose(np.nan_to_num(da.values), np.nan_to_num(exp), atol=tol))
                for i, j in ((0, 0), (6, 0), (0, 4), (6, 4), (3, 2)):
                    x, y = g.node_xy(i, j)
                    self.assertAlmostEqual(float(da.easting.values[j, i]), x, places=5)
                    self.assertAlmostEqual(float(da.northing.values[j, i]), y, places=5)
            # and the hand-built compressed grid decodes identically in both readers
            p = os.path.join(d, 'comp.grd')
            g2 = make_grid()
            with open(p, 'wb') as fh:
                fh.write(build_geosoft(list(g2.values), g2.nx, g2.ny, g2.dx, g2.dy, g2.x0, g2.y0,
                                       compressed=True))
            da = oasis.load_oasis_montaj_grid(p)
            ours = np.array(geosoft.read_grd(p).values).reshape(g2.ny, g2.nx)
            self.assertTrue(np.array_equal(np.isnan(da.values), np.isnan(ours)))
            self.assertTrue(np.allclose(np.nan_to_num(da.values), np.nan_to_num(ours)))


# ------------------------------------------------------------------- GXF
# GXF spec (rev 3, section 6) 10 x 8 example.  The spec prints #TRANSFORM
# 5.0E-03 / -118.835 for its compressed listing but that is 10x off its own
# uncompressed listing; 5.0E-04 / -11.8835 reproduces it exactly.
GXF_SPEC_PLAIN = """#POINTS
10
#ROWS
8
#DUMMY
-9999
#GRID
-9999 -9999 -9999 -9999 -9999 -9999 -9999 -9999 -9999 -9999
-9999 -9999 -9999 -9999 -9999 -9999 -9999 -9999 -9999 -9999
-9999 -9999 -9999 -9999 -9999   1.0   2.5     0 -1.0 -9999
-9999 -9999 -9999 -9999   1.0   1.5   4.5   1.0     0 -9999
-9999 -9999 -9999   1.5   4.8   6.2   1.1 -1.6 -9999 -9999
-9999 -9999   4.6   9.1 11.5 -9999 -9999 -9999 -9999 -9999
-9999 -9999   3.1   1.6     0 -9999 -9999 -9999 -9999 -9999
-9999 -9999 -9999   0.5 -9999 -9999 -9999 -9999 -9999 -9999
"""
GXF_SPEC_COMPRESSED = """#POINTS
 10
#ROWS
 8
#TRANSFORM
 5.0E-04 -11.8835
#GTYPE
 3
#GRID
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!!!!!!!!!!!!!(5@(V^'y,'br!!!
!!!!!!!!!!!!(5@(@J)),(5@'y,!!!
!!!!!!!!!(@J)/h)Nr(7T'UT!!!!!!
!!!!!!)+@*5@*j^!!!!!!!!!!!!!!!
!!!!!!(c|(B^'y,!!!!!!!!!!!!!!!
!!!!!!!!!(*6!!!!!!!!!!!!!!!!!!
"""
GXF_SPEC_REPEAT = """#POINTS
 10
#ROWS
 8
#TRANSFORM
 5.0E-04,-11.8835,"mGal"
#GTYPE
 3
#GRID
$ row 1 - comment lines inside compressed data start with $
\"\"\"%%/!!!
\"\"\"%%/!!!
\"\"\"%%*!!!(5@(V^'y,'br!!!
\"\"\"%%)!!!(5@(@J)),(5@'y,!!!
!!!!!!!!!(@J)/h)Nr(7T'UT!!!!!!
!!!!!!)+@*5@*j^\"\"\"%%*!!!
!!!!!!(c|(B^'y,\"\"\"%%*!!!
!!!!!!!!!(*6\"\"\"%%+!!!
"""


class GxfTests(GridAssertions):
    def test_base90_digits(self):
        self.assertEqual(geosoft._b90('%%%'), 0)
        self.assertEqual(geosoft._b90('%%&'), 1)
        self.assertEqual(geosoft._b90('~~~'), 90 ** 3 - 1)
        self.assertEqual(geosoft._b90('%%/'), 10)
        self.assertEqual(geosoft._b90('(5@'), 3 * 8100 + 16 * 90 + 27)

    def test_spec_example_compressed_matches_plain(self):
        plain = geosoft.read_gxf(GXF_SPEC_PLAIN.encode())
        comp = geosoft.read_gxf(GXF_SPEC_COMPRESSED.encode())
        rep = geosoft.read_gxf(GXF_SPEC_REPEAT.encode())
        self.assertEqual((plain.nx, plain.ny), (10, 8))
        self.assertEqual(plain.metadata['dummy_nodes'], 59)
        self.assertGridEqual(plain, comp)
        self.assertGridEqual(plain, rep)
        self.assertEqual(comp.metadata['gxf']['gtype'], 3)
        self.assertEqual(rep.metadata['gxf']['transform'][2], 'mGal')
        # spot checks: row j=2 (third stored row, SENSE 1 = south first)
        self.assertEqual(list(plain.values[25:29]), [1.0, 2.5, 0.0, -1.0])
        self.assertEqual(plain.get(3, 7), 0.5)       # last stored row = north
        self.assertAlmostEqual(comp.get(4, 5), 11.5)

    def test_hand_built_compressed_with_repeat_and_dummies(self):
        # GTYPE 2, TRANSFORM scale 0.5 offset -10: I90 = (Z + 10) / 0.5
        def enc(v):
            i = int(round((v + 10.0) / 0.5))
            return chr(37 + i // 90) + chr(37 + i % 90)
        def count(n):                       # 2-digit base-90 repeat count
            return chr(37 + n // 90) + chr(37 + n % 90)
        rows = [[1.0, 1.0, 1.0, NAN, 2.5],          # '""' + count 3 + value, dummy, value
                [NAN, NAN, NAN, NAN, -7.5]]         # '""' + count 4 + '!!', value
        body = '""%s%s!!%s\n""%s!!%s\n' % (count(3), enc(1.0), enc(2.5), count(4), enc(-7.5))
        text = ('#TITLE\n"hand built"\n#POINTS\n5\n#ROWS\n2\n#PTSEPARATION\n2\n#RWSEPARATION\n3\n'
                '#XORIGIN\n10\n#YORIGIN\n20\n#TRANSFORM\n0.5 -10\n#GTYPE\n2\n#GRID\n' + body)
        g = geosoft.read_gxf(text.encode())
        self.assertEqual((g.nx, g.ny, g.dx, g.dy, g.x0, g.y0), (5, 2, 2.0, 3.0, 10.0, 20.0))
        self.assertEqual(g.name, 'hand built')
        for j, row in enumerate(rows):
            for i, v in enumerate(row):
                got = g.get(i, j)
                if v != v:
                    self.assertTrue(got != got, (i, j, got))
                else:
                    self.assertAlmostEqual(got, v, msg=(i, j))

    def test_sense_orientations(self):
        # 3 x 2 grid, value = 1 + i + 3*j: SW=1 SE=3 NW=4 NE=6
        cases = {
            1: (3, 2, '1 2 3\n4 5 6\n'),        # bottom-left, rows run right
            -1: (2, 3, '1 4\n2 5\n3 6\n'),      # bottom-left, rows run up
            2: (2, 3, '4 1\n5 2\n6 3\n'),       # upper-left, rows run down
            -2: (3, 2, '4 5 6\n1 2 3\n'),       # upper-left, rows run right
            3: (3, 2, '6 5 4\n3 2 1\n'),        # upper-right, rows run left
            -3: (2, 3, '6 3\n5 2\n4 1\n'),      # upper-right, rows run down
            4: (2, 3, '3 6\n2 5\n1 4\n'),       # bottom-right, rows run up
            -4: (3, 2, '3 2 1\n6 5 4\n'),       # bottom-right, rows run left
        }
        for sense, (points, rows, body) in cases.items():
            horizontal = sense in (1, -2, 3, -4)
            pt, rw = (10, 20) if horizontal else (20, 10)
            text = ('#POINTS\n%d\n#ROWS\n%d\n#PTSEPARATION\n%d\n#RWSEPARATION\n%d\n#XORIGIN\n100\n'
                    '#YORIGIN\n200\n#SENSE\n%d\n#GRID\n%s' % (points, rows, pt, rw, sense, body))
            g = geosoft.read_gxf(text.encode())
            self.assertEqual((g.nx, g.ny, g.dx, g.dy, g.x0, g.y0), (3, 2, 10.0, 20.0, 100.0, 200.0), sense)
            self.assertEqual(list(g.values), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 'SENSE %d' % sense)
            self.assertEqual(g.get(0, 1), 4.0)    # NW
            self.assertEqual(g.get(2, 0), 3.0)    # SE
            if sense != 1:
                self.assertTrue(any('SENSE' in w for w in g.metadata['warnings']))
        with self.assertRaises(ValueError):
            geosoft.read_gxf(b'#POINTS\n1\n#ROWS\n1\n#SENSE\n5\n#GRID\n1\n')

    def test_defaults_and_dummy(self):
        # no #DUMMY -> -1e12 (GDAL default); value on the label line; comments ignored
        text = ('comment line\n#POINTS 2\n#ROWS\n2\nthis comment is skipped\n#GRID\n'
                '1 -1e12\n-1e+12 4\n')
        g = geosoft.read_gxf(text.encode())
        self.assertEqual((g.dx, g.dy, g.x0, g.y0), (1.0, 1.0, 0.0, 0.0))
        self.assertEqual(g.values[0], 1.0)
        self.assertTrue(g.values[1] != g.values[1])
        self.assertTrue(g.values[2] != g.values[2])
        self.assertEqual(g.values[3], 4.0)
        # #UNIT_LENGTH ft
        g2 = geosoft.read_gxf(b'#POINTS\n1\n#ROWS\n1\n#UNIT_LENGTH\nft,0.3048\n#GRID\n7\n')
        self.assertEqual(g2.units, 'ft')


# ----------------------------------------------------------- Geosoft XYZ
XYZ_SAMPLE = """/ ------------------------------------------------------------------------------
/ XYZ EXPORT [08/21/2026]
/ DATABASE   [.\\survey.gdb]
/ ------------------------------------------------------------------------------
/
/      X          Y        Z       mag      radalt
/==============================================
Line 15700
//Flight 1
//Date 2026/01/01
 500000.0  4500000.0  1200.5  52345.1  80.0
 500010.0  4500000.0  *       52346.2  81.0
 500020.0  4500000.0  1200.9  *        *
Tie 5
//Flight 2
 500000.0  4500100.0  1201.0  52340.0  79.5
 500000.0  4500200.0  1201.5  52341.0  79.0
"""


class XyzTests(unittest.TestCase):
    def test_read_sample(self):
        ps = geosoft.read_xyz(XYZ_SAMPLE.encode())
        self.assertEqual(ps.n, 5)
        self.assertEqual(ps.metadata['channels'], ['X', 'Y', 'Z', 'mag', 'radalt'])
        self.assertEqual(sorted(ps.attributes), ['date', 'flight', 'line', 'line_type', 'mag', 'radalt'])
        self.assertEqual(ps.attributes['line'], ['15700'] * 3 + ['5'] * 2)
        self.assertEqual(ps.attributes['line_type'], ['Line'] * 3 + ['Tie'] * 2)
        self.assertEqual(ps.attributes['flight'], ['1', '1', '1', '2', '2'])
        self.assertEqual(ps.attributes['date'], ['2026/01/01'] * 3 + [None, None])
        self.assertEqual(ps.point(0), (500000.0, 4500000.0, 1200.5))
        self.assertTrue(math.isnan(ps.point(1)[2]))
        self.assertEqual(ps.attributes['mag'][1], 52346.2)
        self.assertTrue(math.isnan(ps.attributes['mag'][2]))
        self.assertTrue(math.isnan(ps.attributes['radalt'][2]))
        self.assertEqual(ps.metadata['dummies'], 3)
        self.assertEqual(ps.provenance['format'], 'geosoft_xyz')
        self.assertEqual(ps.metadata['warnings'], [])

    def test_write_and_reread(self):
        ps = geosoft.read_xyz(XYZ_SAMPLE.encode())
        data = geosoft.write_xyz(ps, io.BytesIO())
        text = data.decode()
        self.assertTrue(text.startswith('/ X Y Z mag radalt\n'))
        self.assertIn('\nLine 15700\n//Flight 1\n//Date 2026/01/01\n', text)
        self.assertIn('\nTie 5\n//Flight 2\n', text)
        self.assertIn('500010.0 4500000.0 * 52346.2 81.0\n', text)
        ps2 = geosoft.read_xyz(data)
        self.assertEqual(ps2.n, ps.n)
        for key in ('line', 'line_type', 'flight', 'date'):
            self.assertEqual(ps2.attributes[key], ps.attributes[key], key)
        for key in ('mag', 'radalt'):
            for a, b in zip(ps.attributes[key], ps2.attributes[key]):
                if a != a:
                    self.assertTrue(b != b)
                else:
                    self.assertEqual(a, b)
        for k in range(ps.n):
            for a, b in zip(ps.point(k), ps2.point(k)):
                self.assertTrue((a != a and b != b) or a == b)

    def test_comma_header_and_abbreviated_lines(self):
        text = '/X,Y,Z,K\nL10\n1,2,3,4\nT20\n5,6,7,*\nBase\n8,9,10,11\n'
        ps = geosoft.read_xyz(text.encode())
        self.assertEqual(ps.metadata['channels'], ['X', 'Y', 'Z', 'K'])
        self.assertEqual(ps.attributes['line'], ['10', '20', 'Base'])
        self.assertEqual(ps.attributes['line_type'], ['Line', 'Tie', 'Base'])
        self.assertEqual(ps.attributes['K'][0], 4.0)
        self.assertTrue(math.isnan(ps.attributes['K'][1]))
        # no header at all -> X Y Z + generated names, warning recorded
        ps2 = geosoft.read_xyz(b'Line 1\n1 2 3 4\n')
        self.assertEqual(ps2.metadata['channels'], ['X', 'Y', 'Z', 'ch4'])
        self.assertTrue(ps2.metadata['warnings'])


# ------------------------------------------------------------------- UBC
class UbcTests(unittest.TestCase):
    def test_ordering_3x2x4(self):
        mesh = b'3 2 4\n1000 2000 500\n3*10\n2*20\n4*5\n'
        ne, nn, nz = 3, 2, 4
        # UBC order: z fastest (top -> bottom), then easting, then northing.
        # Encode each cell's UBC position so any mix-up shows: v = kz + 10*ix + 100*iy
        values = [kz + 10 * ix + 100 * iy for iy in range(nn) for ix in range(ne) for kz in range(nz)]
        self.assertEqual(values[0], 0)           # top-south-west cell is value #1
        model = ('\n'.join(str(v) for v in values) + '\n').encode()
        bm = ubc.read_ubc(mesh, model, name='code')
        self.assertEqual(bm.count, [3, 2, 4])
        self.assertEqual(bm.block_size, [10.0, 20.0, 5.0])
        self.assertEqual(bm.origin, [1000.0, 2000.0, 480.0])        # Z0 - sum(dz)
        v = bm.attributes['code']['values']
        self.assertEqual(v[bm.index(0, 0, 3)], 0)                   # top (k = nz-1) south-west
        self.assertEqual(v[bm.index(0, 0, 0)], 3)                   # bottom (k = 0) south-west
        self.assertEqual(v[bm.index(2, 0, 3)], 20)                  # top, east-most, south
        self.assertEqual(v[bm.index(0, 1, 3)], 100)                 # top, west, north
        self.assertEqual(v[bm.index(2, 1, 0)], 3 + 20 + 100)        # bottom north-east
        self.assertEqual(bm.centroid(0, 0, 3), (1005.0, 2010.0, 497.5))
        self.assertEqual(bm.centroid(0, 0, 0), (1005.0, 2010.0, 482.5))
        self.assertEqual(bm.provenance['format'], 'ubc')
        # round trip reproduces the exact UBC order
        mesh2, model2 = ubc.write_ubc(bm, io.BytesIO(), io.BytesIO(), 'code')
        self.assertEqual([int(float(t)) for t in model2.decode().split()], values)
        self.assertEqual(mesh2.decode().splitlines()[:2], ['3 2 4', '1000.0 2000.0 500.0'])
        bm2 = ubc.read_ubc(mesh2, model2, name='code')
        self.assertEqual(list(bm2.attributes['code']['values']), list(v))
        self.assertEqual(bm2.origin, bm.origin)

    def test_explicit_widths_and_paths(self):
        mesh = b'2 2 2\n0 0 100\n10 10\n10 10\n50 50\n'
        with tempfile.TemporaryDirectory() as d:
            mp = os.path.join(d, 'm.msh')
            dp = os.path.join(d, 'dens.mod')
            with open(mp, 'wb') as fh:
                fh.write(mesh)
            with open(dp, 'w') as fh:
                fh.write('\n'.join(str(k) for k in range(8)) + '\n')
            bm = ubc.read_ubc(mp, models={'density': dp})
            self.assertEqual(bm.origin, [0.0, 0.0, 0.0])
            self.assertEqual(bm.name, 'm')
            self.assertEqual(bm.provenance['path'], mp)
            self.assertEqual(bm.provenance['model_paths'], {'density': dp})
            self.assertEqual(bm.attributes['density']['values'][bm.index(0, 0, 1)], 0.0)
            self.assertEqual(bm.attributes['density']['values'][bm.index(0, 0, 0)], 1.0)
            out_m = os.path.join(d, 'out.msh')
            out_d = os.path.join(d, 'out.mod')
            self.assertEqual(ubc.write_ubc(bm, out_m, out_d, 'density'), (out_m, out_d))
            self.assertTrue(os.path.exists(out_d))

    def test_variable_widths_rejected(self):
        with self.assertRaises(ValueError) as cm:
            ubc.read_ubc(b'2 1 1\n0 0 0\n10 20\n5\n5\n')
        self.assertIn('variable', str(cm.exception))
        with self.assertRaises(ValueError):
            ubc.read_ubc(b'2 1 1\n0 0 0\n2*10\n5\n5\n', b'1\n')    # too few model values
        bm = BlockModel([0, 0, 0], [1, 1, 1], [1, 1, 1], azimuth=10.0)
        bm.add_attribute('a', [1.0])
        with self.assertRaises(ValueError):
            ubc.write_ubc(bm, io.BytesIO(), io.BytesIO(), 'a')


# ------------------------------------------------- Irap / ZMAP / CPS-3
class ColumnMajorOrientation(GridAssertions):
    """3 x 2 grid with value = 1 + i + 3*j: north-west node = 4, south-east
    node = 3, so a row/column or north/south mix-up is caught."""

    def check(self, g):
        self.assertEqual((g.nx, g.ny), (3, 2))
        self.assertEqual((g.x0, g.y0, g.dx, g.dy), (100.0, 200.0, 10.0, 20.0))
        self.assertEqual(list(g.values), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(g.get(0, 1), 4.0)    # NW
        self.assertEqual(g.get(2, 0), 3.0)    # SE

    def test_irap_row_major_south_first(self):
        data = b'-996 2 10.0 20.0\n100.0 120.0 200.0 220.0\n3 0.0 100.0 200.0\n0 0 0 0 0 0 0\n1 2 3 4 5 6\n'
        self.check(irap.read_irap(data))
        g = irap.read_irap(data)
        self.assertEqual(irap.read_irap(irap.write_irap(g, io.BytesIO())).values, g.values)

    def test_zmap_column_major_north_down(self):
        data = (b'! comment\n@t, GRID, 5\n20, -9999.0, , 7, 1\n2, 3, 100.0, 120.0, 200.0, 220.0\n'
                b'0.0, 0.0, 0.0\n@\n4 1\n5 2\n6 3\n')
        self.check(zmap.read_zmap(data))
        # written form: first column (west) first, north to south, fixed width
        g = zmap.read_zmap(data)
        out = zmap.write_zmap(g, io.BytesIO()).decode().splitlines()
        self.assertEqual(out[7].split(), ['4.0000000', '1.0000000'])
        self.assertEqual(out[8].split(), ['5.0000000', '2.0000000'])
        self.assertEqual(out[9].split(), ['6.0000000', '3.0000000'])
        # fixed-width fields that touch (no separator) still parse
        touching = (b'@t, GRID, 5\n6, -999.0, , 1, 1\n2, 3, 100.0, 120.0, 200.0, 220.0\n0.0, 0.0, 0.0\n@\n'
                    b'   4.0   1.0\n   5.0   2.0\n-999.0   3.0\n')
        g2 = zmap.read_zmap(touching)
        self.assertTrue(g2.get(2, 1) != g2.get(2, 1))
        self.assertEqual(g2.get(2, 0), 3.0)

    def test_cps3_column_major_north_down(self):
        data = (b'FSASCI 0 1 "Computed" 0 1.0E+30\nFSATTR 0 0\nFSLIMI 100.0 120.0 200.0 220.0 1.0 6.0\n'
                b'FSNROW 2 3\nFSXINC 10.0 20.0\n-> a comment\n4 1 5 2\n6 3\n')
        g = cps3.read_cps3(data)
        self.check(g)
        self.assertEqual(g.provenance['format'], 'cps3')
        self.assertTrue(any('unverified' in w for w in g.metadata['warnings']))
        self.assertEqual(g.metadata['cps3']['null'], 1e30)
        nulls = data.replace(b'4 1 5 2', b'4 1.0E+30 5 2')
        g2 = cps3.read_cps3(nulls)
        self.assertTrue(g2.get(0, 0) != g2.get(0, 0))
        self.assertEqual(g2.metadata['null_nodes'], 1)


if __name__ == '__main__':
    unittest.main()
