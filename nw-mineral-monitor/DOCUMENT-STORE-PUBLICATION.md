# WS12 source-document store and citation publication

`pipelines/build_doc_store.py` is the publication boundary between the
checksum-pinned source PDFs staged under `pipelines/cache/` and the archived
corpus that every citation in this system opens. Part A's
`var/ws12/manifest.jsonl` is canonical for every harvested asset and source
occurrence; the reviewed inventories supply the remaining legacy pilot plus
page counts and citation metadata. The builder admits a document only when its
URL, byte count, SHA-256, page count, and rights reconcile, writes exactly two
objects per document, resolves reviewed citations against the stored text
layer, and emits `site/data/docs/manifest.json` plus the private, gitignored
lineage catalog `var/ws12/document-assets.json`.
`pipelines/config/ws12_documents.json` registers 35 rows; the current
generation stores the 25 documents whose rights are affirmatively resolved.

It does not crawl, discover, or fetch documents; that is the WS12 harvest
pipeline's job. It does not upload to S3, presign anything, edit
`states/*.yaml`, change a release flag, touch `site/data/manifest.json`, or
satisfy any DONE gate. It does not rewrite, reflow, re-rasterize, or
otherwise alter a source page, and it does not assert that a quote is
highlightable when the text layer does not contain it.

## Inputs

Every input lives outside `site/`.

```text
pipelines/config/ws12_documents.json     reviewed registry: portal, filing key,
                                         retrieval date, licence note, subjects
var/ws12/manifest.jsonl                  canonical harvested raw identity,
                                         source occurrences, portal mine links
pipelines/config/ws12_citations.json     reviewed citations for documents that
                                         carry no WS9 grade evidence
pipelines/config/{st}_grade_sources.json checksum-pinned document identity
grades-research/{st}/reviewed_grade_evidence.json   WS11 reviewed page cites
site/data/grades/grades.json             browser grade rows carrying page cites
pipelines/cache/**/*.pdf                 the staged source PDFs (gitignored)
var/ws12/document-assets.json            normalized private lineage (output)
```

A registry row either names an `inventory` and a `source_id` inside it, or
carries the full identity inline for a document no grade inventory pins:

```json
{
  "source_id": "igs-mrds-w015681",
  "state": "ID",
  "portal": "igs-mines",
  "mine_id": "stategeo-igs-dd-1-if0126",
  "retrieved": "2026-08-14",
  "title": "MRDS-W015681: ST. LOUIS MINE — USGS Mineral Resources Data System record",
  "authority": "U.S. Geological Survey Data Series 20, served by the Idaho Geological Survey",
  "catalog_url": "https://www.idahogeology.org/WebMap4/WebData/Mines.aspx?Operation=Details&IGSID=IF0126",
  "document_url": "https://www.idahogeology.org/Uploads/Data/MILS_MRDS/MRDS-W015681.pdf",
  "local_path": "pipelines/cache/ws12/manual-if0126/MRDS-W015681.pdf",
  "bytes": 110393,
  "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "pages": 1,
  "subjects": [
    {"state": "ID", "mine_id": "stategeo-igs-dd-1-if0126", "label": "St. Louis Mine"}
  ]
}
```

The abbreviated example shows one row only; the committed registry expands to
all 35 rows. Every row also carries `public_domain` and a nonempty
`rights_basis` — see Rights below. Absolute paths, `..` segments, symlinks, paths outside the
repository, duplicate JSON keys, and non-finite numbers are rejected. Every
staged file is re-hashed and re-sized on every run, and a byte of drift fails
the build before any object or manifest is written.

## Key scheme and identity

```text
docs/{state}/{portal}/{mine_id}/{sha256}/raw.pdf
docs/{state}/{portal}/{mine_id}/{sha256}/searchable.pdf
```

`{sha256}` is `doc_id`, the SHA-256 of the raw original, for both variants —
never a filename, a portal URL, or a row index — so re-OCR replaces one object
without moving the document or breaking a citation. A manifest's asserted key
is never trusted: the validator recomputes it from the row's own state,
portal, mine_id, and doc_id and fails on any difference.

`{mine_id}` is the document's filing subject and carries a dataset namespace
(`ws9-`, `stategeo-`, `mrds-`, `usmin-`, `mlrs-`) so identifiers from
different catalogues cannot collide in one path. A document about a district,
a whole state, or the whole country is filed under a reserved `district-`,
`statewide-`, or `statewide-us` key with the reserved `US` scope. The filing
key is a location, not a coverage claim; `subjects` enumerates every mine the
document is cited for. When one file appears in several state inventories —
USGS Professional Paper 610 sits in seven — exactly one registry row must set
`"filing": true`, and the build fails rather than letting registry order pick
the key.

The private normalized catalog prevents the key scheme from collapsing three
different concepts. `assets` deduplicate only on the raw SHA-256;
`source_occurrences` retain every portal/source URL and retrieval observation;
and `mine_links` retain both exact portal mine IDs and internal subject IDs.
Raw and searchable variants carry independent `harvested`, `pending`, or
`store_ready` status. When an asset appears in both inputs, harvest provenance
wins and the addendum only enriches its store keys, searchable SHA, and
citation subjects. Store-only documents remain `legacy_pilot`, preserving the
25-document rollout while portals migrate to Part A.

## Text layer

`raw.pdf` is the original, byte-identical to what the portal served.
`searchable.pdf` carries a text layer over those same pages. Three states,
never collapsed:

- `native` — the original already had a text layer. The searchable copy is
  deliberately the same bytes and the manifest says so. Re-OCR would add
  noise, not evidence.
- `ocr_added` — the original was image-only. `ocrmypdf --skip-text` is used
  when installed; otherwise each page is rendered, read with Tesseract, and
  an invisible text layer is inserted at the recognised word boxes of the
  untouched original page. The tool and version are recorded.
- `absent` — no text layer could be produced. The document still opens; its
  citations can pin a page but cannot be highlighted.

The builder refuses an OCR product whose page count differs from the original
and refuses one that returns the original bytes. Pagination is therefore 1:1
and page N of a citation is page N of the object.

## Citations

A citation is `{doc_id, page, page_cite, quote, quote_located, state,
mine_id, dataset}`. The page is decided by the store, not asserted by the
reviewer: the builder searches the searchable copy's text layer for the
quote, and only a located quote sets `quote_located: true`. When the quote
cannot be found, the reviewer's `pdf_page` or the printed page is used and
`quote_located` is false, which the viewer shows as "not located on this
page — page shown as cited" rather than highlighting something plausible.
The current generation has 202 citations, 141 of them located.

Browser grade rows join the store only when their source URL is a document
we hold and the mine they name is already a declared subject of it. A row
whose document is not stored keeps its portal link and gains nothing, which
is the honest outcome rather than a chip that opens the wrong file.

## Rights

Public reachability is not a storage licence. Every registry row states
`public_domain` explicitly and gives a `rights_basis`; the builder never
reads, copies, OCRs, or publishes the bytes of a row whose `public_domain` is
false, and the resolver refuses to serve one even if a manifest somehow
carried it. Rights are a property of the bytes, so two registry rows for the
same document must agree; a disagreement fails the build rather than letting
row order decide.

The 25 stored documents are all works of the United States Government under
17 U.S.C. 105 (USGS and U.S. Bureau of Mines). Four registered rows are
withheld pending review — `atdm-mr191-5` (Alaska DGGS), `azbm-b137` (Arizona
Bureau of Mines), and the two NBMG Mining District Files
`nbmg-mdf-21600006` and `nbmg-mdf-21600019`. Each is a state geological
survey's own publication served openly by that survey; open service is not an
affirmative public-domain basis, so they stay registered and unstored. This
mirrors the WS12 harvest rights gate, which skipped Idaho Mineral Property
File `IF0131_001.pdf` on the same reasoning; that file is likewise absent
here.

## Delivery

The corpus is private. `docs/` is deliberately absent from the CloudFront
prefix allowlist in `SiteBucketPolicy`, so it cannot be crawled: the only way
to read a stored PDF is a presigned GET minted by `infra/docs_lambda.py`
after it has verified the caller's Cognito access token, for one object, with
a 300-second TTL. `site/viewer.html` opens that object, pins the page, and
highlights the quote in the text layer. The originating portal URL is
displayed beside the document for provenance and is never fetched.

`site/data/docs/manifest.json` is the reviewed local build artifact. Promotion
uploads it to private key `private/ws12/document-store-manifest.json`; the
CloudFront policy explicitly denies the old public path. After authentication,
the Docs API returns a minimized browser catalog with titles, stable IDs,
subject joins, and reviewed citations but no S3 keys, object hashes, or rights
internals. The browser never downloads the source-of-truth manifest directly.

Raw originals are tagged `ws12-variant=raw` at upload and transition to
Infrequent Access after 30 days. Searchable copies stay in Standard because
every citation opens one. `pipelines/validate_doc_store.py` fails if the
deployed lifecycle disagrees with the manifest's declared retention, including
the explicit positive-size override required for originals below 128 KiB.

## Build and verify

```bash
python3 pipelines/build_doc_store.py \
  --registry pipelines/config/ws12_documents.json \
  --store-dir pipelines/cache/ws12/store
python3 pipelines/validate_doc_store.py --store-dir pipelines/cache/ws12/store
```

For the production registry the build requires
`var/ws12/manifest.jsonl` and atomically exports
`var/ws12/document-assets.json`. Custom fixture registries may opt in with
`--harvest-manifest` and `--asset-catalog`; they do not silently consume the
operator's production runtime state.

The local store hardlinks store objects to their sources wherever the bytes
are identical, so a generation costs almost no disk; only an OCR product is
new bytes. The build is byte-reproducible: the same inputs produce the same
manifest SHA-256.

## Promotion and deploy boundary

```bash
ENABLE_LEGACY_DOC_STORE=true bash infra/deploy.sh upload-doc-store
```

`upload-doc-store` runs the gate with `--store-dir` first, so an unverified
generation never becomes the served corpus. Each object is written with the
manifest's explicit SHA-256 checksum, using three process-bounded attempts
with an exact remote-identity check between attempts, then read back with
`head-object --checksum-mode ENABLED` and compared to the manifest before it
is counted; local success is never treated as remote proof. `bash
infra/deploy.sh preflight` and CI both run the gate without a store, which
checks the manifest, the citation contract, and the privacy and lifecycle
invariants but proves nothing about uploaded objects.

Publishing the corpus does not enable a state, satisfy a DONE gate, or make
any document a title or grade conclusion.

## Fail-closed conditions

- a staged file missing, resized, or rehashing differently from its pinned inventory;
- a production build missing the canonical Part A harvest manifest;
- one normalized source URL mapping to a different harvested and addendum raw SHA;
- any asset occurrence lacking affirmative public-domain, non-paywalled rights;
- an asset/source/mine link with a dangling or cross-document reference;
- a page count differing from the pinned inventory;
- shared bytes registered under more than one key path with no declared filing row;
- an OCR pass that changes pagination, adds no readable text, or returns the original bytes;
- a manifest whose metrics do not reconcile with its own rows;
- a manifest that is not the canonical encoding of its own contents;
- an object key that does not derive from its row's state, portal, mine_id, and doc_id;
- a `doc_id` that is not the SHA-256 of the raw original;
- a citation past the end of its document, or filed against a subject no document declares;
- a located quote claimed for a document with no text layer;
- a manifest declaring the corpus public, or a presign TTL over 3600 seconds;
- `docs/*` appearing in the CloudFront bucket-policy allowlist;
- `viewer.html` missing from that allowlist;
- a deployed lifecycle that disagrees with the manifest's declared retention;
- a store object missing, resized, or rehashing differently from the manifest.

## Current real-data blockers

The corpus is 25 documents, not the whole cited literature. Four registered
rows are withheld pending a rights review, as Rights above records; until
that review lands, their citations resolve to nothing and the affected mines
keep their portal links. Four staged PDFs (`usgs-b723`, `usgs-pp104`,
`usgs-pp431`, `ugs-ss44`) sit in the cache but are pinned by no grade-source
inventory and are therefore not admitted; they need an inventory row first.
The current generation records 16 documents as `ocr_added` and nine as
native-text. Mixed PDFs are probed page-by-page, so a cover or plate with no
text is no longer hidden by a different page's native text layer. The 61
unlocated citations are quotes whose text-layer match failed — degraded scan
OCR, or a printed page whose reviewed number does not match the PDF index;
each opens its cited page without a highlight and is counted honestly in
`metrics.citations_quote_located`. Idaho has no reviewed grade evidence, so
its stored documents carry reviewed WS12 citations instead. Delivery is also
gated: `ENABLE_LEGACY_DOC_STORE` defaults to false, so an ordinary deploy
ships neither the viewer nor the corpus. Neither a cached PDF nor an uploaded
object is a substitute for the publisher's record, and the store does not
claim reuse rights beyond each document's recorded basis.

## Verification

```bash
python3 ci/run_tests.py
```

The tests are entirely local, open no socket, and need neither the private
PDF cache nor a PDF toolchain: `tests/test_ws12_doc_store.py` assembles
minimal PDFs by hand and injects the probe and OCR boundaries, covering the
key scheme, doc_id identity, source drift, page-count drift, shared-byte
filing, the OCR guards, quote location, manifest canonicalisation and metric
reconciliation, store hash verification, and the delivery gate. Its portal
death case blocks `socket` outright and asserts every citation still resolves
to a stored key with identical results after the portal URLs are replaced
with dead ones. `tests/test_ws12_citation_viewer.py` drives
`infra/docs_lambda.py` against an in-memory S3 and Cognito to prove that an
unauthenticated caller, an expired session, an unknown document, an
out-of-range page, and an unknown variant all get no signature at all, and
checks the shipped viewer's vendored assets, fragment parameters, and mobile
contract. `npm run test:doc-viewer` additionally launches a real mobile
Chromium session against the actual IF0126 searchable PDF, requires byte-range
delivery and nonblank page art, verifies text-layer highlighting, and blocks
both Idaho publisher host variants to prove portal-death behavior.
