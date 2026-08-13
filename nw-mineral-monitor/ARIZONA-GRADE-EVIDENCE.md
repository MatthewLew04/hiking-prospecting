# Arizona WS9 grade evidence

The Arizona producer creates a private, national-compiler-compatible grade
input with 26 distinct graded mine/property targets from three independent
official primary publications. It also creates the complete Arizona PP 610
anchor: all 42 district entries in Figure 7 and the Arizona chapter on printed
pages 35-53. This is evidence only. It does not mark Arizona DONE, enable a
release, edit `states/AZ.yaml`, update coverage or a browser manifest, or
publish a layer.

## Reviewed source corpus

`pipelines/config/az_grade_sources.json` records the official catalog and
document URL, authority, citation, publication year, exact byte count, page
count, and PDF SHA-256 for four documents:

- Arizona Bureau of Mines Bulletin 137, *Arizona Lode Gold Mines and Gold
  Mining*, revised 1967: 16,459,054 bytes, 258 PDF pages, SHA-256
  `70d4717defa73dda580a4c291d791f43f245934f1a5b60c8e988f996da7c0a57`;
- U.S. Bureau of Mines Information Circular 6991, *Gold Mining and Milling in
  the Wickenburg Area, Maricopa and Yavapai Counties, Arizona* (1938):
  8,221,170 bytes, 93 PDF pages, SHA-256
  `aec5cbb938c56930e965163572e7bd01caba77095a5ce85db41ca951cd2e0632`;
- USGS Bulletin 782, *Ore Deposits of the Jerome and Bradshaw Mountains
  Quadrangles, Arizona* (1926): 22,710,341 bytes, 223 PDF pages, SHA-256
  `72262793e44bbdfeb01fc0f56329e1d4e47bc6d5f7d3743ed08fcac601aa5d02`;
  and
- Koschmann and Bergendahl, USGS Professional Paper 610, *Principal
  Gold-Producing Districts of the United States* (1968): 39,132,819 bytes,
  290 PDF pages, SHA-256
  `f4c1f048aaffe1e8d1431983e0a7b3f1bb543fab0f5380cd42e85c0a6a840896`.

The two Arizona publications are served by the Arizona Geological Survey's
official repository; the USGS documents are served by `pubs.usgs.gov`. The
producer permits only HTTPS URLs on the pinned AZGS and USGS hosts. A redirect
outside those hosts, checksum drift, changed page count, missing reviewed
source, traversal path, symlink, duplicate ID, or unreviewed schema field
fails closed.

The human-reviewed grade rows are in
`grades-research/az/reviewed_grade_evidence.json`. Every row retains the exact
source quotation, numbered printed-page citation, source PDF page, target,
district, county, years/basis, and one or more native Au, Ag, Cu, Pb, Zn, or Fe
measurements. Ranges are not averaged: the selected native measurement is the
stated lower endpoint and the verbatim quote retains the whole range. Historic
dollar-per-ton statements are not converted, so these rows do not depend on an
unfinished commodity-price table.

The PP 610 inventory is in
`grades-research/az/pp610_district_inventory.json`. It contains all 42 Figure
7 entries across Cochise, Gila, Greenlee, Maricopa, Mohave, Pima, Pinal, Santa
Cruz, Yavapai, and then-Yuma counties. `source_heading` preserves text-layer
artifacts such as `DOS CABE7.AS DISTRICT` and `PLOMOSA DISTRIC'I1`; the public
district names are separately normalized. The Alaskan mine grade row uses its
current county, La Paz, while Bulletin 137's 1967 text describes the location
under the pre-1983 Yuma County boundary.

## Page and quotation binding

All three grade documents are historic scans. For every cited PDF page the
producer renders a PNG with `pdftoppm -r 300 -png`, hashes the exact PNG bytes,
and requires that SHA-256 to equal the reviewed row's `page_image_sha256`.
Tesseract `--psm 6` is retained only as a diagnostic search cross-check; OCR
does not decide punctuation, fractions, or numerical values. This matters for
source glyphs such as `1½` and for tabular material in IC 6991.

The reviewed set deliberately excludes plausible rows whose scan was damaged
or whose quotation began on a preceding page. The 26 accepted quotations have
an OCR diagnostic word-match range of 0.889-1.000, but the 23 unique rendered
page hashes—not those OCR scores—are the review authority.

Each grade source gets a canonical derived page index containing its document
hash, cited-page text and image hashes, numbered page cites, diagnostic scores,
and explicit `page_image_sha256` review boundary. The page-index SHA-256 is
embedded in the source identity passed to the national compiler.

PP 610 uses the official embedded text layer. The producer finds each exact
reviewed district heading in ordered page bounding boxes, extracts the first
complete following sentence (or the source-page fragment at a page foot), and
checks its words against the independently extracted whole-page text. All 42
Arizona checks score 0.913-1.000. The complete output is then accepted by
`build_national_grade_evidence.validate_pp610_document`.

## Operator workflow

Poppler (`pdfinfo`, `pdftotext`, and `pdftoppm`) and Tesseract are required for
a full build. Validate the small checked-in review manifests without a PDF
cache:

```bash
python3 pipelines/build_arizona_grade_evidence.py check
```

Fetch or verify the four checksum-pinned official PDFs:

```bash
python3 pipelines/build_arizona_grade_evidence.py fetch
```

The PDFs remain under ignored `pipelines/cache/az-grade-sources/` and must not
be copied below `site/`. Build the private compiler inputs with:

```bash
python3 pipelines/build_arizona_grade_evidence.py build
```

The default output is:

```text
build-inputs/ws9/az-grade-evidence/
  build.json
  grades/az.json
  pp610/az.json
  page-indexes/<source-id>.<page-index-sha256>.json
```

The verified local build reports exactly 26 graded targets, 26 verbatim
quotes, 26 numbered page cites, three grade sources, 23 unique scan pages, and
42 PP 610 districts. With Python 3.14.6, Poppler 25.04.0, and Tesseract 5.5.0,
the current default artifacts are:

- `build.json`: SHA-256
  `91f32708109eb5432f4bd9b775ad3ddbcf575d18ab5e59889c5055e7299ad5cf`;
- `grades/az.json`: SHA-256
  `6a02e58433195b683b3b5c15211c6735f600c5f626a199898b87f13bd7de9782`;
- `pp610/az.json`: SHA-256
  `7b8639e5fd9b8818341f579c2f701c4d325f86570ccc01cb1524a40471d1e1a3`;
- B137 page index:
  `abf4ee3ba66e6e7b90f32044b9902609d88810508421b6742fab6785b21f2027`;
- IC 6991 page index:
  `f1bfbbeb2e58cd443a798fc76f02389ce07329c4b82a46a79ad7ca85984c32f6`;
- Bulletin 782 page index:
  `e23d8e1a443266cd4350364efbd342cac49695373c6ea8033596a405c3a55d61`;
  and
- PP 610 page index:
  `0151d7bb6ea6a23a80a3b67e311137f81180ae2f3575ef11d40b7bafe37dd091`.

Run the manifest, adversarial, and full-cache integration suite with:

```bash
python3 -m unittest tests.test_arizona_grade_evidence -v
```

The current run passes all 11 tests. The integration test skips when the
private PDF cache is absent; source identity, host restriction, cache
containment, source diversity, scan-page hash, range selection, PP 610
completeness, checksum drift, OCR noise, and page-fragment checks always run.

## National handoff and remaining gates

`build.json` declares `effect: evidence_only_no_release_or_done_mutation` and
contains descriptors ready for a future private Arizona entry in the national
grade inventory. This work intentionally makes no shared registry, validator,
manifest, coverage, release, UI, or DONE change.

The grade threshold and PP 610 anchor are only two Arizona DONE-gate inputs.
Claims/open-ground, geology/faults, aeromagnetics, county recorder coverage,
quad maps, CI acceptance, and 49-state storage/heap evidence remain owned by
their respective workstreams. Those unresolved gates prevent interpreting
this package as Arizona release approval.

For bit-identical reproduction, freeze the Poppler and Tesseract versions
recorded in `build.json`. A deliberate toolchain change should produce new
page-index hashes and receive page review rather than inheriting these review
identities.
