# GEOMODEL — the 3-D geological modeller (Leapfrog-style alternate view)

Every mine card on the map has **⛰ OPEN 3D MODEL**. It opens `site/model3d.html`
in a new tab with a model built around that site: real terrain, draped satellite
/ USGS topo / Macrostrat geology imagery, the AOI's mapped units and faults
draped on the ground, the graded mines, targets and BLM claim centroids nearby,
and the groups a geologist fills listed as *not started* with the step that
fills them — underground workings, a stratigraphic (pancake) model, block
models, sections (see §8 for the page itself). Everything the
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
* **Structural geology** (TOOLS ▸ Structural data / Stereonet / Form
  interpolant) — the pathway that lets a district with *no drilling* still
  carry orientation. Dip and dip azimuth are derived automatically from where
  a mapped contact or fault trace crosses the terrain (a least-squares plane
  through the 3-D trace: the three-point problem run continuously along the
  line, with relief / spread / RMS gates so flat ground never produces a
  confident-looking number), digitised by hand on the draped map, or imported
  from CSV. From there: a lower-hemisphere stereonet with Kamb, exponential
  Kamb and Schmidt contouring, Bingham and Fisher statistics, selection linked
  both ways to the 3-D scene; declustering; a **form interpolant** whose
  gradient is constrained by the poles (Lajaunie's potential field), giving
  form surfaces in 3-D and form lines on topography; and a **structural
  trend** field whose anisotropy halves every range. See §7.

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
      │  (writes a viewport hand-off to IndexedDB 'nwmm-geomodel': the loaded tiled
      │   points AND the USGS geology polygons + fault traces the map has loaded
      │   around the site — so every dot gets draped rock units, not only AOI bundles)
      ▼
 gm-viewer.js   app shell: boot, layer tree, inspector, import/export, autosave (IndexedDB)
 gm-site.js     site bootstrap: terrarium terrain → Grid2D, imagery mosaic → texture,
                AOI geology/faults (clip + ear-clip + subdivide + drape), grades, targets, claims
 gm-render.js   three.js: one Group per object, clipping planes, picking, tubes, instanced blocks
 gm-tools.js    Section, Workings, Georef, Stratigraphy, Blocks/kriging, Implicit tools
 gm-engine.js   numerics (pure): IDW, RBF, variograms, OK, stratigraphy, block models,
                plane cuts, marching tetrahedra, workings constructors  ──▶ gm-worker.js (Web Worker)
 gm-structural.js  structural geology: poles, three-point derivation from map
                traces, declustering, stereonet projection + contouring, Bingham /
                Fisher statistics, the gradient (form) interpolant, trend fields
 gm-struct-tools.js  the three structural tool panels
 gm-more-tools.js  measure · notes (pinned, with a source) · trace a fault / contact / vein
 gm-geom-tools.js  project a trace down dip (vein / fault sheet) · contours · plane from a measurement
 gm-map-model.js   Model the rock from the map (contacts + derived dips → inferred pancake) · stated water level
 gm-formats.js  every reader/writer (OMF v0.9 + v2.0 incl. a Parquet/Thrift writer, Surfer,
                Geosoft GRD/GXF/XYZ, Arc ASCII, ZMAP+, Irap, CPS-3, UBC, OBJ, DXF, GOCAD,
                Leapfrog .msh, CSV tables, SEG-Y, LAS, PNG, ZIP)
 gm-core.js     object model + JSON project (shared with Python), UTM, IndexedDB store
 gm-ui.js       DOM helpers, variogram plot

pipelines/geomodel/          Python twin (stdlib only; numpy used when present)
   model.py  interp.py  stratigraphy.py  blockmodel.py  slicing.py  workings.py  contours.py  mapmodel.py  kit.py
   narrative.py  resolve.py  agentbuild.py  render2d.py  publish.py   <- prose -> model
   mapplate.py   assay.py                                             <- plates, grades
   formats/{omf1,omf2,parquet_lite,thrift_compact,surfer,geosoft,arcascii,zmap,irap,cps3,
            ubc,obj,dxf,gocad,lfmsh,tables,segy,las}.py
pipelines/geomodel_kit.py    CLI: site | export | convert | info | list | mines | narrate
services/minevis/            HTTP service: /tools /call /jobs/<id> (see its README)
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
| `points` | xyz + attribute columns | roles `mines`, `targets`, `claims`, `samples`, `contacts`, `collars`; role `structural` additionally guarantees the columns `dip` (0–90), `dip_azimuth` (0–360, clockwise from north, down-dip direction) and `polarity` (+1 right way up, −1 overturned), and renders as oriented discs; role `trend` is the same contract plus a `strength` column and carries its `TrendField` in `metadata.trend` |
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
* Orientations derived from a map trace inherit the accuracy of both the map
  and the DEM. Every derived measurement carries `relief_m`, `fit_rms_m` and
  `window_m`, and `confidence: 'inferred'`; windows without relief, without
  spread, or with a poor plane fit are counted and rejected rather than
  guessed, and a layer that yields nothing says so instead of returning zeros.
* The form interpolant is a real gradient-constrained RBF, not offset points
  pretending to be one — but it is a dense O(n³) solve in the browser, so it
  caps at a few hundred measurements and declustering is the way to get under
  the cap. It is blind to faults and truncating intrusions, and clustered data
  produces geologically implausible surfaces; both are stated in the panel.
* Kriging and RBF are deterministic, documented implementations (moving
  neighbourhood OK, spherical-family variograms, Gaussian-elimination solves);
  they are research tools, not a replacement for a resource geologist.
* OMF objects imported into Leapfrog cannot be reloaded — re-import after a
  refresh, or use the CSV/grid route for layers you expect to update.

## 5. Roadmap — from mine files to underground maps

**Delivered 2026-08-27 (build `2026-08-27-struct1`)** — the structural layer
described in §7, plus the platform work it needed: display settings are now
part of the project (colormap, attribute, glyph size, labels and cut-offs
survive a reload), the camera has an orthographic mode (`o` / `p`), and the
scene carries a legend and a scale bar. The rest of the plan lives in the
[`LEAPFROG-PARITY.md`](LEAPFROG-PARITY.md) — the read of the 23 Seequent
course guides and papers against this code, with every verified gap, what was
built from it on 2026-09-02, and what is set aside as drillhole-only.


**Delivered 2026-09-01** — the corpus autopopulator
([`AUTOPOPULATE.md`](AUTOPOPULATE.md)): `pipelines/geomodel_corpus.py`
bridges the WS12 document store's mines to buildable references (per-citation
quote joins, reviewed-evidence names, state-survey sites; ambiguity parks,
never guesses) and carves each mine's own text out of its documents;
`pipelines/geomodel_autopopulate.py` runs every (mine, document) description
through the minevis build path with an audited omit-answer policy and
publishes `site/models/<slug>-<hash8>/` plus `site/data/models/index.json`.
The map's grade cards read that index: minerals, workings by type, levels
and depth, the underground lexicon (`narrative.lexicon`), the source
documents, and OPEN 3D MODEL — DESCRIBED WORKINGS opening the pregenerated
project. This is roadmap item 2 below made batch; item 1 (plates) remains.

The architecture was laid out so the next steps are data, not plumbing:

1. **Plates out of the WS12 document store** — the harvester already stores
   mine-file PDFs; a `pipelines/geomodel/plates.py` step can render candidate
   pages (those with "level", "section", "plan", scale bars) to PNG with their
   document/page provenance so the Georeference tool opens them directly.
2. **Text → geometry** — an extraction pass over OCR'd text ("adit … N 45° E …
   900 ft", "shaft 300 ft deep", "100-foot level") into `workings` constructors
   (bearing/length/depth/level) with `confidence: 'described'`, reviewed in the
   viewer.  *Delivered as the autopopulator above for the stored corpus; the
   WS13 cloud corpus plugs into the same `assignments()` seam when its text
   becomes reachable.*
3. **Soil and rock composition pancakes** — assay / soil / drill tables (CSV,
   LAS) → `drillholes` + `samples` → kriged block models per unit; lithology
   intervals → contact points → the stratigraphy builder. The data model already
   carries units, lithology and per-block categories for this.
4. **Geophysics on surfaces** — sample imported grids onto any mesh's vertices
   (Leapfrog's "evaluate on surface"); the property-drape path is the first half.
5. **Shared models** — the project JSON is small and self-contained; putting
   it behind the existing Docs API would make models citable from ASK.

### From a written description (`services/minevis`)

An agent on the EC2 box hands in **prose** and gets back a **3-D model URL**.
No front-end change was needed: `model3d.html` already accepts `?project=<url>`.

```
narrative.py   USGS/USBM prose -> typed elements + the questions it leaves open
resolve.py     mine name -> located, cited CANDIDATES out of grades.json
mapplate.py    a georeferenced scan + traces -> SURVEYED elements
assay.py       quoted grades (selected != average) + a stated vein attitude
agentbuild.py  spec + mine -> Project, using only workings.py primitives
render2d.py    plan / longitudinal section / isometric as stdlib SVG
publish.py     content-addressed models/<slug>-<hash8>/ + manifest.json
```

Prose can only ever produce `described` geometry. `surveyed` is reachable only
by tracing a plan or section whose georeference can be checked — `mapplate`
reports the implied metres-per-pixel and, with three or more control points,
how far they disagree. A plate that is missing its georeference or its
elevation is a question, not a plate draped at zero.

Three rules run through all of it, and they are enforced by tests:

1. **Nothing is invented.** A missing bearing is a question, never a default.
   The only exceptions are *definitional* (an unqualified "shaft" is vertical
   by definition of the word) and they are listed per element in `defaults`.
2. **Every element carries the sentence it came from** and that sentence's
   character span in the input text.
3. **Confidence is per field** — `surveyed` (traced off a georeferenced plan),
   `described` (read off the text), `assumed` (supplied in answer to a
   question) — and it is visible everywhere: solid, dashed and dotted in the
   drawings, counted in every legend, itemised in `manifest.json`. The failure
   mode this is designed against is a hand-drawn-from-text adit being read as
   a survey.

A model goes to the public `models/` prefix by default and inherits the Cognito
**app** gate — which gates the app, not the object, so anyone who can construct
its URL can fetch the file. `private: true` writes it under `private/` instead,
absent from the CloudFront read allowlist by construction, and returns
short-lived signed links (`sign_model_url` mints fresh ones).

[`MINE-VISUALS-GUIDE.md`](MINE-VISUALS-GUIDE.md) is the how-to.
`AGENT-VISUALS.md` has the plan and phasing; `services/minevis/README.md` has
the tool schemas and the agent wiring.

## 6. Running and testing

```bash
python3 tools/range_server.py 8000         # then http://localhost:8000/model3d.html?lat=42.147&lon=-113.125&name=Silver%20Hills&gi=157
python3 pipelines/geomodel_kit.py site --grade-index 157 --radius 2500   # batch kit -> exports/geomodel/<slug>/
python3 pipelines/geomodel_kit.py convert in.omf out.dxf                # any supported pair
python3 pipelines/geomodel_kit.py info survey.sgy

python3 pipelines/geomodel_kit.py mines "White Caps" --state NV                  # name -> candidates
python3 pipelines/geomodel_kit.py narrate --text "An adit driven N45E for 900 feet."
python3 pipelines/geomodel_kit.py narrate --file desc.txt --mine-id grades:17 --out build/
python3 services/minevis/server.py --state-dir /var/lib/minevis                 # the agent's HTTP service

python3 ci/run_tests.py                                        # everything, with the strict-skip check
python3 -m unittest discover -s tests -p 'test_geomodel_*.py'  # 468 Python tests (formats vs GDAL/pyarrow/omf-rust/ezdxf/segyio/lasio; parser, placement, views, geometry)
python3 tests/test_minevis_service.py                          # 69 service tests (in-process, no network)
node tools/test_gm_formats.mjs      # JS readers/writers vs the Python fixtures (171 checks)
node tools/test_gm_engine.mjs       # JS numerics vs Python (227 checks: interpolants, stratigraphy, workings, extrusion, contours, elevation, clipping)
node tools/test_gm_structural.mjs   # structural numerics (105 checks)
npm run test:model3d                # the six headless browser harnesses below, in order
node tools/test_model3d.mjs         # the page: boot, layers, section, workings, georef, stratigraphy, kriging, reload (33 checks)
node tools/test_model3d_structural.mjs   # derive, stereonet, form interpolant, trends, arming, row delete, set elevation (53 checks)
node tools/test_model3d_ui.mjs      # the shell: tool host, arming, Esc, menu order, readiness, key, pick card, undo, scenes, render image (29 checks)
node tools/test_model3d_render.mjs  # tube picking, pick-through, screen-space dashes, opacity, labels, declutter, slab clip (31 checks)
node tools/test_model3d_tools.mjs   # section PNG/SVG, thick slice, contact-from-trace, input classification, implicit bounds, measure, notes, polylines (42 checks)
node tools/test_model3d_geom.mjs    # extrude down dip, contours, plane from a measurement (29 checks)
node tools/test_model3d_site.mjs    # hand-off geology draped for any site, ages parsed, USMIN glyphs (40 checks)
node tools/test_model3d_mapmodel.mjs   # model the rock from the map, water level, refusals (20 checks)
node tools/test_map_handoff.mjs     # the map's hand-off of USGS geology, faults and points (28 checks, real local PMTiles)
```

### Cross-check setup

`pipelines/geomodel` is pure standard library on purpose: every reader and
writer is validated against somebody else's implementation of the same format
rather than against itself. 35 of the tests therefore need reference libraries
and reference files, and `ci/run_tests.py` fails the run if they skip —
an unreviewed skip is a cross-check that silently stopped happening.

```bash
pip install -r ci/requirements-crosscheck.txt
python3 tools/fetch_gm_refs.py            # ~20 s, ~115 MB, idempotent
python3 tools/fetch_gm_refs.py --build-omf2   # + Seequent's reader (needs cargo)
```

The corpus lands in `~/.cache/nw-mineral-monitor/gm-ref`; set `GM_REF_DIR` to
put it elsewhere, and `GM_OMF1_PYTHON` to point at an existing omf 1.0.1
interpreter. `tests/gm_ref.py` is the only place that knows these paths.

| Reference | Source | Validates |
| --- | --- | --- |
| GDAL (via rasterio) | pip | Surfer 6/7, ArcInfo ASCII, GXF, ZMAP — both directions |
| `oasis_montaj_grd.py` | fatiando/harmonica, pinned commit | Geosoft `.grd`, float and short |
| ezdxf | pip + a generated minimal R12 file | DXF R12 meshes, 3-D polylines, points, CRLF |
| trimesh | pip | OBJ triangle meshes |
| segyio / lasio | pip | SEG-Y rev1 traces and headers; LAS 2.0 |
| pyarrow | pip | OMF v2.0 Parquet schema strings and column values |
| `one_of_everything.omf` | gmggroup/omf-rust, pinned commit | OMF v2.0 reader against the reference file |
| `test_v09.omf` | generated by `omf` 1.0.1 in its own venv | OMF v0.9 reader, and v0.9 → v2.0 conversion |
| `omf2` wheel | built from omf-rust (cargo) | our OMF v2.0 output read back by Seequent's reader |
| GOCAD `.ts` samples | lanl/LaGriT + SCEC CFM, pinned commits | TSurf parsing: TFACE, per-vertex and per-face properties, no-data |

Every fetched file is checksum-pinned in `tools/fetch_gm_refs.py`. A mismatch
fails loudly rather than quietly cross-checking against something else.

Vendored: three.js 0.185 (`site/assets/three/`, `npm run vendor:three`). No
CDN, no build step, no npm dependency at runtime.

## 8. The page (build 2026-09-02-ui2)

Four critiques of the page — a first-time user, a daily Leapfrog user, an
information designer and a workflow coach, each judging 26 screenshots and
the panel code — agreed on the same faults: nine tools behind one unordered
menu, a tool panel that took over the properties panel for the session, no
undo, a described-workings model that opened with nothing dashed on screen,
and a confidence key that vanished exactly when every working was described.
The shell was rebuilt around them.

**Three columns.** Left, the layer tree in three bands — INPUTS (terrain,
images, map geology and outlines, structure, mines, claims, imports), MODELS
(workings, stratigraphy, surfaces, block models), OUTPUTS (sections, notes) —
with the groups a step fills always listed, empty, as *— not started · step
n*, so a first-time user sees where the workings will go before there are
any. A filter box narrows the rows; badges say ⚠ warnings, ✕ failed to draw,
∅ nothing digitised or built yet; hovering a row explains its tag in words;
double-click zooms; right-click (or ⋯) opens the menu — zoom, properties,
show only this, export, rename, delete — on rows, on groups, and on the
scene itself (sections through here, hide, centre the view). Right, the
**layer inspector** on top and a **tool host** below it. Opening a tool never
deselects the layer; the host's title bar says *STEP n/9 · NAME*, shows
◎ ARMED while a click mode is live, and closes with DONE ✕. With nothing
selected the inspector shows **WHERE THINGS STAND**: the nine steps with
their state (done with what they produced, ready, or blocked with the missing
input), and *Start here →*.

**The nine steps.** `TOOL_STEPS` in `gm-tools.js` is the one table behind
the TOOLS ▾ menu (grouped FROM THE MAP / FROM THE GEOLOGY / VOLUMES / SEE IT,
numbered, with a readiness hint per item), each panel's NEEDS / HAS / NEXT
strip (✓ or ✗ per prerequisite with an OPEN button to the step that provides
it), the progress card and HELP > THE ORDER: 1 georeference a scan, 2
workings from maps, 3 structural data, 4 stereonet, 5 form interpolant and
trends, 6 stratigraphy, 7 implicit surface, 8 block model and kriging, 9
section and slice. Readiness is computed from the project alone.

**One arming path.** Every click mode — trace, adit, shaft, raise, stope,
section line, georeference PICK, contact points, ± points, virtual drillhole,
structural digitising, stereonet selection — goes through `Tools.arm()`,
which paints a strip at the top-left of the viewport saying what the clicks
do, how to get out, and what will be written where and at what confidence
(*TRACE — click along the working · Enter finishes · Esc cancels → Workings as
sketched on the ground*). A trace off the bare ground cannot be committed as
*surveyed* without a warning. Esc leaves the armed mode first, closes the tool
second, clears the pick third, the selection last. Opening the Workings or
Stratigraphy panel creates no layer; the first committed feature does.

**Honesty cues that cannot hide.** The confidence key (solid surveyed,
dashed described, dotted assumed, with counts) is drawn whenever any working
is on screen, one class or three; working-type colours and claim colours are
keyed beside it. A model whose workings were read from prose opens on those
workings with the ground at 55 % and says so in a toast (VIEW ▾ > Solid ground
restores it). The PICKED card lands under the layer title with the feature
highlighted in white, its confidence in words, the document and page, and the
sentence it was read from. VIEW ▾ > Render image writes the scale bar (with
*nominal* in perspective), the confidence key, the NOT A SURVEY sentence, north,
the viewing direction and a project / CRS / VE / date footer into the PNG; the
kit zip carries the same picture and the same sentence in its README.
`confidenceSentence()` and `legendModel()` are the single sources the banner,
the legend and the image all read.

**Undo and dialogs.** Deleting a layer, a section, a working, a scene or a
measurement goes through `app.destructive()`: the inverse is kept, the toast
offers UNDO for eight seconds, Ctrl/Cmd+Z undoes the last one. Deleting a
layer lists what depends on it. Rename, confirm, replace-vs-merge are in-page
dialogs with Enter and Esc. The header says *saved hh:mm* after every autosave
and turns red with the message when a save fails.

**Scene.** Buttons under the north arrow (PLAN N S E W BELOW ISO · FIT ·
ORTHO/PERSP · SLICE · KEY) and Leapfrog's keys: d plan with north up, n s e w,
u from below, i isometric, f / Home fit, o / p projection, l look at the
section (Shift+l flips), ? help. The status bar reads *projection · VE ·
looking az / plunge* — the polarity check from the structural course. The
default sections start hidden and become visible when chosen in the Section
tool. Scenes (camera, projection, VE, visibility, opacity, drape, active
section) save and restore from VIEW ▾. Below 1100 px the panes become
drawers and the banner a strip.

**Tests.** `tools/test_model3d_ui.mjs` drives all of it through
`window.gmApp` and the DOM: the split column, arming and Esc, the menu order
and readiness, no layer on panel open, the key with one class, the pick card,
undo, filter, right-click, keys, scenes, the render image, and a described
model opening on its workings.

## 7. Structural geology — orientation without drillholes

Leapfrog's fundamentals course spends five of six modules on drilling data.
This project usually has none: what it has is a geological map, a DEM, mapped
fault traces, scanned mine plans and a handful of graded mines. The one
Leapfrog course that fits that exactly is *Model From Map*, and its structural
extension. §7 is that workflow.

### 7.1 The measurement

A planar structural measurement is a `points` object with `role: 'structural'`
and three guaranteed columns:

| column | range | meaning |
|---|---|---|
| `dip` | 0 – 90 | degrees below horizontal |
| `dip_azimuth` | 0 – 360 | clockwise from north, **in the down-dip direction** |
| `polarity` | +1 / −1 | right way up / overturned |

Dip and dip azimuth rather than strike, for the reason the guide gives: it
removes the right-hand-rule ambiguity and the regional convention differences,
and it is directly readable by a machine. `normaliseStructural()` accepts
`strike` (converted with the right-hand rule), the usual column synonyms, and
polarity written as `0`/`1`, `±1` or the words `overturned` / `inverted`; it
folds an out-of-range dip back into 0–90 by rotating the azimuth 180° and says
so in a warning rather than silently.

The upward pole of a plane dipping δ toward α is
`[sin α · sin δ, cos α · sin δ, cos δ]` — a plane that faces north has a pole
that tilts north, which is the gradient of the surface. Everything downstream
(stereonet, statistics, form interpolant, trends) works on poles.

Measurements render as oriented discs: `disc sides = 3` gives the triangle
glyph whose apex points down dip, a tick runs down the dip line, and a short
stub along the pole is blue for right-way-up and orange for overturned.
Colour by dip, dip azimuth, polarity, or any column — a category column
selected on the stereonet colours the 3-D scene immediately.

### 7.2 Derivation from a mapped trace (the three-point problem)

`TOOLS ▸ Structural data ▸ DERIVE FROM ALL TRACE LAYERS`, or
`TOOLS ▸ Derive structure from all mapped traces`.

Where a planar contact crosses topography it leaves a trace whose 3-D shape
encodes the plane's orientation. A sliding window along each draped trace is
fitted with a least-squares plane (PCA; the normal is the smallest
eigenvector), and the plane's dip and dip azimuth are read off the normal.

The window **grows** — from `window` up to `max_window` — until the segment
has enough relief and enough spread to determine a plane, so a smooth trace on
gentle ground is looked at over a longer distance rather than being answered
badly. Three gates decide whether a reading is emitted at all:

| gate | default | why |
|---|---|---|
| `min_relief` | 20 m | a trace on flat ground carries no dip information |
| `min_spread` | 25 m | measured **in map view** — a trace that is straight in plan leaves the plane free to rotate about that line however much elevation it gains, and the least-squares answer there is a meaningless near-vertical plane |
| `max_rms` | 15 m | the contact is not planar over this window, or the map and the DEM disagree |

Rejections are counted by reason and shown in the panel. A layer that yields
nothing warns that the ground is too flat rather than returning a dip of zero.
One consequence is worth stating plainly: a genuinely vertical structure also
traces a straight line in plan, so it comes back as *indeterminate* rather than
as 90°. The trace cannot tell "vertical" from "unconstrained" apart, and
guessing 90° would be a fabrication. (This gate matters: run against the real
Cassia geology with the gate on the 3-D spread instead of the plan spread, the
median derived dip was 87° — the degenerate answer. With the plan-view gate it
is 10°, which is what flat-lying Basin-and-Range cover actually does.)

Each surviving point carries `relief_m`, `plan_spread_m`, `fit_rms_m`,
`span_m`, `window_m`, `n_pts`, its source layer and `confidence: 'inferred'`, so a weak reading stays
visibly weak, and a hand-digitised or field measurement placed over the top of
it at `surveyed` confidence outranks it.

The same routine runs on mapped **fault** traces, which is how a fault surface
gets its dip in the Model-From-Map workflow.

### 7.3 Digitising and draping

`POINT + DOWN-DIP (2 clicks)` places a measurement on the ground or on a
georeferenced plate and takes its azimuth from the bearing between the two
clicks; `POINT ONLY` takes a typed azimuth, with `FROM VIEW` to read the
direction you are currently looking along. The panel repeats the course's
orientation check: rotate until the mapped dip tick points to the top of the
screen and confirm the azimuth — a reading 180° out means you are looking at
the symbol backwards.

`SET ELEVATION FROM TOPOGRAPHY` is not optional housekeeping. Measurements
digitised off a flat map have no elevation, sit at or below the model base,
and are then **silently classified as outside the boundary and dropped** when a
surface is built — three pages of the Model From Map course are about exactly
this. The original z is kept in `z_original`.

### 7.4 Declustering

`radius` groups measurements spatially (optionally within a category);
`angular tolerance` discards outliers from the cluster mean; the measurement
closest to the mean survives, weighted by an optional numeric `priority`
column. A cluster too inconsistently oriented to have a meaningful mean is
dropped **whole** and counted — the guide warns about this, so it is surfaced
rather than hidden. Declustering is the supported way to get a large derived
set under the form interpolant's point cap.

### 7.5 Stereonet

Lower hemisphere, equatorial or polar grid, equal-area (Schmidt, the default)
or equal-angle (Wulff). Poles for every dataset, great circles for planar data,
and density contours by:

* **Kamb** — counting circle sized so the expected count is σ² ; contoured in σ
* **exponential Kamb** — Vollmer's smoothly weighted variant
* **Schmidt** — the 1 % area count, contoured in % per 1 % area

`desample` (0–1, default 0.5) thins the *picture* only; every measurement is
always used for the statistics and the contours.

**Bingham** gives the mean plane (whose pole is e1), the best-fit great circle
through the poles (whose pole, e3, *is* the fold hinge — and the best-fit plane
*is* the profile plane, the ideal section through the fold), all three
eigenvalues and Woodcock's K, which classifies the fabric as a cluster or a
girdle. **Fisher** gives the mean, κ, R̄ and α95, with the guide's caveat
surfaced automatically: when the mean dip exceeds 50° the panel says the Fisher
mean is being pulled by the steepest measurements and points at Bingham
instead.

Selection runs both ways. `LASSO ON THE NET` and `CLICK POINTS` pick on the
stereonet; `BOX IN THE SCENE` drags a rectangle over the 3-D view and picks
what falls inside it. Either way `ASSIGN TO CATEGORY` writes a category column
on the *source* layer, which immediately becomes a colour-by option on the
layer, a filter on the net, and a domain for the form interpolant.

Export is PNG (the canvas) or SVG (vector, with the statistics in the caption).

### 7.6 Form interpolant

`TOOLS ▸ Form interpolant & trends`. An RBF whose **gradient** is constrained
by the poles — Lajaunie et al. (1997), the potential-field method, implemented
directly rather than approximated with offset points:

    f(x) = Σⱼ ∇K(x − pⱼ)·cⱼ + g·x       K(r) = r³,  H = 3(r I + d dᵀ / r)

solved for `∇f(pᵢ) = poleᵢ` at every measurement, with a constant drift and the
side condition `Σ cⱼ = 0` that the cubic kernel needs. The level sets of `f`
are therefore everywhere tangent to the measured planes: they *are* the form
surfaces. Absolute values are meaningless — they are shifted so the centre of
the box is zero, and the thresholds label surfaces rather than dating them,
exactly as the guide says.

The panel reports the maximum and mean angle between the reproduced gradient
and the measured pole; anything above about half a degree means the fit is not
honouring the data and the smoothing or the point cap needs attention.
`also evaluate onto topography` writes the interpolant onto the topography grid
as a property layer — the map-view **form lines**.

Limits, stated in the panel and worth repeating: form interpolants are blind to
faults and to intrusions that truncate the fabric, because they only see
planar structural data; and they are very sensitive to clustering, which is why
declustering comes first.

### 7.7 Trends

A **structural trend** is built from structural measurements, meshes, or mapped
fault traces. At any point the nearest input supplies the local plane, and the
anisotropy ratio starts at `strength` on the input and **halves every
`range`** — 5:5:1 on the surface, 2.5:2.5:1 at one range, 1.25:1.25:1 at two,
approaching but never reaching isotropic, and floored at 1:1:1 once it passes
`log₂(strength)` ranges (about 2.3 ranges at the default strength, which is why
the guide calls 3× the range "practically indistinguishable from isotropic").
Glyph size encodes local strength, so the extent of the trend is visible, and
glyphs are simply omitted where it has decayed away.

A **global trend** is the single-plane version: dip, dip azimuth and *pitch*
(the direction of maximum continuity, measured in the plane from strike) plus
`Ellipsoid Ratios`, defaulting to Leapfrog's 3, 3, 1. `SET FROM THE BINGHAM
MEAN PLANE` takes it from the data — and says so when the data is a girdle,
because one plane cannot describe a fold and a structural trend will. It is
stored on the project as `metadata.global_trend` and exposed to the other tools
through `globalAnisotropy()`.

### 7.8 What is here since 2026-09-02, and what is deliberately not

Built in the Leapfrog-parity pass ([`LEAPFROG-PARITY.md`](LEAPFROG-PARITY.md)):

* **A vein or fault sheet from a trace** — `TOOLS ▾ > Project a trace down
  dip` (`gm-geom-tools.js`, engine `extrudePolyline` / `extrude_polyline`):
  a mapped or traced line plus a dip becomes a ribbon (open trace) or a
  sheared prism (closed outline), with `VERTICAL WALL` for dip 90. The dip
  comes from a document (described), from the Bingham mean of the readings
  derived along that very part (inferred), or is typed (assumed); the mesh's
  confidence is the weaker of the trace's and the dip's, the depth is stated
  in the name as the user's projection distance, and a dip azimuth within
  20° of the trace's strike is refused with the strike printed rather than
  producing a degenerate sheet. This is the fault surface of the Model From
  Map course without the fault blocks.
* **A plane from one measurement** — `Plane from a measurement`: a finite
  rectangle with a stated attitude through a structural point or a typed
  location, role vein or fault, labelled a statement of attitude, not a
  modelled surface (the same corners as `assay.vein_surface`).
* **Contours** of any grid (topography, pancake bases, form-interpolant
  fields, property grids), draped where the grid is a property.
* **A contact from a mapped unit boundary** as a stratigraphic base (source
  kind *trace* in the Stratigraphy tool), with the warning that a heightfield
  through one draped trace carries no dip away from the line; faults are
  refused as bases.
* **Traced faults, contacts and veins** (`Trace a fault / contact / vein`),
  **measurements** (`Measure`) and **pinned notes with a source** (`Notes`).
* **Rock for every site.** The map hands the modeller the USGS geology
  polygons and fault traces it has loaded around the point (national SGMC
  tiles, or a state survey's map where one replaces them), and `gm-site.js`
  drapes them exactly as it drapes an AOI bundle — unit meshes, outlines,
  faults — with the unit's age read into `t0 / t1` from `age_min / age_max`
  or parsed from the age text with a geologic time table (an unreadable age
  is a warning, never a guess). A site inside an AOI bundle keeps the bundle
  and says the hand-off was skipped, so no contact is drawn twice. With
  USGS GEOLOGY off on the map, the hand-off says so instead of pretending.
* **Mine features for every site.** USMIN points (shafts, adits, prospect
  pits, open pits, dumps, mills) come through the same hand-off as a
  `features` layer drawn as type glyphs — square, triangle turned to the
  adit's mapped azimuth, circle, diamond, hexagon, cross — labelled as
  surface locations digitised from topographic maps: no depth, no extent.
* **Model the rock from the map** (`TOOLS ▾`, and the one-click button on
  the WHERE THINGS STAND card): Leapfrog's Model From Map made automatic and
  honest. Units are ordered youngest-first by age; the vertices where a
  unit's draped outline meets an older unit's are its contact points; each
  contact takes the nearest orientation derived along a trace (§7.2) within
  300 m and gets a point 100 m down dip; the existing pancake builder then
  makes base surfaces and closed unit volumes, all `provenance.confidence =
  inferred`, `method: model from map`. A unit touching nothing older, or with
  fewer than three contacts, is skipped and named; with no readings anywhere
  the bases follow the contacts at the surface and every base says so; ties
  in age are not contacted against each other; readings derived along faults
  are excluded and the faults are declared not honoured (fault blocks stay
  out of scope, §7.8). The RESULT block lists every count.
* **Water level as stated** — an elevation or *below the collar* with a
  source → a horizontal plane (role `water`), *described* with a source and
  *assumed* without, never a computed head.
* Set elevation for any layer with a scope that never touches surveyed rows;
  implicit surfaces clipped below the topography with the daylight line
  computed; the global trend restored on reload and applied by the Implicit
  tool (the anisotropic RBF now shares one length unit with the isotropic
  one); thick slices; section drawings exported as PNG and SVG with their
  key, sources and the NOT A SURVEY line; section images drawn in the 2-D
  panel.

Still deliberately not here: a **structural surface** in Leapfrog's sense (a
potential-field solve that honours contact points *and* off-contact
orientations at once — the form interpolant honours orientations, the
pancake honours contacts, and joining them is the next engine phase), **fault
blocks and fault-against-fault terminations** (they need mesh-against-mesh
trimming and a chronology, and without drillholes there is nothing to
terminate against but a guess), **vein systems with terminations**, and
**rose diagrams**.
