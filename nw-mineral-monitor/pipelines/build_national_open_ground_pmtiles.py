#!/usr/bin/env python3
"""Build checked federal open-ground PMTiles for the 19 claim states.

The analysis unit is one BLM CadNSDI PLSS section polygon.  Active federal
claims are assigned from their authoritative MLRS legal-section identifiers;
land status is supplied independently as an explicitly checked mineral-
disposition classification for the same section identifiers.  The result is
one polygon feature per section, never a title determination.

All raw inputs and their exact 19-state inventory remain in private staging.
Only a validated, content-addressed PMTiles archive and an atomically replaced
publication pointer are written.  This module never edits the public manifest
or a state registry entry.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter, defaultdict
from datetime import datetime

from state_registry import (
    ALL_STATES, CLAIM_STATES, DEFAULTS_PATH, load_states, state_path,
)


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
DEFAULT_STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')
INFRA = os.path.join(ROOT, 'infra')
if INFRA not in sys.path:
    sys.path.insert(0, INFRA)
from spatial_clip import StateClipIndex
from validate_national import _pmtiles_header as _strict_pmtiles_header


SCHEMA_VERSION = 1
SYSTEM = 'federal_open_ground'
PROFILES = ('progress', 'full', 'release')
KINDS = ('plss', 'active_claims', 'land_status')
SAFE_INTEGER_MAX = (1 << 53) - 1
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SECTION_ID_RE = re.compile(r'^[A-Z]{2}[A-Za-z0-9_.:/ -]{1,158}$')

PLSS_SOURCE = ('https://gis.blm.gov/arcgis/rest/services/Cadastral/'
               'BLM_Natl_PLSS_CadNSDI/MapServer/2')
ACTIVE_CLAIMS_SOURCE = ('https://gis.blm.gov/nlsdb/rest/services/'
                        'Mining_Claims/MiningClaims/MapServer/1')
LAND_STATUS_SOURCES = {
    'sma': ('https://gis.blm.gov/arcgis/rest/services/lands/'
            'BLM_Natl_SMA_LimitedScale/MapServer/1'),
    'withdrawals': ('https://gis.blm.gov/nlsdb/rest/services/Land_Tenure/'
                    'Withdrawals_Case_Land_Status/MapServer/0'),
    'segregations': ('https://gis.blm.gov/nlsdb/rest/services/Land_Tenure/'
                     'Segregations_Lands_Minerals_Both/MapServer/0,1'),
    'nlcs': ('https://gis.blm.gov/arcgis/rest/services/lands/'
             'BLM_Natl_NLCS_WLD_WSA/MapServer/0,1'),
}
SOURCES = {
    'plss': PLSS_SOURCE,
    'active_claims': ACTIVE_CLAIMS_SOURCE,
    'land_status': LAND_STATUS_SOURCES,
}
CLIP_AUTHORITY = 'U.S. Census Bureau TIGERweb, January 1 2025 vintage'
CLIP_METHOD = ('PLSS analysis-unit representative point within authoritative '
               'state polygon')
MINERAL_DISPOSITIONS = frozenset({
    'open_to_location', 'withdrawn', 'non_federal', 'unknown',
})
OUTPUT_STATUSES = frozenset({
    'OPEN', 'ACTIVE', 'WITHDRAWN', 'NONFEDERAL', 'UNKNOWN',
})
TITLE_CAVEAT = ('Research lead only; not a title determination. Verify current '
                'mineral ownership, MLRS, and county records before staking.')
WITHDRAWAL_CAVEAT = ('Withdrawal, segregation, and surface-management data can '
                     'lag or be generalized; verify current BLM land-status '
                     'records before relying on this classification.')
REQUIRED_TILE_FIELDS = frozenset({
    'system', 'st', 'status', 'open_count', 'section_count',
    'open_fraction', 'mineral_title_status', 'mineral_title_source',
    'mineral_title_ref', 'mineral_title_reviewed',
    'title_caveat', 'withdrawal_caveat', 'provenance',
})
MINERAL_TITLE_STATUSES = frozenset({
    'public_domain_locatable', 'non_federal', 'unknown',
})


class PublicationError(ValueError):
    """A staging, derivation, tiling, or publication invariant failed."""


def _reject_constant(value):
    raise PublicationError(f'non-standard JSON number {value}')


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def _strict_json_bytes(raw, label):
    try:
        return json.loads(
            raw.decode('utf-8'), parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f'{label} is not strict UTF-8 JSON: {exc}') from exc


def _read_strict_json(path, label=None):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot read {label or path}: {exc}') from exc
    return _strict_json_bytes(raw, label or path), raw


def _sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError as exc:
        raise PublicationError(f'cannot hash {path}: {exc}') from exc
    return digest.hexdigest()


def _is_int(value, minimum=0):
    return (isinstance(value, int) and not isinstance(value, bool) and
            value >= minimum)


def _valid_date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be an ISO YYYY-MM-DD date')
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError as exc:
        raise PublicationError(f'{label} is not a calendar date') from exc
    return value


def _text(value, label, *, nullable=False, required=False, limit=512):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        suffix = ' or null' if nullable else ''
        raise PublicationError(f'{label} must be text{suffix}')
    value = value.strip()
    if required and not value:
        raise PublicationError(f'{label} must be nonempty text')
    if len(value) > limit:
        raise PublicationError(f'{label} exceeds {limit} characters')
    return value or None


def _finite(value, label, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PublicationError(f'{label} must be a finite number')
    result = float(value)
    if not math.isfinite(result):
        raise PublicationError(f'{label} must be a finite number')
    if low is not None and result < low or high is not None and result > high:
        raise PublicationError(f'{label} is outside [{low}, {high}]')
    return result


def _outside(path, parent):
    path = os.path.realpath(path)
    parent = os.path.realpath(parent)
    try:
        return os.path.commonpath((path, parent)) != parent
    except ValueError:
        return True


def _registry_claim_states():
    """Derive the open-ground scope from reviewed registry data."""
    states = load_states()
    if set(states) != set(ALL_STATES):
        raise PublicationError('registry must contain the exact 49-state scope')
    codes = {
        code for code, row in states.items()
        if row.get('regime') == 'claim'
    }
    if codes != set(CLAIM_STATES) or len(codes) != 19:
        raise PublicationError(
            'registry claim regime must equal the canonical 19 BLM states')
    for code, row in states.items():
        applicable = (row.get('open_ground') or {}).get('applicable')
        if applicable is not (code in codes):
            raise PublicationError(
                f'{code} registry open_ground.applicable disagrees with regime')
        if code not in codes:
            continue
        federal = [system for system in row.get('claim_systems', [])
                   if isinstance(system, dict) and
                   system.get('id') == 'federal_mlrs']
        if len(federal) != 1 or federal[0].get('browser_delivery') != 'pmtiles':
            raise PublicationError(
                f'{code} registry needs one tiled federal_mlrs claim system')
    return tuple(sorted(codes))


def _safe_staging_paths(staging_dir, inventory_path):
    staging = os.path.realpath(staging_dir)
    inventory = os.path.realpath(inventory_path)
    if not os.path.isdir(staging):
        raise PublicationError(f'private staging directory is missing: {staging}')
    if not os.path.isfile(inventory):
        raise PublicationError(f'private staging inventory is missing: {inventory}')
    if not _outside(staging, SITE) or not _outside(inventory, SITE):
        raise PublicationError(
            'raw open-ground staging and inventory must remain outside site/')
    return staging, inventory


def _snapshot_filename(code, kind):
    return f'{code.lower()}_{kind}.json'


def _snapshot_path(staging, code, kind, entry):
    expected = _snapshot_filename(code, kind)
    if not isinstance(entry, dict) or entry.get('file') != expected:
        got = entry.get('file') if isinstance(entry, dict) else None
        raise PublicationError(
            f'inventory {code}.{kind}.file must be {expected!r}, got {got!r}')
    path = os.path.realpath(os.path.join(staging, expected))
    if _outside(path, staging):
        raise PublicationError(f'inventory {code}.{kind} escapes staging')
    if not os.path.isfile(path):
        raise PublicationError(
            f'inventory {code}.{kind} file is missing: {expected}')
    return path


def _validate_inventory_entry(code, kind, entry, staging):
    allowed = {'file', 'n', 'bytes', 'sha256', 'retrieved', 'complete',
               'partial_reason'}
    if not isinstance(entry, dict) or not set(entry) <= allowed:
        raise PublicationError(
            f'inventory {code}.{kind} contains unsupported fields')
    path = _snapshot_path(staging, code, kind, entry)
    if not _is_int(entry.get('n')):
        raise PublicationError(f'inventory {code}.{kind}.n must be nonnegative')
    if not _is_int(entry.get('bytes'), 2):
        raise PublicationError(f'inventory {code}.{kind}.bytes must be positive')
    if (not isinstance(entry.get('sha256'), str) or
            SHA256_RE.fullmatch(entry['sha256']) is None):
        raise PublicationError(f'inventory {code}.{kind}.sha256 is invalid')
    _valid_date(entry.get('retrieved'), f'inventory {code}.{kind}.retrieved')
    if not isinstance(entry.get('complete'), bool):
        raise PublicationError(
            f'inventory {code}.{kind}.complete must be boolean')
    reason = entry.get('partial_reason')
    if not entry['complete'] and (not isinstance(reason, str) or not reason.strip()):
        raise PublicationError(
            f'inventory {code}.{kind} needs partial_reason when incomplete')
    if entry['complete'] and reason is not None:
        raise PublicationError(
            f'inventory {code}.{kind} cannot have partial_reason when complete')
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise PublicationError(f'cannot stat {code}.{kind}: {exc}') from exc
    digest = _sha256_file(path)
    if size != entry['bytes'] or digest != entry['sha256']:
        raise PublicationError(
            f'{code}.{kind} bytes/sha256 differ from inventory')
    return path, {'bytes': size, 'sha256': digest, 'n': entry['n']}


def _load_clip_indexes(state_clips_path, expected_sha):
    path = os.path.realpath(state_clips_path)
    actual_sha = _sha256_file(path)
    if actual_sha != expected_sha:
        raise PublicationError(
            'inventory clip artifact sha256 does not match --state-clips')
    clips, _ = _read_strict_json(path, 'authoritative state clips')
    if (not isinstance(clips, dict) or clips.get('schema_version') != 1 or
            not isinstance(clips.get('source'), str) or
            'tigerweb' not in clips['source'].lower() or
            'January 1 2025' not in clips['source'] or
            not isinstance(clips.get('states'), dict) or
            set(clips['states']) != set(ALL_STATES)):
        raise PublicationError(
            'state clips must be the exact 49-state Census 2025 index')
    indexes = {}
    for code in sorted(CLAIM_STATES):
        geometry = clips['states'][code]
        if (not isinstance(geometry, dict) or
                geometry.get('type') not in ('Polygon', 'MultiPolygon')):
            raise PublicationError(f'authoritative {code} clip is not a polygon')
        indexes[code] = StateClipIndex(geometry)
    return indexes, actual_sha, path


def load_inventory(staging_dir, inventory_path,
                   state_clips_path=DEFAULT_STATE_CLIPS):
    """Load and eagerly checksum the exact 57-file, 19-state contract."""
    staging, inventory_path = _safe_staging_paths(staging_dir, inventory_path)
    inventory, inventory_raw = _read_strict_json(
        inventory_path, 'federal open-ground staging inventory')
    required = {'schema_version', 'system', 'created', 'sources', 'clip', 'states'}
    if not isinstance(inventory, dict) or set(inventory) != required:
        raise PublicationError(
            'inventory keys must be exactly schema_version/system/created/'
            'sources/clip/states')
    if inventory.get('schema_version') != SCHEMA_VERSION:
        raise PublicationError(f'inventory schema_version must be {SCHEMA_VERSION}')
    if inventory.get('system') != SYSTEM or inventory.get('sources') != SOURCES:
        raise PublicationError('inventory source authorities are invalid')
    _valid_date(inventory.get('created'), 'inventory.created')
    clip = inventory.get('clip')
    if (not isinstance(clip, dict) or
            set(clip) != {'authority', 'method', 'artifact_sha256'} or
            clip.get('authority') != CLIP_AUTHORITY or
            clip.get('method') != CLIP_METHOD or
            not isinstance(clip.get('artifact_sha256'), str) or
            SHA256_RE.fullmatch(clip['artifact_sha256']) is None):
        raise PublicationError('inventory state-clip provenance is invalid')
    codes = _registry_claim_states()
    registry = load_states()
    mineral_estates = {
        code: registry[code]['open_ground']['mineral_estate']
        for code in codes
    }
    mineral_estates_sha256 = _sha256_bytes(json.dumps(
        mineral_estates, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode())
    registry_paths = [DEFAULTS_PATH] + [state_path(code) for code in codes]
    registry_hashes = {
        os.path.realpath(path): _sha256_file(path) for path in registry_paths
    }
    states = inventory.get('states')
    if not isinstance(states, dict) or set(states) != set(codes):
        missing = sorted(set(codes) - set(states or {}))
        extra = sorted(set(states or {}) - set(codes))
        raise PublicationError(
            f'inventory states must be exact registry 19; '
            f'missing={missing}, extra={extra}')
    paths = {}
    integrity = {}
    for code in codes:
        row = states[code]
        if not isinstance(row, dict) or set(row) != set(KINDS):
            raise PublicationError(
                f'inventory {code} must contain exactly {list(KINDS)}')
        for kind in KINDS:
            path, stats = _validate_inventory_entry(
                code, kind, row[kind], staging)
            paths[(code, kind)] = path
            integrity[(code, kind)] = stats
    indexes, clip_sha, clip_path = _load_clip_indexes(
        state_clips_path, clip['artifact_sha256'])
    return {
        'staging': staging,
        'inventory_path': inventory_path,
        'inventory': inventory,
        'inventory_raw': inventory_raw,
        'inventory_sha256': _sha256_bytes(inventory_raw),
        'codes': codes,
        'paths': paths,
        'integrity': integrity,
        'clip_indexes': indexes,
        'clip_sha256': clip_sha,
        'state_clips_path': clip_path,
        'mineral_estates': mineral_estates,
        'mineral_estates_sha256': mineral_estates_sha256,
        'registry_hashes': registry_hashes,
    }


def _snapshot_partial(entry, data):
    partial = not entry['complete'] or not data['complete']
    partial = partial or any(bool(data.get(flag)) for flag in (
        'capped', 'truncated', 'partial'))
    total = data.get('total_available')
    if total is not None:
        if not _is_int(total) or total < data['n']:
            raise PublicationError(
                'snapshot total_available must be an integer >= n')
        partial = partial or total > data['n']
    if partial:
        reason = data.get('partial_reason') or entry.get('partial_reason')
        if not isinstance(reason, str) or not reason.strip():
            raise PublicationError('partial snapshot needs a nonempty partial_reason')
    elif data.get('partial_reason') is not None:
        raise PublicationError(
            'complete snapshot cannot carry partial_reason')
    return bool(partial)


def _load_snapshot(context, code, kind):
    entry = context['inventory']['states'][code][kind]
    path = context['paths'][(code, kind)]
    data, raw = _read_strict_json(path, f'{code}.{kind} snapshot')
    digest = _sha256_bytes(raw)
    if len(raw) != entry['bytes'] or digest != entry['sha256']:
        raise PublicationError(f'{code}.{kind} changed after inventory load')
    common = {
        'schema_version', 'state', 'kind', 'retrieved', 'complete', 'n',
        'capped', 'truncated', 'partial', 'total_available', 'partial_reason',
    }
    allowed = {
        'plss': common | {'source', 'type', 'features'},
        'active_claims': common | {
            'system', 'source', 'mode', 'unmapped_count', 'claims'},
        'land_status': common | {'sources', 'classifications'},
    }[kind]
    if not isinstance(data, dict) or not set(data) <= allowed:
        unknown = sorted(set(data or {}) - allowed) if isinstance(data, dict) else []
        raise PublicationError(
            f'{code}.{kind} snapshot has unsupported fields {unknown}')
    if (data.get('schema_version') != SCHEMA_VERSION or
            data.get('state') != code or data.get('kind') != kind):
        raise PublicationError(
            f'{code}.{kind} snapshot schema/state/kind identity is invalid')
    if data.get('retrieved') != entry['retrieved']:
        raise PublicationError(
            f'{code}.{kind} retrieved date differs from inventory')
    _valid_date(data.get('retrieved'), f'{code}.{kind}.retrieved')
    if not isinstance(data.get('complete'), bool):
        raise PublicationError(f'{code}.{kind}.complete must be boolean')
    n = data.get('n')
    if not _is_int(n) or n != entry['n']:
        raise PublicationError(
            f'{code}.{kind} n={n!r} differs from inventory n={entry["n"]}')
    for flag in ('capped', 'truncated', 'partial'):
        if flag in data and not isinstance(data[flag], bool):
            raise PublicationError(f'{code}.{kind}.{flag} must be boolean')
    expected_source = {
        'plss': ('source', PLSS_SOURCE),
        'active_claims': ('source', ACTIVE_CLAIMS_SOURCE),
        'land_status': ('sources', LAND_STATUS_SOURCES),
    }[kind]
    if data.get(expected_source[0]) != expected_source[1]:
        raise PublicationError(f'{code}.{kind} source authority is invalid')
    collection = {
        'plss': 'features',
        'active_claims': 'claims',
        'land_status': 'classifications',
    }[kind]
    if not isinstance(data.get(collection), list) or len(data[collection]) != n:
        raise PublicationError(
            f'{code}.{kind}.{collection} must contain exactly n rows')
    if kind == 'plss' and data.get('type') != 'FeatureCollection':
        raise PublicationError(f'{code}.plss must be a FeatureCollection')
    if kind == 'active_claims' and (
            data.get('system') != 'federal_mlrs' or data.get('mode') != 'active'):
        raise PublicationError(
            f'{code}.active_claims must be authoritative federal_mlrs active data')
    return data, _snapshot_partial(entry, data), {
        'bytes': len(raw), 'sha256': digest, 'n': n,
        'retrieved': data['retrieved'],
    }


def _validate_ring(ring, label):
    if not isinstance(ring, list) or len(ring) < 4:
        raise PublicationError(f'{label} must have at least four positions')
    points = []
    for index, position in enumerate(ring):
        if not isinstance(position, list) or len(position) != 2:
            raise PublicationError(f'{label}[{index}] must be [longitude, latitude]')
        points.append((
            _finite(position[0], f'{label}[{index}].longitude', -180, 180),
            _finite(position[1], f'{label}[{index}].latitude', -90, 90),
        ))
    if points[0] != points[-1]:
        raise PublicationError(f'{label} is not closed')
    if len(set(points[:-1])) < 3:
        raise PublicationError(f'{label} is degenerate')
    double_area = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, points[1:]))
    if abs(double_area) <= 1e-12:
        raise PublicationError(f'{label} has zero area')
    return points, double_area


def _geometry_parts(geometry, label):
    if not isinstance(geometry, dict) or set(geometry) != {'type', 'coordinates'}:
        raise PublicationError(f'{label} must be a strict polygon geometry')
    kind = geometry.get('type')
    coordinates = geometry.get('coordinates')
    polygons = [coordinates] if kind == 'Polygon' else coordinates
    if kind not in ('Polygon', 'MultiPolygon') or not isinstance(polygons, list):
        raise PublicationError(f'{label} must be Polygon or MultiPolygon')
    if not polygons:
        raise PublicationError(f'{label} has no polygons')
    parsed = []
    for polygon_index, polygon in enumerate(polygons):
        if not isinstance(polygon, list) or not polygon:
            raise PublicationError(f'{label} polygon {polygon_index} has no rings')
        rings = []
        for ring_index, ring in enumerate(polygon):
            rings.append(_validate_ring(
                ring, f'{label}.coordinates[{polygon_index}][{ring_index}]'))
        parsed.append(rings)
    all_points = [point for polygon in parsed for ring, _ in polygon
                  for point in ring]
    xs = [point[0] for point in all_points]
    ys = [point[1] for point in all_points]
    if max(xs) - min(xs) > 2 or max(ys) - min(ys) > 2:
        raise PublicationError(f'{label} is not a plausible PLSS analysis unit')
    return parsed


def _representative_point(parsed, label):
    outer, double_area = max(
        (polygon[0] for polygon in parsed), key=lambda value: abs(value[1]))
    factor = 1 / (3 * double_area)
    x = factor * sum(
        (x1 + x2) * (x1 * y2 - x2 * y1)
        for (x1, y1), (x2, y2) in zip(outer, outer[1:]))
    y = factor * sum(
        (y1 + y2) * (x1 * y2 - x2 * y1)
        for (x1, y1), (x2, y2) in zip(outer, outer[1:]))
    if not math.isfinite(x) or not math.isfinite(y):
        raise PublicationError(f'{label} has no finite representative point')
    return x, y


def _section_id(value, code, label):
    value = _text(value, label, required=True, limit=160)
    if SECTION_ID_RE.fullmatch(value) is None or not value.startswith(code):
        raise PublicationError(f'{label} is not a {code} section identifier')
    return value


def _parse_plss(context, code, data):
    sections = {}
    feature_ids = set()
    allowed_feature = {'type', 'id', 'properties', 'geometry'}
    allowed_properties = {
        'section_id', 'label', 'township', 'range', 'section', 'meridian',
    }
    for index, feature in enumerate(data['features']):
        prefix = f'{code}.plss.features[{index}]'
        if (not isinstance(feature, dict) or
                not set(feature) <= allowed_feature or
                feature.get('type') != 'Feature'):
            raise PublicationError(f'{prefix} is not a strict GeoJSON feature')
        properties = feature.get('properties')
        if (not isinstance(properties, dict) or
                not set(properties) <= allowed_properties):
            raise PublicationError(f'{prefix}.properties schema is invalid')
        section_id = _section_id(
            properties.get('section_id'), code, f'{prefix}.section_id')
        if feature.get('id') is not None and feature['id'] != section_id:
            raise PublicationError(f'{prefix}.id disagrees with section_id')
        if section_id in sections:
            raise PublicationError(f'{code}.plss duplicates section {section_id!r}')
        label = _text(properties.get('label'), f'{prefix}.label', nullable=True,
                      limit=240) or section_id
        parsed = _geometry_parts(feature.get('geometry'), f'{prefix}.geometry')
        longitude, latitude = _representative_point(parsed, f'{prefix}.geometry')
        if not context['clip_indexes'][code].contains(longitude, latitude):
            raise PublicationError(
                f'{prefix} lies outside the authoritative {code} state clip')
        feature_id = _feature_id(code, section_id)
        if feature_id in feature_ids:
            raise PublicationError(f'stable feature-ID collision for {section_id!r}')
        feature_ids.add(feature_id)
        sections[section_id] = {
            'id': feature_id,
            'label': label,
            'geometry': feature['geometry'],
        }
    if not sections:
        raise PublicationError(f'{code}.plss contains no analysis units')
    return sections


def _parse_claims(code, data, section_ids):
    if not _is_int(data.get('unmapped_count')):
        raise PublicationError(f'{code}.active_claims.unmapped_count is invalid')
    counts = defaultdict(int)
    serials = set()
    uncertain = False
    unmapped_count = 0
    allowed = {
        'serial', 'name', 'disposition', 'source_object_id', 'section_ids',
        'mapping_complete',
    }
    for index, claim in enumerate(data['claims']):
        prefix = f'{code}.active_claims.claims[{index}]'
        if not isinstance(claim, dict) or not set(claim) <= allowed:
            raise PublicationError(f'{prefix} schema is invalid')
        serial = _text(claim.get('serial'), f'{prefix}.serial', required=True,
                       limit=160)
        if serial in serials:
            raise PublicationError(f'{code}.active_claims duplicates serial {serial!r}')
        serials.add(serial)
        _text(claim.get('name'), f'{prefix}.name', nullable=True, limit=512)
        _text(claim.get('disposition'), f'{prefix}.disposition', nullable=True,
              limit=128)
        object_id = claim.get('source_object_id')
        if object_id is not None and (isinstance(object_id, bool) or
                                      not isinstance(object_id, (int, str))):
            raise PublicationError(f'{prefix}.source_object_id is invalid')
        mapped = claim.get('mapping_complete')
        if not isinstance(mapped, bool):
            raise PublicationError(f'{prefix}.mapping_complete must be boolean')
        values = claim.get('section_ids')
        if not isinstance(values, list):
            raise PublicationError(f'{prefix}.section_ids must be an array')
        normalized = [
            _section_id(value, code, f'{prefix}.section_ids[{offset}]')
            for offset, value in enumerate(values)
        ]
        if len(set(normalized)) != len(normalized):
            raise PublicationError(f'{prefix} duplicates a section identifier')
        if mapped and not normalized:
            raise PublicationError(
                f'{prefix} claims complete mapping but has no sections')
        if not mapped:
            unmapped_count += 1
            uncertain = True
        unknown = set(normalized) - section_ids
        if unknown:
            uncertain = True
        for section_id in set(normalized) & section_ids:
            counts[section_id] += 1
    if unmapped_count != data['unmapped_count']:
        raise PublicationError(
            f'{code}.active_claims unmapped_count disagrees with rows')
    return counts, uncertain, unmapped_count


def _parse_land_status(code, data, section_ids, mineral_estate):
    rows = {}
    uncertain_ids = set()
    allowed = {
        'section_id', 'mineral_disposition', 'surface_manager',
        'withdrawal_refs', 'checked_sources', 'boundary_uncertain', 'evidence',
        'mineral_title_status', 'mineral_title_source', 'mineral_title_ref',
        'mineral_title_reviewed',
    }
    required_checks = set(LAND_STATUS_SOURCES)
    for index, row in enumerate(data['classifications']):
        prefix = f'{code}.land_status.classifications[{index}]'
        if not isinstance(row, dict) or set(row) != allowed:
            raise PublicationError(f'{prefix} schema is invalid')
        section_id = _section_id(
            row.get('section_id'), code, f'{prefix}.section_id')
        if section_id in rows:
            raise PublicationError(
                f'{code}.land_status duplicates section {section_id!r}')
        if section_id not in section_ids:
            raise PublicationError(
                f'{prefix} references a section absent from PLSS')
        disposition = row.get('mineral_disposition')
        if disposition not in MINERAL_DISPOSITIONS:
            raise PublicationError(f'{prefix}.mineral_disposition is invalid')
        manager = _text(row.get('surface_manager'), f'{prefix}.surface_manager',
                        nullable=True, limit=160)
        refs = row.get('withdrawal_refs')
        if not isinstance(refs, list) or len(refs) > 20:
            raise PublicationError(f'{prefix}.withdrawal_refs is invalid')
        refs = [_text(value, f'{prefix}.withdrawal_refs', required=True,
                      limit=160) for value in refs]
        if len(set(refs)) != len(refs):
            raise PublicationError(f'{prefix}.withdrawal_refs has duplicates')
        if disposition == 'withdrawn' and not refs:
            raise PublicationError(
                f'{prefix} withdrawn classification needs withdrawal_refs')
        if disposition != 'withdrawn' and refs:
            raise PublicationError(
                f'{prefix} non-withdrawn classification cannot carry withdrawals')
        checks = row.get('checked_sources')
        if (not isinstance(checks, list) or len(set(checks)) != len(checks) or
                not set(checks) <= required_checks):
            raise PublicationError(f'{prefix}.checked_sources is invalid')
        boundary_uncertain = row.get('boundary_uncertain')
        if not isinstance(boundary_uncertain, bool):
            raise PublicationError(
                f'{prefix}.boundary_uncertain must be boolean')
        evidence = _text(row.get('evidence'), f'{prefix}.evidence', required=True,
                         limit=800)
        title_status = row.get('mineral_title_status')
        title_source = row.get('mineral_title_source')
        title_ref = row.get('mineral_title_ref')
        title_reviewed = row.get('mineral_title_reviewed')
        if title_status not in MINERAL_TITLE_STATUSES:
            raise PublicationError(
                f'{prefix}.mineral_title_status is invalid')
        if not isinstance(title_reviewed, bool):
            raise PublicationError(
                f'{prefix}.mineral_title_reviewed must be boolean')
        if title_status == 'unknown':
            if (title_source is not None or title_ref is not None or
                    title_reviewed is not False):
                raise PublicationError(
                    f'{prefix} unknown mineral title cannot carry source/ref/review')
        else:
            title_source = _text(
                title_source, f'{prefix}.mineral_title_source', required=True,
                limit=512)
            title_ref = _text(
                title_ref, f'{prefix}.mineral_title_ref', required=True,
                limit=240)
            expected = mineral_estate if isinstance(mineral_estate, dict) else {}
            if (not title_source.startswith('https://') or
                    title_reviewed is not True or
                    expected.get('status') != 'reviewed_ingested' or
                    title_source != expected.get('source_url')):
                raise PublicationError(
                    f'{prefix} mineral title is not bound to the reviewed '
                    'state-registry ingest')
        if (disposition == 'open_to_location' and
                title_status != 'public_domain_locatable'):
            raise PublicationError(
                f'{prefix} OPEN requires reviewed public-domain locatable title')
        if (disposition == 'non_federal' and
                title_status != 'non_federal'):
            raise PublicationError(
                f'{prefix} NONFEDERAL requires reviewed non-federal title')
        uncertain = (set(checks) != required_checks or boundary_uncertain or
                     disposition == 'unknown')
        if uncertain:
            uncertain_ids.add(section_id)
        rows[section_id] = {
            'mineral_disposition': disposition,
            'surface_manager': manager or 'UNKNOWN',
            'withdrawal_refs': refs,
            'checked_sources': sorted(checks),
            'boundary_uncertain': boundary_uncertain,
            'evidence': evidence,
            'mineral_title_status': title_status,
            'mineral_title_source': title_source,
            'mineral_title_ref': title_ref,
            'mineral_title_reviewed': title_reviewed,
        }
    missing = section_ids - set(rows)
    uncertain_ids.update(missing)
    return rows, uncertain_ids, missing


def _feature_id(code, section_id):
    identity = f'{SYSTEM}\x1f{code}\x1f{section_id}'.encode('utf-8')
    digest = hashlib.blake2b(
        identity, digest_size=8, person=b'nwmm-open-v1').digest()
    return (int.from_bytes(digest, 'big') & SAFE_INTEGER_MAX) or 1


def _status(active_count, land, claim_uncertain, land_uncertain):
    if active_count:
        return 'ACTIVE'
    if claim_uncertain or land_uncertain or land is None:
        return 'UNKNOWN'
    return {
        'open_to_location': 'OPEN',
        'withdrawn': 'WITHDRAWN',
        'non_federal': 'NONFEDERAL',
        'unknown': 'UNKNOWN',
    }[land['mineral_disposition']]


def _output_feature(code, section_id, section, active_count, land, status,
                    provenance, as_of, partial):
    if status not in OUTPUT_STATUSES:
        raise PublicationError(f'internal status {status!r} is invalid')
    open_count = 1 if status == 'OPEN' else 0
    section_count = 1
    open_fraction = open_count / section_count
    if (open_count not in (0, 1) or section_count != 1 or
            open_fraction != open_count or
            (status == 'OPEN') is not (open_fraction == 1.0)):
        raise PublicationError(f'internal open-ground math failed for {section_id}')
    land = land or {
        'mineral_disposition': 'unknown',
        'surface_manager': 'UNKNOWN',
        'withdrawal_refs': [],
        'checked_sources': [],
        'boundary_uncertain': True,
        'evidence': 'No section-level land-status row was present.',
        'mineral_title_status': 'unknown',
        'mineral_title_source': None,
        'mineral_title_ref': None,
        'mineral_title_reviewed': False,
    }
    properties = {
        'fid': section['id'],
        'system': SYSTEM,
        'st': code,
        'unit_id': section_id,
        'section_id': section_id,
        'label': section['label'],
        'status': status,
        'open_count': open_count,
        'section_count': section_count,
        'open_fraction': open_fraction,
        'active_count': active_count,
        'mineral_disposition': land['mineral_disposition'],
        'surface_manager': land['surface_manager'],
        'withdrawal_count': len(land['withdrawal_refs']),
        'withdrawal_refs': '; '.join(land['withdrawal_refs']),
        'checked_sources': ','.join(land['checked_sources']),
        'boundary_uncertain': 1 if land['boundary_uncertain'] else 0,
        'evidence': land['evidence'],
        'mineral_title_status': land['mineral_title_status'],
        'mineral_title_source': land['mineral_title_source'] or '',
        'mineral_title_ref': land['mineral_title_ref'] or '',
        'mineral_title_reviewed': (
            1 if land['mineral_title_reviewed'] else 0),
        'title_caveat': TITLE_CAVEAT,
        'withdrawal_caveat': WITHDRAWAL_CAVEAT,
        'provenance': provenance,
        'as_of': as_of,
        'partial': 1 if partial else 0,
    }
    return {
        'type': 'Feature',
        'id': section['id'],
        'properties': properties,
        'geometry': section['geometry'],
    }


def _selected_codes(context, selected_states=None):
    if selected_states is None:
        return context['codes']
    if isinstance(selected_states, str):
        selected_states = [selected_states]
    if (not isinstance(selected_states, (list, tuple, set)) or
            any(not isinstance(code, str) or not code.strip()
                for code in selected_states)):
        raise PublicationError('selected states must be nonempty state codes')
    normalized = tuple(sorted({code.strip().upper()
                               for code in selected_states}))
    if not normalized:
        raise PublicationError('at least one --state is required when filtering')
    invalid = set(normalized) - set(context['codes'])
    if invalid:
        raise PublicationError(
            f'open-ground scope contains non-claim states {sorted(invalid)}')
    return normalized


def stream_states(context, output_path, profile='release', selected_states=None):
    """Validate staged inputs and stream one section feature per analysis unit."""
    if profile not in PROFILES:
        raise PublicationError(f'unsupported profile {profile!r}')
    codes = _selected_codes(context, selected_states)
    strict = profile in ('full', 'release')
    state_counts = {}
    status_counts = Counter()
    state_status_counts = {}
    partial_states = set()
    active_claim_counts = {}
    inputs = {}
    seen_feature_ids = set()
    total_open = 0
    total_sections = 0
    with open(output_path, 'w', encoding='utf-8') as output:
        for code in codes:
            plss, plss_partial, plss_stats = _load_snapshot(
                context, code, 'plss')
            claims, claims_partial, claim_stats = _load_snapshot(
                context, code, 'active_claims')
            land_data, land_partial, land_stats = _load_snapshot(
                context, code, 'land_status')
            sections = _parse_plss(context, code, plss)
            section_ids = set(sections)
            active_by_section, mapping_uncertain, unmapped = _parse_claims(
                code, claims, section_ids)
            land, uncertain_land, missing_land = _parse_land_status(
                code, land_data, section_ids,
                context['mineral_estates'][code])
            input_partial = plss_partial or claims_partial or land_partial
            state_partial = bool(
                input_partial or mapping_uncertain or uncertain_land or missing_land)
            if strict and input_partial:
                raise PublicationError(
                    f'{code} has partial/capped input; {profile} forbids it')
            if strict and mapping_uncertain:
                raise PublicationError(
                    f'{code} has unmapped claims or claim sections absent from PLSS')
            if strict and (uncertain_land or missing_land):
                raise PublicationError(
                    f'{code} land status is unknown, unchecked, boundary-uncertain, '
                    'or missing a PLSS section')
            if state_partial:
                partial_states.add(code)
            as_of = max(plss['retrieved'], claims['retrieved'],
                        land_data['retrieved'])
            provenance = (
                f'PLSS:{plss_stats["sha256"][:12]};'
                f'MLRS:{claim_stats["sha256"][:12]};'
                f'LAND:{land_stats["sha256"][:12]}')
            code_statuses = Counter()
            for section_id in sorted(sections):
                section = sections[section_id]
                if section['id'] in seen_feature_ids:
                    raise PublicationError(
                        f'national feature-ID collision at {code}.{section_id}')
                seen_feature_ids.add(section['id'])
                active_count = active_by_section.get(section_id, 0)
                land_row = land.get(section_id)
                status = _status(
                    active_count, land_row,
                    claims_partial or mapping_uncertain,
                    land_partial or section_id in uncertain_land)
                feature = _output_feature(
                    code, section_id, section, active_count, land_row, status,
                    provenance, as_of, state_partial)
                json.dump(feature, output, separators=(',', ':'), allow_nan=False)
                output.write('\n')
                code_statuses[status] += 1
                status_counts[status] += 1
                total_open += feature['properties']['open_count']
                total_sections += feature['properties']['section_count']
            if sum(code_statuses.values()) != len(sections):
                raise PublicationError(f'{code} did not emit one feature per section')
            state_counts[code] = len(sections)
            state_status_counts[code] = {
                status: code_statuses.get(status, 0)
                for status in sorted(OUTPUT_STATUSES)
            }
            active_claim_counts[code] = claims['n']
            inputs[code] = {
                'plss': plss_stats,
                'active_claims': dict(claim_stats, unmapped_count=unmapped),
                'land_status': land_stats,
            }
    if total_sections <= 0:
        raise PublicationError('open-ground publication has no PLSS sections')
    if sum(state_counts.values()) != total_sections:
        raise PublicationError('national section-count invariant failed')
    ordered_feature_ids = sorted(seen_feature_ids)
    feature_ids_sha256 = _sha256_bytes(json.dumps(
        ordered_feature_ids, separators=(',', ':'),
        allow_nan=False).encode('utf-8'))
    return {
        'n': total_sections,
        'scope_states': list(codes),
        'states': state_counts,
        'state_status_counts': state_status_counts,
        'active_claims': active_claim_counts,
        'status_counts': {status: status_counts.get(status, 0)
                          for status in sorted(OUTPUT_STATUSES)},
        'open_count': total_open,
        'section_count': total_sections,
        'open_fraction': total_open / total_sections,
        'partial_states': sorted(partial_states),
        'inputs': inputs,
        'source_id_inventory': {
            'status': 'complete_at_derivation',
            'source_records': total_sections,
            'ids_sha256': feature_ids_sha256,
        },
    }


def assert_inputs_unchanged(context):
    """Rehash the inventory, clip, and all 57 inputs before publication."""
    try:
        with open(context['inventory_path'], 'rb') as source:
            inventory_raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot re-read staging inventory: {exc}') from exc
    if (inventory_raw != context['inventory_raw'] or
            _sha256_bytes(inventory_raw) != context['inventory_sha256']):
        raise PublicationError('staging inventory changed during build')
    if _sha256_file(context['state_clips_path']) != context['clip_sha256']:
        raise PublicationError('authoritative state clips changed during build')
    current_estates_sha = _sha256_bytes(json.dumps(
        context['mineral_estates'], sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode())
    if current_estates_sha != context['mineral_estates_sha256']:
        raise PublicationError(
            'mineral-estate registry snapshot changed during build')
    for path, digest in context['registry_hashes'].items():
        if _sha256_file(path) != digest:
            raise PublicationError(
                'state registry/defaults changed during open-ground build')
    for code in context['codes']:
        for kind in KINDS:
            path = context['paths'][(code, kind)]
            expected = context['integrity'][(code, kind)]
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                raise PublicationError(
                    f'{code}.{kind} changed/disappeared during build: {exc}') from exc
            digest = _sha256_file(path)
            if size != expected['bytes'] or digest != expected['sha256']:
                raise PublicationError(f'{code}.{kind} changed during build')


def validate_pmtiles(path, expected_states=None, expected_title_sources=None,
                     expected_source_inventory=None):
    """Fully decode PMTiles/MVT and enforce the open-ground layer contract."""
    expected_states = tuple(sorted(set(expected_states or ())))
    if (any(not isinstance(code, str) or code not in CLAIM_STATES
            for code in expected_states)):
        raise PublicationError('expected PMTiles states must be claim-state codes')
    try:
        checked = _strict_pmtiles_header(
            path, expected_layers=['open_ground'],
            required_properties={'open_ground': set(REQUIRED_TILE_FIELDS)},
            verify_feature_properties=True,
            collect_feature_ids=True,
            expected_state=(expected_states[0]
                            if len(expected_states) == 1 else None),
            expected_open_ground_title_sources=expected_title_sources)
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        raise PublicationError(f'invalid PMTiles archive: {exc}') from exc
    if set(checked['source_layers']) != {'open_ground'}:
        raise PublicationError('PMTiles must contain exactly open_ground')
    feature_count = checked['semantic_layer_counts'].get('open_ground', 0)
    if feature_count <= 0:
        raise PublicationError('PMTiles open_ground layer has no decoded features')
    tiled_ids = checked.get('maxzoom_feature_ids', {}).get('open_ground', [])
    if (not tiled_ids or any(not isinstance(value, int) or
                             isinstance(value, bool) or value <= 0
                             for value in tiled_ids) or
            len(tiled_ids) != len(set(tiled_ids))):
        raise PublicationError(
            'PMTiles open_ground max-zoom feature IDs are invalid')
    ids_sha256 = _sha256_bytes(json.dumps(
        sorted(tiled_ids), separators=(',', ':'),
        allow_nan=False).encode('utf-8'))
    maxzoom_instances = checked.get(
        'maxzoom_feature_instances', {}).get('open_ground', 0)
    source_id_inventory = {
        'status': 'complete_at_derivation',
        'source_records': len(tiled_ids),
        'maxzoom_feature_instances': maxzoom_instances,
        'maxzoom_unique_tiled_ids': len(tiled_ids),
        'ids_sha256': ids_sha256,
    }
    if expected_source_inventory is not None:
        expected = expected_source_inventory
        if (not isinstance(expected, dict) or
                expected.get('status') != 'complete_at_derivation' or
                not _is_int(expected.get('source_records'), 1) or
                not isinstance(expected.get('ids_sha256'), str) or
                SHA256_RE.fullmatch(expected['ids_sha256']) is None):
            raise PublicationError(
                'expected open-ground source-ID inventory is invalid')
        if (len(tiled_ids) != expected['source_records'] or
                ids_sha256 != expected['ids_sha256'] or
                maxzoom_instances < expected['source_records']):
            raise PublicationError(
                'PMTiles open-ground source-section ID reconciliation failed; '
                f'source_records={expected["source_records"]}, '
                f'unique_tiled_ids={len(tiled_ids)}, '
                f'maxzoom_instances={maxzoom_instances}')
    return {
        'bytes': checked['bytes'],
        'sha256': _sha256_file(path),
        'tile_entries': checked['tile_entries'],
        'tile_contents': checked['tile_contents'],
        'decoded_features': feature_count,
        'source_layers': ['open_ground'],
        'source_id_inventory': source_id_inventory,
    }


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _scope_slug(codes, all_codes):
    if tuple(codes) == tuple(all_codes):
        return 'national'
    if len(codes) == 1:
        return codes[0].lower()
    return '-'.join(code.lower() for code in codes)


def _artifact_key(codes, all_codes):
    if tuple(codes) == tuple(all_codes):
        return SYSTEM
    return f'{SYSTEM}_{"_".join(code.lower() for code in codes)}'


def install_immutable(source, publish_dir, scope_slug, expected_states,
                      expected_title_sources=None,
                      expected_source_inventory=None):
    """Install a checked content-addressed archive without overwriting it."""
    publish_dir = os.path.realpath(publish_dir)
    os.makedirs(publish_dir, exist_ok=True)
    source_meta = validate_pmtiles(
        source, expected_states, expected_title_sources,
        expected_source_inventory)
    name = (f'federal-open-ground-{scope_slug}-'
            f'{source_meta["sha256"][:20]}.pmtiles')
    destination = os.path.join(publish_dir, name)
    if os.path.exists(destination):
        existing = validate_pmtiles(
            destination, expected_states, expected_title_sources,
            expected_source_inventory)
        if existing['sha256'] != source_meta['sha256']:
            raise PublicationError(
                f'content-addressed destination collision at {name}')
        return destination, existing
    handle, pending = tempfile.mkstemp(
        prefix='.federal-open-ground-', suffix='.tmp', dir=publish_dir)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as output, open(source, 'rb') as archive:
            shutil.copyfileobj(archive, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        installed = validate_pmtiles(
            pending, expected_states, expected_title_sources,
            expected_source_inventory)
        if installed['sha256'] != source_meta['sha256']:
            raise PublicationError('archive changed while copying for publication')
        try:
            os.link(pending, destination)
        except FileExistsError:
            existing = validate_pmtiles(
                destination, expected_states, expected_title_sources,
                expected_source_inventory)
            if existing['sha256'] != source_meta['sha256']:
                raise PublicationError(
                    f'content-addressed destination collision at {name}')
            os.unlink(pending)
            return destination, existing
        os.unlink(pending)
        _fsync_directory(publish_dir)
        return destination, installed
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _latest_entry(context, stats, archive_path, archive_meta, latest_path,
                  profile):
    return {
        'system': SYSTEM,
        'format': 'pmtiles',
        'file': os.path.relpath(archive_path, os.path.dirname(latest_path)),
        'source_layers': ['open_ground'],
        'scope': {
            'kind': ('national' if tuple(stats['scope_states']) ==
                     tuple(context['codes']) else 'states'),
            'states': stats['scope_states'],
        },
        'n': stats['n'],
        'states': stats['states'],
        'active_claims': stats['active_claims'],
        'status_counts': stats['status_counts'],
        'state_status_counts': stats['state_status_counts'],
        'open_count': stats['open_count'],
        'section_count': stats['section_count'],
        'open_fraction': stats['open_fraction'],
        'partial_states': stats['partial_states'],
        'source_id_inventory': archive_meta['source_id_inventory'],
        'profile': profile,
        'inventory_created': context['inventory']['created'],
        'inventory_sha256': context['inventory_sha256'],
        'inputs': stats['inputs'],
        'sources': SOURCES,
        'mineral_estates': {
            code: context['mineral_estates'][code]
            for code in stats['scope_states']
        },
        'mineral_estates_sha256': context['mineral_estates_sha256'],
        'clip': {
            'authority': CLIP_AUTHORITY,
            'method': CLIP_METHOD,
            'artifact_sha256': context['clip_sha256'],
        },
        'caveats': {
            'title': TITLE_CAVEAT,
            'withdrawals': WITHDRAWAL_CAVEAT,
        },
        'bytes': archive_meta['bytes'],
        'sha256': archive_meta['sha256'],
    }


def merge_latest(latest_path, artifact_key, entry):
    """Merge under a lock while preserving the newest unrelated pointers."""
    latest_path = os.path.realpath(latest_path)
    directory = os.path.dirname(latest_path)
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, '.federal-open-ground-latest.lock')
    with open(lock_path, 'a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if os.path.exists(latest_path):
            latest, _ = _read_strict_json(
                latest_path, 'latest publication manifest')
        else:
            latest = {'schema_version': 1, 'artifacts': {}}
        if not isinstance(latest, dict) or latest.get('schema_version') != 1:
            raise PublicationError(
                'latest manifest must be a schema_version 1 object')
        artifacts = latest.get('artifacts')
        if not isinstance(artifacts, dict):
            raise PublicationError('latest manifest artifacts must be an object')
        artifacts[artifact_key] = entry
        mode = (stat.S_IMODE(os.stat(latest_path).st_mode)
                if os.path.exists(latest_path) else 0o644)
        handle, pending = tempfile.mkstemp(prefix='.latest-', dir=directory)
        try:
            os.fchmod(handle, mode)
            with os.fdopen(handle, 'w', encoding='utf-8') as output:
                json.dump(latest, output, separators=(',', ':'), allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, latest_path)
            _fsync_directory(directory)
        except Exception:
            try:
                os.unlink(pending)
            except FileNotFoundError:
                pass
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return latest


def build(staging_dir, inventory_path, publish_dir, *, latest_manifest=None,
          state_clips=DEFAULT_STATE_CLIPS, profile='release', states=None,
          tippecanoe='tippecanoe'):
    """Derive, tile, validate, install, and point at one checked generation."""
    if profile not in PROFILES:
        raise PublicationError(f'profile must be one of {PROFILES}')
    executable = shutil.which(tippecanoe)
    if not executable:
        raise PublicationError('tippecanoe >=2.79 with PMTiles output is required')
    context = load_inventory(staging_dir, inventory_path, state_clips)
    codes = _selected_codes(context, states)
    publish_dir = os.path.realpath(publish_dir)
    if not _outside(publish_dir, context['staging']):
        raise PublicationError('publish directory must be outside raw staging')
    default_pointer = ('latest.json' if profile in ('full', 'release')
                       else 'progress.json')
    latest_manifest = os.path.realpath(
        latest_manifest or os.path.join(publish_dir, default_pointer))
    if _outside(latest_manifest, publish_dir):
        raise PublicationError('latest manifest must remain inside publish directory')
    slug = _scope_slug(codes, context['codes'])
    with tempfile.TemporaryDirectory(prefix='nwmm-open-ground-') as temporary:
        sequence = os.path.join(temporary, 'open_ground.geojsonseq')
        pending_archive = os.path.join(temporary, 'open-ground.pmtiles')
        stats = stream_states(
            context, sequence, profile=profile, selected_states=codes)
        description = json.dumps({
            'schema': 'nwmm-federal-open-ground-v1',
            'inventory_sha256': context['inventory_sha256'],
            'features': stats['n'],
            'states': stats['scope_states'],
            'profile': profile,
        }, separators=(',', ':'))
        command = [
            executable, '--force', '--output', pending_archive,
            '--minimum-zoom=0', '--maximum-zoom=13',
            '--base-zoom=13', '--no-feature-limit', '--no-tile-size-limit',
            '--no-tiny-polygon-reduction-at-maximum-zoom',
            '--detect-shared-borders', '--read-parallel',
            '--preserve-input-order', '--quiet',
            '--use-attribute-for-id=fid', '--exclude=fid',
            f'--description={description}',
            '--attribution=U.S. Bureau of Land Management and U.S. Census Bureau',
            '-L', f'open_ground:{sequence}',
        ]
        subprocess.run(command, check=True)
        expected_title_sources = {
            code: (context['mineral_estates'][code].get('source_url')
                   if context['mineral_estates'][code].get('status') ==
                   'reviewed_ingested' else None)
            for code in codes
        }
        validate_pmtiles(
            pending_archive, codes, expected_title_sources,
            stats['source_id_inventory'])
        assert_inputs_unchanged(context)
        archive_path, archive_meta = install_immutable(
            pending_archive, publish_dir, slug, codes,
            expected_title_sources, stats['source_id_inventory'])
    assert_inputs_unchanged(context)
    entry = _latest_entry(
        context, stats, archive_path, archive_meta, latest_manifest, profile)
    key = _artifact_key(codes, context['codes'])
    merge_latest(latest_manifest, key, entry)
    return {
        'artifact': archive_path,
        'latest_manifest': latest_manifest,
        'artifact_key': key,
        'entry': entry,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=('Build immutable federal open-ground PMTiles from '
                     'private PLSS, active-claim, and land-status staging'))
    parser.add_argument('--staging-dir', required=True)
    parser.add_argument('--inventory', required=True,
                        help='checksum-pinned exact 19-state inventory JSON')
    parser.add_argument('--publish-dir', required=True)
    parser.add_argument('--latest-manifest')
    parser.add_argument('--state-clips', default=DEFAULT_STATE_CLIPS)
    parser.add_argument('--profile', choices=PROFILES, default='release')
    parser.add_argument('--state', action='append', dest='states',
                        help='claim state to publish; repeat, or omit for all 19')
    parser.add_argument('--tippecanoe', default='tippecanoe')
    args = parser.parse_args(argv)
    try:
        result = build(
            args.staging_dir, args.inventory, args.publish_dir,
            latest_manifest=args.latest_manifest,
            state_clips=args.state_clips, profile=args.profile,
            states=args.states, tippecanoe=args.tippecanoe)
    except (PublicationError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
