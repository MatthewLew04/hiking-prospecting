# WS9 coverage report — grade enrichment round 2 (California + Idaho)

Run date 2026-08-08. Every figure below is reproducible: sources are cached
as page-indexed text under `pipelines/cache/pagetext/` (committed; the PDFs
themselves re-fetch by URL into `pipelines/cache/pdfs/`), curated rows live
in `grades-research/rows_ca_r2.json` / `rows_id_r2.json`, and
`pipelines/grades_ca.py` + `pipelines/grades_id.py` rebuild the splice from
them idempotently — every quote is re-validated verbatim against its cited
page on every run, and a failed quote aborts the splice.

## Headline

**544 curated rows** (349 CA, 195 ID) extracted from **20 sources** —
**4,092 PDF pages triaged, 1,232 pages flagged by the assay-language sweep,
389 pages actually cited by rows.** Splice result: **+361 new dataset rows**
(291 CA, 70 ID) and **183 enrichment quotes** merged into existing rows by
mine+county key (58 CA, 125 ID); 39 primary-grade upgrades (all revertible;
the displaced quote stays on the row). Dataset: 3,008 → **3,366 rows**
(fresh-base build: 3,369; the 3-row difference is three round-1 CA rows that
merge into round-2 rows on rebuild — deterministic after the first pass).
0 quote-validation failures at splice, 0 cap drops in round 2.

## Per-volume: triaged vs extracted (what's left on the table)

| key | source | pdf pp. | assay-hit pp. | cited pp. | rows | left on table |
|---|---|---|---|---|---|---|
| logan_b108 | Logan 1934, Mother Lode Gold Belt (CDMG B 108) | 294 | 145 | 59 | 93 | ~86 hit-pages unread — El Dorado seam mines, minor Mariposa |
| cjmg29 | CJMG v.29 (1933): Tucker & Sampson Kern + Averill Redding–Weaverville | 496 | 126 | 62 | 90 | non-gold Kern chapters (silver/tungsten credited to SB Co.) |
| cjmg49 | CJMG v.49 (1953): Wright et al. San Bernardino register | 784 | 129 | 41 | 60 | ~88 hit-pages — the register is enormous; Kings/Mendocino chapters untouched |
| csmb_b78 | Bradley 1918, Quicksilver Resources (CSMB B 78) | 458 | 174 | 36 | 39 | county-total tables + minor prospects; Forstner B 27 (1903) cached-not-mined |
| pp73 | Lindgren 1911, Tertiary Gravels (PP 73) | 272 | 65 | 26 | 40 | narrative geology; all value tables captured |
| pp610 | PP 610 district roll-ups (CA + ID chapters) | 290 | 81 | 35 | 48 | other-state chapters (NV/MT/etc. already covered in round 0) |
| igs_b11 | Piper & Laney 1926, Silver City (IBMG B 11) | 98 | 50 | 17 | 27 | mine-by-mine geology; all tenor statements captured |
| b528 | Umpleby 1913, Lemhi County (B 528) | 203 | 70 | 21 | 26 | Eldorado/Pratt Cr./Kirtley Cr. sections carry no figures |
| pp97 | Umpleby 1917, Mackay (PP 97) | 152 | 46 | 7 | 9 | smelter-schedule pages (excluded by spec) |
| b877 | Ross 1937, Bayhorse (B 877) | 187 | 48 | 11 | 14 | Clayton silver mine: described in detail, **no grade figure exists** |
| b969f | Cooper 1951, Stibnite Sb-W-Au (B 969-F) | 49 | 30 | 8 | 9 | drill-log tables (per-hole, sub-row granularity) |
| ismir1915/17/18 | Idaho Mine Inspector annuals — round-2 deeper cuts | 423 | 164 | 35 | 43 | round 1 took 58 rows; near-duplicate restatements deliberately skipped |
| igs_b14 | Anderson 1931, Eastern Cassia (IBMG B 14) — completion sweep | 200 | 23 | 4 | 5 | Miller property / Mineral Gulch / War Eagle Peak prospects have **no numeric figures** (verified page-by-page); Silver Hills 0.03 oz Au print-verified against the page image (tesseract misread 0.08) |
| igs_p26 | Ballard 1928, Rocky Bar quadrangle (P 26) — OCR'd this round | 44 | 32 | 9 | 16 | fills the round-1 "Rocky Bar per-mine grades" gap |
| igs_p49 | Anderson 1939, Atlanta district (P 49) — OCR'd this round | 86 | 47 | 13 | 16 | claim-list pages |
| igs_p61 | Anderson 1943, Blackbird cobalt (P 61) — OCR'd this round | 42 | 20 | 4 | 6 | cobalt-only tenors (no Co field in schema; figures preserved in quotes) |
| igs_p72 | Staley 1945, Snake River fine gold (P 72) — OCR'd this round | 14 | 9 | 1 | 1 | per-county oz totals (no per-mine attribution) |
| web | Liberty Gold 2026 Black Pine MRE news release | — | — | — | 2 | see "not fetchable" below |

## Queue items identified but NOT ingested (and why)

- **Julihn & Horton, Mines of the Southern Mother Lode (USBM Bull. 413 /
  survey parts)** — sole open copy found at UNT Digital Library, which
  refuses non-interactive clients (robots); no Internet Archive copy.
  Logan B 108 + PP 610 cover the same belt at lower per-mine density.
  *Manual download of the UNT PDF would slot straight into the pipeline.*
- **Clark, Gold Districts of California (CDMG B 193)** — the IA copy is
  lending-restricted (in-copyright); respected. PP 610 carries the district
  roll-ups instead.
- **Liberty Gold Black Pine NI 43-101 / S-K 1300 technical reports
  (SEDAR+)** — SEDAR+ and libertygold.com both refuse the sandbox fetcher;
  the 2026 MRE **news release** (verbatim-quoted, URL-cited) carries the
  headline grades. *The PFS PDF (Nov 2024) would add the historic-drilling
  assay leg.*
- **CSMB Report of the State Mineralogist, remaining ~50 volumes** — this
  round mined the two queue-named registers (Kern v.29, San Bernardino
  v.49). The 1880s–1920s annual reports (IA identifiers `annualreportofst*`)
  are the largest untapped CA reservoir; same pipeline applies.
- **ISMIR 1907** — image-only scan AND directory format (round-1 grep
  re-verified: no extractable per-mine figures). Not OCR'd.
- **IBMG P-83 (Yankee Fork)** — image-only; General Custer/Lucky Boy grades
  already carried from USGS B 539. OCR pass possible next round.
- **Coeur d'Alene PP 62** — still the standing Ag-Pb gap (round-1 note);
  belongs to a Coeur d'Alene-focused round with the Ag price table now in
  place.

## Conversions and caps used

Gold $20.67/oz pre-1934, $35.00 1934–71 (per-row `conv` metadata names the
price and era). Silver $-per-ton rows convert by the year of the statement
against the annual-average table in `gradeslib.AG_PRICE` (USGS Historical
Statistics / DS 140 silver series; 10 rows converted this round, e.g. Trade
Dollar 1903-09, Calico 1883-86). Tungsten: 1 unit = 20 lb WO₃ = 1%/short
ton. Quicksilver: production in 76-lb flasks (`hgf`), tenor stays in the
quote. Placer: `plc` flag + `$/yd³` field — never mixed into $/ton. Bonanza
caps (round-0 convention): stated averages >50 oz/t Au and any figure
>610 oz/t Au are dropped at intake (0 hits this round; round-1 CA bonanza
rows are grandfathered per ASSUMPTIONS #36).

## richOpen after the splice (county_gold rerun)

Biggest movers (stakeable rank): **Kern, CA 43→11** (richOpen +15),
**El Dorado, CA 58→13** (+14), **San Bernardino, CA 41→10** (+9, its first
grade rows ever), Shasta 30→20, **Lemhi, ID 7→2** (+7), **Elmore, ID 13→8**
(+6 — the Rocky Bar/Atlanta gap closing), Amador 23→17, Owyhee 74→60.
First-ever grade rows: San Bernardino/Mariposa/Placer (CA), Twin Falls (ID).
CA rich+open: 94 mines ≥0.3 oz/t with no active claim within 400 m.

## Cassia County (acceptance)

**12 rows, all with verbatim quotes**: Silver Hills (full 6-metal assay incl.
4.5% Sb, print-verified), Melcher ×2, Big Bertha, Albion group, Golden Eagle
×2, Valentine cinnabar, Ruth (new — 6 cars zinc carbonate, WWI), Hazel Pine
(new — 14 cars shipped ca. 1914), Black Pine Mine (primary now Liberty Gold
2026 MRE 0.99 g/t high-grade subset; carries the 2026 indicated-resource
quote, the B-14 district-history quote and the MRDS record), Snake River
placers near Milner (new — P-72 black-sand concentrate values, placer-flagged).
