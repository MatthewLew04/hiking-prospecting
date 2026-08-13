#!/usr/bin/env python3
"""Promote NV + UT to first-class map states: MRDS sites, USMIN points, boundaries,
auto-districts; rebuild manifest totals for 7 states."""
import csv, json, collections, shapefile, re, glob, os, hashlib
csv.field_size_limit(10**9)
import sys; sys.path.insert(0, '.')
from extract_nw import classify, GROUPS_ORDER, SMAP  # reuse grouping/status maps

OUT = 'site/data/'
PRIVATE = 'build-inputs/data/'
R5 = lambda v: round(float(v), 5)
NEW = {'Nevada':'NV','Utah':'UT'}

# ---------- MRDS ----------
dist = {}
with open('/tmp/rdbms/Districts.txt', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f, delimiter='\t'):
        d = (row.get('district') or '').strip().strip('"'); i = (row.get('dep_id') or '').strip().strip('"')
        if d and i and i not in dist: dist[i] = d
by = {c: {k: [] for k in ('id','nm','st','g','c','d','x','y')} for c in NEW.values()}
auto = collections.defaultdict(lambda: {'n':0,'xs':0.0,'ys':0.0,'coms':collections.Counter()})
nbad = 0
with open('/home/claude/mining-data/mrds.csv', encoding='utf-8', errors='replace') as f:
    for row in csv.DictReader(f):
        code = NEW.get(row.get('state'))
        if not code: continue
        try: x, y = R5(row['longitude']), R5(row['latitude'])
        except (ValueError, TypeError): nbad += 1; continue
        if not (-120.5 < x < -108.5 and 34.5 < y < 42.5): nbad += 1; continue
        coms = ' · '.join(c for c in (row.get('commod1'), row.get('commod2'), row.get('commod3')) if c)
        d = dist.get(row['dep_id'], '')
        b = by[code]
        b['id'].append(row['dep_id']); b['nm'].append(row.get('site_name') or 'Unnamed')
        b['st'].append(SMAP.get(row.get('dev_stat') or 'Unknown','U'))
        b['g'].append(classify(coms)); b['c'].append(coms); b['d'].append(d)
        b['x'].append(x); b['y'].append(y)
        if d:
            a = auto[(code, d.title())]; a['n'] += 1; a['xs'] += x; a['ys'] += y
            if row.get('commod1'): a['coms'][row['commod1']] += 1
for code, b in by.items():
    o = {'src':'mrds','state':code,'retrieved':'USGS dump 2022 (legacy)','n':len(b['id']), **b}
    open(PRIVATE+f'sites/mrds_{code.lower()}.json','w').write(json.dumps(o, separators=(',',':'), ensure_ascii=False))
    print(f'mrds_{code.lower()}: {o["n"]}')
print('bad coords skipped:', nbad)

# ---------- auto-districts (merge into existing auto.json with same dedupe) ----------
def norm(n):
    n = n.lower(); n = re.sub(r'\b(district|dist\.?|area|mining)\b','', n)
    return re.sub(r'[^a-z0-9]+',' ', n).strip()
items = []
for (code, name), a in auto.items():
    if a['n'] >= 5:
        items.append({'name':name,'state':code,'n':a['n'],
                      'commodities':[c for c,_ in a['coms'].most_common(3)],
                      'lon':round(a['xs']/a['n'],4),'lat':round(a['ys']/a['n'],4)})
merged = {}
for it in sorted(items, key=lambda x:-x['n']):
    key = (it['state'], norm(it['name'])); hit = merged.get(key)
    if not hit:
        for (st,nm),m in merged.items():
            if st==it['state']:
                a,b2 = norm(it['name']), nm
                if a and b2 and (a in b2 or b2 in a) and abs(m['lon']-it['lon'])<0.35 and abs(m['lat']-it['lat'])<0.35:
                    hit = m; break
    if hit:
        tot = hit['n']+it['n']
        hit['lon']=round((hit['lon']*hit['n']+it['lon']*it['n'])/tot,4)
        hit['lat']=round((hit['lat']*hit['n']+it['lat']*it['n'])/tot,4); hit['n']=tot
    else: merged[key]=dict(it)
newauto = sorted(merged.values(), key=lambda x:-x['n'])
old = json.load(open(OUT+'districts/auto.json'))
allitems = old['items'] + newauto
open(OUT+'districts/auto.json','w').write(json.dumps({'src':'mrds-districts','n':len(allitems),'items':allitems}, separators=(',',':')))
print('auto districts: +', len(newauto), 'NV/UT →', len(allitems), 'total')

# ---------- USMIN ----------
ub = {c: {k: [] for k in ('t','q','yr','sc','x','y')} for c in NEW.values()}
TYPES, TIDX = [], {}
for si, series in enumerate(['24k','48k','625k']):
    rr = shapefile.Reader(f'/tmp/usmin/USGS_TopoMineSymbols_ver10_Shapefiles/USGS_TopoMineSymbols_{series}_Points')
    fields = [fl[0] for fl in rr.fields[1:]]; fi = {n:i for i,n in enumerate(fields)}
    st_i, ty_i, tn_i, td_i = fi['State'], fi['Ftr_Type'], fi['Topo_Name'], fi['Topo_Date']
    for sr in rr.iterShapeRecords():
        st = sr.record[st_i]
        if st not in ('NV','UT'): continue
        t = sr.record[ty_i] or 'Unknown'
        if t not in TIDX: TIDX[t] = len(TYPES); TYPES.append(t)
        pt = sr.shape.points[0]; b = ub[st]
        b['t'].append(TIDX[t]); b['q'].append(sr.record[tn_i] or '')
        try: b['yr'].append(int(sr.record[td_i]))
        except (ValueError, TypeError): b['yr'].append(0)
        b['sc'].append(si); b['x'].append(R5(pt[0])); b['y'].append(R5(pt[1]))
for code, b in ub.items():
    o = {'src':'usmin','state':code,'retrieved':'USMIN v10 (2023)','n':len(b['t']),'types':TYPES,'scales':['24k','48k','625k'], **b}
    open(PRIVATE+f'sites/usmin_{code.lower()}.json','w').write(json.dumps(o, separators=(',',':'), ensure_ascii=False))
    print(f'usmin_{code.lower()}: {o["n"]}')

# ---------- boundaries ----------
def rgeom(g, nd=4):
    def rc(c):
        if isinstance(c[0],(int,float)): return [round(c[0],nd), round(c[1],nd)]
        return [rc(v) for v in c]
    return {'type':g['type'],'coordinates':rc(g['coordinates'])}
sts = json.load(open(PRIVATE+'boundaries/states.json'))
have = {f['properties']['st'] for f in sts['features']}
rr = shapefile.Reader('/tmp/states/cb_2023_us_state_500k'); sf=[f[0] for f in rr.fields[1:]]
for s in rr.iterShapeRecords():
    rec = dict(zip(sf, s.record))
    if rec.get('STUSPS') in ('NV','UT') and rec['STUSPS'] not in have:
        sts['features'].append({'type':'Feature','properties':{'st':rec['STUSPS'],'name':rec['NAME'],
                                'fips':rec['STATEFP']},
                                'geometry':rgeom(s.shape.__geo_interface__)})
sts['n']=len(sts['features'])
open(PRIVATE+'boundaries/states.json','w').write(json.dumps(sts, separators=(',',':')))
cos = json.load(open(PRIVATE+'boundaries/counties.json'))
FIPS = {'32':'NV','49':'UT'}
rr = shapefile.Reader('/tmp/counties/cb_2023_us_county_500k'); cf=[f[0] for f in rr.fields[1:]]
for s in rr.iterShapeRecords():
    rec = dict(zip(cf, s.record))
    if rec.get('STATEFP') in FIPS:
        cos['features'].append({'type':'Feature','properties':{'st':FIPS[rec['STATEFP']],'name':rec['NAME'],
                                'fips':rec['GEOID']},
                                'geometry':rgeom(s.shape.__geo_interface__)})
cos['n']=len(cos['features'])
open(PRIVATE+'boundaries/counties.json','w').write(json.dumps(cos, separators=(',',':')))
print('boundaries: states', sts['n'], '| counties', cos['n'])

# ---------- strict private input inventory + public district count ----------
man = json.load(open('build-inputs/manifest.json'))
for st in ('nv','ut'):
    for kind in ('mrds','usmin'):
        f = PRIVATE+f'sites/{kind}_{st}.json'
        d = json.load(open(f)); man['sites'][f'{kind}_{st}'] = {'n':d['n'],'file':f'data/sites/{kind}_{st}.json','retrieved':d.get('retrieved')}
    f = PRIVATE+f'claims/{st}_active.json'
    d = json.load(open(f)); man['claims'][f'{st}_active'] = {'n':d['n'],'file':f'data/claims/{st}_active.json','retrieved':d.get('retrieved')}
man['totals']['sites'] = sum(v['n'] for v in man['sites'].values())
man['totals']['claims_active'] = sum(v['n'] for k,v in man['claims'].items() if 'active' in k)
man['totals']['claims_closed'] = sum(v['n'] for k,v in man['claims'].items() if 'closed' in k)
for key in ('states','counties'):
    f = PRIVATE+f'boundaries/{key}.json'; d = json.load(open(f))
    man['boundaries'][key].update(
        n=d['n'], bytes=os.path.getsize(f),
        sha256=hashlib.sha256(open(f,'rb').read()).hexdigest())
man['totals']['boundary_states'] = man['boundaries']['states']['n']
man['totals']['boundary_counties'] = man['boundaries']['counties']['n']
open('build-inputs/manifest.json','w').write(json.dumps(man, separators=(',',':')))
public = json.load(open(OUT+'manifest.json'))
public['districts']['auto']['n'] = len(allitems)
open(OUT+'manifest.json','w').write(json.dumps(public, separators=(',',':')))
print('private input totals:', man['totals'])
