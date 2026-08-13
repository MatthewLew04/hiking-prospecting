#!/usr/bin/env python3
"""Build the public WS11 state × DONE-gate coverage artifact."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile

from state_registry import GATE_KEYS, load_states, validate_registry

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
OUT = os.path.join(ROOT, 'site', 'data', 'coverage.json')


def build():
    check = validate_registry()
    if not check['ok']:
        raise ValueError('\n'.join(check['errors']))
    rows = []
    for code, state in load_states().items():
        gates = {}
        for key in GATE_KEYS:
            gate = state['done_gate'][key]
            gates[key] = {'status': gate['status'], 'evidence': gate['evidence']}
            for extra in ('artifacts', 'metrics', 'finding', 'reviewed'):
                if gate.get(extra) is not None:
                    gates[key][extra] = gate[extra]
        passed = all(g['status'] in ('pass', 'not_applicable') for g in gates.values())
        rows.append({
            'state': code, 'name': state['name'], 'phase': state['phase'],
            'regime': state['regime'], 'release': state['release']['status'],
            'enabled': bool(state['release']['enabled']),
            'baseline_visible': bool(state['release'].get('baseline_visible')),
            'gate_passed': passed, 'gates': gates,
        })
    return {
        'schema_version': 1,
        'scope': '49 states; Hawaii excluded',
        'gate_keys': list(GATE_KEYS),
        'summary': {
            'states': len(rows),
            'claim_states': sum(r['regime'] == 'claim' for r in rows),
            'non_claim_states': sum(r['regime'] == 'non_claim' for r in rows),
            'released': sum(r['enabled'] for r in rows),
            'gate_complete': sum(r['gate_passed'] for r in rows),
        },
        'states': rows,
    }


def encoded(obj):
    return (json.dumps(obj, indent=2, sort_keys=False) + '\n').encode()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args(argv)
    try:
        data = encoded(build())
    except ValueError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    if args.check:
        try:
            old = open(OUT, 'rb').read()
        except FileNotFoundError:
            print(f'ERROR: missing generated coverage artifact {OUT}', file=sys.stderr)
            return 1
        if old != data:
            print('ERROR: coverage.json is stale; run pipelines/build_coverage.py',
                  file=sys.stderr)
            return 1
        print('coverage.json is current')
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    handle, pending = tempfile.mkstemp(
        prefix='.coverage-', dir=os.path.dirname(OUT))
    try:
        if os.path.exists(OUT):
            os.fchmod(handle, stat.S_IMODE(os.stat(OUT).st_mode))
        with os.fdopen(handle, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(pending, OUT)
    finally:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
    print(f'wrote {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
