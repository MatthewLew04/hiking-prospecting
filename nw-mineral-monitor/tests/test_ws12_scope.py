"""WS12 scope assertion: exactly 49 states, Alaska in, Hawaii/DC out.

The scope must agree across the WS12 portal registry, the WS11 state
registry, and the states/ directory, and every in-scope state must carry an
explicit portal packet so a silently dropped state cannot pass CI.
"""
import copy
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import portal_registry
import state_registry


EXPECTED_SCOPE = frozenset(
    'AK AL AR AZ CA CO CT DE FL GA IA ID IL IN KS KY LA MA MD ME MI MN MO '
    'MS MT NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA '
    'WI WV WY'.split())


class ScopeAssertionTests(unittest.TestCase):
    def test_scope_is_exactly_49_unique_codes(self):
        self.assertEqual(len(portal_registry.SCOPE_STATES), 49)
        self.assertEqual(portal_registry.SCOPE_STATES, EXPECTED_SCOPE)

    def test_alaska_in_scope_hawaii_and_dc_out(self):
        self.assertIn('AK', portal_registry.SCOPE_STATES)
        self.assertNotIn('HI', portal_registry.SCOPE_STATES)
        self.assertNotIn('DC', portal_registry.SCOPE_STATES)
        self.assertEqual(portal_registry.validate_scope(), [])

    def test_scope_agrees_with_ws11_state_registry(self):
        self.assertEqual(portal_registry.SCOPE_STATES,
                         state_registry.ALL_STATES)

    def test_scope_agrees_with_states_meta_and_directory(self):
        meta_path = os.path.join(ROOT, 'states', '_meta.yaml')
        with open(meta_path, encoding='utf-8') as source:
            meta = json.load(source)
        claim_states = set(meta['claim_states'])
        self.assertTrue(claim_states <= portal_registry.SCOPE_STATES)
        self.assertNotIn('HI', claim_states)
        self.assertNotIn('DC', claim_states)
        self.assertIn('49 states', meta['scope'])
        state_files = {
            name[:-5] for name in os.listdir(os.path.join(ROOT, 'states'))
            if name.endswith('.yaml') and not name.startswith('_')}
        self.assertEqual(state_files, set(portal_registry.SCOPE_STATES))

    def test_tampered_scope_fails_validation(self):
        original = portal_registry.SCOPE_STATES
        try:
            portal_registry.SCOPE_STATES = original - {'AK'}
            errors = portal_registry.validate_scope()
            self.assertTrue(any('AK' in error for error in errors))
            self.assertTrue(any('49' in error for error in errors))
            portal_registry.SCOPE_STATES = (original - {'AK'}) | {'HI'}
            errors = portal_registry.validate_scope()
            self.assertTrue(any('HI' in error for error in errors))
            portal_registry.SCOPE_STATES = (original - {'AK'}) | {'DC'}
            errors = portal_registry.validate_scope()
            self.assertTrue(any('DC' in error for error in errors))
        finally:
            portal_registry.SCOPE_STATES = original


class RegistryCoverageTests(unittest.TestCase):
    def test_every_in_scope_state_has_a_portal_packet(self):
        result = portal_registry.validate_registry()
        self.assertEqual(result['errors'], [])
        self.assertTrue(result['ok'])
        self.assertEqual(result['states_covered'], 49)

    def test_hidden_files_are_ignored_by_the_loader(self):
        # macOS AppleDouble entries ('._ak.yaml') extracted on Linux must not
        # be parsed as portal packets.
        with tempfile.TemporaryDirectory() as staging:
            for name in os.listdir(portal_registry.PORTALS_DIR):
                if name.endswith('.yaml'):
                    with open(os.path.join(portal_registry.PORTALS_DIR, name),
                              encoding='utf-8') as handle:
                        payload = handle.read()
                    with open(os.path.join(staging, name), 'w',
                              encoding='utf-8') as handle:
                        handle.write(payload)
            with open(os.path.join(staging, '._ak.yaml'), 'wb') as handle:
                handle.write(b'\x00\x05\x16\x07binary-appledouble\xa3')
            result = portal_registry.validate_registry(staging)
            self.assertTrue(result['ok'], result['errors'])

    def test_missing_state_packet_fails_validation(self):
        with tempfile.TemporaryDirectory() as staging:
            for name in os.listdir(portal_registry.PORTALS_DIR):
                if not name.endswith('.yaml') or name == 'wy.yaml':
                    continue
                source = os.path.join(portal_registry.PORTALS_DIR, name)
                with open(source, encoding='utf-8') as handle:
                    payload = handle.read()
                with open(os.path.join(staging, name), 'w',
                          encoding='utf-8') as handle:
                    handle.write(payload)
            result = portal_registry.validate_registry(staging)
            self.assertFalse(result['ok'])
            self.assertTrue(any(
                'in-scope states without a portal packet' in error and
                'WY' in error for error in result['errors']))

    def test_out_of_scope_packet_fails_validation(self):
        with tempfile.TemporaryDirectory() as staging:
            for name in os.listdir(portal_registry.PORTALS_DIR):
                if name.endswith('.yaml'):
                    source = os.path.join(portal_registry.PORTALS_DIR, name)
                    with open(source, encoding='utf-8') as handle:
                        payload = handle.read()
                    with open(os.path.join(staging, name), 'w',
                              encoding='utf-8') as handle:
                        handle.write(payload)
            with open(os.path.join(portal_registry.PORTALS_DIR, 'de.yaml'),
                      encoding='utf-8') as handle:
                envelope = json.load(handle)
            envelope['jurisdiction'] = 'HI'
            for row in envelope['portals']:
                row['jurisdiction'] = 'HI'
                row['id'] = 'hi_' + row['id']
            with open(os.path.join(staging, 'hi.yaml'), 'w',
                      encoding='utf-8') as handle:
                json.dump(envelope, handle, indent=2)
            result = portal_registry.validate_registry(staging)
            self.assertFalse(result['ok'])
            self.assertTrue(any(
                'outside the 49-state scope' in error and 'HI' in error
                for error in result['errors']))


class EntryVerifiedStatusTests(unittest.TestCase):
    def _load(self, code):
        path = os.path.join(portal_registry.PORTALS_DIR, f'{code}.yaml')
        with open(path, encoding='utf-8') as source:
            return json.load(source)

    def test_entry_verified_rows_validate_and_stay_incomplete(self):
        registry = portal_registry.load_registry()
        rows = [row for row in registry.values()
                if row['status'] == 'entry_verified_discovery_pending']
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertFalse(row['harvest_state']['full_crawl_complete'])
            self.assertIsNone(row['harvest_state']['manifest_sha256'])
            self.assertNotIn('crawler', row)

    def test_entry_verified_rejects_executable_crawler_config(self):
        envelope = self._load('de')
        row = copy.deepcopy(envelope['portals'][0])
        row['crawler'] = {'adapter': 'html_catalog',
                         'automation_permitted': True,
                         'min_interval_seconds': 1.0}
        with self.assertRaises(portal_registry.PortalRegistryError):
            portal_registry.validate_portal(row)

    def test_blocked_probe_cohort_records_evidence(self):
        registry = portal_registry.load_registry()
        for portal_id in ('ma_survey_publications', 'nh_survey_publications',
                          'vt_survey_publications', 'il_ilmines_wiki'):
            row = registry[portal_id]
            self.assertEqual(row['status'], 'blocked_by_access_controls')
            self.assertGreaterEqual(len(row['probe']['result']), 20)
            self.assertFalse(row['harvest_state']['full_crawl_complete'])


if __name__ == '__main__':
    unittest.main()
