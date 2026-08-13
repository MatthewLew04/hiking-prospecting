"""Fail-closed national/browser contracts for state-survey baselines."""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

national = importlib.import_module('validate_national')
az = importlib.import_module('build_arizona_state_survey_pmtiles')
co = importlib.import_module('build_colorado_state_survey_pmtiles')
nv = importlib.import_module('build_nevada_state_survey_pmtiles')
ut = importlib.import_module('build_utah_state_survey_pmtiles')


def manifest():
    with open(os.path.join(ROOT, 'site', 'data', 'manifest.json'),
              encoding='utf-8') as source:
        return json.load(source)


class StateSurveyIntegrationTests(unittest.TestCase):
    def test_national_groups_match_every_builder_owned_atomic_key_set(self):
        modules = {'AZ': az, 'CO': co, 'NV': nv, 'UT': ut}
        for state, module in modules.items():
            with self.subTest(state=state):
                self.assertEqual(
                    national._STATE_SURVEY_BASELINE_GROUPS[state]['keys'],
                    frozenset(module.BASELINE_KEYS))

    def test_every_advertised_atomic_group_dispatches_exact_builder_validator(self):
        baselines = manifest()['national_baselines']
        modules = {'AZ': az, 'CO': co, 'NV': nv, 'UT': ut}
        patches = [mock.patch.object(module, 'validate_manifest_baselines')
                   for module in modules.values()]
        exact = [patcher.start() for patcher in patches]
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(patches)])
        qa = national.QA()
        national.validate_state_survey_baselines(qa, baselines)
        self.assertEqual(qa.errors, [])
        for (state, _), called in zip(modules.items(), exact):
            if national._STATE_SURVEY_BASELINE_GROUPS[state]['keys'] & set(baselines):
                called.assert_called_once_with(
                    {'national_baselines': baselines},
                    pmtiles_header=national._pmtiles_header)
            else:
                called.assert_not_called()

    def test_complete_utah_group_dispatches_only_the_utah_exact_validator(self):
        baselines = {
            key: {'status': 'baseline_not_release'}
            for key in national._STATE_SURVEY_BASELINE_GROUPS['UT']['keys']
        }
        with mock.patch.object(ut, 'validate_manifest_baselines') as exact:
            qa = national.QA()
            national.validate_state_survey_baselines(qa, baselines)
        self.assertEqual(qa.errors, [])
        exact.assert_called_once_with(
            {'national_baselines': baselines},
            pmtiles_header=national._pmtiles_header)

    def test_partial_and_unrecognized_sets_fail_closed(self):
        co_keys = national._STATE_SURVEY_BASELINE_GROUPS['CO']['keys']
        qa = national.QA()
        national.validate_state_survey_baselines(
            qa, {next(iter(co_keys)): {'status': 'baseline_not_release'}})
        self.assertTrue(any('CO state-survey' in error and 'atomic three-archive'
                            in error for error in qa.errors), qa.errors)

        ut_keys = national._STATE_SURVEY_BASELINE_GROUPS['UT']['keys']
        qa = national.QA()
        national.validate_state_survey_baselines(
            qa, {next(iter(ut_keys)): {'status': 'baseline_not_release'}})
        self.assertTrue(any('UT state-survey' in error and 'atomic four-archive'
                            in error for error in qa.errors), qa.errors)

        qa = national.QA()
        national.validate_state_survey_baselines(qa, {
            'xy_unchecked': {'status': 'baseline_not_release', 'format': 'pmtiles'},
        })
        self.assertTrue(any('no exact offline validator' in error
                            for error in qa.errors), qa.errors)

    def test_registry_generic_pointers_match_exact_az_co_and_ut_manifest_entries(self):
        baselines = manifest()['national_baselines']
        qa = national.QA()
        national.validate_registry_baseline_pointers(qa, baselines)
        self.assertEqual(qa.errors, [])

        states = national.load_states()
        broken = copy.deepcopy(states)
        broken['AZ']['geological_survey']['baseline_artifacts'][
            'az_azgs_map35_2025']['baseline_sha256'] = '0' * 64
        qa = national.QA()
        with mock.patch.object(national, 'load_states', return_value=broken):
            national.validate_registry_baseline_pointers(qa, baselines)
        self.assertTrue(any('AZ registry az_azgs_map35_2025.baseline_sha256'
                            in error for error in qa.errors), qa.errors)

    def test_registry_requires_exact_generic_pointer_table_for_advertised_utah(self):
        baselines = copy.deepcopy(manifest()['national_baselines'])
        for index, key in enumerate(sorted(
                national._STATE_SURVEY_BASELINE_GROUPS['UT']['keys'])):
            baselines[key] = {
                'status': 'baseline_not_release',
                'file': f'data/tiles/states/ut/{key}.pmtiles',
                'n': index + 1,
                'sha256': str(index + 1) * 64,
            }
        states = copy.deepcopy(national.load_states())
        states['UT']['geological_survey']['baseline_artifacts'] = {
            key: {
                'baseline_manifest_key': f'national_baselines.{key}',
                'baseline_browser_path': entry['file'],
                'baseline_features': entry['n'],
                'baseline_sha256': entry['sha256'],
            }
            for key, entry in baselines.items() if key.startswith('ut_ugs_')
        }
        qa = national.QA()
        with mock.patch.object(national, 'load_states', return_value=states):
            national.validate_registry_baseline_pointers(qa, baselines)
        self.assertEqual(qa.errors, [])

        missing = copy.deepcopy(states)
        del missing['UT']['geological_survey']['baseline_artifacts']
        qa = national.QA()
        with mock.patch.object(national, 'load_states', return_value=missing):
            national.validate_registry_baseline_pointers(qa, baselines)
        self.assertTrue(any(
            'UT registry has no geological_survey.baseline_artifacts table'
            in error for error in qa.errors), qa.errors)

        stale = copy.deepcopy(states)
        key = sorted(national._STATE_SURVEY_BASELINE_GROUPS['UT']['keys'])[0]
        stale['UT']['geological_survey']['baseline_artifacts'][key][
            'baseline_sha256'] = '0' * 64
        qa = national.QA()
        with mock.patch.object(national, 'load_states', return_value=stale):
            national.validate_registry_baseline_pointers(qa, baselines)
        self.assertTrue(any(
            f'UT registry {key}.baseline_sha256' in error
            for error in qa.errors), qa.errors)

    def test_registry_keeps_research_baselines_out_of_done_release(self):
        states = national.load_states()
        for code in ('AZ', 'CO', 'UT'):
            with self.subTest(state=code):
                state = states[code]
                pointers = state['geological_survey']['baseline_artifacts']
                self.assertEqual(
                    set(pointers),
                    national._STATE_SURVEY_BASELINE_GROUPS[code]['keys'])
                self.assertEqual(state['release']['status'], 'building')
                self.assertFalse(state['release']['enabled'])
                self.assertEqual(state['done_gate']['geology_faults']['status'], 'fail')

    def test_browser_uses_one_generic_lazy_state_survey_contract(self):
        with open(os.path.join(ROOT, 'site', 'index.html'),
                  encoding='utf-8') as source:
            html = source.read()
        self.assertIn('const STATE_SURVEY_LAYERS = []', html)
        self.assertIn('entry.browser_descriptor', html)
        self.assertIn('function syncStateSurveyLayers()', html)
        self.assertIn("['==',['get','st'],descriptor.state]", html)
        self.assertIn('...STATE_SURVEY_LAYERS.flatMap', html)
        self.assertIn('stateSurveyFeatureTitle(item.layer,p)', html)
        self.assertIn('releasePmtiles(descriptor.file)', html)
        self.assertNotIn('function syncNvBaselines()', html)
        self.assertNotRegex(
            html,
            r"(?:fetch|jget)\s*\(\s*['\"`]data/tiles/states/.+\.geojson")


if __name__ == '__main__':
    unittest.main()
