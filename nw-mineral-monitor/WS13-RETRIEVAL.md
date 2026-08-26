# WS13 retrieval — implementation and operator runbook

Closes the gap the WS13 handoff describes: 852,027 embedded chunks sat in a private-VPC
Postgres while the ASK Lambda served a 3.2 MB SQLite file holding two documents. This
document records what is now in the repository, what has to be run against live AWS to
activate it, and what is deliberately still open.

Nothing here has been executed against production. Every live step below is a command for an
operator to run deliberately; the code is written, tested offline, and shipped dark.

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
  suffix on both sides (15,581 rows end in ` County`, 51,685 are bare).
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

- **Per-page confidence was NULL for all 760,043 pages.** `pdftoppm` is not on `$PATH` in the
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

## Operator sequence

Everything below is opt-in. Run it in this order.

**0. Rebuild and upload the worker bundle.** The S3 bundle currently matches the *old*
committed source, so none of the worker changes take effect until it is rebuilt — and it must
now also contain `ws13_migrate.py` and `ws13_migrations.sql`, which `ws13_seed.py` imports.

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

- **The viewer does not yet open a WS13 document.** `site/viewer.html` resolves through the
  docs API manifest, which covers the legacy stored corpus only. A citation with no
  `source_url` therefore renders as inert cited text carrying its rights badge, not a dead
  link — `docChip` refuses to paint a button it cannot open. Extending the presigning path
  over `ws13/searchable/` is the next piece of work and is outside Phases A–E.
- **24 of the 25 known-item fixtures are unwritten.** They need the live corpus and a human
  to verify each one; inventing them would make the gate meaningless.
- **Per-page confidence still needs a re-OCR pass** over 760,043 pages to become real. The
  renderer fix stops the silent failure for everything processed from now on.
- 12,519 map plates (75,698 pages, 176.7 GB) still have no consumer, ~3,058 OCR documents have
  zero-byte sidecars, and Cohere remains ~4 weeks out against a non-adjustable cap. None of
  these block retrieval.
