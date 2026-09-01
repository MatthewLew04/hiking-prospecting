"""geomodel.agentbuild — placement rules, and the refusal to place what the
text does not locate."""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import agentbuild, narrative, workings as wk  # noqa: E402

SITE = {'name': 'Silver King', 'mine_id': 'grades:17', 'lon': -116.87, 'lat': 36.877,
        'elevation_m': 1900.0, 'source': 'USGS Bulletin 723', 'source_url': 'https://example.invalid/b723',
        'elevation_source': 'AWS Terrain Tiles, zoom 13'}

PROSE = ('The Main shaft was sunk to a depth of 620 feet. '
         'An adit driven N45E for 900 feet cuts the vein. '
         'On the 300 level a drift was extended 450 feet N 20 W. '
         'A winze was sunk 120 ft below the 400 level.')


def build(prose=PROSE, site=None, answers=None):
    spec = narrative.parse(prose)
    if answers:
        spec = narrative.apply_answers(spec, answers)
    return spec, agentbuild.build(spec, dict(site or SITE))


def placed(built, kind):
    return [p for p in built['placed'] if p['kind'] == kind]


class CollarTests(unittest.TestCase):
    def test_the_collar_is_the_resolved_coordinate_and_the_terrain_z(self):
        _, built = build()
        self.assertAlmostEqual(built['collar']['z'], 1900.0, places=6)
        self.assertAlmostEqual(built['collar']['lon'], -116.87, places=6)
        self.assertEqual(built['crs']['kind'], 'utm')
        self.assertEqual(built['crs']['zone'], 11)

    def test_an_unlocated_mine_is_refused_not_placed_at_zero(self):
        with self.assertRaises(agentbuild.Unplaceable):
            agentbuild.build(narrative.parse(PROSE), dict(SITE, lon=None, lat=None))

    def test_a_missing_terrain_elevation_is_refused_not_placed_at_sea_level(self):
        with self.assertRaises(agentbuild.Unplaceable) as ctx:
            agentbuild.build(narrative.parse(PROSE), dict(SITE, elevation_m=None))
        self.assertIn('terrain', str(ctx.exception))


class PlacementTests(unittest.TestCase):
    def test_lengths_are_metres_and_are_converted_exactly_once(self):
        _, built = build()
        by_type = built['summary']['by_type']
        self.assertAlmostEqual(by_type['shaft'], 620 * wk.FT, places=3)
        self.assertAlmostEqual(by_type['drift'], 450 * wk.FT, places=3)
        # the adit rises 0.5 % for drainage, so its 3-D length is a shade longer
        self.assertAlmostEqual(by_type['adit'], 900 * wk.FT, delta=0.02)

    def test_a_vertical_shaft_drops_straight_down_from_the_collar(self):
        _, built = build()
        sh = placed(built, 'shaft')[0]
        self.assertEqual(sh['placement'], 'the collar')
        self.assertAlmostEqual(sh['start'][0], sh['end'][0], places=3)
        self.assertAlmostEqual(sh['start'][1], sh['end'][1], places=3)
        self.assertAlmostEqual(sh['start'][2] - sh['end'][2], 620 * wk.FT, places=2)

    def test_an_adit_starts_at_the_surface_even_when_it_reaches_a_level(self):
        _, built = build('An adit driven N45E for 900 feet cuts the vein on the 300 level. '
                         'The Main shaft was sunk 620 feet.')
        ad = placed(built, 'adit')[0]
        self.assertEqual(ad['placement'], 'the collar')
        self.assertAlmostEqual(ad['start'][2], 1900.0, places=3)

    def test_a_bearing_becomes_the_right_direction_on_the_ground(self):
        _, built = build()
        ad = placed(built, 'adit')[0]
        dx, dy = ad['end'][0] - ad['start'][0], ad['end'][1] - ad['start'][1]
        self.assertAlmostEqual(math.degrees(math.atan2(dx, dy)) % 360.0, 45.0, places=2)
        dr = placed(built, 'drift')[0]
        dx, dy = dr['end'][0] - dr['start'][0], dr['end'][1] - dr['start'][1]
        self.assertAlmostEqual(math.degrees(math.atan2(dx, dy)) % 360.0, 340.0, places=2)

    def test_a_level_sits_at_its_named_depth_below_the_collar(self):
        _, built = build()
        self.assertAlmostEqual(built['levels']['300'], 1900.0 - 300 * wk.FT, places=3)
        self.assertAlmostEqual(built['levels']['400'], 1900.0 - 400 * wk.FT, places=3)

    def test_a_level_working_starts_where_the_level_meets_the_shaft(self):
        _, built = build()
        dr = placed(built, 'drift')[0]
        self.assertIn('at the 300 level', dr['placement'])
        self.assertAlmostEqual(dr['start'][2], 1900.0 - 300 * wk.FT, places=3)
        self.assertAlmostEqual(dr['start'][0], built['collar']['x'], places=3)

    def test_a_winze_hangs_from_its_own_level(self):
        _, built = build()
        wz = placed(built, 'winze')[0]
        self.assertAlmostEqual(wz['start'][2], 1900.0 - 400 * wk.FT, places=3)
        self.assertAlmostEqual(wz['start'][2] - wz['end'][2], 120 * wk.FT, places=3)

    def test_an_inclined_shaft_walks_sideways_as_it_goes_down(self):
        _, built = build('An inclined shaft was sunk 400 feet at 45 degrees, bearing N90E.')
        sh = placed(built, 'shaft')[0]
        drop = sh['start'][2] - sh['end'][2]
        run = math.hypot(sh['end'][0] - sh['start'][0], sh['end'][1] - sh['start'][1])
        self.assertAlmostEqual(drop, run, places=2)                 # 45° means equal
        self.assertAlmostEqual(math.hypot(drop, run), 400 * wk.FT, places=2)
        self.assertAlmostEqual(sh['end'][1], sh['start'][1], places=2)   # due east

    def test_a_raise_joins_two_level_stations(self):
        _, built = build('The Main shaft was sunk 620 feet. '
                         'A raise connects the 400 and 300 levels.')
        rs = placed(built, 'raise')[0]
        self.assertAlmostEqual(rs['start'][2], 1900.0 - 400 * wk.FT, places=3)
        self.assertAlmostEqual(rs['end'][2], 1900.0 - 300 * wk.FT, places=3)

    def test_from_the_shaft_on_a_level_means_the_shaft_at_that_level(self):
        _, built = build('The Main shaft was sunk 620 feet. On the 300 level a drift was '
                         'extended 450 feet N 20 W from the shaft.')
        dr = placed(built, 'drift')[0]
        self.assertIn('at the 300 level', dr['placement'])
        self.assertAlmostEqual(dr['start'][2], 1900.0 - 300 * wk.FT, places=3)

    def test_a_stope_follows_the_drift_on_its_level(self):
        _, built = build('The Main shaft was sunk 620 feet. '
                         'On the 300 level a drift was extended 450 feet N 20 W. '
                         'The vein was stoped for 300 feet on the 300 level to a height of 80 feet.')
        st = placed(built, 'stope')[0]
        self.assertIn('340', st['placement'])                        # the drift's bearing
        self.assertIn('default, not a stated figure', st['note'])
        solids = [m for m in built['project'].by_kind('mesh') if m.role == 'stope']
        self.assertEqual(len(solids), 1)
        lo, hi = solids[0].bounds()[2], solids[0].bounds()[5]
        self.assertAlmostEqual(hi - lo, 80 * wk.FT, places=3)


class RefusalTests(unittest.TestCase):
    def test_an_element_with_no_datum_becomes_a_placement_gap(self):
        _, built = build('A drift was extended 450 feet N45E on the No. 3 level.')
        self.assertEqual(built['placed'], [])
        self.assertEqual(len(built['gaps']), 1)
        gap = built['gaps'][0]
        self.assertEqual(gap['kind'], 'placement')
        self.assertTrue(gap['required'])
        self.assertIn('No. 3', gap['question'])

    def test_an_incline_without_a_direction_is_refused(self):
        _, built = build('An incline was sunk 400 feet at 45 degrees.')
        self.assertEqual(built['placed'], [])
        self.assertIn('direction', built['gaps'][0]['question'])

    def test_a_refusal_does_not_stop_the_rest_of_the_model(self):
        _, built = build(PROSE + ' A crosscut was driven 200 feet on the No. 7 level.')
        self.assertEqual(len(built['placed']), 4)
        self.assertEqual(len(built['gaps']), 1)

    def test_a_level_below_the_shaft_bottom_is_warned_about_not_hidden(self):
        _, built = build('The Main shaft was sunk 200 feet. '
                         'On the 900 level a drift was extended 100 feet N45E.')
        self.assertTrue(built['warnings'])
        self.assertIn('below the bottom', built['warnings'][0])

    def test_a_plate_elevation_that_fights_the_level_name_is_warned_about_in_the_right_units(self):
        # the levels table holds elevations, so the warning has to convert
        # before it says "below the collar": a 300 level is 91 m down, not 1809
        spec = narrative.parse('On the 300 level a drift was extended 450 feet N 20 W.')
        el = spec['elements'][0]
        el['path'] = [(-116.87, 36.877, None), (-116.869, 36.8775, None)]
        el['elevation_m'] = 1750.0
        el['plate'] = 'p3'
        built = agentbuild.build(spec, dict(SITE))
        self.assertEqual(built['warnings'],
                         ['the "300" level is drawn at 1750 m on plate p3 but its name puts it '
                          'at 1809 m, 91 m below the collar; the surveyed elevation is used'])
        # the surveyed elevation still wins; only the sentence was wrong
        self.assertAlmostEqual(built['levels']['300'], 1750.0, places=3)


class ProvenanceTests(unittest.TestCase):
    def test_every_feature_carries_its_quote_span_and_field_confidence(self):
        spec, built = build()
        by_id = dict((e['id'], e) for e in spec['elements'])
        ws = built['workings']
        self.assertTrue(ws.features)
        for feat in ws.features:
            self.assertIn(feat['element_id'], by_id)
            self.assertEqual(feat['quote'], by_id[feat['element_id']]['quote'])
            self.assertEqual(feat['span'], by_id[feat['element_id']]['span'])
            self.assertIn(feat['confidence'], wk.CONFIDENCE)
            self.assertTrue(feat['fields'])
            self.assertEqual(feat['source']['url'], SITE['source_url'])
            self.assertEqual(feat['source']['mine_id'], 'grades:17')

    def test_units_in_records_the_source_units_while_geometry_stays_metric(self):
        _, built = build()
        for feat in built['workings'].features:
            self.assertEqual(feat['units_in'], 'ft')

    def test_an_answered_field_is_carried_through_as_assumed(self):
        spec = narrative.parse('On the 300 level a drift was extended 450 feet. '
                               'The Main shaft was sunk 620 feet.')
        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        spec = narrative.apply_answers(spec, [{'id': gap['id'], 'value': 45.0,
                                               'because': 'same vein as the adit'}])
        built = agentbuild.build(spec, dict(SITE))
        drift = [f for f in built['workings'].features if f['type'] == 'drift'][0]
        self.assertEqual(drift['confidence'], 'assumed')
        self.assertEqual(drift['fields']['bearing_deg'], 'assumed')
        self.assertEqual(built['confidence'], {'surveyed': 0, 'described': 1, 'assumed': 1})

    def test_a_surface_feature_keeps_its_citation_too(self):
        _, built = build('A glory hole 40 feet across marks the outcrop.')
        feat = built['workings'].features[0]
        self.assertEqual(feat['type'], 'pit')
        self.assertEqual(feat['source']['url'], SITE['source_url'])
        self.assertEqual(feat['source']['mine_id'], 'grades:17')
        self.assertTrue(feat['quote'])
        self.assertIn('not a survey', feat['notes'])

    def test_definitional_defaults_are_declared_on_the_feature(self):
        _, built = build('A shaft was sunk 300 feet.')
        feat = built['workings'].features[0]
        self.assertEqual(feat['defaults'], ['dip_deg'])


class DeterminismTests(unittest.TestCase):
    def test_two_builds_of_one_spec_are_identical(self):
        _, a = build()
        _, b = build()
        self.assertEqual(json.dumps(a['placed'], sort_keys=True), json.dumps(b['placed'], sort_keys=True))
        # geometry bytes match; only the wall-clock fields differ, and those are
        # excluded from the content address on purpose
        self.assertEqual(agentbuild.stable_bytes(a['project']),
                         agentbuild.stable_bytes(b['project']))
        self.assertEqual(agentbuild.content_sha256(a['project']),
                         agentbuild.content_sha256(b['project']))
        self.assertNotIn(b'"created"', agentbuild.stable_bytes(a['project']))

    def test_element_order_does_not_depend_on_the_order_they_were_written(self):
        _, a = build('The Main shaft was sunk 620 feet. On the 300 level a drift was '
                     'extended 450 feet N 20 W.')
        _, b = build('On the 300 level a drift was extended 450 feet N 20 W. '
                     'The Main shaft was sunk 620 feet.')
        self.assertEqual([p['kind'] for p in a['placed']], [p['kind'] for p in b['placed']])
        self.assertAlmostEqual(placed(a, 'drift')[0]['start'][2],
                               placed(b, 'drift')[0]['start'][2], places=6)


class ExportTests(unittest.TestCase):
    def test_all_four_interchange_files_are_written(self):
        _, built = build()
        with tempfile.TemporaryDirectory() as d:
            manifest = agentbuild.write_exports(built, d)
            names = [n for n, _ in manifest]
            self.assertEqual(names, ['model.geomodel.json', 'model.omf',
                                     'workings.dxf', 'workings.geojson'])
            for name in names:
                self.assertTrue((Path(d) / name).stat().st_size > 0, name)
            gj = json.loads((Path(d) / 'workings.geojson').read_text(encoding='utf-8'))
            self.assertEqual(len(gj['features']), 4)
            for feat in gj['features']:
                lon, lat, _ = feat['geometry']['coordinates'][0]
                self.assertAlmostEqual(lon, -116.87, delta=0.02)
                self.assertAlmostEqual(lat, 36.877, delta=0.02)
                self.assertIn(feat['properties']['confidence'], wk.CONFIDENCE)

    def test_the_project_reloads_from_its_own_json(self):
        from geomodel.model import Project
        _, built = build()
        with tempfile.TemporaryDirectory() as d:
            agentbuild.write_exports(built, d, formats=('json',))
            again = Project.load(str(Path(d) / 'model.geomodel.json'))
        ws = [o for o in again.objects if getattr(o, 'role', '') == 'workings'][0]
        self.assertEqual(len(ws.features), 4)
        self.assertEqual(ws.metadata['builder'], agentbuild.BUILDER_VERSION)


if __name__ == '__main__':
    unittest.main()
