"""Adversarial tests for the New Mexico WS11 grade evidence producer."""

import copy
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pipelines'))

import build_national_grade_evidence as national
import build_new_mexico_grade_evidence as nm


def load(path):
    with open(path, encoding='utf-8') as source:
        return json.load(source)


SOURCES_PATH = ROOT / 'pipelines/config/nm_grade_sources.json'
REVIEWED_PATH = ROOT / 'grades-research/nm/reviewed_grade_evidence.json'
DISTRICTS_PATH = ROOT / 'grades-research/nm/pp610_district_inventory.json'


class NewMexicoGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_document = load(SOURCES_PATH)
        self.reviewed_document = load(REVIEWED_PATH)
        self.district_document = load(DISTRICTS_PATH)
        self.sources = nm.validate_source_inventory(self.source_document)

    def test_reviewed_inventory_meets_explicit_threshold_observation(self):
        mines, used = nm.validate_reviewed(
            self.reviewed_document, self.sources)
        districts, figure_sha = nm.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(len(mines), 26)
        self.assertEqual(used, {'pp68', 'b870'})
        self.assertEqual({mine['mine_id'] for mine in mines},
                         nm.EXPECTED_MINE_IDS)
        self.assertEqual(len(districts), 17)
        self.assertEqual(
            [(row['district_id'], row['name'], row['county'],
              row['pdf_page'], row['source_heading']) for row in districts],
            list(nm.PP610_DISTRICTS))
        self.assertEqual(
            figure_sha,
            'fc9646c4ab5e7faeb06a9accf2dd0fac101c28a7b64f80ccfee93c04b9d5cb02')

    def test_sources_are_checksum_pinned_official_usgs_documents(self):
        self.assertEqual(set(self.sources), nm.EXPECTED_SOURCE_IDS)
        for source in self.sources.values():
            self.assertRegex(source['sha256'], r'^[0-9a-f]{64}$')
            self.assertGreater(source['bytes'], 10_000_000)
            self.assertGreater(source['pages'], 0)
            for field in ('document_url', 'catalog_url'):
                self.assertIn(
                    urllib.parse.urlparse(source[field]).hostname,
                    nm.OFFICIAL_HOSTS)
        self.assertEqual(self.sources['pp68']['text_mode'], 'embedded_scan')
        self.assertEqual(self.sources['b870']['text_mode'], 'embedded_scan')
        self.assertEqual(self.sources['pp610']['text_mode'], 'embedded')

    def test_unofficial_host_traversal_and_source_omission_fail_closed(self):
        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['document_url'] = 'https://example.test/report.pdf'
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                    'approved official'):
            nm.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['local_path'] = '../outside.pdf'
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                    'normalized relative'):
            nm.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'].pop()
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                    'exactly pp68'):
            nm.validate_source_inventory(bad)

    def test_every_scan_quote_requires_a_reviewed_page_render_hash(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0].pop('page_image_sha256')
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                    r"missing=.*page_image_sha256"):
            nm.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][1]['evidence'][0]['page_image_sha256'] = 'not-a-hash'
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError, 'SHA-256'):
            nm.validate_reviewed(bad, self.sources)

    def test_target_identity_and_source_diversity_cannot_be_reduced(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['mine_id'] = 'nm-unreviewed-substitute'
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                    'identity set is incomplete'):
            nm.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        for mine in bad['mines']:
            for evidence in mine['evidence']:
                if evidence['source_id'] == 'b870':
                    evidence['source_id'] = 'pp68'
                    evidence['pdf_page'] = 385
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                    'consume both'):
            nm.validate_reviewed(bad, self.sources)

    def test_pp610_inventory_is_exact_ordered_figure_19_set(self):
        bad = copy.deepcopy(self.district_document)
        bad['districts'].pop()
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError, 'all 17'):
            nm.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['districts'][0], bad['districts'][1] = (
            bad['districts'][1], bad['districts'][0])
        with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                    'reviewed Figure 19'):
            nm.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['figure_page_image_sha256'] = '0' * 64
        _, figure_sha = nm.validate_district_inventory(bad, self.sources)
        self.assertEqual(figure_sha, '0' * 64)

    def test_ranges_are_not_averaged_and_traces_are_not_zero(self):
        mines, _ = nm.validate_reviewed(self.reviewed_document, self.sources)
        by_id = {mine['mine_id']: mine for mine in mines}
        new_era = by_id['nm-new-era-mine']['evidence'][0]
        self.assertEqual(
            {row['commodity']: row['value']
             for row in new_era['measurements']}, {'Cu': 17, 'Ag': 161})
        self.assertIn('161 to 256', new_era['verbatim_quote'])

        owl = by_id['nm-owl-mine']['evidence'][0]
        self.assertEqual(
            {row['commodity']: row['value'] for row in owl['measurements']},
            {'Pb': 0.5, 'Zn': 1.2})
        self.assertNotIn('Au', {row['commodity'] for row in owl['measurements']})
        self.assertNotIn('Ag', {row['commodity'] for row in owl['measurements']})

    def test_source_checksum_drift_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.pdf'
            path.write_bytes(b'%PDF-changed')
            source = dict(self.sources['b870'])
            source['resolved_path'] = path
            source['bytes'] = len(b'%PDF-changed')
            source['sha256'] = '0' * 64
            with self.assertRaisesRegex(nm.NewMexicoEvidenceError,
                                        'source drift'):
                nm.verify_pdf(source)

    def test_checked_in_manifests_validate_without_pdf_cache(self):
        self.assertEqual(nm.check_inputs(), {
            'mines': 26, 'grade_sources': 2, 'pp610_districts': 17})

    def test_full_build_matches_national_contract_and_never_releases(self):
        if not all(self.sources[source_id]['resolved_path'].is_file()
                   for source_id in nm.EXPECTED_SOURCE_IDS):
            self.skipTest('official checksum-pinned PDF cache is not installed')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            output = Path(directory) / 'nm-grade-evidence'
            report, artifact = nm.build(output=output)
            self.assertEqual(report['metrics']['graded_mines'], 26)
            self.assertEqual(report['metrics']['primary_sources'], 2)
            self.assertEqual(report['metrics']['verbatim_quotes'], 26)
            self.assertEqual(report['metrics']['page_cites'], 26)
            self.assertEqual(report['metrics']['pp610_districts'], 17)
            self.assertEqual(report['metrics']['scan_image_pages_review_bound'],
                             17)
            self.assertEqual(
                report['effect'], 'evidence_only_no_release_or_done_mutation')
            self.assertFalse(
                report['threshold_observation']['is_release_decision'])
            self.assertNotIn('"enabled"', json.dumps(report))
            self.assertNotIn('"done"', json.dumps(report).lower())
            self.assertEqual(
                artifact['sha256'], nm.sha256_file(output / 'build.json'))

            grades = load(output / 'grades/nm.json')
            pp610 = load(output / 'pp610/nm.json')
            grade_result = national.validate_grade_document(
                grades, 'NM', {}, '0' * 64)
            pp_result = national.validate_pp610_document(pp610, 'NM')
            self.assertEqual(grade_result['metrics']['graded_mines'], 26)
            self.assertEqual(grade_result['metrics']['primary_sources'], 2)
            self.assertEqual(pp_result['district_count'], 17)
            for descriptor in report['artifacts']['page_indexes'].values():
                page_index = output / descriptor['path']
                self.assertEqual(
                    descriptor['sha256'], nm.sha256_file(page_index))


if __name__ == '__main__':
    unittest.main()
