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
