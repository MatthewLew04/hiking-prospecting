import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

mrds = importlib.import_module('build_national_mrds_pmtiles')


def normalized_feature(identifier, state):
    return state, {
        'type': 'Feature',
        'id': identifier,
        'properties': {
            'id': identifier,
            'nm': f'Occurrence {identifier}',
            'st': state,
            'status': 'OC',
            'ex': 0,
            'g': 5,
        },
        'geometry': {'type': 'Point', 'coordinates': [-110.0, 40.0]},
    }


def mrds_zip():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('mrds.csv', 'country,state,latitude,longitude,dep_id\n')
    return output.getvalue()


class NationalMrdsTests(unittest.TestCase):
    def test_top_level_and_property_ids_are_the_same_stable_dep_id(self):
        payload = (
            'country,state,latitude,longitude,dep_id,site_name,dev_stat\n'
            'United States,Nevada,40,-117,27,Example,Occurrence\n'
        ).encode()
        rows = list(mrds.iter_features(io.BytesIO(payload)))
        self.assertEqual(len(rows), 1)
        _, feature = rows[0]
        self.assertEqual(feature['id'], 27)
        self.assertEqual(feature['properties']['id'], 27)

    def test_writer_and_inventory_reject_duplicate_or_missing_ids(self):
        counts = {state: 0 for state in sorted(mrds.ALL_STATES)}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'mrds.geojson')
            with self.assertRaisesRegex(RuntimeError, 'duplicates dep_id'):
                mrds._write_feature_collection(
                    path,
                    iter((normalized_feature(7, 'AK'),
                          normalized_feature(7, 'AZ'))),
                    counts, set())
        with self.assertRaisesRegex(RuntimeError, 'reconciliation failed'):
            mrds._source_id_inventory({1, 2}, [1], 1)
        inventory = mrds._source_id_inventory({1, 2}, [1, 2], 3)
        self.assertEqual(inventory['maxzoom_feature_instances'], 3)

    def test_build_is_lossless_and_stamps_canonical_id_inventory(self):
        ordered_states = sorted(mrds.ALL_STATES)
        rows = [normalized_feature(index, state)
                for index, state in enumerate(ordered_states, 1)]
        expected_ids = list(range(1, 50))
        commands = []
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            output = os.path.join(site, 'data', 'tiles', 'national', 'mrds.pmtiles')
            manifest = os.path.join(site, 'data', 'manifest.json')
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(manifest, 'w', encoding='utf-8') as target:
                json.dump({'name': 'test'}, target)

            def fake_tippecanoe(command, check):
                self.assertTrue(check)
                commands.append(command)
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as archive:
                    archive.write(b'PMTiles\x03unit-test')

            with mock.patch.object(mrds, 'SITE', site), \
                    mock.patch.object(mrds, 'OUT', output), \
                    mock.patch.object(mrds, 'MANIFEST', manifest), \
                    mock.patch.object(mrds.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(mrds, 'cached_get', return_value=mrds_zip()), \
                    mock.patch.object(mrds, 'iter_features', return_value=iter(rows)), \
                    mock.patch.object(mrds.subprocess, 'run', side_effect=fake_tippecanoe), \
                    mock.patch.object(mrds, '_pmtiles_header', return_value={
                        'bytes': len(b'PMTiles\x03unit-test'),
                        'maxzoom_feature_ids': {'mrds': expected_ids},
                        'maxzoom_feature_instances': {'mrds': 49},
                    }) as validate:
                result = mrds.build()

            with open(manifest, encoding='utf-8') as source:
                entry = json.load(source)['national_baselines']['mrds']

        command = commands[0]
        self.assertIn('--base-zoom=12', command)
        self.assertIn('--no-feature-limit', command)
        self.assertIn('--no-tile-size-limit', command)
        self.assertNotIn('--drop-densest-as-needed', command)
        self.assertEqual(entry['n'], 49)
        self.assertEqual(set(entry['states']), mrds.ALL_STATES)
        self.assertTrue(all(value == 1 for value in entry['states'].values()))
        self.assertEqual(entry['source_id_inventory']['source_records'], 49)
        self.assertEqual(entry['source_id_inventory']['maxzoom_unique_tiled_ids'], 49)
        self.assertEqual(len(entry['source_id_inventory']['ids_sha256']), 64)
        self.assertEqual(result['features'], 49)
        self.assertTrue(validate.call_args.kwargs['verify_feature_properties'])
        self.assertTrue(validate.call_args.kwargs['collect_feature_ids'])


if __name__ == '__main__':
    unittest.main()
