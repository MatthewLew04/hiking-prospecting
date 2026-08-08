
37. **WS9 multi-commodity grade rows merge by mine+county, never duplicate.**
    Round-2 CA/ID rows (`rows_{ca,id}_r2.json`) are validated VERBATIM
    against cached page-indexed PDFs before splicing; a mine already in the
    dataset gains the new quote in `xq` (all quotes travel with the row) and
    a primary-grade upgrade only when the new figure is richer at an
    equal-or-better basis (production average > ore shipped/resource >
    assay > value-text > assay-text). Upgrades are revertible — the
    displaced primary is stashed in `xq` tagged `<owner>:prev` and restored
    on rebuild. New columns: pb/zn/cu (%), sb (%), wo3 (units = % WO3),
    hgf (Hg production, 76-lb flasks), yd3 ($/yd³) + plc placer flag, nat
    (native-units summary), conv (conversion metadata: price, price year,
    price source). Silver $-conversions use the annual-average table in
    gradeslib.AG_PRICE (USGS Historical Statistics/DS 140); gold stays
    $20.67 pre-1934 / $35 1934-71. Bonanza caps per round 0 (averages
    >50 oz/t and anything >610 oz/t dropped at intake); round-1 CA bonanza
    rows grandfathered (#36). Geolocation is county-scoped MRDS name match
    (unique <5 km cluster required; district-centroid fallback for
    roll-ups); ambiguous names stay unlocated rather than invented.
