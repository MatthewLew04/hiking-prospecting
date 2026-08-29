"""geomodel.assay — the grades a description quotes, the basis that makes them
mean different things, and the vein it is allowed to draw."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import agentbuild, assay, narrative, render2d  # noqa: E402
from geomodel.narrative import FT  # noqa: E402

SITE = {'name': 'White Caps mine', 'mine_id': 'grades:17', 'lon': -116.87, 'lat': 36.877,
        'elevation_m': 1116.0, 'source_url': 'https://example.invalid/b723'}


def read(text):
    return assay.parse(text, narrative.parse(text))


def one(text):
    got = read(text)['assays']
    assert got, 'no assay read from %r' % text
    return got[0]


class ValueTests(unittest.TestCase):
    def test_ounces_per_ton_with_the_metal_after_the_figure(self):
        a = one('The ore averaged 0.5 ounce gold to the ton.')
        self.assertEqual((a['commodity'], a['value'], a['unit']), ('au', 0.5, 'oz/ton'))

    def test_ounces_with_the_metal_before_the_figure(self):
        a = one('Gold ran 0.5 oz. per ton in the shoot.')
        self.assertEqual((a['commodity'], a['value']), ('au', 0.5))

    def test_abbreviations_and_plurals(self):
        for text in ('assays of 20 oz silver', 'assays of 20 ounces of silver',
                     'assays of 20 oz. silver'):
            a = one('The ore carried %s to the ton.' % text)
            self.assertEqual((a['commodity'], a['value']), ('ag', 20.0), text)

    def test_dollars_a_ton(self):
        for text in ('$19.14 a ton', '$19.14 to the ton', '$19.14 per ton', '$19.14'):
            a = one('The average gross value was %s.' % text)
            self.assertEqual((a['commodity'], a['value'], a['unit']), ('usd', 19.14, '$/ton'), text)

    def test_per_cent_base_metals(self):
        for text, want in (('12 per cent lead', ('pb', 12.0)),
                           ('4.5 percent copper', ('cu', 4.5)),
                           ('8% zinc', ('zn', 8.0))):
            a = one('The concentrate carried %s.' % text)
            self.assertEqual((a['commodity'], a['value']), want, text)
            self.assertEqual(a['unit'], '%')

    def test_thousands_separator(self):
        self.assertEqual(one('Selected samples ran 1,200 ounces of silver.')['value'], 1200.0)

    def test_several_figures_in_one_sentence_are_all_read_in_order(self):
        got = read('Selected samples assayed 40 ounces of silver and 12 per cent lead.')['assays']
        self.assertEqual([(a['commodity'], a['value']) for a in got],
                         [('ag', 40.0), ('pb', 12.0)])
        self.assertEqual([a['id'] for a in got], ['a1', 'a2'])

    def test_a_figure_with_no_metal_named_is_a_question_not_a_guess(self):
        got = read('Assays ran 30 ounces to the ton.')
        self.assertIsNone(got['assays'][0]['commodity'])
        gap = got['gaps'][0]
        self.assertEqual(gap['field'], 'commodity')
        self.assertFalse(gap['required'], 'an unnamed metal must not block the model')
        self.assertIn(None, [o['value'] for o in gap['options']])

    def test_prose_with_no_grades_reads_nothing(self):
        got = read('An adit was driven N45E for 900 feet.')
        self.assertEqual(got['assays'], [])
        self.assertEqual(got['gaps'], [])


class BasisTests(unittest.TestCase):
    """A picked sample and a mill average are different claims about a mine."""

    def test_selected_samples_are_marked_selected(self):
        for text in ('Selected samples assayed 40 ounces of silver.',
                     'Picked ore ran 40 ounces of silver.',
                     'The bonanza ore carried 40 ounces of silver.',
                     'High-grade shoots assayed 40 ounces of silver.'):
            self.assertEqual(one(text)['basis'], 'selected', text)

    def test_averages_are_marked_average(self):
        for text in ('The ore averaged 0.5 ounce gold.',
                     'Mill heads ran 0.5 ounce gold.',
                     'Production averaged 0.5 ounce gold to the ton.'):
            self.assertEqual(one(text)['basis'], 'average', text)

    def test_a_shipment_is_neither(self):
        self.assertEqual(one('A carload shipped 40 ounces of silver to the ton.')['basis'],
                         'shipment')

    def test_a_bare_assay_claims_nothing_more_than_assay(self):
        self.assertEqual(one('The vein assayed 0.5 ounce gold.')['basis'], 'assay')


class WidthTests(unittest.TestCase):
    def test_widths_in_feet_inches_and_metres(self):
        cases = (('across 3 feet', 3 * FT), ('over a width of 3 feet', 3 * FT),
                 ('across 8 inches', 8 * FT / 12.0), ('across 1.5 m', 1.5))
        for text, want in cases:
            a = one('The ore averaged 0.5 ounce gold %s.' % text)
            self.assertAlmostEqual(a['width_m'], want, places=5, msg=text)

    def test_a_hyphenated_width_before_the_noun(self):
        a = one('A 3-foot streak averaged 0.5 ounce gold to the ton.')
        self.assertAlmostEqual(a['width_m'], 3 * FT, places=5)

    def test_no_width_stated_is_none_not_a_default(self):
        self.assertIsNone(one('The ore averaged 0.5 ounce gold.')['width_m'])


class AttachmentTests(unittest.TestCase):
    def test_an_assay_attaches_to_the_working_just_described(self):
        text = ('On the 300 level a drift was extended 450 feet N 20 W; the ore averaged '
                '0.5 ounce gold to the ton. An adit was driven N45E 900 feet; assays ran '
                '12 per cent lead.')
        spec = assay.attach(narrative.parse(text), text)
        by_id = dict((e['id'], e['kind']) for e in spec['elements'])
        got = dict((a['commodity'], by_id.get(a['element'])) for a in spec['assays'])
        self.assertEqual(got, {'au': 'drift', 'pb': 'adit'})

    def test_an_assay_before_any_working_attaches_to_nothing(self):
        text = 'The ore averaged 0.5 ounce gold. An adit was driven N45E 900 feet.'
        spec = assay.attach(narrative.parse(text), text)
        self.assertIsNone(spec['assays'][0]['element'])

    def test_attaching_records_coverage_and_keeps_gap_ids_contiguous(self):
        text = ('On the 300 level a drift was extended 450 feet; assays ran 30 ounces '
                'to the ton.')
        spec = assay.attach(narrative.parse(text), text)
        self.assertEqual(spec['coverage']['assays'], 1)
        self.assertEqual([g['id'] for g in spec['gaps']],
                         ['g%d' % i for i in range(1, len(spec['gaps']) + 1)])

    def test_every_assay_carries_its_sentence(self):
        text = 'An adit was driven N45E 900 feet; the ore averaged 0.5 ounce gold.'
        spec = assay.attach(narrative.parse(text), text)
        a = spec['assays'][0]
        lo, hi = a['span']
        self.assertEqual(' '.join(text[lo:hi].split()), a['quote'])
        self.assertIn('0.5 ounce gold', a['quote'])


class VeinTests(unittest.TestCase):
    def test_a_stated_strike_and_dip_give_a_vein(self):
        v = assay.parse_vein('The vein strikes N45E and dips 70 degrees to the northwest.')
        self.assertEqual((v['strike_deg'], v['dip_deg'], v['dip_direction_deg']),
                         (45.0, 70.0, 315.0))
        self.assertFalse(v['dip_direction_assumed'])

    def test_a_dip_with_no_direction_says_the_direction_was_assumed(self):
        v = assay.parse_vein('The vein strikes N45E and dips 70 degrees.')
        self.assertEqual(v['dip_direction_deg'], 135.0)
        self.assertTrue(v['dip_direction_assumed'])

    def test_a_strike_without_a_dip_is_not_a_surface(self):
        self.assertIsNone(assay.parse_vein('The vein strikes N45E for 900 feet.'))

    def test_a_dip_without_a_strike_is_not_a_surface(self):
        self.assertIsNone(assay.parse_vein('The vein dips 70 degrees northwest.'))

    def test_no_vein_mentioned_at_all(self):
        self.assertIsNone(assay.parse_vein('An adit was driven N45E, dipping 2 degrees.'))


class ProjectTests(unittest.TestCase):
    TEXT = ('The vein strikes N45E and dips 70 degrees to the northwest. '
            'The Main shaft was sunk 620 feet. '
            'On the 300 level a drift was extended 450 feet N 20 W; the ore averaged '
            '0.5 ounce gold to the ton across 3 feet. '
            'Selected samples assayed 40 ounces of silver.')

    def built(self, text=None):
        text = text or self.TEXT
        spec = assay.attach(narrative.parse(text), text)
        return spec, agentbuild.build(spec, dict(SITE))

    def test_grade_points_sit_on_the_working_they_were_quoted_for(self):
        spec, built = self.built()
        self.assertEqual(built['assays'], 2)
        ps = [o for o in built['project'].objects
              if o.kind == 'points' and (o.metadata or {}).get('schema') == 'nwmm-assay/1'][0]
        drift = [p for p in built['placed'] if p['kind'] == 'drift'][0]
        x, y, z = ps.point(0)
        self.assertAlmostEqual(x, (drift['start'][0] + drift['end'][0]) / 2.0, places=3)
        self.assertAlmostEqual(z, drift['start'][2], places=3)

    def test_every_grade_point_keeps_its_basis_and_its_quote(self):
        spec, built = self.built()
        ps = [o for o in built['project'].objects
              if o.kind == 'points' and (o.metadata or {}).get('schema') == 'nwmm-assay/1'][0]
        self.assertEqual(sorted(ps.attributes['basis']), ['average', 'selected'])
        for q in ps.attributes['quote']:
            self.assertTrue(q)

    def test_an_assay_with_no_metal_is_left_out_rather_than_plotted_as_unknown(self):
        spec, built = self.built('An adit was driven N45E 900 feet; assays ran 30 ounces '
                                 'to the ton.')
        self.assertEqual(built['assays'], 0)

    def test_the_vein_surface_is_a_stated_attitude_and_says_so(self):
        spec, built = self.built()
        mesh = [o for o in built['project'].objects if getattr(o, 'role', '') == 'vein'][0]
        self.assertEqual(mesh.metadata['strike_deg'], 45.0)
        self.assertEqual(mesh.metadata['dip_deg'], 70.0)
        self.assertEqual(mesh.metadata['anchor'], 'centre of the described workings')
        self.assertIn('not an interpolated', mesh.metadata['note'])
        self.assertEqual(mesh.n_triangles, 2)

    def test_the_vein_plane_really_has_the_stated_dip(self):
        import math

        spec, built = self.built()
        mesh = [o for o in built['project'].objects if getattr(o, 'role', '') == 'vein'][0]
        v = [mesh.vertex(i) for i in range(4)]
        # normal from two edges, then the angle between it and vertical
        e1 = [v[1][k] - v[0][k] for k in range(3)]
        e2 = [v[3][k] - v[0][k] for k in range(3)]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        dip = math.degrees(math.acos(abs(nz) / math.sqrt(nx * nx + ny * ny + nz * nz)))
        self.assertAlmostEqual(dip, 70.0, places=3)

    def test_no_grade_surface_is_ever_interpolated(self):
        spec, built = self.built()
        roles = [getattr(o, 'role', '') for o in built['project'].objects]
        self.assertNotIn('topography', roles)
        self.assertEqual([r for r in roles if r == 'vein'], ['vein'])


class RenderTests(unittest.TestCase):
    def built(self):
        text = ProjectTests.TEXT
        spec = assay.attach(narrative.parse(text), text)
        return agentbuild.build(spec, dict(SITE))

    def test_a_selected_sample_is_drawn_hollow_and_an_average_filled(self):
        svg = render2d.plan(self.built())
        body = svg[:svg.index('class="assay-key"')]
        hollow = re.findall(r'<circle[^>]*fill="none"[^>]*stroke-dasharray[^>]*/>', body)
        filled = re.findall(r'<circle[^>]*fill="%s"' % re.escape(render2d.ASSAY), body)
        self.assertEqual(len(hollow), 1)
        self.assertEqual(len(filled), 1)

    def test_the_grade_and_metal_are_labelled(self):
        svg = render2d.plan(self.built())
        self.assertIn('0.5 AU', svg)
        self.assertIn('40 AG', svg)

    def test_the_key_explains_the_two_markers(self):
        svg = render2d.plan(self.built())
        self.assertIn('selected sample', svg)
        self.assertIn('representative', svg)

    def test_the_vein_trace_is_drawn_with_its_attitude(self):
        svg = render2d.plan(self.built())
        self.assertIn('class="vein"', svg)
        self.assertIn('vein 045', svg)

    def test_assays_appear_in_section_too(self):
        svg = render2d.section(self.built())
        self.assertIn('class="assays"', svg)

    def test_a_model_with_no_assays_draws_no_key(self):
        spec = narrative.parse('An adit was driven N45E 900 feet.')
        built = agentbuild.build(spec, dict(SITE))
        svg = render2d.plan(built)
        self.assertNotIn('assay-key', svg)
        self.assertNotIn('class="assays"', svg)


if __name__ == '__main__':
    unittest.main()
