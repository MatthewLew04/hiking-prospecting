"""geomodel_autopopulate — the batch driver: omit policy, index, idempotence.

Terrain is stubbed exactly as the service tests stub it; everything publishes
into a temp dir.  The mine rows come from the committed grades bundle (the
same dependency the service tests take), so ``grades:12`` is a real located
row and no network is touched.

``WriteIndexTests`` exercise the index writer on synthetic results with no
build at all: the compact schema-2 rows, one ``card.json`` per model, alias
rows, carry-forward from a previous index of either schema, and the size
budget that lets the index scale to tens of thousands of mines.
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

COMPACT_KEYS = {'l', 'p', 'n', 'm', 'x', 'w', 'c'}


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


DOC = {'doc_id': 'a' * 64, 'title': 'The Divide Silver District, Nevada',
       'source_url': 'https://pubs.usgs.gov/bul/0715k/report.pdf',
       'catalog_url': None, 'publication_year': 1921,
       'citation': 'Knopf, A., 1921, USGS Bulletin 715-K.', 'pages': 30,
       'cited_pages': [2, 7], 'sections': 8}


def model(mid, primary=True, described=7, assumed=0, surveyed=0, total_m=1039.7,
          year=1921):
    return {
        'model_id': mid,
        'project_url': '/models/%s/model.geomodel.json' % mid,
        'model_url': 'model3d.html?project=/models/%s/model.geomodel.json' % mid,
        'doc_id': DOC['doc_id'], 'doc_title': DOC['title'],
        'source_url': DOC['source_url'], 'publication_year': year, 'pages': [2, 3],
        'confidence': {'surveyed': surveyed, 'described': described, 'assumed': assumed},
        'elements': surveyed + described + assumed, 'omitted': 14,
        'summary': {'total_m': total_m, 'by_type': {'shaft': 902.5, 'crosscut': 137.2}},
        'levels': ['45', '100'], 'level_depths_m': {'45': 13.7, '100': 30.5},
        'assay_commodities': ['ag', 'au'], 'assays': 3, 'vein': True,
        'republished': False, 'primary': primary,
    }


def result(key, label='Tonopah Divide mine', n_models=1, minerals=('Gold', 'Silver'),
           rows=(), errored=False, site_kind='grades', mid=None):
    ms = [model((mid or 'tonopah-divide-mine-c0125b0') + str(i), primary=(i == 0),
                described=7 - i) for i in range(n_models)]
    extent = ({'total_m': 1039.7, 'by_type': {'shaft': 902.5, 'crosscut': 137.2},
               'levels': ['45', '100'], 'deepest_level_m': 30.5} if ms else None)
    return {'key': key, 'label': label, 'site_kind': site_kind,
            'methods': ['citation_quote'], 'store_mine_ids': ['ws9-nv-tonopah-divide'],
            'grade_rows': list(rows), 'documents': [dict(DOC)],
            'models': ms, 'primary': ms[0]['model_id'] if ms else None,
            'lexicon': {'kinds': {'shaft': {'count': 16, 'surfaces': {'shaft': 16}}},
                        'verbs': {'sunk': 2}, 'level_labels': ['100'],
                        'sentences': 40, 'mining_sentences': 22},
            'minerals': list(minerals), 'extent': extent, 'errored': errored}


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

    def read_card(self, model_id):
        with open(self.site / 'models' / model_id / 'card.json') as fh:
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

    def test_index_row_is_compact_and_the_card_carries_the_facts(self):
        self.run_driver([unit([COMPLETE])])
        idx = self.read_index()
        self.assertEqual(idx['schema_version'], 2)
        row = idx['by_mine'][MINE]
        self.assertEqual(set(row), COMPACT_KEYS)
        self.assertEqual(row['n'], 1)
        self.assertEqual(row['m'], ['Gold', 'Silver'])
        self.assertEqual(row['l'], 'Test mine')
        card = self.read_card(row['p'])
        self.assertEqual(card['primary'], row['p'])
        self.assertEqual(card['models'][0]['model_id'], row['p'])
        self.assertEqual(row['x'], round(card['extent']['total_m']))
        self.assertEqual(row['w'], card['models'][0]['elements'])
        self.assertEqual(row['c'], [card['models'][0]['confidence']['described'], 0, 0])
        self.assertIn('adit', card['extent']['by_type'])
        self.assertIn('shaft', card['lexicon']['kinds'])
        self.assertEqual(card['documents'][0]['title'], 'Test Report')
        # the full record keeps what the index row leaves out
        self.assertIn('summary', card['models'][0])
        self.assertIn('levels', card['models'][0])
        self.assertIn('level_depths_m', card['models'][0])
        self.assertEqual(card['methods'], ['evidence_name'])
        project = card['models'][0]['project_url']
        self.assertTrue((self.site / project.lstrip('/')).exists(), project)

    def test_group_rows_alias_to_the_canonical_entry(self):
        self.run_driver([unit([COMPLETE], rows=(12, 868))])
        idx = self.read_index()
        self.assertEqual(idx['by_mine']['grades:868'], {'a': MINE})

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
        self.assertTrue(idx['stats']['partial_update'])
        self.assertEqual(idx['stats']['built_mines'], 2)
        for key in (MINE, 'grades:17'):
            self.assertTrue((self.site / 'models' / idx['by_mine'][key]['p']
                             / 'card.json').exists())

    def test_a_scoped_run_over_a_schema_1_index_upgrades_it(self):
        # the previous deploy wrote the full record inline; the untouched row
        # is rewritten compact and its card.json appears beside its model
        first = self.run_driver([unit([COMPLETE])])
        model_id = first['units'][0]['builds'][0]['model_id']
        card = self.read_card(model_id)
        old_entry = {k: card[k] for k in ('key', 'label', 'site_kind', 'methods',
                                          'store_mine_ids', 'grade_rows', 'documents',
                                          'models', 'primary', 'lexicon', 'minerals',
                                          'extent')}
        os.remove(self.site / 'models' / model_id / 'card.json')
        with open(self.index, 'w') as fh:
            json.dump({'schema_version': 1, 'by_mine': {
                MINE: old_entry, 'grades:868': {'alias': MINE}}}, fh)
        other = unit([COMPLETE], key='grades:17', rows=(17,))
        self.run_driver([other], only=['grades:17'])
        idx = self.read_index()
        self.assertEqual(idx['schema_version'], 2)
        self.assertEqual(set(idx['by_mine'][MINE]), COMPACT_KEYS)
        self.assertEqual(idx['by_mine'][MINE]['p'], model_id)
        self.assertEqual(idx['by_mine']['grades:868'], {'a': MINE})
        self.assertEqual(self.read_card(model_id)['documents'][0]['title'],
                         'Test Report')

    def test_documents_without_buildable_text_still_index(self):
        bare = unit([])
        self.run_driver([bare])
        row = self.read_index()['by_mine'][MINE]
        self.assertEqual(set(row), COMPACT_KEYS)
        self.assertEqual(row['n'], 0)
        self.assertIsNone(row['p'])
        self.assertEqual(row['c'], [0, 0, 0])
        self.assertEqual(row['x'], 0)
        self.assertEqual(row['m'], ['Gold', 'Silver'])   # the grade columns still say

    def test_dry_run_builds_nothing_and_writes_no_index(self):
        self.run_driver([unit([COMPLETE])], dry_run=True)
        self.assertFalse(self.index.exists())
        self.assertFalse((self.site / 'models').exists())

    def test_only_accepts_an_alias_row(self):
        self.run_driver([unit([COMPLETE], rows=(12, 868))], only=['grades:868'])
        idx = self.read_index()
        self.assertEqual(idx['by_mine'][MINE]['n'], 1)

    def test_a_failed_build_keeps_the_previous_entry_and_its_model(self):
        first = self.run_driver([unit([COMPLETE])])
        model_id = first['units'][0]['builds'][0]['model_id']
        model_dir = self.site / 'models' / model_id
        self.assertTrue(model_dir.exists())
        # now every build errors (offline + no cached elevation)
        with mock.patch.object(resolve, 'elevation', lambda *a, **k: None):
            second = self.run_driver([unit([COMPLETE])])
        self.assertEqual(second['units'][0]['builds'][0]['state'], 'error')
        row = self.read_index()['by_mine'][MINE]
        self.assertTrue(row.get('cf'))
        self.assertEqual(row['p'], model_id)      # the old row stands
        self.assertEqual(row['n'], 1)
        self.assertTrue(model_dir.exists())       # and its files survive
        self.assertTrue((model_dir / 'card.json').exists())

    def test_a_clean_full_run_prunes_unreferenced_model_dirs_only(self):
        self.run_driver([unit([COMPLETE])])
        stale = self.site / 'models' / 'stale-mine-00000000'
        stale.mkdir()
        (stale / 'card.json').write_text('{}')
        self.run_driver([unit([COMPLETE])])
        self.assertFalse(stale.exists())
        kept = self.read_index()['by_mine'][MINE]['p']
        self.assertTrue((self.site / 'models' / kept / 'card.json').exists())


class WriteIndexTests(unittest.TestCase):
    """write_index on synthetic results: no build, no terrain, no corpus."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / 'site'
        self.site.mkdir()

    def write(self, results, **kw):
        return autop.write_index(results, str(self.site), **kw)

    def index(self):
        with open(self.site / 'data' / 'models' / 'index.json', encoding='utf-8') as fh:
            return json.load(fh)

    def card(self, mid):
        with open(self.site / 'models' / mid / 'card.json', encoding='utf-8') as fh:
            return json.load(fh)

    def test_every_dot_namespace_key_is_written_as_given(self):
        keys = ['grades:3', 'stategeo:IGS DD-1 IF0126', 'mrds:10012345',
                'usmin:987654', 'ardf:MD012']
        self.write([result(k, mid='m-%d-' % i) for i, k in enumerate(keys)],
                   generated='2026-09-02T00:00:00Z')
        idx = self.index()
        self.assertEqual(idx['schema_version'], 2)
        self.assertEqual(idx['generated'], '2026-09-02T00:00:00Z')
        self.assertEqual(set(idx['by_mine']), set(keys))
        for k in keys:
            self.assertEqual(set(idx['by_mine'][k]), COMPACT_KEYS, k)

    def test_the_row_is_cut_from_the_full_record(self):
        got = self.write([result('grades:12', n_models=2, rows=(12, 868))])
        row = self.index()['by_mine']['grades:12']
        self.assertEqual(row, {'l': 'Tonopah Divide mine',
                               'p': 'tonopah-divide-mine-c0125b00', 'n': 2,
                               'm': ['Gold', 'Silver'], 'x': 1040,
                               'w': 7 + 6, 'c': [7, 0, 0]})
        self.assertEqual(self.index()['by_mine']['grades:868'], {'a': 'grades:12'})
        self.assertEqual(got['model_ids'], {'tonopah-divide-mine-c0125b00',
                                            'tonopah-divide-mine-c0125b01'})
        self.assertEqual(got['cards'], 2)
        self.assertEqual(got['stats']['built_mines'], 1)
        self.assertEqual(got['stats']['built_models'], 2)

    def test_label_and_minerals_are_capped_in_the_row_not_the_card(self):
        label = 'A' * 80
        minerals = ['Gold', 'Silver', 'Lead', 'Zinc', 'Copper', 'Antimony',
                    'Tungsten', 'Mercury']
        self.write([result('grades:1', label=label, minerals=minerals)])
        row = self.index()['by_mine']['grades:1']
        self.assertEqual(len(row['l']), autop.LABEL_MAX)
        self.assertTrue(row['l'].endswith('…'))
        self.assertEqual(row['m'], minerals[:autop.MINERALS_MAX])
        card = self.card(row['p'])
        self.assertEqual(card['label'], label)
        self.assertEqual(card['minerals'], minerals)

    def test_one_card_per_model_carries_the_full_record(self):
        r = result('grades:12', n_models=2)
        r['models'][1]['composition'] = {'units': ['quartz vein']}
        self.write([r], generated='2026-09-02T00:00:00Z')
        for m in r['models']:
            card = self.card(m['model_id'])
            self.assertEqual(card['schema_version'], 2)
            self.assertEqual(card['model_id'], m['model_id'])
            self.assertEqual(card['key'], 'grades:12')
            self.assertEqual(card['primary'], r['models'][0]['model_id'])
            self.assertEqual(card['methods'], ['citation_quote'])
            doc = card['documents'][0]
            self.assertEqual((doc['title'], doc['source_url'], doc['publication_year'],
                              doc['cited_pages']),
                             (DOC['title'], DOC['source_url'], 1921, [2, 7]))
            self.assertEqual(len(card['models']), 2)
            for cm in card['models']:
                for field in ('confidence', 'omitted', 'summary', 'levels',
                              'level_depths_m', 'assay_commodities', 'assays', 'vein'):
                    self.assertIn(field, cm, field)
            self.assertEqual(card['models'][1]['composition'], {'units': ['quartz vein']})
            self.assertNotIn('composition', card['models'][0])
            self.assertEqual(card['lexicon']['kinds']['shaft']['count'], 16)
            self.assertEqual(card['extent']['deepest_level_m'], 30.5)

    def test_a_mine_without_a_model_has_a_row_and_no_card(self):
        self.write([result('mrds:5', n_models=0)])
        row = self.index()['by_mine']['mrds:5']
        self.assertEqual(row, {'l': 'Tonopah Divide mine', 'p': None, 'n': 0,
                               'm': ['Gold', 'Silver'], 'x': 0, 'w': 0, 'c': [0, 0, 0]})
        self.assertFalse((self.site / 'models').exists())

    def test_an_errored_mine_keeps_its_previous_schema_2_row(self):
        # the previous run published two models; this run's builds all failed
        self.write([result('grades:12', n_models=2)], generated='t1')
        prev = self.index()['by_mine']
        got = self.write([result('grades:12', n_models=0, errored=True)],
                         previous=prev, generated='t2')
        row = self.index()['by_mine']['grades:12']
        self.assertEqual(row['cf'], 't2')
        self.assertEqual({k: v for k, v in row.items() if k != 'cf'},
                         prev['grades:12'])
        # every model of the kept row is known to the prune, via its card
        self.assertEqual(got['model_ids'], {'tonopah-divide-mine-c0125b00',
                                            'tonopah-divide-mine-c0125b01'})
        self.assertEqual(got['stats']['built_models'], 2)

    def test_an_errored_mine_keeps_a_previous_schema_1_row_and_gains_its_card(self):
        full = result('grades:12', n_models=1)
        full.pop('errored')
        previous = {'grades:12': full, 'grades:868': {'alias': 'grades:12'}}
        got = self.write([result('grades:12', n_models=0, errored=True, rows=(12, 868))],
                         previous=previous, generated='t2')
        row = self.index()['by_mine']['grades:12']
        self.assertEqual(row['p'], full['models'][0]['model_id'])
        self.assertEqual(row['cf'], 't2')
        self.assertEqual(self.index()['by_mine']['grades:868'], {'a': 'grades:12'})
        self.assertEqual(got['model_ids'], {full['models'][0]['model_id']})
        self.assertEqual(self.card(row['p'])['documents'][0]['title'], DOC['title'])

    def test_an_errored_mine_with_nothing_published_before_is_not_carried(self):
        previous = {'grades:12': {'l': 'x', 'p': None, 'n': 0, 'm': [], 'x': 0,
                                  'w': 0, 'c': [0, 0, 0]}}
        self.write([result('grades:12', n_models=0, errored=True)], previous=previous)
        row = self.index()['by_mine']['grades:12']
        self.assertNotIn('cf', row)
        self.assertEqual(row['n'], 0)

    def test_a_successful_rebuild_replaces_the_previous_row(self):
        self.write([result('grades:12', n_models=1)], generated='t1')
        prev = self.index()['by_mine']
        self.write([result('grades:12', n_models=1, mid='rebuilt-')], previous=prev)
        row = self.index()['by_mine']['grades:12']
        self.assertEqual(row['p'], 'rebuilt-0')
        self.assertNotIn('cf', row)

    def test_merge_previous_keeps_untouched_rows_in_either_schema(self):
        full = result('grades:1', n_models=1, mid='old-full-')
        full.pop('errored')
        previous = {
            'grades:1': full, 'grades:2': {'alias': 'grades:1'},
            'mrds:9': {'l': 'Kept', 'p': 'kept-00000000', 'n': 1, 'm': [],
                       'x': 5, 'w': 1, 'c': [1, 0, 0]},
            'usmin:3': {'a': 'mrds:9'},
        }
        got = self.write([result('grades:12', mid='new-')], previous=previous,
                         merge_previous=True)
        by_mine = self.index()['by_mine']
        self.assertEqual(set(by_mine), {'grades:12', 'grades:1', 'grades:2',
                                        'mrds:9', 'usmin:3'})
        self.assertEqual(set(by_mine['grades:1']), COMPACT_KEYS)
        self.assertEqual(by_mine['grades:2'], {'a': 'grades:1'})
        self.assertEqual(by_mine['mrds:9'], previous['mrds:9'])
        self.assertEqual(by_mine['usmin:3'], {'a': 'mrds:9'})
        self.assertTrue((self.site / 'models' / 'old-full-0' / 'card.json').exists())
        self.assertEqual(got['model_ids'], {'new-0', 'old-full-0', 'kept-00000000'})
        self.assertEqual(got['stats']['built_mines'], 3)
        # without the merge the untouched rows are gone: a full run owns the index
        self.write([result('grades:12', mid='new-')], previous=previous)
        self.assertEqual(set(self.index()['by_mine']), {'grades:12'})

    def test_sixteen_rows_stay_under_four_kilobytes(self):
        # every field at its cap: a 60-char label, a 57-char model id, six
        # minerals, a long key — and the file is written without whitespace
        minerals = ['Gold', 'Silver', 'Lead', 'Zinc', 'Copper', 'Antimony']
        results = [result('stategeo:IGS DD-1 IF%04d' % i, label='M' * 70,
                          minerals=minerals,
                          mid='montgomery-shoshone-mine-bonanza-ore-above-200-f-')
                   for i in range(16)]
        for i, r in enumerate(results):
            r['models'][0]['model_id'] = ('montgomery-shoshone-mine-bonanza-ore-above-200-f-'
                                          '%08x' % i)                    # 57 chars
        self.write(results, stats={'mines': 16})
        path = self.site / 'data' / 'models' / 'index.json'
        raw = path.read_bytes()
        self.assertNotIn(b'\n', raw)
        by_mine = self.index()['by_mine']
        self.assertEqual(len(by_mine), 16)
        rows = json.dumps(by_mine, separators=(',', ':'), sort_keys=True,
                          ensure_ascii=False).encode('utf-8')
        self.assertLess(len(rows), 4096, len(rows))
        # ...which is what lets tens of thousands of mines fit one file
        self.assertLess(len(rows) / 16, 256)

    def test_stats_are_copied_in_and_counted_from_the_final_index(self):
        got = self.write([result('grades:1'), result('grades:2', n_models=0)],
                         stats={'mines': 2, 'documents': 25}, grades_generated='g1')
        idx = self.index()
        self.assertEqual(idx['stats'], {'mines': 2, 'documents': 25,
                                        'built_mines': 1, 'built_models': 1})
        self.assertEqual(idx['grades_generated'], 'g1')
        self.assertEqual(got['index_path'], str(self.site / 'data' / 'models' / 'index.json'))
        self.assertIn('card.json', idx['note'])

    def test_the_published_index_when_present_is_within_budget(self):
        path = ROOT / 'site' / 'data' / 'models' / 'index.json'
        if not path.exists():
            return
        idx = json.loads(path.read_text(encoding='utf-8'))
        if idx.get('schema_version') != 2:
            return          # a schema-1 deploy is legal until the next run
        rows = json.dumps(idx['by_mine'], separators=(',', ':'), sort_keys=True,
                          ensure_ascii=False).encode('utf-8')
        self.assertLess(len(rows) / max(1, len(idx['by_mine'])), 256)
        for key, row in idx['by_mine'].items():
            if 'a' in row:
                self.assertIn(row['a'], idx['by_mine'], key)
                continue
            self.assertTrue(COMPACT_KEYS <= set(row), key)
            if row['p']:
                self.assertTrue((ROOT / 'site' / 'models' / row['p'] / 'card.json').exists(), key)


if __name__ == '__main__':
    unittest.main()
