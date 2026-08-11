#!/usr/bin/env python3
"""Validate the committed WS10 inventory, vectors, rasters and UI contract."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from geology_quads import ranked_rows, stable_grade_key


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = ROOT / "site"
ASSETS = HERE / "cache" / "ws10" / "assets"
TARGET_OVERLAYS = HERE / "config" / "geology_quad_target_overlays.json"
SEED_CONFIG = HERE / "config" / "geology_quad_seeds.json"

EXPECTED_SEED_SELECTIONS = {
    "seed-delamar": "dwm-193",
    "seed-jackson": "jackson-pgm-2019",
    "seed-black-pine": "anderson-1931-plate-xviii",
    "seed-grass-valley": "johnston-pp194-plate-1",
}
JACKSON_SOURCE_SHA256 = (
    "03747dd438ff1aef2cfd67b041d73ef46b0dfe12c338d090a670b31114df5b86"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BOUNDS_TOLERANCE = 1e-9


def load(path: Path):
    with path.open() as handle:
        return json.load(handle)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def require(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def recorded_int(value, message: str, *, positive: bool = False) -> int:
    require(
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value > 0 if positive else value >= 0),
        message,
    )
    return value


def require_sha256(value, message: str) -> str:
    require(isinstance(value, str) and SHA256.fullmatch(value) is not None, message)
    return value


def same_bounds(actual, expected, *, tolerance: float = BOUNDS_TOLERANCE) -> bool:
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    if len(actual) != 4 or len(expected) != 4:
        return False
    try:
        return all(
            math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
            for left, right in zip(actual, expected)
        )
    except (TypeError, ValueError):
        return False


def point_in_bounds(coordinates, bounds) -> bool:
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        return False
    if not isinstance(bounds, list) or len(bounds) != 4:
        return False
    try:
        x, y = (float(value) for value in coordinates)
        west, south, east, north = (float(value) for value in bounds)
    except (TypeError, ValueError):
        return False
    return (
        west - BOUNDS_TOLERANCE <= x <= east + BOUNDS_TOLERANCE
        and south - BOUNDS_TOLERANCE <= y <= north + BOUNDS_TOLERANCE
    )


def local_asset(url: str) -> Path:
    prefix = "/ws10-assets/"
    require(url.startswith(prefix), f"asset URL is outside {prefix}: {url}")
    return ASSETS / url[len(prefix) :]


def validate_overlay_config(overlay_config: dict) -> tuple[set[str], dict[str, str]]:
    require(
        overlay_config.get("schema") == "nwmm.ws10.target-overlays.v1",
        "target-overlay config schema changed",
    )
    configured_layers = {
        layer["id"]: layer for layer in overlay_config.get("layers", [])
    }
    raster_specs = overlay_config.get("raster_specs") or {}
    sources = overlay_config.get("sources") or {}
    mapping = overlay_config.get("target_layers") or {}
    require(len(configured_layers) == 14, "expected 14 ranked-map layers")
    require(len(mapping) == 15, "expected 15 ranked target-overlay selections")
    require(
        set(configured_layers) == set(raster_specs) == set(mapping.values()),
        "ranked-map layers, raster specs, and target selections differ",
    )
    counts = Counter(mapping.values())
    require(counts.get("hailey-of-91-340") == 2, "Hailey must serve two ranked targets")
    require(
        all(count == 1 for layer_id, count in counts.items() if layer_id != "hailey-of-91-340"),
        "only Hailey may be shared by ranked targets",
    )

    referenced_sources = set()
    for layer_id, configured in configured_layers.items():
        spec = raster_specs[layer_id]
        source_name = spec.get("source")
        source = sources.get(source_name)
        require(source is not None, f"{layer_id} references an unknown NGMDB source")
        referenced_sources.add(source_name)
        require(
            "ngmdb.usgs.gov/ngm-bin/pdp/download.pl" in source.get("url", ""),
            f"{layer_id} source is not an NGMDB KMZ holding",
        )
        require_sha256(source.get("sha256"), f"{layer_id} source checksum is invalid")
        require(
            configured.get("source_url") == source["url"],
            f"{layer_id} source URL differs from its reviewed source record",
        )
        require(
            same_bounds((configured.get("raster") or {}).get("bounds"), spec.get("bounds")),
            f"{layer_id} configured bounds differ from its reviewed GroundOverlay",
        )
    require(referenced_sources == set(sources), "unused or missing NGMDB source records")
    return set(configured_layers), mapping


def validate_targets(
    inventory: dict,
    layers: dict[str, dict],
    ranked_layer_ids: set[str],
    ranked_mapping: dict[str, str],
) -> None:
    grades = load(SITE / "data" / "grades" / "grades.json")
    expected, _ = ranked_rows(grades)
    ranked = [target for target in inventory["targets"] if target.get("rank") is not None]
    seeds = [target for target in inventory["targets"] if target.get("forced_seed")]
    require(len(ranked) == 15, f"expected 15 ranked targets, got {len(ranked)}")
    require(len(seeds) == 4, f"expected four forced seeds, got {len(seeds)}")
    expected_keys = [stable_grade_key(row) for _, _, row in expected[:15]]
    actual_keys = [target["grade_ref"]["stable_key"] for target in ranked]
    require(actual_keys == expected_keys, "ranked targets do not match the richOpen formula")
    require([target["rank"] for target in ranked] == list(range(1, 16)), "rank sequence")
    require(
        {target["id"] for target in seeds}
        == set(EXPECTED_SEED_SELECTIONS),
        "forced-seed set changed",
    )
    require(len(inventory["targets"]) == 19, "expected exactly 19 inventory targets")
    expected_target_ids = set(ranked_mapping) | set(EXPECTED_SEED_SELECTIONS)
    require(
        {target["id"] for target in inventory["targets"]} == expected_target_ids,
        "inventory targets differ from the reviewed overlay selections",
    )
    for target in inventory["targets"]:
        quads = target.get("quads") or {}
        require(quads.get("containing"), f"{target['id']} lacks a containing quad")
        require(len(quads.get("adjacent") or []) == 8, f"{target['id']} lacks eight adjacent quads")
        for quad in [quads["containing"]] + quads["adjacent"]:
            require(quad.get("code") or quad.get("mapcode"), f"{target['id']} quad lacks code")
            require(len(quad.get("bounds") or []) == 4, f"{target['id']} quad lacks bounds")
        require(target.get("candidates") or target.get("gap"), f"{target['id']} is silently empty")

        expected_layer_id = (
            EXPECTED_SEED_SELECTIONS.get(target["id"])
            or ranked_mapping.get(target["id"])
        )
        selected = target.get("selected_layer_ids")
        require(
            isinstance(selected, list) and len(selected) == 1,
            f"{target['id']} must have exactly one selected_layer_id",
        )
        layer_id = selected[0]
        require(layer_id == expected_layer_id, f"{target['id']} selection differs from config")
        require(layer_id in layers, f"{target['id']} selects missing layer {layer_id}")
        require(
            target["id"] in (layers[layer_id].get("target_ids") or []),
            f"{layer_id} does not declare target {target['id']}",
        )
        bounds = (layers[layer_id].get("raster") or {}).get("bounds")
        require(
            point_in_bounds(target.get("coordinates"), bounds),
            f"{target['id']} coordinate lies outside selected layer {layer_id}",
        )

    selected_counts = Counter(
        target["selected_layer_ids"][0] for target in inventory["targets"]
    )
    require(len(selected_counts) == 18, "19 targets must resolve to 18 unique layers")
    require(selected_counts.get("hailey-of-91-340") == 2, "Hailey sharing changed")
    require(
        set(selected_counts) == ranked_layer_ids | set(EXPECTED_SEED_SELECTIONS.values()),
        "selected target layers differ from the 14 ranked maps plus four seeds",
    )
    gaps = {target["id"] for target in inventory["targets"] if target.get("gap")}
    require("seed-grass-valley" in gaps, "Grass Valley gap is not explicit")
    require("seed-jackson" not in gaps, "Jackson still reports a raster-acquisition gap")


def validate_cog(path: Path) -> None:
    try:
        import tifffile
    except ImportError as exc:
        raise RuntimeError("tifffile is required for local asset validation") from exc
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        require(page.is_tiled, f"{path} is not tiled")
        require(all(tag in page.tags for tag in (33550, 33922, 34735)), f"{path} lacks GeoTIFF tags")
        offsets = list(page.dataoffsets)
        require(offsets == sorted(offsets), f"{path} tile offsets are not monotonic")
        require(bool(offsets) and page.offset < offsets[0], f"{path} IFD is not before tiles")


def publication_marker(raster: dict) -> bool:
    return (
        raster.get("status") == "ready"
        and raster.get("build_status") == "uploaded-and-verified"
        and bool(raster.get("published"))
        and bool(raster.get("remote_verified"))
    )


def validate_object_pointer(layer_id: str, raster: dict, kind: str) -> int:
    metadata = raster.get(kind)
    url = raster.get(f"{kind}_url")
    require(bool(metadata) == bool(url), f"{layer_id} {kind} metadata/URL pairing changed")
    if not metadata:
        return 0
    folder, extension = ("cogs", ".tif") if kind == "cog" else (f"{kind}s", ".webp")
    expected_url = f"/ws10-assets/{folder}/{layer_id}{extension}"
    require(url == expected_url, f"{layer_id} {kind} URL is not canonical")
    require(
        metadata.get("object_key") == expected_url.removeprefix("/"),
        f"{layer_id} {kind} object pointer differs from its URL",
    )
    require_sha256(metadata.get("sha256"), f"{layer_id} {kind} checksum is invalid")
    return recorded_int(metadata.get("bytes"), f"{layer_id} {kind} byte count is invalid", positive=True)


def validate_recorded_raster(
    layer_id: str,
    raster: dict,
    *,
    expects_preview: bool,
) -> tuple[int, int]:
    require(raster.get("status") == "ready", f"{layer_id} is not ready")
    require(raster.get("block_reason") in (None, ""), f"{layer_id} retains a block reason")
    require(publication_marker(raster), f"{layer_id} lacks an explicit remote-publication marker")
    require(raster.get("verification_note"), f"{layer_id} lacks a verification note")
    bounds = raster.get("bounds")
    require(
        isinstance(bounds, list)
        and len(bounds) == 4
        and all(isinstance(value, (int, float)) and math.isfinite(value) for value in bounds),
        f"{layer_id} has invalid bounds",
    )
    minzoom = recorded_int(raster.get("minzoom"), f"{layer_id} minzoom is invalid")
    maxzoom = recorded_int(raster.get("maxzoom"), f"{layer_id} maxzoom is invalid")
    require(minzoom <= maxzoom, f"{layer_id} zoom range is reversed")

    expected_template = f"/ws10-assets/tiles/{layer_id}/{{z}}/{{x}}/{{y}}.webp"
    require(
        raster.get("tile_url_template") == expected_template,
        f"{layer_id} tile URL template is not canonical",
    )
    tiles = raster.get("tiles") or {}
    require(
        tiles.get("object_prefix") == f"ws10-assets/tiles/{layer_id}",
        f"{layer_id} tile object prefix is invalid",
    )
    tile_count = recorded_int(
        tiles.get("tile_count"), f"{layer_id} has no recorded tiles", positive=True
    )
    tile_bytes = recorded_int(
        tiles.get("bytes"), f"{layer_id} tile bytes are missing", positive=True
    )
    require(tiles.get("format") == "WebP", f"{layer_id} tile format changed")
    require(tiles.get("scheme") == "XYZ", f"{layer_id} tile scheme changed")
    require(tiles.get("tile_size") == 256, f"{layer_id} tile size changed")
    zoom_counts = tiles.get("zoom_counts") or {}
    expected_zooms = {str(zoom) for zoom in range(minzoom, maxzoom + 1)}
    require(set(zoom_counts) == expected_zooms, f"{layer_id} zoom-count keys are incomplete")
    counted_tiles = sum(
        recorded_int(count, f"{layer_id} has an invalid z{zoom} tile count", positive=True)
        for zoom, count in zoom_counts.items()
    )
    require(counted_tiles == tile_count, f"{layer_id} recorded tile counts do not add up")

    cog_bytes = validate_object_pointer(layer_id, raster, "cog")
    cog = raster.get("cog") or {}
    require(cog.get("tiled") is True, f"{layer_id} COG is not recorded as tiled")
    require(cog.get("ifd_before_tile_data") is True, f"{layer_id} COG IFD order changed")
    require(cog.get("crs") == "EPSG:4326", f"{layer_id} COG CRS changed")

    has_legend = bool(raster.get("legend") or raster.get("legend_url"))
    has_preview = bool(raster.get("preview") or raster.get("preview_url"))
    require(not (has_legend and has_preview), f"{layer_id} publishes both legend and preview")
    require(has_legend or has_preview, f"{layer_id} lacks a legend or map preview")
    require(
        has_preview is expects_preview,
        f"{layer_id} supplemental image is labeled as the wrong asset kind",
    )
    supplemental_kind = "preview" if has_preview else "legend"
    supplemental_bytes = validate_object_pointer(layer_id, raster, supplemental_kind)
    absent_kind = "legend" if has_preview else "preview"
    require(
        not raster.get(absent_kind) and not raster.get(f"{absent_kind}_url"),
        f"{layer_id} has stale {absent_kind} metadata",
    )
    if has_preview:
        require(
            raster["preview"].get("kind") == "map-preview",
            f"{layer_id} preview is not explicitly labeled map-preview",
        )
    return tile_count + 2, tile_bytes + cog_bytes + supplemental_bytes


def validate_local_layer_assets(layer_id: str, raster: dict) -> None:
    """Validate every retained artifact, while accepting explicit post-publish eviction."""
    paths = {
        "cog": local_asset(raster["cog_url"]),
        "tiles": ASSETS / "tiles" / layer_id,
    }
    for kind in ("legend", "preview"):
        if raster.get(kind):
            paths[kind] = local_asset(raster[f"{kind}_url"])

    retained = [path for path in paths.values() if path.exists()]
    if not retained:
        require(
            publication_marker(raster),
            f"{layer_id} local assets are absent without a publication marker",
        )
        return

    cog = paths["cog"]
    if cog.exists():
        require(cog.is_file(), f"{layer_id} local COG is not a file")
        require(digest(cog) == raster["cog"]["sha256"], f"{layer_id} COG checksum mismatch")
        validate_cog(cog)
    tile_dir = paths["tiles"]
    if tile_dir.exists():
        require(tile_dir.is_dir(), f"{layer_id} local tiles path is not a directory")
        tile_count = sum(1 for path in tile_dir.rglob("*.webp") if path.is_file())
        require(tile_count == raster["tiles"]["tile_count"], f"{layer_id} tile-count mismatch")
    for kind in ("legend", "preview"):
        path = paths.get(kind)
        if path is not None and path.exists():
            require(path.is_file(), f"{layer_id} local {kind} is not a file")
            require(
                digest(path) == raster[kind]["sha256"],
                f"{layer_id} {kind} checksum mismatch",
            )


def validate_source_provenance(
    layers: dict[str, dict],
    overlay_config: dict,
    seed_config: dict,
    ranked_layer_ids: set[str],
) -> None:
    sources = overlay_config["sources"]
    specs = overlay_config["raster_specs"]
    configured = {layer["id"]: layer for layer in overlay_config["layers"]}
    for layer_id in ranked_layer_ids:
        layer = layers[layer_id]
        spec = specs[layer_id]
        reviewed_source = sources[spec["source"]]
        built_source = (layer.get("build") or {}).get("source") or {}
        require(layer.get("source_url") == reviewed_source["url"], f"{layer_id} source URL changed")
        require(
            built_source.get("source_url") == reviewed_source["url"],
            f"{layer_id} built source URL differs from reviewed config",
        )
        require(
            built_source.get("sha256") == reviewed_source["sha256"],
            f"{layer_id} NGMDB source checksum differs from reviewed config",
        )
        recorded_int(
            built_source.get("bytes"), f"{layer_id} source byte count is invalid", positive=True
        )
        raster_bounds = layer["raster"].get("bounds")
        require(
            same_bounds(raster_bounds, spec.get("bounds")),
            f"{layer_id} raster bounds differ from reviewed config",
        )
        extraction = (layer.get("build") or {}).get("extraction") or {}
        require(
            same_bounds(extraction.get("kml_bounds"), spec.get("bounds")),
            f"{layer_id} extracted KML bounds differ from reviewed config",
        )
        require(
            same_bounds((configured[layer_id].get("raster") or {}).get("bounds"), raster_bounds),
            f"{layer_id} inventory bounds differ from configured layer bounds",
        )

    seed_layers = {layer["id"]: layer for layer in seed_config.get("layers", [])}
    require(set(seed_layers) == set(EXPECTED_SEED_SELECTIONS.values()), "seed layer set changed")
    forced_selections = {
        seed["id"]: (seed.get("selected_layer_ids") or [])
        for seed in seed_config.get("forced_seeds", [])
    }
    require(
        forced_selections
        == {target_id: [layer_id] for target_id, layer_id in EXPECTED_SEED_SELECTIONS.items()},
        "forced-seed overlay selections changed",
    )

    jackson_layer = layers["jackson-pgm-2019"]
    jackson_raster = jackson_layer["raster"]
    jackson_config = seed_layers["jackson-pgm-2019"]
    expected_bounds = jackson_config["raster"]["bounds"]
    require(
        jackson_layer.get("source_url") == jackson_config.get("source_url")
        and "ngmdb.usgs.gov/ngm-bin/pdp/download.pl" in jackson_layer.get("source_url", ""),
        "Jackson is not attributed to the configured public NGMDB KMZ holding",
    )
    require(
        ((jackson_layer.get("build") or {}).get("source") or {}).get("sha256")
        == JACKSON_SOURCE_SHA256,
        "Jackson NGMDB KMZ checksum changed",
    )
    require(
        same_bounds(jackson_raster.get("bounds"), expected_bounds),
        "Jackson bounds differ from the configured NGMDB GroundOverlay",
    )
    jackson_extraction = (jackson_layer.get("build") or {}).get("extraction") or {}
    require(
        same_bounds(jackson_extraction.get("kml_bounds"), expected_bounds),
        "Jackson extracted KML bounds differ from config",
    )


def validate_publication(
    inventory: dict,
    expected_layer_ids: set[str],
    object_count: int,
    byte_count: int,
) -> None:
    publication = inventory.get("asset_publication") or {}
    require(publication.get("object_prefix") == "ws10-assets/", "publication prefix changed")
    require(publication.get("cloudfront_base") == "/ws10-assets/", "CloudFront base changed")
    require(
        publication.get("ready_layer_ids") == sorted(expected_layer_ids),
        "publication ready-layer list is stale",
    )
    require(
        publication.get("object_count") == object_count,
        "publication object count differs from layer metadata",
    )
    require(
        publication.get("bytes") == byte_count,
        "publication byte count differs from layer metadata",
    )
    require(publication.get("remote_verified"), "publication lacks remote verification")
    require(
        publication.get("summary_method") == "state-derived-ready-layer-asset-metadata",
        "publication summary is not derived from cumulative ready-layer metadata",
    )


def validate_layers(
    inventory: dict,
    layers: dict[str, dict],
    overlay_config: dict,
    seed_config: dict,
    ranked_layer_ids: set[str],
    skip_assets: bool,
) -> None:
    expected_ready = ranked_layer_ids | set(EXPECTED_SEED_SELECTIONS.values())
    require(len(expected_ready) == 18, "expected 14 ranked-map plus four seed layers")
    require(set(layers) == expected_ready, "inventory layer set differs from the 18 selected maps")
    ready = {
        layer_id
        for layer_id, layer in layers.items()
        if (layer.get("raster") or {}).get("status") == "ready"
    }
    require(ready == expected_ready, f"unexpected ready layer set: {sorted(ready)}")

    expected_targets_by_layer: dict[str, set[str]] = {layer_id: set() for layer_id in ready}
    for target in inventory["targets"]:
        expected_targets_by_layer[target["selected_layer_ids"][0]].add(target["id"])

    total_objects = 0
    total_bytes = 0
    for layer_id in sorted(ready):
        layer = layers[layer_id]
        require(
            set(layer.get("target_ids") or []) == expected_targets_by_layer[layer_id],
            f"{layer_id} target associations differ from reviewed selections",
        )
        objects, bytes_ = validate_recorded_raster(
            layer_id,
            layer.get("raster") or {},
            expects_preview=layer_id in ranked_layer_ids,
        )
        total_objects += objects
        total_bytes += bytes_
        if not skip_assets:
            validate_local_layer_assets(layer_id, layer["raster"])

    validate_source_provenance(
        layers, overlay_config, seed_config, ranked_layer_ids
    )
    validate_publication(inventory, expected_ready, total_objects, total_bytes)
    stats = inventory.get("stats") or {}
    require(stats.get("layers") == 18, "layer statistic is stale")
    require(stats.get("ready_layers") == 18, "ready-layer statistic is stale")
    require(stats.get("mapped_targets") == 19, "mapped-target statistic is stale")
    require(stats.get("total_targets") == 19, "total-target statistic is stale")


def validate_vector(inventory: dict) -> None:
    geology = load(SITE / "data" / "geology" / "delamar24k.json")
    targets = load(SITE / "data" / "targets" / "delamar24k.json")
    source = geology["sources"]["dwm193"]
    require(source.get("kind") == "native vector", "DWM source is not labeled native vector")
    require(source.get("scoring_source") is True, "DWM source is not an explicit scoring source")
    require(source.get("native_scale") == 24000, "DWM native scale changed")
    require(len(geology["units"]) == 523 and len(geology["faults"]) == 192, "DWM counts changed")
    require(sum(unit.get("sn") == "sinter" for unit in geology["units"]) == 36, "sinter overlays changed")
    require(targets["stats"]["raster_units_excluded"] == 0, "unexpected raster unit entered scorer")
    require("explicitly excluded" in targets["honesty"], "rescan does not state raster exclusion")
    rescans = {item["id"]: item for item in inventory.get("rescans", [])}
    rescan = rescans.get("delamar24k")
    require(rescan and rescan.get("status") == "ready", "De Lamar rescan is not ready")
    require(rescan.get("targets") == targets["stats"]["targets"], "rescan summary is stale")


def validate_outbox(inventory: dict) -> None:
    drafts = inventory.get("outbox", {}).get("drafts", [])
    require(len(drafts) == 1 and drafts[0].get("sendable") is False, "outbox guardrail changed")
    draft = load(SITE / drafts[0]["data_url"])
    require(draft.get("sendable") is False and draft.get("sent") is False, "draft became sendable/sent")
    email = draft["to"][0]["email"]
    require(email == "Brian.Swanson@conservation.ca.gov", "draft recipient differs from reviewed contact")
    require(email in drafts[0]["to"] and draft["subject"] == drafts[0]["subject"], "outbox summary mismatch")


def validate_ui() -> None:
    html = (SITE / "index.html").read_text()
    for marker in (
        "loadQuadGeology",
        "quadSetLayer",
        "quadSetTargetLayer",
        "data-quad-target-layer",
        "TARGET OVERLAY SWITCHER",
        "quadOpacity",
        "DRAFTS ONLY",
    ):
        require(marker in html, f"UI marker missing: {marker}")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S | re.I)
    require(bool(scripts), "no inline script found")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write("\n".join(scripts))
        path = handle.name
    try:
        subprocess.run(["node", "--check", path], check=True, capture_output=True, text=True)
    finally:
        os.unlink(path)


def validate_git_policy() -> None:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    heavy = [
        path
        for path in result.stdout.splitlines()
        if re.search(r"\.(?:pdf|tiff?|png|jpe?g|webp|kmz|zip)$", path, re.I)
        and ("ws10" in path.lower() or "geology-quad" in path.lower())
    ]
    require(not heavy, f"WS10 raster/source files are tracked: {heavy}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-assets", action="store_true", help="skip ignored local raster checks")
    args = parser.parse_args()
    inventory = load(SITE / "data" / "geology-quads" / "inventory.json")
    overlay_config = load(TARGET_OVERLAYS)
    seed_config = load(SEED_CONFIG)
    require(inventory.get("schema") == "nwmm.ws10.quad-geology.v1", "inventory schema changed")
    layer_ids = [layer["id"] for layer in inventory.get("layers", [])]
    require(len(layer_ids) == len(set(layer_ids)), "inventory contains duplicate layer ids")
    layers = {layer["id"]: layer for layer in inventory["layers"]}
    ranked_layer_ids, ranked_mapping = validate_overlay_config(overlay_config)
    validate_targets(inventory, layers, ranked_layer_ids, ranked_mapping)
    validate_layers(
        inventory,
        layers,
        overlay_config,
        seed_config,
        ranked_layer_ids,
        args.skip_assets,
    )
    validate_vector(inventory)
    validate_outbox(inventory)
    validate_ui()
    validate_git_policy()
    print(
        "WS10 validation passed: 19 mapped targets, 18 ready/0 blocked tiled layers, "
        "reviewed source provenance, native-vector rescan, guarded outbox, target "
        "switcher UI, and no tracked rasters."
    )


if __name__ == "__main__":
    main()
