
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
    rank. The result is 19 target rows but 18 unique selected rasters: four
    seed overlays plus 14 ranked-map selections, because Idaho Bonanza and
    Atlanta share the Hailey regional map. Containing plus
    edge/corner-adjacent USGS 7.5-minute quads define catalog-search coverage,
    not a claim that every returned map contains target-scale evidence.

39. **A map-catalog gap is data, not an omitted row or a geologic
    conclusion.** The inventory records `gap` when the searched NGMDB/state
    catalogs return no intersecting geologic map at 1:62,500 or larger, and
    records known regional/mine-scale substitutes and watch-list sources
    separately. “Gap” means no qualifying catalog result was found on the
    retrieval date. It does not mean the area is unmapped in every archive,
    geologically uninteresting, or safe to infer at coarse scale. Grass
    Valley's missing modern CGS quad is intentionally visible this way even
    though Johnston PP 194 supplies a historical substitute. Conversely, a
    selectable regional fallback is not allowed to erase a scale limitation:
    Willow Creek/Pearl (1:125,000), Azurite and New Trail (1:100,000), and
    Excelsior, Mc Grath, Idaho Bonanza/Atlanta, and Mammoth (1:250,000) retain
    explicit notes. Their finer non-georeferenced scans/GIS holdings remain
    upgrade candidates instead of being forced into unreviewed warps.

40. **Georeference confidence and remote verification control publication.**
    A standard 7.5-minute collar may use official quad corners after
    collar/edge inspection. Irregular report plates retain control-point and
    residual/error metadata. Even a reviewed local build is only
    `processing` / `built-awaiting-upload`; `ready` is recorded with
    `--mark-ready` only after its S3/CloudFront objects and alignment are
    verified. A low-confidence or unreviewed fit remains non-toggleable even
    if a tile pyramid can technically be made. The system prefers an honest
    blank over a persuasive bad warp. Every selected NGMDB KMZ is additionally
    pinned to its exact KML/raster members and GroundOverlay bounds, requires
    zero rotation, and has its associated target coordinate checked inside
    the image footprint; the shared Hailey raster contains both targets.

41. **Raster and vector geology have different evidentiary jobs.** Scanned
    sheets are visual overlays. Only a real GIS database normalized with
    source id, attributed unit description, citation, and map scale feeds WS6
    lexicon scoring. DWM-193 therefore drives the sole native 1:24,000 rescan.
    Jackson's seed overlay instead uses the official public NGMDB
    4096×4096 georeferenced KMZ, with a legend cropped from the NGMDB sheet
    preview. Its original CGS PDF remains email-delivered through the
    California ADA workflow, and native attributed GIS is unavailable
    publicly; Jackson therefore has no vector rescan. The project owner
    waived a separate reuse review for this academic deployment, with
    CGS/NGMDB attribution preserved and no open-content license asserted.
    The unsent outbox draft is superseded for raster acquisition and remains
    only a possible native-GIS request. Raster pixels are never vectorized or
    scored as if they were attributed polygons.

42. **S3 is the published-raster system of record; git contains pointers and
    provenance only.** Source scans and extracted plates use ignored
    `pipelines/cache/ws10/` staging while a layer is built; their official
    URLs and pinned checksums remain in git so the cache is reproducible and
    may be evicted. Published COGs, legends/previews, and XYZ tiles live in
    the fixed S3 `ws10-assets/` prefix. Site deploys exclude that prefix from
    every `sync --delete`; the explicit asset upload is add/update only. Inventory,
    normalized vector JSON, and the CGS request draft are reviewable git
    artifacts. The request's `sendable: false` state is intentional: a
    drafted recipient/body is not authorization for this app or its
    operators to send email automatically. Limited local disk is handled one
    layer at a time: build, upload, verify remotely, mark ready while local
    checksums can still be revalidated, then evict only that layer's exact
    COG/XYZ/legend-or-preview outputs. Eviction is refused until remote
    verification is recorded. The guarded asset-eviction command retains
    cached official sources by default; on this space-constrained workstation
    those exact source caches may then be removed separately because their
    official URLs, hashes, byte counts, and all published pointers remain.

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

44. **A target checkbox represents a real selected raster, not a catalog
    row.** MAP INVENTORY shows 19 target controls; the sidebar shows the 18
    unique underlying layers. Enabling either control closes the inventory
    when applicable and pans/fits to the selected footprint so a distant
    overlay is visible. Shared target controls stay synchronized. A target
    whose selected asset is not ready displays its status and receives no
    no-op checkbox.

45. **A map preview is not a legend.** The four seed overlays have reviewed
    crops of actual collar/unit keys and may expose `legend_url`. The 14
    ranked NGMDB selections expose a reduced whole-sheet `preview_url` for
    orientation. Preview and legend metadata are mutually exclusive, and UI
    copy must not invite users to decode map colors from a generic preview.

46. **A stored document's key path is where it is filed, not a claim
    about what it covers.** WS12 stores every cited document twice under
    `docs/{state}/{portal}/{mine_id}/{sha256}/{raw|searchable}.pdf` — the
    raw original as the provenance copy and a searchable copy whose text
    layer sits on those same pages. `doc_id` is the SHA-256 of the raw
    bytes, so a citation survives re-OCR, a re-crawl, and a portal
    redesign. Because one bulletin serves many mines, the `mine_id`
    segment records a single filing subject and the manifest's `subjects`
    array carries every mine the document is actually cited for; a
    document about a district or a whole state is filed under a reserved
    `district-` or `statewide-` key, and a national document under the
    reserved `US` scope. Identifiers from different datasets are
    namespaced (`ws9-`, `stategeo-`, `mrds-`, `usmin-`, `mlrs-`) so two
    catalogues can never collide in one path. This does not mean a
    document is irrelevant to a mine absent from its filing key, and it
    does not make the store a substitute for the publisher's own record.

47. **A searchable copy that equals its original is the honest outcome,
    not a skipped step.** When a source PDF already carries a text layer,
    WS12 stores the searchable variant as those same bytes and records
    `text_layer.status: native`; only an image-only scan gets
    `ocr_added`, with the tool named in the manifest. The builder refuses
    an OCR pass that changes the page count or returns the original bytes,
    so pagination stays 1:1 and page N of a citation is page N of the
    object. Quotes are then located in that text layer, and one that
    cannot be found is recorded `quote_located: false` and shown to the
    reader as "not located on this page" rather than highlighted
    somewhere plausible. This does not assert that OCR is accurate, and
    an unlocated quote is not evidence the source is wrong.

48. **A 3-D model is a digitising bridge, not new evidence.** The modeller
    (`site/model3d.html`, `pipelines/geomodel/`) builds everything from
    sources the map already shows — public terrain, map-scale geology,
    cited grades, BLM centroids — and from what a user traces off a
    georeferenced scan. Workings therefore carry the scan's accuracy, and
    every feature records its source document/page and a confidence
    (`surveyed` / `sketched` / `inferred` / `described`); a traced line is
    never presented as a survey. Interpolated surfaces, kriged blocks and
    implicit shells are deterministic research estimates of those inputs
    with their parameters written into the object's metadata, not
    resource estimates. Files are exchanged only in published formats
    (OMF v0.9/v2.0, DXF, OBJ, GOCAD, Surfer/Geosoft/GXF/ZMAP/Irap grids, UBC,
    CSV, SEG-Y, LAS); proprietary project databases (.aproj, .gdb, .tks)
    are out of scope rather than approximated. Leapfrog's `.msh` layout
    is community-documented and flagged as such on every read.
