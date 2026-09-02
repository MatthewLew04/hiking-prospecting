# Leapfrog parity — what the 3-D modeller has, lacks, and will not do

_Written 2026-09-02 from a read of 23 Seequent documents (the Leapfrog Geo
fundamentals course guides 1–5, the structural modelling course guides 1–6,
the Model From Map, Vein Modelling, Advanced Surface Editing, Advanced
Stratigraphic Modelling, Geophysical Data and Hydrogeology course guides, the
Geo and Edge workflow sheets, the two white papers, the solution brief and
the structural-modelling supplementary slides — 1,322 distinct features and
280 UI conventions) against the modeller's own source (465 implemented
features, 262 recorded usability faults). Every claimed gap was checked
against the code before it was allowed to stand; the tables below say what
was verified, what was built in this pass, and what is deferred and why._

This is the roadmap `GEOMODEL.md` §5 refers to. The honesty rules of
`GEOMODEL.md` §4 bind every item here: nothing is invented, a described
working never draws like a surveyed one, and a feature that would need data
the district does not have is refused rather than faked.

## 1. The user this is for

Leapfrog's fundamentals course spends five of six modules on drilling data.
This project's user has none. What they have is a geological map, a DEM,
mapped fault traces, scanned mine plans and sections, historic prose, a few
cited grades, and BLM claim centroids. The one Leapfrog course that fits that
exactly is *Model From Map* with its structural extension, and that is the
workflow the modeller is built around (`GEOMODEL.md` §7). So every Leapfrog
feature was scored by its value **without drillholes** (1–5), and anything
that only means something with drilling was set aside at value 1–2 without
spending verification effort on it (§6 below).

## 2. What the modeller already had

| area | in the modeller before this pass |
|---|---|
| structural | planar structural data with the dip / dip-azimuth / polarity contract; derivation of orientation from where mapped traces cross the DEM (the three-point problem with relief / spread / RMS gates); digitising on the draped map; declustering; a lower-hemisphere stereonet with Kamb, exponential Kamb and Schmidt contours, Bingham and Fisher statistics, selection linked both ways; a gradient-constrained form interpolant with form surfaces and form lines; structural trend fields; a global trend plane |
| geological | draped map geology and faults on real terrain; pancake stratigraphy from contact points / grids / constants with deposit and erosion rules, unit volumes, virtual drillhole; implicit RBF surfaces from signed points; workings digitised from georeferenced plans and sections (adits, drifts, shafts, raises, stopes) and parsed from prose with per-element confidence |
| numeric | block models, experimental variograms and fits, ordinary kriging / IDW / nearest with search and domains, cut-offs and grade–tonnage |
| drilling | collar / survey / interval import with minimum-curvature desurvey, interval display, samples to points, LAS logs — present, but not the district's data |
| outputs | vertical sections with clipping, intersection lines, pancake fill, block slices, nearby workings; a 2-D section strip; DXF export; screenshots; scale bar in orthographic |
| formats | OMF v0.9 and v2.0, DXF, OBJ, GOCAD, Leapfrog .msh, Surfer / Geosoft / GXF / Arc ASCII / ZMAP / Irap grids, UBC, CSV tables, Geosoft XYZ, SEG-Y, LAS, GeoJSON, images and PDF pages |

## 3. Verified gaps that matter without drillholes (value 4–5)

Each of these was checked against the source by an agent trying to prove it
already existed, and by a second one trying to prove it worthless or
dishonest for this user. Value and effort are the adjusted figures.

| id | value | effort | gap | status |
|---|---|---|---|---|
| G-01 | 4 | S | Clickable provenance in the inspector and PICKED section: source layer -> select it, source document / page / URL -> open it, image plane -> zoom to it, any URL-typed column as a link, and a citation line beside the confidence class of every picked working | shipped: PICKED card with source sentence and links, PROVENANCE links, hover readout while a tool is open |
| G-02 | 4 | M | Extrude any traced or mapped line down dip: vein surface from a vein trace plus a stated dip (outcrop trace projected to depth), and extruded walls / prisms from any polyline (claim or outline walls, to an elevation or depth, along a dip) | shipped: `extrudePolyline` in both engines + the Project a trace down dip tool (stated / derived / typed dip, confidence the weaker of trace and dip, degenerate azimuth refused) |
| G-03 | 4 | S | Warning and status badges on tree objects (red/orange exclamation, [empty], build-failed, greyed while processing) | shipped: ⚠ / ✕ / ∅ badges on rows, site warnings toasted |
| G-04 | 4 | M | Export the 2-D section drawing as PNG / SVG | shipped: EXPORT PNG / SVG of the 2-D section with key, sources and the NOT A SURVEY line |
| G-05 | 5 | S | Scene picking fidelity: tubes resolve to their feature / interval row (face -> part map), pick through translucent terrain, pixel-consistent thresholds, section quad inert outside its tool, and a drillholes pick readout (hole, from, to, values, open table) | shipped: tubes resolve to their feature, translucent context yields to data behind it, pixel-consistent thresholds |
| G-06 | 4 | M | Sample-table contract for cited grades: value, unit, width / support, basis (selected vs average vs plain), source — honoured by display, filters, compositing and kriging | deferred |
| G-07 | 4 | M | Contact surface from a mapped unit boundary (From GIS Vector Data / Contact Points refine) | shipped: unit base from a mapped unit boundary (source kind *trace*, faults refused, no-dip warning) |
| G-09 | 4 | M | Tool panel vs layer inspector: the Shape List / Properties Panel / tool-window separation | shipped: layer inspector above a closable tool host; one arm() path with the mode strip; Esc semantics |
| G-10 | 4 | M | Undo / redo and per-element delete in digitising tools | shipped (layers, sections, workings features): delete goes through a toast with UNDO, Ctrl/Cmd+Z; per-measurement delete is in the structural panel |
| G-11 | 4 | M | Render Image with overlays (legend, confidence key, scale bar, north arrow, NOT A SURVEY banner, project / CRS / VE / date footer) at a chosen size with supersampling, download and copy to clipboard | shipped: VIEW ▾ > Render image writes scale bar, confidence key, NOT A SURVEY sentence, north, view direction and a CRS/VE footer into the PNG; the kit zip includes it |
| G-12 | 5 | M | Distance function and distance buffers (distance to workings, faults, contacts, mines, samples) | deferred |
| G-13 | 5 | M | Plan view / level plan (horizontal section at an elevation) | not built: a horizontal clip is the Section tool with a plan camera (d) — a true level-plan drawing is deferred |
| G-14 | 5 | M | Fault surface from a mapped or digitised trace (Surface or Vertical Wall), honouring dips | shipped: a fault sheet is Project a trace down dip with role fault (no fault blocks) |
| G-15 | 5 | L | Contact / structural surface from map contacts and structural disks: a potential-field solve with value (contact) and gradient (pole) constraints - New Deposit > From Structural Data (on-contact disks alone) and Structural Surface (contacts plus off-contact measurements) - promotable to a unit base | deferred |
| G-16 | 4 | S | Offset surfaces from any reference mesh (constant offset, direction, true thickness, chaining upward from a known base) | deferred |
| G-17 | 4 | S | Build diagnostics and stratigraphic error reporting (pinch-out errors, surface inconsistencies, leftover Unknown) | deferred |
| G-18 | 4 | S | Multiple inputs per surface (Add > points / polylines / GIS lines; per-input enable, boundary filter and query filter) | deferred |
| G-19 | 4 | S | Mesh / output-volume properties: volume, area, connected parts, closed / manifold check with SHOW OPEN EDGES, tonnes at a density for stopes and units, workings by level, the iso-shell Interval / Volume / Mean / Units report, and a unit x body cross-tab | deferred |
| G-20 | 4 | S | Confidence styling and banner counting for modelled surfaces and bodies (described vein planes and projected surfaces draw like surveyed meshes) | shipped for extruded and plane meshes (metadata.confidence, provenance); the banner still counts workings only |
| G-21 | 4 | S | Property grids are profiled as elevations on sections (a magnetics grid draws a line at Z = nT) | deferred |
| G-22 | 4 | S | Coordinate-system and unit check when a grid is imported (no-reprojection rule made visible) | shipped: the grid-import dialog states the project CRS and flags a degree-looking extent |
| G-23 | 4 | S | Project Notes and per-object Comments (editable, exported) | shipped: per-layer NOTES saved with the project and written into the kit README |
| G-24 | 4 | M | Complete, stacking in-scene legends: per-layer legend toggle, keys for working types, claims status, role colours and every visible draped map unit, the confidence key always available, rebuilt only on change, with a font-size setting | shipped: the confidence key shows whenever any working is on screen, working types and claims are keyed; per-layer legend pins are deferred |
| G-25 | 4 | S | Measure distances (ruler) between picked points or objects: 3-D length, plan length, delta z, bearing and plunge in m and ft, multi-segment, keep as an annotation line | shipped: Measure (3-D, plan, Δz, bearing, plunge in m and ft; keep as an annotation line) |
| G-26 | 4 | S | Click-to-inspect details panel: placement, feature highlight, zoom-to-feature, locate in tree | shipped: PICKED card under the layer title, highlight overlay, ZOOM TO FEATURE, tree row scrolled into view |
| G-29 | 4 | S | Save / autosave feedback and destructive-action semantics (delete, replace-vs-merge, start screen, unguarded PROJECTS menu) | shipped: saved-at chip, save-failure toast, REPLACE / MERGE / CANCEL, guarded PROJECTS menu, delete closes the project, start screen reopens from the brand |
| G-32 | 4 | M | Labels for workings, levels, meshes and sections (Show Text) with formatting and limits stated | shipped: labels for linesets, meshes and sections, decluttered by priority, cap stated |
| G-33 | 4 | S | Transparency that preserves confidence styling; dash patterns in screen space | shipped: opacity multiplies a stored base, dashes are pixels rescaled with zoom |
| G-34 | 4 | S | Finite plane from one stated attitude in the viewer (vein wall / fault wall for sectioning) | shipped: `planeMesh` in both engines + Plane from a measurement tool + MAKE A PLANE in the structural panel |
| G-36 | 4 | M | Invalid Value Handling for numeric columns ('<0.02', 'tr', '-', 'nd', 'bdl', negative / zero / infinite): per-column detection and counts, Omit / Replace / Keep rules, reviewed flag, rules stored with the layer and applied by every model | deferred |
| G-37 | 4 | M | Table viewer / editor for any tabular layer (points, structural, workings features, collar, survey, intervals): sortable virtualised grid, find / filter, row <-> scene <-> stereonet linkage, column -> colour-by / label-by, edit cells with an audit log, per-row delete / undo last, export CSV | shipped (read-only): TABLE… on points and linesets — sortable, filterable, row → zoom + highlight, CSV export; editing is deferred |
| G-38 | 4 | M | Query and display filters on any layer (Build Query: column / test / value, AND/OR; numeric range; per-category tick-list visibility), saved per layer, honoured by rendering, picking, legend counts, the 2-D panel, the stereonet and every builder's sample / input selector, with 'showing n of m' and unit / basis awareness for estimates | deferred |
| G-40 | 4 | M | GeoTIFF and georeferenced raster import: TIFF / GeoTIFF reader (DEMs, geophysical grids, georeferenced map / photo rasters via embedded tags), world-file sidecars (.tfw/.pgw/.jgw/.wld), batch images, a crop tool, and import-time three-marker georeference with in-scene registration | shipped (world files): an image dropped with its .tfw/.pgw/.jgw/.wld is placed from the affine terms; a GeoTIFF reader is deferred |
| G-43 | 4 | M | General polyline drawing and editing tool on any object (draped map, georeferenced plate, section image, slicer): trace a fault / contact / vein / interpretation line, drag / insert / delete nodes, split, simplify, close / open, re-drape, snap, Shift to navigate, save with an edit log | shipped: Trace a fault / contact / vein (sketched, on the ground or a plate; feeds DERIVE) |
| G-44 | 4 | M | Global trend / anisotropy actually applied: a trend source selector (none / project global trend / typed dip-azimuth-pitch-ratios / Bingham mean of a structural layer / described vein attitude) wired into the implicit surface, kriging and stratigraphy bases, with dip and plunge exposed, the RBF anisotropic scaling fixed, and the saved trend reloaded | shipped: RBF anisotropy shares one length unit, global trend restored on reload and applied by the Implicit tool (kriging not yet) |
| G-49 | 4 | M | Contour lines from any 2-D grid (topography, pancake bases, form-interpolant fields, kriged / magnetic / radiometric / thickness property grids) in the scene, on level plans and the section panel, with labels, drape and DXF / GeoJSON / send-to-map export | shipped: `contourGrid` in both engines, the Contours tool, CONTOURS… on every grid |
| G-52 | 4 | M | Slicer navigation and parity: thick slice (slab between two planes, asymmetric), step keys and step size, look-at-slice key, numeric position, axis presets, ALIGN TO VIEW creation, per-object slice mode (sliced / unsliced), slider range from the model, persisted slicer state | shipped: thick slice (a slab of the band width), slider range from the project bounds, editable z range; step keys deferred |
| G-54 | 4 | M | Section sheet layout: title block, scale, legend group, plan inset, comments | deferred |
| G-55 | 4 | M | Long section along a polyline (vein or drift trace) | deferred |
| G-56 | 5 | M | Section from a georeferenced section image, and image planes drawn in the 2-D panel | shipped: SECTION ON THIS IMAGE and in-plane section images drawn in the 2-D panel |
| G-58 | 4 | M | Annotations: text notes and markers pinned in the scene with a source quote | shipped: Notes (a points layer with text, source, page, url; labels forced on) |
| G-60 | 5 | L | Interpret on sections and images: draw polylines on a scanned / SEG-Y section image, the section plane or the slicer and use them as surface inputs - contact points for a stratigraphy unit, signed points for the implicit surface (offset along the plane normal), fault / contact traces for structural derivation and a fault surface - with tangent ribbons, provenance and rebuild | deferred |
| G-70 | 4 | M | Exact clipping toggle / show unclipped surface (judge a surface's fit above topography; clip implicit surfaces to topography and extents) | shipped: `clipMeshToTopography` + daylight trace, clip below topography on the Implicit panel |
| G-72 | 4 | S | Input classification before building (inputs outside the boundary or above topography, Set Elevation for any contact layer, minimum data check) | shipped: input classification per unit, DRAPE for no-elevation points, refusal below 3 distinct points |
| G-76 | 4 | M | Water-table / drainage-level surface from prose ('water at the 300 level', 'the adit drains the workings') or a typed elevation | deferred |
| G-91 | 4 | M | Set Elevation / drape on topography for any layer (points, structural, drillhole collars, linesets, plan images): surface picker (topography, any grid, any mesh), offset, scope (only missing z / only confidence = ...), keep z_original and RESTORE, true image drape on the terrain | shipped: `setElevationFrom` for points, linesets and collars, scopes that never touch surveyed rows, RESTORE |
| G-97 | 4 | M | Model boundary object shared by every builder (extents with handles, Enclose Object, lateral extent polygon, base / z range, topography cap) and per-interpolant boundaries (bounding box + pad, inside a closed mesh / unit volume / fault block, within a distance of samples, clip below topography, boundary filter) | shipped (step 1): z from / to, clip below topography and iso value on the Implicit panel; a shared boundary object is deferred |

## 4. Verified gaps of moderate value (value 3)

Real, honest, and useful, but each below the line for this pass. They are
candidates for the next one, in this order.

| id | value | effort | gap | status |
|---|---|---|---|---|
| G-08 | 3 | S | Public regional geophysics (USGS magnetic anomaly, NURE radiometric K) draped on the 3-D terrain | deferred |
| G-27 | 3 | S | Hover readout while a tool panel is open | shipped: the readout is suppressed only while a click mode is armed |
| G-28 | 3 | S | View azimuth / plunge readout and orientation overlays: continuous 'looking az / plunge' text (the polarity check), axis triad with E/N/Z labels, labelled grid in orthographic plan, true north-up top view, and VIEW > Overlays toggles | shipped: status bar reads projection · VE · looking az / plunge; north arrow is a button |
| G-30 | 3 | S | First-time-user path: workflow-ordered TOOLS menu, a start-here card, one canonical tool name everywhere | shipped: TOOLS ▾ in workflow order with readiness, WHERE THINGS STAND card, HELP > THE ORDER |
| G-31 | 3 | S | Domained estimation that restricts the samples, not only the target blocks (hard boundaries) | deferred |
| G-35 | 3 | S | Stereonet statistics per selection, per category and per dataset | deferred |
| G-39 | 3 | S | GIS vector import: shapefile (.shp/.dbf/.prj), KML/KMZ, GeoPackage, MapInfo; 'Filter data' clip; polygons kept as polygons; drape toggle; GIS views | deferred |
| G-41 | 3 | M | Planned (virtual) drillholes: saved planned holes (collar or target, dip, azimuth, length, lift / drift), offset series and grid, evaluated against every model along the inclined trace (stratigraphy units, block attributes, property grids, distance fields, implicit / stope / unit meshes, workings intersections and their confidence), drilling prognosis table, CSV import / export | deferred |
| G-42 | 3 | M | Vein hangingwall/footwall from a reference surface plus thickness (medial plane, min/max thickness, pinch-out) | deferred |
| G-45 | 3 | M | Dependency tracking: input links under every built object, View Relationships in both directions, dependency-aware delete (Confirm Delete lists dependents that will be removed or left stale), stale badges when an input changed or is missing, and one-click REBUILD STALE from stored parameters | deferred |
| G-46 | 3 | M | Categorical evaluation / tagging of block models, grids and points beyond the pancake: inside / outside any closed mesh (vein shells, stope prisms = mined out, pit shells, depletion), mapped map-unit polygon at surface, distance class, combined categories, used as estimation and tonnage domains, with a per-unit x boundary volume report and a click readout of every evaluation | deferred |
| G-47 | 3 | M | Numeric colourmap editor for grids, points, meshes, structural and block layers: fixed range clamp (min / max, percentile stretch, histogram), value transform (linear / log10 / signed log / quantile), invert and centre-on-zero with a diverging default for signed data, discrete class intervals (equal / quantile / log / k-means / progressive-double / manual, editable bounds, inclusivity, a highlighted zero / contact / threshold interval), a cyclic map for azimuths, project-level named colourmaps applicable to any layer and written to the legend | deferred |
| G-48 | 3 | M | Evaluations: sample any numeric model, property grid, form interpolant / trend field, RBF field, distance field or block attribute onto surfaces, image planes, points, topography, block models and sections, as a named attribute with provenance and colour-by | deferred |
| G-50 | 3 | M | Interpolate any points layer to a draped 2-D property grid (soil / rock-chip geochem, cited grades in map view, Geosoft XYZ survey channels) with method, cell size, search radius and provenance; draw line-organised point data as flight / survey lines | deferred |
| G-51 | 3 | M | Saved scenes / camera bookmarks (camera + projection + VE + layer visibility, opacity and display + active section, offset, side, slice and band + imagery + legend toggle), restorable, updatable, deletable, and deep-linkable by URL | shipped: VIEW ▾ > SCENES save and restore camera, projection, VE, visibility, opacity, drape and the active section |
| G-53 | 3 | M | Processing panel: running tasks with progress, CANCEL (terminate and respawn the worker), timeouts, errors that jump to the object, a pre-run cost estimate and point / node caps for interpolants, and the remaining main-thread loops moved off-thread | deferred |
| G-57 | 3 | M | Structural data on sections: apparent-dip ticks within a distance band, dip / dip-direction labels, per-layer distance filter, band mode for the 3-D clip | deferred |
| G-59 | 3 | M | Rose diagram of strikes / trace bearings | deferred |
| G-61 | 3 | L | Fault blocks: partition surfaces, stratigraphy and block models by active faults (per-block surfaces with boundary filter, Copy Chronology To, block boundaries, stated throw, per-block vein Outside lithology) | deferred |
| G-62 | 3 | S | Column mapping and 'Import As' types honoured for every table kind (X/Y/Z pickers, Numeric / Category / Text / Date) | deferred |
| G-63 | 3 | S | Display by value for points and drillhole intervals: size / cylinder radius by a numeric column (log option, min / max px), value-range display filter, highlight top N % / 'enhance high values', size legend, depth-tested solid points | deferred |
| G-64 | 3 | S | Points and structural table validation: duplicate / coincident rows, missing coordinates, out-of-bounds, 'Ignore duplicated rows' | deferred |
| G-65 | 3 | S | Export of image planes and sections (image + world file, section sidecar), per-part mesh export, and export warnings shown to the user (layers skipped by OMF / DXF, drillhole intervals reduced to traces, kit README import click-path) | deferred |
| G-66 | 3 | S | Import Column: join extra columns onto an existing points / structural / mines layer from a CSV (by id or by coordinates) | deferred |
| G-68 | 3 | S | Per-unit thickness limits (Min/Max offset limits, pinch-out control, disable limits) | deferred |
| G-69 | 3 | M | Two-sided colouring of contact, fault and vein surfaces (younger / older lithology, hangingwall / footwall) with side identification on pick, both swatches in the legend, and Swap Younging Side | deferred |
| G-71 | 3 | M | Model lithology list (define lithologies manually with colours and chronological order; seed from map units) | deferred |
| G-73 | 3 | S | A Geophysical Data group in the layer tree with editable band name, units and role | deferred |
| G-74 | 3 | S | Property-grid legend with quantity and units, and an honest opacity control for draped grids | deferred |
| G-77 | 3 | S | View presets, hotkeys and hotkey hygiene: true plan with north up (d), s / e / w, l / Shift+l look at the section, u from below, Home fit, Hide all / Show all, Leapfrog capital aliases; modifier and focus guards, tiered Esc, one generated HOTKEYS table in VIEW and HELP | shipped: d n s e w u i f Home o p l keys, look-from buttons |
| G-78 | 3 | S | Orthographic by default, persisted projection and VE, and an honest VE readout | shipped (readout): the status bar says which projection and that the scale bar is nominal in perspective; orthographic-by-default is deferred |
| G-84 | 3 | S | NOT A SURVEY banner: recall, per-project reset, collapsed state | shipped: VIEW ▾ toggles the banner; it resets per project |
| G-87 | 3 | S | 2-D flattened section export for CAD (distance, elevation) and section line to the map | deferred |
| G-88 | 3 | M | 2-D section panel usability: looking direction / swap front, editable z-range, resizable strip, pan and zoom, print scale | shipped: resizable strip, editable z range; pan / zoom in the strip deferred |
| G-89 | 3 | S | Face-dip and dip-azimuth colouring of topography and meshes | deferred |
| G-90 | 3 | S | Contour legend with labelled σ / % levels and contouring controls | deferred |
| G-92 | 3 | M | Scene / stereonet selection kit for any points, structural or workings layer: box, paint / brush (width, Ctrl to deselect, occlusion-aware), lasso and bullseye on the net with Esc cancel and closing hint, Invert, 'selected N of M', Assign to category / new category, Save, orbit kept alive (Shift-drag) | deferred |
| G-93 | 3 | M | Linear structural data (lineations: plunge / trend) as a first-class type - import with PLUNGE/TREND recognised, arrow glyphs, stereonet points, Fisher statistics - and stop misreading them as planes | deferred |
| G-94 | 3 | M | Univariate statistics and graphs for numeric columns of any table (histogram with bin width / log / cumulative, log-probability, box plot by category, Table of Statistics with basis breakdown, interval-length and length-weighted statistics, bin-to-scene highlighting, copy CSV) | deferred |
| G-95 | 3 | M | Vein / surface lateral boundary (Vein Boundary edited with a polyline, adjust plane, limit to data extent) | deferred |
| G-96 | 3 | M | Intrusion and vein bodies in the surface chronology (cross-cutting bodies that replace the units they cut; output volumes) | deferred |
| G-100 | 3 | M | Grid import options (clip to model extent, resolution, no-data) and a post-import RESAMPLE / REGRID | deferred |
| G-101 | 3 | M | Georeferenced plan export and send-to-map raster: overhead orthographic render (grid, scale bar, legend margin) written as PNG + world file / .prj (optionally GeoTIFF), and a property grid or contours pushed back to the map as a georeferenced image overlay | deferred |
| G-102 | 3 | M | Edit Colours for category columns: per-value colour chip and visibility eye, a shared project palette so one unit or set has one colour everywhere (draped geology, strat volumes, block 'unit', section ribbons, legend, stereonet), seeded from the published map legend, with palette import / export | deferred |
| G-104 | 3 | M | Group-level colour-by and legend for draped map geology (colour every unit by age or lithology, one key) | deferred |
| G-106 | 3 | M | Estimator attributes (status, sample count, nearest-sample distance) and per-block interrogation | deferred |
| G-108 | 3 | M | Parameter report and audit trail for every numeric product (exportable, with the honesty statement) | deferred |
| G-109 | 3 | M | Inclined section plane (dip / dip azimuth), e.g. along a described vein | deferred |
| G-110 | 3 | S | Share / export a scene for licence-free viewing (self-contained bundle or link) | deferred |
| G-112 | 3 | M | Described fault from prose (stated strike and dip) drawn as a fault plane in the narrative pipeline | deferred |
| G-113 | 3 | M | Use-Polarity toggle and an explicit 'unknown' polarity for trace-derived readings | deferred |
| G-114 | 3 | M | Stereonet data display: per-category colour and visibility, display filter, query filter, per-dataset toggles | deferred |

## 5. Refuted

Three claimed gaps did not survive: either the honest version is impossible
without drillholes and mesh-against-mesh machinery this engine does not
have, or the capability is a container for features the district cannot
feed.

| id | gap | why |
|---|---|---|
| G-105 | Numeric RBF interpolant tool (XYZ + value points -> scalar field -> iso-value shells) | Lens 1: the engine half is real and unused for numeric values — RBF with linear/cubic/thin_plate/gaussian/spheroidal/multiquadric kernels, spheroidal range/sill, drift, smoothing, anisotropy (gm-engine.js:45, 597-646, 608-720), worker ops rbfFit/rbfPredict/sca |
| G-115 | Vein system with terminations (splays ending against a master vein, HW/FW side, chronological order) | Lens 1 (nothing): no vein container, no termination, no mesh-by-field clipping; every implicit surface is an independent mesh (site/assets/geomodel/gm-tools.js:535-550) whose metadata.implicit stores parameters but not the RBF (gm-engine.js:1929) although RBF. |
| G-118 | Fault system: chronology ordering and fault-against-fault terminations | Lens 1 (nothing): KINDS has no faultsystem (site/assets/geomodel/gm-core.js:316; pipelines/geomodel/model.py:988), there is no clipMeshBySurface or any mesh-against-mesh trimming in gm-engine.js (the export list at lines 59-2012 has only the pancake chronology |

## 6. Set aside: drillhole-only, or verified down to value 1–2

Listed so nobody re-discovers them. Most are Leapfrog's drilling machinery
(compositing, back-flagging, interval selection, correlation views, drilling
prognoses, flow models) — right for an operator with core, meaningless for
a map, a DEM and old plans. The rest were verified as marginal for this user.

| id | gap |
|---|---|
| G-67 | Surface Chronology dialog: activate / deactivate surfaces, [inactive] surfaces kept without cutting, background lithology |
| G-75 | UBC mesh + model pairing on import (3-D inversion grids) |
| G-79 | User groups / subfolders, move between groups, persistent collapse, tree search |
| G-80 | New-project CRS choice, unique project identity, duplicate / Save As |
| G-81 | Contextual help: '?' per panel, tooltips on every header button and tree tag, HELP generated from the same tables |
| G-82 | Keyboard and pointer support in menus, modals and toasts; styled rename/confirm dialogs |
| G-83 | Properties-panel stability: in-place updates instead of full rebuilds, throttled sliders |
| G-85 | Value transform (log with pre-log shift) and pre-transform clipping / capping with a histogram |
| G-86 | Serial (fence) sections |
| G-98 | I/J/K index filter, single-slab / one-bench stepping, cell edges and greyed inactive cells for block models and 3-D grids |
| G-99 | Iso-surfaces and enclosed volumes (Higher / Lower / Interval shells) from a block model attribute or an imported voxel (the IDW grid interpolant on gridded geophysics or a kriged block model) |
| G-103 | Bulk selection and operations in the layer tree (multi-select, select-all-of-kind, solo / hide others, clear scene) |
| G-107 | Block model calculations and filters (typed variables, expressions, Category From Numeric) |
| G-111 | Fault-zone (damage zone) volume of stated width |
| G-116 | Structural (spatially varying) trend applied to an interpolant or estimator: contact surfaces and kriging whose anisotropy follows a curved fabric, with an outside value / ratio floor |
| G-117 | Live in-scene preview of dialog geometry with drag handles: block-grid extents box, section plane widget, image-plane corners, and an interactive moving plane / draw-plane line for setting the global trend (SET FROM PLANE) |
| G-119 | Drillhole validators: collar max-depth exceeded, overlapping segments, gaps, duplicate collar/survey, wedges and re-drills, survey deviation severity |
| G-120 | Categorical (lithology) interval colouring with a category legend and editable colours |
| G-121 | Per-surface resolution, smoothing and snapping controls (surface resolution, adaptive, Snap to data, maximum snap distance) |
| G-122 | Copy / static copy of surfaces and models for side-by-side experiments |
| G-123 | Topography from points, fixed elevation, merged grids and added height data |
| G-124 | Vein and intrusion drillhole machinery (vein segments and midpoints, HW/FW auto-assignment, end-of-hole points, multiple intersections, category compositing, point generation, value clipping) - mostly drillhole-only |
| G-125 | Geological model container in the tree (Boundary / Lithologies / Surface Chronology / Output Volumes nesting, whole-model visibility, per-volume control) |
| G-126 | Surface-issue diagnostics after a build (parts, closed, extent versus data, aspect) with the matching fix, per the course's symptom table |
| G-127 | Import-time threshold and no-data handling for 3-D grids |
| G-128 | Grid export options: Surfer variant, Geosoft data type and .gi sidecar note, axis-aligned resample for rotated grids |
| G-129 | Rotation-centre control: anchor on click, visible pivot, Home reset |
| G-130 | Mesh display modes: independent faces / smooth / edges toggles |
| G-131 | Variogram tooling: directional / axis / radial variograms, a plot that matches the anisotropic model, fit quality, and no silent default variogram |
| G-132 | Grade-tonnage: domain restriction, attribute choice, per-unit density, curve, export and recorded assumptions |
| G-133 | Contour polylines / added values as an explicit, flagged manual interpretation on a numeric model |
| G-134 | Declustering of numeric samples (cell declustering weights, declustered mean) |
| G-135 | Batch export of section sheets (zip / multi-page) |
| G-136 | Graph and table outputs that match the screen: stereonet SVG = canvas (contours, alpha95 cone, selection, polar net, labels, colour bar) and PDF, variogram plot and grade-tonnage table EXPORT PNG / CSV / COPY |
| G-137 | Editable form-surface thresholds, named interpolants kept side by side, resolution control |
| G-138 | Stereonet contouring and statistics off the main thread for large derived sets |
| G-139 | True spatial search radius in declustering (and exposed priority / keep-fraction parameters) |
| G-140 | One-step drillhole set import (collar + survey + intervals in one drop, filename detection, preview) |
| G-141 | Colour-gradient file import (Geosoft .tbl, Surfer / MapInfo .clr, ER Mapper .lut), colourmap export / import / share between layers, and OMF colormap round-trip |
| G-142 | SEG-Y import wizard: depth vs time, datum / z top-bottom, byte locations, endian, clip, slice selection |
| G-143 | Per-unit property table (density, susceptibility, hydraulic K) that writes block attributes and feeds grade–tonnage |
| G-144 | Block grid definition: explicit origin / size / count / z range, extent from any layer ('enclose layer...' + pad) or a rectangle drawn in plan, azimuth from the global trend strike or the described vein strike, a preview outline before CREATE, and local refinement as a finer block model clipped to a buffer around workings / faults / veins |
| G-145 | Colour scheme (white background / print theme for figures and exports), UI and legend font size, resizable panels |
| G-146 | Estimator search options: sector (octant) search, outlier restriction, drillhole limit, block discretisation |
| G-147 | Indicator RBF interpolant (extent / probability above a cut-off) |
| G-148 | Multi-domained interpolants and non-destructive re-runs (one model per unit, results kept side by side) |
| G-149 | Estimate validation: cross-validation, estimator comparison, swath plots |
| G-150 | Movies: turntable / section-sweep / scene-to-scene animation export |
| G-151 | Smooth blending of trend inputs (overlapping domains) and trend types beyond nearest-input |
| G-152 | Named, persisted stereonets and the 3-D stereonet bowl in the scene |
| G-153 | Fix Errors panel: errors vs warnings, tree icons, grouping by type / hole / table, ignore rows, one-click fixes, export errors CSV |
| G-154 | Split view / secondary plan viewport (locked north-up minimap inset showing the section line and the main camera footprint while slicing) |
| G-155 | Sub-blocked / octree block models (or partial-block fractions along domain boundaries) |
| G-156 | Fill Slicer / Show faces: capped cut faces on closed volumes |
| G-157 | 'Negative dip points down' survey convention control |
| G-158 | Desurvey method options (spherical arc / balanced tangent / raw tangent for trenches) |
| G-159 | Trace display options: line width / cylinder radius, 2-D lines vs cylinders, hole-ID label toggle and position (start / end), depth markers, interval text |
| G-160 | Interval Mid Points as a full points table (all columns, categorical too, one click) |
| G-161 | LAS import dialog: collar position when the ~Well section has none, survey attachment, curve selection and step, unit handling |
| G-162 | Simple kriging (known mean) and combined estimators exposed as methods |
| G-163 | Downhole graphs and LAS log display: a second numeric column (assay, LAS curve) drawn as a curve / log track beside the trace with position, width, scale and range options |
| G-164 | Compositing UI for interval samples (interval-length histogram, New Numeric Composite: entire hole / subset of codes / intervals from other table, residual rule, minimum coverage %, raw-vs-composite comparison) |
| G-165 | Group Lithologies (grouped category column on an interval table, live scene preview, auto-group, edit later) |
| G-166 | Reload / Append drillholes and tables, Map to Source, Import Column into an existing table, combined drillhole sets |
| G-167 | Downhole structural data (hole + depth + dip/dip direction or alpha-beta(-gamma), bottom/top-of-core reference), located by desurvey and usable in stereonet / declustering / form interpolant |
| G-168 | Drillholes and planned holes on sections and in the 2-D section panel: traces within a band, up to three coloured data columns, drillhole-centric exports (planned-hole parameters and prognosis tables, import-error CSV) |
| G-169 | Domain validation (boundary analysis plot) |
| G-170 | Interval Selection tool (paint intervals in the scene into a new column; add / remove / individual / all visible / invert; assign to new lithology; unassigned) |
| G-171 | Back-flagging and table combination: Evaluated Column, Evaluation Table, Merged Table, Majority Composite, Majority Category From Other Table |
| G-172 | Drillhole correlation view / strip logs between holes |
| G-173 | Advanced stratigraphy from drillhole intervals (Stratigraphic Data Explorer, layer/contact statistics, missing contacts, pinch-out points, true thickness, compositing) - drillhole-only |
| G-174 | Layer-conforming flow grid from the pancake model (N layers per unit, minimum thickness) and MODFLOW / FEFLOW export |
| G-175 | Import flow models and time-dependent results (MODFLOW .nam, heads .hds, MT3D .ucn, FEFLOW .fem/.dac) with a timestep slider |
| G-176 | Structural form as thickness control and polarity flip for stratigraphic thickness (Advanced Stratigraphy) |

## 7. What Leapfrog does that this modeller will not

* **Load its projects.** `.aproj` is a proprietary database; the supported
  interchange is the files Leapfrog imports and exports (OMF, DXF, grids,
  CSV), all of which round-trip here. OMF-imported objects cannot be reloaded
  in Leapfrog — the README in every kit says so.
* **Drillhole modelling.** Compositing, back-flagging, interval selection,
  vein segments from intercepts, drilling prognoses, correlation views. When
  a target graduates to samples the collar / survey / assay CSVs go into
  Leapfrog beside the kit this modeller exports.
* **Fault systems and vein systems with terminations.** Refuted above: they
  need surface-against-surface trimming and a chronology the engine does
  not have, and without drillholes there is nothing to terminate against but
  a guess.
* **Invent an answer.** A missing bearing is a question; flat ground yields
  no dip; a trace with no elevation is draped first or refused; a described
  adit draws dashed everywhere it appears, including the exported image.

## 8. How the UI was judged

Four independent critiques were run against 26 screenshots of the live page
and the panel code: a first-time user, a daily Leapfrog user expecting its
conventions, an information designer, and a workflow coach. Their merged
findings drove the shell restructuring recorded in `GEOMODEL.md` §8 (the
split right column, the one arming path and its strip, the workflow-ordered
TOOLS menu with readiness, the WHERE THINGS STAND card, the banded tree with
the step groups always present, the confidence key that never hides, the
model opening on its workings, undo, in-page dialogs, the render image with
its overlays, scenes, hotkeys, right-click, the status bar). The 262 code
faults the source readers recorded are in the same analysis and the
high-severity ones are fixed in this pass; the rest are ordinary tickets.
