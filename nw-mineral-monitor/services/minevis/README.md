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
| `parse_mine_description` | yes | prose → typed elements + the questions the prose leaves open |
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
- **Access.** `model3d.html` sits behind the Cognito app gate when `auth.json`
  is present, so generated models inherit that gate. Stated plainly, and
  consistent with what `DEPLOY.md` already says: that gates the *app*, not the
  S3 objects — anyone who constructs the `models/<id>/model.geomodel.json` URL
  can fetch the raw file through CloudFront. If that is not acceptable for a
  given model, it needs a private prefix with presigned reads.

---

## What this service does *not* own

The AWS surface belongs to a separate piece of work and must not be edited from
here. What it needs to provide:

1. `models/*` allowed through `SiteBucketPolicy → AllowCloudFrontRead`.
2. An instance-profile role with `s3:PutObject` and `s3:AbortMultipartUpload`
   on `<bucket>/models/*` **only** — no `s3:Delete*`.
3. **`--exclude "models/*"` on both site syncs in `deploy.sh`.** `models/`
   exists only in the bucket, never in `site/`, so a `--delete` sync would
   otherwise remove every agent-generated model on the next deploy.

Until those land, leave `NWMM_MODELS_BUCKET` unset and the service publishes
locally — everything else behaves identically.
