import io
import email.message
import json
import os
import sys
import tempfile
import unittest
from urllib import error as urlerror


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import mine_file_harvest as harvest
import portal_registry


class FixtureClient:
    def __init__(self, pages):
        self.pages = pages

    def bytes(self, url, accept, max_bytes):
        value = self.pages[url]
        return value, {}, url


class JsonFixtureClient:
    def __init__(self, pages):
        self.pages = pages

    def json(self, url, max_bytes=32 * 1024 * 1024):
        return self.pages[url], {}, url


class Response:
    def __init__(self, raw, content_type='application/pdf',
                 url='https://fixture.invalid/document.pdf'):
        self.raw = io.BytesIO(raw)
        self.headers = email.message.Message()
        self.headers['Content-Type'] = content_type
        self.headers['Content-Length'] = str(len(raw))
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()

    def read(self, size=-1):
        return self.raw.read(size)


class DocumentClient:
    def __init__(self, bodies):
        self.bodies = bodies

    def open(self, url, accept='*/*'):
        return Response(self.bodies[url])


class RateLimitOpener:
    def __init__(self, retry_after='120'):
        self.retry_after, self.calls = retry_after, []

    def open(self, req, timeout=None):
        self.calls.append(req.full_url)
        headers = email.message.Message()
        if self.retry_after is not None:
            headers['Retry-After'] = self.retry_after
        raise urlerror.HTTPError(req.full_url, 429, 'Too Many Requests',
                                 headers, None)


class RecordingRobots:
    def __init__(self):
        self.urls = []

    def allowed(self, url):
        self.urls.append(url)
        return True, 'fixture'

    def delay(self, url):
        return 0


class RedirectOpener:
    def __init__(self, source, target):
        self.source, self.target, self.calls = source, target, []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        if url == self.source:
            headers = email.message.Message()
            headers['Location'] = self.target
            raise urlerror.HTTPError(url, 302, 'Found', headers, None)
        return Response(b'ok', content_type='text/plain', url=url)


class FakeS3:
    def __init__(self, wrong=False):
        self.body = b''
        self.extra = None
        self.wrong = wrong

    def upload_fileobj(self, stream, bucket, key, ExtraArgs=None):
        self.body = stream.read()
        self.extra = ExtraArgs

    def head_object(self, Bucket, Key):
        metadata = dict(self.extra['Metadata'])
        if self.wrong:
            metadata['sha256'] = '0' * 64
        return {'ContentLength': len(self.body), 'ContentType': 'application/pdf',
                'Metadata': metadata}


class WS12RegistryTests(unittest.TestCase):
    def test_registry_is_complete_and_explicit(self):
        result = portal_registry.validate_registry()
        self.assertTrue(result['ok'], '\n'.join(result['errors']))
        self.assertEqual(result['portals'], 59)
        self.assertEqual(result['states_covered'], 49)
        rows = portal_registry.load_registry()
        expected_p2 = set(
            'AR FL ID LA MS MT ND NE OR SD WA WY'.split())
        observed = {row['jurisdiction'] for row in rows.values()
                    if row['tier'] == 2}
        # ID/MT are Tier-1 holdings but still satisfy their phase-2 probe;
        # the 2026-08-14 cohort added further tier-2 states beyond phase 2.
        self.assertTrue(expected_p2 <= (observed | {'ID', 'MT'}))
        for code in 'MI MO PA VA NC SC GA'.split():
            self.assertTrue(any(row['jurisdiction'] == code
                                for row in rows.values()), code)
        for portal_id, row in rows.items():
            with self.subTest(portal_id=portal_id):
                self.assertIn('detail_page_pattern', row)
                self.assertIn('pdf_link_pattern', row)
                self.assertIn('id_scheme', row)
                self.assertFalse(row['harvest_state']['full_crawl_complete'])
                self.assertIsNone(row['harvest_state']['manifest_sha256'])

    def test_executable_portals_have_reviewed_terms(self):
        for row in portal_registry.load_registry().values():
            if row['status'] == 'harvest_ready':
                self.assertEqual(
                    row['access']['terms_status'],
                    'reviewed_no_automation_prohibition')
                self.assertTrue(row['crawler']['automation_permitted'])
                self.assertGreaterEqual(
                    row['crawler']['min_interval_seconds'], 0.25)

    def test_igs_contract_has_acceptance_seed_and_two_enumerations(self):
        row = portal_registry.load_registry()['igs_mines']
        self.assertIn('IF0126', row['crawler']['acceptance_seed_ids'])
        self.assertEqual(row['crawler']['current_item_id'],
                         'a2d491a1f53f449281e48e171c28ffaf')
        self.assertGreaterEqual(len(row['crawler']['legacy_id_ranges']), 20)
        self.assertIn('MILS_MRDS', row['rights_rules'][0]['url_pattern'])
        self.assertIn('hub.arcgis.com', row['crawler']['current_hub_dataset_url'])


class WS12QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = harvest.QueueDB(os.path.join(self.temp.name, 'queue.sqlite3'))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_targeted_igs_seed_is_small_and_never_claims_full_cursor(self):
        registry = portal_registry.load_registry()
        runner = harvest.Harvester(registry, self.db, harvest.MemorySink())
        runner.seed(['igs_mines'], mine_ids=['IF0126'])
        tasks = self.db.conn.execute(
            'SELECT kind, payload FROM tasks ORDER BY rowid').fetchall()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({row['kind'] for row in tasks},
                         {'html_detail', 'arcgis_hub_metadata'})
        self.assertEqual({json.loads(row['payload'])['portal_source']
                          for row in tasks}, {'legacy', 'current'})
        self.db.maybe_complete(['igs_mines'], full_scope=False)
        run = self.db.conn.execute(
            'SELECT * FROM portal_runs WHERE portal_id=?',
            ('igs_mines',)).fetchone()
        self.assertEqual(run['crawl_scope'], 'targeted:IF0126')
        self.assertFalse(run['cursor_exhausted'])
        self.assertIsNone(run['completed_at'])

    def test_if0126_parser_skips_unverified_minedoc_and_queues_usgs(self):
        registry = portal_registry.load_registry()
        portal = registry['igs_mines']
        detail_url = portal['crawler']['detail_url_template'].format(
            mine_id='IF0126')
        page = b'''<html><h1>IF0126 : St. Louis Mine</h1>
          <p>County: Butte Idaho PLSS (TRSQQ): 03N 24E 15 NENW Latitude: 43</p>
          <a href="/Uploads/Data/MineDocs/IF0131_001.pdf">assay</a>
          <a href="/Uploads/Data/MILS_MRDS/MILS-160230014.pdf">download</a>
          <a href="/Uploads/Data/MILS_MRDS/MRDS-W015681.pdf">download</a>
        </html>'''
        runner = harvest.Harvester(
            registry, self.db, harvest.MemorySink(),
            client_factory=lambda unused: FixtureClient({detail_url: page}))
        task = {'url': detail_url, 'payload': {
            'mine_id': 'IF0126', 'portal_source': 'legacy'}}
        runner._html_detail(task, portal)
        candidates = [dict(row) for row in self.db.conn.execute(
            'SELECT * FROM document_candidates ORDER BY source_url')]
        self.assertEqual(len(candidates), 3)
        assay = next(row for row in candidates if 'MineDocs' in row['source_url'])
        self.assertEqual(assay['disposition'], 'skipped')
        # MineDocs now match the state_archive_research_copy rule, but the
        # default run still refuses them: admission is an explicit opt-in.
        self.assertEqual(assay['reason'], 'research_copy_admission_disabled')
        self.assertEqual(assay['rights_status'], 'state_archive_research_copy')
        queued = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE kind='document'").fetchone()['n']
        self.assertEqual(queued, 2)
        source = self.db.conn.execute(
            'SELECT * FROM source_records').fetchone()
        self.assertEqual(source['mine_name'], 'St. Louis Mine')
        metadata = json.loads(source['metadata'])
        self.assertEqual(metadata['county'], 'Butte')
        self.assertEqual(metadata['trs'], '03N 24E 15 NENW')

    def test_current_hub_probe_is_explicit_and_blocks_full_completion(self):
        registry = portal_registry.load_registry()
        portal = registry['igs_mines']
        url = portal['crawler']['current_hub_dataset_url']
        payload = {'data': {
            'id': portal['crawler']['current_item_id'] + '_0',
            'attributes': {
                'recordCount': 9424,
                'layer': {'hasAttachments': False},
            },
        }}
        runner = harvest.Harvester(
            registry, self.db, harvest.MemorySink(),
            client_factory=lambda unused: JsonFixtureClient({url: payload}))
        runner._arcgis_hub_metadata({
            'url': url, 'payload': {
                'portal_source': 'current', 'target_ids': ['IF0126']}}, portal)
        status = json.loads(self.db.conn.execute(
            "SELECT value FROM observations WHERE portal_id='igs_mines' "
            "AND name='arcgis_current_inventory'").fetchone()['value'])
        self.assertFalse(status['has_attachments'])
        self.assertFalse(status['target_ids_verified'])
        self.assertEqual(status['feature_service_access'],
                         'robots_denied_http_403')

        self.db.seed_portal(portal, crawl_scope='full')
        self.db.maybe_complete(['igs_mines'], full_scope=True)
        run = self.db.conn.execute(
            "SELECT * FROM portal_runs WHERE portal_id='igs_mines'").fetchone()
        self.assertFalse(run['cursor_exhausted'])
        self.assertIsNone(run['completed_at'])

    def test_hash_dedupe_and_closed_manifest_rights_fields(self):
        raw = b'%PDF-1.4\nfixture\n%%EOF\n'
        urls = ['https://fixture.invalid/a.pdf',
                'https://fixture.invalid/b.pdf']
        portal = {
            'id': 'fixture', 'jurisdiction': 'ID',
            'crawler': {'min_interval_seconds': 0.25}}
        sink = harvest.MemorySink()
        runner = harvest.Harvester(
            {'fixture': portal}, self.db, sink,
            client_factory=lambda unused: DocumentClient(
                {url: raw for url in urls}))
        for index, url in enumerate(urls):
            payload = {
                'portal_id': 'fixture', 'portal_source': 'test',
                'source_url': url, 'mine_id': f'M{index}',
                'mine_name': 'Fixture Mine', 'state': 'ID',
                'document_title': f'Doc {index}',
                'rights_status': 'public_domain',
                'rights_basis': 'U.S. federal work under 17 U.S.C. 105.'}
            self.db.enqueue(
                'fixture', f'doc-{index}', 'document', url, payload)
        runner.run(['fixture'], full_scope=False)
        self.assertEqual(len(sink.objects), 1)
        self.assertEqual(self.db.conn.execute(
            'SELECT COUNT(*) AS n FROM documents').fetchone()['n'], 2)
        path = os.path.join(self.temp.name, 'manifest.jsonl')
        self.assertEqual(self.db.write_manifest(path), 2)
        with open(path, encoding='utf-8') as source:
            rows = [json.loads(line) for line in source]
        for row in rows:
            self.assertEqual(set(row), set(harvest.MANIFEST_FIELDS))
            self.assertIs(row['public_domain'], True)
            self.assertIs(row['paywalled'], False)
            self.assertEqual(row['sha256'], rows[0]['sha256'])
            self.assertEqual(row['s3_uri'], rows[0]['s3_uri'])

    def test_manifest_refuses_unverified_rights(self):
        payload = {
            'portal_id': 'fixture', 'portal_source': 'test',
            'source_url': 'https://fixture.invalid/x.pdf', 'mine_id': 'M1',
            'mine_name': 'Mine', 'state': 'ID',
            'rights_status': 'unverified'}
        with self.assertRaisesRegex(harvest.HarvestError, 'rights-unverified'):
            self.db.record_document(payload, {
                'sha256': 'a' * 64, 'bytes': 10,
                's3_uri': 's3://fixture/a.pdf',
                'content_type': 'application/pdf'})

    def test_coverage_distinguishes_unknown_from_established_zero(self):
        registry = portal_registry.load_registry()
        coverage = self.db.coverage(registry)
        by_id = {row['portal_id']: row for row in coverage['portals']}
        self.assertIsNone(by_id['igs_mines']['documents_found'])
        self.assertEqual(by_id['igs_mines']['counts_status'], 'not_started')
        self.assertEqual(by_id['ca_mines_online']['documents_found'], 0)
        self.assertEqual(
            by_id['ca_mines_online']['counts_status'],
            'registry_established_no_attachments')
        self.assertFalse(by_id['ca_mines_online']['crawl_complete'])
        self.assertFalse(by_id['ca_mines_online']['cursor_exhausted'])

    def test_refresh_never_resets_completed_document_downloads(self):
        self.db.enqueue('p', 'index', 'html_index', 'https://x.test/', {})
        self.db.enqueue('p', 'doc', 'document', 'https://x.test/a.pdf', {})
        self.db.finish('index')
        self.db.finish('doc')
        # A run row is needed only so refresh can clear its completion marker.
        self.db.conn.execute(
            '''INSERT INTO portal_runs(portal_id, registry_sha256, seeded_at,
               crawl_scope, cursor_exhausted, completed_at)
               VALUES ('p', ?, 'now', 'full', 1, 'now')''', ('a' * 64,))
        self.db.conn.commit()
        self.db.refresh(['p'])
        states = {row['task_key']: row['status'] for row in
                  self.db.conn.execute('SELECT task_key, status FROM tasks')}
        self.assertEqual(states, {'index': 'pending', 'doc': 'done'})

    def test_reopen_recovers_interrupted_active_document_task(self):
        self.db.enqueue('p', 'doc', 'document', 'https://x.test/a.pdf', {})
        claimed = self.db.claim(['p'])
        self.assertEqual(claimed['task_key'], 'doc')
        path = self.db.path
        self.db.close()
        self.db = harvest.QueueDB(path)
        recovered = self.db.claim(['p'])
        self.assertEqual(recovered['task_key'], 'doc')
        self.assertEqual(recovered['attempts'], 2)

    def test_redirect_rechecks_https_host_robots_and_throttle_policy(self):
        source = 'https://portal.test/start'
        target = 'https://files.test/document.pdf'
        robots = RecordingRobots()
        client = harvest.HttpClient(
            min_interval_seconds=0, robots=robots,
            allowed_hosts={'portal.test', 'files.test'},
            opener=RedirectOpener(source, target))
        with client.open(source) as response:
            self.assertEqual(response.read(), b'ok')
        self.assertEqual(robots.urls, [source, target])

        blocked = harvest.HttpClient(
            min_interval_seconds=0, robots=RecordingRobots(),
            allowed_hosts={'portal.test'}, opener=RedirectOpener(source, target))
        with self.assertRaisesRegex(harvest.PermanentSkip, 'allowlisted'):
            blocked.open(source)

    def test_robots_redirect_is_refused_before_destination_request(self):
        source = 'https://fixture.invalid/robots.txt'
        target = 'https://unreviewed.invalid/robots.txt'
        opener = RedirectOpener(source, target)
        policy = harvest.RobotsPolicy(opener=opener)
        with self.assertRaisesRegex(
                harvest.TransientHarvestError, 'robots redirect refused'):
            policy.rules('https://fixture.invalid/document.pdf')
        self.assertEqual(opener.calls, [source])

    def test_malformed_redirect_port_is_a_policy_skip(self):
        client = harvest.HttpClient(
            robots=RecordingRobots(), allowed_hosts={'fixture.invalid'},
            opener=RedirectOpener('', ''))
        with self.assertRaisesRegex(harvest.PermanentSkip, 'unsafe_or_non_https'):
            client.open('https://fixture.invalid:bad/document.pdf')

    def test_s3_sink_verifies_remote_bytes_hash_and_pdf_metadata(self):
        raw = b'%PDF-1.4\nverified\n%%EOF\n'
        digest = harvest.hashlib.sha256(raw).hexdigest()
        client = FakeS3()
        sink = harvest.S3OriginalSink('fixture-bucket', client=client)
        uri = sink.put(io.BytesIO(raw), digest, len(raw),
                       'application/pdf', 'fixture')
        self.assertIn(digest, uri)
        self.assertEqual(client.extra['ServerSideEncryption'], 'AES256')
        self.assertEqual(client.extra['Metadata']['public-domain'], 'true')
        with self.assertRaisesRegex(harvest.TransientHarvestError, 'verification'):
            harvest.S3OriginalSink(
                'fixture-bucket', client=FakeS3(wrong=True)).put(
                    io.BytesIO(raw), digest, len(raw), 'application/pdf', 'fixture')


class ResearchCopyAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = harvest.QueueDB(os.path.join(self.temp.name, 'queue.sqlite3'))
        self.registry = portal_registry.load_registry()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    PAYLOAD = {
        'portal_id': 'igs_mines', 'portal_source': 'legacy',
        'source_url': 'https://idahogeology.org/Uploads/Data/MineDocs/EC0433_040.pdf',
        'mine_id': 'EC0433', 'mine_name': 'Fixture Mine', 'state': 'ID',
        'rights_status': 'state_archive_research_copy',
        'rights_basis': 'IGS Mineral Property File scan; private research '
                        'copy, no public-domain assertion.',
    }

    def test_minedocs_rule_yields_research_copy_status(self):
        portal = self.registry['igs_mines']
        payload = harvest._pdf_candidate_payload(
            portal, 'legacy',
            'https://idahogeology.org/Uploads/Data/MineDocs/EC0433_040.pdf',
            'EC0433', 'Fixture Mine')
        self.assertEqual(payload['rights_status'],
                         'state_archive_research_copy')
        self.assertIn('research copy', payload['rights_basis'])
        mils = harvest._pdf_candidate_payload(
            portal, 'legacy',
            'https://idahogeology.org/Uploads/Data/MILS_MRDS/MRDS-X1.pdf',
            'EC0433', 'Fixture Mine')
        self.assertEqual(mils['rights_status'], 'public_domain')

    def test_disabled_harvester_skips_and_enabled_requires_research_sink(self):
        portal = self.registry['igs_mines']
        runner = harvest.Harvester(self.registry, self.db, harvest.MemorySink())
        self.assertFalse(runner._enqueue_document(portal, dict(self.PAYLOAD)))
        row = self.db.conn.execute(
            'SELECT * FROM document_candidates').fetchone()
        self.assertEqual(row['disposition'], 'skipped')
        self.assertEqual(row['reason'], 'research_copy_admission_disabled')
        with self.assertRaisesRegex(ValueError, 'research sink'):
            harvest.Harvester(self.registry, self.db, harvest.MemorySink(),
                              admit_research_copies=True)

    def test_enabled_harvester_queues_research_copy(self):
        portal = self.registry['igs_mines']
        runner = harvest.Harvester(
            self.registry, self.db, harvest.MemorySink(),
            admit_research_copies=True, research_sink=harvest.MemorySink())
        self.assertTrue(runner._enqueue_document(portal, dict(self.PAYLOAD)))
        task = self.db.conn.execute(
            "SELECT * FROM tasks WHERE kind='document'").fetchone()
        self.assertIsNotNone(task)

    def test_record_document_labels_research_copy_truthfully(self):
        result = {'sha256': '0' * 64, 'bytes': 10,
                  's3_uri': 's3://fixture/research-copies/igs_mines/00/x.pdf',
                  'content_type': 'application/pdf'}
        self.db.record_document(dict(self.PAYLOAD), result)
        row = self.db.conn.execute('SELECT * FROM documents').fetchone()
        self.assertEqual(row['admission_class'], 'state_archive_research_copy')
        self.assertEqual(row['public_domain'], 0)
        self.assertEqual(row['paywalled'], 0)
        manifest_path = os.path.join(self.temp.name, 'manifest.jsonl')
        self.db.write_manifest(manifest_path)
        with open(manifest_path, encoding='utf-8') as handle:
            manifest_row = json.loads(handle.readline())
        self.assertEqual(manifest_row['admission_class'],
                         'state_archive_research_copy')
        self.assertFalse(manifest_row['public_domain'])

    def test_record_document_still_refuses_unverified(self):
        payload = dict(self.PAYLOAD, rights_status='unverified')
        result = {'sha256': '1' * 64, 'bytes': 10,
                  's3_uri': 's3://fixture/x.pdf',
                  'content_type': 'application/pdf'}
        with self.assertRaisesRegex(harvest.HarvestError, 'rights-unverified'):
            self.db.record_document(payload, result)

    def test_old_schema_queue_migrates_preserving_rows(self):
        path = os.path.join(self.temp.name, 'legacy-queue.sqlite3')
        conn = harvest.sqlite3.connect(path)
        conn.executescript('''
            CREATE TABLE hash_objects (
                sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL,
                s3_uri TEXT NOT NULL, content_type TEXT NOT NULL);
            CREATE TABLE documents (
                portal_id TEXT NOT NULL, portal_source TEXT NOT NULL,
                source_url TEXT NOT NULL, mine_id TEXT NOT NULL,
                mine_name TEXT NOT NULL, state TEXT NOT NULL, county TEXT,
                trs TEXT, document_title TEXT NOT NULL, doc_date TEXT,
                doc_type TEXT,
                sha256 TEXT NOT NULL REFERENCES hash_objects(sha256),
                bytes INTEGER NOT NULL, retrieval_date TEXT NOT NULL,
                content_type TEXT NOT NULL, s3_uri TEXT NOT NULL, etag TEXT,
                last_modified TEXT,
                public_domain INTEGER NOT NULL CHECK(public_domain = 1),
                rights_basis TEXT NOT NULL,
                paywalled INTEGER NOT NULL CHECK(paywalled = 0),
                PRIMARY KEY(portal_id, portal_source, source_url, mine_id));
        ''')
        conn.execute(
            "INSERT INTO hash_objects VALUES ('a'||substr('%s',2), 5, "
            "'s3://fixture/a.pdf', 'application/pdf')" % ('a' * 64))
        conn.execute(
            "INSERT INTO documents VALUES ('igs_mines','legacy',"
            "'https://x.test/a.pdf','IF0001','Mine','ID',NULL,NULL,'T',NULL,"
            "NULL,'%s',5,'2026-08-14','application/pdf','s3://fixture/a.pdf',"
            "NULL,NULL,1,'usgs public domain basis',0)" % ('a' * 64))
        conn.commit()
        conn.close()
        migrated = harvest.QueueDB(path)
        try:
            row = migrated.conn.execute('SELECT * FROM documents').fetchone()
            self.assertEqual(row['admission_class'], 'public_domain')
            self.assertEqual(row['public_domain'], 1)
            self.assertEqual(row['mine_id'], 'IF0001')
        finally:
            migrated.close()


class CandidateUrlAndFetchCacheTests(unittest.TestCase):
    def setUp(self):
        self.registry = portal_registry.load_registry()
        self.portal = self.registry['igs_mines']

    def test_fragment_wrapped_pdf_url_is_extracted(self):
        wrapped = ('https://idahogeology.org/WebMap4/WebData/Mines.aspx'
                   '?Operation=Details&IGSID=BA0001'
                   '#https://www.idahogeology.org/Uploads/Data/ISMIR/'
                   '1923_ISMIR.pdf#')
        self.assertEqual(
            harvest.canonical_candidate_url(wrapped, self.portal),
            'https://idahogeology.org/Uploads/Data/ISMIR/1923_ISMIR.pdf')

    def test_www_alias_collapses_and_fragment_is_dropped(self):
        self.assertEqual(
            harvest.canonical_candidate_url(
                'https://www.idahogeology.org/Uploads/Data/MineDocs/'
                'BO0499_002.pdf#page=2', self.portal),
            'https://idahogeology.org/Uploads/Data/MineDocs/BO0499_002.pdf')
        # Hosts outside the portal allowlist keep their exact netloc.
        self.assertEqual(
            harvest.canonical_candidate_url(
                'https://www.example.gov/a.pdf', self.portal),
            'https://www.example.gov/a.pdf')

    def test_document_cache_hit_records_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'queue.sqlite3'))
            try:
                url = ('https://idahogeology.org/Uploads/Data/MineDocs/'
                       'BO0499_002.pdf')
                db.conn.execute(
                    "INSERT INTO hash_objects VALUES (?, 7, "
                    "'s3://fixture/research-copies/x.pdf', 'application/pdf')",
                    ('b' * 64,))
                db.record_url_object(url, 'b' * 64)

                def refuse_client(portal):
                    raise AssertionError('cache hit must not open a client')

                runner = harvest.Harvester(
                    self.registry, db, harvest.MemorySink(),
                    client_factory=refuse_client,
                    admit_research_copies=True,
                    research_sink=harvest.MemorySink())
                payload = {
                    'portal_id': 'igs_mines', 'portal_source': 'legacy',
                    'source_url': url, 'mine_id': 'BO0100',
                    'mine_name': 'Second Mine', 'state': 'ID',
                    'rights_status': 'state_archive_research_copy',
                    'rights_basis': 'IGS Mineral Property File scan; '
                                    'private research copy.',
                }
                runner._document({'url': url, 'payload': payload}, self.portal)
                row = db.conn.execute(
                    "SELECT * FROM documents WHERE mine_id='BO0100'").fetchone()
                self.assertEqual(row['sha256'], 'b' * 64)
                self.assertEqual(row['admission_class'],
                                 'state_archive_research_copy')
            finally:
                db.close()


class TwoNodePeerSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = portal_registry.load_registry()

    def tearDown(self):
        self.temp.cleanup()

    def _queue(self, name, order='asc'):
        return harvest.QueueDB(
            os.path.join(self.temp.name, name), claim_order=order)

    def test_claim_order_desc_takes_the_other_end(self):
        db = self._queue('q.sqlite3', order='desc')
        try:
            for index in range(3):
                db.enqueue('p', f't{index}', 'document',
                           f'https://x.test/{index}.pdf', {})
            self.assertEqual(db.claim(['p'])['task_key'], 't2')
            asc = harvest.QueueDB(db.path)
            self.assertEqual(asc.claim(['p'])['task_key'], 't0')
            asc.close()
        finally:
            db.close()

    def test_peer_maps_exchange_and_clear_stragglers(self):
        share = os.path.join(self.temp.name, 'share')
        node_a = self._queue('a.sqlite3')
        node_b = self._queue('b.sqlite3', order='desc')
        try:
            url = 'https://idahogeology.org/Uploads/Data/MineDocs/EC0433_040.pdf'
            node_a.conn.execute(
                "INSERT INTO hash_objects VALUES (?, 9, "
                "'s3://fixture/research-copies/x.pdf', 'application/pdf')",
                ('c' * 64,))
            node_a.record_url_object(url, 'c' * 64)
            sync_a = harvest.PeerSync(share, 'node-a', node_a,
                                      interval_seconds=0)
            sync_b = harvest.PeerSync(share, 'node-b', node_b,
                                      interval_seconds=0)
            sync_a.export_map()
            self.assertEqual(sync_b.import_peer_maps(), 1)
            self.assertEqual(sync_b.import_peer_maps(), 0)
            cached = node_b.url_object(url)
            self.assertEqual(cached['sha256'], 'c' * 64)

            def refuse_client(portal):
                raise AssertionError('peer-synced URL must not be re-fetched')

            runner = harvest.Harvester(
                self.registry, node_b, harvest.MemorySink(),
                client_factory=refuse_client, admit_research_copies=True,
                research_sink=harvest.MemorySink())
            payload = {
                'portal_id': 'igs_mines', 'portal_source': 'legacy',
                'source_url': url, 'mine_id': 'EC0500',
                'mine_name': 'Straggler Mine', 'state': 'ID',
                'rights_status': 'state_archive_research_copy',
                'rights_basis': 'IGS Mineral Property File scan; '
                                'private research copy.',
            }
            runner._document({'url': url, 'payload': payload},
                             self.registry['igs_mines'])
            row = node_b.conn.execute(
                "SELECT sha256 FROM documents WHERE mine_id='EC0500'").fetchone()
            self.assertEqual(row['sha256'], 'c' * 64)
        finally:
            node_a.close()
            node_b.close()

    def test_peer_sync_rejects_malformed_rows_and_bad_node_names(self):
        share = os.path.join(self.temp.name, 'share2')
        db = self._queue('c.sqlite3')
        try:
            with self.assertRaises(ValueError):
                harvest.PeerSync(share, 'Bad Name!', db)
            os.makedirs(share, exist_ok=True)
            with open(os.path.join(share, 'urlmap-evil.jsonl'), 'w',
                      encoding='utf-8') as handle:
                handle.write('{"source_url": "https://x.test/a.pdf", '
                             '"sha256": "nothex", "bytes": 1, '
                             '"s3_uri": "s3://x/a.pdf", '
                             '"content_type": "application/pdf"}\n')
                handle.write('{"source_url": "https://x.test/b.pdf", '
                             '"sha256": "' + 'd' * 64 + '", "bytes": -5, '
                             '"s3_uri": "s3://x/b.pdf", '
                             '"content_type": "application/pdf"}\n')
            sync = harvest.PeerSync(share, 'node-c', db, interval_seconds=0)
            self.assertEqual(sync.import_peer_maps(), 0)
        finally:
            db.close()


class LicensedCopyAdmissionTests(unittest.TestCase):
    AZGS_PAGE = {
        'https://data.azgs.arizona.edu/api/v1/metadata?collection_group=ADMM&offset=0&limit=100': {
            'data': [{
                'collection_id': 'ADMM-FIXTURE-1',
                'metadata': {
                    'title': 'Octave Mine file',
                    'license': {'type': 'CC BY-NC-SA 4.0',
                                'url': 'https://creativecommons.org/licenses/by-nc-sa/4.0/'},
                    'files': [{'name': 'octave.pdf', 'type': 'document'}],
                },
            }, {
                'collection_id': 'ADMM-FIXTURE-2',
                'metadata': {
                    'title': 'Federal reprint',
                    'license': {'type': 'Public Domain',
                                'url': 'https://creativecommons.org/publicdomain/mark/1.0/'},
                    'files': [{'name': 'usgs-reprint.pdf', 'type': 'document'}],
                },
            }],
            'collectionCount': 2,
        }}

    def _run_azgs(self, db, **kwargs):
        registry = portal_registry.load_registry()
        portal = registry['azgs_admmr']
        url = harvest._query_url(portal['crawler']['metadata_url'], {
            'collection_group': portal['crawler']['collection_group'],
            'offset': 0, 'limit': portal['crawler']['page_size']})
        page = {url: self.AZGS_PAGE[
            'https://data.azgs.arizona.edu/api/v1/metadata'
            '?collection_group=ADMM&offset=0&limit=100']}
        runner = harvest.Harvester(
            registry, db, harvest.MemorySink(),
            client_factory=lambda unused: JsonFixtureClient(page), **kwargs)
        runner._azgs_metadata({'url': url, 'payload': {'offset': 0}}, portal)

    def test_cc_licensed_default_skips_and_pd_still_queues(self):
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'q.sqlite3'))
            try:
                self._run_azgs(db)
                rows = {row['source_url']: row for row in db.conn.execute(
                    'SELECT * FROM document_candidates')}
                cc = next(v for k, v in rows.items() if 'octave' in k)
                self.assertEqual(cc['disposition'], 'skipped')
                self.assertEqual(cc['reason'],
                                 'licensed_copy_admission_disabled')
                self.assertEqual(cc['rights_status'], 'cc_by_nc_sa_licensed')
                pd = next(v for k, v in rows.items() if 'usgs-reprint' in k)
                self.assertEqual(pd['disposition'], 'queued')
                self.assertEqual(pd['rights_status'], 'public_domain')
            finally:
                db.close()

    def test_cc_licensed_enabled_queues_with_attribution_basis(self):
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'q.sqlite3'))
            try:
                self._run_azgs(db, admit_licensed_copies=True,
                               licensed_sink=harvest.MemorySink())
                task = db.conn.execute(
                    "SELECT payload FROM tasks WHERE kind='document' AND "
                    "url LIKE '%octave%'").fetchone()
                payload = json.loads(task['payload'])
                self.assertEqual(payload['rights_status'],
                                 'cc_by_nc_sa_licensed')
                self.assertIn('CC BY-NC-SA 4.0', payload['rights_basis'])
                self.assertIn('ADMM-FIXTURE-1', payload['rights_basis'])
                result = {'sha256': 'e' * 64, 'bytes': 11,
                          's3_uri': 's3://fixture/licensed-copies/x.pdf',
                          'content_type': 'application/pdf'}
                db.record_document(payload, result)
                row = db.conn.execute(
                    "SELECT * FROM documents WHERE sha256=?",
                    ('e' * 64,)).fetchone()
                self.assertEqual(row['admission_class'], 'cc_by_nc_sa_licensed')
                self.assertEqual(row['public_domain'], 0)
            finally:
                db.close()

    def test_enabled_without_sink_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'q.sqlite3'))
            try:
                with self.assertRaisesRegex(ValueError, 'licensed sink'):
                    harvest.Harvester(
                        portal_registry.load_registry(), db,
                        harvest.MemorySink(), admit_licensed_copies=True)
            finally:
                db.close()

    def test_pre_licensed_schema_queue_migrates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'old.sqlite3')
            db = harvest.QueueDB(path)
            db.conn.execute(
                "INSERT INTO hash_objects VALUES (?, 5, 's3://x/a.pdf', "
                "'application/pdf')", ('f' * 64,))
            db.conn.commit()
            db.close()
            # simulate the pre-licensed CHECK by rebuilding without the class
            conn = harvest.sqlite3.connect(path)
            conn.executescript('''
                DROP TABLE documents;
                CREATE TABLE documents (
                    portal_id TEXT NOT NULL, portal_source TEXT NOT NULL,
                    source_url TEXT NOT NULL, mine_id TEXT NOT NULL,
                    mine_name TEXT NOT NULL, state TEXT NOT NULL, county TEXT,
                    trs TEXT, document_title TEXT NOT NULL, doc_date TEXT,
                    doc_type TEXT,
                    sha256 TEXT NOT NULL REFERENCES hash_objects(sha256),
                    bytes INTEGER NOT NULL, retrieval_date TEXT NOT NULL,
                    content_type TEXT NOT NULL, s3_uri TEXT NOT NULL,
                    etag TEXT, last_modified TEXT,
                    admission_class TEXT NOT NULL DEFAULT 'public_domain'
                        CHECK(admission_class IN
                              ('public_domain','state_archive_research_copy')),
                    public_domain INTEGER NOT NULL CHECK(
                        (admission_class = 'public_domain'
                         AND public_domain = 1)
                        OR (admission_class = 'state_archive_research_copy'
                            AND public_domain = 0)),
                    rights_basis TEXT NOT NULL,
                    paywalled INTEGER NOT NULL CHECK(paywalled = 0),
                    PRIMARY KEY(portal_id, portal_source, source_url, mine_id));
            ''')
            conn.execute(
                "INSERT INTO documents VALUES ('p','s','https://x.test/a.pdf',"
                "'M1','Mine','ID',NULL,NULL,'T',NULL,NULL,?,5,'2026-08-15',"
                "'application/pdf','s3://x/a.pdf',NULL,NULL,"
                "'state_archive_research_copy',0,'archive basis text',0)",
                ('f' * 64,))
            conn.commit()
            conn.close()
            migrated = harvest.QueueDB(path)
            try:
                row = migrated.conn.execute(
                    'SELECT admission_class FROM documents').fetchone()
                self.assertEqual(row['admission_class'],
                                 'state_archive_research_copy')
                ddl = migrated.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='documents'"
                ).fetchone()
                self.assertIn('cc_by_nc_sa_licensed', ddl['sql'])
            finally:
                migrated.close()


class NbmgEmbeddedInventoryTests(unittest.TestCase):
    PAGE = (b'<html><script>var mddata = [\n'
            b'["16700022","EASTSIDE","MINERAL","DOUBLE EAGLE VEN\\EIN CLAIM",'
            b'"","1980","","","","1670/16700022.pdf"],\n'
            b'["16700023","EASTSIDE","MINERAL","CONOCO PROJECT","","1981",'
            b'"","","","1670/16700023.pdf"],\n'
            b'] //this is only a portion of the records. when ready to make '
            b'this code live, replace this block</script></html>')

    def test_decodes_js_isms_and_records_partial_inventory_blocker(self):
        registry = portal_registry.load_registry()
        portal = registry['nbmg_mining_district_files']
        index_url = portal['crawler']['index_url']
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'queue.sqlite3'))
            try:
                runner = harvest.Harvester(
                    registry, db, harvest.MemorySink(),
                    client_factory=lambda unused: FixtureClient(
                        {index_url: self.PAGE}),
                    admit_research_copies=True,
                    research_sink=harvest.MemorySink())
                runner._nbmg_index({'url': index_url, 'payload': {}}, portal)
                tasks = db.conn.execute(
                    "SELECT payload FROM tasks WHERE kind='document'").fetchall()
                self.assertEqual(len(tasks), 2)
                payload = json.loads(tasks[0]['payload'])
                self.assertEqual(payload['rights_status'],
                                 'state_archive_research_copy')
                blocker = db.conn.execute(
                    "SELECT value FROM observations WHERE "
                    "name='crawl_completion_blocker'").fetchone()
                self.assertIn('source_declares_partial_inventory',
                              blocker['value'])
                # The typo'd backslash survives as a literal character.
                titles = db.conn.execute(
                    'SELECT mine_name FROM document_candidates').fetchall()
                self.assertTrue(any('EASTSIDE' in row['mine_name']
                                    for row in titles))
            finally:
                db.close()

    def test_partial_inventory_blocker_prevents_cursor_exhaustion(self):
        registry = portal_registry.load_registry()
        portal = registry['nbmg_mining_district_files']
        index_url = portal['crawler']['index_url']
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'queue.sqlite3'))
            try:
                runner = harvest.Harvester(
                    registry, db, harvest.MemorySink(),
                    client_factory=lambda unused: FixtureClient(
                        {index_url: self.PAGE}))
                db.seed_portal(portal, crawl_scope='full')
                runner._nbmg_index({'url': index_url, 'payload': {}}, portal)
                for task in db.conn.execute(
                        "SELECT task_key FROM tasks").fetchall():
                    db.finish(task['task_key'], 'skipped', 'fixture')
                db.maybe_complete(['nbmg_mining_district_files'],
                                  full_scope=True)
                run = db.conn.execute(
                    'SELECT cursor_exhausted, completed_at FROM portal_runs '
                    "WHERE portal_id='nbmg_mining_district_files'").fetchone()
                self.assertFalse(run['cursor_exhausted'])
                self.assertIsNone(run['completed_at'])
            finally:
                db.close()


class RetryAfterTests(unittest.TestCase):
    def test_parse_retry_after_seconds_dates_and_garbage(self):
        self.assertEqual(harvest.parse_retry_after('120'), 120.0)
        self.assertEqual(harvest.parse_retry_after('0'), 0.0)
        self.assertEqual(harvest.parse_retry_after('-5'), 0.0)
        self.assertEqual(harvest.parse_retry_after('99999'), 3600.0)
        self.assertIsNone(harvest.parse_retry_after(None))
        self.assertIsNone(harvest.parse_retry_after(''))
        self.assertIsNone(harvest.parse_retry_after('not-a-date'))
        future = email.utils.format_datetime(
            harvest.dt.datetime.now(harvest.dt.timezone.utc) +
            harvest.dt.timedelta(seconds=90))
        parsed = harvest.parse_retry_after(future)
        self.assertGreater(parsed, 60.0)
        self.assertLessEqual(parsed, 90.5)
        past = email.utils.format_datetime(
            harvest.dt.datetime.now(harvest.dt.timezone.utc) -
            harvest.dt.timedelta(seconds=90))
        self.assertEqual(harvest.parse_retry_after(past), 0.0)

    def test_transient_http_carries_server_retry_after(self):
        client = harvest.HttpClient(
            min_interval_seconds=0, robots=RecordingRobots(),
            allowed_hosts={'fixture.invalid'},
            opener=RateLimitOpener(retry_after='120'))
        with self.assertRaises(harvest.TransientHarvestError) as caught:
            client.open('https://fixture.invalid/catalog')
        self.assertEqual(caught.exception.retry_after, 120.0)

        bare = harvest.HttpClient(
            min_interval_seconds=0, robots=RecordingRobots(),
            allowed_hosts={'fixture.invalid'},
            opener=RateLimitOpener(retry_after=None))
        with self.assertRaises(harvest.TransientHarvestError) as caught:
            bare.open('https://fixture.invalid/catalog')
        self.assertIsNone(caught.exception.retry_after)

    def test_queue_retry_honors_server_backoff_over_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'queue.sqlite3'))
            try:
                db.enqueue('p', 'doc', 'document', 'https://x.test/a.pdf', {})
                task = db.claim(['p'])
                before = harvest.time.time()
                self.assertTrue(db.retry(task, 'HTTP 429', retry_after=120.0))
                row = db.conn.execute(
                    "SELECT not_before FROM tasks WHERE task_key='doc'"
                ).fetchone()
                self.assertGreaterEqual(row['not_before'], before + 120.0)
                self.assertLessEqual(
                    row['not_before'], harvest.time.time() + 3600.0)
            finally:
                db.close()

    def test_queue_retry_jitter_stays_bounded_without_server_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            db = harvest.QueueDB(os.path.join(directory, 'queue.sqlite3'))
            try:
                db.enqueue('p', 'doc', 'document', 'https://x.test/a.pdf', {})
                task = db.claim(['p'])
                before = harvest.time.time()
                self.assertTrue(db.retry(task, 'transient'))
                row = db.conn.execute(
                    "SELECT not_before FROM tasks WHERE task_key='doc'"
                ).fetchone()
                # attempts=1 -> base backoff 1s, jitter adds at most 25%.
                self.assertGreaterEqual(row['not_before'], before + 1.0)
                self.assertLessEqual(
                    row['not_before'], harvest.time.time() + 1.25)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
