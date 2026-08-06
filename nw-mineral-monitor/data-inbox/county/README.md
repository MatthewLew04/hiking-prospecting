# data-inbox/county/ — operator-supplied county recorder exports (WS5)

Drop recorder index exports here, one folder per county key from
`pipelines/config/county_portals.json`:

    data-inbox/county/cassia/whatever.csv

Accepted: CSV, TSV, JSON (list of objects). Headers are sniffed — common
aliases all work (Instrument/Doc No/Entry, Document Type/Kind, Date
Recorded/Filed, Grantor/From/Party 1, Grantee/To/Party 2, Legal
Description/Remarks/Comments, Book/Page, Claim Name). Minimum useful row: a
doc type + either a claim name or a TRS in the legal text.

Where the data comes from (Cassia has NO online index — verified 2026-08-06):

- a records request to recorder@cassia.gov — `county_records.py` prints a
  prefilled request, also shown in the map UI;
- a visit to the records vault (call ahead, (208) 878-5240) — type what you
  find into a spreadsheet, any reasonable columns;
- any county portal you can search in a browser (iDoc Market etc.) — export
  or copy/paste results. The pipeline never scrapes portals itself.

Then:  `python3 pipelines/county_records.py`   (add `--demo` to test with
`demo/county_sample.csv`, which is synthetic).

Every ingested instrument is classified (notice of location / mining claim /
amended / assessment affidavit / quitclaim / deed), TRS-parsed, and
fuzzy-matched to MLRS serials by claim name + section. Unmatched recent
locations become WATCH alerts: **COUNTY-RECORDED — NOT IN MLRS**.
