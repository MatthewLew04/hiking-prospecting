"""geomodel.mapplate — the only path to `surveyed` confidence, and the checks
that stop it being claimed without a georeference that holds up."""
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import agentbuild, mapplate, narrative, render2d, workings as wk  # noqa: E402
from leapfrog_export import utm_fwd, utm_zone  # noqa: E402

SITE = {'name': 'White Caps mine', 'mine_id': 'grades:17', 'lon': -116.87, 'lat': 36.877,
        'elevation_m': 1116.0, 'source_url': 'https://example.invalid/b723'}

#: a square plan: 100..900 px across 0.0100 deg of longitude at 36.876 N
PLAN = {'plate_id': 'p3', 'image': 'plate3.png', 'width': 1000, 'height': 800,
        'plane': 'plan',
        'control': [[100, 700, -116.8700, 36.8760],
                    [900, 700, -116.8600, 36.8760],
                    [100, 100, -116.8700, 36.8820]],
        'level': '300', 'elevation_m': 1025.0,
        'source': {'doc': 'USGS Bulletin 723', 'page': '147', 'figure': 'Plate 3'}}

TRACE = {'id': 't1', 'kind': 'drift', 'name': '300 level drift',
         'points': [[150, 650], [500, 400], [800, 300]]}

SECTION = {'plate_id': 's1', 'image': 'sec.png', 'width': 1200, 'height': 600,
           'plane': 'section', 'p1': [-116.8720, 36.8770], 'p2': [-116.8620, 36.8770],
           'z_top': 1150.0, 'z_bottom': 850.0,
           'source': {'doc': 'USGS Bulletin 723', 'figure': 'Plate 4'}}


def plan(**kw):
    out = dict(PLAN)
    out.update(kw)
    return out


class ValidationTests(unittest.TestCase):
    """Malformed input is an error; missing information is a question."""

    def test_a_good_plate_normalises(self):
        got = mapplate.validate_plate(PLAN)
        self.assertEqual(got['plate_id'], 'p3')
        self.assertEqual(got['plane'], 'plan')
        self.assertEqual(len(got['control']), 3)
        self.assertEqual(got['elevation_m'], 1025.0)

    def test_structural_mistakes_are_errors_not_questions(self):
        for bad, why in (
                (plan(width=0), 'zero width'),
                (plan(plane='oblique'), 'unknown plane'),
                (plan(control=[[100, 700, -116.87]]), 'short control point'),
                (plan(control=[[100, 700, -400.0, 36.876], [900, 700, -116.86, 36.876]]), 'bad lon'),
                (plan(anchor={'px': [1, 1], 'lonlat': [-116.87, 36.876],
                              'scale_m_per_px': -1.0}), 'negative scale'),
                (dict(SECTION, z_top=800.0, z_bottom=900.0), 'inverted section')):
            with self.assertRaises(mapplate.PlateError, msg=why):
                mapplate.validate_plate(bad)

    def test_a_georeference_that_cannot_be_solved_is_a_correctable_error(self):
        """These reach the agent as a 400 with guidance, never as a 500."""
        cases = {
            'collinear': plan(control=[[100, 700, -116.8700, 36.8760],
                                       [500, 700, -116.8660, 36.8760],
                                       [900, 700, -116.8620, 36.8760]]),
            'same pixel': plan(control=[[100, 700, -116.8700, 36.8760],
                                        [100, 700, -116.8600, 36.8760]]),
            'same ground': plan(control=[[100, 700, -116.8700, 36.8760],
                                         [900, 300, -116.8700, 36.8760]]),
            'flat section': dict(SECTION, p2=list(SECTION['p1'])),
        }
        for label, bad in cases.items():
            with self.assertRaises(mapplate.PlateError, msg=label) as ctx:
                mapplate.validate_plate(bad)
            self.assertTrue(str(ctx.exception), label)

    def test_the_collinear_message_says_what_to_do_about_it(self):
        with self.assertRaises(mapplate.PlateError) as ctx:
            mapplate.validate_plate(plan(control=[[100, 700, -116.8700, 36.8760],
                                                  [500, 700, -116.8660, 36.8760],
                                                  [900, 700, -116.8620, 36.8760]]))
        self.assertIn('one line', str(ctx.exception))
        self.assertIn('scale bar', str(ctx.exception))

    def test_two_good_control_points_are_still_enough(self):
        p = mapplate.validate_plate(plan(control=PLAN['control'][:2]))
        self.assertAlmostEqual(mapplate.scale_check(p)['m_per_px'], 1.11, delta=0.02)
        self.assertIsNone(mapplate.scale_check(p)['residual_m'])

    def test_a_trace_far_outside_the_scan_is_caught_as_a_units_mistake(self):
        p = mapplate.validate_plate(PLAN)
        with self.assertRaises(mapplate.PlateError) as ctx:
            mapplate.validate_traces(p, [{'kind': 'drift', 'points': [[0, 0], [50000, 40000]]}])
        self.assertIn('really pixels', str(ctx.exception))

    def test_a_plate_with_no_traces_can_still_be_checked(self):
        # checking a georeference before tracing anything on it is the first
        # thing anyone does
        p = mapplate.validate_plate(PLAN)
        self.assertEqual(mapplate.validate_traces(p, None), [])
        self.assertEqual(mapplate.validate_traces(p, []), [])
        self.assertTrue(mapplate.scale_check(p)['m_per_px'] > 0)

    def test_traces_of_the_wrong_type_still_error(self):
        p = mapplate.validate_plate(PLAN)
        with self.assertRaises(mapplate.PlateError):
            mapplate.validate_traces(p, 'a drift')

    def test_trace_shape_is_enforced(self):
        p = mapplate.validate_plate(PLAN)
        for bad in ([{'kind': 'drift', 'points': [[1, 1]]}],
                    [{'kind': 'escalator', 'points': [[1, 1], [2, 2]]}],
                    [{'id': 't1', 'kind': 'drift', 'points': [[1, 1], [2, 2]]},
                     {'id': 't1', 'kind': 'adit', 'points': [[1, 1], [2, 2]]}]):
            with self.assertRaises(mapplate.PlateError):
                mapplate.validate_traces(p, bad)

    def test_a_stope_cannot_be_traced_as_a_polyline(self):
        self.assertNotIn('stope', mapplate.TRACEABLE)

    def test_missing_georeference_is_a_question(self):
        p = mapplate.validate_plate(plan(control=None))
        gaps = mapplate.plate_gaps(p, [])
        self.assertTrue(any(g['field'] == 'control' and g['required'] for g in gaps))
        self.assertIn('no georeference', [g['question'] for g in gaps][0])

    def test_one_control_point_asks_for_the_second(self):
        p = mapplate.validate_plate(plan(control=[PLAN['control'][0]]))
        gap = [g for g in mapplate.plate_gaps(p, []) if g['field'] == 'control'][0]
        self.assertIn('one control point', gap['question'])

    def test_a_plan_with_no_elevation_is_asked_about_not_draped_at_zero(self):
        p = mapplate.validate_plate(plan(level=None, elevation_m=None))
        gaps = mapplate.plate_gaps(p, [{'kind': 'drift', 'points': [[1, 1], [2, 2]]}])
        gap = [g for g in gaps if g['kind'] == 'plate_elevation'][0]
        self.assertTrue(gap['required'])
        self.assertIn(None, [o['value'] for o in gap['options']])

    def test_a_trace_carrying_its_own_level_satisfies_the_elevation_question(self):
        p = mapplate.validate_plate(plan(level=None, elevation_m=None))
        traces = mapplate.validate_traces(p, [dict(TRACE, level='400')])
        self.assertFalse([g for g in mapplate.plate_gaps(p, traces)
                          if g['kind'] == 'plate_elevation'])

    def test_an_incomplete_section_names_what_is_missing(self):
        p = mapplate.validate_plate(dict(SECTION, z_bottom=None))
        gap = [g for g in mapplate.plate_gaps(p, []) if g['required']][0]
        self.assertIn('z_bottom', gap['question'])

    def test_an_empty_plate_is_only_an_optional_question(self):
        p = mapplate.validate_plate(PLAN)
        gaps = mapplate.plate_gaps(p, [])
        self.assertEqual([g['required'] for g in gaps], [False])
        self.assertIn('nothing has been traced', gaps[0]['question'])


class GeoreferenceTests(unittest.TestCase):
    def test_the_georeference_is_solved_in_metres_not_degrees(self):
        # 0.01 deg of longitude at 36.876 N is ~890 m, 0.006 deg of latitude is
        # ~666 m.  Fitted in degrees the two axes would come out equal.
        got = mapplate.scale_check(mapplate.validate_plate(PLAN))
        self.assertAlmostEqual(got['m_per_px'], 1.11, delta=0.02)
        self.assertLess(got['residual_m'], 1e-6)

    def test_control_points_that_disagree_are_reported(self):
        skewed = plan(control=PLAN['control'] + [[900, 100, -116.8600, 36.8700]])
        got = mapplate.scale_check(mapplate.validate_plate(skewed))
        self.assertGreater(got['residual_m'], 100.0,
                           'a badly tied plate must not look well tied')

    def test_an_anchor_and_a_scale_bar_georeference_a_plate(self):
        p = mapplate.validate_plate(plan(control=None, anchor={
            'px': [500, 400], 'lonlat': [-116.8650, 36.8790],
            'scale_m_per_px': 1.25, 'rotation_deg': 0.0}))
        self.assertAlmostEqual(mapplate.scale_check(p)['m_per_px'], 1.25, places=6)
        els = mapplate.traces_to_elements(p, mapplate.validate_traces(p, [dict(TRACE, level='300')]))
        self.assertEqual(len(els[0]['path']), 3)

    def test_one_control_point_plus_an_anchor_is_solved_from_the_anchor(self):
        # the one-control-point question offers the anchor as the answer, so an
        # agent that adds one without deleting its lone point must get the
        # anchor's scale rather than a solve that cannot be done
        p = mapplate.validate_plate(plan(control=[PLAN['control'][0]], anchor={
            'px': [500, 400], 'lonlat': [-116.8650, 36.8790],
            'scale_m_per_px': 1.25, 'rotation_deg': 0.0}))
        self.assertFalse([g for g in mapplate.plate_gaps(p, [TRACE]) if g['field'] == 'control'])
        self.assertAlmostEqual(mapplate.scale_check(p)['m_per_px'], 1.25, places=6)
        img, _, _ = mapplate.image_plane(p)
        self.assertEqual(img.provenance['georeference'], 'anchor + scale bar')

    def test_solving_a_plate_with_no_georeference_at_all_is_a_plate_error(self):
        # plate_gaps asks about this rather than erroring, so it only happens to
        # a caller that solves a plate it never checked — still a PlateError
        with self.assertRaises(mapplate.PlateError) as ctx:
            mapplate.image_plane(mapplate.validate_plate(plan(control=None)))
        self.assertIn('no georeference', str(ctx.exception))

    def test_a_traced_plan_lands_where_the_control_points_say(self):
        p = mapplate.validate_plate(PLAN)
        traces = mapplate.validate_traces(p, [{'kind': 'drift', 'points': [[100, 700], [900, 700]]}])
        el = mapplate.traces_to_elements(p, traces)[0]
        self.assertAlmostEqual(el['path'][0][0], -116.8700, places=6)
        self.assertAlmostEqual(el['path'][0][1], 36.8760, places=6)
        self.assertAlmostEqual(el['path'][1][0], -116.8600, places=6)

    def test_a_section_trace_carries_its_own_elevations(self):
        p = mapplate.validate_plate(SECTION)
        traces = mapplate.validate_traces(p, [{'kind': 'shaft', 'points': [[600, 0], [600, 600]]}])
        el = mapplate.traces_to_elements(p, traces)[0]
        self.assertAlmostEqual(el['path'][0][2], 1150.0, places=3)
        self.assertAlmostEqual(el['path'][1][2], 850.0, places=3)


class ElementTests(unittest.TestCase):
    def test_traced_elements_are_surveyed_and_cite_their_plate(self):
        p = mapplate.validate_plate(PLAN)
        el = mapplate.traces_to_elements(p, mapplate.validate_traces(p, [TRACE]))[0]
        self.assertEqual(el['confidence'], 'surveyed')
        self.assertEqual(el['fields']['path'], 'surveyed')
        self.assertEqual(el['quote'], 'traced from Plate 3, USGS Bulletin 723, p. 147')
        self.assertEqual(el['source']['doc'], 'USGS Bulletin 723')
        self.assertEqual(el['plate'], 'p3')
        self.assertEqual(el['trace'], 't1')
        self.assertEqual(el['units_in'], 'm')

    def test_ids_come_from_the_plate_and_trace_not_a_running_count(self):
        p = mapplate.validate_plate(PLAN)
        els = mapplate.traces_to_elements(p, mapplate.validate_traces(p, [TRACE]))
        self.assertEqual(els[0]['id'], 'e-p3-t1')


class AttachTests(unittest.TestCase):
    PROSE = 'The Main shaft was sunk to a depth of 620 feet. An adit driven N45E for 900 feet.'

    def spec(self):
        return narrative.parse(self.PROSE, mine_id='grades:17')

    def test_traced_elements_join_the_prose_ones_without_renumbering_them(self):
        base = self.spec()
        got = mapplate.attach(base, [dict(PLAN, traces=[TRACE])])
        self.assertEqual([e['id'] for e in got['elements']][:2],
                         [e['id'] for e in base['elements']][:2])
        self.assertEqual(got['elements'][-1]['id'], 'e-p3-t1')
        self.assertEqual(got['coverage']['traced_elements'], 1)
        self.assertEqual(got['coverage']['plates'], 1)

    def test_reattaching_a_plate_replaces_it_rather_than_duplicating_it(self):
        once = mapplate.attach(self.spec(), [dict(PLAN, traces=[TRACE])])
        twice = mapplate.attach(once, [dict(PLAN, traces=[TRACE])])
        self.assertEqual([e['id'] for e in once['elements']],
                         [e['id'] for e in twice['elements']])
        self.assertEqual(len(twice['plates']), 1)

    def test_a_plate_that_is_still_missing_something_contributes_no_geometry(self):
        got = mapplate.attach(self.spec(),
                              [dict(plan(control=None), traces=[TRACE])])
        self.assertEqual(got['coverage']['traced_elements'], 0)
        self.assertFalse(got['plates'][0]['usable'])
        self.assertGreater(got['coverage']['unresolved'], 0)

    def test_answering_the_one_control_point_question_with_an_anchor_builds(self):
        # the plate the question invites: the lone point kept, an anchor added
        p = plan(control=[PLAN['control'][0]],
                 anchor={'px': [100, 700], 'lonlat': [-116.8700, 36.8760],
                         'scale_m_per_px': 1.1, 'rotation_deg': 0.0})
        got = mapplate.attach(self.spec(), [dict(p, traces=[TRACE])])
        self.assertTrue(got['plates'][0]['usable'])
        self.assertEqual(got['coverage']['traced_elements'], 1)
        self.assertAlmostEqual(got['plates'][0]['scale']['m_per_px'], 1.1, places=6)

    def test_gap_ids_stay_contiguous_after_attaching(self):
        got = mapplate.attach(self.spec(), [dict(plan(control=None), traces=[TRACE])])
        self.assertEqual([g['id'] for g in got['gaps']],
                         ['g%d' % i for i in range(1, len(got['gaps']) + 1)])


class PlateAnswerTests(unittest.TestCase):
    def test_a_plate_question_cannot_be_answered_through_answers(self):
        spec = mapplate.attach(narrative.parse('An adit was driven N45E 900 feet.'),
                               [{'plate_id': 'p1', 'image': 'x.png', 'width': 100,
                                 'height': 100, 'plane': 'plan', 'traces': []}])
        gap = [g for g in spec['gaps'] if g.get('plate')][0]
        with self.assertRaises(ValueError) as ctx:
            narrative.apply_answers(spec, [{'id': gap['id'], 'value': 'anything'}])
        self.assertIn('Send the plate again', str(ctx.exception))

    def test_prose_questions_still_answer_normally_alongside_a_plate(self):
        text = 'On the 300 level a drift was extended 450 feet.'
        spec = mapplate.attach(narrative.parse(text), [dict(PLAN, traces=[TRACE])])
        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        got = narrative.apply_answers(spec, [{'id': gap['id'], 'value': 45.0}])
        self.assertEqual(got['elements'][0]['bearing_deg'], 45.0)
        self.assertTrue(any(e.get('plate') == 'p3' for e in got['elements']))


class BuildTests(unittest.TestCase):
    def built(self, plates=None, prose=None):
        spec = narrative.parse(
            prose or 'The Main shaft was sunk to a depth of 620 feet. '
                     'An adit driven N45E for 900 feet cuts the vein.', mine_id='grades:17')
        if plates:
            spec = mapplate.attach(spec, plates)
        return spec, agentbuild.build(spec, dict(SITE))

    def test_a_traced_working_is_placed_where_it_was_traced(self):
        spec, built = self.built([dict(PLAN, traces=[TRACE])])
        rec = [p for p in built['placed'] if p['element'] == 'e-p3-t1'][0]
        self.assertEqual(rec['placement'], 'traced off p3')
        self.assertEqual(rec['confidence'], 'surveyed')
        # pixel [150, 650]: 800 px spans 0.0100 deg of longitude and 600 px
        # spans 0.0060 deg of latitude, so it is 50 px east and 50 px north of
        # the [100, 700] control point
        zone, north = utm_zone(-116.87, 36.877)
        x, y = utm_fwd(-116.8700 + 50 * 0.0100 / 800, 36.8760 + 50 * 0.0060 / 600, zone, north)
        self.assertAlmostEqual(rec['start'][0], x, delta=2.0)
        self.assertAlmostEqual(rec['start'][1], y, delta=2.0)
        self.assertAlmostEqual(rec['start'][2], 1025.0, places=3)

    def test_a_surveyed_level_elevation_beats_the_naming_convention(self):
        # the prose puts the 300 level 300 ft below a 1116 m collar (1024.6 m);
        # the plate says it is drawn at 1005 m, and the plate wins
        spec, built = self.built(
            [dict(plan(elevation_m=1005.0), traces=[TRACE])],
            prose='The Main shaft was sunk 620 feet. '
                  'On the 300 level a drift was extended 450 feet N 20 W.')
        self.assertAlmostEqual(built['levels']['300'], 1005.0, places=3)
        self.assertTrue(any('surveyed elevation is used' in w for w in built['warnings']),
                        built['warnings'])
        described = [p for p in built['placed'] if p['kind'] == 'drift'
                     and p['confidence'] == 'described'][0]
        self.assertAlmostEqual(described['start'][2], 1005.0, places=3)

    def test_agreeing_elevations_are_not_warned_about(self):
        # 1116 - 300 ft = 1024.6 m, so a plate drawn at 1025 m agrees
        spec, built = self.built(
            [dict(PLAN, traces=[TRACE])],
            prose='The Main shaft was sunk 620 feet. '
                  'On the 300 level a drift was extended 450 feet N 20 W.')
        self.assertEqual(built['warnings'], [])

    def test_a_plan_with_no_elevation_at_all_refuses_to_place_rather_than_drape_at_zero(self):
        p = plan(level=None, elevation_m=None)
        spec = narrative.parse('The Main shaft was sunk 620 feet.')
        spec = mapplate.attach(spec, [dict(p, traces=[TRACE])])
        # the plate is unusable, so no geometry reached the builder at all
        self.assertEqual(spec['coverage']['traced_elements'], 0)
        built = agentbuild.build(spec, dict(SITE))
        self.assertEqual([r['kind'] for r in built['placed']], ['shaft'])

    def test_a_traced_shaft_becomes_the_datum_for_described_level_workings(self):
        traced_shaft = {'id': 'ts', 'kind': 'shaft', 'name': 'Main',
                        'points': [[500, 400], [500, 400]]}
        spec, built = self.built(
            [dict(SECTION, traces=[{'id': 'ts', 'kind': 'shaft', 'name': 'Main',
                                    'points': [[600, 0], [600, 600]]}])],
            prose='On the 300 level a drift was extended 450 feet N 20 W.')
        drift = [p for p in built['placed'] if p['kind'] == 'drift'][0]
        self.assertIn('at the 300 level', drift['placement'])

    def test_the_confidence_tally_separates_surveyed_from_described(self):
        spec, built = self.built([dict(PLAN, traces=[TRACE])])
        self.assertEqual(built['confidence'], {'surveyed': 1, 'described': 2, 'assumed': 0})

    def test_a_traced_element_carries_its_provenance_onto_the_feature(self):
        spec, built = self.built([dict(PLAN, traces=[TRACE])])
        feat = [f for f in built['workings'].features if f.get('traced_from') == 'p3'][0]
        self.assertEqual(feat['confidence'], 'surveyed')
        self.assertEqual(feat['trace'], 't1')
        self.assertEqual(feat['source']['url'], SITE['source_url'])
        self.assertEqual(feat['units_in'], 'm')


class RenderTests(unittest.TestCase):
    def test_surveyed_draws_solid_while_described_stays_dashed(self):
        spec = narrative.parse('The Main shaft was sunk to a depth of 620 feet. '
                               'An adit driven N45E for 900 feet cuts the vein.')
        spec = mapplate.attach(spec, [dict(PLAN, traces=[TRACE])])
        built = agentbuild.build(spec, dict(SITE))
        svg = render2d.plan(built)
        import re

        drawn = re.findall(r'<(?:polyline|polygon|rect)[^>]*stroke-width="2\.2"[^>]*/>', svg)
        solid = [d for d in drawn if 'stroke-dasharray' not in d]
        self.assertEqual(len(solid), 1, 'exactly the traced drift should be solid')
        self.assertIn('surveyed — 1', svg)
        self.assertIn('described — 2', svg)


if __name__ == '__main__':
    unittest.main()
