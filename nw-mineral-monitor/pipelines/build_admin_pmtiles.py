#!/usr/bin/env python3
"""Build the frozen 2025 TIGERweb administrative PMTiles baseline.

This is a publication boundary, not a network fetcher.  It accepts two
checksummed private staging snapshots (states and counties), proves their exact
49-state/3,138-county FIPS inventories, builds twice with path-free Tippecanoe
arguments, and scans every unique MVT payload before preparing publication.

Public publication is explicit.  After a fixed grace window it replaces the
PMTiles archive and the byte-identical Lambda/build-side state clips, then
updates only ``national_baselines.admin`` and ``sources.boundaries`` in the
latest manifest as one rollback-protected transaction.  ``BaseException`` is
caught deliberately so an interrupt cannot leave those public identities from
different generations.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
import time
import urllib.error
import urllib.parse
import urllib.request

from state_registry import ALL_STATES
from validate_national import (
    _decompress_pmtiles,
    _pmtiles_header as _strict_pmtiles_header,
)


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
ADMIN_OUT = os.path.join(SITE, 'data', 'tiles', 'context', 'admin.pmtiles')
STATE_CLIPS_OUT = os.path.join(ROOT, 'infra', 'state_clips.json')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
PUBLICATION_LOCK = os.path.join(
    ROOT, 'build-inputs', '.staging', '.admin-publication.lock')

SERVICE = ('https://tigerweb.geo.census.gov/arcgis/rest/services/'
           'TIGERweb/State_County/MapServer')
SOURCE = SERVICE + '/0 (States) and /1 (Counties), January 1 2025 vintage'
AUTHORITY = 'U.S. Census Bureau'
VINTAGE = 'January 1 2025'
SERVICE_VINTAGE_TEXT = 'January 1, 2025'
BOUNDARIES_SOURCE = (
    'U.S. Census Bureau TIGERweb State_County MapServer, '
    'January 1 2025 vintage')
SYSTEM = 'national_admin_tigerweb'
SNAPSHOT_CONTRACT = 'tigerweb-objectids-double-pass-v1'
SCHEMA_VERSION = 1
MINZOOM = 0
MAXZOOM = 10
PUBLICATION_GRACE_SECONDS = 30
ATTRIBUTION = 'U.S. Census Bureau TIGERweb, January 1 2025 vintage'
EXPECTED_BOUNDS = [-179.23109, 24.39631, 179.85968, 71.43979]
EXPECTED_COUNTS = {'states': 49, 'counties': 3_138}
EXPECTED_FIPS_IDS_SHA256 = {
    'states': '155b69af91d4816940212a1ab613d9afaf6dd3219eaa9bd1ef63037ba1bcaef4',
    'counties': 'a37a3c2581375c33746a4fe50ab907b9fdde986521113b9f508d4fb155b48da1',
}
EXPECTED_PROPERTIES_SHA256 = {
    'states': '9557f8d931cbb98a3d55a98dee359d52544aa9396e57ff510fcb9633f2fcb4b3',
    'counties': '8af5dcb479d0312f1cf012d909231e1b5d53e25557fa9798671368650c40aa64',
}
PREVIOUS_PATH_DEPENDENT_ARTIFACT = {
    'bytes': 7_895_670,
    'sha256': '4aba83f4929ab7c04ccd3c6b0a9d938d45c9ab082789479ba70d01c6d6c446aa',
}
CURRENT_ACCEPTED_ARTIFACT = {
    'bytes': 7_743_967,
    'sha256': '94c3a78b2ca17f02223e6d5161afde763a370b515e710723e76395b520e2c3df',
}
CURRENT_ACCEPTED_STATE_CLIPS = {
    'bytes': 707_923,
    'sha256': '33c09d367d74a1ce0c88934d4adb548557733bf7da9105be039f5f16ed22c552',
}
# The reviewed NV/AZ/CO/UT state-survey descriptors bind the byte identity of
# ``infra/state_clips.json``.  The order below is therefore a publication
# contract, not presentation trivia.  It is the historical TIGERweb source
# order used by those reviewed builds; pinning it makes a fresh exact source
# capture reproduce the same 707,923-byte document without migrating every
# downstream descriptor merely to sort JSON object keys.
STATE_CLIP_ORDER = (
    'NH', 'CA', 'NJ', 'SC', 'MI', 'AR', 'MS', 'MO', 'MT', 'KS', 'IN', 'SD',
    'CO', 'PA', 'WA', 'LA', 'ME', 'NY', 'NV', 'AK', 'VT', 'CT', 'DE', 'NM',
    'NC', 'WI', 'OR', 'NE', 'GA', 'AL', 'UT', 'OH', 'OK', 'TN', 'WY', 'MA',
    'VA', 'IA', 'AZ', 'TX', 'ND', 'KY', 'WV', 'FL', 'IL', 'MN', 'MD', 'RI',
    'ID',
)
REQUIRED_PROPERTIES = {
    'states': ('fips', 'name', 'st'),
    'counties': ('fips', 'name', 'st'),
}
SNAPSHOT_FILES = {'states': 'states.geojson', 'counties': 'counties.geojson'}
SNAPSHOT_FIELDS = {
    'states': ('GEOID', 'STUSAB', 'NAME'),
    'counties': ('GEOID', 'STATE', 'NAME'),
}
SHA256_RE = re.compile(r'[0-9a-f]{64}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
TIPPECANOE_RE = re.compile(r'tippecanoe v?(\d+)\.(\d+)(?:\.(\d+))?')
CAPTURE_USER_AGENT = 'nw-mineral-monitor/11 exact admin snapshot producer'
TRANSIENT_HTTP = frozenset((429, 500, 502, 503, 504))

# Census state FIPS identity is part of this frozen baseline.  DC, territories,
# and Hawaii are intentionally absent; all and only the WS11 state codes remain.
STATE_IDENTITIES = {
    '01': ('AL', 'Alabama'), '02': ('AK', 'Alaska'),
    '04': ('AZ', 'Arizona'), '05': ('AR', 'Arkansas'),
    '06': ('CA', 'California'), '08': ('CO', 'Colorado'),
    '09': ('CT', 'Connecticut'), '10': ('DE', 'Delaware'),
    '12': ('FL', 'Florida'), '13': ('GA', 'Georgia'),
    '16': ('ID', 'Idaho'), '17': ('IL', 'Illinois'),
    '18': ('IN', 'Indiana'), '19': ('IA', 'Iowa'),
    '20': ('KS', 'Kansas'), '21': ('KY', 'Kentucky'),
    '22': ('LA', 'Louisiana'), '23': ('ME', 'Maine'),
    '24': ('MD', 'Maryland'), '25': ('MA', 'Massachusetts'),
    '26': ('MI', 'Michigan'), '27': ('MN', 'Minnesota'),
    '28': ('MS', 'Mississippi'), '29': ('MO', 'Missouri'),
    '30': ('MT', 'Montana'), '31': ('NE', 'Nebraska'),
    '32': ('NV', 'Nevada'), '33': ('NH', 'New Hampshire'),
    '34': ('NJ', 'New Jersey'), '35': ('NM', 'New Mexico'),
    '36': ('NY', 'New York'), '37': ('NC', 'North Carolina'),
    '38': ('ND', 'North Dakota'), '39': ('OH', 'Ohio'),
    '40': ('OK', 'Oklahoma'), '41': ('OR', 'Oregon'),
    '42': ('PA', 'Pennsylvania'), '44': ('RI', 'Rhode Island'),
    '45': ('SC', 'South Carolina'), '46': ('SD', 'South Dakota'),
    '47': ('TN', 'Tennessee'), '48': ('TX', 'Texas'),
    '49': ('UT', 'Utah'), '50': ('VT', 'Vermont'),
    '51': ('VA', 'Virginia'), '53': ('WA', 'Washington'),
    '54': ('WV', 'West Virginia'), '55': ('WI', 'Wisconsin'),
    '56': ('WY', 'Wyoming'),
}

if set(code for code, _ in STATE_IDENTITIES.values()) != set(ALL_STATES):
    raise RuntimeError('admin state-FIPS table does not equal the WS11 49 states')


class PublicationError(ValueError):
    """A private snapshot, tile artifact, or publication step is invalid."""


def _reject_json_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def _strict_json_bytes(raw, label):
    try:
        value = json.loads(
            raw.decode('utf-8'), parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PublicationError(f'{label} is not strict JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise PublicationError(f'{label} top level must be an object')
    return value


def _read_strict_json(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot read {label}: {exc}') from exc
    return _strict_json_bytes(raw, label), raw


def _canonical_bytes(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PublicationError(f'value is not canonical JSON: {exc}') from exc


def _manifest_bytes(value):
    """Encode the public manifest exactly as ``reconcile_manifest.py`` does.

    Build artifacts use sorted, UTF-8 canonical JSON, but the repository's
    public-manifest contract intentionally preserves insertion order and uses
    JSON's ASCII escapes. Keeping this encoder separate prevents an artifact
    canonicalizer from making a freshly published manifest immediately stale.
    """
    try:
        return json.dumps(value, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PublicationError(f'value is not valid manifest JSON: {exc}') from exc


def _state_clip_bytes(state_geometries):
    """Serialize the accepted clip document in its pinned historical order."""
    if (not isinstance(state_geometries, dict) or
            set(state_geometries) != set(STATE_CLIP_ORDER) or
            len(STATE_CLIP_ORDER) != len(set(STATE_CLIP_ORDER)) or
            set(STATE_CLIP_ORDER) != set(ALL_STATES)):
        raise PublicationError('state clip geometry inventory is not exact')
    ordered_states = {}
    for code in STATE_CLIP_ORDER:
        geometry = state_geometries[code]
        _validate_polygon_geometry(geometry, f'state clip {code}')
        # Pin geometry-member order too. JSON object order is semantically
        # irrelevant, but these bytes are already provenance-bound downstream.
        ordered_states[code] = {
            'type': geometry['type'],
            'coordinates': geometry['coordinates'],
        }
    document = {
        'schema_version': 1,
        'source': SOURCE,
        'note': ('Build-side/Lambda spatial clips only; browser delivery is the '
                 'admin PMTiles archive.'),
        'states': ordered_states,
    }
    try:
        raw = json.dumps(
            document, sort_keys=False, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PublicationError(f'state clips are not strict JSON: {exc}') from exc
    if (len(raw) != CURRENT_ACCEPTED_STATE_CLIPS['bytes'] or
            _sha256_bytes(raw) != CURRENT_ACCEPTED_STATE_CLIPS['sha256']):
        raise PublicationError(
            'fresh TIGERweb state clips do not reproduce the accepted '
            'downstream byte identity')
    return document, raw


def _sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _ids_sha256(ids):
    return _sha256_bytes(json.dumps(
        sorted(ids), separators=(',', ':'), allow_nan=False).encode('ascii'))


def _properties_sha256(rows):
    """Hash exact browser semantics, independent of feature/tile ordering."""
    values = sorted(
        [feature_id, *signature]
        for feature_id, signature in rows.items())
    return _sha256_bytes(_canonical_bytes(values))


def _outside(path, parent):
    path = os.path.realpath(path)
    parent = os.path.realpath(parent)
    try:
        return os.path.commonpath((path, parent)) != parent
    except ValueError:
        return True


def _private_file(path, label):
    path = os.path.realpath(path)
    if not os.path.isfile(path) or not _outside(path, SITE):
        raise PublicationError(f'{label} must be a private file outside site/')
    return path


def _private_output(path):
    path = os.path.realpath(path)
    forbidden = {
        os.path.realpath(os.sep), os.path.realpath(ROOT),
        os.path.realpath(os.path.expanduser('~')),
    }
    if not _outside(path, SITE) or path in forbidden:
        raise PublicationError(
            'private output directory must be a narrow path outside site/')
    os.makedirs(path, exist_ok=True)
    if not os.path.isdir(path):
        raise PublicationError('private output path is not a directory')
    return path


def _date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be YYYY-MM-DD')
    return value


def _text(value, label, maximum=300):
    if (not isinstance(value, str) or not value.strip() or value != value.strip() or
            len(value) > maximum or any(ord(char) < 32 for char in value)):
        raise PublicationError(f'{label} must be substantive trimmed text')
    return value


def _validate_coordinate_tree(value, label, depth=0):
    if depth > 8 or not isinstance(value, list) or not value:
        raise PublicationError(f'{label} has invalid coordinate nesting')
    if all(isinstance(item, (int, float)) and not isinstance(item, bool)
           for item in value):
        if len(value) < 2 or not all(math.isfinite(item) for item in value[:2]):
            raise PublicationError(f'{label} has a non-finite coordinate')
        lon, lat = value[:2]
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise PublicationError(f'{label} coordinate is outside WGS84')
        return
    for index, item in enumerate(value):
        _validate_coordinate_tree(item, f'{label}[{index}]', depth + 1)


def _validate_polygon_geometry(value, label):
    if not isinstance(value, dict) or set(value) != {'type', 'coordinates'}:
        raise PublicationError(f'{label} must be a strict GeoJSON geometry')
    geometry_type = value.get('type')
    coordinates = value.get('coordinates')
    if geometry_type not in ('Polygon', 'MultiPolygon'):
        raise PublicationError(f'{label} must be Polygon/MultiPolygon')
    _validate_coordinate_tree(coordinates, f'{label}.coordinates')
    polygons = [coordinates] if geometry_type == 'Polygon' else coordinates
    if not isinstance(polygons, list) or not polygons:
        raise PublicationError(f'{label} has no polygons')
    for polygon_index, polygon in enumerate(polygons):
        if not isinstance(polygon, list) or not polygon:
            raise PublicationError(f'{label} polygon {polygon_index} has no rings')
        for ring_index, ring in enumerate(polygon):
            if (not isinstance(ring, list) or len(ring) < 4 or any(
                    not isinstance(position, list) or len(position) != 2 or
                    any(not isinstance(number, (int, float)) or
                        isinstance(number, bool) or not math.isfinite(number)
                        for number in position)
                    for position in ring) or ring[0] != ring[-1] or
                    len({tuple(position) for position in ring[:-1]}) < 3):
                raise PublicationError(
                    f'{label} ring {polygon_index}/{ring_index} is degenerate '
                    'or not closed')


def _snapshot_query(layer):
    state_codes = ','.join(f"'{code}'" for code in sorted(ALL_STATES))
    state_fips = ','.join(f"'{code}'" for code in sorted(STATE_IDENTITIES))
    return {
        'where': (f'STUSAB IN ({state_codes})' if layer == 'states'
                  else f'STATE IN ({state_fips})'),
        'out_fields': ['OBJECTID', *SNAPSHOT_FIELDS[layer]],
        'out_sr': 4326,
        'geometry_precision': 5,
        'max_allowable_offset': 0.002,
    }


def _request_json(url, params=None, *, post=False, tries=6):
    params = params or {}
    encoded = urllib.parse.urlencode(params).encode('utf-8')
    headers = {'User-Agent': CAPTURE_USER_AGENT, 'Accept': 'application/json'}
    if post:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(url, data=encoded, headers=headers)
    else:
        suffix = ('?' + encoded.decode('ascii')) if encoded else ''
        request = urllib.request.Request(url + suffix, headers=headers)
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise PublicationError(f'TIGERweb returned non-object JSON from {url}')
            error = value.get('error')
            if not error:
                return value
            last = RuntimeError(f'TIGERweb error from {url}: {error}')
            code = error.get('code') if isinstance(error, dict) else None
            if code not in TRANSIENT_HTTP:
                raise last
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in TRANSIENT_HTTP:
                raise PublicationError(
                    f'TIGERweb HTTP {exc.code} from {url}') from exc
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise PublicationError(
        f'TIGERweb request failed after {tries} attempts: {last}')


def _metadata_observation(layer):
    layer_id = 0 if layer == 'states' else 1
    expected_name = 'States' if layer == 'states' else 'Counties'
    expected_description = (
        f'{expected_name} (or statistically equivalent entities); '
        f'{SERVICE_VINTAGE_TEXT} vintage')
    url = f'{SERVICE}/{layer_id}'
    metadata = _request_json(url, {'f': 'json'})
    capabilities = {
        item.strip().casefold()
        for item in str(metadata.get('capabilities') or '').split(',')}
    fields = metadata.get('fields')
    if (metadata.get('id') != layer_id or
            metadata.get('name') != expected_name or
            metadata.get('type') != 'Feature Layer' or
            metadata.get('geometryType') != 'esriGeometryPolygon' or
            metadata.get('description') != expected_description or
            metadata.get('copyrightText') != 'Source: U.S. Census Bureau' or
            'query' not in capabilities or not isinstance(fields, list)):
        raise PublicationError(f'TIGERweb {layer} metadata identity changed')
    schema = {
        field.get('name'): field.get('type')
        for field in fields if isinstance(field, dict) and
        isinstance(field.get('name'), str)}
    expected_schema = {
        'OBJECTID': 'esriFieldTypeOID',
        'GEOID': 'esriFieldTypeString',
        'NAME': 'esriFieldTypeString',
        ('STUSAB' if layer == 'states' else 'STATE'): 'esriFieldTypeString',
    }
    mismatches = {
        field: (expected_type, schema.get(field))
        for field, expected_type in expected_schema.items()
        if schema.get(field) != expected_type}
    typed_oids = sorted(
        field.get('name') for field in fields if isinstance(field, dict) and
        field.get('type') == 'esriFieldTypeOID' and
        isinstance(field.get('name'), str))
    if mismatches or typed_oids != ['OBJECTID']:
        raise PublicationError(
            f'TIGERweb {layer} required field schema changed: {mismatches}')
    selected = {
        'id': metadata.get('id'),
        'name': metadata.get('name'),
        'type': metadata.get('type'),
        'geometryType': metadata.get('geometryType'),
        'description': metadata.get('description'),
        'copyrightText': metadata.get('copyrightText'),
        'capabilities': metadata.get('capabilities'),
        'currentVersion': metadata.get('currentVersion'),
        'maxRecordCount': metadata.get('maxRecordCount'),
        'objectIdField': 'OBJECTID',
        'sourceSpatialReference': metadata.get('sourceSpatialReference'),
        'fields': [
            {key: field.get(key) for key in ('name', 'type', 'alias', 'length')}
            for field in fields if isinstance(field, dict)
        ],
    }
    return selected, _sha256_bytes(_canonical_bytes(selected))


def _positive_object_ids(values, label):
    if not isinstance(values, list) or not values:
        raise PublicationError(f'TIGERweb {label} returned no object IDs')
    result = []
    for value in values:
        if (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise PublicationError(
                f'TIGERweb {label} object IDs must be positive integers')
        result.append(value)
    result.sort()
    if len(result) != len(set(result)):
        raise PublicationError(f'TIGERweb {label} object IDs contain duplicates')
    return result


def _id_observation(layer):
    layer_id = 0 if layer == 'states' else 1
    query_url = f'{SERVICE}/{layer_id}/query'
    where = _snapshot_query(layer)['where']
    ids_result = _request_json(query_url, {
        'where': where, 'returnIdsOnly': 'true', 'f': 'json',
    }, post=True)
    if ids_result.get('objectIdFieldName') != 'OBJECTID':
        raise PublicationError(
            f'TIGERweb {layer} returnIdsOnly object-ID field changed')
    object_ids = _positive_object_ids(ids_result.get('objectIds'), layer)
    count_result = _request_json(query_url, {
        'where': where, 'returnCountOnly': 'true', 'f': 'json',
    }, post=True)
    count = count_result.get('count')
    if (not isinstance(count, int) or isinstance(count, bool) or
            count != len(object_ids) or count != EXPECTED_COUNTS[layer]):
        raise PublicationError(
            f'TIGERweb {layer} count/ID inventory is {count}/{len(object_ids)}, '
            f'expected {EXPECTED_COUNTS[layer]}')
    return object_ids, _ids_sha256(object_ids)


def _capture_feature_pass(layer, object_ids, *, pass_label):
    layer_id = 0 if layer == 'states' else 1
    query_url = f'{SERVICE}/{layer_id}/query'
    query = _snapshot_query(layer)
    fields = query['out_fields']
    selected_fields = set(SNAPSHOT_FIELDS[layer])
    rows = []
    page_size = 500
    for page_number, start in enumerate(
            range(0, len(object_ids), page_size), start=1):
        expected = object_ids[start:start + page_size]
        response = _request_json(query_url, {
            'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(fields),
            'returnGeometry': 'true',
            'returnTrueCurves': 'false',
            'outSR': query['out_sr'],
            'geometryPrecision': query['geometry_precision'],
            'maxAllowableOffset': query['max_allowable_offset'],
            'orderByFields': 'OBJECTID ASC',
            'f': 'geojson',
        }, post=True)
        if response.get('exceededTransferLimit') is True:
            raise PublicationError(
                f'TIGERweb {layer} {pass_label} page exceeded transfer limit')
        features = response.get('features')
        if response.get('type') != 'FeatureCollection' or not isinstance(
                features, list):
            raise PublicationError(
                f'TIGERweb {layer} {pass_label} page is not GeoJSON')
        observed = []
        for index, feature in enumerate(features):
            label = f'TIGERweb {layer} {pass_label} page {page_number}[{index}]'
            if (not isinstance(feature, dict) or
                    set(feature) != {'type', 'id', 'geometry', 'properties'} or
                    feature.get('type') != 'Feature' or
                    not isinstance(feature.get('properties'), dict) or
                    set(feature['properties']) != set(fields)):
                raise PublicationError(f'{label} feature schema is invalid')
            properties = feature['properties']
            object_id = properties.get('OBJECTID')
            if (not isinstance(object_id, int) or isinstance(object_id, bool) or
                    feature.get('id') != object_id):
                raise PublicationError(f'{label} OBJECTID/top-level ID changed')
            _validate_polygon_geometry(feature.get('geometry'), f'{label}.geometry')
            observed.append(object_id)
            rows.append((object_id, {
                'type': 'Feature',
                'properties': {
                    field: properties[field] for field in SNAPSHOT_FIELDS[layer]
                    if field in selected_fields},
                'geometry': feature['geometry'],
            }))
        if observed != expected:
            raise PublicationError(
                f'TIGERweb {layer} {pass_label} page object IDs changed; '
                f'expected={expected[:3]}..{expected[-3:]}, '
                f'observed={observed[:3]}..{observed[-3:]}')
        print(
            f'TIGERweb {layer} {pass_label}: '
            f'{min(start + len(expected), len(object_ids))}/{len(object_ids)}',
            flush=True)
    if [object_id for object_id, _ in rows] != object_ids:
        raise PublicationError(
            f'TIGERweb {layer} {pass_label} did not exhaust pinned object IDs')
    return rows


def _capture_layer(layer, retrieved):
    metadata_before, metadata_sha256 = _metadata_observation(layer)
    object_ids, object_ids_sha256 = _id_observation(layer)
    first = _capture_feature_pass(layer, object_ids, pass_label='first pass')
    second = _capture_feature_pass(layer, object_ids, pass_label='second pass')
    if first != second:
        changed = [
            object_id for (object_id, before), (_, after) in zip(first, second)
            if before != after]
        raise PublicationError(
            f'TIGERweb {layer} feature content changed between passes; '
            f'OBJECTIDs={changed[:25]}')
    metadata_after, metadata_after_sha256 = _metadata_observation(layer)
    object_ids_after, object_ids_after_sha256 = _id_observation(layer)
    if (metadata_after != metadata_before or
            metadata_after_sha256 != metadata_sha256):
        raise PublicationError(f'TIGERweb {layer} metadata changed during capture')
    if (object_ids_after != object_ids or
            object_ids_after_sha256 != object_ids_sha256):
        raise PublicationError(
            f'TIGERweb {layer} object-ID inventory changed during capture')
    # Source-service order is OBJECTID. Public bytes and evidence instead use
    # stable administrative identity order, independent of service row order.
    features = [feature for _, feature in first]
    features.sort(key=lambda feature: int(feature['properties']['GEOID']))
    normalized = _normalize_features(features, layer)
    if _normalized_properties_sha256(
            normalized, layer) != EXPECTED_PROPERTIES_SHA256[layer]:
        raise PublicationError(
            f'TIGERweb {layer} exact FIPS/name/state values drifted')
    records_sha256 = _sha256_bytes(_canonical_bytes(features))
    source = f'{SERVICE}/{0 if layer == "states" else 1}'
    source_snapshot_id = _sha256_bytes(_canonical_bytes({
        'layer': layer, 'source': source,
        'metadata_sha256': metadata_sha256,
        'object_ids_sha256': object_ids_sha256,
        'records_sha256': records_sha256,
    }))
    document = {
        'schema_version': SCHEMA_VERSION,
        'system': SYSTEM,
        'vintage': VINTAGE,
        'layer': layer,
        'source': source,
        'retrieved': retrieved,
        'complete': True,
        'truncated': False,
        'pagination': {
            'method': SNAPSHOT_CONTRACT,
            'source_count': len(object_ids),
            'fetched_count': len(features),
            'object_id_field': 'OBJECTID',
            'object_ids_sha256': object_ids_sha256,
            'metadata_sha256': metadata_sha256,
            'records_sha256': records_sha256,
            'page_size': 500,
            'page_count': math.ceil(len(object_ids) / 500),
            'full_second_feature_pass': True,
            'postflight_metadata_match': True,
            'postflight_object_ids_match': True,
            'exceeded_transfer_limit': False,
            'source_snapshot_id': source_snapshot_id,
        },
        'query': _snapshot_query(layer),
        'type': 'FeatureCollection',
        'features': features,
    }
    _validate_snapshot_header(document, layer)
    return document


def _install_new_private_files(files, output_directory):
    destinations = {
        name: os.path.join(output_directory, name) for name in files}
    existing = sorted(path for path in destinations.values() if os.path.exists(path))
    if existing:
        raise PublicationError(
            f'private capture refuses to overwrite existing files: {existing}')
    prepared = []
    installed = []
    try:
        for name, raw in files.items():
            destination = destinations[name]
            prepared.append((_atomic_pending_bytes(
                raw, destination, f'.{name}-capture-'), destination))
        for pending, destination in prepared:
            os.replace(pending, destination)
            installed.append(destination)
    except BaseException:
        for destination in reversed(installed):
            try:
                os.unlink(destination)
            except FileNotFoundError:
                pass
        raise
    finally:
        for pending, _ in prepared:
            try:
                os.unlink(pending)
            except FileNotFoundError:
                pass
    return destinations


def capture_staging(output_directory, *, retrieved=None):
    """Capture one exact official TIGERweb generation into private staging."""
    output_directory = _private_output(output_directory)
    retrieved = retrieved or dt.date.today().isoformat()
    _date(retrieved, 'capture retrieval date')
    documents = {
        layer: _capture_layer(layer, retrieved)
        for layer in ('states', 'counties')}
    raw = {layer: _canonical_bytes(document)
           for layer, document in documents.items()}
    layers = {}
    for layer in ('states', 'counties'):
        layers[layer] = {
            'file': SNAPSHOT_FILES[layer],
            'bytes': len(raw[layer]),
            'sha256': _sha256_bytes(raw[layer]),
            'count': EXPECTED_COUNTS[layer],
            'fips_ids_sha256': EXPECTED_FIPS_IDS_SHA256[layer],
            'properties_sha256': EXPECTED_PROPERTIES_SHA256[layer],
        }
    inventory = {
        'schema_version': SCHEMA_VERSION,
        'system': SYSTEM,
        'created': retrieved,
        'source': SERVICE,
        'vintage': VINTAGE,
        'layers': layers,
    }
    files = {
        SNAPSHOT_FILES[layer]: raw[layer] for layer in ('states', 'counties')}
    files['inventory.json'] = _canonical_bytes(inventory)
    destinations = _install_new_private_files(files, output_directory)
    # Re-open through the publication builder's independent strict boundary.
    context = load_staging(
        output_directory, os.path.join(output_directory, 'inventory.json'))
    result = {
        'staging_dir': output_directory,
        'inventory': destinations['inventory.json'],
        'inventory_sha256': context['inventory_sha256'],
        'layers': layers,
        'retrieved': retrieved,
    }
    print(json.dumps({'private_capture': result}, indent=2, sort_keys=True))
    return result


def _validate_snapshot_header(document, layer):
    required = {
        'schema_version', 'system', 'vintage', 'layer', 'source', 'retrieved',
        'complete', 'truncated', 'pagination', 'query', 'type', 'features',
    }
    if set(document) != required:
        raise PublicationError(
            f'{layer} snapshot keys must be exactly {sorted(required)}')
    if (document.get('schema_version') != SCHEMA_VERSION or
            document.get('system') != SYSTEM or
            document.get('vintage') != VINTAGE or
            document.get('layer') != layer or
            document.get('source') !=
            f'{SERVICE}/{0 if layer == "states" else 1}' or
            document.get('complete') is not True or
            document.get('truncated') is not False or
            document.get('type') != 'FeatureCollection' or
            document.get('query') != _snapshot_query(layer)):
        raise PublicationError(f'{layer} snapshot identity/query is invalid')
    _date(document.get('retrieved'), f'{layer}.retrieved')
    features = document.get('features')
    if not isinstance(features, list):
        raise PublicationError(f'{layer}.features must be an array')
    pagination = document.get('pagination')
    expected = EXPECTED_COUNTS[layer]
    if (not isinstance(pagination, dict) or set(pagination) != {
            'method', 'source_count', 'fetched_count', 'object_id_field',
            'object_ids_sha256', 'metadata_sha256', 'records_sha256',
            'page_size', 'page_count', 'full_second_feature_pass',
            'postflight_metadata_match', 'postflight_object_ids_match',
            'exceeded_transfer_limit', 'source_snapshot_id'} or
            pagination.get('method') != SNAPSHOT_CONTRACT or
            pagination.get('source_count') != expected or
            pagination.get('fetched_count') != expected or
            pagination.get('object_id_field') != 'OBJECTID' or
            any(not isinstance(pagination.get(field), str) or
                SHA256_RE.fullmatch(pagination[field]) is None
                for field in ('object_ids_sha256', 'metadata_sha256',
                              'records_sha256', 'source_snapshot_id')) or
            pagination.get('page_size') != 500 or
            pagination.get('page_count') !=
            math.ceil(expected / pagination['page_size']) or
            pagination.get('full_second_feature_pass') is not True or
            pagination.get('postflight_metadata_match') is not True or
            pagination.get('postflight_object_ids_match') is not True or
            pagination.get('exceeded_transfer_limit') is not False):
        raise PublicationError(f'{layer} pagination/count evidence is invalid')
    if len(features) != expected:
        raise PublicationError(
            f'{layer} snapshot has {len(features)} features, expected {expected}')
    records_sha256 = _sha256_bytes(_canonical_bytes(features))
    source_snapshot_id = _sha256_bytes(_canonical_bytes({
        'layer': layer,
        'source': document['source'],
        'metadata_sha256': pagination['metadata_sha256'],
        'object_ids_sha256': pagination['object_ids_sha256'],
        'records_sha256': records_sha256,
    }))
    if (pagination['records_sha256'] != records_sha256 or
            pagination['source_snapshot_id'] != source_snapshot_id):
        raise PublicationError(
            f'{layer} record/source snapshot fingerprint is invalid')
    return features


def _normalize_feature(feature, layer, index, *, state_identities):
    label = f'{layer}.features[{index}]'
    if (not isinstance(feature, dict) or
            set(feature) != {'type', 'properties', 'geometry'} or
            feature.get('type') != 'Feature'):
        raise PublicationError(f'{label} must be a strict GeoJSON Feature')
    properties = feature.get('properties')
    expected_fields = set(SNAPSHOT_FIELDS[layer])
    if not isinstance(properties, dict) or set(properties) != expected_fields:
        raise PublicationError(
            f'{label}.properties must be exactly {sorted(expected_fields)}')
    geometry = feature.get('geometry')
    _validate_polygon_geometry(geometry, f'{label}.geometry')
    fips = properties.get('GEOID')
    pattern = r'\d{2}' if layer == 'states' else r'\d{5}'
    if not isinstance(fips, str) or re.fullmatch(pattern, fips) is None:
        raise PublicationError(f'{label}.GEOID is not canonical FIPS')
    name = _text(properties.get('NAME'), f'{label}.NAME')
    if layer == 'states':
        identity = state_identities.get(fips)
        if identity is None:
            raise PublicationError(f'{label} has unexpected state FIPS {fips}')
        st, expected_name = identity
        if properties.get('STUSAB') != st or name != expected_name:
            raise PublicationError(f'{label} state FIPS/code/name identity changed')
    else:
        state_fips = properties.get('STATE')
        identity = state_identities.get(state_fips)
        if (not isinstance(state_fips, str) or fips[:2] != state_fips or
                identity is None):
            raise PublicationError(f'{label} county/state FIPS identity changed')
        st = identity[0]
    fid = int(fips)
    normalized = {
        'type': 'Feature',
        'id': fid,
        'properties': {'fid': fid, 'fips': fips, 'st': st, 'name': name},
        'geometry': geometry,
    }
    return fid, normalized


def _normalize_features(features, layer, *, state_identities=STATE_IDENTITIES,
                        expected_count=None, expected_ids_sha256=None):
    if layer not in SNAPSHOT_FILES or not isinstance(features, list):
        raise PublicationError('unknown admin snapshot layer/features')
    expected_count = (EXPECTED_COUNTS[layer] if expected_count is None
                      else expected_count)
    expected_ids_sha256 = (EXPECTED_FIPS_IDS_SHA256[layer]
                           if expected_ids_sha256 is None
                           else expected_ids_sha256)
    rows = {}
    for index, feature in enumerate(features):
        fid, normalized = _normalize_feature(
            feature, layer, index, state_identities=state_identities)
        if fid in rows:
            raise PublicationError(f'{layer} duplicates FIPS {fid:05d}')
        rows[fid] = normalized
    ids = sorted(rows)
    if len(ids) != expected_count:
        raise PublicationError(
            f'{layer} has {len(ids)} unique FIPS IDs, expected {expected_count}')
    actual_hash = _ids_sha256(ids)
    if actual_hash != expected_ids_sha256:
        raise PublicationError(
            f'{layer} FIPS inventory hash changed: {actual_hash}')
    return [rows[fid] for fid in ids]


def _normalized_properties_sha256(rows, layer):
    return _properties_sha256({
        row['id']: tuple(row['properties'][field]
                         for field in REQUIRED_PROPERTIES[layer])
        for row in rows})


def _validate_inventory_entry(entry, layer, path):
    if not isinstance(entry, dict) or set(entry) != {
            'file', 'bytes', 'sha256', 'count', 'fips_ids_sha256',
            'properties_sha256'}:
        raise PublicationError(f'inventory.layers.{layer} schema is invalid')
    if (entry.get('file') != SNAPSHOT_FILES[layer] or
            entry.get('count') != EXPECTED_COUNTS[layer] or
            entry.get('fips_ids_sha256') != EXPECTED_FIPS_IDS_SHA256[layer] or
            entry.get('properties_sha256') != EXPECTED_PROPERTIES_SHA256[layer] or
            not isinstance(entry.get('bytes'), int) or
            isinstance(entry.get('bytes'), bool) or entry['bytes'] <= 0 or
            not isinstance(entry.get('sha256'), str) or
            SHA256_RE.fullmatch(entry['sha256']) is None):
        raise PublicationError(f'inventory.layers.{layer} identity is invalid')
    actual_bytes = os.path.getsize(path)
    actual_sha256 = _sha256_file(path)
    if actual_bytes != entry['bytes'] or actual_sha256 != entry['sha256']:
        raise PublicationError(
            f'{layer} staging bytes/SHA-256 disagree with inventory')
    return {'bytes': actual_bytes, 'sha256': actual_sha256}


def load_staging(staging_dir, inventory_path):
    staging = os.path.realpath(staging_dir)
    if not os.path.isdir(staging) or not _outside(staging, SITE):
        raise PublicationError('staging directory must be private and outside site/')
    inventory_path = _private_file(inventory_path, 'admin staging inventory')
    if os.path.commonpath((inventory_path, staging)) != staging:
        raise PublicationError('admin inventory must be inside its staging directory')
    inventory, inventory_raw = _read_strict_json(
        inventory_path, 'admin staging inventory')
    if set(inventory) != {
            'schema_version', 'system', 'created', 'source', 'vintage', 'layers'}:
        raise PublicationError('admin inventory top-level schema is invalid')
    if (inventory.get('schema_version') != SCHEMA_VERSION or
            inventory.get('system') != SYSTEM or
            inventory.get('source') != SERVICE or
            inventory.get('vintage') != VINTAGE):
        raise PublicationError('admin inventory source identity is invalid')
    _date(inventory.get('created'), 'inventory.created')
    layers = inventory.get('layers')
    if not isinstance(layers, dict) or set(layers) != set(SNAPSHOT_FILES):
        raise PublicationError('admin inventory must contain states and counties')
    documents = {}
    source_snapshot = {}
    normalized = {}
    integrity = {}
    paths = {}
    retrieved = set()
    for layer in ('states', 'counties'):
        path = _private_file(
            os.path.join(staging, SNAPSHOT_FILES[layer]), f'{layer} snapshot')
        if os.path.dirname(path) != staging:
            raise PublicationError(f'{layer} snapshot path escapes staging directory')
        integrity[layer] = _validate_inventory_entry(layers[layer], layer, path)
        document, _ = _read_strict_json(path, f'{layer} snapshot')
        features = _validate_snapshot_header(document, layer)
        normalized[layer] = _normalize_features(features, layer)
        properties_sha256 = _normalized_properties_sha256(
            normalized[layer], layer)
        if properties_sha256 != EXPECTED_PROPERTIES_SHA256[layer]:
            raise PublicationError(
                f'{layer} exact FIPS/name/state property inventory changed: '
                f'{properties_sha256}')
        documents[layer] = document
        source_snapshot[layer] = dict(document['pagination'])
        paths[layer] = path
        retrieved.add(document['retrieved'])
    if len(retrieved) != 1:
        raise PublicationError('states and counties must be one retrieval generation')
    state_geometries = {
        row['properties']['st']: row['geometry'] for row in normalized['states']}
    clip_document, clip_bytes = _state_clip_bytes(state_geometries)
    return {
        'staging': staging,
        'inventory_path': inventory_path,
        'inventory_sha256': _sha256_bytes(inventory_raw),
        'inventory_bytes': len(inventory_raw),
        'paths': paths,
        'integrity': integrity,
        'source_snapshot': source_snapshot,
        'normalized': normalized,
        'clip_document': clip_document,
        'clip_bytes': clip_bytes,
        'retrieved': retrieved.pop(),
    }


def _recheck_staging(context):
    if (_sha256_file(context['inventory_path']) != context['inventory_sha256'] or
            os.path.getsize(context['inventory_path']) != context['inventory_bytes']):
        raise PublicationError('admin staging inventory changed during build')
    for layer, path in context['paths'].items():
        expected = context['integrity'][layer]
        if (_sha256_file(path) != expected['sha256'] or
                os.path.getsize(path) != expected['bytes']):
            raise PublicationError(f'{layer} staging snapshot changed during build')


def _sequence_bytes(features):
    return b''.join(_canonical_bytes(feature) + b'\n' for feature in features)


def _description(context):
    return _canonical_bytes({
        'schema': 'nwmm-national-admin-pmtiles-v1',
        'vintage': VINTAGE,
        'counts': EXPECTED_COUNTS,
        'fips_ids_sha256': EXPECTED_FIPS_IDS_SHA256,
        'inventory_sha256': context['inventory_sha256'],
    }).decode('utf-8')


def _tippecanoe_command(description):
    command = [
        'tippecanoe', '--force', '--output=admin.pmtiles',
        f'--minimum-zoom={MINZOOM}', f'--maximum-zoom={MAXZOOM}',
        f'--base-zoom={MAXZOOM}', '--no-feature-limit', '--no-tile-size-limit',
        '--detect-shared-borders', '--simplification=10',
        '--simplify-only-low-zooms',
        '--no-tiny-polygon-reduction-at-maximum-zoom',
        '--use-attribute-for-id=fid', '--exclude=fid',
        '--preserve-input-order', '--quiet', '--name=admin.pmtiles',
        f'--description={description}', f'--attribution={ATTRIBUTION}',
        '-L', 'states:states.geojsonseq',
        '-L', 'counties:counties.geojsonseq',
    ]
    if any('/' in argument or '\\' in argument for argument in command):
        raise PublicationError('Tippecanoe command is not path-free')
    return command


def _tippecanoe_version():
    try:
        result = subprocess.run(
            ['tippecanoe', '--version'], check=True, capture_output=True,
            text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicationError('tippecanoe >=2.79 is required') from exc
    match = TIPPECANOE_RE.search((result.stdout or '') + (result.stderr or ''))
    if match is None or tuple(int(value or 0) for value in match.groups()) < (2, 79, 0):
        raise PublicationError('tippecanoe >=2.79 is required')


def _write_bytes(path, raw):
    with open(path, 'wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def _run_tippecanoe(directory, sequences, description):
    if set(sequences) != {'states', 'counties'}:
        raise PublicationError('admin build needs exact states/counties sequences')
    for layer, raw in sequences.items():
        _write_bytes(os.path.join(directory, f'{layer}.geojsonseq'), raw)
    environment = os.environ.copy()
    environment.update({
        'TIPPECANOE_MAX_THREADS': '1', 'LC_ALL': 'C', 'LANG': 'C', 'TZ': 'UTC',
    })
    subprocess.run(
        _tippecanoe_command(description), cwd=directory, env=environment,
        check=True)
    output = os.path.join(directory, 'admin.pmtiles')
    if not os.path.isfile(output):
        raise PublicationError('tippecanoe did not create admin.pmtiles')
    return output


def _read_pmtiles_metadata(path):
    try:
        with open(path, 'rb') as archive:
            header = archive.read(127)
            if len(header) != 127 or header[:8] != b'PMTiles\x03':
                raise PublicationError('admin artifact is not PMTiles v3')
            metadata_offset, metadata_length = struct.unpack_from('<2Q', header, 24)
            archive.seek(metadata_offset)
            payload = archive.read(metadata_length)
        payload = _decompress_pmtiles(payload, header[97])
        return _strict_json_bytes(payload, 'admin PMTiles metadata')
    except OSError as exc:
        raise PublicationError(f'cannot read admin PMTiles metadata: {exc}') from exc


def _path_free_metadata(path, expected_description):
    metadata = _read_pmtiles_metadata(path)
    generator = metadata.get('generator')
    options = metadata.get('generator_options')
    if (metadata.get('name') != 'admin.pmtiles' or
            metadata.get('description') != expected_description or
            metadata.get('attribution') != ATTRIBUTION or
            not isinstance(generator, str) or TIPPECANOE_RE.fullmatch(generator) is None or
            not isinstance(options, str) or '/' in options or '\\' in options):
        raise PublicationError('admin PMTiles metadata is not path-free and pinned')
    serialized = _canonical_bytes(metadata).decode('utf-8')
    if any(marker in serialized for marker in (
            context_marker for context_marker in
            (os.path.realpath(os.path.dirname(path)), 'nwmm-admin-')
            if context_marker)):
        raise PublicationError('admin PMTiles metadata leaks a build path')
    return {
        'status': 'complete_path_free_reproducible_metadata',
        'name': 'admin.pmtiles',
        'metadata_sha256': _sha256_bytes(_canonical_bytes(metadata)),
        'generator_options_sha256': _sha256_bytes(_canonical_bytes(options)),
    }


def _expected_semantics(context):
    return {
        layer: {
            row['id']: tuple(row['properties'][field]
                            for field in REQUIRED_PROPERTIES[layer])
            for row in context['normalized'][layer]
        }
        for layer in ('states', 'counties')
    }


def _admin_feature_validator(expected):
    seen = {'states': {}, 'counties': {}}
    maxzoom_ids = {'states': set(), 'counties': set()}

    def validate(layer, feature, at_maxzoom):
        if layer not in REQUIRED_PROPERTIES:
            raise PublicationError(f'unexpected admin source layer {layer!r}')
        properties = feature.get('properties')
        required = set(REQUIRED_PROPERTIES[layer])
        if not isinstance(properties, dict) or set(properties) != required:
            raise PublicationError(
                f'admin {layer} feature properties must be exactly {sorted(required)}')
        feature_id = feature.get('id')
        if (not isinstance(feature_id, int) or isinstance(feature_id, bool) or
                feature_id < 0):
            raise PublicationError(f'admin {layer} feature has invalid top-level ID')
        fips, name, st = (properties[field] for field in REQUIRED_PROPERTIES[layer])
        pattern = r'\d{2}' if layer == 'states' else r'\d{5}'
        if (not isinstance(fips, str) or re.fullmatch(pattern, fips) is None or
                int(fips) != feature_id or not isinstance(name, str) or
                not name.strip() or not isinstance(st, str) or
                st not in ALL_STATES):
            raise PublicationError(f'admin {layer} feature semantics are invalid')
        signature = (fips, name, st)
        expected_signature = expected[layer].get(feature_id)
        if expected_signature is None or signature != expected_signature:
            raise PublicationError(
                f'admin {layer} FIPS {fips} differs from private staging')
        previous = seen[layer].setdefault(feature_id, signature)
        if previous != signature:
            raise PublicationError(
                f'admin {layer} FIPS {fips} changes across tiles')
        if at_maxzoom:
            maxzoom_ids[layer].add(feature_id)

    return validate, {'seen': seen, 'maxzoom_ids': maxzoom_ids}


def _validate_pmtiles(path, context, expected_description, *,
                      pmtiles_header=_strict_pmtiles_header):
    expected = _expected_semantics(context)
    feature_validator, evidence = _admin_feature_validator(expected)
    try:
        metadata = pmtiles_header(
            path, ['states', 'counties'], REQUIRED_PROPERTIES,
            verify_feature_properties=True, collect_feature_ids=True,
            expected_geometry_types={'states': {3}, 'counties': {3}},
            feature_validator=feature_validator)
    except (OSError, ValueError) as exc:
        raise PublicationError(f'admin PMTiles semantic scan failed: {exc}') from exc
    if (metadata.get('source_layers') != ['counties', 'states'] or
            metadata.get('minzoom') != MINZOOM or
            metadata.get('maxzoom') != MAXZOOM or
            metadata.get('bounds') != EXPECTED_BOUNDS or
            metadata.get('field_types') != {
                layer: {field: 'String' for field in REQUIRED_PROPERTIES[layer]}
                for layer in ('counties', 'states')}):
        raise PublicationError('admin PMTiles header/layer contract changed')
    inventory = {}
    for layer in ('states', 'counties'):
        expected_ids = sorted(expected[layer])
        observed_ids = metadata.get('maxzoom_feature_ids', {}).get(layer, [])
        instances = metadata.get('maxzoom_feature_instances', {}).get(layer, 0)
        if (observed_ids != expected_ids or
                evidence['maxzoom_ids'][layer] != set(expected_ids) or
                evidence['seen'][layer] != expected[layer] or
                not isinstance(instances, int) or instances < len(expected_ids)):
            raise PublicationError(
                f'admin {layer} maximum-zoom FIPS inventory does not reconcile')
        identity_hash = _ids_sha256(observed_ids)
        if identity_hash != EXPECTED_FIPS_IDS_SHA256[layer]:
            raise PublicationError(f'admin {layer} FIPS inventory hash changed')
        inventory[layer] = {
            'status': 'complete_at_retrieval',
            'source_records': EXPECTED_COUNTS[layer],
            'maxzoom_unique_tiled_ids': len(observed_ids),
            'maxzoom_feature_instances': instances,
            'ids_sha256': identity_hash,
            'properties_sha256': _properties_sha256(expected[layer]),
        }
    return metadata, inventory, _path_free_metadata(path, expected_description)


def _assert_identical_builds(first, second):
    first_size, second_size = os.path.getsize(first), os.path.getsize(second)
    first_sha, second_sha = _sha256_file(first), _sha256_file(second)
    if first_size != second_size or first_sha != second_sha:
        raise PublicationError(
            'admin PMTiles builds are not byte-identical: '
            f'first={first_size}/{first_sha}, second={second_size}/{second_sha}')
    return {'bytes': first_size, 'sha256': first_sha}


def _descriptor(context, artifact, scan, metadata_evidence):
    clip_bytes = context['clip_bytes']
    return {
        'schema_version': 1,
        'format': 'pmtiles',
        'file': 'data/tiles/context/admin.pmtiles',
        'source': {
            'authority': AUTHORITY,
            'service': SERVICE,
            'vintage': VINTAGE,
            'layers': {'states': 0, 'counties': 1},
        },
        'source_snapshot': {
            'contract': SNAPSHOT_CONTRACT,
            'inventory_sha256': context['inventory_sha256'],
            'layers': {
                layer: {
                    **dict(context['integrity'][layer]),
                    **{
                        field: context['source_snapshot'][layer][field]
                        for field in (
                            'source_snapshot_id', 'metadata_sha256',
                            'object_ids_sha256', 'records_sha256')
                    },
                }
                for layer in ('states', 'counties')},
        },
        'retrieved': context['retrieved'],
        'source_layers': ['states', 'counties'],
        'required_properties': {
            layer: list(REQUIRED_PROPERTIES[layer])
            for layer in ('states', 'counties')},
        'counts': dict(EXPECTED_COUNTS),
        'fips_id_inventories': scan,
        'state_clips': {
            'file': 'infra/state_clips.json',
            'bytes': len(clip_bytes),
            'sha256': _sha256_bytes(clip_bytes),
        },
        'bytes': artifact['bytes'],
        'sha256': artifact['sha256'],
        'minzoom': MINZOOM,
        'maxzoom': MAXZOOM,
        'bounds': list(EXPECTED_BOUNDS),
        'deterministic_rebuild': {
            'status': 'two_byte_identical_builds',
            'bytes': artifact['bytes'],
            'sha256': artifact['sha256'],
        },
        'reproducible_metadata': metadata_evidence,
    }


def _atomic_pending_copy(source, destination, prefix):
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    handle, pending = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=directory)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as output, open(source, 'rb') as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        return pending
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _atomic_pending_bytes(raw, destination, prefix):
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    handle, pending = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=directory)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        return pending
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _reserve_backup(path, label):
    handle, backup = tempfile.mkstemp(
        prefix=f'.{label}-rollback-', dir=os.path.dirname(path))
    os.close(handle)
    os.unlink(backup)
    return backup


def _merge_admin_manifest(descriptor, manifest_path=MANIFEST):
    manifest, original = _read_strict_json(manifest_path, 'latest manifest')
    if original != _manifest_bytes(manifest):
        raise PublicationError(
            'latest manifest is not reconcile-canonical; run '
            'pipelines/reconcile_manifest.py before admin publication')
    baselines = manifest.get('national_baselines')
    if not isinstance(baselines, dict):
        raise PublicationError('latest manifest national_baselines must be an object')
    sources = manifest.get('sources')
    if not isinstance(sources, dict):
        raise PublicationError('latest manifest sources must be an object')
    baselines['admin'] = descriptor
    sources['boundaries'] = BOUNDARIES_SOURCE
    raw = _manifest_bytes(manifest)
    mode = stat.S_IMODE(os.stat(manifest_path).st_mode)
    handle, pending = tempfile.mkstemp(
        prefix='.manifest-admin-', suffix='.tmp',
        dir=os.path.dirname(manifest_path))
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, manifest_path)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def _publish_bundle(admin_pending, clip_pending, descriptor, *,
                    admin_out=ADMIN_OUT, clips_out=STATE_CLIPS_OUT,
                    manifest_path=MANIFEST, manifest_merge=_merge_admin_manifest):
    """Install artifact, clips, and latest manifest with BaseException rollback."""
    prepared = [(admin_pending, admin_out), (clip_pending, clips_out)]
    os.makedirs(os.path.dirname(admin_out), exist_ok=True)
    os.makedirs(os.path.dirname(clips_out), exist_ok=True)
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    lock_path = (PUBLICATION_LOCK if manifest_path == MANIFEST else
                 os.path.join(os.path.dirname(manifest_path), '.admin.lock'))
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, 'a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        manifest_backup = None
        manifest_backup_ready = False
        manifest_mutation_started = False
        backups = []
        try:
            manifest_backup = _reserve_backup(manifest_path, 'admin-manifest')
            shutil.copy2(manifest_path, manifest_backup)
            manifest_backup_ready = True
            for index, (pending, destination) in enumerate(prepared):
                backup = None
                state = {
                    'destination': destination, 'backup': None,
                    'old_moved': False, 'new_installed': False,
                }
                backups.append(state)
                if os.path.exists(destination):
                    backup = _reserve_backup(destination, f'admin-{index}')
                    state['backup'] = backup
                    # Record intent before the syscall. If an asynchronous
                    # BaseException arrives after replace(2) but before Python
                    # resumes, rollback still restores the moved old file.
                    state['old_moved'] = True
                    os.replace(destination, backup)
                state['new_installed'] = True
                os.replace(pending, destination)
            manifest_mutation_started = True
            manifest_merge(descriptor, manifest_path)
            if (_sha256_file(admin_out) != descriptor['sha256'] or
                    os.path.getsize(admin_out) != descriptor['bytes'] or
                    _sha256_file(clips_out) !=
                    descriptor['state_clips']['sha256'] or
                    os.path.getsize(clips_out) !=
                    descriptor['state_clips']['bytes']):
                raise PublicationError('admin publication postcondition failed')
            merged, _ = _read_strict_json(manifest_path, 'published manifest')
            if ((merged.get('national_baselines') or {}).get('admin') != descriptor or
                    (merged.get('sources') or {}).get('boundaries') !=
                    BOUNDARIES_SOURCE):
                raise PublicationError(
                    'latest manifest lost the admin descriptor/source truth')
        except BaseException:
            rollback_errors = []
            for state in reversed(backups):
                destination = state['destination']
                backup = state['backup']
                try:
                    if state['old_moved'] and backup is not None:
                        if os.path.exists(backup):
                            os.replace(backup, destination)
                    elif state['new_installed']:
                        if os.path.exists(destination):
                            os.unlink(destination)
                except BaseException as exc:  # pragma: no cover - catastrophic FS failure
                    rollback_errors.append(f'{destination}: {exc}')
            try:
                if (manifest_mutation_started and manifest_backup_ready and
                        manifest_backup is not None and
                        os.path.exists(manifest_backup)):
                    os.replace(manifest_backup, manifest_path)
            except BaseException as exc:  # pragma: no cover - catastrophic FS failure
                rollback_errors.append(f'{manifest_path}: {exc}')
            if rollback_errors:
                raise RuntimeError(
                    'admin publication rollback was incomplete: ' +
                    '; '.join(rollback_errors))
            raise
        else:
            for state in backups:
                backup = state['backup']
                if backup is not None and os.path.exists(backup):
                    os.unlink(backup)
            if manifest_backup is not None and os.path.exists(manifest_backup):
                os.unlink(manifest_backup)
        finally:
            for state in backups:
                backup = state['backup']
                if backup is not None:
                    try:
                        os.unlink(backup)
                    except FileNotFoundError:
                        pass
            if manifest_backup is not None:
                try:
                    os.unlink(manifest_backup)
                except FileNotFoundError:
                    pass
            for pending, _ in prepared:
                try:
                    os.unlink(pending)
                except FileNotFoundError:
                    pass


def _publish_after_grace(admin_pending, clip_pending, descriptor, *,
                         grace_seconds=PUBLICATION_GRACE_SECONDS,
                         publish_bundle=_publish_bundle):
    try:
        print(
            'Admin private QA is complete; atomic artifact + state clips + '
            f'latest-manifest publication begins in {grace_seconds} seconds',
            flush=True)
        time.sleep(grace_seconds)
        publish_bundle(admin_pending, clip_pending, descriptor)
    except BaseException:
        for pending in (admin_pending, clip_pending):
            try:
                os.unlink(pending)
            except FileNotFoundError:
                pass
        raise


def _install_private(source, raw, descriptor, output_directory):
    destinations = {
        'artifact': os.path.join(output_directory, 'admin.pmtiles'),
        'state_clips': os.path.join(output_directory, 'state_clips.json'),
        'descriptor': os.path.join(output_directory, 'admin-descriptor.json'),
    }
    pending = []
    try:
        pending.append((_atomic_pending_copy(
            source, destinations['artifact'], '.admin-private-'),
                        destinations['artifact']))
        pending.append((_atomic_pending_bytes(
            raw, destinations['state_clips'], '.clips-private-'),
                        destinations['state_clips']))
        pending.append((_atomic_pending_bytes(
            _canonical_bytes(descriptor), destinations['descriptor'],
            '.descriptor-private-'), destinations['descriptor']))
        for source_path, destination in pending:
            os.replace(source_path, destination)
    finally:
        for source_path, _ in pending:
            try:
                os.unlink(source_path)
            except FileNotFoundError:
                pass
    return destinations


def build(staging_dir, inventory_path, *, publish=False,
          private_output_dir=None, grace_seconds=PUBLICATION_GRACE_SECONDS,
          pmtiles_header=_strict_pmtiles_header):
    if publish == (private_output_dir is not None):
        raise PublicationError(
            'choose exactly one of public publication or a private output directory')
    output_directory = None
    if not publish:
        output_directory = _private_output(private_output_dir)
    _tippecanoe_version()
    context = load_staging(staging_dir, inventory_path)
    description = _description(context)
    sequences = {
        layer: _sequence_bytes(context['normalized'][layer])
        for layer in ('states', 'counties')}
    admin_pending = None
    clips_pending = None
    with tempfile.TemporaryDirectory(
            prefix='nwmm-admin-build-first-') as first_directory, \
            tempfile.TemporaryDirectory(
                prefix='nwmm-admin-build-second-') as second_directory:
        first = _run_tippecanoe(first_directory, sequences, description)
        second = _run_tippecanoe(second_directory, sequences, description)
        artifact = _assert_identical_builds(first, second)
        first_metadata, first_scan, first_reproducible = _validate_pmtiles(
            first, context, description, pmtiles_header=pmtiles_header)
        second_metadata, second_scan, second_reproducible = _validate_pmtiles(
            second, context, description, pmtiles_header=pmtiles_header)
        if (first_metadata != second_metadata or first_scan != second_scan or
                first_reproducible != second_reproducible):
            raise PublicationError('admin double-build semantic evidence changed')
        descriptor = _descriptor(
            context, artifact, first_scan, first_reproducible)
        _recheck_staging(context)
        if publish:
            try:
                admin_pending = _atomic_pending_copy(
                    first, ADMIN_OUT, '.admin-publish-')
                clips_pending = _atomic_pending_bytes(
                    context['clip_bytes'], STATE_CLIPS_OUT, '.clips-publish-')
            except BaseException:
                for pending in (admin_pending, clips_pending):
                    if pending is not None:
                        try:
                            os.unlink(pending)
                        except FileNotFoundError:
                            pass
                raise
        else:
            destinations = _install_private(
                first, context['clip_bytes'], descriptor, output_directory)
    if publish:
        try:
            _recheck_staging(context)
            _publish_after_grace(
                admin_pending, clips_pending, descriptor,
                grace_seconds=grace_seconds)
        except BaseException:
            for pending in (admin_pending, clips_pending):
                try:
                    os.unlink(pending)
                except FileNotFoundError:
                    pass
            raise
        destinations = {
            'artifact': ADMIN_OUT,
            'state_clips': STATE_CLIPS_OUT,
            'manifest': MANIFEST,
        }
    result = {
        'mode': 'public' if publish else 'private_validation',
        'descriptor': descriptor,
        'destinations': destinations,
        'current_accepted_before_next_publication': {
            'artifact': CURRENT_ACCEPTED_ARTIFACT,
            'state_clips': CURRENT_ACCEPTED_STATE_CLIPS,
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--staging-dir',
                        help='private directory containing frozen snapshots')
    source.add_argument('--capture-staging',
                        help='capture official TIGERweb into this new private directory')
    parser.add_argument('--inventory',
                        help='private inventory JSON; defaults inside staging-dir')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--private-output-dir',
                      help='validate/build outside site without publishing')
    mode.add_argument('--publish', action='store_true',
                      help='after a 30-second grace, publish all three identities')
    args = parser.parse_args(argv)
    if args.capture_staging and args.publish:
        parser.error('--capture-staging is private-only and cannot use --publish')
    if args.capture_staging and args.inventory:
        parser.error('--capture-staging creates its own exact inventory')
    try:
        staging = args.staging_dir
        if args.capture_staging:
            captured = capture_staging(args.capture_staging)
            staging = captured['staging_dir']
        inventory = args.inventory or os.path.join(staging, 'inventory.json')
        build(staging, inventory, publish=args.publish,
              private_output_dir=args.private_output_dir)
    except (PublicationError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
