# Arizona state-survey baselines

Status: two independent private audits passed on 2026-08-13 with identical
complete archive bytes, and the corrective three-archive production transaction
published those exact bytes. This work remains `baseline_not_release` and
browser-disabled; it does not pass an Arizona DONE gate or alter
`states/AZ.yaml`.

## Source choice and identity

The statewide source is the Arizona Geological Survey's 2025 republication of
Map 35, collection `AGMS-1749135591815-872`. It restores the 2013 vector trace
of the 2000 map in a GeMS-compatible package. It is the complete statewide
1:1,000,000 baseline registered for Arizona; it is not a polygon-by-polygon
mosaic of larger-scale DGM maps.

The collection endpoint generates a ZIP per request. Its member timestamps,
ZIP byte count, and ZIP hash are transport observations, not source identity.
The builder therefore pins both:

- canonical AZGS catalog metadata SHA-256
  `acd549849cf3f7b924b102d26148dbf819b61dd2f4b1138170e41e0a5f9b1d10`;
- extracted member
  `AGMS-1749135591815-872/gisdata/layers/AZStatewide.gpkg`, exactly
  28,155,904 bytes, SHA-256
  `ee871b3fa38ec32e1fe4b41608b94758094b76ea4f2db7ef5a54c584c108924e`.

The pinned EPSG:26912 GeoPackage contains:

| layer/table | records | typed-schema SHA-256 |
|---|---:|---|
| `MapUnitPolys` | 4,841 | `42fec93f873d3abf2131476f3a85539c3e9526b41cd308277811e3127c71ddf5` |
| `ContactsAndFaults` | 15,563 | `7949e26bcae6d2b5ac9c8539b0ed39ca6c5ff4b88fc0e980ab16b69c23909b4d` |
| `DataSources` | 189 | `654504c943886e5d4cc944b97195266d553e70c34cc3e8b10b974df35cb26012` |
| `DescriptionOfMapUnits` | 50 | `b2f9a37a0796b332db0da5485af0c9a62b91c490d8fdae03f251d268f5d99886` |
| `Glossary` | 6 | `8d47c8a6d10c995c34b147f64ade1095708bfc28e65ded43e89043b1fb15656f` |
| `Symbology` | 2,273 | `53239707c5c6345c8175771b786c5f057ad03b12e3fef251aa54385fa388c817` |

Every geology polygon and structural line retains its GeMS `DataSourceID`.
The package's `DataSources` rows contain unique identifiers but no URL, native
scale, or citation text. Accordingly every feature records Map 35's
1:1,000,000 compilation scale and a status explaining that the retained GeMS
link does not establish a larger native scale. Detailed DGM substitution is
explicitly pending and outside this baseline.

Mining districts and critical-mineral occurrences come from the public
University of Arizona/AZGS Feature Service item
`beef607714624113b8f69c2a4bbc6a2d`, whose selected item metadata SHA-256 is
`a03846a2b182c9f6b1fb0ca6a948c7e8fb277b36fa1807109480e7220553f282`.
The builder pins typed layer metadata and `returnIdsOnly` snapshots, fetches
exact POST object-ID pages, hashes a full content pass, repeats the complete
content pass, and repeats the ID/metadata snapshot. The district service does
not state a map scale, so its scale is recorded as `not stated`; the point
occurrence layer records scale as not applicable. Public contact, email, and
telephone fields are deliberately not republished.

## Loss and boundary inventories

The authoritative boundary is the repository's checksum-pinned Census 2025
49-state clip. Validation precedes intersection.

- Map 35 geology: 4,841 source polygons, zero empty or invalid geometries,
  zero outside, 118 boundary-clipped, 4,841 tiled.
- Map 35 structure: 15,563 source lines, zero empty or invalid geometries,
  two entirely outside Arizona (`FID` 10075 and 15427), 109 boundary-clipped,
  and 15,559 tiled.
- Two valid two-vertex structural artifacts (`FID` 11371 and 11825) are
  respectively 0.219 m and 1.597 m long in a 1:1,000,000 compilation. They are
  below z12 MVT encoding resolution. Their source IDs, coordinates hashes, and
  lengths are pinned, and they are omitted rather than fabricated into longer
  lines.
- AZGS districts: 287 source/tiled polygons, zero empty, invalid, or outside,
  12 boundary-clipped.
- AZGS critical minerals: 24 source/tiled points, all inside Arizona.

Tippecanoe runs with fixed z0-z12, no feature limit, no tile-size limit,
`--drop-rate=1`, no tiny-polygon reduction at maximum zoom, and simplification
only below maximum zoom. Inputs and outputs use stable relative basenames under
Tippecanoe's private working directory. Name, description, attribution, and
generator metadata are explicit, and validation rejects absolute paths,
staging paths, or parent/current-directory tokens in embedded metadata. Every
archive is built twice at the same stable basename and must be byte-identical.
Two full pipeline invocations under different random staging roots must also
produce identical complete bytes. The semantic validator then scans every MVT
feature, required property, and maximum-zoom ID. A statewide GeoJSON is never
public.

## Corrective artifact contract

Two separate 2026-08-13 full private invocations, each with a different random
staging root, produced these exact deterministic outputs. The corrective
production transaction published the same complete bytes atomically. The
critical-mineral archive retains all 24 points at every encoded zoom because
density dropping is disabled. These fingerprints are the reviewed Arizona
publication contract.

| manifest key | public file | source layer(s) and unique features | bytes | SHA-256 | PMTiles bounds |
|---|---|---|---:|---|---|
| `az_azgs_map35_2025` | `data/tiles/states/az/azgs-map35.pmtiles` | `az_azgs_map35_geology` 4,841; `az_azgs_map35_faults` 15,559 | 28,043,535 | `f274a90887e9bc80e02df09a157590ba7c5b9f66be21bc08bb8bd43cc5f54a36` | `[-114.815212,31.332343,-109.04517,37.003547]` |
| `az_azgs_mining_districts` | `data/tiles/states/az/azgs-mining-districts.pmtiles` | `az_azgs_mining_districts` 287 | 1,924,600 | `17fe8297d192ae9179ec9a71e3211619638205fe556692a082d07e39567c7765` | `[-114.687164,31.33306,-109.045361,37.000031]` |
| `az_azgs_critical_minerals` | `data/tiles/states/az/azgs-critical-minerals.pmtiles` | `az_azgs_critical_minerals` 24 | 191,099 | `b89dccebf5e4ec1f7826f2cb5e9b46e7b18095da093f2f3de96b0c1f3e606a1e` | `[-114.315556,31.454167,-109.07527,36.50611]` |

Canonical raw PMTiles metadata SHA-256 values are
`23d14708712bbca9ff77aa49cb79dcb44a202ab56779577e4152ad587053e25d`
for Map 35,
`ce80e79678ef5f847a089b35e28344d5806e12136ffc75e3e36074d61d9d083a`
for districts, and
`44ceabeb50fccdf4380842c904482f28f326fb1bc1ff694ada9b6dda2dba716d`
for critical minerals. Their generator options contain only stable relative
basenames.

Every feature in every layer has these required properties:

`fid`, `st`, `source_dataset`, `source_id`, `source_record_id`,
`source_scale`, `source_scale_status`, `source_ref`, `source_url`, and
`publication_id`.

## Proposed lazy browser descriptor

This is the handoff contract for a generic state-survey loader. The Arizona
pipeline intentionally does not edit the UI, reconciler, or shared validator.

```json
{
  "state": "AZ",
  "activation": {
    "selected_state": true,
    "explicit_layer_toggle": true,
    "minimum_zoom": 5.5,
    "require_manifest_bounds_intersection": true,
    "teardown_source_and_pmtiles_protocol_on_false": true
  },
  "required_properties": [
    "fid", "st", "source_dataset", "source_id", "source_record_id",
    "source_scale", "source_scale_status", "source_ref", "source_url",
    "publication_id"
  ],
  "sources": [
    {
      "source_id": "az-map35",
      "manifest_key": "az_azgs_map35_2025",
      "file": "data/tiles/states/az/azgs-map35.pmtiles",
      "minimum_zoom": 0,
      "maximum_zoom": 12,
      "bounds": [-114.815212, 31.332343, -109.04517, 37.003547],
      "layers": [
        {
          "id": "az-map35-geology-fill",
          "source_layer": "az_azgs_map35_geology",
          "geometry": "fill",
          "unique_features": 4841,
          "paint": {"fill-color": "#806f68", "fill-opacity": 0.28}
        },
        {
          "id": "az-map35-faults-line",
          "source_layer": "az_azgs_map35_faults",
          "geometry": "line",
          "unique_features": 15559,
          "paint": {
            "line-color": "#f1c75b",
            "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.35, 11, 1.3],
            "line-opacity": 0.82
          }
        }
      ]
    },
    {
      "source_id": "az-districts",
      "manifest_key": "az_azgs_mining_districts",
      "file": "data/tiles/states/az/azgs-mining-districts.pmtiles",
      "minimum_zoom": 0,
      "maximum_zoom": 12,
      "bounds": [-114.687164, 31.33306, -109.045361, 37.000031],
      "layers": [
        {
          "id": "az-districts-fill",
          "source_layer": "az_azgs_mining_districts",
          "geometry": "fill",
          "unique_features": 287,
          "paint": {"fill-color": "#c98500", "fill-opacity": 0.08}
        },
        {
          "id": "az-districts-line",
          "source_layer": "az_azgs_mining_districts",
          "geometry": "line",
          "unique_features": 287,
          "paint": {
            "line-color": "#dca632", "line-width": 1,
            "line-dasharray": [4, 2], "line-opacity": 0.82
          }
        }
      ]
    },
    {
      "source_id": "az-critical-minerals",
      "manifest_key": "az_azgs_critical_minerals",
      "file": "data/tiles/states/az/azgs-critical-minerals.pmtiles",
      "minimum_zoom": 0,
      "maximum_zoom": 12,
      "bounds": [-114.315556, 31.454167, -109.07527, 36.50611],
      "layers": [
        {
          "id": "az-critical-minerals-circle",
          "source_layer": "az_azgs_critical_minerals",
          "geometry": "circle",
          "unique_features": 24,
          "paint": {
            "circle-color": "#d86cff",
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 2, 11, 5],
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": 0.7,
            "circle-opacity": 0.9
          }
        }
      ]
    }
  ]
}
```

## Operation

Use the workspace geospatial Python environment:

```sh
/Users/matthewlew/miniconda3/bin/python \
  pipelines/build_arizona_state_survey_pmtiles.py --audit
```

The default action with no mode flag is also a private audit. Publication is
deliberately explicit and should use a coordination window when another
builder may stamp the shared manifest:

```sh
/Users/matthewlew/miniconda3/bin/python \
  pipelines/build_arizona_state_survey_pmtiles.py \
  --publish --manifest-grace-seconds 30
```

The publisher re-reads the latest public manifest, prepares a same-mode
replacement, detects concurrent changes before and after archive installation,
and rolls back all three archives on any `BaseException`, including an
interrupt. Existing unrelated manifest keys are preserved. Published offline
validation is available through `--check`.

Focused tests:

```sh
python3 -m unittest tests.test_arizona_state_survey_pmtiles -v
/Users/matthewlew/miniconda3/bin/python -m unittest discover \
  -s tests -p 'test_arizona_state_survey_pmtiles.py' -v
```

The focused suite has 16 tests. It includes a real Tippecanoe regression that
builds the same fixture beneath two different temporary roots and requires
identical complete archive bytes, identical SHA-256, identical metadata hash,
and no staging path in raw PMTiles metadata.
