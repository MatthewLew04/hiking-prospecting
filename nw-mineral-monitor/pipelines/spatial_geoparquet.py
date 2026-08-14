#!/usr/bin/env python3
"""Optional production GeoParquet/DuckDB adapter for the WS12 spatial store.

``spatial_store.py`` is the dependency-free ingestion/query fallback.  This
module exports that audited store to compressed GeoParquet and opens it as
DuckDB spatial views for production-scale scans.  Imports are deliberately
lazy: ordinary repository tests do not require pyarrow or duckdb, while a
production build fails with an actionable dependency message.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
from pathlib import Path
from typing import Any, Mapping, Sequence


def _point(value: Sequence[Any]) -> bytes:
    return struct.pack("<BIdd", 1, 1, float(value[0]), float(value[1]))


def _line(value: Sequence[Sequence[Any]]) -> bytes:
    return (struct.pack("<BI", 1, 2) + struct.pack("<I", len(value))
            + b"".join(struct.pack("<dd", float(point[0]), float(point[1]))
                       for point in value))


def _polygon(value: Sequence[Sequence[Sequence[Any]]]) -> bytes:
    parts = [struct.pack("<BI", 1, 3), struct.pack("<I", len(value))]
    for ring in value:
        parts.append(struct.pack("<I", len(ring)))
        parts.extend(struct.pack("<dd", float(point[0]), float(point[1]))
                     for point in ring)
    return b"".join(parts)


def geometry_wkb(geometry: Mapping[str, Any]) -> bytes:
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Point":
        return _point(coordinates)
    if kind == "LineString":
        return _line(coordinates)
    if kind == "Polygon":
        return _polygon(coordinates)
    children = {
        "MultiPoint": (4, "Point"),
        "MultiLineString": (5, "LineString"),
        "MultiPolygon": (6, "Polygon"),
    }
    if kind in children:
        code, child_kind = children[kind]
        values = coordinates or []
        return (struct.pack("<BI", 1, code) + struct.pack("<I", len(values))
                + b"".join(geometry_wkb({"type": child_kind, "coordinates": child})
                           for child in values))
    raise ValueError(f"unsupported GeoJSON geometry for WKB export: {kind}")


def _deps():
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "GeoParquet export requires pyarrow (install the WS12 production GIS extras)"
        ) from exc
    return pa, pq


def _bounds(geometry: Mapping[str, Any]) -> tuple[float, float, float, float]:
    positions = []
    def visit(value):
        if (isinstance(value, list) and len(value) >= 2
                and isinstance(value[0], (int, float))
                and isinstance(value[1], (int, float))):
            positions.append((float(value[0]), float(value[1])))
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(geometry.get("coordinates"))
    xs, ys = zip(*positions)
    return min(xs), min(ys), max(xs), max(ys)


def export_geoparquet(sqlite_path: str | os.PathLike[str],
                      output_directory: str | os.PathLike[str]) -> dict[str, Any]:
    """Export one compressed GeoParquet per feature kind plus catalog tables."""
    pa, pq = _deps()
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    kinds = [row[0] for row in connection.execute(
        "SELECT DISTINCT kind FROM features ORDER BY kind")]
    manifest = {"schema": "nwmm.ws12.geoparquet.v1", "crs": "OGC:CRS84",
                "layers": {}, "catalogs": {}}
    for kind in kinds:
        records = []
        geometry_types = set()
        total_bounds = [180.0, 90.0, -180.0, -90.0]
        for row in connection.execute("""
            SELECT f.*,l.title AS source_map,l.citation AS source_citation,
                   l.source_url,l.scale AS source_scale,l.scale_denominator
            FROM features f JOIN layers l USING(layer_id)
            WHERE f.kind=? ORDER BY f.feature_pk
        """, (kind,)):
            geometry = json.loads(row["geometry_json"])
            west, south, east, north = _bounds(geometry)
            total_bounds = [min(total_bounds[0], west), min(total_bounds[1], south),
                            max(total_bounds[2], east), max(total_bounds[3], north)]
            geometry_types.add(geometry["type"])
            records.append({
                "feature_pk": row["feature_pk"], "layer_id": row["layer_id"],
                "external_id": row["external_id"], "kind": row["kind"],
                "name": row["name"], "state": row["state"], "lon": row["lon"],
                "lat": row["lat"], "min_lon": west, "min_lat": south,
                "max_lon": east, "max_lat": north,
                "geometry": geometry_wkb(geometry),
                "properties_json": row["properties_json"],
                "source_map": row["source_map"],
                "source_citation": row["source_citation"],
                "source_url": row["source_url"], "source_scale": row["source_scale"],
                "scale_denominator": row["scale_denominator"],
            })
        if not records:
            continue
        table = pa.Table.from_pylist(records)
        geo = {"version": "1.1.0", "primary_column": "geometry", "columns": {
            "geometry": {"encoding": "WKB", "geometry_types": sorted(geometry_types),
                         "bbox": total_bounds}}}
        metadata = dict(table.schema.metadata or {})
        metadata[b"geo"] = json.dumps(geo, separators=(",", ":")).encode("utf-8")
        table = table.replace_schema_metadata(metadata)
        filename = f"{kind}.parquet"
        pq.write_table(table, output / filename, compression="zstd",
                       use_dictionary=True, write_statistics=True)
        manifest["layers"][kind] = {"file": filename, "features": len(records),
                                    "geometry_types": sorted(geometry_types),
                                    "bounds": total_bounds}

    for table_name in ("layers", "documents", "document_aliases", "rasters"):
        records = [dict(row) for row in connection.execute(
            f"SELECT * FROM {table_name}")]  # fixed allowlist, never user input
        if records:
            filename = f"{table_name}.parquet"
            pq.write_table(pa.Table.from_pylist(records), output / filename,
                           compression="zstd", use_dictionary=True)
            manifest["catalogs"][table_name] = {"file": filename,
                                                 "rows": len(records)}
    connection.close()
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


class DuckDBSpatial:
    """DuckDB connection over the exported GeoParquet feature partitions."""

    def __init__(self, directory: str | os.PathLike[str], *, database: str = ":memory:"):
        try:
            import duckdb  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "DuckDB spatial runtime requires duckdb (production GIS extras)"
            ) from exc
        self.directory = Path(directory).resolve()
        self.manifest = json.loads((self.directory / "manifest.json").read_text())
        self.connection = duckdb.connect(database)
        try:
            self.connection.execute("LOAD spatial")
        except Exception as exc:
            raise RuntimeError(
                "DuckDB spatial extension is unavailable; install it in the deployment image"
            ) from exc
        files = [str(self.directory / row["file"])
                 for row in self.manifest.get("layers", {}).values()]
        if not files:
            raise RuntimeError("GeoParquet manifest has no feature partitions")
        quoted = ",".join("'" + path.replace("'", "''") + "'" for path in files)
        self.connection.execute(f"""
            CREATE VIEW spatial_features AS
            SELECT * EXCLUDE(geometry), ST_GeomFromWKB(geometry) AS geom
            FROM read_parquet([{quoted}], union_by_name=true)
        """)

    def covering(self, kind: str, lat: float, lon: float):
        """Return rows whose exact geometry covers/intersects the WGS84 point."""
        return self.connection.execute("""
            SELECT * EXCLUDE(geom) FROM spatial_features
            WHERE kind=? AND min_lon<=? AND max_lon>=? AND min_lat<=? AND max_lat>=?
              AND ST_Intersects(geom, ST_Point(?,?))
            ORDER BY scale_denominator NULLS LAST,layer_id,external_id
        """, [kind, lon, lon, lat, lat, lon, lat]).fetchall()

    def close(self):
        self.connection.close()


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite")
    parser.add_argument("output_directory")
    args = parser.parse_args()
    print(json.dumps(export_geoparquet(args.sqlite, args.output_directory), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
