# Colorado state-survey baselines (published; not a state release)

## Status and scope

This three-archive `baseline_not_release` set passed its lossless audit and was
published atomically after production clearance and an explicit 30-second
pre-stamp grace period. Publishing this baseline did not mark Colorado DONE and
did not enable a release flag. The isolated builder is
`pipelines/build_colorado_state_survey_pmtiles.py`; its default mode builds and
validates in `build-inputs/.staging` and then removes its temporary directory.
Production publication uses a separate `--publish` operation, a 30-second
pre-stamp grace period, a latest-manifest merge restricted to the exact three
Colorado baseline keys, and BaseException rollback for all archives and the
manifest.

The private audit uses Tippecanoe v2.79.0, Shapely 2.0.3 / GEOS 3.11.3,
Fiona 1.9.5, and the exact 49-state TIGERweb January 1, 2025 boundary artifact
`infra/state_clips.json`. Every source is normalized twice and must be
byte-identical. Each PMTiles archive is then built in two independent private
directories from identical relative input names, scanned in full at every zoom,
checked at maxzoom for every unique top-level feature ID and required property,
and required to be byte-identical. No density or tile-size dropping is enabled.

## Source selection

### Tweto statewide geology and structures

The selected statewide vector is the exact `MapSourceID='map50'` subset of the
official USGS Cooperative National Geologic Map (CNGM) Earth Surface Feature
Service, ArcGIS item `8323586344b747c6b44731d399ec1307`. The CNGM DataSources
table binds `map50` to:

> Tweto, Ogden, 1979, Geologic map of Colorado: U.S. Geological Survey, scale
> 1:500,000.

The literal feature linkage is also pinned: every selected polygon and fault
has `DataSourceID='1035'`; DataSources row 1035 cites the same Tweto map and
links to NGMDB product 68589. Each MVT feature retains literal `MapSourceID`
and `DataSourceID` properties, their snake-case equivalents, both citations,
the map scale, source feature ID, and source map identity.

The current 2026 SGMC GeMS aggregate was not selected because the official
download is 1,818,900,910 bytes, covers a multi-state aggregate, and Colorado
is not identified among the revised per-state replacement packages. Fetching
the exact official CNGM `map50` rows avoids silently mixing maps while retaining
the complete 9,500-polygon and 10,238-fault Tweto source identity.

CGS MI-16 was not used as the vector transport because its current catalog
exposes a 179,398,577-byte PDF, not a downloadable GIS vector. The CNGM subset
is the official digital equivalent and preserves the map identity explicitly.

### CGS ON-006 fault overlays

CGS ON-006-15M is registered by the live CGS web map item
`04f86e4c09cc426eb5408a2e67f0aaa9` and official Fault_Server service. Its
`Quaternary Faults` and `Cenozoic faults` layers are ingested into separate MVT
source layers. The builder never promotes either age label into “active” and
never merges them into one ambiguous state-fault layer.

### CGS ON-007-08D districts

The official ZIP at
`https://coloradogeologicalsurvey.org/Docs/Pubs/ON-007-08D-v20201112.zip` is
202,712,855 bytes with SHA-256
`cd2234141333df794c48e8fb55096c12bfa6067fc9fc75b7c9fd5672ee77afe4`.
The full 14-member name/size/CRC/compressed-size inventory is pinned, as are the
six shapefile-component checksums. The shapefile contract is:

- driver: ESRI Shapefile;
- CRS: EPSG:26913;
- records: 383;
- native bounds: `[226446.34040592395, 4102227.912323936,
  676912.45885984, 4538592.5179551]`;
- declared geometry: Polygon (actual 382 Polygon, one MultiPolygon);
- fields: `Source str:150`, `District str:254`, `WebPage str:254`,
  `County_1 str:150`, `County_2 str:150`, `Note str:254`.

CGS says these 1:150,000 boundaries are estimated and subjective. Every browser
feature carries that warning; the layer is not mineral tenure or a delineated
resource boundary.

## Exact source and geometry inventories

| Source layer | Source/tiled rows | Normalized bytes | Normalized SHA-256 | Empty / fully outside / clipped / repaired |
|---|---:|---:|---|---:|
| `co_cngm_tweto_geology` | 9,500 / 9,500 | 22,698,188 | `846e4c65ccae2a92cc0c5ccf6dc406561623002a466bc17bb7c98aaab552c23b` | 0 / 0 / 158 / 17 |
| `co_cngm_tweto_faults` | 10,238 / 10,238 | 11,829,025 | `515fa0a2c73aac66af0c6a6f394bd399c5a82f361c8ce1fab3df4458465897c1` | 0 / 0 / 13 / 0 |
| `co_cgs_on006_quaternary_faults` | 864 / 864 | 1,002,827 | `da4a8eec6b69cb0a788e471d00d279a18e70028233f233c0ac1e455f5c25733b` | 0 / 0 / 2 / 0 |
| `co_cgs_on006_cenozoic_faults` | 2,698 / 2,698 | 2,886,033 | `2df3be6a984a34d6976749112b8a59a25f47ac1170d18deca159b745657f4777` | 0 / 0 / 4 / 0 |
| `co_cgs_on007_districts` | 383 / 383 | 6,224,627 | `902a182cf18ce029d2989ee2cf2e9e1e94c54a9d6b52e3846297eadce594691f` | 0 / 0 / 1 / 1 |

All 17 Tweto repairs are pinned Ring Self-intersections with Polygon → Polygon
→ Polygon transitions; the greatest absolute/relative area deltas are bounded
by 1e-12 in EPSG:4326 square degrees. The district repair is source row 3
(public feature ID 4), also Polygon → Polygon → Polygon, with an absolute area
delta of about 5.59e-9 square metres and a relative delta of about 5.42e-16.
Only district feature 225 (Stateline Diamond) changes under the authoritative
Colorado intersection. Exact ID-list hashes for every empty, outside, clipped,
and repair category are encoded in the builder contracts.

## Exact private archive fingerprints

These are the exact published products. Two independent private builds produced
byte-identical archives and path-free PMTiles metadata before the stamp.

| Manifest key / proposed file | Unique source features | Bytes | SHA-256 | Bounds | PMTiles metadata SHA-256 |
|---|---:|---:|---|---|---|
| `co_usgs_cngm_tweto_500k` / `data/tiles/states/co/usgs-cngm-tweto-500k.pmtiles` | 19,738 | 21,784,121 | `219538d916e59d09d6cbf6393939f5bdbc4d2b74d7ae61c7c8e85d77f5d6db0b` | `[-109.0602, 36.992455, -102.042093, 41.00344]` | `d1ca8ef347c3d50919860f60af251b37686548bc3d668c80c9549ed465a9dc67` |
| `co_cgs_on006_faults` / `data/tiles/states/co/cgs-on006-faults.pmtiles` | 3,562 | 2,702,319 | `b18612dcb1ce79b1bdc35d5bc7e48ba44e9ab3f14a859a4db0c27c5f85c14a1d` | `[-109.060042, 36.9992, -103.224372, 41.00307]` | `b87a901fbb2bfd3e3fb58fcc600f025a054ca026d7f65230668dc12bb791c70a` |
| `co_cgs_on007_districts` / `data/tiles/states/co/cgs-on007-districts.pmtiles` | 383 | 1,160,482 | `5b5ad631bb936baf76faa74067ec03a2460941b3d75e3d1d66181e1b8d18a98e` | `[-108.116396, 37.050137, -103.009432, 40.997144]` | `18df0850648aa56b27168c7bbae2842cb72b81c3a2d7f4c06d79b5c95687d15e` |

All archives are minzoom 0 / maxzoom 12. At z12, the distinct feature-ID
inventories are exactly 9,500 geology, 10,238 Tweto faults, 864 Quaternary
faults, 2,698 Cenozoic faults, and 383 districts. MVT instances are higher when
a source feature crosses tile boundaries; this is duplication by tiling, not a
source-record count.

The atomic stamp merged only
`co_usgs_cngm_tweto_500k`, `co_cgs_on006_faults`, and
`co_cgs_on007_districts` into the latest manifest. The pre-stamp manifest
SHA-256 was
`0e263755f0d409e2beda97b347018430c370e28bc97d4f5a3acb2ed1ff70ef49`;
the immediate post-stamp SHA-256 was
`f561f737bb9fa317b40ee912971a86a5e6a1af2c48d8cb7c7d47f1fb3e894e65`.
The canonical manifest projection with only those three keys removed stayed
`5dd6d3e6a0e6b3fbba3ac8401d630115868deede96e7ae9429141f84654b2bff`,
which proves unrelated manifest content was preserved during the merge.

## Proposed generic lazy-browser descriptors

The exact machine-readable descriptors are generated by
`_browser_descriptor()` for the three publication entries. Shared UI was not
edited. Every layer defaults off and loads its archive lazily at or above the
activation zoom.

| Manifest key | File | Source layer | Geometry | Count | Activation | Proposed style |
|---|---|---|---|---:|---:|---|
| `co_usgs_cngm_tweto_500k` | `data/tiles/states/co/usgs-cngm-tweto-500k.pmtiles` | `co_cngm_tweto_geology` | polygon | 9,500 | z4 | fill `#b69b72` at 0.22, outline `#6f604d` |
| `co_usgs_cngm_tweto_500k` | same | `co_cngm_tweto_faults` | line | 10,238 | z5 | line `#4a3b32` at 0.78, width 0.65→1.8 |
| `co_cgs_on006_faults` | `data/tiles/states/co/cgs-on006-faults.pmtiles` | `co_cgs_on006_quaternary_faults` | line | 864 | z5 | line `#d94841` at 0.88, width 0.8→2.2 |
| `co_cgs_on006_faults` | same | `co_cgs_on006_cenozoic_faults` | line | 2,698 | z6 | dashed line `#b86b28` at 0.72, width 0.7→1.8 |
| `co_cgs_on007_districts` | `data/tiles/states/co/cgs-on007-districts.pmtiles` | `co_cgs_on007_districts` | polygon | 383 | z5 | fill `#d19a37` at 0.14, outline `#8a5b16` |

Every descriptor also carries the exact archive bounds above, `lazy: true`,
`default_visible: false`, min/max zoom, a semantic warning, and these required
property contracts:

- all layers: `fid`, `st`, `source_dataset`, `source_id`, `source_scale`,
  `source_scale_status`, `source_ref`, `source_url`, `publication_id`;
- Tweto layers: literal and normalized `MapSourceID` / `DataSourceID`, plus
  `source_map_citation` and `data_source_citation`;
- ON-006 layers: `fault_age_scope`;
- districts: `district_name`, `boundary_status`.

## Reproduction and tests

Use the pinned Conda environment because the system Python does not provide the
required geospatial libraries:

```sh
/Users/matthewlew/miniconda3/bin/python -m unittest discover \
  -s tests -p 'test_colorado_state_survey_pmtiles.py' -v

# Private, ephemeral, two-source-pass + two-independent-PMTiles-build audit:
/Users/matthewlew/miniconda3/bin/python \
  pipelines/build_colorado_state_survey_pmtiles.py
```

The default private audit does not retain archives. The completed production
operation installed all three archives plus their manifest entries as one
rollback-protected atomic set after the documented grace period. It did not edit
`states/CO.yaml`, shared UI/reconcile/validator code, release flags, or DONE
state.
