#!/usr/bin/env python3
"""Extract Au/Ag ore grades from the USGS MRDS relational dump for WA/OR/ID/MT/WY/NV/UT.
Sources inside the dump: Production_detail + Resource_detail (structured grd/grd_units),
Analytical_data + Comments (+ Production.work_cmt) via regex over free text."""
import csv, json, re, sys
csv.field_size_limit(10**9)

STATES = {'Washington':'WA','Oregon':'OR','Idaho':'ID','Montana':'MT','Wyoming':'WY','Nevada':'NV','Utah':'UT'}
R = '/tmp/rdbms/'

# dep_id -> site meta from the national flat file
site = {}
with open('/home/claude/mining-data/mrds.csv', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f):
        st = STATES.get(row.get('state') or '')
        if not st: continue
        try: lat, lon = round(float(row['latitude']),5), round(float(row['longitude']),5)
        except (ValueError, TypeError): lat = lon = None
        site[row['dep_id']] = (row.get('site_name') or 'Unnamed', st, row.get('county') or '', lat, lon)
print('sites in 7 states:', len(site))

def to_opt(val, units):
    u = (units or '').lower().strip()
    try: v = float(val)
    except (ValueError, TypeError): return None
    if v <= 0: return None
    if re.search(r'oz.*(/|per)?\s*(s?t|st|ton)|tr[ -]?oz', u) or u in ('oz/t','oz/st','ozpt','oz'):
        return v
    if re.search(r'^(g|gm|gms|grams?)\s*/?\s*(t|mt|tonne|ton)', u) or u in ('g/t','g/mt','gm/mt','ppm','mg/kg'):
        return v / 34.2857
    if 'pct' in u or u == '%' or 'percent' in u:
        return ('PCT', v * 291.667)   # flagged: caller decides (Au-in-percent = unit error)
    if u in ('ppb',): return v / 34285.7
    return None

recs = []
def add(dep, au, ag, usd, era, yr, basis, quote, table):
    meta = site.get(dep)
    if not meta: return
    name, st, county, lat, lon = meta
    structured = basis in ('production average', 'resource estimate')
    # sanity ceilings: structured tables report averages (Au>50 opt = unit error);
    # free-text can be hand-picked specimens (real ceiling ~610 opt Au on record)
    au_cap = 50 if structured else 650
    ag_cap = 3000 if structured else 10000
    if au is not None and not (0.001 <= au <= au_cap): au = None
    if ag is not None and not (0.01 <= ag <= ag_cap): ag = None
    if au is None and ag is None and usd is None: return
    recs.append({'mine_name': name, 'district': None, 'state': st, 'county': county,
        'lat': lat, 'lon': lon, 'au_opt': round(au,3) if au else None, 'ag_opt': round(ag,2) if ag else None,
        'dollars_per_ton': usd, 'dollar_era': era, 'tonnage': None, 'years': yr or None,
        'basis': basis, 'quote': quote[:200], 'dep_id': dep,
        'source': {'title': f'USGS MRDS {table} (dep_id {dep})',
                   'url': f'https://mrdata.usgs.gov/mrds/show-mrds.php?dep_id={dep}', 'page': None}})

def strip(s): return (s or '').strip().strip('"')

# ---- structured tables ----
for fname, basis in (('Production_detail','production average'), ('Resource_detail','resource estimate')):
    n = 0
    with open(R+fname+'.txt', encoding='utf-8', errors='replace') as f:
        rd = csv.DictReader(f, delimiter='\t')
        for row in rd:
            code = strip(row.get('code')).upper()
            if code not in ('AU','AG'): continue
            dep = strip(row.get('dep_id'))
            opt = to_opt(strip(row.get('grd')), strip(row.get('grd_units')))
            if opt is None: continue
            if isinstance(opt, tuple):          # percent units
                if code == 'AU': continue       # gold-in-percent = data-entry error
                opt = opt[1]
            q = f"{strip(row.get('commod'))} grade {strip(row.get('grd'))} {strip(row.get('grd_units'))} ({fname}, yr {strip(row.get('yr')) or '?'})"
            add(dep, opt if code=='AU' else None, opt if code=='AG' else None, None, None,
                strip(row.get('yr')), basis, q, fname)
            n += 1
    print(fname, 'grade rows kept:', n)

# ---- free-text regex ----
NUM = r'(\.\d{1,3}|\d{1,4}(?:[.,]\d{1,3})?)'   # allows leading-decimal OCR like ".34"
OZ = re.compile(NUM + r'\s*(?:troy\s+)?(?:oz|ozs?\.?|ounces?)\s*(?:of\s+)?(gold|silver|au|ag)?\s*(?:per|/|a|to\s+the)\s*(?:short\s+)?ton', re.I)
OZ2 = re.compile(r'(gold|silver)\s*(?:values?|content|grade|assays?|averag\w*|runs?|ran)[^.;]{0,30}?' + NUM + r'\s*(?:oz|ounces?)', re.I)
USD = re.compile(r'\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:per|a|/|to\s+the)\s*ton', re.I)
def scan_text(dep, text, yr, table, ctg=''):
    if not text or 'ton' not in text.lower(): return
    t = text
    for m in OZ.finditer(t):
        val = float(m.group(1).replace(',',''))
        com = (m.group(2) or '').lower()
        ctx = t[max(0,m.start()-90):m.end()+60]
        if not com:  # attribute by nearby word
            low = ctx.lower()
            com = 'gold' if ('gold' in low and 'silver' not in low) else ('silver' if 'silver' in low else '')
        if com in ('gold','au'): add(dep, val, None, None, None, yr, 'assay-text', ctx, table)
        elif com in ('silver','ag'): add(dep, None, val, None, None, yr, 'assay-text', ctx, table)
    for m in OZ2.finditer(t):
        val = float(m.group(2).replace(',',''))
        ctx = t[max(0,m.start()-60):m.end()+80]
        if 'per ton' not in ctx.lower() and '/ton' not in ctx.lower(): continue
        if m.group(1).lower()=='gold': add(dep, val, None, None, None, yr, 'assay-text', ctx, table)
        else: add(dep, None, val, None, None, yr, 'assay-text', ctx, table)
    for m in USD.finditer(t):
        val = float(m.group(1).replace(',',''))
        if val < 1 or val > 50000: continue
        ctx = t[max(0,m.start()-90):m.end()+60]
        add(dep, None, None, val, None, yr, 'value-text', ctx, table)

with open(R+'Analytical_data.txt', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f, delimiter='\t'):
        scan_text(strip(row.get('dep_id')), strip(row.get('anl_data')), None, 'Analytical_data')
print('after analytical:', len(recs))

with open(R+'Comments.txt', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f, delimiter='\t'):
        scan_text(strip(row.get('dep_id')), strip(row.get('cmt_txt')), None, 'Comments/'+strip(row.get('ctg')))
print('after comments:', len(recs))

with open(R+'Production.txt', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f, delimiter='\t'):
        scan_text(strip(row.get('dep_id')), strip(row.get('work_cmt')), strip(row.get('yr')), 'Production')
print('after production work_cmt:', len(recs))

# dedupe identical (dep, au, ag, usd, basis)
seen, out = set(), []
for r in recs:
    k = (r['dep_id'], r['au_opt'], r['ag_opt'], r['dollars_per_ton'], r['basis'])
    if k in seen: continue
    seen.add(k); out.append(r)
json.dump(out, open('/home/claude/nw/grades/raw_mrds.json','w'), separators=(',',':'))
from collections import Counter
print('FINAL records:', len(out), '| by state:', Counter(r['state'] for r in out))
print('with au:', sum(1 for r in out if r['au_opt']), '| with ag:', sum(1 for r in out if r['ag_opt']),
      '| $/ton only:', sum(1 for r in out if not r['au_opt'] and not r['ag_opt']))
print('top au:', sorted(((r['au_opt'], r['mine_name'], r['state']) for r in out if r['au_opt']), reverse=True)[:8])
