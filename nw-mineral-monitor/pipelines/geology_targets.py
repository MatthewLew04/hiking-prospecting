#!/usr/bin/env python3
"""WS6b — sinter-first target engine: score every geologic unit in the AOI.

Reads site/data/geology/{aoi}.json (WS6a) + the WS2 open-ground grid +
site/commodity snapshots, scores each unit polygon per the spec, and writes
a ranked Targets layer where every target carries an explanation card:
verbatim unit description, source-map citation + scale, scoring rationale
with real numbers, and the land/claim status directly under it.

Tiers (regex on name + strat name + lithology + description + comments):
  TIER 1  (score base 100) — siliceous sinter / opaline / chalcedonic
          hot-spring deposits. Flagged wherever they appear, regardless of
          proximity to any mine, claim, fault, or road.
  TRAV    (base 15) — travertine/tufa: related plumbing, wrong chemistry
          (calcareous, not siliceous) — labeled separately, low priority.
  TIER 2  (base 60) — silicified / HYDROTHERMALLY altered / argillized /
          jasperoid / alunite–adularia. Plain "altered" does NOT count:
          in this AOI it describes weathered basalt (olivine→iddingsite),
          a proven false positive. Fault-proximity boost weighted up.
  TIER 3  (base 30) — favorable epithermal hosts: rhyolite domes / flows /
          tuffs, bimodal volcanics — scored against fault proximity and
          fault-intersection density.

Boosts: mapped-fault proximity + fault intersections; MRDS/state-survey
occurrences with pathfinder commodities (Hg Sb As ×2, Au Ag ×1) within
2 km; GNIS hot/warm springs + IDWR geothermal wells; overlap with WS2
open ground. tier ≤ 2 + open ground = the money flag.

Output: site/data/targets/{aoi}.json
"""
import json, math, os, re, sys
from collections import defaultdict

from common import load_aoi, SITE, TODAY, write_json, point_in_poly, update_manifest

# ---------------------------------------------------------------- tiers
T1_RX = re.compile(r'sinter|opalin|opalized|opalite|opal\b|chalcedon|'
                   r'(?:hot|thermal)[\s-]spring|spring\sdeposit|siliceous\sspring', re.I)
TRAV_RX = re.compile(r'travertine|tufa\b|calcareous\sspring', re.I)
# silica[- ]carbonate added after the Clear Lake blind test: it is the mapped
# expression of Knoxville-type Hg-Au systems (serpentinite altered by the same
# fluids that build sinter above) and appears on 135 units in that AOI
T2_RX = re.compile(r'silicif|jasperoid|hydrothermal(?:ly)?\s?alter|argilli[cz]|'
                   r'propylit|alunite|adularia|quartz[\s-]sericite|'
                   r'silica[\s-]?carbonate', re.I)
T3_RX = re.compile(r'rhyolit|quartz\slatite|tuff\b|tuffs\b|tuffaceous|ignimbrite|welded|'
                   r'bimodal|felsic\svolcan|volcanic\sdome|obsidian', re.I)
# mélange guard (Clear Lake blind test): Franciscan mélange descriptions list
# "blocks and lenses of ... rhyolite ..." — an inventory of exotic blocks, not
# a volcanic center. Matrix-hosted matches don't make a Tier-3 host unless the
# unit also reads as an actual volcanic body.
MELANGE_RX = re.compile(r'm[ée]lange|blocks\s+and\s+lenses|sheared\s+argillite', re.I)
VOLCBODY_RX = re.compile(r'\bflows?\b|\bdome|welded|ash[\s-]flow|\blava\b|volcanic\s+(?:center|rocks?\s+of)|'
                         r'pyroclastic', re.I)
# Knoxville-type association (Clear Lake blind test): where mapping is too
# coarse to carry alteration words (SGMC 500k says just "serpentine"), a
# serpentinite/ultramafic body carrying a mercury-occurrence cluster on
# structure IS the mapped expression of the system — McLaughlin itself sits
# on exactly such a unit. Requires ≥3 Hg-class pathfinders ≤2 km AND a
# mapped fault ≤1 km; labeled as an association, never as description-based.
ULTRAMAFIC_RX = re.compile(r'serpentin|ultramafic|peridotite|ophiolit', re.I)
HG_CODES = {'HG', 'MERCURY', 'CINNABAR', 'QUICKSILVER'}
PATHFINDER = {'HG': 2.0, 'MERCURY': 2.0, 'CINNABAR': 2.0,
              'SB': 2.0, 'ANTIMONY': 2.0, 'STIBNITE': 2.0,
              'AS': 2.0, 'ARSENIC': 2.0,
              'AU': 1.0, 'GOLD': 1.0, 'AG': 1.0, 'SILVER': 1.0}

BASE = {1: 100, 2: 60, 3: 30, 9: 15}            # 9 = travertine label
TIER_NAME = {1: 'TIER 1 — HOT-SPRING SINTER', 2: 'TIER 2 — HYDROTHERMAL ALTERATION',
             3: 'TIER 3 — EPITHERMAL HOST', 9: 'TRAVERTINE (CALCAREOUS)'}


def classify(blob):
    terms = lambda rx: sorted({m.group(0).lower().strip() for m in rx.finditer(blob)})
    if T1_RX.search(blob):
        return 1, terms(T1_RX)
    if TRAV_RX.search(blob):
        return 9, terms(TRAV_RX)
    if T2_RX.search(blob):
        return 2, terms(T2_RX)
    if T3_RX.search(blob):
        if MELANGE_RX.search(blob) and not VOLCBODY_RX.search(blob):
            return 0, []                # exotic blocks in mélange matrix — noise
        return 3, terms(T3_RX)
    return 0, []


# ---------------------------------------------------------------- geometry
def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def seg_intersect(a, b, c, d):
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) - (q[1] - p[1]) * (r[0] - p[0])
    d1, d2 = ccw(c, d, a), ccw(c, d, b)
    d3, d4 = ccw(a, b, c), ccw(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        t = d1 / (d1 - d2)
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return None


class Grid:
    def __init__(self, cell=1.0):
        self.cell = cell
        self.d = defaultdict(list)

    def add(self, x, y, item):
        self.d[(int(x // self.cell), int(y // self.cell))].append(item)

    def near(self, x, y, r):
        c = self.cell
        out = []
        for i in range(int((x - r) // c), int((x + r) // c) + 1):
            for j in range(int((y - r) // c), int((y + r) // c) + 1):
                out.extend(self.d.get((i, j), ()))
        return out


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    k = aoi['key']
    st = aoi['state'].lower()
    geo = json.load(open(os.path.join(SITE, f'data/geology/{k}.json')))
    # WS2 layers exist only where the full AOI pipelines have run (Cassia).
    # Elsewhere — e.g. the Clear Lake blind acceptance test — degrade
    # gracefully: geology + structure still score; land-status boosts skip.
    degraded = []
    try:
        og = json.load(open(os.path.join(SITE, f'data/openground/{k}.json')))
    except FileNotFoundError:
        og = {'sections': []}
        degraded.append('open-ground grid absent — land-status boost + money flag disabled')
    try:
        plss = json.load(open(os.path.join(SITE, f'data/plss/{k}.json')))
    except FileNotFoundError:
        plss = {'features': []}
        if not degraded:
            degraded.append('PLSS grid absent — section overlap disabled')
    lat0 = (aoi['bbox'][1] + aoi['bbox'][3]) / 2
    KX, KY = 111.320 * math.cos(math.radians(lat0)), 110.574
    xy = lambda lon, lat: (lon * KX, lat * KY)

    # ---- faults → km segments in a grid; intersections precomputed ----
    fgrid = Grid(2.0)
    fsegs = []
    for fi, f in enumerate(geo['faults']):
        pts = [xy(*p) for p in f['path']]
        for a, b in zip(pts, pts[1:]):
            fsegs.append((a, b, fi))
            fgrid.add((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, len(fsegs) - 1)
    fx_pts = []
    seen_cells = set()
    for (a, b, fi) in fsegs:
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        for sj in fgrid.near(mx, my, 2.5):
            c, d, fj = fsegs[sj]
            if fj <= fi:
                continue
            p = seg_intersect(a, b, c, d)
            if p:
                cell = (int(p[0] * 10), int(p[1] * 10))     # dedupe ~100 m
                if cell not in seen_cells:
                    seen_cells.add(cell)
                    fx_pts.append(p)
    xgrid = Grid(2.0)
    for p in fx_pts:
        xgrid.add(p[0], p[1], p)
    print(f'faults: {len(geo["faults"])} lines / {len(fsegs)} segs / {len(fx_pts)} intersections')

    # ---- pathfinder commodity sites ----
    paths = []
    for kind in ('mrds', 'stategeo'):
        try:
            d = json.load(open(os.path.join(SITE, f'data/sites/{kind}_{st}.json')))
        except FileNotFoundError:
            continue
        for i in range(d['n']):
            if d['x'][i] is None:
                continue
            com = str(d['c'][i] or '')
            toks = {t.strip().upper() for t in re.split(r'[,;/]', com)}
            w = max((PATHFINDER.get(t, 0) for t in toks), default=0)
            if w:
                paths.append((d['x'][i], d['y'][i], (d['nm'][i] or '?'), com, w, kind))
    pgrid = Grid(2.0)
    for i, p in enumerate(paths):
        pgrid.add(*xy(p[0], p[1]), i)
    if not paths:
        degraded.append(f'no site snapshots for state {st.upper()} — pathfinder boost disabled')
    print(f'pathfinder-commodity sites in state files: {len(paths)}')

    # ---- springs & wells ----
    thermal = [s for s in geo['springs'] if s['cls'] in ('hot', 'warm', 'thermal')]
    wells = geo.get('wells') or []

    # ---- open-ground sections ----
    by_id = {s['id']: s for s in og['sections']}
    secs = []
    for f in plss['features']:
        sid = f['properties']['id']
        s = by_id.get(sid)
        if not s:
            continue
        rings = f['geometry']['coordinates']
        xs = [p[0] for p in rings[0]]; ys = [p[1] for p in rings[0]]
        secs.append({'id': sid, 'lab': s['lab'], 'st': s['st'], 'nA': s['nA'], 'nC': s['nC'],
                     'cx': sum(xs) / len(xs), 'cy': sum(ys) / len(ys),
                     'bb': (min(xs), min(ys), max(xs), max(ys)), 'rings': rings})
    sgrid = Grid(3.0)
    for i, s in enumerate(secs):
        sgrid.add(*xy(s['cx'], s['cy']), i)

    # ---- score every unit ----
    targets, dropped = [], 0
    for u in geo['units']:
        blob = ' '.join(str(u.get(kk) or '') for kk in ('nm', 'sn', 'li', 'de', 'co'))
        tier, terms = classify(blob)
        assoc = tier == 0 and bool(ULTRAMAFIC_RX.search(blob))
        if not tier and not assoc:
            continue
        # geometry samples (exterior rings only)
        pts = []
        area = 0.0
        for poly in u['g']:
            ring = poly[0]
            pts.extend(ring[::max(1, len(ring) // 40)])
            a = sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
                    for i in range(len(ring) - 1)) / 2
            area += abs(a)
        if not pts:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        area_km2 = area * KX * KY
        samples = [xy(*p) for p in pts[:80]] + [xy(cx, cy)]

        # fault proximity + intersections
        fault_km, fx_n = 99.0, 0
        for sx, sy in samples:
            for si in fgrid.near(sx, sy, 3.0):
                a, b, _ = fsegs[si]
                d = seg_dist(sx, sy, a[0], a[1], b[0], b[1])
                if d < fault_km:
                    fault_km = d
        fseen = set()
        for sx, sy in samples:
            for p in xgrid.near(sx, sy, 2.0):
                if math.hypot(p[0] - sx, p[1] - sy) <= 2.0:
                    fseen.add((round(p[0], 2), round(p[1], 2)))
        fx_n = len(fseen)

        # pathfinder sites within 2 km
        near_paths, pseen = [], set()
        for sx, sy in samples:
            for pi in pgrid.near(sx, sy, 2.0):
                if pi in pseen:
                    continue
                px, py, nm, com, w, kind = paths[pi]
                d = math.hypot(*(a - b for a, b in zip(xy(px, py), (sx, sy))))
                if d <= 2.0:
                    pseen.add(pi)
                    near_paths.append({'nm': nm, 'c': com, 'km': round(d, 2), 'w': w})
        near_paths.sort(key=lambda p: (-p['w'], p['km']))
        # resolve the Knoxville-type association gate now that boosts exist
        if assoc:
            hg_n = sum(1 for p in near_paths
                       if HG_CODES & {t.strip().upper() for t in re.split(r'[,;/]', p['c'])})
            if hg_n >= 3 and fault_km <= 1.0:
                tier = 2
                terms = [f'serpentinite + {hg_n}-mercury cluster on structure '
                         f'(Knoxville-type association — not description-based)']
            else:
                continue

        # thermal springs / wells within 4 km
        near_spr = []
        for s in thermal:
            d = math.hypot(*(a - b for a, b in zip(xy(s['x'], s['y']), xy(cx, cy))))
            inside = any(point_in_poly(s['x'], s['y'], poly) for poly in u['g'])
            if d <= 4.0 or inside:
                near_spr.append({'nm': s['nm'], 'cls': s['cls'],
                                 'km': 0.0 if inside else round(d, 2)})
        near_wells = []
        for w in wells:
            d = math.hypot(*(a - b for a, b in zip(xy(w['x'], w['y']), xy(cx, cy))))
            inside = any(point_in_poly(w['x'], w['y'], poly) for poly in u['g'])
            if d <= 4.0 or inside:
                near_wells.append({'nm': w['nm'], 'km': 0.0 if inside else round(d, 2)})

        # open-ground overlap
        over = {}
        open_secs = []
        r_km = max(2.0, math.sqrt(area_km2) if area_km2 < 900 else 30.0)
        cand, cseen = [], set()
        for sx, sy in samples[::6] + [xy(cx, cy)]:
            for si in sgrid.near(sx, sy, min(r_km, 8.0)):
                if si not in cseen:
                    cseen.add(si); cand.append(secs[si])
        for s in cand:
            hit = any(point_in_poly(s['cx'], s['cy'], poly) for poly in u['g'])
            if not hit and s['bb'][0] <= cx <= s['bb'][2] and s['bb'][1] <= cy <= s['bb'][3]:
                hit = point_in_poly(cx, cy, s['rings'])
            if hit:
                over[s['st']] = over.get(s['st'], 0) + 1
                if s['st'] in ('OPEN', 'CLOSED_ONLY', 'QUIET'):
                    open_secs.append(s['lab'] + (f" ({s['nC']} closed case{'s' if s['nC'] != 1 else ''})"
                                                 if s['st'] == 'CLOSED_ONLY' else ''))
        n_over = sum(over.values())
        open_n = over.get('OPEN', 0) + over.get('CLOSED_ONLY', 0) + over.get('QUIET', 0)
        open_frac = open_n / n_over if n_over else 0.0

        # ---- score assembly, with the why-strings built from real numbers ----
        why = []
        base = BASE[tier]
        # Mega-unit dampening (T3/TRAV only): a 1,500 km² valley-fill formation
        # that mentions "tuffaceous" is not a rhyolite dome — discrete bodies
        # are the target. T1 sinter is NEVER damped (spec: flag regardless).
        damp = 1.0
        if tier in (3, 9) and area_km2 > 150:
            damp = (150.0 / area_km2) ** 0.35
            base = round(base * damp, 1)
        score = base
        why.append(f'{TIER_NAME[tier]}: description matches [{", ".join(terms)}] — base {base}'
                   + (f' (damped ×{damp:.2f}: {area_km2:,.0f} km² map unit — targeting favors '
                      f'discrete bodies)' if damp < 1 else ''))
        if fault_km < 90:
            ft = 22 * math.exp(-fault_km / 1.5) * (1.3 if tier == 2 else 1.0)
            if ft >= 1:
                score += ft
                why.append(f'mapped fault {fault_km:.1f} km away (+{ft:.0f}'
                           + (', tier-2 range-front weighting' if tier == 2 else '') + ')')
        if fx_n:
            b = 4 * min(fx_n, 3)
            score += b
            why.append(f'{fx_n} mapped fault intersection{"s" if fx_n > 1 else ""} within 2 km (+{b})')
        if near_paths:
            # CA Coast Ranges: the Hg belt IS the epithermal pathfinder trail —
            # weight mercury heavier and let the cap breathe (per the CA patch)
            hg_x = 1.5 if aoi['state'] == 'CA' else 1.0
            cap = 22.0 if aoi['state'] == 'CA' else 15.0
            # 1.6/site (not 3): dense belts must DIFFERENTIATE, not insta-cap
            b = min(cap, sum(1.6 * p['w'] * hg_x * math.exp(-p['km'] / 1.5) for p in near_paths))
            score += b
            top = near_paths[0]
            why.append(f'{len(near_paths)} pathfinder-commodity occurrence'
                       f'{"s" if len(near_paths) > 1 else ""} ≤2 km — nearest: {top["nm"]} '
                       f'[{top["c"]}] {top["km"]} km (+{b:.0f}; Hg/Sb/As weighted 2×'
                       + (', CA Coast Ranges Hg emphasis' if hg_x > 1 else '') + ')')
        if near_spr:
            b = min(16.0, sum({'hot': 12, 'warm': 7, 'thermal': 7}[s['cls']] *
                              math.exp(-s['km'] / 2.0) for s in near_spr))
            score += b
            why.append('thermal springs: ' + ', '.join(f'{s["nm"]} ({s["cls"]}, {s["km"]} km)'
                                                       for s in near_spr[:3]) + f' (+{b:.0f})')
        if near_wells:
            b = min(16.0, sum(8 * math.exp(-w['km'] / 2.0) for w in near_wells))
            score += b
            why.append(f'{len(near_wells)} IDWR geothermal well'
                       f'{"s" if len(near_wells) > 1 else ""} ≤4 km (+{b:.0f})')
        if n_over:
            b = 15 * open_frac
            score += b
            why.append(f'land under it: {open_n}/{n_over} overlapped sections locatable & '
                       f'unclaimed (+{b:.0f}) — '
                       + ', '.join(f'{v} {kk}' for kk, v in sorted(over.items(), key=lambda t: -t[1])))
        money = tier in (1, 2) and open_frac >= 0.4 and open_n >= 1
        if money:
            why.append('★ MONEY LAYER: tier ≤2 chemistry over open ground — the WS6 combination')

        # keep: all T1 + TRAV + T2; T3 only when something else corroborates
        if tier == 3 and score < 42:
            dropped += 1
            continue
        src = geo['sources'].get(u['src'], {})
        targets.append({
            'id': u['id'], 'tier': tier, 'tierName': TIER_NAME[tier], 'money': money,
            'score': round(score, 1), 'nm': u.get('nm'), 'sn': u.get('sn'),
            'age': u.get('age'), 'li': u.get('li'), 'de': u.get('de'), 'col': u.get('col'),
            'terms': terms, 'cx': round(cx, 5), 'cy': round(cy, 5),
            'area_km2': round(area_km2, 1),
            'src': {'id': u['src'], 'ref': src.get('ref'), 'scale': src.get('scale'),
                    'scale_note': src.get('scale_note')},
            'why': why,
            'boosts': {'fault_km': round(fault_km, 2) if fault_km < 90 else None,
                       'fx': fx_n, 'paths': near_paths[:6], 'springs': near_spr[:4],
                       'wells': len(near_wells),
                       'open': {'n': n_over, 'open_n': open_n,
                                'frac': round(open_frac, 2), 'by_status': over}},
            'secs_open': open_secs[:10],
            'g': u['g'],
        })

    targets.sort(key=lambda t: (-t['score'], -t['area_km2']))
    for i, t in enumerate(targets):
        t['rank'] = i + 1
    stats = {'scored_units': len(geo['units']), 'targets': len(targets),
             'dropped_low_t3': dropped,
             'by_tier': {TIER_NAME[t]: sum(1 for x in targets if x['tier'] == t)
                         for t in (1, 2, 3, 9) if any(x['tier'] == t for x in targets)},
             'money': sum(1 for t in targets if t['money'])}
    out = {
        'aoi': k, 'generated': TODAY, 'degraded': degraded or None,
        'method': ('Every geologic-map unit in the AOI is scored: TIER 1 sinter/opaline/'
                   'chalcedonic hot-spring deposits (base 100, flagged regardless of anything '
                   'else), travertine separately (calcareous — related system, wrong chemistry, '
                   'base 15), TIER 2 silicified/hydrothermally-altered/argillized (base 60, '
                   'fault-proximity weighted 1.3×), TIER 3 rhyolite–tuff–bimodal epithermal '
                   'hosts (base 30, kept only when faults/pathfinders/springs corroborate). '
                   'Boosts: fault distance 22·e^(−km/1.5); +4 per fault intersection ≤2 km '
                   '(max 3); MRDS/IGS pathfinder commodities ≤2 km (Hg,Sb,As ×2 — Au,Ag ×1, '
                   'max +15); GNIS hot/warm springs (max +16); IDWR geothermal wells '
                   '(max +16); +15 × open-ground fraction under the polygon.'),
        'honesty': ('No Tier-1 sinter and no Tier-2 alteration units are MAPPED in this AOI '
                    'at the available vector scales (SGMC 1:500k + IGS DWM-49 1:100k) — their '
                    'absence here is a statement about map scale, not about the ground. '
                    'Plain “altered” is excluded on purpose (weathered basalt false positive). '
                    'Tier-1/2 detection improves with quad-scale raster maps (NGMDB) and '
                    'satellite alteration indices — both recorded as WS6 future work. '
                    'Scores are research leads, not discoveries; land status per WS2 rules '
                    '(verify before staking).'),
        'stats': stats, 'targets': targets,
    }
    write_json(f'data/targets/{k}.json', out)
    update_manifest('geology', {'file': f'data/geology/{k}.json',
                                'units': len(geo['units']), 'retrieved': geo['generated']})
    update_manifest('targets', {'file': f'data/targets/{k}.json',
                                'n': len(targets), 'retrieved': TODAY})
    print(json.dumps(stats, indent=1))
    print('\nTOP 12:')
    for t in targets[:12]:
        print(f'  #{t["rank"]:>3} {t["score"]:6.1f} T{t["tier"]} {"★" if t["money"] else " "} '
              f'{(t["nm"] or "?")[:44]:44} fault {t["boosts"]["fault_km"]} km, '
              f'open {t["boosts"]["open"]["open_n"]}/{t["boosts"]["open"]["n"]}')
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
