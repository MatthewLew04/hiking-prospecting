# Alaska state claims and ARDF publication contract

This pipeline publishes Alaska's state-law mining claims and the Alaska
Resource Data File (ARDF) occurrence backbone. It does **not** publish or
substitute for federal Alaska MLRS claims, and running it never enables the AK
release or DONE gates.

## Authoritative private snapshots

`pipelines/fetch_ak_claims.py` and `pipelines/fetch_ardf.py` write raw JSON only
to private staging outside `site/`. Each ArcGIS layer capture:

1. pins reviewed layer metadata and the typed OID field;
2. obtains the complete `returnIdsOnly` inventory;
3. POSTs exact, sorted pages by those object IDs (never an offset cursor);
4. repeats every feature page and compares a canonical hash for every row;
5. repeats metadata and `returnIdsOnly` after the feature passes; and
6. atomically replaces a mode-0600 staging file only if all observations agree.

The reviewed 2026-08-13 source identity is:

| Source layer | Records | OID inventory SHA-256 | Record SHA-256 |
|---|---:|---|---|
| DNR active | 39,269 | `0485c20d51b54e3365ef09862a99bd5ffb84a264d5befaea13125b33cd44c544` | `bc4f98f71367ce33332e77e821030fcab4b13fa6319d91f1d82892a6d672f2d1` |
| DNR pending | 51 | `c331e85759e39947a7591a5b52bab252382637ab99ac844cee79867609389c68` | `3a50d733a536330dc3821ba97599f69d387cc1f7b2b24cb2fdd4fd70dc1829ec` |
| DNR closed | 79,480 | `f8de22d4beb0f032e67914544cec05c496357883405e57bb89ee38e4d2f4fa53` | `976e743caf50652927a78b9e5c4971a5dcbeac1e9b34dd8becd623ade0eb3081` |
| ARDF | 7,692 | `7c5cd4f66b8790294fecad6d9ab0dde10b647cdab9d98f1a5b4e2832a668bb8d` | `c7501d1e1174a1e5ee8b11bcff651930a11b4ddada73adb02c3157d0773c93bb` |

The complete private staging files are also pinned in
`pipelines/build_alaska_pmtiles.py`. Any metadata, inventory, row, date, or
file-hash drift stops the build for review instead of silently refreshing the
contract.

## Lossless tiled delivery

The builder emits three PMTiles archives and no browser-facing statewide JSON:

- `data/tiles/claims/ak-state.pmtiles`, z0-z13: 39,263 active, 51 pending,
  and 79,462 closed polygons;
- `data/tiles/claims/ak-state-precision.pmtiles`, z0-z19: six
  `active_precision` and eighteen `closed_precision` polygons; and
- `data/tiles/national/ardf.pmtiles`, z0-z13: 7,692 ARDF points.

Six reviewed official ARDF rows (OBJECTIDs `2828`, `3251`, `3367`, `3662`,
`5307`, and `6568`) have a blank `Site_type`. Optional source blanks are not
silently dropped and are never guessed: the normalized display value is the
literal `Not reported by source`, paired with a `source_blank` status property
(`g_status`, `typ_status`, or `district_status`). Reported values carry
`reported`. The manifest records blank-field counts and the exact six
blank-type OBJECTIDs. The private QA pass full-scans every required ARDF
property on every maximum-zoom feature, so Tippecanoe's omission of a null
attribute cannot escape as a metadata-only success.

The corrective ARDF manifest records this exact `source_quality` object:

```json
{
  "source_state_blanks": 1,
  "source_blank_fields": {
    "commodities_main": 75,
    "district": 400,
    "site_type": 6
  },
  "source_blank_site_type_objectids": [2828, 3251, 3367, 3662, 5307, 6568],
  "text_truncations": {
    "age": 216,
    "geo": 3985,
    "loc": 2120,
    "model": 1,
    "work": 1143
  }
}
```

Its exact required PMTiles property schema is `st`, `id`, `nm`, `g`,
`g_status`, `group`, `ex`, `typ`, `typ_status`, `status`, `district`, and
`district_status`. Each status is exactly `reported` or `source_blank`; a
`source_blank` value must equal the literal sentinel, while a `reported` value
must be nonempty and must not equal it.

Twenty-four official DNR polygons fall below the z13 MVT quantization grid.
They are routed by a reviewed `(status, source_oid, source geometry SHA-256)`
inventory into the precision archive. The builder does not widen, replace, or
convert them to points. It validates the source geometry before normal RFC
7946 ring-orientation conversion and requires all 24 top-level feature IDs at
z19. The base and precision source-OID and feature-ID sets must be disjoint,
and their union must equal exactly 39,269 active, 51 pending, and 79,480 closed
source rows.

Every archive is built with fixed relative input names, one Tippecanoe worker,
stable top-level IDs, and no feature or tile-size drop limit. A full decoded
maximum-zoom scan checks every source ID and required browser property.
Decompressed PMTiles metadata is separately checked for private path leakage
and fingerprinted.

## Private reproducibility run

Use two unrelated empty directories outside `site/`:

```sh
python3 pipelines/build_alaska_pmtiles.py \
  --claims-staging /private/path/claims.json \
  --ardf-staging /private/path/ardf.json \
  --no-manifest \
  --private-output-dir /private/path/build-a

python3 pipelines/build_alaska_pmtiles.py \
  --claims-staging /private/path/claims.json \
  --ardf-staging /private/path/ardf.json \
  --no-manifest \
  --private-output-dir /private/path/build-b
```

`--no-manifest` and `--private-output-dir` are inseparable. The private output
directory must be outside `site/`, may not be a symlink, and cannot update the
manifest. Compare the three artifact SHA-256 values, `artifact_set_sha256`,
the three path-independent metadata fingerprints, and
`path_independent_metadata_set_sha256`. Any difference blocks publication.

The initial split candidate produced two identical reproducibility sets and
was published on 2026-08-13:

| Archive | Bytes | SHA-256 |
|---|---:|---|
| Base claims | 125,013,988 | `7614d92b12a34cabe9bfd9db838b2f68d28a86e95791cc91db443e32d4a35854` |
| Precision claims | 260,487 | `21d7cbc47030e938836ab420608c1296a075fa8200b1f7495b706d348d0f073c` |
| ARDF | 18,698,221 | `ee0f6ac7373b60796e0cf8f571846534fbac44bd8099c913a6ca0172411f1f66` |

The artifact-set fingerprint is
`ca5c74bd7c19960a73d0d0add33d726200a9b546eade7bec2a65f661209c5116`;
the path-independent metadata-set fingerprint is
`1f966e7e0bcd416d9bfe6f617a4a6c2f62150879ffaf1cfcbbef64114710490e`.

The first exhaustive post-publication progress run correctly kept that
generation red: six source-blank `Site_type` values had become absent MVT
properties. The source and feature-ID inventories remained exact, but exact
IDs are not enough when a required browser property is missing. This exposed
the metadata-only gap and triggered the fail-closed normalization above.

The corrective generation was then rebuilt in two new private roots. All three
archives are byte-identical across the two runs; the claims archives remain
byte-identical to the initial split generation. The only changed artifact is
ARDF:

| Archive | Bytes | SHA-256 |
|---|---:|---|
| Base claims | 125,013,988 | `7614d92b12a34cabe9bfd9db838b2f68d28a86e95791cc91db443e32d4a35854` |
| Precision claims | 260,487 | `21d7cbc47030e938836ab420608c1296a075fa8200b1f7495b706d348d0f073c` |
| Corrected ARDF | 19,207,022 | `2e576908351dcd344b6503e84d578dd20b6cf92fb00e9ed32a123f022bcb159a` |

The corrective artifact-set fingerprint is
`79f9ad3d74744a9ff3ffeac54da6b32cdb7cd346ed90b87b48575639c14158ad`;
the path-independent metadata-set fingerprint is
`f4dd0c4cd8208d441b901e712eacc376decf931d822f4c3b98e85127f0975207`.
Both private ARDF scans found 7,692 unique IDs and 8,284 maximum-zoom feature
instances, with exact values for all twelve required browser fields. The
corrective generation was explicitly cleared and published transactionally on
2026-08-13 from the checksum-pinned staging inputs; no private candidate was
promoted. Independent public checks reproduced all three byte counts and
SHA-256 values above, the manifest's exact counts and `source_quality`, and the
exhaustive claims/ARDF semantic scans. The public manifest SHA-256 immediately
after that corrective stamp was
`0312a8ed80bd07509975eb0a17a4e0489f56fc65940be3528de3faff9c2fce0e`;
its canonical projection with only the two Alaska entries removed remained
unchanged at
`047e46b793dc7b5bac6c782b05f58b01dba94ba6a5f39ae549a9482de660c53b`.

## Browser and validator integration

The ordinary source layers remain `active`, `pending`, and `closed`. Their
manifest entry records archive `minzoom: 0`, `maxzoom: 13`, and explicit
browser `activation_zoom: 8`. The
precision archive must be added as a separate PMTiles source with polygon
layers `active_precision` and `closed_precision`; it must never be labeled or
styled as a centroid layer. Use z19 as its activation/minimum display zoom so
the whole 24-feature precision inventory is present. Ordinary Alaska polygon
layers must consume the main entry's z8 activation: no-drop overview tiles
below z8 are intentionally complete but much larger, while z8 and higher stay
near the prior interactive payload envelope. Hardcoding a separate UI value
instead of reading the descriptor is a contract regression.

The validator must independently full-scan the base counts
`39,263/51/79,462`, the precision counts `6/18`, and ARDF `7,692`, then verify
the exact disjoint feature-ID and source-OID unions recorded in the manifest.
It must also validate the pinned 24 source geometry hashes. Counting only the
base archive, accepting a duplicate across archives, treating a precision
polygon as a point, or loading statewide GeoJSON is a release failure.

PMTiles remains range-requested and bulk origin storage remains zero. The
published generation's worst z13 5x4 base-claims window is 104,513 compressed bytes and
307,560 decoded bytes; the z19 precision window adds at most 3,701 compressed
and 4,278 decoded bytes. Shared browser acceptance passed at 89.1 MB heap and
0 MB bulk origin storage, including manifest-driven z8 base and z19 precision
source lifecycles, real polygon queries/search/popups, and teardown. That
baseline acceptance does not satisfy Alaska's per-state DONE gate: federal
MLRS, COG, and the other independent release evidence remain required.

## Atomic publication

With explicit reviewer clearance, omit the private flags:

```sh
python3 pipelines/build_alaska_pmtiles.py \
  --claims-staging /private/path/claims.json \
  --ardf-staging /private/path/ardf.json
```

After all private-equivalent validation completes, the builder prints an
explicit 30-second grace notice. It then replaces the base claims, precision
claims, ARDF, and the latest manifest as one rollback-capable transaction.
`BaseException`, including interruption during manifest stamping, restores all
prior archives and the prior manifest. The manifest merge preserves unrelated
keys. Publication still does not enable AK release/DONE: federal MLRS and every
other Alaska gate remain independently required.
