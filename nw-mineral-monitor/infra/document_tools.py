"""Bounded runtime access to the private WS12 document SQLite index.

The Lambda downloads one exact, SHA-256-metadata-pinned S3 object into /tmp and
reuses it while its ETag is unchanged.  Tool results expose short OCR excerpts
and resolvable title/page/source-URL citations, never complete pages or stored
originals.

Ranking is Reciprocal Rank Fusion (k = 60) over a lexical and a vector arm.
It replaces a 0.75*cosine + 0.25*lexical blend that assigned any chunk without
a stored embedding a vector score of -1.0: one missing vector dropped a perfect
keyword match below every embedded chunk, which is exactly the failure mode of
a partially embedded corpus.  Under RRF a row missing from an arm contributes
nothing from that arm instead of being actively penalised.

The lexical arm exists only where the index carries FTS5 (schema_meta fts5='1')
and the question carries terms.  The fts5='0' fallback index answers with an
unranked LIKE filter in (document_id, page, ordinal) order, which is a page
walk: admitting it to the fusion would give document order one full arm's vote.
"""
from __future__ import annotations

import hashlib
import contextlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import Any, Mapping


TOOL_NAMES = frozenset({"search_documents", "docs_for"})
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")
# Reciprocal Rank Fusion constant, shared with the WS13 retrieval Lambda so a
# hit ranked here and a hit ranked there carry comparable scores.
RRF_K = 60
_CACHE: dict[str, str | int | None] = {"etag": None, "path": None, "bytes": None}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _configured_path() -> str | None:
    local = os.environ.get("DOC_INDEX_PATH", "").strip()
    return local or None


def _materialize(path: str | None = None, *, s3_client=None) -> str | None:
    local = path or _configured_path()
    if local and os.path.isfile(local):
        return local
    bucket = os.environ.get("DOC_INDEX_BUCKET", "").strip()
    key = os.environ.get("DOC_INDEX_KEY", "").strip()
    if not bucket or not key:
        return None
    if s3_client is None:
        import boto3  # Lambda runtime dependency; intentionally lazy in tests
        s3_client = boto3.client("s3")
    head = s3_client.head_object(Bucket=bucket, Key=key)
    etag = str(head.get("ETag") or "").strip('"')
    byte_count = int(head.get("ContentLength") or 0)
    maximum = int(os.environ.get("DOC_INDEX_MAX_BYTES", str(3_700_000_000)))
    if byte_count <= 4096 or byte_count > maximum:
        raise RuntimeError(
            f"document index size {byte_count} is outside 4097..{maximum} bytes")
    expected_sha = str((head.get("Metadata") or {}).get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise RuntimeError("document index S3 object lacks sha256 metadata")
    cached = str(_CACHE.get("path") or "")
    if (_CACHE.get("etag") == etag and _CACHE.get("bytes") == byte_count and
            cached and os.path.isfile(cached)):
        return cached
    target = os.path.join(tempfile.gettempdir(), "nwmm-ws12-doc-index.sqlite3")
    pending = target + ".pending"
    with open(pending, "wb") as destination:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"]
        for block in iter(lambda: body.read(1024 * 1024), b""):
            destination.write(block)
    actual_bytes = os.path.getsize(pending)
    actual_sha = _sha256_file(pending)
    if actual_bytes != byte_count or actual_sha != expected_sha:
        with contextlib.suppress(OSError):
            os.unlink(pending)
        raise RuntimeError(
            f"document index verification failed: expected {byte_count}/{expected_sha}, "
            f"got {actual_bytes}/{actual_sha}")
    os.replace(pending, target)
    _CACHE.update({"etag": etag, "path": target, "bytes": byte_count})
    return target


def _connect(path: str) -> sqlite3.Connection:
    # mode=ro still reads a sibling WAL during local builds. Deployed uploads
    # are checkpointed first, so this remains read-only in both environments.
    connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro",
                                 uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    version = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if not version or version[0] != "1":
        connection.close()
        raise RuntimeError("unsupported document index schema")
    return connection


def _terms(query: str) -> list[str]:
    return [term for term in WORD_RE.findall(query or "") if len(term) > 1][:24]


def _excerpt(value: str, terms: list[str], maximum: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= maximum:
        return text
    lowered = text.lower()
    offsets = [lowered.find(term.lower()) for term in terms]
    offsets = [offset for offset in offsets if offset >= 0]
    center = min(offsets) if offsets else 0
    start = max(0, center - maximum // 3)
    end = min(len(text), start + maximum)
    if end - start < maximum:
        start = max(0, end - maximum)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _citation(metadata: dict, page: int) -> dict:
    title = str(metadata.get("title") or "").strip()
    source_url = str(metadata.get("source_url") or "").strip()
    if (not title or page <= 0 or
            not re.fullmatch(r"https?://[^\s]+", source_url)):
        raise RuntimeError("indexed chunk lacks a resolvable title/page/source URL")
    return {"document_title": title, "page": page, "source_url": source_url,
            "markdown": f"[{title}, p. {page}]({source_url})"}


def _hash_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for token in WORD_RE.findall(text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "little") % dimensions
        vector[index] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _query_embedding(text: str, model: str, dimensions: int) -> list[float]:
    if model == "local-hash-smoke-test-v1":
        return _hash_embedding(text, dimensions)
    if os.environ.get("DOC_VECTOR_SEARCH", "true").lower() != "true":
        raise RuntimeError("runtime vector search is disabled")
    if not model.startswith("amazon.titan-embed-text"):
        raise RuntimeError(f"no runtime query embedder for model {model}")
    import boto3  # Lambda runtime dependency; intentionally lazy in tests
    client = boto3.client("bedrock-runtime")
    response = client.invoke_model(
        modelId=model, contentType="application/json", accept="application/json",
        body=json.dumps({"inputText": text, "dimensions": dimensions,
                         "normalize": True}),
    )
    body = response["body"].read() if hasattr(response["body"], "read") else response["body"]
    value = json.loads(body)
    vector = [float(item) for item in value["embedding"]]
    if len(vector) != dimensions:
        raise RuntimeError("query embedding dimensions do not match the index")
    return vector


def _cosine(query: list[float], blob: bytes, dimensions: int) -> float:
    if len(query) != dimensions or len(blob) != dimensions * 4:
        raise RuntimeError("stored embedding dimensions are invalid")
    candidate = struct.unpack(f"<{dimensions}f", blob)
    qnorm = math.sqrt(sum(value * value for value in query)) or 1.0
    cnorm = math.sqrt(sum(value * value for value in candidate)) or 1.0
    return sum(a * b for a, b in zip(query, candidate)) / (qnorm * cnorm)


def _search(connection: sqlite3.Connection, arguments: Mapping[str, Any]) -> dict:
    query = str(arguments.get("query") or "").strip()[:1000]
    mine_id = str(arguments.get("mine_id") or "").strip()[:256] or None
    portal = str(arguments.get("portal") or "").strip()[:128] or None
    state = str(arguments.get("state") or "").strip()[:32].upper() or None
    county = str(arguments.get("county") or "").strip()[:128] or None
    sha256 = str(arguments.get("sha256") or "").strip()[:128].lower() or None
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("sha256 filter must be 64 lowercase hex characters")
    terms = _terms(query)
    if not terms and not mine_id:
        raise ValueError("query or mine_id is required")
    limit = min(max(int(arguments.get("limit") or 6), 1), 12)
    maximum = min(max(int(arguments.get("max_excerpt_chars") or 760), 120), 1000)
    filters: list[str] = []
    params: list[Any] = []
    if mine_id:
        filters.append("EXISTS(SELECT 1 FROM document_sources ds WHERE ds.document_id=c.document_id AND ds.mine_id=?)")
        params.append(mine_id)
    if portal:
        filters.append("EXISTS(SELECT 1 FROM document_sources ds WHERE ds.document_id=c.document_id AND ds.portal_id=?)")
        params.append(portal)
    if state:
        filters.append("EXISTS(SELECT 1 FROM document_sources ds WHERE "
                       "ds.document_id=c.document_id AND upper(trim(ds.state))=?)")
        params.append(state)
    if county:
        # County is spelled both ways in the harvest (bare and '... County'),
        # exactly as it is in ws13_documents, so normalise the argument and
        # match it against both spellings rather than trusting either.
        key = re.sub(r"\s+county$", "", county.lower()).strip()
        filters.append("EXISTS(SELECT 1 FROM document_sources ds WHERE "
                       "ds.document_id=c.document_id AND "
                       "lower(trim(coalesce(ds.county,''))) IN (?,?))")
        params.extend([key, key + " county"])
    if sha256:
        # documents.document_id IS the SHA-256 of the raw original —
        # pipelines/document_index.py refuses a materialized copy whose digest
        # differs — so WS13's sha256 filter lands on the same value here.
        filters.append("c.document_id=?")
        params.append(sha256)
    extra = " AND " + " AND ".join(filters) if filters else ""
    fts5 = connection.execute(
        "SELECT value FROM schema_meta WHERE key='fts5'").fetchone()[0] == "1"
    candidate_limit = min(max(limit * 16, 64), 512)
    if terms and fts5:
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        rows = connection.execute(
            "SELECT c.*,bm25(chunk_fts) AS rank FROM chunk_fts JOIN chunks c "
            "ON c.chunk_id=chunk_fts.chunk_id WHERE chunk_fts MATCH ?" + extra +
            " ORDER BY rank LIMIT ?", [expression, *params, candidate_limit]).fetchall()
    else:
        lexical = ("(" + " OR ".join("lower(c.text) LIKE ?" for _ in terms) + ")"
                   if terms else "1=1")
        likes = [f"%{term.lower()}%" for term in terms]
        rows = connection.execute(
            "SELECT c.*,0 AS rank FROM chunks c WHERE " + lexical + extra +
            " ORDER BY c.document_id,c.page,c.ordinal LIMIT ?",
            [*likes, *params, candidate_limit]).fetchall()
    candidate_rows = {row["chunk_id"]: dict(row) for row in rows}
    # The lexical arm is exactly the ranked output of the query above, captured
    # before the mine-scope backfill: 1-based, best first. It exists only when
    # that query actually ranked something, which means BOTH terms and FTS5.
    # With no terms the SELECT degenerates to `1=1 ORDER BY document_id,page,
    # ordinal`; on an index built without FTS5 (pipelines/document_index.py
    # writes schema_meta fts5='0' and a plain table when the building SQLite
    # lacks the module) the LIKE branch orders by the same three columns with
    # rank literally 0. Both are page walks rather than relevance rankings, so
    # neither forms an arm — feeding one to RRF would hand document order the
    # same 1/61 vote as the embedding model's top hit.
    lexical_ranks = ({row["chunk_id"]: index + 1 for index, row in enumerate(rows)}
                     if (terms and fts5) else {})
    # A mine-scoped question can use every bounded chunk for that mine, so a
    # synonym absent from FTS can still be recovered by the vector model. It
    # reuses `extra` — every document-level filter, mine included — so a
    # state/county/portal bound the caller asked for is not quietly widened
    # back out by the backfill.
    if mine_id:
        for row in connection.execute(
                "SELECT c.*,0 AS rank FROM chunks c WHERE 1=1" + extra +
                " ORDER BY c.document_id,c.page,c.ordinal LIMIT ?",
                [*params, candidate_limit]):
            candidate_rows.setdefault(row["chunk_id"], dict(row))
    ranked_rows = list(candidate_rows.values())
    # Name the arm that actually ran. An fts5='0' index answers keyword queries
    # with an unranked LIKE filter, so reporting "fts" there would tell the
    # model the excerpts were relevance-ordered when they are in page order.
    retrieval_mode = ("fts" if (terms and fts5)
                      else "like_filter" if terms else "metadata_filter")
    embedding_model = None
    embedding_error = None
    vector_ranks = {}
    if query and ranked_rows:
        placeholders = ",".join("?" for _ in ranked_rows)
        model_row = connection.execute(
            "SELECT model,dimensions,count(*) AS n FROM chunk_embeddings WHERE chunk_id IN (" +
            placeholders + ") GROUP BY model,dimensions ORDER BY "
            "CASE WHEN model LIKE 'amazon.titan-embed-text%' THEN 0 ELSE 1 END,n DESC LIMIT 1",
            [row["chunk_id"] for row in ranked_rows]).fetchone()
        if model_row:
            embedding_model = model_row["model"]
            dimensions = int(model_row["dimensions"])
            try:
                query_vector = _query_embedding(query, embedding_model, dimensions)
                vector_rows = connection.execute(
                    "SELECT chunk_id,vector FROM chunk_embeddings WHERE model=? AND "
                    "chunk_id IN (" + placeholders + ")",
                    [embedding_model, *[row["chunk_id"] for row in ranked_rows]]).fetchall()
                vectors = {row["chunk_id"]: row["vector"] for row in vector_rows}
                # Only rows that actually carry a vector enter the vector arm.
                # The rest are absent from it, which under RRF costs them that
                # arm's contribution and nothing more.
                scored = [
                    (row, _cosine(query_vector, vectors[row["chunk_id"]], dimensions))
                    for row in ranked_rows if vectors.get(row["chunk_id"])]
                scored.sort(key=lambda item: (-item[1], item[0]["document_id"],
                                              item[0]["page"], item[0]["ordinal"]))
                vector_ranks = {row["chunk_id"]: index + 1
                                for index, (row, _score) in enumerate(scored)}
                retrieval_mode = ("hybrid_fts_vector" if (terms and fts5)
                                  else "like_filter_vector" if terms
                                  else "vector_mine_scope")
            except Exception as exc:  # lexical results remain safe and available
                embedding_error = str(exc)[:300]
    # Reciprocal Rank Fusion, k = 60: score = sum over arms of 1/(60 + rank).
    # The blend this replaces scored a vector-less row at -1.0, so a single
    # missing embedding sank a perfect keyword match beneath every embedded
    # chunk. Ties keep the original (document_id, page, ordinal) order, so a
    # lexical-only result set comes out in exactly the order it went in.
    for row in ranked_rows:
        lexical_rank = lexical_ranks.get(row["chunk_id"])
        vector_rank = vector_ranks.get(row["chunk_id"])
        row["_ranks"] = {"lexical": lexical_rank, "vector": vector_rank}
        row["_rrf_score"] = ((1.0 / (RRF_K + lexical_rank) if lexical_rank else 0.0) +
                             (1.0 / (RRF_K + vector_rank) if vector_rank else 0.0))
    ranked_rows.sort(key=lambda row: (-row["_rrf_score"], row["document_id"],
                                      row["page"], row["ordinal"]))
    rows = ranked_rows[:limit]
    hits = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        page = int(row["page"])
        hits.append({
            "chunk_id": row["chunk_id"], "document_id": row["document_id"],
            "page": page, "excerpt": _excerpt(row["text"], terms, maximum),
            # Both arms' ranks travel with the hit so a fusion regression is
            # visible in the tool result instead of only in the final order.
            "ranks": row["_ranks"], "rrf_score": round(row["_rrf_score"], 6),
            "citation": _citation(metadata, page),
            "metadata": {key: metadata.get(key) for key in
                         ("portals", "portal_sources", "mine_ids", "mine_names",
                          "state", "county", "trs", "doc_date", "doc_type")},
        })
    result = {"status": "loaded", "query": query, "mine_id": mine_id,
              "portal": portal, "count": len(hits), "hits": hits,
              "filters_applied": {"mine_id": mine_id, "portal": portal,
                                  "state": state, "county": county,
                                  "sha256": sha256},
              "retrieval_mode": retrieval_mode, "embedding_model": embedding_model,
              "citation_rule": ("Use only these excerpts for document claims; cite each "
                                "claim with the returned title, exact page, and source URL."),
              "bounded": {"max_hits": limit, "max_excerpt_chars": maximum}}
    # search_documents advertises the full WS13 filter set. This bounded slice
    # has no column for any of these three — admission_class is a WS13 rights
    # class and the year bounds need WS13's parsed doc_year_min/doc_year_max,
    # since doc_date here is free text — so answering a 1930s-only question
    # with an unbounded sweep and saying nothing would read as evidence.
    unapplied = [key for key in ("admission_class", "year_min", "year_max")
                 if arguments.get(key) not in (None, "", [])]
    if unapplied:
        result["filters_not_applied"] = unapplied
    if embedding_error:
        result["embedding_error"] = embedding_error
    _validate_result(result)
    return result


def _docs_for(connection: sqlite3.Connection, arguments: Mapping[str, Any]) -> dict:
    mine_id = str(arguments.get("mine_id") or "").strip()[:256]
    if not mine_id:
        raise ValueError("mine_id is required")
    rows = connection.execute(
        "SELECT DISTINCT d.document_id,d.page_count,d.indexed_at,d.embedded_at,ds.* "
        "FROM documents d JOIN document_sources ds ON ds.document_id=d.document_id "
        "LEFT JOIN document_site_links dl ON dl.document_id=d.document_id "
        "WHERE ds.mine_id=? OR dl.site_id=? ORDER BY d.document_id,"
        "CASE WHEN lower(trim(ds.title)) GLOB '*.pdf' THEN 1 ELSE 0 END,"
        "CASE WHEN ds.doc_date IS NULL OR trim(ds.doc_date)='' THEN 1 ELSE 0 END,"
        "ds.title,ds.source_url LIMIT 200",
        (mine_id, mine_id)).fetchall()
    documents, seen = [], set()
    for row in rows:
        if row["document_id"] in seen:
            continue
        seen.add(row["document_id"])
        documents.append({
            "document_id": row["document_id"], "title": row["title"],
            "portal": row["portal_id"], "portal_id": row["portal_id"],
            "portal_source": row["portal_source"], "mine_ids": [row["mine_id"]],
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
    return {"status": "loaded", "mine_id": mine_id, "count": len(documents),
            "documents": documents,
            "note": "Use search_documents for exact page citations."}


def _validate_result(result: Mapping[str, Any]) -> None:
    for index, hit in enumerate(result.get("hits") or []):
        citation = hit.get("citation") if isinstance(hit, dict) else None
        if (not isinstance(citation, dict) or
                not str(citation.get("document_title") or "").strip() or
                not isinstance(citation.get("page"), int) or citation["page"] <= 0 or
                not re.fullmatch(r"https?://[^\s]+", str(citation.get("source_url") or ""))):
            raise RuntimeError(f"document search hit {index} failed citation validation")


def execute(name: str, arguments: Mapping[str, Any] | None = None, *,
            db_path: str | None = None, s3_client=None) -> dict:
    if name not in TOOL_NAMES:
        raise ValueError(f"unknown WS12 document tool: {name}")
    path = _materialize(db_path, s3_client=s3_client)
    if path is None:
        return {"status": "not_loaded", "tool": name, "count": None,
                "error": "WS12 private document index is not deployed"}
    connection = _connect(path)
    try:
        return (_search(connection, arguments or {}) if name == "search_documents"
                else _docs_for(connection, arguments or {}))
    finally:
        connection.close()


__all__ = ["TOOL_NAMES", "execute"]
