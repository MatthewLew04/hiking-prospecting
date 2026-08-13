#!/usr/bin/env python3
"""CA Mines Online (MOL) → stategeo_ca.json — California's state mine layer.

CGS/DOC Mines Online is the SMARA-era inventory: mine name, status,
operator, lead agency, report year. It plays the role the state-survey
databases (IGS DD-1, DOGAMI MILO…) play elsewhere: better-curated,
currently-tracked mines to sit beside legacy MRDS. Commodity is not in the
service schema — group defaults to other/unknown; MRDS carries CA
commodities.

Output matches stategeo_{st}.json so the map's blue layer + county engines
consume it unchanged.
"""
import json

from common import arcgis_query, write_build_input, TODAY

BASE = 'https://gis.conservation.ca.gov/server/rest/services/MOL/MOLMines/FeatureServer'


def run():
    cols = {'src': 'stategeo', 'state': 'CA',
            'source': 'CGS/DOC Mines Online (MOL) — SMARA mine inventory',
            'retrieved': TODAY, 'n': 0,
            'id': [], 'nm': [], 'c': [], 'ty': [], 'stx': [], 'g': [], 'x': [], 'y': []}
    seen = set()
    for f in arcgis_query(BASE, 0, {'where': '1=1', 'outFields': '*',
                                    'returnGeometry': 'false'}, ttl_days=30):
        a = {k.lower(): v for k, v in f['attributes'].items()}
        mid = a.get('mine_id')
        if not mid or mid in seen:
            continue
        lon, lat = a.get('longitude'), a.get('latitude')
        if lon is None or lat is None or not (-125 <= lon <= -113 and 32 <= lat <= 42.5):
            continue
        seen.add(mid)
        status = a.get('minestatus') or a.get('rec_status') or ''
        yr = a.get('reportyear')
        cols['id'].append(str(mid))
        cols['nm'].append(a.get('minename') or '(unnamed)')
        cols['c'].append('')                       # commodity not in MOL schema
        cols['ty'].append('surface/underground mine (SMARA)'
                          + (f' · reported {yr}' if yr else ''))
        cols['stx'].append(str(status))
        cols['g'].append(5)
        cols['x'].append(round(lon, 5))
        cols['y'].append(round(lat, 5))
    cols['n'] = len(cols['id'])
    write_build_input('sites', 'stategeo_ca', cols)
    from collections import Counter
    print(f'CA MOL mines: {cols["n"]:,}; status:', dict(Counter(cols['stx']).most_common(6)))
    return cols


if __name__ == '__main__':
    run()
