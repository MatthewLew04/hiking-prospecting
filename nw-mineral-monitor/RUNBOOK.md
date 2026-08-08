
## Grades rebuild (WS9 — rounds 1+2, CA + ID)

Order matters; each step is idempotent:

```
python3 pipelines/grades_ca.py     # CA round 1 (embedded rows) + round 2 (rows_ca_r2.json)
python3 pipelines/grades_id.py     # ID round 2 (rows_id_r2.json) + county backfill
python3 pipelines/county_gold.py   # richOpen / stakeable ranking rerun
```

Inputs: `grades-research/rows_{ca,id}_r2.json` (curated rows, page-cited),
`pipelines/cache/pagetext/*.json.gz` (page-indexed source text — committed;
quotes are re-validated against it on every run and a failure aborts),
`site/data/sites/mrds_{ca,id}.json` + the full MRDS dump (auto-fetched to
`pipelines/cache/mrds.csv` if absent) for county-scoped geolocation, and
`site/data/claims/{ca,id}_active.json` for open distances. Source PDFs
re-fetch by URL into `pipelines/cache/pdfs/` (gitignored) only when a
pagetext file needs rebuilding. If `build_grades.py` (round 0) is ever
rerun, run grades_ca.py + grades_id.py again afterward — they own the
'ca-r1'/'ca-r2'/'id-r2' row tags and the schema migration.
