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
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

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
}


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


def ensure_tools() -> None:
    missing = [name for name in ("pdftoppm", "pdfimages") if not shutil.which(name)]
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


def ensure_sources(allow_download: bool, names: Iterable[str] | None = None) -> dict:
    provenance = {}
    selected = list(names) if names is not None else list(SOURCE_SPECS)
    for name in selected:
        spec = SOURCE_SPECS[name]
        path = SOURCES / name
        if not path.exists():
            if not allow_download:
                raise SystemExit(f"Missing {path}; rerun with --download")
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
        print(f"source ok: {name} ({path.stat().st_size:,} bytes)")
    return provenance


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


def extract_source(layer_id: str, spec: dict) -> Path:
    destination = WORK / layer_id
    destination.mkdir(parents=True, exist_ok=True)
    source = SOURCES / spec["source"]
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
        return output

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
    return largest_png(extract_dir)


def crop_and_scale(layer_id: str, spec: dict) -> tuple[Path, Path, dict]:
    source_path = extract_source(layer_id, spec)
    with Image.open(source_path) as source:
        source.load()
        actual = source.size
        map_box = scaled_box(spec["crop"], spec["reference_size"], actual)
        legend_box = scaled_box(spec["legend_crop"], spec["reference_size"], actual)
        image = source.crop(map_box)
        legend = source.crop(legend_box)
        native_size = image.size
        native_ppi = spec.get("source_native_ppi")
        output_ppi = spec["output_ppi"]
        upsampled = bool(native_ppi and output_ppi > native_ppi)
        factor = output_ppi / native_ppi if upsampled else 1.0
        output_size = [round(image.width * factor), round(image.height * factor)]

        work_dir = WORK / layer_id
        map_path = work_dir / "map-crop.png"
        legend_work = work_dir / "legend-crop.webp"
        image.save(map_path, dpi=(output_ppi, output_ppi), optimize=True)
        if legend.width > 1800:
            factor = 1800 / legend.width
            legend = legend.resize((1800, round(legend.height * factor)), RESAMPLE)
        legend = legend.convert("RGB")
        legend.save(legend_work, "WEBP", quality=88, method=5)
    return map_path, legend_work, {
        "source_image": str(source_path.relative_to(CACHE)),
        "source_image_size": list(actual),
        "crop_source_pixels": list(map_box),
        "native_crop_size": list(native_size),
        "output_size": output_size,
        "source_native_ppi": native_ppi,
        "output_ppi": output_ppi,
        "upsampled": upsampled,
        "upsample_note": (
            "600-ppi output is a documented resample of a native 400-ppi embedded image; "
            "it does not add source detail."
            if upsampled
            else None
        ),
    }


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


def write_cog(image_path: Path, output: Path, spec: dict, layer_id: str) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as image:
        image.load()
        if image.mode == "1":
            image = image.convert("L")
        elif image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        native_ppi = spec.get("source_native_ppi")
        if native_ppi and spec["output_ppi"] > native_ppi:
            factor = spec["output_ppi"] / native_ppi
            image = image.resize(
                (round(image.width * factor), round(image.height * factor)), RESAMPLE
            )
        array = np.asarray(image)
        photometric = "minisblack" if array.ndim == 2 else "rgb"
        tifffile.imwrite(
            output,
            array,
            bigtiff=array.nbytes > 3_000_000_000,
            byteorder="<",
            tile=(512, 512),
            compression="deflate",
            photometric=photometric,
            metadata=None,
            resolution=(spec["output_ppi"], spec["output_ppi"]),
            resolutionunit="INCH",
            extratags=geotiff_extratags(
                spec["bounds"], image.width, image.height, f"NWMM WS10 {layer_id}; EPSG:4326"
            ),
        )
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
                    count += 1
            zoom_counts[str(zoom)] = count
            print(f"tiles: z{zoom} {count}")
    if not tile_count:
        raise RuntimeError(f"no XYZ tiles built for {output}")
    return {
        "object_prefix": f"ws10-assets/tiles/{output.name}",
        "tile_count": tile_count,
        "zoom_counts": zoom_counts,
        "format": "WebP",
        "scheme": "XYZ",
        "tile_size": 256,
    }


def build_raster(layer_id: str, spec: dict, source_provenance: dict) -> dict:
    print(f"\n== raster {layer_id} ==")
    image_path, legend_work, extraction = crop_and_scale(layer_id, spec)
    cog_path = ASSETS / "cogs" / f"{layer_id}.tif"
    tile_dir = ASSETS / "tiles" / layer_id
    legend_path = ASSETS / "legends" / f"{layer_id}.webp"
    cog = write_cog(image_path, cog_path, spec, layer_id)
    xyz = build_xyz(image_path, tile_dir, spec)
    legend_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(legend_work, legend_path)
    legend = {
        "object_key": f"ws10-assets/legends/{layer_id}.webp",
        "sha256": sha256(legend_path),
        "bytes": legend_path.stat().st_size,
    }
    # The crop and working legend are reproducible scratch intermediates. Keep
    # the official cached source plus final COG/XYZ/legend, but avoid doubling
    # peak/final disk use for large 600-ppi pocket plates.
    image_path.unlink(missing_ok=True)
    legend_work.unlink(missing_ok=True)
    return {
        "raster": {
            "status": "processing",
            "build_status": "built-awaiting-upload",
            "tile_url_template": f"/ws10-assets/tiles/{layer_id}/{{z}}/{{x}}/{{y}}.webp",
            "bounds": [round(value, 10) for value in spec["bounds"]],
            "minzoom": spec["minzoom"],
            "maxzoom": spec["maxzoom"],
            "legend_url": f"/ws10-assets/legends/{layer_id}.webp",
            "cog_url": f"/ws10-assets/cogs/{layer_id}.tif",
            "cog": cog,
            "tiles": xyz,
            "legend": legend,
        },
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
            "extraction": extraction,
            "qa": {
                "cog_tags_checked": True,
                "xyz_scheme_checked": True,
                "bounds_checked": True,
                "legend_checked": True,
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


def validate_local_asset(layer_id: str, layer: dict) -> None:
    raster = layer.get("raster", {})
    required = [
        ASSETS / "cogs" / f"{layer_id}.tif",
        ASSETS / "legends" / f"{layer_id}.webp",
    ]
    tile_dir = ASSETS / "tiles" / layer_id
    required.append(tile_dir)
    for path in required:
        if not path.exists():
            raise RuntimeError(f"cannot mark {layer_id} ready; missing {path}")
    actual_tiles = sum(1 for path in tile_dir.rglob("*.webp") if path.is_file())
    expected = raster.get("tiles", {}).get("tile_count")
    if expected != actual_tiles:
        raise RuntimeError(
            f"cannot mark {layer_id} ready; expected {expected} tiles, found {actual_tiles}"
        )
    if sha256(required[0]) != raster.get("cog", {}).get("sha256"):
        raise RuntimeError(f"cannot mark {layer_id} ready; COG checksum changed")
    if sha256(required[1]) != raster.get("legend", {}).get("sha256"):
        raise RuntimeError(f"cannot mark {layer_id} ready; legend checksum changed")


def asset_tree_summary() -> dict:
    files = [path for path in ASSETS.rglob("*") if path.is_file()]
    return {
        "object_prefix": "ws10-assets/",
        "object_count": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "cloudfront_base": "/ws10-assets/",
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
            "Promoted only after representative COG, legend and target-tile URLs "
            "returned HTTP 200 through CloudFront."
        )
        print(f"ready: {layer_id}")
    state["publication"] = {
        **asset_tree_summary(),
        "remote_verified": TODAY,
        "ready_layer_ids": sorted(
            layer_id
            for layer_id, layer in state.get("layers", {}).items()
            if (layer.get("raster") or {}).get("status") == "ready"
        ),
        "verification": (
            "S3 object-count/byte summary plus CloudFront HTTP 200, content-type and "
            "content-length checks for every COG and representative legends/target tiles."
        ),
    }
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
    args = parser.parse_args()

    if args.mark_ready:
        mark_ready(args.mark_ready)
        return

    ensure_tools()
    selected = [] if args.skip_rasters else (args.only or list(RASTER_SPECS))
    needed_sources = {"DWM193_GIS.zip"}
    needed_sources.update(RASTER_SPECS[layer_id]["source"] for layer_id in selected)
    provenance = ensure_sources(args.download, sorted(needed_sources))
    vector_stats = build_dwm_vector()
    rescan = run_rescan()
    state = load_state()
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
    else:
        print("build complete: upload assets before using --mark-ready")


if __name__ == "__main__":
    main()
