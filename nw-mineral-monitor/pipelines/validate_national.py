#!/usr/bin/env python3
"""Executable WS11 progress/release gate and scale-regression validator."""
from __future__ import annotations

import argparse
import copy
import gzip
import json
import os
import re
import struct
import subprocess
import sys
import hashlib
import math
import zlib

from build_coverage import build as build_coverage, encoded as coverage_bytes, OUT as COVERAGE
from build_inputs import (BUILD_INPUTS, MANIFEST as BUILD_INPUT_MANIFEST,
                          artifact_path as build_input_path,
                          load_manifest as load_build_input_manifest)
from build_national_grade_evidence import (
    DATASET as GRADE_EVIDENCE_DATASET,
    PublicationError as GradeEvidenceError,
    canonical_bytes as canonical_grade_evidence_bytes,
    validate_compiled_state_document,
)
from build_ci_acceptance_evidence import (
    AcceptanceError as CIAcceptanceError,
    validate_release_evidence_file as validate_ci_release_evidence_file,
)
from build_zero_inventory_evidence import (
    ZeroInventoryError,
    validate_evidence_file as validate_zero_inventory_evidence_file,
)
from state_registry import (ALL_STATES, CLAIM_STATES, RegistryError, load_states,
                            validate_registry, validate_state)
from reconcile_manifest import (baseline_totals as expected_baseline_totals,
                                tiled_layers as expected_tiled_layers)

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'infra')))
from spatial_clip import StateClipIndex

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
BUDGETS = os.path.join(ROOT, 'ci', 'budgets.json')
STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')
BASE_METAL_PRICES = os.path.join(HERE, 'config', 'base_metal_prices.json')
ALASKA_STATE_CLAIMS_SOURCE = (
    'https://arcgis.dnr.alaska.gov/arcgis/rest/services/OpenData/'
    'NaturalResource_StateMiningClaim/MapServer')
ARDF_SOURCE = (
    'https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/services/'
    'ARDF_features/FeatureServer/0')
ALASKA_SNAPSHOT_DATE = '2026-08-13'
ARDF_EXPECTED_COUNT = 7_692
ARDF_EXPECTED_STAGING_SHA256 = (
    '9e35dd394f1e1f2702e1d309bf6ab6e49859300ff643dd00d6dd755e1cc95be3')
ARDF_SOURCE_SNAPSHOT_CONTRACT = 'arcgis-objectids-double-pass-v1'
ARDF_EXPECTED_SOURCE_SNAPSHOT_INVENTORY = {
    'n': 7_692,
    'minimum_object_id': 1,
    'maximum_object_id': 7_719,
    'object_ids_sha256':
        '7c5cd4f66b8790294fecad6d9ab0dde10b647cdab9d98f1a5b4e2832a668bb8d',
    'layer_metadata_sha256':
        '0bb304fa62e4e429496c0f25e1fed3bb295680312bbe02809de1d027c938687b',
    'records_sha256':
        'c7501d1e1174a1e5ee8b11bcff651930a11b4ddada73adb02c3157d0773c93bb',
}
ARDF_BLANK_SITE_TYPE_OBJECTIDS = [2828, 3251, 3367, 3662, 5307, 6568]
# Artifact-side identities for the same six reviewed source rows. These bind
# the source OBJECTID evidence above to the browser-visible ARDF identifiers.
ARDF_BLANK_SITE_TYPE_IDS = frozenset(
    ('JU177', 'LC054', 'LG111', 'MC152', 'PE117', 'SR276'))
ARDF_SOURCE_VALUE_MISSING = 'Not reported by source'
ARDF_SOURCE_VALUE_REPORTED = 'reported'
ARDF_SOURCE_VALUE_BLANK = 'source_blank'
ARDF_EXPECTED_SOURCE_QUALITY = {
    'source_state_blanks': 1,
    'source_blank_fields': {
        'commodities_main': 75,
        'district': 400,
        'site_type': 6,
    },
    'source_blank_site_type_objectids': ARDF_BLANK_SITE_TYPE_OBJECTIDS,
    'text_truncations': {
        'age': 216,
        'geo': 3985,
        'loc': 2120,
        'model': 1,
        'work': 1143,
    },
}
GEOPHYS_SOURCES = [
    ('https://energy.usgs.gov/arcgis/rest/services/Hosted/'
     'Airborne_Geophysical_Surveys/FeatureServer/0'),
    ('https://energy.usgs.gov/arcgis/rest/services/MRData/'
     'Earth_MRI_Acquisitions/MapServer/3'),
]
ADMIN_SOURCE = ('https://tigerweb.geo.census.gov/arcgis/rest/services/'
                'TIGERweb/State_County/MapServer')
ADMIN_BOUNDARIES_SOURCE = (
    'U.S. Census Bureau TIGERweb State_County MapServer, '
    'January 1 2025 vintage')
ADMIN_ARTIFACT_SHA256 = (
    '94c3a78b2ca17f02223e6d5161afde763a370b515e710723e76395b520e2c3df')
ADMIN_ARTIFACT_BYTES = 7_743_967
ADMIN_STATE_CLIPS_SHA256 = (
    '33c09d367d74a1ce0c88934d4adb548557733bf7da9105be039f5f16ed22c552')
ADMIN_STATE_CLIPS_BYTES = 707_923
ADMIN_BOUNDS = [-179.23109, 24.39631, 179.85968, 71.43979]
ADMIN_COUNTS = {'states': 49, 'counties': 3_138}
ADMIN_FIPS_IDS_SHA256 = {
    'states': '155b69af91d4816940212a1ab613d9afaf6dd3219eaa9bd1ef63037ba1bcaef4',
    'counties': 'a37a3c2581375c33746a4fe50ab907b9fdde986521113b9f508d4fb155b48da1',
}
ADMIN_PROPERTIES_SHA256 = {
    'states': '9557f8d931cbb98a3d55a98dee359d52544aa9396e57ff510fcb9633f2fcb4b3',
    'counties': '8af5dcb479d0312f1cf012d909231e1b5d53e25557fa9798671368650c40aa64',
}
ADMIN_MAXZOOM_INSTANCES = {'states': 11_264, 'counties': 25_713}
ADMIN_SOURCE_INVENTORY_SHA256 = (
    'efb0489ec81d5533c6c8bfead365aea745f00b8cca7c083b37001f58859eaae7')
ADMIN_SOURCE_SNAPSHOTS = {
    'states': {
        'bytes': 713_160,
        'sha256': 'f758b07d69956b5da501d523989d65a29bbaa050470d1ef0d766cd4354f85d0a',
        'source_snapshot_id':
            '1bb64ba3a2a7ed8bcd683b6ca3b2cdcb6083e7ed3d4c15f4b8223814af8f4c51',
        'metadata_sha256':
            '62655956508712191e87ee4efb3cd1e4b93a94e8ebd530d6189b49ef9b06cedc',
        'object_ids_sha256':
            '88df756a9dff5c39700265c2069de3e2bec96600f29637b9b02af36f3b036f07',
        'records_sha256':
            '5712b39001299ddb95b13c1b2cb78752c088992d35d36d22c52a696d26fc7b0d',
    },
    'counties': {
        'bytes': 5_103_512,
        'sha256': '79598c0c3669e5915df8ace40e4e1aebac6c2fa6e54a388e6e7ab8839866dce1',
        'source_snapshot_id':
            'e6e069adfb3ff75253619dda734ea105246ebc36f4559b14c694149a7d5bc56a',
        'metadata_sha256':
            '71920ac98c2d332d35ce362df763484d58a9eaaa8bcb3da51083a52cf2c89fa5',
        'object_ids_sha256':
            'e58e793bec14e090f1acb927ed900287ddf06d2b8cc9196e7327c44520c507e9',
        'records_sha256':
            'c476fe0a3326f6c1aca62ead0e5d90d857efbda909cf6cf220ec699afa9029b9',
    },
}
ADMIN_METADATA_SHA256 = (
    '6f1b8d8c3f4a998cee96f1ccf8aef5fa293c964814ab11676a422d0cb5bdf5f2')
ADMIN_GENERATOR_OPTIONS_SHA256 = (
    'd1d71bb0691ecc45e5b26876020b84504b57d292a8dffc46773fc55751b6caeb')
ADMIN_ATTRIBUTION = 'U.S. Census Bureau TIGERweb, January 1 2025 vintage'
ADMIN_TIPPECANOE_RE = re.compile(r'tippecanoe v?\d+\.\d+(?:\.\d+)?')
ADMIN_STATE_FIPS = {
    '01': 'AL', '02': 'AK', '04': 'AZ', '05': 'AR', '06': 'CA',
    '08': 'CO', '09': 'CT', '10': 'DE', '12': 'FL', '13': 'GA',
    '16': 'ID', '17': 'IL', '18': 'IN', '19': 'IA', '20': 'KS',
    '21': 'KY', '22': 'LA', '23': 'ME', '24': 'MD', '25': 'MA',
    '26': 'MI', '27': 'MN', '28': 'MS', '29': 'MO', '30': 'MT',
    '31': 'NE', '32': 'NV', '33': 'NH', '34': 'NJ', '35': 'NM',
    '36': 'NY', '37': 'NC', '38': 'ND', '39': 'OH', '40': 'OK',
    '41': 'OR', '42': 'PA', '44': 'RI', '45': 'SC', '46': 'SD',
    '47': 'TN', '48': 'TX', '49': 'UT', '50': 'VT', '51': 'VA',
    '53': 'WA', '54': 'WV', '55': 'WI', '56': 'WY',
}


def _reject_json_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f'duplicate JSON object key {key!r}')
        out[key] = value
    return out


def _load_json(path):
    with open(path, encoding='utf-8') as json_file:
        return json.load(json_file, parse_constant=_reject_json_constant,
                         object_pairs_hook=_reject_duplicate_keys)


def _clip_indexes():
    raw = _load_json(STATE_CLIPS)
    if not isinstance(raw, dict) or not isinstance(raw.get('states'), dict):
        raise ValueError('state clips must contain a states object')
    if set(raw['states']) != set(ALL_STATES):
        raise ValueError('state clips must contain exactly the 49 WS11 states')
    out = {}
    for code, geometry in raw['states'].items():
        if not isinstance(geometry, dict) or geometry.get('type') not in (
                'Polygon', 'MultiPolygon'):
            raise ValueError(f'{code} state clip is not Polygon/MultiPolygon')
        out[code] = StateClipIndex(geometry)
    return out


class QA:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def fail(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)


def _is_int(value, minimum=None):
    return (isinstance(value, int) and not isinstance(value, bool) and
            (minimum is None or value >= minimum))


def _is_finite(value, minimum=None):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and (minimum is None or value >= minimum))


def _text(value, minimum=1):
    return isinstance(value, str) and len(value.strip()) >= minimum


def _https(value):
    return _text(value) and re.fullmatch(r'https://[^\s]+', value.strip()) is not None


def _canonical_json_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _admin_expected_description():
    return _canonical_json_bytes({
        'schema': 'nwmm-national-admin-pmtiles-v1',
        'vintage': 'January 1 2025',
        'counts': ADMIN_COUNTS,
        'fips_ids_sha256': ADMIN_FIPS_IDS_SHA256,
        'inventory_sha256': ADMIN_SOURCE_INVENTORY_SHA256,
    }).decode('utf-8')


def _admin_expected_descriptor():
    inventories = {
        layer: {
            'status': 'complete_at_retrieval',
            'source_records': ADMIN_COUNTS[layer],
            'maxzoom_unique_tiled_ids': ADMIN_COUNTS[layer],
            'maxzoom_feature_instances': ADMIN_MAXZOOM_INSTANCES[layer],
            'ids_sha256': ADMIN_FIPS_IDS_SHA256[layer],
            'properties_sha256': ADMIN_PROPERTIES_SHA256[layer],
        }
        for layer in ('states', 'counties')
    }
    return {
        'schema_version': 1,
        'format': 'pmtiles',
        'file': 'data/tiles/context/admin.pmtiles',
        'source': {
            'authority': 'U.S. Census Bureau',
            'service': ADMIN_SOURCE,
            'vintage': 'January 1 2025',
            'layers': {'states': 0, 'counties': 1},
        },
        'source_snapshot': {
            'contract': 'tigerweb-objectids-double-pass-v1',
            'inventory_sha256': ADMIN_SOURCE_INVENTORY_SHA256,
            'layers': copy.deepcopy(ADMIN_SOURCE_SNAPSHOTS),
        },
        'retrieved': '2026-08-13',
        'source_layers': ['states', 'counties'],
        'required_properties': {
            'states': ['fips', 'name', 'st'],
            'counties': ['fips', 'name', 'st'],
        },
        'counts': dict(ADMIN_COUNTS),
        'fips_id_inventories': inventories,
        'state_clips': {
            'file': 'infra/state_clips.json',
            'bytes': ADMIN_STATE_CLIPS_BYTES,
            'sha256': ADMIN_STATE_CLIPS_SHA256,
        },
        'bytes': ADMIN_ARTIFACT_BYTES,
        'sha256': ADMIN_ARTIFACT_SHA256,
        'minzoom': 0,
        'maxzoom': 10,
        'bounds': list(ADMIN_BOUNDS),
        'deterministic_rebuild': {
            'status': 'two_byte_identical_builds',
            'bytes': ADMIN_ARTIFACT_BYTES,
            'sha256': ADMIN_ARTIFACT_SHA256,
        },
        'reproducible_metadata': {
            'status': 'complete_path_free_reproducible_metadata',
            'name': 'admin.pmtiles',
            'metadata_sha256': ADMIN_METADATA_SHA256,
            'generator_options_sha256': ADMIN_GENERATOR_OPTIONS_SHA256,
        },
    }


def _admin_descriptor_schema_valid(entry):
    """True only for the independently pinned accepted candidate descriptor."""
    if not isinstance(entry, dict):
        return False
    try:
        # Dict equality alone treats ``True`` as equal to ``1`` and integral
        # floats as equal to integers. Canonical JSON bytes retain those schema
        # distinctions while remaining insensitive to object member order.
        return (_canonical_json_bytes(entry) ==
                _canonical_json_bytes(_admin_expected_descriptor()))
    except (TypeError, ValueError):
        return False


def _admin_ids_sha256(ids):
    return hashlib.sha256(json.dumps(
        sorted(ids), separators=(',', ':'), allow_nan=False
    ).encode('ascii')).hexdigest()


def _admin_properties_sha256(rows):
    values = sorted([feature_id, *signature]
                    for feature_id, signature in rows.items())
    return hashlib.sha256(_canonical_json_bytes(values)).hexdigest()


def _admin_pmtiles_metadata(path):
    """Read the complete strict PMTiles metadata object for repro validation."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as archive:
            header = archive.read(127)
            if len(header) != 127 or header[:8] != b'PMTiles\x03':
                raise ValueError('admin artifact is not PMTiles v3')
            metadata_offset, metadata_length = struct.unpack_from('<2Q', header, 24)
            if (metadata_offset < 127 or metadata_length <= 0 or
                    metadata_offset + metadata_length > size):
                raise ValueError('admin PMTiles metadata range is invalid')
            archive.seek(metadata_offset)
            payload = archive.read(metadata_length)
        if len(payload) != metadata_length:
            raise ValueError('admin PMTiles metadata is truncated')
        payload = _decompress_pmtiles(payload, header[97])
        metadata = json.loads(
            payload, parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f'cannot read strict admin PMTiles metadata: {exc}') from exc
    if not isinstance(metadata, dict):
        raise ValueError('admin PMTiles metadata top level must be an object')
    return metadata


def _admin_feature_contract():
    seen = {'states': {}, 'counties': {}}
    maxzoom_ids = {'states': set(), 'counties': set()}

    def validate(layer, feature, at_maxzoom):
        if layer not in seen or not isinstance(feature, dict):
            raise ValueError(f'unexpected admin feature layer {layer!r}')
        properties = feature.get('properties')
        required = {'fips', 'name', 'st'}
        if not isinstance(properties, dict) or set(properties) != required:
            raise ValueError(
                f'admin {layer} feature properties must be exactly '
                f'{sorted(required)}')
        feature_id = feature.get('id')
        if (not isinstance(feature_id, int) or isinstance(feature_id, bool) or
                feature_id < 0):
            raise ValueError(f'admin {layer} feature has invalid top-level ID')
        fips = properties.get('fips')
        name = properties.get('name')
        state = properties.get('st')
        pattern = r'\d{2}' if layer == 'states' else r'\d{5}'
        state_fips = fips if layer == 'states' else (
            fips[:2] if isinstance(fips, str) else None)
        if (not isinstance(fips, str) or re.fullmatch(pattern, fips) is None or
                int(fips) != feature_id or
                ADMIN_STATE_FIPS.get(state_fips) != state or
                not isinstance(name, str) or not name.strip() or
                name != name.strip() or
                any(ord(char) < 32 for char in name)):
            raise ValueError(f'admin {layer} feature FIPS/name/state is invalid')
        signature = (fips, name, state)
        previous = seen[layer].setdefault(feature_id, signature)
        if previous != signature:
            raise ValueError(
                f'admin {layer} FIPS {fips} changes properties across tiles')
        if at_maxzoom:
            maxzoom_ids[layer].add(feature_id)

    return validate, {'seen': seen, 'maxzoom_ids': maxzoom_ids}


def _validate_admin_clip_bindings(qa, baselines):
    """Tie every advertised state-survey clip reference to admin generation."""
    if not isinstance(baselines, dict):
        return

    def descriptors(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == 'spatial_clip':
                    yield nested
                else:
                    yield from descriptors(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from descriptors(nested)

    recognized = set().union(*(
        group['keys'] for group in _STATE_SURVEY_BASELINE_GROUPS.values()))
    for baseline_id in sorted(recognized & set(baselines)):
        for index, clip in enumerate(descriptors(baselines[baseline_id])):
            if (not isinstance(clip, dict) or
                    clip.get('artifact') != 'infra/state_clips.json' or
                    clip.get('artifact_sha256') != ADMIN_STATE_CLIPS_SHA256):
                qa.fail(
                    f'national {baseline_id} spatial_clip[{index}] is not '
                    'bound to the exact admin state-clips generation')


def validate_admin_baseline(qa, manifest, *, pmtiles_header=None):
    """Validate the independently pinned 2025 TIGERweb admin generation."""
    if pmtiles_header is None:
        pmtiles_header = _pmtiles_header
    sources = manifest.get('sources') if isinstance(manifest, dict) else None
    if (not isinstance(sources, dict) or
            sources.get('boundaries') != ADMIN_BOUNDARIES_SOURCE):
        qa.fail('manifest sources.boundaries is not the exact TIGERweb '
                'January 1 2025 admin source')
    baselines = (manifest.get('national_baselines')
                 if isinstance(manifest, dict) else None)
    descriptor = baselines.get('admin') if isinstance(baselines, dict) else None
    if not _admin_descriptor_schema_valid(descriptor):
        qa.fail('national admin baseline descriptor is missing or differs from '
                'the independently pinned accepted generation')
        return

    artifact = os.path.join(SITE, 'data', 'tiles', 'context', 'admin.pmtiles')
    if not os.path.isfile(artifact):
        qa.fail('national administrative PMTiles artifact is missing')
        return
    try:
        if (os.path.getsize(artifact) != ADMIN_ARTIFACT_BYTES or
                _sha256_file(artifact) != ADMIN_ARTIFACT_SHA256):
            qa.fail('national administrative PMTiles bytes/SHA-256 do not '
                    'match the accepted clean-room generation')
            return
    except OSError as exc:
        qa.fail(f'national administrative PMTiles cannot be fingerprinted: {exc}')
        return

    try:
        if (os.path.getsize(STATE_CLIPS) != ADMIN_STATE_CLIPS_BYTES or
                _sha256_file(STATE_CLIPS) != ADMIN_STATE_CLIPS_SHA256):
            qa.fail('authoritative state clips bytes/SHA-256 do not match the '
                    'admin descriptor and reviewed state-survey bindings')
            return
        clips = _load_json(STATE_CLIPS)
        if (not isinstance(clips, dict) or set(clips) != {
                'schema_version', 'source', 'note', 'states'} or
                clips.get('schema_version') != 1 or
                clips.get('source') !=
                (ADMIN_SOURCE + '/0 (States) and /1 (Counties), '
                 'January 1 2025 vintage') or
                not isinstance(clips.get('states'), dict) or
                set(clips['states']) != set(ALL_STATES)):
            qa.fail('authoritative state clips schema/source/inventory is invalid')
            return
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'authoritative state clips cannot be validated: {exc}')
        return

    try:
        metadata = _admin_pmtiles_metadata(artifact)
    except ValueError as exc:
        qa.fail(f'national administrative PMTiles metadata: {exc}')
        return
    options = metadata.get('generator_options')
    serialized = _canonical_json_bytes(metadata).decode('utf-8')
    if (metadata.get('name') != 'admin.pmtiles' or
            metadata.get('description') != _admin_expected_description() or
            metadata.get('attribution') != ADMIN_ATTRIBUTION or
            not isinstance(metadata.get('generator'), str) or
            ADMIN_TIPPECANOE_RE.fullmatch(metadata['generator']) is None or
            not isinstance(options, str) or '/' in options or '\\' in options or
            hashlib.sha256(_canonical_json_bytes(metadata)).hexdigest() !=
            ADMIN_METADATA_SHA256 or
            hashlib.sha256(_canonical_json_bytes(options)).hexdigest() !=
            ADMIN_GENERATOR_OPTIONS_SHA256 or
            any(marker in serialized for marker in (
                '/private/', '/tmp/', '/var/', '/Users/', 'nwmm-admin-build-'))):
        qa.fail('national administrative PMTiles metadata is not the exact '
                'path-free reproducible generation')
        return

    feature_validator, evidence = _admin_feature_contract()
    try:
        scan = pmtiles_header(
            artifact, ['states', 'counties'],
            {'states': ['fips', 'name', 'st'],
             'counties': ['fips', 'name', 'st']},
            verify_feature_properties=True, collect_feature_ids=True,
            expected_geometry_types={'states': {3}, 'counties': {3}},
            feature_validator=feature_validator)
    except (OSError, ValueError) as exc:
        qa.fail(f'national administrative PMTiles semantic scan failed: {exc}')
        return
    if (scan.get('source_layers') != ['counties', 'states'] or
            scan.get('minzoom') != 0 or scan.get('maxzoom') != 10 or
            scan.get('bounds') != ADMIN_BOUNDS or
            scan.get('field_types') != {
                'counties': {'fips': 'String', 'name': 'String', 'st': 'String'},
                'states': {'fips': 'String', 'name': 'String', 'st': 'String'},
            }):
        qa.fail('national administrative PMTiles layer/zoom/bounds/property '
                'metadata differs from the accepted generation')
        return
    for layer in ('states', 'counties'):
        ids = (scan.get('maxzoom_feature_ids') or {}).get(layer)
        instances = (scan.get('maxzoom_feature_instances') or {}).get(layer)
        seen = evidence['seen'][layer]
        if (not isinstance(ids, list) or ids != sorted(set(ids)) or
                len(ids) != ADMIN_COUNTS[layer] or
                _admin_ids_sha256(ids) != ADMIN_FIPS_IDS_SHA256[layer] or
                evidence['maxzoom_ids'][layer] != set(ids) or
                len(seen) != ADMIN_COUNTS[layer] or
                set(seen) != set(ids) or
                _admin_properties_sha256(seen) != ADMIN_PROPERTIES_SHA256[layer] or
                instances != ADMIN_MAXZOOM_INSTANCES[layer]):
            qa.fail(f'national administrative PMTiles {layer} full semantic/'
                    'maximum-zoom inventory differs from the accepted generation')


def validate_base_metal_prices(qa, path=BASE_METAL_PRICES):
    try:
        data = _load_json(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'base-metal price table is not strict JSON: {exc}')
        return
    if not isinstance(data, dict):
        qa.fail('base-metal price table must be an object')
        return
    if (data.get('schema_version') != 1 or data.get('status') != 'reviewed' or
            data.get('units') != 'nominal U.S. dollars per pound'):
        qa.fail('base-metal price table is not a reviewed v1 USD/lb series')
    source = data.get('source')
    workbooks = source.get('workbooks') if isinstance(source, dict) else None
    prices = data.get('prices_usd_per_lb')
    if (not isinstance(source, dict) or source.get('public_domain') is not True or
            not _text(source.get('authority')) or not isinstance(workbooks, dict) or
            set(workbooks) != {'Cu', 'Pb', 'Zn'} or not isinstance(prices, dict) or
            set(prices) != {'Cu', 'Pb', 'Zn'}):
        qa.fail('base-metal price provenance and series must cover exactly Cu/Pb/Zn')
        return
    conversion = data.get('conversion')
    if (not isinstance(conversion, dict) or
            not _is_finite(conversion.get('pounds_per_metric_ton'), minimum=1)):
        qa.fail('base-metal price conversion factor is invalid')
    for metal in ('Cu', 'Pb', 'Zn'):
        workbook = workbooks[metal]
        table = prices[metal]
        if not isinstance(workbook, dict) or not isinstance(table, dict):
            qa.fail(f'base-metal {metal}: workbook/price series must be objects')
            continue
        first, last = workbook.get('first_year'), workbook.get('last_year')
        provenance_ok = (
            _is_int(first, 1800) and _is_int(last, first if _is_int(first) else 1800) and
            _https(workbook.get('landing_page')) and _https(workbook.get('workbook_url')) and
            isinstance(workbook.get('sha256'), str) and
            re.fullmatch(r'[0-9a-f]{64}', workbook['sha256']) is not None and
            _is_int(workbook.get('bytes'), 1) and _text(workbook.get('source_column')))
        if not provenance_ok:
            qa.fail(f'base-metal {metal}: workbook provenance/checksum is invalid')
            continue
        expected_years = {str(year) for year in range(first, last + 1)}
        if set(table) != expected_years:
            qa.fail(f'base-metal {metal}: annual series is not contiguous {first}-{last}')
            continue
        if any(not re.fullmatch(r'\d{4}', year) or
               not _is_finite(value, minimum=0) or value == 0
               for year, value in table.items()):
            qa.fail(f'base-metal {metal}: prices must be finite positive annual values')


def _materialize(path, code):
    if not isinstance(path, str) or not path.strip():
        raise ValueError('artifact path must be nonempty text')
    return path.replace('{state}', code.lower()).replace('{STATE}', code)


def _resolve_artifact(path, code, release=False):
    path = _materialize(path, code)
    if os.path.isabs(path):
        raise ValueError('artifact path must be browser-relative')
    normalized = os.path.normpath(path)
    if normalized == '..' or normalized.startswith('../'):
        raise ValueError('artifact path escapes site/')
    resolved = os.path.realpath(os.path.join(SITE, normalized))
    try:
        confined = os.path.commonpath((os.path.realpath(SITE), resolved)) == os.path.realpath(SITE)
    except ValueError:
        confined = False
    if not confined:
        raise ValueError('artifact path escapes site/')
    if release:
        budgets = _load_json(BUDGETS)
        delivery = budgets.get('delivery') if isinstance(budgets, dict) else None
        raw_prefix = (delivery.get('immutable_release_prefix')
                      if isinstance(delivery, dict) else None)
        if not isinstance(raw_prefix, str) or not raw_prefix:
            raise ValueError('immutable release prefix is missing from CI budgets')
        prefix = os.path.normpath(raw_prefix)
        if normalized != prefix and not normalized.startswith(prefix + os.sep):
            raise ValueError(f'release artifact must be below immutable prefix {prefix}/')
    return resolved


def _decompress_pmtiles(data, compression):
    if compression == 1:
        return data
    if compression == 2:
        decoder = zlib.decompressobj(wbits=31)
        # PMTiles directories and metadata should remain tiny compared with
        # tile data. Bound inflation so a crafted gzip cannot exhaust CI.
        result = decoder.decompress(data, 256 * 1024 * 1024 + 1)
        if len(result) > 256 * 1024 * 1024 or not decoder.eof:
            raise ValueError('PMTiles gzip member is oversized or truncated')
        if decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('PMTiles gzip member has trailing data')
        return result
    raise ValueError(f'unsupported PMTiles internal compression {compression}')


def _varint(data, position):
    value = 0
    shift = 0
    while position < len(data) and shift <= 63:
        byte = data[position]
        position += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    raise ValueError('malformed PMTiles directory varint')


def _directory_entries(data):
    """Decode all four PMTiles directory columns, rejecting corrupt ranges."""
    count, position = _varint(data, 0)
    # Four one-byte columns per entry is the absolute minimum encoding size.
    if count <= 0 or count > 10_000_000 or count > max(0, (len(data) - 1) // 4):
        raise ValueError(f'invalid PMTiles root entry count {count}')
    tile_ids = []
    current = 0
    for index in range(count):
        delta, position = _varint(data, position)
        if index and delta == 0:
            raise ValueError('PMTiles directory has duplicate/unordered tile ids')
        current += delta
        tile_ids.append(current)
    run_lengths = []
    lengths = []
    for target in (run_lengths, lengths):
        for _ in range(count):
            value, position = _varint(data, position)
            target.append(value)
    offsets = []
    for index in range(count):
        value, position = _varint(data, position)
        offset = ((offsets[index - 1] + lengths[index - 1])
                  if value == 0 and index else value - 1)
        if offset < 0 or lengths[index] <= 0:
            raise ValueError('invalid PMTiles directory offset/length')
        offsets.append(offset)
    if position != len(data):
        raise ValueError('PMTiles root directory has trailing/corrupt bytes')
    entries = list(zip(tile_ids, run_lengths, lengths, offsets))
    for index, (tile_id, run_length, length, offset) in enumerate(entries):
        if tile_id < 0 or run_length < 0 or length <= 0 or offset < 0:
            raise ValueError('invalid PMTiles directory entry')
        if index and entries[index - 1][0] >= tile_id:
            raise ValueError('PMTiles directory is not strictly ordered')
    return entries


def _directory_count(data):
    """Compatibility helper retained for focused unit tests."""
    return len(_directory_entries(data))


def _protobuf_fields(data):
    """Yield minimally decoded protobuf fields while rejecting truncation."""
    position = 0
    while position < len(data):
        tag, position = _varint(data, position)
        field_number, wire_type = tag >> 3, tag & 7
        if field_number <= 0:
            raise ValueError('MVT protobuf contains field zero')
        if wire_type == 0:
            value, position = _varint(data, position)
        elif wire_type == 1:
            if position + 8 > len(data):
                raise ValueError('truncated MVT fixed64 field')
            value, position = data[position:position + 8], position + 8
        elif wire_type == 2:
            length, position = _varint(data, position)
            if length < 0 or position + length > len(data):
                raise ValueError('truncated MVT length-delimited field')
            value, position = data[position:position + length], position + length
        elif wire_type == 5:
            if position + 4 > len(data):
                raise ValueError('truncated MVT fixed32 field')
            value, position = data[position:position + 4], position + 4
        else:
            raise ValueError(f'unsupported MVT protobuf wire type {wire_type}')
        yield field_number, wire_type, value


def _packed_varints(data, label):
    values = []
    position = 0
    while position < len(data):
        try:
            value, position = _varint(data, position)
        except ValueError as exc:
            raise ValueError(f'malformed MVT {label}') from exc
        values.append(value)
    return values


def _mvt_value(data):
    values = []
    for field, wire, value in _protobuf_fields(data):
        if field == 1 and wire == 2:
            try:
                values.append(value.decode('utf-8'))
            except UnicodeDecodeError as exc:
                raise ValueError('MVT string value is not UTF-8') from exc
        elif field == 2 and wire == 5:
            values.append(struct.unpack('<f', value)[0])
        elif field == 3 and wire == 1:
            values.append(struct.unpack('<d', value)[0])
        elif field in (4, 5) and wire == 0:
            values.append(value)
        elif field == 6 and wire == 0:
            values.append((value >> 1) ^ -(value & 1))
        elif field == 7 and wire == 0 and value in (0, 1):
            values.append(bool(value))
    if len(values) != 1:
        raise ValueError('MVT property value must contain exactly one typed value')
    if isinstance(values[0], float) and not math.isfinite(values[0]):
        raise ValueError('MVT property value is not finite')
    return values[0]


def _validate_mvt_geometry(values):
    if not values:
        raise ValueError('MVT feature has empty geometry')
    position = 0
    while position < len(values):
        command = values[position]
        position += 1
        command_id, count = command & 7, command >> 3
        if count <= 0 or command_id not in (1, 2, 7):
            raise ValueError('MVT feature has an invalid geometry command')
        parameters = 2 * count if command_id in (1, 2) else 0
        if position + parameters > len(values):
            raise ValueError('MVT feature geometry command is truncated')
        position += parameters


def _mvt_feature(data, keys, values):
    ids, tags, geometries, geometry_types = [], [], [], []
    for field, wire, value in _protobuf_fields(data):
        if field == 1 and wire == 0:
            ids.append(value)
        elif field == 2 and wire == 2:
            tags.extend(_packed_varints(value, 'feature tags'))
        elif field == 2 and wire == 0:
            tags.append(value)
        elif field == 3 and wire == 0:
            geometry_types.append(value)
        elif field == 4 and wire == 2:
            geometries.extend(_packed_varints(value, 'feature geometry'))
        elif field == 4 and wire == 0:
            geometries.append(value)
    if len(tags) % 2:
        raise ValueError('MVT feature tag indexes are not key/value pairs')
    properties = {}
    for key_index, value_index in zip(tags[::2], tags[1::2]):
        if key_index >= len(keys) or value_index >= len(values):
            raise ValueError('MVT feature tag index is outside its layer dictionary')
        key = keys[key_index]
        if key in properties:
            raise ValueError(f'MVT feature repeats property {key!r}')
        properties[key] = values[value_index]
    if len(geometry_types) != 1 or geometry_types[0] not in (1, 2, 3):
        raise ValueError('MVT feature lacks a valid geometry type')
    _validate_mvt_geometry(geometries)
    if len(ids) > 1:
        raise ValueError('MVT feature repeats its feature ID')
    return {'id': ids[0] if ids else None, 'properties': properties,
            'geometry_type': geometry_types[0]}


def _mvt_layer(data, semantic=False):
    names, versions, extents, raw_features, keys, raw_values = [], [], [], [], [], []
    for field, wire, value in _protobuf_fields(data):
        if field == 1 and wire == 2:
            try:
                names.append(value.decode('utf-8'))
            except UnicodeDecodeError as exc:
                raise ValueError('MVT layer name is not UTF-8') from exc
        elif field == 2 and wire == 2:
            # Even the progress profile rejects structurally malformed feature
            # protobufs; the release profile additionally checks tags/geometry.
            list(_protobuf_fields(value))
            raw_features.append(value)
        elif field == 3 and wire == 2:
            try:
                keys.append(value.decode('utf-8'))
            except UnicodeDecodeError as exc:
                raise ValueError('MVT property key is not UTF-8') from exc
        elif field == 4 and wire == 2:
            raw_values.append(value)
        elif field == 5 and wire == 0:
            extents.append(value)
        elif field == 15 and wire == 0:
            versions.append(value)
    if len(names) != 1 or not names[0] or not raw_features:
        raise ValueError('MVT layer needs one name and at least one feature')
    if versions and (len(versions) != 1 or versions[0] not in (1, 2)):
        raise ValueError('MVT layer declares an invalid version')
    if extents and (len(extents) != 1 or not 1 <= extents[0] <= 2 ** 24):
        raise ValueError('MVT layer declares an invalid extent')
    if len(keys) != len(set(keys)) or any(not key for key in keys):
        raise ValueError('MVT layer property keys are empty or duplicated')
    features = []
    if semantic:
        values = [_mvt_value(value) for value in raw_values]
        features = [_mvt_feature(feature, keys, values) for feature in raw_features]
    return {'name': names[0], 'features': features,
            'feature_count': len(raw_features)}


def _mvt_layers(data, semantic=False):
    layers = [_mvt_layer(value, semantic=semantic)
              for field, wire, value in _protobuf_fields(data)
              if field == 3 and wire == 2]
    names = [layer['name'] for layer in layers]
    if not layers or len(names) != len(set(names)):
        raise ValueError('MVT tile has no layers or duplicate layer names')
    return layers


def _mvt_layer_name(data):
    """Compatibility wrapper for focused tests and callers."""
    return _mvt_layer(data)['name']


def _mvt_layer_names(data):
    return {layer['name'] for layer in _mvt_layers(data)}


def _bounds_intersect(left, right):
    return (left[0] <= right[2] and left[2] >= right[0] and
            left[1] <= right[3] and left[3] >= right[1])


_NATIONAL_PROVENANCE_DATASETS = {
    'geology': {'usgs_sgmc_v1_1', 'usgs_sim3340'},
    'faults': {'usgs_sgmc_v1_1', 'usgs_sim3340', 'usgs_qfaults_2020'},
}
_NATIONAL_PROVENANCE_STATUSES = {
    'explicit', 'source_reference_omits_scale',
    'reserved_001_no_reference_row', 'source_marks_unspecified',
}
_NATIONAL_SOURCE_ID_PREFIX = {
    'usgs_sgmc_v1_1': 'sgmc:',
    'usgs_sim3340': 'sim3340:',
    'usgs_qfaults_2020': 'qfaults:',
}
_GEOMETRY_NORMALIZATION = 'minimum_nonzero_32bit_web_mercator'
_GEOMETRY_NORMALIZATION_ENGINE = 'nwmm_web_mercator_32bit_v1'
_GEOMETRY_NORMALIZATION_REASON = (
    'distinct_source_vertices_collapsed_at_32bit_tile_quantization')
_GEOMETRY_NORMALIZATION_FIELDS = (
    'geometry_normalization', 'geometry_normalization_engine',
    'geometry_normalization_reason', 'geometry_normalization_delta_m',
    'geometry_normalization_parts', 'source_geometry_sha256',
    'source_geometry_length_m')


def _record_national_provenance_feature(inventories, layer, feature):
    """Validate and de-duplicate one tiled national geology/fault feature."""
    allowed_datasets = _NATIONAL_PROVENANCE_DATASETS.get(layer)
    if allowed_datasets is None:
        raise ValueError(f'no national provenance contract exists for layer {layer!r}')
    properties = feature['properties']
    fid = properties.get('fid')
    state = properties.get('st')
    state_long = properties.get('state')
    dataset = properties.get('source_dataset')
    source_alias = properties.get('src')
    source_id = properties.get('source_id')
    source_record_id = properties.get('source_record_id')
    scale = properties.get('source_scale')
    scale_status = properties.get('source_scale_status')
    reference = properties.get('source_ref')
    source_url = properties.get('source_url')
    normalization_values = {
        field: properties.get(field) for field in _GEOMETRY_NORMALIZATION_FIELDS}
    has_normalization = any(value is not None
                            for value in normalization_values.values())
    if not _is_int(fid, 1) or feature.get('id') != fid:
        raise ValueError(
            f'PMTiles source layer {layer} feature fid/top-level ID is invalid')
    if state not in ALL_STATES or state_long != state:
        raise ValueError(
            f'PMTiles source layer {layer} feature st/state identity is invalid')
    if dataset not in allowed_datasets or source_alias != dataset:
        raise ValueError(
            f'PMTiles source layer {layer} feature src/source_dataset is invalid')
    if (not isinstance(source_id, str) or source_id != source_id.strip() or
            not source_id.startswith(_NATIONAL_SOURCE_ID_PREFIX[dataset])):
        raise ValueError(
            f'PMTiles source layer {layer} feature source_id is invalid')
    if (not isinstance(source_record_id, str) or
            source_record_id != source_record_id.strip() or
            re.fullmatch(r'\d+', source_record_id) is None):
        raise ValueError(
            f'PMTiles source layer {layer} feature source_record_id is invalid')
    if any(not isinstance(value, str) or value != value.strip() or not value
           for value in (scale, reference, source_url)):
        raise ValueError(
            f'PMTiles source layer {layer} feature provenance text is invalid')
    if re.fullmatch(r'(?:https?|ftp)://[^\s]+', source_url) is None:
        raise ValueError(
            f'PMTiles source layer {layer} feature source_url '
            f'{source_url!r} is not an absolute source URL')
    if scale_status not in _NATIONAL_PROVENANCE_STATUSES:
        raise ValueError(
            f'PMTiles source layer {layer} feature source_scale_status is invalid')
    if ((dataset == 'usgs_sim3340' and state != 'AK') or
            (dataset == 'usgs_sgmc_v1_1' and state == 'AK') or
            (scale_status == 'reserved_001_no_reference_row' and
             dataset != 'usgs_sim3340') or
            (scale_status == 'source_marks_unspecified' and
             dataset != 'usgs_qfaults_2020')):
        raise ValueError(
            f'PMTiles source layer {layer} feature source/state provenance conflicts')
    if has_normalization:
        delta = normalization_values['geometry_normalization_delta_m']
        parts = normalization_values['geometry_normalization_parts']
        length = normalization_values['source_geometry_length_m']
        if (any(value is None for value in normalization_values.values()) or
                normalization_values['geometry_normalization'] !=
                _GEOMETRY_NORMALIZATION or
                normalization_values['geometry_normalization_engine'] !=
                _GEOMETRY_NORMALIZATION_ENGINE or
                normalization_values['geometry_normalization_reason'] !=
                _GEOMETRY_NORMALIZATION_REASON or
                not _is_finite(delta, 0) or delta <= 0 or delta > 0.02 or
                not _is_int(parts, 1) or
                not _is_finite(length, 0) or length <= 0 or
                re.fullmatch(r'[0-9a-f]{64}',
                             normalization_values['source_geometry_sha256']) is None):
            raise ValueError(
                f'PMTiles source layer {layer} feature geometry normalization '
                'audit is invalid')

    inventory = inventories.setdefault(layer, {
        '_fingerprints': {},
        'states': {code: 0 for code in sorted(ALL_STATES)},
        'by_source': {},
        'source_scale_status': {},
        'geometry_normalizations': [],
    })
    identity = (state, dataset, source_id, source_record_id, scale, scale_status,
                reference, source_url,
                tuple(normalization_values[field]
                      for field in _GEOMETRY_NORMALIZATION_FIELDS))
    fingerprint = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).digest()
    prior = inventory['_fingerprints'].get(fid)
    if prior is not None:
        if prior != fingerprint:
            raise ValueError(
                f'PMTiles source layer {layer} reuses fid {fid} with other provenance')
        return
    inventory['_fingerprints'][fid] = fingerprint
    inventory['states'][state] += 1
    inventory['by_source'][dataset] = inventory['by_source'].get(dataset, 0) + 1
    inventory['source_scale_status'][scale_status] = (
        inventory['source_scale_status'].get(scale_status, 0) + 1)
    if has_normalization:
        inventory['geometry_normalizations'].append({
            'fid': fid, 'st': state, 'source_dataset': dataset,
            'source_id': source_id, 'source_record_id': source_record_id,
            **{field: normalization_values[field]
               for field in _GEOMETRY_NORMALIZATION_FIELDS},
        })


def _fid_coverage(fids, source_records, sample_limit=1_000):
    """Describe the contiguous builder-fid contract without hiding omissions."""
    if not _is_int(source_records, 0):
        return {}
    missing = []
    missing_count = 0
    for fid in range(1, source_records + 1):
        if fid not in fids:
            missing_count += 1
            if len(missing) < sample_limit:
                missing.append(fid)
    extras = sorted(fid for fid in fids if fid > source_records)
    return {
        'source_records': source_records,
        'missing_fid_count': missing_count,
        'missing_fids': missing,
        'missing_fids_truncated': missing_count > len(missing),
        'extra_fid_count': len(extras),
        'extra_fids': extras[:sample_limit],
        'extra_fids_truncated': len(extras) > sample_limit,
    }


def _finalize_feature_inventories(inventories, expected_source_records=None):
    finalized = {}
    for layer, inventory in inventories.items():
        fids = inventory['_fingerprints']
        finalized[layer] = {
            'n': len(fids),
            'unique_tiled_fids': len(fids),
            'states': inventory['states'],
            'by_source': inventory['by_source'],
            'source_scale_status': inventory['source_scale_status'],
            'geometry_normalizations': sorted(
                inventory['geometry_normalizations'], key=lambda item: item['fid']),
        }
        expected = ((expected_source_records or {}).get(layer)
                    if isinstance(expected_source_records, dict) else None)
        finalized[layer].update(_fid_coverage(fids, expected))
    return finalized


def _finalize_maxzoom_fids(fids_by_layer, expected_source_records=None):
    finalized = {}
    for layer, fids in fids_by_layer.items():
        value = {'unique_tiled_fids': len(fids)}
        expected = ((expected_source_records or {}).get(layer)
                    if isinstance(expected_source_records, dict) else None)
        value.update(_fid_coverage(fids, expected))
        finalized[layer] = value
    return finalized


def _pmtiles_header(path, expected_layers=None, required_properties=None, *,
                    verify_feature_properties=False, expected_state=None,
                    expected_bounds=None, collect_feature_inventory=False,
                    expected_source_records=None, collect_feature_ids=False,
                    expected_open_ground_title_sources=None,
                    expected_geometry_types=None, feature_validator=None):
    if ((collect_feature_inventory or collect_feature_ids or
         feature_validator is not None) and
            not verify_feature_properties):
        raise ValueError(
            'feature inventory/validation requires a full semantic PMTiles scan')
    if feature_validator is not None and not callable(feature_validator):
        raise ValueError('PMTiles feature validator must be callable')
    if (expected_source_records is not None and
            (not isinstance(expected_source_records, dict) or
             any(not isinstance(layer, str) or not _is_int(count, 0)
                 for layer, count in expected_source_records.items()))):
        raise ValueError('expected source-record inventory must map layers to counts')
    if (expected_open_ground_title_sources is not None and
            (not isinstance(expected_open_ground_title_sources, dict) or
             any(not isinstance(code, str) or len(code) != 2 or
                 (source is not None and
                  (not isinstance(source, str) or
                   not source.startswith('https://')))
                 for code, source in expected_open_ground_title_sources.items()))):
        raise ValueError(
            'expected open-ground title sources must map states to HTTPS URLs/null')
    if (expected_geometry_types is not None and
            (not isinstance(expected_geometry_types, dict) or
             any(not isinstance(layer, str) or not layer or
                 not isinstance(types, (set, frozenset, list, tuple)) or
                 not types or any(value not in (1, 2, 3) for value in types)
                 for layer, types in expected_geometry_types.items()))):
        raise ValueError(
            'expected geometry types must map layers to MVT type IDs')
    expected_geometry_types = {
        layer: set(types) for layer, types in (expected_geometry_types or {}).items()
    }
    with open(path, 'rb') as fh:
        head = fh.read(127)
    if len(head) < 127 or head[:7] != b'PMTiles':
        raise ValueError('bad PMTiles magic/header')
    if head[7] != 3:
        raise ValueError(f'unsupported PMTiles version {head[7]}')
    size = os.path.getsize(path)
    values = struct.unpack_from('<11Q', head, 8)
    (root_offset, root_length, metadata_offset, metadata_length,
     leaf_offset, leaf_length, tile_offset, tile_length,
     addressed, entries, contents) = values
    ranges = ((root_offset, root_length, 'root directory'),
              (metadata_offset, metadata_length, 'metadata'),
              (tile_offset, tile_length, 'tile data'))
    if leaf_length:
        ranges += ((leaf_offset, leaf_length, 'leaf directory'),)
    for offset, length, label in ranges:
        if offset < 127 or length <= 0 or offset + length > size:
            raise ValueError(f'invalid PMTiles {label} range')
    ordered_ranges = sorted((offset, offset + length, label)
                            for offset, length, label in ranges)
    for previous, current in zip(ordered_ranges, ordered_ranges[1:]):
        if previous[1] > current[0]:
            raise ValueError(f'PMTiles {previous[2]} overlaps {current[2]}')
    if not (addressed > 0 and entries > 0 and contents > 0):
        raise ValueError('PMTiles archive declares no tiles')
    clustered, internal_compression, tile_compression, tile_type = head[96:100]
    if clustered not in (0, 1):
        raise ValueError(f'PMTiles clustered flag is invalid ({clustered})')
    if internal_compression not in (1, 2):
        raise ValueError(f'unsupported PMTiles internal compression {internal_compression}')
    if tile_compression not in (1, 2):
        raise ValueError(f'unsupported PMTiles MVT compression {tile_compression}')
    if tile_type != 1:
        raise ValueError(f'PMTiles vector artifact has tile type {tile_type}, expected MVT')
    minzoom, maxzoom = head[100], head[101]
    if minzoom > maxzoom or maxzoom > 24:
        raise ValueError('PMTiles zoom range is invalid')
    minlon, minlat, maxlon, maxlat = (
        value / 10_000_000 for value in struct.unpack_from('<4i', head, 102))
    if not (-180 <= minlon < maxlon <= 180 and -90 <= minlat < maxlat <= 90):
        raise ValueError('PMTiles bounds are invalid')
    center_zoom = head[118]
    center_lon, center_lat = (
        value / 10_000_000 for value in struct.unpack_from('<2i', head, 119))
    if (not minzoom <= center_zoom <= maxzoom or
            not -180 <= center_lon <= 180 or not -90 <= center_lat <= 90):
        raise ValueError('PMTiles center is invalid')
    with open(path, 'rb') as fh:
        fh.seek(root_offset)
        root = _decompress_pmtiles(fh.read(root_length), internal_compression)
        fh.seek(metadata_offset)
        metadata_bytes = _decompress_pmtiles(fh.read(metadata_length),
                                              internal_compression)
        root_directory = _directory_entries(root)
        tile_entries = []
        leaf_pointers = []
        for tile_id, run_length, length, offset in root_directory:
            if run_length:
                tile_entries.append((tile_id, run_length, length, offset))
                continue
            if not leaf_length or offset + length > leaf_length:
                raise ValueError('PMTiles root points outside its leaf directory range')
            leaf_pointers.append((tile_id, offset, length))
        unique_leaf_ranges = sorted({(offset, length)
                                     for _, offset, length in leaf_pointers})
        for previous, current in zip(unique_leaf_ranges, unique_leaf_ranges[1:]):
            if previous[0] + previous[1] > current[0]:
                raise ValueError('PMTiles leaf directory ranges overlap')
        decoded_leaves = {}
        for offset, length in unique_leaf_ranges:
            fh.seek(leaf_offset + offset)
            leaf = _decompress_pmtiles(fh.read(length), internal_compression)
            decoded = _directory_entries(leaf)
            if any(run_length == 0 for _, run_length, _, _ in decoded):
                raise ValueError('PMTiles leaf directory contains another leaf pointer')
            decoded_leaves[(offset, length)] = decoded
            tile_entries.extend(decoded)
        for expected_tile_id, offset, length in leaf_pointers:
            if decoded_leaves[(offset, length)][0][0] != expected_tile_id:
                raise ValueError('PMTiles leaf pointer tile id does not match its directory')
        tile_entries.sort()
        max_tile_id = (4 ** (maxzoom + 1) - 1) // 3
        for index, (tile_id, run_length, length, offset) in enumerate(tile_entries):
            if offset + length > tile_length:
                raise ValueError('PMTiles directory points outside tile data')
            if tile_id + run_length > max_tile_id:
                raise ValueError('PMTiles directory tile id exceeds declared maxzoom')
            if (index and tile_entries[index - 1][0] +
                    tile_entries[index - 1][1] > tile_id):
                raise ValueError('PMTiles addressed tile ranges overlap')
        computed_addressed = sum(item[1] for item in tile_entries)
        computed_contents = len({(item[3], item[2]) for item in tile_entries})
        if (len(tile_entries) != entries or computed_addressed != addressed or
                computed_contents != contents):
            raise ValueError('PMTiles header tile counts do not match its directories')
        sample_indexes = {0, len(tile_entries) - 1}
        if len(tile_entries) > 2:
            sample_indexes.update({len(tile_entries) // 4, len(tile_entries) // 2,
                                   3 * len(tile_entries) // 4})
        sample_ranges = {(tile_entries[index][3], tile_entries[index][2])
                         for index in sample_indexes}
        sample_tiles = []
        for offset, length in sorted(sample_ranges):
            fh.seek(tile_offset + offset)
            sample_tiles.append(fh.read(length))
    sampled_layers = set()
    for tile_bytes in sample_tiles:
        sampled_layers.update(_mvt_layer_names(
            _decompress_pmtiles(tile_bytes, tile_compression)))
    try:
        metadata = json.loads(metadata_bytes, parse_constant=_reject_json_constant,
                              object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f'invalid PMTiles JSON metadata: {exc}') from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get('vector_layers'), list):
        raise ValueError('PMTiles metadata needs a vector_layers array')
    vector_layers = metadata['vector_layers']
    if (not vector_layers or any(not isinstance(item, dict) or
            not isinstance(item.get('id'), str) or not item['id'] or
            not isinstance(item.get('fields'), dict) or
            any(not isinstance(key, str) or not key or not isinstance(value, str)
                for key, value in item.get('fields', {}).items())
            for item in vector_layers)):
        raise ValueError('PMTiles vector layer metadata schema is invalid')
    layer_ids = [item['id'] for item in vector_layers]
    if len(layer_ids) != len(set(layer_ids)):
        raise ValueError('PMTiles metadata has duplicate vector layer ids')
    layer_metadata = {item['id']: item for item in vector_layers}
    source_layers = set(layer_metadata)
    if not sampled_layers <= source_layers:
        raise ValueError('sample MVT layer is absent from PMTiles metadata')
    if (expected_layers is not None and
            (not isinstance(expected_layers, (list, tuple, set)) or
             any(not isinstance(item, str) or not item for item in expected_layers))):
        raise ValueError('expected PMTiles layers must be nonempty strings')
    expected_layers = set(expected_layers or [])
    if expected_layers and not expected_layers <= source_layers:
        raise ValueError(f'PMTiles source layers {sorted(source_layers)} do not contain '
                         f'{sorted(expected_layers)}')
    if isinstance(required_properties, dict):
        if any(not isinstance(layer, str) or
               not isinstance(fields, (list, tuple, set)) or
               any(not isinstance(field, str) or not field for field in fields)
               for layer, fields in required_properties.items()):
            raise ValueError('required PMTiles properties mapping is invalid')
        requirements = {layer: set(fields) for layer, fields in required_properties.items()}
    else:
        if (required_properties is not None and
                (not isinstance(required_properties, (list, tuple, set)) or
                 any(not isinstance(field, str) or not field
                     for field in required_properties))):
            raise ValueError('required PMTiles properties must be nonempty strings')
        requirements = {layer: set(required_properties or [])
                        for layer in expected_layers}
    if requirements:
        for source_layer, required in requirements.items():
            if source_layer not in expected_layers:
                raise ValueError(f'required-property layer {source_layer!r} is not expected')
            fields = set((layer_metadata.get(source_layer, {}).get('fields') or {}).keys())
            missing = required - fields
            if missing:
                raise ValueError(f'PMTiles source layer {source_layer} lacks required '
                                 f'properties {sorted(missing)}')
    archive_bounds = [minlon, minlat, maxlon, maxlat]
    if expected_bounds is not None:
        if (not isinstance(expected_bounds, (list, tuple)) or not expected_bounds or
                any(not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
                    any(not isinstance(value, (int, float)) or isinstance(value, bool) or
                        not math.isfinite(value) for value in bbox)
                    for bbox in expected_bounds)):
            raise ValueError('expected state bounds must be one or more numeric bboxes')
        if not any(_bounds_intersect(archive_bounds, bbox) for bbox in expected_bounds):
            raise ValueError('PMTiles bounds do not intersect the registered state')
    semantic_layer_counts = {}
    maxzoom_layer_counts = {}
    feature_inventories = {}
    maxzoom_fids = {}
    generic_maxzoom_ids = {}
    if verify_feature_properties:
        if expected_state is not None and (
                not isinstance(expected_state, str) or
                not re.fullmatch(r'[A-Z]{2}', expected_state)):
            raise ValueError('expected PMTiles state must be an uppercase state code')
        # DONE means every encoded feature is structurally and semantically
        # checked. Metadata plus a five-tile sample is insufficient for the
        # per-polygon provenance contract, so release archives get a full
        # content scan. The directory/hash checks above still bind this scan
        # to the exact immutable file recorded by the registry.
        content_ranges = sorted({(item[3], item[2]) for item in tile_entries})
        maxzoom_first = (4 ** maxzoom - 1) // 3
        maxzoom_after = (4 ** (maxzoom + 1) - 1) // 3
        maxzoom_ranges = {
            (offset, length)
            for tile_id, run_length, length, offset in tile_entries
            if tile_id < maxzoom_after and tile_id + run_length > maxzoom_first
        }
        seen_expected = set()
        with open(path, 'rb') as fh:
            for offset, length in content_ranges:
                at_maxzoom = (offset, length) in maxzoom_ranges
                fh.seek(tile_offset + offset)
                payload = fh.read(length)
                if len(payload) != length:
                    raise ValueError('PMTiles tile payload is truncated')
                layers = _mvt_layers(
                    _decompress_pmtiles(payload, tile_compression), semantic=True)
                for decoded in layers:
                    name = decoded['name']
                    semantic_layer_counts[name] = (
                        semantic_layer_counts.get(name, 0) + decoded['feature_count'])
                    if at_maxzoom:
                        maxzoom_layer_counts[name] = (
                            maxzoom_layer_counts.get(name, 0) +
                            decoded['feature_count'])
                    required = requirements.get(name, set())
                    if name in expected_layers:
                        seen_expected.add(name)
                    for feature in decoded['features']:
                        properties = feature['properties']
                        allowed_geometry = expected_geometry_types.get(name)
                        if (allowed_geometry is not None and
                                feature['geometry_type'] not in allowed_geometry):
                            raise ValueError(
                                f'PMTiles source layer {name} feature geometry '
                                f'type {feature["geometry_type"]} is not one of '
                                f'{sorted(allowed_geometry)}')
                        missing = required - set(properties)
                        if missing:
                            raise ValueError(
                                f'PMTiles source layer {name} feature lacks required '
                                f'properties {sorted(missing)}')
                        if expected_state is not None and properties.get('st') != expected_state:
                            raise ValueError(
                                f'PMTiles source layer {name} feature state '
                                f'{properties.get("st")!r} is not {expected_state}')
                        if collect_feature_inventory and name in expected_layers:
                            _record_national_provenance_feature(
                                feature_inventories, name, feature)
                            if at_maxzoom:
                                maxzoom_fids.setdefault(name, set()).add(
                                    properties['fid'])
                        if collect_feature_ids and at_maxzoom and name in expected_layers:
                            feature_id = feature.get('id')
                            if (not isinstance(feature_id, int) or
                                    isinstance(feature_id, bool) or feature_id < 0):
                                raise ValueError(
                                    f'PMTiles source layer {name} feature has no '
                                    'nonnegative top-level ID')
                            generic_maxzoom_ids.setdefault(name, set()).add(feature_id)
                        if feature_validator is not None and name in expected_layers:
                            feature_validator(name, feature, at_maxzoom)
                        if name == 'open_ground' and required:
                            open_count = properties.get('open_count')
                            section_count = properties.get('section_count')
                            fraction = properties.get('open_fraction')
                            status = properties.get('status')
                            title_status = properties.get('mineral_title_status')
                            title_source = properties.get('mineral_title_source')
                            title_ref = properties.get('mineral_title_ref')
                            title_reviewed = properties.get(
                                'mineral_title_reviewed')
                            counts_valid = all(
                                isinstance(value, int) and not isinstance(value, bool) and
                                value >= 0 for value in (open_count, section_count))
                            fraction_valid = (isinstance(fraction, (int, float)) and
                                              not isinstance(fraction, bool) and
                                              math.isfinite(fraction) and
                                              0 <= fraction <= 1)
                            expected_fraction = ((open_count / section_count)
                                                 if counts_valid and section_count else 0)
                            if (not counts_valid or open_count > section_count or
                                    not fraction_valid or
                                    not math.isclose(fraction, expected_fraction,
                                                     rel_tol=1e-6, abs_tol=1e-9)):
                                raise ValueError(
                                    'open_ground feature has inconsistent count/fraction math')
                            if (section_count != 1 or
                                    status not in {
                                        'OPEN', 'ACTIVE', 'WITHDRAWN',
                                        'NONFEDERAL', 'UNKNOWN'} or
                                    (status == 'OPEN') != (open_count == 1)):
                                raise ValueError(
                                    'open_ground feature status disagrees with '
                                    'section-level open-ground math')
                            if (title_status not in {
                                    'public_domain_locatable', 'non_federal',
                                    'unknown'} or
                                    title_reviewed not in (0, 1) or
                                    not isinstance(title_source, str) or
                                    not isinstance(title_ref, str)):
                                raise ValueError(
                                    'open_ground feature has invalid mineral-title status')
                            if title_status == 'unknown':
                                if (title_reviewed != 0 or title_source or title_ref):
                                    raise ValueError(
                                        'unknown open_ground mineral title carries '
                                        'source, reference, or reviewed=true')
                            elif (title_reviewed != 1 or
                                  not title_source.startswith('https://') or
                                  not title_ref.strip()):
                                raise ValueError(
                                    'reviewed open_ground mineral title lacks an '
                                    'HTTPS source or record reference')
                            if (title_status != 'unknown' and
                                    expected_open_ground_title_sources is not None):
                                expected_title_source = (
                                    expected_open_ground_title_sources.get(
                                        properties.get('st')))
                                if (expected_title_source is None or
                                        title_source != expected_title_source):
                                    raise ValueError(
                                        'open_ground mineral title source does not '
                                        'match the reviewed state-registry ingest')
                            if status == 'OPEN' and not (
                                    title_status == 'public_domain_locatable' and
                                    title_reviewed == 1):
                                raise ValueError(
                                    'OPEN feature lacks reviewed public-domain '
                                    'locatable mineral title')
                            if status == 'NONFEDERAL' and not (
                                    title_status == 'non_federal' and
                                    title_reviewed == 1):
                                raise ValueError(
                                    'NONFEDERAL feature lacks reviewed '
                                    'non-federal mineral title')
        # A declared zero-count layer may legitimately have no tile features;
        # completeness/count evidence is validated separately. Any layer that
        # is present, however, has been scanned in full above.
    return {'version': head[7], 'bytes': size, 'bounds': [minlon, minlat, maxlon, maxlat],
            'minzoom': minzoom, 'maxzoom': maxzoom,
            'source_layers': sorted(source_layers), 'root_entries': len(root_directory),
            'sample_layers': sorted(sampled_layers),
            'tile_entries': entries, 'tile_contents': contents,
            'description': metadata.get('description'),
            'semantic_layer_counts': semantic_layer_counts,
            # These names deliberately distinguish source records from MVT
            # instances duplicated/clipped across tiles and zoom levels.
            'all_zoom_feature_instances': semantic_layer_counts,
            'maxzoom_feature_instances': maxzoom_layer_counts,
            'feature_inventories': _finalize_feature_inventories(
                feature_inventories, expected_source_records),
            'maxzoom_feature_inventories': _finalize_maxzoom_fids(
                maxzoom_fids, expected_source_records),
            'maxzoom_feature_ids': {
                layer: sorted(generic_maxzoom_ids.get(layer, set()))
                for layer in sorted(expected_layers)
            } if collect_feature_ids else {},
            'field_types': {
                layer: dict(layer_metadata[layer]['fields'])
                for layer in sorted(layer_metadata)
            }}


def _tiff_header(path, expected_bounds=None):
    try:
        result = subprocess.run(['gdalinfo', '-json', path], check=True,
                                capture_output=True, text=True)
        info = json.loads(result.stdout, parse_constant=_reject_json_constant,
                          object_pairs_hook=_reject_duplicate_keys)
    except FileNotFoundError as exc:
        raise ValueError('gdalinfo is required to validate released COGs') from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f'GDAL cannot decode raster: {exc}') from exc
    if not isinstance(info, dict):
        raise ValueError('gdalinfo JSON must be an object')
    metadata = info.get('metadata')
    if not isinstance(metadata, dict):
        metadata = {}
    image_structure = metadata.get('IMAGE_STRUCTURE')
    if not isinstance(image_structure, dict):
        image_structure = {}
    if info.get('driverShortName') not in ('GTiff', 'COG'):
        raise ValueError(f'raster driver is {info.get("driverShortName")}, not GeoTIFF/COG')
    if image_structure.get('LAYOUT') != 'COG' and info.get('driverShortName') != 'COG':
        raise ValueError('GeoTIFF is not marked with COG layout')
    size = info.get('size') or []
    bands = info.get('bands')
    if (not isinstance(size, list) or len(size) != 2 or
            any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in size) or
            not isinstance(bands, list) or not bands or
            any(not isinstance(band, dict) for band in bands)):
        raise ValueError('COG has no raster dimensions/bands')
    coordinate_system = info.get('coordinateSystem')
    transform = info.get('geoTransform')
    gcps = info.get('gcps')
    valid_transform = (isinstance(transform, list) and len(transform) == 6 and
                       all(isinstance(value, (int, float)) and
                           not isinstance(value, bool) and math.isfinite(value)
                           for value in transform) and transform[1] != 0 and transform[5] != 0)
    valid_gcps = isinstance(gcps, dict) and bool(gcps.get('gcpList'))
    if (not isinstance(coordinate_system, dict) or
            not (coordinate_system.get('wkt') or coordinate_system.get('projjson')) or
            not (valid_transform or valid_gcps)):
        raise ValueError('COG lacks georeferencing/CRS')
    if max(size) >= 1024 and any(not isinstance(band.get('overviews'), list) or
                                 not band['overviews'] for band in bands):
        raise ValueError('statewide COG lacks internal overviews')
    wgs84 = info.get('wgs84Extent')
    coordinates = (wgs84.get('coordinates') if isinstance(wgs84, dict) and
                   wgs84.get('type') in ('Polygon', 'MultiPolygon') else None)
    points = []
    if coordinates is not None:
        def collect(value):
            if (isinstance(value, list) and len(value) >= 2 and
                    all(isinstance(item, (int, float)) and not isinstance(item, bool)
                        and math.isfinite(item) for item in value[:2])):
                points.append((float(value[0]), float(value[1])))
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(coordinates)
    if not points:
        raise ValueError('COG lacks a usable WGS84 extent')
    raster_bounds = [min(point[0] for point in points), min(point[1] for point in points),
                     max(point[0] for point in points), max(point[1] for point in points)]
    if not (-180 <= raster_bounds[0] < raster_bounds[2] <= 180 and
            -90 <= raster_bounds[1] < raster_bounds[3] <= 90):
        raise ValueError('COG WGS84 extent is invalid')
    if expected_bounds is not None:
        if (not isinstance(expected_bounds, (list, tuple)) or not expected_bounds or
                not any(_bounds_intersect(raster_bounds, bbox)
                        for bbox in expected_bounds
                        if isinstance(bbox, (list, tuple)) and len(bbox) == 4)):
            raise ValueError('COG bounds do not intersect the registered state')
    return {'bytes': os.path.getsize(path), 'size': size, 'bands': len(bands),
            'bounds': raster_bounds}


def _validate_zero_fault_inventory(qa, code, layer):
    asserted = layer.get('zero_inventory')
    if not isinstance(asserted, dict):
        qa.fail(f'{code} faults: zero layer needs national-baseline evidence')
        return False
    try:
        evidence_path = _resolve_artifact(
            asserted.get('evidence_artifact'), code, release=True)
        evidence = validate_zero_inventory_evidence_file(
            evidence_path, code, 'faults',
            manifest_path=os.path.join(SITE, 'data', 'manifest.json'),
            expected_sha=asserted.get('sha256'), site=SITE)
        manifest = _load_json(os.path.join(SITE, 'data', 'manifest.json'))
        baseline = (manifest.get('national_baselines') or {}).get('faults')
    except (ZeroInventoryError, OSError, TypeError, ValueError) as exc:
        qa.fail(f'{code} faults: zero-inventory evidence is invalid ({exc})')
        return False
    binding = evidence.get('baseline')
    if (asserted.get('baseline_manifest_key') != 'national_baselines.faults' or
            asserted.get('baseline_sha256') != binding.get('sha256') or
            asserted.get('state_count') != 0 or asserted.get('complete') is not True or
            not isinstance(baseline, dict) or
            layer.get('sha256') != baseline.get('sha256') or
            layer.get('bytes') != baseline.get('bytes') or
            layer.get('source_layers') != ['faults'] or
            (layer.get('layer_metadata') or {}).get('faults', {}).get('n') != 0):
        qa.fail(f'{code} faults: zero release is not the exact checksummed national '
                'fault baseline with descriptor n=0')
        return False
    return True


def validate_artifacts(qa, states, release_profile=False, require_all=False):
    for code, state in states.items():
        if not state['release']['enabled'] and not require_all:
            continue
        layers = [('geology', state['geology'], False),
                  ('faults', state['faults'], False),
                  ('aeromag', state['aeromag'], True)]
        if state['regime'] == 'non_claim':
            layers.append(('land_context', state['land_context'], False))
            if state.get('aml', {}).get('release_inventory_status') == 'ingested_complete':
                layers.append(('aml', state['aml'], False))
            if state.get('trust_land', {}).get('release_inventory_status') == 'ingested_complete':
                layers.append(('trust_land', state['trust_land'], False))
        else:
            for system in state['claim_systems']:
                parts = system.get('publication_artifacts')
                if isinstance(parts, dict) and parts:
                    layers += [(f'claims:{system["id"]}:{part_id}', part, False)
                               for part_id, part in parts.items()]
                else:
                    layers.append((f'claims:{system["id"]}', system, False))
        raw_envelopes = state.get('query_envelopes') or []
        state_bounds = [item.get('bbox') for item in raw_envelopes
                        if isinstance(item, dict) and isinstance(item.get('bbox'), list)]
        for label, layer, raster in layers:
            declared_metadata = layer.get('layer_metadata')
            declared_total = (sum(item.get('n', -1)
                                  for item in declared_metadata.values())
                              if isinstance(declared_metadata, dict) and
                              all(isinstance(item, dict) and
                                  _is_int(item.get('n'), 0)
                                  for item in declared_metadata.values()) else None)
            zero_faults = label == 'faults' and declared_total == 0
            if zero_faults and not _validate_zero_fault_inventory(qa, code, layer):
                continue
            artifact = layer.get('artifact') or layer.get('browser_path')
            if not artifact:
                qa.fail(f'{code} {label}: no tile artifact path')
                continue
            try:
                path = _resolve_artifact(artifact, code, release=True)
            except ValueError as exc:
                qa.fail(f'{code} {label}: {exc}')
                continue
            if not os.path.isfile(path):
                qa.fail(f'{code} {label}: missing artifact {path}')
                continue
            try:
                if raster:
                    meta = _tiff_header(path, expected_bounds=state_bounds)
                else:
                    title_sources = None
                    if label.endswith(':open_ground'):
                        estate = ((state.get('open_ground') or {}).get(
                            'mineral_estate') or {})
                        source = (estate.get('source_url')
                                  if estate.get('status') == 'reviewed_ingested'
                                  else None)
                        title_sources = {code: source}
                    meta = _pmtiles_header(
                        path, layer.get('source_layers'),
                        layer.get('required_properties'),
                        verify_feature_properties=not zero_faults,
                        expected_state=None if zero_faults else code,
                        expected_bounds=state_bounds,
                        expected_open_ground_title_sources=title_sources,
                        collect_feature_ids=title_sources is not None)
            except ValueError as exc:
                qa.fail(f'{code} {label}: {exc}')
                continue
            if int(layer.get('bytes') or -1) != meta['bytes']:
                qa.fail(f'{code} {label}: registry bytes do not match artifact')
            if not raster:
                declared = layer.get('layer_metadata')
                semantic = meta.get('semantic_layer_counts') or {}
                if isinstance(declared, dict):
                    for source_layer, item in declared.items():
                        n = item.get('n') if isinstance(item, dict) else None
                        seen = semantic.get(source_layer, 0)
                        # MVT features can repeat across zoom levels, so the
                        # archive scan cannot reconcile an exact source-row
                        # count. It can and must prove the declared layer is
                        # materially present (or absent for a genuine zero).
                        if isinstance(n, int) and not isinstance(n, bool):
                            if n > 0 and seen <= 0:
                                qa.fail(f'{code} {label}: declared nonempty source layer '
                                        f'{source_layer} has no decoded tile features')
                            if n == 0 and seen > 0:
                                qa.fail(f'{code} {label}: declared zero source layer '
                                        f'{source_layer} contains decoded tile features')
                if label.endswith(':open_ground'):
                    inventory = layer.get('source_id_inventory')
                    tiled_ids = (meta.get('maxzoom_feature_ids') or {}).get(
                        'open_ground', [])
                    tiled_sha = hashlib.sha256(json.dumps(
                        sorted(tiled_ids), separators=(',', ':'),
                        allow_nan=False).encode('utf-8')).hexdigest()
                    maxzoom_instances = (meta.get(
                        'maxzoom_feature_instances') or {}).get(
                            'open_ground', 0)
                    if not (
                            isinstance(inventory, dict) and
                            inventory.get('status') ==
                            'complete_at_derivation' and
                            _is_int(inventory.get('source_records'), 1) and
                            inventory.get('source_records') == len(tiled_ids) ==
                            inventory.get('maxzoom_unique_tiled_ids') and
                            inventory.get('maxzoom_feature_instances') ==
                            maxzoom_instances and
                            inventory.get('ids_sha256') == tiled_sha):
                        qa.fail(
                            f'{code} {label}: open-ground archive does not '
                            'reconcile every derived source-section ID')
            sha = hashlib.sha256()
            with open(path, 'rb') as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b''):
                    sha.update(block)
            if layer.get('sha256') != sha.hexdigest():
                qa.fail(f'{code} {label}: registry sha256 does not match artifact')


def validate_release_registry_candidates(qa, states):
    """Apply the full per-state DONE contract even while releases are disabled."""
    for code, state in states.items():
        candidate = copy.deepcopy(state)
        candidate['release']['status'] = 'done'
        candidate['release']['enabled'] = True
        try:
            validate_state(candidate, f'states/{code}.yaml')
        except RegistryError as exc:
            for message in str(exc).splitlines():
                qa.fail(message)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            qa.fail(f'states/{code}.yaml: release-candidate validation crashed closed: {exc}')


def validate_ci_budgets(qa, states, require_all=False):
    try:
        budgets = _load_json(BUDGETS)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'CI budgets are not strict JSON: {exc}')
        return
    browser = budgets.get('browser') if isinstance(budgets, dict) else None
    if not isinstance(browser, dict):
        qa.fail('CI budgets need a browser object')
        return
    for field in ('heap_mb_max', 'bulk_origin_storage_mb_max'):
        if not _is_finite(browser.get(field), minimum=0):
            qa.fail(f'CI browser budget {field} must be finite and nonnegative')
            return
    for code, state in states.items():
        if not state['release']['enabled'] and not require_all:
            continue
        ci = (state['release'].get('acceptance') or {}).get('ci_scale') or {}
        heap = ci.get('heap_mb')
        storage = ci.get('bulk_origin_storage_mb')
        if _is_finite(heap, minimum=0) and heap > browser['heap_mb_max']:
            qa.fail(f'{code}: measured heap {heap} MB exceeds {browser["heap_mb_max"]} MB')
        if (_is_finite(storage, minimum=0) and
                storage > browser['bulk_origin_storage_mb_max']):
            qa.fail(f'{code}: bulk origin storage {storage} MB exceeds '
                    f'{browser["bulk_origin_storage_mb_max"]} MB')


def validate_no_statewide_json(qa, states):
    # Browser-addressable whole-state legacy files are tolerated only for the
    # currently released pre-WS11 states. No new registry layer may point at
    # them, and final release refuses every such file.
    for code, state in states.items():
        for root in (state['geology'], state['faults'], state['aeromag'],
                     state['land_context'], *state['claim_systems']):
            path = str(root.get('browser_path') or root.get('url') or '')
            if re.search(r'\.(?:geo)?json(?:$|[?#])', path, re.I):
                qa.fail(f'{code}: statewide registry browser JSON forbidden: {path}')


def validate_scoring(qa, states=None, release=False):
    grades = os.path.join(SITE, 'data', 'grades', 'grades.json')
    if not os.path.exists(grades):
        # The browser's legacy aggregate is optional. Release grade acceptance
        # is derived from the immutable per-state WS9 evidence, never this file.
        return {}
    try:
        data = _load_json(grades)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'grades artifact is not strict JSON: {exc}')
        return
    if not isinstance(data, dict):
        qa.fail('grades artifact must be an object')
        return {}
    n = data.get('n')
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        qa.fail(f'grades n must be a nonnegative integer, got {n!r}')
        return {}
    for field, value in data.items():
        if isinstance(value, list) and len(value) != n:
            qa.fail(f'grades {field} length must equal n={n}')
    typed = data.get('open_ground')
    if typed is None:
        qa.warn('legacy grades artifact has no typed open_ground column; migration '
                'is still required before it can drive browser scoring')
        return {}
    if not isinstance(typed, list) or len(typed) != n:
        qa.fail(f'grades open_ground length must equal n={n}')
        return
    state_column = data.get('st')
    if not isinstance(state_column, list) or len(state_column) != n:
        qa.fail(f'grades st length must equal n={n}')
        return {}
    allowed = {'measured', 'unknown', 'not_applicable'}
    states = states or load_states()
    required_text_columns = ('name', 'src', 'quote', 'url')
    for field in required_text_columns:
        column = data.get(field)
        if not isinstance(column, list) or len(column) != n:
            qa.fail(f'grades {field} length must equal n={n}')
    for field in ('x', 'y'):
        column = data.get(field)
        if not isinstance(column, list) or len(column) != n:
            qa.fail(f'grades {field} length must equal n={n}')
    primary_source_column = data.get('primary_source_id')
    if primary_source_column is not None and (
            not isinstance(primary_source_column, list) or
            len(primary_source_column) != n or
            any(value is not None and not _text(value)
                for value in primary_source_column)):
        qa.fail(f'grades primary_source_id must contain n strings/nulls')
        primary_source_column = None
    metrics = {code: {'mines': set(), 'sources': set(), 'source_urls': {},
                      'quotes': 0, 'page_cites': 0} for code in states}
    page_pattern = re.compile(r'\bpp?\.\s*\d', re.I)
    grade_fields = ('au', 'ag', 'cu', 'pb', 'zn', 'sb', 'wo3', 'hgf', 'usd', 'yd3')
    for i, item in enumerate(typed):
        if not isinstance(item, dict):
            qa.fail(f'grades row {i}: missing open_ground object')
            continue
        status = item.get('status')
        distance = item.get('distance_m')
        if status not in allowed:
            qa.fail(f'grades row {i}: invalid open_ground status {status!r}')
        if status in ('not_applicable', 'unknown') and distance is not None:
            qa.fail(f'grades row {i}: {status} must carry null distance')
        if status == 'measured' and not (
                isinstance(distance, (int, float)) and not isinstance(distance, bool)
                and math.isfinite(distance) and distance >= 0):
            qa.fail(f'grades row {i}: measured distance must be finite and nonnegative')
        score = item.get('score')
        if status != 'measured' and score is not None:
            qa.fail(f'grades row {i}: {status} open-ground score must be null')
        if status == 'measured' and score is not None and not (
                _is_finite(score) and 0 <= score <= 1):
            qa.fail(f'grades row {i}: measured open-ground score must be null or 0..1')
        state = state_column[i]
        # Membership is determined from the registry set, not the existence of
        # a numeric distance. N/A and zero therefore cannot collapse together.
        if not isinstance(state, str) or state not in states:
            qa.fail(f'grades row {i}: invalid/missing WS11 state {state!r}')
            continue
        if states[state]['regime'] == 'claim':
            if status == 'not_applicable':
                qa.fail(f'grades row {i}: claim state {state} cannot be not_applicable')
        else:
            if status != 'not_applicable':
                qa.fail(f'grades row {i}: non-claim state {state} must be not_applicable')
        x_values, y_values = data.get('x'), data.get('y')
        if (isinstance(x_values, list) and len(x_values) == n and
                isinstance(y_values, list) and len(y_values) == n and
                ((x_values[i] is None) != (y_values[i] is None) or
                 (x_values[i] is not None and
                  (not _finite_coordinate(x_values[i], -180, 180) or
                   not _finite_coordinate(y_values[i], -90, 90))))):
            qa.fail(f'grades row {i}: coordinates are invalid')
        if not all(isinstance(data.get(field), list) and len(data[field]) == n
                   for field in required_text_columns):
            continue
        name, source, quote, url = (data[field][i] for field in required_text_columns)
        grade_values = [data[field][i] for field in grade_fields
                        if isinstance(data.get(field), list) and len(data[field]) == n]
        valid_grade = any(_is_finite(value, minimum=0) for value in grade_values
                          if value is not None)
        # Release acceptance is derived only from rows that actually carry a
        # mine identity, verbatim text, an HTTPS source, and a page citation.
        if (_text(name) and _text(source) and _text(quote, 8) and _https(url) and
                page_pattern.search(source) and valid_grade):
            normalized_name = re.sub(r'\s+', ' ', name.strip().casefold())
            metrics[state]['mines'].add(normalized_name)
            source_id = (primary_source_column[i].strip()
                         if primary_source_column is not None and
                         _text(primary_source_column[i]) else None)
            if source_id:
                metrics[state]['sources'].add(source_id)
                metrics[state]['source_urls'].setdefault(source_id, set()).add(url.strip())
            metrics[state]['quotes'] += 1
            metrics[state]['page_cites'] += 1
    return {code: {'graded_mines': len(values['mines']),
                   'primary_sources': len(values['sources']),
                   'primary_source_ids': sorted(values['sources']),
                   'primary_source_urls': {
                       source_id: sorted(urls)
                       for source_id, urls in values['source_urls'].items()},
                   'verbatim_quotes': values['quotes'],
                   'page_cites': values['page_cites']}
            for code, values in metrics.items()}


def _strict_evidence_json(qa, relative_path, code, label):
    try:
        path = _resolve_artifact(relative_path, code, release=False)
    except ValueError as exc:
        qa.fail(f'{code} {label}: {exc}')
        return None
    if not path.endswith('.json') or not os.path.isfile(path):
        qa.fail(f'{code} {label}: evidence artifact must be an existing JSON file')
        return None
    try:
        value = _load_json(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'{code} {label}: evidence artifact is not strict JSON ({exc})')
        return None
    if not isinstance(value, dict) or value.get('state') != code:
        qa.fail(f'{code} {label}: evidence artifact state identity is invalid')
        return None
    return value


def _validate_grade_evidence(qa, code, state, acceptance):
    """Bind the grade DONE assertion to one compiler-produced state artifact."""
    asserted = acceptance.get('grades')
    if not isinstance(asserted, dict):
        qa.fail(f'{code}: grade acceptance must be an object')
        return None
    relative = asserted.get('evidence_artifact')
    try:
        path = _resolve_artifact(relative, code, release=True)
    except ValueError as exc:
        qa.fail(f'{code} grade evidence: {exc}')
        return None
    expected_sha = asserted.get('sha256')
    normalized = os.path.normpath(relative) if isinstance(relative, str) else ''
    if (not isinstance(expected_sha, str) or
            re.fullmatch(r'[0-9a-f]{64}', expected_sha) is None or
            not normalized.endswith('.json') or
            os.path.basename(normalized) != f'{expected_sha}.json' or
            not os.path.isfile(path)):
        qa.fail(f'{code}: grade evidence must be existing content-addressed JSON '
                'with a matching registry sha256')
        return None
    try:
        with open(path, 'rb') as artifact:
            raw = artifact.read()
        actual_sha = hashlib.sha256(raw).hexdigest()
        evidence = json.loads(raw, parse_constant=_reject_json_constant,
                              object_pairs_hook=_reject_duplicate_keys)
        if raw != canonical_grade_evidence_bytes(evidence):
            raise GradeEvidenceError('state artifact is not canonical JSON')
        checked = validate_compiled_state_document(evidence, code)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError,
            GradeEvidenceError, KeyError, TypeError) as exc:
        qa.fail(f'{code}: compiled grade evidence is invalid ({exc})')
        return None
    if actual_sha != expected_sha:
        qa.fail(f'{code}: grade evidence sha256 does not match its artifact')
        return None
    metrics = checked['metrics']
    for field in ('graded_mines', 'primary_sources', 'verbatim_quotes', 'page_cites'):
        if asserted.get(field) != metrics[field]:
            qa.fail(f'{code}: grade acceptance {field} does not match compiled evidence')
    if asserted.get('low_endowment_finding') != checked['low_endowment_finding']:
        qa.fail(f'{code}: grade acceptance low-endowment finding does not match '
                'compiled evidence')
    if checked['grade_requirement'].get('done_gate_eligible') is not True:
        qa.fail(f'{code}: compiled grade evidence is not DONE-gate eligible')
    return checked


def _evidence_jurisdiction_ids(value, jurisdiction_type, *, allow_empty=False):
    if (not isinstance(value, list) or
            (not value and not allow_empty) or len(value) != len(set(value))):
        return None
    if jurisdiction_type == 'county':
        valid = all(isinstance(item, str) and re.fullmatch(r'\d{5}', item)
                    for item in value)
    elif jurisdiction_type == 'recording_district':
        valid = all(_text(item) and item == item.strip() and len(item) <= 100
                    for item in value)
    else:
        valid = False
    return set(value) if valid else None


def _validate_recorder_evidence(qa, code, state, acceptance):
    """Reconcile the recorder claim join, portal matrix, and system identities."""
    if state.get('regime') != 'claim':
        return
    asserted = acceptance.get('recorders')
    if not isinstance(asserted, dict):
        qa.fail(f'{code}: recorder acceptance must be an object')
        return
    evidence = _strict_evidence_json(
        qa, asserted.get('evidence_artifact'), code, 'recorder inventory')
    if evidence is None:
        return
    jurisdiction_type = asserted.get('jurisdiction_type')
    asserted_active = asserted.get('active_claims')
    expected_type = 'recording_district' if code == 'AK' else 'county'
    configured = state.get('recorder') if isinstance(state.get('recorder'), dict) else {}
    expected_evidence_keys = {
        'schema_version', 'state', 'jurisdiction_type', 'inventory_complete',
        'active_claims', 'live_claim_jurisdiction_ids',
        'covered_jurisdiction_ids', 'matrix_jurisdiction_ids', 'claim_systems',
    }
    live = _evidence_jurisdiction_ids(
        asserted.get('live_claim_jurisdiction_ids'), jurisdiction_type,
        allow_empty=asserted_active == 0)
    covered = _evidence_jurisdiction_ids(
        asserted.get('covered_jurisdiction_ids'), jurisdiction_type,
        allow_empty=asserted_active == 0)
    if (set(evidence) != expected_evidence_keys or
            evidence.get('schema_version') != 1 or evidence.get('state') != code or
            jurisdiction_type != expected_type or
            configured.get('jurisdiction_type') != expected_type or
            evidence.get('jurisdiction_type') != expected_type or
            evidence.get('inventory_complete') is not True or
            asserted.get('inventory_complete') is not True or
            not _is_int(asserted_active, 0) or
            live is None or covered is None or live != covered or
            ((asserted_active == 0) != (live == set()))):
        qa.fail(f'{code}: recorder evidence jurisdiction schema/coverage is invalid')
        return

    evidence_live = _evidence_jurisdiction_ids(
        evidence.get('live_claim_jurisdiction_ids'), expected_type,
        allow_empty=asserted_active == 0)
    evidence_covered = _evidence_jurisdiction_ids(
        evidence.get('covered_jurisdiction_ids'), expected_type,
        allow_empty=asserted_active == 0)
    evidence_matrix = _evidence_jurisdiction_ids(
        evidence.get('matrix_jurisdiction_ids'), expected_type,
        allow_empty=asserted_active == 0)
    matrix = configured.get('matrix')
    matrix_ids = ([item.get('jurisdiction_id') for item in matrix]
                  if isinstance(matrix, list) and
                  all(isinstance(item, dict) for item in matrix) else [])
    matrix_set = _evidence_jurisdiction_ids(
        matrix_ids, expected_type, allow_empty=asserted_active == 0)
    if (evidence.get('active_claims') != asserted_active or
            evidence_live != live or evidence_covered != covered or
            evidence_matrix != live or matrix_set != live):
        qa.fail(f'{code}: recorder evidence live/covered/matrix jurisdiction IDs '
                'do not reconcile exactly')

    system_rows = evidence.get('claim_systems')
    expected_systems = {
        item.get('id') for item in state.get('claim_systems', [])
        if isinstance(item, dict) and _text(item.get('id'))}
    if (not isinstance(system_rows, list) or
            any(not isinstance(item, dict) or not _text(item.get('system_id'))
                for item in system_rows) or
            len(system_rows) != len({item.get('system_id') for item in system_rows
                                    if isinstance(item, dict)}) or
            {item.get('system_id') for item in system_rows
             if isinstance(item, dict)} != expected_systems):
        qa.fail(f'{code}: recorder evidence claim-system identity is invalid')
        return
    system_union = set()
    system_active = 0
    registry_systems = {
        item.get('id'): item for item in state.get('claim_systems', [])
        if isinstance(item, dict)}
    for item in system_rows:
        system_ids = item.get('live_claim_jurisdiction_ids')
        active_count = item.get('active_claims')
        registered_counts = registry_systems.get(item.get('system_id'), {}).get(
            'source_layer_counts')
        registered_active = (registered_counts.get('active')
                             if isinstance(registered_counts, dict) else None)
        if (not _is_int(active_count, 0) or active_count != registered_active or
                ((active_count == 0) != (system_ids == []))):
            qa.fail(f'{code}: recorder evidence active counts do not match the '
                    'published claim-system inventories')
            return
        system_active += active_count
        # A claim system may have no live records, but any IDs it does assert
        # must use the state's recorder-jurisdiction model.
        if system_ids == []:
            continue
        parsed = _evidence_jurisdiction_ids(system_ids, expected_type)
        if parsed is None:
            qa.fail(f'{code}: recorder evidence system jurisdiction IDs are invalid')
            return
        system_union.update(parsed)
    if system_active != asserted_active or system_union != live:
        qa.fail(f'{code}: recorder evidence system inventories do not union to the '
                'active count/live-claim jurisdictions')


def _validate_ci_evidence(qa, code, acceptance):
    """Replay immutable browser evidence against the current release inputs."""
    asserted = acceptance.get('ci_scale')
    if not isinstance(asserted, dict):
        qa.fail(f'{code}: CI acceptance must be an object')
        return
    try:
        artifact = _resolve_artifact(
            asserted.get('evidence_artifact'), code, release=True)
        evidence = validate_ci_release_evidence_file(
            artifact, code,
            manifest_path=os.path.join(SITE, 'data', 'manifest.json'),
            coverage_path=os.path.join(SITE, 'data', 'coverage.json'),
            budgets_path=BUDGETS,
            expected_sha=asserted.get('sha256'))
    except (CIAcceptanceError, OSError, TypeError, ValueError) as exc:
        qa.fail(f'{code}: CI acceptance evidence is invalid ({exc})')
        return
    toggle = evidence.get('state_toggle')
    measurements = evidence.get('measurements')
    limits = evidence.get('budget_limits')
    if (evidence.get('run_url') != asserted.get('run_url') or
            evidence.get('commit') != asserted.get('commit') or
            evidence.get('statewide_browser_json') is not False or
            asserted.get('statewide_browser_json') is not False or
            not isinstance(toggle, dict) or toggle.get('state') != code or
            toggle.get('enabled') is not True or toggle.get('green') is not True or
            asserted.get('state_toggle_green') is not True or
            not isinstance(measurements, dict) or
            measurements.get('heap_mb') != asserted.get('heap_mb') or
            measurements.get('bulk_origin_storage_mb') !=
            asserted.get('bulk_origin_storage_mb') or
            not _is_finite(measurements.get('heap_mb'), minimum=0) or
            not _is_finite(measurements.get('bulk_origin_storage_mb'), minimum=0)):
        qa.fail(f'{code}: CI evidence does not bind the green state-toggle run, '
                'commit, measurements, and tiled-only result')
        return
    if (not isinstance(limits, dict) or
            measurements['heap_mb'] > limits['heap_mb_max'] or
            measurements['bulk_origin_storage_mb'] >
            limits['bulk_origin_storage_mb_max']):
        qa.fail(f'{code}: CI evidence measurements exceed the accepted budgets')


def _validate_expiration_watch_evidence(qa, code, state, acceptance):
    """Bind claim-state DONE to a complete current-system watch result."""
    if state.get('regime') != 'claim':
        return
    asserted = acceptance.get('expiration_watch')
    if not isinstance(asserted, dict):
        qa.fail(f'{code}: expiration-watch acceptance must be an object')
        return
    evidence = _strict_evidence_json(
        qa, asserted.get('evidence_artifact'), code, 'expiration-watch state run')
    if evidence is None:
        return
    expected_systems = {item.get('id') for item in state.get('claim_systems', [])
                        if isinstance(item, dict)}
    rows = evidence.get('systems')
    if (evidence.get('schema_version') != 1 or evidence.get('state') != code or
            evidence.get('run_id') != asserted.get('run_id') or
            evidence.get('generated') != asserted.get('generated') or
            evidence.get('complete') is not True or asserted.get('complete') is not True or
            not isinstance(rows, dict) or set(rows) != expected_systems or
            set(asserted.get('system_ids') or []) != expected_systems or
            any(not isinstance(row, dict) or row.get('status') != 'complete' or
                not _is_int(row.get('active_now'), minimum=0) or
                not isinstance(row.get('source_snapshot_sha256'), str) or
                re.fullmatch(r'[0-9a-f]{64}',
                             row.get('source_snapshot_sha256', '')) is None
                for row in rows.values())):
        qa.fail(f'{code}: expiration-watch evidence does not bind a complete run and '
                'checksummed snapshot for every claim system')


def _validate_claim_publication_evidence(qa, code, state):
    """Bind each claim PMTiles publication to complete upstream inventory evidence."""
    if state.get('regime') != 'claim':
        return
    try:
        with open(STATE_CLIPS, 'rb') as clip_file:
            expected_clip_sha = hashlib.sha256(clip_file.read()).hexdigest()
    except OSError as exc:
        qa.fail(f'{code}: cannot hash authoritative state clips ({exc})')
        return
    for system in state.get('claim_systems', []):
        if not isinstance(system, dict):
            qa.fail(f'{code}: claim publication system row is invalid')
            continue
        system_id = system.get('id')
        evidence = _strict_evidence_json(
            qa, system.get('publication_inventory_artifact'), code,
            f'{system_id} publication inventory')
        if evidence is None:
            continue
        counts = system.get('source_layer_counts')
        layers = system.get('source_layers')
        evidence_counts = evidence.get('source_layer_counts')
        counts_valid = (
            isinstance(layers, list) and layers and len(layers) == len(set(layers)) and
            isinstance(counts, dict) and set(counts) == set(layers) and
            all(_is_int(value, minimum=0) for value in counts.values()) and
            sum(counts.values()) > 0)
        split_valid = True
        if system_id == 'federal_mlrs':
            registry_parts = system.get('publication_artifacts')
            evidence_parts = evidence.get('publication_artifacts')
            split_valid = (
                isinstance(registry_parts, dict) and
                isinstance(evidence_parts, dict) and
                set(registry_parts) == set(evidence_parts) == {'claims', 'open_ground'})
            if split_valid:
                for part_id, part in registry_parts.items():
                    part_evidence = evidence_parts.get(part_id)
                    part_layers = part.get('source_layers')
                    expected_part_counts = ({layer: counts.get(layer)
                                             for layer in part_layers}
                                            if isinstance(part_layers, list) and
                                            isinstance(counts, dict) else None)
                    if (not isinstance(part_evidence, dict) or
                            part_evidence.get('complete') is not True or
                            part_evidence.get('truncated') is not False or
                            part_evidence.get('source_layers') != part_layers or
                            part_evidence.get('source_layer_counts') !=
                            expected_part_counts or
                            part_evidence.get('artifact_sha256') != part.get('sha256') or
                            not isinstance(part_evidence.get('input_sha256'), str) or
                            re.fullmatch(r'[0-9a-f]{64}',
                                         part_evidence.get('input_sha256', '')) is None):
                        split_valid = False
                        break
                    if (part_id == 'open_ground' and
                            part_evidence.get('source_id_inventory') !=
                            part.get('source_id_inventory')):
                        split_valid = False
                        break
        else:
            split_valid = (
                isinstance(evidence.get('input_sha256'), str) and
                re.fullmatch(r'[0-9a-f]{64}', evidence.get('input_sha256', '')) is not None)
        if (evidence.get('schema_version') != 1 or
                evidence.get('system_id') != system_id or
                evidence.get('source_id') != system.get('source_id') or
                evidence.get('retrieved') != system.get('retrieved') or
                evidence.get('complete') is not True or system.get('complete') is not True or
                evidence.get('truncated') is not False or system.get('truncated') is not False or
                evidence.get('pagination_exhausted') is not True or
                evidence.get('state_clip_sha256') != expected_clip_sha or
                not split_valid or not counts_valid or evidence_counts != counts):
            qa.fail(f'{code}: {system_id} publication evidence does not bind complete '
                    'pagination, split artifact/input checksums, and exact source-layer counts')


def _validate_nonclaim_equivalent_evidence(qa, code, state):
    """Bind AML/trust ingestion or explicit gaps to reviewed source evidence."""
    if state.get('regime') != 'non_claim':
        return
    for kind, registry in (('aml', state.get('aml')),
                           ('trust_land', state.get('trust_land'))):
        if not isinstance(registry, dict):
            qa.fail(f'{code}: {kind} release registry is invalid')
            continue
        status = registry.get('release_inventory_status')
        evidence = _strict_evidence_json(
            qa, registry.get('evidence_artifact'), code,
            f'{kind} release inventory decision')
        if evidence is None:
            continue
        source_urls = evidence.get('official_source_urls')
        common_valid = (
            evidence.get('schema_version') == 1 and
            evidence.get('state') == code and evidence.get('kind') == kind and
            evidence.get('release_inventory_status') == status and
            evidence.get('complete') is True and
            isinstance(evidence.get('reviewed'), str) and
            re.fullmatch(r'\d{4}-\d{2}-\d{2}', evidence.get('reviewed', '')) is not None and
            isinstance(source_urls, list) and bool(source_urls) and
            all(_https(url) for url in source_urls)
        )
        registry_urls = [value for key, value in registry.items()
                         if key.endswith('_url') and _https(value)]
        if (not common_valid or
                (registry_urls and not any(url in source_urls for url in registry_urls))):
            qa.fail(f'{code}: {kind} evidence does not bind its state, reviewed '
                    'decision, and official registry source')
            continue
        if status == 'ingested_complete':
            metadata = registry.get('layer_metadata')
            counts = ({layer: item.get('n') for layer, item in metadata.items()}
                      if isinstance(metadata, dict) and
                      all(isinstance(item, dict) for item in metadata.values()) else None)
            if (evidence.get('artifact_sha256') != registry.get('sha256') or
                    evidence.get('source_layer_counts') != counts or
                    not isinstance(evidence.get('input_sha256'), str) or
                    re.fullmatch(r'[0-9a-f]{64}', evidence.get('input_sha256', '')) is None or
                    not isinstance(evidence.get('retrieved'), str) or
                    re.fullmatch(r'\d{4}-\d{2}-\d{2}', evidence.get('retrieved', '')) is None):
                qa.fail(f'{code}: {kind} ingestion evidence does not bind input/archive '
                        'checksums, retrieval date, and exact source-layer counts')
        elif status in ('documented_unavailable', 'not_applicable'):
            if (evidence.get('spatial_inventory_available') is not False or
                    not _text(evidence.get('finding'), minimum=40)):
                qa.fail(f'{code}: {kind} unavailability evidence needs an explicit '
                        'reviewed finding and spatial_inventory_available=false')
        else:
            qa.fail(f'{code}: {kind} evidence has no releasable inventory status')


def _validate_district_anchor_evidence(qa, code, state, acceptance):
    """Require PP 610 extracted evidence, including compiler-nested evidence."""
    asserted = acceptance.get('district_anchor')
    if not isinstance(asserted, dict):
        qa.fail(f'{code}: PP 610 district-anchor acceptance must be an object')
        return
    grades = acceptance.get('grades')
    grade_artifact = (grades.get('evidence_artifact')
                      if isinstance(grades, dict) else None)
    artifact = asserted.get('artifact') or grade_artifact
    if artifact != grade_artifact:
        qa.fail(f'{code}: PP 610 must use the same immutable state artifact as grades')
        return
    evidence = _strict_evidence_json(qa, artifact, code, 'PP 610 district anchor')
    if evidence is None:
        return
    evidence = evidence.get('pp610')
    if not isinstance(evidence, dict):
        qa.fail(f'{code}: compiled grade evidence has no nested PP 610 object')
        return
    pp610 = next((item for item in state.get('historic_serials', [])
                  if isinstance(item, dict) and item.get('source_id') == 'pp610'), None)
    source = evidence.get('source')
    districts = evidence.get('districts')
    count = asserted.get('district_count')
    rows_valid = (
        isinstance(districts, list) and
        all(isinstance(row, dict) and _text(row.get('district_id')) and
            _text(row.get('name')) and _text(row.get('page_cite')) and
            any(char.isdigit() for char in row.get('page_cite', '')) and
            _text(row.get('verbatim_quote'), minimum=8) and
            row.get('quote_verbatim') is True and
            isinstance(row.get('page_text_sha256'), str) and
            re.fullmatch(r'[0-9a-f]{64}', row.get('page_text_sha256', ''))
            for row in districts) and
        len({row.get('district_id') for row in districts
             if isinstance(row, dict)}) == len(districts)
    )
    no_district = asserted.get('no_district_finding')
    nested_finding = evidence.get('no_district_finding')
    if (not isinstance(source, dict) or source.get('source_id') != 'pp610' or
            not isinstance(pp610, dict) or source.get('url') != pp610.get('url') or
            source.get('document_sha256') != asserted.get('source_sha256') or
            not isinstance(asserted.get('source_sha256'), str) or
            re.fullmatch(r'[0-9a-f]{64}', asserted.get('source_sha256', '')) is None or
            evidence.get('complete') is not True or asserted.get('complete') is not True or
            evidence.get('district_count') != count or
            not _is_int(count, minimum=0) or not rows_valid or len(districts) != count or
            (count == 0 and (not _text(no_district, minimum=40) or
                             not isinstance(nested_finding, dict) or
                             nested_finding.get('finding') != no_district or
                             nested_finding.get('review_complete') is not True or
                             not isinstance(nested_finding.get('pages_reviewed'), list) or
                             not nested_finding.get('pages_reviewed'))) or
            (count > 0 and (no_district is not None or nested_finding is not None))):
        qa.fail(f'{code}: PP 610 district evidence does not reconcile its source, '
                'page-cited extraction, count, and explicit zero finding')


def _validate_ranked_target_evidence(qa, code, state, quad_acceptance):
    """Bind the top-five quad set to a checksummed scoring-engine result."""
    target_set_artifact = quad_acceptance.get('ranked_targets_artifact')
    target_set_sha = quad_acceptance.get('ranked_targets_sha256')
    try:
        ranked_path = _resolve_artifact(target_set_artifact, code, release=True)
    except ValueError as exc:
        qa.fail(f'{code} ranked top-five target set: {exc}')
        return None
    if (not isinstance(target_set_sha, str) or
            re.fullmatch(r'[0-9a-f]{64}', target_set_sha) is None or
            os.path.basename(ranked_path) != f'{target_set_sha}.json'):
        qa.fail(f'{code}: ranked top-five target set is not content-addressed')
        return None
    try:
        ranked = _load_json(ranked_path)
        ranked_actual_sha = _artifact_sha256(ranked_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'{code}: ranked top-five target set is not strict JSON ({exc})')
        return None
    if (not isinstance(ranked, dict) or ranked.get('state') != code or
            ranked_actual_sha != target_set_sha):
        qa.fail(f'{code}: ranked top-five target set identity/hash is invalid')
        return None
    method_id = ranked.get('method_id')
    scoring_path = ranked.get('scoring_artifact')
    scoring_sha = ranked.get('scoring_sha256')
    input_hashes = ranked.get('input_sha256s')
    if (not _text(method_id) or not _text(scoring_path) or
            not isinstance(scoring_sha, str) or
            re.fullmatch(r'[0-9a-f]{64}', scoring_sha) is None or
            not isinstance(input_hashes, dict) or not input_hashes or
            any(not _text(key) or not isinstance(value, str) or
                re.fullmatch(r'[0-9a-f]{64}', value) is None
                for key, value in input_hashes.items())):
        qa.fail(f'{code}: ranked targets lack a versioned scoring method and input hashes')
        return None
    try:
        scoring_file = _resolve_artifact(scoring_path, code, release=True)
        if os.path.basename(scoring_file) != f'{scoring_sha}.json':
            raise ValueError('scoring artifact is not content-addressed')
        scoring = _load_json(scoring_file)
        actual_sha = _artifact_sha256(scoring_file)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'{code}: state scoring output cannot be loaded/checksummed ({exc})')
        return None
    if (not isinstance(scoring, dict) or scoring.get('state') != code or
            actual_sha != scoring_sha):
        qa.fail(f'{code}: state scoring output identity/hash is invalid')
        return None

    # Preferred WS11 compiler contract. Reuse the compiler's strict semantic
    # replay so release validation cannot drift to a weaker parallel schema.
    if scoring.get('dataset') == 'ws11-national-target-scoring-evidence':
        try:
            from build_national_target_scoring_evidence import (
                _registry_context, validate_compiled_state_document)
            registry, registry_sha = _registry_context()
            validate_compiled_state_document(
                scoring, code, registry[code], registry_sha)
        except (KeyError, ValueError) as exc:
            qa.fail(f'{code}: compiled national target-scoring evidence is invalid ({exc})')
            return None
        compiled_inputs = {
            name: descriptor['sha256']
            for name, descriptor in scoring['input_artifacts'].items()
        }
        if (scoring.get('method_id') != method_id or
                input_hashes != compiled_inputs):
            qa.fail(f'{code}: ranked target wrapper does not bind the compiled '
                    'method/input artifacts')
            return None
        score_rows = scoring.get('targets')
        if not isinstance(score_rows, list) or len(score_rows) < 5:
            qa.fail(f'{code}: compiled scoring evidence has fewer than five targets')
            return None
        expected = [
            (row['target_id'], row['rank'], row['score']['total'])
            for row in score_rows[:5]
        ]
        ranked_rows = ranked.get('targets')
        if (not isinstance(ranked_rows, list) or len(ranked_rows) != 5 or
                any(not isinstance(row, dict) or set(row) != {
                    'target_id', 'rank', 'score'} or
                    not _text(row.get('target_id')) or
                    not _is_int(row.get('rank'), minimum=1) or
                    not _is_finite(row.get('score')) for row in ranked_rows)):
            qa.fail(f'{code}: ranked top-five target evidence schema is invalid')
            return None
        actual = [(row['target_id'], row['rank'], row['score'])
                  for row in sorted(ranked_rows, key=lambda row: row['rank'])]
        if (actual != expected or ranked.get('schema_version') != 1 or
                ranked.get('method_id') != method_id or
                ranked.get('input_sha256s') != compiled_inputs):
            qa.fail(f'{code}: ranked top-five targets do not match the compiled '
                    'national scoring artifact')
            return None
        return {row[0] for row in expected}

    # Legacy scoring evidence remains readable for existing reviewed AOI
    # bundles, but it must still be immutable and checksum-bound.
    required_inputs = {'grades', 'geology', 'land_context'}
    if state.get('regime') == 'claim':
        required_inputs.add('open_ground')
    if (re.fullmatch(r'ws11-target-score-v\d+', method_id) is None or
            not required_inputs <= set(input_hashes)):
        qa.fail(f'{code}: ranked targets lack the required scoring inputs')
        return None
    score_rows = scoring.get('targets')
    expected_open_status = ('not_applicable' if state.get('regime') == 'non_claim'
                            else 'measured')
    def score_row_valid(row):
        if not isinstance(row, dict):
            return False
        land = row.get('land_context')
        open_ground = row.get('open_ground')
        components = row.get('score_components')
        component_open = (components.get('open_ground')
                          if isinstance(components, dict) else None)
        if (not _text(row.get('target_id')) or
                not _is_int(row.get('rank'), minimum=1) or
                not _is_finite(row.get('score')) or
                not isinstance(land, dict) or
                not all(_text(land.get(field)) for field in
                        ('surface_class', 'mineral_class', 'approach')) or
                not isinstance(open_ground, dict) or
                open_ground.get('status') != expected_open_status or
                not isinstance(components, dict) or
                not _is_finite(components.get('grade'), minimum=0) or
                not _is_finite(components.get('geology'), minimum=0) or
                not isinstance(component_open, dict) or
                component_open.get('status') != expected_open_status):
            return False
        open_value = component_open.get('value')
        if expected_open_status == 'measured':
            if not _is_finite(open_value, minimum=0):
                return False
        elif open_value is not None:
            return False
        total = (components['grade'] + components['geology'] +
                 (open_value if open_value is not None else 0))
        if not math.isclose(row['score'], total, rel_tol=1e-9, abs_tol=1e-6):
            return False
        if state.get('regime') == 'claim':
            rich = row.get('rich_open')
            if (not isinstance(rich, dict) or rich.get('status') != 'measured' or
                    not _is_finite(rich.get('score'), minimum=0) or
                    not math.isclose(rich['score'], row['score'],
                                     rel_tol=1e-9, abs_tol=1e-6)):
                return False
        return True
    score_schema = (
        scoring.get('schema_version') == 1 and scoring.get('method_id') == method_id and
        scoring.get('input_sha256s') == input_hashes and actual_sha == scoring_sha and
        isinstance(score_rows, list) and len(score_rows) >= 5 and
        all(score_row_valid(row) for row in score_rows)
    )
    if not score_schema:
        qa.fail(f'{code}: checksummed scoring output schema/regime semantics are invalid')
        return None
    ordered_scores = sorted(score_rows, key=lambda row: row['rank'])
    def evidence_order(row):
        open_term = row['score_components']['open_ground']
        value = open_term.get('value')
        status = open_term['status']
        status_rank = (0 if status == 'measured' and value > 0 else
                       1 if status == 'not_applicable' else
                       2 if status == 'measured' and value == 0 else 3)
        return (-row['score'], status_rank, row['target_id'])
    expected_order = sorted(score_rows, key=evidence_order)
    if ([row['rank'] for row in ordered_scores[:5]] != [1, 2, 3, 4, 5] or
            [row['target_id'] for row in ordered_scores] !=
            [row['target_id'] for row in expected_order] or
            len({row['target_id'] for row in ordered_scores}) != len(ordered_scores)):
        qa.fail(f'{code}: scoring output lacks unique deterministic richOpen ranks; '
                'N/A and measured zero must remain distinct')
        return None
    ranked_rows = ranked.get('targets')
    if (not isinstance(ranked_rows, list) or len(ranked_rows) != 5 or
            any(not isinstance(row, dict) or _text(row.get('target_id')) is False or
                not _is_int(row.get('rank'), minimum=1) or
                not _is_finite(row.get('score')) for row in ranked_rows)):
        qa.fail(f'{code}: ranked top-five target evidence schema is invalid')
        return None
    expected = [(row['target_id'], row['rank'], row['score'])
                for row in ordered_scores[:5]]
    actual = [(row['target_id'], row['rank'], row['score'])
              for row in sorted(ranked_rows, key=lambda row: row['rank'])]
    if actual != expected:
        qa.fail(f'{code}: ranked top-five targets do not match the scoring artifact')
        return None
    return {row['target_id'] for row in ordered_scores[:5]}


def validate_release_evidence(qa, states, grade_metrics=None, require_all=False):
    """Tie asserted gate metrics to locally inspectable release evidence."""
    for code, state in states.items():
        if not state['release']['enabled'] and not require_all:
            continue
        acceptance = state['release']['acceptance']
        _validate_claim_publication_evidence(qa, code, state)
        _validate_nonclaim_equivalent_evidence(qa, code, state)
        _validate_recorder_evidence(qa, code, state, acceptance)
        _validate_expiration_watch_evidence(qa, code, state, acceptance)
        _validate_ci_evidence(qa, code, acceptance)
        checked_grades = _validate_grade_evidence(qa, code, state, acceptance)
        _validate_district_anchor_evidence(qa, code, state, acceptance)
        # `checked_grades` is deliberately unused here beyond forcing strict
        # validation. All release counters and low-endowment assertions are
        # reconciled inside that one artifact boundary.
        del checked_grades
        quad_targets = acceptance['quad_maps']['targets']
        ranked_ids = _validate_ranked_target_evidence(
            qa, code, state, acceptance['quad_maps'])
        accepted_ids = {item.get('target_id') for item in quad_targets
                        if isinstance(item, dict)}
        if ranked_ids is not None and accepted_ids != ranked_ids:
            qa.fail(f'{code}: quad inventories do not match the ranked top-five targets')
        for item in quad_targets:
            evidence = _strict_evidence_json(
                qa, item.get('inventory_artifact'), code,
                f'quad inventory {item.get("target_id")}')
            if evidence is None:
                continue
            quads = evidence.get('quadrangles')
            if (evidence.get('target_id') != item.get('target_id') or
                    not isinstance(quads, list) or not quads or
                    any(not isinstance(quad, dict) or
                        not _text(quad.get('map_id')) or not _https(quad.get('url')) or
                        not _text(quad.get('scale')) for quad in quads)):
                qa.fail(f'{code}: quad inventory {item.get("target_id")} schema is invalid')


def validate_browser_delivery_contract(qa, release=False):
    """Prevent browser/deploy regressions to statewide JSON delivery."""
    try:
        with open(os.path.join(SITE, 'index.html'), encoding='utf-8') as site_file:
            source = site_file.read()
    except OSError as exc:
        qa.fail(f'browser entry point cannot be read: {exc}')
        return
    state_code = '(?:' + '|'.join(code.lower() for code in ALL_STATES) + ')'
    statewide_path = (
        r'data/(?:claims|sites|boundaries|geophys|faults|land-context|land_context|'
        r'aml|trust-land|trust_land)/|'
        r'data/(?:geology|targets|openground|plss)/(?:states?/)?' +
        state_code + r'(?:[._/-]|$)')
    forbidden = re.compile(
        r"(?:fetch|jget)\s*\(\s*(?:`|'|\")(?=[^`'\"\n]*(?:" + statewide_path + r"))",
        re.I)
    if forbidden.search(source):
        qa.fail('browser code fetches a whole-state JSON/GeoJSON delivery path')
    # Dynamic concatenation can hide the prefix from the fetch call. Reject
    # any literal public legacy prefix in release HTML; descriptive copy uses
    # generic wording and does not need these implementation paths.
    if release and re.search(statewide_path, source, re.I):
        qa.fail('release browser contains a statewide JSON/GeoJSON path literal')
    deploy_path = os.path.join(ROOT, 'infra', 'deploy.sh')
    try:
        with open(deploy_path, encoding='utf-8') as deploy_file:
            deploy = deploy_file.read()
    except OSError as exc:
        qa.fail(f'deploy script cannot be read: {exc}')
        return
    for prefix in ('claims/*', 'sites/*'):
        if not re.search(r'--exclude\s+["\']?' + re.escape(prefix), deploy):
            qa.fail(f'deploy sync must exclude legacy data/{prefix}')
    for prefix in ('data/claims/', 'data/sites/'):
        if not re.search(r'aws\s+s3\s+rm\s+["\']s3://\$bucket/' +
                         re.escape(prefix) + r'["\']\s+--recursive', deploy, re.I):
            qa.fail(f'deploy must remove the public legacy {prefix} prefix')
    boundary_folder = os.path.join(SITE, 'data', 'boundaries')
    if os.path.isdir(boundary_folder):
        for name in os.listdir(boundary_folder):
            if name.lower().endswith(('.json', '.geojson')):
                qa.fail(f'public administrative boundary JSON is forbidden: '
                        f'data/boundaries/{name}')
    geophys_folder = os.path.join(SITE, 'data', 'geophys')
    if os.path.isdir(geophys_folder):
        for name in os.listdir(geophys_folder):
            if name.lower().endswith(('.json', '.geojson')):
                qa.fail(f'public national geophysics JSON is forbidden: '
                        f'data/geophys/{name}; publish the PMTiles survey index')
    # A future unchecked file must not become deployable merely because no
    # code references it yet. AOI bundles such as geology/cassia.json remain
    # valid; a two-letter state basename in a statewide layer family does not.
    sensitive = ('geology', 'faults', 'land-context', 'land_context', 'aml',
                 'trust-land', 'trust_land', 'targets', 'openground', 'plss')
    for folder in sensitive:
        root = os.path.join(SITE, 'data', folder)
        if not os.path.isdir(root):
            continue
        for walk_root, _, names in os.walk(root):
            for name in names:
                stem, ext = os.path.splitext(name)
                if (ext.lower() in ('.json', '.geojson') and
                        stem.upper() in ALL_STATES):
                    rel = os.path.relpath(os.path.join(walk_root, name), SITE)
                    qa.fail(f'public statewide browser JSON is forbidden: {rel}')


def _finite_coordinate(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and low <= value <= high)


def _artifact_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _alaska_description(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} PMTiles description must be JSON text')
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant,
                            object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f'{label} PMTiles description is invalid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise ValueError(f'{label} PMTiles description must contain an object')
    return parsed


_ALASKA_INVENTORY_FIELDS = {
    'status', 'source_records', 'maxzoom_feature_instances',
    'maxzoom_unique_tiled_ids', 'ids_sha256',
}
_ALASKA_COMBINED_INVENTORY_FIELDS = _ALASKA_INVENTORY_FIELDS | {
    'base_records', 'precision_records', 'disjoint_union_complete',
}
_ALASKA_CLAIM_PROPERTIES = (
    'st', 'system', 'source_oid', 'serial', 'status', 'source_status',
    'acres', 'part', 'url', 'lon', 'lat',
)
# Independent acceptance pin for the 24 unchanged official source polygons
# routed around z13 MVT quantization. Keep this validator dependency-neutral:
# build_alaska_pmtiles imports _pmtiles_header from this module.
ALASKA_PRECISION_SOURCE_GEOMETRY_SHA256 = {
    'active': {
        4387: '60d1d17f5abcd19935b41e0d8e44dce0bfa78d1cecda62fec2cab8888ee1e915',
        28315: '83afffd05ba27e39acf242f4c067d4042e369648ac3ccb77a784debc3163273b',
        28336: '0eba77381d275119b2c36bd344d18edc0ad2bececc6235432556137109f09c63',
        28339: '8317bf06b9cdd7be21e4b2602f25003c0567725b7f04991b917dade4140f27fa',
        29696: '7645d613407e2619845355e52e757e90290ac757f8b5efaf99ac7ec815a55769',
        34704: '0db415acc689575f3cdef4a09c69612e1ae7c8ce6fafd961842223e84efdc15f',
    },
    'closed': {
        2862: '3b459374998c2d7023b046d7ca7480ca7b54b1aefccda4a668beff8817210d4b',
        42145: 'fe026666d90e8641bca33a1f6e4f428905c14880414e82a4c529f487ae2a596f',
        58122: '36c641ced9ac7733e2bb865ca14f15c508da96abe53f4f6cf1848ad1a3605f81',
        58231: '6e7570b91d31ec86699ff51bb7fcf740029b979093563d18b5a54f47b87f2450',
        58233: 'f073b192b25b590d07b6d3f7692ce9cfea3c279a6c05cb83d2deff168f172f39',
        61311: 'a8623f238dcfa6a8ec37c7d3ae83960b8e54c4139e2570ed57c783b7dff3feb6',
        64278: '9223f98695b0eb8f7eeea92193c760abc9505bc318c6cee499a1887b1839ccbd',
        64745: '42ff841fc30745f6664629f2c5ae96ff763ba3a95c7bd7b3f92a3ba95f72f2d6',
        67536: '7b7a043539104c95973d937f1a33ebcf8b7ebba3d5b1e39d008ec47e57533ae1',
        67799: '997b9236399cfa68ab7efcb1a356cd5d7983098d3b2a8b640e6e8f073f0bbc15',
        67813: 'e062ae6a0bf019924fe13fccbad7e1533d8390c7b428d9ae776cc0fe8f6d7a69',
        67860: '2d6e0167d5fb48f4c96e5fc7c9baacfdc8c366465010d41398368c451355890d',
        69440: '8c616c34ef38c7492f44c54d323e3f82eb6ce92f01563cbfe4f1bb7239c0beda',
        69857: '66824c3dd832ead315ab336ef11afc0ecb7aaaa9b048ec3d31d1e8ed220f7e5e',
        76315: '626243247c52705b0b57bbef7aea15969ad0a25fdd7c1a0e9af2d360dd203b53',
        76538: '5acf399fa4a12a1b04f78227ad7280c9d9a2f9a41901c30ee76c50eccd33913a',
        78830: 'aed914c27887785c9bc915c9eff9a23e6963ea5537ae71f879abd3ae07d5f39d',
        79329: '81709f4769aa9bcaa68951ddbb9d708f79324fd17bbed79a3a0cccc46a5b8b9a',
    },
}
_ALASKA_PRECISION_REQUIRED_FIELDS = {
    'file', 'format', 'source_layers', 'n', 'by_status',
    'source_objectids', 'source_geometry_sha256', 'source_id_inventory',
    'minzoom', 'maxzoom', 'activation_zoom', 'bytes', 'sha256', 'note',
}


def _alaska_claim_delivery_schema_valid(entry):
    """Return whether the top-level half of the split AK claim contract is exact."""
    return (
        isinstance(entry, dict) and entry.get('format') == 'pmtiles' and
        entry.get('file') == 'data/tiles/claims/ak-state.pmtiles' and
        entry.get('source_layers') == ['active', 'pending', 'closed'] and
        entry.get('source') == ALASKA_STATE_CLAIMS_SOURCE and
        entry.get('system') == 'alaska_state_claims' and
        entry.get('jurisdiction') == 'state' and
        entry.get('minzoom') == 0 and entry.get('maxzoom') == 13 and
        entry.get('activation_zoom') == 8 and
        isinstance(entry.get('retrieved'), str) and
        re.fullmatch(r'\d{4}-\d{2}-\d{2}', entry['retrieved']) is not None and
        isinstance(entry.get('staging_sha256'), str) and
        re.fullmatch(r'[0-9a-f]{64}', entry['staging_sha256']) is not None)


def _alaska_precision_descriptor_schema_valid(precision):
    """Accept only the one checksum-bearing nested public PMTiles descriptor."""
    expected_objectids = {
        status: sorted(ALASKA_PRECISION_SOURCE_GEOMETRY_SHA256[status])
        for status in ('active', 'closed')
    }
    expected_geometry = {
        status: {
            str(source_oid): digest
            for source_oid, digest in sorted(
                ALASKA_PRECISION_SOURCE_GEOMETRY_SHA256[status].items())
        }
        for status in ('active', 'closed')
    }
    expected_counts = {
        status: len(expected_objectids[status])
        for status in ('active', 'closed')
    }
    counts = precision.get('by_status') if isinstance(precision, dict) else None
    inventories = (
        precision.get('source_id_inventory')
        if isinstance(precision, dict) else None)
    inventory_counts = {
        'active_precision': expected_counts['active'],
        'closed_precision': expected_counts['closed'],
    }
    inventory_valid = (
        isinstance(inventories, dict) and
        set(inventories) == set(inventory_counts) and
        all(
            isinstance(inventories.get(layer), dict) and
            set(inventories[layer]) == _ALASKA_INVENTORY_FIELDS and
            inventories[layer].get('status') == 'complete_at_retrieval' and
            inventories[layer].get('source_records') == count and
            inventories[layer].get('maxzoom_unique_tiled_ids') == count and
            _is_int(inventories[layer].get('maxzoom_feature_instances'), count) and
            isinstance(inventories[layer].get('ids_sha256'), str) and
            re.fullmatch(
                r'[0-9a-f]{64}', inventories[layer]['ids_sha256']) is not None
            for layer, count in inventory_counts.items()))
    return (
        isinstance(precision, dict) and
        set(precision) == _ALASKA_PRECISION_REQUIRED_FIELDS and
        precision.get('file') ==
        'data/tiles/claims/ak-state-precision.pmtiles' and
        precision.get('format') == 'pmtiles' and
        precision.get('source_layers') ==
        ['active_precision', 'closed_precision'] and
        precision.get('minzoom') == 0 and precision.get('maxzoom') == 19 and
        precision.get('activation_zoom') == 19 and
        precision.get('n') == sum(expected_counts.values()) and
        counts == expected_counts and
        precision.get('source_objectids') == expected_objectids and
        precision.get('source_geometry_sha256') == expected_geometry and
        inventory_valid and
        _is_int(precision.get('bytes'), 1) and
        isinstance(precision.get('sha256'), str) and
        re.fullmatch(r'[0-9a-f]{64}', precision['sha256']) is not None and
        isinstance(precision.get('note'), str) and bool(precision['note'].strip()))


def _alaska_ids_sha256(ids):
    return hashlib.sha256(json.dumps(
        ids, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')).hexdigest()


def _alaska_compact_integer_inventory(ids):
    ordered = sorted(ids)
    return {
        'records': len(ordered),
        'minimum_id': ordered[0] if ordered else None,
        'maximum_id': ordered[-1] if ordered else None,
        'ids_sha256': _alaska_ids_sha256(ordered),
    }


def _scan_alaska_archive(qa, *, label, entry, layers, properties,
                         numeric_properties, counts, dataset, snapshot,
                         staging_sha256, source_id_inventory, minzoom,
                         maxzoom, geometry_types=None, string_properties=None,
                         feature_validator=None):
    """Fully scan one advertised Alaska archive and return its exact IDs."""
    try:
        artifact = _resolve_artifact(entry.get('file'), label)
    except ValueError as exc:
        qa.fail(f'national {label} PMTiles: {exc}')
        return None
    if not os.path.isfile(artifact):
        qa.fail(f'national {label} PMTiles artifact is missing')
        return None
    try:
        meta = _pmtiles_header(
            artifact, layers, properties, verify_feature_properties=True,
            expected_state='AK', collect_feature_ids=True,
            expected_geometry_types=geometry_types,
            feature_validator=feature_validator)
    except (OSError, ValueError) as exc:
        qa.fail(f'national {label} PMTiles: {exc}')
        return None
    if meta['source_layers'] != sorted(layers):
        qa.fail(f'national {label} PMTiles source layers are not exact')
    if meta['minzoom'] != minzoom or meta['maxzoom'] != maxzoom:
        qa.fail(
            f'national {label} PMTiles zoom range does not match its manifest')
    missing_metadata_properties = {
        layer: sorted(set(properties[layer]) - set(
            meta['field_types'].get(layer, {})))
        for layer in layers
        if set(properties[layer]) - set(meta['field_types'].get(layer, {}))
    }
    if missing_metadata_properties:
        qa.fail(
            f'national {label} PMTiles lacks required properties '
            f'{missing_metadata_properties}')
    wrong_numeric_types = {
        f'{layer}.{field}': meta['field_types'].get(layer, {}).get(field)
        for layer, fields in numeric_properties.items()
        for field in fields
        if meta['field_types'].get(layer, {}).get(field) != 'Number'
    }
    if wrong_numeric_types:
        qa.fail(
            f'national {label} PMTiles numeric property types are invalid: '
            f'{wrong_numeric_types}')
    wrong_string_types = {
        f'{layer}.{field}': meta['field_types'].get(layer, {}).get(field)
        for layer, fields in (string_properties or {}).items()
        for field in fields
        if meta['field_types'].get(layer, {}).get(field) != 'String'
    }
    if wrong_string_types:
        qa.fail(
            f'national {label} PMTiles string property types are invalid: '
            f'{wrong_string_types}')

    inventory_schema_valid = (
        isinstance(source_id_inventory, dict) and
        set(source_id_inventory) == set(layers))
    if not inventory_schema_valid:
        qa.fail(f'national {label} source-ID inventory is missing or invalid')
    ids_by_layer = {}
    instances_by_layer = {}
    for layer in layers:
        expected_count = counts.get(layer) if isinstance(counts, dict) else None
        ids = (meta.get('maxzoom_feature_ids') or {}).get(layer)
        instances = (meta.get('maxzoom_feature_instances') or {}).get(layer)
        if not isinstance(ids, list):
            ids = []
        ids_by_layer[layer] = ids
        instances_by_layer[layer] = instances
        digest = _alaska_ids_sha256(ids)
        if (not _is_int(expected_count, 0) or
                ids != sorted(set(ids)) or len(ids) != expected_count or
                not _is_int(instances, expected_count)):
            qa.fail(
                f'national {label} {layer} source records do not reconcile '
                'to unique maxzoom PMTiles feature IDs '
                f'(declared={expected_count}, tiled={len(ids)}, '
                f'instances={instances})')
        inventory = (source_id_inventory.get(layer)
                     if inventory_schema_valid else None)
        if (not isinstance(inventory, dict) or
                set(inventory) != _ALASKA_INVENTORY_FIELDS or
                inventory.get('status') != 'complete_at_retrieval' or
                inventory.get('source_records') != expected_count or
                inventory.get('maxzoom_feature_instances') != instances or
                inventory.get('maxzoom_unique_tiled_ids') != len(ids) or
                inventory.get('ids_sha256') != digest):
            qa.fail(
                f'national {label} {layer} source-ID inventory does not '
                'match the exact PMTiles feature set')
    if not _is_int(entry.get('bytes'), 1) or entry['bytes'] != meta['bytes']:
        qa.fail(f'national {label} manifest bytes do not match artifact')
    expected_sha = entry.get('sha256')
    if (not isinstance(expected_sha, str) or
            re.fullmatch(r'[0-9a-f]{64}', expected_sha) is None):
        qa.fail(f'national {label} manifest sha256 is invalid')
    else:
        try:
            actual_sha = _artifact_sha256(artifact)
        except OSError as exc:
            qa.fail(f'national {label} PMTiles cannot be checksummed: {exc}')
        else:
            if actual_sha != expected_sha:
                qa.fail(
                    f'national {label} manifest sha256 does not match artifact')
    try:
        description = _alaska_description(meta.get('description'), label)
    except ValueError as exc:
        qa.fail(str(exc))
    else:
        expected_description = {
            'schema': 'nwmm-alaska-pmtiles-v1',
            'dataset': dataset,
            'snapshot': snapshot,
            'counts': counts,
            'staging_sha256': staging_sha256,
        }
        if description != expected_description:
            qa.fail(
                f'national {label} PMTiles embedded counts/provenance do not '
                'reconcile')
    return {
        'meta': meta,
        'ids': ids_by_layer,
        'instances': instances_by_layer,
    }


def _validate_alaska_claims_split(qa, entry):
    label = 'ALASKA_STATE_CLAIMS'
    if not isinstance(entry, dict):
        qa.fail(f'national {label} baseline entry must be an object')
        return
    delivery_valid = _alaska_claim_delivery_schema_valid(entry)
    if not delivery_valid:
        qa.fail(f'national {label} baseline delivery schema is invalid')

    statuses = ('active', 'pending', 'closed')
    top_counts = entry.get('by_status')
    top_counts_valid = (
        _is_int(entry.get('n'), 1) and isinstance(top_counts, dict) and
        set(top_counts) == set(statuses) and
        all(_is_int(top_counts.get(status), 0) for status in statuses) and
        sum(top_counts.values()) == entry.get('n'))
    if not top_counts_valid:
        qa.fail(f'national {label} manifest counts do not reconcile')

    quality_fields = {
        'collapsed_point_rings', 'counterclockwise_exteriors',
        'exact_duplicate_rows', 'future_labor_dates',
        'repeated_serial_rows', 'zero_area_features', 'zero_area_rings',
    }
    source_quality = entry.get('source_quality')
    quality_valid = (
        isinstance(source_quality, dict) and
        set(source_quality) == quality_fields and
        all(_is_int(value, 0) for value in source_quality.values()))
    if not quality_valid:
        qa.fail(f'national {label} source-quality schema is invalid')
    elif any(source_quality[field] != 0 for field in (
            'collapsed_point_rings', 'zero_area_features',
            'zero_area_rings')):
        qa.fail(
            'national ALASKA_STATE_CLAIMS source geometry is incomplete; '
            'collapsed/zero-area rows require a higher-precision refetch')

    base = entry.get('base_delivery')
    base_counts = base.get('by_status') if isinstance(base, dict) else None
    base_valid = (
        isinstance(base, dict) and
        set(base) == {'n', 'by_status', 'source_id_inventory'} and
        _is_int(base.get('n'), 1) and isinstance(base_counts, dict) and
        set(base_counts) == set(statuses) and
        all(_is_int(base_counts.get(status), 0) for status in statuses) and
        sum(base_counts.values()) == base.get('n'))
    if not base_valid:
        qa.fail(
            f'national {label} base-delivery schema/counts are invalid; the '
            'single-archive compatibility schema is incomplete')

    precision = entry.get('precision_overflow')
    precision_counts = (
        precision.get('by_status') if isinstance(precision, dict) else None)
    precision_valid = _alaska_precision_descriptor_schema_valid(precision)
    if not precision_valid:
        qa.fail(
            f'national {label} precision-overflow delivery schema/counts are '
            'invalid or absent')

    # This exceptional set is checksum-bound in the builder. Pin it again at
    # the national acceptance boundary so no point/centroid substitution or
    # silent reassignment can masquerade as a lossless split.
    expected_geometry = {
        status: {
            str(source_oid): digest
            for source_oid, digest in sorted(
                ALASKA_PRECISION_SOURCE_GEOMETRY_SHA256[status].items())
        }
        for status in ('active', 'closed')
    }
    expected_objectids = {
        status: sorted(ALASKA_PRECISION_SOURCE_GEOMETRY_SHA256[status])
        for status in ('active', 'closed')
    }
    if (not isinstance(precision, dict) or
            precision.get('source_objectids') != expected_objectids or
            precision.get('source_geometry_sha256') != expected_geometry):
        qa.fail(
            f'national {label} precision source-polygon identity/geometry '
            'inventory changed')

    if not (delivery_valid and top_counts_valid and base_valid and
            precision_valid):
        return
    precision_by_status = {
        'active': precision_counts['active'],
        'pending': 0,
        'closed': precision_counts['closed'],
    }
    if any(base_counts[status] + precision_by_status[status] !=
           top_counts[status] for status in statuses):
        qa.fail(
            f'national {label} base/precision status counts do not form the '
            'exact combined source inventory')

    compact_fields = {'records', 'minimum_id', 'maximum_id', 'ids_sha256'}
    source_objectid_inventory = entry.get('source_objectid_inventory')
    object_inventory_valid = (
        isinstance(source_objectid_inventory, dict) and
        set(source_objectid_inventory) == set(statuses))
    if not object_inventory_valid:
        qa.fail(
            f'national {label} source-OBJECTID delivery inventory is missing '
            'or invalid')
    else:
        snapshot_inventory = entry.get('source_snapshot_inventory')
        for status in statuses:
            row = source_objectid_inventory.get(status)
            parts_valid = (
                isinstance(row, dict) and
                set(row) == {'source', 'base', 'precision',
                             'disjoint_union_complete'} and
                row.get('disjoint_union_complete') is True and
                all(isinstance(row.get(part), dict) and
                    set(row[part]) == compact_fields
                    for part in ('source', 'base', 'precision')))
            if not parts_valid:
                qa.fail(
                    f'national {label} {status} source-OBJECTID partition is '
                    'invalid')
                continue
            source_part, base_part, precision_part = (
                row['source'], row['base'], row['precision'])
            expected_precision_ids = expected_objectids.get(status, [])
            if (source_part.get('records') != top_counts[status] or
                    base_part.get('records') != base_counts[status] or
                    precision_part != _alaska_compact_integer_inventory(
                        expected_precision_ids)):
                qa.fail(
                    f'national {label} {status} source-OBJECTID partition '
                    'counts/digest do not reconcile')
            snapshot = (snapshot_inventory.get(status)
                        if isinstance(snapshot_inventory, dict) else None)
            if (not isinstance(snapshot, dict) or
                    snapshot.get('n') != top_counts[status] or
                    snapshot.get('minimum_object_id') !=
                    source_part.get('minimum_id') or
                    snapshot.get('maximum_object_id') !=
                    source_part.get('maximum_id') or
                    snapshot.get('object_ids_sha256') !=
                    source_part.get('ids_sha256')):
                qa.fail(
                    f'national {label} {status} source-OBJECTID inventory '
                    'does not match the reviewed source snapshot')

    claim_properties = {
        status: _ALASKA_CLAIM_PROPERTIES for status in statuses}
    claim_numeric = {
        status: ('source_oid', 'lon', 'lat') for status in statuses}
    main = _scan_alaska_archive(
        qa, label=label, entry=entry, layers=list(statuses),
        properties=claim_properties, numeric_properties=claim_numeric,
        counts=base_counts, dataset='alaska_state_claims',
        snapshot=entry['retrieved'], staging_sha256=entry['staging_sha256'],
        source_id_inventory=base['source_id_inventory'], minzoom=0,
        maxzoom=13, geometry_types={status: {3} for status in statuses})

    precision_layers = ('active_precision', 'closed_precision')
    precision_layer_counts = {
        'active_precision': precision_counts['active'],
        'closed_precision': precision_counts['closed'],
    }
    precision_properties = {
        layer: _ALASKA_CLAIM_PROPERTIES for layer in precision_layers}
    precision_numeric = {
        layer: ('source_oid', 'lon', 'lat') for layer in precision_layers}
    precision_scan = _scan_alaska_archive(
        qa, label='ALASKA_STATE_CLAIMS_PRECISION', entry=precision,
        layers=list(precision_layers), properties=precision_properties,
        numeric_properties=precision_numeric, counts=precision_layer_counts,
        dataset='alaska_state_claims_precision', snapshot=entry['retrieved'],
        staging_sha256=entry['staging_sha256'],
        source_id_inventory=precision['source_id_inventory'], minzoom=0,
        maxzoom=19, geometry_types={layer: {3} for layer in precision_layers})
    if main is None or precision_scan is None:
        return

    combined_inventory = entry.get('source_id_inventory')
    combined_schema_valid = (
        isinstance(combined_inventory, dict) and
        set(combined_inventory) == set(statuses))
    if not combined_schema_valid:
        qa.fail(f'national {label} combined source-ID inventory is invalid')
        return
    precision_layer_for = {
        'active': 'active_precision', 'pending': None,
        'closed': 'closed_precision',
    }
    for status in statuses:
        base_ids = main['ids'][status]
        precision_layer = precision_layer_for[status]
        precision_ids = (precision_scan['ids'][precision_layer]
                         if precision_layer else [])
        overlap = set(base_ids) & set(precision_ids)
        combined_ids = sorted(set(base_ids) | set(precision_ids))
        base_instances = main['instances'][status]
        precision_instances = (
            precision_scan['instances'][precision_layer]
            if precision_layer else 0)
        inventory = combined_inventory.get(status)
        if (overlap or len(combined_ids) != top_counts[status] or
                not isinstance(inventory, dict) or
                set(inventory) != _ALASKA_COMBINED_INVENTORY_FIELDS or
                inventory.get('status') != 'complete_at_retrieval' or
                inventory.get('source_records') != top_counts[status] or
                inventory.get('maxzoom_unique_tiled_ids') !=
                top_counts[status] or
                inventory.get('maxzoom_feature_instances') !=
                base_instances + precision_instances or
                inventory.get('ids_sha256') !=
                _alaska_ids_sha256(combined_ids) or
                inventory.get('base_records') != base_counts[status] or
                inventory.get('precision_records') !=
                precision_by_status[status] or
                inventory.get('disjoint_union_complete') is not True):
            qa.fail(
                f'national {label} {status} base/precision source-ID '
                'inventories are not an exact disjoint union')


def _alaska_ardf_feature_contract():
    """Return a full-scan ARDF validator and its maxzoom source-blank evidence."""
    state = {
        'signatures': {},
        'top_level_ids': {},
        'maxzoom_ids': set(),
        'blank_ids': {
            'commodities_main': set(),
            'site_type': set(),
            'district': set(),
        },
    }
    value_status_fields = (
        ('g', 'g_status', 'commodities_main'),
        ('typ', 'typ_status', 'site_type'),
        ('district', 'district_status', 'district'),
    )

    def validate(layer, feature, at_maxzoom):
        if layer != 'ardf':
            raise ValueError(f'unexpected Alaska ARDF source layer {layer!r}')
        properties = feature['properties']
        ardf_id = properties.get('id')
        if (not isinstance(ardf_id, str) or
                re.fullmatch(r'[A-Z]{2}\d{3}', ardf_id) is None):
            raise ValueError('ARDF feature id is not a canonical ARDF identifier')
        for field in ('st', 'nm', 'status'):
            value = properties.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f'ARDF feature {ardf_id} has invalid browser string {field}')
        group = properties.get('group')
        existing = properties.get('ex')
        if (not isinstance(group, int) or isinstance(group, bool) or
                not 0 <= group <= 5):
            raise ValueError(
                f'ARDF feature {ardf_id} has invalid numeric commodity group')
        if (not isinstance(existing, int) or isinstance(existing, bool) or
                existing not in (0, 1)):
            raise ValueError(
                f'ARDF feature {ardf_id} has invalid numeric existing flag')
        for value_field, status_field, source_field in value_status_fields:
            value = properties.get(value_field)
            value_status = properties.get(status_field)
            if (not isinstance(value, str) or not value.strip() or
                    value_status not in {
                        ARDF_SOURCE_VALUE_REPORTED, ARDF_SOURCE_VALUE_BLANK}):
                raise ValueError(
                    f'ARDF feature {ardf_id} has invalid {source_field} '
                    'value/status domain')
            is_sentinel = value == ARDF_SOURCE_VALUE_MISSING
            is_blank = value_status == ARDF_SOURCE_VALUE_BLANK
            if is_sentinel != is_blank:
                raise ValueError(
                    f'ARDF feature {ardf_id} {source_field} sentinel/status '
                    'semantics disagree')

        top_level_id = feature.get('id')
        if (not isinstance(top_level_id, int) or isinstance(top_level_id, bool) or
                top_level_id < 0):
            raise ValueError(
                f'ARDF feature {ardf_id} has no nonnegative top-level ID')
        signature = tuple(properties[field] for field in (
            'st', 'id', 'nm', 'g', 'g_status', 'group', 'ex', 'typ',
            'typ_status', 'status', 'district', 'district_status'))
        previous = state['signatures'].setdefault(ardf_id, signature)
        if previous != signature:
            raise ValueError(
                f'ARDF feature {ardf_id} changes browser semantics across tiles')
        previous_top_level = state['top_level_ids'].setdefault(
            ardf_id, top_level_id)
        if previous_top_level != top_level_id:
            raise ValueError(
                f'ARDF feature {ardf_id} changes top-level ID across tiles')
        if at_maxzoom:
            state['maxzoom_ids'].add(ardf_id)
            for value_field, status_field, source_field in value_status_fields:
                if properties[status_field] == ARDF_SOURCE_VALUE_BLANK:
                    state['blank_ids'][source_field].add(ardf_id)

    return validate, state


def _validate_alaska_ardf(qa, entry):
    label = 'ARDF'
    if not isinstance(entry, dict):
        qa.fail(f'national {label} baseline entry must be an object')
        return
    n = entry.get('n')
    delivery_valid = (
        entry.get('format') == 'pmtiles' and
        entry.get('file') == 'data/tiles/national/ardf.pmtiles' and
        entry.get('source_layer') == 'ardf' and
        entry.get('source') == ARDF_SOURCE and entry.get('minzoom') == 0 and
        entry.get('maxzoom') == 13 and
        entry.get('retrieved') == ALASKA_SNAPSHOT_DATE and
        entry.get('staging_sha256') == ARDF_EXPECTED_STAGING_SHA256 and
        entry.get('source_snapshot_contract') ==
        ARDF_SOURCE_SNAPSHOT_CONTRACT and
        entry.get('source_snapshot_inventory') ==
        ARDF_EXPECTED_SOURCE_SNAPSHOT_INVENTORY)
    counts_valid = (
        n == ARDF_EXPECTED_COUNT and entry.get('states') == {'AK': n})
    source_quality = entry.get('source_quality')
    quality_valid = source_quality == ARDF_EXPECTED_SOURCE_QUALITY
    if not delivery_valid:
        qa.fail(f'national {label} baseline delivery schema is invalid')
    if not counts_valid:
        qa.fail(f'national {label} manifest counts do not reconcile')
    if not quality_valid:
        qa.fail(f'national {label} source-quality schema is invalid')
    if not (delivery_valid and counts_valid):
        return
    properties = {'ardf': (
        'st', 'id', 'nm', 'g', 'g_status', 'group', 'ex', 'typ',
        'typ_status', 'status', 'district', 'district_status')}
    string_properties = {'ardf': (
        'st', 'id', 'nm', 'g', 'g_status', 'typ', 'typ_status', 'status',
        'district', 'district_status')}
    feature_validator, feature_evidence = _alaska_ardf_feature_contract()
    scan = _scan_alaska_archive(
        qa, label=label, entry=entry, layers=['ardf'], properties=properties,
        numeric_properties={'ardf': ('group', 'ex')}, counts={'ardf': n},
        dataset='ardf', snapshot=entry['retrieved'],
        staging_sha256=entry['staging_sha256'],
        source_id_inventory=entry.get('source_id_inventory'), minzoom=0,
        maxzoom=13, geometry_types={'ardf': {1}},
        string_properties=string_properties,
        feature_validator=feature_validator)
    if scan is None:
        return
    blank_counts = {
        field: len(ids) for field, ids in feature_evidence['blank_ids'].items()}
    expected_blank_counts = ARDF_EXPECTED_SOURCE_QUALITY['source_blank_fields']
    if (len(feature_evidence['maxzoom_ids']) != n or
            blank_counts != expected_blank_counts):
        qa.fail(
            'national ARDF browser source-blank inventory does not reconcile '
            f'(expected={expected_blank_counts}, tiled={blank_counts})')
    if (feature_evidence['blank_ids']['site_type'] !=
            ARDF_BLANK_SITE_TYPE_IDS):
        qa.fail(
            'national ARDF blank Site_type browser-ID inventory changed '
            f'(expected={sorted(ARDF_BLANK_SITE_TYPE_IDS)}, '
            f'tiled={sorted(feature_evidence["blank_ids"]["site_type"])})')


def validate_alaska_baselines(qa, baselines, release=False):
    """Validate the lossless Alaska base+precision claim split and ARDF.

    The old single-archive shape is deliberately not accepted: it cannot
    prove that precision-overflow source polygons reached the browser. Missing
    or partial progress entries remain ordinary QA failures, never exceptions
    or implied DONE/release evidence.
    """
    values = baselines if isinstance(baselines, dict) else {}
    claims = values.get('alaska_state_claims')
    ardf = values.get('ardf')
    if claims is None:
        if release:
            qa.fail('national ALASKA_STATE_CLAIMS PMTiles baseline is missing')
    else:
        _validate_alaska_claims_split(qa, claims)
    if ardf is None:
        if release:
            qa.fail('national ARDF PMTiles baseline is missing')
    else:
        _validate_alaska_ardf(qa, ardf)


_STATE_SURVEY_BASELINE_GROUPS = {
    'AZ': {
        'keys': frozenset((
            'az_azgs_map35_2025',
            'az_azgs_mining_districts',
            'az_azgs_critical_minerals',
        )),
        'module': 'build_arizona_state_survey_pmtiles',
    },
    'CO': {
        'keys': frozenset((
            'co_usgs_cngm_tweto_500k',
            'co_cgs_on006_faults',
            'co_cgs_on007_districts',
        )),
        'module': 'build_colorado_state_survey_pmtiles',
    },
    'NV': {
        'keys': frozenset((
            'nv_usgs_ds249',
            'nv_nbmg_onegeology_250k',
            'nv_nbmg_mining_districts',
        )),
        'module': 'build_nevada_state_survey_pmtiles',
    },
    'UT': {
        'keys': frozenset((
            'ut_ugs_map179dm_500k',
            'ut_ugs_ds7_quaternary_faults',
            'ut_ugs_ofr695_mining_districts',
            'ut_ugs_ofr757_umos',
        )),
        'module': 'build_utah_state_survey_pmtiles',
    },
}
_NEVADA_STATE_SURVEY_BASELINES = _STATE_SURVEY_BASELINE_GROUPS['NV']['keys']


def _validate_state_survey_group(qa, baselines, state):
    """Run one builder's lossless offline validator for its atomic set."""
    group = _STATE_SURVEY_BASELINE_GROUPS[state]
    present = group['keys'] & set(baselines)
    if not present:
        return False
    if present != group['keys']:
        size = {3: 'three', 4: 'four'}.get(
            len(group['keys']), str(len(group['keys'])))
        qa.fail(
            f'{state} state-survey baselines must be advertised as one atomic '
            f'{size}-archive set; present={sorted(present)}')
        return True
    try:
        module = __import__(group['module'], fromlist=['validate_manifest_baselines'])
        exact = getattr(module, 'validate_manifest_baselines')
        exact({'national_baselines': baselines}, pmtiles_header=_pmtiles_header)
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError,
            TypeError, ValueError) as exc:
        qa.fail(f'{state} state-survey baseline validation failed: {exc}')
    return True


def validate_state_survey_baselines(qa, baselines):
    """Fail closed on every advertised research-only state-survey archive.

    A ``baseline_not_release`` entry is deployable browser data even though it
    does not release its state.  It therefore cannot live outside an exact,
    builder-owned atomic group or bypass that builder's checksum, schema,
    clipping, repair, full-MVT, and maximum-zoom source-ID validation.
    """
    if not isinstance(baselines, dict):
        return
    recognized = set().union(*(
        group['keys'] for group in _STATE_SURVEY_BASELINE_GROUPS.values()))
    advertised = {
        key for key, entry in baselines.items()
        if isinstance(entry, dict) and entry.get('status') == 'baseline_not_release'
    }
    unknown = advertised - recognized
    if unknown:
        qa.fail('unrecognized baseline_not_release state-survey entries have no '
                f'exact offline validator: {sorted(unknown)}')
    for state in sorted(_STATE_SURVEY_BASELINE_GROUPS):
        _validate_state_survey_group(qa, baselines, state)


def validate_nevada_state_survey_baselines(qa, baselines):
    """Fully validate advertised Nevada research baselines in every profile.

    The three archives publish atomically and remain explicitly decoupled from
    release/DONE status. Once any is advertised, progress CI nevertheless owns
    their exact manifest, checksum, source-inventory, repair/clip, and full MVT
    semantic contracts.
    """
    # Compatibility wrapper retained for focused Nevada builder tests.  The
    # national progress path below uses ``validate_state_survey_baselines``.
    if isinstance(baselines, dict):
        _validate_state_survey_group(qa, baselines, 'NV')


def validate_registry_baseline_pointers(qa, baselines):
    """Keep reviewable registry pointers pinned to advertised artifacts.

    Baseline builders atomically update the public manifest. A stale registry
    hash would otherwise leave two contradictory, individually plausible
    provenance records while the progress profile remained green.
    """
    try:
        states = load_states()
        alaska = states['AK']
    except (KeyError, RegistryError, TypeError, ValueError) as exc:
        qa.fail(f'state registry baseline pointers cannot be loaded: {exc}')
        return
    systems = alaska.get('claim_systems')
    dnr = next((item for item in systems or []
                if isinstance(item, dict) and
                item.get('id') == 'alaska_state_claims'), None)
    pointers = {
        'ardf': alaska.get('occurrence_backbone'),
        'alaska_state_claims': dnr,
    }
    for baseline_id, pointer in pointers.items():
        entry = baselines.get(baseline_id) if isinstance(baselines, dict) else None
        if not isinstance(entry, dict):
            # Presence/absence is already governed by the baseline validator.
            continue
        if not isinstance(pointer, dict):
            qa.fail(f'AK registry has no {baseline_id} baseline pointer')
            continue
        expected = {
            'baseline_manifest_key': f'national_baselines.{baseline_id}',
            'baseline_browser_path': entry.get('file'),
            'baseline_features': entry.get('n'),
            'baseline_sha256': entry.get('sha256'),
        }
        for field, value in expected.items():
            if pointer.get(field) != value:
                qa.fail(f'AK registry {baseline_id}.{field}={pointer.get(field)!r}; '
                        f'manifest advertises {value!r}')

    nevada = states['NV']
    geology_pointers = (nevada.get('geology') or {}).get('baseline_artifacts')
    report47 = next((item for item in nevada.get('historic_serials') or []
                     if isinstance(item, dict) and
                     item.get('source_id') == 'nbmg_report_47'), None)
    nv_pointers = {
        'nv_usgs_ds249': (
            geology_pointers.get('nv_usgs_ds249')
            if isinstance(geology_pointers, dict) else None),
        'nv_nbmg_onegeology_250k': (
            geology_pointers.get('nv_nbmg_onegeology_250k')
            if isinstance(geology_pointers, dict) else None),
        'nv_nbmg_mining_districts': report47,
    }
    for baseline_id, pointer in nv_pointers.items():
        entry = baselines.get(baseline_id) if isinstance(baselines, dict) else None
        if not isinstance(entry, dict):
            continue
        if not isinstance(pointer, dict):
            qa.fail(f'NV registry has no {baseline_id} baseline pointer')
            continue
        expected = {
            'baseline_manifest_key': f'national_baselines.{baseline_id}',
            'baseline_browser_path': entry.get('file'),
            'baseline_features': entry.get('n'),
            'baseline_sha256': entry.get('sha256'),
        }
        for field, value in expected.items():
            if pointer.get(field) != value:
                qa.fail(f'NV registry {baseline_id}.{field}={pointer.get(field)!r}; '
                        f'manifest advertises {value!r}')

    # New state adapters use one reviewable survey-level pointer table.  NV's
    # older split geology/serial layout above remains supported during the UI
    # migration, while AZ and future adapters can be checked generically.
    for code, state in states.items():
        survey = state.get('geological_survey') or {}
        generic = survey.get('baseline_artifacts')
        group = _STATE_SURVEY_BASELINE_GROUPS.get(code)
        allowed = group['keys'] if group else frozenset()
        present = (allowed & set(baselines)
                   if isinstance(baselines, dict) else set())
        if generic is None:
            if code != 'NV' and present:
                qa.fail(
                    f'{code} registry has no geological_survey.'
                    'baseline_artifacts table for its advertised atomic '
                    'state-survey set')
            continue
        if not isinstance(generic, dict):
            qa.fail(f'{code} registry geological_survey.baseline_artifacts '
                    'must be an object')
            continue
        if set(generic) - allowed:
            qa.fail(f'{code} registry has unrecognized state-survey baseline '
                    f'pointers: {sorted(set(generic) - allowed)}')
        if present and set(generic) != allowed:
            qa.fail(f'{code} registry state-survey pointers do not cover the '
                    f'advertised atomic set: {sorted(set(generic))}')
        for baseline_id, pointer in generic.items():
            entry = (baselines.get(baseline_id)
                     if isinstance(baselines, dict) else None)
            if not isinstance(entry, dict):
                qa.fail(f'{code} registry advertises absent baseline {baseline_id}')
                continue
            if not isinstance(pointer, dict):
                qa.fail(f'{code} registry {baseline_id} pointer must be an object')
                continue
            expected = {
                'baseline_manifest_key': f'national_baselines.{baseline_id}',
                'baseline_browser_path': entry.get('file'),
                'baseline_features': entry.get('n'),
                'baseline_sha256': entry.get('sha256'),
            }
            for field, value in expected.items():
                if pointer.get(field) != value:
                    qa.fail(
                        f'{code} registry {baseline_id}.{field}='
                        f'{pointer.get(field)!r}; manifest advertises {value!r}')


_GEOLOGY_FAULT_PROVENANCE_PROPERTIES = [
    'source_dataset', 'source_id', 'source_scale', 'source_scale_status',
    'source_ref', 'source_url', 'source_catalog_id_status', 'source_record_id',
    'source_geometry_sha256', 'source_geometry_length_m',
    'geometry_normalization', 'geometry_normalization_engine',
    'geometry_normalization_reason', 'geometry_normalization_delta_m',
    'geometry_normalization_parts',
]
_GEOLOGY_FAULT_SOURCE_SPECS = {
    'sgmc': {
        'title': 'State Geologic Map Compilation geodatabase v1.1',
        'authority': 'U.S. Geological Survey',
        'url': ('https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/'
                'SB_5888bf4fe4b05ccb964bab9d_USGS_SGMC_feature/FeatureServer'),
        'item': ('https://www.arcgis.com/home/item.html?'
                 'id=3890cfedb3204aa8828765a2ccfaeb38'),
        'doi': 'https://doi.org/10.5066/F7WH2N65',
        'release': '2017 v1.1',
        'scope': '48 conterminous states',
    },
    'alaska': {
        'title': 'Geologic map of Alaska, SIM 3340',
        'authority': 'U.S. Geological Survey',
        'url': ('https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/'
                'Geologic_map_of_Alaska_FeatureLayer/FeatureServer'),
        'item': ('https://www.arcgis.com/home/item.html?'
                 'id=b80ab6a076ef44b184caf8026bed8e4f'),
        'doi': 'https://doi.org/10.3133/sim3340',
        'release': '2015',
        'scope': 'Alaska',
    },
    'macrostrat_gap_fill': {
        'used': False,
        'reason': 'No state coverage gap remains after SGMC plus SIM 3340.',
    },
}


def _validate_national_point_id_inventory(qa, baseline_id, entry, meta,
                                          source_layer):
    """Bind a national point baseline to every stable max-zoom source ID.

    Buffered points can appear in adjacent MVT tiles, so raw feature
    instances are not source counts. Completeness is the unique top-level ID
    set; its canonical sorted-list hash is recorded in the public manifest.
    """
    inventory = entry.get('source_id_inventory')
    ids = (meta.get('maxzoom_feature_ids') or {}).get(source_layer)
    instances = (meta.get('maxzoom_feature_instances') or {}).get(source_layer)
    if (not isinstance(inventory, dict) or
            set(inventory) != {
                'status', 'source_records', 'maxzoom_feature_instances',
                'maxzoom_unique_tiled_ids', 'ids_sha256'}):
        qa.fail(f'national {baseline_id.upper()} source-ID inventory is missing or invalid')
        return
    if (not isinstance(ids, list) or
            any(not _is_int(value, 0) for value in ids) or
            ids != sorted(set(ids))):
        qa.fail(f'national {baseline_id.upper()} maxzoom IDs are invalid or duplicated')
        return
    digest = hashlib.sha256(json.dumps(
        ids, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    expected = entry.get('n')
    if (inventory.get('status') != 'complete_at_retrieval' or
            not _is_int(expected, 1) or
            inventory.get('source_records') != expected or
            inventory.get('maxzoom_unique_tiled_ids') != len(ids) or
            len(ids) != expected or
            inventory.get('maxzoom_feature_instances') != instances or
            not _is_int(instances, expected) or
            inventory.get('ids_sha256') != digest):
        qa.fail(
            f'national {baseline_id.upper()} source records do not reconcile '
            'to unique maxzoom PMTiles feature IDs')


def _validate_national_geology_fault_entry(
        qa, baseline_id, entry, inventory, *, maxzoom_inventory=None,
        all_zoom_instances=None, maxzoom_instances=None, archive_maxzoom=None):
    """Reconcile reviewed manifest provenance with a full PMTiles inventory."""
    label = baseline_id.upper()
    expected_sources = dict(_GEOLOGY_FAULT_SOURCE_SPECS)
    expected_datasets = set(_NATIONAL_PROVENANCE_DATASETS[baseline_id])
    expected_statuses = ({
        'explicit', 'source_reference_omits_scale',
        'reserved_001_no_reference_row',
    } if baseline_id == 'geology' else {
        'explicit', 'source_reference_omits_scale',
        'reserved_001_no_reference_row', 'source_marks_unspecified',
    })
    expected_summary = (
        'U.S. Geological Survey SGMC v1.1 and SIM 3340'
        if baseline_id == 'geology' else
        'U.S. Geological Survey SGMC v1.1, SIM 3340, and Qfaults')
    if baseline_id == 'faults':
        expected_sources['qfaults'] = None

    sources = entry.get('sources')
    if (entry.get('source') != expected_summary or
            not isinstance(sources, dict) or set(sources) != set(expected_sources)):
        qa.fail(f'national {label} source inventory is invalid')
    else:
        for source_id, expected in _GEOLOGY_FAULT_SOURCE_SPECS.items():
            if sources.get(source_id) != expected:
                qa.fail(f'national {label} {source_id} provenance is invalid')
        if baseline_id == 'faults':
            qfaults = sources.get('qfaults')
            download = qfaults.get('download') if isinstance(qfaults, dict) else None
            if (not isinstance(qfaults, dict) or set(qfaults) != {
                    'title', 'authority', 'url', 'doi', 'release', 'download'} or
                    qfaults.get('title') !=
                    'Quaternary Fault and Fold Database for the Nation' or
                    qfaults.get('authority') != 'U.S. Geological Survey' or
                    qfaults.get('url') !=
                    'https://earthquake.usgs.gov/static/lfs/nshm/qfaults/Qfaults_GIS.zip' or
                    qfaults.get('doi') != 'https://doi.org/10.5066/P9BCVRCK' or
                    not _text(qfaults.get('release')) or
                    not isinstance(download, dict) or set(download) != {
                        'bytes', 'sha256', 'etag', 'last_modified'} or
                    not _is_int(download.get('bytes'), 1) or
                    not isinstance(download.get('sha256'), str) or
                    re.fullmatch(r'[0-9a-f]{64}', download['sha256']) is None or
                    any(value is not None and not _text(value)
                        for value in (download.get('etag'),
                                      download.get('last_modified')))):
                qa.fail('national FAULTS Qfaults download provenance is invalid')

    n = entry.get('n')
    states = entry.get('states')
    by_source = entry.get('by_source')
    scale_status = entry.get('source_scale_status')
    coverage = entry.get('coverage')
    zero_states = (sorted(state for state, count in states.items() if count == 0)
                   if isinstance(states, dict) else None)
    state_minimum = 1 if baseline_id == 'geology' else 0
    counters_valid = (
        _is_int(n, 1) and isinstance(states, dict) and
        set(states) == set(ALL_STATES) and
        all(_is_int(value, state_minimum) for value in states.values()) and
        sum(states.values()) == n and
        isinstance(by_source, dict) and set(by_source) == expected_datasets and
        all(_is_int(value, 1) for value in by_source.values()) and
        sum(by_source.values()) == n and
        isinstance(scale_status, dict) and set(scale_status) == expected_statuses and
        all(_is_int(value, 1) for value in scale_status.values()) and
        sum(scale_status.values()) == n)
    if not counters_valid:
        qa.fail(f'national {label} state/source/scale counts do not reconcile')
    if (not isinstance(coverage, dict) or set(coverage) != {
            'states', 'excluded_state_codes', 'zero_feature_states'} or
            coverage.get('states') != 49 or
            coverage.get('excluded_state_codes') != ['DC', 'HI', 'PR'] or
            coverage.get('zero_feature_states') != zero_states or
            (baseline_id == 'geology' and zero_states != [])):
        qa.fail(f'national {label} 49-state coverage/zero inventory is invalid')
    if entry.get('provenance_properties') != _GEOLOGY_FAULT_PROVENANCE_PROPERTIES:
        qa.fail(f'national {label} provenance-property schema is invalid')
    normalization = entry.get('geometry_normalization')
    normalized_features = (normalization.get('features')
                           if isinstance(normalization, dict) else None)
    expected_normalized = ((inventory or {}).get('geometry_normalizations', [])
                           if isinstance(inventory, dict) else [])
    normalized_rows_valid = (
        isinstance(normalized_features, list) and
        all(isinstance(item, dict) and set(item) == {
            'fid', 'st', 'source_dataset', 'source_id', 'source_record_id',
            *_GEOMETRY_NORMALIZATION_FIELDS} and
            _is_int(item.get('fid'), 1) and item.get('st') in ALL_STATES and
            item.get('source_dataset') in expected_datasets and
            _text(item.get('source_id')) and
            item.get('geometry_normalization') == _GEOMETRY_NORMALIZATION and
            item.get('geometry_normalization_engine') ==
            _GEOMETRY_NORMALIZATION_ENGINE and
            item.get('geometry_normalization_reason') ==
            _GEOMETRY_NORMALIZATION_REASON and
            _is_finite(item.get('geometry_normalization_delta_m'), 0) and
            0 < item['geometry_normalization_delta_m'] <= 0.02 and
            _is_int(item.get('geometry_normalization_parts'), 1) and
            _is_finite(item.get('source_geometry_length_m'), 0) and
            item['source_geometry_length_m'] > 0 and
            re.fullmatch(r'[0-9a-f]{64}',
                         item.get('source_geometry_sha256') or '') is not None and
            isinstance(item.get('source_record_id'), str) and
            re.fullmatch(r'\d+', item['source_record_id']) is not None
            for item in normalized_features) and
        [item['fid'] for item in normalized_features] ==
        sorted({item['fid'] for item in normalized_features}))
    expected_normalization_sha = hashlib.sha256(json.dumps(
        normalized_features or [], sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()
    if (not isinstance(normalization, dict) or set(normalization) != {
            'engine', 'reason', 'count', 'inventory_sha256', 'features'} or
            normalization.get('engine') != _GEOMETRY_NORMALIZATION_ENGINE or
            normalization.get('reason') != _GEOMETRY_NORMALIZATION_REASON or
            not normalized_rows_valid or
            normalization.get('count') != len(normalized_features or []) or
            normalization.get('inventory_sha256') != expected_normalization_sha or
            normalized_features != expected_normalized):
        qa.fail(f'national {label} geometry-normalization inventory is invalid')
    reconciliation = entry.get('tile_fid_reconciliation')
    if (not isinstance(reconciliation, dict) or set(reconciliation) != {
            'source_records', 'unique_tiled_fids', 'maxzoom',
            'maxzoom_unique_tiled_fids', 'missing_fid_count',
            'extra_fid_count', 'deterministic_builds',
            'diagnostic_missing_fids',
            'diagnostic_missing_fids_sha256'} or
            reconciliation.get('source_records') != n or
            reconciliation.get('unique_tiled_fids') != n or
            reconciliation.get('maxzoom') != 12 or
            reconciliation.get('maxzoom_unique_tiled_fids') != n or
            reconciliation.get('missing_fid_count') != 0 or
            reconciliation.get('extra_fid_count') != 0 or
            reconciliation.get('deterministic_builds') != 2 or
            not isinstance(reconciliation.get('diagnostic_missing_fids'), list) or
            reconciliation['diagnostic_missing_fids'] != sorted(set(
                reconciliation['diagnostic_missing_fids'])) or
            any(not _is_int(fid, 1) or fid > n
                for fid in reconciliation['diagnostic_missing_fids']) or
            reconciliation.get('diagnostic_missing_fids_sha256') !=
            hashlib.sha256(json.dumps(
                reconciliation['diagnostic_missing_fids'],
                separators=(',', ':')).encode('ascii')).hexdigest() or
            [item['fid'] for item in (normalized_features or [])] !=
            reconciliation['diagnostic_missing_fids']):
        qa.fail(f'national {label} tile-fid reconciliation evidence is invalid')
    inventory_valid = (
        isinstance(inventory, dict) and
        inventory.get('n') == n and
        inventory.get('unique_tiled_fids', inventory.get('n')) == n and
        inventory.get('source_records', n) == n and
        inventory.get('missing_fid_count', 0) == 0 and
        inventory.get('extra_fid_count', 0) == 0 and
        inventory.get('states') == states and
        inventory.get('by_source') == by_source and
        inventory.get('source_scale_status') == scale_status)
    maxzoom_valid = True
    if maxzoom_inventory is not None:
        maxzoom_valid = (
            isinstance(maxzoom_inventory, dict) and
            maxzoom_inventory.get('unique_tiled_fids') == n and
            maxzoom_inventory.get('source_records') == n and
            maxzoom_inventory.get('missing_fid_count') == 0 and
            maxzoom_inventory.get('extra_fid_count') == 0 and
            archive_maxzoom == 12 and
            _is_int(maxzoom_instances, n) and
            _is_int(all_zoom_instances, maxzoom_instances))
    if not inventory_valid or not maxzoom_valid:
        details = {
            'source_records': n,
            'unique_tiled_fids': (inventory or {}).get('unique_tiled_fids'),
            'z12_instances': maxzoom_instances,
            'all_zoom_instances': all_zoom_instances,
            'missing_fids': (inventory or {}).get('missing_fids'),
            'missing_fid_count': (inventory or {}).get('missing_fid_count'),
            'extra_fids': (inventory or {}).get('extra_fids'),
            'maxzoom_unique_tiled_fids': (
                maxzoom_inventory or {}).get('unique_tiled_fids'),
            'maxzoom_missing_fids': (maxzoom_inventory or {}).get('missing_fids'),
        }
        qa.fail(f'national {label} source-record/tiled-fid inventory does not '
                f'reconcile: {json.dumps(details, separators=(",", ":"))}')


_PUBLIC_BUILD_INPUT_EXTENSIONS = frozenset(
    ('.geojsonseq', '.shp', '.dbf', '.shx', '.zip'))
_TEMP_TILE_DIRECTORY_NAMES = frozenset(
    ('tmp', 'temp', 'temporary', 'stage', 'staging', 'build', 'build-inputs'))


def _expected_public_pmtiles(manifest):
    """Return the exact PMTiles paths currently advertised for deployment."""
    expected = set()
    ws56 = manifest.get('ws56') if isinstance(manifest, dict) else None
    geophys = ws56.get('geophys_surveys') if isinstance(ws56, dict) else None
    if isinstance(geophys, dict) and geophys.get('format') == 'pmtiles':
        if isinstance(geophys.get('file'), str):
            expected.add(os.path.normpath(geophys['file']))
    baselines = (manifest.get('national_baselines')
                 if isinstance(manifest, dict) else None)
    if isinstance(baselines, dict):
        for baseline_id, entry in baselines.items():
            if baseline_id == 'admin':
                # The physical archive is declared only by the independently
                # pinned exact descriptor. A caller-controlled path or a
                # self-consistent forged descriptor cannot grant an exemption.
                if _admin_descriptor_schema_valid(entry):
                    expected.add('data/tiles/context/admin.pmtiles')
                continue
            if (isinstance(entry, dict) and entry.get('format') == 'pmtiles' and
                    isinstance(entry.get('file'), str)):
                expected.add(os.path.normpath(entry['file']))
            # Alaska's lossless delivery is one logical baseline split across
            # an ordinary polygon archive and a tiny high-precision overflow
            # archive.  This is the sole accepted nested PMTiles descriptor;
            # ``validate_alaska_baselines`` independently validates its exact
            # path, schema, checksum, source IDs, geometries, and disjoint
            # union before the overall profile can pass.  Do not recurse over
            # arbitrary manifest objects, because that would let a stray
            # nested path evade the undeclared-artifact guard.
            precision = (entry.get('precision_overflow')
                         if baseline_id == 'alaska_state_claims' and
                         isinstance(entry, dict) else None)
            if (_alaska_claim_delivery_schema_valid(entry) and
                    _alaska_precision_descriptor_schema_valid(precision)):
                # Add the acceptance-pinned path, never a caller-controlled
                # nested value. A malformed descriptor therefore leaves the
                # physical archive visible to the undeclared-artifact guard.
                expected.add('data/tiles/claims/ak-state-precision.pmtiles')
    tiled_layers = manifest.get('tiled_layers') if isinstance(manifest, dict) else None
    if isinstance(tiled_layers, list):
        for descriptor in tiled_layers:
            if (isinstance(descriptor, dict) and
                    descriptor.get('delivery') == 'pmtiles' and
                    isinstance(descriptor.get('url'), str)):
                expected.add(os.path.normpath(descriptor['url']))
    return {path for path in expected
            if path == 'data/tiles' or path.startswith('data/tiles' + os.sep)}


def validate_public_tile_tree(qa, manifest):
    """Reject deployable staging/build inputs and undeclared tile artifacts.

    ``aws s3 sync site/data`` does not make dot-directories private. Keeping a
    shapefile or GeoJSON sequence beneath ``site/`` is therefore a publication
    regression even when the browser never names it.
    """
    data_root = os.path.join(SITE, 'data')
    tiles_root = os.path.join(data_root, 'tiles')
    expected_pmtiles = _expected_public_pmtiles(manifest)
    if os.path.isdir(tiles_root):
        for walk_root, directories, names in os.walk(tiles_root, followlinks=False):
            for directory in list(directories):
                path = os.path.join(walk_root, directory)
                lower = directory.lower()
                hidden = directory.startswith('.')
                temporary = (
                    lower in _TEMP_TILE_DIRECTORY_NAMES or
                    lower.startswith(('tmp-', 'temp-', 'staging-', '.tmp-', '.temp-')) or
                    lower.endswith(('-tmp', '-temp', '-staging')))
                if hidden or temporary or os.path.islink(path):
                    rel = os.path.relpath(path, SITE)
                    qa.fail(f'public tile tree contains hidden/temp directory: {rel}')
                    # Do not traverse a directory whose contents are already
                    # forbidden (and a symlink must never escape this scan).
                    directories.remove(directory)
            for name in names:
                path = os.path.join(walk_root, name)
                rel = os.path.normpath(os.path.relpath(path, SITE))
                components = rel.split(os.sep)
                if (any(component.startswith('.') for component in components) or
                        os.path.islink(path)):
                    qa.fail(f'public tile tree contains hidden/symlink artifact: {rel}')
                    continue
                if not name.lower().endswith('.pmtiles'):
                    qa.fail(f'public tile tree contains non-PMTiles build input: {rel}')
                    continue
                if rel not in expected_pmtiles:
                    qa.fail(f'public tile tree contains undeclared PMTiles artifact: {rel}')
    if os.path.isdir(data_root):
        for walk_root, directories, names in os.walk(data_root, followlinks=False):
            # A symlinked directory is rejected by the tile-tree pass when it
            # is under tiles; never follow any public-data symlink here.
            directories[:] = [name for name in directories
                              if not os.path.islink(os.path.join(walk_root, name))]
            for name in names:
                if os.path.splitext(name)[1].lower() in _PUBLIC_BUILD_INPUT_EXTENSIONS:
                    rel = os.path.relpath(os.path.join(walk_root, name), SITE)
                    qa.fail(f'public site data contains staging/build input: {rel}')


def validate_manifest_orphans(qa, release=False):
    path = os.path.join(SITE, 'data', 'manifest.json')
    try:
        man = _load_json(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        qa.fail(f'manifest is not strict JSON: {exc}')
        return
    if not isinstance(man, dict):
        qa.fail('manifest top level must be an object')
        return
    try:
        clip_indexes = _clip_indexes()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        qa.fail(f'authoritative state clips are invalid: {exc}')
        return
    if man.get('region') != sorted(ALL_STATES):
        qa.fail('manifest region must be the exact sorted 49-state WS11 scope')
    expected = expected_tiled_layers()
    if man.get('tiled_layers') != expected:
        qa.fail('manifest tiled_layers is stale or differs from release-enabled registry')
    validate_public_tile_tree(qa, man)
    public_sections = {}
    for section in ('sites', 'claims'):
        value = man.get(section)
        if not isinstance(value, dict):
            qa.fail(f'manifest {section} must be an object')
            value = {}
        elif value:
            qa.fail(f'public manifest {section} must be empty; legacy JSON is a private build input')
        public_sections[section] = value
    try:
        private_manifest = load_build_input_manifest(
            BUILD_INPUT_MANIFEST, root=BUILD_INPUTS, require_files=True)
        sections = {section: private_manifest[section]
                    for section in ('sites', 'claims')}
    except (OSError, ValueError) as exc:
        qa.fail(f'private build-input manifest is invalid: {exc}')
        private_manifest = {'totals': {}}
        sections = {'sites': {}, 'claims': {}}
    if isinstance(private_manifest, dict):
        boundaries = private_manifest.get('boundaries')
        if not isinstance(boundaries, dict) or set(boundaries) != {'states', 'counties'}:
            qa.fail('private build-input boundaries must contain states and counties')
        else:
            for key, expected_n in (('states', 7), ('counties', 302)):
                entry = boundaries[key]
                try:
                    boundary_path = build_input_path(
                        'boundaries', key, entry, root=BUILD_INPUTS)
                    boundary = _load_json(boundary_path)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    qa.fail(f'private boundary {key} is invalid: {exc}')
                    continue
                features = boundary.get('features') if isinstance(boundary, dict) else None
                if (not isinstance(features, list) or len(features) != expected_n or
                        boundary.get('n') != expected_n or entry.get('n') != expected_n):
                    qa.fail(f'private boundary {key} must contain {expected_n} features')
                    continue
                fips = []
                for feature in features:
                    props = feature.get('properties') if isinstance(feature, dict) else None
                    geometry = feature.get('geometry') if isinstance(feature, dict) else None
                    value = props.get('fips') if isinstance(props, dict) else None
                    width = 2 if key == 'states' else 5
                    if (not isinstance(value, str) or
                            re.fullmatch(rf'\d{{{width}}}', value) is None or
                            not isinstance(geometry, dict) or
                            geometry.get('type') not in ('Polygon', 'MultiPolygon')):
                        qa.fail(f'private boundary {key} has invalid FIPS/polygon schema')
                        break
                    fips.append(value)
                if len(fips) == len(features) and len(set(fips)) != len(fips):
                    qa.fail(f'private boundary {key} has duplicate FIPS identities')
    computed_totals = {'sites': 0, 'claims_active': 0, 'claims_closed': 0}
    for section in ('sites', 'claims'):
        for key, entry in sections[section].items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                qa.fail(f'manifest {section} entries need textual keys and object values')
                continue
            match = (re.fullmatch(r'(mrds|stategeo|usmin)_([a-z]{2})', key)
                     if section == 'sites' else
                     re.fullmatch(r'([a-z]{2})_(active|closed)', key))
            if not match:
                qa.fail(f'manifest {section}.{key}: key does not match its artifact schema')
                continue
            expected_state = (match.group(2).upper() if section == 'sites'
                              else match.group(1).upper())
            if expected_state not in ALL_STATES:
                qa.fail(f'manifest {section}.{key}: state is outside WS11')
            if section == 'claims' and expected_state not in CLAIM_STATES:
                qa.fail(f'manifest claims.{key}: {expected_state} is not a claim state')
            entry_n = entry.get('n')
            if not _is_int(entry_n, 0):
                qa.fail(f'manifest {section}.{key}: n must be a nonnegative integer')
            rel = entry.get('file')
            expected_rel = f'data/{section}/{key}.json'
            if not isinstance(rel, str) or os.path.normpath(rel) != expected_rel:
                qa.fail(f'manifest {section}.{key}: file must be {expected_rel}')
                continue
            try:
                artifact = build_input_path(
                    section, key, entry, root=BUILD_INPUTS)
            except ValueError as exc:
                qa.fail(f'build-input manifest {section}.{key}: {exc}')
                continue
            if not os.path.isfile(artifact):
                qa.fail(f'build-input manifest {section}.{key}: declared file is missing ({rel})')
                continue
            try:
                data = _load_json(artifact)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                qa.fail(f'manifest {section}.{key}: unreadable JSON ({exc})')
                continue
            if not isinstance(data, dict):
                qa.fail(f'manifest {section}.{key}: artifact must be an object')
                continue
            n = data.get('n')
            if not _is_int(n, 0) or n != entry_n:
                qa.fail(f'manifest {section}.{key}: n={entry_n} but artifact n={n}')
                continue
            if data.get('state') != expected_state:
                qa.fail(f'manifest {section}.{key}: artifact state must be {expected_state}')
            computed_totals['sites' if section == 'sites'
                            else f'claims_{match.group(2)}'] += n
            # Row-aligned columns are identified by the schema's coordinate
            # columns. Dictionary arrays such as USMIN `types`/`scales` are
            # intentionally shorter and referenced by integer row columns.
            for field in ('x', 'y'):
                value = data.get(field)
                if not isinstance(value, list) or len(value) != n:
                    qa.fail(f'manifest {section}.{key}: {field} length differs from n={n}')
            x_values, y_values = data.get('x'), data.get('y')
            if isinstance(x_values, list) and isinstance(y_values, list):
                for index, (x, y) in enumerate(zip(x_values, y_values)):
                    if not _finite_coordinate(x, -180, 180) or not _finite_coordinate(y, -90, 90):
                        qa.fail(f'manifest {section}.{key}: row {index} has invalid coordinates')
                        break
            if section == 'sites':
                source_id = match.group(1)
                if data.get('src') != source_id:
                    qa.fail(f'manifest sites.{key}: src identity must be {source_id}')
                required_strings = (('id', 'nm', 'st') if source_id == 'mrds'
                                    else ('id', 'nm') if source_id == 'stategeo'
                                    else ('q',))
                for field in required_strings:
                    value = data.get(field)
                    if (not isinstance(value, list) or len(value) != n or
                            any(not isinstance(item, str) or not item.strip()
                                for item in value)):
                        qa.fail(f'manifest sites.{key}: {field} must contain n nonempty strings')
                if source_id == 'usmin':
                    types, type_indexes = data.get('types'), data.get('t')
                    if (not isinstance(types, list) or not types or
                            any(not isinstance(item, str) or not item.strip() for item in types) or
                            not isinstance(type_indexes, list) or len(type_indexes) != n or
                            any(not _is_int(item, 0) or item >= len(types)
                                for item in type_indexes)):
                        qa.fail(f'manifest sites.{key}: USMIN type dictionary/index is invalid')
            if section == 'claims':
                required = ('serial', 'name', 'type')
                for field in required:
                    value = data.get(field)
                    if not isinstance(value, list) or len(value) != n:
                        qa.fail(f'manifest claims.{key}: {field} length differs from n={n}')
                if isinstance(data.get('serial'), list) and any(
                        not isinstance(item, str) or not item.strip() for item in data['serial']):
                    qa.fail(f'manifest claims.{key}: serial values must be nonempty strings')
                if isinstance(data.get('type'), list) and any(
                        not isinstance(item, str) or not item.strip() for item in data['type']):
                    qa.fail(f'manifest claims.{key}: type values must be nonempty strings')
                if isinstance(data.get('name'), list) and any(
                        item is not None and not isinstance(item, str) for item in data['name']):
                    qa.fail(f'manifest claims.{key}: name values must be strings or null')
                mode = key.rsplit('_', 1)[-1]
                if data.get('layer') != mode:
                    qa.fail(f'manifest claims.{key}: layer identity must be {mode}')
                if expected_state in clip_indexes and isinstance(x_values, list) and isinstance(y_values, list):
                    for index, (x, y) in enumerate(zip(x_values, y_values)):
                        if (_finite_coordinate(x, -180, 180) and
                                _finite_coordinate(y, -90, 90) and
                                not clip_indexes[expected_state].contains(x, y)):
                            qa.fail(f'manifest claims.{key}: row {index} lies outside '
                                    f'the authoritative {expected_state} boundary')
                            break
                if release and data.get('partial_after_spatial_clip'):
                    qa.fail(f'manifest claims.{key}: partial clipped snapshot needs a fresh pull')
    private_totals = private_manifest.get('totals')
    if not isinstance(private_totals, dict):
        qa.fail('private build-input totals must be an object')
    else:
        for field, computed in computed_totals.items():
            if private_totals.get(field) != computed:
                qa.fail(f'private build-input totals.{field}='
                        f'{private_totals.get(field)!r}, computed {computed}')
    totals = man.get('totals')
    if not isinstance(totals, dict):
        qa.fail('manifest totals must be an object')
    else:
        for field, computed in expected_baseline_totals(man).items():
            if totals.get(field) != computed:
                qa.fail(f'manifest totals.{field}={totals.get(field)!r}, '
                        f'tiled baselines contain {computed}')
    for section in ('sites', 'claims'):
        folder = os.path.join(SITE, 'data', section)
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            names = []
        except OSError as exc:
            qa.fail(f'public {section} folder cannot be read: {exc}')
            names = []
        for name in names:
            if name.endswith('.json'):
                qa.fail(f'public legacy build input exists: data/{section}/{name}')
    geophys = os.path.join(SITE, 'data', 'tiles', 'geophys', 'surveys.pmtiles')
    geophys_entry = ((man.get('ws56') or {}).get('geophys_surveys')
                     if isinstance(man.get('ws56'), dict) else None)
    if not os.path.isfile(geophys):
        qa.fail('national aeromagnetic survey-index PMTiles artifact is missing')
    elif not isinstance(geophys_entry, dict):
        qa.fail('national aeromagnetic survey-index manifest entry is missing')
    else:
        try:
            geophys_meta = _pmtiles_header(
                geophys, ['surveys'], {'surveys': ['src', 'nm']},
                verify_feature_properties=True, collect_feature_ids=True)
        except ValueError as exc:
            qa.fail(f'national aeromagnetic survey-index PMTiles: {exc}')
        else:
            by_source = geophys_entry.get('by_source')
            n = geophys_entry.get('n')
            if (geophys_entry.get('file') !=
                    'data/tiles/geophys/surveys.pmtiles' or
                    geophys_entry.get('format') != 'pmtiles' or
                    geophys_entry.get('source_layer') != 'surveys' or
                    geophys_entry.get('required_properties') != ['src', 'nm'] or
                    geophys_entry.get('sources') != GEOPHYS_SOURCES or
                    not isinstance(geophys_entry.get('retrieved'), str) or
                    re.fullmatch(r'\d{4}-\d{2}-\d{2}',
                                 geophys_entry.get('retrieved', '')) is None or
                    not _is_int(n, 1) or not isinstance(by_source, dict) or
                    set(by_source) != {'airborne', 'earthmri'} or
                    any(not _is_int(value, 1) for value in by_source.values()) or
                    sum(by_source.values()) != n):
                qa.fail('national aeromagnetic survey-index manifest schema/counts '
                        'are invalid')
            tiled_ids = geophys_meta.get('maxzoom_feature_ids', {}).get('surveys', [])
            source_ids = geophys_entry.get('source_id_inventory')
            tiled_id_sha = hashlib.sha256(json.dumps(
                tiled_ids, separators=(',', ':'), allow_nan=False
            ).encode('utf-8')).hexdigest()
            if (not isinstance(source_ids, dict) or set(source_ids) != {
                    'status', 'source_records', 'maxzoom_unique_tiled_ids',
                    'ids_sha256'} or
                    source_ids.get('status') != 'complete_at_retrieval' or
                    source_ids.get('source_records') != n or
                    source_ids.get('maxzoom_unique_tiled_ids') != len(tiled_ids) or
                    len(tiled_ids) != n or source_ids.get('ids_sha256') != tiled_id_sha):
                qa.fail('national aeromagnetic survey-index source IDs do not '
                        'reconcile to unique maxzoom PMTiles feature IDs')
            if geophys_entry.get('bytes') != geophys_meta['bytes']:
                qa.fail('national aeromagnetic survey-index bytes do not match artifact')
            sha = hashlib.sha256()
            with open(geophys, 'rb') as archive:
                for block in iter(lambda: archive.read(1024 * 1024), b''):
                    sha.update(block)
            if (not isinstance(geophys_entry.get('sha256'), str) or
                    re.fullmatch(r'[0-9a-f]{64}',
                                 geophys_entry.get('sha256', '')) is None or
                    sha.hexdigest() != geophys_entry.get('sha256')):
                qa.fail('national aeromagnetic survey-index sha256 does not match artifact')
    baselines = man.get('national_baselines')
    if not isinstance(baselines, dict):
        baselines = {}
        if release:
            qa.fail('manifest national_baselines must be an object')
    validate_admin_baseline(qa, man)
    validate_alaska_baselines(qa, baselines, release=release)
    validate_state_survey_baselines(qa, baselines)
    _validate_admin_clip_bindings(qa, baselines)
    validate_registry_baseline_pointers(qa, baselines)
    baseline_specs = {
        'mrds': {
            'layers': ['mrds'], 'state_scope': set(ALL_STATES), 'exact_scope': True,
            'feature_ids': True,
            # Null optional fields are legitimately omitted by MVT. These are
            # the canonical non-null fields required on every normalized row.
            'properties': ['nm', 'st', 'status', 'g', 'ex'],
        },
        'usmin': {
            'layers': ['usmin'], 'state_scope': set(ALL_STATES), 'exact_scope': True,
            'feature_ids': True,
            'properties': ['st', 'typ', 'agg'],
        },
        'stategeo': {
            'layers': ['stategeo'], 'state_scope': set(ALL_STATES), 'exact_scope': False,
            'properties': ['id', 'nm', 'st', 'typ', 'commodities', 'status', 'g', 'ex'],
        },
        'claims': {
            'layers': ['active', 'closed'], 'state_scope': set(CLAIM_STATES),
            'exact_scope': False,
            'properties': {'active': ['serial', 'st', 'type'],
                           'closed': ['serial', 'st', 'type', 'partial']},
        },
        'geology': {
            'layers': ['geology'], 'state_scope': set(ALL_STATES),
            'exact_scope': True, 'minimum_state_count': 1,
            'feature_inventory': True,
            'properties': {
                'geology': (
                    'fid', 'st', 'state', 'src', 'source_dataset', 'source_id',
                    'source_record_id', 'source_scale', 'source_scale_status', 'source_ref',
                    'source_url'),
            },
        },
        'faults': {
            'layers': ['faults'], 'state_scope': set(ALL_STATES),
            'exact_scope': True, 'minimum_state_count': 0,
            'feature_inventory': True,
            'properties': {
                'faults': (
                    'fid', 'st', 'state', 'src', 'source_dataset', 'source_id',
                    'source_record_id', 'source_scale', 'source_scale_status', 'source_ref',
                    'source_url'),
            },
        },
    }
    for baseline_id, spec in baseline_specs.items():
        entry = baselines.get(baseline_id)
        if not entry:
            if release or baseline_id in (
                    'mrds', 'usmin', 'stategeo', 'claims', 'geology', 'faults'):
                qa.fail(f'national {baseline_id.upper()} PMTiles baseline is missing')
            continue
        if not isinstance(entry, dict):
            qa.fail(f'national {baseline_id.upper()} baseline entry must be an object')
            continue
        expected_file = f'data/tiles/national/{baseline_id}.pmtiles'
        declared_layers = (entry.get('source_layers') if len(spec['layers']) > 1
                           else [entry.get('source_layer')])
        if (entry.get('format') != 'pmtiles' or declared_layers != spec['layers'] or
                entry.get('file') != expected_file or
                not _text(entry.get('source')) or
                not isinstance(entry.get('retrieved'), str) or
                re.fullmatch(r'\d{4}-\d{2}-\d{2}', entry['retrieved']) is None):
            qa.fail(f'national {baseline_id.upper()} baseline delivery schema is invalid')
            continue
        try:
            artifact = _resolve_artifact(entry['file'], baseline_id.upper())
        except ValueError as exc:
            qa.fail(f'national {baseline_id.upper()} PMTiles: {exc}')
            continue
        if not os.path.isfile(artifact):
            qa.fail(f'national {baseline_id.upper()} PMTiles artifact is missing')
            continue
        try:
            expected_source_records = (
                {spec['layers'][0]: entry.get('n')}
                if spec.get('feature_inventory') else None)
            meta = _pmtiles_header(
                artifact, spec['layers'], spec['properties'],
                verify_feature_properties=(spec.get('feature_inventory', False) or
                                           spec.get('feature_ids', False)),
                collect_feature_inventory=spec.get('feature_inventory', False),
                collect_feature_ids=spec.get('feature_ids', False),
                expected_source_records=expected_source_records)
        except ValueError as exc:
            qa.fail(f'national {baseline_id.upper()} PMTiles: {exc}')
            continue
        counts = entry.get('states')
        n = entry.get('n')
        count_states = set(counts) if isinstance(counts, dict) else set()
        minimum_state_count = spec.get('minimum_state_count', 1)
        if (not _is_int(n, 1) or not isinstance(counts, dict) or not counts or
                not count_states <= spec['state_scope'] or
                (spec['exact_scope'] and count_states != spec['state_scope']) or
                any(not _is_int(value, minimum_state_count)
                    for value in counts.values()) or
                sum(counts.values()) != n):
            scope = '49-state ' if spec['exact_scope'] else ''
            qa.fail(f'national {baseline_id.upper()} baseline lacks valid {scope}counts')
        if entry.get('bytes') != meta['bytes']:
            qa.fail(f'national {baseline_id.upper()} manifest bytes do not match artifact')
        expected_sha = entry.get('sha256')
        if not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha):
            qa.fail(f'national {baseline_id.upper()} manifest sha256 is invalid')
        else:
            sha = hashlib.sha256()
            with open(artifact, 'rb') as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b''):
                    sha.update(block)
            if sha.hexdigest() != expected_sha:
                qa.fail(f'national {baseline_id.upper()} manifest sha256 does not match artifact')
        if baseline_id in ('geology', 'faults'):
            inventory = meta.get('feature_inventories', {}).get(
                spec['layers'][0])
            _validate_national_geology_fault_entry(
                qa, baseline_id, entry, inventory,
                maxzoom_inventory=meta.get(
                    'maxzoom_feature_inventories', {}).get(spec['layers'][0]),
                all_zoom_instances=meta.get(
                    'all_zoom_feature_instances', {}).get(spec['layers'][0]),
                maxzoom_instances=meta.get(
                    'maxzoom_feature_instances', {}).get(spec['layers'][0]),
                archive_maxzoom=meta.get('maxzoom'))
        if spec.get('feature_ids'):
            _validate_national_point_id_inventory(
                qa, baseline_id, entry, meta,
                spec['layers'][0])
        if baseline_id == 'stategeo':
            sources = entry.get('sources')
            if (not isinstance(sources, dict) or set(sources) != count_states or
                    any(not isinstance(item, dict) or item.get('n') != counts[state] or
                        not _text(item.get('build_input')) or
                        item.get('build_input') != item.get('manifest_key') or
                        not _text(item.get('source')) or
                        not isinstance(item.get('sha256'), str) or
                        re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is None
                        for state, item in (sources or {}).items())):
                qa.fail('national STATEGEO source provenance does not match its state counts')
        if baseline_id == 'claims':
            by_mode = entry.get('by_mode')
            valid_modes = isinstance(by_mode, dict) and set(by_mode) == {'active', 'closed'}
            mode_total = 0
            aggregate = {state: 0 for state in count_states}
            if valid_modes:
                for mode in ('active', 'closed'):
                    mode_entry = by_mode[mode]
                    mode_counts = (mode_entry.get('states')
                                   if isinstance(mode_entry, dict) else None)
                    if (not isinstance(mode_counts, dict) or
                            not set(mode_counts) <= count_states or
                            any(not _is_int(value, 1) for value in mode_counts.values()) or
                            mode_entry.get('n') != sum(mode_counts.values())):
                        valid_modes = False
                        break
                    mode_total += mode_entry['n']
                    for state, value in mode_counts.items():
                        aggregate[state] += value
            if not valid_modes or mode_total != n or aggregate != counts:
                qa.fail('national CLAIMS active/closed counts do not reconcile')
            partial = entry.get('partial_states')
            snapshots = entry.get('partial_snapshots')
            if (not isinstance(partial, list) or len(partial) != len(set(partial)) or
                    any(state not in count_states for state in partial) or
                    not isinstance(snapshots, list) or
                    len(snapshots) != len(set(snapshots)) or
                    {item.split('_', 1)[0].upper() for item in snapshots
                     if isinstance(item, str) and '_' in item} != set(partial)):
                qa.fail('national CLAIMS partial-state provenance is invalid')
            if release and partial:
                qa.fail(f'national CLAIMS baseline is partial for {sorted(partial)}')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', choices=('progress', 'release'), default='progress')
    ap.add_argument('--require-all', action='store_true')
    args = ap.parse_args(argv)
    qa = QA()
    reg = validate_registry()
    qa.errors.extend(reg['errors'])
    if qa.errors:
        states = {}
    else:
        states = load_states()
        validate_no_statewide_json(qa, states)
        release = args.profile == 'release' or args.require_all
        validate_base_metal_prices(qa)
        validate_scoring(qa, states=states, release=release)
        validate_manifest_orphans(qa, release=release)
        validate_browser_delivery_contract(qa, release=release)
        if args.require_all:
            validate_release_registry_candidates(qa, states)
        validate_artifacts(qa, states, release_profile=release,
                           require_all=args.require_all)
        # A single completed P1/P2 state is allowed to ship before 49/49, but
        # it must never bypass artifact-backed DONE evidence merely because
        # the operator selected the normal progress/deploy profile. The
        # validator itself skips disabled states; --require-all expands the
        # same checks to every release candidate.
        validate_release_evidence(qa, states, require_all=args.require_all)
        validate_ci_budgets(qa, states, require_all=args.require_all)
        current = coverage_bytes(build_coverage())
        if not os.path.exists(COVERAGE) or open(COVERAGE, 'rb').read() != current:
            qa.fail('site/data/coverage.json is missing or stale')
        if args.require_all:
            incomplete = [code for code, s in states.items()
                          if not s['release']['enabled'] or s['release']['status'] != 'done']
            if incomplete:
                qa.fail(f'--require-all needs all 49 states enabled/done; incomplete={incomplete}')
    for warning in qa.warnings:
        print('WARNING:', warning)
    for error in qa.errors:
        print('ERROR:', error, file=sys.stderr)
    print(json.dumps({'profile': args.profile, 'ok': not qa.errors,
                      'errors': len(qa.errors), 'warnings': len(qa.warnings)}))
    return 0 if not qa.errors else 1


if __name__ == '__main__':
    sys.exit(main())
