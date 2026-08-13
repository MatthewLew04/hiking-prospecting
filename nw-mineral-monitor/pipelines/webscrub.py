#!/usr/bin/env python3
"""WS4 — historic web scrub for the AOI's named features.

Sources actually automatable from here (each fact keeps provenance):
- Chronicling America via the loc.gov JSON API (the old chroniclingamerica
  search API 404s as of 2026) — rate-limited, cached
- MSHA Mines dataset (arlweb.msha.gov OpenGovernmentData) — full download,
  filtered to the AOI county FIPS → 20th-century mine IDs & operators
- Google Books volumes API — best-effort (the sandbox egress IP is often
  429'd; the fetcher backs off and the run stays useful without it)
Bot-walled / no-API sources become curated deep links rendered in the
dossier instead (HathiTrust, Mindat, The Diggings, NGMDB, IDL AML):
see dossier.py and ASSUMPTIONS.md.

Dedup: name variants are collapsed (strip mine/claim/group/no./#N suffixes)
and hits are keyed by (source, canonical URL). Output:
site/data/history/{aoi}.json = {byName: {canon: [hit...]}, msha: [...]}
"""
import io, json, os, re, sys, time, urllib.parse, zipfile
from collections import defaultdict
from common import (load_aoi, load_state, cached_get, SITE, TODAY, write_json,
                    load_build_input)

JUNK = re.compile(r'\b(gravel|pit|quarry|unnamed|unknown|prospect|placer|occurrence|'
                  r'deposit|deposits|clay|sand|stone|pumicite|cinder|borrow|'
                  r'area|adit|shaft|workings?)\b', re.I)
DEFAULT_SETTINGS = {'max_features': 60, 'chronam_rps': 0.8}


def webscrub_settings(aoi):
    settings = {**DEFAULT_SETTINGS, **(aoi.get('webscrub') or {})}
    if (not isinstance(settings['max_features'], int) or
            isinstance(settings['max_features'], bool) or
            settings['max_features'] < 1):
        raise ValueError('webscrub.max_features must be a positive integer')
    if (not isinstance(settings['chronam_rps'], (int, float)) or
            isinstance(settings['chronam_rps'], bool) or
            not 0 < settings['chronam_rps'] <= 2):
        raise ValueError('webscrub.chronam_rps must be in (0, 2]')
    return settings


def state_search_name(code):
    """Use the reviewable registry name, never a state-code special case."""
    return load_state(code)['name']


def strip_parens(name):
    return re.sub(r'\s*\([^)]*\)', '', name or '').strip()


def canon(name):
    n = strip_parens(name).lower()
    n = re.sub(r'\b(the|mine|mines|mining|claims?|group|lode|placer|prospect|'
               r'property|tunnel|shaft|no\.?\s*\d+|#\s*\d+|\d+)\b', ' ', n)
    n = re.sub(r'[^a-z ]+', ' ', n)
    return re.sub(r'\s+', ' ', n).strip()


def gather_names(aoi):
    """Named features worth sweeping: graded mines first, then MRDS, then claims."""
    st = aoi['state']
    x0, y0, x1, y1 = aoi['bbox']
    ranked = []
    with open(os.path.join(SITE, 'data/grades/grades.json'),
              encoding='utf-8') as source:
        g = json.load(source)
    for i in range(g['n']):
        if g['st'][i] == st and g['x'][i] is not None and x0 <= g['x'][i] <= x1 and y0 <= g['y'][i] <= y1:
            ranked.append(g['name'][i])
    # Legacy AOI snapshots are optional research inputs, never a prerequisite
    # for running Chronicling America in a newly registered state.
    try:
        m = load_build_input('sites', f'mrds_{st.lower()}')
    except (KeyError, FileNotFoundError, ValueError):
        m = None
    if m:
        for i in range(m['n']):
            if (m['x'][i] is not None and m['y'][i] is not None and
                    x0 <= m['x'][i] <= x1 and y0 <= m['y'][i] <= y1 and m['nm'][i]):
                ranked.append(m['nm'][i])
    claim_path = os.path.join(SITE, f'data/openground/{aoi["key"]}_claims.json')
    if os.path.isfile(claim_path):
        with open(claim_path, encoding='utf-8') as source:
            c = json.load(source)
        for row in c.get('active', []) + c.get('closed', []):
            if row.get('name'):
                ranked.append(row['name'])
    for name in (aoi.get('webscrub') or {}).get('seed_names', []):
        if isinstance(name, str) and name.strip():
            ranked.append(name.strip())
    seen, out = set(), []
    for n in ranked:
        cn = canon(n)
        if len(cn) < 4 or JUNK.search(n or '') or cn in seen: continue
        seen.add(cn)
        out.append((cn, strip_parens(n)))
    return out[:webscrub_settings(aoi)['max_features']]


def chronam(term, state_word, rps):
    q = urllib.parse.quote(f'"{term}" {state_word}')
    url = (f'https://www.loc.gov/collections/chronicling-america/'
           f'?q={q}&fo=json&c=8&at=results')      # at=results: 1.9MB→30KB, 2× faster
    try:
        j = json.loads(cached_get(url, ttl_days=60, min_interval=1.0 / rps))
    except Exception as e:                        # noqa: BLE001
        return [{'err': str(e)[:100]}]
    hits = []
    for r in j.get('results', [])[:8]:
        hits.append({'kind': 'newspaper', 'title': (r.get('title') or '')[:160],
                     'date': r.get('date'), 'url': r.get('id') or r.get('url'),
                     'partof': (r.get('partof') or [None])[0],
                     'src': url, 'retrieved': TODAY})
    return hits


_gb_fails = [0]


def gbooks(term, state_word, rps):
    if _gb_fails[0] >= 3: return []               # IP is rate-limited — stop burning time
    q = urllib.parse.quote(f'"{term}" {state_word} mining')
    url = f'https://www.googleapis.com/books/v1/volumes?q={q}&maxResults=5'
    try:
        j = json.loads(cached_get(url, ttl_days=60, min_interval=1.0 / rps, tries=1))
        _gb_fails[0] = 0
    except Exception:
        _gb_fails[0] += 1                         # 429s expected on shared egress IPs
        return []
    hits = []
    for it in j.get('items', []):
        v = it.get('volumeInfo', {})
        hits.append({'kind': 'book', 'title': (v.get('title') or '')[:160],
                     'date': v.get('publishedDate'),
                     'url': v.get('canonicalVolumeLink') or v.get('infoLink'),
                     'src': url, 'retrieved': TODAY})
    return hits


def msha(aoi):
    """Mine IDs + operators for the county from MSHA's full Mines dataset."""
    url = 'https://arlweb.msha.gov/OpenGovernmentData/DataSets/Mines.zip'
    raw = cached_get(url, ttl_days=30, binary=True)
    z = zipfile.ZipFile(io.BytesIO(raw))
    txt = z.read(z.namelist()[0]).decode('latin-1')
    lines = txt.splitlines()
    unq = lambda s: s.strip().strip('"')
    hdr = [unq(h) for h in lines[0].split('|')]
    idx = {h: i for i, h in enumerate(hdr)}
    fips = aoi['county_fips'][-3:]
    st = aoi['state']
    rows = []
    for ln in lines[1:]:
        p = [unq(v) for v in ln.split('|')]
        if len(p) < len(hdr): continue
        try:
            if p[idx['STATE']] != st: continue
            if p[idx['FIPS_CNTY_CD']].zfill(3) != fips: continue
        except Exception:
            continue
        rows.append({'mine_id': p[idx.get('MINE_ID', 0)],
                     'name': p[idx.get('CURRENT_MINE_NAME', 1)],
                     'operator': p[idx.get('CURRENT_OPERATOR_NAME', 2)] if 'CURRENT_OPERATOR_NAME' in idx else None,
                     'status': p[idx['CURRENT_MINE_STATUS']] if 'CURRENT_MINE_STATUS' in idx else None,
                     'status_dt': p[idx['CURRENT_STATUS_DT']] if 'CURRENT_STATUS_DT' in idx else None,
                     'type': p[idx['CURRENT_MINE_TYPE']] if 'CURRENT_MINE_TYPE' in idx else None,
                     'commodity': p[idx['PRIMARY_CANVASS']] if 'PRIMARY_CANVASS' in idx else None,
                     'lat': p[idx['LATITUDE']] if 'LATITUDE' in idx else None,
                     'lon': p[idx['LONGITUDE']] if 'LONGITUDE' in idx else None,
                     'src': url, 'retrieved': TODAY})
    return rows, hdr


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    settings = webscrub_settings(aoi)
    state_word = state_search_name(aoi['state'])
    names = gather_names(aoi)
    print(f'sweeping {len(names)} canonical names')
    rps = settings['chronam_rps']
    by_name = {}
    for i, (cn, display) in enumerate(names):
        hits = chronam(display, state_word, rps)
        hits += gbooks(display, state_word, rps)
        good = [h for h in hits if 'err' not in h]
        if good:
            # dedup by URL
            seen, ded = set(), []
            for h in good:
                u = h.get('url')
                if u and u not in seen: seen.add(u); ded.append(h)
            by_name[cn] = {'display': display, 'hits': ded}
        if (i + 1) % 10 == 0: print(f'  {i+1}/{len(names)} names swept')
    print(f'names with hits: {len(by_name)}')
    try:
        msha_rows, hdr = msha(aoi)
        print(f'MSHA mines in county: {len(msha_rows)}')
    except Exception as e:                        # noqa: BLE001
        msha_rows = []; print('MSHA skipped:', str(e)[:120])
    out = {'aoi': aoi['key'], 'generated': TODAY,
           'note': 'Automated sweep: Chronicling America (loc.gov API), Google Books '
                   '(best-effort), MSHA Mines dataset. Newspaper OCR is noisy — hits are '
                   'leads, open the page image. Dedup by canonical name + URL.',
           'byName': by_name, 'msha': msha_rows}
    write_json(f'data/history/{aoi["key"]}.json', out)
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
