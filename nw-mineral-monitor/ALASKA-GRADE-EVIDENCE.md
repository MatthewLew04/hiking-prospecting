# Alaska WS11 grade evidence

Alaska has a private, national-compiler-compatible WS9 evidence package with
26 distinct graded mines, prospects, deposits, or occurrences; 26 verbatim
quotations and numbered page citations; and two consumed independent official
primary-source series. The package also carries a conservative Alaska Resource
Data File (ARDF) crosswalk and the complete Alaska PP 610 anchor: all 43
numbered districts in Figure 5. This is evidence only. It does not mark Alaska
DONE, enable a release, edit `states/AK.yaml`, update coverage or a browser
manifest, or publish a layer.

## Reviewed source corpus

`pipelines/config/ak_grade_sources.json` pins three official documents by
catalog URL, document URL, exact byte count, page count, and SHA-256:

- Roe (1994), *U.S. Bureau of Mines mineral investigations in the Unakwik
  area, Chugach National Forest, Alaska*, USBM Open-File Report 50-94:
  8,028,244 bytes, 194 PDF pages, SHA-256
  `844be116be0e97bdddea12dd35cce87ea18fe9f4699ae8e492f04a6825011ed9`;
- Roehm (1943), *Strategic and critical mineral occurrences in southeastern
  Alaska*, Alaska Territorial Department of Mines Miscellaneous Report 191-5:
  3,614,781 bytes, 98 PDF pages, SHA-256
  `dc8f364af1f8c72f91eb36e1f77cd53ca55ab74beb90d8170ec526e1a5bd42ad`;
  and
- Koschmann and Bergendahl (1968), *Principal Gold-Producing Districts of the
  United States*, USGS Professional Paper 610: 39,132,819 bytes, 290 PDF
  pages, SHA-256
  `f4c1f048aaffe1e8d1431983e0a7b3f1bb543fab0f5380cd42e85c0a6a840896`.

The two Alaska reports are served through the Alaska Division of Geological &
Geophysical Surveys publication system; PP 610 is served by USGS. Only HTTPS
URLs on `dggs.alaska.gov` and `pubs.usgs.gov` are allowed. The producer also
hard-codes the reviewed bytes, pages, text mode, and document hashes. An
unofficial host, redirect, checksum or byte drift, page-count change, missing
source, source repin, path traversal, or symlink fails closed.

The reviewed extraction is
`grades-research/ak/reviewed_grade_evidence.json`. Eighteen Unakwik-area
targets come from OFR 50-94 and eight southeastern Alaska targets come from MR
191-5. The rows preserve native Au, Ag, Cu, Pb, and Zn measurements; ppm Au or
Ag is represented as the dimensionally equivalent national-schema unit
`grams_per_metric_tonne`. Unsupported nickel and platinum-group measurements
remain visible in verbatim quotations but are not emitted as a supported
national commodity. Ranges are not averaged, and adjacent table columns are
identified in each basis field.

MR 191-5 combines separately paginated report sections in one PDF. Its page
cites therefore state both the numbered section page and the PDF page, for
example `Copper occurrences section p. 3 (PDF p. 49)`. That prevents the
source's internal pagination from being silently mistaken for a PDF index.

## ARDF occurrence backbone

`grades-research/ak/ardf_target_crosswalk.json` preserves the reviewed subset
of the current official USGS ARDF feature service. Its 21 source records are
bound to canonical record-set SHA-256
`9449d184938389de6d1368dd047203cd9909ffce134e2fb18e95e7810448c7e5`
and service-metadata SHA-256
`9d83142a2b1abf145fde09054e9aff8e04e1d90bf62d2f03da66e02c2ab5e290`.
The producer requires the exact reviewed record identities and target
decisions.

Twenty-three of the 26 grade targets link to 21 unique ARDF records using
exact names, published alternate names, report-defined aggregation, or
coordinates. Three targets remain explicit findings rather than guessed
links:

- the Bureau-discovered Saddle occurrence;
- the Bureau-discovered Slipper Point occurrence; and
- the Kasaan-area Shepard mine.

The SI040 `quad_63360` value is normalized to `D-7` after removing a trailing
control character returned by the service; that normalization is documented
in the review scope. ARDF supplies occurrence identity and context only. It
does not replace the two official report quotations, numbered page cites, or
page hashes that support grades. The crosswalk is emitted only as the private
artifact `backbone/ak-ardf-crosswalk.json`.

## Complete PP 610 Alaska anchor

`grades-research/ak/pp610_district_inventory.json` is a complete visual
transcription of all 43 numbered Alaska entries in PP 610 Figure 5, printed
page 10 (PDF page 16):

- Cook Inlet-Susitna: Kenai Peninsula, Valdez Creek, Willow Creek, and
  Yentna-Cache Creek;
- Copper River: Chistochina and Nizina;
- Kuskokwim: Georgetown, Goodnews Bay, McKinley, and Tuluksak-Aniak;
- Northwestern Alaska: Shungnak;
- Seward Peninsula: Council, Fairhaven, Kougarok, Koyuk, Nome, Port Clarence,
  and Solomon-Bluff;
- Southeastern Alaska: Chichagof, Juneau, Ketchikan-Hyder, Porcupine, and
  Yakataga;
- Southwestern Alaska: Unga;
- Yukon: Bonnifield, Chandalar, Chisana, Circle, Eagle, Fairbanks, Fortymile,
  Iditarod, Innoko, Hot Springs, Kantishna, Koyukuk, Marshall, Nabesna,
  Rampart, Ruby, Richardson, and Tolovana; and
- Prince William Sound: Port Valdez.

The figure page is bound to the deterministic 300-dpi render SHA-256
`d40c49349862becee868cf6afdf7ba87b002d95bba125e732c84652fe0dc4e85`.
Each district retains its exact numbered figure fragment as a distinct
verbatim quote and uses the numbered cite `Figure 5, p. 10 (PDF p. 16)`.
PP 610 therefore supports a conventional, complete Alaska district inventory;
no no-district exception is needed.

## Page-level review boundary

Both Alaska grade reports are image-backed historical scans. Their embedded
text is a useful cross-check, but it cannot decide table columns, superscripts,
punctuation, or poorly recognized figures. Every one of the 24 distinct cited
grade pages is rendered with `pdftoppm -r 300 -png -singlefile`. The producer
requires the resulting page-image SHA-256 to equal the human-reviewed row
before emitting evidence. `pdftotext -enc UTF-8 -layout` and quote similarity
remain diagnostic only.

OFR 50-94's current diagnostic scores range from 0.916667 to 1.0. MR 191-5's
older scan ranges from 0.411765 to 0.903226. PP 610's Figure 5 text-layer word
coverage ranges from 0.5 to 1.0 because the extraction omits several labels,
including Shungnak, Chandalar, and Koyukuk. In all three cases the deterministic
page-image hash—not OCR similarity—is the review authority.

Each source receives a canonical page index with the source PDF hash, cited
PDF page, page-text hash, 300-dpi page-image hash, diagnostic score, and
explicit review-boundary label. The page-index hash becomes part of the source
identity passed to the national compiler.

## Operator workflow

Validate the small reviewed manifests without installing the private PDF
cache:

```bash
python3 pipelines/build_alaska_grade_evidence.py check
```

Fetch or verify the three checksum-pinned official PDFs under the ignored
`pipelines/cache/ak-grade-sources/` directory:

```bash
python3 pipelines/build_alaska_grade_evidence.py fetch
```

Build the private compiler inputs:

```bash
python3 pipelines/build_alaska_grade_evidence.py build
```

The default output is:

```text
build-inputs/ws9/ak-grade-evidence/
  build.json
  backbone/ak-ardf-crosswalk.json
  grades/ak.json
  pp610/ak.json
  page-indexes/<source-id>.<page-index-sha256>.json
```

The verified canonical artifacts are:

- `build.json`: SHA-256
  `6877f8dfc43e953aa9e2692e5b622ee05dd631acad312f25eed6a3928563606a`;
- `grades/ak.json`: SHA-256
  `983ce0cc7eef35aa6bc0cac9b399c0a5a4aba29ddcac85fe532c132926a8a371`;
- `pp610/ak.json`: SHA-256
  `99aa007e354d23a3b9cd4cbfa53488142f1f6a43a909f0c2116e40dfc8ece9e5`;
- `backbone/ak-ardf-crosswalk.json`: SHA-256
  `1c7846117e0171196c95461b3f1caf7938228a46b2b18677febaa91e8da94efd`;
- MR 191-5 page index:
  `26fbc7f9172fa33aaf8ce7132df4ca6ad64c43316cf212a1df3c6e8e8b532e1e`;
- OFR 50-94 page index:
  `a7e23aa622bbd491d13ec82c9cb167560e506262b90da3fc4ca76450d03d8031`;
  and
- PP 610 page index:
  `556c2b2de4d6a5910949d68da174041acc2addee5145af1502d321376ce923c8`.

The build records producer SHA-256
`e43c89474d785d490d1c6125e84e9643390bc1f022711d2f1e119bbb22f4c400`.
Two independent full-cache builds were compared recursively and were
byte-identical. Run the 13 focused adversarial and integration tests with:

```bash
python3 -m unittest tests.test_alaska_grade_evidence -v
```

The suite covers source pins and official-host restrictions, cache
containment, checksum drift, exact target identity, two-source diversity,
mandatory numbered page cites and page-image hashes, unsupported-commodity
handling, exact ordered Figure 5 completeness, ARDF record tampering, guessed
no-match links, national compiler compatibility, deterministic rebuilds, and
the no-release boundary.

## National handoff and unresolved DONE gates

`build.json` declares `effect: evidence_only_no_release_or_done_mutation` and
`is_release_decision: false`. Its grade and PP 610 descriptors can be composed
later into the private exact-49 national evidence inventory; the ARDF artifact
can be consumed by Alaska's occurrence pipeline without altering grade
provenance.

This package satisfies only Alaska's isolated grade threshold, ARDF occurrence
crosswalk, and PP 610 anchor. Federal MLRS claims, Alaska state mining claims
and rent deadlines, closed claims and defensible open-ground evidence,
best-available statewide geology and faults, aeromagnetic provenance, recorder
coverage, top-five quad inventory, acceptance CI, and national storage/heap
evidence remain separate gates. Alaska must stay disabled until those
independent requirements are complete.

For bit-identical reproduction, freeze the Poppler and Python versions
recorded in `build.json`. A deliberate rendering-tool change must produce new
page-index hashes and receive page review rather than inheriting these review
identities.
