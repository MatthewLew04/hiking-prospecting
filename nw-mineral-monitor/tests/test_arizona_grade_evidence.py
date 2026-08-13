"""Adversarial tests for the Arizona WS9 evidence producer."""

import copy
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pipelines'))

import build_arizona_grade_evidence as az
import build_national_grade_evidence as national


def load(path):
    with open(path, encoding='utf-8') as source:
        return json.load(source)


SOURCES_PATH = ROOT / 'pipelines/config/az_grade_sources.json'
REVIEWED_PATH = ROOT / 'grades-research/az/reviewed_grade_evidence.json'
DISTRICTS_PATH = ROOT / 'grades-research/az/pp610_district_inventory.json'


class ArizonaGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_document = load(SOURCES_PATH)
        self.reviewed_document = load(REVIEWED_PATH)
        self.district_document = load(DISTRICTS_PATH)
        self.sources = az.validate_source_inventory(self.source_document)

    def test_reviewed_inventory_is_explicit_and_meets_threshold_observation(self):
        mines, used = az.validate_reviewed(
            self.reviewed_document, self.sources)
        districts = az.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(len(mines), 26)
        self.assertEqual(used, {'azbm-b137', 'usbm-ic6991', 'usgs-b782'})
        self.assertEqual(len(districts), 42)
        self.assertEqual(
            {row['county'] for row in districts},
            {'Cochise', 'Gila', 'Greenlee', 'Maricopa', 'Mohave', 'Pima',
             'Pinal', 'Santa Cruz', 'Yavapai', 'Yuma'})
        self.assertTrue(all(
            evidence.get('historic_values') is None
            for mine in mines for evidence in mine['evidence']))

        by_id = {mine['mine_id']: mine for mine in mines}
        southern_cross = by_id['az-southern-cross-mine']['evidence'][0]
        self.assertEqual(southern_cross['measurements'][0]['value'], 0.75)
        self.assertIn('from 0.75 to 1.0', southern_cross['verbatim_quote'])
        lincoln = by_id['az-lincoln-mine']['evidence'][0]
        self.assertEqual(
            {row['commodity']: row['value'] for row in lincoln['measurements']},
            {'Au': 1, 'Ag': 10, 'Cu': 15})

    def test_sources_are_checksum_pinned_official_azgs_or_usgs_documents(self):
        self.assertEqual(set(self.sources), az.EXPECTED_SOURCE_IDS)
        for source in self.sources.values():
            self.assertRegex(source['sha256'], r'^[0-9a-f]{64}$')
            self.assertGreater(source['bytes'], 1_000_000)
            self.assertGreater(source['pages'], 0)
            for field in ('document_url', 'catalog_url'):
                host = urllib.parse.urlparse(source[field]).hostname
                self.assertIn(host, az.OFFICIAL_HOSTS)
        for source_id in az.EXPECTED_GRADE_SOURCE_IDS:
            self.assertEqual(self.sources[source_id]['text_mode'], 'ocr')
        self.assertEqual(self.sources['pp610']['text_mode'], 'embedded')

    def test_unofficial_host_cache_traversal_and_source_omission_fail_closed(self):
        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['document_url'] = 'https://example.test/report.pdf'
        with self.assertRaisesRegex(az.ArizonaEvidenceError,
                                    'approved official'):
            az.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['local_path'] = '../outside.pdf'
        with self.assertRaisesRegex(az.ArizonaEvidenceError,
                                    'normalized relative'):
            az.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'].pop()
        with self.assertRaisesRegex(az.ArizonaEvidenceError,
                                    'exactly the four reviewed'):
            az.validate_source_inventory(bad)

    def test_scan_quote_requires_reviewed_page_render_hash(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0].pop('page_image_sha256')
        with self.assertRaisesRegex(az.ArizonaEvidenceError,
                                    'scan page SHA-256'):
            az.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][1]['mine_id'] = bad['mines'][0]['mine_id']
        with self.assertRaisesRegex(az.ArizonaEvidenceError,
                                    'duplicate mine_id'):
            az.validate_reviewed(bad, self.sources)

    def test_grade_source_diversity_cannot_be_silently_reduced(self):
        bad = copy.deepcopy(self.reviewed_document)
        for mine in bad['mines']:
            for evidence in mine['evidence']:
                if evidence['source_id'] == 'usbm-ic6991':
                    evidence['source_id'] = 'azbm-b137'
                    evidence['pdf_page'] = 31
        with self.assertRaisesRegex(az.ArizonaEvidenceError,
                                    'all three independent'):
            az.validate_reviewed(bad, self.sources)

    def test_pp610_inventory_cannot_drop_or_replace_a_district(self):
        bad = copy.deepcopy(self.district_document)
        bad['districts'].pop()
        with self.assertRaisesRegex(az.ArizonaEvidenceError, 'all 42'):
            az.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['districts'][-1]['district_id'] = 'az-substitute'
        with self.assertRaisesRegex(az.ArizonaEvidenceError, 'identity set'):
            az.validate_district_inventory(bad, self.sources)

    def test_quote_cross_check_handles_scan_noise_but_not_unrelated_text(self):
        quote = ('From January, 1925, to May, 1933, the mine produced 722 '
                 'tons of ore that averaged 1.69 per cent of copper, 0.503 '
                 'ounces of gold, and 0.37 ounces of silver per ton.')
        ocr = ('From January 1925 to May 1933 the mine produced 722 tons of '
               'ore that averaged 1.69 per cent of copper 0.503 ounces of '
               'gold and O.37 ounces of silver per ton')
        self.assertGreaterEqual(az.quote_match_score(quote, ocr), 0.9)
        self.assertLess(
            az.quote_match_score(quote, 'unrelated geology prose'), 0.2)

    def test_page_bottom_fragment_does_not_absorb_distant_figure_noise(self):
        blocks = [
            {'x_min': 380.0, 'x_max': 500.0, 'x_center': 440.0,
             'y_min': 300.0, 'text': 'GLOBE-MIAMI DISTRICT'},
            {'x_min': 320.0, 'x_max': 560.0, 'x_center': 440.0,
             'y_min': 320.0,
             'text': 'The Globe-Miami district is in central Gila County'},
            {'x_min': 380.0, 'x_max': 400.0, 'x_center': 390.0,
             'y_min': 470.0, 'text': '~~ <graph noise> .'},
        ]
        quote = az.first_district_sentence(
            blocks, 'GLOBE-MIAMI DISTRICT', 'az-globe-miami')
        self.assertEqual(
            quote, 'The Globe-Miami district is in central Gila County')
        self.assertNotIn('noise', quote)

    def test_source_checksum_drift_is_rejected_before_page_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.pdf'
            path.write_bytes(b'%PDF-changed')
            source = dict(self.sources['usgs-b782'])
            source['resolved_path'] = path
            source['bytes'] = len(b'%PDF-changed')
            source['sha256'] = '0' * 64
            with self.assertRaisesRegex(az.ArizonaEvidenceError,
                                        'source drift'):
                az.verify_pdf(source)

    def test_checked_in_manifests_validate_without_pdf_cache(self):
        self.assertEqual(az.check_inputs(), {
            'mines': 26, 'grade_sources': 3, 'pp610_districts': 42})

    def test_full_local_build_matches_national_contract_and_does_not_release(self):
        if not all(self.sources[source_id]['resolved_path'].is_file()
                   for source_id in az.EXPECTED_SOURCE_IDS):
            self.skipTest('official checksum-pinned PDF cache is not installed')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            output = Path(directory) / 'az-grade-evidence'
            report, artifact = az.build(output=output)
            self.assertEqual(report['metrics']['graded_mines'], 26)
            self.assertEqual(report['metrics']['primary_sources'], 3)
            self.assertEqual(report['metrics']['verbatim_quotes'], 26)
            self.assertEqual(report['metrics']['page_cites'], 26)
            self.assertEqual(report['metrics']['pp610_districts'], 42)
            self.assertEqual(report['metrics']['scan_image_pages_review_bound'],
                             23)
            self.assertEqual(
                report['effect'], 'evidence_only_no_release_or_done_mutation')
            self.assertFalse(
                report['threshold_observation']['is_release_decision'])
            self.assertNotIn('"enabled"', json.dumps(report))
            self.assertNotIn('"done"', json.dumps(report).lower())
            self.assertEqual(
                artifact['sha256'], az.sha256_file(output / 'build.json'))

            grades = load(output / 'grades/az.json')
            pp610 = load(output / 'pp610/az.json')
            grade_result = national.validate_grade_document(
                grades, 'AZ', {}, '0' * 64)
            pp_result = national.validate_pp610_document(pp610, 'AZ')
            self.assertEqual(grade_result['metrics']['graded_mines'], 26)
            self.assertEqual(grade_result['metrics']['primary_sources'], 3)
            self.assertEqual(pp_result['district_count'], 42)
            for descriptor in report['artifacts']['page_indexes'].values():
                page_index = output / descriptor['path']
                self.assertEqual(
                    descriptor['sha256'], az.sha256_file(page_index))


if __name__ == '__main__':
    unittest.main()
