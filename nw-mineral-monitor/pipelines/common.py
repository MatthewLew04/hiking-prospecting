"""Shared plumbing for all NW Mineral Monitor pipelines.

Design rules (see ASSUMPTIONS.md):
- every remote fetch goes through cached_get(): responses land in
  pipelines/cache/ keyed by URL hash, with a TTL, so re-runs are idempotent
  and never re-download blindly;
- every emitted fact carries provenance {"src": url, "retrieved": date};
- ArcGIS paging uses an OBJECTID cursor (BLM servers lie about resultOffset);
- polite: default 0.6 s between uncached requests, honest User-Agent.
"""
import json, os, re, time, hashlib, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'cache')
SITE = os.path.normpath(os.path.join(HERE, '..', 'site'))
os.makedirs(CACHE, exist_ok=True)

UA = {'User-Agent': 'nw-mineral-monitor/1.0 (research pipeline; contact: repo owner)',
      'Accept': 'application/json'}
TODAY = time.strftime('%Y-%m-%d')
_last_net = [0.0]


def load_aoi(key=None):
    cfg = json.load(open(os.path.join(HERE, 'config', 'aoi.json')))
    k = key or os.environ.get('AOI') or cfg['default']
    a = dict(cfg['aois'][k]); a['key'] = k
    return a


def cached_get(url, ttl_days=7, binary=False, min_interval=0.6, tries=3):
    """GET with an on-disk cache. Returns bytes (binary=True) or str.
    binary fetches send Accept: */* — some servers (MSHA) 406 on
    Accept: application/json for zip downloads."""
    h = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = os.path.join(CACHE, h + ('.bin' if binary else '.txt'))
    meta = path + '.meta'
    if os.path.exists(path) and os.path.exists(meta):
        m = json.load(open(meta))
        if time.time() - m['t'] < ttl_days * 86400:
            b = open(path, 'rb').read()
            return b if binary else b.decode('utf-8', 'replace')
    wait = _last_net[0] + min_interval - time.time()
    if wait > 0: time.sleep(wait)
    last = None
    hdrs = dict(UA)
    if binary: hdrs['Accept'] = '*/*'
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=120) as r:
                b = r.read()
            _last_net[0] = time.time()
            with open(path, 'wb') as f: f.write(b)
            json.dump({'t': time.time(), 'url': url}, open(meta, 'w'))
            return b if binary else b.decode('utf-8', 'replace')
        except Exception as e:                       # noqa: BLE001 — retried
            last = e; _last_net[0] = time.time(); time.sleep(2 * (i + 1))
    raise RuntimeError(f'fetch failed after {tries} tries: {url} ({last})')


def arcgis_query(base, layer, params, ttl_days=7, page=2000, max_pages=200):
    """Full paged query via OBJECTID cursor. Yields feature dicts."""
    cursor = 0
    base_where = params.pop('where', '1=1')
    for _ in range(max_pages):
        p = dict(params)
        p.update({'where': f'({base_where}) AND OBJECTID>{cursor}',
                  'orderByFields': 'OBJECTID', 'resultRecordCount': page, 'f': 'json'})
        url = f'{base}/{layer}/query?' + urllib.parse.urlencode(p)
        j = json.loads(cached_get(url, ttl_days=ttl_days))
        if 'error' in j:
            raise RuntimeError(f'ArcGIS error on {base}/{layer}: {j["error"]}')
        feats = j.get('features', [])
        if not feats:
            return
        for f in feats:
            cursor = max(cursor, f['attributes'].get('OBJECTID', cursor))
            yield f


def envelope(bbox):
    return {'geometryType': 'esriGeometryEnvelope', 'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'geometry': json.dumps({'xmin': bbox[0], 'ymin': bbox[1],
                                    'xmax': bbox[2], 'ymax': bbox[3],
                                    'spatialReference': {'wkid': 4326}})}


# ---------- PLSS helpers ----------
# CadNSDI id formats (verified 2026-08): PLSSID = SS MM TTTFD RRRFD D
#   e.g. ID08 0120S 0220E 0  → ID, Boise Mer (08), T12S, R22E, dup 0
#   FRSTDIVID = PLSSID + 'SN' + section(2) + dup(1), e.g. ...SN140 = sec 14
def plssid(state_mer, twn, tdir, rng, rdir, dup='0'):
    return f'{state_mer}{int(twn):03d}0{tdir}{int(rng):03d}0{rdir}{dup}'


def frstdivid(state_mer, twn, tdir, rng, rdir, sec, dup='0'):
    return f'{plssid(state_mer, twn, tdir, rng, rdir)}SN{int(sec):02d}{dup}'


TRS_RE = re.compile(
    r'T\.?\s*(\d{1,3})\s*([NS])\.?[,;\s]*R\.?\s*(\d{1,3})\s*([EW])\.?[,;\s]*'
    r'(?:S(?:ec|ection)?s?\.?\s*)(\d{1,2})', re.I)


def parse_trs(text):
    """'T12S R22E Sec 14' (many spellings) -> (12,'S',22,'E',14) or None."""
    m = TRS_RE.search(text or '')
    if not m: return None
    t, td, r, rd, s = m.groups()
    return int(t), td.upper(), int(r), rd.upper(), int(s)


# CSE_META legal entries: 'ID 08 0140S 0230E 027 A NW|...'
META_RE = re.compile(r'([A-Z]{2})\s+(\d{2})\s+(\d{3,4})([NS])\s+(\d{3,4})([EW])\s+(\d{1,3})')


def sections_from_cse_meta(meta):
    """Set of FRSTDIVIDs a claim's legal description touches."""
    out = set()
    for m in META_RE.finditer(meta or ''):
        st, mer, t, td, r, rd, sec = m.groups()
        out.add(f'{st}{mer}{int(t)//10:03d}0{td}{int(r)//10:03d}0{rd}0SN{int(sec):02d}0')
    return out


def trs_label(fid):
    """FRSTDIVID -> human 'T12S R22E Sec 14 (Boise Mer.)'-ish label (no meridian name)."""
    m = re.match(r'([A-Z]{2})(\d{2})(\d{3})\d([NS])(\d{3})\d([EW])\dSN(\d{2})\d', fid)
    if not m: return fid
    st, mer, t, td, r, rd, sec = m.groups()
    return f'T{int(t)}{td} R{int(r)}{rd} Sec {int(sec)} ({st} mer.{mer})'


def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def point_in_poly(x, y, rings):
    """Even-odd across all rings (handles holes)."""
    cnt = sum(1 for r in rings if point_in_ring(x, y, r))
    return cnt % 2 == 1


def prov(url):
    return {'src': url, 'retrieved': TODAY}


def write_json(relpath, obj):
    path = os.path.join(SITE, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, 'w'), separators=(',', ':'))
    print(f'wrote {relpath} ({os.path.getsize(path):,} bytes)')
