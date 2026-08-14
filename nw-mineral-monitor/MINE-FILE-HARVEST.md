# WS12 mine-file harvest

Part A is a registry-driven, fail-closed document harvester. The registry is
`portals/*.yaml`; `pipelines/portal_registry.py` validates all 33 named state
and federal entries. Every portal records its type, verified entry URL,
detail-page and PDF patterns (including explicit `null` findings), identifier
scheme, probe result, access status, and an unasserted harvest state. The
registry deliberately distinguishes a harvestable source from an index-only,
manual-request, publication-only, blocked, or probed-no-attachment source.

The executable adapters currently cover the three largest verified machine
patterns:

- IGS: a bounded 20-prefix legacy IGSID sweep plus the official current
  ArcGIS Hub dataset metadata. Hub reports 9,424 records and no native
  attachments. The underlying feature-service host currently denies robots
  verification, so exact current IGSID membership/external popup links remain
  an explicit completion blocker; they are never inferred by relabeling a
  legacy detail-page fetch. The diff records that source status alongside the
  harvested legacy hashes.
- AZGS: every page of the public `collection_group=ADMM` metadata API, with
  named collection-file URLs and per-collection privacy/license checks.
- NBMG: the complete `mddata` inventory embedded in the retired Mining
  District Files search page and its direct static PDF paths. The current
  ResourceSpace library remains a reconciliation target because its
  `robots.txt` requires a 10-second delay and disallows `/filestore`.

`harvest_ready` means an adapter, public endpoint, robots policy, and terms
review exist. It does **not** mean a full crawl has been completed. All
committed `harvest_state.full_crawl_complete` values remain false. The runtime
coverage output is the only place a future exhaustive run can carry a
completed cursor and manifest evidence.

## Rights and access gate

The scope is strict public-domain material, not merely material that can be
opened in a browser. A document task is created only when a portal/file rule
provides an affirmative public-domain basis. Every manifest row therefore has
the literal values `public_domain: true` and `paywalled: false`, plus a
nonempty `rights_basis`. Unknown rights, Creative Commons licenses, corporate
property files, purchase-only items, authentication responses, and detected
paywalls are skipped and retained in the SQLite `document_candidates` audit.

This matters for the IGS acceptance record. IF0126 exposes three links:

- `MILS-160230014.pdf` — eligible USGS federal work;
- `MRDS-W015681.pdf` — eligible USGS federal work;
- `IF0131_001.pdf` — a shared/secondary MineDocs property file with no
  established public-domain basis, logged as `rights_unverified` and skipped.

The crawler checks `robots.txt` before every URL, uses its crawl delay when it
is stricter than the registry throttle, retries transient responses through
the durable queue, and fails closed if robots policy cannot be verified.
Executable portals also require a dated terms review finding no automated
download prohibition. Access controls are never bypassed.

## Storage and manifest contract

The SQLite queue, JSONL manifest, coverage JSON, and diff JSON are metadata
only and live below the gitignored `var/ws12/` runtime directory. PDF bytes
are held only in a bounded spooled temporary file while hashing and upload;
the durable original goes directly to S3:

```text
ws12/originals/{portal_id}/{sha256[0:2]}/{sha256}.pdf
```

The uploader uses `boto3` when available and otherwise the AWS CLI. Hashes are
global in the queue database, so the same bytes discovered under several
mines or source variants upload once while retaining every provenance row.
An incremental refresh resets catalog/detail tasks but never resets completed
document tasks; newly discovered URLs alone enter the download frontier.

Each `manifest.jsonl` row has exactly these fields:

```text
schema_version, source_url, portal_id, portal_source, mine_id, mine_name,
state, county, trs, document_title, doc_date, doc_type, sha256, bytes,
retrieval_date, content_type, s3_uri, etag, last_modified, public_domain,
rights_basis, paywalled
```

The skipped-candidate audit remains in `queue.sqlite3`; it is intentionally
not mislabeled as a downloaded-file manifest row.

### Canonical document lineage

`var/ws12/manifest.jsonl` is the authoritative byte/provenance input for both
downstream document products. `document_index.py` ingests it directly;
`document_assets.py` normalizes the same rows for the citation-PDF store. The
normalized private catalog keeps three separate relations:

- `assets`: one row per raw SHA-256, with raw/searchable key and readiness;
- `source_occurrences`: every portal/source-URL observation of those bytes;
- `mine_links`: portal IDs and internal mine IDs linked to an asset without
  pretending that either identifier is the document identity.

The current addendum store still contains 23 pilot documents not yet produced
by a Part A adapter. They remain explicit `legacy_pilot` occurrences. Its two
IF0126 assets, however, are enriched from harvested rows and never from older
hand-maintained URL declarations. A normalized `www`/non-`www` source URL
resolving to a different store SHA is stale lineage and fails the build.

## Commands

Validate every named portal, including all WS11 phase-2 probes and the
requested MI/MO/PHUMMIS/VA/NC/SC/GA probes:

```bash
python3 pipelines/portal_registry.py
```

Run the narrowly scoped acceptance crawl. A targeted crawl can never mark a
full portal cursor complete:

```bash
python3 pipelines/mine_file_harvest.py crawl \
  --portal igs_mines --mine-id IF0126 \
  --queue var/ws12/queue.sqlite3 \
  --manifest var/ws12/manifest.jsonl \
  --coverage var/ws12/coverage.json \
  --diff-dir var/ws12/diffs \
  --s3-bucket "$WS12_S3_BUCKET" \
  --s3-prefix ws12/originals
```

Run every currently executable full adapter, or replace `all` with a single
portal ID. Omit `--max-tasks` for an exhaustive cursor:

```bash
python3 pipelines/mine_file_harvest.py crawl \
  --portal all --refresh \
  --queue var/ws12/queue.sqlite3 \
  --manifest var/ws12/manifest.jsonl \
  --coverage var/ws12/coverage.json \
  --diff-dir var/ws12/diffs \
  --s3-bucket "$WS12_S3_BUCKET" \
  --s3-prefix ws12/originals
```

Re-export metadata without making network or S3 calls:

```bash
python3 pipelines/mine_file_harvest.py export \
  --queue var/ws12/queue.sqlite3 \
  --manifest var/ws12/manifest.jsonl \
  --coverage var/ws12/coverage.json \
  --diff-dir var/ws12/diffs
```

Build or inspect the metadata-only private lineage catalog (the output is
gitignored; no PDF bytes or local cache paths are written):

```bash
python3 pipelines/document_assets.py \
  --harvest-manifest var/ws12/manifest.jsonl \
  --document-store-manifest var/ws12/document-store-manifest.json \
  --output var/ws12/document-assets.json
```

## Completion and coverage semantics

Coverage emits one row for every registry portal, even when no crawler is
enabled. Its keys include `crawl_started`, `counts_status`, nullable document
counts, `crawl_complete`, `cursor_exhausted`, `crawl_scope`, `completed_at`,
`manifest_rows`, `unique_hashes`, and `manifest_rows_sha256`.

Never-started executable portals have `counts_status: not_started` and null
counts. An explicit registry finding that a portal has no public documents
may report zero with `counts_status: registry_established_no_attachments` but
still does not claim a completed crawl. A run becomes complete only after the
full seeded frontier, including its final API/page cursor, has no pending,
active, or error task. Targeted runs and nonzero document counts cannot set
that badge.

The corrected live IF0126 run on 2026-08-14 produced two legacy provenance
rows for two eligible USGS documents and two unique S3 originals. Current
ArcGIS Hub metadata was probed separately; it reports no native attachments,
while current feature membership/external links remain robots-blocked:

```text
ws12/originals/igs_mines/d2/d29aab7b4e9fcde0e084dddc84ef9da37d0c15860af4674bf58bd0decd71e07f.pdf  110833 bytes
ws12/originals/igs_mines/3c/3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1.pdf  110393 bytes
```

S3 `HEAD` verified both objects' byte length, `application/pdf` content type,
and stored SHA-256 metadata. Coverage truthfully remains
`crawl_scope: targeted:IF0126`, `cursor_exhausted: false`, and
`crawl_complete: false`, with `completion_blocker.reason:
robots_denied_http_403`. No claim is made that three Tier-1 portals have
already completed their full production crawls.
