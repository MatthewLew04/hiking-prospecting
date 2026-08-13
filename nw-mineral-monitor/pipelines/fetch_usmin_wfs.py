#!/usr/bin/env python3
"""Statewide USMIN (topo-map mine features) via mrdata WFS → columnar file.

Same output shape as the existing usmin_{st}.json files (types/scales
dictionaries + per-point indices), so the map and county_gold consume it
unchanged. Pages with WFS 2.0 startIndex; points-high + points-low tiers.

Usage: python3 fetch_usmin_wfs.py CA
"""
import json, re, sys

from common import cached_get, write_build_input, TODAY

WFS = 'https://mrdata.usgs.gov/services/usmin'
PAGE = 5000
MAX_PAGES = 80
STATE_BBOX = {          # generous; mirrors lambda_updater
    'CA': (32.4, -124.5, 42.1, -114.0),
    'NV': (35.0, -120.1, 42.1, -114.0),
}


def run(st):
    st = st.upper()
    y0, x0, y1, x1 = STATE_BBOX[st]
    types, scales = [], []
    tidx, sidx = {}, {}
    cols = {'src': 'usmin', 'state': st,
            'retrieved': f'USMIN via mrdata WFS {TODAY}', 'n': 0,
            'types': types, 'scales': scales, 't': [], 'q': [], 'yr': [],
            'sc': [], 'x': [], 'y': []}
    seen = set()
    # the server ignores startIndex (same first page repeats) but honors
    # bbox+count — so page by TILING the state bbox, subdividing any tile
    # that hits the count cap
    def tiles(y0, x0, y1, x1, depth=0):
        yield from _tile_fetch(y0, x0, y1, x1, depth)

    def _tile_fetch(ty0, tx0, ty1, tx1, depth):
        url = (f'{WFS}?service=WFS&request=GetFeature&version=2.0.0'
               f'&typenames={tn}&count={PAGE}'
               f'&bbox={ty0},{tx0},{ty1},{tx1},urn:ogc:def:crs:EPSG::4326')
        xml = cached_get(url, ttl_days=90)
        members = re.findall(r'<(?:ms|wfs):member>(.*?)</(?:ms|wfs):member>', xml, re.S)
        if len(members) >= PAGE and depth < 3:
            mx, my = (tx0 + tx1) / 2, (ty0 + ty1) / 2
            for q in ((ty0, tx0, my, mx), (ty0, mx, my, tx1),
                      (my, tx0, ty1, mx), (my, mx, ty1, tx1)):
                yield from _tile_fetch(*q, depth + 1)
            return
        if len(members) >= PAGE:
            print(f'\nWARNING: tile cap at depth {depth} ({ty0},{tx0}) — undercounted')
        yield from members

    for tn in ('ms:points-high', 'ms:points-low'):
        # 1° tiles over the state bbox
        import math as _m
        lat_steps = range(int(_m.floor(y0)), int(_m.ceil(y1)))
        lon_steps = range(int(_m.floor(x0)), int(_m.ceil(x1)))
        cells = [(la, lo, la + 1, lo + 1) for la in lat_steps for lo in lon_steps]
        for ci, (a, b, c, d) in enumerate(cells):
            for blob in tiles(max(a, y0), max(b, x0), min(c, y1), min(d, x1)):
                a = {m.group(1).lower(): m.group(2).strip()
                     for m in re.finditer(r'<ms:([A-Za-z0-9_]+)>([^<]{0,200})</ms:\1>', blob)}
                if (a.get('state') or '').upper() != st:
                    continue
                pm = re.search(r'<gml:pos[^>]*>([-\d. ]+)</gml:pos>', blob)
                if not pm:
                    continue
                lat, lon = (float(v) for v in pm.group(1).split()[:2])
                key = (a.get('gda_id') or f'{lon:.5f},{lat:.5f},{a.get("ftr_type")}')
                if key in seen:
                    continue
                seen.add(key)
                ty = a.get('ftr_type') or 'Feature'
                if ty not in tidx:
                    tidx[ty] = len(types); types.append(ty)
                sc_raw = (a.get('topo_scale') or '').replace('1:', '').replace(',', '')
                sc = {'62500': '625k', '63360': '625k'}.get(sc_raw) or \
                     ((sc_raw[:-3] + 'k') if sc_raw.endswith('000') else (sc_raw or '?'))
                if sc not in sidx:
                    sidx[sc] = len(scales); scales.append(sc)
                yr = None
                m = re.search(r'(18|19|20)\d{2}', a.get('topo_date') or '')
                if m:
                    yr = int(m.group(0))
                cols['t'].append(tidx[ty])
                cols['q'].append(a.get('topo_name') or '?')
                cols['yr'].append(yr)
                cols['sc'].append(sidx[sc])
                cols['x'].append(round(lon, 5))
                cols['y'].append(round(lat, 5))
            print(f'  {tn} tile {ci + 1}/{len(cells)} (total {len(seen):,})', end='\r')
        print()
    cols['n'] = len(cols['x'])
    write_build_input('sites', f'usmin_{st.lower()}', cols)
    print(f'{st}: {cols["n"]:,} USMIN features, {len(types)} types, scales {scales}')
    return cols


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else 'CA')
