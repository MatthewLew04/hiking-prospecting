#!/usr/bin/env python3
"""Compile reviewed state YAML into stdlib-only Lambda/browser metadata."""
from __future__ import annotations

import json
import os
import sys

from state_registry import load_states, validate_registry

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
OUT = os.path.join(ROOT, 'infra', 'state_runtime.json')


def build():
    result = validate_registry()
    if not result['ok']:
        raise ValueError('\n'.join(result['errors']))
    states = {}
    for code, row in load_states().items():
        states[code] = {
            'name': row['name'], 'regime': row['regime'], 'phase': row['phase'],
            # Lambda needs compact numeric bboxes, not review-only ids/notes.
            'query_envelopes': [item['bbox'] for item in row['query_envelopes']],
            'claim_systems': [s['id'] for s in row['claim_systems']],
            'occurrence_backbone': (row.get('occurrence_backbone') or {}).get('source_id'),
            'release': row['release'],
        }
    return {'schema_version': 1, 'states': states}


if __name__ == '__main__':
    try:
        data = build()
    except ValueError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
    with open(OUT, 'w') as fh:
        json.dump(data, fh, indent=2)
        fh.write('\n')
    print(f'wrote {OUT}')
