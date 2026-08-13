#!/usr/bin/env python3
"""Build the PLSS + active-claim inputs for open-ground analysis.

The official MLRS map layer has claim polygons but no legal-description
columns. This producer snapshots the official MLRS and CadNSDI object-ID sets,
clips PLSS sections to the authoritative Census state polygon, and maps a
claim to every section whose polygon interior overlaps it. It refuses to
publish unless the resulting unique claim serial set exactly matches an
uncapped, machine-attested active snapshot from ``lambda_updater.py``.

Raw/private outputs only; this module never writes below ``site/`` and never
classifies mineral disposition or open ground.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import build_federal_mlrs_pmtiles as mlrs
import build_national_open_ground_pmtiles as open_ground
from state_registry import CLAIM_STATES, load_states


CLAIMS_LAYER = open_ground.ACTIVE_CLAIMS_SOURCE
PLSS_LAYER = open_ground.PLSS_SOURCE
USER_AGENT = 'nw-mineral-monitor-open-ground-staging/1.0'
PAGE = 250


class StagingError(ValueError):
    pass


def _shapely():
    try:
        from shapely.geometry import GeometryCollection, MultiPolygon, mapping, shape
        from shapely.ops import unary_union
    except ImportError as exc:
        raise StagingError(
            'Shapely 2.x is required for the claim/PLSS overlay') from exc
    return GeometryCollection, MultiPolygon, mapping, shape, unary_union


def _strict_json(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
        value = mlrs._strict_json_bytes(raw, label)
    except (OSError, mlrs.PublicationError) as exc:
        raise StagingError(str(exc)) from exc
    return value, raw


def _request_json(url, params, *, post=False, tries=6):
    encoded = urllib.parse.urlencode(params).encode('ascii')
    request = urllib.request.Request(
        url if post else url + '?' + encoded.decode('ascii'),
        data=encoded if post else None,
        headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                value = json.load(response)
            error = value.get('error') if isinstance(value, dict) else None
            if not error:
                return value
            last = RuntimeError(f'ArcGIS error from {url}: {error}')
            if not isinstance(error, dict) or error.get('code') not in (
                    429, 500, 502, 503, 504):
                raise last
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise StagingError(f'ArcGIS HTTP {exc.code}: {url}') from exc
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise StagingError(f'ArcGIS request failed after {tries} attempts: {last}')


def _envelope_param(bbox):
    return json.dumps({
        'xmin': bbox[0], 'ymin': bbox[1], 'xmax': bbox[2], 'ymax': bbox[3],
        'spatialReference': {'wkid': 4326},
    }, separators=(',', ':'))


def _layer_contract(layer, expected_name):
    metadata = _request_json(layer, {'f': 'json'})
    raw_fields = metadata.get('fields')
    if not isinstance(raw_fields, list):
        raise StagingError(f'{layer} official layer has no field schema')
    fields = {item.get('name') for item in raw_fields
              if isinstance(item, dict)}
    oid_fields = sorted(
        item.get('name') for item in raw_fields
        if isinstance(item, dict) and
        item.get('type') == 'esriFieldTypeOID' and
        isinstance(item.get('name'), str))
    # These BLM MapServer layer documents currently omit ArcGIS's optional
    # top-level ``objectIdField`` key.  The typed field schema is the
    # authoritative identity.  If a service does advertise either optional
    # alias, it still has to agree with that schema.
    advertised_oid = metadata.get('objectIdField')
    advertised_oid_name = metadata.get('objectIdFieldName')
    if (metadata.get('name') != expected_name or
            metadata.get('geometryType') != 'esriGeometryPolygon' or
            oid_fields != ['OBJECTID'] or
            advertised_oid not in (None, 'OBJECTID') or
            advertised_oid_name not in (None, 'OBJECTID') or
            'OBJECTID' not in fields):
        raise StagingError(f'{layer} official layer identity/schema changed')
    selected = {
        'name': metadata['name'], 'geometryType': metadata['geometryType'],
        'objectIdField': 'OBJECTID',
        'maxRecordCount': metadata.get('maxRecordCount'),
        'fields': sorted(fields),
    }
    return hashlib.sha256(json.dumps(
        selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _snapshot_ids(layer, where, envelopes):
    ids = set()
    for bbox in envelopes:
        result = _request_json(f'{layer}/query', {
            'where': where, 'geometryType': 'esriGeometryEnvelope',
            'geometry': _envelope_param(bbox), 'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'returnIdsOnly': 'true', 'f': 'json',
        })
        if result.get('objectIdFieldName') != 'OBJECTID':
            raise StagingError(f'{layer} object-ID field changed')
        if 'objectIds' not in result:
            raise StagingError(f'{layer} returned no object-ID array')
        # ArcGIS returns JSON null, not [], for a valid empty ID result. That
        # is a truthful zero (important for thin claim states), whereas an
        # absent key is a malformed/incomplete response.
        values = result['objectIds']
        if values is None:
            values = []
        if not isinstance(values, list):
            raise StagingError(f'{layer} returned an invalid object-ID array')
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise StagingError(f'{layer} returned an invalid object ID')
            ids.add(value)
    return sorted(ids)


def _iter_features(layer, ids, fields):
    emitted = set()
    for start in range(0, len(ids), PAGE):
        expected = ids[start:start + PAGE]
        data = _request_json(f'{layer}/query', {
            'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(('OBJECTID', *fields)),
            'returnGeometry': 'true', 'returnTrueCurves': 'false',
            'outSR': 4326, 'geometryPrecision': 7,
            'orderByFields': 'OBJECTID ASC', 'f': 'geojson',
        }, post=True)
        features = data.get('features')
        if not isinstance(features, list):
            raise StagingError(f'{layer} page has no GeoJSON feature array')
        actual = []
        by_id = {}
        for feature in features:
            properties = feature.get('properties') if isinstance(feature, dict) else None
            oid = properties.get('OBJECTID') if isinstance(properties, dict) else None
            if isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0:
                raise StagingError(f'{layer} page has an invalid OBJECTID')
            actual.append(oid)
            if oid in by_id:
                raise StagingError(f'{layer} page duplicates OBJECTID {oid}')
            by_id[oid] = feature
        if sorted(actual) != expected:
            raise StagingError(
                f'{layer} object-ID page drift at offset {start}')
        for oid in expected:
            emitted.add(oid)
            yield by_id[oid]
    if emitted != set(ids):
        raise StagingError(f'{layer} did not emit its exact object-ID snapshot')


def _round_geometry(value):
    if isinstance(value, (list, tuple)):
        return [_round_geometry(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StagingError('geometry contains a nonfinite coordinate')
        return round(value, 7)
    return value


def _polygon_only(geometry):
    GeometryCollection, MultiPolygon, _, _, unary_union = _shapely()
    if geometry.is_empty:
        return None
    if geometry.geom_type in ('Polygon', 'MultiPolygon'):
        return geometry
    if isinstance(geometry, GeometryCollection):
        polygons = [part for part in geometry.geoms
                    if part.geom_type in ('Polygon', 'MultiPolygon') and
                    not part.is_empty]
        return unary_union(polygons) if polygons else None
    if isinstance(geometry, MultiPolygon):
        return geometry
    return None


def _claim_centroid(geometry):
    coordinates = geometry.get('coordinates') if isinstance(geometry, dict) else None
    if geometry.get('type') == 'Polygon':
        ring = coordinates[0] if coordinates else None
    elif geometry.get('type') == 'MultiPolygon':
        ring = coordinates[0][0] if coordinates and coordinates[0] else None
    else:
        ring = None
    if not isinstance(ring, list) or len(ring) < 4:
        raise StagingError('claim geometry has no exterior ring')
    area = cx = cy = 0.0
    for left, right in zip(ring, ring[1:]):
        cross = left[0] * right[1] - right[0] * left[1]
        area += cross
        cx += (left[0] + right[0]) * cross
        cy += (left[1] + right[1]) * cross
    if abs(area) < 1e-12:
        raise StagingError('claim exterior has zero centroid area')
    return cx / (3 * area), cy / (3 * area)


def _active_snapshot(path, code, clip_sha, envelope_count):
    data, raw = _strict_json(path, f'{code} active MLRS snapshot')
    context = {
        'clip_sha256': clip_sha,
        'query_envelope_counts': {code: envelope_count},
    }
    if (not isinstance(data, dict) or data.get('state') != code or
            data.get('layer') != 'active' or not mlrs._is_int(data.get('n')) or
            not isinstance(data.get('serial'), list) or
            len(data['serial']) != data['n'] or
            len(set(data['serial'])) != data['n']):
        raise StagingError(f'{code} active snapshot identity/counts are invalid')
    try:
        if not mlrs._snapshot_provenance_complete(context, code, 'active', data):
            raise StagingError(f'{code} active snapshot pagination is incomplete')
    except mlrs.PublicationError as exc:
        raise StagingError(str(exc)) from exc
    return data, raw


def _bbox_cells(bounds):
    left, bottom, right, top = bounds
    for x in range(math.floor(left), math.floor(right) + 1):
        for y in range(math.floor(bottom), math.floor(top) + 1):
            yield x, y


def build_state(code, active_snapshot, plss_output, claims_output, *,
                state_clips=mlrs.DEFAULT_STATE_CLIPS):
    code = str(code).upper()
    if code not in CLAIM_STATES:
        raise StagingError(f'{code!r} is not a registry claim state')
    outputs = [os.path.realpath(plss_output), os.path.realpath(claims_output)]
    if len(set(outputs)) != 2 or any(not mlrs._outside(path, mlrs.SITE)
                                    for path in outputs):
        raise StagingError('distinct PLSS/claim outputs must remain outside site/')
    for path in outputs:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    clips, raw_clip = _strict_json(state_clips, 'authoritative state clips')
    if (not isinstance(clips, dict) or clips.get('schema_version') != 1 or
            not isinstance(clips.get('states'), dict) or
            code not in clips['states']):
        raise StagingError('authoritative state-clips schema is invalid')
    clip_sha = hashlib.sha256(raw_clip).hexdigest()
    registry = load_states()[code]
    envelopes = [item['bbox'] for item in registry['query_envelopes']]
    active, active_raw = _active_snapshot(
        active_snapshot, code, clip_sha, len(envelopes))
    _, _, mapping, shape, unary_union = _shapely()
    state_geometry = shape(clips['states'][code])
    if not state_geometry.is_valid:
        raise StagingError(f'authoritative {code} state polygon is invalid')

    claims_meta = _layer_contract(CLAIMS_LAYER, 'Active Mining Claims')
    plss_meta = _layer_contract(PLSS_LAYER, 'PLSS Section')
    claim_ids = _snapshot_ids(CLAIMS_LAYER, '1=1', envelopes)
    plss_ids = _snapshot_ids(PLSS_LAYER, f"PLSSID LIKE '{code}%'", envelopes)
    if not plss_ids:
        raise StagingError(f'official CadNSDI returned no {code} PLSS sections')

    sections = {}
    section_shapes = {}
    for feature in _iter_features(
            PLSS_LAYER, plss_ids,
            ('PLSSID', 'FRSTDIVID', 'FRSTDIVLAB', 'FRSTDIVNO')):
        properties = feature['properties']
        section_id = properties.get('FRSTDIVID')
        if not isinstance(section_id, str) or not section_id.startswith(code):
            raise StagingError(f'PLSS OBJECTID {properties.get("OBJECTID")} has bad ID')
        geometry = shape(feature.get('geometry'))
        if not geometry.is_valid:
            raise StagingError(f'PLSS section {section_id} source geometry is invalid')
        clipped = _polygon_only(geometry.intersection(state_geometry))
        if clipped is None or clipped.is_empty or clipped.area <= 0:
            continue
        if not clipped.is_valid:
            raise StagingError(f'PLSS section {section_id} clip is invalid')
        if section_id in sections:
            raise StagingError(f'PLSS source duplicates section {section_id}')
        geojson = mapping(clipped)
        geojson['coordinates'] = _round_geometry(geojson['coordinates'])
        sections[section_id] = {
            'type': 'Feature', 'id': section_id,
            'properties': {
                'section_id': section_id,
                'label': properties.get('FRSTDIVLAB') or section_id,
            },
            'geometry': geojson,
        }
        section_shapes[section_id] = clipped

    grid = defaultdict(set)
    for section_id, geometry in section_shapes.items():
        for cell in _bbox_cells(geometry.bounds):
            grid[cell].add(section_id)

    from spatial_clip import StateClipIndex
    clip_index = StateClipIndex(clips['states'][code])
    grouped = {}
    for feature in _iter_features(
            CLAIMS_LAYER, claim_ids, ('CSE_NR', 'CSE_NAME', 'CSE_DISP')):
        properties = feature['properties']
        serial = properties.get('CSE_NR')
        if not isinstance(serial, str) or not serial.strip():
            raise StagingError(f'claim OBJECTID {properties.get("OBJECTID")} has no serial')
        longitude, latitude = _claim_centroid(feature.get('geometry'))
        if not clip_index.contains(longitude, latitude):
            continue
        geometry = shape(feature.get('geometry'))
        if not geometry.is_valid or geometry.geom_type not in ('Polygon', 'MultiPolygon'):
            raise StagingError(f'claim {serial} source geometry is invalid')
        row = grouped.setdefault(serial, {
            'names': set(), 'dispositions': set(), 'object_ids': [], 'geometries': [],
        })
        if properties.get('CSE_NAME'):
            row['names'].add(str(properties['CSE_NAME']).strip())
        if properties.get('CSE_DISP'):
            row['dispositions'].add(str(properties['CSE_DISP']).strip())
        row['object_ids'].append(properties['OBJECTID'])
        row['geometries'].append(geometry)

    if any(not isinstance(serial, str) or not serial for serial in active['serial']):
        raise StagingError(f'{code} active snapshot serials must be nonempty text')
    expected_serials = set(active['serial'])
    if set(grouped) != expected_serials:
        missing = sorted(expected_serials - set(grouped))[:20]
        extra = sorted(set(grouped) - expected_serials)[:20]
        raise StagingError(
            f'live claim serials differ from exact active snapshot; '
            f'missing={missing}, extra={extra}')

    claim_rows = []
    unmapped = 0
    for serial in sorted(grouped):
        row = grouped[serial]
        geometry = unary_union(row['geometries'])
        candidates = set()
        for cell in _bbox_cells(geometry.bounds):
            candidates.update(grid.get(cell, ()))
        mapped = []
        for section_id in sorted(candidates):
            overlap = geometry.intersection(section_shapes[section_id])
            if not overlap.is_empty and overlap.area > 0:
                mapped.append(section_id)
        if not mapped:
            unmapped += 1
        claim_rows.append({
            'serial': serial,
            'name': '; '.join(sorted(row['names'])) or None,
            'disposition': '; '.join(sorted(row['dispositions'])) or None,
            'source_object_id': ','.join(map(str, sorted(row['object_ids']))),
            'section_ids': mapped,
            'mapping_complete': bool(mapped),
        })

    retrieved = active['retrieved']
    plss_document = {
        'schema_version': 1, 'state': code, 'kind': 'plss',
        'retrieved': retrieved, 'complete': True, 'n': len(sections),
        'capped': False, 'truncated': False, 'partial': False,
        'source': PLSS_LAYER, 'type': 'FeatureCollection',
        'features': [sections[key] for key in sorted(sections)],
    }
    claims_document = {
        'schema_version': 1, 'state': code, 'kind': 'active_claims',
        'retrieved': retrieved, 'complete': True, 'n': len(claim_rows),
        'capped': False, 'truncated': False, 'partial': False,
        'system': 'federal_mlrs', 'source': CLAIMS_LAYER, 'mode': 'active',
        'unmapped_count': unmapped, 'claims': claim_rows,
    }
    # Reuse the downstream parser before any private output is replaced.
    context = {'clip_indexes': {code: clip_index}}
    parsed_sections = open_ground._parse_plss(context, code, plss_document)
    open_ground._parse_claims(code, claims_document, set(parsed_sections))

    documents = [plss_document, claims_document]
    pending = []
    backups = []
    try:
        for path, document in zip(outputs, documents):
            descriptor, temporary = tempfile.mkstemp(
                prefix='.open-ground-staging-', suffix='.json',
                dir=os.path.dirname(path))
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump(document, target, separators=(',', ':'), allow_nan=False)
                target.flush(); os.fsync(target.fileno())
            pending.append(temporary)
        for path in outputs:
            if os.path.exists(path):
                descriptor, backup = tempfile.mkstemp(
                    prefix='.open-ground-backup-', dir=os.path.dirname(path))
                os.close(descriptor)
                with open(path, 'rb') as source, open(backup, 'wb') as target:
                    target.write(source.read())
                backups.append((path, backup))
            else:
                backups.append((path, None))
        for temporary, path in zip(pending, outputs):
            os.replace(temporary, path)
        pending = []
    except BaseException:
        for path, backup in backups:
            if backup is None:
                try: os.unlink(path)
                except FileNotFoundError: pass
            elif os.path.exists(backup):
                os.replace(backup, path)
        raise
    finally:
        for path in pending:
            try: os.unlink(path)
            except FileNotFoundError: pass
        for _, backup in backups:
            if backup:
                try: os.unlink(backup)
                except FileNotFoundError: pass

    return {
        'state': code, 'claims': len(claim_rows), 'sections': len(sections),
        'unmapped_claims': unmapped,
        'active_snapshot_sha256': hashlib.sha256(active_raw).hexdigest(),
        'claims_object_ids': len(claim_ids), 'plss_object_ids': len(plss_ids),
        'claims_object_ids_sha256': hashlib.sha256(json.dumps(
            claim_ids, separators=(',', ':')).encode()).hexdigest(),
        'plss_object_ids_sha256': hashlib.sha256(json.dumps(
            plss_ids, separators=(',', ':')).encode()).hexdigest(),
        'claims_metadata_sha256': claims_meta,
        'plss_metadata_sha256': plss_meta,
        'state_clip_sha256': clip_sha,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build private PLSS/claim-section staging for one claim state')
    parser.add_argument('--state', required=True)
    parser.add_argument('--active-snapshot', required=True)
    parser.add_argument('--plss-output', required=True)
    parser.add_argument('--claims-output', required=True)
    parser.add_argument('--state-clips', default=mlrs.DEFAULT_STATE_CLIPS)
    args = parser.parse_args(argv)
    try:
        result = build_state(
            args.state, args.active_snapshot, args.plss_output,
            args.claims_output, state_clips=args.state_clips)
    except (StagingError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
