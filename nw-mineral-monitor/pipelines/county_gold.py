#!/usr/bin/env python3
"""County gold-signal ranking — every county in the 49-state scope.

Two scores per county, both fully itemized (see GOLD-COUNTIES.md):

  STAKE  (the ranking lens): gold evidence you could still act on —
         cited-grade gold mines on open ground (no active claim within
         400 m; distances precomputed in grades.json), gold sites that were
         staked and later dropped (closed claim ≤400 m, nothing active now —
         somebody proved it, then let it go), unclaimed gold occurrences,
         validated by producer counts and physical topo-map workings.
         Only computable where a claims snapshot exists.
  ENDOW  (context, every county): raw endowment regardless of claim status —
         producers, site counts, rich grades, current staking interest.

Scope is the registry's 49 states (Hawaii excluded, like everything else in
the repo).  The county polygons are decoded from the published national
admin.pmtiles archive so the ranking joins on exactly the FIPS set the map
draws.  Sites come from the national MRDS bulk CSV (49 states), the national
USMIN archive (49 states), the six legacy state-survey snapshots, and ARDF
for Alaska.  Claims come from the eight legacy federal MLRS snapshots and,
for Alaska, the DNR state-law claim archive.

Honesty notes baked into the output:
  - Claim states with no claims snapshot (AZ AR CO FL LA MS NE NM ND SD)
    are PENDING: stakeable is unmeasurable there (every site would falsely
    read "unclaimed"), so the map shows an endowment preview instead.
  - The 30 non-claim states have no open-ground concept; endowment only.
  - Alaska's "claimed" test uses DNR state-law claims only; the federal
    MLRS snapshot for Alaska is not in the repo.
  - NV/UT/WY closed-claim files are truncated to the NEWEST 250k — the
    dropped-ground metric UNDERCOUNTS there; CA has no closed snapshot.
  - Cited grades exist for eight states; elsewhere the rich-open component
    is zero because it is UNMEASURED, not because it was measured as zero.
  - MRDS coordinates slop hundreds of meters; the 400 m open-ground test
    inherits that.  Patented private land shows no claims and looks "open"
    — the same WS2 trap, called out on every card.
  - Gold commodity = the word in MRDS/state-survey commodity strings (or
    "Au" among ARDF main commodities); no inference from names.

Output: site/data/counties_gold.json  +  GOLD-COUNTIES.md (repo root)
"""
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import defaultdict

from common import (SITE, TODAY, write_json, point_in_poly, update_manifest,
                    load_build_input, cached_get)
from state_registry import ALL_STATES, CLAIM_STATES, NON_CLAIM_STATES
from build_national_mrds_pmtiles import (iter_features as iter_mrds,
                                         SOURCE as MRDS_SOURCE)

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
ADMIN = os.path.join(SITE, 'data', 'tiles', 'context', 'admin.pmtiles')
USMIN = os.path.join(SITE, 'data', 'tiles', 'national', 'usmin.pmtiles')
ARDF = os.path.join(SITE, 'data', 'tiles', 'national', 'ardf.pmtiles')
AK_CLAIMS = os.path.join(SITE, 'data', 'tiles', 'claims', 'ak-state.pmtiles')
AK_CLAIMS_PRECISION = os.path.join(SITE, 'data', 'tiles', 'claims', 'ak-state-precision.pmtiles')
ADMIN_ZOOM = 10            # archive maxzoom: ~40 m/unit, exact FIPS inventory
USMIN_ZOOM = 13            # archive maxzoom: every source record present
ARDF_ZOOM = 13
AK_CLAIMS_ZOOM = 13        # archive maxzoom: the only zoom that keeps every polygon
AK_PRECISION_ZOOM = 19     # the z19 overflow archive holds 24 narrow source polygons
EXPECTED_COUNTIES = 3_138  # states/_meta.yaml scope; admin manifest counts
STATEGEO_STATES = ('ca', 'id', 'mt', 'or', 'wa', 'wy')
TRUNCATED_CLOSED = {'NV': 1230906, 'UT': 451957, 'WY': 287066}   # total_available
STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'ID': 'Idaho', 'IL': 'Illinois',
    'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas', 'KY': 'Kentucky',
    'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland', 'MA': 'Massachusetts',
    'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri',
    'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire',
    'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island',
    'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas',
    'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}


def load(rel):
    return json.load(open(os.path.join(SITE, rel)))


# ------------------------------------------------------------ tile decoding
_LAYER_RE = re.compile(r'"layer": "([^"]+)"')


def decode_features(path, layers, zoom):
    """Stream (layer, feature) pairs out of a published PMTiles archive.

    tippecanoe-decode prints one feature per line inside per-tile, per-layer
    FeatureCollection wrappers.  Reading it as a line stream keeps the
    200 MB USMIN decode out of memory.  Polygon features arrive as tile
    pieces (clipped, with tippecanoe's small buffer); callers group by the
    stable top-level id.
    """
    decoder = shutil.which('tippecanoe-decode')
    if not decoder:
        raise RuntimeError('tippecanoe-decode >=2.79 is required to read the '
                           'published PMTiles archives (brew install tippecanoe)')
    if not os.path.exists(path):
        raise RuntimeError(f'missing archive {path} — run git lfs pull')
    cmd = [decoder, '-Z', str(zoom), '-z', str(zoom)]
    for layer in layers:
        cmd += ['-l', layer]
    cmd.append(path)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1 << 20)
    layer = None
    try:
        for line in proc.stdout:
            s = line.lstrip()
            if s.startswith('{ "type": "FeatureCollection"'):
                m = _LAYER_RE.search(s)
                if m:
                    layer = m.group(1)
                continue
            if not s.startswith('{ "type": "Feature"'):
                continue
            yield layer, json.loads(s.rstrip().rstrip(','))
    finally:
        proc.stdout.close()
        err = proc.stderr.read()
        proc.stderr.close()
        if proc.wait() != 0:
            raise RuntimeError(f'tippecanoe-decode failed on {path}: {err.strip()}')


def polygons(geometry):
    t = geometry.get('type')
    if t == 'Polygon':
        return [geometry['coordinates']]
    if t == 'MultiPolygon':
        return geometry['coordinates']
    return []


def ring_area_centroid(ring):
    """Signed shoelace area and centroid of one ring (lon/lat degrees)."""
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return 0.0, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return abs(a) / 2, cx / (3 * a), cy / (3 * a)


# ---------------------------------------------------------------- counties
class Counties:
    """Point-in-county over the decoded admin tile pieces.

    Every county is a list of tile pieces; a point is inside the county if it
    is inside any piece.  Overlapping tile buffers belong to the same county,
    so the first hit is the answer.
    """

    def __init__(self, rows):
        self.rows = rows                       # per county: st, name, fips, cx, cy
        self.pieces = []                       # (county idx, bbox, rings)
        self.grid = defaultdict(list)          # 0.2° cells -> piece idxs
        self.memo = {}                         # 0.02° cell -> idx | 'mixed'
        for i, r in enumerate(rows):
            for poly in r.pop('pieces'):
                xs = [p[0] for p in poly[0]]
                ys = [p[1] for p in poly[0]]
                bb = (min(xs), min(ys), max(xs), max(ys))
                j = len(self.pieces)
                self.pieces.append((i, bb, poly))
                for gx in range(math.floor(bb[0] / 0.2), math.floor(bb[2] / 0.2) + 1):
                    for gy in range(math.floor(bb[1] / 0.2), math.floor(bb[3] / 0.2) + 1):
                        self.grid[(gx, gy)].append(j)

    def _pip(self, x, y):
        for j in self.grid.get((math.floor(x / 0.2), math.floor(y / 0.2)), ()):
            i, bb, poly = self.pieces[j]
            if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] and point_in_poly(x, y, poly):
                return i
        return None

    def assign(self, x, y):
        """Cell-memoized point-in-county: interior 0.02° cells resolve once."""
        key = (math.floor(x / 0.02), math.floor(y / 0.02))
        m = self.memo.get(key)
        if m == 'mixed':
            return self._pip(x, y)
        if m is not None:
            return m
        i = self._pip(x, y)
        x0, y0 = key[0] * 0.02, key[1] * 0.02
        corners = {self._pip(x0 + dx, y0 + dy)
                   for dx in (0.001, 0.019) for dy in (0.001, 0.019)}
        self.memo[key] = i if corners == {i} else 'mixed'
        return i


def national_counties():
    """Every county polygon in the published 49-state admin archive."""
    by_fips = {}
    for _, f in decode_features(ADMIN, ['counties'], ADMIN_ZOOM):
        p = f.get('properties') or {}
        fips, st, name = p.get('fips'), p.get('st'), p.get('name')
        if not (fips and st and name):
            raise RuntimeError(f'admin county piece without fips/st/name: {p}')
        polys = polygons(f.get('geometry') or {})
        if not polys:
            continue
        row = by_fips.get(fips)
        if row is None:
            row = by_fips[fips] = {'st': st, 'name': name, 'fips': fips,
                                   'pieces': [], 'parts': []}
        elif row['st'] != st or row['name'] != name:
            raise RuntimeError(f'admin FIPS {fips} carries two identities')
        for poly in polys:
            row['pieces'].append(poly)
            row['parts'].append(ring_area_centroid(poly[0]))
    rows = []
    for fips in sorted(by_fips):
        row = by_fips[fips]
        parts = row.pop('parts')
        # Aleutians West straddles the antimeridian: average on one side.
        wrap = any(c[1] > 100 for c in parts) and any(c[1] < -100 for c in parts)
        a_sum = sum(a for a, _, _ in parts)
        if a_sum > 0:
            cx = sum(a * ((x - 360) if (wrap and x > 0) else x) for a, x, _ in parts) / a_sum
            cy = sum(a * y for a, _, y in parts) / a_sum
        else:
            cx = sum(x for _, x, _ in parts) / len(parts)
            cy = sum(y for _, _, y in parts) / len(parts)
        if cx < -180:
            cx += 360
        row['cx'], row['cy'] = round(cx, 4), round(cy, 4)
        rows.append(row)
    states = {r['st'] for r in rows}
    if states != ALL_STATES or len(rows) != EXPECTED_COUNTIES:
        raise RuntimeError(
            f'admin archive scope changed: {len(states)} states / {len(rows)} counties; '
            f'expected {len(ALL_STATES)} / {EXPECTED_COUNTIES} '
            f'(missing {sorted(ALL_STATES - states)}, extra {sorted(states - ALL_STATES)})')
    return Counties(rows)


# ------------------------------------------------------------------ inputs
def legacy_claims(st, kind):
    """One legacy federal MLRS centroid snapshot, or None if undeclared."""
    try:
        return load_build_input('claims', f'{st.lower()}_{kind}')
    except (FileNotFoundError, ValueError):
        return None


def alaska_state_claims():
    """DNR state-law claim centroids from the published archive.

    Returns {'active': [(x, y)...], 'closed': [...]} where active includes the
    pending layer (a pending location is somebody's live interest, not open
    ground).  Every polygon is reassembled from its tile pieces by stable id
    and reduced to the center of its bounding box — a claim is ≤160 acres, so
    that lands well inside the 0.01° grid cell used by the claimed test.
    Only the archive's maximum zoom keeps every polygon (tiny-polygon
    reduction drops a few dozen narrow ones below it), and the 24 polygons
    that only exist in the z19 precision-overflow archive are read from there;
    the union is checked against the manifest's source inventory.
    """
    boxes = {'active': {}, 'pending': {}, 'closed': {}}
    decodes = [decode_features(AK_CLAIMS, ['active', 'pending', 'closed'], AK_CLAIMS_ZOOM)]
    if os.path.exists(AK_CLAIMS_PRECISION):
        decodes.append(decode_features(
            AK_CLAIMS_PRECISION, ['active_precision', 'closed_precision'], AK_PRECISION_ZOOM))
    for decode in decodes:
      for layer, f in decode:
        layer = (layer or '').replace('_precision', '')
        fid = f.get('id')
        if layer not in boxes or fid is None:
            continue
        for poly in polygons(f.get('geometry') or {}):
            xs = [p[0] for p in poly[0]]
            ys = [p[1] for p in poly[0]]
            bb = boxes[layer].get(fid)
            if bb is None:
                boxes[layer][fid] = [min(xs), min(ys), max(xs), max(ys)]
            else:
                bb[0] = min(bb[0], min(xs)); bb[1] = min(bb[1], min(ys))
                bb[2] = max(bb[2], max(xs)); bb[3] = max(bb[3], max(ys))
    man = load('data/manifest.json')
    inv = ((man.get('national_baselines') or {}).get('alaska_state_claims') or {}) \
        .get('source_id_inventory') or {}
    for layer, ids in boxes.items():
        expected = (inv.get(layer) or {}).get('source_records')
        if expected is not None and len(ids) != expected:
            raise RuntimeError(
                f'Alaska {layer} claims: decoded {len(ids):,} polygons at z{AK_CLAIMS_ZOOM}, '
                f'manifest inventory says {expected:,} — raise AK_CLAIMS_ZOOM')
    out = {'active': [], 'closed': []}
    for layer, ids in boxes.items():
        target = out['closed' if layer == 'closed' else 'active']
        for bb in ids.values():
            target.append(((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2))
    return out


def ardf_gold_sites():
    """ARDF occurrences carrying Au among their main commodities."""
    seen = set()
    for _, f in decode_features(ARDF, ['ardf'], ARDF_ZOOM):
        fid = f.get('id')
        if fid in seen:
            continue
        seen.add(fid)
        p = f.get('properties') or {}
        mains = {c.strip().rstrip('?') for c in str(p.get('g') or '').split(',')}
        if 'Au' not in mains:
            continue
        x, y = f['geometry']['coordinates'][:2]
        producer = str(p.get('production') or '').lower().startswith('yes')
        yield x, y, p.get('nm') or p.get('id') or '(unnamed)', producer


def usmin_workings():
    """Every unique USMIN point that is a mining feature, not a borrow pit."""
    seen = set()
    for _, f in decode_features(USMIN, ['usmin'], USMIN_ZOOM):
        fid = f.get('id')
        if fid in seen:
            continue
        seen.add(fid)
        p = f.get('properties') or {}
        if p.get('agg'):
            continue
        x, y = f['geometry']['coordinates'][:2]
        yield x, y


def mrds_rows():
    archive = cached_get(MRDS_SOURCE, ttl_days=365, binary=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        with bundle.open('mrds.csv') as csv_file:
            for st, f in iter_mrds(csv_file):
                p = f['properties']
                x, y = f['geometry']['coordinates']
                yield st, x, y, p


# --------------------------------------------------------------------- run
def run():
    counties = national_counties()
    N = len(counties.rows)
    print(f'counties: {N} across {len({r["st"] for r in counties.rows})} states')

    # ---- claim-point grids (0.01° ≈ 1 km cells) for the 400 m tests ----
    act_grid, clo_grid = defaultdict(int), defaultdict(int)
    act_by_cty, clo_by_cty = [0] * N, [0] * N
    have_active, have_closed = set(), set()
    claims_source = {}

    def ingest_points(pts, grid, per, label):
        n = 0
        for x, y in pts:
            if x is None:
                continue
            grid[(math.floor(x / 0.01), math.floor(y / 0.01))] += 1
            ci = counties.assign(x, y)
            if ci is not None:
                per[ci] += 1
            n += 1
        print(f'  {label}: {n:,} assigned')

    for st in sorted(CLAIM_STATES):
        if st == 'AK':
            continue
        for kind, grid, per, have in (('active', act_grid, act_by_cty, have_active),
                                      ('closed', clo_grid, clo_by_cty, have_closed)):
            d = legacy_claims(st, kind)
            if d is None:
                continue
            have.add(st)
            claims_source[st] = 'federal_mlrs_snapshot'
            ingest_points(zip(d['x'], d['y']), grid, per, f'{st} {kind} (federal MLRS snapshot)')
    ak = alaska_state_claims()
    have_active.add('AK'); have_closed.add('AK')
    claims_source['AK'] = 'alaska_dnr_state_claims'
    ingest_points(ak['active'], act_grid, act_by_cty, 'AK active+pending (DNR state claims)')
    ingest_points(ak['closed'], clo_grid, clo_by_cty, 'AK closed (DNR state claims)')

    def near_tight(grid, x, y):
        """Same cell plus the four ±400 m neighbours (≤~1 km) — the 'claimed' test."""
        return grid.get((math.floor(x / 0.01), math.floor(y / 0.01)), 0) \
             + grid.get((math.floor((x - 0.004) / 0.01), math.floor(y / 0.01)), 0) \
             + grid.get((math.floor((x + 0.004) / 0.01), math.floor(y / 0.01)), 0) \
             + grid.get((math.floor(x / 0.01), math.floor((y - 0.004) / 0.01)), 0) \
             + grid.get((math.floor(x / 0.01), math.floor((y + 0.004) / 0.01)), 0)

    # ---- gold sites from MRDS (49 states) + state surveys + ARDF ----
    M = [dict(au_sites=0, au_prod=0, au_open=0, au_dropped_open=0,
              gr_n=0, gr_rich=0, gr_open=0, gr_rich_open=0, gr_max=0.0,
              usmin=0, act=0, clo=0) for _ in range(N)]
    top = [[] for _ in range(N)]
    sites_by_state = defaultdict(int)

    def gold_site(ci, x, y, name, producer):
        m = M[ci]
        m['au_sites'] += 1
        if producer:
            m['au_prod'] += 1
        claimed = near_tight(act_grid, x, y) > 0
        if not claimed:
            m['au_open'] += 1
            if near_tight(clo_grid, x, y) > 0:
                m['au_dropped_open'] += 1
                if len(top[ci]) < 40:
                    top[ci].append({'nm': name, 'k': 'dropped',
                                    'x': round(x, 5), 'y': round(y, 5),
                                    'prod': int(producer)})

    n_mrds = 0
    for st, x, y, p in mrds_rows():
        if 'gold' not in (p.get('commodities') or '').lower():
            continue
        ci = counties.assign(x, y)
        if ci is None:
            continue
        n_mrds += 1
        sites_by_state[st] += 1
        gold_site(ci, x, y, p.get('nm'), p.get('status') in ('P', 'PP'))
    print(f'  MRDS (national CSV): {n_mrds:,} gold sites in-county')

    for st in STATEGEO_STATES:
        try:
            d = load_build_input('sites', f'stategeo_{st}')
        except (FileNotFoundError, ValueError):
            continue
        n_au = 0
        for i in range(d['n']):
            x, y = d['x'][i], d['y'][i]
            if x is None or 'gold' not in str(d['c'][i] or '').lower():
                continue
            ci = counties.assign(x, y)
            if ci is None:
                continue
            n_au += 1
            sites_by_state[st.upper()] += 1
            gold_site(ci, x, y, d['nm'][i], 'produc' in str(d['stx'][i] or '').lower())
        print(f'  {st} stategeo: {n_au} gold sites')

    n_ardf = 0
    for x, y, name, producer in ardf_gold_sites():
        ci = counties.assign(x, y)
        if ci is None:
            continue
        n_ardf += 1
        sites_by_state['AK'] += 1
        gold_site(ci, x, y, name, producer)
    print(f'  AK ARDF: {n_ardf} gold occurrences')

    # ---- cited grades (au) — open distances precomputed in the dataset ----
    g = load('data/grades/grades.json')
    grade_states = set()
    for i in range(g['n']):
        if g['st'][i]:
            grade_states.add(g['st'][i])
        if g['au'][i] is None or g['x'][i] is None:
            continue
        ci = counties.assign(g['x'][i], g['y'][i])
        if ci is None:
            continue
        m = M[ci]
        au = g['au'][i]
        openg = (g['open'][i] or 0) >= 400
        rich = au >= 0.3
        m['gr_n'] += 1
        m['gr_max'] = max(m['gr_max'], au)
        if rich: m['gr_rich'] += 1
        if openg: m['gr_open'] += 1
        if rich and openg:
            m['gr_rich_open'] += 1
            top[ci].insert(0, {'nm': g['name'][i], 'k': 'graded', 'au': au,
                               'open_m': g['open'][i], 'dist': g['dist'][i],
                               'x': round(g['x'][i], 5), 'y': round(g['y'][i], 5),
                               'src': (g['src'][i] or '')[:90]})
    print(f'  grades: {g["n"]:,} cited rows across {sorted(grade_states)}')

    # ---- usmin workings per county (national archive) ----
    n_usmin = 0
    for x, y in usmin_workings():
        ci = counties.assign(x, y)
        if ci is not None:
            M[ci]['usmin'] += 1
            n_usmin += 1
    print(f'  USMIN (national archive, aggregate pits excluded): {n_usmin:,} workings')

    for i in range(N):
        M[i]['act'] = act_by_cty[i]
        M[i]['clo'] = clo_by_cty[i]

    # ---- scores, itemized ----
    out_rows = []
    for i, r in enumerate(counties.rows):
        m = M[i]
        st = r['st']
        claim_applicable = st in CLAIM_STATES
        pending = claim_applicable and st not in have_active
        why_s, why_e = [], []
        def add(lst, pts, txt):
            if pts >= 0.5:
                lst.append(f'{txt} (+{pts:.0f})')
            return pts
        stake = 0.0
        # rich-open grades dominate by design: 6/mine to 8 mines, +2 beyond
        ro = m['gr_rich_open']
        stake += add(why_s, 6.0 * min(ro, 8) + 2.0 * max(0, ro - 8),
                     f"{ro} cited-grade gold mines ≥0.3 oz/t on OPEN ground")
        stake += add(why_s, min(9, 1.5 * max(0, m['gr_open'] - ro)),
                     f"{m['gr_open'] - ro} more graded gold mines on open ground")
        stake += add(why_s, min(30, 1.2 * math.sqrt(m['au_dropped_open'])),
                     f"{m['au_dropped_open']} gold sites STAKED-THEN-DROPPED (closed claim ≤~1 km, nothing active)")
        stake += add(why_s, min(20, 0.6 * math.sqrt(m['au_open'])),
                     f"{m['au_open']} gold sites with no active claim nearby")
        stake += add(why_s, min(14, 1.0 * math.sqrt(m['au_prod'])),
                     f"{m['au_prod']} gold producers/past producers (endowment validation)")
        stake += add(why_s, min(5, m['usmin'] / 400),
                     f"{m['usmin']:,} topo-map workings (things were actually dug)")
        endow = 0.0
        endow += add(why_e, min(30, 3.0 * math.sqrt(m['au_prod'])),
                     f"{m['au_prod']} gold producers/past producers")
        endow += add(why_e, min(25, 1.6 * math.sqrt(m['au_sites'])),
                     f"{m['au_sites']} gold-commodity sites")
        endow += add(why_e, min(20, 6.0 * math.sqrt(m['gr_rich'])),
                     f"{m['gr_rich']} cited grades ≥0.3 oz/t (max {m['gr_max']:.2g} oz/t)")
        endow += add(why_e, min(15, 3.0 * min(5, m['gr_max'])),
                     f'richest cited grade {m["gr_max"]:.2g} oz/t Au')
        endow += add(why_e, min(10, m['act'] / 800),
                     f"{m['act']:,} active claims (current market interest)")
        notes = []
        if st in TRUNCATED_CLOSED:
            notes.append(f"closed-claim file truncated to newest 250k of "
                         f"{TRUNCATED_CLOSED[st]:,} — dropped-ground UNDERCOUNTED")
        if pending:
            notes.append('NO CLAIMS SNAPSHOT for this state yet — stakeable is UNMEASURABLE '
                         '(every site would falsely read "unclaimed"). Endowment shown '
                         'instead; stakeable computes automatically once a federal MLRS '
                         'snapshot for the state lands in build-inputs and county_gold.py '
                         'is re-run.')
        elif claim_applicable and st not in have_closed:
            notes.append('no closed-claim snapshot for this state — staked-then-dropped '
                         'floored at zero; patented/park ground not screened out of "open".')
        if st == 'AK':
            notes.append('Alaska "claimed" test uses DNR state-law claims only (active + '
                         'pending vs closed polygons). The federal MLRS snapshot for Alaska '
                         'is not in the repo, so federal claims are not screened. ARDF '
                         'occurrences are counted alongside MRDS (corroboration, not '
                         'duplication).')
        if claim_applicable and not pending and st not in grade_states:
            notes.append('no cited-grade corpus for this state yet — the rich-open grade '
                         'component is UNMEASURED (zero here), not measured as zero.')
        if not claim_applicable:
            notes.append('OPEN GROUND N/A — this is a non-claim state. Use the land-context '
                         'route (state mineral lease or private negotiation), not staking.')
        out_rows.append({'st': st, 'name': r['name'], 'fips': r['fips'],
                         'cx': r['cx'], 'cy': r['cy'],
                         'regime': 'claim' if claim_applicable else 'non_claim',
                         'claims_source': claims_source.get(st),
                         'stake': None if (pending or not claim_applicable) else round(stake, 1),
                         'open_ground_status': ('not_applicable' if not claim_applicable else
                                                'unknown' if pending else 'measured'),
                         'pending': pending or None,
                         'disp': (round(endow * 0.55, 1)
                                  if (pending or not claim_applicable) else round(stake, 1)),
                         'endow': round(endow, 1), 'm': m,
                         'why_stake': (['stakeability not applicable — non-claim state; '
                                        'see land context'] if not claim_applicable else
                                       ['stakeable unmeasurable — no claims snapshot for '
                                        'this state yet (see note)'] if pending else why_s),
                         'why_endow': why_e,
                         'top': top[i][:8], 'notes': notes})

    out_rows.sort(key=lambda r: (-(r['stake'] if r['stake'] is not None else -1),
                                 -r['endow'], r['st'], r['name']))
    rk = 0
    for r in out_rows:
        if r['stake'] is not None:
            rk += 1; r['rank'] = rk
        else:
            r['rank'] = None               # pending / non-claim rank by endowment only
    by_endow = sorted(range(len(out_rows)),
                      key=lambda i: (-out_rows[i]['endow'], out_rows[i]['st'], out_rows[i]['name']))
    for pos, i in enumerate(by_endow):
        out_rows[i]['rank_endow'] = pos + 1

    measured = sorted(have_active)
    pending_states = sorted(CLAIM_STATES - have_active)
    out = {
        'generated': TODAY,
        'lens': 'STAKE — gold evidence still actionable (open cited grades, staked-then-'
                'dropped sites, unclaimed occurrences), validated by producers + workings. '
                'ENDOW shown alongside for contrast and is the only score in states '
                'without a claims snapshot or without a claim system.',
        'method': ('Every MRDS site with a gold commodity (national bulk CSV, 49 states), '
                   'every state-survey site with a gold commodity (CA ID MT OR WA WY) and '
                   'every ARDF occurrence with Au among its main commodities (AK), every '
                   'cited gold grade (open distances precomputed against the active-claim '
                   'snapshot; eight states), every claim centroid and every USMIN mining '
                   'working (national archive, aggregate pits excluded) assigned to its '
                   'county by point-in-polygon against the published Census TIGERweb '
                   'county archive (January 1 2025 vintage, 3,138 counties). "Claimed" = '
                   'active-claim centroid within ~400 m (grid test, inherits MRDS coordinate '
                   'slop); "dropped" = closed claim that near with nothing active. Claims '
                   'snapshots: federal MLRS for CA ID MT NV OR UT WA WY; Alaska DNR state '
                   'claims for AK. NV/UT/WY closed files hold only the newest 250k records '
                   '— dropped-ground undercounts there; CA has no closed snapshot. Patented '
                   'private ground shows no claims and reads "open" — verify ownership, '
                   'always.'),
        'scope': {
            'states': len(ALL_STATES), 'counties': len(out_rows),
            'hawaii_excluded': True,
            'stake_measured_states': measured,
            'pending_claim_states': pending_states,
            'non_claim_states': sorted(NON_CLAIM_STATES),
            'grade_corpus_states': sorted(grade_states),
            'claims_source': claims_source,
            'gold_sites_by_state': dict(sorted(sites_by_state.items())),
            'sources': {
                'counties': 'site/data/tiles/context/admin.pmtiles (Census TIGERweb, 2025-01-01)',
                'mrds': MRDS_SOURCE,
                'usmin': 'site/data/tiles/national/usmin.pmtiles (USGS USMIN)',
                'ardf': 'site/data/tiles/national/ardf.pmtiles (USGS ARDF)',
                'alaska_claims': 'site/data/tiles/claims/ak-state.pmtiles (Alaska DNR)',
                'federal_claims': 'build-inputs claims snapshots (BLM MLRS)',
                'grades': 'site/data/grades/grades.json',
            },
        },
        'counties': out_rows,
    }
    write_json('data/counties_gold.json', out)
    update_manifest('counties_gold', {
        'file': 'data/counties_gold.json', 'n': len(out_rows), 'retrieved': TODAY,
        'states': len(ALL_STATES), 'stake_measured_states': measured,
        'pending_claim_states': pending_states})
    write_report(out)
    print('\nTOP 15 (stakeable):')
    for r in [x for x in out_rows if x['rank']][:15]:
        print(f"  #{r['rank']:>2} {r['stake']:6.1f}  {r['name']}, {r['st']}   "
              f"(endow #{r['rank_endow']}: {r['endow']:.0f})  "
              f"richOpen {r['m']['gr_rich_open']}, dropped {r['m']['au_dropped_open']}, "
              f"openAu {r['m']['au_open']}")
    return out


def write_report(out):
    rows = out['counties']
    scope = out['scope']
    ranked = [r for r in rows if r['rank']]
    def table(rs, n, endow_lens=False):
        head = ('| # | County | Endow | Stake (rank) | Producers | Gold sites | Rich grades | Active claims |'
                if endow_lens else
                '| # | County | Stake | Endow (rank) | Rich-open grades | Staked-then-dropped | Open Au sites | Producers |')
        lines = [head, '|---|---|---|---|---|---|---|---|']
        for r in rs[:n]:
            m = r['m']
            if endow_lens:
                lines.append(f"| {r['rank_endow']} | **{r['name']}, {r['st']}** | {r['endow']} | "
                             f"{r['stake'] if r['stake'] is not None else '—'}"
                             f"{' (#'+str(r['rank'])+')' if r['rank'] else ''} | {m['au_prod']} | "
                             f"{m['au_sites']} | {m['gr_rich']} | {m['act']:,} |")
            else:
                lines.append(f"| {r['rank']} | **{r['name']}, {r['st']}** | {r['stake']} | "
                             f"{r['endow']} (#{r['rank_endow']}) | {m['gr_rich_open']} | "
                             f"{m['au_dropped_open']} | {m['au_open']} | {m['au_prod']} |")
        return '\n'.join(lines)
    by_endow = sorted(rows, key=lambda r: r['rank_endow'])
    ak = [r for r in ranked if r['st'] == 'AK']
    ida = [r for r in ranked if r['st'] == 'ID']
    names = lambda codes: ', '.join(f'{STATE_NAMES.get(c, c)} ({c})' for c in codes)
    md = [
        '# County gold signal — stakeable-first ranking (49 states, 3,138 counties)\n',
        f'_Generated {out["generated"]} by `pipelines/county_gold.py` from the repo\'s own '
        'archives and snapshots. Ranking lens: **stakeable gold** — see the map layer '
        '(GOLD BY COUNTY) for the same data interactively, with an endowment lens for '
        'the states where stakeable cannot yet be measured._\n',
        f'**Method.** {out["method"]}\n',
        '## Coverage — what each state\'s score means\n',
        f'- **Stakeable measured ({len(scope["stake_measured_states"])} states):** '
        f'{names(scope["stake_measured_states"])}. A claims snapshot exists, so "open", '
        '"claimed" and "staked-then-dropped" are real tests. Alaska is measured against '
        'DNR state-law claims only (no federal MLRS snapshot for Alaska in the repo).\n'
        f'- **Claims pending ({len(scope["pending_claim_states"])} claim states):** '
        f'{names(scope["pending_claim_states"])}. Federal mining claims apply, but no '
        'snapshot is in the repo yet, so stakeable is unmeasurable and only endowment '
        'is scored (shown muted on the map).\n'
        f'- **Non-claim states ({len(scope["non_claim_states"])}):** '
        f'{names(scope["non_claim_states"])}. No federal locatable-mineral system; '
        'endowment only, and the route is a state lease or private negotiation.\n'
        f'- **Cited-grade corpus:** {names(scope["grade_corpus_states"])}. Everywhere else '
        'the rich-open-grade component is unmeasured, not zero.\n',
        '## Top 25 — stakeable, all measured states\n', table(ranked, 25),
        '\n## Alaska — every borough and census area, ranked\n',
        'Scored against Alaska DNR state-law claims (active + pending vs closed) with '
        'MRDS and ARDF gold occurrences. No cited-grade corpus for Alaska yet.\n',
        table(ak, len(ak)),
        '\n## Idaho — every county, ranked\n', table(ida, len(ida)),
        '\n## Top 25 — endowment, all 49 states\n',
        'Endowment ignores claim status, so it is comparable across every county in scope.\n',
        table(by_endow, 25, endow_lens=True),
        '\n## Reading the components\n',
        '- **Rich-open grades** — mines with a *cited* assay/production grade ≥0.3 oz/t Au '
        'and no active claim within 400 m today. The strongest lead class in the dataset: '
        'documented gold, nobody holding it. Each one is quote-backed in the map dossier.\n'
        '- **Staked-then-dropped** — a gold-commodity site with closed claims near it and '
        'nothing active: someone believed enough to stake and file, then let it lapse. '
        'Classic re-examination targets (fee-hike years shed good ground).\n'
        '- **Open Au sites** — gold occurrences/prospects with no nearby active claim; '
        'weakest class alone (MRDS slop, patented-land trap) but volume matters.\n'
        '- **Producers** validate that the county\'s system actually made ore.\n',
        '## Caveats (same rules as the map)\n',
        '- Active-claim proximity is a research screen, **not a title search** — patented '
        'private land shows no BLM claims and reads "open" here.\n'
        '- NV/UT/WY closed-claim snapshots are truncated (newest 250k) → '
        'staked-then-dropped **undercounts** in those states; CA has no closed snapshot.\n'
        '- Alaska federal claims are not screened (DNR state claims only).\n'
        '- MRDS coordinates can be off by hundreds of meters; grades include specimen '
        'assays (read each quote).\n'
        '- Verify land status, withdrawals, and county records before staking anything.\n',
    ]
    p = os.path.join(ROOT, 'GOLD-COUNTIES.md')
    open(p, 'w').write('\n'.join(md))
    print('wrote GOLD-COUNTIES.md')


if __name__ == '__main__':
    run()
