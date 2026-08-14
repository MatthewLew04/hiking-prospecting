#!/usr/bin/env python3
"""WS12 mine-file OCR, identity linking, and citation index.

The crawler owns discovery and immutable originals.  This module consumes its
JSONL manifest, verifies local originals when available, OCRs one SHA-256
document at a time, and builds a private SQLite FTS/vector index.  The only
public outputs are compact document metadata and aggregate coverage JSON.

Production binaries are deliberately invoked as subprocesses rather than
vendored: OCRmyPDF/Tesseract provide OCR and Poppler provides page-preserving
text extraction.  Tests inject an OCR runner and never require those binaries
or network access.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import dataclasses
import datetime as dt
import difflib
import hashlib
import itertools
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unicodedata
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol, Sequence


SCHEMA_VERSION = 1
PIPELINE_VERSION = "ws12-doc-index-v1"
DEFAULT_DB = Path("var/ws12/document-index.sqlite3")
DEFAULT_MANIFEST = Path("var/ws12/manifest.jsonl")
DEFAULT_PUBLIC_INDEX = Path("site/data/docs/index.json")
DEFAULT_COVERAGE = Path("site/data/docs/coverage.json")
DEFAULT_HARVEST_COVERAGE = Path("var/ws12/coverage.json")
DEFAULT_WORK = Path("pipelines/cache/ws12")
DEFAULT_PACKAGE = Path("var/ws12/deploy/document-index.sqlite3")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
STATE_RE = re.compile(r"^[A-Z]{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TRS_RE = re.compile(
    r"\bT(?:ownship)?\.?\s*(\d{1,3})\s*([NS])\.?\s*[,;/ -]*"
    r"R(?:ange)?\.?\s*(\d{1,3})\s*([EW])\.?\s*[,;/ -]*"
    r"S(?:ec(?:tion)?s?)?\.?\s*(\d{1,2})\b",
    re.I,
)
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")
CANON_DROP_RE = re.compile(
    r"\b(the|mine|mines|mining|claims?|group|lode|placer|prospect|property|"
    r"tunnel|shaft|no\.?\s*\d+|#\s*\d+|\d+)\b",
    re.I,
)


class DocumentIndexError(RuntimeError):
    """A manifest, OCR, or index invariant was violated."""


@dataclasses.dataclass(frozen=True)
class ManifestRow:
    source_url: str
    portal_id: str
    portal_source: str
    mine_id: str
    mine_names: tuple[str, ...]
    state: str
    county: str | None
    trs: tuple[str, ...]
    title: str
    doc_date: str | None
    doc_type: str | None
    sha256: str
    byte_count: int
    retrieval_date: str
    content_type: str
    s3_uri: str
    local_path: str | None
    etag: str | None
    last_modified: str | None
    public_domain: bool
    paywalled: bool
    rights_basis: str

    @property
    def document_id(self) -> str:
        return self.sha256

    @property
    def source_id(self) -> str:
        identity = "\0".join((self.portal_id, self.portal_source,
                                self.mine_id, self.source_url))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class OCRPage:
    page: int
    text: str
    engine: str = "injected"
    confidence: float | None = None


@dataclasses.dataclass(frozen=True)
class OCRResult:
    pages: tuple[OCRPage, ...]
    searchable_pdf: str | None = None
    engine: str = "injected"
    engine_version: str | None = None


@dataclasses.dataclass(frozen=True)
class FallbackResult:
    text: str
    engine: str
    confidence: float | None = None


class OCRRunner(Protocol):
    def run(self, input_pdf: str, work_dir: str) -> OCRResult: ...


class FallbackOCR(Protocol):
    def recognize(self, input_pdf: str, page: int,
                  work_dir: str) -> FallbackResult: ...


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _json_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        raise DocumentIndexError("expected string or string array")
    out: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _require_url(value: object, label: str) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DocumentIndexError(f"{label} must be an HTTP(S) URL")
    return text


def _require_s3(value: object) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise DocumentIndexError("s3_uri must be an s3://bucket/key URI")
    return text


def normalize_trs(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    match = TRS_RE.search(value)
    if not match:
        return value.upper()
    township, tdir, range_no, rdir, section = match.groups()
    return (f"T{int(township)}{tdir.upper()} R{int(range_no)}{rdir.upper()} "
            f"Sec {int(section)}")


def extract_trs(text: str, limit: int = 24) -> tuple[str, ...]:
    out: list[str] = []
    for match in TRS_RE.finditer(text or ""):
        township, tdir, range_no, rdir, section = match.groups()
        value = (f"T{int(township)}{tdir.upper()} R{int(range_no)}{rdir.upper()} "
                 f"Sec {int(section)}")
        if value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return tuple(out)


def parse_manifest_row(value: dict, base_dir: str | os.PathLike[str] = ".") -> ManifestRow:
    if not isinstance(value, dict):
        raise DocumentIndexError("manifest row must be an object")
    if value.get("public_domain") is not True or value.get("paywalled") is not False:
        raise DocumentIndexError(
            "rights must be explicit: public_domain=true and paywalled=false")
    rights_basis = str(value.get("rights_basis") or "").strip()
    if not rights_basis:
        raise DocumentIndexError("rights_basis is required for public-domain ingestion")
    version = value.get("schema_version", 1)
    if version != 1:
        raise DocumentIndexError(f"unsupported manifest schema_version {version!r}")
    source_url = _require_url(value.get("source_url"), "source_url")
    portal_id = str(value.get("portal_id") or "").strip()
    portal_source = str(value.get("portal_source") or portal_id).strip()
    mine_id = str(value.get("mine_id") or value.get("portal_mine_id") or "").strip()
    if not portal_id or not portal_source or not mine_id:
        raise DocumentIndexError("portal_id, portal_source, and mine_id are required")
    mine_names = _json_list(value.get("mine_names") or value.get("mine_name"))
    if not mine_names:
        raise DocumentIndexError("mine_name is required")
    state = str(value.get("state") or "").upper().strip()
    if not STATE_RE.fullmatch(state):
        raise DocumentIndexError("state must be a two-letter code")
    county = str(value.get("county") or "").strip() or None
    trs = tuple(normalize_trs(item) for item in _json_list(value.get("trs")))
    title = str(value.get("document_title") or value.get("title") or "").strip()
    if not title:
        title = Path(urllib.parse.urlsplit(source_url).path).name or mine_names[0]
    doc_date = str(value.get("doc_date") or "").strip() or None
    doc_type = str(value.get("doc_type") or "").strip() or None
    sha256 = str(value.get("sha256") or "").lower().strip()
    if not SHA_RE.fullmatch(sha256):
        raise DocumentIndexError("sha256 must be 64 lowercase hex characters")
    byte_count = value.get("bytes")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
        raise DocumentIndexError("bytes must be a positive integer")
    retrieval_date = str(value.get("retrieval_date") or "").strip()
    if not DATE_RE.fullmatch(retrieval_date):
        raise DocumentIndexError("retrieval_date must be YYYY-MM-DD")
    content_type = str(value.get("content_type") or "application/pdf").lower().strip()
    if content_type not in ("application/pdf", "application/octet-stream"):
        raise DocumentIndexError(f"unsupported content_type {content_type!r}")
    s3_uri = _require_s3(value.get("s3_uri"))
    local_value = value.get("local_path") or value.get("path")
    local_path = None
    if local_value:
        candidate = Path(str(local_value))
        if not candidate.is_absolute():
            candidate = Path(base_dir) / candidate
        local_path = str(candidate.resolve())
    return ManifestRow(
        source_url=source_url, portal_id=portal_id, portal_source=portal_source,
        mine_id=mine_id, mine_names=mine_names, state=state, county=county,
        trs=trs, title=title, doc_date=doc_date, doc_type=doc_type,
        sha256=sha256, byte_count=byte_count, retrieval_date=retrieval_date,
        content_type=content_type, s3_uri=s3_uri, local_path=local_path,
        etag=str(value.get("etag") or "").strip() or None,
        last_modified=str(value.get("last_modified") or "").strip() or None,
        public_domain=True, paywalled=False, rights_basis=rights_basis,
    )


def read_manifest(path: str | os.PathLike[str]) -> Iterator[ManifestRow]:
    manifest = Path(path)
    with manifest.open(encoding="utf-8") as handle:
        first = ""
        while not first:
            first = handle.readline()
            if not first:
                return
            first = first.strip()
        if first.startswith("["):
            rest = handle.read()
            values = json.loads(first + rest)
            if not isinstance(values, list):
                raise DocumentIndexError("JSON manifest must be an array")
            for value in values:
                yield parse_manifest_row(value, manifest.parent)
            return
        for line_no, line in enumerate(itertools.chain((first,), handle), 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
                yield parse_manifest_row(value, manifest.parent)
            except (ValueError, DocumentIndexError) as exc:
                raise DocumentIndexError(f"{manifest}:{line_no}: {exc}") from exc


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256)=64),
  byte_count INTEGER NOT NULL CHECK(byte_count>0),
  content_type TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  downloaded_at TEXT,
  ocr_started_at TEXT,
  ocr_completed_at TEXT,
  indexed_at TEXT,
  embedded_at TEXT,
  pipeline_version TEXT,
  page_count INTEGER,
  low_confidence_pages INTEGER NOT NULL DEFAULT 0,
  pending_fallback_pages INTEGER NOT NULL DEFAULT 0,
  ocr_engine TEXT,
  ocr_engine_version TEXT,
  searchable_pdf_uri TEXT,
  lease_until TEXT,
  error_stage TEXT,
  error_message TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_sources (
  source_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  portal_id TEXT NOT NULL,
  portal_source TEXT NOT NULL,
  mine_id TEXT NOT NULL,
  mine_names_json TEXT NOT NULL,
  state TEXT NOT NULL,
  county TEXT,
  trs_json TEXT NOT NULL,
  title TEXT NOT NULL,
  doc_date TEXT,
  doc_type TEXT,
  source_url TEXT NOT NULL,
  retrieval_date TEXT NOT NULL,
  s3_uri TEXT NOT NULL,
  local_path TEXT,
  etag TEXT,
  last_modified TEXT,
  public_domain INTEGER NOT NULL CHECK(public_domain=1),
  paywalled INTEGER NOT NULL CHECK(paywalled=0),
  rights_basis TEXT NOT NULL,
  downloaded_at TEXT,
  UNIQUE(portal_id, portal_source, mine_id, source_url)
);
CREATE INDEX IF NOT EXISTS source_document_idx ON document_sources(document_id);
CREATE INDEX IF NOT EXISTS source_mine_idx ON document_sources(mine_id);
CREATE INDEX IF NOT EXISTS source_portal_idx ON document_sources(portal_id);
CREATE TABLE IF NOT EXISTS pages (
  document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  page INTEGER NOT NULL CHECK(page>0),
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  engine TEXT NOT NULL,
  confidence REAL,
  quality_score REAL NOT NULL,
  fallback_status TEXT NOT NULL,
  PRIMARY KEY(document_id, page)
);
CREATE TABLE IF NOT EXISTS fallback_queue (
  document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  page INTEGER NOT NULL,
  quality_score REAL NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(document_id, page)
);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  page INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  start_char INTEGER NOT NULL,
  end_char INTEGER NOT NULL,
  text TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(document_id, page, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id, page);
CREATE TABLE IF NOT EXISTS chunk_embeddings (
  chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK(dimensions>0),
  vector BLOB NOT NULL,
  PRIMARY KEY(chunk_id, model)
);
CREATE TABLE IF NOT EXISTS site_records (
  site_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  county TEXT,
  names_json TEXT NOT NULL,
  trs_json TEXT NOT NULL,
  external_ids_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_site_links (
  document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  site_id TEXT NOT NULL REFERENCES site_records(site_id) ON DELETE CASCADE,
  method TEXT NOT NULL,
  confidence TEXT NOT NULL,
  score REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  PRIMARY KEY(document_id, site_id)
);
CREATE TABLE IF NOT EXISTS identity_candidates (
  document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  site_id TEXT NOT NULL REFERENCES site_records(site_id) ON DELETE CASCADE,
  score REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  PRIMARY KEY(document_id, site_id)
);
"""


def connect(path: str | os.PathLike[str], readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    if not readonly:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(db_path), timeout=30)
    else:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA_SQL)
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5("
                "chunk_id UNINDEXED, text, title, mine_names, mine_ids, portal_ids, trs)"
            )
            fts5 = "1"
        except sqlite3.OperationalError:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS chunk_fts (chunk_id TEXT PRIMARY KEY, "
                "text TEXT, title TEXT, mine_names TEXT, mine_ids TEXT, "
                "portal_ids TEXT, trs TEXT)"
            )
            fts5 = "0"
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('fts5',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fts5,)
        )
        connection.commit()
    return connection


def sha256_file(path: str | os.PathLike[str], block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def verify_original(row: ManifestRow) -> bool:
    """Verify a local original.  S3-only rows are trusted to Part A's pin."""
    if not row.local_path:
        return False
    path = Path(row.local_path)
    if not path.is_file():
        raise DocumentIndexError(f"local original does not exist: {path}")
    size = path.stat().st_size
    if size != row.byte_count:
        raise DocumentIndexError(
            f"byte mismatch for {path}: manifest={row.byte_count}, actual={size}")
    actual = sha256_file(path)
    if actual != row.sha256:
        raise DocumentIndexError(
            f"SHA-256 mismatch for {path}: manifest={row.sha256}, actual={actual}")
    return True


def ingest_manifest(connection: sqlite3.Connection, rows: Iterable[ManifestRow],
                    verify_local: bool = True) -> dict[str, int]:
    found = downloaded = duplicates = 0
    now = utc_now()
    for row in rows:
        local_verified = verify_original(row) if verify_local and row.local_path else False
        is_downloaded = bool(local_verified or row.s3_uri)
        prior = connection.execute(
            "SELECT document_id FROM document_sources WHERE source_id=?", (row.source_id,)
        ).fetchone()
        if prior and prior["document_id"] == row.document_id:
            duplicates += 1
        connection.execute(
            "INSERT INTO documents(document_id,sha256,byte_count,content_type,"
            "discovered_at,downloaded_at,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(document_id) DO UPDATE SET "
            "byte_count=excluded.byte_count,content_type=excluded.content_type,"
            "downloaded_at=COALESCE(documents.downloaded_at,excluded.downloaded_at),"
            "updated_at=excluded.updated_at",
            (row.document_id, row.sha256, row.byte_count, row.content_type,
             now, now if is_downloaded else None, now),
        )
        connection.execute(
            "INSERT INTO document_sources(source_id,document_id,portal_id,portal_source,"
            "mine_id,mine_names_json,state,county,trs_json,title,doc_date,doc_type,"
            "source_url,retrieval_date,s3_uri,local_path,etag,last_modified,public_domain,"
            "paywalled,rights_basis,downloaded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET "
            "document_id=excluded.document_id,portal_id=excluded.portal_id,"
            "portal_source=excluded.portal_source,mine_id=excluded.mine_id,"
            "mine_names_json=excluded.mine_names_json,state=excluded.state,"
            "county=excluded.county,trs_json=excluded.trs_json,title=excluded.title,"
            "doc_date=excluded.doc_date,doc_type=excluded.doc_type,"
            "source_url=excluded.source_url,retrieval_date=excluded.retrieval_date,"
            "s3_uri=excluded.s3_uri,local_path=excluded.local_path,etag=excluded.etag,"
            "last_modified=excluded.last_modified,"
            "public_domain=excluded.public_domain,paywalled=excluded.paywalled,"
            "rights_basis=excluded.rights_basis,"
            "downloaded_at=COALESCE(document_sources.downloaded_at,excluded.downloaded_at)",
            (row.source_id, row.document_id, row.portal_id, row.portal_source,
             row.mine_id, json.dumps(row.mine_names), row.state, row.county,
             json.dumps(row.trs), row.title, row.doc_date, row.doc_type,
             row.source_url, row.retrieval_date, row.s3_uri, row.local_path,
             row.etag, row.last_modified, 1, 0, row.rights_basis,
             now if is_downloaded else None),
        )
        found += 1
        downloaded += int(is_downloaded)
    connection.commit()
    return {"found": found, "downloaded": downloaded, "unchanged": duplicates}


def _run(command: Sequence[str], *, timeout: int = 1800,
         text: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True,
                              text=text, timeout=timeout)
    except FileNotFoundError as exc:
        raise DocumentIndexError(f"required executable not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(
            "utf-8", "replace")
        raise DocumentIndexError(
            f"command failed ({command[0]}): {stderr[-1200:].strip()}") from exc


def _tool_version(executable: str) -> str | None:
    try:
        result = subprocess.run([executable, "--version"], check=False,
                                capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    line = (result.stdout or result.stderr or "").splitlines()
    return line[0][:160] if line else None


def _pdf_page_count(path: str) -> int:
    result = _run(["pdfinfo", path], timeout=120, text=True)
    match = re.search(r"^Pages:\s*(\d+)\s*$", result.stdout, re.M)
    if not match or int(match.group(1)) <= 0:
        raise DocumentIndexError(f"pdfinfo returned no positive page count for {path}")
    return int(match.group(1))


def _extract_pdf_pages(path: str) -> tuple[str, ...]:
    count = _pdf_page_count(path)
    result = _run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"],
                  timeout=max(300, count * 30))
    raw = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else result.stdout
    pages = raw.split("\f")
    if len(pages) > count and not pages[-1].strip():
        pages.pop()
    pages = pages[:count] + [""] * max(0, count - len(pages))
    return tuple(page.replace("\x00", "").strip() for page in pages)


def text_quality(text: str) -> float:
    """Conservative 0..1 OCR quality proxy used only for fallback routing."""
    text = unicodedata.normalize("NFKC", text or "").strip()
    if not text:
        return 0.0
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return 0.0
    readable = sum(char.isalnum() or char in ".,;:!?()[]%$#&'\"/-" for char in chars)
    alpha_numeric = sum(char.isalnum() for char in chars)
    words = WORD_RE.findall(text)
    wordish = sum(len(word) >= 2 and any(char.isalpha() for char in word) for word in words)
    length_score = min(1.0, len(chars) / 220.0)
    readable_ratio = readable / len(chars)
    alnum_ratio = alpha_numeric / len(chars)
    word_score = min(1.0, wordish / 28.0)
    return round(max(0.0, min(1.0,
                              0.25 * length_score + 0.25 * readable_ratio +
                              0.2 * alnum_ratio + 0.3 * word_score)), 4)


def _tesseract_page(input_pdf: str, page: int, work_dir: str) -> OCRPage:
    prefix = os.path.join(work_dir, f"page-{page:05d}")
    _run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
          "-r", "300", "-png", input_pdf, prefix], timeout=600)
    image_path = prefix + ".png"
    result = _run(["tesseract", image_path, "stdout", "--psm", "1", "tsv"], timeout=900)
    decoded = result.stdout.decode("utf-8", "replace")
    reader = csv.DictReader(decoded.splitlines(), delimiter="\t")
    lines: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    confidences: list[float] = []
    for row in reader:
        word = (row.get("text") or "").strip()
        if not word:
            continue
        lines[(row.get("block_num", ""), row.get("par_num", ""),
               row.get("line_num", ""))].append(word)
        try:
            confidence = float(row.get("conf") or -1)
            if confidence >= 0:
                confidences.append(confidence / 100.0)
        except ValueError:
            pass
    text = "\n".join(" ".join(words) for words in lines.values())
    with contextlib.suppress(OSError):
        os.unlink(image_path)
    return OCRPage(page, text, "tesseract", (sum(confidences) / len(confidences)
                                              if confidences else None))


class SystemOCRRunner:
    """OCRmyPDF first; direct page Tesseract is the installed-binary fallback."""

    def __init__(self, force_ocr: bool = False):
        self.force_ocr = force_ocr

    def run(self, input_pdf: str, work_dir: str) -> OCRResult:
        os.makedirs(work_dir, exist_ok=True)
        ocrmypdf = shutil.which("ocrmypdf")
        searchable = os.path.join(work_dir, "searchable.pdf")
        if ocrmypdf:
            mode = "--force-ocr" if self.force_ocr else "--skip-text"
            _run([ocrmypdf, mode, "--rotate-pages", "--deskew", "--jobs", "1",
                  "--optimize", "0", "--output-type", "pdf", input_pdf, searchable],
                 timeout=7200)
            pages = _extract_pdf_pages(searchable)
            return OCRResult(
                tuple(OCRPage(i, text, "ocrmypdf/tesseract", None)
                      for i, text in enumerate(pages, 1)),
                searchable, "ocrmypdf/tesseract", _tool_version(ocrmypdf),
            )

        # A clean runtime can still process mixed searchable/scanned PDFs with
        # the same Tesseract baseline.  Searchable pages avoid needless raster
        # OCR; low-quality pages are rendered at 300 dpi and sent to Tesseract.
        baseline = _extract_pdf_pages(input_pdf)
        output: list[OCRPage] = []
        for page, text in enumerate(baseline, 1):
            if not self.force_ocr and text_quality(text) >= 0.62:
                output.append(OCRPage(page, text, "pdftotext-existing", None))
            else:
                output.append(_tesseract_page(input_pdf, page, work_dir))
        return OCRResult(tuple(output), None, "tesseract-page",
                         _tool_version("tesseract"))


class CommandFallback:
    """Stronger OCR adapter using a shell-free JSON argv template.

    Placeholders: ``{input_pdf}``, ``{page}``, ``{output}``, ``{work_dir}``.
    The command writes either UTF-8 text or JSON with text/engine/confidence to
    ``{output}``.  This keeps commercial/cloud OCR policy outside this repo.
    """

    def __init__(self, argv: Sequence[str], engine: str = "strong-ocr-command"):
        if not argv:
            raise DocumentIndexError("fallback argv must not be empty")
        self.argv = tuple(str(item) for item in argv)
        self.engine = engine

    @classmethod
    def from_json(cls, value: str) -> "CommandFallback":
        try:
            argv = json.loads(value)
        except ValueError as exc:
            raise DocumentIndexError("fallback command must be a JSON argv array") from exc
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise DocumentIndexError("fallback command must be a JSON argv array")
        return cls(argv)

    def recognize(self, input_pdf: str, page: int, work_dir: str) -> FallbackResult:
        output = os.path.join(work_dir, f"fallback-{page:05d}.txt")
        values = {"input_pdf": input_pdf, "page": str(page), "output": output,
                  "work_dir": work_dir}
        command = [part.format(**values) for part in self.argv]
        _run(command, timeout=1800)
        try:
            raw = Path(output).read_text(encoding="utf-8")
        except OSError as exc:
            raise DocumentIndexError(f"fallback did not write {output}") from exc
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if isinstance(value, dict):
            text = str(value.get("text") or "")
            engine = str(value.get("engine") or self.engine)
            confidence = value.get("confidence")
            confidence = float(confidence) if confidence is not None else None
        else:
            text, engine, confidence = raw, self.engine, None
        return FallbackResult(text.strip(), engine, confidence)


def _materialize(connection: sqlite3.Connection, document_id: str,
                 work_dir: str) -> str:
    sources = connection.execute(
        "SELECT local_path,s3_uri FROM document_sources WHERE document_id=? "
        "ORDER BY CASE WHEN local_path IS NULL THEN 1 ELSE 0 END, source_id",
        (document_id,),
    ).fetchall()
    if not sources:
        raise DocumentIndexError(f"document {document_id} has no source")
    for source in sources:
        path = source["local_path"]
        if path and Path(path).is_file():
            return path
    target = os.path.join(work_dir, "original.pdf")
    s3_uri = sources[0]["s3_uri"]
    _run(["aws", "s3", "cp", "--only-show-errors", s3_uri, target], timeout=3600)
    return target


def _document_metadata(connection: sqlite3.Connection, document_id: str,
                       page_text: str = "") -> dict:
    sources = connection.execute(
        "SELECT * FROM document_sources WHERE document_id=? "
        "ORDER BY retrieval_date, source_id", (document_id,)
    ).fetchall()
    if not sources:
        raise DocumentIndexError(f"document {document_id} has no metadata")
    first = min(sources, key=lambda source: (
        str(source["title"] or "").lower().endswith(".pdf"),
        not bool(source["doc_date"]),
        not bool(source["doc_type"]),
        source["retrieval_date"], source["source_id"],
    ))
    def unique(field: str, json_value: bool = False) -> list[str]:
        values: list[str] = []
        for row in sources:
            raw = json.loads(row[field]) if json_value else [row[field]]
            for value in raw:
                value = str(value or "").strip()
                if value and value not in values:
                    values.append(value)
        return values
    trs = unique("trs_json", True)
    for value in extract_trs(page_text):
        if value not in trs:
            trs.append(value)
    return {
        "document_id": document_id,
        "sha256": document_id,
        "title": first["title"],
        "portals": unique("portal_id"),
        "portal_sources": unique("portal_source"),
        "portal_ids": unique("portal_id"),
        "mine_ids": unique("mine_id"),
        "mine_names": unique("mine_names_json", True),
        "state": first["state"],
        "county": first["county"],
        "trs": trs,
        "doc_date": first["doc_date"],
        "doc_type": first["doc_type"],
        "source_url": first["source_url"],
    }


def chunk_page(text: str, target_chars: int = 1400,
               overlap_chars: int = 180) -> list[tuple[int, int, str]]:
    """Chunk one page only; offsets remain anchors in that page's OCR text."""
    if target_chars < 200 or overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("invalid chunk size/overlap")
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        limit = min(len(text), start + target_chars)
        end = limit
        if limit < len(text):
            floor = start + max(100, target_chars // 2)
            candidates = [text.rfind("\n\n", floor, limit),
                          text.rfind(". ", floor, limit),
                          text.rfind("\n", floor, limit),
                          text.rfind(" ", floor, limit)]
            split = max(candidates)
            if split >= floor:
                end = split + (2 if text[split:split + 2] in ("\n\n", ". ") else 1)
        body = text[start:end].strip()
        if body:
            actual_start = text.find(body, start, end + 1)
            chunks.append((actual_start, actual_start + len(body), body))
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap_chars)
        whitespace = text.find(" ", next_start, min(len(text), next_start + 80))
        start = whitespace + 1 if whitespace >= 0 else next_start
    return chunks


def _vector_blob(values: Sequence[float]) -> bytes:
    if not values or any(not math.isfinite(float(value)) for value in values):
        raise DocumentIndexError("embedding must contain finite values")
    return struct.pack(f"<{len(values)}f", *(float(value) for value in values))


class HashEmbedder:
    """Deterministic local smoke-test embedder; never advertised as semantic."""

    model = "local-hash-smoke-test-v1"

    def __init__(self, dimensions: int = 64):
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        rows: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in WORD_RE.findall(text.lower()):
                digest = hashlib.sha256(token.encode()).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                vector[index] += -1.0 if digest[4] & 1 else 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            rows.append([value / norm for value in vector])
        return rows


class BedrockEmbedder:
    """Lazy AWS Bedrock Titan v2 embedder; no network is touched on import."""

    def __init__(self, model: str = "amazon.titan-embed-text-v2:0",
                 dimensions: int = 1024, client=None):
        self.model = model
        self.dimensions = dimensions
        if client is None:
            try:
                import boto3  # type: ignore
            except ImportError as exc:
                raise DocumentIndexError("boto3 is required for Bedrock embeddings") from exc
            client = boto3.client("bedrock-runtime")
        self.client = client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            response = self.client.invoke_model(
                modelId=self.model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps({"inputText": text, "dimensions": self.dimensions,
                                 "normalize": True}),
            )
            body = response["body"].read() if hasattr(response["body"], "read") else response["body"]
            value = json.loads(body)
            vectors.append([float(item) for item in value["embedding"]])
        return vectors


def _replace_index(connection: sqlite3.Connection, document_id: str,
                   pages: Sequence[tuple[OCRPage, float, str]],
                   embedder: Embedder | None, target_chars: int,
                   overlap_chars: int) -> int:
    all_text = "\n".join(page.text for page, _, _ in pages)
    metadata = _document_metadata(connection, document_id, all_text[:250_000])
    connection.execute("DELETE FROM chunk_embeddings WHERE chunk_id IN "
                       "(SELECT chunk_id FROM chunks WHERE document_id=?)", (document_id,))
    old_ids = [row[0] for row in connection.execute(
        "SELECT chunk_id FROM chunks WHERE document_id=?", (document_id,))]
    for chunk_id in old_ids:
        connection.execute("DELETE FROM chunk_fts WHERE chunk_id=?", (chunk_id,))
    connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
    records: list[tuple] = []
    for page, _, _ in pages:
        for ordinal, (start, end, text) in enumerate(
                chunk_page(page.text, target_chars, overlap_chars), 1):
            identity = f"{document_id}:{page.page}:{ordinal}:{hashlib.sha256(text.encode()).hexdigest()}"
            chunk_id = hashlib.sha256(identity.encode()).hexdigest()
            chunk_meta = dict(metadata, page=page.page, chunk=ordinal,
                              start_char=start, end_char=end)
            records.append((chunk_id, document_id, page.page, ordinal, start, end,
                            text, json.dumps(chunk_meta, separators=(",", ":"))))
    connection.executemany(
        "INSERT INTO chunks(chunk_id,document_id,page,ordinal,start_char,end_char,text,"
        "metadata_json) VALUES(?,?,?,?,?,?,?,?)", records)
    for record in records:
        chunk_id, _, _, _, _, _, text, _ = record
        connection.execute(
            "INSERT INTO chunk_fts(chunk_id,text,title,mine_names,mine_ids,portal_ids,trs) "
            "VALUES(?,?,?,?,?,?,?)",
            (chunk_id, text, metadata["title"], " ".join(metadata["mine_names"]),
             " ".join(metadata["mine_ids"]), " ".join(metadata["portal_ids"]),
             " ".join(metadata["trs"])),
        )
    if embedder and records:
        batch_size = 16
        for offset in range(0, len(records), batch_size):
            batch = records[offset:offset + batch_size]
            vectors = embedder.embed([record[6] for record in batch])
            if len(vectors) != len(batch):
                raise DocumentIndexError("embedder returned the wrong number of vectors")
            for record, vector in zip(batch, vectors):
                connection.execute(
                    "INSERT INTO chunk_embeddings(chunk_id,model,dimensions,vector) "
                    "VALUES(?,?,?,?)", (record[0], embedder.model, len(vector),
                                         _vector_blob(vector)))
    return len(records)


def process_document(connection: sqlite3.Connection, document_id: str,
                     runner: OCRRunner, fallback: FallbackOCR | None = None,
                     embedder: Embedder | None = None, work_root: str | os.PathLike[str] = DEFAULT_WORK,
                     quality_threshold: float = 0.62, target_chars: int = 1400,
                     overlap_chars: int = 180, force: bool = False) -> dict:
    row = connection.execute("SELECT * FROM documents WHERE document_id=?",
                             (document_id,)).fetchone()
    if not row:
        raise DocumentIndexError(f"unknown document {document_id}")
    if (row["indexed_at"] and row["pipeline_version"] == PIPELINE_VERSION and
            row["pending_fallback_pages"] == 0 and not force):
        return {"document_id": document_id, "status": "unchanged",
                "pages": row["page_count"],
                "chunks": connection.execute(
                    "SELECT count(*) FROM chunks WHERE document_id=?", (document_id,)
                ).fetchone()[0]}
    now = utc_now()
    lease = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=4)).replace(
        microsecond=0).isoformat()
    connection.execute(
        "UPDATE documents SET ocr_started_at=?,lease_until=?,error_stage=NULL,"
        "error_message=NULL,updated_at=? WHERE document_id=?", (now, lease, now, document_id))
    connection.commit()
    doc_work = Path(work_root) / document_id[:2] / document_id
    doc_work.mkdir(parents=True, exist_ok=True)
    try:
        input_pdf = _materialize(connection, document_id, str(doc_work))
        db_sha = sha256_file(input_pdf)
        if db_sha != document_id:
            raise DocumentIndexError(
                f"materialized SHA-256 mismatch: expected {document_id}, got {db_sha}")
        result = runner.run(input_pdf, str(doc_work))
        if not result.pages or tuple(page.page for page in result.pages) != tuple(
                range(1, len(result.pages) + 1)):
            raise DocumentIndexError("OCR runner must return every page numbered from 1")
        processed: list[tuple[OCRPage, float, str]] = []
        low_count = pending_count = 0
        for baseline_page in result.pages:
            page = baseline_page
            quality = text_quality(page.text)
            status = "not_needed"
            if quality < quality_threshold:
                low_count += 1
                if fallback:
                    try:
                        stronger = fallback.recognize(input_pdf, page.page, str(doc_work))
                        stronger_quality = text_quality(stronger.text)
                        if (stronger_quality > quality or
                                (stronger.confidence or 0) > (page.confidence or 0)):
                            page = OCRPage(page.page, stronger.text, stronger.engine,
                                           stronger.confidence)
                            quality = stronger_quality
                        if (quality >= quality_threshold or
                                (page.confidence is not None and
                                 page.confidence >= quality_threshold)):
                            status = "completed"
                            connection.execute(
                                "DELETE FROM fallback_queue WHERE document_id=? AND page=?",
                                (document_id, page.page))
                        else:
                            status = "review_required"
                            pending_count += 1
                            connection.execute(
                                "INSERT INTO fallback_queue(document_id,page,quality_score,status,"
                                "attempts,error,updated_at) VALUES(?,?,?,?,1,NULL,?) "
                                "ON CONFLICT(document_id,page) DO UPDATE SET "
                                "quality_score=excluded.quality_score,status=excluded.status,"
                                "attempts=fallback_queue.attempts+1,error=NULL,"
                                "updated_at=excluded.updated_at",
                                (document_id, page.page, quality, status, utc_now()))
                    except Exception as exc:  # queue retains a bounded operator error
                        status = "failed"
                        pending_count += 1
                        connection.execute(
                            "INSERT INTO fallback_queue(document_id,page,quality_score,status,"
                            "attempts,error,updated_at) VALUES(?,?,?,?,1,?,?) "
                            "ON CONFLICT(document_id,page) DO UPDATE SET status=excluded.status,"
                            "attempts=fallback_queue.attempts+1,error=excluded.error,"
                            "updated_at=excluded.updated_at",
                            (document_id, page.page, quality, status, str(exc)[:1000], utc_now()))
                else:
                    status = "pending"
                    pending_count += 1
                    connection.execute(
                        "INSERT INTO fallback_queue(document_id,page,quality_score,status,"
                        "attempts,error,updated_at) VALUES(?,?,?,?,0,NULL,?) "
                        "ON CONFLICT(document_id,page) DO UPDATE SET quality_score=excluded.quality_score,"
                        "status=excluded.status,updated_at=excluded.updated_at",
                        (document_id, page.page, quality, status, utc_now()))
            else:
                connection.execute("DELETE FROM fallback_queue WHERE document_id=? AND page=?",
                                   (document_id, page.page))
            processed.append((page, quality, status))
        connection.execute("DELETE FROM pages WHERE document_id=?", (document_id,))
        connection.executemany(
            "INSERT INTO pages(document_id,page,text,text_sha256,engine,confidence,"
            "quality_score,fallback_status) VALUES(?,?,?,?,?,?,?,?)",
            [(document_id, page.page, page.text,
              hashlib.sha256(page.text.encode("utf-8")).hexdigest(), page.engine,
              page.confidence, quality, status) for page, quality, status in processed],
        )
        chunks = _replace_index(connection, document_id, processed, embedder,
                                target_chars, overlap_chars)
        completed = utc_now()
        connection.execute(
            "UPDATE documents SET ocr_completed_at=?,indexed_at=?,embedded_at=?,"
            "pipeline_version=?,page_count=?,low_confidence_pages=?,"
            "pending_fallback_pages=?,ocr_engine=?,ocr_engine_version=?,"
            "searchable_pdf_uri=?,lease_until=NULL,error_stage=NULL,error_message=NULL,"
            "updated_at=? WHERE document_id=?",
            (completed if not pending_count else None, completed,
             completed if embedder else None, PIPELINE_VERSION, len(processed), low_count,
             pending_count, result.engine, result.engine_version,
             result.searchable_pdf, completed, document_id),
        )
        connection.commit()
        return {"document_id": document_id,
                "status": "indexed" if not pending_count else "needs_fallback",
                "pages": len(processed), "chunks": chunks,
                "low_confidence_pages": low_count,
                "pending_fallback_pages": pending_count,
                "embedded": bool(embedder)}
    except Exception as exc:
        connection.rollback()
        connection.execute(
            "UPDATE documents SET lease_until=NULL,error_stage='ocr_index',error_message=?,"
            "updated_at=? WHERE document_id=?", (str(exc)[:2000], utc_now(), document_id))
        connection.commit()
        raise


def pending_documents(connection: sqlite3.Connection, limit: int | None = None,
                      force: bool = False) -> list[str]:
    where = ("1=1" if force else
             "indexed_at IS NULL OR pipeline_version IS NULL OR pipeline_version<>? "
             "OR pending_fallback_pages>0")
    params: list[object] = [] if force else [PIPELINE_VERSION]
    sql = f"SELECT document_id FROM documents WHERE {where} ORDER BY discovered_at,document_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row[0] for row in connection.execute(sql, params)]


@dataclasses.dataclass(frozen=True)
class SiteRecord:
    site_id: str
    state: str
    county: str | None
    names: tuple[str, ...]
    trs: tuple[str, ...]
    external_ids: tuple[str, ...]


def parse_site_record(value: dict) -> SiteRecord:
    if not isinstance(value, dict):
        raise DocumentIndexError("site record must be an object")
    site_id = str(value.get("site_id") or value.get("our_id") or value.get("id") or "").strip()
    state = str(value.get("state") or "").upper().strip()
    names = _json_list(value.get("names") or value.get("mine_names") or value.get("name"))
    if not site_id or not STATE_RE.fullmatch(state) or not names:
        raise DocumentIndexError("site_id, two-letter state, and name(s) are required")
    county = str(value.get("county") or "").strip() or None
    trs = tuple(normalize_trs(item) for item in _json_list(value.get("trs")))
    ids: list[str] = [site_id]
    external = value.get("external_ids") or {}
    if isinstance(external, dict):
        candidates: list[object] = []
        for namespace, raw in external.items():
            for item in _json_list(raw):
                candidates.extend((item, f"{namespace}:{item}"))
    else:
        candidates = list(_json_list(external))
    for key in ("mrds_id", "usmin_id", "portal_id", "mine_id"):
        if value.get(key):
            candidates.extend((value[key], f"{key}:{value[key]}"))
    for candidate in candidates:
        item = str(candidate).strip()
        if item and item not in ids:
            ids.append(item)
    return SiteRecord(site_id, state, county, names, trs, tuple(ids))


def read_site_records(path: str | os.PathLike[str]) -> list[SiteRecord]:
    source = Path(path)
    if source.suffix.lower() == ".jsonl":
        values = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
    else:
        value = json.loads(source.read_text(encoding="utf-8"))
        values = value if isinstance(value, list) else value.get("records") or value.get("sites")
        if not isinstance(values, list):
            raise DocumentIndexError("site catalog must contain a records/sites array")
    return [parse_site_record(value) for value in values]


def site_records_from_columnar(path: str | os.PathLike[str]) -> list[SiteRecord]:
    """Adapt repository MRDS/stategeo build inputs into the common catalog."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    count = value.get("n")
    ids, names = value.get("id"), value.get("nm")
    state = str(value.get("state") or "").upper()
    source = str(value.get("src") or "site")
    if (not isinstance(count, int) or not isinstance(ids, list) or
            not isinstance(names, list) or len(ids) != count or len(names) != count or
            not STATE_RE.fullmatch(state)):
        raise DocumentIndexError(f"{path} is not a named columnar site artifact")
    out: list[SiteRecord] = []
    for source_id, name in zip(ids, names):
        if not source_id or not name:
            continue
        site_id = f"{source}:{source_id}"
        out.append(SiteRecord(site_id, state, None, (str(name),), (),
                              (site_id, str(source_id), f"{source}:{source_id}")))
    return out


def import_site_records(connection: sqlite3.Connection,
                        records: Iterable[SiteRecord]) -> int:
    count = 0
    for record in records:
        connection.execute(
            "INSERT INTO site_records(site_id,state,county,names_json,trs_json,"
            "external_ids_json) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(site_id) DO UPDATE SET state=excluded.state,"
            "county=excluded.county,names_json=excluded.names_json,"
            "trs_json=excluded.trs_json,external_ids_json=excluded.external_ids_json",
            (record.site_id, record.state, record.county, json.dumps(record.names),
             json.dumps(record.trs), json.dumps(record.external_ids)),
        )
        count += 1
    connection.commit()
    return count


def canonical_name(value: str) -> str:
    text = re.sub(r"\s*\([^)]*\)", "", str(value or "").lower())
    text = CANON_DROP_RE.sub(" ", text)
    text = re.sub(r"[^a-z ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_number(value: str) -> str | None:
    match = re.search(r"(?:no\.?\s*|#\s*)?(\d+)\s*$", value.strip(), re.I)
    return match.group(1).lstrip("0") if match else None


def name_similarity(left: str, right: str) -> float:
    """The WS5 blend: 60% sequence ratio + 40% token Jaccard."""
    a, b = canonical_name(left), canonical_name(right)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    jaccard = len(at & bt) / len(at | bt) if at | bt else 0.0
    score = 0.6 * ratio + 0.4 * jaccard
    an, bn = _name_number(left), _name_number(right)
    if an and bn and an != bn:
        score -= 0.25
    elif an and bn:
        score = min(1.0, score + 0.15)
    return max(0.0, score)


def _id_variants(value: str) -> set[str]:
    normalized = re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())
    values = {normalized} if normalized else set()
    # State-survey IDs often arrive as "IGS DD-1 IF0126" while the portal
    # uses IF0126.  The terminal alphanumeric token is safe only at length 4+.
    tokens = re.findall(r"[A-Z]+\d+|\d+[A-Z]+|[A-Z0-9]{4,}", str(value).upper())
    if tokens and len(tokens[-1]) >= 4:
        values.add(tokens[-1])
    return values


def _trs_set(values: Iterable[str]) -> set[str]:
    return {re.sub(r"\s+", "", normalize_trs(value)).upper() for value in values if value}


def link_identities(connection: sqlite3.Connection, document_id: str | None = None,
                    ambiguity_margin: float = 0.08) -> dict[str, int]:
    """Join documents to site records: exact IDs, then unique fuzzy name+TRS."""
    documents = [document_id] if document_id else [row[0] for row in connection.execute(
        "SELECT document_id FROM documents ORDER BY document_id")]
    sites_by_state: dict[str, list[SiteRecord]] = defaultdict(list)
    for row in connection.execute("SELECT * FROM site_records ORDER BY site_id"):
        record = SiteRecord(row["site_id"], row["state"], row["county"],
                            tuple(json.loads(row["names_json"])),
                            tuple(json.loads(row["trs_json"])),
                            tuple(json.loads(row["external_ids_json"])))
        sites_by_state[record.state].append(record)
    exact_count = fuzzy_count = unresolved = 0
    for doc_id in documents:
        try:
            metadata = _document_metadata(connection, doc_id, "\n".join(
                row[0] for row in connection.execute(
                    "SELECT text FROM pages WHERE document_id=? ORDER BY page", (doc_id,))))
        except DocumentIndexError:
            continue
        connection.execute("DELETE FROM document_site_links WHERE document_id=?", (doc_id,))
        connection.execute("DELETE FROM identity_candidates WHERE document_id=?", (doc_id,))
        candidates = sites_by_state.get(metadata["state"], [])
        doc_ids = set().union(*(_id_variants(value) for value in metadata["mine_ids"]))
        exact: list[SiteRecord] = []
        for site in candidates:
            site_ids = set().union(*(_id_variants(value) for value in site.external_ids))
            if doc_ids & site_ids:
                exact.append(site)
        if exact:
            for site in exact:
                evidence = {"document_ids": sorted(doc_ids & set().union(
                    *(_id_variants(value) for value in site.external_ids)))}
                connection.execute(
                    "INSERT INTO document_site_links(document_id,site_id,method,confidence,"
                    "score,evidence_json) VALUES(?,?,?,?,?,?)",
                    (doc_id, site.site_id, "exact_id", "HIGH", 1.0,
                     json.dumps(evidence, separators=(",", ":"))))
                exact_count += 1
            continue
        doc_trs = _trs_set(metadata["trs"])
        if not doc_trs:
            unresolved += 1
            continue
        ranked: list[tuple[float, SiteRecord, str, str, set[str]]] = []
        for site in candidates:
            shared_trs = doc_trs & _trs_set(site.trs)
            if not shared_trs:
                continue
            if (metadata["county"] and site.county and
                    metadata["county"].casefold() != site.county.casefold()):
                continue
            best = max(((name_similarity(left, right), left, right)
                        for left in metadata["mine_names"] for right in site.names),
                       default=(0.0, "", ""))
            if best[0] >= 0.55:
                ranked.append((best[0] + 0.5, site, best[1], best[2], shared_trs))
        ranked.sort(key=lambda item: (-item[0], item[1].site_id))
        for score, site, left, right, shared in ranked[:3]:
            connection.execute(
                "INSERT INTO identity_candidates(document_id,site_id,score,evidence_json) "
                "VALUES(?,?,?,?)", (doc_id, site.site_id, score,
                                     json.dumps({"document_name": left,
                                                 "site_name": right,
                                                 "shared_trs": sorted(shared)},
                                                separators=(",", ":"))))
        if not ranked or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < ambiguity_margin):
            unresolved += 1
            continue
        score, site, left, right, shared = ranked[0]
        similarity = score - 0.5
        confidence = "HIGH" if similarity >= 0.9 else "MEDIUM" if similarity >= 0.72 else "LOW"
        connection.execute(
            "INSERT INTO document_site_links(document_id,site_id,method,confidence,score,"
            "evidence_json) VALUES(?,?,?,?,?,?)",
            (doc_id, site.site_id, "fuzzy_name_trs", confidence, score,
             json.dumps({"document_name": left, "site_name": right,
                         "name_similarity": round(similarity, 4),
                         "shared_trs": sorted(shared)}, separators=(",", ":"))))
        fuzzy_count += 1
    connection.commit()
    return {"exact": exact_count, "fuzzy": fuzzy_count, "unresolved": unresolved}


def _fts_tokens(query: str) -> list[str]:
    return [token for token in WORD_RE.findall(query or "") if len(token) > 1][:24]


def _excerpt(text: str, terms: Sequence[str], maximum: int = 760) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= maximum:
        return compact
    lowered = compact.lower()
    offsets = [lowered.find(term.lower()) for term in terms]
    offsets = [offset for offset in offsets if offset >= 0]
    center = min(offsets) if offsets else 0
    start = max(0, center - maximum // 3)
    end = min(len(compact), start + maximum)
    if end - start < maximum:
        start = max(0, end - maximum)
    return ("…" if start else "") + compact[start:end].strip() + ("…" if end < len(compact) else "")


def search_documents(connection: sqlite3.Connection, query: str, *, mine_id: str | None = None,
                     portal: str | None = None, limit: int = 6,
                     max_excerpt_chars: int = 760) -> dict:
    """Return bounded, page-anchored excerpts; never a complete OCR page."""
    terms = _fts_tokens(query)
    if not terms and not mine_id:
        return {"query": query, "count": 0, "hits": [],
                "note": "query or mine_id is required"}
    limit = min(max(int(limit), 1), 12)
    max_excerpt_chars = min(max(int(max_excerpt_chars), 120), 1000)
    fts5_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='fts5'").fetchone()
    fts5 = bool(fts5_row and fts5_row[0] == "1")
    filters: list[str] = []
    params: list[object] = []
    if mine_id:
        filters.append("EXISTS(SELECT 1 FROM document_sources ds WHERE ds.document_id=c.document_id AND ds.mine_id=?)")
        params.append(mine_id)
    if portal:
        filters.append("EXISTS(SELECT 1 FROM document_sources ds WHERE ds.document_id=c.document_id AND ds.portal_id=?)")
        params.append(portal)
    where_filter = (" AND " + " AND ".join(filters)) if filters else ""
    if fts5 and terms:
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        sql = ("SELECT c.*,bm25(chunk_fts) AS rank FROM chunk_fts "
               "JOIN chunks c ON c.chunk_id=chunk_fts.chunk_id "
               "WHERE chunk_fts MATCH ?" + where_filter + " ORDER BY rank LIMIT ?")
        rows = connection.execute(sql, [expression, *params, limit]).fetchall()
    else:
        clauses = ["lower(c.text) LIKE ?" for _ in terms]
        lexical = "(" + " OR ".join(clauses) + ")" if clauses else "1=1"
        values = [f"%{term.lower()}%" for term in terms]
        sql = ("SELECT c.*,0 AS rank FROM chunks c WHERE " + lexical + where_filter +
               " ORDER BY c.document_id,c.page,c.ordinal LIMIT ?")
        rows = connection.execute(sql, [*values, *params, limit]).fetchall()
    hits: list[dict] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        citation = {
            "document_title": metadata["title"],
            "page": int(row["page"]),
            "source_url": metadata["source_url"],
        }
        citation["markdown"] = (
            f"[{metadata['title']}, p. {row['page']}]({metadata['source_url']})")
        hits.append({
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "page": int(row["page"]),
            "excerpt": _excerpt(row["text"], terms, max_excerpt_chars),
            "citation": citation,
            "metadata": {key: metadata.get(key) for key in
                         ("portals", "portal_sources", "mine_ids", "mine_names",
                          "state", "county", "trs", "doc_date", "doc_type")},
        })
    return {
        "query": query, "mine_id": mine_id, "portal": portal,
        "count": len(hits), "hits": hits,
        "citation_rule": ("Use only these excerpts for document claims. Cite every claim "
                          "with the hit's exact document title, page, and source_url."),
        "bounded": {"max_hits": limit, "max_excerpt_chars": max_excerpt_chars},
    }


def documents_for(connection: sqlite3.Connection, mine_id: str, limit: int = 100) -> dict:
    if not str(mine_id or "").strip():
        return {"mine_id": mine_id, "count": 0, "documents": [],
                "error": "mine_id is required"}
    limit = min(max(int(limit), 1), 200)
    rows = connection.execute(
        "SELECT DISTINCT d.document_id,d.page_count,d.indexed_at,d.embedded_at,ds.* "
        "FROM documents d JOIN document_sources ds ON ds.document_id=d.document_id "
        "LEFT JOIN document_site_links dl ON dl.document_id=d.document_id "
        "WHERE ds.mine_id=? OR dl.site_id=? "
        "ORDER BY ds.title,ds.source_url LIMIT ?", (mine_id, mine_id, limit)
    ).fetchall()
    documents: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row["document_id"] in seen:
            continue
        seen.add(row["document_id"])
        documents.append({
            "document_id": row["document_id"], "title": row["title"],
            "portal": row["portal_id"], "portal_source": row["portal_source"],
            "portal_id": row["portal_id"], "mine_ids": [row["mine_id"]],
            "mine_names": json.loads(row["mine_names_json"]), "state": row["state"],
            "county": row["county"], "trs": json.loads(row["trs_json"]),
            "doc_date": row["doc_date"], "doc_type": row["doc_type"],
            "page_count": row["page_count"],
            "indexed_pages": connection.execute(
                "SELECT count(*) FROM pages WHERE document_id=?", (row["document_id"],)
            ).fetchone()[0],
            "source_url": row["source_url"], "indexed": bool(row["indexed_at"]),
            "embedded": bool(row["embedded_at"]),
        })
    return {"mine_id": mine_id, "count": len(documents), "documents": documents,
            "note": "Exact page citations are returned by search_documents."}


def validate_citations(value: dict) -> None:
    """Fail closed if a document-search response lacks a resolvable citation."""
    if not isinstance(value, dict) or not isinstance(value.get("hits"), list):
        raise DocumentIndexError("document tool response must contain hits")
    for index, hit in enumerate(value["hits"]):
        citation = hit.get("citation") if isinstance(hit, dict) else None
        if (not isinstance(citation, dict) or
                not str(citation.get("document_title") or "").strip() or
                not isinstance(citation.get("page"), int) or citation["page"] <= 0):
            raise DocumentIndexError(f"hit {index} lacks title/page citation")
        _require_url(citation.get("source_url"), f"hit {index} source_url")


def _atomic_json(path: str | os.PathLike[str], value: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8") + b"\n"
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".",
                                     delete=False) as handle:
        handle.write(payload)
        temporary = handle.name
    os.replace(temporary, destination)


def public_index(connection: sqlite3.Connection) -> dict:
    documents: list[dict] = []
    by_mine: dict[str, list[str]] = defaultdict(list)
    for doc in connection.execute("SELECT * FROM documents ORDER BY document_id"):
        sources = connection.execute(
            "SELECT * FROM document_sources WHERE document_id=? ORDER BY retrieval_date,source_id",
            (doc["document_id"],)).fetchall()
        if not sources:
            continue
        # Prefer rich catalog/page metadata over a bare link filename. Adding
        # canonical harvest source variants to an already OCR'd hash must not
        # downgrade a title such as "MRDS-W015681: ST. LOUIS MINE" to the
        # less useful "MRDS-W015681.pdf".
        first = min(sources, key=lambda source: (
            str(source["title"] or "").lower().endswith(".pdf"),
            not bool(source["doc_date"]),
            not bool(source["doc_type"]),
            source["retrieval_date"], source["source_id"],
        ))
        mine_ids: list[str] = []
        mine_names: list[str] = []
        portals: list[str] = []
        portal_sources: list[str] = []
        trs: list[str] = []
        source_urls: list[str] = []
        for source in sources:
            for target, value in ((mine_ids, source["mine_id"]),
                                  (portals, source["portal_id"]),
                                  (portal_sources, source["portal_source"]),
                                  (source_urls, source["source_url"])):
                if value and value not in target:
                    target.append(value)
            for target, field in ((mine_names, "mine_names_json"), (trs, "trs_json")):
                for value in json.loads(source[field]):
                    if value not in target:
                        target.append(value)
        linked = [row[0] for row in connection.execute(
            "SELECT site_id FROM document_site_links WHERE document_id=? ORDER BY site_id",
            (doc["document_id"],))]
        for value in linked:
            if value not in mine_ids:
                mine_ids.append(value)
        for mine_id in mine_ids:
            by_mine[mine_id].append(doc["document_id"])
        documents.append({
            "document_id": doc["document_id"], "sha256": doc["sha256"],
            "title": first["title"], "portal": portals[0], "portals": portals,
            "portal_source": portal_sources[0], "portal_sources": portal_sources,
            "portal_id": portals[0], "mine_ids": mine_ids,
            "mine_names": mine_names, "state": first["state"],
            "county": first["county"], "trs": trs, "doc_date": first["doc_date"],
            "doc_type": first["doc_type"], "page_count": doc["page_count"],
            "indexed_pages": connection.execute(
                "SELECT count(*) FROM pages WHERE document_id=?", (doc["document_id"],)
            ).fetchone()[0],
            "chunks": connection.execute(
                "SELECT count(*) FROM chunks WHERE document_id=?", (doc["document_id"],)
            ).fetchone()[0],
            "source_url": source_urls[0], "source_urls": source_urls,
        })
    generated = connection.execute("SELECT max(updated_at) FROM documents").fetchone()[0]
    return {"schema_version": SCHEMA_VERSION, "generated": generated,
            "documents": documents,
            "by_mine": {key: value for key, value in sorted(by_mine.items())}}


def load_harvest_coverage(path: str | os.PathLike[str] | None) -> dict | None:
    if not path:
        return None
    source = Path(path)
    if not source.is_file():
        return None
    value = json.loads(source.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("portals"), list):
        raise DocumentIndexError("Part A coverage must be schema_version 1 with portals[]")
    return value


def coverage(connection: sqlite3.Connection, harvest_coverage: dict | None = None) -> dict:
    keys = connection.execute(
        "SELECT DISTINCT portal_id,state FROM document_sources ORDER BY portal_id,state"
    ).fetchall()
    measured: dict[tuple[str, str], dict] = {}
    for key in keys:
        portal, state = key
        rows = connection.execute(
            "SELECT d.document_id,d.downloaded_at,d.ocr_completed_at,d.indexed_at,"
            "d.embedded_at,d.low_confidence_pages,d.pending_fallback_pages,d.error_stage "
            "FROM documents d WHERE EXISTS (SELECT 1 FROM document_sources ds "
            "WHERE ds.document_id=d.document_id AND ds.portal_id=? AND ds.state=?) "
            "ORDER BY d.document_id", (portal, state)).fetchall()
        measured[(portal, state)] = {
            "portal": portal, "state": state, "found": len(rows),
            "downloaded": sum(bool(row["downloaded_at"]) for row in rows),
            "ocrd": sum(bool(row["ocr_completed_at"]) for row in rows),
            "indexed": sum(bool(row["indexed_at"]) for row in rows),
            "embedded": sum(bool(row["embedded_at"]) for row in rows),
            "low_confidence_pages": sum(row["low_confidence_pages"] for row in rows),
            "pending_fallback": sum(row["pending_fallback_pages"] for row in rows),
            "errors": sum(bool(row["error_stage"]) for row in rows),
        }
    portals: list[dict] = []
    consumed: set[tuple[str, str]] = set()
    if harvest_coverage:
        for row in harvest_coverage["portals"]:
            portal = str(row.get("portal_id") or "").strip()
            jurisdiction = str(row.get("jurisdiction") or "").upper().strip()
            if not portal or not jurisdiction:
                raise DocumentIndexError("Part A coverage portal_id/jurisdiction are required")
            # Federal cross-cutting portals span state rows and are aggregated
            # by portal rather than being matched to a two-letter state code.
            matching = ([value for (pid, _), value in measured.items() if pid == portal]
                        if jurisdiction in ("US", "FEDERAL") else
                        [measured[(portal, jurisdiction)]] if (portal, jurisdiction) in measured
                        else [])
            consumed.update((portal, value["state"]) for value in matching)
            def summed(field: str) -> int:
                return sum(int(value[field]) for value in matching)
            crawl_complete = row.get("crawl_complete") is True
            cursor_exhausted = row.get("cursor_exhausted") is True
            declared_found = row.get("documents_found")
            declared_downloaded = row.get("documents_downloaded")
            known_found = (declared_found if isinstance(declared_found, int) and
                           (declared_found > 0 or crawl_complete) else
                           summed("found") if matching else None)
            known_downloaded = (declared_downloaded if isinstance(declared_downloaded, int) and
                                (declared_downloaded > 0 or crawl_complete) else
                                summed("downloaded") if matching else None)
            stage_zero_known = crawl_complete and known_found == 0
            portals.append({
                "portal": portal, "state": jurisdiction,
                "registry_status": row.get("registry_status"),
                "crawl_started": row.get("crawl_started") is True,
                "counts_status": row.get("counts_status"),
                "found": known_found, "downloaded": known_downloaded,
                "ocrd": summed("ocrd") if matching else 0 if stage_zero_known else None,
                "indexed": summed("indexed") if matching else 0 if stage_zero_known else None,
                "embedded": summed("embedded") if matching else 0 if stage_zero_known else None,
                "low_confidence_pages": summed("low_confidence_pages") if matching else
                                        0 if stage_zero_known else None,
                "pending_fallback": summed("pending_fallback") if matching else
                                    0 if stage_zero_known else None,
                "errors": summed("errors") if matching else 0 if stage_zero_known else None,
                "crawl_complete": crawl_complete,
                "cursor_exhausted": cursor_exhausted,
                "crawl_scope": row.get("crawl_scope"),
                "completed_at": row.get("completed_at"),
                "completion_blocker": row.get("completion_blocker"),
                "manifest_rows": row.get("manifest_rows"),
                "unique_hashes": row.get("unique_hashes"),
                "manifest_rows_sha256": row.get("manifest_rows_sha256"),
                "tasks_pending": row.get("tasks_pending"),
                "tasks_error": row.get("tasks_error"),
                "tasks_skipped": row.get("tasks_skipped"),
            })
    for key, value in measured.items():
        if key in consumed:
            continue
        portals.append({**value, "registry_status": "manifest_only",
                        "crawl_started": True, "counts_status": "manifest_only",
                        "crawl_complete": False, "cursor_exhausted": False,
                        "crawl_scope": None, "completed_at": None,
                        "completion_blocker": None,
                        "manifest_rows": value["found"], "unique_hashes": value["found"],
                        "manifest_rows_sha256": None,
                        "tasks_pending": None, "tasks_error": None, "tasks_skipped": None})
    portals.sort(key=lambda value: (value["state"], value["portal"]))
    documents = connection.execute("SELECT * FROM documents").fetchall()
    totals = {
        "found": len(documents),
        "unique_documents": len(documents),
        "discovered_links": sum(
            int(value["found"]) for value in portals
            if isinstance(value.get("found"), int)
        ),
        "downloaded": sum(bool(row["downloaded_at"]) for row in documents),
        "ocrd": sum(bool(row["ocr_completed_at"]) for row in documents),
        "indexed": sum(bool(row["indexed_at"]) for row in documents),
        "embedded": sum(bool(row["embedded_at"]) for row in documents),
        "low_confidence_pages": sum(row["low_confidence_pages"] for row in documents),
        "pending_fallback": sum(row["pending_fallback_pages"] for row in documents),
        "errors": sum(bool(row["error_stage"]) for row in documents),
    }
    generated = connection.execute("SELECT max(updated_at) FROM documents").fetchone()[0]
    return {"schema_version": SCHEMA_VERSION, "generated": generated,
            "portals": portals, "totals": totals,
            "unknown_portals": sum(value["found"] is None for value in portals),
            "notes": [
                "Counts are evidence-derived; missing is never reported as zero coverage.",
                "ocrd requires every low-confidence page to clear the stronger-OCR queue.",
                "A hash-deduplicated document can contribute to more than one portal row."
            ]}


def export_public(connection: sqlite3.Connection,
                  index_path: str | os.PathLike[str] = DEFAULT_PUBLIC_INDEX,
                  coverage_path: str | os.PathLike[str] = DEFAULT_COVERAGE,
                  harvest_coverage: dict | None = None) -> dict:
    index = public_index(connection)
    dashboard = coverage(connection, harvest_coverage)
    _atomic_json(index_path, index)
    _atomic_json(coverage_path, dashboard)
    return {"documents": len(index["documents"]), "coverage": dashboard["totals"],
            "index_path": str(index_path), "coverage_path": str(coverage_path)}


def package_database(connection: sqlite3.Connection,
                     output: str | os.PathLike[str] = DEFAULT_PACKAGE) -> dict:
    """Create a WAL-free, integrity-checked SQLite object safe for one-file S3 upload."""
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    active_path = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if destination == active_path:
        raise DocumentIndexError("package output must differ from the live WAL database")
    connection.commit()
    checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint and checkpoint[0] != 0:
        raise DocumentIndexError(f"cannot checkpoint document index WAL: {tuple(checkpoint)}")
    temporary = destination.with_name(destination.name + ".pending")
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(str(temporary) + suffix)
    packaged = sqlite3.connect(str(temporary))
    try:
        connection.backup(packaged)
        packaged.execute("PRAGMA journal_mode=DELETE")
        packaged.execute("PRAGMA foreign_keys=ON")
        integrity = packaged.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = packaged.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise DocumentIndexError(
                f"packaged DB failed integrity: {integrity}; foreign keys={len(foreign_keys)}")
        packaged.commit()
    finally:
        packaged.close()
    # immutable=1 deliberately ignores WAL: successful verification proves the
    # exact one-file deployment artifact contains the committed schema/data.
    check = sqlite3.connect(f"file:{temporary}?mode=ro&immutable=1", uri=True)
    try:
        version = check.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if not version or version[0] != str(SCHEMA_VERSION):
            raise DocumentIndexError("packaged DB has the wrong schema version")
        document_count = check.execute("SELECT count(*) FROM documents").fetchone()[0]
        chunk_count = check.execute("SELECT count(*) FROM chunks").fetchone()[0]
    finally:
        check.close()
    os.replace(temporary, destination)
    byte_count = destination.stat().st_size
    digest = sha256_file(destination)
    metadata = {
        "schema_version": SCHEMA_VERSION, "pipeline_version": PIPELINE_VERSION,
        "sha256": digest, "bytes": byte_count, "documents": document_count,
        "chunks": chunk_count, "artifact": str(destination), "packaged_at": utc_now(),
        "journal_mode": "delete", "integrity_check": "ok",
    }
    _atomic_json(str(destination) + ".meta.json", metadata)
    return metadata


def _fallback_from_args(args) -> FallbackOCR | None:
    value = args.fallback_command_json or os.environ.get("DOC_OCR_FALLBACK_COMMAND_JSON")
    return CommandFallback.from_json(value) if value else None


def _embedder_from_name(name: str) -> Embedder | None:
    if name == "none":
        return None
    if name == "hash":
        return HashEmbedder()
    if name == "bedrock":
        return BedrockEmbedder()
    raise DocumentIndexError(f"unknown embedder {name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="upsert and locally verify a Part A manifest")
    ingest.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ingest.add_argument("--no-verify-local", action="store_true")
    ocr = sub.add_parser("ocr", help="OCR/index pending hash-deduplicated documents")
    ocr.add_argument("--work-root", default=str(DEFAULT_WORK))
    ocr.add_argument("--document-id")
    ocr.add_argument("--limit", type=int)
    ocr.add_argument("--quality-threshold", type=float, default=0.62)
    ocr.add_argument("--fallback-command-json")
    ocr.add_argument("--embedder", choices=("bedrock", "hash", "none"), default="bedrock")
    ocr.add_argument("--force", action="store_true")
    sites = sub.add_parser("import-sites", help="load normalized or repo-columnar site IDs")
    sites.add_argument("paths", nargs="+")
    sites.add_argument("--columnar", action="store_true")
    link = sub.add_parser("link", help="join documents to site records")
    link.add_argument("--document-id")
    search = sub.add_parser("search", help="inspect bounded citation search output")
    search.add_argument("query")
    search.add_argument("--mine-id")
    search.add_argument("--portal")
    search.add_argument("--limit", type=int, default=6)
    docs = sub.add_parser("docs-for", help="list documents linked to a mine ID")
    docs.add_argument("mine_id")
    export = sub.add_parser("export", help="write compact public index and coverage")
    export.add_argument("--index", default=str(DEFAULT_PUBLIC_INDEX))
    export.add_argument("--coverage", default=str(DEFAULT_COVERAGE))
    export.add_argument("--portal-status", default=str(DEFAULT_HARVEST_COVERAGE),
                        help="Part A coverage JSON; absent keeps manifest-only rows explicit")
    package = sub.add_parser("package", help="checkpoint and verify a one-file deploy DB")
    package.add_argument("--output", default=str(DEFAULT_PACKAGE))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = connect(args.db)
    try:
        if args.command == "ingest":
            result = ingest_manifest(connection, read_manifest(args.manifest),
                                     verify_local=not args.no_verify_local)
        elif args.command == "ocr":
            ids = ([args.document_id] if args.document_id else
                   pending_documents(connection, args.limit, args.force))
            embedder = _embedder_from_name(args.embedder)
            fallback = _fallback_from_args(args)
            rows = [process_document(
                connection, document_id, SystemOCRRunner(), fallback, embedder,
                args.work_root, args.quality_threshold, force=args.force)
                    for document_id in ids]
            result = {"processed": len(rows), "documents": rows}
        elif args.command == "import-sites":
            records: list[SiteRecord] = []
            for path in args.paths:
                records.extend(site_records_from_columnar(path) if args.columnar
                               else read_site_records(path))
            result = {"imported": import_site_records(connection, records)}
        elif args.command == "link":
            result = link_identities(connection, args.document_id)
        elif args.command == "search":
            result = search_documents(connection, args.query, mine_id=args.mine_id,
                                      portal=args.portal, limit=args.limit)
            validate_citations(result)
        elif args.command == "docs-for":
            result = documents_for(connection, args.mine_id)
        elif args.command == "export":
            result = export_public(connection, args.index, args.coverage,
                                   load_harvest_coverage(args.portal_status))
        else:
            result = package_database(connection, args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
