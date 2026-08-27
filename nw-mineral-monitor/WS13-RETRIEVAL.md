# WS13 retrieval — implementation and operator runbook

Closes the gap the WS13 handoff describes: 852,027 embedded chunks sat in a private-VPC
Postgres while the ASK Lambda served a 3.2 MB SQLite file holding two documents. This
document records what is now in the repository, what has to be run against live AWS to
activate it, and what is deliberately still open.

Four steps HAVE been executed against production — the schema migrations and the provenance
backfill on 2026-08-25, then the worker-bundle upload and Phase A on 2026-08-27, all recorded
below. Everything else is a command for an operator to run deliberately; the code is written,
tested offline, and shipped dark.

This count is the first thing to update when something is run. It has been wrong once already
— it said "nothing has been executed against production" for a day after the migrations ran,
twenty lines above the section recording them.

## State verified against AWS on 2026-08-25 (read-only)

| Fact | Value | Why it matters |
|---|---|---|
| NAT gateways | 0 | a VPC-attached Lambda has no egress |
| VPC interface endpoints | 0 | and cannot reach Secrets Manager, S3 or Bedrock either |
| DB subnets | `subnet-05fbd5a24361ba3cb`, `subnet-0f09ac65853437382`, `subnet-0e1ea2386c106fda0` | all `MapPublicIpOnLaunch=true`, which is why EC2 reaches Bedrock and a Lambda ENI still will not |
| DB security group | `sg-05ebc8c61bfebe67b` | ingress tcp/5432 from `sg-0a0594b37a7d3087d` only — no SG change is needed or permitted |
| RDS `nwmm-ws13` | available, 16.14, `IAMDatabaseAuthentication=false` | the query Lambda takes a DSN from a CloudFormation dynamic reference, not `rds-db:connect` |
| ASG `ws13-workers` | desired 0, **zero scaling policies** | fixed below |
| CloudWatch alarms | 0 in the account | fixed below |
| `ws13-ocr-dlq` | 8 messages | nothing polls it; requeue with `ws13_enqueue.py` |

## What was built

### Phase A — the ANN index

- `pipelines/ws13_build_ann_index.py` — runs on the in-VPC host. Pauses the backfill only
  after classifying the process's parent (a cloud-init shell whose next line is
  `shutdown -h now` must die first; an unreadable ppid **refuses** rather than guessing),
  builds `ws13_chunks_titan_hnsw` over `titan_embedding::halfvec(1024)` with
  `halfvec_cosine_ops`, asserts the plan, and measures p95 at `ef_search` 40/100/200.
- `pipelines/ws13_index_contract.py` — the offline half of the same trap. It proves the
  `CREATE INDEX` expression and the query `ORDER BY` are byte-identical, that the opclass is
  `halfvec_cosine_ops`, that the operator is `<=>`, and that **both** sides are cast to
  `halfvec(1024)`. Casting only the column still parses, still returns the right rows, and
  silently abandons the index for a 852,027-row sequential scan.
- The gate probe is plain `EXPLAIN` (`EXPLAIN_SQL`). `EXPLAIN (ANALYZE, BUFFERS)`
  (`EXPLAIN_ANALYZE_SQL`) exists only for the operator's deliberate `--measure` run, because
  `ANALYZE` executes the statement — a gate that used it would *be* the scan it prevents.

### Phase B — the query path

- `infra/ws13_query_lambda.py` — thin, in-VPC, talks only to Postgres. It **cannot** call
  Bedrock, so it never embeds the query: the non-VPC ASK function embeds with
  `amazon.titan-embed-text-v2:0` (`dimensions=1024, normalize=true`) and passes the vector in.
  Costs $0 against $32.85/mo for NAT or $65.70 for endpoints.
- `infra/ws13_retrieval.yaml` — `Architectures: [x86_64]` declared explicitly, the three DB
  subnets, `sg-0a0594b37a7d3087d`, no `secretsmanager:GetSecretValue` at runtime.
- `op: "ping"` is the B2 network probe. Run it before anything else.
- The lexical arm ships **enabled**, the vector arm behind `VectorArmEnabled` (default
  `false`), and ASK keeps pointing at SQLite until `WS13_RETRIEVAL_ENABLED=true`. The ANN
  index is therefore a quality upgrade, not a launch dependency.

### Phase C — retrieval and citations

- `pipelines/ws13_migrations.sql` / `ws13_migrate.py` — the approved DDL, idempotent.
  `admission_class` is `GENERATED ALWAYS AS (split_part(s3_key,'/',2)) STORED`;
  `doc_year_min`/`doc_year_max` are **also generated**, over `IMMUTABLE`
  `ws13_doc_year_min/max(text)`, so a document indexed after the migration cannot end up with
  a parseable `doc_date` and NULL year bounds. `ws13_county_key(text)` normalises the county
  suffix on both sides. Over the 56,282 indexed documents the split is 11,507 suffixed /
  43,241 bare; the 15,581 / 51,685 quoted in the original handoff covers all 68,809
  manifest rows.
- Three columns the citation contract needs and `ws13_documents` did not have:
  `source_url`, `rights_basis`, `public_domain`. `pipelines/ws13_backfill_provenance.py`
  fills them for the existing 56,282 rows from the WS12 manifest, and refuses any document
  whose rights class disagrees across the manifest, the `s3_key` prefix and
  `admission_class`.
- Fusion is Reciprocal Rank Fusion, k=60, in both `ws13_query_lambda.py` and
  `infra/document_tools.py`. The old `0.75·vector + 0.25·lexical` scored a vector-less row at
  `-1.0`, which pushed a perfect keyword match below every embedded chunk.
- Over-fetch 200 on the halfvec index, then re-rank on the exact fp32 column.
- Citation resolver: `viewer_key` falls back to `s3_key` for all 27,294 born-digital
  documents. `admission_class` and `rights_basis` travel on every citation, with
  `attribution_required` / `non_commercial` / `share_alike` and a `rights_terms` string. An
  unknown `admission_class` **raises** rather than emitting a citation with unknown rights.

### Phase D — the mine-id bridge

`pipelines/ws13_mine_id_map.py` builds `ws13_mine_id_map(front_end_id, ws13_mine_id, method,
confidence, verified, evidence, updated_at)`. `mine_ids ILIKE 'stategeo%'` returns zero rows
across all 56,282 documents; the corpus namespaces are AZGS `ADMM-…` and bare IGS codes.
Tiers are exact embedded code, prefix namespace, then fuzzy name — an ambiguous match is
recorded as ambiguous and **left unmapped** rather than guessed, because the retrieval Lambda
now reads this table on the hot path and a wrong high-confidence row mis-attributes documents
to a mine.

### Phase E — proving it

`tests/fixtures/ws13_known_items.json` carries the one human-verified triple the repository
already had (IF0126, page 1, `LAVA CREEK DISTRICT`). `tests/test_ws13_known_items.py` asserts
fixture *integrity* offline and never skips; `require_complete()` gates cutover and refuses a
fixture in which too few items assert the vector arm — the one failure the gate exists to
catch. `tools/ws13_gen_known_items.py` proposes the remaining 24 from the live corpus for
human verification; `tools/ws13_live_known_items.py` is the live runner.

### AWS worker fleet updates

- **Per-page confidence was NULL for all 760,059 pages.** `pdftoppm` is not on `$PATH` in the
  ocrmypdf image, so every render exited 127 and tier-1 escalation had never fired once.
  `ws13_worker.py` now probes `pdftoppm` / `pdftocairo` / `gs` once per process (ghostscript
  is always present) and records an unmeasured page as **NULL, not `false`** — "we did not
  measure" must not read as "not weak".
- **A per-document budget.** Rendering makes documents much slower, and `MAX_DOC_SECONDS`
  bounded only each container, not the document — a document that outran the 3600 s SQS
  `VisibilityTimeout` was processed twice concurrently and eventually dead-lettered.
  `WS13_DOC_BUDGET_SECONDS` (7200 s) is a real deadline, with visibility heartbeats.
- **The fleet watcher had never fired.** It polled `pgrep -f ws13_worker.py`, which matched
  its own command line, so no node ever uploaded a log or self-terminated. Replaced with a
  node agent that waits on recorded PIDs *and* per-worker exit statuses, so it can tell "queue
  drained" from "every worker died at startup" — the latter now holds the node for diagnosis
  instead of letting 40 nodes boot, die and retire in a loop.
- **Self-shutdown no longer means self-replacement.** `InstanceInitiatedShutdownBehavior:
  terminate`, and the node leaves via `terminate-instance-in-auto-scaling-group
  --should-decrement-desired-capacity`.
- **A scale-in policy exists**, target-tracking on backlog per in-service instance counting
  **both** visible and not-visible messages, plus a lifecycle drain hook so scale-in never
  kills a node holding in-flight documents.
- **Alarms**, where the account had none: worker exit failures, DLQ depth, idle fleet. The
  node agent makes one cheap SQS call a minute so AWS/SQS metrics keep publishing — without
  it the idle alarm sits in `INSUFFICIENT_DATA` in exactly the state it exists to detect.
- `pipelines/ws13_reap_stale.py` reaps manifest rows stuck at `status='running'` (one,
  `5c991bfa4e90`, is in that state now) into `error`, where `ws13_enqueue.py --status error`
  can pick them up.

## Applied to production on 2026-08-25

Steps 1 and 2 have been run against `nwmm-ws13` via SSM on `i-0818521a8b3ff7c90`. Nothing
else has.

- **Migrations** — 20 statements in one transaction, all 27 `--check` assertions green.
  `admission_class`, `doc_year_min`, `doc_year_max` are STORED generated columns;
  `ws13_county_key` / `ws13_doc_year_min` / `ws13_doc_year_max` are IMMUTABLE;
  `ws13_reader` exists with SELECT on the four WS13 tables and **no** write privileges.
- **Provenance backfill** — the manifest streamed to 106,396 rows over 68,809 distinct
  sha256 with 0 malformed rows and **0 rights-class conflicts**; all 56,282 indexed
  documents matched and were updated. `source_url` coverage went 0% → **100%**, and
  licensed/research copies lacking a `rights_basis` went 45,325 → **0**.
- Measured after the fact: `research-copies` 32,312 / `licensed-copies` 13,013 /
  `originals` 10,957, matching the harvest record exactly. 20,918 documents now carry
  parseable year bounds (range 1808–2017); the other 35,364 have no parseable `doc_date`
  and are excluded from a year filter rather than silently treated as in range.
- `ws13_reader` is still **NOLOGIN**. It needs
  `ws13_migrate.py --apply --reader-secret-arn ...` on the in-VPC host before the retrieval
  Lambda can connect — deliberately deferred until that stack is deployed.
- The embedding backfill was left **running** (pid 255517). Phase A pauses it — see below;
  on 2026-08-27 that happened.

## Applied to production on 2026-08-27

Two more operations, making four in total.

- **The worker bundle was rebuilt and uploaded.** `sha256
  d9da7a342a405c8192938397bc08fef725d9b819690ce92665a623cbcb63b81d`, 21 members, 198,650
  bytes, downloaded back out of S3 and re-verified against these sources. What it replaced
  held **3 of 21 files** — recorded in step 0 above.
- **Phase A was run**, detached, on `i-0818521a8b3ff7c90`:
  `ws13_build_ann_index.py --pause-backfill --build --verify --measure --yes`.
  - The backfill is **stopped**: `SIGTERM → 255517`, confirmed gone. Which also stops Cohere,
    deliberately — that is the recorded decision, and pausing the process is the only clean
    control because the Cohere thread resets its own budget file at UTC midnight.
  - The parent check that this step exists to perform **passed cleanly and is worth
    recording**: `parent_kind: orphaned`, `ppid 1`, `reason: "parent is PID 1: the process is
    already orphaned, so no shell can walk on to a shutdown line"`. The trap the runbook warns
    about did not apply, because the backfill had already been reparented to init. The tool
    determined that itself rather than being told.
  - **The index is built and usable.** `create_seconds` **626.3**, `analyze_seconds` **49.3**,
    **2.15 GiB**, `valid=true`, over 848,032 rows with `maintenance_work_mem=3GB`,
    `max_parallel_maintenance_workers=2`, `statement_timeout=0`, against `db.m7g.large`
    (2 vCPU / 8 GB) holding a steady 5.05 GB FreeableMemory and 2 connections beforehand.
  - **`verify` passed**: `index_exists`, `index_usable`, `problems: []`, and all **4** probe
    shapes plan through `ws13_chunks_titan_hnsw` at `ef_search=200` — including the filtered
    `state=ID` shape, which flattens to a nested loop over the semi-join and is the shape that
    can silently lose the index.
  - **`measure`**, read against the 30 s API Gateway deadline, not the Lambda's 60 s:

    | `ef_search` | p50 | p95 | max |
    |---|---|---|---|
    | 40 | 51.5 ms | 57.9 ms | 57.9 ms |
    | 100 | 79.8 ms | 87.6 ms | 89.0 ms |
    | 200 | 127.1 ms | 137.6 ms | 139.1 ms |

    All three leave the lexical arm, RRF and citation assembly comfortable room inside 30 s.

**It does not run for hours.** The code said so in three places and printed it to the operator;
the first real run took **626.3 s**. A hardcoded guess, wrong by roughly 20×, in front of
someone deciding whether to keep a terminal open. Corrected to the measurement and the hardware
it was taken on — and note the reason `statement_timeout=0` matters was never the duration, it
is that a build cut off partway leaves an INVALID index that this script deliberately will not
drop.

**Run it detached anyway.** `AWS-RunShellScript` defaults to a 3600 s `executionTimeout`. At
626 s a synchronous invocation would in fact have fit — but nothing had measured that, the
failure mode if it did not fit was losing the whole run (SSM kills the command, the client
drops, Postgres cancels the `CREATE INDEX`), and the cost of detaching is one `nohup setsid`.
Keep detaching: the measurement above is one run, at these settings, on this instance class,
and a larger corpus or a smaller box moves it.

## Per-page confidence: a two-phase pass

`ws13_pages.confidence` is NULL for all 760,059 rows because `pdftoppm` is not on `$PATH` in
the ocrmypdf image and every render exited 127. Two corrections to how that gets fixed:

**The scope is 323,059 pages, not 760,059.** The other 437,000 pages belong to born-digital
documents carrying a publisher text layer; there is no OCR confidence to measure on them.

**Measuring and re-OCRing are different operations.** Measuring writes `ws13_pages` and
nothing else. Replacing OCR text changes `ws13_chunks.text`, which forces re-chunking, which
forces re-embedding through Titan, which invalidates those `ws13_chunks_titan_hnsw` entries.
So: measure everything first, then re-OCR only what measures weak — and that set is
unknowable until the measurement has run.

### Phase 1 — measure (`pipelines/ws13_confidence_pass.py`)

Renders each page of the stored `searchable.pdf` at 150 dpi and scores it with a tesseract
TSV. Tesseract rather than a "better" engine on purpose: the number being produced *is* a
tesseract word-confidence, and `CONF_THRESHOLD` 60 / `ESCALATE_THRESHOLD` 45 in
`ws13_worker.py` are calibrated to that scale. A different engine would produce a
differently-scaled score *and* replace the text, triggering the cascade above.

**A page that cannot be measured leaves the work set, one way or the other.** A terminal
reason (no image, no scored word, page absent, searchable PDF gone) leaves on its first
attempt. Everything else — an S3 timeout, a container timeout — is transient and re-admitted
on the next run, because a five-second network fault must not abandon a page for good, but
`ws13_conf_skips.attempts` counts and `MAX_TRANSIENT_ATTEMPTS` (5) ends it. Without that
counter a page that fails transiently on *every* sweep is deterministic in fact whatever it is
in classification: the shard never reached zero remaining, so the pass never reported done, so
the fleet wrapper swept the same unmeasurable pages for its whole 24 h ceiling. An exhausted
page is never counted as measured — it is reported as its own number in the run summary and by
`--verify-complete`, because "0 pages remaining" must not quietly absorb pages that were given
up on.

**The unit is a sweep, not a recorded row**, and that distinction is the counter's whole
value. `run_shard()` rewinds whenever a pass made progress, because a document cut short by
`DOC_SECONDS` leaves pages behind the cursor — the 1,407-page document needs several passes.
So one sweep reaches the same unmeasurable page repeatedly, and charging every row it wrote
spent all five attempts *inside a single sweep*: two in the ordinary two-document case, all
five whenever a deadline-cut document kept supplying the progress that triggers the rewind. A
page whose S3 fetch was throttled for a few minutes was then retired permanently with
`confidence IS NULL` — which is the precise failure the counter exists to prevent, inverted.
`_counted_this_sweep` charges each page at most once per process, and the same map keeps the
run summary counting *pages* rather than rows.

**The exit code is a contract**, and `1` is deliberately not part of it: `0` the shard is
measured, `10` pages remain and this sweep measured some, `11` pages remain and this sweep
measured nothing, `2` bad shard arithmetic, `3` no docker or no renderer, `12`
`--verify-complete` found the corpus unfinished. `1` is what CPython returns for an uncaught
exception, and the old "1 means work remains" made a dead process and an ordinary first sweep
the same observable to the caller.

**`--verify-complete --shards N` is the end-of-run assertion.** It names every shard that still
owes pages and reports the exhausted set beside the remaining one. It reads `ws13_pages` rather
than the fleet's S3 claim objects on purpose: a node that died before it ever claimed a slot
leaves nothing behind in S3, and leaves every page it never measured in the database.

Cost is dominated by `docker run`, not by OCR. The real work is ~1.2 s/page; container
startup against the ocrmypdf image is 1.5–3 s. One container per **document** rather than two
per **page** takes launches from ~646,000 to ~28,988. At a ~1.6 s/page **seed** that is ~144 core-hours.
The seed is an estimate, not a measurement, and nothing in the pass replaces it on its own:
`--plan` projects from `--rate`, which defaults to the seed. Before committing a fleet size,
run one shard over a few dozen documents with `--limit`, read `pages_per_second` from the
heartbeat it writes, and pass that back as `--rate`. The two container shapes differ by ~4×, so
a fleet sized off the wrong seed is wrong by that much.

The heartbeat key is whatever `status_key()` returns, and there are only two forms:
`ws13/confidence/status.json` for an unsharded run — which the sizing run above *is*, since
`--shards` defaults to 1 — and `ws13/confidence/status-<shard:04d>-of-<shards:04d>.json` once
sharded, e.g. `status-0007-of-0640.json`. This used to be documented as
`status-<shard>.json`, which is neither: an operator substituting a shard index into it got
`NoSuchKey` for the one number the whole sizing argument rests on.

**A shard cannot be split below one document, and that governs the sizing.** Measured skew of
the worst shard against the mean, over the real page distribution:

| Shards | Hash worst/mean | Size-ordered worst/mean | Wall clock | Efficiency |
|---|---|---|---|---|
| 64 | 1.44× | 1.23× | 2.76 h | 81% |
| **128** | **1.56×** | **1.47×** | **1.65 h** | **68%** |
| 320 | 2.93× | 2.23× | 1.00 h | 45% |
| 640 | 4.01× | 3.53× | 0.79 h | 28% |

Wall clock is set by the **worst** shard, not the mean, so cost is *not* flat in fleet size —
an earlier draft of this document said it was, and that was wrong. At 640 shards a single
1,407-page document is 2.8× the 505-page mean on its own, and no document-granular assignment
can fix that; ordering by page count and dealing round robin (deterministic on every node, no
coordination) recovers only 1.05–1.31×.

So 40 nodes costs ~$18.41 to finish in 0.79 h at 28% efficiency, against ~$7.68 in 1.65 h at
68% for 8 nodes. **Recommended: one `c7g.16xlarge`, 64 processes, ~2.76 h, ~$6.41 at 81%
efficiency** — the cheapest option, and it needs no cross-node shard assignment at all because
the shard index is just the local process index. `FleetMode: confidence` on the ASG exists but
carries three unresolved blockers in its slot-claim protocol (see below); it is not the
recommended path for this pass.

Stop the Cohere backfill first either way — it contends on the same 2-vCPU `db.m7g.large`.

### Phase 2 — re-OCR only the weak tail

Sized only after Phase 1. If it lands near the lexical proxy's 14%, that is ~45,000 pages:

- **Tesseract tier-1 escalation** (`--oversample 400 --clean-final`) — already coded, free
- **AWS Textract `DetectDocumentText`** — native per-word confidence, no fleet, ~$68 for 45k
- **PaddleOCR PP-OCRv5 on CPU** — better on degraded scans, 3–5× slower
- GPU engines are effectively out: the G/VT quota is 8 vCPU (one `g5.2xlarge`) and all GPU
  spot quota is 0

Whatever is chosen, Phase 2 re-chunks and re-embeds what it touches. Plan that; do not
stumble into it.

### `FleetMode: confidence` — the three blockers are closed

`infra/ws13_fleet.yaml` gained a `FleetMode` parameter that lets the existing ASG run the
confidence pass. It works by having each node claim one of `ConfidenceNodeSlots` slots through
an S3 object, deriving its shard indices from the slot. Review found three blockers in that
protocol. Each is now fixed, and `tests/test_ws13_fleet_template.py` runs the template's own
shell against a stub `aws`/`date`/`sleep` to hold them fixed:

1. **A replacement no longer decrements the group it was born to rescue.** A replacement for a
   hard-dead node arrives long before `ClaimStaleSeconds`, so it used to find no free slot and
   retire **with** `--should-decrement-desired-capacity` — permanently removing the only
   capacity that could ever reclaim the dead node's shard. Claiming now waits
   `ClaimStaleSeconds + 300` s, re-attempting, and only then gives capacity back: after that
   window every unfinished claim has demonstrably been refreshed by a live node, so the
   "this node is surplus" that a decrement asserts is actually true. A run in which every slot
   is already `complete` breaks out at once rather than waiting.
2. **Exit 1 is no longer overloaded.** `ws13_confidence_pass.main()` returns `10` for "pages
   remain and this sweep measured some", `11` for "pages remain and this sweep measured
   nothing", and leaves `1` to mean what CPython makes it mean. The sweep loop sweeps straight
   on for 10, backs off geometrically (60 s → 900 s) for 11 and gives up after 6 consecutive
   stalls, and retries an unhandled `1` three times before failing the worker to the node
   agent. Nothing hot-loops to the 24 h ceiling any more.
3. **A node asserts before it retires.** A node that finishes its shard cleanly now looks for a
   slot that is unclaimed, or whose holder has stopped refreshing, and *adopts* it rather than
   retiring — so the group cannot decrement to zero while a shard is unmeasured, because the
   last node out has by then found nothing claimable. What is still outstanding is published
   as `WS13/Fleet SlotsIncomplete`, and an **unclaimed** slot counts (it has no object in S3,
   which is exactly the hole this counts). The authoritative check is against the database,
   not the claims: `ws13_confidence_pass.py --verify-complete --shards N` names every shard
   that still owes pages, and reports separately any pages that were given up on rather than
   measured.

The adopt step waits the same `ClaimStaleSeconds + 300` s the boot-time claim does, for the
same reason: one attempt, then a decrement, would have re-created blocker 1 at the other end of
the node's life — and there worse, because that node has already proved it can measure a shard.
The node's own lifetime clock is kept separate from the generation clock, so adopting does not
make a partial failure in the new generation look like "everything died at boot" and park a
proven node in the 6 h hold.

### The fourth blocker: the template could not be deployed at all

**The rendered `UserData` was ~30,000 bytes. EC2's limit is 16,384.**
`aws cloudformation deploy` failed on the `LaunchTemplate` resource with *User data is limited
to 16384 bytes* and rolled the stack back, before a node booted, in **both** modes — the claim
script was written in both. Measured across this file's history with its declared defaults:

| commit | UserData | |
|---|---:|---|
| `f335c8c` | 8,599 B | ok — the fleet that OCR'd the corpus |
| `c1accaf` | 20,689 B | **over** — `FleetMode: confidence` arrives |
| `19dea85` | 20,689 B | **over** |
| before this change | ~30,000 B | **over** |
| now | **3,736 B** | ok |

So `FleetMode: confidence` was never deployable, from the commit that introduced it —
consistent with the pass never having run, but stated nowhere, while the shell tests all passed
over a template that could not reach a node.

The five scripts now live in `infra/fleet/` and ship inside `ws13/fleet/bundle.tar.gz`, which
the node already downloads. `UserData` is the ~40 lines that install packages, fetch the
bundle, export the parameters as environment and run `node_boot.sh`.
`tests/test_ws13_fleet_template.py` runs the **committed files** rather than re-deriving them
from heredocs, and `UserDataSizeTests` fails if the block passes half the limit again.

**What that changes, plainly: the node's shell is versioned with the bundle now, not with the
stack.** A stack update no longer changes what a node runs — rebuilding and uploading the
bundle does. That is why `tools/build_ws13_bundle.py` prints a sha256 per member and untars
`bundle_manifest.json` beside the code: `cat /opt/ws13/bundle_manifest.json` on a node is how
you tell which version it has. Two guards come with it: UserData refuses to continue if
`node_boot.sh` is missing after unpacking, rather than leaving a node in service holding
nothing, and a test asserts every `WS13_*` name the scripts read is exported by UserData —
a parameter that stops reaching the node is now an empty string there, not a deploy-time error.

Fixed or not, the recommendation is unchanged: the efficient shard count is 64–128 and a
single `c7g.16xlarge` supplies 64 with no coordination at all, so this pass does not need a
fleet. Driving the pass from SQS the way the OCR worker already is remains the way to remove
the bespoke claim protocol entirely — and it would remove this size blocker with it.

## Failed documents (`pipelines/ws13_rescue.py`)

Six `error` and one stuck `running`, all `ocr_queue`, all with `pages=NULL` and no
`ws13_documents` row — they contributed no text at all. The handoff called them
509-megapixel map plates needing reclassification; the recorded reasons say otherwise:

| Recorded | Count | Meaning | Remedy |
|---|---|---|---|
| `ocr_exit_4` | 5 | `INVALID_OUTPUT_PDF` — OCR ran, PDF/A validation failed | `--output-type pdf`, skipping PDF/A entirely |
| `ocr_exit_7` | 2 | `CHILD_PROCESS_ERROR` — tesseract crashed or was killed | larger `--tesseract-timeout`, then page-seg alternatives, then skip the page |

Two of those also report `invalid jpeg data reading stream`, where no OCR setting helps and
the input must be repaired first (`qpdf --decode-level=all`, then a ghostscript rewrite). The
rescue tool classifies on the recorded exit code and escalates a remedy ladder, defaults to
`--dry-run`, ends a document that exhausts the ladder in a terminal state with a *classified*
reason rather than a traceback fragment, and re-enters rescued documents through
`ws13_enqueue.py` so there is one indexing path rather than two.

Three things it will not do, each of which it used to:

- **Retire a document over a node defect.** Exit 3 `MISSING_DEPENDENCY`, exit 5
  `FILE_ACCESS_ERROR` and a `BAD_ARGS` from the fleet's own `WS13_OCR_EXTRA_ARGS` are
  properties of the image, the disk and the environment. They are now *deferred*: reported,
  and the manifest row left untouched at `error` so the next `--all-failed` sweep sees it again
  once the real defect is fixed. Writing `unrescuable` on them retired an indexable document
  permanently, because `SETTLED_STATUSES` excludes that status for good. A row that has never
  been diagnosed at all (`stale_running_reaped`, `unclassified`) is deferred for the same
  reason.
- **End a whole run on one document.** Each ladder runs inside per-document containment, so an
  S3 object that will not fetch costs that document and nothing else; the rest of the plan is
  still attempted, and the faulted row is left selectable rather than classified from its
  traceback.
- **Count `[OCR skipped on page N]` as rescued text.** That line is ocrmypdf reporting a page
  it did not read; at ~23 characters each, against `MIN_RESCUE_CHARS` 20, a handful of them
  cleared the threshold on their own — and the rung most likely to produce them is
  `--skip-big`, whose entire purpose is to abandon an unreadable page. `sidecar_chars()` now
  strips them before counting and carries the skipped count into the attempt trail.

## Operator sequence

Everything below is opt-in. Run it in this order. Steps 1 and 2 are done.

**0. Rebuild and upload the worker bundle.** The S3 bundle currently matches the *old*
committed source, so none of the worker changes take effect until it is rebuilt — and it must
now also contain `ws13_migrate.py` and `ws13_migrations.sql`, which `ws13_seed.py` imports.

```bash
tools/build_ws13_bundle.py
```

There was no builder until now; the bundle was assembled by hand, which is how it came to be
missing `ws13_migrate.py` once already and how it came to be four days behind the source. The
builder will not write an archive that is not **closed under its own imports**: it parses each
member with `ast`, and a `ws13_*` sibling that any member imports and the list does not carry
fails the build by name rather than becoming a `ModuleNotFoundError` on the seeding node. It
also cross-checks its list against `ws13_seed.BUNDLE_FILES` and against every
`python3 ws13_*.py` the launch template invokes.

The archive is **byte-reproducible** — pinned mtimes, cleared uid/gid, no gzip timestamp — so
`--verify` answers the question a fixed S3 key cannot: is the object up there the bundle these
sources build? And it carries `bundle_manifest.json`, a sha256 per member, which untars beside
the code: `cat /opt/ws13/bundle_manifest.json` on a node says which bundle that node is
running, instead of inferring it from a `LaunchTime`.

The **upload is a separate step**. The builder has no S3 write path at all — no `boto3`, no
`subprocess`, asserted by test — but not because the bucket is unwritable. It is writable with
the project credentials, and this is where an earlier draft of this section was wrong: it said
the local permission classifier refused the write, a claim carried over from the handoff and
never tested. The real reason is that building and publishing are different decisions, with
`--verify` and the digest sitting between them; a builder that uploaded on success would run
both checks only after the fleet could already download the result. It prints the `aws s3 cp`
line; run that deliberately.

**Done on 2026-08-27.** `sha256 d9da7a342a405c8192938397bc08fef725d9b819690ce92665a623cbcb63b81d`,
21 members, 198,650 bytes, round-tripped out of S3 and re-verified against these sources.

What it replaced is worth recording, because it was worse than "stale": the object in S3 held
**three** files — `ws13_seed.py`, `ws13_worker.py`, `ws13_enqueue.py`, all at their 2026-08-24
sizes — plus three `._*` AppleDouble forks from a macOS `tar czf` run without
`COPYFILE_DISABLE=1`. Eighteen members were absent, including the three `ws13_seed.py` imports
and aborts by name without. So `FleetMode: ocr` would have died in the seeder before enqueuing
anything, and `FleetMode: confidence` had no `ws13_confidence_pass.py` on the node at all —
that mode has never once been able to run.

**1. Migrations, on the in-VPC host** (`i-0818521a8b3ff7c90`):

```bash
python3 tools/ws13_ssm.py --script pipelines/ws13_migrate.py --with-dsn --dry-run
```

Then apply with `--yes`. Follow with `--check` to prove it landed.

**2. Provenance backfill** — required before any citation can resolve by URL:

```bash
python3 pipelines/ws13_backfill_provenance.py --dry-run --bucket nw-mineral-monitor-730883236375
```

**3. Phase A, the index.** Read `--dry-run` output before `--yes`; step (a) touches a process
whose parent may shut the node down:

```bash
python3 pipelines/ws13_build_ann_index.py --pause-backfill --build --verify --measure --report /tmp/ann.json --dry-run
```

**4. Network probe, then deploy the retrieval stack** with `VectorArmEnabled=false`, and
invoke it with `{"op":"ping"}`. Expect a response in under 3 s.

**5. Ship the lexical arm dark**, shadow 48–72 h with
`tools/ws13_live_known_items.py --shadow`, then canary, then cutover. Rollback is flipping
`WS13_RETRIEVAL_ENABLED` back to `false`.

## Deliberately not done

- **The viewer opens a WS13 document; the map does not yet offer one.** `site/viewer.html`
  used to resolve every document through the WS12 manifest, so a WS13 citation was inert text.
  It now speaks both corpora: `#corpus=ws13` carries the stored original's `s3_key` and its
  `rights_basis` — the two facts the docs API needs because this corpus is indexed in a
  Postgres that API cannot read — and the WS13 chrome prints the licence beside the page
  rather than implying public domain by silence. Neither field grants anything: the request is
  still gated on a live Cognito session, and the rights class is read off the key's
  `ws12/{class}/` prefix, not off the link, so a link cannot assert its own rights class. The
  viewer also performs the end-of-document check `validate_ws13_request()` explicitly delegates
  to it, since no page count is reachable from that function.

  **What the key check is and is not.** `validate_ws13_request()` anchors the key's *shape* to
  the two servable prefixes and cross-checks its *digest* against the document id only when the
  key names one. The flat archive shape (`ws12/research-copies/IF0131_001.pdf`) names no digest
  and cannot be checked that way — the function says so, and
  `tests/test_ws13_doc_resolver.py::test_a_flat_archive_key_names_no_digest_and_is_still_accepted`
  pins the 200. `rights_basis` is checked for presence, not against the document; it is the
  attribution text the licence line is built from. So a hand-crafted fragment pairing document
  A's id with document B's flat key renders B's pages under A's sha256 and a caller-supplied
  licence string. That is a false provenance label *inside* the session gate, not a way through
  it, and closing it needs an authoritative lookup this API cannot reach — the same reason the
  link carries the key in the first place. Earlier drafts of this section and of
  `viewer.html`'s header said the key was "re-validated against the document" without
  qualification, which claimed a check the resolver does not perform.

  What is still open is the other end: `docChip` in `site/index.html` resolves a document
  through `docFind()`, the WS12 manifest, and renders nothing for an id it does not carry — so
  a WS13 citation still falls back to `docCiteStatic`, inert text with its rights badge. Making
  those chips openable means carrying `s3_key`, `rights_basis` and `viewer_key_kind` from the
  retrieval Lambda's citation into the chip's fragment, and there are no such citations to
  carry until `WS13_RETRIEVAL_ENABLED=true`. That is a step in the cutover, not before it.
- **24 of the 25 known-item fixtures are unwritten.** They need the live corpus and a human
  to verify each one; inventing them would make the gate meaningless.
- **Per-page confidence still needs a re-OCR pass** over the 323,059 OCR pages to become
  real (the other 437,000 are born-digital and have no OCR confidence to measure). The
  renderer fix stops the silent failure for everything processed from now on.
- 12,519 map plates (75,698 pages, 176.7 GB) still have no consumer, ~3,058 OCR documents have
  zero-byte sidecars, and Cohere remains ~4 weeks out against a non-adjustable cap. None of
  these block retrieval.
