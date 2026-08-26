"""Contracts for the WS13 two-shape document resolver in infra/docs_lambda.py.

The defects these pin down, in the order they would bite:

  * ws13_documents.searchable_key is populated for all 28,988 ocr_queue
    documents and NULL for all 27,294 born_digital ones. A presign endpoint
    that only knows the ws13/searchable/ shape cannot open half the corpus, and
    one that guesses a searchable key for a born-digital document signs a URL
    that resolves to an S3 error body the viewer renders as a broken PDF.
  * admission_class is GENERATED ALWAYS AS (split_part(s3_key, '/', 2)) STORED,
    so the ws12/{class}/ prefix IS the rights signal. Reading the class off the
    request instead of off the key would let a caller relabel one of the 13,013
    CC BY-NC-SA copies or 32,312 research copies as a public-domain original
    and strip the attribution that makes serving it defensible.
  * A presigned URL can never grant more than the role holds, and DocsRole is
    scoped to named prefixes — so the key must be constrained to those same two
    shapes here, with an anchored pattern, or the endpoint becomes "presign
    anything in this bucket" for any signed-in caller.
  * DocsRole holds s3:GetObject and NOT s3:ListBucket, so S3 answers a HEAD of
    an absent key with 403 AccessDenied rather than 404. Treating only 404 as
    "absent" turns every missing object into a 502.
  * The rights vocabulary is duplicated between the two Lambdas because they
    ship as separate zips. Drift there tells a reader two different licences
    for one document, so the tables are compared directly.

Nothing here touches the network, AWS or a database: boto3 and botocore are
stubbed as module stubs the way tests/test_ws13_embed_backfill.py does it, and
the fake S3 records exactly which keys were probed and which were signed.
"""

import importlib.util
import json
import os
import re
import sys
import types
import unittest
import urllib.parse
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
INFRA = os.path.join(ROOT, 'infra')
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))
sys.path.insert(0, INFRA)

TEMPLATE = os.path.join(INFRA, 'template.yaml')
VIEWER = os.path.join(ROOT, 'site', 'viewer.html')

# psycopg is a deployment dependency of the retrieval Lambda, not of this test
# host. ws13_query_lambda degrades to psycopg = None when it is absent; bind a
# stub so importing it for the rights-table comparison cannot reach a driver.
if 'psycopg' not in sys.modules:
    try:
        import psycopg                                  # noqa: F401
    except ImportError:
        _stub = types.ModuleType('psycopg')
        _stub.connect = mock.MagicMock()
        sys.modules['psycopg'] = _stub

import ws13_query_lambda as ql


# Two real documents, one from each half of the corpus. The OCR document's
# original is the deep harvest shape ws12/originals/{portal}/{sha[:2]}/
# {sha}.pdf; the born-digital one is the flat archive shape
# ws12/research-copies/{name}.pdf, so the resolver is exercised against both
# depths rather than one template.
OCR_SHA = '3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1'
BORN_SHA = 'd29aab7b4e9fcde0e084dddc84ef9da37d0c15860af4674bf58bd0decd71e07f'
LICENSED_SHA = 'ab' * 32
OCR_ORIGINAL = f'ws12/originals/igs_mines/3c/{OCR_SHA}.pdf'
OCR_SEARCHABLE = f'ws13/searchable/3c/{OCR_SHA}/searchable.pdf'
BORN_ORIGINAL = 'ws12/research-copies/IF0131_001.pdf'
LICENSED_ORIGINAL = f'ws12/licensed-copies/azgs/ab/{LICENSED_SHA}.pdf'
RESEARCH_BASIS = 'Idaho Geological Survey mine file, reproduced for research'
LICENSED_BASIS = 'Arizona Geological Survey document repository'


class FakeClientError(Exception):
    """Minimal botocore-style exception, shaped like the one docs_lambda reads."""

    def __init__(self, code):
        self.response = {'Error': {'Code': str(code)}}
        super().__init__(str(code))


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class FakeS3:
    """In-memory S3 that records every key probed and every key signed."""

    def __init__(self, objects=None, manifest_bytes=b'{}'):
        self.objects = dict(objects or {})
        self.manifest_bytes = manifest_bytes
        self.heads = []
        self.signed = []
        self.get_calls = 0
        # A HEAD of an absent key answers 403 for a role without
        # s3:ListBucket; 404 is what a role that can list would see. Both are
        # exercised.
        self.missing_code = '403'

    def get_object(self, Bucket, Key):
        self.get_calls += 1
        return {'Body': _Body(self.manifest_bytes)}

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if Key not in self.objects:
            raise FakeClientError(self.missing_code)
        return {'ContentLength': self.objects[Key]}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.signed.append({'operation': operation, 'params': Params,
                            'expires_in': ExpiresIn})
        return (f'https://bucket.s3.example.test/{Params["Key"]}'
                f'?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=deadbeef')


class FakeCognito:
    def __init__(self):
        self.tokens = []
        self.reject = False

    def get_user(self, AccessToken):
        self.tokens.append(AccessToken)
        if self.reject:
            raise FakeClientError('NotAuthorizedException')
        return {'Username': 'codyClinger'}


def load_docs_lambda(s3, cognito):
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
            'ws13_docs_lambda_under_test', os.path.join(INFRA, 'docs_lambda.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def event(query, headers=None, method='GET'):
    return {
        'requestContext': {'http': {'method': method}},
        'headers': headers if headers is not None else {'x-auth-token': 'live-token'},
        'queryStringParameters': query,
    }


def ws13_query(**overrides):
    """A WS13 presign request, defaulted to the OCR document."""
    query = {'corpus': 'ws13', 'doc_id': OCR_SHA, 's3_key': OCR_ORIGINAL,
             'viewer_key_kind': 'searchable', 'page': '7'}
    query.update(overrides)
    return {key: value for key, value in query.items() if value is not None}


class Ws13ResolverTests(unittest.TestCase):
    def endpoint(self, objects=None):
        if objects is None:
            objects = {OCR_SEARCHABLE: 220_113, OCR_ORIGINAL: 110_393,
                       BORN_ORIGINAL: 87_004, LICENSED_ORIGINAL: 44_100}
        s3 = FakeS3(objects)
        cognito = FakeCognito()
        module = load_docs_lambda(s3, cognito)
        module.BUCKET = 'nw-mineral-monitor-test'
        module.SITE_URL = 'https://d1.cloudfront.example.test'
        module.s3 = s3
        module.cognito = cognito
        module._manifest.update({'at': 0.0, 'value': None})
        return module, s3, cognito

    def body(self, response):
        return json.loads(response['body'])

    def call(self, query, objects=None, headers=None):
        module, s3, cognito = self.endpoint(objects)
        response = module.handler(event(query, headers=headers), None)
        return module, s3, response

    # --- the two shapes ----------------------------------------------------

    def test_an_ocr_document_resolves_to_its_ws13_searchable_copy(self):
        module, s3, response = self.call(ws13_query())
        self.assertEqual(response['statusCode'], 200)
        payload = self.body(response)
        self.assertEqual(payload['viewer_key_kind'], 'searchable')
        self.assertEqual(payload['text_layer'], 'ocr_added')
        self.assertEqual(payload['page'], 7)
        self.assertEqual(payload['bytes'], 220_113)
        self.assertEqual(len(s3.signed), 1)
        self.assertEqual(s3.signed[0]['params']['Key'], OCR_SEARCHABLE)
        self.assertEqual(s3.signed[0]['params']['ResponseContentType'],
                         'application/pdf')
        self.assertEqual(s3.signed[0]['expires_in'], module.PRESIGN_TTL)
        # The shape was verified against the object store, not inferred from
        # the request: the key that was signed is the key that answered a HEAD.
        self.assertEqual(s3.heads, [OCR_SEARCHABLE])

    def test_a_born_digital_document_resolves_to_its_ws12_original(self):
        module, s3, response = self.call(ws13_query(
            doc_id=BORN_SHA, s3_key=BORN_ORIGINAL,
            viewer_key_kind='born_digital_original',
            rights_basis=RESEARCH_BASIS))
        self.assertEqual(response['statusCode'], 200)
        payload = self.body(response)
        self.assertEqual(payload['viewer_key_kind'], 'born_digital_original')
        # A born-digital original already carries its publisher's text layer,
        # so the viewer may search and highlight it.
        self.assertEqual(payload['text_layer'], 'native')
        self.assertEqual(payload['bytes'], 87_004)
        self.assertEqual(s3.signed[0]['params']['Key'], BORN_ORIGINAL)
        # The searchable copy is probed first and is absent; nothing under
        # ws13/ was signed for a document that has no OCR output.
        self.assertEqual(
            s3.heads,
            [f'ws13/searchable/d2/{BORN_SHA}/searchable.pdf', BORN_ORIGINAL])

    def test_the_searchable_copy_is_preferred_only_when_it_really_exists(self):
        # The caller's kind is a hint, not the decision: a document whose OCR
        # output is present is served from it even when the request says the
        # original is the viewer, because the hint cannot be verified and the
        # object can.
        module, s3, response = self.call(ws13_query(
            viewer_key_kind='born_digital_original'))
        self.assertEqual(self.body(response)['viewer_key_kind'], 'searchable')
        self.assertEqual(s3.signed[0]['params']['Key'], OCR_SEARCHABLE)

    def test_the_kind_hint_can_only_weaken_the_text_layer_claim(self):
        # An OCR document whose searchable copy has not been written yet is
        # served from the raster original, and the response must NOT promise a
        # text layer: the viewer would offer text search over page images and
        # silently find nothing.
        objects = {OCR_ORIGINAL: 110_393}
        module, s3, response = self.call(ws13_query(), objects=objects)
        self.assertEqual(response['statusCode'], 200)
        payload = self.body(response)
        self.assertEqual(payload['viewer_key_kind'],
                         'scanned_original_no_text_layer')
        self.assertEqual(payload['text_layer'], 'absent')
        self.assertEqual(s3.signed[0]['params']['Key'], OCR_ORIGINAL)

    def test_a_request_with_no_kind_hint_takes_the_weaker_claim(self):
        objects = {OCR_ORIGINAL: 110_393}
        module, s3, response = self.call(
            ws13_query(viewer_key_kind=None), objects=objects)
        self.assertEqual(self.body(response)['viewer_key_kind'],
                         'scanned_original_no_text_layer')

    def test_variant_raw_serves_the_stored_original_not_the_ocr_copy(self):
        # 'raw' means the same thing it means for the legacy corpus: the
        # original as harvested. Silently returning the OCR copy would make the
        # provenance view a different file from the one whose digest is the
        # document id.
        module, s3, response = self.call(ws13_query(variant='raw'))
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(s3.signed[0]['params']['Key'], OCR_ORIGINAL)
        self.assertEqual(s3.heads, [OCR_ORIGINAL])

    # --- key validation ----------------------------------------------------

    def test_a_key_outside_the_two_prefixes_is_refused(self):
        outside = [
            'private/ws12/document-store-manifest.json',
            'ws12/deploy/document-index.sqlite3',
            'docs/ID/igs/stategeo-igs-dd-1-if0126/' + OCR_SHA + '/searchable.pdf',
            'data/docs/manifest.json',
            'index.html',
            '',
        ]
        for key in outside:
            with self.subTest(key=key):
                module, s3, response = self.call(ws13_query(s3_key=key))
                self.assertEqual(response['statusCode'], 400)
                self.assertEqual(s3.signed, [])
                self.assertEqual(s3.heads, [])

    def test_traversal_and_prefix_lookalikes_are_refused(self):
        traversal = [
            'ws12/originals/../deploy/document-index.sqlite3',
            'ws12/originals/igs_mines/../../deploy/document-index.sqlite3',
            'ws12/originals/./' + OCR_SHA + '.pdf',
            '/ws12/originals/igs_mines/3c/' + OCR_SHA + '.pdf',
            'ws12//originals/igs_mines/3c/' + OCR_SHA + '.pdf',
            # A valid prefix that is only a substring of the key.
            '../../ws12/originals/igs_mines/3c/' + OCR_SHA + '.pdf',
            'x-ws12/originals/igs_mines/3c/' + OCR_SHA + '.pdf',
            # A valid prefix that is only a prefix of the CLASS segment.
            'ws12/originals-public/igs_mines/3c/' + OCR_SHA + '.pdf',
            'ws12/research-copies-draft/IF0131_001.pdf',
            # An embedded newline: surrounding whitespace is stripped like
            # every other parameter, but a key cannot be two keys.
            'ws12/originals/igs_mines/3c/\n' + OCR_SHA + '.pdf',
            'ws12/originals/igs_mines/3c/' + OCR_SHA + '.pdf\nws12/deploy/x',
            'ws12/originals/%2e%2e/deploy/document-index.sqlite3',
        ]
        for key in traversal:
            with self.subTest(key=key):
                module, s3, response = self.call(ws13_query(s3_key=key))
                self.assertEqual(response['statusCode'], 400)
                self.assertEqual(s3.signed, [])
                self.assertEqual(s3.heads, [])

    def test_a_viewer_key_from_another_document_is_refused(self):
        # A mismatched pair of citation fields must fail rather than be
        # quietly ignored: the two fields disagreeing means one of them is
        # about a different document.
        other = f'ws13/searchable/d2/{BORN_SHA}/searchable.pdf'
        module, s3, response = self.call(ws13_query(viewer_key=other))
        self.assertEqual(response['statusCode'], 400)
        self.assertIn('viewer_key', self.body(response)['error'])
        self.assertEqual(s3.signed, [])

    def test_the_ocr_sidecar_text_is_not_signable(self):
        # ws13_worker.py writes sidecar.txt beside every searchable copy. It is
        # extracted text, not a page image, and no request may name it.
        sidecar = f'ws13/searchable/3c/{OCR_SHA}/sidecar.txt'
        module, s3, response = self.call(ws13_query(viewer_key=sidecar))
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(s3.signed, [])

    def test_a_digest_named_key_must_name_the_requested_document(self):
        # The searchable copy is keyed by doc_id while the rights class is read
        # off the ws12 key, so a mismatched pair would serve one document's OCR
        # text labelled with another document's licence.
        module, s3, response = self.call(ws13_query(
            s3_key=f'ws12/licensed-copies/azgs/ab/{LICENSED_SHA}.pdf',
            rights_basis=LICENSED_BASIS))
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(s3.signed, [])
        self.assertEqual(s3.heads, [])

    def test_a_flat_archive_key_names_no_digest_and_is_still_accepted(self):
        # ws12/research-copies/IF0131_001.pdf carries no digest to compare, so
        # the check above must be conditional and not a key template.
        module, s3, response = self.call(ws13_query(
            doc_id=BORN_SHA, s3_key=BORN_ORIGINAL,
            viewer_key_kind='born_digital_original',
            rights_basis=RESEARCH_BASIS))
        self.assertEqual(response['statusCode'], 200)

    def test_a_matching_viewer_key_is_accepted(self):
        module, s3, response = self.call(ws13_query(viewer_key=OCR_SEARCHABLE))
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(s3.signed[0]['params']['Key'], OCR_SEARCHABLE)

    def test_a_partial_doc_id_is_refused_rather_than_matched(self):
        # The legacy corpus resolves an 8-character prefix against its
        # manifest. This corpus has no such index in reach, so a prefix would
        # be a guess across 56,282 documents.
        for doc_id in (OCR_SHA[:12], OCR_SHA.upper()[:12], 'not-a-digest', ''):
            with self.subTest(doc_id=doc_id):
                module, s3, response = self.call(ws13_query(doc_id=doc_id))
                self.assertEqual(response['statusCode'], 400)
                self.assertEqual(s3.signed, [])

    def test_an_uppercase_digest_is_normalised_not_refused(self):
        module, s3, response = self.call(ws13_query(doc_id=OCR_SHA.upper()))
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(self.body(response)['doc_id'], OCR_SHA)

    def test_only_the_two_shapes_can_reach_a_signature(self):
        # The chokepoint in front of presign(), driven by a resolver that has
        # been made to return a key nothing validated. This is the guarantee
        # that keeps a future edit from turning the endpoint into "presign
        # anything in this bucket".
        module, s3, _ = self.endpoint()
        smuggled = {'key': 'private/ws12/document-store-manifest.json',
                    'kind': 'searchable', 'bytes': 10}
        with mock.patch.object(module, 'resolve_ws13_doc',
                               return_value=smuggled):
            response = module.handler(event(ws13_query()), None)
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(s3.signed, [])

    # --- rights ------------------------------------------------------------

    def test_the_rights_fields_survive_into_the_response(self):
        module, s3, response = self.call(ws13_query(
            doc_id=BORN_SHA, s3_key=BORN_ORIGINAL,
            viewer_key_kind='born_digital_original',
            rights_basis=RESEARCH_BASIS))
        payload = self.body(response)
        self.assertEqual(payload['admission_class'], 'research-copies')
        self.assertEqual(payload['rights_basis'], RESEARCH_BASIS)
        self.assertIn(RESEARCH_BASIS, payload['rights_terms'])
        self.assertTrue(payload['attribution_required'])
        self.assertTrue(payload['non_commercial'])
        self.assertFalse(payload['share_alike'])

    def test_a_licensed_copy_carries_its_share_alike_licence(self):
        module, s3, response = self.call(ws13_query(
            doc_id=LICENSED_SHA, s3_key=LICENSED_ORIGINAL,
            viewer_key_kind='born_digital_original',
            rights_basis=LICENSED_BASIS))
        payload = self.body(response)
        self.assertEqual(payload['admission_class'], 'licensed-copies')
        self.assertIn('CC BY-NC-SA', payload['rights_terms'])
        self.assertIn(LICENSED_BASIS, payload['rights_terms'])
        self.assertTrue(payload['share_alike'])

    def test_a_public_domain_original_needs_no_basis(self):
        payload = self.body(self.call(ws13_query())[2])
        self.assertEqual(payload['admission_class'], 'originals')
        self.assertIsNone(payload['rights_basis'])
        self.assertFalse(payload['attribution_required'])

    def test_a_licensed_copy_without_its_basis_is_never_signed(self):
        for key in (LICENSED_ORIGINAL, BORN_ORIGINAL):
            with self.subTest(key=key):
                module, s3, response = self.call(ws13_query(
                    doc_id=LICENSED_SHA, s3_key=key, rights_basis=None,
                    viewer_key_kind='born_digital_original'))
                self.assertEqual(response['statusCode'], 403)
                self.assertIn('rights_basis', self.body(response)['error'])
                self.assertEqual(s3.signed, [])
                self.assertEqual(s3.heads, [])

    def test_admission_class_is_read_off_the_key_not_off_the_request(self):
        # The prefix is the rights signal, so a request that claims a different
        # class is a contradiction and is refused rather than resolved in
        # either direction.
        module, s3, response = self.call(ws13_query(
            doc_id=LICENSED_SHA, s3_key=LICENSED_ORIGINAL,
            admission_class='originals', rights_basis=LICENSED_BASIS))
        self.assertEqual(response['statusCode'], 400)
        self.assertIn('admission_class', self.body(response)['error'])
        self.assertEqual(s3.signed, [])

    def test_an_agreeing_admission_class_is_accepted(self):
        module, s3, response = self.call(ws13_query(
            doc_id=LICENSED_SHA, s3_key=LICENSED_ORIGINAL,
            admission_class='licensed-copies', rights_basis=LICENSED_BASIS,
            viewer_key_kind='born_digital_original'))
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(self.body(response)['admission_class'],
                         'licensed-copies')

    def test_the_rights_table_matches_the_retrieval_lambda(self):
        # The two Lambdas ship as separate zips, so this table is duplicated
        # rather than imported. Drift would print one licence in a search
        # result and a different one in the viewer for the same document.
        module, _, _ = self.endpoint()
        self.assertEqual(module.RIGHTS_BY_CLASS, ql.RIGHTS_BY_CLASS)
        self.assertEqual(tuple(sorted(module.RIGHTS_BY_CLASS)),
                         tuple(sorted(ql.ADMISSION_CLASSES)))
        for admission_class in ql.ADMISSION_CLASSES:
            basis = 'a named licensor'
            self.assertEqual(module.rights_for(admission_class, basis),
                             ql.rights_for(admission_class, basis))

    def test_the_viewer_key_kind_vocabulary_matches_the_retrieval_lambda(self):
        # Every value citation_for() can emit must be one this endpoint accepts
        # back, or the viewer would hand in a word the presign call rejects.
        module, _, _ = self.endpoint()
        self.assertEqual(sorted(module.VIEWER_KEY_KINDS),
                         sorted(module.TEXT_LAYER_BY_KIND))
        with open(os.path.join(INFRA, 'ws13_query_lambda.py'),
                  encoding='utf-8') as handle:
            emitted = set(re.findall(r'viewer_key_kind = "([a-z_]+)"',
                                     handle.read()))
        self.assertTrue(emitted)
        self.assertTrue(emitted.issubset(set(module.VIEWER_KEY_KINDS)))

    def test_every_text_layer_claim_is_one_doc_store_defines(self):
        module, _, _ = self.endpoint()
        for status in module.TEXT_LAYER_BY_KIND.values():
            self.assertIn(status, module.doc_store.TEXT_LAYER_STATUSES)

    # --- failing closed ----------------------------------------------------

    def test_a_document_stored_under_neither_shape_fails_closed(self):
        module, s3, response = self.call(ws13_query(), objects={})
        self.assertEqual(response['statusCode'], 404)
        self.assertIn('neither shape', self.body(response)['error'])
        self.assertEqual(s3.signed, [])
        self.assertEqual(s3.heads, [OCR_SEARCHABLE, OCR_ORIGINAL])

    def test_a_head_denied_for_want_of_list_bucket_reads_as_absent(self):
        # DocsRole holds s3:GetObject and not s3:ListBucket, so S3 answers a
        # HEAD of a key that is not there with 403, never 404. Both codes have
        # to mean the same thing here or every missing object becomes a 502.
        for code in ('403', 'AccessDenied', '404', 'NoSuchKey', 'NotFound'):
            with self.subTest(code=code):
                module, s3, _ = self.endpoint(objects={OCR_ORIGINAL: 110_393})
                s3.missing_code = code
                response = module.handler(event(ws13_query()), None)
                self.assertEqual(response['statusCode'], 200)
                self.assertEqual(s3.signed[0]['params']['Key'], OCR_ORIGINAL)

    def test_an_unexpected_s3_error_is_reported_not_swallowed(self):
        module, s3, _ = self.endpoint(objects={})
        s3.missing_code = 'InternalError'
        response = module.handler(event(ws13_query()), None)
        self.assertEqual(response['statusCode'], 502)
        self.assertEqual(s3.signed, [])

    def test_an_unauthenticated_ws13_request_gets_no_signature_at_all(self):
        module, s3, response = self.call(ws13_query(), headers={})
        self.assertEqual(response['statusCode'], 401)
        self.assertEqual(s3.signed, [])
        self.assertEqual(s3.heads, [])

    def test_an_expired_session_gets_no_signature_at_all(self):
        module, s3, cognito = self.endpoint()
        cognito.reject = True
        response = module.handler(event(ws13_query()), None)
        self.assertEqual(response['statusCode'], 401)
        self.assertEqual(s3.signed, [])

    def test_an_unknown_corpus_is_refused(self):
        module, s3, response = self.call(ws13_query(corpus='ws14'))
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(s3.signed, [])

    def test_an_unknown_viewer_key_kind_is_refused(self):
        module, s3, response = self.call(ws13_query(viewer_key_kind='text'))
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(s3.signed, [])

    def test_a_page_outside_a_citation_chip_is_refused_not_clamped(self):
        for page in ('0', '-3', '100000', 'seven'):
            with self.subTest(page=page):
                module, s3, response = self.call(ws13_query(page=page))
                self.assertEqual(response['statusCode'], 400)
                self.assertEqual(s3.signed, [])

    # --- properties the legacy endpoint already had ------------------------

    def test_the_short_ttl_is_preserved_for_both_corpora(self):
        module, s3, response = self.call(ws13_query())
        self.assertEqual(module.PRESIGN_TTL, 300)
        self.assertEqual(module.MANIFEST_TTL, 300)
        self.assertLessEqual(module.PRESIGN_TTL, 3600)
        self.assertEqual(self.body(response)['expires_in'], 300)
        self.assertEqual(s3.signed[0]['expires_in'], 300)

    def test_the_ws13_path_never_reads_the_ws12_manifest(self):
        # 56,282 WS13 documents must not become unopenable because one JSON
        # object in the legacy store is unpublished or failed validation.
        module, s3, response = self.call(ws13_query())
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(s3.get_calls, 0)

    def test_the_viewer_link_carries_no_credential_and_no_query_string(self):
        module, s3, response = self.call(ws13_query(quote='assay returns'))
        viewer = self.body(response)['viewer_url']
        self.assertTrue(viewer.startswith(
            'https://d1.cloudfront.example.test/viewer.html#'))
        self.assertNotIn('?', viewer)
        self.assertNotIn('X-Amz-Signature', viewer)
        self.assertNotIn('live-token', viewer)
        self.assertIn('corpus=ws13', viewer)

    def test_a_quote_is_carried_and_bounded(self):
        module, s3, response = self.call(ws13_query(quote='x' * 5000))
        payload = self.body(response)
        self.assertEqual(len(payload['quote']), module.MAX_QUOTE)

    def test_the_legacy_corpus_is_still_the_default(self):
        # No corpus parameter must keep resolving through the WS12 manifest,
        # which this fixture does not publish; a WS13 answer here would mean
        # the branch had swallowed the legacy path.
        module, s3, _ = self.endpoint()
        response = module.handler(event({'doc_id': OCR_SHA}), None)
        self.assertIn(response['statusCode'], (500, 503))
        self.assertEqual(s3.signed, [])


class Ws13ViewerTests(unittest.TestCase):
    """site/viewer.html is the client of this resolver, and was not one.

    Until now the viewer resolved every document through the WS12 manifest, so
    a WS13 citation rendered as inert cited text carrying its rights badge --
    a provable citation with nothing to open. The viewer now speaks both
    corpora; these tests hold the two halves of that contract together, which
    is where it can silently come apart: the viewer sends the fields the
    resolver requires, and it renders the fields the resolver returns.
    """

    @classmethod
    def setUpClass(cls):
        with open(VIEWER, encoding='utf-8') as handle:
            cls.viewer = handle.read()
        cls.resolve = cls.viewer.split('async function resolve()', 1)[1].split(
            'function paintChrome', 1)[0]

    def ws13_body(self, **overrides):
        module, _s3, cognito = Ws13ResolverTests.endpoint(self)
        response = module.handler(event(ws13_query(**overrides)), None)
        self.assertEqual(response['statusCode'], 200, response['body'])
        return json.loads(response['body'])

    def test_the_viewer_asks_for_the_ws13_corpus_by_name(self):
        self.assertIn("q.get('corpus')", self.viewer)
        self.assertIn("url.searchParams.set('corpus', WS13)", self.resolve)

    def test_it_sends_the_two_fields_this_resolver_cannot_do_without(self):
        # s3_key names the stored original AND carries the rights class in its
        # ws12/{class}/ prefix; rights_basis names the licence. A request
        # missing the key is refused by validate_ws13_request().
        self.assertIn("url.searchParams.set('s3_key', p.s3Key)", self.resolve)
        self.assertIn("url.searchParams.set('rights_basis', p.rightsBasis)",
                      self.resolve)
        module, _s3, cognito = Ws13ResolverTests.endpoint(self)
        response = module.handler(event(ws13_query(s3_key=None)), None)
        self.assertEqual(response['statusCode'], 400)
        self.assertIn('s3_key is required', json.loads(response['body'])['error'])

    def test_a_link_without_the_key_fails_in_the_viewer_before_the_request(self):
        # Sending the request anyway would spend a round trip to be told what
        # the link already shows, and the message would be about a parameter
        # the reader never typed.
        self.assertIn('is missing its <code>s3_key</code>', self.viewer)
        self.assertIn('if (!p.s3Key)', self.resolve)

    def test_the_quote_still_never_enters_the_request_line(self):
        # Same rule as the legacy corpus: the quote is matched client-side and
        # must not reach API Gateway access logs.
        self.assertNotIn("searchParams.set('quote'", self.resolve)
        self.assertNotIn("searchParams.set('q'", self.resolve)

    def test_every_rights_field_the_resolver_returns_is_rendered(self):
        # Serving 13,013 CC BY-NC-SA copies and 32,312 archive research copies
        # is defensible only if the terms are printed beside the page. A field
        # this response carries and the viewer drops would imply public domain
        # by silence.
        body = self.ws13_body(s3_key=LICENSED_ORIGINAL, doc_id=LICENSED_SHA,
                              rights_basis=LICENSED_BASIS,
                              viewer_key_kind='born_digital_original')
        chrome = self.viewer.split('function ws13Provenance', 1)[1].split(
            '\n}', 1)[0]
        for field in ('rights_terms', 'attribution_required', 'non_commercial',
                      'share_alike'):
            with self.subTest(field=field):
                self.assertIn(field, body)
                self.assertIn(f'd.{field}', chrome)
        self.assertTrue(body['non_commercial'])
        self.assertTrue(body['share_alike'])

    def test_every_response_field_the_viewer_reads_is_one_this_api_returns(self):
        # The failure this catches is a viewer printing 'undefined' where a
        # licence should be. Every d.<field> the WS13 chrome reads has to exist
        # in a real response from this handler.
        body = self.ws13_body()
        chrome = self.viewer.split('function ws13Provenance', 1)[1].split(
            '\n}', 1)[0]
        read = set(re.findall(r'\bd\.([a-z0-9_]+)', chrome))
        self.assertTrue(read)
        self.assertEqual(sorted(read - set(body)), [])

    def test_it_does_not_expect_the_ws12_manifest_fields(self):
        # This response carries no title, authority, page count or catalog URL
        # -- the WS13 index is in a Postgres this API cannot read -- so the
        # WS13 chrome must not reach for them.
        body = self.ws13_body()
        for field in ('title', 'authority', 'state', 'pages', 'catalog_url',
                      'retrieved', 'page_cite'):
            self.assertNotIn(field, body)
        chrome = self.viewer.split('function ws13Provenance', 1)[1].split(
            '\n}', 1)[0]
        for field in ('d.title', 'd.authority', 'd.pages', 'd.catalog_url',
                      'd.retrieved', 'd.page_cite'):
            with self.subTest(field=field):
                self.assertNotIn(field, chrome)

    def test_the_end_of_document_check_this_api_delegates_is_implemented(self):
        # validate_ws13_request() bounds the page to MAX_PAGE and says why it
        # cannot do more: no page count is reachable from that function, so
        # the real end-of-document check is the viewer's. Without it a citation
        # past the end renders as a failed read and one wasted re-signature.
        module, _s3, _cognito = Ws13ResolverTests.endpoint(self)
        # Proof that the check cannot live here: a page far past any real
        # document is signed, because nothing on this path knows the count.
        response = module.handler(event(ws13_query(page='9000')), None)
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body'])['page'], 9000)
        self.assertIn('function notePageCount', self.viewer)
        self.assertIn('S.pdf.numPages', self.viewer)
        self.assertIn('is past the end of this', self.viewer)
        self.assertIn('the citation does not resolve here', self.viewer)

    def test_a_page_past_the_bound_is_still_refused_by_the_api(self):
        module, _s3, _cognito = Ws13ResolverTests.endpoint(self)
        response = module.handler(
            event(ws13_query(page=str(module.MAX_PAGE + 1))), None)
        self.assertEqual(response['statusCode'], 400)

    def test_the_legacy_corpus_remains_the_viewers_default(self):
        # A link with no corpus must keep resolving through the WS12 manifest;
        # 56,282 WS13 documents becoming the default would break every
        # existing citation.
        self.assertIn("const corpus = (q.get('corpus') || 'ws12').trim()",
                      self.viewer)
        self.assertIn("corpus === WS13 ? WS13 : 'ws12'", self.viewer)

    def test_the_viewer_link_this_api_mints_is_one_the_viewer_can_read(self):
        # ws13_viewer_url() and params() are the two ends of one fragment.
        body = self.ws13_body(quote='assay returns')
        fragment = body['viewer_url'].split('#', 1)[1]
        sent = dict(urllib.parse.parse_qsl(fragment, keep_blank_values=True))
        for key in sent:
            with self.subTest(param=key):
                self.assertIn(f"q.get('{key}')", self.viewer)


class DocsRolePolicyTests(unittest.TestCase):
    """The role is the ceiling: a presigned URL never grants more than it."""

    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE, encoding='utf-8') as handle:
            cls.template = handle.read()
        head = cls.template.split('  DocsRole:', 1)[1]
        cls.policy = head.split('  DocsFunction:', 1)[0]

    def test_the_role_names_both_ws13_shapes(self):
        for resource in (
                "${SiteBucket.Arn}/ws13/searchable/*/*/searchable.pdf",
                "${SiteBucket.Arn}/ws12/originals/*.pdf",
                "${SiteBucket.Arn}/ws12/licensed-copies/*.pdf",
                "${SiteBucket.Arn}/ws12/research-copies/*.pdf"):
            with self.subTest(resource=resource):
                self.assertIn(resource, self.policy)

    def test_the_role_is_not_widened_to_the_bucket_or_a_bare_prefix(self):
        # ws12/ also holds ws12/deploy/document-index.sqlite3, and ws13/ holds
        # the OCR sidecar text; neither is a document this role may read.
        for widened in ("${SiteBucket.Arn}/*", "${SiteBucket.Arn}/ws12/*",
                        "${SiteBucket.Arn}/ws13/*",
                        "${SiteBucket.Arn}/ws13/searchable/*'"):
            with self.subTest(widened=widened):
                self.assertNotIn(widened, self.policy)

    def test_the_role_is_read_only(self):
        self.assertIn('Action: [ s3:GetObject ]', self.policy)
        for mutating in ('s3:PutObject', 's3:DeleteObject', 's3:ListBucket',
                         's3:*'):
            with self.subTest(mutating=mutating):
                self.assertNotIn(mutating, self.policy)

    def test_the_ws13_shapes_stay_out_of_the_cloudfront_allowlist(self):
        window = self.template.split('Sid: AllowCloudFrontRead', 1)[1]
        window = window.split('Condition:', 1)[0]
        for private in ('ws13/searchable', 'ws12/originals',
                        'ws12/licensed-copies', 'ws12/research-copies'):
            with self.subTest(private=private):
                self.assertNotIn(private, window)


if __name__ == '__main__':
    unittest.main()
