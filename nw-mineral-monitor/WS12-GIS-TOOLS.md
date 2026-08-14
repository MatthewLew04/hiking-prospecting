# WS12 GIS-as-tools contract

The ASK agent exposes six coordinate/document tools: `geology_at`,
`claims_at`, `mines_near`, `faults_near`, `mag_at`, and `docs_for`.
Every geologic unit and fault result carries its source map, citation, URL,
scale text, and (when the source states an exact representative fraction) a
numeric `scale_denominator`.

## Spatial store

The production format is compressed GeoParquet queried with DuckDB Spatial.
The dependency-free build first creates an audited SQLite/RTree store; this is
also the local/test fallback and the source for deterministic GeoParquet
export. Originals and generated database/GeoParquet artifacts belong in
private object storage, not git.

```bash
python3 pipelines/spatial_store.py build \
  --root . \
  --output pipelines/cache/ws12/spatial.sqlite \
  --replace \
  --geoparquet-output pipelines/cache/ws12/geoparquet
```

For a fast, deterministic acceptance artifact containing the repository's
named Cassia/Owyhee/Jackson geology, quad raster contexts, and public document
metadata (but not the large claims/sites catalogs), use:

```bash
python3 pipelines/spatial_store.py build \
  --root . \
  --output pipelines/cache/ws12/spatial-acceptance.sqlite \
  --replace --scope acceptance
```

The deployment artifact must use the default `--scope full`; the acceptance
scope is deliberately labeled in `store_metadata` and is not a production
substitute.

`--geoparquet-output` requires `pyarrow`; querying the export requires
`duckdb` with its `spatial` extension. The SQLite fallback uses only Python's
standard library and SQLite RTree. A build checkpoints WAL, switches to
`journal_mode=DELETE`, and leaves one deployable `.sqlite` object with no
required `-wal` or `-shm` sidecars.

Set `NWMM_SPATIAL_DB` while running the national geology/fault builder to
ingest its complete pre-tile SGMC, Alaska SIM 3340, and Qfaults sequences
before those large temporary files are removed. Additional statewide pre-tile
vectors are declared in `build-inputs/ws12/spatial-layers.json`; registered
files are byte-count and SHA-256 checked before ingestion. The normal build
also ingests WS10 native vectors, claims/sites, Cassia land status, geophysical
survey footprints, all ready quad-raster footprints, gate-passed numeric
aeromagnetic COG descriptors, and `site/data/docs/index.json` when present.

## Runtime

Upload the self-contained database to a private S3 key and configure the ASK
Lambda:

```text
SPATIAL_DB_BUCKET=<private bucket>
SPATIAL_DB_KEY=private/ws12/spatial.sqlite3
```

After the stack exists, upload a generated store with:

```bash
infra/deploy.sh upload-spatial-store pipelines/cache/ws12/spatial.sqlite
```

The command refuses WAL/SHM sidecars, runs an immutable SQLite integrity and
schema check, attaches the exact SHA-256 as S3 metadata, and verifies the
remote byte count and hash metadata. The runtime independently verifies those
bytes before opening the database.

`infra/spatial_tools.py` downloads that exact key to Lambda `/tmp`, caches by
ETag, opens it read-only, and exports `TOOL_NAMES` plus
`execute(name, arguments)`. The browser posts
`{"localTool":{"name":"geology_at","input":{...}}}` to the authenticated
ASK endpoint. If the generated store is not deployed, it falls back to native
WS10 JSON and currently loaded PMTiles and labels the result
`loaded_viewport...`; it never presents a viewport miss as a statewide zero.

## Evidence semantics

- `geology_at` returns every covering **vector** unit, finest exact scale
  first. `finest` is never chosen from an image.
- `higher_resolution_raster_context` names covering WS10 map images whose
  native unit GIS is unavailable. Such rows have
  `unit_status: not_queryable_from_raster`.
- `claims_at` distinguishes `polygon_covers_point` from approximate
  `representative_point_nearby` records. The latter cannot establish title.
- `mag_at` samples a numeric COG or a declared numeric sample and returns nT
  plus survey/grid provenance. It never converts display-raster colours to
  nanoteslas; a missing sample is unknown, not zero.
- `docs_for` returns document metadata, `page_count`, and `indexed_pages`.
  Exact page citations come only from bounded document-search hits.

The pinned Jackson acceptance record is official USGS SGMC FeatureServer layer
3 `OBJECTID=16735`: unit `J`, Jurassic marine rocks, Jennings (2010),
1:750,000. CGS Jackson PGM-19-01 is separately reported as 1:24,000
raster-only context. DWM-193 remains native 1:24,000 vector evidence in
Owyhee County. This distinction is intentional: a 1:24,000 image and a
1:750,000 queryable vector are different evidence products.

Focused verification:

```bash
python3 -m unittest tests.test_ws12_spatial_tools -v
```
