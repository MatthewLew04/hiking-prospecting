# Mine visuals — a quick how-to

Turn a written description of a mine into a 3-D model, a plan, a section and an
isometric — from the command line, or from an agent over HTTP.

Nothing here needs AWS. Nothing here changes the website.

---

## The 60-second version

```bash
python3 pipelines/geomodel_kit.py mines "White Caps" --state NV
python3 pipelines/geomodel_kit.py narrate --mine-id grades:17 --out build/ \
  --text "The Main shaft was sunk on the vein to a depth of 620 feet. The No. 2 adit, driven N45E for 900 feet, cuts the vein at the 300 level."
```

You get `build/model.geomodel.json` (drop it on `model3d.html`, or open
`model3d.html?project=<url>`), `model.omf` for Leapfrog, `workings.dxf`,
`workings.geojson`, and `plan.svg` / `section.svg` / `iso.svg`.

---

## Step by step

### 1. Find the mine

Historic mining names collide constantly, so this **always returns candidates
and never picks one**. Choosing the wrong one georeferences the wrong hole in
the ground.

```bash
python3 pipelines/geomodel_kit.py mines "White Caps" --state NV
```

```
3 candidate(s) for 'White Caps'  [AMBIGUOUS - choose one]
  grades:17      White Caps mine                     NV, Manhattan
                 https://pubs.usgs.gov/bul/0723/report.pdf
  grades:18      White Caps mine (upper-level ore analyses)   NV, Manhattan
  grades:2460    White Caps Mine                     NV, Nye
```

Take the `grades:N` id of the one you mean. `--state` rejects mismatches;
`--district` and `--county` only add weight. If your mine isn't in the bundle,
skip this and pass coordinates instead (see below).

### 2. See what the description will produce

```bash
python3 pipelines/geomodel_kit.py narrate --file description.txt
```

This is offline, instant, and builds nothing. It shows the workings it found,
the grades it found, and — the important part — **the questions the text does
not answer**:

```
  e3   drift                described   length_m=137.16  level=300
       "On the 300 level a drift was extended 450 feet;"
  a1   au    0.5 oz/ton average    across 0.91 m
  g1   REQUIRED  No bearing is stated for the drift. What bearing was it driven on?
         45.0                         same as the adit (45°)
         null                         unknown — omit this element
```

**Expect questions.** Old prose is genuinely ambiguous. A parser that never
asked would be a parser that invented.

### 3. Answer them

Write a small JSON file. `because` is copied verbatim into the audit manifest.

```json
[{"id": "g1", "value": 45.0, "because": "the drift follows the same vein as the adit"}]
```

`"value": null` **omits** that element rather than guessing it.

### 4. Build

```bash
python3 pipelines/geomodel_kit.py narrate --file description.txt \
  --mine-id grades:17 --answers answers.json --out build/
```

```
built 3 of 3 elements at White Caps mine (grades:17)
  shaft     Main             the collar
  adit      No. 2            the collar
  drift                      the Main at the 300 level

confidence                 {'surveyed': 0, 'described': 2, 'assumed': 1}
```

Useful flags: `--context` adds terrain, draped geology and grade points (slower,
fetches tiles) · `--offline` never fetches tiles, and refuses rather than
guessing when one is missing · `--json` prints the whole result as JSON
(elements, grades, coverage, confidence, unresolved questions, content hash).

The CLI writes files to `--out`; it does not publish. Publishing — and the URLs
that come with it — is what the service does.

---

## Reading the result — what to trust

This is the part that matters. The line style is not decoration:

| | means | drawn |
|---|---|---|
| `surveyed` | traced off a georeferenced plan or section | **solid** |
| `described` | read off the source text | **dashed** |
| `assumed` | you or an agent answered a question | **dotted** |

An element's confidence is the **weakest of its fields**, so one assumed bearing
makes the whole drift dotted. Every legend states the counts, and every drawing
says in words that it came from a description.

`manifest.json` is the audit trail. Every element carries the sentence it came
from and that sentence's character span in your input, so any number in the
model can be traced back to the words that produced it. Answers are listed
**separately** from elements — that list is exactly the set of numbers that did
*not* come out of the document.

A few things are *definitional* rather than invented: an unqualified "shaft" is
vertical because that is what the word means. Those are named per element in
`defaults`.

### Things it will not do

- **No terrain, no model.** If the elevation tile is unavailable the build is
  refused rather than placed at sea level.
- **Mentions are not workings.** "developed by two adits and a vertical shaft"
  names workings without describing any; they are counted and quoted, never
  drawn.
- **No invented grade surface.** Grades are points. Interpolating a surface from
  three quoted sentences would manufacture a resource out of an anecdote.

---

## Grades

Grades quoted in the same prose are read automatically, and two things travel
with the number:

- **basis** — `selected` (picked, bonanza, high-grade), `average`, `shipment`,
  or plain `assay`. "Selected samples assayed 40 ounces" and "the mill heads
  averaged 0.4 ounce" are not the same claim, so they do not plot the same:
  **filled = representative, hollow = selected sample.**
- **width** — a bonanza figure over eight inches is not a mining grade.

A figure with no metal named ("assays ran 30 ounces to the ton") becomes an
optional question rather than a guess. A total ("the group yielded $1,000,000")
is production, not a grade, and is ignored.

State a **strike and a dip** and you also get a vein surface. One without the
other does not define a surface, so you get nothing.

---

## Surveyed geometry (tracing a plate)

Prose can never be better than `described`. To get `surveyed`, trace a plan or
section whose georeference can be checked. Check it first:

```bash
curl -s localhost:8787/call -d '{"name":"check_map_plate","arguments":{"plate":{
  "plate_id":"p3","image":"plate3.png","width":1000,"height":800,"plane":"plan",
  "control":[[100,700,-116.87,36.876],[900,700,-116.86,36.876],[100,100,-116.87,36.882]],
  "level":"300","elevation_m":1025.0,
  "source":{"doc":"USGS Bulletin 723","page":"147","figure":"Plate 3"}}}}'
```

It reports the implied metres-per-pixel and, with three or more control points,
**how far they disagree**. A large residual means the plate was tied wrongly —
better to know before you build on it. Then pass the plate (with `traces`) to
`build_mine_visual`.

A plan needs an elevation or a level; without one it is a question, never a plan
draped at zero. A level elevation read off a surveyed plate **overrides** the
"300 level = 300 ft below the collar" convention, and says so in the warnings
when the two disagree.

---

## Running the service (for an agent)

```bash
python3 services/minevis/server.py --state-dir /var/lib/minevis
```

Binds `127.0.0.1:8787` — the loopback bind is the security model. `GET /tools`
returns an OpenAI function-schema array you paste straight into the agent's
`tools`.

**With no `NWMM_MODELS_BUCKET` set it publishes to local disk and serves the
files itself**, so the whole loop works with no AWS at all.

The round trip:

```
build_mine_visual(text=…, mine_id="grades:17")   -> {job_id}
get_job(job_id)   -> {state:"questions", spec_id:"s7f9f5e4b", questions:[…]}
build_mine_visual(spec_id="s7f9f5e4b", answers=[{id:"g1", value:45.0, because:"…"}])
get_job(job_id)   -> {state:"done", model_url:"https://…", confidence:{…}}
```

`questions` is a normal outcome, not a failure.

`services/minevis/README.md` has the full tool reference.

---

## Private models

By default a model goes to the public `models/` prefix: it inherits the Cognito
**app** gate, but anyone who can construct its URL can fetch the raw file. If
that is not acceptable:

```
build_mine_visual(…, private: true, expires_in: 600)
```

It is written under `private/` — absent from the CloudFront read allowlist by
construction — and comes back as short-lived signed links. When they expire:

```
sign_model_url(model_id="silver-king-9f2c1e0a", expires_in: 300)
```

Links are clamped to 30–3600 seconds, the same range the document store uses.

---

## Common questions

**"Why is everything dashed?"** Because it was read from prose. That is the
point — a hand-drawn-from-text adit must never pass for a survey at a glance.

**"It asked me five questions for one paragraph."** Normal. Answer the ones you
can and `null` the rest; nulled elements are left out rather than guessed.

**"The same description built twice gave the same URL."** By design — model ids
are content-addressed over the spec, the mine and the builder version, so an
unchanged rebuild is a no-op.

**"A sentence was ignored."** It wasn't. A phrasing the grammar doesn't know
becomes a question with the sentence quoted, and every parse reports coverage so
a systematically missed construction is visible.

**"Can I put a model on the public map?"** `workings.geojson` is the WGS84
footprint for exactly that.

---

*Never enter adits or shafts.*
