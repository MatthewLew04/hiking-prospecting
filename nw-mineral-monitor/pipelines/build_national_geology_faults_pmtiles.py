#!/usr/bin/env python3
"""Build the WS11 national geology and fault baselines as PMTiles.

Browser-addressable GeoJSON is never written.  The builder snapshots official
USGS object IDs before paging, writes normalized GeoJSON sequence only inside a
temporary directory, validates both PMTiles archives, then atomically stamps
``manifest.national_baselines``.

Sources
-------
* Conterminous geology and bedrock faults: USGS SGMC v1.1 Feature Service
  (the published 2017 compilation).  Every service feature carries its source
  map citation; the native scale is parsed from that citation and retained.
* Alaska geology and bedrock faults: USGS SIM 3340 Feature Service.  SOURCE is
  joined to the service's ``nsarefs`` table before any feature is emitted.
* Quaternary faults: the official USGS Qfaults bulk shapefile archive.  The
  archive already carries mapped scale and fault/report identifiers.

The newer 2026 GeMS conversion of SGMC is registered separately as the current
data release.  Its direct bulk asset is not silently substituted here: this
baseline names and hashes the exact official service snapshot it actually
uses.  Macrostrat is not needed as a gap-fill because SGMC plus SIM 3340 cover
all 49 WS11 states; the manifest records that fact explicitly.
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
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from common import TODAY
from state_registry import ALL_STATES

try:
    import fiona
except ImportError:  # pragma: no cover - exercised by build preflight
    fiona = None


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
GEOLOGY_OUT = os.path.join(SITE, 'data', 'tiles', 'national', 'geology.pmtiles')
FAULTS_OUT = os.path.join(SITE, 'data', 'tiles', 'national', 'faults.pmtiles')

TARGET_STATES = frozenset(ALL_STATES)
CONUS_STATES = TARGET_STATES - {'AK'}
EXCLUDED_CODES = frozenset(('DC', 'HI', 'PR'))

SGMC_DOI = 'https://doi.org/10.5066/F7WH2N65'
SGMC_ITEM = 'https://www.arcgis.com/home/item.html?id=3890cfedb3204aa8828765a2ccfaeb38'
SGMC_SERVICE = (
    'https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/'
    'SB_5888bf4fe4b05ccb964bab9d_USGS_SGMC_feature/FeatureServer')
SGMC_GEOLOGY = f'{SGMC_SERVICE}/3'
SGMC_STRUCTURE = f'{SGMC_SERVICE}/1'

AK_DOI = 'https://doi.org/10.3133/sim3340'
AK_ITEM = 'https://www.arcgis.com/home/item.html?id=b80ab6a076ef44b184caf8026bed8e4f'
AK_SERVICE = (
    'https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/'
    'Geologic_map_of_Alaska_FeatureLayer/FeatureServer')
AK_GEOLOGY = f'{AK_SERVICE}/6'
AK_LINES = f'{AK_SERVICE}/5'
AK_REFS = f'{AK_SERVICE}/12'

QFAULTS_DOI = 'https://doi.org/10.5066/P9BCVRCK'
QFAULTS_URL = (
    'https://earthquake.usgs.gov/static/lfs/nshm/qfaults/Qfaults_GIS.zip')

ARCGIS_PAGE = 500
TIPPECANOE_DETAILS = {'geology': 14, 'faults': 20}
WEB_MERCATOR_WORLD = 2 ** 32
WEB_MERCATOR_MAX_LAT = 85.05112878
GEOMETRY_NORMALIZATION = 'minimum_nonzero_32bit_web_mercator'
GEOMETRY_NORMALIZATION_ENGINE = 'nwmm_web_mercator_32bit_v1'
GEOMETRY_NORMALIZATION_REASON = (
    'distinct_source_vertices_collapsed_at_32bit_tile_quantization')
USER_AGENT = (
    'nw-mineral-monitor/11 national geology PMTiles builder '
    '(public USGS research data)')
SCALE_RE = re.compile(
    r'(?i)(?:scale(?:\s+(?:ca\.?|approximately))?\s*)?'
    r'\b1\s*:\s*([0-9][0-9,]*(?:\s*(?:to|-|and)\s*1\s*:\s*[0-9][0-9,]*)?)')
FAULT_RE = re.compile(r'\b(?:fault|shear(?:\s+zone)?)\b', re.I)
AK_001_REFERENCE = (
    'SIM 3340 metadata: SOURCE codes ending 001 are reserved for the '
    '1:250,000-scale quadrangle topographic map or another hydrologic spatial '
    'source; the published nsarefs table contains no citation row for this code.')

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
    'WA Offshore': 'WA', 'CA offshore': 'CA',
}

if (len(TARGET_STATES) != 49 or 'HI' in TARGET_STATES or
        len(CONUS_STATES) != 48 or TARGET_STATES & EXCLUDED_CODES):
    raise RuntimeError('WS11 geology target must be exactly 49 states without HI/DC/PR')


def _text(value, limit=500):
    if value is None:
        return None
    value = re.sub(r'\s+', ' ', str(value)).strip()
    return value[:limit] if value else None


def _ci_properties(feature):
    properties = feature.get('properties') or feature.get('attributes') or {}
    return {str(key).casefold(): value for key, value in properties.items()}


def _positive_oid(value, label='OBJECTID'):
    if isinstance(value, bool):
        raise RuntimeError(f'{label} must be a positive integer, got {value!r}')
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'{label} must be a positive integer, got {value!r}') from exc
    if value <= 0:
        raise RuntimeError(f'{label} must be a positive integer, got {value!r}')
    return value


def _source_scale(reference):
    """Return a nonempty scale plus whether it was explicit in the source."""
    reference = _text(reference, 2_000)
    if not reference:
        raise RuntimeError('source reference is empty')
    match = SCALE_RE.search(reference)
    if not match:
        return 'not stated in source reference', 'source_reference_omits_scale'
    value = re.sub(r'\s+', ' ', match.group(1)).strip()
    return f'1:{value}', 'explicit'


def _source_id(dataset, state, reference):
    digest = hashlib.sha256(reference.encode('utf-8')).hexdigest()[:16]
    return f'{dataset}:{state}:{digest}'


def _plain_geometry(value, expected):
    if hasattr(value, '__geo_interface__'):
        value = value.__geo_interface__
    if not isinstance(value, dict):
        try:
            value = dict(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError('feature geometry is not a mapping') from exc
    geometry_type = value.get('type')
    if geometry_type not in expected:
        raise RuntimeError(
            f'feature geometry {geometry_type!r} is not one of {sorted(expected)}')
    coordinates = value.get('coordinates')
    positions = 0

    def convert(item):
        nonlocal positions
        if (isinstance(item, (list, tuple)) and len(item) >= 2 and
                all(isinstance(part, (int, float)) and not isinstance(part, bool)
                    for part in item[:2])):
            longitude, latitude = float(item[0]), float(item[1])
            if (not math.isfinite(longitude) or not math.isfinite(latitude) or
                    not -180 <= longitude <= 180 or not -90 <= latitude <= 90):
                raise RuntimeError(
                    f'feature has invalid coordinate ({longitude}, {latitude})')
            positions += 1
            # Eight decimal places retain sub-centimetre source distinctions.
            # Six places (~0.1 m) made eleven valid Qfault traces zero-length
            # before Tippecanoe ever saw them.
            return [round(longitude, 8), round(latitude, 8)]
        if not isinstance(item, (list, tuple)) or not item:
            raise RuntimeError('feature geometry has malformed coordinate nesting')
        return [convert(child) for child in item]

    converted = convert(coordinates)
    if positions < 2:
        raise RuntimeError('feature geometry has too few positions')
    return {'type': geometry_type, 'coordinates': converted}


def _geometry_sha256(geometry):
    return hashlib.sha256(json.dumps(
        geometry, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()


def _mercator_world(position):
    longitude, latitude = position[:2]
    if not -WEB_MERCATOR_MAX_LAT <= latitude <= WEB_MERCATOR_MAX_LAT:
        raise RuntimeError(
            f'line latitude {latitude} is outside Web Mercator tile bounds')
    x = (longitude + 180.0) / 360.0 * WEB_MERCATOR_WORLD
    y = ((1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi) /
         2.0 * WEB_MERCATOR_WORLD)
    return x, y


def _from_mercator_world(x, y):
    longitude = x / WEB_MERCATOR_WORLD * 360.0 - 180.0
    latitude = math.degrees(math.atan(math.sinh(
        math.pi * (1.0 - 2.0 * y / WEB_MERCATOR_WORLD))))
    return [round(longitude, 8), round(latitude, 8)]


def _quantized_world(position):
    # Tippecanoe's maximum useful z+detail is 32 bits. Half-up quantization
    # matches the integer coordinate boundary that determines whether a line
    # has any encodable extent.
    return tuple(math.floor(value + 0.5) for value in _mercator_world(position))


def _distance_m(left, right):
    lon1, lat1 = map(math.radians, left[:2])
    lon2, lat2 = map(math.radians, right[:2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = (math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) *
             math.sin(dlon / 2) ** 2)
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(value)))


def _line_length_m(parts):
    return sum(_distance_m(left, right)
               for part in parts for left, right in zip(part, part[1:]))


def _normalize_collapsed_line_parts(geometry, properties, source_record_id):
    """Give valid sub-centimetre lines the minimum encodable tile extent.

    The original eight-decimal geometry is cryptographically identified in
    every changed feature. Only a part whose distinct source vertices all map
    to one 32-bit Web-Mercator coordinate is changed. The selected endpoint is
    moved just 0.55 coordinate units across the nearest boundary in its native
    direction, then the result is checked against the same quantizer.
    """
    parts = ([geometry['coordinates']] if geometry['type'] == 'LineString'
             else geometry['coordinates'])
    source_geometry = json.loads(json.dumps(geometry))
    source_hash = _geometry_sha256(source_geometry)
    source_length = _line_length_m(parts)
    changed = []
    for part_index, part in enumerate(parts):
        if len(set(map(tuple, part))) < 2:
            raise RuntimeError(
                f'line source record {source_record_id} part {part_index} '
                'has no distinct source vertices')
        if len({_quantized_world(position) for position in part}) > 1:
            continue
        candidates = []
        for index, (left, right) in enumerate(zip(part, part[1:])):
            left_world, right_world = _mercator_world(left), _mercator_world(right)
            length = math.hypot(right_world[0] - left_world[0],
                                right_world[1] - left_world[1])
            if length:
                candidates.append((length, index, left_world, right_world))
        if not candidates:
            raise RuntimeError(
                f'line source record {source_record_id} part {part_index} '
                'has no nonzero segment')
        _, index, left_world, right_world = max(candidates)
        axis = 0 if abs(right_world[0] - left_world[0]) >= abs(
            right_world[1] - left_world[1]) else 1
        direction = 1 if right_world[axis] > left_world[axis] else -1
        adjusted = list(right_world)
        quantized = _quantized_world(part[index])
        adjusted[axis] = quantized[axis] + direction * 0.55
        original = list(part[index + 1])
        replacement = _from_mercator_world(*adjusted)
        part[index + 1] = replacement
        if len({_quantized_world(position) for position in part}) < 2:
            raise RuntimeError(
                f'line source record {source_record_id} normalization did not '
                'produce a nonzero tile segment')
        changed.append({
            'part': part_index, 'vertex': index + 1,
            'delta_m': _distance_m(original, replacement),
        })
    if not changed:
        return geometry, None
    audit = {
        'geometry_normalization': GEOMETRY_NORMALIZATION,
        'geometry_normalization_engine': GEOMETRY_NORMALIZATION_ENGINE,
        'geometry_normalization_reason': GEOMETRY_NORMALIZATION_REASON,
        'geometry_normalization_delta_m': round(
            max(item['delta_m'] for item in changed), 6),
        'geometry_normalization_parts': len(changed),
        'source_geometry_sha256': source_hash,
        'source_geometry_length_m': round(source_length, 6),
        'source_record_id': str(source_record_id),
    }
    properties.update(audit)
    return geometry, audit


def _request_json(url, params, *, post=False, tries=6):
    encoded = urllib.parse.urlencode(params).encode('ascii')
    if post:
        request = urllib.request.Request(url, data=encoded, headers={
            'User-Agent': USER_AGENT, 'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        })
    else:
        request = urllib.request.Request(
            url + '?' + encoded.decode('ascii'),
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = json.load(response)
            error = data.get('error') if isinstance(data, dict) else None
            if not error:
                return data
            last = RuntimeError(f'ArcGIS error from {url}: {error}')
            code = error.get('code') if isinstance(error, dict) else None
            if code not in (429, 500, 502, 503, 504):
                raise last
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f'ArcGIS request failed: HTTP {exc.code} ({url})') from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'ArcGIS request failed after {tries} attempts: {last}')


def _snapshot_ids(layer, where):
    data = _request_json(f'{layer}/query', {
        'where': where, 'returnIdsOnly': 'true', 'f': 'json',
    })
    field = data.get('objectIdFieldName')
    raw_ids = data.get('objectIds')
    if not isinstance(field, str) or not field or not isinstance(raw_ids, list):
        raise RuntimeError(f'{layer} did not return a valid object-ID snapshot')
    ids = [_positive_oid(value, field) for value in raw_ids]
    if not ids or len(ids) != len(set(ids)):
        raise RuntimeError(f'{layer} object-ID snapshot is empty or duplicated')
    return field, sorted(ids)


def _iter_snapshot(layer, where, fields, *, geometry, page=ARCGIS_PAGE):
    """Yield one immutable ArcGIS object-ID snapshot with exact page checks."""
    oid_field, ids = _snapshot_ids(layer, where)
    label = '/'.join(layer.rstrip('/').split('/')[-2:])
    print(f'{label}: pinned {len(ids):,} object IDs')
    emitted = 0
    next_report = 50_000
    for start in range(0, len(ids), page):
        expected = ids[start:start + page]
        params = {
            'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(dict.fromkeys((oid_field, *fields))),
            'returnGeometry': str(bool(geometry)).lower(),
            'orderByFields': f'{oid_field} ASC',
            'f': 'geojson' if geometry else 'json',
        }
        if geometry:
            params.update({
                'outSR': 4326, 'geometryPrecision': 8,
                'returnTrueCurves': 'false',
            })
        data = _request_json(f'{layer}/query', params, post=True)
        features = data.get('features')
        if not isinstance(features, list):
            raise RuntimeError(f'{layer} snapshot page has no feature array')
        actual = [
            _positive_oid(_ci_properties(feature).get(oid_field.casefold()), oid_field)
            for feature in features
        ]
        if actual != expected:
            raise RuntimeError(
                f'{layer} snapshot page mismatch at {start}: '
                f'expected {expected[:3]}..{expected[-3:]}, '
                f'got {actual[:3]}..{actual[-3:]}')
        yield from features
        emitted += len(features)
        if emitted >= next_report or emitted == len(ids):
            print(f'{label}: {emitted:,}/{len(ids):,}')
            next_report = ((emitted // 50_000) + 1) * 50_000
    if emitted != len(ids):
        raise RuntimeError(f'{layer} emitted {emitted:,}; snapshot has {len(ids):,}')


def _state_where(states):
    return 'STATE IN (' + ','.join(f"'{state}'" for state in sorted(states)) + ')'


def _base_properties(*, fid, state, dataset, source_id, source_record_id,
                     scale, scale_status, reference, source_url):
    source_record_id = str(source_record_id)
    if re.fullmatch(r'\d+', source_record_id) is None:
        raise RuntimeError(
            f'{dataset} feature has invalid source record ID {source_record_id!r}')
    return {
        'fid': fid,
        'st': state,
        'state': state,
        'src': dataset,
        'source_dataset': dataset,
        'source_id': source_id,
        'source_record_id': source_record_id,
        'source_scale': scale,
        'source_scale_status': scale_status,
        'source_ref': _text(reference, 500),
        'source_url': _text(source_url, 300),
    }


def _normalize_sgmc_geology(feature, fid):
    p = _ci_properties(feature)
    state = _text(p.get('state'), 2)
    if state not in CONUS_STATES:
        raise RuntimeError(f'SGMC geology leaked unsupported state {state!r}')
    reference = _text(p.get('reference'), 2_000)
    scale, status = _source_scale(reference)
    props = _base_properties(
        fid=fid, state=state, dataset='usgs_sgmc_v1_1',
        source_id=_source_id('sgmc', state, reference), scale=scale,
        source_record_id=p.get('objectid'),
        scale_status=status, reference=reference,
        source_url=p.get('digital_url') or p.get('ngmdb1') or SGMC_DOI)
    props.update({
        'unit_label': _text(p.get('sgmc_label') or p.get('orig_label'), 40),
        'unit_name': _text(p.get('unit_name'), 160),
        'lithology': _text(p.get('generalized_lith') or p.get('major1'), 100),
        'age_min': _text(p.get('age_min'), 80),
        'age_max': _text(p.get('age_max'), 80),
    })
    return state, status, {
        'type': 'Feature', 'id': fid,
        'properties': {key: value for key, value in props.items() if value is not None},
        'geometry': _plain_geometry(feature.get('geometry'), {'Polygon', 'MultiPolygon'}),
    }


def _normalize_sgmc_fault(feature, fid):
    p = _ci_properties(feature)
    state = _text(p.get('state'), 2)
    if state not in CONUS_STATES:
        raise RuntimeError(f'SGMC structure leaked unsupported state {state!r}')
    description = _text(p.get('description'), 200)
    if not description or not FAULT_RE.search(description):
        raise RuntimeError(f'SGMC non-fault structure escaped server filter: {description!r}')
    reference = _text(p.get('reference'), 2_000)
    scale, status = _source_scale(reference)
    props = _base_properties(
        fid=fid, state=state, dataset='usgs_sgmc_v1_1',
        source_id=_source_id('sgmc', state, reference), scale=scale,
        source_record_id=p.get('objectid'),
        scale_status=status, reference=reference,
        source_url=p.get('digital_url') or p.get('ngmdb1') or SGMC_DOI)
    props.update({
        'fault_type': description,
        'notes': _text(p.get('misc'), 200),
    })
    return state, status, {
        'type': 'Feature', 'id': fid,
        'properties': {key: value for key, value in props.items() if value is not None},
        'geometry': _plain_geometry(feature.get('geometry'),
                                    {'LineString', 'MultiLineString'}),
    }


def _ak_references():
    references = {}
    for feature in _iter_snapshot(
            AK_REFS, '1=1', ('SOURCE', 'REFERENCE'), geometry=False, page=1_500):
        p = _ci_properties(feature)
        source = _text(p.get('source'), 40)
        reference = _text(p.get('reference'), 2_000)
        if not source or not reference or source in references:
            raise RuntimeError(f'AK SIM 3340 has invalid/duplicate source reference {source!r}')
        references[source] = reference
    if not references:
        raise RuntimeError('AK SIM 3340 reference table is empty')
    return references


def _ak_provenance(source, references):
    """Resolve an Alaska source without hiding SIM 3340's documented 001 gap."""
    reference = references.get(source)
    if reference:
        scale, status = _source_scale(reference)
        return reference, scale, status
    if re.fullmatch(r'[A-Z]{2}001', source or ''):
        return (AK_001_REFERENCE, 'nominal 1:250,000 compilation',
                'reserved_001_no_reference_row')
    raise RuntimeError(f'AK source {source!r} has no SIM 3340 reference')


def _normalize_ak_geology(feature, fid, references):
    p = _ci_properties(feature)
    source = _text(p.get('source'), 40)
    reference, scale, status = _ak_provenance(source, references)
    props = _base_properties(
        fid=fid, state='AK', dataset='usgs_sim3340',
        source_id=f'sim3340:{source}', scale=scale, scale_status=status,
        source_record_id=p.get('objectid'),
        reference=reference, source_url=AK_DOI)
    props.update({
        'unit_label': _text(p.get('state_label') or p.get('state_label2'), 40),
        'unit_name': _text(p.get('state_unitname'), 160),
        'age': _text(p.get('age_range'), 100),
        'quadrangle': _text(p.get('quadrangle'), 60),
        'color': _text(p.get('rgb_color'), 20),
    })
    return 'AK', status, {
        'type': 'Feature', 'id': fid,
        'properties': {key: value for key, value in props.items() if value is not None},
        'geometry': _plain_geometry(feature.get('geometry'), {'Polygon', 'MultiPolygon'}),
    }


def _normalize_ak_fault(feature, fid, references):
    p = _ci_properties(feature)
    fault_type = _text(p.get('line_type'), 200)
    if not fault_type or not FAULT_RE.search(fault_type):
        raise RuntimeError(f'AK non-fault line escaped server filter: {fault_type!r}')
    source = _text(p.get('source'), 40)
    reference, scale, status = _ak_provenance(source, references)
    props = _base_properties(
        fid=fid, state='AK', dataset='usgs_sim3340',
        source_id=f'sim3340:{source}', scale=scale, scale_status=status,
        source_record_id=p.get('objectid'),
        reference=reference, source_url=AK_DOI)
    props.update({'fault_type': fault_type})
    return 'AK', status, {
        'type': 'Feature', 'id': fid,
        'properties': props,
        'geometry': _plain_geometry(feature.get('geometry'),
                                    {'LineString', 'MultiLineString'}),
    }


def _download(url, path, tries=6):
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(request, timeout=180) as response, \
                    open(path, 'wb') as output:
                headers = dict(response.headers.items())
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            declared = headers.get('Content-Length')
            if declared is not None and int(declared) != size:
                raise RuntimeError(
                    f'{url} declared {declared} bytes but returned {size}')
            if size <= 0:
                raise RuntimeError(f'{url} returned an empty download')
            return {
                'bytes': size, 'sha256': digest.hexdigest(),
                'etag': headers.get('ETag'),
                'last_modified': headers.get('Last-Modified'),
            }
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
            if attempt + 1 < tries:
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'download failed after {tries} attempts ({url}): {last}')


def _extract_qfaults(archive_path, directory):
    required = (
        'SHP/Qfaults_US_Database.shp', 'SHP/Qfaults_US_Database.shx',
        'SHP/Qfaults_US_Database.dbf', 'SHP/Qfaults_US_Database.prj',
        'SHP/ca_offshore.shp', 'SHP/ca_offshore.shx',
        'SHP/ca_offshore.dbf', 'SHP/ca_offshore.prj',
    )
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError('Qfaults ZIP has duplicate member names')
            missing = sorted(set(required) - set(names))
            if missing:
                raise RuntimeError(f'Qfaults ZIP is missing required members: {missing}')
            total = sum(archive.getinfo(name).file_size for name in required)
            if total <= 0 or total > 1_000_000_000:
                raise RuntimeError(f'Qfaults ZIP extraction size is implausible: {total}')
            for name in required:
                target = os.path.join(directory, os.path.basename(name))
                with archive.open(name) as source, open(target, 'wb') as output:
                    shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f'Qfaults download is not a valid ZIP: {exc}') from exc
    return (os.path.join(directory, 'Qfaults_US_Database.shp'),
            os.path.join(directory, 'ca_offshore.shp'))


def _fiona_feature(feature):
    properties = dict(feature['properties'])
    properties['_source_record_id'] = str(feature.id)
    geometry = feature['geometry']
    if hasattr(geometry, '__geo_interface__'):
        geometry = geometry.__geo_interface__
    return {'properties': properties, 'geometry': geometry}


def _normalize_qfault(feature, fid, *, offshore=False):
    p = _ci_properties(feature)
    location = _text(p.get('location'), 100)
    if location == 'Hawaii':
        return None
    state = STATE_NAMES.get(location)
    if state not in TARGET_STATES:
        raise RuntimeError(f'Qfaults has unmapped location {location!r}')
    if offshore:
        fault_id = _text(p.get('fault_id'), 40)
        section = _text(p.get('section_id'), 20)
        scale = _text(p.get('mapped_sca'), 100)
        name = p.get('fault_name') or p.get('fault_zone')
        age = p.get('flt_age')
        slip_rate = p.get('slip_rate')
        slip_sense = p.get('slip_sense')
        fault_type = p.get('line_type')
        reference = p.get('flt_source') or 'USGS offshore Qfaults source record'
        source_url = QFAULTS_DOI
    else:
        fault_id = _text(p.get('fault_id'), 40)
        section = _text(p.get('section_id'), 20)
        scale = _text(p.get('scale'), 100)
        name = p.get('fault_name') or p.get('section_na')
        age = p.get('age')
        slip_rate = p.get('slip_rate')
        slip_sense = p.get('slip_sense')
        fault_type = p.get('linetype')
        reference = p.get('cooperator') or 'USGS Qfaults source record'
        source_url = p.get('fault_url') or QFAULTS_DOI
    if not scale:
        raise RuntimeError(f'Qfaults feature lacks source scale: {p!r}')
    scale_status = 'source_marks_unspecified' if scale.casefold() == 'unspecified' \
        else 'explicit'
    if fault_id:
        source_id = f'qfaults:{fault_id}' + (f':{section}' if section else '')
        catalog_id_status = 'present'
    elif offshore and re.fullmatch(r'\d+', str(p.get('_source_record_id') or '')):
        # The official offshore shapefile omits FAULT_ID on hundreds of valid
        # cited traces.  Preserve its stable source-row identity and label the
        # omission; never manufacture a USGS catalog ID.
        source_id = f'qfaults:ca_offshore:row:{p["_source_record_id"]}'
        catalog_id_status = 'absent_in_source'
    else:
        raise RuntimeError(f'Qfaults feature lacks fault ID: {p!r}')
    props = _base_properties(
        fid=fid, state=state, dataset='usgs_qfaults_2020',
        source_id=source_id, scale=scale, scale_status=scale_status,
        source_record_id=p.get('_source_record_id'),
        reference=reference, source_url=source_url)
    props.update({
        'name': _text(name, 160), 'fault_type': _text(fault_type, 100),
        'age': _text(age, 100), 'slip_rate': _text(slip_rate, 100),
        'slip_sense': _text(slip_sense, 60),
        'certainty': _text(p.get('certainty'), 100),
        'source_catalog_id_status': catalog_id_status,
    })
    return state, scale_status, {
        'type': 'Feature', 'id': fid,
        'properties': {key: value for key, value in props.items() if value is not None},
        'geometry': _plain_geometry(feature.get('geometry'),
                                    {'LineString', 'MultiLineString'}),
    }


def _empty_stats():
    return {
        'n': 0,
        'states': {state: 0 for state in sorted(TARGET_STATES)},
        'by_source': {},
        'source_scale_status': {},
        'geometry_normalizations': [],
    }


def _record(stats, state, source, scale_status, feature=None):
    stats['n'] += 1
    stats['states'][state] += 1
    stats['by_source'][source] = stats['by_source'].get(source, 0) + 1
    stats['source_scale_status'][scale_status] = (
        stats['source_scale_status'].get(scale_status, 0) + 1)
    props = (feature or {}).get('properties') or {}
    if props.get('geometry_normalization'):
        stats['geometry_normalizations'].append(_normalization_audit(props))


def _normalization_audit(props):
    return {
        'fid': props['fid'], 'st': props['st'],
        'source_dataset': props['source_dataset'],
        'source_id': props['source_id'],
        'source_record_id': props['source_record_id'],
        'source_geometry_sha256': props['source_geometry_sha256'],
        'source_geometry_length_m': props['source_geometry_length_m'],
        'geometry_normalization': props['geometry_normalization'],
        'geometry_normalization_engine': props['geometry_normalization_engine'],
        'geometry_normalization_reason': props['geometry_normalization_reason'],
        'geometry_normalization_delta_m': props['geometry_normalization_delta_m'],
        'geometry_normalization_parts': props['geometry_normalization_parts'],
    }


def _write_feature(output, feature):
    json.dump(feature, output, separators=(',', ':'), allow_nan=False)
    output.write('\n')


def _stream_geology(path, references):
    stats = _empty_stats()
    fid = 0
    sgmc_fields = (
        'OBJECTID', 'STATE', 'ORIG_LABEL', 'SGMC_LABEL', 'UNIT_NAME',
        'AGE_MIN', 'AGE_MAX', 'MAJOR1', 'GENERALIZED_LITH', 'REFERENCE',
        'DIGITAL_URL', 'NGMDB1')
    ak_fields = (
        'OBJECTID', 'SOURCE', 'STATE_LABEL', 'STATE_LABEL2', 'STATE_UNITNAME',
        'AGE_RANGE', 'QUADRANGLE', 'RGB_COLOR')
    with open(path, 'w', encoding='utf-8') as output:
        print('streaming 48-state USGS SGMC geology')
        for raw in _iter_snapshot(
                SGMC_GEOLOGY, _state_where(CONUS_STATES), sgmc_fields, geometry=True):
            fid += 1
            state, status, feature = _normalize_sgmc_geology(raw, fid)
            _write_feature(output, feature)
            _record(stats, state, 'usgs_sgmc_v1_1', status, feature)
        print('streaming Alaska USGS SIM 3340 geology')
        for raw in _iter_snapshot(AK_GEOLOGY, '1=1', ak_fields, geometry=True):
            fid += 1
            state, status, feature = _normalize_ak_geology(raw, fid, references)
            _write_feature(output, feature)
            _record(stats, state, 'usgs_sim3340', status, feature)
    present = {state for state, count in stats['states'].items() if count}
    if present != TARGET_STATES:
        raise RuntimeError(
            f'national geology does not cover exactly 49 states; '
            f'missing={sorted(TARGET_STATES - present)}, '
            f'extra={sorted(present - TARGET_STATES)}')
    return stats


def _stream_faults(path, references, qfault_paths):
    stats = _empty_stats()
    fid = 0
    sgmc_fields = (
        'OBJECTID', 'STATE', 'DESCRIPTION', 'MISC', 'REFERENCE',
        'DIGITAL_URL', 'NGMDB1')
    ak_fields = ('OBJECTID', 'SOURCE', 'LINE_TYPE')
    sgmc_where = (
        f'({_state_where(CONUS_STATES)}) AND '
        "(DESCRIPTION LIKE '%fault%' OR DESCRIPTION LIKE '%shear%')")
    with open(path, 'w', encoding='utf-8') as output:
        print('streaming 48-state USGS SGMC faults')
        for raw in _iter_snapshot(
                SGMC_STRUCTURE, sgmc_where, sgmc_fields, geometry=True):
            fid += 1
            state, status, feature = _normalize_sgmc_fault(raw, fid)
            _write_feature(output, feature)
            _record(stats, state, 'usgs_sgmc_v1_1', status, feature)
        print('streaming Alaska USGS SIM 3340 faults')
        for raw in _iter_snapshot(
                AK_LINES, "LINE_TYPE LIKE '%fault%'", ak_fields, geometry=True):
            fid += 1
            state, status, feature = _normalize_ak_fault(raw, fid, references)
            _write_feature(output, feature)
            _record(stats, state, 'usgs_sim3340', status, feature)
        print('streaming USGS Qfaults bulk archive')
        for path_index, source_path in enumerate(qfault_paths):
            with fiona.open(source_path) as source:
                if source.crs and str(source.crs).upper() not in (
                        'EPSG:4326', 'OGC:CRS84'):
                    raise RuntimeError(
                        f'Qfaults shapefile has unexpected CRS {source.crs!r}')
                for raw in source:
                    fid += 1
                    normalized = _normalize_qfault(
                        _fiona_feature(raw), fid, offshore=bool(path_index))
                    if normalized is None:
                        fid -= 1
                        continue
                    state, status, feature = normalized
                    _write_feature(output, feature)
                    _record(stats, state, 'usgs_qfaults_2020', status, feature)
    if stats['n'] != fid:
        raise RuntimeError(f'fault feature ID/count mismatch: {fid} != {stats["n"]}')
    if set(stats['states']) != TARGET_STATES:
        raise RuntimeError('fault state matrix is not exactly the WS11 49')
    if not all(stats['by_source'].get(source, 0) > 0 for source in (
            'usgs_sgmc_v1_1', 'usgs_sim3340', 'usgs_qfaults_2020')):
        raise RuntimeError(f'fault source unexpectedly empty: {stats["by_source"]}')
    return stats


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_pmtiles(path, layer, source_records):
    # Keep one authoritative PMTiles structural parser in the repository.
    from validate_national import _pmtiles_header
    required = (
        'fid', 'st', 'state', 'src', 'source_dataset', 'source_id',
        'source_record_id', 'source_scale', 'source_scale_status',
        'source_ref', 'source_url')
    return _pmtiles_header(
        path, [layer], {layer: required},
        verify_feature_properties=True, collect_feature_inventory=True,
        expected_source_records={layer: source_records})


def _assert_lossless_inventory(metadata, layer, source_records,
                               diagnostic_missing_fids=()):
    inventory = (metadata.get('feature_inventories') or {}).get(layer) or {}
    maxzoom = (metadata.get('maxzoom_feature_inventories') or {}).get(layer) or {}
    if (metadata.get('maxzoom') != 12 or
            inventory.get('unique_tiled_fids') != source_records or
            inventory.get('source_records') != source_records or
            inventory.get('missing_fid_count') != 0 or
            inventory.get('extra_fid_count') != 0 or
            maxzoom.get('unique_tiled_fids') != source_records or
            maxzoom.get('source_records') != source_records or
            maxzoom.get('missing_fid_count') != 0 or
            maxzoom.get('extra_fid_count') != 0):
        raise RuntimeError(
            f'{layer} source/tile fid loss at z12: source={source_records}, '
            f'all={inventory}, maxzoom={maxzoom}')
    return {
        'source_records': source_records,
        'unique_tiled_fids': inventory['unique_tiled_fids'],
        'maxzoom': 12,
        'maxzoom_unique_tiled_fids': maxzoom['unique_tiled_fids'],
        'missing_fid_count': 0, 'extra_fid_count': 0,
        'deterministic_builds': 2,
        'diagnostic_missing_fids': list(diagnostic_missing_fids),
        'diagnostic_missing_fids_sha256': hashlib.sha256(json.dumps(
            list(diagnostic_missing_fids), separators=(',', ':')).encode(
                'ascii')).hexdigest(),
    }


def _run_tippecanoe(sequence, output, layer, attribution):
    print(f'building {layer}.pmtiles')
    detail = TIPPECANOE_DETAILS[layer]
    subprocess.run([
        'tippecanoe', '--force', '--output', output,
        '--minimum-zoom=0', '--maximum-zoom=12',
        f'--full-detail={detail}',
        '--no-feature-limit', '--no-tile-size-limit', '--detect-shared-borders',
        '--simplify-only-low-zooms',
        '--no-tiny-polygon-reduction-at-maximum-zoom',
        # Each GeoJSON feature already carries its stable numeric top-level
        # ``id`` as well as the queryable ``fid`` property.  Do not promote
        # the property with --use-attribute-for-id: Tippecanoe intentionally
        # consumes that attribute and removes it from the tile schema.
        '--read-parallel', '--quiet',
        f'--name=NWMM national {layer}',
        f'--description=Lossless WS11 national {layer} baseline',
        f'--attribution={attribution}', '-L', f'{layer}:{sequence}',
    ], check=True)


def _inventory_gaps(metadata, layer):
    inventory = (metadata.get('feature_inventories') or {}).get(layer) or {}
    maxzoom = (metadata.get('maxzoom_feature_inventories') or {}).get(layer) or {}
    for label, value in (('all-zoom', inventory), ('z12', maxzoom)):
        if (value.get('missing_fids_truncated') or
                value.get('extra_fid_count') not in (None, 0)):
            raise RuntimeError(
                f'{layer} {label} tile inventory cannot be repaired exactly: {value}')
        if value.get('missing_fid_count', 0) != len(value.get('missing_fids') or []):
            raise RuntimeError(
                f'{layer} {label} missing-fid inventory is incomplete: {value}')
    return sorted(set((inventory.get('missing_fids') or []) +
                      (maxzoom.get('missing_fids') or [])))


def _normalize_missing_lines(sequence, missing_fids):
    missing = set(missing_fids)
    if not missing:
        return []
    directory = os.path.dirname(sequence)
    handle, pending = tempfile.mkstemp(prefix='.fault-normalized-', dir=directory)
    seen = set()
    audits = []
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as output, \
                open(sequence, encoding='utf-8') as source:
            for line_number, line in enumerate(source, 1):
                try:
                    feature = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f'fault sequence line {line_number} is invalid JSON') from exc
                props = feature.get('properties') or {}
                fid = props.get('fid')
                if fid in missing:
                    if fid in seen:
                        raise RuntimeError(f'fault sequence duplicates missing fid {fid}')
                    seen.add(fid)
                    geometry, audit = _normalize_collapsed_line_parts(
                        feature.get('geometry'), props, props.get('source_record_id'))
                    if audit is None:
                        raise RuntimeError(
                            f'fault fid {fid} was absent from z12 but its source '
                            'vertices do not collapse at 32-bit quantization')
                    feature['geometry'] = geometry
                    audits.append(_normalization_audit(props))
                _write_feature(output, feature)
            output.flush()
            os.fsync(output.fileno())
        if seen != missing:
            raise RuntimeError(
                f'fault normalization could not find fids {sorted(missing - seen)}')
        audits.sort(key=lambda item: item['fid'])
        os.replace(pending, sequence)
        return audits
    except BaseException:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _build_twice(sequence, output, layer, attribution, source_records, stats):
    """Diagnose loss, minimally repair collapsed lines, then prove determinism."""
    print(f'{layer}: lossless diagnostic build')
    _run_tippecanoe(sequence, output, layer, attribution)
    diagnostic = _validate_pmtiles(output, layer, source_records)
    diagnostic_missing = _inventory_gaps(diagnostic, layer)
    first = None
    final_metadata = None
    start = 1
    if diagnostic_missing:
        if layer != 'faults':
            raise RuntimeError(
                f'{layer} has non-line source/tile omissions {diagnostic_missing}; '
                'no geometry is invented or reclassified')
        print(f'{layer}: diagnostic missing fids {diagnostic_missing}; '
              'checking exact 32-bit line collapse')
        stats['geometry_normalizations'] = _normalize_missing_lines(
            sequence, diagnostic_missing)
    else:
        reconciliation = _assert_lossless_inventory(
            diagnostic, layer, source_records, diagnostic_missing)
        first = (os.path.getsize(output), _sha256(output))
        final_metadata = diagnostic
        start = 2
        print(f'{layer}: deterministic accepted build 1/2')
    for build_number in range(start, 3):
        print(f'{layer}: deterministic accepted build {build_number}/2')
        _run_tippecanoe(sequence, output, layer, attribution)
        metadata = _validate_pmtiles(output, layer, source_records)
        reconciliation = _assert_lossless_inventory(
            metadata, layer, source_records, diagnostic_missing)
        fingerprint = (os.path.getsize(output), _sha256(output))
        if first is None:
            first = fingerprint
        elif fingerprint != first:
            raise RuntimeError(
                f'{layer} PMTiles is nondeterministic: first={first}, '
                f'second={fingerprint}')
        final_metadata = metadata
    return {
        'bytes': first[0], 'sha256': first[1],
        'metadata': final_metadata, 'reconciliation': reconciliation,
    }


def _read_manifest():
    try:
        with open(MANIFEST, encoding='utf-8') as source:
            manifest = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'cannot read manifest {MANIFEST}: {exc}') from exc
    if not isinstance(manifest, dict):
        raise RuntimeError('manifest root must be an object')
    return manifest


def _entry(file_name, layer, stats, build_result, sources):
    source_summary = (
        'U.S. Geological Survey SGMC v1.1 and SIM 3340'
        if layer == 'geology' else
        'U.S. Geological Survey SGMC v1.1, SIM 3340, and Qfaults')
    normalizations = sorted(
        stats['geometry_normalizations'], key=lambda item: item['fid'])
    normalization_sha = hashlib.sha256(json.dumps(
        normalizations, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()
    return {
        'file': f'data/tiles/national/{file_name}',
        'format': 'pmtiles', 'source_layer': layer,
        'source': source_summary,
        'n': stats['n'], 'states': stats['states'],
        'by_source': stats['by_source'],
        'source_scale_status': stats['source_scale_status'],
        'retrieved': TODAY, 'sources': sources,
        'coverage': {
            'states': 49, 'excluded_state_codes': sorted(EXCLUDED_CODES),
            'zero_feature_states': sorted(
                state for state, count in stats['states'].items() if count == 0),
        },
        'provenance_properties': [
            'source_dataset', 'source_id', 'source_scale',
            'source_scale_status', 'source_ref', 'source_url',
            'source_catalog_id_status', 'source_record_id',
            'source_geometry_sha256', 'source_geometry_length_m',
            'geometry_normalization', 'geometry_normalization_engine',
            'geometry_normalization_reason',
            'geometry_normalization_delta_m',
            'geometry_normalization_parts'],
        'geometry_normalization': {
            'engine': GEOMETRY_NORMALIZATION_ENGINE,
            'reason': GEOMETRY_NORMALIZATION_REASON,
            'count': len(normalizations),
            'inventory_sha256': normalization_sha,
            'features': normalizations,
        },
        'tile_fid_reconciliation': build_result['reconciliation'],
        'bytes': build_result['bytes'], 'sha256': build_result['sha256'],
    }


def _stamp_manifest(geology, faults):
    manifest = _read_manifest()
    baselines = manifest.setdefault('national_baselines', {})
    baselines['geology'] = geology
    baselines['faults'] = faults
    directory = os.path.dirname(MANIFEST)
    mode = stat.S_IMODE(os.stat(MANIFEST).st_mode)
    handle, pending = tempfile.mkstemp(prefix='.manifest-geology-faults-', dir=directory)
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, separators=(',', ':'), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, MANIFEST)
    except BaseException:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _publication_entries(geology_stats, faults_stats, geology_build,
                         faults_build, qfault_download):
    common_sources = {
        'sgmc': {
            'title': 'State Geologic Map Compilation geodatabase v1.1',
            'authority': 'U.S. Geological Survey', 'url': SGMC_SERVICE,
            'item': SGMC_ITEM, 'doi': SGMC_DOI, 'release': '2017 v1.1',
            'scope': '48 conterminous states',
        },
        'alaska': {
            'title': 'Geologic map of Alaska, SIM 3340',
            'authority': 'U.S. Geological Survey', 'url': AK_SERVICE,
            'item': AK_ITEM, 'doi': AK_DOI, 'release': '2015',
            'scope': 'Alaska',
        },
        'macrostrat_gap_fill': {
            'used': False,
            'reason': 'No state coverage gap remains after SGMC plus SIM 3340.',
        },
    }
    geology_entry = _entry(
        'geology.pmtiles', 'geology', geology_stats, geology_build,
        common_sources)
    fault_sources = dict(common_sources)
    fault_sources['qfaults'] = {
        'title': 'Quaternary Fault and Fold Database for the Nation',
        'authority': 'U.S. Geological Survey', 'url': QFAULTS_URL,
        'doi': QFAULTS_DOI,
        'release': '2020 database; official archive refreshed 2025',
        'download': qfault_download,
    }
    faults_entry = _entry(
        'faults.pmtiles', 'faults', faults_stats, faults_build, fault_sources)
    return geology_entry, faults_entry


def _reserve_backup(path, label):
    handle, backup = tempfile.mkstemp(
        prefix=f'.{label}-rollback-', dir=os.path.dirname(path))
    os.close(handle)
    os.unlink(backup)
    return backup


def _publish_pair(pending_geology, pending_faults, geology_entry, faults_entry):
    """Replace two archives plus manifest, rolling all three back on failure."""
    publications = (
        (pending_geology, GEOLOGY_OUT, 'geology'),
        (pending_faults, FAULTS_OUT, 'faults'),
    )
    for pending, public, label in publications:
        if not os.path.isfile(pending) or not os.path.isfile(public):
            raise RuntimeError(
                f'{label} publication requires pending and existing public files')
        os.chmod(pending, stat.S_IMODE(os.stat(public).st_mode))
    manifest_mode = stat.S_IMODE(os.stat(MANIFEST).st_mode)
    handle, manifest_backup = tempfile.mkstemp(
        prefix='.manifest-geology-faults-rollback-',
        dir=os.path.dirname(MANIFEST))
    os.close(handle)
    shutil.copy2(MANIFEST, manifest_backup)
    os.chmod(manifest_backup, manifest_mode)
    archive_backups = []
    try:
        for pending, public, label in publications:
            backup = _reserve_backup(public, label)
            archive_backups.append((public, backup))
            os.replace(public, backup)
            os.replace(pending, public)
        _stamp_manifest(geology_entry, faults_entry)
    except BaseException:
        rollback_errors = []
        for public, backup in reversed(archive_backups):
            try:
                if os.path.exists(backup):
                    os.replace(backup, public)
            except BaseException as exc:  # pragma: no cover - catastrophic FS failure
                rollback_errors.append(f'{public}: {exc}')
        try:
            if os.path.exists(manifest_backup):
                os.replace(manifest_backup, MANIFEST)
        except BaseException as exc:  # pragma: no cover - catastrophic FS failure
            rollback_errors.append(f'{MANIFEST}: {exc}')
        if rollback_errors:
            raise RuntimeError(
                'geology/fault publication failed and rollback was incomplete: ' +
                '; '.join(rollback_errors))
        raise
    else:
        for _, backup in archive_backups:
            os.unlink(backup)
        os.unlink(manifest_backup)


def build():
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe >=2.79 with PMTiles output is required')
    if fiona is None:
        raise RuntimeError('Fiona is required to read the official Qfaults bulk shapefile')
    # Import the shared strict parser before any network or tiling work.  This
    # catches repository-module incompatibilities up front; _validate_pmtiles
    # still invokes the same canonical implementation immediately before
    # publication.
    from validate_national import _pmtiles_header  # noqa: F401
    os.makedirs(os.path.dirname(GEOLOGY_OUT), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nwmm-national-geology-') as temporary:
        qfault_zip = os.path.join(temporary, 'Qfaults_GIS.zip')
        qfault_download = _download(QFAULTS_URL, qfault_zip)
        qfault_paths = _extract_qfaults(qfault_zip, temporary)
        references = _ak_references()

        geology_sequence = os.path.join(temporary, 'geology.geojsonseq')
        faults_sequence = os.path.join(temporary, 'faults.geojsonseq')
        pending_geology = os.path.join(temporary, 'geology.pmtiles')
        pending_faults = os.path.join(temporary, 'faults.pmtiles')

        # Tile and remove the much larger geology sequence before streaming
        # faults. Both pending PMTiles remain private until both validations
        # finish, while peak scratch use stays bounded on release runners.
        geology_stats = _stream_geology(geology_sequence, references)
        geology_build = _build_twice(
            geology_sequence, pending_geology, 'geology',
            'U.S. Geological Survey SGMC v1.1 and SIM 3340',
            geology_stats['n'], geology_stats)
        os.unlink(geology_sequence)
        faults_stats = _stream_faults(faults_sequence, references, qfault_paths)
        faults_build = _build_twice(
            faults_sequence, pending_faults, 'faults',
            'U.S. Geological Survey SGMC v1.1, SIM 3340, and Qfaults',
            faults_stats['n'], faults_stats)
        os.unlink(faults_sequence)

        geology_entry, faults_entry = _publication_entries(
            geology_stats, faults_stats, geology_build, faults_build,
            qfault_download)

        grace = os.environ.get('NWMM_MANIFEST_STAMP_GRACE_SECONDS', '0')
        try:
            grace_seconds = int(grace)
        except ValueError as exc:
            raise RuntimeError(
                'NWMM_MANIFEST_STAMP_GRACE_SECONDS must be an integer') from exc
        if not 0 <= grace_seconds <= 60:
            raise RuntimeError(
                'NWMM_MANIFEST_STAMP_GRACE_SECONDS must be from 0 to 60')
        print('both archives validated twice with exact source/z12 fid '
              f'reconciliation; public replacement begins in {grace_seconds} seconds')
        if grace_seconds:
            time.sleep(grace_seconds)

        # The latest manifest is read only inside this transaction after the
        # grace period, preserving unrelated concurrent baseline keys. Any
        # BaseException restores both old archives and the old manifest.
        _publish_pair(
            pending_geology, pending_faults, geology_entry, faults_entry)

    result = {
        'geology': {
            'artifact': os.path.relpath(GEOLOGY_OUT, SITE),
            'features': geology_stats['n'], 'bytes': geology_build['bytes'],
            'sha256': geology_build['sha256'],
            'geometry_normalizations': len(
                geology_stats['geometry_normalizations']),
        },
        'faults': {
            'artifact': os.path.relpath(FAULTS_OUT, SITE),
            'features': faults_stats['n'], 'bytes': faults_build['bytes'],
            'sha256': faults_build['sha256'],
            'geometry_normalizations': len(
                faults_stats['geometry_normalizations']),
            'zero_feature_states': faults_entry['coverage']['zero_feature_states'],
        },
    }
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    build()
