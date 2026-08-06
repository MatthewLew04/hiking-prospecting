#!/usr/bin/env python3
"""Stamp nv_closed/ut_closed into the manifest after fetch_closed_desc runs."""
import json, os

SITE = '/home/claude/nw/site'
man = json.load(open(f'{SITE}/data/manifest.json'))
for st in ('nv', 'ut'):
    p = f'{SITE}/data/claims/{st}_closed.json'
    if not os.path.exists(p):
        print(f'{st}_closed.json missing — skipped'); continue
    d = json.load(open(p))
    man.setdefault('claims', {})[f'{st}_closed'] = {
        'file': f'data/claims/{st}_closed.json', 'n': d['n'],
        'retrieved': d['retrieved'],
        **({'truncated': True, 'total_available': d['total_available']}
           if d.get('truncated') else {})}
    print(f'{st}_closed: n={d["n"]:,} of {d.get("total_available"):,}')
man['totals']['claims_closed'] = sum(
    v['n'] for k, v in man['claims'].items() if k.endswith('_closed'))
json.dump(man, open(f'{SITE}/data/manifest.json', 'w'), separators=(',', ':'))
print('claims_closed total:', f"{man['totals']['claims_closed']:,}")
