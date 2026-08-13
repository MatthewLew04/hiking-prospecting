# National administrative baseline publication

`pipelines/build_admin_pmtiles.py` is the offline publication boundary for the
49-state Census TIGERweb administrative baseline. It does not query TIGERweb.
An adapter must first freeze the exact January 1, 2025 states and counties
generation in private staging; raw GeoJSON must never be copied below `site/`.

The hardened generation was atomically published after the private capture,
two clean-room rebuilds, full semantic scan, and coordinated review:

| Identity | Accepted value |
|---|---:|
| PMTiles bytes | 7,743,967 |
| PMTiles SHA-256 | `94c3a78b2ca17f02223e6d5161afde763a370b515e710723e76395b520e2c3df` |
| State records / ID SHA-256 | 49 / `155b69af91d4816940212a1ab613d9afaf6dd3219eaa9bd1ef63037ba1bcaef4` |
| County records / ID SHA-256 | 3,138 / `a37a3c2581375c33746a4fe50ab907b9fdde986521113b9f508d4fb155b48da1` |
| Zoom / bounds | z0–10 / `[-179.23109,24.39631,179.85968,71.43979]` |
| State clips bytes / SHA-256 | 707,923 / `33c09d367d74a1ce0c88934d4adb548557733bf7da9105be039f5f16ed22c552` |

These values identify the current accepted public generation. The path-free
archive intentionally reproduces the existing clip bytes because the reviewed
NV/AZ/CO/UT state-survey descriptors bind that exact clip SHA-256.

The previous 7,895,670-byte PMTiles generation (SHA-256 `4aba83f4…`) had legacy
path-dependent metadata: `name` and
`description` expose the absolute public output path, while
`generator_options` exposes that path and the former private temporary input
paths. It was not grandfathered or stamped into the new descriptor. The
current descriptor's deterministic-rebuild evidence applies only to the two
path-free candidate builds the hardened builder actually compared.

The retained private candidate accepted for validator integration is:

| Identity | Published candidate value |
|---|---:|
| PMTiles bytes / SHA-256 | 7,743,967 / `94c3a78b2ca17f02223e6d5161afde763a370b515e710723e76395b520e2c3df` |
| State clips bytes / SHA-256 | 707,923 / `33c09d367d74a1ce0c88934d4adb548557733bf7da9105be039f5f16ed22c552` |
| Descriptor bytes / SHA-256 | 2,892 / `3a05d0700f4f9d42558c7af1043b54ba4ca22768faf3df32d82d5ac1fede4984` |
| Metadata / generator-options SHA-256 | `6f1b8d8c3f4a998cee96f1ccf8aef5fa293c964814ab11676a422d0cb5bdf5f2` / `d1d71bb0691ecc45e5b26876020b84504b57d292a8dffc46773fc55751b6caeb` |

The published descriptor also pins the official double-pass source snapshot, exact
counts and FIPS/property inventories, z0–10 bounds, and two byte-identical
clean-room archive builds. The retained private staging and candidates remain
available for audit through final global validation.

## Private staging generation

The builder accepts exactly three files outside `site/`:

```text
/private/admin-2025/
  inventory.json
  states.geojson
  counties.geojson
```

`inventory.json` is strict JSON:

```json
{
  "schema_version": 1,
  "system": "national_admin_tigerweb",
  "created": "2026-08-13",
  "source": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer",
  "vintage": "January 1 2025",
  "layers": {
    "states": {
      "file": "states.geojson",
      "bytes": 123,
      "sha256": "<64 lowercase hex>",
      "count": 49,
      "fips_ids_sha256": "155b69af91d4816940212a1ab613d9afaf6dd3219eaa9bd1ef63037ba1bcaef4",
      "properties_sha256": "9557f8d931cbb98a3d55a98dee359d52544aa9396e57ff510fcb9633f2fcb4b3"
    },
    "counties": {
      "file": "counties.geojson",
      "bytes": 456,
      "sha256": "<64 lowercase hex>",
      "count": 3138,
      "fips_ids_sha256": "a37a3c2581375c33746a4fe50ab907b9fdde986521113b9f508d4fb155b48da1",
      "properties_sha256": "8af5dcb479d0312f1cf012d909231e1b5d53e25557fa9798671368650c40aa64"
    }
  }
}
```

The explicit private-only capture-and-build route is:

```sh
python3 pipelines/build_admin_pmtiles.py \
  --capture-staging /private/admin-2025 \
  --private-output-dir /private/admin-candidate
```

`--capture-staging` cannot be combined with `--publish` or an operator-supplied
inventory. It validates exact layer metadata and Census vintage text, pins the
filtered object-ID and independent count observations, fetches seven or fewer
exact 500-ID pages twice, and repeats metadata/IDs/counts postflight before it
writes any staging generation. The target files must not already exist.

Each snapshot is also strict JSON with these exact top-level keys:

```json
{
  "schema_version": 1,
  "system": "national_admin_tigerweb",
  "vintage": "January 1 2025",
  "layer": "states",
  "source": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/0",
  "retrieved": "2026-08-13",
  "complete": true,
  "truncated": false,
  "pagination": {
    "method": "tigerweb-objectids-double-pass-v1",
    "source_count": 49,
    "fetched_count": 49,
    "object_id_field": "OBJECTID",
    "object_ids_sha256": "<64 lowercase hex>",
    "metadata_sha256": "<64 lowercase hex>",
    "records_sha256": "<64 lowercase hex>",
    "page_size": 500,
    "page_count": 1,
    "full_second_feature_pass": true,
    "postflight_metadata_match": true,
    "postflight_object_ids_match": true,
    "exceeded_transfer_limit": false,
    "source_snapshot_id": "<canonical layer/source/metadata/IDs/records SHA-256>"
  },
  "query": {
    "where": "<the exact builder-declared state-code filter>",
    "out_fields": ["OBJECTID", "GEOID", "STUSAB", "NAME"],
    "out_sr": 4326,
    "geometry_precision": 5,
    "max_allowable_offset": 0.002
  },
  "type": "FeatureCollection",
  "features": []
}
```

The counties snapshot uses layer 1, fields `OBJECTID, GEOID, STATE, NAME`, the
exact 49-state FIPS filter, count 3,138, and seven 500-ID pages. The adapter
pins the service's object-ID list and selected metadata, retrieves every exact
ID page twice, and repeats metadata and ID observations after retrieval. An
exceeded-transfer-limit response or any between-pass mutation fails before a
snapshot is written. Both snapshots must carry the same retrieval date.
Features are polygonal WGS84 geometry with exactly the published queried
properties (the producer-only `OBJECTID` is removed after reconciliation).
State FIPS, abbreviation, and name must match the frozen 49-state table. County
GEOID must be a five-digit FIPS whose first two digits equal its `STATE` value.
Wrong, missing, unexpected, and duplicate identities all fail. Inventory byte
hashes, file hashes, record counts, exact property hashes, and the canonical
sorted numeric FIPS-list hashes are checked independently.

## Deterministic build and semantic acceptance

For a non-publishing review build:

```sh
python3 pipelines/build_admin_pmtiles.py \
  --staging-dir /private/admin-2025 \
  --private-output-dir /private/admin-candidate
```

Tippecanoe 2.79 or newer is required. Inputs and output use fixed relative
basenames, one worker, a fixed locale/time zone, deterministic input ordering,
z10 base/max zoom, and no feature or tile-size dropping. The builder creates
the archive twice in independent temporary directories and requires identical
bytes and SHA-256. PMTiles name, description, attribution, generator identity,
and generator options must be path-free.

Both archives then receive a complete semantic scan of every unique tile
payload. Metadata field declarations alone are insufficient. Every encoded
feature must have exactly `fips`, `name`, and `st`; polygon geometry; a numeric
top-level ID equal to the zero-padded FIPS value; and values identical to the
private snapshot. The unique maximum-zoom IDs must exactly equal the 49 and
3,138 source inventories and reproduce both accepted ID-list hashes. The
archive must contain only `states` and `counties`, cover z0–10, and retain the
recorded bounds.

The private output contains only:

```text
admin.pmtiles
state_clips.json
admin-descriptor.json
```

The clips file is deterministic strict JSON derived from the same state
features used for tiling. Its top-level, state, and geometry-member order is an
explicitly pinned historical serialization contract. A fresh capture must
therefore reproduce both the parsed geometries and the existing 707,923-byte
`33c09d…` identity used by every reviewed NV/AZ/CO/UT state-survey clip
descriptor; merely sorting JSON keys is not a new spatial generation.

## Manifest descriptor and validator handoff

A candidate descriptor is shaped for `national_baselines.admin` and records:

- exact source authority, service, January 1, 2025 vintage, and layer numbers,
  plus the private inventory and per-layer snapshot fingerprints;
- retrieval date, `states`/`counties` layers, and exact required properties;
- 49/3,138 source counts;
- per-layer source records, maximum-zoom unique IDs and instances, and FIPS-list
  SHA-256, plus a canonical exact `fips`/`name`/`st` property-inventory
  SHA-256;
- `infra/state_clips.json` path, bytes, and SHA-256;
- PMTiles path, bytes, SHA-256, z0–10, and exact bounds;
- two-byte-identical-build evidence and path-free metadata hashes.

The release validator requires this exact independently pinned descriptor,
exactly identifies `sources.boundaries` as Census TIGERweb State_County,
January 1 2025, verifies artifact and clip fingerprints and path-free metadata,
and repeats the full per-feature/max-zoom FIPS/property reconciliation. It also
requires every advertised state-survey `spatial_clip` reference to bind the same
clip artifact and SHA-256. A malformed or missing descriptor leaves the
physical archive visible to the undeclared-PMTiles guard.

## Atomic public transaction

Public mutation requires the explicit `--publish` flag. After private QA, the
builder prints a 30-second grace warning. It then takes the publication lock,
rereads the latest manifest, and changes only `national_baselines.admin` plus
`sources.boundaries` (to the exact TIGERweb January 1 2025 source truth) while
installing these two matching files:

```text
site/data/tiles/context/admin.pmtiles
infra/state_clips.json
```

All unrelated manifest keys are preserved. The archive, clips, and latest
manifest are backed up before replacement.
Every failure, including `KeyboardInterrupt` and other `BaseException`
subclasses, restores all three previous identities and removes prepared files.
Input hashes are checked again before the grace window. No builder run should
be published while another workflow is writing the manifest.
