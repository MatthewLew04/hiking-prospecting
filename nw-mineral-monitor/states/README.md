# WS11 state registry

Each `XX.yaml` file is a JSON-compatible YAML document. Keeping the files in
the JSON subset lets the stdlib-only pipelines read them without PyYAML while
remaining reviewable as YAML.

The registry separates legal regime from data readiness:

- `claim` means federal Mining Law claim layers are applicable. Alaska also
  declares its independent state-claim system.
- `non_claim` means open-ground/staking is not applicable. Those states must
  publish target-level land context instead.
- `release.status: done` is legal only when every required DONE-gate item is
  `pass` (or an explicitly allowed `not_applicable`). Baseline national tile
  services may be visible while a state remains `building`; that is not a
  state release.

Every claim state also carries a typed `open_ground.mineral_estate` review.
The registry distinguishes an unidentified source, an official but uningested
candidate, an unavailable/insufficient candidate, and a reviewed ingest.
`surface_management_is_not_title` is always true: the national BLM Surface
Management Agency polygons describe surface administrative jurisdiction, not
public-domain mineral ownership. A reviewed ingest must name the exact title
field and locatable-mineral values. Until then, absence of a closure polygon
remains `UNKNOWN`, never `OPEN`.

Run the registry and gate checks from the project directory:

```sh
python3 pipelines/state_registry.py validate
python3 pipelines/build_coverage.py --check
```

Never put a statewide browser GeoJSON path in a registry entry. Claims,
statewide geology/faults, land context, and survey-index polygons must resolve
to PMTiles/vector tiles; statewide rasters must resolve to COG/WMTS/WMS.

Every file authorized by an enabled release is immutable and
content-addressed below `map-assets/releases/`. The basename is the lowercase
SHA-256 plus its delivery suffix (`.pmtiles`, `.tif`/`.tiff`, or `.json`), and
the registry records a positive exact byte count. Hidden, temporary,
noncanonical, and traversal path segments are invalid. The flat registry
schema uses this exact path/checksum/size mapping:

| Release file | Path field | SHA-256 field | Bytes field |
| --- | --- | --- | --- |
| PMTiles or COG delivery | `artifact` | `sha256` | `bytes` |
| Grade, CI, or zero-inventory JSON | `evidence_artifact` | `sha256` | `bytes` |
| Recorder, expiration-watch, AML, or trust decision JSON | `evidence_artifact` | `evidence_sha256` | `evidence_bytes` |
| Claim publication inventory JSON | `publication_inventory_artifact` | `publication_inventory_sha256` | `publication_inventory_bytes` |
| Ranked-target JSON | `ranked_targets_artifact` | `ranked_targets_sha256` | `ranked_targets_bytes` |
| Per-target quad inventory JSON | `inventory_artifact` | `inventory_sha256` | `inventory_bytes` |

`district_anchor.artifact`, when present, must be exactly the grade evidence
JSON and therefore reuses that file's grade `sha256` and `bytes`; its
`source_sha256` continues to identify the upstream PP 610 document, not the
compiled release file. AML and trust objects with `ingested_complete` carry
two independent descriptors: `artifact`/`sha256`/`bytes` for their PMTiles and
`evidence_artifact`/`evidence_sha256`/`evidence_bytes` for the decision JSON.
Disabled states retain null descriptor fields and authorize no release files.

Claim-state release evidence is artifact-backed. `release.acceptance.recorders`
declares a `jurisdiction_type`, complete inventory artifact, and exact live and
covered jurisdiction IDs; the recorder matrix and evidence JSON must contain
the same IDs. The normal type is `county` with five-digit FIPS IDs. Alaska uses
`recording_district` names and must inventory both `federal_mlrs` and
`alaska_state_claims`; borough or county FIPS are not substitutes. CI
acceptance likewise points to strict JSON binding the green run URL, commit,
state toggle, current heap/storage limits and measurements, and the absence of
statewide browser JSON.

Grade acceptance points to the immutable, content-addressed state JSON emitted
by `pipelines/build_national_grade_evidence.py`. Both
`release.acceptance.grades.evidence_artifact`, its exact `sha256`, and its
exact `bytes` are required for a released state; the filename is the digest. The validator
recomputes mine/source/quote/page counts, rechecks all primary-source identities
and citations, and validates either the 25-mine/two-source threshold or the
embedded two-primary-source low-endowment finding. `district_anchor.artifact`
is the same state JSON (or may be omitted in favor of the grade artifact), and
its nested `pp610` object is the only accepted district-anchor schema. The
legacy public `data/grades/grades.json` is not release evidence.

Every released claim system also points to a content-addressed
publication-inventory JSON with exact checksum and byte metadata. Its
state/system identity, retrieval date, exhausted pagination, non-truncated
completion flags, authoritative state-clip checksum, input checksum, and exact
per-source-layer counts must match the registry and PMTiles layer metadata.
Federal MLRS uses two immutable `publication_artifacts`: `claims` carries the
active/closed point layers, while `open_ground` carries the PLSS polygons.
Their source-layer sets must be disjoint, and their union, property schemas,
and reviewed counts must exactly match the logical `federal_mlrs` system.
The open-ground part additionally carries a `source_id_inventory`; unique
maximum-zoom feature IDs, its canonical ID-set hash, and the instance count
must reconcile every derived PLSS section. A density-dropped section cannot
pass. This avoids repacking independently validated products into one archive.
The DONE evidence also includes a state expiration-watch run: it must cover
every claim system declared for that state, report complete non-null counts,
and bind each diff baseline with a snapshot checksum. Alaska therefore cannot
pass on its DNR watch alone while its federal MLRS watch is absent (or vice
versa).

For a non-claim release, one land-context PMTiles archive must contain both a
statewide `land_context` layer and a `target_context` point layer for at least
the accepted top five. Every target point carries its surface class, mineral
class, score/rank, and party to approach; a generic ownership polygon is not a
substitute for the promised per-target card.

`release.acceptance.quad_maps.ranked_targets_artifact` is an immutable state
scoring JSON whose filename equals `ranked_targets_sha256`; its exact
`ranked_targets_bytes` is also required, and all three live below
`site/map-assets/releases/`. The preferred artifact is emitted by
`build_national_target_scoring_evidence.py`. The validator replays that
compiler's current-registry contract, reconciles all four input hashes, and
requires the separate five-row wrapper and quad inventories to match its first
five ranks exactly. The scoring artifact records separate nonnegative grade,
geology, and open-ground components. Claim-state releases require measured
open-ground; non-claim releases require a JSON null open-ground value with
status `not_applicable`. Rank validation is status-aware, so an N/A term can
never collapse into a measured numeric zero.

AML and state mineral-leasing sources are not satisfied by a registry URL
alone. `release_inventory_status` must be `ingested_complete` (immutable
PMTiles with reviewed complete layer counts) or an explicit artifact-backed
unavailability finding. Trust-land programs also need a reviewed
`offering_class`; an offered or limited program cannot be marked not
applicable.
