"""Runtime adapter for the six WS12 GIS/document tools.

The browser has a viewport-scoped fallback for interactive use.  Production
workers should point ``SPATIAL_DB`` at the generated SQLite artifact so tool
answers are independent of the current map viewport.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
PIPELINES = ROOT / "pipelines"
if str(PIPELINES) not in sys.path:
    sys.path.insert(0, str(PIPELINES))

from spatial_store import SpatialStore  # noqa: E402


TOOL_NAMES = frozenset({
    "geology_at", "claims_at", "mines_near", "faults_near", "mag_at", "docs_for",
})
_CACHE: dict[str, str | int | None] = {"etag": None, "path": None, "bytes": None}


def _db_path() -> str:
    return os.environ.get("SPATIAL_DB", str(ROOT / "build/spatial/ws12.sqlite"))


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_store(path: str) -> None:
    """Validate the identity-bound store without a second full-file scan.

    Deployment runs ``integrity_check`` before upload and the runtime has just
    hashed every downloaded byte against immutable S3 metadata. Repeating a
    full SQLite integrity scan here can push the first GIS request past API
    Gateway's 30-second ceiling for the national store, without adding an
    independent integrity boundary.
    """
    connection = sqlite3.connect(
        f"file:{Path(path).resolve()}?mode=ro&immutable=1", uri=True)
    try:
        version = connection.execute(
            "SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()
        if not version or version[0] != "1":
            raise RuntimeError("unsupported WS12 spatial database schema")
        required = {"store_metadata", "geology", "faults", "claims", "mines"}
        present = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(present):
            raise RuntimeError("WS12 spatial database is missing required tables")
    finally:
        connection.close()


def _materialize_db(path: str) -> str | None:
    if os.path.isfile(path):
        return path
    bucket = os.environ.get("SPATIAL_DB_BUCKET", "").strip()
    key = os.environ.get("SPATIAL_DB_KEY", "").strip()
    if not bucket or not key:
        return None
    # The configured key is exact. Never list or infer a broad bucket prefix.
    import boto3  # Lambda runtime dependency; intentionally lazy for local tests
    client = boto3.client("s3")
    head = client.head_object(Bucket=bucket, Key=key)
    etag = str(head.get("ETag") or "").strip('"')
    byte_count = int(head.get("ContentLength") or 0)
    maximum = int(os.environ.get("SPATIAL_DB_MAX_BYTES", str(8_000_000_000)))
    if byte_count <= 4096 or byte_count > maximum:
        raise RuntimeError(
            f"spatial database size {byte_count} is outside 4097..{maximum} bytes")
    expected_sha = str((head.get("Metadata") or {}).get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise RuntimeError("spatial database S3 object lacks sha256 metadata")
    cached = _CACHE.get("path")
    if (cached and _CACHE.get("etag") == etag and
            _CACHE.get("bytes") == byte_count and os.path.isfile(str(cached))):
        return str(cached)
    target = os.path.join(tempfile.gettempdir(), "nwmm-ws12-spatial.sqlite")
    pending = target + ".pending"
    client.download_file(bucket, key, pending)
    actual_bytes, actual_sha = os.path.getsize(pending), _sha256_file(pending)
    if actual_bytes != byte_count or actual_sha != expected_sha:
        with contextlib.suppress(OSError):
            os.unlink(pending)
        raise RuntimeError(
            f"spatial database verification failed: expected {byte_count}/{expected_sha}, "
            f"got {actual_bytes}/{actual_sha}")
    _validate_store(pending)
    os.replace(pending, target)
    _CACHE.update({"etag": etag, "path": target, "bytes": byte_count})
    return target


def execute(name: str, arguments: Mapping[str, Any] | None = None, *,
            db_path: str | None = None) -> dict[str, Any]:
    """Execute one allowlisted tool and return a JSON-safe result.

    Missing deployment data is an explicit ``not_loaded`` result, not an
    empty hit set.  Invalid arguments are caller errors and remain exceptions
    so an HTTP adapter can return 400 rather than laundering them as no data.
    """
    if name not in TOOL_NAMES:
        raise ValueError(f"unknown WS12 spatial tool: {name}")
    path = _materialize_db(db_path or _db_path())
    if path is None:
        return {
            "status": "not_loaded", "tool": name, "count": None,
            "error": "WS12 spatial database is not deployed",
        }
    with SpatialStore(path, read_only=True) as store:
        method = getattr(store, name)
        return method(**dict(arguments or {}))


__all__ = ["TOOL_NAMES", "execute"]
