#!/usr/bin/env python3
"""Migrate private state-survey snapshots into one PMTiles archive.

The compatibility inputs remain the per-state ``stategeo_<st>`` entries in
``build-inputs/manifest.json``.  This builder validates those snapshots as a
single publication unit and streams their rows to a temporary GeoJSON
sequence.  The only browser artifact it writes is
``site/data/tiles/national/stategeo.pmtiles``; a nationwide or statewide
GeoJSON file is never placed beneath ``site/``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile

from common import TODAY
from build_inputs import (BUILD_INPUTS, MANIFEST as BUILD_MANIFEST,
                          artifact_path, load_manifest as load_build_manifest)
from state_registry import ALL_STATES


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
OUT = os.path.join(SITE, 'data', 'tiles', 'national', 'stategeo.pmtiles')

SOURCE_LAYER = 'stategeo'
KEY_RE = re.compile(r'^stategeo_([a-z]{2})$')
REQUIRED_COLUMNS = ('id', 'nm', 'c', 'ty', 'stx', 'g', 'x', 'y')
TEXT_LIMITS = {'id': 160, 'nm': 512, 'c': 512, 'ty': 256, 'stx': 256}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest():
    try:
        manifest = load_build_manifest(BUILD_MANIFEST, root=BUILD_INPUTS)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f'cannot read build-input manifest {BUILD_MANIFEST}: {exc}') from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get('sites'), dict):
        raise RuntimeError('manifest.sites must be an object')
    return manifest


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f'{label} must be a positive integer')
    return value


def _discover_inputs(manifest):
    """Return canonical, state-sorted stategeo inputs declared by manifest."""
    inputs = []
    states = set()
    for key, entry in sorted(manifest['sites'].items()):
        if not key.startswith('stategeo_'):
            continue
        match = KEY_RE.fullmatch(key)
        if not match:
            raise RuntimeError(f'invalid stategeo manifest key {key!r}')
        state = match.group(1).upper()
        if state not in ALL_STATES:
            raise RuntimeError(f'{key} identifies unsupported WS11 state {state!r}')
        if state in states:
            raise RuntimeError(f'manifest contains duplicate stategeo state {state}')
        if not isinstance(entry, dict):
            raise RuntimeError(f'manifest.sites.{key} must be an object')
        expected_file = f'data/sites/{key}.json'
        if entry.get('file') != expected_file:
            raise RuntimeError(
                f'manifest.sites.{key}.file must be {expected_file!r}')
        count = _positive_int(entry.get('n'), f'manifest.sites.{key}.n')
        try:
            path = artifact_path('sites', key, entry, root=BUILD_INPUTS)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        states.add(state)
        inputs.append({
            'key': key,
            'state': state,
            'file': expected_file,
            'path': path,
            'n': count,
            'manifest_entry': entry,
        })
    if not inputs:
        raise RuntimeError('manifest.sites has no stategeo_<st> inputs')
    return sorted(inputs, key=lambda item: item['state'])


def _load_columns(item):
    try:
        with open(item['path'], 'rb') as source:
            raw = source.read()
        columns = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'{item["key"]} is not readable JSON: {exc}') from exc
    if not isinstance(columns, dict):
        raise RuntimeError(f'{item["key"]} root must be an object')
    if columns.get('src') != SOURCE_LAYER:
        raise RuntimeError(f'{item["key"]}.src must equal {SOURCE_LAYER!r}')
    if columns.get('state') != item['state']:
        raise RuntimeError(
            f'{item["key"]}.state must equal {item["state"]!r}')
    if columns.get('n') != item['n']:
        raise RuntimeError(
            f'{item["key"]}.n={columns.get("n")!r} does not match '
            f'manifest count {item["n"]}')
    for field in ('source', 'retrieved'):
        if not isinstance(columns.get(field), str) or not columns[field].strip():
            raise RuntimeError(f'{item["key"]}.{field} must be non-empty text')
    missing = [field for field in REQUIRED_COLUMNS if field not in columns]
    if missing:
        raise RuntimeError(f'{item["key"]} is missing columns: {missing}')
    for field, values in columns.items():
        if isinstance(values, list) and len(values) != item['n']:
            raise RuntimeError(
                f'{item["key"]}.{field} has {len(values)} rows; '
                f'expected {item["n"]}')
    for field in REQUIRED_COLUMNS:
        if not isinstance(columns[field], list):
            raise RuntimeError(f'{item["key"]}.{field} must be an array')
    return columns, hashlib.sha256(raw).hexdigest(), len(raw)


def _row_text(columns, field, index, item, required=False):
    value = columns[field][index]
    if field == 'id' and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise RuntimeError(
            f'{item["key"]} row {index} {field} must be text')
    value = value.strip()
    if required and not value:
        raise RuntimeError(
            f'{item["key"]} row {index} {field} must not be empty')
    if len(value) > TEXT_LIMITS[field]:
        raise RuntimeError(
            f'{item["key"]} row {index} {field} exceeds '
            f'{TEXT_LIMITS[field]} characters')
    return value


def _coordinate(value, axis, index, item):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(
            f'{item["key"]} row {index} {axis} must be numeric')
    value = float(value)
    limit = 180 if axis == 'x' else 90
    if not math.isfinite(value) or not -limit <= value <= limit:
        raise RuntimeError(
            f'{item["key"]} row {index} {axis} is not a finite '
            f'world coordinate')
    return round(value, 6)


def _is_existing(status_text):
    value = status_text.lower()
    return int(
        bool(re.search(r'\b(producer|active)\b', value)) and
        not bool(re.search(r'\b(past|inactive)\b', value)))


def _normalize_row(columns, index, item, feature_id):
    record_id = _row_text(columns, 'id', index, item, required=True)
    name = _row_text(columns, 'nm', index, item, required=True)
    commodities = _row_text(columns, 'c', index, item)
    feature_type = _row_text(columns, 'ty', index, item)
    status_text = _row_text(columns, 'stx', index, item)
    group = columns['g'][index]
    if (isinstance(group, bool) or not isinstance(group, int) or
            not 0 <= group <= 5):
        raise RuntimeError(
            f'{item["key"]} row {index} g must be an integer from 0 to 5')
    longitude = _coordinate(columns['x'][index], 'x', index, item)
    latitude = _coordinate(columns['y'][index], 'y', index, item)
    properties = {
        'fid': feature_id,
        'st': item['state'],
        'nm': name,
        'id': record_id,
        'g': group,
        'ex': _is_existing(status_text),
    }
    if status_text:
        properties['status'] = status_text
    if commodities:
        properties['commodities'] = commodities
    if feature_type:
        properties['typ'] = feature_type
    return record_id, {
        'type': 'Feature',
        'id': feature_id,
        'properties': properties,
        'geometry': {
            'type': 'Point',
            'coordinates': [longitude, latitude],
        },
    }


def _stream_stategeo(path, inputs):
    """Write checked GeoJSONSeq and return counts plus input provenance."""
    counts = {}
    sources = {}
    emitted = 0
    with open(path, 'w', encoding='utf-8') as output:
        for item in inputs:
            columns, input_sha256, input_bytes = _load_columns(item)
            seen_ids = set()
            for index in range(item['n']):
                record_id, feature = _normalize_row(
                    columns, index, item, emitted + 1)
                if record_id in seen_ids:
                    raise RuntimeError(
                        f'{item["key"]} has duplicate record id {record_id!r}')
                seen_ids.add(record_id)
                json.dump(feature, output, separators=(',', ':'))
                output.write('\n')
                emitted += 1
            counts[item['state']] = item['n']
            sources[item['state']] = {
                'manifest_key': item['key'],
                'build_input': item['key'],
                'n': item['n'],
                'source': columns['source'].strip(),
                'retrieved': columns['retrieved'].strip(),
                'bytes': input_bytes,
                'sha256': input_sha256,
            }
    expected = sum(item['n'] for item in inputs)
    if emitted != expected:
        raise RuntimeError(
            f'stategeo emitted {emitted:,} rows; expected {expected:,}')
    return {'n': emitted, 'states': counts, 'sources': sources}


def _validate_pmtiles(path):
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as archive:
            header = archive.read(8)
    except OSError as exc:
        raise RuntimeError(f'tippecanoe did not create {path}') from exc
    if header != b'PMTiles\x03' or size < 127:
        raise RuntimeError(f'{path} is not a valid PMTiles v3 archive')


def _assert_inputs_unchanged(inputs, manifest, stats):
    current = _discover_inputs(manifest)
    before = [(item['key'], item['manifest_entry']) for item in inputs]
    after = [(item['key'], item['manifest_entry']) for item in current]
    if after != before:
        raise RuntimeError(
            'manifest stategeo inputs changed during the PMTiles build; retry')
    for item in current:
        expected = stats['sources'][item['state']]
        if (os.path.getsize(item['path']) != expected['bytes'] or
                _sha256(item['path']) != expected['sha256']):
            raise RuntimeError(
                f'{item["key"]} changed during the PMTiles build; retry')


def _stamp_manifest(manifest, stats, byte_count, sha256):
    manifest.setdefault('national_baselines', {})[SOURCE_LAYER] = {
        'file': 'data/tiles/national/stategeo.pmtiles',
        'format': 'pmtiles',
        'source_layer': SOURCE_LAYER,
        'n': stats['n'],
        'states': stats['states'],
        'retrieved': TODAY,
        'source': 'Validated legacy state geological-survey snapshots',
        'sources': stats['sources'],
        'bytes': byte_count,
        'sha256': sha256,
    }
    directory = os.path.dirname(MANIFEST)
    mode = stat.S_IMODE(os.stat(MANIFEST).st_mode)
    handle, pending = tempfile.mkstemp(prefix='.manifest-stategeo-', dir=directory)
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, MANIFEST)
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def build():
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe >=2.79 with PMTiles output is required')
    initial_manifest = _read_manifest()
    inputs = _discover_inputs(initial_manifest)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nwmm-stategeo-') as temporary:
        sequence = os.path.join(temporary, 'stategeo.geojsonseq')
        pending_archive = os.path.join(temporary, 'stategeo.pmtiles')
        stats = _stream_stategeo(sequence, inputs)
        subprocess.run([
            'tippecanoe', '--force', '--output', pending_archive,
            '--minimum-zoom=0', '--maximum-zoom=13',
            '--drop-densest-as-needed',
            '--use-attribute-for-id=fid', '--read-parallel', '--quiet',
            '--attribution=State geological surveys; see manifest provenance',
            '-L', f'{SOURCE_LAYER}:{sequence}',
        ], check=True)
        _validate_pmtiles(pending_archive)
        byte_count = os.path.getsize(pending_archive)
        sha256 = _sha256(pending_archive)
        latest_inputs = _read_manifest()
        _assert_inputs_unchanged(inputs, latest_inputs, stats)
        os.replace(pending_archive, OUT)
    with open(MANIFEST, encoding='utf-8') as source:
        public_manifest = json.load(source)
    _stamp_manifest(public_manifest, stats, byte_count, sha256)
    result = {
        'artifact': os.path.relpath(OUT, SITE),
        'source_layer': SOURCE_LAYER,
        'features': stats['n'],
        'states': len(stats['states']),
        'bytes': byte_count,
        'sha256': sha256,
    }
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    build()
