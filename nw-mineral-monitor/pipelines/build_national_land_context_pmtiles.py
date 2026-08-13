#!/usr/bin/env python3
"""Publish per-target land context for the exact 30 WS11 non-claim states.

Adapters freeze three private snapshots for every state: the ranked-target
output, surface-management polygons, and independent mineral-interest
polygons.  This builder does no portal scraping and never treats a surface
manager as evidence of mineral ownership.  It validates the complete 90-file
inventory, requires every ranked target to join unambiguously to one record
from each ownership domain, builds one two-layer PMTiles archive per state,
fully decodes every MVT payload, and installs only content-addressed files.

Raw JSON remains outside ``site/``.  The only mutable output is an atomically
replaced latest-generation index.  This module never edits the browser
manifest, state registry, coverage dashboard, or a release switch.
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
from datetime import datetime

from land_context import normalize_land_context
from state_registry import (
    ALL_STATES, NON_CLAIM_STATES, load_states, national_sources,
)


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
INFRA = os.path.join(ROOT, 'infra')
if INFRA not in sys.path:
    sys.path.insert(0, INFRA)

from spatial_clip import StateClipIndex
from validate_national import (
    _decompress_pmtiles,
    _directory_entries,
    _mvt_layers,
    _pmtiles_header,
)


SCHEMA_VERSION = 1
SYSTEM = 'national_nonclaim_land_context'
KINDS = ('ranked_targets', 'surface_ownership', 'mineral_interests')
SOURCE_LAYERS = ('land_context', 'target_context')
TOP_TARGET_COUNT = 5
SAFE_INTEGER_MAX = (1 << 53) - 1
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PROPERTY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,62}$')
METHOD_RE = re.compile(r'^ws11-target-score-v\d+$')

DEFAULT_STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')
REGISTRY_INPUTS = tuple(
    [os.path.join(ROOT, 'states', '_defaults.yaml')]
    + [os.path.join(ROOT, 'states', f'{code}.yaml')
       for code in sorted(ALL_STATES)]
    + [os.path.join(ROOT, 'pipelines', 'config', 'national_sources.json')]
)
CLIP_AUTHORITY = 'U.S. Census Bureau TIGERweb, January 1 2025 vintage'
CLIP_METHOD = (
    'Every input coordinate must be inside or on the boundary of the '
    'authoritative state polygon; adapters must pre-clip source geometry'
)
SURFACE_CLASSES = frozenset((
    'federal', 'state', 'state_trust', 'tribal', 'local', 'private',
    'mixed', 'unknown',
))
MINERAL_CLASSES = frozenset((
    'state', 'state_trust', 'private', 'federal_reserved',
    'federal_acquired', 'tribal', 'split_estate', 'mixed', 'unknown',
))
MINERAL_EVIDENCE_BASES = frozenset((
    'mineral_title_record', 'state_mineral_inventory',
    'private_title_research', 'federal_mineral_record', 'tribal_record',
    'unresolved',
))
CONFIDENCE_CLASSES = frozenset(('verified', 'probable', 'limited', 'unknown'))

REQUIRED_TILE_FIELDS = {
    'land_context': frozenset((
        'st', 'context_id', 'surface_record_id', 'mineral_interest_id',
        'surface_class', 'mineral_class', 'approach', 'provenance',
    )),
    'target_context': frozenset((
        'st', 'target_id', 'target_rank', 'score', 'surface_class',
        'mineral_class', 'approach', 'open_ground_status',
        'open_ground_display', 'provenance',
    )),
}


class PublicationError(ValueError):
    """A private-input, semantic, tiling, or publication invariant failed."""


def _reject_constant(value):
    raise PublicationError(f'non-standard JSON number {value}')


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def _read_strict_json(path, label=None):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot read {label or path}: {exc}') from exc
    try:
        value = json.loads(
            raw.decode('utf-8'), parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(
            f'{label or path} is not strict UTF-8 JSON: {exc}') from exc
    return value, raw


def _canonical_json(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PublicationError(f'cannot encode canonical JSON: {exc}') from exc


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


def _registry_generation_sha256():
    """Hash every file that can influence the merged publication contract."""
    digest = hashlib.sha256()
    for path in REGISTRY_INPUTS:
        relative = os.path.relpath(path, ROOT).encode('utf-8')
        digest.update(len(relative).to_bytes(4, 'big'))
        digest.update(relative)
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise PublicationError(
                f'cannot stat registry input {path}: {exc}') from exc
        digest.update(size.to_bytes(8, 'big'))
        try:
            with open(path, 'rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
        except OSError as exc:
            raise PublicationError(
                f'cannot hash registry input {path}: {exc}') from exc
    return digest.hexdigest()


def _outside(path, parent):
    path = os.path.realpath(path)
    parent = os.path.realpath(parent)
    try:
        return os.path.commonpath((path, parent)) != parent
    except ValueError:
        return True


def _is_int(value, minimum=0):
    return (isinstance(value, int) and not isinstance(value, bool) and
            value >= minimum)


def _finite(value, label, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PublicationError(f'{label} must be a finite number')
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise PublicationError(f'{label} must be a finite number >= {minimum}')
    return result


def _text(value, label, *, minimum=1, maximum=2048):
    if not isinstance(value, str):
        raise PublicationError(f'{label} must be text')
    value = value.strip()
    if len(value) < minimum or len(value) > maximum:
        raise PublicationError(
            f'{label} must contain {minimum}..{maximum} characters')
    return value


def _optional_text(value, label, *, maximum=2048):
    if value is None:
        return None
    return _text(value, label, maximum=maximum)


def _date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be an ISO YYYY-MM-DD date')
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError as exc:
        raise PublicationError(f'{label} is not a calendar date') from exc
    return value


def _https_urls(value, label):
    if (not isinstance(value, list) or not value or len(value) > 20 or
            len(value) != len(set(value))):
        raise PublicationError(f'{label} must be a nonempty unique URL array')
    for index, url in enumerate(value):
        if (not isinstance(url, str) or not url.startswith('https://') or
                any(character.isspace() for character in url)):
            raise PublicationError(f'{label}[{index}] must be an HTTPS URL')
    return list(value)


def _scalar(value, label):
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > 4000:
            raise PublicationError(f'{label} exceeds 4000 characters')
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not -SAFE_INTEGER_MAX <= value <= SAFE_INTEGER_MAX:
            raise PublicationError(f'{label} exceeds JavaScript-safe integer range')
        return value
    if isinstance(value, float):
        return _finite(value, label)
    raise PublicationError(f'{label} must be a scalar GeoJSON/MVT property')


def _registry_nonclaim_states():
    states = load_states()
    if set(states) != set(ALL_STATES):
        raise PublicationError('registry must contain the exact WS11 49 states')
    codes = {code for code, row in states.items()
             if row.get('regime') == 'non_claim'}
    if codes != set(NON_CLAIM_STATES) or len(codes) != 30:
        raise PublicationError(
            'registry non-claim regime must equal the canonical 30 states')
    for code in codes:
        row = states[code]
        if ((row.get('open_ground') or {}).get('applicable') is not False or
                (row.get('open_ground') or {}).get('display_when_missing') != 'N/A' or
                row.get('claim_systems')):
            raise PublicationError(
                f'{code} registry does not implement non-claim N/A semantics')
        context = row.get('land_context')
        if (not isinstance(context, dict) or
                set(context.get('source_layers') or []) != set(SOURCE_LAYERS)):
            raise PublicationError(
                f'{code} registry lacks the two-layer land-context contract')
    return states, tuple(sorted(codes))


def _safe_staging_paths(staging_dir, inventory_path):
    staging = os.path.realpath(staging_dir)
    inventory = os.path.realpath(inventory_path)
    if not os.path.isdir(staging):
        raise PublicationError(f'private staging directory is missing: {staging}')
    if not os.path.isfile(inventory):
        raise PublicationError(f'private inventory is missing: {inventory}')
    if not _outside(staging, SITE) or not _outside(inventory, SITE):
        raise PublicationError(
            'raw land-context staging and inventory must remain outside site/')
    if _outside(inventory, staging):
        raise PublicationError('inventory must remain inside its staging directory')
    return staging, inventory


def _snapshot_filename(code, kind):
    return f'{code.lower()}_{kind}.json'


def _validate_inventory_entry(code, kind, entry, staging):
    expected_name = _snapshot_filename(code, kind)
    required = {'file', 'n', 'bytes', 'sha256'}
    if not isinstance(entry, dict) or set(entry) != required:
        raise PublicationError(
            f'inventory {code}.{kind} keys must be exactly {sorted(required)}')
    if entry.get('file') != expected_name:
        raise PublicationError(
            f'inventory {code}.{kind}.file must be {expected_name!r}')
    if not _is_int(entry.get('n'), 1):
        raise PublicationError(f'inventory {code}.{kind}.n must be positive')
    if not _is_int(entry.get('bytes'), 2):
        raise PublicationError(f'inventory {code}.{kind}.bytes must be positive')
    if (not isinstance(entry.get('sha256'), str) or
            SHA256_RE.fullmatch(entry['sha256']) is None):
        raise PublicationError(f'inventory {code}.{kind}.sha256 is invalid')
    path = os.path.realpath(os.path.join(staging, expected_name))
    if _outside(path, staging):
        raise PublicationError(f'inventory {code}.{kind} escapes staging')
    if not os.path.isfile(path):
        raise PublicationError(f'inventory {code}.{kind} file is missing')
    size = os.path.getsize(path)
    digest = _sha256_file(path)
    if size != entry['bytes'] or digest != entry['sha256']:
        raise PublicationError(
            f'{code}.{kind} bytes/sha256 differ from inventory')
    return path, {'n': entry['n'], 'bytes': size, 'sha256': digest}


def _load_clip_indexes(path, expected_sha):
    path = os.path.realpath(path)
    actual_sha = _sha256_file(path)
    if actual_sha != expected_sha:
        raise PublicationError(
            'inventory clip artifact sha256 does not match --state-clips')
    clips, _ = _read_strict_json(path, 'authoritative state clips')
    if (not isinstance(clips, dict) or clips.get('schema_version') != 1 or
            not isinstance(clips.get('source'), str) or
            'TIGERweb' not in clips['source'] or
            'January 1 2025' not in clips['source'] or
            not isinstance(clips.get('states'), dict) or
            set(clips['states']) != set(ALL_STATES)):
        raise PublicationError(
            'state clips must be the exact 49-state Census 2025 index')
    indexes = {}
    geometries = {}
    for code in sorted(NON_CLAIM_STATES):
        geometry = clips['states'][code]
        if (not isinstance(geometry, dict) or
                geometry.get('type') not in ('Polygon', 'MultiPolygon')):
            raise PublicationError(f'authoritative {code} clip is not a polygon')
        indexes[code] = StateClipIndex(geometry)
        geometries[code] = geometry
    return indexes, geometries, actual_sha, path


def load_inventory(staging_dir, inventory_path,
                   state_clips_path=DEFAULT_STATE_CLIPS):
    """Eagerly checksum the exact 90-file, 30-state input generation."""
    staging, inventory_path = _safe_staging_paths(staging_dir, inventory_path)
    inventory, raw = _read_strict_json(
        inventory_path, 'national land-context staging inventory')
    required = {'schema_version', 'system', 'created', 'clip', 'states'}
    if not isinstance(inventory, dict) or set(inventory) != required:
        raise PublicationError(
            'inventory keys must be exactly schema_version/system/created/clip/states')
    if inventory.get('schema_version') != SCHEMA_VERSION:
        raise PublicationError(f'inventory schema_version must be {SCHEMA_VERSION}')
    if inventory.get('system') != SYSTEM:
        raise PublicationError(f'inventory system must be {SYSTEM!r}')
    _date(inventory.get('created'), 'inventory.created')
    clip = inventory.get('clip')
    if (not isinstance(clip, dict) or
            set(clip) != {'authority', 'method', 'artifact_sha256'} or
            clip.get('authority') != CLIP_AUTHORITY or
            clip.get('method') != CLIP_METHOD or
            not isinstance(clip.get('artifact_sha256'), str) or
            SHA256_RE.fullmatch(clip['artifact_sha256']) is None):
        raise PublicationError('inventory state-clip provenance is invalid')
    registry_before = _registry_generation_sha256()
    registry, codes = _registry_nonclaim_states()
    source_catalog = national_sources()
    registry_sha256 = _registry_generation_sha256()
    if registry_before != registry_sha256:
        raise PublicationError('state/source registry changed while loading')
    states = inventory.get('states')
    if not isinstance(states, dict) or set(states) != set(codes):
        missing = sorted(set(codes) - set(states or {}))
        extra = sorted(set(states or {}) - set(codes))
        raise PublicationError(
            f'inventory states must be exact registry 30; '
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
    indexes, clip_geometries, clip_sha, clip_path = _load_clip_indexes(
        state_clips_path, clip['artifact_sha256'])
    return {
        'staging': staging,
        'inventory_path': inventory_path,
        'inventory': inventory,
        'inventory_raw': raw,
        'inventory_sha256': _sha256_bytes(raw),
        'registry': registry,
        'source_catalog': source_catalog,
        'registry_sha256': registry_sha256,
        'codes': codes,
        'paths': paths,
        'integrity': integrity,
        'clip_indexes': indexes,
        'clip_geometries': clip_geometries,
        'clip_sha256': clip_sha,
        'state_clips_path': clip_path,
    }


def _pagination(value, n, label):
    required = {
        'method', 'expected_count', 'fetched_count', 'page_size',
        'page_offsets', 'page_row_counts', 'pagination_exhausted',
        'source_snapshot_id',
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PublicationError(
            f'{label} keys must be exactly {sorted(required)}')
    if value.get('method') not in ('offset', 'single_file'):
        raise PublicationError(f'{label}.method must be offset or single_file')
    if value.get('expected_count') != n or value.get('fetched_count') != n:
        raise PublicationError(
            f'{label} expected_count/fetched_count must equal inventory n')
    if not _is_int(value.get('page_size'), 1):
        raise PublicationError(f'{label}.page_size must be positive')
    if value.get('pagination_exhausted') is not True:
        raise PublicationError(f'{label}.pagination_exhausted must be true')
    size = value['page_size']
    offsets = value.get('page_offsets')
    row_counts = value.get('page_row_counts')
    expected_offsets = list(range(0, n, size))
    expected_counts = [min(size, n - offset) for offset in expected_offsets]
    if offsets != expected_offsets or row_counts != expected_counts:
        raise PublicationError(
            f'{label} page offsets/counts do not exhaust expected_count')
    if value['method'] == 'single_file' and (
            offsets != [0] or row_counts != [n]):
        raise PublicationError(
            f'{label} single_file acquisition must have one complete page')
    _text(value.get('source_snapshot_id'), f'{label}.source_snapshot_id',
          maximum=300)
    return value


def _point_on_segment(x, y, x1, y1, x2, y2, tolerance=1e-9):
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > tolerance * max(1.0, abs(x2 - x1), abs(y2 - y1)):
        return False
    return (min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance and
            min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance)


def _inside_or_boundary(index, x, y):
    if index.contains(x, y):
        return True
    return any(
        _point_on_segment(x, y, ring[position][0], ring[position][1],
                          ring[position + 1][0], ring[position + 1][1])
        for polygon in index.polygons for ring in polygon
        for position in range(len(ring) - 1))


def _position(value, label):
    if not isinstance(value, list) or len(value) < 2 or len(value) > 3:
        raise PublicationError(
            f'{label} must contain longitude/latitude and optional elevation')
    x = _finite(value[0], f'{label}.longitude')
    y = _finite(value[1], f'{label}.latitude')
    if not -180 <= x <= 180 or not -90 <= y <= 90:
        raise PublicationError(f'{label} is outside WGS84 coordinate bounds')
    if len(value) == 3:
        _finite(value[2], f'{label}.elevation')
    return x, y


def _geometry(value, label, *, expected):
    if not isinstance(value, dict) or set(value) != {'type', 'coordinates'}:
        raise PublicationError(f'{label} must be a strict GeoJSON geometry')
    kind = value.get('type')
    if kind not in expected:
        raise PublicationError(
            f'{label} geometry must be one of {sorted(expected)}')
    coordinates = value.get('coordinates')
    positions = []
    if kind == 'Point':
        positions = [_position(coordinates, f'{label}.coordinates')]
    else:
        polygons = [coordinates] if kind == 'Polygon' else coordinates
        if not isinstance(polygons, list) or not polygons:
            raise PublicationError(f'{label} has no polygons')
        for polygon_index, polygon in enumerate(polygons):
            if not isinstance(polygon, list) or not polygon:
                raise PublicationError(
                    f'{label} polygon {polygon_index} has no rings')
            for ring_index, ring in enumerate(polygon):
                if not isinstance(ring, list) or len(ring) < 4:
                    raise PublicationError(
                        f'{label} ring {polygon_index}.{ring_index} is too short')
                parsed = [
                    _position(point,
                              f'{label}.coordinates[{polygon_index}]'
                              f'[{ring_index}][{point_index}]')
                    for point_index, point in enumerate(ring)
                ]
                if parsed[0] != parsed[-1]:
                    raise PublicationError(
                        f'{label} ring {polygon_index}.{ring_index} is not closed')
                if len(set(parsed[:-1])) < 3:
                    raise PublicationError(
                        f'{label} ring {polygon_index}.{ring_index} is degenerate')
                twice_area = sum(
                    x1 * y2 - x2 * y1
                    for (x1, y1), (x2, y2) in zip(parsed, parsed[1:]))
                if abs(twice_area) <= 1e-12:
                    raise PublicationError(
                        f'{label} ring {polygon_index}.{ring_index} has zero area')
                positions.extend(parsed)
    return kind, positions


def _properties(value, label):
    if not isinstance(value, dict) or not value or len(value) > 64:
        raise PublicationError(f'{label} must contain 1..64 fields')
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or PROPERTY_RE.fullmatch(key) is None:
            raise PublicationError(f'{label} property key {key!r} is invalid')
        result[key] = _scalar(item, f'{label}.{key}')
    return result


def _strict_feature(value, label, expected_geometry):
    if (not isinstance(value, dict) or
            set(value) != {'type', 'id', 'properties', 'geometry'} or
            value.get('type') != 'Feature'):
        raise PublicationError(f'{label} must be a strict GeoJSON Feature')
    properties = _properties(value.get('properties'), f'{label}.properties')
    record_id = value.get('id')
    if (isinstance(record_id, bool) or
            not isinstance(record_id, (str, int))):
        raise PublicationError(f'{label}.id must be text or an integer')
    identity = _text(str(record_id), f'{label}.id', maximum=180)
    geometry_type, positions = _geometry(
        value.get('geometry'), f'{label}.geometry', expected=expected_geometry)
    return identity, properties, geometry_type, positions


def _source_snapshot(context, code, kind):
    entry = context['inventory']['states'][code][kind]
    path = context['paths'][(code, kind)]
    data, raw = _read_strict_json(path, f'{code}.{kind} snapshot')
    digest = _sha256_bytes(raw)
    if len(raw) != entry['bytes'] or digest != entry['sha256']:
        raise PublicationError(f'{code}.{kind} changed after inventory load')
    common = {
        'schema_version', 'state', 'kind', 'source_ids',
        'official_source_urls', 'retrieved', 'complete', 'truncated',
        'pagination', 'type', 'features',
    }
    extra = ({'method_id', 'input_sha256s', 'top_target_count'}
             if kind == 'ranked_targets'
             else {'evidence_domain', 'coverage_status'})
    if not isinstance(data, dict) or set(data) != common | extra:
        raise PublicationError(
            f'{code}.{kind} snapshot keys must be exactly '
            f'{sorted(common | extra)}')
    if (data.get('schema_version') != SCHEMA_VERSION or
            data.get('state') != code or data.get('kind') != kind or
            data.get('complete') is not True or data.get('truncated') is not False or
            data.get('type') != 'FeatureCollection' or
            not isinstance(data.get('features'), list)):
        raise PublicationError(
            f'{code}.{kind} identity/completeness/FeatureCollection is invalid')
    if len(data['features']) != entry['n']:
        raise PublicationError(f'{code}.{kind} feature count differs from inventory')
    source_ids = data.get('source_ids')
    if (not isinstance(source_ids, list) or not source_ids or
            len(source_ids) != len(set(source_ids)) or
            any(not isinstance(value, str) or
                re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,179}', value) is None
                for value in source_ids)):
        raise PublicationError(f'{code}.{kind}.source_ids are invalid')
    data['official_source_urls'] = _https_urls(
        data.get('official_source_urls'),
        f'{code}.{kind}.official_source_urls')
    _date(data.get('retrieved'), f'{code}.{kind}.retrieved')
    _pagination(data.get('pagination'), entry['n'],
                f'{code}.{kind}.pagination')
    if kind == 'ranked_targets':
        if (not isinstance(data.get('method_id'), str) or
                METHOD_RE.fullmatch(data['method_id']) is None):
            raise PublicationError(
                f'{code}.ranked_targets.method_id is not versioned')
        hashes = data.get('input_sha256s')
        if (not isinstance(hashes, dict) or
                not {'grades', 'geology', 'land_context'} <= set(hashes) or
                any(not isinstance(key, str) or not key or
                    not isinstance(value, str) or SHA256_RE.fullmatch(value) is None
                    for key, value in hashes.items())):
            raise PublicationError(
                f'{code}.ranked_targets.input_sha256s are invalid')
        if data.get('top_target_count') != TOP_TARGET_COUNT:
            raise PublicationError(
                f'{code}.ranked_targets.top_target_count must be {TOP_TARGET_COUNT}')
    else:
        expected_domain = ('surface_management' if kind == 'surface_ownership'
                           else 'mineral_interest')
        if (data.get('evidence_domain') != expected_domain or
                data.get('coverage_status') != 'statewide_complete'):
            raise PublicationError(
                f'{code}.{kind} needs independent {expected_domain} evidence '
                'with statewide_complete coverage')
    if kind == 'surface_ownership':
        catalog = context['source_catalog']
        for source_id in data['source_ids']:
            source_url = (catalog.get(source_id) or {}).get('url')
            if source_url and source_url not in data['official_source_urls']:
                raise PublicationError(
                    f'{code}.surface_ownership does not cite the configured '
                    f'official URL for {source_id}')
    return data, digest


def _feature_id(code, layer, identity):
    seed = f'{SYSTEM}\x1f{code}\x1f{layer}\x1f{identity}'.encode('utf-8')
    digest = hashlib.blake2b(
        seed, digest_size=8, person=b'nwmm-lctx-v1').digest()
    return (int.from_bytes(digest, 'big') & SAFE_INTEGER_MAX) or 1


def _clip_positions(context, code, positions, label):
    index = context['clip_indexes'][code]
    for x, y in positions:
        if not _inside_or_boundary(index, x, y):
            raise PublicationError(
                f'{label} contains a coordinate outside authoritative {code}; '
                'adapter must pre-clip before inventory freeze')


def _clip_geometry(context, code, geometry, positions, label):
    _clip_positions(context, code, positions, label)
    if not _geometry_contains_geometry(
            context['clip_geometries'][code], geometry):
        raise PublicationError(
            f'{label} crosses outside authoritative {code}; adapter must '
            'pre-clip edges and holes before inventory freeze')


def _polygon_rings(geometry):
    polygons = (geometry['coordinates'] if geometry['type'] == 'MultiPolygon'
                else [geometry['coordinates']])
    for polygon in polygons:
        yield polygon


def _geometry_bbox(geometry):
    _, positions = _geometry(
        geometry, 'indexed geometry', expected={'Polygon', 'MultiPolygon'})
    xs = [position[0] for position in positions]
    ys = [position[1] for position in positions]
    return min(xs), min(ys), max(xs), max(ys)


def _ring_contains_or_boundary(ring, x, y):
    inside = False
    for first, second in zip(ring, ring[1:]):
        x1, y1 = first[:2]
        x2, y2 = second[:2]
        if _point_on_segment(x, y, x1, y1, x2, y2):
            return True, True
        if ((y1 > y) != (y2 > y) and
                x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
    return inside, False


def _ring_interior_point(ring):
    """Return a deterministic point strictly inside a valid simple ring."""
    ys = sorted({float(point[1]) for point in ring[:-1]})
    candidates = []
    for first, second in zip(ys, ys[1:]):
        if second > first:
            candidates.append((first + second) / 2)
    if ys:
        candidates.append((ys[0] + ys[-1]) / 2)
    for y in candidates:
        intersections = []
        for first, second in zip(ring, ring[1:]):
            x1, y1 = first[:2]
            x2, y2 = second[:2]
            if (y1 > y) != (y2 > y):
                intersections.append(
                    x1 + (x2 - x1) * (y - y1) / (y2 - y1))
        intersections.sort()
        for left, right in zip(intersections[::2], intersections[1::2]):
            if right <= left:
                continue
            point = ((left + right) / 2, y)
            inside, boundary = _ring_contains_or_boundary(ring, *point)
            if inside and not boundary:
                return point
    raise PublicationError('cannot find an interior point for a valid polygon ring')


def _covers_geometry(geometry, point):
    x, y = point
    for polygon in _polygon_rings(geometry):
        parity = False
        for ring in polygon:
            inside, boundary = _ring_contains_or_boundary(ring, x, y)
            if boundary:
                return True
            parity ^= inside
        if parity:
            return True
    return False


def _proper_segment_intersection(a, b, c, d, tolerance=1e-12):
    def orientation(p, q, r):
        return ((q[0] - p[0]) * (r[1] - p[1]) -
                (q[1] - p[1]) * (r[0] - p[0]))
    first = orientation(a, b, c)
    second = orientation(a, b, d)
    third = orientation(c, d, a)
    fourth = orientation(c, d, b)
    return ((first > tolerance and second < -tolerance or
             first < -tolerance and second > tolerance) and
            (third > tolerance and fourth < -tolerance or
             third < -tolerance and fourth > tolerance))


def _geometry_contains_geometry(container, subject):
    """Prove polygon containment, including concavities and interior holes."""
    container_segments = [
        (tuple(first[:2]), tuple(second[:2]))
        for polygon in _polygon_rings(container) for ring in polygon
        for first, second in zip(ring, ring[1:])
    ]
    for polygon in _polygon_rings(subject):
        for ring in polygon:
            for first, second in zip(ring, ring[1:]):
                a, b = tuple(first[:2]), tuple(second[:2])
                if (not _covers_geometry(container, a) or
                        not _covers_geometry(container, b)):
                    return False
                # A segment with contained endpoints may still cross a
                # concavity or a hole. Proper boundary crossings prove that
                # some of the segment leaves the mineral-interest polygon.
                if any(_proper_segment_intersection(a, b, c, d)
                       for c, d in container_segments):
                    return False
                for fraction in (0.25, 0.5, 0.75):
                    sample = (a[0] + (b[0] - a[0]) * fraction,
                              a[1] + (b[1] - a[1]) * fraction)
                    if not _covers_geometry(container, sample):
                        return False
    # Subject edges can surround a container hole without crossing it. Probe
    # each hole interior so excluded land/water is not silently filled.
    for polygon in _polygon_rings(container):
        for hole in polygon[1:]:
            if _covers_geometry(subject, _ring_interior_point(hole)):
                return False
    return True


def _load_surface(context, code, data, digest):
    configured = set((context['registry'][code].get('land_context') or {}).get(
        'source_ids') or [])
    if not set(data['source_ids']) <= configured:
        raise PublicationError(
            f'{code}.surface_ownership source_ids are not declared in registry')
    rows = {}
    fids = set()
    for index, feature in enumerate(data['features']):
        label = f'{code}.surface_ownership.features[{index}]'
        identity, properties, _, positions = _strict_feature(
            feature, label, {'Polygon', 'MultiPolygon'})
        if properties.get('record_id') != identity:
            raise PublicationError(f'{label}.record_id must exactly match Feature.id')
        surface_class = properties.get('surface_class')
        if surface_class not in SURFACE_CLASSES:
            raise PublicationError(f'{label}.surface_class is invalid')
        source_id = properties.get('source_id')
        if source_id not in data['source_ids']:
            raise PublicationError(f'{label}.source_id is not declared by snapshot')
        mineral_id = _text(properties.get('mineral_interest_id'),
                           f'{label}.mineral_interest_id', maximum=180)
        manager = _optional_text(properties.get('surface_manager'),
                                 f'{label}.surface_manager', maximum=300)
        source_scale = _text(properties.get('source_scale'),
                             f'{label}.source_scale', maximum=180)
        _clip_geometry(context, code, feature['geometry'], positions, label)
        fid = _feature_id(code, 'land_context', identity)
        if identity in rows or fid in fids:
            raise PublicationError(
                f'{code}.surface_ownership duplicates identity/stable feature id')
        rows[identity] = {
            'record_id': identity,
            'surface_class': surface_class,
            'surface_manager': manager,
            'source_id': source_id,
            'source_scale': source_scale,
            'source_ref': _optional_text(
                properties.get('source_ref'), f'{label}.source_ref'),
            'as_of': _optional_text(properties.get('as_of'), f'{label}.as_of',
                                    maximum=100),
            'mineral_interest_id': mineral_id,
            'geometry': feature['geometry'],
            'bbox': _geometry_bbox(feature['geometry']),
            'fid': fid,
            'input_sha256': digest,
        }
        fids.add(fid)
    return rows


def _load_minerals(context, code, data, digest):
    rows = {}
    fids = set()
    for index, feature in enumerate(data['features']):
        label = f'{code}.mineral_interests.features[{index}]'
        identity, properties, _, positions = _strict_feature(
            feature, label, {'Polygon', 'MultiPolygon'})
        if properties.get('record_id') != identity:
            raise PublicationError(f'{label}.record_id must exactly match Feature.id')
        mineral_class = properties.get('mineral_class')
        if mineral_class not in MINERAL_CLASSES:
            raise PublicationError(f'{label}.mineral_class is invalid')
        source_id = properties.get('source_id')
        if source_id not in data['source_ids']:
            raise PublicationError(f'{label}.source_id is not declared by snapshot')
        basis = properties.get('evidence_basis')
        if basis not in MINERAL_EVIDENCE_BASES:
            raise PublicationError(
                f'{label}.evidence_basis must be mineral-interest evidence, '
                'never surface management')
        if ((basis == 'unresolved') is not (mineral_class == 'unknown')):
            raise PublicationError(
                f'{label} unresolved evidence and unknown mineral class must agree')
        confidence = properties.get('confidence')
        if confidence not in CONFIDENCE_CLASSES:
            raise PublicationError(f'{label}.confidence is invalid')
        note = _text(properties.get('note'), f'{label}.note',
                     minimum=20, maximum=1500)
        _clip_geometry(context, code, feature['geometry'], positions, label)
        fid = _feature_id(code, 'mineral_interests', identity)
        if identity in rows or fid in fids:
            raise PublicationError(
                f'{code}.mineral_interests duplicates identity/stable feature id')
        rows[identity] = {
            'record_id': identity,
            'mineral_class': mineral_class,
            'confidence': confidence,
            'evidence_basis': basis,
            'source_id': source_id,
            'source_ref': _optional_text(
                properties.get('source_ref'), f'{label}.source_ref'),
            'note': note,
            'geometry': feature['geometry'],
            'bbox': _geometry_bbox(feature['geometry']),
            'fid': fid,
            'input_sha256': digest,
        }
        fids.add(fid)
    return rows


def _context_input_sha256(surface_sha256, mineral_sha256):
    return hashlib.sha256(
        f'{surface_sha256}:{mineral_sha256}'.encode('ascii')).hexdigest()


def _load_targets(context, code, data, digest, expected_context_sha256):
    if data['input_sha256s'].get('land_context') != expected_context_sha256:
        raise PublicationError(
            f'{code}.ranked_targets was not scored from the staged '
            'surface/mineral input generation')
    if 'open_ground' in data['input_sha256s']:
        raise PublicationError(
            f'{code}.ranked_targets must not hash a claim/open-ground input '
            'for a non-claim state')
    required = {
        'target_id', 'target_rank', 'score', 'score_grade', 'score_geology',
        'open_ground_status', 'open_ground_value', 'open_ground_display',
        'surface_record_id', 'mineral_interest_id', 'surface_class',
        'mineral_class', 'approach', 'source_id',
    }
    rows = {}
    ranks = set()
    fids = set()
    for index, feature in enumerate(data['features']):
        label = f'{code}.ranked_targets.features[{index}]'
        identity, properties, _, positions = _strict_feature(
            feature, label, {'Point'})
        missing = required - set(properties)
        if missing:
            raise PublicationError(f'{label} lacks required fields {sorted(missing)}')
        if properties.get('target_id') != identity:
            raise PublicationError(f'{label}.target_id must exactly match Feature.id')
        if properties.get('source_id') not in data['source_ids']:
            raise PublicationError(f'{label}.source_id is not declared by snapshot')
        rank = properties.get('target_rank')
        if not _is_int(rank, 1):
            raise PublicationError(f'{label}.target_rank must be a positive integer')
        score = _finite(properties.get('score'), f'{label}.score', minimum=0)
        grade = _finite(properties.get('score_grade'),
                        f'{label}.score_grade', minimum=0)
        geology = _finite(properties.get('score_geology'),
                          f'{label}.score_geology', minimum=0)
        if not math.isclose(score, grade + geology, rel_tol=1e-9, abs_tol=1e-6):
            raise PublicationError(
                f'{label}.score must equal grade + geology with no numeric '
                'open-ground term')
        if (properties.get('open_ground_status') != 'not_applicable' or
                properties.get('open_ground_display') != 'N/A' or
                properties.get('open_ground_value') is not None):
            raise PublicationError(
                f'{label} must preserve open ground as not_applicable/N/A/null, '
                'never numeric zero')
        forbidden = {
            key for key in properties
            if key in ('score_open_ground', 'open_ground_score',
                       'open_ground_fraction', 'rich_open')
        }
        if forbidden:
            raise PublicationError(
                f'{label} contains numeric/claim scoring fields {sorted(forbidden)}')
        surface_id = _text(properties.get('surface_record_id'),
                           f'{label}.surface_record_id', maximum=180)
        mineral_id = _text(properties.get('mineral_interest_id'),
                           f'{label}.mineral_interest_id', maximum=180)
        if properties.get('surface_class') not in SURFACE_CLASSES:
            raise PublicationError(f'{label}.surface_class is invalid')
        if properties.get('mineral_class') not in MINERAL_CLASSES:
            raise PublicationError(f'{label}.mineral_class is invalid')
        _text(properties.get('approach'), f'{label}.approach', maximum=80)
        point = positions[0]
        _clip_positions(context, code, positions, label)
        fid = _feature_id(code, 'target_context', identity)
        if identity in rows or rank in ranks or fid in fids:
            raise PublicationError(
                f'{code}.ranked_targets duplicates target/rank/stable feature id')
        rows[identity] = {
            'target_id': identity,
            'target_rank': rank,
            'score': score,
            'score_grade': grade,
            'score_geology': geology,
            'surface_record_id': surface_id,
            'mineral_interest_id': mineral_id,
            'staged_surface_class': properties['surface_class'],
            'staged_mineral_class': properties['mineral_class'],
            'staged_approach': properties['approach'],
            'name': _optional_text(properties.get('name'), f'{label}.name',
                                   maximum=300),
            'source_id': properties['source_id'],
            'point': point,
            'geometry': feature['geometry'],
            'fid': fid,
            'input_sha256': digest,
        }
        ranks.add(rank)
        fids.add(fid)
    if len(rows) < TOP_TARGET_COUNT:
        raise PublicationError(
            f'{code}.ranked_targets needs at least {TOP_TARGET_COUNT} targets')
    expected_ranks = set(range(1, len(rows) + 1))
    if ranks != expected_ranks:
        raise PublicationError(
            f'{code}.ranked_targets ranks must be contiguous 1..{len(rows)}')
    ranked = sorted(rows.values(), key=lambda row: row['target_rank'])
    deterministic = sorted(
        rows.values(), key=lambda row: (-row['score'], row['target_id']))
    if [row['target_id'] for row in ranked] != [
            row['target_id'] for row in deterministic]:
        raise PublicationError(
            f'{code}.ranked_targets does not have deterministic N/A-aware rank order')
    return rows


def _bbox_contains(bbox, point, tolerance=1e-10):
    return (bbox[0] - tolerance <= point[0] <= bbox[2] + tolerance and
            bbox[1] - tolerance <= point[1] <= bbox[3] + tolerance)


def _normalize_state(context, code):
    """Join independent evidence and return canonical two-layer features."""
    snapshots = {}
    digests = {}
    for kind in KINDS:
        snapshots[kind], digests[kind] = _source_snapshot(context, code, kind)
    surfaces = _load_surface(
        context, code, snapshots['surface_ownership'],
        digests['surface_ownership'])
    minerals = _load_minerals(
        context, code, snapshots['mineral_interests'],
        digests['mineral_interests'])
    context_sha = _context_input_sha256(
        digests['surface_ownership'], digests['mineral_interests'])
    targets = _load_targets(
        context, code, snapshots['ranked_targets'],
        digests['ranked_targets'], context_sha)

    missing_minerals = {
        row['mineral_interest_id'] for row in surfaces.values()
        if row['mineral_interest_id'] not in minerals
    }
    if missing_minerals:
        raise PublicationError(
            f'{code}.surface_ownership references missing mineral records '
            f'{sorted(missing_minerals)[:5]}')
    referenced_minerals = {
        row['mineral_interest_id'] for row in surfaces.values()
    }
    if referenced_minerals != set(minerals):
        raise PublicationError(
            f'{code} surface/mineral statewide join silently drops mineral records; '
            f'unused={sorted(set(minerals) - referenced_minerals)[:5]}')

    context_features = []
    context_properties = {}
    for surface_id, surface in sorted(surfaces.items()):
        mineral = minerals[surface['mineral_interest_id']]
        if not _geometry_contains_geometry(
                mineral['geometry'], surface['geometry']):
            raise PublicationError(
                f'{code} surface polygon {surface_id!r} is not pre-partitioned '
                f'inside mineral-interest polygon {mineral["record_id"]!r}')
        card = normalize_land_context({
            'class': surface['surface_class'],
            'manager': surface['surface_manager'],
            'source': surface['source_ref'] or surface['source_id'],
            'scale': surface['source_scale'],
            'as_of': surface['as_of'],
        }, context['registry'][code], {
            'class': mineral['mineral_class'],
            'confidence': mineral['confidence'],
            'source': mineral['source_ref'] or mineral['source_id'],
            'note': mineral['note'],
        })
        if (card.get('regime') != 'non_claim' or
                card.get('open_ground') != 'not_applicable' or
                card['mineral_ownership']['class'] != mineral['mineral_class']):
            raise PublicationError(
                f'{code} common land-context normalizer violated regime/evidence')
        approach = card['approach']
        provenance = (
            f'surface:{surface["source_id"]}:{surface["input_sha256"][:16]}|'
            f'mineral:{mineral["source_id"]}:{mineral["input_sha256"][:16]}')
        properties = {
            'st': code,
            'context_id': surface_id,
            'surface_record_id': surface_id,
            'mineral_interest_id': mineral['record_id'],
            'surface_class': surface['surface_class'],
            'surface_manager': surface['surface_manager'],
            'surface_source_id': surface['source_id'],
            'surface_source_scale': surface['source_scale'],
            'mineral_class': mineral['mineral_class'],
            'ownership_confidence': mineral['confidence'],
            'mineral_evidence_basis': mineral['evidence_basis'],
            'mineral_source_id': mineral['source_id'],
            'approach': approach['kind'],
            'approach_agency': approach.get('agency'),
            'approach_url': approach.get('url'),
            'note': approach.get('note'),
            'ownership_note': mineral['note'],
            'open_ground_status': 'not_applicable',
            'open_ground_display': 'N/A',
            'provenance': provenance,
        }
        context_properties[surface_id] = properties
        context_features.append({
            'type': 'Feature',
            'id': surface['fid'],
            'properties': {'fid': surface['fid'], **properties},
            'geometry': surface['geometry'],
        })

    target_features = []
    for target in sorted(targets.values(), key=lambda row: row['target_rank']):
        surface_hits = [
            identity for identity, row in surfaces.items()
            if _bbox_contains(row['bbox'], target['point']) and
            _covers_geometry(row['geometry'], target['point'])
        ]
        mineral_hits = [
            identity for identity, row in minerals.items()
            if _bbox_contains(row['bbox'], target['point']) and
            _covers_geometry(row['geometry'], target['point'])
        ]
        if surface_hits != [target['surface_record_id']]:
            raise PublicationError(
                f'{code} target {target["target_id"]!r} does not join exactly '
                f'to declared surface record; hits={sorted(surface_hits)[:5]}')
        if mineral_hits != [target['mineral_interest_id']]:
            raise PublicationError(
                f'{code} target {target["target_id"]!r} does not join exactly '
                f'to declared mineral record; hits={sorted(mineral_hits)[:5]}')
        context_row = context_properties[target['surface_record_id']]
        if context_row['mineral_interest_id'] != target['mineral_interest_id']:
            raise PublicationError(
                f'{code} target {target["target_id"]!r} surface/mineral '
                'foreign-key pair disagrees with statewide context')
        if (target['staged_surface_class'] != context_row['surface_class'] or
                target['staged_mineral_class'] != context_row['mineral_class'] or
                target['staged_approach'] != context_row['approach']):
            raise PublicationError(
                f'{code} target {target["target_id"]!r} scoring land context '
                'does not match the independently staged ownership evidence')
        properties = {
            'st': code,
            'target_id': target['target_id'],
            'target_rank': target['target_rank'],
            'score': target['score'],
            'score_grade': target['score_grade'],
            'score_geology': target['score_geology'],
            'name': target['name'],
            'context_id': target['surface_record_id'],
            'surface_record_id': target['surface_record_id'],
            'mineral_interest_id': target['mineral_interest_id'],
            'surface_class': context_row['surface_class'],
            'surface_manager': context_row['surface_manager'],
            'mineral_class': context_row['mineral_class'],
            'ownership_confidence': context_row['ownership_confidence'],
            'approach': context_row['approach'],
            'approach_agency': context_row['approach_agency'],
            'open_ground_status': 'not_applicable',
            'open_ground_display': 'N/A',
            'provenance': (
                f'ranking:{target["source_id"]}:{target["input_sha256"][:16]}|'
                f'{context_row["provenance"]}'),
        }
        # There deliberately is no numeric open_ground_value/score property.
        target_features.append({
            'type': 'Feature',
            'id': target['fid'],
            'properties': {'fid': target['fid'], **properties},
            'geometry': target['geometry'],
        })
    return {
        'snapshots': snapshots,
        'input_sha256s': digests,
        'context_input_sha256': context_sha,
        'context_features': context_features,
        'target_features': target_features,
        'context_properties': context_properties,
        'targets': targets,
        'top_target_ids': [
            row['target_id'] for row in
            sorted(targets.values(), key=lambda row: row['target_rank'])[
                :TOP_TARGET_COUNT]],
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
    codes = tuple(sorted({code.strip().upper() for code in selected_states}))
    if not codes:
        raise PublicationError('at least one --state is required when filtering')
    invalid = set(codes) - set(context['codes'])
    if invalid:
        raise PublicationError(
            f'land-context scope contains claim/unsupported states '
            f'{sorted(invalid)}')
    return codes


def _write_sequence(path, features):
    with open(path, 'w', encoding='utf-8') as output:
        for feature in features:
            json.dump(feature, output, separators=(',', ':'), allow_nan=False)
            output.write('\n')


def _tippecanoe_executable(command):
    executable = shutil.which(command)
    if not executable:
        raise PublicationError(
            'tippecanoe >=2.79 with PMTiles output is required')
    try:
        result = subprocess.run(
            [executable, '--version'], check=True, capture_output=True,
            text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicationError(f'cannot execute tippecanoe: {exc}') from exc
    version_text = f'{result.stdout}\n{result.stderr}'.strip()
    match = re.search(r'\bv(\d+)\.(\d+)(?:\.(\d+))?', version_text)
    version = tuple(int(value or 0) for value in match.groups()) if match else None
    if version is None or version < (2, 79, 0):
        raise PublicationError(
            f'tippecanoe >=2.79 is required, got {version_text!r}')
    return executable, '.'.join(map(str, version))


def _all_tile_entries(path):
    with open(path, 'rb') as source:
        header = source.read(127)
        if len(header) != 127:
            raise PublicationError('PMTiles header is truncated')
        (root_offset, root_length, _metadata_offset, _metadata_length,
         leaf_offset, leaf_length, tile_offset, tile_length,
         _addressed, _entries, _contents) = struct.unpack_from('<11Q', header, 8)
        internal_compression, tile_compression = header[97], header[98]
        source.seek(root_offset)
        root = _directory_entries(_decompress_pmtiles(
            source.read(root_length), internal_compression))
        entries = []
        for tile_id, run_length, length, offset in root:
            if run_length:
                entries.append((tile_id, run_length, length, offset))
                continue
            if not leaf_length or offset + length > leaf_length:
                raise PublicationError('PMTiles leaf pointer is invalid')
            source.seek(leaf_offset + offset)
            leaf = _directory_entries(_decompress_pmtiles(
                source.read(length), internal_compression))
            if any(item[1] == 0 for item in leaf):
                raise PublicationError('nested PMTiles leaf pointer is invalid')
            entries.extend(leaf)
    return sorted(entries), tile_offset, tile_length, tile_compression


def _same_number(actual, expected):
    return (isinstance(actual, (int, float)) and not isinstance(actual, bool) and
            math.isfinite(actual) and
            math.isclose(float(actual), float(expected),
                         rel_tol=1e-9, abs_tol=1e-6))


def _semantic_pmtiles(path, code, normalized):
    entries, tile_offset, tile_length, compression = _all_tile_entries(path)
    expected_context = normalized['context_properties']
    expected_targets = normalized['targets']
    seen = {layer: set() for layer in SOURCE_LAYERS}
    decoded = {layer: 0 for layer in SOURCE_LAYERS}
    ranges = sorted({(offset, length) for _, _, length, offset in entries})
    with open(path, 'rb') as source:
        for offset, length in ranges:
            if offset + length > tile_length:
                raise PublicationError('PMTiles tile entry exceeds tile data')
            source.seek(tile_offset + offset)
            raw = source.read(length)
            if len(raw) != length:
                raise PublicationError('PMTiles tile payload is truncated')
            try:
                layers = _mvt_layers(
                    _decompress_pmtiles(raw, compression), semantic=True)
            except (ValueError, zlib.error) as exc:
                raise PublicationError(f'invalid MVT payload: {exc}') from exc
            for layer in layers:
                name = layer['name']
                if name not in SOURCE_LAYERS:
                    raise PublicationError(
                        f'PMTiles contains unexpected source layer {name!r}')
                for feature in layer['features']:
                    decoded[name] += 1
                    properties = feature['properties']
                    missing = REQUIRED_TILE_FIELDS[name] - set(properties)
                    if missing:
                        raise PublicationError(
                            f'{name} MVT feature lacks {sorted(missing)}')
                    if properties.get('st') != code:
                        raise PublicationError(
                            f'{name} MVT feature state is not {code}')
                    expected_geometry = 3 if name == 'land_context' else 1
                    if feature['geometry_type'] != expected_geometry:
                        raise PublicationError(
                            f'{name} MVT feature has wrong geometry type')
                    if name == 'land_context':
                        identity = properties.get('context_id')
                        expected = expected_context.get(identity)
                        if expected is None:
                            raise PublicationError(
                                f'land_context MVT has unknown context_id {identity!r}')
                        for key in (
                                'surface_record_id', 'mineral_interest_id',
                                'surface_class', 'mineral_class', 'approach',
                                'provenance'):
                            if properties.get(key) != expected.get(key):
                                raise PublicationError(
                                    f'land_context MVT {identity!r} changed {key}')
                    else:
                        identity = properties.get('target_id')
                        expected = expected_targets.get(identity)
                        if expected is None:
                            raise PublicationError(
                                f'target_context MVT has unknown target_id {identity!r}')
                        if (properties.get('target_rank') != expected['target_rank'] or
                                not _same_number(properties.get('score'),
                                                 expected['score']) or
                                properties.get('surface_record_id') !=
                                expected['surface_record_id'] or
                                properties.get('mineral_interest_id') !=
                                expected['mineral_interest_id'] or
                                properties.get('surface_class') !=
                                expected['staged_surface_class'] or
                                properties.get('mineral_class') !=
                                expected['staged_mineral_class'] or
                                properties.get('approach') !=
                                expected['staged_approach'] or
                                properties.get('open_ground_status') !=
                                'not_applicable' or
                                properties.get('open_ground_display') != 'N/A'):
                            raise PublicationError(
                                f'target_context MVT {identity!r} changed '
                                'rank/score/join/N/A semantics')
                        numeric_open = [
                            key for key, value in properties.items()
                            if key not in ('open_ground_status',
                                           'open_ground_display') and
                            'open_ground' in key and
                            isinstance(value, (int, float)) and
                            not isinstance(value, bool)
                        ]
                        if numeric_open:
                            raise PublicationError(
                                f'target_context MVT encodes N/A as numeric '
                                f'{sorted(numeric_open)}')
                    _text(properties.get('provenance'),
                          f'{name} MVT provenance', maximum=1000)
                    seen[name].add(identity)
    expected_ids = {
        'land_context': set(expected_context),
        'target_context': set(expected_targets),
    }
    for layer in SOURCE_LAYERS:
        if not decoded[layer]:
            raise PublicationError(f'PMTiles {layer} has no decoded features')
        if seen[layer] != expected_ids[layer]:
            raise PublicationError(
                f'PMTiles {layer} identifiers differ from input; '
                f'missing={sorted(expected_ids[layer] - seen[layer])[:5]}, '
                f'extra={sorted(seen[layer] - expected_ids[layer])[:5]}')
    return {'decoded_features': decoded, 'record_ids': seen}


def _state_bounds(context, code):
    return [item['bbox'] for item in context['registry'][code]['query_envelopes']]


def validate_pmtiles(path, code, normalized, expected_bounds=None):
    """Fully decode one archive and prove both layers preserve all records."""
    if code not in NON_CLAIM_STATES:
        raise PublicationError('PMTiles state must be one of the 30 non-claim states')
    try:
        checked = _pmtiles_header(
            path, expected_layers=SOURCE_LAYERS,
            required_properties={
                layer: sorted(REQUIRED_TILE_FIELDS[layer])
                for layer in SOURCE_LAYERS
            },
            verify_feature_properties=True, expected_state=code,
            expected_bounds=expected_bounds)
        if set(checked['source_layers']) != set(SOURCE_LAYERS):
            raise PublicationError(
                'PMTiles metadata must contain exactly land_context/target_context')
        semantic = _semantic_pmtiles(path, code, normalized)
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        if isinstance(exc, PublicationError):
            raise
        raise PublicationError(f'invalid PMTiles archive: {exc}') from exc
    return {
        'bytes': checked['bytes'],
        'sha256': _sha256_file(path),
        'tile_entries': checked['tile_entries'],
        'tile_contents': checked['tile_contents'],
        'decoded_features': semantic['decoded_features'],
        'source_layers': list(SOURCE_LAYERS),
    }


def assert_inputs_unchanged(context):
    """Rehash the inventory, clip, and all 90 snapshots before pointing."""
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
    if _registry_generation_sha256() != context['registry_sha256']:
        raise PublicationError('state/source registry changed during build')
    for code in context['codes']:
        for kind in KINDS:
            path = context['paths'][(code, kind)]
            expected = context['integrity'][(code, kind)]
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                raise PublicationError(
                    f'{code}.{kind} changed/disappeared during build: {exc}') from exc
            if size != expected['bytes'] or _sha256_file(path) != expected['sha256']:
                raise PublicationError(f'{code}.{kind} changed during build')


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _install_file(source, destination, validator=None):
    """Install add-only content; an existing name must be byte-identical."""
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(destination):
        if _sha256_file(source) != _sha256_file(destination):
            raise PublicationError(
                f'content-addressed destination collision at {destination}')
        if validator:
            validator(destination)
        return destination
    handle, pending = tempfile.mkstemp(
        prefix='.land-context-', suffix='.tmp', dir=directory)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as output, open(source, 'rb') as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if _sha256_file(source) != _sha256_file(pending):
            raise PublicationError('artifact changed during publication copy')
        if validator:
            validator(pending)
        try:
            os.link(pending, destination)
        except FileExistsError:
            if _sha256_file(source) != _sha256_file(destination):
                raise PublicationError(
                    f'content-addressed destination collision at {destination}')
            if validator:
                validator(destination)
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        _fsync_directory(directory)
        return destination
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _install_bytes(raw, destination):
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    handle, source = tempfile.mkstemp(prefix='.generation-', dir=directory)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        return _install_file(source, destination)
    finally:
        try:
            os.unlink(source)
        except FileNotFoundError:
            pass


def _merge_latest(latest_path, entries):
    latest_path = os.path.realpath(latest_path)
    directory = os.path.dirname(latest_path)
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, '.land-context-latest.lock')
    with open(lock_path, 'a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if os.path.exists(latest_path):
            latest, _ = _read_strict_json(
                latest_path, 'latest land-context generation index')
        else:
            latest = {
                'schema_version': SCHEMA_VERSION,
                'system': SYSTEM,
                'artifacts': {},
            }
        if (not isinstance(latest, dict) or
                set(latest) != {'schema_version', 'system', 'artifacts'} or
                latest.get('schema_version') != SCHEMA_VERSION or
                latest.get('system') != SYSTEM or
                not isinstance(latest.get('artifacts'), dict)):
            raise PublicationError(
                'latest index must be an exact national land-context schema v1 object')
        latest['artifacts'].update(entries)
        mode = (stat.S_IMODE(os.stat(latest_path).st_mode)
                if os.path.exists(latest_path) else 0o644)
        handle, pending = tempfile.mkstemp(prefix='.latest-', dir=directory)
        try:
            os.fchmod(handle, mode)
            with os.fdopen(handle, 'wb') as output:
                output.write(_canonical_json(latest))
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


def _generation(context, code, normalized, archive_path, archive_meta,
                latest_manifest, tippecanoe_version):
    snapshots = normalized['snapshots']
    counts = {
        'land_context': len(normalized['context_features']),
        'target_context': len(normalized['target_features']),
    }
    return {
        'schema_version': SCHEMA_VERSION,
        'system': SYSTEM,
        'state': code,
        'complete': True,
        'inventory_created': context['inventory']['created'],
        'inventory_sha256': context['inventory_sha256'],
        'registry_sha256': context['registry_sha256'],
        'clip': {
            'authority': CLIP_AUTHORITY,
            'method': CLIP_METHOD,
            'artifact_sha256': context['clip_sha256'],
        },
        'inputs': {
            kind: {
                'sha256': normalized['input_sha256s'][kind],
                'n': context['inventory']['states'][code][kind]['n'],
                'retrieved': snapshots[kind]['retrieved'],
                'source_ids': snapshots[kind]['source_ids'],
                'official_source_urls': snapshots[kind][
                    'official_source_urls'],
                'complete': True,
                'truncated': False,
                'pagination_exhausted': True,
            }
            for kind in KINDS
        },
        'scoring': {
            'method_id': snapshots['ranked_targets']['method_id'],
            'input_sha256s': snapshots['ranked_targets']['input_sha256s'],
            'context_input_sha256': normalized['context_input_sha256'],
            'open_ground': {'status': 'not_applicable', 'value': None,
                            'display': 'N/A'},
            'top_target_ids': normalized['top_target_ids'],
        },
        'publication': {
            'format': 'pmtiles',
            'file': os.path.relpath(
                archive_path, os.path.dirname(latest_manifest)),
            'bytes': archive_meta['bytes'],
            'sha256': archive_meta['sha256'],
            'source_layers': list(SOURCE_LAYERS),
            'required_properties': {
                layer: sorted(REQUIRED_TILE_FIELDS[layer])
                for layer in SOURCE_LAYERS
            },
            'source_layer_counts': counts,
            'layer_metadata': {
                layer: {'n': counts[layer], 'availability': 'complete',
                        'complete': True}
                for layer in SOURCE_LAYERS
            },
            'tippecanoe_version': tippecanoe_version,
            'no_feature_limit': True,
            'no_tile_size_limit': True,
            'full_semantic_validation': True,
        },
    }


def build(staging_dir, inventory_path, publish_dir, *, latest_manifest=None,
          state_clips=DEFAULT_STATE_CLIPS, states=None,
          tippecanoe='tippecanoe'):
    """Validate, tile, install immutable generations, then atomically point."""
    context = load_inventory(staging_dir, inventory_path, state_clips)
    codes = _selected_codes(context, states)
    executable, tippecanoe_version = _tippecanoe_executable(tippecanoe)
    publish_dir = os.path.realpath(publish_dir)
    if not _outside(publish_dir, context['staging']):
        raise PublicationError('publish directory must be outside raw staging')
    latest_manifest = os.path.realpath(
        latest_manifest or os.path.join(publish_dir, 'latest.json'))
    if _outside(latest_manifest, publish_dir):
        raise PublicationError(
            'latest generation index must remain inside publish directory')

    prepared = []
    with tempfile.TemporaryDirectory(prefix='nwmm-land-context-') as temporary:
        for code in codes:
            normalized = _normalize_state(context, code)
            context_sequence = os.path.join(
                temporary, f'{code.lower()}-land-context.geojsonseq')
            target_sequence = os.path.join(
                temporary, f'{code.lower()}-target-context.geojsonseq')
            archive = os.path.join(
                temporary, f'{code.lower()}-land-context.pmtiles')
            _write_sequence(context_sequence, normalized['context_features'])
            _write_sequence(target_sequence, normalized['target_features'])
            description = json.dumps({
                'schema': 'nwmm-national-nonclaim-land-context-v1',
                'state': code,
                'land_context': len(normalized['context_features']),
                'target_context': len(normalized['target_features']),
                'inventory_sha256': context['inventory_sha256'],
                'open_ground': 'not_applicable',
            }, separators=(',', ':'))
            command = [
                executable, '--force', '--output', archive,
                '--minimum-zoom=0', '--maximum-zoom=14',
                '--no-feature-limit', '--no-tile-size-limit',
                '--detect-shared-borders', '--read-parallel',
                '--preserve-input-order', '--quiet',
                '--use-attribute-for-id=fid', '--exclude=fid',
                f'--description={description}',
                '--attribution=Reviewed surface and independent '
                'mineral-interest sources; see immutable generation evidence',
                '-L', f'land_context:{context_sequence}',
                '-L', f'target_context:{target_sequence}',
            ]
            subprocess.run(command, check=True)
            archive_meta = validate_pmtiles(
                archive, code, normalized, _state_bounds(context, code))
            prepared.append({
                'code': code,
                'normalized': normalized,
                'archive': archive,
                'archive_meta': archive_meta,
            })

        # All 90 files are checked, even for a selected-state build. Any
        # mutation during tiling aborts before an immutable file is installed.
        assert_inputs_unchanged(context)
        pointer_entries = {}
        for item in prepared:
            code = item['code']
            normalized = item['normalized']
            archive_meta = item['archive_meta']
            archive_name = (
                f'{code.lower()}-land-context-'
                f'{archive_meta["sha256"]}.pmtiles')
            archive_path = os.path.join(
                publish_dir, 'artifacts', archive_name)
            validator = lambda path, c=code, n=normalized: validate_pmtiles(
                path, c, n, _state_bounds(context, c))
            _install_file(item['archive'], archive_path, validator=validator)
            generation = _generation(
                context, code, normalized, archive_path, archive_meta,
                latest_manifest, tippecanoe_version)
            generation_raw = _canonical_json(generation)
            generation_sha = _sha256_bytes(generation_raw)
            generation_name = (
                f'{code.lower()}-land-context-generation-'
                f'{generation_sha}.json')
            generation_path = os.path.join(
                publish_dir, 'generations', generation_name)
            _install_bytes(generation_raw, generation_path)
            pointer_entries[code] = {
                'state': code,
                'system': SYSTEM,
                'generation_file': os.path.relpath(
                    generation_path, os.path.dirname(latest_manifest)),
                'generation_sha256': generation_sha,
                'artifact_file': os.path.relpath(
                    archive_path, os.path.dirname(latest_manifest)),
                'artifact_sha256': archive_meta['sha256'],
                'inventory_sha256': context['inventory_sha256'],
                'registry_sha256': context['registry_sha256'],
            }

        # A late mutation may leave an unreferenced immutable object, but it
        # can never advance the publication pointer.
        assert_inputs_unchanged(context)
        _merge_latest(latest_manifest, pointer_entries)

    return {
        'latest_manifest': latest_manifest,
        'states': list(codes),
        'artifact_keys': sorted(pointer_entries),
        'layers': list(SOURCE_LAYERS),
        'published_states': len(pointer_entries),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=('Build immutable per-target land-context PMTiles for '
                     'the exact 30 WS11 non-claim states'))
    parser.add_argument('--staging-dir', required=True,
                        help='private directory containing all 90 snapshots')
    parser.add_argument('--inventory', required=True,
                        help='checksum-pinned exact 30-state inventory JSON')
    parser.add_argument('--publish-dir', required=True)
    parser.add_argument('--latest-manifest')
    parser.add_argument('--state-clips', default=DEFAULT_STATE_CLIPS)
    parser.add_argument('--state', action='append', dest='states',
                        help='non-claim state to build; repeat, or omit for all 30')
    parser.add_argument('--tippecanoe', default='tippecanoe')
    args = parser.parse_args(argv)
    try:
        result = build(
            args.staging_dir, args.inventory, args.publish_dir,
            latest_manifest=args.latest_manifest,
            state_clips=args.state_clips, states=args.states,
            tippecanoe=args.tippecanoe)
    except (PublicationError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
