#!/usr/bin/env python3
"""WS11 state-registry loader and two-regime contract validator.

State files deliberately use JSON-compatible YAML so the stdlib-only runtime
does not acquire a YAML dependency. The validator is intentionally strict:
an incomplete state remains visible on the coverage dashboard but cannot be
marked released.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import math
from functools import lru_cache
from pathlib import PurePosixPath

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STATES_DIR = os.path.join(ROOT, 'states')
NATIONAL_PATH = os.path.join(HERE, 'config', 'national_sources.json')
DEFAULTS_PATH = os.path.join(STATES_DIR, '_defaults.yaml')
STATE_CLIPS_PATH = os.path.join(ROOT, 'infra', 'state_clips.json')

ALL_STATES = frozenset(
    'AL AK AZ AR CA CO CT DE FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS '
    'MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA '
    'WV WI WY'.split()
)

# BLM public-domain states in the national mining-claim spatial data. Keep
# this list synchronized with the authoritative citation in states/_meta.yaml.
CLAIM_STATES = frozenset(
    'AK AZ AR CA CO FL ID LA MS MT NE NV NM ND OR SD UT WA WY'.split()
)
NON_CLAIM_STATES = ALL_STATES - CLAIM_STATES

GATE_KEYS = (
    'claims_or_land_context',
    'geology_faults',
    'aeromag',
    'grades',
    'recorders',
    'quad_maps',
    'ci_scale',
)
GATE_STATUSES = frozenset(('pass', 'fail', 'blocked', 'not_applicable'))
TILED_VECTOR_FORMATS = frozenset(('pmtiles', 'remote_vector_tiles'))
TILED_RASTER_FORMATS = frozenset(('cog', 'wmts', 'wms', 'remote_raster_tiles'))
RELEASE_VECTOR_FORMATS = frozenset(('pmtiles',))
RELEASE_RASTER_FORMATS = frozenset(('cog',))
MINERAL_ESTATE_STATUSES = frozenset((
    'not_identified', 'official_candidate_not_ingested',
    'official_candidate_unavailable', 'official_candidate_insufficient',
    'reviewed_ingested',
))
RELEASE_PREFIX_PARTS = ('map-assets', 'releases')
RELEASE_TEMP_NAME = re.compile(
    r'(?:^#.*#$|~$|\.(?:bak|orig|part|swp|temp|tmp)$|'
    r'^(?:tmp|temp|temporary|stage|staging|build|build-inputs)$|'
    r'^(?:tmp|temp|staging)-|-(?:tmp|temp|staging)$)', re.IGNORECASE)


class RegistryError(ValueError):
    """A registry entry violates the WS11 contract."""


def _reject_json_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f'duplicate JSON object key {key!r}')
        out[key] = value
    return out


def _read_json_yaml(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as fh:
            value = json.load(fh, parse_constant=_reject_json_constant,
                              object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(value, dict):
            raise ValueError('top level must be an object')
        return value
    except (json.JSONDecodeError, ValueError) as exc:
        raise RegistryError(
            f'{path}: state YAML must stay in the JSON-compatible subset: {exc}'
        ) from exc


def _merge(base: dict, override: dict) -> dict:
    """Recursive mapping merge; lists are deliberately replaced, not joined."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        raise RegistryError('registry defaults and state overrides must be objects')
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache(maxsize=1)
def registry_defaults() -> dict:
    return _read_json_yaml(DEFAULTS_PATH)


@lru_cache(maxsize=1)
def national_sources() -> dict:
    return _read_json_yaml(NATIONAL_PATH)['sources']


def state_path(code: str) -> str:
    return os.path.join(STATES_DIR, f'{code.upper()}.yaml')


@lru_cache(maxsize=64)
def load_state(code: str, validate: bool = True) -> dict:
    code = code.upper()
    if code not in ALL_STATES:
        raise RegistryError(f'unsupported state {code!r}; Hawaii is outside WS11')
    path = state_path(code)
    if not os.path.exists(path):
        raise RegistryError(f'missing registry entry: {path}')
    raw = _read_json_yaml(path)
    defaults = registry_defaults()
    regime = raw.get('regime')
    row = _merge(defaults.get('common', {}), defaults.get(regime, {}))
    row = _merge(row, raw)
    if validate:
        validate_state(row, path)
    return row


def load_states(validate: bool = True) -> dict[str, dict]:
    return {code: load_state(code, validate=validate) for code in sorted(ALL_STATES)}


def is_claim_state(code: str) -> bool:
    return load_state(code)['regime'] == 'claim'


def open_ground_applicable(code: str) -> bool:
    return is_claim_state(code)


def _need(row: dict, key: str, path: str, errors: list[str]):
    if key not in row or row[key] in (None, '', [], {}):
        errors.append(f'{path}: missing {key}')


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _is_count(value, minimum=0):
    return (isinstance(value, int) and not isinstance(value, bool) and
            value >= minimum)


def _is_measurement(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and value >= 0)


def _nonempty_text(value, minimum=1):
    return isinstance(value, str) and len(value.strip()) >= minimum


def _https_url(value):
    return (_nonempty_text(value) and
            re.fullmatch(r'https://[^\s]+', value.strip()) is not None)


def is_xyz_tile_template(value):
    """Return whether *value* is a browser-safe XYZ URL template.

    A COG is the immutable raster artifact and provenance boundary, but it is
    not itself a MapLibre ``tiles`` URL.  Released raster adapters therefore
    also have to name an actual XYZ template.  Keep this stdlib-only and
    deliberately conservative: local browser-relative paths and HTTPS URLs
    are accepted; protocol-relative, traversal, TIFF, and arbitrary-scheme
    URLs are not.
    """
    if not _nonempty_text(value) or any(char.isspace() for char in value):
        return False
    if any(value.count(token) != 1 for token in ('{z}', '{x}', '{y}')):
        return False
    if value.startswith('//'):
        return False
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', value) and not value.startswith('https://'):
        return False
    if not (value.startswith('https://') or value.startswith('/') or
            re.match(r'^[A-Za-z0-9_.{}-]', value)):
        return False
    path = value.split('?', 1)[0].split('#', 1)[0]
    if any(part == '..' for part in path.split('/')):
        return False
    if re.search(r'\.tiff?$', path, re.I):
        return False
    return True


@lru_cache(maxsize=1)
def _state_clips() -> dict:
    return _read_json_yaml(STATE_CLIPS_PATH)['states']


def _coordinate_pairs(value):
    if (isinstance(value, list) and len(value) >= 2 and
            all(isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in value[:2])):
        yield float(value[0]), float(value[1])
    elif isinstance(value, list):
        for child in value:
            yield from _coordinate_pairs(child)


def _valid_jurisdiction_id(value, jurisdiction_type):
    """Validate recorder jurisdiction identifiers without treating Alaska as a county."""
    if jurisdiction_type == 'county':
        return isinstance(value, str) and re.fullmatch(r'\d{5}', value) is not None
    if jurisdiction_type == 'recording_district':
        return (_nonempty_text(value) and value == value.strip() and
                len(value) <= 100 and '\n' not in value and '\r' not in value)
    return False


def _validate_release_file(obj, path_field, sha_field, bytes_field, label,
                           errors, suffixes):
    """Validate one immutable upload descriptor expressed as sibling fields.

    The registry intentionally keeps its established flat field names.  This
    helper is the single release-path contract shared by tiled deliveries and
    JSON evidence: canonical browser-relative path, immutable prefix, exact
    digest basename, positive byte count, and no hidden/temporary segments.
    Disabled-state null placeholders never call this helper.
    """
    if not isinstance(obj, dict):
        errors.append(f'{label}: immutable release descriptor must be an object')
        return False
    raw_path = obj.get(path_field)
    sha256 = obj.get(sha_field)
    size = obj.get(bytes_field)
    valid_path = isinstance(raw_path, str) and bool(raw_path)
    path_obj = PurePosixPath(raw_path) if valid_path else PurePosixPath('.')
    if valid_path:
        valid_path = (
            '\\' not in raw_path and '\n' not in raw_path and '\r' not in raw_path and
            all(32 <= ord(char) != 127 for char in raw_path) and
            not path_obj.is_absolute() and path_obj.as_posix() == raw_path and
            len(path_obj.parts) > len(RELEASE_PREFIX_PARTS) and
            path_obj.parts[:len(RELEASE_PREFIX_PARTS)] == RELEASE_PREFIX_PARTS and
            all(part not in ('', '.', '..') and not part.startswith('.') and
                RELEASE_TEMP_NAME.search(part) is None for part in path_obj.parts))
    valid_sha = (isinstance(sha256, str) and
                 re.fullmatch(r'[0-9a-f]{64}', sha256) is not None)
    valid_bytes = (isinstance(size, int) and not isinstance(size, bool) and size > 0)
    suffix = path_obj.suffix.lower() if valid_path else ''
    content_addressed = (
        valid_path and valid_sha and suffix in suffixes and
        path_obj.name == f'{sha256}{suffix}')
    if not (valid_path and valid_sha and valid_bytes and content_addressed):
        errors.append(
            f'{label}: {path_field}/{sha_field}/{bytes_field} must describe an exact '
            f'content-addressed {sorted(suffixes)} file below map-assets/releases/')
        return False
    return True


def _release_reference(container, path_field, context, metadata=None,
                       sha_field='sha256', bytes_field='bytes'):
    if isinstance(container, dict) and container.get(path_field) not in (None, ''):
        yield (context, container, path_field,
               container if metadata is None else metadata,
               sha_field, bytes_field)


def _release_delivery_references(delivery, context, *, include_zero=False):
    if not isinstance(delivery, dict):
        return
    yield from _release_reference(delivery, 'artifact', context)
    zero = delivery.get('zero_inventory')
    if include_zero and isinstance(zero, dict):
        yield from _release_reference(
            zero, 'evidence_artifact', f'{context}.zero_inventory')


def release_file_descriptors(row):
    """Yield the exact upload fields authorized by one enabled state schema.

    Each tuple is ``(context, path_object, path_field, metadata_object,
    sha_field, bytes_field)``. Keeping this inventory in the registry module
    lets deployment consume precisely the same flat sibling mapping that
    release validation enforces.
    """
    for field in ('geology', 'faults', 'aeromag'):
        yield from _release_delivery_references(
            row.get(field), field, include_zero=field == 'faults')
    systems = row.get('claim_systems')
    if isinstance(systems, list):
        for index, system in enumerate(systems):
            if not isinstance(system, dict):
                continue
            context = f'claim_systems[{index}]'
            parts = system.get('publication_artifacts')
            if (system.get('id') == 'federal_mlrs' and
                    isinstance(parts, dict)):
                for part_id in sorted(parts):
                    yield from _release_delivery_references(
                        parts[part_id], f'{context}.publication_artifacts.{part_id}')
            else:
                yield from _release_delivery_references(system, context)
            yield from _release_reference(
                system, 'publication_inventory_artifact', context,
                sha_field='publication_inventory_sha256',
                bytes_field='publication_inventory_bytes')
    if row.get('regime') == 'non_claim':
        yield from _release_delivery_references(row.get('land_context'), 'land_context')
        for field in ('aml', 'trust_land'):
            container = row.get(field)
            if not isinstance(container, dict):
                continue
            if container.get('release_inventory_status') == 'ingested_complete':
                yield from _release_delivery_references(container, field)
            yield from _release_reference(
                container, 'evidence_artifact', field,
                sha_field='evidence_sha256', bytes_field='evidence_bytes')
    release = row.get('release')
    acceptance = release.get('acceptance') if isinstance(release, dict) else None
    if not isinstance(acceptance, dict):
        return
    grades = acceptance.get('grades')
    yield from _release_reference(
        grades, 'evidence_artifact', 'release.acceptance.grades')
    district = acceptance.get('district_anchor')
    # The district pointer is either absent or exactly the grade artifact.
    # Its source_sha256 is PP 610's upstream document checksum, so upload
    # metadata comes from the grade descriptor instead.
    yield from _release_reference(
        district, 'artifact', 'release.acceptance.district_anchor', grades)
    if row.get('regime') == 'claim':
        for field in ('recorders', 'expiration_watch'):
            container = acceptance.get(field)
            yield from _release_reference(
                container, 'evidence_artifact', f'release.acceptance.{field}',
                sha_field='evidence_sha256', bytes_field='evidence_bytes')
    quads = acceptance.get('quad_maps')
    yield from _release_reference(
        quads, 'ranked_targets_artifact', 'release.acceptance.quad_maps',
        sha_field='ranked_targets_sha256', bytes_field='ranked_targets_bytes')
    targets = quads.get('targets') if isinstance(quads, dict) else None
    if isinstance(targets, list):
        for index, target in enumerate(targets):
            yield from _release_reference(
                target, 'inventory_artifact',
                f'release.acceptance.quad_maps.targets[{index}]',
                sha_field='inventory_sha256', bytes_field='inventory_bytes')
    ci = acceptance.get('ci_scale')
    yield from _release_reference(
        ci, 'evidence_artifact', 'release.acceptance.ci_scale')


def _validate_release_acceptance(row: dict, path: str, errors: list[str]):
    """Validate metrics that cannot be inferred from tile headers alone."""
    acceptance = (row.get('release') or {}).get('acceptance')
    if not isinstance(acceptance, dict):
        errors.append(f'{path}: released state needs release.acceptance metrics')
        return
    grades = acceptance.get('grades')
    if not isinstance(grades, dict):
        errors.append(f'{path}: release.acceptance.grades must be an object')
        grades = {}
    grade_artifact = grades.get('evidence_artifact')
    _validate_release_file(
        grades, 'evidence_artifact', 'sha256', 'bytes',
        f'{path}: grade acceptance', errors, {'.json'})
    finding = grades.get('low_endowment_finding')
    counters = ('graded_mines', 'primary_sources', 'verbatim_quotes', 'page_cites')
    if any(not _is_count(grades.get(field)) for field in counters):
        errors.append(f'{path}: grade acceptance counters must be nonnegative integers')
    quantitative = (
        all(_is_count(grades.get(field)) for field in counters) and
        grades['graded_mines'] >= 25 and grades['primary_sources'] >= 2 and
        grades['verbatim_quotes'] >= grades['graded_mines'] and
        grades['page_cites'] >= grades['graded_mines']
    )
    finding_sources = finding.get('sources') if isinstance(finding, dict) else None
    sources_valid = (isinstance(finding_sources, list) and len(finding_sources) >= 2 and
                     all(isinstance(source, dict) and
                         _nonempty_text(source.get('source_id')) and
                         _nonempty_text(source.get('title'), 3) and
                         _nonempty_text(source.get('authority')) and
                         source.get('primary') is True and
                         _https_url(source.get('url')) and
                         isinstance(source.get('document_sha256'), str) and
                         re.fullmatch(r'[0-9a-f]{64}',
                                      source.get('document_sha256', '')) is not None and
                         isinstance(source.get('page_index_sha256'), str) and
                         re.fullmatch(r'[0-9a-f]{64}',
                                      source.get('page_index_sha256', '')) is not None and
                         _nonempty_text(source.get('page_cite')) and
                         any(char.isdigit() for char in source.get('page_cite', '')) and
                         _nonempty_text(source.get('verbatim_quote'), 8) and
                         source.get('quote_verbatim') is True and
                         isinstance(source.get('page_text_sha256'), str) and
                         re.fullmatch(r'[0-9a-f]{64}',
                                      source.get('page_text_sha256', '')) is not None
                         for source in finding_sources))
    source_ids = ([source.get('source_id') for source in finding_sources]
                  if sources_valid else [])
    documented_finding = (
        isinstance(finding, dict) and
        _nonempty_text(finding.get('finding'), 40) and
        finding.get('review_complete') is True and sources_valid and
        len(source_ids) == len(set(source_ids))
    )
    if not (quantitative or documented_finding):
        errors.append(f'{path}: grade acceptance needs 25 mines/2 primary sources '
                      'with typed source evidence and quote/page counts, or a '
                      'documented two-source finding')
    district = acceptance.get('district_anchor')
    district_count = (district.get('district_count')
                      if isinstance(district, dict) else None)
    district_artifact = (district.get('artifact')
                         if isinstance(district, dict) else None) or grade_artifact
    district_valid = (
        isinstance(district, dict) and district.get('source_id') == 'pp610' and
        _nonempty_text(district_artifact) and district_artifact == grade_artifact and
        isinstance(district.get('source_sha256'), str) and
        re.fullmatch(r'[0-9a-f]{64}', district['source_sha256']) is not None and
        _is_count(district_count) and district.get('complete') is True and
        (district_count > 0 or
         _nonempty_text(district.get('no_district_finding'), 40))
    )
    if not district_valid:
        errors.append(f'{path}: release needs a complete PP 610 district-anchor artifact '
                      'or an explicit no-district finding')
    recorder = acceptance.get('recorders')
    if not isinstance(recorder, dict):
        errors.append(f'{path}: release.acceptance.recorders must be an object')
        recorder = {}
    if row.get('regime') == 'claim':
        code = row.get('state')
        expected_type = 'recording_district' if code == 'AK' else 'county'
        jurisdiction_type = recorder.get('jurisdiction_type')
        configured_type = _mapping(row.get('recorder')).get('jurisdiction_type')
        live = recorder.get('live_claim_jurisdiction_ids')
        covered = recorder.get('covered_jurisdiction_ids')
        active_claims = recorder.get('active_claims')
        lists_valid = (
            isinstance(live, list) and isinstance(covered, list) and
            all(_valid_jurisdiction_id(item, jurisdiction_type)
                for item in live + covered) and
            len(live) == len(set(live)) and len(covered) == len(set(covered))
        )
        activity_valid = (
            _is_count(active_claims) and
            ((active_claims == 0 and live == [] and covered == []) or
             (active_claims > 0 and bool(live) and bool(covered))))
        # A zero matrix is valid only as an explicit active_count=0 result from
        # the artifact-backed spatial join; it is never inferred from a blank
        # registry row or turned into a fake county.
        _validate_release_file(
            recorder, 'evidence_artifact', 'evidence_sha256', 'evidence_bytes',
            f'{path}: recorder acceptance', errors, {'.json'})
        if (jurisdiction_type != expected_type or configured_type != expected_type or
                recorder.get('inventory_complete') is not True or
                not activity_valid or not lists_valid or
                set(live or []) != set(covered or [])):
            errors.append(f'{path}: recorder acceptance must identify and cover exactly every '
                          f'live-claim {expected_type.replace("_", " ")}')
        matrix = _mapping(row.get('recorder')).get('matrix')
        matrix_valid = isinstance(matrix, list) and all(
            isinstance(item, dict) and
            _valid_jurisdiction_id(item.get('jurisdiction_id'), expected_type) and
            item.get('status') == 'accepted' and
            _nonempty_text(item.get('portal_vendor')) and
            _https_url(item.get('portal_url'))
            for item in (matrix or []))
        matrix_ids = ([item['jurisdiction_id'] for item in matrix]
                      if matrix_valid else [])
        if (not matrix_valid or len(matrix_ids) != len(set(matrix_ids)) or
                (lists_valid and set(matrix_ids) != set(live))):
            errors.append(f'{path}: accepted recorder matrix does not match live-claim '
                          'jurisdictions')
        watch_raw = acceptance.get('expiration_watch')
        watch = _mapping(watch_raw)
        expected_systems = set(system_ids for system_ids in (
            item.get('id') for item in row.get('claim_systems', [])
            if isinstance(item, dict)) if _nonempty_text(system_ids))
        watch_systems = watch.get('system_ids') if isinstance(watch, dict) else None
        _validate_release_file(
            watch, 'evidence_artifact', 'evidence_sha256', 'evidence_bytes',
            f'{path}: expiration-watch acceptance', errors, {'.json'})
        if (not isinstance(watch_raw, dict) or
                not _nonempty_text(watch.get('run_id')) or
                not isinstance(watch.get('generated'), str) or
                re.fullmatch(r'\d{4}-\d{2}-\d{2}T[^\s]+',
                             watch.get('generated', '')) is None or
                watch.get('complete') is not True or
                not isinstance(watch_systems, list) or
                set(watch_systems) != expected_systems or
                len(watch_systems) != len(set(watch_systems))):
            errors.append(f'{path}: claim-state release needs a complete expiration-watch '
                          'run covering every declared claim system')
    quad_acceptance = acceptance.get('quad_maps')
    quads = quad_acceptance.get('targets') if isinstance(quad_acceptance, dict) else None
    ranked_content_addressed = _validate_release_file(
        quad_acceptance, 'ranked_targets_artifact', 'ranked_targets_sha256',
        'ranked_targets_bytes', f'{path}: ranked-target acceptance', errors,
        {'.json'})
    quad_ids = ([item.get('target_id') for item in quads]
                if isinstance(quads, list) and all(isinstance(item, dict) for item in quads)
                else [])
    if (not ranked_content_addressed or
            not isinstance(quads, list) or len(quads) != 5 or
            any(not isinstance(item, dict) or
                not _nonempty_text(item.get('target_id')) or
                not _validate_release_file(
                    item, 'inventory_artifact', 'inventory_sha256',
                    'inventory_bytes', f'{path}: quad target inventory', errors,
                    {'.json'}) for item in (quads or [])) or
            len(quad_ids) != len(set(quad_ids))):
        errors.append(f'{path}: quad-map acceptance needs content-addressed ranked '
                      'evidence and inventories for exactly five targets')
    ci = acceptance.get('ci_scale')
    if not isinstance(ci, dict):
        errors.append(f'{path}: release.acceptance.ci_scale must be an object')
        ci = {}
    ci_artifact_valid = _validate_release_file(
        ci, 'evidence_artifact', 'sha256', 'bytes',
        f'{path}: CI acceptance', errors, {'.json'})
    if (not ci_artifact_valid or
            not _https_url(ci.get('run_url')) or
            not isinstance(ci.get('commit'), str) or
            not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',
                             ci.get('commit', '')) or
            ci.get('state_toggle_green') is not True or
            ci.get('statewide_browser_json') is not False or
            not _is_measurement(ci.get('heap_mb')) or
            not _is_measurement(ci.get('bulk_origin_storage_mb'))):
        errors.append(f'{path}: CI acceptance needs content-addressed release evidence, '
                      'an exact green run/full commit, and measured budgets')


def _validate_delivery(obj: dict, label: str, errors: list[str], raster=False,
                       release=False, zero_state=None):
    if not isinstance(obj, dict):
        errors.append(f'{label}: expected object')
        return
    fmt = obj.get('browser_delivery')
    allowed = ((RELEASE_RASTER_FORMATS if raster else RELEASE_VECTOR_FORMATS)
               if release else
               (TILED_RASTER_FORMATS if raster else TILED_VECTOR_FORMATS))
    if fmt not in allowed:
        errors.append(f'{label}: browser_delivery {fmt!r} must be one of {sorted(allowed)}')
    raw_path = obj.get('browser_path') or obj.get('url') or ''
    if not isinstance(raw_path, str):
        errors.append(f'{label}: browser path/url must be text')
        raw_path = ''
    p = raw_path
    if re.search(r'\.(?:geo)?json(?:$|[?#])', p, re.I):
        errors.append(f'{label}: statewide browser JSON is forbidden ({p})')
    if release:
        for field in ('artifact', 'sha256', 'bytes'):
            if obj.get(field) in (None, ''):
                errors.append(f'{label}: released tiled data needs {field}')
        if not raster:
            source_layers = obj.get('source_layers')
            if (not isinstance(source_layers, list) or not source_layers or
                    any(not isinstance(item, str) or not item for item in source_layers) or
                    len(source_layers) != len(set(source_layers))):
                errors.append(f'{label}: released PMTiles needs unique source_layers')
            required_properties = obj.get('required_properties')
            if isinstance(required_properties, list):
                valid_properties = (
                    bool(required_properties) and
                    all(_nonempty_text(item) for item in required_properties) and
                    len(required_properties) == len(set(required_properties)))
            elif isinstance(required_properties, dict):
                valid_properties = (
                    bool(required_properties) and
                    set(required_properties) == set(source_layers or []) and
                    all(isinstance(items, list) and bool(items) and
                        all(_nonempty_text(item) for item in items) and
                        len(items) == len(set(items))
                        for items in required_properties.values()))
            else:
                valid_properties = False
            if not valid_properties:
                errors.append(f'{label}: released PMTiles needs required_properties '
                              'as unique strings, optionally keyed by source layer')
            layer_metadata = obj.get('layer_metadata')
            metadata_total = (sum(item['n'] for item in layer_metadata.values())
                              if isinstance(layer_metadata, dict) and
                              all(isinstance(item, dict) and
                                  _is_count(item.get('n'))
                                  for item in layer_metadata.values()) else None)
            zero = obj.get('zero_inventory')
            zero_valid = (
                zero_state is not None and isinstance(zero, dict) and
                set(zero) == {
                    'evidence_artifact', 'sha256', 'bytes', 'baseline_manifest_key',
                    'baseline_sha256', 'state_count', 'complete'} and
                _validate_release_file(
                    zero, 'evidence_artifact', 'sha256', 'bytes',
                    f'{label}: zero inventory', errors, {'.json'}) and
                zero.get('baseline_manifest_key') == 'national_baselines.faults' and
                isinstance(zero.get('baseline_sha256'), str) and
                re.fullmatch(r'[0-9a-f]{64}', zero['baseline_sha256']) is not None and
                zero.get('state_count') == 0 and zero.get('complete') is True)
            if zero is not None and not zero_valid:
                errors.append(
                    f'{label}: zero_inventory must be the exact content-addressed '
                    'national fault-baseline descriptor')
            metadata_valid = (
                isinstance(layer_metadata, dict) and
                set(layer_metadata) == set(source_layers or []) and
                all(isinstance(item, dict) and _is_count(item.get('n')) and
                    item.get('availability') == 'complete' and
                    item.get('complete') is True
                    for item in layer_metadata.values()) and
                (metadata_total > 0 or metadata_total == 0 and zero_valid))
            if not metadata_valid:
                errors.append(f'{label}: released layer_metadata must exactly cover every '
                              'declared source layer with reviewed counts and complete status; '
                              'zero needs content-addressed national-baseline evidence')
        suffixes = {'.tif', '.tiff'} if raster else {'.pmtiles'}
        _validate_release_file(
            obj, 'artifact', 'sha256', 'bytes', label, errors, suffixes)
        if (not isinstance(obj.get('bytes'), int) or isinstance(obj.get('bytes'), bool)
                or obj.get('bytes', 0) <= 127):
            errors.append(f'{label}: released artifact bytes must be an integer >127')
        if raster:
            if not is_xyz_tile_template(obj.get('tile_url')):
                errors.append(f'{label}: released COG needs a valid XYZ tile_url '
                              'template containing {z}/{x}/{y}; a TIFF URL is not tiles')
            if obj.get('tile_size', 256) not in (256, 512):
                errors.append(f'{label}: released raster tile_size must be 256 or 512')
            bounds = obj.get('bounds')
            valid_bounds = (
                isinstance(bounds, list) and len(bounds) == 4 and
                all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) for value in bounds) and
                -180 <= bounds[0] < bounds[2] <= 180 and
                -90 <= bounds[1] < bounds[3] <= 90)
            if not valid_bounds:
                errors.append(f'{label}: released raster needs finite ordered EPSG:4326 bounds')
            minzoom, maxzoom = obj.get('minzoom'), obj.get('maxzoom')
            if (not isinstance(minzoom, int) or isinstance(minzoom, bool) or
                    not isinstance(maxzoom, int) or isinstance(maxzoom, bool) or
                    not 0 <= minzoom <= maxzoom <= 24):
                errors.append(f'{label}: released raster needs integer 0<=minzoom<=maxzoom<=24')


def validate_state(row: dict, path: str = '<state>') -> list[str]:
    if not isinstance(row, dict):
        raise RegistryError(f'{path}: state registry entry must be an object')
    errors: list[str] = []
    for key in (
        'schema_version', 'state', 'name', 'regime', 'phase', 'query_envelopes',
        'geological_survey', 'geology', 'faults', 'aeromag', 'historic_serials',
        'aml', 'trust_land', 'recorder', 'land_context',
        'done_gate', 'release',
    ):
        _need(row, key, path, errors)
    raw_code = row.get('state')
    code = str(raw_code or '').upper()
    if code not in ALL_STATES:
        errors.append(f'{path}: state {code!r} is not one of the 49 WS11 states')
    if raw_code != code:
        errors.append(f'{path}: state code must be uppercase canonical text')
    if row.get('schema_version') != 1 or isinstance(row.get('schema_version'), bool):
        errors.append(f'{path}: schema_version must be integer 1')
    if os.path.basename(path) not in ('<state>', f'{code}.yaml'):
        errors.append(f'{path}: filename/state mismatch ({code})')
    regime = row.get('regime')
    expected = 'claim' if code in CLAIM_STATES else 'non_claim'
    if regime != expected:
        errors.append(f'{path}: {code} regime must be {expected}, got {regime!r}')
    if isinstance(row.get('phase'), bool) or row.get('phase') not in (1, 2, 3, 4):
        errors.append(f'{path}: phase must be one of 1, 2, 3, 4')
    envelopes = row.get('query_envelopes')
    if not isinstance(envelopes, list) or not envelopes:
        errors.append(f'{path}: query_envelopes must be a nonempty list')
    else:
        envelope_ids = []
        valid_bboxes = []
        for index, envelope in enumerate(envelopes):
            bbox = envelope.get('bbox') if isinstance(envelope, dict) else None
            good = (isinstance(bbox, list) and len(bbox) == 4 and
                    all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) for v in bbox))
            if not good:
                errors.append(f'{path}: query_envelopes[{index}].bbox must be four finite numbers')
                continue
            x0, y0, x1, y1 = bbox
            if not (-180 <= x0 < x1 <= 180 and -90 <= y0 < y1 <= 90):
                errors.append(f'{path}: query_envelopes[{index}].bbox is unordered/out of range')
            else:
                valid_bboxes.append(bbox)
            if (not isinstance(envelope, dict) or
                    not _nonempty_text(envelope.get('id'))):
                errors.append(f'{path}: query_envelopes[{index}] needs a textual id')
            else:
                envelope_ids.append(envelope['id'])
            if not isinstance(envelope, dict) or envelope.get('crs') != 'EPSG:4326':
                errors.append(f'{path}: query_envelopes[{index}].crs must be EPSG:4326')
        if len(envelope_ids) != len(set(envelope_ids)):
            errors.append(f'{path}: query envelope ids must be unique')
        if code in ALL_STATES and valid_bboxes:
            geometry = _mapping(_state_clips().get(code))
            points = list(_coordinate_pairs(geometry.get('coordinates')))
            # Circular longitude avoids the Alaska antimeridian mean landing
            # in Canada. A registry envelope wholly unrelated to the state is
            # therefore rejected before it reaches MLRS or a state service.
            if not points:
                errors.append(f'{path}: official state footprint has no usable coordinates')
            else:
                mean_x = math.degrees(math.atan2(
                    sum(math.sin(math.radians(x)) for x, _ in points),
                    sum(math.cos(math.radians(x)) for x, _ in points)))
                mean_y = sum(y for _, y in points) / len(points)
                if not any(x0 <= mean_x <= x1 and y0 <= mean_y <= y1
                           for x0, y0, x1, y1 in valid_bboxes):
                    errors.append(f'{path}: query envelopes do not cover the official state footprint')
                # A centroid-only check accepted a tiny box placed around the
                # mean while omitting virtually the entire state. A claim-state
                # query is the upstream acquisition boundary, so even one
                # omitted authoritative clip vertex can silently omit a border
                # claim: those states require exact vertex containment. A
                # non-claim registry envelope may intentionally trim offshore
                # state-water vertices, so retain the documented broad-footprint
                # tolerance there.
                covered = sum(
                    any(x0 <= x <= x1 and y0 <= y <= y1
                        for x0, y0, x1, y1 in valid_bboxes)
                    for x, y in points)
                if regime == 'claim' and covered != len(points):
                    errors.append(
                        f'{path}: claim-state query envelope union must contain '
                        'every authoritative state-footprint vertex')
                elif regime != 'claim' and covered / len(points) < 0.80:
                    errors.append(
                        f'{path}: query envelope union covers too little of the '
                        'official state footprint')
    open_ground = _mapping(row.get('open_ground'))
    if open_ground.get('applicable') is not (regime == 'claim'):
        errors.append(f'{path}: open_ground.applicable disagrees with regime')
    display = open_ground.get('display_when_missing')
    if regime == 'non_claim' and display != 'N/A':
        errors.append(f'{path}: non-claim open ground missing value must display N/A')
    if regime == 'claim':
        estate_raw = open_ground.get('mineral_estate')
        estate = _mapping(estate_raw)
        estate_keys = {
            'status', 'authority', 'source_url', 'ownership_field',
            'locatable_values', 'surface_management_is_not_title',
            'reviewed', 'finding',
        }
        status = estate.get('status')
        values = estate.get('locatable_values')
        if not isinstance(estate_raw, dict) or set(estate) != estate_keys:
            errors.append(
                f'{path}: claim-state open_ground.mineral_estate schema is invalid')
        if status not in MINERAL_ESTATE_STATUSES:
            errors.append(
                f'{path}: claim-state mineral-estate status is invalid')
        if estate.get('surface_management_is_not_title') is not True:
            errors.append(
                f'{path}: surface management must never be treated as mineral title')
        if (not _nonempty_text(estate.get('reviewed')) or
                re.fullmatch(r'\d{4}-\d{2}-\d{2}', estate.get('reviewed', '')) is None):
            errors.append(
                f'{path}: mineral-estate review date must be YYYY-MM-DD')
        if not _nonempty_text(estate.get('finding'), 60):
            errors.append(
                f'{path}: mineral-estate review needs an explicit finding')
        if (not isinstance(values, list) or
                any(not _nonempty_text(value) for value in values) or
                len(set(values or [])) != len(values or [])):
            errors.append(
                f'{path}: mineral-estate locatable_values must be unique text')
        if status == 'not_identified':
            if (estate.get('authority') is not None or
                    estate.get('source_url') is not None or
                    estate.get('ownership_field') is not None or values != []):
                errors.append(
                    f'{path}: unidentified mineral-estate source cannot invent '
                    'authority, URL, field, or values')
        elif status in MINERAL_ESTATE_STATUSES:
            if (not _nonempty_text(estate.get('authority')) or
                    not _https_url(estate.get('source_url'))):
                errors.append(
                    f'{path}: official mineral-estate candidate needs authority '
                    'and an HTTPS source URL')
            field = estate.get('ownership_field')
            if field is not None and not _nonempty_text(field):
                errors.append(
                    f'{path}: mineral-estate ownership_field must be text or null')
            if status == 'reviewed_ingested' and (
                    not _nonempty_text(field) or not values):
                errors.append(
                    f'{path}: reviewed mineral-estate ingest needs an ownership '
                    'field and explicit locatable values')
    raw_systems = row.get('claim_systems')
    if not isinstance(raw_systems, list):
        errors.append(f'{path}: claim_systems must be a list (empty for non-claim states)')
        systems = []
    else:
        systems = raw_systems
    release = _mapping(row.get('release'))
    if release.get('status') not in ('building', 'done', 'blocked'):
        errors.append(f'{path}: release.status must be building, done, or blocked')
    if not isinstance(release.get('enabled'), bool):
        errors.append(f'{path}: release.enabled must be boolean')
    if release.get('enabled') is True and release.get('status') != 'done':
        errors.append(f'{path}: release.enabled=true requires release.status=done')
    if release.get('status') == 'done' and release.get('enabled') is not True:
        errors.append(f'{path}: release.status=done requires release.enabled=true')
    is_release = release.get('status') == 'done' or release.get('enabled') is True
    if any(not isinstance(system, dict) for system in systems):
        errors.append(f'{path}: every claim system must be an object')
    raw_system_ids = [s.get('id') for s in systems if isinstance(s, dict)]
    if any(not _nonempty_text(item) for item in raw_system_ids):
        errors.append(f'{path}: claim system ids must be nonempty text')
    system_ids = [item for item in raw_system_ids if isinstance(item, str)]
    ids = set(system_ids)
    if len(system_ids) != len(ids):
        errors.append(f'{path}: claim system ids must be unique')
    if regime == 'claim' and 'federal_mlrs' not in ids:
        errors.append(f'{path}: claim state lacks federal_mlrs claim system')
    if regime == 'non_claim' and systems:
        errors.append(f'{path}: non-claim state must not declare staking/claim systems')
    if code == 'AK' and 'alaska_state_claims' not in ids:
        errors.append(f'{path}: Alaska must declare its separate state-claim system')
    for system in (item for item in systems if isinstance(item, dict)):
        for field in ('id', 'authority', 'source_id'):
            if not _nonempty_text(system.get(field)):
                errors.append(f'{path}: claim system needs text {field}')
        if system.get('expiration_watch') is not True:
            errors.append(f'{path}: claim system {system.get("id")} needs expiration_watch=true')
        layers = system.get('layers')
        if not isinstance(layers, dict):
            errors.append(f'{path}: claim system {system.get("id")} layers must be an object')
            layers = {}
        if system.get('id') == 'federal_mlrs' and not {'active', 'closed'} <= set(layers):
            errors.append(f'{path}: federal_mlrs layers must include active and closed')
        source_layers = system.get('source_layers')
        if source_layers is not None and (
                not isinstance(source_layers, list) or
                any(not isinstance(item, str) or not item for item in source_layers) or
                len(source_layers) != len(set(source_layers))):
            errors.append(f'{path}: claim system source_layers must be unique strings')
        publication_parts = system.get('publication_artifacts')
        split_federal_release = (is_release and system.get('id') == 'federal_mlrs' and
                                 isinstance(publication_parts, dict))
        _validate_delivery(system, f'{path}: claim_systems.{system.get("id")}', errors,
                           release=is_release and not split_federal_release)
        if split_federal_release:
            if set(publication_parts) != {'claims', 'open_ground'}:
                errors.append(f'{path}: released federal_mlrs publication_artifacts '
                              'must contain claims and open_ground')
            for part_id, part in publication_parts.items():
                _validate_delivery(
                    part, f'{path}: claim_systems.federal_mlrs.'
                    f'publication_artifacts.{part_id}', errors, release=True)
    if code == 'AK':
        backbone = _mapping(row.get('occurrence_backbone'))
        if (backbone.get('source_id') != 'ardf' or
                backbone.get('role') != 'primary_occurrence_backbone' or
                not backbone.get('feature_service_url')):
            errors.append(f'{path}: Alaska occurrence backbone must be ARDF with its feature service')
        alaska_system = next((item for item in systems if isinstance(item, dict)
                              and item.get('id') == 'alaska_state_claims'), {})
        if set(_mapping(alaska_system.get('layers'))) != {'active', 'pending', 'closed'}:
            errors.append(f'{path}: Alaska state claims must map active, pending, and closed layers')
        policy = _mapping(alaska_system.get('deadline_policy'))
        if (policy.get('effective_dated') is not True or
                not _https_url(_mapping(policy.get('rent')).get('source_url')) or
                not _https_url(_mapping(policy.get('labor')).get('source_url'))):
            errors.append(f'{path}: Alaska state claims need effective-dated rent/labor policy')
    if is_release:
        for system in (item for item in systems if isinstance(item, dict)):
            required = ({'active', 'closed', 'open_ground'}
                        if system.get('id') == 'federal_mlrs'
                        else {'active', 'pending', 'closed'})
            release_layers = system.get('source_layers')
            release_layer_set = (set(release_layers)
                                 if isinstance(release_layers, list) and
                                 all(isinstance(item, str) for item in release_layers)
                                 else set())
            if not required <= release_layer_set:
                errors.append(f'{path}: released {system.get("id")} PMTiles lacks required '
                              f'source_layers {sorted(required)}')
            if system.get('id') == 'federal_mlrs':
                parts = system.get('publication_artifacts')
                parts_valid = isinstance(parts, dict) and set(parts) == {
                    'claims', 'open_ground'}
                if parts_valid:
                    claims_layers = _mapping(parts.get('claims')).get('source_layers')
                    open_layers = _mapping(parts.get('open_ground')).get('source_layers')
                    parts_valid = (claims_layers == ['active', 'closed'] and
                                   open_layers == ['open_ground'])
                combined = ([layer for part in parts.values()
                             for layer in (_mapping(part).get('source_layers') or [])]
                            if isinstance(parts, dict) else [])
                if (not parts_valid or len(combined) != len(set(combined)) or
                        set(combined) != release_layer_set):
                    errors.append(f'{path}: released federal_mlrs needs disjoint immutable '
                                  'claims(active/closed) and open_ground publication artifacts')
                else:
                    root_properties = system.get('required_properties')
                    root_metadata = system.get('layer_metadata')
                    for part_id, part in parts.items():
                        part_layers = part['source_layers']
                        part_properties = part.get('required_properties')
                        part_metadata = part.get('layer_metadata')
                        if (not isinstance(root_properties, dict) or
                                not isinstance(part_properties, dict) or
                                part_properties != {layer: root_properties.get(layer)
                                                    for layer in part_layers} or
                                not isinstance(root_metadata, dict) or
                                not isinstance(part_metadata, dict) or
                                part_metadata != {layer: root_metadata.get(layer)
                                                 for layer in part_layers}):
                            errors.append(f'{path}: federal_mlrs {part_id} artifact schema/count '
                                          'metadata must match its logical system layers')
                    open_part = _mapping(parts.get('open_ground'))
                    open_inventory = _mapping(
                        open_part.get('source_id_inventory'))
                    open_n = _mapping(
                        _mapping(open_part.get('layer_metadata')).get(
                            'open_ground')).get('n')
                    source_records = open_inventory.get('source_records')
                    unique_ids = open_inventory.get(
                        'maxzoom_unique_tiled_ids')
                    maxzoom_instances = open_inventory.get(
                        'maxzoom_feature_instances')
                    if not (
                            open_inventory.get('status') ==
                            'complete_at_derivation' and
                            _is_count(source_records) and source_records > 0 and
                            source_records == open_n == unique_ids and
                            _is_count(maxzoom_instances) and
                            maxzoom_instances >= source_records and
                            isinstance(open_inventory.get('ids_sha256'), str) and
                            re.fullmatch(
                                r'[0-9a-f]{64}',
                                open_inventory.get('ids_sha256', '')) is not None):
                        errors.append(
                            f'{path}: federal_mlrs open_ground artifact needs '
                            'exact lossless source-section ID reconciliation')
            publication_counts = system.get('source_layer_counts')
            publication_artifact_valid = _validate_release_file(
                system, 'publication_inventory_artifact',
                'publication_inventory_sha256', 'publication_inventory_bytes',
                f'{path}: claim_systems.{system.get("id")} publication inventory',
                errors, {'.json'})
            publication_valid = (
                publication_artifact_valid and
                isinstance(system.get('retrieved'), str) and
                re.fullmatch(r'\d{4}-\d{2}-\d{2}', system.get('retrieved', '')) is not None and
                system.get('complete') is True and
                system.get('truncated') is False and
                isinstance(publication_counts, dict) and
                set(publication_counts) == release_layer_set and
                all(_is_count(value) for value in publication_counts.values()) and
                sum(publication_counts.values()) > 0)
            layer_metadata = system.get('layer_metadata')
            metadata_counts = ({layer: metadata.get('n')
                                for layer, metadata in layer_metadata.items()}
                               if isinstance(layer_metadata, dict) and
                               all(isinstance(metadata, dict) for metadata in
                                   layer_metadata.values()) else None)
            if not publication_valid or metadata_counts != publication_counts:
                errors.append(f'{path}: released {system.get("id")} needs a complete, '
                              'non-truncated publication inventory with exact nonnegative '
                              'source-layer counts matching layer_metadata')
            properties = system.get('required_properties')
            if system.get('id') == 'federal_mlrs':
                required_by_layer = {
                    'active': {'st', 'serial', 'status'},
                    'closed': {'st', 'serial', 'status'},
                    'open_ground': {
                        'st', 'status', 'open_count', 'section_count',
                        'open_fraction', 'mineral_title_status',
                        'mineral_title_source', 'mineral_title_ref',
                        'mineral_title_reviewed'},
                }
                valid = (isinstance(properties, dict) and
                         all(layer in properties and
                             isinstance(properties[layer], list) and
                             fields <= set(properties[layer])
                             for layer, fields in required_by_layer.items()))
                if not valid:
                    errors.append(f'{path}: released federal_mlrs requires typed '
                                  'claim identity/status and open-ground math properties')
            elif system.get('id') == 'alaska_state_claims':
                required_by_layer = {
                    layer: {'st', 'system', 'serial', 'status'}
                    for layer in ('active', 'pending', 'closed')
                }
                valid = (isinstance(properties, dict) and
                         all(layer in properties and
                             isinstance(properties[layer], list) and
                             fields <= set(properties[layer])
                             for layer, fields in required_by_layer.items()))
                if not valid:
                    errors.append(f'{path}: released Alaska state claims require '
                                  'system/serial/status properties on every layer')
    _validate_delivery(row.get('geology'), f'{path}: geology', errors,
                       release=is_release)
    _validate_delivery(row.get('faults'), f'{path}: faults', errors,
                       release=is_release, zero_state=code)
    _validate_delivery(row.get('aeromag'), f'{path}: aeromag', errors, raster=True,
                       release=is_release)
    aeromag = _mapping(row.get('aeromag'))
    if regime == 'non_claim':
        _validate_delivery(row.get('land_context'), f'{path}: land_context', errors,
                           release=is_release)
    if is_release:
        geology = _mapping(row.get('geology'))
        faults = _mapping(row.get('faults'))
        geology_props = geology.get('required_properties')
        fault_props = faults.get('required_properties')
        valid_geology_props = (isinstance(geology_props, list) and
                               all(isinstance(item, str) for item in geology_props))
        valid_fault_props = (isinstance(fault_props, list) and
                             all(isinstance(item, str) for item in fault_props))
        if (not _nonempty_text(geology.get('source_id')) or
                geology.get('best_available_verified') is not True or
                geology.get('source_scale_recorded_per_polygon') is not True or
                not valid_geology_props or
                not {'st', 'source_id', 'source_scale'} <= set(geology_props)):
            errors.append(f'{path}: released geology needs verified best-available source '
                          'and st/source_id/source_scale on every polygon')
        if (not _nonempty_text(faults.get('source_id')) or
                faults.get('best_available_verified') is not True or
                faults.get('source_scale_recorded_per_feature') is not True or
                not valid_fault_props or
                not {'st', 'source_id', 'source_scale'} <= set(fault_props)):
            errors.append(f'{path}: released faults need verified best-available source '
                          'and st/source_id/source_scale on every feature')
        survey_ids = aeromag.get('survey_index_source_ids')
        if (not _nonempty_text(aeromag.get('raster_source')) or
                aeromag.get('survey_index_provenance_required') is not True or
                not isinstance(survey_ids, list) or not survey_ids or
                any(not _nonempty_text(item) for item in survey_ids) or
                len(survey_ids) != len(set(survey_ids))):
            errors.append(f'{path}: released aeromag needs identified survey-index provenance')
        if regime == 'non_claim':
            context = _mapping(row.get('land_context'))
            context_props = context.get('required_properties')
            context_layers = context.get('source_layers')
            required_context_layers = {'land_context', 'target_context'}
            required_context_props = {
                'land_context': {'st', 'surface_class', 'mineral_class', 'approach'},
                'target_context': {
                    'st', 'target_id', 'target_rank', 'score', 'surface_class',
                    'mineral_class', 'approach'},
            }
            valid_context_props = (
                isinstance(context_layers, list) and
                required_context_layers <= set(context_layers) and
                isinstance(context_props, dict) and
                set(context_props) == set(context_layers) and
                all(isinstance(context_props.get(layer), list) and
                    required <= set(context_props[layer])
                    for layer, required in required_context_props.items()))
            metadata = context.get('layer_metadata')
            valid_context_metadata = (
                isinstance(metadata, dict) and set(metadata) == set(context_layers or []) and
                all(isinstance(item, dict) and _is_count(item.get('n')) and
                    item.get('complete') is True and
                    item.get('availability') == 'complete'
                    for item in metadata.values()) and
                metadata.get('land_context', {}).get('n', 0) > 0 and
                metadata.get('target_context', {}).get('n', 0) >= 5)
            if (context.get('mineral_ownership_verified') is not True or
                    context.get('approach_route_required') is not True or
                    not valid_context_props or not valid_context_metadata):
                errors.append(f'{path}: released non-claim land context needs verified '
                              'statewide ownership plus at least five complete per-target '
                              'surface/mineral/approach records')
    aml = _mapping(row.get('aml'))
    aml_url = aml.get('state_inventory_url')
    aml_release_status = aml.get('release_inventory_status')
    if (not _nonempty_text(aml.get('source_id')) or
            not _nonempty_text(aml.get('status')) or
            (aml_url is not None and not _https_url(aml_url)) or
            aml_release_status not in
            ('pending_review', 'ingested_complete', 'documented_unavailable') or
            not any(_nonempty_text(aml.get(field), 20)
                    for field in ('evidence', 'scope', 'gap'))):
        errors.append(f'{path}: AML registry needs a source/status, official HTTPS URL '
                      'when available, and an explicit scope/evidence/gap finding')
    trust = _mapping(row.get('trust_land'))
    offered = trust.get('mineral_leasing_offered')
    offering_class = trust.get('offering_class')
    trust_release_status = trust.get('release_inventory_status')
    if (not (isinstance(offered, bool) or _nonempty_text(offered)) or
            not _https_url(trust.get('portal_url')) or
            offering_class not in ('unknown', 'offered', 'limited', 'not_offered') or
            trust_release_status not in
            ('pending_review', 'ingested_complete', 'documented_unavailable',
             'not_applicable') or
            not _nonempty_text(trust.get('evidence'), 30)):
        errors.append(f'{path}: trust-land registry needs a typed offering finding, '
                      'official HTTPS portal, and substantive evidence')
    if is_release and regime == 'non_claim':
        aml_evidence_valid = _validate_release_file(
            aml, 'evidence_artifact', 'evidence_sha256', 'evidence_bytes',
            f'{path}: AML decision evidence', errors, {'.json'})
        if aml_release_status == 'pending_review':
            errors.append(f'{path}: released non-claim state cannot leave AML inventory pending')
        elif aml_release_status == 'ingested_complete':
            _validate_delivery(aml, f'{path}: aml', errors, release=True)
            aml_properties = aml.get('required_properties')
            if (aml.get('source_layers') != ['aml'] or
                    not isinstance(aml_properties, list) or
                    not {'st', 'source_id', 'status'} <= set(aml_properties)):
                errors.append(f'{path}: released AML inventory needs canonical aml layer '
                              'with state/source/status properties')
        elif (aml_release_status == 'documented_unavailable' and
              not aml_evidence_valid):
            errors.append(f'{path}: unavailable AML inventory needs an evidence artifact')

        trust_evidence_valid = _validate_release_file(
            trust, 'evidence_artifact', 'evidence_sha256', 'evidence_bytes',
            f'{path}: trust-land decision evidence', errors, {'.json'})
        if offering_class == 'unknown':
            errors.append(f'{path}: released non-claim state needs a reviewed trust-land '
                          'mineral-leasing offering class')
        if trust_release_status == 'pending_review':
            errors.append(f'{path}: released non-claim state cannot leave trust-land '
                          'inventory pending')
        elif trust_release_status == 'ingested_complete':
            if offering_class not in ('offered', 'limited'):
                errors.append(f'{path}: ingested trust-land inventory requires an offered '
                              'or limited leasing program')
            _validate_delivery(trust, f'{path}: trust_land', errors, release=True)
            trust_properties = trust.get('required_properties')
            if (trust.get('source_layers') != ['trust_land'] or
                    not isinstance(trust_properties, list) or
                    not {'st', 'mineral_class', 'approach'} <= set(trust_properties)):
                errors.append(f'{path}: released trust-land inventory needs canonical '
                              'trust_land layer with ownership/approach properties')
        elif trust_release_status in ('documented_unavailable', 'not_applicable'):
            if not trust_evidence_valid:
                errors.append(f'{path}: non-tiled trust-land finding needs an evidence artifact')
            if (offering_class in ('offered', 'limited') and
                    trust_release_status == 'not_applicable'):
                errors.append(f'{path}: an offered/limited trust-land program cannot be '
                              'marked not applicable')
        if not trust_evidence_valid:
            errors.append(f'{path}: released trust-land decision needs an evidence artifact')
    survey = _mapping(row.get('geological_survey'))
    for key in ('name', 'catalog_url', 'gis_endpoints'):
        if key not in survey:
            errors.append(f'{path}: geological_survey.{key} is required')
    if (not _nonempty_text(survey.get('name')) or
            not _https_url(survey.get('catalog_url')) or
            not isinstance(survey.get('gis_endpoints'), list) or
            not survey.get('gis_endpoints') or
            any(not isinstance(item, dict) or
                not _https_url(item.get('url'))
                for item in (survey.get('gis_endpoints') or []))):
        errors.append(f'{path}: geological survey needs a name, HTTPS catalog, and valid GIS endpoints')
    serials = row.get('historic_serials')
    if not isinstance(serials, list):
        errors.append(f'{path}: historic_serials must be a list')
        serials = []
    if not any(s.get('source_id') == 'pp610' for s in serials if isinstance(s, dict)):
        errors.append(f'{path}: historic_serials must include PP 610 as the district anchor')
    recorder = _mapping(row.get('recorder'))
    recorder_type = 'recording_district' if code == 'AK' else 'county'
    recorder_scope = ('recording_districts_with_live_claims' if code == 'AK'
                      else 'counties_with_live_claims')
    if (recorder.get('scope') != recorder_scope or
            recorder.get('jurisdiction_type') != recorder_type):
        errors.append(f'{path}: recorder must use {recorder_type} jurisdictions and '
                      f'scope={recorder_scope}')
    raw_gates = row.get('done_gate')
    if not isinstance(raw_gates, dict):
        errors.append(f'{path}: done_gate must be an object')
        gates = {}
    else:
        gates = raw_gates
    if set(gates) != set(GATE_KEYS):
        missing = sorted(set(GATE_KEYS) - set(gates))
        extra = sorted(set(gates) - set(GATE_KEYS))
        errors.append(f'{path}: done_gate keys mismatch; missing={missing}, extra={extra}')
    for key, gate in gates.items():
        if not isinstance(gate, dict) or gate.get('status') not in GATE_STATUSES:
            errors.append(f'{path}: done_gate.{key}.status is invalid')
        if isinstance(gate, dict) and not _nonempty_text(gate.get('evidence'), 8):
            errors.append(f'{path}: done_gate.{key} needs explicit textual evidence')
        if isinstance(gate, dict) and gate.get('status') == 'not_applicable':
            allowed_na = regime == 'non_claim' and key == 'recorders'
            if not allowed_na:
                errors.append(f'{path}: done_gate.{key} cannot be not_applicable for {regime}')
    done = (set(gates) == set(GATE_KEYS) and
            all(isinstance(gate, dict) and
                gate.get('status') in ('pass', 'not_applicable')
                for gate in gates.values()))
    if release.get('status') == 'done' and not done:
        errors.append(f'{path}: release.status=done but one or more DONE gates do not pass')
    if release.get('enabled') is True and not done:
        errors.append(f'{path}: release.enabled=true but one or more DONE gates do not pass')
    if is_release:
        _validate_release_acceptance(row, path, errors)
    if errors:
        raise RegistryError('\n'.join(errors))
    return errors


def validate_registry() -> dict:
    errors = []
    found = {
        os.path.splitext(name)[0]
        for name in os.listdir(STATES_DIR)
        if re.fullmatch(r'[A-Z]{2}\.yaml', name)
    }
    missing = sorted(ALL_STATES - found)
    extra = sorted(found - ALL_STATES)
    if missing:
        errors.append(f'missing state files: {", ".join(missing)}')
    if extra:
        errors.append(f'unexpected state files: {", ".join(extra)}')
    rows = {}
    for code in sorted(ALL_STATES & found):
        try:
            rows[code] = load_state(code)
        except RegistryError as exc:
            errors.append(str(exc))
    if {c for c, r in rows.items() if r['regime'] == 'claim'} != set(CLAIM_STATES):
        errors.append('claim-state set differs from the foundational WS11 set')
    # One content-addressed path is one immutable object even when multiple
    # released states share it. Per-state validation pins every descriptor;
    # this registry-wide pass rejects mutually impossible byte metadata.
    shared_release_files = {}
    for code, row in sorted(rows.items()):
        if _mapping(row.get('release')).get('enabled') is not True:
            continue
        for (context, path_container, path_field, metadata_container,
             sha_field, bytes_field) in release_file_descriptors(row):
            artifact = path_container.get(path_field)
            descriptor = (metadata_container.get(sha_field),
                          metadata_container.get(bytes_field))
            previous = shared_release_files.setdefault(
                artifact, (descriptor, f'{code}.{context}'))
            if previous[0] != descriptor:
                errors.append(
                    f'{artifact}: conflicting release checksum/bytes between '
                    f'{previous[1]} and {code}.{context}')
    return {'ok': not errors, 'states': len(rows), 'claim_states': len(CLAIM_STATES),
            'non_claim_states': len(NON_CLAIM_STATES), 'errors': errors}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('command', choices=('validate', 'show', 'list'))
    ap.add_argument('state', nargs='?')
    args = ap.parse_args(argv)
    if args.command == 'validate':
        out = validate_registry()
        print(json.dumps(out, indent=2))
        return 0 if out['ok'] else 1
    if args.command == 'show':
        if not args.state:
            ap.error('show requires a state code')
        print(json.dumps(load_state(args.state), indent=2))
        return 0
    rows = load_states()
    print('\n'.join(f'{code}\t{row["regime"]}\tP{row["phase"]}\t{row["name"]}'
                    for code, row in rows.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
