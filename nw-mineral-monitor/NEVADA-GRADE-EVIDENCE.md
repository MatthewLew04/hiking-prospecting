# Nevada-first WS9 grade evidence

The Nevada producer now creates a private, national-compiler-compatible grade
input with 26 distinct graded mines from 12 official primary documents. It
also creates the complete Nevada PP 610 anchor: all 71 districts in Figure 16
and the district entries on printed pages 171-200. This is evidence only. It
does not mark Nevada DONE, enable a release, edit `states/NV.yaml`, update a
browser manifest, or publish a layer.

This producer is intentionally separate from
`pipelines/build_nevada_state_survey_pmtiles.py`: that builder owns tiled NBMG
geology and mining-district geometry, while this one owns document/page grade
provenance. The only JSON emitted here stays in private evidence staging; no
statewide GeoJSON is browser-delivered.

## Reviewed inputs

The source corpus is declared in
`pipelines/config/nv_grade_sources.json`. Every entry records the official
catalog and document URLs, title, authority, publication year, exact byte
count, PDF page count, and document SHA-256.

The 13 pinned documents are:

- two image-only NBMG Mining District Files: item 21600006, *Report of the
  Great Bend Mine*, and item 21600019, *Goldfield, Nevada*;
- USGS Bulletins 407, 408, 414, 601, 715-K, 741, 762, and 906-D;
- USGS Professional Papers 171 and 406; and
- Koschmann and Bergendahl, USGS Professional Paper 610.

All 13 documents are consumed in the current build: 12 support grade rows and
PP 610 supports the district anchor. The inventory permits only official
`data.nbmg.unr.edu`, `collections.nbmg.unr.edu`, and `pubs.usgs.gov` HTTPS
hosts. A redirect outside those hosts, source checksum drift, changed PDF page
count, duplicate ID, traversal path, symlink, or unreviewed schema field fails
closed.

The human-reviewed mine rows are in
`grades-research/nv/reviewed_grade_evidence.json`. Each row retains the exact
source quotation, numbered page citation, source PDF page, mine/district/county
identity, and one or more native Au, Ag, Cu, Pb, Zn, or Fe measurements. The
current set deliberately uses native reported grades, not inferred
dollar-per-ton conversions, so it is independent of the still-incomplete
national six-commodity price table.

The 71-row PP 610 inventory is in
`grades-research/nv/pp610_district_inventory.json`. Its scope is the complete
Nevada chapter, PDF pages 177-206 (printed pages 171-200). `source_heading`
preserves OCR/text-layer peculiarities such as `SIJ,VER PEAK DISTRICT` so the
builder can locate the reviewed heading without silently correcting source
text. Public-facing district names are separately normalized.

## Quote and page verification

For USGS documents with a text layer, the builder extracts only the cited page
with `pdftotext -enc UTF-8 -layout`, hashes those exact bytes, and requires the
reviewed quote to match the page. The resulting page-text SHA-256 is carried on
every evidence row.

The two NBMG Mining District Files are image-only. For them, the review
boundary is stronger than OCR text: the builder renders the cited PDF page at
300 dpi and requires its PNG SHA-256 to equal the page-render hash stored on
the reviewed row. Tesseract `--psm 6` is retained as a diagnostic search
cross-check, not as authority for punctuation or numbers. This distinction is
important for historic typewritten tables; OCR can misread `0.23 oz.Au` even
when the reviewed page image is unchanged.

Each source gets a canonical derived page index containing its document hash,
cited page hashes, quote-check scores, page cites, and—for NBMG pages—the page
image hashes and explicit `page_image_sha256` review boundary. The SHA-256 of
that page index is embedded in the source identity supplied to the national
compiler. Thus a source, page extraction, or reviewed image change produces a
different evidence identity.

PP 610 quotes are extracted from ordered page bounding boxes as the first
complete sentence following the exact district heading. When a heading occurs
at the foot of a page, the producer preserves only the source-page fragment;
it never binds next-page text to the preceding page hash. The complete set is
then checked by `validate_pp610_document`.

## Operator workflow

Poppler (`pdfinfo`, `pdftotext`, and `pdftoppm`) and Tesseract are required for
a full build. First validate the small review manifests without downloading
PDFs:

```bash
python3 pipelines/build_nevada_grade_evidence.py check
```

Fetch only the documents used by the current build, or verify an existing
cache:

```bash
python3 pipelines/build_nevada_grade_evidence.py fetch
```

`fetch --all` is equivalent today because every pinned document is consumed.
PDFs are held under ignored `pipelines/cache/nv-grade-sources/`; source PDFs
must not be copied under `site/`.

Build the compiler inputs in private staging:

```bash
python3 pipelines/build_nevada_grade_evidence.py build
```

The default output is:

```text
build-inputs/ws9/nv-grade-evidence/
  build.json
  grades/nv.json
  pp610/nv.json
  page-indexes/<source-id>.<page-index-sha256>.json
```

`build.json` inventories every output by relative path, bytes, and SHA-256,
records the extractor versions, reports computed metrics, and declares
`effect: evidence_only_no_release_or_done_mutation`. `grades/nv.json` is
validated by `build_national_grade_evidence.validate_grade_document`; the PP
610 input is validated by `validate_pp610_document` before any output report is
accepted.

Run the adversarial and full-cache integration tests with:

```bash
python3 -m unittest tests.test_nevada_grade_evidence -v
```

The full integration test skips when the official PDF cache is absent. The
manifest-only, host restriction, traversal, checksum-drift, duplicate-mine,
image-hash, 71-district completeness, OCR-noise, and page-fragment tests always
run.

## National handoff and blockers

The grade and PP 610 descriptors in `build.json` are ready to become the `NV`
entry in the private exact-49 inventory consumed by
`pipelines/build_national_grade_evidence.py`. Descriptor paths are relative to
this Nevada output root; preserve or adjust that relationship when composing
the national inventory.

Do not interpret the isolated Nevada threshold observation as the full
per-state DONE gate. The national publisher still requires all 49 state inputs
and a reviewed six-commodity price configuration, while Nevada release also
depends on claims/open-ground, geology/faults, aeromag, recorder, quad-map, CI,
and scale-budget evidence owned by other workstreams. No release/DONE state is
changed here.

For bit-identical release reproduction, freeze the Poppler and Tesseract
versions recorded in `build.json`; OCR and PDF text extraction can vary across
tool versions even when the pinned official PDF is identical. Any deliberate
toolchain change should generate new page-index hashes and receive review
rather than reusing old identities.
