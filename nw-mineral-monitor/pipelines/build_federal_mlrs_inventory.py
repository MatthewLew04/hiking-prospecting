#!/usr/bin/env python3
"""Compile a strict private inventory for the 19-state federal MLRS build.

This tool never downloads data and never writes beneath ``site/``. It hashes
the 38 canonical updater snapshots, derives completeness from the updater's
machine-produced cursor/clip evidence, then runs the publication builder's
full row validator in progress mode before atomically installing
``inventory.json``. Operators cannot promote a checkpoint by editing a count
or boolean in an inventory by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import time

import build_federal_mlrs_pmtiles as mlrs


class InventoryError(ValueError):
    pass


def _read_snapshot(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise InventoryError(f'{label} cannot be read: {exc}') from exc
    try:
        data = mlrs._strict_json_bytes(raw, label)
    except mlrs.PublicationError as exc:
        raise InventoryError(str(exc)) from exc
    if not isinstance(data, dict):
        raise InventoryError(f'{label} must be a JSON object')
    return data, raw


def _partial_reasons(data, provenance_complete):
    reasons = []
    if not provenance_complete:
        reasons.append('producer pagination/clip attestation is absent or incomplete')
    for field in ('truncated', 'partial', 'partial_after_spatial_clip'):
        value = data.get(field)
        if value is not None and not isinstance(value, bool):
            raise InventoryError(f'snapshot {field} must be boolean')
        if value:
            reasons.append(field)
    total = data.get('total_available')
    n = data.get('n')
    if total is not None:
        if not mlrs._is_int(total):
            raise InventoryError('snapshot total_available must be nonnegative')
        if mlrs._is_int(n) and total > n:
            reasons.append('total_available exceeds emitted rows')
    return reasons


def compile_inventory(staging_dir, *, output=None, state_clips=None,
                      created=None):
    staging = os.path.realpath(staging_dir)
    if not os.path.isdir(staging) or not mlrs._outside(staging, mlrs.SITE):
        raise InventoryError('MLRS inventory staging must be a directory outside site/')
    output = os.path.realpath(output or os.path.join(staging, 'inventory.json'))
    if os.path.dirname(output) != staging or os.path.basename(output) != 'inventory.json':
        raise InventoryError('MLRS inventory output must be STAGING/inventory.json')
    state_clips = os.path.realpath(state_clips or mlrs.DEFAULT_STATE_CLIPS)
    clip_sha = mlrs._sha256_file(state_clips)
    registry = mlrs.load_states()
    codes = mlrs._registry_claim_states()
    context = {
        'clip_sha256': clip_sha,
        'query_envelope_counts': {
            code: len(registry[code]['query_envelopes']) for code in codes
        },
    }
    states = {}
    for code in codes:
        states[code] = {}
        for mode in mlrs.MODES:
            filename = f'{code.lower()}_{mode}.json'
            path = os.path.join(staging, filename)
            data, raw = _read_snapshot(path, f'{code}.{mode} snapshot')
            if data.get('state') != code or data.get('layer') != mode:
                raise InventoryError(f'{code}.{mode} snapshot identity is invalid')
            n = data.get('n')
            if not mlrs._is_int(n):
                raise InventoryError(f'{code}.{mode} snapshot n must be nonnegative')
            try:
                mlrs._valid_date(data.get('retrieved'), f'{code}.{mode}.retrieved')
                provenance_complete = mlrs._snapshot_provenance_complete(
                    context, code, mode, data)
            except mlrs.PublicationError as exc:
                raise InventoryError(str(exc)) from exc
            reasons = _partial_reasons(data, provenance_complete)
            entry = {
                'file': filename,
                'n': n,
                'bytes': len(raw),
                'sha256': mlrs._sha256_bytes(raw),
                'retrieved': data['retrieved'],
                'complete': not reasons,
            }
            if reasons:
                entry['partial_reason'] = '; '.join(reasons)
            states[code][mode] = entry

    created = created or time.strftime('%Y-%m-%d')
    try:
        mlrs._valid_date(created, 'inventory.created')
    except mlrs.PublicationError as exc:
        raise InventoryError(str(exc)) from exc
    inventory = {
        'schema_version': mlrs.SCHEMA_VERSION,
        'system': mlrs.SYSTEM,
        'source': mlrs.SOURCE,
        'created': created,
        'clip': {
            'authority': mlrs.CLIP_AUTHORITY,
            'method': mlrs.CLIP_METHOD,
            'artifact_sha256': clip_sha,
        },
        'states': states,
    }
    prior_mode = (stat.S_IMODE(os.stat(output).st_mode)
                  if os.path.exists(output) else 0o600)
    descriptor, pending = tempfile.mkstemp(
        prefix='.inventory-', suffix='.json', dir=staging)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(inventory, target, separators=(',', ':'), allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(pending, prior_mode)
        try:
            checked = mlrs.load_inventory(staging, pending, state_clips)
            with tempfile.TemporaryDirectory(prefix='nwmm-mlrs-inventory-') as work:
                layers = {mode: os.path.join(work, f'{mode}.geojsonseq')
                          for mode in mlrs.MODES}
                stats = mlrs.stream_snapshots(checked, layers, profile='progress')
                mlrs.assert_inputs_unchanged(checked, stats)
        except mlrs.PublicationError as exc:
            raise InventoryError(str(exc)) from exc
        os.replace(pending, output)
        pending = None
    finally:
        if pending and os.path.exists(pending):
            os.unlink(pending)
    return inventory


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Compile a private, producer-attested 19-state MLRS inventory')
    parser.add_argument('--staging-dir', required=True)
    parser.add_argument('--output')
    parser.add_argument('--state-clips', default=mlrs.DEFAULT_STATE_CLIPS)
    parser.add_argument('--created')
    args = parser.parse_args(argv)
    try:
        inventory = compile_inventory(
            args.staging_dir, output=args.output, state_clips=args.state_clips,
            created=args.created)
    except (InventoryError, OSError) as exc:
        parser.error(str(exc))
    complete = sum(
        row[mode]['complete'] for row in inventory['states'].values()
        for mode in mlrs.MODES)
    print(json.dumps({'inventory': args.output or os.path.join(
        os.path.realpath(args.staging_dir), 'inventory.json'),
                      'snapshots': 38, 'complete': complete,
                      'partial': 38 - complete}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
