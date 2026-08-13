#!/usr/bin/env python3
"""Fetch USGS MRDS points for an AOI bbox via mrdata WFS → columnar site file.

Bridges the gap for states with no MRDS snapshot yet (CA first): writes the
same columnar shape the offline target engine reads from the private
``build-inputs`` inventory. For statewide rollout use the bulk MRDS dump
instead — this is the AOI-sized tool (WFS caps responses; we page by bbox).

Usage: python3 fetch_mrds_wfs.py <aoi_key>
"""
import json, re, sys

from common import load_aoi, cached_get, write_build_input, TODAY

WFS = 'https://mrdata.usgs.gov/services/mrds'
PAGE = 3000


def run(aoi_key):
    aoi = load_aoi(aoi_key)
    st = aoi['state']
    x0, y0, x1, y1 = aoi['bbox']
    cols = {'src': 'mrds', 'state': st, 'retrieved': TODAY, 'n': 0,
            'id': [], 'nm': [], 'st': [], 'g': [], 'c': [], 'd': [], 'x': [], 'y': []}
    # service exposes significance tiers, not one layer: mrds-high (named/
    # developed records) + mrds-low (occurrences)
    xml = ''
    for tn in ('ms:mrds-high', 'ms:mrds-low'):
        url = (f'{WFS}?service=WFS&request=GetFeature&version=2.0.0&typenames={tn}'
               f'&count={PAGE}&bbox={y0},{x0},{y1},{x1},urn:ogc:def:crs:EPSG::4326')
        try:
            xml += cached_get(url, ttl_days=60)
        except Exception as e:                    # noqa: BLE001
            print(f'{tn} unavailable: {e}')
    n_raw = 0
    for m in re.finditer(r'<(?:ms|wfs):member>(.*?)</(?:ms|wfs):member>', xml, re.S):
        blob = m.group(1)
        a = {am.group(1).lower(): am.group(2).strip()
             for am in re.finditer(r'<ms:([A-Za-z0-9_]+)>([^<]{0,400})</ms:\1>', blob)}
        pm = re.search(r'<gml:pos[^>]*>([-\d. ]+)</gml:pos>', blob)
        if not pm:
            continue
        lat, lon = (float(v) for v in pm.group(1).split()[:2])
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        n_raw += 1
        # status → the map's compact codes
        dev = (a.get('dev_stat') or a.get('dev_st') or '').lower()
        code = 'P' if 'producer' in dev and 'past' not in dev else \
               'PP' if 'past producer' in dev else \
               'PR' if 'prospect' in dev else 'OC'
        # this WFS carries commodity CODES in code_list ("HG AU SB") — join
        # comma-style so the engines' token matchers (HG/AU/AG/SB/AS) hit
        com = ', '.join((a.get('code_list') or '').split()) or \
              ', '.join(x for x in (a.get('commod1'), a.get('commod2'), a.get('commod3')) if x)
        cols['id'].append(a.get('dep_id') or a.get('mrds_id') or '')
        cols['nm'].append(a.get('site_name') or a.get('name') or '(unnamed)')
        cols['st'].append(code)
        cols['g'].append(5)                      # commodity group resolved client-side; 5 = other
        cols['c'].append(com)
        cols['d'].append(a.get('district') or '')
        cols['x'].append(round(lon, 5))
        cols['y'].append(round(lat, 5))
    cols['n'] = len(cols['id'])
    cols['note'] = (f'AOI-bbox extract via mrdata WFS ({aoi["name"]}) — NOT statewide. '
                    f'Commodity group g defaults to other/unknown pending the full snapshot.')
    write_build_input('sites', f'mrds_{st.lower()}', cols)
    print(f'{st}: {cols["n"]} MRDS sites in bbox (raw members {n_raw})')
    if cols['n'] >= PAGE:
        print('WARNING: hit WFS page cap — bbox needs tiling for full coverage')
    return cols


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else 'clearlake')
