# Federal MLRS PMTiles publication

`pipelines/build_federal_mlrs_pmtiles.py` is the registry-driven publication
boundary for federal BLM claims. It consumes private snapshots, never S3 or
browser JSON, and publishes one immutable PMTiles generation with exactly two
source layers: `active` and `closed`.

This path is intentionally separate from the legacy compatibility builder.
It does not overwrite `site/data/tiles/national/claims.pmtiles`, alter
`site/data/manifest.json`, enable a state, or assert a DONE gate.

## Required private staging contract

Download the private `staging/claims/` objects to a directory outside
`site/`. The directory must contain all 38 canonical files:

```text
ak_active.json
ak_closed.json
...
wy_active.json
wy_closed.json
inventory.json
```

Compile the inventory from the downloaded snapshots rather than hand-writing
counts or completeness flags:

```bash
python3 pipelines/build_federal_mlrs_inventory.py \
  --staging-dir /private/mlrs-2026-08-13 \
  --state-clips infra/state_clips.json \
  --created 2026-08-13
```

The compiler hashes all 38 files, derives completeness from the producer's
pagination/clip attestation, then runs the full row/identity/state-clip
validator before atomically replacing the private `inventory.json`. The
inventory must name exactly the registry's 19 claim states and checksum every
file:

```json
{
  "schema_version": 1,
  "system": "federal_mlrs",
  "source": "https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer",
  "created": "2026-08-13",
  "clip": {
    "authority": "U.S. Census Bureau TIGERweb, January 1 2025 vintage",
    "method": "claim-polygon centroid within authoritative state polygon",
    "artifact_sha256": "<sha256 of infra/state_clips.json>"
  },
  "states": {
    "AK": {
      "active": {
        "file": "ak_active.json",
        "n": 0,
        "bytes": 231,
        "sha256": "<64 lowercase hex characters>",
        "retrieved": "2026-08-13",
        "complete": true
      },
      "closed": {
        "file": "ak_closed.json",
        "n": 0,
        "bytes": 210,
        "sha256": "<64 lowercase hex characters>",
        "retrieved": "2026-08-13",
        "complete": false,
        "partial_reason": "pagination has not completed"
      }
    }
  }
}
```

The abbreviated example shows one state only; a real inventory with anything
other than all 19 states and both modes fails before snapshot processing.
`complete` must be an explicit boolean. A false value requires a reason and
is rejected by `full` and `release` profiles. Snapshot flags such as
`truncated`, `partial`, `partial_after_spatial_clip`, or `total_available > n`
also make a snapshot partial even if an inventory incorrectly says complete.

Each snapshot uses the Lambda's columnar schema. Both modes require `state`,
`layer`, `retrieved`, `n`, and row-aligned `serial`, `name`, `type`, `x`, `y`,
`admin_state`, and `geo_state` arrays. Active additionally requires `disp` and
`acres`. The builder checks strict JSON, state/layer identity, row alignment,
serial uniqueness, coordinates, text/numeric types, inventory bytes and SHA,
and point containment in the checksum-pinned Census state polygon.

New updater snapshots also carry machine-produced `source`, `spatial_clip`,
and `pagination` attestations. The pagination record pins OBJECTID cursor
direction, 2,000-row page size, cumulative nonempty pages, registered-envelope
count, and one terminal empty page for every completed envelope. `complete` is
true only after all envelopes reach that empty page. The clip record binds the
same full `infra/state_clips.json` SHA-256 used by the builder. Full/release
profiles require this evidence; a legacy snapshot with none of the three
attestations is explicitly partial and remains usable only for a diagnostic
progress build. A missing, mixed, or contradictory attestation fails in every
profile. Thus an operator cannot turn a checkpoint or capped pull into a
release simply by writing `"complete": true` in `inventory.json`.

An explicitly complete zero-row snapshot is valid. Its state remains present
in the publication manifest with count `0`; it is never changed to or confused
with `N/A`. `N/A` remains exclusive to the non-claim regime. The builder does
not emit fake sentinel features for zero-row states.

## Build and publish an immutable generation

Use a private working directory and an explicit output location:

```bash
python3 pipelines/build_federal_mlrs_pmtiles.py \
  --staging-dir /private/mlrs-2026-08-13 \
  --inventory /private/mlrs-2026-08-13/inventory.json \
  --state-clips infra/state_clips.json \
  --publish-dir build-output/federal-mlrs \
  --profile release
```

The default profile is `release`. `full` is the same strict completeness gate;
`progress` permits partial snapshots, marks every emitted row from them with
`partial=1`, and records the partial states/snapshots. To prevent a diagnostic
run from replacing the default release pointer, progress writes
`progress.json` unless `--latest-manifest` is explicitly supplied. Progress
output must not be promoted as release-complete.

The builder writes temporary GeoJSON sequences outside `site/`, calls
Tippecanoe with `active` and `closed` inputs, and fixes the base zoom at z13.
`--no-feature-limit` and `--no-tile-size-limit` prevent the as-needed density
guards from silently deleting records at maximum zoom; normal lower-zoom point
sampling remains available for overview-tile budgets. Every source row carries
a deterministic, status-scoped, JavaScript-safe integer `fid`, which becomes
the MVT top-level feature ID. Before publication, the builder fully decodes the
pending PMTiles archive and requires the unique z13 IDs and z13 feature-instance
coverage to reconcile independently for `active` and `closed`: unique IDs must
equal the normalized source-ID sets and raw instances must not undercount them.
Raw instances can be higher because a buffered point at a tile boundary is
legitimately encoded in both neighboring tiles. It then rehashes all 38 inputs.
The published archive is content-addressed:

```text
federal-mlrs-<first-20-sha256-hex>.pmtiles
latest.json
```

The archive is installed and fsynced before an advisory-lock-protected,
atomic replacement of `latest.json`. The latest manifest is read inside the
lock, so unrelated concurrent artifact entries are preserved. A crash can
leave an unreferenced immutable generation, but it cannot make `latest.json`
point at an incomplete archive. Input mutation after tiling leaves the latest
pointer unchanged.

`latest.json` records a `source_id_inventory` with the normalized record count,
unique max-zoom tiled-ID count, canonical sorted-ID SHA-256, and the same fields
for each source layer. The raw ID arrays stay private build memory. A count or
digest match is evidence for the exact immutable archive named by `sha256`; it
is not inferred from PMTiles metadata or a sample tile.

## Promotion and deploy boundary

The checked generation can be built directly beneath a site-data path only
after review, for example:

```bash
python3 pipelines/build_federal_mlrs_pmtiles.py \
  --staging-dir /private/mlrs-2026-08-13 \
  --inventory /private/mlrs-2026-08-13/inventory.json \
  --publish-dir site/data/tiles/national/federal-mlrs \
  --profile release
```

The builder's checked national generation may be staged below
`site/data/tiles/**`, which the normal data sync deploys. Per-state archives
used as DONE-gate evidence must instead be promoted to their content-addressed
registry paths below `site/map-assets/releases/`; `deploy` and `update-site`
upload that immutable tree before publishing its mutable manifest pointers,
and `upload-release-assets` performs the same add-only upload explicitly.
Promotion still requires a reviewed merge from this generation's
`latest.json` into the public manifest and the relevant registry artifact
fields. Each tiled delivery uses exact `artifact`/`sha256`/`bytes`; each claim
publication inventory uses `publication_inventory_artifact`,
`publication_inventory_sha256`, and `publication_inventory_bytes`. That merge
is intentionally not performed by this builder, so an
archive build cannot silently turn on a state or replace the currently served
compatibility baseline.

Promotion means copying the reviewed bytes, not reusing the builder's friendly
pointer filename: the release PMTiles basename must be exactly
`<full-sha256>.pmtiles`, and a promoted publication inventory basename must be
exactly `<full-sha256>.json`. Keep `latest.json` and all review-tree files out
of `site/map-assets/releases/`; the release uploader accepts only registry
descriptors and rejects those mutable or unreferenced files.

## Known real-data blockers

- The Lambda producer now pages state-clipped closed claims to exhaustion by
  default. `CLOSED_CAP` is an explicit progress/debug override only; capped
  output is labeled `truncated` and rejected by full/release publication.
  Its active and closed EventBridge schedules are regression-tested to cover
  every one of the registry's 19 claim states exactly once per mode.
  Existing NV/UT/WY compatibility archives remain partial historical inputs
  until fresh uncapped pulls are built and promoted.
- The per-state release contract requires federal `active`, `closed`, and
  derived `open_ground` source layers. This builder deliberately publishes the
  two authoritative claim-status layers requested here. Registry promotion
  records this archive as the logical system's `claims` publication artifact
  and the separately validated `OPEN-GROUND-PUBLICATION.md` output as its
  `open_ground` publication artifact. Their disjoint layer sets, counts,
  schemas, input checksums, and archive hashes must reconcile before any claim
  state's DONE gate may pass.
- Alaska state-law claims remain a separate AK DNR archive. This federal
  builder neither replaces nor merges that dual-system dataset.

## Verification

The tests are entirely local and require no S3 access:

```bash
python3 -m unittest tests.test_federal_mlrs_pmtiles -v
```

They cover exact registry scope, honest zeroes, clip provenance and containment,
identity/ragged/duplicate failures, partial/capped rejection, stable IDs,
input mutation, lossless max-zoom source-ID reconciliation, PMTiles layer
metadata, immutable publication, concurrent latest-manifest merge, and
failure-before-pointer-update behavior.
