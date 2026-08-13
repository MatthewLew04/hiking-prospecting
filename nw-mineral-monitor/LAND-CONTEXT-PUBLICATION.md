# WS11 non-claim land-context publication

`pipelines/build_national_land_context_pmtiles.py` is the publication boundary
for the 30 WS11 non-claim states. It does not fetch portals, update
`states/*.yaml`, edit `site/data/manifest.json`, or enable a state. Without a
complete private input generation it publishes nothing.

No production land-context generation is checked into this repository. The
current registry and coverage dashboard remain the authority on what has and
has not passed the state DONE gate.

## Why there are three inputs

Each state must freeze these independent snapshots:

1. `ranked_targets`: the complete versioned scoring output, with at least five
   deterministic ranks and point geometry.
2. `surface_ownership`: the reviewed statewide surface-management partition.
3. `mineral_interests`: the reviewed statewide mineral-title/mineral-interest
   partition.

Surface management is never mineral title. Every surface polygon therefore
has an explicit `mineral_interest_id` foreign key. A mineral record must cite
one of these evidence bases:

- `mineral_title_record`
- `state_mineral_inventory`
- `private_title_research`
- `federal_mineral_record`
- `tribal_record`
- `unresolved`

`surface_management` is not an accepted mineral evidence basis. An unresolved
mineral review must use `mineral_class: unknown`; the builder never copies the
surface class into the mineral class.

## Private staging contract

Raw files must be outside `site/`. The inventory covers the exact 30 registry
non-claim states and exactly 90 files:

```text
private-land-context/
  inventory.json
  al_ranked_targets.json
  al_surface_ownership.json
  al_mineral_interests.json
  ...
  wv_ranked_targets.json
  wv_surface_ownership.json
  wv_mineral_interests.json
```

The inventory is strict JSON:

```json
{
  "schema_version": 1,
  "system": "national_nonclaim_land_context",
  "created": "2026-08-13",
  "clip": {
    "authority": "U.S. Census Bureau TIGERweb, January 1 2025 vintage",
    "method": "Every input coordinate must be inside or on the boundary of the authoritative state polygon; adapters must pre-clip source geometry",
    "artifact_sha256": "<sha256 of infra/state_clips.json>"
  },
  "states": {
    "MI": {
      "ranked_targets": {
        "file": "mi_ranked_targets.json",
        "n": 25,
        "bytes": 12345,
        "sha256": "<64 lowercase hex>"
      },
      "surface_ownership": {
        "file": "mi_surface_ownership.json",
        "n": 123,
        "bytes": 45678,
        "sha256": "<64 lowercase hex>"
      },
      "mineral_interests": {
        "file": "mi_mineral_interests.json",
        "n": 87,
        "bytes": 34567,
        "sha256": "<64 lowercase hex>"
      }
    }
  }
}
```

Every snapshot is a strict GeoJSON `FeatureCollection` with:

- `schema_version`, `state`, `kind`, `source_ids`, and nonempty official HTTPS
  source URLs;
- `retrieved`, `complete: true`, and `truncated: false`;
- a pagination record whose source count, fetched count, offsets, row counts,
  and exhaustion flag reconcile exactly with inventory `n`;
- only WGS84 Point, Polygon, or MultiPolygon geometry allowed by that kind.

Adapters must query the authoritative service count independently, exhaust all
pages, freeze a source snapshot ID/ETag/revision, pre-clip ownership geometry
to `infra/state_clips.json`, and only then calculate the inventory byte count
and SHA-256. Landing exactly on a service transfer cap is not evidence of
completeness.

### Ranked targets

The ranked snapshot adds:

```json
{
  "method_id": "ws11-target-score-v1",
  "input_sha256s": {
    "grades": "<sha256>",
    "geology": "<sha256>",
    "land_context": "<context input sha256>"
  },
  "top_target_count": 5
}
```

Each target is a Point whose feature ID equals `target_id`. Required scalar
properties are:

```text
target_id, target_rank, score, score_grade, score_geology,
open_ground_status, open_ground_value, open_ground_display,
surface_record_id, mineral_interest_id,
surface_class, mineral_class, approach, source_id
```

For a non-claim state, the only valid open-ground tuple is
`not_applicable / null / N/A`. Numeric zero is rejected. `score` must equal
`score_grade + score_geology`; no zero-valued open-ground term is added. Ranks
must be unique and contiguous, with deterministic order by descending score
then target ID.

The ranked `land_context` input hash is:

```text
sha256("<surface snapshot sha256>:<mineral snapshot sha256>")
```

This prevents publishing target cards scored against an older ownership
generation.

### Ownership joins

Surface features are Polygon/MultiPolygon records with at least:

```text
record_id, surface_class, surface_manager, source_id, source_scale,
mineral_interest_id
```

Their `source_id` must already be declared in the state's registry
`land_context.source_ids`. Registry review comes before publication.

Mineral-interest features are Polygon/MultiPolygon records with at least:

```text
record_id, mineral_class, confidence, evidence_basis, source_id, note
```

Adapters must pre-partition surface geometry along mineral-interest boundaries.
The builder proves that every published surface polygon is contained by its
referenced mineral polygon and that no mineral record is silently dropped.
Every ranked target must intersect exactly its declared surface record and
exactly its declared mineral record; overlap ambiguity fails the build.

## Build and validation

Tippecanoe 2.79 or newer is required:

```bash
python3 pipelines/build_national_land_context_pmtiles.py \
  --staging-dir /absolute/private-land-context \
  --inventory /absolute/private-land-context/inventory.json \
  --publish-dir /absolute/release-assets/land-context
```

For a review build, `--state MI` may be repeated. A selected-state build still
checksums all 90 frozen files before and after tiling, so it cannot silently
mix national generations.

The tiler runs with no feature limit and no tile-size limit. After tiling, the
builder traverses the full PMTiles directory, decompresses every unique tile
payload, decodes every MVT feature, and checks:

- the archive contains exactly `land_context` polygons and `target_context`
  points;
- every feature has the state and required release properties;
- the exact context and target identifier sets survived tiling;
- rank, score, join keys, surface class, mineral class, and approach match the
  frozen inputs;
- every target still says `not_applicable` / `N/A` and has no numeric
  open-ground field.

All 90 input snapshots, the inventory, Census clip, 49 state registry files,
registry defaults, and national source catalog are rehashed before installation
and again before pointer advancement. The immutable generation records the
registry-generation hash used to choose sources and routes.

## Outputs and release handoff

Successful builds create only add-only, content-addressed objects plus an
atomic index:

```text
artifacts/mi-land-context-<full artifact sha256>.pmtiles
generations/mi-land-context-generation-<full evidence sha256>.json
latest.json
```

The immutable generation records input hashes and counts, source provenance,
the clip hash, scoring method and top five IDs, PMTiles hash and bytes,
required properties, exact layer counts, and full-scan status. `latest.json`
points to both immutable files and preserves states not selected by the current
run.

Publication is not release. An operator must review the immutable generation,
copy/promote the exact content-addressed artifact through the normal release
workflow as `<full artifact sha256>.pmtiles`, populate the state's registry
`artifact`/`sha256`/`bytes` and count/evidence fields, run
the national release validator and browser/storage acceptance suite, and only
then consider the state DONE. This builder deliberately performs none of
those registry or release mutations. Its friendly artifact and generation
filenames plus `latest.json` remain in the review tree; they are not valid
release-upload objects.
