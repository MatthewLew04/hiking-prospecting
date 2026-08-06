#!/usr/bin/env python3
"""WS6a — geologic map ingest for the AOI (sinter-first targeting feedstock).

Best-available fallback order (per the WS6 spec), with source + scale
recorded per polygon so every target card can cite its map:

  1. Macrostrat harmonized vector polygons (macrostrat.org, CC-BY). The
     carto tileset serves the BEST scale available per area (it harmonizes
     IGS quads, SGMC 1:500k, and the 1:5M NA map into one layer), and the
     API returns each unit's full geometry + verbatim description + a
     citation for its source map. Mechanics: enumerate carto vector tiles
     over the bbox at z10 with a ~150-line pure-stdlib MVT decoder (no pip
     deps, matching the repo rule) JUST to learn which map_ids + line
     geometries exist, then pull clean unclipped unit polygons +
     attributes from /api/v2/geologic_units/map?map_id=… in batches.
     Fault/contact LINES come straight from the tiles (clip seams don't
     matter for distance math).
  2. USGS SGMC via mrdata WFS — fallback if Macrostrat is down (SGMC is
     itself one of Macrostrat's sources, so normally redundant).
  3. IGS ArcGIS REST — attempted historically, 502s from this sandbox
     (see ASSUMPTIONS.md); IGS content reaches us through Macrostrat.
  Rasters (scanned NGMDB quads) are future work — noted, not fetched.

Also fetched here because the target engine scores with them:
  - USGS NSHM hazfaults2014 (Quaternary faults, ArcGIS) — range-front
    structures with names + slip sense;
  - GNIS named springs in the bbox (hot/warm classified from the name);
  - IDWR geothermal wells if the service is discoverable (skipped with an
    honest note if not).

Output: site/data/geology/{aoi}.json
"""
import json, math, os, struct, sys, urllib.parse

from common import load_aoi, cached_get, arcgis_query, envelope, prov, write_json, TODAY

MS = 'https://macrostrat.org/api/v2'
TILES = 'https://tiles.macrostrat.org/carto/{z}/{x}/{y}.mvt'
ZOOM = 10
HAZFAULTS = 'https://earthquake.usgs.gov/arcgis/rest/services/haz/hazfaults2014/MapServer'
GNIS = 'https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer'
IDWR = 'https://gis.idwr.idaho.gov/hosting/rest/services'


# ---------------------------------------------------------------- MVT decode
def _varints(buf):
    i, n = 0, len(buf)
    while i < n:
        v, shift = 0, 0
        while True:
            b = buf[i]; i += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        yield v


def _fields(buf):
    """Yield (field_no, wire_type, value) from a protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        v, shift = 0, 0
        while True:
            b = buf[i]; i += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        fno, wt = v >> 3, v & 7
        if wt == 0:
            x, shift = 0, 0
            while True:
                b = buf[i]; i += 1
                x |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            yield fno, wt, x
        elif wt == 2:
            ln, shift = 0, 0
            while True:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            yield fno, wt, buf[i:i + ln]; i += ln
        elif wt == 5:
            yield fno, wt, buf[i:i + 4]; i += 4
        elif wt == 1:
            yield fno, wt, buf[i:i + 8]; i += 8
        else:
            raise ValueError(f'wire type {wt}')


def _value(buf):
    for fno, wt, v in _fields(buf):
        if fno == 1: return v.decode('utf-8', 'replace')
        if fno == 2: return struct.unpack('<f', v)[0]
        if fno == 3: return struct.unpack('<d', v)[0]
        if fno in (4, 5): return v
        if fno == 6: return (v >> 1) ^ -(v & 1)
        if fno == 7: return bool(v)
    return None


def _zz(v):
    return (v >> 1) ^ -(v & 1)


def decode_tile(data, z, x, y, want_layers):
    """MVT → {layer: [ {props, type, coords(lon/lat)} ]}. Polygons come back
    as lists of rings; lines as lists of paths."""
    out = {}
    n2 = 2 ** z
    def lonlat(px, py, extent):
        lon = (x + px / extent) / n2 * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n2))))
        return round(lon, 5), round(lat, 5)
    for fno, wt, layer_buf in _fields(data):
        if fno != 3:
            continue
        name, extent, keys, vals, feats = None, 4096, [], [], []
        for f2, w2, v2 in _fields(layer_buf):
            if f2 == 1: name = v2.decode()
            elif f2 == 3: keys.append(v2.decode('utf-8', 'replace'))
            elif f2 == 4: vals.append(_value(v2))
            elif f2 == 5: extent = v2
            elif f2 == 2: feats.append(v2)
        if name not in want_layers:
            continue
        rows = []
        for fb in feats:
            props, gtype, geom = {}, 0, []
            for f3, w3, v3 in _fields(fb):
                if f3 == 2:
                    t = list(_varints(v3))
                    for i in range(0, len(t) - 1, 2):
                        if t[i] < len(keys) and t[i + 1] < len(vals):
                            props[keys[t[i]]] = vals[t[i + 1]]
                elif f3 == 3:
                    gtype = v3
                elif f3 == 4:
                    geom = list(_varints(v3))
            cx = cy = 0
            parts, cur = [], []
            i = 0
            while i < len(geom):
                cmd, cnt = geom[i] & 7, geom[i] >> 3
                i += 1
                if cmd == 1:                      # MoveTo
                    for _ in range(cnt):
                        cx += _zz(geom[i]); cy += _zz(geom[i + 1]); i += 2
                        if cur:
                            parts.append(cur)
                        cur = [lonlat(cx, cy, extent)]
                elif cmd == 2:                    # LineTo
                    for _ in range(cnt):
                        cx += _zz(geom[i]); cy += _zz(geom[i + 1]); i += 2
                        cur.append(lonlat(cx, cy, extent))
                elif cmd == 7:                    # ClosePath
                    if cur:
                        cur.append(cur[0])
                else:
                    break
            if cur:
                parts.append(cur)
            rows.append({'props': props, 'type': gtype, 'coords': parts})
        out[name] = rows
    return out


def tiles_for(bbox, z):
    def t(lon, lat):
        n = 2 ** z
        tx = int((lon + 180) / 360 * n)
        ty = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        return tx, ty
    x0, y0 = t(bbox[0], bbox[3])
    x1, y1 = t(bbox[2], bbox[1])
    return [(z, xx, yy) for xx in range(x0, x1 + 1) for yy in range(y0, y1 + 1)]


# ---------------------------------------------------------------- fetch bits
def fetch_units(bbox, notes):
    map_ids, lines = set(), []
    tls = tiles_for(bbox, ZOOM)
    print(f'macrostrat carto: {len(tls)} tiles @ z{ZOOM}')
    for z, x, y in tls:
        try:
            raw = cached_get(TILES.format(z=z, x=x, y=y), ttl_days=30, binary=True)
            layers = decode_tile(raw, z, x, y, {'units', 'lines'})
        except Exception as e:                    # noqa: BLE001
            notes.append(f'tile {z}/{x}/{y} failed: {e}')
            continue
        for u in layers.get('units', []):
            mid = u['props'].get('map_id')
            if mid is not None:
                map_ids.add(int(mid))
        for ln in layers.get('lines', []):
            p = ln['props']
            typ = str(p.get('type') or p.get('descrip') or '').lower()
            if 'fault' not in typ:
                continue
            for path in ln['coords']:
                if len(path) >= 2:
                    lines.append({'nm': p.get('name') or None, 'ty': typ[:40],
                                  'src': 'macrostrat', 'path': path})
    print(f'  map_ids: {len(map_ids)}, fault lines (tile pieces): {len(lines)}')

    units, refs = [], {}
    ids = sorted(map_ids)
    for i in range(0, len(ids), 20):
        batch = ids[i:i + 20]
        url = f'{MS}/geologic_units/map?map_id={",".join(map(str, batch))}&format=geojson'
        j = json.loads(cached_get(url, ttl_days=30))
        s = j['success']
        refs.update({str(k): v.strip() for k, v in (s.get('refs') or {}).items()})
        for f in s['data']['features']:
            units.append(f)
        print(f'  attrs+geometry {min(i + 20, len(ids))}/{len(ids)}', end='\r')
    print()
    return units, lines, refs


def source_scales(units, notes):
    scales = {}
    for sid in sorted({str(f['properties']['source_id']) for f in units}):
        try:
            j = json.loads(cached_get(f'{MS}/geologic_units/map/legend?source_id={sid}&format=json',
                                      ttl_days=30))
            d = j['success']['data']
            if d:
                scales[sid] = d[0].get('scale')
        except Exception as e:                    # noqa: BLE001
            notes.append(f'legend scale lookup failed for source {sid}: {e}')
    return scales


def simple_env(bbox):
    """earthquake.usgs.gov's WAF 403s the JSON-object envelope; the plain
    comma form is accepted."""
    return {'geometryType': 'esriGeometryEnvelope', 'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'geometry': f'{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}'}


def fetch_faults(bbox, notes):
    out = []
    try:
        # single unpaged query — the USGS WAF 403s ArcGIS paging params
        # (orderByFields/OBJECTID cursors); this layer is tiny in any county bbox
        url = (f'{HAZFAULTS}/0/query?' + urllib.parse.urlencode(dict(simple_env(bbox),
               where='1=1', outFields='*', returnGeometry='true',
               geometryPrecision=5, outSR=4326, f='json')))
        j = json.loads(cached_get(url, ttl_days=30))
        for f in j.get('features', []):
            at = {k.lower(): v for k, v in f['attributes'].items()}
            nm = at.get('name') or at.get('fault_name') or at.get('faultname')
            ty = at.get('slip_sense') or at.get('sliptype') or at.get('cfault_id') or ''
            for path in (f.get('geometry') or {}).get('paths') or []:
                out.append({'nm': nm, 'ty': str(ty)[:40], 'src': 'hazfaults2014',
                            'path': [(round(p[0], 5), round(p[1], 5)) for p in path]})
    except Exception as e:                        # noqa: BLE001
        notes.append(f'hazfaults2014 unavailable: {e}')
    if not out:
        notes.append('hazfaults2014 (NSHM seismogenic faults) has no features in this bbox — '
                     'that model only carries major hazard-model faults. Range-front and other '
                     'mapped faults come from the geologic maps themselves (Macrostrat lines).')
    print(f'  quaternary fault paths: {len(out)}')
    return out


def fetch_springs(bbox, notes):
    out = []
    # GNIS named springs — physical-point layers carry gaz_featureclass;
    # springs sit in 'Landforms'(5) / 'Other Hydrographic Features'(7)
    try:
        seen = set()
        for layer in (5, 7):
            # OBJECTID must be in outFields or arcgis_query's cursor never
            # advances (this server also ignores the spatial filter — the
            # bbox check below is what actually scopes it)
            for f in arcgis_query(GNIS, layer, dict(envelope(bbox),
                                  where="gaz_featureclass='Spring'",
                                  outFields='OBJECTID,gaz_id,gaz_name,gaz_featureclass',
                                  returnGeometry='true', outSR=4326),
                                  ttl_days=30):
                at = {k.lower(): v for k, v in f['attributes'].items()}
                gid = at.get('gaz_id')
                if gid is not None and gid in seen:
                    continue
                seen.add(gid)
                nm = at.get('gaz_name') or at.get('feature_name') or ''
                g = f.get('geometry') or {}
                if g.get('x') is None:
                    continue
                # belt+braces: this server has ignored the spatial filter before
                if not (bbox[0] <= g['x'] <= bbox[2] and bbox[1] <= g['y'] <= bbox[3]):
                    continue
                low = nm.lower()
                cls = 'hot' if 'hot' in low else 'warm' if 'warm' in low else \
                      'thermal' if 'therm' in low else 'cold'
                out.append({'nm': nm, 'cls': cls, 'x': round(g['x'], 5),
                            'y': round(g['y'], 5), 'src': 'GNIS'})
    except Exception as e:                        # noqa: BLE001
        notes.append(f'GNIS springs unavailable: {e}')
    print(f'  GNIS springs: {len(out)} ({sum(1 for s in out if s["cls"] != "cold")} thermal-named)')
    return out


def fetch_geothermal_wells(bbox, notes):
    out = []
    try:
        # discover an IDWR geothermal service, if any
        cand = []
        root = json.loads(cached_get(f'{IDWR}?f=json', ttl_days=30))
        for folder in root.get('folders', []):
            try:
                jj = json.loads(cached_get(f'{IDWR}/{folder}?f=json', ttl_days=30))
                for s in jj.get('services', []):
                    if 'geotherm' in s['name'].lower():
                        cand.append((s['name'], s['type']))
            except Exception:                     # noqa: BLE001
                pass
        for name, typ in cand[:1]:
            base = f'{IDWR}/{name}/{typ}'
            meta = json.loads(cached_get(f'{base}?f=json', ttl_days=30))
            lid = (meta.get('layers') or [{'id': 0}])[0]['id']
            for f in arcgis_query(base, lid, dict(envelope(bbox), outFields='*',
                                  returnGeometry='true', outSR=4326), ttl_days=30):
                g = f.get('geometry') or {}
                if g.get('x') is None:
                    continue
                at = {k.lower(): v for k, v in f['attributes'].items()}
                out.append({'nm': at.get('wellname') or at.get('well_name') or 'geothermal well',
                            'x': round(g['x'], 5), 'y': round(g['y'], 5),
                            'src': f'IDWR {name}'})
        if not cand:
            notes.append('no IDWR geothermal service found under gis.idwr.idaho.gov/hosting — '
                         'geothermal-well boost runs on GNIS thermal springs only')
    except Exception as e:                        # noqa: BLE001
        notes.append(f'IDWR geothermal lookup failed: {e}')
    print(f'  geothermal wells: {len(out)}')
    return out


# ---------------------------------------------------------------- clip/pack
def clip_ring(ring, bbox):
    """Sutherland–Hodgman rectangle clip."""
    x0, y0, x1, y1 = bbox
    def clip_edge(pts, inside, intersect):
        out = []
        for i in range(len(pts)):
            a, b = pts[i - 1], pts[i]
            ia, ib = inside(a), inside(b)
            if ib:
                if not ia:
                    out.append(intersect(a, b))
                out.append(b)
            elif ia:
                out.append(intersect(a, b))
        return out
    def ix_v(x):
        return lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))
    def ix_h(y):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y)
    pts = list(ring)
    if pts and pts[0] == pts[-1]:
        pts = pts[:-1]
    for inside, ix in [(lambda p: p[0] >= x0, ix_v(x0)), (lambda p: p[0] <= x1, ix_v(x1)),
                       (lambda p: p[1] >= y0, ix_h(y0)), (lambda p: p[1] <= y1, ix_h(y1))]:
        pts = clip_edge(pts, inside, ix)
        if not pts:
            return []
    pts.append(pts[0])
    return [(round(x, 5), round(y, 5)) for x, y in pts]


def simplify(path, tol=0.0004):
    """Douglas–Peucker, tol in degrees (~40 m)."""
    if len(path) < 3:
        return path
    def dp(pts):
        if len(pts) < 3:
            return pts
        (ax, ay), (bx, by) = pts[0], pts[-1]
        dx, dy = bx - ax, by - ay
        den = math.hypot(dx, dy) or 1e-12
        imax, dmax = 0, -1
        for i in range(1, len(pts) - 1):
            px, py = pts[i]
            d = abs(dx * (ay - py) - dy * (ax - px)) / den
            if d > dmax:
                imax, dmax = i, d
        if dmax > tol:
            left = dp(pts[:imax + 1]); right = dp(pts[imax:])
            return left[:-1] + right
        return [pts[0], pts[-1]]
    closed = path[0] == path[-1] and len(path) > 3
    if closed:
        # DP on a closed ring with identical endpoints collapses it (the
        # anchor segment has zero length) — split at the farthest vertex
        pts = path[:-1]
        k = max(range(1, len(pts)),
                key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
        out = dp(pts[:k + 1])[:-1] + dp(pts[k:] + [pts[0]])[:-1]
        out.append(out[0])
        return out if len(out) >= 4 else path
    return dp(path)


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    k = aoi['key']
    bbox = aoi['bbox']
    notes = []

    units_raw, ms_lines, refs = fetch_units(bbox, notes)
    scales = source_scales(units_raw, notes)
    faults = fetch_faults(bbox, notes) + [
        {'nm': l['nm'], 'ty': l['ty'], 'src': l['src'],
         'path': simplify([p for p in l['path']
                           if bbox[0] - .05 <= p[0] <= bbox[2] + .05 and
                              bbox[1] - .05 <= p[1] <= bbox[3] + .05])}
        for l in ms_lines]
    faults = [f for f in faults if len(f['path']) >= 2]
    springs = fetch_springs(bbox, notes)
    wells = fetch_geothermal_wells(bbox, notes)

    units = []
    for f in units_raw:
        p = f['properties']
        g = f['geometry']
        polys = g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']]
        rings_out = []
        for poly in polys:
            pr = []
            for ring in poly:
                r = clip_ring([(pt[0], pt[1]) for pt in ring], bbox)
                if len(r) >= 4:
                    pr.append(simplify(r))
            if pr:
                rings_out.append(pr)
        if not rings_out:
            continue
        units.append({
            'id': p['map_id'], 'src': str(p['source_id']),
            'nm': p.get('name'), 'sn': p.get('strat_name') or None,
            'li': p.get('lith') or None, 'de': p.get('descrip') or None,
            'co': p.get('comments') or None,
            'age': p.get('best_int_name') or None,
            't0': p.get('b_age'), 't1': p.get('t_age'),
            'col': p.get('color') or None,
            'g': rings_out,                       # MultiPolygon rings, clipped to bbox
        })
    scale_words = {'tiny': '~1:20,000,000', 'small': '~1:5,000,000',
                   'medium': '~1:500,000', 'large': '1:24k–1:250k (quad-scale)'}
    sources = {sid: {'ref': refs.get(sid) or 'Macrostrat source (citation unavailable)',
                     'scale': scales.get(sid), 'scale_note': scale_words.get(scales.get(sid))}
               for sid in sorted({u['src'] for u in units})}

    out = {
        'aoi': k, 'generated': TODAY,
        'provenance': [prov(f'{MS}/geologic_units/map (map_id batches; carto tiles z{ZOOM} '
                            f'for coverage discovery)'),
                       prov(f'{HAZFAULTS} (USGS NSHM Quaternary faults)'),
                       prov(f'{GNIS} (GNIS named springs)')],
        'license': 'Macrostrat CC-BY 4.0; USGS public domain',
        'fallback_note': ('Vector order per WS6 spec: Macrostrat harmonized (serves IGS/SGMC/'
                          'NA-scale, best scale per area, recorded per unit) → SGMC WFS '
                          '(mrdata.usgs.gov, untapped fallback) → IGS REST (502 from this '
                          'environment, reaches us via Macrostrat) → scanned NGMDB rasters '
                          '(future work).'),
        'notes': notes,
        'units': units, 'faults': faults, 'springs': springs, 'wells': wells,
        'sources': sources,
    }
    write_json(f'data/geology/{k}.json', out)
    print(f'units: {len(units)}  faults: {len(faults)}  springs: {len(springs)}  '
          f'wells: {len(wells)}  sources: {len(sources)}')
    for n in notes:
        print('NOTE:', n)
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
