#!/usr/bin/env python3
"""Build the private WS12 document-asset lineage catalog.

Part A's harvest manifest is authoritative for downloaded-byte identity and
portal provenance.  The addendum document-store manifest may enrich those
assets with raw/searchable store keys and OCR status; it must never replace a
harvest occurrence.  Store-only rows keep the existing 25-document pilot
usable while their portals are migrated into the harvester.

The output is metadata-only, canonical JSON under ``var/ws12``.  It separates
one SHA-keyed asset from its source occurrences and mine links, which prevents
URL churn or the same PDF appearing under several mines from creating false
document identities.  No PDF bytes or local cache paths are emitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Iterable, Mapping

import doc_store
import document_index


SCHEMA_VERSION = 1
DATASET = "ws12-document-assets"
DEFAULT_HARVEST = Path("var/ws12/manifest.jsonl")
DEFAULT_STORE_MANIFEST = Path("var/ws12/document-store-manifest.json")
DEFAULT_OUTPUT = Path("var/ws12/document-assets.json")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DocumentAssetError(ValueError):
    """A lineage input or normalized catalog violates the WS12 contract."""


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DocumentAssetError(f"cannot encode canonical asset catalog: {exc}") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _strict_json(raw: str, label: str) -> object:
    try:
        return json.loads(
            raw, parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise DocumentAssetError(f"{label} is not strict JSON: {exc}") from exc


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _canonical_source_url(value: str) -> str:
    """Normalize transport/``www`` aliases only for drift detection."""
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/+", "/", urllib.parse.unquote(parsed.path or "/"))
    return urllib.parse.urlunsplit(("https", host + port, path,
                                    parsed.query, ""))


def read_harvest_manifest(path: str | os.PathLike[str]) -> list[document_index.ManifestRow]:
    """Read strict JSONL and apply the Part B fail-closed row parser."""
    rows: list[document_index.ManifestRow] = []
    try:
        with open(path, encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                value = _strict_json(line, f"harvest manifest line {line_number}")
                if not isinstance(value, dict):
                    raise DocumentAssetError(
                        f"harvest manifest line {line_number} must be an object")
                # Research copies are truthfully non-public-domain and are
                # excluded from the public-domain lineage catalog by policy.
                if document_index._is_research_copy_row(value):
                    continue
                try:
                    rows.append(document_index.parse_manifest_row(
                        value, Path(path).resolve().parent))
                except document_index.DocumentIndexError as exc:
                    raise DocumentAssetError(
                        f"harvest manifest line {line_number}: {exc}") from exc
    except OSError as exc:
        raise DocumentAssetError(f"cannot read harvest manifest {path}: {exc}") from exc
    return rows


def _new_asset(doc_id: str, byte_count: int) -> dict:
    return {
        "doc_id": doc_id,
        "bytes": byte_count,
        "content_type": "application/pdf",
        "lineage_sources": set(),
        "source_ids": set(),
        "rights": {"public_domain": True, "paywalled": False, "bases": set()},
        "raw": {
            "status": "pending", "key": None, "sha256": doc_id,
            "bytes": byte_count, "archive_s3_uris": set(),
        },
        "searchable": {
            "status": "pending", "key": None, "sha256": None, "bytes": None,
        },
    }


def _asset(assets: dict[str, dict], doc_id: str, byte_count: int,
           *, public_domain: object, paywalled: object, rights_basis: object,
           lineage: str) -> dict:
    if not isinstance(doc_id, str) or SHA_RE.fullmatch(doc_id) is None:
        raise DocumentAssetError("asset doc_id must be a lowercase raw SHA-256")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
        raise DocumentAssetError(f"asset {doc_id} has an invalid byte count")
    # A missing/unknown/contradictory right is never promoted by inheritance.
    if public_domain is not True or paywalled is not False:
        raise DocumentAssetError(
            f"asset {doc_id} is not affirmatively public-domain and non-paywalled")
    basis = str(rights_basis or "").strip()
    if len(basis) < 8:
        raise DocumentAssetError(f"asset {doc_id} lacks an affirmative rights basis")
    row = assets.setdefault(doc_id, _new_asset(doc_id, byte_count))
    if row["bytes"] != byte_count or row["raw"]["bytes"] != byte_count:
        raise DocumentAssetError(
            f"asset {doc_id} has conflicting raw byte counts "
            f"{row['bytes']} and {byte_count}")
    row["lineage_sources"].add(lineage)
    row["rights"]["bases"].add(basis)
    return row


def _merge_occurrence(target: dict, *, titles: Iterable[object] = (),
                      states: Iterable[object] = (), counties: Iterable[object] = (),
                      trs: Iterable[object] = (), doc_dates: Iterable[object] = (),
                      doc_types: Iterable[object] = (),
                      retrieval_dates: Iterable[object] = (),
                      archive_s3_uris: Iterable[object] = (),
                      catalog_urls: Iterable[object] = (),
                      etags: Iterable[object] = (),
                      last_modified: Iterable[object] = ()) -> None:
    for name, values in (
        ("titles", titles), ("states", states), ("counties", counties),
        ("trs", trs), ("doc_dates", doc_dates), ("doc_types", doc_types),
        ("retrieval_dates", retrieval_dates),
        ("archive_s3_uris", archive_s3_uris),
        ("catalog_urls", catalog_urls), ("etags", etags),
        ("last_modified", last_modified),
    ):
        target[name].update(str(value).strip() for value in values if value)


def _occurrence(occurrences: dict[str, dict], *, doc_id: str, origin: str,
                portal_id: str, portal_source: str, source_url: str) -> dict:
    occurrence_id = _stable_id(
        "occ", doc_id, portal_id, portal_source, source_url)
    row = occurrences.setdefault(occurrence_id, {
        "occurrence_id": occurrence_id,
        "doc_id": doc_id,
        "origin": origin,
        "portal_id": portal_id,
        "portal_source": portal_source,
        "source_url": source_url,
        "titles": set(), "states": set(), "counties": set(), "trs": set(),
        "doc_dates": set(), "doc_types": set(), "retrieval_dates": set(),
        "archive_s3_uris": set(), "catalog_urls": set(), "etags": set(),
        "last_modified": set(),
    })
    if row["origin"] != origin:
        raise DocumentAssetError(
            f"source occurrence {occurrence_id} has conflicting origins")
    return row


def _mine_link(links: dict[str, dict], *, doc_id: str, namespace: str,
               mine_id: str, state: str, method: str, labels: Iterable[object],
               occurrence_ids: Iterable[str]) -> None:
    link_id = _stable_id("mine", doc_id, namespace, state, mine_id)
    row = links.setdefault(link_id, {
        "link_id": link_id, "doc_id": doc_id, "mine_namespace": namespace,
        "mine_id": mine_id, "state": state, "link_methods": set(),
        "labels": set(), "source_occurrence_ids": set(),
    })
    row["link_methods"].add(method)
    row["labels"].update(str(value).strip() for value in labels if value)
    row["source_occurrence_ids"].update(occurrence_ids)


def _as_manifest_row(value: object) -> document_index.ManifestRow:
    if isinstance(value, document_index.ManifestRow):
        return value
    if not isinstance(value, dict):
        raise DocumentAssetError("harvest row must be an object")
    try:
        return document_index.parse_manifest_row(value)
    except document_index.DocumentIndexError as exc:
        raise DocumentAssetError(str(exc)) from exc


def build_catalog(harvest_rows: Iterable[object], store_manifest: Mapping | None = None,
                  *, generated: str | None = None) -> dict:
    """Normalize harvest and addendum metadata into one deterministic catalog."""
    assets: dict[str, dict] = {}
    occurrences: dict[str, dict] = {}
    links: dict[str, dict] = {}
    harvest_dates: list[str] = []

    for value in harvest_rows:
        row = _as_manifest_row(value)
        asset = _asset(
            assets, row.document_id, row.byte_count,
            public_domain=row.public_domain, paywalled=row.paywalled,
            rights_basis=row.rights_basis, lineage="harvest",
        )
        asset["raw"]["status"] = "harvested"
        asset["raw"]["archive_s3_uris"].add(row.s3_uri)
        occurrence = _occurrence(
            occurrences, doc_id=row.document_id, origin="harvest",
            portal_id=row.portal_id, portal_source=row.portal_source,
            source_url=row.source_url,
        )
        _merge_occurrence(
            occurrence, titles=(row.title,), states=(row.state,),
            counties=(row.county,), trs=row.trs, doc_dates=(row.doc_date,),
            doc_types=(row.doc_type,), retrieval_dates=(row.retrieval_date,),
            archive_s3_uris=(row.s3_uri,), etags=(row.etag,),
            last_modified=(row.last_modified,),
        )
        _mine_link(
            links, doc_id=row.document_id,
            namespace=f"portal:{row.portal_id}", mine_id=row.mine_id,
            state=row.state, method="harvest_exact", labels=row.mine_names,
            occurrence_ids=(occurrence["occurrence_id"],),
        )
        harvest_dates.append(row.retrieval_date)

    normalized_store = None
    if store_manifest is not None:
        try:
            normalized_store = doc_store.validate_manifest(dict(store_manifest))
        except doc_store.DocStoreError as exc:
            raise DocumentAssetError(f"document-store manifest: {exc}") from exc
        occurrence_urls: dict[str, set[str]] = {}
        for occurrence in occurrences.values():
            occurrence_urls.setdefault(
                _canonical_source_url(occurrence["source_url"]), set()).add(
                    occurrence["doc_id"])
        for document in normalized_store["documents"]:
            url_owners = occurrence_urls.get(
                _canonical_source_url(document["source_url"]), set())
            if url_owners and document["doc_id"] not in url_owners:
                raise DocumentAssetError(
                    f"document-store source URL {document['source_url']} is "
                    f"harvested as {sorted(url_owners)}, not raw SHA "
                    f"{document['doc_id']}; the addendum identity is stale")
            rights = document["rights"]
            asset = _asset(
                assets, document["doc_id"], document["raw"]["bytes"],
                public_domain=rights["public_domain"],
                paywalled=rights["paywalled"], rights_basis=rights["basis"],
                lineage="addendum_store",
            )
            if document["raw"]["sha256"] != document["doc_id"]:
                raise DocumentAssetError(
                    f"store raw SHA does not equal doc_id {document['doc_id']}")
            asset["source_ids"].update(document["source_ids"])
            asset["raw"].update({
                "status": "store_ready", "key": document["raw"]["key"],
                "sha256": document["raw"]["sha256"],
                "bytes": document["raw"]["bytes"],
            })
            asset["searchable"].update({
                "status": "store_ready", "key": document["searchable"]["key"],
                "sha256": document["searchable"]["sha256"],
                "bytes": document["searchable"]["bytes"],
            })

            document_occurrences = [
                item["occurrence_id"] for item in occurrences.values()
                if item["doc_id"] == document["doc_id"]
            ]
            # Preserve the legacy pilot only where Part A has no authoritative
            # occurrence for these bytes.  In particular, IF0126 comes solely
            # from manifest.jsonl even though the old registry also names it.
            if not document_occurrences:
                occurrence = _occurrence(
                    occurrences, doc_id=document["doc_id"], origin="legacy_pilot",
                    portal_id=document["portal"], portal_source="legacy_pilot",
                    source_url=document["source_url"],
                )
                _merge_occurrence(
                    occurrence, titles=(document["title"],),
                    states=(document["state"],),
                    retrieval_dates=(document["retrieved"],),
                    catalog_urls=(document["catalog_url"],),
                )
                document_occurrences = [occurrence["occurrence_id"]]
            for subject in document["subjects"]:
                _mine_link(
                    links, doc_id=document["doc_id"], namespace="internal",
                    mine_id=subject["mine_id"], state=subject["state"],
                    method="addendum_subject", labels=(subject["label"],),
                    occurrence_ids=document_occurrences,
                )

    if not assets:
        raise DocumentAssetError("cannot build an empty document-asset catalog")
    date_candidates = harvest_dates[:]
    if normalized_store is not None:
        date_candidates.append(normalized_store["generated"])
    generated = generated or max(date_candidates)
    if not isinstance(generated, str) or DATE_RE.fullmatch(generated) is None:
        raise DocumentAssetError("generated must be YYYY-MM-DD")

    asset_rows = []
    for doc_id in sorted(assets):
        row = assets[doc_id]
        row["lineage_sources"] = sorted(row["lineage_sources"])
        row["source_ids"] = sorted(row["source_ids"])
        row["rights"]["bases"] = sorted(row["rights"]["bases"])
        row["raw"]["archive_s3_uris"] = sorted(row["raw"]["archive_s3_uris"])
        asset_rows.append(row)
    occurrence_rows = []
    for occurrence_id in sorted(occurrences):
        row = occurrences[occurrence_id]
        for name in (
            "titles", "states", "counties", "trs", "doc_dates", "doc_types",
            "retrieval_dates", "archive_s3_uris", "catalog_urls", "etags",
            "last_modified",
        ):
            row[name] = sorted(row[name])
        occurrence_rows.append(row)
    link_rows = []
    for link_id in sorted(links):
        row = links[link_id]
        row["link_methods"] = sorted(row["link_methods"])
        row["labels"] = sorted(row["labels"])
        row["source_occurrence_ids"] = sorted(row["source_occurrence_ids"])
        link_rows.append(row)

    metrics = {
        "assets": len(asset_rows),
        "harvest_assets": sum("harvest" in row["lineage_sources"] for row in asset_rows),
        "legacy_pilot_assets": sum(
            "harvest" not in row["lineage_sources"] for row in asset_rows),
        "source_occurrences": len(occurrence_rows),
        "mine_links": len(link_rows),
        "raw_store_ready": sum(row["raw"]["status"] == "store_ready"
                               for row in asset_rows),
        "searchable_store_ready": sum(
            row["searchable"]["status"] == "store_ready" for row in asset_rows),
        "searchable_pending": sum(
            row["searchable"]["status"] == "pending" for row in asset_rows),
    }
    catalog = {
        "schema_version": SCHEMA_VERSION, "dataset": DATASET,
        "generated": generated, "metrics": metrics, "assets": asset_rows,
        "source_occurrences": occurrence_rows, "mine_links": link_rows,
    }
    return validate_catalog(catalog)


def _expect_keys(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise DocumentAssetError(f"{label} must be an object")
    if set(value) != expected:
        raise DocumentAssetError(
            f"{label} keys mismatch: missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}")
    return value


def _string_list(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (nonempty and not value) or
            any(not isinstance(item, str) or not item.strip() for item in value)):
        raise DocumentAssetError(f"{label} must be a sorted string array")
    if value != sorted(set(value)):
        raise DocumentAssetError(f"{label} must be sorted and deduplicated")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DocumentAssetError(f"{label} must be non-empty trimmed text")
    return value


def _http_url(value: object, label: str) -> str:
    text = _text(value, label)
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DocumentAssetError(f"{label} must be an HTTP(S) URL")
    return text


def validate_catalog(catalog: object) -> dict:
    """Validate referential integrity and deterministic normalized ordering."""
    top = _expect_keys(catalog, {
        "schema_version", "dataset", "generated", "metrics", "assets",
        "source_occurrences", "mine_links",
    }, "asset catalog")
    if top["schema_version"] != SCHEMA_VERSION or top["dataset"] != DATASET:
        raise DocumentAssetError("unsupported document-asset catalog version or dataset")
    if not isinstance(top["generated"], str) or DATE_RE.fullmatch(top["generated"]) is None:
        raise DocumentAssetError("asset catalog.generated must be YYYY-MM-DD")
    if not isinstance(top["assets"], list) or not top["assets"]:
        raise DocumentAssetError("asset catalog.assets must be non-empty")

    assets: dict[str, dict] = {}
    for index, value in enumerate(top["assets"]):
        label = f"asset catalog.assets[{index}]"
        row = _expect_keys(value, {
            "doc_id", "bytes", "content_type", "lineage_sources", "source_ids",
            "rights", "raw", "searchable",
        }, label)
        doc_id = row["doc_id"]
        if not isinstance(doc_id, str) or SHA_RE.fullmatch(doc_id) is None:
            raise DocumentAssetError(f"{label}.doc_id must be a lowercase SHA-256")
        if doc_id in assets:
            raise DocumentAssetError(f"duplicate asset {doc_id}")
        if (not isinstance(row["bytes"], int) or isinstance(row["bytes"], bool)
                or row["bytes"] <= 0 or row["content_type"] != "application/pdf"):
            raise DocumentAssetError(f"{label} has invalid bytes/content_type")
        _string_list(row["lineage_sources"], f"{label}.lineage_sources", nonempty=True)
        _string_list(row["source_ids"], f"{label}.source_ids")
        rights = _expect_keys(
            row["rights"], {"public_domain", "paywalled", "bases"}, f"{label}.rights")
        if rights["public_domain"] is not True or rights["paywalled"] is not False:
            raise DocumentAssetError(f"{label}.rights is not affirmatively cleared")
        _string_list(rights["bases"], f"{label}.rights.bases", nonempty=True)
        raw = _expect_keys(row["raw"], {
            "status", "key", "sha256", "bytes", "archive_s3_uris",
        }, f"{label}.raw")
        if raw["status"] not in ("harvested", "store_ready"):
            raise DocumentAssetError(f"{label}.raw.status is invalid")
        if raw["sha256"] != doc_id or raw["bytes"] != row["bytes"]:
            raise DocumentAssetError(f"{label}.raw identity differs from the asset")
        archive_uris = _string_list(
            raw["archive_s3_uris"], f"{label}.raw.archive_s3_uris")
        if any(not uri.startswith("s3://") for uri in archive_uris):
            raise DocumentAssetError(f"{label}.raw archive URI must use s3://")
        if raw["status"] == "store_ready":
            if (not isinstance(raw["key"], str) or
                    not raw["key"].endswith(f"/{doc_id}/raw.pdf")):
                raise DocumentAssetError(f"{label}.raw store key is missing")
        elif raw["key"] is not None:
            raise DocumentAssetError(f"{label}.raw pending store key must be null")
        searchable = _expect_keys(row["searchable"], {
            "status", "key", "sha256", "bytes",
        }, f"{label}.searchable")
        if searchable["status"] == "store_ready":
            if (not isinstance(searchable["key"], str) or
                    not searchable["key"].endswith(
                        f"/{doc_id}/searchable.pdf") or
                    not isinstance(searchable["sha256"], str) or
                    SHA_RE.fullmatch(searchable["sha256"]) is None or
                    not isinstance(searchable["bytes"], int) or
                    isinstance(searchable["bytes"], bool) or searchable["bytes"] <= 0):
                raise DocumentAssetError(f"{label}.searchable store identity is invalid")
        elif searchable != {"status": "pending", "key": None,
                            "sha256": None, "bytes": None}:
            raise DocumentAssetError(f"{label}.searchable pending state is invalid")
        if ((raw["status"] == "store_ready") !=
                (searchable["status"] == "store_ready")):
            raise DocumentAssetError(
                f"{label} must make raw and searchable store readiness atomic")
        if raw["status"] == "store_ready" and (
                raw["key"].rsplit("/", 1)[0] !=
                searchable["key"].rsplit("/", 1)[0]):
            raise DocumentAssetError(
                f"{label} raw and searchable keys must share a document directory")
        assets[doc_id] = row
    if [row["doc_id"] for row in top["assets"]] != sorted(assets):
        raise DocumentAssetError("asset catalog.assets must be sorted by doc_id")

    occurrences: dict[str, dict] = {}
    occurrence_array_fields = {
        "titles", "states", "counties", "trs", "doc_dates", "doc_types",
        "retrieval_dates", "archive_s3_uris", "catalog_urls", "etags",
        "last_modified",
    }
    for index, value in enumerate(top["source_occurrences"]):
        label = f"asset catalog.source_occurrences[{index}]"
        row = _expect_keys(value, {
            "occurrence_id", "doc_id", "origin", "portal_id", "portal_source",
            "source_url", *occurrence_array_fields,
        }, label)
        if row["doc_id"] not in assets:
            raise DocumentAssetError(f"{label} references an unknown asset")
        expected = _stable_id(
            "occ", row["doc_id"], row["portal_id"], row["portal_source"],
            row["source_url"])
        if row["occurrence_id"] != expected or expected in occurrences:
            raise DocumentAssetError(f"{label}.occurrence_id is invalid or duplicated")
        if row["origin"] not in ("harvest", "legacy_pilot"):
            raise DocumentAssetError(f"{label}.origin is invalid")
        _text(row["portal_id"], f"{label}.portal_id")
        _text(row["portal_source"], f"{label}.portal_source")
        _http_url(row["source_url"], f"{label}.source_url")
        for name in occurrence_array_fields:
            _string_list(
                row[name], f"{label}.{name}",
                nonempty=name in ("titles", "states", "retrieval_dates"))
        if any(re.fullmatch(r"[A-Z]{2}", state) is None for state in row["states"]):
            raise DocumentAssetError(f"{label}.states contains an invalid state")
        if any(DATE_RE.fullmatch(value) is None for value in row["retrieval_dates"]):
            raise DocumentAssetError(
                f"{label}.retrieval_dates contains an invalid date")
        if any(not uri.startswith("s3://") for uri in row["archive_s3_uris"]):
            raise DocumentAssetError(
                f"{label}.archive_s3_uris contains a non-S3 URI")
        occurrences[expected] = row
    if [row["occurrence_id"] for row in top["source_occurrences"]] != sorted(occurrences):
        raise DocumentAssetError("source occurrences must be sorted by occurrence_id")

    links: dict[str, dict] = {}
    for index, value in enumerate(top["mine_links"]):
        label = f"asset catalog.mine_links[{index}]"
        row = _expect_keys(value, {
            "link_id", "doc_id", "mine_namespace", "mine_id", "state",
            "link_methods", "labels", "source_occurrence_ids",
        }, label)
        if row["doc_id"] not in assets:
            raise DocumentAssetError(f"{label} references an unknown asset")
        _text(row["mine_namespace"], f"{label}.mine_namespace")
        _text(row["mine_id"], f"{label}.mine_id")
        if not isinstance(row["state"], str) or re.fullmatch(
                r"[A-Z]{2}", row["state"]) is None:
            raise DocumentAssetError(f"{label}.state is invalid")
        expected = _stable_id(
            "mine", row["doc_id"], row["mine_namespace"], row["state"],
            row["mine_id"])
        if row["link_id"] != expected or expected in links:
            raise DocumentAssetError(f"{label}.link_id is invalid or duplicated")
        _string_list(row["link_methods"], f"{label}.link_methods", nonempty=True)
        _string_list(row["labels"], f"{label}.labels", nonempty=True)
        occurrence_ids = _string_list(
            row["source_occurrence_ids"], f"{label}.source_occurrence_ids",
            nonempty=True)
        for occurrence_id in occurrence_ids:
            occurrence = occurrences.get(occurrence_id)
            if occurrence is None or occurrence["doc_id"] != row["doc_id"]:
                raise DocumentAssetError(
                    f"{label} references an unknown or cross-document occurrence")
        links[expected] = row
    if [row["link_id"] for row in top["mine_links"]] != sorted(links):
        raise DocumentAssetError("mine links must be sorted by link_id")

    metrics = {
        "assets": len(assets),
        "harvest_assets": sum("harvest" in row["lineage_sources"]
                              for row in assets.values()),
        "legacy_pilot_assets": sum("harvest" not in row["lineage_sources"]
                                   for row in assets.values()),
        "source_occurrences": len(occurrences), "mine_links": len(links),
        "raw_store_ready": sum(row["raw"]["status"] == "store_ready"
                               for row in assets.values()),
        "searchable_store_ready": sum(
            row["searchable"]["status"] == "store_ready"
            for row in assets.values()),
        "searchable_pending": sum(row["searchable"]["status"] == "pending"
                                  for row in assets.values()),
    }
    if top["metrics"] != metrics:
        raise DocumentAssetError(
            f"asset catalog metrics do not reconcile: {top['metrics']} != {metrics}")
    return top


def write_catalog(catalog: Mapping, path: str | os.PathLike[str]) -> dict:
    normalized = validate_catalog(dict(catalog))
    raw = canonical_bytes(normalized)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, pending = tempfile.mkstemp(prefix=".document-assets-", dir=destination.parent)
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(pending, destination)
    except BaseException:
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise
    return {
        "path": str(destination), "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw), **normalized["metrics"],
    }


def load_store_manifest(path: str | os.PathLike[str]) -> dict:
    try:
        return doc_store.load_manifest(path, "document-store manifest")
    except doc_store.DocStoreError as exc:
        raise DocumentAssetError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest-manifest", default=DEFAULT_HARVEST)
    parser.add_argument("--document-store-manifest", default=DEFAULT_STORE_MANIFEST)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--generated", default=None)
    args = parser.parse_args(argv)
    try:
        catalog = build_catalog(
            read_harvest_manifest(args.harvest_manifest),
            load_store_manifest(args.document_store_manifest),
            generated=args.generated,
        )
        result = write_catalog(catalog, args.output)
    except (OSError, DocumentAssetError) as exc:
        parser.exit(1, f"document asset build failed: {exc}\n")
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
