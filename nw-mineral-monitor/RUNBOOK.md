
## Grades rebuild (WS9 — rounds 1+2, CA + ID)

Order matters; each step is idempotent:

```
python3 pipelines/grades_ca.py     # CA round 1 (embedded rows) + round 2 (rows_ca_r2.json)
python3 pipelines/grades_id.py     # ID round 2 (rows_id_r2.json) + county backfill
python3 pipelines/county_gold.py   # richOpen / stakeable ranking rerun
```

Inputs: `grades-research/rows_{ca,id}_r2.json` (curated rows, page-cited),
`pipelines/cache/pagetext/*.json.gz` (page-indexed source text — committed;
quotes are re-validated against it on every run and a failure aborts),
`site/data/sites/mrds_{ca,id}.json` + the full MRDS dump (auto-fetched to
`pipelines/cache/mrds.csv` if absent) for county-scoped geolocation, and
`site/data/claims/{ca,id}_active.json` for open distances. Source PDFs
re-fetch by URL into `pipelines/cache/pdfs/` (gitignored) only when a
pagetext file needs rebuilding. If `build_grades.py` (round 0) is ever
rerun, run grades_ca.py + grades_id.py again afterward — they own the
'ca-r1'/'ca-r2'/'id-r2' row tags and the schema migration.

## Quad-scale geology build and deploy (WS10)

WS10 deliberately has two deliverables with different storage rules:

- `site/data/geology-quads/inventory.json` is small, reviewable metadata and
  is committed to git with the other site data;
- source PDFs, extracted plates, working rasters, COGs, legends/previews, and XYZ
  tiles stay under `pipelines/cache/ws10/` and are ignored by git. Only the
  publishable files under `pipelines/cache/ws10/assets/` go to the S3-only
  `ws10-assets/` prefix.

### Prerequisites

The current implementation is Python-native: Pillow, NumPy, tifffile, Fiona,
pyproj, and Shapely build the attributed vector output, georeferenced tiled
TIFFs, legends/previews, and XYZ WebP tiles. Poppler supplies only the `pdftoppm` and
`pdfimages` extraction executables. **No GDAL command-line program is required
by this implementation.** A Conda environment is the least-friction install:

```bash
conda install -c conda-forge pillow numpy tifffile fiona pyproj shapely poppler
python3 -c "from PIL import Image; import numpy,tifffile,fiona,pyproj,shapely; print('WS10 Python dependencies OK')"
command -v pdftoppm pdfimages
```

With another Python environment, install those six packages with its package
manager and install Poppler separately (for example, `brew install poppler`
on macOS). Fiona may bundle GDAL libraries internally; that does not create a
runtime dependency on `gdal_translate`, `gdalwarp`, `gdaladdo`, `gdalinfo`,
or `gdal2tiles.py`.

The Anderson Plate XVIII and Johnston PP 194 pocket-plate images embedded in
the official PDFs are **native 400 ppi**. The pipeline extracts those native
images losslessly, preserves `source_native_ppi: 400` in build provenance,
and creates an explicitly labeled 600-ppi output resample. That 1.5× resample
meets the output convention but does not invent or imply additional source
detail. Never describe these sources as native 600-ppi scans.

Jackson PGM-19-01 follows a different raster path. Its map image is the
official public NGMDB 4096×4096 georeferenced KMZ GroundOverlay, and its
true unit legend is cropped from the NGMDB sheet preview. The original CGS
PDF still uses California's email-delivery/ADA workflow, while native GIS
is not publicly available. Project-owner direction permits this academic
deployment without a separate reuse review; preserve CGS/NGMDB attribution
and do not describe the product as openly licensed. Jackson remains
raster-only and must not create a vector rescan.

The other NGMDB-selected rasters use `legend_mode: map-preview`. Their
reduced whole-sheet WebP is published under `ws10-assets/previews/` and must
be labeled **map preview**, never **legend**: it provides orientation but does
not reliably decode unit colors or symbols. A `legend_url` is reserved for a
reviewed crop of an actual collar/unit key. A ready layer may publish one or
the other, not both.

### Rebuild the inventory and assets

Run from `nw-mineral-monitor/`:

```bash
python3 pipelines/geology_quads.py
# Normalize DWM-193 native GIS and run its WS6 rescan once, without rasters:
python3 pipelines/prepare_quad_geology.py --download --skip-rasters
# Build only one raster at a time; substitute a real layer id:
python3 pipelines/prepare_quad_geology.py --download --skip-vector --only mayflower-mbmg-ofr-505
```

`geology_quads.py` deterministically rebuilds the top-15 rich-open target
selection, derives containing + adjacent USGS 7.5-minute quads, merges the
catalog snapshot/current catalog responses, and writes the committed
inventory. The four required seed areas remain in the inventory independently
of score rank. A catalog miss must produce an explicit `gap`; it must not
drop the target row.

The final target switcher has 19 rows backed by 18 unique raster layers: four
seed overlays plus 14 ranked-map selections. Idaho Bonanza and Atlanta both
map to `hailey-of-91-340`, so they share one layer rather than duplicating its
COG and tile pyramid. The UI must render a checkbox for every ready target,
keep shared controls synchronized, close the inventory when a map is enabled,
and pan/fit to that layer's bounds. A row without a genuinely ready tiled
asset must show its status instead of a nonworking checkbox.

Every ranked selection is acquired as an official NGMDB KMZ. The extractor
must match the configured KML member and raster href, reject unexpected
rotation, compare the KML bounds to the pinned configuration, and verify each
associated target coordinate inside the GroundOverlay footprint. Hailey must
contain both associated coordinates. These checks establish raster placement;
they do not make a regional sheet equivalent to quad-scale mapping.

The selection notes preserve the material scale exceptions:

- Willow Creek/Pearl: 1:125,000 Boise fallback; IGS P-41 (1:48,768) awaits
  manual georeferencing.
- Azurite: target-containing 1:100,000 Robinson Mountain fallback.
- New Trail: 1:100,000 Ivanpah surficial fallback, not detailed bedrock.
- Excelsior: 1:250,000 Dillon fallback; Bannack–Grayling I-433 (1:31,680)
  remains the detailed scan upgrade.
- Mc Grath: 1:250,000 Medford fallback; DOGAMI GMS-38 (1:24,000) remains the
  detailed scan upgrade.
- Idaho Bonanza and Atlanta: shared 1:250,000 Hailey fallback; OFR 2004-1205
  Plate 1 (1:24,000) remains the target-specific scan upgrade.
- Mammoth: 1:250,000 Challis fallback; IGS GM-45 (1:100,000 PDF/native GIS)
  remains the finer upgrade without a public georeferenced raster.

Warner and Niagara also retain newer/finer non-georeferenced candidates while
serving reviewed 1:62,500 georeferenced fallbacks. Do not discard these
candidates when rebuilding inventory: they are the manual-GCP upgrade queue.

The default inventory run preserves the dated catalog snapshot so a normal
rebuild is reproducible. Refresh official USGS quad and NGMDB responses only
when intentionally updating research, then review the inventory diff:

```bash
python3 pipelines/geology_quads.py --refresh
```

`prepare_quad_geology.py` acquires/verifies source files, extracts pocket
plates, georeferences approved sheets, creates COG/XYZ and legend-or-preview
products, and updates the generated asset-state pointer. Re-runs can omit
`--download` once the checksum-verified sources are cached. A successful
raster build is still `status: processing` / `build_status: built-awaiting-upload`;
local existence is never treated as proof that a
CloudFront object is live. Standard 7.5-minute sheets use official quad
corners. Irregular plates use reviewed control metadata and remain non-ready
whenever confidence is insufficient. A
visually plausible but unreviewed warp is not shippable.

For vector-bearing products such as IGS DWM-193, retain the source citation
and scale on every normalized unit, then rerun the WS6 lexicon scoring for
the 24k AOI. The rescan record belongs in the WS10 inventory; the raster is
only a visual overlay and must not substitute for the vector analysis.

Before publishing, check the worktree and the asset tree separately:

```bash
git status --short
find pipelines/cache/ws10/assets -type f | sort
python3 -c "import tifffile; f=tifffile.TiffFile('pipelines/cache/ws10/assets/cogs/anderson-1931-plate-xviii.tif'); p=f.pages[0]; print(p.is_tiled,p.shape); f.close()"
```

`git status` must not list a PDF, TIFF, PNG/JPEG/WebP tile pyramid, or other
generated raster. Small legends and map previews are raster products too and
stay in the ignored asset tree; the inventory stores their S3/CloudFront
pointers.

### Publish sequentially in this order

The pointer JSON must never reach users before the objects it names.

On a space-constrained workstation, never stage all 18 COGs and pyramids at
once. Complete this cycle for one layer before building the next:

```bash
# 1. Build and locally validate one layer.
python3 pipelines/prepare_quad_geology.py --download --skip-vector --only mayflower-mbmg-ofr-505

# 2. Preview, then perform the add/update-only S3 upload.
cd infra
WS10_UPLOAD_DRY_RUN=1 bash deploy.sh upload-ws10-assets
bash deploy.sh upload-ws10-assets
cd ..

# 3. Verify its COG, target tiles, and legend OR map preview through CloudFront,
#    inspect alignment, then promote while the local files still exist.
python3 pipelines/prepare_quad_geology.py --mark-ready mayflower-mbmg-ofr-505
python3 pipelines/geology_quads.py

# 4. Reclaim disk only after promotion recorded remote verification.
python3 pipelines/prepare_quad_geology.py --evict-ready-local mayflower-mbmg-ofr-505
```

Repeat with each remaining layer id. `--evict-ready-local` refuses anything
that is not both `ready` and remotely verified, deletes only that layer's
exact local COG/XYZ/legend-or-preview outputs, and retains cached official
sources plus generated checksums/provenance. The verified S3 objects then are
the published-raster system of record and the layer remains reproducible. On
a space-constrained workstation, an exact source/work cache may be removed
separately after verification because its official URL, SHA-256, byte count,
and extraction contract are committed; source KMZ/PDF files are not uploaded
by `upload-ws10-assets`. Do not remove ignored files with a broad recursive
command.

The dry-run upload command prints the exact S3 changes. The real upload is
add/update only: it intentionally has no `--delete`, uploads to the fixed
`s3://<site-bucket>/ws10-assets/` prefix, sets cache metadata, and invalidates
that CloudFront path. `deploy` and `update-site` explicitly exclude
`ws10-assets/*` from every destructive sync, so an ordinary code/data deploy
cannot erase S3-only geology. Old versions remain until an operator reviews
and removes them or an S3 lifecycle policy does so.

Do not run `--mark-ready` merely because the upload command exited zero.
First compare S3 object keys/counts with generated state, fetch the COG and
the layer's legend or map preview through CloudFront, sample representative
XYZ URLs, and complete the alignment review. `--mark-ready` revalidates the
still-local checksums/counts and records `ready` /
`uploaded-and-verified`; the following `geology_quads.py` run copies that
reviewed state into the site inventory.

After every layer has completed that pre-eviction gate, rebuild and perform
the final metadata/UI validation before deploying pointers:

```bash
python3 pipelines/geology_quads.py
/Users/matthewlew/miniconda3/bin/python pipelines/validate_quad_geology.py --skip-assets
bash infra/deploy.sh update-site
```

Here `--skip-assets` is intentional: evicted raster files were already
validated locally by their build and `--mark-ready`, then verified remotely
before deletion. It must never be used to promote or excuse an unverified
layer. The final validator still checks the exact top-15 ranking plus four
seeds, 19 target-to-layer mappings, eight adjacent quads per target, explicit
gaps, 18 ready/zero blocked unique layers, remote-verification stamps,
mutually exclusive legend/preview metadata, DWM-193 vector/rescan invariants,
Jackson's raster-only provenance, the unsent outbox guardrail, target-switcher
UI markers and JavaScript syntax, and the no-raster-in-git policy. A failure
blocks the site deploy; Node.js must be on `PATH`. On another workstation,
replace the absolute Miniconda executable with a dependency-capable Python.

Then verify through the CloudFront URL, not an S3 URL:

1. Open MAP INVENTORY and confirm 19 target rows and 19 ready checkboxes map
   to 18 unique layers, with explicit gaps and watch-list entries retained.
2. Enable every target row. The modal must close, the map must pan/fit to the
   selected footprint, and the raster must be visible at the destination.
3. Confirm Idaho Bonanza and Atlanta toggle the same Hailey layer and stay
   synchronized rather than downloading duplicate rasters.
4. Move the opacity slider and inspect all 18 layers at their four corners
   and across obvious internal control features.
5. Open provenance for each layer. Citation, year, scale, retrieval date,
   source link, cited-grade association, and fallback warning (when present)
   must be visible. A whole-sheet thumbnail must say **map preview**; only an
   actual key crop may say **legend**.
6. Confirm a missing/low-confidence future asset stays cataloged or in review
   and cannot masquerade as a trusted ready overlay.
7. Confirm the DWM-193 vector rescan record cites 1:24,000 source geology.

If a ready layer returns 403/404, compare its inventory URL with:

```bash
aws s3 ls "s3://$(aws cloudformation describe-stacks \
  --stack-name nw-mineral-monitor --query \
  "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" \
  --output text)/ws10-assets/" --recursive
```

Re-upload assets before redeploying pointers. If tiles are correct in S3 but
look stale, wait for the invalidation from `upload-ws10-assets` or invalidate
`/ws10-assets/*`; do not rename an inventory pointer to conceal a bad build.

### Outbox rule

`outbox/cgs-jackson-gis-request.md` and the matching
`site/data/outbox/` metadata are a reviewable **draft**, not a mail queue.
The site exposes the draft but has no send action. It remains unsent and is
superseded for raster acquisition because NGMDB already provides the official
georeferenced KMZ. The draft requests native attributed GIS only; do not
paste, send, or automate it without the operator explicitly approving the
final recipient, subject, and body. Receipt of a CGS database would start a
new vector ingest and provenance review, not silently replace the live NGMDB
raster or retroactively create a Jackson rescan.

Jackson PGM-19-01 is one of the four seed overlays in the 18-layer set.
Promote it only after its public NGMDB KMZ-derived COG/tiles and
sheet-preview-derived legend pass the same remote-object and alignment checks
as the other layers. The project owner waived a separate reuse review for
this academic deployment;
that decision does not assert an open license. The email-delivered CGS PDF
and nonpublic native GIS are not prerequisites for publishing this raster.
