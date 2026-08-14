#!/usr/bin/env python3
"""Build and query the WS12 spatial evidence store.

The public map is deliberately PMTiles-first, but PMTiles are a presentation
format rather than an analyst database.  This module keeps a second,
dependency-free SQLite representation of the *pre-tile* vector records.  It
uses SQLite RTree for candidate selection and small, audited WGS84 geometry
routines for exact point/near queries.  The resulting database is a generated
deployment artifact; it is not committed to git.

Optional raster sampling uses rasterio when it is installed.  A build without
rasterio still records COG identities and survey provenance and fails closed
for numeric samples instead of interpreting display colours as nanoteslas.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
EARTH_RADIUS_M = 6_371_008.8
KINDS = frozenset({
    "geology", "claim", "mine", "fault", "land_status",
    "magnetic_sample", "survey", "other",
})
WS6_SOURCE_URLS = {
    "133": "https://doi.org/10.3133/ds1052",
    "281": "https://www.idahogeology.org/product/DWM-49",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def _object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def validate_lat_lon(lat: Any, lon: Any) -> tuple[float, float]:
    latitude, longitude = _finite(lat), _finite(lon)
    if latitude is None or longitude is None:
        raise ValueError("lat and lon must be finite numbers")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("lat/lon are outside EPSG:4326 bounds")
    return latitude, longitude


def parse_scale_denominator(value: Any) -> int | None:
    """Return an exact representative-fraction denominator, if stated.

    Ranges intentionally return ``None``: silently treating ``1:24k-1:250k``
    as a 1:24,000 map would violate the finest-scale evidence contract.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        result = int(value)
        return result if result > 0 else None
    text = str(value or "")
    matches = re.findall(r"1\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM]?)", text)
    values: list[int] = []
    for raw, suffix in matches:
        number = float(raw.replace(",", ""))
        if suffix.casefold() == "k":
            number *= 1_000
        elif suffix.casefold() == "m":
            number *= 1_000_000
        if number > 0:
            values.append(int(round(number)))
    return values[0] if len(set(values)) == 1 else None


def _positions(value: Any) -> Iterator[tuple[float, float]]:
    if (isinstance(value, (list, tuple)) and len(value) >= 2
            and _finite(value[0]) is not None and _finite(value[1]) is not None):
        yield float(value[0]), float(value[1])
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _positions(child)


def geometry_bounds(geometry: Mapping[str, Any] | None) -> tuple[float, float, float, float]:
    if not geometry or not isinstance(geometry.get("coordinates"), (list, tuple)):
        raise ValueError("feature has no supported coordinates")
    positions = list(_positions(geometry["coordinates"]))
    if not positions:
        raise ValueError("feature geometry has no finite positions")
    xs, ys = zip(*positions)
    if min(xs) < -180 or max(xs) > 180 or min(ys) < -90 or max(ys) > 90:
        raise ValueError("spatial store requires EPSG:4326 geometry")
    return min(xs), min(ys), max(xs), max(ys)


def _point_on_segment(lon: float, lat: float, a: Sequence[float],
                      b: Sequence[float], tolerance: float = 1e-11) -> bool:
    ax, ay, bx, by = float(a[0]), float(a[1]), float(b[0]), float(b[1])
    cross = (lon - ax) * (by - ay) - (lat - ay) * (bx - ax)
    if abs(cross) > tolerance * max(1.0, abs(bx - ax), abs(by - ay)):
        return False
    dot = (lon - ax) * (lon - bx) + (lat - ay) * (lat - by)
    return dot <= tolerance


def point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    previous = ring[-1]
    for current in ring:
        if _point_on_segment(lon, lat, previous, current):
            return True
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        if ((y1 > lat) != (y2 > lat)):
            crossing_x = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing_x:
                inside = not inside
        previous = current
    return inside


def point_in_polygon(lon: float, lat: float,
                     rings: Sequence[Sequence[Sequence[float]]]) -> bool:
    return bool(rings and point_in_ring(lon, lat, rings[0])
                and not any(point_in_ring(lon, lat, hole) for hole in rings[1:]))


def geometry_covers_point(geometry: Mapping[str, Any], lon: float, lat: float) -> bool:
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Point":
        return bool(coordinates and abs(float(coordinates[0]) - lon) <= 1e-11
                    and abs(float(coordinates[1]) - lat) <= 1e-11)
    if kind == "MultiPoint":
        return any(abs(float(p[0]) - lon) <= 1e-11
                   and abs(float(p[1]) - lat) <= 1e-11 for p in coordinates or [])
    if kind == "Polygon":
        return point_in_polygon(lon, lat, coordinates or [])
    if kind == "MultiPolygon":
        return any(point_in_polygon(lon, lat, polygon) for polygon in coordinates or [])
    if kind == "LineString":
        return any(_point_on_segment(lon, lat, a, b)
                   for a, b in zip(coordinates or [], (coordinates or [])[1:]))
    if kind == "MultiLineString":
        return any(geometry_covers_point({"type": "LineString", "coordinates": line},
                                         lon, lat) for line in coordinates or [])
    return False


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    value = (math.sin(dp / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def _segment_distance_m(lat: float, lon: float, a: Sequence[float],
                        b: Sequence[float]) -> float:
    # Local equirectangular projection is stable for the short segments in
    # geologic/claim vectors; endpoint haversines retain global correctness.
    mean_lat = math.radians((lat + float(a[1]) + float(b[1])) / 3)
    scale_x = math.cos(mean_lat) * math.pi * EARTH_RADIUS_M / 180
    scale_y = math.pi * EARTH_RADIUS_M / 180
    ax, ay = (float(a[0]) - lon) * scale_x, (float(a[1]) - lat) * scale_y
    bx, by = (float(b[0]) - lon) * scale_x, (float(b[1]) - lat) * scale_y
    dx, dy = bx - ax, by - ay
    denominator = dx * dx + dy * dy
    if denominator == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / denominator))
    return math.hypot(ax + t * dx, ay + t * dy)


def _line_distance_m(lat: float, lon: float,
                     line: Sequence[Sequence[float]]) -> float:
    if not line:
        return math.inf
    if len(line) == 1:
        return haversine_m(lat, lon, float(line[0][1]), float(line[0][0]))
    return min(_segment_distance_m(lat, lon, a, b)
               for a, b in zip(line, line[1:]))


def geometry_distance_m(geometry: Mapping[str, Any], lat: float, lon: float) -> float:
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Point":
        return haversine_m(lat, lon, float(coordinates[1]), float(coordinates[0]))
    if kind == "MultiPoint":
        return min((haversine_m(lat, lon, float(p[1]), float(p[0]))
                    for p in coordinates or []), default=math.inf)
    if kind == "LineString":
        return _line_distance_m(lat, lon, coordinates or [])
    if kind == "MultiLineString":
        return min((_line_distance_m(lat, lon, line) for line in coordinates or []),
                   default=math.inf)
    if kind == "Polygon":
        if point_in_polygon(lon, lat, coordinates or []):
            return 0.0
        return min((_line_distance_m(lat, lon, ring) for ring in coordinates or []),
                   default=math.inf)
    if kind == "MultiPolygon":
        return min((geometry_distance_m({"type": "Polygon", "coordinates": polygon},
                                        lat, lon) for polygon in coordinates or []),
                   default=math.inf)
    return math.inf


def _radius_bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = radius_m / 111_320.0
    dlon = radius_m / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


class SpatialStore:
    """SQLite/RTree spatial store with stable, JSON-serializable tool results."""

    def __init__(self, path: str | os.PathLike[str], *, read_only: bool = False):
        self.path = os.fspath(path)
        self.read_only = read_only
        if read_only:
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        if not self.read_only:
            # A deploy copies one database object, not SQLite's process-local
            # WAL sidecars. Checkpoint and leave a self-contained DELETE-mode
            # file before a builder reports success or packages the artifact.
            self.connection.commit()
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.close()

    def __enter__(self) -> "SpatialStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS layers (
                layer_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                citation TEXT,
                source_url TEXT,
                scale TEXT,
                scale_denominator INTEGER,
                state TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS features (
                feature_pk INTEGER PRIMARY KEY,
                layer_id TEXT NOT NULL REFERENCES layers(layer_id) ON DELETE CASCADE,
                external_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT,
                state TEXT,
                lon REAL,
                lat REAL,
                geometry_json TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                UNIQUE(layer_id, external_id)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS feature_index USING rtree(
                feature_pk, min_lon, max_lon, min_lat, max_lat
            );
            CREATE INDEX IF NOT EXISTS features_kind ON features(kind);
            CREATE INDEX IF NOT EXISTS features_external_id ON features(external_id);
            CREATE TABLE IF NOT EXISTS rasters (
                raster_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT,
                url TEXT,
                band INTEGER NOT NULL DEFAULT 1,
                unit TEXT,
                source_url TEXT,
                citation TEXT,
                state TEXT,
                min_lon REAL NOT NULL,
                max_lon REAL NOT NULL,
                min_lat REAL NOT NULL,
                max_lat REAL NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                portal TEXT,
                portal_id TEXT,
                state TEXT,
                source_url TEXT NOT NULL,
                page_count INTEGER,
                indexed_pages INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS document_aliases (
                alias TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                PRIMARY KEY(alias, document_id)
            );
            CREATE INDEX IF NOT EXISTS document_alias_lookup ON document_aliases(alias);
        """)
        self.connection.execute(
            "INSERT OR REPLACE INTO store_metadata(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),))
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.connection:
            yield

    def register_layer(self, layer_id: str, kind: str, title: str, *,
                       citation: str | None = None, source_url: str | None = None,
                       scale: str | None = None, scale_denominator: int | None = None,
                       state: str | None = None,
                       metadata: Mapping[str, Any] | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"unsupported spatial kind: {kind}")
        denominator = scale_denominator or parse_scale_denominator(scale)
        self.connection.execute("""
            INSERT INTO layers(layer_id,kind,title,citation,source_url,scale,
                               scale_denominator,state,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(layer_id) DO UPDATE SET
              kind=excluded.kind,title=excluded.title,citation=excluded.citation,
              source_url=excluded.source_url,scale=excluded.scale,
              scale_denominator=excluded.scale_denominator,state=excluded.state,
              metadata_json=excluded.metadata_json
        """, (str(layer_id), kind, str(title), citation, source_url, scale,
              denominator, state, _json(dict(metadata or {}))))

    def add_feature(self, layer_id: str, external_id: Any,
                    geometry: Mapping[str, Any], properties: Mapping[str, Any], *,
                    kind: str | None = None, name: str | None = None,
                    state: str | None = None) -> int:
        bounds = geometry_bounds(geometry)
        layer = self.connection.execute(
            "SELECT kind,state FROM layers WHERE layer_id=?", (layer_id,)).fetchone()
        if layer is None:
            raise ValueError(f"unregistered layer: {layer_id}")
        feature_kind = kind or layer["kind"]
        if feature_kind not in KINDS:
            raise ValueError(f"unsupported feature kind: {feature_kind}")
        props = dict(properties)
        feature_name = name or next((str(props[key]) for key in
                                     ("name", "nm", "unit_name", "site_name", "serial")
                                     if props.get(key) not in (None, "")), None)
        feature_state = state or props.get("st") or props.get("state") or layer["state"]
        lon = _finite(props.get("lon") if "lon" in props else props.get("x"))
        lat = _finite(props.get("lat") if "lat" in props else props.get("y"))
        if geometry.get("type") == "Point":
            lon, lat = float(geometry["coordinates"][0]), float(geometry["coordinates"][1])
        external = str(external_id)
        self.connection.execute("""
            INSERT INTO features(layer_id,external_id,kind,name,state,lon,lat,
                                 geometry_json,properties_json)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(layer_id,external_id) DO UPDATE SET
              kind=excluded.kind,name=excluded.name,state=excluded.state,
              lon=excluded.lon,lat=excluded.lat,
              geometry_json=excluded.geometry_json,
              properties_json=excluded.properties_json
        """, (layer_id, external, feature_kind, feature_name, feature_state,
              lon, lat, _json(geometry), _json(props)))
        row = self.connection.execute(
            "SELECT feature_pk FROM features WHERE layer_id=? AND external_id=?",
            (layer_id, external)).fetchone()
        feature_pk = int(row[0])
        self.connection.execute("DELETE FROM feature_index WHERE feature_pk=?", (feature_pk,))
        min_lon, min_lat, max_lon, max_lat = bounds
        self.connection.execute(
            "INSERT INTO feature_index VALUES(?,?,?,?,?)",
            (feature_pk, min_lon, max_lon, min_lat, max_lat))
        return feature_pk

    def ingest_geojson(self, path: str | os.PathLike[str], *, layer_id: str,
                       kind: str, title: str, citation: str | None = None,
                       source_url: str | None = None, scale: str | None = None,
                       scale_denominator: int | None = None,
                       state: str | None = None) -> int:
        self.register_layer(layer_id, kind, title, citation=citation,
                            source_url=source_url, scale=scale,
                            scale_denominator=scale_denominator, state=state,
                            metadata={"input": os.fspath(path)})
        count = 0
        with open(path, encoding="utf-8") as source:
            first = source.read(1)
            source.seek(0)
            if first in ("\x1e", "{"):
                # A FeatureCollection is ordinary JSON; otherwise consume an
                # RFC 8142/line-delimited sequence without materializing it.
                try:
                    payload = json.load(source)
                except json.JSONDecodeError:
                    source.seek(0)
                    payload = None
                if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
                    features: Iterable[Mapping[str, Any]] = payload.get("features") or []
                elif isinstance(payload, dict) and payload.get("type") == "Feature":
                    features = [payload]
                elif payload is None:
                    source.seek(0)
                    features = (json.loads(line.lstrip("\x1e")) for line in source
                                if line.strip().lstrip("\x1e"))
                else:
                    raise ValueError(f"unsupported GeoJSON shape: {path}")
            else:
                raise ValueError(f"unsupported GeoJSON input: {path}")
            with self.transaction():
                for index, feature in enumerate(features, 1):
                    props = feature.get("properties") or {}
                    external_id = (feature.get("id") if feature.get("id") is not None
                                   else props.get("source_id") or props.get("fid") or index)
                    self.add_feature(layer_id, external_id, feature.get("geometry") or {},
                                     props, state=state)
                    count += 1
        return count

    def ingest_ws6_geology(self, path: str | os.PathLike[str], *,
                           layer_prefix: str | None = None,
                           state: str | None = None) -> dict[str, int]:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        prefix = layer_prefix or f"ws6:{data.get('aoi') or Path(path).stem}"
        sources = data.get("sources") or {}
        source_keys = {str(unit.get("src") or "unknown") for unit in data.get("units") or []}
        counts = {"geology": 0, "fault": 0}
        with self.transaction():
            for source_key in sorted(source_keys):
                source = sources.get(source_key) or {}
                scale = source.get("scale_note") or source.get("scale")
                layer_id = f"{prefix}:geology:{source_key}"
                self.register_layer(
                    layer_id, "geology", source.get("title") or source.get("ref")
                    or f"{data.get('aoi')} geologic map {source_key}",
                    citation=source.get("ref"),
                    source_url=source.get("url") or WS6_SOURCE_URLS.get(source_key),
                    scale=scale, scale_denominator=source.get("scale_denominator"),
                    state=state,
                    metadata={"aoi": data.get("aoi"), "input": os.fspath(path),
                              "source_key": source_key})
            for index, unit in enumerate(data.get("units") or [], 1):
                source_key = str(unit.get("src") or "unknown")
                props = dict(unit)
                geometry = {"type": "MultiPolygon", "coordinates": props.pop("g")}
                self.add_feature(f"{prefix}:geology:{source_key}",
                                 unit.get("id", index), geometry, props, kind="geology",
                                 state=state)
                counts["geology"] += 1

            fault_sources = {str(row.get("src") or "unknown")
                             for row in data.get("faults") or []}
            for source_key in sorted(fault_sources):
                source = sources.get(source_key) or {}
                layer_id = f"{prefix}:fault:{source_key}"
                self.register_layer(
                    layer_id, "fault", source.get("title") or source.get("ref")
                    or f"{data.get('aoi')} mapped faults {source_key}",
                    citation=source.get("ref"),
                    source_url=source.get("url") or WS6_SOURCE_URLS.get(source_key),
                    scale=source.get("scale_note") or source.get("scale"),
                    scale_denominator=source.get("scale_denominator"),
                    state=state,
                    metadata={"aoi": data.get("aoi"), "input": os.fspath(path),
                              "source_key": source_key})
            for index, fault in enumerate(data.get("faults") or [], 1):
                source_key = str(fault.get("src") or "unknown")
                props = dict(fault)
                geometry = {"type": "LineString", "coordinates": props.pop("path")}
                self.add_feature(f"{prefix}:fault:{source_key}",
                                 fault.get("id", index), geometry, props, kind="fault",
                                 state=state)
                counts["fault"] += 1
        return counts

    def ingest_columnar_points(self, path: str | os.PathLike[str], *,
                               layer_id: str, kind: str, title: str,
                               source_url: str | None = None) -> int:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        xs, ys = data.get("x") or [], data.get("y") or []
        if len(xs) != len(ys):
            raise ValueError(f"columnar x/y length mismatch: {path}")
        self.register_layer(layer_id, kind, title, source_url=source_url,
                            state=data.get("state"),
                            metadata={"input": os.fspath(path), "retrieved": data.get("retrieved")})
        array_fields = {key: value for key, value in data.items()
                        if isinstance(value, list) and len(value) == len(xs)}
        id_fields = ("id", "serial", "source_id", "record_id")
        count = 0
        with self.transaction():
            for index, (lon, lat) in enumerate(zip(xs, ys)):
                if _finite(lon) is None or _finite(lat) is None:
                    continue
                props = {key: values[index] for key, values in array_fields.items()}
                props.setdefault("state", data.get("state"))
                external = next((props[key] for key in id_fields
                                 if props.get(key) not in (None, "")), index)
                self.add_feature(layer_id, external,
                                 {"type": "Point", "coordinates": [lon, lat]}, props,
                                 kind=kind, state=data.get("state"))
                count += 1
        return count

    def ingest_land_status(self, plss_path: str | os.PathLike[str],
                           status_path: str | os.PathLike[str], *,
                           layer_id: str, title: str) -> int:
        with open(plss_path, encoding="utf-8") as source:
            plss = json.load(source)
        with open(status_path, encoding="utf-8") as source:
            status = json.load(source)
        rows = {str(row.get("id")): row for row in status.get("sections") or []
                if row.get("id") not in (None, "")}
        self.register_layer(
            layer_id, "land_status", title,
            citation=status.get("note"), state="ID" if status.get("aoi") == "cassia" else None,
            metadata={"plss_input": os.fspath(plss_path),
                      "status_input": os.fspath(status_path),
                      "generated": status.get("generated")})
        count = 0
        with self.transaction():
            for index, feature in enumerate(plss.get("features") or [], 1):
                props = dict(feature.get("properties") or {})
                external = str(props.get("id") or feature.get("id") or index)
                props.update(rows.get(external) or {})
                self.add_feature(layer_id, external, feature.get("geometry") or {}, props,
                                 kind="land_status")
                count += 1
        return count

    def ingest_geophys_surveys(self, path: str | os.PathLike[str], *,
                               layer_id: str = "geophys:surveys") -> int:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        provenance = data.get("provenance") or []
        self.register_layer(
            layer_id, "survey", "USGS airborne and Earth MRI survey footprints",
            citation="; ".join(str(row.get("src")) for row in provenance
                               if isinstance(row, dict) and row.get("src")),
            metadata={"input": os.fspath(path), "generated": data.get("generated"),
                      "provenance": provenance})
        count = 0
        with self.transaction():
            for index, survey in enumerate(data.get("surveys") or [], 1):
                ring = survey.get("g")
                if not isinstance(ring, list) or len(ring) < 3:
                    continue
                props = dict(survey)
                props.pop("g", None)
                external = (survey.get("id") or survey.get("src_id")
                            or f"{survey.get('src','survey')}:{index}")
                self.add_feature(layer_id, external,
                                 {"type": "Polygon", "coordinates": [ring]}, props,
                                 kind="survey")
                count += 1
        return count

    def ingest_vector_registry(self, path: str | os.PathLike[str], *,
                               root: str | os.PathLike[str]) -> dict[str, int]:
        """Ingest declarative pre-tile GeoJSON/GeoJSONSeq layer inputs.

        Production builders write this registry before PMTiles generation so
        statewide geology, faults, claim polygons, and land-context vectors
        all enter the analyst store without reverse-engineering tiles.
        """
        with open(path, encoding="utf-8") as source:
            registry = json.load(source)
        if not isinstance(registry.get("layers"), list):
            raise ValueError("spatial vector registry requires a layers array")
        counts = {}
        root_path = Path(root)
        for entry in registry["layers"]:
            required = ("id", "kind", "path", "title")
            if any(not entry.get(key) for key in required):
                raise ValueError(f"spatial vector registry row lacks {required}: {entry!r}")
            source_path = root_path / entry["path"]
            if not source_path.is_file():
                raise FileNotFoundError(f"registered pre-tile vector is missing: {source_path}")
            payload = source_path.read_bytes()
            if entry.get("bytes") is not None and entry["bytes"] != len(payload):
                raise ValueError(f"registered vector byte count changed: {source_path}")
            if entry.get("sha256") and hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                raise ValueError(f"registered vector sha256 changed: {source_path}")
            counts[entry["id"]] = self.ingest_geojson(
                source_path, layer_id=entry["id"], kind=entry["kind"],
                title=entry["title"], citation=entry.get("citation"),
                source_url=entry.get("source_url"), scale=entry.get("scale"),
                scale_denominator=entry.get("scale_denominator"),
                state=entry.get("state"))
        return counts

    def register_raster(self, raster_id: str, *, kind: str, title: str,
                        bounds: Sequence[float], path: str | None = None,
                        url: str | None = None, band: int = 1,
                        unit: str | None = None, source_url: str | None = None,
                        citation: str | None = None, state: str | None = None,
                        metadata: Mapping[str, Any] | None = None) -> None:
        if len(bounds) != 4 or any(_finite(value) is None for value in bounds):
            raise ValueError("raster bounds must be [west,south,east,north]")
        west, south, east, north = map(float, bounds)
        if not west < east or not south < north:
            raise ValueError("raster bounds are inverted or empty")
        self.connection.execute("""
            INSERT OR REPLACE INTO rasters(
              raster_id,kind,title,path,url,band,unit,source_url,citation,state,
              min_lon,max_lon,min_lat,max_lat,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (raster_id, kind, title, path, url, int(band), unit, source_url,
              citation, state, west, east, south, north, _json(dict(metadata or {}))))

    def ingest_document_index(self, path: str | os.PathLike[str]) -> int:
        """Load the public WS12 docs v1 index without copying OCR snippets."""
        with open(path, encoding="utf-8") as source:
            payload = json.load(source)
        documents = payload.get("documents") if isinstance(payload, dict) else None
        if not isinstance(documents, list):
            raise ValueError("docs index must contain a documents array")
        by_mine = payload.get("by_mine") or {}
        reverse: dict[str, set[str]] = defaultdict(set)
        for alias, ids in by_mine.items():
            for document_id in ids if isinstance(ids, list) else []:
                reverse[str(document_id)].add(str(alias))
        with self.transaction():
            self.connection.execute("DELETE FROM document_aliases")
            self.connection.execute("DELETE FROM documents")
            for row in documents:
                document_id = str(row.get("document_id") or "")
                source_url = str(row.get("source_url") or "")
                title = str(row.get("title") or "")
                if not document_id or not source_url or not title:
                    raise ValueError("docs index rows require document_id/title/source_url")
                page_count = row.get("page_count")
                indexed_pages = row.get("indexed_pages")
                if (not isinstance(page_count, int) or isinstance(page_count, bool)
                        or page_count < 0):
                    raise ValueError("docs index rows require non-negative integer page_count")
                if (not isinstance(indexed_pages, int) or isinstance(indexed_pages, bool)
                        or indexed_pages < 0 or indexed_pages > page_count):
                    raise ValueError("docs index rows require indexed_pages <= page_count")
                self.connection.execute("""
                    INSERT INTO documents(document_id,title,portal,portal_id,state,
                                          source_url,page_count,indexed_pages,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (document_id, title, row.get("portal"), row.get("portal_id"),
                      row.get("state"), source_url, page_count, indexed_pages,
                      _json(dict(row))))
                aliases = set(map(str, row.get("mine_ids") or [])) | reverse[document_id]
                for alias in sorted(alias for alias in aliases if alias):
                    self.connection.execute(
                        "INSERT OR IGNORE INTO document_aliases(alias,document_id) VALUES(?,?)",
                        (alias.casefold(), document_id))
            self.connection.execute(
                "INSERT OR REPLACE INTO store_metadata(key,value) VALUES('docs_index',?)",
                (_json({"path": os.fspath(path), "documents": len(documents),
                        "schema": payload.get("schema")}),))
        return len(documents)

    def _candidates(self, kind: str, bounds: Sequence[float]) -> list[sqlite3.Row]:
        west, south, east, north = bounds
        return self.connection.execute("""
            SELECT f.*, l.title AS layer_title, l.citation, l.source_url,
                   l.scale, l.scale_denominator, l.metadata_json AS layer_metadata_json
            FROM feature_index r
            JOIN features f ON f.feature_pk=r.feature_pk
            JOIN layers l ON l.layer_id=f.layer_id
            WHERE f.kind=? AND r.max_lon>=? AND r.min_lon<=?
              AND r.max_lat>=? AND r.min_lat<=?
        """, (kind, west, east, south, north)).fetchall()

    @staticmethod
    def _feature(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
        return json.loads(row["geometry_json"]), _object(row["properties_json"])

    @staticmethod
    def _scale(row: sqlite3.Row, props: Mapping[str, Any]) -> tuple[str | None, int | None]:
        raw = (props.get("source_scale") or props.get("scale")
               or props.get("scale_note") or row["scale"])
        explicit = props.get("scale_denominator") or props.get("native_scale")
        denominator = (parse_scale_denominator(explicit) or parse_scale_denominator(raw)
                       or parse_scale_denominator(props.get("co")))
        if denominator is None and row["scale_denominator"]:
            denominator = int(row["scale_denominator"])
        return str(raw) if raw not in (None, "") else None, denominator

    def geology_at(self, lat: Any, lon: Any) -> dict[str, Any]:
        latitude, longitude = validate_lat_lon(lat, lon)
        rows = []
        for row in self._candidates("geology", (longitude, latitude, longitude, latitude)):
            geometry, props = self._feature(row)
            if not geometry_covers_point(geometry, longitude, latitude):
                continue
            scale, denominator = self._scale(row, props)
            citation = props.get("source_ref") or props.get("citation") or row["citation"]
            source_url = props.get("source_url") or props.get("url") or row["source_url"]
            rows.append({
                "layer_id": row["layer_id"], "feature_id": row["external_id"],
                "unit_symbol": props.get("unit_label") or props.get("sn") or props.get("map_unit"),
                "unit_name": props.get("unit_name") or props.get("nm") or row["name"],
                "description": props.get("description") or props.get("de") or props.get("unit_description"),
                "age": props.get("age") or props.get("age_range")
                       or " to ".join(filter(None, [props.get("age_min"), props.get("age_max")])) or None,
                "lithology": props.get("lithology") or props.get("li"),
                "source_map": row["layer_title"], "source_citation": citation,
                "source_url": source_url, "source_scale": scale,
                "scale_denominator": denominator, "state": row["state"],
                "source_id": props.get("source_id") or props.get("src"),
                "evidence_kind": "vector_unit",
            })
        rows.sort(key=lambda item: (item["scale_denominator"] is None,
                                    item["scale_denominator"] or math.inf,
                                    str(item["layer_id"]), str(item["feature_id"])))
        raster_rows = self.connection.execute("""
            SELECT * FROM rasters WHERE kind='geology'
              AND min_lon<=? AND max_lon>=? AND min_lat<=? AND max_lat>=?
            ORDER BY raster_id
        """, (longitude, longitude, latitude, latitude)).fetchall()
        raster_context = []
        for raster in raster_rows:
            metadata = _object(raster["metadata_json"])
            if metadata.get("native_vector_ingested"):
                continue
            raw_scale = metadata.get("source_scale") or metadata.get("scale")
            denominator = (parse_scale_denominator(metadata.get("scale_denominator"))
                           or parse_scale_denominator(raw_scale))
            raster_context.append({
                "raster_id": raster["raster_id"], "source_map": raster["title"],
                "source_citation": raster["citation"],
                "source_url": raster["source_url"], "source_scale": raw_scale,
                "scale_denominator": denominator,
                "evidence_kind": "raster_map_context",
                "unit_status": "not_queryable_from_raster",
                "limitation": (metadata.get("limitation") or
                               "The georeferenced map image covers this point, but no "
                               "native unit vector is ingested; it cannot be asserted as "
                               "the source of a rock-unit classification."),
            })
        raster_context.sort(key=lambda item: (item["scale_denominator"] is None,
                                              item["scale_denominator"] or math.inf,
                                              item["raster_id"]))
        return {
            "query": {"lat": latitude, "lon": longitude},
            "count": len(rows), "units": rows, "finest": rows[0] if rows else None,
            "higher_resolution_raster_context": raster_context,
            "ordering": "finest exact representative-fraction scale first; unknown scales last",
            "note": ("Every row is a vector polygon covering the point. Raster-only quad "
                     "coverage is context, never a unit classification."),
        }

    def claims_at(self, lat: Any, lon: Any, *, include_closed: bool = True,
                  representative_point_radius_m: float = 50.0) -> dict[str, Any]:
        latitude, longitude = validate_lat_lon(lat, lon)
        radius = max(0.0, float(representative_point_radius_m))
        candidates = self._candidates("claim", _radius_bbox(latitude, longitude, radius))
        matches = []
        for row in candidates:
            geometry, props = self._feature(row)
            status = str(props.get("status") or props.get("disp") or props.get("layer") or "unknown")
            if not include_closed and "clos" in status.casefold():
                continue
            distance = geometry_distance_m(geometry, latitude, longitude)
            exact_geometry = geometry.get("type") in ("Polygon", "MultiPolygon")
            if (exact_geometry and distance != 0) or (not exact_geometry and distance > radius):
                continue
            matches.append({
                "claim_id": props.get("serial") or row["external_id"],
                "name": props.get("name") or props.get("nm") or row["name"],
                "status": status, "claim_type": props.get("type"),
                "system": props.get("system") or props.get("system_id") or "federal_mlrs",
                "state": row["state"], "distance_m": round(distance, 2),
                "relationship": "polygon_covers_point" if exact_geometry else "representative_point_nearby",
                "exact_claim_geometry": exact_geometry,
                "source_url": props.get("source_url") or props.get("url") or row["source_url"],
                "source_layer": row["layer_id"],
            })
        matches.sort(key=lambda item: (item["distance_m"], str(item["claim_id"])))
        return {
            "query": {"lat": latitude, "lon": longitude}, "count": len(matches),
            "claims": matches,
            "note": ("Polygon matches are spatial evidence. Representative-point matches are "
                     "explicitly approximate and cannot establish whether the query point is claimed."),
        }

    def _near(self, kind: str, lat: Any, lon: Any, radius_m: Any,
              *, limit: int = 100) -> tuple[float, float, float, list[tuple[float, sqlite3.Row, dict[str, Any]]]]:
        latitude, longitude = validate_lat_lon(lat, lon)
        radius = _finite(radius_m)
        if radius is None or radius < 0:
            raise ValueError("radius_m must be a non-negative finite number")
        matches = []
        for row in self._candidates(kind, _radius_bbox(latitude, longitude, radius)):
            geometry, props = self._feature(row)
            distance = geometry_distance_m(geometry, latitude, longitude)
            if distance <= radius:
                matches.append((distance, row, props))
        matches.sort(key=lambda item: (item[0], item[1]["layer_id"], item[1]["external_id"]))
        return latitude, longitude, radius, matches[:max(1, min(int(limit), 1_000))]

    def mines_near(self, lat: Any, lon: Any, radius_m: Any, *,
                   limit: int = 100) -> dict[str, Any]:
        latitude, longitude, radius, matches = self._near(
            "mine", lat, lon, radius_m, limit=limit)
        mines = []
        for distance, row, props in matches:
            mines.append({
                "mine_id": props.get("id") or props.get("source_id") or row["external_id"],
                "mine_name": props.get("name") or props.get("nm") or row["name"],
                "state": row["state"], "distance_m": round(distance, 2),
                "commodities": props.get("commodities") or props.get("c"),
                "status": props.get("status") or props.get("stx") or props.get("st"),
                "source": row["layer_title"], "source_url": props.get("source_url")
                or props.get("url") or row["source_url"],
                "lat": row["lat"], "lon": row["lon"],
            })
        return {"query": {"lat": latitude, "lon": longitude, "radius_m": radius},
                "count": len(mines), "mines": mines}

    def faults_near(self, lat: Any, lon: Any, radius_m: Any, *,
                    limit: int = 100) -> dict[str, Any]:
        latitude, longitude, radius, matches = self._near(
            "fault", lat, lon, radius_m, limit=limit)
        faults = []
        for distance, row, props in matches:
            scale, denominator = self._scale(row, props)
            faults.append({
                "fault_id": props.get("source_id") or props.get("id") or row["external_id"],
                "fault_name": props.get("name") or props.get("nm"),
                "fault_type": props.get("fault_type") or props.get("ty"),
                "distance_m": round(distance, 2), "state": row["state"],
                "source_map": row["layer_title"],
                "source_citation": props.get("source_ref") or row["citation"],
                "source_url": props.get("source_url") or props.get("url") or row["source_url"],
                "source_scale": scale, "scale_denominator": denominator,
            })
        return {"query": {"lat": latitude, "lon": longitude, "radius_m": radius},
                "count": len(faults), "faults": faults}

    def _surveys_at(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        surveys = []
        for row in self._candidates("survey",
                                    (longitude, latitude, longitude, latitude)):
            geometry, props = self._feature(row)
            if not geometry_covers_point(geometry, longitude, latitude):
                continue
            surveys.append({
                "survey_id": props.get("id") or props.get("src_id") or row["external_id"],
                "name": props.get("name") or props.get("nm") or row["name"],
                "year": props.get("year") or props.get("yr"),
                "kind": props.get("kind"),
                "line_spacing_km": props.get("spacing"),
                "flight_altitude_m": props.get("alt"),
                "altitude_type": props.get("alt_type"),
                "publication": props.get("pub"),
                "source": props.get("src") or row["layer_title"],
                "source_url": props.get("source_url") or row["source_url"],
            })
        surveys.sort(key=lambda row: (str(row.get("year") or ""),
                                      str(row.get("survey_id") or "")), reverse=True)
        return surveys

    def mag_at(self, lat: Any, lon: Any) -> dict[str, Any]:
        latitude, longitude = validate_lat_lon(lat, lon)
        surveys = self._surveys_at(latitude, longitude)
        point_rows = []
        # Pre-sampled grids are the dependency-free path and are useful for
        # deterministic tests and serverless deployments with fixed samples.
        for row in self._candidates("magnetic_sample",
                                    _radius_bbox(latitude, longitude, 50_000)):
            geometry, props = self._feature(row)
            value = _finite(props.get("value_nt") if "value_nt" in props else props.get("nt"))
            if value is None:
                continue
            distance = geometry_distance_m(geometry, latitude, longitude)
            cell = _finite(props.get("cell_size_m")) or 0.0
            point_rows.append((distance, cell, row, props, value))
        point_rows.sort(key=lambda item: (item[0], item[1], item[2]["external_id"]))
        if point_rows:
            distance, cell, row, props, value = point_rows[0]
            return {
                "query": {"lat": latitude, "lon": longitude}, "status": "sampled",
                "value_nt": value, "distance_to_sample_m": round(distance, 2),
                "cell_size_m": cell or None, "survey": props.get("survey"),
                "survey_id": props.get("survey_id"),
                "source": row["layer_title"],
                "source_url": props.get("source_url") or row["source_url"],
                "provenance": props.get("provenance") or props.get("citation")
                or row["citation"],
                "covering_surveys": surveys,
            }

        raster = self.connection.execute("""
            SELECT * FROM rasters WHERE kind='aeromag'
              AND min_lon<=? AND max_lon>=? AND min_lat<=? AND max_lat>=?
            ORDER BY COALESCE(json_extract(metadata_json,'$.cell_size_m'),1e30), raster_id
            LIMIT 1
        """, (longitude, longitude, latitude, latitude)).fetchone()
        if raster is None:
            return {"query": {"lat": latitude, "lon": longitude},
                    "status": "not_covered", "value_nt": None,
                    "covering_surveys": surveys,
                    "note": "No registered numeric aeromagnetic COG covers this point."}
        source = raster["path"] or raster["url"]
        try:
            import rasterio  # type: ignore
            from rasterio.warp import transform  # type: ignore
        except ImportError:
            return {
                "query": {"lat": latitude, "lon": longitude},
                "status": "sampler_dependency_unavailable", "value_nt": None,
                "raster_id": raster["raster_id"], "source": raster["title"],
                "source_url": raster["source_url"], "provenance": raster["citation"],
                "covering_surveys": surveys,
                "note": "Install rasterio in the spatial-tool runtime; display-raster colours are never converted to nT.",
            }
        try:
            with rasterio.open(source) as dataset:
                xs, ys = [longitude], [latitude]
                if dataset.crs and str(dataset.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
                    xs, ys = transform("EPSG:4326", dataset.crs, xs, ys)
                sampled = next(dataset.sample(zip(xs, ys), indexes=int(raster["band"]), masked=True))
                raw = sampled[0] if getattr(sampled, "ndim", 1) else sampled
                if getattr(raw, "mask", False):
                    raise ValueError("point is nodata")
                value = float(raw)
        except Exception as exc:
            return {
                "query": {"lat": latitude, "lon": longitude}, "status": "sample_failed",
                "value_nt": None, "raster_id": raster["raster_id"],
                "source": raster["title"], "source_url": raster["source_url"],
                "provenance": raster["citation"], "covering_surveys": surveys,
                "error": str(exc)[:240],
            }
        return {
            "query": {"lat": latitude, "lon": longitude}, "status": "sampled",
            "value_nt": value, "unit": raster["unit"] or "nT",
            "raster_id": raster["raster_id"], "source": raster["title"],
            "source_url": raster["source_url"], "provenance": raster["citation"],
            "metadata": _object(raster["metadata_json"]),
            "covering_surveys": surveys,
        }

    def docs_for(self, mine_id: Any) -> dict[str, Any]:
        alias = str(mine_id or "").strip()
        if not alias:
            raise ValueError("mine_id is required")
        loaded = self.connection.execute(
            "SELECT value FROM store_metadata WHERE key='docs_index'").fetchone()
        if loaded is None:
            return {"mine_id": alias, "status": "not_loaded", "count": None,
                    "documents": [],
                    "note": "The WS12 public document index has not been loaded into this store."}
        rows = self.connection.execute("""
            SELECT d.* FROM document_aliases a
            JOIN documents d ON d.document_id=a.document_id
            WHERE a.alias=? ORDER BY d.title,d.document_id
        """, (alias.casefold(),)).fetchall()
        documents = []
        for row in rows:
            metadata = _object(row["metadata_json"])
            documents.append({
                "document_id": row["document_id"], "title": row["title"],
                "portal": row["portal"], "portal_id": row["portal_id"],
                "state": row["state"], "source_url": row["source_url"],
                "page_count": row["page_count"],
                "indexed_pages": row["indexed_pages"],
                "mine_names": metadata.get("mine_names") or [],
                "doc_date": metadata.get("doc_date"), "doc_type": metadata.get("doc_type"),
                "county": metadata.get("county"), "trs": metadata.get("trs"),
                "citation": {"document_title": row["title"],
                             "source_url": row["source_url"]},
            })
        return {"mine_id": alias, "status": "loaded", "count": len(documents),
                "documents": documents}

    def stats(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "layers": dict(self.connection.execute(
                "SELECT kind,COUNT(*) FROM layers GROUP BY kind").fetchall()),
            "features": dict(self.connection.execute(
                "SELECT kind,COUNT(*) FROM features GROUP BY kind").fetchall()),
            "documents": self.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "rasters": self.connection.execute("SELECT COUNT(*) FROM rasters").fetchone()[0],
        }


def _remove_store_for_replace(path: Path) -> None:
    """Remove only one generated database and its two exact SQLite sidecars."""
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def build_default_store(root: str | os.PathLike[str], output: str | os.PathLike[str],
                        *, replace: bool = False,
                        scope: str = "full") -> dict[str, Any]:
    if scope not in ("full", "acceptance"):
        raise ValueError("spatial build scope must be 'full' or 'acceptance'")
    root_path, output_path = Path(root).resolve(), Path(output).resolve()
    if replace:
        _remove_store_for_replace(output_path)
    with SpatialStore(output_path) as store:
        store.initialize()
        ws6_states = {"cassia": "ID", "delamar24k": "ID", "clearlake": "CA"}
        for path in sorted((root_path / "site/data/geology").glob("*.json")):
            store.ingest_ws6_geology(path, state=ws6_states.get(path.stem))
        if scope == "full":
            for path in sorted((root_path / "build-inputs/data/claims").glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                store.ingest_columnar_points(
                    path, layer_id=f"claims:{path.stem}", kind="claim",
                    title=f"BLM MLRS {data.get('state')} {data.get('layer')} claim records",
                    source_url="https://mlrs.blm.gov/s/")
            for path in sorted((root_path / "build-inputs/data/sites").glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                store.ingest_columnar_points(
                    path, layer_id=f"sites:{path.stem}", kind="mine",
                    title=str(data.get("source") or data.get("src") or path.stem))
            for plss_path in sorted((root_path / "site/data/plss").glob("*.json")):
                status_path = root_path / "site/data/openground" / plss_path.name
                if status_path.is_file():
                    store.ingest_land_status(
                        plss_path, status_path, layer_id=f"land-status:{plss_path.stem}",
                        title=f"{plss_path.stem.title()} PLSS section land-status screen")

            surveys = root_path / "build-inputs/data/geophys/surveys.json"
            if surveys.is_file():
                store.ingest_geophys_surveys(surveys)

        # WS10 raster maps are useful context, but only a ready native vector
        # (currently DWM-193) may answer a unit question. Register every ready
        # raster footprint with that limitation intact.
        quad_inventory = root_path / "site/data/geology-quads/inventory.json"
        if quad_inventory.is_file():
            inventory = json.loads(quad_inventory.read_text(encoding="utf-8"))
            for layer in inventory.get("layers") or []:
                raster = layer.get("raster") or {}
                if raster.get("status") != "ready" or not raster.get("bounds"):
                    continue
                store.register_raster(
                    f"quad:{layer['id']}", kind="geology", title=layer.get("label") or layer["id"],
                    bounds=raster["bounds"], url=raster.get("cog_url"),
                    source_url=layer.get("product_url") or layer.get("source_url"),
                    citation=layer.get("citation"), state=layer.get("state"),
                    metadata={
                        "source_scale": layer.get("scale"),
                        "scale_denominator": parse_scale_denominator(layer.get("scale")),
                        "series": layer.get("series"),
                        "native_vector_ingested": bool(
                            (layer.get("vector") or {}).get("status") == "ready"),
                        "limitation": ("Native vector units are separately ingested."
                                       if (layer.get("vector") or {}).get("status") == "ready"
                                       else "Native unit GIS is not publicly ingested; this "
                                            "georeferenced raster is visual context only."),
                    })

        # Gate-passed WS11 aeromagnetic descriptors identify numeric COGs;
        # their XYZ display tiles are never sampled or converted from colour.
        manifest_path = root_path / "site/data/manifest.json"
        if scope == "full" and manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for descriptor in manifest.get("tiled_layers") or []:
                if descriptor.get("delivery") != "cog" or descriptor.get("kind") != "aeromag":
                    continue
                cog = descriptor.get("cog") or {}
                cog_url = cog.get("url")
                if not cog_url:
                    continue
                local = root_path / "site" / str(cog_url).lstrip("/")
                store.register_raster(
                    f"aeromag:{descriptor.get('id')}", kind="aeromag",
                    title=descriptor.get("title") or descriptor.get("attribution")
                    or descriptor.get("id"), bounds=descriptor["bounds"],
                    path=str(local) if local.is_file() else None,
                    url=None if local.is_file() else cog_url, unit="nT",
                    source_url=descriptor.get("source_url") or cog_url,
                    citation=descriptor.get("source_ref") or descriptor.get("attribution"),
                    state=descriptor.get("state"),
                    metadata={"sha256": cog.get("sha256"), "bytes": cog.get("bytes"),
                              "cell_size_m": descriptor.get("cell_size_m"),
                              "survey_provenance": descriptor.get("survey_provenance")})

        vector_registry = root_path / "build-inputs/ws12/spatial-layers.json"
        if vector_registry.is_file():
            store.ingest_vector_registry(vector_registry, root=root_path)
        docs = root_path / "site/data/docs/index.json"
        if docs.is_file():
            store.ingest_document_index(docs)
        store.connection.execute(
            "INSERT OR REPLACE INTO store_metadata(key,value) VALUES('build_scope',?)",
            (scope,))
        store.connection.commit()
        store.connection.execute("PRAGMA optimize")
        return store.stats()


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build from repository pre-tile inputs")
    build.add_argument("--root", default=os.path.dirname(os.path.dirname(__file__)))
    build.add_argument("--output", required=True)
    build.add_argument("--replace", action="store_true")
    build.add_argument(
        "--scope", choices=("full", "acceptance"), default="full",
        help="full loads all repository layers; acceptance loads named geology/docs fixtures")
    build.add_argument("--geoparquet-output",
                       help="optional production GeoParquet directory (requires pyarrow)")
    query = sub.add_parser("query", help="run one tool query")
    query.add_argument("--db", required=True)
    query.add_argument("tool", choices=("geology_at", "claims_at", "mines_near",
                                        "faults_near", "mag_at", "docs_for"))
    query.add_argument("arguments", help="JSON object")
    args = parser.parse_args()
    if args.command == "build":
        result = build_default_store(
            args.root, args.output, replace=args.replace, scope=args.scope)
        if args.geoparquet_output:
            from spatial_geoparquet import export_geoparquet
            result["geoparquet"] = export_geoparquet(
                args.output, args.geoparquet_output)
        print(json.dumps(result, indent=2))
        return 0
    arguments = json.loads(args.arguments)
    with SpatialStore(args.db, read_only=True) as store:
        method = getattr(store, args.tool)
        print(json.dumps(method(**arguments), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
