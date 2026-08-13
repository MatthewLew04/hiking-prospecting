# NW Mineral Monitor — user guide

_Build 2026-08-13 · WS11 national baseline and visible per-state release gates._

The Monitor is one self-contained map app (`site/index.html`, MapLibre GL) for
49 states (all except Hawaii). Its immutable national baseline currently
contains 898,684 MRDS/USMIN/state-survey/Alaska-ARDF features and 458,049 active
plus 1,262,983 closed tiled federal BLM claim centroids. The national geology
switches add 559,279 USGS geology polygons and 500,743 fault features, each
carrying its source map, scale, scale-status finding, citation, and URL.
Alaska separately has
39,269 active, 51 pending, and 79,480 closed DNR state-claim polygons; that
state system does not replace the still-missing federal Alaska MLRS artifact.
The cited-grade corpus, researched
districts, and Cassia County deep tools remain richer in the existing western
footprint while the visible state × gate grid records the national research
still outstanding. Baseline visibility is not a state release: only the
coverage dashboard says whether every required state artifact and evidence
item has passed.

**The one rule:** everything here is a research lead, **not a title search**.
Patented private land shows no BLM claims and looks "open" in every layer.
Active claims are private mineral property. Verify at BLM, the county
recorder, and on the ground before staking — and never enter adits or shafts.

---

## 1 · Quick start

Local (no login):

    cd nw-mineral-monitor && python3 tools/range_server.py 8000
    → http://localhost:8000

Hosted (Cognito login, nightly claim refresh, alert emails): `DEPLOY.md`;
after any change: `git push` then `cd infra && bash deploy.sh` — the footer's
build stamp must match the version at the top of this guide.

Screen layout: **header** (live counts, search, basemap DARK/SAT/TOPO, INTEL /
ASK / + DATA / WATCH / READING / ABOUT buttons) · **left sidebar** (layer
toggles + filters, top to bottom below) · **map** · **right detail panel**
(opens when you click anything) · **footer** (coords, disclaimers, data
freshness).

---

## 2 · What changed in the latest releases

**WS10 — quad-scale geology (2026-08-11).** **MAP INVENTORY** now gives all
19 targets a checkbox: the top 15 rich-open rows plus the four required seed
areas (De Lamar–Swisher Mountain, Jackson, Black Pine, and Grass Valley).
Those controls select 18 real underlying rasters — four seed overlays and 14
ranked-map selections — because Idaho Bonanza and Atlanta share the Hailey
regional map. Enabling a map closes the inventory and automatically pans/fits
to its footprint, so an overlay in another state no longer appears to do
nothing. See §7d.

Every ranked selection is an official NGMDB georeferenced KMZ whose KML
bounds and target containment were reviewed. Some are explicitly labeled
regional fallbacks where a finer target map is available only as a
non-georeferenced scan; those finer products remain upgrade candidates. The
four seed maps provide reviewed legend crops. Ranked selections provide a
reduced map preview, which is not presented as a geologic-unit legend.
Jackson uses the official public NGMDB 4096×4096 georeferenced KMZ and a true
legend crop from its NGMDB sheet preview. It is published under the project
owner's academic-use direction with CGS/NGMDB attribution; no open-content
license is claimed.

**WS5 — county-direct claim extraction (2026-08-06).** Claims become public record at the
county recorder weeks-to-months before they appear in BLM's MLRS (state
recording first; the FLPMA filing is due within 90 days, then adjudication
lag). The new pipeline turns that gap into a signal: ingest recorder index
exports, match instruments to MLRS serials, and surface two new WATCH alert
classes — **COUNTY-RECORDED — NOT IN MLRS** (someone staked; BLM doesn't show
it yet) and **ASSESSMENT FILED (COUNTY)** (claimant actively maintaining).
Cassia has **no online index** (verified 2026-08-06), so the workflow is
operator-assisted — see §6.

**WS6 — geology targets (sinter-first).** The full geologic map for the
Cassia AOI (harmonized via Macrostrat: USGS SGMC 1:500k + IGS Twin Falls
1:100k here, with the source map cited per unit), plus mapped faults, GNIS
hot/warm springs, and IDWR geothermal wells — scored by a tiered engine into
**58 ranked exploration targets**, each with a full explanation card. See §5.

**County gold-signal ranking.** The existing western county research set is scored for
**stakeable gold** (what you could still act on) and **endowment** (where the
gold is, staked or not), with an interactive choropleth, itemized score
cards, and `GOLD-COUNTIES.md`. See §7.

Earlier releases (WS1–WS4, same architecture): universal data ingest,
open-ground section grid, expiration watch, dossiers, automated history
sweep — folded into the guide below where you'd actually use them.

---

## 3 · National baseline layers (49 states, excluding Hawaii)

**SITE LAYERS**
- **USGS MRDS mineral sites** — amber. ◯ circle = producer/active in the
  record; ● dot = old mine/prospect/occurrence. Coordinates can be off by
  hundreds of meters; status flags frozen ~2011. Click → record + MRDS link.
- **State geo-survey databases** — blue. The current compatibility archive
  carries six reviewed state inventories; the coverage dashboard shows which
  additional state adapters and artifacts are still building. These are often
  better located than legacy MRDS records.
- **Alaska ARDF occurrences** — purple. The 7,692-record USGS Alaska Resource
  Data File is the occurrence backbone in Alaska and retains commodity,
  district, geologic-description, workings, and primary-reference fields.
- **Topo-map workings (USMIN)** — steel. Shafts, adits, prospect pits
  digitized from 1958–2001 topo sheets — the best "something was actually dug
  here" evidence. Sub-toggle folds in gravel/borrow pits (off by default).
- **Cited ore grades** — gold dots, brighter/bigger = richer. 3,369 rows
  with quote-backed oz/ton values from USGS bulletins and inspector reports.
  Click one: the grade, the verbatim quote, the source PDF. This remains the
  existing western grade corpus while the per-state 25-mine research gates are
  visibly incomplete; national occurrence coverage is not a claim of national
  grade completion.
- At low zoom the point layers render as density heat; zoom past ~7 for
  individual sites. **COMMODITY FILTER** chips + ALL/EXISTING/OLD radio
  filter every site layer at once.

**CLAIM LAYERS**
- **Active claims (tiled build snapshot)** — teal centroids, 458,049 in the
  currently published compatibility archive. Exact state counts and the build
  date come from the manifest.
- **Live claim boundaries (BLM)** — at zoom ≥ 10.5, exact current polygons
  straight from BLM for the viewport. Always current; the badge shows count.
- **Closed claims (historic)** — maroon embers streamed from the same PMTiles
  archive. Once-staked, later-dropped ground is not automatically open: verify
  current claims, withdrawals, and ownership. NV/UT/WY are explicitly marked
  partial because their source snapshots were capped before state clipping.
- **Alaska state claims** — DNR polygons with separate active+pending and
  closed toggles. These are state-law claims with their own rent and annual
  labor clocks. They are labeled separately from federal MLRS everywhere; a
  DNR layer never fills missing federal coverage, and a closed record alone
  does not prove present mineral-entry availability.

**CONTEXT / FAMOUS DISTRICTS** — county lines, 28 curated district cards +
7 Cassia deep-dives + 1,056 auto-derived MRDS district labels.

**Search** (header) finds mines, claims by name or serial, districts, and
places. **+ DATA / MY DATA**: drop CSV, XLSX, GeoJSON, KML/KMZ, GPX, or
zipped SHP on the map — lat/lon, UTM, and PLSS legals ("T12S R22E Sec 14")
auto-geocode through national BLM CadNSDI; layers persist in your browser and
can be exported. Include a `state`/`st` column. If the same township/range is
present under more than one principal meridian in that state, also include a
`plss_meridian` code such as `08` or `ID08`; the importer reports ambiguity
instead of silently choosing the first section returned.

---

## 4 · OPEN GROUND — CASSIA (section grid)

Toggle **SECTION STATUS GRID**: every square-mile section in Cassia County
colored by claim/land status, computed from claim *legal descriptions* (not
geometry guesswork) × surface management × withdrawals:

- green **OPEN** — historic workings present, no active claim, federal
  locatable surface: the prime research squares
- amber **CLOSED_ONLY** — was claimed, dropped, nothing active today
- red **ACTIVE** — actively claimed (private mineral property)
- violet **WITHDRAWN** — not open to location; gray **NONFEDERAL** — private/
  state surface (the patented-ground trap lives here); dark **QUIET**
- Click a section → why it's classified, its workings, and every serial
  touching it (each links to a dossier).

## 5 · GEOLOGY TARGETS — CASSIA (WS6)

Two toggles:
- **GEOLOGIC MAP + FAULTS** — the harmonized geologic map in its source
  colors + dashed fault traces. Click any unit for its verbatim description
  and source-map citation with scale.
- **TARGETS (RANKED)** — 58 scored polygons. Colors: red = Tier 1 (sinter /
  opaline hot-spring deposits), orange = Tier 2 (silicified / hydrothermally
  altered), blue = Tier 3 (rhyolite–tuff–bimodal epithermal hosts), violet =
  travertine (related plumbing, wrong chemistry). Thick outline + ★ = the
  **money layer**: tier ≤2 chemistry sitting on open ground. Very large
  regional units render as faint outlines so they can't flood the view.

Click a target → its card: **WHY THIS SCORES** (the actual arithmetic —
fault distance, fault intersections, pathfinder occurrences with Hg/Sb/As
weighted double, thermal springs/geothermal wells, open-ground fraction),
the **verbatim unit description**, the source-map citation + scale, the open
sections under it, and fly-to / source-record links. "▸ ranked target list"
in the sidebar gives the top 20.

Honesty note printed in the layer itself: **no Tier-1 sinter or Tier-2
alteration is mapped in Cassia at the available scales** (1:500k + 1:100k) —
that's a statement about map resolution, not the ground. Scanned quad-scale
maps and satellite alteration indices are the recorded upgrade path
(ASSUMPTIONS.md #27–31). Current #1: the Jim Sage-area Miocene rhyolite
complex (fault-dense, geothermally active, 30/34 overlapped sections open).

## 6 · COUNTY RECORDS — CASSIA (WS5)

Cassia has no online recorded-document index, so this runs operator-assisted:

1. Sidebar → **"coverage matrix + records request"** — a prefilled request
   (doc types × date range × your townships) to email to recorder@cassia.gov,
   or call (208) 878-5240 for a records-vault appointment.
2. Drop whatever comes back — CSV/TSV/JSON export, or a spreadsheet you type
   from the index books — into `data-inbox/county/cassia/` (any sane column
   headers work).
3. `python3 pipelines/county_records.py`, redeploy. Instruments are
   classified (notice of location / amended / assessment affidavit /
   quitclaim / deed), TRS-parsed, and fuzzy-matched to MLRS serials by claim
   name + section with HIGH/MED/LOW confidence.

Results surface in three places: matched instruments appear inside each
claim's **dossier**; new locations with no MLRS match appear in **WATCH** as
teal **COUNTY-RECORDED — NOT IN MLRS** (the earliest public signal a claim
exists — the watch Lambda auto-retires it once the case reaches MLRS); and
assessment affidavits appear as **ASSESSMENT FILED (COUNTY)**. Try the full
flow with synthetic data: `python3 pipelines/county_records.py --demo`
(everything shows a DEMO tag; don't deploy it). Neighboring-county portal
status: `COUNTY-COVERAGE.md`.

## 7 · GOLD SIGNAL — COUNTIES

Toggle **STAKEABLE-GOLD CHOROPLETH**: all 244 counties shaded by stakeable-
gold score (dark → bright gold). Hover for rank; click a county (anywhere
without a site/target on top) for its card:

- **STAKEABLE — itemized**: cited-grade gold mines ≥0.3 oz/t on open ground
  (the strongest lead class — documented gold, nobody holding it), gold
  sites **staked-then-dropped** (closed claim nearby, nothing active —
  someone proved it, then let it lapse), unclaimed gold occurrences,
  producer validation, workings density.
- **ENDOWMENT — itemized**: the same county scored for raw gold regardless
  of claim status. High endowment + low stakeable = great gold, all locked
  up (most of Nevada).
- **BEST EVIDENCE** — the county's top open cited-grade mines and dropped
  sites, each one click from flying there.

"▸ ranked county list" gives the top 20; `GOLD-COUNTIES.md` has the top 25
plus **every Idaho county** ranked. Current top: Jackson OR, Okanogan WA,
Custer ID, Snohomish WA, Josephine OR — Idaho's belt is Custer / Blaine /
Lemhi (endowment #1 of all 244) / Valley / Idaho County / Elmore. Cassia is
#69 — gold-thin, as expected. Caveats on every card: the 400 m open test
inherits MRDS coordinate slop; NV/UT/WY dropped-ground undercounts
(truncated closed files); patented land reads "open."

## 7b · GEOPHYSICS — WS7

Two USGS rasters stream live (no processing, no hosting): **MAGNETIC
ANOMALY** (North America compilation — reds high, blues low; epithermal
alteration often destroys magnetite, so hunt discrete lows on structure)
and **RADIOMETRIC K** (potassium — adularia is K-feldspar, potassic
alteration lights up). One at a time, opacity slider, best over the SAT
basemap. **SURVEY COVERAGE** is the trust layer: 819 airborne-survey
footprints — hover for year, type (M/R/G/EM), line spacing, altitude,
line-km, because a 1969 8-km regional and a modern drape survey look
identical in a pretty raster. High-res Earth MRI grids: PATCH-PLAN.md.

## 7c · CALIFORNIA (phase 1 → claims + grades live 2026-08-08)

CA is in the state chips (42,771 MRDS sites incl. 24k gold and the full
Hg belt; **312,453 active claims** since 2026-08-08 — closed claims arrive
with the Sept 3 monthly rule). The **cited-grades layer now covers CA**:
60 quote-backed mines from the Mother Lode, Alleghany, Grass Valley,
Randsburg and Weaverville-quad literature (PP 157/172/194, Bulls 430/540),
so ASK queries like "richest unclaimed gold in CA" work, and the county
choropleth scores CA on real rich-open grades (Nevada Co = CA's best at
#17 overall; its staked-then-dropped term stays 0 until closed claims
land — expect it to climb). The engine's CA rules: Coast Ranges Hg
pathfinder emphasis and the Knoxville-type serpentinite+Hg+fault
association — validated by the Clear Lake blind test (`python3
geology_targets.py clearlake`; Wilbur Springs and Knoxville INSIDE targets
#1/#4, Sulphur Bank 0.13 km, McLaughlin 0.54 km). Napa and Yolo recorders
run official online index search (operator-export); see
config/county_portals.json. Full CA campaign status: PATCH-PLAN.md.

## 7d · GEOLOGY (QUAD) — WS10

Open **GEOLOGY (QUAD)** in the sidebar and click **MAP INVENTORY**. At the top
of the modal, **TARGET OVERLAY SWITCHER** has one row for each of the 19
targets. Check a row to enable its selected map: the modal closes and the map
automatically pans/fits to that raster's bounds. Reopen **MAP INVENTORY** to
turn it off or switch targets. Idaho Bonanza and Atlanta intentionally point
to the same Hailey raster, so those two checkboxes and the underlying layer
state stay synchronized. The sidebar lists the 18 unique maps rather than
duplicating Hailey; enabling one there also pans to it.

Use the shared opacity slider to keep structures, workings, grades, and live
claims visible beneath a scan. Open a map's provenance card for its full
citation, year, scale, retrieval date, source/product link, selection note,
and georeference method. A **legend** is a reviewed crop of the map's actual
unit key. A **map preview** is a reduced view of the whole source sheet used
for orientation; it is not a legend and should not be used to decode colors
or symbols. The four seed overlays have legend crops; the 14 ranked-map
selections use map previews.

The inventory remains a research record as well as a switcher. For each of
the top 15 ranked targets and four forced seeds it lists the containing and
adjacent 7.5-minute quads, catalog candidates, format (GIS / GeoTIFF / scan /
unpublished), selection status, and notes. Its status words are deliberate:

- **ready** — georeferenced asset is published and can be toggled;
- **processing / built-awaiting-upload** — reviewed local products exist, but
  their S3/CloudFront objects have not yet been verified and promoted;
- **cataloged** — a source is known, but no reviewed live warp is promised;
- **review** — an irregular plate or weak fit needs manual control-point
  review and is withheld from the trusted live set;
- **blocked** — acquisition or publication cannot proceed yet; the deployed
  18-layer set has no blocked layer;
- **gap** — no qualifying geologic map at 1:62,500 or larger was found in
  the searched catalogs; this means “not found,” not “no useful geology.”

All NGMDB selections use the exact bounds in their KML GroundOverlay. The
pipeline verifies the raster member, checks rotation, and requires every
associated target coordinate to fall inside the image footprint before the
layer can be selected. Standard 7.5-minute sheets otherwise snap to official
quad corners. Pocket plates are extracted at their native resolution.
Anderson Plate XVIII and Johnston PP 194 Plate 1 embed native 400-ppi images;
provenance retains that value, and the generated 600-ppi output is explicitly
labeled as a resample that adds no source detail. Irregular plates use
reviewed control metadata and stop before `ready` when confidence is
insufficient. DWM-193's real GIS database also feeds the WS6 unit schema and
a 24k-resolution rescan instead of using raster pixels as the analytical
layer.

Seven ranked target selections carry an explicit scale/fallback warning:

- Willow Creek/Pearl uses the 1:125,000 Boise folio while IGS P-41 at
  1:48,768 awaits a reviewed georeference;
- Azurite uses the target-containing 1:100,000 Robinson Mountain map;
- New Trail uses a 1:100,000 Ivanpah surficial map rather than implying
  detailed bedrock coverage;
- Excelsior uses 1:250,000 Dillon while the 1:31,680 Bannack–Grayling I-433
  scan remains the detailed upgrade candidate;
- Mc Grath uses 1:250,000 Medford while DOGAMI GMS-38 at 1:24,000 awaits
  manual control points;
- Idaho Bonanza and Atlanta share 1:250,000 Hailey while target-specific
  OFR 2004-1205 Plate 1 at 1:24,000 awaits a reviewed georeference; and
- Mammoth uses 1:250,000 Challis while IGS GM-45 at 1:100,000 remains the
  finer PDF/GIS upgrade candidate without a public georeferenced raster.

The inventory also retains finer/newer non-georeferenced candidates for
other targets, including Warner and Niagara. A selectable regional fallback
means “verified image footprint and honest scale,” not “best possible map is
already finished.”

The inventory includes a watch-list link for areas such as Grass Valley,
where the modern CGS quad is an explicit gap. Jackson is different: its live
raster comes from the official public NGMDB 4096×4096 georeferenced KMZ, and
its legend is cropped from the NGMDB sheet preview. The original CGS PDF still
uses California's email-delivery/ADA workflow, and native attributed GIS is
not publicly available. The project owner waived a separate reuse review for
this academic deployment; CGS/NGMDB attribution is retained, but no open
license is claimed. Jackson therefore has no vector rescan. The CGS Jackson
GIS email shown in the outbox remains **draft only**, unsent, and superseded
for raster acquisition; it would request native GIS rather than the already
available raster.

## 8 · WATCH, ASK, dossiers, INTEL

**WATCH** (header; appears once there's a digest or county signals): the
alert feed. Red **ACTIVE→CLOSED** (claim vanished from the active layer —
ground may be opening), green **NEW FILING**, amber **LIKELY LAPSED — verify**
(Sept-1 fee window; only fires against an operator-supplied fee report, never
guessed), teal **COUNTY-RECORDED — NOT IN MLRS**, green **ASSESSMENT FILED
(COUNTY)**. Click an alert → fly + dossier. Hosted, the same digest goes out
by SES email / webhook daily at 13:10 UTC (6-hourly Aug 25–Sep 10).

**ASK** (header): deterministic Q&A over the loaded data. Worth typing:
- "top 10 mines we should go and claim to maximize gold" → ranked stakeable
  picks: cited grade ≥0.3 oz/t + no active claim within 400 m, production
  records outranking one-off assays; each pick opens the quote + dossier
- "which county has the most gold" → the stakeable ranking, with fly-links
- "show me targets" / "sinter" / "epithermal" → WS6 targets
- "open sections" / "was claimed now open" / "split estate" → section grid
- "richest gold in idaho" / "highest grade silver unclaimed" → cited grades
- "watch alerts" → the current digest
The AI chip (hosted) handles out-of-distribution questions via Bedrock.

**Dossiers**: any claim popup → **📋 DOSSIER**. Serial, status, type, acres,
sections from the legal description, county-recorder instruments (WS5
matches, with confidence), automated history sweep (Chronicling America ·
Google Books · MSHA, each hit dated + linked), and GO DEEPER links: MLRS
serial register (claimant names + addresses live there), The Diggings,
Mindat, full-text Mining & Scientific Press / E&MJ searches, recorder + SoS.
Mines with cited grades get the same treatment via their popups.

**INTEL**: monthly top-10 regional developments. **READING**: source library.
**ABOUT / SOURCES**: every dataset, count, and method on one screen.

---

## 9 · Operating it (pipelines · RUNBOOK)

The existing data pipelines are cached and idempotent (`pipelines/cache/`)
and mostly stdlib-only. WS10 raster preparation is the exception: it uses
Pillow, NumPy, tifffile, Fiona, pyproj, and Shapely plus Poppler's
`pdftoppm`/`pdfimages`. The current equivalent requires no GDAL command-line
tools. AOI pipelines remain config-driven by `pipelines/config/aoi.json`
(add a county entry + run with `AOI=<key>` to spin up a second AOI).

| Task | Command |
|---|---|
| County recorder cycle (WS5) | `python3 pipelines/county_records.py` (add `--demo` to test) |
| Geology + targets refresh (WS6) | `python3 pipelines/fetch_geology.py && python3 pipelines/geology_targets.py` |
| County gold ranking | `python3 pipelines/county_gold.py` |
| WS10 map inventory | `python3 pipelines/geology_quads.py` |
| WS10 build one raster | `python3 pipelines/prepare_quad_geology.py --download --skip-vector --only <layer-id>` (outputs `processing` / `built-awaiting-upload`) |
| Upload + promote one WS10 raster | `cd infra && WS10_UPLOAD_DRY_RUN=1 bash deploy.sh upload-ws10-assets && bash deploy.sh upload-ws10-assets`; verify its remote COG/tiles/legend-or-preview, then `cd .. && python3 pipelines/prepare_quad_geology.py --mark-ready <layer-id>` |
| Reclaim local disk after promotion | `python3 pipelines/prepare_quad_geology.py --evict-ready-local <layer-id>`; only remotely verified ready assets can be evicted, and the S3 copy plus checksums remain authoritative |
| Final WS10 metadata/UI validation + deploy | `python3 pipelines/geology_quads.py && /Users/matthewlew/miniconda3/bin/python pipelines/validate_quad_geology.py --skip-assets && bash infra/deploy.sh update-site` after all 18 layers passed their pre-eviction checks |
| Claims / PLSS / land status / open ground | `fetch_claims_aoi.py` → `fetch_plss.py` → `fetch_landstatus.py` → `open_ground.py` |
| Dossiers + history sweep | `webscrub.py` → `dossier.py` |
| Permanent layer from a file | drop in `data-inbox/` → `inbox_ingest.py` |
| Deploy | `git push` → `cd infra && bash deploy.sh` (check footer build) |

Refresh cadence that matters: claims nightly (Lambda, hosted) or on demand;
county records whenever you get an export; geology ~yearly; county gold after
any claims refresh. September: upload the MLRS fee CSV (RUNBOOK §4) so the
fee-window scan can flag lapses honestly.

Full docs in the repo: `RUNBOOK.md` (including the WS10 build/upload order),
`ASSUMPTIONS.md` (judgment calls and confidence semantics), `COUNTY-COVERAGE.md`,
`GOLD-COUNTIES.md`, `DEPLOY.md`, `DEMO.md`, `GRADES-PLAN.md`.

## 10 · A workflow that uses all of it

1. **GOLD SIGNAL** choropleth → pick a county (say Custer) → card → fly to
   its best open cited-grade mine.
2. Toggle **cited grades** + **USMIN workings** + **active claims** there;
   read the grade quotes; check nothing active sits on it (live boundaries
   at z≥10.5).
3. In Cassia: **TARGETS** + **SECTION STATUS GRID** — find tier-colored
   geology over green/amber sections (the ★ money combination when tier ≤2).
4. Open the **dossier** of anything interesting — history, recorder links,
   serial register for claimant names.
5. **COUNTY RECORDS** → send the records request; when the export lands,
   re-run WS5 — new locations near your area show up in **WATCH** before
   they ever reach MLRS.
6. Verify land status at BLM + the recorder. Then go hike it.
