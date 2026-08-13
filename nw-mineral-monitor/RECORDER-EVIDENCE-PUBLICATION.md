# WS11 recorder evidence publication

`pipelines/build_national_recorder_evidence.py` compiles the recorder portion
of the WS11 DONE gate for the exact 19 claim states:

```text
AK AZ AR CA CO FL ID LA MS MT NE NV NM ND OR SD UT WA WY
```

It is an offline evidence compiler. It does not download claims, query or
scrape recorder portals, edit `states/*.yaml`, change release flags, update the
browser manifest, or publish map data. Those separations are intentional: a
recorder matrix becomes release evidence only after an operator has reviewed
the official jurisdiction source and each portal row.

Lower-48 jurisdictions use five-digit county FIPS. Alaska uses the names in
the Alaska DNR recording-district boundary service—not boroughs, census areas,
or county-equivalent FIPS. Federal Alaska MLRS claims and Alaska state mining
claims are joined independently and remain separate in the evidence.

## Inputs

Put all inputs beneath one private staging directory outside `site/`. Paths in
the root inventory and the two claim publication inventories must be relative,
normalized, and contained by their respective inventory directories. Symlinks,
absolute paths, traversal, duplicate JSON keys, and non-finite JSON numbers are
rejected.

A typical layout is:

```text
/private/ws11-recorders/
  inventory.json
  state-clips.json
  publications/
    federal.json
    ak-state.json
    active/
      ak.geojsonseq
      az.geojsonseq
      ...
      wy.geojsonseq
      ak-state.geojsonseq
  jurisdictions/
    ak.json
    az.json
    ...
    wy.json
  matrices/
    ak.json
    az.json
    ...
    wy.json
```

Every descriptor is `{path, bytes, sha256}`. `sha256` is lowercase SHA-256 of
the exact bytes, and `bytes` is the exact file size. The root inventory has
this strict shape:

```json
{
  "schema_version": 1,
  "dataset": "ws11-national-recorder-evidence",
  "snapshot": "2026-08-13",
  "state_clips": {
    "path": "state-clips.json",
    "bytes": 123,
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "publications": {
    "federal_mlrs": {
      "path": "publications/federal.json",
      "bytes": 123,
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    },
    "alaska_state_claims": {
      "path": "publications/ak-state.json",
      "bytes": 123,
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  },
  "jurisdictions": {
    "AK": {"path": "jurisdictions/ak.json", "bytes": 123, "sha256": "0000000000000000000000000000000000000000000000000000000000000000"},
    "AZ": {"path": "jurisdictions/az.json", "bytes": 123, "sha256": "0000000000000000000000000000000000000000000000000000000000000000"}
  },
  "portal_matrices": {
    "AK": {"path": "matrices/ak.json", "bytes": 123, "sha256": "0000000000000000000000000000000000000000000000000000000000000000"},
    "AZ": {"path": "matrices/az.json", "bytes": 123, "sha256": "0000000000000000000000000000000000000000000000000000000000000000"}
  }
}
```

The abbreviated example must be expanded to all 19 keys in both state maps.
The two publication keys are exact; aliases or combined Alaska systems are not
accepted.

### Active-claim publication inventories

Each claim system has its own checksum-pinned publication inventory:

```json
{
  "schema_version": 1,
  "system_id": "federal_mlrs",
  "source_url": "https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer",
  "created": "2026-08-13",
  "states": {
    "NV": {
      "active": {
        "file": "active/nv.geojsonseq",
        "format": "geojsonseq_v1",
        "n": 275121,
        "bytes": 123456789,
        "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "retrieved": "2026-08-13",
        "complete": true,
        "truncated": false,
        "total_available": 275121
      }
    }
  }
}
```

The federal inventory must contain exactly all 19 claim states. The Alaska
state inventory must contain exactly `AK`. `complete` must be true,
`truncated` false, and `n` must equal `total_available`; this deliberately
rejects capped or partial snapshots. A complete zero-row system is represented
by a zero-byte sequence with all three counts set to zero. A state whose exact,
complete published systems all contain zero active rows emits an explicit
`active_claims: 0` finding with empty live/covered/matrix lists. It does not
invent a county. Missing, partial, capped, or unreadable claim input is never
treated as zero.

The active files are newline-delimited GeoJSON Features. Every record ends in
a newline; an RFC 8142 record-separator byte is allowed at the start of a
record. The strict per-record contract is:

```json
{
  "type": "Feature",
  "id": "federal_mlrs:NV:NMC123456",
  "properties": {
    "claim_id": "NMC123456",
    "st": "NV",
    "system_id": "federal_mlrs",
    "status": "active"
  },
  "geometry": {"type": "Point", "coordinates": [-116.1, 39.2]}
}
```

Additional properties are allowed, but the four identity properties are
required and exact. Feature IDs must be unique within one state/system file.
Accepted geometry types are Point, MultiPoint, Polygon, and MultiPolygon. A
polygon claim activates every recorder jurisdiction it intersects; it is not
reduced to a centroid for this gate.

The upstream claims publisher should produce these sequences from the same
checked active snapshot used for its immutable claims archive. Do not derive
recorder evidence from the browser PMTiles archive, an API query with a result
cap, a manually sampled export, or a closed-claims layer.

### Authoritative state clips

`state-clips.json` uses the existing build-side schema:

```json
{
  "schema_version": 1,
  "source": "https://tigerweb.geo.census.gov/... January 1 2025 ...",
  "note": "Optional review note",
  "states": {"AL": {"type": "Polygon", "coordinates": []}}
}
```

It must contain exactly the WS11 49 states and identify the reviewed Census
TIGERweb January 1, 2025 source. The compiler uses it to reject claims carrying
the right state code but geometry outside that state. Use the checked
`infra/state_clips.json` bytes or a byte-identical private copy and pin the
chosen file in the root inventory.

### Recorder jurisdiction polygons

Each state jurisdiction artifact is a strict FeatureCollection:

```json
{
  "schema_version": 1,
  "type": "FeatureCollection",
  "state": "NV",
  "jurisdiction_type": "county",
  "authority": "U.S. Census Bureau TIGER/Line",
  "official_url": "https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html",
  "retrieved": "2026-08-13",
  "complete": true,
  "features": [
    {
      "type": "Feature",
      "properties": {"jurisdiction_id": "32007", "name": "Elko County"},
      "geometry": {"type": "Polygon", "coordinates": []}
    }
  ]
}
```

Lower-48 `jurisdiction_id` values must be five-digit county FIPS whose state
prefix matches the file's state. Alaska instead uses
`jurisdiction_type: recording_district`, and both `jurisdiction_id` and `name`
must be the reviewed DNR recording-district name, for example `Fairbanks
Recording District`. Alaska's official boundary source belongs in
`official_url`.

All real geometries must be closed, valid Polygon or MultiPolygon coordinates.
The empty coordinate arrays above are schema illustrations only and will fail.

### Reviewed portal matrices

Each matrix is operator-reviewed input, not scraper configuration:

```json
{
  "schema_version": 1,
  "state": "NV",
  "jurisdiction_type": "county",
  "status": "reviewed",
  "reviewed_on": "2026-08-13",
  "reviewed_by": "reviewer identity",
  "complete": true,
  "official_directory_url": "https://www.nvsos.gov/...",
  "rows": [
    {
      "jurisdiction_id": "32007",
      "status": "accepted",
      "portal_vendor": "Elko County Recorder search",
      "portal_url": "https://example.gov/official-recorder-search",
      "official_url": "https://example.gov/recorder"
    }
  ]
}
```

Every row must be `accepted`, name a portal vendor or official manual-access
facility, and provide both an HTTPS access URL and an HTTPS official-government
URL. Every ID must exist in the state's authoritative polygon artifact. Extra
reviewed rows are allowed, but every jurisdiction derived from live claims
must have a row. A directory lead, an unverified aggregator, or a vendor name
without an official URL is not accepted coverage.

## Build and verify

Choose a publication directory outside the private staging tree:

```bash
python3 pipelines/build_national_recorder_evidence.py \
  --inventory /private/ws11-recorders/inventory.json \
  --publish /review/ws11-recorder-evidence

python3 pipelines/build_national_recorder_evidence.py \
  --inventory /private/ws11-recorders/inventory.json \
  --publish /review/ws11-recorder-evidence \
  --validate-only
```

`--validate-only` ignores `--inventory`; it validates the current pointer,
content-addressed run, exact registry digest, and all 19 state evidence files.
The argument remains required so build and verify invocations use the same
operator wrapper.

The output is:

```text
/review/ws11-recorder-evidence/
  latest.json
  runs/<run-sha256>.json
  states/ak/<evidence-sha256>.json
  ...
  states/wy/<evidence-sha256>.json
```

State evidence contains exactly the schema consumed by
`validate_national._validate_recorder_evidence`:

```json
{
  "schema_version": 1,
  "state": "AK",
  "jurisdiction_type": "recording_district",
  "inventory_complete": true,
  "active_claims": 1,
  "live_claim_jurisdiction_ids": ["Fairbanks Recording District"],
  "covered_jurisdiction_ids": ["Fairbanks Recording District"],
  "matrix_jurisdiction_ids": ["Fairbanks Recording District"],
  "claim_systems": [
    {"system_id": "federal_mlrs", "active_claims": 0, "live_claim_jurisdiction_ids": []},
    {"system_id": "alaska_state_claims", "active_claims": 1, "live_claim_jurisdiction_ids": ["Fairbanks Recording District"]}
  ]
}
```

The run document carries the input inventory, state-clip, publication, system
count, registry, and state-evidence hashes. Inputs are rehashed after every
spatial join, immediately before immutable installation, and again before the
atomic pointer replacement. A failed build leaves `latest.json` unchanged.
Reusing identical inputs is idempotent; a different payload at an existing
content-addressed path is a hard collision failure.

## Promotion into the release registry

Publication does not promote evidence automatically. After review:

1. Copy or upload the immutable state JSON to a browser-relative immutable
   evidence path.
2. Populate the state's `recorder.matrix` with exactly the rows whose IDs are
   in `live_claim_jurisdiction_ids`. Preserve `status: accepted`,
   `portal_vendor`, and the reviewed `portal_url`.
3. Set `release.acceptance.recorders.evidence_artifact`, `evidence_sha256`, and
   `evidence_bytes` from that immutable JSON's run descriptor.
4. Copy `jurisdiction_type`, `inventory_complete`,
   `live_claim_jurisdiction_ids`, and `covered_jurisdiction_ids` exactly from
   the evidence.
5. Run the national release validator. Do not toggle `released` merely because
   recorder evidence passed; every other per-state DONE item remains
   independent.

The release validator reconciles four sets exactly: asserted live IDs,
asserted covered IDs, evidence matrix IDs, and registry matrix IDs. It also
requires the per-system jurisdiction union to equal the state live set.

## Fail-closed conditions

The compiler rejects, before advancing its pointer:

- any state set other than the exact claim-state 19;
- a missing, renamed, merged, or extra claim system;
- partial, capped, truncated, count-mismatched, or mutated active inputs;
- a claim with the wrong state/system/status identity;
- duplicate active feature IDs;
- off-state or jurisdiction-unmapped claim geometry;
- county IDs that are not state-matching five-digit FIPS;
- Alaska borough/FIPS substitutions for DNR recording-district names;
- missing, duplicate, unaccepted, non-HTTPS, or nonofficial portal rows;
- a zero combined active inventory unless every system input is complete,
  uncapped, untruncated, declares `n == total_available == 0`, and the emitted
  active count plus live/covered/matrix lists are exactly zero/empty;
- input symlinks, path traversal, public raw staging, or checksum drift;
- immutable output collisions or a pointer/run/evidence reconciliation error.

## Current real-data blockers

The compiler and synthetic acceptance suite do not make any state DONE. A real
national run still needs all of the following reviewed inputs:

- an uncapped, complete federal active-claim publication for all 19 states,
  including federal Alaska;
- a checksum-pinned Alaska DNR active state-claim sequence produced from the
  same reviewed snapshot as its state-claim publication;
- authoritative statewide county polygons for the 18 lower-48 claim states
  and the complete Alaska DNR recording-district polygon layer; and
- accepted portal-vendor/official-URL matrices for every jurisdiction returned
  by the live-claim joins.

Legacy capped claim snapshots, the existing Alaska browser baseline by itself,
and unverified recorder leads are not substitutes for those inputs.
