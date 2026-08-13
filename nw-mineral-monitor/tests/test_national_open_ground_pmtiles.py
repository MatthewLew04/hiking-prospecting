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

ground = importlib.import_module('build_national_open_ground_pmtiles')


POINTS = {
    'AK': (-150.0, 64.0), 'AZ': (-112.0, 34.0),
    'AR': (-92.5, 35.0), 'CA': (-120.0, 37.0),
    'CO': (-105.5, 39.0), 'FL': (-82.0, 28.0),
    'ID': (-114.0, 44.0), 'LA': (-92.0, 31.0),
    'MS': (-89.5, 32.5), 'MT': (-110.0, 47.0),
    'NE': (-100.0, 41.0), 'NV': (-117.0, 39.0),
    'NM': (-106.0, 34.0), 'ND': (-100.0, 47.0),
    'OR': (-120.0, 44.0), 'SD': (-100.0, 44.0),
    'UT': (-111.5, 39.0), 'WA': (-120.5, 47.0),
    'WY': (-107.5, 43.0),
}


def _section_id(code, number=1):
    return f'{code}SECTION{number:03d}'


def _square(longitude, latitude, half=0.01):
    return {
        'type': 'Polygon',
        'coordinates': [[
            [longitude - half, latitude - half],
            [longitude + half, latitude - half],
            [longitude + half, latitude + half],
            [longitude - half, latitude + half],
            [longitude - half, latitude - half],
        ]],
    }


def _plss(code):
    longitude, latitude = POINTS[code]
    section_id = _section_id(code)
    return {
        'schema_version': 1,
        'state': code,
        'kind': 'plss',
        'retrieved': '2026-08-13',
        'complete': True,
        'n': 1,
        'source': ground.PLSS_SOURCE,
        'type': 'FeatureCollection',
        'features': [{
            'type': 'Feature',
            'id': section_id,
            'properties': {
                'section_id': section_id,
                'label': f'{code} test section',
            },
            'geometry': _square(longitude, latitude),
        }],
    }


def _claims(code):
    claims = []
    if code == 'AK':
        claims.append({
            'serial': 'AK000001',
            'name': 'Checked test claim',
            'disposition': 'ACTIVE',
            'source_object_id': 1,
            'section_ids': [_section_id(code)],
            'mapping_complete': True,
        })
    return {
        'schema_version': 1,
        'state': code,
        'kind': 'active_claims',
        'retrieved': '2026-08-13',
        'complete': True,
        'n': len(claims),
        'system': 'federal_mlrs',
        'source': ground.ACTIVE_CLAIMS_SOURCE,
        'mode': 'active',
        'unmapped_count': 0,
        'claims': claims,
    }


def _land_row(code):
    title_source = f'https://example.test/mineral-estate/{code.lower()}'
    return {
        'section_id': _section_id(code),
        'mineral_disposition': 'open_to_location',
        'surface_manager': 'BLM',
        'withdrawal_refs': [],
        'checked_sources': sorted(ground.LAND_STATUS_SOURCES),
        'boundary_uncertain': False,
        'evidence': 'All configured land-status sources checked for this section.',
        'mineral_title_status': 'public_domain_locatable',
        'mineral_title_source': title_source,
        'mineral_title_ref': f'TEST-TITLE:{_section_id(code)}',
        'mineral_title_reviewed': True,
    }


def _land(code):
    return {
        'schema_version': 1,
        'state': code,
        'kind': 'land_status',
        'retrieved': '2026-08-13',
        'complete': True,
        'n': 1,
        'sources': copy.deepcopy(ground.LAND_STATUS_SOURCES),
        'classifications': [_land_row(code)],
    }


def _write_json(path, value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    with open(path, 'wb') as output:
        output.write(raw)
    return raw


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
    if isinstance(value, int) and not isinstance(value, bool):
        return b'\x20' + _varint(value)
    if isinstance(value, float):
        return b'\x19' + struct.pack('<d', value)
    raise TypeError(value)


def _valid_pmtiles(path, *, layer='open_ground', omit_field=None,
                   garbage_tile=False, property_overrides=None, feature_id=1):
    properties = {
        'system': ground.SYSTEM,
        'st': 'NV',
        'status': 'OPEN',
        'open_count': 1,
        'section_count': 1,
        'open_fraction': 1.0,
        'title_caveat': ground.TITLE_CAVEAT,
        'withdrawal_caveat': ground.WITHDRAWAL_CAVEAT,
        'provenance': 'PLSS:111111111111;MLRS:222222222222;LAND:333333333333',
        'mineral_title_status': 'public_domain_locatable',
        'mineral_title_source': 'https://example.test/mineral-estate/nv',
        'mineral_title_ref': 'TEST-TITLE:NVSECTION001',
        'mineral_title_reviewed': 1,
    }
    properties.update(property_overrides or {})
    if omit_field:
        properties.pop(omit_field, None)
    fields = {
        name: ('Number' if isinstance(value, (int, float)) else 'String')
        for name, value in properties.items()
    }
    metadata_raw = json.dumps({
        'vector_layers': [{'id': layer, 'fields': fields}],
    }, separators=(',', ':')).encode()
    keys_and_values = bytearray()
    tags = bytearray()
    for index, (key, value) in enumerate(properties.items()):
        encoded_key = key.encode()
        encoded_value = _mvt_value(value)
        keys_and_values += b'\x1a' + _varint(len(encoded_key)) + encoded_key
        keys_and_values += b'\x22' + _varint(len(encoded_value)) + encoded_value
        tags += _varint(index) + _varint(index)
    # A small valid MVT polygon: MoveTo, three LineTo deltas, ClosePath.
    geometry = b'\x09\x02\x02\x1a\x14\x00\x00\x14\x13\x00\x0f'
    feature = (b'\x08' + _varint(feature_id) +
               b'\x12' + _varint(len(tags)) + tags +
               b'\x18\x03' + b'\x22' + _varint(len(geometry)) + geometry)
    encoded_layer = layer.encode()
    mvt_layer = (b'\x78\x02' + b'\x0a' + _varint(len(encoded_layer)) +
                 encoded_layer + b'\x12' + _varint(len(feature)) + feature +
                 keys_and_values + b'\x28\x80\x20')
    mvt = b'\x1a' + _varint(len(mvt_layer)) + mvt_layer
    tile = b'garbage' if garbage_tile else gzip.compress(mvt, mtime=0)
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
    struct.pack_into('<4i', header, 102, -1_200_000_000, 350_000_000,
                     -1_140_000_000, 420_000_000)
    header[118] = 0
    struct.pack_into('<2i', header, 119, -1_170_000_000, 385_000_000)
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
        makers = {
            'plss': _plss,
            'active_claims': _claims,
            'land_status': _land,
        }
        for code in sorted(ground.CLAIM_STATES):
            states[code] = {}
            for kind in ground.KINDS:
                data = makers[kind](code)
                self.snapshots[(code, kind)] = data
                filename = ground._snapshot_filename(code, kind)
                raw = _write_json(os.path.join(self.staging, filename), data)
                states[code][kind] = {
                    'file': filename,
                    'n': data['n'],
                    'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest(),
                    'retrieved': data['retrieved'],
                    'complete': True,
                }
        self.inventory = {
            'schema_version': 1,
            'system': ground.SYSTEM,
            'created': '2026-08-13',
            'sources': copy.deepcopy(ground.SOURCES),
            'clip': {
                'authority': ground.CLIP_AUTHORITY,
                'method': ground.CLIP_METHOD,
                'artifact_sha256': ground._sha256_file(
                    ground.DEFAULT_STATE_CLIPS),
            },
            'states': states,
        }
        self.save_inventory()

    def save_inventory(self):
        _write_json(self.inventory_path, self.inventory)

    def rewrite(self, code, kind, mutate):
        data = copy.deepcopy(self.snapshots[(code, kind)])
        mutate(data)
        collection = {
            'plss': 'features',
            'active_claims': 'claims',
            'land_status': 'classifications',
        }[kind]
        data['n'] = len(data[collection])
        self.snapshots[(code, kind)] = data
        entry = self.inventory['states'][code][kind]
        raw = _write_json(os.path.join(self.staging, entry['file']), data)
        entry['n'] = data['n']
        entry['bytes'] = len(raw)
        entry['sha256'] = hashlib.sha256(raw).hexdigest()
        entry['retrieved'] = data['retrieved']
        self.save_inventory()

    def load(self):
        context = ground.load_inventory(
            self.staging, self.inventory_path, ground.DEFAULT_STATE_CLIPS)
        # Synthetic fixtures explicitly model a reviewed title source. The
        # checked-in production registry remains honestly uningested.
        context['mineral_estates'] = {
            code: {
                'status': 'reviewed_ingested',
                'source_url': (
                    f'https://example.test/mineral-estate/{code.lower()}'),
            }
            for code in ground.CLAIM_STATES
        }
        context['mineral_estates_sha256'] = ground._sha256_bytes(json.dumps(
            context['mineral_estates'], sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode())
        return context

    def registry_patch(self):
        rows = copy.deepcopy(ground.load_states())
        for code in ground.CLAIM_STATES:
            rows[code]['open_ground']['mineral_estate'].update({
                'status': 'reviewed_ingested',
                'source_url': (
                    f'https://example.test/mineral-estate/{code.lower()}'),
                'ownership_field': 'TEST_TITLE',
                'locatable_values': ['PUBLIC_DOMAIN'],
            })
        return mock.patch.object(ground, 'load_states', return_value=rows)

    def stream(self, *, profile='release', states=None, context=None):
        context = context or self.load()
        output = os.path.join(self.base, 'open_ground.geojsonseq')
        stats = ground.stream_states(
            context, output, profile=profile, selected_states=states)
        with open(output, encoding='utf-8') as source:
            features = [json.loads(line) for line in source]
        return stats, features


class NationalOpenGroundPublicationTests(unittest.TestCase):
    def test_exact_19_state_scope_and_one_polygon_per_section(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            stats, features = fixture.stream(context=context)

        self.assertEqual(set(context['codes']), set(ground.CLAIM_STATES))
        self.assertEqual(len(context['codes']), 19)
        self.assertEqual(stats['n'], 19)
        self.assertEqual(stats['section_count'], 19)
        self.assertEqual(set(stats['states']), set(ground.CLAIM_STATES))
        self.assertTrue(all(value == 1 for value in stats['states'].values()))
        self.assertEqual(len(features), 19)
        self.assertTrue(all(row['geometry']['type'] == 'Polygon'
                            for row in features))
        self.assertEqual(len({row['properties']['unit_id'] for row in features}),
                         19)
        self.assertEqual(stats['status_counts']['ACTIVE'], 1)
        self.assertEqual(stats['status_counts']['OPEN'], 18)
        self.assertEqual(stats['state_status_counts']['AK']['ACTIVE'], 1)
        self.assertEqual(stats['state_status_counts']['AZ']['OPEN'], 1)
        self.assertEqual(stats['open_fraction'], 18 / 19)
        self.assertEqual(stats['partial_states'], [])

    def test_status_precedence_math_caveats_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)

            def withdrawn(data):
                row = data['classifications'][0]
                row['mineral_disposition'] = 'withdrawn'
                row['withdrawal_refs'] = ['W-123']

            def non_federal(data):
                row = data['classifications'][0]
                row['mineral_disposition'] = 'non_federal'
                row['surface_manager'] = 'STATE'
                row['mineral_title_status'] = 'non_federal'

            fixture.rewrite('AR', 'land_status', withdrawn)
            fixture.rewrite('CA', 'land_status', non_federal)
            _, features = fixture.stream()
        by_state = {row['properties']['st']: row['properties'] for row in features}
        self.assertEqual(by_state['AK']['status'], 'ACTIVE')
        self.assertEqual(by_state['AR']['status'], 'WITHDRAWN')
        self.assertEqual(by_state['AR']['withdrawal_count'], 1)
        self.assertEqual(by_state['CA']['status'], 'NONFEDERAL')
        self.assertEqual(by_state['AZ']['status'], 'OPEN')
        for code, properties in by_state.items():
            expected = 1 if code not in {'AK', 'AR', 'CA'} else 0
            self.assertEqual(properties['open_count'], expected)
            self.assertEqual(properties['section_count'], 1)
            self.assertEqual(properties['open_fraction'], float(expected))
            self.assertNotEqual(properties['status'], 'N/A')
            self.assertIn('not a title determination',
                          properties['title_caveat'])
            self.assertIn('verify current BLM',
                          properties['withdrawal_caveat'])
            self.assertRegex(
                properties['provenance'],
                r'^PLSS:[0-9a-f]{12};MLRS:[0-9a-f]{12};LAND:[0-9a-f]{12}$')

    def test_per_state_scope_still_uses_exact_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            stats, features = fixture.stream(states=['nv'])
            fixture.inventory['states'].pop('AR')
            fixture.save_inventory()
            with self.assertRaisesRegex(ground.PublicationError, 'exact registry 19'):
                fixture.load()
        self.assertEqual(stats['scope_states'], ['NV'])
        self.assertEqual(stats['states'], {'NV': 1})
        self.assertEqual(features[0]['properties']['st'], 'NV')
        self.assertEqual(features[0]['properties']['status'], 'OPEN')

    def test_missing_extra_state_bad_sources_and_bad_clip_fail_closed(self):
        mutations = [
            lambda value: value['states'].pop('AR'),
            lambda value: value['states'].__setitem__(
                'NY', copy.deepcopy(value['states']['AR'])),
            lambda value: value['sources'].__setitem__('plss', 'unreviewed'),
            lambda value: value['clip'].__setitem__('artifact_sha256', '0' * 64),
            lambda value: value['clip'].__setitem__('method', 'bbox only'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                mutate(fixture.inventory)
                fixture.save_inventory()
                with self.assertRaises(ground.PublicationError):
                    fixture.load()

    def test_missing_or_checksum_changed_file_fails_inventory_load(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            path = os.path.join(fixture.staging, 'az_land_status.json')
            os.unlink(path)
            with self.assertRaisesRegex(ground.PublicationError, 'missing'):
                fixture.load()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            path = os.path.join(fixture.staging, 'az_land_status.json')
            with open(path, 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(ground.PublicationError, 'sha256'):
                fixture.load()

    def test_release_full_reject_partial_and_progress_never_fakes_open(self):
        for profile in ('release', 'full'):
            with self.subTest(profile=profile), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                entry = fixture.inventory['states']['AZ']['active_claims']
                entry['complete'] = False
                entry['partial_reason'] = 'pagination checkpoint incomplete'
                fixture.save_inventory()
                with self.assertRaisesRegex(ground.PublicationError,
                                            'partial/capped'):
                    fixture.stream(profile=profile)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            entry = fixture.inventory['states']['AZ']['active_claims']
            entry['complete'] = False
            entry['partial_reason'] = 'pagination checkpoint incomplete'
            fixture.save_inventory()
            stats, features = fixture.stream(profile='progress', states=['AZ'])
        self.assertEqual(stats['partial_states'], ['AZ'])
        self.assertEqual(features[0]['properties']['status'], 'UNKNOWN')
        self.assertEqual(features[0]['properties']['partial'], 1)
        self.assertEqual(features[0]['properties']['open_count'], 0)

    def test_hidden_cap_and_total_available_are_rejected(self):
        mutations = [
            lambda data: data.update(
                capped=True, partial_reason='debug row cap was used'),
            lambda data: data.update(
                total_available=1, partial_reason='one upstream row missing'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.rewrite('AZ', 'active_claims', mutate)
                with self.assertRaisesRegex(ground.PublicationError,
                                            'partial/capped'):
                    fixture.stream()

    def test_unmapped_claim_blocks_release_and_suppresses_open_in_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)

            def mutate(data):
                data['claims'].append({
                    'serial': 'AZUNMAPPED',
                    'name': None,
                    'disposition': 'ACTIVE',
                    'source_object_id': 9,
                    'section_ids': [],
                    'mapping_complete': False,
                })
                data['unmapped_count'] = 1

            fixture.rewrite('AZ', 'active_claims', mutate)
            with self.assertRaisesRegex(ground.PublicationError,
                                        'unmapped claims'):
                fixture.stream(states=['AZ'])
            stats, features = fixture.stream(
                profile='progress', states=['AZ'])
        self.assertEqual(stats['partial_states'], ['AZ'])
        self.assertEqual(features[0]['properties']['status'], 'UNKNOWN')

    def test_duplicate_claims_bad_mapping_and_duplicate_land_rows_fail(self):
        cases = []

        def duplicate_claim(data):
            row = {
                'serial': 'AZDUP', 'name': None, 'disposition': 'ACTIVE',
                'source_object_id': 1, 'section_ids': [_section_id('AZ')],
                'mapping_complete': True,
            }
            data['claims'].extend((copy.deepcopy(row), copy.deepcopy(row)))

        cases.append(('duplicates serial', 'active_claims', duplicate_claim))

        def dishonest_mapping(data):
            data['claims'].append({
                'serial': 'AZEMPTY', 'name': None, 'disposition': 'ACTIVE',
                'source_object_id': 2, 'section_ids': [],
                'mapping_complete': True,
            })

        cases.append(('no sections', 'active_claims', dishonest_mapping))
        cases.append((
            'duplicates section', 'land_status',
            lambda data: data['classifications'].append(
                copy.deepcopy(data['classifications'][0]))))
        for message, kind, mutate in cases:
            with self.subTest(message=message), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.rewrite('AZ', kind, mutate)
                with self.assertRaisesRegex(ground.PublicationError, message):
                    fixture.stream(states=['AZ'])

    def test_land_status_must_cover_every_section_and_all_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'AZ', 'land_status',
                lambda data: data['classifications'][0][
                    'checked_sources'].remove('nlcs'))
            with self.assertRaisesRegex(ground.PublicationError,
                                        'unknown, unchecked'):
                fixture.stream(states=['AZ'])
            stats, features = fixture.stream(
                profile='progress', states=['AZ'])
        self.assertEqual(stats['partial_states'], ['AZ'])
        self.assertEqual(features[0]['properties']['status'], 'UNKNOWN')

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'AZ', 'land_status', lambda data: data['classifications'].clear())
            with self.assertRaisesRegex(ground.PublicationError,
                                        'missing a PLSS section'):
                fixture.stream(states=['AZ'])

    def test_withdrawal_evidence_invariants_fail_closed(self):
        cases = [
            lambda row: row.update(mineral_disposition='withdrawn'),
            lambda row: row['withdrawal_refs'].append('W-1'),
            lambda row: row.update(boundary_uncertain='false'),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.rewrite(
                    'AZ', 'land_status',
                    lambda data, change=mutate: change(data['classifications'][0]))
                with self.assertRaises(ground.PublicationError):
                    fixture.stream(states=['AZ'])

    def test_open_cannot_be_inferred_without_reviewed_mineral_title(self):
        cases = [
            lambda row: row.update(mineral_title_status='unknown',
                                   mineral_title_source=None,
                                   mineral_title_ref=None,
                                   mineral_title_reviewed=False),
            lambda row: row.update(mineral_title_reviewed=False),
            lambda row: row.update(mineral_title_source='https://wrong.test/title'),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.rewrite(
                    'AZ', 'land_status',
                    lambda data, change=mutate: change(
                        data['classifications'][0]))
                with self.assertRaisesRegex(
                        ground.PublicationError,
                        'OPEN requires|reviewed state-registry ingest'):
                    fixture.stream(states=['AZ'])

    def test_geometry_identity_and_state_clip_are_validated(self):
        def outside(data):
            data['features'][0]['geometry'] = _square(0.0, 0.0)

        def unclosed(data):
            data['features'][0]['geometry']['coordinates'][0][-1] = [0.0, 0.0]

        def degenerate(data):
            data['features'][0]['geometry']['coordinates'] = [[
                [-112.0, 34.0], [-112.0, 34.0], [-112.0, 34.0],
                [-112.0, 34.0],
            ]]

        def wrong_state(data):
            feature = data['features'][0]
            feature['id'] = 'NVSECTION001'
            feature['properties']['section_id'] = 'NVSECTION001'

        cases = {
            'outside': outside,
            'not closed': unclosed,
            'degenerate': degenerate,
            'not a AZ section': wrong_state,
        }
        for message, mutate in cases.items():
            with self.subTest(message=message), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.rewrite('AZ', 'plss', mutate)
                with self.assertRaisesRegex(ground.PublicationError, message):
                    fixture.stream(states=['AZ'])

    def test_stable_ids_are_state_scoped_and_javascript_safe(self):
        value = ground._feature_id('NV', 'NVSECTION001')
        self.assertEqual(value, ground._feature_id('NV', 'NVSECTION001'))
        self.assertNotEqual(value, ground._feature_id('UT', 'UTSECTION001'))
        self.assertNotEqual(value, ground._feature_id('NV', 'NVSECTION002'))
        self.assertTrue(0 < value <= ground.SAFE_INTEGER_MAX)

    def test_inventory_clip_and_any_of_57_inputs_cannot_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            fixture.stream(context=context, states=['NV'])
            with open(fixture.inventory_path, 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(ground.PublicationError,
                                        'inventory changed'):
                ground.assert_inputs_unchanged(context)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            # Mutate an unselected state: per-state publication still checks all
            # 57 inventory-pinned artifacts before changing its pointer.
            path = context['paths'][('AR', 'land_status')]
            with open(path, 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(ground.PublicationError,
                                        'AR.land_status changed'):
                ground.assert_inputs_unchanged(context)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            context['mineral_estates']['NV']['source_url'] = (
                'https://example.test/mineral-estate/mutated')
            with self.assertRaisesRegex(
                    ground.PublicationError,
                    'mineral-estate registry snapshot changed'):
                ground.assert_inputs_unchanged(context)

    def test_pmtiles_validation_requires_exact_layer_and_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.pmtiles')
            _valid_pmtiles(valid)
            self.assertEqual(ground.validate_pmtiles(valid)['source_layers'],
                             ['open_ground'])
            wrong = os.path.join(directory, 'wrong.pmtiles')
            _valid_pmtiles(wrong, layer='active')
            with self.assertRaisesRegex(ground.PublicationError,
                                        'open_ground'):
                ground.validate_pmtiles(wrong)
            missing = os.path.join(directory, 'missing.pmtiles')
            _valid_pmtiles(missing, omit_field='open_fraction')
            with self.assertRaisesRegex(ground.PublicationError,
                                        'required properties'):
                ground.validate_pmtiles(missing)
            garbage = os.path.join(directory, 'garbage.pmtiles')
            _valid_pmtiles(garbage, garbage_tile=True)
            with self.assertRaisesRegex(ground.PublicationError,
                                        'invalid PMTiles archive'):
                ground.validate_pmtiles(garbage)
            with self.assertRaisesRegex(ground.PublicationError,
                                        'feature state'):
                ground.validate_pmtiles(valid, ['AZ'])
            fake_title = os.path.join(directory, 'fake-title.pmtiles')
            _valid_pmtiles(fake_title, property_overrides={
                'mineral_title_status': 'unknown',
                'mineral_title_source': '',
                'mineral_title_ref': '',
                'mineral_title_reviewed': 0,
            })
            with self.assertRaisesRegex(
                    ground.PublicationError, 'public-domain locatable'):
                ground.validate_pmtiles(fake_title)
            wrong_title_source = os.path.join(
                directory, 'wrong-title-source.pmtiles')
            _valid_pmtiles(wrong_title_source)
            with self.assertRaisesRegex(
                    ground.PublicationError, 'reviewed state-registry ingest'):
                ground.validate_pmtiles(
                    wrong_title_source, ['NV'],
                    {'NV': 'https://official.example/mineral-estate/nv'})
            wrong_math_status = os.path.join(directory, 'wrong-status.pmtiles')
            _valid_pmtiles(wrong_math_status, property_overrides={
                'status': 'UNKNOWN',
            })
            with self.assertRaisesRegex(
                    ground.PublicationError, 'status disagrees'):
                ground.validate_pmtiles(wrong_math_status)
            missing_section = {
                'status': 'complete_at_derivation',
                'source_records': 2,
                'ids_sha256': ground._sha256_bytes(json.dumps(
                    [1, 2], separators=(',', ':')).encode()),
            }
            with self.assertRaisesRegex(
                    ground.PublicationError, 'source-section ID reconciliation'):
                ground.validate_pmtiles(
                    valid, expected_source_inventory=missing_section)

    def test_build_installs_content_addressed_per_state_archive_and_merges(self):
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
                _valid_pmtiles(
                    command[command.index('--output') + 1],
                    feature_id=ground._feature_id('NV', _section_id('NV')))

            with mock.patch.object(ground.shutil, 'which',
                                   return_value='/tippecanoe'), \
                    mock.patch.object(ground.subprocess, 'run',
                                      side_effect=fake_tippecanoe), \
                    fixture.registry_patch():
                result = ground.build(
                    fixture.staging, fixture.inventory_path, fixture.publish,
                    latest_manifest=fixture.latest, states=['NV'])

            with open(fixture.latest, encoding='utf-8') as source:
                latest = json.load(source)
            artifact_sha = ground._sha256_file(result['artifact'])
            publish_names = os.listdir(fixture.publish)
        self.assertEqual(result['artifact_key'], 'federal_open_ground_nv')
        self.assertEqual(latest['artifacts']['other'], {'n': 7})
        self.assertEqual(latest['operator_note'], 'preserve me')
        entry = latest['artifacts']['federal_open_ground_nv']
        self.assertEqual(entry['scope'], {'kind': 'states', 'states': ['NV']})
        self.assertEqual(entry['states'], {'NV': 1})
        self.assertEqual(entry['state_status_counts']['NV']['OPEN'], 1)
        self.assertEqual(entry['partial_states'], [])
        self.assertEqual(entry['sha256'], artifact_sha)
        self.assertRegex(
            os.path.basename(result['artifact']),
            r'^federal-open-ground-nv-[0-9a-f]{20}\.pmtiles$')
        layer_index = commands[0].index('-L')
        self.assertTrue(commands[0][layer_index + 1].startswith('open_ground:'))
        self.assertIn('--no-feature-limit', commands[0])
        self.assertIn('--no-tile-size-limit', commands[0])
        self.assertNotIn('--drop-densest-as-needed', commands[0])
        self.assertEqual(entry['source_id_inventory']['source_records'], 1)
        self.assertEqual(
            entry['source_id_inventory']['maxzoom_unique_tiled_ids'], 1)
        self.assertFalse(any(name.endswith(('.jsonseq', '.geojson'))
                             for name in publish_names))

    def test_concurrent_latest_merge_preserved_and_failures_do_not_point(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            os.makedirs(fixture.publish)
            _write_json(fixture.latest, {'schema_version': 1, 'artifacts': {}})

            def concurrent_tippecanoe(command, check):
                _write_json(fixture.latest, {
                    'schema_version': 1,
                    'artifacts': {'concurrent': {'generation': 2}},
                })
                _valid_pmtiles(
                    command[command.index('--output') + 1],
                    feature_id=ground._feature_id('NV', _section_id('NV')))

            with mock.patch.object(ground.shutil, 'which',
                                   return_value='/tippecanoe'), \
                    mock.patch.object(ground.subprocess, 'run',
                                      side_effect=concurrent_tippecanoe), \
                    fixture.registry_patch():
                ground.build(fixture.staging, fixture.inventory_path,
                             fixture.publish, latest_manifest=fixture.latest,
                             states=['NV'])
            with open(fixture.latest, encoding='utf-8') as source:
                latest = json.load(source)
        self.assertEqual(latest['artifacts']['concurrent'], {'generation': 2})
        self.assertIn('federal_open_ground_nv', latest['artifacts'])

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            os.makedirs(fixture.publish)
            original = _write_json(
                fixture.latest,
                {'schema_version': 1, 'artifacts': {'sentinel': True}})

            def invalid_tippecanoe(command, check):
                with open(command[command.index('--output') + 1], 'wb') as output:
                    output.write(b'PMTiles\x03short')

            with mock.patch.object(ground.shutil, 'which',
                                   return_value='/tippecanoe'), \
                    mock.patch.object(ground.subprocess, 'run',
                                      side_effect=invalid_tippecanoe), \
                    fixture.registry_patch():
                with self.assertRaises(ground.PublicationError):
                    ground.build(
                        fixture.staging, fixture.inventory_path, fixture.publish,
                        latest_manifest=fixture.latest, states=['NV'])
            with open(fixture.latest, 'rb') as source:
                self.assertEqual(source.read(), original)
            self.assertFalse(any(name.endswith('.pmtiles')
                                 for name in os.listdir(fixture.publish)))

    def test_input_change_during_tiling_never_updates_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            os.makedirs(fixture.publish)
            original = _write_json(
                fixture.latest,
                {'schema_version': 1, 'artifacts': {'sentinel': True}})

            def mutating_tippecanoe(command, check):
                _valid_pmtiles(
                    command[command.index('--output') + 1],
                    feature_id=ground._feature_id('NV', _section_id('NV')))
                with open(os.path.join(fixture.staging, 'ar_land_status.json'),
                          'ab') as output:
                    output.write(b' ')

            with mock.patch.object(ground.shutil, 'which',
                                   return_value='/tippecanoe'), \
                    mock.patch.object(ground.subprocess, 'run',
                                      side_effect=mutating_tippecanoe), \
                    fixture.registry_patch():
                with self.assertRaisesRegex(ground.PublicationError,
                                            'changed during'):
                    ground.build(
                        fixture.staging, fixture.inventory_path, fixture.publish,
                        latest_manifest=fixture.latest, states=['NV'])
            with open(fixture.latest, 'rb') as source:
                self.assertEqual(source.read(), original)
            self.assertFalse(any(name.endswith('.pmtiles')
                                 for name in os.listdir(fixture.publish)))

    def test_raw_staging_inside_site_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_site = os.path.join(directory, 'site')
            os.makedirs(fake_site)
            fixture = Fixture(fake_site)
            with mock.patch.object(ground, 'SITE', fake_site):
                with self.assertRaisesRegex(ground.PublicationError,
                                            'outside site'):
                    fixture.load()


if __name__ == '__main__':
    unittest.main()
