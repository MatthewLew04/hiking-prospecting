#!/usr/bin/env python3
"""Build the 49-state airborne/Earth MRI survey-index trust layer.

The ArcGIS GeoJSON responses exist only in a private temporary build directory
outside ``site/``.  The only browser artifact is range-readable PMTiles; this
is deliberately not a statewide GeoJSON fallback, even during a rebuild.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.parse

from common import cached_get, TODAY
from validate_national import _pmtiles_header

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
OUT = os.path.join(SITE, 'data', 'tiles', 'geophys', 'surveys.pmtiles')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
PRIVATE_STAGING_ROOT = os.path.join(ROOT, 'build-inputs', '.staging')

AIRBORNE = ('https://energy.usgs.gov/arcgis/rest/services/Hosted/'
            'Airborne_Geophysical_Surveys/FeatureServer/0')
EARTHMRI = ('https://energy.usgs.gov/arcgis/rest/services/MRData/'
            'Earth_MRI_Acquisitions/MapServer/3')
SOURCES = (AIRBORNE, EARTHMRI)
WS11_WINDOWS = (
    (-125.0, 24.0, -66.0, 50.0),       # contiguous United States
    (-180.0, 50.0, -129.0, 72.5),      # Alaska, west of Greenwich
    (172.0, 50.0, 180.0, 72.5),        # Aleutians across the antimeridian
)


def _reject_constant(value):
    raise ValueError(f'non-finite JSON constant {value!r}')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key {key!r}')
        result[key] = value
    return result


def _read_manifest():
    with open(MANIFEST, 'rb') as source:
        raw = source.read()
    value = json.loads(
        raw.decode('utf-8'), parse_constant=_reject_constant,
        object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise RuntimeError('public manifest must be a strict JSON object')
    return value, hashlib.sha256(raw).hexdigest()


def _request(base, params):
    data = json.loads(cached_get(
        base + '/query?' + urllib.parse.urlencode(params), ttl_days=30))
    if not isinstance(data, dict) or data.get('error'):
        raise RuntimeError(f'{base}: invalid ArcGIS response {data!r}')
    return data


def _object_id(value, label):
    if isinstance(value, bool):
        raise RuntimeError(f'{label}: object id must be an integer')
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'{label}: object id must be an integer') from exc
    if parsed < 0 or str(value).strip() not in {str(parsed), f'{parsed}.0'}:
        raise RuntimeError(f'{label}: object id must be a nonnegative integer')
    return parsed


def _query(base, oid, fields, chunk_size=500):
    """Fetch one immutable ArcGIS ID snapshot without retaining browser JSON.

    Offset pagination can silently skip or duplicate rows while a hosted layer
    changes. ArcGIS explicitly exempts ``returnIdsOnly`` from its feature cap,
    so pin the complete ID set first and reconcile every chunk to it.
    """
    snapshot = _request(base, {
        'where': '1=1', 'returnIdsOnly': 'true', 'f': 'json',
    })
    advertised_oid = snapshot.get('objectIdFieldName')
    if (advertised_oid is not None and
            str(advertised_oid).casefold() != oid.casefold()):
        raise RuntimeError(
            f'{base}: object-id field {advertised_oid!r} does not match {oid!r}')
    raw_ids = snapshot.get('objectIds')
    if not isinstance(raw_ids, list) or not raw_ids:
        raise RuntimeError(f'{base}: ArcGIS object-id snapshot is empty')
    ids = [_object_id(value, base) for value in raw_ids]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f'{base}: ArcGIS object-id snapshot contains duplicates')
    ids.sort()
    out = []
    seen = set()
    for start in range(0, len(ids), chunk_size):
        expected = ids[start:start + chunk_size]
        data = _request(base, {
            'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(fields),
            'returnGeometry': 'true', 'outSR': 4326,
            'geometryPrecision': 5, 'f': 'geojson',
        })
        features = data.get('features')
        if not isinstance(features, list):
            raise RuntimeError(f'{base}: ArcGIS feature page is invalid')
        page = {}
        for feature in features:
            if not isinstance(feature, dict):
                raise RuntimeError(f'{base}: ArcGIS feature row is invalid')
            properties = feature.get('properties')
            if not isinstance(properties, dict):
                raise RuntimeError(f'{base}: ArcGIS feature properties are invalid')
            feature_id = _object_id(properties.get(oid), base)
            if feature_id in page or feature_id in seen:
                raise RuntimeError(f'{base}: ArcGIS feature page contains duplicate IDs')
            page[feature_id] = feature
        if set(page) != set(expected):
            missing = sorted(set(expected) - set(page))[:5]
            extra = sorted(set(page) - set(expected))[:5]
            raise RuntimeError(
                f'{base}: ArcGIS ID snapshot/page mismatch; missing={missing}, extra={extra}')
        for feature_id in expected:
            out.append(page[feature_id])
            seen.add(feature_id)
    if seen != set(ids):
        raise RuntimeError(f'{base}: ArcGIS object-id snapshot was not exhausted')
    return out


def _pairs(value):
    if (isinstance(value, list) and len(value) >= 2 and
            isinstance(value[0], (int, float)) and
            isinstance(value[1], (int, float))):
        yield value[0], value[1]
    elif isinstance(value, list):
        for child in value:
            yield from _pairs(child)


def _in_scope(feature):
    coords = list(_pairs((feature.get('geometry') or {}).get('coordinates')))
    if not coords:
        return False
    minx = min(p[0] for p in coords)
    maxx = max(p[0] for p in coords)
    miny = min(p[1] for p in coords)
    maxy = max(p[1] for p in coords)
    return any(maxx >= x0 and minx <= x1 and maxy >= y0 and miny <= y1
               for x0, y0, x1, y1 in WS11_WINDOWS)


def _clean(value):
    return None if value in (None, '', '-9999', '-9999.0') else value


def _normalize_air(feature):
    p = feature.get('properties') or {}
    alt_type = {'B': 'barometric', 'D': 'drape', 'AG': 'above ground'}.get(
        str(_clean(p.get('altitude_t')) or ''), _clean(p.get('altitude_t')))
    props = {
        'fid': int(p['fid']), 'src': 'airborne',
        'nm': p.get('name') or p.get('survey') or 'Unnamed airborne survey',
        'st': _clean(p.get('state')), 'yr': _clean(p.get('year')),
        'flown': _clean(p.get('date_flown')), 'kind': _clean(p.get('type')),
        'spacing': _clean(p.get('spacing1')), 'alt': _clean(p.get('altitude1')),
        'alt_type': alt_type, 'lnkm': _clean(p.get('lnkm')),
        'by': _clean(p.get('flown_by')), 'pub': _clean(p.get('pubid')),
        'has': ''.join(k.upper() for k in ('mag', 'rad', 'grav', 'em')
                       if str(p.get(k) or '').upper().startswith('Y')) or None,
    }
    feature['properties'] = {k: v for k, v in props.items() if v is not None}
    return feature


def _normalize_earthmri(feature):
    p = feature.get('properties') or {}
    props = {
        'fid': 2_000_000 + int(p['OBJECTID']), 'src': 'earthmri',
        'nm': p.get('PNAME') or p.get('ALIAS') or 'Earth MRI acquisition',
        'yr': _clean(p.get('YEARSTART')), 'yr_end': _clean(p.get('YEAREND')),
        'kind': 'Magnetic/Radiometric (Earth MRI)',
        'program': _clean(p.get('PROGRAM')), 'by': _clean(p.get('AFFILIATIO')),
        'ref': _clean(p.get('WEBSITE')) or _clean(p.get('PDATA')),
    }
    feature['properties'] = {k: v for k, v in props.items() if v is not None}
    return feature


def _source_id_inventory(features):
    ids = [feature.get('properties', {}).get('fid') for feature in features]
    if (not ids or any(not isinstance(value, int) or isinstance(value, bool) or
                       value < 0 for value in ids) or len(ids) != len(set(ids))):
        raise RuntimeError('national survey source IDs are invalid or duplicated')
    ordered = sorted(ids)
    digest = hashlib.sha256(json.dumps(
        ordered, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return {'status': 'complete_at_retrieval', 'source_records': len(ordered),
            'maxzoom_unique_tiled_ids': len(ordered), 'ids_sha256': digest}


def build():
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe >=2.79 is required')
    airborne = _query(AIRBORNE, 'fid', (
        'fid', 'survey', 'state', 'type', 'name', 'flown_by', 'date_flown',
        'spacing1', 'altitude_t', 'altitude1', 'lnkm', 'pubid', 'year',
        'mag', 'em', 'rad', 'grav'))
    earthmri = _query(EARTHMRI, 'OBJECTID', (
        'OBJECTID', 'PID', 'PNAME', 'YEARSTART', 'YEAREND', 'PROGRAM',
        'AFFILIATIO', 'WEBSITE', 'PDATA', 'ALIAS'))
    features = [_normalize_air(f) for f in airborne if _in_scope(f)]
    features += [_normalize_earthmri(f) for f in earthmri if _in_scope(f)]
    if not features or not any(f['properties']['src'] == 'earthmri' for f in features):
        raise RuntimeError('national survey query was unexpectedly empty/incomplete')
    source_id_inventory = _source_id_inventory(features)
    manifest, manifest_before_sha = _read_manifest()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.commonpath((os.path.realpath(SITE),
                           os.path.realpath(PRIVATE_STAGING_ROOT))) == \
            os.path.realpath(SITE):
        raise RuntimeError('geophysics staging root must be outside public site/')
    os.makedirs(PRIVATE_STAGING_ROOT, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix='nwmm-geophys-', dir=PRIVATE_STAGING_ROOT) as tmp:
        geojson = os.path.join(tmp, 'surveys.geojson')
        pending = os.path.join(tmp, 'surveys.pmtiles')
        with open(geojson, 'w', encoding='utf-8') as fh:
            json.dump({'type': 'FeatureCollection', 'features': features}, fh,
                      separators=(',', ':'))
        subprocess.run([
            'tippecanoe', '--force', '--output', pending,
            '--minimum-zoom=0', '--maximum-zoom=10',
            '--use-attribute-for-id=fid', '--no-feature-limit',
            '--no-tile-size-limit', '--simplify-only-low-zooms', '--quiet',
            '--no-tiny-polygon-reduction-at-maximum-zoom',
            '--attribution=U.S. Geological Survey airborne inventory and Earth MRI',
            '-L', f'surveys:{geojson}',
        ], check=True)
        # Validate every decoded MVT feature before replacing the public
        # archive. A metadata-only layer declaration cannot attest that the
        # survey/source identity actually survived tiling.
        meta = _pmtiles_header(
            pending, ['surveys'], {'surveys': ['src', 'nm']},
            verify_feature_properties=True, collect_feature_ids=True)
        tiled_ids = meta['maxzoom_feature_ids'].get('surveys', [])
        expected_ids = sorted(feature['properties']['fid'] for feature in features)
        if tiled_ids != expected_ids:
            expected, actual = set(expected_ids), set(tiled_ids)
            raise RuntimeError(
                'national survey PMTiles source-ID reconciliation failed; '
                f'missing={sorted(expected-actual)[:20]}, '
                f'extra={sorted(actual-expected)[:20]}')
        artifact_bytes = meta['bytes']
        digest = hashlib.sha256()
        with open(pending, 'rb') as archive:
            for block in iter(lambda: archive.read(1024 * 1024), b''):
                digest.update(block)
        artifact_sha256 = digest.hexdigest()
        by_source = {}
        for feature in features:
            src = feature['properties']['src']
            by_source[src] = by_source.get(src, 0) + 1
        manifest.setdefault('ws56', {})['geophys_surveys'] = {
            'file': 'data/tiles/geophys/surveys.pmtiles', 'format': 'pmtiles',
            'source_layer': 'surveys', 'required_properties': ['src', 'nm'],
            'n': len(features), 'by_source': by_source, 'retrieved': TODAY,
            'sources': list(SOURCES), 'bytes': artifact_bytes,
            'sha256': artifact_sha256,
            'source_id_inventory': source_id_inventory,
        }
        descriptor, manifest_pending = tempfile.mkstemp(
            prefix='.manifest-geophys-', dir=os.path.dirname(MANIFEST))
        archive_backup = os.path.join(tmp, 'previous-surveys.pmtiles')
        had_archive = os.path.isfile(OUT)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as fh:
                json.dump(manifest, fh, separators=(',', ':'), allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            # Do not overwrite a manifest stamp produced by another national
            # builder during this run. The expensive archive remains pending.
            with open(MANIFEST, 'rb') as current:
                current_sha = hashlib.sha256(current.read()).hexdigest()
            if current_sha != manifest_before_sha:
                raise RuntimeError('public manifest changed during geophysics build')
            if had_archive:
                shutil.copyfile(OUT, archive_backup)
            try:
                os.replace(pending, OUT)
                os.replace(manifest_pending, MANIFEST)
            except BaseException:
                if had_archive:
                    os.replace(archive_backup, OUT)
                elif os.path.exists(OUT):
                    os.unlink(OUT)
                raise
        finally:
            if os.path.exists(manifest_pending):
                os.unlink(manifest_pending)
    result = {'artifact': os.path.relpath(OUT, SITE), 'features': len(features),
              'by_source': by_source, 'bytes': artifact_bytes,
              'sha256': artifact_sha256}
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    build()
