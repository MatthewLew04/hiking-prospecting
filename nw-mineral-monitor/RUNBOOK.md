
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
- source PDFs, extracted plates, working rasters, COGs, legends, and XYZ
  tiles stay under `pipelines/cache/ws10/` and are ignored by git. Only the
  publishable files under `pipelines/cache/ws10/assets/` go to the S3-only
  `ws10-assets/` prefix.

### Prerequisites

The current implementation is Python-native: Pillow, NumPy, tifffile, Fiona,
pyproj, and Shapely build the attributed vector output, georeferenced tiled
TIFFs, legends, and XYZ WebP tiles. Poppler supplies only the `pdftoppm` and
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

### Rebuild the inventory and assets

Run from `nw-mineral-monitor/`:

```bash
python3 pipelines/geology_quads.py
python3 pipelines/prepare_quad_geology.py --download  # first run; verifies pinned SHA-256s
python3 pipelines/geology_quads.py                    # merge generated build state
```

`geology_quads.py` deterministically rebuilds the top-15 rich-open target
selection, derives containing + adjacent USGS 7.5-minute quads, merges the
catalog snapshot/current catalog responses, and writes the committed
inventory. The four required seed areas remain in the inventory independently
of score rank. A catalog miss must produce an explicit `gap`; it must not
drop the target row.

The default inventory run preserves the dated catalog snapshot so a normal
rebuild is reproducible. Refresh official USGS quad and NGMDB responses only
when intentionally updating research, then review the inventory diff:

```bash
python3 pipelines/geology_quads.py --refresh
```

`prepare_quad_geology.py` acquires/verifies source files, extracts pocket
plates, georeferences approved sheets, creates COG/XYZ/legend products, and
updates the generated asset-state pointer. Re-runs can omit `--download` once
the checksum-verified sources are cached. A successful raster build is still
`status: processing` / `build_status: built-awaiting-upload`; local existence
is never treated as proof that a CloudFront object is live. Standard
7.5-minute sheets use official quad corners. Irregular plates use reviewed
control metadata and remain non-ready whenever confidence is insufficient. A
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

`git status` must not list a PDF, TIFF, PNG/JPEG tile pyramid, or other
generated raster. Small legends are raster products too and stay in the
ignored asset tree; the inventory stores their S3/CloudFront pointer.

### Publish in this order

The pointer JSON must never reach users before the objects it names:

```bash
cd infra
WS10_UPLOAD_DRY_RUN=1 bash deploy.sh upload-ws10-assets
bash deploy.sh upload-ws10-assets
cd ..
# Verify the uploaded COGs, legends, and representative XYZ URLs first.
python3 pipelines/prepare_quad_geology.py --mark-ready dwm-193 anderson-1931-plate-xviii johnston-pp194-plate-1
python3 pipelines/geology_quads.py
/Users/matthewlew/miniconda3/bin/python pipelines/validate_quad_geology.py
bash infra/deploy.sh update-site
```

The first command prints the exact S3 changes. The real upload is add/update
only: it intentionally has no `--delete`, uploads to the fixed
`s3://<site-bucket>/ws10-assets/` prefix, sets cache metadata, and invalidates
that CloudFront path. `deploy` and `update-site` explicitly exclude
`ws10-assets/*` from every destructive sync, so an ordinary code/data deploy
cannot erase S3-only geology. Old versions remain until an operator reviews
and removes them or an S3 lifecycle policy does so.

Do not run `--mark-ready` merely because the upload command exited zero.
First compare S3 object keys/counts with generated state, fetch the COG and
legend through CloudFront, sample representative XYZ URLs, and complete the
alignment review. `--mark-ready` revalidates local checksums/counts and records
`ready` / `uploaded-and-verified`; the following `geology_quads.py` run is what
copies that reviewed state into the site inventory. Deploy the site only
afterward. On another workstation, replace the absolute Miniconda executable
with any Python that has the documented WS10 dependencies.

The final validator checks the exact top-15 ranking plus four seeds, eight
adjacent quads per target, explicit gaps, the three-ready/one-blocked layer
set, local COG tags/checksums and XYZ counts, remote-verification stamps,
DWM-193 vector/rescan invariants, the unsent outbox guardrail, inline UI
JavaScript syntax, and the no-raster-in-git policy. A failure blocks the site
deploy; Node.js must be on `PATH` for its inline-script syntax check.
`--skip-assets` is for metadata-only diagnostics, not final QA.

Then verify through the CloudFront URL, not an S3 URL:

1. Open MAP INVENTORY and confirm all 15 ranked targets plus forced seeds
   appear, including explicit gaps and watch-list entries.
2. Toggle each `ready` layer, move its opacity slider, and inspect alignment
   at all four corners and across obvious internal control features.
3. Open its legend and provenance; citation, year, scale, retrieval date,
   source link, and cited-grade association must be present.
4. Confirm a missing/low-confidence asset stays cataloged or in review and
   cannot masquerade as a trusted ready overlay.
5. Confirm the DWM-193 vector rescan record cites 1:24,000 source geology.

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
The site exposes the draft but has no send action. Do not paste, send, or
automate delivery to CGS without the operator explicitly approving the final
recipient, subject, and body. Receipt of a CGS database starts a new ingest
and provenance review; it does not silently replace the cataloged raster.

Jackson PGM-19-01 remains explicitly `blocked`: its PDF is email-gated and
CGS web-tile/database reuse rights have not been confirmed. The unsent draft
request is the next action, not an acquired source. Do not build, upload, or
mark a Jackson layer ready until CGS supplies the source and written rights
are reviewed.
