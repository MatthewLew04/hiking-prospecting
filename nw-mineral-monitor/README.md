# NW Mineral Monitor

Interactive mineral-research map for **49 states (all except Hawaii)**. The
shared national baseline is delivered as range-read PMTiles: 265,702 MRDS
occurrences, 570,484 USMIN map features, 54,806 validated state-survey mine
records, 7,692 Alaska ARDF occurrences, and 1,721,032 spatially clipped legacy
federal MLRS claim centroids. A second national PMTiles pair carries 559,279
USGS geology polygons and 500,743 fault features with source map, URL, scale,
and scale-status provenance on every encoded feature. The current MRDS and
USMIN archives additionally reconcile all 265,702 and 570,484 stable source
IDs, respectively, in exhaustive maximum-zoom scans bound to their exact file
hashes. The national geology/fault pair now reconciles every one of its
559,279 and 500,743 source IDs at maximum zoom. The only geometry
normalization is a checksum-bound 0.002224 m adjustment to one 5.25 mm Qfault
trace whose distinct vertices otherwise collapse at Web-Mercator's 32-bit
tile precision. Alaska's separate state-law source now reconciles all 118,800
DNR claim records (39,269 active, 51 pending, 79,480 closed). Its immutable
delivery is an exact disjoint union of 118,776 ordinary polygons
(39,263/51/79,462) and 24 unchanged z19 precision-overflow polygons (6 active,
18 closed); no source polygon is dropped, widened, or fabricated. This state
system does not substitute for the still-missing federal Alaska MLRS archive.
State releases are separate from baseline visibility: the
public coverage dashboard
keeps every state marked building until its full DONE gate passes.
The 1,829-feature USGS airborne/Earth MRI survey-footprint trust layer is also
PMTiles-only; its current build reconciles all 1,829 official source IDs to
unique max-zoom feature IDs and forbids density/tile-size dropping.
Reviewed, reproducible grade packages now exist for the six P1 claim states
(AK, AZ, CO, NM, NV, and UT) and the P2 Homestake-belt state (SD). Each has 26
unique graded targets, at least two official primary grade documents,
verbatim numbered-page citations, reviewed page-image hashes where the source
is scan-backed, and a complete state PP 610 district anchor. These are
evidence-only private builds: they clear the numeric/source quota in
isolation, but no public grade gate—and no state—is DONE until its
content-addressed release evidence and every other gate item are accepted.

**Run locally:** `python3 tools/range_server.py 8000` → http://localhost:8000
(no login locally). PMTiles requires HTTP byte ranges; plain
`python3 -m http.server` is not a supported preview server.
**Host on AWS (with private nightly MLRS staging, live viewport queries, and Cognito sign-in):** see [`DEPLOY.md`](DEPLOY.md) — ~15 minutes with `infra/deploy.sh`. The browser reads immutable PMTiles builds; scheduled raw claim pulls stay in a CloudFront-inaccessible staging prefix until a checked tile build is published. The hosted site requires a Cognito login (`auth.json` in the bucket turns the gate on; the repo ships without it, so local use stays open).

## Layout

| Path | What |
|---|---|
| `site/index.html` | The whole app (MapLibre GL, vanilla JS) |
| `site/assets/` | MapLibre GL JS 5.24 (vendored — no CDN dependency) |
| `site/data/tiles/national/` · `tiles/claims/` | Browser-facing PMTiles baselines for MRDS, USMIN, ARDF, state-survey records, federal claims, national geology/faults, and Alaska state claims |
| `build-inputs/` | Strict non-public inventory plus legacy columnar snapshots consumed by offline analysis and PMTiles builders |
| `states/` | Reviewable 49-state adapter registry: regime, survey/GIS sources, serials, AML, trust land, recorder matrix, and DONE-gate evidence |
| `site/data/coverage.json` | Generated state × gate-item dashboard; no asserted pass is inferred from source registration |
| `site/data/districts/` | `curated.json` (28, cited), `cassia.json` (7 deep-dive), `auto.json` (1,056 from MRDS tags) |
| `site/data/manifest.json` | Public tiled-layer inventory, counts, freshness stamps, and live-query spec; it contains no build-input JSON paths |
| `site/data/plss/` · `openground/` · `dossiers/` · `history/` · `alerts/` · `userlayers/` | WS1–WS4 bundles: PLSS sections, section-status grid, mine dossiers, web-scrub corpus, watch digests, ingested layers |
| `site/data/geology/` · `targets/` · `county/` | WS5–WS6 bundles: harmonized geologic map + faults + springs (cited per unit), ranked sinter-first targets with scoring rationale, county-recorder instruments + coverage |
| `site/data/geology-quads/inventory.json` | WS10 top-grade-target map inventory, quad/candidate metadata, overlay pointers, provenance, georeference confidence, gaps, and 24k rescan references |
| `var/ws12/document-store-manifest.json` · `site/viewer.html` | WS12 ignored/private stored-document manifest and the PDF.js citation viewer a chip opens; signed-in browsers receive a minimized catalog from the Docs API |
| `pipelines/cache/ws12/store/` | Gitignored local generation of the document store; hardlinked to its sources and uploaded explicitly to the private S3 `docs/` prefix — PDFs never enter git |
| `portals/` · `pipelines/mine_file_harvest.py` | WS12 strict portal registry plus robots/terms-aware, throttled, resumable, hash-deduplicated public-document harvester |
| `site/data/docs/` · `pipelines/document_index.py` | Public mine-document metadata/coverage plus the private OCR, page-chunk, embedding, identity-join, and citation-index builder |
| `pipelines/spatial_store.py` · `infra/spatial_tools.py` | Private RTree spatial evidence store and ASK tools for geology, claims, mines, faults, magnetic grids, and mine documents |
| `outbox/` · `site/data/outbox/` | Draft-only correspondence and its UI-safe metadata; an outbox file is never authorization to send it |
| `pipelines/` | AOI research pipelines (PLSS, claims w/ legals, land status, open-ground compute, web scrub, dossiers, inbox ingest) — config-driven via `config/aoi.json`, cached, idempotent |
| `site/model3d.html` · `site/assets/geomodel/` · `pipelines/geomodel/` | **3-D geological modeller** (Leapfrog-style alternate view): every mine card's **OPEN 3D MODEL** builds a model around the site — terrain + draped imagery/geology, **structural geology without drillholes** (dip/dip-azimuth derived from where mapped contacts and faults cross the DEM, digitised on the draped map, stereonet with Kamb contouring + Bingham/Fisher, declustering, form interpolant, structural trends), workings digitised from historic maps (georeferenced level plans / sections → adits, drifts, shafts, stopes), pancake stratigraphy, kriged block models, slicing; imports/exports OMF v0.9 + v2.0, DXF/OBJ/GOCAD, Surfer/Geosoft/GXF/ZMAP/Irap grids, UBC, CSV, SEG-Y, LAS — see [`GEOMODEL.md`](GEOMODEL.md); what it has, lacks and will not do relative to Leapfrog is in [`LEAPFROG-PARITY.md`](LEAPFROG-PARITY.md) |
| `pipelines/leapfrog_export.py` · `exports/leapfrog/` | Leapfrog Geo starter kits: the map's **LEAPFROG EXPORT** button packages the current view client-side (UTM CSVs w/ grades, AOI shapefiles, Arc/Info ASCII DEM, round-trip-validated OMF v0.9, README) and the pipeline builds full-AOI kits — see [`LEAPFROG.md`](LEAPFROG.md); 3D TERRAIN is the in-browser counterpart |
| `MLRS-PUBLICATION.md` | Operator contract for building immutable 19-state federal active/closed PMTiles from checksum-pinned private staging |
| `OPEN-GROUND-PUBLICATION.md` | Conservative PLSS-section open-ground derivation and immutable PMTiles publication contract for the exact 19 claim states |
| `NONCLAIM-EQUIVALENTS-PUBLICATION.md` | Exact 30-state AML/trust-land inventory, evidence, and immutable PMTiles publication contract for the non-claim regime |
| `LAND-CONTEXT-PUBLICATION.md` | Exact 30-state per-target surface/mineral ownership join and immutable land-context PMTiles publication contract |
| `GRADE-EVIDENCE-PUBLICATION.md` | Exact 49-state grade, PP 610 district-anchor, quotation/page-cite, and multi-commodity price evidence contract |
| `ALASKA-GRADE-EVIDENCE.md` · `ARIZONA-GRADE-EVIDENCE.md` · `COLORADO-GRADE-EVIDENCE.md` · `NEVADA-GRADE-EVIDENCE.md` · `NEW-MEXICO-GRADE-EVIDENCE.md` · `UTAH-GRADE-EVIDENCE.md` · `SOUTH-DAKOTA-GRADE-EVIDENCE.md` | Reviewed state grade corpora, page/image hash verification, PP 610 district extraction, and evidence-only build workflows |
| `RECORDER-EVIDENCE-PUBLICATION.md` | Exact 19-state live-claim jurisdiction join and reviewed recorder-portal coverage evidence contract, including Alaska recording districts |
| `CI-ACCEPTANCE-EVIDENCE.md` | Content-addressed per-state browser acceptance evidence bound to the candidate manifest, coverage grid, budgets, tiled descriptors, real load/query/request observations, and state off/on lifecycle |
| `DOCUMENT-STORE-PUBLICATION.md` | WS12 raw/searchable object contract, sha256 key scheme, OCR and quote-location rules, private presigned delivery, and the citation-to-page publication boundary |
| `TARGET-SCORING-EVIDENCE.md` | Exact 49-state richOpen/land-context target-score evidence with regime-aware N/A-vs-zero ordering and checksum-bound grade, geology, and land inputs |
| `ZERO-INVENTORY-EVIDENCE.md` | Content-addressed proof for truthful zero-feature state/layer results, bound to the fully scanned national archive instead of fake sentinel geometry |
| `NATIONAL-GEOLOGY-FAULT-LOSSLESS.md` | Exact source-OID/row audit and fail-closed z12 `fid` reconciliation contract for the 49-state geology/fault PMTiles pair |
| `NEVADA-STATE-SURVEY-BASELINES.md` · `ARIZONA-STATE-SURVEY-BASELINES.md` · `COLORADO-STATE-SURVEY-BASELINES.md` · `UTAH-STATE-SURVEY-BASELINES.md` | Lossless official state-survey baseline sources, clipping/repair/exclusion inventories, PMTiles fingerprints, and lazy browser descriptor contracts |
| `NATIONAL-BASELINE-PUBLICATION.md` | Truth matrix for national feeds: actual tiled artifacts, remote queries/tiles, AOI ingestion, link-only integrations, and lossless MRDS/USMIN/MLRS point publication |
| `OPEN-GROUND-PUBLICATION.md` | Conservative PLSS-section open-ground contract, including exact MLRS/CadNSDI spatial-join staging and independent four-source mineral-disposition evidence |
| `pipelines/cache/ws10/assets/` | Gitignored staging for published COGs, legends/previews, and XYZ tiles; upload explicitly to S3 `ws10-assets/` — rasters never enter git |
| `data-inbox/` | Drop files here + run `pipelines/inbox_ingest.py` → permanent map layers |
| `demo/` | `messy_cassia.csv` acceptance-test file (see DEMO.md) |
| `infra/` | CloudFormation template, Lambdas (claims updater, AI relay, **expiration watch**), deploy script |

Browser-facing PMTiles and COG files are configured for Git LFS rather than
ordinary Git blobs; the WS11 workflow checks them out with LFS before validating
their bytes, hashes, directories, and tile payloads. The current working tree's
24 tiled artifacts still need to be staged and committed through LFS before CI
can pass. A pointer-only checkout is not a release checkout and will fail the
artifact gate rather than silently ship.

## The four workstreams (2026-08)

**WS1 universal ingest** — drag CSV/XLSX/GeoJSON/KML/KMZ/GPX/zipped-SHP onto
the map; lat-lon, UTM, and PLSS legal descriptions ("T12S R22E Sec 14")
auto-geocode; layers persist (IndexedDB) with a registry, export, and
full-attribute popups. **WS2 open ground** — a section-status grid for the
AOI (default Cassia County): open-with-history / was-claimed-now-open /
active / withdrawn / non-federal, from claim legal descriptions × SMA ×
withdrawal-segregation cases; every section popup shows its evidence.
**WS2d expiration watch** — registry-driven daily MLRS disposition diffs for
all 19 claim states + an Aug 25–Sep 10 six-hourly fee-window scan, with private
state snapshots, state-labelled SES/webhook alerts, and an on-map WATCH panel.
Alaska's independent DNR state-claim rent/labor watch is merged without mixing
its identifiers with federal MLRS. **WS3 dossiers** — per-mine/per-claim research files: cited
facts, MLRS serial-register path, county-recorder & SoS guidance, Mindat /
Chronicling America / HathiTrust prefilled searches. **WS4 web scrub** —
automated Chronicling America + Google Books + MSHA sweep, deduped by
name-variant, rendered as a chronological history in each dossier. The scrub
uses the registry's full state name and safe defaults for any AOI; absent
legacy claim/MRDS inputs are optional instead of forcing an Idaho-only failure.
National mine and claim cards expose prefilled Chronicling America and EDGAR
research plus an explicitly link-only SEDAR+ route. Those links are research
entry points, not locally ingested filing corpora.

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

## 3-D model (2026-08-21)

`site/model3d.html` is the Leapfrog-style alternate view. A mine card's **⛰ OPEN
3D MODEL** opens it around that site with the terrarium terrain, draped
satellite / USGS topo / Macrostrat imagery, the AOI's mapped units and faults
on the ground, graded mines, targets and claim centroids. Tools: georeference a
scanned level plan or section and trace adits / drifts / shafts / raises /
stopes into 3-D (feet converted at the door, every feature keeps its source and
confidence); a pancake stratigraphy builder (RBF / kriging contact surfaces with
deposit / erosion rules, unit volumes, virtual drillholes); block models with
variogram fitting and ordinary kriging; RBF implicit surfaces; sections that
clip, intersect, fill and slice everything with a 2-D section panel. File I/O
covers what Leapfrog (OMF v2.0 for 2025.1+, v0.9 for older, DXF, OBJ, GOCAD,
CSV, grids), Surfer, Geosoft (GRD, GXF, XYZ, UBC) and Kingdom (ZMAP+, Irap,
SEG-Y, LAS) read and write. The Python twin `pipelines/geomodel/` builds the
same kits in batch (`pipelines/geomodel_kit.py site --grade-index 157`) and
converts between any supported formats. Details, format matrix, honesty notes
and the mine-files → underground-maps roadmap: [`GEOMODEL.md`](GEOMODEL.md).

Turning a **written** mine description into a 3-D model — from the command line
or from an agent over HTTP — is [`MINE-VISUALS-GUIDE.md`](MINE-VISUALS-GUIDE.md).

## WS12 — mine files, cited documents, and GIS tools (2026-08-14)

WS12 adds a validated 33-entry state/federal mine-file portal registry. Three
reviewed adapters are executable today (IGS, AZGS ADMMR, and NBMG); every other
requested portal is retained with an explicit publication-catalog,
index-only, manual-request, access-blocked, or no-attachment result. The
harvester obeys robots and reviewed terms, throttles by portal, resumes from a
SQLite frontier, records candidates and skips, deduplicates by SHA-256, and
writes byte-verified originals only to private S3. Rights fail closed: a
publicly reachable file is downloaded only when state/federal public-domain
status is established, and paywalled or ambiguous-rights material is skipped.
See `MINE-FILE-HARVEST.md`.

Eligible PDFs flow through page-preserving OCR, stronger-OCR fallback queues,
page-local chunks, Titan embeddings, and exact-ID then fuzzy-name+TRS joins.
The browser receives only compact metadata and coverage; original PDFs, OCR
text, vectors, and the SQLite search index remain private. ASK document facts
must come from bounded search hits and include document title, exact PDF page,
and source URL. The IF0126 acceptance corpus includes its eligible USGS
MILS/MRDS records; the rights-unverified corporate property file is recorded
as a skip rather than silently treated as public domain. See
`WS12-DOCUMENT-INDEX.md`.

The coordinate tools query a generated SQLite/RTree evidence store (with an
optional GeoParquet/DuckDB production export): `geology_at`, `claims_at`,
`mines_near`, `faults_near`, `mag_at`, and `docs_for`. Geology and fault rows
retain their named source map and representative-fraction scale. Queryable
vectors and raster-only context stay distinct, so Jackson's 1:24,000 image
cannot masquerade as a unit polygon while DWM-193's native 1:24,000 vectors
can. See `WS12-GIS-TOOLS.md`.

## Auto-updating

Hosted: EventBridge → Lambda re-pulls federal MLRS **active claims nightly**
and **closed claims monthly** for all 19 claim states. Raw updater snapshots stay under
private `staging/claims/` for the PMTiles publication job; statewide JSON is
never a browser artifact. The expiration watch also pages all 19 active layers
to completion into private, state-clipped `watch/federal/` snapshots. Only
alert-sized, state-labelled evidence is public, and interrupted or unavailable
states render unknown rather than zero. In Alaska, a separate scheduled watcher tracks the
DNR state-claim rent and labor clocks, and the committed state-claim polygons
remain explicitly separate from federal MLRS. At zoom ≥10.5 the browser can also query
BLM GIS for live viewport polygons.

After a complete 19-state run, the watcher also writes immutable, content-hash-
named `data/evidence/watch/<run-id>/<state>-<sha256>.json` DONE-gate evidence.
Each row binds `active_now` to the SHA-256 of its private source snapshot; AK
evidence is withheld until both `federal_mlrs` and `alaska_state_claims` are
complete. These artifacts support review but do not toggle any state release.
For registry promotion, copy the reviewed canonical JSON bytes to
`site/map-assets/releases/**/<sha256>.json` and record that object's exact
`evidence_artifact`/`evidence_sha256`/`evidence_bytes`; the runtime key is a
review source, not itself a release path.

BLM server quirks the updater handles (hard-won; don't "simplify" them away): use OBJECTID-cursor pagination (not `resultOffset`); short pages with `exceededTransferLimit=true` are normal — stop only on an empty page; query with bbox envelopes (detailed polygons exhaust the request budget); `GEO_STATE`/`ADMIN_STATE` are mostly NULL — selection must be spatial; send a User-Agent header (default python-urllib gets 403). `CSE_TYPE_NR` decode: 3841xx=lode, 3842xx=placer, 3843xx=tunnel, 3844xx=mill.

## Known limits

The compatibility claim archive honestly preserves three incomplete capped
snapshots: NV, UT, and WY need fresh state-clipped closed pulls before release.
Alaska's DNR base+precision delivery is now losslessly reconciled, but Alaska
still needs its separate federal MLRS artifact and the other per-state DONE
evidence; the state system is not accepted as a replacement for federal MLRS.
MRDS is a legacy occurrence catalogue, and USMIN features are historical map
symbols rather than field verification. State-survey databases differ in scope.
Most importantly, national baselines do not make a state DONE: open ground or
land context, best-scale geology/faults, aeromag provenance, cited grade quotas,
recorder coverage, quad inventory, and scale acceptance must all pass together.

⚠ Planning aid only. Never enter adits or shafts. Active claims are private mineral property; verify land status before prospecting.
