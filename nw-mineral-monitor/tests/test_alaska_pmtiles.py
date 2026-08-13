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

alaska = importlib.import_module('build_alaska_pmtiles')


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def clockwise_square(west=-150.0, south=60.0, east=-149.0, north=61.0):
    return [[west, south], [west, north], [east, north], [east, south],
            [west, south]]


def source_inventory(n, first, last, seed):
    return {
        'n': n,
        'minimum_object_id': first,
        'maximum_object_id': last,
        'object_ids_sha256': seed * 64,
        'layer_metadata_sha256': chr(ord(seed) + 1) * 64,
        'records_sha256': chr(ord(seed) + 2) * 64,
        'verification': {
            'page_mode': 'exact_object_ids',
            'full_second_feature_pass': True,
            'postflight_metadata_match': True,
            'postflight_object_ids_match': True,
            'geometry_precision': 8,
        },
    }


CLAIM_SOURCE_INVENTORY = {
    'active': source_inventory(2, 1, 2, 'a'),
    'pending': source_inventory(1, 1, 1, 'd'),
    'closed': source_inventory(1, 1, 1, 'g'),
}
CLAIM_SOURCE_EXPECTED = {
    status: {key: value for key, value in inventory.items()
             if key != 'verification'}
    for status, inventory in CLAIM_SOURCE_INVENTORY.items()
}
ARDF_SOURCE_INVENTORY = source_inventory(2, 1, 3, 'j')
ARDF_SOURCE_EXPECTED = {
    key: value for key, value in ARDF_SOURCE_INVENTORY.items()
    if key != 'verification'
}


def claim(serial, status, *, source_oid=1, geometry=None, name='Test claim'):
    number = serial.split()[-1]
    return {
        'source_objectid': source_oid,
        'claim_key': f'alaska_state_claims:{serial}',
        'system_id': 'alaska_state_claims',
        'jurisdiction': 'state',
        'serial': serial,
        'adl': serial,
        'name': name,
        'status': status,
        'source_status': status.upper(),
        'posting_date': 1_577_836_800_000,
        'annual_labor_filed': 1_756_684_800_000,
        'acres': '40.000',
        'mtrsc': 'F001N001E01',
        'meridian_township_range': '1',
        'sections': '1',
        'file_number': number,
        'refresh_date': 1_786_060_800_000,
        'info_link': ('https://dnr.alaska.gov/projects/las/#filetype/ADL/'
                      f'filenumber/{number}'),
        'geometry': geometry or {'rings': [clockwise_square()]},
    }


def claims_snapshot():
    duplicate = claim('ADL 100', 'active')
    second = copy.deepcopy(duplicate)
    second['source_objectid'] = 2
    return {
        'state': 'AK',
        'system_id': 'alaska_state_claims',
        'retrieved': alaska.SNAPSHOT_DATE,
        'source': alaska.CLAIMS_SOURCE,
        'snapshot_contract': alaska.SNAPSHOT_CONTRACT,
        'source_inventory': copy.deepcopy(CLAIM_SOURCE_INVENTORY),
        'layers': {
            'active': [duplicate, second],
            'pending': [claim('ADL 200', 'pending')],
            'closed': [claim('ADL 300', 'closed')],
        },
    }


def ardf_properties(oid=1, number='AA001'):
    return {
        'OBJECTID': oid,
        'Site': 'Test occurrence',
        'Commodities_main': 'Au, Ag',
        'Quad_250': 'AA',
        'Quad_63360': 'A-1',
        'Latitude': 60.5,
        'Longitude': -149.5,
        'Location': 'Near the test creek.',
        'Commodities_other': 'Cu',
        'Ore_minerals': 'Gold',
        'Gangue_minerals': 'Quartz',
        'Site_type': 'Occurrence',
        'Site_status': 'Inactive',
        'Production': 'None',
        'Generic_model': '',
        'Deposit_model': 'Lode gold',
        'Geologic_description': 'Quartz vein in metamorphic rock.',
        'Workings_exploration': 'Mapped.',
        'Additional_comments': '',
        'Expanded_References': '',
        'ARDF_no': number,
        'Reporter': 'USGS',
        'Last_report_date': '1/1/2020',
        'MRDS_no': 'M0001',
        'Age': 'Mesozoic',
        'Deposit_model_number': '',
        'Alteration': '',
        'Production_notes': '',
        'Reserves': '',
        'Primary_reference': 'Test (2020)',
        'State': 'AK',
        'District': 'Test district',
        'Host_rock': 'Schist',
        'Host_rock_age': 'Paleozoic',
        'Assoc_ign_rock': 'Granite',
        'Ign_rock_age': 'Mesozoic',
        'References_': '',
        'Reporter_affiliation': 'USGS',
        'Quadrangle': 'Test',
        'SYMBOL': 'Occurrence',
    }


def ardf_snapshot():
    return {
        'state': 'AK',
        'source_id': 'ardf',
        'retrieved': alaska.SNAPSHOT_DATE,
        'source': alaska.ARDF_SOURCE,
        'snapshot_contract': alaska.SNAPSHOT_CONTRACT,
        'source_inventory': copy.deepcopy(ARDF_SOURCE_INVENTORY),
        'n': 2,
        'features': [
            {'properties': ardf_properties(1, 'AA001'),
             'geometry': {'x': -149.5, 'y': 60.5}},
            {'properties': ardf_properties(3, 'AA002'),
             'geometry': {'x': -149.5, 'y': 60.5}},
        ],
    }


def write_valid_pmtiles(path, layers, description):
    attribution = ('U.S. Geological Survey Alaska Resource Data File'
                   if layers == ['ardf'] else
                   'Alaska Department of Natural Resources')
    name = os.path.basename(path)
    metadata = json.dumps({
        'name': name,
        'description': description,
        'attribution': attribution,
        'generator': 'tippecanoe v2.79.0',
        'generator_options': f'tippecanoe --output {name}',
        'vector_layers': [
            {'id': layer, 'fields': {
                key: ('Number' if key in alaska.NUMERIC_PM_FIELDS else 'String')
                for key in (
                    ('st', 'id', 'nm', 'g', 'g_status', 'group', 'ex', 'typ',
                     'typ_status', 'status', 'district', 'district_status')
                    if layer == 'ardf' else
                    ('st', 'system', 'source_oid', 'serial', 'status', 'source_status',
                     'acres', 'part', 'url', 'lon', 'lat'))
            }} for layer in layers
        ],
    }, separators=(',', ':')).encode()
    root = b'\x01'
    tile = b'nonempty-vector-tile'
    root_offset = 127
    metadata_offset = root_offset + len(root)
    tile_offset = metadata_offset + len(metadata)
    header = bytearray(127)
    header[:8] = b'PMTiles\x03'
    struct.pack_into(
        '<11Q', header, 8,
        root_offset, len(root), metadata_offset, len(metadata), 0, 0,
        tile_offset, len(tile), 1, 1, 1,
    )
    header[96] = 1
    header[97] = 1
    header[98] = 1
    header[99] = 1
    header[100] = 0
    header[101] = 14
    with open(path, 'wb') as output:
        output.write(header)
        output.write(root)
        output.write(metadata)
        output.write(tile)


def fake_id_inventory(_path, layers, expected_ids, _feature_properties=None):
    return {
        layer: {
            'status': 'complete_at_retrieval',
            'source_records': len(expected_ids[layer]),
            'maxzoom_feature_instances': len(expected_ids[layer]),
            'maxzoom_unique_tiled_ids': len(expected_ids[layer]),
            'ids_sha256': hashlib.sha256(json.dumps(
                sorted(expected_ids[layer]), separators=(',', ':')
            ).encode()).hexdigest(),
        }
        for layer in layers
    }


class AlaskaPmtilesTests(unittest.TestCase):
    COUNTS = {'active': 2, 'pending': 1, 'closed': 1}

    def test_claim_stream_preserves_and_reports_source_duplicates(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_CLAIM_COUNTS', self.COUNTS), \
                mock.patch.object(
                    alaska, 'EXPECTED_CLAIM_SOURCE_INVENTORY',
                    CLAIM_SOURCE_EXPECTED):
            paths = {status: os.path.join(directory, status + '.seq')
                     for status in alaska.CLAIM_STATUSES}
            stats = alaska._stream_claims(claims_snapshot(), paths)
            features = {}
            for status, path in paths.items():
                with open(path, encoding='utf-8') as source:
                    features[status] = [json.loads(line) for line in source]

        self.assertEqual(stats['n'], 4)
        self.assertEqual(stats['by_status'], self.COUNTS)
        self.assertEqual(stats['unique_serials'], 3)
        self.assertEqual(stats['repeated_serial_rows'], 1)
        self.assertEqual(stats['exact_duplicate_rows'], 1)
        self.assertEqual(stats['geometry'].get('zero_area_feature', 0), 0)
        self.assertEqual(stats['geometry'].get('zero_area_rings', 0), 0)
        identifiers = [feature['id'] for rows in features.values() for feature in rows]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(all(0 < identifier <= alaska.SAFE_INTEGER_MAX
                            for identifier in identifiers))
        self.assertEqual(features['active'][0]['properties']['parts'], 2)
        self.assertNotEqual(features['active'][0]['properties']['part'],
                            features['active'][1]['properties']['part'])
        self.assertEqual(features['closed'][0]['geometry']['type'], 'Polygon')
        self.assertEqual(features['active'][0]['properties']['lon'], -149.5)
        self.assertEqual(features['active'][0]['properties']['lat'], 60.5)

    def test_claim_stream_rejects_zero_area_geometry_instead_of_dropping_it(self):
        snapshot = claims_snapshot()
        snapshot['layers']['closed'][0]['geometry'] = {'rings': [[
            [-150.0, 60.0], [-149.5, 60.0], [-149.0, 60.0],
            [-150.0, 60.0],
        ]]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_CLAIM_COUNTS', self.COUNTS), \
                mock.patch.object(
                    alaska, 'EXPECTED_CLAIM_SOURCE_INVENTORY',
                    CLAIM_SOURCE_EXPECTED):
            paths = {status: os.path.join(directory, status + '.seq')
                     for status in alaska.CLAIM_STATUSES}
            with self.assertRaisesRegex(
                    ValueError, 'zero-area rings.*higher geometry precision'):
                alaska._stream_claims(snapshot, paths)

    def test_precision_partition_is_pinned_disjoint_and_source_exact(self):
        snapshot_value = claims_snapshot()
        geometry_hash = alaska._canonical_sha256(
            snapshot_value['layers']['active'][1]['geometry'])
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_CLAIM_COUNTS', self.COUNTS), \
                mock.patch.object(
                    alaska, 'EXPECTED_CLAIM_SOURCE_INVENTORY',
                    CLAIM_SOURCE_EXPECTED), \
                mock.patch.object(
                    alaska, 'EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256',
                    {'active': {2: geometry_hash}, 'closed': {}}):
            paths = {status: os.path.join(directory, status + '.seq')
                     for status in alaska.CLAIM_STATUSES}
            precision = {
                'active': os.path.join(directory, 'active-precision.seq'),
                'closed': os.path.join(directory, 'closed-precision.seq'),
            }
            stats = alaska._stream_claims(
                snapshot_value, paths, precision)
            with open(paths['active'], encoding='utf-8') as source:
                base_active = [json.loads(line) for line in source]
            with open(precision['active'], encoding='utf-8') as source:
                overflow = [json.loads(line) for line in source]
            inventory = alaska._source_objectid_delivery_inventory(stats)

        self.assertEqual(stats['base_by_status'], {
            'active': 1, 'pending': 1, 'closed': 1})
        self.assertEqual(stats['precision_n'], 1)
        self.assertEqual(stats['precision_source_objectids'], {
            'active': [2], 'closed': []})
        self.assertEqual(len(base_active), 1)
        self.assertEqual(len(overflow), 1)
        self.assertEqual(overflow[0]['properties']['source_oid'], 2)
        self.assertEqual(overflow[0]['geometry']['type'], 'Polygon')
        self.assertTrue(
            set(stats['_feature_ids']['active']).isdisjoint(
                stats['_precision_feature_ids'][
                    alaska.PRECISION_LAYERS['active']]))
        self.assertTrue(inventory['active']['disjoint_union_complete'])
        self.assertEqual(inventory['active']['source']['records'], 2)
        self.assertEqual(inventory['active']['base']['records'], 1)
        self.assertEqual(inventory['active']['precision']['records'], 1)

        changed = claims_snapshot()
        changed['layers']['active'][1]['geometry']['rings'][0][1][1] += 1e-8
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_CLAIM_COUNTS', self.COUNTS), \
                mock.patch.object(
                    alaska, 'EXPECTED_CLAIM_SOURCE_INVENTORY',
                    CLAIM_SOURCE_EXPECTED), \
                mock.patch.object(
                    alaska, 'EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256',
                    {'active': {2: geometry_hash}, 'closed': {}}):
            paths = {status: os.path.join(directory, status + '.seq')
                     for status in alaska.CLAIM_STATUSES}
            with self.assertRaisesRegex(
                    ValueError, 'geometry changed.*OBJECTID 2'):
                alaska._stream_claims(
                    changed, paths, {
                        'active': os.path.join(directory, 'active-precision.seq'),
                        'closed': os.path.join(directory, 'closed-precision.seq'),
                    })

    def test_esri_polygon_assigns_holes_and_separate_exteriors(self):
        outer = clockwise_square()
        hole_ccw = [
            [-149.8, 60.2], [-149.2, 60.2], [-149.2, 60.8],
            [-149.8, 60.8], [-149.8, 60.2],
        ]
        other = clockwise_square(-148.0, 60.0, -147.0, 61.0)
        geometry, facts = alaska._esri_polygon(
            {'rings': [outer, hole_ccw, other]}, 'geometry')
        self.assertEqual(geometry['type'], 'MultiPolygon')
        self.assertEqual(sorted(len(polygon) for polygon in geometry['coordinates']),
                         [1, 2])
        self.assertEqual(facts['parts'], 2)
        self.assertEqual(facts['zero_area_rings'], 0)
        with self.assertRaisesRegex(ValueError, 'not closed'):
            alaska._esri_polygon({'rings': [outer[:-1]]}, 'geometry')
        bad = copy.deepcopy(outer)
        bad[1][1] = 90
        with self.assertRaisesRegex(ValueError, 'outside'):
            alaska._esri_polygon({'rings': [bad]}, 'geometry')

        narrow = [[-150.00000001, 60.0], [-150.00000001, 60.00000008],
                  [-149.99999999, 60.00000008], [-149.99999999, 60.0],
                  [-150.00000001, 60.0]]
        preserved, _ = alaska._esri_polygon({'rings': [narrow]}, 'geometry')
        encoded = preserved['coordinates'][0]
        self.assertIn([-150.00000001, 60.00000008], encoded)
        self.assertIn([-149.99999999, 60.00000008], encoded)

    def test_representative_point_is_stable_and_avoids_polygon_holes(self):
        exterior = clockwise_square(-150.0, 60.0, -148.0, 62.0)
        hole = [
            [-149.5, 60.5], [-148.5, 60.5], [-148.5, 61.5],
            [-149.5, 61.5], [-149.5, 60.5],
        ]
        geometry = {'type': 'Polygon', 'coordinates': [exterior, hole]}
        first = alaska._representative_point(geometry, 'claim.geometry')
        second = alaska._representative_point(copy.deepcopy(geometry),
                                               'claim.geometry')
        self.assertEqual(first, second)
        self.assertTrue(alaska._point_in_ring(first, exterior))
        self.assertFalse(alaska._point_in_ring(first, hole))

        degenerate = {
            'type': 'Polygon',
            'coordinates': [[[-150.0, 60.0], [-149.5, 60.0],
                             [-149.0, 60.0], [-150.0, 60.0]]],
        }
        self.assertEqual(
            alaska._representative_point(degenerate, 'claim.geometry'),
            (-149.5, 60.0))

    def test_ardf_stream_checks_identity_order_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_ARDF_COUNT', 2), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_SOURCE_INVENTORY',
                    ARDF_SOURCE_EXPECTED), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS',
                    frozenset()):
            path = os.path.join(directory, 'ardf.seq')
            stats = alaska._stream_ardf(ardf_snapshot(), path)
            with open(path, encoding='utf-8') as source:
                features = [json.loads(line) for line in source]
        self.assertEqual(stats['n'], 2)
        self.assertEqual(stats['unique_objectids'], 2)
        self.assertEqual(stats['unique_ardf_numbers'], 2)
        self.assertEqual(features[0]['properties']['id'], 'AA001')
        self.assertEqual(features[0]['properties']['g'], 'Au, Ag')
        self.assertEqual(features[0]['properties']['group'], 0)
        self.assertEqual(features[0]['properties']['ex'], 0)
        self.assertEqual(features[0]['properties']['typ'], 'Occurrence')
        self.assertEqual(features[0]['properties']['typ_status'], 'reported')
        self.assertEqual(features[0]['properties']['g_status'], 'reported')
        self.assertEqual(features[0]['properties']['district_status'], 'reported')
        self.assertEqual(stats['source_blank_fields'], {})
        self.assertEqual(features[0]['id'], alaska._feature_id('ardf', 'AA001'))

        duplicate = ardf_snapshot()
        duplicate['features'][1]['properties']['ARDF_no'] = 'AA001 '
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_ARDF_COUNT', 2), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_SOURCE_INVENTORY',
                    ARDF_SOURCE_EXPECTED), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS',
                    frozenset()):
            with self.assertRaisesRegex(ValueError, 'duplicate normalized ARDF_no'):
                alaska._stream_ardf(duplicate, os.path.join(directory, 'ardf.seq'))

    def test_ardf_source_blanks_are_explicit_and_identity_pinned(self):
        snapshot = ardf_snapshot()
        snapshot['features'][0]['properties']['Site_type'] = ''
        snapshot['features'][0]['properties']['Commodities_main'] = ''
        snapshot['features'][0]['properties']['District'] = ''
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_ARDF_COUNT', 2), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_SOURCE_INVENTORY',
                    ARDF_SOURCE_EXPECTED), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS',
                    frozenset((1,))):
            path = os.path.join(directory, 'ardf.seq')
            stats = alaska._stream_ardf(snapshot, path)
            with open(path, encoding='utf-8') as source:
                feature = json.loads(next(source))
        properties = feature['properties']
        for field in ('g', 'typ', 'district'):
            self.assertEqual(properties[field], alaska.SOURCE_VALUE_MISSING)
        self.assertEqual(properties['g_status'], 'source_blank')
        self.assertEqual(properties['typ_status'], 'source_blank')
        self.assertEqual(properties['district_status'], 'source_blank')
        self.assertEqual(stats['source_blank_fields'], {
            'commodities_main': 1, 'district': 1, 'site_type': 1})
        self.assertEqual(stats['source_blank_site_type_objectids'], [1])

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alaska, 'EXPECTED_ARDF_COUNT', 2), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_SOURCE_INVENTORY',
                    ARDF_SOURCE_EXPECTED), \
                mock.patch.object(
                    alaska, 'EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS',
                    frozenset((3,))):
            with self.assertRaisesRegex(
                    ValueError, 'blank Site_type OBJECTID inventory changed'):
                alaska._stream_ardf(
                    snapshot, os.path.join(directory, 'ardf.seq'))

    def test_ardf_maxzoom_semantics_are_exhaustive_and_exact(self):
        reported = {
            'st': 'AK', 'id': 'AA001', 'nm': 'Reported', 'g': 'Au',
            'g_status': 'reported', 'group': 0, 'ex': 0,
            'typ': 'Occurrence', 'typ_status': 'reported',
            'status': 'Inactive', 'district': 'Test',
            'district_status': 'reported',
        }
        blank = dict(reported, id='AA002', nm='Blank',
                     g=alaska.SOURCE_VALUE_MISSING,
                     g_status='source_blank',
                     typ=alaska.SOURCE_VALUE_MISSING,
                     typ_status='source_blank',
                     district=alaska.SOURCE_VALUE_MISSING,
                     district_status='source_blank')
        expected = {11: reported, 22: blank}
        features = [
            {'id': 11, 'properties': dict(reported)},
            {'id': 22, 'properties': dict(blank)},
            {'id': 22, 'properties': dict(blank)},
        ]
        with mock.patch.object(
                alaska, '_pmtiles_maxzoom_features', return_value=features):
            result = alaska._ardf_semantic_inventory('ardf.pmtiles', expected)
        self.assertEqual(result['unique_feature_ids'], 2)
        self.assertEqual(result['maxzoom_feature_instances'], 3)

        wrong = copy.deepcopy(features)
        wrong[1]['properties']['typ'] = 'Occurrence'
        with mock.patch.object(
                alaska, '_pmtiles_maxzoom_features', return_value=wrong):
            with self.assertRaisesRegex(
                    ValueError, 'source_blank lacks the explicit sentinel'):
                alaska._ardf_semantic_inventory('ardf.pmtiles', expected)

        changed = copy.deepcopy(features)
        changed[0]['properties']['district'] = 'Other'
        with mock.patch.object(
                alaska, '_pmtiles_maxzoom_features', return_value=changed):
            with self.assertRaisesRegex(
                    ValueError, 'changed browser properties'):
                alaska._ardf_semantic_inventory('ardf.pmtiles', expected)

    def test_pmtiles_validation_requires_exact_layers_fields_and_counts(self):
        description = alaska._description(
            'ardf', {'ardf': 2}, 'a' * 64)
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.pmtiles')
            write_valid_pmtiles(valid, ['ardf'], description)
            metadata = alaska._validate_pmtiles(
                valid,
                {'ardf': (
                    'st', 'id', 'nm', 'g', 'g_status', 'group', 'ex', 'typ',
                    'typ_status', 'status', 'district', 'district_status')},
                description)
            self.assertEqual(metadata['embedded']['counts'], {'ardf': 2})
            wrong = os.path.join(directory, 'wrong.pmtiles')
            write_valid_pmtiles(wrong, ['ardf'], alaska._description(
                'ardf', {'ardf': 1}, 'a' * 64))
            with self.assertRaisesRegex(ValueError, 'does not match input'):
                alaska._validate_pmtiles(
                    wrong, {'ardf': ('st', 'id')}, description)

    def test_pmtiles_validation_rejects_string_typed_numeric_fields(self):
        description = alaska._description('ardf', {'ardf': 2}, 'a' * 64)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'ardf.pmtiles')
            write_valid_pmtiles(path, ['ardf'], description)
            with open(path, 'r+b') as artifact:
                content = artifact.read()
                content = content.replace(b'"group":"Number"',
                                          b'"group":"String"')
                artifact.seek(0)
                artifact.write(content)
                artifact.truncate()
            with self.assertRaisesRegex(ValueError, 'nonnumeric'):
                alaska._validate_pmtiles(
                    path,
                    {'ardf': (
                        'st', 'id', 'nm', 'g', 'g_status', 'group', 'ex',
                        'typ', 'typ_status', 'status', 'district',
                        'district_status')},
                    description)

    def test_feature_id_inventory_requires_every_source_record(self):
        layers = {'active': ('st', 'serial')}
        expected_ids = {'active': [11, 22]}
        complete = {
            'maxzoom_feature_ids': {'active': [11, 22]},
            'maxzoom_feature_instances': {'active': 3},
        }
        with mock.patch.object(
                alaska, '_strict_pmtiles_header', return_value=complete) as header:
            inventory = alaska._feature_id_inventory(
                'claims.pmtiles', layers, expected_ids)
        header.assert_called_once_with(
            'claims.pmtiles', ['active'], layers,
            verify_feature_properties=True, collect_feature_ids=True)
        self.assertEqual(inventory['active']['source_records'], 2)
        self.assertEqual(inventory['active']['maxzoom_unique_tiled_ids'], 2)
        self.assertEqual(inventory['active']['maxzoom_feature_instances'], 3)

        incomplete = {
            'maxzoom_feature_ids': {'active': [11]},
            'maxzoom_feature_instances': {'active': 1},
        }
        with mock.patch.object(
                alaska, '_strict_pmtiles_header', return_value=incomplete):
            with self.assertRaisesRegex(
                    ValueError, 'feature-ID reconciliation failed.*missing='):
                alaska._feature_id_inventory(
                    'claims.pmtiles', layers, expected_ids)

    def test_build_publishes_only_valid_pmtiles_without_touching_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            os.makedirs(site)
            claims_path = os.path.join(directory, 'claims.json')
            ardf_path = os.path.join(directory, 'ardf.json')
            with open(claims_path, 'w', encoding='utf-8') as output:
                json.dump(claims_snapshot(), output, separators=(',', ':'))
            with open(ardf_path, 'w', encoding='utf-8') as output:
                json.dump(ardf_snapshot(), output, separators=(',', ':'))
            before = {path: file_sha256(path)
                      for path in (claims_path, ardf_path)}
            claims_out = os.path.join(
                site, 'data', 'tiles', 'claims', 'ak-state.pmtiles')
            precision_out = os.path.join(
                site, 'data', 'tiles', 'claims',
                'ak-state-precision.pmtiles')
            ardf_out = os.path.join(
                site, 'data', 'tiles', 'national', 'ardf.pmtiles')
            commands = []

            def fake_tippecanoe(command, check, env, cwd):
                self.assertTrue(check)
                self.assertEqual(env['TIPPECANOE_MAX_THREADS'], '1')
                self.assertFalse(any(directory in value for value in command))
                self.assertIn('--no-feature-limit', command)
                self.assertIn('--no-tile-size-limit', command)
                self.assertNotIn('--drop-densest-as-needed', command)
                commands.append(command)
                output = os.path.join(
                    cwd, command[command.index('--output') + 1])
                layers = [command[index + 1].split(':', 1)[0]
                          for index, value in enumerate(command) if value == '-L']
                description = next(value.split('=', 1)[1] for value in command
                                   if value.startswith('--description='))
                write_valid_pmtiles(output, layers, description)

            with mock.patch.object(alaska, 'SITE', site), \
                    mock.patch.object(alaska, 'CLAIMS_OUT', claims_out), \
                    mock.patch.object(
                        alaska, 'CLAIMS_PRECISION_OUT', precision_out), \
                    mock.patch.object(alaska, 'ARDF_OUT', ardf_out), \
                    mock.patch.object(alaska, 'EXPECTED_CLAIM_COUNTS', self.COUNTS), \
                    mock.patch.object(
                        alaska, 'EXPECTED_CLAIM_SOURCE_INVENTORY',
                        CLAIM_SOURCE_EXPECTED), \
                    mock.patch.object(
                        alaska, 'EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256',
                        {'active': {}, 'closed': {}}), \
                    mock.patch.object(alaska, 'EXPECTED_ARDF_COUNT', 2), \
                    mock.patch.object(
                        alaska, 'EXPECTED_ARDF_SOURCE_INVENTORY',
                        ARDF_SOURCE_EXPECTED), \
                    mock.patch.object(
                        alaska, 'EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS',
                        frozenset()), \
                    mock.patch.object(
                        alaska, 'EXPECTED_CLAIMS_STAGING_SHA256',
                        before[claims_path]), \
                    mock.patch.object(
                        alaska, 'EXPECTED_ARDF_STAGING_SHA256',
                        before[ardf_path]), \
                    mock.patch.object(alaska.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(alaska.subprocess, 'run', side_effect=fake_tippecanoe), \
                    mock.patch.object(
                        alaska, '_feature_id_inventory', side_effect=fake_id_inventory), \
                    mock.patch.object(
                        alaska, '_ardf_semantic_inventory', return_value={
                            'status': 'complete_exact_browser_properties',
                            'unique_feature_ids': 2,
                            'maxzoom_feature_instances': 2,
                            'browser_fields': list(alaska.ARDF_BROWSER_FIELDS),
                        }):
                result = alaska.build(
                    claims_path, ardf_path, update_manifest=False)

            self.assertEqual(len(commands), 3)
            claims_command, precision_command, ardf_command = commands
            self.assertIn(
                f'--maximum-zoom={alaska.CLAIMS_MAXZOOM}', claims_command)
            self.assertIn(
                f'--base-zoom={alaska.CLAIMS_MAXZOOM}', claims_command)
            self.assertIn(
                f'--maximum-zoom={alaska.CLAIMS_PRECISION_MAXZOOM}',
                precision_command)
            self.assertIn(
                f'--base-zoom={alaska.CLAIMS_PRECISION_MAXZOOM}',
                precision_command)
            self.assertIn(
                f'--maximum-zoom={alaska.ARDF_MAXZOOM}', ardf_command)
            self.assertIn(
                f'--base-zoom={alaska.ARDF_MAXZOOM}', ardf_command)
            self.assertEqual(result['artifacts']['alaska_state_claims']['n'], 4)
            self.assertEqual(
                result['artifacts']['alaska_state_claims']['activation_zoom'], 8)
            self.assertEqual(result['artifacts']['ardf']['n'], 2)
            self.assertEqual(
                result['artifacts']['alaska_state_claims'][
                    'precision_overflow']['by_status'],
                {'active': 0, 'closed': 0})
            self.assertEqual(
                result['artifacts']['alaska_state_claims'][
                    'precision_overflow']['activation_zoom'],
                alaska.CLAIMS_PRECISION_MAXZOOM)
            self.assertFalse(result['manifest_updated'])
            self.assertEqual(result['output_mode'], 'public_candidate')
            self.assertRegex(result['artifact_set_sha256'], r'^[0-9a-f]{64}$')
            self.assertRegex(
                result['path_independent_metadata_set_sha256'],
                r'^[0-9a-f]{64}$')
            self.assertTrue(os.path.isfile(claims_out))
            self.assertTrue(os.path.isfile(precision_out))
            self.assertTrue(os.path.isfile(ardf_out))
            self.assertEqual(stat.S_IMODE(os.stat(claims_out).st_mode), 0o644)
            self.assertEqual(
                stat.S_IMODE(os.stat(precision_out).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.stat(ardf_out).st_mode), 0o644)
            self.assertEqual(
                before,
                {path: file_sha256(path)
                 for path in (claims_path, ardf_path)})
            browser_json = [
                os.path.join(root, name) for root, _, files in os.walk(site)
                for name in files if name.endswith(('.json', '.geojson', '.geojsonseq'))
            ]
            self.assertEqual(browser_json, [])

    def test_private_output_directory_cannot_target_browser_tree_or_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            os.makedirs(site)
            private = os.path.join(directory, 'private')
            with mock.patch.object(alaska, 'SITE', site):
                self.assertEqual(
                    alaska._private_output_directory(private),
                    os.path.realpath(private))
                with self.assertRaisesRegex(ValueError, 'outside site'):
                    alaska._private_output_directory(
                        os.path.join(site, 'private-build'))
            with self.assertRaisesRegex(ValueError, 'cannot be combined'):
                with mock.patch.object(
                        alaska.shutil, 'which', return_value='/tippecanoe'):
                    alaska.build(
                        os.path.join(directory, 'missing-claims.json'),
                        os.path.join(directory, 'missing-ardf.json'),
                        update_manifest=True, private_output_dir=private)

    def test_path_independent_metadata_rejects_private_generator_path(self):
        description = alaska._description('ardf', {'ardf': 2}, 'a' * 64)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'ardf.pmtiles')
            write_valid_pmtiles(path, ['ardf'], description)
            result = alaska._path_independent_metadata(
                path, description,
                'U.S. Geological Survey Alaska Resource Data File')
            self.assertEqual(
                result['status'],
                'complete_path_free_reproducible_metadata')
            metadata = alaska._pmtiles_json_metadata(path)
            metadata['generator_options'] = (
                'tippecanoe /private/staging/ardf.geojsonseq')
            with mock.patch.object(
                    alaska, '_pmtiles_json_metadata', return_value=metadata):
                with self.assertRaisesRegex(ValueError, 'path-free'):
                    alaska._path_independent_metadata(
                        path, description,
                        'U.S. Geological Survey Alaska Resource Data File')

    def test_invalid_second_archive_never_replaces_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            claims_path = os.path.join(directory, 'claims.json')
            ardf_path = os.path.join(directory, 'ardf.json')
            with open(claims_path, 'w', encoding='utf-8') as output:
                json.dump(claims_snapshot(), output)
            with open(ardf_path, 'w', encoding='utf-8') as output:
                json.dump(ardf_snapshot(), output)
            claims_staging_sha = file_sha256(claims_path)
            ardf_staging_sha = file_sha256(ardf_path)
            claims_out = os.path.join(site, 'claims.pmtiles')
            precision_out = os.path.join(site, 'precision.pmtiles')
            ardf_out = os.path.join(site, 'ardf.pmtiles')
            os.makedirs(site)
            with open(claims_out, 'wb') as output:
                output.write(b'existing-claims')
            with open(precision_out, 'wb') as output:
                output.write(b'existing-precision')
            with open(ardf_out, 'wb') as output:
                output.write(b'existing-ardf')
            calls = 0

            def fake_tippecanoe(command, check, env, cwd):
                nonlocal calls
                self.assertEqual(env['TIPPECANOE_MAX_THREADS'], '1')
                calls += 1
                output = os.path.join(
                    cwd, command[command.index('--output') + 1])
                if calls == 2:
                    with open(output, 'wb') as target:
                        target.write(b'PMTiles\x03truncated')
                    return
                layers = [command[index + 1].split(':', 1)[0]
                          for index, value in enumerate(command) if value == '-L']
                description = next(value.split('=', 1)[1] for value in command
                                   if value.startswith('--description='))
                write_valid_pmtiles(output, layers, description)

            with mock.patch.object(alaska, 'SITE', site), \
                    mock.patch.object(alaska, 'CLAIMS_OUT', claims_out), \
                    mock.patch.object(
                        alaska, 'CLAIMS_PRECISION_OUT', precision_out), \
                    mock.patch.object(alaska, 'ARDF_OUT', ardf_out), \
                    mock.patch.object(alaska, 'EXPECTED_CLAIM_COUNTS', self.COUNTS), \
                    mock.patch.object(
                        alaska, 'EXPECTED_CLAIM_SOURCE_INVENTORY',
                        CLAIM_SOURCE_EXPECTED), \
                    mock.patch.object(
                        alaska, 'EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256',
                        {'active': {}, 'closed': {}}), \
                    mock.patch.object(alaska, 'EXPECTED_ARDF_COUNT', 2), \
                    mock.patch.object(
                        alaska, 'EXPECTED_ARDF_SOURCE_INVENTORY',
                        ARDF_SOURCE_EXPECTED), \
                    mock.patch.object(
                        alaska, 'EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS',
                        frozenset()), \
                    mock.patch.object(
                        alaska, 'EXPECTED_CLAIMS_STAGING_SHA256',
                        claims_staging_sha), \
                    mock.patch.object(
                        alaska, 'EXPECTED_ARDF_STAGING_SHA256',
                        ardf_staging_sha), \
                    mock.patch.object(alaska.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(alaska.subprocess, 'run', side_effect=fake_tippecanoe):
                with self.assertRaisesRegex(ValueError, 'not a PMTiles v3'):
                    alaska.build(claims_path, ardf_path, update_manifest=False)
            with open(claims_out, 'rb') as source:
                self.assertEqual(source.read(), b'existing-claims')
            with open(precision_out, 'rb') as source:
                self.assertEqual(source.read(), b'existing-precision')
            with open(ardf_out, 'rb') as source:
                self.assertEqual(source.read(), b'existing-ardf')

    def test_rejects_staging_inside_browser_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            os.makedirs(site)
            path = os.path.join(site, 'claims.json')
            with open(path, 'w') as output:
                output.write('{}')
            with mock.patch.object(alaska, 'SITE', site):
                with self.assertRaisesRegex(ValueError, 'outside site'):
                    alaska._private_staging_path(path, 'claims')

    def test_manifest_merge_preserves_latest_unrelated_sections_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, 'manifest.json')
            original = {
                'claims': {},
                'sites': {},
                'national_baselines': {'geology': {'sha256': 'latest'}},
                'concurrent_migration': {'kept': True},
            }
            with open(manifest_path, 'w', encoding='utf-8') as output:
                json.dump(original, output)
            os.chmod(manifest_path, 0o640)
            entries = {
                'alaska_state_claims': {'file': 'ak-state.pmtiles'},
                'ardf': {'file': 'ardf.pmtiles'},
            }
            with mock.patch.object(alaska, 'MANIFEST', manifest_path):
                alaska._stamp_manifest(entries)
            with open(manifest_path, encoding='utf-8') as source:
                merged = json.load(source)
            self.assertEqual(merged['claims'], {})
            self.assertEqual(merged['sites'], {})
            self.assertEqual(merged['national_baselines']['geology'],
                             {'sha256': 'latest'})
            self.assertEqual(merged['national_baselines']['ardf'], entries['ardf'])
            self.assertEqual(merged['concurrent_migration'], {'kept': True})
            self.assertEqual(stat.S_IMODE(os.stat(manifest_path).st_mode), 0o640)

    def test_bundle_and_manifest_roll_back_together_on_base_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, 'manifest.json')
            claims = os.path.join(directory, 'claims.pmtiles')
            ardf = os.path.join(directory, 'ardf.pmtiles')
            precision = os.path.join(directory, 'precision.pmtiles')
            pending_claims = os.path.join(directory, 'pending-claims')
            pending_precision = os.path.join(directory, 'pending-precision')
            pending_ardf = os.path.join(directory, 'pending-ardf')
            for path, content in (
                    (manifest, b'{"old":true}'),
                    (claims, b'old-claims'), (ardf, b'old-ardf'),
                    (precision, b'old-precision'),
                    (pending_claims, b'new-claims'),
                    (pending_precision, b'new-precision'),
                    (pending_ardf, b'new-ardf')):
                with open(path, 'wb') as output:
                    output.write(content)

            def interrupted_stamp(_entries):
                with open(manifest, 'wb') as output:
                    output.write(b'{"partial":true}')
                raise KeyboardInterrupt()

            with mock.patch.object(alaska, 'MANIFEST', manifest), \
                    mock.patch.object(
                        alaska, '_stamp_manifest', side_effect=interrupted_stamp):
                with self.assertRaises(KeyboardInterrupt):
                    alaska._publish_bundle([
                        (pending_claims, claims),
                        (pending_precision, precision),
                        (pending_ardf, ardf),
                    ], {'alaska_state_claims': {}, 'ardf': {}})

            for path, expected in (
                    (manifest, b'{"old":true}'),
                    (claims, b'old-claims'),
                    (precision, b'old-precision'),
                    (ardf, b'old-ardf')):
                with open(path, 'rb') as source:
                    self.assertEqual(source.read(), expected)
            self.assertFalse(os.path.exists(pending_claims))
            self.assertFalse(os.path.exists(pending_precision))
            self.assertFalse(os.path.exists(pending_ardf))
            self.assertEqual([
                name for name in os.listdir(directory) if 'rollback-' in name
            ], [])

    def test_grace_interruption_removes_all_prepared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = []
            for name in ('claims', 'precision', 'ardf'):
                pending = os.path.join(directory, 'pending-' + name)
                destination = os.path.join(directory, name + '.pmtiles')
                with open(pending, 'wb') as output:
                    output.write(b'candidate')
                prepared.append((pending, destination))
            with mock.patch.object(
                    alaska.time, 'sleep', side_effect=KeyboardInterrupt), \
                    mock.patch.object(alaska, '_publish_bundle') as publish:
                with self.assertRaises(KeyboardInterrupt):
                    alaska._publish_after_grace(
                        prepared, {'alaska_state_claims': {}, 'ardf': {}}, True)
            publish.assert_not_called()
            self.assertFalse(any(os.path.exists(path) for path, _ in prepared))
            self.assertFalse(any(os.path.exists(path) for _, path in prepared))


if __name__ == '__main__':
    unittest.main()
