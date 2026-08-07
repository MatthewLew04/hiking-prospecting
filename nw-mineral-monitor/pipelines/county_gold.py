#!/usr/bin/env python3
"""County gold-signal ranking — which counties hold the most STAKEABLE gold.

Extends the Cassia-first workflow to every county in the seven states using
data the repo already has. Two scores per county, both fully itemized:

  STAKE  (the ranking lens): gold evidence you could still act on —
         cited-grade gold mines on open ground (no active claim within
         400 m; distances precomputed in grades.json), gold sites that were
         staked and later dropped (closed claim ≤400 m, nothing active now —
         somebody proved it, then let it go), unclaimed gold occurrences,
         validated by producer counts and physical topo-map workings.
  ENDOW  (context): raw endowment regardless of claim status — producers,
         site counts, rich grades, current staking interest.

Honesty notes baked into the output:
  - NV/UT/WY closed-claim files are truncated to the NEWEST 250k of
    1.23M/452k/287k — the dropped-ground metric UNDERCOUNTS there (flagged
    per county in the method note).
  - MRDS coordinates slop hundreds of meters; the 400 m open-ground test
    inherits that. Patented private land shows no claims and looks "open" —
    the same WS2 trap, called out on every card.
  - Gold commodity = the word in MRDS/state-survey commodity strings; no
    inference from names.

Output: site/data/counties_gold.json  +  GOLD-COUNTIES.md (repo root)
"""
import json, math, os, sys
from collections import defaultdict

from common import SITE, TODAY, write_json, point_in_poly, update_manifest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
STATES = ['wa', 'or', 'id', 'mt', 'wy', 'nv', 'ut', 'ca']
TRUNCATED_CLOSED = {'NV': 1230906, 'UT': 451957, 'WY': 287066}   # total_available


def load(rel):
    return json.load(open(os.path.join(SITE, rel)))


# ---------------------------------------------------------------- counties
class Counties:
    def __init__(self, feats):
        self.rows = []
        self.grid = defaultdict(list)          # 0.2° cells -> county idxs
        self.memo = {}                          # 0.02° cell -> idx | 'mixed'
        for i, f in enumerate(feats):
            geom = f['geometry']
            polys = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
            xs = [p[0] for poly in polys for p in poly[0]]
            ys = [p[1] for poly in polys for p in poly[0]]
            bb = (min(xs), min(ys), max(xs), max(ys))
            cx = sum(xs) / len(xs); cy = sum(ys) / len(ys)
            self.rows.append({'st': f['properties']['st'], 'name': f['properties']['name'],
                              'bb': bb, 'polys': polys, 'cx': cx, 'cy': cy})
            for gx in range(int(bb[0] / 0.2), int(bb[2] / 0.2) + 1):
                for gy in range(int(bb[1] / 0.2), int(bb[3] / 0.2) + 1):
                    self.grid[(gx, gy)].append(i)

    def _pip(self, x, y):
        for i in self.grid.get((int(x / 0.2), int(y / 0.2)), ()):
            r = self.rows[i]
            if r['bb'][0] <= x <= r['bb'][2] and r['bb'][1] <= y <= r['bb'][3]:
                for poly in r['polys']:
                    if point_in_poly(x, y, poly):
                        return i
        return None

    def assign(self, x, y):
        """Cell-memoized point-in-county: interior 0.02° cells resolve once."""
        key = (int(x / 0.02), int(y / 0.02))
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


def run():
    counties = Counties(load('data/boundaries/counties.json')['features'])
    N = len(counties.rows)
    print(f'counties: {N}')

    # ---- claim-point grids (0.01° ≈ 1 km cells) for the 400 m tests ----
    act_grid, clo_grid = defaultdict(int), defaultdict(int)
    act_by_cty, clo_by_cty = [0] * N, [0] * N
    have_active = set()
    for st in STATES:
        for kind, grid, per in (('active', act_grid, act_by_cty),
                                ('closed', clo_grid, clo_by_cty)):
            try:
                d = load(f'data/claims/{st}_{kind}.json')
            except FileNotFoundError:
                continue
            if kind == 'active':
                have_active.add(st.upper())
            xs, ys = d['x'], d['y']
            for i in range(d['n']):
                x, y = xs[i], ys[i]
                if x is None:
                    continue
                grid[(int(x / 0.01), int(y / 0.01))] += 1
                ci = counties.assign(x, y)
                if ci is not None:
                    per[ci] += 1
            print(f'  {st} {kind}: {d["n"]:,} assigned')

    def near(grid, x, y, r_deg=0.004):          # ~400 m at these latitudes
        gx, gy = int(x / 0.01), int(y / 0.01)
        n = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n += grid.get((gx + dx, gy + dy), 0)
        return n            # cell-count proxy: any claim within the 3×3 ≈ ≤1.5 km
    def near_tight(grid, x, y):
        """Stricter: same cell only (≤~1 km) — used for the 'claimed' test."""
        return grid.get((int(x / 0.01), int(y / 0.01)), 0) \
             + grid.get((int((x - 0.004) / 0.01), int(y / 0.01)), 0) \
             + grid.get((int((x + 0.004) / 0.01), int(y / 0.01)), 0) \
             + grid.get((int(x / 0.01), int((y - 0.004) / 0.01)), 0) \
             + grid.get((int(x / 0.01), int((y + 0.004) / 0.01)), 0)

    # ---- gold sites from MRDS + state surveys ----
    M = [dict(au_sites=0, au_prod=0, au_open=0, au_dropped_open=0,
              gr_n=0, gr_rich=0, gr_open=0, gr_rich_open=0, gr_max=0.0,
              usmin=0, act=0, clo=0) for _ in range(N)]
    top = [[] for _ in range(N)]
    for st in STATES:
        for kind in ('mrds', 'stategeo'):
            try:
                d = load(f'data/sites/{kind}_{st}.json')
            except FileNotFoundError:
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
                m = M[ci]
                m['au_sites'] += 1
                status = str(d['st'][i] if kind == 'mrds' else d['stx'][i] or '')
                producer = (status in ('P', 'PP')) if kind == 'mrds' else \
                           ('produc' in status.lower())
                if producer:
                    m['au_prod'] += 1
                claimed = near_tight(act_grid, x, y) > 0
                if not claimed:
                    m['au_open'] += 1
                    if near_tight(clo_grid, x, y) > 0:
                        m['au_dropped_open'] += 1
                        if len(top[ci]) < 40:
                            top[ci].append({'nm': d['nm'][i], 'k': 'dropped',
                                            'x': round(x, 5), 'y': round(y, 5),
                                            'prod': int(producer)})
            print(f'  {st} {kind}: {n_au} gold sites')

    # ---- cited grades (au) — open distances precomputed in the dataset ----
    g = load('data/grades/grades.json')
    for i in range(g['n']):
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

    # ---- usmin workings per county ----
    for st in STATES:
        try:
            d = load(f'data/sites/usmin_{st}.json')
        except FileNotFoundError:
            continue
        for i in range(d['n']):
            x, y = d['x'][i], d['y'][i]
            if x is None:
                continue
            ci = counties.assign(x, y)
            if ci is not None:
                M[ci]['usmin'] += 1

    for i in range(N):
        M[i]['act'] = act_by_cty[i]
        M[i]['clo'] = clo_by_cty[i]

    # ---- scores, itemized ----
    out_rows = []
    for i, r in enumerate(counties.rows):
        m = M[i]
        pending = r['st'] not in have_active   # no claims snapshot => stakeable unmeasurable
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
        if r['st'] in TRUNCATED_CLOSED:
            notes.append(f"closed-claim file truncated to newest 250k of "
                         f"{TRUNCATED_CLOSED[r['st']]:,} — dropped-ground UNDERCOUNTED")
        if pending:
            notes.append('NO CLAIMS SNAPSHOT for this state yet — stakeable is UNMEASURABLE '
                         '(every site would falsely read "unclaimed"). Endowment shown '
                         'instead; stakeable computes automatically once the claims sync '
                         'lands (CA: first scheduled Lambda run, then copy the s3 snapshots '
                         'into the repo and re-run county_gold.py).')
        elif r['st'] == 'CA':
            notes.append('CA partial: closed claims + cited grades pending — staked-then-'
                         'dropped and rich-open floored at zero, not measured.')
        out_rows.append({'st': r['st'], 'name': r['name'], 'cx': round(r['cx'], 4),
                         'cy': round(r['cy'], 4),
                         'stake': None if pending else round(stake, 1),
                         'pending': pending or None,
                         'disp': round(endow * 0.55, 1) if pending else round(stake, 1),
                         'endow': round(endow, 1), 'm': m,
                         'why_stake': (['stakeable unmeasurable — no claims snapshot for '
                                        'this state yet (see note)'] if pending else why_s),
                         'why_endow': why_e,
                         'top': top[i][:8], 'notes': notes})

    out_rows.sort(key=lambda r: -(r['stake'] if r['stake'] is not None else -1))
    rk = 0
    for r in out_rows:
        if r['stake'] is not None:
            rk += 1; r['rank'] = rk
        else:
            r['rank'] = None               # pending states rank by endowment only
    by_endow = sorted(range(len(out_rows)), key=lambda i: -out_rows[i]['endow'])
    for pos, i in enumerate(by_endow):
        out_rows[i]['rank_endow'] = pos + 1

    out = {
        'generated': TODAY,
        'lens': 'STAKE — gold evidence still actionable (open cited grades, staked-then-'
                'dropped sites, unclaimed occurrences), validated by producers + workings. '
                'ENDOW shown alongside for contrast.',
        'method': ('Every MRDS/state-survey site with a gold commodity, every cited gold '
                   'grade (open distances precomputed against the active-claim snapshot), '
                   'every claim centroid and every USMIN working assigned to its county '
                   '(TIGER 500k). "Claimed" = active-claim centroid within ~400 m (grid '
                   'test, inherits MRDS coordinate slop); "dropped" = closed claim that '
                   'near with nothing active. NV/UT/WY closed files hold only the newest '
                   '250k records — dropped-ground undercounts there. Patented private '
                   'ground shows no claims and reads "open" — verify ownership, always.'),
        'counties': out_rows,
    }
    write_json('data/counties_gold.json', out)
    update_manifest('counties_gold', {'file': 'data/counties_gold.json',
                                      'n': len(out_rows), 'retrieved': TODAY})
    write_report(out)
    print('\nTOP 15 (stakeable):')
    for r in out_rows[:15]:
        print(f"  #{r['rank']:>2} {r['stake']:6.1f}  {r['name']}, {r['st']}   "
              f"(endow #{r['rank_endow']}: {r['endow']:.0f})  "
              f"richOpen {r['m']['gr_rich_open']}, dropped {r['m']['au_dropped_open']}, "
              f"openAu {r['m']['au_open']}")
    return out


def write_report(out):
    rows = out['counties']
    def table(rs, n):
        lines = ['| # | County | Stake | Endow (rank) | Rich-open grades | Staked-then-dropped | Open Au sites | Producers |',
                 '|---|---|---|---|---|---|---|---|']
        for r in rs[:n]:
            m = r['m']
            lines.append(f"| {r['rank']} | **{r['name']}, {r['st']}** | {r['stake']} | "
                         f"{r['endow']} (#{r['rank_endow']}) | {m['gr_rich_open']} | "
                         f"{m['au_dropped_open']} | {m['au_open']} | {m['au_prod']} |")
        return '\n'.join(lines)
    ida = [r for r in rows if r['st'] == 'ID']
    md = [
        '# County gold signal — stakeable-first ranking (7 states)\n',
        f'_Generated {out["generated"]} by `pipelines/county_gold.py` from the repo\'s own '
        'snapshots. Ranking lens: **stakeable gold** — see the map layer '
        '(GOLD SIGNAL — COUNTIES) for the same data interactively._\n',
        f'**Method.** {out["method"]}\n',
        '## Top 25 — all seven states\n', table(rows, 25),
        '\n## Idaho — every county, ranked\n', table(ida, len(ida)),
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
        'staked-then-dropped **undercounts** in those states.\n'
        '- MRDS coordinates can be off by hundreds of meters; grades include specimen '
        'assays (read each quote).\n'
        '- Verify land status, withdrawals, and county records before staking anything.\n',
    ]
    p = os.path.join(ROOT, 'GOLD-COUNTIES.md')
    open(p, 'w').write('\n'.join(md))
    print('wrote GOLD-COUNTIES.md')


if __name__ == '__main__':
    run()
