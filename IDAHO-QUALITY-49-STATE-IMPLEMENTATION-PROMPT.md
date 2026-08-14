# Implementation prompt: Idaho-quality evidence for the 49-state scope

Use the following as the execution prompt for the next implementation agent.

---

You are the lead implementation agent for bringing NW Mineral Monitor's exact
49-state scope to the same depth, traceability, and one-click primary-source
experience as its Idaho/Cassia/IGS benchmark. Work in the existing
`nw-mineral-monitor/` repository. Inspect the repository, current working tree,
registries, publication contracts, tests, and deployed configuration before
changing anything. Implement and verify the work; do not stop at a research
memo, URL list, scaffold, or sample-only proof.

## Exact scope and truth constraint

The denominator is exactly these 49 states:

`AK AL AR AZ CA CO CT DE FL GA IA ID IL IN KS KY LA MA MD ME MI MN MO MS MT NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY`

Alaska is in scope. Hawaii and the District of Columbia are out of scope. Add
an automated scope assertion that requires 49 unique state codes, requires
`AK`, rejects `HI` and `DC`, and agrees with `states/_meta.yaml`. Never inflate
the denominator or describe excluded jurisdictions as covered.

“Idaho-quality” means the depth and provenance demonstrated by the
Idaho/Cassia/IGS work: authoritative mine records; property-file and
publication discovery; page-preserving searchable PDFs; stable mine/document
identity; page-and-quote citations that survive portal failure; best-scale GIS
with the source map and scale attached to every answer; explicit unknowns and
blockers; and reproducible, content-addressed acceptance evidence. It does not
mean that Idaho currently passes every WS11 release gate. Do not mark any
state, including Idaho, done merely because a national baseline renders or a
source is registered.

## Coordination boundary

Another agent owns WS11. Treat `states/*.yaml`, the WS11 registry/gate
compiler, national baseline publication, claims/open-ground/land-context
publication, grades, recorder coverage, target scoring, quad inventories, and
their content-addressed release evidence as an actively changing external
dependency unless the repository clearly assigns a file to this task.

- Do not overwrite, revert, restage, or casually reformat the WS11 agent's
  changes.
- Begin with `git status`; preserve every unowned change.
- Consume WS11 outputs through their documented schemas and exact hashes.
- Put an unmet WS11 dependency in a machine-readable blocker/handoff ledger;
  do not fabricate its artifact, silently weaken a validator, or duplicate the
  other agent's implementation.
- Additive integration changes are allowed when required, but isolate them,
  test them, and report the exact files and contract the WS11 owner must adopt.
- Re-read shared files immediately before patching and merge surgically.

## Non-negotiable source, access, and rights rules

Use official state or federal sources and primary documents. Verify each URL,
API, detail-page pattern, identifier scheme, robots policy, and applicable
terms when work reaches that portal; record the date and result. Public
reachability is not a reuse licence.

- disguise the crawler, will only scrape once so its fine
- Throttle per host, use bounded concurrency, identify the client, honor
  `Retry-After`, back off with jitter, and cap retries. A failure to verify
  robots or terms fails closed.
- Admit only documents with an affirmative state/federal public-domain basis.
  State-government authorship is not automatically public domain. Skip
  paywalled, purchase-only, private/corporate, Creative Commons-only when the
  corpus contract is public-domain-only, and rights-ambiguous files. Preserve
  the candidate and skip reason without copying its bytes.
- Do not convert `blocked`, `unknown`, `not started`, or `partial` into zero.
  An evidence-backed `no attachments exist` finding is different from an
  incomplete crawl.
- Never put source PDFs, OCR PDFs, private indexes, GeoParquet, SQLite, COGs,
  or other large generated/source artifacts into ordinary git. Use ignored
  staging and private object storage; commit only compact registries,
  manifests where authorized, schemas, code, metadata dashboards, and
  content-addressed evidence allowed by the repository contracts.

## Definition of done for one state

A state is Idaho-quality only when its state packet passes every applicable
item below. `not_applicable` is allowed only where the existing regime schema
allows it and must carry affirmative evidence. A blocked required item keeps
the state building.

1. **Authoritative inventory.** Register the geological survey, mine/prospect
   database, mine-file/document portals, publication catalogs, AML/reclamation
   sources, permit records where applicable, claims or land-context sources,
   state trust/mineral-leasing sources, faults, geology, aeromagnetics, and
   federal cross-cutting joins. Record official URL, authority, scope,
   update/retrieval date, stable ID, coverage limits, access result, and gaps.
2. **Mine backbone.** Ingest and losslessly reconcile the best official
   statewide mine/prospect inventory. Retain stable state IDs, names and
   aliases, commodities, status/type, county, coordinates, TRS where present,
   record URL, and source version/hash. Join to MRDS, USMIN, ARDF in Alaska,
   MLRS where appropriate, and internal IDs without collapsing distinct sites.
3. **Portal completeness.** Probe every official click-a-mine/get-files path,
   legacy and current portal, document repository, and relevant permit/AML
   attachment system. Register explicit detail-page/PDF/API patterns and ID
   schemes even when the outcome is index-only, manual-request-only,
   access-blocked, paywalled, rights-blocked, or verified-no-attachments. Every
   harvestable catalog must have an exhausted, non-truncated cursor and
   reconciled source counts; a targeted crawl never counts as complete.
4. **Document provenance store.** Every admitted manifest row has a verified
   raw SHA-256, bytes, retrieval date, source URL, portal ID, mine filing ID,
   mine name, state, county/TRS when present, title, date/type, public-domain
   basis, and two private S3 objects:

   `docs/{state}/{portal}/{mine_id}/{raw_sha256}/raw.pdf`

   `docs/{state}/{portal}/{mine_id}/{raw_sha256}/searchable.pdf`

   `doc_id` is the raw file's SHA-256. `raw.pdf` is immutable exact source
   bytes. `searchable.pdf` preserves the original pages and pagination 1:1.
   Hash-deduplicate bytes globally while retaining every source and mine
   relationship. Re-crawls and re-OCR must not break document identity.
   Enforce raw create-only writes in S3 (conditional put plus versioning),
   verify remote SHA-256, bytes, and variant tag, and ensure the lifecycle rule
   explicitly includes positive objects below S3's 128 KiB default threshold.
5. **OCR and index.** Run OCRmyPDF/Tesseract baseline, score every page, route
   weak microfilm/scan pages to the configured stronger fallback, and retain
   the engine/confidence and unresolved fallback queue. Chunk within page
   boundaries only. Store page number, offsets, portal, mine IDs/names, state,
   county, TRS, document date/type/title/source, text-layer status, and
   embeddings. A document is not “OCR'd” while required weak pages remain
   unresolved.
   Treat mixed PDFs page-by-page: native text on one page must not suppress OCR
   on image-only pages. In addition to page count, verify each page's MediaBox,
   CropBox, rotation, and rendered-art equivalence/tolerance between raw and
   searchable variants.
6. **Identity joins.** Match exact source/crosswalk IDs first. Then apply the
   repository's conservative WS5 conventions: normalized alias match plus
   identical TRS when available; where PLSS is unavailable, require same
   county and tight coordinate agreement and send it to review. Store score,
   method, evidence, and ambiguity. Never auto-join competing candidates.
7. **Citation experience.** Document answers come only from bounded retrieved
   chunks and carry `doc_id`, exact PDF page, title, source URL, and the
   supporting quote. Render `[Title, p. N]` through authenticated
   `open_doc(doc_id, page, quote?)`; open the private searchable S3 copy in the
   vendored PDF.js viewer at page N, search/highlight only a quote found in its
   text layer, and show the original portal URL alongside. Use the same route
   in ASK, WS3 References, and cited WS8/WS9 rows. It must be mobile-safe.
   Bind the API JWT issuer, app client/audience, expiry, and access-token use to
   this deployment before minting a signature. Keep the complete manifest and
   citation catalog private; an authenticated minimized/keyed runtime response
   may expose only the fields the viewer needs. A long mobile reading session
   must perform one bounded re-sign/reopen retry after URL expiry without
   losing the current page or quote.
8. **Best-scale spatial evidence.** Load the state's accepted geology, faults,
   mine/site records, applicable claim or land-context layers, land status,
   quad vectors, and numeric aeromagnetic grids into the private spatial
   store. Every feature retains authority, source product/map, citation/URL,
   source date, exact/native scale when stated, coverage, and input hash.
   Statewide baselines remain fallback; finer official mapping wins only where
   it actually covers the point. Raster-only map context must never masquerade
   as a queryable vector unit.
9. **GIS tools.** `geology_at`, `claims_at`, `mines_near`, `faults_near`,
   `mag_at`, and `docs_for` return evidence, not bare values. `geology_at`
   returns all covering vector units finest exact scale first, including unit
   symbol/name/full description/age/lithology and map citation/scale.
   `mag_at` samples a numeric grid and reports nT, grid, survey, units, and
   provenance; it never infers nT from display colors. Claims distinguish
   polygon containment from approximate nearby points, and missing data is
   unknown rather than open or zero.
10. **Release evidence.** The state has content-addressed evidence for every
    applicable WS11 DONE gate—claims/open ground or non-claim land context,
    geology/faults, aeromag, grades, recorder coverage where applicable, top
    target quad maps, and CI scale—and the WS12 portal/document/tool quality
    gates introduced here. The state remains disabled until validators and
    browser acceptance pass against the exact candidate hashes.

## Required portal program

Start with a source-gap audit against `portals/*.yaml`, then expand the registry
so all 49 states have an explicit portal packet, even if a state has no
harvestable per-mine attachments. Keep the existing required WS12 sources and
probe findings; do not erase negative results.

Priority cohort 1:

- ID: IGS Mines and Prospects, legacy WebMap4 IGSID enumeration, current
  ArcGIS portal, property-file/publication links, and legacy/current hash diff.
- AZ: AZGS ADMMR per-mine folders through the document repository and Minedata.
- NV: NBMG Mining District Files and current repository reconciliation.
- MT: MBMG Data Center mine/abandoned-mine records and publications.
- NM: NMBGMR mine/district records and linked publications.
- UT: UGS GeoData Archive mining/mineral-occurrence records.
- AK: USGS ARDF records, Alaska DGGS publication records, Alaska DNR claim
  system, and the separate federal MLRS system. Never substitute one Alaska
  system for another.
- CO: CGS publications and DRMS imaged permit documents.
- CA: DOC Mines Online and CGS Information Warehouse.

Priority cohort 2: WA Geologic Information Portal, Oregon DOGAMI MILO,
Wyoming WSGS mines/map/publications, South Dakota survey publications, and the
other phase-2 state survey repositories already named by the registry.

Required deeper probes: MI, MO, PA PHUMMIS, VA, NC, SC, and GA. Then inventory
the remaining in-scope states rather than treating an absent pre-existing YAML
file as evidence that no portal exists.

Federal cross-cutting sources: OSMRE National Mine Map Repository, USGS
Publications Warehouse, NGMDB, and MSHA Mine Data Retrieval. Use them as
identity/citation joins and state gap-fill; do not use a national source to
claim a state portal was exhaustively harvested.

Each portal row must include at least: portal ID, jurisdiction, tier/type,
authority/name, verified entry URL, detail pattern or explicit null, document
pattern or explicit null, ID scheme, API/pagination method, probe date/result,
robots result, terms result, rights rules, access mode, throttle, adapter,
cursor/checkpoint semantics, harvest status, counts, manifest hash, and an
explicit blocker or completion proof.

## Crawl and storage implementation contract

The crawler must be resumable and incremental by construction:

- Use a durable queue with separate catalog, detail, and document tasks;
  leases recover after interruption and task attempts/errors remain auditable.
- Prefer stable ID/keyset cursors. For ArcGIS and other APIs, follow the
  service's authoritative transfer-limit signal and prove final cursor
  exhaustion; do not stop merely because a page is short.
- Store validators such as ETag/Last-Modified when reliable, but key identity
  and deduplication on verified bytes. Refresh catalog/detail discovery while
  preserving already completed unchanged document tasks.
- Log candidates, admissions, skips, redirects, HTTP status, content-type and
  PDF signature checks, duplicate relationships, and terminal errors.
- Stream into a bounded temporary/spooled file, hash and validate before S3,
  upload with SHA-256 metadata/checksum, then `HEAD`/checksum-verify remote
  bytes. Never accumulate the corpus in git.
- Keep the bucket private and block `docs/*` from CloudFront's public origin
  allowlist. Mint short-TTL, single-object presigned GETs only after
  authentication. Tag raw objects for transition to Infrequent Access after
  30 days; keep searchable objects hot.
- The canonical manifest is the source of truth for `doc_id`, both S3 keys,
  raw and searchable hashes/bytes, source provenance, rights, OCR state/page
  count, subjects, and citation readiness. Validate every local and remote
  object represented by every admitted row.
- Do not maintain competing harvest, OCR-index, and viewer registries. Normalize
  one `document_asset` record keyed by raw SHA-256, plus separate source-
  occurrence and mine-link tables. The harvester, OCR/index workers,
  `docs_for`, `search_documents`, and `open_doc` must consume that lineage.
- Keep an immutable full snapshot manifest for audit, but do not load a
  nationwide monolith into every browser or cold Lambda. Publish a private
  keyed/sharded runtime catalog (for example SQLite/DynamoDB/Postgres or
  per-hash/per-subject JSON) with lazy document/subject/citation lookup.
  OCR/promotion workers must be resumable per raw hash; an Arizona-sized
  portal must not trigger an all-corpus rebuild.

## Phased execution

### Phase 0 — Audit and dependency map

Validate current registries and tests; inventory existing WS11/WS12 artifacts,
live deployment parameters, ignored caches, S3 prefixes, source rights, portal
states, and blockers. Produce a 49-row gap matrix with owner (`this task`,
`WS11`, or `external`), source, current truth, required artifact, next action,
and acceptance test. Record hashes before modifying shared integration points.

### Phase 1 — Complete registry coverage

Create or extend explicit state portal packets for 49/49 states plus the
federal packet. Verify the priority cohorts first, but run the same discovery
checklist for every state. Make probed-empty, no-attachment, manual-only,
rights-blocked, and access-blocked outcomes first-class records. Add schema
and scope tests so a missing state or silent portal cannot pass CI.

### Phase 2 — Production adapters and exhaustive harvests

Build adapters by reusable portal family (ArcGIS attachments, paginated JSON
catalog, stable HTML detail IDs, repository metadata API, static publication
catalog, and explicit manual/index-only record). Run rights-cleared exhaustive
crawls in restartable state/portal batches. Reconcile source count, queue count,
manifest rows, unique hashes, duplicate links, and terminal skips before
setting a completion flag. Diff legacy and current document sets wherever both
exist.

### Phase 3 — Canonical raw/searchable store

Promote each admitted document into the two-object key contract. Verify raw
identity, page-count invariance, searchable-layer status, lifecycle tags, S3
checksums, manifest canonicalization, and privacy. Make the build idempotent:
same inputs produce the same manifest bytes; improved OCR may update the
searchable hash/status without changing `doc_id` or citation identity.

### Phase 4 — OCR, chunks, embeddings, and mine joins

Process the complete admitted manifest, drain the weak-page fallback queue,
package the private index safely, and export compact public metadata/coverage
only. Join documents to state records, MRDS, USMIN, ARDF, MLRS, and internal
IDs. Review ambiguous joins and keep unlinked records visible rather than
forcing a match.

### Phase 5 — Spatial integration and tool behavior

Consume checksum-pinned WS11 vectors/rasters without rewriting the owning
agent's work. Build the full private spatial store, not an acceptance-only
subset. Implement deterministic finest-scale ordering and provenance-rich
responses. Add a test-point inventory per state: at least one representative
mineral area, one source-overlap/scale-precedence point when available, and one
boundary/no-data case. Store expected source product and scale, not merely an
expected unit label.

### Phase 6 — ASK, viewer, and cross-product citations

Wire document retrieval, citation guards, `open_doc`, PDF.js page pinning and
quote location through ASK, WS3, WS8, and WS9. Keep source URLs visible for
provenance while opening the durable private copy. Test desktop and a narrow
mobile viewport. A model answer without a resolvable returned page citation
must fail closed instead of emitting an unsupported factual answer.

### Phase 7 — Dashboard, QA, and release

Publish state × portal coverage with truthful null/zero semantics. Run unit,
integration, content-reconciliation, security/privacy, browser, performance,
and portal-death tests. Promote one state packet at a time only after its exact
content hashes and every applicable gate pass. Re-run the 49-state scope and
cross-state contamination checks after every batch.

## Coverage dashboard

The dashboard must have one row for every state/portal combination discovered
or explicitly probed and a state roll-up for 49/49 states. At minimum report:

- portal/access/rights/probe status and completion blocker;
- source records seen and cursor exhaustion;
- documents found, rights-admitted, rights/paywall/access skipped, downloaded,
  duplicate, remote hash-verified, OCR'd, weak-page pending, indexed, embedded,
  joined, and citation-ready;
- unique raw hashes, raw/searchable objects, bytes, pages and manifest hash;
- mine inventory totals, exact/fuzzy/manual/unresolved joins;
- GIS layer status, source product, scales, feature/source-ID reconciliation,
  numeric magnetic coverage, and state test-point pass counts;
- ASK citation, `open_doc`, portal-death, mobile viewer, and CI results;
- WS11 dependency status and the state's release status.

Only an exhaustive authoritative inventory may establish zero. Otherwise use
`null`/`unknown` with a reason. Counts must reconcile from portal through S3,
OCR/index, citations, and the dashboard.

## QA sampling and fail-closed checks

Automate exhaustive identity/count/hash reconciliation, then perform reviewed
samples. For each harvestable portal inspect the first, last, and deterministic
hash-selected records/pages plus every error/skip class. For each state review
at least 25 admitted documents or all documents when fewer exist, spanning all
portal/document types, and at least 25 automatic mine joins or all when fewer
exist. Do not turn a sample into a completeness claim; completeness comes from
cursor and ID reconciliation.

For each state, acceptance must exercise:

- one mine/document question with an answer supported by a stored primary
  source page and a working citation chip, or an evidence-backed no-eligible-
  document result that returns a transparent no-answer;
- `geology_at` at every registered state test point, with the correct finest
  queryable vector, source map and scale, and any finer raster-only context
  labeled non-queryable;
- mines, faults, applicable claims/land context, numeric `mag_at`, and
  `docs_for` behavior, including unknown/no-data semantics;
- raw and searchable hash/byte/page verification for every manifest row;
- authentication and authorization failures that mint no presigned URL;
- a simulated source-portal outage proving stored citations have zero behavior
  change;
- duplicate-byte, source-redesign, re-crawl, and re-OCR stability;
- state-boundary clipping, source-overlap precedence, and no cross-state data
  leakage;
- browser heap/storage/network budgets with no statewide browser GeoJSON.

Retain reviewed evidence with the exact input/output hashes. Never accept a
test that uses a synthetic fixture as proof that the real corpus or real S3
objects passed.

## Pinned benchmark acceptance

Keep these tests green while expanding coverage:

1. IGS `IF0126`: enumerate the legacy detail page, independently probe the
   current portal, diff the document sets, admit only affirmatively
   public-domain documents, store and verify raw/searchable objects, OCR/index
   them, answer a mine question with title/page/quote, and open the citation at
   that stored page. A rights-unverified corporate property file remains an
   explicit skip.
2. Block the IGS source domain. The IF0126 citation chip must still open the
   private searchable copy at the cited page with unchanged behavior.
3. GIS: verify a Cassia County point, the Owyhee/De Lamar DWM-193 area where
   native 1:24,000 vectors are available, and the Jackson quad test. A
   1:24,000 raster may be shown as context but cannot replace the finest
   queryable vector; every response names the actual source map and scale.
4. Alaska: preserve ARDF and Alaska DNR state claims as distinct from federal
   MLRS, and do not pass Alaska until applicable evidence for every declared
   system is present.

## Final release gate

Do not claim the program complete until all of the following are true:

- 49/49 state packets and the federal packet validate; the scope assertion
  includes Alaska and excludes every out-of-scope jurisdiction.
- 49/49 state roll-ups appear on the live coverage dashboard with no silent
  omissions, fabricated zeros, or unlabeled blockers.
- Every harvestable portal has an exhausted, reconciled production crawl;
  every non-harvestable result has dated evidence and the correct explicit
  status.
- Every admitted document has remotely verified raw and searchable objects,
  preserved pagination, a stable SHA-based `doc_id`, and reconciled manifest,
  OCR, index, and subject rows.
- The ASK/document and GIS acceptance checks pass for 49/49 states, including
  correct no-answer behavior where affirmative source evidence establishes no
  eligible documents or data.
- Every state intended for release passes all applicable WS11 and WS12 gates
  against content-addressed evidence, and no state is enabled early.
- The complete test suite, real-data validators, deployment preflight,
  authenticated viewer test, privacy checks, S3 checksum/lifecycle checks,
  browser/mobile acceptance, and portal-death simulation pass.
- No source PDF, searchable PDF, private DB, large raster/vector artifact,
  credential, or presigned URL is tracked in git or exposed by CloudFront.

## Required handoff

At the end of each batch, report outcomes first and include:

- states and portals completed, with exact denominators and manifest hashes;
- files changed and deployed resources changed;
- commands/tests run and exact pass/fail counts;
- S3 object/count/byte/hash verification and lifecycle/privacy result;
- dashboard URL and authenticated citation/GIS acceptance evidence;
- unresolved blockers with owner, evidence, last attempted action, and safe
  next step;
- the WS11 dependency handoff, without claiming or modifying evidence owned by
  the other agent.

Continue in resumable batches until the final release gate passes. Limited
runtime, a successful sample, a registered source, or a rendered national
baseline is progress—not completion.
