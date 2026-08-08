# WS9 curated-row spec (grade enrichment round 2 — CA + ID)

One JSON object per extracted statement. Emit a JSON **array** of these.
Every numeric field null/omitted unless the SOURCE states it.

```json
{
  "name": "Kenyon mine",              // as the source names the mine; add a
                                       // parenthetical qualifier if the row is
                                       // about a specific shoot/level/lot,
                                       // e.g. "Melones mine (4,125-foot level)"
  "district": "Rand (Kern)",          // district (county) — human label
  "county": "Kern",                   // REQUIRED whenever the chapter/context
                                       // gives it (county registers always do)
  "state": "CA",                      // CA or ID
  "keys": ["kenyon"],                 // lowercase match keys for MRDS lookup
  "excl": [],                          // substrings that must NOT be in a match
  "lat": null, "lon": null,           // only if the source itself locates the
                                       // mine (T/R/S is fine to leave null)
  "anchor_hint": "Randsburg, Kern Co.",// nearest named town/camp for anchoring
  "metal": "Au",                      // which metal a $-per-ton figure is in
                                       // (Au default; "Ag" for silver-ore $/t)
  "au_opt": null,                     // oz/troy per short ton, stated directly
  "ag_opt": null,
  "au_gpt": null,                     // modern g/t (Liberty Gold etc.)
  "usd_per_ton": 100.0,               // historic $ per ton, NOT converted
  "pb_pct": null, "zn_pct": null, "cu_pct": null, "sb_pct": null,
  "wo3_units": null,                  // 1 unit = 20 lb WO3 = 1% per short ton
  "hg_flasks": null,                  // quicksilver PRODUCTION, 76-lb flasks
  "usd_per_yd3": null, "plc": null,   // placer: $ per cubic yard + flag 1
  "basis": "value-text",              // production average | production |
                                       // assay | assay-text | value-text |
                                       // ore shipped | resource estimate |
                                       // district production
  "years": "pre-1910",                // era of the figure, as source implies
  "price_year": 1910,                 // year for $-conversion price lookup
  "tonnage": null,                    // production/tonnage note if stated
  "commodities": "Gold",             // commodity set named by the source
  "src_key": "cjmg29",               // pagetext cache key
  "page": 40,                          // PRINTED page number (the citation)
  "pdf_page": 60,                      // pdf page index the quote sits on
  "quote": "One lens in the Kenyon was 10 feet thick and averaged $100 to the ton."
}
```

## Hard rules (identical to rounds 0-1 — see sources_id_or.md)
- **Quote is VERBATIM from the cached page text.** Join hyphenated line
  breaks, collapse whitespace, drop obvious mid-quote artifacts (page
  headers) — change NOTHING else. Never paraphrase, never "fix" OCR beyond
  the substitutions already sanctioned (AI;say→Assay class). The pipeline
  validates every quote against the cited page and DROPS failures.
- **Ranges record the lower bound**; the range stays in the quote.
- **Bonanza/specimen values**: keep, basis `assay-text` / `ore shipped`;
  derived lot arithmetic allowed when dividend+divisor are in the quote.
- **Concentrates ≠ crude ore**: the quote must say it; note in basis/name.
- **Averages > 50 oz/t Au are unit errors** — do not emit; anything
  > 610 oz/t Au is dropped by the pipeline.
- **$ figures stay in historic dollars** in `usd_per_ton` (+`price_year`);
  conversion happens in the pipeline ($20.67 pre-1934 / $35 1934-71; Ag by
  annual-average table).
- **Placer** ($/yd³, cents/yd) → `usd_per_yd3` + `plc:1`, never `usd_per_ton`.
- One row per mine per distinct statement kind; ≤2 rows per mine unless the
  statements are genuinely different figures (e.g. mill average + bonanza lot).
- Skip: county/state totals with no mine attached, mill costs, smelter
  schedules, dump samples with no named mine, "reported values" with no
  number, patents/claims lists.
- District production roll-ups (PP 610, Clark-style) ARE wanted:
  `name: "<X> district"`, basis `district production`.

## Per-mine target
Prefer the statement highest on: production average > ore shipped/mill run >
assay (engineer's sampling) > value-text ("said to carry") > assay-text
(specimen). Capture tonnage context into `tonnage` when adjacent.
