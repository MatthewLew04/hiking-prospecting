"""geomodel_autopopulate — the batch driver: omit policy, index, idempotence.

Terrain is stubbed exactly as the service tests stub it; everything publishes
into a temp dir.  The mine rows come from the committed grades bundle (the
same dependency the service tests take), so ``grades:12`` is a real located
row and no network is touched.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))
sys.path.insert(0, str(ROOT / 'services'))

import geomodel_autopopulate as autop  # noqa: E402
from geomodel import publish, resolve  # noqa: E402

MINE = 'grades:12'   # a located row in the committed bundle

COMPLETE = ('An adit was driven N45E for 900 feet. '
            'A shaft was sunk 300 feet on the vein.')
PARTIAL = COMPLETE + ' On the 300 level a drift was extended 450 feet.'
ONLY_PARTIAL = 'On the 300 level a drift was extended 450 feet.'
NOTHING = 'The claim was located in 1902 and assessment work was recorded.'


def unit(texts, key=MINE, rows=(12,)):
    return {
        'key': key, 'site_kind': 'grades',
        'site_ref': {'kind': 'grades', 'mine_id': key, 'method': 'evidence_name',
                     'name': 'Test mine', 'rows': list(rows), 'located': True},
        'label': 'Test mine', 'grade_rows': list(rows),
        'store_mine_ids': ['ws9-test'], 'methods': ['evidence_name'],
        'texts': [{'doc_id': 'a' * 64, 'title': 'Test Report',
                   'source_url': 'https://example.test/r.pdf',
                   'publication_year': 1920, 'pages': [2], 'span': [0, 10],
                   'text': t, 'citation_pages': []} for t in texts],
        'documents': [{'doc_id': 'a' * 64, 'title': 'Test Report',
                       'source_url': 'https://example.test/r.pdf',
                       'catalog_url': None, 'publication_year': 1920,
                       'citation': 'Test Report, 1920.', 'pages': 4,
                       'cited_pages': [2], 'sections': len(texts)}],
    }


def payload(units):
    return {'units': units, 'parked': [],
            'stats': {'documents': 1, 'linked_store_ids': 1,
                      'citation_links': 0, 'parked': 0, 'mines': len(units),
                      'mines_with_text': len(units),
                      'sections': sum(len(u['texts']) for u in units)}}


def synthetic_grades():
    n = 3400
    cols = {'n': n}
    for name in ('au', 'ag', 'pb', 'zn', 'cu', 'sb', 'wo3', 'hgf'):
        cols[name] = [None] * n
    cols['au'][12] = 0.1
    cols['ag'][12] = 25.0
    return cols


class DriverHarness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.site = base / 'site'
        self.state = base / 'state'
        self.index = base / 'index.json'
        self.ledger = base / 'ledger.json'
        self.site.mkdir()
        patcher = mock.patch.object(resolve, 'elevation',
                                    lambda *a, **k: 1900.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_driver(self, units, **kw):
        args = dict(site_dir=str(self.site), state_dir=str(self.state),
                    context=False, offline=True, log=lambda *a: None,
                    index_path=str(self.index), ledger_path=str(self.ledger),
                    grades=synthetic_grades())
        args.update(kw)
        return autop.run(units=payload(units), **args)

    def read_index(self):
        with open(self.index) as fh:
            return json.load(fh)


class BuildOneTests(DriverHarness):

    def ctx(self):
        return autop.make_context(str(self.state),
                                  publish.LocalTarget(str(self.site)),
                                  None, 13, True, lambda *a: None)

    def test_open_questions_are_answered_omit_and_audited(self):
        got = autop.build_one(MINE, 'grades', {}, PARTIAL, self.ctx(),
                              context=False)
        self.assertEqual(got['state'], 'done')
        self.assertTrue(got['answers'])
        for answer in got['answers']:
            self.assertIsNone(answer['value'])
            self.assertEqual(answer['because'], autop.OMIT_BECAUSE)
        with open(self.site / got['key_prefix'] / 'manifest.json') as fh:
            manifest = json.load(fh)
        recorded = {a['gap'] for a in manifest['answers']}
        self.assertEqual(recorded, {a['id'] for a in got['answers']})
        for a in manifest['answers']:
            self.assertEqual(a['because'], autop.OMIT_BECAUSE)

    def test_the_complete_description_needs_no_answers(self):
        got = autop.build_one(MINE, 'grades', {}, COMPLETE, self.ctx(),
                              context=False)
        self.assertEqual(got['state'], 'done')
        self.assertEqual(got['answers'], [])
        self.assertEqual(got['confidence']['assumed'], 0)

    def test_text_without_workings_is_skipped_not_built(self):
        got = autop.build_one(MINE, 'grades', {}, NOTHING, self.ctx(),
                              context=False)
        self.assertEqual(got, {'state': 'skipped', 'reason': 'no-elements',
                               'coverage': got['coverage'],
                               'mentions': got['mentions']})

    def test_a_description_that_omits_to_nothing_publishes_nothing(self):
        got = autop.build_one(MINE, 'grades', {}, ONLY_PARTIAL, self.ctx(),
                              context=False)
        self.assertEqual(got['state'], 'skipped')
        self.assertEqual(got['reason'], 'all-elements-omitted')
        self.assertFalse(list((self.site / 'models').glob('*'))
                         if (self.site / 'models').exists() else [])

    def test_a_stope_only_description_is_a_model_not_a_false_omission(self):
        # a stope is a mesh, not a workings line; emptiness is judged on
        # built elements, not on the line count
        text = ('The ore was stoped for 100 feet along N. 45 E. on the 300 '
                'level, with a back height of 40 feet.')
        got = autop.build_one(MINE, 'grades', {}, text, self.ctx(),
                              context=False)
        self.assertEqual(got['state'], 'done')
        self.assertTrue((self.site / got['key_prefix']).exists())

    def test_omitted_is_a_count_of_elements_not_of_answers(self):
        # PARTIAL's drift (e3) misses its bearing: one omission, however many
        # null answers the question loop hands over
        got = autop.build_one(MINE, 'grades', {}, PARTIAL, self.ctx(),
                              context=False)
        self.assertEqual(got['state'], 'done')
        self.assertEqual(got['omitted_elements'], ['e3'])
        self.assertEqual(len(got['answers']), 1)


class RunTests(DriverHarness):

    def test_index_entry_carries_the_card_facts(self):
        self.run_driver([unit([COMPLETE])])
        idx = self.read_index()
        entry = idx['by_mine'][MINE]
        self.assertEqual(entry['primary'], entry['models'][0]['model_id'])
        self.assertEqual(entry['minerals'], ['Gold', 'Silver'])
        self.assertIn('adit', entry['extent']['by_type'])
        self.assertIn('shaft', entry['lexicon']['kinds'])
        self.assertEqual(entry['documents'][0]['title'], 'Test Report')
        project = entry['models'][0]['project_url']
        self.assertTrue((self.site / project.lstrip('/')).exists(), project)

    def test_group_rows_alias_to_the_canonical_entry(self):
        self.run_driver([unit([COMPLETE], rows=(12, 868))])
        idx = self.read_index()
        self.assertEqual(idx['by_mine']['grades:868'], {'alias': MINE})

    def test_republishing_unchanged_is_a_no_op_with_a_stable_id(self):
        first = self.run_driver([unit([COMPLETE])])
        second = self.run_driver([unit([COMPLETE])])
        m1 = first['units'][0]['builds'][0]
        m2 = second['units'][0]['builds'][0]
        self.assertEqual(m1['model_id'], m2['model_id'])
        self.assertFalse(m2['republished'])

    def test_a_scoped_run_merges_into_the_existing_index(self):
        self.run_driver([unit([COMPLETE])])
        other = unit([COMPLETE], key='grades:17', rows=(17,))
        self.run_driver([unit([COMPLETE]), other], only=['grades:17'])
        idx = self.read_index()
        self.assertIn(MINE, idx['by_mine'])        # untouched entry survives
        self.assertIn('grades:17', idx['by_mine'])

    def test_documents_without_buildable_text_still_index(self):
        bare = unit([])
        self.run_driver([bare])
        entry = self.read_index()['by_mine'][MINE]
        self.assertEqual(entry['models'], [])
        self.assertIsNone(entry['primary'])
        self.assertEqual(len(entry['documents']), 1)

    def test_dry_run_builds_nothing_and_writes_no_index(self):
        self.run_driver([unit([COMPLETE])], dry_run=True)
        self.assertFalse(self.index.exists())
        self.assertFalse((self.site / 'models').exists())

    def test_only_accepts_an_alias_row(self):
        self.run_driver([unit([COMPLETE], rows=(12, 868))], only=['grades:868'])
        idx = self.read_index()
        self.assertTrue(idx['by_mine'][MINE]['models'])

    def test_a_failed_build_keeps_the_previous_entry_and_its_model(self):
        first = self.run_driver([unit([COMPLETE])])
        model_id = first['units'][0]['builds'][0]['model_id']
        model_dir = self.site / 'models' / model_id
        self.assertTrue(model_dir.exists())
        # now every build errors (offline + no cached elevation)
        with mock.patch.object(resolve, 'elevation', lambda *a, **k: None):
            second = self.run_driver([unit([COMPLETE])])
        self.assertEqual(second['units'][0]['builds'][0]['state'], 'error')
        entry = self.read_index()['by_mine'][MINE]
        self.assertTrue(entry.get('carried_forward'))
        self.assertTrue(entry['models'])          # the old entry stands
        self.assertTrue(model_dir.exists())       # and its files survive


if __name__ == '__main__':
    unittest.main()
