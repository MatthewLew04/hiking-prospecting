"""Adversarial tests for the Alaska WS11 evidence-only producer."""

import copy
import json
import sys
import tempfile
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pipelines'))

import build_alaska_grade_evidence as ak
import build_national_grade_evidence as national


def load(path):
    with open(path, encoding='utf-8') as source:
        return json.load(source)


SOURCES_PATH = ROOT / 'pipelines/config/ak_grade_sources.json'
REVIEWED_PATH = ROOT / 'grades-research/ak/reviewed_grade_evidence.json'
DISTRICTS_PATH = ROOT / 'grades-research/ak/pp610_district_inventory.json'
ARDF_PATH = ROOT / 'grades-research/ak/ardf_target_crosswalk.json'
OUTPUT = ROOT / 'build-inputs/ws9/ak-grade-evidence'


class AlaskaGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_document = load(SOURCES_PATH)
        self.reviewed_document = load(REVIEWED_PATH)
        self.district_document = load(DISTRICTS_PATH)
        self.ardf_document = load(ARDF_PATH)
        self.sources = ak.validate_source_inventory(self.source_document)

    def test_reviewed_inventory_meets_exact_evidence_bar(self):
        mines, used = ak.validate_reviewed(self.reviewed_document, self.sources)
        districts, figure_sha = ak.validate_district_inventory(
            self.district_document, self.sources)
        ardf = ak.validate_ardf_crosswalk(
            self.ardf_document, [mine['mine_id'] for mine in mines])
        self.assertEqual(len(mines), 26)
        self.assertEqual(tuple(mine['mine_id'] for mine in mines), ak.MINE_IDS)
        self.assertEqual(used, {'atdm-mr191-5', 'usbm-ofr50-94'})
        self.assertEqual(Counter(
            evidence['source_id']
            for mine in mines for evidence in mine['evidence']),
            {'usbm-ofr50-94': 18, 'atdm-mr191-5': 8})
        self.assertEqual(len({
            (evidence['source_id'], evidence['pdf_page'])
            for mine in mines for evidence in mine['evidence']}), 24)
        self.assertTrue(all(
            any(character.isdigit() for character in evidence['page_cite'])
            and evidence['quote_verbatim'] is True
            and len(evidence['page_image_sha256']) == 64
            for mine in mines for evidence in mine['evidence']))
        self.assertEqual(len(districts), 43)
        self.assertEqual(figure_sha, ak.PP610_FIGURE_5_IMAGE_SHA256)
        self.assertEqual(
            (ardf['linked'], ardf['unique_linked_records'], ardf['unmatched']),
            (23, 21, 3))

    def test_sources_are_exact_checksum_pinned_official_documents(self):
        self.assertEqual(set(self.sources), ak.SOURCE_IDS)
        for source_id, source in self.sources.items():
            self.assertEqual(
                (source['bytes'], source['sha256'], source['pages'],
                 source['text_mode']), ak.SOURCE_PINS[source_id])
            for field in ('document_url', 'catalog_url'):
                self.assertIn(
                    urllib.parse.urlparse(source[field]).hostname,
                    ak.OFFICIAL_HOSTS)

    def test_unofficial_host_traversal_source_omission_and_repin_fail_closed(self):
        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['document_url'] = 'https://example.test/report.pdf'
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'approved official'):
            ak.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['local_path'] = '../outside.pdf'
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'normalized'):
            ak.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'].pop()
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'exactly both'):
            ak.validate_source_inventory(bad)

        bad = copy.deepcopy(self.source_document)
        bad['sources'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'reviewed source pin'):
            ak.validate_source_inventory(bad)

    def test_every_grade_quote_requires_a_numbered_cite_and_page_render_hash(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0].pop('page_image_sha256')
        with self.assertRaisesRegex(ak.AlaskaEvidenceError,
                                    r'missing=.*page_image_sha256'):
            ak.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['page_image_sha256'] = 'not-a-hash'
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'SHA-256'):
            ak.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['page_cite'] = 'unnumbered page'
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'numbered page'):
            ak.validate_reviewed(bad, self.sources)

    def test_target_identity_and_two_source_diversity_cannot_be_reduced(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['mine_id'] = 'ak-unreviewed-substitute'
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'identities/order'):
            ak.validate_reviewed(bad, self.sources)

        bad = copy.deepcopy(self.reviewed_document)
        for mine in bad['mines']:
            evidence = mine['evidence'][0]
            if evidence['source_id'] == 'atdm-mr191-5':
                evidence['source_id'] = 'usbm-ofr50-94'
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'both official'):
            ak.validate_reviewed(bad, self.sources)

    def test_unsupported_nickel_and_pge_values_remain_quote_only(self):
        mines, _ = ak.validate_reviewed(self.reviewed_document, self.sources)
        measurements = [
            measurement
            for mine in mines
            for evidence in mine['evidence']
            for measurement in evidence['measurements']
        ]
        self.assertTrue({row['commodity'] for row in measurements}
                        <= {'Au', 'Ag', 'Cu', 'Pb', 'Zn', 'Fe'})
        self.assertFalse({'Ni', 'Pd', 'Pt', 'PGE'} &
                         {row['commodity'] for row in measurements})
        quote_text = ' '.join(
            evidence['verbatim_quote']
            for mine in mines for evidence in mine['evidence']).lower()
        self.assertIn('nickel', quote_text)

    def test_ardf_backbone_rejects_record_drift_and_guessed_links(self):
        mines, _ = ak.validate_reviewed(self.reviewed_document, self.sources)
        mine_ids = [mine['mine_id'] for mine in mines]

        bad = copy.deepcopy(self.ardf_document)
        bad['records'][0]['site'] = 'invented substitute'
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'canonical SHA-256'):
            ak.validate_ardf_crosswalk(bad, mine_ids)

        bad = copy.deepcopy(self.ardf_document)
        bad['records'][0]['site'] = 'invented substitute'
        bad['records_sha256'] = ak.sha256_bytes(ak.canonical_bytes(bad['records']))
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'record-set hash'):
            ak.validate_ardf_crosswalk(bad, mine_ids)

        bad = copy.deepcopy(self.ardf_document)
        saddle = next(row for row in bad['targets']
                      if row['mine_id'] == 'ak-saddle-occurrence')
        saddle.clear()
        saddle.update({
            'mine_id': 'ak-saddle-occurrence', 'status': 'linked',
            'ardf_no': 'AN144', 'match_basis': 'guessed from a generic name'})
        with self.assertRaisesRegex(ak.AlaskaEvidenceError,
                                    'preserve the explicit no-match'):
            ak.validate_ardf_crosswalk(bad, mine_ids)

    def test_pp610_inventory_is_exact_ordered_complete_figure_5(self):
        districts, _ = ak.validate_district_inventory(
            self.district_document, self.sources)
        self.assertEqual(
            [(row['district_id'], row['name'], row['region']) for row in districts],
            list(ak.PP610_FIGURE_5))
        self.assertEqual([row['figure_number'] for row in districts],
                         list(range(1, 44)))

        bad = copy.deepcopy(self.district_document)
        bad['districts'].pop()
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'all 43'):
            ak.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['districts'][0], bad['districts'][1] = (
            bad['districts'][1], bad['districts'][0])
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'Figure 5 district'):
            ak.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['figure_page_image_sha256'] = '0' * 64
        with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'review hash changed'):
            ak.validate_district_inventory(bad, self.sources)

        bad = copy.deepcopy(self.district_document)
        bad['districts'][1]['verbatim_quote'] = bad['districts'][0]['verbatim_quote']
        with self.assertRaisesRegex(ak.AlaskaEvidenceError,
                                    'quote does not bind|duplicates'):
            ak.validate_district_inventory(bad, self.sources)

    def test_valid_but_drifted_page_hash_fails_at_build_boundary(self):
        bad = copy.deepcopy(self.reviewed_document)
        bad['mines'][0]['evidence'][0]['page_image_sha256'] = 'f' * 64
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            reviewed = Path(directory) / 'reviewed.json'
            reviewed.write_text(json.dumps(bad), encoding='utf-8')
            fake_page = {
                'raw': b'Blackjack diagnostic quote text',
                'text': bad['mines'][0]['evidence'][0]['verbatim_quote'],
                'page_text_sha256': '1' * 64,
                'page_image_sha256': '0' * 64,
            }
            with mock.patch.object(ak, 'verify_pdf'), mock.patch.object(
                    ak, 'page_record', return_value=fake_page):
                with self.assertRaisesRegex(ak.AlaskaEvidenceError,
                                            'page-image SHA-256 changed'):
                    ak.build(reviewed_path=reviewed,
                             output=Path(directory) / 'output')

    def test_source_checksum_drift_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.pdf'
            path.write_bytes(b'%PDF-changed')
            source = dict(self.sources['usbm-ofr50-94'])
            source['resolved_path'] = path
            source['bytes'] = len(b'%PDF-changed')
            source['sha256'] = '0' * 64
            with self.assertRaisesRegex(ak.AlaskaEvidenceError, 'source drift'):
                ak.verify_pdf(source)

    def test_checked_manifests_validate_without_pdf_cache(self):
        self.assertEqual(ak.check_inputs(), {
            'mines': 26,
            'grade_sources': 2,
            'pp610_districts': 43,
            'ardf_targets_linked': 23,
            'ardf_unique_records': 21,
            'ardf_explicit_unmatched_findings': 3,
        })
        # Validation is manifest-only: a normalized but absent cache path is
        # acceptable to check and will be required only by fetch/build.
        changed = copy.deepcopy(self.source_document)
        for row in changed['sources']:
            row['local_path'] = (
                f'pipelines/cache/ak-grade-sources/absent-{row["source_id"]}.pdf')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            sources = Path(directory) / 'sources.json'
            sources.write_text(json.dumps(changed), encoding='utf-8')
            self.assertEqual(ak.check_inputs(sources_path=sources)['mines'], 26)

    def test_canonical_private_build_is_national_compatible_and_evidence_only(self):
        report = load(OUTPUT / 'build.json')
        grades = load(OUTPUT / 'grades/ak.json')
        pp610 = load(OUTPUT / 'pp610/ak.json')
        ardf = load(OUTPUT / 'backbone/ak-ardf-crosswalk.json')
        grade_result = national.validate_grade_document(
            grades, 'AK', {}, '0' * 64)
        pp_result = national.validate_pp610_document(pp610, 'AK')
        self.assertEqual(grade_result['metrics']['graded_mines'], 26)
        self.assertEqual(grade_result['metrics']['primary_sources'], 2)
        self.assertEqual(pp_result['district_count'], 43)
        self.assertEqual(len(ardf['records']), 21)
        self.assertEqual(report['effect'],
                         'evidence_only_no_release_or_done_mutation')
        self.assertFalse(report['threshold_observation']['is_release_decision'])
        self.assertNotIn('"enabled"', json.dumps(report))
        self.assertNotIn('"done"', json.dumps(report).lower())
        for descriptor in report['artifacts']['page_indexes'].values():
            path = OUTPUT / descriptor['path']
            self.assertEqual(descriptor['bytes'], path.stat().st_size)
            self.assertEqual(descriptor['sha256'], ak.sha256_file(path))

    def test_full_local_build_is_byte_reproducible(self):
        if not all(source['resolved_path'].is_file()
                   for source in self.sources.values()):
            self.skipTest('official checksum-pinned Alaska PDF cache is not installed')
        build_parent = ROOT / 'build-inputs'
        build_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_parent) as directory:
            first_output = Path(directory) / 'first'
            second_output = Path(directory) / 'second'
            first, first_artifact = ak.build(output=first_output)
            second, second_artifact = ak.build(output=second_output)
            self.assertEqual(first['metrics'], second['metrics'])
            self.assertEqual(first_artifact['sha256'], second_artifact['sha256'])
            first_files = sorted(path.relative_to(first_output)
                                 for path in first_output.rglob('*') if path.is_file())
            second_files = sorted(path.relative_to(second_output)
                                  for path in second_output.rglob('*') if path.is_file())
            self.assertEqual(first_files, second_files)
            for relative in first_files:
                self.assertEqual((first_output / relative).read_bytes(),
                                 (second_output / relative).read_bytes())
            self.assertEqual(first['metrics']['grade_page_images_review_bound'], 24)
            self.assertEqual(first['metrics']['ardf_targets_linked'], 23)
            self.assertFalse(first['threshold_observation']['is_release_decision'])


if __name__ == '__main__':
    unittest.main()
