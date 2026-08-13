#!/usr/bin/env python3
"""Merge grade records (library agents + MRDS dump), geolocate, aggregate per mine,
compute distance-to-nearest-active-claim, emit site/data/grades/grades.json."""
import json, re, math, csv, os
from collections import defaultdict
csv.field_size_limit(10**9)

G = '/home/claude/nw/grades/'
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
raw = (json.load(open(G+'raw_greatbasin_nvut.json')) +
       json.load(open(G+'raw_id_or.json')) +
       json.load(open(G+'raw_mrds.json')))
print('raw records:', len(raw))

# ---- name index from national MRDS (7 states) for geolocating library records ----
STATES = {'Washington':'WA','Oregon':'OR','Idaho':'ID','Montana':'MT','Wyoming':'WY','Nevada':'NV','Utah':'UT'}
def norm(n):
    n = (n or '').lower()
    n = re.sub(r'\b(the|mine|mines|mining|group|claims?|property|prospect|lode|no\.?\s*\d+|shaft|tunnel|lease)\b', ' ', n)
    return re.sub(r'[^a-z0-9]+', ' ', n).strip()
byname = defaultdict(list)
with open('/home/claude/mining-data/mrds.csv', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f):
        st = STATES.get(row.get('state') or '')
        if not st: continue
        try: lat, lon = round(float(row['latitude']),5), round(float(row['longitude']),5)
        except (ValueError, TypeError): continue
        k = norm(row.get('site_name'))
        if k: byname[(st,k)].append((lat,lon,row['dep_id']))

# ---- enrichment: tonnage + site meta by dep_id ----
meta = {}
with open('/home/claude/mining-data/mrds.csv', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f):
        st = STATES.get(row.get('state') or '')
        if not st: continue
        meta[row['dep_id']] = {'ds': row.get('dev_stat') or None, 'wt': row.get('work_type') or None,
            'pz': row.get('prod_size') or None, 'com': ', '.join(c for c in (row.get('commod1'), row.get('commod2'), row.get('commod3')) if c) or None,
            'cnty': row.get('county') or None}
def strip(s): return (s or '').strip().strip('"')
TONTXT = re.compile(r'([\d,]{2,12})\s*(short\s+|metric\s+)?tons?\b', re.I)
prod_t, res_t = {}, {}
with open('/tmp/rdbms/Production.txt', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f, delimiter='\t'):
        dep = strip(row.get('dep_id'))
        if dep not in meta: continue
        mined, units, yr = strip(row.get('mined')), strip(row.get('units')), strip(row.get('yr'))
        s = None
        if mined:
            try: s = f"{float(mined):,.0f} {units or 'tons'} mined" + (f" ({yr})" if yr else '')
            except ValueError: pass
        if not s:
            m = TONTXT.search(strip(row.get('work_cmt')))
            if m: s = f"{m.group(1)} tons (production note{', '+yr if yr else ''})"
        if s and (dep not in prod_t or len(s) > len(prod_t[dep])): prod_t[dep] = s
with open('/tmp/rdbms/Resources.txt', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f, delimiter='\t'):
        dep = strip(row.get('dep_id'))
        if dep not in meta: continue
        tot, units, yr = strip(row.get('tot_resources')), strip(row.get('units')), strip(row.get('yr'))
        if not tot: tot = strip(row.get('reserves'))
        if tot:
            try: s = f"resource {float(tot):,.0f} {units or 'mt'}" + (f" ({yr})" if yr else '')
            except ValueError: continue
            if dep not in res_t or len(s) > len(res_t[dep]): res_t[dep] = s
print('tonnage: production', len(prod_t), '| resources', len(res_t), '| meta', len(meta))

matched = 0
for r in raw:
    if r.get('lat') is not None: continue
    hits = byname.get((r['state'], norm(r['mine_name'])), [])
    # unique location cluster? (same mine may have several rows w/ near-identical coords)
    if hits:
        lats = [h[0] for h in hits]; lons = [h[1] for h in hits]
        if max(lats)-min(lats) < 0.05 and max(lons)-min(lons) < 0.05:
            r['lat'], r['lon'] = lats[0], lons[0]
            r.setdefault('dep_id', hits[0][2]); matched += 1
print('library records geolocated by name match:', matched)

# ---- aggregate per mine (state + normalized name + rounded coords) ----
mines = {}
for r in raw:
    key = (r['state'], norm(r['mine_name']),
           None if r.get('lat') is None else (round(r['lat'],3), round(r['lon'],3)))
    m = mines.setdefault(key, {'name': r['mine_name'], 'st': r['state'],
        'dist': r.get('district'), 'x': r.get('lon'), 'y': r.get('lat'),
        'dep': r.get('dep_id'), 'recs': []})
    if not m['dist'] and r.get('district'): m['dist'] = r['district']
    m['recs'].append(r)
print('unique mines:', len(mines))

# ---- open-ground: nearest active-claim centroid (5-state footprint only) ----
grid = defaultdict(list)
import glob
for f in glob.glob(os.path.join(ROOT, 'build-inputs', 'data', 'claims',
                               '*_active.json')):
    d = json.load(open(f))
    for i in range(d['n']):
        x, y = d['x'][i], d['y'][i]
        grid[(int(x/0.05), int(y/0.05))].append((x,y))
print('claim cells:', len(grid))
def near_claim_m(x, y):
    cx, cy = int(x/0.05), int(y/0.05)
    best = None
    for dx in (-1,0,1):
        for dy in (-1,0,1):
            for (px,py) in grid.get((cx+dx, cy+dy), ()):
                p = math.pi/180
                a = .5-math.cos((py-y)*p)/2+math.cos(y*p)*math.cos(py*p)*(1-math.cos((px-x)*p))/2
                d = 12742000*math.asin(math.sqrt(a))
                if best is None or d < best: best = d
    return None if best is None else int(best)   # None = nothing within ~5km cellring

FOOT = {'WA','OR','ID','MT','WY','NV','UT'}
out = {'n':0,'name':[],'st':[],'dist':[],'x':[],'y':[],'au':[],'ag':[],'usd':[],
       'basis':[],'yrs':[],'open':[],'nrec':[],'dep':[],'quote':[],'src':[],'url':[],
       'ton':[],'ds':[],'wt':[],'pz':[],'com':[],'cnty':[]}
def best_of(m, field):
    vals = [(r[field], r) for r in m['recs'] if r.get(field) is not None]
    return max(vals, key=lambda v: v[0]) if vals else (None, None)
for m in mines.values():
    au, r_au = best_of(m, 'au_opt')
    ag, r_ag = best_of(m, 'ag_opt')
    usd, r_usd = best_of(m, 'dollars_per_ton')
    lead = r_au or r_ag or r_usd
    if lead is None: continue
    if m['x'] is not None and m['st'] in FOOT:
        op = near_claim_m(m['x'], m['y'])
        op = 5000 if op is None else op          # >cell ring ≈ open beyond 5 km
    else:
        op = -1                                   # unknown (no claims data: NV/UT or unlocated)
    out['n'] += 1
    out['name'].append(m['name']); out['st'].append(m['st']); out['dist'].append(m['dist'])
    out['x'].append(m['x']); out['y'].append(m['y'])
    out['au'].append(au); out['ag'].append(ag); out['usd'].append(usd)
    out['basis'].append(lead['basis']); out['yrs'].append(lead.get('years'))
    out['open'].append(op); out['nrec'].append(len(m['recs'])); out['dep'].append(m.get('dep'))
    out['quote'].append(lead['quote'])
    out['src'].append(lead['source']['title'] + (f", {lead['source']['page']}" if lead['source'].get('page') else ''))
    out['url'].append(lead['source']['url'])
    # tonnage: library statement > MRDS production > MRDS resource (combine when both MRDS kinds exist)
    dep = m.get('dep')
    lib_t = next((r['tonnage'] for r in m['recs'] if r.get('tonnage')), None)
    if isinstance(lib_t, (int, float)): lib_t = f"{lib_t:,.0f} tons"
    parts = [p for p in (prod_t.get(dep), res_t.get(dep)) if p]
    out['ton'].append(lib_t or (' · '.join(parts) if parts else None))
    mt = meta.get(dep, {})
    out['ds'].append(mt.get('ds')); out['wt'].append(mt.get('wt'))
    out['pz'].append({'S':'Small','M':'Medium','L':'Large'}.get(mt.get('pz'), mt.get('pz')))
    out['com'].append(mt.get('com')); out['cnty'].append(mt.get('cnty'))
out['generated'] = '2026-07-30'
out['note'] = ('best cited grade per mine; open = metres to nearest ACTIVE claim centroid '
               '(5000 = none within ~5 km; -1 = unknown: outside claims footprint or unlocated). '
               'assay-text/bonanza values are hand-picked specimens, not mine averages — read the quote.')
grades_path = os.path.join(ROOT, 'site', 'data', 'grades', 'grades.json')
os.makedirs(os.path.dirname(grades_path), exist_ok=True)
json.dump(out, open(grades_path,'w'), separators=(',',':'))
from collections import Counter
print('mines emitted:', out['n'], '| by state:', Counter(out['st']))
print('located:', sum(1 for x in out['x'] if x is not None),
      '| open>=400m:', sum(1 for i,o in enumerate(out['open']) if o>=400 and out['au'][i]))
print('gold mines open>=400m w/ au>=0.3:', sum(1 for i in range(out['n']) if out['open'][i]>=400 and (out['au'][i] or 0)>=0.3))
# manifest
manifest_path = os.path.join(ROOT, 'site', 'data', 'manifest.json')
man = json.load(open(manifest_path))
man['grades'] = {'file':'data/grades/grades.json','n':out['n'],'retrieved':'2026-07-30'}
json.dump(man, open(manifest_path,'w'), separators=(',',':'))
print('manifest updated')
