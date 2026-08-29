# minevis — mine description in, 3-D model URL out

An agent on this box hands in a **mine description in prose** and gets back a
**3-D model URL** plus rendered views, without a human touching the viewer.

```
POST /call {"name":"build_mine_visual","arguments":{"text":"An adit driven N45E for 900 feet…","mine_id":"grades:17"}}
  -> {"job_id":"j-…"}
GET  /jobs/j-…
  -> {"state":"done","model_url":"https://…/model3d.html?project=/models/silver-king-9f2c1e0a/model.geomodel.json", …}
```

Nothing in the browser had to change for this to work: `site/model3d.html`
already accepts `?project=<url>` and loads whatever it finds there.

---

## Run it

```bash
python3 services/minevis/server.py --state-dir /var/lib/minevis
```

It binds `127.0.0.1:8787`. **The loopback bind is the security model** — the
agent runs on the same box. If the agent lives in a different container on this
host, set `MINEVIS_TOKEN` and have it send `X-MineVis-Token`.

| flag / env | what it does |
|---|---|
| `--host` / `--port` | bind address (default `127.0.0.1:8787`) |
| `--state-dir`, `MINEVIS_STATE` | jobs, parsed specs, and — with no bucket — the models themselves |
| `--workers` | concurrent builds (default 2) |
| `--base-url` | public site URL for the returned links (overrides `NWMM_SITE_URL`) |
| `--zoom` | terrain tile zoom (13 ≈ 15 m at 42°N) |
| `--offline` | never fetch terrain tiles; a mine with no cached tile is refused rather than placed at sea level |
| `NWMM_MODELS_BUCKET` | the S3 bucket to publish into |
| `NWMM_SITE_URL` | public base URL, so `model_url` is clickable |
| `MINEVIS_TOKEN` | shared secret; when set, every route except `/healthz` requires `X-MineVis-Token` |

**With no bucket configured the service still works**: it publishes to
`<state-dir>/models` and serves those files itself at `/models/…`. That is a
complete, provable loop with no AWS involvement at all — the URLs are just
paths under this service rather than CloudFront, and the result says so in its
`storage` and `note` fields.

`systemd`: copy `minevis.service` to `/etc/systemd/system/`, uncomment the two
`Environment=` lines for the bucket, `systemctl enable --now minevis`.

---

## Wiring the agent

```python
import json, urllib.request

BASE = "http://127.0.0.1:8787"

def tools():
    with urllib.request.urlopen(BASE + "/tools") as r:
        return json.load(r)                     # paste straight into `tools=`

def call(name, arguments):
    req = urllib.request.Request(
        BASE + "/call",
        data=json.dumps({"name": name, "arguments": arguments}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

# in the agent's tool-call loop:
result = call(tool_call.function.name, json.loads(tool_call.function.arguments))
```

`GET /tools` returns the OpenAI function-schema array verbatim. Any client that
speaks OpenAI-style tool calling — the Qwen and Kimi variants included — can use
it unmodified.

---

## The five tools

| tool | sync? | what it does |
|---|---|---|
| `mine_lookup` | yes | name → **candidates** from the 3,369-mine grades bundle, with coordinates and citations |
| `parse_mine_description` | yes | prose → typed elements, mentions, quoted grades, vein attitude, and the questions the prose leaves open |
| `check_map_plate` | yes | check a scan's georeference before building with it |
| `sign_model_url` | yes | mint fresh signed links for a private model |
| `build_mine_visual` | **no** | → `job_id`; builds, renders and publishes |
| `get_job` | yes | poll: `queued` \| `running` \| `done` \| `questions` \| `error` |
| `list_mine_documents` | yes | the scanned sources held for a mine, so the agent can read its own prose |

### `mine_lookup` returns candidates, never a pick

There are Bluebirds in four states. Auto-picking would silently georeference the
wrong hole in the ground, so the tool ranks candidates and hands back a
`which_mine` question. **The agent chooses.** A stated `state` rejects
mismatches outright; `district` and `county` only add weight.

An unlocated mine (the bundle has no coordinate) comes back with
`"located": false` rather than being dropped.

### The question round trip

```
agent → build_mine_visual(text="…", mine_id="grades:17")
      ← {job_id}
agent → get_job(job_id)
      ← {state: "questions", spec_id: "s7f9f5e4b",
         questions: [{id:"g1", field:"bearing_deg",
                      question:"No bearing is stated for the drift. What bearing was it driven on?",
                      quote:"On the 300 level a drift was extended 450 feet.",
                      options:[{value:45.0, label:"same as the adit (45°)"},
                               {value:null, label:"unknown — omit this element"}]}]}
agent   (re-reads the source document, decides)
agent → build_mine_visual(spec_id="s7f9f5e4b",
                          answers=[{id:"g1", value:45.0, because:"same vein as the adit"}])
      ← {job_id}
agent → get_job(job_id)
      ← {state: "done", model_url:"https://…", views:{…}, exports:{…},
         confidence:{surveyed:0, described:2, assumed:1}, unresolved:[]}
```

`questions` is a **normal outcome, not a failure.** Old prose is genuinely
ambiguous, and a parser that never asked would be a parser that invented.

A `value` of `null` omits that element rather than guessing it. Whatever the
agent puts in `because` is copied verbatim into `manifest.json`.

---

## What comes back, and how much to trust it

Every element carries the sentence it came from and that sentence's character
span in the input text. Confidence is **per field**:

| | meaning | drawn as |
|---|---|---|
| `surveyed` | traced off a georeferenced plan | solid |
| `described` | read off the source text | dashed |
| `assumed` | supplied in answer to a question | dotted |

An element's own confidence is the weakest of its fields, so one assumed
bearing makes the whole drift dotted.

### Surveyed geometry, and the only way to get it

Prose can never be better than `described`. To get `surveyed`, hand
`build_mine_visual` a `plates` array: a scan, its pixel size, a georeference
(≥ 2 control points `[px, py, lon, lat]`, or one anchor plus a scale bar), the
elevation or level it is drawn at, and the workings traced on it in pixels.
`check_map_plate` validates one first and reports the implied metres-per-pixel
plus, with three or more control points, **how far they disagree** — a large
residual means the plate was tied wrongly, and it is better to know before the
model is built. A plate missing its georeference or its elevation comes back as
a question; it is never draped at zero. Re-sending a plate replaces it.

### Grades

Grades quoted in the same prose come back as `assays`, each carrying its
**basis**: `selected` (picked, bonanza, high-grade), `average`, `shipment`, or
a plain `assay`. That distinction is kept everywhere — a selected sample plots
hollow where an average plots filled — because "selected samples assayed 40
ounces" and "the mill heads averaged 0.4 ounce" are not the same claim about a
deposit. A width travels with the value when the text gives one, and a figure
with no metal named becomes an optional question rather than a guess.

A stated strike **and** dip produce a vein surface. One without the other does
not, and no grade surface is ever interpolated from quoted figures — kriging
three sentences would manufacture a resource out of an anecdote.

Workings the text *names* without describing — "the mine is developed by two
adits and a vertical shaft" — come back as **mentions**, not elements. They
carry their count and their quote, they raise one optional question, and they
are never drawn: an inventory line is not a survey, and promoting one to
geometry would only pile up required questions that the following sentences
already answer. The legend on every view states the
counts, and the subtitle says in words that the drawing came from a
description. **The failure mode this is designed against is a
hand-drawn-from-text adit being read as a survey.**

`manifest.json` sits next to the model and is the audit trail: input text hash,
every quote with its span, per-field confidence, the definitional defaults
(an unqualified "shaft" is vertical by definition of the word), and — listed
separately, because these are the numbers that did *not* come from the
document — every answer the agent gave with its justification.

### Files published per model

```
models/<mine-slug>-<hash8>/
    model.geomodel.json   the viewer opens this: model3d.html?project=<url>
    model.omf             OMF v2.0 — Leapfrog Geo 2025.1+, Seequent Evo
    workings.dxf          DXF R12 — 3-D polylines + stope solids
    workings.geojson      WGS84 footprint for the main map
    plan.svg section.svg iso.svg
    manifest.json
```

`hash8` is content addressed over *(normalised spec + resolved mine + builder
version)*, so **one description always lands on one URL** and rebuilding it
unchanged is a no-op — the result comes back with `"republished": false`.

---

## Honest limits

- **Expect questions on the first pass.** A large fraction of real
  descriptions leave at least one required field open. That is the product
  working, not the parser failing.
- **A phrasing the grammar does not know becomes a question too**, with the
  sentence quoted — never a silent omission. Every parse reports `coverage`,
  so a systematically missed construction is visible.
- **The first build in a district is slow.** Terrain tiles have to be fetched;
  that is why builds are asynchronous.
- **No terrain, no model.** If the tile is unavailable the build is refused
  rather than placed at sea level, because a zero elevation is a lie about a
  mountain.
- **Access.** By default `model3d.html` sits behind the Cognito app gate when
  `auth.json` is present, so a public model inherits that gate. Stated plainly:
  that gates the *app*, not the S3 objects — anyone who constructs the
  `models/<id>/model.geomodel.json` URL can fetch the raw file through
  CloudFront. When that is not acceptable, pass `private: true`.

### Private models

`build_mine_visual(…, private: true, expires_in: 600)` writes the model under
`private/` instead. That prefix is **absent from the CloudFront read allowlist
in `infra/template.yaml` by construction**, so the distribution cannot serve it
at all; the only way in is a signed link, exactly as the WS12 document store
works. The bucket's CORS rule is already bucket-wide and GET/HEAD only with the
signature doing the authorising, so `model3d.html` fetches a signed project
cross-origin unchanged.

Links expire — 300 s by default, clamped to 30–3600, the same range the document
store uses. `sign_model_url(model_id, expires_in)` mints fresh ones without
rebuilding the model. The stored `manifest.json` holds **keys, never URLs**, so
it does not go stale.

Its one AWS requirement: the writer role needs `s3:PutObject` on
`<bucket>/private/models/*`, and whoever signs needs `s3:GetObject` on the same.
No read-policy change is needed — `private/` is already unreachable through
CloudFront, and `deploy.sh` already excludes `private/*` from its `--delete`
site sync, so a private model also survives a deploy today.

---

## What this service does *not* own

The AWS surface belongs to a separate piece of work and must not be edited from
here. What it needs to provide:

1. `models/*` allowed through `SiteBucketPolicy → AllowCloudFrontRead`.
2. An instance-profile role with `s3:PutObject` and `s3:AbortMultipartUpload`
   on `<bucket>/models/*` **only** — no `s3:Delete*`.
3. **`--exclude "models/*"` on both site syncs in `deploy.sh`.** `models/`
   exists only in the bucket, never in `site/`, so the `--delete` sync would
   otherwise remove every agent-generated public model on the next deploy.
   The private prefix is already safe: `sync_public_site_without_pointers`
   excludes `private/*` today, so only the public prefix needs adding.

Until those land, leave `NWMM_MODELS_BUCKET` unset and the service publishes
locally — everything else behaves identically.
