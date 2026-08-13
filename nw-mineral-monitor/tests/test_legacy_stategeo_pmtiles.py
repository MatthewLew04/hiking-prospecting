import copy
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

stategeo = importlib.import_module('build_legacy_stategeo_pmtiles')


def sample_columns(state='ID'):
    return {
        'src': 'stategeo',
        'state': state,
        'source': f'{state} geological survey test inventory',
        'retrieved': '2026-08-13',
        'n': 2,
        'id': ['REC-1', 'REC-2'],
        'nm': ['Active Mine', 'Old Prospect'],
        'c': ['gold · silver', ''],
        'ty': ['underground mine', 'prospect'],
        'stx': ['ACTIVE', 'inactive'],
        'g': [0, 5],
        'x': [-114.1, -113.9],
        'y': [44.2, 44.3],
    }


def write_fixture(base, state='ID', columns=None):
    columns = columns or sample_columns(state)
    input_root = os.path.join(base, 'build-inputs')
    site = os.path.join(base, 'site')
    key = f'stategeo_{state.lower()}'
    relative = f'data/sites/{key}.json'
    source_path = os.path.join(input_root, relative)
    os.makedirs(os.path.dirname(source_path), exist_ok=True)
    with open(source_path, 'w', encoding='utf-8') as output:
        json.dump(columns, output, separators=(',', ':'))
    manifest = {
        'schema_version': 1,
        'sites': {
            key: {'n': columns['n'], 'file': relative,
                  'retrieved': columns['retrieved']},
        },
        'claims': {},
        'boundaries': {},
        'totals': {'sites': columns['n'], 'claims_active': 0,
                   'claims_closed': 0, 'boundary_states': 0,
                   'boundary_counties': 0},
    }
    input_manifest = os.path.join(input_root, 'manifest.json')
    with open(input_manifest, 'w', encoding='utf-8') as output:
        json.dump(manifest, output, separators=(',', ':'))
    public_manifest = os.path.join(site, 'data', 'manifest.json')
    os.makedirs(os.path.dirname(public_manifest), exist_ok=True)
    with open(public_manifest, 'w', encoding='utf-8') as output:
        json.dump({'name': 'test', 'sites': {}, 'claims': {}, 'totals': {},
                   'national_baselines': {'keep': {'n': 7}}}, output,
                  separators=(',', ':'))
    return manifest, input_manifest, public_manifest, input_root, site


class LegacyStategeoPmtilesTests(unittest.TestCase):
    def test_stream_emits_compact_browser_properties_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _, _, input_root, _ = write_fixture(directory)
            sequence = os.path.join(directory, 'stategeo.geojsonseq')
            with mock.patch.object(stategeo, 'BUILD_INPUTS', input_root):
                inputs = stategeo._discover_inputs(manifest)
                stats = stategeo._stream_stategeo(sequence, inputs)
            with open(sequence, encoding='utf-8') as source:
                rows = [json.loads(line) for line in source]

        self.assertEqual(stats['n'], 2)
        self.assertEqual(stats['states'], {'ID': 2})
        self.assertEqual(stats['sources']['ID']['manifest_key'], 'stategeo_id')
        self.assertEqual(stats['sources']['ID']['n'], 2)
        self.assertEqual(len(stats['sources']['ID']['sha256']), 64)
        self.assertEqual(rows[0], {
            'type': 'Feature',
            'id': 1,
            'properties': {
                'fid': 1,
                'st': 'ID',
                'nm': 'Active Mine',
                'id': 'REC-1',
                'g': 0,
                'ex': 1,
                'status': 'ACTIVE',
                'commodities': 'gold · silver',
                'typ': 'underground mine',
            },
            'geometry': {'type': 'Point', 'coordinates': [-114.1, 44.2]},
        })
        self.assertEqual(rows[1]['properties']['ex'], 0)
        self.assertNotIn('commodities', rows[1]['properties'])

    def test_canonical_manifest_and_payload_identity_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _, _, input_root, _ = write_fixture(directory)
            malformed = copy.deepcopy(manifest)
            malformed['sites']['stategeo_id']['file'] = 'data/sites/renamed.json'
            with mock.patch.object(stategeo, 'BUILD_INPUTS', input_root):
                with self.assertRaisesRegex(RuntimeError, 'file must be'):
                    stategeo._discover_inputs(malformed)

                inputs = stategeo._discover_inputs(manifest)
                columns = sample_columns('MT')
                with open(inputs[0]['path'], 'w', encoding='utf-8') as output:
                    json.dump(columns, output)
                with self.assertRaisesRegex(RuntimeError, r'\.state must equal'):
                    stategeo._stream_stategeo(
                        os.path.join(directory, 'bad.geojsonseq'), inputs)

    def test_row_counts_ids_groups_and_coordinates_fail_loudly(self):
        mutations = {
            'rows': lambda value: value['x'].pop(),
            'duplicate record id': lambda value: value['id'].__setitem__(1, 'REC-1'),
            'g must be': lambda value: value['g'].__setitem__(0, True),
            'finite world coordinate': lambda value: value['x'].__setitem__(0, float('nan')),
        }
        for message, mutate in mutations.items():
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                columns = sample_columns()
                mutate(columns)
                manifest, _, _, input_root, _ = write_fixture(
                    directory, columns=columns)
                with mock.patch.object(stategeo, 'BUILD_INPUTS', input_root):
                    inputs = stategeo._discover_inputs(manifest)
                    with self.assertRaisesRegex(RuntimeError, message):
                        stategeo._stream_stategeo(
                            os.path.join(directory, 'bad.geojsonseq'), inputs)

    def test_build_publishes_archive_then_atomically_stamps_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            _, input_manifest, manifest_path, input_root, site = write_fixture(directory)
            output = os.path.join(
                site, 'data', 'tiles', 'national', 'stategeo.pmtiles')
            commands = []

            def fake_tippecanoe(command, check):
                self.assertTrue(check)
                commands.append(command)
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as archive:
                    archive.write(b'PMTiles\x03' + b'x' * 160)

            with mock.patch.object(stategeo, 'SITE', site), \
                    mock.patch.object(stategeo, 'MANIFEST', manifest_path), \
                    mock.patch.object(stategeo, 'BUILD_INPUTS', input_root), \
                    mock.patch.object(stategeo, 'BUILD_MANIFEST', input_manifest), \
                    mock.patch.object(stategeo, 'OUT', output), \
                    mock.patch.object(stategeo.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(stategeo.subprocess, 'run', side_effect=fake_tippecanoe):
                result = stategeo.build()

            with open(manifest_path, encoding='utf-8') as source:
                manifest = json.load(source)
            stamped = manifest['national_baselines']['stategeo']
            self.assertEqual(manifest['national_baselines']['keep'], {'n': 7})
            self.assertEqual(stamped['source_layer'], 'stategeo')
            self.assertEqual(stamped['states'], {'ID': 2})
            self.assertEqual(stamped['n'], 2)
            self.assertEqual(stamped['bytes'], os.path.getsize(output))
            self.assertEqual(len(stamped['sha256']), 64)
            self.assertEqual(result['features'], 2)
            self.assertEqual(commands[0][-2], '-L')
            self.assertTrue(commands[0][-1].startswith('stategeo:'))
            self.assertFalse(any(name.endswith('.geojson') or
                                 name.endswith('.geojsonseq')
                                 for _, _, names in os.walk(site)
                                 for name in names))

    def test_input_change_during_build_aborts_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            _, input_manifest, manifest_path, input_root, site = write_fixture(directory)
            source_path = os.path.join(
                input_root, 'data', 'sites', 'stategeo_id.json')
            output = os.path.join(
                site, 'data', 'tiles', 'national', 'stategeo.pmtiles')
            with open(manifest_path, 'rb') as source:
                manifest_before = source.read()

            def fake_tippecanoe(command, check):
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as archive:
                    archive.write(b'PMTiles\x03' + b'x' * 160)
                with open(source_path, 'a', encoding='utf-8') as changed:
                    changed.write(' ')

            with mock.patch.object(stategeo, 'SITE', site), \
                    mock.patch.object(stategeo, 'MANIFEST', manifest_path), \
                    mock.patch.object(stategeo, 'BUILD_INPUTS', input_root), \
                    mock.patch.object(stategeo, 'BUILD_MANIFEST', input_manifest), \
                    mock.patch.object(stategeo, 'OUT', output), \
                    mock.patch.object(stategeo.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(stategeo.subprocess, 'run', side_effect=fake_tippecanoe):
                with self.assertRaisesRegex(RuntimeError, 'changed during'):
                    stategeo.build()

            self.assertFalse(os.path.exists(output))
            with open(manifest_path, 'rb') as source:
                self.assertEqual(source.read(), manifest_before)

    def test_invalid_pmtiles_preserves_prior_archive_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            _, input_manifest, manifest_path, input_root, site = write_fixture(directory)
            output = os.path.join(
                site, 'data', 'tiles', 'national', 'stategeo.pmtiles')
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(output, 'wb') as archive:
                archive.write(b'previous-good-archive')
            with open(manifest_path, 'rb') as source:
                manifest_before = source.read()

            def fake_tippecanoe(command, check):
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as archive:
                    archive.write(b'not-pmtiles')

            with mock.patch.object(stategeo, 'SITE', site), \
                    mock.patch.object(stategeo, 'MANIFEST', manifest_path), \
                    mock.patch.object(stategeo, 'BUILD_INPUTS', input_root), \
                    mock.patch.object(stategeo, 'BUILD_MANIFEST', input_manifest), \
                    mock.patch.object(stategeo, 'OUT', output), \
                    mock.patch.object(stategeo.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(stategeo.subprocess, 'run', side_effect=fake_tippecanoe):
                with self.assertRaisesRegex(RuntimeError, 'valid PMTiles'):
                    stategeo.build()

            with open(output, 'rb') as archive:
                self.assertEqual(archive.read(), b'previous-good-archive')
            with open(manifest_path, 'rb') as source:
                self.assertEqual(source.read(), manifest_before)


if __name__ == '__main__':
    unittest.main()
