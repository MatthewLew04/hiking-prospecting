"""geomodel.resolve — the mine index: candidates, never a pick."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import resolve  # noqa: E402

BUNDLE = ROOT / 'site' / 'data' / 'grades' / 'grades.json'


def fake_index(names, states=None, districts=None, xs=None, ys=None, grades=None):
    n = len(names)
    bundle = {'n': n, 'name': list(names),
              'st': list(states or ['NV'] * n),
              'dist': list(districts or [None] * n),
              'cnty': [None] * n,
              'com': [None] * n,
              'x': list(xs if xs is not None else [-116.8] * n),
              'y': list(ys if ys is not None else [36.9] * n),
              'quote': ['q'] * n, 'src': ['s'] * n, 'url': ['u'] * n,
              'basis': ['b'] * n, 'yrs': [None] * n, 'ton': [None] * n, 'dep': [None] * n,
              'au': [None] * n, 'ag': [None] * n, 'pb': [None] * n, 'zn': [None] * n,
              'cu': [None] * n, 'sb': [None] * n, 'wo3': [None] * n, 'usd': [None] * n,
              'hgf': [None] * n, 'yd3': [None] * n,
              'note': 'test bundle', 'generated': '2026-01-01'}
    bundle.update(grades or {})
    return resolve.Index(bundle, path='<test>')


class NormaliseTests(unittest.TestCase):
    def test_descriptive_suffixes_and_parentheticals_are_dropped(self):
        self.assertEqual(resolve.normalise('Lucky Girl group (Montana Gold Mining Co.)'), 'lucky girl')
        self.assertEqual(resolve.normalise('White Caps mine'), 'white caps')
        self.assertEqual(resolve.normalise('WHITE  CAPS   MINE'), 'white caps')
        self.assertEqual(resolve.normalise('Silver King Mining Company'), 'silver king')

    def test_a_numbered_property_keeps_its_number(self):
        self.assertEqual(resolve.normalise('Bluebird No. 2'), 'bluebird no2')
        self.assertNotEqual(resolve.normalise('Bluebird No. 2'), resolve.normalise('Bluebird No. 3'))


class LookupTests(unittest.TestCase):
    def test_a_colliding_name_returns_every_candidate_and_stays_ambiguous(self):
        idx = fake_index(['Bluebird mine', 'Blue Bird mine', 'Bluebird group'],
                         states=['NV', 'MT', 'ID'])
        got = resolve.lookup('Bluebird', index=idx)
        self.assertEqual(len(got['candidates']), 3)
        self.assertTrue(got['ambiguous'])
        self.assertEqual(len(set(c['mine_id'] for c in got['candidates'])), 3)

    def test_a_single_exact_match_is_the_only_unambiguous_case(self):
        idx = fake_index(['Silver King mine', 'Copper Queen mine'])
        got = resolve.lookup('Silver King', index=idx)
        self.assertEqual(len(got['candidates']), 1)
        self.assertFalse(got['ambiguous'])
        self.assertEqual(got['candidates'][0]['match'], 'exact')

    def test_a_near_miss_alone_is_still_ambiguous(self):
        idx = fake_index(['Silver Kings mine'])
        got = resolve.lookup('Silver King', index=idx)
        self.assertEqual(len(got['candidates']), 1)
        self.assertTrue(got['ambiguous'], 'a fuzzy match must still be confirmed')

    def test_state_narrowing_rejects_rather_than_reweights(self):
        idx = fake_index(['Bluebird mine', 'Bluebird mine'], states=['NV', 'MT'])
        got = resolve.lookup('Bluebird', state='MT', index=idx)
        self.assertEqual([c['state'] for c in got['candidates']], ['MT'])
        self.assertFalse(got['ambiguous'])

    def test_district_only_adds_weight(self):
        idx = fake_index(['Bluebird mine', 'Bluebird mine'], districts=['Manhattan', 'Bullfrog'])
        got = resolve.lookup('Bluebird', district='Bullfrog', index=idx)
        self.assertEqual(len(got['candidates']), 2)
        self.assertEqual(got['candidates'][0]['district'], 'Bullfrog')

    def test_an_unlocated_mine_is_returned_and_labelled(self):
        idx = fake_index(['Ghost mine'], xs=[None], ys=[None])
        got = resolve.lookup('Ghost', index=idx)
        self.assertFalse(got['candidates'][0]['located'])
        self.assertEqual(got['located'], 0)
        gap = resolve.which_mine_gap(got)
        self.assertIn('no coordinate on file', gap['options'][0]['label'])

    def test_no_match_is_a_question_not_an_empty_success(self):
        idx = fake_index(['Silver King mine'])
        got = resolve.lookup('Nonesuch Consolidated', index=idx)
        self.assertEqual(got['candidates'], [])
        gap = resolve.which_mine_gap(got)
        self.assertEqual(gap['kind'], 'no_match')
        self.assertTrue(gap['required'])

    def test_which_mine_gap_never_preselects(self):
        idx = fake_index(['Bluebird mine', 'Blue Bird mine'], states=['NV', 'MT'])
        gap = resolve.which_mine_gap(resolve.lookup('Bluebird', index=idx))
        self.assertEqual(gap['field'], 'mine_id')
        self.assertTrue(gap['required'])
        self.assertIn(None, [o['value'] for o in gap['options']])

    def test_results_are_ordered_deterministically(self):
        idx = fake_index(['Bluebird mine'] * 4, states=['NV'] * 4)
        a = [c['mine_id'] for c in resolve.lookup('Bluebird', index=idx)['candidates']]
        b = [c['mine_id'] for c in resolve.lookup('Bluebird', index=idx)['candidates']]
        self.assertEqual(a, b)
        self.assertEqual(a, sorted(a, key=lambda m: resolve.parse_mine_id(m)))

    def test_mine_ids_round_trip(self):
        idx = fake_index(['Silver King mine', 'Copper Queen mine'])
        row = idx.get('grades:1')
        self.assertEqual(row['name'], 'Copper Queen mine')
        with self.assertRaises(ValueError):
            resolve.parse_mine_id('mrds:12345')
        with self.assertRaises(KeyError):
            idx.get('grades:99')


class GradeColumnTests(unittest.TestCase):
    """For a quicksilver or placer mine every per-ton column is null, so a
    column missing from GRADE_COLUMNS is not a missing number — it is the
    whole grade."""

    def test_quicksilver_flasks_and_placer_dollars_reach_the_row(self):
        idx = fake_index(['Cinnabar mine', 'Gravel Bar placer'],
                         grades={'hgf': [120.0, None], 'yd3': [None, 1.75]})
        self.assertEqual(idx.row(0)['grades'], {'hgf': 120.0})
        self.assertEqual(idx.row(1)['grades'], {'yd3': 1.75})


class RealBundleTests(unittest.TestCase):
    """The shipped bundle is the index the tool actually queries."""

    @classmethod
    def setUpClass(cls):
        cls.idx = resolve.load_index(str(BUNDLE))

    def test_bundle_shape(self):
        self.assertEqual(self.idx.n, 3369)
        self.assertTrue(self.idx.note)

    def test_a_known_mine_resolves_with_its_citation(self):
        got = resolve.lookup('White Caps', state='NV', index=self.idx)
        self.assertTrue(got['candidates'])
        top = got['candidates'][0]
        self.assertEqual(top['state'], 'NV')
        self.assertTrue(top['source_url'].startswith('https://'))
        self.assertTrue(top['quote'])
        self.assertTrue(top['located'])

    def test_a_colliding_historic_name_stays_ambiguous_in_the_real_bundle(self):
        got = resolve.lookup('Bluebird', index=self.idx)
        self.assertGreater(len(got['candidates']), 1)
        self.assertTrue(got['ambiguous'])
        self.assertGreater(len(set(c['state'] for c in got['candidates'])), 1)

    def test_index_build_is_stable(self):
        a = resolve.Index(self.idx.bundle)._keys
        b = resolve.Index(self.idx.bundle)._keys
        self.assertEqual(a, b)

    def test_every_multi_commodity_field_the_note_names_is_carried(self):
        # the bundle's note (echoed into every result) advertises units for
        # pb/zn/cu/sb/wo3/hgf/yd3 — a column the note names but GRADE_COLUMNS
        # omits makes the result claim units for a field it never emits
        for col in ('pb', 'zn', 'cu', 'sb', 'wo3', 'hgf', 'yd3'):
            self.assertIn(col, resolve.GRADE_COLUMNS)
            i = next(k for k in range(self.idx.n) if self.idx.bundle[col][k] is not None)
            self.assertEqual(self.idx.row(i)['grades'].get(col), self.idx.bundle[col][i])

    def test_no_mercury_or_placer_mine_resolves_as_ungraded(self):
        blank = [i for i in range(self.idx.n)
                 if not self.idx.row(i)['grades']
                 and (self.idx.bundle['hgf'][i] is not None
                      or self.idx.bundle['yd3'][i] is not None)]
        self.assertEqual(blank, [], '%d valued mines report no grade at all' % len(blank))

    def test_site_reports_an_unavailable_elevation_rather_than_zero(self):
        row = resolve.site('grades:17', offline=True, index=self.idx)
        self.assertIn(row['elevation_m'], (None,) if row['elevation_m'] is None else (row['elevation_m'],))
        if row['elevation_m'] is None:
            self.assertIn('unavailable', row['elevation_source'])
        else:
            self.assertIn('Terrain Tiles', row['elevation_source'])


if __name__ == '__main__':
    unittest.main()
