#!/usr/bin/env python3
"""WS2b/c — per-section open-ground compute for the AOI.

Inputs (all produced by sibling fetchers + existing site data):
  site/data/plss/{aoi}.json              — section polygons
  site/data/openground/{aoi}_claims.json — active+closed cases w/ CSE_META sections
  pipelines/cache/landstatus_{aoi}.json  — SMA, withdrawals, segregations, WSA
  build-inputs/data/sites/{mrds,usmin,stategeo}_{st}.json — historic points

Section status ladder (first match wins):
  ACTIVE      — ≥1 active claim case touches the section (by legal description)
  WITHDRAWN   — withdrawal/WSA/park polygon covers the section centroid,
                or SMA = NPS/FWS/DOD (not open to location)
  NONFEDERAL  — SMA at centroid is private/state/unknown → no BLM claims
                possible ON SURFACE (patented ground trap — see note)
  OPEN        — historic mine/prospect features present AND no active claim
                AND federal locatable surface (BLM/USFS)
  CLOSED_ONLY — like OPEN but the section also has ≥1 closed case and no
                historic-feature requirement (recency-colored in UI)
  QUIET       — federal locatable surface, no claims either way, no features
Flags: split (mineral segregation present), seg_sur, edge (SMA generalized
boundary within ~300 m of centroid — assignment uncertain).

Every section carries the evidence counts so the popup can show its work.
"""
import json, os, sys, time
from collections import defaultdict
from common import (load_aoi, SITE, HERE, point_in_poly, write_json, TODAY,
                    load_build_input)

LOCATABLE = {'BLM', 'USFS'}          # surface agencies open to location (generally)
NEVER = {'NPS', 'FWS', 'DOD', 'USBR', 'BIA'}   # not open to location


def load(rel):
    return json.load(open(os.path.join(SITE, rel)))


def ring_bbox(rings):
    xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
    return min(xs), min(ys), max(xs), max(ys)


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    k = aoi['key']
    plss = load(f'data/plss/{k}.json')
    claims = load(f'data/openground/{k}_claims.json')
    ls = json.load(open(os.path.join(HERE, 'cache', f'landstatus_{k}.json')))

    # --- claims by section (from legal descriptions — exact, no spatial guess) ---
    act_by_sec, clo_by_sec = defaultdict(list), defaultdict(list)
    for c in claims['active']:
        for s in c['secs']: act_by_sec[s].append(c)
    for c in claims['closed']:
        for s in c['secs']: clo_by_sec[s].append(c)

    # --- historic features by section (point-in-polygon, gridded) ---
    st = aoi['state'].lower()
    feats = []
    for kind in ('mrds', 'usmin', 'stategeo'):
        try:
            d = load_build_input('sites', f'{kind}_{st}')
        except (FileNotFoundError, ValueError):
            continue
        xs, ys = d['x'], d['y']
        names = d.get('nm') if isinstance(d.get('nm'), list) else None
        for i in range(len(xs)):
            if xs[i] is None: continue
            feats.append((xs[i], ys[i], kind, names[i] if names else None))
    x0, y0, x1, y1 = aoi['bbox']
    feats = [f for f in feats if x0 <= f[0] <= x1 and y0 <= f[1] <= y1]
    print(f'historic features in bbox: {len(feats)}')

    grid = defaultdict(list)
    for f in feats:
        grid[(int(f[0] / 0.02), int(f[1] / 0.02))].append(f)

    # --- land-status polygons, pre-bboxed ---
    def prep(polys):
        out = []
        for p in polys:
            if not p['rings']: continue
            out.append((ring_bbox(p['rings']), p))
        return out
    sma_p = prep(ls['sma']); wdl_p = prep(ls['withdrawals'])
    segm_p = prep(ls['seg_min']); segs_p = prep(ls['seg_sur']); wsa_p = prep(ls['wsa'])

    def hit(px, py, prepped):
        for (bx0, by0, bx1, by1), p in prepped:
            if bx0 <= px <= bx1 and by0 <= py <= by1 and point_in_poly(px, py, p['rings']):
                yield p

    sections, stats = [], defaultdict(int)
    for f in plss['features']:
        pr = f['properties']; fid = pr['id']
        rings = f['geometry']['coordinates']
        bx0, by0, bx1, by1 = ring_bbox(rings)
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2

        # historic features inside the section
        hist = []
        for gx in range(int(bx0 / 0.02) - 1, int(bx1 / 0.02) + 2):
            for gy in range(int(by0 / 0.02) - 1, int(by1 / 0.02) + 2):
                for (px, py, kind, nm) in grid.get((gx, gy), ()):
                    if bx0 <= px <= bx1 and by0 <= py <= by1 and point_in_poly(px, py, rings):
                        hist.append((kind, nm))

        # surface agency at centroid (largest-specificity wins: last non-UND hit)
        agency = None
        for p in hit(cx, cy, sma_p):
            a = p['agency']
            if a and a != 'UND': agency = a
        wdl = [p['attrs'] for p in hit(cx, cy, wdl_p)]
        wsa = [p for p in hit(cx, cy, wsa_p)]
        seg_min = bool(next(hit(cx, cy, segm_p), None))
        seg_sur = bool(next(hit(cx, cy, segs_p), None))

        acts = act_by_sec.get(fid, []); clos = clo_by_sec.get(fid, [])

        if acts: status = 'ACTIVE'
        elif wdl or wsa or (agency in NEVER): status = 'WITHDRAWN'
        elif agency not in LOCATABLE: status = 'NONFEDERAL'
        elif clos: status = 'CLOSED_ONLY'
        elif hist: status = 'OPEN'
        else: status = 'QUIET'
        stats[status] += 1

        # newest closure year, if any dated cases (sparse — dates rarely served)
        yrs = [c['disp_dt'] for c in clos if c.get('disp_dt')]
        newest = max(yrs) if yrs else None

        sections.append({'id': fid, 'lab': pr['lab'],
                         'st': status, 'ag': agency,
                         'nA': len(acts), 'nC': len(clos), 'nH': len(hist),
                         'split': 1 if (seg_min and status in ('OPEN', 'CLOSED_ONLY', 'QUIET')) else 0,
                         'segS': 1 if seg_sur else 0,
                         'wdl': [w.get('CSE_NAME') or w.get('CSE_NR') for w in wdl][:3],
                         'closedNew': newest,
                         'hist': [f'{nm} [{kind}]' if nm else f'[{kind}]'
                                  for kind, nm in hist[:12]],
                         'actSer': [c['ser'] for c in acts[:10]],
                         'cloSer': [c['ser'] for c in clos[:10]]})

    out = {'aoi': k, 'name': aoi['name'], 'generated': TODAY,
           'note': ('RESEARCH LEAD ONLY. Section status derives from BLM GIS snapshots '
                    '(claims legal descriptions, generalized SMA, withdrawal/segregation cases). '
                    'It is NOT a title search: patented private parcels show no BLM claims, '
                    'SMA boundaries are generalized, adjudication lags reality. '
                    'Verify at BLM and the county recorder before staking.'),
           'stats': dict(stats), 'sections': sections}
    write_json(f'data/openground/{k}.json', out)
    print('status counts:', dict(stats))
    open_gold = [s for s in sections if s['st'] == 'OPEN' and s['nH'] > 0]
    print(f'OPEN sections with historic features: {len(open_gold)}')
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
