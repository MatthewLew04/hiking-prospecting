import copy
import hashlib
import importlib
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

claims = importlib.import_module('build_legacy_claims_pmtiles')


def snapshot(state, mode, serials, *, partial=False, optionals=True):
    n = len(serials)
    data = {
        'state': state,
        'layer': mode,
        'retrieved': '2026-08-13',
        'n': n,
        'serial': list(serials),
        'name': [f'{mode.title()} {index}' for index in range(n)],
        'type': ['L'] * n,
        'x': [-116.0 - index / 10 for index in range(n)],
        'y': [43.0 + index / 10 for index in range(n)],
    }
    if optionals:
        data['disp'] = ['FILED'] * n
        data['acres'] = [20.66] * n
    if partial:
        data.update({
            'truncated': True,
            'total_available': n + 10,
            'partial_after_spatial_clip': True,
        })
    return data


def write_snapshot(input_root, key, data, entry_extra=None):
    directory = os.path.join(input_root, 'data', 'claims')
    os.makedirs(directory, exist_ok=True)
    relative = f'data/claims/{key}.json'
    with open(os.path.join(input_root, relative), 'w', encoding='utf-8') as output:
        json.dump(data, output, separators=(',', ':'))
    entry = {'file': relative, 'n': data['n'], 'retrieved': data['retrieved']}
    entry.update(entry_extra or {})
    return entry


def write_valid_pmtiles(path, layers=('active', 'closed')):
    metadata = json.dumps({
        'vector_layers': [{'id': layer, 'fields': {}} for layer in layers]
    }, separators=(',', ':')).encode()
    root = b'\x01'
    tile = b'not-an-empty-vector-tile'
    root_offset = 127
    metadata_offset = root_offset + len(root)
    tile_offset = metadata_offset + len(metadata)
    header = bytearray(127)
    header[:8] = b'PMTiles\x03'
    struct.pack_into(
        '<11Q', header, 8,
        root_offset, len(root),
        metadata_offset, len(metadata),
        0, 0,
        tile_offset, len(tile),
        1, 1, 1,
    )
    header[96] = 1       # clustered
    header[97] = 1       # uncompressed internal directories/metadata
    header[98] = 1       # uncompressed tile data
    header[99] = 1       # vector MVT
    header[100] = 0
    header[101] = 13
    with open(path, 'wb') as output:
        output.write(header)
        output.write(root)
        output.write(metadata)
        output.write(tile)


class LegacyClaimsPmtilesTests(unittest.TestCase):
    def make_manifest(self, input_root, *, partial_closed=True):
        active = snapshot('ID', 'active', ['ID100', 'ID101'])
        closed = snapshot(
            'NV', 'closed', ['NV200'], partial=partial_closed, optionals=False)
        entries = {
            'id_active': write_snapshot(input_root, 'id_active', active),
            'nv_closed': write_snapshot(input_root, 'nv_closed', closed),
        }
        return {'name': 'test', 'claims': entries, 'national_baselines': {}}

    def write_private_manifest(self, input_root, stream_manifest):
        private = {
            'schema_version': 1,
            'sites': {},
            'claims': stream_manifest['claims'],
            'boundaries': {},
            'totals': {
                'sites': 0,
                'claims_active': sum(
                    entry['n'] for key, entry in stream_manifest['claims'].items()
                    if key.endswith('_active')),
                'claims_closed': sum(
                    entry['n'] for key, entry in stream_manifest['claims'].items()
                    if key.endswith('_closed')),
                'boundary_states': 0,
                'boundary_counties': 0,
            },
        }
        path = os.path.join(input_root, 'manifest.json')
        with open(path, 'w', encoding='utf-8') as output:
            json.dump(private, output, separators=(',', ':'))
        return private, path

    def test_streams_separate_layers_with_stable_safe_ids_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = os.path.join(directory, 'build-inputs')
            manifest = self.make_manifest(input_root)
            paths = {
                'active': os.path.join(directory, 'active.geojsonseq'),
                'closed': os.path.join(directory, 'closed.geojsonseq'),
            }
            with mock.patch.object(claims, 'BUILD_INPUTS', input_root):
                stats = claims._stream_claims(manifest, paths)
            with open(paths['active'], encoding='utf-8') as source:
                active = [json.loads(line) for line in source]
            with open(paths['closed'], encoding='utf-8') as source:
                closed = [json.loads(line) for line in source]

        self.assertEqual([row['properties']['serial'] for row in active],
                         ['ID100', 'ID101'])
        self.assertEqual(closed[0]['properties'], {
            'st': 'NV', 'serial': 'NV200', 'nm': 'Closed 0',
            'type': 'L', 'partial': 1,
        })
        self.assertEqual(set(active[0]['properties']),
                         {'st', 'serial', 'nm', 'type', 'disp', 'acres'})
        identifiers = [row['id'] for row in active + closed]
        self.assertEqual(len(set(identifiers)), 3)
        self.assertTrue(all(0 < value <= claims.SAFE_INTEGER_MAX
                            for value in identifiers))
        self.assertEqual(active[0]['id'], claims._feature_id('ID', 'active', 'ID100'))
        self.assertNotEqual(active[0]['id'], claims._feature_id('ID', 'closed', 'ID100'))
        self.assertEqual(stats, {
            'n': 3,
            'states': {'ID': 2, 'NV': 1},
            'by_mode': {
                'active': {'n': 2, 'states': {'ID': 2}},
                'closed': {'n': 1, 'states': {'NV': 1}},
            },
            'snapshots': 2,
            'partial_states': ['NV'],
            'partial_snapshots': ['nv_closed'],
        })

    def test_rejects_identity_alignment_duplicate_and_coordinate_failures(self):
        mutations = [
            ('state identity', lambda data: data.update(state='NV')),
            ('layer identity', lambda data: data.update(layer='closed')),
            ('rows; expected', lambda data: data['name'].pop()),
            ('duplicate serial', lambda data: data['serial'].__setitem__(1, 'ID100')),
            ('finite number', lambda data: data['x'].__setitem__(0, float('nan'))),
            ('outside', lambda data: data['y'].__setitem__(0, 91)),
        ]
        for expected_error, mutate in mutations:
            with self.subTest(expected_error=expected_error), \
                    tempfile.TemporaryDirectory() as directory:
                input_root = os.path.join(directory, 'build-inputs')
                manifest = self.make_manifest(input_root, partial_closed=False)
                active_path = os.path.join(input_root, 'data', 'claims', 'id_active.json')
                with open(active_path, encoding='utf-8') as source:
                    active = json.load(source)
                mutate(active)
                with open(active_path, 'w', encoding='utf-8') as output:
                    json.dump(active, output, allow_nan=True)
                paths = {
                    'active': os.path.join(directory, 'active.seq'),
                    'closed': os.path.join(directory, 'closed.seq'),
                }
                with mock.patch.object(claims, 'BUILD_INPUTS', input_root):
                    with self.assertRaisesRegex(ValueError, expected_error):
                        claims._stream_claims(manifest, paths)

    def test_rejects_stale_manifest_count_and_noncanonical_file_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = os.path.join(directory, 'build-inputs')
            manifest = self.make_manifest(input_root)
            paths = {'active': os.path.join(directory, 'a.seq'),
                     'closed': os.path.join(directory, 'c.seq')}
            stale = copy.deepcopy(manifest)
            stale['claims']['id_active']['n'] = 99
            with mock.patch.object(claims, 'BUILD_INPUTS', input_root):
                with self.assertRaisesRegex(ValueError, 'does not match artifact'):
                    claims._stream_claims(stale, paths)
            wrong_file = copy.deepcopy(manifest)
            wrong_file['claims']['id_active']['file'] = 'data/claims/nv_closed.json'
            with mock.patch.object(claims, 'BUILD_INPUTS', input_root):
                with self.assertRaisesRegex(ValueError, 'file must be'):
                    claims._stream_claims(wrong_file, paths)

    def test_pmtiles_validation_requires_v3_nonempty_vector_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.pmtiles')
            write_valid_pmtiles(valid)
            metadata = claims._validate_pmtiles(valid)
            self.assertEqual(metadata['version'], 3)
            self.assertEqual(metadata['source_layers'], ['active', 'closed'])
            self.assertEqual(metadata['tile_entries'], 1)

            missing_layer = os.path.join(directory, 'missing.pmtiles')
            write_valid_pmtiles(missing_layer, ('active',))
            with self.assertRaisesRegex(ValueError, 'do not contain'):
                claims._validate_pmtiles(missing_layer)

            truncated = os.path.join(directory, 'truncated.pmtiles')
            with open(truncated, 'wb') as output:
                output.write(b'PMTiles\x03')
            with self.assertRaisesRegex(ValueError, 'not a PMTiles v3'):
                claims._validate_pmtiles(truncated)

    def test_build_publishes_archive_and_atomically_stamps_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            input_root = os.path.join(directory, 'build-inputs')
            stream_manifest = self.make_manifest(input_root)
            private_data, private_manifest = self.write_private_manifest(
                input_root, stream_manifest)
            manifest_data = {
                'name': 'test', 'sites': {}, 'claims': {}, 'totals': {},
                'national_baselines': {},
            }
            manifest = os.path.join(site, 'data', 'manifest.json')
            output = os.path.join(site, 'data', 'tiles', 'national',
                                  'claims.pmtiles')
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(manifest, 'w', encoding='utf-8') as target:
                json.dump(manifest_data, target, separators=(',', ':'))
            os.chmod(manifest, 0o640)
            commands = []

            def fake_tippecanoe(command, check):
                self.assertTrue(check)
                commands.append(command)
                pending = command[command.index('--output') + 1]
                write_valid_pmtiles(pending)

            with mock.patch.object(claims, 'SITE', site), \
                    mock.patch.object(claims, 'MANIFEST', manifest), \
                    mock.patch.object(claims, 'BUILD_INPUTS', input_root), \
                    mock.patch.object(claims, 'BUILD_MANIFEST', private_manifest), \
                    mock.patch.object(claims, 'OUT', output), \
                    mock.patch.object(claims.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(claims.subprocess, 'run', side_effect=fake_tippecanoe):
                result = claims.build()

            with open(manifest, encoding='utf-8') as source:
                stamped_manifest = json.load(source)
            stamped = stamped_manifest['national_baselines']['claims']
            self.assertEqual(stamped['source_layers'], ['active', 'closed'])
            self.assertEqual(stamped['n'], 3)
            self.assertEqual(stamped['states'], {'ID': 2, 'NV': 1})
            self.assertEqual(stamped['by_mode']['active']['states'], {'ID': 2})
            self.assertEqual(stamped['by_mode']['closed']['states'], {'NV': 1})
            self.assertEqual(stamped['partial_states'], ['NV'])
            self.assertEqual(stamped['partial_snapshots'], ['nv_closed'])
            self.assertEqual(stamped['bytes'], os.path.getsize(output))
            with open(output, 'rb') as source:
                output_sha256 = hashlib.sha256(source.read()).hexdigest()
            self.assertEqual(stamped['sha256'], output_sha256)
            self.assertEqual(stamped_manifest['claims'], {})
            with open(private_manifest, encoding='utf-8') as source:
                self.assertEqual(json.load(source), private_data)
            self.assertEqual(stat.S_IMODE(os.stat(manifest).st_mode), 0o640)
            self.assertEqual(result['features'], 3)
            layer_args = [commands[0][index + 1]
                          for index, value in enumerate(commands[0]) if value == '-L']
            self.assertEqual([value.split(':', 1)[0] for value in layer_args],
                             ['active', 'closed'])
            self.assertTrue(all(value.endswith('.geojsonseq') for value in layer_args))
            self.assertEqual(
                [name for root, _, files in os.walk(site) for name in files
                 if name.endswith(('.geojson', '.geojsonseq'))], [])

    def test_invalid_tippecanoe_output_never_replaces_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            input_root = os.path.join(directory, 'build-inputs')
            stream_manifest = self.make_manifest(input_root)
            _, private_manifest = self.write_private_manifest(
                input_root, stream_manifest)
            manifest_data = {
                'name': 'test', 'sites': {}, 'claims': {}, 'totals': {},
                'national_baselines': {'claims': {'sentinel': True}},
            }
            manifest = os.path.join(site, 'data', 'manifest.json')
            output = os.path.join(site, 'data', 'tiles', 'national',
                                  'claims.pmtiles')
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(manifest, 'w', encoding='utf-8') as target:
                json.dump(manifest_data, target, separators=(',', ':'))
            with open(output, 'wb') as target:
                target.write(b'existing-archive')
            with open(manifest, 'rb') as source:
                original_manifest = source.read()
            with open(output, 'rb') as source:
                original_archive = source.read()

            def fake_tippecanoe(command, check):
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as target:
                    target.write(b'PMTiles\x03too-short')

            with mock.patch.object(claims, 'SITE', site), \
                    mock.patch.object(claims, 'MANIFEST', manifest), \
                    mock.patch.object(claims, 'BUILD_INPUTS', input_root), \
                    mock.patch.object(claims, 'BUILD_MANIFEST', private_manifest), \
                    mock.patch.object(claims, 'OUT', output), \
                    mock.patch.object(claims.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(claims.subprocess, 'run', side_effect=fake_tippecanoe):
                with self.assertRaisesRegex(ValueError, 'not a PMTiles v3'):
                    claims.build()
            with open(manifest, 'rb') as source:
                self.assertEqual(source.read(), original_manifest)
            with open(output, 'rb') as source:
                self.assertEqual(source.read(), original_archive)


if __name__ == '__main__':
    unittest.main()
