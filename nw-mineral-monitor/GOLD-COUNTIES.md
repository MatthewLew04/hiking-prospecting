# County gold signal — stakeable-first ranking (49 states, 3,138 counties)

_Generated 2026-09-01 by `pipelines/county_gold.py` from the repo's own archives and snapshots. Ranking lens: **stakeable gold** — see the map layer (GOLD BY COUNTY) for the same data interactively, with an endowment lens for the states where stakeable cannot yet be measured._

**Method.** Every MRDS site with a gold commodity (national bulk CSV, 49 states), every state-survey site with a gold commodity (CA ID MT OR WA WY) and every ARDF occurrence with Au among its main commodities (AK), every cited gold grade (open distances precomputed against the active-claim snapshot; eight states), every claim centroid and every USMIN mining working (national archive, aggregate pits excluded) assigned to its county by point-in-polygon against the published Census TIGERweb county archive (January 1 2025 vintage, 3,138 counties). "Claimed" = active-claim centroid within ~400 m (grid test, inherits MRDS coordinate slop); "dropped" = closed claim that near with nothing active. Claims snapshots: federal MLRS for CA ID MT NV OR UT WA WY; Alaska DNR state claims for AK. NV/UT/WY closed files hold only the newest 250k records — dropped-ground undercounts there; CA has no closed snapshot. Patented private ground shows no claims and reads "open" — verify ownership, always.

## Coverage — what each state's score means

- **Stakeable measured (9 states):** Alaska (AK), California (CA), Idaho (ID), Montana (MT), Nevada (NV), Oregon (OR), Utah (UT), Washington (WA), Wyoming (WY). A claims snapshot exists, so "open", "claimed" and "staked-then-dropped" are real tests. Alaska is measured against DNR state-law claims only (no federal MLRS snapshot for Alaska in the repo).
- **Claims pending (10 claim states):** Arkansas (AR), Arizona (AZ), Colorado (CO), Florida (FL), Louisiana (LA), Mississippi (MS), North Dakota (ND), Nebraska (NE), New Mexico (NM), South Dakota (SD). Federal mining claims apply, but no snapshot is in the repo yet, so stakeable is unmeasurable and only endowment is scored (shown muted on the map).
- **Non-claim states (30):** Alabama (AL), Connecticut (CT), Delaware (DE), Georgia (GA), Iowa (IA), Illinois (IL), Indiana (IN), Kansas (KS), Kentucky (KY), Massachusetts (MA), Maryland (MD), Maine (ME), Michigan (MI), Minnesota (MN), Missouri (MO), North Carolina (NC), New Hampshire (NH), New Jersey (NJ), New York (NY), Ohio (OH), Oklahoma (OK), Pennsylvania (PA), Rhode Island (RI), South Carolina (SC), Tennessee (TN), Texas (TX), Virginia (VA), Vermont (VT), Wisconsin (WI), West Virginia (WV). No federal locatable-mineral system; endowment only, and the route is a state lease or private negotiation.
- **Cited-grade corpus:** California (CA), Idaho (ID), Montana (MT), Nevada (NV), Oregon (OR), Utah (UT), Washington (WA), Wyoming (WY). Everywhere else the rich-open-grade component is unmeasured, not zero.

## Top 25 — stakeable, all measured states

| # | County | Stake | Endow (rank) | Rich-open grades | Staked-then-dropped | Open Au sites | Producers |
|---|---|---|---|---|---|---|---|
| 1 | **Lemhi County, ID** | 128.8 | 100.0 (#2) | 16 | 443 | 604 | 505 |
| 2 | **Jackson County, OR** | 128.4 | 90.6 (#26) | 15 | 452 | 757 | 356 |
| 3 | **Okanogan County, WA** | 122.9 | 90.8 (#24) | 10 | 544 | 981 | 311 |
| 4 | **Custer County, ID** | 116.7 | 93.8 (#13) | 8 | 450 | 720 | 306 |
| 5 | **Josephine County, OR** | 114.2 | 91.7 (#20) | 9 | 410 | 552 | 419 |
| 6 | **Snohomish County, WA** | 114.2 | 74.7 (#44) | 8 | 571 | 766 | 136 |
| 7 | **Blaine County, ID** | 114.0 | 89.3 (#29) | 9 | 372 | 531 | 346 |
| 8 | **Madison County, MT** | 110.8 | 91.9 (#18) | 8 | 356 | 520 | 464 |
| 9 | **Elmore County, ID** | 110.5 | 90.6 (#25) | 16 | 142 | 215 | 217 |
| 10 | **Valley County, ID** | 108.2 | 94.6 (#12) | 9 | 304 | 691 | 136 |
| 11 | **El Dorado County, CA** | 106.8 | 79.7 (#39) | 15 | 0 | 1865 | 550 |
| 12 | **Idaho County, ID** | 101.2 | 94.6 (#11) | 6 | 415 | 705 | 760 |
| 13 | **Jefferson County, MT** | 99.4 | 88.5 (#30) | 5 | 573 | 702 | 433 |
| 14 | **Baker County, OR** | 99.1 | 79.0 (#40) | 5 | 917 | 1212 | 404 |
| 15 | **Amador County, CA** | 98.2 | 85.2 (#34) | 14 | 0 | 658 | 240 |
| 16 | **Kern County, CA** | 98.0 | 91.6 (#21) | 17 | 0 | 370 | 540 |
| 17 | **San Bernardino County, CA** | 96.9 | 100.0 (#1) | 9 | 8 | 1426 | 688 |
| 18 | **Shasta County, CA** | 95.8 | 90.3 (#27) | 15 | 0 | 537 | 388 |
| 19 | **Beaverhead County, MT** | 95.6 | 93.2 (#15) | 7 | 212 | 286 | 217 |
| 20 | **Grant County, OR** | 94.8 | 87.4 (#33) | 5 | 723 | 794 | 282 |
| 21 | **Nevada County, CA** | 94.5 | 87.8 (#32) | 13 | 0 | 1422 | 968 |
| 22 | **Lincoln County, NV** | 93.3 | 97.5 (#8) | 9 | 46 | 147 | 215 |
| 23 | **Whatcom County, WA** | 92.8 | 90.1 (#28) | 11 | 147 | 234 | 105 |
| 24 | **Boise County, ID** | 87.7 | 92.2 (#17) | 7 | 219 | 286 | 434 |
| 25 | **Esmeralda County, NV** | 86.6 | 100.0 (#4) | 10 | 35 | 83 | 319 |

## Alaska — every borough and census area, ranked

Scored against Alaska DNR state-law claims (active + pending vs closed) with MRDS and ARDF gold occurrences. No cited-grade corpus for Alaska yet.

| # | County | Stake | Endow (rank) | Rich-open grades | Staked-then-dropped | Open Au sites | Producers |
|---|---|---|---|---|---|---|---|
| 49 | **Nome Census Area, AK** | 52.6 | 57.9 (#74) | 0 | 236 | 1389 | 1020 |
| 51 | **Yukon-Koyukuk Census Area, AK** | 51.1 | 65.0 (#61) | 0 | 196 | 1299 | 1051 |
| 59 | **Matanuska-Susitna Borough, AK** | 44.4 | 60.6 (#70) | 0 | 163 | 610 | 258 |
| 67 | **Southeast Fairbanks Census Area, AK** | 39.5 | 63.9 (#64) | 0 | 123 | 409 | 272 |
| 72 | **Copper River Census Area, AK** | 33.8 | 55.5 (#83) | 0 | 82 | 446 | 103 |
| 77 | **Denali Borough, AK** | 32.9 | 57.5 (#75) | 0 | 23 | 475 | 222 |
| 78 | **Kenai Peninsula Borough, AK** | 32.5 | 55.2 (#88) | 0 | 33 | 434 | 165 |
| 79 | **Fairbanks North Star Borough, AK** | 32.3 | 61.7 (#68) | 0 | 59 | 181 | 475 |
| 80 | **Bethel Census Area, AK** | 31.9 | 56.5 (#79) | 0 | 40 | 385 | 156 |
| 82 | **Northwest Arctic Borough, AK** | 31.7 | 59.7 (#71) | 0 | 56 | 278 | 159 |
| 84 | **Juneau City and Borough, AK** | 31.3 | 55.1 (#90) | 0 | 31 | 414 | 138 |
| 91 | **Chugach Census Area, AK** | 28.6 | 55.3 (#86) | 0 | 8 | 470 | 145 |
| 98 | **Prince of Wales-Hyder Census Area, AK** | 26.1 | 55.0 (#94) | 0 | 1 | 575 | 108 |
| 122 | **Hoonah-Angoon Census Area, AK** | 18.5 | 50.8 (#122) | 0 | 0 | 269 | 74 |
| 124 | **Aleutians East Borough, AK** | 18.2 | 33.3 (#180) | 0 | 28 | 201 | 11 |
| 129 | **Lake and Peninsula Borough, AK** | 16.7 | 35.0 (#167) | 0 | 19 | 171 | 13 |
| 130 | **Petersburg Borough, AK** | 16.6 | 39.2 (#147) | 0 | 5 | 156 | 41 |
| 135 | **Kodiak Island Borough, AK** | 15.6 | 37.2 (#156) | 0 | 4 | 137 | 38 |
| 137 | **Ketchikan Gateway Borough, AK** | 15.2 | 44.5 (#136) | 0 | 0 | 210 | 42 |
| 138 | **Haines Borough, AK** | 15.1 | 35.9 (#161) | 0 | 10 | 99 | 28 |
| 139 | **Sitka City and Borough, AK** | 15.0 | 39.7 (#145) | 0 | 1 | 196 | 29 |
| 142 | **Yakutat City and Borough, AK** | 14.9 | 30.0 (#196) | 0 | 16 | 65 | 28 |
| 149 | **Anchorage Municipality, AK** | 13.9 | 33.5 (#178) | 0 | 5 | 65 | 40 |
| 156 | **Dillingham Census Area, AK** | 12.7 | 26.2 (#218) | 0 | 10 | 93 | 10 |
| 171 | **Kusilvak Census Area, AK** | 10.3 | 29.3 (#200) | 0 | 0 | 50 | 36 |
| 186 | **Aleutians West Census Area, AK** | 7.7 | 21.2 (#258) | 0 | 0 | 100 | 3 |
| 190 | **Wrangell City and Borough, AK** | 6.9 | 19.2 (#268) | 0 | 0 | 61 | 5 |
| 238 | **North Slope Borough, AK** | 2.8 | 7.6 (#405) | 0 | 0 | 21 | 0 |
| 246 | **Skagway Municipality, AK** | 2.4 | 6.6 (#425) | 0 | 0 | 5 | 1 |
| 309 | **Bristol Bay Borough, AK** | 0.0 | 0.0 (#627) | 0 | 0 | 0 | 0 |

## Idaho — every county, ranked

| # | County | Stake | Endow (rank) | Rich-open grades | Staked-then-dropped | Open Au sites | Producers |
|---|---|---|---|---|---|---|---|
| 1 | **Lemhi County, ID** | 128.8 | 100.0 (#2) | 16 | 443 | 604 | 505 |
| 4 | **Custer County, ID** | 116.7 | 93.8 (#13) | 8 | 450 | 720 | 306 |
| 7 | **Blaine County, ID** | 114.0 | 89.3 (#29) | 9 | 372 | 531 | 346 |
| 9 | **Elmore County, ID** | 110.5 | 90.6 (#25) | 16 | 142 | 215 | 217 |
| 10 | **Valley County, ID** | 108.2 | 94.6 (#12) | 9 | 304 | 691 | 136 |
| 12 | **Idaho County, ID** | 101.2 | 94.6 (#11) | 6 | 415 | 705 | 760 |
| 24 | **Boise County, ID** | 87.7 | 92.2 (#17) | 7 | 219 | 286 | 434 |
| 31 | **Gem County, ID** | 74.8 | 73.0 (#47) | 9 | 54 | 66 | 56 |
| 39 | **Shoshone County, ID** | 57.0 | 88.3 (#31) | 1 | 212 | 282 | 288 |
| 62 | **Owyhee County, ID** | 42.7 | 93.1 (#16) | 2 | 40 | 81 | 267 |
| 73 | **Clearwater County, ID** | 33.7 | 55.5 (#85) | 0 | 68 | 237 | 131 |
| 76 | **Bonner County, ID** | 33.3 | 65.7 (#59) | 1 | 55 | 150 | 81 |
| 81 | **Camas County, ID** | 31.8 | 64.0 (#63) | 1 | 69 | 101 | 63 |
| 90 | **Adams County, ID** | 28.7 | 45.9 (#133) | 0 | 95 | 143 | 58 |
| 101 | **Power County, ID** | 23.6 | 38.6 (#150) | 0 | 70 | 98 | 57 |
| 103 | **Cassia County, ID** | 22.9 | 33.0 (#182) | 0 | 18 | 40 | 38 |
| 105 | **Butte County, ID** | 22.4 | 50.2 (#124) | 1 | 26 | 34 | 31 |
| 118 | **Ada County, ID** | 19.3 | 35.2 (#164) | 0 | 40 | 68 | 45 |
| 133 | **Washington County, ID** | 16.2 | 37.5 (#154) | 0 | 7 | 19 | 30 |
| 134 | **Latah County, ID** | 16.0 | 40.8 (#144) | 0 | 17 | 45 | 29 |
| 160 | **Boundary County, ID** | 12.4 | 25.4 (#226) | 0 | 16 | 31 | 17 |
| 164 | **Bonneville County, ID** | 12.1 | 31.7 (#188) | 0 | 4 | 14 | 34 |
| 166 | **Bannock County, ID** | 11.5 | 15.7 (#293) | 0 | 13 | 16 | 8 |
| 168 | **Clark County, ID** | 10.8 | 17.8 (#278) | 0 | 9 | 14 | 9 |
| 172 | **Jerome County, ID** | 9.5 | 18.7 (#272) | 0 | 6 | 23 | 13 |
| 176 | **Twin Falls County, ID** | 9.0 | 22.4 (#247) | 0 | 0 | 24 | 21 |
| 180 | **Kootenai County, ID** | 8.5 | 16.9 (#281) | 0 | 5 | 23 | 7 |
| 181 | **Bear Lake County, ID** | 8.5 | 15.1 (#299) | 0 | 6 | 17 | 8 |
| 182 | **Bingham County, ID** | 8.4 | 16.1 (#288) | 0 | 5 | 14 | 11 |
| 184 | **Benewah County, ID** | 8.2 | 16.5 (#285) | 0 | 4 | 19 | 10 |
| 185 | **Nez Perce County, ID** | 8.1 | 14.9 (#301) | 0 | 5 | 22 | 6 |
| 193 | **Gooding County, ID** | 6.8 | 12.4 (#330) | 0 | 4 | 10 | 6 |
| 197 | **Canyon County, ID** | 6.4 | 14.8 (#303) | 0 | 1 | 13 | 9 |
| 216 | **Jefferson County, ID** | 4.3 | 8.8 (#379) | 0 | 1 | 5 | 3 |
| 231 | **Oneida County, ID** | 3.3 | 2.8 (#543) | 0 | 3 | 3 | 0 |
| 234 | **Minidoka County, ID** | 3.1 | 8.8 (#380) | 0 | 0 | 5 | 3 |
| 237 | **Lewis County, ID** | 2.8 | 8.0 (#396) | 0 | 0 | 3 | 3 |
| 256 | **Teton County, ID** | 1.8 | 4.6 (#492) | 0 | 0 | 1 | 1 |
| 278 | **Lincoln County, ID** | 0.6 | 2.7 (#553) | 0 | 0 | 1 | 0 |
| 280 | **Payette County, ID** | 0.6 | 1.6 (#581) | 0 | 0 | 1 | 0 |
| 284 | **Caribou County, ID** | 0.4 | 0.2 (#623) | 0 | 0 | 0 | 0 |
| 308 | **Franklin County, ID** | 0.0 | 0.1 (#625) | 0 | 0 | 0 | 0 |
| 310 | **Fremont County, ID** | 0.0 | 0.0 (#1064) | 0 | 0 | 0 | 0 |
| 311 | **Madison County, ID** | 0.0 | 0.0 (#1065) | 0 | 0 | 0 | 0 |

## Top 25 — endowment, all 49 states

Endowment ignores claim status, so it is comparable across every county in scope.

| # | County | Endow | Stake (rank) | Producers | Gold sites | Rich grades | Active claims |
|---|---|---|---|---|---|---|---|
| 1 | **San Bernardino County, CA** | 100.0 | 96.9 (#17) | 688 | 1993 | 13 | 12,335 |
| 2 | **Lemhi County, ID** | 100.0 | 128.8 (#1) | 505 | 1108 | 25 | 8,082 |
| 3 | **Elko County, NV** | 100.0 | 64.8 (#33) | 559 | 858 | 15 | 39,287 |
| 4 | **Esmeralda County, NV** | 100.0 | 86.6 (#25) | 319 | 837 | 19 | 26,238 |
| 5 | **Eureka County, NV** | 100.0 | 75.9 (#30) | 219 | 315 | 15 | 27,104 |
| 6 | **Humboldt County, NV** | 100.0 | 52.8 (#48) | 289 | 613 | 13 | 28,743 |
| 7 | **Nye County, NV** | 99.0 | 78.7 (#28) | 520 | 1176 | 10 | 40,269 |
| 8 | **Lincoln County, NV** | 97.5 | 93.3 (#22) | 215 | 371 | 13 | 6,034 |
| 9 | **Pershing County, NV** | 97.0 | 42.1 (#63) | 421 | 803 | 8 | 17,732 |
| 10 | **White Pine County, NV** | 95.9 | 43.1 (#61) | 273 | 371 | 7 | 23,802 |
| 11 | **Idaho County, ID** | 94.6 | 101.2 (#12) | 760 | 1505 | 16 | 3,681 |
| 12 | **Valley County, ID** | 94.6 | 108.2 (#10) | 136 | 985 | 14 | 3,670 |
| 13 | **Custer County, ID** | 93.8 | 116.7 (#4) | 306 | 1079 | 13 | 3,000 |
| 14 | **Lander County, NV** | 93.4 | 56.4 (#43) | 526 | 647 | 12 | 29,009 |
| 15 | **Beaverhead County, MT** | 93.2 | 95.6 (#19) | 217 | 604 | 14 | 2,523 |
| 16 | **Owyhee County, ID** | 93.1 | 42.7 (#62) | 267 | 473 | 13 | 2,444 |
| 17 | **Boise County, ID** | 92.2 | 87.7 (#24) | 434 | 873 | 14 | 1,763 |
| 18 | **Madison County, MT** | 91.9 | 110.8 (#8) | 464 | 1097 | 13 | 1,540 |
| 19 | **Clark County, NV** | 91.7 | 82.1 (#26) | 215 | 350 | 9 | 2,998 |
| 20 | **Josephine County, OR** | 91.7 | 114.2 (#5) | 419 | 1586 | 19 | 1,395 |
| 21 | **Kern County, CA** | 91.6 | 98.0 (#16) | 540 | 763 | 31 | 1,272 |
| 22 | **Sierra County, CA** | 91.4 | 36.2 (#69) | 601 | 1034 | 11 | 1,227 |
| 23 | **Tooele County, UT** | 91.1 | 56.8 (#40) | 244 | 396 | 7 | 4,182 |
| 24 | **Okanogan County, WA** | 90.8 | 122.9 (#3) | 311 | 1104 | 12 | 606 |
| 25 | **Elmore County, ID** | 90.6 | 110.5 (#9) | 217 | 395 | 28 | 492 |

## Reading the components

- **Rich-open grades** — mines with a *cited* assay/production grade ≥0.3 oz/t Au and no active claim within 400 m today. The strongest lead class in the dataset: documented gold, nobody holding it. Each one is quote-backed in the map dossier.
- **Staked-then-dropped** — a gold-commodity site with closed claims near it and nothing active: someone believed enough to stake and file, then let it lapse. Classic re-examination targets (fee-hike years shed good ground).
- **Open Au sites** — gold occurrences/prospects with no nearby active claim; weakest class alone (MRDS slop, patented-land trap) but volume matters.
- **Producers** validate that the county's system actually made ore.

## Caveats (same rules as the map)

- Active-claim proximity is a research screen, **not a title search** — patented private land shows no BLM claims and reads "open" here.
- NV/UT/WY closed-claim snapshots are truncated (newest 250k) → staked-then-dropped **undercounts** in those states; CA has no closed snapshot.
- Alaska federal claims are not screened (DNR state claims only).
- MRDS coordinates can be off by hundreds of meters; grades include specimen assays (read each quote).
- Verify land status, withdrawals, and county records before staking anything.
