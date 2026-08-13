#!/usr/bin/env python3
"""Compile reviewed WS11 target-scoring evidence for the exact 49 states.

This compiler is intentionally isolated from releases.  It consumes four
checksum-pinned, privately staged inputs for every state and writes immutable
JSON evidence only.  It does not derive targets from browser data, invent
missing land status, edit the state registry, update a manifest, or toggle a
release.

Claim-state totals contain grade + geology + measured open-ground terms.
Non-claim totals contain grade + geology; their open-ground term is the typed
value ``not_applicable``/``null``/``N/A`` and every target instead carries an
independently evidenced surface/mineral/approach card.  The deterministic sort
keeps that legal N/A distinct from measured numeric zero.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from urllib.parse import urlsplit

from state_registry import load_states, validate_registry


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
DATASET = 'ws11-national-target-scoring-evidence'
EFFECT = 'evidence_only_no_release_mutation'

ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
SHA_RE = re.compile(r'[0-9a-f]{64}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')

SURFACE_CLASSES = frozenset(
    ('federal', 'state', 'private', 'tribal', 'local', 'mixed', 'unknown'))
MINERAL_CLASSES = frozenset(
    ('federal', 'state', 'private', 'tribal', 'local', 'split_estate',
     'mixed', 'unknown'))
MINERAL_CONFIDENCE = frozenset(('verified', 'probable', 'unknown'))
APPROACH_KINDS = frozenset((
    'state_lease', 'private_negotiation', 'federal_leasing_agency',
    'tribal_mineral_authority', 'multiple_rightsholders',
    'title_research_required', 'no_available_route',
))

SORT_POLICY = {
    'score': 'descending_total',
    'open_ground_tie_break': [
        'positive_measured', 'not_applicable', 'measured_zero'],
    'area_km2': 'descending',
    'target_id': 'ascending',
    'not_applicable_is_numeric_zero': False,
}


class PublicationError(ValueError):
    """An input or compiled artifact violates the scoring-evidence contract."""


def _reject_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def strict_json_bytes(raw, label):
    try:
        return json.loads(
            raw, parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PublicationError(f'{label} is not strict JSON: {exc}') from exc


def load_strict_json(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot read {label}: {exc}') from exc
    return strict_json_bytes(raw, label), raw


def canonical_bytes(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PublicationError(f'value is not canonical JSON: {exc}') from exc


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _expect_object(value, label):
    if not isinstance(value, dict):
        raise PublicationError(f'{label} must be an object')
    return value


def _expect_list(value, label, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = 'a non-empty list' if nonempty else 'a list'
        raise PublicationError(f'{label} must be {qualifier}')
    return value


def _expect_keys(value, required, optional, label):
    _expect_object(value, label)
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise PublicationError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def _text(value, label, *, minimum=1, maximum=5000):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value or
            '\r' in value):
        raise PublicationError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def _identifier(value, label):
    value = _text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be a stable identifier')
    return value


def _sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be a lowercase SHA-256')
    return value


def _date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be YYYY-MM-DD')
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise PublicationError(f'{label} is not a calendar date') from exc
    return value


def _https(value, label):
    value = _text(value, label, maximum=2048)
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or
            parsed.password or parsed.fragment or
            any(character.isspace() for character in value)):
        raise PublicationError(
            f'{label} must be an HTTPS URL without credentials or fragment')
    return value


def _integer(value, label, minimum=0):
    if (not isinstance(value, int) or isinstance(value, bool) or
            value < minimum):
        raise PublicationError(f'{label} must be an integer >= {minimum}')
    return value


def _number(value, label, *, minimum=0.0, maximum=None):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value < minimum or
            (maximum is not None and value > maximum)):
        upper = '' if maximum is None else f' and <= {maximum}'
        raise PublicationError(
            f'{label} must be finite and >= {minimum}{upper}')
    return float(value)


def _inside(path, parent):
    try:
        return os.path.commonpath((os.path.realpath(path),
                                   os.path.realpath(parent))) == os.path.realpath(parent)
    except ValueError:
        return False


def _assert_private(path, label):
    if _inside(path, SITE):
        raise PublicationError(
            f'{label} is inside public site/; raw scoring staging must remain private')
    if os.path.islink(path):
        raise PublicationError(f'{label} must not be a symlink')


def _review(value, label, snapshot):
    _expect_keys(value, ('status', 'reviewed_on', 'reviewed_by'), (), label)
    if value['status'] != 'reviewed':
        raise PublicationError(f'{label}.status must be reviewed')
    reviewed_on = _date(value['reviewed_on'], f'{label}.reviewed_on')
    if reviewed_on > snapshot:
        raise PublicationError(f'{label}.reviewed_on cannot be after snapshot')
    return {
        'status': 'reviewed',
        'reviewed_on': reviewed_on,
        'reviewed_by': _text(
            value['reviewed_by'], f'{label}.reviewed_by', minimum=3,
            maximum=300),
    }


def _sha_list(value, label, *, nonempty=True):
    rows = _expect_list(value, label, nonempty=nonempty)
    normalized = [_sha(item, f'{label}[{index}]')
                  for index, item in enumerate(rows)]
    if len(set(normalized)) != len(normalized):
        raise PublicationError(f'{label} contains duplicate SHA-256 references')
    return sorted(normalized)


def _text_list(value, label, *, nonempty=True, maximum_items=100):
    rows = _expect_list(value, label, nonempty=nonempty)
    if len(rows) > maximum_items:
        raise PublicationError(f'{label} exceeds {maximum_items} entries')
    normalized = [
        _text(item, f'{label}[{index}]', maximum=300)
        for index, item in enumerate(rows)
    ]
    folded = [item.casefold() for item in normalized]
    if len(set(folded)) != len(folded):
        raise PublicationError(f'{label} contains duplicate terms')
    return sorted(normalized, key=lambda item: (item.casefold(), item))


def _source_urls(value, label):
    rows = _expect_list(value, label, nonempty=True)
    normalized = [_https(item, f'{label}[{index}]')
                  for index, item in enumerate(rows)]
    if len(set(normalized)) != len(normalized):
        raise PublicationError(f'{label} contains duplicate URLs')
    return sorted(normalized)


def _canonical_name(value):
    return re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()


@dataclass(frozen=True)
class Artifact:
    label: str
    relative_path: str
    path: str
    bytes: int
    sha256: str


def _artifact_descriptor(value, staging_root, label):
    _expect_keys(value, ('path', 'bytes', 'sha256'), (), label)
    relative = _text(value['path'], f'{label}.path', maximum=500)
    parts = relative.replace('\\', '/').split('/')
    if (os.path.isabs(relative) or relative.startswith(('/', '\\')) or
            any(part in ('', '.', '..') for part in parts)):
        raise PublicationError(f'{label}.path must be a normalized relative path')
    size = _integer(value['bytes'], f'{label}.bytes', minimum=1)
    expected_sha = _sha(value['sha256'], f'{label}.sha256')
    unresolved = os.path.abspath(os.path.join(staging_root, relative))
    path = os.path.realpath(unresolved)
    if unresolved != path:
        raise PublicationError(f'{label}.path must not traverse a symlink')
    if not _inside(path, staging_root):
        raise PublicationError(f'{label}.path escapes private staging')
    _assert_private(path, label)
    try:
        actual_size = os.path.getsize(path)
        actual_sha = sha256_file(path)
    except OSError as exc:
        raise PublicationError(f'{label} artifact is unreadable: {exc}') from exc
    if actual_size != size or actual_sha != expected_sha:
        raise PublicationError(
            f'{label} checksum/size mismatch: expected {size}/{expected_sha}, '
            f'got {actual_size}/{actual_sha}')
    return Artifact(label, relative, path, size, expected_sha)


def _registry_context():
    checked = validate_registry()
    if not checked.get('ok'):
        raise PublicationError(
            'state registry is invalid: ' + '; '.join(checked.get('errors') or []))
    states = load_states()
    if len(states) != 49:
        raise PublicationError(f'WS11 registry must contain exactly 49 states, got {len(states)}')
    rows = {}
    for code, state in states.items():
        regime = state['regime']
        systems = sorted(
            item['id'] for item in (state.get('claim_systems') or [])
            if isinstance(item, dict) and isinstance(item.get('id'), str))
        if regime == 'claim' and not systems:
            raise PublicationError(f'{code} claim registry has no claim systems')
        if regime == 'non_claim' and systems:
            raise PublicationError(f'{code} non-claim registry declares claim systems')
        rows[code] = {
            'name': state['name'],
            'regime': regime,
            'phase': state['phase'],
            'claim_systems': systems,
        }
    return rows, sha256_bytes(canonical_bytes(rows))


def _common_document(document, code, kind, method_id, snapshot, required_extra):
    required = (
        'schema_version', 'state', 'kind', 'complete', 'truncated',
        'method_id', 'review', 'source_urls', *required_extra)
    _expect_keys(document, required, (), f'{code}.{kind}')
    if (document['schema_version'] != 1 or document['state'] != code or
            document['kind'] != kind):
        raise PublicationError(f'{code}.{kind} identity is invalid')
    if document['complete'] is not True or document['truncated'] is not False:
        raise PublicationError(f'{code}.{kind} must be complete and not truncated')
    if document['method_id'] != method_id:
        raise PublicationError(f'{code}.{kind}.method_id does not match inventory')
    _review(document['review'], f'{code}.{kind}.review', snapshot)
    _source_urls(document['source_urls'], f'{code}.{kind}.source_urls')


def validate_ranked_targets(document, code, method_id, snapshot):
    kind = 'ranked_targets'
    _common_document(document, code, kind, method_id, snapshot, ('targets',))
    rows = _expect_list(document['targets'], f'{code}.{kind}.targets', nonempty=True)
    if len(rows) > 100000:
        raise PublicationError(f'{code}.{kind} exceeds 100000 targets')
    by_id = {}
    ranks = set()
    logical = set()
    ordered_ranks = []
    for index, row in enumerate(rows):
        label = f'{code}.{kind}.targets[{index}]'
        _expect_keys(row, (
            'target_id', 'name', 'declared_rank', 'area_km2',
            'longitude', 'latitude'), ('district',), label)
        target_id = _identifier(row['target_id'], f'{label}.target_id')
        name = _text(row['name'], f'{label}.name', maximum=300)
        rank = _integer(row['declared_rank'], f'{label}.declared_rank', minimum=1)
        area = _number(row['area_km2'], f'{label}.area_km2', minimum=0)
        longitude = _number(
            row['longitude'], f'{label}.longitude', minimum=-180, maximum=180)
        latitude = _number(
            row['latitude'], f'{label}.latitude', minimum=-90, maximum=90)
        identity = (_canonical_name(name), longitude, latitude)
        if target_id in by_id or rank in ranks or identity in logical:
            raise PublicationError(
                f'{code}.{kind} contains duplicate target, rank, or name/location')
        normalized = {
            'target_id': target_id,
            'name': name,
            'declared_rank': rank,
            'area_km2': area,
            'longitude': longitude,
            'latitude': latitude,
        }
        if 'district' in row:
            normalized['district'] = _text(
                row['district'], f'{label}.district', maximum=300)
        by_id[target_id] = normalized
        ranks.add(rank)
        logical.add(identity)
        ordered_ranks.append(rank)
    expected = list(range(1, len(rows) + 1))
    if ordered_ranks != expected:
        raise PublicationError(
            f'{code}.{kind} must be stored in contiguous declared rank order 1..{len(rows)}')
    return by_id


def validate_terms(document, code, kind, method_id, snapshot):
    _common_document(
        document, code, kind, method_id, snapshot,
        ('source_artifact_sha256', 'targets'))
    source_sha = _sha(
        document['source_artifact_sha256'],
        f'{code}.{kind}.source_artifact_sha256')
    rows = _expect_list(document['targets'], f'{code}.{kind}.targets', nonempty=True)
    by_id = {}
    for index, row in enumerate(rows):
        label = f'{code}.{kind}.targets[{index}]'
        _expect_keys(
            row, ('target_id', 'score', 'terms', 'evidence_refs', 'rationale'),
            (), label)
        target_id = _identifier(row['target_id'], f'{label}.target_id')
        if target_id in by_id:
            raise PublicationError(f'{code}.{kind} duplicates target_id {target_id}')
        evidence = _sha_list(row['evidence_refs'], f'{label}.evidence_refs')
        if source_sha not in evidence:
            raise PublicationError(
                f'{label}.evidence_refs must include source_artifact_sha256')
        by_id[target_id] = {
            'score': _number(row['score'], f'{label}.score', maximum=100),
            'terms': _text_list(row['terms'], f'{label}.terms'),
            'evidence_refs': evidence,
            'rationale': _text(
                row['rationale'], f'{label}.rationale', minimum=12),
        }
    return by_id


def validate_open_ground(document, code, method_id, snapshot, claim_systems):
    kind = 'open_ground'
    _common_document(document, code, kind, method_id, snapshot, (
        'coverage_status', 'all_ranked_targets_covered',
        'source_snapshot_sha256s', 'targets'))
    if (document['coverage_status'] != 'statewide_complete' or
            document['all_ranked_targets_covered'] is not True):
        raise PublicationError(
            f'{code}.{kind} must declare complete statewide/all-target coverage')
    snapshots = _expect_object(
        document['source_snapshot_sha256s'],
        f'{code}.{kind}.source_snapshot_sha256s')
    if set(snapshots) != set(claim_systems):
        raise PublicationError(
            f'{code}.{kind} must cover exact registry claim systems: '
            f'expected={claim_systems}, got={sorted(snapshots)}')
    normalized_snapshots = {
        system: _sha(digest, f'{code}.{kind}.source_snapshot_sha256s.{system}')
        for system, digest in snapshots.items()
    }
    allowed_evidence = set(normalized_snapshots.values())
    rows = _expect_list(document['targets'], f'{code}.{kind}.targets', nonempty=True)
    by_id = {}
    for index, row in enumerate(rows):
        label = f'{code}.{kind}.targets[{index}]'
        _expect_keys(row, (
            'target_id', 'status', 'value', 'unit', 'score',
            'evidence_refs', 'rationale'), (), label)
        target_id = _identifier(row['target_id'], f'{label}.target_id')
        if target_id in by_id:
            raise PublicationError(f'{code}.{kind} duplicates target_id {target_id}')
        if row['status'] != 'measured' or row['unit'] != 'fraction':
            raise PublicationError(
                f'{label} must use measured open ground in fraction units')
        evidence = _sha_list(row['evidence_refs'], f'{label}.evidence_refs')
        if set(evidence) != allowed_evidence:
            raise PublicationError(
                f'{label}.evidence_refs must include every pinned claim-system '
                'snapshot exactly once')
        by_id[target_id] = {
            'status': 'measured',
            'value': _number(row['value'], f'{label}.value', maximum=1),
            'unit': 'fraction',
            'score': _number(row['score'], f'{label}.score', maximum=100),
            'evidence_refs': evidence,
            'rationale': _text(
                row['rationale'], f'{label}.rationale', minimum=12),
        }
    return by_id


def _ownership(value, label, allowed_classes, expected_sha, *, mineral=False):
    required = ('class', 'party', 'evidence_sha256')
    if mineral:
        required += ('confidence',)
    _expect_keys(value, required, (), label)
    ownership_class = value['class']
    if ownership_class not in allowed_classes:
        raise PublicationError(f'{label}.class is invalid')
    evidence_sha = _sha(value['evidence_sha256'], f'{label}.evidence_sha256')
    if evidence_sha != expected_sha:
        raise PublicationError(f'{label} is not bound to its staged evidence snapshot')
    result = {
        'class': ownership_class,
        'party': _text(value['party'], f'{label}.party', maximum=300),
        'evidence_sha256': evidence_sha,
    }
    if mineral:
        if value['confidence'] not in MINERAL_CONFIDENCE:
            raise PublicationError(f'{label}.confidence is invalid')
        result['confidence'] = value['confidence']
    return result


def _approach(value, label, expected_sha):
    _expect_keys(
        value, ('kind', 'party', 'portal_url', 'evidence_sha256'), (), label)
    if value['kind'] not in APPROACH_KINDS:
        raise PublicationError(f'{label}.kind is invalid')
    evidence_sha = _sha(value['evidence_sha256'], f'{label}.evidence_sha256')
    if evidence_sha != expected_sha:
        raise PublicationError(f'{label} is not bound to its leasing/title snapshot')
    portal = value['portal_url']
    if portal is not None:
        portal = _https(portal, f'{label}.portal_url')
    return {
        'kind': value['kind'],
        'party': _text(value['party'], f'{label}.party', maximum=300),
        'portal_url': portal,
        'evidence_sha256': evidence_sha,
    }


def validate_land_context(document, code, method_id, snapshot):
    kind = 'land_context'
    _common_document(document, code, kind, method_id, snapshot, (
        'coverage_status', 'source_snapshot_sha256s', 'targets'))
    if document['coverage_status'] != 'per_target_complete':
        raise PublicationError(
            f'{code}.{kind}.coverage_status must be per_target_complete')
    snapshots = _expect_object(
        document['source_snapshot_sha256s'],
        f'{code}.{kind}.source_snapshot_sha256s')
    expected_keys = {'surface', 'mineral', 'leasing_or_title'}
    if set(snapshots) != expected_keys:
        raise PublicationError(
            f'{code}.{kind}.source_snapshot_sha256s must contain '
            'surface, mineral, and leasing_or_title')
    snapshots = {
        key: _sha(value, f'{code}.{kind}.source_snapshot_sha256s.{key}')
        for key, value in snapshots.items()
    }
    rows = _expect_list(document['targets'], f'{code}.{kind}.targets', nonempty=True)
    by_id = {}
    for index, row in enumerate(rows):
        label = f'{code}.{kind}.targets[{index}]'
        _expect_keys(row, (
            'target_id', 'open_ground', 'surface', 'mineral', 'approach',
            'rationale'), (), label)
        target_id = _identifier(row['target_id'], f'{label}.target_id')
        if target_id in by_id:
            raise PublicationError(f'{code}.{kind} duplicates target_id {target_id}')
        open_term = row['open_ground']
        _expect_keys(
            open_term, ('status', 'value', 'unit', 'score', 'display'), (),
            f'{label}.open_ground')
        if open_term != {
                'status': 'not_applicable', 'value': None, 'unit': None,
                'score': None, 'display': 'N/A'}:
            raise PublicationError(
                f'{label}.open_ground must be typed not_applicable/null/N/A, '
                'never numeric zero')
        by_id[target_id] = {
            'surface': _ownership(
                row['surface'], f'{label}.surface', SURFACE_CLASSES,
                snapshots['surface']),
            'mineral': _ownership(
                row['mineral'], f'{label}.mineral', MINERAL_CLASSES,
                snapshots['mineral'], mineral=True),
            'approach': _approach(
                row['approach'], f'{label}.approach',
                snapshots['leasing_or_title']),
            'rationale': _text(
                row['rationale'], f'{label}.rationale', minimum=12),
        }
    return by_id


def _same_targets(code, ranked, **components):
    expected = set(ranked)
    for name, rows in components.items():
        if set(rows) != expected:
            missing = sorted(expected - set(rows))
            extra = sorted(set(rows) - expected)
            raise PublicationError(
                f'{code}.{name} target join mismatch: missing={missing}, extra={extra}')


def _percent(value):
    rendered = f'{value * 100:.6f}'.rstrip('0').rstrip('.')
    return f'{rendered}%'


def target_sort_key(target):
    """Return the canonical richOpen key without coercing N/A to zero."""
    score = target.get('score') or {}
    total = score.get('total')
    open_term = target.get('open_ground') or {}
    status = open_term.get('status')
    component = open_term.get('score')
    if (status == 'measured' and isinstance(component, (int, float)) and
            not isinstance(component, bool) and component > 0):
        evidence_rank = 0
    elif status == 'not_applicable':
        evidence_rank = 1
    elif status == 'measured' and component == 0:
        evidence_rank = 2
    else:
        evidence_rank = 3
    return (-total, evidence_rank, -target['area_km2'], target['target_id'])


def _compile_state(code, registry_row, registry_sha, inventory_sha, snapshot,
                   method_id, artifacts, documents):
    ranked = validate_ranked_targets(
        documents['ranked_targets'], code, method_id, snapshot)
    grade = validate_terms(
        documents['grade_terms'], code, 'grade_terms', method_id, snapshot)
    geology = validate_terms(
        documents['geology_terms'], code, 'geology_terms', method_id, snapshot)
    regime = registry_row['regime']
    if regime == 'claim':
        regime_rows = validate_open_ground(
            documents['open_ground'], code, method_id, snapshot,
            registry_row['claim_systems'])
        _same_targets(
            code, ranked, grade_terms=grade, geology_terms=geology,
            open_ground=regime_rows)
    else:
        regime_rows = validate_land_context(
            documents['land_context'], code, method_id, snapshot)
        _same_targets(
            code, ranked, grade_terms=grade, geology_terms=geology,
            land_context=regime_rows)

    targets = []
    for target_id, target in ranked.items():
        grade_row = grade[target_id]
        geology_row = geology[target_id]
        if regime == 'claim':
            measured = regime_rows[target_id]
            open_term = dict(
                measured, display=_percent(measured['value']))
            land_context = None
            total = grade_row['score'] + geology_row['score'] + measured['score']
        else:
            open_term = {
                'status': 'not_applicable', 'value': None, 'unit': None,
                'score': None, 'display': 'N/A',
            }
            land_context = regime_rows[target_id]
            total = grade_row['score'] + geology_row['score']
        row = {
            'target_id': target_id,
            'name': target['name'],
            'rank': target['declared_rank'],
            'area_km2': target['area_km2'],
            'location': {
                'longitude': target['longitude'],
                'latitude': target['latitude'],
            },
            'score': {
                'total': round(total, 8),
                'grade': grade_row['score'],
                'geology': geology_row['score'],
                'open_ground': open_term['score'],
            },
            'grade': grade_row,
            'geology': geology_row,
            'open_ground': open_term,
            'land_context': land_context,
        }
        if 'district' in target:
            row['district'] = target['district']
        targets.append(row)
    targets.sort(key=target_sort_key)
    computed_ids = [row['target_id'] for row in targets]
    declared_ids = [
        row['target_id'] for row in
        sorted(ranked.values(), key=lambda row: row['declared_rank'])]
    if computed_ids != declared_ids:
        raise PublicationError(
            f'{code}.ranked_targets declared ranks disagree with deterministic '
            'grade/geology/regime scoring order')
    for rank, target in enumerate(targets, 1):
        target['rank'] = rank

    metrics = {
        'targets': len(targets),
        'measured_open_ground': sum(
            row['open_ground']['status'] == 'measured' for row in targets),
        'measured_zero_open_ground': sum(
            row['open_ground']['status'] == 'measured' and
            row['open_ground']['score'] == 0 for row in targets),
        'not_applicable_open_ground': sum(
            row['open_ground']['status'] == 'not_applicable' for row in targets),
        'land_context_cards': sum(row['land_context'] is not None for row in targets),
    }
    if regime == 'claim':
        regime_evidence = {
            'open_ground': {
                'status': 'measured',
                'coverage_status': documents['open_ground']['coverage_status'],
                'all_ranked_targets_covered': documents['open_ground'][
                    'all_ranked_targets_covered'],
                'source_snapshot_sha256s': {
                    key: documents['open_ground']['source_snapshot_sha256s'][key]
                    for key in sorted(
                        documents['open_ground']['source_snapshot_sha256s'])
                },
            },
            'land_context': None,
        }
    else:
        regime_evidence = {
            'open_ground': {
                'status': 'not_applicable', 'value': None, 'unit': None,
                'score': None, 'display': 'N/A',
            },
            'land_context': {
                'coverage_status': documents['land_context']['coverage_status'],
                'source_snapshot_sha256s': {
                    key: documents['land_context']['source_snapshot_sha256s'][key]
                    for key in sorted(
                        documents['land_context']['source_snapshot_sha256s'])
                },
            },
        }
    state_document = {
        'schema_version': 1,
        'dataset': DATASET,
        'snapshot': snapshot,
        'state': code,
        'state_name': registry_row['name'],
        'regime': regime,
        'registry_sha256': registry_sha,
        'inventory_sha256': inventory_sha,
        'method_id': method_id,
        'input_artifacts': {
            name: {'bytes': artifact.bytes, 'sha256': artifact.sha256}
            for name, artifact in sorted(artifacts.items())
        },
        'regime_evidence': regime_evidence,
        'sort_policy': SORT_POLICY,
        'metrics': metrics,
        'targets': targets,
        'effect': EFFECT,
    }
    validate_compiled_state_document(
        state_document, code, registry_row, registry_sha)
    return state_document


def _validate_compiled_term(value, label):
    _expect_keys(value, ('score', 'terms', 'evidence_refs', 'rationale'), (), label)
    score = _number(value['score'], f'{label}.score', maximum=100)
    terms = _text_list(value['terms'], f'{label}.terms')
    evidence = _sha_list(value['evidence_refs'], f'{label}.evidence_refs')
    rationale = _text(value['rationale'], f'{label}.rationale', minimum=12)
    if value != {
            'score': score, 'terms': terms, 'evidence_refs': evidence,
            'rationale': rationale}:
        raise PublicationError(f'{label} is not canonical')
    return score


def _validate_compiled_context(value, label, expected_snapshots=None):
    _expect_keys(value, ('surface', 'mineral', 'approach', 'rationale'), (), label)
    surface = value['surface']
    mineral = value['mineral']
    approach = value['approach']
    _expect_keys(surface, ('class', 'party', 'evidence_sha256'), (), f'{label}.surface')
    _expect_keys(
        mineral, ('class', 'party', 'evidence_sha256', 'confidence'), (),
        f'{label}.mineral')
    _expect_keys(
        approach, ('kind', 'party', 'portal_url', 'evidence_sha256'), (),
        f'{label}.approach')
    if surface['class'] not in SURFACE_CLASSES:
        raise PublicationError(f'{label}.surface.class is invalid')
    if mineral['class'] not in MINERAL_CLASSES:
        raise PublicationError(f'{label}.mineral.class is invalid')
    if mineral['confidence'] not in MINERAL_CONFIDENCE:
        raise PublicationError(f'{label}.mineral.confidence is invalid')
    if approach['kind'] not in APPROACH_KINDS:
        raise PublicationError(f'{label}.approach.kind is invalid')
    for name, row in (('surface', surface), ('mineral', mineral),
                      ('approach', approach)):
        _text(row['party'], f'{label}.{name}.party', maximum=300)
        _sha(row['evidence_sha256'], f'{label}.{name}.evidence_sha256')
    if expected_snapshots is not None:
        expected = {
            'surface': expected_snapshots['surface'],
            'mineral': expected_snapshots['mineral'],
            'approach': expected_snapshots['leasing_or_title'],
        }
        for name, digest in expected.items():
            if value[name]['evidence_sha256'] != digest:
                raise PublicationError(
                    f'{label}.{name} is not bound to regime_evidence snapshots')
    if approach['portal_url'] is not None:
        _https(approach['portal_url'], f'{label}.approach.portal_url')
    _text(value['rationale'], f'{label}.rationale', minimum=12)


def validate_compiled_state_document(document, code, registry_row=None,
                                     registry_sha256=None):
    _expect_keys(document, (
        'schema_version', 'dataset', 'snapshot', 'state', 'state_name',
        'regime', 'registry_sha256', 'inventory_sha256', 'method_id',
        'input_artifacts', 'regime_evidence', 'sort_policy', 'metrics',
        'targets', 'effect'), (),
        f'{code} compiled scoring evidence')
    if (document['schema_version'] != 1 or document['dataset'] != DATASET or
            document['state'] != code or document['effect'] != EFFECT):
        raise PublicationError(f'{code} compiled scoring identity is invalid')
    _date(document['snapshot'], f'{code}.snapshot')
    _identifier(document['method_id'], f'{code}.method_id')
    _sha(document['inventory_sha256'], f'{code}.inventory_sha256')
    _sha(document['registry_sha256'], f'{code}.registry_sha256')
    if registry_row is not None and (
            document['state_name'] != registry_row['name'] or
            document['regime'] != registry_row['regime']):
        raise PublicationError(f'{code} compiled scoring registry identity is stale')
    if registry_sha256 is not None and document['registry_sha256'] != registry_sha256:
        raise PublicationError(f'{code} compiled scoring registry checksum is stale')
    regime = document['regime']
    if regime not in ('claim', 'non_claim'):
        raise PublicationError(f'{code}.regime is invalid')
    expected_inputs = {
        'ranked_targets', 'grade_terms', 'geology_terms',
        'open_ground' if regime == 'claim' else 'land_context'}
    artifacts = _expect_object(document['input_artifacts'], f'{code}.input_artifacts')
    if set(artifacts) != expected_inputs:
        raise PublicationError(f'{code}.input_artifacts do not match its regime')
    for name, descriptor in artifacts.items():
        _expect_keys(descriptor, ('bytes', 'sha256'), (), f'{code}.input_artifacts.{name}')
        _integer(descriptor['bytes'], f'{code}.input_artifacts.{name}.bytes', minimum=1)
        _sha(descriptor['sha256'], f'{code}.input_artifacts.{name}.sha256')
    regime_evidence = _expect_object(
        document['regime_evidence'], f'{code}.regime_evidence')
    _expect_keys(
        regime_evidence, ('open_ground', 'land_context'), (),
        f'{code}.regime_evidence')
    if regime == 'claim':
        open_summary = regime_evidence['open_ground']
        _expect_keys(open_summary, (
            'status', 'coverage_status', 'all_ranked_targets_covered',
            'source_snapshot_sha256s'), (),
            f'{code}.regime_evidence.open_ground')
        if (open_summary['status'] != 'measured' or
                open_summary['coverage_status'] != 'statewide_complete' or
                open_summary['all_ranked_targets_covered'] is not True or
                regime_evidence['land_context'] is not None):
            raise PublicationError(
                f'{code}.regime_evidence does not prove complete open ground')
        systems = _expect_object(
            open_summary['source_snapshot_sha256s'],
            f'{code}.regime_evidence.open_ground.source_snapshot_sha256s')
        expected_systems = set((registry_row or {}).get('claim_systems') or [])
        if registry_row is not None and set(systems) != expected_systems:
            raise PublicationError(
                f'{code}.regime_evidence claim systems disagree with registry')
        if not systems:
            raise PublicationError(
                f'{code}.regime_evidence needs at least one claim-system snapshot')
        for system, digest in systems.items():
            _identifier(system, f'{code}.regime_evidence.claim_system')
            _sha(digest, f'{code}.regime_evidence.{system}')
    else:
        if regime_evidence['open_ground'] != {
                'status': 'not_applicable', 'value': None, 'unit': None,
                'score': None, 'display': 'N/A'}:
            raise PublicationError(
                f'{code}.regime_evidence.open_ground must remain typed N/A')
        land_summary = regime_evidence['land_context']
        _expect_keys(land_summary, (
            'coverage_status', 'source_snapshot_sha256s'), (),
            f'{code}.regime_evidence.land_context')
        if land_summary['coverage_status'] != 'per_target_complete':
            raise PublicationError(
                f'{code}.regime_evidence land context is not complete')
        snapshots = _expect_object(
            land_summary['source_snapshot_sha256s'],
            f'{code}.regime_evidence.land_context.source_snapshot_sha256s')
        if set(snapshots) != {'surface', 'mineral', 'leasing_or_title'}:
            raise PublicationError(
                f'{code}.regime_evidence land-context snapshots are incomplete')
        for name, digest in snapshots.items():
            _sha(digest, f'{code}.regime_evidence.land_context.{name}')
    if document['sort_policy'] != SORT_POLICY:
        raise PublicationError(f'{code}.sort_policy is not the N/A-aware contract')
    targets = _expect_list(document['targets'], f'{code}.targets', nonempty=True)
    seen = set()
    computed = []
    for index, row in enumerate(targets):
        label = f'{code}.targets[{index}]'
        _expect_keys(row, (
            'target_id', 'name', 'rank', 'area_km2', 'location', 'score',
            'grade', 'geology', 'open_ground', 'land_context'), ('district',),
            label)
        target_id = _identifier(row['target_id'], f'{label}.target_id')
        if target_id in seen:
            raise PublicationError(f'{code} compiled targets duplicate {target_id}')
        seen.add(target_id)
        rank = _integer(row['rank'], f'{label}.rank', minimum=1)
        if rank != index + 1:
            raise PublicationError(f'{code} compiled ranks must be contiguous and ordered')
        _text(row['name'], f'{label}.name', maximum=300)
        area = _number(row['area_km2'], f'{label}.area_km2')
        location = row['location']
        _expect_keys(location, ('longitude', 'latitude'), (), f'{label}.location')
        _number(location['longitude'], f'{label}.longitude', minimum=-180, maximum=180)
        _number(location['latitude'], f'{label}.latitude', minimum=-90, maximum=90)
        if 'district' in row:
            _text(row['district'], f'{label}.district', maximum=300)
        grade = _validate_compiled_term(row['grade'], f'{label}.grade')
        geology = _validate_compiled_term(row['geology'], f'{label}.geology')
        score = row['score']
        _expect_keys(score, ('total', 'grade', 'geology', 'open_ground'), (), f'{label}.score')
        total = _number(score['total'], f'{label}.score.total', maximum=300)
        if score['grade'] != grade or score['geology'] != geology:
            raise PublicationError(f'{label}.score component values do not reconcile')
        open_term = row['open_ground']
        if regime == 'claim':
            _expect_keys(open_term, (
                'status', 'value', 'unit', 'score', 'evidence_refs',
                'rationale', 'display'), (), f'{label}.open_ground')
            if open_term['status'] != 'measured' or open_term['unit'] != 'fraction':
                raise PublicationError(f'{label}.open_ground is not measured')
            value = _number(
                open_term['value'], f'{label}.open_ground.value', maximum=1)
            open_score = _number(
                open_term['score'], f'{label}.open_ground.score', maximum=100)
            if open_term['display'] != _percent(value):
                raise PublicationError(f'{label}.open_ground.display is not derived')
            evidence_refs = _sha_list(
                open_term['evidence_refs'], f'{label}.open_ground.evidence_refs')
            if set(evidence_refs) != set(systems.values()):
                raise PublicationError(
                    f'{label}.open_ground evidence does not match regime_evidence '
                    'claim-system snapshots')
            _text(open_term['rationale'], f'{label}.open_ground.rationale', minimum=12)
            if row['land_context'] is not None:
                raise PublicationError(f'{label} claim target cannot carry non-claim context')
        else:
            if open_term != {
                    'status': 'not_applicable', 'value': None, 'unit': None,
                    'score': None, 'display': 'N/A'}:
                raise PublicationError(
                    f'{label}.open_ground must remain typed N/A, never zero')
            open_score = None
            _validate_compiled_context(
                row['land_context'], f'{label}.land_context', snapshots)
        expected_total = grade + geology + (open_score or 0)
        if not math.isclose(total, expected_total, rel_tol=1e-12, abs_tol=1e-8):
            raise PublicationError(f'{label}.score.total does not equal its components')
        if score['open_ground'] != open_score:
            raise PublicationError(f'{label}.score.open_ground loses typed semantics')
        computed.append({
            'target_id': target_id, 'area_km2': area,
            'score': {'total': total}, 'open_ground': open_term,
        })
    if [row['target_id'] for row in computed] != [
            row['target_id'] for row in sorted(computed, key=target_sort_key)]:
        raise PublicationError(f'{code} compiled targets are not deterministically sorted')
    metrics = {
        'targets': len(targets),
        'measured_open_ground': sum(
            row['open_ground']['status'] == 'measured' for row in targets),
        'measured_zero_open_ground': sum(
            row['open_ground']['status'] == 'measured' and
            row['open_ground']['score'] == 0 for row in targets),
        'not_applicable_open_ground': sum(
            row['open_ground']['status'] == 'not_applicable' for row in targets),
        'land_context_cards': sum(row['land_context'] is not None for row in targets),
    }
    if document['metrics'] != metrics:
        raise PublicationError(f'{code}.metrics do not reconcile with target rows')
    return {'metrics': metrics, 'targets': targets}


def load_inventory(inventory_path):
    original = os.path.abspath(inventory_path)
    _assert_private(original, 'target-scoring inventory')
    inventory_path = os.path.realpath(original)
    inventory, inventory_raw = load_strict_json(
        inventory_path, 'target-scoring inventory')
    _expect_keys(inventory, (
        'schema_version', 'dataset', 'snapshot', 'method_id', 'review',
        'states'), (), 'target-scoring inventory')
    if inventory['schema_version'] != 1 or inventory['dataset'] != DATASET:
        raise PublicationError(
            f'inventory must be schema 1 and dataset={DATASET!r}')
    snapshot = _date(inventory['snapshot'], 'inventory.snapshot')
    method_id = _identifier(inventory['method_id'], 'inventory.method_id')
    _review(inventory['review'], 'inventory.review', snapshot)
    registry, registry_sha = _registry_context()
    states = _expect_object(inventory['states'], 'inventory.states')
    if set(states) != set(registry):
        missing = sorted(set(registry) - set(states))
        extra = sorted(set(states) - set(registry))
        raise PublicationError(
            f'inventory must contain exact registry 49: missing={missing}, extra={extra}')
    staging_root = os.path.dirname(inventory_path)
    artifacts = {}
    used_paths = set()
    for code in sorted(registry):
        label = f'inventory.states.{code}'
        regime = registry[code]['regime']
        required = (
            'ranked_targets', 'grade_terms', 'geology_terms',
            'open_ground' if regime == 'claim' else 'land_context')
        _expect_keys(states[code], required, (), label)
        artifacts[code] = {}
        for name in required:
            artifact = _artifact_descriptor(
                states[code][name], staging_root, f'{code}.{name}')
            if artifact.path in used_paths:
                raise PublicationError(
                    f'{code}.{name} reuses an artifact path assigned elsewhere')
            used_paths.add(artifact.path)
            artifacts[code][name] = artifact
    return {
        'inventory_path': inventory_path,
        'inventory_bytes': len(inventory_raw),
        'inventory_sha256': sha256_bytes(inventory_raw),
        'snapshot': snapshot,
        'method_id': method_id,
        'registry': registry,
        'registry_sha256': registry_sha,
        'state_artifacts': artifacts,
    }


def _verify_unchanged(context):
    try:
        size = os.path.getsize(context['inventory_path'])
        digest = sha256_file(context['inventory_path'])
    except OSError as exc:
        raise PublicationError(f'inventory changed/disappeared during build: {exc}') from exc
    if size != context['inventory_bytes'] or digest != context['inventory_sha256']:
        raise PublicationError('inventory changed during build')
    for artifacts in context['state_artifacts'].values():
        for artifact in artifacts.values():
            try:
                size = os.path.getsize(artifact.path)
                digest = sha256_file(artifact.path)
            except OSError as exc:
                raise PublicationError(
                    f'{artifact.label} changed/disappeared during build: {exc}') from exc
            if size != artifact.bytes or digest != artifact.sha256:
                raise PublicationError(f'{artifact.label} changed during build')


def _atomic_write(path, raw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.' + os.path.basename(path) + '.', suffix='.tmp',
        dir=os.path.dirname(path))
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _install_immutable(path, raw):
    if os.path.exists(path):
        try:
            with open(path, 'rb') as existing:
                current = existing.read()
        except OSError as exc:
            raise PublicationError(f'cannot read existing immutable artifact: {exc}') from exc
        if current != raw:
            raise PublicationError(f'content-addressed path collision at {path}')
        return
    _atomic_write(path, raw)


def _relative_json_path(value, label):
    if (not isinstance(value, str) or not value.endswith('.json') or
            os.path.isabs(value) or
            any(part in ('', '.', '..')
                for part in value.replace('\\', '/').split('/'))):
        raise PublicationError(f'{label} must be a safe relative JSON path')
    return value.replace('\\', '/')


def validate_pointer(publish_dir):
    publish_dir = os.path.realpath(publish_dir)
    pointer, pointer_raw = load_strict_json(
        os.path.join(publish_dir, 'latest.json'), 'scoring latest pointer')
    _expect_keys(pointer, (
        'schema_version', 'dataset', 'snapshot', 'run', 'sha256', 'states',
        'targets', 'effect'), (), 'scoring latest pointer')
    if (pointer['schema_version'] != 1 or pointer['dataset'] != DATASET or
            pointer['states'] != 49 or pointer['effect'] != EFFECT):
        raise PublicationError('scoring latest pointer identity is invalid')
    if pointer_raw != canonical_bytes(pointer):
        raise PublicationError('scoring latest pointer is not canonical JSON')
    run_ref = _relative_json_path(pointer['run'], 'scoring latest pointer.run')
    run_sha = _sha(pointer['sha256'], 'scoring latest pointer.sha256')
    if os.path.basename(run_ref) != f'{run_sha}.json':
        raise PublicationError('scoring run path is not content addressed')
    run_path = os.path.join(publish_dir, run_ref)
    run, run_raw = load_strict_json(run_path, 'scoring run')
    if sha256_bytes(run_raw) != run_sha or run_raw != canonical_bytes(run):
        raise PublicationError('scoring run checksum/canonical form is invalid')
    _expect_keys(run, (
        'schema_version', 'dataset', 'snapshot', 'states', 'targets',
        'registry_sha256', 'inventory', 'method_id', 'sort_policy',
        'state_evidence', 'effect'), (), 'scoring run')
    if (run['schema_version'] != 1 or run['dataset'] != DATASET or
            run['states'] != 49 or run['effect'] != EFFECT or
            run['sort_policy'] != SORT_POLICY):
        raise PublicationError('scoring run identity is invalid')
    registry, registry_sha = _registry_context()
    if run['registry_sha256'] != registry_sha:
        raise PublicationError('scoring run registry checksum is stale')
    _identifier(run['method_id'], 'scoring run.method_id')
    _expect_keys(run['inventory'], ('bytes', 'sha256'), (), 'scoring run.inventory')
    _integer(run['inventory']['bytes'], 'scoring run.inventory.bytes', minimum=1)
    _sha(run['inventory']['sha256'], 'scoring run.inventory.sha256')
    refs = _expect_object(run['state_evidence'], 'scoring run.state_evidence')
    if set(refs) != set(registry):
        raise PublicationError('scoring run does not reference exact 49-state evidence')
    total_targets = 0
    for code in sorted(refs):
        descriptor = refs[code]
        _expect_keys(descriptor, (
            'file', 'bytes', 'sha256', 'regime', 'target_count', 'metrics'), (),
            f'scoring run.state_evidence.{code}')
        state_ref = _relative_json_path(
            descriptor['file'], f'scoring run.state_evidence.{code}.file')
        state_sha = _sha(
            descriptor['sha256'], f'scoring run.state_evidence.{code}.sha256')
        if os.path.basename(state_ref) != f'{state_sha}.json':
            raise PublicationError(f'{code} state evidence path is not content addressed')
        state_path = os.path.join(publish_dir, state_ref)
        state, state_raw = load_strict_json(state_path, f'{code} scoring evidence')
        if (len(state_raw) != descriptor['bytes'] or
                sha256_bytes(state_raw) != state_sha or
                state_raw != canonical_bytes(state)):
            raise PublicationError(f'{code} scoring evidence checksum/canonical form is invalid')
        checked = validate_compiled_state_document(
            state, code, registry[code], registry_sha)
        count = _integer(
            descriptor['target_count'],
            f'scoring run.state_evidence.{code}.target_count', minimum=1)
        if (descriptor['regime'] != registry[code]['regime'] or
                descriptor['metrics'] != checked['metrics'] or
                count != checked['metrics']['targets'] or
                state['method_id'] != run['method_id'] or
                state['inventory_sha256'] != run['inventory']['sha256']):
            raise PublicationError(f'{code} scoring run descriptor does not reconcile')
        total_targets += count
    if (run['targets'] != total_targets or pointer['targets'] != total_targets or
            pointer['snapshot'] != run['snapshot']):
        raise PublicationError('scoring national target totals do not reconcile')
    return {'pointer': pointer, 'pointer_bytes': len(pointer_raw), 'run': run}


def build(inventory_path, publish_dir, before_commit=None):
    """Compile all 49 states and atomically publish one evidence pointer."""
    context = load_inventory(inventory_path)
    publish_dir = os.path.realpath(publish_dir)
    staging = os.path.dirname(context['inventory_path'])
    if _inside(publish_dir, staging):
        raise PublicationError('publication directory must be outside private staging')

    state_documents = {}
    state_refs = {}
    total_targets = 0
    for code in sorted(context['registry']):
        artifacts = context['state_artifacts'][code]
        documents = {}
        for name, artifact in artifacts.items():
            documents[name], _ = load_strict_json(
                artifact.path, f'{code} {name} evidence')
        document = _compile_state(
            code, context['registry'][code], context['registry_sha256'],
            context['inventory_sha256'], context['snapshot'],
            context['method_id'], artifacts, documents)
        raw = canonical_bytes(document)
        digest = sha256_bytes(raw)
        relative = f'states/{code.lower()}/{digest}.json'
        state_documents[code] = (raw, relative)
        state_refs[code] = {
            'file': relative,
            'bytes': len(raw),
            'sha256': digest,
            'regime': context['registry'][code]['regime'],
            'target_count': document['metrics']['targets'],
            'metrics': document['metrics'],
        }
        total_targets += document['metrics']['targets']

    run = {
        'schema_version': 1,
        'dataset': DATASET,
        'snapshot': context['snapshot'],
        'states': 49,
        'targets': total_targets,
        'registry_sha256': context['registry_sha256'],
        'inventory': {
            'bytes': context['inventory_bytes'],
            'sha256': context['inventory_sha256'],
        },
        'method_id': context['method_id'],
        'sort_policy': SORT_POLICY,
        'state_evidence': state_refs,
        'effect': EFFECT,
    }
    run_raw = canonical_bytes(run)
    run_sha = sha256_bytes(run_raw)
    run_relative = f'runs/{run_sha}.json'
    pointer = {
        'schema_version': 1,
        'dataset': DATASET,
        'snapshot': context['snapshot'],
        'run': run_relative,
        'sha256': run_sha,
        'states': 49,
        'targets': total_targets,
        'effect': EFFECT,
    }
    if before_commit is not None:
        before_commit(context)
    _verify_unchanged(context)
    for code in sorted(state_documents):
        raw, relative = state_documents[code]
        _install_immutable(os.path.join(publish_dir, relative), raw)
    _install_immutable(os.path.join(publish_dir, run_relative), run_raw)
    _verify_unchanged(context)
    _atomic_write(
        os.path.join(publish_dir, 'latest.json'), canonical_bytes(pointer))
    checked = validate_pointer(publish_dir)
    if checked['pointer'] != pointer or checked['run'] != run:
        raise PublicationError('post-publication scoring verification disagrees')
    return {
        'pointer': 'latest.json',
        'run': run_relative,
        'states': 49,
        'targets': total_targets,
        'claim_states': sum(
            row['regime'] == 'claim' for row in context['registry'].values()),
        'non_claim_states': sum(
            row['regime'] == 'non_claim' for row in context['registry'].values()),
        'effect': EFFECT,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', help='private exact-49 reviewed inventory JSON')
    parser.add_argument('--publish', help='evidence output directory (outside staging)')
    parser.add_argument('--validate', metavar='PUBLISH_DIR',
                        help='validate an existing latest.json publication')
    args = parser.parse_args(argv)
    try:
        if args.validate:
            if args.inventory or args.publish:
                parser.error('--validate cannot be combined with --inventory/--publish')
            result = validate_pointer(args.validate)
            output = {
                'ok': True,
                'states': result['run']['states'],
                'targets': result['run']['targets'],
                'run': result['pointer']['run'],
            }
        else:
            if not args.inventory or not args.publish:
                parser.error('--inventory and --publish are required for a build')
            output = build(args.inventory, args.publish)
            output['ok'] = True
    except PublicationError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
