# WS11 national target-scoring evidence

`pipelines/build_national_target_scoring_evidence.py` is the evidence compiler
for national richOpen/target ranking. It compiles reviewed inputs for the exact
49-state registry; it does not research targets, fill missing evidence, edit
`states/*.yaml`, update `site/data/manifest.json`, create browser layers, or
enable a release.

No production inventory or compiled artifact is included in the repository.
An operator must supply a complete private inventory containing real reviewed
inputs. There is no default inventory, fallback data, partial-state mode, or
synthetic generation path.

## Two-regime scoring contract

Every target retains independent grade and geology components. Claim-state
totals are:

```text
grade score + geology score + measured open-ground score
```

Every claim state must provide a complete statewide open-ground snapshot and a
measurement for every ranked target. The snapshot keys must exactly match the
claim systems in the current registry. This makes Alaska special by contract:
its open-ground input must pin both `federal_mlrs` and
`alaska_state_claims` generations.

Non-claim totals are:

```text
grade score + geology score
```

Their open-ground value is always the following typed object:

```json
{
  "status": "not_applicable",
  "value": null,
  "unit": null,
  "score": null,
  "display": "N/A"
}
```

Numeric zero is invalid in that object. Each non-claim target must instead
carry an independently evidenced land-context card containing a surface class,
a mineral-ownership class and confidence, and an approach party such as a
state leasing office or private mineral-rights owner. Surface evidence cannot
stand in for mineral ownership evidence.

The deterministic tie order is positive measured open-ground evidence, legal
N/A, then measured zero, followed by descending target area and ascending
target ID. N/A therefore never passes through numeric coercion and cannot
collide with a measured zero sort key.

## Private inventory

Run inputs must remain outside `site/`. The inventory and every referenced file
are strict JSON: duplicate object keys, `NaN`, path traversal, absolute paths,
symlinks, checksum drift, and non-canonical descriptor shapes fail closed.

The top-level inventory is:

```json
{
  "schema_version": 1,
  "dataset": "ws11-national-target-scoring-evidence",
  "snapshot": "2026-08-13",
  "method_id": "ws11-richopen-reviewed-v1",
  "review": {
    "status": "reviewed",
    "reviewed_on": "2026-08-13",
    "reviewed_by": "Named review team"
  },
  "states": {
    "NV": {
      "ranked_targets": {"path": "states/nv/ranked_targets.json", "bytes": 1, "sha256": "..."},
      "grade_terms": {"path": "states/nv/grade_terms.json", "bytes": 1, "sha256": "..."},
      "geology_terms": {"path": "states/nv/geology_terms.json", "bytes": 1, "sha256": "..."},
      "open_ground": {"path": "states/nv/open_ground.json", "bytes": 1, "sha256": "..."}
    },
    "MI": {
      "ranked_targets": {"path": "states/mi/ranked_targets.json", "bytes": 1, "sha256": "..."},
      "grade_terms": {"path": "states/mi/grade_terms.json", "bytes": 1, "sha256": "..."},
      "geology_terms": {"path": "states/mi/geology_terms.json", "bytes": 1, "sha256": "..."},
      "land_context": {"path": "states/mi/land_context.json", "bytes": 1, "sha256": "..."}
    }
  }
}
```

`states` must equal the current registry exactly: 19 claim states and 30
non-claim states, all states except Hawaii. A missing or extra state fails the
entire run before publication. Every descriptor's `bytes` and `sha256` bind the
exact file bytes; changing whitespace is a checksum change.

Each of the four state documents declares schema version, state, kind,
`complete: true`, `truncated: false`, the same method ID, at least one HTTPS
source URL, and an explicit reviewed status/date/reviewer. All four target sets
must join exactly by stable `target_id`; duplicate IDs, ranks, or identical
name/location identities fail.

### Ranked targets

The ranked-target document supplies target identity, name, contiguous
`declared_rank`, area, longitude/latitude, and an optional district. The
compiler recomputes totals and sort order from the other three inputs and
rejects a declared ranking that disagrees.

### Grade and geology terms

Each term document pins a reviewed `source_artifact_sha256`. Every target row
provides a finite score from 0 through 100, one or more literal matched terms,
one or more evidence SHA-256 references including that source artifact, and a
review rationale. These inputs are independent and must cover the complete
ranked target set.

### Claim-state open ground

An open-ground document declares:

- `coverage_status: statewide_complete`;
- `all_ranked_targets_covered: true`;
- `source_snapshot_sha256s` keyed by the exact registry claim systems;
- one `measured` fraction from 0 through 1 and a score from 0 through 100 for
  every target.

Every target evidence-reference set must contain every declared claim-system
snapshot exactly once. A measured `0.0` is valid and remains numeric zero in
output; absent, unknown, or N/A open-ground values are invalid for claim states.

### Non-claim land context

A land-context document declares `coverage_status: per_target_complete` and
pins separate `surface`, `mineral`, and `leasing_or_title` source snapshots.
Each target repeats the exact typed-N/A object and provides:

- surface ownership/management class, party, and matching evidence hash;
- mineral ownership class, party, confidence, and independent matching hash;
- approach kind, named party, optional HTTPS portal, and matching leasing/title
  evidence hash;
- a review rationale.

Allowed approaches include state lease, private negotiation, a federal leasing
agency, a tribal mineral authority, multiple rightsholders, required title
research, and an explicitly documented absence of an available route.

## Build and validate

Publish an all-state reviewed evidence run outside raw staging:

```bash
python3 pipelines/build_national_target_scoring_evidence.py \
  --inventory /private/ws11-targets/inventory.json \
  --publish /reviewed/ws11-target-scoring
```

Validate the current pointer and every referenced state blob later with:

```bash
python3 pipelines/build_national_target_scoring_evidence.py \
  --validate /reviewed/ws11-target-scoring
```

The output layout is:

```text
<publish>/
  latest.json
  runs/<run-sha256>.json
  states/<state>/<state-evidence-sha256>.json
```

State and run documents are canonical JSON whose filenames are their exact
SHA-256. `latest.json` is replaced atomically only after all 49 states compile,
all immutable blobs install, and every input is rehashed. A source mutation
during a build can leave an unreferenced immutable blob but cannot install a
mixed `latest.json` pointer.

Every state blob embeds the exact byte count and SHA-256 of its four reviewed
inputs, the registry and inventory hashes, computed component/total scores,
typed open-ground evidence, land-context cards where applicable, and computed
metrics. Its `regime_evidence` summary also preserves the claim-system snapshot
mapping (including both Alaska systems) or the three independent land-context
snapshot hashes. Private source paths are deliberately omitted. Every output
declares `effect: evidence_only_no_release_mutation`.

## DONE-gate handoff

This compiler is a prerequisite, not a release decision. Copy a selected state
blob unchanged below `site/map-assets/releases/` and bind its digest as the
`scoring_sha256` in a separately content-addressed five-row ranked wrapper.
Set `release.acceptance.quad_maps.ranked_targets_artifact` and
`ranked_targets_sha256`/`ranked_targets_bytes` to that wrapper. Each of the five
target rows likewise records its quad `inventory_artifact`, exact
`inventory_sha256`, and exact `inventory_bytes`. The DONE validator replays the state
blob through this compiler's semantic validator, compares its method and four
input hashes to the wrapper, and requires the wrapper plus quad inventories to
match the compiled first five ranks. Copying evidence never toggles a state.

The compiler currently has no production reviewed inventory, so it publishes
nothing when merely checked out or run without explicit arguments.
