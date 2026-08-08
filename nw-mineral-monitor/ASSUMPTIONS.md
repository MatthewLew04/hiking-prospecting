# ASSUMPTIONS — judgment calls made while building WS1–WS4

Recorded per the working agreement: never block on a question, write the
call down instead. Each entry says what was decided, why, and what to do
if you want it differently.

## Scope & architecture

1. **Extended the existing app, no rewrite.** All four workstreams are new
   modules inside the existing `site/index.html` MapLibre app, new
   `pipelines/` scripts, and new Lambda/EventBridge resources in the existing
   CloudFormation stack. Existing symbology (circles = active/existing,
   dots = old) is untouched; the new section grid renders *under* the point
   layers and is off by default.

2. **AOI is config-driven, Cassia is the default.** `pipelines/config/aoi.json`
   holds bbox, PLSS meridian (`ID08` = Boise), UTM zone, county FIPS, recorder
   contact, and scrub budgets. Adding another county = add an entry, run the
   five pipeline scripts with `AOI=<key>`. The 7-state point/claim layers are
   unchanged; the *section-level* analysis (open ground, watch, dossiers)
   runs on the AOI only — PLSS polygons for all seven states would be
   gigabytes and BLM's server would hate us.

3. **Section-level, not aliquot-level.** MLRS `CSE_META` legal descriptions
   go down to quarter-quarter aliquots; we aggregate at the section (1 sq mi)
   because (a) the acceptance test is section-level, (b) aliquot polygons
   aren't served by CadNSDI's public layer at national scope (the
   "PLSS Intersected" layer is huge and partial), and (c) a research lead
   at section resolution is honest about the underlying data quality.

## WS2 — open ground

4. **Claims map to sections via their legal descriptions, not geometry.**
   `CSE_META` on the MLRS GIS layers carries the exact PLSS legals
   ("ID 08 0140S 0230E 027 A NW|…"). That's authoritative and free — no
   spatial-join uncertainty. A claim whose CSE_META is empty (rare) simply
   doesn't mark sections; its centroid dot still shows on the map.

5. **SMA is the *LimitedScale generalized* service** (the only queryable
   national SMA REST layer). Boundary error can reach a few hundred metres,
   so a section whose centroid sits near an ownership boundary can be
   misclassified. Sections get status from the *centroid* agency. The UI
   disclaimer covers this; a parcel-grade SMA (state geodatabase download)
   is the upgrade path if you ever need it.

6. **"Open to location" = surface agency BLM or USFS**, minus withdrawal /
   segregation / WSA / wilderness overlays and minus NPS/FWS/DOD/USBR/BIA.
   Acquired lands, R&PP leases, and mineral-estate quirks are NOT modeled —
   split-estate is flagged (mineral-segregation polygons) but severed
   private-surface/federal-minerals ground is not detected (needs the BLM
   subsurface-estate dataset; noted as future work).

7. **Closure recency is sparse by necessity.** The public claim layers carry
   NO disposition dates. `Locatables_Case_Disp` yields dates for only ~60 of
   5,037 closed Cassia cases, so "was claimed, now open" colors one bucket
   (amber) instead of a recency ramp, and dated cases show their date in the
   dossier. True closure dates live in the serial register (linked
   everywhere).

## WS2d — expiration watch

8. **ACTIVE→CLOSED detection is snapshot diffing** of the MLRS active layer
   for the AOI (daily). First run seeds state; alerts start on run 2.
   "Disappeared from the active layer" is treated as closed — BLM sometimes
   re-serials cases, so the alert text says "verify the serial register."

9. **LIKELY-LAPSED needs a fee report we cannot scrape.** Fee-payment
   actions live only behind the MicroStrategy app at reports.blm.gov —
   interactive, session-based, no stable data URL. Decision: no scraping of
   walled apps. Instead, the seasonal (Aug 25–Sep 10, 6-hourly) job reads an
   operator-supplied CSV at `s3://<bucket>/watch/fee_status.csv` (export it
   from the MLRS "Mining Claims" public report once at the start of the
   window — 2-minute manual step, RUNBOOK step 8). With the file present,
   active claims with no current-assessment-year fee action are flagged
   LIKELY LAPSED with the mandated "lead, not conclusion" language. Without
   it, the digest says plainly that fee data was unavailable. No fake flags.

10. **SES starts in sandbox mode** — sender AND recipient must be verified
    until AWS grants production access. Runbook covers it; email silently
    skips if the env vars are empty, webhook + on-map alerts still work.

## WS3 — dossiers

11. **Bot-walled sources became prefilled deep links, not scrapes:** county
    recorder (Cassia has no verified public online index; phone + address +
    guidance included), Idaho SoS (Salesforce app), Mindat (403s bots),
    The Diggings, HathiTrust (403s bots), reports.blm.gov (session app).
    Claimant names/addresses therefore come from the linked MLRS serial
    register rather than being cached into our JSON — which also keeps
    stale-address liability out of the dataset. The dossier notes address
    staleness explicitly (spec requirement).

12. **Claim dossiers assemble client-side.** Precompiled dossiers for 6,548
    claims ballooned to 17 MB of repeated link boilerplate; facts already
    ship in `{aoi}_claims.json`. Only the 9 Cassia graded mines get compiled
    dossiers (18 KB). Same rendered result, 1000× smaller.

## WS4 — web scrub

13. **Chronicling America moved.** The classic
    chroniclingamerica.loc.gov/search API 404s (retired 2025); the sweep uses
    the loc.gov collection JSON API at ~0.8 req/s with a 60-day cache.
    OCR is noisy — hits are leads; the UI says so.

14. **Google Books is best-effort:** the shared egress IP here is usually
    429'd (retested 2026-08-06 — still limited). The fetcher retries once,
    caches, and the run proceeds without it. Running `webscrub.py` from any
    residential connection (or Lambda) fills the gap on the next pass —
    idempotent by design. Partial mitigation shipped instead: Internet
    Archive hosts 150 Mining & Scientific Press volumes and 3,800+ E&MJ
    issues with open *browser* full-text search; its cross-collection FTS
    API is not public (metadata backend only; fts params 400), so dossiers
    carry a prefilled `archive.org/search?…&sin=TXT` deep link rather than
    automated hits.

15. **HathiTrust/MSP/E&MJ runs are link-outs** (no public full-text API,
    bot-walled). MSHA is fully automated (their open-data Mines.zip),
    filtered to county FIPS. Idaho AML inventory has no stable public
    download; IDL contact link ships in the reading room instead.

16. **Scrub budget: 60 canonical names** (graded mines first, then MRDS
    named sites, then claim names; junk names like "Gravel Pit" excluded;
    variants collapsed before querying). Config: `webscrub.max_features`.

## WS1 — ingest

17. **PLSS geocoding places rows at the section CENTROID** and records the
    section id + label on the feature. A legal description locates to an
    area, not a point — pretending otherwise would be false precision.
    Aliquot parsing (NW¼SE¼) is not attempted (see #3).

18. **Meridian assumption:** bare TRS text ("T12S R22E Sec 14") in the AOI
    is assumed Boise Meridian (all of Cassia is). The live-CadNSDI fallback
    covers Idaho outside the cache; other-state PLSS in dropped files would
    need a state hint — rows report as un-geocoded rather than guessing.

19. **Browser parsers:** CSV/GeoJSON/KML/GPX hand-rolled or DOM-native;
    XLSX via vendored SheetJS mini (no legacy .xls); KMZ/zipped-SHP via
    vendored JSZip + a minimal shp/dbf reader (point/polyline/polygon,
    2D; Z/M flattened). Odd projections in .prj are NOT reprojected — WGS84
    lon/lat assumed, which covers the GPS/Google-Earth files prospectors
    actually carry. Server twin (`inbox_ingest.py`, stdlib-only) handles
    CSV/GeoJSON/KML/GPX for the `data-inbox/` folder path.

20. **User layers persist in the browser** (IndexedDB), not S3 — no write
    path to the bucket exists from the static site, which is the security
    posture we want. Export-as-GeoJSON is one click; drop the export into
    `data-inbox/` and run `inbox_ingest.py` to make a layer permanent for
    all users.

## Meta

21. **The AI answerer (ASK) doesn't have open-ground/dossier tools yet** —
    the deterministic query engine and the new UI cover the workflows; add
    `query_openground` to `ask_lambda.py` TOOLS + browser executor as a
    future increment.

22. **reports.blm.gov / MLRS case-detail automation** was probed (both serve
    200 but are interactive JS apps). If BLM ever publishes the flat-file
    extracts they've promised for MLRS, `fetch_claims_aoi.py` is where they
    plug in.

## WS5 — county-direct claim extraction (2026-08-06)

23. **Cassia has no online recorded-document index** (verified 2026-08-06 at
    cassia.gov/recorder: vault by appointment, records request to
    recorder@cassia.gov). The adapter therefore runs OPERATOR-ASSISTED:
    the pipeline emits a prefilled records request, ingests whatever
    export/transcription comes back from `data-inbox/county/<county>/`
    (CSV/TSV/JSON, headers sniffed), and never scrapes. Portals that block
    automated verification via robots.txt (iDoc Market et al.) are marked
    `unverified` in the coverage matrix rather than probed around — same
    no-walled-apps posture as WS2d's fee report.

24. **Matching is claim name + TRS, never claimant.** The public MLRS GIS
    carries no claimant names (see #WS3), so county grantor/grantee fields
    corroborate a match in the dossier but don't drive it. Confidence tiers
    HIGH (name ≥.9 + section overlap) / MEDIUM / LOW ride on every match and
    every alert; numbered-series names ("PMG 370" vs "PMG 371") are
    penalized so series claims don't cross-match.

25. **"COUNTY-RECORDED — NOT IN MLRS" is a lead with a clock on it.** State
    law records the location first; FLPMA (43 U.S.C. § 1744) gives 90 days
    to file with BLM, plus adjudication lag. The alert text says exactly
    that. The watch Lambda retires the alert automatically once a same-name
    case appears in the MLRS active layer, and skips county alerts entirely
    when the deployed county file is demo data.

26. **`--demo` is the only path that ingests `demo/county_sample.csv`** —
    synthetic rows (marked in-file) that exercise every doc class and both
    signal kinds against real Cassia serials. A real (empty-inbox) run ships
    with the site so nothing synthetic can be mistaken for a record.

## WS6 — geologic maps + lithology targeting (2026-08-06)

27. **Macrostrat is the vector source of record, by measurement not
    preference.** The spec's order (IGS vector → NGMDB → SGMC → Macrostrat)
    was probed: IGS ArcGIS REST still 502s from this environment (#12-era
    finding), NGMDB vector products are per-quad downloads, SGMC's WFS
    works — and Macrostrat already harmonizes SGMC 1:500k *plus* the IGS
    Twin Falls 30×60 DWM-49 1:100k for this AOI, serving the best available
    scale per area with a per-unit citation. Every unit carries source_id,
    verbatim description, citation, and scale in `data/geology/{aoi}.json`;
    SGMC-WFS remains the coded fallback. Scanned-quad georeferencing
    (rasters) is recorded as the Tier-1 upgrade path, not attempted.

28. **Zero Tier-1 / Tier-2 units in Cassia is a map-scale fact, not an
    engine bug.** No mapped unit at 1:500k/1:100k carries sinter/opaline/
    chalcedony or hydrothermal-alteration language here. The tier regexes
    stay sinter-first (they fire immediately if a finer map ever enters the
    stack); rankings in this AOI are Tier-3 hosts × faults × geothermal ×
    pathfinders × open ground. The UI note and every card say so.

29. **Plain "altered" is excluded from Tier 2 on purpose:** the AOI's only
    "altered" matches are weathered basalt (olivine→iddingsite) in DWM-49 —
    a proven false positive. Tier 2 requires silicif-/jasperoid/argillic/
    propylitic/alunite/adularia/hydrothermally-altered language.
    Travertine/tufa is scored separately at low weight (calcareous — right
    plumbing, wrong chemistry), exactly per spec.

30. **Faults come from the geologic maps (Macrostrat line layer), clipped
    per tile** — seams are irrelevant for distance/intersection math.
    USGS NSHM hazfaults2014 was integrated but contains zero features in
    this bbox (it only models major seismogenic structures); its fetch
    stays in as a supplement for future AOIs. The earthquake.usgs.gov WAF
    403s ArcGIS paging params — that fetch is single-shot by design.

31. **Sentinel-2/ASTER alteration indices (WS6c) are recorded as future
    work**, not shipped: the corroborating-raster value is real, but it
    needs scene selection + cloud masking to avoid painting false
    confidence over 2,600 mi². The target JSON schema already has room
    (`boosts`) for a satellite term.

## CA patch + WS7 (2026-08-07)

32. **The Clear Lake blind test failed first, passed second — both runs are
    the record.** v1 lexicon found zero T1/T2 (no map unit in the AOI says
    "sinter"/"opal" at any published scale) and drowned in mélange noise.
    v2 fixes are each grounded in the maps themselves: silica-carbonate →
    Tier 2 (135 units); mélange block-inventory guard; the Knoxville-type
    ASSOCIATION rule (serpentinite + ≥3 Hg ≤2 km + fault ≤1 km ⇒ Tier 2,
    labeled as association, never dressed up as description-based — needed
    because McLaughlin's corner is SGMC-500k-only and says just
    "serpentine"). Result: Wilbur Springs and Knoxville INSIDE targets #1/#4,
    Sulphur Bank 0.13 km, McLaughlin 0.54 km. See PATCH-PLAN.md.

33. **WS7 rasters stream; nothing is reprocessed.** Magnetic = mrdata
    mapcache WMTS `magnetic` (GoogleMapsCompatible), K% = `aerorad` WMS —
    both verified tile-serving from the pipeline environment. Earth MRI
    high-res GeoTIFF→COG tiling is specced (PATCH-PLAN) but deliberately
    not shipped untested. The trust layer (819 airborne footprints w/ year,
    spacing, altitude) ships because a 1949 5-mile-spacing survey and a
    modern 200 m block look identical in a pretty raster.

34. **Earth MRI WFS GetFeature 400s** on every parameter variant tried
    (GetCapabilities fine) — outlines will come from the ScienceBase
    collection instead. **USGS Qfaults national service still answers
    "Service not started"** — active-fault styling waits; geology-map fault
    arcs (already scored) carry structure meanwhile.

35. **CA claims wiring is schedule-only until first deploy+run.** BBOX +
    nightly/monthly rules ship now; snapshots appear when the rules fire
    (CA active likely chains 2-4 legs; closed capped at newest 250k like
    NV). The CA state chip is live and simply shows nothing until then.

36. **CA cited grades are curated literature extraction, not a database
    dump.** 60 mines hand-extracted 2026-08-08 from PP 157 (Mother Lode),
    PP 172 (Alleghany), PP 194 (Grass Valley), Bull 430 (Randsburg), Bull
    540 (Weaverville quad) — every row carries the verbatim quote + page.
    $/ton → oz/t at $20.67 (all quoted figures predate 1934); PP 194
    states most grades in ounces directly. Bonanza lots (Sixteen to One's
    80-lb $5,000 lot, Rainbow's 1,953-lb $116,337 shipment) convert to
    absurd-looking oz/t on purpose — same convention as the NV bonanza
    rows, and the endowment term caps at 5 oz/t. Coordinates come from
    MRDS CA name-matching inside a 20 km district anchor (57/60 matched;
    Chilena, Mascot, Craig unlocated → documented, unscored). Rebuild any
    time with `python3 pipelines/grades_ca.py` (idempotent — drops CA rows
    and re-splices). Caveat carried in the county notes: patented/park
    ground (Empire State Historic Park!) still reads "open" until the
    withdrawal overlay lands; CA staked-then-dropped stays 0 until
    ca_closed.json (Sept 3 rule, or manual closed pull).

37. **The 2026-08-08 tab crash ("Aw, Snap error 5" + FILE_ERROR_NO_SPACE)
    was RAM + a full disk — NOT the app writing storage.** Code audit:
    the app persists only the auth token (localStorage) and user-imported
    layers (IndexedDB `nwmm-userlayers`); bulk claims/geology were never
    written to browser storage — the LevelDB NO_SPACE lines came from a
    browser extension failing on a full disk. Our real bug: every state's
    claims were built into permanent Feature arrays AND handed statewide
    to the GL worker (with CA live: ~743k active + 1.3M closed features,
    twice over) — on a disk-full Mac (no swap) the renderer gets killed.
    Reproduced headless: the 08-07g build's renderer died at the
    closed-claims toggle; the 08-08b build ran the same script at a
    113–211 MB heap plateau. Fix (build 2026-08-08b): columnar files are
    the only resident copy; pushes are banded (grid-decimated summary
    below z7 with weighted heatmaps, viewport-only detail above, 200k
    cap); layer-off frees the worker index; geojson sources capped at
    maxzoom 12; storage governance (150 MB userlayers budget with
    eviction, legacy-cache purge, quota-safe writes, global
    unhandledrejection handler); `?debug=1` panel shows heap/storage/
    pushed counts live. Server-side vector tiles (tippecanoe→PMTiles on
    S3) remain the tier-2 upgrade in PATCH-PLAN — right architecture,
    not needed to stop the crash. `tools/measure.js` is the Playwright
    harness that produced the numbers.
