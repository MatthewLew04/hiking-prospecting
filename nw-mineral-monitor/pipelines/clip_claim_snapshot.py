#!/usr/bin/env python3
"""Repair a legacy columnar claim snapshot with the official state clip.

This is a compatibility migration for pre-PMTiles browser snapshots. New
national runs clip in Lambda before landing in private staging.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'infra')))
from spatial_clip import StateClipIndex

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')


def clip(data, geometry, source_rows=None):
    old_n = data['n']
    index = StateClipIndex(geometry)
    keep = [i for i, (x, y) in enumerate(zip(data['x'], data['y']))
            if x is not None and y is not None and index.contains(x, y)]
    for key, value in list(data.items()):
        if isinstance(value, list) and len(value) == old_n:
            data[key] = [value[i] for i in keep]
    data['n'] = len(keep)
    previous = data.get('spatial_clip') or {}
    source_rows = int(source_rows or previous.get('rows_before') or old_n)
    data['spatial_clip'] = {
        'authority': 'U.S. Census Bureau TIGERweb, January 1 2025 vintage',
        'method': 'claim-polygon centroid within authoritative state polygon',
        'rows_before': source_rows,
        'rows_removed': source_rows - len(keep),
    }
    if data.get('layer') == 'closed' and data.get('truncated') and len(keep) < old_n:
        data['partial_after_spatial_clip'] = True
        data['partial_note'] = (
            'Legacy capped envelope snapshot was clipped to the state; a fresh '
            'state-clipped MLRS pull must page onward to refill the cap.')
    return data


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('state')
    ap.add_argument('mode', choices=('active', 'closed'))
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--source-rows', type=int)
    args = ap.parse_args(argv)
    code = args.state.upper()
    path = os.path.join(ROOT, 'site', 'data', 'claims',
                        f'{code.lower()}_{args.mode}.json')
    clips = json.load(open(CLIPS, encoding='utf-8'))['states']
    if code not in clips:
        print(f'ERROR: no official clip for {code}', file=sys.stderr)
        return 1
    data = json.load(open(path, encoding='utf-8'))
    original_n = data['n']
    result = clip(data, clips[code], args.source_rows)
    if args.check:
        ok = result['n'] == original_n
        print(json.dumps({'state': code, 'mode': args.mode,
                          'rows': result['n'], 'would_remove': original_n-result['n']}))
        return 0 if ok else 1
    temp = path + '.tmp'
    with open(temp, 'w', encoding='utf-8') as fh:
        json.dump(result, fh, separators=(',', ':'))
    os.replace(temp, path)
    print(json.dumps({'state': code, 'mode': args.mode,
                      'rows_before': original_n, 'rows_after': result['n'],
                      'rows_removed': original_n-result['n']}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
