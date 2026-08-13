# DEMO SCRIPT — proving WS1–WS4 on Cassia County

Five minutes, in order. Works on the deployed site or locally
(`python3 tools/range_server.py 8000` → http://localhost:8000). PMTiles
requires HTTP byte ranges, so the standard-library static server is not a
supported preview path.

## WS1 — universal ingest (the acceptance test)

1. Open the map, sign in.
2. Drag `demo/messy_cassia.csv` from Finder onto the map. The drop overlay
   flashes; the **MY DATA** panel gains "messy_cassia.csv (5)".
3. The map flies to the new layer. Six rows went in; five landed:
   - **Journal adit** — only location field is the text
     `"T12S R22E Sec 14, Cassia ID"` → geocoded via PLSS into **section 14,
     T12S R22E** (click it: the popup shows `geocoded via plss → T12S R22E
     Sec 14` and the section-centroid caveat). This is the spec's
     acceptance test. ✔
   - **Basin creek placer** — plain lat/lon columns.
   - **Shaft (grandpa's topo)** — UTM easting/northing + zone column.
   - **Sunset group / Marion No. 2** — two more PLSS spellings
     ("T. 13 S., R. 22 E., Sec. 5" and "T15S R23E sec 22").
   - **Mystery workings** — "somewhere up the canyon" → honestly reported
     as 1 row not geocoded (ingest log, bottom of MY DATA).
4. Same file also works headless: `python3 pipelines/inbox_ingest.py` on
   anything in `data-inbox/` (that's how this file was first verified).
5. Try an XLSX/KMZ/GPX/zipped shapefile — same flow. Layers persist in the
   browser (IndexedDB); ⇩ exports clean GeoJSON.

## WS2 — open ground + section grid

1. Sidebar → **OPEN GROUND — CASSIA** → toggle **SECTION STATUS GRID**.
   The map flies to Cassia; 1,889 sections render:
   green = open-with-history (54), amber = was-claimed-now-open (222),
   red = actively claimed (101), purple = withdrawn (19), gray = non-federal.
2. Click a green section (e.g. T12S R27E) — the popup *shows its work*:
   historic workings count and names, "no active claim touches this
   section", surface agency, and the not-a-title-search disclaimer.
3. Click an amber section — closed serials are listed, each linking to a
   dossier. Click a red one — the active serials that cover it.
4. Note the split-estate flag on sections with mineral segregations.

## WS2d — expiration watch

1. Header **WATCH** button → the national digest panel (generated only after
   all 19 federal claim-state snapshots complete, with Alaska DNR shown as its
   separate system; also emailed if SES is configured). Each alert: state, serial,
   claimant-name link path, TRS from the legal description, evidence line,
   and a deep link that flies the map to the claim.
2. Force a run: `bash infra/deploy.sh watch` (first run seeds, second
   diffs). Seasonal fee-window scan: `bash infra/deploy.sh watch seasonal` —
   without the fee CSV uploaded it *says* fee data was unavailable
   (never fakes a lapse flag); RUNBOOK §4 shows the 2-minute upload.
3. Open `…/data/alerts/latest.json` — the two-system national digest. Its
   per-state rows distinguish `unknown`/`null` from a complete zero. Private
   statewide snapshots remain below the non-public `watch/` prefix.
4. For a DONE-gate review, use the content-addressed state artifact returned
   in the invocation's `release_evidence` map. It binds the state/system counts
   to private snapshot SHA-256 values; Alaska contains both federal and DNR
   systems or no new release-evidence set is emitted.

## WS3 — dossiers

1. Click any gold-ramp grade dot in Cassia (e.g. Silver Hills, Black Pine
   district) → **📋 DOSSIER**. Facts each carry source + retrieval date;
   the GO DEEPER list prefills: MLRS serial register, Cassia County
   Recorder (phone/address/what to ask for), Idaho SoS business search,
   Mindat, Chronicling America, HathiTrust.
2. Click any claim dot → **📋 DOSSIER** — serial, status, type, acreage,
   and the exact sections from its legal description, plus the same
   research links. Address-staleness note at the bottom.
3. MSHA cross-refs appear automatically where a Cassia mine has a federal
   mine ID.

## WS4 — historic web scrub

1. In a dossier with history (Black Pine group, Vipont area names), the
   **HISTORY — AUTOMATED SWEEP** timeline lists dated newspaper hits
   (Chronicling America) and book hits, chronologically, each a citation
   link to the page image.
2. Rebuild anytime: `python3 pipelines/webscrub.py` (cached, polite,
   idempotent) then `python3 pipelines/dossier.py`.
3. `site/data/history/cassia.json` is the raw dedup'd corpus —
   `byName` keyed on canonical name variants.

## Provenance spot-check (any workstream)

Every derived datum traces back: open-ground popups name their inputs;
dossier facts carry `source · retrieved`; history hits carry the query URL;
pipeline caches store the fetch URL beside every response
(`pipelines/cache/*.meta`).
