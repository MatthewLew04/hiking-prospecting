# hiking-prospecting

Mining research and interactive maps for prospecting — old mines, active claims, and mineral sources compiled from government datasets plus the historical record.

## The maps

**[`nw-mineral-monitor/`](nw-mineral-monitor/) — the Northwest US map (WA · OR · ID · MT · WY).** 177,994 sites from 7 databases, 113k active + 819k closed BLM claims with nightly auto-refresh when hosted, live claim boundaries queried straight from BLM at high zoom, and 472 mining districts. Run locally (`cd nw-mineral-monitor/site && python3 -m http.server 8000`) or host on AWS in ~15 minutes — see [`nw-mineral-monitor/DEPLOY.md`](nw-mineral-monitor/DEPLOY.md).

**[`cassia-mineral-monitor.html`](cassia-mineral-monitor.html) — the original Cassia County, Idaho deep dive.** Single self-contained file, works offline from disk. Every mine, claim, and mineral occurrence in the county, plus the researched story of its seven districts. [`cassia-mining-research.md`](cassia-mining-research.md) is the companion report; the county data layers live in [`data/`](data/).

## Sources

USGS MRDS · USGS USMIN v10 · BLM MLRS (live + snapshots) · Idaho Geological Survey DD-1 · Washington Geological Survey DDS-30 · DOGAMI MILO-4 · MBMG AIM · WSGS Mines and Minerals · US Census TIGER — full citations and retrieval logs inside each map's About panel and in `data/summary_*.md`.

⚠ Planning aids only. Never enter adits or shafts. Active claims are private mineral property; verify land ownership and claim status on the ground before prospecting.
