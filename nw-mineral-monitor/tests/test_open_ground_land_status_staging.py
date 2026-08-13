import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_open_ground_land_status_staging as land

try:
    from shapely.geometry import Polygon
except ImportError:
    Polygon = None


class OpenGroundLandStatusPureTests(unittest.TestCase):
    def test_only_explicit_mineral_scope_blocks(self):
        for value in ('All', 'ALL MINERALS', 'LOCATABLE MINERALS',
                      'METALLIC MINERALS'):
            with self.subTest(value=value):
                self.assertTrue(land._mineral_scope_blocks(value))
        for value in (None, '', 'N/A', 'SURFACE ESTATE CLOSED',
                      'Not segregated', 'CLSD-EXCP METALIFRUS MNG'):
            with self.subTest(value=value):
                self.assertFalse(land._mineral_scope_blocks(value))

    def test_closed_or_terminated_case_is_not_current(self):
        self.assertFalse(land._current_case({'CSE_DISP': 'Closed'}))
        self.assertFalse(land._current_case({'CSE_DISP': 'Terminated'}))
        self.assertFalse(land._current_case({'CSE_DISP': None}))
        self.assertTrue(land._current_case({'CSE_DISP': 'Authorized'}))
        self.assertTrue(land._current_case({'CSE_DISP': 'Interim'}))

    def test_surface_management_never_becomes_title(self):
        self.assertEqual(land._manager({
            'ADMIN_AGENCY_CODE': 'BLM', 'ADMIN_DEPT_CODE': 'DOI',
            'ADMIN_UNIT_NAME': 'Example Field Office'}), 'BLM')
        self.assertEqual(land._manager({
            'ADMIN_AGENCY_CODE': 'UND', 'ADMIN_DEPT_CODE': 'UND',
            'ADMIN_UNIT_NAME': 'Undetermined'}), 'UNKNOWN')

    def test_bounded_references_are_deterministic_and_auditable(self):
        values = [f'withdrawals:X{i:02d}' for i in range(30)]
        first = land._bounded_refs(values)
        second = land._bounded_refs(reversed(values))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 20)
        self.assertRegex(first[-1], r'^additional-11:sha256:[0-9a-f]{24}$')

    def test_registered_source_families_are_exact(self):
        self.assertEqual(set(land.REQUIRED_FAMILIES),
                         {'sma', 'withdrawals', 'segregations', 'nlcs'})
        self.assertEqual({spec['family'] for spec in land.LAYERS.values()},
                         set(land.REQUIRED_FAMILIES))


@unittest.skipUnless(Polygon, 'Shapely not installed')
class OpenGroundLandStatusGeometryTests(unittest.TestCase):
    def setUp(self):
        self.section = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        self.surface = {
            'key': 'sma', 'manager': 'BLM', 'blocking': False,
            'reference': 'sma:OBJECTID-1', 'geometry': self.section,
        }

    def test_no_closure_never_becomes_open(self):
        from shapely.ops import unary_union
        row = land._classify(self.section, [self.surface], unary_union)
        self.assertEqual(row['mineral_disposition'], 'unknown')
        self.assertFalse(row['boundary_uncertain'])
        self.assertEqual(row['surface_manager'], 'BLM')
        self.assertIn('SMA is not mineral title', row['evidence'])

    def test_whole_section_block_is_withdrawn(self):
        from shapely.ops import unary_union
        block = {
            'key': 'withdrawals', 'manager': None, 'blocking': True,
            'reference': 'withdrawals:NMC1', 'geometry': self.section,
        }
        row = land._classify(
            self.section, [self.surface, block], unary_union)
        self.assertEqual(row['mineral_disposition'], 'withdrawn')
        self.assertFalse(row['boundary_uncertain'])
        self.assertEqual(row['withdrawal_refs'], ['withdrawals:NMC1'])

    def test_partial_block_is_unknown_and_boundary_uncertain(self):
        from shapely.ops import unary_union
        block = {
            'key': 'segregations_minerals', 'manager': None,
            'blocking': True, 'reference': 'segregations:NMC2',
            'geometry': Polygon([(0, 0), (.5, 0), (.5, 1), (0, 1), (0, 0)]),
        }
        row = land._classify(
            self.section, [self.surface, block], unary_union)
        self.assertEqual(row['mineral_disposition'], 'unknown')
        self.assertTrue(row['boundary_uncertain'])
        self.assertEqual(row['withdrawal_refs'], [])

    @staticmethod
    def plss_document():
        section_id = 'AZSECTION001'
        return {
            'schema_version': 1, 'state': 'AZ', 'kind': 'plss',
            'retrieved': '2026-08-13', 'complete': True, 'n': 1,
            'source': land.open_ground.PLSS_SOURCE,
            'type': 'FeatureCollection',
            'features': [{
                'type': 'Feature', 'id': section_id,
                'properties': {'section_id': section_id,
                               'label': 'AZ test section'},
                'geometry': {
                    'type': 'Polygon',
                    'coordinates': [[[-112.01, 33.99], [-111.99, 33.99],
                                     [-111.99, 34.01], [-112.01, 34.01],
                                     [-112.01, 33.99]]],
                },
            }],
        }

    def test_end_to_end_empty_source_snapshots_stay_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            plss = os.path.join(directory, 'az_plss.json')
            output = os.path.join(directory, 'az_land_status.json')
            with open(plss, 'w', encoding='utf-8') as target:
                json.dump(self.plss_document(), target, separators=(',', ':'))
            with mock.patch.object(land, '_metadata_contract',
                                   return_value='a' * 64), \
                    mock.patch.object(land.claim_plss, '_snapshot_ids',
                                      return_value=[]), \
                    mock.patch.object(land.claim_plss, '_iter_features',
                                      return_value=iter(())):
                report = land.build_state(
                    'AZ', plss, output, '2026-08-13')
            with open(output, encoding='utf-8') as source:
                document = json.load(source)
        self.assertEqual(report['open_sections'], 0)
        self.assertFalse(report['release_ready'])
        self.assertEqual(report['unknown_sections'], 1)
        self.assertEqual(len(report['sources']), 6)
        self.assertEqual(
            document['classifications'][0]['mineral_disposition'], 'unknown')
        self.assertEqual(set(document['classifications'][0]['checked_sources']),
                         set(land.REQUIRED_FAMILIES))

    def test_source_id_mutation_never_publishes_output(self):
        with tempfile.TemporaryDirectory() as directory:
            plss = os.path.join(directory, 'az_plss.json')
            output = os.path.join(directory, 'az_land_status.json')
            with open(plss, 'w', encoding='utf-8') as target:
                json.dump(self.plss_document(), target, separators=(',', ':'))
            with mock.patch.object(land, '_metadata_contract',
                                   return_value='a' * 64), \
                    mock.patch.object(land.claim_plss, '_snapshot_ids',
                                      side_effect=[[], [1]]), \
                    mock.patch.object(land.claim_plss, '_iter_features',
                                      return_value=iter(())):
                with self.assertRaisesRegex(
                        land.LandStatusStagingError, 'changed during'):
                    land.build_state('AZ', plss, output, '2026-08-13')
            self.assertFalse(os.path.exists(output))


if __name__ == '__main__':
    unittest.main()
