# USGS Mine Data for Cassia County, Idaho (FIPS 16031) — Retrieval Summary

Prepared 2026-07-29. All coordinates WGS84 (EPSG:4326). County boundary source: `cassia_boundary.geojson`
(bbox -114.286653, 41.988209 to -112.999965, 42.687832).

## Output files

| File | Features | Geometry |
|---|---|---|
| `mrds_cassia.geojson` | 249 | Point |
| `usmin_points_cassia.geojson` | 279 | Point |
| `usmin_polys_cassia.geojson` | 49 | Polygon |

All features verified: numeric coordinates, every vertex inside the county bbox padded by 0.03 deg
(0 out-of-bounds in all three files).

## GOAL 1 — USGS MRDS (Mineral Resources Data System)

### Downloads / queries used
- Full flattened dump (primary source): `https://mrdata.usgs.gov/mrds/mrds-csv.zip`
  (25,791,223 bytes; server file date 2022-08-23; contains `mrds.csv`, 304,632 records, 46 columns).
- Relational tables (used only to add `district` and to obtain unambiguous per-commodity rows):
  `https://mrdata.usgs.gov/mrds/rdbms-tab-all.zip` (118 MB; tables dated 2019-03-06; used
  `Districts.txt` and `Commodity.txt`).
- Catalog page consulted for available products: `https://mrdata.usgs.gov/mrds/`.

### Selection method
1. Attribute selection from `mrds.csv`: `state` contains "Idaho" AND `county` = "Cassia" -> 243 records.
2. Spatial selection: point-in-polygon test (shapely) of every MRDS record with valid lat/lon against the
   Cassia County boundary polygon -> 248 records.
3. Union of both sets, deduplicated on `dep_id` -> **249 unique records** (all have valid coordinates).
   - 1 record is attribute-only (outside polygon): dep_id 10089123 "General Grant Placer", plotted ~1 km
     north of the boundary (Snake River/Lake Walcott shoreline) but attributed to Cassia County.
   - 6 records are polygon-only (county attribute names a neighboring county): 10048481 "Minidoka"
     (Blaine), 10070326 "Snake River Placers" (Minidoka), 10154555 "U-30-3" (Box Elder, UT),
     10216689 "Big Southern Butte" (Butte), 10265681 "Arco" (Butte), 10290455 "Broken Arrow Ranch" (Power).
     The first two sit on the Snake River county line; "Big Southern Butte" and "Arco" appear to be
     MRDS coordinate errors (their namesake localities are in Butte County, ~100 km north, but the
     stored coordinates fall inside Cassia County). Included per the union rule; flagged here.

### GeoJSON properties
`id` (dep_id), `name` (site_name), `dev_status` (dev_stat), `commodities` (comma-joined, ordered
Primary -> Secondary -> Tertiary from the relational Commodity table), `commod_primary` (Primary-importance
commodities only), `ore`, `gangue`, `work_type`, `prod_size`, `district` (from Districts.txt; "; "-joined
when multiple), `url` = `https://mrdata.usgs.gov/mrds/show-mrds.php?dep_id=<ID>`.

### Development-status breakdown (249 records)
| dev_status | count |
|---|---|
| Occurrence | 85 |
| Unknown | 71 |
| Past Producer | 60 |
| Producer | 19 |
| Prospect | 14 |

### Top 12 commodities (count of records listing each commodity, any importance)
Counted from the relational Commodity table, so commodity names that contain commas
(e.g. "Sand and Gravel, Construction") are counted as single commodities.

| Commodity | Records |
|---|---|
| Sand and Gravel, Construction | 89 |
| Geothermal | 41 |
| Gold | 31 |
| Lead | 30 |
| Silver | 21 |
| Copper | 21 |
| Uranium | 13 |
| Zinc | 13 |
| Iron | 12 |
| REE | 11 |
| Titanium | 10 |
| Garnet | 10 |

(Next: Pumice 8, Zirconium 7, Clay 6.)

### Notable named mines (all "S" = small production; no Medium/Large producers in the county)
- **Black Pine Mine** (Past Producer, Gold-Silver, Black Pine District) — the district's namesake
  open-pit gold mine.
- Black Pine District polymetallic/mercury group: **Silver Hills Mine**, **Ruth Mine**, **Hazel Pine Mine**,
  **Valentine Mine**, **Miller Cinnabar**.
- Stokes District (Albion Mountains) Pb-Ag-Au group: **Melcher Mine**, **Albion Group Mine**,
  **Golden Eagle Mine**, **Big Bertha Group**, **Old Dominion Mine**.
- **Excelsior** (listed as Producer; Ag-Au-Cu-Pb-Zn).
- **City of Rocks Mine** (Feldspar-Uranium-Beryllium-Thorium, Almo Area).
- **Oakley Valley Stone, Inc.** and **Rocky Mnt Quartzite Quarry** (the well-known Oakley dimension-stone
  quartzite quarries).
- Numerous Snake River gold placers (Lake Walcott / Burley / Acequia areas) and 3 dimension-marble
  quarries (Albion / Declo / Oakley areas).

## GOAL 2 — USGS USMIN Prospect- and Mine-Related Features (topographic map symbols)

### Downloads / queries used
- Landing page: `https://mrdata.usgs.gov/usmin/` (mrdata serves a 17-western-state copy with per-state
  files and WFS `https://mrdata.usgs.gov/services/wfs/usmin`; the WFS was used as a cross-check —
  a GetFeature `resultType=hits` request on the county bbox returned 330 points, consistent with 279
  in-polygon + bbox margin).
- Authoritative data release (used for the outputs), found via the ScienceBase catalog API
  (`https://www.sciencebase.gov/catalog/items?q=USMIN Idaho&format=json`):
  ScienceBase item **5a1492c3e4b09fc93dcfd574** — *Prospect- and Mine-Related Features from U.S.
  Geological Survey 7.5- and 15-Minute Topographic Quadrangle Maps of the United States (ver. 10.0,
  May 2023)*. File downloaded: `USGS_TopoMineSymbols_ver10_Shapefiles.zip` (265,690,118 bytes) via
  `https://www.sciencebase.gov/catalog/file/get/5a1492c3e4b09fc93dcfd574?f=__disk__12%2F47%2F4b%2F12474b2b284d1219d5e0493fb2468137005bc4be`
- The zip contains national shapefiles by map-series scale: `..._24k_Points/Polygons`,
  `..._48k_Points/Polygons`, `..._625k_Points/Polygons` (GCS WGS 1984).

### Clipping method
Every point tested with shapely point-in-polygon against the county boundary; polygons kept when they
intersect the county polygon (none extended beyond the padded bbox, so no geometry needed cutting).
Results merged across scales, `topo_scale` retained:

- Points: 24k series 250, 62.5k series 29, 48k series 0 -> **279 points**.
- Polygons: 24k series 49, others 0 -> **49 polygons**.
- Every clipped feature's own `county` attribute = "Cassia" (independent confirmation of the clip).

### GeoJSON properties (original values kept)
`ftr_type`, `ftr_name`, `topo_name`, `topo_date` (map year), `topo_scale`, `state`, `county`, `remarks`,
`gda_id`, `scanid`, and for points `ftr_azimut` (symbol azimuth).

### Feature-type breakdown
Points (279): Prospect Pit 91, Gravel Pit 85, Borrow Pit 41, Quarry 25, Adit 14, Mine Shaft 7,
Open Pit Mine 7, Gravel/Borrow Pit - Undifferentiated 6, Sand Pit 3.

Polygons (49): Gravel Pit 30, Borrow Pit 14, Disturbed Surface 2, Sand Pit 2, Quarry 1.

Source topo map dates range 1958-2001. Only 8 points carry a feature name (e.g. Myers Mine,
Melcher Mine, Silver Hills Mine, Ruth Mine, Tolman Mine, Hazel Pine Mine, Worthington Mine).

## Caveats
- **MRDS is a legacy database**: USGS ceased systematic updates in 2011; the CSV dump is the 2022-08-23
  build and the relational tables are the 2019-03-06 build. Recent operations (post-2011) are not
  reflected. `prod_size` uses historical single-letter codes (here S = Small 28, N = No production 21,
  200 blank); meanings per the MRDS metadata have varied over time.
- Some MRDS commodity names contain commas (e.g. "Sand and Gravel, Construction",
  "Stone, Crushed/Broken", "Marble, Dimension"), so naive comma-splitting of the `commodities` string
  over-splits; the breakdown above used the relational per-commodity rows.
- Only 49/249 MRDS records have a mining-district entry and 49/249 have `prod_size`; `ore`/`gangue`
  are sparsely populated.
- Two included MRDS records ("Arco", "Big Southern Butte") are probable MRDS coordinate errors —
  attributes point to Butte County localities but coordinates fall inside Cassia County (see above).
- USMIN features are digitized symbols from historic topo maps (1958-2001 here): locations reflect the
  map, not field verification; the same physical feature can appear in both the 24k and 62.5k series
  (29 of the 279 points are from 62.5k/15-minute quads), and features may no longer exist on the ground.
- USMIN polygons for this county exist only in the 24k series; none required clipping at the boundary.

## Citations
- U.S. Geological Survey, 2005 (data current as of 2011; files rebuilt 2019/2022), *Mineral Resources
  Data System (MRDS)*: U.S. Geological Survey, Reston, Virginia. https://mrdata.usgs.gov/mrds/
- Horton, J.D., and San Juan, C.A., 2023, *Prospect- and mine-related features from U.S. Geological
  Survey 7.5- and 15-minute topographic quadrangle maps of the United States (ver. 10.0, May 2023)*:
  U.S. Geological Survey data release, https://doi.org/10.5066/F78W3CHG (ScienceBase item
  5a1492c3e4b09fc93dcfd574).
