# New Mexico WS11 grade evidence

New Mexico has a private, national-compiler-compatible WS9 evidence package
with 26 distinct graded mine/property targets, 26 verbatim quotations and
numbered page citations, and two consumed official primary sources. It also
contains the complete New Mexico PP 610 anchor: all 17 numbered districts in
Figure 19. This is evidence only. It does not mark New Mexico DONE, enable a
release, edit `states/NM.yaml`, update coverage or a browser manifest, or
publish a layer.

## Reviewed source corpus

`pipelines/config/nm_grade_sources.json` pins three official USGS documents by
catalog URL, document URL, exact byte count, page count, and SHA-256:

- Lindgren, Graton, and Gordon (1910), *The Ore Deposits of New Mexico*, USGS
  Professional Paper 68: 245,325,470 bytes, 400 PDF pages, SHA-256
  `8d49dfbc18d0b969d9123e29e5c629fd2a3f36bf9b4cc54a14b17111e5828e6f`;
- Lasky (1936), *Geology and Ore Deposits of the Bayard Area, Central Mining
  District, New Mexico*, USGS Bulletin 870: 14,685,945 bytes, 162 PDF pages,
  SHA-256
  `b3fa4b736e71e8ee8824dc73c3e92e9792b045c801f08d1c6061fd4c6b46ed4a`;
  and
- Koschmann and Bergendahl (1968), *Principal Gold-Producing Districts of the
  United States*, USGS Professional Paper 610: 39,132,819 bytes, 290 PDF
  pages, SHA-256
  `f4c1f048aaffe1e8d1431983e0a7b3f1bb543fab0f5380cd42e85c0a6a840896`.

Only HTTPS URLs on `pubs.usgs.gov` are allowed. A redirect outside that host,
checksum or byte drift, page-count change, missing source, path traversal,
symlink, duplicate ID, or unreviewed schema field fails closed.

The reviewed extraction is
`grades-research/nm/reviewed_grade_evidence.json`. Twenty targets come from PP
68, spanning Red River, Apache/Black Range, Hillsboro, Lake Valley, Victorio,
Pinos Altos, Apache No. 2, and Fremont. Six Central-district targets come from
Bulletin 870: Lucky Bill, Ground Hog, Three Brothers, Owl, Silver King, and
Lion No. 2. The set retains native Au, Ag, Cu, Pb, Zn, and Fe measurements.

Ranges are not averaged. A selected measurement records the stated lower
endpoint while its complete range remains in the verbatim quotation. A trace
is omitted, never encoded as zero. Historical dollar-per-ton values are also
left in their quotations but are not converted to metal grade, so the package
does not depend on an unfinished historical commodity-price table.

## PP 610 completeness anchor

`grades-research/nm/pp610_district_inventory.json` transcribes all 17 numbered
entries in PP 610 Figure 19 on printed page 203:

- Tijeras Canyon; Mogollon; Elizabethtown-Baldy; Organ;
- Central, Pinos Altos, and Steeple Rock;
- Lordsburg; White Oaks; Nogal; Jarilla; Cochiti; Willow Creek;
- Old Placer and New Placer; Hillsboro; and Rosedale.

The exact Figure 19 page is bound to the 300-dpi rendered-image SHA-256
`fc9646c4ab5e7faeb06a9accf2dd0fac101c28a7b64f80ccfee93c04b9d5cb02`.
The producer also finds each independently reviewed chapter heading in ordered
PDF bounding boxes and derives the first complete descriptive sentence after
that heading. All 17 derived quotations pass an 85-percent word-coverage check
against their cited page and are accepted by
`build_national_grade_evidence.validate_pp610_document`.

The inventory preserves source text-layer artifacts in locator headings—for
example `TIJE.RAS CANYON DISTRICT` and
`ELIZABETHTOWN -BALDY DISTRICT`—while public names remain normalized. Current
county names are used for targets in present-day Hidalgo County even where PP
68 predates that county's creation.

## Page-level review boundary

PP 68 and Bulletin 870 are image-backed historical scans. Their embedded OCR
is useful for finding candidate passages but cannot decide punctuation,
fractions, or table columns. Every one of the 17 distinct cited grade pages is
therefore rendered with `pdftoppm -r 300 -png`; the producer requires the
resulting PNG SHA-256 to match the reviewed row before it emits any evidence.
The embedded `pdftotext -enc UTF-8 -layout` output and quote-match score are
retained only as diagnostic cross-checks. The current diagnostic range is
0.846-1.000; the page-image hash, not OCR similarity, is the human-review
authority.

The Bonanza and Silver King evidence comes from visually reviewed table rows.
Their basis fields identify the source columns so adjacent assay values cannot
silently shift. Narrative rows keep the complete source sentence or the
minimal adjoining sentence needed to identify the stated basis.

Each source receives a canonical derived page index containing the source PDF
hash, cited PDF and printed pages, page-text hash, 300-dpi page-image hash,
diagnostic score, and explicit review-boundary label. The page-index hash is
then part of the source identity passed to the national compiler.

## Operator workflow

Validate the small checked-in manifests without installing the private PDF
cache:

```bash
python3 pipelines/build_new_mexico_grade_evidence.py check
```

Fetch or verify the three checksum-pinned USGS PDFs under the ignored
`pipelines/cache/nm-grade-sources/` directory:

```bash
python3 pipelines/build_new_mexico_grade_evidence.py fetch
```

Build the private compiler inputs:

```bash
python3 pipelines/build_new_mexico_grade_evidence.py build
```

The default output is:

```text
build-inputs/ws9/nm-grade-evidence/
  build.json
  grades/nm.json
  pp610/nm.json
  page-indexes/<source-id>.<page-index-sha256>.json
```

The verified canonical artifacts are:

- `build.json`: SHA-256
  `221f114c29ac9ff0867950cda6c55d0736a1a2bda9fda4856d28e20ed571e302`;
- `grades/nm.json`: SHA-256
  `de9ce4becaac8792f96d5e12f909c1a28fe616141fa7e8ab6ec2bc99dc24732f`;
- `pp610/nm.json`: SHA-256
  `0c77fed80b5e643b06bac69e3762bb8f045e6ff3ad858d5ae26de61b52b1fd50`;
- Bulletin 870 page index:
  `e3e056a737e166e9a0b7cdba43b9c89d7c9dcc02b0b06750652deebec713b909`;
- PP 68 page index:
  `ee062611abd5b7cb340926eed3e672c0bf1f729d4778326d5c00f17d45c51a82`;
  and
- PP 610 page index:
  `5310208335225786891289411cbeddb254c3e390566ba6e7a8636878815d07ff`.

Two independent full-cache builds were compared recursively and were
byte-identical. Run the focused adversarial and integration suite with:

```bash
python3 -m unittest tests.test_new_mexico_grade_evidence -v
```

The suite covers source identity and host restrictions, cache containment,
checksum drift, exact target identity, exact ordered Figure 19 completeness,
source diversity, mandatory page-image hashes, range/trace handling, the
national compiler contracts, and the no-release boundary.

## National handoff and unresolved DONE gates

`build.json` declares `effect: evidence_only_no_release_or_done_mutation` and
`is_release_decision: false`. Its grade and PP 610 descriptors can be composed
later into the private exact-49 national evidence inventory.

This package satisfies only New Mexico's isolated grade threshold and PP 610
anchor. Federal active/closed claims and defensible open-ground evidence,
best-available statewide geology and faults, aeromagnetic provenance, county
recorder coverage, top-five quad inventory, green acceptance CI, and national
storage/heap evidence remain separate gates. New Mexico must stay disabled
until those independent requirements are complete.

For bit-identical reproduction, freeze the Poppler and Python versions recorded
in `build.json`. A deliberate toolchain change must produce new page-index
hashes and receive page review rather than inheriting these review identities.
