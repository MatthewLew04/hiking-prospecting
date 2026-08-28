"""Mesh / line / point interchange formats: OBJ, DXF, GOCAD, Leapfrog .msh."""
import sys, unittest, tempfile, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import io
import math
import struct

from geomodel.model import Mesh, LineSet, PointSet, NAN
from geomodel.formats import obj as objfmt
from geomodel.formats import dxf as dxffmt
from geomodel.formats import gocad as gocadfmt
from geomodel.formats import lfmsh
from geomodel.formats import sniff

from gm_ref import GOCAD_DIR as REF_GOCAD, DXF_DIR as REF_DXF   # noqa: E402


def pyramid(name='pyramid'):
    """5 vertices, 6 triangles (square base + 4 sides)."""
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


class TempDirMixin(object):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='nwmm-geomodel-')

    def path(self, name):
        return os.path.join(self.tmp, name)


# ------------------------------------------------------------------------ OBJ
class TestOBJ(TempDirMixin, unittest.TestCase):
    def test_round_trip(self):
        m = pyramid('Test Surface')
        p = objfmt.write_obj(m, self.path('a.obj'))
        self.assertTrue(os.path.exists(p))
        r = objfmt.read_obj(p)
        self.assertEqual(r.n_vertices, 5)
        self.assertEqual(r.n_triangles, 6)
        self.assertEqual(list(r.vertices), [float(v) for v in m.vertices])
        self.assertEqual(list(r.triangles), list(m.triangles))
        self.assertEqual(r.name, 'Test_Surface')
        self.assertEqual(r.provenance['format'], 'obj')
        self.assertEqual(r.provenance['path'], p)
        self.assertEqual(r.metadata['warnings'], [])
        r.validate()

    def test_bytes_in_bytesio_out(self):
        m = pyramid()
        data = objfmt.write_obj(m, io.BytesIO())
        self.assertIsInstance(data, bytes)
        self.assertTrue(data.startswith(b'# Wavefront OBJ'))
        r = objfmt.read_obj(data)
        self.assertEqual(r.n_triangles, 6)
        self.assertIsNone(r.provenance['path'])

    def test_index_forms_negative_polygons_groups_lines(self):
        txt = (b"# comment\n"
               b"mtllib x.mtl\n"
               b"o thing\n"
               b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
               b"vt 0 0\nvn 0 0 1\n"
               b"usemtl red\n"
               b"g quad\n"
               b"f 1/1/1 2/1/1 3/1/1 4/1/1\n"      # quad -> 2 triangles
               b"g neg\n"
               b"f -1 -2 -3\n"                      # negative (relative) indices
               b"l 1 2 3\n"
               b"f 1//1 2//1 \\\n 3//1\n"           # line continuation, v//vn form
               b"f 1 2 9\n")                        # out of range -> skipped
        r = objfmt.read_obj(txt)
        self.assertEqual(r.n_vertices, 4)
        self.assertEqual(r.n_triangles, 4)
        tri = [tuple(r.triangle(k)) for k in range(r.n_triangles)]
        self.assertEqual(tri[0], (0, 1, 2))
        self.assertEqual(tri[1], (0, 2, 3))
        self.assertEqual(tri[2], (3, 2, 1))
        self.assertEqual(tri[3], (0, 1, 2))
        self.assertEqual(r.metadata['lines'], 1)
        self.assertEqual(r.metadata['groups'], ['thing', 'quad', 'neg'])
        self.assertEqual(r.metadata['polygons_triangulated'], 1)
        self.assertEqual(r.metadata['materials'], ['red'])
        self.assertIn('group', r.attributes)
        self.assertEqual(r.attributes['group']['location'], 'faces')
        self.assertEqual(list(r.attributes['group']['values']), [1, 1, 2, 2])
        self.assertTrue(any('invalid' in w for w in r.metadata['warnings']))
        self.assertTrue(any('polyline' in w for w in r.metadata['warnings']))
        self.assertEqual(r.name, 'thing')

    def test_write_normals(self):
        m = pyramid()
        data = objfmt.write_obj(m, io.BytesIO(), normals=True).decode()
        self.assertEqual(data.count('\nvn '), 5)
        self.assertIn('f 1//1 3//3 2//2', data)
        r = objfmt.read_obj(data.encode())
        self.assertEqual(r.n_triangles, 6)
        self.assertEqual(r.metadata['normals'], 5)

    def test_trimesh_cross_check(self):
        try:
            import trimesh
            import numpy as np
        except ImportError:
            self.skipTest('optional cross-validator unavailable: trimesh / numpy not installed')
        m = pyramid('tm')
        p = objfmt.write_obj(m, self.path('tm.obj'))
        tm = trimesh.load(p, process=False, force='mesh')
        self.assertEqual(tm.vertices.shape, (5, 3))
        self.assertEqual(tm.faces.shape, (6, 3))
        self.assertTrue(np.allclose(tm.vertices, np.array(m.vertices).reshape(-1, 3)))
        self.assertTrue((tm.faces == np.array(m.triangles).reshape(-1, 3)).all())
        # and a trimesh-exported OBJ back through our reader
        p2 = self.path('tm_out.obj')
        tm.export(p2)
        r = objfmt.read_obj(p2)
        self.assertEqual(r.n_vertices, 5)
        self.assertEqual(r.n_triangles, 6)
        self.assertTrue(np.allclose(np.array(r.vertices).reshape(-1, 3), tm.vertices))


# ------------------------------------------------------------------------ DXF
class TestDXF(TempDirMixin, unittest.TestCase):
    def test_write_read_round_trip(self):
        m, ls, ps = pyramid('Test Surface'), sample_lines(), sample_points()
        p = dxffmt.write_dxf([m, ls, ps], self.path('a.dxf'))
        text = Path(p).read_bytes().decode('utf-8')
        self.assertIn('AC1009', text)
        self.assertIn('$INSUNITS', text)
        self.assertIn('$EXTMIN', text)
        self.assertIn('$EXTMAX', text)
        self.assertEqual(text.count('\n3DFACE\r\n'), 6)
        self.assertEqual(text.count('\nPOLYLINE\r\n'), 2)
        self.assertEqual(text.count('\nVERTEX\r\n'), 5)
        self.assertEqual(text.count('\nSEQEND\r\n'), 2)
        self.assertEqual(text.count('\nPOINT\r\n'), 2)
        objs = dxffmt.read_dxf(p)
        kinds = [(o.kind, o.name) for o in objs]
        self.assertEqual(kinds, [('mesh', 'Test_Surface'), ('lineset', 'Drift_300_level'), ('points', 'collars')])
        rm, rl, rp = objs
        self.assertEqual(rm.n_vertices, 5)           # vertices de-duplicated
        self.assertEqual(rm.n_triangles, 6)
        rm.validate()
        self.assertEqual(sorted(rm.vertex(i) for i in range(5)), sorted(m.vertex(i) for i in range(5)))
        self.assertEqual([len(pt) for pt in rl.parts], [3, 2])
        self.assertEqual(rl.part_xyz(0), ls.part_xyz(0))
        self.assertEqual(rp.n, 2)
        self.assertEqual(rp.point(1), (4.0, 5.0, 6.0))
        self.assertEqual(rm.color, [255, 0, 0])        # from the LAYER table (ACI 1)
        self.assertEqual(rm.provenance['format'], 'dxf')
        self.assertEqual(rm.metadata['acadver'], 'AC1009')

    def test_layer_names_and_sanitising(self):
        self.assertEqual(dxffmt.sanitise_layer('Ore Zone #1 (2024)'), 'Ore_Zone_1_2024')
        self.assertEqual(len(dxffmt.sanitise_layer('x' * 60)), 31)
        self.assertEqual(dxffmt.sanitise_layer(''), '0')
        m = pyramid('ignored')
        data = dxffmt.write_dxf(m, io.BytesIO(), layer_names=['TOPO'])
        objs = dxffmt.read_dxf(data)
        self.assertEqual([o.name for o in objs], ['TOPO'])
        data = dxffmt.write_dxf([m, pyramid('ignored')], io.BytesIO())
        self.assertEqual([o.name for o in dxffmt.read_dxf(data)], ['ignored', 'ignored_2'])

    def test_single_object_and_bad_kind(self):
        data = dxffmt.write_dxf(sample_points(), io.BytesIO())
        self.assertIsInstance(data, bytes)
        self.assertEqual(dxffmt.read_dxf(data)[0].n, 2)
        with self.assertRaises(TypeError):
            dxffmt.write_dxf(object(), io.BytesIO())

    def test_reference_min_r12_and_line_endings(self):
        path = REF_DXF / 'min_r12.dxf'
        if not path.exists():
            self.skipTest('optional cross-validator unavailable: reference min_r12.dxf not available')
        objs = dxffmt.read_dxf(str(path))
        kinds = {o.kind: o for o in objs}
        self.assertEqual(set(kinds), {'mesh', 'lineset', 'points'})
        self.assertEqual(kinds['mesh'].n_triangles, 1)
        self.assertEqual(kinds['lineset'].parts, [[0, 1, 2]])
        self.assertEqual(kinds['points'].point(0), (5.0, 5.0, 5.0))
        self.assertEqual(kinds['mesh'].metadata['skipped'], {'TEXT': 1})
        raw = path.read_bytes()
        crlf = raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        self.assertEqual([o.kind for o in dxffmt.read_dxf(crlf)], [o.kind for o in objs])
        self.assertEqual(sniff(head=raw[:64], path='x.dxf'), 'dxf')

    def test_hand_written_polyface_polymesh_lwpolyline_solid(self):
        def tags(*pairs):
            return ''.join('%d\n%s\n' % (c, v) for c, v in pairs)
        body = tags((0, 'SECTION'), (2, 'ENTITIES'))
        # polyface mesh: 4 vertices, 1 quad + 1 triangle (with an invisible edge)
        body += tags((0, 'POLYLINE'), (8, 'PF'), (66, 1), (70, 64), (71, 4), (72, 2))
        for x, y, z in [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 1)]:
            body += tags((0, 'VERTEX'), (8, 'PF'), (10, x), (20, y), (30, z), (70, 192))
        body += tags((0, 'VERTEX'), (8, 'PF'), (10, 0), (20, 0), (30, 0), (70, 128), (71, 1), (72, 2), (73, 3), (74, 4))
        body += tags((0, 'VERTEX'), (8, 'PF'), (10, 0), (20, 0), (30, 0), (70, 128), (71, -1), (72, 3), (73, 4))
        body += tags((0, 'SEQEND'))
        # polygon mesh 2 x 3
        body += tags((0, 'POLYLINE'), (8, 'PM'), (66, 1), (70, 16), (71, 2), (72, 3))
        for i in range(2):
            for j in range(3):
                body += tags((0, 'VERTEX'), (8, 'PM'), (10, i), (20, j), (30, i * j), (70, 64))
        body += tags((0, 'SEQEND'))
        # R2000-style LWPOLYLINE (with subclass markers / handle / owner) closed, elevated
        body += tags((0, 'LWPOLYLINE'), (5, '2A'), (330, '1F'), (100, 'AcDbEntity'), (8, 'LW'),
                     (100, 'AcDbPolyline'), (90, 3), (70, 1), (38, 25.0),
                     (10, 0), (20, 0), (10, 4), (20, 0), (10, 4), (20, 3))
        # SOLID (bow-tie corner order) and a LINE and a 2-D POLYLINE with elevation
        body += tags((0, 'SOLID'), (8, 'S'), (10, 0), (20, 0), (30, 0), (11, 1), (21, 0), (31, 0),
                     (12, 0), (22, 1), (32, 0), (13, 1), (23, 1), (33, 0))
        body += tags((0, 'LINE'), (8, 'L'), (10, 0), (20, 0), (30, 0), (11, 1), (21, 1), (31, 1))
        body += tags((0, 'POLYLINE'), (8, 'L'), (66, 1), (70, 0), (10, 0), (20, 0), (30, 7.5))
        body += tags((0, 'VERTEX'), (8, 'L'), (10, 0), (20, 0), (30, 0))
        body += tags((0, 'VERTEX'), (8, 'L'), (10, 2), (20, 0), (30, 0))
        body += tags((0, 'SEQEND'))
        body += tags((0, 'TEXT'), (8, 'T'), (1, 'hi'), (10, 0), (20, 0), (30, 0))
        body += tags((0, 'ENDSEC'), (0, 'EOF'))
        objs = {(o.kind, o.name): o for o in dxffmt.read_dxf(body.encode())}
        pf = objs[('mesh', 'PF')]
        self.assertEqual(pf.n_vertices, 4)
        self.assertEqual(pf.n_triangles, 3)
        pf.validate()
        pm = objs[('mesh', 'PM')]
        self.assertEqual(pm.n_vertices, 6)
        self.assertEqual(pm.n_triangles, 4)
        lw = objs[('lineset', 'LW')]
        self.assertEqual([len(p) for p in lw.parts], [4])           # closed -> first vertex repeated
        self.assertEqual(lw.part_xyz(0)[0], (0.0, 0.0, 25.0))
        self.assertEqual(lw.part_xyz(0)[-1], (0.0, 0.0, 25.0))
        s = objs[('mesh', 'S')]
        self.assertEqual(s.n_triangles, 2)
        ln = objs[('lineset', 'L')]
        self.assertEqual([len(p) for p in ln.parts], [2, 2])
        self.assertEqual(ln.part_xyz(1), [(0.0, 0.0, 7.5), (2.0, 0.0, 7.5)])
        self.assertEqual(ln.metadata['skipped'], {'TEXT': 1})

    def test_bulge_tessellation(self):
        pts = dxffmt._bulge_points((0, 0, 0), (2, 0, 0), 1.0, max_deg=10)
        self.assertTrue(len(pts) >= 15)
        for x, y, z in pts:
            self.assertAlmostEqual(math.hypot(x - 1.0, y), 1.0, places=9)   # semicircle r = 1
        self.assertLess(pts[len(pts) // 2][1], 0)                              # CCW from (0,0) -> (2,0) bulges down
        pts = dxffmt._bulge_points((0, 0, 0), (2, 0, 0), -0.25)
        self.assertAlmostEqual(pts[len(pts) // 2][1], 0.25, places=6)         # sagitta = bulge * half chord

    def test_ezdxf_reads_ours(self):
        try:
            import ezdxf
        except ImportError:
            self.skipTest('optional cross-validator unavailable: ezdxf not installed')
        m, ls, ps = pyramid('surf'), sample_lines(), sample_points()
        p = dxffmt.write_dxf([m, ls, ps], self.path('ez.dxf'))
        doc = ezdxf.readfile(p)
        self.assertEqual(doc.dxfversion, 'AC1009')
        self.assertEqual(doc.header.get('$INSUNITS'), 6)
        self.assertEqual(tuple(doc.header.get('$EXTMIN')), (0.0, 0.0, 0.0))
        self.assertEqual(tuple(doc.header.get('$EXTMAX')), (10.0, 10.0, 8.0))
        msp = doc.modelspace()
        self.assertEqual(len(msp.query('3DFACE')), 6)
        polys = msp.query('POLYLINE')
        self.assertEqual(len(polys), 2)
        self.assertEqual(sorted(len(pl.vertices) for pl in polys), [2, 3])
        self.assertTrue(all(pl.is_3d_polyline for pl in polys))
        self.assertEqual(len(msp.query('POINT')), 2)
        self.assertEqual({e.dxf.layer for e in msp}, {'surf', 'Drift_300_level', 'collars'})
        self.assertIn('surf', doc.layers)
        self.assertEqual(len(doc.audit().errors), 0)
        face = msp.query('3DFACE')[0]
        self.assertEqual(tuple(face.dxf.vtx2), tuple(face.dxf.vtx3))

    def test_ours_reads_ezdxf(self):
        try:
            import ezdxf
        except ImportError:
            self.skipTest('optional cross-validator unavailable: ezdxf not installed')
        for ver in ('R12', 'R2000', 'R2013'):
            doc = ezdxf.new(ver)
            msp = doc.modelspace()
            doc.layers.add('SURF', color=1)
            doc.layers.add('LINES', color=3)
            msp.add_3dface([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], dxfattribs={'layer': 'SURF'})
            msp.add_3dface([(0, 0, 0), (1, 0, 0), (0.5, 0.5, 1)], dxfattribs={'layer': 'SURF'})
            msp.add_polyline3d([(0, 0, 0), (1, 1, 1), (2, 0, 2)], dxfattribs={'layer': 'LINES'})
            msp.add_polyline3d([(0, 0, 0), (1, 1, 1), (2, 0, 2)], close=True, dxfattribs={'layer': 'LINES'})
            msp.add_line((0, 0, 0), (5, 5, 5), dxfattribs={'layer': 'LINES'})
            pf = msp.add_polyface(dxfattribs={'layer': 'PF'})
            pf.append_face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
            pf.append_face([(0, 0, 0), (1, 0, 0), (0.5, 0.5, 1)])
            pm = msp.add_polymesh((3, 4), dxfattribs={'layer': 'PM'})
            for i in range(3):
                for j in range(4):
                    pm.set_mesh_vertex((i, j), (i, j, i * j))
            msp.add_point((7, 8, 9), dxfattribs={'layer': 'PTS'})
            msp.add_text('hello', dxfattribs={'layer': 'TXT'})
            if ver != 'R12':
                msp.add_lwpolyline([(0, 0, 0.5), (2, 0, 0), (2, 2, 0)], format='xyb', close=True,
                                   dxfattribs={'layer': 'LW', 'elevation': 10})
            p = self.path('ez_%s.dxf' % ver)
            doc.saveas(p)
            objs = {(o.kind, o.name): o for o in dxffmt.read_dxf(p)}
            surf = objs[('mesh', 'SURF')]
            self.assertEqual((surf.n_vertices, surf.n_triangles), (5, 3))
            self.assertEqual(surf.color, [255, 0, 0])
            lines = objs[('lineset', 'LINES')]
            self.assertEqual([len(pt) for pt in lines.parts], [3, 4, 2])
            self.assertEqual(lines.color, [0, 255, 0])
            self.assertEqual(objs[('mesh', 'PF')].n_triangles, 3)
            self.assertEqual(objs[('mesh', 'PF')].n_vertices, 5)
            self.assertEqual(objs[('mesh', 'PM')].n_triangles, 12)
            self.assertEqual(objs[('points', 'PTS')].point(0), (7.0, 8.0, 9.0))
            self.assertEqual(surf.metadata['skipped'], {'TEXT': 1})
            if ver != 'R12':
                lw = objs[('lineset', 'LW')]
                self.assertEqual(len(lw.parts), 1)
                self.assertTrue(len(lw.parts[0]) > 4)                   # arc tessellated
                self.assertTrue(all(abs(z - 10.0) < 1e-9 for z in lw.vertices[2::3]))
                self.assertTrue(any('bulge' in w for w in lw.metadata['warnings']))


# ---------------------------------------------------------------------- GOCAD
class TestGOCAD(TempDirMixin, unittest.TestCase):
    def test_reference_samples(self):
        if not REF_GOCAD.exists():
            self.skipTest('optional cross-validator unavailable: reference GOCAD samples not available')
        expect = {'cfm.ts': (23, 24), 'input_3tri_all_props.ts': (6, 3),
                  'input_3tri_node_props.ts': (6, 3), 'input_small_TFACE.ts': (12, 9)}
        for name, (nv, nt) in expect.items():
            path = REF_GOCAD / name
            if not path.exists():
                continue
            objs = gocadfmt.read_gocad(str(path))
            self.assertEqual(len(objs), 1, name)
            m = objs[0]
            self.assertEqual(m.kind, 'mesh', name)
            self.assertEqual((m.n_vertices, m.n_triangles), (nv, nt), name)
            m.validate()
            self.assertTrue(all(0 <= t < nv for t in m.triangles))
            self.assertEqual(m.provenance['format'], 'gocad_ts')
        cfm = gocadfmt.read_gocad(str(REF_GOCAD / 'cfm.ts'))[0]
        self.assertEqual(cfm.name, 'MJVA-GLPS-GLDS-Goldstone_Lake_fault-CFM1')
        # '*solid*color:1 1 1 1' in the pinned CFM5 file (sha256 012a83ea…),
        # normalised floats like the 0.501961 0 0 1 asserted below. The old
        # expectation here was #32cd32, which appears nowhere in that file;
        # it had never run, because this class skipped whenever the corpus
        # was missing and the corpus was being looked for at a path that did
        # not exist on any machine still building this.
        self.assertEqual(cfm.color, [255, 255, 255])
        self.assertEqual(cfm.vertex(0), (506833.15625, 3923295.25, -10589.7001953125))
        props = gocadfmt.read_gocad(str(REF_GOCAD / 'input_3tri_all_props.ts'))[0]
        self.assertEqual(props.color, [128, 0, 0])                            # 0.501961 0 0 1
        self.assertEqual(props.role, 'fault')
        self.assertIn('Dip_Azim', props.attributes)
        self.assertEqual(props.attributes['Dip_Azim']['location'], 'vertices')
        self.assertAlmostEqual(props.attributes['FrameX_Trend']['values'][0], 157.116)
        self.assertAlmostEqual(props.attributes['Dip_Azim']['values'][0], 67.1159)
        self.assertAlmostEqual(props.attributes['FaultStrike']['values'][5], 337.11)
        self.assertTrue(math.isnan(props.attributes['GaussianCurvature']['values'][0]))   # 1e+38
        self.assertEqual(props.attributes['ZoneId']['location'], 'faces')
        self.assertEqual(list(props.attributes['ZoneId']['values']), [1.0, 2.0, 3.0])
        small = gocadfmt.read_gocad(str(REF_GOCAD / 'input_small_TFACE.ts'))[0]
        self.assertEqual(small.metadata['tfaces'], 2)
        self.assertTrue(any('HEADER' in w for w in small.metadata['warnings']))
        self.assertEqual(list(small.attributes['FaultStrike']['values'])[:2], [337.116, 337.085])
        pl_path = REF_GOCAD / 'pynoddy.pl'
        if pl_path.exists():
            ls = gocadfmt.read_gocad(str(pl_path))[0]
            self.assertEqual(ls.kind, 'lineset')
            self.assertEqual(ls.name, 'test_0001_p')
            self.assertEqual(len(ls.parts), 40)                   # ids restart at 1 per ILINE
            self.assertEqual(ls.n_vertices, 80)
            self.assertTrue(all(len(p) == 2 for p in ls.parts))
            self.assertGreater(ls.length(), 0)

    def test_concatenated_objects_nodata_atom_depth(self):
        txt = """GOCAD TSurf 1
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
        objs = gocadfmt.read_gocad(txt.encode())
        self.assertEqual([o.kind for o in objs], ['mesh', 'points', 'lineset', 'lineset'])
        m, ps, ls, loop = objs
        self.assertEqual(m.name, 'depth surf')
        self.assertEqual(m.color, [255, 0, 0])
        self.assertEqual(list(m.vertices[2::3]), [-100.0, -100.0, -110.0, -100.0])   # Depth -> negated, ATOM shares xyz
        self.assertEqual(list(m.triangles), [0, 1, 2, 1, 3, 2])
        g = m.attributes['grade']['values']
        self.assertEqual(g[0], 1.5)
        self.assertTrue(math.isnan(g[1]))
        self.assertEqual(g[3], 1.5)                                                 # ATOM inherits properties
        self.assertEqual(list(m.attributes['vec_1']['values']), [0.0, 2.0, 4.0, 0.0])
        self.assertEqual(list(m.attributes['vec_2']['values']), [1.0, 3.0, 5.0, 1.0])
        self.assertTrue(any('Depth' in w for w in m.metadata['warnings']))
        self.assertTrue(any('Voxet' in w for w in m.metadata['warnings']))
        self.assertEqual(m.metadata['coordinate_system']['ZPOSITIVE'], 'Depth')
        self.assertEqual(ps.n, 2)
        self.assertEqual(ps.attributes, {'au': [0.5, 0.7]})
        self.assertEqual(ps.color, [0, 0, 255])
        self.assertEqual(ls.parts, [[0, 1, 2], [3, 4]])
        self.assertEqual(list(ls.segments), [0, 1, 1, 2, 3, 4])
        self.assertEqual(loop.parts, [[0, 1, 2, 0]])
        self.assertEqual(len(loop.segments), 6)

    def test_round_trips(self):
        m = pyramid('Surf One')
        m.attributes['grade'] = {'location': 'vertices', 'values': [1.0, NAN, 3.0, 4.0, 5.0]}
        m.attributes['zone'] = {'location': 'faces', 'values': [1, 1, 2, 2, 3, 3]}
        p = gocadfmt.write_gocad(m, self.path('m.ts'))
        text = Path(p).read_text()
        self.assertTrue(text.startswith('GOCAD TSurf 1\nHEADER {\nname:Surf One\n'))
        self.assertIn('AXIS_NAME "X" "Y" "Z"', text)
        self.assertIn('AXIS_UNIT "m" "m" "m"', text)
        self.assertIn('ZPOSITIVE Elevation', text)
        self.assertIn('PROPERTIES grade', text)
        self.assertIn('TRGL_PROPERTIES zone', text)
        self.assertIn('PVRTX 1 0 0 0 1', text)
        self.assertIn('TRGL 1 3 2 1', text)
        self.assertEqual(text.count('\nTFACE\n'), 1)
        r = gocadfmt.read_gocad(p)[0]
        self.assertEqual(list(r.vertices), list(m.vertices))
        self.assertEqual(list(r.triangles), list(m.triangles))
        self.assertEqual(r.name, 'Surf One')
        self.assertEqual(r.color, [255, 0, 0])
        self.assertTrue(math.isnan(r.attributes['grade']['values'][1]))
        self.assertEqual(list(r.attributes['zone']['values']), [1.0, 1.0, 2.0, 2.0, 3.0, 3.0])
        # Depth output negates Z
        d = gocadfmt.write_gocad(m, io.BytesIO(), zpositive='Depth').decode()
        self.assertIn('ZPOSITIVE Depth', d)
        self.assertIn('PVRTX 5 5 5 -8', d)
        self.assertEqual(list(gocadfmt.read_gocad(d.encode())[0].vertices), list(m.vertices))
        # PLine
        ls = sample_lines()
        d = gocadfmt.write_gocad(ls, io.BytesIO()).decode()
        self.assertEqual(d.count('ILINE'), 2)
        self.assertIn('SEG 1 2', d)
        r = gocadfmt.read_gocad(d.encode())[0]
        self.assertEqual([len(pt) for pt in r.parts], [3, 2])
        self.assertEqual(r.part_xyz(0), ls.part_xyz(0))
        # VSet with numeric + text attributes (text dropped)
        ps = sample_points()
        ps.attributes['au'] = [0.5, 1.5]
        d = gocadfmt.write_gocad(ps, io.BytesIO()).decode()
        self.assertIn('GOCAD VSet 1', d)
        self.assertIn('PROPERTIES au', d)
        self.assertNotIn('hole', d)
        r = gocadfmt.read_gocad(d.encode())[0]
        self.assertEqual(list(r.xyz), list(ps.xyz))
        self.assertEqual(r.attributes['au'], [0.5, 1.5])
        self.assertEqual(sniff(head=d.encode()[:64]), 'gocad_ts')

    def test_color_parsing(self):
        self.assertEqual(gocadfmt._parse_color('#32cd32'), [50, 205, 50])
        self.assertEqual(gocadfmt._parse_color('0.501961 0 0 1'), [128, 0, 0])
        self.assertEqual(gocadfmt._parse_color('255 128 0'), [255, 128, 0])
        self.assertIsNone(gocadfmt._parse_color('red'))

    def test_not_gocad(self):
        with self.assertRaises(ValueError):
            gocadfmt.read_gocad(b'hello world\n')


# ----------------------------------------------------------------- Leapfrog msh
class TestLeapfrogMsh(TempDirMixin, unittest.TestCase):
    def test_round_trip_and_layout(self):
        m = pyramid('lf')
        p = lfmsh.write_msh(m, self.path('lf.msh'))
        data = Path(p).read_bytes()
        self.assertTrue(data.startswith(b'%%ARANZ-1.0\n\n[index]\nTri Integer 3 6;\nLocation Double 3 5;\n\n[binary]\n'))
        k = data.find(b'[binary]') + 9
        self.assertEqual(struct.unpack('<3i', data[k:k + 12]), (15732735, 1115938331, 1072939210))
        self.assertEqual(len(data), k + 12 + 6 * 3 * 4 + 5 * 3 * 8)
        self.assertEqual(struct.unpack('<3i', data[k + 12:k + 24]), (0, 2, 1))
        self.assertEqual(struct.unpack('<3d', data[k + 12 + 72:k + 12 + 96]), (0.0, 0.0, 0.0))
        r = lfmsh.read_msh(p)
        self.assertEqual(list(r.vertices), list(m.vertices))
        self.assertEqual(list(r.triangles), list(m.triangles))
        self.assertEqual(r.name, 'lf')
        self.assertEqual(r.provenance['format'], 'lf_msh')
        self.assertTrue(any('reverse-engineered' in w for w in r.metadata['warnings']))
        self.assertEqual(r.metadata['index'][0]['name'], 'Tri')
        self.assertEqual(sniff(head=data[:64]), 'lf_msh')
        b = lfmsh.write_msh(m, io.BytesIO())
        self.assertEqual(b, data)
        self.assertEqual(lfmsh.read_msh(b).n_triangles, 6)

    def test_generic_index_extra_arrays_and_missing_newline(self):
        hdr = b'%%ARANZ-1.0\n\n[index]\nTri Integer 3 1;\nLocation Double 3 3;\nColour Integer 1 3;\nFaceTag Double 1 1;\n\n[binary]'
        payload = (struct.pack('<3i', 15732735, 1115938331, 1072939210)
                   + struct.pack('<3i', 0, 1, 2)
                   + struct.pack('<9d', 0, 0, 0, 1, 0, 0, 0, 1, 0)
                   + struct.pack('<3i', 7, 8, 9)
                   + struct.pack('<d', 2.5))
        for blob in (hdr + payload, hdr + b'\n' + payload, hdr + b'\r\n' + payload):
            r = lfmsh.read_msh(blob)
            self.assertEqual((r.n_vertices, r.n_triangles), (3, 1))
            self.assertEqual(list(r.triangles), [0, 1, 2])
            self.assertEqual(r.attributes['Colour']['location'], 'vertices')
            self.assertEqual(list(r.attributes['Colour']['values']), [7.0, 8.0, 9.0])
            self.assertEqual(r.attributes['FaceTag']['location'], 'faces')
            self.assertEqual(list(r.attributes['FaceTag']['values']), [2.5])
            self.assertTrue(any('unknown' in w for w in r.metadata['warnings']))
        entries = lfmsh.parse_index('[index]\nTri Integer 3 10;\nLocation Double 3 7;\n[binary]')
        self.assertEqual([(e[0], e[1], e[3]) for e in entries], [('Tri', 'i', [3, 10]), ('Location', 'd', [3, 7])])

    def test_errors(self):
        with self.assertRaises(ValueError):
            lfmsh.read_msh(b'not a mesh')
        with self.assertRaises(ValueError):
            lfmsh.read_msh(b'%%ARANZ-1.0\n\n[index]\nTri Integer 3 2;\nLocation Double 3 3;\n\n[binary]\n' + b'\0' * 20)


if __name__ == '__main__':
    unittest.main()
