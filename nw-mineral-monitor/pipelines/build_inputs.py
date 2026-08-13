#!/usr/bin/env python3
"""Strict access to repository-private, non-browser build inputs.

Large legacy columnar snapshots are inputs to PMTiles and offline research
jobs.  They intentionally live outside ``site/`` so a static-site upload can
never publish them by accident.  Paths in ``build-inputs/manifest.json`` are
relative to the private ``build-inputs/`` root, never to the browser root.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
BUILD_INPUTS = os.path.join(ROOT, 'build-inputs')
MANIFEST = os.path.join(BUILD_INPUTS, 'manifest.json')
SCHEMA_VERSION = 1
SECTIONS = ('sites', 'claims', 'boundaries')
SITE_KEY = re.compile(r'^(mrds|stategeo|usmin)_([a-z]{2})$')
CLAIM_KEY = re.compile(r'^([a-z]{2})_(active|closed)$')
BOUNDARY_KEY = re.compile(r'^(states|counties)$')


class BuildInputError(ValueError):
    """The private build-input inventory or one of its paths is invalid."""


def _reject_constant(value):
    raise BuildInputError(f'non-standard JSON number {value}')


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BuildInputError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def canonical_relative(section, key):
    if section not in SECTIONS:
        raise BuildInputError(f'unknown build-input section {section!r}')
    pattern = (SITE_KEY if section == 'sites' else
               CLAIM_KEY if section == 'claims' else BOUNDARY_KEY)
    if not isinstance(key, str) or pattern.fullmatch(key) is None:
        raise BuildInputError(f'invalid build-input key {section}.{key}')
    return f'data/{section}/{key}.json'


def artifact_path(section, key, entry=None, *, root=None, require_file=True):
    """Resolve one canonical input without allowing traversal or aliases."""
    root = os.path.realpath(root or BUILD_INPUTS)
    expected = canonical_relative(section, key)
    if entry is not None:
        if not isinstance(entry, dict) or entry.get('file') != expected:
            got = entry.get('file') if isinstance(entry, dict) else None
            raise BuildInputError(
                f'{section}.{key}.file must be {expected!r}, got {got!r}')
    section_root = os.path.realpath(os.path.join(root, 'data', section))
    path = os.path.realpath(os.path.join(root, expected))
    if os.path.commonpath((section_root, path)) != section_root:
        raise BuildInputError(f'{section}.{key} escapes private build-input root')
    if require_file and not os.path.isfile(path):
        raise BuildInputError(f'{section}.{key} input is missing: {expected}')
    return path


def _validate(manifest, *, root, require_files):
    if not isinstance(manifest, dict):
        raise BuildInputError('build-input manifest must be an object')
    if set(manifest) != {
            'schema_version', 'sites', 'claims', 'boundaries', 'totals'}:
        raise BuildInputError(
            'build-input manifest keys must be exactly schema_version, sites, '
            'claims, boundaries, totals')
    if manifest.get('schema_version') != SCHEMA_VERSION:
        raise BuildInputError(
            f'build-input schema_version must be {SCHEMA_VERSION}')
    computed = {'sites': 0, 'claims_active': 0, 'claims_closed': 0,
                'boundary_states': 0, 'boundary_counties': 0}
    for section in SECTIONS:
        entries = manifest.get(section)
        if not isinstance(entries, dict):
            raise BuildInputError(f'build-input {section} must be an object')
        for key, entry in entries.items():
            canonical_relative(section, key)
            if not isinstance(entry, dict):
                raise BuildInputError(f'build-input {section}.{key} must be an object')
            n = entry.get('n')
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise BuildInputError(
                    f'build-input {section}.{key}.n must be a nonnegative integer')
            artifact_path(
                section, key, entry, root=root, require_file=require_files)
            total_key = ('sites' if section == 'sites' else
                         f'claims_{key.rsplit("_", 1)[1]}'
                         if section == 'claims' else f'boundary_{key}')
            computed[total_key] += n
    totals = manifest.get('totals')
    if not isinstance(totals, dict) or set(totals) != set(computed):
        raise BuildInputError(
            'build-input totals must contain exactly sites, claims_active, claims_closed')
    for key, expected in computed.items():
        if totals.get(key) != expected:
            raise BuildInputError(
                f'build-input totals.{key}={totals.get(key)!r}, computed {expected}')
    return manifest


def load_manifest(path=None, *, root=None, require_files=True):
    path = path or MANIFEST
    root = root or os.path.dirname(path)
    try:
        with open(path, encoding='utf-8') as source:
            manifest = json.load(
                source, parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildInputError(f'cannot read build-input manifest {path}: {exc}') from exc
    return _validate(manifest, root=root, require_files=require_files)


def load_artifact(section, key, *, manifest=None, root=None):
    """Load one declared private artifact and check its declared row count."""
    root = root or BUILD_INPUTS
    manifest = manifest or load_manifest(root=root)
    try:
        entry = manifest[section][key]
    except KeyError as exc:
        raise BuildInputError(f'undeclared build input {section}.{key}') from exc
    path = artifact_path(section, key, entry, root=root)
    try:
        with open(path, encoding='utf-8') as source:
            payload = json.load(
                source, parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildInputError(f'{section}.{key} is not strict JSON: {exc}') from exc
    if not isinstance(payload, dict) or payload.get('n') != entry['n']:
        got = payload.get('n') if isinstance(payload, dict) else None
        raise BuildInputError(
            f'{section}.{key} artifact n={got!r}, manifest n={entry["n"]}')
    return payload


def write_manifest(manifest, path=None, *, root=None):
    """Validate and atomically write a private inventory."""
    path = path or MANIFEST
    root = root or os.path.dirname(path)
    _validate(manifest, root=root, require_files=True)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
    handle, pending = tempfile.mkstemp(prefix='.build-input-manifest-', dir=directory)
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def write_artifact(section, key, payload, *, entry_fields=None,
                   manifest_path=None, root=None):
    """Atomically replace one private snapshot and update its inventory row."""
    if not isinstance(payload, dict):
        raise BuildInputError(f'{section}.{key} payload must be an object')
    n = payload.get('n')
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise BuildInputError(f'{section}.{key} payload n must be nonnegative')
    root = root or BUILD_INPUTS
    manifest_path = manifest_path or os.path.join(root, 'manifest.json')
    manifest = load_manifest(manifest_path, root=root, require_files=True)
    destination = artifact_path(
        section, key, root=root, require_file=False)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    handle, pending = tempfile.mkstemp(
        prefix=f'.{key}-', suffix='.json', dir=os.path.dirname(destination))
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(payload, output, separators=(',', ':'), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, destination)
    except Exception:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise
    old = manifest[section].get(key, {})
    entry = dict(old)
    entry.update(entry_fields or {})
    entry.update({'n': n, 'file': canonical_relative(section, key)})
    if payload.get('retrieved') is not None:
        entry['retrieved'] = payload['retrieved']
    manifest[section][key] = entry
    manifest['totals'] = {
        'sites': sum(item['n'] for item in manifest['sites'].values()),
        'claims_active': sum(
            item['n'] for name, item in manifest['claims'].items()
            if name.endswith('_active')),
        'claims_closed': sum(
            item['n'] for name, item in manifest['claims'].items()
            if name.endswith('_closed')),
        'boundary_states': manifest['boundaries'].get('states', {}).get('n', 0),
        'boundary_counties': manifest['boundaries'].get('counties', {}).get('n', 0),
    }
    write_manifest(manifest, manifest_path, root=root)
    return destination
