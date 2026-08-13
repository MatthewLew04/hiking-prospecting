"""Adversarial tests for the Colorado WS11 evidence-only producer."""

import copy
import json
import sys
import tempfile
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pipelines'))

import build_colorado_grade_evidence as co
import build_national_grade_evidence as national


def load(path):
    with open(path, encoding='utf-8') as source:
        return json.load(source)


SOURCES_PATH = ROOT / 'pipelines/config/co_grade_sources.json'
REVIEWED_PATH = ROOT / 'grades-research/co/reviewed_grade_evidence.json'
DISTRICTS_PATH = ROOT / 'grades-research/co/pp610_district_inventory.json'
OUTPUT = ROOT / 'build-inputs/ws9/co-grade-evidence'


class ColoradoGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_document = load(SOURCES_PATH)
        self.reviewed_document = load(REVIEWED_PATH)
        self.district_document = load(DISTRICTS_PATH)
        self.sources = co.validate_source_inventory(self.source_document)

    def test_reviewed_inventory_meets_quantitative_bar_without_price_derivation(self):
        mines, used = co.validate_reviewed(self.reviewed_document, self.sources)
        districts, figure_sha = co.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(len(mines), 26)
        self.assertEqual(used, {'pp359', 'b478'})
        self.assertEqual(Counter(
            evidence['source_id']
            for mine in mines for evidence in mine['evidence']),
            {'pp359': 20, 'b478': 6})
        self.assertEqual(len(districts), 44)
        self.assertEqual(figure_sha, '5359463896b41004e99ae21a350d2987ef3a20a09e5dafe5b133e33703b57fb6')
        commodities = {
            row['commodity']
            for mine in mines
            for evidence in mine['evidence']
            for row in evidence['measurements']
        }
        self.assertEqual(commodities, {'Au', 'Ag', 'Cu', 'Pb', 'Zn'})
        self.assertNotIn('historic_values', json.dumps(mines))

    def test_sources_are_checksum_pinned_official_usgs_documents(self):
        self.assertEqual(set(self.sources), {'pp359', 'b478', 'pp610'})
        for source in self.sources.values():
            self.assertRegex(source['sha256'], r'^[0-9a-f]{64}$')
            self.assertGreater(source['bytes'], 10_000_000)
            self.assertGreater(source['pages'], 100)
            self.assertEqual(source['text_mode'], 'embedded')
            for field in ('document_url', 'catalog_url'):
                self.assertEqual(
                    urllib.parse.urlparse(source[field]).hostname,
                    'pubs.usgs.gov')

    def test_source_host_and_cache_path_fail_closed(self):
        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['document_url'] = 'https://example.test/report.pdf'
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'official USGS'):
            co.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['local_path'] = '../outside.pdf'
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'normalized'):
            co.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'].append(copy.deepcopy(bad['sources'][0]))
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'duplicate source_id'):
            co.validate_source_inventory(bad)

    def test_table_rows_require_reviewed_image_hash_but_narrative_rows_forbid_it(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0].pop('page_image_sha256')
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'table-page image'):
            co.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        narrative = next(
            mine['evidence'][0] for mine in bad['mines']
            if mine['evidence'][0]['source_id'] == 'b478')
        narrative['page_image_sha256'] = '0' * 64
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'narrative text'):
            co.validate_reviewed(bad, self.sources)

    def test_duplicate_mine_and_measurement_are_rejected(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][1]['mine_id'] = bad['mines'][0]['mine_id']
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'duplicate mine_id'):
            co.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        evidence = bad['mines'][0]['evidence'][0]
        evidence['measurements'].append(copy.deepcopy(evidence['measurements'][0]))
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'duplicated'):
            co.validate_reviewed(bad, self.sources)

    def test_pp610_inventory_is_exact_figure_10_not_a_famous_district_subset(self):
        districts, _ = co.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(
            [(row['name'], row['county']) for row in districts],
            list(co.PP610_FIGURE_10))
        self.assertEqual(
            [(row['pdf_page'], row['source_heading']) for row in districts],
            list(co.PP610_DESCRIPTION_LOCATORS))
        self.assertEqual(
            sum(row['source_heading'] is None for row in districts), 4)
        self.assertTrue(all(
            row['page_cite'] == f'p. {row["pdf_page"] - 6}'
            for row in districts))
        self.assertTrue(all(len(row['verbatim_quote']) >= 80
                            for row in districts))

        bad = copy.deepcopy(self.district_document)
        bad['districts'].pop()
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'all 44'):
            co.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['districts'][0], bad['districts'][1] = (
            bad['districts'][1], bad['districts'][0])
        with self.assertRaisesRegex(co.ColoradoEvidenceError, 'Figure 10 district'):
            co.validate_district_inventory(bad, self.sources)

    def test_narrative_quote_matching_is_strict_enough_to_reject_unrelated_text(self):
        quote = ('Statements of the superintendent put the average yield of this '
                 'upper ore at 21 ounces silver and 3 ounces gold.')
        page = ('The Black Crook mine was reviewed. ' + quote +
                ' The values fell with depth.')
        self.assertEqual(co.extraction.quote_match_score(quote, page), 1.0)
        self.assertLess(
            co.extraction.quote_match_score(quote, 'unrelated geology prose'), 0.2)

    def test_source_checksum_drift_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.pdf'
            path.write_bytes(b'%PDF-changed')
            source = dict(self.sources['b478'])
            source['resolved_path'] = path
            source['bytes'] = len(b'%PDF-changed')
            source['sha256'] = '0' * 64
            with self.assertRaisesRegex(co.ColoradoEvidenceError, 'source drift'):
                co.verify_pdf(source)

    def test_checked_in_build_is_national_compiler_compatible_and_evidence_only(self):
        report = load(OUTPUT / 'build.json')
        grades = load(OUTPUT / 'grades/co.json')
        pp610 = load(OUTPUT / 'pp610/co.json')
        grade_result = national.validate_grade_document(
            grades, 'CO', {}, '0' * 64)
        pp_result = national.validate_pp610_document(pp610, 'CO')
        self.assertEqual(grade_result['metrics']['graded_mines'], 26)
        self.assertEqual(grade_result['metrics']['primary_sources'], 2)
        self.assertEqual(pp_result['district_count'], 44)
        self.assertEqual(
            report['effect'], 'evidence_only_no_release_or_done_mutation')
        self.assertFalse(report['threshold_observation']['is_release_decision'])
        self.assertNotIn('"enabled"', json.dumps(report))
        for descriptor in report['artifacts']['page_indexes'].values():
            path = OUTPUT / descriptor['path']
            self.assertEqual(descriptor['bytes'], path.stat().st_size)
            self.assertEqual(descriptor['sha256'], co.sha256_file(path))

    def test_full_local_build_is_reproducible_and_does_not_publish(self):
        if not all(source['resolved_path'].is_file()
                   for source in self.sources.values()):
            self.skipTest('official checksum-pinned Colorado PDF cache is not installed')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            first_output = Path(directory) / 'first'
            second_output = Path(directory) / 'second'
            first, first_artifact = co.build(output=first_output)
            second, second_artifact = co.build(output=second_output)
            self.assertEqual(first['metrics'], second['metrics'])
            self.assertEqual(first_artifact['sha256'], second_artifact['sha256'])
            self.assertEqual(first['metrics']['graded_mines'], 26)
            self.assertEqual(first['metrics']['pp610_districts'], 44)
            self.assertEqual(first['metrics']['figure_10_page_hashes'], 1)
            self.assertEqual(first['metrics']['pp610_description_page_hashes'], 30)
            self.assertEqual(first['metrics']['pp359_table_image_pages_review_bound'], 2)
            self.assertFalse(first['threshold_observation']['is_release_decision'])


if __name__ == '__main__':
    unittest.main()
