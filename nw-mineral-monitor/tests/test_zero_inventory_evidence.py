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

import build_zero_inventory_evidence as zero
import state_registry
import validate_national


class Fixture:
    def __init__(self, root, state='DE'):
        self.root = root
        self.site = os.path.join(root, 'site')
        self.manifest_path = os.path.join(self.site, 'data', 'manifest.json')
        self.baseline_path = os.path.join(
            self.site, 'data', 'tiles', 'national', 'faults.pmtiles')
        os.makedirs(os.path.dirname(self.baseline_path), exist_ok=True)
        with open(self.baseline_path, 'wb') as target:
            target.write(b'fault-baseline-fixture' * 16)
        with open(self.baseline_path, 'rb') as source:
            raw = source.read()
        states = {code: 1 for code in sorted(state_registry.ALL_STATES)}
        states[state] = 0
        self.manifest = {
            'region': sorted(state_registry.ALL_STATES),
            'national_baselines': {'faults': {
                'file': 'data/tiles/national/faults.pmtiles',
                'format': 'pmtiles', 'source_layer': 'faults',
                'n': sum(states.values()), 'states': states,
                'coverage': {
                    'states': 49, 'excluded_state_codes': ['DC', 'HI', 'PR'],
                    'zero_feature_states': [state],
                },
                'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            }},
        }
        os.makedirs(os.path.dirname(self.manifest_path), exist_ok=True)
        self.write_manifest()
        self.publish = os.path.join(
            self.site, 'map-assets', 'releases', 'zero-inventory')

    def write_manifest(self):
        with open(self.manifest_path, 'w', encoding='utf-8') as target:
            json.dump(self.manifest, target, sort_keys=True)

    def build(self, state='DE'):
        return zero.build(
            state, manifest_path=self.manifest_path,
            publish_dir=self.publish, site=self.site)


class ZeroInventoryEvidenceTests(unittest.TestCase):
    def test_builds_content_addressed_finding_without_geometry_or_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            with open(fixture.manifest_path, 'rb') as source:
                before_manifest = source.read()
            with open(fixture.baseline_path, 'rb') as source:
                before_baseline = source.read()
            result = fixture.build()
            path = os.path.join(fixture.site, result['evidence_artifact'])
            self.assertEqual(result['bytes'], os.path.getsize(path))
            evidence = zero.validate_evidence_file(
                path, 'DE', 'faults', manifest_path=fixture.manifest_path,
                expected_sha=result['sha256'], site=fixture.site)
            self.assertEqual(evidence['n'], 0)
            self.assertTrue(evidence['complete'])
            with open(fixture.manifest_path, 'rb') as source:
                self.assertEqual(source.read(), before_manifest)
            with open(fixture.baseline_path, 'rb') as source:
                self.assertEqual(source.read(), before_baseline)
            self.assertEqual(
                [name for name in os.listdir(os.path.dirname(path))
                 if name.endswith('.pmtiles')], [])

    def test_nonzero_unknown_and_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            with self.assertRaises(zero.ZeroInventoryError):
                fixture.build('NV')
            with self.assertRaises(zero.ZeroInventoryError):
                fixture.build('HI')
            result = fixture.build()
            path = os.path.join(fixture.site, result['evidence_artifact'])
            with open(path, 'ab') as target:
                target.write(b' ')
            with self.assertRaises(zero.ZeroInventoryError):
                zero.validate_evidence_file(
                    path, 'DE', 'faults', manifest_path=fixture.manifest_path,
                    expected_sha=result['sha256'], site=fixture.site)

    def test_registry_allows_zero_faults_only_with_exact_evidence_contract(self):
        digest = 'a' * 64
        layer = {
            'browser_delivery': 'pmtiles',
            'artifact': f'map-assets/releases/national/{"b" * 64}.pmtiles',
            'sha256': 'b' * 64, 'bytes': 256,
            'source_layers': ['faults'],
            'required_properties': ['st', 'source_id', 'source_scale'],
            'layer_metadata': {
                'faults': {'n': 0, 'availability': 'complete', 'complete': True}},
            'zero_inventory': {
                'evidence_artifact': (
                    f'map-assets/releases/zero-inventory/de/{digest}.json'),
                'sha256': digest,
                'bytes': 256,
                'baseline_manifest_key': 'national_baselines.faults',
                'baseline_sha256': 'b' * 64,
                'state_count': 0,
                'complete': True,
            },
        }
        errors = []
        state_registry._validate_delivery(
            layer, 'DE: faults', errors, release=True, zero_state='DE')
        self.assertEqual(errors, [])
        for mutation in (
                lambda row: row.update(zero_inventory=None),
                lambda row: row['zero_inventory'].update(state_count=1),
                lambda row: row['zero_inventory'].update(sha256='bad')):
            changed = copy.deepcopy(layer)
            mutation(changed)
            errors = []
            state_registry._validate_delivery(
                changed, 'DE: faults', errors, release=True, zero_state='DE')
            self.assertTrue(errors)

    def test_validator_binds_registry_assertion_to_current_baseline(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Fixture(root)
            result = fixture.build()
            layer = {
                'source_layers': ['faults'],
                'layer_metadata': {
                    'faults': {'n': 0, 'availability': 'complete', 'complete': True}},
                'bytes': fixture.manifest['national_baselines']['faults']['bytes'],
                'sha256': fixture.manifest['national_baselines']['faults']['sha256'],
                'zero_inventory': result,
            }
            budgets = os.path.join(root, 'budgets.json')
            with open(budgets, 'w', encoding='utf-8') as target:
                json.dump({'delivery': {
                    'immutable_release_prefix': 'map-assets/releases'}}, target)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', fixture.site), \
                    mock.patch.object(validate_national, 'BUDGETS', budgets):
                self.assertTrue(validate_national._validate_zero_fault_inventory(
                    qa, 'DE', layer))
            self.assertEqual(qa.errors, [])
            layer['sha256'] = '0' * 64
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', fixture.site), \
                    mock.patch.object(validate_national, 'BUDGETS', budgets):
                self.assertFalse(validate_national._validate_zero_fault_inventory(
                    qa, 'DE', layer))
            self.assertTrue(qa.errors)


if __name__ == '__main__':
    unittest.main()
