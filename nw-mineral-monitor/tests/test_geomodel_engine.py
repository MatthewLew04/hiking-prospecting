"""geomodel numerical engine: interpolation, kriging, stratigraphy, block
models, slicing, iso-surfaces, workings and the kit builder (offline)."""
import json
import math
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel.model import (Grid2D, Mesh, LineSet, PointSet, BlockModel, Drillholes,  # noqa: E402
                            ImagePlane, Project, utm_crs)
from geomodel import interp, stratigraphy, blockmodel, slicing, workings, kit  # noqa: E402


def synthetic(n=150, seed=1):
    rnd = random.Random(seed)
    f = lambda x, y: 100 + 0.01 * x + 0.02 * y + 5 * math.sin(x / 50.0)
    pts = [(rnd.uniform(0, 500), rnd.uniform(0, 300), 0.0) for _ in range(n)]
    return pts, [f(p[0], p[1]) for p in pts], f


class InterpTests(unittest.TestCase):
    def test_idw_and_rbf_recover_smooth_surface(self):
        pts, vals, f = synthetic()
        tg = [(250, 150, 0), (10, 10, 0), (480, 290, 0)]
        truth = [f(*t[:2]) for t in tg]
        est = interp.idw(pts, vals, tg, dim=2)
        for e, t in zip(est, truth):
            self.assertLess(abs(e - t), 1.5)
        for kernel in interp.RBF_KERNELS:
            r = interp.RBF(kernel=kernel, dim=2).fit(pts, vals)
            a, b = r.predict(tg), r.predict_np(tg)
            for x, y, t in zip(a, b, truth):
                self.assertLess(abs(x - y), 1e-6, kernel)
                self.assertLess(abs(x - t), 1.0, kernel)
            # exact at the data
            self.assertLess(abs(r.predict([pts[7]])[0] - vals[7]), 1e-6)

    def test_kriging_exact_and_variogram_fit(self):
        pts, vals, f = synthetic()
        exp = interp.empirical_variogram(pts, vals, dim=2)
        self.assertTrue(exp and all(e['pairs'] > 0 for e in exp))
        vg = interp.fit_variogram(exp)
        self.assertGreater(vg.sill, 0)
        est, var = interp.ordinary_kriging(pts, vals, [pts[3], (250, 150, 0)], vg, dim=2)
        self.assertAlmostEqual(est[0], vals[3], places=9)
        self.assertAlmostEqual(var[0], 0.0, places=9)
        self.assertLess(abs(est[1] - f(250, 150)), 1.5)
        self.assertGreater(var[1], 0)
        j = vg.to_json()
        self.assertEqual(interp.Variogram.from_json(j).gamma(100.0), vg.gamma(100.0))

    def test_anisotropy_distance(self):
        an = interp.Anisotropy([100, 20, 5], azimuth=0)
        self.assertAlmostEqual(an.distance((0, 0, 0), (0, 100, 0)), 1.0)
        self.assertAlmostEqual(an.distance((0, 0, 0), (100, 0, 0)), 5.0)
        self.assertAlmostEqual(an.distance((0, 0, 0), (0, 0, 5)), 1.0)

    def test_grid_from_points_methods(self):
        pts, vals, f = synthetic()
        for method in ('rbf', 'idw', 'ok', 'nn'):
            g = interp.grid_from_points(pts, vals, method=method, n=25)
            self.assertEqual(g.metadata['interpolation']['method'], method)
            v = g.sample(250, 150)
            self.assertLess(abs(v - f(250, 150)), 3.0, method)

    def test_implicit_sphere(self):
        rnd = random.Random(3)
        sp, sv = [], []
        for _ in range(150):
            th, ph = rnd.uniform(0, 2 * math.pi), math.acos(rnd.uniform(-1, 1))
            for r, s in ((50, 0.0), (60, 10.0), (40, -10.0)):
                sp.append((r * math.sin(ph) * math.cos(th), r * math.sin(ph) * math.sin(th), r * math.cos(ph)))
                sv.append(s)
        rb = interp.RBF(kernel='linear', dim=3).fit(sp, sv)
        a, b, c = rb.predict_np([(45, 0, 0), (0, 50, 0), (0, 0, 55)])
        self.assertLess(abs(a + 5), 1.0)
        self.assertLess(abs(b), 1.0)
        self.assertLess(abs(c - 5), 1.0)


class StratigraphyTests(unittest.TestCase):
    def setUp(self):
        self.topo = Grid2D(21, 16, 0, 0, 50, 50, values=[300 + 0.05 * i * 50 + 10 * math.sin(j / 3.0) for j in range(16) for i in range(21)], name='topo', role='topography')
        rnd = random.Random(2)
        self.pts = PointSet(name='contact A')
        for _ in range(40):
            x, y = rnd.uniform(0, 1000), rnd.uniform(0, 750)
            self.pts.add(x, y, 250 + 0.02 * x)
        self.units = [{'name': 'Alluvium', 'contact': 'erosion', 'base': self.pts},
                      {'name': 'Tuff', 'contact': 'deposit', 'base': 200.0},
                      {'name': 'Rhyolite', 'contact': 'deposit', 'base': Grid2D(21, 16, 0, 0, 50, 50, values=[150 + 0.1 * i * 50 for j in range(16) for i in range(21)])},
                      {'name': 'Basement', 'base': None}]

    def test_build_is_monotonic_and_rules_apply(self):
        sm, bases, topo = stratigraphy.build_stratigraphy(self.topo, self.units)
        self.assertEqual([u['base'] is not None for u in sm.units], [True, True, True, False])
        for idx in range(len(topo.values)):
            seq = [topo.values[idx]] + [g.values[idx] for g in bases if g]
            for a in range(len(seq) - 1):
                self.assertGreaterEqual(seq[a], seq[a + 1] - 1e-9)
        grids = {g.id: g for g in bases if g}
        col = stratigraphy.column_at(sm, grids, 500, 300, topo)
        self.assertEqual([c['name'] for c in col], ['Alluvium', 'Tuff', 'Rhyolite', 'Basement'])
        self.assertIsNone(col[-1]['base'])
        # the rhyolite base (150 + 0.1x) rises above the tuff's constant 200 m base east of x=500:
        # a DEPOSIT is on-lapped, so tuff's base is lifted there and rhyolite pinches to zero
        self.assertGreaterEqual(col[1]['base'], 200.0 - 1e-9)
        self.assertEqual(stratigraphy.unit_at(sm, grids, 500, 300, col[1]['base'] + 1, topo), 'Tuff')
        self.assertIsNone(stratigraphy.unit_at(sm, grids, 500, 300, 5000, topo))
        vols = stratigraphy.stratigraphy_volumes(sm, grids, topo)
        self.assertEqual(len(vols), 4)
        for v in vols:
            v.validate()
            self.assertGreater(v.n_triangles, 100)
        ribs = slicing.stratigraphy_section(sm, grids, topo, (0, 100), (1000, 600), 50)
        self.assertEqual(len(ribs), 4)
        bm = blockmodel.create_blockmodel((0, 0, 100, 1000, 750, 400), (50, 50, 25))
        stratigraphy.tag_blockmodel(bm, sm, grids, topo)
        tags = set(bm.attributes['unit']['values'])
        self.assertTrue({'Basement', 'Alluvium', 'Tuff', ''} <= tags)


class BlockModelTests(unittest.TestCase):
    def test_estimate_ok_idw_domain_and_grade_tonnage(self):
        rnd = random.Random(5)
        bm = blockmodel.create_blockmodel((0, 0, 100, 1000, 750, 400), (50, 50, 25))
        self.assertEqual(bm.count, [20, 15, 12])
        samples = PointSet(name='au')
        for _ in range(120):
            x, y, z = rnd.uniform(0, 1000), rnd.uniform(0, 750), rnd.uniform(100, 400)
            samples.add(x, y, z, au=max(0, 2 + 0.003 * x - 0.004 * abs(z - 250) + rnd.gauss(0, 0.3)))
        blockmodel.estimate(bm, samples, 'au', method='ok', max_points=12)
        est = bm.attributes['au_est']['values']
        self.assertEqual(sum(1 for v in est if v == v), bm.n)
        self.assertIn('au_var', bm.attributes)
        self.assertEqual(bm.metadata['estimates'][0]['method'], 'ok')
        bm.add_attribute('dom', ['A' if i % 2 else 'B' for i in range(bm.n)], kind='category')
        blockmodel.estimate(bm, samples, 'au', method='idw', domain='dom', domain_value='A', out_name='au_a')
        vals = bm.attributes['au_a_est']['values']
        self.assertEqual(sum(1 for v in vals if v == v), bm.n // 2)
        rows = blockmodel.grade_tonnage(bm, 'au_est', [0.0, 100.0])
        self.assertEqual(rows[0]['blocks'], bm.n)
        self.assertEqual(rows[1]['blocks'], 0)
        ps = blockmodel.blockmodel_to_points(bm, 'au_est')
        self.assertEqual(ps.n, bm.n)
        img = slicing.blockmodel_plane_sample(bm, 'au_est', (500, 375, 250), (0, 1, 0), resolution=25)
        self.assertGreater(sum(1 for v in img['values'] if v == v), 100)

    def test_composite(self):
        ps = PointSet(name='s')
        for k in range(10):
            ps.add(0, 0, -k - 0.5, **{'hole': 'H1', 'from': k, 'to': k + 1, 'au': float(k)})
        c = blockmodel.composite(ps, 'au', target_length=2.0)
        self.assertEqual(c.n, 5)
        self.assertAlmostEqual(c.numeric('au')[0], 0.5)


class SlicingTests(unittest.TestCase):
    def sphere_field(self, n=21, r=30.0):
        field = []
        for k in range(n):
            for j in range(n):
                for i in range(n):
                    x, y, z = (i - 10) * 5, (j - 10) * 5, (k - 10) * 5
                    field.append(math.sqrt(x * x + y * y + z * z) - r)
        return field

    def test_isosurface_and_plane_cuts(self):
        field = self.sphere_field()
        m = slicing.isosurface(field, (21, 21, 21), (-50, -50, -50), (5, 5, 5), 0.0)
        m.validate()
        radii = [math.sqrt(sum(c * c for c in m.vertex(i))) for i in range(m.n_vertices)]
        self.assertLess(max(radii), 30.01)
        self.assertGreater(min(radii), 29.0)
        outward = 0
        for t in range(m.n_triangles):
            a, b, c = [m.vertex(i) for i in m.triangle(t)]
            u = [b[i] - a[i] for i in range(3)]
            w = [c[i] - a[i] for i in range(3)]
            nrm = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
            cen = [(a[i] + b[i] + c[i]) / 3 for i in range(3)]
            outward += sum(nrm[i] * cen[i] for i in range(3)) > 0
        self.assertEqual(outward, m.n_triangles)
        for nrm, pt, expect in (((0, 0, 1), (0, 0, 0), 2 * math.pi * 30), ((1, 1, 0), (0, 0, 0), 2 * math.pi * 30),
                                ((0, 0, 1), (0, 0, 12.5), 2 * math.pi * math.sqrt(30 ** 2 - 12.5 ** 2))):
            ls = slicing.mesh_plane_intersection(m, pt, nrm)
            self.assertEqual(len(ls.parts), 1, (nrm, pt))
            self.assertLess(abs(ls.length() - expect) / expect, 0.01)
            self.assertEqual(ls.part_xyz(0)[0], ls.part_xyz(0)[-1])

    def test_profile_and_near_plane(self):
        g = Grid2D(11, 11, 0, 0, 10, 10, values=[float(i) for j in range(11) for i in range(11)])
        prof = slicing.grid_profile(g, (0, 50), (100, 50), 10)
        self.assertAlmostEqual(prof[-1][3], 10.0)
        ws = LineSet(role='workings')
        ws.add_polyline([(0, 50, 5), (100, 50, 5)], {'type': 'adit'})
        ws.add_polyline([(50, 0, 8), (50, 100, 8)], {'type': 'drift'})
        near = slicing.lineset_near_plane(ws, (50, 50, 5), (1, 0, 0), 10)
        self.assertEqual(len(near.parts), 1)
        self.assertEqual(near.features[0]['type'], 'drift')


class WorkingsTests(unittest.TestCase):
    def test_constructors_units_and_geojson(self):
        topo = Grid2D(3, 3, 0, 0, 500, 500, values=[1500.0] * 9)
        ws = workings.new_workings('w', mine='Test')
        workings.add_adit(ws, (100, 100, 0), 90, 1000, units_in='ft', terrain=topo, name='No. 1 adit', grade_pct=0.5)
        workings.add_shaft(ws, (200, 200, 0), 300, units_in='ft', terrain=topo, name='shaft')
        workings.add_level_working(ws, [(0, 0), (50, 0), (50, 50)], 1400.0, kind='drift', level='300')
        workings.add_raise(ws, (50, 50, 1400), (50, 50, 1450))
        workings.add_decline(ws, (0, 0, 1500), [(0, 100, -10), (90, 100, -10)])
        self.assertEqual(len(ws.parts), 5)
        adit = ws.part_xyz(0)
        self.assertAlmostEqual(adit[1][0] - adit[0][0], 1000 * 0.3048, places=6)
        self.assertAlmostEqual(adit[0][2], 1500.0)
        self.assertAlmostEqual(adit[1][2] - adit[0][2], 1000 * 0.3048 * 0.005, places=6)
        shaft = ws.part_xyz(1)
        self.assertAlmostEqual(shaft[0][2] - shaft[1][2], 300 * 0.3048, places=6)
        s = workings.summary(ws)
        self.assertEqual(s['n_features'], 5)
        self.assertIn('adit', s['by_type'])
        gj = workings.to_geojson(ws, utm_crs(12, True))
        self.assertEqual(len(gj['features']), 5)
        lon, lat = gj['features'][0]['geometry']['coordinates'][0][:2]
        self.assertTrue(-120 < lon < -110 and 0 < lat < 1)   # UTM 12N origin region
        st = workings.stope_prism([(0, 0), (10, 0), (10, 10), (5, 5), (0, 10)], 100, 120, name='stope')
        st.validate()
        self.assertEqual(st.n_triangles, 2 * 3 + 2 * 5)    # caps (3 tris each for 5 verts) + 5 quads

    def test_georeference_plan_and_section(self):
        ip = ImagePlane('data:,', 400, 300, plane='plan')
        workings.georef_plan_from_scale(ip, (200, 150), (1000, 2000), 0.5, rotation_deg=0, elevation=50)
        x, y, z = ip.pixel_to_world(200, 150)
        self.assertAlmostEqual(x, 1000)
        self.assertAlmostEqual(y, 2000)
        self.assertEqual(z, 50)
        x2, y2, _ = ip.pixel_to_world(300, 150)
        self.assertAlmostEqual(x2, 1050)
        x3, y3, _ = ip.pixel_to_world(200, 250)
        self.assertAlmostEqual(y3, 1950)       # pixel y down = south
        sec = workings.section_image('data:,', 200, 100, (0, 0), (200, 0), 100, 0)
        self.assertEqual(workings.trace_to_world(sec, [(100, 50)]), [(100.0, 0.0, 50.0)])
        world = workings.trace_to_world(ip, [(200, 150), (300, 150)], level_z=42)
        self.assertEqual(world[0][2], 42)


class KitTests(unittest.TestCase):
    def test_offline_site_model_and_exports(self):
        # offline: no terrain tiles -> topo has no data but the project still builds and exports
        proj = kit.build_site_model(-113.125, 42.147, radius_m=800, name='Silver Hills test', aoi='cassia',
                                    offline=True, zoom=12, log=lambda *a: None)
        kinds = [o.kind for o in proj.objects]
        self.assertIn('grid2d', kinds)
        self.assertIn('stratmodel', kinds)
        self.assertEqual(kinds.count('section'), 2)
        self.assertEqual(proj.crs['epsg'], 32612)
        topo = proj.by_kind('grid2d')[0]
        zr = topo.zrange()
        self.assertTrue('warnings' in topo.metadata or zr[0] == zr[0])   # no cache -> warned; cached tiles -> real Z
        with tempfile.TemporaryDirectory() as td:
            manifest = kit.export_project(proj, td, formats=('json', 'omf2', 'omf1', 'surfer', 'gxf', 'dxf', 'csv'), log=lambda *a: None)
            names = [m[0] for m in manifest]
            self.assertIn('silver-hills-test.geomodel.json', names)
            self.assertIn('silver-hills-test.omf', names)
            back = Project.load(os.path.join(td, 'silver-hills-test.geomodel.json'))
            self.assertEqual(len(back.objects), len(proj.objects))
            from geomodel.formats import omf2
            p2 = omf2.read_omf2(os.path.join(td, 'silver-hills-test.omf'))
            self.assertTrue(any(o.kind == 'grid2d' for o in p2.objects))
            info = kit.describe(os.path.join(td, 'topography.gxf'))
            self.assertIn('gxf', info)
            kit.convert(os.path.join(td, 'topography.gxf'), os.path.join(td, 'topo.zmap'), log=lambda *a: None)
            self.assertTrue(os.path.exists(os.path.join(td, 'topo.zmap')))

    def test_aoi_lookup_and_clipping(self):
        self.assertEqual(kit.aoi_for_point(-113.125, 42.147), 'cassia')
        self.assertIsNone(kit.aoi_for_point(-100, 30))
        ring = [(-10, -10), (20, -10), (20, 20), (-10, 20)]
        c = kit.clip_ring_rect(ring, 0, 0, 10, 10)
        self.assertEqual(sorted(c), [(0.0, 0.0), (0.0, 10.0), (10.0, 0.0), (10.0, 10.0)])
        pts, tris = kit.subdivide_triangles([(0, 0), (100, 0), (0, 100)], [(0, 1, 2)], 30)
        self.assertTrue(all(math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]) <= 30 + 1e-9 for t in tris for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0]))))


if __name__ == '__main__':
    unittest.main()
