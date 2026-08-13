# WS9 national grade-evidence publication

`pipelines/build_national_grade_evidence.py` is the evidence-publication
foundation for the 49-state WS9 grade leg. It compiles reviewed inputs; it does
not research mines, infer missing grades, update `states/*.yaml`, toggle a
release, or add a browser layer.

## Publication contract

The build requires one private inventory outside `site/`. The inventory must
contain exactly the current 49-state registry (all states except Hawaii), one
grade-evidence JSON and one PP 610 JSON per state, and a separately reviewed
price configuration. Every input descriptor carries exact `path`, `bytes`, and
`sha256`; absolute paths, traversal, symlinks, checksum changes, duplicate JSON
keys, `NaN`, and files under the public site tree fail closed.

The compiler writes JSON only:

```text
<publish>/
  latest.json
  runs/<run-sha256>.json
  states/<state>/<state-evidence-sha256>.json
```

The state and run filenames are hashes of canonical JSON. `latest.json` is an
atomic pointer. Inputs are rehashed before immutable installation and again
before pointer replacement, so a changed input can leave only harmless orphan
blobs, never a pointer to a mixed build. All output declares
`effect: evidence_only_no_release_mutation`.

Run a progress publication with:

```bash
python3 pipelines/build_national_grade_evidence.py \
  --inventory /private/ws9/inventory.json \
  --publish /reviewed/ws9-grade-evidence
```

Progress publication is intentionally allowed to show incomplete states. To
use the compiler as a DONE-gate precheck, add `--require-done`; this fails
without replacing `latest.json` if any state lacks both accepted alternatives.
It still does not enable a state.

## Release DONE-gate handoff

A release does not derive grade acceptance from the legacy browser aggregate
`site/data/grades/grades.json`. Copy the selected compiler state blob unchanged
below the immutable release prefix and point the state registry at it:

```json
{
  "release": {
    "acceptance": {
      "grades": {
        "evidence_artifact": "map-assets/releases/grade-evidence/states/nv/<sha256>.json",
        "sha256": "<sha256>",
        "bytes": 12345,
        "graded_mines": 25,
        "primary_sources": 2,
        "verbatim_quotes": 25,
        "page_cites": 25,
        "low_endowment_finding": null
      },
      "district_anchor": {
        "source_id": "pp610",
        "artifact": "map-assets/releases/grade-evidence/states/nv/<sha256>.json",
        "source_sha256": "<PP-610-document-sha256>",
        "district_count": 1,
        "complete": true,
        "no_district_finding": null
      }
    }
  }
}
```

The artifact basename must be its exact SHA-256. The release validator parses
strict canonical JSON, checks its current registry/state/dataset identity, and
recomputes mine, source, quote, page-cite, and PP 610 counts from the embedded
rows. Registry counters cannot exceed or differ from those computed values.
Primary-source URLs and document/page hashes, normalized measurements, and an
optional two-source low-endowment finding are revalidated. The PP 610 anchor is
consumed directly from the artifact's nested `pp610` object, so no manual
schema translation or second evidence file is permitted. For a reviewed zero
district result, `district_anchor.no_district_finding` must equal the nested
finding text.

Disabled/building states may retain the null defaults. Evidence becomes
mandatory only when a state is evaluated as a release candidate; copying a
blob never toggles `release.enabled` or changes the browser manifest.

## State evidence

Each graded mine has a stable state-scoped `mine_id` and one or more unique
evidence rows. Each row must identify a declared primary source and preserve:

- a verbatim quote and `quote_verbatim: true`;
- a numbered page, plate, or sheet cite;
- SHA-256 identities for the source document, page index, and cited page text;
- at least one native grade or historic nominal-dollar-per-short-ton value.

Duplicate source IDs, mine IDs, normalized mine-name/district pairs, evidence
IDs, source/page/quote triples, and commodity values within an evidence row are
rejected. Declared but unused sources are also rejected so source counts cannot
be inflated.

The state metrics are computed, never supplied:

```text
graded_mines
primary_sources
verbatim_quotes
page_cites
pp610_districts
primary_source_ids
```

A state is evidence-eligible only when it has at least 25 distinct graded mines
from at least two used primary sources, with quote and page-cite counts at least
equal to its mine count, or when it has a separate checksum-pinned low-endowment
finding based on at least two unique primary sources. Otherwise its output is
`incomplete` with explicit gaps. Zero is never silently treated as a finding.

## Commodity normalization

The reviewed price config must cover exactly Au, Ag, Cu, Pb, Zn, and Fe with
contiguous annual values, primary-source identities, review date, and reviewer.
Historic conversions require an exact configured year; the compiler does not
substitute a nearest year.

Canonical grade units are:

| Commodity | Canonical unit | Accepted native units | Historic price unit |
|---|---|---|---|
| Au, Ag | troy ounces per short ton | oz/short ton; grams/metric tonne | nominal USD/troy oz |
| Cu, Pb, Zn, Fe | percent contained metal | percent; ppm; pounds/short ton | nominal USD/lb contained metal |

Historic value per short ton is divided by the reviewed commodity price. For
base metals, one weight percent in a short ton is exactly 20 pounds, so the
formula is `USD/t ÷ (20 × USD/lb)`. Every normalized output retains the input,
formula, price year/value/unit, price-source ID, and price-config SHA-256.

Iron is deliberately strict: an ore price quoted in dollars per long ton is
not interchangeable with dollars per pound of contained Fe and is rejected.
No production Fe normalization should be published until a reviewer supplies
the required compatible annual series.

## PP 610 anchor

Every state must carry a complete PP 610 artifact tied to the reviewed PP 610
document and page-index hashes. Each extracted district needs a unique ID,
name, page cite, verbatim quote, and cited-page hash. A state with no extracted
districts must instead include a 40-character-or-longer finding, the unique
page ranges reviewed, and `review_complete: true`. An empty district list by
itself fails.

## Current production blockers

This foundation intentionally ships without fabricated state evidence. A
production national inventory still needs all 49 reviewed state grade and PP
610 artifacts, and the existing repository price inputs do not yet provide a
single reviewed six-commodity series compatible with this contract—most
notably Fe in dollars per pound of contained metal. Until those inputs exist,
the compiler can validate fixtures and honest progress inventories but cannot
make any state DONE.
