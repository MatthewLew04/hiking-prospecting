#!/usr/bin/env python3
"""Compile a real per-state browser run into immutable WS11 CI evidence.

The browser runner owns observation; this compiler owns trust boundaries.  It
accepts a private strict-JSON result plus the exact candidate release manifest,
coverage snapshot, and current budget file used by that run.  It reconciles
every state descriptor and measurement, then writes one content-addressed JSON
below ``site/map-assets/releases``.  It never edits the registry, coverage,
manifest, or release flags.
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

from state_registry import ALL_STATES, GATE_KEYS


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
BUDGETS = os.path.join(ROOT, 'ci', 'budgets.json')
DEFAULT_PUBLISH = os.path.join(
    SITE, 'map-assets', 'releases', 'ci-acceptance')
DATASET = 'ws11-state-ci-acceptance'
BROWSER_TEST_ID = 'nwmm-state-release-browser'
BROWSER_TEST_VERSION = 2
SHA_RE = re.compile(r'[0-9a-f]{64}')
COMMIT_RE = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
FAILURE_FIELDS = (
    'page_errors',
    'map_errors',
    'request_failures',
    'http_errors',
    'console_errors',
    'unhandled_rejections',
    'statewide_json_requests',
)


class AcceptanceError(ValueError):
    """A runner result or candidate release input violates the contract."""


def _reject_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def strict_json_bytes(raw, label):
    try:
        value = json.loads(raw, parse_constant=_reject_constant,
                           object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AcceptanceError(f'{label} is not strict JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f'{label} top level must be an object')
    return value


def canonical_bytes(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(f'evidence is not canonical JSON: {exc}') from exc


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _inside(path, parent):
    try:
        return os.path.commonpath((os.path.realpath(path),
                                   os.path.realpath(parent))) == os.path.realpath(parent)
    except ValueError:
        return False


def _text(value, label, minimum=1, maximum=4096):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value):
        raise AcceptanceError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def _identifier(value, label):
    value = _text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise AcceptanceError(f'{label} is not a stable identifier')
    return value


def _sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise AcceptanceError(f'{label} must be a lowercase SHA-256')
    return value


def _integer(value, label, minimum=0):
    if (not isinstance(value, int) or isinstance(value, bool) or
            value < minimum):
        raise AcceptanceError(f'{label} must be an integer >= {minimum}')
    return value


def _number(value, label, minimum=0):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value < minimum):
        raise AcceptanceError(f'{label} must be finite and >= {minimum}')
    return value


def _expect_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise AcceptanceError(f'{label} must be an object')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise AcceptanceError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def _https(value, label):
    value = _text(value, label, maximum=2048)
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or
            parsed.password or parsed.fragment or any(char.isspace() for char in value)):
        raise AcceptanceError(f'{label} must be an HTTPS URL without credentials/fragment')
    return value


def _timestamp(value, label):
    value = _text(value, label, maximum=40)
    if not value.endswith('Z'):
        raise AcceptanceError(f'{label} must be a UTC ISO-8601 timestamp ending in Z')
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError as exc:
        raise AcceptanceError(f'{label} is not an ISO-8601 timestamp') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise AcceptanceError(f'{label} must use UTC')
    return value


@dataclass(frozen=True)
class Snapshot:
    label: str
    path: str
    raw: bytes
    value: dict
    bytes: int
    sha256: str


def _snapshot(path, label, *, private=False):
    path = os.path.abspath(path)
    if os.path.islink(path):
        raise AcceptanceError(f'{label} must not be a symlink')
    if private and _inside(path, SITE):
        raise AcceptanceError(f'{label} is inside public site/; raw runner data is private')
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise AcceptanceError(f'cannot read {label}: {exc}') from exc
    return Snapshot(label, path, raw, strict_json_bytes(raw, label),
                    len(raw), sha256_bytes(raw))


def _binding(value, snapshot, label):
    _expect_keys(value, ('bytes', 'sha256'), (), label)
    size = _integer(value['bytes'], f'{label}.bytes', minimum=1)
    digest = _sha(value['sha256'], f'{label}.sha256')
    if size != snapshot.bytes or digest != snapshot.sha256:
        raise AcceptanceError(
            f'{label} does not match the exact {snapshot.label} bytes/checksum')


def _validate_coverage(coverage, code):
    if (_integer(coverage.get('schema_version'), 'coverage.schema_version', 1)
            != 1):
        raise AcceptanceError('coverage must be schema_version 1')
    rows = coverage.get('states')
    if (not isinstance(rows, list) or len(rows) != len(ALL_STATES) or
            any(not isinstance(row, dict) for row in rows)):
        raise AcceptanceError('coverage must contain exactly 49 state objects')
    by_state = {}
    for row in rows:
        state = row.get('state')
        if state in by_state:
            raise AcceptanceError(f'coverage has duplicate state {state!r}')
        by_state[state] = row
    if set(by_state) != set(ALL_STATES):
        raise AcceptanceError('coverage state identities are not the exact WS11 scope')
    summary = coverage.get('summary')
    released = sum(row.get('enabled') is True for row in rows)
    gate_complete = sum(row.get('gate_passed') is True for row in rows)
    if not isinstance(summary, dict):
        raise AcceptanceError('coverage summary does not reconcile state toggles/gates')
    summary_states = _integer(summary.get('states'), 'coverage.summary.states')
    summary_released = _integer(
        summary.get('released'), 'coverage.summary.released')
    summary_gate_complete = _integer(
        summary.get('gate_complete'), 'coverage.summary.gate_complete')
    if (summary_states != 49 or summary_released != released or
            summary_gate_complete != gate_complete):
        raise AcceptanceError('coverage summary does not reconcile state toggles/gates')
    target = by_state[code]
    gates = target.get('gates')
    if (target.get('enabled') is not True or target.get('gate_passed') is not True or
            target.get('release') != 'done' or not isinstance(gates, dict) or
            set(gates) != set(GATE_KEYS) or
            any(not isinstance(gate, dict) or
                gate.get('status') not in ('pass', 'not_applicable')
                for gate in gates.values())):
        raise AcceptanceError(
            f'{code} candidate coverage is not enabled, gate-passed, and done')
    return target


def _browser_path(value, label, suffix=None):
    value = _text(value, label, maximum=2048)
    path = value.split('?', 1)[0].split('#', 1)[0]
    if re.search(r'\.(?:geo)?json$', path, re.I):
        raise AcceptanceError(f'{label} is forbidden statewide browser JSON')
    if any(part == '..' for part in path.replace('\\', '/').split('/')):
        raise AcceptanceError(f'{label} contains path traversal')
    if suffix and not path.lower().endswith(suffix):
        raise AcceptanceError(f'{label} must end in {suffix}')
    return value


def _style_ids(descriptor, label):
    styles = descriptor.get('style_layers')
    if (not isinstance(styles, list) or not styles or
            any(not isinstance(style, dict) or
                not isinstance(style.get('id'), str) or not style['id']
                for style in styles)):
        raise AcceptanceError(f'{label}.style_layers must identify browser layers')
    ids = [style['id'] for style in styles]
    if len(ids) != len(set(ids)):
        raise AcceptanceError(f'{label}.style_layers has duplicate IDs')
    return ids


def _bounds(value, label):
    if (not isinstance(value, list) or len(value) != 4 or
            any(not isinstance(item, (int, float)) or isinstance(item, bool) or
                not math.isfinite(item) for item in value) or
            not -180 <= value[0] < value[2] <= 180 or
            not -90 <= value[1] < value[3] <= 90):
        raise AcceptanceError(f'{label} must be ordered finite EPSG:4326 bounds')
    return list(value)


def _validate_manifest(manifest, code):
    if manifest.get('region') != sorted(ALL_STATES):
        raise AcceptanceError('release manifest region must be exact sorted WS11 scope')
    rows = manifest.get('tiled_layers')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AcceptanceError('release manifest tiled_layers must be a list of objects')
    all_ids = [row.get('id') for row in rows]
    if (any(not isinstance(item, str) or not item for item in all_ids) or
            len(all_ids) != len(set(all_ids))):
        raise AcceptanceError('release manifest descriptor IDs must be unique text')
    if any(row.get('state') not in ALL_STATES for row in rows):
        raise AcceptanceError('release manifest has a descriptor outside WS11')
    selected = [row for row in rows if row.get('state') == code]
    if not selected:
        raise AcceptanceError(f'release manifest advertises no descriptors for {code}')
    normalized = {}
    source_urls = {}
    runtime_style_ids = set()
    for row in selected:
        label = f'{code} descriptor {row.get("id")!r}'
        descriptor_id = _identifier(row.get('id'), f'{label}.id')
        source_id = _identifier(row.get('source_id'), f'{label}.source_id')
        category = _identifier(row.get('kind'), f'{label}.kind')
        delivery = row.get('delivery')
        if delivery not in ('pmtiles', 'cog'):
            raise AcceptanceError(f'{label}.delivery must be pmtiles or cog')
        url = _browser_path(row.get('url'), f'{label}.url',
                            suffix='.pmtiles' if delivery == 'pmtiles' else None)
        style_ids = _style_ids(row, label)
        runtime_ids = [f'ws11-{item}' for item in style_ids]
        if runtime_style_ids.intersection(runtime_ids):
            raise AcceptanceError(f'{label} collides with another runtime style layer')
        runtime_style_ids.update(runtime_ids)
        prior_url = source_urls.setdefault(source_id, url)
        if prior_url != url:
            raise AcceptanceError(
                f'{label}.source_id maps to more than one advertised URL')
        source_layer = None
        declared_n = None
        if delivery == 'pmtiles':
            source_layer = _text(row.get('source_layer'),
                                 f'{label}.source_layer', maximum=256)
            declared_n = _integer(row.get('n'), f'{label}.n')
            if row.get('availability') != 'complete' or row.get('complete') is not True:
                raise AcceptanceError(
                    f'{label} must declare complete availability and a reviewed count')
            raw_bounds = row.get('view_bounds')
            if not isinstance(raw_bounds, list) or not raw_bounds:
                raise AcceptanceError(f'{label}.view_bounds must be nonempty')
            visit_bounds = [
                _bounds(bounds, f'{label}.view_bounds[{index}]')
                for index, bounds in enumerate(raw_bounds)]
            activation_minzoom = _integer(
                row.get('activation_minzoom'), f'{label}.activation_minzoom')
            if activation_minzoom > 24:
                raise AcceptanceError(f'{label}.activation_minzoom exceeds 24')
            expected_filter = ['==', ['get', 'st'], code]
            if row.get('filter') != expected_filter or any(
                    style.get('filter') != expected_filter
                    for style in row['style_layers']):
                raise AcceptanceError(f'{label} lacks the exact state filter')
            visit_minzoom, visit_maxzoom = activation_minzoom, 24
            state_filter = expected_filter
        else:
            tile_template = url
            if any(tile_template.count(token) != 1 for token in ('{z}', '{x}', '{y}')):
                raise AcceptanceError(f'{label}.url must be an XYZ tile template')
            cog = row.get('cog')
            if not isinstance(cog, dict):
                raise AcceptanceError(f'{label}.cog provenance is missing')
            _browser_path(cog.get('url'), f'{label}.cog.url')
            if not re.search(r'\.tiff?$', cog['url'].split('?', 1)[0], re.I):
                raise AcceptanceError(f'{label}.cog.url must identify a TIFF')
            _sha(cog.get('sha256'), f'{label}.cog.sha256')
            _integer(cog.get('bytes'), f'{label}.cog.bytes', minimum=128)
            visit_bounds = [_bounds(row.get('bounds'), f'{label}.bounds')]
            visit_minzoom = _integer(row.get('minzoom'), f'{label}.minzoom')
            visit_maxzoom = _integer(row.get('maxzoom'), f'{label}.maxzoom')
            if not visit_minzoom <= visit_maxzoom <= 24:
                raise AcceptanceError(f'{label} raster zoom range is invalid')
            state_filter = None
        normalized[descriptor_id] = {
            'descriptor': row,
            'descriptor_sha256': sha256_bytes(canonical_bytes(row)),
            'delivery': delivery,
            'source_id': source_id,
            'source_layer': source_layer,
            'source_url': ('pmtiles://' + url if delivery == 'pmtiles' else url),
            'runtime_style_layer_ids': runtime_ids,
            'declared_n': declared_n,
            'visit_category': category,
            'visit_bounds': visit_bounds,
            'visit_minzoom': visit_minzoom,
            'visit_maxzoom': visit_maxzoom,
            'state_filter': state_filter,
        }
    return normalized


def _validate_budgets(value):
    if (_integer(value.get('schema_version'), 'budgets.schema_version', 1)
            != 1):
        raise AcceptanceError('budget config must be schema_version 1')
    browser = value.get('browser')
    if not isinstance(browser, dict):
        raise AcceptanceError('budget config has no browser object')
    return {
        'heap_mb_max': _number(browser.get('heap_mb_max'),
                               'browser.heap_mb_max'),
        'bulk_origin_storage_mb_max': _number(
            browser.get('bulk_origin_storage_mb_max'),
            'browser.bulk_origin_storage_mb_max'),
    }


def _validate_failures(value):
    _expect_keys(value, FAILURE_FIELDS, (), 'browser result.failures')
    for field in FAILURE_FIELDS:
        rows = value[field]
        if (not isinstance(rows, list) or
                any(not isinstance(item, str) for item in rows)):
            raise AcceptanceError(f'browser result.failures.{field} must be a text list')
        if rows:
            raise AcceptanceError(
                f'browser result is not green: failures.{field}={rows!r}')
    return {field: [] for field in FAILURE_FIELDS}


def _validate_samples(value, limits, descriptor_orders):
    if not isinstance(value, list) or not value:
        raise AcceptanceError('browser result.measurement_samples must be nonempty')
    samples = []
    labels = set()
    off_samples = 0
    visited = set()
    for index, row in enumerate(value):
        label = f'browser result.measurement_samples[{index}]'
        _expect_keys(row, ('label', 'phase', 'descriptor_id', 'visit_order', 'heap_mb',
                           'bulk_origin_storage_mb'), (), label)
        sample_label = _identifier(row['label'], f'{label}.label')
        phase = row['phase']
        if phase not in ('state_off_settled', 'descriptor_visit_settled'):
            raise AcceptanceError(f'{label}.phase is not a required settled phase')
        if sample_label in labels:
            raise AcceptanceError('browser measurement labels must be unique')
        labels.add(sample_label)
        descriptor_id = row['descriptor_id']
        visit_order = row['visit_order']
        if phase == 'state_off_settled':
            if descriptor_id is not None or visit_order is not None:
                raise AcceptanceError('state-off measurement cannot name a descriptor visit')
            off_samples += 1
        else:
            descriptor_id = _identifier(descriptor_id, f'{label}.descriptor_id')
            visit_order = _integer(visit_order, f'{label}.visit_order')
            if (descriptor_id in visited or
                    descriptor_orders.get(descriptor_id) != visit_order):
                raise AcceptanceError(
                    f'{label} does not match one exact sequential descriptor visit')
            visited.add(descriptor_id)
        heap = _number(row['heap_mb'], f'{label}.heap_mb')
        storage = _number(row['bulk_origin_storage_mb'],
                          f'{label}.bulk_origin_storage_mb')
        if heap > limits['heap_mb_max'] or storage > limits['bulk_origin_storage_mb_max']:
            raise AcceptanceError(f'{label} exceeds current browser budget')
        samples.append({
            'label': sample_label,
            'phase': phase,
            'descriptor_id': descriptor_id,
            'visit_order': visit_order,
            'heap_mb': heap,
            'bulk_origin_storage_mb': storage,
        })
    if off_samples != 1 or visited != set(descriptor_orders):
        raise AcceptanceError(
            'browser measurements must contain state-off plus every sequential '
            'descriptor visit exactly once')
    return samples, {
        'heap_mb': max(row['heap_mb'] for row in samples),
        'bulk_origin_storage_mb': max(
            row['bulk_origin_storage_mb'] for row in samples),
    }


def _validate_observations(value, descriptors, code):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise AcceptanceError('browser result.descriptor_observations must be a list')
    by_id = {}
    required = (
        'descriptor_id', 'descriptor_sha256', 'delivery', 'source_id',
        'source_layer', 'source_url', 'runtime_style_layer_ids',
        'visit_order', 'visit_mode', 'visit_category', 'visit_bounds_index',
        'visit_center', 'visit_zoom', 'state_filter',
        'source_present', 'source_loaded', 'state_scope_applied', 'queryable',
        'query_status', 'queried_features', 'successful_source_requests',
    )
    for index, row in enumerate(value):
        label = f'browser result.descriptor_observations[{index}]'
        _expect_keys(row, required, (), label)
        descriptor_id = _identifier(row['descriptor_id'], f'{label}.descriptor_id')
        if descriptor_id in by_id:
            raise AcceptanceError(f'browser result duplicates descriptor {descriptor_id}')
        by_id[descriptor_id] = row
    if set(by_id) != set(descriptors):
        missing = sorted(set(descriptors) - set(by_id))
        extra = sorted(set(by_id) - set(descriptors))
        raise AcceptanceError(
            f'{code} descriptor observations must be exact: missing={missing}, extra={extra}')
    compiled = []
    visit_orders = set()
    for descriptor_id, expected in descriptors.items():
        row = by_id[descriptor_id]
        label = f'{code} browser descriptor {descriptor_id}'
        if (row['descriptor_sha256'] != expected['descriptor_sha256'] or
                row['delivery'] != expected['delivery'] or
                row['source_id'] != expected['source_id'] or
                row['source_layer'] != expected['source_layer'] or
                row['source_url'] != expected['source_url'] or
                row['runtime_style_layer_ids'] != expected['runtime_style_layer_ids']):
            raise AcceptanceError(f'{label} identity does not match release manifest')
        visit_order = _integer(row['visit_order'], f'{label}.visit_order')
        bounds_index = _integer(
            row['visit_bounds_index'], f'{label}.visit_bounds_index')
        center = row['visit_center']
        zoom = _number(row['visit_zoom'], f'{label}.visit_zoom')
        if visit_order in visit_orders:
            raise AcceptanceError(f'{label} duplicates sequential visit order {visit_order}')
        visit_orders.add(visit_order)
        if (row['visit_mode'] != 'exclusive_sequential' or
                row['visit_category'] != expected['visit_category'] or
                bounds_index >= len(expected['visit_bounds']) or
                not isinstance(center, list) or len(center) != 2 or
                any(not isinstance(item, (int, float)) or isinstance(item, bool) or
                    not math.isfinite(item) for item in center) or
                not (expected['visit_bounds'][bounds_index][0] <= center[0] <=
                     expected['visit_bounds'][bounds_index][2] and
                     expected['visit_bounds'][bounds_index][1] <= center[1] <=
                     expected['visit_bounds'][bounds_index][3]) or
                not expected['visit_minzoom'] <= zoom <= expected['visit_maxzoom'] or
                row['state_filter'] != expected['state_filter']):
            raise AcceptanceError(
                f'{label} lacks an exact in-bounds category/zoom/state-filter visit')
        if (row['source_present'] is not True or row['source_loaded'] is not True or
                row['state_scope_applied'] is not True or
                row['queryable'] is not True):
            raise AcceptanceError(
                f'{label} was not present, loaded, state-scoped, and queryable')
        requests = _integer(row['successful_source_requests'],
                            f'{label}.successful_source_requests', minimum=1)
        queried = row['queried_features']
        if expected['delivery'] == 'pmtiles':
            queried = _integer(queried, f'{label}.queried_features')
            if expected['declared_n'] == 0:
                if row['query_status'] != 'declared_zero' or queried != 0:
                    raise AcceptanceError(
                        f'{label} may report zero only because manifest n=0')
            elif row['query_status'] != 'nonempty' or queried <= 0:
                raise AcceptanceError(
                    f'{label} declares n>0 but did not return browser features')
        elif row['query_status'] != 'raster_loaded' or queried is not None:
            raise AcceptanceError(
                f'{label} raster must report loaded tiles and null feature count')
        compiled.append({
            'descriptor_id': descriptor_id,
            'descriptor_sha256': expected['descriptor_sha256'],
            'delivery': expected['delivery'],
            'source_id': expected['source_id'],
            'source_layer': expected['source_layer'],
            'source_url': expected['source_url'],
            'runtime_style_layer_ids': expected['runtime_style_layer_ids'],
            'declared_n': expected['declared_n'],
            'visit_order': visit_order,
            'visit_mode': 'exclusive_sequential',
            'visit_category': expected['visit_category'],
            'visit_bounds_index': bounds_index,
            'visit_center': center,
            'visit_zoom': zoom,
            'state_filter': expected['state_filter'],
            'source_present': True,
            'source_loaded': True,
            'state_scope_applied': True,
            'queryable': True,
            'query_status': row['query_status'],
            'queried_features': queried,
            'successful_source_requests': requests,
        })
    if visit_orders != set(range(len(descriptors))):
        raise AcceptanceError('descriptor visits must use exact sequential order 0..n-1')
    return sorted(compiled, key=lambda row: row['descriptor_id'])


def _validate_runner(result, code, expected_commit, expected_run_url,
                     snapshots, coverage_row, descriptors, limits):
    required = (
        'schema_version', 'test', 'generated', 'state', 'profile', 'status',
        'run_url', 'commit', 'input_bindings', 'browser', 'state_toggle',
        'descriptor_observations', 'measurement_samples', 'failures',
        'statewide_browser_json',
    )
    _expect_keys(result, required, (), 'browser result')
    if (_integer(result['schema_version'], 'browser result.schema_version', 1) != 1 or
            result['profile'] != 'release' or
            result['status'] != 'green' or result['state'] != code):
        raise AcceptanceError('browser result schema/profile/status/state is invalid')
    generated = _timestamp(result['generated'], 'browser result.generated')
    test = result['test']
    _expect_keys(test, ('id', 'version'), (), 'browser result.test')
    version = _integer(test['version'], 'browser result.test.version', minimum=1)
    if test['id'] != BROWSER_TEST_ID or version != BROWSER_TEST_VERSION:
        raise AcceptanceError('browser result test ID/version is unsupported')
    commit = result['commit']
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise AcceptanceError('browser result.commit must be a full Git SHA')
    run_url = _https(result['run_url'], 'browser result.run_url')
    if commit != expected_commit or run_url != expected_run_url:
        raise AcceptanceError('browser result commit/run URL differs from CLI identity')
    bindings = result['input_bindings']
    _expect_keys(bindings, ('manifest', 'coverage', 'budgets'), (),
                 'browser result.input_bindings')
    for name in ('manifest', 'coverage', 'budgets'):
        _binding(bindings[name], snapshots[name],
                 f'browser result.input_bindings.{name}')

    browser = result['browser']
    _expect_keys(browser, ('engine', 'engine_version', 'playwright_version',
                           'headless'), (), 'browser result.browser')
    if (browser['engine'] != 'chromium' or browser['headless'] is not True):
        raise AcceptanceError('browser result must come from headless Chromium')
    normalized_browser = {
        'engine': 'chromium',
        'engine_version': _text(
            browser['engine_version'], 'browser result.browser.engine_version',
            maximum=100),
        'playwright_version': _text(
            browser['playwright_version'],
            'browser result.browser.playwright_version', maximum=100),
        'headless': True,
    }
    toggle = result['state_toggle']
    _expect_keys(toggle, (
        'state', 'coverage_enabled', 'coverage_gate_passed', 'initial_on',
        'off_observed', 'on_observed', 'final_on', 'green'), (),
        'browser result.state_toggle')
    if (toggle.get('state') != code or
            any(toggle.get(field) is not True for field in (
                'coverage_enabled', 'coverage_gate_passed', 'initial_on',
                'off_observed', 'on_observed', 'final_on', 'green')) or
            coverage_row.get('enabled') is not True or
            coverage_row.get('gate_passed') is not True):
        raise AcceptanceError(
            f'{code} browser result does not prove the candidate off/on toggle')
    if result['statewide_browser_json'] is not False:
        raise AcceptanceError('browser result detected or failed to exclude statewide JSON')
    failures = _validate_failures(result['failures'])
    observations = _validate_observations(
        result['descriptor_observations'], descriptors, code)
    descriptor_orders = {
        row['descriptor_id']: row['visit_order'] for row in observations}
    samples, measurements = _validate_samples(
        result['measurement_samples'], limits, descriptor_orders)
    return {
        'generated': generated,
        'browser': normalized_browser,
        'state_toggle': {
            'state': code,
            'enabled': True,
            'gate_passed': True,
            'initial_on': True,
            'off_observed': True,
            'on_observed': True,
            'final_on': True,
            'green': True,
        },
        'descriptors': observations,
        'measurement_samples': samples,
        'measurements': measurements,
        'failures': failures,
    }


def _verify_unchanged(snapshots):
    for snapshot in snapshots.values():
        try:
            with open(snapshot.path, 'rb') as source:
                raw = source.read()
        except OSError as exc:
            raise AcceptanceError(
                f'{snapshot.label} changed/disappeared during compilation: {exc}') from exc
        if len(raw) != snapshot.bytes or sha256_bytes(raw) != snapshot.sha256:
            raise AcceptanceError(f'{snapshot.label} changed during compilation')


def _publish_root(path):
    release_root = os.path.realpath(
        os.path.join(SITE, 'map-assets', 'releases'))
    requested = os.path.abspath(path)
    if os.path.lexists(requested) and os.path.islink(requested):
        raise AcceptanceError('publish directory must not be a symlink')
    path = os.path.realpath(requested)
    if not _inside(path, release_root):
        raise AcceptanceError(
            'publish directory must be below site/map-assets/releases/')
    return path


def _atomic_install(path, raw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, 'rb') as existing:
            if existing.read() != raw:
                raise AcceptanceError(f'content-addressed collision at {path}')
        return
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


def _safe_publish_path(publish_dir, code, digest):
    release_root = os.path.realpath(
        os.path.join(SITE, 'map-assets', 'releases'))
    state_dir = os.path.join(publish_dir, code.lower())
    if (not _inside(os.path.realpath(state_dir), release_root) or
            (os.path.lexists(state_dir) and os.path.islink(state_dir))):
        raise AcceptanceError('state evidence directory escapes the release prefix')
    path = os.path.join(state_dir, f'{digest}.json')
    if (not _inside(os.path.realpath(path), release_root) or
            (os.path.lexists(path) and os.path.islink(path))):
        raise AcceptanceError(
            'CI evidence path escapes the release prefix or is a symlink')
    return path


def validate_evidence_document(value, code, *, manifest_snapshot,
                               coverage_snapshot, budgets_snapshot):
    """Recompute a compiled artifact against the exact current release inputs."""
    required = (
        'schema_version', 'dataset', 'state', 'profile', 'status', 'generated',
        'run_url', 'commit', 'browser_test', 'browser', 'state_toggle',
        'statewide_browser_json', 'runner_result', 'release_inputs', 'descriptors',
        'measurement_samples', 'measurements', 'budget_limits', 'failures',
        'effect',
    )
    _expect_keys(value, required, (), 'CI evidence')
    if (_integer(value['schema_version'], 'CI evidence.schema_version', 1) != 1 or
            value['dataset'] != DATASET or value['state'] != code or
            value['profile'] != 'release' or value['status'] != 'green' or
            value['effect'] != 'evidence_only_no_release_mutation' or
            value['statewide_browser_json'] is not False):
        raise AcceptanceError('CI evidence identity is invalid')
    _timestamp(value['generated'], 'CI evidence.generated')
    _https(value['run_url'], 'CI evidence.run_url')
    if (not isinstance(value['commit'], str) or
            COMMIT_RE.fullmatch(value['commit']) is None):
        raise AcceptanceError('CI evidence.commit must be a full Git SHA')

    browser_test = value['browser_test']
    _expect_keys(browser_test, ('id', 'version'), (), 'CI evidence.browser_test')
    if (browser_test['id'] != BROWSER_TEST_ID or
            _integer(browser_test['version'],
                     'CI evidence.browser_test.version', 1) != BROWSER_TEST_VERSION):
        raise AcceptanceError('CI evidence browser test ID/version is unsupported')
    browser = value['browser']
    _expect_keys(browser, ('engine', 'engine_version', 'playwright_version',
                           'headless'), (), 'CI evidence.browser')
    if (browser.get('engine') != 'chromium' or browser.get('headless') is not True):
        raise AcceptanceError('CI evidence must identify headless Chromium')
    _text(browser.get('engine_version'), 'CI evidence.browser.engine_version',
          maximum=100)
    _text(browser.get('playwright_version'),
          'CI evidence.browser.playwright_version', maximum=100)

    runner = value['runner_result']
    _expect_keys(runner, ('bytes', 'sha256'), (), 'CI evidence.runner_result')
    _integer(runner['bytes'], 'CI evidence.runner_result.bytes', minimum=1)
    _sha(runner['sha256'], 'CI evidence.runner_result.sha256')

    snapshots = {
        'manifest': manifest_snapshot,
        'coverage': coverage_snapshot,
        'budgets': budgets_snapshot,
    }
    bindings = value['release_inputs']
    _expect_keys(bindings, tuple(snapshots), (), 'CI evidence.release_inputs')
    for name, snapshot in snapshots.items():
        _binding(bindings[name], snapshot, f'CI evidence.release_inputs.{name}')

    coverage_row = _validate_coverage(coverage_snapshot.value, code)
    expected_descriptors = _validate_manifest(manifest_snapshot.value, code)
    limits = _validate_budgets(budgets_snapshot.value)
    if value['budget_limits'] != limits:
        raise AcceptanceError('CI evidence budget limits do not match current budgets')

    toggle = value['state_toggle']
    _expect_keys(toggle, (
        'state', 'enabled', 'gate_passed', 'initial_on', 'off_observed',
        'on_observed', 'final_on', 'green'), (), 'CI evidence.state_toggle')
    if (toggle.get('state') != code or
            any(toggle.get(field) is not True for field in (
                'enabled', 'gate_passed', 'initial_on', 'off_observed',
                'on_observed', 'final_on', 'green')) or
            coverage_row.get('enabled') is not True or
            coverage_row.get('gate_passed') is not True):
        raise AcceptanceError('CI evidence does not prove the current state toggle')

    compiled_rows = value['descriptors']
    if not isinstance(compiled_rows, list):
        raise AcceptanceError('CI evidence.descriptors must be a list')
    raw_rows = []
    compiled_required = (
        'descriptor_id', 'descriptor_sha256', 'delivery', 'source_id',
        'source_layer', 'source_url', 'runtime_style_layer_ids', 'declared_n',
        'visit_order', 'visit_mode', 'visit_category', 'visit_bounds_index',
        'visit_center', 'visit_zoom', 'state_filter',
        'source_present', 'source_loaded', 'state_scope_applied', 'queryable',
        'query_status', 'queried_features', 'successful_source_requests',
    )
    for index, row in enumerate(compiled_rows):
        label = f'CI evidence.descriptors[{index}]'
        _expect_keys(row, compiled_required, (), label)
        descriptor_id = row.get('descriptor_id')
        expected = expected_descriptors.get(descriptor_id)
        if expected is None or row.get('declared_n') != expected['declared_n']:
            raise AcceptanceError(
                f'{label}.declared_n/descriptor identity differs from manifest')
        raw_rows.append({key: item for key, item in row.items()
                         if key != 'declared_n'})
    recomputed = _validate_observations(raw_rows, expected_descriptors, code)
    if compiled_rows != recomputed:
        raise AcceptanceError(
            'CI evidence descriptors are not canonical compiler observations')

    descriptor_orders = {
        row['descriptor_id']: row['visit_order'] for row in compiled_rows}
    samples, measurements = _validate_samples(
        value['measurement_samples'], limits, descriptor_orders)
    if value['measurement_samples'] != samples or value['measurements'] != measurements:
        raise AcceptanceError(
            'CI evidence measurements do not derive from the settled samples')
    failures = _validate_failures(value['failures'])
    if value['failures'] != failures:
        raise AcceptanceError('CI evidence failure sets are not canonical')
    return value


def validate_release_evidence_file(path, code, *, manifest_path, coverage_path,
                                   budgets_path, expected_sha=None):
    """Validate a published blob, including current manifest/coverage replay."""
    path = os.path.abspath(path)
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise AcceptanceError(f'cannot read CI evidence: {exc}') from exc
    value = strict_json_bytes(raw, 'CI evidence')
    digest = sha256_bytes(raw)
    if expected_sha is not None and _sha(expected_sha, 'CI acceptance.sha256') != digest:
        raise AcceptanceError('CI evidence checksum differs from release acceptance')
    if os.path.basename(path) != f'{digest}.json':
        raise AcceptanceError('CI evidence filename is not its content SHA-256')
    if raw != canonical_bytes(value):
        raise AcceptanceError('CI evidence is not canonical JSON')
    snapshots = {
        'manifest': _snapshot(manifest_path, 'current release manifest'),
        'coverage': _snapshot(coverage_path, 'current coverage'),
        'budgets': _snapshot(budgets_path, 'current CI budgets'),
    }
    return validate_evidence_document(
        value, code, manifest_snapshot=snapshots['manifest'],
        coverage_snapshot=snapshots['coverage'], budgets_snapshot=snapshots['budgets'])


def validate_published(path, *, expected_state=None):
    path = os.path.abspath(path)
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise AcceptanceError(f'cannot read CI evidence: {exc}') from exc
    value = strict_json_bytes(raw, 'CI evidence')
    digest = sha256_bytes(raw)
    if os.path.basename(path) != f'{digest}.json':
        raise AcceptanceError('CI evidence filename is not its content SHA-256')
    if raw != canonical_bytes(value):
        raise AcceptanceError('CI evidence is not canonical JSON')
    required = (
        'schema_version', 'dataset', 'state', 'profile', 'status', 'generated',
        'run_url', 'commit', 'browser_test', 'browser', 'state_toggle',
        'statewide_browser_json', 'runner_result', 'release_inputs', 'descriptors',
        'measurement_samples', 'measurements', 'budget_limits', 'failures',
        'effect',
    )
    _expect_keys(value, required, (), 'CI evidence')
    if (_integer(value['schema_version'], 'CI evidence.schema_version', 1) != 1 or
            value['dataset'] != DATASET or
            value['profile'] != 'release' or value['status'] != 'green' or
            value['effect'] != 'evidence_only_no_release_mutation' or
            value['statewide_browser_json'] is not False or
            (expected_state is not None and value['state'] != expected_state)):
        raise AcceptanceError('CI evidence identity is invalid')
    return value


def build(*, browser_result_path, manifest_path, coverage_path, state,
          commit, run_url, publish_dir=DEFAULT_PUBLISH, budgets_path=BUDGETS,
          before_commit=None):
    """Validate one browser run and install a directly consumable state blob."""
    code = str(state).upper()
    if code not in ALL_STATES or code != state:
        raise AcceptanceError('state must be an uppercase WS11 code (Hawaii excluded)')
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise AcceptanceError('commit must be a full 40- or 64-character Git SHA')
    run_url = _https(run_url, 'run_url')
    snapshots = {
        'result': _snapshot(browser_result_path, 'browser result', private=True),
        'manifest': _snapshot(manifest_path, 'candidate release manifest'),
        'coverage': _snapshot(coverage_path, 'candidate coverage'),
        'budgets': _snapshot(budgets_path, 'CI budgets'),
    }
    coverage_row = _validate_coverage(snapshots['coverage'].value, code)
    descriptors = _validate_manifest(snapshots['manifest'].value, code)
    limits = _validate_budgets(snapshots['budgets'].value)
    checked = _validate_runner(
        snapshots['result'].value, code, commit, run_url, snapshots,
        coverage_row, descriptors, limits)
    evidence = {
        'schema_version': 1,
        'dataset': DATASET,
        'state': code,
        'profile': 'release',
        'status': 'green',
        'generated': checked['generated'],
        'run_url': run_url,
        'commit': commit,
        'browser_test': {
            'id': BROWSER_TEST_ID,
            'version': BROWSER_TEST_VERSION,
        },
        'browser': checked['browser'],
        'state_toggle': checked['state_toggle'],
        'statewide_browser_json': False,
        'runner_result': {
            'bytes': snapshots['result'].bytes,
            'sha256': snapshots['result'].sha256,
        },
        'release_inputs': {
            name: {'bytes': snapshots[name].bytes,
                   'sha256': snapshots[name].sha256}
            for name in ('manifest', 'coverage', 'budgets')
        },
        'descriptors': checked['descriptors'],
        'measurement_samples': checked['measurement_samples'],
        'measurements': checked['measurements'],
        'budget_limits': limits,
        'failures': checked['failures'],
        'effect': 'evidence_only_no_release_mutation',
    }
    validate_evidence_document(
        evidence, code, manifest_snapshot=snapshots['manifest'],
        coverage_snapshot=snapshots['coverage'],
        budgets_snapshot=snapshots['budgets'])
    raw = canonical_bytes(evidence)
    digest = sha256_bytes(raw)
    publish_dir = _publish_root(publish_dir)
    path = _safe_publish_path(publish_dir, code, digest)
    if before_commit is not None:
        before_commit(snapshots)
    _verify_unchanged(snapshots)
    _atomic_install(path, raw)
    _verify_unchanged(snapshots)
    checked_evidence = validate_release_evidence_file(
        path, code, manifest_path=manifest_path, coverage_path=coverage_path,
        budgets_path=budgets_path, expected_sha=digest)
    if checked_evidence != evidence:
        raise AcceptanceError('published CI evidence differs after installation')
    relative = os.path.relpath(path, os.path.realpath(SITE)).replace(os.sep, '/')
    acceptance = {
        'evidence_artifact': relative,
        'sha256': digest,
        'bytes': len(raw),
        'run_url': run_url,
        'commit': commit,
        'state_toggle_green': True,
        'statewide_browser_json': False,
        'heap_mb': evidence['measurements']['heap_mb'],
        'bulk_origin_storage_mb':
            evidence['measurements']['bulk_origin_storage_mb'],
    }
    return {
        'state': code,
        'evidence_artifact': relative,
        'sha256': digest,
        'bytes': len(raw),
        'acceptance': acceptance,
        'descriptors': len(descriptors),
        'effect': 'evidence_only_no_release_mutation',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-result', required=True,
                        help='private strict-JSON output from the browser runner')
    parser.add_argument('--manifest', required=True,
                        help='exact candidate release manifest tested by the browser')
    parser.add_argument('--coverage', required=True,
                        help='exact candidate coverage JSON tested by the browser')
    parser.add_argument('--state', required=True,
                        help='uppercase candidate state code')
    parser.add_argument('--commit', required=True,
                        help='full Git commit SHA expected in the browser result')
    parser.add_argument('--run-url', required=True,
                        help='exact HTTPS CI run URL expected in the browser result')
    parser.add_argument('--budgets', default=BUDGETS,
                        help='budget file used by the run (default: ci/budgets.json)')
    parser.add_argument('--publish-dir', default=DEFAULT_PUBLISH,
                        help='directory below site/map-assets/releases/')
    args = parser.parse_args(argv)
    try:
        result = build(
            browser_result_path=args.browser_result,
            manifest_path=args.manifest,
            coverage_path=args.coverage,
            state=args.state,
            commit=args.commit,
            run_url=args.run_url,
            budgets_path=args.budgets,
            publish_dir=args.publish_dir,
        )
    except AcceptanceError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
