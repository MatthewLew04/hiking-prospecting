#!/usr/bin/env python3
"""Shared library for the WS9 grade-enrichment pipelines (grades_ca.py round 2,
grades_id.py). Conventions inherited from grades_ca.py round 1 (ASSUMPTIONS #36):

- every row carries a VERBATIM quote + page-level citation into a cached,
  page-indexed source PDF; rows are validated against the cached page text
  and a row that fails validation is dropped loudly, never silently kept;
- era-correct dollar conversions: Au $20.67/oz before 1934, $35.00 after
  (to 1971); Ag by year via AG_PRICE (annual averages, sourced below);
- bonanza handling: assay-text/specimen values are kept verbatim but capped
  by CAP_* sanity ceilings from the round-1 extraction (averages > 50 oz/t
  Au are unit errors; anything > 610 oz/t Au — the historic specimen record
  — is dropped); scoring-side capping stays in county_gold (5 oz/t).
- placer values are FLAGGED (plc=1) and carry $/yd3 in their own field —
  never mixed into $/ton;
- geolocation: MRDS name-match near a district anchor, never invented;
- splice is idempotent: each pipeline owns its rows via the 'own' tag and
  drops/re-adds exactly those; enrichment of rows owned by other pipelines
  is additive (extra quotes tagged with 'own') and reversible.

PDF cache: pipelines/cache/pdfs/<key>.pdf  (large, re-fetchable, gitignored)
Page text: pipelines/cache/pagetext/<key>.json.gz  (committed — the durable
           page-indexed cache the rebuild runs from; includes printed-page
           offset calibration so 'p. 61' resolves to a PDF page)
"""
import gzip, json, math, os, re, subprocess, sys, time, urllib.request

from build_inputs import load_artifact as load_build_artifact

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.normpath(os.path.join(HERE, '..', 'site'))
PDFS = os.path.join(HERE, 'cache', 'pdfs')
PTXT = os.path.join(HERE, 'cache', 'pagetext')
os.makedirs(PDFS, exist_ok=True)
os.makedirs(PTXT, exist_ok=True)

UA = 'nw-mineral-monitor/1.0 (research pipeline; contact: repo owner)'

# ---------------------------------------------------------------- prices ---
AU_PRE1934 = 20.67      # $/oz statutory, all pre-1934 dollar figures
AU_1934_71 = 35.00      # $/oz, 1934-1971 figures
# Annual-average US silver prices, $/troy oz (New York; 1935-55 figures are
# the US Treasury newly-mined domestic price where that governed reporting).
# Values from USGS Minerals Yearbook / Historical Statistics for Mineral and
# Material Commodities (DS 140, silver) annual price series.
AG_PRICE = {
    1870: 1.328, 1875: 1.240, 1880: 1.145, 1885: 1.065, 1890: 1.046,
    1892: 0.870, 1894: 0.630, 1895: 0.653, 1897: 0.604, 1900: 0.620,
    1902: 0.525, 1905: 0.606, 1907: 0.654, 1910: 0.538, 1912: 0.615,
    1915: 0.507, 1916: 0.658, 1917: 0.824, 1918: 0.968, 1919: 1.120,
    1920: 1.019, 1921: 0.631, 1923: 0.648, 1925: 0.694, 1927: 0.567,
    1929: 0.533, 1930: 0.385, 1931: 0.290, 1932: 0.282, 1933: 0.350,
    1934: 0.480, 1935: 0.642, 1936: 0.774, 1937: 0.773, 1938: 0.646,
    1939: 0.679, 1940: 0.711, 1945: 0.711, 1950: 0.742, 1955: 0.891,
}
AG_SRC = ('USGS Historical Statistics for Mineral and Material Commodities '
          '(DS 140), silver annual averages')

BASE_METAL_PRICE_PATH = os.path.join(HERE, 'config', 'base_metal_prices.json')
BASE_METAL_SRC = ('USGS Historical Statistics for Mineral and Material '
                  'Commodities (Data Series 140), annual Cu/Pb/Zn prices')

def ag_price(year):
    """Annual-average Ag price for a year (nearest table year <=2 off)."""
    if year in AG_PRICE:
        return AG_PRICE[year]
    for d in (1, -1, 2, -2):
        if year + d in AG_PRICE:
            return AG_PRICE[year + d]
    ks = sorted(AG_PRICE)
    k = min(ks, key=lambda y: abs(y - year))
    return AG_PRICE[k]

def au_price(year):
    return AU_PRE1934 if (year or 1900) < 1934 else AU_1934_71


def base_metal_price(metal, year):
    """Nearest annual Cu/Pb/Zn benchmark in nominal dollars per pound."""
    with open(BASE_METAL_PRICE_PATH, encoding='utf-8') as source:
        raw = json.load(source)
    if raw.get('status') != 'reviewed':
        raise ValueError('Cu/Pb/Zn price table is not reviewed; dollar conversion refused')
    table = {int(y): value for y, value in raw['prices_usd_per_lb'][metal.title()].items()}
    if not table:
        raise ValueError(f'no reviewed annual {metal} prices are configured')
    year = year or 1900
    return table[min(table, key=lambda y: abs(y - year))]

# oz/t sanity ceilings (round-1 conventions)
CAP_AU_AVG = 50.0       # a stated *average* above this is a unit error: drop
CAP_AU_ANY = 610.0      # historic specimen record: anything above is dropped
CAP_AG_ANY = 10000.0
GPT_TO_OPT = 1 / 34.2857    # g/tonne -> troy oz/short ton  (0.029166)

# ------------------------------------------------------------------ fetch ---
def fetch_pdf(key, url, quiet=False):
    """Cache a source PDF under pipelines/cache/pdfs/<key>.pdf."""
    path = os.path.join(PDFS, key + '.pdf')
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        return path
    if not quiet:
        print(f'  fetching {key}: {url}')
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=900) as r, open(path + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(path + '.part', path)
    return path

# --------------------------------------------------------------- pagetext ---
_PGNUM = re.compile(r'^\s{0,12}(\d{1,4})\s{0,12}$')

def _printed_offset(pages):
    """Calibrate printed-page -> pdf-index: mode of (printed - pdfpage)."""
    from collections import Counter
    votes = Counter()
    for i, tx in enumerate(pages, 1):
        lines = [l for l in tx.splitlines()[:4] + tx.splitlines()[-4:] if l.strip()]
        for l in lines:
            m = _PGNUM.match(l)
            if m:
                p = int(m.group(1))
                if 0 < p < len(pages) + 400:
                    votes[p - i] += 1
    if not votes:
        return None
    off, n = votes.most_common(1)[0]
    return off if n >= max(4, len(pages) // 200) else None

def build_pagetext(key, url=None, force=False):
    """pdftotext the cached PDF into a page-indexed json.gz; returns dict."""
    out = os.path.join(PTXT, key + '.json.gz')
    if os.path.exists(out) and not force:
        return json.load(gzip.open(out, 'rt'))
    pdf = os.path.join(PDFS, key + '.pdf')
    if not os.path.exists(pdf):
        if not url:
            raise FileNotFoundError(f'no cached PDF and no URL for {key}')
        fetch_pdf(key, url)
    txt = subprocess.run(['pdftotext', '-q', pdf, '-'],
                         capture_output=True, text=True).stdout
    pages = txt.split('\f')
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    d = {'key': key, 'url': url, 'n': len(pages),
         'offset': _printed_offset(pages), 'pages': pages}
    json.dump(d, gzip.open(out, 'wt'))
    return d

def load_pagetext(key):
    return json.load(gzip.open(os.path.join(PTXT, key + '.json.gz'), 'rt'))

def printed_to_pdf(pt, printed):
    """Best pdf 1-based index for a printed page number."""
    if pt.get('offset') is not None:
        i = printed - pt['offset']          # offset = printed - pdf_index
        if 1 <= i <= pt['n']:
            return i
    return min(max(printed, 1), pt['n'])

# ------------------------------------------------------------- validation ---
def _norm(s):
    s = (s or '').lower()
    s = s.replace('’', "'").replace('‘', "'")
    s = s.replace('“', '"').replace('”', '"')
    s = re.sub(r'-\s*\n\s*', '', s)          # printed hyphenation
    s = re.sub(r'[^a-z0-9$.%]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def _tokens(s):
    return _norm(s).split()

def quote_on_page(quote, pagetxt):
    """exact -> True; else best token-window similarity 0..1."""
    nq, np_ = _norm(quote), _norm(pagetxt)
    if nq and nq in np_:
        return 1.0
    qt, pt = nq.split(), np_.split()
    if not qt or not pt:
        return 0.0
    qset = set(qt)
    w = len(qt)
    best = 0.0
    step = max(1, w // 4)
    for i in range(0, max(1, len(pt) - w + 1), step):
        window = pt[i:i + int(w * 1.3)]
        inter = sum(1 for t in qt if t in set(window))
        best = max(best, inter / w)
        if best > 0.97:
            break
    return best

def validate_rows(rows, min_fuzzy=0.85, slack=2):
    """Each row: needs src_key, page (printed) or pdf_page, quote.
    Returns (ok_rows, failures); sets row['_vscore'], row['_pdf_page']."""
    cache = {}
    ok, bad = [], []
    for r in rows:
        k = r['src_key']
        if k is None:                      # web source (press release): no PDF
            r['_vscore'] = None
            ok.append(r)
            continue
        if k not in cache:
            cache[k] = load_pagetext(k)
        pt = cache[k]
        base = r.get('pdf_page') or printed_to_pdf(pt, r['page'])
        best, bestpg = 0.0, base
        for d in range(-slack, slack + 1):
            i = base + d
            if 1 <= i <= pt['n']:
                s = quote_on_page(r['quote'], pt['pages'][i - 1])
                if s > best:
                    best, bestpg = s, i
                if best >= 1.0:
                    break
        r['_vscore'], r['_pdf_page'] = round(best, 3), bestpg
        (ok if best >= min_fuzzy else bad).append(r)
    return ok, bad

# ------------------------------------------------------------ conversions ---
def year_of(yrs):
    """Pull a representative year out of a 'yrs' string for price lookup."""
    ys = [int(y) for y in re.findall(r'(18\d\d|19\d\d|20\d\d)', str(yrs or ''))]
    return ys[-1] if ys else None

def usd_to_au_opt(usd, year):
    p = au_price(year)
    return round(usd / p, 3), p

def normalize_row(r, cap=True):
    """Fill normalized fields from native statements on a curated row.
    Native fields a curated row may carry:
      au_opt, ag_opt          (already oz/t — no conversion)
      au_gpt                  (g/tonne, modern)  -> au_opt
      usd_per_ton (+era year) -> au_opt when metal=='Au' (default)
      usd_per_ton (+era year) -> metal_pct for Cu/Pb/Zn when the dollar
                                 statement is explicitly single-metal
      pb_pct zn_pct cu_pct sb_pct  (percent)
      wo3_units               (1 unit = 20 lb WO3 = 1% per short ton)
      hg_flasks               (production, 76-lb flasks)
      usd_per_yd3 / plc       (placer)
    Sets r['conv'] describing any $ conversion, r['nat'] compact native set.
    Returns False if the row violates the bonanza/sanity ceilings."""
    year = r.get('price_year') or year_of(r.get('years'))
    conv = []
    if r.get('au_gpt') is not None and r.get('au_opt') is None:
        r['au_opt'] = round(r['au_gpt'] * GPT_TO_OPT, 3)
        conv.append(f"Au {r['au_gpt']} g/t -> oz/short ton x0.02917")
    if r.get('usd_per_ton') is not None and r.get('au_opt') is None \
            and r.get('metal', 'Au') == 'Au':
        v, p = usd_to_au_opt(r['usd_per_ton'], year)
        r['au_opt'] = v
        conv.append(f"Au $/ton at ${p:.2f}/oz"
                    f" ({'statutory pre-1934' if p == AU_PRE1934 else '1934-71'})")
    if r.get('usd_per_ton') is not None and r.get('metal') == 'Ag' \
            and r.get('ag_opt') is None:
        p = ag_price(year or 1900)
        r['ag_opt'] = round(r['usd_per_ton'] / p, 1)
        conv.append(f"Ag $/ton at ${p:.3f}/oz ({year} avg; {AG_SRC})")
    metal = str(r.get('metal') or '').title()
    pct_field = {'Cu': 'cu_pct', 'Pb': 'pb_pct', 'Zn': 'zn_pct'}.get(metal)
    if r.get('usd_per_ton') is not None and pct_field \
            and r.get(pct_field) is None:
        p = base_metal_price(metal, year)
        # one short ton at one weight percent contains 20 lb of metal
        r[pct_field] = round(r['usd_per_ton'] / (20 * p), 3)
        conv.append(f"{metal} $/ton at ${p:.4f}/lb ({year} nearest annual avg; "
                    f"{BASE_METAL_SRC})")
    # sanity / bonanza ceilings (round-0/2 convention; round-1 CA bonanza
    # rows are grandfathered with cap=False — ASSUMPTIONS #36)
    au = r.get('au_opt')
    if cap and au is not None:
        if au > CAP_AU_ANY:
            return False
        if au > CAP_AU_AVG and r.get('basis') in ('production average',
                                                  'production'):
            return False
    if cap and (r.get('ag_opt') or 0) > CAP_AG_ANY:
        return False
    nat = []
    if r.get('usd_per_ton') is not None:
        nat.append(f"${r['usd_per_ton']}/ton")
    if r.get('au_gpt') is not None:
        nat.append(f"{r['au_gpt']} g/t Au")
    for f, lab in (('au_opt', 'oz/t Au'), ('ag_opt', 'oz/t Ag')):
        if r.get(f) is not None and not any(lab[-2:] in x for x in nat):
            nat.append(f"{r[f]} {lab}")
    for f, lab in (('pb_pct', '% Pb'), ('zn_pct', '% Zn'), ('cu_pct', '% Cu'),
                   ('sb_pct', '% Sb')):
        if r.get(f) is not None:
            nat.append(f"{r[f]}{lab}")
    if r.get('wo3_units') is not None:
        nat.append(f"{r['wo3_units']} units WO3")
    if r.get('hg_flasks') is not None:
        nat.append(f"{r['hg_flasks']:,} flasks Hg")
    if r.get('usd_per_yd3') is not None:
        nat.append(f"${r['usd_per_yd3']}/yd3 (placer)")
    r['nat'] = ' · '.join(nat) if nat else None
    r['conv'] = '; '.join(conv) if conv else None
    return True

# ------------------------------------------------------------------ triage ---
ASSAY_PAT = [
    (3, re.compile(r'\$\s?\d[\d,.]*\s?(?:a|per)\s+ton', re.I)),
    (3, re.compile(r'(?:ounces?|oz\.?)\s+(?:of\s+)?(?:gold|silver)?\s*'
                   r'(?:to|per)\s+(?:the\s+)?ton', re.I)),
    (2, re.compile(r'\bassay(?:s|ed|ing)?\b', re.I)),
    (2, re.compile(r'\baverag(?:e[sd]?|ing)\b', re.I)),
    (2, re.compile(r'mill\s+(?:run|test|heads?)', re.I)),
    (2, re.compile(r'yield(?:s|ed|ing)?\s+\$?\d', re.I)),
    (2, re.compile(r'\bflasks?\b', re.I)),
    (2, re.compile(r'per\s+cent(?:\s+of)?\s+(?:antimony|copper|lead|zinc|'
                   r'quicksilver|tungsten|WO3)', re.I)),
    (2, re.compile(r'per\s+cubic\s+yard|\bcents?\s+(?:a|per)\s+(?:cubic\s+)?'
                   r'yard\b', re.I)),
    (1, re.compile(r'\bore\s+(?:shoot|body|shipped|carried)\b', re.I)),
    (1, re.compile(r'\bg/t\b|\bgrams?\s+per\s+tonne\b', re.I)),
]

def triage(key):
    """Score every page of a volume for assay language. Returns dict with
    ranked pages; caches to pagetext/<key>.triage.json."""
    out = os.path.join(PTXT, key + '.triage.json')
    if os.path.exists(out):
        return json.load(open(out))
    pt = load_pagetext(key)
    scores = []
    for i, tx in enumerate(pt['pages'], 1):
        s = sum(w * len(p.findall(tx)) for w, p in ASSAY_PAT)
        scores.append(s)
    ranked = sorted(range(1, pt['n'] + 1), key=lambda i: -scores[i - 1])
    d = {'key': key, 'n': pt['n'], 'offset': pt.get('offset'),
         'hit_pages': sum(1 for s in scores if s > 0),
         'scores': scores, 'ranked': ranked[:400]}
    json.dump(d, open(out, 'w'))
    return d

# ------------------------------------------------------- MRDS geolocation ---
def base_name(s):
    """Merge key: mine name with parenthetical qualifiers stripped."""
    return re.sub(r'\s*\(.*$', '', s or '').strip()

def canon(s):
    s = re.sub(r'[^a-z0-9 ]+', ' ', (s or '').lower().replace('-', ' '))
    s = re.sub(r'\b(the|mine|mines|mining|group|claim|claims|lode|quartz|'
               r'consolidated|con|no|inc|co|company|prospect|property)\b',
               ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def mrds_index(state):
    m = load_build_artifact('sites', f'mrds_{state.lower()}')
    return [(canon(m['nm'][i]), m['x'][i], m['y'][i]) for i in range(m['n'])
            if m['x'][i] is not None and m['nm'][i]]

# --- full MRDS relational dump (county-aware; mrdata.usgs.gov mrds-csv.zip) --
def mrds_csv_path():
    for p in (os.environ.get('MRDS_CSV') or '',
              os.path.expanduser('~/mining-data/mrds.csv'),
              os.path.join(HERE, 'cache', 'mrds.csv')):
        if p and os.path.exists(p):
            return p
    # fetch + extract into pipelines/cache/
    import zipfile, io
    print('  fetching mrds-csv.zip (25 MB) ...')
    req = urllib.request.Request('https://mrdata.usgs.gov/mrds/mrds-csv.zip',
                                 headers={'User-Agent': UA, 'Accept': '*/*'})
    b = urllib.request.urlopen(req, timeout=600).read()
    zipfile.ZipFile(io.BytesIO(b)).extract('mrds.csv', os.path.join(HERE, 'cache'))
    return os.path.join(HERE, 'cache', 'mrds.csv')

_MRDS_ST = {}
def mrds_state(state):
    """(name-index, district-index, meta) for a state from the full dump."""
    st = state.upper()
    if st in _MRDS_ST:
        return _MRDS_ST[st]
    import csv
    csv.field_size_limit(10 ** 9)
    FULL = {'CA': 'California', 'ID': 'Idaho', 'NV': 'Nevada', 'UT': 'Utah',
            'OR': 'Oregon', 'WA': 'Washington', 'MT': 'Montana', 'WY': 'Wyoming'}
    byname, bydist, meta = {}, {}, {}
    with open(mrds_csv_path(), encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f):
            if (row.get('state') or '') != FULL.get(st):
                continue
            try:
                lat = round(float(row['latitude']), 5)
                lon = round(float(row['longitude']), 5)
            except (ValueError, TypeError, KeyError):
                continue
            cty = (row.get('county') or '').strip().lower()
            k = canon(row.get('site_name'))
            if k:
                byname.setdefault(k, []).append((lat, lon, cty, row['dep_id']))
            d = canon(row.get('district'))
            if d:
                bydist.setdefault(d, []).append((lat, lon, cty))
            meta[row['dep_id']] = {
                'ds': row.get('dev_stat') or None,
                'wt': row.get('work_type') or None,
                'pz': row.get('prod_size') or None,
                'com': ', '.join(c for c in (row.get('commod1'),
                                             row.get('commod2'),
                                             row.get('commod3')) if c) or None,
                'cnty': row.get('county') or None}
    _MRDS_ST[st] = (byname, bydist, meta)
    return _MRDS_ST[st]

def _cluster(hits):
    """Accept a hit list only if it clusters within ~5 km."""
    lats = [h[0] for h in hits]; lons = [h[1] for h in hits]
    if max(lats) - min(lats) < 0.05 and max(lons) - min(lons) < 0.05:
        return (lons[0], lats[0], hits[0][3] if len(hits[0]) > 3 else None)
    return None

def locate_by_county(rows, state):
    """WS9 geolocation: MRDS name match scoped by the row's county;
    fallback to district-centroid for district roll-ups. Never invented:
    ambiguous names stay unlocated."""
    byname, bydist, meta = mrds_state(state)
    hit = miss = 0
    for r in rows:
        if r.get('lon') is not None:
            r['x'], r['y'] = r['lon'], r['lat']
            hit += 1
            continue
        cty = (r.get('county') or '').strip().lower()
        found = None
        # 1) exact canon name within county
        cands = [h for h in byname.get(canon(base_name(r['name'])), [])
                 if not cty or not h[2] or h[2] == cty]
        if cands:
            found = _cluster(cands)
        # 2) key containment within county
        if not found:
            for k in (r.get('keys') or []):
                ck = canon(k)
                if not ck:
                    continue
                cands = []
                for nm, hits in byname.items():
                    if ck in nm and not any(e in nm for e in r.get('excl', ())):
                        cands += [h for h in hits if not cty or not h[2]
                                  or h[2] == cty]
                if cands:
                    c = _cluster(cands)
                    if c:
                        found = c
                        break
        # 3) district centroid (roll-ups and last resort for named district)
        if not found and r.get('district'):
            dk = canon(re.sub(r'\(.*$', '', r['district']))
            hits = [h for h in bydist.get(dk, [])
                    if not cty or not h[2] or h[2] == cty]
            if len(hits) >= 2:
                lats = sorted(h[0] for h in hits); lons = sorted(h[1] for h in hits)
                found = (lons[len(lons) // 2], lats[len(lats) // 2], None)
        if found:
            r['x'], r['y'] = found[0], found[1]
            if found[2]:
                r.setdefault('dep', found[2])
                m = meta.get(found[2]) or {}
                for f in ('ds', 'wt', 'pz'):
                    r.setdefault(f, m.get(f))
                if not r.get('commodities'):
                    r['commodities'] = m.get('com')
            hit += 1
        else:
            r['x'] = r['y'] = None
            miss += 1
    print(f'  located {hit}/{len(rows)} ({state}, county-scoped MRDS match; '
          f'{miss} unlocated)')
    return rows

def locate(rows, anchors, state, radius_km=20):
    """grades_ca.py round-1 convention: name match near a district anchor."""
    pts = mrds_index(state)
    hit = 0
    for r in rows:
        if r.get('lon') is not None:               # curated coordinate wins
            r['x'], r['y'] = r['lon'], r['lat']
            hit += 1
            continue
        ax, ay = anchors[r['anchor']]
        coslat = math.cos(math.radians(ay))
        best, bs = None, 1e9
        for cn, x, y in pts:
            dk = math.hypot((x - ax) * 111.32 * coslat, (y - ay) * 111.32)
            if dk > radius_km:
                continue
            q = 0
            for k in r.get('keys') or [r['name']]:
                ck = canon(k)
                if cn == ck:
                    q = 2
                    break
                if ck and ck in cn:
                    q = max(q, 1)
            if q == 0 or any(e in cn for e in r.get('excl', ())):
                continue
            score = dk - 25 * q
            if score < bs:
                bs, best = score, (x, y)
        if best:
            r['x'], r['y'] = best
            hit += 1
        else:
            r['x'] = r['y'] = None
    print(f'  located {hit}/{len(rows)} rows ({state})')
    return rows

# ----------------------------------------------------------- open ground ---
def open_metres(rows, state):
    """Attach typed open-ground state plus the legacy numeric distance.

    `status` is the authoritative discriminator. `distance_m=0` is a real
    measured zero and therefore sorts differently from `not_applicable` and
    `unknown`, both of which carry null distance and score components.
    """
    try:
        from state_registry import load_state
        reg = load_state(state)
    except (FileNotFoundError, ValueError):
        reg = None
    if reg and reg['regime'] == 'non_claim':
        for r in rows:
            r['open_ground'] = {'status': 'not_applicable', 'distance_m': None,
                                'score': None,
                                'reason': 'No federal or state staking system applies.'}
            r['open'] = None
        print(f'  {state}: open ground not applicable (non-claim regime)')
        return rows
    try:
        c = load_build_artifact('claims', f'{state.lower()}_active')
    except (FileNotFoundError, ValueError):
        for r in rows:
            r['open_ground'] = {'status': 'unknown', 'distance_m': None,
                                'score': None,
                                'reason': 'Active-claim artifact is missing for a claim state.'}
            r['open'] = None
        print(f'  no active-claims file for {state}: open ground unknown')
        return rows
    grid = {}
    for x, y in zip(c['x'], c['y']):
        if x is None:
            continue
        grid.setdefault((int(x / 0.02), int(y / 0.02)), []).append((x, y))
    for r in rows:
        if r.get('x') is None:
            r['open_ground'] = {'status': 'unknown', 'distance_m': None,
                                'score': None,
                                'reason': 'Mine location is unresolved.'}
            r['open'] = None
            continue
        x, y = r['x'], r['y']
        coslat = math.cos(math.radians(y))
        gx, gy = int(x / 0.02), int(y / 0.02)
        best = 1e12
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                for cx, cy in grid.get((gx + dx, gy + dy), ()):
                    d = math.hypot((cx - x) * 111320 * coslat,
                                   (cy - y) * 111320)
                    best = min(best, d)
        distance = 5000 if best > 5000 else int(round(best))
        r['open'] = distance
        r['open_ground'] = {'status': 'measured', 'distance_m': distance,
                            'score': None,
                            'source': f'private build input claims.{state.lower()}_active'}
    return rows

# ------------------------------------------------- grades.json splice/merge --
NEWCOLS = ('pb', 'zn', 'cu', 'sb', 'wo3', 'hgf', 'yd3', 'plc',
           'nat', 'conv', 'own', 'xq', 'open_ground')

def load_grades():
    p = os.path.join(SITE, 'data/grades/grades.json')
    g = json.load(open(p))
    for k in NEWCOLS:                       # schema migration, idempotent
        if k not in g:
            g[k] = [None] * g['n']
    return g, p

def listcols(g):
    return [k for k in g if isinstance(g[k], list)]

def drop_own(g, own):
    """Remove rows owned by `own`; on shared rows, revert any primary
    upgrade this owner made (the pre-upgrade primary is stashed in an xq
    entry tagged '<own>:prev') and strip this owner's extra quotes. Makes
    the splice fully rebuildable."""
    keep = [i for i in range(g['n']) if (g.get('own') or [None] * g['n'])[i] != own]
    for k in listcols(g):
        g[k] = [g[k][i] for i in keep]
    g['n'] = len(keep)
    prev_tag = own + ':prev'
    for i in range(g['n']):
        if not g['xq'][i]:
            continue
        prevs = [q for q in g['xq'][i] if q.get('own') == prev_tag]
        if prevs:                      # undo this owner's primary upgrade
            q = prevs[0]
            g['quote'][i] = q['q']; g['src'][i] = q['s']; g['url'][i] = q['u']
            g['basis'][i] = q.get('b'); g['yrs'][i] = q.get('y')
            for col in ('au', 'ag', 'usd', 'nat', 'conv'):
                if col in q:
                    g[col][i] = q[col]
        removed = [q for q in g['xq'][i]
                   if q.get('own') in (own, prev_tag)]
        if removed:
            g['nrec'][i] = max(1, (g['nrec'][i] or 1) - len(removed))
        g['xq'][i] = [q for q in g['xq'][i]
                      if q.get('own') not in (own, prev_tag)] or None
    return g

def key_of(name, cnty, st):
    return (canon(name), (cnty or '').strip().lower() or None, st)

def find_existing(g, name, cnty, st, x=None, y=None):
    """mine+county match against existing rows; county may be unknown on the
    dataset side (library rows) — then accept a name match within ~15 km."""
    cn = canon(base_name(name))
    best = None
    for i in range(g['n']):
        if g['st'][i] != st:
            continue
        if canon(base_name(g['name'][i])) != cn:
            continue
        gc = (g['cnty'][i] or '').strip().lower() or None
        c = (cnty or '').strip().lower() or None
        if gc and c and gc != c:
            continue
        if gc is None or c is None:
            if x is not None and g['x'][i] is not None:
                d = math.hypot((g['x'][i] - x) * 111.32 *
                               math.cos(math.radians(y)),
                               (g['y'][i] - y) * 111.32)
                if d > 15:
                    continue
        best = i
        break
    return best

def find_existing_row(g, r, st):
    """find_existing + alias pass over the curated row's match keys."""
    i = find_existing(g, r['name'], r.get('county'), st, r.get('x'), r.get('y'))
    if i is not None:
        return i
    c = (r.get('county') or '').strip().lower() or None
    for k in (r.get('keys') or []):
        ck = canon(k)
        if not ck or ck == canon(base_name(r['name'])):
            continue
        for i in range(g['n']):
            if g['st'][i] != st or canon(base_name(g['name'][i])) != ck:
                continue
            gc = (g['cnty'][i] or '').strip().lower() or None
            if gc and c and gc != c:
                continue
            return i
    return None

BASIS_RANK = {'production average': 5, 'production': 5, 'ore shipped': 4,
              'resource estimate': 4, 'assay': 3, 'district production': 3,
              'value-text': 2, 'assay-text': 1}

def enrich(g, i, r, own):
    """Additive merge of curated row r into existing row i (never duplicate).
    The row joins as an extra quote and fills gaps; it takes over as the
    PRIMARY grade only when it is richer AND its basis is at least as
    trustworthy (round-0 'best cited grade per mine' semantics with a
    bonanza guard)."""
    new_au = r.get('au_opt')
    old_au = g['au'][i]
    upgrade = (new_au is not None and
               (old_au is None or new_au > old_au) and
               BASIS_RANK.get(r.get('basis'), 0) >=
               BASIS_RANK.get(g['basis'][i], 0))
    if upgrade:
        # old primary becomes an extra quote tagged '<own>:prev' so drop_own
        # can restore it (revertible upgrade); r becomes the primary
        oldq = {'q': g['quote'][i], 's': g['src'][i], 'u': g['url'][i],
                'b': g['basis'][i], 'y': g['yrs'][i], 'own': own + ':prev',
                'au': g['au'][i], 'ag': g['ag'][i], 'usd': g['usd'][i],
                'nat': g['nat'][i], 'conv': g['conv'][i]}
        g['xq'][i] = (g['xq'][i] or []) + [oldq]
        g['quote'][i] = r['quote']
        g['src'][i] = r['src_cite']; g['url'][i] = r['src_url']
        g['basis'][i] = r.get('basis'); g['yrs'][i] = r.get('years')
        g['au'][i] = new_au
        if r.get('ag_opt') is not None:
            g['ag'][i] = r['ag_opt']
        if r.get('usd_per_ton') is not None:
            g['usd'][i] = r['usd_per_ton']
        if r.get('nat'):
            g['nat'][i] = r['nat']
        if r.get('conv'):
            g['conv'][i] = r['conv']
        g['nrec'][i] = (g['nrec'][i] or 1) + 1
        _fill_gaps(g, i, r)
        return
    xq = {'q': r['quote'], 's': r['src_cite'], 'u': r['src_url'],
          'b': r.get('basis'), 'y': r.get('years'), 'own': own}
    if r.get('nat'):
        xq['n'] = r['nat']
    g['xq'][i] = (g['xq'][i] or []) + [xq]
    _fill_gaps(g, i, r)
    g['nrec'][i] = (g['nrec'][i] or 1) + 1

def _fill_gaps(g, i, r):
    for col, f in (('au', 'au_opt'), ('ag', 'ag_opt'), ('usd', 'usd_per_ton'),
                   ('pb', 'pb_pct'), ('zn', 'zn_pct'), ('cu', 'cu_pct'),
                   ('sb', 'sb_pct'), ('wo3', 'wo3_units'), ('hgf', 'hg_flasks'),
                   ('yd3', 'usd_per_yd3'), ('nat', 'nat'), ('conv', 'conv')):
        if g[col][i] is None and r.get(f) is not None:
            g[col][i] = r[f]
    if g['cnty'][i] is None and r.get('county'):
        g['cnty'][i] = r['county']
    if g['ton'][i] is None and r.get('tonnage'):
        g['ton'][i] = r['tonnage']
    if g['x'][i] is None and r.get('x') is not None:
        g['x'][i] = round(r['x'], 5); g['y'][i] = round(r['y'], 5)
    for col in ('ds', 'wt', 'pz'):
        if g[col][i] is None and r.get(col):
            g[col][i] = r[col]
    if g['dep'][i] is None and r.get('dep'):
        g['dep'][i] = r['dep']
    if g['com'][i] is None and r.get('commodities'):
        g['com'][i] = r['commodities']
    if r.get('plc'):
        g['plc'][i] = 1

def append_row(g, r, st, own):
    g['name'].append(r['name']); g['st'].append(st)
    g['dist'].append(r.get('district'))
    g['x'].append(round(r['x'], 5) if r.get('x') is not None else None)
    g['y'].append(round(r['y'], 5) if r.get('y') is not None else None)
    g['au'].append(r.get('au_opt')); g['ag'].append(r.get('ag_opt'))
    g['usd'].append(r.get('usd_per_ton'))
    g['basis'].append(r.get('basis')); g['yrs'].append(r.get('years'))
    g['open'].append(r.get('open')); g['open_ground'].append(
        r.get('open_ground') or {'status': 'unknown', 'distance_m': None,
                                 'score': None, 'reason': 'Legacy row; not evaluated'})
    g['nrec'].append(1)
    g['dep'].append(r.get('dep'))
    g['quote'].append(r['quote'])
    g['src'].append(r['src_cite']); g['url'].append(r['src_url'])
    g['ton'].append(r.get('tonnage'))
    g['ds'].append(r.get('ds')); g['wt'].append(r.get('wt'))
    g['pz'].append({'S': 'Small', 'M': 'Medium', 'L': 'Large'}.get(
        r.get('pz'), r.get('pz')))
    g['com'].append(r.get('commodities'))
    g['cnty'].append(r.get('county'))
    g['pb'].append(r.get('pb_pct')); g['zn'].append(r.get('zn_pct'))
    g['cu'].append(r.get('cu_pct')); g['sb'].append(r.get('sb_pct'))
    g['wo3'].append(r.get('wo3_units')); g['hgf'].append(r.get('hg_flasks'))
    g['yd3'].append(r.get('usd_per_yd3')); g['plc'].append(1 if r.get('plc') else None)
    g['nat'].append(r.get('nat')); g['conv'].append(r.get('conv'))
    g['own'].append(own); g['xq'].append(None)
    g['n'] += 1

def splice(rows, st, own, note_add):
    """Idempotent: drop own-tagged rows/enrichments, re-merge `rows`."""
    g, p = load_grades()
    g = drop_own(g, own)
    added = enriched = 0
    for r in rows:
        i = find_existing_row(g, r, st)
        if i is not None:
            enrich(g, i, r, own)
            enriched += 1
        else:
            append_row(g, r, st, own)
            added += 1
    g['generated'] = time.strftime('%Y-%m-%d')
    if note_add and note_add not in g['note']:
        g['note'] += ' ' + note_add
    json.dump(g, open(p, 'w'), separators=(',', ':'))
    mp = os.path.join(SITE, 'data/manifest.json')
    try:
        man = json.load(open(mp))
        man['grades'] = {'file': 'data/grades/grades.json', 'n': g['n'],
                         'retrieved': g['generated']}
        json.dump(man, open(mp, 'w'), separators=(',', ':'))
    except FileNotFoundError:
        pass
    print(f'  splice[{own}]: +{added} new rows, {enriched} enriched, '
          f'total {g["n"]}')
    return added, enriched

# ------------------------------------------------------------- row intake ---
def curated(path):
    """Load a curated rows JSON file (grades-research/rows_*.json)."""
    return json.load(open(path))

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'pagetext':
        d = build_pagetext(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None,
                           force='--force' in sys.argv)
        print(json.dumps({'key': d['key'], 'pages': d['n'],
                          'offset': d['offset']}))
    elif cmd == 'triage':
        t = triage(sys.argv[2])
        print(json.dumps({'key': t['key'], 'pages': t['n'],
                          'hit_pages': t['hit_pages'],
                          'top': t['ranked'][:30]}))
    else:
        print('usage: gradeslib.py pagetext <key> [url] | triage <key>')
