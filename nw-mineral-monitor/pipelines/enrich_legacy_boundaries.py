#!/usr/bin/env python3
"""Join stable Census FIPS to private legacy boundary build inputs.

The legacy seven-state/eight-state boundary polygons predate the national
administrative archive and omitted FIPS. This build-time join reads the
already-published national ``admin.pmtiles`` archive, whose manifest
provenance is U.S. Census Bureau TIGERweb (January 1, 2025 vintage), and joins
only its authoritative properties. Legacy geometries are preserved byte-for-
byte at the coordinate level; only root metadata, ``n``, and feature
properties are normalized.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
BOUNDARIES = os.path.join(ROOT, 'build-inputs', 'data', 'boundaries')
ADMIN = os.path.join(ROOT, 'site', 'data', 'tiles', 'context', 'admin.pmtiles')
SOURCE = ('U.S. Census Bureau TIGERweb State_County MapServer, '
          'January 1 2025 vintage (joined from national admin.pmtiles)')


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _features(value):
    if not isinstance(value, dict):
        return
    if value.get('type') == 'Feature' and isinstance(value.get('geometry'), dict):
        yield value
    for child in value.get('features') or ():
        yield from _features(child)


def _admin_index(admin_path, layer):
    decoder = shutil.which('tippecanoe-decode')
    if not decoder:
        raise RuntimeError('tippecanoe-decode >=2.79 is required for the FIPS join')
    completed = subprocess.run(
        [decoder, '-Z', '0', '-z', '0', '-l', layer, admin_path],
        check=True, capture_output=True, text=True)
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'admin PMTiles decode is invalid JSON: {exc}') from exc
    rows = []
    for feature in _features(decoded):
        properties = feature.get('properties') or {}
        if all(isinstance(properties.get(field), str) and properties[field]
               for field in ('fips', 'st', 'name')):
            rows.append(properties)
    if not rows:
        raise RuntimeError(f'admin PMTiles layer {layer!r} has no FIPS rows')
    return rows


def _county_name(name):
    value = re.sub(r'\s+', ' ', str(name).strip()).casefold()
    # Current scope is western counties, but retaining Census area suffixes
    # makes this join deterministic if the private compatibility set expands.
    return re.sub(
        r'\s+(county|parish|borough|census area|municipality|city and borough)$',
        '', value)


def _atomic_json(path, payload):
    mode = stat.S_IMODE(os.stat(path).st_mode)
    handle, pending = tempfile.mkstemp(prefix='.boundary-', dir=os.path.dirname(path))
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(payload, output, separators=(',', ':'), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def enrich(boundary_root=BOUNDARIES, admin_path=ADMIN):
    admin_sha = _sha256(admin_path)
    stats = {}
    for key in ('states', 'counties'):
        path = os.path.join(boundary_root, f'{key}.json')
        with open(path, encoding='utf-8') as source:
            payload = json.load(source)
        features = payload.get('features') if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise RuntimeError(f'{key}.json must contain a nonempty features array')
        admin_rows = _admin_index(admin_path, key)
        if key == 'states':
            index = {row['st']: row['fips'] for row in admin_rows}
            identity = lambda props: props.get('st')
        else:
            index = {(row['st'], _county_name(row['name'])): row['fips']
                     for row in admin_rows}
            identity = lambda props: (
                props.get('st'), _county_name(props.get('name')))
        missing = []
        seen = set()
        for feature in features:
            props = feature.get('properties') if isinstance(feature, dict) else None
            geometry = feature.get('geometry') if isinstance(feature, dict) else None
            if (not isinstance(props, dict) or not isinstance(geometry, dict) or
                    geometry.get('type') not in ('Polygon', 'MultiPolygon')):
                raise RuntimeError(f'{key}.json contains a non-polygon feature')
            lookup = identity(props)
            fips = index.get(lookup)
            if fips is None:
                missing.append(lookup)
                continue
            if fips in seen:
                raise RuntimeError(f'{key}.json duplicates Census FIPS {fips}')
            seen.add(fips)
            props['fips'] = fips
        if missing:
            raise RuntimeError(f'{key}.json has unmatched Census identities: {missing[:10]}')
        payload['type'] = 'FeatureCollection'
        payload['n'] = len(features)
        payload['source'] = SOURCE
        payload['fips_join'] = {
            'artifact': 'site/data/tiles/context/admin.pmtiles',
            'artifact_sha256': admin_sha,
            'layer': key,
            'key': 'state abbreviation' if key == 'states'
                   else 'state abbreviation + normalized county name',
        }
        _atomic_json(path, payload)
        stats[key] = len(features)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--boundary-root', default=BOUNDARIES)
    parser.add_argument('--admin', default=ADMIN)
    args = parser.parse_args(argv)
    stats = enrich(args.boundary_root, args.admin)
    print(json.dumps(stats, sort_keys=True))


if __name__ == '__main__':
    main()
