# The Grade Hunt — unearthing oz/ton for the Great Basin, plan & status

*The goal: make "sort historic gold mines by oz per ton, show me the unclaimed ones" a one-line query. This document is the plan, what's already implemented, and what genuinely requires a human.*

## What's live now (Phase 1–3, done 2026-07-31)

The site now carries a **cited ore-grade dataset: 2,948 historic mines** across WA/OR/ID/MT/WY plus Nevada and Utah (the Great Basin core), 2,819 of them mapped, every grade backed by a verbatim quote and a source link. It was built three ways at once. First, the **USGS MRDS relational dump** (already in this project's raw archive) was mined table by table — `Production_detail` and `Resource_detail` carry literal grade columns nobody surfaces, and 64 MB of free-text comments plus the assay table yielded thousands more via pattern extraction with unit normalization (g/t, ppm, percent → troy oz/short ton) and sanity ceilings (gold "averages" above 50 oz/t are unit errors; text values above the 610 oz/t historical specimen record are dropped). Second and third, **two library sweeps over digitized archives**: OCR'd USGS Professional Papers and Bulletins (PP 66 Goldfield alone yielded a dozen mines), USBM Information Circulars and *Mineral Resources of the U.S.* annuals on archive.org, DOGAMI's Oregon Metal Mines Handbooks, and the Idaho Mine Inspector reports already scanned in this repo — 370 hand-verified records with page-level citations, from the Comstock to Cassia County (Silver Hills: 0.03 oz Au / 42.4 oz Ag, Anderson 1931).

Every mapped grade is then **cross-referenced against the live BLM claims snapshot**: `open` = metres to the nearest active claim. That makes the original query real: **193 mapped gold mines grade ≥0.3 oz/t with no active claim within 400 m.** On the site: the gold-ramp "Cited ore grades" layer (brighter/bigger = richer), click any dot for the grade, the quote, the source, and an OPEN GROUND badge; in ASK, try *"sort gold sites by highest oz per ton," "unclaimed gold mines with the highest grades in idaho,"* or *"highest silver grades near oakley"* — and the AI answerer gained a `query_grades` tool for anything fancier.

**Read grades like a prospector, not a promoter.** "Assay-text" values are usually hand-picked specimens — the Mohawk's 580 oz/t was a bonanza pocket, not the mill feed. Production averages are the trustworthy numbers. The quote travels with every record precisely so you can judge. Dollar-per-ton figures are historic dollars ($20.67/oz gold before 1934, $35 after) and are stored unconverted with their era. And open-ground is a snapshot-based screen, not a title search — verify at the county recorder and check withdrawals before staking anything.

## What I could not do, by design

No phone calls (no such capability), no account creation or credential automation, no scraping past bot-blocks or paywalls (NBMG's district-file viewer and HathiTrust's reader both refuse non-interactive clients — respected). Everything above came from openly accessible archives, which turned out to hold far more than expected.

## The human packet (Phase 4 — this part is yours)

The two `grades-research/sources_*.md` files each end with a **requires-physical-visit table**. The highest-value targets, in order: (1) **NBMG county bulletins** — modestly priced print/digital products whose per-mine grade tables are the single richest untapped source for Nevada; buying the 4–5 counties you care about beats any amount of scraping. (2) **NBMG Mining District Files** (data.nbmg.unr.edu) — browse interactively; each district folder holds scanned assay maps and reports. (3) **Idaho State Archives** — Mine Inspector working files for the ~35 report years never digitized. (4) **County recorders** (Cassia County: Burley) — pre-1976 claim location books, the only record of exactly where 19th-century claims sat. (5) **UNR / UI / UO Special Collections** — Grant Smith's Comstock records, Callaghan's Delamar work, DOGAMI's MILO mine files. When you call or visit, ask for "mining district files / mine inspector mine files / claim location indexes for <district>, <years>" and request scan-on-demand — most state archives offer it cheaply.

## Next machine phases (say the word)

**Phase 5 — full NV/UT layers:** promote Nevada and Utah from grades-only to full first-class states (MRDS + USMIN site layers from the national dumps on disk; the big lift is claims — Nevada has ~400k active, a multi-hour pull that also grows the nightly Lambda). **Phase 6 — deeper text mining:** Coeur d'Alene PP 62, Lindgren's 22nd Annual Report (Blue Mountains), and OCR of the image-only IBMG pamphlets via the pdf pipeline. **Phase 7 — old maps:** a historical-topo basemap (USGS NGMDB tile services) so the 1900s sheet sits under the grade dots. **Phase 8 — refresh automation:** fold grade rebuilds into the deploy script.

*One correction to the brief: the Great Basin's mining record starts with the 1859 Comstock strike — the 1840s traffic through the Basin was emigration, and its paper trail (diaries, wagon-train ledgers) predates the assay record.*

## Files

`site/data/grades/grades.json` (the dataset the site reads) · `grades-research/raw_greatbasin_nvut.json`, `raw_id_or.json` (370 library records, page-cited) · `raw_mrds.json` (5,262 dump extractions) · `sources_nvut.md`, `sources_id_or.md` (annotated bibliographies + visit lists) · `extract_mrds_grades.py`, `build_grades.py` (the pipeline — rerun after any raw-file addition).
