# NW Mineral Monitor

Interactive map of mines, mining claims, and mineral sources across **Washington, Oregon, Idaho, Montana, and Wyoming** — 177,994 sites from 7 government databases, 113,330 active + 819,158 closed BLM mining claims, and 472 mining districts (28 deeply researched, 7 Cassia County deep-dives, 437 auto-derived from MRDS).

**Run locally:** `cd site && python3 -m http.server 8000` → http://localhost:8000 (no login locally)
**Host on AWS (with nightly auto-updating claims + Cognito sign-in):** see [`DEPLOY.md`](DEPLOY.md) — ~15 minutes with `infra/deploy.sh`. The hosted site requires a Cognito login (`auth.json` in the bucket turns the gate on; the repo ships without it, so local use stays open).

## Layout

| Path | What |
|---|---|
| `site/index.html` | The whole app (MapLibre GL, vanilla JS) |
| `site/assets/` | MapLibre GL JS 5.24 (vendored — no CDN dependency) |
| `site/data/sites/` | Per-state columnar site files: `mrds_*` (USGS MRDS), `usmin_*` (USGS topo-map mine features), `stategeo_*` (IGS DD-1 / WGS DDS-30 / DOGAMI MILO-4 / MBMG AIM / WSGS) |
| `site/data/claims/` | Per-state claim centroids: `*_active.json` (serial, name, type, disposition, acres) and `*_closed.json` — refreshed by the Lambda when hosted |
| `site/data/districts/` | `curated.json` (28, cited), `cassia.json` (7 deep-dive), `auto.json` (437 from MRDS tags) |
| `site/data/manifest.json` | Layer inventory, counts, freshness stamps, live-query spec |
| `site/data/plss/` · `openground/` · `dossiers/` · `history/` · `alerts/` · `userlayers/` | WS1–WS4 bundles: PLSS sections, section-status grid, mine dossiers, web-scrub corpus, watch digests, ingested layers |
| `site/data/geology/` · `targets/` · `county/` | WS5–WS6 bundles: harmonized geologic map + faults + springs (cited per unit), ranked sinter-first targets with scoring rationale, county-recorder instruments + coverage |
| `site/data/geology-quads/inventory.json` | WS10 top-grade-target map inventory, quad/candidate metadata, overlay pointers, provenance, georeference confidence, gaps, and 24k rescan references |
| `outbox/` · `site/data/outbox/` | Draft-only correspondence and its UI-safe metadata; an outbox file is never authorization to send it |
| `pipelines/` | AOI research pipelines (PLSS, claims w/ legals, land status, open-ground compute, web scrub, dossiers, inbox ingest) — config-driven via `config/aoi.json`, cached, idempotent |
| `pipelines/cache/ws10/assets/` | Gitignored staging for published COGs, legends/previews, and XYZ tiles; upload explicitly to S3 `ws10-assets/` — rasters never enter git |
| `data-inbox/` | Drop files here + run `pipelines/inbox_ingest.py` → permanent map layers |
| `demo/` | `messy_cassia.csv` acceptance-test file (see DEMO.md) |
| `infra/` | CloudFormation template, Lambdas (claims updater, AI relay, **expiration watch**), deploy script |

## The four workstreams (2026-08)

**WS1 universal ingest** — drag CSV/XLSX/GeoJSON/KML/KMZ/GPX/zipped-SHP onto
the map; lat-lon, UTM, and PLSS legal descriptions ("T12S R22E Sec 14")
auto-geocode; layers persist (IndexedDB) with a registry, export, and
full-attribute popups. **WS2 open ground** — a section-status grid for the
AOI (default Cassia County): open-with-history / was-claimed-now-open /
active / withdrawn / non-federal, from claim legal descriptions × SMA ×
withdrawal-segregation cases; every section popup shows its evidence.
**WS2d expiration watch** — daily MLRS disposition diff + Aug 25–Sep 10
6-hourly fee-window scan, SES email + webhook + on-map WATCH panel with
deep links. **WS3 dossiers** — per-mine/per-claim research files: cited
facts, MLRS serial-register path, county-recorder & SoS guidance, Mindat /
Chronicling America / HathiTrust prefilled searches. **WS4 web scrub** —
automated Chronicling America + Google Books + MSHA sweep, deduped by
name-variant, rendered as a chronological history in each dossier.

## WS5 + WS6 (2026-08-06)

**WS5 county-direct claim extraction** — claims become real at the county
recorder before BLM ever sees them (state recording first; FLPMA's 90-day
BLM window + adjudication lag after). Cassia has no online index (verified),
so the adapter is operator-assisted: prefilled records request, header-sniffed
ingest from `data-inbox/county/`, doc-type classification, TRS parsing, fuzzy
name+TRS matching to MLRS serials (confidence-tiered), county instruments in
each claim dossier, and two new WATCH signal classes — **COUNTY-RECORDED —
NOT IN MLRS** and **ASSESSMENT FILED (COUNTY)**. Per-county coverage matrix
in `COUNTY-COVERAGE.md`. **WS6 geology targets (sinter-first)** — the full
geologic map for the AOI (Macrostrat-harmonized: SGMC 1:500k + IGS DWM-49
1:100k here, citation + scale per unit) plus mapped faults, GNIS hot/warm
springs, and IDWR geothermal wells, scored by a tiered engine: T1 sinter/
opaline hot-spring deposits (flagged anywhere, regardless of proximity to
anything), T2 silicified/hydrothermally-altered (fault-weighted), T3
rhyolite–tuff–bimodal epithermal hosts, travertine labeled separately; boosts
for fault intersections, pathfinder commodities (Hg Sb As ×2), thermal
springs/wells, and WS2 open ground (tier≤2 + open = the money flag). Every
target renders an explanation card: verbatim unit description, source-map
citation + scale, the arithmetic of its score, and the land status under it.

Judgment calls: `ASSUMPTIONS.md`. Operations: `RUNBOOK.md`. Walkthrough: `DEMO.md`.

## WS10 — quad-scale geology (2026-08-11)

**Geology (quad)** brings the best available target-covering mapping to the
top of the rich-open grade score. The deployed inventory has 19 target rows:
the top 15 ranked targets plus the four required seed areas. Its 19
checkboxes point to 18 real underlying rasters — four seed overlays and 14
ranked-map selections — because Idaho Bonanza and Atlanta intentionally share
the target-covering Hailey sheet. Checking a row closes the inventory and
automatically pans/fits the map to the selected overlay; the two rows that
share Hailey stay synchronized. There are no placeholder checkboxes that
render an empty layer.

The four seed overlays are IGS DWM-193 at De Lamar–Swisher Mountain, Jackson
PGM-19-01, Anderson's 1931 Plate XVIII at Black Pine, and Johnston PP 194
Plate 1 at Grass Valley. The 14 ranked selections come from official NGMDB
georeferenced KMZ holdings. Their KML GroundOverlay bounds were inspected,
rotation was checked, and every associated target coordinate — both targets
for Hailey — was verified inside its image footprint. A reduced whole-sheet
**map preview** accompanies these ranked selections; it is context, not a
geologic-unit legend. The four seed products instead expose reviewed collar
legend crops. Citation, scale, retrieval date, source link, selection note,
and georeference provenance remain available for every layer.

Some immediately usable selections are intentionally regional scale
exceptions: Willow Creek/Pearl uses 1:125,000, Azurite and New Trail use
1:100,000, and Excelsior, Mc Grath, Idaho Bonanza/Atlanta, and Mammoth use
1:250,000. The inventory labels those limitations and retains the finer but
non-georeferenced P-41, Bannack–Grayling I-433, DOGAMI GMS-38, OFR 2004-1205,
and IGS GM-45 products as upgrade candidates rather than shipping an
unreviewed warp. Catalog candidates and the Grass Valley modern-map gap stay
visible; a fallback is not described as quad-scale evidence merely because
it can be toggled.

Jackson uses the official public NGMDB 4096×4096 georeferenced KMZ raster and
a true legend crop made from the NGMDB sheet preview. The project owner
directed academic-use deployment without a separate reuse review; the
inventory preserves CGS/NGMDB attribution and does not claim an open-content
license. The original CGS PDF remains available only through its
email-delivery/ADA workflow, and native attributed GIS is not publicly
available. Consequently Jackson is a visual overlay, not a vector source,
and has no WS6 rescan. The unsent CGS outbox draft is retained only as a
superseded request for native GIS. DWM-193's native GIS is normalized into
the WS6 unit schema and feeds the sole 1:24,000 rescan. Downloaded source
PDFs/KMZs and working rasters use the ignored `pipelines/cache/` tree only
while staged and may be evicted after their official URL and checksum are
recorded. Published COGs, previews, legends, and tiles live in S3; raster
artifacts never enter git.

The current builder uses Pillow, NumPy, tifffile, Fiona, pyproj, Shapely, and
Poppler; it does not require GDAL command-line tools. Anderson and Johnston
pocket plates retain native 400-ppi provenance, while their 600-ppi web/COG
output is explicitly recorded as a resample that adds no source detail.
Build, QA, disk-safe sequential upload/readiness/eviction order, and the
unsent, superseded CGS GIS-request draft are documented in `RUNBOOK.md`.
Final prepublish QA combines each layer's local checksum/tile validation and
remote-object verification before eviction with a metadata/UI validation of
the final 18-ready/zero-blocked set, the DWM-193 native-vector rescan,
outbox guardrails, and the no-raster-in-git policy.

## Auto-updating

Hosted: EventBridge → Lambda re-pulls **active claims nightly** (09:10 UTC) and **closed claims monthly** from BLM MLRS, rewriting `data/claims/*` in S3 (15-min CloudFront TTL). In the browser: at zoom ≥ 10.5 the map queries BLM's GIS directly for the current viewport and draws **live claim polygons** — current even between snapshots. Everything degrades gracefully to the committed snapshot if BLM is down.

BLM server quirks the updater handles (hard-won; don't "simplify" them away): use OBJECTID-cursor pagination (not `resultOffset`); short pages with `exceededTransferLimit=true` are normal — stop only on an empty page; query with bbox envelopes (detailed polygons exhaust the request budget); `GEO_STATE`/`ADMIN_STATE` are mostly NULL — selection must be spatial; send a User-Agent header (default python-urllib gets 403). `CSE_TYPE_NR` decode: 3841xx=lode, 3842xx=placer, 3843xx=tunnel, 3844xx=mill.

## Known limits

Wyoming closed claims truncated to the most recent 250,000 of 287,066. MRDS is legacy (~2011). USMIN features come from 1958–2001 topo maps. State databases differ in scope (MT = abandoned-mines inventory; OR MILO mixes occurrences and borrow pits; WY is explicitly incomplete). Full caveats in the map's About panel.

⚠ Planning aid only. Never enter adits or shafts. Active claims are private mineral property; verify land status before prospecting.
