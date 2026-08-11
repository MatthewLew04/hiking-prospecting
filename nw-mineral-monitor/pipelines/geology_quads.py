#!/usr/bin/env python3
"""Build the WS10 quad-geology inventory.

The grades JSON is the system of record for target ranking.  Remote catalog
results are a dated snapshot in the emitted inventory: normal runs preserve
that snapshot while recomputing the target selection; ``--refresh`` queries
the official USGS 7.5-minute grid and NGMDB catalog through common.cached_get.

No raster is downloaded here.  Heavy acquisition/georeferencing is handled by
prepare_quad_geology.py and lives below the ignored pipelines/cache/ws10 tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse

from common import HERE, SITE, TODAY, cached_get, write_json


GRADES = os.path.join(SITE, "data", "grades", "grades.json")
OUTPUT_REL = os.path.join("data", "geology-quads", "inventory.json")
OUTPUT = os.path.join(SITE, OUTPUT_REL)
CONFIG = os.path.join(HERE, "config", "geology_quad_seeds.json")
TARGET_OVERLAYS = os.path.join(HERE, "config", "geology_quad_target_overlays.json")
ASSET_STATE = os.path.join(HERE, "config", "geology_quad_assets.json")
MANIFEST = os.path.join(SITE, "data", "manifest.json")

QUAD_API = (
    "https://carto.nationalmap.gov/arcgis/rest/services/"
    "map_indices/MapServer/10/query"
)
NGMDB_API = "https://ngmdb.usgs.gov/ngm-bin/ngm_search_json.pl"
NGMDB_PRODUCT = "https://ngmdb.usgs.gov/Prodesc/proddesc_{id}.htm"
STATE_CATALOG_KEYS = {
    "CA": ("California Geological Survey", "california_information_warehouse"),
    "ID": ("Idaho Geological Survey", "idaho_geological_survey"),
    "MT": ("Montana Bureau of Mines and Geology", "montana_mbmg"),
    "NV": ("Nevada Bureau of Mines and Geology", "nevada_nbmg"),
    "OR": ("Oregon DOGAMI", "oregon_dogami"),
    "WA": ("Washington Geological Survey", "washington_geology"),
    "WY": ("Wyoming State Geological Survey", "wyoming_wsgs"),
}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def column_row(grades: dict, index: int) -> dict:
    return {
        key: value[index]
        for key, value in grades.items()
        if isinstance(value, list) and index < len(value)
    }


def score_row(row: dict) -> float | None:
    """Exact client stakeAnswer score; None means the row is ineligible."""
    au, open_m = row.get("au"), row.get("open")
    if au is None or au < 0.3 or open_m is None or open_m < 400:
        return None
    solid = 1.35 if re.search(r"production|shipped", row.get("basis") or "") else 1.0
    return min(float(au), 5.0) * solid + min(float(open_m), 5000.0) / 12500.0


def stable_grade_key(row: dict) -> str:
    if row.get("dep"):
        return f"mrds:{row['dep']}"
    raw = "|".join(
        [
            row.get("st") or "",
            row.get("cnty") or "",
            slug(row.get("name") or "unnamed"),
            f"{float(row.get('x')):.5f}" if row.get("x") is not None else "",
            f"{float(row.get('y')):.5f}" if row.get("y") is not None else "",
        ]
    )
    return "grade:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def target_id(row: dict) -> str:
    if row.get("dep"):
        return f"grade-dep-{row['dep']}"
    return f"grade-{slug(row.get('st') or 'xx')}-{slug(row.get('name') or 'unnamed')[:38]}-{stable_grade_key(row).split(':')[-1][:8]}"


def grade_ref(row: dict, index: int, rank_all: int | None = None) -> dict:
    return {
        "stable_key": stable_grade_key(row),
        "dep": row.get("dep"),
        "dep_id": row.get("dep"),
        "name": row.get("name"),
        "state": row.get("st"),
        "county": row.get("cnty"),
        "coordinates": [row.get("x"), row.get("y")],
        "source_index": index,
        "source_index_note": "Transient convenience only; stable_key is authoritative.",
        "global_eligible_rank": rank_all,
    }


def ranked_rows(grades: dict) -> tuple[list[tuple[float, int, dict]], dict[int, int]]:
    rows = []
    for index in range(grades["n"]):
        row = column_row(grades, index)
        score = score_row(row)
        if score is not None:
            rows.append((score, index, row))
    # Python's stable sort intentionally preserves grades-table order for ties,
    # matching modern JavaScript Array.sort behavior used by the app.
    rows.sort(key=lambda item: -item[0])
    return rows, {index: rank for rank, (_, index, _) in enumerate(rows, 1)}


def find_seed_grade(grades: dict, match: dict) -> tuple[int, dict]:
    for index in range(grades["n"]):
        row = column_row(grades, index)
        if match.get("dep") and str(row.get("dep") or "") != str(match["dep"]):
            continue
        if match.get("name") and row.get("name") != match["name"]:
            continue
        if match.get("state") and row.get("st") != match["state"]:
            continue
        return index, row
    raise RuntimeError(f"forced seed grade row not found: {match}")


def feature_bounds(feature: dict) -> list[float]:
    rings = feature.get("geometry", {}).get("rings", [])
    points = [point for ring in rings for point in ring]
    if not points:
        raise RuntimeError("USGS quad feature has no polygon geometry")
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def quad_query_url(x: float, y: float) -> str:
    envelope = f"{x - .14},{y - .14},{x + .14},{y + .14}"
    params = {
        "where": "1=1",
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "CELL_ID,CELL_NAME,CELL_MAPCODE,STATE_ALPHA",
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "json",
    }
    return QUAD_API + "?" + urllib.parse.urlencode(params)


def fetch_quads(x: float, y: float) -> tuple[dict, str]:
    url = quad_query_url(x, y)
    payload = json.loads(cached_get(url, ttl_days=30))
    if payload.get("error"):
        raise RuntimeError(f"USGS quad API error: {payload['error']}")
    cells = []
    for feature in payload.get("features", []):
        attrs = feature.get("attributes", {})
        bounds = feature_bounds(feature)
        cells.append(
            {
                "name": attrs.get("CELL_NAME"),
                "mapcode": attrs.get("CELL_MAPCODE"),
                "state": attrs.get("STATE_ALPHA"),
                "bounds": [round(value, 7) for value in bounds],
                "_center": [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2],
            }
        )
    containing = next(
        (
            cell
            for cell in cells
            if cell["bounds"][0] - 1e-6 <= x <= cell["bounds"][2] + 1e-6
            and cell["bounds"][1] - 1e-6 <= y <= cell["bounds"][3] + 1e-6
        ),
        None,
    )
    if not containing:
        raise RuntimeError(f"no containing USGS 7.5-minute cell at {x},{y}")
    cx, cy = containing["_center"]
    neighborhood = [
        cell
        for cell in cells
        if abs(cell["_center"][0] - cx) <= 0.126
        and abs(cell["_center"][1] - cy) <= 0.126
    ]
    neighborhood.sort(key=lambda cell: (-cell["_center"][1], cell["_center"][0]))
    clean = []
    for cell in neighborhood:
        item = {key: value for key, value in cell.items() if key != "_center"}
        item["code"] = item.get("mapcode")
        item["role"] = "containing" if cell is containing else "adjacent"
        clean.append(item)
    return {
        "containing": next(item for item in clean if item["role"] == "containing"),
        "adjacent": [item for item in clean if item["role"] == "adjacent"],
        "source": url,
        "retrieved": TODAY,
    }, url


def ngmdb_query_url(quads: dict) -> str:
    cells = [quads["containing"]] + quads["adjacent"]
    west = min(cell["bounds"][0] for cell in cells)
    south = min(cell["bounds"][1] for cell in cells)
    east = max(cell["bounds"][2] for cell in cells)
    north = max(cell["bounds"][3] for cell in cells)
    params = [
        ("bc_ul", f"{north:.7f},{west:.7f}"),
        ("bc_lr", f"{south:.7f},{east:.7f}"),
        ("scale", "1"),
        ("scale2", "62500"),
        ("geologictheme", "geolgenbed"),
        ("geologictheme", "geolgensur"),
        ("geologictheme", "geolstruc"),
        ("sort", "scale"),
        ("range", "250"),
    ]
    return NGMDB_API + "?" + urllib.parse.urlencode(params)


def scale_denominator(value) -> int:
    match = re.search(r"([\d,]+)\s*$", str(value or ""))
    return int(match.group(1).replace(",", "")) if match else 10**12


def candidate_formats(row: dict) -> list[str]:
    values = []
    if row.get("gis"):
        values.append("GIS")
    if row.get("img") == "True":
        values.append("scan")
    if row.get("online") == "True":
        values.append("online")
    detail = (row.get("formats") or "").strip()
    if detail and detail not in values:
        values.append(detail)
    return values or ["catalog record"]


def fetch_candidates(quads: dict, limit: int = 16) -> tuple[list[dict], str]:
    url = ngmdb_query_url(quads)
    payload = json.loads(cached_get(url, ttl_days=30))
    search = payload.get("ngmdb_catalog_search", {})
    raw = search.get("results", [])
    raw.sort(key=lambda row: (-int(row.get("year") or 0), scale_denominator(row.get("scale"))))
    candidates = []
    for row in raw[:limit]:
        product_id = row.get("id")
        candidates.append(
            {
                "catalog_id": product_id,
                "title": row.get("title"),
                "authors": row.get("authors"),
                "year": row.get("year"),
                "scale": row.get("scale"),
                "scale_denominator": scale_denominator(row.get("scale")),
                "series": row.get("series"),
                "publisher": row.get("published_by"),
                "formats": candidate_formats(row),
                "status": "cataloged",
                "catalog_url": NGMDB_PRODUCT.format(id=product_id),
                "source": "USGS National Geologic Map Database catalog",
            }
        )
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
    return candidates, url


def merge_candidates(catalog: list[dict], additions: list[dict]) -> list[dict]:
    merged, seen = [], set()
    for candidate in additions + catalog:
        key = str(candidate.get("catalog_id") or slug(candidate.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(candidate)
    merged.sort(
        key=lambda row: (
            -int(row.get("year") or 0),
            int(row.get("scale_denominator") or scale_denominator(row.get("scale"))),
        )
    )
    for rank, candidate in enumerate(merged, 1):
        candidate["rank"] = rank
    return merged


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive JSON-object merge used for reviewed/generated asset state."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_asset_state() -> dict:
    if not os.path.exists(ASSET_STATE):
        return {}
    with open(ASSET_STATE) as handle:
        return json.load(handle)


def apply_asset_state(layers: list[dict], state: dict) -> list[dict]:
    by_id = state.get("layers", {})
    return [deep_merge(layer, by_id.get(layer["id"], {})) for layer in layers]


def apply_rescan_state(rescans: list[dict], state: dict) -> list[dict]:
    generated = state.get("rescans", {})
    return [deep_merge(item, generated.get(item["id"], {})) for item in rescans]


def apply_target_overlay_selection(targets: list[dict], overlay_config: dict) -> None:
    """Attach one reviewed overlay selection to every ranked target.

    The reviewed config records exact NGMDB GroundOverlay bounds.  Refuse to
    emit a target checkbox if its coordinate falls outside the selected
    footprint; a working-but-wrong checkbox is worse than an explicit gap.
    """
    layers = {layer["id"]: layer for layer in overlay_config.get("layers", [])}
    mapping = overlay_config.get("target_layers", {})
    for target in targets:
        layer_id = mapping.get(target["id"])
        if not layer_id:
            continue
        layer = layers.get(layer_id)
        if layer is None:
            raise RuntimeError(f"selected overlay {layer_id!r} is not defined")
        bounds = (layer.get("raster") or {}).get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            raise RuntimeError(f"selected overlay {layer_id!r} has invalid bounds")
        x, y = (float(value) for value in target["coordinates"])
        west, south, east, north = (float(value) for value in bounds)
        if not (west <= x <= east and south <= y <= north):
            raise RuntimeError(
                f"selected overlay {layer_id!r} does not contain {target['id']} at {x},{y}"
            )
        target["selected_layer_ids"] = [layer_id]


def base_target(
    row: dict,
    index: int,
    rank: int | None,
    rank_all: int | None,
    score: float | None,
    *,
    forced: dict | None = None,
) -> dict:
    forced = forced or {}
    coordinates = forced.get("coordinates") or [row.get("x"), row.get("y")]
    return {
        "id": forced.get("id") or target_id(row),
        "rank": rank,
        "forced_seed": bool(forced),
        "seed_order": forced.get("order"),
        "name": forced.get("name") or row.get("name"),
        "state": forced.get("state") or row.get("st"),
        "county": forced.get("county") or row.get("cnty"),
        "district": forced.get("district") or row.get("dist"),
        "score": round(score, 6) if score is not None else None,
        "score_method": "stakeAnswer richOpen-weighted score" if score is not None else None,
        "coordinates": coordinates,
        "grade_ref": grade_ref(row, index, rank_all),
        "selected_layer_ids": list(forced.get("selected_layer_ids", [])),
        "catalog_query_url": None,
        "state_catalog": None,
        "quads": None,
        "candidates": [],
        "gap": forced.get("gap"),
        "notes": forced.get("notes"),
    }


def load_existing() -> dict:
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT) as handle:
        return json.load(handle)


def preserve_snapshot(target: dict, existing_by_id: dict) -> None:
    previous = existing_by_id.get(target["id"], {})
    for key in ("quads", "catalog_query_url", "candidates"):
        if previous.get(key) is not None:
            target[key] = previous[key]


def update_manifest(inventory: dict) -> None:
    with open(MANIFEST) as handle:
        manifest = json.load(handle)
    manifest["ws10"] = {
        "map_inventory": {
            "file": OUTPUT_REL.replace(os.sep, "/"),
            "ranked_targets": inventory["stats"]["ranked_targets"],
            "seed_targets": inventory["stats"]["seed_targets"],
            "layers": inventory["stats"]["layers"],
            "ready_layers": inventory["stats"]["ready_layers"],
            "retrieved": inventory["generated"],
        },
        "outbox": {
            "drafts": inventory["outbox"]["drafts"],
            "sendable": False,
        },
    }
    with open(MANIFEST, "w") as handle:
        json.dump(manifest, handle, separators=(",", ":"))
    print("manifest: ws10 map inventory + outbox")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="refresh official USGS quadrangle and NGMDB catalog snapshots",
    )
    parser.add_argument("--top", type=int, default=15, help="ranked grade targets to keep")
    args = parser.parse_args()

    with open(GRADES) as handle:
        grades = json.load(handle)
    with open(CONFIG) as handle:
        config = json.load(handle)
    with open(TARGET_OVERLAYS) as handle:
        overlay_config = json.load(handle)
    existing = load_existing()
    existing_by_id = {target["id"]: target for target in existing.get("targets", [])}

    eligible, all_ranks = ranked_rows(grades)
    targets = []
    for rank, (score, index, row) in enumerate(eligible[: args.top], 1):
        targets.append(base_target(row, index, rank, all_ranks[index], score))

    for seed in config["forced_seeds"]:
        index, row = find_seed_grade(grades, seed["grade_match"])
        targets.append(
            base_target(
                row,
                index,
                None,
                all_ranks.get(index),
                score_row(row),
                forced=seed,
            )
        )

    seed_by_target = {seed["id"]: seed for seed in config["forced_seeds"]}
    for target in targets:
        state_catalog = STATE_CATALOG_KEYS.get(target["state"])
        if state_catalog:
            label, source_key = state_catalog
            target["state_catalog"] = {
                "label": label,
                "url": config["official_sources"][source_key],
                "retrieved": config["official_sources"]["retrieved"],
                "note": (
                    "Authoritative state-survey catalog reference; the saved candidate "
                    "rows below are the reproducible NGMDB snapshot plus curated priorities."
                ),
            }
        x, y = target["coordinates"]
        if x is None or y is None:
            raise RuntimeError(f"target lacks coordinates: {target['id']}")
        if args.refresh:
            target["quads"], _ = fetch_quads(float(x), float(y))
            target["candidates"], target["catalog_query_url"] = fetch_candidates(target["quads"])
        else:
            preserve_snapshot(target, existing_by_id)
            if target["quads"] is None:
                raise RuntimeError(
                    f"no saved catalog snapshot for {target['id']}; run with --refresh"
                )
        seed = seed_by_target.get(target["id"])
        if seed:
            target["candidates"] = merge_candidates(
                target["candidates"], seed.get("priority_candidates", [])
            )
        if not target["candidates"] and not target["gap"]:
            target["gap"] = "No ≤1:62,500 geologic-map record returned by NGMDB for the 3×3 quad neighborhood."

    apply_target_overlay_selection(targets, overlay_config)

    asset_state = load_asset_state()
    configured_layers = list(config["layers"]) + list(overlay_config.get("layers", []))
    layer_ids = [layer["id"] for layer in configured_layers]
    if len(layer_ids) != len(set(layer_ids)):
        raise RuntimeError("duplicate quad-geology layer id in reviewed configs")
    layers = apply_asset_state(configured_layers, asset_state)
    rescans = apply_rescan_state(config.get("rescans", []), asset_state)
    ready_layers = sum(
        1 for layer in layers if layer.get("raster", {}).get("status") == "ready"
    )
    inventory = {
        "schema": "nwmm.ws10.quad-geology.v1",
        "generated": TODAY,
        "asset_base_url": config["asset_base_url"],
        "asset_publication": asset_state.get("publication"),
        "stats": {
            "ranked_targets": args.top,
            "seed_targets": len(config["forced_seeds"]),
            "total_targets": len(targets),
            "layers": len(layers),
            "ready_layers": ready_layers,
            "mapped_targets": sum(bool(target.get("selected_layer_ids")) for target in targets),
            "explicit_gaps": sum(bool(target.get("gap")) for target in targets),
        },
        "methodology": config["methodology"],
        "sources": config["official_sources"],
        "overlay_selection": {
            "config_schema": overlay_config.get("schema"),
            "reviewed": overlay_config.get("generated"),
            "note": overlay_config.get("note"),
        },
        "targets": targets,
        "layers": layers,
        "rescans": rescans,
        "watchlist": config.get("watchlist", []),
        "outbox": config["outbox"],
    }
    write_json(OUTPUT_REL, inventory)
    update_manifest(inventory)


if __name__ == "__main__":
    main()
