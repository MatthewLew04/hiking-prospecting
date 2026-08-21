# GEOMODEL — the 3-D geological modeller (Leapfrog-style alternate view)

Every mine card on the map has **⛰ OPEN 3D MODEL**. It opens `site/model3d.html`
in a new tab with a model built around that site: real terrain, draped satellite
/ USGS topo / Macrostrat geology imagery, the AOI's mapped units and faults
draped on the ground, the graded mines, targets and BLM claim centroids nearby,
and empty scaffolds for the things a geologist adds — underground workings,
a stratigraphic (pancake) model, block models, sections. Everything the
modeller produces round-trips with the desktop packages: **Leapfrog Geo**
(OMF v2.0 for 2025.1+, OMF v0.9 for ≤ 2024.1, plus DXF/OBJ/GOCAD/CSV/grids),
**Surfer** (.grd), **Geosoft Oasis montaj / Target** (.grd, GXF, XYZ, UBC),
**Kingdom** (ZMAP+, Irap, SEG-Y, LAS) and generic GIS (GeoJSON, DXF).

The same engine exists twice on purpose — Python under `pipelines/geomodel/`
(batch kits, CLI conversion, the reference implementation with byte-exact
format tests) and JavaScript under `site/assets/geomodel/` (the browser app,
cross-checked against Python to 1e-6 by `tools/test_gm_engine.mjs` and
`tools/test_gm_formats.mjs`).

## 1. The problem this solves

Historic mine maps are **3-D information in a 2-D format**: a level plan is a
horizontal slice at a stated elevation, a longitudinal section is a vertical
slice along the vein, and the text says "the adit runs N 45° E for 900 ft to the
shaft, which is 300 ft deep". Nothing in a GIS puts those together. Leapfrog
does, but only from drillholes and structural data you have to enter by hand.

The modeller's workflow is the digitising bridge:

1. **Georeference the scan** (TOOLS ▸ Georeference): a level plan becomes a
   horizontal `ImagePlane` at its level elevation (2+ tie points, or one point +
   a scale bar + a rotation); a longitudinal/cross section becomes a vertical
   plane between two surface points with top/bottom elevations (the Oasis montaj
   "Georeference Section Image" convention; Leapfrog's yellow/green markers).
2. **Trace** drifts and crosscuts on the plan (clicks are read through the
   georeference, at the level elevation), **add adits** from the portal (bearing
   + length + grade, or click portal and end), **shafts / winzes** from the collar
   (depth, dip, azimuth), **raises** between levels, **stopes** as outlines
   extruded between two elevations. Map units (feet) are converted once, at the
   door. Every feature carries type, level, source document/page, confidence.
3. **See it**: tubes colour-coded by type under the terrain; slice through it;
   the 2-D section panel shows the classic long-section; **SEND FOOTPRINT TO MAP**
   puts the plan-view trace back on the main map as a MY DATA layer; export DXF
   / OMF / GeoJSON.

The same page does the other three things a geologist wants from Leapfrog:

* **Pancake stratigraphy** (TOOLS ▸ Stratigraphy): units youngest-first, each
  with a base from contact points (RBF / kriging / IDW), an imported surface
  grid, a constant, or a thickness; *deposit* bases on-lap older units,
  *erosion* bases cut them (Leapfrog's surface-chronology rules reduced to
  heightfields); BUILD gives base surfaces, closed unit volumes, section ribbons,
  virtual drillholes, and tags block models by unit.
* **Kriging / block model** (TOOLS ▸ Block model): block grid, sample layer
  (assay points, graded mines, imported XYZ), experimental variogram + fit
  (spherical/exponential/gaussian/linear/power, nugget, anisotropy), moving-
  neighbourhood ordinary kriging (or IDW / nearest) optionally inside one unit,
  cut-offs, grade–tonnage, export CSV / UBC / OMF.
* **Implicit surfaces** (TOOLS ▸ Implicit surface): RBF of signed distances →
  iso-surface (veins, intrusions, ore shells) — the FastRBF idea.
* **Sections & slicing** (TOOLS ▸ Section & slice): draw a line or use W–E /
  S–N presets; the plane clips the model, intersects every mesh and surface,
  fills the pancake units, samples block models, projects nearby workings; the
  offset slider sweeps; the 2-D panel renders the section the way it is drawn
  on paper (distance vs elevation, VE) and exports DXF/PNG.
* **Geophysics**: Surfer/Geosoft/GXF/ZMAP/Irap grids import as *property*
  layers draped in colour on the terrain (or flat at an elevation), SEG-Y
  sections become vertical image planes, LAS logs attach to drillholes.

## 2. Architecture

```
site/index.html ──OPEN 3D MODEL──▶ site/model3d.html?lat&lon&name&gi&aoi&r
      │  (writes a viewport hand-off of loaded tiled points to IndexedDB 'nwmm-geomodel')
      ▼
 gm-viewer.js   app shell: boot, layer tree, inspector, import/export, autosave (IndexedDB)
 gm-site.js     site bootstrap: terrarium terrain → Grid2D, imagery mosaic → texture,
                AOI geology/faults (clip + ear-clip + subdivide + drape), grades, targets, claims
 gm-render.js   three.js: one Group per object, clipping planes, picking, tubes, instanced blocks
 gm-tools.js    Section, Workings, Georef, Stratigraphy, Blocks/kriging, Implicit tools
 gm-engine.js   numerics (pure): IDW, RBF, variograms, OK, stratigraphy, block models,
                plane cuts, marching tetrahedra, workings constructors  ──▶ gm-worker.js (Web Worker)
 gm-formats.js  every reader/writer (OMF v0.9 + v2.0 incl. a Parquet/Thrift writer, Surfer,
                Geosoft GRD/GXF/XYZ, Arc ASCII, ZMAP+, Irap, CPS-3, UBC, OBJ, DXF, GOCAD,
                Leapfrog .msh, CSV tables, SEG-Y, LAS, PNG, ZIP)
 gm-core.js     object model + JSON project (shared with Python), UTM, IndexedDB store
 gm-ui.js       DOM helpers, variogram plot

pipelines/geomodel/          Python twin (stdlib only; numpy used when present)
   model.py  interp.py  stratigraphy.py  blockmodel.py  slicing.py  workings.py  kit.py
   formats/{omf1,omf2,parquet_lite,thrift_compact,surfer,geosoft,arcascii,zmap,irap,cps3,
            ubc,obj,dxf,gocad,lfmsh,tables,segy,las}.py
pipelines/geomodel_kit.py    CLI: site | export | convert | info | list
```

### Object model (`nwmm-geomodel/1`)

One JSON project = CRS (WGS84/UTM zone, metres, Z = elevation) + origin + a
list of objects, each with id / name / colour / visibility / provenance /
metadata. Typed arrays are stored as `{"@f64": base64}` blobs so the browser
wraps them without parsing. Kinds:

| kind | what | notes |
|---|---|---|
| `grid2d` | node-registered regular grid | roles `topography` / `contact` / `surface` / `property`; values south-row-first, x fastest (Surfer/Geosoft order) |
| `mesh` | triangles | roles `geology` (draped map unit), `unit` (closed pancake volume), `contact`, `stope`, `section` |
| `lineset` | polylines + per-part features | roles `workings` (schema `nwmm-workings/1`: type, level, level_z, width_m, source, confidence, units_in), `faults`, `geology-outline`, `drillhole-traces`, `section` |
| `points` | xyz + attribute columns | roles `mines`, `targets`, `claims`, `samples`, `contacts`, `structural`, `collars` |
| `blockmodel` | regular blocks, i-fastest attributes | numeric + category attributes; estimates recorded in metadata |
| `drillholes` | collar / survey / interval tables | Leapfrog conventions, dip positive down, minimum-curvature desurvey |
| `imageplane` | georeferenced scan | `plan` (control points + elevation) or `section` (two top corners + z top/bottom) |
| `stratmodel` | ordered units + contact rules | references the base grids |
| `section` | saved section line / plane | products recomputed on load |

### Where things run

* Everything interactive runs in the browser; the numerics run in a module
  Web Worker (`EngineClient`) with a main-thread fallback. RBF fit for 1 500
  centres ≈ 2 s, kriging 20 k blocks ≈ 1 s, marching tetrahedra 100³ ≈ 50 ms.
* Projects autosave to IndexedDB (`nwmm-geomodel`, whitelisted by the map's
  storage guard) and export as `.geomodel.json`; nothing leaves the browser.
* The Python side builds the same project from the repo bundles
  (`geomodel_kit.py site --grade-index 157`) for batch kits or when a browser is
  not available, and converts files between any supported pair.

## 3. Format matrix

| format | read | write | who uses it | notes |
|---|---|---|---|---|
| OMF v2.0 (`.omf`, zip + Parquet) | ✓ | ✓ | Leapfrog Geo 2025.1+, Seequent Evo | validated against Seequent's `omf-rust` reader (0 problems) and pyarrow |
| OMF v0.9 (`.omf`, binary) | ✓ | ✓ | Leapfrog Geo 5.x – 2024.1 | validated against the reference `omf` 1.0.1 reader |
| DXF R12 | ✓ | ✓ | Leapfrog, AutoCAD, Surpac, Vulcan, QGIS | 3DFACE meshes, 3-D POLYLINE, POINT; reads polyface/LWPOLYLINE too (ezdxf-validated) |
| OBJ | ✓ | ✓ | Leapfrog, everything | triangles (trimesh-validated) |
| GOCAD TSurf / PLine / VSet | ✓ | ✓ | Leapfrog, Petrel, Kingdom | properties, ZPOSITIVE Depth |
| Leapfrog `.msh` | ✓ | ✓ | Leapfrog native mesh | community-documented layout, flagged as reverse-engineered |
| Surfer `.grd` DSAA / DSBB / DSRB | ✓ | ✓ | Surfer, Leapfrog elevation + 2-D geophysical grids, Oasis montaj | GDAL-validated both directions |
| Geosoft `.grd` (v2 binary) | ✓ (incl. compressed) | ✓ (uncompressed) | Oasis montaj, Leapfrog 2025+ | `.gi` sidecar is not written (undocumented binary) |
| Geosoft GXF | ✓ (incl. base-90) | ✓ | Oasis montaj, Leapfrog 2025+ | all 8 SENSE orientations |
| Geosoft XYZ | ✓ | ✓ | Oasis montaj databases (channels, Line/Tie) | GDB itself is proprietary: use the free Geosoft Viewer's XYZ export |
| Arc/Info ASCII | ✓ | ✓ | Leapfrog, QGIS | |
| ZMAP+ | ✓ | ✓ | Kingdom, Petrel, Landmark | node-registered |
| Irap classic | ✓ | ✓ | Petrel/RMS | rotation honoured |
| CPS-3 | ✓ | – | GeoFrame/Petrel | column direction flagged |
| UBC mesh + model | ✓ | ✓ | Geosoft voxels, Leapfrog 3-D grids, SimPEG | `.geosoft_voxel` is proprietary: export UBC from montaj |
| CSV points / structural / block model / drillholes | ✓ | ✓ | Leapfrog importers | header synonyms, Leapfrog-style block-model header + sidecar |
| SEG-Y rev 0/1 | ✓ | ✓ | Kingdom, OpendTect, GPR/seismic/resistivity | IBM/IEEE/int; becomes a section image + trace points |
| LAS 2.0 (3.0 partial) | ✓ | ✓ | Kingdom, Leapfrog downhole | attaches to drillholes |
| GeoJSON | ✓ | ✓ | the map's MY DATA, QGIS | WGS84 footprint of workings |
| images / PDF pages | ✓ | – | scanned maps | georeferenced as ImagePlanes |

Not done on purpose: Leapfrog `.aproj` projects, Geosoft `.gdb` / `.geosoft_voxel`,
Kingdom `.tks` databases — all proprietary without a public spec; the supported
interchange is the files those packages import/export.

## 4. Honesty and limits

* Terrain is the public AWS terrarium composite (~10–30 m); imagery tiles are
  draped as context. Draped geology meshes are map polygons following terrain,
  not modelled volumes.
* Graded mines carry cited historic figures; claims are BLM centroids; nothing
  here is a resource estimate, a title search or an access permission.
* Workings digitised from a map inherit that map's accuracy; the feature
  schema records source, page and confidence so a sketch never masquerades as a
  survey. Never enter adits or shafts.
* Kriging and RBF are deterministic, documented implementations (moving
  neighbourhood OK, spherical-family variograms, Gaussian-elimination solves);
  they are research tools, not a replacement for a resource geologist.
* OMF objects imported into Leapfrog cannot be reloaded — re-import after a
  refresh, or use the CSV/grid route for layers you expect to update.

## 5. Roadmap — from mine files to underground maps

The architecture was laid out so the next steps are data, not plumbing:

1. **Plates out of the WS12 document store** — the harvester already stores
   mine-file PDFs; a `pipelines/geomodel/plates.py` step can render candidate
   pages (those with "level", "section", "plan", scale bars) to PNG with their
   document/page provenance so the Georeference tool opens them directly.
2. **Text → geometry** — an extraction pass over OCR'd text ("adit … N 45° E …
   900 ft", "shaft 300 ft deep", "100-foot level") into `workings` constructors
   (bearing/length/depth/level) with `confidence: 'described'`, reviewed in the
   viewer.
3. **Soil and rock composition pancakes** — assay / soil / drill tables (CSV,
   LAS) → `drillholes` + `samples` → kriged block models per unit; lithology
   intervals → contact points → the stratigraphy builder. The data model already
   carries units, lithology and per-block categories for this.
4. **Geophysics on surfaces** — sample imported grids onto any mesh's vertices
   (Leapfrog's "evaluate on surface"); the property-drape path is the first half.
5. **Shared models** — the project JSON is small and self-contained; putting
   it behind the existing Docs API would make models citable from ASK.

## 6. Running and testing

```bash
python3 tools/range_server.py 8000         # then http://localhost:8000/model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills&gi=157
python3 pipelines/geomodel_kit.py site --grade-index 157 --radius 2500   # batch kit -> exports/geomodel/<slug>/
python3 pipelines/geomodel_kit.py convert in.omf out.dxf                # any supported pair
python3 pipelines/geomodel_kit.py info survey.sgy

python3 -m unittest discover -s tests -p 'test_geomodel_*.py'   # 157 Python tests (formats vs GDAL/pyarrow/omf-rust/ezdxf/segyio/lasio)
node tools/test_gm_formats.mjs      # JS readers/writers vs the Python fixtures (185 checks)
node tools/test_gm_engine.mjs       # JS numerics vs Python (166 checks)
node tools/test_model3d.mjs         # headless browser acceptance of the page (28 checks)
```

Vendored: three.js 0.185 (`site/assets/three/`, `npm run vendor:three`). No
CDN, no build step, no npm dependency at runtime.
