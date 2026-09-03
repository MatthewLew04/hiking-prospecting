# WS13 → 3-D — every document to its mine, every mine to a model

_The framework for turning the 56,282-document WS13 corpus into 3-D models of
the workings and the underground mineral composition, one located mine at a
time, on AWS where the corpus lives. Written 2026-09-02; the code is
`pipelines/ws13_geomodel.py` and the modules it drives. Read
`GEOMODEL.md` §4 first: nothing here invents a number, and every element of
every model carries the sentence it came from._

## 1. What it does, in one paragraph

A document in `nwmm-ws13` is joined to the mines it names, its text is carved
into the stretch that is about each mine, that stretch is parsed into typed
workings (adits, shafts, drifts, raises, stopes, levels) and into what the
rock carries (minerals, grades, ore zones, host rock, alteration), the
questions the prose leaves open are answered by omission, the elements that
survive are placed on real terrain at a located site, the model is published
content-addressed to `models/<slug>-<hash8>/` with a manifest that quotes its
sources and states its rights, and a compact index tells every dot on the map
which model to open. A document that names no locatable mine, or describes
no workings, or whose rights cannot be stated, is parked with its reason and
never becomes a model. The whole thing is sharded, ledgered, rerun-safe and
sized from a measured rate, exactly like the WS13 confidence pass.

## 2. The funnel

The first thing to run, and the only thing to run before any fleet is sized:

```
python3 pipelines/ws13_geomodel.py --plan            # in-VPC host, read-only
```

| stage | keeps | drops, with the ledger reason |
|---|---|---|
| documents | all 56,282 indexed rows | — |
| mine hints | rows with `mine_ids` or `mine_names`, or a verified `ws13_mine_id_map` row | `no mine named` |
| workings vocabulary | a `tsvector` hit on adit / tunnel / shaft / winze / raise / stope / drift / crosscut / level / incline / decline / portal / collar in `ws13_chunks` | `no workings vocabulary` |
| rights | `public_domain`, or a `rights_basis` whose terms can be carried (attribution, non-commercial, share-alike propagate to the model) | `rights unstated` — refused, never published |
| resolved | exactly one located site per named mine (§4) | `ambiguous` / `unlocated` / `district container` (its named mines are tried one by one) |
| carved | a name-anchored section that *describes* development (`geomodel_corpus.sections`) | `no descriptive section` |
| parsed | at least one element after the omit policy | `no elements` / `all omitted` |
| built | terrain reachable, collar placed | `unplaceable` (never sea level) |
| published | manifest with quotes, spans, rights, composition | — |

`--plan` prints this table with counts and the reasons for every drop. The
counts are the design's honesty: the corpus has 760,059 pages, and most of
them are not about a mine's workings.

## 3. Where it runs and what it touches

* **Host.** The in-VPC host (`i-0818521a8b3ff7c90`, via SSM) for `--plan`,
  `--limit` sizing runs and single documents; the `ws13-workers` fleet in
  `WS13_MODE=geomodel` for the corpus. The host has egress, so terrain tiles
  (AWS terrarium) and Bedrock are reachable; a VPC-attached Lambda is not the
  place for this (no NAT, no endpoints — `WS13-RETRIEVAL.md`).
* **Reads.** `ws13_documents` (sha256, state, county, mine_ids, mine_names,
  title, portal, doc_type, doc_date, source_url, rights_basis, public_domain,
  admission_class), `ws13_mine_id_map` (the Phase-D bridge: verified identity
  rows are the strongest tier), `ws13_chunks` (the lexical prefilter and the
  text fallback), the per-page sidecar text under
  `s3://<bucket>/ws13/searchable/<sha256>/` (the clean page text; chunks are
  de-overlapped only when the sidecar is missing, and the ledger says which).
  Site references come from the repo bundles on the host: `grades.json`,
  `stategeo_<st>.json`, `mrds_<st>.json`, `usmin_<st>.json`, ARDF.
* **Writes.** `ws13_geomodel_runs` (the ledger, §7), `models/<id>/` in the
  site bucket (`model.geomodel.json`, `model.omf`, `workings.dxf`,
  `workings.geojson`, `plan.svg`, `section.svg`, `iso.svg`, `manifest.json`,
  `card.json`), `data/models/index.json` (compact), the heartbeat
  `ws13/geomodel/status-<shard:04d>-of-<shards:04d>.json`.
* **Never.** `ws13_chunks.text` or any embedding (this pass reads text; the
  confidence pass owns re-OCR), the document store objects, `index.html`.
* **Packaging.** The worker bundle (`tools/build_ws13_bundle.py`) does not
  carry the modeller; a geomodel node needs a checkout at `NWMM_ROOT`
  (the driver exits 3 with that message otherwise). Adding the modeller and
  the site bundles to the bundle members is the first packaging step.
* **Offline.** Every stage also runs against a fixture:
  `python3 pipelines/ws13_geomodel.py --plan --fixture tests/fixtures/ws13_geomodel`
  prints the funnel over eight synthetic documents (a clean description, a
  district report naming three mines, no workings vocabulary, an ambiguous
  name, missing rights, an unchanged republish, an all-omitted description,
  an unknown phrasing) and `tests/test_ws13_geomodel.py` asserts every drop
  reason, the ledger, the exit codes and the shard partition.

## 4. Document → mine → site (the resolver)

`geomodel_corpus.SiteIndex` indexes every located site the front end knows,
by normalised core name, state and county, across five namespaces:
`grades:<row>`, `stategeo:<id>`, `mrds:<dep_id>`, `usmin:<fid>`,
`ardf:<number>`. `resolve(doc)` tries tiers, strongest first, and the first
tier yielding exactly one located candidate wins:

1. a **verified** `ws13_mine_id_map` row with relation `identity`;
2. an **embedded code** in `mine_ids` that is itself a front-end id;
3. **exact core name + state + county**, unique across namespaces — the same
   physical mine in two namespaces (same name, same state, within 2 km) is
   merged into one candidate carrying every key under `also`;
4. **exact core name + state**, unique;
5. a **district / county** relation is not a mine: the document is parked as
   a container and, separately, every mine name the text mentions is
   resolved by the same rules so a district report yields one section — and
   one model — per uniquely resolvable mine.

Fuzzy name matches are recorded as ambiguous and parked; they are never a
tier. Evidence on every candidate says exactly what matched (the normalised
name, the state, the county, the merge distance). This is the rule that keeps
a document from georeferencing the wrong hole in the ground — the same rule
`resolve.py` and `mine_lookup` have always applied, now over every namespace.

## 5. Text → model

* **Carve.** `sections(pages, target_cores, other_cores)`: a window starts
  where the mine's name appears and ends at the first mention of another
  subject; a window survives only if a sentence *describes* development.
  Attribution is preferred over recall (`AUTOPOPULATE.md` §2).
* **Parse.** `narrative.parse` → elements with quote, span, page, per-field
  confidence `described`; every open field is a question. `narrative.lexicon`
  → the vocabulary census. `assay.attach` → quoted grades with basis
  (selected ≠ average ≠ shipment), width, level; a stated strike and dip → a
  vein plane.
* **Compose** (`geomodel/composition.py`). The underground mineral
  composition, from a ~120-entry lexicon of ore, gangue, alteration and host
  terms with historic synonyms (blende, mispickel, horn silver, heavy spar…):
  every mention with quote and span, its role, the commodity it implies
  (galena → lead; `ore` implies nothing), the level or working the sentence
  ties it to, and the zone (oxidized / sulphide). `by_level` is the column a
  geologist wants: what the 300 level carried. Level-tied statements become
  a `samples` point set placed at the level's elevation on the working the
  sentence names, or at the collar when it names none, with a `placement`
  column that says which. Statements tied to nothing stay in the manifest
  only. Where the site also has MRDS commodity fields, agreement and
  disagreement are recorded; the text is never overwritten by the record.
* **Answer.** The unattended policy is **omit**: every question is answered
  `null`, logged in `manifest.json` under `answers` with the policy's
  justification, so an auditor sees exactly which elements the text could not
  support. An `Answerer` interface exists for a model-backed answerer; its
  contract is that a value is only ever returned together with a verbatim
  quote that exists in the carved text, else `null`, and every such answer is
  `assumed` and dotted. It ships as `NullAnswerer`.
* **Build.** `services/minevis/tools.run_build` in process, on real terrain
  (tiles cached per node), with `kit.build_site_model` context when asked
  (draped AOI geology, graded mines, claims). A collar that cannot be placed
  on terrain is `unplaceable`, never sea level.
* **Publish.** `publish.publish` to the `models/` prefix, content-addressed
  over (normalised spec + site + builder version + policy), so an unchanged
  document is a no-op. The manifest carries the document's `source_url`,
  `rights_basis`, `public_domain`, `admission_class` and the derived
  `attribution_required` / `non_commercial` / `share_alike` flags with a
  `rights_terms` string — a model derived from a licensed research copy
  carries the copy's terms — plus the `composition` block. A non-public-domain
  document without a `rights_basis` is refused.

### What the 3-D file contains

`model.geomodel.json` (the viewer's project; OMF, DXF and GeoJSON beside it):
topography; the workings lineset (type, level, level_z, source doc / page /
quote, confidence per feature — described draws dashed); stope volumes; the
assay points (metal, value, unit, basis, width, level); the **composition
points** (minerals, role, zone, level, placement, quote); the vein plane when
an attitude was stated; the water plane when a level was stated; optional
draped geology, graded mines and claims. The viewer's PICKED card shows every
column of a picked point, so the minerals at the 300 level read straight off
the model.

## 6. Sharding, ledger, heartbeat, exit codes

* **Shards.** Documents partition by a stable hash of `sha256` modulo
  `--shards`; the Postgres and Python expressions are documented in the driver
  and agree, so a fleet's shards are a partition, not a set of guesses.
* **Ledger.** `ws13_geomodel_runs(sha256, mine_key, run_id, status, reason,
  model_id, content_hash, counts, warnings, attempts, updated_at)`, statuses
  `planned / parked / skipped / built / published / error`. A `(sha256,
  mine_key)` whose content hash is unchanged and status is `published` is
  skipped on rerun. `--migrate --check` applies and verifies the DDL the way
  `ws13_migrate.py` does (guarded: an existing table with the wrong
  definition fails loudly).
* **Heartbeat.** `ws13/geomodel/status-<shard:04d>-of-<shards:04d>.json`
  (`status.json` unsharded): documents done / remaining, models published,
  parked by reason, documents per second, models per second.
* **Exit codes** — the contract `run_worker.sh` sweeps on, and `1` is
  deliberately not part of it: `0` shard finished · `10` progress made, work
  remains (sweep again) · `11` no progress, work remains (back off) · `2` bad
  shard arithmetic · `3` environment (no DSN, no bucket, no terrain) · `12`
  `--verify-complete` found work remaining.
* **Fleet.** `WS13_MODE=geomodel` in `infra/fleet/run_worker.sh` mirrors the
  confidence branch. Size it from a `--limit 200` run's measured documents
  per second; the arithmetic in `WS13-RETRIEVAL.md` (worst shard sets the
  wall clock; one `c7g.16xlarge` with 64 processes is the cheapest shape)
  applies unchanged, and the per-document cost here is dominated by terrain
  tiles on first touch of a district, not by parsing.

## 7. Operator runbook

```bash
# 0. on the in-VPC host, read-only: the funnel
python3 pipelines/ws13_geomodel.py --plan

# 1. the ledger table, once
python3 pipelines/ws13_geomodel.py --migrate --check

# 2. a sizing run: 200 documents, local publish, measured rate in the heartbeat
python3 pipelines/ws13_geomodel.py --limit 200 --publish local --out var/geomodel/ws13-sizing

# 3. one document, end to end, with the full trace
python3 pipelines/ws13_geomodel.py --doc <sha256> --publish local --verbose

# 4. the corpus: fleet in geomodel mode (or one large host, 64 processes)
WS13_MODE=geomodel WS13_SHARD_COUNT=64 ...      # see infra/fleet/run_worker.sh

# 5. the end-of-run assertion, then the index, then the site
python3 pipelines/ws13_geomodel.py --verify-complete --shards 64
python3 pipelines/ws13_geomodel.py --index          # compact index + card.json per model
bash infra/deploy.sh update-site
```

Gates, in order: `--plan` before any spend; the sizing run's rate before any
fleet size; the known-item fixture (`tests/fixtures/ws13_known_items.json`
pattern — human-verified document → mine → model triples) before a
certifying run; `--verify-complete` before the index; the parked ledger
reviewed, by reason, after it.

## 8. Honest limits

* **Most documents do not describe workings.** The funnel says how many do;
  a big corpus is not a big model count.
* **OCR quality bounds the parser.** Pages with `low_confidence` are parsed
  anyway, but the ledger records the page confidence beside every element,
  and a spec whose elements all come from low-confidence pages is published
  with that warning in its manifest.
* **Names collide.** The resolver parks rather than guesses; the parked
  ledger is the work list for a human, and a verified `ws13_mine_id_map`
  row is how a decision is recorded so the next run resolves.
* **Prose gives `described`, never `surveyed`.** Solid lines come only from a
  georeferenced plate traced in the viewer (`mapplate.py`). This run does not
  trace plates; it could feed the plate finder (`GEOMODEL.md` §5 item 1).
* **Composition is what the text says.** A mineral list is a statement, not
  an assay; a grade is quoted with its basis; nothing is interpolated into a
  resource.
* **Rights travel.** A model from a licensed copy is as licensed as the copy.
* **The answerer is off.** Until a model-backed answerer that can only answer
  with a quote is reviewed against the known items, every gap is omitted.

## 9. Delivered, and next

Delivered: the driver, ledger, migrations, fleet mode, offline fixture and
tests; the resolver over every namespace; composition at depth; the compact
index with per-model cards; the map cards for MRDS, USMIN, state-survey and
ARDF dots. Next, in order: run `--plan` on the host and record the funnel
here; the sizing run; the known-item fixture for this run; the reviewed
answerer; national geology as batch context (the Python side reads the AOI
bundles today, not the national tiles); the ARDF `Workings_exploration` text
as a second source through the same `Corpus` interface.
