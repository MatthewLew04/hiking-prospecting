# AUTOPOPULATE — every mine the corpus describes, modelled and on its card

`pipelines/geomodel_autopopulate.py` walks the WS12 document store, connects
each document to the mine it describes, carves out the stretch of text that
is about that mine, parses it with the geomodel narrative engine, and
publishes a Leapfrog-style 3-D model per (mine, document) — the same
artifact, at the same kind of URL, that the minevis agent service publishes
one at a time. The map then knows: a graded mine's card shows **what the
documents say is underground** (minerals, workings by type and length,
levels and depth, the underground vocabulary the text uses), lists the
source documents, and its **OPEN 3D MODEL — DESCRIBED WORKINGS** button
opens the pregenerated model with the workings under the terrain.

```
document store          geomodel_corpus.py           geomodel_autopopulate.py
(25 docs, 3,763 pages)  ├ page_texts   (fitz, cached)  ├ narrative.parse + assay.attach
var/ws12/…manifest.json ├ build_bridges (doc→mine)     ├ the omit answer policy
pipelines/cache/ws12/   ├ sections     (per-mine text) ├ minevis tools.run_build
store/…searchable.pdf   └ assignments  (work units)    ├ publish → site/models/<id>/
                                                       └ site/data/models/index.json
```

Run it:

```bash
python3 pipelines/geomodel_autopopulate.py             # everything
python3 pipelines/geomodel_autopopulate.py --dry-run   # parse + report, no builds
python3 pipelines/geomodel_autopopulate.py --only grades:44 --no-context
python3 pipelines/geomodel_kit.py autopopulate -- --dry-run   # same, via the kit CLI
npm run test:autopop                                   # the contract, end to end
```

## 1. Connecting documents to mines — the bridge never guesses

The store names mines in its own namespaces (`ws9-*` reviewed-evidence ids,
`stategeo-*` state-survey records, `district-*` containers); the modeller
builds only from a grades-bundle row (`grades:N`) or a bare coordinate.
`geomodel_corpus.build_bridges` translates with tiered, recorded methods —
strongest first, and anything ambiguous is **parked with a reason, never
chosen**:

| method | how a document mine becomes buildable |
|---|---|
| `citation_quote` | the reviewed citation's verbatim quote equals exactly one grades row's quote — per **citation**, because a district report's citations each name their own mine |
| `evidence_name` | the `ws9-*` id resolves through `grades-research/*/reviewed_grade_evidence.json` to a name+state matching exactly one bundle row |
| `subject_lookup` | the document subject's label+state gives `resolve.lookup` a single unambiguous, located, exact candidate |
| `stategeo_site` | a `stategeo-*` id matches a state-survey site record with coordinates (built by lon/lat) |

One physical mine is several bundle rows ("Victoria mine (1,050-foot
level, rich ore)" and "(shipping ore)" are one hole in the ground), so rows
sharing a normalised name + state collapse to one **canonical located row**;
the other rows become `{"alias": …}` entries in the index so a click on any
of them finds the model. The parked list — no bundle row, unlocated, tier
conflict, district containers — is in the ledger, entry by entry.

## 2. Carving the mine's own text — attribution before recall

A district report describes forty mines; feeding a page to the parser would
pin one mine's adit on another. `geomodel_corpus.sections` takes
name-anchored windows: a window starts where the mine's name (or a located
citation quote) appears and ends at the first mention of a *different*
subject of the same document — or of any `<Name> mine`-style phrase that is
not the target, so mines the store never registered still cut the window.
Tables of contents mention every mine and describe none, so a window
survives only if a real sentence *describes* development (a mining noun
plus a development verb or a measurement, not leader-dotted index lines).
Both filters err toward losing text rather than misattributing it.

## 3. Building — the service's own path, with an audited answer policy

Each (mine, document) text runs through `services/minevis/tools.run_build`
— the exact code the agent service runs, in process. Historic prose
reliably leaves required fields open, and the parser never invents; the
unattended policy is **omit**: every open question is answered
`value: null` ("omit this element rather than guess it"), which is a legal,
audited answer. Every omission lands in the model's `manifest.json` under
`answers` with this module's justification string, so an auditor sees
exactly which elements the text could not support. A description that omits
to nothing publishes nothing; a mine whose text yields no elements is
recorded, not modelled; described geometry draws dashed in the viewer and
the NOT-A-SURVEY banner states the counts.

Levels of output, all recorded in `var/geomodel/autopopulate-ledger.json`:

| outcome | meaning |
|---|---|
| `done` | model published under `site/models/<slug>-<hash8>/` |
| `skipped: no-elements` | documents attached, text has no parseable workings |
| `skipped: all-elements-omitted` | everything the text half-described was omitted |
| `error: unplaceable` | no coordinate or no terrain tile — never sea level |
| parked | the bridge could not name a buildable mine without guessing |

Publishing is content-addressed (same description ⇒ same URL; republishing
unchanged is a no-op), and a full run prunes model directories the fresh
index no longer references.

## 4. What the map reads — `site/data/models/index.json` (schema 2)

The index is **compact** so it scales from 16 mines to tens of thousands:
one short row per mine (about 150 bytes, under 256 at every cap) and the
full record in a `card.json` beside each model, fetched only when a card
opens. `geomodel_autopopulate.write_index(results, site_dir, previous)` writes
both; the WS13 driver (`WS13-GEOMODEL.md`) calls the same function.

```jsonc
{"schema_version": 2, "generated": "…", "stats": {…}, "by_mine": {
  "grades:12":  {"l": "Tonopah Divide mine",          // label
                 "p": "tonopah-divide-mine-4c141151",  // primary model id
                 "n": 1,                               // models for this mine
                 "m": ["Gold", "Silver"],              // minerals / commodities (≤ 6)
                 "x": 1040,                            // total workings length, m
                 "w": 7,                               // elements drawn
                 "c": [7, 0, 0]},                      // described, assumed, surveyed
  "grades:868": {"a": "grades:12"}                     // alias: the same mine
}}
```

`site/models/<model_id>/card.json` carries what the compact row does not:
the source documents (title, url, year, cited pages), every model's
confidence, omitted count, summary, levels and level depths, assay
commodities and assays, the vein, the composition (minerals by level, the
commodities they imply, the placed points), the lexicon, the extent and the
methods that linked the document to the mine.

Keys are namespaced by the dot they belong to — `grades:<row>`,
`stategeo:<id>`, `mrds:<dep_id>`, `usmin:<fid>`, `ardf:<id>` — and the map's
MRDS, USMIN, state-survey, ARDF and graded-mine cards all look themselves up.
`site/index.html` fetches the index once, renders the **UNDERGROUND — FROM
THE DOCUMENTS** section from the compact row at once (minerals, extent,
counts, the OPEN 3D MODEL — DESCRIBED WORKINGS button), then fetches the
card to fill the documents, levels and assays, with a placeholder while it
loads and a message naming the file if it fails. A mine with documents but no
buildable description says so: *nothing is drawn rather than guessed.* When
more than one document describes a mine, each gets its own model and the
strongest (most described elements, then total length, then newest) is `p`.
A schema-1 index still renders (the rows are read inline) until the next run
rewrites it.

`narrative.lexicon(text)` is the vocabulary half: a deterministic census of
the workings words a description uses (surface forms by canonical kind,
mining verbs, level labels), separate from `parse()` which turns words into
elements and questions. `geomodel/composition.py` is the mineral half: every
ore, gangue, alteration and host term the text names, with its quote, the
level it is tied to and the commodity it implies.

## 5. Honest limits

* **Only text on this machine.** The WS12 citation store (25 documents,
  3,763 pages, rights-resolved, with searchable text layers) is the corpus;
  the 56,282-document WS13 corpus has no local text (in-VPC Postgres, S3
  sidecars) and no admitted front-end↔corpus bridge for most ids, so it is
  a *source* this driver is shaped to accept later — `assignments()` is the
  seam — not one it reads today.
* **22 located buildable mines** is what the store + bundle joins support;
  the 177 parked ids are listed with reasons, and most are mines the grades
  bundle simply does not carry.
* The window cutters favour attribution over recall: prose after an early
  false boundary is lost, and a mine described only in an unregistered
  neighbour's section stays unmodelled.
* A described model is a **digitising bridge, not new evidence** — the same
  rule as the rest of GEOMODEL.md §4. Everything here inherits it: quotes
  and spans on every element, per-field confidence, dashed rendering,
  omissions listed separately from the document's own numbers.

Tests: `tests/test_geomodel_corpus.py` (bridge and sectionizer),
`tests/test_geomodel_autopopulate.py` (policy, index, idempotence),
`tests/test_geomodel_lexicon.py`, `tools/test_autopop_frontend.mjs`
(card contract + published-index coherence) — `npm run test:autopop`.
