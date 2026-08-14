# WS12 document OCR and ASK index

WS12 Part B consumes the Part A JSONL manifest and produces a private,
page-anchored SQLite search/vector index. Original PDFs, searchable derivatives,
page text, and embeddings stay outside git. The only browser artifacts are
`site/data/docs/index.json` (document metadata and mine-to-document IDs) and
`site/data/docs/coverage.json` (portal stage counts and crawl state).

## Contracts and safety

Each manifest row follows `pipelines/config/document_manifest.schema.json`.
The ingest gate requires the crawler's exact `sha256`, `bytes`, `s3_uri`, source
URL, portal/mine identity, and retrieval date. Rights fail closed:
`public_domain` must be literal `true`, `paywalled` literal `false`, and
`rights_basis` non-empty. A government portal hosting a private corporate file
does not by itself make that file public domain.

Documents deduplicate on raw SHA-256 while every portal/source URL remains in
`document_sources`. The live build uses SQLite WAL for crash-safe resumability.
Never upload that live file directly: the `package` command checkpoints it,
backs it up into a one-file DELETE-journal artifact, runs integrity and foreign
key checks, reopens it in immutable mode, and emits exact SHA/byte metadata.

The citation-store side consumes the same Part A rows through
`pipelines/document_assets.py`. Its private `var/ws12/document-assets.json`
expresses the equivalent normalized model as `assets`, `source_occurrences`,
and `mine_links`, then enriches it with raw/searchable store keys. This is a
lineage bridge, not a second crawler: for IF0126 the harvested URL, portal ID,
mine ID, S3 original, bytes, and raw SHA are authoritative. The older
25-document pilot remains available only for hashes that have no harvested
occurrence yet.

OCR prefers OCRmyPDF (which invokes Tesseract), then extracts each physical PDF
page independently with `pdftotext`. If OCRmyPDF is unavailable, existing text
is retained and weak pages are rendered at 300 dpi with `pdftoppm` and sent
directly to Tesseract. This repository installs no OCR binary. It also does not
need `pdfjs-dist`: Poppler/Tesseract are the production CLI boundary, while
unit tests inject OCR page text and need neither external binaries nor network.

Low-quality text is routed through the shell-free argv adapter configured by
`DOC_OCR_FALLBACK_COMMAND_JSON`; placeholders are `{input_pdf}`, `{page}`,
`{output}`, and `{work_dir}`. The output is UTF-8 text or JSON containing
`text`, `engine`, and optional 0..1 `confidence`. A page that still falls below
the threshold remains in `fallback_queue`; its document is not counted OCR'd.

Chunks never cross pages. Each stores page number, page offsets, portal/source,
mine IDs and names, state, county, TRS, document date/type, title, and source
URL. Production embeddings use Bedrock Titan Text Embeddings v2; the local hash
embedder is an explicit deterministic smoke-test model. Runtime search reports
`retrieval_mode` and `embedding_model` and hybrid-reranks bounded FTS candidates.

Identity joins use exact portal/MRDS/USMIN/our IDs first. Otherwise they reuse
the WS5 name score (60% sequence ratio, 40% token Jaccard) and require matching
TRS. Ambiguous matches remain candidates instead of becoming asserted links.

## Build and inspect

```bash
python3 pipelines/document_index.py --db var/ws12/document-index.sqlite3 \
  ingest --manifest var/ws12/manifest.jsonl

python3 pipelines/document_index.py --db var/ws12/document-index.sqlite3 \
  ocr --embedder bedrock

python3 pipelines/document_index.py --db var/ws12/document-index.sqlite3 \
  import-sites --columnar build-inputs/data/sites/mrds_id.json \
  build-inputs/data/sites/stategeo_id.json

python3 pipelines/document_index.py --db var/ws12/document-index.sqlite3 link

python3 pipelines/document_index.py --db var/ws12/document-index.sqlite3 \
  export --portal-status var/ws12/coverage.json

python3 pipelines/document_index.py --db var/ws12/document-index.sqlite3 \
  search "What production is reported?" --mine-id IF0126
```

The Part A coverage input keeps every registered portal, including probes with
no files. An incomplete crawl exports `found: null`, not a fabricated zero;
only a complete, exhausted crawl can establish zero. The resulting dashboard
separately reports found, downloaded, OCR'd, indexed, embedded, fallback
pending, errors, `crawl_complete`, and `cursor_exhausted`.

## Runtime and citations

Package/upload the private index explicitly:

```bash
./infra/deploy.sh upload-doc-index var/ws12/document-index.sqlite3
```

The command uploads only the verified package to
`private/ws12/document-index.sqlite3`, adds SHA-256 metadata, and checks remote
bytes and metadata after the PUT. The ASK Lambda has read access only to the
exact WS12 database keys. Its authenticated `localTool` mode routes GIS tools
to `spatial_tools` and `search_documents`/`docs_for` to `document_tools`.

`search_documents` returns at most 12 excerpts of at most 1,000 characters;
every hit contains document title, exact PDF page, and source URL. The model
must render `[document title, p. N](source_url)`. If it uses document search but
omits a resolvable returned citation, the relay withholds the answer and emits
a citation-guard notice instead.

Focused offline tests:

```bash
python3 -m unittest tests.test_document_index tests.test_document_ask_runtime -v
```
