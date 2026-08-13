#!/usr/bin/env python3
"""Lossless, mutation-detecting ArcGIS feature-layer snapshots.

This module deliberately does not use offset/cursor pagination or the shared
HTTP cache.  A run pins the service's complete ``returnIdsOnly`` inventory,
fetches exact pages by those object IDs, repeats every feature page, and then
rechecks both layer metadata and the complete ID inventory.  The caller may
publish the resulting JSON only after all four observations agree.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = 'nw-mineral-monitor/1.0 (research pipeline; contact: repo owner)'
TRANSIENT_HTTP = frozenset((429, 500, 502, 503, 504))


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()


def _request_json(url, params=None, *, post=False, tries=6):
    params = params or {}
    encoded = urllib.parse.urlencode(params).encode('utf-8')
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if post:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(url, data=encoded, headers=headers)
    else:
        suffix = ('?' + encoded.decode('ascii')) if encoded else ''
        request = urllib.request.Request(url + suffix, headers=headers)
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise RuntimeError(f'ArcGIS returned non-object JSON from {url}')
            error = value.get('error')
            if not error:
                return value
            last = RuntimeError(f'ArcGIS error from {url}: {error}')
            code = error.get('code') if isinstance(error, dict) else None
            if code not in TRANSIENT_HTTP:
                raise last
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in TRANSIENT_HTTP:
                raise RuntimeError(
                    f'ArcGIS request failed: HTTP {exc.code} ({url})') from exc
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'ArcGIS request failed after {tries} attempts: {last}')


def _positive_oid(value, label):
    if isinstance(value, bool):
        raise RuntimeError(f'{label} object ID must be a positive integer')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f'{label} object ID must be a positive integer') from exc
    if result <= 0 or str(value).strip() not in (str(result), f'{result}.0'):
        raise RuntimeError(f'{label} object ID must be a positive integer')
    return result


def _typed_oid_field(metadata, layer_url):
    typed = [
        field.get('name') for field in metadata.get('fields') or []
        if isinstance(field, dict) and
        field.get('type') == 'esriFieldTypeOID' and
        isinstance(field.get('name'), str) and field.get('name')
    ]
    if len(typed) != 1:
        raise RuntimeError(
            f'{layer_url} must advertise exactly one typed object-ID field')
    oid = typed[0]
    advertised = [metadata.get('objectIdField'),
                  metadata.get('objectIdFieldName')]
    for candidate in advertised:
        if (candidate is not None and str(candidate).strip() and
                str(candidate).casefold() != oid.casefold()):
            raise RuntimeError(
                f'{layer_url} object-ID metadata disagrees with typed {oid}')
    return oid


def _selected_metadata(metadata, oid_field):
    spatial = metadata.get('sourceSpatialReference') or {}
    return {
        'id': metadata.get('id'),
        'name': metadata.get('name'),
        'type': metadata.get('type'),
        'geometryType': metadata.get('geometryType'),
        'capabilities': metadata.get('capabilities'),
        'currentVersion': metadata.get('currentVersion'),
        'maxRecordCount': metadata.get('maxRecordCount'),
        'serviceItemId': metadata.get('serviceItemId'),
        'objectIdField': oid_field,
        'sourceSpatialReference': {
            key: spatial.get(key) for key in ('wkid', 'latestWkid')
            if spatial.get(key) is not None
        },
        'fields': [
            {'name': field.get('name'), 'type': field.get('type')}
            for field in metadata.get('fields') or []
            if isinstance(field, dict)
        ],
    }


def _inspect_layer(layer_url, *, expected_name, expected_geometry,
                   required_fields):
    metadata = _request_json(layer_url, {'f': 'json'})
    if (metadata.get('name') != expected_name or
            metadata.get('type') != 'Feature Layer' or
            metadata.get('geometryType') != expected_geometry):
        raise RuntimeError(
            f'{layer_url} layer identity/geometry contract changed')
    capabilities = {
        value.strip().casefold()
        for value in str(metadata.get('capabilities') or '').split(',')
    }
    if 'query' not in capabilities:
        raise RuntimeError(f'{layer_url} no longer advertises query capability')
    oid_field = _typed_oid_field(metadata, layer_url)
    schema = {
        field.get('name'): field.get('type')
        for field in metadata.get('fields') or []
        if isinstance(field, dict) and isinstance(field.get('name'), str)
    }
    expected_schema = dict(required_fields)
    expected_schema.setdefault(oid_field, 'esriFieldTypeOID')
    mismatches = {
        field: (expected_type, schema.get(field))
        for field, expected_type in expected_schema.items()
        if schema.get(field) != expected_type
    }
    if mismatches:
        raise RuntimeError(
            f'{layer_url} required field schema changed: {mismatches}')
    selected = _selected_metadata(metadata, oid_field)

    ids_result = _request_json(f'{layer_url}/query', {
        'where': '1=1', 'returnIdsOnly': 'true', 'f': 'json',
    })
    advertised_oid = ids_result.get('objectIdFieldName')
    if (not isinstance(advertised_oid, str) or
            advertised_oid.casefold() != oid_field.casefold()):
        raise RuntimeError(
            f'{layer_url} returnIdsOnly object-ID field changed')
    raw_ids = ids_result.get('objectIds')
    if not isinstance(raw_ids, list) or not raw_ids:
        raise RuntimeError(
            f'{layer_url} returned an empty/invalid object-ID snapshot')
    ids = sorted(_positive_oid(value, layer_url) for value in raw_ids)
    if len(ids) != len(set(ids)):
        raise RuntimeError(
            f'{layer_url} object-ID snapshot contains duplicates')
    return {
        'oid_field': oid_field,
        'ids': ids,
        'object_ids_sha256': canonical_sha256(ids),
        'metadata': selected,
        'metadata_sha256': canonical_sha256(selected),
    }


def _fetch_pages(layer_url, snapshot, out_fields, *, page,
                 geometry_precision, expected_hashes=None, progress=None):
    oid_field = snapshot['oid_field']
    ids = snapshot['ids']
    fields = list(dict.fromkeys((oid_field, *out_fields)))
    rows = [] if expected_hashes is None else None
    hashes = []
    emitted = 0
    for start in range(0, len(ids), page):
        expected = ids[start:start + page]
        result = _request_json(f'{layer_url}/query', {
            'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(fields),
            'returnGeometry': 'true',
            'returnTrueCurves': 'false',
            'outSR': 4326,
            'geometryPrecision': geometry_precision,
            'orderByFields': f'{oid_field} ASC',
            'f': 'json',
        }, post=True)
        if result.get('exceededTransferLimit') is True:
            raise RuntimeError(
                f'{layer_url} exceeded its transfer limit for an exact ID page')
        features = result.get('features')
        if not isinstance(features, list):
            raise RuntimeError(f'{layer_url} snapshot page has no feature array')
        actual = []
        page_hashes = []
        for feature in features:
            if not isinstance(feature, dict) or not isinstance(
                    feature.get('attributes'), dict):
                raise RuntimeError(f'{layer_url} snapshot page has an invalid feature')
            actual.append(_positive_oid(
                feature['attributes'].get(oid_field), layer_url))
            page_hashes.append(canonical_sha256(feature))
        if actual != expected:
            raise RuntimeError(
                f'{layer_url} object-ID snapshot/page mismatch at {start}; '
                f'expected={expected[:3]}..{expected[-3:]}, '
                f'observed={actual[:3]}..{actual[-3:]}')
        if expected_hashes is not None:
            expected_page_hashes = expected_hashes[start:start + len(expected)]
            if page_hashes != expected_page_hashes:
                changed = [
                    expected[index] for index, (before, after) in enumerate(
                        zip(expected_page_hashes, page_hashes)) if before != after
                ]
                raise RuntimeError(
                    f'{layer_url} feature content mutated during retrieval; '
                    f'object IDs={changed[:10]}')
        else:
            rows.extend(features)
            hashes.extend(page_hashes)
        emitted += len(features)
        if progress is not None:
            progress(emitted, len(ids))
    if emitted != len(ids):
        raise RuntimeError(
            f'{layer_url} emitted {emitted}; pinned snapshot has {len(ids)}')
    return rows, hashes


def capture_layer(layer_url, *, expected_name, expected_geometry,
                  required_fields, out_fields, page=500,
                  geometry_precision=8, progress=None,
                  verify_progress=None):
    """Return exact source features and a reviewable mutation-check record."""
    before = _inspect_layer(
        layer_url, expected_name=expected_name,
        expected_geometry=expected_geometry,
        required_fields=required_fields)
    rows, row_hashes = _fetch_pages(
        layer_url, before, out_fields, page=page,
        geometry_precision=geometry_precision, progress=progress)
    _fetch_pages(
        layer_url, before, out_fields, page=page,
        geometry_precision=geometry_precision,
        expected_hashes=row_hashes, progress=verify_progress)
    after = _inspect_layer(
        layer_url, expected_name=expected_name,
        expected_geometry=expected_geometry,
        required_fields=required_fields)
    if (before['metadata_sha256'] != after['metadata_sha256'] or
            before['oid_field'] != after['oid_field']):
        raise RuntimeError(f'{layer_url} metadata mutated during retrieval')
    if (before['ids'] != after['ids'] or
            before['object_ids_sha256'] != after['object_ids_sha256']):
        raise RuntimeError(
            f'{layer_url} object-ID inventory mutated during retrieval')
    return rows, {
        'layer_url': layer_url,
        'layer_name': expected_name,
        'geometry_type': expected_geometry,
        'object_id_field': before['oid_field'],
        'n': len(before['ids']),
        'minimum_object_id': before['ids'][0],
        'maximum_object_id': before['ids'][-1],
        'object_ids_sha256': before['object_ids_sha256'],
        'layer_metadata_sha256': before['metadata_sha256'],
        'records_sha256': canonical_sha256(rows),
        'verification': {
            'page_mode': 'exact_object_ids',
            'full_second_feature_pass': True,
            'postflight_metadata_match': True,
            'postflight_object_ids_match': True,
            'geometry_precision': geometry_precision,
        },
        'metadata': before['metadata'],
    }
