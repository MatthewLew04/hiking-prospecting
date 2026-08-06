#!/usr/bin/env python3
"""WS1 (server side) — ingest files dropped into data-inbox/ and register
them as map layers. The browser drag-drop does the same job client-side;
this twin exists so headless/batch workflows (and the demo script) work.

Supported: .csv, .geojson/.json, .kml, .gpx  (XLSX/KMZ/SHP-zip: use the
browser ingest, which carries the parsers — keeping this script stdlib-only.)

Geometry detection, in order:
  1. lat/lon columns (many header spellings, incl. y/x, northing/easting-as-latlon)
  2. UTM easting/northing columns (+ zone column or AOI default zone)
  3. PLSS legal description anywhere in the row ("T12S R22E Sec 14") →
     section polygon from site/data/plss/{aoi}.json; the row is placed at the
     section CENTROID and carries the section id + polygon reference.

Output: site/data/userlayers/{slug}.geojson + registry.json entry.
"""
import csv, io, json, math, os, re, sys, xml.dom.minidom
from common import load_aoi, SITE, TODAY, parse_trs, frstdivid, write_json

INBOX = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'data-inbox'))
LATS = {'lat', 'latitude', 'y', 'lat_dd', 'latdd', 'lat_dec', 'dec_lat', 'ycoord', 'y_coord', 'northing_dd'}
LONS = {'lon', 'lng', 'long', 'longitude', 'x', 'lon_dd', 'londd', 'lon_dec', 'dec_long', 'dec_lon', 'xcoord', 'x_coord', 'easting_dd'}
EAST = {'easting', 'utm_e', 'utme', 'east', 'utm_easting', 'x_utm'}
NORTH = {'northing', 'utm_n', 'utmn', 'north', 'utm_northing', 'y_utm'}
ZONE = {'zone', 'utm_zone', 'utmz'}


def utm_to_ll(e, n, zone, northern=True):
    """Standard TM inverse (WGS84). Good to <1 m — plenty for research leads."""
    a, f = 6378137.0, 1 / 298.257223563
    k0, e2 = 0.9996, f * (2 - f)
    ep2 = e2 / (1 - e2)
    x = e - 500000.0
    y = n if northern else n - 10000000.0
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
           + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
           + (151 * e1 ** 3 / 96) * math.sin(6 * mu))
    sp, cp, tp = math.sin(phi), math.cos(phi), math.tan(phi)
    c1 = ep2 * cp * cp
    t1 = tp * tp
    n1 = a / math.sqrt(1 - e2 * sp * sp)
    r1 = a * (1 - e2) / (1 - e2 * sp * sp) ** 1.5
    d = x / (n1 * k0)
    lat = phi - (n1 * tp / r1) * (d * d / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * ep2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * ep2 + 24 * t1 * t1) * d ** 5 / 120) / cp
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    return math.degrees(lat), math.degrees(lon) + math.degrees(lon0) - 0  # lon already radians offset


def load_sections(aoi):
    p = json.load(open(os.path.join(SITE, f'data/plss/{aoi["key"]}.json')))
    idx = {}
    for f in p['features']:
        rings = f['geometry']['coordinates']
        xs = [pt[0] for r in rings for pt in r]; ys = [pt[1] for r in rings for pt in r]
        idx[f['properties']['id']] = {'lab': f['properties']['lab'],
                                      'cx': sum(xs) / len(xs), 'cy': sum(ys) / len(ys)}
    return idx


def sniff_row(row, hdrmap, aoi, secidx):
    """row(dict) -> (lon, lat, method, extra) or None"""
    lo = {k.lower().strip(): v for k, v in row.items() if k}
    for lk in LATS & set(hdrmap):
        for lnk in LONS & set(hdrmap):
            try:
                lat, lon = float(lo[lk]), float(lo[lnk])
                if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
                    return lon, lat, 'latlon', {}
            except (ValueError, TypeError): pass
    ek = next((k for k in EAST if k in hdrmap), None)
    nk = next((k for k in NORTH if k in hdrmap), None)
    if ek and nk:
        try:
            e, n = float(lo[ek]), float(lo[nk])
            zk = next((k for k in ZONE if k in hdrmap), None)
            zone = int(re.sub(r'\D', '', str(lo[zk]))) if zk and lo.get(zk) else aoi['utm_zone']
            if 100000 < e < 900000 and 3000000 < n < 9000000:
                lat, lon = utm_to_ll(e, n, zone)
                return lon, lat, f'utm z{zone}', {}
        except (ValueError, TypeError): pass
    blob = ' '.join(str(v) for v in row.values() if v)
    trs = parse_trs(blob)
    if trs:
        t, td, r, rd, s = trs
        fid = frstdivid(aoi['plss_state_meridian'], t, td, r, rd, s)
        sec = secidx.get(fid)
        if sec:
            return sec['cx'], sec['cy'], 'plss', {'plss_id': fid, 'plss_lab': sec['lab']}
        return None, None, 'plss-notfound', {'plss_query': f'T{t}{td} R{r}{rd} Sec {s}'}
    return None


def ingest_csv(path, aoi, secidx):
    rows = list(csv.DictReader(open(path, encoding='utf-8-sig', errors='replace')))
    if not rows: return None
    hdrmap = {k.lower().strip() for k in rows[0].keys() if k}
    feats, misses = [], 0
    for row in rows:
        hit = sniff_row(row, hdrmap, aoi, secidx)
        if not hit or hit[0] is None: misses += 1; continue
        lon, lat, method, extra = hit
        props = {k: v for k, v in row.items() if k}
        props['_geocode'] = method; props.update(extra)
        feats.append({'type': 'Feature', 'properties': props,
                      'geometry': {'type': 'Point', 'coordinates': [round(lon, 6), round(lat, 6)]}})
    return feats, misses


def ingest_geojson(path):
    j = json.load(open(path, encoding='utf-8'))
    if j.get('type') == 'FeatureCollection': return j['features'], 0
    if j.get('type') == 'Feature': return [j], 0
    return None


def _text(el):
    return ''.join(n.data for n in el.childNodes if n.nodeType == n.TEXT_NODE).strip()


def ingest_kml(path):
    doc = xml.dom.minidom.parse(path)
    feats = []
    for pm in doc.getElementsByTagName('Placemark'):
        name = next((_text(e) for e in pm.getElementsByTagName('name')), None)
        for co in pm.getElementsByTagName('coordinates'):
            pts = [p for p in _text(co).replace('\n', ' ').split(' ') if p.strip()]
            coords = [[float(c.split(',')[0]), float(c.split(',')[1])] for c in pts]
            if not coords: continue
            geom = ({'type': 'Point', 'coordinates': coords[0]} if len(coords) == 1 else
                    {'type': 'LineString', 'coordinates': coords})
            feats.append({'type': 'Feature', 'properties': {'name': name}, 'geometry': geom})
    return feats, 0


def ingest_gpx(path):
    doc = xml.dom.minidom.parse(path)
    feats = []
    for tag, gt in (('wpt', 'Point'), ('trkpt', None)):
        for el in doc.getElementsByTagName(tag):
            lat, lon = float(el.getAttribute('lat')), float(el.getAttribute('lon'))
            if tag == 'wpt':
                nm = next((_text(e) for e in el.getElementsByTagName('name')), None)
                feats.append({'type': 'Feature', 'properties': {'name': nm},
                              'geometry': {'type': 'Point', 'coordinates': [lon, lat]}})
    trk = [[float(p.getAttribute('lon')), float(p.getAttribute('lat'))]
           for p in doc.getElementsByTagName('trkpt')]
    if trk:
        feats.append({'type': 'Feature', 'properties': {'name': 'track'},
                      'geometry': {'type': 'LineString', 'coordinates': trk}})
    return feats, 0


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    secidx = load_sections(aoi)
    regpath = os.path.join(SITE, 'data/userlayers/registry.json')
    reg = json.load(open(regpath)) if os.path.exists(regpath) else {'layers': []}
    done = {l['file'] for l in reg['layers']}
    for fn in sorted(os.listdir(INBOX)):
        path = os.path.join(INBOX, fn)
        slug = re.sub(r'[^a-z0-9]+', '-', os.path.splitext(fn)[0].lower()).strip('-')
        outfn = f'{slug}.geojson'
        if outfn in done or fn.startswith('.'): continue
        ext = os.path.splitext(fn)[1].lower()
        try:
            got = (ingest_csv(path, aoi, secidx) if ext == '.csv' else
                   ingest_geojson(path) if ext in ('.geojson', '.json') else
                   ingest_kml(path) if ext == '.kml' else
                   ingest_gpx(path) if ext == '.gpx' else None)
        except Exception as e:                    # noqa: BLE001
            print(f'{fn}: FAILED — {e}'); continue
        if not got: print(f'{fn}: unsupported here (use the browser ingest)'); continue
        feats, misses = got
        fc = {'type': 'FeatureCollection', 'name': fn,
              'ingested': TODAY, 'rows_ungeocoded': misses, 'features': feats}
        write_json(f'data/userlayers/{outfn}', fc)
        reg['layers'].append({'file': outfn, 'source': fn, 'n': len(feats),
                              'misses': misses, 'added': TODAY})
        print(f'{fn}: {len(feats)} features ({misses} rows not geocoded)')
    write_json('data/userlayers/registry.json', reg)


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
