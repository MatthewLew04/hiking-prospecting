# South Dakota grade-leg evidence

This package supplies private, checksum-bound WS11 grade inputs for South
Dakota. It is evidence only: it does not enable South Dakota, mark a DONE gate,
or write registry, coverage, manifest, release, or public `site/` state.

## Result

- 26 unique graded mines or targets, each with a verbatim quotation and
  numbered page cite
- 2 independent official primary publications: 20 targets from USBM Bulletin
  427 and 6 targets from USGS Bulletin 1332-A
- 22 distinct grade pages bound to deterministic 300-dpi PNG SHA-256 values
- Au, Ag, and Cu native measurements
- all 7 South Dakota districts numbered in PP 610 Figure 23, plus a verbatim
  district-description locator for each
- national grade and PP 610 compiler validation green

The quantitative threshold is an observation, not a release decision.
The canonical `build.json` SHA-256 is
`c27d781b645fbaddb76694d561d38a8184b8d3d75dc85ec5141800d761114a10`.

## Pinned official sources

| Source | Role | Exact PDF SHA-256 |
| --- | --- | --- |
| P. T. Allsman, *Reconnaissance of Gold-Mining Districts in the Black Hills, South Dakota*, USBM Bulletin 427 (1940) | 20 historic grade targets | `337492be711a0b188f4aa82cc1d852fe3d37aeefd818f24387aad0cf6d4a4714` |
| R. W. Bayley, *A Preliminary Report on the Geology and Gold Deposits of the Rochford District, Black Hills, South Dakota*, USGS Bulletin 1332-A (1972) | 6 Rochford target/table rows | `34ad89b99c94f3cc2c550628757a752ff98091982430063c310bf30efa4f9a4c` |
| A. H. Koschmann and M. H. Bergendahl, *Principal Gold-Producing Districts of the United States*, USGS PP 610 (1968) | complete district anchor | `f4c1f048aaffe1e8d1431983e0a7b3f1bb543fab0f5380cd42e85c0a6a840896` |

The source inventory records official catalog/document URLs, exact byte counts,
page counts, and local cache confinement. Fetches fail closed on a changed
host, redirect, byte count, checksum, PDF signature, or page count. The UNT
federal scan fetch supports its bounded SHA-256 ALTCHA proof-of-work page.

## Review and conversion boundary

USBM Bulletin 427 is an image-only scan. Its OCR output is diagnostic; the
reviewed 300-dpi page image is authoritative. Bulletin 1332-A has embedded
text, but numeric table columns are imperfect, so its selected table rows use
the same page-image review boundary.

Seventeen Bulletin 427 records express a dollar value per short ton and state
the gold price in the same quotation. Those rows are converted to troy ounces
per short ton as:

```text
Au oz/short ton = quoted USD/short ton / quoted USD/troy ounce
```

Both operands, the method, and the exact result rounded to 10 decimal places
remain in the reviewed private input. The producer rejects arithmetic drift or
an operand absent from the quotation. Three other Bulletin 427 rows use native
reported Au/Ag grades.

Six Bulletin 1332-A rows are reported in parts per million. The reviewed input
retains the ppm values explicitly; Au and Ag use the exact identity 1 ppm = 1
gram per metric tonne, while Cu remains parts per million. Mutation tests reject
unit or value drift.

## PP 610 completeness

PP 610 Figure 23, printed page 233 / PDF page 239, is the completeness
boundary. Its reviewed page-image SHA-256 is
`3e86b782d004dd14166d649b7197e76bb7a14e864ddfbbf5359a6de526b3d52c`.
The exact numbered inventory is:

1. Deadwood-Two Bit
2. Lead
3. Garden
4. Bald Mountain
5. Squaw Creek
6. Hill City
7. Keystone

Removing, replacing, or reordering a district fails validation. Chapter pages
235–239 provide the seven verbatim district locator sentences.

## Reproduce

From the repository root:

```bash
python3 pipelines/build_south_dakota_grade_evidence.py check
python3 pipelines/build_south_dakota_grade_evidence.py fetch
python3 pipelines/build_south_dakota_grade_evidence.py build
python3 -m unittest -v tests.test_south_dakota_grade_evidence
```

Private compiler inputs are written below
`build-inputs/ws9/sd-grade-evidence/`. A full build emits:

- `grades/sd.json`
- `pp610/sd.json`
- content-addressed page indexes for all three publications
- `build.json`, including input and artifact hashes, tool versions, metrics,
  limitations, and an explicit evidence-only effect

## Limitations

- The evidence is concentrated in the documented Black Hills and
  Homestake/Rochford endowment; it is not an inventory of every occurrence in
  South Dakota.
- Historic statements can describe reported samples, shipment averages,
  production, or engineer estimates. `basis` and `years` preserve those
  distinctions rather than presenting every value as a modern resource grade.
- OCR quality does not establish the quotation. Human-reviewed page images and
  their hashes do.
- This package satisfies only the WS11 grade leg and PP 610 anchor. It makes no
  claim about South Dakota's other per-state DONE-gate items.
