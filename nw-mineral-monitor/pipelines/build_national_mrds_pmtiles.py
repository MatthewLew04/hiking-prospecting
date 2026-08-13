#!/usr/bin/env python3
"""Build the 49-state MRDS occurrence baseline as one PMTiles archive.

The USGS bulk CSV is read only in the build process.  The temporary GeoJSON
never enters ``site/``; the browser receives a range-readable PMTiles archive.
MRDS is a legacy occurrence catalogue, not a claim or land-status source.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import zipfile

from common import cached_get, TODAY
from state_registry import ALL_STATES
from validate_national import _pmtiles_header

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
OUT = os.path.join(SITE, 'data', 'tiles', 'national', 'mrds.pmtiles')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
SOURCE = 'https://mrdata.usgs.gov/mrds/mrds-csv.zip'

STATE_NAMES = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR',
    'California': 'CA', 'Colorado': 'CO', 'Connecticut': 'CT',
    'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA', 'Idaho': 'ID',
    'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS',
    'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN',
    'Mississippi': 'MS', 'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE',
    'Nevada': 'NV', 'New Hampshire': 'NH', 'New Jersey': 'NJ',
    'New Mexico': 'NM', 'New York': 'NY', 'North Carolina': 'NC',
    'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK', 'Oregon': 'OR',
    'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT',
    'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA',
    'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY',
}

PRECIOUS = ('gold', 'silver', 'electrum')
BASE = ('copper', 'lead', 'zinc', 'nickel', 'cobalt', 'molybdenum')
CRITICAL = ('antimony', 'tungsten', 'lithium', 'rare earth', 'beryllium',
            'niobium', 'tantalum', 'platinum', 'palladium', 'chromium')
STONE = ('stone', 'limestone', 'sand', 'gravel', 'clay', 'gypsum', 'quartzite')
ENERGY = ('coal', 'uranium', 'thorium', 'geothermal')


def commodity_group(text: str) -> int:
    """Return the map's stable group index (precious/base/critical/etc.)."""
    value = (text or '').lower()
    if any(term in value for term in PRECIOUS):
        return 0
    if any(term in value for term in BASE):
        return 1
    if any(term in value for term in CRITICAL):
        return 2
    if any(term in value for term in STONE):
        return 3
    if any(term in value for term in ENERGY):
        return 4
    return 5


def status_code(value: str) -> str:
    value = (value or '').lower()
    if 'past producer' in value:
        return 'PP'
    if 'producer' in value:
        return 'P'
    if 'prospect' in value:
        return 'PR'
    if 'occurrence' in value:
        return 'OC'
    if 'plant' in value:
        return 'PL'
    return 'U'


def iter_features(csv_file):
    """Yield compact GeoJSON features plus their two-letter state code."""
    reader = csv.DictReader(io.TextIOWrapper(csv_file, encoding='utf-8-sig',
                                              errors='replace', newline=''))
    for row in reader:
        if (row.get('country') or '').strip() != 'United States':
            continue
        state = STATE_NAMES.get((row.get('state') or '').strip())
        if state not in ALL_STATES:
            continue
        try:
            lat = float(row['latitude'])
            lon = float(row['longitude'])
            identifier = int(row['dep_id'])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon) and
                -90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        commodities = ', '.join(value.strip() for value in (
            row.get('commod1') or '', row.get('commod2') or '',
            row.get('commod3') or '') if value.strip())
        status = status_code(row.get('dev_stat'))
        props = {
            'id': identifier,
            'nm': (row.get('site_name') or '(unnamed)').strip()[:160],
            'st': state,
            'status': status,
            'ex': int(status == 'P'),
            'g': commodity_group(commodities),
            'commodities': commodities[:220] or None,
            'county': (row.get('county') or '').strip()[:100] or None,
            'deposit': (row.get('dep_type') or '').strip()[:100] or None,
            'quality': (row.get('score') or '').strip()[:2] or None,
        }
        props = {key: value for key, value in props.items() if value is not None}
        yield state, {
            'type': 'Feature', 'id': identifier, 'properties': props,
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
        }


def _write_feature_collection(path, features, counts, source_ids):
    with open(path, 'w', encoding='utf-8') as output:
        output.write('{"type":"FeatureCollection","features":[')
        comma = False
        for state, feature in features:
            identifier = feature.get('id')
            if (not isinstance(identifier, int) or isinstance(identifier, bool) or
                    identifier < 0):
                raise RuntimeError(f'MRDS feature has invalid stable ID {identifier!r}')
            if identifier in source_ids:
                raise RuntimeError(f'MRDS source duplicates dep_id {identifier}')
            source_ids.add(identifier)
            if comma:
                output.write(',')
            json.dump(feature, output, separators=(',', ':'))
            comma = True
            counts[state] += 1
        output.write(']}')


def _source_id_inventory(source_ids, tiled_ids, maxzoom_instances):
    """Reconcile the normalized source IDs to unique max-zoom MVT IDs."""
    expected = sorted(source_ids)
    observed = sorted(tiled_ids)
    if (not expected or any(not isinstance(value, int) or isinstance(value, bool) or
                            value < 0 for value in expected) or
            len(expected) != len(set(expected))):
        raise RuntimeError('MRDS normalized source IDs are invalid or duplicated')
    if observed != expected:
        expected_set, observed_set = set(expected), set(observed)
        raise RuntimeError(
            'MRDS PMTiles source-ID reconciliation failed; '
            f'missing={sorted(expected_set - observed_set)[:20]}, '
            f'extra={sorted(observed_set - expected_set)[:20]}')
    # Buffered point geometry may be encoded in both tiles at a boundary. The
    # unique top-level ID set is the source-record completeness contract; raw
    # MVT instances must cover it but are not expected to be one-per-record.
    if (not isinstance(maxzoom_instances, int) or
            maxzoom_instances < len(expected)):
        raise RuntimeError(
            'MRDS PMTiles max-zoom feature instances do not cover the source; '
            f'instances={maxzoom_instances}, source_records={len(expected)}')
    digest = hashlib.sha256(json.dumps(
        expected, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return {
        'status': 'complete_at_retrieval',
        'source_records': len(expected),
        'maxzoom_feature_instances': maxzoom_instances,
        'maxzoom_unique_tiled_ids': len(observed),
        'ids_sha256': digest,
    }


def _validate_pmtiles(path, source_ids):
    try:
        metadata = _pmtiles_header(
            path, ['mrds'],
            # --use-attribute-for-id consumes the `id` property into the MVT
            # top-level ID, so identity is enforced through collect_feature_ids.
            {'mrds': ['nm', 'st', 'status', 'ex', 'g']},
            verify_feature_properties=True, collect_feature_ids=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'MRDS PMTiles semantic validation failed: {exc}') from exc
    tiled_ids = metadata.get('maxzoom_feature_ids', {}).get('mrds', [])
    instances = metadata.get('maxzoom_feature_instances', {}).get('mrds', 0)
    metadata['source_id_inventory'] = _source_id_inventory(
        source_ids, tiled_ids, instances)
    return metadata


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as archive_file:
        for block in iter(lambda: archive_file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def build():
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe >=2.79 is required')
    archive = cached_get(SOURCE, ttl_days=90, binary=True)
    counts = {state: 0 for state in sorted(ALL_STATES)}
    source_ids = set()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nwmm-mrds-') as tmp:
        geojson = os.path.join(tmp, 'mrds.geojson')
        pending_archive = os.path.join(tmp, 'mrds.pmtiles')
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            candidates = [name for name in bundle.namelist() if name.lower().endswith('.csv')]
            if candidates != ['mrds.csv']:
                raise RuntimeError(f'unexpected MRDS archive members: {candidates}')
            with bundle.open(candidates[0]) as csv_file:
                _write_feature_collection(
                    geojson, iter_features(csv_file), counts, source_ids)
        missing = sorted(state for state, count in counts.items() if count == 0)
        if missing:
            raise RuntimeError(f'MRDS bulk source has no features for: {", ".join(missing)}')
        total = sum(counts.values())
        if total != len(source_ids):
            raise RuntimeError(
                f'MRDS state counts total {total} differs from source-ID inventory')
        subprocess.run([
            'tippecanoe', '--force', '--output', pending_archive,
            '--minimum-zoom=0', '--maximum-zoom=12',
            # Retain every record at z12 while Tippecanoe's normal base-zoom
            # sampling keeps lower zooms usable. As-needed density/tile limits
            # must never silently remove a source record from maximum zoom.
            '--base-zoom=12', '--use-attribute-for-id=id',
            '--no-feature-limit', '--no-tile-size-limit', '--quiet',
            '--attribution=U.S. Geological Survey Mineral Resources Data System',
            '-L', f'mrds:{geojson}',
        ], check=True)
        tile_metadata = _validate_pmtiles(pending_archive, source_ids)
        if total != tile_metadata['source_id_inventory']['source_records']:
            raise RuntimeError('MRDS tiled source-ID inventory changed after reconciliation')
        byte_count = tile_metadata['bytes']
        artifact_sha256 = _sha256(pending_archive)
        os.replace(pending_archive, OUT)
    with open(MANIFEST, encoding='utf-8') as manifest_file:
        manifest = json.load(manifest_file)
    manifest.setdefault('national_baselines', {})['mrds'] = {
        'file': 'data/tiles/national/mrds.pmtiles', 'format': 'pmtiles',
        'source_layer': 'mrds', 'n': total, 'states': counts,
        'retrieved': TODAY, 'source': SOURCE,
        'bytes': byte_count, 'sha256': artifact_sha256,
        'source_id_inventory': tile_metadata['source_id_inventory'],
        'note': 'Legacy occurrence catalogue; USGS ceased systematic MRDS updates in 2011.',
    }
    with open(MANIFEST, 'w', encoding='utf-8') as manifest_file:
        json.dump(manifest, manifest_file, separators=(',', ':'))
    result = {'artifact': os.path.relpath(OUT, SITE), 'features': total,
              'states': len([count for count in counts.values() if count]),
              'bytes': byte_count, 'sha256': artifact_sha256}
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    build()
