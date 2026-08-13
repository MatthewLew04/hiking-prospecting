# Nevada state-survey baselines

Nevada is WS11's first P1 state, but these archives are **baselines, not a
state release**. They do not satisfy the claims/open-ground, expiration-watch,
recorder, grade, quad-map, aeromagnetic, or CI-scale gates. `states/NV.yaml`
therefore remains `release.enabled: false`, `release.status: building`, and no
DONE-gate status is changed to `pass`.

## Published products

`pipelines/build_nevada_state_survey_pmtiles.py` creates three PMTiles-only
products under `site/data/tiles/states/nv/`:

| Manifest key | PMTiles source layer(s) | Official source | Native scale |
|---|---|---|---|
| `national_baselines.nv_usgs_ds249` | `nv_ds249_geology`, `nv_ds249_faults` | USGS Data Series 249 v1.1, prepared in cooperation with NBMG | 1:250,000 |
| `national_baselines.nv_nbmg_onegeology_250k` | `nv_nbmg_onegeology_250k` | NBMG 2013 OneGeology ArcGIS layer 23 | 1:250,000 |
| `national_baselines.nv_nbmg_mining_districts` | `nv_nbmg_mining_districts` | NBMG Report 47z live ArcGIS layer 0 | 1:1,000,000 |

The DS 249 bulk GIS and NBMG OneGeology conversion are intentionally separate.
They have different publication identities and feature inventories, so the
pipeline never relabels one as the other.

Each encoded feature carries `st`, `source_dataset`, `source_id`,
`source_scale`, `source_scale_status`, `source_ref`, `source_url`, and
`publication_id`. Source attributes such as unit name, lithology, age, county
source, fault type, and district identity are retained where applicable.

## Rebuild

The DS 249 source is an ESRI shapefile in NAD27 / UTM zone 11N. Run the builder
with a Python environment that supplies Fiona coordinate transforms and with
Tippecanoe 2.79 or newer on `PATH`:

```sh
/Users/matthewlew/miniconda3/bin/python \
  pipelines/build_nevada_state_survey_pmtiles.py \
  --manifest-grace-seconds 30
```

The bounded grace window is for coordinating with any other national builder
that may stamp `site/data/manifest.json`. The publisher also compares the
manifest hash immediately before and after installing the archive set; a race
rolls the new archives back instead of silently losing another builder's data.

The pipeline performs these fail-closed checks:

1. Downloads the official 183,472,749-byte DS 249 ZIP and requires SHA-256
   `06da03ff35a08562baec56c6f889568dbbab562f11a18ca3226f2733bc44428e`.
2. Requires every shapefile component and EPSG:26711/expected geometry schema,
   then transforms to WGS84 inside a private
   `build-inputs/.staging/nwmm-nv-baselines-*` directory outside `site/`.
3. Requires the reviewed ArcGIS layer-metadata and complete object-ID snapshot
   hashes, requests exact ID chunks, and rejects source drift, missing,
   duplicate, extra, or reordered rows.
4. Validates source topology, applies a version-pinned `shapely.make_valid`
   operation only to the reviewed object-ID set, verifies geometry transitions,
   dropped part counts, and absolute/relative area-delta ceilings, and only then
   intersects service polygons with the checksum-recorded Census TIGERweb
   January 1, 2025 Nevada boundary from `infra/state_clips.json`. The manifest
   inventories repairs, changed/unchanged features, and adjoining-sheet features
   that fall wholly outside Nevada. Output coordinates must remain in a tight
   Nevada envelope.
5. Runs pinned Tippecanoe v2.79.0 with explicit full-detail 12 and no feature
   or tile-size dropping.
6. Fully scans every encoded MVT tile for structural validity and per-feature
   state/source/scale provenance, and reconciles unique maximum-zoom feature
   IDs to source counts before publication.
7. Computes artifact byte counts and SHA-256 values, then atomically merges the
   three distinct baseline keys into the latest manifest.

Temporary ZIPs, shapefiles, and `.geojsonseq` files never enter `site/`, even
under a hidden directory; publishing or transiently deploy-exposing statewide
JSON is a CI regression. Only the three validated `.pmtiles` files are moved
into `site/data/tiles/states/nv/`.

## Known source reconciliation

The checksum-pinned DS 249 geology shapefile contains 38,696 source records.
Two zero-area records (`28814` and `30918`) have null geometry, leaving 38,694
tileable polygons. The manifest records both exceptions verbatim. They are not
silently counted as browser features.

The legacy ESRI layer declaration also understates multipart geometry. The
reviewed geology inventory is 38,623 `Polygon` and 71 `MultiPolygon` records;
the fault inventory is 54,712 `LineString` and one `MultiLineString` record.
The pipeline hashes each per-type source-record ID list, preserves the actual
record type, and fails if that inventory changes. DS 249 geology has 187
reviewed ring self-intersections. Shapely 2.0.3 / GEOS 3.11.3 repairs them in
native EPSG:26711 before reprojection: 185 `Polygon→Polygon→Polygon` and two
`MultiPolygon→MultiPolygon→MultiPolygon`, with no non-polygon parts dropped.
The exact repaired-ID hash is
`8e29716492b8d72f241912b09e7aa7b80dfbc19b098a1db50368cb3c96fc3973`;
the observed maximum absolute area change is `0.00003337860107421875` square
meters (acceptance ceiling `0.001`) and maximum relative change is
`6.074700175754273e-15` (ceiling `1e-12`). All DS 249 faults validate without
repair.

DS 249 contributes 93,392 rendered baseline features: 38,694 geology polygons
and 54,698 fault traces. Its non-null source inventory is 93,407 records. The
15-record difference is fully reconciled, not silently dropped: the records
are valid two-vertex fault traces only `0.09375` to `1.9080421903092184` meters
long in native EPSG:26711 (`13.467420525851987` meters total), below z12 MVT
encoding resolution. The baseline records their exact FIDs and source-record
IDs and hashes, each original geometry hash and native length, source
scale/CRS, and the Tippecanoe v2.79.0 z12/full-detail-12 contract under reason
code `below_mvt_maxzoom_encoding_resolution`. The pipeline deliberately omits
them before tiling and proves `54,713 = 54,698 tiled + 15 exclusions`, with no
overlap, gap, extra ID, or fabricated/extended fault geometry. This exception
is baseline-only and does not satisfy Nevada's geology/faults DONE gate.

The live OneGeology conversion exposes the same two zero-area source slivers as
typed polygons with empty coordinate arrays (current service object IDs `28833`
and `31033`). Its 54,389-record object-ID snapshot also embeds adjoining-sheet
sources, including NBMG Map 150, so the number tileable as Nevada geometry is
established by the authoritative intersection—not by subtracting only those two
empty records. Every fully outside and boundary-clipped object ID is recorded
with a list hash and area totals.

The reviewed OneGeology snapshot contains 189 invalid geometries, all reported
as ring self-intersections. Under Shapely 2.0.3 / GEOS 3.11.3,
`shapely.make_valid` yields 187 `Polygon→Polygon→Polygon` and 2
`MultiPolygon→MultiPolygon→MultiPolygon` transitions, with no non-polygon parts
dropped. The maximum observed absolute area change is
`1.3766765505351941e-14` square degrees and the maximum relative change is
`2.3684682383909695e-14`; both acceptance ceilings are conservatively `1e-12`.
The exact 189-object ID list SHA-256 is
`f5dc88e70fa77e0fc9c8d713a27787f4b2da49289dcb978d95b7b07cfc9553a3`.
A changed ID set, validity reason, type transition, engine version, dropped
piece, or threshold violation fails the build before tiling. Report 47's 535
polygons require no topology repair in the reviewed snapshot.

The authoritative Nevada intersection excludes 2,370 adjoining-sheet
OneGeology records and clips 330 boundary-crossing records; their exact ID-list
hashes are recorded in the manifest. It leaves 52,017 tiled Nevada polygons.
For Report 47, one live polygon (object ID `535`) lies wholly outside Nevada and
15 cross the boundary, leaving 534 tiled polygons. Exclusion is always after
topology validation/repair, and the manifest records pre/post clip area totals.

The Report 47z catalog describes 534 mapped polygon units and 526 recognized
districts, while the current official ArcGIS layer returns 535 polygons. The
pipeline publishes the exact live object-ID snapshot and records this catalog
drift. Report 47 itself warns that its generalized polygons are not
ground-truthed boundaries; the layer is a literature/file index, not mineral
tenure.

## Offline acceptance check

After publication, validate the manifest/artifact pair without contacting any
remote source. The normal national progress validator invokes this same exact
three-archive check whenever any Nevada baseline key is advertised; the set is
atomic and remains independent of release status.

```sh
python3 pipelines/build_nevada_state_survey_pmtiles.py --check
/Users/matthewlew/miniconda3/bin/python \
  tests/test_nevada_state_survey_pmtiles.py -v
```

`--check` recomputes every artifact SHA-256 and byte count, checks source/null
inventories and the Report 47 count reconciliation, and repeats the complete
semantic PMTiles scan. A future source refresh must be rebuilt and reviewed; it
must not be “fixed” by editing manifest counts or hashes by hand.

## Official references

- [USGS Data Series 249](https://doi.org/10.3133/ds249)
- [NBMG statewide geologic maps](https://www.nbmg.unr.edu/Maps%26Data/StatewideGeologicMaps.html)
- [NBMG OneGeology ArcGIS service](https://gisweb.unr.edu/nbmg/rest/services/Geology/NBMG_Geology/MapServer)
- [NBMG Report 47z](https://pubs.nbmg.unr.edu/CDP-Mining-districts-NV-2nd-ed-p/r047z.htm)
- [NBMG mining-district files](https://collections.nbmg.unr.edu/)
