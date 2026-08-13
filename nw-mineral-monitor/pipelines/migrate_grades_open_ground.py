#!/usr/bin/env python3
"""One-time/idempotent migration from legacy numeric open to typed status."""
import json
import os

from state_registry import CLAIM_STATES

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PATH = os.path.join(ROOT, 'site', 'data', 'grades', 'grades.json')


def migrate(data):
    rows = []
    for state, value in zip(data['st'], data['open']):
        if state not in CLAIM_STATES:
            rows.append({'status': 'not_applicable', 'distance_m': None,
                         'score': None,
                         'reason': 'No federal or state staking system applies.'})
        elif value is None or value < 0:
            rows.append({'status': 'unknown', 'distance_m': None, 'score': None,
                         'reason': 'Legacy row lacked measured active-claim distance.'})
        else:
            rows.append({'status': 'measured', 'distance_m': value, 'score': None,
                         'source': f'private build input claims.{state.lower()}_active'})
    data['open_ground'] = rows
    return data


if __name__ == '__main__':
    data = migrate(json.load(open(PATH)))
    with open(PATH, 'w') as fh:
        json.dump(data, fh, separators=(',', ':'))
    print(f'migrated {len(data["open_ground"]):,} grade rows')
