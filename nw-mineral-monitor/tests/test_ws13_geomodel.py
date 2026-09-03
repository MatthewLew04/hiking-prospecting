"""ws13_geomodel — the sharded corpus driver, proven offline.

No Postgres is reachable from this machine and no S3 either, so every stage
runs against ``tests/fixtures/ws13_geomodel/`` through :class:`FixtureCorpus`:
eight ws13_documents-shaped rows, one ws13_mine_id_map row, per-page text.
The resolver is the real ``geomodel_corpus.SiteIndex`` fed the committed
grades bundle only (an empty sites directory, no ARDF), so every mine the
fixture names is found by the grades tier and nothing depends on the size or
the drift of the state-survey files.  Terrain is stubbed the way
tests/test_geomodel_autopopulate.py stubs it — ``resolve.elevation`` returns
a constant — so a build exercises the driver, not the tile network.

What is pinned: the funnel counts and every drop reason; the ledger status
and reason of every (document, mine); the exit-code contract (0 / 10 / 11 /
2 / 3 / 12); the content-hash skip on rerun; the rights refusal and the
rights block a published manifest carries; the heartbeat's shape and key;
that the shard partition covers every document exactly once for any shard
count and that the Python and SQL expressions are the same arithmetic; the
migration file's idempotence and --check; the answerer contract; and that
the fleet shell sweeps the geomodel mode on the same exit codes as the
confidence mode.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))
sys.path.insert(0, str(ROOT / 'services'))

# psycopg and boto3 are the fleet's dependencies, not the test host's; the
# driver imports them lazily and ws13_migrate (used by --migrate) at the top.
for _name in ('psycopg', 'boto3'):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            _stub = types.ModuleType(_name)
            _stub.connect = mock.MagicMock()
            _stub.client = mock.MagicMock()
            sys.modules[_name] = _stub

import ws13_geomodel as wg                       # noqa: E402
import geomodel_autopopulate as autop            # noqa: E402
import geomodel_corpus as corpus                 # noqa: E402
from geomodel import publish, resolve            # noqa: E402

FIXTURE = ROOT / 'tests' / 'fixtures' / 'ws13_geomodel'
LABELS = ('clean', 'district', 'novocab', 'ambiguous', 'norights', 'unchanged',
          'omit', 'unknown')
SHA = {label: hashlib.sha256(('ws13-geomodel-fixture:' + label).encode()).hexdigest()
       for label in LABELS}
MINE = {'clean': 'grades:12', 'unchanged': 'grades:8', 'omit': 'grades:27',
        'unknown': 'grades:22'}
DISTRICT_MINES = {'grades:20', 'grades:31', 'grades:33'}     # Nemo, Flaxie, Bluster


class Harness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / 'state'
        self.site = base / 'site'
        self.terrain = base / 'terrain'
        self.terrain.mkdir()
        empty = base / 'nosites'
        empty.mkdir()
        self.index = corpus.SiteIndex(root=str(ROOT), sites_dir=str(empty), ardf_paths=())
        patcher = mock.patch.object(resolve, 'elevation', lambda *a, **k: 1900.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.logs = []

    def argv(self, *extra):
        return ['--fixture', str(FIXTURE), '--state-dir', str(self.state),
                '--site-dir', str(self.site), '--offline',
                '--terrain-cache', str(self.terrain)] + list(extra)

    def run_driver(self, *extra, resolver='index'):
        report = []
        rc = wg.run(self.argv(*extra),
                    resolver=self.index if resolver == 'index' else resolver,
                    log=self.logs.append, report=report)
        return rc, (report[0] if report else None)

    def ledger(self):
        path = self.state / 'ledger.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def latest(self):
        rows = {}
        for row in self.ledger():
            rows[(row['sha256'], row['mine_key'])] = row
        return rows

    def rows_for(self, label):
        return {k[1]: r for k, r in self.latest().items() if k[0] == SHA[label]}

    def models(self):
        root = self.site / 'models'
        return sorted(os.listdir(root)) if root.exists() else []

    def manifest(self, model_id):
        with open(self.site / 'models' / model_id / 'manifest.json') as fh:
            return json.load(fh)

    def status(self, shard=0, shards=1):
        with open(self.state / wg.status_key(shard, shards)) as fh:
            return json.load(fh)


# ------------------------------------------------------------------ funnel
class FunnelTests(Harness):

    def test_plan_prints_the_funnel_and_builds_nothing(self):
        rc, summary = self.run_driver('--plan')
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)
        funnel = summary['funnel']
        self.assertEqual([funnel.counts[s] for s in funnel.STEPS], [8, 8, 7, 6, 5, 4, 4])
        self.assertEqual(funnel.drops['with_workings_vocabulary'],
                         {'no workings vocabulary': 1})
        self.assertEqual(list(funnel.drops['rights_permit_a_derived_model']),
                         ['rights: no rights_basis on a licensed-copies document'])
        (reason,) = funnel.drops['resolvable_to_a_located_site']
        self.assertTrue(reason.startswith('ambiguous'), reason)
        (reason,) = funnel.drops['parseable_elements']
        self.assertTrue(reason.startswith('no-elements'), reason)
        self.assertEqual(self.models(), [])
        self.assertEqual(self.ledger(), [])          # --plan is read-only
        self.assertTrue(any('worst-shard h' in line for line in self.logs))

    def test_dry_run_records_planned_rows_and_builds_nothing(self):
        rc, _ = self.run_driver('--dry-run')
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)
        planned = {k for k, r in self.latest().items() if r['status'] == 'planned'}
        self.assertEqual(planned, {(SHA['clean'], 'grades:12'), (SHA['unchanged'], 'grades:8'),
                                   (SHA['omit'], 'grades:27')}
                         | {(SHA['district'], m) for m in DISTRICT_MINES})
        self.assertEqual(self.models(), [])


# --------------------------------------------------------------- the sweep
class SweepTests(Harness):

    def test_a_full_sweep_ledgers_every_outcome(self):
        rc, summary = self.run_driver()
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)
        self.assertEqual(summary['stats']['models_published'], 6)
        self.assertEqual(len(self.models()), 6)
        self.assertEqual(self.rows_for('clean')['grades:12']['status'], 'published')
        self.assertEqual(set(self.rows_for('district')), DISTRICT_MINES)
        self.assertTrue(all(r['status'] == 'published'
                            for r in self.rows_for('district').values()))
        self.assertEqual(self.rows_for('novocab'), {})     # dropped before the ledger
        amb = self.rows_for('ambiguous')[wg.NO_MINE]
        self.assertEqual(amb['status'], 'parked')
        self.assertTrue(amb['reason'].startswith('ambiguous'), amb['reason'])
        self.assertIn("'Combination mine'", amb['reason'])
        rights = self.rows_for('norights')[wg.NO_MINE]
        self.assertEqual((rights['status'], rights['reason']),
                         ('parked', 'rights: no rights_basis on a licensed-copies document'))
        self.assertEqual(self.rows_for('unchanged')['grades:8']['status'], 'published')
        unknown = self.rows_for('unknown')['grades:22']
        self.assertEqual(unknown['status'], 'skipped')
        self.assertTrue(unknown['reason'].startswith('no-elements'), unknown['reason'])
        self.assertEqual((unknown['counts']['elements'], unknown['counts']['questions'],
                          unknown['counts']['mentions']), (0, 2, 2))
        for row in self.latest().values():
            self.assertIn(row['status'], wg.STATUSES)
            self.assertEqual(row['attempts'], 1)
            self.assertTrue(row['run_id'].startswith('r-'))

    def test_the_verified_id_map_row_resolves_through_the_identity_tier(self):
        self.run_driver()
        row = self.rows_for('unchanged')['grades:8']
        self.assertEqual(row['counts']['method'], 'identity_row')

    def test_exit_codes_follow_the_contract(self):
        rc, summary = self.run_driver('--limit', '0')
        self.assertEqual(rc, wg.EXIT_NO_PROGRESS)
        self.assertEqual((summary['work_set'], summary['remaining']), (7, 7))
        rc, summary = self.run_driver('--limit', '3')
        self.assertEqual(rc, wg.EXIT_WORK_REMAINS)
        self.assertEqual((summary['finished_now'], summary['remaining']), (3, 4))
        rc, summary = self.run_driver()
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)
        self.assertEqual((summary['done_before'], summary['remaining']), (3, 0))
        rc, summary = self.run_driver()
        self.assertEqual((rc, summary['processed']), (wg.EXIT_SHARD_DONE, 0))

    def test_a_rerun_skips_unchanged_published_models_on_the_content_hash(self):
        rc, first = self.run_driver()
        before = {m: os.stat(self.site / 'models' / m / 'manifest.json').st_mtime_ns
                  for m in self.models()}
        first_rows = self.latest()
        rc, second = self.run_driver('--rebuild')
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)
        self.assertEqual(second['stats']['models_published'], 0)
        self.assertEqual(second['stats']['models_unchanged'], 6)
        for key, row in self.latest().items():
            if row['status'] != 'published':
                continue
            self.assertEqual(row['reason'], 'unchanged: skipped on rerun')
            self.assertEqual(row['model_id'], first_rows[key]['model_id'])
            self.assertEqual(row['content_hash'], first_rows[key]['content_hash'])
            self.assertEqual(row['attempts'], 2)
        after = {m: os.stat(self.site / 'models' / m / 'manifest.json').st_mtime_ns
                 for m in self.models()}
        self.assertEqual(before, after)

    def test_the_content_hash_carries_every_version_and_the_policy(self):
        text = 'A shaft was sunk 300 feet.'
        h1 = wg.content_hash(text, context=False)
        self.assertNotEqual(h1, wg.content_hash(text, context=True))
        self.assertNotEqual(h1, wg.content_hash(text + ' ', context=False))
        with mock.patch.object(wg, 'DRIVER_VERSION', 'nwmm-ws13-geomodel/2'):
            self.assertNotEqual(h1, wg.content_hash(text, context=False))

    def test_a_document_is_processed_by_one_shard_only(self):
        seen = {}
        for shard in range(3):
            rc, summary = self.run_driver('--shard', str(shard), '--shards', '3')
            self.assertEqual(rc, wg.EXIT_SHARD_DONE)
            for row in self.ledger():
                seen.setdefault(row['sha256'], set()).add(wg.shard_of(row['sha256'], 3))
        self.assertEqual(len(seen), 7)
        self.assertTrue(all(len(s) == 1 for s in seen.values()))
        self.assertEqual(len(self.models()), 6)

    def test_doc_runs_one_document_end_to_end(self):
        rc, summary = self.run_driver('--doc', SHA['clean'])
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)
        self.assertEqual(set(self.latest()), {(SHA['clean'], 'grades:12')})
        self.assertEqual(self.models(), ['tonopah-divide-mine-' +
                                         self.rows_for('clean')['grades:12']['model_id'][-8:]])

    def test_verify_complete_answers_from_the_ledger(self):
        rc, _ = self.run_driver('--verify-complete', '--shards', '4')
        self.assertEqual(rc, wg.EXIT_INCOMPLETE)
        self.run_driver()
        rc, _ = self.run_driver('--verify-complete', '--shards', '4')
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)

    def test_a_missing_resolver_parks_everything(self):
        with mock.patch.object(corpus, 'SiteIndex', None):
            rc, summary = self.run_driver(resolver=None)
        self.assertEqual(rc, wg.EXIT_SHARD_DONE)
        parked = [r for r in self.latest().values() if r['status'] == 'parked']
        self.assertEqual(len(parked), 7)      # every document with workings vocabulary
        unavailable = [r for r in parked if r['reason'] == 'resolver unavailable']
        self.assertEqual(len(unavailable), 6)  # all but the one refused on rights first
        self.assertEqual(self.models(), [])


# --------------------------------------------------------------- honesty
class HonestyTests(Harness):

    def test_rights_refusal_never_reaches_the_target(self):
        self.run_driver()
        self.assertFalse([m for m in self.models() if 'silver-peak' in m])
        row = self.rows_for('norights')[wg.NO_MINE]
        self.assertEqual(row['status'], 'parked')
        self.assertIsNone(row['model_id'])

    def test_a_published_manifest_carries_the_documents_rights(self):
        self.run_driver()
        arizona = self.manifest(self.rows_for('unchanged')['grades:8']['model_id'])
        block = arizona['source_document']
        self.assertEqual(block['sha256'], SHA['unchanged'])
        self.assertEqual(block['admission_class'], 'research-copies')
        self.assertEqual(block['rights_basis'],
                         'Nevada Bureau of Mines and Geology mining district file')
        self.assertIs(block['public_domain'], False)
        self.assertEqual((block['attribution_required'], block['non_commercial'],
                          block['share_alike']), (True, True, False))
        self.assertIn(block['rights_basis'], block['rights_terms'])
        self.assertEqual(block['text_source'], 'sidecar')
        self.assertEqual(block['pages'], [1])
        clean = self.manifest(self.rows_for('clean')['grades:12']['model_id'])['source_document']
        self.assertEqual(clean['source_url'], 'https://pubs.usgs.gov/bul/0715k/report.pdf')
        self.assertEqual((clean['public_domain'], clean['attribution_required']), (True, False))

    def test_the_omit_policy_answers_null_and_the_model_assumes_nothing(self):
        self.run_driver()
        row = self.rows_for('omit')['grades:27']
        self.assertEqual(row['status'], 'published')
        self.assertEqual((row['counts']['answers'], row['counts']['omitted']), (1, 1))
        self.assertEqual(row['counts']['confidence']['assumed'], 0)
        man = self.manifest(row['model_id'])
        self.assertEqual(len(man['answers']), 1)
        for answer in man['answers']:
            self.assertIsNone(answer['value'])
            self.assertEqual(answer['because'], autop.OMIT_BECAUSE)
        # apply_answers drops an omitted element from the spec, so the drift is
        # absent from `elements` and present only under `answers`: the list of
        # numbers that did NOT come out of the document
        self.assertFalse([e for e in man['elements'] if e['kind'] == 'drift'])
        self.assertEqual({e['kind'] for e in man['elements']}, {'shaft'})
        self.assertEqual(man['confidence']['assumed'], 0)

    def test_a_district_report_gives_one_model_per_mine_with_its_own_sentences(self):
        self.run_driver()
        rows = self.rows_for('district')
        ids = {r['model_id'] for r in rows.values()}
        self.assertEqual(len(ids), 3)
        nemo = self.manifest(rows['grades:20']['model_id'])
        quotes = ' '.join(e['quote'] for e in nemo['elements'])
        self.assertIn('Nemo', quotes)
        self.assertNotIn('Flaxie', quotes)
        self.assertNotIn('Bluster', quotes)

    def test_unknown_phrasing_is_questions_not_elements(self):
        self.run_driver()
        row = self.rows_for('unknown')['grades:22']
        self.assertEqual(row['status'], 'skipped')
        self.assertFalse([m for m in self.models() if 'wall-mine' in m])

    def test_the_index_and_cards_are_written_through_write_index(self):
        self.run_driver()
        with open(self.site / 'data' / 'models' / 'index.json') as fh:
            index = json.load(fh)
        self.assertEqual(index['schema_version'], autop.INDEX_SCHEMA)
        self.assertEqual(index['stats']['built_models'], 6)
        self.assertEqual(index['by_mine']['grades:12']['n'], 1)
        self.assertEqual(index['by_mine']['grades:22']['n'], 0)     # documents, no model
        self.assertEqual(index['by_mine']['grades:868'], {'a': 'grades:12'})
        card = self.site / 'models' / index['by_mine']['grades:12']['p'] / 'card.json'
        self.assertTrue(card.exists())
        with open(card) as fh:
            self.assertEqual(json.load(fh)['documents'][0]['doc_id'], SHA['clean'])

    def test_sharded_runs_write_one_index_per_shard(self):
        shard = wg.shard_of(SHA['clean'], 2)
        self.run_driver('--shard', str(shard), '--shards', '2')
        folder = self.site / 'data' / 'models'
        self.assertTrue((folder / ('index-%04d-of-0002.json' % shard)).exists())
        self.assertFalse((folder / 'index.json').exists())
        with open(folder / ('index-%04d-of-0002.json' % shard)) as fh:
            self.assertIn('grades:12', json.load(fh)['by_mine'])


# -------------------------------------------------------------- heartbeat
class HeartbeatTests(Harness):

    def test_the_heartbeat_shape_and_key(self):
        self.run_driver('--limit', '3')
        beat = self.status()
        for key in ('generated', 'phase', 'run_id', 'shard', 'shards', 'documents_work_set',
                    'documents_done', 'documents_remaining', 'models_published',
                    'models_unchanged', 'errors', 'parked_by_reason', 'skipped_by_reason',
                    'documents_per_second', 'models_per_second', 'seconds_per_document',
                    'eta_hours', 'funnel', 'elapsed_seconds'):
            self.assertIn(key, beat)
        self.assertEqual(beat['phase'], 'finished')
        self.assertEqual((beat['documents_done'], beat['documents_remaining']), (3, 4))
        self.assertEqual(beat['models_published'], 4)
        self.assertEqual(beat['parked_by_reason'],
                         {"ambiguous: tier 4 name_state yields 2 physical mines for "
                          "'Combination mine'": 1})
        self.assertGreater(beat['documents_per_second'], 0)
        self.assertIsNotNone(beat['eta_hours'])

    def test_sharded_heartbeats_take_the_per_shard_key(self):
        self.assertEqual(wg.status_key(0, 1), 'ws13/geomodel/status.json')
        self.assertEqual(wg.status_key(7, 640), 'ws13/geomodel/status-0007-of-0640.json')
        self.run_driver('--shard', '0', '--shards', '2')
        self.assertTrue((self.state / 'ws13' / 'geomodel' / 'status-0000-of-0002.json').exists())


# --------------------------------------------------------------- sharding
class ShardTests(unittest.TestCase):

    def test_python_and_sql_are_the_same_arithmetic(self):
        self.assertEqual(wg.SHARD_EXPR, "('x' || substr(d.sha256, 1, 8))::bit(32)::bigint")
        text = (ROOT / 'pipelines' / 'ws13_confidence_pass.py').read_text()
        theirs = re.search(r'^SHARD_EXPR = "(.*)"$', text, re.M).group(1)
        self.assertEqual(theirs.replace('p.sha256', 'd.sha256'), wg.SHARD_EXPR)
        for label, sha in SHA.items():
            # bit(32) over the first 8 hex digits is the unsigned 32-bit int
            self.assertEqual(wg.shard_of(sha, 64), int(sha[:8], 16) % 64)
            self.assertEqual(wg.shard_of(sha, 1), 0)

    def test_the_partition_covers_every_document_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            corp = wg.FixtureCorpus(str(FIXTURE), tmp)
            for shards in (1, 2, 3, 5, 8, 64):
                seen = []
                for shard in range(shards):
                    seen += [d['sha256'] for d in corp.documents(shard, shards)]
                self.assertEqual(sorted(seen), sorted(SHA.values()), shards)
            for shards in (2, 3, 5):
                for shard in range(shards):
                    for sha, _ in corp.candidates(shard, shards).items():
                        self.assertEqual(wg.shard_of(sha, shards), shard)

    def test_bad_shard_arithmetic_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['--fixture', str(FIXTURE), '--state-dir', tmp]
            self.assertEqual(wg.run(args + ['--shard', '3', '--shards', '2'], log=lambda m: None),
                             wg.EXIT_BAD_SHARD)
            self.assertEqual(wg.run(args + ['--shards', '0'], log=lambda m: None),
                             wg.EXIT_BAD_SHARD)
            self.assertEqual(wg.run(args + ['--rate', '0'], log=lambda m: None),
                             wg.EXIT_BAD_SHARD)

    def test_capacity_reports_the_worst_shard(self):
        report = wg.capacity(list(SHA.values()), rate=0.25, shard_counts=(2, 8))
        self.assertEqual(report['documents'], 8)
        row = report['rows'][0]
        self.assertGreaterEqual(row['worst_documents'], row['mean_documents'])
        self.assertAlmostEqual(row['worst_hours'], row['worst_documents'] / 0.25 / 3600.0)


# ------------------------------------------------------------ environment
class EnvironmentTests(unittest.TestCase):

    def test_no_dsn_and_no_bucket_is_exit_3(self):
        with mock.patch.dict(os.environ, {'WS13_DB_DSN': '', 'WS13_BUCKET': ''}):
            rc = wg.run(['--dsn', '', '--bucket', ''], log=lambda m: None)
        self.assertEqual(rc, wg.EXIT_ENVIRONMENT)

    def test_offline_without_a_terrain_cache_is_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = wg.run(['--fixture', str(FIXTURE), '--state-dir', tmp, '--offline',
                         '--terrain-cache', os.path.join(tmp, 'absent')], log=lambda m: None)
        self.assertEqual(rc, wg.EXIT_ENVIRONMENT)

    def test_s3_publish_without_a_models_bucket_is_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'terrain'))
            with mock.patch.dict(os.environ, {'NWMM_MODELS_BUCKET': ''}):
                rc = wg.run(['--fixture', str(FIXTURE), '--state-dir', tmp, '--offline',
                             '--terrain-cache', os.path.join(tmp, 'terrain'),
                             '--publish', 's3', '--models-bucket', ''], log=lambda m: None)
        self.assertEqual(rc, wg.EXIT_ENVIRONMENT)

    def test_the_exit_codes_leave_1_to_the_interpreter(self):
        codes = {wg.EXIT_SHARD_DONE, wg.EXIT_BAD_SHARD, wg.EXIT_ENVIRONMENT,
                 wg.EXIT_WORK_REMAINS, wg.EXIT_NO_PROGRESS, wg.EXIT_INCOMPLETE}
        self.assertEqual(codes, {0, 2, 3, 10, 11, 12})


# ------------------------------------------------------------------- text
class TextTests(unittest.TestCase):

    def test_sidecar_key_is_the_workers_upload_key(self):
        sha = SHA['clean']
        self.assertEqual(wg.sidecar_key(sha), 'ws13/searchable/%s/%s/sidecar.txt' % (sha[:2], sha))
        worker = (ROOT / 'pipelines' / 'ws13_worker.py').read_text()
        self.assertIn("ws13/searchable/{sha[:2]}/{sha}/sidecar.txt", worker)

    def test_sidecar_pages_split_on_form_feed(self):
        self.assertEqual(wg.pages_from_sidecar('one\x00\ftwo\f'), ['one', 'two', ''])

    def test_chunks_are_de_overlapped_on_start_char(self):
        rows = [(1, 0, 0, 6, 'abcdef'), (1, 1, 4, 10, 'efghij'),
                (3, 0, 0, 3, 'xyz')]
        self.assertEqual(wg.pages_from_chunks(rows), ['abcdefghij', '', 'xyz'])
        self.assertEqual(wg.pages_from_chunks(rows, page_count=4), ['abcdefghij', '', 'xyz', ''])

    def test_the_fixture_corpus_falls_back_to_chunks_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = os.path.join(tmp, 'fixture')
            os.makedirs(os.path.join(fixture, 'chunks'))
            sha = 'f' * 64
            with open(os.path.join(fixture, 'documents.jsonl'), 'w') as fh:
                fh.write(json.dumps({'sha256': sha, 'pages': 2, 'mine_names': ['X mine'],
                                     'state': 'NV', 'admission_class': 'originals',
                                     'public_domain': True}) + '\n')
            with open(os.path.join(fixture, 'chunks', sha + '.jsonl'), 'w') as fh:
                fh.write(json.dumps({'page': 2, 'ordinal': 1, 'start_char': 5, 'end_char': 10,
                                     'text': 'fghij'}) + '\n')
                fh.write(json.dumps({'page': 2, 'ordinal': 0, 'start_char': 0, 'end_char': 7,
                                     'text': 'abcdefg'}) + '\n')
            corp = wg.FixtureCorpus(fixture, os.path.join(tmp, 'state'))
            self.assertEqual(corp.page_texts(sha), (['', 'abcdefghij'], 'chunks'))
            with self.assertRaises(wg.CorpusUnavailable):
                corp.page_texts('0' * 64)

    def test_the_fixture_prefilter_counts_workings_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            corp = wg.FixtureCorpus(str(FIXTURE), tmp)
            hits = corp.candidates()
            self.assertNotIn(SHA['novocab'], hits)
            self.assertGreater(hits[SHA['clean']], 0)
            self.assertEqual(set(hits), set(SHA.values()) - {SHA['novocab']})


# -------------------------------------------------------------------- SQL
class SqlTests(unittest.TestCase):

    def test_every_statement_names_its_columns(self):
        for name, sql in wg.SQL.items():
            self.assertNotIn('SELECT *', sql.upper(), name)
            self.assertNotIn('.*', sql, name)
        for column in wg.DOCUMENT_COLUMNS:
            self.assertIn('d.%s' % column, wg.SQL['documents'])
        for column in wg.LEDGER_COLUMNS:
            self.assertIn('r.%s' % column, wg.SQL['ledger_load'])
        self.assertIn('c.page, c.ordinal, c.start_char, c.end_char, c.text', wg.SQL['chunks'])

    def test_documents_join_the_id_map_on_every_spelling(self):
        self.assertIn('LEFT JOIN ws13_mine_id_map m ON m.ws13_mine_id_all && d.mine_ids',
                      wg.SQL['documents'])
        for column in wg.MAP_COLUMNS:
            self.assertIn("'%s', m.%s" % (column, column), wg.SQL['documents'])

    def test_the_prefilter_uses_the_english_tsvector_and_the_whole_vocabulary(self):
        self.assertIn("c.tsv @@ to_tsquery('english', %s)", wg.SQL['candidates'])
        self.assertEqual(wg.WORKINGS_TSQUERY.split(' | '), list(wg.WORKINGS_VOCABULARY))
        self.assertEqual(len(wg.WORKINGS_VOCABULARY), 13)
        worker = (ROOT / 'pipelines' / 'ws13_worker.py').read_text()
        self.assertIn("to_tsvector('english', %s)", worker)

    def test_the_ledger_upsert_is_keyed_on_document_and_mine(self):
        self.assertIn('ON CONFLICT (sha256, mine_key) DO UPDATE', wg.SQL['ledger_put'])
        conn = FakeConn()
        ledger = wg.PostgresLedger(conn)
        row = wg.ledger_row('a' * 64, 'grades:1', 'r-1', 'published', 'published', 'm-1', 'h',
                            {'elements': 2}, ['w'], 3)
        ledger.put(row)
        sql, params = conn.statements[-1]
        self.assertIn('INSERT INTO ws13_geomodel_runs', sql)
        self.assertEqual(params[:4], ('a' * 64, 'grades:1', 'r-1', 'published'))
        self.assertEqual(json.loads(params[7]), {'elements': 2})
        self.assertEqual(params[9], 3)

    def test_the_postgres_corpus_shards_in_sql(self):
        conn = FakeConn(rows=[])
        corp = wg.PostgresCorpus('postgresql://x', 'bucket', psycopg_module=FakePsycopg(conn),
                                 s3_client=mock.MagicMock())
        list(corp.documents(3, 8))
        sql, params = conn.statements[-1]
        self.assertIn('mod(%s, %%s) = %%s' % wg.SHARD_EXPR, sql)
        self.assertEqual(params[1:3], (8, 3))
        corp.candidates(3, 8)
        sql, params = conn.statements[-1]
        self.assertEqual(params, (wg.WORKINGS_TSQUERY, 8, 3))
        self.assertEqual(corp.ledger.load(3, 8), {})
        sql, params = conn.statements[-1]
        self.assertIn(wg.shard_expr('r'), sql)

    def test_the_postgres_corpus_falls_back_to_chunks_when_the_sidecar_is_missing(self):
        conn = FakeConn(rows=[(1, 0, 0, 5, 'hello'), (1, 1, 3, 11, 'lo world')])
        s3 = mock.MagicMock()

        class NoSuchKey(Exception):
            response = {'Error': {'Code': 'NoSuchKey'}}
        s3.get_object.side_effect = NoSuchKey()
        corp = wg.PostgresCorpus('postgresql://x', 'bucket', psycopg_module=FakePsycopg(conn),
                                 s3_client=s3)
        self.assertEqual(corp.page_texts('a' * 64), (['hello world'], 'chunks'))
        s3.get_object.side_effect = None
        s3.get_object.return_value = {'Body': mock.MagicMock(read=lambda: b'p1\fp2')}
        self.assertEqual(corp.page_texts('a' * 64), (['p1', 'p2'], 'sidecar'))


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = -1

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Records every statement; answers with canned rows."""

    def __init__(self, rows=None, answers=None):
        self.statements = []
        self.rows = rows or []
        self.answers = answers or {}
        self.committed = self.rolled_back = 0
        self.transactions = 0

    def execute(self, sql, params=()):
        self.statements.append((' '.join(sql.split()), tuple(params)))
        for needle, rows in self.answers.items():
            if needle in sql:
                return FakeCursor(rows)
        return FakeCursor(self.rows)

    def transaction(self):
        conn = self

        class Tx:
            def __enter__(self_inner):
                conn.transactions += 1
                return conn

            def __exit__(self_inner, *exc):
                return False
        return Tx()

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


class FakePsycopg:
    def __init__(self, conn):
        self.conn = conn

    def connect(self, dsn, autocommit=False):
        return self.conn


# ---------------------------------------------------------------- rights
class RightsTests(unittest.TestCase):

    @staticmethod
    def table(path):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            targets = getattr(node, 'targets', None) or (
                [node.target] if hasattr(node, 'target') else [])
            for t in targets:
                if isinstance(t, ast.Name) and t.id == 'RIGHTS_BY_CLASS':
                    return ast.literal_eval(node.value)
        raise AssertionError('no RIGHTS_BY_CLASS in %s' % path)

    def test_the_rights_table_is_the_lambdas_table(self):
        self.assertEqual(wg.RIGHTS_BY_CLASS, self.table(ROOT / 'infra' / 'ws13_query_lambda.py'))
        self.assertEqual(wg.RIGHTS_BY_CLASS, self.table(ROOT / 'infra' / 'docs_lambda.py'))

    def test_rights_for_refuses_what_it_cannot_state(self):
        ok = wg.rights_for({'admission_class': 'originals', 'public_domain': True})
        self.assertEqual(ok['status'], 'ok')
        self.assertEqual((ok['rights']['attribution_required'], ok['rights']['non_commercial'],
                          ok['rights']['share_alike']), (False, False, False))
        licensed = wg.rights_for({'admission_class': 'licensed-copies', 'public_domain': False,
                                  'rights_basis': 'AZGS ADMMR'})
        self.assertEqual(licensed['status'], 'ok')
        self.assertEqual((licensed['rights']['attribution_required'],
                          licensed['rights']['non_commercial'],
                          licensed['rights']['share_alike']), (True, True, True))
        self.assertIn('AZGS ADMMR', licensed['rights']['rights_terms'])
        for doc in ({'admission_class': 'licensed-copies', 'public_domain': False},
                    {'admission_class': 'research-copies', 'public_domain': False,
                     'rights_basis': '  '},
                    {'admission_class': 'originals', 'public_domain': None},
                    {'admission_class': 'originals', 'public_domain': False},
                    {'admission_class': 'mystery', 'public_domain': True}):
            got = wg.rights_for(doc)
            self.assertEqual(got['status'], 'refused', doc)
            self.assertTrue(got['reason'].startswith('rights:'), got['reason'])


# -------------------------------------------------------------- answerer
class AnswererTests(Harness):

    PARTIAL = ('An adit was driven N45E for 900 feet. A shaft was sunk 300 feet on the '
               'vein. On the 300 level a drift was extended 450 feet.')

    def ctx(self, name):
        site = self.site / name
        site.mkdir(parents=True)
        return autop.make_context(str(self.state / name), publish.LocalTarget(str(site)),
                                  None, 13, True, lambda *a: None)

    def test_the_null_answerer_is_build_one(self):
        theirs = autop.build_one('grades:12', 'grades', {}, self.PARTIAL, self.ctx('a'),
                                 context=False)
        ours = wg.build_stage('grades:12', 'grades', {}, self.PARTIAL, self.ctx('b'),
                              context=False, answerer=wg.NullAnswerer())
        self.assertEqual((theirs['state'], ours['state']), ('done', 'done'))
        self.assertEqual(theirs['model_id'], ours['model_id'])
        self.assertEqual(theirs['answers'], ours['answers'])
        self.assertEqual(theirs['omitted_elements'], ours['omitted_elements'])
        self.assertEqual(theirs['confidence'], ours['confidence'])

    def test_an_answer_without_a_verbatim_quote_is_an_omit(self):
        class Guessing(wg.Answerer):
            name = 'guessing'

            def answer(self, question, text, spec):
                return {'value': 45.0, 'because': 'the vein trends northeast',
                        'quote': 'this sentence is not in the text'}
        warnings = []
        got = wg.answers_for([{'id': 'g1', 'element': 'e3'}], self.PARTIAL, {}, Guessing(),
                             warnings)
        self.assertEqual(got, [{'id': 'g1', 'value': None, 'because': autop.OMIT_BECAUSE}])
        self.assertTrue(warnings and 'discarded' in warnings[0])

    def test_an_answer_with_a_verbatim_quote_is_kept_and_attributed(self):
        class Quoting(wg.Answerer):
            name = 'quoting'

            def answer(self, question, text, spec):
                return {'value': 45.0, 'because': 'same vein as the adit',
                        'quote': 'An adit was driven N45E'}
        warnings = []
        got = wg.answers_for([{'id': 'g1', 'element': 'e3'}], self.PARTIAL, {}, Quoting(),
                             warnings)
        self.assertEqual(got[0]['value'], 45.0)
        self.assertIn('quoting:', got[0]['because'])
        self.assertIn('An adit was driven N45E', got[0]['because'])
        self.assertEqual(warnings, [])
        ours = wg.build_stage('grades:12', 'grades', {}, self.PARTIAL, self.ctx('c'),
                              context=False, answerer=Quoting())
        self.assertEqual(ours['state'], 'done')
        self.assertEqual(ours['confidence']['assumed'], 1)     # answered, and dotted
        self.assertEqual(ours['omitted_elements'], [])


# ---------------------------------------------------------------- ledger
class LedgerTests(unittest.TestCase):

    def test_jsonl_ledger_replays_the_latest_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ledger.jsonl')
            ledger = wg.JsonlLedger(path)
            ledger.put(wg.ledger_row(SHA['clean'], 'grades:12', 'r-1', 'error', 'boom'))
            ledger.put(wg.ledger_row(SHA['clean'], 'grades:12', 'r-2', 'published', 'ok',
                                     'm', 'h', attempts=2))
            again = wg.JsonlLedger(path)
            rows = again.load(0, 1)
            self.assertEqual(rows[(SHA['clean'], 'grades:12')]['status'], 'published')
            self.assertEqual(again.load(1 - wg.shard_of(SHA['clean'], 2), 2), {})

    def test_ledger_rows_refuse_a_seventh_status(self):
        with self.assertRaises(ValueError):
            wg.ledger_row('a' * 64, '-', 'r', 'done')

    def test_doc_terminal_reads_attempts(self):
        sha = 'a' * 64
        rows = {(sha, 'grades:1'): wg.ledger_row(sha, 'grades:1', 'r', 'error', 'x', attempts=1)}
        self.assertFalse(wg.doc_terminal(rows, sha))
        rows[(sha, 'grades:1')]['attempts'] = wg.MAX_ATTEMPTS
        self.assertTrue(wg.doc_terminal(rows, sha))
        rows[(sha, 'grades:2')] = wg.ledger_row(sha, 'grades:2', 'r', 'built', 'publish failed')
        self.assertFalse(wg.doc_terminal(rows, sha))
        self.assertFalse(wg.doc_terminal({}, sha))


# ------------------------------------------------------------- migrations
class MigrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import ws13_migrate
        cls.sql_text = (ROOT / 'pipelines' / 'ws13_geomodel_migrations.sql').read_text()
        cls.statements = ws13_migrate.split_statements(cls.sql_text)

    def test_every_statement_is_idempotent(self):
        self.assertEqual(len(self.statements), 19)
        for st in self.statements:
            label = st['label']
            if label.startswith('CREATE TABLE') or label.startswith('CREATE INDEX'):
                self.assertIn('IF NOT EXISTS', label)
            if 'ADD COLUMN' in label:
                self.assertIn('ADD COLUMN IF NOT EXISTS', label)
            if 'DROP CONSTRAINT' in label:
                self.assertIn('DROP CONSTRAINT IF EXISTS', label)
            if label.startswith('DO '):
                self.assertIn('END', label)

    def test_the_status_constraint_names_every_status(self):
        status = [s['label'] for s in self.statements
                  if 'ADD CONSTRAINT ws13_geomodel_runs_status' in s['label']]
        self.assertEqual(len(status), 1)
        for name in wg.STATUSES:
            self.assertIn("'%s'" % name, status[0])
        self.assertIn('NOT VALID', status[0])
        guard = [s for s in self.statements if 'status_guard' in s['label']]
        self.assertEqual(len(guard), 1)

    def test_migrate_runs_every_statement_in_one_transaction(self):
        conn = FakeConn()
        report = wg.migrate(conn, echo=False)
        self.assertEqual(len(report), 19)
        self.assertEqual(conn.transactions, 1)
        self.assertEqual(len(conn.statements), 19)

    def catalogue(self, columns=wg.LEDGER_COLUMNS, constraints=None, indexes=wg.REQUIRED_INDEXES):
        if constraints is None:
            constraints = [
                ('ws13_geomodel_runs_pkey', 'PRIMARY KEY (sha256, mine_key)'),
                ('ws13_geomodel_runs_status',
                 "CHECK ((status = ANY (ARRAY['planned'::text, 'parked'::text, "
                 "'skipped'::text, 'built'::text, 'published'::text, 'error'::text])))"),
                ('ws13_geomodel_runs_published', "CHECK (((status <> 'published'::text) OR "
                                                 "((model_id IS NOT NULL) AND "
                                                 "(content_hash IS NOT NULL))))")]
        return FakeConn(answers={'pg_attribute': [(c,) for c in columns],
                                 'pg_constraint': list(constraints),
                                 'pg_indexes': [(i,) for i in indexes]})

    def test_check_passes_a_finished_catalogue(self):
        results = wg.run_checks(self.catalogue())
        self.assertTrue(all(ok for _, ok, _ in results), [r for r in results if not r[1]])

    def test_check_reports_every_gap(self):
        results = wg.run_checks(self.catalogue(columns=('sha256', 'mine_key'),
                                               indexes=('ws13_geomodel_runs_pkey',)))
        missing = {name for name, ok, _ in results if not ok}
        self.assertIn('column ws13_geomodel_runs.content_hash', missing)
        self.assertIn('index ws13_geomodel_runs_status', missing)
        narrowed = [('ws13_geomodel_runs_pkey', 'PRIMARY KEY (sha256)'),
                    ('ws13_geomodel_runs_status', "CHECK ((status = ANY (ARRAY['planned'::text])))")]
        results = wg.run_checks(self.catalogue(constraints=narrowed))
        missing = {name: detail for name, ok, detail in results if not ok}
        self.assertIn('status constraint admits every status', missing)
        self.assertIn('parked', missing['status constraint admits every status'])
        self.assertIn('primary key (sha256, mine_key)', missing)
        self.assertIn('constraint ws13_geomodel_runs_published', missing)

    def test_check_on_the_cli_exits_1_on_a_gap_and_0_when_clean(self):
        conn = self.catalogue(columns=('sha256',))
        with mock.patch.object(wg, '_connect', lambda args: conn):
            self.assertEqual(wg.run(['--check', '--dsn', 'postgresql://x'], log=lambda m: None), 1)
        conn = self.catalogue()
        with mock.patch.object(wg, '_connect', lambda args: conn):
            self.assertEqual(wg.run(['--check', '--dsn', 'postgresql://x'], log=lambda m: None), 0)
            self.assertEqual(wg.run(['--migrate', '--dsn', 'postgresql://x'], log=lambda m: None), 0)
        self.assertEqual(conn.committed, 1)


# ------------------------------------------------------------------ fleet
FAKE_PYTHON = '''#!/bin/sh
echo "$*" >> "$FAKE_PY_CALLS"
code=$(head -1 "$FAKE_RC")
tail -n +2 "$FAKE_RC" > "$FAKE_RC.next" && mv "$FAKE_RC.next" "$FAKE_RC"
[ -n "$code" ] || code=0
exit "$code"
'''
FAKE_SLEEP = '''#!/bin/sh
echo "$1" >> "$FAKE_SLEEPS"
exit 0
'''


class FleetTests(unittest.TestCase):
    """The geomodel branch of infra/fleet/run_worker.sh, run with stubs."""

    SCRIPT = ROOT / 'infra' / 'fleet' / 'run_worker.sh'

    def sandbox(self):
        tmp = Path(tempfile.mkdtemp(prefix='ws13-geomodel-fleet-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        opt, log, binp = tmp / 'opt' / 'ws13', tmp / 'var' / 'log', tmp / 'bin'
        for p in (opt / 'status', log, binp):
            p.mkdir(parents=True)
        body = self.SCRIPT.read_text().replace('/opt/ws13', str(opt)).replace('/var/log', str(log))
        (opt / 'run_worker.sh').write_text(body)
        for name, text in (('python3', FAKE_PYTHON), ('sleep', FAKE_SLEEP)):
            path = binp / name
            path.write_text(text)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return types.SimpleNamespace(root=tmp, opt=opt, log=log, bin=binp,
                                     calls=tmp / 'py.log', sleeps=tmp / 'sleeps.log',
                                     rc=tmp / 'rc.txt')

    def sweep(self, codes, mode='geomodel', worker='1'):
        box = self.sandbox()
        box.rc.write_text('\n'.join(str(c) for c in codes) + '\n')
        box.calls.write_text('')
        box.sleeps.write_text('')
        env = dict(os.environ)
        env.update({'PATH': '%s:%s' % (box.bin, env['PATH']), 'WS13_MODE': mode,
                    'WS13_NODE_SLOT': '1', 'WS13_WORKERS_PER_NODE': '2',
                    'WS13_SHARD_COUNT': '64', 'WS13_SWEEP_DOCS': '25',
                    'WS13_SWEEP_MAX_SECONDS': '86400',
                    'WS13_DRAIN_FILE': str(box.root / 'drain'),
                    'FAKE_PY_CALLS': str(box.calls), 'FAKE_SLEEPS': str(box.sleeps),
                    'FAKE_RC': str(box.rc)})
        done = subprocess.run(['bash', str(box.opt / 'run_worker.sh'), worker], env=env,
                              cwd=str(box.opt), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        status = (box.opt / 'status' / worker).read_text().strip()
        calls = box.calls.read_text().splitlines()
        naps = [int(n) for n in box.sleeps.read_text().split()]
        return status, calls, naps, box

    def test_the_script_parses(self):
        subprocess.run(['bash', '-n', str(self.SCRIPT)], check=True)
        text = self.SCRIPT.read_text()
        self.assertIn('if [ "$WS13_MODE" = geomodel ]; then', text)
        self.assertIn('if [ "$WS13_MODE" = confidence ]; then', text)

    def test_geomodel_mode_sweeps_the_driver_on_the_contract(self):
        status, calls, naps, box = self.sweep([11, 10, 0])
        self.assertEqual(status, '0')
        self.assertEqual(len(calls), 3)
        # slot 1, two workers per node, process 1 -> shard 2; sharded, published to S3
        self.assertTrue(all(c.startswith(
            '%s/ws13_geomodel.py --shard 2 --shards 64 --publish s3 --limit 25' % box.opt)
            for c in calls), calls)
        self.assertEqual(naps, [60])
        self.assertIn('finished nothing', (box.log / 'ws13-geomodel-1.log').read_text())

    def test_environment_and_arithmetic_failures_reach_the_agent(self):
        for code in (2, 3, 12):
            status, calls, _naps, _box = self.sweep([code, 0])
            self.assertEqual((status, len(calls)), (str(code), 1), code)

    def test_an_unhandled_exception_is_retried_then_failed(self):
        status, calls, naps, _box = self.sweep([1] * 10)
        self.assertEqual((status, len(calls), naps), ('1', 3, [60, 120]))

    def test_the_ocr_mode_is_untouched(self):
        status, calls, _naps, _box = self.sweep([0], mode='ocr')
        self.assertEqual((status, len(calls)), ('0', 1))
        self.assertTrue(calls[0].startswith('ws13_worker.py'), calls)


if __name__ == '__main__':
    unittest.main()
