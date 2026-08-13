"""Adversarial tests for the Nevada-first WS9 evidence producer."""

import copy
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pipelines'))

import build_national_grade_evidence as national
import build_nevada_grade_evidence as nv


def load(path):
    with open(path, encoding='utf-8') as source:
        return json.load(source)


SOURCES_PATH = ROOT / 'pipelines/config/nv_grade_sources.json'
REVIEWED_PATH = ROOT / 'grades-research/nv/reviewed_grade_evidence.json'
DISTRICTS_PATH = ROOT / 'grades-research/nv/pp610_district_inventory.json'


class NevadaGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_document = load(SOURCES_PATH)
        self.reviewed_document = load(REVIEWED_PATH)
        self.district_document = load(DISTRICTS_PATH)
        self.sources = nv.validate_source_inventory(self.source_document)

    def test_reviewed_inventory_is_explicit_and_meets_nevada_threshold_observation(self):
        mines, used = nv.validate_reviewed(self.reviewed_document, self.sources)
        districts = nv.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(len(mines), 26)
        self.assertEqual(len(used), 12)
        self.assertIn('nbmg-mdf-21600019', used)
        self.assertIn('nbmg-mdf-21600006', used)
        self.assertEqual(len(districts), 71)
        self.assertEqual(
            {row['county'] for row in districts},
            {'Churchill', 'Clark', 'Elko', 'Esmeralda', 'Eureka',
             'Humboldt', 'Lander', 'Lincoln', 'Lyon', 'Mineral', 'Nye',
             'Pershing', 'Storey', 'Washoe', 'White Pine'})
        self.assertTrue(all(
            evidence.get('historic_values') is None
            for mine in mines for evidence in mine['evidence']))

    def test_sources_are_checksum_pinned_official_nbmg_or_usgs_documents(self):
        self.assertEqual(len(self.sources), 13)
        for source in self.sources.values():
            self.assertRegex(source['sha256'], r'^[0-9a-f]{64}$')
            self.assertGreater(source['bytes'], 1_000_000)
            self.assertGreater(source['pages'], 0)
            for field in ('document_url', 'catalog_url'):
                host = urllib.parse.urlparse(source[field]).hostname
                self.assertIn(host, nv.OFFICIAL_HOSTS)
        self.assertEqual(self.sources['nbmg-mdf-21600019']['text_mode'], 'ocr')
        self.assertEqual(self.sources['pp610']['text_mode'], 'embedded')

    def test_unofficial_host_and_cache_traversal_fail_closed(self):
        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['document_url'] = 'https://example.test/report.pdf'
        with self.assertRaisesRegex(nv.NevadaEvidenceError, 'approved official'):
            nv.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['local_path'] = '../outside.pdf'
        with self.assertRaisesRegex(nv.NevadaEvidenceError, 'normalized relative'):
            nv.validate_source_inventory(bad)

    def test_image_only_quote_requires_reviewed_page_render_hash(self):
        bad = copy.deepcopy(self.reviewed_document)
        evidence = bad['mines'][0]['evidence'][0]
        self.assertEqual(evidence['source_id'], 'nbmg-mdf-21600019')
        evidence.pop('page_image_sha256')
        with self.assertRaisesRegex(nv.NevadaEvidenceError, 'page SHA-256'):
            nv.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][1]['mine_id'] = bad['mines'][0]['mine_id']
        with self.assertRaisesRegex(nv.NevadaEvidenceError, 'duplicate mine_id'):
            nv.validate_reviewed(bad, self.sources)

    def test_pp610_inventory_cannot_silently_drop_a_district(self):
        bad = copy.deepcopy(self.district_document)
        bad['districts'].pop()
        with self.assertRaisesRegex(nv.NevadaEvidenceError, 'all 71'):
            nv.validate_district_inventory(bad, self.sources)

    def test_quote_cross_check_handles_ocr_noise_but_not_unrelated_text(self):
        quote = ('The Jumbo Extension during the year 1915 shipped 22,562 tons, '
                 'averaging 1.35 oz. Au., 4.41 oz. Ag., and 2.79% Cu.')
        ocr = ('The Jumbo Extension during the year 1915 shipped 22,562 tons, '
               'averaging 1.35 OZ. Au., LeAl on. Ag., and 2.79% Cu.')
        self.assertGreaterEqual(nv.quote_match_score(quote, ocr), 0.85)
        self.assertLess(nv.quote_match_score(quote, 'unrelated geology prose'), 0.2)

    def test_page_bottom_fragment_does_not_absorb_distant_figure_noise(self):
        blocks = [
            {'x_min': 380.0, 'x_max': 500.0, 'x_center': 440.0,
             'y_min': 300.0, 'text': 'GOODSPRINGS DISTRICT'},
            {'x_min': 320.0, 'x_max': 560.0, 'x_center': 440.0,
             'y_min': 320.0,
             'text': 'The Goodsprings district spans southern Clark County'},
            {'x_min': 380.0, 'x_max': 400.0, 'x_center': 390.0,
             'y_min': 470.0, 'text': '~~ <graph noise> .'},
        ]
        quote = nv.first_district_sentence(
            blocks, 'GOODSPRINGS DISTRICT', 'nv-goodsprings')
        self.assertEqual(
            quote, 'The Goodsprings district spans southern Clark County')
        self.assertNotIn('noise', quote)

    def test_source_checksum_drift_is_rejected_before_page_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.pdf'
            path.write_bytes(b'%PDF-changed')
            source = dict(self.sources['usgs-b407'])
            source['resolved_path'] = path
            source['bytes'] = len(b'%PDF-changed')
            source['sha256'] = '0' * 64
            with self.assertRaisesRegex(nv.NevadaEvidenceError, 'source drift'):
                nv.verify_pdf(source)

    def test_checked_in_manifests_validate_without_pdf_cache(self):
        self.assertEqual(nv.check_inputs(), {
            'mines': 26, 'grade_sources': 12, 'pp610_districts': 71})

    def test_full_local_build_matches_national_contract_and_does_not_release(self):
        required = {
            evidence['source_id']
            for mine in self.reviewed_document['mines']
            for evidence in mine['evidence']
        } | {'pp610'}
        if not all(self.sources[source_id]['resolved_path'].is_file()
                   for source_id in required):
            self.skipTest('official checksum-pinned PDF cache is not installed')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            output = Path(directory) / 'nv-grade-evidence'
            report, artifact = nv.build(output=output)
            self.assertEqual(report['metrics']['graded_mines'], 26)
            self.assertEqual(report['metrics']['primary_sources'], 12)
            self.assertEqual(report['metrics']['pp610_districts'], 71)
            self.assertEqual(
                report['effect'], 'evidence_only_no_release_or_done_mutation')
            self.assertFalse(report['threshold_observation']['is_release_decision'])
            self.assertNotIn('"enabled"', json.dumps(report))
            self.assertEqual(
                artifact['sha256'], nv.sha256_file(output / 'build.json'))

            grades = load(output / 'grades/nv.json')
            pp610 = load(output / 'pp610/nv.json')
            grade_result = national.validate_grade_document(
                grades, 'NV', {}, '0' * 64)
            pp_result = national.validate_pp610_document(pp610, 'NV')
            self.assertEqual(grade_result['metrics']['graded_mines'], 26)
            self.assertEqual(pp_result['district_count'], 71)
            for descriptor in report['artifacts']['page_indexes'].values():
                page_index = output / descriptor['path']
                self.assertEqual(descriptor['sha256'], nv.sha256_file(page_index))


if __name__ == '__main__':
    unittest.main()
