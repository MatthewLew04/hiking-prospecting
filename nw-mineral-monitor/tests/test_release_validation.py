import copy
import gzip
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import validate_national
import build_national_grade_evidence as national_grades
import build_national_target_scoring_evidence as national_scoring
import state_registry


def _varint(value):
    out = bytearray()
    while value > 0x7f:
        out.append((value & 0x7f) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _minimal_pmtiles(path, valid_tile=True, addressed=1, *,
                     properties=None, empty_semantics=False,
                     bounds=(-120.0, 35.0, -114.0, 42.0),
                     layer_name='claims'):
    properties = {'st': 'NV'} if properties is None else properties
    feature = _varint(8) + _varint(1)
    key_values = b''
    if not empty_semantics:
        tags = bytearray()
        for index, (key, value) in enumerate(properties.items()):
            key_bytes = key.encode()
            key_values += b'\x1a' + _varint(len(key_bytes)) + key_bytes
            if isinstance(value, bool):
                encoded_value = b'\x38' + _varint(int(value))
            elif isinstance(value, int):
                if value >= 0:
                    encoded_value = b'\x28' + _varint(value)
                else:
                    encoded_value = b'\x30' + _varint((value << 1) ^ (value >> 63))
            elif isinstance(value, float):
                encoded_value = b'\x19' + struct.pack('<d', value)
            else:
                value_bytes = str(value).encode()
                encoded_value = b'\x0a' + _varint(len(value_bytes)) + value_bytes
            key_values += b'\x22' + _varint(len(encoded_value)) + encoded_value
            tags += _varint(index) + _varint(index)
        # Point geometry: MoveTo(1), zig-zag encoded dx=1, dy=1.
        geometry = b'\x09\x02\x02'
        feature += (b'\x12' + _varint(len(tags)) + tags + b'\x18\x01' +
                    b'\x22' + _varint(len(geometry)) + geometry)
    layer_bytes = layer_name.encode()
    layer = (b'\x78\x02' + b'\x0a' + _varint(len(layer_bytes)) + layer_bytes + b'\x12' +
             _varint(len(feature)) + feature + key_values + b'\x28\x80\x20')
    mvt = b'\x1a' + _varint(len(layer)) + layer
    tile_compression = 2 if valid_tile else 1
    tile = gzip.compress(mvt, mtime=0) if valid_tile else b'\x00'
    root_raw = b'\x01\x00\x01' + _varint(len(tile)) + b'\x01'
    root = gzip.compress(root_raw, mtime=0)
    metadata = gzip.compress(json.dumps({
        'vector_layers': [{'id': layer_name, 'fields': {
            key: ('Boolean' if isinstance(value, bool) else
                  'Number' if isinstance(value, (int, float)) else 'String')
            for key, value in properties.items()}}]
    }).encode(), mtime=0)
    root_offset = 127
    metadata_offset = root_offset + len(root)
    tile_offset = metadata_offset + len(metadata)
    header = bytearray(127)
    header[:8] = b'PMTiles\x03'
    struct.pack_into('<11Q', header, 8, root_offset, len(root),
                     metadata_offset, len(metadata), tile_offset, 0,
                     tile_offset, len(tile), addressed, 1, 1)
    header[96:102] = bytes((1, 2, tile_compression, 1, 0, 0))
    struct.pack_into('<4i', header, 102,
                     *(round(value * 10_000_000) for value in bounds))
    header[118] = 0
    struct.pack_into('<2i', header, 119,
                     round(((bounds[0] + bounds[2]) / 2) * 10_000_000),
                     round(((bounds[1] + bounds[3]) / 2) * 10_000_000))
    with open(path, 'wb') as artifact:
        artifact.write(header + root + metadata + tile)


def _alaska_pmtiles(path, layers, fields, description, *, string_numeric=None,
                    feature_counts=None, geometry_type=1, maxzoom=0,
                    feature_id_offset=0, feature_properties=None):
    counts = description['counts']
    tile_counts = counts if feature_counts is None else feature_counts
    encoded_layers = []
    for layer_index, layer_id in enumerate(layers):
        keys_and_values = bytearray()
        layer_fields = list(fields[layer_id])
        for field in layer_fields:
            key = field.encode()
            keys_and_values += b'\x1a' + _varint(len(key)) + key

        default_properties = {}
        for field in layer_fields:
            if field in {'lon', 'lat'} and field != string_numeric:
                value = -150.0 if field == 'lon' else 60.0
            elif field in {'group', 'ex', 'source_oid'} and field != string_numeric:
                value = 0
            else:
                value = 'AK' if field == 'st' else f'{field}-value'
            default_properties[field] = value

        rows = []
        for row_index in range(tile_counts[layer_id]):
            values = dict(default_properties)
            if feature_properties is not None:
                overrides = feature_properties(layer_id, row_index, dict(values))
                if not isinstance(overrides, dict):
                    raise TypeError('feature_properties must return an object')
                values.update(overrides)
            rows.append(values)

        encoded_values = []
        value_indexes = {}
        row_tags = []
        for values in rows:
            tags = bytearray()
            for field_index, field in enumerate(layer_fields):
                value = values[field]
                identity = (type(value), value)
                value_index = value_indexes.get(identity)
                if value_index is None:
                    value_index = len(encoded_values)
                    value_indexes[identity] = value_index
                    if isinstance(value, bool):
                        encoded = b'\x38' + _varint(int(value))
                    elif isinstance(value, int):
                        encoded = b'\x28' + _varint(value)
                    elif isinstance(value, float):
                        encoded = b'\x19' + struct.pack('<d', value)
                    else:
                        text = str(value).encode()
                        encoded = b'\x0a' + _varint(len(text)) + text
                    encoded_values.append(encoded)
                tags += _varint(field_index) + _varint(value_index)
            row_tags.append(tags)
        for value in encoded_values:
            keys_and_values += b'\x22' + _varint(len(value)) + value

        features = bytearray()
        for row_index, tags in enumerate(row_tags):
            feature_id = (feature_id_offset + (layer_index + 1) * 1000 +
                          row_index + 1)
            if geometry_type == 3:
                # One valid MVT polygon ring: MoveTo, three LineTo vertices,
                # then ClosePath. Coordinates are delta/zig-zag encoded.
                geometry = b'\x09\x02\x02\x1a\x12\x00\x00\x12\x11\x00\x0f'
            else:
                geometry = b'\x09\x02\x02'
            feature = (b'\x08' + _varint(feature_id) +
                       b'\x12' + _varint(len(tags)) + tags +
                       b'\x18' + _varint(geometry_type) +
                       b'\x22' + _varint(len(geometry)) + geometry)
            features += b'\x12' + _varint(len(feature)) + feature
        layer_name = layer_id.encode()
        layer = (b'\x78\x02\x0a' + _varint(len(layer_name)) + layer_name +
                 features + keys_and_values + b'\x28\x80\x20')
        encoded_layers.append(b'\x1a' + _varint(len(layer)) + layer)
    mvt = b''.join(encoded_layers)
    tile = gzip.compress(mvt, mtime=0)
    first_tile_id = (4 ** maxzoom - 1) // 3
    root_raw = (b'\x01' + _varint(first_tile_id) + b'\x01' +
                _varint(len(tile)) + b'\x01')
    root = gzip.compress(root_raw, mtime=0)
    metadata = gzip.compress(json.dumps({
        'description': json.dumps(description, sort_keys=True, separators=(',', ':')),
        'vector_layers': [
            {'id': layer_id,
             'fields': {
                 field: ('Number' if field in {'lon', 'lat', 'group', 'ex',
                                                'source_oid'} and
                         field != string_numeric else 'String')
                 for field in fields[layer_id]
             }}
            for layer_id in layers
        ],
    }).encode(), mtime=0)
    root_offset = 127
    metadata_offset = root_offset + len(root)
    tile_offset = metadata_offset + len(metadata)
    header = bytearray(127)
    header[:8] = b'PMTiles\x03'
    struct.pack_into('<11Q', header, 8, root_offset, len(root),
                     metadata_offset, len(metadata), tile_offset, 0,
                     tile_offset, len(tile), 1, 1, 1)
    header[96:102] = bytes((1, 2, 2, 1, 0, maxzoom))
    struct.pack_into('<4i', header, 102, -170_000_0000, 500_000_000,
                     -130_000_0000, 720_000_000)
    header[118] = maxzoom
    struct.pack_into('<2i', header, 119, -150_000_0000, 600_000_000)
    with open(path, 'wb') as artifact:
        artifact.write(header + root + metadata + tile)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as artifact:
        digest.update(artifact.read())
    return digest.hexdigest()


_ARDF_BROWSER_FIELDS = [
    'st', 'id', 'nm', 'g', 'g_status', 'group', 'ex', 'typ',
    'typ_status', 'status', 'district', 'district_status',
]
_ARDF_BLANK_TYPE_IDS = tuple(sorted(validate_national.ARDF_BLANK_SITE_TYPE_IDS))
_ARDF_BLANK_G_ROWS = frozenset((0, *range(6, 80)))
_ARDF_BLANK_TYPE_ROWS = frozenset(range(len(_ARDF_BLANK_TYPE_IDS)))
_ARDF_BLANK_DISTRICT_ROWS = frozenset((1, *range(80, 479)))


def _fixture_ardf_feature_properties(layer, row_index, defaults):
    if layer != 'ardf':
        return {}
    synthetic_id = (
        f'A{chr(ord("A") + row_index // 1000)}{row_index % 1000:03d}')
    ardf_id = (_ARDF_BLANK_TYPE_IDS[row_index]
               if row_index < len(_ARDF_BLANK_TYPE_IDS) else synthetic_id)
    values = {
        'st': 'AK',
        'id': ardf_id,
        'nm': f'Occurrence {row_index}',
        'g': 'Au',
        'g_status': validate_national.ARDF_SOURCE_VALUE_REPORTED,
        'group': row_index % 6,
        'ex': row_index % 2,
        'typ': 'Occurrence',
        'typ_status': validate_national.ARDF_SOURCE_VALUE_REPORTED,
        'status': 'Reported occurrence',
        'district': 'Fixture district',
        'district_status': validate_national.ARDF_SOURCE_VALUE_REPORTED,
    }
    if row_index in _ARDF_BLANK_G_ROWS:
        values['g'] = validate_national.ARDF_SOURCE_VALUE_MISSING
        values['g_status'] = validate_national.ARDF_SOURCE_VALUE_BLANK
    if row_index in _ARDF_BLANK_TYPE_ROWS:
        values['typ'] = validate_national.ARDF_SOURCE_VALUE_MISSING
        values['typ_status'] = validate_national.ARDF_SOURCE_VALUE_BLANK
    if row_index in _ARDF_BLANK_DISTRICT_ROWS:
        values['district'] = validate_national.ARDF_SOURCE_VALUE_MISSING
        values['district_status'] = validate_national.ARDF_SOURCE_VALUE_BLANK
    return values


def _write_fixture_ardf(path, description, *, missing_property=None,
                        string_numeric=None, mutate=None):
    fields = [field for field in _ARDF_BROWSER_FIELDS
              if field != missing_property]

    def feature_properties(layer, row_index, defaults):
        values = _fixture_ardf_feature_properties(layer, row_index, defaults)
        if mutate is not None:
            mutate(row_index, values)
        return values

    _alaska_pmtiles(
        path, ['ardf'], {'ardf': fields}, description,
        string_numeric=string_numeric, maxzoom=13,
        feature_properties=feature_properties)


def _alaska_baseline_fixtures(site, *, missing_claim_property=None):
    retrieved = validate_national.ALASKA_SNAPSHOT_DATE
    claims_staging = '1' * 64
    ardf_staging = validate_national.ARDF_EXPECTED_STAGING_SHA256
    base_counts = {'active': 2, 'pending': 1, 'closed': 3}
    precision_status_counts = {'active': 6, 'closed': 18}
    claims_counts = {
        'active': base_counts['active'] + precision_status_counts['active'],
        'pending': base_counts['pending'],
        'closed': base_counts['closed'] + precision_status_counts['closed'],
    }
    claim_layers = ['active', 'pending', 'closed']
    claim_properties = {
        layer: ['st', 'system', 'source_oid', 'serial', 'status', 'source_status',
                'acres', 'part', 'url', 'lon', 'lat'] for layer in claim_layers
    }
    if missing_claim_property:
        for layer in claim_layers:
            claim_properties[layer].remove(missing_claim_property)
    claims_path = os.path.join(site, 'data', 'tiles', 'claims', 'ak-state.pmtiles')
    ardf_path = os.path.join(site, 'data', 'tiles', 'national', 'ardf.pmtiles')
    os.makedirs(os.path.dirname(claims_path), exist_ok=True)
    os.makedirs(os.path.dirname(ardf_path), exist_ok=True)
    _alaska_pmtiles(claims_path, claim_layers, claim_properties, {
        'schema': 'nwmm-alaska-pmtiles-v1',
        'dataset': 'alaska_state_claims',
        'snapshot': retrieved,
        'counts': base_counts,
        'staging_sha256': claims_staging,
    }, geometry_type=3, maxzoom=13)
    precision_layers = ['active_precision', 'closed_precision']
    precision_counts = {
        'active_precision': precision_status_counts['active'],
        'closed_precision': precision_status_counts['closed'],
    }
    precision_properties = {
        layer: list(next(iter(claim_properties.values())))
        for layer in precision_layers}
    precision_path = os.path.join(
        site, 'data', 'tiles', 'claims', 'ak-state-precision.pmtiles')
    _alaska_pmtiles(precision_path, precision_layers, precision_properties, {
        'schema': 'nwmm-alaska-pmtiles-v1',
        'dataset': 'alaska_state_claims_precision',
        'snapshot': retrieved,
        'counts': precision_counts,
        'staging_sha256': claims_staging,
    }, geometry_type=3, maxzoom=19, feature_id_offset=10_000)
    _write_fixture_ardf(ardf_path, {
        'schema': 'nwmm-alaska-pmtiles-v1',
        'dataset': 'ardf',
        'snapshot': retrieved,
        'counts': {'ardf': validate_national.ARDF_EXPECTED_COUNT},
        'staging_sha256': ardf_staging,
    })
    def source_id_inventory(layer_ids, counts, feature_id_offset=0):
        return {
            layer_id: {
                'status': 'complete_at_retrieval',
                'source_records': counts[layer_id],
                'maxzoom_feature_instances': counts[layer_id],
                'maxzoom_unique_tiled_ids': counts[layer_id],
                'ids_sha256': hashlib.sha256(json.dumps(
                    [feature_id_offset + (layer_index + 1) * 1000 + row_index + 1
                     for row_index in range(counts[layer_id])],
                    separators=(',', ':')).encode()).hexdigest(),
            }
            for layer_index, layer_id in enumerate(layer_ids)
        }
    base_inventory = source_id_inventory(claim_layers, base_counts)
    precision_inventory = source_id_inventory(
        precision_layers, precision_counts, feature_id_offset=10_000)
    geometry_pin = validate_national.ALASKA_PRECISION_SOURCE_GEOMETRY_SHA256
    precision_objectids = {
        status: sorted(geometry_pin[status]) for status in ('active', 'closed')}
    def compact(values):
        values = sorted(values)
        return {'records': len(values),
                'minimum_id': values[0] if values else None,
                'maximum_id': values[-1] if values else None,
                'ids_sha256': hashlib.sha256(json.dumps(
                    values, sort_keys=True, separators=(',', ':')).encode()
                ).hexdigest()}
    combined_inventory = {}
    source_objectids = {}
    source_snapshot_inventory = {}
    for status_index, status in enumerate(claim_layers):
        base_ids = [(status_index + 1) * 1000 + index + 1
                    for index in range(base_counts[status])]
        precision_layer = {'active': 'active_precision',
                           'closed': 'closed_precision'}.get(status)
        precision_index = (precision_layers.index(precision_layer)
                           if precision_layer else None)
        precision_ids = ([10_000 + (precision_index + 1) * 1000 + index + 1
                          for index in range(precision_counts[precision_layer])]
                         if precision_layer else [])
        combined_ids = sorted(set(base_ids) | set(precision_ids))
        combined_inventory[status] = {
            'status': 'complete_at_retrieval',
            'source_records': claims_counts[status],
            'maxzoom_feature_instances': claims_counts[status],
            'maxzoom_unique_tiled_ids': claims_counts[status],
            'ids_sha256': hashlib.sha256(json.dumps(
                combined_ids, separators=(',', ':')).encode()).hexdigest(),
            'base_records': base_counts[status],
            'precision_records': len(precision_ids),
            'disjoint_union_complete': True,
        }
        precision_oids = precision_objectids.get(status, [])
        base_oids = list(range(100000 + status_index * 100,
                              100000 + status_index * 100 + base_counts[status]))
        source_oids = sorted(base_oids + precision_oids)
        source_objectids[status] = {
            'source': compact(source_oids), 'base': compact(base_oids),
            'precision': compact(precision_oids),
            'disjoint_union_complete': True,
        }
        source_snapshot_inventory[status] = {
            'n': claims_counts[status],
            'minimum_object_id': source_oids[0],
            'maximum_object_id': source_oids[-1],
            'object_ids_sha256': compact(source_oids)['ids_sha256'],
        }
    return {
        'alaska_state_claims': {
            'file': 'data/tiles/claims/ak-state.pmtiles',
            'format': 'pmtiles',
            'source_layers': claim_layers,
            'n': sum(claims_counts.values()),
            'by_status': claims_counts,
            'system': 'alaska_state_claims',
            'jurisdiction': 'state',
            'retrieved': retrieved,
            'minzoom': 0,
            'maxzoom': 13,
            'activation_zoom': 8,
            'source': validate_national.ALASKA_STATE_CLAIMS_SOURCE,
            'staging_sha256': claims_staging,
            'source_quality': {
                'collapsed_point_rings': 0,
                'counterclockwise_exteriors': 0,
                'exact_duplicate_rows': 0,
                'future_labor_dates': 0,
                'repeated_serial_rows': 0,
                'zero_area_features': 0,
                'zero_area_rings': 0,
            },
            'source_id_inventory': combined_inventory,
            'source_objectid_inventory': source_objectids,
            'source_snapshot_inventory': source_snapshot_inventory,
            'base_delivery': {
                'n': sum(base_counts.values()), 'by_status': base_counts,
                'source_id_inventory': base_inventory,
            },
            'precision_overflow': {
                'file': 'data/tiles/claims/ak-state-precision.pmtiles',
                'format': 'pmtiles', 'source_layers': precision_layers,
                'n': sum(precision_counts.values()),
                'by_status': precision_status_counts,
                'source_objectids': precision_objectids,
                'source_geometry_sha256': {
                    status: {str(key): value for key, value in rows.items()}
                    for status, rows in geometry_pin.items()},
                'source_id_inventory': precision_inventory,
                'minzoom': 0, 'maxzoom': 19, 'activation_zoom': 19,
                'bytes': os.path.getsize(precision_path),
                'sha256': _sha256(precision_path),
                'note': 'Unchanged official source polygons.',
            },
            'bytes': os.path.getsize(claims_path),
            'sha256': _sha256(claims_path),
        },
        'ardf': {
            'file': 'data/tiles/national/ardf.pmtiles',
            'format': 'pmtiles',
            'source_layer': 'ardf',
            'n': validate_national.ARDF_EXPECTED_COUNT,
            'states': {'AK': validate_national.ARDF_EXPECTED_COUNT},
            'retrieved': retrieved,
            'minzoom': 0,
            'maxzoom': 13,
            'source': validate_national.ARDF_SOURCE,
            'staging_sha256': ardf_staging,
            'source_snapshot_contract':
                validate_national.ARDF_SOURCE_SNAPSHOT_CONTRACT,
            'source_snapshot_inventory': copy.deepcopy(
                validate_national.ARDF_EXPECTED_SOURCE_SNAPSHOT_INVENTORY),
            'source_quality': copy.deepcopy(
                validate_national.ARDF_EXPECTED_SOURCE_QUALITY),
            'source_id_inventory': source_id_inventory(
                ['ardf'], {'ardf': validate_national.ARDF_EXPECTED_COUNT}),
            'bytes': os.path.getsize(ardf_path),
            'sha256': _sha256(ardf_path),
        },
    }


def _geology_fault_entry(layer):
    states = {state: 1 for state in sorted(validate_national.ALL_STATES)}
    sources = copy.deepcopy(validate_national._GEOLOGY_FAULT_SOURCE_SPECS)
    if layer == 'geology':
        by_source = {'usgs_sgmc_v1_1': 48, 'usgs_sim3340': 1}
        statuses = {
            'explicit': 47, 'source_reference_omits_scale': 1,
            'reserved_001_no_reference_row': 1,
        }
        source = 'U.S. Geological Survey SGMC v1.1 and SIM 3340'
    else:
        for state in ('DE', 'FL', 'MD', 'ND', 'NE'):
            states[state] = 0
        by_source = {
            'usgs_sgmc_v1_1': 20, 'usgs_sim3340': 1,
            'usgs_qfaults_2020': 23,
        }
        statuses = {
            'explicit': 40, 'source_reference_omits_scale': 1,
            'reserved_001_no_reference_row': 1,
            'source_marks_unspecified': 2,
        }
        source = 'U.S. Geological Survey SGMC v1.1, SIM 3340, and Qfaults'
        sources['qfaults'] = {
            'title': 'Quaternary Fault and Fold Database for the Nation',
            'authority': 'U.S. Geological Survey',
            'url': ('https://earthquake.usgs.gov/static/lfs/nshm/qfaults/'
                    'Qfaults_GIS.zip'),
            'doi': 'https://doi.org/10.5066/P9BCVRCK',
            'release': 'reviewed fixture',
            'download': {
                'bytes': 128, 'sha256': 'f' * 64,
                'etag': 'fixture-etag', 'last_modified': 'fixture-date',
            },
        }
    n = sum(states.values())
    return {
        'source': source, 'n': n, 'states': states,
        'by_source': by_source, 'source_scale_status': statuses,
        'sources': sources,
        'coverage': {
            'states': 49, 'excluded_state_codes': ['DC', 'HI', 'PR'],
            'zero_feature_states': sorted(
                state for state, count in states.items() if count == 0),
        },
        'provenance_properties': list(
            validate_national._GEOLOGY_FAULT_PROVENANCE_PROPERTIES),
        'geometry_normalization': {
            'engine': validate_national._GEOMETRY_NORMALIZATION_ENGINE,
            'reason': validate_national._GEOMETRY_NORMALIZATION_REASON,
            'count': 0,
            'inventory_sha256': hashlib.sha256(b'[]').hexdigest(),
            'features': [],
        },
        'tile_fid_reconciliation': {
            'source_records': n, 'unique_tiled_fids': n,
            'maxzoom': 12, 'maxzoom_unique_tiled_fids': n,
            'missing_fid_count': 0, 'extra_fid_count': 0,
            'deterministic_builds': 2,
            'diagnostic_missing_fids': [],
            'diagnostic_missing_fids_sha256': hashlib.sha256(b'[]').hexdigest(),
        },
    }


class ReleaseArtifactValidationTests(unittest.TestCase):
    def test_alaska_validator_builder_import_order_and_precision_pin(self):
        import importlib
        builder = importlib.import_module('build_alaska_pmtiles')
        validator = importlib.import_module('validate_national')
        self.assertEqual(
            validator.ALASKA_PRECISION_SOURCE_GEOMETRY_SHA256,
            builder.EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256)

    @staticmethod
    def _write_json(path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as artifact:
            json.dump(value, artifact, sort_keys=True)

    @staticmethod
    def _grade_source(source_id, url=None):
        return {
            'source_id': source_id,
            'title': f'Reviewed primary source {source_id}',
            'authority': 'Unit Test Geological Survey',
            'url': url or f'https://example.test/{source_id}.pdf',
            'primary': True,
            'document_sha256': hashlib.sha256(
                f'{source_id}-document'.encode()).hexdigest(),
            'page_index_sha256': hashlib.sha256(
                f'{source_id}-index'.encode()).hexdigest(),
        }

    def _compiled_grade_document(self, code='NV'):
        registry, registry_sha = national_grades._registry_context()
        source = self._grade_source('nv-primary')
        low_sources = []
        for index in (1, 2):
            row = self._grade_source(f'nv-low-{index}')
            row.update({
                'page_cite': f'p. {index}',
                'verbatim_quote': (
                    f'Verbatim finding from independent low-endowment source {index}.'),
                'quote_verbatim': True,
                'page_text_sha256': hashlib.sha256(
                    f'low-page-{index}'.encode()).hexdigest(),
            })
            low_sources.append(row)
        pp610 = self._grade_source(
            'pp610', 'https://pubs.usgs.gov/pp/0610/report.pdf')
        price_sha = hashlib.sha256(b'reviewed-price-config').hexdigest()
        document = {
            'schema_version': 1,
            'dataset': national_grades.DATASET,
            'snapshot': '2026-08-13',
            'state': code,
            'state_name': registry[code]['name'],
            'regime': registry[code]['regime'],
            'registry_sha256': registry_sha,
            'price_config_sha256': price_sha,
            'input_artifacts': {
                'grades': {'path': f'grades/{code.lower()}.json', 'bytes': 100,
                           'sha256': hashlib.sha256(b'grades').hexdigest()},
                'low_endowment': {
                    'path': f'low/{code.lower()}.json', 'bytes': 100,
                    'sha256': hashlib.sha256(b'low').hexdigest()},
                'pp610': {'path': f'pp610/{code.lower()}.json', 'bytes': 100,
                          'sha256': hashlib.sha256(b'pp610').hexdigest()},
            },
            'metrics': {
                'graded_mines': 1, 'primary_sources': 1,
                'verbatim_quotes': 1, 'page_cites': 1,
                'primary_source_ids': ['nv-primary'], 'pp610_districts': 1,
            },
            'grade_requirement': {
                'status': 'documented_low_endowment',
                'done_gate_eligible': True, 'gaps': [],
            },
            'primary_sources': [source],
            'mines': [{
                'mine_id': 'nv-mine-1', 'name': 'Nevada reviewed mine',
                'district': 'Nevada test district',
                'evidence': [{
                    'evidence_id': 'nv-grade-1', 'source_id': 'nv-primary',
                    'page_cite': 'p. 9',
                    'verbatim_quote': 'The reported gold grade was one ounce per ton.',
                    'quote_verbatim': True,
                    'page_text_sha256': hashlib.sha256(b'grade-page').hexdigest(),
                    'normalized_measurements': [{
                        'commodity': 'Au', 'value': 1.0,
                        'unit': 'troy_ounces_per_short_ton',
                        'input_value': 1.0,
                        'input_unit': 'troy_ounces_per_short_ton',
                        'method': 'identity',
                    }],
                }],
            }],
            'low_endowment_finding': {
                'finding': ('Two independent primary-source reviews document that this '
                            'fixture cannot support twenty-five distinct graded mines.'),
                'review_complete': True,
                'sources': low_sources,
            },
            'pp610': {
                'source': pp610, 'complete': True, 'district_count': 1,
                'districts': [{
                    'district_id': 'nv-001', 'name': 'Nevada test district',
                    'page_cite': 'p. 187',
                    'verbatim_quote': 'A documented Nevada mineral district.',
                    'quote_verbatim': True,
                    'page_text_sha256': hashlib.sha256(b'pp610-page').hexdigest(),
                }],
                'no_district_finding': None,
            },
            'effect': 'evidence_only_no_release_mutation',
        }
        return document

    def _publish_compiled_grade(self, site, document):
        raw = national_grades.canonical_bytes(document)
        digest = hashlib.sha256(raw).hexdigest()
        relative = (f'map-assets/releases/grade-evidence/states/'
                    f'{document["state"].lower()}/{digest}.json')
        path = os.path.join(site, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as artifact:
            artifact.write(raw)
        low = document.get('low_endowment_finding')
        metrics = document['metrics']
        acceptance = {
            'grades': {
                'evidence_artifact': relative, 'sha256': digest,
                'graded_mines': metrics['graded_mines'],
                'primary_sources': metrics['primary_sources'],
                'verbatim_quotes': metrics['verbatim_quotes'],
                'page_cites': metrics['page_cites'],
                'low_endowment_finding': low,
            },
            'district_anchor': {
                'source_id': 'pp610', 'artifact': relative,
                'source_sha256': document['pp610']['source']['document_sha256'],
                'district_count': document['pp610']['district_count'],
                'complete': True, 'no_district_finding': None,
            },
        }
        return acceptance

    def test_recorder_evidence_reconciles_alaska_recording_districts_and_systems(self):
        state = {
            'regime': 'claim',
            'recorder': {
                'jurisdiction_type': 'recording_district',
                'matrix': [{
                    'jurisdiction_id': 'Fairbanks Recording District',
                    'status': 'accepted', 'portal_vendor': 'Alaska DNR Recorder',
                    'portal_url': 'https://dnr.alaska.gov/ssd/recoff/',
                }],
            },
            'claim_systems': [
                {'id': 'federal_mlrs',
                 'source_layer_counts': {'active': 0}},
                {'id': 'alaska_state_claims',
                 'source_layer_counts': {'active': 1}},
            ],
        }
        acceptance = {'recorders': {
            'jurisdiction_type': 'recording_district',
            'inventory_complete': True,
            'active_claims': 1,
            'evidence_artifact': 'data/evidence/ak-recorders.json',
            'live_claim_jurisdiction_ids': ['Fairbanks Recording District'],
            'covered_jurisdiction_ids': ['Fairbanks Recording District'],
        }}
        evidence = {
            'schema_version': 1, 'state': 'AK',
            'jurisdiction_type': 'recording_district',
            'inventory_complete': True,
            'active_claims': 1,
            'live_claim_jurisdiction_ids': ['Fairbanks Recording District'],
            'covered_jurisdiction_ids': ['Fairbanks Recording District'],
            'matrix_jurisdiction_ids': ['Fairbanks Recording District'],
            'claim_systems': [
                {'system_id': 'federal_mlrs',
                 'active_claims': 0,
                 'live_claim_jurisdiction_ids': []},
                {'system_id': 'alaska_state_claims',
                 'active_claims': 1,
                 'live_claim_jurisdiction_ids': ['Fairbanks Recording District']},
            ],
        }
        with tempfile.TemporaryDirectory() as site:
            self._write_json(os.path.join(site, 'data/evidence/ak-recorders.json'),
                             evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_recorder_evidence(
                    qa, 'AK', state, acceptance)
            self.assertEqual(qa.errors, [])
            evidence['matrix_jurisdiction_ids'] = ['Juneau Recording District']
            self._write_json(os.path.join(site, 'data/evidence/ak-recorders.json'),
                             evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_recorder_evidence(
                    qa, 'AK', state, acceptance)
            self.assertTrue(any('do not reconcile exactly' in error
                                for error in qa.errors))

    def test_recorder_evidence_accepts_only_proven_zero_live_inventory(self):
        state = {
            'regime': 'claim',
            'recorder': {'jurisdiction_type': 'county', 'matrix': []},
            'claim_systems': [{
                'id': 'federal_mlrs',
                'source_layer_counts': {'active': 0},
            }],
        }
        acceptance = {'recorders': {
            'jurisdiction_type': 'county', 'inventory_complete': True,
            'active_claims': 0,
            'evidence_artifact': 'data/evidence/fl-recorders.json',
            'live_claim_jurisdiction_ids': [],
            'covered_jurisdiction_ids': [],
        }}
        evidence = {
            'schema_version': 1, 'state': 'FL', 'jurisdiction_type': 'county',
            'inventory_complete': True, 'active_claims': 0,
            'live_claim_jurisdiction_ids': [],
            'covered_jurisdiction_ids': [], 'matrix_jurisdiction_ids': [],
            'claim_systems': [{
                'system_id': 'federal_mlrs', 'active_claims': 0,
                'live_claim_jurisdiction_ids': [],
            }],
        }
        with tempfile.TemporaryDirectory() as site:
            path = os.path.join(site, 'data/evidence/fl-recorders.json')
            self._write_json(path, evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_recorder_evidence(
                    qa, 'FL', state, acceptance)
            self.assertEqual(qa.errors, [])
            evidence['active_claims'] = 1
            self._write_json(path, evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_recorder_evidence(
                    qa, 'FL', state, acceptance)
            self.assertTrue(qa.errors)

    def test_legacy_unbound_ci_summary_cannot_satisfy_done_gate(self):
        acceptance = {'ci_scale': {
            'evidence_artifact': 'data/evidence/nv-ci.json',
            'sha256': 'a' * 64,
            'run_url': 'https://ci.example.test/runs/123',
            'commit': 'a' * 40, 'state_toggle_green': True,
            'statewide_browser_json': False,
            'heap_mb': 42.5, 'bulk_origin_storage_mb': 0,
        }}
        evidence = {
            'schema_version': 1, 'state': 'NV', 'profile': 'release',
            'status': 'green', 'run_url': 'https://ci.example.test/runs/123',
            'commit': 'a' * 40,
            'state_toggle': {'state': 'NV', 'enabled': True, 'green': True},
            'statewide_browser_json': False,
            'measurements': {'heap_mb': 42.5, 'bulk_origin_storage_mb': 0},
            'budget_limits': {'heap_mb_max': 211,
                              'bulk_origin_storage_mb_max': 0},
        }
        budgets = {'browser': {'heap_mb_max': 211,
                               'bulk_origin_storage_mb_max': 0}}
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            budget_path = os.path.join(root, 'budgets.json')
            self._write_json(os.path.join(site, 'data/evidence/nv-ci.json'), evidence)
            self._write_json(budget_path, budgets)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), \
                    mock.patch.object(validate_national, 'BUDGETS', budget_path):
                validate_national._validate_ci_evidence(qa, 'NV', acceptance)
            self.assertTrue(any('CI acceptance evidence is invalid' in error
                                for error in qa.errors))

    def test_claim_publication_evidence_binds_clip_input_and_layer_counts(self):
        system = {
            'id': 'federal_mlrs', 'source_id': 'federal_mlrs',
            'publication_inventory_artifact': 'data/evidence/nv-mlrs.json',
            'retrieved': '2026-08-13', 'complete': True, 'truncated': False,
            'source_layers': ['active', 'closed', 'open_ground'],
            'source_layer_counts': {'active': 5, 'closed': 6, 'open_ground': 7},
            'publication_artifacts': {
                'claims': {'source_layers': ['active', 'closed'], 'sha256': 'c' * 64},
                'open_ground': {
                    'source_layers': ['open_ground'], 'sha256': 'd' * 64,
                    'source_id_inventory': {
                        'status': 'complete_at_derivation',
                        'source_records': 7,
                        'maxzoom_feature_instances': 9,
                        'maxzoom_unique_tiled_ids': 7,
                        'ids_sha256': 'e' * 64,
                    },
                },
            },
        }
        state = {'regime': 'claim', 'claim_systems': [system]}
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            clips = os.path.join(root, 'state_clips.json')
            clip_bytes = b'{"states":{"NV":{}}}'
            with open(clips, 'wb') as clip_file:
                clip_file.write(clip_bytes)
            evidence = {
                'schema_version': 1, 'state': 'NV',
                'system_id': 'federal_mlrs', 'source_id': 'federal_mlrs',
                'retrieved': '2026-08-13', 'complete': True,
                'truncated': False, 'pagination_exhausted': True,
                'state_clip_sha256': hashlib.sha256(clip_bytes).hexdigest(),
                'source_layer_counts': {'active': 5, 'closed': 6,
                                        'open_ground': 7},
                'publication_artifacts': {
                    'claims': {
                        'complete': True, 'truncated': False,
                        'source_layers': ['active', 'closed'],
                        'source_layer_counts': {'active': 5, 'closed': 6},
                        'artifact_sha256': 'c' * 64, 'input_sha256': 'a' * 64,
                    },
                    'open_ground': {
                        'complete': True, 'truncated': False,
                        'source_layers': ['open_ground'],
                        'source_layer_counts': {'open_ground': 7},
                        'artifact_sha256': 'd' * 64, 'input_sha256': 'b' * 64,
                        'source_id_inventory': {
                            'status': 'complete_at_derivation',
                            'source_records': 7,
                            'maxzoom_feature_instances': 9,
                            'maxzoom_unique_tiled_ids': 7,
                            'ids_sha256': 'e' * 64,
                        },
                    },
                },
            }
            self._write_json(os.path.join(site, 'data/evidence/nv-mlrs.json'), evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), \
                    mock.patch.object(validate_national, 'STATE_CLIPS', clips):
                validate_national._validate_claim_publication_evidence(
                    qa, 'NV', state)
            self.assertEqual(qa.errors, [])
            evidence['pagination_exhausted'] = False
            self._write_json(os.path.join(site, 'data/evidence/nv-mlrs.json'), evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), \
                    mock.patch.object(validate_national, 'STATE_CLIPS', clips):
                validate_national._validate_claim_publication_evidence(
                    qa, 'NV', state)
            self.assertTrue(any('complete pagination' in error for error in qa.errors))

    def test_pp610_anchor_requires_page_cited_extraction(self):
        with tempfile.TemporaryDirectory() as site:
            evidence = self._compiled_grade_document()
            acceptance = self._publish_compiled_grade(site, evidence)
            state = {'historic_serials': [{
                'source_id': 'pp610',
                'url': 'https://pubs.usgs.gov/pp/0610/report.pdf'}]}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_grade_evidence(
                    qa, 'NV', state, acceptance)
                validate_national._validate_district_anchor_evidence(
                    qa, 'NV', state, acceptance)
            self.assertEqual(qa.errors, [])
            evidence['pp610']['districts'][0]['verbatim_quote'] = ''
            acceptance = self._publish_compiled_grade(site, evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_district_anchor_evidence(
                    qa, 'NV', state, acceptance)
            self.assertTrue(any('PP 610 district evidence' in error for error in qa.errors))

    def test_nonclaim_equivalent_evidence_binds_ingestion_and_explicit_gap(self):
        with tempfile.TemporaryDirectory() as site:
            aml_path = 'data/evidence/mi-aml.json'
            trust_path = 'data/evidence/mi-trust.json'
            aml_url = 'https://www.michigan.gov/egle/about/organization/remediation-and-redevelopment'
            trust_url = 'https://www.michigan.gov/dnr/managing-resources/minerals/metallic'
            self._write_json(os.path.join(site, aml_path), {
                'schema_version': 1, 'state': 'MI', 'kind': 'aml',
                'release_inventory_status': 'documented_unavailable',
                'complete': True, 'reviewed': '2026-08-13',
                'official_source_urls': [aml_url],
                'spatial_inventory_available': False,
                'finding': ('The agency review found no complete public statewide '
                            'spatial abandoned-mine inventory suitable for ingestion.'),
            })
            self._write_json(os.path.join(site, trust_path), {
                'schema_version': 1, 'state': 'MI', 'kind': 'trust_land',
                'release_inventory_status': 'ingested_complete',
                'complete': True, 'reviewed': '2026-08-13',
                'official_source_urls': [trust_url], 'retrieved': '2026-08-13',
                'input_sha256': 'b' * 64, 'artifact_sha256': 'a' * 64,
                'source_layer_counts': {'trust_land': 9},
            })
            state = {'regime': 'non_claim', 'aml': {
                'state_inventory_url': aml_url,
                'release_inventory_status': 'documented_unavailable',
                'evidence_artifact': aml_path,
            }, 'trust_land': {
                'portal_url': trust_url,
                'release_inventory_status': 'ingested_complete',
                'evidence_artifact': trust_path, 'sha256': 'a' * 64,
                'layer_metadata': {'trust_land': {'n': 9}},
            }}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_nonclaim_equivalent_evidence(
                    qa, 'MI', state)
            self.assertEqual(qa.errors, [])
            with open(os.path.join(site, aml_path), encoding='utf-8') as source:
                bad = json.load(source)
            bad['finding'] = 'too short'
            self._write_json(os.path.join(site, aml_path), bad)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_nonclaim_equivalent_evidence(
                    qa, 'MI', state)
            self.assertTrue(any('unavailability evidence' in error for error in qa.errors))

    def test_expiration_watch_evidence_requires_all_claim_system_snapshots(self):
        with tempfile.TemporaryDirectory() as site:
            relative = 'data/evidence/ak-watch.json'
            evidence = {
                'schema_version': 1, 'state': 'AK', 'run_id': 'run-1',
                'generated': '2026-08-13T12:00:00+00:00', 'complete': True,
                'systems': {
                    'federal_mlrs': {
                        'status': 'complete', 'active_now': 8,
                        'source_snapshot_sha256': 'a' * 64},
                    'alaska_state_claims': {
                        'status': 'complete', 'active_now': 12,
                        'source_snapshot_sha256': 'b' * 64},
                },
            }
            self._write_json(os.path.join(site, relative), evidence)
            state = {'regime': 'claim', 'claim_systems': [
                {'id': 'federal_mlrs'}, {'id': 'alaska_state_claims'}]}
            acceptance = {'expiration_watch': {
                'evidence_artifact': relative, 'run_id': 'run-1',
                'generated': evidence['generated'], 'complete': True,
                'system_ids': ['federal_mlrs', 'alaska_state_claims'],
            }}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_expiration_watch_evidence(
                    qa, 'AK', state, acceptance)
            self.assertEqual(qa.errors, [])
            evidence['systems'].pop('federal_mlrs')
            self._write_json(os.path.join(site, relative), evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_expiration_watch_evidence(
                    qa, 'AK', state, acceptance)
            self.assertTrue(any('every claim system' in error for error in qa.errors))

    def test_ranked_top_five_is_bound_to_checksummed_scoring_output(self):
        with tempfile.TemporaryDirectory() as site:
            folder = os.path.join(site, 'map-assets', 'releases', 'targets', 'nv')
            os.makedirs(folder)
            inputs = {name: str(index) * 64 for index, name in enumerate(
                ('grades', 'geology', 'land_context', 'open_ground'), start=1)}
            targets = [{
                'target_id': f'nv-{rank}', 'rank': rank, 'score': 100 - rank,
                'land_context': {'surface_class': 'federal',
                                 'mineral_class': 'federal_locatable',
                                 'approach': 'federal_staking'},
                'open_ground': {'status': 'measured'},
                'score_components': {
                    'grade': 40, 'geology': 55 - rank,
                    'open_ground': {'status': 'measured', 'value': 5}},
                'rich_open': {'status': 'measured', 'score': 100 - rank},
            } for rank in range(1, 6)]
            scoring = {'schema_version': 1, 'state': 'NV',
                       'method_id': 'ws11-target-score-v1',
                       'input_sha256s': inputs, 'targets': targets}
            scoring_temporary = os.path.join(folder, 'scoring.tmp.json')
            self._write_json(scoring_temporary, scoring)
            scoring_sha = _sha256(scoring_temporary)
            scoring_relative = f'map-assets/releases/targets/nv/{scoring_sha}.json'
            scoring_path = os.path.join(site, scoring_relative)
            os.replace(scoring_temporary, scoring_path)
            ranked = {'schema_version': 1, 'state': 'NV',
                      'method_id': 'ws11-target-score-v1',
                      'scoring_artifact': scoring_relative,
                      'scoring_sha256': scoring_sha,
                      'input_sha256s': inputs,
                      'targets': [{key: row[key] for key in
                                   ('target_id', 'rank', 'score')} for row in targets]}
            ranked_temporary = os.path.join(folder, 'ranked.tmp.json')
            self._write_json(ranked_temporary, ranked)
            ranked_sha = _sha256(ranked_temporary)
            ranked_relative = f'map-assets/releases/targets/nv/{ranked_sha}.json'
            ranked_path = os.path.join(site, ranked_relative)
            os.replace(ranked_temporary, ranked_path)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                ids = validate_national._validate_ranked_target_evidence(
                    qa, 'NV', {'regime': 'claim'},
                    {'ranked_targets_artifact': ranked_relative,
                     'ranked_targets_sha256': ranked_sha})
            self.assertEqual(qa.errors, [])
            self.assertEqual(ids, {f'nv-{rank}' for rank in range(1, 6)})
            ranked['targets'][0]['score'] = -1
            self._write_json(ranked_path, ranked)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_ranked_target_evidence(
                    qa, 'NV', {'regime': 'claim'},
                    {'ranked_targets_artifact': ranked_relative,
                     'ranked_targets_sha256': ranked_sha})
            self.assertTrue(qa.errors)

    def test_ranked_top_five_accepts_compiler_state_artifact_and_replays_it(self):
        with tempfile.TemporaryDirectory() as site:
            folder = os.path.join(site, 'map-assets', 'releases', 'targets', 'nv')
            os.makedirs(folder)
            registry, registry_sha = national_scoring._registry_context()
            systems = {system: hashlib.sha256(system.encode()).hexdigest()
                       for system in registry['NV']['claim_systems']}
            inputs = {
                'ranked_targets': {'bytes': 101, 'sha256': '1' * 64},
                'grade_terms': {'bytes': 102, 'sha256': '2' * 64},
                'geology_terms': {'bytes': 103, 'sha256': '3' * 64},
                'open_ground': {'bytes': 104, 'sha256': '4' * 64},
            }
            targets = []
            for rank in range(1, 6):
                grade, geology, open_score = 40.0, 50.0 - rank, 5.0
                targets.append({
                    'target_id': f'nv-compiled-{rank}',
                    'name': f'NV compiled target {rank}', 'rank': rank,
                    'area_km2': 10.0 - rank,
                    'location': {'longitude': -116.0 + rank / 10,
                                 'latitude': 38.0 + rank / 10},
                    'score': {'total': grade + geology + open_score,
                              'grade': grade, 'geology': geology,
                              'open_ground': open_score},
                    'grade': {'score': grade, 'terms': ['reviewed grade'],
                              'evidence_refs': ['a' * 64],
                              'rationale': 'Reviewed grade evidence supports this target.'},
                    'geology': {'score': geology, 'terms': ['reviewed geology'],
                                'evidence_refs': ['b' * 64],
                                'rationale': 'Reviewed geology evidence supports this target.'},
                    'open_ground': {
                        'status': 'measured', 'value': 0.5, 'unit': 'fraction',
                        'score': open_score, 'evidence_refs': sorted(systems.values()),
                        'rationale': 'Complete statewide open-ground measurement.',
                        'display': '50%'},
                    'land_context': None,
                })
            compiled = {
                'schema_version': 1, 'dataset': national_scoring.DATASET,
                'snapshot': '2026-08-13', 'state': 'NV',
                'state_name': registry['NV']['name'], 'regime': 'claim',
                'registry_sha256': registry_sha, 'inventory_sha256': '5' * 64,
                'method_id': 'ws11-richopen-reviewed-v1',
                'input_artifacts': inputs,
                'regime_evidence': {
                    'open_ground': {
                        'status': 'measured', 'coverage_status': 'statewide_complete',
                        'all_ranked_targets_covered': True,
                        'source_snapshot_sha256s': systems},
                    'land_context': None},
                'sort_policy': national_scoring.SORT_POLICY,
                'metrics': {'targets': 5, 'measured_open_ground': 5,
                            'measured_zero_open_ground': 0,
                            'not_applicable_open_ground': 0,
                            'land_context_cards': 0},
                'targets': targets, 'effect': national_scoring.EFFECT,
            }
            national_scoring.validate_compiled_state_document(
                compiled, 'NV', registry['NV'], registry_sha)
            scoring_tmp = os.path.join(folder, 'compiled.tmp.json')
            self._write_json(scoring_tmp, compiled)
            scoring_sha = _sha256(scoring_tmp)
            scoring_relative = f'map-assets/releases/targets/nv/{scoring_sha}.json'
            os.replace(scoring_tmp, os.path.join(site, scoring_relative))
            input_hashes = {name: row['sha256'] for name, row in inputs.items()}
            ranked = {
                'schema_version': 1, 'state': 'NV',
                'method_id': compiled['method_id'],
                'scoring_artifact': scoring_relative,
                'scoring_sha256': scoring_sha, 'input_sha256s': input_hashes,
                'targets': [{'target_id': row['target_id'], 'rank': row['rank'],
                             'score': row['score']['total']} for row in targets],
            }
            ranked_tmp = os.path.join(folder, 'ranked.tmp.json')
            self._write_json(ranked_tmp, ranked)
            ranked_sha = _sha256(ranked_tmp)
            ranked_relative = f'map-assets/releases/targets/nv/{ranked_sha}.json'
            os.replace(ranked_tmp, os.path.join(site, ranked_relative))
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                ids = validate_national._validate_ranked_target_evidence(
                    qa, 'NV', {'regime': 'claim'}, {
                        'ranked_targets_artifact': ranked_relative,
                        'ranked_targets_sha256': ranked_sha})
            self.assertEqual(qa.errors, [])
            self.assertEqual(ids, {f'nv-compiled-{rank}' for rank in range(1, 6)})

    def test_registry_baseline_pointers_must_match_manifest(self):
        baselines = {
            'ardf': {'file': 'data/tiles/national/ardf.pmtiles', 'n': 7,
                     'sha256': 'a' * 64},
            'alaska_state_claims': {
                'file': 'data/tiles/claims/ak-state.pmtiles', 'n': 11,
                'sha256': 'b' * 64},
        }
        registry = {'AK': {
            'occurrence_backbone': {
                'baseline_manifest_key': 'national_baselines.ardf',
                'baseline_browser_path': baselines['ardf']['file'],
                'baseline_features': 7,
                'baseline_sha256': 'stale',
            },
            'claim_systems': [{
                'id': 'alaska_state_claims',
                'baseline_manifest_key': 'national_baselines.alaska_state_claims',
                'baseline_browser_path': baselines['alaska_state_claims']['file'],
                'baseline_features': 11,
                'baseline_sha256': 'b' * 64,
            }],
        }, 'NV': {}}
        qa = validate_national.QA()
        with mock.patch.object(validate_national, 'load_states',
                               return_value=registry):
            validate_national.validate_registry_baseline_pointers(qa, baselines)
        self.assertTrue(any('ardf.baseline_sha256' in error
                            for error in qa.errors))
        self.assertFalse(any('alaska_state_claims' in error
                             for error in qa.errors))

    def test_zero_filled_pmtiles_header_is_rejected(self):
        with tempfile.NamedTemporaryFile() as artifact:
            artifact.write(b'PMTiles\x03' + b'\0' * 119)
            artifact.flush()
            with self.assertRaises(ValueError):
                validate_national._pmtiles_header(artifact.name, ['claims'])

    def test_current_admin_archive_has_real_directories_and_layers(self):
        path = os.path.join(ROOT, 'site', 'data', 'tiles', 'context',
                            'admin.pmtiles')
        meta = validate_national._pmtiles_header(path, ['states', 'counties'])
        self.assertGreater(meta['tile_entries'], 0)
        self.assertGreater(meta['root_entries'], 0)

    def test_artifact_path_cannot_escape_site(self):
        with self.assertRaises(ValueError):
            validate_national._resolve_artifact('../../outside.pmtiles', 'NV')

    def test_valid_minimal_pmtiles_checks_real_mvt_payload(self):
        with tempfile.NamedTemporaryFile() as artifact:
            _minimal_pmtiles(artifact.name)
            meta = validate_national._pmtiles_header(
                artifact.name, ['claims'], ['st'],
                verify_feature_properties=True, expected_state='NV',
                expected_bounds=[[-120.01, 35.0, -114.04, 42.01]])
            self.assertEqual(meta['sample_layers'], ['claims'])

    def test_full_scan_can_reconcile_unique_maxzoom_top_level_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = os.path.join(directory, 'ids.pmtiles')
            _minimal_pmtiles(artifact, properties={'src': 'airborne', 'nm': 'Survey'})
            meta = validate_national._pmtiles_header(
                artifact, ['claims'], {'claims': ['src', 'nm']},
                verify_feature_properties=True, collect_feature_ids=True)
            self.assertEqual(meta['maxzoom_feature_ids'], {'claims': [1]})
            with self.assertRaisesRegex(ValueError, 'full semantic'):
                validate_national._pmtiles_header(
                    artifact, ['claims'], collect_feature_ids=True)
            self.assertEqual(meta['semantic_layer_counts'], {'claims': 1})

    def test_metadata_only_feature_cannot_attest_release_properties(self):
        with tempfile.NamedTemporaryFile() as artifact:
            _minimal_pmtiles(artifact.name, empty_semantics=True)
            with self.assertRaisesRegex(ValueError, 'geometry type|empty geometry'):
                validate_national._pmtiles_header(
                    artifact.name, ['claims'], ['st'],
                    verify_feature_properties=True, expected_state='NV')

    def test_release_feature_state_and_archive_bounds_are_enforced(self):
        with tempfile.NamedTemporaryFile() as artifact:
            _minimal_pmtiles(artifact.name, properties={'st': 'CA'})
            with self.assertRaisesRegex(ValueError, 'is not NV'):
                validate_national._pmtiles_header(
                    artifact.name, ['claims'], ['st'],
                    verify_feature_properties=True, expected_state='NV')
            with self.assertRaisesRegex(ValueError, 'do not intersect'):
                validate_national._pmtiles_header(
                    artifact.name, ['claims'], ['st'],
                    verify_feature_properties=True, expected_state='CA',
                    expected_bounds=[[-80.0, 25.0, -79.0, 26.0]])

    def test_valid_looking_pmtiles_with_garbage_tile_is_rejected(self):
        with tempfile.NamedTemporaryFile() as artifact:
            _minimal_pmtiles(artifact.name, valid_tile=False)
            with self.assertRaises(ValueError):
                validate_national._pmtiles_header(artifact.name, ['claims'])

    def test_pmtiles_header_counts_must_match_directories(self):
        with tempfile.NamedTemporaryFile() as artifact:
            _minimal_pmtiles(artifact.name, addressed=2)
            with self.assertRaisesRegex(ValueError, 'counts'):
                validate_national._pmtiles_header(artifact.name, ['claims'])

    def test_pmtiles_required_properties_are_enforced(self):
        with tempfile.NamedTemporaryFile() as artifact:
            _minimal_pmtiles(artifact.name)
            with self.assertRaisesRegex(ValueError, 'properties'):
                validate_national._pmtiles_header(
                    artifact.name, ['claims'], ['serial'])

    def test_open_ground_title_source_is_bound_to_reviewed_registry_ingest(self):
        properties = {
            'st': 'NV', 'status': 'OPEN', 'open_count': 1,
            'section_count': 1, 'open_fraction': 1.0,
            'mineral_title_status': 'public_domain_locatable',
            'mineral_title_source': 'https://official.example/nv-title',
            'mineral_title_ref': 'MTP-NV-001',
            'mineral_title_reviewed': 1,
        }
        with tempfile.NamedTemporaryFile(suffix='.pmtiles') as artifact:
            _minimal_pmtiles(
                artifact.name, properties=properties,
                layer_name='open_ground')
            meta = validate_national._pmtiles_header(
                artifact.name, ['open_ground'],
                {'open_ground': set(properties)},
                verify_feature_properties=True, expected_state='NV',
                expected_open_ground_title_sources={
                    'NV': 'https://official.example/nv-title'})
            self.assertGreater(
                meta['semantic_layer_counts']['open_ground'], 0)
            with self.assertRaisesRegex(
                    ValueError, 'reviewed state-registry ingest'):
                validate_national._pmtiles_header(
                    artifact.name, ['open_ground'],
                    {'open_ground': set(properties)},
                    verify_feature_properties=True, expected_state='NV',
                    expected_open_ground_title_sources={
                        'NV': 'https://official.example/different-title'})

    def test_national_geology_full_scan_recomputes_feature_inventory(self):
        properties = {
            'fid': 1, 'st': 'NV', 'state': 'NV',
            'src': 'usgs_sgmc_v1_1',
            'source_dataset': 'usgs_sgmc_v1_1',
            'source_id': 'sgmc:NV:fixture', 'source_record_id': '1',
            'source_scale': '1:500,000',
            'source_scale_status': 'explicit',
            'source_ref': 'Reviewed map, scale 1:500,000',
            'source_url': 'https://example.test/geology',
        }
        required = {'geology': tuple(properties)}
        with tempfile.NamedTemporaryFile() as artifact:
            _minimal_pmtiles(
                artifact.name, properties=properties, layer_name='geology')
            meta = validate_national._pmtiles_header(
                artifact.name, ['geology'], required,
                verify_feature_properties=True, collect_feature_inventory=True)
        inventory = meta['feature_inventories']['geology']
        self.assertEqual(inventory['n'], 1)
        self.assertEqual(inventory['states']['NV'], 1)
        self.assertEqual(inventory['by_source'], {'usgs_sgmc_v1_1': 1})
        self.assertEqual(inventory['source_scale_status'], {'explicit': 1})

    def test_national_geology_full_scan_rejects_bad_provenance_fields(self):
        base = {
            'fid': 1, 'st': 'NV', 'state': 'NV',
            'src': 'usgs_sgmc_v1_1',
            'source_dataset': 'usgs_sgmc_v1_1',
            'source_id': 'sgmc:NV:fixture', 'source_record_id': '1',
            'source_scale': '1:500,000',
            'source_scale_status': 'explicit',
            'source_ref': 'Reviewed map, scale 1:500,000',
            'source_url': 'https://example.test/geology',
        }
        mutations = {
            'fid': lambda row: row.update(fid='1'),
            'state': lambda row: row.update(state='CA'),
            'src': lambda row: row.update(src='usgs_sim3340'),
            'source_dataset': lambda row: row.update(source_dataset='unknown'),
            'source_id': lambda row: row.update(source_id='invented:1'),
            'source_scale': lambda row: row.update(source_scale=''),
            'source_scale_status': lambda row:
                row.update(source_scale_status='unknown'),
            'source_ref': lambda row: row.update(source_ref=''),
            'source_url': lambda row: row.update(source_url='not-an-absolute-url'),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.NamedTemporaryFile() as artifact:
                properties = copy.deepcopy(base)
                mutate(properties)
                _minimal_pmtiles(
                    artifact.name, properties=properties, layer_name='geology')
                with self.assertRaises(ValueError):
                    validate_national._pmtiles_header(
                        artifact.name, ['geology'], {'geology': tuple(base)},
                        verify_feature_properties=True,
                        collect_feature_inventory=True)

    def test_geology_fault_manifest_reconciles_honest_zero_states_and_scan(self):
        for layer in ('geology', 'faults'):
            with self.subTest(layer=layer):
                entry = _geology_fault_entry(layer)
                inventory = {
                    field: copy.deepcopy(entry[field])
                    for field in ('n', 'states', 'by_source',
                                  'source_scale_status')
                }
                inventory.update({
                    'unique_tiled_fids': entry['n'],
                    'source_records': entry['n'],
                    'missing_fid_count': 0, 'extra_fid_count': 0,
                })
                qa = validate_national.QA()
                validate_national._validate_national_geology_fault_entry(
                    qa, layer, entry, inventory)
                self.assertEqual(qa.errors, [])
        self.assertEqual(
            _geology_fault_entry('faults')['coverage']['zero_feature_states'],
            ['DE', 'FL', 'MD', 'ND', 'NE'])

    def test_geology_fault_manifest_rejects_forged_counts_and_provenance(self):
        mutations = {
            'state-count': lambda row: row['states'].update(NV=2),
            'source-count': lambda row: row['by_source'].update(
                usgs_sgmc_v1_1=47),
            'zero-list': lambda row: row['coverage'].update(
                zero_feature_states=['NV']),
            'source-url': lambda row: row['sources']['sgmc'].update(
                url='https://example.test/forged'),
            'properties': lambda row: row['provenance_properties'].remove(
                'source_scale'),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                entry = _geology_fault_entry('geology')
                inventory = {
                    field: copy.deepcopy(entry[field])
                    for field in ('n', 'states', 'by_source',
                                  'source_scale_status')
                }
                inventory.update({
                    'unique_tiled_fids': entry['n'],
                    'source_records': entry['n'],
                    'missing_fid_count': 0, 'extra_fid_count': 0,
                })
                mutate(entry)
                qa = validate_national.QA()
                validate_national._validate_national_geology_fault_entry(
                    qa, 'geology', entry, inventory)
                self.assertTrue(qa.errors)

    def test_geology_fault_manifest_rejects_missing_stable_tiled_fid(self):
        entry = _geology_fault_entry('geology')
        inventory = {
            field: copy.deepcopy(entry[field])
            for field in ('n', 'states', 'by_source', 'source_scale_status')
        }
        inventory.update({
            'unique_tiled_fids': entry['n'] - 1,
            'source_records': entry['n'],
            'missing_fid_count': 1, 'missing_fids': [17],
            'extra_fid_count': 0, 'extra_fids': [],
        })
        maxzoom = {
            'unique_tiled_fids': entry['n'] - 1,
            'source_records': entry['n'],
            'missing_fid_count': 1, 'missing_fids': [17],
            'extra_fid_count': 0, 'extra_fids': [],
        }
        qa = validate_national.QA()
        validate_national._validate_national_geology_fault_entry(
            qa, 'geology', entry, inventory,
            maxzoom_inventory=maxzoom,
            all_zoom_instances=entry['n'] * 5,
            maxzoom_instances=entry['n'] * 2,
            archive_maxzoom=12)
        self.assertTrue(any('source-record/tiled-fid inventory' in error and
                            '"missing_fids":[17]' in error
                            for error in qa.errors))

    def test_release_artifacts_request_full_feature_and_state_validation(self):
        with tempfile.NamedTemporaryFile() as artifact:
            artifact.write(b'x' * 256)
            artifact.flush()
            digest = _sha256(artifact.name)
            vector = {
                'artifact': 'map-assets/releases/nv/test.pmtiles',
                'source_layers': ['claims'], 'required_properties': ['st'],
                'layer_metadata': {
                    'claims': {'n': 1, 'availability': 'complete', 'complete': True}},
                'bytes': 256, 'sha256': digest,
            }
            raster = {
                'artifact': 'map-assets/releases/nv/test.tif',
                'bytes': 256, 'sha256': digest,
            }
            state = {
                'release': {'enabled': True}, 'regime': 'non_claim',
                'query_envelopes': [{'bbox': [-120.01, 35.0, -114.04, 42.01]}],
                'geology': dict(vector), 'faults': dict(vector),
                'aeromag': raster, 'land_context': dict(vector),
                'claim_systems': [],
            }
            qa = validate_national.QA()
            with mock.patch.object(validate_national, '_resolve_artifact',
                                   return_value=artifact.name), mock.patch.object(
                    validate_national, '_pmtiles_header',
                    return_value={'bytes': 256,
                                  'semantic_layer_counts': {'claims': 1}}) as pmtiles, mock.patch.object(
                    validate_national, '_tiff_header',
                    return_value={'bytes': 256}):
                validate_national.validate_artifacts(qa, {'NV': state})
            self.assertEqual(qa.errors, [])
            self.assertEqual(pmtiles.call_count, 3)
            for call in pmtiles.call_args_list:
                self.assertIs(call.kwargs['verify_feature_properties'], True)
                self.assertEqual(call.kwargs['expected_state'], 'NV')
                self.assertEqual(call.kwargs['expected_bounds'],
                                 [[-120.01, 35.0, -114.04, 42.01]])

    def test_zero_fault_release_uses_evidence_without_fake_state_feature(self):
        with tempfile.NamedTemporaryFile() as artifact:
            artifact.write(b'x' * 256)
            artifact.flush()
            digest = _sha256(artifact.name)
            vector = {
                'artifact': 'map-assets/releases/nv/test.pmtiles',
                'source_layers': ['units'], 'required_properties': ['st'],
                'layer_metadata': {
                    'units': {'n': 1, 'availability': 'complete', 'complete': True}},
                'bytes': 256, 'sha256': digest,
            }
            faults = {
                'artifact': 'map-assets/releases/national/faults.pmtiles',
                'source_layers': ['faults'], 'required_properties': ['st'],
                'layer_metadata': {
                    'faults': {'n': 0, 'availability': 'complete', 'complete': True}},
                'bytes': 256, 'sha256': digest,
                'zero_inventory': {'evidence_artifact': 'unused'},
            }
            state = {
                'release': {'enabled': True}, 'regime': 'claim',
                'query_envelopes': [{'bbox': [-80, 25, -79, 26]}],
                'geology': vector, 'faults': faults,
                'aeromag': {'artifact': 'map-assets/releases/nv/test.tif',
                            'bytes': 256, 'sha256': digest},
                'claim_systems': [],
            }
            qa = validate_national.QA()
            with mock.patch.object(validate_national, '_resolve_artifact',
                                   return_value=artifact.name), mock.patch.object(
                    validate_national, '_validate_zero_fault_inventory',
                    return_value=True) as zero_check, mock.patch.object(
                    validate_national, '_pmtiles_header',
                    side_effect=lambda _path, layers, *_args, **_kwargs: {
                        'bytes': 256,
                        'semantic_layer_counts': {
                            layer: 1 for layer in layers if layer != 'faults'}},
                ) as pmtiles, \
                    mock.patch.object(validate_national, '_tiff_header',
                                      return_value={'bytes': 256}):
                validate_national.validate_artifacts(qa, {'NV': state})
            self.assertEqual(qa.errors, [])
            zero_check.assert_called_once_with(qa, 'NV', faults)
            fault_call = next(call for call in pmtiles.call_args_list
                              if call.args[1] == ['faults'])
            self.assertFalse(fault_call.kwargs['verify_feature_properties'])
            self.assertIsNone(fault_call.kwargs['expected_state'])

    def test_strict_json_rejects_duplicate_keys_and_nan(self):
        for content in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.subTest(content=content), tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8') as artifact:
                artifact.write(content)
                artifact.flush()
                with self.assertRaises(ValueError):
                    validate_national._load_json(artifact.name)

    def test_cog_metadata_type_errors_fail_closed(self):
        fake = mock.Mock(stdout=json.dumps({
            'driverShortName': 'COG', 'size': ['100', 100], 'bands': [{}],
            'coordinateSystem': {'wkt': 'EPSG'}, 'geoTransform': [0, 1, 0, 0, 0, -1],
        }))
        with tempfile.NamedTemporaryFile() as artifact, mock.patch.object(
                validate_national.subprocess, 'run', return_value=fake):
            with self.assertRaises(ValueError):
                validate_national._tiff_header(artifact.name)

    def test_cog_must_have_wgs84_extent_in_the_registered_state(self):
        fake = mock.Mock(stdout=json.dumps({
            'driverShortName': 'COG', 'size': [100, 100],
            'bands': [{}],
            'coordinateSystem': {'wkt': 'EPSG:4326'},
            'geoTransform': [-120, .01, 0, 42, 0, -.01],
            'wgs84Extent': {'type': 'Polygon', 'coordinates': [[
                [-120, 35], [-114, 35], [-114, 42], [-120, 42], [-120, 35]]]},
        }))
        with tempfile.NamedTemporaryFile() as artifact, mock.patch.object(
                validate_national.subprocess, 'run', return_value=fake):
            meta = validate_national._tiff_header(
                artifact.name, expected_bounds=[[-120.01, 35, -114.04, 42.01]])
            self.assertEqual(meta['bounds'], [-120.0, 35.0, -114.0, 42.0])
            with self.assertRaisesRegex(ValueError, 'do not intersect'):
                validate_national._tiff_header(
                    artifact.name, expected_bounds=[[-80, 25, -79, 26]])

    def test_base_metal_prices_require_contiguous_checksummed_series(self):
        with open(validate_national.BASE_METAL_PRICES, encoding='utf-8') as source:
            data = json.load(source)
        data['prices_usd_per_lb']['Cu'].pop('1901')
        data['source']['workbooks']['Pb']['sha256'] = 'not-a-sha'
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as artifact:
            json.dump(data, artifact)
            artifact.flush()
            qa = validate_national.QA()
            validate_national.validate_base_metal_prices(qa, artifact.name)
        self.assertTrue(any('contiguous' in error for error in qa.errors))
        self.assertTrue(any('checksum' in error for error in qa.errors))

    def test_manifest_wrong_container_fails_without_crashing(self):
        with tempfile.TemporaryDirectory() as site:
            os.makedirs(os.path.join(site, 'data'))
            manifest = {'region': sorted(validate_national.ALL_STATES),
                        'tiled_layers': [], 'sites': [], 'claims': {}, 'totals': {}}
            with open(os.path.join(site, 'data', 'manifest.json'), 'w',
                      encoding='utf-8') as artifact:
                json.dump(manifest, artifact)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), mock.patch.object(
                    validate_national, 'expected_tiled_layers', return_value=[]):
                validate_national.validate_manifest_orphans(qa)
            self.assertTrue(any('manifest sites must be an object' in error
                                for error in qa.errors))

    def test_advertised_alaska_baselines_pass_exact_schema_in_progress(self):
        with tempfile.TemporaryDirectory() as site:
            baselines = _alaska_baseline_fixtures(site)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_alaska_baselines(
                    qa, baselines, release=False)
        self.assertEqual(qa.errors, [])

    def test_advertised_alaska_count_bytes_and_sha_fail_in_progress(self):
        with tempfile.TemporaryDirectory() as site:
            baselines = _alaska_baseline_fixtures(site)
            baselines['alaska_state_claims']['by_status']['pending'] = 2
            baselines['alaska_state_claims']['n'] += 1
            baselines['ardf']['bytes'] += 1
            baselines['ardf']['sha256'] = '0' * 64
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_alaska_baselines(
                    qa, baselines, release=False)
        self.assertTrue(any('status counts do not form' in error for error in qa.errors))
        self.assertTrue(any('source-OBJECTID partition counts/digest' in error
                            for error in qa.errors))
        self.assertTrue(any('bytes do not match' in error for error in qa.errors))
        self.assertTrue(any('sha256 does not match' in error for error in qa.errors))

    def test_advertised_alaska_source_record_missing_from_tiles_fails(self):
        with tempfile.TemporaryDirectory() as site:
            baselines = _alaska_baseline_fixtures(site)
            claims = baselines['alaska_state_claims']
            artifact = os.path.join(site, claims['file'])
            claim_properties = {
                layer: ['st', 'system', 'source_oid', 'serial', 'status', 'source_status',
                        'acres', 'part', 'url', 'lon', 'lat']
                for layer in ('active', 'pending', 'closed')
            }
            _alaska_pmtiles(
                artifact, ['active', 'pending', 'closed'], claim_properties, {
                    'schema': 'nwmm-alaska-pmtiles-v1',
                    'dataset': 'alaska_state_claims',
                    'snapshot': claims['retrieved'],
                    'counts': claims['base_delivery']['by_status'],
                    'staging_sha256': claims['staging_sha256'],
                }, feature_counts={'active': 1, 'pending': 1, 'closed': 3},
                geometry_type=3, maxzoom=13)
            claims['bytes'] = os.path.getsize(artifact)
            claims['sha256'] = _sha256(artifact)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_alaska_baselines(
                    qa, baselines, release=False)
        self.assertTrue(any(
            'ALASKA_STATE_CLAIMS active source records do not reconcile' in error
            for error in qa.errors))
        self.assertTrue(any(
            'ALASKA_STATE_CLAIMS active source-ID inventory does not match' in error
            for error in qa.errors))

    def test_advertised_alaska_zero_area_source_quality_fails(self):
        with tempfile.TemporaryDirectory() as site:
            baselines = _alaska_baseline_fixtures(site)
            baselines['alaska_state_claims']['source_quality'][
                'zero_area_features'] = 1
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_alaska_baselines(
                    qa, baselines, release=False)
        self.assertTrue(any(
            'collapsed/zero-area rows require a higher-precision refetch' in error
            for error in qa.errors))

    def test_advertised_alaska_required_properties_fail_in_progress(self):
        with tempfile.TemporaryDirectory() as site:
            baselines = _alaska_baseline_fixtures(
                site, missing_claim_property='url')
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_alaska_baselines(
                    qa, baselines, release=False)
        self.assertTrue(any('lacks required properties' in error for error in qa.errors))

    def test_advertised_alaska_numeric_properties_fail_if_string_typed(self):
        with tempfile.TemporaryDirectory() as site, mock.patch.object(
                validate_national, 'SITE', site):
            baselines = _alaska_baseline_fixtures(site)
            ardf = baselines['ardf']
            artifact = os.path.join(site, ardf['file'])
            _write_fixture_ardf(artifact, {
                'schema': 'nwmm-alaska-pmtiles-v1',
                'dataset': 'ardf',
                'snapshot': ardf['retrieved'],
                'counts': {'ardf': ardf['n']},
                'staging_sha256': ardf['staging_sha256'],
            }, string_numeric='group')
            ardf['bytes'] = os.path.getsize(artifact)
            ardf['sha256'] = _sha256(artifact)
            qa = validate_national.QA()
            validate_national.validate_alaska_baselines(
                qa, baselines, release=False)
        self.assertTrue(any('numeric property types' in error
                            for error in qa.errors))

    def test_advertised_ardf_omitted_browser_status_fails_full_scan(self):
        with tempfile.TemporaryDirectory() as site, mock.patch.object(
                validate_national, 'SITE', site):
            baselines = _alaska_baseline_fixtures(site)
            ardf = baselines['ardf']
            artifact = os.path.join(site, ardf['file'])
            _write_fixture_ardf(artifact, {
                'schema': 'nwmm-alaska-pmtiles-v1',
                'dataset': 'ardf',
                'snapshot': ardf['retrieved'],
                'counts': {'ardf': ardf['n']},
                'staging_sha256': ardf['staging_sha256'],
            }, missing_property='g_status')
            ardf['bytes'] = os.path.getsize(artifact)
            ardf['sha256'] = _sha256(artifact)
            qa = validate_national.QA()
            validate_national.validate_alaska_baselines(
                qa, baselines, release=False)
        self.assertTrue(any('lacks required properties' in error
                            for error in qa.errors))

    def test_advertised_ardf_status_sentinel_mismatch_fails_full_scan(self):
        with tempfile.TemporaryDirectory() as site, mock.patch.object(
                validate_national, 'SITE', site):
            baselines = _alaska_baseline_fixtures(site)
            ardf = baselines['ardf']
            artifact = os.path.join(site, ardf['file'])

            def mutate(row_index, properties):
                if row_index == 0:
                    properties['typ_status'] = (
                        validate_national.ARDF_SOURCE_VALUE_REPORTED)

            _write_fixture_ardf(artifact, {
                'schema': 'nwmm-alaska-pmtiles-v1',
                'dataset': 'ardf',
                'snapshot': ardf['retrieved'],
                'counts': {'ardf': ardf['n']},
                'staging_sha256': ardf['staging_sha256'],
            }, mutate=mutate)
            ardf['bytes'] = os.path.getsize(artifact)
            ardf['sha256'] = _sha256(artifact)
            qa = validate_national.QA()
            validate_national.validate_alaska_baselines(
                qa, baselines, release=False)
        self.assertTrue(any('sentinel/status semantics disagree' in error
                            for error in qa.errors))

    def test_advertised_ardf_blank_count_tamper_fails_contract(self):
        with tempfile.TemporaryDirectory() as site, mock.patch.object(
                validate_national, 'SITE', site):
            baselines = _alaska_baseline_fixtures(site)
            baselines['ardf']['source_quality']['source_blank_fields'][
                'site_type'] = 5
            qa = validate_national.QA()
            validate_national.validate_alaska_baselines(
                qa, baselines, release=False)
        self.assertTrue(any('source-quality schema is invalid' in error
                            for error in qa.errors))

    def test_advertised_ardf_blank_objectid_tamper_fails_contract(self):
        with tempfile.TemporaryDirectory() as site, mock.patch.object(
                validate_national, 'SITE', site):
            baselines = _alaska_baseline_fixtures(site)
            baselines['ardf']['source_quality'][
                'source_blank_site_type_objectids'][0] = 2829
            qa = validate_national.QA()
            validate_national.validate_alaska_baselines(
                qa, baselines, release=False)
        self.assertTrue(any('source-quality schema is invalid' in error
                            for error in qa.errors))

    def test_alaska_baselines_optional_in_progress_but_required_for_release(self):
        progress = validate_national.QA()
        validate_national.validate_alaska_baselines(progress, {}, release=False)
        self.assertEqual(progress.errors, [])
        release = validate_national.QA()
        validate_national.validate_alaska_baselines(release, {}, release=True)
        self.assertEqual(len(release.errors), 2)

    def test_nonclaim_open_ground_zero_is_not_na(self):
        states = {'NV': {'regime': 'claim'}, 'NY': {'regime': 'non_claim'}}
        grades = {
            'n': 2, 'st': ['NV', 'NY'], 'name': ['A mine', 'B mine'],
            'src': ['Report, p. 2', 'Report, p. 3'],
            'quote': ['a measured grade', 'another measured grade'],
            'url': ['https://example.test/a.pdf', 'https://example.test/b.pdf'],
            'x': [-116.0, -75.0], 'y': [39.0, 43.0], 'au': [1.0, 2.0],
            'open_ground': [
                {'status': 'measured', 'distance_m': 0, 'score': 0},
                {'status': 'measured', 'distance_m': 0, 'score': 0},
            ],
        }
        with tempfile.TemporaryDirectory() as site:
            folder = os.path.join(site, 'data', 'grades')
            os.makedirs(folder)
            with open(os.path.join(folder, 'grades.json'), 'w', encoding='utf-8') as artifact:
                json.dump(grades, artifact)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_scoring(qa, states=states, release=True)
        self.assertTrue(any('non-claim state NY must be not_applicable' in error
                            for error in qa.errors))

    def test_asserted_grade_counts_cannot_exceed_computed_rows(self):
        with tempfile.TemporaryDirectory() as site:
            evidence = self._compiled_grade_document()
            acceptance = self._publish_compiled_grade(site, evidence)
            acceptance['grades']['graded_mines'] = 25
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_grade_evidence(
                    qa, 'NV', {}, acceptance)
        self.assertTrue(any('graded_mines does not match compiled evidence' in error
                            for error in qa.errors))

    def test_compiled_grade_release_rejects_forged_evidence_and_metrics(self):
        def mutate_metrics(document):
            document['metrics']['graded_mines'] = 25

        def duplicate_mine(document):
            document['mines'].append(copy.deepcopy(document['mines'][0]))
            document['metrics']['graded_mines'] = 2

        def nonverbatim_quote(document):
            document['mines'][0]['evidence'][0]['quote_verbatim'] = False

        def unnumbered_page(document):
            document['mines'][0]['evidence'][0]['page_cite'] = 'appendix'

        def unknown_source(document):
            document['mines'][0]['evidence'][0]['source_id'] = 'undeclared-source'

        def insecure_source_url(document):
            document['primary_sources'][0]['url'] = 'http://example.test/source.pdf'

        def duplicate_low_source(document):
            document['low_endowment_finding']['sources'][1]['source_id'] = (
                document['low_endowment_finding']['sources'][0]['source_id'])

        def paraphrased_low_finding(document):
            document['low_endowment_finding']['sources'][0]['quote_verbatim'] = False

        def bad_pp610_page_hash(document):
            document['pp610']['districts'][0]['page_text_sha256'] = 'not-a-sha'

        mutations = {
            'dataset': lambda document: document.update(dataset='forged-dataset'),
            'metrics': mutate_metrics,
            'duplicate_mine': duplicate_mine,
            'quote': nonverbatim_quote,
            'page_cite': unnumbered_page,
            'source_id': unknown_source,
            'source_url': insecure_source_url,
            'low_source': duplicate_low_source,
            'low_quote': paraphrased_low_finding,
            'pp610_hash': bad_pp610_page_hash,
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as site:
                evidence = self._compiled_grade_document()
                mutation(evidence)
                acceptance = self._publish_compiled_grade(site, evidence)
                qa = validate_national.QA()
                with mock.patch.object(validate_national, 'SITE', site):
                    validate_national._validate_grade_evidence(
                        qa, 'NV', {}, acceptance)
                self.assertTrue(qa.errors, label)

    def test_grade_release_rejects_checksum_tamper_and_nonimmutable_path(self):
        with tempfile.TemporaryDirectory() as site:
            evidence = self._compiled_grade_document()
            acceptance = self._publish_compiled_grade(site, evidence)
            relative = acceptance['grades']['evidence_artifact']
            with open(os.path.join(site, relative), 'ab') as artifact:
                artifact.write(b' ')
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_grade_evidence(
                    qa, 'NV', {}, acceptance)
            self.assertTrue(any('invalid' in error or 'sha256' in error
                                for error in qa.errors))

            acceptance['grades']['evidence_artifact'] = 'data/evidence/nv.json'
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_grade_evidence(
                    qa, 'NV', {}, acceptance)
            self.assertTrue(any('immutable prefix' in error for error in qa.errors))

    def test_nested_pp610_explicit_zero_finding_is_release_consumable(self):
        with tempfile.TemporaryDirectory() as site:
            evidence = self._compiled_grade_document()
            evidence['pp610']['districts'] = []
            evidence['pp610']['district_count'] = 0
            evidence['pp610']['no_district_finding'] = {
                'finding': ('The complete PP 610 state review found no district entry '
                            'for this explicit zero-district acceptance fixture.'),
                'pages_reviewed': ['p. 1-12'],
                'review_complete': True,
            }
            evidence['metrics']['pp610_districts'] = 0
            acceptance = self._publish_compiled_grade(site, evidence)
            acceptance['district_anchor']['district_count'] = 0
            acceptance['district_anchor']['no_district_finding'] = (
                evidence['pp610']['no_district_finding']['finding'])
            state = {'historic_serials': [{
                'source_id': 'pp610',
                'url': 'https://pubs.usgs.gov/pp/0610/report.pdf'}]}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_grade_evidence(
                    qa, 'NV', state, acceptance)
                validate_national._validate_district_anchor_evidence(
                    qa, 'NV', state, acceptance)
            self.assertEqual(qa.errors, [])

    def test_quantitative_25_mine_two_source_compiler_artifact_passes(self):
        with tempfile.TemporaryDirectory() as site:
            evidence = self._compiled_grade_document()
            evidence['primary_sources'].append(self._grade_source('nv-primary-2'))
            template = evidence['mines'][0]
            mines = []
            for index in range(25):
                mine = copy.deepcopy(template)
                mine['mine_id'] = f'nv-mine-{index + 1}'
                mine['name'] = f'Nevada reviewed mine {index + 1}'
                mine['district'] = f'Nevada district {index + 1}'
                row = mine['evidence'][0]
                row['evidence_id'] = f'nv-grade-{index + 1}'
                row['source_id'] = ('nv-primary' if index % 2 == 0
                                    else 'nv-primary-2')
                row['page_cite'] = f'p. {index + 1}'
                row['verbatim_quote'] = (
                    f'The unique reviewed grade statement for mine {index + 1}.')
                row['page_text_sha256'] = hashlib.sha256(
                    f'grade-page-{index + 1}'.encode()).hexdigest()
                mines.append(mine)
            evidence['mines'] = mines
            evidence['metrics'].update({
                'graded_mines': 25, 'primary_sources': 2,
                'verbatim_quotes': 25, 'page_cites': 25,
                'primary_source_ids': ['nv-primary', 'nv-primary-2'],
            })
            evidence['grade_requirement'] = {
                'status': 'meets_quantitative_bar',
                'done_gate_eligible': True, 'gaps': [],
            }
            evidence['low_endowment_finding'] = None
            evidence['input_artifacts'].pop('low_endowment')
            acceptance = self._publish_compiled_grade(site, evidence)
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national._validate_grade_evidence(
                    qa, 'NV', {}, acceptance)
            self.assertEqual(qa.errors, [])

    def test_require_all_checks_artifacts_even_for_disabled_states(self):
        state = {
            'release': {'enabled': False}, 'regime': 'non_claim',
            'geology': {'browser_path': 'missing-geology.pmtiles'},
            'faults': {'browser_path': 'missing-faults.pmtiles'},
            'aeromag': {'browser_path': 'missing-aeromag.tif'},
            'land_context': {'browser_path': 'missing-context.pmtiles'},
            'claim_systems': [],
        }
        qa = validate_national.QA()
        validate_national.validate_artifacts(qa, {'NY': state}, require_all=False)
        self.assertEqual(qa.errors, [])
        validate_national.validate_artifacts(qa, {'NY': state}, require_all=True)
        self.assertEqual(len(qa.errors), 4)

    def test_require_all_applies_full_registry_gate_to_disabled_state(self):
        sys.path.insert(0, os.path.join(ROOT, 'pipelines'))
        import state_registry
        state = copy.deepcopy(state_registry.load_state('NY'))
        self.assertFalse(state['release']['enabled'])
        qa = validate_national.QA()
        validate_national.validate_release_registry_candidates(qa, {'NY': state})
        self.assertTrue(any('release.status=done but one or more DONE gates' in error
                            for error in qa.errors))
        self.assertTrue(any('grade acceptance needs 25 mines' in error
                            for error in qa.errors))

    def test_browser_contract_detects_legacy_fetch_and_missing_deploy_guards(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            infra = os.path.join(root, 'infra')
            os.makedirs(site)
            os.makedirs(infra)
            with open(os.path.join(site, 'index.html'), 'w', encoding='utf-8') as html:
                html.write("fetch('data/claims/nv_active.json')")
            with open(os.path.join(infra, 'deploy.sh'), 'w', encoding='utf-8') as deploy:
                deploy.write('aws s3 sync site s3://bucket')
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), mock.patch.object(
                    validate_national, 'ROOT', root):
                validate_national.validate_browser_delivery_contract(qa, release=True)
        self.assertTrue(any('whole-state' in error for error in qa.errors))
        self.assertTrue(any('sync must exclude' in error for error in qa.errors))
        self.assertTrue(any('must remove' in error for error in qa.errors))

    def test_browser_contract_rejects_statewide_geology_and_land_context_fetches(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            infra = os.path.join(root, 'infra')
            os.makedirs(site)
            os.makedirs(infra)
            with open(os.path.join(site, 'index.html'), 'w', encoding='utf-8') as html:
                html.write("fetch('data/geology/nv.geojson'); "
                           "jget('data/land-context/mi.geojson')")
            with open(os.path.join(infra, 'deploy.sh'), 'w', encoding='utf-8') as deploy:
                deploy.write(
                    "aws s3 sync site/data s3://$bucket/data --exclude 'claims/*' "
                    "--exclude 'sites/*'\n"
                    'aws s3 rm "s3://$bucket/data/claims/" --recursive\n'
                    'aws s3 rm "s3://$bucket/data/sites/" --recursive\n')
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), mock.patch.object(
                    validate_national, 'ROOT', root):
                validate_national.validate_browser_delivery_contract(qa, release=True)
        self.assertTrue(any('whole-state JSON/GeoJSON' in error for error in qa.errors))
        self.assertTrue(any('statewide JSON/GeoJSON path literal' in error
                            for error in qa.errors))

    def test_browser_contract_rejects_public_boundary_geojson(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            infra = os.path.join(root, 'infra')
            boundaries = os.path.join(site, 'data', 'boundaries')
            os.makedirs(boundaries)
            os.makedirs(infra)
            with open(os.path.join(site, 'index.html'), 'w', encoding='utf-8') as html:
                html.write('<!doctype html>')
            with open(os.path.join(boundaries, 'counties.json'), 'w', encoding='utf-8') as data:
                data.write('{"type":"FeatureCollection","features":[]}')
            with open(os.path.join(infra, 'deploy.sh'), 'w', encoding='utf-8') as deploy:
                deploy.write(
                    "aws s3 sync site/data s3://$bucket/data --exclude 'claims/*' "
                    "--exclude 'sites/*'\n"
                    'aws s3 rm "s3://$bucket/data/claims/" --recursive\n'
                    'aws s3 rm "s3://$bucket/data/sites/" --recursive\n')
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), mock.patch.object(
                    validate_national, 'ROOT', root):
                validate_national.validate_browser_delivery_contract(qa, release=True)
        self.assertTrue(any('administrative boundary JSON' in error
                            for error in qa.errors))

    def test_browser_contract_rejects_public_national_geophysics_json(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            infra = os.path.join(root, 'infra')
            geophys = os.path.join(site, 'data', 'geophys')
            os.makedirs(geophys)
            os.makedirs(infra)
            with open(os.path.join(site, 'index.html'), 'w', encoding='utf-8') as html:
                html.write('<!doctype html>')
            with open(os.path.join(geophys, 'surveys.json'), 'w', encoding='utf-8') as data:
                data.write('{"n":0,"surveys":[]}')
            with open(os.path.join(infra, 'deploy.sh'), 'w', encoding='utf-8') as deploy:
                deploy.write(
                    "aws s3 sync site/data s3://$bucket/data --exclude 'claims/*' "
                    "--exclude 'sites/*'\n"
                    'aws s3 rm "s3://$bucket/data/claims/" --recursive\n'
                    'aws s3 rm "s3://$bucket/data/sites/" --recursive\n')
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site), mock.patch.object(
                    validate_national, 'ROOT', root):
                validate_national.validate_browser_delivery_contract(qa, release=True)
        self.assertTrue(any('national geophysics JSON' in error
                            for error in qa.errors))

    def test_public_tile_tree_rejects_hidden_staging_and_raw_build_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            data = os.path.join(site, 'data')
            tiles = os.path.join(data, 'tiles')
            os.makedirs(os.path.join(tiles, 'context'))
            os.makedirs(os.path.join(tiles, 'states', 'nv',
                                     '.nv-baselines-working'))
            manifest = {
                'ws56': {}, 'national_baselines': {}, 'tiled_layers': []}
            for relative in (
                    'tiles/context/admin.pmtiles',
                    'tiles/states/nv/orphan.pmtiles',
                    'tiles/states/nv/.nv-baselines-working/input.geojsonseq',
                    'private-looking/source.zip'):
                path = os.path.join(data, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as artifact:
                    artifact.write(b'x')
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_public_tile_tree(qa, manifest)
        self.assertTrue(any('hidden/temp directory' in error for error in qa.errors))
        self.assertTrue(any('undeclared PMTiles artifact' in error for error in qa.errors))
        self.assertTrue(any('staging/build input' in error and 'source.zip' in error
                            for error in qa.errors))

    def test_public_tile_tree_allows_only_manifest_advertised_pmtiles(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            paths = (
                'data/tiles/context/admin.pmtiles',
                'data/tiles/geophys/surveys.pmtiles',
                'data/tiles/national/geology.pmtiles',
                'data/tiles/states/nv/geology.pmtiles',
            )
            for relative in paths:
                path = os.path.join(site, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as artifact:
                    artifact.write(b'x')
            manifest = {
                'ws56': {'geophys_surveys': {
                    'format': 'pmtiles',
                    'file': 'data/tiles/geophys/surveys.pmtiles'}},
                'national_baselines': {
                    'admin': validate_national._admin_expected_descriptor(),
                    'geology': {
                        'format': 'pmtiles',
                        'file': 'data/tiles/national/geology.pmtiles'}},
                'tiled_layers': [{
                    'delivery': 'pmtiles',
                    'url': 'data/tiles/states/nv/geology.pmtiles'}],
            }
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_public_tile_tree(qa, manifest)
        self.assertEqual(qa.errors, [])

    def test_public_tile_tree_allows_only_the_alaska_precision_nested_slot(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            baselines = _alaska_baseline_fixtures(site)
            baselines['admin'] = validate_national._admin_expected_descriptor()
            for relative in ('data/tiles/context/admin.pmtiles',
                             'data/tiles/claims/arbitrary-nested.pmtiles'):
                path = os.path.join(site, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as artifact:
                    artifact.write(b'x')
            baselines['alaska_state_claims']['unrecognized_nested'] = {
                'format': 'pmtiles',
                'file': 'data/tiles/claims/arbitrary-nested.pmtiles',
            }
            manifest = {'ws56': {}, 'national_baselines': baselines,
                        'tiled_layers': []}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_public_tile_tree(qa, manifest)
        self.assertEqual(len(qa.errors), 1)
        self.assertIn('arbitrary-nested.pmtiles', qa.errors[0])

    def test_public_tile_tree_rejects_malformed_alaska_precision_slot(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            baselines = _alaska_baseline_fixtures(site)
            baselines['admin'] = validate_national._admin_expected_descriptor()
            precision = baselines['alaska_state_claims']['precision_overflow']
            precision.pop('note')
            admin = os.path.join(site, 'data', 'tiles', 'context', 'admin.pmtiles')
            os.makedirs(os.path.dirname(admin), exist_ok=True)
            with open(admin, 'wb') as artifact:
                artifact.write(b'x')
            manifest = {'ws56': {}, 'national_baselines': baselines,
                        'tiled_layers': []}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_public_tile_tree(qa, manifest)
        self.assertEqual(len(qa.errors), 1)
        self.assertIn('ak-state-precision.pmtiles', qa.errors[0])

    def test_public_tile_tree_rejects_alaska_precision_path_substitution(self):
        with tempfile.TemporaryDirectory() as root:
            site = os.path.join(root, 'site')
            baselines = _alaska_baseline_fixtures(site)
            baselines['admin'] = validate_national._admin_expected_descriptor()
            arbitrary = 'data/tiles/states/ut/not-really-alaska.pmtiles'
            arbitrary_path = os.path.join(site, arbitrary)
            os.makedirs(os.path.dirname(arbitrary_path), exist_ok=True)
            with open(arbitrary_path, 'wb') as artifact:
                artifact.write(b'x')
            baselines['alaska_state_claims']['precision_overflow'][
                'file'] = arbitrary
            admin = os.path.join(site, 'data', 'tiles', 'context', 'admin.pmtiles')
            os.makedirs(os.path.dirname(admin), exist_ok=True)
            with open(admin, 'wb') as artifact:
                artifact.write(b'x')
            manifest = {'ws56': {}, 'national_baselines': baselines,
                        'tiled_layers': []}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', site):
                validate_national.validate_public_tile_tree(qa, manifest)
        self.assertEqual(len(qa.errors), 2)
        self.assertTrue(any('ak-state-precision.pmtiles' in error
                            for error in qa.errors))
        self.assertTrue(any('not-really-alaska.pmtiles' in error
                            for error in qa.errors))


if __name__ == '__main__':
    unittest.main()
