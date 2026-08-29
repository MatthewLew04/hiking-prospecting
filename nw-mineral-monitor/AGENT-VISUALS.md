# Agent mine visuals — implementation plan

**Status (2026-08-25): phases 1–4 built and tested; phase 2's AWS half and phase 5 are not.**

| Phase | State |
|---|---|
| **1** — `narrative` · `resolve` · `agentbuild` · `render2d` · CLI · tests | **built** |
| **2** — `publish.py` | **built**, against a `Target` interface; the AWS half (IAM role, `models/*` allowlist, `deploy.sh --exclude`) is still owed by the "AWS worker updates" run |
| **3** — `services/minevis/` HTTP + `/tools` + job queue + systemd unit + wiring doc | **built** |
| **4** — `list_mine_documents`, map-plate georeferencing handoff (`mapplate.py`), grades and vein attitude from assay text (`assay.py`) | **built** |
| **5** — private models with presigned reads | not built (conditional on the Cognito gate proving insufficient) |

With no `NWMM_MODELS_BUCKET` set the service publishes to local disk and serves the
files itself, so the whole loop — prose in, model URL out, questions in between —
is provable on the box today with no AWS change at all.

Test counts: 48 narrative · 17 resolve · 27 agentbuild · 22 render2d · 27 publish · 30 mapplate · 37 assay · 60 service = 268 new tests.

Goal: an agent running on an EC2 instance (a Qwen or Kimi variant doing OpenAI-style
tool calls) hands in a **mine description in prose** and gets back a **3-D model URL**
plus rendered views, without a human touching the viewer.

Decisions taken (2026-08-24):

| Question | Decision |
|---|---|
| Where the builder runs | HTTP service on the agent's own EC2 box |
| Tool contract | OpenAI-style function schemas (`/tools` returns the array) |
| Delivery | S3 `models/` prefix → CloudFront URL |
| Mine context | Resolved from the site's existing data (`grades.json`, 3,369 mines) |
| Text → geometry | Hybrid: deterministic parser first, agent answers typed gaps |
| S3 write auth | EC2 instance role scoped to `s3:PutObject` on `<bucket>/models/*` |
| Who can open a model | Behind the existing Cognito app login |
| Gap handling | Tool returns typed questions; agent answers; deterministic rebuild |
| Latency | Async job + poll |

---

## 0a. Ownership boundary — do not duplicate the "AWS worker updates" run

A separate Claude Code session ("AWS worker updates") owns the AWS and EC2 surface. To keep
the two from colliding, this plan claims only the application layer:

| Area | Owner |
|---|---|
| `pipelines/geomodel/{narrative,resolve,agentbuild,render2d,publish}.py` | **this plan** |
| `services/minevis/` (HTTP service, `/tools` schema, job queue) | **this plan** |
| `tests/test_geomodel_*.py`, `tests/test_minevis_service.py` | **this plan** |
| `infra/template.yaml` — IAM role, instance profile, `models/*` allowlist | **AWS worker run** |
| `infra/deploy.sh` — `--exclude "models/*"` on both site syncs | **AWS worker run** |
| EC2 provisioning, instance profile attachment, systemd unit installation | **AWS worker run** |

Phase 2 below is therefore a **request to that run**, not work to be done here. `publish.py`
is written against an interface — "the process has `s3:PutObject` on `<bucket>/models/*` and
knows the bucket name" — and is testable with a local filesystem stub, so Phase 1 and 3 do
not block on any AWS change landing.

One already-landed edit to be aware of, made before this boundary was drawn: commit
`8a372c5` added `- !Sub '${SiteBucket.Arn}/model3d.html'` to `SiteBucketPolicy`. That was
required to stop CloudFront 403-ing the 3-D page itself and is unrelated to `models/*`. It
should be kept, not reverted.

---

## 0. The one thing that makes this cheap

`site/assets/geomodel/gm-viewer.js` already accepts `?project=<url>`:

```js
if (q.get('project')) { await loadProjectUrl(q.get('project')); }
```

`loadProjectUrl` fetches the URL and runs it through `importBytes(..., {asProject:true})`,
which already understands `geomodel.json`, OMF v2, OMF v0.9 and the kit zip.

**So the entire browser side of this feature is already deployed.** Publishing a model is
"write a file to S3 and return a link". No change to `model3d.html`, `gm-viewer.js` or any
other front-end file is required for Phase 1–3.

---

## 1. Architecture

```
  mine description (prose)  ─┐
  mine name / MRDS id       ─┤
                             ▼
   pipelines/geomodel/narrative.py     deterministic parser
                             │          prose → WorkingsSpec + gaps[]
                             ▼
   pipelines/geomodel/resolve.py       name → lon/lat/elev/commodity/grades
                             │          (index over site/data/grades/grades.json)
                             ▼
   pipelines/geomodel/agentbuild.py    spec + site → Project
                             │          reuses workings.py / kit.py / stratigraphy.py
                             ▼
   pipelines/geomodel/render2d.py      SVG plan · section · isometric (stdlib)
                             │
                             ▼
   pipelines/geomodel/publish.py       s3://<bucket>/models/<id>/…
                             │
                             ▼
   services/minevis/server.py          stdlib HTTP + job queue on the EC2 box
                             │          GET /tools · POST /call · GET /jobs/<id>
                             ▼
   agent tool call → { model_url, views, exports, confidence, unresolved }
```

Every module below `services/` is pure standard library, matching the existing
`pipelines/geomodel` rule. The service layer is the only new process.

---

## 2. New modules

### 2.1 `pipelines/geomodel/narrative.py` — the parser

Turns USGS/USBM-style prose into a typed `WorkingsSpec`. Deterministic, no model calls.

Grammar to cover (all seen in the WS12 corpus):

| Class | Examples |
|---|---|
| Bearing | `N45E`, `N 45° E`, `S 30 W`, `045°`, `due east`, `northeasterly`, `along the vein` |
| Length | `900 ft`, `900 feet`, `900'`, `1,200-foot`, `275 m`, `about 400 feet` |
| Vertical | `shaft sunk to 300 ft`, `inclined shaft 45°, 420 ft`, `winze 120 ft below the 400 level` |
| Level | `400 level`, `No. 3 level`, `adit level`, `100-ft level`, `main haulage level` |
| Element | adit, tunnel, crosscut, drift, incline, decline, raise, winze, shaft, stope, glory hole, portal |
| Relation | `driven N45E from the portal`, `from the 300 level`, `connects the 200 and 300 levels` |
| Count | `three adits`, `two shafts`, `a series of raises` |

Emitted element:

```json
{
  "id": "e3",
  "kind": "adit",
  "from": {"ref": "portal-1"},
  "bearing_deg": 45.0,
  "length_m": 274.32,
  "units_in": "ft",
  "confidence": "described",
  "quote": "An adit driven N45E for 900 feet cuts the vein…",
  "span": [1204, 1259]
}
```

Emitted gap:

```json
{
  "id": "g1", "element": "e5", "field": "bearing_deg", "required": true,
  "question": "The 300-level drift has no stated bearing. What bearing was it driven on?",
  "quote": "…a drift was extended 450 feet on the 300 level…",
  "options": [{"value": 45.0, "label": "same as the adit (N45E)"},
              {"value": null, "label": "unknown — omit this element"}]
}
```

Non-negotiable parser rules:

1. **Never invent.** A missing bearing is a gap, never a default. No element is emitted
   with a fabricated number.
2. **Every element carries its verbatim quote and character span.** This is what makes the
   result auditable and keeps it inside ASSUMPTIONS #48 — a model is a digitising bridge,
   not new evidence.
3. **Three confidence levels, and they are visible everywhere:**
   `surveyed` (traced off a georeferenced plan) · `described` (parsed from text) ·
   `assumed` (supplied by the agent or an operator in answer to a gap).

### 2.2 `pipelines/geomodel/resolve.py` — mine lookup

Builds an index from `site/data/grades/grades.json` — a columnar file with 3,369 mines and
these parallel arrays: `name, st, dist, cnty, x, y, com, au, ag, pb, zn, cu, sb, wo3, usd,
basis, quote, src, url, ton, yrs, dep`.

- Normalised name matching (case, punctuation, `Mine`/`Group`/`No. 2` suffixes) plus
  state/district/county narrowing.
- **Always returns candidates, never auto-picks.** There are many mines called "Bluebird";
  an ambiguous name becomes a `which_mine` gap.
- Returns `lon, lat, state, district, county, commodity, grade rows, source quote + url`.
- Collar elevation comes from the terrarium terrain tile for that point, cached on the box.

### 2.3 `pipelines/geomodel/agentbuild.py` — spec → Project

- Places the portal/collar at the resolved coordinate, Z from terrain.
- Walks the spec in dependency order, calling the **existing** primitives — no new geometry
  code: `workings.add_adit`, `add_shaft`, `add_level_working`, `add_raise`, `add_decline`,
  `stope_prism`, `summary`, `to_geojson`.
- Optional site context (async path only): `kit.build_site_model` for terrain, draped
  geology pancakes, grade points and claims.
- Writes `model.geomodel.json`, `model.omf` (OMF v2.0, Leapfrog 2025.1+), `workings.dxf`,
  `workings.geojson`.

### 2.4 `pipelines/geomodel/render2d.py` — the static views

Stdlib SVG (no matplotlib, no numpy):

- **plan** — workings in map view over a hillshade-free contour skeleton, north arrow, bar scale
- **section** — longitudinal section along the dominant strike, level lines labelled
- **iso** — simple axonometric of the whole workings set

`described` elements draw dashed, `assumed` dotted, `surveyed` solid. A legend states the
counts. This convention is the honesty mechanism: a described adit must never look like a
surveyed one.

### 2.5 `pipelines/geomodel/publish.py` — S3

- Key layout: `models/<mine-slug>-<hash8>/{model.geomodel.json,model.omf,workings.dxf,
  workings.geojson,plan.svg,section.svg,iso.svg,manifest.json}`
- `hash8` is content-addressed over *(normalised spec + resolved mine + builder version)*,
  so the same description always yields the same URL and republishing is idempotent.
- `manifest.json` records: input text hash, every quote, per-element confidence, the answers
  the agent gave and which tool call gave them, builder version, UTC timestamp.
- Returns `https://<dist>/model3d.html?project=/models/<id>/model.geomodel.json`.

---

## 3. The EC2 service — `services/minevis/`

`server.py`, stdlib `ThreadingHTTPServer`, bound to `127.0.0.1:8787` by default.

| Route | Purpose |
|---|---|
| `GET /tools` | The OpenAI function-schema array. Paste straight into the agent's `tools`. |
| `POST /call` | `{"name": "...", "arguments": {...}}` → sync result, or `{"job_id": "..."}` |
| `GET /jobs/<id>` | `{state: queued\|running\|done\|error, result?, questions?, error?}` |
| `GET /healthz` | liveness for systemd |

- Thread pool + on-disk job directory, so a restart does not lose a running build.
- Auth: loopback bind is the default; an optional `X-MineVis-Token` shared secret covers the
  case where the agent runs in a different container on the same host.
- Shipped with a `systemd` unit and a README section for the agent-side wiring.

### 3.1 Tool surface

```
mine_lookup(name, state?, district?, county?)
  → {candidates: [{mine_id, name, state, district, lon, lat, commodity, grades, source_url}]}

parse_mine_description(text, mine_id?)         # synchronous, no network
  → {spec_id, elements: [...], gaps: [...], coverage: {parsed_chars, elements, unresolved}}

build_mine_visual(spec_id | text, mine_id | {lat, lon}, answers?, views?, context?)
  → {job_id}

get_job(job_id)
  → done:      {model_url, views: {plan, section, iso}, exports: {...},
                confidence: {surveyed, described, assumed}, unresolved: [...]}
     questions: {questions: [...], spec_id}
     error:     {error, detail}
```

`list_mine_documents(mine_id)` arrives in Phase 4 — it exposes the WS12 store (25 documents,
3,763 pages, 202 citations) so the agent can fetch its own source prose instead of being
handed it.

### 3.2 The gap round trip

```
agent → build_mine_visual(text="…", mine_id="grades:1841")
      ← {job_id}
agent → get_job(job_id)
      ← {questions: [{id:"g1", field:"bearing_deg", question:"…", quote:"…"}], spec_id:"s7"}
agent   (re-reads the source document, decides)
agent → build_mine_visual(spec_id="s7", answers=[{id:"g1", value:45.0,
                                                  because:"same vein as the adit"}])
      ← {job_id}
agent → get_job(job_id)
      ← {model_url:"https://…/model3d.html?project=/models/silver-king-9f2c1e0a/model.geomodel.json", …}
```

Both builds run the same deterministic builder. Answers land in `manifest.json` tagged
`assumed` with the agent's own justification string, so an auditor can see exactly which
numbers came from the document and which came from the model.

---

## 4. AWS changes — **owned by the "AWS worker updates" run, not by this plan**

Listed here as the interface this feature needs, so that run has the requirements in one
place. Nothing in this section should be edited from the geomodel side.

1. **`infra/template.yaml`**
   - add `- !Sub '${SiteBucket.Arn}/models/*'` to `SiteBucketPolicy → AllowCloudFrontRead`
   - add `AgentModelWriterRole` + instance profile: `s3:PutObject`,
     `s3:AbortMultipartUpload` on `<bucket>/models/*` **only**, nothing else, no `s3:Delete*`
   - new outputs: `AgentModelWriterRoleArn`, and reuse `BucketName` / `SiteURL`

2. **`infra/deploy.sh` — a real trap to fix first.**
   `sync_public_site_without_pointers` runs `aws s3 sync "$SITE" "s3://$bucket" --delete`.
   `models/` exists only in the bucket, never in `site/`, so **every site deploy would delete
   every agent-generated model**. Add `--exclude "models/*"` to that sync, exactly as
   `ws13/*`, `staging/*` and `watch/*` are already excluded. Same for `update-site`.

3. **Access.** `model3d.html` sits behind the Cognito app gate when `auth.json` is present,
   so generated models inherit that gate. Stated honestly, and consistent with what
   DEPLOY.md already says: this gates the *app*, not the S3 objects — anyone who constructs
   the `models/<id>/model.geomodel.json` URL can fetch the raw file through CloudFront. If
   that is not acceptable for a given model, Phase 5 adds a private prefix with presigned
   reads, mirroring the document store.

---

## 5. Tests

Everything runs under `ci/run_tests.py`, and any optional cross-check must use the
`optional cross-validator unavailable: ` skip prefix — the convention we just repaired.

- **`tests/test_geomodel_narrative.py`**
  - a corpus of real public-domain phrasings → expected spec (the core table)
  - **no-invention test**: text with a missing bearing must produce a gap and must not
    produce an element with a bearing
  - **idempotence**: same text + same answers ⇒ byte-identical model id
  - **unit conversion**: feet/metres/`'` all land on the same metres value
  - round trip: spec → build → `workings.summary()` matches declared counts
- **`tests/test_geomodel_render2d.py`** — SVG well-formedness, scale bar arithmetic,
  dashed-vs-solid by confidence, deterministic output bytes
- **`tests/test_minevis_service.py`** — in-process server on an ephemeral port: `/tools`
  schema validity against the OpenAI function-schema shape, job lifecycle, restart recovery,
  auth rejection, error envelopes. No network.
- **`tests/test_geomodel_resolve.py`** — ambiguous names return candidates, never a pick;
  index build is stable.

---

## 6. Phasing

| Phase | Contents | Depends on AWS? | Touches the browser? |
|---|---|---|---|
| **1** | `narrative.py`, `resolve.py`, `agentbuild.py`, `render2d.py`, CLI `geomodel_kit.py narrate`, all four test files | no | no |
| **2** | `publish.py`, template.yaml role + `models/*` allowlist, deploy.sh `--exclude "models/*"` | yes | no |
| **3** | `services/minevis/` HTTP + `/tools` + job queue + systemd unit + agent wiring doc | no | no |
| **4** | WS12 document integration (`list_mine_documents`), map-plate georeferencing handoff, stratigraphy and grade pancakes from assay text | no | no |
| **5** | Private models with presigned reads, if the Cognito-app gate proves insufficient | yes | small |

Phase 1 is provable end-to-end on the EC2 box with no AWS involvement at all — it writes
files to local disk and the CLI prints the same JSON the tool will later return.

---

## 7. Honest limits

- **Old prose is genuinely ambiguous.** Expect a large fraction of elements to raise at
  least one gap on the first pass. The question loop is the product, not a workaround for a
  weak parser — a parser that never asked would be a parser that invented.
- **Described ≠ surveyed.** Dashed rendering, per-element confidence in the manifest, and a
  banner in the viewer are all mandatory. The failure mode to design against is a
  hand-drawn-from-text adit being read as a survey.
- **First call per district is slow** — terrain tiles must be fetched. That is why the build
  path is async.
- **Name collisions are the norm** in historic mining names. `mine_lookup` returns candidates
  and the agent must choose; auto-picking would silently georeference the wrong hole in the
  ground.
- **The parser will meet phrasings it does not know.** Those become gaps too, with the
  sentence quoted, rather than silent omissions — coverage statistics are reported on every
  parse so a systematically missed construction is visible.
