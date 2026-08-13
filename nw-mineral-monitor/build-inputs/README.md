# Private build inputs

This directory contains large, row-oriented compatibility snapshots used by
offline analysis and the PMTiles migration builders. It is repository data,
not static-site content: nothing beneath `build-inputs/` is browser-addressed
or uploaded by `infra/deploy.sh`.

`manifest.json` is the strict inventory. Its paths are relative to this
directory and are intentionally limited to `data/sites/<key>.json` and
`data/claims/<key>.json`. Run `python3 pipelines/reconcile_manifest.py --check`
to verify private counts alongside the public tiled manifest.

Browser-delivered national layers live under `site/data/tiles/` as PMTiles
(or COGs for raster products). Never copy these input snapshots back under
`site/`.
