# Per-state CI acceptance evidence

`pipelines/build_ci_acceptance_evidence.py` converts one real browser release
run into the immutable JSON consumed by `release.acceptance.ci_scale`. It is an
evidence compiler, not a release command: it never changes `states/*.yaml`,
`site/data/manifest.json`, `site/data/coverage.json`, or any release flag.

## Candidate snapshots

Run the browser against candidate copies of the release manifest and coverage
file. The candidate coverage must still contain the exact 49-state scope, but
the state under test must read:

```json
{
  "state": "NV",
  "release": "done",
  "enabled": true,
  "gate_passed": true
}
```

Its seven gates must all be `pass` or the legally explicit
`not_applicable`. This snapshot is a browser fixture until review succeeds; do
not replace the checked-in public files or enable the registry state merely to
run the test. The compiler hashes the exact manifest and coverage bytes. A run
against one candidate cannot be presented as evidence for another.

The candidate manifest must advertise at least one `tiled_layers` descriptor
for the state. Every vector descriptor needs a stable ID, source ID, PMTiles
URL, source layer, `availability: "complete"`, `complete: true`, and a reviewed
nonnegative `n`. Every COG descriptor needs its XYZ browser template and the
immutable COG URL, byte count, and SHA-256.

## Browser runner JSON contract

The raw result is private CI output, never a file under `site/`. It is strict
JSON: duplicate keys, `NaN`, infinities, unknown fields, and schema drift fail.
The browser test identity is fixed at:

```json
{"id":"nwmm-state-release-browser","version":2}
```

The top-level runner document has these exact fields:

```json
{
  "schema_version": 1,
  "test": {"id": "nwmm-state-release-browser", "version": 2},
  "generated": "2026-08-13T19:20:21Z",
  "state": "NV",
  "profile": "release",
  "status": "green",
  "run_url": "https://ci.example.test/runs/4901",
  "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "input_bindings": {
    "manifest": {"bytes": 123, "sha256": "<64 lowercase hex>"},
    "coverage": {"bytes": 456, "sha256": "<64 lowercase hex>"},
    "budgets": {"bytes": 789, "sha256": "<64 lowercase hex>"}
  },
  "browser": {
    "engine": "chromium",
    "engine_version": "140.0.7339.5",
    "playwright_version": "1.62.1",
    "headless": true
  },
  "state_toggle": {
    "state": "NV",
    "coverage_enabled": true,
    "coverage_gate_passed": true,
    "initial_on": true,
    "off_observed": true,
    "on_observed": true,
    "final_on": true,
    "green": true
  },
  "descriptor_observations": [],
  "measurement_samples": [],
  "failures": {
    "page_errors": [],
    "map_errors": [],
    "request_failures": [],
    "http_errors": [],
    "console_errors": [],
    "unhandled_rejections": [],
    "statewide_json_requests": []
  },
  "statewide_browser_json": false
}
```

The CLI repeats the state, commit, and run URL. They must equal the raw result
exactly. Commits are full 40- or 64-character lowercase Git object IDs, not a
short display SHA. The run URL is HTTPS and cannot contain credentials or a
fragment.

### Descriptor observations

There is exactly one observation for every manifest descriptor whose `state`
matches the candidate, and no extras. The runner computes
`descriptor_sha256` over canonical JSON (UTF-8, sorted keys, compact
separators) for that complete manifest descriptor.

```json
{
  "descriptor_id": "nv-geology-units",
  "descriptor_sha256": "<descriptor SHA-256>",
  "delivery": "pmtiles",
  "source_id": "nv-geology-source",
  "source_layer": "units",
  "source_url": "pmtiles://map-assets/releases/nv/<sha256>.pmtiles",
  "runtime_style_layer_ids": ["ws11-nv-geology-units"],
  "visit_order": 0,
  "visit_mode": "exclusive_sequential",
  "visit_category": "geology",
  "visit_bounds_index": 0,
  "visit_center": [-117.0, 38.5],
  "visit_zoom": 4,
  "state_filter": ["==", ["get", "st"], "NV"],
  "source_present": true,
  "source_loaded": true,
  "state_scope_applied": true,
  "queryable": true,
  "query_status": "nonempty",
  "queried_features": 4,
  "successful_source_requests": 2
}
```

For PMTiles, the compiler first requires a nonempty, ordered `view_bounds`
array, integer `activation_minzoom`, and the exact state filter both on the
descriptor and every style layer. The runner then visits each descriptor
alone, in exact order `0..n-1`, at a center within one advertised bound and a
zoom at or above activation. It records the category and exact state filter
that were active during the query. Alaska's multiple antimeridian-safe bounds
are represented by `visit_bounds_index`; they are never collapsed into a fake
wrapped rectangle.

`queried_features` means features remaining after that exact state filter, not
every row in a shared archive. The allowed results are:

| Descriptor | Required observation |
|---|---|
| PMTiles with `n > 0` | `query_status: "nonempty"`, `queried_features > 0` |
| PMTiles with `n == 0` | `query_status: "declared_zero"`, `queried_features: 0` |
| COG/XYZ raster | `query_status: "raster_loaded"`, `queried_features: null` |

All three cases require the source and runtime style layers to be present, the
source to settle loaded, the state scope to be applied, the query/probe to
complete without error, and at least one successful source request. Missing is
never converted to zero. A vector layer reporting zero without manifest
`n: 0` fails.

### Measurements and error capture

The runner records one settled state-off sample plus one settled sample for
every exclusive sequential descriptor visit:

```json
[
  {
    "label": "nv-off",
    "phase": "state_off_settled",
    "descriptor_id": null,
    "visit_order": null,
    "heap_mb": 37.2,
    "bulk_origin_storage_mb": 0
  },
  {
    "label": "nv-visit-0",
    "phase": "descriptor_visit_settled",
    "descriptor_id": "nv-geology-units",
    "visit_order": 0,
    "heap_mb": 64.5,
    "bulk_origin_storage_mb": 0
  }
]
```

The published state-on maximum is computed across these sequential visits;
the test never allocates every heavyweight descriptor simultaneously. Every
sample—not only the maximum—must fit the current limits in `ci/budgets.json`.
Precise heap measurement must therefore be available; null is not accepted.
The runner must collect page exceptions, MapLibre errors,
failed requests, HTTP errors, console errors, unhandled rejections, and any
request for statewide JSON. Every list must be empty.

## Compile and review

```bash
python3 pipelines/build_ci_acceptance_evidence.py \
  --browser-result /private/ci/NV-browser.json \
  --manifest /private/ci/NV-manifest.json \
  --coverage /private/ci/NV-coverage.json \
  --state NV \
  --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --run-url https://ci.example.test/runs/4901 \
  --publish-dir site/map-assets/releases/ci-acceptance
```

The only output file is:

```text
site/map-assets/releases/ci-acceptance/nv/<evidence-sha256>.json
```

There is no mutable `latest.json`. Input files are rehashed immediately before
and after installation. An input race can leave, at worst, an unreferenced
content-addressed blob; it cannot update a state or manifest.

On success, stdout includes the exact object to copy—after human review—into
`release.acceptance.ci_scale`:

```json
{
  "evidence_artifact": "map-assets/releases/ci-acceptance/nv/<sha256>.json",
  "sha256": "<sha256>",
  "bytes": 12345,
  "run_url": "https://ci.example.test/runs/4901",
  "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "state_toggle_green": true,
  "statewide_browser_json": false,
  "heap_mb": 64.5,
  "bulk_origin_storage_mb": 0
}
```

The emitted evidence retains the runner-result hash, candidate manifest and
coverage hashes, budget hash and limits, browser/test versions, complete
descriptor reconciliation, raw settled measurements, and empty error sets.
The DONE validator checks the content-addressed filename and hash, then replays
the evidence against the current public manifest, current coverage snapshot,
and current budget file. A stale candidate hash, forged descriptor observation,
missing raster request/lifecycle proof, zero queried features for a positive
manifest count, unsupported browser-test version, over-budget sample, or any
browser/network/map error fails closed. The compiler still does not authorize
or enable a release.
