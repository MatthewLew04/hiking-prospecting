# National baseline publication truth

WS11 distinguishes source availability from implemented delivery and from a
reviewed state release. Registering a national agency URL does not make a
source browser-visible, ingested, immutable, or DONE-gate evidence.

## Current delivery matrix

| Baseline | Browser delivery now | Provenance and acceptance | Publication truth |
|---|---|---|---|
| Federal MLRS | Compatibility `claims.pmtiles` plus a live active-claim viewport query | Compatibility counts and partial-state metadata are checked; the live query is not a closed-claim archive | The compatibility archive covers CA, ID, MT, NV, OR, UT, WA, and WY only; NV, UT, and WY are partial and federal AK is absent. It is not the 19-state release. |
| Alaska DNR state claims | Separate ordinary and precision-overflow polygon PMTiles archives; ordinary display activates at z8 and precision at z19 | Manifest source counts, complete semantic scans, maximum-zoom top-level IDs, and exact disjoint source-OID/feature-ID union | The base preserves 39,263 active, 51 pending, and 79,462 closed polygons; precision preserves 6 active and 18 closed unchanged polygons, for an exact combined 39,269/51/79,480 source inventory. It never substitutes for the still-missing federal Alaska MLRS artifact, and it does not make Alaska DONE. |
| Alaska ARDF | One Alaska PMTiles point layer | Corrected archive SHA-256 `2e576908351dcd344b6503e84d578dd20b6cf92fb00e9ed32a123f022bcb159a`, all 12 required browser properties, and exact 7,692-source-ID reconciliation | Complete at the recorded retrieval. Official source blanks are retained as `Not reported by source` with a paired `source_blank` status, never guessed or silently omitted; this is Alaska's occurrence backbone, not claim tenure. |
| Official state-survey baselines (NV, AZ, CO, UT) | Atomic per-state PMTiles groups loaded lazily from manifest descriptors and searched only in currently loaded tiles | Builder-owned exact source inventories, clipping/repair/exclusion evidence, required properties, maximum-zoom IDs, archive bytes/hashes, state filters, registry pointers, and browser lifecycle checks | All remain `baseline_not_release`. Utah's four accepted archives expose 22,635 Map 179DM geology units, 67,571 Map 179DM structures, 19,232 DS-7 faults, 185 OFR-695 districts, and 7,787 OFR-757 UMOS records. Baseline visibility does not make any state DONE or release-enabled. |
| MRDS | One 49-state PMTiles point layer; viewport search/query sees loaded tiles | Artifact hash, state counts, rendered features, and exhaustive maximum-zoom source-ID reconciliation | All 265,702 current normalized IDs are bound to the exact archive fingerprint and rescanned by the progress gate; the hardened builder applies the same gate before future publication. |
| USMIN | One 49-state PMTiles point layer; viewport search/query sees loaded tiles | Snapshot count/OID bounds, artifact hash, rendered features, and exhaustive maximum-zoom source-ID reconciliation | All 570,484 current snapshot IDs are bound to the exact archive fingerprint and rescanned by the progress gate; the hardened builder applies the same gate before future publication. |
| SGMC + Alaska SIM 3340 | National geology PMTiles snapshot | Per-feature source URL/scale plus two deterministic builds and exhaustive source/z12 `fid` reconciliation | The current 559,279-feature archive preserves every source ID. Macrostrat remains a live optional map, not an immutable gap-fill artifact in this snapshot. |
| Qfaults | Included with SGMC/SIM 3340 in the national faults PMTiles snapshot | Official ZIP retrieval provenance, per-feature source/scale, two deterministic builds, and exhaustive source/z12 `fid` reconciliation | The current 500,743-feature archive preserves every source ID. One 5.25 mm Qfault trace has an explicit checksum-bound 2.224 mm tile-quantization normalization; no feature is dropped. |
| Macrostrat | Remote vector tiles | Browser toggle and feature popup only | It is not copied into national PMTiles, not represented as used gap-fill in the current snapshot, and not live-probed by browser acceptance. |
| National magnetic anomaly | Remote USGS WMTS raster | Browser lifecycle and reviewed URL are tested with a deterministic request stub | Browser-visible but externally hosted; CI does not prove current upstream availability or immutable raster bytes. |
| Airborne survey index + Earth MRI blocks | One national PMTiles footprint layer | Exact ArcGIS ID snapshot, full max-zoom ID reconciliation, artifact SHA-256, and browser render acceptance | These are survey/acquisition footprints and provenance, not a bundled national high-resolution raster collection. |
| PLSS legal geocoder | Live BLM CadNSDI ArcGIS query | Identity and ambiguity behavior use mocked browser fixtures | National lookup is network-dependent and applies to PLSS states. Published PLSS polygons remain AOI-scoped; there is no national PLSS PMTiles artifact. |
| BLM Surface Management Agency | Registered source; AOI/private staging producer input only | Source registration and producer tests do not constitute browser acceptance | There is no national SMA manifest entry, standalone browser layer, or published PMTiles artifact. Surface management is never mineral title. |
| PAD-US | Registered source only | Candidate provenance for future reviewed land-context adapters | There is no PAD-US producer, manifest entry, browser layer, or published PMTiles artifact. |
| e-AMLIS | Registered source only | National reference for state-by-state AML review | The generic non-claim publisher does not fetch e-AMLIS. There is no national e-AMLIS manifest entry, browser layer, or published PMTiles artifact. |
| Chronicling America | AOI-built JSON corpus | Source citations exist in the AOI corpus; no 49-state coverage gate | The source is national, but automated public ingestion is currently AOI-only. |
| EDGAR / SEDAR+ | External landing links | Link construction only | No filing search results are ingested, indexed, or provenance-stamped. These integrations are `link_only`. |

The canonical source catalog uses these delivery labels:

- `pmtiles`: an actual browser tile artifact exists.
- `remote_vector_tiles`: the browser reads an external vector-tile service.
- `remote_arcgis_query`: the browser performs a live ArcGIS lookup; no national
  tile artifact is implied.
- `aoi_ingest`: automated results exist only for configured AOIs until an
  explicit national inventory says otherwise.
- `registered_source`: an official source is recorded for provenance or future
  producer work, but there is no corresponding browser delivery.
- `link_only`: the integration opens an external research portal and has not
  ingested results.

## Lossless national point/claim builds

The MRDS, USMIN, and production federal MLRS builders use stable nonnegative MVT
top-level IDs: MRDS `dep_id`, USMIN `OBJECTID`, and a deterministic MLRS hash of
system, state, status layer, and serial. MLRS IDs are capped to JavaScript's
safe-integer range and are collision-checked across both layers.

Their Tippecanoe commands fix the base zoom to the declared maximum and use
`--no-feature-limit --no-tile-size-limit`. This preserves every source record
at maximum zoom while allowing normal deterministic sampling below the base
zoom for overview-map budgets. A build succeeds only when a full decoded scan
finds exactly the normalized unique source-ID set at maximum zoom. Tile buffers
can duplicate point instances along seams, so raw instance counts are recorded
and must not be smaller than the source count. The manifest or immutable MLRS
pointer records counts and a SHA-256 of the canonical sorted ID list; raw
source-ID arrays remain private build memory.

The current MRDS and USMIN generations carry fingerprint-bound inventories and
the progress validator repeats the full decoded maximum-zoom scan. The same
guards run before a future builder may replace either archive. Federal MLRS
retains this as a future-publication contract until its exact 19-state inputs
exist; no partial compatibility archive is re-labelled as complete.

For federal MLRS, `build_federal_mlrs_inventory.py` compiles the exact private
38-snapshot inventory from producer pagination/clip evidence before the
PMTiles builder runs. It cannot turn a capped pull or checkpoint into a
complete generation by trusting an operator-entered boolean.
