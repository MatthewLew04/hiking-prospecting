import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

import doc_store
import document_assets as assets
import build_doc_store as builder
from tests.test_ws12_doc_store import Fixture


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def harvest_row(doc_id, *, mine_id="IF0126", mine_name="St. Louis Mine",
                source_url="https://idahogeology.org/files/record.pdf",
                byte_count=1234, public_domain=True, paywalled=False,
                rights_basis="U.S. federal-government work under 17 U.S.C. 105."):
    return {
        "schema_version": 1,
        "source_url": source_url,
        "portal_id": "igs_mines",
        "portal_source": "legacy",
        "mine_id": mine_id,
        "mine_name": mine_name,
        "state": "ID",
        "county": "Butte",
        "trs": "T3N R24E Sec 15",
        "document_title": "Canonical harvested mine record",
        "doc_date": None,
        "doc_type": "USGS mine-record extract",
        "sha256": doc_id,
        "bytes": byte_count,
        "retrieval_date": "2026-08-14",
        "content_type": "application/pdf",
        "s3_uri": f"s3://private/ws12/originals/igs_mines/{doc_id}.pdf",
        "etag": "fixture",
        "last_modified": "Fri, 14 Aug 2026 00:00:00 GMT",
        "public_domain": public_domain,
        "paywalled": paywalled,
        "rights_basis": rights_basis,
    }


def store_document(doc_id, *, source_url, mine_id, title, byte_count=1234,
                   source_id="legacy-source", subject_mine=None,
                   public_domain=True):
    subject_mine = subject_mine or mine_id
    prefix = f"docs/ID/igs-mines/{mine_id}/{doc_id}"
    return {
        "doc_id": doc_id,
        "state": "ID",
        "portal": "igs-mines",
        "mine_id": mine_id,
        "title": title,
        "authority": "U.S. Geological Survey",
        "source_url": source_url,
        "catalog_url": "https://www.idahogeology.org/catalog/IF0126",
        "retrieved": "2026-08-14",
        "pages": 1,
        "pagination_preserved": True,
        "raw": {"key": f"{prefix}/raw.pdf", "sha256": doc_id,
                "bytes": byte_count},
        "searchable": {"key": f"{prefix}/searchable.pdf", "sha256": doc_id,
                       "bytes": byte_count},
        "text_layer": {"status": "native", "tool": "fixture native text",
                       "pages_with_text": 1, "characters": 100},
        "subjects": [{"state": "ID", "mine_id": subject_mine,
                      "label": "St. Louis Mine"}],
        "source_ids": [source_id],
        "rights": {
            "public_domain": public_domain,
            "paywalled": False,
            "basis": "U.S. federal-government work under 17 U.S.C. 105.",
        },
    }


def store_manifest(documents):
    value = {
        "schema_version": 1,
        "dataset": "ws12-document-store",
        "generated": "2026-08-14",
        "store": {
            "key_template": "docs/{state}/{portal}/{mine_id}/{sha256}/{variant}.pdf",
            "variants": ["raw", "searchable"],
            "public_prefix": False,
            "delivery": "presigned_get",
            "presign_ttl_seconds": 300,
            "raw_transition_days": 30,
            "raw_storage_class": "STANDARD_IA",
        },
        "metrics": doc_store.recompute_metrics(documents, []),
        "documents": documents,
        "citations": [],
    }
    return doc_store.validate_manifest(value)


class DocumentAssetBridgeTests(unittest.TestCase):
    def fixture(self):
        if_sha = digest("if0126")
        legacy_sha = digest("legacy pilot")
        store = store_manifest([
            store_document(
                if_sha,
                source_url="https://www.idahogeology.org/files/legacy-record.pdf",
                mine_id="stategeo-igs-dd-1-if0126",
                title="Legacy-declared title",
                source_id="igs-if0126",
            ),
            store_document(
                legacy_sha,
                source_url="https://pubs.usgs.gov/legacy-pilot.pdf",
                mine_id="statewide-id",
                title="Existing pilot report",
                source_id="usgs-existing",
            ),
        ])
        rows = [
            harvest_row(if_sha),
            # The same source occurrence can link one byte asset to another
            # portal mine without creating another document or occurrence.
            harvest_row(if_sha, mine_id="IF0999", mine_name="Alias Prospect"),
        ]
        return if_sha, legacy_sha, rows, store

    def test_harvest_is_authoritative_and_legacy_pilot_is_preserved(self):
        if_sha, legacy_sha, rows, store = self.fixture()
        catalog = assets.build_catalog(rows, store)
        self.assertEqual(catalog["metrics"]["assets"], 2)
        self.assertEqual(catalog["metrics"]["harvest_assets"], 1)
        self.assertEqual(catalog["metrics"]["legacy_pilot_assets"], 1)
        by_id = {row["doc_id"]: row for row in catalog["assets"]}
        self.assertEqual(by_id[if_sha]["lineage_sources"],
                         ["addendum_store", "harvest"])
        self.assertEqual(by_id[if_sha]["raw"]["status"], "store_ready")
        self.assertTrue(by_id[if_sha]["raw"]["key"].endswith("/raw.pdf"))
        self.assertEqual(by_id[if_sha]["searchable"]["status"], "store_ready")
        self.assertEqual(by_id[legacy_sha]["lineage_sources"], ["addendum_store"])

        occurrences = [row for row in catalog["source_occurrences"]
                       if row["doc_id"] == if_sha]
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["origin"], "harvest")
        self.assertEqual(occurrences[0]["source_url"], rows[0]["source_url"])
        self.assertNotIn("legacy-record.pdf", occurrences[0]["source_url"])
        legacy = [row for row in catalog["source_occurrences"]
                  if row["doc_id"] == legacy_sha]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["origin"], "legacy_pilot")

        portal_mines = {(row["mine_namespace"], row["mine_id"])
                        for row in catalog["mine_links"] if row["doc_id"] == if_sha}
        self.assertIn(("portal:igs_mines", "IF0126"), portal_mines)
        self.assertIn(("portal:igs_mines", "IF0999"), portal_mines)
        self.assertIn(("internal", "stategeo-igs-dd-1-if0126"), portal_mines)

    def test_merge_is_order_independent_and_exact_duplicates_dedupe(self):
        unused, unused_legacy, rows, store = self.fixture()
        first = assets.build_catalog(rows + [copy.deepcopy(rows[0])], store)
        second = assets.build_catalog(list(reversed(rows)), store)
        self.assertEqual(assets.canonical_bytes(first), assets.canonical_bytes(second))
        self.assertEqual(first["metrics"]["assets"], 2)
        self.assertEqual(first["metrics"]["source_occurrences"], 2)

    def test_same_hash_with_conflicting_bytes_fails(self):
        if_sha, unused, rows, store = self.fixture()
        bad = harvest_row(
            if_sha, source_url="https://idahogeology.org/files/second.pdf",
            byte_count=9999)
        with self.assertRaisesRegex(assets.DocumentAssetError,
                                    "conflicting raw byte counts"):
            assets.build_catalog(rows + [bad], store)

    def test_same_canonical_source_url_with_a_different_store_sha_fails(self):
        if_sha, unused, rows, store = self.fixture()
        stale = copy.deepcopy(store)
        stale_doc = next(row for row in stale["documents"] if row["doc_id"] == if_sha)
        stale_doc["source_url"] = "https://www.idahogeology.org/files/record.pdf"
        stale_doc["doc_id"] = digest("stale bytes")
        stale_doc["raw"]["sha256"] = stale_doc["doc_id"]
        stale_doc["raw"]["key"] = stale_doc["raw"]["key"].replace(
            if_sha, stale_doc["doc_id"])
        stale_doc["searchable"]["sha256"] = stale_doc["doc_id"]
        stale_doc["searchable"]["key"] = stale_doc["searchable"]["key"].replace(
            if_sha, stale_doc["doc_id"])
        stale["metrics"] = doc_store.recompute_metrics(
            stale["documents"], stale["citations"])
        with self.assertRaisesRegex(assets.DocumentAssetError, "identity is stale"):
            assets.build_catalog(rows, stale)

    def test_rights_fail_closed_for_harvest_and_store(self):
        if_sha, unused, rows, store = self.fixture()
        for changes in (
            {"public_domain": False}, {"public_domain": None},
            {"paywalled": True}, {"rights_basis": ""},
        ):
            with self.subTest(changes=changes):
                bad = harvest_row(if_sha)
                bad.update(changes)
                with self.assertRaisesRegex(assets.DocumentAssetError, "rights"):
                    assets.build_catalog([bad], store)
        unresolved = copy.deepcopy(store)
        unresolved["documents"][0]["rights"]["public_domain"] = False
        unresolved["metrics"] = doc_store.recompute_metrics(
            unresolved["documents"], unresolved["citations"])
        with self.assertRaisesRegex(assets.DocumentAssetError,
                                    "not affirmatively public-domain"):
            assets.build_catalog(rows, unresolved)

    def test_referential_validation_rejects_cross_document_mine_link(self):
        unused, unused_legacy, rows, store = self.fixture()
        catalog = assets.build_catalog(rows, store)
        broken = copy.deepcopy(catalog)
        target = next(row for row in broken["mine_links"]
                      if row["mine_namespace"] == "internal")
        wrong = next(row["occurrence_id"] for row in broken["source_occurrences"]
                     if row["doc_id"] != target["doc_id"])
        target["source_occurrence_ids"] = [wrong]
        with self.assertRaisesRegex(assets.DocumentAssetError, "cross-document"):
            assets.validate_catalog(broken)

    def test_jsonl_duplicate_keys_fail_and_catalog_write_is_canonical(self):
        if_sha, unused, rows, store = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.jsonl"
            path.write_text('{"sha256":"a","sha256":"b"}\n', encoding="utf-8")
            with self.assertRaisesRegex(assets.DocumentAssetError,
                                        "duplicate JSON object key"):
                assets.read_harvest_manifest(path)
            output = Path(temporary) / "private" / "assets.json"
            catalog = assets.build_catalog(rows, store)
            result = assets.write_catalog(catalog, output)
            raw = output.read_bytes()
            self.assertEqual(raw, assets.canonical_bytes(catalog))
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertNotIn(b"local_path", raw)
            self.assertNotIn(b".pdf%PDF", raw)

    def test_addendum_build_hook_emits_the_private_lineage_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            report = fixture.documents["report"]
            row = harvest_row(
                report["sha256"], byte_count=report["bytes"],
                source_url="https://official.example.test/files/report.pdf")
            harvest = Path(temporary) / "manifest.jsonl"
            harvest.write_text(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8")
            catalog_path = Path(temporary) / "var" / "ws12" / "document-assets.json"
            result = fixture.build(
                harvest_manifest_path=str(harvest),
                asset_catalog_path=str(catalog_path),
                require_harvest_lineage=True)
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            assets.validate_catalog(catalog)
            self.assertEqual(result["asset_catalog_assets"], 2)
            self.assertEqual(catalog["metrics"]["harvest_assets"], 1)
            self.assertEqual(catalog["metrics"]["legacy_pilot_assets"], 1)

    def test_explicit_missing_harvest_manifest_fails_before_pdf_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            called = []

            def probe(unused):
                called.append(True)
                raise AssertionError("PDF probe must not run")

            with self.assertRaisesRegex(
                    builder.DocumentBuildError, "canonical harvest manifest"):
                fixture.build(
                    harvest_manifest_path=str(Path(temporary) / "missing.jsonl"),
                    require_harvest_lineage=False, probe=probe)
            self.assertEqual(called, [])


class ShippedDocumentAssetLineageTests(unittest.TestCase):
    def test_harvest_bridge_uses_the_sanitized_private_manifest_fixture(self):
        store = doc_store.load_manifest(
            ROOT / "tests" / "fixtures" / "ws12_document_store_manifest.json")
        rows = [harvest_row(
            document["doc_id"], source_url=document["source_url"],
            byte_count=document["raw"]["bytes"],
            rights_basis=document["rights"]["basis"])
            for document in store["documents"]]
        catalog = assets.build_catalog(rows, store)
        self.assertEqual(catalog["metrics"]["assets"], 2)
        self.assertEqual(catalog["metrics"]["raw_store_ready"], 2)
        self.assertEqual(catalog["metrics"]["searchable_store_ready"], 2)
        if_ids = {row["sha256"] for row in rows}
        if_occurrences = [row for row in catalog["source_occurrences"]
                          if row["doc_id"] in if_ids]
        self.assertEqual(len(if_occurrences), 2)
        self.assertEqual({row["origin"] for row in if_occurrences}, {"harvest"})
        self.assertTrue(all(row["portal_id"] == "igs_mines"
                            for row in if_occurrences))

    def test_public_if0126_index_is_minimized_but_keeps_both_hashes(self):
        with open(ROOT / "site" / "data" / "docs" / "index.json",
                  encoding="utf-8") as source:
            index = json.load(source)
        expected = {
            "d29aab7b4e9fcde0e084dddc84ef9da37d0c15860af4674bf58bd0decd71e07f",
            "3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1",
        }
        self.assertEqual(set(index["by_mine"]["IF0126"]), expected)
        for document in index["documents"]:
            self.assertNotIn("raw", document)
            self.assertNotIn("searchable", document)
            self.assertNotIn("rights", document)
            self.assertNotIn("quote", document)


if __name__ == "__main__":
    unittest.main()
