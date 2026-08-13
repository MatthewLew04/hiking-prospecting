# Federal open-ground PMTiles publication

`pipelines/build_national_open_ground_pmtiles.py` is the production boundary
between private section-level staging and the claim-state `open_ground` vector
layer. It supports either one reviewed claim state or the exact national set
of 19 claim states. It does not process non-claim states, write browser
GeoJSON, edit `site/data/manifest.json`, enable a registry state, or claim that
a map classification establishes mineral title.

## Analysis contract

The analysis unit is one CadNSDI PLSS section polygon. Each emitted feature is
that section, with:

- `st`, `unit_id`, and `section_id` identity;
- `status`: `ACTIVE`, `OPEN`, `WITHDRAWN`, `NONFEDERAL`, or `UNKNOWN`;
- `open_count`, `section_count`, and `open_fraction` (`section_count` is one;
  `open_count` is one only for `OPEN`);
- active-claim and mineral-disposition evidence;
- a title caveat, withdrawal caveat, and checksummed provenance token.

`ACTIVE` wins when one or more staged active MLRS claims name the section in
their legal description. Otherwise an independently produced land-status row
must explicitly classify the mineral disposition. Surface manager alone is
never treated as proof that the mineral estate is open to location.

An `OPEN` result therefore requires all of the following:

1. the state is one of the registry's exact 19 claim states;
2. the complete, uncapped active MLRS snapshot contains no active claim mapped
   to the section and no unmapped active claim anywhere in the state;
3. the section has a land-status row checked against SMA, withdrawals,
   segregations, and NLCS wilderness/WSA sources;
4. that row explicitly says `open_to_location`, is not boundary-uncertain, and
   carries no withdrawal reference.

This is a conservative research screen. It is not a title search and does not
replace current MLRS, Master Title Plat, Historical Index, withdrawal case,
state mineral-title, or county-recorder research.

## Private staging layout

Raw staging and its inventory must be outside `site/`. The inventory must name
exactly the registry-derived claim states (AK, AZ, AR, CA, CO, FL, ID, LA, MS,
MT, NE, NV, NM, ND, OR, SD, UT, WA, WY), and exactly three artifacts per state:

```text
private-open-ground/
  inventory.json
  ak_plss.json
  ak_active_claims.json
  ak_land_status.json
  ...
  wy_plss.json
  wy_active_claims.json
  wy_land_status.json
```

The two geometry-backed inputs can be produced one state at a time from an
uncapped active snapshot and the current official services:

```bash
python3 pipelines/build_open_ground_claim_plss_staging.py \
  --state NV \
  --active-snapshot /private/mlrs/nv_active.json \
  --plss-output /private/open-ground/nv_plss.json \
  --claims-output /private/open-ground/nv_active_claims.json
```

The producer requires Shapely 2.x. It snapshots exact object-ID sets from the
official active-claim polygon and CadNSDI section layers, reconciles every
object-ID page, clips sections to the checksum-pinned Census state polygon,
and uses positive-area polygon intersection to map claims. Mere boundary
touching is not counted. Duplicate MLRS records for one serial are unioned;
the unique live serial set must equal the exact machine-attested active
snapshot, or neither output is replaced. The two private output files are
installed as a rollback-safe pair. Each run reports official layer-metadata,
object-ID-set, active-snapshot, and state-clip hashes for inventory review.

This producer does not create the third `land_status` file and never calls a
section open. Mineral disposition still requires an independent exhaustive
overlay against all four sources below. Any claim with no positive-area PLSS
intersection is emitted as `mapping_complete: false`, increments
`unmapped_count`, and blocks a full/release build.

A separate conservative producer can build that third private input while
preserving the title boundary:

```bash
python3 pipelines/build_open_ground_land_status_staging.py \
  --state NV \
  --plss-snapshot /private/open-ground/nv_plss.json \
  --retrieved 2026-08-13 \
  --output /private/open-ground/nv_land_status.json
```

It snapshots and page-reconciles the official SMA, withdrawals, minerals and
surface segregations, Wilderness, and WSA layers. A section fully covered by
current mineral segregation, withdrawal, or designated Wilderness can be
classified `withdrawn`; a crossed boundary is `unknown`. Everything else
also remains `unknown` because BLM's national SMA layer represents surface
administrative jurisdiction and explicitly does not establish land title or
mineral-estate ownership. The producer therefore reports `open_sections: 0`
and `release_ready: false`; it never converts the absence of a closure polygon
into open ground. A reviewed public-domain mineral-estate/title source must be
added state by state before any `open_to_location` classification can pass a
full or release build.

Every inventory entry has the exact file name, row count, byte count, SHA-256,
retrieval date, and a boolean `complete`. `complete: false` requires a
nonempty `partial_reason`. The inventory also pins the exact Census 2025 state
clip hash and the fixed authoritative source endpoints used by the builder.

Minimal inventory shape:

```json
{
  "schema_version": 1,
  "system": "federal_open_ground",
  "created": "2026-08-13",
  "sources": {
    "plss": "<exact builder PLSS_SOURCE>",
    "active_claims": "<exact builder ACTIVE_CLAIMS_SOURCE>",
    "land_status": {
      "sma": "<exact builder source>",
      "withdrawals": "<exact builder source>",
      "segregations": "<exact builder source>",
      "nlcs": "<exact builder source>"
    }
  },
  "clip": {
    "authority": "U.S. Census Bureau TIGERweb, January 1 2025 vintage",
    "method": "PLSS analysis-unit representative point within authoritative state polygon",
    "artifact_sha256": "<64 lowercase hex characters>"
  },
  "states": {
    "AK": {
      "plss": {
        "file": "ak_plss.json",
        "n": 1,
        "bytes": 1000,
        "sha256": "<64 lowercase hex characters>",
        "retrieved": "2026-08-13",
        "complete": true
      },
      "active_claims": {"...": "..."},
      "land_status": {"...": "..."}
    }
  }
}
```

The real `states` object must contain all 19 states even for a per-state build.
All 57 files are checksummed when the inventory is opened and rechecked before
the publication pointer changes. This catches mutation of an unselected state
as well as mutation of a selected input.

## Snapshot schemas

All snapshots are strict UTF-8 JSON: duplicate keys, NaN/Infinity, unknown root
fields, ragged counts, wrong source identity, and a mismatched retrieval date
fail closed.

### PLSS sections

```json
{
  "schema_version": 1,
  "state": "NV",
  "kind": "plss",
  "retrieved": "2026-08-13",
  "complete": true,
  "n": 1,
  "source": "<exact builder PLSS_SOURCE>",
  "type": "FeatureCollection",
  "features": [{
    "type": "Feature",
    "id": "NV...",
    "properties": {
      "section_id": "NV...",
      "label": "T12N R34E Sec 5"
    },
    "geometry": {"type": "Polygon", "coordinates": []}
  }]
}
```

Section IDs must be state-prefixed and unique. Geometry must be a finite,
closed, nondegenerate Polygon or MultiPolygon at plausible PLSS scale. Its
representative point must fall inside the checksum-pinned authoritative state
polygon. Upstream staging remains responsible for state-edge geometric
clipping; the representative-point test prevents wrong-state partitioning.

### Active federal claims

```json
{
  "schema_version": 1,
  "state": "NV",
  "kind": "active_claims",
  "retrieved": "2026-08-13",
  "complete": true,
  "n": 1,
  "system": "federal_mlrs",
  "source": "<exact builder ACTIVE_CLAIMS_SOURCE>",
  "mode": "active",
  "unmapped_count": 0,
  "claims": [{
    "serial": "NMC123456",
    "name": "Example",
    "disposition": "ACTIVE",
    "source_object_id": 123,
    "section_ids": ["NV..."],
    "mapping_complete": true
  }]
}
```

The upstream producer must retain the entire authoritative active layer and
spatially join its claim polygons to CadNSDI sections by positive-area
intersection. Duplicate serials, duplicate section IDs, an empty supposedly
complete mapping, or a mapping to a section absent from the staged PLSS
partition makes the state unreleaseable.
`unmapped_count` must equal the number of rows with
`mapping_complete: false`; it cannot be a hand-entered waiver.

### Land-status classifications

```json
{
  "schema_version": 1,
  "state": "NV",
  "kind": "land_status",
  "retrieved": "2026-08-13",
  "complete": true,
  "n": 1,
  "sources": {"sma": "...", "withdrawals": "...", "segregations": "...", "nlcs": "..."},
  "classifications": [{
    "section_id": "NV...",
    "mineral_disposition": "open_to_location",
    "surface_manager": "BLM",
    "withdrawal_refs": [],
    "checked_sources": ["nlcs", "segregations", "sma", "withdrawals"],
    "boundary_uncertain": false,
    "evidence": "Section-level classification evidence.",
    "mineral_title_status": "public_domain_locatable",
    "mineral_title_source": "https://official.example/mineral-estate",
    "mineral_title_ref": "MTP-or-source-record-id",
    "mineral_title_reviewed": true
  }]
}
```

Allowed dispositions are `open_to_location`, `withdrawn`, `non_federal`, and
`unknown`. A `withdrawn` row needs at least one withdrawal/closure reference;
other dispositions cannot carry one. Release/full builds require exactly one
fully checked, boundary-certain, non-unknown row for every PLSS section.

The title fields are independent of the four closure/surface checks. `OPEN`
requires `public_domain_locatable`, an HTTPS source and record reference,
`mineral_title_reviewed: true`, and an exact match to a state registry
`mineral_estate.status: reviewed_ingested` source. `NONFEDERAL` similarly
requires reviewed non-federal title. Unknown title carries null source/ref and
false review. This prevents a section from becoming open merely because no
withdrawal polygon was returned.

All three snapshot kinds may carry `capped`, `truncated`, `partial`,
`total_available`, and `partial_reason`. A true flag, `total_available > n`, a
false completeness value, missing section coverage, an unmapped claim, or an
unknown/unchecked land row is rejected by `full` and `release`.

## Progress and release behavior

`progress` exists for reviewable, explicitly partial tiles. It never promotes
an incomplete no-claim result to `OPEN`:

- a mapped active claim remains `ACTIVE`;
- incomplete/capped claim coverage makes every other section in that state
  `UNKNOWN`;
- missing, unchecked, boundary-uncertain, or unknown land status makes the
  affected section `UNKNOWN`;
- every feature in an affected state carries `partial: 1`, and the pointer
  lists that state in `partial_states`.

`full` and `release` both fail before tiling when any selected state is
partial. A per-state release can therefore pass independently without
silently reducing the exact 19-state inventory contract. A national release
requires every one of the 19 states to pass.

## Commands

One state:

```bash
python3 pipelines/build_national_open_ground_pmtiles.py \
  --staging-dir /private/open-ground \
  --inventory /private/open-ground/inventory.json \
  --state NV \
  --publish-dir /review/open-ground/nv \
  --profile release
```

All 19 claim states (omit `--state`):

```bash
python3 pipelines/build_national_open_ground_pmtiles.py \
  --staging-dir /private/open-ground \
  --inventory /private/open-ground/inventory.json \
  --publish-dir site/data/tiles/national/federal-open-ground \
  --profile release
```

The builder streams temporary GeoJSONSeq outside the browser tree and invokes
tippecanoe with a single `open_ground` source layer. Density dropping and tile
size/feature limits are disabled. It then checks PMTiles v3
directories and counts, decompresses every unique MVT tile payload, and
validates every decoded feature's tags, geometry, required properties, state
identity (for per-state builds), and open-count/fraction math. Only then does
it compares the unique top-level feature IDs at maximum zoom to the derived
PLSS section-ID count and canonical ID-set hash. One omitted or extra section
aborts publication. The builder then installs a content-addressed archive
without overwrite, rehashes all inputs, and atomically merges its entry into
`latest.json`. It preserves unrelated entries written concurrently.

A release operator must still review and promote the immutable per-state path
and its exact bytes/hash/counts into that state's registry release evidence.
Copy the checked PMTiles bytes to a path below `site/map-assets/releases/`
whose basename is exactly `<full-sha256>.pmtiles`; map the builder pointer's
`file`/`sha256`/`bytes` to registry `artifact`/`sha256`/`bytes`. Do not copy the
review tree's `latest.json` or friendly/truncated archive name into the release
tree: both are rejected as unreferenced or noncanonical release assets.
Building an archive alone does not satisfy the DONE gate and does not enable a
state.

## Verification

The tests synthesize private inputs and fake only tippecanoe's archive bytes;
they do not download or publish real data:

```bash
python3 -m unittest tests.test_national_open_ground_pmtiles -v
```

They cover the exact 19-state/57-file contract, registry regime checks,
state-clipped polygon identity, one-feature-per-section math, status
precedence, honest progress degradation, caps and partials, unmapped claims,
land-source coverage, withdrawal evidence, stable IDs, input mutation,
PMTiles metadata plus real decoded MVT semantics/garbage rejection,
per-state/national scope, immutable installation, concurrent pointer merges,
and failure-before-pointer-update behavior.
