"""geomodel.narrative — the prose parser, its refusal to invent, and the
identity guarantees the publish path depends on."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import narrative  # noqa: E402
from geomodel.narrative import FT  # noqa: E402


def one(text, kind=None):
    spec = narrative.parse(text)
    els = [e for e in spec['elements'] if kind is None or e['kind'] == kind]
    assert els, 'no %s element parsed from %r' % (kind or 'element', text)
    return spec, els[0]


def fields(el, *names):
    return tuple(el.get(n) for n in names)


class GrammarTests(unittest.TestCase):
    """The core table: one real public-domain phrasing per grammar class."""

    def test_bearing_forms_all_reach_the_same_azimuth(self):
        for phrasing in ('An adit was driven N45E for 900 feet.',
                         'An adit was driven N 45° E for 900 feet.',
                         'An adit was driven N. 45° E. for 900 feet.',
                         'An adit was driven on a bearing of 45 degrees for 900 feet.',
                         'An adit was driven on 045° for 900 feet.'):
            _, el = one(phrasing, 'adit')
            self.assertAlmostEqual(el['bearing_deg'], 45.0, places=4, msg=phrasing)
            self.assertEqual(el['bearing_precision'], 'stated', phrasing)

    def test_bearing_quadrants_cover_all_four(self):
        cases = {'N45E': 45.0, 'N45W': 315.0, 'S45E': 135.0, 'S45W': 225.0,
                 'S 30 W': 210.0, 'N 20 W': 340.0}
        for text, want in cases.items():
            _, el = one('A drift was driven %s for 100 feet on the 300 level.' % text, 'drift')
            self.assertAlmostEqual(el['bearing_deg'], want, places=4, msg=text)

    def test_bearing_minutes_are_kept(self):
        _, el = one("An adit was driven N. 45° 30' E. for 900 feet.", 'adit')
        self.assertAlmostEqual(el['bearing_deg'], 45.5, places=4)

    def test_due_is_exact_but_a_sector_word_is_only_approximate(self):
        _, exact = one('An adit runs due east 350 feet.', 'adit')
        self.assertEqual((exact['bearing_deg'], exact['bearing_precision']), (90.0, 'stated'))

        spec, vague = one('The No. 2 adit extends 1,200-foot northeasterly.', 'adit')
        self.assertEqual(vague['bearing_precision'], 'approximate')
        # imprecision is surfaced as a question, but does not block the build
        imprecise = [g for g in spec['gaps'] if g['kind'] == 'imprecise']
        self.assertEqual(len(imprecise), 1)
        self.assertFalse(imprecise[0]['required'])

    def test_length_forms_all_reach_the_same_metres(self):
        for phrasing in ('An adit was driven N45E 900 ft.',
                         'An adit was driven N45E 900 feet.',
                         "An adit was driven N45E 900'.",
                         'An adit was driven N45E for a distance of 900 feet.',
                         'A 900-foot adit was driven N45E.'):
            _, el = one(phrasing, 'adit')
            self.assertAlmostEqual(el['length_m'], 900 * FT, places=4, msg=phrasing)
            self.assertEqual(el['units_in'], 'ft', phrasing)

    def test_thousands_separator_and_metric_input(self):
        _, imperial = one('An adit was driven N45E 1,200 feet.', 'adit')
        self.assertAlmostEqual(imperial['length_m'], 1200 * FT, places=4)
        _, metric = one('A crosscut was run S 30 W for 275 m from the 400 level.', 'crosscut')
        self.assertAlmostEqual(metric['length_m'], 275.0, places=6)
        self.assertEqual(metric['units_in'], 'm')

    def test_approximate_lengths_are_flagged_not_silently_exact(self):
        _, el = one('An adit was driven N45E about 400 feet.', 'adit')
        self.assertAlmostEqual(el['length_m'], 400 * FT, places=4)
        self.assertEqual(el.get('measure_precision'), 'approximate')

    def test_shaft_depth_dip_and_incline(self):
        _, vertical = one('The Main shaft was sunk to a depth of 620 feet.', 'shaft')
        self.assertAlmostEqual(vertical['depth_m'], 620 * FT, places=4)
        self.assertEqual(vertical['dip_deg'], 90.0)
        self.assertIn('dip_deg', vertical['defaults'])   # definitional, and declared

        _, inclined = one('An inclined shaft was sunk 420 feet at 45 degrees.', 'shaft')
        self.assertAlmostEqual(inclined['depth_m'], 420 * FT, places=4)
        self.assertEqual(inclined['dip_deg'], 45.0)
        self.assertEqual(inclined['defaults'], [])       # nothing definitional here

    def test_winze_below_a_level(self):
        spec, el = one('A winze was sunk 120 ft below the 400 level.', 'winze')
        self.assertAlmostEqual(el['depth_m'], 120 * FT, places=4)
        self.assertEqual(el['level'], '400')
        self.assertAlmostEqual(el['level_depth_m'], 400 * FT, places=4)

    def test_level_labels(self):
        for text, label in (('A drift runs N45E 100 feet on the 400 level.', '400'),
                            ('A drift runs N45E 100 feet on the 100-ft level.', '100'),
                            ('A drift runs N45E 100 feet on the No. 3 level.', 'No. 3'),
                            ('A drift runs N45E 100 feet on the adit level.', 'adit'),
                            ('A drift runs N45E 100 feet on the main haulage level.', 'main haulage')):
            _, el = one(text, 'drift')
            self.assertEqual(el['level'], label, text)

    def test_a_level_that_was_itself_driven_becomes_a_drift(self):
        spec = narrative.parse('The 300 level was extended 600 feet N 20 W.')
        self.assertEqual([e['kind'] for e in spec['elements']], ['drift'])
        el = spec['elements'][0]
        self.assertEqual(el['level'], '300')
        self.assertAlmostEqual(el['length_m'], 600 * FT, places=4)
        self.assertAlmostEqual(el['bearing_deg'], 340.0, places=4)

    def test_a_bare_level_mention_never_becomes_geometry(self):
        spec = narrative.parse('An adit driven N45E for 900 feet cuts the vein on the 300 level.')
        self.assertEqual([e['kind'] for e in spec['elements']], ['adit'])
        self.assertEqual(spec['elements'][0]['level'], '300')

    def test_relations(self):
        _, frm = one('A crosscut was driven N45E 150 feet from the portal.', 'crosscut')
        self.assertEqual(frm['from'], {'ref': 'portal'})

        _, lvl = one('A crosscut was driven N45E 150 feet from the 300 level.', 'crosscut')
        self.assertEqual(lvl['from'], {'ref': 'level', 'level': '300'})

        _, conn = one('A raise connects the 200 and 300 levels, a distance of 100 feet.', 'raise')
        self.assertEqual(conn['connects'], ['200', '300'])

        _, ft = one('A raise was driven from the 500 level to the 400 level, a distance of 100 feet.',
                    'raise')
        self.assertEqual(ft['connects'], ['500', '400'])
        self.assertAlmostEqual(ft['length_m'], 100 * FT, places=4)

    def test_a_raise_between_two_levels_is_not_asked_for_a_length(self):
        spec = narrative.parse('A raise connects the 400 and 300 levels.')
        el = spec['elements'][0]
        self.assertEqual(el['connects'], ['400', '300'])
        self.assertEqual([g for g in spec['gaps'] if g['required']], [],
                         'the two levels already fix the length')

    def test_counts_ride_on_the_element_when_something_was_measured(self):
        _, two = one('Two shafts were sunk 300 feet.', 'shaft')
        self.assertEqual(two['count'], 2)
        self.assertEqual(two['name'], '')            # "Two" is a count, not a name
        self.assertAlmostEqual(two['depth_m'], 300 * FT, places=4)

    def test_element_kinds(self):
        wanted = {'adit': 'An adit was driven N45E 900 feet.',
                  'tunnel': 'A tunnel was driven N45E 900 feet.',
                  'crosscut': 'A crosscut was driven N45E 900 feet on the 300 level.',
                  'drift': 'A drift was driven N45E 900 feet on the 300 level.',
                  'decline': 'A decline was driven N45E 900 feet.',
                  'raise': 'A raise was driven 100 feet from the 500 level to the 400 level.',
                  'winze': 'A winze was sunk 120 feet below the 400 level.',
                  'shaft': 'A shaft was sunk 300 feet.',
                  'stope': 'The vein was stoped for 300 feet on the 200 level.',
                  'pit': 'A glory hole 40 feet across marks the outcrop.',
                  'trench': 'A trench 60 feet long exposes the vein.'}
        for kind, text in wanted.items():
            spec = narrative.parse(text)
            self.assertIn(kind, [e['kind'] for e in spec['elements']], text)

    def test_stope_keeps_its_back_height(self):
        _, el = one('The vein was stoped for 300 feet on the 200 level to a height of 80 feet.', 'stope')
        self.assertAlmostEqual(el['length_m'], 300 * FT, places=4)
        self.assertAlmostEqual(el['height_m'], 80 * FT, places=4)

    def test_names(self):
        for text, name in (('The Main shaft was sunk 300 feet.', 'Main'),
                           ('The No. 2 adit was driven N45E 900 feet.', 'No. 2'),
                           ('An adit was driven N45E 900 feet.', '')):
            _, el = one(text)
            self.assertEqual(el['name'], name, text)


class MentionTests(unittest.TestCase):
    """Workings the text names without describing are counted and quoted, but
    never turned into geometry — an inventory line is not a survey."""

    def test_an_inventory_sentence_produces_mentions_not_elements(self):
        spec = narrative.parse('The mine is developed by two adits and a vertical shaft. '
                               'The shaft was sunk to a depth of 640 feet.')
        self.assertEqual([(e['kind'], e.get('depth_m') is not None) for e in spec['elements']],
                         [('shaft', True)])
        self.assertEqual([(m['id'], m['kind'], m['count']) for m in spec['mentions']],
                         [('m1', 'adit', 2), ('m2', 'shaft', 1)])
        self.assertEqual(spec['coverage']['mentions'], 2)

    def test_a_mention_asks_only_an_optional_question(self):
        spec = narrative.parse('Three adits were driven on the property.')
        self.assertEqual(spec['elements'], [])
        self.assertEqual(len(spec['gaps']), 1)
        gap = spec['gaps'][0]
        self.assertEqual(gap['kind'], 'mention')
        self.assertFalse(gap['required'])
        self.assertIn('3 adits', gap['question'])
        self.assertTrue(gap['quote'])

    def test_an_unstated_count_is_reported_as_unstated(self):
        spec = narrative.parse('A series of raises connect the levels.')
        self.assertEqual([(m['kind'], m['count']) for m in spec['mentions']], [('raise', None)])
        self.assertIn('unstated number', spec['gaps'][0]['question'].lower())

    def test_a_mention_is_not_also_reported_as_an_unparsed_phrasing(self):
        spec = narrative.parse('The mine is developed by two adits.')
        self.assertEqual([g['kind'] for g in spec['gaps']], ['mention'])

    def test_anything_measured_at_all_stays_an_element(self):
        for text in ('A shaft was sunk 300 feet.',
                     'A drift runs N45E 100 feet on the 400 level.',
                     'A crosscut was driven 150 feet from the portal.'):
            self.assertTrue(narrative.parse(text)['elements'], text)
            self.assertEqual(narrative.parse(text)['mentions'], [], text)


class HeightTests(unittest.TestCase):
    """A stated elevation is not a distance travelled.  Reading one as the
    other is how a 1,140-foot adit becomes a 6,450-foot one."""

    def test_an_elevation_is_not_read_as_a_length(self):
        _, el = one('The lower adit, at an elevation of about 6,450 feet, was driven '
                    'S. 62 E. for 1,140 feet.', 'adit')
        self.assertAlmostEqual(el['length_m'], 1140 * FT, places=3)
        self.assertEqual([h['kind'] for h in el['heights']], ['elevation'])
        self.assertAlmostEqual(el['heights'][0]['m'], 6450 * FT, places=3)

    def test_a_vertical_offset_is_not_read_as_a_length(self):
        _, el = one('The upper adit, 180 feet higher, follows the vein northeasterly '
                    'for some 400 feet.', 'adit')
        self.assertAlmostEqual(el['length_m'], 400 * FT, places=3)
        self.assertEqual([h['kind'] for h in el['heights']], ['offset'])

    def test_a_winze_sunk_below_a_level_still_reads_as_a_depth(self):
        _, el = one('A winze was sunk 120 ft below the 400 level.', 'winze')
        self.assertAlmostEqual(el['depth_m'], 120 * FT, places=4)
        self.assertNotIn('heights', el)


class ReferenceTests(unittest.TestCase):
    """A working that is being referred to is not a second working."""

    def test_connecting_with_the_shaft_does_not_invent_a_second_shaft(self):
        spec = narrative.parse('The lower adit connects with the shaft at the 200 level.')
        self.assertEqual([e['kind'] for e in spec['elements']], ['adit'])
        self.assertEqual(spec['elements'][0]['level'], '200')

    def test_stoping_above_a_level_does_not_drive_that_level(self):
        spec = narrative.parse('Stoping above the 300 level extended for about 250 feet '
                               'along the shoot and to a height of 60 feet.')
        self.assertEqual([e['kind'] for e in spec['elements']], ['stope'])
        el = spec['elements'][0]
        self.assertEqual(el['level'], '300')
        self.assertAlmostEqual(el['length_m'], 250 * FT, places=3)
        self.assertAlmostEqual(el['height_m'], 60 * FT, places=3)

    def test_driving_a_level_still_makes_a_drift(self):
        spec = narrative.parse('The 300 level was extended 600 feet N 20 W.')
        self.assertEqual([e['kind'] for e in spec['elements']], ['drift'])


class NoInventionTests(unittest.TestCase):
    """Rule 1. A missing number is a question, never a default."""

    def test_missing_bearing_produces_a_gap_and_no_bearing(self):
        spec = narrative.parse('On the 300 level a drift was extended 450 feet.')
        el = spec['elements'][0]
        self.assertNotIn('bearing_deg', el)
        self.assertIsNone(el.get('bearing_deg'))
        gaps = [g for g in spec['gaps'] if g['field'] == 'bearing_deg' and g['element'] == el['id']]
        self.assertEqual(len(gaps), 1)
        self.assertTrue(gaps[0]['required'])
        self.assertIn('bearing', gaps[0]['question'].lower())

    def test_no_element_is_ever_emitted_with_a_fabricated_number(self):
        prose = ('A shaft was sunk on the vein. A drift was run on the 300 level. '
                 'A crosscut was driven to the east. A raise was put up to surface.')
        spec = narrative.parse(prose)
        for el in spec['elements']:
            for field in ('bearing_deg', 'length_m', 'depth_m'):
                if el.get(field) is None:
                    continue
                # anything present must be traceable to a number in the quote
                self.assertRegex(el['quote'], r'\d', '%s %s' % (el['id'], field))
        # and every required-but-absent field is asked about
        for el in spec['elements']:
            for field in narrative.REQUIRED.get(el['kind'], ()):
                if el.get(field) is None:
                    self.assertTrue(any(g['element'] == el['id'] and g['field'] == field
                                        and g['required'] for g in spec['gaps']),
                                    'unasked %s.%s' % (el['id'], field))

    def test_a_range_is_a_question_not_a_midpoint(self):
        spec = narrative.parse('A drift was extended 400 to 500 feet on the 300 level, bearing 072 degrees.')
        el = spec['elements'][0]
        self.assertIsNone(el.get('length_m'))
        gap = [g for g in spec['gaps'] if g['kind'] == 'range'][0]
        self.assertTrue(gap['required'])
        values = [o['value'] for o in gap['options']]
        self.assertIn(round(400 * FT, 4), values)
        self.assertIn(round(500 * FT, 4), values)
        self.assertIn(None, values)               # "omit" is always available

    def test_every_element_carries_a_verbatim_quote_and_a_real_span(self):
        prose = ('The Main shaft was sunk to a depth of 620 feet. '
                 'On the 300 level a drift was extended 450 feet N45E.')
        spec = narrative.parse(prose)
        self.assertTrue(spec['elements'])
        for el in spec['elements']:
            a, b = el['span']
            self.assertLess(a, b)
            self.assertEqual(' '.join(prose[a:b].split()), el['quote'])

    def test_unknown_phrasing_becomes_a_question_not_a_silent_omission(self):
        spec = narrative.parse('The ore was breasted out overhand above the back of the level.')
        self.assertEqual(spec['elements'], [])
        unparsed = [g for g in spec['gaps'] if g['kind'] == 'unparsed']
        self.assertEqual(len(unparsed), 1)
        self.assertIn('breasted', unparsed[0]['quote'])
        self.assertEqual(spec['coverage']['unparsed_sentences'], 1)

    def test_prose_with_no_workings_yields_nothing_at_all(self):
        spec = narrative.parse('The property lies four miles north of town and is reached by a good road.')
        self.assertEqual(spec['elements'], [])
        self.assertEqual(spec['gaps'], [])


class ConfidenceTests(unittest.TestCase):
    def test_parsed_fields_are_described_and_answers_are_assumed(self):
        spec = narrative.parse('On the 300 level a drift was extended 450 feet.')
        el = spec['elements'][0]
        self.assertEqual(el['fields']['length_m'], 'described')
        self.assertEqual(el['confidence'], 'described')

        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        after = narrative.apply_answers(spec, [{'id': gap['id'], 'value': 45.0,
                                                'because': 'same vein as the adit'}])
        el2 = after['elements'][0]
        self.assertEqual(el2['bearing_deg'], 45.0)
        self.assertEqual(el2['fields']['bearing_deg'], 'assumed')
        self.assertEqual(el2['confidence'], 'assumed')      # weakest field wins
        self.assertEqual(after['answers'][0]['because'], 'same vein as the adit')
        self.assertEqual(after['coverage']['unresolved'], 0)

    def test_answering_null_omits_the_element_rather_than_guessing(self):
        spec = narrative.parse('On the 300 level a drift was extended 450 feet.')
        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        after = narrative.apply_answers(spec, [{'id': gap['id'], 'value': None}])
        self.assertEqual(after['elements'], [])
        self.assertEqual(after['gaps'], [])                  # no orphan questions

    def test_resending_the_same_answer_is_a_no_op_not_an_error(self):
        spec = narrative.parse('On the 300 level a drift was extended 450 feet.')
        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        once = narrative.apply_answers(spec, [{'id': gap['id'], 'value': 45.0}])
        twice = narrative.apply_answers(once, [{'id': gap['id'], 'value': 45.0}])
        self.assertEqual(twice['elements'], once['elements'])
        self.assertEqual(len(twice['answers']), 1)

    def test_changing_a_settled_answer_is_refused_and_says_why(self):
        spec = narrative.parse('On the 300 level a drift was extended 450 feet.')
        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        once = narrative.apply_answers(spec, [{'id': gap['id'], 'value': 45.0}])
        with self.assertRaises(ValueError) as ctx:
            narrative.apply_answers(once, [{'id': gap['id'], 'value': 90.0}])
        self.assertIn('already answered', str(ctx.exception))

    def test_unknown_gap_id_is_rejected(self):
        spec = narrative.parse('A shaft was sunk on the vein.')
        with self.assertRaises(ValueError):
            narrative.apply_answers(spec, [{'id': 'g999', 'value': 1.0}])


class DeterminismTests(unittest.TestCase):
    PROSE = ('The Main shaft was sunk to a depth of 620 feet. An adit driven N45E for 900 feet '
             'cuts the vein on the 300 level. A winze was sunk 120 ft below the 400 level.')

    def test_same_text_gives_a_byte_identical_spec(self):
        a = json.dumps(narrative.parse(self.PROSE), sort_keys=True)
        b = json.dumps(narrative.parse(self.PROSE), sort_keys=True)
        self.assertEqual(a, b)

    def test_spec_id_is_stable_and_text_sensitive(self):
        base = narrative.parse(self.PROSE)
        self.assertEqual(base['spec_id'], narrative.parse(self.PROSE)['spec_id'])
        self.assertNotEqual(base['spec_id'], narrative.parse(self.PROSE + ' A raise was put up.')['spec_id'])
        self.assertNotEqual(base['spec_id'], narrative.parse(self.PROSE, mine_id='grades:12')['spec_id'])

    def test_answers_do_not_change_the_spec_id(self):
        spec = narrative.parse('On the 300 level a drift was extended 450 feet.')
        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        after = narrative.apply_answers(spec, [{'id': gap['id'], 'value': 45.0}])
        self.assertEqual(after['spec_id'], spec['spec_id'])

    def test_ids_are_positional_and_stable(self):
        spec = narrative.parse(self.PROSE)
        self.assertEqual([e['id'] for e in spec['elements']],
                         ['e%d' % i for i in range(1, len(spec['elements']) + 1)])
        self.assertEqual([g['id'] for g in spec['gaps']],
                         ['g%d' % i for i in range(1, len(spec['gaps']) + 1)])


class CoverageTests(unittest.TestCase):
    def test_coverage_reports_what_was_and_was_not_understood(self):
        prose = ('The Main shaft was sunk to a depth of 620 feet. '
                 'The ore was breasted out overhand above the back. '
                 'The property is reached by a good road.')
        cov = narrative.parse(prose)['coverage']
        self.assertEqual(cov['sentences'], 3)
        self.assertEqual(cov['mining_sentences'], 2)
        self.assertEqual(cov['sentences_with_elements'], 1)
        self.assertEqual(cov['unparsed_sentences'], 1)
        self.assertEqual(cov['elements'], 1)
        self.assertGreater(cov['chars'], cov['parsed_chars'])

    def test_abbreviations_do_not_split_sentences(self):
        got = narrative.sentences('The No. 2 adit runs N. 45 E. for 900 ft. The shaft is 300 ft deep.')
        self.assertEqual(len(got), 2)

    def test_a_bearing_ending_in_a_quadrant_letter_still_ends_the_sentence(self):
        prose = ('On the 300 level a drift was extended 450 feet N 20 W. '
                 'The vein was stoped for 300 feet on the 300 level to a height of 80 feet.')
        self.assertEqual(len(narrative.sentences(prose)), 2)
        spec = narrative.parse(prose)
        quotes = set(e['quote'] for e in spec['elements'])
        self.assertEqual(len(quotes), 2, 'each element must quote only its own sentence')
        for el in spec['elements']:
            self.assertNotIn('stoped', el['quote']) if el['kind'] == 'drift' else None


if __name__ == '__main__':
    unittest.main()
