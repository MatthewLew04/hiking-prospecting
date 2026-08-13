#!/usr/bin/env python3
"""Build reviewed non-claim AML and trust-land PMTiles generations.

This is deliberately a publication builder, not a state-portal scraper.  A
registry-driven adapter freezes each source response in private staging and
records the exact retrieval/pagination contract in a checksummed inventory.
This module then validates all 30 non-claim decisions, checks every spatial
feature against the authoritative state polygon, builds canonical ``aml`` or
``trust_land`` PMTiles, fully decodes the resulting MVT, and atomically updates
an independent publication pointer.

An adapter may instead stage a reviewed, explicit finding that no suitable
spatial inventory is publicly available.  Such a finding produces a small
evidence artifact, never a fabricated empty layer.  This module never edits a
state registry, browser manifest, or release switch, and never publishes the
source GeoJSON.
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

from state_registry import ALL_STATES, NON_CLAIM_STATES, load_states


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
SYSTEM = 'nonclaim_equivalents'
KINDS = ('aml', 'trust_land')
INGESTED = 'ingested_complete'
FINDING_STATUSES = {
    'aml': frozenset(('documented_unavailable',)),
    'trust_land': frozenset(('documented_unavailable', 'not_applicable')),
}
ALL_STATUSES = {
    kind: frozenset((INGESTED,)) | FINDING_STATUSES[kind]
    for kind in KINDS
}
OFFERING_CLASSES = frozenset(('offered', 'limited', 'not_offered'))
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PROPERTY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,62}$')
SAFE_INTEGER_MAX = (1 << 53) - 1

DEFAULT_STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')
CLIP_AUTHORITY = 'U.S. Census Bureau TIGERweb, January 1 2025 vintage'
CLIP_METHOD = ('Every source coordinate must be inside or on the boundary of '
               'the authoritative state polygon before tiling')

REQUIRED_TILE_FIELDS = {
    'aml': frozenset(('st', 'source_id', 'status', 'record_id', 'provenance')),
    'trust_land': frozenset((
        'st', 'mineral_class', 'approach', 'record_id', 'provenance')),
}
GEOMETRY_TYPES = {
    'aml': frozenset(('Point', 'MultiPoint')),
    'trust_land': frozenset(('Polygon', 'MultiPolygon')),
}
MVT_GEOMETRY_TYPES = {'aml': 1, 'trust_land': 3}


class PublicationError(ValueError):
    """A private-input or immutable-publication invariant failed."""


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
        data = json.loads(
            raw.decode('utf-8'), parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(
            f'{label or path} is not strict UTF-8 JSON: {exc}') from exc
    return data, raw


def _canonical_json(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8')
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


def _date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be an ISO YYYY-MM-DD date')
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError as exc:
        raise PublicationError(f'{label} is not a calendar date') from exc
    return value


def _text(value, label, *, minimum=1, maximum=1024):
    if not isinstance(value, str):
        raise PublicationError(f'{label} must be text')
    value = value.strip()
    if len(value) < minimum or len(value) > maximum:
        raise PublicationError(
            f'{label} must contain {minimum}..{maximum} characters')
    return value


def _https(value, label):
    value = _text(value, label, maximum=2048)
    if not value.startswith('https://'):
        raise PublicationError(f'{label} must be an HTTPS URL')
    return value


def _registry_nonclaim_states():
    """Return the exact reviewed 30-state non-claim registry scope."""
    states = load_states()
    if set(states) != set(ALL_STATES):
        raise PublicationError('registry must contain the exact WS11 49 states')
    codes = {code for code, row in states.items()
             if row.get('regime') == 'non_claim'}
    if codes != set(NON_CLAIM_STATES) or len(codes) != 30:
        raise PublicationError(
            'registry non-claim regime must equal the canonical 30 states')
    if any((states[code].get('open_ground') or {}).get('applicable') is not False
           for code in codes):
        raise PublicationError(
            'every non-claim registry entry must mark open ground not applicable')
    return states, tuple(sorted(codes))


def _safe_staging_paths(staging_dir, inventory_path):
    staging = os.path.realpath(staging_dir)
    inventory = os.path.realpath(inventory_path)
    if not os.path.isdir(staging):
        raise PublicationError(f'private staging directory is missing: {staging}')
    if not os.path.isfile(inventory):
        raise PublicationError(f'private staging inventory is missing: {inventory}')
    if not _outside(staging, SITE) or not _outside(inventory, SITE):
        raise PublicationError(
            'raw AML/trust staging and inventory must remain outside site/')
    if _outside(inventory, staging):
        raise PublicationError('inventory must remain inside its staging directory')
    return staging, inventory


def _snapshot_filename(code, kind):
    return f'{code.lower()}_{kind}.json'


def _validate_inventory_entry(code, kind, entry, staging):
    expected_name = _snapshot_filename(code, kind)
    required = {'file', 'n', 'bytes', 'sha256', 'release_inventory_status'}
    if not isinstance(entry, dict) or set(entry) != required:
        raise PublicationError(
            f'inventory {code}.{kind} keys must be exactly {sorted(required)}')
    if entry.get('file') != expected_name:
        raise PublicationError(
            f'inventory {code}.{kind}.file must be {expected_name!r}')
    status = entry.get('release_inventory_status')
    if status not in ALL_STATUSES[kind]:
        raise PublicationError(
            f'inventory {code}.{kind} has invalid reviewed decision {status!r}')
    if not _is_int(entry.get('n')):
        raise PublicationError(f'inventory {code}.{kind}.n must be nonnegative')
    if (status == INGESTED) is not (entry['n'] > 0):
        raise PublicationError(
            f'inventory {code}.{kind} ingestion must have rows and findings must not')
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
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise PublicationError(f'cannot stat {code}.{kind}: {exc}') from exc
    digest = _sha256_file(path)
    if size != entry['bytes'] or digest != entry['sha256']:
        raise PublicationError(
            f'{code}.{kind} bytes/sha256 differ from inventory')
    return path, {'bytes': size, 'sha256': digest, 'n': entry['n']}


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
    for code in sorted(NON_CLAIM_STATES):
        geometry = clips['states'][code]
        if (not isinstance(geometry, dict) or
                geometry.get('type') not in ('Polygon', 'MultiPolygon')):
            raise PublicationError(f'authoritative {code} clip is not a polygon')
        indexes[code] = StateClipIndex(geometry)
    return indexes, actual_sha, path


def load_inventory(staging_dir, inventory_path,
                   state_clips_path=DEFAULT_STATE_CLIPS):
    """Eagerly checksum the exact 60-file, 30-state private inventory."""
    staging, inventory_path = _safe_staging_paths(staging_dir, inventory_path)
    inventory, raw = _read_strict_json(
        inventory_path, 'non-claim equivalent staging inventory')
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
    registry, codes = _registry_nonclaim_states()
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
    indexes, clip_sha, clip_path = _load_clip_indexes(
        state_clips_path, clip['artifact_sha256'])
    return {
        'staging': staging,
        'inventory_path': inventory_path,
        'inventory': inventory,
        'inventory_raw': raw,
        'inventory_sha256': _sha256_bytes(raw),
        'registry': registry,
        'codes': codes,
        'paths': paths,
        'integrity': integrity,
        'clip_indexes': indexes,
        'clip_sha256': clip_sha,
        'state_clips_path': clip_path,
    }


def _official_urls(value, label):
    if (not isinstance(value, list) or not value or
            len(value) != len(set(value)) or len(value) > 20):
        raise PublicationError(
            f'{label} must be a nonempty, unique URL array')
    return [_https(url, f'{label}[{index}]')
            for index, url in enumerate(value)]


def _registry_binding(context, code, kind, snapshot):
    registry = context['registry'][code].get(kind)
    if not isinstance(registry, dict):
        raise PublicationError(f'{code} registry lacks {kind}')
    registry_status = registry.get('release_inventory_status')
    status = snapshot['release_inventory_status']
    if registry_status not in ('pending_review', status):
        raise PublicationError(
            f'{code}.{kind} staged decision disagrees with reviewed registry status')
    urls = snapshot['official_source_urls']
    registry_urls = [value for key, value in registry.items()
                     if key.endswith('_url') and isinstance(value, str) and
                     value.startswith('https://')]
    if registry_urls and not any(url in urls for url in registry_urls):
        raise PublicationError(
            f'{code}.{kind} snapshot does not cite an official registry URL')
    if kind == 'aml':
        source_id = registry.get('source_id')
        if isinstance(source_id, str) and source_id.strip() and source_id != snapshot['source_id']:
            raise PublicationError(
                f'{code}.aml source_id disagrees with the state registry')
    else:
        staged_class = snapshot['offering_class']
        registry_class = registry.get('offering_class')
        if registry_class not in ('unknown', staged_class):
            raise PublicationError(
                f'{code}.trust_land offering class disagrees with the registry')
        if status == INGESTED and staged_class not in ('offered', 'limited'):
            raise PublicationError(
                f'{code}.trust_land offering_class must be offered/limited '
                'for a spatial inventory')
        if status == 'not_applicable' and staged_class != 'not_offered':
            raise PublicationError(
                f'{code}.trust_land not_applicable requires offering_class=not_offered')


def _pagination(value, n, label):
    keys = {
        'method', 'expected_count', 'fetched_count', 'page_size',
        'page_offsets', 'page_row_counts', 'pagination_exhausted',
        'source_snapshot_id',
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise PublicationError(
            f'{label} keys must be exactly {sorted(keys)}')
    method = value.get('method')
    if method not in ('offset', 'single_file'):
        raise PublicationError(f'{label}.method must be offset or single_file')
    if value.get('expected_count') != n or value.get('fetched_count') != n:
        raise PublicationError(
            f'{label} expected_count/fetched_count must equal inventory n')
    if not _is_int(value.get('page_size'), 1):
        raise PublicationError(f'{label}.page_size must be positive')
    if value.get('pagination_exhausted') is not True:
        raise PublicationError(
            f'{label}.pagination_exhausted must be true')
    page_size = value['page_size']
    offsets = value.get('page_offsets')
    row_counts = value.get('page_row_counts')
    if (not isinstance(offsets, list) or not isinstance(row_counts, list) or
            len(offsets) != len(row_counts) or not offsets):
        raise PublicationError(f'{label} page arrays are invalid')
    expected_offsets = list(range(0, n, page_size))
    expected_counts = [min(page_size, n - offset)
                       for offset in expected_offsets]
    if offsets != expected_offsets or row_counts != expected_counts:
        raise PublicationError(
            f'{label} offsets/row counts do not exhaust expected_count')
    if method == 'single_file' and (offsets != [0] or row_counts != [n]):
        raise PublicationError(
            f'{label} single_file acquisition must have exactly one complete page')
    _text(value.get('source_snapshot_id'), f'{label}.source_snapshot_id',
          maximum=300)
    return value


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PublicationError(f'{label} must be a finite number')
    value = float(value)
    if not math.isfinite(value):
        raise PublicationError(f'{label} must be a finite number')
    return value


def _position(value, label):
    if not isinstance(value, list) or len(value) < 2:
        raise PublicationError(f'{label} must contain longitude and latitude')
    x = _finite(value[0], f'{label}.longitude')
    y = _finite(value[1], f'{label}.latitude')
    if not -180 <= x <= 180 or not -90 <= y <= 90:
        raise PublicationError(f'{label} is outside WGS84 coordinate bounds')
    if len(value) > 3:
        raise PublicationError(f'{label} has unsupported coordinate dimensions')
    if len(value) == 3:
        _finite(value[2], f'{label}.elevation')
    return x, y


def _positions(geometry, label):
    if not isinstance(geometry, dict) or set(geometry) != {'type', 'coordinates'}:
        raise PublicationError(f'{label} must be a strict GeoJSON geometry')
    kind = geometry.get('type')
    coordinates = geometry.get('coordinates')
    positions = []
    rings = []
    if kind == 'Point':
        positions = [_position(coordinates, f'{label}.coordinates')]
    elif kind == 'MultiPoint':
        if not isinstance(coordinates, list) or not coordinates:
            raise PublicationError(f'{label} MultiPoint is empty')
        positions = [_position(value, f'{label}.coordinates[{index}]')
                     for index, value in enumerate(coordinates)]
    elif kind in ('Polygon', 'MultiPolygon'):
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
                parsed = [_position(
                    value,
                    f'{label}.coordinates[{polygon_index}][{ring_index}][{index}]')
                    for index, value in enumerate(ring)]
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
                rings.append(parsed)
    else:
        raise PublicationError(f'{label} has unsupported geometry type {kind!r}')
    return kind, positions, rings


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


def _scalar(value, label):
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > 2048:
            raise PublicationError(f'{label} exceeds 2048 characters')
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not -SAFE_INTEGER_MAX <= value <= SAFE_INTEGER_MAX:
            raise PublicationError(f'{label} exceeds JavaScript-safe integer range')
        return value
    if isinstance(value, float):
        return _finite(value, label)
    raise PublicationError(f'{label} must be a scalar MVT property')


def _feature_id(code, kind, record_id):
    identity = f'{SYSTEM}\x1f{code}\x1f{kind}\x1f{record_id}'.encode('utf-8')
    digest = hashlib.blake2b(
        identity, digest_size=8, person=b'nwmm-eq-v1').digest()
    return (int.from_bytes(digest, 'big') & SAFE_INTEGER_MAX) or 1


def _normalize_feature(context, code, kind, snapshot, feature, index,
                       input_sha):
    label = f'{code}.{kind}.features[{index}]'
    if (not isinstance(feature, dict) or
            set(feature) != {'type', 'id', 'properties', 'geometry'} or
            feature.get('type') != 'Feature'):
        raise PublicationError(f'{label} must be a strict GeoJSON Feature')
    properties = feature.get('properties')
    if (not isinstance(properties, dict) or not properties or
            len(properties) > 64):
        raise PublicationError(f'{label}.properties must contain 1..64 fields')
    normalized = {}
    for key, value in properties.items():
        if not isinstance(key, str) or PROPERTY_RE.fullmatch(key) is None:
            raise PublicationError(f'{label} property key {key!r} is invalid')
        normalized[key] = _scalar(value, f'{label}.properties.{key}')
    record_id = _text(normalized.get('record_id'),
                      f'{label}.properties.record_id', maximum=180)
    feature_identity = feature.get('id')
    if isinstance(feature_identity, bool) or not isinstance(
            feature_identity, (str, int)) or str(feature_identity) != record_id:
        raise PublicationError(f'{label}.id must exactly match record_id')
    if normalized.get('st') not in (None, code):
        raise PublicationError(f'{label}.properties.st is not {code}')
    if kind == 'aml':
        normalized['source_id'] = _text(
            normalized.get('source_id'), f'{label}.properties.source_id',
            maximum=180)
        normalized['status'] = _text(
            normalized.get('status'), f'{label}.properties.status',
            maximum=180)
    else:
        normalized['mineral_class'] = _text(
            normalized.get('mineral_class'),
            f'{label}.properties.mineral_class', maximum=240)
        normalized['approach'] = _text(
            normalized.get('approach'), f'{label}.properties.approach',
            minimum=10, maximum=1200)
    geometry_type, positions, _ = _positions(
        feature.get('geometry'), f'{label}.geometry')
    if geometry_type not in GEOMETRY_TYPES[kind]:
        raise PublicationError(
            f'{label} geometry must be one of {sorted(GEOMETRY_TYPES[kind])}')
    for x, y in positions:
        if not _inside_or_boundary(context['clip_indexes'][code], x, y):
            raise PublicationError(
                f'{label} contains a coordinate outside the authoritative {code} clip')
    normalized['st'] = code
    normalized['record_id'] = record_id
    if not isinstance(normalized.get('provenance'), str) or not normalized['provenance'].strip():
        normalized['provenance'] = (
            f'{snapshot["source_id"]}:{input_sha[:16]}')
    normalized['provenance'] = _text(
        normalized['provenance'], f'{label}.properties.provenance',
        maximum=1000)
    fid = _feature_id(code, kind, record_id)
    normalized['fid'] = fid
    return record_id, fid, {
        'type': 'Feature',
        'id': fid,
        'properties': normalized,
        'geometry': feature['geometry'],
    }


def _load_snapshot(context, code, kind):
    entry = context['inventory']['states'][code][kind]
    path = context['paths'][(code, kind)]
    data, raw = _read_strict_json(path, f'{code}.{kind} snapshot')
    digest = _sha256_bytes(raw)
    if len(raw) != entry['bytes'] or digest != entry['sha256']:
        raise PublicationError(f'{code}.{kind} changed after inventory load')
    common = {
        'schema_version', 'state', 'kind', 'release_inventory_status',
        'source_id', 'reviewed', 'complete', 'official_source_urls',
    }
    status = entry['release_inventory_status']
    keys = set(common)
    if kind == 'trust_land':
        keys.add('offering_class')
    if status == INGESTED:
        keys |= {'retrieved', 'truncated', 'pagination', 'type', 'features'}
    else:
        keys |= {'spatial_inventory_available', 'finding'}
    if not isinstance(data, dict) or set(data) != keys:
        raise PublicationError(
            f'{code}.{kind} snapshot keys must be exactly {sorted(keys)}')
    if (data.get('schema_version') != SCHEMA_VERSION or
            data.get('state') != code or data.get('kind') != kind or
            data.get('release_inventory_status') != status):
        raise PublicationError(f'{code}.{kind} snapshot identity/decision is invalid')
    if data.get('complete') is not True:
        raise PublicationError(f'{code}.{kind} must be explicitly complete')
    _text(data.get('source_id'), f'{code}.{kind}.source_id', maximum=180)
    _date(data.get('reviewed'), f'{code}.{kind}.reviewed')
    data['official_source_urls'] = _official_urls(
        data.get('official_source_urls'), f'{code}.{kind}.official_source_urls')
    if kind == 'trust_land' and data.get('offering_class') not in OFFERING_CLASSES:
        raise PublicationError(f'{code}.trust_land offering_class is invalid')
    _registry_binding(context, code, kind, data)
    if status != INGESTED:
        if data.get('spatial_inventory_available') is not False:
            raise PublicationError(
                f'{code}.{kind} finding must set spatial_inventory_available=false')
        data['finding'] = _text(
            data.get('finding'), f'{code}.{kind}.finding',
            minimum=40, maximum=4000)
        if entry['n'] != 0:
            raise PublicationError(f'{code}.{kind} finding inventory n must be zero')
        return {
            'status': status,
            'snapshot': data,
            'input_sha256': digest,
            'n': 0,
            'features': [],
            'record_ids': set(),
        }
    _date(data.get('retrieved'), f'{code}.{kind}.retrieved')
    if data.get('truncated') is not False:
        raise PublicationError(f'{code}.{kind} truncated input cannot publish')
    if data.get('type') != 'FeatureCollection' or not isinstance(
            data.get('features'), list):
        raise PublicationError(f'{code}.{kind} must be a FeatureCollection')
    if len(data['features']) != entry['n'] or not data['features']:
        raise PublicationError(
            f'{code}.{kind} feature count differs from its inventory')
    _pagination(data.get('pagination'), entry['n'], f'{code}.{kind}.pagination')
    features = []
    record_ids = set()
    feature_ids = set()
    for index, feature in enumerate(data['features']):
        record_id, fid, normalized = _normalize_feature(
            context, code, kind, data, feature, index, digest)
        if record_id in record_ids:
            raise PublicationError(f'{code}.{kind} duplicates record_id {record_id!r}')
        if fid in feature_ids:
            raise PublicationError(f'{code}.{kind} stable feature-ID collision')
        record_ids.add(record_id)
        feature_ids.add(fid)
        features.append(normalized)
    return {
        'status': status,
        'snapshot': data,
        'input_sha256': digest,
        'n': len(features),
        'features': features,
        'record_ids': record_ids,
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
    normalized = tuple(sorted({code.strip().upper() for code in selected_states}))
    if not normalized:
        raise PublicationError('at least one --state is required when filtering')
    invalid = set(normalized) - set(context['codes'])
    if invalid:
        raise PublicationError(
            f'non-claim equivalent scope contains claim/unsupported states '
            f'{sorted(invalid)}')
    return normalized


def _write_sequence(path, features):
    with open(path, 'w', encoding='utf-8') as output:
        for feature in features:
            json.dump(feature, output, separators=(',', ':'), allow_nan=False)
            output.write('\n')


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


def _semantic_pmtiles(path, code, kind, expected_records=None):
    entries, tile_offset, tile_length, compression = _all_tile_entries(path)
    expected_geometry = MVT_GEOMETRY_TYPES[kind]
    records = set()
    decoded = 0
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
                if layer['name'] != kind:
                    raise PublicationError(
                        f'PMTiles contains unexpected layer {layer["name"]!r}')
                for feature in layer['features']:
                    decoded += 1
                    properties = feature['properties']
                    missing = REQUIRED_TILE_FIELDS[kind] - set(properties)
                    if missing:
                        raise PublicationError(
                            f'{kind} MVT feature lacks {sorted(missing)}')
                    if properties.get('st') != code:
                        raise PublicationError(
                            f'{kind} MVT feature state is not {code}')
                    if feature['geometry_type'] != expected_geometry:
                        raise PublicationError(
                            f'{kind} MVT feature has wrong geometry type')
                    record_id = properties.get('record_id')
                    _text(record_id, f'{kind} MVT record_id', maximum=180)
                    _text(properties.get('provenance'),
                          f'{kind} MVT provenance', maximum=1000)
                    if kind == 'aml':
                        _text(properties.get('source_id'),
                              'aml MVT source_id', maximum=180)
                        _text(properties.get('status'),
                              'aml MVT status', maximum=180)
                    else:
                        _text(properties.get('mineral_class'),
                              'trust_land MVT mineral_class', maximum=240)
                        _text(properties.get('approach'),
                              'trust_land MVT approach', minimum=10, maximum=1200)
                    records.add(record_id)
    if decoded <= 0:
        raise PublicationError(f'PMTiles {kind} layer has no decoded features')
    if expected_records is not None and records != set(expected_records):
        missing = sorted(set(expected_records) - records)[:5]
        extra = sorted(records - set(expected_records))[:5]
        raise PublicationError(
            f'PMTiles {kind} record IDs differ from input; '
            f'missing={missing}, extra={extra}')
    return decoded, records


def _state_bounds(context, code):
    return [item['bbox'] for item in context['registry'][code]['query_envelopes']]


def validate_pmtiles(path, code, kind, expected_records=None,
                     expected_bounds=None):
    """Fully decode one state/kind archive and prove row preservation."""
    if code not in NON_CLAIM_STATES or kind not in KINDS:
        raise PublicationError('PMTiles scope must be one non-claim state/kind')
    try:
        checked = _pmtiles_header(
            path, expected_layers=[kind],
            required_properties={kind: set(REQUIRED_TILE_FIELDS[kind])},
            verify_feature_properties=True, expected_state=code,
            expected_bounds=expected_bounds)
        if set(checked['source_layers']) != {kind}:
            raise PublicationError(f'PMTiles must contain exactly {kind}')
        decoded, records = _semantic_pmtiles(
            path, code, kind, expected_records=expected_records)
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        if isinstance(exc, PublicationError):
            raise
        raise PublicationError(f'invalid PMTiles archive: {exc}') from exc
    return {
        'bytes': checked['bytes'],
        'sha256': _sha256_file(path),
        'tile_entries': checked['tile_entries'],
        'tile_contents': checked['tile_contents'],
        'decoded_features': decoded,
        'record_ids': records,
        'source_layers': [kind],
    }


def assert_inputs_unchanged(context):
    """Rehash the inventory, clip, and all 60 decisions before pointing."""
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
    """Install add-only content; an existing name must have identical bytes."""
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(destination):
        if _sha256_file(source) != _sha256_file(destination):
            raise PublicationError(
                f'content-addressed destination collision at {destination}')
        if validator:
            validator(destination)
        return destination
    handle, pending = tempfile.mkstemp(prefix='.nonclaim-', suffix='.tmp', dir=directory)
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
    handle, source = tempfile.mkstemp(prefix='.evidence-', dir=directory)
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


def _evidence(code, kind, loaded, archive_meta=None):
    snapshot = loaded['snapshot']
    evidence = {
        'schema_version': 1,
        'state': code,
        'kind': kind,
        'release_inventory_status': loaded['status'],
        'complete': True,
        'reviewed': snapshot['reviewed'],
        'official_source_urls': snapshot['official_source_urls'],
        'input_sha256': loaded['input_sha256'],
    }
    if kind == 'trust_land':
        evidence['offering_class'] = snapshot['offering_class']
    if loaded['status'] == INGESTED:
        evidence.update({
            'spatial_inventory_available': True,
            'retrieved': snapshot['retrieved'],
            'artifact_sha256': archive_meta['sha256'],
            'source_layer_counts': {kind: loaded['n']},
            'pagination': snapshot['pagination'],
        })
    else:
        evidence.update({
            'spatial_inventory_available': False,
            'finding': snapshot['finding'],
        })
    return evidence


def _merge_latest(latest_path, entries):
    latest_path = os.path.realpath(latest_path)
    directory = os.path.dirname(latest_path)
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, '.nonclaim-equivalents-latest.lock')
    with open(lock_path, 'a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if os.path.exists(latest_path):
            latest, _ = _read_strict_json(latest_path, 'latest publication manifest')
        else:
            latest = {'schema_version': 1, 'artifacts': {}}
        if not isinstance(latest, dict) or latest.get('schema_version') != 1:
            raise PublicationError('latest manifest must be schema_version 1')
        artifacts = latest.get('artifacts')
        if not isinstance(artifacts, dict):
            raise PublicationError('latest manifest artifacts must be an object')
        artifacts.update(entries)
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


def build(staging_dir, inventory_path, publish_dir, *, latest_manifest=None,
          state_clips=DEFAULT_STATE_CLIPS, states=None,
          tippecanoe='tippecanoe'):
    """Validate, tile, install immutable artifacts, then atomically point."""
    executable = shutil.which(tippecanoe)
    context = load_inventory(staging_dir, inventory_path, state_clips)
    codes = _selected_codes(context, states)
    publish_dir = os.path.realpath(publish_dir)
    if not _outside(publish_dir, context['staging']):
        raise PublicationError('publish directory must be outside raw staging')
    latest_manifest = os.path.realpath(
        latest_manifest or os.path.join(publish_dir, 'latest.json'))
    if _outside(latest_manifest, publish_dir):
        raise PublicationError('latest manifest must remain inside publish directory')

    prepared = []
    with tempfile.TemporaryDirectory(prefix='nwmm-nonclaim-') as temporary:
        for code in codes:
            for kind in KINDS:
                loaded = _load_snapshot(context, code, kind)
                item = {'code': code, 'kind': kind, 'loaded': loaded}
                if loaded['status'] == INGESTED:
                    if not executable:
                        raise PublicationError(
                            'tippecanoe >=2.79 with PMTiles output is required '
                            'for ingested decisions')
                    sequence = os.path.join(temporary, f'{code.lower()}-{kind}.geojsonseq')
                    archive = os.path.join(temporary, f'{code.lower()}-{kind}.pmtiles')
                    _write_sequence(sequence, loaded['features'])
                    description = json.dumps({
                        'schema': 'nwmm-nonclaim-equivalents-v1',
                        'state': code,
                        'kind': kind,
                        'features': loaded['n'],
                        'input_sha256': loaded['input_sha256'],
                    }, separators=(',', ':'))
                    command = [
                        executable, '--force', '--output', archive,
                        '--minimum-zoom=0', '--maximum-zoom=14',
                        '--no-feature-limit', '--no-tile-size-limit',
                        '--detect-shared-borders', '--read-parallel',
                        '--preserve-input-order', '--quiet',
                        '--use-attribute-for-id=fid', '--exclude=fid',
                        f'--description={description}',
                        '--attribution=State mineral/AML authorities; see evidence artifact',
                        '-L', f'{kind}:{sequence}',
                    ]
                    subprocess.run(command, check=True)
                    meta = validate_pmtiles(
                        archive, code, kind, loaded['record_ids'],
                        _state_bounds(context, code))
                    item.update({'archive': archive, 'archive_meta': meta})
                prepared.append(item)

        # Any of the 60 private decisions changing while tippecanoe ran aborts
        # before the first immutable file or pointer is installed.
        assert_inputs_unchanged(context)
        entries = {}
        for item in prepared:
            code, kind, loaded = item['code'], item['kind'], item['loaded']
            decision = {
                'release_inventory_status': loaded['status'],
                'n': loaded['n'],
                'input_sha256': loaded['input_sha256'],
            }
            archive_meta = item.get('archive_meta')
            if archive_meta:
                archive_name = f'{archive_meta["sha256"]}.pmtiles'
                archive_path = os.path.join(publish_dir, archive_name)
                validator = lambda path, c=code, k=kind, records=loaded['record_ids']: (
                    validate_pmtiles(path, c, k, records, _state_bounds(context, c)))
                _install_file(item['archive'], archive_path, validator=validator)
                decision.update({
                    'format': 'pmtiles',
                    'file': os.path.relpath(archive_path, os.path.dirname(latest_manifest)),
                    'source_layers': [kind],
                    'required_properties': sorted(REQUIRED_TILE_FIELDS[kind]),
                    'layer_metadata': {
                        kind: {'n': loaded['n'], 'availability': 'complete',
                               'complete': True}},
                    'bytes': archive_meta['bytes'],
                    'sha256': archive_meta['sha256'],
                })
            evidence = _evidence(code, kind, loaded, archive_meta)
            evidence_raw = _canonical_json(evidence)
            evidence_sha256 = _sha256_bytes(evidence_raw)
            evidence_name = f'{evidence_sha256}.json'
            evidence_path = os.path.join(publish_dir, 'evidence', evidence_name)
            _install_bytes(evidence_raw, evidence_path)
            decision['evidence_file'] = os.path.relpath(
                evidence_path, os.path.dirname(latest_manifest))
            decision['evidence_bytes'] = len(evidence_raw)
            decision['evidence_sha256'] = evidence_sha256
            if kind == 'trust_land':
                decision['offering_class'] = loaded['snapshot']['offering_class']
            item['decision'] = decision

        assert_inputs_unchanged(context)
        for code in codes:
            decisions = {
                item['kind']: item['decision'] for item in prepared
                if item['code'] == code
            }
            entries[f'{SYSTEM}_{code.lower()}'] = {
                'system': SYSTEM,
                'state': code,
                'inventory_created': context['inventory']['created'],
                'inventory_sha256': context['inventory_sha256'],
                'clip': {
                    'authority': CLIP_AUTHORITY,
                    'method': CLIP_METHOD,
                    'artifact_sha256': context['clip_sha256'],
                },
                'decisions': decisions,
            }
        _merge_latest(latest_manifest, entries)

    return {
        'latest_manifest': latest_manifest,
        'states': list(codes),
        'artifact_keys': sorted(entries),
        'decisions': sum(len(item['decisions']) for item in entries.values()),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=('Build immutable AML/trust-land PMTiles or explicit '
                     'finding evidence for the 30 non-claim states'))
    parser.add_argument('--staging-dir', required=True)
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
            latest_manifest=args.latest_manifest, state_clips=args.state_clips,
            states=args.states, tippecanoe=args.tippecanoe)
    except (PublicationError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
