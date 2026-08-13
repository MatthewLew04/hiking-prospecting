import datetime as dt
import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))
sys.path.insert(0, os.path.join(ROOT, 'infra'))

import state_registry
from build_runtime_registry import build as build_runtime_registry
from ak_deadlines import state_claim_deadlines, rent_amount
from land_context import normalize_land_context
from migrate_grades_open_ground import migrate
from spatial_clip import StateClipIndex
from geology_targets import target_sort_key


class RegistryTests(unittest.TestCase):
    def test_exact_49_and_19_claim_states(self):
        self.assertEqual(len(state_registry.ALL_STATES), 49)
        self.assertNotIn('HI', state_registry.ALL_STATES)
        self.assertEqual(state_registry.CLAIM_STATES, frozenset(
            'AK AZ AR CA CO FL ID LA MS MT NE NV NM ND OR SD UT WA WY'.split()))

    def test_national_research_links_are_not_hardcoded_to_idaho(self):
        for relative in ('site/index.html', 'pipelines/dossier.py'):
            with self.subTest(relative=relative):
                with open(os.path.join(ROOT, relative), encoding='utf-8') as source:
                    text = source.read()
                self.assertNotRegex(text, r'Chronicling America[^\n]+\+idaho')
                self.assertNotRegex(text, r'HathiTrust[^\n]+idaho')

    def test_alaska_browser_copy_does_not_claim_precision_is_pending(self):
        with open(os.path.join(ROOT, 'site', 'index.html'),
                  encoding='utf-8') as source:
            text = source.read()
        self.assertNotIn('a higher-precision rebuild is pending', text)
        self.assertNotIn(
            'compatibility archive is missing one or more source IDs', text)

    def test_registry_valid(self):
        result = state_registry.validate_registry()
        self.assertTrue(result['ok'], '\n'.join(result['errors']))

    def test_release_file_descriptor_is_exact_and_content_addressed(self):
        digest = 'a' * 64
        valid = {
            'artifact': f'map-assets/releases/nv/{digest}.pmtiles',
            'sha256': digest, 'bytes': 128,
        }
        errors = []
        self.assertTrue(state_registry._validate_release_file(
            valid, 'artifact', 'sha256', 'bytes', 'fixture', errors,
            {'.pmtiles'}))
        self.assertEqual(errors, [])
        for mutation in (
                {'artifact': 'map-assets/releases/nv/friendly.pmtiles'},
                {'artifact': f'map-assets/releases/.staging/{digest}.pmtiles'},
                {'sha256': 'A' * 64}, {'bytes': True}, {'bytes': 0}):
            with self.subTest(mutation=mutation):
                candidate = dict(valid)
                candidate.update(mutation)
                errors = []
                self.assertFalse(state_registry._validate_release_file(
                    candidate, 'artifact', 'sha256', 'bytes', 'fixture',
                    errors, {'.pmtiles'}))
                self.assertTrue(errors)

    def test_disabled_defaults_keep_all_release_metadata_null(self):
        row = state_registry.load_state('NY')
        self.assertFalse(row['release']['enabled'])
        for delivery in ('geology', 'faults', 'aeromag', 'land_context'):
            self.assertIsNone(row[delivery]['artifact'])
            self.assertIsNone(row[delivery]['sha256'])
            self.assertIsNone(row[delivery]['bytes'])
        acceptance = row['release']['acceptance']
        self.assertIsNone(acceptance['grades']['bytes'])
        self.assertIsNone(acceptance['recorders']['evidence_sha256'])
        self.assertIsNone(acceptance['recorders']['evidence_bytes'])
        self.assertIsNone(acceptance['expiration_watch']['evidence_sha256'])
        self.assertIsNone(acceptance['expiration_watch']['evidence_bytes'])
        self.assertIsNone(acceptance['quad_maps']['ranked_targets_bytes'])
        self.assertIsNone(acceptance['ci_scale']['bytes'])
        for finding in ('aml', 'trust_land'):
            self.assertIsNone(row[finding]['evidence_artifact'])
            self.assertIsNone(row[finding]['evidence_sha256'])
            self.assertIsNone(row[finding]['evidence_bytes'])
        claim_row = state_registry.load_state('NV')
        federal = next(system for system in claim_row['claim_systems']
                       if system['id'] == 'federal_mlrs')
        self.assertIsNone(federal['artifact'])
        self.assertIsNone(federal['sha256'])
        self.assertIsNone(federal['bytes'])
        self.assertIsNone(federal['publication_inventory_artifact'])
        self.assertIsNone(federal['publication_inventory_sha256'])
        self.assertIsNone(federal['publication_inventory_bytes'])

    def test_registry_rejects_boolean_phase_and_unrelated_envelope(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        row['phase'] = True
        row['query_envelopes'] = [
            {'id': 'wrong', 'bbox': [0, 0, 1, 1], 'crs': 'EPSG:4326'}]
        with self.assertRaises(state_registry.RegistryError):
            state_registry.validate_state(row)

    def test_registry_rejects_tiny_envelope_around_state_mean(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        row['query_envelopes'] = [{
            'id': 'tiny-centroid-box',
            'bbox': [-114.99775, 36.54710, -114.99575, 36.54910],
            'crs': 'EPSG:4326',
        }]
        with self.assertRaisesRegex(
                state_registry.RegistryError,
                'must contain every authoritative state-footprint vertex'):
            state_registry.validate_state(row)

    def test_claim_states_register_typed_mineral_title_findings(self):
        for code in state_registry.CLAIM_STATES:
            with self.subTest(state=code):
                estate = state_registry.load_state(code)['open_ground'][
                    'mineral_estate']
                self.assertIn(estate['status'],
                              state_registry.MINERAL_ESTATE_STATUSES)
                self.assertTrue(estate['surface_management_is_not_title'])
                self.assertGreaterEqual(len(estate['finding']), 60)

    def test_mineral_title_registry_cannot_promote_sma_or_fake_source(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        estate = row['open_ground']['mineral_estate']
        estate.update({
            'status': 'reviewed_ingested',
            'authority': 'Surface map',
            'source_url': 'http://example.test/not-official',
            'ownership_field': 'ADMIN_AGENCY_CODE',
            'locatable_values': [],
            'surface_management_is_not_title': False,
        })
        with self.assertRaisesRegex(
                state_registry.RegistryError,
                'surface management|HTTPS source|locatable values'):
            state_registry.validate_state(row)

    def test_claim_state_envelope_must_cover_every_authoritative_vertex(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        # This was the former Nevada envelope: it contains 166/167 clip
        # vertices, which is insufficient for border-claim acquisition.
        row['query_envelopes'][0]['bbox'][2] = -114.04
        with self.assertRaisesRegex(
                state_registry.RegistryError,
                'must contain every authoritative state-footprint vertex'):
            state_registry.validate_state(row)

    def test_alaska_contract_requires_ardf_layers_and_deadlines(self):
        row = copy.deepcopy(state_registry.load_state('AK'))
        row['occurrence_backbone'] = {}
        state_system = next(system for system in row['claim_systems']
                            if system['id'] == 'alaska_state_claims')
        state_system.pop('deadline_policy')
        state_system['layers'] = {'active': 0}
        with self.assertRaises(state_registry.RegistryError):
            state_registry.validate_state(row)

    def test_release_cannot_be_asserted_with_evidence_strings(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        for gate in row['done_gate'].values():
            gate.update({'status': 'pass', 'evidence': 'ok'})
        row['release'].update({'status': 'done', 'enabled': True})
        with self.assertRaises(state_registry.RegistryError):
            state_registry.validate_state(row)

    def test_release_acceptance_type_errors_fail_closed(self):
        mutations = [
            ('grades', []),
            ('recorders', []),
            ('quad_maps', []),
            ('ci_scale', []),
        ]
        for key, value in mutations:
            with self.subTest(key=key):
                row = copy.deepcopy(state_registry.load_state('NV'))
                for gate in row['done_gate'].values():
                    gate.update({'status': 'pass', 'evidence': 'reviewed evidence'})
                row['release'].update({'status': 'done', 'enabled': True})
                row['release']['acceptance'][key] = value
                with self.assertRaises(state_registry.RegistryError):
                    state_registry.validate_state(row)

    def test_grade_counter_strings_and_fake_finding_do_not_bypass_gate(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        for gate in row['done_gate'].values():
            gate.update({'status': 'pass', 'evidence': 'reviewed evidence'})
        row['release'].update({'status': 'done', 'enabled': True})
        row['release']['acceptance']['grades'] = {
            'graded_mines': 25, 'primary_sources': 2,
            'verbatim_quotes': '25', 'page_cites': '25',
            'low_endowment_finding': {
                'finding': 'x' * 80, 'sources': 'two sources',
                'artifact': 'data/evidence/nv.json',
            },
        }
        with self.assertRaises(state_registry.RegistryError):
            state_registry.validate_state(row)

    def test_grade_acceptance_requires_content_addressed_compiler_artifact(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        grades = row['release']['acceptance']['grades']
        grades.update({
            'graded_mines': 25, 'primary_sources': 2,
            'verbatim_quotes': 25, 'page_cites': 25,
            'low_endowment_finding': None,
        })
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertTrue(any('evidence_artifact/sha256/bytes' in error
                            for error in errors))
        grades.update({
            'evidence_artifact': (
                'map-assets/releases/grade-evidence/states/nv/' + 'a' * 64 + '.json'),
            'sha256': 'b' * 64,
            'bytes': 100,
        })
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertTrue(any('evidence_artifact/sha256/bytes' in error
                            for error in errors))
        grades['sha256'] = 'a' * 64
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertFalse(any('grade acceptance: evidence_artifact' in error
                             for error in errors))

    def test_ci_acceptance_requires_content_addressed_hash_and_full_commit(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        ci = row['release']['acceptance']['ci_scale']
        ci.update({
            'evidence_artifact': 'data/evidence/nv-ci.json',
            'sha256': 'a' * 64,
            'bytes': 100,
            'run_url': 'https://ci.example.test/runs/1',
            'commit': 'abcdef0123456789',
            'state_toggle_green': True,
            'statewide_browser_json': False,
            'heap_mb': 50,
            'bulk_origin_storage_mb': 0,
        })
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertTrue(any('content-addressed release evidence' in error
                            for error in errors))
        ci.update({
            'evidence_artifact': (
                'map-assets/releases/ci-acceptance/nv/' + 'a' * 64 + '.json'),
            'commit': 'b' * 40,
        })
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertFalse(any('CI acceptance needs' in error for error in errors))

    def test_release_status_and_enabled_must_move_together(self):
        for status, enabled in (('done', False), ('building', True)):
            with self.subTest(status=status, enabled=enabled):
                row = copy.deepcopy(state_registry.load_state('NV'))
                row['release'].update({'status': status, 'enabled': enabled})
                with self.assertRaises(state_registry.RegistryError):
                    state_registry.validate_state(row)

    def test_empty_recorder_inventory_cannot_release_claim_state(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        acceptance = row['release']['acceptance']['recorders']
        acceptance.update({
            'jurisdiction_type': 'county', 'inventory_complete': True,
            'evidence_artifact': 'data/evidence/nv-counties.json',
            'live_claim_jurisdiction_ids': [], 'covered_jurisdiction_ids': [],
        })
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertTrue(any('recorder acceptance' in error for error in errors))

    def test_alaska_recorder_gate_uses_recording_districts_not_county_fips(self):
        row = copy.deepcopy(state_registry.load_state('AK'))
        self.assertEqual(row['recorder']['jurisdiction_type'], 'recording_district')
        self.assertEqual(row['release']['acceptance']['recorders']['jurisdiction_type'],
                         'recording_district')
        acceptance = row['release']['acceptance']['recorders']
        acceptance.update({
            'inventory_complete': True,
            'active_claims': 1,
            'evidence_artifact': (
                'map-assets/releases/recorders/states/ak/' + 'a' * 64 + '.json'),
            'evidence_sha256': 'a' * 64,
            'evidence_bytes': 100,
            'live_claim_jurisdiction_ids': ['Fairbanks Recording District'],
            'covered_jurisdiction_ids': ['Fairbanks Recording District'],
        })
        row['recorder']['matrix'] = [{
            'jurisdiction_id': 'Fairbanks Recording District',
            'status': 'accepted', 'portal_vendor': 'Alaska DNR Recorder',
            'portal_url': 'https://dnr.alaska.gov/ssd/recoff/',
        }]
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertFalse(any('recorder acceptance' in error or
                             'recorder matrix' in error for error in errors), errors)

    def test_release_claim_system_needs_complete_publication_inventory(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        system = next(item for item in row['claim_systems']
                      if item['id'] == 'federal_mlrs')
        system.update({
            'source_layers': ['active', 'closed', 'open_ground'],
            'required_properties': {
                'active': ['st', 'serial', 'status'],
                'closed': ['st', 'serial', 'status'],
                'open_ground': ['st', 'status', 'open_count', 'section_count',
                                'open_fraction'],
            },
            'source_layer_counts': {'active': 1, 'closed': 1, 'open_ground': 1},
            'layer_metadata': {
                'active': {'n': 1}, 'closed': {'n': 1},
                'open_ground': {'n': 1},
            },
        })
        row['release'].update({'status': 'done', 'enabled': True})
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'publication inventory'):
                state_registry.validate_state(row)

    def test_recorder_zero_live_is_explicit_active_zero_not_fake_county(self):
        row = copy.deepcopy(state_registry.load_state('FL'))
        row['recorder']['matrix'] = []
        acceptance = row['release']['acceptance']['recorders']
        acceptance.update({
            'jurisdiction_type': 'county', 'inventory_complete': True,
            'active_claims': 0,
            'evidence_artifact': (
                'map-assets/releases/recorders/states/fl/' + 'a' * 64 + '.json'),
            'evidence_sha256': 'a' * 64,
            'evidence_bytes': 100,
            'live_claim_jurisdiction_ids': [],
            'covered_jurisdiction_ids': [],
        })
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertFalse(any('recorder acceptance must identify' in error
                             for error in errors))
        acceptance['active_claims'] = 1
        errors = []
        state_registry._validate_release_acceptance(row, '<state>', errors)
        self.assertTrue(any('recorder acceptance must identify' in error
                            for error in errors))

    def test_federal_release_requires_separate_claim_and_open_ground_artifacts(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        system = next(item for item in row['claim_systems']
                      if item['id'] == 'federal_mlrs')
        system['publication_artifacts'] = {'claims': {}, 'open_ground': {}}
        row['release'].update({'status': 'done', 'enabled': True})
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'disjoint immutable'):
            state_registry.validate_state(row)

    def test_claim_release_needs_watch_for_every_system(self):
        row = copy.deepcopy(state_registry.load_state('AK'))
        row['release'].update({'status': 'done', 'enabled': True})
        row['release']['acceptance']['expiration_watch'] = {
            'evidence_artifact': (
                'map-assets/releases/watch/states/ak/' + 'a' * 64 + '.json'),
            'evidence_sha256': 'a' * 64, 'evidence_bytes': 100,
            'run_id': 'run-1', 'generated': '2026-08-13T12:00:00+00:00',
            'complete': True, 'system_ids': ['alaska_state_claims'],
        }
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'every declared claim system'):
            state_registry.validate_state(row)

    def test_release_claim_properties_are_not_self_selected(self):
        row = copy.deepcopy(state_registry.load_state('NV'))
        system = next(item for item in row['claim_systems']
                      if item['id'] == 'federal_mlrs')
        system.update({
            'source_layers': ['active', 'closed', 'open_ground'],
            'required_properties': {
                'active': ['foo'], 'closed': ['foo'],
                'open_ground': ['foo'],
            },
        })
        row['release'].update({'status': 'done', 'enabled': True})
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'open-ground math properties'):
            state_registry.validate_state(row)

    def test_nonclaim_release_requires_per_target_land_context_layer(self):
        row = copy.deepcopy(state_registry.load_state('MI'))
        context = row['land_context']
        context.update({
            'artifact': 'map-assets/releases/mi/land-context.pmtiles',
            'bytes': 1024, 'sha256': 'a' * 64,
            'mineral_ownership_verified': True,
            'source_layers': ['land_context'],
            'required_properties': {
                'land_context': ['st', 'surface_class', 'mineral_class', 'approach'],
            },
            'layer_metadata': {
                'land_context': {'n': 1, 'availability': 'complete', 'complete': True},
            },
        })
        row['release'].update({'status': 'done', 'enabled': True})
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'per-target'):
            state_registry.validate_state(row)

    def test_nonclaim_release_cannot_skip_aml_or_trust_inventory_review(self):
        row = copy.deepcopy(state_registry.load_state('MI'))
        row['release'].update({'status': 'done', 'enabled': True})
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'AML inventory pending'):
            state_registry.validate_state(row)
        row['aml'].update({
            'release_inventory_status': 'documented_unavailable',
            'evidence_artifact': (
                'map-assets/releases/nonclaim/mi/' + 'a' * 64 + '.json'),
            'evidence_sha256': 'a' * 64,
            'evidence_bytes': 100,
        })
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'trust-land mineral-leasing offering class'):
            state_registry.validate_state(row)

    def test_malformed_nested_registry_values_never_raise_type_error(self):
        for key, value in (
                ('open_ground', []), ('claim_systems', {}), ('release', []),
                ('done_gate', []), ('geological_survey', []),
                ('historic_serials', {}), ('recorder', []),
                ('occurrence_backbone', [])):
            with self.subTest(key=key):
                row = copy.deepcopy(state_registry.load_state('AK'))
                row[key] = value
                with self.assertRaises(state_registry.RegistryError):
                    state_registry.validate_state(row)

    def test_aml_and_trust_land_cannot_be_placeholder_objects(self):
        row = copy.deepcopy(state_registry.load_state('MI'))
        row['aml'] = {'x': 1}
        row['trust_land'] = {'x': 1}
        with self.assertRaisesRegex(state_registry.RegistryError,
                                    'AML registry|trust-land registry'):
            state_registry.validate_state(row)

    def test_registry_json_rejects_duplicate_keys_and_nan(self):
        for content in ('{"state":"NV","state":"CA"}', '{"phase":NaN}'):
            with self.subTest(content=content), tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8') as registry:
                registry.write(content)
                registry.flush()
                with self.assertRaises(state_registry.RegistryError):
                    state_registry._read_json_yaml(registry.name)

    def test_lambda_query_envelopes_are_numeric_bboxes(self):
        runtime = build_runtime_registry()['states']
        for state in runtime.values():
            self.assertTrue(state['query_envelopes'])
            for bbox in state['query_envelopes']:
                self.assertEqual(len(bbox), 4)
                self.assertTrue(all(isinstance(v, (int, float)) for v in bbox))

    def test_lambda_state_clips_cover_exact_49(self):
        path = os.path.join(ROOT, 'infra', 'state_clips.json')
        with open(path, encoding='utf-8') as clip_file:
            clips = json.load(clip_file)['states']
        self.assertEqual(set(clips), set(state_registry.ALL_STATES))
        self.assertTrue(all(g['type'] in ('Polygon', 'MultiPolygon')
                            for g in clips.values()))
        ca = StateClipIndex(clips['CA'])
        self.assertTrue(ca.contains(-121.49, 38.58))
        self.assertFalse(ca.contains(-115.14, 36.17))

    def test_alaska_dual_namespaces(self):
        ak = state_registry.load_state('AK')
        self.assertEqual(ak['occurrence_backbone']['source_id'], 'ardf')
        self.assertEqual({s['id'] for s in ak['claim_systems']},
                         {'federal_mlrs', 'alaska_state_claims'})

    def test_na_and_zero_are_distinct(self):
        sample = {'n': 2, 'st': ['NY', 'NV'], 'open': [0, 0]}
        got = migrate(sample)['open_ground']
        self.assertEqual(got[0]['status'], 'not_applicable')
        self.assertIsNone(got[0]['distance_m'])
        self.assertEqual(got[1]['status'], 'measured')
        self.assertEqual(got[1]['distance_m'], 0)

    def test_target_sort_keeps_na_distinct_from_measured_zero(self):
        def target(target_id, status, component):
            return {'id': target_id, 'score': 50, 'area_km2': 10,
                    'boosts': {'open': {'status': status, 'score': component}}}
        rows = [target('zero', 'measured', 0),
                target('unknown', 'unknown', None),
                target('na', 'not_applicable', None)]
        rows.sort(key=target_sort_key)
        self.assertEqual([row['id'] for row in rows], ['na', 'zero', 'unknown'])

    def test_land_context_never_infers_minerals_from_surface(self):
        row = normalize_land_context({'class': 'state', 'manager': 'Example'},
                                     state_registry.load_state('NY'))
        self.assertEqual(row['mineral_ownership']['class'], 'unknown')
        self.assertEqual(row['open_ground'], 'not_applicable')


class AlaskaDeadlineTests(unittest.TestCase):
    def test_independent_rent_and_labor_clocks(self):
        got = state_claim_deadlines('2026-06-01', 2026)
        self.assertEqual(got['rent']['initial_due'], '2026-07-16')
        self.assertEqual(got['rent']['subsequent_due'], '2026-09-01')
        self.assertEqual(got['rent']['received_grace_ends'], '2026-11-30')
        self.assertEqual(got['rent']['abandonment_if_unpaid'], '2026-12-01')
        self.assertEqual(got['labor']['cash_in_lieu_due'], '2026-09-01')
        self.assertEqual(got['labor']['statement_recording_due'], '2026-11-30')

    def test_selected_land_conveyance_clock(self):
        got = state_claim_deadlines('2020-01-01', 2026, conveyed_date='2026-05-01')
        self.assertEqual(got['rent']['initial_due'], '2026-07-30')

    def test_rent_requires_effective_schedule(self):
        with self.assertRaises(ValueError):
            rent_amount(3, 40, {})


if __name__ == '__main__':
    unittest.main()
