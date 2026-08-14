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
        self.assertEqual(result['portals'], 33)
        rows = portal_registry.load_registry()
        expected_p2 = set(
            'AR FL ID LA MS MT ND NE OR SD WA WY'.split())
        observed = {row['jurisdiction'] for row in rows.values()
                    if row['tier'] == 2}
        # ID/MT are Tier-1 holdings but still satisfy their phase-2 probe.
        self.assertEqual(observed | {'ID', 'MT'}, expected_p2)
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
        self.assertEqual(assay['reason'], 'rights_unverified')
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


if __name__ == '__main__':
    unittest.main()
