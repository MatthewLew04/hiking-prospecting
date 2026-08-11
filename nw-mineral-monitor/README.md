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
| `pipelines/cache/ws10/assets/` | Gitignored staging for quad scans, COGs, legends, and XYZ tiles; upload explicitly to S3 `ws10-assets/` — rasters never enter git |
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

**Geology (quad)** brings the best available detailed mapping to the targets
that rise to the top of the rich-open grade score. The committed inventory
covers the top 15 grade targets plus the four required seed areas, records
their containing and adjacent USGS 7.5-minute quads, ranks catalog results,
and shows a gap explicitly when no qualifying map is available. Ready maps
render as independently toggleable XYZ overlays with opacity, legend, full
citation, retrieval date, and source link. Cataloged, processing, and
low-confidence/manual-review records remain visible in the inventory without
being presented as trustworthy live warps.

The implemented overlays are IGS DWM-193 at De Lamar–Swisher Mountain,
Anderson's 1931 Plate XVIII at Black Pine, and Johnston PP 194 Plate 1 at
Grass Valley. DWM-193's native GIS is normalized into the WS6 unit schema and
feeds a 1:24,000 rescan. Jackson PGM-19-01 remains explicitly blocked: the
source is email-gated, its CGS request has not been sent, and web-tile/database
reuse rights are pending. Source PDFs, working rasters, COGs, and tiles stay
under the ignored `pipelines/cache/` tree and in S3 only.

The current builder uses Pillow, NumPy, tifffile, Fiona, pyproj, Shapely, and
Poppler; it does not require GDAL command-line tools. Anderson and Johnston
pocket plates retain native 400-ppi provenance, while their 600-ppi web/COG
output is explicitly recorded as a resample that adds no source detail.
Build, QA, two-phase upload/readiness order, and the draft-only CGS database
request are documented in `RUNBOOK.md`. Final prepublish QA runs
`pipelines/validate_quad_geology.py` with the dependency-capable Python; it
checks inventory, assets, native-vector rescan, outbox/UI guardrails, and the
no-raster-in-git policy before site deployment.

## Auto-updating

Hosted: EventBridge → Lambda re-pulls **active claims nightly** (09:10 UTC) and **closed claims monthly** from BLM MLRS, rewriting `data/claims/*` in S3 (15-min CloudFront TTL). In the browser: at zoom ≥ 10.5 the map queries BLM's GIS directly for the current viewport and draws **live claim polygons** — current even between snapshots. Everything degrades gracefully to the committed snapshot if BLM is down.

BLM server quirks the updater handles (hard-won; don't "simplify" them away): use OBJECTID-cursor pagination (not `resultOffset`); short pages with `exceededTransferLimit=true` are normal — stop only on an empty page; query with bbox envelopes (detailed polygons exhaust the request budget); `GEO_STATE`/`ADMIN_STATE` are mostly NULL — selection must be spatial; send a User-Agent header (default python-urllib gets 403). `CSE_TYPE_NR` decode: 3841xx=lode, 3842xx=placer, 3843xx=tunnel, 3844xx=mill.

## Known limits

Wyoming closed claims truncated to the most recent 250,000 of 287,066. MRDS is legacy (~2011). USMIN features come from 1958–2001 topo maps. State databases differ in scope (MT = abandoned-mines inventory; OR MILO mixes occurrences and borrow pits; WY is explicitly incomplete). Full caveats in the map's About panel.

⚠ Planning aid only. Never enter adits or shafts. Active claims are private mineral property; verify land status before prospecting.
