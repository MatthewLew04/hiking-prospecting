#!/usr/bin/env python3
"""Validate the committed WS10 inventory, vectors, rasters and UI contract."""
from __future__ import annotations

import argparse
import hashlib
import json
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


def local_asset(url: str) -> Path:
    prefix = "/ws10-assets/"
    require(url.startswith(prefix), f"asset URL is outside {prefix}: {url}")
    return ASSETS / url[len(prefix) :]


def validate_targets(inventory: dict) -> None:
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
        == {"seed-delamar", "seed-jackson", "seed-black-pine", "seed-grass-valley"},
        "forced-seed set changed",
    )
    for target in inventory["targets"]:
        quads = target.get("quads") or {}
        require(quads.get("containing"), f"{target['id']} lacks a containing quad")
        require(len(quads.get("adjacent") or []) == 8, f"{target['id']} lacks eight adjacent quads")
        for quad in [quads["containing"]] + quads["adjacent"]:
            require(quad.get("code") or quad.get("mapcode"), f"{target['id']} quad lacks code")
            require(len(quad.get("bounds") or []) == 4, f"{target['id']} quad lacks bounds")
        require(target.get("candidates") or target.get("gap"), f"{target['id']} is silently empty")
    gaps = {target["id"] for target in inventory["targets"] if target.get("gap")}
    require({"seed-jackson", "seed-grass-valley"} <= gaps, "required gaps are not explicit")


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


def validate_layers(inventory: dict, skip_assets: bool) -> None:
    layers = {layer["id"]: layer for layer in inventory["layers"]}
    expected_ready = {
        "dwm-193",
        "anderson-1931-plate-xviii",
        "johnston-pp194-plate-1",
    }
    ready = {
        layer_id
        for layer_id, layer in layers.items()
        if (layer.get("raster") or {}).get("status") == "ready"
    }
    require(ready == expected_ready, f"unexpected ready layer set: {sorted(ready)}")
    jackson = layers["jackson-pgm-2019"]["raster"]
    require(jackson.get("status") == "blocked", "Jackson must remain blocked")
    require(not jackson.get("tile_url_template"), "Jackson must not expose a tile pointer")
    require(inventory["stats"]["ready_layers"] == len(ready), "ready-layer statistic is stale")
    if skip_assets:
        return
    for layer_id in ready:
        raster = layers[layer_id]["raster"]
        cog = local_asset(raster["cog_url"])
        legend = local_asset(raster["legend_url"])
        tile_dir = ASSETS / "tiles" / layer_id
        for path in (cog, legend, tile_dir):
            require(path.exists(), f"missing local asset: {path}")
        tile_count = sum(1 for path in tile_dir.rglob("*.webp") if path.is_file())
        require(tile_count == raster["tiles"]["tile_count"], f"{layer_id} tile-count mismatch")
        require(digest(cog) == raster["cog"]["sha256"], f"{layer_id} COG checksum mismatch")
        require(digest(legend) == raster["legend"]["sha256"], f"{layer_id} legend checksum mismatch")
        require(raster.get("remote_verified"), f"{layer_id} lacks remote verification date")
        validate_cog(cog)


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
    for marker in ("loadQuadGeology", "quadSetLayer", "quadOpacity", "DRAFTS ONLY"):
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
        if re.search(r"\.(?:pdf|tiff?|png|jpe?g|webp)$", path, re.I)
        and ("ws10" in path.lower() or "geology-quad" in path.lower())
    ]
    require(not heavy, f"WS10 raster/source files are tracked: {heavy}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-assets", action="store_true", help="skip ignored local raster checks")
    args = parser.parse_args()
    inventory = load(SITE / "data" / "geology-quads" / "inventory.json")
    require(inventory.get("schema") == "nwmm.ws10.quad-geology.v1", "inventory schema changed")
    validate_targets(inventory)
    validate_layers(inventory, args.skip_assets)
    validate_vector(inventory)
    validate_outbox(inventory)
    validate_ui()
    validate_git_policy()
    print(
        "WS10 validation passed: 15 ranked + 4 seeds, 3 ready/1 blocked layers, "
        "native-vector rescan, guarded outbox, UI syntax, and no tracked rasters."
    )


if __name__ == "__main__":
    main()
