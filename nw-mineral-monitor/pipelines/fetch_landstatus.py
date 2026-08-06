#!/usr/bin/env python3
"""Fetch land-status inputs for the open-ground compute:
- SMA (Surface Management Agency) generalized polygons intersecting the AOI
- withdrawal case polygons (Land_Tenure/Withdrawals_Case_Land_Status)
- segregation polygons (minerals + surface)
- NLCS Wilderness / WSA polygons

Everything is cached raw; the open_ground.py step consumes the cache and
assigns per-section status. SMA is the *LimitedScale generalized* service —
boundary-adjacent sections carry real uncertainty (see ASSUMPTIONS.md).
"""
import json, sys, urllib.parse
from common import load_aoi, envelope, cached_get, arcgis_query, write_json, prov

SMA = 'https://gis.blm.gov/arcgis/rest/services/lands/BLM_Natl_SMA_LimitedScale/MapServer'
WDL = 'https://gis.blm.gov/nlsdb/rest/services/Land_Tenure/Withdrawals_Case_Land_Status/MapServer'
SEG = 'https://gis.blm.gov/nlsdb/rest/services/Land_Tenure/Segregations_Lands_Minerals_Both/MapServer'
WSA = 'https://gis.blm.gov/arcgis/rest/services/lands/BLM_Natl_NLCS_WLD_WSA/MapServer'


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    out = {'aoi': aoi['key'], 'sma': [], 'withdrawals': [], 'seg_min': [],
           'seg_sur': [], 'wsa': [], 'provenance': {}}

    # SMA — one shot (few generalized features), heavier simplification is fine
    p = envelope(aoi['bbox'])
    p.update({'where': '1=1', 'outFields': 'ADMIN_AGENCY_CODE,ADMIN_DEPT_CODE,ADMIN_UNIT_TYPE',
              'returnGeometry': 'true', 'outSR': 4326, 'geometryPrecision': 4,
              'maxAllowableOffset': '0.001', 'f': 'json'})
    j = json.loads(cached_get(SMA + '/1/query?' + urllib.parse.urlencode(p), ttl_days=90))
    for f in j.get('features', []):
        out['sma'].append({'agency': f['attributes'].get('ADMIN_AGENCY_CODE'),
                           'dept': f['attributes'].get('ADMIN_DEPT_CODE'),
                           'unit': f['attributes'].get('ADMIN_UNIT_TYPE'),
                           'rings': (f.get('geometry') or {}).get('rings') or []})
    out['provenance']['sma'] = prov(SMA + '/1 (LimitedScale — generalized!)')
    print(f'SMA polygons: {len(out["sma"])}')

    def pull_cases(base, layer, key, fields, keep=None):
        p = envelope(aoi['bbox'])
        p.update({'outFields': fields, 'returnGeometry': 'true',
                  'outSR': 4326, 'geometryPrecision': 5})
        n = 0
        for f in arcgis_query(base, layer, dict(p), ttl_days=30):
            at = f['attributes']
            out[key].append({'attrs': {k: v for k, v in at.items()
                                       if (keep is None or k in keep) and v not in (None, '')},
                             'rings': (f.get('geometry') or {}).get('rings') or []})
            n += 1
        print(f'{key}: {n}')

    KEEP = {'CSE_NR', 'CSE_NAME', 'CSE_TYPE_NR', 'CSE_DISP', 'CSE_LND_STATUS',
            'SEG_MIN', 'SEG_SUR', 'CSE_DISP_DT', 'DOC_TYPE', 'DOC_NR', 'US_RIGHTS'}
    pull_cases(WDL, 0, 'withdrawals', '*', KEEP)
    pull_cases(SEG, 0, 'seg_min', '*', KEEP)
    pull_cases(SEG, 1, 'seg_sur', '*', KEEP)
    for lyr, nm in ((0, 'Wilderness'), (1, 'WSA')):
        p = envelope(aoi['bbox'])
        p.update({'outFields': 'OBJECTID,NLCS_NAME', 'returnGeometry': 'true',
                  'outSR': 4326, 'geometryPrecision': 5})
        try:
            for f in arcgis_query(WSA, lyr, dict(p), ttl_days=90):
                out['wsa'].append({'name': f['attributes'].get('NLCS_NAME'), 'kind': nm,
                                   'rings': (f.get('geometry') or {}).get('rings') or []})
        except Exception as e:                    # noqa: BLE001
            print(f'  ({nm} skipped: {str(e)[:80]})')
    print(f'wsa/wld: {len(out["wsa"])}')
    out['provenance']['withdrawals'] = prov(WDL + '/0')
    out['provenance']['segregations'] = prov(SEG + '/0,1')
    out['provenance']['wsa'] = prov(WSA + '/0,1')

    import os
    from common import HERE
    path = os.path.join(HERE, 'cache', f'landstatus_{aoi["key"]}.json')
    json.dump(out, open(path, 'w'), separators=(',', ':'))
    print(f'cached -> {path} ({os.path.getsize(path):,} bytes)')
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
