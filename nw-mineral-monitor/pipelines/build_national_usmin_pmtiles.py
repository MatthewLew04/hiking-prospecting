#!/usr/bin/env python3
"""Build the 49-state USMIN mine-symbol baseline as PMTiles.

The authoritative USGS ArcGIS response is consumed one 2,000-feature page at
a time and normalized directly into temporary newline-delimited GeoJSON.  No
nationwide or statewide GeoJSON is written beneath ``site/``; the sole browser
artifact is ``site/data/tiles/national/usmin.pmtiles``.

Layer metadata and fields were verified against the live service on
2026-08-13.  The query is pinned to layer 17 (points), an OBJECTID upper bound,
and ascending OBJECTID order.  ``resultOffset`` pages are checked for strict
OID monotonicity and against a snapshot count so a changing or truncated
service fails loudly instead of publishing an incomplete archive.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from common import TODAY
from state_registry import ALL_STATES
from validate_national import _pmtiles_header


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
OUT = os.path.join(SITE, 'data', 'tiles', 'national', 'usmin.pmtiles')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')

SERVICE = ('https://energy.usgs.gov/arcgis/rest/services/Hosted/'
           'USMin_Prospect_and_mine_related_map_features/FeatureServer')
LAYER_ID = 17
LAYER = f'{SERVICE}/{LAYER_ID}'
QUERY = f'{LAYER}/query'
DOI = 'https://doi.org/10.5066/F78W3CHG'
PAGE = 2_000
FIELDS = (
    'objectid', 'state', 'county', 'ftr_type', 'ftr_name', 'ftr_azimut',
    'topo_name', 'topo_date', 'topo_scale', 'gda_id', 'scanid',
)
TARGET_STATES = frozenset(ALL_STATES)
EXCLUDED_CODES = frozenset(('HI', 'PR', 'DC'))
STATE_WHERE = 'state IN (' + ','.join(
    f"'{state}'" for state in sorted(TARGET_STATES)) + ')'
USER_AGENT = ('nw-mineral-monitor/11 national USMIN PMTiles builder '
              '(public USGS research data)')

if len(TARGET_STATES) != 49 or TARGET_STATES & EXCLUDED_CODES:
    raise RuntimeError('WS11 USMIN target must be exactly 49 states without HI/PR/DC')


def _request_json(params, tries=6):
    """GET one ArcGIS response with bounded retries and no persistent cache."""
    url = QUERY + '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'application/geo+json, application/json',
    })
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = json.load(response)
            error = data.get('error') if isinstance(data, dict) else None
            if not error:
                return data
            last = RuntimeError(f'ArcGIS error: {error}')
            try:
                code = int(error.get('code'))
            except (AttributeError, TypeError, ValueError):
                code = 0
            if code and code not in (429, 500, 502, 503, 504):
                raise last
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f'USMIN request failed: HTTP {exc.code}') from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'USMIN request failed after {tries} attempts: {last}')


def _snapshot_bounds():
    """Return a count and maximum OID fixed before offset paging begins."""
    statistics = [
        {'statisticType': 'count', 'onStatisticField': 'objectid',
         'outStatisticFieldName': 'feature_count'},
        {'statisticType': 'max', 'onStatisticField': 'objectid',
         'outStatisticFieldName': 'maximum_oid'},
    ]
    data = _request_json({
        'where': STATE_WHERE,
        'outStatistics': json.dumps(statistics, separators=(',', ':')),
        'returnGeometry': 'false',
        'f': 'json',
    })
    features = data.get('features') or []
    attributes = (features[0].get('attributes') or {}) if len(features) == 1 else {}
    count = attributes.get('feature_count')
    maximum_oid = attributes.get('maximum_oid')
    if (isinstance(count, bool) or not isinstance(count, int) or count <= 0 or
            isinstance(maximum_oid, bool) or not isinstance(maximum_oid, int) or
            maximum_oid <= 0):
        raise RuntimeError(f'USMIN snapshot statistics are invalid: {attributes!r}')
    return count, maximum_oid


def _object_id(feature):
    value = (feature.get('properties') or {}).get('objectid')
    if isinstance(value, bool):
        raise RuntimeError(f'USMIN feature has invalid objectid {value!r}')
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'USMIN feature has invalid objectid {value!r}') from exc
    if value <= 0:
        raise RuntimeError(f'USMIN feature has invalid objectid {value!r}')
    return value


def _iter_source_features(expected_count, maximum_oid):
    """Yield a checked OBJECTID-ordered snapshot using ArcGIS offsets."""
    offset = 0
    last_oid = 0
    pages = 0
    snapshot_where = f'({STATE_WHERE}) AND objectid <= {maximum_oid}'
    while offset < expected_count:
        data = _request_json({
            'where': snapshot_where,
            'outFields': ','.join(FIELDS),
            'returnGeometry': 'true',
            'outSR': 4326,
            'geometryPrecision': 6,
            'orderByFields': 'objectid ASC',
            'resultOffset': offset,
            'resultRecordCount': PAGE,
            'f': 'geojson',
        })
        features = data.get('features')
        if not isinstance(features, list) or not features:
            raise RuntimeError(
                f'USMIN pagination stopped at {offset:,}/{expected_count:,} features')
        page_oids = [_object_id(feature) for feature in features]
        if page_oids != sorted(page_oids) or page_oids[0] <= last_oid or any(
                right <= left for left, right in zip(page_oids, page_oids[1:])):
            raise RuntimeError(
                f'USMIN OBJECTID order/uniqueness failed at resultOffset {offset}')
        if page_oids[-1] > maximum_oid:
            raise RuntimeError('USMIN page crossed its snapshot OBJECTID upper bound')
        if offset + len(features) > expected_count:
            raise RuntimeError('USMIN service returned more rows than its snapshot count')
        for feature in features:
            yield feature
        offset += len(features)
        last_oid = page_oids[-1]
        pages += 1
        if pages > math.ceil(expected_count / PAGE) + 1:
            raise RuntimeError('USMIN pagination exceeded its count-derived page budget')
        exceeded = bool(
            data.get('exceededTransferLimit') or
            (data.get('properties') or {}).get('exceededTransferLimit'))
        if offset < expected_count and not exceeded and len(features) < PAGE:
            raise RuntimeError(
                f'USMIN service declared an early final page at {offset:,}/'
                f'{expected_count:,} features')
    if offset != expected_count:
        raise RuntimeError(f'USMIN count mismatch: fetched {offset}, expected {expected_count}')
    if last_oid != maximum_oid:
        raise RuntimeError(
            f'USMIN maximum OBJECTID mismatch: fetched {last_oid}, expected {maximum_oid}')


def _text(value, limit):
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


def _optional_int(value):
    if value in (None, '') or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize(feature):
    properties = feature.get('properties') or {}
    oid = _object_id(feature)
    state = _text(properties.get('state'), 2)
    state = state.upper() if state else None
    if state not in TARGET_STATES:
        raise RuntimeError(f'USMIN query leaked unsupported state code {state!r}')

    geometry = feature.get('geometry') or {}
    coordinates = geometry.get('coordinates')
    if (geometry.get('type') != 'Point' or not isinstance(coordinates, list) or
            len(coordinates) < 2):
        raise RuntimeError(f'USMIN objectid {oid} has invalid point geometry')
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'USMIN objectid {oid} has nonnumeric coordinates') from exc
    if not (math.isfinite(longitude) and math.isfinite(latitude) and
            -180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise RuntimeError(f'USMIN objectid {oid} has out-of-range coordinates')

    feature_type = _text(properties.get('ftr_type'), 40) or 'Feature'
    compact = {
        'fid': oid,
        'st': state,
        'co': _text(properties.get('county'), 50),
        'typ': feature_type,
        'agg': int(bool(re.search(r'gravel|borrow|sand pit|disturbed',
                                  feature_type, re.I))),
        'nm': _text(properties.get('ftr_name'), 50),
        'az': _optional_int(properties.get('ftr_azimut')),
        'quad': _text(properties.get('topo_name'), 50),
        'yr': _optional_int(properties.get('topo_date')),
        'scale': _optional_int(properties.get('topo_scale')),
        'gda': _optional_int(properties.get('gda_id')),
        'scan': _optional_int(properties.get('scanid')),
    }
    compact = {key: value for key, value in compact.items() if value is not None}
    return state, {
        'type': 'Feature',
        'id': oid,
        'properties': compact,
        'geometry': {
            'type': 'Point',
            'coordinates': [round(longitude, 6), round(latitude, 6)],
        },
    }


def _stream_usmin(path):
    """Write temporary GeoJSON sequence and return checked build statistics."""
    expected_count, maximum_oid = _snapshot_bounds()
    counts = {state: 0 for state in sorted(TARGET_STATES)}
    first_oid = None
    last_oid = None
    source_ids = []
    emitted = 0
    with open(path, 'w', encoding='utf-8') as output:
        for source_feature in _iter_source_features(expected_count, maximum_oid):
            state, feature = _normalize(source_feature)
            json.dump(feature, output, separators=(',', ':'))
            output.write('\n')
            counts[state] += 1
            emitted += 1
            first_oid = feature['id'] if first_oid is None else first_oid
            last_oid = feature['id']
            source_ids.append(feature['id'])
    if emitted != expected_count:
        raise RuntimeError(f'USMIN emitted {emitted:,}; expected {expected_count:,}')
    found = {state for state, count in counts.items() if count}
    if found != TARGET_STATES:
        missing = sorted(TARGET_STATES - found)
        extra = sorted(found - TARGET_STATES)
        raise RuntimeError(
            f'USMIN source does not cover exactly the WS11 49; '
            f'missing={missing}, extra={extra}')
    return {
        'n': emitted,
        'states': counts,
        'first_oid': first_oid,
        'maximum_oid': last_oid,
        'snapshot_maximum_oid': maximum_oid,
        # Kept only in private build memory for exact max-zoom reconciliation;
        # the public manifest receives the count and canonical digest, not this
        # several-hundred-thousand-element array.
        'source_ids': source_ids,
    }


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _source_id_inventory(source_ids, tiled_ids, maxzoom_instances):
    expected = sorted(source_ids)
    observed = sorted(tiled_ids)
    if (not expected or any(not isinstance(value, int) or isinstance(value, bool) or
                            value <= 0 for value in expected) or
            len(expected) != len(set(expected))):
        raise RuntimeError('USMIN normalized OBJECTIDs are invalid or duplicated')
    if observed != expected:
        expected_set, observed_set = set(expected), set(observed)
        raise RuntimeError(
            'USMIN PMTiles source-ID reconciliation failed; '
            f'missing={sorted(expected_set - observed_set)[:20]}, '
            f'extra={sorted(observed_set - expected_set)[:20]}')
    # Tile buffers can duplicate a point at a tile seam. Exact completeness is
    # therefore the unique ID set; feature instances may exceed source rows.
    if (not isinstance(maxzoom_instances, int) or
            maxzoom_instances < len(expected)):
        raise RuntimeError(
            'USMIN PMTiles max-zoom feature instances do not cover its source; '
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
            # Tippecanoe consumes `fid` into the MVT top-level ID. Require the
            # remaining semantic fields and reconcile identity separately.
            path, ['usmin'], {'usmin': ['st', 'typ', 'agg']},
            verify_feature_properties=True, collect_feature_ids=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'USMIN PMTiles semantic validation failed: {exc}') from exc
    tiled_ids = metadata.get('maxzoom_feature_ids', {}).get('usmin', [])
    instances = metadata.get('maxzoom_feature_instances', {}).get('usmin', 0)
    metadata['source_id_inventory'] = _source_id_inventory(
        source_ids, tiled_ids, instances)
    return metadata


def _update_manifest(stats, byte_count, sha256, source_id_inventory):
    with open(MANIFEST, encoding='utf-8') as source:
        manifest = json.load(source)
    manifest.setdefault('national_baselines', {})['usmin'] = {
        'file': 'data/tiles/national/usmin.pmtiles',
        'format': 'pmtiles',
        'source_layer': 'usmin',
        'n': stats['n'],
        'states': stats['states'],
        'retrieved': TODAY,
        'source': LAYER,
        'doi': DOI,
        'excluded_state_codes': sorted(EXCLUDED_CODES),
        'snapshot_maximum_oid': stats['snapshot_maximum_oid'],
        'bytes': byte_count,
        'sha256': sha256,
        'source_id_inventory': source_id_inventory,
    }
    directory = os.path.dirname(MANIFEST)
    handle, pending = tempfile.mkstemp(prefix='.manifest-usmin-', dir=directory)
    try:
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
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nwmm-usmin-') as temporary:
        sequence = os.path.join(temporary, 'usmin.geojsonseq')
        pending_archive = os.path.join(temporary, 'usmin.pmtiles')
        stats = _stream_usmin(sequence)
        subprocess.run([
            'tippecanoe', '--force', '--output', pending_archive,
            '--minimum-zoom=0', '--maximum-zoom=13',
            # Preserve the complete OBJECTID snapshot at z13. Normal base-zoom
            # sampling still keeps overview tiles compact, while as-needed tile
            # limits cannot silently remove source records from maximum zoom.
            '--base-zoom=13', '--no-feature-limit', '--no-tile-size-limit',
            '--use-attribute-for-id=fid', '--read-parallel', '--quiet',
            '--attribution=U.S. Geological Survey USMIN v10.0 (May 2023)',
            '-L', f'usmin:{sequence}',
        ], check=True)
        tile_metadata = _validate_pmtiles(pending_archive, stats['source_ids'])
        if stats['n'] != tile_metadata['source_id_inventory']['source_records']:
            raise RuntimeError('USMIN tiled source-ID inventory changed after reconciliation')
        byte_count = tile_metadata['bytes']
        sha256 = _sha256(pending_archive)
        os.replace(pending_archive, OUT)
    _update_manifest(
        stats, byte_count, sha256, tile_metadata['source_id_inventory'])
    result = {
        'artifact': os.path.relpath(OUT, SITE),
        'source_layer': 'usmin',
        'features': stats['n'],
        'states': len([count for count in stats['states'].values() if count]),
        'bytes': byte_count,
        'sha256': sha256,
    }
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    build()
