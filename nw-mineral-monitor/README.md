# NW Mineral Monitor

Interactive map of mines, mining claims, and mineral sources across **Washington, Oregon, Idaho, Montana, and Wyoming** — 177,994 sites from 7 government databases, 113,330 active + 819,158 closed BLM mining claims, and 472 mining districts (28 deeply researched, 7 Cassia County deep-dives, 437 auto-derived from MRDS).

**Run locally:** `cd site && python3 -m http.server 8000` → http://localhost:8000 (no login locally)
**Host on AWS (with nightly auto-updating claims + Cognito sign-in):** see [`DEPLOY.md`](DEPLOY.md) — ~15 minutes with `infra/deploy.sh`. The hosted site requires a Cognito login (`auth.json` in the bucket turns the gate on; the repo ships without it, so local use stays open).

## Layout

| Path | What |
|---|---|
| `site/index.html` | The whole app (MapLibre GL, vanilla JS) |
| `site/assets/` | MapLibre GL JS 5.24 (vendored — no CDN dependency) |
| `site/data/sites/` | Per-state columnar site files: `mrds_*` (USGS MRDS), `usmin_*` (USGS topo-map mine features), `stategeo_*` (IGS DD-1 / WGS DDS-30 / DOGAMI MILO-4 / MBMG AIM / WSGS) |
| `site/data/claims/` | Per-state claim centroids: `*_active.json` (serial, name, type, disposition, acres) and `*_closed.json` — refreshed by the Lambda when hosted |
| `site/data/districts/` | `curated.json` (28, cited), `cassia.json` (7 deep-dive), `auto.json` (437 from MRDS tags) |
| `site/data/manifest.json` | Layer inventory, counts, freshness stamps, live-query spec |
| `infra/` | CloudFormation template, Lambda updater, deploy script |

## Auto-updating

Hosted: EventBridge → Lambda re-pulls **active claims nightly** (09:10 UTC) and **closed claims monthly** from BLM MLRS, rewriting `data/claims/*` in S3 (15-min CloudFront TTL). In the browser: at zoom ≥ 10.5 the map queries BLM's GIS directly for the current viewport and draws **live claim polygons** — current even between snapshots. Everything degrades gracefully to the committed snapshot if BLM is down.

BLM server quirks the updater handles (hard-won; don't "simplify" them away): use OBJECTID-cursor pagination (not `resultOffset`); short pages with `exceededTransferLimit=true` are normal — stop only on an empty page; query with bbox envelopes (detailed polygons exhaust the request budget); `GEO_STATE`/`ADMIN_STATE` are mostly NULL — selection must be spatial; send a User-Agent header (default python-urllib gets 403). `CSE_TYPE_NR` decode: 3841xx=lode, 3842xx=placer, 3843xx=tunnel, 3844xx=mill.

## Known limits

Wyoming closed claims truncated to the most recent 250,000 of 287,066. MRDS is legacy (~2011). USMIN features come from 1958–2001 topo maps. State databases differ in scope (MT = abandoned-mines inventory; OR MILO mixes occurrences and borrow pits; WY is explicitly incomplete). Full caveats in the map's About panel.

⚠ Planning aid only. Never enter adits or shafts. Active claims are private mineral property; verify land status before prospecting.
