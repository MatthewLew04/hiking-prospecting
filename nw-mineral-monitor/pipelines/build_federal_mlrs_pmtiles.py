#!/usr/bin/env python3
"""Publish checked 19-state federal MLRS snapshots as immutable PMTiles.

Raw columnar snapshots remain in a private staging directory.  A strict,
checksum-pinned inventory declares exactly one active and one closed snapshot
for every registry claim state.  The builder verifies every row against the
authoritative Census state clips, streams temporary GeoJSON sequences outside
the browser tree, creates active/closed MVT source layers, and publishes an
immutable content-addressed archive.  An atomically replaced ``latest.json``
is the publication pointer; it is updated only after the archive and all
inputs have been revalidated.

This module deliberately does not write ``site/data/manifest.json`` or the
legacy ``claims.pmtiles`` compatibility archive.  Operators choose an explicit
publish directory and can promote the checked latest entry separately.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
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
from datetime import datetime

from state_registry import ALL_STATES, CLAIM_STATES, load_states
from validate_national import _pmtiles_header


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
DEFAULT_STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')
INFRA = os.path.join(ROOT, 'infra')
if INFRA not in sys.path:
    sys.path.insert(0, INFRA)
from spatial_clip import StateClipIndex


SCHEMA_VERSION = 1
SYSTEM = 'federal_mlrs'
SOURCE = ('https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/'
          'MiningClaims/MapServer')
CLIP_AUTHORITY = 'U.S. Census Bureau TIGERweb, January 1 2025 vintage'
CLIP_METHOD = 'claim-polygon centroid within authoritative state polygon'
PAGINATION_METHOD = 'OBJECTID cursor to empty page for every query envelope'
PAGINATION_ORDER = {'active': 'OBJECTID ASC', 'closed': 'OBJECTID DESC'}
PAGINATION_PAGE_SIZE = 2000
MODES = ('active', 'closed')
PROFILES = ('progress', 'full', 'release')
SAFE_INTEGER_MAX = (1 << 53) - 1
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
REQUIRED_COLUMNS = {
    'active': ('serial', 'name', 'type', 'x', 'y', 'admin_state',
               'geo_state', 'disp', 'acres'),
    'closed': ('serial', 'name', 'type', 'x', 'y', 'admin_state',
               'geo_state'),
}
ALLOWED_ROOT_FIELDS = frozenset({
    'state', 'layer', 'retrieved', 'n',
    *REQUIRED_COLUMNS['active'],
    'truncated', 'total_available', 'envelope_total_upper_bound',
    'partial', 'partial_after_spatial_clip', 'partial_note',
    'spatial_clip', 'source',
    'pagination',
})
TEXT_LIMITS = {'serial': 160, 'name': 512, 'type': 64, 'disp': 128,
               'admin_state': 64, 'geo_state': 64}


class PublicationError(ValueError):
    """A staging, inventory, tiling, or publication invariant failed."""


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
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be an ISO YYYY-MM-DD date')
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError as exc:
        raise PublicationError(f'{label} is not a calendar date') from exc
    return value


def _is_int(value, minimum=0):
    return (isinstance(value, int) and not isinstance(value, bool) and
            value >= minimum)


def _registry_claim_states():
    """Derive and cross-check the exact federal claim scope from registry."""
    states = load_states()
    claim_codes = {
        code for code, row in states.items() if row.get('regime') == 'claim'
    }
    if claim_codes != set(CLAIM_STATES) or len(claim_codes) != 19:
        raise PublicationError(
            'registry claim regime must equal the canonical 19 BLM claim states')
    for code in sorted(claim_codes):
        systems = [system for system in states[code].get('claim_systems', [])
                   if isinstance(system, dict) and system.get('id') == SYSTEM]
        if len(systems) != 1:
            raise PublicationError(
                f'{code} registry must declare exactly one {SYSTEM} system')
        system = systems[0]
        if (system.get('layers') != {'active': 1, 'closed': 2} or
                system.get('browser_delivery') != 'pmtiles'):
            raise PublicationError(
                f'{code} {SYSTEM} registry layers/delivery are invalid')
    return tuple(sorted(claim_codes))


def _outside(path, parent):
    path = os.path.realpath(path)
    parent = os.path.realpath(parent)
    try:
        return os.path.commonpath((path, parent)) != parent
    except ValueError:
        return True


def _safe_staging_paths(staging_dir, inventory_path):
    staging = os.path.realpath(staging_dir)
    inventory = os.path.realpath(inventory_path)
    if not os.path.isdir(staging):
        raise PublicationError(f'private staging directory is missing: {staging}')
    if not os.path.isfile(inventory):
        raise PublicationError(f'private staging inventory is missing: {inventory}')
    if not _outside(staging, SITE) or not _outside(inventory, SITE):
        raise PublicationError('raw MLRS staging and inventory must remain outside site/')
    return staging, inventory


def _snapshot_path(staging, code, mode, entry):
    expected = f'{code.lower()}_{mode}.json'
    if not isinstance(entry, dict) or entry.get('file') != expected:
        got = entry.get('file') if isinstance(entry, dict) else None
        raise PublicationError(
            f'inventory {code}.{mode}.file must be {expected!r}, got {got!r}')
    path = os.path.realpath(os.path.join(staging, expected))
    if _outside(path, staging):
        raise PublicationError(f'inventory {code}.{mode} escapes staging directory')
    if not os.path.isfile(path):
        raise PublicationError(f'inventory {code}.{mode} file is missing: {expected}')
    return path


def _validate_inventory_entry(code, mode, entry, staging):
    allowed = {'file', 'n', 'bytes', 'sha256', 'retrieved', 'complete',
               'partial_reason'}
    if not isinstance(entry, dict) or not set(entry) <= allowed:
        raise PublicationError(
            f'inventory {code}.{mode} contains unsupported fields')
    path = _snapshot_path(staging, code, mode, entry)
    if not _is_int(entry.get('n')):
        raise PublicationError(f'inventory {code}.{mode}.n must be nonnegative')
    if not _is_int(entry.get('bytes'), 2):
        raise PublicationError(f'inventory {code}.{mode}.bytes must be positive')
    if (not isinstance(entry.get('sha256'), str) or
            SHA256_RE.fullmatch(entry['sha256']) is None):
        raise PublicationError(f'inventory {code}.{mode}.sha256 is invalid')
    _valid_date(entry.get('retrieved'), f'inventory {code}.{mode}.retrieved')
    if not isinstance(entry.get('complete'), bool):
        raise PublicationError(f'inventory {code}.{mode}.complete must be boolean')
    reason = entry.get('partial_reason')
    if not entry['complete'] and (not isinstance(reason, str) or not reason.strip()):
        raise PublicationError(
            f'inventory {code}.{mode} needs partial_reason when complete=false')
    if entry['complete'] and reason is not None:
        raise PublicationError(
            f'inventory {code}.{mode} cannot have partial_reason when complete=true')
    return path


def _load_clip_indexes(state_clips_path, expected_sha):
    actual_sha = _sha256_file(state_clips_path)
    if actual_sha != expected_sha:
        raise PublicationError(
            'inventory clip artifact sha256 does not match --state-clips')
    clips, _ = _read_strict_json(state_clips_path, 'authoritative state clips')
    if (not isinstance(clips, dict) or clips.get('schema_version') != 1 or
            not isinstance(clips.get('source'), str) or
            'tigerweb' not in clips['source'].lower() or
            'January 1 2025' not in clips['source'] or
            not isinstance(clips.get('states'), dict) or
            set(clips['states']) != set(ALL_STATES)):
        raise PublicationError(
            'state-clips artifact must be the exact 49-state Census 2025 index')
    indexes = {}
    for code in sorted(CLAIM_STATES):
        geometry = clips['states'][code]
        if (not isinstance(geometry, dict) or
                geometry.get('type') not in ('Polygon', 'MultiPolygon')):
            raise PublicationError(f'authoritative {code} clip is not a polygon')
        indexes[code] = StateClipIndex(geometry)
    return indexes, actual_sha


def load_inventory(staging_dir, inventory_path, state_clips_path):
    """Load the exact 38-snapshot contract and authoritative clip indexes."""
    staging, inventory_path = _safe_staging_paths(staging_dir, inventory_path)
    inventory, inventory_raw = _read_strict_json(
        inventory_path, 'federal MLRS staging inventory')
    required = {'schema_version', 'system', 'source', 'created', 'clip', 'states'}
    if not isinstance(inventory, dict) or set(inventory) != required:
        raise PublicationError(
            'inventory keys must be exactly schema_version/system/source/'
            'created/clip/states')
    if inventory.get('schema_version') != SCHEMA_VERSION:
        raise PublicationError(f'inventory schema_version must be {SCHEMA_VERSION}')
    if inventory.get('system') != SYSTEM or inventory.get('source') != SOURCE:
        raise PublicationError('inventory system/source is not federal BLM MLRS')
    _valid_date(inventory.get('created'), 'inventory.created')
    clip = inventory.get('clip')
    if (not isinstance(clip, dict) or
            set(clip) != {'authority', 'method', 'artifact_sha256'} or
            clip.get('authority') != CLIP_AUTHORITY or
            clip.get('method') != CLIP_METHOD or
            not isinstance(clip.get('artifact_sha256'), str) or
            SHA256_RE.fullmatch(clip['artifact_sha256']) is None):
        raise PublicationError('inventory authoritative clip provenance is invalid')
    codes = _registry_claim_states()
    states = inventory.get('states')
    if not isinstance(states, dict) or set(states) != set(codes):
        missing = sorted(set(codes) - set(states or {}))
        extra = sorted(set(states or {}) - set(codes))
        raise PublicationError(
            f'inventory states must be exact registry 19; missing={missing}, extra={extra}')
    paths = {}
    for code in codes:
        row = states[code]
        if not isinstance(row, dict) or set(row) != set(MODES):
            raise PublicationError(
                f'inventory {code} must contain exactly active and closed')
        for mode in MODES:
            paths[(code, mode)] = _validate_inventory_entry(
                code, mode, row[mode], staging)
    state_clips_path = os.path.realpath(state_clips_path)
    indexes, clip_sha = _load_clip_indexes(
        state_clips_path, clip['artifact_sha256'])
    registry = load_states()
    return {
        'staging': staging,
        'inventory_path': inventory_path,
        'inventory': inventory,
        'inventory_raw': inventory_raw,
        'inventory_sha256': _sha256_bytes(inventory_raw),
        'codes': codes,
        'paths': paths,
        'clip_indexes': indexes,
        'clip_sha256': clip_sha,
        'query_envelope_counts': {
            code: len(registry[code]['query_envelopes']) for code in codes
        },
        'state_clips_path': state_clips_path,
    }


def _finite(value, label, low=None, high=None, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PublicationError(f'{label} must be a finite number')
    result = float(value)
    if not math.isfinite(result):
        raise PublicationError(f'{label} must be a finite number')
    if low is not None and result < low or high is not None and result > high:
        raise PublicationError(f'{label} is outside [{low}, {high}]')
    return result


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


def _snapshot_provenance_complete(context, code, mode, data):
    """Validate machine-produced clip/pagination evidence when present.

    Older snapshots remain usable only in progress mode: absence returns
    false and therefore marks the rows partial. A malformed or mismatched
    attestation always fails instead of degrading silently.
    """
    pagination = data.get('pagination')
    spatial = data.get('spatial_clip')
    source = data.get('source')
    if pagination is None and spatial is None and source is None:
        return False
    if source != SOURCE:
        raise PublicationError(f'{code}.{mode} source is not federal BLM MLRS')
    expected_clip = {
        'method': CLIP_METHOD,
        'artifact_sha256': context['clip_sha256'],
        'version': f"state-centroid-{context['clip_sha256'][:16]}",
    }
    if spatial != expected_clip:
        raise PublicationError(
            f'{code}.{mode} spatial-clip evidence does not match the build clip')
    required = {
        'schema_version', 'method', 'order', 'page_size', 'pages',
        'envelopes', 'completed_envelopes', 'terminal_empty_pages', 'complete',
    }
    if not isinstance(pagination, dict) or set(pagination) != required:
        raise PublicationError(f'{code}.{mode} pagination evidence schema is invalid')
    envelope_count = context['query_envelope_counts'][code]
    complete = pagination.get('complete')
    if (pagination.get('schema_version') != 1 or
            pagination.get('method') != PAGINATION_METHOD or
            pagination.get('order') != PAGINATION_ORDER[mode] or
            pagination.get('page_size') != PAGINATION_PAGE_SIZE or
            not _is_int(pagination.get('pages')) or
            pagination.get('envelopes') != envelope_count or
            not _is_int(pagination.get('completed_envelopes')) or
            not _is_int(pagination.get('terminal_empty_pages')) or
            pagination['completed_envelopes'] > envelope_count or
            pagination['terminal_empty_pages'] != pagination['completed_envelopes'] or
            not isinstance(complete, bool)):
        raise PublicationError(f'{code}.{mode} pagination evidence is inconsistent')
    if complete and pagination['completed_envelopes'] != envelope_count:
        raise PublicationError(
            f'{code}.{mode} claims completeness without exhausting every envelope')
    if not complete and pagination['completed_envelopes'] >= envelope_count:
        raise PublicationError(
            f'{code}.{mode} incomplete pagination contradicts exhausted envelopes')
    return complete


def _snapshot_partial(context, code, mode, entry, data):
    partial = not entry['complete']
    partial = partial or not _snapshot_provenance_complete(
        context, code, mode, data)
    partial = partial or any(bool(data.get(flag)) for flag in (
        'truncated', 'partial', 'partial_after_spatial_clip'))
    total = data.get('total_available')
    if total is not None:
        if not _is_int(total):
            raise PublicationError('snapshot total_available must be nonnegative')
        partial = partial or total > data['n']
    return bool(partial)


def _load_snapshot(context, code, mode):
    entry = context['inventory']['states'][code][mode]
    path = context['paths'][(code, mode)]
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'{code}.{mode} cannot be read: {exc}') from exc
    digest = _sha256_bytes(raw)
    if len(raw) != entry['bytes'] or digest != entry['sha256']:
        raise PublicationError(
            f'{code}.{mode} bytes/sha256 differ from inventory')
    data = _strict_json_bytes(raw, f'{code}.{mode} snapshot')
    if not isinstance(data, dict):
        raise PublicationError(f'{code}.{mode} snapshot must be an object')
    unknown = set(data) - ALLOWED_ROOT_FIELDS
    if unknown:
        raise PublicationError(
            f'{code}.{mode} snapshot has unsupported fields {sorted(unknown)}')
    if data.get('state') != code or data.get('layer') != mode:
        raise PublicationError(
            f'{code}.{mode} snapshot state/layer identity is invalid')
    if data.get('retrieved') != entry['retrieved']:
        raise PublicationError(
            f'{code}.{mode} retrieved date differs from inventory')
    _valid_date(data.get('retrieved'), f'{code}.{mode}.retrieved')
    n = data.get('n')
    if not _is_int(n) or n != entry['n']:
        raise PublicationError(
            f'{code}.{mode} n={n!r} differs from inventory n={entry["n"]}')
    for column in REQUIRED_COLUMNS[mode]:
        if not isinstance(data.get(column), list):
            raise PublicationError(f'{code}.{mode}.{column} must be a column array')
    for field, values in data.items():
        if isinstance(values, list) and len(values) != n:
            raise PublicationError(
                f'{code}.{mode}.{field} has {len(values)} rows; expected {n}')
    for flag in ('truncated', 'partial', 'partial_after_spatial_clip'):
        if flag in data and not isinstance(data[flag], bool):
            raise PublicationError(f'{code}.{mode}.{flag} must be boolean')
    for field in ('total_available', 'envelope_total_upper_bound'):
        if field in data and data[field] is not None and not _is_int(data[field]):
            raise PublicationError(f'{code}.{mode}.{field} must be nonnegative')
    if ('partial_note' in data and
            (not isinstance(data['partial_note'], str) or
             not data['partial_note'].strip())):
        raise PublicationError(f'{code}.{mode}.partial_note must be nonempty text')
    return data, _snapshot_partial(context, code, mode, entry, data), {
        'bytes': len(raw), 'sha256': digest, 'n': n,
    }


def _feature_id(code, mode, serial):
    identity = f'{SYSTEM}\x1f{code}\x1f{mode}\x1f{serial}'.encode('utf-8')
    digest = hashlib.blake2b(
        identity, digest_size=8, person=b'nwmm-mlrs-v1').digest()
    return (int.from_bytes(digest, 'big') & SAFE_INTEGER_MAX) or 1


def _feature(context, code, mode, data, index, partial):
    prefix = f'{code}.{mode}[{index}]'
    serial = _text(
        data['serial'][index], f'{prefix}.serial', required=True,
        limit=TEXT_LIMITS['serial'])
    name = _text(
        data['name'][index], f'{prefix}.name', nullable=True,
        limit=TEXT_LIMITS['name'])
    claim_type = _text(
        data['type'][index], f'{prefix}.type', required=True,
        limit=TEXT_LIMITS['type'])
    longitude = _finite(data['x'][index], f'{prefix}.x', -180, 180)
    latitude = _finite(data['y'][index], f'{prefix}.y', -90, 90)
    if not context['clip_indexes'][code].contains(longitude, latitude):
        raise PublicationError(
            f'{prefix} lies outside the authoritative {code} boundary')
    for column in ('admin_state', 'geo_state'):
        _text(data[column][index], f'{prefix}.{column}', nullable=True,
              limit=TEXT_LIMITS[column])
    disposition = (_text(data['disp'][index], f'{prefix}.disp', nullable=True,
                         limit=TEXT_LIMITS['disp']) if mode == 'active' else None)
    properties = {
        'fid': _feature_id(code, mode, serial),
        'system': SYSTEM,
        'st': code,
        'serial': serial,
        'nm': name,
        'type': claim_type,
        'status': 'CLOSED' if mode == 'closed' else (disposition or 'ACTIVE'),
        'partial': 1 if partial else None,
    }
    if mode == 'active':
        properties['disp'] = disposition
        properties['acres'] = _finite(
            data['acres'][index], f'{prefix}.acres', 0, None, nullable=True)
    properties = {key: value for key, value in properties.items()
                  if value is not None}
    return serial, properties['fid'], {
        'type': 'Feature',
        'id': properties['fid'],
        'properties': properties,
        'geometry': {
            'type': 'Point',
            'coordinates': [round(longitude, 6), round(latitude, 6)],
        },
    }


def stream_snapshots(context, layer_paths, profile='release'):
    """Validate all 38 snapshots and stream active/closed GeoJSONSeq."""
    if profile not in PROFILES:
        raise PublicationError(f'unsupported profile {profile!r}')
    if set(layer_paths) != set(MODES) or len(set(layer_paths.values())) != 2:
        raise PublicationError('layer paths must provide distinct active/closed files')
    strict = profile in ('full', 'release')
    counts = {mode: {code: 0 for code in context['codes']} for mode in MODES}
    totals = {mode: 0 for mode in MODES}
    partial_states = set()
    partial_snapshots = []
    input_stats = {}
    feature_ids = set()
    feature_ids_by_mode = {mode: [] for mode in MODES}
    claim_statuses = set()
    outputs = {mode: open(layer_paths[mode], 'w', encoding='utf-8')
               for mode in MODES}
    try:
        for code in context['codes']:
            for mode in MODES:
                data, partial, file_stats = _load_snapshot(context, code, mode)
                key = f'{code.lower()}_{mode}'
                input_stats[key] = file_stats
                if strict and partial:
                    raise PublicationError(
                        f'{code}.{mode} is partial/capped; {profile} publication forbids it')
                serials = set()
                for index in range(data['n']):
                    serial, feature_id, feature = _feature(
                        context, code, mode, data, index, partial)
                    if serial in serials:
                        raise PublicationError(
                            f'{code}.{mode} duplicates serial {serial!r}')
                    serials.add(serial)
                    claim_identity = (code, serial)
                    if claim_identity in claim_statuses:
                        raise PublicationError(
                            f'{code} serial {serial!r} occurs in both active and closed')
                    claim_statuses.add(claim_identity)
                    if feature_id in feature_ids:
                        raise PublicationError(
                            f'stable feature-ID collision at {code}.{mode} {serial!r}')
                    feature_ids.add(feature_id)
                    feature_ids_by_mode[mode].append(feature_id)
                    json.dump(feature, outputs[mode], separators=(',', ':'),
                              allow_nan=False)
                    outputs[mode].write('\n')
                counts[mode][code] = data['n']
                totals[mode] += data['n']
                if partial:
                    partial_states.add(code)
                    partial_snapshots.append(key)
    finally:
        for output in outputs.values():
            output.close()
    # Individual thin states may be true zeroes. The national archive still
    # needs both real MVT layers; a wholly empty status feed is not publishable.
    for mode in MODES:
        if totals[mode] <= 0:
            raise PublicationError(
                f'national {mode} feed is empty; cannot create required MVT layer')
    states = {code: sum(counts[mode][code] for mode in MODES)
              for code in context['codes']}
    return {
        'n': sum(totals.values()),
        'states': states,
        'by_mode': {mode: {'n': totals[mode], 'states': counts[mode]}
                    for mode in MODES},
        'zero_states': {
            mode: [code for code, n in counts[mode].items() if n == 0]
            for mode in MODES
        },
        'partial_states': sorted(partial_states),
        'partial_snapshots': sorted(partial_snapshots),
        'inputs': input_stats,
        # Private in-memory build input for the post-Tippecanoe audit. The
        # publication pointer records only counts and canonical ID digests.
        '_source_ids': {
            mode: sorted(feature_ids_by_mode[mode]) for mode in MODES
        },
    }


def assert_inputs_unchanged(context, stats):
    """Rehash inventory and all 38 files immediately before publication."""
    try:
        with open(context['inventory_path'], 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot re-read staging inventory: {exc}') from exc
    if (raw != context['inventory_raw'] or
            _sha256_bytes(raw) != context['inventory_sha256']):
        raise PublicationError('staging inventory changed during build')
    if _sha256_file(context['state_clips_path']) != context['clip_sha256']:
        raise PublicationError('authoritative state clips changed during build')
    for code in context['codes']:
        for mode in MODES:
            key = f'{code.lower()}_{mode}'
            path = context['paths'][(code, mode)]
            expected = stats['inputs'][key]
            try:
                size = os.path.getsize(path)
                digest = _sha256_file(path)
            except OSError as exc:
                raise PublicationError(
                    f'{code}.{mode} changed/disappeared during build: {exc}') from exc
            if size != expected['bytes'] or digest != expected['sha256']:
                raise PublicationError(f'{code}.{mode} changed during build')


def _decompress(data, compression):
    if compression == 1:
        return data
    if compression == 2:
        return gzip.decompress(data)
    raise PublicationError(
        f'unsupported PMTiles internal compression {compression}')


def _source_id_inventory(expected_by_layer, tiled_by_layer,
                         maxzoom_instances_by_layer):
    if not isinstance(expected_by_layer, dict) or set(expected_by_layer) != set(MODES):
        raise PublicationError('MLRS source-ID inventory must declare active and closed')
    by_layer = {}
    combined = []
    for mode in MODES:
        expected = sorted(expected_by_layer[mode])
        observed = sorted(tiled_by_layer.get(mode, []))
        if (not expected or
                any(not isinstance(value, int) or isinstance(value, bool) or
                    value <= 0 or value > SAFE_INTEGER_MAX for value in expected) or
                len(expected) != len(set(expected))):
            raise PublicationError(
                f'MLRS {mode} normalized source IDs are invalid or duplicated')
        if observed != expected:
            expected_set, observed_set = set(expected), set(observed)
            raise PublicationError(
                f'MLRS {mode} PMTiles source-ID reconciliation failed; '
                f'missing={sorted(expected_set - observed_set)[:20]}, '
                f'extra={sorted(observed_set - expected_set)[:20]}')
        instances = maxzoom_instances_by_layer.get(mode, 0)
        # A buffered point on a tile seam can have multiple MVT instances. The
        # exact lossless contract is the unique top-level ID set, while instance
        # count is recorded as a diagnostic and must be no smaller than source.
        if not isinstance(instances, int) or instances < len(expected):
            raise PublicationError(
                f'MLRS {mode} max-zoom feature instances do not cover the source; '
                f'instances={instances}, source_records={len(expected)}')
        digest = hashlib.sha256(json.dumps(
            expected, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        by_layer[mode] = {
            'source_records': len(expected),
            'maxzoom_feature_instances': instances,
            'maxzoom_unique_tiled_ids': len(observed),
            'ids_sha256': digest,
        }
        combined.extend(expected)
    combined.sort()
    if len(combined) != len(set(combined)):
        raise PublicationError('MLRS stable IDs collide across active/closed layers')
    digest = hashlib.sha256(json.dumps(
        combined, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return {
        'status': 'complete_at_retrieval',
        'source_records': len(combined),
        'maxzoom_feature_instances': sum(
            row['maxzoom_feature_instances'] for row in by_layer.values()),
        'maxzoom_unique_tiled_ids': sum(
            row['maxzoom_unique_tiled_ids'] for row in by_layer.values()),
        'ids_sha256': digest,
        'by_layer': by_layer,
    }


def validate_pmtiles(path, expected_ids_by_layer=None):
    """Validate PMTiles v3 structure plus exact source layers/properties."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as archive:
            header = archive.read(127)
    except OSError as exc:
        raise PublicationError(f'cannot read PMTiles archive: {exc}') from exc
    if len(header) != 127 or header[:8] != b'PMTiles\x03':
        raise PublicationError('tippecanoe output is not PMTiles v3')
    values = struct.unpack_from('<11Q', header, 8)
    (root_offset, root_length, metadata_offset, metadata_length,
     leaf_offset, leaf_length, tile_offset, tile_length,
     addressed, entries, contents) = values
    ranges = [(root_offset, root_length, 'root'),
              (metadata_offset, metadata_length, 'metadata'),
              (tile_offset, tile_length, 'tiles')]
    if leaf_length:
        ranges.append((leaf_offset, leaf_length, 'leaves'))
    for offset, length, label in ranges:
        if offset < 127 or length <= 0 or offset + length > size:
            raise PublicationError(f'PMTiles {label} range is invalid')
    ordered = sorted(ranges)
    if any(left[0] + left[1] > right[0]
           for left, right in zip(ordered, ordered[1:])):
        raise PublicationError('PMTiles ranges overlap')
    if not (addressed > 0 and entries > 0 and contents > 0 and
            addressed >= entries >= contents):
        raise PublicationError('PMTiles directory counts are invalid')
    if header[99] != 1 or header[100] > header[101] or header[101] > 24:
        raise PublicationError('PMTiles vector type/zoom range is invalid')
    with open(path, 'rb') as archive:
        archive.seek(metadata_offset)
        metadata_raw = archive.read(metadata_length)
    metadata = _strict_json_bytes(
        _decompress(metadata_raw, header[97]), 'PMTiles metadata')
    layers = metadata.get('vector_layers') if isinstance(metadata, dict) else None
    if not isinstance(layers, list):
        raise PublicationError('PMTiles vector layer metadata is missing')
    indexed = {layer.get('id'): layer for layer in layers
               if isinstance(layer, dict) and isinstance(layer.get('id'), str)}
    if set(indexed) != set(MODES):
        raise PublicationError(
            f'PMTiles source layers must be exactly {list(MODES)}')
    required_fields = {'system', 'st', 'serial', 'type', 'status'}
    for mode in MODES:
        fields = indexed[mode].get('fields')
        if not isinstance(fields, dict) or not required_fields <= set(fields):
            raise PublicationError(
                f'PMTiles {mode} layer lacks required identity fields')
    result = {'bytes': size, 'sha256': _sha256_file(path),
              'tile_entries': entries, 'tile_contents': contents,
              'source_layers': list(MODES)}
    if expected_ids_by_layer is not None:
        try:
            semantic = _pmtiles_header(
                path, list(MODES),
                {mode: ['system', 'st', 'serial', 'type', 'status']
                 for mode in MODES},
                verify_feature_properties=True, collect_feature_ids=True)
        except (OSError, ValueError) as exc:
            raise PublicationError(
                f'MLRS PMTiles semantic validation failed: {exc}') from exc
        result['source_id_inventory'] = _source_id_inventory(
            expected_ids_by_layer,
            semantic.get('maxzoom_feature_ids', {}),
            semantic.get('maxzoom_feature_instances', {}))
    return result


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some filesystems do not permit directory fsync. File fsync and
        # atomic replace still preserve pointer safety on those platforms.
        pass


def install_immutable(source, publish_dir):
    """Install a validated content-addressed archive without overwriting it."""
    publish_dir = os.path.realpath(publish_dir)
    os.makedirs(publish_dir, exist_ok=True)
    source_meta = validate_pmtiles(source)
    name = f'federal-mlrs-{source_meta["sha256"][:20]}.pmtiles'
    destination = os.path.join(publish_dir, name)
    if os.path.exists(destination):
        existing = validate_pmtiles(destination)
        if existing['sha256'] != source_meta['sha256']:
            raise PublicationError(
                f'content-addressed destination collision at {name}')
        return destination, existing
    handle, pending = tempfile.mkstemp(
        prefix='.federal-mlrs-', suffix='.tmp', dir=publish_dir)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as output, open(source, 'rb') as archive:
            shutil.copyfileobj(archive, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        installed_meta = validate_pmtiles(pending)
        if installed_meta['sha256'] != source_meta['sha256']:
            raise PublicationError('archive changed while copying for publication')
        # Link gives an atomic no-overwrite install. A concurrent identical
        # build may win; in that case validate and reuse its immutable bytes.
        try:
            os.link(pending, destination)
        except FileExistsError:
            existing = validate_pmtiles(destination)
            if existing['sha256'] != source_meta['sha256']:
                raise PublicationError(
                    f'content-addressed destination collision at {name}')
            os.unlink(pending)
            return destination, existing
        os.unlink(pending)
        _fsync_directory(publish_dir)
        return destination, installed_meta
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _latest_entry(context, stats, archive_path, archive_meta, latest_path,
                  profile):
    retrieved = sorted({
        context['inventory']['states'][code][mode]['retrieved']
        for code in context['codes'] for mode in MODES
    })
    return {
        'system': SYSTEM,
        'source': SOURCE,
        'format': 'pmtiles',
        'file': os.path.relpath(archive_path, os.path.dirname(latest_path)),
        'source_layers': list(MODES),
        'n': stats['n'],
        'states': stats['states'],
        'by_mode': stats['by_mode'],
        'zero_states': stats['zero_states'],
        'partial_states': stats['partial_states'],
        'partial_snapshots': stats['partial_snapshots'],
        'profile': profile,
        'inventory_created': context['inventory']['created'],
        'inventory_sha256': context['inventory_sha256'],
        'inputs': stats['inputs'],
        'clip': {
            'authority': CLIP_AUTHORITY,
            'method': CLIP_METHOD,
            'artifact_sha256': context['clip_sha256'],
        },
        'retrieved': {'first': retrieved[0], 'last': retrieved[-1]},
        'bytes': archive_meta['bytes'],
        'sha256': archive_meta['sha256'],
        'source_id_inventory': archive_meta['source_id_inventory'],
    }


def merge_latest(latest_path, entry):
    """Merge under an advisory lock, preserving the newest unrelated data."""
    latest_path = os.path.realpath(latest_path)
    directory = os.path.dirname(latest_path)
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, '.federal-mlrs-latest.lock')
    with open(lock_path, 'a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if os.path.exists(latest_path):
            latest, _ = _read_strict_json(latest_path, 'latest publication manifest')
        else:
            latest = {'schema_version': 1, 'artifacts': {}}
        if not isinstance(latest, dict) or latest.get('schema_version') != 1:
            raise PublicationError('latest manifest must be a schema_version 1 object')
        artifacts = latest.get('artifacts')
        if not isinstance(artifacts, dict):
            raise PublicationError('latest manifest artifacts must be an object')
        artifacts[SYSTEM] = entry
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
          state_clips=DEFAULT_STATE_CLIPS, profile='release',
          tippecanoe='tippecanoe'):
    """Build, validate, install, and atomically point to one MLRS release."""
    if profile not in PROFILES:
        raise PublicationError(f'profile must be one of {PROFILES}')
    executable = shutil.which(tippecanoe)
    if not executable:
        raise PublicationError('tippecanoe >=2.79 with PMTiles output is required')
    context = load_inventory(staging_dir, inventory_path, state_clips)
    publish_dir = os.path.realpath(publish_dir)
    if not _outside(publish_dir, context['staging']):
        raise PublicationError('publish directory must be outside raw staging')
    default_pointer = ('latest.json' if profile in ('full', 'release')
                       else 'progress.json')
    latest_manifest = os.path.realpath(
        latest_manifest or os.path.join(publish_dir, default_pointer))
    if _outside(latest_manifest, publish_dir):
        raise PublicationError('latest manifest must remain inside publish directory')
    with tempfile.TemporaryDirectory(prefix='nwmm-federal-mlrs-') as temporary:
        layer_paths = {mode: os.path.join(temporary, f'{mode}.geojsonseq')
                       for mode in MODES}
        pending_archive = os.path.join(temporary, 'federal-mlrs.pmtiles')
        stats = stream_snapshots(context, layer_paths, profile=profile)
        description = json.dumps({
            'schema': 'nwmm-federal-mlrs-v1',
            'inventory_sha256': context['inventory_sha256'],
            'features': stats['n'],
            'states': len(context['codes']),
            'profile': profile,
        }, separators=(',', ':'))
        command = [
            executable, '--force', '--output', pending_archive,
            '--minimum-zoom=0', '--maximum-zoom=13',
            # All stable IDs must survive at z13. Tippecanoe may perform its
            # normal deterministic lower-zoom sampling, but as-needed density
            # and tile-size limits may not silently alter the max-zoom corpus.
            '--base-zoom=13', '--no-feature-limit', '--no-tile-size-limit',
            '--read-parallel', '--preserve-input-order', '--quiet',
            '--use-attribute-for-id=fid', '--exclude=fid',
            f'--description={description}',
            '--attribution=U.S. Bureau of Land Management MLRS',
        ]
        for mode in MODES:
            command.extend(('-L', f'{mode}:{layer_paths[mode]}'))
        subprocess.run(command, check=True)
        pending_meta = validate_pmtiles(pending_archive, stats['_source_ids'])
        if pending_meta['source_id_inventory']['source_records'] != stats['n']:
            raise PublicationError(
                'MLRS tiled source-ID inventory differs from normalized source count')
        assert_inputs_unchanged(context, stats)
        archive_path, archive_meta = install_immutable(
            pending_archive, publish_dir)
        # install_immutable binds the installed file to the pending SHA-256; the
        # expensive decoded-ID scan therefore applies to those identical bytes.
        archive_meta['source_id_inventory'] = pending_meta['source_id_inventory']
    # Recheck after the potentially long archive copy and before changing the
    # only publication pointer. If this fails, the immutable archive is merely
    # an unreferenced, safe-to-garbage-collect generation.
    assert_inputs_unchanged(context, stats)
    entry = _latest_entry(
        context, stats, archive_path, archive_meta, latest_manifest, profile)
    merge_latest(latest_manifest, entry)
    return {
        'artifact': archive_path,
        'latest_manifest': latest_manifest,
        'entry': entry,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build immutable federal MLRS PMTiles from private staging')
    parser.add_argument('--staging-dir', required=True,
                        help='private directory containing <state>_<mode>.json')
    parser.add_argument('--inventory', required=True,
                        help='strict checksum-pinned 19-state inventory JSON')
    parser.add_argument('--publish-dir', required=True,
                        help='destination for content-addressed PMTiles')
    parser.add_argument('--latest-manifest',
                        help='atomic publication pointer (default: PUBLISH/latest.json)')
    parser.add_argument('--state-clips', default=DEFAULT_STATE_CLIPS,
                        help='authoritative 49-state Census clip artifact')
    parser.add_argument('--profile', choices=PROFILES, default='release',
                        help='release/full reject partial inputs; progress labels them')
    parser.add_argument('--tippecanoe', default='tippecanoe')
    args = parser.parse_args(argv)
    try:
        result = build(
            args.staging_dir, args.inventory, args.publish_dir,
            latest_manifest=args.latest_manifest,
            state_clips=args.state_clips, profile=args.profile,
            tippecanoe=args.tippecanoe)
    except (PublicationError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
