#!/usr/bin/env python3
"""WS7c — aeromagnetic/radiometric survey-index provenance layer.

The WS7 rasters (magnetic anomaly WMTS, aerorad K% WMS) stream straight
from USGS — no processing here. What this script fetches is the TRUST
layer: survey outline polygons with year / flight-line spacing / altitude,
so hovering anywhere on the mag overlay tells you how much to believe the
pixel under the cursor (a 1949 survey at 5-mile spacing and a 2022 Earth
MRI block at 200 m spacing look identical in a pretty raster).

Sources (both mrdata WFS, verified 2026-08-07):
  - services/airborne  ms:footprint — the USGS airborne-survey inventory
    (magnetic + radiometric legacy flights, CONUS-wide)
  - services/earthmri  ms:outlines  — Earth MRI acquisition outlines (the
    modern high-res blocks; these are the ones worth chasing as GeoTIFFs
    in WS7b)

Output: pipelines/cache/geophys/surveys.json (private regional research cache).
The browser trust layer is built nationally by build_geophys_pmtiles.py and
is published only as range-readable PMTiles.
"""
import json, os, re, sys, urllib.parse

from common import CACHE, cached_get, prov, TODAY

# full-AOI bbox incl. California
BBOX = (-125.0, 32.4, -103.9, 49.1)
WFS = 'https://mrdata.usgs.gov/services/{svc}'
MAXF = 4000


def wfs_features(svc, typename, notes):
    """Minimal GML 3.2 parse: one polygon outline + flat attributes per
    member. Stdlib-only, tolerant of MapServer's namespace habits."""
    url = (WFS.format(svc=svc) + '?service=WFS&request=GetFeature&version=2.0.0'
           f'&typenames={typename}&count={MAXF}'
           f'&bbox={BBOX[1]},{BBOX[0]},{BBOX[3]},{BBOX[2]},urn:ogc:def:crs:EPSG::4326')
    try:
        xml = cached_get(url, ttl_days=60)
    except Exception as e:                        # noqa: BLE001
        notes.append(f'{svc}/{typename} unavailable: {e}')
        return []
    out = []
    for m in re.finditer(r'<(?:ms|wfs):member>(.*?)</(?:ms|wfs):member>', xml, re.S):
        blob = m.group(1)
        attrs = {}
        for am in re.finditer(r'<ms:([A-Za-z0-9_]+)>([^<]{0,300})</ms:\1>', blob):
            attrs[am.group(1).lower()] = am.group(2).strip()
        pm = re.search(r'<gml:posList[^>]*>([^<]+)</gml:posList>', blob)
        if not pm:
            continue
        nums = pm.group(1).split()
        ring = []
        for i in range(0, len(nums) - 1, 2):
            # EPSG:4326 axis order = lat lon in GML 3.2
            ring.append((round(float(nums[i + 1]), 4), round(float(nums[i]), 4)))
        if len(ring) < 4:
            continue
        # cheap simplify: cap ring size
        if len(ring) > 120:
            step = len(ring) // 100 + 1
            ring = ring[::step] + [ring[0]]
        out.append({'attrs': attrs, 'ring': ring})
    return out


def run():
    notes = []
    surveys = []
    def clean(v):
        return None if v in (None, '', '-9999', '-9999.0') else v
    for f in wfs_features('airborne', 'ms:footprint', notes):
        a = f['attrs']
        alt_t = {'B': 'barometric', 'D': 'drape', 'AG': 'above ground'}.get(
            clean(a.get('altitude_t')) or '', clean(a.get('altitude_t')))
        surveys.append({
            'src': 'airborne',
            'nm': a.get('name') or a.get('survey') or '?',
            'yr': clean(a.get('year')) or clean(a.get('date_flown')),
            'flown': clean(a.get('date_flown')),
            'spacing': clean(a.get('spacing1')),          # as published (km typ.)
            'alt': clean(a.get('altitude1')),
            'alt_type': alt_t,
            'kind': a.get('type') or '?',                  # M/R/G/EM flags + drape code
            'has': ''.join(k.upper() for k in ('mag', 'rad', 'grav', 'em')
                           if (a.get(k) or '').upper().startswith('Y'))
                   or None,
            'lnkm': clean(a.get('lnkm')),
            'by': a.get('flown_by') or None,
            'pub': clean(a.get('pubid')),
            'g': f['ring'],
        })
    for f in wfs_features('earthmri', 'ms:outlines', notes):
        a = f['attrs']
        surveys.append({
            'src': 'earthmri',
            'nm': a.get('pname') or a.get('name') or a.get('title') or 'Earth MRI block',
            'yr': a.get('yearstart') or a.get('year') or None,
            'spacing': a.get('linespacing') or a.get('spacing') or None,
            'alt': a.get('height') or None,
            'kind': a.get('datatype') or a.get('method') or 'Earth MRI acquisition',
            'ref': a.get('weblink') or a.get('sciencebase') or a.get('url') or None,
            'g': f['ring'],
        })
    if not any(s['src'] == 'airborne' for s in surveys):
        notes.append('airborne footprints empty — attribute names may have shifted; '
                     'inspect one GetFeature response and update the attr map')
    out = {
        'generated': TODAY,
        'bbox': BBOX,
        'provenance': [prov(WFS.format(svc='airborne') + ' (ms:footprint)'),
                       prov(WFS.format(svc='earthmri') + ' (ms:outlines)')],
        'note': ('Survey trust layer for the WS7 rasters. Legacy inventory + Earth MRI '
                 'acquisition outlines; spacing/altitude fields carried verbatim where '
                 'the service provides them. Earth MRI blocks are the WS7b '
                 'high-res GeoTIFF candidates.'),
        'notes': notes,
        'n': len(surveys),
        'surveys': surveys,
    }
    path = os.path.join(CACHE, 'geophys', 'surveys.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as destination:
        json.dump(out, destination, separators=(',', ':'), allow_nan=False)
    print(f'wrote private cache {path} ({os.path.getsize(path):,} bytes)')
    by = {}
    for s in surveys:
        by[s['src']] = by.get(s['src'], 0) + 1
    print('surveys by source:', by)
    for n in notes:
        print('NOTE:', n)
    return out


if __name__ == '__main__':
    run()
