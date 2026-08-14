"""Contracts for the WS12 presign endpoint and the citation viewer wiring."""

import importlib.util
import json
import os
import re
import sys
import types
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
INFRA = os.path.join(ROOT, 'infra')
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))
sys.path.insert(0, INFRA)

import doc_store


VIEWER = os.path.join(ROOT, 'site', 'viewer.html')
INDEX = os.path.join(ROOT, 'site', 'index.html')
ASK_LAMBDA = os.path.join(INFRA, 'ask_lambda.py')
MANIFEST = os.path.join(ROOT, 'site', doc_store.MANIFEST_RELATIVE)


class FakeClientError(Exception):
    """Minimal botocore-style exception consumed by docs_lambda."""

    def __init__(self, code):
        self.response = {'Error': {'Code': str(code)}}
        super().__init__(str(code))


class FakeS3:
    """In-memory S3 that records exactly what it was asked to sign."""

    def __init__(self, manifest_bytes):
        self.manifest_bytes = manifest_bytes
        self.signed = []
        self.get_calls = 0
        self.fail_get = False

    def get_object(self, Bucket, Key):
        self.get_calls += 1
        if self.fail_get:
            raise FakeClientError('NoSuchKey')
        return {'Body': _Body(self.manifest_bytes)}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.signed.append({'operation': operation, 'params': Params,
                            'expires_in': ExpiresIn})
        return (f'https://bucket.s3.example.test/{Params["Key"]}'
                f'?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=deadbeef')


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class FakeCognito:
    def __init__(self):
        self.tokens = []
        self.reject = False

    def get_user(self, AccessToken):
        self.tokens.append(AccessToken)
        if self.reject:
            raise FakeClientError('NotAuthorizedException')
        return {'Username': 'codyClinger'}


def _load_docs_lambda(s3, cognito):
    """Import the handler without constructing real AWS clients."""
    fake_boto3 = types.ModuleType('boto3')
    fake_boto3.client = lambda service: {'s3': s3, 'cognito-idp': cognito}[service]
    fake_botocore = types.ModuleType('botocore')
    fake_exceptions = types.ModuleType('botocore.exceptions')
    fake_exceptions.ClientError = FakeClientError
    fake_botocore.exceptions = fake_exceptions
    previous = {name: sys.modules.get(name)
                for name in ('boto3', 'botocore', 'botocore.exceptions')}
    sys.modules['boto3'] = fake_boto3
    sys.modules['botocore'] = fake_botocore
    sys.modules['botocore.exceptions'] = fake_exceptions
    try:
        spec = importlib.util.spec_from_file_location(
            'docs_lambda_under_test', os.path.join(INFRA, 'docs_lambda.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _event(method='GET', headers=None, query=None):
    return {
        'requestContext': {'http': {'method': method}},
        'headers': headers if headers is not None else {'x-auth-token': 'live-token'},
        'queryStringParameters': query or {},
    }


class PresignEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MANIFEST, 'rb') as handle:
            cls.manifest_bytes = handle.read()
        cls.manifest = doc_store.load_manifest(MANIFEST)
        cls.citation = doc_store.citations_for_subject(
            cls.manifest, 'ID', 'stategeo-igs-dd-1-if0126')[0]

    def endpoint(self):
        s3 = FakeS3(self.manifest_bytes)
        cognito = FakeCognito()
        module = _load_docs_lambda(s3, cognito)
        module.BUCKET = 'nw-mineral-monitor-test'
        module.SITE_URL = 'https://d1.cloudfront.example.test'
        module.s3 = s3
        module.cognito = cognito
        module._manifest.update({'at': 0.0, 'value': None})
        return module, s3, cognito

    def body(self, response):
        return json.loads(response['body'])

    def test_a_signed_in_caller_gets_the_searchable_copy_and_its_page(self):
        module, s3, cognito = self.endpoint()
        response = module.handler(_event(query={
            'doc_id': self.citation['doc_id'],
            'page': str(self.citation['page']),
            'quote': self.citation['quote']}), None)
        self.assertEqual(response['statusCode'], 200)
        payload = self.body(response)
        self.assertEqual(payload['page'], self.citation['page'])
        self.assertTrue(payload['quote_located'])
        self.assertEqual(payload['variant'], 'searchable')
        document = next(row for row in self.manifest['documents']
                        if row['doc_id'] == self.citation['doc_id'])
        self.assertEqual(payload['bytes'], document['searchable']['bytes'])
        self.assertEqual(cognito.tokens, ['live-token'])
        self.assertEqual(len(s3.signed), 1)
        self.assertTrue(s3.signed[0]['params']['Key'].endswith('searchable.pdf'))
        self.assertEqual(s3.signed[0]['params']['ResponseContentType'],
                         'application/pdf')
        self.assertEqual(s3.signed[0]['expires_in'], module.PRESIGN_TTL)

    def test_the_signed_url_expires_within_the_manifest_declared_ttl(self):
        module, s3, _ = self.endpoint()
        module.handler(_event(query={'doc_id': self.citation['doc_id']}), None)
        declared = self.manifest['store']['presign_ttl_seconds']
        self.assertLessEqual(s3.signed[0]['expires_in'], declared)
        self.assertLessEqual(s3.signed[0]['expires_in'], 3600)

    def test_an_unauthenticated_caller_gets_no_signature_at_all(self):
        module, s3, _ = self.endpoint()
        response = module.handler(
            _event(headers={}, query={'doc_id': self.citation['doc_id']}), None)
        self.assertEqual(response['statusCode'], 401)
        self.assertEqual(s3.signed, [])

    def test_an_expired_session_gets_no_signature_at_all(self):
        module, s3, cognito = self.endpoint()
        cognito.reject = True
        response = module.handler(
            _event(query={'doc_id': self.citation['doc_id']}), None)
        self.assertEqual(response['statusCode'], 401)
        self.assertEqual(s3.signed, [])

    def test_preflight_is_answered_before_authentication(self):
        module, s3, cognito = self.endpoint()
        response = module.handler(_event(method='OPTIONS', headers={}), None)
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(cognito.tokens, [])
        self.assertEqual(s3.signed, [])

    def test_an_unknown_document_is_a_404_and_never_a_signed_guess(self):
        module, s3, _ = self.endpoint()
        response = module.handler(_event(query={'doc_id': 'a' * 64}), None)
        self.assertEqual(response['statusCode'], 404)
        self.assertEqual(s3.signed, [])

    def test_a_page_past_the_end_is_refused_rather_than_clamped(self):
        module, s3, _ = self.endpoint()
        response = module.handler(_event(query={
            'doc_id': self.citation['doc_id'], 'page': '99999'}), None)
        self.assertEqual(response['statusCode'], 404)
        self.assertIn('past the end', self.body(response)['error'])
        self.assertEqual(s3.signed, [])

    def test_the_raw_original_can_be_signed_for_provenance(self):
        module, s3, _ = self.endpoint()
        response = module.handler(_event(query={
            'doc_id': self.citation['doc_id'], 'variant': 'raw'}), None)
        self.assertEqual(response['statusCode'], 200)
        payload = self.body(response)
        self.assertEqual(payload['variant'], 'raw')
        self.assertEqual(payload['sha256'], payload['doc_id'])
        document = next(row for row in self.manifest['documents']
                        if row['doc_id'] == self.citation['doc_id'])
        self.assertEqual(payload['bytes'], document['raw']['bytes'])
        self.assertTrue(s3.signed[0]['params']['Key'].endswith('raw.pdf'))

    def test_an_unknown_variant_is_refused(self):
        module, s3, _ = self.endpoint()
        response = module.handler(_event(query={
            'doc_id': self.citation['doc_id'], 'variant': 'thumbnail'}), None)
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(s3.signed, [])

    def test_the_viewer_link_carries_no_credential_and_no_query_string(self):
        module, _, _ = self.endpoint()
        response = module.handler(_event(query={
            'doc_id': self.citation['doc_id'],
            'quote': self.citation['quote']}), None)
        viewer = self.body(response)['viewer_url']
        self.assertTrue(viewer.startswith('https://d1.cloudfront.example.test/viewer.html#'))
        self.assertNotIn('?', viewer)
        self.assertNotIn('X-Amz-Signature', viewer)
        self.assertNotIn('live-token', viewer)

    def test_the_authenticated_catalog_omits_storage_internals(self):
        module, _, _ = self.endpoint()
        response = module.handler(_event(query={'catalog': '1'}), None)
        self.assertEqual(response['statusCode'], 200)
        catalog = self.body(response)
        self.assertEqual(catalog['metrics']['documents'], 25)
        document = catalog['documents'][0]
        for private in ('raw', 'searchable', 'rights'):
            self.assertNotIn(private, document)
        self.assertIn('doc_id', document)
        self.assertTrue(catalog['citations'])

    def test_a_manifest_that_does_not_validate_is_never_signed_against(self):
        s3 = FakeS3(b'{"schema_version": 1, "dataset": "ws12-document-store"}')
        cognito = FakeCognito()
        module = _load_docs_lambda(s3, cognito)
        module.BUCKET = 'nw-mineral-monitor-test'
        module.s3, module.cognito = s3, cognito
        module._manifest.update({'at': 0.0, 'value': None})
        response = module.handler(_event(query={'doc_id': 'a' * 64}), None)
        self.assertEqual(response['statusCode'], 500)
        self.assertEqual(s3.signed, [])

    def test_an_unpublished_manifest_reports_unavailable_not_empty(self):
        module, s3, _ = self.endpoint()
        s3.fail_get = True
        response = module.handler(_event(query={'doc_id': 'a' * 64}), None)
        self.assertEqual(response['statusCode'], 503)
        self.assertIn('not published yet', self.body(response)['error'])

    def test_the_manifest_is_cached_across_warm_invocations(self):
        module, s3, _ = self.endpoint()
        for _ in range(3):
            module.handler(_event(query={'doc_id': self.citation['doc_id']}), None)
        self.assertEqual(s3.get_calls, 1)
        self.assertEqual(len(s3.signed), 3)


class ViewerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(VIEWER, encoding='utf-8') as handle:
            cls.viewer = handle.read()
        with open(INDEX, encoding='utf-8') as handle:
            cls.index = handle.read()

    def test_the_viewer_loads_only_vendored_assets(self):
        for asset in ('assets/pdfjs/pdf.min.mjs', 'assets/pdfjs/pdf.worker.min.mjs',
                      'assets/pdfjs/standard_fonts/', 'assets/pdfjs/wasm/'):
            self.assertIn(asset, self.viewer)
            self.assertTrue(os.path.exists(
                os.path.join(ROOT, 'site', asset.rstrip('/'))), asset)
        self.assertNotIn('https://cdn', self.viewer)
        self.assertNotIn('unpkg.com', self.viewer)
        self.assertNotIn('cdnjs', self.viewer)

    def test_scanned_page_decoders_are_vendored(self):
        # Survey scans are JBIG2; without the WASM decoder the page art fails
        # to decode and the reader gets a blank sheet under a working text layer.
        for name in ('jbig2.wasm', 'openjpeg.wasm'):
            self.assertTrue(os.path.exists(
                os.path.join(ROOT, 'site', 'assets', 'pdfjs', 'wasm', name)), name)
        self.assertIn('wasmUrl', self.viewer)

    def test_the_viewer_reads_its_parameters_from_the_fragment(self):
        self.assertIn('location.hash', self.viewer)
        self.assertIn("q.get('doc')", self.viewer)
        self.assertIn("q.get('page')", self.viewer)

    def test_the_viewer_never_opens_the_portal_url(self):
        self.assertIn('getDocument({', self.viewer)
        self.assertNotIn('getDocument({url: d.source_url', self.viewer)
        self.assertIn('this viewer reads our stored copy', self.viewer)

    def test_the_viewer_is_mobile_safe(self):
        self.assertIn('width=device-width', self.viewer)
        self.assertIn('@media (max-width:900px)', self.viewer)
        self.assertIn('viewport-fit=cover', self.viewer)
        self.assertIn('recoverSignedRead', self.viewer)
        self.assertIn('S.resignAttempts < 1', self.viewer)
        self.assertIn('Refreshing the private document link', self.viewer)

    def test_the_viewer_is_not_indexable(self):
        self.assertIn('noindex', self.viewer)

    def test_the_map_registers_open_doc_and_renders_citation_chips(self):
        self.assertIn("if (name==='open_doc') return execOpenDoc(a);", self.index)
        self.assertIn('function execOpenDoc', self.index)
        self.assertIn('function openDoc', self.index)
        self.assertIn('function docChip', self.index)
        self.assertIn('function docChipForCitation', self.index)
        self.assertIn('rememberDocQuote(out.doc_id, out.page, out.quote)', self.index)
        self.assertIn('data-quote="${esc(q)}"', self.index)
        self.assertIn('this.dataset.quote', self.index)
        self.assertIn('docEncodeQuote(quote)', self.index)
        self.assertIn('docDecodeQuote(quoteToken)', self.index)
        self.assertIn('viewer.html#', self.index)
        self.assertIn("url.searchParams.set('catalog','1')", self.index)
        self.assertNotIn("jget('data/docs/manifest.json')", self.index)

    def test_the_quote_never_enters_the_docs_api_request_line(self):
        request = self.viewer.split('async function resolve()', 1)[1].split(
            'const token = accessToken()', 1)[0]
        self.assertNotIn("url.searchParams.set('quote'", request)
        self.assertIn('S.quote = p.quote', request)

    def test_a_chip_click_carries_the_quote_into_the_viewer_fragment(self):
        # Merely pinning the page is insufficient: the addendum requires the
        # cited quote to be pre-searched and highlighted in PDF.js.
        self.assertIn(
            'const q = quote || (c && c.quote) || docQuoteFor(', self.index)
        self.assertIn("if (q) params.set('q', q);", self.index)
        self.assertNotIn(
            "onclick=\"openDoc('${docId}',${n})\"", self.index)

    def test_highlight_status_comes_from_the_rendered_text_layer(self):
        self.assertIn('const highlighted = highlight(layer);', self.viewer)
        self.assertIn("? 'highlighted in the text layer'", self.viewer)
        self.assertIn('return marked;', self.viewer)
        self.assertIn('searching this stored page', self.viewer)

    def test_stored_http_citations_are_deterministically_upgraded_to_chips(self):
        # search_documents still returns the publisher URL for provenance.
        # The browser must recognize a stored title/page/URL citation even if
        # the model omitted a separate open_doc round.
        self.assertIn('docChipForCitation(label, url)', self.index)
        self.assertIn('DOCS.byUrl', self.index)
        self.assertIn('DOCS.byDocPage', self.index)
        self.assertIn('rememberSearchDocumentHit(hit)', self.index)
        self.assertIn('DOC_SEARCH_CITATIONS[docSearchKey(', self.index)

    def test_the_ask_relay_and_the_browser_agree_on_the_tool_name(self):
        with open(ASK_LAMBDA, encoding='utf-8') as handle:
            relay = handle.read()
        declared = set(re.findall(r'\{"toolSpec": \{"name": "([a-z_]+)"', relay))
        self.assertIn('open_doc', declared,
                      'the relay must offer open_doc or citation chips never appear')
        # Every tool the relay offers must have a browser executor; the two
        # files are edited independently and nothing else catches a mismatch.
        for name in sorted(declared):
            self.assertTrue(
                f"name==='{name}'" in self.index or f"'{name}'" in self.index,
                f'{name} is offered by the relay but has no browser executor')

    def test_the_doc_citation_chip_outranks_the_outbound_link_rule(self):
        chip = self.index.index(r'\]\(doc:')
        link = self.index.index(r'\]\((https?:')
        self.assertLess(chip, link,
                        'a doc: citation must be converted before the http rule '
                        'can render it as an ordinary outbound link')


if __name__ == '__main__':
    unittest.main()
