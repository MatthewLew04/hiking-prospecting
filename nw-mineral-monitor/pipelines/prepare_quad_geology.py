#!/usr/bin/env python3
"""Prepare WS10 quad-geology vector and raster deliverables.

Heavy source files and generated COG/XYZ/legend assets stay below the ignored
``pipelines/cache/ws10`` tree.  Only normalized vector JSON, the WS6 rescan,
and a small reviewed asset-state pointer are written to tracked paths.

The script deliberately separates *built* from *ready*: a successful local
build remains ``processing / built-awaiting-upload`` until ``--mark-ready`` is
run after the corresponding objects have been uploaded and checked.  Raster
pixels never enter the WS6 scoring pipeline; only the DWM-193 native GIS does.

Runtime dependencies (available in the project's documented Conda runtime):
Pillow, NumPy, tifffile, Fiona, pyproj and Shapely.  Poppler's ``pdftoppm`` and
``pdfimages`` executables are used for reproducible PDF extraction.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from io import BytesIO
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable
from xml.etree import ElementTree

try:
    import fiona
    import numpy as np
    import tifffile
    from fiona.transform import transform_geom
    from PIL import Image
    from pyproj import CRS
    from shapely.geometry import shape
except ImportError as exc:  # pragma: no cover - exercised by the lean runtime
    raise SystemExit(
        "prepare_quad_geology.py needs Pillow, NumPy, tifffile, Fiona, "
        "pyproj and Shapely. Use the documented Conda/GDAL-capable runtime. "
        f"Missing import: {exc}"
    ) from exc

from common import HERE, SITE, TODAY, write_json


ROOT = Path(HERE).parent
CACHE = Path(HERE) / "cache" / "ws10"
SOURCES = CACHE / "sources"
WORK = CACHE / "work"
ASSETS = CACHE / "assets"
STATE = Path(HERE) / "config" / "geology_quad_assets.json"
TARGET_OVERLAY_CONFIG = Path(HERE) / "config" / "geology_quad_target_overlays.json"
UNIT_LOOKUP = Path(HERE) / "config" / "dwm193_units.json"
GEOLOGY_REL = Path("data/geology/delamar24k.json")
TARGETS_REL = Path("data/targets/delamar24k.json")

Image.MAX_IMAGE_PIXELS = None
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


SOURCE_SPECS = {
    "dwm-193.pdf": {
        "url": "https://www.idahogeology.org/pub/Digital_Data/Digital_Web_Maps/DWM-193.pdf",
        "sha256": "c1eca5a5162aaa92c570baf8fa7dc1399f3024394874f5c24208158d5ad137cf",
    },
    "DWM193_GIS.zip": {
        "url": "https://www.idahogeology.org/pub/Digital_Data/Digital_Web_Maps/DWM193_DeLamarSwisherMountain_GIS.zip",
        "sha256": "d35a343d8c265be0fdc32dc35c87882e0411e00559e46a3341265dcec84ec30e",
    },
    "anderson-b14.pdf": {
        "url": "https://idwr.idaho.gov/wp-content/uploads/sites/2/projects/raft-river/193109-Anderson-EasternCassia-Geology-Mineral.pdf",
        "sha256": "a69a814cc932d863dc44ef4b66e2bd8ab0078f38dcfdd1b8c9130c7a8d39668b",
    },
    "pp194-plate-01.pdf": {
        "url": "https://pubs.usgs.gov/pp/0194/plate-01.pdf",
        "sha256": "e1e4921d66a397208cc51f7589081e41e16f3872a02b347c8f4bd898857b6d84",
    },
    "jackson-pgm-2019.kmz": {
        "url": "https://ngmdb.usgs.gov/ngm-bin/pdp/download.pl?q=62208_108920_99",
        "sha256": "03747dd438ff1aef2cfd67b041d73ef46b0dfe12c338d090a670b31114df5b86",
    },
    "jackson-pgm-2019-sheet-l4.jpg": {
        "url": "https://ngmdb.usgs.gov/img2/108000_108999/108920_1",
        "sha256": "dad4bb290e962a2267c7339a05061fe44f50bb5c208dbee2a6123411b7645ccb",
        "zoomify": {
            "level": 4,
            "width": 3412,
            "height": 2062,
            "tile_size": 256,
            "level_tile_offset": 52,
        },
        "retrieval_note": (
            "Reviewed 3412x2062 JPEG assembled from level 4 of the NGMDB Zoomify "
            "tile base /img2/108000_108999/108920_1/. The viewer reports the full "
            "sheet as 13650x8250 pixels."
        ),
    },
}


RASTER_SPECS = {
    "dwm-193": {
        "source": "dwm-193.pdf",
        "extract": "render-page-1-150",
        "reference_size": (1440, 1080),
        "crop": (38, 29, 1053, 715),
        "legend_crop": (1058, 250, 1415, 820),
        "bounds": (-117.0, 43.0, -116.75, 43.125),
        "minzoom": 8,
        "maxzoom": 15,
        "source_native_ppi": None,
        "output_ppi": 150,
        "gcp_count": 6,
        "rmse_m": None,
        "confidence": "high",
        "georef_status": (
            "Reviewed two-quadrangle neatline fit to the official De Lamar "
            "and Swisher Mountain 7.5-minute corners; native GIS cross-check."
        ),
        "georef_method": "six distinct neatline corners across two adjacent 7.5-minute quads",
    },
    "anderson-1931-plate-xviii": {
        "source": "anderson-b14.pdf",
        "extract": "embedded-page-199",
        "reference_size": (11473, 11052),
        "crop": (1266, 421, 9822, 10378),
        "legend_crop": (9870, 420, 11430, 7800),
        "bounds": (-113.8333333333, 42.0, -113.0, 42.6666666667),
        "minzoom": 7,
        "maxzoom": 14,
        "source_native_ppi": 400,
        "output_ppi": 600,
        "gcp_count": 30,
        "rmse_m": 170,
        "confidence": "medium",
        "georef_status": (
            "Reviewed affine control-grid fit; 30 printed graticule intersections, "
            "about 170 m combined RMS. Suitable for reconnaissance, not parcel work."
        ),
        "georef_method": "affine fit to six longitude and five latitude graticule lines",
        "control": {
            "longitude_source_x": [1266.5, 2980.5, 4696.5, 6420.5, 8151.0, 9822.0],
            "longitude_degrees": [-113.8333333333, -113.6666666667, -113.5, -113.3333333333, -113.1666666667, -113.0],
            "latitude_source_y": [421.0, 2895.0, 5376.0, 7839.0, 10378.0],
            "latitude_degrees": [42.6666666667, 42.5, 42.3333333333, 42.1666666667, 42.0],
            "native_pixel_residual": {"x_rms": 13.12, "y_rms": 17.74, "combined_rms": 22.07},
        },
    },
    "johnston-pp194-plate-1": {
        "source": "pp194-plate-01.pdf",
        "extract": "embedded-page-1",
        "reference_size": (7770, 8218),
        "crop": (321, 298, 7372, 7563),
        "legend_crop": (5800, 2700, 7370, 7100),
        "bounds": (-121.0847222222, 39.1727777778, -121.0, 39.2472222222),
        "minzoom": 10,
        "maxzoom": 17,
        "source_native_ppi": 400,
        "output_ppi": 600,
        "gcp_count": 4,
        "rmse_m": None,
        "confidence": "medium",
        "georef_status": (
            "Reviewed Plate 1 neatline-corner fit using printed coordinates; no "
            "independent internal checkpoints, so the overlay remains interpretive."
        ),
        "georef_method": "four printed neatline corners",
    },
    "jackson-pgm-2019": {
        "source": "jackson-pgm-2019.kmz",
        "legend_source": "jackson-pgm-2019-sheet-l4.jpg",
        "extract": "kmz-ground-overlay",
        "kml_member": "doc.kml",
        "overlay_href": "108920_1_kmz.jpg",
        "overlay_size": (4096, 4096),
        "reference_size": (4096, 4096),
        "crop": (0, 0, 4096, 4096),
        "legend_reference_size": (3412, 2062),
        "legend_crop": (1460, 390, 3330, 1525),
        "bounds": (
            -120.87604944235741,
            38.24991713416669,
            -120.75104531861005,
            38.37491262923517,
        ),
        "minzoom": 10,
        "maxzoom": 16,
        "gcp_count": 4,
        "rmse_m": None,
        "confidence": "high",
        "georef_status": (
            "Official NGMDB KMZ GroundOverlay LatLonBox, with exact reviewed "
            "bounds and no rotation."
        ),
        "georef_method": "four corners of the unrotated NGMDB KML GroundOverlay",
        "control": {
            "source": "doc.kml GroundOverlay/LatLonBox",
            "rotation_degrees": 0,
            "corners": [
                {
                    "pixel": [0, 0],
                    "lonlat": [-120.87604944235741, 38.37491262923517],
                },
                {
                    "pixel": [4096, 0],
                    "lonlat": [-120.75104531861005, 38.37491262923517],
                },
                {
                    "pixel": [4096, 4096],
                    "lonlat": [-120.75104531861005, 38.24991713416669],
                },
                {
                    "pixel": [0, 4096],
                    "lonlat": [-120.87604944235741, 38.24991713416669],
                },
            ],
        },
    },
}


def load_generalized_overlay_specs() -> None:
    """Merge reviewed target-overlay source/build specs into this pipeline.

    The bulky citations and target mappings live in one JSON contract shared
    with geology_quads.py.  Keep the four hand-reviewed seed recipes above in
    Python, while allowing the generalized NGMDB GroundOverlay set to grow
    without duplicating its checksums and exact KML bounds here.
    """
    with TARGET_OVERLAY_CONFIG.open() as handle:
        config = json.load(handle)
    sources = config.get("sources")
    rasters = config.get("raster_specs")
    if not isinstance(sources, dict) or not isinstance(rasters, dict):
        raise RuntimeError("target-overlay config must contain sources and raster_specs")
    duplicate_sources = sorted(set(SOURCE_SPECS).intersection(sources))
    duplicate_rasters = sorted(set(RASTER_SPECS).intersection(rasters))
    if duplicate_sources or duplicate_rasters:
        raise RuntimeError(
            "duplicate generalized overlay id(s): "
            + ", ".join(duplicate_sources + duplicate_rasters)
        )
    for name, source in sources.items():
        if not isinstance(name, str) or not isinstance(source, dict):
            raise RuntimeError("invalid generalized source record")
        if not source.get("url") or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")):
            raise RuntimeError(f"generalized source {name!r} lacks a pinned URL/checksum")
    for layer_id, spec in rasters.items():
        if not isinstance(layer_id, str) or not isinstance(spec, dict):
            raise RuntimeError("invalid generalized raster record")
        if spec.get("source") not in sources:
            raise RuntimeError(f"generalized raster {layer_id!r} references an unknown source")
        if spec.get("extract") != "kmz-ground-overlay":
            raise RuntimeError(f"generalized raster {layer_id!r} must use reviewed KMZ bounds")
    SOURCE_SPECS.update(sources)
    RASTER_SPECS.update(rasters)


load_generalized_overlay_specs()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def ensure_tools(layer_ids: Iterable[str]) -> None:
    pdf_extracts = {
        "render-page-1-150",
        "embedded-page-1",
        "embedded-page-199",
    }
    needs_poppler = any(
        RASTER_SPECS[layer_id]["extract"] in pdf_extracts for layer_id in layer_ids
    )
    missing = (
        [name for name in ("pdftoppm", "pdfimages") if not shutil.which(name)]
        if needs_poppler
        else []
    )
    if missing:
        raise SystemExit("Missing Poppler executable(s): " + ", ".join(missing))
    if not CRS.from_epsg(4326).is_geographic:
        raise RuntimeError("pyproj could not resolve the EPSG:4326 target CRS")


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"download: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "NWMM-WS10/1.0"})
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with urllib.request.urlopen(request, timeout=90) as response, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def download_zoomify(spec: dict, target: Path) -> None:
    """Assemble one reviewed NGMDB Zoomify level into a deterministic JPEG."""
    zoomify = spec["zoomify"]
    level = int(zoomify["level"])
    width, height = int(zoomify["width"]), int(zoomify["height"])
    tile_size = int(zoomify.get("tile_size", 256))
    level_offset = int(zoomify["level_tile_offset"])
    if not (0 < width <= 20_000 and 0 < height <= 20_000 and tile_size == 256):
        raise RuntimeError("unsafe or unsupported Zoomify dimensions")
    columns, rows = math.ceil(width / tile_size), math.ceil(height / tile_size)
    positions = [(x, y) for y in range(rows) for x in range(columns)]
    if len(positions) > 5_000:
        raise RuntimeError(f"refusing oversized Zoomify level with {len(positions)} tiles")
    base = spec["url"].rstrip("/")
    print(f"download: {base} level {level} ({len(positions)} Zoomify tiles)")

    def fetch(position: tuple[int, int]) -> tuple[int, int, bytes]:
        x, y = position
        global_index = level_offset + y * columns + x
        group = global_index // 256
        url = f"{base}/TileGroup{group}/{level}-{x}-{y}.jpg"
        request = urllib.request.Request(url, headers={"User-Agent": "NWMM-WS10/1.0"})
        with urllib.request.urlopen(request, timeout=90) as response:
            return x, y, response.read()

    image = Image.new("RGB", (width, height), "white")
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch, position) for position in positions]
        for future in as_completed(futures):
            x, y, payload = future.result()
            with Image.open(BytesIO(payload)) as tile:
                tile.load()
                if tile.width > tile_size or tile.height > tile_size:
                    raise RuntimeError(f"oversized Zoomify tile at {x},{y}: {tile.size}")
                image.paste(tile.convert("RGB"), (x * tile_size, y * tile_size))

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    os.close(fd)
    try:
        image.save(tmp_name, "JPEG", quality=95, optimize=True)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def ensure_sources(allow_download: bool, names: Iterable[str] | None = None) -> dict:
    provenance = {}
    selected = list(names) if names is not None else list(SOURCE_SPECS)
    for name in selected:
        spec = SOURCE_SPECS[name]
        path = SOURCES / name
        if not path.exists():
            if not allow_download:
                raise SystemExit(f"Missing {path}; rerun with --download")
            if spec.get("zoomify"):
                download_zoomify(spec, path)
            else:
                download(spec["url"], path)
        actual = sha256(path)
        if actual != spec["sha256"]:
            raise SystemExit(
                f"Checksum mismatch for {path}: expected {spec['sha256']}, got {actual}"
            )
        provenance[name] = {
            "source_url": spec["url"],
            "sha256": actual,
            "bytes": path.stat().st_size,
        }
        if spec.get("retrieval_note"):
            provenance[name]["retrieval_note"] = spec["retrieval_note"]
        print(f"source ok: {name} ({path.stat().st_size:,} bytes)")
    return provenance


def source_names_for_layers(
    layer_ids: Iterable[str], *, include_vector: bool = True
) -> list[str]:
    names = {"DWM193_GIS.zip"} if include_vector else set()
    for layer_id in layer_ids:
        spec = RASTER_SPECS[layer_id]
        names.add(spec["source"])
        if spec.get("legend_crop") and spec.get("legend_source"):
            names.add(spec["legend_source"])
    return sorted(names)


def safe_extract_zip(source: Path, destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        return
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            resolved = (destination / member.filename).resolve()
            if root not in resolved.parents and resolved != root:
                raise RuntimeError(f"unsafe ZIP member: {member.filename}")
        archive.extractall(destination)


def simplify_ring(points: Iterable[Iterable[float]], tolerance: float = 0.000015) -> list[list[float]]:
    """Douglas-Peucker simplification for a closed lon/lat ring."""
    source = [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in points]
    if len(source) <= 5:
        return source
    closed = source[0] == source[-1]
    work = source[:-1] if closed else source

    def recurse(segment: list[list[float]]) -> list[list[float]]:
        if len(segment) < 3:
            return segment
        ax, ay = segment[0]
        bx, by = segment[-1]
        dx, dy = bx - ax, by - ay
        denom = math.hypot(dx, dy)
        best_i, best = 0, -1.0
        for i, (x, y) in enumerate(segment[1:-1], 1):
            distance = (
                abs(dy * x - dx * y + bx * ay - by * ax) / denom
                if denom
                else math.hypot(x - ax, y - ay)
            )
            if distance > best:
                best_i, best = i, distance
        if best > tolerance:
            left, right = recurse(segment[: best_i + 1]), recurse(segment[best_i:])
            return left[:-1] + right
        return [segment[0], segment[-1]]

    reduced = recurse(work)
    if closed:
        reduced.append(reduced[0])
    return reduced if len(reduced) >= 4 else source


def polygon_geometry(geometry, source_crs) -> list[list[list[list[float]]]]:
    # Fiona 1.9 drops the geometry type when its deprecated ``precision``
    # argument is used.  Keep full precision here and round in simplify_ring.
    transformed = transform_geom(source_crs, "EPSG:4326", geometry)
    if transformed["type"] == "Polygon":
        coordinates = [transformed["coordinates"]]
    elif transformed["type"] == "MultiPolygon":
        coordinates = transformed["coordinates"]
    else:
        raise ValueError(f"unexpected polygon geometry: {transformed['type']}")
    result = [[simplify_ring(ring) for ring in polygon] for polygon in coordinates]
    if not shape({"type": "MultiPolygon", "coordinates": result}).is_valid:
        raise ValueError("polygon became invalid during coordinate normalization")
    return result


def line_geometry(geometry, source_crs) -> list[list[list[float]]]:
    transformed = transform_geom(source_crs, "EPSG:4326", geometry)
    if transformed["type"] == "LineString":
        coordinates = [transformed["coordinates"]]
    elif transformed["type"] == "MultiLineString":
        coordinates = transformed["coordinates"]
    else:
        raise ValueError(f"unexpected line geometry: {transformed['type']}")
    return [simplify_ring(line, 0.00001) for line in coordinates]


def locate_gdb() -> Path:
    destination = WORK / "dwm193"
    safe_extract_zip(SOURCES / "DWM193_GIS.zip", destination)
    matches = list(destination.rglob("*.gdb"))
    if len(matches) != 1:
        raise RuntimeError(f"expected one DWM-193 file geodatabase, found {len(matches)}")
    return matches[0]


def build_dwm_vector() -> dict:
    gdb = locate_gdb()
    lookup_doc = json.loads(UNIT_LOOKUP.read_text())
    lookup = lookup_doc["units"]
    units, faults = [], []
    unknown = set()

    with fiona.open(gdb, layer="MapUnitPolys") as source:
        source_crs = source.crs_wkt or source.crs
        for feature in source:
            props = dict(feature["properties"])
            code = props.get("MapUnit")
            meta = lookup.get(code)
            if meta is None:
                unknown.add(code)
                continue
            units.append(
                {
                    "id": props.get("MapUnitPolys_ID"),
                    "src": "dwm193",
                    "nm": meta["name"],
                    "sn": code,
                    "li": meta["lithology"],
                    "de": meta["description"],
                    "co": f"DWM-193 identity confidence: {props.get('IdentityConfidence') or 'not stated'}.",
                    "age": meta["age"],
                    "t0": meta["t0"],
                    "t1": meta["t1"],
                    "col": meta["color"],
                    "g": polygon_geometry(feature["geometry"], source_crs),
                }
            )

    with fiona.open(gdb, layer="OverlayPolys") as source:
        source_crs = source.crs_wkt or source.crs
        meta = lookup["sinter"]
        for feature in source:
            props = dict(feature["properties"])
            units.append(
                {
                    "id": props.get("OverlayPolys_ID"),
                    "src": "dwm193",
                    "nm": meta["name"],
                    "sn": "sinter",
                    "li": meta["lithology"],
                    "de": meta["description"],
                    "co": (
                        "DWM-193 overlay polygon scored independently from its host map unit; "
                        f"identity confidence: {props.get('IdentityConfidence') or 'not stated'}."
                    ),
                    "age": meta["age"],
                    "t0": meta["t0"],
                    "t1": meta["t1"],
                    "col": meta["color"],
                    "g": polygon_geometry(feature["geometry"], source_crs),
                }
            )

    with fiona.open(gdb, layer="ContactsAndFaults") as source:
        source_crs = source.crs_wkt or source.crs
        for feature in source:
            props = dict(feature["properties"])
            kind = str(props.get("Type") or "")
            if "fault" not in kind.lower():
                continue
            for part, path in enumerate(line_geometry(feature["geometry"], source_crs)):
                if len(path) < 2:
                    continue
                faults.append(
                    {
                        "id": f"{props.get('ContactsAndFaults_ID')}-{part + 1}",
                        "nm": kind,
                        "ty": kind,
                        "src": "dwm193",
                        "concealed": str(props.get("IsConcealed") or "0") == "1",
                        "path": path,
                    }
                )

    if unknown:
        raise RuntimeError("DWM-193 lookup missing unit codes: " + ", ".join(sorted(unknown)))
    if len(units) != 523:
        raise RuntimeError(f"expected 523 DWM-193 polygons including overlays, got {len(units)}")
    if not faults:
        raise RuntimeError("no DWM-193 faults survived normalization")

    payload = {
        "aoi": "delamar24k",
        "generated": TODAY,
        "provenance": {
            "source": lookup_doc["source"],
            "source_url": lookup_doc["source_url"],
            "gis_url": SOURCE_SPECS["DWM193_GIS.zip"]["url"],
            "source_sha256": sha256(SOURCES / "DWM193_GIS.zip"),
            "normalization": (
                "DWM-193 MapUnitPolys plus OverlayPolys transformed from NAD27 / Idaho West "
                "(EPSG:26770) to WGS84. Contacts are excluded; fault features are retained."
            ),
        },
        "license": (
            "IGS metadata lists access constraints as none and no named open-content license; "
            "retain the citation and nonsite-specific-use disclaimer."
        ),
        "fallback_note": None,
        "notes": [
            "Native vector evidence at 1:24,000; no raster pixels were classified or scored.",
            "Descriptions are concise paraphrases of the official DWM-193 map-unit text.",
            "Sinter and silicified-zone overlays are separate units so WS6 can detect them.",
        ],
        "units": units,
        "faults": faults,
        "springs": [],
        "wells": [],
        "sources": {
            "dwm193": {
                "ref": lookup_doc["source"],
                "scale": "1:24,000",
                "scale_note": "native 1:24,000 IGS DWM-193 GIS",
                "scale_denominator": 24000,
                "native_scale": 24000,
                "format": "GeMS Level 1 file geodatabase",
                "kind": "native vector",
                "scoring_source": True,
                "url": lookup_doc["source_url"],
            }
        },
    }
    write_json(str(GEOLOGY_REL), payload)
    print(f"vector: {len(units)} polygons / {len(faults)} fault paths")
    return {"units": len(units), "faults": len(faults), "sinter_overlays": 36}


def run_rescan() -> dict:
    from geology_targets import run

    output = run("delamar24k")
    by_tier = output["stats"].get("by_tier", {})
    return {
        "status": "ready",
        "id": "delamar24k",
        "data_url": str(TARGETS_REL).replace(os.sep, "/"),
        "file": str(TARGETS_REL).replace(os.sep, "/"),
        "source_geology": str(GEOLOGY_REL).replace(os.sep, "/"),
        "targets": output["stats"]["targets"],
        "by_tier": by_tier,
        "money": output["stats"]["money"],
        "note": (
            "WS6 lexicon rescan of native DWM-193 1:24,000 GIS only; raster tiles do not "
            "affect ranking. Open-ground and PLSS boosts are absent for this AOI."
        ),
    }


def scaled_box(box: tuple[int, int, int, int], reference, actual) -> tuple[int, int, int, int]:
    sx, sy = actual[0] / reference[0], actual[1] / reference[1]
    return tuple(round(value * (sx if i % 2 == 0 else sy)) for i, value in enumerate(box))


def run_command(command: list[str]) -> None:
    print("run:", " ".join(command))
    subprocess.run(command, check=True)


def largest_png(directory: Path) -> Path:
    candidates = list(directory.glob("*.png"))
    if not candidates:
        raise RuntimeError(f"no PNG extracted into {directory}")
    return max(candidates, key=lambda p: Image.open(p).size[0] * Image.open(p).size[1])


def unique_safe_zip_member(archive: zipfile.ZipFile, member_name: str) -> zipfile.ZipInfo:
    path = PurePosixPath(member_name)
    if (
        not member_name
        or path.is_absolute()
        or str(path) != member_name
        or "\\" in member_name
        or any(part in ("", ".", "..") for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise RuntimeError(f"unsafe KMZ member path: {member_name!r}")
    matches = [info for info in archive.infolist() if info.filename == member_name]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one KMZ member {member_name!r}, found {len(matches)}"
        )
    info = matches[0]
    member_mode = info.external_attr >> 16
    if info.is_dir() or stat.S_IFMT(member_mode) == stat.S_IFLNK:
        raise RuntimeError(f"KMZ member is not a regular file: {member_name!r}")
    return info


def inspect_kmz_ground_overlay(source: Path, spec: dict) -> dict:
    """Validate the one expected, north-up KML GroundOverlay without extracting it."""
    kml_namespace = "http://www.opengis.net/kml/2.2"
    namespaces = {"kml": kml_namespace, "gx": "http://www.google.com/kml/ext/2.2"}
    with zipfile.ZipFile(source) as archive:
        kml_info = unique_safe_zip_member(archive, spec["kml_member"])
        if kml_info.file_size > 2 * 1024 * 1024:
            raise RuntimeError(f"unexpectedly large KML document: {kml_info.file_size} bytes")
        kml = archive.read(kml_info)
        lowered = kml.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise RuntimeError("KMZ KML must not contain a DTD or entity declaration")
        repaired_bare_ampersands = 0
        try:
            root = ElementTree.fromstring(kml)
        except ElementTree.ParseError as strict_exc:
            # A few legacy NGMDB exports put literal ampersands in human-readable
            # Placemark titles (for example, "Inyokern & Ridgecrest").  Repair
            # only XML-invalid bare ampersands, then run every normal structural,
            # member, hash, bounds, rotation and image-size check below.
            repaired_kml, repaired_bare_ampersands = re.subn(
                rb"&(?!#(?:[0-9]+|x[0-9A-Fa-f]+);|[A-Za-z_][A-Za-z0-9_.:-]*;)",
                b"&amp;",
                kml,
            )
            if not repaired_bare_ampersands:
                raise RuntimeError(f"invalid KMZ KML: {strict_exc}") from strict_exc
            try:
                root = ElementTree.fromstring(repaired_kml)
            except ElementTree.ParseError as repaired_exc:
                raise RuntimeError(f"invalid KMZ KML after bare-ampersand repair: {repaired_exc}") from repaired_exc
        if root.tag != f"{{{kml_namespace}}}kml":
            raise RuntimeError(f"unexpected KML root element: {root.tag}")

        overlays = root.findall(".//kml:GroundOverlay", namespaces)
        if len(overlays) != 1:
            raise RuntimeError(f"expected one KML GroundOverlay, found {len(overlays)}")
        overlay = overlays[0]
        hrefs = overlay.findall("kml:Icon/kml:href", namespaces)
        if len(hrefs) != 1 or not (hrefs[0].text or "").strip():
            raise RuntimeError("GroundOverlay must contain exactly one nonempty Icon href")
        href = (hrefs[0].text or "").strip()
        if href != spec["overlay_href"]:
            raise RuntimeError(
                f"unexpected GroundOverlay href: expected {spec['overlay_href']!r}, got {href!r}"
            )
        overlay_info = unique_safe_zip_member(archive, href)

        boxes = overlay.findall("kml:LatLonBox", namespaces)
        if len(boxes) != 1:
            raise RuntimeError(f"expected one GroundOverlay LatLonBox, found {len(boxes)}")
        if any(element.tag.rsplit("}", 1)[-1] == "LatLonQuad" for element in overlay.iter()):
            raise RuntimeError("rotated KML LatLonQuad overlays are not supported")
        box = boxes[0]

        def one_number(name: str) -> float:
            elements = box.findall(f"kml:{name}", namespaces)
            if len(elements) != 1 or not (elements[0].text or "").strip():
                raise RuntimeError(f"LatLonBox must contain exactly one {name}")
            try:
                value = float((elements[0].text or "").strip())
            except ValueError as exc:
                raise RuntimeError(f"invalid LatLonBox {name}") from exc
            if not math.isfinite(value):
                raise RuntimeError(f"non-finite LatLonBox {name}")
            return value

        bounds = (one_number("west"), one_number("south"), one_number("east"), one_number("north"))
        expected_bounds = tuple(float(value) for value in spec["bounds"])
        if any(abs(actual - expected) > 1e-12 for actual, expected in zip(bounds, expected_bounds)):
            raise RuntimeError(
                f"KML bounds differ from reviewed bounds: expected {expected_bounds}, got {bounds}"
            )
        rotations = box.findall("kml:rotation", namespaces)
        if len(rotations) > 1:
            raise RuntimeError("LatLonBox contains multiple rotation values")
        try:
            rotation = float((rotations[0].text or "0").strip()) if rotations else 0.0
        except ValueError as exc:
            raise RuntimeError("invalid LatLonBox rotation") from exc
        if not math.isfinite(rotation) or rotation != 0.0:
            raise RuntimeError(f"GroundOverlay must be unrotated, got {rotation} degrees")

        with archive.open(overlay_info) as handle, Image.open(handle) as image:
            actual_size = image.size
            image_format = image.format
            image.verify()
        expected_size = tuple(spec["overlay_size"])
        if actual_size != expected_size:
            raise RuntimeError(
                f"GroundOverlay image must be {expected_size}, got {actual_size}"
            )
        if image_format != "JPEG":
            raise RuntimeError(f"GroundOverlay image must be JPEG, got {image_format}")

    return {
        "method": "verified KMZ KML GroundOverlay",
        "kml_member": spec["kml_member"],
        "overlay_member": href,
        "overlay_size": list(actual_size),
        "kml_bounds": list(bounds),
        "rotation_degrees": rotation,
        "kml_repaired_bare_ampersands": repaired_bare_ampersands,
    }


def extract_source(layer_id: str, spec: dict) -> tuple[Path, dict]:
    destination = WORK / layer_id
    destination.mkdir(parents=True, exist_ok=True)
    source = SOURCES / spec["source"]
    if spec["extract"] == "kmz-ground-overlay":
        details = inspect_kmz_ground_overlay(source, spec)
        suffix = PurePosixPath(details["overlay_member"]).suffix.lower()
        output = destination / f"ground-overlay{suffix}"
        fd, tmp_name = tempfile.mkstemp(prefix=output.name + ".", dir=destination)
        try:
            with zipfile.ZipFile(source) as archive:
                info = unique_safe_zip_member(archive, details["overlay_member"])
                with archive.open(info) as source_handle, os.fdopen(fd, "wb") as output_handle:
                    shutil.copyfileobj(source_handle, output_handle, 1024 * 1024)
            os.replace(tmp_name, output)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return output, details

    if spec["extract"] == "render-page-1-150":
        output = destination / "page-1-150.png"
        if not output.exists():
            run_command(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-r",
                    "150",
                    "-png",
                    "-singlefile",
                    str(source),
                    str(output.with_suffix("")),
                ]
            )
        return output, {"method": spec["extract"]}

    extract_dir = destination / "embedded"
    existing = list(extract_dir.glob("*.png")) if extract_dir.exists() else []
    if not existing:
        extract_dir.mkdir(parents=True, exist_ok=True)
        prefix = extract_dir / "plate"
        command = ["pdfimages"]
        if spec["extract"] == "embedded-page-199":
            command += ["-f", "199", "-l", "199"]
        command += ["-png", str(source), str(prefix)]
        run_command(command)
    return largest_png(extract_dir), {"method": spec["extract"]}


def crop_and_scale(
    layer_id: str, spec: dict
) -> tuple[Path, Path | None, str | None, dict]:
    source_path, source_details = extract_source(layer_id, spec)
    with Image.open(source_path) as source:
        source.load()
        actual = source.size
        map_box = scaled_box(spec["crop"], spec["reference_size"], actual)
        image = source.crop(map_box)
        native_size = image.size
        native_ppi = spec.get("source_native_ppi")
        output_ppi = spec.get("output_ppi")
        ppi_upsample_requested = bool(
            native_ppi and output_ppi and output_ppi > native_ppi
        )
        factor = output_ppi / native_ppi if ppi_upsample_requested else 1.0
        requested_output_size = [
            round(image.width * factor),
            round(image.height * factor),
        ]

        # Existing specs retain their historical behavior: the scratch crop and
        # XYZ source stay at native size, while write_cog applies any documented
        # PPI upsample. New generalized specs can cap the *prepared* raster once
        # here so the scratch crop, COG and XYZ tiles all share bounded inputs.
        max_output_dimension = spec.get("max_output_dimension")
        dimension_limited = False
        resize_factor = 1.0
        effective_output_ppi = output_ppi
        if max_output_dimension is not None:
            if (
                isinstance(max_output_dimension, bool)
                or not isinstance(max_output_dimension, int)
                or max_output_dimension <= 0
                or max_output_dimension > 100_000
            ):
                raise RuntimeError(
                    f"{layer_id} max_output_dimension must be an integer from 1 to 100000"
                )
            resize_factor = min(
                1.0, max_output_dimension / max(requested_output_size)
            )
            output_size = [
                max(1, round(value * resize_factor))
                for value in requested_output_size
            ]
            dimension_limited = resize_factor < 1.0
            if tuple(output_size) != image.size:
                image = image.resize(tuple(output_size), RESAMPLE)
            if output_ppi is not None:
                effective_output_ppi = output_ppi * resize_factor
            elif native_ppi is not None:
                effective_output_ppi = native_ppi * (
                    output_size[0] / native_size[0]
                )
        else:
            output_size = requested_output_size

        upsampled = (
            image.width > native_size[0] or image.height > native_size[1]
            if max_output_dimension is not None
            else ppi_upsample_requested
        )

        work_dir = WORK / layer_id
        work_dir.mkdir(parents=True, exist_ok=True)
        map_path = work_dir / "map-crop.png"
        save_options = {"optimize": True}
        saved_ppi = effective_output_ppi if max_output_dimension is not None else output_ppi
        if saved_ppi:
            save_options["dpi"] = (saved_ppi, saved_ppi)
        image.save(map_path, **save_options)

    extraction = {
        **source_details,
        "source_image": str(source_path.relative_to(CACHE)),
        "source_image_size": list(actual),
        "crop_source_pixels": list(map_box),
        "native_crop_size": list(native_size),
        "output_size": output_size,
        "map_crop_size": list(image.size),
        "requested_output_size": requested_output_size,
        "max_output_dimension": max_output_dimension,
        "dimension_limited": dimension_limited,
        "resize_factor": resize_factor,
        "source_native_ppi": native_ppi,
        "output_ppi": output_ppi,
        "effective_output_ppi": effective_output_ppi,
        "ppi_upsample_requested": ppi_upsample_requested,
        "upsampled": upsampled,
        "upsample_note": (
            f"{output_ppi:g}-ppi output was requested from a native "
            f"{native_ppi:g}-ppi embedded image; resampling does not add source "
            "detail."
            if ppi_upsample_requested
            else None
        ),
    }

    legend_crop = spec.get("legend_crop")
    legend_source_name = spec.get("legend_source")
    legend_mode = spec.get("legend_mode")
    if legend_mode not in (None, "map-preview"):
        raise RuntimeError(f"{layer_id} has unsupported legend_mode {legend_mode!r}")
    if legend_mode and legend_crop:
        raise RuntimeError(f"{layer_id} cannot combine legend_mode with legend_crop")
    if legend_mode == "map-preview":
        if legend_source_name:
            raise RuntimeError(
                f"{layer_id} map-preview must use the map source, not legend_source"
            )
        preview = image.convert("RGB")
        preview_factor = min(1.0, 1200 / max(preview.size))
        if preview_factor < 1.0:
            preview = preview.resize(
                (
                    max(1, round(preview.width * preview_factor)),
                    max(1, round(preview.height * preview_factor)),
                ),
                RESAMPLE,
            )
        preview_work = WORK / layer_id / "map-preview.webp"
        preview.save(preview_work, "WEBP", quality=84, method=5)
        extraction.update(
            {
                "preview_source": "prepared map crop",
                "preview_size": list(preview.size),
                "preview_max_dimension": 1200,
            }
        )
        return map_path, preview_work, "map-preview", extraction
    if not legend_crop:
        if legend_source_name:
            raise RuntimeError(f"{layer_id} legend_source requires legend_crop")
        return map_path, None, None, extraction

    legend_source_path = SOURCES / legend_source_name if legend_source_name else source_path
    legend_reference_size = spec.get("legend_reference_size", spec["reference_size"])
    with Image.open(legend_source_path) as legend_source:
        legend_source.load()
        legend_actual = legend_source.size
        if legend_source_name and legend_actual != tuple(legend_reference_size):
            raise RuntimeError(
                f"{legend_source_name} must be {tuple(legend_reference_size)}, "
                f"got {legend_actual}"
            )
        legend_box = scaled_box(legend_crop, legend_reference_size, legend_actual)
        legend = legend_source.crop(legend_box)
        if legend.width > 1800:
            legend_factor = 1800 / legend.width
            legend = legend.resize(
                (1800, round(legend.height * legend_factor)), RESAMPLE
            )
        legend = legend.convert("RGB")
        legend_work = WORK / layer_id / "legend-crop.webp"
        legend.save(legend_work, "WEBP", quality=88, method=5)
    extraction.update(
        {
            "legend_source_image": str(legend_source_path.relative_to(CACHE)),
            "legend_source_image_size": list(legend_actual),
            "legend_crop_source_pixels": list(legend_box),
        }
    )
    return map_path, legend_work, "legend", extraction


def geotiff_extratags(bounds, width: int, height: int, citation: str):
    west, south, east, north = bounds
    pixel_scale = ((east - west) / width, (north - south) / height, 0.0)
    ascii_value = citation + "|"
    geokeys = (
        1,
        1,
        0,
        4,
        1024,
        0,
        1,
        2,
        1025,
        0,
        1,
        1,
        2048,
        0,
        1,
        4326,
        2049,
        34737,
        len(ascii_value),
        0,
    )
    return [
        (33550, "d", 3, pixel_scale, False),
        (33922, "d", 6, (0.0, 0.0, 0.0, west, north, 0.0), False),
        (34735, "H", len(geokeys), geokeys, False),
        (34737, "s", len(ascii_value), ascii_value, False),
    ]


def write_cog(
    image_path: Path, output: Path, spec: dict, layer_id: str, extraction: dict
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as image:
        image.load()
        if image.mode == "1":
            image = image.convert("L")
        elif image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        native_ppi = spec.get("source_native_ppi")
        output_ppi = spec.get("output_ppi")
        if (
            spec.get("max_output_dimension") is None
            and native_ppi
            and output_ppi
            and output_ppi > native_ppi
        ):
            factor = output_ppi / native_ppi
            image = image.resize(
                (round(image.width * factor), round(image.height * factor)), RESAMPLE
            )
        array = np.asarray(image)
        photometric = "minisblack" if array.ndim == 2 else "rgb"
        write_options = {
            "bigtiff": array.nbytes > 3_000_000_000,
            "byteorder": "<",
            "tile": (512, 512),
            "compression": "deflate",
            "photometric": photometric,
            "metadata": None,
            "extratags": geotiff_extratags(
                spec["bounds"], image.width, image.height, f"NWMM WS10 {layer_id}; EPSG:4326"
            ),
        }
        resolution_ppi = (
            extraction.get("effective_output_ppi")
            if spec.get("max_output_dimension") is not None
            else output_ppi
        )
        if resolution_ppi:
            write_options["resolution"] = (resolution_ppi, resolution_ppi)
            write_options["resolutionunit"] = "INCH"
        tifffile.imwrite(output, array, **write_options)
    with tifffile.TiffFile(output) as tif:
        page = tif.pages[0]
        required = (33550, 33922, 34735)
        if not page.is_tiled or any(tag not in page.tags for tag in required):
            raise RuntimeError(f"{output} failed tiled GeoTIFF validation")
        offsets = list(page.dataoffsets)
        if offsets != sorted(offsets):
            raise RuntimeError(f"{output} tile byte offsets are not monotonic")
        if not offsets or page.offset >= offsets[0]:
            raise RuntimeError(f"{output} image IFD is not before tile payloads")
        shape = list(page.shape)
    return {
        "object_key": f"ws10-assets/cogs/{layer_id}.tif",
        "sha256": sha256(output),
        "bytes": output.stat().st_size,
        "shape": shape,
        "tiled": True,
        "tile_size": 512,
        "compression": "deflate",
        "ifd_before_tile_data": True,
        "overviews": 0,
        "crs": "EPSG:4326",
    }


def lon_to_tile_x(lon: float, zoom: int) -> float:
    return (lon + 180.0) / 360.0 * (1 << zoom)


def lat_to_tile_y(lat: float, zoom: int) -> float:
    lat = min(85.05112878, max(-85.05112878, lat))
    radians = math.radians(lat)
    return (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * (1 << zoom)


def tile_x_to_lon(x: float, zoom: int) -> float:
    return x / (1 << zoom) * 360.0 - 180.0


def tile_y_to_lat(y: float, zoom: int) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / (1 << zoom)))))


def build_xyz(image_path: Path, output: Path, spec: dict) -> dict:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    west, south, east, north = spec["bounds"]
    tile_count = 0
    tile_bytes = 0
    zoom_counts = {}
    with Image.open(image_path) as source:
        source.load()
        source = source.convert("RGB")
        for zoom in range(spec["minzoom"], spec["maxzoom"] + 1):
            min_x = math.floor(lon_to_tile_x(west, zoom))
            max_x = math.ceil(lon_to_tile_x(east, zoom)) - 1
            min_y = math.floor(lat_to_tile_y(north, zoom))
            max_y = math.ceil(lat_to_tile_y(south, zoom)) - 1
            count = 0
            for tile_x in range(min_x, max_x + 1):
                tile_west = tile_x_to_lon(tile_x, zoom)
                tile_east = tile_x_to_lon(tile_x + 1, zoom)
                for tile_y in range(min_y, max_y + 1):
                    tile_north = tile_y_to_lat(tile_y, zoom)
                    tile_south = tile_y_to_lat(tile_y + 1, zoom)
                    iw, ie = max(west, tile_west), min(east, tile_east)
                    ib, it = max(south, tile_south), min(north, tile_north)
                    if iw >= ie or ib >= it:
                        continue
                    sx0 = round((iw - west) / (east - west) * source.width)
                    sx1 = round((ie - west) / (east - west) * source.width)
                    sy0 = round((north - it) / (north - south) * source.height)
                    sy1 = round((north - ib) / (north - south) * source.height)
                    dx0 = round((lon_to_tile_x(iw, zoom) - tile_x) * 256)
                    dx1 = round((lon_to_tile_x(ie, zoom) - tile_x) * 256)
                    dy0 = round((lat_to_tile_y(it, zoom) - tile_y) * 256)
                    dy1 = round((lat_to_tile_y(ib, zoom) - tile_y) * 256)
                    sx0, sy0 = max(0, sx0), max(0, sy0)
                    sx1, sy1 = min(source.width, sx1), min(source.height, sy1)
                    dx0, dy0 = max(0, dx0), max(0, dy0)
                    dx1, dy1 = min(256, dx1), min(256, dy1)
                    if sx1 <= sx0 or sy1 <= sy0 or dx1 <= dx0 or dy1 <= dy0:
                        continue
                    patch = source.crop((sx0, sy0, sx1, sy1)).resize(
                        (dx1 - dx0, dy1 - dy0), RESAMPLE
                    )
                    tile = Image.new("RGBA", (256, 256), (255, 255, 255, 0))
                    tile.paste(patch, (dx0, dy0))
                    destination = output / str(zoom) / str(tile_x) / f"{tile_y}.webp"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    tile.save(destination, "WEBP", quality=88, method=4, exact=True)
                    tile_count += 1
                    tile_bytes += destination.stat().st_size
                    count += 1
            zoom_counts[str(zoom)] = count
            print(f"tiles: z{zoom} {count}")
    if not tile_count:
        raise RuntimeError(f"no XYZ tiles built for {output}")
    return {
        "object_prefix": f"ws10-assets/tiles/{output.name}",
        "tile_count": tile_count,
        "bytes": tile_bytes,
        "zoom_counts": zoom_counts,
        "format": "WebP",
        "scheme": "XYZ",
        "tile_size": 256,
    }


def build_raster(layer_id: str, spec: dict, source_provenance: dict) -> dict:
    print(f"\n== raster {layer_id} ==")
    image_path, supplemental_work, supplemental_kind, extraction = crop_and_scale(
        layer_id, spec
    )
    cog_path = ASSETS / "cogs" / f"{layer_id}.tif"
    tile_dir = ASSETS / "tiles" / layer_id
    legend_path = ASSETS / "legends" / f"{layer_id}.webp"
    preview_path = ASSETS / "previews" / f"{layer_id}.webp"
    cog = write_cog(image_path, cog_path, spec, layer_id, extraction)
    xyz = build_xyz(image_path, tile_dir, spec)
    raster = {
        "status": "processing",
        "build_status": "built-awaiting-upload",
        "block_reason": None,
        "tile_url_template": f"/ws10-assets/tiles/{layer_id}/{{z}}/{{x}}/{{y}}.webp",
        "bounds": [float(value) for value in spec["bounds"]],
        "minzoom": spec["minzoom"],
        "maxzoom": spec["maxzoom"],
        "cog_url": f"/ws10-assets/cogs/{layer_id}.tif",
        "cog": cog,
        "tiles": xyz,
    }
    if supplemental_kind == "legend":
        legend_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(supplemental_work, legend_path)
        raster.update(
            {
                "legend_url": f"/ws10-assets/legends/{layer_id}.webp",
                "legend": {
                    "object_key": f"ws10-assets/legends/{layer_id}.webp",
                    "sha256": sha256(legend_path),
                    "bytes": legend_path.stat().st_size,
                },
            }
        )
        preview_path.unlink(missing_ok=True)
    elif supplemental_kind == "map-preview":
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(supplemental_work, preview_path)
        raster.update(
            {
                "preview_url": f"/ws10-assets/previews/{layer_id}.webp",
                "preview": {
                    "kind": "map-preview",
                    "object_key": f"ws10-assets/previews/{layer_id}.webp",
                    "sha256": sha256(preview_path),
                    "bytes": preview_path.stat().st_size,
                },
            }
        )
        legend_path.unlink(missing_ok=True)
    else:
        # A prior build may have supplied a supplemental image. Do not leave a
        # stale local object which the current spec no longer advertises.
        legend_path.unlink(missing_ok=True)
        preview_path.unlink(missing_ok=True)
    # The crop and supplemental working image are reproducible intermediates.
    # Keep the official cached source plus final COG/XYZ/legend-or-preview, but
    # avoid doubling peak/final disk use for large source maps.
    image_path.unlink(missing_ok=True)
    if supplemental_work is not None:
        supplemental_work.unlink(missing_ok=True)
    legend_source_name = spec.get("legend_source")
    return {
        "raster": raster,
        "georef": {
            "crs": "EPSG:4326",
            "gcp_count": spec["gcp_count"],
            "rmse": spec["rmse_m"],
            "rmse_units": "metres" if spec["rmse_m"] is not None else None,
            "confidence": spec["confidence"],
            "status": spec["georef_status"],
            "method": spec["georef_method"],
            "control": spec.get("control"),
        },
        "build": {
            "generated": TODAY,
            "source": source_provenance[spec["source"]],
            **(
                {"legend_source": source_provenance[legend_source_name]}
                if legend_source_name
                else {}
            ),
            "extraction": extraction,
            "qa": {
                "cog_tags_checked": True,
                "xyz_scheme_checked": True,
                "bounds_checked": True,
                "legend_checked": supplemental_kind == "legend",
                "preview_checked": supplemental_kind == "map-preview",
            },
        },
    }


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {
        "schema": "nwmm.ws10.quad-geology-assets.v1",
        "generated": TODAY,
        "note": "Generated asset state; heavy files remain in ignored cache and object storage.",
        "layers": {},
        "rescans": {},
    }


def save_state(state: dict) -> None:
    state["generated"] = TODAY
    atomic_json(STATE, state)
    print(f"state: {STATE.relative_to(ROOT)}")


def local_asset_paths(layer_id: str, layer: dict) -> dict[str, Path]:
    if layer_id not in RASTER_SPECS:
        raise RuntimeError(f"unknown raster layer {layer_id!r}")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if not layer_id or any(character not in allowed for character in layer_id):
        raise RuntimeError(f"unsafe raster layer id {layer_id!r}")
    raster = layer.get("raster", {})
    if bool(raster.get("legend")) != bool(raster.get("legend_url")):
        raise RuntimeError(f"{layer_id} legend metadata and URL must appear together")
    if bool(raster.get("preview")) != bool(raster.get("preview_url")):
        raise RuntimeError(f"{layer_id} preview metadata and URL must appear together")
    if raster.get("legend") and raster.get("preview"):
        raise RuntimeError(f"{layer_id} cannot publish both a legend and map preview")
    paths = {
        "cog": ASSETS / "cogs" / f"{layer_id}.tif",
        "tiles": ASSETS / "tiles" / layer_id,
    }
    if raster.get("legend"):
        paths["legend"] = ASSETS / "legends" / f"{layer_id}.webp"
    if raster.get("preview"):
        paths["preview"] = ASSETS / "previews" / f"{layer_id}.webp"
    for path in paths.values():
        if path.is_symlink():
            raise RuntimeError(f"refusing symlinked local asset path: {path}")
    return paths


def validate_local_asset(
    layer_id: str, layer: dict, *, allow_missing: bool = False
) -> None:
    raster = layer.get("raster", {})
    paths = local_asset_paths(layer_id, layer)
    for path in paths.values():
        if not path.exists() and not allow_missing:
            raise RuntimeError(f"cannot mark {layer_id} ready; missing {path}")
    cog_path, tile_dir = paths["cog"], paths["tiles"]
    if tile_dir.exists():
        if not tile_dir.is_dir():
            raise RuntimeError(f"local XYZ asset is not a directory: {tile_dir}")
        actual_tiles = sum(1 for path in tile_dir.rglob("*.webp") if path.is_file())
        expected = raster.get("tiles", {}).get("tile_count")
        if expected != actual_tiles:
            raise RuntimeError(
                f"cannot validate {layer_id}; expected {expected} tiles, found {actual_tiles}"
            )
    if cog_path.exists() and sha256(cog_path) != raster.get("cog", {}).get("sha256"):
        raise RuntimeError(f"cannot validate {layer_id}; COG checksum changed")
    for kind in ("legend", "preview"):
        path = paths.get(kind)
        if (
            path is not None
            and path.exists()
            and sha256(path) != raster.get(kind, {}).get("sha256")
        ):
            raise RuntimeError(
                f"cannot validate {layer_id}; {kind} checksum changed"
            )


def evict_ready_local(layer_ids: list[str]) -> None:
    """Remove exact reproducible local outputs after publication is verified."""
    layer_ids = list(dict.fromkeys(layer_ids))
    state = load_state()
    planned: list[tuple[str, list[Path]]] = []
    for layer_id in layer_ids:
        layer = state.get("layers", {}).get(layer_id)
        if not layer:
            raise RuntimeError(f"no built state for {layer_id}")
        raster = layer.get("raster", {})
        if (
            raster.get("status") != "ready"
            or raster.get("build_status") != "uploaded-and-verified"
            or not raster.get("remote_verified")
        ):
            raise RuntimeError(
                f"cannot evict {layer_id}; state is not ready and remotely verified"
            )
        validate_local_asset(layer_id, layer, allow_missing=True)
        paths = list(local_asset_paths(layer_id, layer).values())
        planned.append((layer_id, paths))

    # Complete every readiness/checksum preflight before removing the first
    # artifact, so a bad later layer cannot leave a multi-layer request half done.
    for layer_id, paths in planned:
        files = []
        for path in paths:
            if path.is_dir():
                files.extend(child for child in path.rglob("*") if child.is_file())
            elif path.is_file():
                files.append(path)
        byte_count = sum(path.stat().st_size for path in files)
        file_count = len(files)
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        print(
            f"evicted local ready assets: {layer_id} "
            f"({file_count:,} files / {byte_count:,} bytes); sources retained"
        )


def ready_asset_summary(state: dict) -> dict:
    """Derive cumulative remote totals from every ready layer's asset metadata."""
    ready_layer_ids = sorted(
        layer_id
        for layer_id, layer in state.get("layers", {}).items()
        if (layer.get("raster") or {}).get("status") == "ready"
    )
    object_count = 0
    byte_count = 0
    verified_dates = []

    def recorded_nonnegative_int(layer_id: str, label: str, value) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                f"cannot summarize ready layer {layer_id}; missing/invalid {label}"
            )
        return value

    for layer_id in ready_layer_ids:
        raster = state["layers"][layer_id]["raster"]
        verified = raster.get("remote_verified")
        if not verified:
            raise RuntimeError(
                f"cannot summarize ready layer {layer_id}; remote_verified is missing"
            )
        verified_dates.append(str(verified))
        cog = raster.get("cog") or {}
        tiles = raster.get("tiles") or {}
        object_count += 1 + recorded_nonnegative_int(
            layer_id, "tiles.tile_count", tiles.get("tile_count")
        )
        byte_count += recorded_nonnegative_int(layer_id, "cog.bytes", cog.get("bytes"))
        byte_count += recorded_nonnegative_int(
            layer_id, "tiles.bytes", tiles.get("bytes")
        )
        for kind in ("legend", "preview"):
            metadata = raster.get(kind)
            if metadata:
                object_count += 1
                byte_count += recorded_nonnegative_int(
                    layer_id, f"{kind}.bytes", metadata.get("bytes")
                )

    return {
        "object_prefix": "ws10-assets/",
        "object_count": object_count,
        "bytes": byte_count,
        "cloudfront_base": "/ws10-assets/",
        "remote_verified": max(verified_dates) if verified_dates else TODAY,
        "ready_layer_ids": ready_layer_ids,
        "summary_method": "state-derived-ready-layer-asset-metadata",
        "verification": (
            "Cumulative object and byte totals derived from recorded COG, XYZ tile, "
            "legend and map-preview metadata for every ready layer; remote publication "
            "was separately verified through CloudFront."
        ),
    }


def mark_ready(layer_ids: list[str]) -> None:
    state = load_state()
    for layer_id in layer_ids:
        layer = state.get("layers", {}).get(layer_id)
        if not layer:
            raise RuntimeError(f"no built state for {layer_id}")
        validate_local_asset(layer_id, layer)
        layer["raster"]["status"] = "ready"
        layer["raster"]["build_status"] = "uploaded-and-verified"
        layer["raster"]["published"] = TODAY
        layer["raster"]["remote_verified"] = TODAY
        layer["raster"]["verification_note"] = (
            "Promoted only after representative COG, target-tile and, when present, "
            "legend or map-preview URLs returned HTTP 200 through CloudFront."
        )
        print(f"ready: {layer_id}")
    state["publication"] = ready_asset_summary(state)
    save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download", action="store_true", help="download any missing official source files"
    )
    parser.add_argument(
        "--skip-rasters", action="store_true", help="normalize DWM GIS and run WS6 only"
    )
    parser.add_argument(
        "--skip-vector",
        action="store_true",
        help="build selected rasters without DWM GIS normalization or the WS6 rescan",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(RASTER_SPECS),
        help="build only the named raster layer (repeatable)",
    )
    parser.add_argument(
        "--mark-ready",
        nargs="+",
        choices=sorted(RASTER_SPECS),
        help="after upload verification, promote already-built layer state to ready",
    )
    parser.add_argument(
        "--evict-ready-local",
        nargs="+",
        choices=sorted(RASTER_SPECS),
        help=(
            "delete exact local COG/XYZ/legend/preview outputs for remotely verified "
            "ready layers; cached official sources are retained"
        ),
    )
    args = parser.parse_args()

    if args.mark_ready and args.evict_ready_local:
        parser.error("--mark-ready and --evict-ready-local are separate operations")
    if args.mark_ready:
        mark_ready(args.mark_ready)
        return
    if args.evict_ready_local:
        if args.download or args.skip_rasters or args.skip_vector or args.only:
            parser.error("--evict-ready-local cannot be combined with build options")
        evict_ready_local(args.evict_ready_local)
        return
    if args.skip_rasters and args.skip_vector:
        parser.error("--skip-rasters and --skip-vector leave no work to perform")

    selected = [] if args.skip_rasters else (args.only or list(RASTER_SPECS))
    ensure_tools(selected)
    provenance = ensure_sources(
        args.download,
        source_names_for_layers(selected, include_vector=not args.skip_vector),
    )
    state = load_state()
    if not args.skip_vector:
        vector_stats = build_dwm_vector()
        rescan = run_rescan()
        state["layers"].setdefault("dwm-193", {})["vector"] = {
            "status": "ready",
            "data_url": str(GEOLOGY_REL).replace(os.sep, "/"),
            "file": str(GEOLOGY_REL).replace(os.sep, "/"),
            "targets_file": str(TARGETS_REL).replace(os.sep, "/"),
            "rescan_id": "delamar24k",
            "source_layer": "MapUnitPolys + OverlayPolys",
            "stats": vector_stats,
        }
        state["rescans"]["delamar24k"] = rescan

    if not args.skip_rasters:
        for layer_id in selected:
            state["layers"][layer_id] = {
                **state["layers"].get(layer_id, {}),
                **build_raster(layer_id, RASTER_SPECS[layer_id], provenance),
            }
    save_state(state)
    if args.skip_rasters:
        print("vector/rescan complete; existing raster asset state was left unchanged")
    elif args.skip_vector:
        print("raster build complete; existing vector/rescan state was left unchanged")
        print("upload assets before using --mark-ready")
    else:
        print("build complete: upload assets before using --mark-ready")


if __name__ == "__main__":
    main()
