#!/usr/bin/env python3
"""Migrate private legacy claim snapshots into national PMTiles.

The historical columnar snapshots and their inventory live beneath
``build-inputs/``, outside the static browser root.  The only browser artifact
is a range-readable PMTiles archive.  Temporary GeoJSON sequences are created
outside ``site/`` and removed after tippecanoe finishes.

This builder is intentionally strict.  A stale manifest count, a mismatched
state/layer identity, a ragged column, a duplicate claim serial, or an invalid
coordinate aborts the build before either the PMTiles archive or manifest is
published.
"""
from __future__ import annotations

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
import tempfile

from common import TODAY
from build_inputs import (BUILD_INPUTS, MANIFEST as BUILD_MANIFEST,
                          artifact_path, load_manifest as load_build_manifest)
from state_registry import CLAIM_STATES


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
OUT = os.path.join(SITE, 'data', 'tiles', 'national', 'claims.pmtiles')

SOURCE = ('https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/'
          'MiningClaims/MapServer')
MODES = ('active', 'closed')
KEY_RE = re.compile(r'^([a-z]{2})_(active|closed)$')
REQUIRED_COLUMNS = ('serial', 'name', 'type', 'x', 'y')
OPTIONAL_COLUMNS = ('disp', 'acres')
SAFE_INTEGER_MAX = (1 << 53) - 1
EXPECTED_SOURCE_LAYERS = frozenset(MODES)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _feature_id(state, mode, serial):
    """Return a stable, collision-checkable integer safe in JavaScript."""
    identity = f'{state}\x1f{mode}\x1f{serial}'.encode('utf-8')
    digest = hashlib.blake2b(
        identity, digest_size=8, person=b'nwmm-claim-v1').digest()
    value = int.from_bytes(digest, 'big') & SAFE_INTEGER_MAX
    # Zero is legal in MVT, but avoiding it catches missing/unset IDs in more
    # readers and keeps feature-state handling unambiguous.
    return value or 1


def _finite_number(value, label, *, minimum=None, maximum=None, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a finite number')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    if minimum is not None and value < minimum:
        raise ValueError(f'{label} is outside [{minimum}, {maximum}]')
    if maximum is not None and value > maximum:
        raise ValueError(f'{label} is outside [{minimum}, {maximum}]')
    return value


def _optional_text(value, label):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string or null')
    value = value.strip()
    return value or None


def _snapshot_is_partial(entry, data, n):
    """Preserve every known legacy incompleteness signal."""
    partial = False
    for source in (entry, data):
        partial = partial or any(bool(source.get(flag)) for flag in (
            'partial', 'partial_after_spatial_clip', 'truncated'))
        if 'total_available' in source and source['total_available'] is not None:
            total = source['total_available']
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError(f'total_available must be a nonnegative integer, got {total!r}')
            partial = partial or total > n
    return bool(partial)


def _resolve_snapshot(key, entry):
    expected = f'data/claims/{key}.json'
    relative = entry.get('file') if isinstance(entry, dict) else None
    if relative != expected:
        raise ValueError(f'claims.{key}.file must be {expected!r}, got {relative!r}')
    try:
        return artifact_path('claims', key, entry, root=BUILD_INPUTS)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _load_snapshot(key, entry, state, mode):
    path = _resolve_snapshot(key, entry)
    try:
        with open(path, encoding='utf-8') as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'claims.{key} is not readable JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise ValueError(f'claims.{key} must contain a JSON object')
    if data.get('state') != state:
        raise ValueError(
            f'claims.{key} state identity is {data.get("state")!r}, expected {state!r}')
    if data.get('layer') != mode:
        raise ValueError(
            f'claims.{key} layer identity is {data.get("layer")!r}, expected {mode!r}')
    n = data.get('n')
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f'claims.{key}.n must be a nonnegative integer')
    manifest_n = entry.get('n')
    if isinstance(manifest_n, bool) or not isinstance(manifest_n, int):
        raise ValueError(f'manifest claims.{key}.n must be an integer')
    if manifest_n != n:
        raise ValueError(
            f'claims.{key} manifest n={manifest_n} does not match artifact n={n}')
    for column in REQUIRED_COLUMNS:
        if not isinstance(data.get(column), list):
            raise ValueError(f'claims.{key}.{column} must be a column array')
    # Unknown arrays are still columnar data and must align.  This catches a
    # future schema addition without silently assigning values to wrong rows.
    for column, values in data.items():
        if isinstance(values, list) and len(values) != n:
            raise ValueError(
                f'claims.{key}.{column} has {len(values)} rows; expected n={n}')
    for column in OPTIONAL_COLUMNS:
        if column in data and not isinstance(data[column], list):
            raise ValueError(f'claims.{key}.{column} must be a column array')
    return data, _snapshot_is_partial(entry, data, n)


def _claim_feature(key, data, state, mode, index, partial):
    serial = data['serial'][index]
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError(f'claims.{key}.serial[{index}] must be a nonempty string')
    serial = serial.strip()
    longitude = _finite_number(
        data['x'][index], f'claims.{key}.x[{index}]', minimum=-180, maximum=180)
    latitude = _finite_number(
        data['y'][index], f'claims.{key}.y[{index}]', minimum=-90, maximum=90)
    properties = {
        'st': state,
        'serial': serial,
        'nm': _optional_text(data['name'][index], f'claims.{key}.name[{index}]'),
        'type': _optional_text(data['type'][index], f'claims.{key}.type[{index}]'),
        'disp': _optional_text(
            data['disp'][index], f'claims.{key}.disp[{index}]')
            if 'disp' in data else None,
        'acres': _finite_number(
            data['acres'][index], f'claims.{key}.acres[{index}]', nullable=True)
            if 'acres' in data else None,
        'partial': 1 if partial else None,
    }
    properties = {name: value for name, value in properties.items() if value is not None}
    return serial, {
        'type': 'Feature',
        'id': _feature_id(state, mode, serial),
        'properties': properties,
        'geometry': {'type': 'Point', 'coordinates': [longitude, latitude]},
    }


def _stream_claims(manifest, layer_paths):
    """Write two temporary GeoJSON sequences and return verified counts."""
    if set(layer_paths) != set(MODES) or len(set(layer_paths.values())) != len(MODES):
        raise ValueError('layer_paths must provide distinct active and closed files')
    claims = manifest.get('claims')
    if not isinstance(claims, dict):
        raise ValueError('manifest claims must be an object')
    selected = []
    for key, entry in claims.items():
        match = KEY_RE.fullmatch(key)
        if not match:
            continue
        state, mode = match.group(1).upper(), match.group(2)
        if state not in CLAIM_STATES:
            raise ValueError(f'claims.{key} belongs to non-claim state {state}')
        if not isinstance(entry, dict):
            raise ValueError(f'manifest claims.{key} must be an object')
        selected.append((key, entry, state, mode))
    if not selected:
        raise ValueError('manifest declares no *_active/_closed claim snapshots')

    counts = {mode: {} for mode in MODES}
    totals = {mode: 0 for mode in MODES}
    partial_states = set()
    partial_snapshots = []
    feature_ids = set()
    outputs = {mode: open(layer_paths[mode], 'w', encoding='utf-8')
               for mode in MODES}
    try:
        for key, entry, state, mode in sorted(selected):
            data, partial = _load_snapshot(key, entry, state, mode)
            serials = set()
            for index in range(data['n']):
                serial, feature = _claim_feature(
                    key, data, state, mode, index, partial)
                if serial in serials:
                    raise ValueError(
                        f'claims.{key} has duplicate serial identity {serial!r}')
                serials.add(serial)
                feature_id = feature['id']
                if feature_id in feature_ids:
                    raise ValueError(
                        f'deterministic feature ID collision at claims.{key} serial {serial!r}')
                feature_ids.add(feature_id)
                json.dump(feature, outputs[mode], separators=(',', ':'), allow_nan=False)
                outputs[mode].write('\n')
            counts[mode][state] = data['n']
            totals[mode] += data['n']
            if partial:
                partial_states.add(state)
                partial_snapshots.append(key)
    finally:
        for output in outputs.values():
            output.close()
    for mode in MODES:
        if totals[mode] <= 0:
            raise ValueError(f'manifest has no rows for the {mode} source layer')
        counts[mode] = dict(sorted(counts[mode].items()))
    state_totals = {
        state: sum(counts[mode].get(state, 0) for mode in MODES)
        for state in sorted(set().union(*(counts[mode] for mode in MODES)))
    }
    return {
        'n': sum(totals.values()),
        'states': state_totals,
        'by_mode': {
            mode: {'n': totals[mode], 'states': counts[mode]}
            for mode in MODES
        },
        'snapshots': len(selected),
        'partial_states': sorted(partial_states),
        'partial_snapshots': sorted(partial_snapshots),
    }


def _decompress_metadata(data, compression):
    if compression == 1:
        return data
    if compression == 2:
        return gzip.decompress(data)
    raise ValueError(f'unsupported PMTiles internal compression {compression}')


def _validate_pmtiles(path):
    """Validate the PMTiles v3 header, nonempty ranges, and source layers."""
    try:
        with open(path, 'rb') as archive:
            header = archive.read(127)
    except OSError as exc:
        raise ValueError(f'tippecanoe did not create {path}') from exc
    if len(header) != 127 or header[:8] != b'PMTiles\x03':
        raise ValueError(f'{path} is not a PMTiles v3 archive')
    size = os.path.getsize(path)
    values = struct.unpack_from('<11Q', header, 8)
    (root_offset, root_length, metadata_offset, metadata_length,
     leaf_offset, leaf_length, tile_offset, tile_length,
     addressed, entries, contents) = values
    ranges = [
        (root_offset, root_length, 'root directory'),
        (metadata_offset, metadata_length, 'metadata'),
        (tile_offset, tile_length, 'tile data'),
    ]
    if leaf_length:
        ranges.append((leaf_offset, leaf_length, 'leaf directory'))
    for offset, length, label in ranges:
        if offset < 127 or length <= 0 or offset + length > size:
            raise ValueError(f'invalid PMTiles {label} range')
    ordered = sorted(ranges)
    for left, right in zip(ordered, ordered[1:]):
        if left[0] + left[1] > right[0]:
            raise ValueError('PMTiles archive ranges overlap')
    if not (addressed > 0 and entries > 0 and contents > 0 and
            addressed >= entries >= contents):
        raise ValueError('PMTiles archive declares no usable tile entries')
    if header[99] != 1:
        raise ValueError(f'PMTiles tile type {header[99]} is not vector MVT')
    if header[100] > header[101] or header[101] > 24:
        raise ValueError('PMTiles zoom range is invalid')
    with open(path, 'rb') as archive:
        archive.seek(metadata_offset)
        metadata_bytes = archive.read(metadata_length)
    try:
        metadata = json.loads(_decompress_metadata(metadata_bytes, header[97]))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'PMTiles metadata is invalid: {exc}') from exc
    source_layers = {
        layer.get('id') for layer in metadata.get('vector_layers', [])
        if isinstance(layer, dict) and layer.get('id')
    }
    if not EXPECTED_SOURCE_LAYERS <= source_layers:
        raise ValueError(
            f'PMTiles source layers {sorted(source_layers)} do not contain '
            f'{sorted(EXPECTED_SOURCE_LAYERS)}')
    return {
        'version': 3,
        'bytes': size,
        'source_layers': sorted(source_layers),
        'tile_entries': entries,
        'tile_contents': contents,
    }


def _install_archive(source, destination):
    """Copy to a same-filesystem hidden file, then atomically replace output."""
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    handle, pending = tempfile.mkstemp(
        prefix='.claims-pmtiles-', suffix='.tmp', dir=directory)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as target, open(source, 'rb') as archive:
            shutil.copyfileobj(archive, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        metadata = _validate_pmtiles(pending)
        sha256 = _sha256(pending)
        os.replace(pending, destination)
        return metadata['bytes'], sha256
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _update_manifest(stats, byte_count, sha256, expected_claims):
    current_inputs = load_build_manifest(BUILD_MANIFEST, root=BUILD_INPUTS)
    if current_inputs.get('claims') != expected_claims:
        raise RuntimeError(
            'private claim inputs changed during PMTiles build; rerun the migration')
    with open(MANIFEST, encoding='utf-8') as source:
        manifest = json.load(source)
    manifest.setdefault('national_baselines', {})['claims'] = {
        'file': 'data/tiles/national/claims.pmtiles',
        'format': 'pmtiles',
        'source_layers': list(MODES),
        'n': stats['n'],
        'states': stats['states'],
        'by_mode': stats['by_mode'],
        'partial_states': stats['partial_states'],
        'partial_snapshots': stats['partial_snapshots'],
        'retrieved': TODAY,
        'source': SOURCE,
        'note': ('Compatibility archive built from manifest-declared, state-clipped '
                 'legacy MLRS centroid snapshots.'),
        'bytes': byte_count,
        'sha256': sha256,
    }
    directory = os.path.dirname(MANIFEST)
    current_mode = stat.S_IMODE(os.stat(MANIFEST).st_mode)
    handle, pending = tempfile.mkstemp(prefix='.manifest-claims-', dir=directory)
    try:
        os.chmod(pending, current_mode)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, MANIFEST)
    except Exception:
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def build():
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe >=2.79 with PMTiles output is required')
    inputs_manifest = load_build_manifest(BUILD_MANIFEST, root=BUILD_INPUTS)
    expected_claims = inputs_manifest.get('claims')
    with tempfile.TemporaryDirectory(prefix='nwmm-legacy-claims-') as temporary:
        sequences = {
            mode: os.path.join(temporary, f'{mode}.geojsonseq')
            for mode in MODES
        }
        pending_archive = os.path.join(temporary, 'claims.pmtiles')
        stats = _stream_claims(inputs_manifest, sequences)
        command = [
            'tippecanoe', '--force', '--output', pending_archive,
            '--minimum-zoom=0', '--maximum-zoom=13',
            '--drop-densest-as-needed', '--read-parallel', '--quiet',
            '--attribution=U.S. Bureau of Land Management MLRS',
        ]
        for mode in MODES:
            command.extend(('-L', f'{mode}:{sequences[mode]}'))
        subprocess.run(command, check=True)
        _validate_pmtiles(pending_archive)
        byte_count, sha256 = _install_archive(pending_archive, OUT)
    _update_manifest(stats, byte_count, sha256, expected_claims)
    result = {
        'artifact': os.path.relpath(OUT, SITE),
        'source_layers': list(MODES),
        'features': stats['n'],
        'states': len(stats['states']),
        'by_mode': stats['by_mode'],
        'partial_states': stats['partial_states'],
        'bytes': byte_count,
        'sha256': sha256,
    }
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    build()
