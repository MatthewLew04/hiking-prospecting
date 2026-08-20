# Leapfrog Geo starter kit — cassia

Exported 2026-08-18 by `pipelines/leapfrog_export.py`. Everything below is in
**WGS84 / UTM zone 12N (EPSG:32612), meters** — set exactly that CRS when
Leapfrog asks, and every layer lands in the same XYZ space.
AOI bbox (lon/lat): -114.3212 41.9610 -112.9625 42.7078
Elevations: AWS Terrain Tiles (Mapzen terrarium): 3DEP/SRTM/GMTED2010 composite, https://registry.opendata.aws/terrain-tiles/, sampled at tile zoom 12 (~28 m/px here)

## Files

- mines_grades.csv — 12 graded mines (grade columns preserve units: oz/ton, %, WO3 units, Hg flasks)
- targets.csv — 58 scored geology targets (unit centroids)
- claims_active.csv/.shp — 1511 BLM claim centroids (centroids, NOT boundaries — BLM public GIS carries no corners)
- claims_closed.csv/.shp — 5037 BLM claim centroids (centroids, NOT boundaries — BLM public GIS carries no corners)
- geology_units.shp — 304 harmonized map polygons (import as GIS vector data; drape onto topography)
- faults.shp — 516 mapped structures
- plss_sections.shp — 1889 sections with open-ground status (OPEN/ACTIVE/CLOSED_ONLY/WITHDRAWN/NONFEDERAL/QUIET; research lead only, never a title opinion)
- topo_dem.asc — 1432 x 1081 Arc/Info ASCII grid @ 80 m (AWS Terrain Tiles (Mapzen terrarium))
- cassia.omf — OMF v0.9 bundle (Leapfrog: Leapfrog Geo menu > OMF > Import)

## Import order in Leapfrog Geo

1. **Topography** — right-click **Topographies > New Topography > Import
   Elevation Grid**, pick `topo_dem.asc` (Arc/Info ASCII grid). This becomes
   the project topography every GIS layer drapes onto.
2. **GIS vectors** — right-click **GIS Data, Maps and Photos > Import Vector
   Data**, multi-select `geology_units.shp`, `faults.shp`,
   `plss_sections.shp`, `claims_active.shp`, `claims_closed.shp`. When asked
   for elevation handling choose *Drape on topography* (the attributes ride
   along; colour `plss_sections` by `status`, claims by `status`).
3. **Points** — right-click **Points > Import Points**, pick
   `mines_grades.csv`: East/North/Elev are the first three columns; keep the
   grade columns as numeric data so you can filter/colour by `au_ozt`,
   `ag_ozt`, etc. Repeat for `targets.csv` (colour by `score`).
4. **Or do 1–3 in one step** — **Leapfrog Geo menu > OMF > Import** on
   `cassia.omf` brings in the point sets (with grades attached), draped
   faults, and a context topo mesh. OMF import is one-shot (objects cannot
   be reloaded), so prefer the CSV/SHP/ASC route for layers you expect to
   refresh from the monitor.

## Honesty notes (same rules as the map)

- Claim locations are **BLM MLRS centroids**, not staked corners; a claim
  point in 3-D space still cannot establish title. Verify serials at
  mlrs.blm.gov before acting on open ground.
- Section `status` is the monitor's conservative research lead
  (OPEN / ACTIVE / CLOSED_ONLY / WITHDRAWN / NONFEDERAL / QUIET), not a
  mineral-title opinion.
- Grades are the best *cited historic* figure per mine with source text
  preserved in `mines_grades.csv` — they are leads for sampling, not a
  resource estimate. Units differ by column (oz/ton, %, WO3 units, Hg
  flasks, $/yd³) and are never converted.
- Terrain is a public composite (3DEP/SRTM); expect ~10 m vertical noise —
  fine for draping and viewshed thinking, not for survey work.
