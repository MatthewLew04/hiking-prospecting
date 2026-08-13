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

import build_ci_acceptance_evidence as ci_evidence
import validate_national
from state_registry import ALL_STATES, GATE_KEYS


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Fixture:
    def __init__(self, root):
        self.root = root
        self.private = os.path.join(root, 'private')
        self.site = os.path.join(root, 'site')
        self.publish = os.path.join(
            self.site, 'map-assets', 'releases', 'ci-acceptance')
        os.makedirs(self.private)
        os.makedirs(self.site)
        self.paths = {
            'manifest': os.path.join(self.private, 'candidate-manifest.json'),
            'coverage': os.path.join(self.private, 'candidate-coverage.json'),
            'budgets': os.path.join(self.private, 'budgets.json'),
            'result': os.path.join(self.private, 'browser-result.json'),
        }
        self.commit = 'a' * 40
        self.run_url = 'https://ci.example.test/runs/4901'
        self.manifest = {
            'name': 'Candidate release',
            'region': sorted(ALL_STATES),
            'tiled_layers': [
                {
                    'id': 'nv-geology-units', 'state': 'NV',
                    'regime': 'claim', 'kind': 'geology',
                    'delivery': 'pmtiles', 'source_id': 'nv-geology-source',
                    'url': 'map-assets/releases/nv/geology-abc.pmtiles',
                    'source_layer': 'units', 'n': 12,
                    'view_bounds': [[-120.0, 35.0, -114.0, 42.0]],
                    'activation_minzoom': 4,
                    'filter': ['==', ['get', 'st'], 'NV'],
                    'availability': 'complete', 'complete': True,
                    'style_layers': [{
                        'id': 'nv-geology-units', 'type': 'fill',
                        'filter': ['==', ['get', 'st'], 'NV'],
                        'paint': {'fill-color': '#64748b'},
                    }],
                },
                {
                    'id': 'nv-federal-pending-zero', 'state': 'NV',
                    'regime': 'claim', 'kind': 'federal_mlrs-pending',
                    'system': 'federal_mlrs', 'delivery': 'pmtiles',
                    'source_id': 'nv-claims-source',
                    'url': 'map-assets/releases/nv/claims-abc.pmtiles',
                    'source_layer': 'pending', 'n': 0,
                    'view_bounds': [[-120.0, 35.0, -114.0, 42.0]],
                    'activation_minzoom': 4,
                    'filter': ['==', ['get', 'st'], 'NV'],
                    'availability': 'complete', 'complete': True,
                    'style_layers': [{
                        'id': 'nv-federal-pending-zero', 'type': 'circle',
                        'filter': ['==', ['get', 'st'], 'NV'],
                        'paint': {'circle-radius': 3},
                    }],
                },
                {
                    'id': 'nv-aeromag', 'state': 'NV', 'regime': 'claim',
                    'kind': 'aeromag', 'delivery': 'cog',
                    'source_id': 'nv-aeromag-source',
                    'url': '/map-assets/releases/nv/aeromag/{z}/{x}/{y}.webp',
                    'tile_url_template': (
                        '/map-assets/releases/nv/aeromag/{z}/{x}/{y}.webp'),
                    'bounds': [-120.0, 35.0, -114.0, 42.0],
                    'minzoom': 4, 'maxzoom': 12,
                    'cog': {
                        'url': 'map-assets/releases/nv/aeromag-abc.tif',
                        'sha256': 'b' * 64, 'bytes': 4096,
                    },
                    'style_layers': [{
                        'id': 'nv-aeromag', 'type': 'raster',
                        'paint': {'raster-opacity': 0.65},
                    }],
                },
                {
                    'id': 'al-baseline-other-state', 'state': 'AL',
                    'regime': 'non_claim', 'kind': 'geology',
                    'delivery': 'pmtiles', 'source_id': 'al-source',
                    'url': 'map-assets/releases/al/geology-abc.pmtiles',
                    'source_layer': 'units', 'n': 1,
                    'availability': 'complete', 'complete': True,
                    'style_layers': [{
                        'id': 'al-baseline-other-state', 'type': 'fill',
                    }],
                },
            ],
        }
        state_rows = []
        for code in sorted(ALL_STATES):
            is_target = code == 'NV'
            state_rows.append({
                'state': code,
                'name': code,
                'phase': 1,
                'regime': 'claim' if code == 'NV' else 'non_claim',
                'release': 'done' if is_target else 'building',
                'enabled': is_target,
                'gate_passed': is_target,
                'gates': {
                    gate: {'status': ('pass' if is_target else 'fail'),
                           'evidence': 'reviewed browser fixture'}
                    for gate in GATE_KEYS
                },
            })
        self.coverage = {
            'schema_version': 1,
            'scope': '49 states; Hawaii excluded',
            'summary': {
                'states': 49, 'claim_states': 19, 'non_claim_states': 30,
                'released': 1, 'gate_complete': 1,
            },
            'states': state_rows,
        }
        self.budgets = {
            'schema_version': 1,
            'delivery': {'immutable_release_prefix': 'map-assets/releases'},
            'browser': {
                'heap_mb_max': 211,
                'bulk_origin_storage_mb_max': 0,
            },
        }
        self.write('manifest')
        self.write('coverage')
        self.write('budgets')
        self.result = self._result()
        self.write('result')

    def write(self, name):
        value = getattr(self, name)
        with open(self.paths[name], 'w', encoding='utf-8') as target:
            json.dump(value, target, sort_keys=True)

    def binding(self, name):
        with open(self.paths[name], 'rb') as source:
            raw = source.read()
        return {'bytes': len(raw), 'sha256': _sha(raw)}

    def install_release_inputs(self):
        data = os.path.join(self.site, 'data')
        os.makedirs(data, exist_ok=True)
        for name in ('manifest', 'coverage'):
            with open(self.paths[name], 'rb') as source, open(
                    os.path.join(data, f'{name}.json'), 'wb') as target:
                target.write(source.read())

    def raw_inputs(self):
        values = {}
        for name, path in self.paths.items():
            with open(path, 'rb') as source:
                values[name] = source.read()
        return values

    def _observation(self, descriptor, order):
        delivery = descriptor['delivery']
        declared = descriptor.get('n') if delivery == 'pmtiles' else None
        bounds = (descriptor['view_bounds'][0] if delivery == 'pmtiles'
                  else descriptor['bounds'])
        return {
            'descriptor_id': descriptor['id'],
            'descriptor_sha256': _sha(ci_evidence.canonical_bytes(descriptor)),
            'delivery': delivery,
            'source_id': descriptor['source_id'],
            'source_layer': descriptor.get('source_layer'),
            'source_url': (f'pmtiles://{descriptor["url"]}'
                           if delivery == 'pmtiles' else descriptor['url']),
            'runtime_style_layer_ids': [
                f'ws11-{row["id"]}' for row in descriptor['style_layers']],
            'visit_order': order,
            'visit_mode': 'exclusive_sequential',
            'visit_category': descriptor['kind'],
            'visit_bounds_index': 0,
            'visit_center': [(bounds[0] + bounds[2]) / 2,
                             (bounds[1] + bounds[3]) / 2],
            'visit_zoom': (descriptor['activation_minzoom']
                           if delivery == 'pmtiles' else descriptor['minzoom']),
            'state_filter': (descriptor['filter']
                             if delivery == 'pmtiles' else None),
            'source_present': True,
            'source_loaded': True,
            'state_scope_applied': True,
            'queryable': True,
            'query_status': ('raster_loaded' if delivery == 'cog' else
                             'declared_zero' if declared == 0 else 'nonempty'),
            'queried_features': (None if delivery == 'cog' else
                                 0 if declared == 0 else 4),
            'successful_source_requests': 2,
        }

    def _result(self):
        selected = [row for row in self.manifest['tiled_layers']
                    if row['state'] == 'NV']
        return {
            'schema_version': 1,
            'test': {'id': ci_evidence.BROWSER_TEST_ID,
                     'version': ci_evidence.BROWSER_TEST_VERSION},
            'generated': '2026-08-13T19:20:21Z',
            'state': 'NV', 'profile': 'release', 'status': 'green',
            'run_url': self.run_url, 'commit': self.commit,
            'input_bindings': {
                name: self.binding(name)
                for name in ('manifest', 'coverage', 'budgets')
            },
            'browser': {
                'engine': 'chromium', 'engine_version': '140.0.7339.5',
                'playwright_version': '1.62.1', 'headless': True,
            },
            'state_toggle': {
                'state': 'NV', 'coverage_enabled': True,
                'coverage_gate_passed': True, 'initial_on': True,
                'off_observed': True, 'on_observed': True,
                'final_on': True, 'green': True,
            },
            'descriptor_observations': [
                self._observation(row, index)
                for index, row in enumerate(selected)],
            'measurement_samples': [
                {'label': 'nv-off', 'phase': 'state_off_settled',
                 'descriptor_id': None, 'visit_order': None,
                 'heap_mb': 37.2, 'bulk_origin_storage_mb': 0},
            ] + [
                {'label': f'nv-visit-{index}',
                 'phase': 'descriptor_visit_settled',
                 'descriptor_id': row['id'], 'visit_order': index,
                 'heap_mb': 50.0 + index * 7.25,
                 'bulk_origin_storage_mb': 0}
                for index, row in enumerate(selected)],
            'failures': {field: [] for field in ci_evidence.FAILURE_FIELDS},
            'statewide_browser_json': False,
        }

    def build(self, **overrides):
        arguments = {
            'browser_result_path': self.paths['result'],
            'manifest_path': self.paths['manifest'],
            'coverage_path': self.paths['coverage'],
            'budgets_path': self.paths['budgets'],
            'state': 'NV', 'commit': self.commit, 'run_url': self.run_url,
            'publish_dir': self.publish,
        }
        arguments.update(overrides)
        with mock.patch.object(ci_evidence, 'SITE', self.site):
            return ci_evidence.build(**arguments)


class CIAcceptanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_content_addressed_directly_consumable_evidence(self):
        before = self.fixture.raw_inputs()
        result = self.fixture.build()
        self.assertTrue(result['evidence_artifact'].startswith(
            'map-assets/releases/ci-acceptance/nv/'))
        self.assertTrue(result['evidence_artifact'].endswith(
            f'/{result["sha256"]}.json'))
        self.assertEqual(result['descriptors'], 3)
        evidence_path = os.path.join(
            self.fixture.site, result['evidence_artifact'])
        self.assertEqual(result['bytes'], os.path.getsize(evidence_path))
        self.assertEqual(result['acceptance']['bytes'], result['bytes'])
        with mock.patch.object(ci_evidence, 'SITE', self.fixture.site):
            evidence = ci_evidence.validate_published(
                evidence_path, expected_state='NV')
        self.assertEqual(evidence['measurements'], {
            'heap_mb': 64.5, 'bulk_origin_storage_mb': 0})
        self.assertEqual(evidence['browser_test'], {
            'id': ci_evidence.BROWSER_TEST_ID,
            'version': ci_evidence.BROWSER_TEST_VERSION})
        self.assertEqual(
            {row['query_status'] for row in evidence['descriptors']},
            {'nonempty', 'declared_zero', 'raster_loaded'})
        self.assertFalse(os.path.exists(
            os.path.join(self.fixture.publish, 'latest.json')))
        self.assertEqual(before, self.fixture.raw_inputs())

        self.fixture.install_release_inputs()
        qa = validate_national.QA()
        with mock.patch.object(validate_national, 'SITE', self.fixture.site), \
                mock.patch.object(validate_national, 'BUDGETS',
                                  self.fixture.paths['budgets']):
            validate_national._validate_ci_evidence(
                qa, 'NV', {'ci_scale': result['acceptance']})
        self.assertEqual(qa.errors, [])

    def test_done_gate_replays_content_not_just_checksum(self):
        """A newly rehashed forgery still fails semantic/current-input replay."""
        mutations = {
            'descriptor-hash': lambda value:
                value['descriptors'][0].update(descriptor_sha256='f' * 64),
            'missing-cog-request': lambda value:
                next(row for row in value['descriptors']
                     if row['delivery'] == 'cog').update(
                         successful_source_requests=0),
            'missing-cog-lifecycle': lambda value:
                next(row for row in value['descriptors']
                     if row['delivery'] == 'cog').update(source_loaded=False),
            'positive-count-zero-query': lambda value:
                next(row for row in value['descriptors']
                     if row['declared_n'] == 12).update(
                         query_status='declared_zero', queried_features=0),
            'candidate-hash': lambda value:
                value['release_inputs']['manifest'].update(sha256='0' * 64),
            'coverage-hash': lambda value:
                value['release_inputs']['coverage'].update(sha256='0' * 64),
            'budgets-hash': lambda value:
                value['release_inputs']['budgets'].update(sha256='0' * 64),
            'browser-version': lambda value:
                value['browser_test'].update(version=999),
            'budget-limit': lambda value:
                value['budget_limits'].update(heap_mb_max=999),
            'measurement': lambda value:
                value['measurement_samples'][1].update(heap_mb=999),
            'map-error': lambda value:
                value['failures']['map_errors'].append('forged green run'),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                fixture = Fixture(os.path.join(self.temporary.name, f'gate-{label}'))
                built = fixture.build()
                fixture.install_release_inputs()
                original = os.path.join(fixture.site, built['evidence_artifact'])
                with open(original, encoding='utf-8') as source:
                    evidence = json.load(source)
                mutate(evidence)
                raw = ci_evidence.canonical_bytes(evidence)
                digest = _sha(raw)
                forged_rel = os.path.join(
                    'map-assets', 'releases', 'ci-acceptance', 'nv',
                    f'{digest}.json')
                forged = os.path.join(fixture.site, forged_rel)
                with open(forged, 'wb') as target:
                    target.write(raw)
                asserted = dict(built['acceptance'])
                asserted.update(evidence_artifact=forged_rel, sha256=digest)
                qa = validate_national.QA()
                with mock.patch.object(validate_national, 'SITE', fixture.site), \
                        mock.patch.object(validate_national, 'BUDGETS',
                                          fixture.paths['budgets']):
                    validate_national._validate_ci_evidence(
                        qa, 'NV', {'ci_scale': asserted})
                self.assertTrue(qa.errors, label)

    def test_observation_set_and_identity_must_exactly_match_manifest(self):
        for label, mutate, pattern in (
            ('missing', lambda rows: rows.pop(), 'observations must be exact'),
            ('duplicate', lambda rows: rows.append(copy.deepcopy(rows[0])),
             'duplicates descriptor'),
            ('source', lambda rows: rows[0].update(source_id='wrong-source'),
             'identity does not match'),
            ('hash', lambda rows: rows[0].update(descriptor_sha256='f' * 64),
             'identity does not match'),
        ):
            with self.subTest(label=label):
                fixture = Fixture(os.path.join(self.temporary.name, label))
                mutate(fixture.result['descriptor_observations'])
                fixture.write('result')
                with self.assertRaisesRegex(ci_evidence.AcceptanceError, pattern):
                    fixture.build()

    def test_zero_is_accepted_only_for_manifest_declared_zero(self):
        observations = self.fixture.result['descriptor_observations']
        nonempty = next(row for row in observations
                        if row['descriptor_id'] == 'nv-geology-units')
        nonempty.update(query_status='declared_zero', queried_features=0)
        self.fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'declares n>0'):
            self.fixture.build()

        fixture = Fixture(os.path.join(self.temporary.name, 'declared-zero'))
        zero = next(row for row in fixture.result['descriptor_observations']
                    if row['descriptor_id'] == 'nv-federal-pending-zero')
        zero.update(query_status='nonempty', queried_features=1)
        fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'may report zero only'):
            fixture.build()

    def test_manifest_requires_reviewed_vector_count_and_raster_probe(self):
        del self.fixture.manifest['tiled_layers'][0]['n']
        self.fixture.write('manifest')
        self.fixture.result['input_bindings']['manifest'] = self.fixture.binding('manifest')
        self.fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError, '\\.n'):
            self.fixture.build()

        fixture = Fixture(os.path.join(self.temporary.name, 'raster'))
        raster = next(row for row in fixture.result['descriptor_observations']
                      if row['delivery'] == 'cog')
        raster['successful_source_requests'] = 0
        fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'successful_source_requests'):
            fixture.build()

    def test_lazy_descriptor_visits_bind_bounds_zoom_category_filter_and_order(self):
        mutations = {
            'bounds': lambda fixture: fixture.manifest['tiled_layers'][0].pop(
                'view_bounds'),
            'activation': lambda fixture:
                fixture.manifest['tiled_layers'][0].update(activation_minzoom=25),
            'manifest-filter': lambda fixture:
                fixture.manifest['tiled_layers'][0].update(
                    filter=['==', ['get', 'st'], 'CA']),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                fixture = Fixture(os.path.join(self.temporary.name, f'lazy-{label}'))
                mutate(fixture)
                fixture.write('manifest')
                fixture.result['input_bindings']['manifest'] = fixture.binding('manifest')
                fixture.write('result')
                with self.assertRaises(ci_evidence.AcceptanceError):
                    fixture.build()

        observation_mutations = {
            'center': lambda row: row.update(visit_center=[0, 0]),
            'zoom': lambda row: row.update(visit_zoom=3),
            'category': lambda row: row.update(visit_category='faults'),
            'filter': lambda row: row.update(
                state_filter=['==', ['get', 'st'], 'CA']),
            'order': lambda row: row.update(visit_order=1),
        }
        for label, mutate in observation_mutations.items():
            with self.subTest(label=f'observation-{label}'):
                fixture = Fixture(os.path.join(
                    self.temporary.name, f'visit-{label}'))
                row = next(item for item in fixture.result['descriptor_observations']
                           if item['descriptor_id'] == 'nv-geology-units')
                mutate(row)
                fixture.write('result')
                with self.assertRaises(ci_evidence.AcceptanceError):
                    fixture.build()

        fixture = Fixture(os.path.join(self.temporary.name, 'visit-sample-order'))
        sample = next(row for row in fixture.result['measurement_samples']
                      if row.get('descriptor_id') == 'nv-geology-units')
        sample['visit_order'] = 2
        fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'sequential descriptor visit'):
            fixture.build()

    def test_errors_json_requests_toggle_and_browser_version_fail_closed(self):
        mutations = {
            'network': lambda result:
                result['failures']['request_failures'].append('tile failed'),
            'map': lambda result:
                result['failures']['map_errors'].append('source error'),
            'json': lambda result:
                result.update(statewide_browser_json=True),
            'toggle': lambda result:
                result['state_toggle'].update(off_observed=False),
            'version': lambda result:
                result['test'].update(version=999),
            'browser': lambda result:
                result['browser'].update(engine='firefox'),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                fixture = Fixture(os.path.join(self.temporary.name, label))
                mutate(fixture.result)
                fixture.write('result')
                with self.assertRaises(ci_evidence.AcceptanceError):
                    fixture.build()

    def test_candidate_hash_commit_run_and_exact_coverage_are_bound(self):
        self.fixture.result['input_bindings']['manifest']['sha256'] = '0' * 64
        self.fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'does not match the exact'):
            self.fixture.build()

        fixture = Fixture(os.path.join(self.temporary.name, 'run'))
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'differs from CLI identity'):
            fixture.build(run_url='https://ci.example.test/runs/other')

        fixture = Fixture(os.path.join(self.temporary.name, 'coverage'))
        target = next(row for row in fixture.coverage['states']
                      if row['state'] == 'NV')
        target['enabled'] = False
        fixture.coverage['summary']['released'] = 0
        fixture.write('coverage')
        fixture.result['input_bindings']['coverage'] = fixture.binding('coverage')
        fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'candidate coverage'):
            fixture.build()

    def test_every_settled_measurement_must_fit_current_budget(self):
        self.fixture.result['measurement_samples'][1]['heap_mb'] = 211.1
        self.fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'exceeds current browser budget'):
            self.fixture.build()

        fixture = Fixture(os.path.join(self.temporary.name, 'phase'))
        fixture.result['measurement_samples'].pop(0)
        fixture.write('result')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'every sequential descriptor visit'):
            fixture.build()

    def test_private_runner_staging_and_release_publish_boundary_are_enforced(self):
        public_result = os.path.join(self.fixture.site, 'browser-result.json')
        with open(self.fixture.paths['result'], 'rb') as source, \
                open(public_result, 'wb') as target:
            target.write(source.read())
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'raw runner data is private'):
            self.fixture.build(browser_result_path=public_result)
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'site/map-assets/releases'):
            self.fixture.build(publish_dir=os.path.join(self.fixture.site, 'data'))

    def test_input_mutation_before_commit_cannot_publish(self):
        def mutate(_snapshots):
            with open(self.fixture.paths['coverage'], 'ab') as target:
                target.write(b' ')

        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'changed during compilation'):
            self.fixture.build(before_commit=mutate)
        self.assertFalse(os.path.exists(self.fixture.publish))

    def test_strict_json_and_content_address_tamper_are_rejected(self):
        with open(self.fixture.paths['result'], 'wb') as target:
            target.write(b'{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ci_evidence.AcceptanceError,
                                    'duplicate JSON'):
            self.fixture.build()

        fixture = Fixture(os.path.join(self.temporary.name, 'tamper'))
        result = fixture.build()
        path = os.path.join(fixture.site, result['evidence_artifact'])
        with open(path, 'ab') as target:
            target.write(b' ')
        with mock.patch.object(ci_evidence, 'SITE', fixture.site), \
                self.assertRaises(ci_evidence.AcceptanceError):
            ci_evidence.validate_published(path, expected_state='NV')


if __name__ == '__main__':
    unittest.main()
