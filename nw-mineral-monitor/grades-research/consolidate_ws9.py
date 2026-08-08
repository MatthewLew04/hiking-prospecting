#!/usr/bin/env python3
"""Consolidate WS9 agent extraction files into the two canonical curated-row
files the pipelines read:
    grades-research/rows_ca_r2.json   (grades_ca.py round 2)
    grades-research/rows_id_r2.json   (grades_id.py)
Adds per-chapter attribution for journal volumes (CJMG 29/49) and keeps
agent_out/ as workpapers. Idempotent."""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
AO = os.path.join(HERE, 'agent_out')

CA_FILES = ['rows_logan_b108.json', 'rows_cjmg29.json', 'rows_cjmg49.json',
            'rows_csmb_b78.json', 'rows_pp73.json']
ID_FILES = ['rows_igs_b11.json', 'rows_b528_pp97.json', 'rows_b877_b969f.json',
            'rows_ismir_r2.json', 'rows_igs_p49_p61.json', 'rows_id_mine.json']
SPLIT_BY_STATE = ['rows_pp610.json']            # CA + ID in one file
OPTIONAL_ID = ['rows_igs_p26.json']             # Rocky Bar, if extracted

def chapter(r):
    """Chapter attribution for journal volumes (src cite prefix)."""
    if r['src_key'] == 'cjmg29':
        if r.get('county') == 'Kern':
            return ('Tucker, W.B. & Sampson, R.J., Gold Resources of Kern '
                    'County')
        return ('Averill, C.V., Gold Deposits of the Redding and Weaverville '
                'Quadrangles')
    if r['src_key'] == 'cjmg49':
        return ('Wright, L.A., Stewart, R.M., Gay, T.E. & Hazenbush, G.C., '
                'Mines and Mineral Deposits of San Bernardino County')
    return None

def load(fn):
    p = os.path.join(AO, fn)
    if not os.path.exists(p):
        print(f'  (skip {fn} — not present)')
        return []
    rows = json.load(open(p))
    for r in rows:
        r['_file'] = fn
        ch = chapter(r)
        if ch:
            r['chapter'] = ch
    return rows

ca, idr = [], []
for f in CA_FILES:
    ca += load(f)
for f in ID_FILES:
    idr += load(f)
for f in SPLIT_BY_STATE:
    for r in load(f):
        (ca if r['state'] == 'CA' else idr).append(r)
for f in OPTIONAL_ID:
    idr += load(f)

for name, rows in (('rows_ca_r2.json', ca), ('rows_id_r2.json', idr)):
    for r in rows:
        assert r.get('quote') and r.get('name') and r.get('state'), r
        assert r.get('src_key') or (r.get('src_cite') and r.get('src_url')), \
            (name, r['name'])
    json.dump(rows, open(os.path.join(HERE, name), 'w'), indent=1)
    print(f'{name}: {len(rows)} rows')
