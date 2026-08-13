import copy
import hashlib
import importlib
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

mlrs = importlib.import_module('build_federal_mlrs_pmtiles')


def _snapshot(code, mode, rows=()):
    clip_sha = mlrs._sha256_file(mlrs.DEFAULT_STATE_CLIPS)
    envelope_count = len(mlrs.load_states()[code]['query_envelopes'])
    data = {
        'state': code,
        'layer': mode,
        'retrieved': '2026-08-13',
        'n': len(rows),
        'serial': [],
        'name': [],
        'type': [],
        'x': [],
        'y': [],
        'admin_state': [],
        'geo_state': [],
        'source': mlrs.SOURCE,
        'spatial_clip': {
            'method': mlrs.CLIP_METHOD,
            'artifact_sha256': clip_sha,
            'version': f'state-centroid-{clip_sha[:16]}',
        },
        'pagination': {
            'schema_version': 1,
            'method': mlrs.PAGINATION_METHOD,
            'order': mlrs.PAGINATION_ORDER[mode],
            'page_size': mlrs.PAGINATION_PAGE_SIZE,
            'pages': 1 if rows else 0,
            'envelopes': envelope_count,
            'completed_envelopes': envelope_count,
            'terminal_empty_pages': envelope_count,
            'complete': True,
        },
    }
    if mode == 'active':
        data['disp'] = []
        data['acres'] = []
    for serial, longitude, latitude in rows:
        data['serial'].append(serial)
        data['name'].append(f'{code} {mode} {serial}')
        data['type'].append('L')
        data['x'].append(longitude)
        data['y'].append(latitude)
        data['admin_state'].append(code)
        data['geo_state'].append(code)
        if mode == 'active':
            data['disp'].append('AUTHORIZED')
            data['acres'].append(20.66)
    return data


def _write_json(path, value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    with open(path, 'wb') as output:
        output.write(raw)
    return raw


def _valid_pmtiles(path, layers=('active', 'closed')):
    metadata = json.dumps({
        'vector_layers': [
            {'id': layer,
             'fields': {'system': 'String', 'st': 'String',
                        'serial': 'String', 'type': 'String',
                        'status': 'String'}}
            for layer in layers
        ]
    }, separators=(',', ':')).encode()
    root = b'\x01'
    tile = b'checked-mvt-payload'
    root_offset = 127
    metadata_offset = root_offset + len(root)
    tile_offset = metadata_offset + len(metadata)
    header = bytearray(127)
    header[:8] = b'PMTiles\x03'
    struct.pack_into(
        '<11Q', header, 8,
        root_offset, len(root), metadata_offset, len(metadata),
        0, 0, tile_offset, len(tile), 1, 1, 1)
    header[96] = 1
    header[97] = 1
    header[98] = 1
    header[99] = 1
    header[100] = 0
    header[101] = 13
    with open(path, 'wb') as output:
        output.write(header)
        output.write(root)
        output.write(metadata)
        output.write(tile)


class Fixture:
    def __init__(self, directory):
        self.base = directory
        self.staging = os.path.join(directory, 'private-staging')
        self.inventory_path = os.path.join(self.staging, 'inventory.json')
        self.publish = os.path.join(directory, 'publish')
        self.latest = os.path.join(self.publish, 'latest.json')
        os.makedirs(self.staging)
        self.snapshots = {}
        states = {}
        for code in sorted(mlrs.CLAIM_STATES):
            states[code] = {}
            for mode in mlrs.MODES:
                rows = ()
                if (code, mode) == ('AK', 'active'):
                    rows = (('AK000001', -150.0, 64.0),)
                elif (code, mode) == ('AZ', 'closed'):
                    rows = (('AZ000002', -112.0, 34.0),)
                data = _snapshot(code, mode, rows)
                self.snapshots[(code, mode)] = data
                filename = f'{code.lower()}_{mode}.json'
                raw = _write_json(os.path.join(self.staging, filename), data)
                states[code][mode] = {
                    'file': filename,
                    'n': data['n'],
                    'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest(),
                    'retrieved': data['retrieved'],
                    'complete': True,
                }
        clip_sha = mlrs._sha256_file(mlrs.DEFAULT_STATE_CLIPS)
        self.inventory = {
            'schema_version': 1,
            'system': mlrs.SYSTEM,
            'source': mlrs.SOURCE,
            'created': '2026-08-13',
            'clip': {
                'authority': mlrs.CLIP_AUTHORITY,
                'method': mlrs.CLIP_METHOD,
                'artifact_sha256': clip_sha,
            },
            'states': states,
        }
        self.save_inventory()

    def save_inventory(self):
        _write_json(self.inventory_path, self.inventory)

    def rewrite_snapshot(self, code, mode, mutate):
        data = copy.deepcopy(self.snapshots[(code, mode)])
        mutate(data)
        self.snapshots[(code, mode)] = data
        entry = self.inventory['states'][code][mode]
        path = os.path.join(self.staging, entry['file'])
        raw = _write_json(path, data)
        entry['n'] = data.get('n')
        entry['bytes'] = len(raw)
        entry['sha256'] = hashlib.sha256(raw).hexdigest()
        entry['retrieved'] = data.get('retrieved')
        self.save_inventory()

    def load(self):
        return mlrs.load_inventory(
            self.staging, self.inventory_path, mlrs.DEFAULT_STATE_CLIPS)

    def stream(self, context=None, profile='release'):
        context = context or self.load()
        active = os.path.join(self.base, 'active.geojsonseq')
        closed = os.path.join(self.base, 'closed.geojsonseq')
        stats = mlrs.stream_snapshots(
            context, {'active': active, 'closed': closed}, profile=profile)
        return stats, active, closed

    def semantic_metadata(self):
        ids = {mode: [] for mode in mlrs.MODES}
        for (code, mode), snapshot in self.snapshots.items():
            ids[mode].extend(
                mlrs._feature_id(code, mode, serial)
                for serial in snapshot['serial'])
        return {
            'maxzoom_feature_ids': {
                mode: sorted(values) for mode, values in ids.items()
            },
            'maxzoom_feature_instances': {
                mode: len(values) for mode, values in ids.items()
            },
        }


class FederalMlrsPublicationTests(unittest.TestCase):
    def test_exact_registry_19_and_zero_row_states_remain_declared_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            stats, active_path, closed_path = fixture.stream(context)
            with open(active_path, encoding='utf-8') as source:
                active = [json.loads(line) for line in source]
            with open(closed_path, encoding='utf-8') as source:
                closed = [json.loads(line) for line in source]

        self.assertEqual(len(context['codes']), 19)
        self.assertEqual(set(context['codes']), set(mlrs.CLAIM_STATES))
        self.assertEqual(set(stats['states']), set(mlrs.CLAIM_STATES))
        self.assertEqual(stats['by_mode']['active']['states']['AR'], 0)
        self.assertEqual(stats['by_mode']['closed']['states']['FL'], 0)
        self.assertIn('AR', stats['zero_states']['active'])
        self.assertEqual(stats['n'], 2)
        self.assertEqual(active[0]['id'], active[0]['properties']['fid'])
        self.assertEqual(active[0]['properties']['system'], 'federal_mlrs')
        self.assertEqual(active[0]['properties']['status'], 'AUTHORIZED')
        self.assertNotIn('partial', active[0]['properties'])
        self.assertEqual(closed[0]['properties']['st'], 'AZ')
        self.assertEqual(closed[0]['properties']['status'], 'CLOSED')

    def test_missing_extra_state_and_bad_clip_provenance_fail_closed(self):
        mutations = [
            lambda value: value['states'].pop('AR'),
            lambda value: value['states'].__setitem__('NY', copy.deepcopy(
                value['states']['AR'])),
            lambda value: value['clip'].__setitem__('artifact_sha256', '0' * 64),
            lambda value: value['clip'].__setitem__('method', 'bbox only'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                mutate(fixture.inventory)
                fixture.save_inventory()
                with self.assertRaises(mlrs.PublicationError):
                    fixture.load()

    def test_state_layer_ragged_duplicate_and_outside_rows_fail(self):
        cases = {
            'identity': lambda value: value.update(state='AZ'),
            'layer': lambda value: value.update(layer='closed'),
            'rows': lambda value: value['name'].pop(),
            'duplicates serial': lambda value: [
                value[column].append(value[column][0])
                for column in mlrs.REQUIRED_COLUMNS['active']
            ] and value.update(n=2),
            'outside': lambda value: value['x'].__setitem__(0, 0.0),
        }
        for message, mutate in cases.items():
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.rewrite_snapshot('AK', 'active', mutate)
                with self.assertRaisesRegex(mlrs.PublicationError, message):
                    fixture.stream()

    def test_same_serial_in_active_and_closed_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite_snapshot(
                'AK', 'closed',
                lambda value: value.update(**_snapshot(
                    'AK', 'closed', (('AK000001', -150.0, 64.0),))))
            with self.assertRaisesRegex(mlrs.PublicationError, 'both active and closed'):
                fixture.stream()

    def test_release_and_full_reject_partial_while_progress_labels_it(self):
        for profile in ('release', 'full'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                entry = fixture.inventory['states']['AK']['active']
                entry['complete'] = False
                entry['partial_reason'] = 'upstream pagination checkpoint incomplete'
                fixture.save_inventory()
                with self.assertRaisesRegex(
                        mlrs.PublicationError, 'partial/capped'):
                    fixture.stream(profile=profile)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            entry = fixture.inventory['states']['AK']['active']
            entry['complete'] = False
            entry['partial_reason'] = 'upstream pagination checkpoint incomplete'
            fixture.save_inventory()
            stats, active_path, _ = fixture.stream(profile='progress')
            with open(active_path, encoding='utf-8') as source:
                active = json.loads(source.readline())
        self.assertEqual(stats['partial_states'], ['AK'])
        self.assertEqual(active['properties']['partial'], 1)

    def test_truncated_snapshot_is_rejected_even_if_inventory_says_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite_snapshot(
                'AZ', 'closed',
                lambda value: value.update(truncated=True, total_available=2))
            with self.assertRaisesRegex(mlrs.PublicationError, 'partial/capped'):
                fixture.stream(profile='release')

    def test_release_requires_machine_attested_pagination_exhaustion(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite_snapshot(
                'AZ', 'active', lambda value: [value.pop(field) for field in (
                    'pagination', 'spatial_clip', 'source')])
            with self.assertRaisesRegex(mlrs.PublicationError, 'partial/capped'):
                fixture.stream(profile='release')
            stats, active_path, _ = fixture.stream(profile='progress')
            with open(active_path, encoding='utf-8') as source:
                rows = [json.loads(line) for line in source if line.strip()]
            self.assertIn('AZ', stats['partial_states'])
            # AZ is a true zero in this fixture, so progress has no fabricated
            # feature to label; the partial state inventory remains explicit.
            self.assertFalse(any(row['properties']['st'] == 'AZ' for row in rows))

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite_snapshot(
                'AK', 'active', lambda value: value['pagination'].update(
                    completed_envelopes=1, terminal_empty_pages=1))
            with self.assertRaisesRegex(
                    mlrs.PublicationError, 'without exhausting every envelope'):
                fixture.stream(profile='release')

    def test_stable_ids_are_status_scoped_and_javascript_safe(self):
        value = mlrs._feature_id('NV', 'active', 'NMC123')
        self.assertEqual(value, mlrs._feature_id('NV', 'active', 'NMC123'))
        self.assertNotEqual(value, mlrs._feature_id('NV', 'closed', 'NMC123'))
        self.assertNotEqual(value, mlrs._feature_id('UT', 'active', 'NMC123'))
        self.assertTrue(0 < value <= mlrs.SAFE_INTEGER_MAX)

    def test_inventory_and_snapshot_hash_mutations_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            stats, _, _ = fixture.stream(context)
            with open(fixture.inventory_path, 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(mlrs.PublicationError, 'inventory changed'):
                mlrs.assert_inputs_unchanged(context, stats)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            stats, _, _ = fixture.stream(context)
            path = context['paths'][('AK', 'active')]
            with open(path, 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(mlrs.PublicationError, 'changed during'):
                mlrs.assert_inputs_unchanged(context, stats)

    def test_pmtiles_validation_requires_exact_layers_and_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.pmtiles')
            _valid_pmtiles(valid)
            self.assertEqual(mlrs.validate_pmtiles(valid)['source_layers'],
                             ['active', 'closed'])
            missing = os.path.join(directory, 'missing.pmtiles')
            _valid_pmtiles(missing, ('active',))
            with self.assertRaisesRegex(mlrs.PublicationError, 'exactly'):
                mlrs.validate_pmtiles(missing)

    def test_pmtiles_validation_rejects_any_maxzoom_source_id_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.pmtiles')
            _valid_pmtiles(valid)
            expected = {'active': [11, 12], 'closed': [21]}
            observed = {
                'maxzoom_feature_ids': {'active': [11], 'closed': [21]},
                'maxzoom_feature_instances': {'active': 1, 'closed': 1},
            }
            with mock.patch.object(mlrs, '_pmtiles_header', return_value=observed):
                with self.assertRaisesRegex(
                        mlrs.PublicationError, 'source-ID reconciliation failed'):
                    mlrs.validate_pmtiles(valid, expected)

            observed = {
                'maxzoom_feature_ids': {'active': [11, 12], 'closed': [21]},
                'maxzoom_feature_instances': {'active': 3, 'closed': 1},
            }
            with mock.patch.object(mlrs, '_pmtiles_header', return_value=observed):
                inventory = mlrs.validate_pmtiles(
                    valid, expected)['source_id_inventory']
            self.assertEqual(
                inventory['by_layer']['active']['maxzoom_feature_instances'], 3)

    def test_build_installs_immutable_archive_and_merges_latest_state(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            os.makedirs(fixture.publish)
            _write_json(fixture.latest, {
                'schema_version': 1,
                'artifacts': {'other': {'n': 7}},
                'operator_note': 'preserve me',
            })
            commands = []

            def fake_tippecanoe(command, check):
                self.assertTrue(check)
                commands.append(command)
                output = command[command.index('--output') + 1]
                _valid_pmtiles(output)
                self.assertIn('--base-zoom=13', command)
                self.assertIn('--no-feature-limit', command)
                self.assertIn('--no-tile-size-limit', command)
                self.assertNotIn('--drop-densest-as-needed', command)
                self.assertNotIn('--extend-zooms-if-still-dropping', command)

            with mock.patch.object(mlrs.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(mlrs.subprocess, 'run', side_effect=fake_tippecanoe), \
                    mock.patch.object(mlrs, '_pmtiles_header',
                                      return_value=fixture.semantic_metadata()):
                result = mlrs.build(
                    fixture.staging, fixture.inventory_path, fixture.publish,
                    latest_manifest=fixture.latest,
                    state_clips=mlrs.DEFAULT_STATE_CLIPS)

            with open(fixture.latest, encoding='utf-8') as source:
                latest = json.load(source)
            entry = latest['artifacts']['federal_mlrs']
            self.assertEqual(latest['artifacts']['other'], {'n': 7})
            self.assertEqual(latest['operator_note'], 'preserve me')
            self.assertEqual(entry['n'], 2)
            self.assertEqual(len(entry['states']), 19)
            self.assertEqual(entry['states']['AR'], 0)
            self.assertEqual(entry['source_id_inventory']['source_records'], 2)
            self.assertEqual(
                entry['source_id_inventory']['maxzoom_unique_tiled_ids'], 2)
            self.assertEqual(
                entry['source_id_inventory']['by_layer']['active']['source_records'], 1)
            self.assertEqual(len(entry['source_id_inventory']['ids_sha256']), 64)
            self.assertEqual(entry['sha256'], mlrs._sha256_file(result['artifact']))
            self.assertRegex(os.path.basename(result['artifact']),
                             r'^federal-mlrs-[0-9a-f]{20}\.pmtiles$')
            self.assertFalse(any(name.endswith(('.json', '.geojsonseq'))
                                 for name in os.listdir(fixture.publish)
                                 if name != 'latest.json'))
            layers = [commands[0][index + 1]
                      for index, value in enumerate(commands[0]) if value == '-L']
            self.assertEqual([value.split(':', 1)[0] for value in layers],
                             ['active', 'closed'])

    def test_latest_merge_reads_newest_manifest_after_tiling(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            os.makedirs(fixture.publish)
            _write_json(fixture.latest, {'schema_version': 1, 'artifacts': {}})

            def fake_tippecanoe(command, check):
                _write_json(fixture.latest, {
                    'schema_version': 1,
                    'artifacts': {'concurrent': {'generation': 2}},
                })
                _valid_pmtiles(command[command.index('--output') + 1])

            with mock.patch.object(mlrs.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(mlrs.subprocess, 'run', side_effect=fake_tippecanoe), \
                    mock.patch.object(mlrs, '_pmtiles_header',
                                      return_value=fixture.semantic_metadata()):
                mlrs.build(
                    fixture.staging, fixture.inventory_path, fixture.publish,
                    latest_manifest=fixture.latest)
            with open(fixture.latest, encoding='utf-8') as source:
                latest = json.load(source)
        self.assertEqual(latest['artifacts']['concurrent'], {'generation': 2})
        self.assertIn('federal_mlrs', latest['artifacts'])

    def test_invalid_archive_or_input_change_never_updates_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            os.makedirs(fixture.publish)
            original = _write_json(
                fixture.latest,
                {'schema_version': 1, 'artifacts': {'sentinel': True}})

            def invalid_tippecanoe(command, check):
                with open(command[command.index('--output') + 1], 'wb') as output:
                    output.write(b'PMTiles\x03short')

            with mock.patch.object(mlrs.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(mlrs.subprocess, 'run', side_effect=invalid_tippecanoe):
                with self.assertRaises(mlrs.PublicationError):
                    mlrs.build(
                        fixture.staging, fixture.inventory_path, fixture.publish,
                        latest_manifest=fixture.latest)
            with open(fixture.latest, 'rb') as source:
                self.assertEqual(source.read(), original)
            self.assertFalse(any(name.endswith('.pmtiles')
                                 for name in os.listdir(fixture.publish)))

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            os.makedirs(fixture.publish)
            original = _write_json(
                fixture.latest,
                {'schema_version': 1, 'artifacts': {'sentinel': True}})

            def mutating_tippecanoe(command, check):
                _valid_pmtiles(command[command.index('--output') + 1])
                path = os.path.join(fixture.staging, 'ak_active.json')
                with open(path, 'ab') as output:
                    output.write(b' ')

            with mock.patch.object(mlrs.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(mlrs.subprocess, 'run',
                                      side_effect=mutating_tippecanoe), \
                    mock.patch.object(mlrs, '_pmtiles_header',
                                      return_value=fixture.semantic_metadata()):
                with self.assertRaisesRegex(mlrs.PublicationError, 'changed during'):
                    mlrs.build(
                        fixture.staging, fixture.inventory_path, fixture.publish,
                        latest_manifest=fixture.latest)
            with open(fixture.latest, 'rb') as source:
                self.assertEqual(source.read(), original)
            self.assertFalse(any(name.endswith('.pmtiles')
                                 for name in os.listdir(fixture.publish)))

    def test_raw_staging_inside_site_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_site = os.path.join(directory, 'site')
            os.makedirs(fake_site)
            fixture = Fixture(fake_site)
            with mock.patch.object(mlrs, 'SITE', fake_site):
                with self.assertRaisesRegex(mlrs.PublicationError, 'outside site'):
                    fixture.load()


if __name__ == '__main__':
    unittest.main()
