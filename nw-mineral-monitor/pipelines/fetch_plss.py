#!/usr/bin/env python3
"""Fetch PLSS section polygons for the configured AOI from BLM CadNSDI.

Output: site/data/plss/{aoi}.json — compact GeoJSON FeatureCollection,
one feature per section: properties {id: FRSTDIVID, lab: 'T12S R22E Sec 14',
t,td,r,rd,s}. Also a lookup index id -> feature order for the client.
"""
import json, re, sys
from common import load_aoi, arcgis_query, envelope, cached_get, prov, write_json

P = 'https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer'


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    params = envelope(aoi['bbox'])
    params.update({'outFields': 'FRSTDIVID,FRSTDIVNO,PLSSID',
                   'returnGeometry': 'true', 'outSR': 4326, 'geometryPrecision': 5})
    feats, seen = [], set()
    for f in arcgis_query(P, 2, dict(params), ttl_days=90):
        at = f['attributes']
        fid = at.get('FRSTDIVID') or ''
        m = re.match(r'([A-Z]{2}\d{2})(\d{3})\d([NS])(\d{3})\d([EW])\dSN(\d{2,3})', fid)
        if not m or fid in seen: continue
        seen.add(fid)
        sm, t, td, r, rd, sec = m.groups()
        if sm != aoi['plss_state_meridian']: continue
        rings = (f.get('geometry') or {}).get('rings')
        if not rings: continue
        feats.append({'type': 'Feature',
                      'properties': {'id': fid, 't': int(t), 'td': td, 'r': int(r),
                                     'rd': rd, 's': int(sec[:2]),
                                     'lab': f'T{int(t)}{td} R{int(r)}{rd} Sec {int(sec[:2])}'},
                      'geometry': {'type': 'Polygon', 'coordinates': rings}})
    feats.sort(key=lambda f: (f['properties']['t'], f['properties']['r'], f['properties']['s']))
    out = {'type': 'FeatureCollection',
           'name': f'PLSS sections — {aoi["name"]}',
           'meridian': aoi['meridian_name'],
           'provenance': prov(P + '/2 (CadNSDI PLSS Section, bbox query)'),
           'features': feats}
    write_json(f'data/plss/{aoi["key"]}.json', out)
    print(f'{len(feats)} sections for {aoi["name"]}')
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
