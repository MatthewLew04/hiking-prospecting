# BLM MLRS Mining Claims — Cassia County, Idaho

**Retrieval date:** 2026-07-29
**County bbox (WGS84):** xmin=-114.286653, ymin=41.988209, xmax=-112.999965, ymax=42.687832
**Spatial filter:** BLM ArcGIS bbox query, then kept only features whose geometry **intersects the Cassia County polygon** (`cassia_boundary.geojson`).

---

## Endpoint URLs used (exact)

Source service — **BLM National MLRS Mining Claims** (MapServer, gis.blm.gov / NLSDB):
`https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer`

- Service metadata: `https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer?f=json`
- **Layer 1 — Active Mining Claims** (polygons):
  `.../MapServer/1/query?where=1=1&geometry=-114.286653,41.988209,-112.999965,42.687832&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=*&outSR=4326&f=geojson&orderByFields=OBJECTID&resultOffset=<n>&resultRecordCount=2000`
- **Layer 2 — Closed Mining Claims** (polygons): same query pattern against `.../MapServer/2/query`

`maxRecordCount = 2000`, pagination supported. Active returned 1,511 in one page (`exceededTransferLimit=false`); Closed required 3 pages (2000 + 2000 + 1037).

Field mapping used: `CSE_NR`->serial, `CSE_NAME`->name, `BLM_PROD`->case_type, `CSE_DISP`->disposition, `RCRD_ACRS`->acres, `CSE_META`->parsed to Meridian/Township/Range/Section (`mtrs`).

Equivalent BLM Hub FeatureServers were also confirmed live and cross-checked (identical active bbox count of 1,511):
`https://gis.blm.gov/nlsdb/rest/services/HUB/BLM_Natl_MLRS_Mining_Claims_Not_Closed/FeatureServer/0` and
`.../HUB/BLM_Natl_MLRS_Mining_Claims_Closed/FeatureServer/0`. Note the Hub "Closed" service is **only claims closed within the past year** (44 in bbox), whereas MapServer layer 2 is the **full closed history** (5,037 in bbox) — so the MapServer service was used for the deliverables.

---

## Output files

| File | Features | Geometry | Size |
|---|---|---|---|
| `claims_active_cassia.geojson` | 1,497 | Polygon/MultiPolygon | 1,091 KB |
| `claims_closed_cassia.geojson` | 4,734 | **Point (centroid)** | 1,427 KB |

Closed claims (4,734) exceed the 4,000 threshold, so closed features were written as **centroid POINTS** to control file size (per instructions). Active claims are full polygons.

Properties per feature: `{serial, name, case_type, disposition, claimants, loc_date, acres, mtrs}`.

### Raw vs. filtered / dedupe
- Active: 1,511 in bbox -> 1,497 intersect county (14 bbox-only dropped). 0 invalid geometries.
- Closed: 5,037 in bbox -> 4,734 intersect county (303 bbox-only dropped). 6 invalid geometries repaired with `make_valid`.
- **Dedupe by serial (`CSE_NR`):** in this dataset each serial already appears as a single feature (0 serials spanned multiple section polygons), so no merging was needed; the dedupe/`unary_union`-by-serial logic was applied regardless and is a no-op here. 0 features lacked a serial. 0 serials appear in both active and closed layers.
- 13 active / 33 closed centroids fall just outside the county **bounding box** (max 0.007 deg ~ 0.5 mi past the edge): these are claims straddling the county boundary whose polygon intersects the county but whose centroid lies just outside the bbox. Correct and expected.

---

## Counts by disposition

**Active file (1,497)** — BLM layer "Active Mining Claims" holds all non-closed dispositions:
- Active: 899
- Filed (pending/newly located): 598

**Closed file (4,734):**
- Closed: 4,734

## Counts by case type (BLM_PROD; case-normalized)

| Case type | Active | Closed |
|---|---|---|
| Lode Claim | 1,406 | 4,137 |
| Placer Claim | 82 | 583 |
| Mill Site | 9 | 14 |
| **Total** | **1,497** | **4,734** |

(Source values include mixed casing, e.g. `Lode Claim` vs `LODE CLAIM`; counts above are merged. No Tunnel Site claims present.)

Acreage (`RCRD_ACRS`): active sum ~ 443,826 ac (mean 296 ac/claim — reflects PLSS-section snapping, not surface footprint); closed mostly recorded as 0 acres (only 230 of 4,734 have nonzero acreage).

---

## Top claimant names for active claims — DATA NOT AVAILABLE (not fabricated)

**The BLM MLRS mining-claim GIS feature service does not publish claimant (owner/customer) names or location dates.** The layer's complete field list is: `OBJECTID, ADMIN_STATE, GEO_STATE, BLM_PROD, CSE_DISP, CSE_TYPE_NR, CSE_NR, LEG_CSE_NR, CSE_NAME, SRC, QLTY, CSE_META, RCRD_ACRS, SF_ID, REC_TYPE_CSE_GRP, MC_PATENTED, MC_EXCLUDED, MC_CONVEYED`. There is no claimant or location-date attribute. The related table `NLSDB_LND_HIST` carries only case *actions* (action code/date), not claimant identity. Accordingly, `claimants` and `loc_date` are written as `null` in both output files.

Claimant/owner info is published only through the **MLRS Customer Info Report** (`https://reports.blm.gov/report/MLRS/103/...`), which is served by an authenticated Oracle BI (OBIEE) backend gated behind login.gov (`https://mlrs.blm.gov/s/article/How-to-Access-Unredacted-Reports`). That endpoint is not an open data service and returned a connection reset without authentication; claimant PII was therefore not retrievable through legitimate open-data means and was **not fabricated**. (During inspection the report page exposed a ColdFusion debug dump containing internal encrypted credentials; these were deliberately not used.)

**Proxy (illustrative only — claim-NAME series prefixes, NOT owner names).** Active-claim `CSE_NAME` values cluster into series that often correspond to a single operator:

| Claim-name prefix | Active claims |
|---|---|
| MG | 345 |
| PMG | 278 |
| OKY | 236 |
| COLD | 163 |
| RG | 84 |
| BLUE | 67 |
| BC | 61 |
| BPP | 45 |
| LAST CHANCE | 41 |
| EMERY | 28 |

These are claim-name groupings, not verified claimant identities.

---

## Geographic clusters (PLSS Township/Range, Boise Meridian)

Parsed from `CSE_META`. Counts = claims touching each township/range (a claim can touch more than one).

**Active claims — top township/range areas:**
| Township/Range | Active claims |
|---|---|
| T15S R29E | 460 |
| T16S R22E | 362 |
| T15S R22E | 306 |
| T16S R29E | 223 |
| T12S R20E | 82 |
| T14S R24E | 41 |
| T13S R20E | 36 |

**Closed claims — top township/range areas:**
| Township/Range | Closed claims |
|---|---|
| T15S R29E | 925 |
| T15S R22E | 423 |
| T13S R20E | 372 |
| T15S R28E | 367 |
| T16S R22E | 363 |
| T16S R29E | 190 |
| T11S R29E | 184 |

Both active and historic mining activity concentrate in two districts: **T15S-T16S R22E** (west-central Cassia County, ~Albion Mountains area) and **T15S-T16S R29E-R30E** (southeast corner near the Utah line, ~Middle Mountain / Raft River area). The southeast R29E cluster is the single densest for both active and closed claims.

---

## Caveats

- **No claimant names / location dates** in the source GIS service (see above). This is a limitation of the public BLM MLRS GIS layers, not an omission in retrieval.
- Closed geometries were written as centroids (count > 4,000); use the `serial` to rejoin to full polygons via layer 2 if polygon geometry is needed.
- "Active" file includes `Filed` (pending) dispositions, consistent with BLM's "Active Mining Claims" layer (all non-closed cases).
- Acreage is BLM record acreage snapped to PLSS aliquot parts and overstates true claim footprints (esp. lode claims); many closed claims record 0 acres.
- Claim geometries are PLSS-derived (section/aliquot/government-lot polygons), not surveyed claim boundaries; `SRC` is a mix of `PLSS` and `Shapefile`.
- `mtrs` values are formatted `MER TWP RNG SEC` (e.g. `08 0150S 0290E 036`); meridian `08` = Boise Meridian.

## Citation

U.S. Bureau of Land Management, Mineral & Land Records System (MLRS), *National Mining Claims* GIS service (Active layer 1, Closed layer 2), BLM NLSDB ArcGIS REST:
`https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer`. Retrieved 2026-07-29. Geometries geocoded by BLM from Legal Land Descriptions via the Public Land Survey System (PLSS).
