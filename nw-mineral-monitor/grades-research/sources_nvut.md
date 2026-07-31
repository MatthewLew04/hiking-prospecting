# Annotated Source List — Historic Great Basin Ore Grades (NV & western UT)

Companion to `raw_greatbasin_nvut.json` (157 cited mine-grade records: 128 NV, 29 UT, 67 districts).
All sources below are fully digitized, open-access, and were downloaded/searched directly (no accounts, no paywalls).
Compiled 2026-07-31.

Conventions used in the dataset: dollar values recorded exactly as stated, tagged
`pre1934($20.67/oz)` or `1934-1971($35/oz)` by production date; nothing converted; every record's
`quote` is a verbatim (OCR-faithful) snippet containing the number(s); coordinates (49 of 157 records)
come from a name+county match against the USGS MRDS national dump (2023 CSV) and only where the match was unambiguous.

## 1. USGS Professional Papers (pubs.usgs.gov — OCR'd PDFs, public domain)

| Doc | What it is | URL | Yield |
|---|---|---|---|
| PP 66 (Ransome, 1909) | Goldfield: the definitive camp monograph; per-mine shipment assays and the famous 1907 Hayes-Monnette carload (609.6 oz Au/ton) | https://pubs.usgs.gov/pp/0066/report.pdf | 13 records — richest single document |
| PP 42 (Spurr, 1905) | Tonopah geology; early shipping-ore cutoff; Comstock comparison footnote ($80/ton C&C bonanza) | https://pubs.usgs.gov/pp/0042/report.pdf | 2 |
| PP 104 (Bastin & Laney, 1918) | Tonopah ore genesis; district $/ton 1913-15 and rich-ore assay table (North Star, West End) | https://pubs.usgs.gov/pp/0104/report.pdf | 3 |
| PP 406 (Nolan, 1962) | Eureka district; Richmond 1870s-80s values, 1950s Consolidated Eureka & Eureka Corp. grades | https://pubs.usgs.gov/pp/0406/report.pdf | 4 |
| PP 171 (Westgate & Knopf, 1932) | Pioche; Combined Metals ore body (constant 7 oz Ag), Prince mine tonnage | https://pubs.usgs.gov/pp/0171/report.pdf | 2 |
| PP 431 (Hotz & Willden, 1964) | Osgood Mountains quad; Getchell mine gold grades (Joralemon data) | https://pubs.usgs.gov/pp/0431/report.pdf | 1 |
| PP 111 (Butler et al., 1920) | Ore Deposits of Utah — statewide compendium; Mercur mill averages, Ophir, whole Deep Creek/Clifton (Gold Hill) chapter | https://pubs.usgs.gov/pp/0111/report.pdf | 10 |
| PP 107 (Lindgren & Loughlin, 1919) | Tintic; district 52.5 oz Ag average, Victoria 6,980 oz Ag assay, Mammoth, Centennial Eureka | https://pubs.usgs.gov/pp/0107/report.pdf | 7 |
| PP 173 (Gilluly, 1932) | Stockton & Fairfield quads: Ophir district mines + Mercur (Sacramento mine) | via archive.org: https://archive.org/details/UsgsPp173StocktonFairfield (pubs.usgs.gov copy lacks direct PDF) | 4 |
| PP 177 (Nolan, 1935) | Gold Hill (Clifton) UT; per-shipment smelter grades (Alvarado 7 oz Au shipments) | https://pubs.usgs.gov/pp/0177/pdf/pp177.pdf | 3 |
| PP 77 (Boutwell, 1912) | Park City; Ontario/Anchor/Daly production stats, vein shipments | https://pubs.usgs.gov/pp/0077/report.pdf | 5 |

## 2. USGS Bulletins (pubs.usgs.gov)

| Doc | What it is | URL | Yield |
|---|---|---|---|
| B 741 (Schrader, 1923) | Jarbidge; mine-by-mine $/ton (Long Hike, O.K., Alpha, Bluster...) | https://pubs.usgs.gov/bul/0741/report.pdf | 9 |
| B 723 (Ferguson, 1924) | Manhattan; district $20.81 avg 1908-12, White Caps table, glory-hole mines | https://pubs.usgs.gov/bul/0723/report.pdf | 8 |
| B 762 (Knopf, 1924) | Rochester; 1920 mill grade 12.63 oz Ag + 0.139 oz Au, lease grades | https://pubs.usgs.gov/bul/0762/report.pdf | 8 |
| B 408 (Emmons, 1910) | Reconnaissance Elko/Lander/Eureka camps: Edgemont, Cornucopia, Mountain City, Tenabo, Safford, Railroad | https://pubs.usgs.gov/bul/0408/report.pdf | 6 |
| B 715-K (Knopf, 1921) | Divide silver district; Tonopah Divide 25 oz Ag ore | https://pubs.usgs.gov/bul/0715k/report.pdf | 4 |
| B 601 (Lindgren, 1915) | National district; the $30,000/ton shipped ore statement | https://pubs.usgs.gov/bul/0601/report.pdf | 3 |
| B 725-I (Ferguson, 1921) | Round Mountain; company vs lessee $/ton, Sunnyside sections | https://pubs.usgs.gov/bul/0725i/report.pdf | 3 |
| B 906-D (Callaghan, 1939) | Searchlight; Quartette stoped ore, Duplex 1926 smelter lots | https://pubs.usgs.gov/bul/0906d/report.pdf | 3 |
| B 414 (Ransome, 1909) | Humboldt Co. notes: Seven Troughs mill run, Unionville (Arizona mine) sampling | https://pubs.usgs.gov/bul/0414/report.pdf | 3 |
| B 407 (Ransome/Emmons/Garrey, 1910) | Bullfrog; Montgomery-Shoshone bonanza >600 oz Ag (few other numbers — mostly geology) | https://pubs.usgs.gov/bul/0407/report.pdf | 1 |

## 3. USGS Monograph (archive.org scan)

- **Monograph 3, Becker 1882, "Geology of the Comstock Lode and Washoe District"** —
  https://archive.org/details/geologyofcomstoc00beck (OCR text `geologyofcomstoc00beck_djvu.txt`).
  Yielded 4 Comstock records (Ophir/Mexican $107/ton, Gould & Curry, 1866 district average $37, Con. Virginia bonanza $80).
  Note: the archive.org OCR uses doubled spaces — grep with `per\s+ton`, not "per ton".

## 4. USBM Information Circulars — Vanderburg county reconnaissances (archive.org, full OCR text)

The single best per-mine grade source for the 1930s leasing era, and full of restated early-camp figures.
Pattern: `https://archive.org/details/pub_usgov-bmines-information-circular_XXXX`.

| IC | County (year) | Districts mined for records | Yield |
|---|---|---|---|
| IC 7043 | Lander (1939) | Austin/Reese River (incl. 1873 Manhattan S.M. Co. $224.50/ton), Battle Mountain, Bullion, Hilltop, Lewis, Buffalo Valley | 10 |
| IC 6902 | Pershing (1936) | Rochester, Seven Troughs (Tyler dumps), Arabia, Imlay, Kennedy, Rosebud, Gold Banks, Haystack | 9 |
| IC 6964 | Clark (1937) | Eldorado (Techatticup 0.55 Au/9.5 Ag), Searchlight, Gold Butte, Goodsprings, Bunkerville | 7 |
| IC 7093 | Churchill (1940) | Fairview ($14.53/ton district), Wonder ($31.95 lessee ore), Eastgate, Jessup, Table Mountain, Fireball | 7 |
| IC 6941 | Mineral (1937) | Aurora (490,168 t @ $2.93), Rawhide/Regent, Cedar Mountain (Omco), Marietta, King | 6 |
| IC 6995 | Humboldt (1938) | Awakening (Jumbo), National (Charleston Hill), Iron Point (Silver Coin), Harmony; Getchell had only a $5-$12 range | 4 |
| IC 7022 | Eureka (1938) | Cortez (Boitano lease 85 oz Ag), Eureka (Silver Connor), Diamond, Mineral Hill | 4 |

## 5. Mineral Resources of the United States annuals (USGS, archive.org)

- **MR 1907 Part I** — https://archive.org/details/mineralresources011907 — Tuscarora dump estimate ($3.50/ton over 4.8 M claimed tons) and the early $35/ton milling floor. 2 records.
- **MR 1909 Part I** — https://archive.org/details/mineralresources011909 — Gold Circle (Midas) first-year $151.76/ton and 1909 mill-feed $36.44/ton. 2 records.
- MR 1908 Part I and MR 1893 were downloaded and searched (Hornsilver, Delamar NV, La Plata UT) but contain totals only, no per-ton figures.

## 6. Utah Geological Survey

- **UGS Open-File Report 695 (Krahulec, 2018), "Mining districts of Utah"** — https://ugspub.nr.utah.gov/publications/open_file_reports/ofr-695.pdf —
  free modern compilation; used to run down the La Plata (Paradise) district, Cache County: UGS confirms it was only a minor
  Fe-Cu-Zn-Pb producer (~$27,000 total) with **no published Au/Ag oz/ton figures** — so no La Plata grade record was forced.

## 7. Coordinates

- **USGS MRDS** full dump (mrds.csv, 2023) — used offline for lat/lon on 49 records where a site name + county match was
  unambiguous (producers preferred). https://mrdata.usgs.gov/mrds/

## Checked but NOT usable / gaps that remain

- **Delamar (Ferguson district), Lincoln Co., NV** — MR 1907/1909 describe the Bamberger-Delamar operation (400-ton cyanide mill)
  but give no $/ton. The standard grade statements are in F.C. Lincoln (1923) and E. Callaghan's Delamar report
  (Univ. of Nevada Bulletin v.31, 1937) — neither freely digitized (see below).
- **Hornsilver/Gold Point, Esmeralda Co.** — MR 1909 gives district totals (926 tons; $21,089 Au + 65,627 oz Ag) but no stated per-ton figure; no free digitized per-ton statement found.
- **La Plata, UT** — no digitized grade data exists (see UGS OFR-695 note above).

## Requires-physical-visit / not-freely-digitized collections

1. **NBMG county bulletins** (B58 Mineral, B59 Humboldt, B62 Clark, B64 Eureka, B73 Lincoln, B77/B78 Nye-south/Esmeralda,
   B83 Churchill, B85 White Pine, B88 Lander, B99A/B Nye, B101/B106 Elko) — the per-mine production/grade tables exist in print
   and paid digital copies only. Sales office: https://pubs.nbmg.unr.edu/ (County bulletins list: https://pubs.nbmg.unr.edu/Articles.asp?ID=262).
2. **NBMG Mining District Files** — tens of thousands of scanned pages (assay sheets, engineers' reports, E&MJ clippings) at
   data.nbmg.unr.edu; the portal rejects non-interactive clients, so treat as an on-site/interactive resource.
   Info: https://nbmg.unr.edu/ (Collections). Great Basin Science Sample and Records Library, Reno.
3. **F.C. Lincoln, "Mining Districts and Mineral Resources of Nevada" (1923)** — public domain and full-view at HathiTrust
   (catalog record 001521890, item mdp.39015011432807) but HathiTrust blocks bulk/automated access; readable interactively:
   https://catalog.hathitrust.org/Record/001521890. NBMG sells a reprint (NP-01).
4. **University of Nevada Bulletins** (Callaghan 1937 Delamar; Nolan 1936 Tuscarora; Grant Smith 1943 "History of the
   Comstock Lode") — not on archive.org; available at UNR/NBMG in print. UNR Special Collections: https://library.unr.edu/spec-coll
5. **County recorder claim/deed books** (Nye, Esmeralda, Elko, Lincoln, Tooele, Juab counties) — original location notices and
   mill returns; never digitized; county courthouse visits required.
6. **Nevada State Inspector of Mines biennial reports; Nevada mine-tax (bullion-tax) ledgers** — Nevada State Archives, Carson City:
   https://nsla.nv.gov/
7. **Utah**: Cache County newspapers (Logan Journal 1891-93) for La Plata assay reports — Utah Digital Newspapers has partial
   coverage (https://digitalnewspapers.org/); mine-level ledgers at Utah State Historical Society, SLC.
