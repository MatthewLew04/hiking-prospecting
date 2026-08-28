# NW Mineral Monitor — the map, explained in plain English

_Written for two readers at once: a geologist who wants to know exactly what
each layer is made of, and someone who has never touched GIS in their life.
Plain language first; **Geologist's note** paragraphs add the technical depth.
Covers build 2026-08-11 (everything through WS10). The operator's manual with
commands and pipelines is `USER-GUIDE.md`; this is the tour._

---

## What this is

This is a treasure map built out of government records — with receipts.

It covers eight states (Washington, Oregon, Idaho, Montana, Wyoming, Nevada,
Utah, California) and layers together, on one screen: nearly **400,000 known
mines and mineral prospects**, about **743,000 active mining claims** (the
ground somebody currently holds) plus **1.3 million historic claims** that
were staked and later dropped, **3,366 old mines with documented ore
richness** — each one backed by a word-for-word quote from the original
government report — the **geologic maps** themselves, **airborne magnetic
and radiation surveys**, and a scoring engine that ranks counties and
individual targets by how much *actionable* gold evidence sits on ground
nobody currently holds.

Nothing on the map is an opinion without a source. Click anything and its
card tells you where the fact came from, what document, what page, and when
it was retrieved. When a layer has a known weakness, the caveat is printed
on the card itself — this map would rather admit a blind spot than fake
confidence.

## The one rule

**Everything here is a research lead, not proof of ownership.** Two traps
matter enough to state up front:

*Active claims are private property.* A teal dot means someone holds the
mineral rights there. You cannot stake it, and you should not dig on it.

*"No claims" does not mean "open for staking."* Old famous mines often sit
on **patented** land — ground that became fully private a century ago. It
shows no federal claims, and looks temptingly empty in every layer. Same for
parks, wilderness, and withdrawn areas. Before boots or paperwork move,
verify at BLM and the county recorder. And never enter old adits or shafts —
they kill people.

## The screen

Across the top: live record counts, a **search box** (mine names, claim
serial numbers, districts, places), basemap buttons (**DARK** for reading
data, **SAT** for real terrain, **TOPO** for contour maps), and the feature
buttons — **INTEL**, **ASK**, **+ DATA**, **WATCH**, **READING**, **ABOUT**.

Down the left: the layer switchboard, described section by section below.
Click any row to turn a layer on or off. The **STATES** chips at the top
limit everything to the states you care about.

The map is the middle. Click anything on it and the **detail panel** opens
on the right with the full story of that thing. The footer shows your
coordinates and how fresh each dataset is.

---

## Part 1 — The dots: mines and prospects (SITE LAYERS)

Four layers of dots, four kinds of evidence.

**USGS MRDS mineral sites (amber).** The federal master list of "somebody
found something here" — mines, prospects, occurrences. A ring means the
record calls it a producer; a plain dot is a prospect or long-dead mine.
This is the broadest net and also the sloppiest: locations can be off by a
few hundred meters, and the status flags froze around 2011.

**State geological survey databases (blue).** Each state's own mine
inventory. Usually better-located and better-curated than MRDS, because the
state geologists actually visit these. In California this layer is the
state's Mines Online inventory of regulated mines.

**Topo-map workings, called USMIN (steel gray).** Every shaft, adit, and
prospect pit that USGS cartographers drew on topographic maps between 1958
and 2001, digitized. This is the best "someone physically dug here" evidence
on the map, because a surveyor stood there and drew the symbol.

**Cited ore grades (gold dots — brighter and bigger = richer).** The crown
jewel. 3,366 historic mines where a government bulletin, professional paper,
or mine-inspector report states how rich the ore actually was — and the map
carries the *exact sentence*, quoted, with the page number and a link to the
source document. Click one: "The average recovered value of the Eureka ore
was $20 a ton." That's 1860s dollars at $20.67 per ounce — about an ounce of
gold per ton of rock.

At low zoom these layers draw as heat-glow so density reads at a glance;
zoom in past ~7 and they become individual clickable dots. The **COMMODITY
FILTER** chips (gold, silver, copper, uranium…) and the ALL / EXISTING / OLD
switch filter all site layers at once.

> **Geologist's note.** Grade rows carry a `basis` field worth respecting:
> *production average* (mill-run over stated tonnage — the gold standard),
> *ore shipped* (lot arithmetic), *assay* / *assay-text* (often hand-picked
> specimens — bonanza numbers like Sixteen to One's $5,000 from an 80-lb lot
> are recorded faithfully and flagged, not averaged away), *value-text*
> (management's claim). Dollar-era values convert at $20.67/oz pre-1934,
> $35 after. Multi-commodity support includes Ag oz/t, Pb/Zn/Cu/Sb %, WO₃
> units, Hg flasks, and placer $/yd³. Every row's `open` field is the
> computed distance to the nearest active-claim centroid.

## Part 2 — The claims: who holds ground (CLAIM LAYERS)

**Active claims (teal dots).** A nightly snapshot of every active mining
claim in the eight states, pulled from BLM's MLRS system — ~743,000 of them,
California (312k) and Nevada (275k) being the giants. Each dot is a claim's
center point. Click for serial, name, type (lode/placer), and acreage — and
its dossier.

**Live claim boundaries.** Zoom in past ~10.5 and the map fetches the exact
current claim *polygons* straight from BLM for what you're looking at. The
nightly dots tell you "roughly here"; the live boundaries tell you "this
exact 20 acres, as of right now."

**Closed claims (dark red embers).** Historic claims that were staked and
later dropped. This layer is quietly one of the most valuable things on the
map: every ember is a spot where someone once believed enough to pay filing
fees — and then stopped. Ground under an ember with nothing active nearby is
"proven interesting, currently unheld." Heavy — loads on demand.

> **Geologist's note.** Closed files for NV/UT/WY are truncated to their
> newest 250k records each (of up to 1.23M in NV) — dropped-ground metrics
> undercount there, and every scorecard that uses them says so. CA's closed
> file arrives with the first monthly pull (Sept 3). Claim centroids are
> derived from PLSS legal descriptions, so they inherit section-level
> precision, not survey-grade corners.

## Part 3 — What's still open: the section grid (OPEN GROUND — CASSIA)

For the project's home county (Cassia County, Idaho), the map answers the
real question — *"can I actually stake here?"* — one square mile at a time.
Every PLSS section is colored:

- **Green (OPEN)** — historic workings present, no active claim, federal
  surface open to mineral entry. The prime squares.
- **Amber (CLOSED_ONLY)** — was claimed once, dropped, nothing active now.
- **Red (ACTIVE)** — currently claimed. Someone else's.
- **Violet (WITHDRAWN)** — closed to staking by law or land order.
- **Gray (NONFEDERAL)** — private or state surface. The patented-ground trap
  lives here.

Click a section and the card explains *why* it's classified that way, lists
its workings, and links every claim serial that ever touched it.

> **Geologist's note.** Status is computed from claim *legal descriptions*
> (township-range-section) crossed with surface-management agency and BLM
> withdrawal cases — not from point-in-polygon guesswork on sloppy
> centroids. That's why it's trustworthy enough to color a decision grid.

## Part 4 — The geology itself

Three geology systems, at three scales, kept honestly separate.

**GEOLOGY — LIVE, ALL STATES.** Flip it on and the actual geologic map —
colored rock units and fault lines — streams in for the entire eight-state
region, at any zoom. Click any colored polygon: rock name, age, lithology,
description, and the citation of the source map it came from. This is
vector data (real digitized shapes with attributes), served live from
Macrostrat's harmonized compilation of federal and state maps.

**GEOLOGY TARGETS — CASSIA.** Where the map stops describing and starts
*hunting*. The engine reads every rock-unit description in the AOI and
scores it with a hot-springs-gold playbook: **Tier 1 (red)** — units whose
own description says sinter/opal/chalcedony, the literal fossil plumbing of
a gold-depositing hot spring; **Tier 2 (orange)** — silicified or
hydrothermally altered rock; **Tier 3 (blue)** — the right volcanic
neighborhood (rhyolite, tuff, bimodal volcanics), weighted by how
fault-shattered it is; **violet** — travertine (related plumbing, wrong
chemistry, tracked separately). Boosts come from pathfinder minerals nearby
(mercury, antimony, arsenic), hot springs and geothermal wells, and — the
star — how much of the target sits on open ground. 58 ranked targets in
Cassia; click one and its card shows the actual arithmetic, term by term,
plus the verbatim unit description that triggered it.

The same engine passed a blind test in California: pointed at the Clear
Lake–Knoxville country with no hints, it re-discovered the geology around
the McLaughlin deposit — Wilbur Springs inside target #1, the Knoxville
district inside #4 — from unit descriptions and structure alone.

**GEOLOGY (QUAD) — the detailed paper maps.** The two systems above top out
at 1:100,000 scale. The really detailed mapping — 7.5-minute quadrangles,
1:24,000, where an individual sinter mound might actually be drawn — mostly
exists as *paper maps and PDF plates*, not databases. WS10 goes and gets
them for the highest-ranked targets: open **MAP INVENTORY**, check a target,
and its best-available quad map drapes over the terrain as a georeferenced
scan (opacity slider provided), with a provenance card giving the citation,
year, scale, and exactly how the scan was pinned to coordinates. 19 targets,
18 maps, and for each one an honest inventory of what finer mapping exists,
what's a regional fallback, and what's an outright gap in the published
record.

> **Geologist's note.** The quad layer keeps a firewall between pictures and
> data. Scanned rasters (extracted from PDF plates at native resolution, or
> official NGMDB georeferenced KMZs, each with documented control — neatline
> corners, graticule-grid affine fits with GCP counts stated) are for *your
> eyes only*: no raster pixel is ever classified or scored. The one quad
> with native GIS (IGS DWM-193) feeds the WS6 scoring schema directly as
> 1:24,000 vector, and it alone gets analytical standing. Scale fallbacks
> are labeled per target (e.g., 1:250k Challis standing in while a 1:24k
> plate awaits reviewed georeferencing) — a selectable map means "verified
> footprint and honest scale," not "best possible map."

## Part 5 — The scores: county gold ranking (GOLD SIGNAL — COUNTIES)

Flip on the choropleth and all 302 counties shade from dark to bright gold
by **stakeable gold** — a score built from evidence you could still act on:
documented-rich mines sitting on open ground, gold sites that were staked
and later dropped, unclaimed occurrences, validated by producer history and
workings density. Click a county for its itemized scorecard — every point
traced to its evidence, with fly-to links to the best individual leads.

Each card also shows **endowment** — the same county scored for raw gold
regardless of who holds it. The gap between the two numbers is the story:
high endowment + low stakeable = great gold, all locked up (most of Nevada).
High stakeable = documented gold nobody is currently holding. Current top
five: Jackson OR, Okanogan WA, Custer ID, Snohomish WA, Josephine OR.

> **Geologist's note.** Every card prints its own error bars: the 400 m
> "open" test inherits MRDS coordinate slop; NV/UT/WY dropped-ground
> undercounts from truncated closed files; CA's staked-then-dropped term is
> floored at zero until its closed file lands; patented land reads "open"
> until the withdrawal overlay ships.

## Part 6 — Physics from the air (GEOPHYSICS)

Two USGS airborne surveys stream as translucent overlays. **Magnetic
anomaly**: reds = magnetic rock, blues = quiet. The prospecting logic, in
one line: hot hydrothermal fluids destroy magnetite, so a *discrete magnetic
low sitting on a fault* can be the shadow of an alteration cell. **Radiometric
potassium**: adularia — a potassium feldspar — grows in exactly the veins
this map hunts, so potassic alteration lights up in K.

The third toggle, **SURVEY COVERAGE**, is the honesty layer: 819 airborne
survey footprints with year, line spacing, and flight height. A 1969 survey
flown at 8-km line spacing and a modern drape survey look identical in a
pretty colored raster — hover before you trust a pixel.

## Part 6½ — Standing in the terrain (3D TERRAIN)

**3D RELIEF** turns the flat map into real topography (USGS 3DEP/SRTM
composite, ~30 m). Everything already on screen — mines, claims, targets,
live geology, even the geophysics rasters — drapes onto it automatically.
Drag with the right mouse button (or ctrl-drag, or two fingers) to tilt and
orbit; the slider exaggerates relief up to 3×. The prospecting logic: old
workings sit where ore met *access* — adits punch into steep range fronts,
placers hug drainage. Flat maps hide that; a tilted view over SAT with
**HILLSHADE** on reads like standing on the ridge. Hillshade also works
alone in 2D as a cheap way to see range fronts and scarps.

This is the fly-around. The modeling-grade 3-D lives in Leapfrog Geo, and
the map exports straight into it: the **LEAPFROG EXPORT** section right
below has one button — frame your area (zoom ≥ 8), turn on the layers you
want, click, and the browser downloads a zip of Leapfrog-ready files (UTM
point CSVs with grades, AOI geology/fault/section shapefiles, a real DEM,
an `.omf` bundle, and a README with the exact import clicks). Tiled layers
export what's loaded for the current view — a snapshot, not a statewide
archive. For full-AOI kits or scripted refresh use
`pipelines/leapfrog_export.py` — see `LEAPFROG.md`.

## Part 7 — The paper trail: records, dossiers, alerts

**COUNTY RECORDS.** New claims appear at the county recorder *weeks to
months* before they show up in BLM's federal system. This section turns that
lag into an early-warning signal: recorder index exports get parsed, matched
to federal serials, and anything recorded-but-not-yet-federal surfaces as an
alert. Cassia County has no online index, so the sidebar generates a
prefilled records request to email or bring to the recorder's office.

**Dossiers.** Any claim or graded mine → **📋 DOSSIER**: status, acreage,
sections from the legal description, matched county instruments with
confidence levels, an automated history sweep (century-old newspapers via
Chronicling America, Google Books, MSHA safety records — each hit dated and
linked), and go-deeper links including the MLRS serial register, where
claimant names and addresses live.

**WATCH.** The alert feed. Red **ACTIVE→CLOSED** (a claim vanished — ground
may be opening), green **NEW FILING**, amber **LIKELY LAPSED — verify**
(September fee-window scan; only fires against an operator-supplied fee
report, never guessed), teal **COUNTY-RECORDED — NOT IN MLRS** (the earliest
public signal a claim exists), **ASSESSMENT FILED**. Hosted deployments
email the same digest daily.

## Part 8 — Asking instead of clicking (ASK, INTEL, READING)

The **ASK** panel answers questions from the loaded data — deterministically
where it can, with an AI assistant behind it for open-ended questions. Things
worth typing, verbatim:

- "top 10 mines we should go and claim to maximize gold" — ranked stakeable
  picks: documented grade ≥0.3 oz/t with no active claim within 400 m
- "which county has the most gold"
- "richest unclaimed gold in california"
- "open sections" · "was claimed now open" · "watch alerts"
- "tell me about the alleghany district"

**INTEL** is a monthly hand-checked top-10 of real regional developments
(mine construction, staking rushes, policy) with sources. **READING** is the
source library. **ABOUT** lists every dataset, count, and method.

## Part 9 — Your own data (+ DATA)

Drag a spreadsheet, GPX from your handheld, KML from Google Earth, GeoJSON,
or zipped shapefile onto the map. Coordinates are auto-detected — including
UTM and township-range-section text like "T12S R22E Sec 14" — and it becomes
a layer alongside everything else, kept in your browser, exportable. Your
waypoints over the government's evidence is the whole point.

---

## A first session that uses all of it (20 minutes)

Open **GOLD SIGNAL** and click the brightest county in reach. On its card,
fly to the top "best evidence" mine. Turn on **cited grades**, **USMIN
workings**, and **active claims**; read the grade quote; zoom past 10.5 so
live boundaries confirm nothing active sits on it. Flip on **GEOLOGY —
LIVE** to see what rock it's in, and **GEOPHYSICS** to see if structure and
alteration agree. If it's a WS10 target, drape the quad map over it. Open
the **dossier** — history, newspapers, claimant links. If it still looks
good: verify at BLM and the county recorder, then go hike it. That last
step is the map's entire purpose.

## Glossary (the ten words that unlock everything)

**Claim** — a legal stake on federal minerals; active = currently held.
**Patented** — a claim that became fully private land long ago; invisible to
claim layers and the classic trap. **Withdrawn** — federal land closed to
staking (parks, wilderness, monuments). **Section** — the one-square-mile
unit of the Public Land Survey System (PLSS); "T12S R22E Sec 14" is an
address in that grid. **oz/t** — troy ounces of gold per ton of rock;
0.3 oz/t was worth mining underground a century ago and reads as "rich"
here. **MRDS / USMIN** — the federal mines database / the dug-workings
symbols off old topo maps. **Epithermal** — shallow, hot-spring-related gold
systems; the deposit style this map's target engine hunts. **Sinter** — the
silica a gold-bearing hot spring leaves at the surface; Tier-1 evidence.
**Quad** — a 7.5-minute USGS map sheet at 1:24,000, the finest standard
mapping scale. **GIS vs. GeoTIFF** — data vs. picture: GIS layers are shapes
the computer can read and score; a GeoTIFF is a scanned map pinned to
coordinates, for human eyes. This map scores only the first kind, and shows
you both.

## Part 7 — the 3-D model (OPEN 3D MODEL)

Every mine, claim and occurrence card ends with **⛰ OPEN 3D MODEL**; the sidebar's
**3D MODEL** section does the same for the view centre (zoom ≥ 9, radius 1.5–6 km).
A new tab opens `model3d.html` with the site's terrain (same terrarium tiles as
3D TERRAIN), draped imagery (VIEW ▾ Drape: satellite / USGS topo / Macrostrat
geology / elevation colours), the AOI's geology and faults on the ground, graded
mines, targets and claim centroids, plus whatever tiled points the map had
loaded in the viewport.

* **Navigate** — left-drag orbit, right-drag pan, wheel zoom, double-click
  re-centres, `f` fit, `t` top, `n` look north, `o` orthographic / `p`
  perspective (orthographic is the right one for interpreting — it does not
  foreshorten, and the scale bar bottom-left is only exact in it), VE slider
  for exaggeration. The legend and scale bar can be turned off in VIEW ▾.
* **Layers** — tick to show/hide, swatch to recolour, ⋯ for zoom / export /
  delete; click a layer or pick in the scene for properties (colour by an
  attribute, cut-offs, labels, tubes).
* **TOOLS ▾ Structural data** — the fastest thing to do in a new model is
  press **DERIVE FROM ALL TRACE LAYERS**. Where a mapped contact or fault
  crosses the terrain, its trace encodes the plane's orientation, and a
  least-squares plane along the line reads dip and dip azimuth straight off
  it — the three-point problem, run continuously. You get discs on the ground
  in seconds, in a district with no drilling at all. Windows without relief or
  without enough bend are rejected, not guessed, and the counts are shown, so
  flat ground honestly returns nothing. Every derived reading carries the
  relief and the fit error it came from and is tagged `inferred`; digitise
  over the top of it (**POINT + DOWN-DIP**, two clicks) wherever you have a
  real one. Also here: **SET ELEVATION FROM TOPOGRAPHY** (do this to anything
  digitised off a flat map, or it will be silently ignored later) and
  **DECLUSTER**.
* **TOOLS ▾ Stereonet** — lower hemisphere, equal-area or equal-angle,
  equatorial or polar. Poles, great circles, and density contours by Kamb,
  exponential Kamb or Schmidt. The statistics panel gives the Bingham mean
  plane and — when the data is folded — the best-fit girdle, whose pole is the
  fold hinge and whose plane is the profile section. Lasso on the net or drag
  a box in the 3-D scene; either selection becomes a category on the layer, so
  it colours the model straight away. Export PNG or SVG.
* **TOOLS ▾ Form interpolant & trends** — the deformation fabric. The form
  interpolant is an RBF whose gradient is pinned by the measurements, so its
  surfaces lie parallel to the bedding or foliation everywhere; tick *evaluate
  onto topography* and you also get form lines in map view. Below it, a
  structural trend (strength and range, halving every range) and a global
  trend plane you can set from the Bingham mean.
* **TOOLS ▾ Section & slice** — draw a section line or use W–E / S–N; the
  plane clips the model and cuts every surface; the 2-D panel shows the section
  the way you would draw it; export DXF / PNG.
* **TOOLS ▾ Workings from maps** — georeference a scanned level plan at its
  level elevation (or a longitudinal section between two surface points), trace
  drifts on it, add adits from portals (bearing + length), shafts from collars
  (depth, dip), raises and stopes; feet convert to metres; **SEND FOOTPRINT TO
  MAP** brings the plan view back to MY DATA.
* **TOOLS ▾ Stratigraphy** — the pancake model (units youngest-first from
  contact points / surfaces / constants, deposit vs erosion rules), volumes,
  virtual drillhole.
* **TOOLS ▾ Block model & kriging** — block grid, samples + value column,
  experimental variogram and fit, ordinary kriging / IDW, domain by unit,
  cut-offs, grade–tonnage.
* **IMPORT / EXPORT** — drop OMF, DXF, OBJ, GOCAD, Surfer/Geosoft grids, GXF,
  ZMAP+, CSV, Geosoft XYZ, SEG-Y, LAS, GeoJSON, images or PDF pages; export the
  whole project for Leapfrog (OMF v2.0 for 2025.1+, v0.9 for older), a per-format
  kit zip with a README of click-paths, or single layers in any format.

Models autosave in the browser (PROJECTS ▾ lists them); nothing is uploaded.
Colour maps, chosen attributes, cut-offs, glyph sizes and labels are saved with
the model, so reopening it looks like what you left.

