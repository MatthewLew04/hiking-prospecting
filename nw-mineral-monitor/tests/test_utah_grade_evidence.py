"""Adversarial tests for the Utah WS11 evidence-only producer."""

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

import build_national_grade_evidence as national
import build_utah_grade_evidence as ut


def load(path):
    with open(path, encoding='utf-8') as source:
        return json.load(source)


SOURCES_PATH = ROOT / 'pipelines/config/ut_grade_sources.json'
REVIEWED_PATH = ROOT / 'grades-research/ut/reviewed_grade_evidence.json'
DISTRICTS_PATH = ROOT / 'grades-research/ut/pp610_district_inventory.json'
OUTPUT = ROOT / 'build-inputs/ws9/ut-grade-evidence'


class UtahGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_document = load(SOURCES_PATH)
        self.reviewed_document = load(REVIEWED_PATH)
        self.district_document = load(DISTRICTS_PATH)
        self.sources = ut.validate_source_inventory(self.source_document)

    def test_reviewed_inventory_meets_bar_with_native_measurements_only(self):
        mines, used = ut.validate_reviewed(
            self.reviewed_document, self.sources)
        districts, figure_sha = ut.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(len(mines), 26)
        self.assertEqual(used, {'pp38', 'pp107', 'pp177'})
        self.assertEqual(Counter(
            evidence['source_id']
            for mine in mines for evidence in mine['evidence']),
            {'pp38': 17, 'pp107': 6, 'pp177': 3})
        self.assertEqual(len({
            (evidence['source_id'], evidence['pdf_page'])
            for mine in mines for evidence in mine['evidence']}), 23)
        self.assertEqual(len(districts), 13)
        self.assertEqual(
            figure_sha,
            '4872182f35a98df033ebd31c70a26e25a14c4a672f8b5059751500fb433dceb6')
        self.assertEqual({
            measurement['commodity']
            for mine in mines
            for evidence in mine['evidence']
            for measurement in evidence['measurements']
        }, {'Au', 'Ag', 'Cu', 'Pb', 'Zn'})
        self.assertNotIn('historic_values', json.dumps(mines))
        self.assertTrue(all(
            evidence['quote_verbatim'] is True and
            evidence['page_cite'].startswith('p. ')
            for mine in mines for evidence in mine['evidence']))

    def test_reviewed_corrections_preserve_source_text_and_page_identity(self):
        mines, _ = ut.validate_reviewed(
            self.reviewed_document, self.sources)
        by_id = {mine['mine_id']: mine['evidence'][0] for mine in mines}

        highland = by_id['ut-highland-boy-mine']
        self.assertIn('and 3 per cent silver.', highland['verbatim_quote'])
        self.assertEqual(
            {row['commodity'] for row in highland['measurements']}, {'Cu'})

        winamuck = by_id['ut-winamuck-mine']
        self.assertIn('ran abut 38 per cent lead', winamuck['verbatim_quote'])

        victoria = by_id['ut-victoria-mine']
        self.assertEqual(
            (victoria['source_id'], victoria['pdf_page'],
             victoria['page_cite']), ('pp107', 203, 'p. 174'))
        self.assertIn('6,980 ounces of silver', victoria['verbatim_quote'])

        rube = by_id['ut-rube-mine']
        baltimore = by_id['ut-new-baltimore-mine']
        self.assertEqual((rube['pdf_page'], rube['page_cite']),
                         (152, 'p. 136'))
        self.assertEqual((baltimore['pdf_page'], baltimore['page_cite']),
                         (166, 'p. 149'))

    def test_sources_are_checksum_pinned_official_usgs_documents(self):
        self.assertEqual(set(self.sources), ut.EXPECTED_SOURCE_IDS)
        self.assertEqual(
            {source_id: source['sha256']
             for source_id, source in self.sources.items()}, {
                'pp38': '494caf06f1ff193c09c57f0d1bc64fbbd3325818c2355fa97d8a14f5fccf678a',
                'pp107': '536882df1f3ef475aa972d85b20e4a57e98666d3e7cee347e1d395b4c05764d5',
                'pp177': '958dcf7c2e2927a845fbec56682ae9ed3273efc93eafd1c8cca50d9a372eb7da',
                'pp610': 'f4c1f048aaffe1e8d1431983e0a7b3f1bb543fab0f5380cd42e85c0a6a840896',
            })
        for source in self.sources.values():
            self.assertGreater(source['bytes'], 10_000_000)
            self.assertGreater(source['pages'], 100)
            for field in ('document_url', 'catalog_url'):
                self.assertEqual(
                    urllib.parse.urlparse(source[field]).hostname,
                    'pubs.usgs.gov')
        for source_id in ut.EXPECTED_GRADE_SOURCE_IDS:
            self.assertEqual(self.sources[source_id]['text_mode'], 'ocr')
        self.assertEqual(self.sources['pp610']['text_mode'], 'embedded')

    def test_unofficial_host_cache_traversal_and_source_omission_fail_closed(self):
        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['document_url'] = 'https://example.test/report.pdf'
        with self.assertRaisesRegex(ut.UtahEvidenceError, 'official USGS'):
            ut.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['local_path'] = '../outside.pdf'
        with self.assertRaisesRegex(ut.UtahEvidenceError, 'normalized'):
            ut.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'].pop()
        with self.assertRaisesRegex(ut.UtahEvidenceError,
                                    'exactly pp38, pp107, pp177, and pp610'):
            ut.validate_source_inventory(bad)

    def test_page_hash_duplicate_identity_and_page_rebinding_fail_closed(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0].pop('page_image_sha256')
        with self.assertRaisesRegex(ut.UtahEvidenceError,
                                    "missing=.*page_image_sha256"):
            ut.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][1]['mine_id'] = bad['mines'][0]['mine_id']
        with self.assertRaisesRegex(ut.UtahEvidenceError, 'duplicate mine_id'):
            ut.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['pdf_page'] += 1
        with self.assertRaisesRegex(ut.UtahEvidenceError,
                                    'source/PDF/printed-page binding changed'):
            ut.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['measurements'].append(
            copy.deepcopy(bad['mines'][0]['evidence'][0]['measurements'][0]))
        with self.assertRaisesRegex(ut.UtahEvidenceError, 'duplicated'):
            ut.validate_reviewed(bad, self.sources)

    def test_pp610_inventory_is_the_complete_ordered_figure_25_legend(self):
        districts, _ = ut.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(
            [(row['district_id'], row['name'], row['county'],
              row['verbatim_quote']) for row in districts],
            list(ut.PP610_FIGURE_25))
        self.assertTrue(all(
            row['pdf_page'] == 247 and
            row['page_cite'] == 'Figure 25, p. 241'
            for row in districts))

        bad = copy.deepcopy(self.district_document)
        bad['districts'].pop()
        with self.assertRaisesRegex(ut.UtahEvidenceError, 'all 13'):
            ut.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['districts'][0], bad['districts'][1] = (
            bad['districts'][1], bad['districts'][0])
        with self.assertRaisesRegex(ut.UtahEvidenceError,
                                    'Figure 25 district'):
            ut.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['figure_page_image_sha256'] = '0' * 64
        _, changed_sha = ut.validate_district_inventory(bad, self.sources)
        self.assertEqual(changed_sha, '0' * 64)

    def test_quote_cross_check_tolerates_scan_noise_not_unrelated_text(self):
        quote = ('Raymond after Daggett states that 1,300 tons ran abut 38 per '
                 'cent lead and 56 ounces silver.')
        ocr = ('Raymond after Daggett states that 1,300 tons ran abut 38 per '
               'cent load and 56 ounces silver')
        self.assertGreaterEqual(ut.quote_match_score(quote, ocr), 0.85)
        self.assertLess(
            ut.quote_match_score(quote, 'unrelated geology prose'), 0.2)

    def test_source_checksum_drift_is_rejected_before_page_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.pdf'
            path.write_bytes(b'%PDF-changed')
            source = dict(self.sources['pp38'])
            source['resolved_path'] = path
            source['bytes'] = len(b'%PDF-changed')
            source['sha256'] = '0' * 64
            with self.assertRaisesRegex(ut.UtahEvidenceError, 'source drift'):
                ut.verify_pdf(source)

    def test_checked_in_manifests_validate_without_pdf_cache(self):
        self.assertEqual(ut.check_inputs(), {
            'mines': 26, 'grade_sources': 3, 'pp610_districts': 13})

    def test_checked_in_build_is_national_compatible_and_evidence_only(self):
        report = load(OUTPUT / 'build.json')
        grades = load(OUTPUT / 'grades/ut.json')
        pp610 = load(OUTPUT / 'pp610/ut.json')
        grade_result = national.validate_grade_document(
            grades, 'UT', {}, '0' * 64)
        pp_result = national.validate_pp610_document(pp610, 'UT')
        self.assertEqual(grade_result['metrics']['graded_mines'], 26)
        self.assertEqual(grade_result['metrics']['primary_sources'], 3)
        self.assertEqual(pp_result['district_count'], 13)
        self.assertEqual(
            report['effect'], 'evidence_only_no_release_or_done_mutation')
        self.assertTrue(
            report['threshold_observation']['at_least_25_graded_mines'])
        self.assertTrue(
            report['threshold_observation']['at_least_2_primary_sources'])
        self.assertTrue(
            report['threshold_observation']['complete_pp610_anchor'])
        self.assertFalse(report['threshold_observation']['is_release_decision'])
        self.assertNotIn('"enabled"', json.dumps(report))
        self.assertNotIn('"done"', json.dumps(report).lower())
        for descriptor in report['artifacts']['page_indexes'].values():
            path = OUTPUT / descriptor['path']
            self.assertEqual(descriptor['bytes'], path.stat().st_size)
            self.assertEqual(descriptor['sha256'], ut.sha256_file(path))

    def test_two_full_local_builds_are_byte_identical_and_do_not_publish(self):
        if not all(source['resolved_path'].is_file()
                   for source in self.sources.values()):
            self.skipTest('official checksum-pinned Utah PDF cache is not installed')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            first_output = Path(directory) / 'first'
            second_output = Path(directory) / 'second'
            first, first_artifact = ut.build(output=first_output)
            second, second_artifact = ut.build(output=second_output)
            self.assertEqual(first['metrics'], second['metrics'])
            self.assertEqual(first_artifact['sha256'], second_artifact['sha256'])
            self.assertEqual(first['metrics']['graded_mines'], 26)
            self.assertEqual(first['metrics']['primary_sources'], 3)
            self.assertEqual(first['metrics']['pp610_districts'], 13)
            self.assertEqual(first['metrics']['scan_image_pages_review_bound'],
                             23)
            self.assertFalse(first['threshold_observation']['is_release_decision'])

            def tree(path):
                return {
                    str(item.relative_to(path)): (item.stat().st_size,
                                                  ut.sha256_file(item))
                    for item in sorted(path.rglob('*')) if item.is_file()
                }

            self.assertEqual(tree(first_output), tree(second_output))
            self.assertFalse((ROOT / 'site/grades/ut.json').exists())


if __name__ == '__main__':
    unittest.main()
