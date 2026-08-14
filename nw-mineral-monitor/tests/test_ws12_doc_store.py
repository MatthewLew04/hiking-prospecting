"""Adversarial tests for the WS12 document store and its citation contract."""

import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
import zlib
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
TEST_MANIFEST = os.path.join(
    ROOT, 'tests', 'fixtures', 'ws12_document_store_manifest.json')
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_doc_store as builder
import doc_store
import validate_doc_store as gate


def _pdf(pages):
    """Build a minimal, dependency-free PDF carrying one text line per page.

    The suite refuses to depend on a PDF toolchain or on the private document
    cache, so the fixtures are assembled by hand the same way tools/measure.js
    builds its PMTiles fixture.
    """
    objects = ['']
    kids, page_ids = [], []
    for index, text in enumerate(pages):
        stream = f'BT /F1 12 Tf 40 700 Td ({text}) Tj ET'.encode('latin-1')
        content_id = 4 + index * 2
        page_id = content_id + 1
        objects.append((content_id,
                        b'<< /Length %d >>\nstream\n%s\nendstream'
                        % (len(stream), stream)))
        objects.append((page_id,
                        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                        b'/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>'
                        % content_id))
        page_ids.append(page_id)
        kids.append(b'%d 0 R' % page_id)
    head = [
        (1, b'<< /Type /Catalog /Pages 2 0 R >>'),
        (2, b'<< /Type /Pages /Kids [%s] /Count %d >>'
            % (b' '.join(kids), len(page_ids))),
        (3, b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'),
    ]
    body = head + [item for item in objects if isinstance(item, tuple)]
    body.sort()
    out = bytearray(b'%PDF-1.4\n')
    offsets = {}
    for number, payload in body:
        offsets[number] = len(out)
        out += b'%d 0 obj\n' % number + payload + b'\nendobj\n'
    start = len(out)
    highest = max(offsets) + 1
    out += b'xref\n0 %d\n0000000000 65535 f \n' % highest
    for number in range(1, highest):
        out += b'%010d 00000 n \n' % offsets.get(number, 0)
    out += (b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (highest, start))
    return bytes(out)


class Fixture:
    """A complete, valid two-document store built under a temporary root."""

    def __init__(self, root):
        self.root = root
        self.cache = os.path.join(root, 'pipelines', 'cache', 'ws12', 'inbox')
        self.store = os.path.join(root, 'store')
        self.site = os.path.join(root, 'site')
        os.makedirs(self.cache)
        os.makedirs(os.path.join(self.site, 'data', 'docs'))
        self.pages = {
            'report': ['Champagne Creek assay report page one',
                       'Only six patented claims exist in the area of interest',
                       'Sampling continued along the ridge'],
            'record': ['Mining District Name LAVA CREEK DISTRICT County BUTTE'],
        }
        self.documents = {}
        self.ocr_geometry = None
        for name, pages in self.pages.items():
            raw = _pdf(pages)
            path = os.path.join(self.cache, f'{name}.pdf')
            with open(path, 'wb') as handle:
                handle.write(raw)
            self.documents[name] = {
                'path': path, 'bytes': len(raw),
                'sha256': doc_store.sha256_bytes(raw), 'pages': len(pages),
            }
        self.registry_path = os.path.join(root, 'ws12_documents.json')
        self.citations_path = os.path.join(root, 'ws12_citations.json')
        self.write_registry(self.default_registry())
        self.write_citations(self.default_citations())

    # -- inputs ---------------------------------------------------------
    def entry(self, name, **overrides):
        row = {
            'source_id': f'fixture-{name}',
            'state': 'ID',
            'portal': 'example-portal',
            'mine_id': 'stategeo-fixture-if0126',
            'retrieved': '2026-08-14',
            'title': f'Fixture document {name}',
            'authority': 'Fixture Geological Survey',
            'catalog_url': 'https://official.example.test/record/IF0126',
            'document_url': f'https://official.example.test/files/{name}.pdf',
            'local_path': os.path.relpath(self.documents[name]['path'], self.root),
            'bytes': self.documents[name]['bytes'],
            'sha256': self.documents[name]['sha256'],
            'pages': self.documents[name]['pages'],
            'public_domain': True,
            'rights_basis': ('Synthetic public-domain fixture created for the '
                             'test suite; no third-party rights are implicated.'),
            'subjects': [{'state': 'ID', 'mine_id': 'stategeo-fixture-if0126',
                          'label': 'Fixture St. Louis Mine'}],
        }
        row.update(overrides)
        return row

    def default_registry(self):
        return {
            'schema_version': 1,
            'dataset': 'ws12-document-registry',
            'portals': {'official.example.test': 'example-portal'},
            'documents': [self.entry('report'), self.entry('record')],
        }

    def default_citations(self):
        return {
            'schema_version': 1,
            'dataset': 'ws12-reviewed-citations',
            'reviewed_on': '2026-08-14',
            'reviewed_by': 'fixture review',
            'review_method': 'fixture',
            'citations': [
                {'citation_id': 'fixture-patented-claims',
                 'source_id': 'fixture-report', 'state': 'ID',
                 'mine_id': 'stategeo-fixture-if0126',
                 'mine_name': 'Fixture St. Louis Mine',
                 'page_cite': 'p. 2', 'pdf_page': 2,
                 'quote': 'Only six patented claims exist in the area of interest'},
                {'citation_id': 'fixture-district',
                 'source_id': 'fixture-record', 'state': 'ID',
                 'mine_id': 'stategeo-fixture-if0126',
                 'mine_name': 'Fixture St. Louis Mine',
                 'page_cite': 'p. 1', 'pdf_page': 1,
                 'quote': 'Mining District Name LAVA CREEK DISTRICT County BUTTE'},
            ],
        }

    def write_registry(self, value):
        with open(self.registry_path, 'w', encoding='utf-8') as handle:
            json.dump(value, handle)

    def write_citations(self, value):
        with open(self.citations_path, 'w', encoding='utf-8') as handle:
            json.dump(value, handle)

    # -- build ----------------------------------------------------------
    def probe(self, path):
        # Inputs are recognised by path, so a byte-identical OCR product is
        # still probed as an OCR product and the no-op guard can fire.
        for name, meta in self.documents.items():
            if os.path.realpath(meta['path']) == os.path.realpath(path):
                texts = list(self.pages[name])
                break
        else:                                   # an OCR product, not an input
            texts = self.ocr_texts
        geometry = self.ocr_geometry if self.ocr_geometry is not None and not any(
            os.path.realpath(meta['path']) == os.path.realpath(path)
            for meta in self.documents.values()) else [
                {'media_box': [0.0, 0.0, 612.0, 792.0],
                 'crop_box': [0.0, 0.0, 612.0, 792.0], 'rotation': 0}
                for _ in texts]
        return {'pages': len(texts), 'page_texts': texts,
                'page_geometry': geometry,
                'pages_with_text': sum(1 for text in texts if text.strip()),
                'characters': sum(len(text.strip()) for text in texts)}

    def build(self, **overrides):
        arguments = {
            'registry_path': self.registry_path,
            'store_dir': self.store,
            'manifest_path': os.path.join(self.site, 'data', 'docs', 'manifest.json'),
            'generated': '2026-08-14',
            'probe': self.probe,
        }
        arguments.update(overrides)
        with mock.patch.object(builder, 'ROOT', self.root), \
                mock.patch.object(builder, 'CITATIONS', self.citations_path), \
                mock.patch.object(builder, 'RESEARCH', os.path.join(self.root, 'absent')), \
                mock.patch.object(builder, 'LEGACY_GRADES', os.path.join(self.root, 'absent.json')):
            return builder.build(**arguments)

    def manifest(self):
        return doc_store.load_manifest(
            os.path.join(self.site, 'data', 'docs', 'manifest.json'))


class DocumentStoreContractTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Fixture(temporary.name)

    def test_build_stores_both_variants_and_hash_verifies_every_row(self):
        fixture = self.fixture()
        result = fixture.build()
        self.assertEqual(result['documents'], 2)
        self.assertEqual(result['citations'], 2)
        self.assertEqual(result['citations_quote_located'], 2)
        manifest = fixture.manifest()
        self.assertEqual(doc_store.verify_store(manifest, fixture.store), [])
        for row in manifest['documents']:
            for variant in doc_store.VARIANTS:
                self.assertTrue(os.path.isfile(
                    doc_store.store_path(fixture.store, row[variant]['key'])))

    def test_doc_id_is_the_raw_sha256_and_both_variants_share_its_directory(self):
        fixture = self.fixture()
        fixture.build()
        for row in fixture.manifest()['documents']:
            self.assertEqual(row['doc_id'], row['raw']['sha256'])
            self.assertEqual(os.path.dirname(row['raw']['key']),
                             os.path.dirname(row['searchable']['key']))
            self.assertIn(f'/{row["doc_id"]}/', row['raw']['key'])
            self.assertTrue(row['raw']['key'].startswith(
                f'docs/{row["state"]}/{row["portal"]}/{row["mine_id"]}/'))

    def test_key_never_encodes_a_filename_or_a_portal_url(self):
        fixture = self.fixture()
        fixture.build()
        for row in fixture.manifest()['documents']:
            for variant in doc_store.VARIANTS:
                key = row[variant]['key']
                self.assertNotIn('report.pdf', key)
                self.assertNotIn('record.pdf', key)
                self.assertNotIn('official.example.test', key)

    def test_build_is_byte_reproducible(self):
        fixture = self.fixture()
        first = fixture.build()
        second = fixture.build()
        self.assertEqual(first['sha256'], second['sha256'])

    def test_manifest_is_canonical_and_reconciles_its_own_metrics(self):
        fixture = self.fixture()
        fixture.build()
        path = os.path.join(fixture.site, 'data', 'docs', 'manifest.json')
        with open(path, 'rb') as handle:
            raw = handle.read()
        manifest = doc_store.validate_manifest(json.loads(raw))
        self.assertEqual(doc_store.canonical_bytes(manifest), raw)
        broken = copy.deepcopy(manifest)
        broken['metrics']['documents'] += 1
        with self.assertRaisesRegex(doc_store.DocStoreError, 'do not reconcile'):
            doc_store.validate_manifest(broken)

    def test_source_drift_fails_before_anything_is_written(self):
        fixture = self.fixture()
        path = fixture.documents['record']['path']
        with open(path, 'rb') as handle:
            raw = bytearray(handle.read())
        raw[-40] ^= 0x20                        # same length, different bytes
        with open(path, 'wb') as handle:
            handle.write(bytes(raw))
        with self.assertRaisesRegex(builder.DocumentBuildError, 'hashes to'):
            fixture.build()
        self.assertFalse(os.path.exists(
            os.path.join(fixture.site, 'data', 'docs', 'manifest.json')))

    def test_missing_local_document_is_named_rather_than_skipped(self):
        fixture = self.fixture()
        os.unlink(fixture.documents['record']['path'])
        with self.assertRaisesRegex(builder.DocumentBuildError, 'not staged locally'):
            fixture.build()

    def test_rights_unresolved_row_is_never_read_or_stored(self):
        fixture = self.fixture()
        registry = fixture.default_registry()
        registry['documents'][0]['public_domain'] = False
        registry['documents'][0]['rights_basis'] = (
            'Public reachability alone is not an affirmative public-domain basis.')
        fixture.write_registry(registry)
        os.unlink(fixture.documents['report']['path'])
        result = fixture.build()
        self.assertEqual(result['documents'], 1)
        self.assertEqual(result['documents_skipped_rights'], 1)
        manifest = fixture.manifest()
        self.assertEqual(manifest['metrics']['documents_servable'], 1)
        self.assertEqual(manifest['metrics']['documents_rights_unresolved'], 0)

    def test_page_count_drift_from_the_pinned_inventory_fails(self):
        fixture = self.fixture()
        registry = fixture.default_registry()
        registry['documents'][0]['pages'] = 99
        fixture.write_registry(registry)
        with self.assertRaisesRegex(builder.DocumentBuildError, 'the inventory pins'):
            fixture.build()

    def test_shared_bytes_need_exactly_one_declared_filing_key(self):
        fixture = self.fixture()
        registry = fixture.default_registry()
        duplicate = fixture.entry('record', source_id='fixture-record-second',
                                  state='NV', mine_id='statewide-nv')
        duplicate['subjects'] = [{'state': 'NV', 'mine_id': 'statewide-nv',
                                  'label': 'Nevada'}]
        registry['documents'].append(duplicate)
        fixture.write_registry(registry)
        with self.assertRaisesRegex(builder.DocumentBuildError,
                                    'different key paths'):
            fixture.build()
        registry['documents'][2]['filing'] = True
        fixture.write_registry(registry)
        manifest = None
        fixture.build()
        manifest = fixture.manifest()
        shared = [row for row in manifest['documents'] if row['state'] == 'NV']
        self.assertEqual(len(shared), 1)
        self.assertEqual(len(manifest['documents']), 2)
        self.assertEqual(
            sorted(shared[0]['source_ids']),
            ['fixture-record', 'fixture-record-second'])

    def test_ocr_runs_only_when_the_original_has_no_text_layer(self):
        fixture = self.fixture()
        fixture.pages['record'] = ['']
        fixture.ocr_texts = ['Mining District Name LAVA CREEK DISTRICT County BUTTE']
        calls = []

        def ocr(source_path, target_path):
            calls.append(source_path)
            shutil.copyfile(source_path, target_path)
            with open(target_path, 'ab') as handle:
                handle.write(b'% an added text layer\n')
            return 'fixture-ocr 1.0'

        fixture.build(ocr=ocr)
        self.assertEqual(len(calls), 1)
        manifest = fixture.manifest()
        statuses = {row['title']: row['text_layer']['status']
                    for row in manifest['documents']}
        self.assertEqual(statuses['Fixture document record'], 'ocr_added')
        self.assertEqual(statuses['Fixture document report'], 'native')
        ocred = [row for row in manifest['documents']
                 if row['text_layer']['status'] == 'ocr_added'][0]
        self.assertNotEqual(ocred['searchable']['sha256'], ocred['doc_id'])
        self.assertEqual(doc_store.verify_store(manifest, fixture.store), [])

    def test_mixed_pdf_ocrs_only_after_detecting_an_image_only_page(self):
        fixture = self.fixture()
        fixture.pages['report'] = [
            'Champagne Creek assay report page one', '',
            'Sampling continued along the ridge']
        fixture.ocr_texts = [
            'Champagne Creek assay report page one',
            'Only six patented claims exist in the area of interest',
            'Sampling continued along the ridge']
        calls = []

        def ocr(source_path, target_path):
            calls.append(source_path)
            shutil.copyfile(source_path, target_path)
            with open(target_path, 'ab') as handle:
                handle.write(b'% mixed-page OCR layer\n')
            return 'fixture-ocr --skip-text'

        fixture.build(ocr=ocr)
        self.assertEqual(len(calls), 1)
        report = next(row for row in fixture.manifest()['documents']
                      if row['title'] == 'Fixture document report')
        self.assertEqual(report['text_layer']['status'], 'ocr_added')
        self.assertEqual(report['text_layer']['pages_with_text'], 3)

    def test_ocr_that_changes_page_geometry_is_rejected(self):
        fixture = self.fixture()
        fixture.pages['record'] = ['']
        fixture.ocr_texts = ['recovered text']
        fixture.ocr_geometry = [{
            'media_box': [0.0, 0.0, 595.0, 842.0],
            'crop_box': [0.0, 0.0, 595.0, 842.0], 'rotation': 90}]

        def ocr(source_path, target_path):
            shutil.copyfile(source_path, target_path)
            with open(target_path, 'ab') as handle:
                handle.write(b'% geometry changed\n')
            return 'fixture-ocr 1.0'

        with self.assertRaisesRegex(builder.DocumentBuildError, 'page geometry'):
            fixture.build(ocr=ocr)

    def test_ocr_that_changes_pagination_is_rejected(self):
        fixture = self.fixture()
        fixture.pages['record'] = ['']
        fixture.ocr_texts = ['page one', 'an invented second page']

        def ocr(source_path, target_path):
            shutil.copyfile(source_path, target_path)
            with open(target_path, 'ab') as handle:
                handle.write(b'% reflowed\n')
            return 'fixture-ocr 1.0'

        with self.assertRaisesRegex(builder.DocumentBuildError,
                                    'pagination must be preserved'):
            fixture.build(ocr=ocr)

    def test_ocr_that_adds_nothing_is_rejected(self):
        fixture = self.fixture()
        fixture.pages['record'] = ['']
        fixture.ocr_texts = ['recovered text']
        with self.assertRaisesRegex(builder.DocumentBuildError,
                                    'produced the original'):
            fixture.build(ocr=lambda source, target: (
                shutil.copyfile(source, target), 'fixture-ocr 1.0')[1])

    def test_a_quote_absent_from_the_text_layer_is_recorded_as_unlocated(self):
        fixture = self.fixture()
        citations = fixture.default_citations()
        citations['citations'][0]['quote'] = 'A sentence that is not on any page'
        fixture.write_citations(citations)
        result = fixture.build()
        self.assertEqual(result['citations'], 2)
        self.assertEqual(result['citations_quote_located'], 1)
        rows = {row['citation_id']: row for row in fixture.manifest()['citations']}
        self.assertFalse(rows['fixture-patented-claims']['quote_located'])
        self.assertEqual(rows['fixture-patented-claims']['page'], 2)

    def test_the_text_layer_page_outranks_the_reviewer_page(self):
        fixture = self.fixture()
        citations = fixture.default_citations()
        citations['citations'][0]['pdf_page'] = 1     # the quote is on page 2
        fixture.write_citations(citations)
        fixture.build()
        rows = {row['citation_id']: row for row in fixture.manifest()['citations']}
        self.assertEqual(rows['fixture-patented-claims']['page'], 2)
        self.assertTrue(rows['fixture-patented-claims']['quote_located'])


class DocumentStoreSchemaTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = Fixture(temporary.name)
        fixture.build()
        return fixture

    def test_rejects_a_key_that_does_not_derive_from_its_own_parts(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        manifest['documents'][0]['raw']['key'] = 'docs/ID/example-portal/x/y/raw.pdf'
        with self.assertRaisesRegex(doc_store.DocStoreError, 'derived store key'):
            doc_store.validate_manifest(manifest)

    def test_rejects_a_doc_id_that_is_not_the_raw_digest(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        row = manifest['documents'][0]
        row['raw']['sha256'] = 'f' * 64
        row['raw']['key'] = doc_store.object_key(
            row['state'], row['portal'], row['mine_id'], row['doc_id'], 'raw')
        with self.assertRaisesRegex(doc_store.DocStoreError,
                                    'SHA-256 of the raw original'):
            doc_store.validate_manifest(manifest)

    def test_rejects_an_unnamespaced_mine_id(self):
        with self.assertRaisesRegex(doc_store.DocStoreError, 'must start with'):
            doc_store.object_key('ID', 'example-portal', 'if0126', 'a' * 64, 'raw')

    def test_rejects_a_store_key_that_escapes_the_root(self):
        with self.assertRaisesRegex(doc_store.DocStoreError, 'unsafe path segment'):
            doc_store.store_path('/tmp/store', 'docs/../../etc/passwd')

    def test_rejects_a_native_text_layer_whose_searchable_copy_differs(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        manifest['documents'][0]['searchable']['sha256'] = 'b' * 64
        with self.assertRaisesRegex(doc_store.DocStoreError,
                                    'must be the original bytes'):
            doc_store.validate_manifest(manifest)

    def test_rejects_a_citation_past_the_end_of_its_document(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        manifest['citations'][0]['page'] = 999
        with self.assertRaisesRegex(doc_store.DocStoreError, 'past the end'):
            doc_store.validate_manifest(manifest)

    def test_rejects_a_citation_for_an_undeclared_subject(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        manifest['citations'][0]['mine_id'] = 'ws9-nv-somewhere-else'
        with self.assertRaisesRegex(doc_store.DocStoreError,
                                    'no document declares'):
            doc_store.validate_manifest(manifest)

    def test_rejects_a_manifest_that_declares_the_corpus_public(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        manifest['store']['public_prefix'] = True
        with self.assertRaisesRegex(doc_store.DocStoreError,
                                    'public_prefix must be false'):
            doc_store.validate_manifest(manifest)

    def test_rejects_an_unbounded_presign_ttl(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        manifest['store']['presign_ttl_seconds'] = 86400
        with self.assertRaisesRegex(doc_store.DocStoreError, 'at most 3600'):
            doc_store.validate_manifest(manifest)

    def test_verify_store_reports_a_missing_and_a_drifted_object(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        first = doc_store.store_path(
            fixture.store, manifest['documents'][0]['raw']['key'])
        os.unlink(first)
        second = doc_store.store_path(
            fixture.store, manifest['documents'][1]['raw']['key'])
        os.chmod(second, 0o644)
        with open(second, 'wb') as handle:
            handle.write(b'%PDF-1.4 not the stored bytes')
        problems = doc_store.verify_store(manifest, fixture.store)
        self.assertTrue(any('missing from the store' in item for item in problems))
        self.assertTrue(any('bytes, manifest says' in item for item in problems))
        # A native-text document's two variants are the same bytes, so
        # tampering with one is reported against both rather than half-hidden.
        drifted = [item for item in problems if 'bytes, manifest says' in item]
        self.assertEqual(len(drifted), 2)
        self.assertEqual(len(problems), 3)


class PortalDeathTests(unittest.TestCase):
    """The acceptance case: block the source domain, change nothing else."""

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = Fixture(temporary.name)
        fixture.build()
        return fixture

    def test_every_citation_resolves_with_the_network_removed(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        # Simulate the portal — and in fact the whole internet — being gone.
        # Nothing in resolution may reach for a socket.
        def refuse(*args, **kwargs):
            raise AssertionError('resolution must not open a network connection')

        with mock.patch.object(socket, 'socket', refuse), \
                mock.patch.object(socket, 'create_connection', refuse), \
                mock.patch.object(socket, 'getaddrinfo', refuse):
            for citation in manifest['citations']:
                resolved = doc_store.resolve_open_doc(
                    manifest, citation['doc_id'], citation['page'],
                    citation['quote'])
                self.assertEqual(resolved['page'], citation['page'])
                self.assertTrue(resolved['key'].startswith('docs/'))
                self.assertTrue(resolved['quote_located'])
                self.assertEqual(doc_store.verify_store(manifest, fixture.store), [])

    def test_resolution_is_identical_with_and_without_the_portal(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        citation = manifest['citations'][0]
        before = doc_store.resolve_open_doc(
            manifest, citation['doc_id'], citation['page'], citation['quote'])
        dead = copy.deepcopy(manifest)
        for row in dead['documents']:
            row['source_url'] = 'https://gone.example.test/404'
            row['catalog_url'] = 'https://gone.example.test/404'
        dead = doc_store.validate_manifest(dead)
        after = doc_store.resolve_open_doc(
            dead, citation['doc_id'], citation['page'], citation['quote'])
        for field in ('doc_id', 'page', 'key', 'sha256', 'quote', 'quote_located'):
            self.assertEqual(before[field], after[field])

    def test_a_source_id_and_a_digest_prefix_both_resolve(self):
        fixture = self.fixture()
        manifest = fixture.manifest()
        expected = doc_store.index_source_ids(manifest)['fixture-record']
        self.assertEqual(doc_store.resolve_doc_id(manifest, 'fixture-record'),
                         expected)
        self.assertEqual(doc_store.resolve_doc_id(manifest, expected[:12]),
                         expected)
        with self.assertRaisesRegex(doc_store.DocStoreError, 'no stored document'):
            doc_store.resolve_doc_id(manifest, 'not-a-document')


class DeliveryGateTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = Fixture(temporary.name)
        fixture.build()
        return fixture

    def manifest_path(self, fixture):
        return os.path.join(fixture.site, 'data', 'docs', 'manifest.json')

    def test_the_shipped_template_and_manifest_pass_the_gate(self):
        result = gate.validate(TEST_MANIFEST)
        self.assertTrue(result['ok'])
        self.assertFalse(result['store_objects_hash_verified'])
        self.assertEqual(result['effect'],
                         'validation_only_no_upload_or_release_mutation')

    def test_complete_manifest_cannot_enter_the_public_site_or_repository(self):
        public_manifest = os.path.join(
            ROOT, 'site', 'data', 'docs', 'manifest.json')
        self.assertFalse(os.path.exists(public_manifest))
        self.assertEqual(
            doc_store.MANIFEST_RELATIVE,
            os.path.join('var', 'ws12', 'document-store-manifest.json'))
        with open(os.path.join(ROOT, '..', '.gitignore'), encoding='utf-8') as handle:
            ignored = handle.read()
        self.assertIn(
            'nw-mineral-monitor/site/data/docs/manifest.json', ignored)

    def test_gate_fails_if_the_corpus_joins_the_public_prefix_allowlist(self):
        with open(os.path.join(ROOT, 'infra', 'template.yaml'), encoding='utf-8') as handle:
            template = handle.read()
        exposed = template.replace(
            "- !Sub '${SiteBucket.Arn}/data/*'",
            "- !Sub '${SiteBucket.Arn}/data/*'\n"
            "              - !Sub '${SiteBucket.Arn}/docs/*'", 1)
        problems = gate.check_private_delivery(exposed)
        self.assertTrue(any('must stay private' in item for item in problems))

    def test_gate_requires_the_full_manifest_to_stay_private(self):
        with open(os.path.join(ROOT, 'infra', 'template.yaml'), encoding='utf-8') as handle:
            template = handle.read()
        exposed = template.replace('Sid: DenyCloudFrontDocumentManifest',
                                   'Sid: RemovedDocumentManifestDeny', 1)
        problems = gate.check_private_delivery(exposed)
        self.assertTrue(any('full document manifest' in item for item in problems))

    def test_gate_fails_if_the_viewer_is_not_cloudfront_readable(self):
        with open(os.path.join(ROOT, 'infra', 'template.yaml'), encoding='utf-8') as handle:
            template = handle.read()
        removed = template.replace(
            "              - !Sub '${SiteBucket.Arn}/viewer.html'\n", '', 1)
        problems = gate.check_private_delivery(removed)
        self.assertTrue(any('viewer.html' in item for item in problems))

    def test_gate_fails_when_the_lifecycle_disagrees_with_the_manifest(self):
        with open(os.path.join(ROOT, 'infra', 'template.yaml'), encoding='utf-8') as handle:
            template = handle.read()
        store = dict(
            raw_transition_days=30, raw_storage_class='STANDARD_IA')
        self.assertEqual(gate.check_lifecycle(template, store), [])
        drifted = dict(store, raw_transition_days=90)
        self.assertTrue(any('90 days' in item
                            for item in gate.check_lifecycle(template, drifted)))
        self.assertTrue(any('lifecycle rule' in item for item in
                            gate.check_lifecycle('no rules here', store)))
        no_small_objects = template.replace("            ObjectSizeGreaterThan: '0'\n", '')
        self.assertTrue(any('128 KiB' in item for item in
                            gate.check_lifecycle(no_small_objects, store)))

    def test_gate_hash_verifies_the_store_when_one_is_supplied(self):
        fixture = self.fixture()
        result = gate.validate(
            self.manifest_path(fixture), store_dir=fixture.store,
            template_path=os.path.join(ROOT, 'infra', 'template.yaml'))
        self.assertTrue(result['store_objects_hash_verified'])
        manifest = fixture.manifest()
        target = doc_store.store_path(
            fixture.store, manifest['documents'][0]['searchable']['key'])
        os.chmod(target, 0o644)
        with open(target, 'wb') as handle:
            handle.write(b'%PDF-1.4 substituted')
        with self.assertRaisesRegex(gate.DocStoreGateError, 'bytes, manifest says'):
            gate.validate(self.manifest_path(fixture), store_dir=fixture.store,
                          template_path=os.path.join(ROOT, 'infra', 'template.yaml'))

    def test_remote_promotion_verifies_both_digest_and_byte_count(self):
        with open(os.path.join(ROOT, 'infra', 'deploy.sh'), encoding='utf-8') as handle:
            deploy = handle.read()
        upload = deploy.split('upload_doc_store()', 1)[1].split(
            'sync_public_data_without_pointers_or_binaries()', 1)[0]
        self.assertIn("--query '[ChecksumSHA256,ContentLength]'", upload)
        self.assertIn('[ "$expected_size" = "$actual_size" ]', upload)
        self.assertIn("entry['bytes']", upload)
        self.assertIn('--checksum-sha256 "$expected_b64"', upload)
        self.assertIn('--cli-read-timeout 120', upload)
        self.assertIn('for put_attempt in 1 2 3', upload)
        self.assertIn('AWS_MAX_ATTEMPTS=1', upload)
        self.assertIn('run_with_deadline 180', upload)
        self.assertIn('document upload failed after 3 bounded attempts', upload)


class ShippedCorpusTests(unittest.TestCase):
    """Contracts the committed manifest itself must keep."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = doc_store.load_manifest(TEST_MANIFEST)

    def test_the_if0126_subject_has_a_stored_citation_that_resolves(self):
        rows = doc_store.citations_for_subject(
            self.manifest, 'ID', 'stategeo-igs-dd-1-if0126')
        self.assertTrue(rows, 'IF0126 must carry at least one stored citation')
        for citation in rows:
            resolved = doc_store.resolve_open_doc(
                self.manifest, citation['doc_id'], citation['page'],
                citation['quote'])
            self.assertTrue(resolved['quote_located'])
            self.assertTrue(resolved['key'].endswith('searchable.pdf'))
            self.assertLessEqual(resolved['page'], resolved['pages'])

    def test_every_document_declares_an_https_portal_url_it_never_opens(self):
        for row in self.manifest['documents']:
            self.assertTrue(row['source_url'].startswith('https://'))
            self.assertTrue(row['catalog_url'].startswith('https://'))
            self.assertNotIn(row['source_url'], row['raw']['key'])

    def test_every_stored_document_keeps_one_page_count_across_variants(self):
        for row in self.manifest['documents']:
            self.assertTrue(row['pagination_preserved'])
            self.assertGreater(row['pages'], 0)
            self.assertLessEqual(row['text_layer']['pages_with_text'], row['pages'])

    def test_no_citation_depends_on_a_document_with_no_text_layer(self):
        documents = doc_store.index_documents(self.manifest)
        for citation in self.manifest['citations']:
            if citation['quote_located']:
                self.assertNotEqual(
                    documents[citation['doc_id']]['text_layer']['status'], 'absent')


if __name__ == '__main__':
    unittest.main()
