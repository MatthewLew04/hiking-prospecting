#!/usr/bin/env python3
"""Publish immutable evidence for an authoritative zero-state baseline count.

This is deliberately narrow. It never creates a map feature or changes a
registry/manifest/release flag. It proves that the current checksummed national
baseline explicitly records zero rows for one state and source layer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile

from state_registry import ALL_STATES


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
DEFAULT_PUBLISH = os.path.join(
    SITE, 'map-assets', 'releases', 'zero-inventory')
DATASET = 'ws11-state-zero-inventory'
SUPPORTED_LAYERS = {'faults'}
SHA_RE = re.compile(r'[0-9a-f]{64}')


class ZeroInventoryError(ValueError):
    pass


def _reject_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def _load(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
        value = json.loads(raw, parse_constant=_reject_constant,
                           object_pairs_hook=_reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ZeroInventoryError(f'{label} is not strict JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise ZeroInventoryError(f'{label} must be an object')
    return raw, value


def canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')


def _sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _inside(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == \
            os.path.realpath(root)
    except ValueError:
        return False


def _baseline(manifest, state, layer, site):
    site = os.path.realpath(site)
    if state not in ALL_STATES or state != state.upper():
        raise ZeroInventoryError('state must be an uppercase WS11 state code')
    if layer not in SUPPORTED_LAYERS:
        raise ZeroInventoryError('unsupported zero-inventory layer')
    entry = (manifest.get('national_baselines') or {}).get(layer)
    states = entry.get('states') if isinstance(entry, dict) else None
    coverage = entry.get('coverage') if isinstance(entry, dict) else None
    if (not isinstance(entry, dict) or entry.get('format') != 'pmtiles' or
            entry.get('source_layer') != layer or not isinstance(states, dict) or
            set(states) != set(ALL_STATES) or states.get(state) != 0 or
            any(not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in states.values()) or
            entry.get('n') != sum(states.values()) or
            not isinstance(coverage, dict) or coverage.get('states') != 49 or
            coverage.get('excluded_state_codes') != ['DC', 'HI', 'PR'] or
            coverage.get('zero_feature_states') != sorted(
                code for code, count in states.items() if count == 0) or
            not isinstance(entry.get('bytes'), int) or
            isinstance(entry.get('bytes'), bool) or entry['bytes'] <= 127 or
            not isinstance(entry.get('sha256'), str) or
            SHA_RE.fullmatch(entry['sha256']) is None or
            not isinstance(entry.get('file'), str)):
        raise ZeroInventoryError(
            f'national_baselines.{layer} does not authoritatively declare {state}=0')
    artifact = os.path.realpath(os.path.join(site, os.path.normpath(entry['file'])))
    if not _inside(artifact, site) or not os.path.isfile(artifact):
        raise ZeroInventoryError('national baseline artifact is missing/outside site')
    size, digest = _sha256_file(artifact)
    if size != entry['bytes'] or digest != entry['sha256']:
        raise ZeroInventoryError('national baseline bytes/checksum do not match manifest')
    return entry


def validate_document(value, state, layer, manifest, site):
    expected_keys = {
        'schema_version', 'dataset', 'state', 'source_layer', 'n', 'complete',
        'finding', 'baseline', 'effect',
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ZeroInventoryError('zero-inventory evidence keys are not exact')
    entry = _baseline(manifest, state, layer, site)
    baseline = value.get('baseline')
    if (value.get('schema_version') != 1 or value.get('dataset') != DATASET or
            value.get('state') != state or value.get('source_layer') != layer or
            value.get('n') != 0 or value.get('complete') is not True or
            value.get('effect') != 'evidence_only_no_feature_or_release_mutation' or
            not isinstance(value.get('finding'), str) or
            len(value['finding'].strip()) < 40 or not isinstance(baseline, dict) or
            set(baseline) != {
                'manifest_key', 'artifact', 'bytes', 'sha256', 'state_count'} or
            baseline.get('manifest_key') != f'national_baselines.{layer}' or
            baseline.get('artifact') != entry['file'] or
            baseline.get('bytes') != entry['bytes'] or
            baseline.get('sha256') != entry['sha256'] or
            baseline.get('state_count') != 0):
        raise ZeroInventoryError(
            'zero-inventory evidence does not bind the current baseline zero')
    return value


def validate_evidence_file(path, state, layer, *, manifest_path=MANIFEST,
                           expected_sha=None, site=SITE):
    site = os.path.realpath(site)
    raw, value = _load(path, 'zero-inventory evidence')
    digest = hashlib.sha256(raw).hexdigest()
    if (expected_sha is not None and
            (not isinstance(expected_sha, str) or
             SHA_RE.fullmatch(expected_sha) is None or expected_sha != digest)):
        raise ZeroInventoryError('zero-inventory evidence checksum mismatch')
    if os.path.basename(path) != f'{digest}.json' or raw != canonical_bytes(value):
        raise ZeroInventoryError('zero-inventory evidence is not canonical/content-addressed')
    _, manifest = _load(manifest_path, 'current release manifest')
    return validate_document(value, state, layer, manifest, site)


def build(state, layer='faults', *, manifest_path=MANIFEST,
          publish_dir=DEFAULT_PUBLISH, site=SITE):
    site = os.path.realpath(site)
    _, manifest = _load(manifest_path, 'current release manifest')
    entry = _baseline(manifest, state, layer, site)
    evidence = {
        'schema_version': 1,
        'dataset': DATASET,
        'state': state,
        'source_layer': layer,
        'n': 0,
        'complete': True,
        'finding': (
            f'The reviewed national {layer} inventory explicitly contains zero '
            f'features for {state}; no placeholder or fabricated geometry is emitted.'),
        'baseline': {
            'manifest_key': f'national_baselines.{layer}',
            'artifact': entry['file'],
            'bytes': entry['bytes'],
            'sha256': entry['sha256'],
            'state_count': 0,
        },
        'effect': 'evidence_only_no_feature_or_release_mutation',
    }
    validate_document(evidence, state, layer, manifest, site)
    raw = canonical_bytes(evidence)
    digest = hashlib.sha256(raw).hexdigest()
    release_root = os.path.realpath(os.path.join(site, 'map-assets', 'releases'))
    publish_dir = os.path.realpath(publish_dir)
    if not _inside(publish_dir, release_root):
        raise ZeroInventoryError('publish directory must be under map-assets/releases')
    state_dir = os.path.join(publish_dir, state.lower())
    if ((os.path.lexists(publish_dir) and os.path.islink(publish_dir)) or
            (os.path.lexists(state_dir) and os.path.islink(state_dir)) or
            not _inside(os.path.realpath(state_dir), release_root)):
        raise ZeroInventoryError('publish/state directory is a symlink or escapes releases')
    output = os.path.join(state_dir, f'{digest}.json')
    os.makedirs(state_dir, exist_ok=True)
    if os.path.lexists(output) and os.path.islink(output):
        raise ZeroInventoryError('zero-inventory evidence path must not be a symlink')
    descriptor, temporary = tempfile.mkstemp(
        prefix='.' + digest, suffix='.tmp', dir=os.path.dirname(output))
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        if os.path.exists(output):
            with open(output, 'rb') as existing:
                if existing.read() != raw:
                    raise ZeroInventoryError('content-addressed collision')
            os.unlink(temporary)
        else:
            os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    relative = os.path.relpath(output, site)
    return {
        'evidence_artifact': relative,
        'sha256': digest,
        'bytes': len(raw),
        'baseline_manifest_key': f'national_baselines.{layer}',
        'baseline_sha256': entry['sha256'],
        'state_count': 0,
        'complete': True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    parser.add_argument('--layer', choices=sorted(SUPPORTED_LAYERS), default='faults')
    parser.add_argument('--manifest', default=MANIFEST)
    parser.add_argument('--publish-dir', default=DEFAULT_PUBLISH)
    args = parser.parse_args(argv)
    print(json.dumps(build(
        args.state, args.layer, manifest_path=args.manifest,
        publish_dir=args.publish_dir), sort_keys=True))


if __name__ == '__main__':
    main()
