#!/usr/bin/env python3
"""Site-name gazetteer — resolve a mine by NAME without a viewport.

The ASK panel could always answer "how many gold sites in Idaho" and never
"what about the Center Star Mine". Every name-shaped lookup it had was
viewport-scoped: query_sites() reads loadedTileFeatures(), so a mine outside
the current PMTiles view simply does not exist, and resolve_place() only knows
the curated district/town index. A question naming a mine therefore parsed
down to its one grounded token — the state — and fell into the statewide
baseline-count branch, which answered a completely different question
("19,741 records in the ID archives") with total confidence.

This builds the missing index: every NAMED record in the immutable baseline
site archives (MRDS + the state surveys; USMIN carries feature types and quad
names, not site names, so it is not a gazetteer source), keyed for name search
and carrying the lat/lon that lets the rest of the tool set — mines_near,
geology_at, claims_at, faults_near, query_grades, docs_for — take over.

Sharded per state because nearly every name question names one ("center star
mine in idaho"), so the common case fetches ~300 KB gzipped instead of 3 MB.

Output: site/data/gazetteer/index.json + site/data/gazetteer/<ST>.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import SITE, TODAY, write_json, update_manifest, BUILD_INPUTS

# USMIN is deliberately absent: its rows are topo-map feature types (Adit,
# Shaft, Prospect Pit) plus a quadrangle name, never a site name. Indexing it
# would make every "adit" query match 121,193 Nevada rows by their type word.
GAZETTEER_SOURCES = ('mrds', 'stategeo')

# MRDS packs status into a two-letter code; the state surveys already carry a
# spelled-out status in `stx`. Normalise to one human string so the frontend
# and the model read the same vocabulary.
MRDS_STATUS = {
    'PP': 'past producer', 'PR': 'producer', 'OC': 'occurrence',
    'P': 'prospect', 'PL': 'processing plant', 'EX': 'explored prospect',
    'UN': 'unknown',
}


def _column(record, key, n):
    """A column that a source may not carry at all, padded to n."""
    values = record.get(key)
    if not isinstance(values, list) or len(values) != n:
        return [''] * n
    return values


def collect():
    """Group every named baseline site record by state."""
    manifest = json.load(open(os.path.join(BUILD_INPUTS, 'manifest.json')))
    sites = manifest.get('sites', {})
    by_state = {}
    provenance = {}
    for key in sorted(sites):
        source = key.split('_')[0]
        if source not in GAZETTEER_SOURCES:
            continue
        entry = sites[key]
        record = json.load(open(os.path.join(BUILD_INPUTS, entry['file'])))
        state = record['state']
        n = record['n']
        names = _column(record, 'nm', n)
        commodities = _column(record, 'c', n)
        mrds_status = _column(record, 'st', n)
        survey_status = _column(record, 'stx', n)
        districts = _column(record, 'd', n)
        kinds = _column(record, 'ty', n)
        ids = _column(record, 'id', n)
        bucket = by_state.setdefault(state, [])
        kept = 0
        for i in range(n):
            name = (names[i] or '').strip()
            # An unnamed row cannot be resolved by name, and "Unnamed
            # prospect" is a placeholder rather than a name — keep it out of
            # the index so it can never win a fuzzy match.
            if not name or name.lower().startswith('unnamed'):
                continue
            status = (MRDS_STATUS.get((mrds_status[i] or '').strip().upper())
                      or (survey_status[i] or '').strip())
            # The state surveys pack "<workings> — <district>" into one field
            # and leave the workings half empty for a bare occurrence, which
            # arrives as a dangling "— Yreka district".
            context = ((districts[i] or '').strip()
                       or (kinds[i] or '').strip().lstrip('-—').strip())
            bucket.append({
                'nm': name,
                'c': (commodities[i] or '').strip(),
                'sx': status,
                'd': context,
                'src': GAZETTEER_SOURCES.index(source),
                'id': ids[i] or '',
                'x': round(float(record['x'][i]), 5),
                'y': round(float(record['y'][i]), 5),
            })
            kept += 1
        provenance.setdefault(state, {})[source] = {
            'records': n, 'named': kept,
            'retrieved': entry.get('retrieved', ''),
            'source': record.get('source', ''),
        }
        print(f'  {key:>16}  {kept:>6} named of {n:>6}')
    return by_state, provenance


def run():
    by_state, provenance = collect()
    if not by_state:
        raise SystemExit('no gazetteer sources found in build-inputs/manifest.json')
    index = {'built': TODAY, 'sources': list(GAZETTEER_SOURCES), 'states': {}}
    total = 0
    for state in sorted(by_state):
        rows = by_state[state]
        # Deterministic order so a rebuild with unchanged inputs is a no-op,
        # and so equal-scoring name matches always tie-break the same way.
        rows.sort(key=lambda r: (r['nm'].lower(), r['src'], r['id']))
        shard = {
            'src': 'gazetteer', 'state': state, 'built': TODAY,
            'n': len(rows), 'srcs': list(GAZETTEER_SOURCES),
            'provenance': provenance.get(state, {}),
        }
        for column in ('nm', 'c', 'sx', 'd', 'src', 'id', 'x', 'y'):
            shard[column] = [row[column] for row in rows]
        relative = f'data/gazetteer/{state}.json'
        write_json(relative, shard)
        index['states'][state] = {
            'n': len(rows), 'file': f'{state}.json',
            'bytes': os.path.getsize(os.path.join(SITE, relative)),
        }
        total += len(rows)
    index['n'] = total
    write_json('data/gazetteer/index.json', index)
    update_manifest('gazetteer', {
        'built': TODAY, 'n': total,
        'states': sorted(index['states']),
        'note': 'named MRDS + state-survey sites, searchable without a viewport',
    })
    print(f'gazetteer: {total:,} named sites across {len(index["states"])} states')


if __name__ == '__main__':
    run()
