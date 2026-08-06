#!/usr/bin/env python3
"""Fetch ACTIVE + CLOSED mining-claim cases for the AOI with FULL attributes
(CSE_META legal descriptions included → exact section assignment, TRS labels).

Output: site/data/openground/{aoi}_claims.json
  {active: [...], closed: [...]} — each case:
  {ser, name, type, disp, acres, secs: [FRSTDIVID...], x, y}
Also probes the Solid_Minerals Locatables_Case_Disp service for disposition
DATES (the base GIS layers carry none) and joins them when available.
"""
import json, sys, urllib.parse
from common import (load_aoi, arcgis_query, envelope, sections_from_cse_meta,
                    prov, write_json, cached_get)

EP = 'https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer'
LOC = 'https://gis.blm.gov/nlsdb/rest/services/Solid_Minerals/Locatables_Case_Disp/MapServer'
TYPE_DECODE = {'384101': 'L', '384103': 'L', '384201': 'P', '384203': 'P',
               '384301': 'T', '384303': 'T', '384401': 'M', '384403': 'M'}


def centroid(rings):
    ring = rings[0]
    xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    return round(sum(xs) / len(xs), 5), round(sum(ys) / len(ys), 5)


def date_index(aoi):
    """CSE_NR -> CSE_DISP_DT (epoch ms) from Locatables_Case_Disp, if served."""
    idx = {}
    try:
        j = json.loads(cached_get(LOC + '?f=json', ttl_days=30))
        layers = j.get('layers') or []
        if not layers: return idx
        params = envelope(aoi['bbox'])
        params.update({'outFields': 'CSE_NR,CSE_DISP,CSE_DISP_DT',
                       'returnGeometry': 'false'})
        for lyr in [l['id'] for l in layers]:
            try:
                for f in arcgis_query(LOC, lyr, dict(params), ttl_days=7):
                    at = f['attributes']
                    if at.get('CSE_NR') and at.get('CSE_DISP_DT'):
                        idx[at['CSE_NR']] = at['CSE_DISP_DT']
            except Exception as e:               # noqa: BLE001 — layer optional
                print(f'  (Locatables layer {lyr} skipped: {str(e)[:80]})')
    except Exception as e:                       # noqa: BLE001 — service optional
        print(f'  (Locatables_Case_Disp unavailable: {str(e)[:80]})')
    print(f'  disposition dates found for {len(idx)} cases')
    return idx


def pull(layer, aoi, ttl):
    params = envelope(aoi['bbox'])
    params.update({'outFields': 'OBJECTID,CSE_NR,CSE_NAME,CSE_TYPE_NR,CSE_DISP,'
                                'RCRD_ACRS,CSE_META,LEG_CSE_NR,MC_PATENTED',
                   'returnGeometry': 'true', 'outSR': 4326, 'geometryPrecision': 5})
    rows, seen = [], set()
    for f in arcgis_query(EP, layer, dict(params), ttl_days=ttl):
        at = f['attributes']
        ser = at.get('CSE_NR')
        if not ser or ser in seen: continue
        seen.add(ser)
        rings = (f.get('geometry') or {}).get('rings')
        x, y = centroid(rings) if rings else (None, None)
        rows.append({'ser': ser, 'name': at.get('CSE_NAME'),
                     'type': TYPE_DECODE.get(str(at.get('CSE_TYPE_NR')), '?'),
                     'disp': at.get('CSE_DISP'), 'leg': at.get('LEG_CSE_NR') or None,
                     'acres': round(at['RCRD_ACRS'], 2) if at.get('RCRD_ACRS') else None,
                     'pat': at.get('MC_PATENTED'),
                     'secs': sorted(sections_from_cse_meta(at.get('CSE_META'))),
                     'x': x, 'y': y})
    return rows


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    print('pulling active cases…'); active = pull(1, aoi, ttl=1)
    print(f'  {len(active)} active')
    print('pulling closed cases…'); closed = pull(2, aoi, ttl=7)
    print(f'  {len(closed)} closed')
    dates = date_index(aoi)
    for r in closed + active:
        if r['ser'] in dates: r['disp_dt'] = dates[r['ser']]
    out = {'aoi': aoi['key'], 'provenance': prov(EP + ' layers 1+2 (bbox, full attrs)'),
           'active': active, 'closed': closed}
    write_json(f'data/openground/{aoi["key"]}_claims.json', out)
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
