import copy
import gzip
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

equiv = importlib.import_module('build_nonclaim_equivalents_pmtiles')


MI_POINT = [-85.5, 44.0]


def _square(x, y, half=0.01):
    return {
        'type': 'Polygon',
        'coordinates': [[
            [x - half, y - half], [x + half, y - half],
            [x + half, y + half], [x - half, y + half],
            [x - half, y - half],
        ]],
    }


def _registry_url(registry):
    return next((value for key, value in registry.items()
                 if key.endswith('_url') and isinstance(value, str) and
                 value.startswith('https://')),
                ('https://www.osmre.gov/programs/abandoned-mine-land-'
                 'reclamation/e-amlis'))


def _finding(code, kind, registry):
    value = {
        'schema_version': 1,
        'state': code,
        'kind': kind,
        'release_inventory_status': (
            'documented_unavailable' if kind == 'aml' else 'not_applicable'),
        'source_id': (registry['source_id'] if kind == 'aml'
                      else f'{code.lower()}_trust_review'),
        'reviewed': '2026-08-13',
        'complete': True,
        'official_source_urls': [_registry_url(registry)],
        'spatial_inventory_available': False,
        'finding': (f'Review of the official {code} {kind} resources found no '
                    'complete public spatial inventory suitable for this '
                    'publication generation.'),
    }
    if kind == 'trust_land':
        value['offering_class'] = 'not_offered'
    return value


def _spatial(code, kind, registry, features):
    value = {
        'schema_version': 1,
        'state': code,
        'kind': kind,
        'release_inventory_status': 'ingested_complete',
        'source_id': (registry['source_id'] if kind == 'aml'
                      else f'{code.lower()}_trust_inventory'),
        'reviewed': '2026-08-13',
        'complete': True,
        'official_source_urls': [_registry_url(registry)],
        'retrieved': '2026-08-13',
        'truncated': False,
        'pagination': {
            'method': 'single_file',
            'expected_count': len(features),
            'fetched_count': len(features),
            'page_size': len(features),
            'page_offsets': [0],
            'page_row_counts': [len(features)],
            'pagination_exhausted': True,
            'source_snapshot_id': 'fixture-etag-1',
        },
        'type': 'FeatureCollection',
        'features': features,
    }
    if kind == 'trust_land':
        value['offering_class'] = 'offered'
    return value


def _aml_feature(record_id='AML-1', point=None):
    return {
        'type': 'Feature',
        'id': record_id,
        'properties': {
            'record_id': record_id,
            'source_id': 'mi_egle_abandoned_mining_wastes',
            'status': 'unreclaimed',
            'name': 'Fixture mine waste site',
        },
        'geometry': {'type': 'Point', 'coordinates': point or MI_POINT},
    }


def _trust_feature(record_id='TRUST-1', point=None):
    point = point or MI_POINT
    return {
        'type': 'Feature',
        'id': record_id,
        'properties': {
            'record_id': record_id,
            'mineral_class': 'state-owned metallic minerals',
            'approach': ('Verify DNR mineral ownership, then use the state '
                         'nomination and leasing process.'),
            'parcel_id': 'P-1',
            'status': 'eligible subject to review',
        },
        'geometry': _square(point[0], point[1]),
    }


def _write_json(path, value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as output:
        output.write(raw)
    return raw


class Fixture:
    def __init__(self, directory, spatial_mi=False):
        self.base = directory
        self.staging = os.path.join(directory, 'private-staging')
        self.inventory_path = os.path.join(self.staging, 'inventory.json')
        self.publish = os.path.join(directory, 'publish')
        self.latest = os.path.join(self.publish, 'latest.json')
        os.makedirs(self.staging)
        self.registry = equiv.load_states()
        self.snapshots = {}
        states = {}
        for code in sorted(equiv.NON_CLAIM_STATES):
            states[code] = {}
            for kind in equiv.KINDS:
                registry = self.registry[code][kind]
                if spatial_mi and code == 'MI':
                    features = ([_aml_feature()] if kind == 'aml'
                                else [_trust_feature()])
                    snapshot = _spatial(code, kind, registry, features)
                else:
                    snapshot = _finding(code, kind, registry)
                self.snapshots[(code, kind)] = snapshot
                filename = equiv._snapshot_filename(code, kind)
                raw = _write_json(os.path.join(self.staging, filename), snapshot)
                states[code][kind] = {
                    'file': filename,
                    'n': len(snapshot.get('features', [])),
                    'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest(),
                    'release_inventory_status': snapshot[
                        'release_inventory_status'],
                }
        self.inventory = {
            'schema_version': 1,
            'system': equiv.SYSTEM,
            'created': '2026-08-13',
            'clip': {
                'authority': equiv.CLIP_AUTHORITY,
                'method': equiv.CLIP_METHOD,
                'artifact_sha256': equiv._sha256_file(
                    equiv.DEFAULT_STATE_CLIPS),
            },
            'states': states,
        }
        self.save_inventory()

    def save_inventory(self):
        _write_json(self.inventory_path, self.inventory)

    def rewrite(self, code, kind, mutate):
        snapshot = copy.deepcopy(self.snapshots[(code, kind)])
        mutate(snapshot)
        self.snapshots[(code, kind)] = snapshot
        entry = self.inventory['states'][code][kind]
        raw = _write_json(os.path.join(self.staging, entry['file']), snapshot)
        entry['n'] = len(snapshot.get('features', []))
        entry['bytes'] = len(raw)
        entry['sha256'] = hashlib.sha256(raw).hexdigest()
        entry['release_inventory_status'] = snapshot[
            'release_inventory_status']
        self.save_inventory()

    def load(self):
        return equiv.load_inventory(
            self.staging, self.inventory_path, equiv.DEFAULT_STATE_CLIPS)


def _varint(value):
    output = bytearray()
    while value > 0x7f:
        output.append((value & 0x7f) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _mvt_value(value):
    if isinstance(value, str):
        encoded = value.encode()
        return b'\x0a' + _varint(len(encoded)) + encoded
    if isinstance(value, bool):
        return b'\x38' + _varint(int(value))
    if isinstance(value, int):
        return b'\x20' + _varint(value)
    if isinstance(value, float):
        return b'\x19' + struct.pack('<d', value)
    raise TypeError(value)


def _valid_pmtiles(path, kind, properties, *, geometry_type=None,
                   omit_field=None, extra_layer=None, garbage=False):
    geometry_type = geometry_type or equiv.MVT_GEOMETRY_TYPES[kind]
    properties = [dict(item) for item in properties]
    if omit_field:
        for item in properties:
            item.pop(omit_field, None)
    keys = sorted({key for item in properties for key in item})
    encoded_values = bytearray()
    raw_features = bytearray()
    value_index = 0
    for feature_index, item in enumerate(properties, 1):
        tags = bytearray()
        for key in keys:
            if key not in item:
                continue
            value = _mvt_value(item[key])
            encoded_values += b'\x22' + _varint(len(value)) + value
            tags += _varint(keys.index(key)) + _varint(value_index)
            value_index += 1
        geometry = (b'\x09\x02\x02' if geometry_type == 1 else
                    b'\x09\x02\x02\x1a\x14\x00\x00\x14\x13\x00\x0f')
        feature = (b'\x08' + _varint(feature_index) +
                   b'\x12' + _varint(len(tags)) + tags +
                   b'\x18' + _varint(geometry_type) +
                   b'\x22' + _varint(len(geometry)) + geometry)
        raw_features += b'\x12' + _varint(len(feature)) + feature
    encoded_keys = bytearray()
    for key in keys:
        raw = key.encode()
        encoded_keys += b'\x1a' + _varint(len(raw)) + raw
    layer_name = kind.encode()
    mvt_layer = (b'\x78\x02' + b'\x0a' + _varint(len(layer_name)) +
                 layer_name + raw_features + encoded_keys + encoded_values +
                 b'\x28\x80\x20')
    mvt = b'\x1a' + _varint(len(mvt_layer)) + mvt_layer
    if extra_layer:
        name = extra_layer.encode()
        other = (b'\x78\x02' + b'\x0a' + _varint(len(name)) + name +
                 raw_features + encoded_keys + encoded_values + b'\x28\x80\x20')
        mvt += b'\x1a' + _varint(len(other)) + other
    tile = b'garbage' if garbage else gzip.compress(mvt, mtime=0)
    layer_meta = [{'id': kind, 'fields': {
        key: ('Number' if isinstance(next(item[key] for item in properties
                                         if key in item), (int, float))
              else 'String') for key in keys}}]
    if extra_layer:
        layer_meta.append({'id': extra_layer, 'fields': layer_meta[0]['fields']})
    metadata_raw = json.dumps(
        {'vector_layers': layer_meta}, separators=(',', ':')).encode()
    root_raw = b'\x01\x00\x01' + _varint(len(tile)) + b'\x01'
    root = gzip.compress(root_raw, mtime=0)
    metadata = gzip.compress(metadata_raw, mtime=0)
    root_offset = 127
    metadata_offset = root_offset + len(root)
    tile_offset = metadata_offset + len(metadata)
    header = bytearray(127)
    header[:8] = b'PMTiles\x03'
    struct.pack_into(
        '<11Q', header, 8,
        root_offset, len(root), metadata_offset, len(metadata),
        0, 0, tile_offset, len(tile), 1, 1, 1)
    header[96:102] = bytes((1, 2, 2, 1, 0, 0))
    struct.pack_into('<4i', header, 102,
                     -900_000_000, 410_000_000, -820_000_000, 490_000_000)
    header[118] = 0
    struct.pack_into('<2i', header, 119, -855_000_000, 440_000_000)
    with open(path, 'wb') as output:
        output.write(header)
        output.write(root)
        output.write(metadata)
        output.write(tile)


def _archive_properties(kind, record_ids=('AML-1',)):
    if kind == 'aml':
        return [{
            'st': 'MI', 'source_id': 'mi_egle_abandoned_mining_wastes',
            'status': 'unreclaimed', 'record_id': record_id,
            'provenance': 'fixture:1234567890abcdef',
        } for record_id in record_ids]
    return [{
        'st': 'MI', 'mineral_class': 'state-owned metallic minerals',
        'approach': 'Contact Michigan DNR for nomination and lease review.',
        'record_id': record_id, 'provenance': 'fixture:1234567890abcdef',
    } for record_id in record_ids]


class NonclaimEquivalentsTests(unittest.TestCase):
    def test_exact_30_scope_normalizes_spatial_rows_and_preserves_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, spatial_mi=True)
            context = fixture.load()
            aml = equiv._load_snapshot(context, 'MI', 'aml')
            trust = equiv._load_snapshot(context, 'MI', 'trust_land')
            finding = equiv._load_snapshot(context, 'AL', 'aml')
        self.assertEqual(len(context['codes']), 30)
        self.assertEqual(set(context['codes']), set(equiv.NON_CLAIM_STATES))
        self.assertEqual(aml['features'][0]['properties']['st'], 'MI')
        self.assertEqual(aml['features'][0]['properties']['status'], 'unreclaimed')
        self.assertEqual(trust['features'][0]['properties']['mineral_class'],
                         'state-owned metallic minerals')
        self.assertEqual(finding['n'], 0)
        self.assertEqual(finding['snapshot']['finding'],
                         fixture.snapshots[('AL', 'aml')]['finding'])

    def test_missing_extra_state_wrong_kind_and_checksum_fail_closed(self):
        mutations = [
            lambda value: value['states'].pop('AL'),
            lambda value: value['states'].__setitem__(
                'AZ', copy.deepcopy(value['states']['AL'])),
            lambda value: value['states']['AL'].pop('aml'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                mutate(fixture.inventory)
                fixture.save_inventory()
                with self.assertRaises(equiv.PublicationError):
                    fixture.load()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with open(os.path.join(fixture.staging, 'al_aml.json'), 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(equiv.PublicationError, 'sha256'):
                fixture.load()

    def test_inventory_and_raw_snapshots_cannot_live_under_site(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_site = os.path.join(directory, 'site')
            os.makedirs(fake_site)
            fixture = Fixture(fake_site)
            with mock.patch.object(equiv, 'SITE', fake_site), self.assertRaisesRegex(
                    equiv.PublicationError, 'outside site'):
                fixture.load()

    def test_partial_truncated_and_bad_pagination_never_publish(self):
        cases = [
            ('explicitly complete', lambda data: data.update(complete=False)),
            ('truncated input', lambda data: data.update(truncated=True)),
            ('exhaust', lambda data: data['pagination'].update(
                pagination_exhausted=False)),
            ('offsets', lambda data: data['pagination'].update(page_offsets=[1])),
            ('expected_count', lambda data: data['pagination'].update(
                expected_count=2)),
        ]
        for message, mutate in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, spatial_mi=True)
                fixture.rewrite('MI', 'aml', mutate)
                context = fixture.load()
                with self.assertRaisesRegex(equiv.PublicationError, message):
                    equiv._load_snapshot(context, 'MI', 'aml')

    def test_registry_source_url_status_and_offering_bindings_are_strict(self):
        cases = [
            ('official registry URL', 'aml', lambda data: data.update(
                official_source_urls=['https://example.invalid/not-official'])),
            ('source_id', 'aml', lambda data: data.update(source_id='invented')),
            ('offering_class', 'trust_land', lambda data: data.update(
                offering_class='not_offered')),
        ]
        for message, kind, mutate in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, spatial_mi=True)
                fixture.rewrite('MI', kind, mutate)
                context = fixture.load()
                with self.assertRaisesRegex(equiv.PublicationError, message):
                    equiv._load_snapshot(context, 'MI', kind)

    def test_geometry_state_clip_identity_and_scalar_properties_are_checked(self):
        mutations = {
            'outside': lambda data: data['features'][0]['geometry'].update(
                coordinates=[0.0, 0.0]),
            'geometry must': lambda data: data['features'][0].update(
                geometry=_square(*MI_POINT)),
            'exactly match': lambda data: data['features'][0].update(id='OTHER'),
            'scalar': lambda data: data['features'][0]['properties'].update(
                nested={'unsafe': True}),
        }
        for message, mutate in mutations.items():
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, spatial_mi=True)
                fixture.rewrite('MI', 'aml', mutate)
                with self.assertRaisesRegex(equiv.PublicationError, message):
                    equiv._load_snapshot(fixture.load(), 'MI', 'aml')

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, spatial_mi=True)
            fixture.rewrite('MI', 'aml', lambda data: data['features'].append(
                copy.deepcopy(data['features'][0])))
            # Keep pagination internally complete so duplicate identity is the
            # failure being tested.
            row = fixture.snapshots[('MI', 'aml')]
            row['pagination'].update(
                expected_count=2, fetched_count=2, page_size=2,
                page_row_counts=[2])
            fixture.rewrite('MI', 'aml', lambda data: data.update(
                pagination=copy.deepcopy(row['pagination'])))
            with self.assertRaisesRegex(equiv.PublicationError, 'duplicates record_id'):
                equiv._load_snapshot(fixture.load(), 'MI', 'aml')

    def test_state_selection_rejects_claim_states_but_keeps_full_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            self.assertEqual(equiv._selected_codes(context, ['mi']), ('MI',))
            with self.assertRaisesRegex(equiv.PublicationError, 'claim/unsupported'):
                equiv._selected_codes(context, ['NV'])
            context = fixture.load()
            with open(context['paths'][('AL', 'aml')], 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(equiv.PublicationError, 'AL.aml changed'):
                equiv.assert_inputs_unchanged(context)

    def test_full_pmtiles_semantics_require_exact_layer_fields_geometry_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.pmtiles')
            _valid_pmtiles(valid, 'aml', _archive_properties('aml'))
            checked = equiv.validate_pmtiles(
                valid, 'MI', 'aml', {'AML-1'}, [[-90, 41, -82, 49]])
            self.assertEqual(checked['record_ids'], {'AML-1'})

            missing = os.path.join(directory, 'missing.pmtiles')
            _valid_pmtiles(missing, 'aml', _archive_properties('aml'),
                           omit_field='status')
            with self.assertRaisesRegex(equiv.PublicationError, 'required properties'):
                equiv.validate_pmtiles(missing, 'MI', 'aml', {'AML-1'})

            wrong_geometry = os.path.join(directory, 'geometry.pmtiles')
            _valid_pmtiles(wrong_geometry, 'aml', _archive_properties('aml'),
                           geometry_type=3)
            with self.assertRaisesRegex(equiv.PublicationError, 'wrong geometry'):
                equiv.validate_pmtiles(wrong_geometry, 'MI', 'aml', {'AML-1'})

            wrong_rows = os.path.join(directory, 'rows.pmtiles')
            _valid_pmtiles(wrong_rows, 'aml', _archive_properties('aml', ('OTHER',)))
            with self.assertRaisesRegex(equiv.PublicationError, 'record IDs differ'):
                equiv.validate_pmtiles(wrong_rows, 'MI', 'aml', {'AML-1'})

            extra = os.path.join(directory, 'extra.pmtiles')
            _valid_pmtiles(extra, 'aml', _archive_properties('aml'),
                           extra_layer='invented')
            with self.assertRaisesRegex(equiv.PublicationError, 'exactly aml'):
                equiv.validate_pmtiles(extra, 'MI', 'aml', {'AML-1'})

            garbage = os.path.join(directory, 'garbage.pmtiles')
            _valid_pmtiles(garbage, 'aml', _archive_properties('aml'), garbage=True)
            with self.assertRaisesRegex(equiv.PublicationError, 'invalid PMTiles'):
                equiv.validate_pmtiles(garbage, 'MI', 'aml', {'AML-1'})

    def test_build_installs_content_addressed_archives_and_exact_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, spatial_mi=True)
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
                layer_arg = command[command.index('-L') + 1]
                kind, sequence = layer_arg.split(':', 1)
                with open(sequence, encoding='utf-8') as source:
                    rows = [json.loads(line) for line in source]
                properties = [row['properties'] for row in rows]
                _valid_pmtiles(command[command.index('--output') + 1],
                               kind, properties)

            with mock.patch.object(equiv.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(equiv.subprocess, 'run',
                                      side_effect=fake_tippecanoe):
                result = equiv.build(
                    fixture.staging, fixture.inventory_path, fixture.publish,
                    latest_manifest=fixture.latest, states=['MI'])

            with open(fixture.latest, encoding='utf-8') as source:
                latest = json.load(source)
            entry = latest['artifacts']['nonclaim_equivalents_mi']
            aml = entry['decisions']['aml']
            trust = entry['decisions']['trust_land']
            with open(os.path.join(fixture.publish, aml['evidence_file']),
                      encoding='utf-8') as source:
                evidence = json.load(source)
            names = os.listdir(fixture.publish)
        self.assertEqual(result['states'], ['MI'])
        self.assertEqual(len(commands), 2)
        self.assertEqual(latest['artifacts']['other'], {'n': 7})
        self.assertEqual(latest['operator_note'], 'preserve me')
        self.assertEqual(aml['layer_metadata']['aml']['n'], 1)
        self.assertEqual(trust['offering_class'], 'offered')
        self.assertEqual(evidence['artifact_sha256'], aml['sha256'])
        self.assertEqual(evidence['source_layer_counts'], {'aml': 1})
        self.assertTrue(evidence['pagination']['pagination_exhausted'])
        self.assertEqual(aml['file'], f'{aml["sha256"]}.pmtiles')
        self.assertEqual(trust['file'], f'{trust["sha256"]}.pmtiles')
        self.assertEqual(aml['evidence_bytes'], len(equiv._canonical_json(evidence)))
        self.assertRegex(aml['evidence_sha256'], r'^[0-9a-f]{64}$')
        self.assertTrue(aml['evidence_file'].endswith(
            f'{aml["evidence_sha256"]}.json'))
        self.assertFalse(any(name.endswith(('.geojson', '.jsonseq')) for name in names))

    def test_finding_only_workflow_needs_no_tiler_and_fabricates_no_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            original_finding = fixture.snapshots[('AL', 'aml')]['finding']
            with mock.patch.object(equiv.shutil, 'which', return_value=None):
                equiv.build(fixture.staging, fixture.inventory_path,
                            fixture.publish, states=['AL'])
            with open(fixture.latest, encoding='utf-8') as source:
                latest = json.load(source)
            decisions = latest['artifacts']['nonclaim_equivalents_al']['decisions']
            with open(os.path.join(fixture.publish,
                                   decisions['aml']['evidence_file']),
                      encoding='utf-8') as source:
                evidence = json.load(source)
            published_names = os.listdir(fixture.publish)
        self.assertNotIn('file', decisions['aml'])
        self.assertNotIn('file', decisions['trust_land'])
        self.assertEqual(evidence['finding'], original_finding)
        self.assertFalse(any(name.endswith('.pmtiles')
                             for name in published_names))

    def test_omitting_state_builds_exact_30_decisions_without_claim_states(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with mock.patch.object(equiv.shutil, 'which', return_value=None):
                result = equiv.build(
                    fixture.staging, fixture.inventory_path, fixture.publish)
            with open(fixture.latest, encoding='utf-8') as source:
                latest = json.load(source)
        keys = {key for key in latest['artifacts']
                if key.startswith('nonclaim_equivalents_')}
        self.assertEqual(result['decisions'], 60)
        self.assertEqual(set(result['states']), set(equiv.NON_CLAIM_STATES))
        self.assertEqual(keys, {
            f'nonclaim_equivalents_{code.lower()}'
            for code in equiv.NON_CLAIM_STATES})

    def test_invalid_tile_or_any_input_change_leaves_pointer_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, spatial_mi=True)
            os.makedirs(fixture.publish)
            original = _write_json(
                fixture.latest,
                {'schema_version': 1, 'artifacts': {'sentinel': True}})

            def invalid_tippecanoe(command, check):
                with open(command[command.index('--output') + 1], 'wb') as output:
                    output.write(b'PMTiles\x03short')

            with mock.patch.object(equiv.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(equiv.subprocess, 'run',
                                      side_effect=invalid_tippecanoe), \
                    self.assertRaises(equiv.PublicationError):
                equiv.build(fixture.staging, fixture.inventory_path,
                            fixture.publish, latest_manifest=fixture.latest,
                            states=['MI'])
            with open(fixture.latest, 'rb') as source:
                self.assertEqual(source.read(), original)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, spatial_mi=True)
            os.makedirs(fixture.publish)
            original = _write_json(
                fixture.latest,
                {'schema_version': 1, 'artifacts': {'sentinel': True}})
            calls = 0

            def mutating_tippecanoe(command, check):
                nonlocal calls
                calls += 1
                kind, sequence = command[command.index('-L') + 1].split(':', 1)
                with open(sequence, encoding='utf-8') as source:
                    rows = [json.loads(line) for line in source]
                _valid_pmtiles(command[command.index('--output') + 1], kind,
                               [row['properties'] for row in rows])
                if calls == 1:
                    with open(os.path.join(fixture.staging, 'al_aml.json'), 'ab') as output:
                        output.write(b' ')

            with mock.patch.object(equiv.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(equiv.subprocess, 'run',
                                      side_effect=mutating_tippecanoe), \
                    self.assertRaisesRegex(equiv.PublicationError, 'AL.aml changed'):
                equiv.build(fixture.staging, fixture.inventory_path,
                            fixture.publish, latest_manifest=fixture.latest,
                            states=['MI'])
            with open(fixture.latest, 'rb') as source:
                self.assertEqual(source.read(), original)


if __name__ == '__main__':
    unittest.main()
