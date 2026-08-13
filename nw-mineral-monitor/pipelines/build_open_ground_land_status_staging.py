#!/usr/bin/env python3
"""Build conservative private land-status rows for federal open-ground work.

This producer exhaustively snapshots the four national source families named
by ``build_national_open_ground_pmtiles`` and overlays them on an already
validated, state-clipped PLSS section snapshot.  It can prove that a section
is unavailable when current mineral segregation, withdrawal, or designated
wilderness polygons cover the whole section.  It deliberately never infers
``open_to_location`` or ``non_federal`` from Surface Management Agency data:
BLM's own metadata says SMA is surface jurisdiction, not title or mineral-
estate ownership.  Sections without an independently reviewed public-domain
mineral-estate source therefore remain ``unknown`` and block release.

Raw/private output only.  This module never writes below ``site/``, creates a
browser artifact, edits a registry/manifest, or enables a state release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict

import build_federal_mlrs_pmtiles as mlrs
import build_national_open_ground_pmtiles as open_ground
import build_open_ground_claim_plss_staging as claim_plss
from state_registry import CLAIM_STATES, load_states


SCHEMA_VERSION = 1
USER_AGENT = 'nw-mineral-monitor-land-status-staging/1.0'
SMA = open_ground.LAND_STATUS_SOURCES['sma']
WITHDRAWALS = open_ground.LAND_STATUS_SOURCES['withdrawals']
SEGREGATION_BASE = open_ground.LAND_STATUS_SOURCES['segregations'].rsplit('/', 1)[0]
NLCS_BASE = open_ground.LAND_STATUS_SOURCES['nlcs'].rsplit('/', 1)[0]
LAYERS = {
    'sma': {
        'url': SMA, 'name': 'Surface Management Agency',
        'fields': ('ADMIN_DEPT_CODE', 'ADMIN_AGENCY_CODE', 'ADMIN_UNIT_NAME'),
        'family': 'sma',
    },
    'withdrawals': {
        'url': WITHDRAWALS, 'name': 'Case Land Status',
        'fields': ('CSE_NR', 'CSE_NAME', 'CSE_DISP', 'CSE_LND_STATUS',
                   'US_RIGHTS', 'SEG_MIN', 'SEG_SUR'),
        'family': 'withdrawals',
    },
    'segregations_minerals': {
        'url': SEGREGATION_BASE + '/0', 'name': 'Minerals Segregated',
        'fields': ('CSE_NR', 'CSE_NAME', 'CSE_DISP', 'SEG_MIN', 'SEG_SUR'),
        'family': 'segregations',
    },
    'segregations_surface': {
        'url': SEGREGATION_BASE + '/1', 'name': 'Surface Segregated',
        'fields': ('CSE_NR', 'CSE_NAME', 'CSE_DISP', 'SEG_MIN', 'SEG_SUR'),
        'family': 'segregations',
    },
    'wilderness': {
        'url': NLCS_BASE + '/0', 'name': 'NLCS Wilderness Area',
        'fields': ('NLCS_ID', 'NLCS_NAME', 'CASEFILE_NO', 'DESIG_DATE'),
        'family': 'nlcs',
    },
    'wsa': {
        'url': NLCS_BASE + '/1', 'name': 'NLCS Wilderness Study Area',
        'fields': ('NLCS_ID', 'NLCS_NAME', 'CASEFILE_NO', 'WSA_RCMND'),
        'family': 'nlcs',
    },
}
REQUIRED_FAMILIES = frozenset(open_ground.LAND_STATUS_SOURCES)
INACTIVE_WORDS = frozenset({'closed', 'terminated', 'cancelled', 'canceled',
                            'expired', 'rejected', 'revoked'})
MINERAL_BLOCK_SCOPES = frozenset({
    'all', 'all mineral', 'all minerals', 'all locatable minerals',
    'locatable mineral', 'locatable minerals', 'metallic minerals',
    'metalliferous minerals', 'metaliferous minerals',
})


class LandStatusStagingError(ValueError):
    pass


def _strict_json(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
        value = mlrs._strict_json_bytes(raw, label)
    except (OSError, mlrs.PublicationError) as exc:
        raise LandStatusStagingError(str(exc)) from exc
    return value, raw


def _normalized(value):
    return ' '.join(str(value or '').strip().lower().split())


def _current_case(properties):
    disposition = _normalized(properties.get('CSE_DISP'))
    return bool(disposition) and not any(
        word in disposition for word in INACTIVE_WORDS)


def _mineral_scope_blocks(value):
    """True only for text that explicitly affects minerals/locatable entry."""
    scope = _normalized(value)
    # Do not substring-match this legal-status field. For example the live
    # service contains ``CLSD-EXCP METALIFRUS MNG``: that phrase expressly
    # preserves metalliferous mining rather than closing it. Unknown phrases
    # remain UNKNOWN until reviewed and added to this exact allowlist.
    return scope in MINERAL_BLOCK_SCOPES


def _metadata_contract(spec):
    metadata = claim_plss._request_json(spec['url'], {'f': 'json'})
    fields = metadata.get('fields')
    if not isinstance(fields, list):
        raise LandStatusStagingError(f'{spec["url"]} has no field schema')
    names = {row.get('name') for row in fields if isinstance(row, dict)}
    field_schema = sorted(
        (row.get('name'), row.get('type')) for row in fields
        if isinstance(row, dict) and isinstance(row.get('name'), str) and
        isinstance(row.get('type'), str))
    oid = sorted(row.get('name') for row in fields
                 if isinstance(row, dict) and
                 row.get('type') == 'esriFieldTypeOID' and
                 isinstance(row.get('name'), str))
    if (metadata.get('name') != spec['name'] or
            metadata.get('geometryType') != 'esriGeometryPolygon' or
            oid != ['OBJECTID'] or 'OBJECTID' not in names or
            not set(spec['fields']) <= names or
            metadata.get('objectIdField') not in (None, 'OBJECTID') or
            metadata.get('objectIdFieldName') not in (None, 'OBJECTID')):
        raise LandStatusStagingError(
            f'{spec["url"]} official layer identity/schema changed')
    selected = {
        'name': metadata['name'], 'geometryType': metadata['geometryType'],
        'objectIdField': 'OBJECTID',
        'maxRecordCount': metadata.get('maxRecordCount'),
        'fields': field_schema,
    }
    return hashlib.sha256(json.dumps(
        selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _reference(key, properties):
    oid = properties['OBJECTID']
    if key in ('withdrawals', 'segregations_minerals'):
        serial = str(properties.get('CSE_NR') or '').strip()
        return f'{key}:{serial or "OBJECTID-" + str(oid)}'
    if key == 'wilderness':
        identity = str(properties.get('CASEFILE_NO') or
                       properties.get('NLCS_ID') or '').strip()
        return f'wilderness:{identity or "OBJECTID-" + str(oid)}'
    return f'{key}:OBJECTID-{oid}'


def _blocking(key, properties):
    if key == 'withdrawals':
        return (_current_case(properties) and
                _normalized(properties.get('CSE_LND_STATUS')) ==
                'withdrawn lands' and
                _mineral_scope_blocks(properties.get('SEG_MIN')))
    if key == 'segregations_minerals':
        return (_current_case(properties) and
                _mineral_scope_blocks(properties.get('SEG_MIN')))
    return key == 'wilderness'


def _manager(properties):
    agency = str(properties.get('ADMIN_AGENCY_CODE') or '').strip().upper()
    department = str(properties.get('ADMIN_DEPT_CODE') or '').strip().upper()
    name = ' '.join(str(properties.get('ADMIN_UNIT_NAME') or '').split())
    if agency and agency not in ('UND', 'UNK'):
        return agency
    if department and department not in ('UND', 'UNK'):
        return department
    if name and name.upper() not in ('NA', 'N/A', 'UNDETERMINED', 'UNKNOWN'):
        return name[:160]
    return 'UNKNOWN'


def _bounded_refs(values):
    refs = sorted(set(values))
    if len(refs) <= 20:
        return refs
    digest = hashlib.sha256(json.dumps(
        refs, separators=(',', ':')).encode()).hexdigest()
    return refs[:19] + [f'additional-{len(refs) - 19}:sha256:{digest[:24]}']


def _source_feature(key, feature, state_geometry, shape):
    properties = feature.get('properties') if isinstance(feature, dict) else None
    if not isinstance(properties, dict):
        raise LandStatusStagingError(f'{key} feature has no properties')
    geometry = shape(feature.get('geometry'))
    if (geometry.is_empty or not geometry.is_valid or
            geometry.geom_type not in ('Polygon', 'MultiPolygon')):
        raise LandStatusStagingError(
            f'{key} OBJECTID {properties.get("OBJECTID")} geometry is invalid')
    clipped = claim_plss._polygon_only(geometry.intersection(state_geometry))
    if clipped is None or clipped.is_empty or clipped.area <= 0:
        return None
    if not clipped.is_valid:
        raise LandStatusStagingError(
            f'{key} OBJECTID {properties.get("OBJECTID")} state clip is invalid')
    return {
        'geometry': clipped,
        'manager': _manager(properties) if key == 'sma' else None,
        'blocking': _blocking(key, properties),
        'reference': _reference(key, properties),
        'key': key,
    }


def _classify(section, records, unary_union):
    point = section.representative_point()
    managers = {record['manager'] for record in records
                if record['key'] == 'sma' and
                record['geometry'].covers(point) and
                record['manager'] != 'UNKNOWN'}
    manager = next(iter(managers)) if len(managers) == 1 else 'UNKNOWN'
    blockers = [record for record in records
                if record['blocking'] and
                record['geometry'].intersects(section)]
    blocker_refs = [record['reference'] for record in blockers]
    covered = 0.0
    if blockers:
        union = unary_union([record['geometry'] for record in blockers])
        covered = union.intersection(section).area / section.area
        covered = max(0.0, min(1.0, covered))
    full = covered >= 1 - 1e-9
    partial = covered > 1e-12 and not full
    if full:
        disposition = 'withdrawn'
        refs = _bounded_refs(blocker_refs)
        evidence = (
            'Whole PLSS section is covered by current mineral segregation, '
            'withdrawal, or designated-wilderness polygons from the checked '
            f'official sources; surface manager={manager}.')
    else:
        disposition = 'unknown'
        refs = []
        if partial:
            reason = ('A current closure/segregation boundary crosses this '
                      'section; section-wide disposition is mixed.')
        else:
            reason = ('No checked national closure polygon covers the whole '
                      'section.')
        evidence = (
            f'{reason} Surface manager={manager}; SMA is not mineral title, '
            'and no independently reviewed public-domain mineral-estate '
            'source is supplied, so OPEN is not inferred.')
    return {
        'mineral_disposition': disposition,
        'surface_manager': manager,
        'withdrawal_refs': refs,
        'checked_sources': sorted(REQUIRED_FAMILIES),
        'boundary_uncertain': partial,
        'evidence': evidence,
        'mineral_title_status': 'unknown',
        'mineral_title_source': None,
        'mineral_title_ref': None,
        'mineral_title_reviewed': False,
    }


def _safe_output(path):
    result = os.path.realpath(path)
    if not mlrs._outside(result, mlrs.SITE):
        raise LandStatusStagingError('land-status output must remain outside site/')
    os.makedirs(os.path.dirname(result), exist_ok=True)
    return result


def build_state(code, plss_snapshot, output, retrieved, *,
                state_clips=mlrs.DEFAULT_STATE_CLIPS):
    code = str(code).upper()
    if code not in CLAIM_STATES:
        raise LandStatusStagingError(f'{code!r} is not a registry claim state')
    output = _safe_output(output)
    if not mlrs._outside(plss_snapshot, mlrs.SITE):
        raise LandStatusStagingError('raw PLSS snapshot must remain outside site/')
    try:
        retrieved = open_ground._valid_date(
            retrieved, 'land-status retrieved')
    except open_ground.PublicationError as exc:
        raise LandStatusStagingError(str(exc)) from exc
    plss, plss_raw = _strict_json(plss_snapshot, f'{code} PLSS snapshot')
    clips, clips_raw = _strict_json(state_clips, 'authoritative state clips')
    if (not isinstance(clips, dict) or clips.get('schema_version') != 1 or
            not isinstance(clips.get('states'), dict) or code not in clips['states']):
        raise LandStatusStagingError('authoritative state-clips schema is invalid')
    try:
        _, _, _, shape, unary_union = claim_plss._shapely()
    except claim_plss.StagingError as exc:
        raise LandStatusStagingError(str(exc)) from exc
    state_geometry = shape(clips['states'][code])
    if state_geometry.is_empty or not state_geometry.is_valid:
        raise LandStatusStagingError(f'authoritative {code} state polygon is invalid')
    from spatial_clip import StateClipIndex
    clip_index = StateClipIndex(clips['states'][code])
    try:
        sections = open_ground._parse_plss(
            {'clip_indexes': {code: clip_index}}, code, plss)
    except open_ground.PublicationError as exc:
        raise LandStatusStagingError(str(exc)) from exc
    section_shapes = {
        section_id: shape(row['geometry'])
        for section_id, row in sections.items()
    }
    if not section_shapes:
        raise LandStatusStagingError(f'{code} PLSS snapshot has no sections')
    registry = load_states()[code]
    envelopes = [row['bbox'] for row in registry['query_envelopes']]

    records = []
    inventory = {}
    for key, spec in LAYERS.items():
        metadata_sha = _metadata_contract(spec)
        ids = claim_plss._snapshot_ids(spec['url'], '1=1', envelopes)
        emitted = 0
        inside = 0
        content_hash = hashlib.sha256()
        for feature in claim_plss._iter_features(
                spec['url'], ids, spec['fields']):
            emitted += 1
            try:
                content_hash.update(json.dumps(
                    feature, sort_keys=True, separators=(',', ':'),
                    allow_nan=False).encode())
                content_hash.update(b'\n')
            except (TypeError, ValueError) as exc:
                raise LandStatusStagingError(
                    f'{key} feature snapshot is not canonical JSON') from exc
            row = _source_feature(key, feature, state_geometry, shape)
            if row is not None:
                records.append(row)
                inside += 1
        if emitted != len(ids):
            raise LandStatusStagingError(f'{key} source count drift')
        final_ids = claim_plss._snapshot_ids(spec['url'], '1=1', envelopes)
        final_metadata_sha = _metadata_contract(spec)
        if final_ids != ids or final_metadata_sha != metadata_sha:
            raise LandStatusStagingError(
                f'{key} official source changed during the build')
        inventory[key] = {
            'source': spec['url'], 'family': spec['family'],
            'object_ids': len(ids),
            'object_ids_sha256': hashlib.sha256(json.dumps(
                ids, separators=(',', ':')).encode()).hexdigest(),
            'feature_snapshot_sha256': content_hash.hexdigest(),
            'state_intersecting_features': inside,
            'metadata_sha256': metadata_sha,
        }

    grid = defaultdict(set)
    for index, record in enumerate(records):
        for cell in claim_plss._bbox_cells(record['geometry'].bounds):
            grid[cell].add(index)
    classifications = []
    counts = Counter()
    for section_id in sorted(section_shapes):
        section = section_shapes[section_id]
        candidates = set()
        for cell in claim_plss._bbox_cells(section.bounds):
            candidates.update(grid.get(cell, ()))
        relevant = [records[index] for index in sorted(candidates)
                    if records[index]['geometry'].intersects(section)]
        row = {'section_id': section_id,
               **_classify(section, relevant, unary_union)}
        counts[row['mineral_disposition']] += 1
        classifications.append(row)

    document = {
        'schema_version': SCHEMA_VERSION, 'state': code,
        'kind': 'land_status', 'retrieved': retrieved,
        'complete': True, 'n': len(classifications),
        'capped': False, 'truncated': False, 'partial': False,
        'sources': open_ground.LAND_STATUS_SOURCES,
        'classifications': classifications,
    }
    try:
        parsed, uncertain, missing = open_ground._parse_land_status(
            code, document, set(section_shapes),
            registry['open_ground']['mineral_estate'])
    except open_ground.PublicationError as exc:
        raise LandStatusStagingError(str(exc)) from exc
    if len(parsed) != len(section_shapes) or missing:
        raise LandStatusStagingError('land-status output failed section reconciliation')
    descriptor, pending = tempfile.mkstemp(
        prefix='.land-status-staging-', suffix='.json',
        dir=os.path.dirname(output))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(document, target, separators=(',', ':'), allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(pending, output)
        pending = None
    finally:
        if pending:
            try:
                os.unlink(pending)
            except FileNotFoundError:
                pass
    return {
        'state': code, 'sections': len(section_shapes),
        'status_counts': dict(sorted(counts.items())),
        'unknown_sections': len(uncertain),
        'open_sections': 0,
        'mineral_title_source': None,
        'release_ready': False,
        'plss_sha256': hashlib.sha256(plss_raw).hexdigest(),
        'state_clip_sha256': hashlib.sha256(clips_raw).hexdigest(),
        'sources': inventory,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build conservative private land-status staging for one claim state')
    parser.add_argument('--state', required=True)
    parser.add_argument('--plss-snapshot', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--retrieved', required=True,
                        help='official-source retrieval date (YYYY-MM-DD)')
    parser.add_argument('--state-clips', default=mlrs.DEFAULT_STATE_CLIPS)
    args = parser.parse_args(argv)
    try:
        result = build_state(
            args.state, args.plss_snapshot, args.output, args.retrieved,
            state_clips=args.state_clips)
    except (LandStatusStagingError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
