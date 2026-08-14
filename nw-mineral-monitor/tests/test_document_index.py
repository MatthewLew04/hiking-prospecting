import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))
sys.path.insert(0, str(ROOT / "infra"))

import document_index as docs
import document_tools as runtime_docs


class FakeRunner:
    def __init__(self, pages):
        self.pages = tuple(pages)
        self.calls = 0

    def run(self, input_pdf, work_dir):
        self.calls += 1
        return docs.OCRResult(self.pages, engine="fixture-ocr", engine_version="1")


class FakeFallback:
    def __init__(self, text, confidence=0.98):
        self.text = text
        self.confidence = confidence
        self.pages = []

    def recognize(self, input_pdf, page, work_dir):
        self.pages.append(page)
        return docs.FallbackResult(self.text, "fixture-strong-ocr", self.confidence)


class DocumentIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / "original.pdf"
        self.original.write_bytes(b"%PDF-1.4\nfixture original bytes\n%%EOF\n")
        self.sha = hashlib.sha256(self.original.read_bytes()).hexdigest()
        self.db = self.root / "docs.sqlite3"
        self.connection = docs.connect(self.db)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def manifest_value(self, **changes):
        value = {
            "schema_version": 1,
            "source_url": "https://www.idahogeology.org/pub/IF0126/mils.pdf",
            "portal_id": "id_igs",
            "portal_source": "legacy",
            "mine_id": "IF0126",
            "mine_name": "Black Diamond Mine",
            "state": "ID",
            "county": "Owyhee",
            "trs": ["T15S R2W Sec 6"],
            "document_title": "MILS property record IF0126",
            "doc_date": "1975",
            "doc_type": "mine property record",
            "sha256": self.sha,
            "bytes": self.original.stat().st_size,
            "retrieval_date": "2026-08-14",
            "content_type": "application/pdf",
            "s3_uri": f"s3://mine-files/originals/id_igs/{self.sha[:2]}/{self.sha}.pdf",
            "local_path": str(self.original),
            "etag": "fixture",
            "last_modified": "2026-08-14T00:00:00Z",
            "public_domain": True,
            "paywalled": False,
            "rights_basis": "U.S. Geological Survey federal work",
        }
        value.update(changes)
        return value

    def ingest(self, **changes):
        row = docs.parse_manifest_row(self.manifest_value(**changes))
        return docs.ingest_manifest(self.connection, [row])

    def test_manifest_rights_fail_closed(self):
        for changes in ({"public_domain": None}, {"public_domain": False},
                        {"paywalled": None}, {"paywalled": True},
                        {"rights_basis": ""}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(docs.DocumentIndexError, "rights"):
                    docs.parse_manifest_row(self.manifest_value(**changes))

    def test_manifest_ingest_verifies_hash_and_is_idempotent(self):
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(first, {"found": 1, "downloaded": 1, "unchanged": 0})
        self.assertEqual(second, {"found": 1, "downloaded": 1, "unchanged": 1})
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM documents").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM document_sources").fetchone()[0], 1)

        self.original.write_bytes(b"changed")
        with self.assertRaisesRegex(docs.DocumentIndexError, "mismatch"):
            self.ingest()

    def test_same_hash_dedupes_ocr_but_preserves_both_sources(self):
        self.ingest()
        other = self.manifest_value(
            source_url="https://pubs.usgs.gov/report.pdf", portal_id="usgs_pubs",
            portal_source="citation_join", mine_id="MRDS-100", mine_name="Black Diamond")
        docs.ingest_manifest(self.connection, [docs.parse_manifest_row(other)])
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM documents").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM document_sources").fetchone()[0], 2)

    def test_source_variants_do_not_double_count_ocr_and_rich_title_wins(self):
        self.ingest()
        variant = self.manifest_value(
            source_url="https://www.idahogeology.org/pub/IF0126/mils-current.pdf",
            portal_source="current", document_title="mils-current.pdf",
            doc_date=None)
        docs.ingest_manifest(self.connection, [docs.parse_manifest_row(variant)])
        text = "Readable federal mine record with commodities and district evidence. " * 8
        docs.process_document(
            self.connection, self.sha, FakeRunner((docs.OCRPage(1, text),)),
            embedder=docs.HashEmbedder(8), work_root=self.root / "work",
            quality_threshold=0.2)
        status = {"schema_version": 1, "portals": [{
            "portal_id": "id_igs", "jurisdiction": "ID",
            "registry_status": "harvest_ready", "documents_found": 2,
            "documents_downloaded": 2, "tasks_pending": 0, "tasks_error": 0,
            "tasks_skipped": 0, "crawl_complete": False,
            "cursor_exhausted": False, "crawl_scope": "targeted:IF0126",
            "completion_blocker": {"reason": "robots_denied_http_403"},
            "completed_at": None, "manifest_rows": 2, "unique_hashes": 1,
        }]}
        row = docs.coverage(self.connection, status)["portals"][0]
        self.assertEqual(row["found"], 2)
        self.assertEqual(row["downloaded"], 2)
        self.assertEqual(row["ocrd"], 1)
        self.assertEqual(row["indexed"], 1)
        self.assertEqual(row["embedded"], 1)
        self.assertEqual(row["completion_blocker"]["reason"],
                         "robots_denied_http_403")
        dashboard = docs.coverage(self.connection, status)
        self.assertEqual(dashboard["totals"]["unique_documents"], 1)
        self.assertEqual(dashboard["totals"]["discovered_links"], 2)
        self.assertEqual(docs.public_index(self.connection)["documents"][0]["title"],
                         "MILS property record IF0126")
        listing = runtime_docs.execute(
            "docs_for", {"mine_id": "IF0126"}, db_path=str(self.db))
        self.assertEqual(listing["documents"][0]["title"],
                         "MILS property record IF0126")

    def test_federal_coverage_aggregates_document_states(self):
        self.ingest(portal_id="usgs_pubs", state="ID")
        status = {"schema_version": 1, "portals": [{
            "portal_id": "usgs_pubs", "jurisdiction": "federal",
            "registry_status": "registered_publication_catalog",
            "documents_found": 1, "documents_downloaded": 1,
            "tasks_pending": 0, "tasks_error": 0, "tasks_skipped": 0,
            "crawl_complete": False, "cursor_exhausted": False,
            "crawl_scope": "citation joins", "completed_at": None,
            "manifest_rows": 1, "unique_hashes": 1,
        }]}
        row = docs.coverage(self.connection, status)["portals"][0]
        self.assertEqual(row["state"], "FEDERAL")
        self.assertEqual(row["found"], 1)
        self.assertEqual(row["downloaded"], 1)

    def test_ocr_routes_weak_page_to_fallback_and_preserves_page_anchors(self):
        self.ingest()
        strong = ("The Black Diamond Mine produced copper ore from underground workings. "
                  "The report records development, shipments, and assay information for "
                  "the property in T15S R2W Sec 6, Owyhee County, Idaho. " * 5)
        fallback_text = ("Recovered microfilm page: production totaled 250 tons of copper "
                         "ore during 1954 and work continued in the lower adit. " * 5)
        runner = FakeRunner((docs.OCRPage(1, strong), docs.OCRPage(2, "x | ?")))
        fallback = FakeFallback(fallback_text)
        result = docs.process_document(
            self.connection, self.sha, runner, fallback, docs.HashEmbedder(16),
            self.root / "work", quality_threshold=0.62, target_chars=300,
            overlap_chars=40)
        self.assertEqual(result["status"], "indexed")
        self.assertEqual(result["pages"], 2)
        self.assertEqual(fallback.pages, [2])
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM fallback_queue").fetchone()[0], 0)
        page_two = self.connection.execute(
            "SELECT * FROM pages WHERE document_id=? AND page=2", (self.sha,)).fetchone()
        self.assertEqual(page_two["engine"], "fixture-strong-ocr")
        self.assertEqual(page_two["fallback_status"], "completed")
        self.assertEqual({row[0] for row in self.connection.execute(
            "SELECT DISTINCT page FROM chunks")}, {1, 2})
        for row in self.connection.execute("SELECT page,metadata_json FROM chunks"):
            metadata = json.loads(row["metadata_json"])
            self.assertEqual(metadata["page"], row["page"])
            self.assertEqual(metadata["mine_ids"], ["IF0126"])
            self.assertEqual(metadata["state"], "ID")
            self.assertIn("T15S R2W Sec 6", metadata["trs"])
        self.assertGreater(self.connection.execute(
            "SELECT count(*) FROM chunk_embeddings").fetchone()[0], 0)

        again = docs.process_document(
            self.connection, self.sha, runner, fallback, docs.HashEmbedder(16),
            self.root / "work")
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(runner.calls, 1)

    def test_unavailable_or_still_weak_fallback_does_not_claim_ocr_complete(self):
        self.ingest()
        runner = FakeRunner((docs.OCRPage(1, "garbled | x"),))
        result = docs.process_document(
            self.connection, self.sha, runner, None, None, self.root / "work",
            quality_threshold=0.9)
        self.assertEqual(result["status"], "needs_fallback")
        self.assertEqual(result["pending_fallback_pages"], 1)
        row = self.connection.execute(
            "SELECT ocr_completed_at,indexed_at FROM documents").fetchone()
        self.assertIsNone(row["ocr_completed_at"])
        self.assertIsNotNone(row["indexed_at"])
        self.assertIn(self.sha, docs.pending_documents(self.connection))
        dashboard = docs.coverage(self.connection)
        self.assertEqual(dashboard["totals"]["ocrd"], 0)
        self.assertEqual(dashboard["totals"]["indexed"], 1)
        self.assertEqual(dashboard["totals"]["pending_fallback"], 1)

    def test_exact_id_then_fuzzy_name_and_trs_identity_joins(self):
        self.ingest()
        records = [
            docs.SiteRecord("stategeo:1", "ID", "Owyhee", ("Different name",), (),
                            ("IGS DD-1 IF0126",)),
            docs.SiteRecord("our:black-diamond", "ID", "Owyhee",
                            ("Black Diamond property",), ("T15S R2W Sec 6",),
                            ("OUR-44",)),
        ]
        docs.import_site_records(self.connection, records)
        result = docs.link_identities(self.connection)
        self.assertEqual(result["exact"], 1)
        links = self.connection.execute(
            "SELECT site_id,method FROM document_site_links").fetchall()
        self.assertEqual([(row[0], row[1]) for row in links],
                         [("stategeo:1", "exact_id")])

        self.connection.execute("DELETE FROM site_records WHERE site_id='stategeo:1'")
        self.connection.commit()
        result = docs.link_identities(self.connection)
        self.assertEqual(result["fuzzy"], 1)
        link = self.connection.execute(
            "SELECT site_id,method FROM document_site_links").fetchone()
        self.assertEqual(tuple(link), ("our:black-diamond", "fuzzy_name_trs"))

    def test_search_is_bounded_and_every_hit_has_title_page_url(self):
        self.ingest()
        page = ("The report states that the Black Diamond lower adit exposed copper ore "
                "and that recorded production was 250 tons during 1954. " * 20)
        docs.process_document(
            self.connection, self.sha, FakeRunner((docs.OCRPage(1, page),)),
            embedder=docs.HashEmbedder(8), work_root=self.root / "work",
            quality_threshold=0.2, target_chars=260, overlap_chars=30)
        result = docs.search_documents(
            self.connection, "What production did the lower adit have?",
            mine_id="IF0126", limit=50, max_excerpt_chars=5000)
        docs.validate_citations(result)
        self.assertLessEqual(result["count"], 12)
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            self.assertLessEqual(len(hit["excerpt"]), 1002)
            self.assertEqual(hit["citation"]["page"], 1)
            self.assertEqual(hit["citation"]["document_title"],
                             "MILS property record IF0126")
            self.assertEqual(hit["citation"]["source_url"],
                             self.manifest_value()["source_url"])

    def test_public_outputs_exclude_ocr_and_private_storage_paths(self):
        self.ingest()
        text = "Black Diamond Mine public property record with enough readable words. " * 12
        docs.process_document(
            self.connection, self.sha, FakeRunner((docs.OCRPage(1, text),)),
            embedder=docs.HashEmbedder(8), work_root=self.root / "work",
            quality_threshold=0.2)
        index_path, coverage_path = self.root / "index.json", self.root / "coverage.json"
        docs.export_public(self.connection, index_path, coverage_path)
        index_raw = index_path.read_text()
        index = json.loads(index_raw)
        self.assertNotIn(text[:30], index_raw)
        self.assertNotIn(str(self.original), index_raw)
        self.assertNotIn("s3://", index_raw)
        self.assertEqual(index["documents"][0]["page_count"], 1)
        self.assertEqual(index["documents"][0]["indexed_pages"], 1)
        self.assertEqual(index["by_mine"]["IF0126"], [self.sha])
        dashboard = json.loads(coverage_path.read_text())
        self.assertEqual(dashboard["portals"][0]["embedded"], 1)

    def test_coverage_keeps_probed_empty_and_unknown_registry_rows_explicit(self):
        self.ingest()
        status = {"schema_version": 1, "portals": [
            {"portal_id": "id_igs", "jurisdiction": "ID",
             "registry_status": "confirmed", "documents_found": 1,
             "documents_downloaded": 1, "tasks_pending": 0, "tasks_error": 0,
             "tasks_skipped": 0, "crawl_complete": True, "cursor_exhausted": True,
             "crawl_scope": "all ids", "completed_at": "2026-08-14T00:00:00Z",
             "manifest_rows": 1, "unique_hashes": 1},
            {"portal_id": "pa_phummis", "jurisdiction": "PA",
             "registry_status": "probed", "documents_found": 0,
             "documents_downloaded": 0, "tasks_pending": 0, "tasks_error": 0,
             "tasks_skipped": 0, "crawl_complete": False, "cursor_exhausted": False,
             "crawl_scope": "probe only", "completed_at": None,
             "manifest_rows": 0, "unique_hashes": 0},
            {"portal_id": "empty_complete", "jurisdiction": "SD",
             "registry_status": "probed_empty", "documents_found": 0,
             "documents_downloaded": 0, "tasks_pending": 0, "tasks_error": 0,
             "tasks_skipped": 0, "crawl_complete": True, "cursor_exhausted": True,
             "crawl_scope": "full", "completed_at": "2026-08-14T00:00:00Z",
             "manifest_rows": 0, "unique_hashes": 0},
        ]}
        dashboard = docs.coverage(self.connection, status)
        by_portal = {row["portal"]: row for row in dashboard["portals"]}
        self.assertIsNone(by_portal["pa_phummis"]["found"])
        self.assertIsNone(by_portal["pa_phummis"]["indexed"])
        self.assertFalse(by_portal["pa_phummis"]["cursor_exhausted"])
        self.assertEqual(by_portal["empty_complete"]["found"], 0)
        self.assertEqual(by_portal["empty_complete"]["indexed"], 0)
        self.assertEqual(dashboard["unknown_portals"], 1)

    def test_package_database_is_wal_free_and_self_contained(self):
        self.ingest()
        text = "Black Diamond public mine record with readable production words. " * 10
        docs.process_document(
            self.connection, self.sha, FakeRunner((docs.OCRPage(1, text),)),
            embedder=docs.HashEmbedder(8), work_root=self.root / "work",
            quality_threshold=0.2)
        output = self.root / "deploy" / "document-index.sqlite3"
        metadata = docs.package_database(self.connection, output)
        self.assertEqual(metadata["integrity_check"], "ok")
        self.assertEqual(metadata["sha256"], docs.sha256_file(output))
        self.assertEqual(metadata["bytes"], output.stat().st_size)
        self.assertFalse(Path(str(output) + "-wal").exists())
        immutable = sqlite3.connect(f"file:{output}?mode=ro&immutable=1", uri=True)
        try:
            self.assertEqual(immutable.execute(
                "SELECT count(*) FROM chunks").fetchone()[0], 1)
        finally:
            immutable.close()

    def test_runtime_adapter_reads_same_index_and_fails_closed_on_bad_citation(self):
        self.ingest()
        text = ("Black Diamond production totaled 250 tons on the lower adit level. " * 8)
        docs.process_document(
            self.connection, self.sha, FakeRunner((docs.OCRPage(1, text),)),
            embedder=docs.HashEmbedder(8), work_root=self.root / "work",
            quality_threshold=0.2)
        result = runtime_docs.execute(
            "search_documents", {"query": "production tons", "mine_id": "IF0126"},
            db_path=str(self.db))
        self.assertEqual(result["status"], "loaded")
        self.assertEqual(result["retrieval_mode"], "hybrid_fts_vector")
        self.assertEqual(result["embedding_model"], "local-hash-smoke-test-v1")
        self.assertEqual(result["hits"][0]["citation"]["page"], 1)
        listing = runtime_docs.execute(
            "docs_for", {"mine_id": "IF0126"}, db_path=str(self.db))
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["documents"][0]["page_count"], 1)

        metadata = json.loads(self.connection.execute(
            "SELECT metadata_json FROM chunks LIMIT 1").fetchone()[0])
        metadata["source_url"] = ""
        self.connection.execute("UPDATE chunks SET metadata_json=?", (json.dumps(metadata),))
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "resolvable"):
            runtime_docs.execute(
                "search_documents", {"query": "production"}, db_path=str(self.db))


if __name__ == "__main__":
    unittest.main()
