import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

snapshot = importlib.import_module('arcgis_snapshot')
claims = importlib.import_module('fetch_ak_claims')
ardf = importlib.import_module('fetch_ardf')


def metadata(name='State Mining Claim Active', geometry='esriGeometryPolygon'):
    return {
        'id': 0,
        'name': name,
        'type': 'Feature Layer',
        'geometryType': geometry,
        'capabilities': 'Map,Query,Data',
        'currentVersion': 11.4,
        'maxRecordCount': 2000,
        'serviceItemId': 'official-item',
        # Alaska DNR currently omits both top-level declarations. The one
        # typed field is the authoritative fallback.
        'objectIdField': None,
        'objectIdFieldName': None,
        'sourceSpatialReference': {'wkid': 102006, 'latestWkid': 3338},
        'fields': [
            {'name': 'OBJECTID', 'type': 'esriFieldTypeOID'},
            {'name': 'CASE_ID', 'type': 'esriFieldTypeString'},
        ],
    }


def feature(oid, serial=None):
    return {
        'attributes': {
            'OBJECTID': oid,
            'CASE_ID': serial or f'ADL {oid}',
        },
        'geometry': {'rings': [[
            [-150.0, 60.0], [-150.0, 60.1], [-149.9, 60.1],
            [-149.9, 60.0], [-150.0, 60.0],
        ]]},
    }


class ArcgisSnapshotTests(unittest.TestCase):
    def _capture(self, responses):
        with mock.patch.object(
                snapshot, '_request_json', side_effect=responses) as request:
            rows, evidence = snapshot.capture_layer(
                'https://example.test/MapServer/0',
                expected_name='State Mining Claim Active',
                expected_geometry='esriGeometryPolygon',
                required_fields={
                    'OBJECTID': 'esriFieldTypeOID',
                    'CASE_ID': 'esriFieldTypeString',
                },
                out_fields=('OBJECTID', 'CASE_ID'),
                page=2,
                geometry_precision=8,
            )
        return rows, evidence, request

    def test_pins_typed_oid_fetches_exact_pages_and_repeats_every_row(self):
        ids = {'objectIdFieldName': 'OBJECTID', 'objectIds': [3, 1, 2]}
        page_one = {'features': [feature(1), feature(2)]}
        page_two = {'features': [feature(3)]}
        rows, evidence, request = self._capture([
            metadata(), ids,
            page_one, page_two,
            page_one, page_two,
            metadata(), ids,
        ])
        self.assertEqual(
            [row['attributes']['OBJECTID'] for row in rows], [1, 2, 3])
        self.assertEqual(evidence['object_id_field'], 'OBJECTID')
        self.assertEqual(evidence['n'], 3)
        self.assertEqual(evidence['minimum_object_id'], 1)
        self.assertEqual(evidence['maximum_object_id'], 3)
        self.assertRegex(evidence['object_ids_sha256'], r'^[0-9a-f]{64}$')
        self.assertRegex(evidence['layer_metadata_sha256'], r'^[0-9a-f]{64}$')
        self.assertRegex(evidence['records_sha256'], r'^[0-9a-f]{64}$')
        self.assertTrue(
            evidence['verification']['full_second_feature_pass'])
        page_calls = [call for call in request.call_args_list
                      if call.kwargs.get('post')]
        self.assertEqual(
            [call.args[1]['objectIds'] for call in page_calls],
            ['1,2', '3', '1,2', '3'])
        self.assertTrue(all(call.args[1]['geometryPrecision'] == 8
                            for call in page_calls))

    def test_rejects_snapshot_page_mismatch_and_duplicate_inventory(self):
        with mock.patch.object(snapshot, '_request_json', side_effect=[
                metadata(),
                {'objectIdFieldName': 'OBJECTID', 'objectIds': [1, 1]},
        ]):
            with self.assertRaisesRegex(RuntimeError, 'contains duplicates'):
                snapshot.capture_layer(
                    'https://example.test/MapServer/0',
                    expected_name='State Mining Claim Active',
                    expected_geometry='esriGeometryPolygon',
                    required_fields={'CASE_ID': 'esriFieldTypeString'},
                    out_fields=('CASE_ID',))

        with self.assertRaisesRegex(RuntimeError, 'snapshot/page mismatch'):
            self._capture([
                metadata(),
                {'objectIdFieldName': 'OBJECTID', 'objectIds': [1, 2]},
                {'features': [feature(1)]},
            ])

    def test_rejects_in_place_row_mutation_during_second_pass(self):
        changed = feature(2, 'ADL 999')
        with self.assertRaisesRegex(RuntimeError, 'content mutated'):
            self._capture([
                metadata(),
                {'objectIdFieldName': 'OBJECTID', 'objectIds': [1, 2]},
                {'features': [feature(1), feature(2)]},
                {'features': [feature(1), changed]},
            ])

    def test_rejects_postflight_id_or_metadata_mutation(self):
        ids = {'objectIdFieldName': 'OBJECTID', 'objectIds': [1]}
        with self.assertRaisesRegex(RuntimeError, 'object-ID inventory mutated'):
            self._capture([
                metadata(), ids,
                {'features': [feature(1)]},
                {'features': [feature(1)]},
                metadata(),
                {'objectIdFieldName': 'OBJECTID', 'objectIds': [1, 2]},
            ])

        changed_metadata = metadata()
        changed_metadata['maxRecordCount'] = 1000
        with self.assertRaisesRegex(RuntimeError, 'metadata mutated'):
            self._capture([
                metadata(), ids,
                {'features': [feature(1)]},
                {'features': [feature(1)]},
                changed_metadata, ids,
            ])

    def test_rejects_layer_identity_or_required_schema_drift(self):
        wrong_name = metadata(name='Other layer')
        with mock.patch.object(snapshot, '_request_json', return_value=wrong_name):
            with self.assertRaisesRegex(RuntimeError, 'identity/geometry'):
                snapshot.capture_layer(
                    'https://example.test/MapServer/0',
                    expected_name='State Mining Claim Active',
                    expected_geometry='esriGeometryPolygon',
                    required_fields={'CASE_ID': 'esriFieldTypeString'},
                    out_fields=('CASE_ID',))
        wrong_type = metadata()
        wrong_type['fields'][1]['type'] = 'esriFieldTypeInteger'
        with mock.patch.object(snapshot, '_request_json', return_value=wrong_type):
            with self.assertRaisesRegex(RuntimeError, 'field schema changed'):
                snapshot.capture_layer(
                    'https://example.test/MapServer/0',
                    expected_name='State Mining Claim Active',
                    expected_geometry='esriGeometryPolygon',
                    required_fields={'CASE_ID': 'esriFieldTypeString'},
                    out_fields=('CASE_ID',))


class AlaskaFetcherTests(unittest.TestCase):
    def test_claim_pull_retains_source_oid_and_reviewable_inventory(self):
        source = feature(7, 'ADL 7')
        source['attributes'].update({
            'CLAIM_NAME': 'Claim seven', 'CSSTTSDSCR': 'Active',
            'NTPSTDT': 1, 'DATE_ALF': 2, 'RFRNCMTRSC': 'F001N001E01',
            'NUM_MTR': '1', 'NMSCTNS': '1', 'TOT_ACRES': '40',
            'FILENUMBER': '7', 'INFO_LINK': 'https://dnr.test/7',
            'RFRSHDT': 3,
        })
        evidence = {'records_sha256': snapshot.canonical_sha256([source])}
        with mock.patch.object(
                claims, 'capture_layer', return_value=([source], evidence)) as capture:
            rows, got_evidence = claims.pull('active')
        self.assertEqual(rows[0]['source_objectid'], 7)
        self.assertEqual(rows[0]['serial'], 'ADL 7')
        self.assertIs(got_evidence, evidence)
        self.assertEqual(capture.call_args.kwargs['geometry_precision'], 8)
        self.assertEqual(capture.call_args.kwargs['page'], 500)

    def test_claim_run_writes_atomic_private_snapshot_contract(self):
        evidences = {
            status: {'records_sha256': status * 2} for status in claims.LAYERS
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'claims.json')
            with mock.patch.object(claims, 'pull', side_effect=[
                    ([{'status': status}], evidences[status])
                    for status in claims.LAYERS
            ]):
                result = claims.run(path)
            with open(path, encoding='utf-8') as source:
                written = json.load(source)
        self.assertEqual(written, result)
        self.assertEqual(
            result['snapshot_contract'], 'arcgis-objectids-double-pass-v1')
        self.assertEqual(set(result['source_inventory']), set(claims.LAYERS))

    def test_ardf_run_retains_properties_and_inventory(self):
        source = {
            'attributes': {'OBJECTID': 1, 'ARDF_no': 'AA001'},
            'geometry': {'x': -149.5, 'y': 60.5},
        }
        evidence = {'records_sha256': snapshot.canonical_sha256([source])}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                ardf, 'capture_layer', return_value=([source], evidence)) as capture:
            path = os.path.join(directory, 'ardf.json')
            result = ardf.run(path)
            with open(path, encoding='utf-8') as opened:
                written = json.load(opened)
        self.assertEqual(written, result)
        self.assertEqual(result['n'], 1)
        self.assertEqual(result['features'][0]['properties']['OBJECTID'], 1)
        self.assertEqual(result['source_inventory'], evidence)
        self.assertEqual(capture.call_args.kwargs['geometry_precision'], 8)

    def test_fetchers_reject_browser_tree_and_output_symlinks(self):
        for module in (claims, ardf):
            with self.subTest(module=module.__name__), self.assertRaisesRegex(
                    ValueError, 'staging-only'):
                module._private_output(os.path.join(
                    ROOT, 'site', 'data', 'raw-snapshot.json'))
            with tempfile.TemporaryDirectory() as directory:
                target = os.path.join(directory, 'target.json')
                link = os.path.join(directory, 'link.json')
                with open(target, 'w', encoding='utf-8') as output:
                    output.write('{}')
                os.symlink(target, link)
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    module._private_output(link)


if __name__ == '__main__':
    unittest.main()
