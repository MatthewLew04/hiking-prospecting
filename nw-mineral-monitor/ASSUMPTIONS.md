
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

38. **WS10's ranked cohort is reproducible and forced seeds do not distort
    it.** “Top 15” means the first 15 located rows from the same rich-open
    weighted grade score used by the app, with stable source order as the
    tie-break. The De Lamar, Jackson, Black Pine, and Grass Valley seed areas
    are then added as `forced_seed` inventory records when they are not in
    that cohort; they do not displace a ranked target or acquire a fake
    rank. Containing plus edge/corner-adjacent USGS 7.5-minute quads define
    catalog-search coverage, not a claim that every returned map contains
    target-scale evidence.

39. **A map-catalog gap is data, not an omitted row or a geologic
    conclusion.** The inventory records `gap` when the searched NGMDB/state
    catalogs return no intersecting geologic map at 1:62,500 or larger, and
    records known regional/mine-scale substitutes and watch-list sources
    separately. “Gap” means no qualifying catalog result was found on the
    retrieval date. It does not mean the area is unmapped in every archive,
    geologically uninteresting, or safe to infer at coarse scale. Grass
    Valley's missing modern CGS quad is intentionally visible this way.

40. **Georeference confidence and remote verification control publication.**
    A standard 7.5-minute collar may use official quad corners after
    collar/edge inspection. Irregular report plates retain control-point and
    residual/error metadata. Even a reviewed local build is only
    `processing` / `built-awaiting-upload`; `ready` is recorded with
    `--mark-ready` only after its S3/CloudFront objects and alignment are
    verified. A low-confidence or unreviewed fit remains non-toggleable even
    if a tile pyramid can technically be made. The system prefers an honest
    blank over a persuasive bad warp.

41. **Raster and vector geology have different evidentiary jobs.** Scanned
    sheets are visual overlays. Only a real GIS database normalized with
    source id, attributed unit description, citation, and map scale feeds WS6
    lexicon scoring. DWM-193 therefore drives a native 1:24,000 rescan while
    the Jackson database remains “unpublished/on request.” Jackson's PDF is
    email-gated, the CGS request is unsent, and product-specific web-tile and
    database reuse rights are still pending. Raster pixels are never
    vectorized or scored as if they were attributed polygons.

42. **S3 is the raster system of record; git contains pointers and
    provenance only.** Source scans, extracted plates, COGs, legends, and
    XYZ tiles live in ignored `pipelines/cache/ws10/` staging and the fixed
    S3 `ws10-assets/` prefix. Site deploys exclude that prefix from every
    `sync --delete`; the explicit asset upload is add/update only. Inventory,
    normalized vector JSON, and the CGS request draft are reviewable git
    artifacts. The request's `sendable: false` state is intentional: a
    drafted recipient/body is not authorization for this app or its
    operators to send email automatically.

43. **The implemented raster path is a documented Python equivalent, not a
    GDAL-CLI workflow.** Pillow + NumPy produce image/tile arrays, tifffile
    writes and validates tiled georeferenced TIFFs, Fiona/pyproj/Shapely
    support the native GIS path, and Poppler extracts PDF images. No
    `gdal_translate`, `gdalwarp`, `gdaladdo`, `gdalinfo`, or `gdal2tiles.py`
    executable is required. Anderson Plate XVIII and Johnston PP 194 Plate 1
    carry native `source_native_ppi: 400`; their 600-ppi output is an
    explicitly labeled 1.5× resample that adds no source information. This
    preserves both the requested output convention and the honest native
    resolution.
