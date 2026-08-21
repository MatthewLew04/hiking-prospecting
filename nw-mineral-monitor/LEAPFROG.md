# Leapfrog Geo integration

> **2026-08-21:** the map now has its own Leapfrog-style 3-D modeller —
> `site/model3d.html`, opened from any mine card's **OPEN 3D MODEL** button
> (see [`GEOMODEL.md`](GEOMODEL.md)). It reads and writes Leapfrog's files
> directly: **OMF v2.0 for Leapfrog Geo 2025.1+** (the version that replaced
> v0.9 in 2025.1) and **OMF v0.9 for 5.x–2024.1**, plus DXF/OBJ/GOCAD meshes,
> Surfer / Geosoft / GXF / Arc ASCII grids, CSV points, structural, drillhole and
> block-model tables. The exporter below remains the scripted AOI-kit route.

Two directions, both shipped:

1. **Monitor → Leapfrog** — two ways:
   - **On the website**: the sidebar's **LEAPFROG EXPORT** section. Frame the
     area (zoom ≥ 8), turn on the layers you want, click **EXPORT CURRENT
     VIEW FOR LEAPFROG**, and the browser builds and downloads a zip of
     Leapfrog-ready files — no tools, no terminal.
   - **Scripted**: `pipelines/leapfrog_export.py` packages a full AOI from
     the repo bundles (same formats, finer DEM control, refreshable).
2. **3-D in the browser** — the map's **3D TERRAIN** sidebar section drapes
   every layer over real relief (AWS terrarium tiles, ~30 m). That is the
   fly-around; Leapfrog is the modeling environment.

## The in-map export button

Everything is generated client-side (the same vendored JSZip the MY DATA
ingester uses; OMF blobs use the browser's native CompressionStream) and all
coordinates land in **WGS84 / UTM, zone auto-picked from the view center,
EPSG stated in the bundled README**. The zip contains, depending on what is
in view and toggled on:

- `mines_grades.csv` — graded mines inside the view (full columnar table,
  bbox-filtered — not tile-dependent), all commodity columns + sources.
- `mrds_sites.csv`, `usmin_features.csv`, `statesurvey_sites.csv`,
  `ardf_sites.csv`, `claims_active.csv`, `claims_closed.csv` — **viewport
  snapshots**: exactly the features the browser has loaded for the current
  view and zoom, deduplicated. Never a statewide archive; the bundled README
  repeats this.
- `<aoi>_geology_units.shp`, `<aoi>_faults.shp`, `<aoi>_plss_sections.shp`
  (with open-ground status), `<aoi>_targets.csv` — full AOI vectors for any
  AOI bundle intersecting the view (whole features, not clipped).
- `topo_dem.asc` + `.prj` — Arc/Info ASCII grid sampled from terrarium tiles
  at a zoom picked from the view size (cell auto-chosen, ≤ ~1.5 M cells).
- `view.omf` — OMF v0.9 bundle of the point sets (grades attached), draped
  faults, and a decimated topo mesh. Skipped with a note on browsers without
  CompressionStream.
- `README-LEAPFROG.md` — CRS, counts, the import click-path, honesty notes.

Terrain fetches carry a 20 s timeout: offline or blocked, the export still
completes with elevation 0 and says so. The button refuses world-scale views
(zoom ≥ 8) so a mis-click can't try to package a continent.

## Why files, not an API

Leapfrog projects are local, proprietary databases; the supported ways in are
its file importers and, since the Evo era, Seequent's cloud (Central /
[Seequent Evo](https://developer.seequent.com/) and its Geoscience Object
API). Evo requires a Seequent ID, an org tenancy, and OAuth — the right tool
for enterprise sync, overkill for a prospecting workflow. The file path is
version-proof and offline: **CSV points, ESRI shapefiles, Arc/Info ASCII
grids, and OMF v0.9** all import into every Leapfrog Geo release this decade.
The exporter targets exactly those four.

The `.omf` writer here is stdlib-only and byte-validated round-trip against
the reference `omf` 1.0.1 reader (the OMF v0.9 implementation Leapfrog's
importer matches — the modern successor to that package no longer installs on
current Python, which is why the format is written directly).

## Export a kit

```bash
python3 pipelines/leapfrog_export.py --aoi cassia
# district-scale kit at full DEM resolution:
python3 pipelines/leapfrog_export.py --aoi cassia \
    --bbox -113.75 42.05 -113.45 42.30 --cell 30
```

Output lands in `exports/leapfrog/<aoi>/` (a generated artifact — commit it
or not as you like; terrain tiles cache under the already-ignored
`pipelines/cache/`): `mines_grades.csv`, `targets.csv`, `claims_active/closed.csv+.shp`,
`geology_units.shp`, `faults.shp`, `plss_sections.shp` (open-ground status
attribute), `topo_dem.asc(+.prj)`, `<aoi>.omf`, and a `README-LEAPFROG.md`
with the exact click-path (Topographies → Import Elevation Grid; GIS Data →
Import Vector Data + drape; Points → Import Points; or one-shot
`OMF > Import`).

Everything is written in **WGS84 / UTM (zone auto-picked from the AOI,
EPSG stated in the README)** in meters, because Leapfrog is a Cartesian XYZ
package — raw lon/lat imports as a flat pancake. Elevations are sampled from
the same terrarium composite the map's 3-D mode streams; tiles cache under
`pipelines/cache/terrain/`. Offline runs degrade honestly: Z becomes 0 and
the kit's README says so.

## What Leapfrog adds on top of this

The kit gives Leapfrog topography, draped geology/faults/claims, and graded
mine points — enough to *see* a district in 3-D and sketch section lines.
Leapfrog's actual power (implicit modeling: RBF-interpolated veins, grade
shells, geological contacts) needs **drillhole intervals or structural
measurements**, which the public sources here don't carry. When a target
graduates to samples — or a nearby operator's drilling shows up in county
records / NI 43-101 filings — that data goes into Leapfrog as
collar/survey/assay CSVs alongside this kit's context layers.

Round-tripping back: Leapfrog exports meshes (OBJ/DXF) and its own OMF; the
map's MY DATA ingester takes GeoJSON/KML/zipped SHP, so 2-D footprints of
modeled surfaces can come back as map layers. A mesh viewer in the browser is
possible (MapLibre custom layer / three.js) but is not built.

## Honesty notes

- OMF-imported objects in Leapfrog **cannot be reloaded** — re-import after a
  refresh; prefer the CSV/SHP route for layers you expect to update from the
  monitor.
- Claim points are BLM MLRS **centroids**, sections' `status` is a research
  lead, grades are cited historic figures with sources attached — the same
  disclaimers as the map, now in 3-D. None of it is a title search or a
  resource estimate.
- Terrain is a public ~30 m composite: good for draping and access thinking,
  not survey-grade.
