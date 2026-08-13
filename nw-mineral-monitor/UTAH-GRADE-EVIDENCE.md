# Utah WS11 grade evidence

Utah has a private, national-compiler-compatible evidence package with 26
distinct graded mine/property targets, 26 verbatim quotations, 26 numbered
page citations, and three independently authored official primary
publications. It also has the complete Utah PP 610 anchor: all 13 numbered
districts in Figure 25. This is evidence only. It does not mark Utah DONE,
enable a release, edit `states/UT.yaml`, update coverage or a public manifest,
or publish a browser layer.

## Reviewed source corpus

`pipelines/config/ut_grade_sources.json` permits only official
`pubs.usgs.gov` HTTPS URLs and checksum-pins four USGS documents:

- Boutwell (1905), *Economic Geology of the Bingham Mining District, Utah*,
  USGS Professional Paper 38: 17,543,503 bytes, 427 PDF pages, SHA-256
  `494caf06f1ff193c09c57f0d1bc64fbbd3325818c2355fa97d8a14f5fccf678a`;
- Lindgren and Loughlin (1919), *Geology and Ore Deposits of the Tintic Mining
  District, Utah*, USGS Professional Paper 107: 59,069,575 bytes, 323 PDF
  pages, SHA-256
  `536882df1f3ef475aa972d85b20e4a57e98666d3e7cee347e1d395b4c05764d5`;
- Nolan (1935), *The Gold Hill Mining District, Utah*, USGS Professional Paper
  177: 10,856,952 bytes, 193 PDF pages, SHA-256
  `958dcf7c2e2927a845fbec56682ae9ed3273efc93eafd1c8cca50d9a372eb7da`;
  and
- Koschmann and Bergendahl (1968), *Principal Gold-Producing Districts of the
  United States*, USGS Professional Paper 610: 39,132,819 bytes, 290 PDF
  pages, SHA-256
  `f4c1f048aaffe1e8d1431983e0a7b3f1bb543fab0f5380cd42e85c0a6a840896`.

The reviewed grade rows are in
`grades-research/ut/reviewed_grade_evidence.json`: 17 Bingham targets from PP
38, six Tintic targets from PP 107, and three Gold Hill targets from PP 177.
Every target has one mine-specific quotation, an exact PDF page and printed
page citation, district, county, years/basis, a reviewed 300-dpi page-image
hash, and one or more native Au, Ag, Cu, Pb, or Zn measurements. Ranges use
their stated lower endpoint and retain the full range in the quotation.
Historic dollar-per-ton values are never converted. District averages,
grouped-claim rows, and dollar-only rows are excluded.

The page review corrected several unsafe attributions in the earlier broad
Nevada/Utah triage material. Victoria, Colorado, and Eureka Hill are bound to
printed page 174 (PDF page 203) of PP 107. The PP 177 rows on printed pages 136
and 149 belong to Rube and New Baltimore, not Alvarado and Shay. The Highland
Boy scan reads `3 per cent silver`, not `$3 silver`; only its unambiguous 12
percent copper measurement is consumed. The Winamuck quotation deliberately
retains the source's printed typo `ran abut 38 per cent lead` instead of
silently modernizing it.

The PP 610 inventory is
`grades-research/ut/pp610_district_inventory.json`. It transcribes the complete
ordered legend of Figure 25, *Gold-mining districts of Utah*, on printed page
241 (PDF page 247): San Francisco, Stateline, Tintic, Gold Mountain, Mount
Baldy, Cottonwood, Bingham, Park City, Camp Floyd, Ophir-Rush Valley, Clifton,
Willow Springs, and American Fork. The exact 300-dpi figure-page image has
SHA-256
`4872182f35a98df033ebd31c70a26e25a14c4a672f8b5059751500fb433dceb6`.
No modern or familiar district is substituted for a numbered PP 610 entry.

## Page-level authority and output

All three grade documents are historic scans. For each of the 23 distinct
cited pages, the producer renders a PNG with `pdftoppm -r 300 -png` and
requires its SHA-256 to equal the reviewed row. Tesseract `--psm 6` is only a
diagnostic quotation cross-check; it does not decide punctuation, target
identity, or numerical values. Each grade source gets a canonical page index
whose hash is embedded in the source identity accepted by
`build_national_grade_evidence.py`.

PP 610 uses its official embedded text layer for word-coverage checks and a
separate 300-dpi Figure 25 page hash as the completeness review boundary. The
producer passes both the grade and PP 610 documents through the existing
national validators before writing `build.json`.

The default private output is:

```text
build-inputs/ws9/ut-grade-evidence/
  build.json
  grades/ut.json
  pp610/ut.json
  page-indexes/<source-id>.<page-index-sha256>.json
```

With Python 3.14.6, Poppler 25.04.0, and Tesseract 5.5.0, the reviewed build
reports 26 mines, three grade sources, 26 verbatim quotations, 26 numbered
page cites, 23 scan-image review pages, one PP 610 figure-image review page,
and 13 PP 610 districts. Its current artifact hashes are:

- `build.json`:
  `fc2c52ca755c3cd1a342173e7bbc3071fc2cb9e671061bc977e0191f3056c7ee`;
- `grades/ut.json`:
  `c88cb6c633315a00174f4e53787ec4c6b1457382c24f77dac3f06007ac546e5e`;
- `pp610/ut.json`:
  `b9cef0f39af765e9a0e99585621b21a410688509e0f34d3735aabdd2552fd37a`;
- PP 38 page index:
  `0e36cc23ac9daf4b6d3fd027a0f48347ebb43616a47346db46fe34c06601c359`;
- PP 107 page index:
  `44e3db8415ef136e8e14a6c0484302df5418bb6479e5d5402125e63305164782`;
- PP 177 page index:
  `93032d5d6e15dc41063fbdb5284ebca13bf511722cc63d36050f12855fd0d6a4`;
  and
- PP 610 page index:
  `c9234ae3f27facf20f0da927b5865e713a1b18d431d1913c57f7cadcdd469266`.

## Operator workflow

Validate the reviewed manifests without the PDF cache:

```bash
python3 pipelines/build_utah_grade_evidence.py check
```

Fetch or verify the four official checksum-pinned PDFs under the ignored
`pipelines/cache/ut-grade-sources/` directory, then build the private inputs:

```bash
python3 pipelines/build_utah_grade_evidence.py fetch --all
python3 pipelines/build_utah_grade_evidence.py build
```

Run the focused adversarial and full-cache reproducibility suite:

```bash
python3 -m unittest tests.test_utah_grade_evidence -v
```

The full-cache test makes two independent builds and compares every output
file by path, byte count, and SHA-256. It skips only when the ignored official
PDF cache is absent; manifest, identity, host, path-containment, source
diversity, page-binding, PP 610 completeness, checksum-drift, national
contract, and no-release tests remain available.

## Remaining state gates

`build.json` declares `effect: evidence_only_no_release_or_done_mutation` and
`is_release_decision: false`. Meeting the isolated WS9 grade threshold and PP
610 anchor does not complete Utah's state gate. Claims/open-ground,
geology/faults, aeromagnetic provenance, recorder coverage, top-five quad-map
inventory, acceptance CI, and national storage/heap evidence remain separate
workstreams. A future national composer must checksum-bind these private
descriptors before publication.

For bit-identical reproduction, freeze the Poppler and Tesseract versions in
`build.json`. A deliberate toolchain change should produce new page-index
hashes and receive page review rather than inheriting these identities.
