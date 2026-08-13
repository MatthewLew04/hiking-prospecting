import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_open_ground_claim_plss_staging as staging
from tests.test_federal_mlrs_pmtiles import _snapshot, _write_json

try:
    import shapely  # noqa: F401
except ImportError:
    shapely = None


def _polygon(left, bottom, right, top):
    return {'type': 'Polygon', 'coordinates': [[
        [left, bottom], [right, bottom], [right, top], [left, top],
        [left, bottom],
    ]]}


class OpenGroundLayerContractTests(unittest.TestCase):
    @staticmethod
    def metadata(**changes):
        value = {
            'name': 'Active Mining Claims',
            'geometryType': 'esriGeometryPolygon',
            'maxRecordCount': 2000,
            'fields': [
                {'name': 'OBJECTID', 'type': 'esriFieldTypeOID'},
                {'name': 'CSE_NR', 'type': 'esriFieldTypeString'},
            ],
        }
        value.update(changes)
        return value

    def test_typed_oid_field_is_authoritative_when_optional_key_absent(self):
        with mock.patch.object(staging, '_request_json',
                               return_value=self.metadata()):
            digest = staging._layer_contract(
                staging.CLAIMS_LAYER, 'Active Mining Claims')
        self.assertRegex(digest, r'^[0-9a-f]{64}$')

    def test_optional_oid_alias_must_agree_with_typed_schema(self):
        with mock.patch.object(staging, '_request_json', return_value=self.metadata(
                objectIdField='FID')):
            with self.assertRaisesRegex(staging.StagingError,
                                        'identity/schema changed'):
                staging._layer_contract(
                    staging.CLAIMS_LAYER, 'Active Mining Claims')

    def test_oid_field_type_drift_fails_closed(self):
        fields = [
            {'name': 'OBJECTID', 'type': 'esriFieldTypeInteger'},
            {'name': 'CSE_NR', 'type': 'esriFieldTypeString'},
        ]
        with mock.patch.object(staging, '_request_json', return_value=self.metadata(
                fields=fields)):
            with self.assertRaisesRegex(staging.StagingError,
                                        'identity/schema changed'):
                staging._layer_contract(
                    staging.CLAIMS_LAYER, 'Active Mining Claims')

    def test_arcgis_null_id_array_is_a_truthful_zero(self):
        with mock.patch.object(staging, '_request_json', return_value={
                'objectIdFieldName': 'OBJECTID', 'objectIds': None}):
            self.assertEqual(staging._snapshot_ids(
                staging.CLAIMS_LAYER, '1=1', [[-1, -1, 1, 1]]), [])

    def test_missing_or_wrong_typed_id_array_fails_closed(self):
        for response in (
                {'objectIdFieldName': 'OBJECTID'},
                {'objectIdFieldName': 'OBJECTID', 'objectIds': 'none'}):
            with self.subTest(response=response), mock.patch.object(
                    staging, '_request_json', return_value=response):
                with self.assertRaisesRegex(staging.StagingError,
                                            'object-ID array'):
                    staging._snapshot_ids(
                        staging.CLAIMS_LAYER, '1=1', [[-1, -1, 1, 1]])


@unittest.skipUnless(shapely, 'Shapely not installed')
class OpenGroundClaimPlssStagingTests(unittest.TestCase):
    def fixture(self, directory, serial='AZ000001'):
        private = os.path.join(directory, 'private')
        os.makedirs(private)
        active = os.path.join(private, 'az_active.json')
        data = _snapshot('AZ', 'active', ((serial, -112.0, 34.0),))
        _write_json(active, data)
        return private, active

    @staticmethod
    def features(layer, ids, fields):
        if layer == staging.PLSS_LAYER:
            yield {'type': 'Feature', 'id': 20,
                   'properties': {
                       'OBJECTID': 20, 'PLSSID': 'AZ140010N0010E0',
                       'FRSTDIVID': 'AZ140010N0010E0SN010',
                       'FRSTDIVLAB': '1', 'FRSTDIVNO': '1'},
                   'geometry': _polygon(-112.1, 33.9, -111.9, 34.1)}
        else:
            yield {'type': 'Feature', 'id': 10,
                   'properties': {'OBJECTID': 10, 'CSE_NR': 'AZ000001',
                                  'CSE_NAME': 'Example', 'CSE_DISP': 'Active'},
                   'geometry': _polygon(-112.05, 33.95, -111.95, 34.05)}

    def run_build(self, directory, *, feature_source=None):
        private, active = self.fixture(directory)
        plss = os.path.join(private, 'az_plss.json')
        claims = os.path.join(private, 'az_active_claims.json')
        feature_source = feature_source or self.features

        def ids(layer, where, envelopes):
            return [20] if layer == staging.PLSS_LAYER else [10]

        with mock.patch.object(staging, '_layer_contract', return_value='a' * 64), \
                mock.patch.object(staging, '_snapshot_ids', side_effect=ids), \
                mock.patch.object(staging, '_iter_features', side_effect=feature_source):
            result = staging.build_state('AZ', active, plss, claims)
        return result, plss, claims

    def test_exact_positive_area_join_emits_downstream_schemas(self):
        with tempfile.TemporaryDirectory() as directory:
            result, plss, claims = self.run_build(directory)
            with open(plss, encoding='utf-8') as source:
                sections = json.load(source)
            with open(claims, encoding='utf-8') as source:
                active = json.load(source)
        self.assertEqual(result['claims'], 1)
        self.assertEqual(result['sections'], 1)
        self.assertEqual(result['unmapped_claims'], 0)
        self.assertEqual(active['claims'][0]['section_ids'],
                         ['AZ140010N0010E0SN010'])
        self.assertTrue(active['claims'][0]['mapping_complete'])
        self.assertEqual(sections['features'][0]['id'],
                         'AZ140010N0010E0SN010')
        self.assertEqual(len(result['claims_object_ids_sha256']), 64)

    def test_live_serial_drift_fails_without_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            private, active = self.fixture(directory, serial='AZ_EXPECTED')
            plss = os.path.join(private, 'az_plss.json')
            claims = os.path.join(private, 'az_active_claims.json')
            with mock.patch.object(staging, '_layer_contract', return_value='a' * 64), \
                    mock.patch.object(staging, '_snapshot_ids', side_effect=[
                        [10], [20]]), \
                    mock.patch.object(staging, '_iter_features', side_effect=self.features):
                with self.assertRaisesRegex(staging.StagingError,
                                            'serials differ'):
                    staging.build_state('AZ', active, plss, claims)
            self.assertFalse(os.path.exists(plss))
            self.assertFalse(os.path.exists(claims))

    def test_boundary_touch_is_unmapped_not_falsely_active(self):
        def touching(layer, ids, fields):
            if layer == staging.PLSS_LAYER:
                yield from self.features(layer, ids, fields)
            else:
                claim = next(self.features(layer, ids, fields))
                claim = copy.deepcopy(claim)
                claim['geometry'] = _polygon(-111.9, 33.95, -111.8, 34.05)
                # Centroid must still match the active snapshot/state; only
                # the section boundary at -111.9 touches.
                yield claim

        with tempfile.TemporaryDirectory() as directory:
            result, _, claims = self.run_build(directory, feature_source=touching)
            with open(claims, encoding='utf-8') as source:
                active = json.load(source)
        self.assertEqual(result['unmapped_claims'], 1)
        self.assertEqual(active['claims'][0]['section_ids'], [])
        self.assertFalse(active['claims'][0]['mapping_complete'])

    def test_pair_publication_rolls_back_if_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            private, active = self.fixture(directory)
            plss = os.path.join(private, 'az_plss.json')
            claims = os.path.join(private, 'az_active_claims.json')
            with open(plss, 'wb') as target:
                target.write(b'old-plss')
            with open(claims, 'wb') as target:
                target.write(b'old-claims')

            def ids(layer, where, envelopes):
                return [20] if layer == staging.PLSS_LAYER else [10]

            real_replace = os.replace
            calls = 0

            def fail_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('simulated second replace failure')
                return real_replace(source, destination)

            with mock.patch.object(staging, '_layer_contract', return_value='a' * 64), \
                    mock.patch.object(staging, '_snapshot_ids', side_effect=ids), \
                    mock.patch.object(staging, '_iter_features', side_effect=self.features), \
                    mock.patch.object(staging.os, 'replace', side_effect=fail_second):
                with self.assertRaisesRegex(OSError, 'second replace'):
                    staging.build_state('AZ', active, plss, claims)
            with open(plss, 'rb') as source:
                self.assertEqual(source.read(), b'old-plss')
            with open(claims, 'rb') as source:
                self.assertEqual(source.read(), b'old-claims')


if __name__ == '__main__':
    unittest.main()
