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

usmin = importlib.import_module('build_national_usmin_pmtiles')


def source_feature(oid, state='AK'):
    return {
        'type': 'Feature',
        'id': oid,
        'properties': {
            'objectid': oid,
            'state': state,
            'county': 'Example County',
            'ftr_type': 'Adit',
            'ftr_name': 'Example Mine',
            'ftr_azimut': 181,
            'topo_name': 'Example A-1',
            'topo_date': 1963,
            'topo_scale': '63360',
            'gda_id': 5000000 + oid,
            'scanid': 300000 + oid,
        },
        'geometry': {'type': 'Point', 'coordinates': [-150.0, 60.0]},
    }


class NationalUsminTests(unittest.TestCase):
    def test_scope_is_exactly_49_states_without_non_ws11_codes(self):
        self.assertEqual(len(usmin.TARGET_STATES), 49)
        self.assertNotIn('HI', usmin.TARGET_STATES)
        self.assertNotIn('PR', usmin.TARGET_STATES)
        self.assertNotIn('DC', usmin.TARGET_STATES)
        self.assertEqual(usmin.LAYER_ID, 17)
        self.assertTrue(usmin.LAYER.endswith('/FeatureServer/17'))
        self.assertNotIn("'HI'", usmin.STATE_WHERE)
        self.assertNotIn("'PR'", usmin.STATE_WHERE)
        self.assertNotIn("'DC'", usmin.STATE_WHERE)

    def test_result_offset_pages_are_oid_ordered_and_snapshot_bounded(self):
        pages = [
            {'type': 'FeatureCollection',
             'features': [source_feature(1), source_feature(2)],
             'properties': {'exceededTransferLimit': True}},
            {'type': 'FeatureCollection',
             'features': [source_feature(4)],
             'properties': {'exceededTransferLimit': False}},
        ]
        with mock.patch.object(usmin, '_request_json', side_effect=pages) as request:
            got = list(usmin._iter_source_features(3, 4))
        self.assertEqual([feature['properties']['objectid'] for feature in got],
                         [1, 2, 4])
        params = [call.args[0] for call in request.call_args_list]
        self.assertEqual([value['resultOffset'] for value in params], [0, 2])
        self.assertEqual([value['orderByFields'] for value in params],
                         ['objectid ASC', 'objectid ASC'])
        self.assertTrue(all('objectid <= 4' in value['where'] for value in params))
        self.assertTrue(all(value['resultRecordCount'] == 2000 for value in params))

    def test_result_offset_duplicate_oid_fails_loudly(self):
        pages = [
            {'features': [source_feature(1), source_feature(2)],
             'properties': {'exceededTransferLimit': True}},
            {'features': [source_feature(2)],
             'properties': {'exceededTransferLimit': False}},
        ]
        with mock.patch.object(usmin, '_request_json', side_effect=pages):
            with self.assertRaisesRegex(RuntimeError, 'order/uniqueness'):
                list(usmin._iter_source_features(3, 3))

    def test_normalize_uses_numeric_oid_and_compact_provenance(self):
        state, feature = usmin._normalize(source_feature(27, 'NV'))
        self.assertEqual(state, 'NV')
        self.assertEqual(feature['id'], 27)
        self.assertEqual(feature['properties'], {
            'fid': 27,
            'st': 'NV',
            'co': 'Example County',
            'typ': 'Adit',
            'agg': 0,
            'nm': 'Example Mine',
            'az': 181,
            'quad': 'Example A-1',
            'yr': 1963,
            'scale': 63360,
            'gda': 5000027,
            'scan': 300027,
        })
        with self.assertRaisesRegex(RuntimeError, 'unsupported state'):
            usmin._normalize(source_feature(28, 'HI'))

    def test_stream_requires_and_counts_every_target_state(self):
        ordered = sorted(usmin.TARGET_STATES)
        features = [source_feature(index, state)
                    for index, state in enumerate(ordered, 1)]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'usmin.geojsonseq')
            with mock.patch.object(usmin, '_snapshot_bounds', return_value=(49, 49)), \
                    mock.patch.object(usmin, '_iter_source_features',
                                      return_value=iter(features)):
                stats = usmin._stream_usmin(path)
            with open(path, encoding='utf-8') as source:
                rows = [json.loads(line) for line in source]
        self.assertEqual(stats['n'], 49)
        self.assertEqual(set(stats['states']), usmin.TARGET_STATES)
        self.assertTrue(all(count == 1 for count in stats['states'].values()))
        self.assertEqual(len(rows), 49)
        self.assertEqual([row['id'] for row in rows], list(range(1, 50)))
        self.assertEqual(stats['source_ids'], list(range(1, 50)))

    def test_source_id_inventory_rejects_tiler_loss_and_duplicate_source_ids(self):
        with self.assertRaisesRegex(RuntimeError, 'reconciliation failed'):
            usmin._source_id_inventory([1, 2], [1], 1)
        with self.assertRaisesRegex(RuntimeError, 'invalid or duplicated'):
            usmin._source_id_inventory([1, 1], [1], 1)
        inventory = usmin._source_id_inventory([1, 2], [1, 2], 3)
        self.assertEqual(inventory['maxzoom_feature_instances'], 3)

    def test_build_updates_manifest_only_after_valid_pmtiles_output(self):
        counts = {state: 1 for state in sorted(usmin.TARGET_STATES)}
        stats = {
            'n': 49,
            'states': counts,
            'first_oid': 1,
            'maximum_oid': 49,
            'snapshot_maximum_oid': 49,
            'source_ids': list(range(1, 50)),
        }
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            output = os.path.join(site, 'data', 'tiles', 'national',
                                  'usmin.pmtiles')
            manifest = os.path.join(site, 'data', 'manifest.json')
            os.makedirs(os.path.dirname(output))
            with open(manifest, 'w', encoding='utf-8') as target:
                json.dump({'name': 'test'}, target)

            def fake_tippecanoe(command, check):
                self.assertTrue(check)
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as archive:
                    archive.write(b'PMTiles\x03unit-test')
                self.assertIn('--base-zoom=13', command)
                self.assertIn('--no-feature-limit', command)
                self.assertIn('--no-tile-size-limit', command)
                self.assertNotIn('--drop-densest-as-needed', command)
                self.assertTrue(command[-2] == '-L')
                self.assertTrue(command[-1].startswith('usmin:'))

            with mock.patch.object(usmin, 'SITE', site), \
                    mock.patch.object(usmin, 'OUT', output), \
                    mock.patch.object(usmin, 'MANIFEST', manifest), \
                    mock.patch.object(usmin.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(usmin, '_stream_usmin', return_value=stats), \
                    mock.patch.object(usmin.subprocess, 'run', side_effect=fake_tippecanoe), \
                    mock.patch.object(usmin, '_pmtiles_header', return_value={
                        'bytes': len(b'PMTiles\x03unit-test'),
                        'maxzoom_feature_ids': {'usmin': list(range(1, 50))},
                        'maxzoom_feature_instances': {'usmin': 49},
                    }) as validate:
                result = usmin.build()
            with open(manifest, encoding='utf-8') as source:
                stamped = json.load(source)['national_baselines']['usmin']
        self.assertEqual(stamped['source_layer'], 'usmin')
        self.assertEqual(stamped['n'], 49)
        self.assertEqual(stamped['states'], counts)
        self.assertEqual(stamped['excluded_state_codes'], ['DC', 'HI', 'PR'])
        self.assertEqual(stamped['source_id_inventory']['source_records'], 49)
        self.assertEqual(
            stamped['source_id_inventory']['maxzoom_unique_tiled_ids'], 49)
        self.assertEqual(len(stamped['source_id_inventory']['ids_sha256']), 64)
        self.assertTrue(validate.call_args.kwargs['verify_feature_properties'])
        self.assertTrue(validate.call_args.kwargs['collect_feature_ids'])
        self.assertEqual(result['states'], 49)


if __name__ == '__main__':
    unittest.main()
