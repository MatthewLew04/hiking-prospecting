# Non-claim AML and trust-land publication

`pipelines/build_nonclaim_equivalents_pmtiles.py` is the release-safe handoff
between state adapters and the WS11 non-claim layers. It accepts exactly the
30 registry states whose `regime` is `non_claim`; a federal claim state cannot
enter this workflow.

The builder does not fetch agency data and does not decide that a source is
unavailable. An adapter/reviewer must freeze either a complete spatial source
or an explicit finding in private staging. The builder verifies that decision,
tiles accepted spatial rows, and emits the evidence needed for registry review.
It never changes `states/*.yaml`, `site/data/manifest.json`, or a release flag.

## Private inventory

Keep the inventory and all 60 decision files outside `site/`:

```text
/private/nonclaim-2026-08-13/
  inventory.json
  al_aml.json
  al_trust_land.json
  ...
  wv_aml.json
  wv_trust_land.json
```

`inventory.json` has this exact shape:

```json
{
  "schema_version": 1,
  "system": "nonclaim_equivalents",
  "created": "2026-08-13",
  "clip": {
    "authority": "U.S. Census Bureau TIGERweb, January 1 2025 vintage",
    "method": "Every source coordinate must be inside or on the boundary of the authoritative state polygon before tiling",
    "artifact_sha256": "<sha256 of infra/state_clips.json>"
  },
  "states": {
    "MI": {
      "aml": {
        "file": "mi_aml.json",
        "n": 418,
        "bytes": 123456,
        "sha256": "<64 lowercase hex>",
        "release_inventory_status": "ingested_complete"
      },
      "trust_land": {
        "file": "mi_trust_land.json",
        "n": 0,
        "bytes": 987,
        "sha256": "<64 lowercase hex>",
        "release_inventory_status": "documented_unavailable"
      }
    }
  }
}
```

`states` must contain every one of the exact 30 non-claim state codes, with
both decisions. Every filename is fixed as `<lowercase-state>_<kind>.json`.
All files are checksummed before work starts and again before the publication
pointer changes. A one-state run still pins and rechecks all 60 files.

## Complete spatial snapshot

An ingested snapshot is strict GeoJSON. It must say `complete: true`,
`truncated: false`, contain a nonempty `FeatureCollection`, cite an official
registry URL, and prove that its frozen count was exhausted:

```json
{
  "schema_version": 1,
  "state": "MI",
  "kind": "aml",
  "release_inventory_status": "ingested_complete",
  "source_id": "mi_egle_abandoned_mining_wastes",
  "reviewed": "2026-08-13",
  "complete": true,
  "official_source_urls": ["https://www.michigan.gov/egle/..."],
  "retrieved": "2026-08-13",
  "truncated": false,
  "pagination": {
    "method": "offset",
    "expected_count": 418,
    "fetched_count": 418,
    "page_size": 200,
    "page_offsets": [0, 200, 400],
    "page_row_counts": [200, 200, 18],
    "pagination_exhausted": true,
    "source_snapshot_id": "count-query-2026-08-13T14:00Z"
  },
  "type": "FeatureCollection",
  "features": []
}
```

Use `method: "single_file"` for a checked download, with one offset `0`, one
row count equal to `n`, and a page size at least `n`. `expected_count` and
`fetched_count` must equal both the feature-array length and inventory `n`.
There is no release-mode cap and no progress artifact from this builder.

Every feature has exactly `type`, `id`, `properties`, and `geometry`.
`id` must equal the scalar `record_id` property. Properties must be MVT-safe
scalars; nested objects and arrays fail. The canonical contracts are:

- `aml`: point or multipoint geometry; `record_id`, `source_id`, and `status`
  are required. The archive also carries `st` and checksummed provenance.
- `trust_land`: polygon or multipolygon geometry; `record_id`,
  `mineral_class`, and a substantive `approach` route are required. The
  snapshot also carries `offering_class: "offered"` or `"limited"`.
  Parcel, lease status, agency, owner, and source fields are preserved when
  the adapter supplies them.

Every coordinate must fall inside or on the Census state polygon. A state
abbreviation property cannot override that check. Duplicate record IDs,
degenerate rings, unclosed polygons, wrong geometry classes, and non-finite
numbers all stop publication.

## Explicit unavailable finding

A reviewer may record `documented_unavailable` when an official-source review
finds no complete public spatial inventory. Trust land may instead use
`not_applicable` only when `offering_class` is `not_offered`.

```json
{
  "schema_version": 1,
  "state": "MA",
  "kind": "aml",
  "release_inventory_status": "documented_unavailable",
  "source_id": "eamlis_ma_national_baseline",
  "reviewed": "2026-08-13",
  "complete": true,
  "official_source_urls": ["https://www.mass.gov/..."],
  "spatial_inventory_available": false,
  "finding": "A substantive reviewed finding of at least forty characters."
}
```

The inventory count must be zero. The builder copies the finding verbatim into
a small content-addressed evidence JSON. It does not emit an empty PMTiles
archive and does not infer absence from a zero-row query. Finding-only runs do
not require Tippecanoe.

## Build and review

Tippecanoe 2.79 or newer is required for an ingested decision:

```sh
python3 pipelines/build_nonclaim_equivalents_pmtiles.py \
  --staging-dir /private/nonclaim-2026-08-13 \
  --inventory /private/nonclaim-2026-08-13/inventory.json \
  --publish-dir /review/nonclaim-2026-08-13 \
  --state MI
```

Repeat `--state` or omit it for all 30. The builder uses no feature- or
tile-size dropping. It fully decodes every unique MVT payload, requires the
canonical layer/property/geometry contract, and proves that the set of encoded
`record_id` values exactly equals the input set. It then installs add-only,
content-addressed PMTiles and evidence files before atomically replacing
`latest.json`. A failed run cannot update that pointer; an unreferenced
content-addressed file can be safely left for an operator to audit.

For `ingested_complete`, copy the pointer's `file`, `bytes`, `sha256`,
`source_layers`, `required_properties`, `layer_metadata`, and `evidence_file`
into the reviewed registry entry. The PMTiles use
`artifact`/`sha256`/`bytes`; the decision JSON uses
`evidence_artifact`/`evidence_sha256`/`evidence_bytes`. For a finding, copy only
the reviewed status, offering class where applicable, and that exact
content-addressed evidence descriptor. Promotion copies only those immutable
objects below `site/map-assets/releases/`, preserving the builder's already
canonical `<full-sha256>.pmtiles` and `<full-sha256>.json` basenames; never copy
its mutable `latest.json`. Run the registry, national release validator, coverage
rebuild/check, and browser acceptance before anyone changes a state to
`release.status: done` and `release.enabled: true`.

No source FeatureCollection or GeoJSON sequence may be copied below `site/`.
The only browser-scale spatial artifacts from this workflow are PMTiles.
