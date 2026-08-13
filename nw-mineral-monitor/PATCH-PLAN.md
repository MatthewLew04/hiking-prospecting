# PATCH — CA expansion + per-state geology + faults + WS7 geophysics

_The 2026-08-07 patch spec, applied in phases. This file is the working plan:
what shipped in commit one, what each remaining campaign needs, and where
every dataset lives. Status keys: **DONE** (shipped + verified), **WIRED**
(config/schedule in place, data arrives on next runs), **PLANNED** (specced
here, not yet built)._

## GLOBAL — AOI now includes California

- **WIRED** `lambda_updater.py` BBOX gains CA; template gains
  `NightlyActiveCaRule` (10:10 UTC, chains as needed) + `MonthlyClosedRule6`
  (3rd of month, newest-250k cap like NV). First run after deploy populates
  private staging snapshots and stamps the checked tile-build inputs —
  the CA state chip (added, build 2026-08-07f) starts showing claims with no
  further work. CA will dominate row counts; the resumable-chain + lock
  machinery (commits 2e9fa2e/c0fa9b4) was built for exactly this.
- **DONE** `fetch_mrds_wfs.py` — AOI-bbox MRDS extractor via mrdata WFS
  (mrds-high + mrds-low tiers, commodity codes) for anywhere the statewide
  snapshots don't exist yet. Used for the Clear Lake test (1,134 sites,
  414 Hg).
- **PLANNED** statewide CA campaigns, in order: MRDS/USMIN CA site snapshots
  (bulk dumps, same columnar files) → CA counties into the private
  `build-inputs/data/boundaries/counties.json` compatibility input
  (TIGER 500k) + `county_gold.py` STATES → CA AML inventory (DOC AML, CGS
  MinesOnline) as a stategeo_ca-style layer → open-ground machinery for CA
  AOIs (PLSS via CadNSDI CA meridians; **withdrawals become first-class**:
  CDCA, monuments, wilderness, state parks — a Tier-1 target inside withdrawn
  ground gets labeled **DEAD**, shown not hidden; plumbing exists in WS2's
  withdrawal overlay, needs the CA layer set).

## WS6(a) — per-state geology sources (replacing the single-source list)

Already true today: `fetch_geology.py` pulls Macrostrat's harmonized stack,
which serves each state's own compilation where Macrostrat carries it, with
per-unit source + scale recorded (verified: IGS DWM-49 inside Cassia, Graymer
+ Blake 100k quads inside Clear Lake). The per-state campaign is about
COVERAGE AUDIT + direct-source fallback where Macrostrat is thin:

| State | Primary (direct) | Status |
|---|---|---|
| WA | WGS statewide surface-geology GIS | **DONE via Macrostrat** (src 19 = WGS statewide, verified Republic district 2026-08-07) |
| OR | DOGAMI OGDC (latest release) | **DONE via Macrostrat** (src 20 = OGDC release 6, verified Bohemia district) |
| ID | IGS GM/DWM digital maps (REST 502s from pipeline env) | DONE via Macrostrat (DWM-49 verified in Cassia) |
| MT | MBMG geologic GIS | **DONE via Macrostrat** (src 25 = Zientek/MBMG spatial databases, verified Butte–Boulder) |
| WY | WSGS statewide GIS + quad series | **DONE via Macrostrat** (src 157 = WSGS Lander 30×60, verified South Pass) |
| CA | CGS GMC 1:750k + Regional 1:250k + quads | **THE GAP STATE** — Mother Lode audit shows SGMC-500k only (no CGS 250k in Macrostrat); Clear Lake has USGS 100k quads. Direct CGS regional-series ingest is the one genuinely needed WS6a campaign |
| all | USGS SGMC (mrdata WFS `sgmc2`, coded fallback) + Macrostrat | DONE |
| all | NGMDB scanned quads, georeferenced raster fallback | PLANNED — the Tier-1 upgrade path everywhere |

Audit run 2026-08-07 (point-sampled Macrostrat refs over Republic WA,
Butte–Boulder MT, Mother Lode CA, South Pass WY, Bohemia OR): every state's
own compilation is already served EXCEPT California's — see table.

## WS6(b) — dedicated FAULTS layer, full AOI — PARTIAL

- **DONE today**: mapped fault/structure arcs ride along with every geology
  fetch (Macrostrat lines — 516 in Cassia, 1,975 in Clear Lake) and drive
  Tier-2/3 scoring (density, intersections, range-front proximity) in every
  AOI, not just Idaho. SGMC `ms:Structure` WFS confirmed queryable as the
  compilation-arc fallback.
- **BLOCKED upstream**: USGS Quaternary Fault & Fold national ArcGIS service
  answers "Service not started" (checked twice, days apart); hazfaults2014
  reachable but hazard-model-only. When qfaults comes back, add it as the
  age/slip-rate-styled ACTIVE fault layer; until then the Qfaults_GIS.zip
  (shapefile) is the manual path.
- **DONE (2026-08-07)**: CGS Fault Activity Map of California wired into
  `fetch_geology.py` for CA AOIs (FeatureServer layers 14 + 16; age class
  carried per trace) — Clear Lake now scores against 2,273 fault paths incl.
  298 CGS-FAM age-classed traces. Qfaults national: third strike
  ("Service not started"), still waiting.

## CALIFORNIA-SPECIFIC dossier/scrub sources — PLANNED

**DONE (2026-08-07)**: CGS/DOC **Mines Online is live as stategeo_ca**
(`fetch_ca_mol.py` — 1,975 SMARA mines, 748 ACTIVE, status-coded so active
ones render as circles). **USMIN CA fetched via the mrdata WFS**
(`fetch_usmin_wfs.py`, bbox-tiled because the server ignores startIndex) —
but USGS's CA digitization is PARTIAL: 3,182 features statewide (vs 121k NV),
noted in-file. Napa + Yolo recorders verified to official online index
search (operator-export). STILL PLANNED: DOC AML inventory detail, CGS/CDMG
county bulletin series into dossiers/scrub, Lake County recorder check.

**DONE (2026-08-08)**: **CA claims live** — first full `ca_active.json`
pulled (312,453 claims, the biggest layer in the system; lock-v2 chains held
against an all-day BLM throttle, manual takeover after a dead chain's 3 h
TTL). County choropleth flipped from pending to real STAKE scores.
**CA cited grades live** (`grades_ca.py` — 60 mines, 10 counties, from
PP 157/172/194 + Bulls 430/540, all quote-backed + page-cited, MRDS-located,
open-distances vs the new active snapshot; see ASSUMPTIONS #36). Result:
Nevada Co jumps to CA-best #17 overall with 13 rich-open graded mines (+58);
Amador #23 (Sutter Creek belt reads open); Sierra stays muted at #67 because
Alleghany is staked wall-to-wall today — the money layer working as
designed. Remaining CA handicap: `ca_closed.json` (Sept 3 monthly rule or
manual `{"mode":"closed","states":["CA"]}`) — every top-15 county carries
+14 to +30 from staked-then-dropped that CA counties currently get as 0.

## ACCEPTANCE TEST — Clear Lake–Knoxville blind sinter scan — **PASSED (v2 lexicon)**

Run `python3 fetch_geology.py clearlake && python3 geology_targets.py
clearlake`. History, recorded honestly:

1. **v1 lexicon FAILED**, exactly as the test intends: zero T1/T2 (no map
   unit in the AOI says "sinter" or "opal" at any available scale — the
   sinter is sub-map-scale), and 120 Franciscan mélange polygons tied at
   64.0 because "rhyolite" in mélange BLOCK INVENTORIES matched Tier 3.
2. Fixes, each grounded in what the maps actually say: **silica-carbonate →
   Tier 2** (135 units carry it — it IS the mapped expression of
   Knoxville-type Hg-Au systems); **mélange guard** (block-inventory matches
   don't make a volcanic host); **Knoxville-type association rule** — a
   serpentinite/ultramafic body with ≥3 Hg occurrences ≤2 km AND a mapped
   fault ≤1 km classifies Tier 2 *labeled as an association, never as
   description-based* (needed because McLaughlin's corner has only SGMC
   500k coverage, which says just "serpentine"); pathfinder de-saturation
   (1.6/site, CA Hg emphasis 1.5×, cap 22) so dense belts differentiate.
3. **Result**: Wilbur Springs INSIDE target #1 (134.6), Knoxville district
   INSIDE #4, Sulphur Bank 0.13 km from #2, McLaughlin 0.54 km from #4 —
   all four textbook loci of the system in/adjacent to top-4 of 202 targets.
   Residual: scores saturate across the serpentinite belt (the whole belt IS
   prospective; intra-belt discrimination needs grades/workings data), and
   description-based Tier 1 stays impossible until NGMDB quad rasters land.

## WS7 — AEROMAGNETIC OVERLAY

- **(a) DONE** — magnetic anomaly raster live in the map: mrdata mapcache
  WMTS `magnetic` layer (GoogleMapsCompatible), GEOPHYSICS sidebar section,
  opacity slider, drapes over any basemap. ScienceBase WMS
  (`mrt/NAmag_webmerc`) recorded as alternate (503 during build).
- **(d) DONE** — radiometric POTASSIUM sibling overlay: mrdata `aerorad`
  WMS `Potassium` layer, same slider. Adularia = K-feldspar; potassic
  alteration lights up in K. U/Th layers exist on the same service if wanted.
- **(c) DONE** — the public provenance/trust layer is a 1,829-feature national
  PMTiles archive built from the official USGS airborne survey inventory and
  Earth MRI acquisition service. It carries year, type, line spacing,
  altitude/drape metadata, and line-km where published. The retired 819-row
  regional WFS snapshot is retained only under private build inputs; public
  national survey JSON is a CI regression.
- **(b) PLANNED** — high-res Earth MRI grids: per survey block, download
  GeoTIFF from ScienceBase (prefer reduced-to-pole), `gdalwarp` to 3857 →
  `gdal_translate`+`gdaladdo` to COG → `gdal2tiles`/titiler-free static
  XYZ pyramid on S3 under `geophys/tiles/{survey}/` → map raster source per
  survey w/ nT legend + perceptual ramp (matplotlib cividis/viridis LUT
  baked at tile time). Survey-index layer (c) is the discovery UI for which
  blocks exist.
- **(e) PLANNED** — scoring hook: where (b) tiles exist, sample the grid
  under Tier-1/2 targets; discrete magnetic low coincident with structure →
  boost, recorded in the why-card like every other term.

## HOTFIX 2026-08-08b — renderer crash / "storage exhaustion" — **DONE**

User's tab died ("Aw, Snap! error 5") with extension LevelDB
FILE_ERROR_NO_SPACE spam. Diagnosis (see ASSUMPTIONS #37): disk full on
the machine + our genuine RAM bomb — statewide Feature arrays held twice
(app + GL worker), which CA's 312k claims pushed over the edge. The
storage-bloat theory was checked and REFUTED (app never persisted bulk
data). Fix shipped in build 2026-08-08b: banded viewport/coarse pushes,
single columnar copy, layer-off frees GL index, storage governance +
`?debug=1` diagnostics. Measured (tools/measure.js, headless Chromium):
08-07g renderer KILLED at the closed-claims toggle; 08-08b full script —
boot all-8-states 113 MB → worst step 211 MB heap, origin storage 0.0 MB,
every toggle survives. PMTiles server-side tiling stays PLANNED as the
tier-2 upgrade (single-file archives + HTTP range requests fit the
S3/CloudFront static stack).

## Verification ledger (this commit)

- Endpoints probed live: mapcache WMTS tile fetch (PNG), aerorad GetMap
  (PNG), airborne WFS (819 footprints w/ attrs), mrds WFS tiers, earthmri
  WFS (GetCapabilities OK / GetFeature 400 — documented), sgmc2 WFS types,
  qfaults national (down).
- Clear Lake acceptance run end-to-end twice (v1 fail → v2 pass), Cassia
  targets re-run on the v2 formula (rankings stable, Jim Sage still #1).
- JS parse-checked; site build 2026-08-07f.
