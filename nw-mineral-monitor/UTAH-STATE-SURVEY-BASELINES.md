# Utah official state-survey baselines

Status: `published_baseline_not_release` on 2026-08-13. The exact four PMTiles
archives are installed below `site/data/tiles/states/ut/` and advertised by the
public manifest. They do not mark Utah DONE or enable its release. No statewide
GeoJSON is installed below `site/`; all build inputs and private scratch are
deleted when the build exits.

Builder: `pipelines/build_utah_state_survey_pmtiles.py`

Tests: `tests/test_utah_state_survey_pmtiles.py`

Atomic group: `ut_ugs_state_survey_baselines_v1`

## Official source identities

The builder checks the live Utah Geological Survey catalogs before and after
each build, then pins each bulk archive byte-for-byte and inventories every ZIP
member.

| Source | Official bulk URL | Bytes | SHA-256 | Members | Member-inventory SHA-256 | Selected source layer(s) |
|---|---|---:|---|---:|---|---|
| UGS Map 179DM, *Digital Geologic Map of Utah*, 1:500,000 | `https://ugspub.nr.utah.gov/publications/GIS_maps/GeologicMapOfUtah.zip` | 27,317,100 | `df02e3692fbf5c2cc64fa143c364cd9e3f10472f97bb94277af07db0fe281484` | 17 | `07eb6b0e039296a24b2b6fbfd899701ff91e9be652b51b0118eea2aeeb0163b8` | `Geology_poly`, `Geology_arc` |
| UGS Data Series 7, *2025 Update to the Utah Quaternary Fault Database* (Hiscock, 2026) | `https://ugspub.nr.utah.gov/publications/data_series/ds-7/ds-7.zip` | 4,478,185 | `7b64620d0f6411891daa172e34fe994bbd5d1531a83e6a48393eea601fec905d` | 79 | `e30a8417c85d80cef93f6851b9ababb8bfb1f93c7176e77b5800e862e3c04459` | `UQFD25_DS7_full` |
| UGS Open-File Report 695, Utah mining districts, 1:1,000,000 | `https://geology.utah.gov/apps/blm_mineral/appfiles/Mining_Districts_20190116gdb.zip` | 36,391,387 | `7e298b2f9dfc130120c1cc7f2db1f894b1c1341d2c67e47cfd4165c7bfe244e6` | 58 | `4717264ac1472dbda82343a19ea39a3d37870a2b691f69a3c7db89081e2cde65` | `mining_districts` |
| UGS Open-File Report 757, Utah Mineral Occurrence System (Rupke, 2023) | `https://ugspub.nr.utah.gov/publications/open_file_reports/ofr-757/ofr-757.zip` | 5,880,910 | `2da50d3ebd41c914d5472111030e57e4f6b9812e78c195e251a2a44fbc9f64c7` | 51 | `226dc58be71895038af5dd6f89011402060617a1026b3509dcb91c3f3e2bf423` | `UMOS_2023_08_25` |

DS-7 is the current UGS replacement for older Utah Quaternary-fault copies.
Its SSZ polygon and “new” subset layers are companion/subset products, not
additional full fault-trace sources, so they are recorded but not duplicated
as separate baseline layers. Historic UMOS `OWNER`, `OPERATOR`, and
`LAND_STATUS` fields are deliberately not republished as current title facts.

## Typed source and spatial accounting

The authoritative Utah clip is the pinned 2025 TIGERweb geometry in
`infra/state_clips.json`, with registered bounds
`[-114.05287, 36.99766, -109.04157, 42.0017]`. Geometry is validated in its
native CRS, transformed to EPSG:4326, intersected with that polygon, and then
dimension-filtered without substituting geometry.

| Normalized source | Source records | Native CRS/type | Empty | Unusable | Fully outside | Clipped | Below encoding | Tiled records |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| Map 179 structures | 68,126 | EPSG:26712 `LineString` | 0 | 2 | 552 | 520 | 1 | 67,571 |
| Map 179 geology | 22,637 | EPSG:26712 `Polygon` | 0 | 0 | 2 | 462 | 0 | 22,635 |
| DS-7 faults | 19,743 | EPSG:26912 `3D MultiLineString` | 0 | 0 | 502 | 35 | 9 | 19,232 |
| OFR-695 districts | 185 | EPSG:26712 `MultiPolygon` | 0 | 0 | 0 | 12 | 0 | 185 |
| OFR-757 UMOS | 7,793 | EPSG:26912 `Point` | 1 | 0 | 5 | 0 | 0 | 7,787 |

All five source schemas, native bounds, feature counts, original feature-ID
sequences, and shifted top-level ID sequences are hash-pinned in the builder.
No polygon required topology repair. The two unusable Map 179 records are
zero-length duplicate-point lines and are fingerprinted individually. Every
empty, outside, clipped, repair, and unusable ID list has an exact SHA-256.

DS-7 dimensional evidence is exhaustive, not sampled: all 19,743 features are
3D and contain 245,248 coordinates. Of those Z values, 245,245 are zero and
three finite elevations occur on source IDs 13854, 19275, and 19558. The three
records and their source geometries are hash-pinned. MVT is two-dimensional,
so the builder inventories every Z and then removes only the Z dimension before
CRS transformation; horizontal geometry remains unchanged.

The z12/full-detail-14 Web Mercator encoding unit is 0.5971642834779395 m.
Complete maxzoom top-level-ID scans found ten valid two-vertex lines below that
resolution:

- Map 179 structure ID 22207: 0.0817746493674161 m after transformation.
- DS-7 IDs 813, 885, 901, 1615, 2834, 2932, 2967, 5784, and 16553:
  0.014673295653765366–0.4520954398908576 m after clipping/transformation.

ID 16553 is a 10.10 m source trace clipped to a 0.1437 m state-edge remnant;
the other nine are native micro-segments. Each exclusion records the source
record ID, scale where available, native length, transformed length, source and
output geometry SHA-256, z12/full14 unit, and review reason. Nothing is dropped
to satisfy tile or feature-size budgets.

## Reproducible normalized sequences

Two full source reads produced identical sequences. The hashes are now
publication-pinned:

| Sequence | Bytes | SHA-256 |
|---|---:|---|
| Map 179 structures | 64,028,641 | `92100bcb0972db8e077f1ee8ede144c91ec685f826c424aeb9f9bc21d05600e3` |
| Map 179 geology | 51,114,310 | `ba81993897a0670aa6c73e4eb439dad36cc98064f54f2d00c818dd6d9c558a95` |
| DS-7 faults | 32,488,559 | `a94c6460bdda343ab4709dbd4d8be50a81be7abde7f39288c61337f08f3ab413` |
| OFR-695 districts | 659,729 | `63e824d76c593ebd124616c3fb735f3508b096220f84d7715748afbc12fae59b` |
| OFR-757 UMOS | 15,790,119 | `de474e7db4e2829b976ee2f40f4c59a7eacda5b870bccbc08c7314e3336d7cba` |

## Published PMTiles fingerprints

Tippecanoe v2.79.0 builds z0–12 with full detail 14, drop rate 1,
`--no-feature-limit`, `--no-tile-size-limit`, and no tiny-polygon reduction at
maximum zoom. Two unrelated tile directories produced byte-identical archives.
Every encoded feature and required property was scanned at every zoom; unique
top-level IDs were compared with the exact expected source IDs at z12.

| Atomic key / site-relative file | Features | Bytes | SHA-256 | Bounds | All-zoom MVT instances | Maxzoom unique IDs |
|---|---:|---:|---|---|---:|---:|
| `ut_ugs_map179dm_500k` / `data/tiles/states/ut/ugs-map179dm-500k.pmtiles` | 90,206 | 59,081,057 | `5651c2118160db14a902117dd6baaf06ac0c342a4264cc1af35613e8586a029b` | `[-114.05287,36.997802,-109.042008,42.000886]` | geology 272,745; structures 849,519 | geology 22,635; structures 67,571 |
| `ut_ugs_ds7_quaternary_faults` / `data/tiles/states/ut/ugs-ds7-quaternary-faults.pmtiles` | 19,232 | 8,245,068 | `ef10b8946f3b6134867abab068e2a61fb58c83ac1a7762347a77d44cc3c0f4d2` | `[-114.049163,37.000342,-109.042432,42.00148]` | 200,541 | 19,232 |
| `ut_ugs_ofr695_mining_districts` / `data/tiles/states/ut/ugs-ofr695-mining-districts.pmtiles` | 185 | 1,279,628 | `f67f043a89402b51e28a7e2cce54b92ab74fb6f06977726a40be562755836d3a` | `[-114.050993,36.99772,-109.04157,41.9999589]` | 4,076 | 185 |
| `ut_ugs_ofr757_umos` / `data/tiles/states/ut/ugs-ofr757-umos.pmtiles` | 7,787 | 16,451,489 | `f1984b03f975a4e8241b88df9e52133efc7c95a9111797c92a9c0aee06b292e2` | `[-114.048868,36.998889,-109.043777,41.991022]` | 112,248 | 7,787 |

Generator metadata uses output/input basenames only. The builder rejects
private staging paths in the full PMTiles metadata, and two path-independent
builds also prove that no temporary path affects the bytes.

## Browser and manifest contracts

Each published `browser_descriptor` is schema version 1 and includes the exact
manifest key, file and `pmtiles://` URL, state `UT`, bounds, min/max zoom,
activation zoom, `lazy: true`, and `default_visible: false`. Every descriptor,
child layer, and MapLibre style carries this explicit filter:

```json
["==", ["get", "st"], "UT"]
```

Child layers bind an exact PMTiles source layer, geometry/style type, bounds,
feature count, activation zoom, semantic warning, and required property list.
Activation begins at zoom 5 for geology, faults, and districts; Map 179
structures and UMOS begin at zoom 6.

`validate_manifest_baselines(manifest, *, pmtiles_header=None)` enforces the
four-key atomic group and accepts the shared strict PMTiles scanner as an
injected `pmtiles_header`. Each manifest entry contains:

- `schema_version`, `status: baseline_not_release`, `state`, `format`, `file`;
- official source/download/member identity, retrieval date, feature counts;
- per-layer normalized source, clip, dimensional, encoding, and ID evidence;
- exact required properties and source-layer declarations;
- atomic-group declaration, provenance warning, bytes/hash/bounds/zoom;
- typed fields, all-zoom semantic counts, path-free metadata evidence;
- byte-identical rebuild evidence and the validated browser descriptor.

The publication routine requires the exact four-key set, pinned sequences, a
second byte-identical build, and an explicit minimum 30-second grace. It stages
all archives and the manifest together, preserves unrelated manifest data by
hash, and catches `BaseException` to restore every prior file and manifest.

### Offline accepted-generation guard

A pre-publication adversarial review found that the first offline validator
proved archive/manifest self-consistency but did not yet bind that pair back to
this reviewed official-source generation. A real replacement fixture containing
the five canonical source-layer names across four valid z0–12 PMTiles files,
but only six unique features and 96,309 total bytes, could declare matching
counts, hashes, fields, browser descriptors, and maximum-zoom IDs and pass that
older validator while omitting the official-source evidence. Nothing was
published while that gap existed.

The validator and publication transaction now reject that fixture and freeze
all of the following independently of manifest-supplied replacement values:

- the exact four artifact byte sizes, SHA-256 values, bounds, all-zoom semantic
  counts, and maximum-zoom unique-ID counts in the table above;
- all four official ZIP byte/hash/member-inventory identities and the exact UGS
  catalog/publication selections, including the DS-7 companion-layer decision
  and the UMOS historic-property exclusion;
- each typed source-layer manifest and source-FID hash, source/tiled geometry
  counts, empty/unusable/repair/Z-dimensional evidence, exact encoding
  exclusions, authoritative TIGERweb clip identity, outside/clipped ID hashes,
  and normalized-sequence byte/hash pair;
- the decoded PMTiles source layers, required properties, exact Utah state
  values, field metadata, full-MVT semantic counts, maximum-zoom IDs, lazy
  browser descriptor, and two-byte-identical-build evidence.

Hashed evidence lists are recomputed offline rather than merely trusting their
declared hashes. `_publish()` repeats the strict PMTiles scans and accepted-
generation checks against the pending private files before moving any public
path, so calling the transaction helper directly cannot bypass the guard.

## Verification and remaining gate

```text
/Users/matthewlew/miniconda3/bin/python \
  tests/test_utah_state_survey_pmtiles.py -v
# 18 tests, all green; includes the real six-feature miniature exploit

/Users/matthewlew/miniconda3/bin/python \
  pipelines/build_utah_state_survey_pmtiles.py
# private double build; all four report two_byte_identical_builds

/Users/matthewlew/miniconda3/bin/python \
  pipelines/build_utah_state_survey_pmtiles.py --check
# Utah state-survey PMTiles manifest validation passed
```

Peak Utah scratch was 929 MiB and returned to 0 B after the audit. After shared
validator/browser integration passed, the production builder repeated the full
official-source audit and two byte-identical builds, observed an explicit
30-second grace, and atomically installed only the four accepted archives and
four manifest entries. The post-stamp public `--check` passed, and the canonical
manifest projection excluding those keys was unchanged. Other Utah DONE-gate
workstreams remain independently required; these official state-survey
baselines alone do not make the state shippable.
