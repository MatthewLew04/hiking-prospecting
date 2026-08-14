import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))
sys.path.insert(0, str(ROOT / "infra"))

from spatial_store import (
    SpatialStore, _remove_store_for_replace, build_default_store,
    parse_scale_denominator,
)
import spatial_tools


SQUARE = [[[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0],
           [-1.0, 1.0], [-1.0, -1.0]]]


class SpatialStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.directory.name, "spatial.sqlite")
        self.store = SpatialStore(self.db)
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def add_layer(self, layer_id, kind, title, scale=None, **extra):
        self.store.register_layer(layer_id, kind, title, scale=scale, **extra)

    def test_geology_at_orders_exact_scales_and_preserves_complete_unit_fields(self):
        self.add_layer("coarse", "geology", "State compilation",
                       scale="1:500,000", citation="Compilation citation",
                       source_url="https://example.test/coarse", state="ID")
        self.add_layer("fine", "geology", "Detailed quadrangle",
                       scale="1:24,000", citation="Quad citation",
                       source_url="https://example.test/fine", state="ID")
        geometry = {"type": "Polygon", "coordinates": SQUARE}
        self.store.add_feature("coarse", "c1", geometry, {
            "unit_label": "Tv", "unit_name": "Volcanic rocks",
            "description": "Regional volcanic unit", "age": "Miocene",
            "lithology": "rhyolite",
        })
        self.store.add_feature("fine", "f1", geometry, {
            "sn": "Tlb", "nm": "Lower basalt", "de": "Stacked sheet flows",
            "age": "Miocene", "li": "Basalt",
        })
        result = self.store.geology_at(0, 0)
        self.assertEqual([row["scale_denominator"] for row in result["units"]],
                         [24_000, 500_000])
        self.assertEqual(result["finest"]["unit_symbol"], "Tlb")
        self.assertEqual(result["finest"]["description"], "Stacked sheet flows")
        self.assertEqual(result["finest"]["source_map"], "Detailed quadrangle")
        self.assertEqual(result["finest"]["source_url"], "https://example.test/fine")

    def test_jackson_raster_is_context_not_a_fabricated_24k_unit(self):
        point = (-120.783, 38.365)
        self.add_layer("sgmc-ca", "geology",
                       "Jennings (2010), Geologic Map of California",
                       scale="1:750,000", citation="Jennings, C.W., 2010",
                       source_url="https://doi.org/10.3133/ds1052", state="CA")
        geometry = {"type": "Polygon", "coordinates": [[
            [-120.9, 38.2], [-120.7, 38.2], [-120.7, 38.5],
            [-120.9, 38.5], [-120.9, 38.2]]]}
        self.store.add_feature("sgmc-ca", "jackson-j", geometry, {
            "unit_label": "J",
            "unit_name": "Jurassic marine rocks, unit 1",
            "description": "Major lithologies: Slate, Graywacke",
            "age": "Jurassic",
            "lithology": "Metamorphic and Sedimentary, undifferentiated",
            "source_ref": "Jennings, 2010, scale 1:750,000",
            "source_url": "https://doi.org/10.3133/ds1052",
        })
        self.store.register_raster(
            "quad:jackson-pgm-2019", kind="geology",
            title="Jackson PGM-19-01 (2019)",
            bounds=[-120.876049, 38.249917, -120.751045, 38.374913],
            source_url="https://ngmdb.usgs.gov/ngm-bin/pdp/download.pl?q=62208_108920_99",
            citation="Holland and O'Neal, 2019, CGS PGM-19-01",
            metadata={"source_scale": "1:24,000", "scale_denominator": 24000,
                      "limitation": "Native GIS unavailable publicly; raster context only."})
        result = self.store.geology_at(point[1], point[0])
        self.assertEqual(result["finest"]["unit_symbol"], "J")
        self.assertIn("Jurassic marine rocks", result["finest"]["unit_name"])
        self.assertIn("Slate", result["finest"]["description"])
        self.assertIn("Jennings", result["finest"]["source_citation"])
        self.assertEqual(result["finest"]["scale_denominator"], 750_000)
        context = result["higher_resolution_raster_context"][0]
        self.assertEqual(context["scale_denominator"], 24_000)
        self.assertEqual(context["unit_status"], "not_queryable_from_raster")
        self.assertNotEqual(result["finest"]["source_map"], context["source_map"])

    def test_claim_mine_fault_and_magnetic_queries_are_spatial_and_cited(self):
        claim_geometry = {"type": "Polygon", "coordinates": SQUARE}
        self.add_layer("claims", "claim", "BLM MLRS",
                       source_url="https://mlrs.blm.gov/s/", state="ID")
        self.store.add_feature("claims", "ID1", claim_geometry,
                               {"serial": "ID1", "name": "Alpha", "disp": "ACTIVE"})
        claim_result = self.store.claims_at(0, 0)
        self.assertEqual(claim_result["claims"][0]["relationship"],
                         "polygon_covers_point")
        self.assertTrue(claim_result["claims"][0]["exact_claim_geometry"])

        self.add_layer("mrds", "mine", "USGS MRDS",
                       source_url="https://mrdata.usgs.gov/mrds/", state="ID")
        self.store.add_feature("mrds", "M1",
                               {"type": "Point", "coordinates": [0.01, 0]},
                               {"id": "M1", "nm": "Mine One", "c": "Gold"})
        mines = self.store.mines_near(0, 0, 2_000)
        self.assertEqual(mines["count"], 1)
        self.assertGreater(mines["mines"][0]["distance_m"], 1_000)

        self.add_layer("faults", "fault", "Detailed fault map",
                       scale="1:24,000", citation="Fault source",
                       source_url="https://example.test/faults", state="ID")
        self.store.add_feature("faults", "F1", {
            "type": "LineString", "coordinates": [[-0.01, 0], [0.01, 0]]},
            {"id": "F1", "name": "Test fault", "fault_type": "normal"})
        faults = self.store.faults_near(0.001, 0, 1_000)
        self.assertEqual(faults["faults"][0]["scale_denominator"], 24_000)
        self.assertEqual(faults["faults"][0]["source_url"],
                         "https://example.test/faults")

        self.add_layer("surveys", "survey", "USGS airborne survey index")
        self.store.add_feature("surveys", "S1", claim_geometry,
                               {"id": "S1", "nm": "Modern survey", "yr": "2024",
                                "spacing": 0.2, "src": "earthmri"})
        self.add_layer("mag", "magnetic_sample", "Numeric aeromagnetic COG",
                       citation="Survey/grid provenance",
                       source_url="https://example.test/mag.tif")
        self.store.add_feature("mag", "sample-1",
                               {"type": "Point", "coordinates": [0, 0]},
                               {"value_nt": -12.5, "cell_size_m": 100,
                                "survey": "Modern survey", "survey_id": "S1"})
        magnetic = self.store.mag_at(0, 0)
        self.assertEqual(magnetic["value_nt"], -12.5)
        self.assertEqual(magnetic["status"], "sampled")
        self.assertEqual(magnetic["covering_surveys"][0]["survey_id"], "S1")

    def test_docs_for_uses_public_index_counts_and_fails_closed_before_load(self):
        missing = self.store.docs_for("IF0126")
        self.assertEqual(missing["status"], "not_loaded")
        self.assertIsNone(missing["count"])
        index_path = os.path.join(self.directory.name, "index.json")
        with open(index_path, "w", encoding="utf-8") as output:
            json.dump({
                "schema": "nwmm.ws12.docs.v1",
                "documents": [{
                    "document_id": "igs-1", "sha256": "a" * 64,
                    "title": "De Lamar mine property file", "portal": "id_igs",
                    "portal_id": "IF0126", "mine_ids": ["IF0126"],
                    "mine_names": ["De Lamar"], "state": "ID", "county": "Owyhee",
                    "trs": None, "doc_date": "1960", "doc_type": "property file",
                    "page_count": 12, "indexed_pages": 10, "chunks": 8,
                    "source_url": "https://example.test/property.pdf",
                }],
                "by_mine": {"IF0126": ["igs-1"]},
            }, output)
        self.store.ingest_document_index(index_path)
        result = self.store.docs_for("if0126")
        self.assertEqual(result["status"], "loaded")
        self.assertEqual(result["documents"][0]["page_count"], 12)
        self.assertEqual(result["documents"][0]["indexed_pages"], 10)
        self.assertNotIn("pages", result["documents"][0])

    def test_ws6_real_dwm193_is_queryable_with_source_scale_and_url(self):
        path = ROOT / "site/data/geology/delamar24k.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.store.ingest_ws6_geology(path, state="ID")
        coordinate = data["units"][0]["g"][0][0][0]
        result = self.store.geology_at(coordinate[1], coordinate[0])
        dwm = [row for row in result["units"] if row["source_id"] == "dwm193"]
        self.assertTrue(dwm)
        self.assertTrue(all(row["scale_denominator"] == 24_000 for row in dwm))
        self.assertTrue(all("idahogeology.org" in row["source_url"] for row in dwm))

    def test_real_pinned_jackson_sgmc_record_and_quad_inventory_stay_distinct(self):
        fixture = ROOT / "site/data/spatial/sgmc-jackson-16735.geojson"
        self.store.ingest_geojson(
            fixture, layer_id="national:geology:jackson-acceptance", kind="geology",
            title="Jennings (2010), Geologic Map of California",
            citation="Jennings et al. 2010, scale 1:750,000",
            source_url="https://ngmdb.usgs.gov/Prodesc/proddesc_96750.htm",
            scale="1:750,000", state="CA")
        inventory = json.loads((ROOT / "site/data/geology-quads/inventory.json").read_text())
        jackson = next(row for row in inventory["layers"]
                       if row["id"] == "jackson-pgm-2019")
        self.store.register_raster(
            "quad:jackson-pgm-2019", kind="geology", title=jackson["label"],
            bounds=jackson["raster"]["bounds"], citation=jackson["citation"],
            source_url=jackson["product_url"],
            metadata={"source_scale": jackson["scale"],
                      "limitation": "Native GIS unavailable publicly; raster context only."})
        result = self.store.geology_at(38.365, -120.783)
        self.assertEqual(result["finest"]["unit_symbol"], "J")
        self.assertEqual(
            result["finest"]["unit_name"],
            "Jurassic marine rocks, unit 1 (Western Sierra Nevada and Western Klamath Mountains)")
        self.assertEqual(
            result["finest"]["description"],
            "Jurassic marine rocks, unit 1 (Western Sierra Nevada and Western Klamath Mountains); "
            "major lithologies: Slate, Graywacke; minor lithologies: Siltstone, Conglomerate; "
            "incidental: Chert, Volcanic, Basalt")
        self.assertEqual(
            result["finest"]["lithology"],
            "Metamorphic and Sedimentary, undifferentiated")
        self.assertEqual(
            result["finest"]["age"],
            "Phanerozoic - Mesozoic - Jurassic - Late-Jurassic to "
            "Phanerozoic - Mesozoic - Triassic")
        self.assertEqual(result["finest"]["scale_denominator"], 750_000)
        self.assertIn("Jennings, C.W.", result["finest"]["source_citation"])
        self.assertEqual(
            result["finest"]["source_url"],
            "https://ngmdb.usgs.gov/Prodesc/proddesc_96750.htm")
        self.assertEqual(result["higher_resolution_raster_context"][0]
                         ["scale_denominator"], 24_000)
        self.assertEqual(result["higher_resolution_raster_context"][0]
                         ["unit_status"], "not_queryable_from_raster")

    def test_close_checkpoints_wal_and_leaves_one_deployable_file(self):
        path = os.path.join(self.directory.name, "checkpoint.sqlite")
        store = SpatialStore(path)
        store.initialize()
        store.register_layer("x", "mine", "fixture")
        store.add_feature("x", "1", {"type": "Point", "coordinates": [0, 0]}, {})
        store.close()
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.exists(path + "-wal"))
        self.assertFalse(os.path.exists(path + "-shm"))
        connection = __import__("sqlite3").connect(path)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        connection.close()

    def test_replace_removes_only_exact_database_sidecars(self):
        path = Path(self.directory.name) / "replace.sqlite"
        untouched = Path(self.directory.name) / "replace.sqlite.backup"
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"),
                          untouched):
            candidate.write_bytes(b"fixture")
        _remove_store_for_replace(path)
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-shm").exists())
        self.assertTrue(untouched.exists())


class SpatialContractTests(unittest.TestCase):
    def test_acceptance_scope_builds_named_geology_and_docs_without_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.sqlite"
            stats = build_default_store(ROOT, path, replace=True, scope="acceptance")
            self.assertGreater(stats["features"]["geology"], 0)
            self.assertGreater(stats["documents"], 0)
            self.assertFalse(Path(str(path) + "-wal").exists())
            self.assertFalse(Path(str(path) + "-shm").exists())
            with SpatialStore(path, read_only=True) as store:
                jackson = store.geology_at(38.365, -120.783)
                self.assertEqual(jackson["finest"]["unit_symbol"], "J")
                self.assertEqual(
                    jackson["higher_resolution_raster_context"][0]["unit_status"],
                    "not_queryable_from_raster")
                self.assertGreater(store.docs_for("IF0126")["count"], 0)

    def test_scale_parser_rejects_ambiguous_ranges(self):
        self.assertEqual(parse_scale_denominator("native 1:24,000"), 24_000)
        self.assertEqual(parse_scale_denominator("~1:500k"), 500_000)
        self.assertIsNone(parse_scale_denominator("1:24k-1:250k"))

    def test_runtime_adapter_reports_missing_database_as_not_loaded(self):
        result = spatial_tools.execute(
            "geology_at", {"lat": 0, "lon": 0},
            db_path=os.path.join(tempfile.gettempdir(), "definitely-not-ws12.sqlite"))
        self.assertEqual(result["status"], "not_loaded")
        self.assertIsNone(result["count"])

    def test_geoparquet_adapter_has_actionable_optional_dependency(self):
        if importlib.util.find_spec("pyarrow") is not None:
            self.skipTest("pyarrow is installed; integration export is environment-specific")
        from spatial_geoparquet import export_geoparquet
        with self.assertRaisesRegex(RuntimeError, "requires pyarrow"):
            export_geoparquet("missing.sqlite", tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()
