# National geology/fault source-to-tile reconciliation

The WS11 national geology and fault baselines are release-safe only when every
source record has the same stable `fid` in the PMTiles archive and at z12. A
full semantic scan enforces both identities; MVT instance counts are not used
as source counts because clipping can repeat a feature across tiles.

## Root cause and repair

The first national build emitted all 559,279 geology records and 500,743 fault
records to GeoJSONSeq, but its z12 tiles contained only 559,278 and 500,674
unique `fid` values. The omitted records were valid, exceptionally small source
geometries—not duplicate records and not invalid null geometry.

The repaired builder:

- requests ArcGIS geometry at eight decimal places without
  `maxAllowableOffset` and keeps eight decimals from Qfaults;
- uses Tippecanoe full detail 14 for polygons and the maximum useful detail 20
  for lines at z12;
- runs an unmodified lossless diagnostic first. Only a `fid` demonstrably absent
  from that full z12 scan can enter repair, and it still fails unless all
  distinct vertices in the affected part round to the same global 32-bit
  Web-Mercator coordinate. The chosen endpoint moves in the native segment
  direction to 0.05 coordinate unit beyond the nearest integer-cell boundary;
- records the source-geometry SHA-256, source length, source row/OID, engine,
  reason, changed-part count, and maximum displacement (hard-limited to 0.02 m)
  on every changed feature and repeats the exact sorted inventory plus its
  canonical SHA-256 in the manifest. The diagnostic missing-`fid` list and hash
  must equal the normalized-`fid` list exactly;
- builds each archive twice at the same path and requires byte-identical output;
- performs two full semantic scans and rejects any missing/extra `fid`, including
  a `fid` absent only at z12, before either public path can be replaced.
- announces a configurable grace period, then publishes both archives and the
  freshly merged manifest under a rollback transaction. A second-file failure,
  manifest failure, `KeyboardInterrupt`, or other `BaseException` restores all
  three prior files and their modes.

No feature is dropped, converted to a point, or assigned invented geology. The
minimum line normalization represents an already distinct, valid source line
at the vector-tile engine's finite coordinate resolution; its original geometry
identity remains reviewable through the recorded hash and metrics.

## Exact omission audit from the superseded build

Geology `fid 111585` was USGS SGMC v1.1 `OBJECTID 111585` (New Hampshire,
Partridge Formation). It is a valid 36.0195 m², three-vertex polygon about
14.4 × 16.5 m. Full detail 14 preserves it at z12.

The SGMC fault omissions below are `fid → OBJECTID (native length in metres)`:

```text
10859→12030 (1.509)       133198→173374 (1.092)
137073→177249 (1.010)     141435→181611 (1.180)
143961→184137 (1.506)     144114→184290 (1.020)
154246→194422 (0.187)     154392→194568 (0.624)
156225→196401 (0.094)     163684→203860 (1.290)
164859→205035 (1.011)     165109→205285 (0.312)
165605→205781 (1.182)     169349→209525 (1.623)
171073→211249 (0.093)
```

Fault `fid 304450` was Alaska SIM 3340 `OBJECTID 56718`, source `BM003`, a
valid 0.896 m concealed right-lateral fault trace.

The Qfault omissions below are `fid → zero-based shapefile row (native length
in metres)`. All are in `Qfaults_US_Database.shp`; none is an offshore-row or
Hawaii-filter bookkeeping error.

```text
392732→4991 (0.241)       393579→5838 (1.204)
393730→5989 (0.568)       413446→25705 (0.785)
413518→25777 (0.891)      431941→44200 (0.496)
432572→44831 (1.113)      434566→46825 (0.0103)
435465→47724 (0.790)      435564→47823 (0.456)
435906→48165 (1.067)      436189→48448 (0.609)
436237→48496 (1.150)      437073→49332 (0.490)
437074→49333 (0.0111)     437147→49406 (0.571)
437190→49449 (0.285)      437197→49456 (1.246)
437206→49465 (0.842)      437209→49468 (0.208)
437219→49478 (0.901)      438017→50276 (0.782)
439277→51536 (0.0740)     440364→52623 (0.0044)
440992→53251 (0.632)      441019→53278 (0.0444)
441029→53288 (0.0563)     442208→54467 (0.0025)
442984→55243 (0.0375)     443404→55663 (0.0044)
443707→55966 (0.326)      445897→58156 (1.519)
446190→58449 (1.434)      446256→58515 (1.141)
451067→63326 (0.107)      452323→64582 (0.0870)
459623→72780 (1.293)      461244→74401 (1.631)
478486→91643 (0.0172)     480310→93467 (0.433)
480343→93500 (0.322)      481772→94929 (0.227)
481773→94930 (0.0666)     483000→96157 (0.0042)
485233→98390 (0.465)      485472→98629 (0.423)
487504→100661 (0.580)     487596→100753 (1.782)
489748→102905 (0.106)     489784→102941 (0.149)
489866→103023 (0.643)     491390→104547 (0.361)
498428→111586 (1.480)
```

The prior six-decimal normalization made eleven of these Qfault traces
coordinate-identical. Eight-decimal preservation removes that upstream loss.
The repaired builder does not alter every sub-centimetre trace. A trace already
encoded by the d20 diagnostic is left byte-for-byte as sourced, even if its
endpoints are close. Only the exact diagnostic omissions that also collapse
under Tippecanoe's 32-bit rounding may receive the audited minimum displacement.
