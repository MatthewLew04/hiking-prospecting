# Colorado WS11 grade evidence

Colorado now has a private, national-compiler-compatible evidence package with
26 distinct graded mines, 26 verbatim quotations and numbered page citations,
and two consumed official primary sources. It also has the complete Colorado
PP 610 anchor: all 44 numbered districts in Figure 10. This is an evidence
result only. It does not mark Colorado DONE, enable a release, edit
`states/CO.yaml`, update the public manifest or coverage grid, or publish a
browser layer.

## Evidence corpus

The checksum-pinned source inventory is
`pipelines/config/co_grade_sources.json`. It permits only official
`pubs.usgs.gov` HTTPS URLs and records the exact document byte count, page
count, and SHA-256 for:

- Sims, Drake, and Tooker (1963), *Economic Geology of the Central City
  District, Gilpin County, Colorado*, USGS Professional Paper 359;
- Irving and Bancroft (1911), *Geology and Ore Deposits Near Lake City,
  Colorado*, USGS Bulletin 478; and
- Koschmann and Bergendahl (1968), *Principal Gold-Producing Districts of the
  United States*, USGS Professional Paper 610.

The human-reviewed rows are in
`grades-research/co/reviewed_grade_evidence.json`. Twenty mines come from the
explicit average-grade columns in PP 359 Tables 11 and 13, printed pages 35 and
37. Six more come from mine-specific Lake City descriptions in Bulletin 478:
Monte Queen, Vermont, Gallic-Vulcan, Golden Fleece, Black Crook, and Moro.
Together the rows retain native Au, Ag, Cu, Pb, and Zn measurements. None uses
a historic dollar-per-ton conversion, so this package does not depend on an
unfinished national commodity-price table.

The district inventory is
`grades-research/co/pp610_district_inventory.json`. It transcribes the complete
numbered legend of PP 610 Figure 10, printed page 85 (PDF page 91), including
both separately numbered Clear Creek placer entries. County-qualified public
names disambiguate those two entries. Each of the 44 entries also carries a
substantive descriptive quotation and printed page citation from the Colorado
chapter.

## Page-level review boundary

Every cited source PDF is verified before extraction. Narrative rows from
Bulletin 478 require the reviewed quote to match the cited page's embedded text
and carry the SHA-256 of the exact `pdftotext -enc UTF-8 -layout` output.

PP 359's embedded text layer merges adjacent numeric table cells—for example,
the visual values `25.35` and `.52` can appear as one extracted token. Treating
that malformed text as authoritative would silently change grades. Therefore
the review boundary for printed pages 35 and 37 is the deterministic SHA-256
of each page rendered at 300 dpi. The reviewed table transcription remains in
the evidence row, while its text-match score is diagnostic only. PP 610 Figure
10 uses the same 300-dpi image boundary for the exact-44 completeness check
because its three-column legend splits some district numbers from their names
in the text layer.

For PP 610's descriptive evidence, 40 district quotations are deterministically
re-derived as the first complete sentence after an exact text-layer heading
using ordered page bounding boxes. Four Figure 10 entries without an
independently extractable heading—both Clear Creek placer entries, Alice, and
Tarryall—use reviewed county-section sentences. Every one of the 44 chapter
quotes must attain at least 85 percent word coverage on its cited page. The
current package binds 30 distinct PP 610 description pages in addition to the
Figure 10 image.

Derived page indexes carry the document hash, page-text hash, page-image hash
where applicable, quote-check score, and explicit review-boundary label. Their
own hashes become part of the source identities passed to the national
compiler.

## Operator workflow

Validate the small reviewed manifests without the PDF cache:

```bash
python3 pipelines/build_colorado_grade_evidence.py check
```

Fetch or verify the three official documents under the ignored
`pipelines/cache/co-grade-sources/` directory:

```bash
python3 pipelines/build_colorado_grade_evidence.py fetch
```

Build the private compiler inputs:

```bash
python3 pipelines/build_colorado_grade_evidence.py build
```

The default output is:

```text
build-inputs/ws9/co-grade-evidence/
  build.json
  grades/co.json
  pp610/co.json
  page-indexes/<source-id>.<page-index-sha256>.json
```

The current canonical build has SHA-256
`3015258f19efbfea44a52ef24b27b20ed018b8c3edf7b7057f612fd3c1df674a`.
Two consecutive full-cache builds are byte-identical. Both `grades/co.json`
and `pp610/co.json` are accepted by the corresponding validators in
`pipelines/build_national_grade_evidence.py` before `build.json` is written.

Run the focused adversarial and integration tests with:

```bash
python3 -m unittest tests.test_colorado_grade_evidence -v
```

The integration test skips when the official PDF cache is absent. Manifest,
host, path traversal, checksum drift, duplicate identity, native-unit,
image-boundary, exact-44-district, compiler-contract, and no-release checks run
without relying on network access.

## National handoff and remaining gate work

The grade and PP 610 descriptors in `build.json` can be composed into the
private exact-49 inventory consumed by the national grade publisher. Descriptor
paths are relative to this Colorado staging directory and must remain
checksum-bound during that composition.

Meeting Colorado's isolated grade threshold is not a state release decision.
The Colorado DONE gate still depends on independently reviewed federal claims,
closed claims and open-ground evidence, best-available statewide geology and
faults, aeromagnetic provenance, recorder coverage, top-five quad inventory,
acceptance CI, and national storage/heap budgets. The producer records
`effect: evidence_only_no_release_or_done_mutation` and
`is_release_decision: false` to make that boundary machine-readable.
