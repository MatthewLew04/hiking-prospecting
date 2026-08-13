# BLM MLRS Mining Claims — NV / UT (ACTIVE layer)

Retrieved: **2026-07-31** (run 14:28–16:48 UTC) from the same BLM MLRS geocoded
mining-claims service documented in `summary_claims_nw.md`. Outputs:
`build-inputs/data/claims/{nv,ut}_active.json` — private columnar JSON,
centroid points only, deduped by `CSE_NR`, **identical schema/key order** to the
existing WA/OR/ID/MT/WY files (`state, layer, retrieved, n, serial, name, type,
disp, acres, x, y`).

Method followed `summary_claims_nw.md` exactly: envelope query (NOT state
polygon), OBJECTID-cursor pagination (`where=OBJECTID>{cursor}`,
`orderByFields=OBJECTID`, `resultRecordCount=2000`, `f=json`, NOT resultOffset),
`geometryPrecision=5`, User-Agent header, terminate only on empty page without
`exceededTransferLimit`. Client-side state assignment: claim-polygon centroid
inside the detailed TIGERweb state boundary buffered 0.005° (~500 m). NV/UT
boundaries cached at `/home/claude/nw/raw/state_exact_nvut.json` (TIGERweb
State_County/MapServer/0, `STUSAB IN ('NV','UT')`, f=geojson, outSR=4326).

Envelopes used (per task spec):

| ST | xmin | ymin | xmax | ymax | note |
|----|------|------|------|------|------|
| NV | -120.01 | 35.00 | -114.03 | 42.01 | contains a slice of eastern CA (Death Valley area) |
| UT | -114.06 | 36.99 | -109.06 | 42.01 | overlaps NV envelope in the -114.06/-114.03 sliver |

## Results (active layer)

| ST | envelope recs | kept in state | outside discarded | CSE_NR dupes | written n | pages | minutes | file MB |
|----|--------------:|--------------:|------------------:|-------------:|----------:|------:|--------:|--------:|
| NV | 291,587 seen (count 291,570 at start) | 275,261 | 16,326 | 0 | **275,261** | 146 | 139.3 | 16.44 |
| UT | 43,263 (== count) | 42,677 | 586 | 0 | **42,677** | 22 | 2.5 | 2.59 |

0 null geometries, 0 geometry failures, 0 null serials, 0 unknown type codes,
0 duplicate serials in either file. Null `name`: NV 1 / UT 2; null `acres`:
NV 13 / UT 3. Zero CSE_NR dupes matches the NW run — all five NW active pulls
also deduped 0 (active layer ≈ one record per claim; multi-parcel duplicates
are a closed-layer phenomenon).

### Disposition (CSE_DISP, uppercased)

| ST | ACTIVE | FILED | UNDER REVIEW | SUBMITTED | ON APPEAL |
|----|-------:|------:|-------------:|----------:|----------:|
| NV | 210,970 | 60,045 | 3,357 | 889 | 0 |
| UT | 18,318 | 24,227 | 130 | 0 | 2 |

**UT is the first state pulled where FILED (56.8%) exceeds ACTIVE** — a recent
staking wave; passed through as-is.

### Claim type (from CSE_TYPE_NR, same 3841xx→L / 3842xx→P / 3843xx→T / 3844xx→M decode)

| ST | L | P | M | T |
|----|--:|--:|--:|--:|
| NV | 246,336 | 22,543 | 6,378 | 4 |
| UT | 29,061 | 13,161 | 443 | 12 |

### Top claim-name first-word prefixes

* NV: MS 3078, RV 2137, TCS 1826, LU 1734, HS 1732, CC 1668, BV 1550, BC 1546
* UT: GR 1281, APC 965, GS 915, SP 885, WHITE 867, PV 678, LVL 641, MV 616

## Closed-layer scoping (returnCountOnly only — NO geometry pulled)

| ST | closed envelope count | vs 250k cap | rough pull estimate at observed NV throughput |
|----|----------------------:|-------------|----------------------------------------------|
| NV | **1,230,881** | 4.9x over | ~615 pages; 10–20 h single job — MUST use the OBJECTID DESC + `where=OBJECTID<cursor` truncation pattern (keep most-recent 250k, `truncated:true`, `total_available`) |
| UT | **451,957** | 1.8x over | ~226 pages; 4–7 h — same DESC truncation pattern required |

(Envelope counts include out-of-state border/CA records; in-state uniques will
be somewhat lower, but both are far above the cap either way. NV closed alone
is ~32% of the national closed layer's 3.8 M records.)

## Timings / server behavior observed

* UT: 22 pages in 2.5 min (~7 s/page). NV: 146 pages in 139 min — page latency
  degraded from ~32 s/page (first hour) to ~95 s/page (final hour), then
  recovered; all pages were full 2000 records (no short pages this run), no
  empty-page-with-exceededTransferLimit events, no exhausted retries.
* The layer is **live**: NV count moved 291,570 → 291,568 during the pull while
  17 new records (OBJECTIDs above the cursor) appeared and were captured
  (seen 291,587). Files are a 2026-07-31 snapshot.

## Verification performed (all passed)

1. **Counts vs server**: UT seen == envelope count exactly (43,263). NV seen
   291,587 vs count-at-pull 291,570 = +0.006% (live-layer growth; within the
   ±0.1% rule); internal accounting exact (kept + outside == seen; written ==
   kept − 0 dupes). Live re-count at verify time: NV 291,568, UT 43,274.
2. **State-polygon reference count** (server-side intersect with the detailed
   TIGER boundary, 590 s timeout needed for NV): NV 275,215 vs written 275,261
   (+0.017%); UT 42,878 vs written 42,677 (−0.47%) — differences are the
   deliberate centroid-vs-polygon-intersect border rule.
3. **Coordinates**: 0 of 317,938 points outside the state envelope (+0.02°
   check pad). Ranges: NV x [−120.00359, −114.03588] y [35.23328, 42.00191];
   UT x [−114.0563, −109.05373] y [37.02672, 41.99205]. Centroids outside the
   *exact* state line (border-straddlers kept by the 500 m buffer): NV 137,
   UT 124 (≤0.3%).
4. **Live spot-checks**: 2 random serials per state re-queried by `CSE_NR`
   (NV105283378, NV101385319, UT101820588, UT106764474): name/type/disp/acres
   exact match, centroid delta 0.00000° — 4/4.
5. Schema key order verified identical to `id_active.json`; all arrays
   index-aligned to `n`.

## Caveats

1. Both files are complete (no truncation — that applies only to future closed
   pulls).
2. Border claims within ~500 m of a state line can appear in two states' files
   (deliberate; same as NW run): NV∩UT 163 shared serials, NV∩OR 153, UT∩ID 34,
   all other pairs 0.
3. Serial prefixes reflect the *administering* office — e.g. `ut_active.json`
   contains NV-prefixed serials for Utah-side border claims (first record
   NV105216478 "WR 221" sits ~0.5 km inside UT at −114.04336, 41.72986).
4. Positions are PLSS-geocoded approximations; `RCRD_ACRS` passed through
   unfiltered.
5. The NV envelope over-fetches eastern California (16,326 discards, ~5.6% of
   the envelope — concentrated in low OBJECTIDs / CAMC serials).

Working files: `/home/claude/nw/raw/fetch_claims_nvut.py` (puller),
`fetch_nvut.log`, `fetch_stats_nvut.json` (full per-job stats),
`envelope_counts_nvut.json` (active+closed envelope counts),
`state_exact_nvut.json` (boundaries), `verify_nvut.py` + `verify_nvut.json`
(verification evidence).
