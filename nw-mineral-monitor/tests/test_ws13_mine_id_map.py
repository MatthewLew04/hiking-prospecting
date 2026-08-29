"""The front-end -> corpus mine-id bridge, driven without a database.

pipelines/ws13_mine_id_map.py had no test file at all, which is how a table
whose own header states its reader query -- ws13_mine_id_all, and (verified OR
confidence >= 0.8) -- ended up with a reader that implemented neither, and how
two thirds of the corpus stayed unreachable without anything going red.

Everything here runs against an in-memory CorpusIndex built from tuples of the
shape load_corpus() SELECTs, so the derivation tiers, the place tiers and the
schema constants are all exercised with no VPC, no Postgres and no AWS. What
cannot run here is the upsert; write_rows() needs a real connection, and the
SQL it sends is asserted as text instead.

The fixtures are the real failure shapes, named for what they are:

  Nevada     NBMG files a mining-district document under the district name and
             numbers it from 10000001; MRDS numbers a Nevada deposit from
             10006806 and records its district. 52 of those integers collide.
  Arizona    AZGS files a collection under a document title ('Cuprite Mine
             Area Total Magnetic Intensity Record') and an ADMM id that
             appears in no map layer. The mine name is inside the title.
  Idaho      The case that already worked, kept so the place tiers can be
             shown not to have moved it.
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

# psycopg is a deployment dependency of the in-VPC host, not of the test host.
# The module imports it at the top and uses psycopg.Error in one except clause
# and Jsonb in write_rows(); neither is reached by anything here.
if "psycopg" not in sys.modules:
    try:
        import psycopg                                     # noqa: F401
    except ModuleNotFoundError:
        _stub = types.ModuleType("psycopg")
        _stub.Error = type("Error", (Exception,), {})
        _json = types.ModuleType("psycopg.types.json")
        _json.Jsonb = lambda value: value
        _types = types.ModuleType("psycopg.types")
        _types.json = _json
        _stub.types = _types
        sys.modules["psycopg"] = _stub
        sys.modules["psycopg.types"] = _types
        sys.modules["psycopg.types.json"] = _json

import ws13_mine_id_map as bridge                           # noqa: E402


def document(sha256, state, mine_ids, mine_names, county=None,
             s3_key=None, source_url=None, doc_type="property_file"):
    """One ws13_documents row in the order load_corpus() SELECTs them."""
    return (sha256, s3_key or f"ws12/originals/{sha256[:2]}/{sha256}.pdf",
            state, list(mine_ids), list(mine_names), source_url, county,
            doc_type)


def district_file(sha256, state, mine_ids, mine_names, county=None):
    """An NBMG mining-district file: its mine_names ARE district names."""
    return document(sha256, state, mine_ids, mine_names, county,
                    doc_type="mining_district_file")


def corpus_of(*rows):
    return bridge.CorpusIndex(list(rows), source_url_available=True)


def record(front_end_id, **fields):
    fields.setdefault("source", "build-inputs/data/sites/mrds_nv.json")
    fields.setdefault("namespace", "mrds")
    fields.setdefault("record_id", front_end_id)
    fields.setdefault("name", None)
    fields.setdefault("state", "NV")
    fields.setdefault("id_form", "bare")
    return bridge.FrontEndRecord(front_end_id, **fields)


def derive(record_, corpus, min_ratio=0.72):
    return bridge.derive(record_, [record_], None, corpus, min_ratio)


NEVADA = corpus_of(
    district_file("a" * 64, "NV", ["10000001", "10000002"],
                  ["CAVE CREEK", "CAVE CREEK"], "ELKO"),
    district_file("b" * 64, "NV", ["10000003"], ["CAVE CREEK"], "ELKO"),
    district_file("c" * 64, "NV", ["900028"], ["ELKO COUNTY GENERAL"], "ELKO"),
    district_file("d" * 64, "NV", ["60000037"], ["OPALITE"], "HUMBOLDT"),
)

ARIZONA = corpus_of(
    document("e" * 64, "AZ", ["ADMM-1552462915154-298"],
             ["Emerald Isle Mine Assay Map East-West Sections"],
             " Mohave County"),
    document("f" * 64, "AZ", ["ADMM-1552461729245-218"],
             ["Cashier Group Claim Map"], " Mohave County"),
    document("g" * 64, "AZ", ["ADMM-1552448557289-145"],
             ["Defense Antimony"], " Maricopa County"),
)


class NameRunTests(unittest.TestCase):
    """What counts as a distinctive run, and what does not."""

    def test_generic_mining_words_are_not_keys(self):
        self.assertEqual(bridge.name_runs("the mine"), [])
        self.assertEqual(bridge.name_runs("claims group"), [])

    def test_a_single_short_word_is_not_a_key(self):
        """'Rand' is a real Mineral County district and 'no' is not a name;
        the floor is on letters, so one of them survives and one does not."""
        self.assertEqual(bridge.name_runs("no 2"), [])
        self.assertIn(("cuprite",), bridge.name_runs("cuprite"))

    def test_a_mine_name_is_found_inside_a_document_title(self):
        runs = bridge.name_runs(
            "cuprite mine area total magnetic intensity record")
        self.assertIn(("cuprite",), runs)
        # 'mine' and 'area' are both dropped before the runs are cut, so
        # 'Cuprite Mine' and the title above agree on the key ('cuprite',)
        # rather than on two keys that never meet.
        self.assertNotIn(("mine",), runs)
        self.assertNotIn(("area",), runs)
        self.assertIn(("cuprite", "total"), runs)

    def test_runs_are_capped_in_length(self):
        long_name = " ".join(f"token{index}" for index in range(12))
        self.assertTrue(all(len(run) <= bridge.RUN_MAX_TOKENS
                            for run in bridge.name_runs(long_name)))


class CountyTests(unittest.TestCase):
    def test_both_spellings_of_a_county_fold_together(self):
        self.assertEqual(bridge.normalize_county(" Maricopa County"),
                         bridge.normalize_county("Maricopa"))

    def test_a_record_on_a_county_line_names_both(self):
        self.assertEqual(bridge.county_keys("Pima, Santa Cruz"),
                         frozenset({"pima", "santa cruz"}))

    def test_silence_on_either_side_is_not_a_disagreement(self):
        """The corpus leaves county null on 1,542 Arizona documents and the
        Nevada map has no county column at all. Treating that as a mismatch
        would refuse the matches the tier exists to make."""
        self.assertTrue(bridge.counties_agree(frozenset(), ("mohave",)))
        self.assertTrue(bridge.counties_agree(frozenset({"mohave"}), ()))

    def test_a_real_disagreement_is_caught(self):
        self.assertFalse(
            bridge.counties_agree(frozenset({"yavapai"}), ("pima",)))


class SiteFileTests(unittest.TestCase):
    """load_site_id_file(), including the spelling the front end really uses."""

    def write(self, payload):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "mrds_nv.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_every_spelling_the_front_end_can_emit_is_enumerated(self):
        """ws12MinesNear() reads the bare id off the PMTiles feature and hands
        it to the model as mine_id; that is the spelling docs_for is called
        with, and it was the one spelling the table had no row for."""
        path = self.write({"src": "mrds", "state": "NV", "n": 1,
                           "id": ["10037553"], "nm": ["Washoe Claims"],
                           "d": ["Candelaria"]})
        records, empty = bridge.load_site_id_file(path)
        self.assertIsNone(empty)
        self.assertEqual(sorted(row.front_end_id for row in records),
                         ["10037553", "mrds-10037553", "mrds:10037553"])
        self.assertEqual({row.id_form for row in records},
                         {"bare", "slug", "namespaced"})

    def test_the_district_and_county_columns_are_read(self):
        path = self.write({"src": "mrds", "state": "AZ", "n": 1,
                           "id": ["10210412"], "nm": ["Cinder Pit"],
                           "county": ["Yavapai"]})
        records, _ = bridge.load_site_id_file(path)
        self.assertEqual({row.county for row in records}, {"Yavapai"})
        self.assertEqual({row.district for row in records}, {None})

    def test_a_ragged_place_column_is_an_error_not_a_silent_shift(self):
        path = self.write({"src": "mrds", "state": "NV", "n": 2,
                           "id": ["1", "2"], "nm": ["A", "B"],
                           "d": ["Candelaria"]})
        with self.assertRaises(ValueError):
            bridge.load_site_id_file(path)

    def test_a_bare_id_two_sources_claim_is_refused_not_merged(self):
        """The reason bare ids were withheld in the first place. They are
        emitted now, and merge_records() is what keeps the promise."""
        rows = [record("1", source="a.json", state="WY"),
                record("1", source="b.json", state="NV")]
        merged = bridge.merge_records(rows)
        self.assertEqual(len(merged), 1)
        best, group, collision = merged[0]
        self.assertIsNotNone(collision)
        mapping = bridge.derive(best, group, collision, NEVADA, 0.72)
        self.assertEqual(mapping.method, "unmapped")
        self.assertEqual(mapping.evidence["reason"], "front_end_id_collision")


class DistrictTierTests(unittest.TestCase):
    """Nevada: the district is the only thing the two namespaces share."""

    def test_a_district_resolves_to_that_districts_files(self):
        mapping = derive(record("10037553", name="Washoe Claims",
                                district="Cave Creek"), NEVADA)
        self.assertEqual(mapping.method, "district_name")
        self.assertEqual(mapping.relation, "district")
        self.assertIn("10000001", mapping.ws13_mine_id_all)
        self.assertIn("10000003", mapping.ws13_mine_id_all)

    def test_the_countywide_files_come_with_the_district(self):
        """A document about Elko County is about every district in it, and no
        district match can reach it -- NBMG files it under 'ELKO COUNTY
        GENERAL', which is not the name of any district."""
        mapping = derive(record("10037553", district="Cave Creek"), NEVADA)
        self.assertIn("900028", mapping.ws13_mine_id_all)

    def test_a_district_row_is_never_marked_verified(self):
        mapping = derive(record("10037553", district="Cave Creek"), NEVADA)
        self.assertFalse(mapping.verified)
        self.assertGreaterEqual(mapping.confidence,
                                bridge.RETRIEVAL_MIN_CONF)

    def test_an_exact_name_match_on_district_files_is_not_identity(self):
        """NBMG's mine_names ARE district names, so an MRDS site called
        'Cave Creek' matching the corpus name 'CAVE CREEK' is true and is not
        a statement that those documents are about that deposit. Reported as
        identity it would have made 15,717 Nevada documents look like
        mine-level records."""
        mapping = derive(record("10037553", name="Cave Creek"), NEVADA)
        self.assertEqual(mapping.relation, "district")
        self.assertEqual(mapping.method, "district_name")

    def test_a_district_name_matching_a_mine_name_is_refused(self):
        """'Summit' is a district in Idaho and a mine name in the Idaho
        corpus. Mapping on that coincidence would file a whole district's
        documents under one mine."""
        idaho = corpus_of(document("7" * 64, "ID", ["CH0447"], ["Summit"]))
        mapping = derive(record("1", district="Summit", state="ID"), idaho)
        self.assertEqual(mapping.method, "unmapped")
        self.assertIn("district_name:summit:not_a_place",
                      mapping.evidence["attempts"])

    def test_countywide_files_do_not_follow_a_mine_level_match(self):
        """The county's general files come with a DISTRICT. Attaching them to
        a mine that merely shares a name would be a much larger claim."""
        mixed = corpus_of(
            document("1" * 64, "NV", ["MINE-1"], ["Cave Creek"], "ELKO"),
            district_file("c" * 64, "NV", ["900028"], ["ELKO COUNTY GENERAL"],
                          "ELKO"))
        mapping = derive(record("1", name="Cave Creek"), mixed)
        self.assertEqual(mapping.relation, "identity")
        self.assertNotIn("900028", mapping.ws13_mine_id_all)

    def test_an_unknown_district_maps_to_nothing(self):
        mapping = derive(record("10037553", district="Nowhere"), NEVADA)
        self.assertEqual(mapping.method, "unmapped")

    def test_the_numeric_collision_does_not_become_a_mapping(self):
        """60000037 is an MRDS deposit id AND an NBMG file number for an
        unrelated document. The code tier used to resolve the equality at
        confidence 1.0 and mark it verified -- 730 such rows are in the live
        table, 'Elk City District' in Idaho pointing at a Nevada file among
        them. The row is written as unmapped instead, which also stops the
        retrieval path from matching the id as supplied."""
        mapping = derive(record("60000037", name="Sand Springs District"),
                         NEVADA)
        self.assertEqual(mapping.method, "unmapped")
        self.assertIsNone(mapping.ws13_mine_id)
        self.assertIn("mine_ids:60000037:numeric_namespace_blocked",
                      mapping.evidence["attempts"])

    def test_a_non_numeric_code_is_still_matched_for_a_blocked_namespace(self):
        """The block is on the shape of the id, not on the source. A namespace
        that carries a real corpus code still resolves through it."""
        corpus = corpus_of(document("9" * 64, "NV", ["ADMM-42"], ["X"]))
        mapping = derive(record("ADMM-42", state="NV"), corpus)
        self.assertEqual(mapping.method, "embedded_code")

    def test_a_numeric_code_from_another_namespace_is_not_blocked(self):
        """Only 'mrds' is in the block list, and it is there on evidence."""
        corpus = corpus_of(document("8" * 64, "NV", ["60000037"], ["X"]))
        mapping = derive(record("60000037", namespace="stategeo",
                                state="NV"), corpus)
        self.assertEqual(mapping.method, "embedded_code")


class PlaceNameTierTests(unittest.TestCase):
    """Arizona: the mine name is inside the collection title."""

    def test_a_mine_name_inside_a_collection_title_maps(self):
        mapping = derive(record("10210412", name="Emerald Isle Mine",
                                state="AZ", county="Mohave"), ARIZONA)
        self.assertEqual(mapping.method, "place_name")
        self.assertEqual(mapping.relation, "identity")
        self.assertEqual(mapping.ws13_mine_id_all,
                         ["ADMM-1552462915154-298"])

    def test_a_county_disagreement_refuses_the_match(self):
        """The failure this second key exists for: the same name in another
        county is another mine."""
        mapping = derive(record("10210412", name="Emerald Isle Mine",
                                state="AZ", county="Pima"), ARIZONA)
        self.assertEqual(mapping.method, "unmapped")

    def test_an_exact_name_is_identity_and_outranks_containment(self):
        mapping = derive(record("10210999", name="Defense Antimony",
                                state="AZ", county="Maricopa"), ARIZONA)
        self.assertEqual(mapping.method, "exact_name")
        self.assertEqual(mapping.relation, "identity")
        self.assertEqual(mapping.confidence, bridge.CONF_EXACT_NAME)

    def test_a_generic_name_maps_to_nothing(self):
        for name in ("Unknown Prospects", "Placers", "Mine"):
            with self.subTest(name=name):
                mapping = derive(record("10210413", name=name, state="AZ"),
                                 ARIZONA)
                self.assertEqual(mapping.method, "unmapped")

    def test_one_word_of_a_two_word_name_does_not_carry_the_match(self):
        """The longest-run probe used to fall all the way to any single
        six-letter token: 'Gibbons Permit' matched four unrelated collections
        on the word 'permit'. A run has to carry most of the name."""
        corpus = corpus_of(
            document("9" * 64, "AZ", ["ADMM-9"],
                     ["Vekol Hills Project permit correspondence"], "Pinal"))
        mapping = derive(record("1", name="Gibbons Permit", state="AZ",
                                county="Pinal"), corpus)
        self.assertEqual(mapping.method, "unmapped")

    def test_a_run_over_the_posting_cap_carries_no_signal(self):
        shared = corpus_of(*[
            document(f"{index:064x}", "AZ", [f"ADMM-{index}"],
                     [f"Copper Basin report {index}"], "Mohave")
            for index in range(bridge.RUN_POSTING_CAP + 5)])
        mapping = derive(record("1", name="Copper Basin", state="AZ",
                                county="Mohave"), shared)
        self.assertEqual(mapping.method, "unmapped")


class PrecedenceTests(unittest.TestCase):
    """The tiers that already worked have to keep working."""

    IDAHO = corpus_of(
        document("1" * 64, "ID", ["IF0126", "if0126"],
                 ["St. Louis Mine", "St. Louis Mine"], "Butte"))

    def test_an_embedded_code_still_beats_every_name_tier(self):
        mapping = bridge.derive(
            bridge.FrontEndRecord("stategeo-igs-dd-1-if0126",
                                  "build-inputs/data/sites/stategeo_id.json",
                                  "stategeo", "IGS DD-1 IF0126",
                                  "St. Louis Mine", "ID", "slug"),
            [], None, self.IDAHO, 0.72)
        self.assertEqual(mapping.method, "embedded_code")
        self.assertEqual(mapping.relation, "identity")
        self.assertTrue(mapping.verified)
        self.assertEqual(mapping.confidence, bridge.CONF_CODE_IN_MINE_IDS)

    def test_every_spelling_of_the_corpus_id_is_carried(self):
        mapping = bridge.derive(
            bridge.FrontEndRecord("if0126-probe", "x.json", "stategeo",
                                  "IF0126", None, "ID", "slug"),
            [], None, self.IDAHO, 0.72)
        self.assertEqual(sorted(mapping.ws13_mine_id_all),
                         ["IF0126", "if0126"])


class SchemaTests(unittest.TestCase):
    """The table definition, and the migration that reaches an existing one."""

    def test_every_method_the_deriver_emits_is_in_the_check(self):
        for method in bridge.METHODS:
            with self.subTest(method=method):
                self.assertIn(f"'{method}'", bridge.CREATE_TABLE_SQL)
                self.assertIn(f"'{method}'", bridge.MIGRATE_SQL)

    def test_every_relation_the_deriver_emits_is_in_the_check(self):
        for relation in bridge.RELATIONS:
            with self.subTest(relation=relation):
                self.assertIn(f"'{relation}'", bridge.CREATE_TABLE_SQL)

    def test_the_migration_is_idempotent_in_its_own_text(self):
        """Run twice against a migrated table it must be a no-op, so every
        statement carries IF NOT EXISTS or IF EXISTS."""
        statements = [line.strip() for line in
                      bridge.MIGRATE_SQL.split(";") if line.strip()]
        self.assertTrue(statements)
        for statement in statements:
            with self.subTest(statement=statement[:60]):
                flat = " ".join(statement.split())
                self.assertTrue(
                    "IF NOT EXISTS" in flat or "IF EXISTS" in flat
                    or flat.startswith("ALTER TABLE ws13_mine_id_map ADD "
                                       "CONSTRAINT")
                    or "VALIDATE CONSTRAINT" in flat, flat)

    def test_the_upsert_writes_the_relation(self):
        self.assertIn("relation = EXCLUDED.relation", bridge.UPSERT_SQL)
        self.assertIn("relation IS DISTINCT FROM", bridge.UPSERT_SQL)

    def test_the_expected_shape_matches_the_create(self):
        for name, _, _ in bridge.EXPECTED_COLUMNS:
            with self.subTest(column=name):
                self.assertIn(name, bridge.CREATE_TABLE_SQL)
        for name in bridge.EXPECTED_CONSTRAINTS:
            with self.subTest(constraint=name):
                self.assertIn(name, bridge.CREATE_TABLE_SQL)

    def test_the_reader_threshold_matches_the_lambda(self):
        """reachability() would report a coverage the product does not have if
        these two drifted apart."""
        lambda_source = (ROOT / "infra" / "ws13_query_lambda.py").read_text(
            encoding="utf-8")
        self.assertIn(f"MINE_MAP_MIN_CONFIDENCE = "
                      f"{bridge.RETRIEVAL_MIN_CONF}", lambda_source)


class RetractionTests(unittest.TestCase):
    """Withdrawing a mapping the UPSERT guard would otherwise protect.

    'verified' in this table means the derivation matched a code against a
    stored id, not that a person confirmed anything -- nothing has ever
    written a row here by hand. So the guard that stops a confirmation being
    downgraded also stops 730 wrong rows being corrected, and there has to be
    a way to say so out loud.
    """

    UNMAPPED = bridge.Mapping(None, None, "unmapped", 0.0, False, {})
    NEW = bridge.Mapping("10000001", ["10000001"], "district_name", 0.85,
                         False, {}, "district")

    def test_a_contradicted_verified_row_is_listed(self):
        rows = [("mrds-60000037", self.UNMAPPED)]
        existing = {"mrds-60000037": (True, "60000037")}
        self.assertEqual(bridge.retractions(rows, existing),
                         ["mrds-60000037"])

    def test_a_row_deriving_the_same_answer_is_not_retracted(self):
        rows = [("x", bridge.Mapping("IF0126", ["IF0126"], "exact_name",
                                     0.9, False, {}))]
        self.assertEqual(bridge.retractions(rows, {"x": (True, "IF0126")}),
                         [])

    def test_a_row_the_upsert_can_replace_itself_is_not_retracted(self):
        """A new verified row passes the guard on its own; deleting first
        would be a needless write."""
        rows = [("x", bridge.Mapping("IF0126", ["IF0126"], "embedded_code",
                                     1.0, True, {}))]
        self.assertEqual(bridge.retractions(rows, {"x": (True, "OTHER")}), [])

    def test_an_unverified_stored_row_needs_no_retraction(self):
        self.assertEqual(
            bridge.retractions([("x", self.NEW)], {"x": (False, "OTHER")}), [])


class ReachabilityTests(unittest.TestCase):
    """The number the module exists to move, measured on documents."""

    def test_a_low_confidence_row_counts_for_nothing(self):
        rows = [("x", bridge.Mapping("10000001", ["10000001"], "fuzzy_name",
                                     0.6, False, {}, "identity"))]
        report = bridge.reachability(NEVADA, rows)
        self.assertEqual(report["NV"]["reachable"], 0)

    def test_district_and_identity_are_counted_apart(self):
        rows = [
            ("a", bridge.Mapping("10000001", ["10000001", "10000002",
                                              "10000003"], "district_name",
                                 0.85, False, {}, "district")),
            ("b", bridge.Mapping("60000037", ["60000037"], "exact_name",
                                 0.9, False, {}, "identity")),
        ]
        report = bridge.reachability(NEVADA, rows)
        self.assertEqual(report["NV"]["documents"], 4)
        self.assertEqual(report["NV"]["reachable"], 3)
        self.assertEqual(report["NV"]["by_relation"],
                         {"district": 2, "identity": 1})

    def test_a_document_reachable_both_ways_is_counted_once(self):
        rows = [
            ("a", bridge.Mapping("10000001", ["10000001"], "exact_name",
                                 0.9, False, {}, "identity")),
            ("b", bridge.Mapping("10000001", ["10000001"], "district_name",
                                 0.85, False, {}, "district")),
        ]
        report = bridge.reachability(NEVADA, rows)
        self.assertEqual(report["NV"]["reachable"], 1)
        self.assertEqual(report["NV"]["by_relation"], {"identity": 1})


if __name__ == "__main__":
    unittest.main()
