#!/usr/bin/env python3
"""Pull CLOSED mining claims for a state, newest-first with a record cap.

NV has 1.23M closed cases and UT 452k — full pulls would be 60+ MB site
files. Matching the WY precedent, we keep the most recent 250,000 per state
(highest OBJECTIDs ≈ most recently touched cases), marked truncated with the
total. DESC OBJECTID cursor: where OBJECTID < cursor, orderBy OBJECTID DESC.

Usage: python3 fetch_closed_desc.py NV [cap]
Emits site/data/claims/{st}_closed.json (columnar, same schema as the rest).
"""
import json, sys, time, urllib.parse, urllib.request

EP = 'https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer'
BBOX = {
    'NV': (-120.01, 35.00, -114.03, 42.01),
    'UT': (-114.06, 36.99, -109.06, 42.01),
}
TYPE_DECODE = {'384101': 'L', '384103': 'L', '384201': 'P', '384203': 'P',
               '384301': 'T', '384303': 'T', '384401': 'M', '384403': 'M'}
UA = {'User-Agent': 'nw-mineral-monitor/1.0 (research pipeline)', 'Accept': 'application/json'}
PAGE = 2000


def fetch(url, tries=8):
    """Retry BOTH transport failures and JSON-carried server errors —
    BLM's NV partition throws {'error': {'code': 503, 'Wait timeout…'}}
    mid-stream under load; treating that as fatal cost a 90-minute pull."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=180) as r:
                j = json.loads(r.read())
            if 'error' in j:
                last = j['error']
                time.sleep(min(60, 5 * (i + 1)))
                continue
            return j
        except Exception as e:                    # noqa: BLE001
            last = e; time.sleep(min(60, 5 * (i + 1)))
    raise RuntimeError(f'fetch failed after {tries}: {last}')


def run(st, cap=250000):
    xmin, ymin, xmax, ymax = BBOX[st]
    geometry = json.dumps({'xmin': xmin, 'ymin': ymin, 'xmax': xmax, 'ymax': ymax,
                           'spatialReference': {'wkid': 4326}})
    base = (f'{EP}/2/query?geometryType=esriGeometryEnvelope'
            f'&geometry={urllib.parse.quote(geometry)}'
            f'&inSR=4326&spatialRel=esriSpatialRelIntersects'
            f'&outFields=OBJECTID,CSE_NR,CSE_NAME,CSE_TYPE_NR'
            f'&returnGeometry=true&outSR=4326&geometryPrecision=5'
            f'&orderByFields=OBJECTID+DESC&resultRecordCount={PAGE}&f=json')
    # total for the truncation note
    cnt = fetch(f'{EP}/2/query?geometryType=esriGeometryEnvelope'
                f'&geometry={urllib.parse.quote(geometry)}&inSR=4326'
                f'&spatialRel=esriSpatialRelIntersects&where=1%3D1'
                f'&returnCountOnly=true&f=json').get('count')
    print(f'{st}: {cnt} closed cases total; keeping most recent {cap}', flush=True)

    # resume from checkpoint if a prior run died mid-pull
    ckpt = f'/tmp/{st.lower()}_closed_ckpt.json'
    cursor, seen = None, set()
    cols = {'serial': [], 'name': [], 'type': [], 'x': [], 'y': []}
    try:
        c = json.load(open(ckpt))
        cursor, cols = c['cursor'], c['cols']
        seen = set(cols['serial'])
        print(f'{st}: resumed from checkpoint — {len(seen):,} records, cursor {cursor}', flush=True)
    except Exception:
        pass
    t0 = time.time()
    page_i = 0
    while len(cols['serial']) < cap:
        where = '1=1' if cursor is None else f'OBJECTID<{cursor}'
        j = fetch(base + '&where=' + urllib.parse.quote(where))
        feats = j.get('features', [])
        if not feats:
            break
        for f in feats:
            at = f['attributes']
            oid = at['OBJECTID']
            cursor = oid if cursor is None else min(cursor, oid)
            ser = at.get('CSE_NR')
            if not ser or ser in seen:
                continue
            seen.add(ser)
            rings = (f.get('geometry') or {}).get('rings')
            if not rings:
                continue
            ring = rings[0]
            cols['serial'].append(ser)
            cols['name'].append(at.get('CSE_NAME'))
            cols['type'].append(TYPE_DECODE.get(str(at.get('CSE_TYPE_NR')), '?'))
            cols['x'].append(round(sum(p[0] for p in ring) / len(ring), 5))
            cols['y'].append(round(sum(p[1] for p in ring) / len(ring), 5))
            if len(cols['serial']) >= cap:
                break
        page_i += 1
        if page_i % 10 == 0:
            json.dump({'cursor': cursor, 'cols': cols}, open(ckpt, 'w'))
            rate = len(cols['serial']) / max(1, time.time() - t0)
            eta = (cap - len(cols['serial'])) / max(1, rate)
            print(f'  {st} page {page_i}: {len(cols["serial"]):,} kept '
                  f'({rate:.0f}/s, ~{eta/60:.0f} min left, ckpt saved)', flush=True)

    n = len(cols['serial'])
    out = {'state': st, 'layer': 'closed', 'retrieved': time.strftime('%Y-%m-%d'),
           'n': n, 'serial': cols['serial'], 'name': cols['name'], 'type': cols['type'],
           'truncated': bool(cnt and n < cnt), 'total_available': cnt,
           'x': cols['x'], 'y': cols['y']}
    path = f'/home/claude/nw/site/data/claims/{st.lower()}_closed.json'
    json.dump(out, open(path, 'w'), separators=(',', ':'))
    import os
    try: os.remove(ckpt)
    except OSError: pass
    print(f'{st}: wrote {n:,} of {cnt:,} -> {path} '
          f'({os.path.getsize(path):,} bytes, {time.time()-t0:.0f}s)', flush=True)


if __name__ == '__main__':
    run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 250000)
