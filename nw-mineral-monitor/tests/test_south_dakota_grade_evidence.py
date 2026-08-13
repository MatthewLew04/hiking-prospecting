"""Adversarial tests for the South Dakota WS11 evidence-only producer."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pipelines'))

import build_national_grade_evidence as national
import build_south_dakota_grade_evidence as sd


def load(path):
    with open(path, encoding='utf-8') as source:
        return json.load(source)


SOURCES_PATH = ROOT / 'pipelines/config/sd_grade_sources.json'
REVIEWED_PATH = ROOT / 'grades-research/sd/reviewed_grade_evidence.json'
DISTRICTS_PATH = ROOT / 'grades-research/sd/pp610_district_inventory.json'
OUTPUT = ROOT / 'build-inputs/ws9/sd-grade-evidence'


class SouthDakotaGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_document = load(SOURCES_PATH)
        self.reviewed_document = load(REVIEWED_PATH)
        self.district_document = load(DISTRICTS_PATH)
        self.sources = sd.validate_source_inventory(self.source_document)

    def test_reviewed_inventory_meets_bar_with_two_primary_sources(self):
        mines, used = sd.validate_reviewed(
            self.reviewed_document, self.sources)
        districts, figure_sha = sd.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(len(mines), 26)
        self.assertEqual(used, {'usbm-b427', 'usgs-b1332a'})
        self.assertEqual(Counter(
            evidence['source_id']
            for mine in mines for evidence in mine['evidence']),
            {'usbm-b427': 20, 'usgs-b1332a': 6})
        self.assertEqual(
            {measurement['commodity']
             for mine in mines
             for evidence in mine['evidence']
             for measurement in evidence['measurements']},
            {'Au', 'Ag', 'Cu'})
        self.assertEqual(len(districts), 7)
        self.assertEqual(
            figure_sha,
            '3e86b782d004dd14166d649b7197e76bb7a14e864ddfbbf5359a6de526b3d52c')
        self.assertNotIn('historic_values', json.dumps(mines))

    def test_sources_are_checksum_pinned_official_federal_documents(self):
        self.assertEqual(set(self.sources), sd.EXPECTED_SOURCE_IDS)
        for source in self.sources.values():
            self.assertRegex(source['sha256'], r'^[0-9a-f]{64}$')
            self.assertGreater(source['bytes'], 500_000)
            self.assertGreater(source['pages'], 0)
            for field in ('document_url', 'catalog_url'):
                self.assertIn(
                    urllib.parse.urlparse(source[field]).hostname,
                    sd.OFFICIAL_HOSTS)
        self.assertEqual(self.sources['usbm-b427']['text_mode'], 'ocr')
        self.assertEqual(self.sources['usgs-b1332a']['text_mode'], 'embedded')
        self.assertEqual(self.sources['pp610']['text_mode'], 'embedded')

    def test_unofficial_host_cache_traversal_and_source_omission_fail_closed(self):
        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['document_url'] = 'https://example.test/report.pdf'
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    'approved official'):
            sd.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['local_path'] = '../outside.pdf'
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError, 'normalized'):
            sd.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'].pop()
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    'exactly usbm-b427'):
            sd.validate_source_inventory(bad)

    def test_every_grade_row_requires_reviewed_image_hash(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0].pop('page_image_sha256')
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    'keys mismatch'):
            sd.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['page_image_sha256'] = 'not-a-hash'
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError, 'SHA-256'):
            sd.validate_reviewed(bad, self.sources)

    def test_quoted_dollar_conversion_retains_operands_and_exact_arithmetic(self):
        mines, _ = sd.validate_reviewed(self.reviewed_document, self.sources)
        converted = [
            evidence
            for mine in mines for evidence in mine['evidence']
            if evidence['derivation']['method'] ==
            'usd_per_short_ton_div_quoted_usd_per_troy_ounce']
        self.assertEqual(len(converted), 17)
        for evidence in converted:
            derivation = evidence['derivation']
            self.assertEqual(
                evidence['measurements'][0]['value'],
                round(derivation['usd_per_short_ton'] /
                      derivation['usd_per_troy_ounce'], 10))

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['measurements'][0]['value'] += 0.01
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    r'round\(value/price'):
            sd.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['derivation']['usd_per_troy_ounce'] = 99
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    r'round\(value/price'):
            sd.validate_reviewed(bad, self.sources)

    def test_ppm_identity_is_explicit_and_mutation_is_rejected(self):
        mines, _ = sd.validate_reviewed(self.reviewed_document, self.sources)
        ppm = [
            evidence
            for mine in mines for evidence in mine['evidence']
            if evidence['derivation']['method'] ==
            'parts_per_million_as_native_units']
        self.assertEqual(len(ppm), 6)
        for evidence in ppm:
            values = evidence['derivation']['parts_per_million']
            for measurement in evidence['measurements']:
                self.assertEqual(
                    measurement['value'], values[measurement['commodity']])

        bad = copy.deepcopy(self.reviewed_document)
        ppm_evidence = next(
            mine['evidence'][0] for mine in bad['mines']
            if mine['evidence'][0]['source_id'] == 'usgs-b1332a')
        ppm_evidence['measurements'][0]['unit'] = 'troy_ounces_per_short_ton'
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    '1 ppm must remain'):
            sd.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        ppm_evidence = next(
            mine['evidence'][0] for mine in bad['mines']
            if mine['evidence'][0]['source_id'] == 'usgs-b1332a')
        ppm_evidence['derivation']['parts_per_million']['Au'] += 1
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    '1 ppm must remain'):
            sd.validate_reviewed(bad, self.sources)

    def test_duplicate_evidence_and_measurement_are_rejected(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][1]['evidence'][0]['evidence_id'] = (
            bad['mines'][0]['evidence'][0]['evidence_id'])
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    'duplicate evidence_id'):
            sd.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        evidence = bad['mines'][0]['evidence'][0]
        evidence['measurements'].append(copy.deepcopy(evidence['measurements'][0]))
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError, 'duplicated'):
            sd.validate_reviewed(bad, self.sources)

    def test_pp610_inventory_is_exact_complete_figure_23(self):
        districts, _ = sd.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(
            [tuple(row[key] for key in
                   ('district_id', 'name', 'county', 'pdf_page', 'page_cite',
                    'source_heading')) for row in districts],
            list(sd.PP610_DISTRICTS))

        bad = copy.deepcopy(self.district_document)
        bad['districts'].pop()
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError, 'all seven'):
            sd.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['districts'][0], bad['districts'][1] = (
            bad['districts'][1], bad['districts'][0])
        with self.assertRaisesRegex(sd.SouthDakotaEvidenceError,
                                    'Figure 23 district'):
            sd.validate_district_inventory(bad, self.sources)

    def test_altcha_solver_is_bounded_and_reproducible(self):
        salt = 'sd-test-salt:'
        number = 173
        fields = {
            'algorithm': 'SHA-256',
            'challenge': hashlib.sha256(
                f'{salt}{number}'.encode()).hexdigest(),
            'salt': salt,
            'signature': 'test-signature',
            'maxnumber': '500',
        }
        payload = json.loads(base64_decode(sd._solve_altcha(fields)))
        self.assertEqual(payload['number'], number)
        self.assertEqual(payload['challenge'], fields['challenge'])

    def test_checked_in_build_is_national_compatible_and_evidence_only(self):
        report = load(OUTPUT / 'build.json')
        grades = load(OUTPUT / 'grades/sd.json')
        pp610 = load(OUTPUT / 'pp610/sd.json')
        grade_result = national.validate_grade_document(
            grades, 'SD', {}, '0' * 64)
        pp_result = national.validate_pp610_document(pp610, 'SD')
        self.assertEqual(grade_result['metrics']['graded_mines'], 26)
        self.assertEqual(grade_result['metrics']['primary_sources'], 2)
        self.assertEqual(pp_result['district_count'], 7)
        self.assertEqual(
            report['effect'], 'evidence_only_no_release_or_done_mutation')
        self.assertFalse(report['threshold_observation']['is_release_decision'])
        self.assertNotIn('"enabled"', json.dumps(report))
        for descriptor in report['artifacts']['page_indexes'].values():
            path = OUTPUT / descriptor['path']
            self.assertEqual(descriptor['bytes'], path.stat().st_size)
            self.assertEqual(descriptor['sha256'], sd.sha256_file(path))

    def test_full_local_build_is_reproducible_and_does_not_publish(self):
        if not all(source['resolved_path'].is_file()
                   for source in self.sources.values()):
            self.skipTest(
                'official checksum-pinned South Dakota PDF cache is not installed')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            first_output = Path(directory) / 'first'
            second_output = Path(directory) / 'second'
            first, first_artifact = sd.build(output=first_output)
            second, second_artifact = sd.build(output=second_output)
            self.assertEqual(first['metrics'], second['metrics'])
            self.assertEqual(first_artifact['sha256'], second_artifact['sha256'])
            self.assertEqual(first['metrics']['graded_mines'], 26)
            self.assertEqual(first['metrics']['primary_sources'], 2)
            self.assertEqual(first['metrics']['pp610_districts'], 7)
            self.assertEqual(first['metrics']['grade_image_pages_review_bound'],
                             22)
            self.assertEqual(first['metrics']['explicit_quote_price_conversions'],
                             17)
            self.assertEqual(first['metrics']['ppm_table_rows'], 6)
            self.assertFalse(first['threshold_observation']['is_release_decision'])
            self.assertFalse(sd.is_inside(first_output, ROOT / 'site'))


def base64_decode(value):
    import base64
    return base64.b64decode(value).decode('utf-8')


if __name__ == '__main__':
    unittest.main()
