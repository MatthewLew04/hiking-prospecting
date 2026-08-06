# data-inbox/

Drop CSV / GeoJSON / KML / GPX files here and run
`python3 ../pipelines/inbox_ingest.py` — each file becomes a permanent map
layer in `site/data/userlayers/` (the browser drag-drop does the same thing
client-side, but layers ingested here ship to every user of the site).

Geometry is auto-detected per row: lat/lon columns (many spellings), UTM
easting/northing (+ optional zone column), or a PLSS legal description
anywhere in the row ("T12S R22E Sec 14" — placed at the section centroid,
tagged with the section id). Rows with no usable location are counted and
reported, never guessed.

`messy_cassia.csv` is the demo/acceptance-test file — see DEMO.md.
