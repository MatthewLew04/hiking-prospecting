"""geomodel.render2d — the three static views, and the line style that keeps a
described working from passing for a surveyed one."""
import math
import re
import sys
import unittest
import xml.dom.minidom
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import agentbuild, narrative, render2d, workings as wk  # noqa: E402
from geomodel.model import Grid2D, farray  # noqa: E402

SITE = {'name': 'Silver King', 'lon': -116.87, 'lat': 36.877, 'elevation_m': 1900.0}

PROSE = ('The Main shaft was sunk to a depth of 620 feet. '
         'An adit driven N45E for 900 feet cuts the vein. '
         'On the 300 level a drift was extended 450 feet N 20 W. '
         'The vein was stoped for 300 feet on the 300 level to a height of 80 feet.')


def build(prose=PROSE, answers=None):
    spec = narrative.parse(prose)
    if answers:
        spec = narrative.apply_answers(spec, answers)
    return agentbuild.build(spec, dict(SITE))


class WellFormednessTests(unittest.TestCase):
    def test_every_view_is_parseable_svg_with_a_viewbox(self):
        built = build()
        for name, svg in render2d.render(built).items():
            doc = xml.dom.minidom.parseString(svg)
            root = doc.documentElement
            self.assertEqual(root.tagName, 'svg', name)
            self.assertEqual(root.getAttribute('xmlns'), 'http://www.w3.org/2000/svg', name)
            self.assertTrue(root.getAttribute('viewBox'), name)
            self.assertTrue(root.getAttribute('aria-label'), name)

    def test_names_are_escaped_not_injected(self):
        built = build()
        built['project'].name = 'Bell & <script>Co</script>'
        svg = render2d.plan(built)
        xml.dom.minidom.parseString(svg)
        self.assertNotIn('<script>', svg)
        self.assertIn('&amp;', svg)

    def test_a_model_with_nothing_placeable_still_renders_and_says_so(self):
        built = build('A drift was extended 450 feet N45E on the No. 3 level.')
        self.assertEqual(built['placed'], [])
        for svg in render2d.render(built).values():
            xml.dom.minidom.parseString(svg)
            self.assertIn('no placeable workings', svg)

    def test_the_requested_views_are_the_views_returned(self):
        built = build()
        self.assertEqual(sorted(render2d.render(built, views=('plan', 'iso'))), ['iso', 'plan'])


class ConfidenceStyleTests(unittest.TestCase):
    """Rule: a described adit must never look like a surveyed one."""

    def test_described_is_dashed_and_assumed_is_dotted(self):
        spec = narrative.parse('The Main shaft was sunk 620 feet. '
                               'On the 300 level a drift was extended 450 feet.')
        gap = [g for g in spec['gaps'] if g['field'] == 'bearing_deg'][0]
        built = agentbuild.build(narrative.apply_answers(spec, [{'id': gap['id'], 'value': 45.0}]),
                                 dict(SITE))
        svg = render2d.plan(built)
        drift = re.search(r'<polyline points="[^"]+" fill="none" stroke="%s"[^>]*/>'
                          % re.escape(render2d._rgb(wk.TYPES['drift']['color'])), svg)
        self.assertIsNotNone(drift)
        self.assertIn('stroke-dasharray="%s"' % render2d.DASH['assumed'], drift.group(0))
        shaft = re.search(r'<rect[^>]*stroke="%s"[^>]*/>'
                          % re.escape(render2d._rgb(wk.TYPES['shaft']['color'])), svg)
        self.assertIn('stroke-dasharray="%s"' % render2d.DASH['described'], shaft.group(0))

    def test_nothing_from_prose_is_ever_drawn_solid(self):
        built = build()
        for svg in render2d.render(built).values():
            for line in re.findall(r'<(?:polyline|polygon|rect)[^>]*stroke-width="2\.2"[^>]*/>', svg):
                self.assertIn('stroke-dasharray', line, line)

    def test_the_legend_states_the_counts(self):
        built = build()
        svg = render2d.plan(built)
        self.assertIn('surveyed — 0', svg)
        self.assertIn('described — %d' % len(built['placed']), svg)
        self.assertIn('assumed — 0', svg)

    def test_the_subtitle_says_in_words_that_this_is_not_a_survey(self):
        for svg in render2d.render(build()).values():
            self.assertIn('not a survey', svg)


class GeometryTests(unittest.TestCase):
    def test_plan_puts_north_up_and_east_right(self):
        built = build('An adit driven due north for 900 feet. '
                      'A tunnel driven due east for 900 feet.')
        svg = render2d.plan(built)
        lines = re.findall(r'<polyline points="([^"]+)"', svg)
        self.assertEqual(len(lines), 2)
        north = [tuple(map(float, p.split(','))) for p in lines[0].split()]
        east = [tuple(map(float, p.split(','))) for p in lines[1].split()]
        self.assertLess(north[-1][1], north[0][1])            # SVG y grows downward
        self.assertAlmostEqual(north[-1][0], north[0][0], places=1)
        self.assertGreater(east[-1][0], east[0][0])
        self.assertAlmostEqual(east[-1][1], east[0][1], places=1)

    def test_a_vertical_shaft_becomes_a_collar_square_in_plan_not_a_dot(self):
        built = build('The Main shaft was sunk 620 feet. An adit driven N45E for 900 feet.')
        svg = render2d.plan(built)
        self.assertEqual(len(re.findall(r'<rect x=', svg)), 1)
        for pts in re.findall(r'<polyline points="([^"]+)"', svg):
            first, last = pts.split()[0], pts.split()[-1]
            self.assertNotEqual(first, last, 'a zero-length polyline is invisible')

    def test_section_places_the_levels_at_their_elevations_and_labels_them(self):
        built = build()
        svg = render2d.section(built)
        self.assertIn('300 level', svg)
        self.assertIn(render2d._n(built['levels']['300']), svg)

    def test_the_dominant_bearing_folds_to_a_strike(self):
        built = build('An adit driven due north for 900 feet.')
        self.assertAlmostEqual(render2d._dominant_bearing(built), 0.0, places=2)
        built = build('An adit driven due east for 900 feet.')
        self.assertAlmostEqual(render2d._dominant_bearing(built), 90.0, places=2)
        built = build('An adit driven N45E for 900 feet. A tunnel driven S45W for 900 feet.')
        self.assertAlmostEqual(render2d._dominant_bearing(built), 45.0, places=2)

    def test_a_stope_is_drawn_as_a_filled_outline(self):
        built = build()
        svg = render2d.plan(built)
        polys = re.findall(r'<polygon[^>]*/>', svg)
        self.assertEqual(len(polys), 1)
        self.assertIn('fill-opacity', polys[0])


class ScaleBarTests(unittest.TestCase):
    def test_the_bar_is_a_round_number_and_its_pixels_match_its_metres(self):
        built = build()
        svg = render2d.plan(built)
        pts = [p for _, _, _, ps in render2d._parts(built) for p in ps]
        view = render2d.View([(p[0], p[1]) for p in pts])
        m = re.search(r'<g class="scale">.*?</g>', svg)
        xs = [float(v) for v in re.findall(r'x1="([-\d.]+)"', m.group(0))]
        x2 = float(re.search(r'x2="([-\d.]+)"', m.group(0)).group(1))
        pixels = x2 - min(xs)
        metres = float(re.search(r'>([\d.]+) m', m.group(0)).group(1))
        self.assertIn(metres / 10 ** math.floor(math.log10(metres)), (1.0, 2.0, 5.0))
        self.assertAlmostEqual(pixels, metres * view.k, places=1)

    def test_the_bar_is_labelled_in_feet_as_well_because_the_source_was(self):
        svg = render2d.plan(build())
        m = re.search(r'>([\d.]+) m  ·  ([\d.]+) ft<', svg)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(2)) * wk.FT, float(m.group(1)), delta=float(m.group(1)) * 0.02)

    def test_isometric_declines_to_show_a_scale_bar(self):
        svg = render2d.iso(build())
        self.assertNotIn('<g class="scale">', svg)
        self.assertIn('no single scale', svg)


class ContourTests(unittest.TestCase):
    def test_contours_are_drawn_when_there_is_terrain(self):
        built = build()
        g = Grid2D(21, 21, built['collar']['x'] - 500, built['collar']['y'] - 500, 50, 50,
                   name='Topography', role='topography')
        g.values = farray([1850.0 + 0.4 * (i + j) for j in range(21) for i in range(21)])
        built['project'].add(g)
        svg = render2d.plan(built)
        self.assertIn('<g class="contours">', svg)

    def test_contours_are_clipped_to_the_drawing_and_do_not_reach_the_title(self):
        built = build()
        # terrain covering far more ground than the workings, as a real site does
        g = Grid2D(81, 81, built['collar']['x'] - 2000, built['collar']['y'] - 2000, 50, 50,
                   name='Topography', role='topography')
        g.values = farray([1800.0 + 2.5 * (i + j) for j in range(81) for i in range(81)])
        built['project'].add(g)
        svg = render2d.plan(built)
        segs = re.findall(r'<line x1="([-\d.]+)" y1="([-\d.]+)" x2="([-\d.]+)" y2="([-\d.]+)" '
                          r'stroke="%s"' % re.escape(render2d.CONTOUR), svg)
        self.assertTrue(segs)
        xmin, ymin, xmax, ymax = render2d.PLOT
        for x0, y0, x1, y1 in segs:
            for x in (float(x0), float(x1)):
                self.assertGreaterEqual(x, xmin - 0.01)
                self.assertLessEqual(x, xmax + 0.01)
            for y in (float(y0), float(y1)):
                self.assertGreaterEqual(y, ymin - 0.01)
                self.assertLessEqual(y, ymax + 0.01)

    def test_clipping_keeps_a_segment_that_only_partly_overlaps(self):
        box = (0.0, 0.0, 10.0, 10.0)
        self.assertEqual(render2d._clip(-5.0, 5.0, 5.0, 5.0, box), (0.0, 5.0, 5.0, 5.0))
        self.assertEqual(render2d._clip(2.0, 2.0, 8.0, 8.0, box), (2.0, 2.0, 8.0, 8.0))
        self.assertIsNone(render2d._clip(-5.0, -5.0, -1.0, -1.0, box))
        self.assertIsNone(render2d._clip(20.0, 1.0, 30.0, 1.0, box))

    def test_no_terrain_draws_no_contours_rather_than_a_fiction(self):
        svg = render2d.plan(build())
        self.assertNotIn('<g class="contours">', svg)


class DeterminismTests(unittest.TestCase):
    def test_the_same_model_renders_the_same_bytes(self):
        a, b = build(), build()
        for name in ('plan', 'section', 'iso'):
            self.assertEqual(render2d.render(a)[name], render2d.render(b)[name], name)

    def test_no_wall_clock_leaks_into_the_drawing(self):
        for svg in render2d.render(build()).values():
            self.assertNotRegex(svg, r'20\d\d-\d\d-\d\dT')


if __name__ == '__main__':
    unittest.main()
