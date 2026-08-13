"""Adversarial tests for the exact-49 target-scoring evidence compiler."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_national_target_scoring_evidence as scoring


def _sha(seed):
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()


class Fixture:
    def __init__(self, root):
        self.root = root
        self.private = os.path.join(root, 'private-scoring')
        self.publish = os.path.join(root, 'reviewed-output')
        self.inventory_path = os.path.join(self.private, 'inventory.json')
        self.registry, _ = scoring._registry_context()
        self.method_id = 'ws11-richopen-reviewed-v1'
        self.documents = {}
        self.states = {}
        os.makedirs(self.private)
        for code in sorted(self.registry):
            self._make_state(code)
        self.inventory = {
            'schema_version': 1,
            'dataset': scoring.DATASET,
            'snapshot': '2026-08-13',
            'method_id': self.method_id,
            'review': self._review(),
            'states': self.states,
        }
        self.write_inventory()

    @staticmethod
    def _review():
        return {
            'status': 'reviewed',
            'reviewed_on': '2026-08-13',
            'reviewed_by': 'Fixture evidence review board',
        }

    def _common(self, code, kind):
        return {
            'schema_version': 1,
            'state': code,
            'kind': kind,
            'complete': True,
            'truncated': False,
            'method_id': self.method_id,
            'review': self._review(),
            'source_urls': [f'https://example.test/{code.lower()}/{kind}'],
        }

    @staticmethod
    def _targets(code):
        return [
            {
                'target_id': f'{code.lower()}-target-1',
                'name': f'{code} ranked target one',
                'declared_rank': 1,
                'area_km2': 12.0,
                'longitude': -100.0,
                'latitude': 40.0,
                'district': f'{code} district one',
            },
            {
                'target_id': f'{code.lower()}-target-2',
                'name': f'{code} ranked target two',
                'declared_rank': 2,
                'area_km2': 8.0,
                'longitude': -99.5,
                'latitude': 40.5,
            },
        ]

    def _terms(self, code, kind, scores):
        source_sha = _sha(f'{code}-{kind}-source')
        value = self._common(code, kind)
        value.update({
            'source_artifact_sha256': source_sha,
            'targets': [
                {
                    'target_id': f'{code.lower()}-target-{index}',
                    'score': score,
                    'terms': [f'{kind} reviewed term {index}'],
                    'evidence_refs': [source_sha],
                    'rationale': (
                        f'Reviewed {kind} evidence supports target {index}.'),
                }
                for index, score in enumerate(scores, 1)
            ],
        })
        return value

    def _make_state(self, code):
        ranking = self._common(code, 'ranked_targets')
        ranking['targets'] = self._targets(code)
        grade = self._terms(code, 'grade_terms', (12.0, 4.0))
        geology = self._terms(code, 'geology_terms', (18.0, 5.0))
        documents = {
            'ranked_targets': ranking,
            'grade_terms': grade,
            'geology_terms': geology,
        }
        if self.registry[code]['regime'] == 'claim':
            snapshots = {
                system: _sha(f'{code}-{system}-open-ground-snapshot')
                for system in self.registry[code]['claim_systems']
            }
            open_ground = self._common(code, 'open_ground')
            open_ground.update({
                'coverage_status': 'statewide_complete',
                'all_ranked_targets_covered': True,
                'source_snapshot_sha256s': snapshots,
                'targets': [
                    {
                        'target_id': f'{code.lower()}-target-1',
                        'status': 'measured',
                        'value': 0.4,
                        'unit': 'fraction',
                        'score': 8.0,
                        'evidence_refs': sorted(snapshots.values()),
                        'rationale': 'Complete reviewed open-ground intersection.',
                    },
                    {
                        'target_id': f'{code.lower()}-target-2',
                        'status': 'measured',
                        'value': 0.0,
                        'unit': 'fraction',
                        'score': 0.0,
                        'evidence_refs': sorted(snapshots.values()),
                        'rationale': 'Complete review measured numeric zero open ground.',
                    },
                ],
            })
            documents['open_ground'] = open_ground
        else:
            snapshots = {
                name: _sha(f'{code}-{name}-land-snapshot')
                for name in ('surface', 'mineral', 'leasing_or_title')
            }
            land = self._common(code, 'land_context')
            land.update({
                'coverage_status': 'per_target_complete',
                'source_snapshot_sha256s': snapshots,
                'targets': [
                    {
                        'target_id': f'{code.lower()}-target-{index}',
                        'open_ground': {
                            'status': 'not_applicable',
                            'value': None,
                            'unit': None,
                            'score': None,
                            'display': 'N/A',
                        },
                        'surface': {
                            'class': 'state',
                            'party': f'{code} state land authority',
                            'evidence_sha256': snapshots['surface'],
                        },
                        'mineral': {
                            'class': 'state',
                            'party': f'{code} state mineral authority',
                            'confidence': 'verified',
                            'evidence_sha256': snapshots['mineral'],
                        },
                        'approach': {
                            'kind': 'state_lease',
                            'party': f'{code} state mineral leasing office',
                            'portal_url': f'https://example.test/{code.lower()}/lease',
                            'evidence_sha256': snapshots['leasing_or_title'],
                        },
                        'rationale': (
                            'Surface and mineral ownership were independently reviewed.'),
                    }
                    for index in (1, 2)
                ],
            })
            documents['land_context'] = land

        self.states[code] = {}
        for kind, value in documents.items():
            relative = f'states/{code.lower()}/{kind}.json'
            self._write(relative, value)
            self.documents[(code, kind)] = copy.deepcopy(value)
            self.states[code][kind] = self._descriptor(relative)

    def _write(self, relative, value):
        path = os.path.join(self.private, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as target:
            json.dump(
                value, target, sort_keys=True, separators=(',', ':'),
                ensure_ascii=False, allow_nan=False)
        return path

    def _descriptor(self, relative):
        path = os.path.join(self.private, relative)
        return {
            'path': relative,
            'bytes': os.path.getsize(path),
            'sha256': scoring.sha256_file(path),
        }

    def write_inventory(self):
        self._write('inventory.json', self.inventory)

    def rewrite(self, code, kind, mutate):
        value = copy.deepcopy(self.documents[(code, kind)])
        mutate(value)
        relative = self.states[code][kind]['path']
        self._write(relative, value)
        self.documents[(code, kind)] = copy.deepcopy(value)
        self.states[code][kind] = self._descriptor(relative)
        self.inventory['states'] = self.states
        self.write_inventory()

    def published_state(self, code):
        checked = scoring.validate_pointer(self.publish)
        descriptor = checked['run']['state_evidence'][code]
        with open(os.path.join(self.publish, descriptor['file']),
                  encoding='utf-8') as source:
            return json.load(source), descriptor


class NationalTargetScoringEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_49_publication_is_content_addressed_and_evidence_only(self):
        result = scoring.build(
            self.fixture.inventory_path, self.fixture.publish)
        self.assertEqual(result['states'], 49)
        self.assertEqual(result['claim_states'], 19)
        self.assertEqual(result['non_claim_states'], 30)
        self.assertEqual(result['targets'], 98)
        checked = scoring.validate_pointer(self.fixture.publish)
        self.assertEqual(
            set(checked['run']['state_evidence']), set(self.fixture.registry))

        nv, nv_ref = self.fixture.published_state('NV')
        ny, _ = self.fixture.published_state('NY')
        self.assertEqual(nv['regime'], 'claim')
        self.assertEqual(nv['targets'][1]['open_ground']['status'], 'measured')
        self.assertEqual(nv['targets'][1]['open_ground']['value'], 0.0)
        self.assertEqual(nv['targets'][1]['open_ground']['score'], 0.0)
        self.assertEqual(ny['regime'], 'non_claim')
        self.assertEqual(ny['targets'][0]['open_ground'], {
            'status': 'not_applicable', 'value': None, 'unit': None,
            'score': None, 'display': 'N/A'})
        self.assertEqual(ny['targets'][0]['score']['total'],
                         ny['targets'][0]['score']['grade'] +
                         ny['targets'][0]['score']['geology'])
        self.assertIsNotNone(ny['targets'][0]['land_context'])

        # Every output carries the exact source bytes/hash bindings needed by
        # a later DONE-gate reconciler; private staging paths are omitted.
        expected = self.fixture.states['NV']
        self.assertEqual(set(nv['input_artifacts']), set(expected))
        for name, descriptor in expected.items():
            self.assertEqual(nv['input_artifacts'][name], {
                'bytes': descriptor['bytes'], 'sha256': descriptor['sha256']})
        self.assertEqual(
            os.path.basename(nv_ref['file']), f'{nv_ref["sha256"]}.json')
        self.assertEqual(
            nv_ref['bytes'],
            os.path.getsize(os.path.join(self.fixture.publish, nv_ref['file'])))

        encoded = json.dumps(checked, sort_keys=True)
        self.assertNotIn('"enabled"', encoded)
        self.assertNotIn('.pmtiles', encoded)
        self.assertNotIn('.geojson', encoded)
        self.assertEqual(checked['run']['effect'],
                         'evidence_only_no_release_mutation')

    def test_alaska_requires_both_registry_claim_system_snapshots(self):
        document = self.fixture.documents[('AK', 'open_ground')]
        self.assertEqual(
            set(document['source_snapshot_sha256s']),
            {'federal_mlrs', 'alaska_state_claims'})
        document = copy.deepcopy(document)
        document['source_snapshot_sha256s'].pop('alaska_state_claims')
        with self.assertRaisesRegex(scoring.PublicationError,
                                    'exact registry claim systems'):
            scoring.validate_open_ground(
                document, 'AK', self.fixture.method_id, '2026-08-13',
                self.fixture.registry['AK']['claim_systems'])

        document = copy.deepcopy(
            self.fixture.documents[('AK', 'open_ground')])
        document['targets'][0]['evidence_refs'] = [
            document['source_snapshot_sha256s']['federal_mlrs']]
        with self.assertRaisesRegex(scoring.PublicationError,
                                    'every pinned claim-system snapshot'):
            scoring.validate_open_ground(
                document, 'AK', self.fixture.method_id, '2026-08-13',
                self.fixture.registry['AK']['claim_systems'])

        scoring.build(self.fixture.inventory_path, self.fixture.publish)
        compiled, _ = self.fixture.published_state('AK')
        self.assertEqual(
            set(compiled['regime_evidence']['open_ground'][
                'source_snapshot_sha256s']),
            {'federal_mlrs', 'alaska_state_claims'})
        tampered = copy.deepcopy(compiled)
        tampered['targets'][0]['open_ground']['evidence_refs'] = [
            tampered['regime_evidence']['open_ground'][
                'source_snapshot_sha256s']['federal_mlrs']]
        with self.assertRaisesRegex(scoring.PublicationError,
                                    'does not match regime_evidence'):
            scoring.validate_compiled_state_document(
                tampered, 'AK', self.fixture.registry['AK'],
                compiled['registry_sha256'])

    def test_missing_and_extra_state_fail_exact_scope(self):
        missing = copy.deepcopy(self.fixture.inventory)
        missing['states'].pop('AL')
        self.fixture.inventory = missing
        self.fixture.write_inventory()
        with self.assertRaisesRegex(scoring.PublicationError,
                                    r'exact registry 49: missing=\[\'AL\'\]'):
            scoring.load_inventory(self.fixture.inventory_path)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.inventory['states']['XX'] = copy.deepcopy(
                fixture.inventory['states']['AL'])
            fixture.write_inventory()
            with self.assertRaisesRegex(scoring.PublicationError,
                                        r'extra=\[\'XX\'\]'):
                scoring.load_inventory(fixture.inventory_path)

    def test_duplicate_targets_and_join_aliases_are_rejected(self):
        def duplicate(value):
            row = copy.deepcopy(value['targets'][0])
            row['declared_rank'] = 3
            value['targets'].append(row)

        self.fixture.rewrite('NV', 'ranked_targets', duplicate)
        with self.assertRaisesRegex(scoring.PublicationError, 'duplicate target'):
            scoring.build(self.fixture.inventory_path, self.fixture.publish)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'grade_terms',
                lambda value: value['targets'].__setitem__(
                    1, dict(value['targets'][1], target_id='mi-extra-target')))
            with self.assertRaisesRegex(scoring.PublicationError,
                                        'target join mismatch'):
                scoring.build(fixture.inventory_path, fixture.publish)

    def test_stale_hash_is_rejected_before_publication(self):
        path = os.path.join(
            self.fixture.private,
            self.fixture.states['NV']['grade_terms']['path'])
        with open(path, 'ab') as target:
            target.write(b' ')
        with self.assertRaisesRegex(scoring.PublicationError,
                                    'checksum/size mismatch'):
            scoring.build(self.fixture.inventory_path, self.fixture.publish)
        self.assertFalse(os.path.exists(
            os.path.join(self.fixture.publish, 'latest.json')))

    def test_bad_types_ranges_and_na_coercion_fail_closed(self):
        self.fixture.rewrite(
            'NV', 'grade_terms',
            lambda value: value['targets'][0].__setitem__('score', True))
        with self.assertRaisesRegex(scoring.PublicationError, 'must be finite'):
            scoring.build(self.fixture.inventory_path, self.fixture.publish)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'NV', 'open_ground',
                lambda value: value['targets'][0].__setitem__('value', 1.01))
            with self.assertRaisesRegex(scoring.PublicationError, r'<= 1'):
                scoring.build(fixture.inventory_path, fixture.publish)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            def coerce_na(value):
                value['targets'][0]['open_ground'].update({
                    'status': 'measured', 'value': 0, 'unit': 'fraction',
                    'score': 0, 'display': '0%'})
            fixture.rewrite('MI', 'land_context', coerce_na)
            with self.assertRaisesRegex(scoring.PublicationError,
                                        'never numeric zero'):
                scoring.build(fixture.inventory_path, fixture.publish)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'land_context',
                lambda value: value['targets'][0]['approach'].__setitem__(
                    'kind', 'open_ground'))
            with self.assertRaisesRegex(scoring.PublicationError,
                                        'approach.kind is invalid'):
                scoring.build(fixture.inventory_path, fixture.publish)

    def test_na_and_measured_zero_have_distinct_deterministic_sort_keys(self):
        otherwise_equal = {
            'area_km2': 10.0,
            'score': {'total': 50.0},
        }
        nonclaim_na = dict(
            otherwise_equal, target_id='nonclaim-na',
            open_ground={
                'status': 'not_applicable', 'value': None,
                'score': None, 'display': 'N/A'})
        claim_zero = dict(
            otherwise_equal, target_id='claim-zero',
            open_ground={
                'status': 'measured', 'value': 0.0,
                'score': 0.0, 'display': '0%'})
        na_key = scoring.target_sort_key(nonclaim_na)
        zero_key = scoring.target_sort_key(claim_zero)
        self.assertNotEqual(na_key, zero_key)
        self.assertLess(na_key, zero_key)
        self.assertEqual(
            [row['target_id'] for row in sorted(
                [claim_zero, nonclaim_na], key=scoring.target_sort_key)],
            ['nonclaim-na', 'claim-zero'])
        self.assertIsNone(nonclaim_na['open_ground']['score'])
        self.assertEqual(claim_zero['open_ground']['score'], 0.0)

    def test_input_mutation_during_build_never_installs_latest(self):
        def mutate(_context):
            path = os.path.join(
                self.fixture.private,
                self.fixture.states['AL']['geology_terms']['path'])
            with open(path, 'ab') as target:
                target.write(b' ')

        with self.assertRaisesRegex(scoring.PublicationError,
                                    'changed during build'):
            scoring.build(
                self.fixture.inventory_path, self.fixture.publish,
                before_commit=mutate)
        self.assertFalse(os.path.exists(
            os.path.join(self.fixture.publish, 'latest.json')))

    def test_public_staging_symlinks_and_non_strict_json_are_rejected(self):
        with mock.patch.object(scoring, 'SITE', self.fixture.private):
            with self.assertRaisesRegex(scoring.PublicationError,
                                        'inside public site'):
                scoring.load_inventory(self.fixture.inventory_path)

        with self.assertRaisesRegex(scoring.PublicationError, 'duplicate JSON'):
            scoring.strict_json_bytes(b'{"a":1,"a":2}', 'fixture')
        with self.assertRaisesRegex(scoring.PublicationError,
                                    'non-standard JSON number'):
            scoring.strict_json_bytes(b'{"a":NaN}', 'fixture')

    def test_declared_rank_must_reconcile_with_computed_score(self):
        def reverse_scores(value):
            value['targets'][0]['score'] = 0.0
            value['targets'][1]['score'] = 90.0

        self.fixture.rewrite('NV', 'geology_terms', reverse_scores)
        with self.assertRaisesRegex(scoring.PublicationError,
                                    'declared ranks disagree'):
            scoring.build(self.fixture.inventory_path, self.fixture.publish)


if __name__ == '__main__':
    unittest.main()
