# Sources and annotations — historic ore-grade dataset, Idaho + SE/E Oregon

Companion file: `raw_id_or.json` (one JSON array, 213 records; schema per task spec, identical to `raw_greatbasin_nvut.json`).
Compiled 2026-07-31 from digitized public archives only (no paywalls, no accounts).

## Conventions used in the dataset
- **Ranges.** Where a source states a range ("$14 to $16 per ton", "6 to 8 ounces"), the numeric field records the **lower bound** and the full range stays in the verbatim quote. Exceptions flagged below.
- **Derived per-ton values (7 records).** The division is ours; the dividend and divisor are verbatim in the quote:
  - Albion group (Cassia Co): $2,200 / 30-ton car = $73.33/t
  - Poorman (Silver City): first 2,000 tons / $547,000 = $273.50/t
  - North Pole mine (Cracker Creek OR): 100,045.04 oz Au & 103,616.19 oz Ag / 158,917.40 t = 0.63 oz Au, 0.65 oz Ag (recovered)
  - Buffalo mine (Granite OR): 33,142 oz Au & 252,893 oz Ag / 42,246 t = 0.78 oz Au, 5.99 oz Ag
  - Humboldt mine (Mormon Basin OR): $225,000 / 35,000 t = $6.43/t
  - Sunday Hill mine (Mormon Basin OR): 120 oz Au / 400 t = 0.30 oz Au
  - Gold Hill 1881 shipment (Boise Basin): $25,000 / 150 t = $166.67/t
- **Upper-bound records (flagged "up to" in quote).** Mullin prospect ($60), Gibbs property ($300), Gold Point ($80), Buffalo–Constitution ($600), Quaker City (2,000 oz Ag), Elkhorn vein Boise Basin ($40). These are peak values, not averages — the quote makes that explicit.
- **Badger mine (Susanville) $300/t record:** OCR of the lower bound of the range is illegible in DOGAMI B-14-B ("�1.50", almost certainly $150); $300 upper bound is clean and is what is recorded.
- **Concentrates vs. crude ore.** Records for concentrates (Melcher, Crown Point $800, De Lamar $476, Black Jack $2,500, Standard/Copperopolis, Croesus $4 gold, La Belleview 1.20/55, Neal district 2.5/5, Bonanza $20–60 in quote) say so in the quote — do not read them as run-of-mine grades.
- **Table transcriptions.** McGregor group (B 539 p. 42), Lucky Boy analysis (B 539 p. 48), and Rainbow company-record table (B-61 p. 129) are linearized from printed tables; bracketed units are ours, numbers verbatim.
- **Dollar eras.** `pre1934($20.67/oz)` for all pre-1934 statements; `1934-1971($35/oz)` for late-1930s DOGAMI handbook entries and USGS B 922-I (Stibnite 1938–40 mill heads). Flagstaff quote states $20.67 basis explicitly.
- **Coordinates** are approximate district/mine centroids added by us for mapping convenience (not from the sources); several minor prospects carry district-center coordinates; null where placement is uncertain.
- OCR line breaks and hyphenation in quotes were silently joined; obvious OCR letter substitutions (e.g., "AI;say"→"Assay", "ouuces"→"ounces", "Ouster"→"Custer") were normalized without changing wording or numbers. "2 1/2" in some quotes renders OCR "2£"/"2!".

## A. On-disk digitized archives (mined first)
1. **Idaho Mine Inspector Annual Reports (ISMIR), 1907–1955** — OCR text of the reports of Robert N. Bell (and successors), Idaho Inspector of Mines. Local: `/home/claude/mining-data/ismir/*.txt`; canonical PDFs: `https://www.idahogeology.org/Uploads/Data/ISMIR/<YEAR>_ISMIR.pdf`.
   - Grade-rich narrative years: **1915, 1917, 1918** (~55 records: Wood River, Gilmore/Lemhi, Bayhorse–Ramshorn, Red Bird, Boise Basin, Banner, Silver City Crown Point, Demming/Flint, Atlanta, Warren-Rescue, Holt/Marshall Lake, Elk City Oro Grande, Mineral district, Seven Devils Red Ledge…).
   - 1907, 1920–1923, 1929, 1931, 1955 are directory/statistics format — company listings and statewide totals only (re-verified this session by grep on "ounce"/"per ton"/"assay": every hit is a state or county production total, a price note, or a mill-cost figure; the single narrative per-ton passage in 1923 is a statewide generalization "$3 to $15 per ton" with no mine attached — nothing extractable).
2. **Anderson, A.L., 1931, Geology and Mineral Resources of Eastern Cassia County, Idaho — IBMG Bulletin 14** (`anderson_1931_b14.txt`; IGS product page `https://www.idahogeology.org/product/b-14`). All quantitative Cassia records: Silver Hills (0.03 oz Au / 42.4 oz Ag average upper-tunnel ore; high-grade "several hundred ounces silver" unquantified), Melcher (99.4 oz Ag galena; $10 Au concentrates), Big Bertha ($14–16), Albion group ($2,200/30-t car), Golden Eagle (0.04/15.6), Valentine cinnabar (0.05/2.7), Hazel Pine (assays barren — recorded only in notes), Ruth (Zn carbonate, no Au/Ag figure).
3. **bowen_1913_b531h.txt** (USGS B 531-H, Bowen — *coal* at Horseshoe Bend/Jerusalem Valley) and **b620l.txt** (USGS B 620-L, Hill — *Snake River fine gold*, placer): read; no lode Au/Ag per-ton grades to extract.

## B. Web sources actually used (all open)
4. **DOGAMI Oregon Metal Mines Handbook** — per-mine entries with grades, the single best Oregon source:
   - **B-14-A (1939), NE Oregon East Half (Baker Co.)** — https://pubs.oregon.gov/dogami/B/B-014A.pdf — Connor Creek, Cornucopia (0.47 oz mine-run 1938), Cracker Creek, Eagle Creek (Sanger, Crystal Palace), Homestead (Iron Dyke), Lower Burnt River (Gold Hill, Gleason, Gold Point, Hallock…), Mormon Basin (Rainbow $12 mill heads / 95,747 t; Giraffe; Randall; Regan; Summit), Rock Creek (Baisley-Elkhorn, Highland-Maxwell 0.42 oz Au + 3.65 oz Ag), Sparta (Macy, New Deal), Virtue (Virtue $20–40/t 1870s, Flagstaff, Chicago-Virtue), Upper Burnt River (Record).
   - **B-14-B (1941), NE Oregon West Half (Grant Co.)** — https://pubs.oregon.gov/dogami/B/B-014B.pdf — Canyon City (Canyon Mountain/Mountain View), Granite (Buffalo, Central), Greenhorn (Ben Harrison, Morning, Royal White), Quartzburg/Dixie Creek (Dixie Meadows, Standard-Copperopolis, Buck Gulch), Susanville (Badger).
   - **B-14-D (1951), NW Oregon** — https://pubs.oregon.gov/dogami/B/B-014D.pdf — Bohemia (Musick+Champion combined $6.90/t for 14 yr at $20.67; district range $1.20–$16; Riverside; War Eagle 2.75 oz Au spur-vein assay), Blue River (Great Northern), Quartzville (Albany, Riverside, Savage/Vandalia, Peak & Dwarf), North Santiam (Crown).
5. **DOGAMI Bulletin 61, Gold and Silver in Oregon (Brooks & Ramp, 1968)** — https://pubs.oregon.gov/dogami/B/B-061.pdf — added this session (12 records): Cornucopia mines 0.48 oz Au + 2.2 oz Ag on 156,388 t (1938–41); North Pole 1895–1908 recovery; E&E $9.28 mill ore 1894–98; Buffalo 1903–65 output; La Belleview concentrates 1.20/55 + $60–300 shipping ore; Present Need 4–5 oz Au shoots; Virtue 0.5–1.0 oz Au; Bonanza $17.85 (1901–04); Red Boy $8.00 on 83,373 t; **Malheur County** Mormon Basin trio (Rainbow $11.24 company table 1911–15, Humboldt ~35,000 t/$225,000, Sunday Hill 1934 run). Also verified: the SE Oregon "isolated districts" chapter (Harney, High Grade OR-side, Lost Cabin, Steens-Pueblo, Spanish Gulch) contains **no per-ton grade figures** — production totals or "small shipments reported" only.
6. **USGS 20th Annual Report, Part III (1900)** — https://pubs.usgs.gov/ar/20-3/report.pdf
   - **Lindgren, "The gold and silver veins of Silver City, De Lamar, and other mining districts in Idaho"** — War Eagle Mountain per-mine averages (Oro Fino, Ida Elmore, Golden Chariot, Minnesota, Mahogany, Cumberland, **Poorman**), Florida Mountain (Black Jack, Trade Dollar), **De Lamar**, Flint, Wood River (Minnie Moore, Parker, Quaker City, North Star), Croesus, **Warren**, **Florence**.
   - **Diller, "Bohemia mining region of western Oregon"** (same volume) — Musick, Champion, Combination, Confidence.
7. **USGS 18th Annual Report, Part III (1898), Lindgren, "The Mining Districts of the Idaho Basin and the Boise Ridge, Idaho"** — https://pubs.usgs.gov/ar/18-3/report.pdf — added this session (20 records): Boise Basin lodes (**Gold Hill $20/t long-term average + 1881 150-ton/$25,000 shipment; Newburg $4–12; Washington claim $20/t gold shoot and 33–90 oz Ag silver vein; Elkhorn vein to $40; Summit vein $10–40; Mountain Chief 1895 mill run $100/t**), Neal district ($10–120 milled ore; 2.5–4 oz Au concentrates; 0.7 Au/44 Ag galena assay), Black Hornet (camp $40 carloads; Viola $15; Ironsides $40 stope; 0.40/4.60 sulphide assay), Golden Star arrastre $33, Pearl/Willow Creek (5 Au/5 Ag shipping ore; 0.85/28.35 sample; Alexander $40; Silver Wreath $40). OCR of the pubs.usgs.gov scan is degraded; all quotes were re-read against page images (pp. 688–716) via page-sliced PDF.
8. **USGS Bulletin 539 (Umpleby 1913), Some Ore Deposits in Northwestern Custer County, Idaho** — https://pubs.usgs.gov/bul/0539/report.pdf — added this session (17 records): **Bayhorse** (Ramshorn 500-oz early ore and 125-oz shoot average; Skylark $2.7M at 8% Cu/80 oz; River View; Excelsior; Pacific; Hoosier 200–400 oz; McGregor 13-shipment table 0.041/69.90; Red Bird 1880–1902 smelter deliveries), **Yankee Fork** (General Custer $600 hand-picked shipping ore and $150–300 early mill runs; Lucky Boy 1.34 Au/25.90 Ag analysis; Golden Sunbeam "big stope" $2–4), **Loon Creek** (Lost Packer $80–90 ore, north/south shoot grades; district galena 60–100 oz).
9. **USGS Bulletin 922-I (White 1940), Antimony Deposits of a Part of the Yellow Pine District, Valley County, Idaho** — https://pubs.usgs.gov/bul/0922i/report.pdf — added this session (4 records): Yellow Pine mine West quarry mill heads $4.07 Au + 0.51 oz Ag on 30,540 t (1938–39); East quarry $5.47 Au on 41,135 t (1939–40); Meadow Creek 100,000-t reserve at 0.14 oz Au; Monday tunnel 240-ft assay average.
10. **Mineral Resources of the U.S. / archive.org** — used for the NV/UT companion set; Idaho/Oregon chapters not re-mined this session (see gaps).

## C. Sources identified but not (fully) mined
- **Lindgren 1901, 22nd Ann. Rept. pt. 2 ("The Gold Belt of the Blue Mountains of Oregon")** — https://pubs.usgs.gov/ar/22-2/report.pdf downloads (728 MB) but was evicted from the sandbox before conversion; its key per-mine figures for Cracker Creek/Sumpter, Virtue, Connor Creek, Red Boy and Bonanza are quoted or superseded in DOGAMI B-61 and B-14 entries already captured. Chapter scans also at https://www.oregon.gov/dogami/milo/archive/Pubs/Lindgren/.
- **IBMG Pamphlet 83 (Anderson 1949, Yankee Fork district)** — https://www.idahogeology.org/pub/Pamphlets/P-83.pdf — downloaded; 53 pages, **image-only scan (no usable OCR text layer)**. General Custer/Lucky Boy grades are instead captured from USGS B 539. Needs OCR pass or manual read for Anderson's compiled mill figures.
- **IBMG Pamphlet 72 (Staley 1945, Fine Gold of Snake River and Lower Salmon River)** — checked; placer values in cents per ton of gravel/black-sand concentrate (Bingham, Cassia "near Milner" $9.51 and $37.89 concentrate figures), not lode ore grades under this schema.
- **USGS B 922-I companions** B 931-F etc. for Stibnite tungsten-gold; not fetched.
- **Mineral Resources of the U.S. annuals** (archive.org) — Idaho/Oregon chapters have yearly per-mine mill-head statements; identifiers exist for most years 1882–1931.
- **IGS Pamphlets** (idahogeology.org/pubs): P-49 (Atlanta), P-61 (Rocky Bar area), P-131. Open PDFs at `https://www.idahogeology.org/pub/Pamphlets/`.

## D. Requires physical/manual visit (not digitized or not verifiable online)
| Material | Where | Contact/URL |
|---|---|---|
| Idaho Mine Inspector originals, 1899–1914, 1916, 1919, 1924–28, 1932–54 (years missing from the ISMIR scan set), incl. mine-by-mine correspondence | Idaho State Archives, Boise | https://history.idaho.gov/state-archives/ |
| Mining district record books, claim location notices, deed books (Owyhee, Boise, Custer, Blaine, Cassia, Lemhi counties) | County recorders (e.g., Owyhee Co., Murphy ID; Custer Co., Challis ID) | https://owyheecounty.net/ ; https://co.custer.id.us/ |
| Trade Dollar Consolidated / De Lamar company mill books & assay ledgers | Idaho State Archives MS collections; Owyhee County Historical Museum, Murphy | https://owyheemuseum.org/ |
| General Custer Mining Co. records (Yankee Fork); Bonanza/Custer townsite collections | Land of the Yankee Fork Interpretive Center, Challis; Idaho State Archives | https://parksandrecreation.idaho.gov/parks/land-yankee-fork/ |
| Bradley Mining Co. / Yellow Pine (Stibnite) operating records, assay maps | Idaho State Archives; MSS at Univ. of Idaho Special Collections, Moscow | https://www.lib.uidaho.edu/special-collections/ |
| Oregon mine assay/mill records: Cornucopia Gold Mines Inc. corporate records; Sumpter Valley Dredge Co. | Baker County Public Library archives; Oregon State Archives, Salem | https://sos.oregon.gov/archives/ |
| DOGAMI MILO mine-file folders (unscanned per-mine correspondence, assay maps, e.g., Mormon Basin/Giraffe folder partially scanned) | DOGAMI Baker City & Portland offices; index at MILO | https://www.oregon.gov/dogami/milo/ |
| Grant County mining records (Canyon City, Susanville, Quartzburg) | Grant County Clerk, Canyon City OR | https://gcoregonlive.com/ |
| UO Special Collections: Bohemia district company papers (Musick Mining & Milling Co., Oregon Securities Co.) | Univ. of Oregon Libraries SCUA, Eugene | https://library.uoregon.edu/special-collections |
| OSU/Horner collection Oregon mining ephemera | OSU Special Collections & Archives, Corvallis | https://scarc.library.oregonstate.edu/ |
| Raymond, R.W., *Statistics of Mines and Mining West of the Rocky Mountains* 1869–1875 originals (some volumes digitized; Idaho/Oregon per-mine mill returns) | archive.org (partial) / Library of Congress | https://archive.org/ |

## E. Known gaps in this dataset (numeric grades not yet captured)
- **SE Oregon proper (Harney, Lake, Steens–Pueblo, High Grade Oregon side, Lost Cabin, Malheur/Vale hot-spring districts).** Verified in DOGAMI B-61: no per-ton grade figures exist in the digitized literature for these districts — only production totals ("placers yielded about $50,000", "small shipments of gold reported from the Farnham and Pueblo prospects") or nothing. Mormon Basin (Malheur Co.) is the closest SE-Oregon district with real grade records (captured). Closing this gap requires DOGAMI MILO mine files (physical) or county records.
- **Rocky Bar per-mine grades** (Elmore Co. — 1915 ISMIR gives Pine Grove $400,000 total only). Needs IGS P-61 or Lindgren 17th/18th AR Rocky Bar coverage.
- **Pierce district lode grades** (only the Lolo Creek $100 pyrite assay captured).
- **Boise Basin placer $/yd** — dredge yardage described without per-yard values in the ISMIR years held; AR 18-3 gives placer production totals, not per-yard tenor, for most camps.
- **Vipont (Utah-line) Idaho-side workings** — Anderson B-14 covers the Idaho side only qualitatively.
- **Anderson 1949 (P-83) Yankee Fork mill statistics** — image-only scan (see C).
- **Coeur d'Alene per-mine Ag-Pb grades** (PP 62, Ransome & Calkins 1908) — only one Coeur d'Alene record in set; the district is Ag-Pb dominant and was deprioritized for the Au/Ag-oz schema, but PP 62 is digitized and minable.
