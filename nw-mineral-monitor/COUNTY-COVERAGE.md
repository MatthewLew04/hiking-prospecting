# WS5 — county recorder coverage matrix

_Generated 2026-08-06 by `pipelines/county_records.py`. Access levels: scrape / bulk-export / operator-export / manual-request / unavailable / unverified (= browser check needed; several portals block automated verification via robots.txt, which we respect)._

Mining claims become public record at the county recorder FIRST: location notices are recorded under state law (Idaho Code tit. 47, ch. 15), and the federal FLPMA filing with BLM is due within 90 days of location (43 U.S.C. § 1744). BLM adjudication + MLRS indexing add more lag, so the county index can lead MLRS by weeks to months — that gap is the WS5 signal.

| County | | Access | Vendor | Verified | Recorder |
|---|---|---|---|---|---|
| Cassia, ID | AOI | **manual-request** | — | ✔ 2026-08-06 | https://www.cassia.gov/recorder |
| Twin Falls, ID |  | **unverified** | — | unverified | https://twinfallscounty.org/recorder/ |
| Minidoka, ID |  | **unverified** | iDoc Market (reported) | unverified | https://www.minidoka.id.us/162/Recorders-Office |
| Power, ID |  | **unverified** | — | unverified | https://www.co.power.id.us/ |
| Oneida, ID |  | **unverified** | — | unverified | https://oneidacountyid.gov/ |
| Box Elder, UT |  | **unverified** | — | unverified | https://www.boxeldercounty.org/recorder.htm |
| Elko, NV |  | **unverified** | — | unverified | https://elkocountynv.net/departments/recorder/index.php |

## Cassia workflow (operator-assisted)

1. `python3 pipelines/county_records.py` writes the prefilled records request into `site/data/county/cassia.json` (also shown in the map UI under COUNTY RECORDS).
2. Email it to recorder@cassia.gov (or visit the vault — call ahead, (208) 878-5240).
3. Drop whatever you get back — CSV/TSV/JSON export, or a spreadsheet you type up from the index books — into `data-inbox/county/cassia/`.
4. Re-run the script: instruments are classified, TRS-parsed, fuzzy-matched to MLRS serials, attached to dossiers, and unmatched new locations surface in WATCH as **COUNTY-RECORDED — NOT IN MLRS**.

`--demo` ingests `demo/county_sample.csv` (synthetic) to see the full flow end-to-end.
