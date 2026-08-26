"""Contract tests for the WS13 migration group.

This group had no test at all, which is how a one-shot year backfill with no
maintenance path, a rights guard that accepted the wrong field index, and a
--check gate that never measured the provenance backfill all shipped
together. What is pinned here:

  * split_statements() against the real pipelines/ws13_migrations.sql -- it is
    a hand-written SQL lexer, and ws13_seed.py depends on it to build a
    production database from scratch. A dollar-quoted DO block cut in half
    still "parses" as several statements and only fails at the server;
  * the year rule. doc_year_min/doc_year_max are GENERATED ALWAYS ... STORED
    over ws13_doc_year_min()/ws13_doc_year_max(), so the documented doc_date
    shapes ('VARIOUS', 'CIRCA 1980', '1930; 1933; 1940', '19740601') are
    asserted against a Python mirror of the same regexp and 1800..2099 bound,
    and the gate query is asserted to call the helpers rather than to carry a
    second copy of the predicate;
  * run_checks() over a fake catalogue: a wrong generation expression, a
    stored year range the helper does not reproduce, and an unfinished
    provenance backfill each have to come back as a MISS;
  * the manifest collapse rule that ws13_backfill_provenance,
    ws13_seed and ws13_enqueue now share, including its rerun idempotence;
  * resolve_provenance(), which is the fix for a requeued document coming
    back from the worker with no rights_basis at all.

No database and no AWS: psycopg and boto3 are stubbed the same way
tests/test_ws13_embed_backfill.py stubs them.
"""
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PIPELINES = ROOT / "pipelines"
sys.path.insert(0, str(PIPELINES))

# Deployment dependencies of the migration host, not of the test host.
for _name in ("psycopg", "boto3"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            _stub = types.ModuleType(_name)
            _stub.connect = mock.MagicMock()
            _stub.client = mock.MagicMock()
            sys.modules[_name] = _stub

import ws13_backfill_provenance as backfill        # noqa: E402
import ws13_enqueue                                # noqa: E402
import ws13_migrate                                # noqa: E402
import ws13_seed                                   # noqa: E402

MIGRATIONS_SQL = PIPELINES / "ws13_migrations.sql"
SQL_TEXT = MIGRATIONS_SQL.read_text(encoding="utf-8")

YEAR_RE = re.compile(r"\d{4}")


def year_bounds(doc_date):
    """Python mirror of ws13_doc_year_min()/ws13_doc_year_max().

    Same regexp, same 1800..2099 bound, same "no match means NULL, never a
    guessed year" rule. regexp_matches(..., 'g') and re.findall() both walk
    left to right over non-overlapping matches, so '19740601' yields 1974 and
    0601 in both, and only the first survives the bound.
    """
    years = [int(hit) for hit in YEAR_RE.findall(doc_date or "")
             if 1800 <= int(hit) <= 2099]
    return (min(years), max(years)) if years else (None, None)


class SplitStatementsTests(unittest.TestCase):
    """The lexer ws13_seed.py builds a production database with."""

    def setUp(self):
        self.statements = ws13_migrate.split_statements(SQL_TEXT)

    def test_every_statement_has_executable_content(self):
        for statement in self.statements:
            self.assertTrue(statement["label"].strip())
            self.assertTrue(statement["sql"].strip())

    def test_dollar_quoted_blocks_are_never_split(self):
        # A DO block cut at the ';' inside its body is the failure this lexer
        # exists to prevent: the halves are syntactically plausible and only
        # the server rejects them.
        tags = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)\$", SQL_TEXT))
        self.assertIn("generated_guard", tags)
        self.assertIn("year_reset", tags)
        for tag in tags:
            marker = "$%s$" % tag
            holders = [s for s in self.statements if marker in s["sql"]]
            # $fn$ opens and closes three separate function bodies; what has
            # to hold is that no block is cut across a statement boundary.
            self.assertEqual(len(holders), SQL_TEXT.count(marker) // 2, marker)
            for holder in holders:
                self.assertEqual(holder["sql"].count(marker), 2, marker)

    def test_no_statement_starts_inside_a_block(self):
        for statement in self.statements:
            head = statement["label"].split()[0].upper()
            self.assertNotIn(head, ("END", "BEGIN", "LOOP", "IF", "ELSE"))

    def test_backslash_literals_survive(self):
        # standard_conforming_strings: '\d{4}' and 'ws13\_%' are literal, and
        # a lexer that treated the backslash as an escape would mis-track the
        # closing quote and merge the next two statements.
        joined = " ".join(s["sql"] for s in self.statements)
        self.assertIn(r"'\d{4}'", joined)
        self.assertIn(r"'ws13\_%'", joined)

    def test_statements_cover_the_migration(self):
        labels = [s["label"] for s in self.statements]
        for fragment in ("ADD COLUMN IF NOT EXISTS admission_class",
                         "ADD COLUMN IF NOT EXISTS doc_year_min",
                         "ADD COLUMN IF NOT EXISTS source_url",
                         "CREATE OR REPLACE FUNCTION ws13_doc_year_min",
                         "CREATE OR REPLACE FUNCTION ws13_doc_year_max",
                         "CREATE OR REPLACE FUNCTION ws13_county_key",
                         "CREATE INDEX IF NOT EXISTS ws13_documents_years",
                         "GRANT SELECT ON ws13_documents"):
            self.assertTrue(any(fragment in label for label in labels),
                            "no statement contains %r" % fragment)

    def test_comment_only_tail_is_not_a_statement(self):
        text = "-- leading\nSELECT 1;\n-- trailing comment only\n"
        self.assertEqual(
            [s["label"] for s in ws13_migrate.split_statements(text)],
            ["SELECT 1"])

    def test_dry_run_reports_every_statement(self):
        with mock.patch("sys.stdout"):
            code = ws13_migrate.main(["--dry-run", "--sql", str(MIGRATIONS_SQL)])
        self.assertEqual(code, 0)


class YearRuleTests(unittest.TestCase):
    """The parse that the generated columns and the gate both depend on."""

    def test_documented_doc_date_shapes(self):
        cases = {
            None: (None, None),
            "": (None, None),
            "VARIOUS": (None, None),
            "1948": (1948, 1948),
            "CIRCA 1980": (1980, 1980),
            "1930; 1933; 1940": (1930, 1940),
            "19740601": (1974, 1974),
            "1974-06-01": (1974, 1974),
            # A 4-digit report number is why the bound is not decoration.
            "Bulletin 1234": (None, None),
        }
        for doc_date, expected in cases.items():
            self.assertEqual(year_bounds(doc_date), expected, repr(doc_date))

    def test_sql_helpers_carry_the_same_regexp_and_bound(self):
        for name in ("ws13_doc_year_min", "ws13_doc_year_max"):
            self.assertIn("CREATE OR REPLACE FUNCTION %s(text)" % name,
                          SQL_TEXT)
        self.assertEqual(SQL_TEXT.count("BETWEEN 1800 AND 2099"), 2)
        self.assertEqual(SQL_TEXT.count(r"regexp_matches(coalesce($1, ''), "
                                        r"'\d{4}', 'g')"), 2)
        self.assertIn("IMMUTABLE", SQL_TEXT)

    def test_year_columns_are_generated_not_backfilled(self):
        # The blocker this group was rewritten for: a one-shot UPDATE leaves
        # every document indexed afterwards with a NULL year range, and
        # ws13_worker.py never writes these columns.
        self.assertIn("GENERATED ALWAYS AS (ws13_doc_year_min(doc_date)) "
                      "STORED", SQL_TEXT)
        self.assertIn("GENERATED ALWAYS AS (ws13_doc_year_max(doc_date)) "
                      "STORED", SQL_TEXT)
        self.assertNotIn("UPDATE ws13_documents", SQL_TEXT)

    def test_gate_calls_the_helpers_instead_of_copying_the_predicate(self):
        gate = ws13_migrate.YEAR_GAP_SQL
        self.assertIn("ws13_doc_year_min(d.doc_date)", gate)
        self.assertIn("ws13_doc_year_max(d.doc_date)", gate)
        self.assertNotIn("1800", gate)
        self.assertNotIn("regexp_matches", gate)


class GenerationExpressionTests(unittest.TestCase):
    """The guard that has to reject split_part(s3_key, '/', 3)."""

    def test_normalisation_strips_only_rendering_noise(self):
        rendered = "split_part(s3_key, '/'::text, 2)"
        self.assertEqual(ws13_migrate._normalized_generation(rendered),
                         "split_part(s3_key,'/',2)")
        self.assertEqual(
            ws13_migrate._normalized_generation("public.ws13_doc_year_min"
                                                "(doc_date)"),
            "ws13_doc_year_min(doc_date)")
        self.assertIsNone(ws13_migrate._normalized_generation(None))

    def test_wrong_field_index_is_not_accepted(self):
        # 'split_part' and 's3_key' both appear, which is all the old guard
        # checked; field 3 is the portal segment, not the rights class.
        wrong = ws13_migrate._normalized_generation(
            "split_part(s3_key, '/'::text, 3)")
        self.assertNotEqual(
            wrong, ws13_migrate.GENERATED_COLUMNS["admission_class"])

    def test_python_and_sql_agree_on_the_expected_expressions(self):
        for column, expected in ws13_migrate.GENERATED_COLUMNS.items():
            quoted = expected.replace("'", "''")
            self.assertIn("('%s', '%s')" % (column, quoted), SQL_TEXT,
                          "%s: the .sql guard and GENERATED_COLUMNS disagree"
                          % column)


class FakeCursor(object):
    def __init__(self, rows, rowcount=-1):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeCatalogue(object):
    """Just enough catalogue and table for run_checks() to run unmodified."""

    ALL_COLUMNS = ("sha256", "s3_key", "doc_date", "title") + \
        ws13_migrate.REQUIRED_COLUMNS

    def __init__(self, columns=None, generation=None, indexes=None,
                 functions=None, can_login=True, tables=None,
                 write_grants=(), documents=()):
        self.columns = list(self.ALL_COLUMNS if columns is None else columns)
        self.generation = dict(
            ws13_migrate.GENERATED_COLUMNS if generation is None
            else generation)
        self.indexes = list(ws13_migrate.REQUIRED_INDEXES
                            if indexes is None else indexes)
        self.functions = dict(
            functions if functions is not None else
            {name: "i" for name in
             ("ws13_county_key",) + ws13_migrate.YEAR_FUNCTIONS})
        self.can_login = can_login
        self.tables = list(ws13_migrate.READER_TABLES
                           if tables is None else tables)
        self.write_grants = set(write_grants)
        self.documents = list(documents)

    def execute(self, query, params=None):
        text = " ".join(query.split())
        if "attnum > 0" in text:
            return FakeCursor([(name,) for name in self.columns])
        if "pg_get_expr" in text:
            return FakeCursor(sorted(self.generation.items()))
        if "FROM pg_indexes" in text:
            wanted = set(params[0])
            return FakeCursor([(n,) for n in self.indexes if n in wanted])
        if "FROM pg_proc" in text:
            wanted = set(params[0])
            return FakeCursor([(n, v) for n, v in sorted(self.functions.items())
                               if n in wanted])
        if "rolcanlogin" in text:
            return FakeCursor([(self.can_login,)])
        if "FROM pg_tables" in text:
            wanted = set(params[0])
            return FakeCursor([(n,) for n in self.tables if n in wanted])
        if "has_table_privilege" in text:
            _role, table, privilege = params
            if privilege == "SELECT":
                return FakeCursor([(table in self.tables,)])
            return FakeCursor([((table, privilege) in self.write_grants,)])
        if "ws13_doc_year_min(d.doc_date)" in text:
            gap = sum(1 for row in self.documents
                      if (row.get("doc_year_min"), row.get("doc_year_max"))
                      != year_bounds(row.get("doc_date")))
            return FakeCursor([(gap,)])
        if "FILTER" in text and "admission_class" in text:
            total = len(self.documents)
            rights = sum(1 for row in self.documents
                         if row.get("admission_class") in
                         ("licensed-copies", "research-copies")
                         and not row.get("rights_basis"))
            unknown = sum(1 for row in self.documents
                          if row.get("admission_class") not in
                          ("originals", "licensed-copies", "research-copies"))
            no_url = sum(1 for row in self.documents
                         if not row.get("source_url"))
            return FakeCursor([(total, rights, unknown, no_url)])
        raise AssertionError("unexpected query: %s" % text)


def document(sha, admission="originals", doc_date="1948",
             rights_basis="public domain", source_url="https://example/1",
             year_min=None, year_max=None, drift=False):
    low, high = year_bounds(doc_date)
    return {
        "sha256": sha, "admission_class": admission, "doc_date": doc_date,
        "rights_basis": rights_basis, "source_url": source_url,
        "doc_year_min": (year_min if drift else low),
        "doc_year_max": (year_max if drift else high),
    }


def failures(results):
    return {name for name, ok, _detail in results if not ok}


class RunChecksTests(unittest.TestCase):
    def corpus(self):
        return [document("a" * 64),
                document("b" * 64, admission="licensed-copies",
                         rights_basis="CC BY-NC-SA 4.0"),
                document("c" * 64, admission="research-copies",
                         rights_basis="state archive research copy",
                         doc_date="VARIOUS")]

    def test_a_finished_database_passes_every_gate(self):
        conn = FakeCatalogue(documents=self.corpus())
        results = ws13_migrate.run_checks(conn, require_login=True,
                                          require_provenance=True)
        self.assertEqual(failures(results), set())

    def test_stale_year_range_is_a_gap(self):
        rows = self.corpus()
        # Exactly the drift a hand-edited helper leaves behind: the column is
        # generated, so nothing else in the catalogue looks wrong.
        rows[0]["doc_year_min"] = 1900
        conn = FakeCatalogue(documents=rows)
        results = ws13_migrate.run_checks(conn)
        self.assertIn("doc_year_min/max agree with the helpers",
                      failures(results))

    def test_wrong_admission_class_expression_is_a_gap(self):
        generation = dict(ws13_migrate.GENERATED_COLUMNS)
        generation["admission_class"] = "split_part(s3_key, '/'::text, 3)"
        conn = FakeCatalogue(generation=generation, documents=self.corpus())
        results = ws13_migrate.run_checks(conn)
        self.assertEqual(
            failures(results),
            {"admission_class is STORED generated from split_part(s3_key,"
             "'/',2)"})

    def test_plain_year_column_is_a_gap(self):
        generation = dict(ws13_migrate.GENERATED_COLUMNS)
        del generation["doc_year_min"]
        conn = FakeCatalogue(generation=generation, documents=self.corpus())
        results = ws13_migrate.run_checks(conn)
        self.assertIn(
            "doc_year_min is STORED generated from ws13_doc_year_min"
            "(doc_date)", failures(results))

    def test_missing_rights_is_advisory_until_required(self):
        rows = self.corpus()
        rows[1]["rights_basis"] = None
        conn = FakeCatalogue(documents=rows)
        self.assertEqual(failures(ws13_migrate.run_checks(conn)), set())
        results = ws13_migrate.run_checks(conn, require_provenance=True)
        self.assertIn("licensed/research copies carry a rights_basis",
                      failures(results))

    def test_source_url_coverage_threshold(self):
        rows = self.corpus()
        rows[2]["source_url"] = None
        conn = FakeCatalogue(documents=rows)
        results = ws13_migrate.run_checks(conn, require_provenance=True)
        self.assertTrue(any(name.startswith("source_url coverage")
                            for name in failures(results)))

    def test_unknown_admission_class_is_reported(self):
        rows = self.corpus()
        rows[0]["admission_class"] = "azgs_admmr"
        conn = FakeCatalogue(documents=rows)
        results = ws13_migrate.run_checks(conn, require_provenance=True)
        self.assertIn("every admission_class is a known rights class",
                      failures(results))

    def test_missing_year_helper_is_a_gap(self):
        conn = FakeCatalogue(functions={"ws13_county_key": "i"},
                             documents=self.corpus())
        results = ws13_migrate.run_checks(conn)
        self.assertIn("function ws13_doc_year_min(text) IMMUTABLE",
                      failures(results))

    def test_write_grant_to_the_reader_is_a_gap(self):
        conn = FakeCatalogue(documents=self.corpus(),
                             write_grants={("ws13_chunks", "UPDATE")})
        results = ws13_migrate.run_checks(conn)
        self.assertIn("ws13_reader has no write privileges", failures(results))

    def test_a_timing_out_gate_reports_a_miss_rather_than_crashing(self):
        class Timeout(FakeCatalogue):
            def execute(self, query, params=None):
                if "ws13_doc_year_min(d.doc_date)" in " ".join(query.split()):
                    raise RuntimeError("canceling statement due to "
                                       "statement timeout")
                return FakeCatalogue.execute(self, query, params)

        results = ws13_migrate.run_checks(Timeout(documents=self.corpus()))
        self.assertIn("doc_year_min/max agree with the helpers",
                      failures(results))

    def test_require_provenance_needs_check(self):
        with self.assertRaises(SystemExit):
            ws13_migrate.main(["--apply", "--require-provenance"])


class ReaderPasswordTests(unittest.TestCase):
    def test_verifier_never_contains_the_password(self):
        verifier = ws13_migrate.scram_sha_256_verifier(
            "hunter2-not-a-real-password", salt=b"0123456789abcdef")
        self.assertNotIn("hunter2", verifier)
        self.assertTrue(verifier.startswith("SCRAM-SHA-256$4096:"))
        stored, server = verifier.split("$")[2].split(":")
        import base64
        self.assertEqual(len(base64.b64decode(stored)), 32)
        self.assertEqual(len(base64.b64decode(server)), 32)

    def test_verifier_is_deterministic_for_a_fixed_salt(self):
        first = ws13_migrate.scram_sha_256_verifier("abc", salt=b"x" * 16)
        second = ws13_migrate.scram_sha_256_verifier("abc", salt=b"x" * 16)
        self.assertEqual(first, second)
        self.assertNotEqual(
            first, ws13_migrate.scram_sha_256_verifier("abd", salt=b"x" * 16))

    def test_non_ascii_password_refuses_rather_than_locking_the_role_out(self):
        # Pre-hashing without SASLprep would store a verifier the client can
        # never reproduce; that must be an error here, not a mystery later.
        with self.assertRaises(SystemExit):
            ws13_migrate.scram_sha_256_verifier("pässword")


def write_manifest(rows):
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", delete=False, encoding="utf-8")
    for row in rows:
        handle.write(json.dumps(row) + "\n")
    handle.close()
    return handle.name


def manifest_row(sha, admission="cc_by_nc_sa_licensed", prefix="licensed-copies",
                 **overrides):
    row = {"sha256": sha, "admission_class": admission,
           "s3_uri": "s3://bucket/ws12/%s/portal/%s.pdf" % (prefix, sha[:2]),
           "source_url": "https://example/%s" % sha[:4],
           "rights_basis": "CC BY-NC-SA 4.0", "public_domain": False}
    row.update(overrides)
    return row


class ManifestCollapseTests(unittest.TestCase):
    def load(self, rows):
        path = write_manifest(rows)
        try:
            return backfill.load_provenance(manifest=path)
        finally:
            os.unlink(path)

    def test_a_later_occurrence_fills_what_the_first_left_empty(self):
        # The rule ws13_seed.py used to disagree with: a plain setdefault
        # keeps the first row's empty rights_basis and the document is
        # indexed with no licence.
        records, stats, conflicts = self.load([
            manifest_row("aa" * 32, rights_basis=None, source_url=None),
            manifest_row("aa" * 32),
        ])
        self.assertEqual(conflicts, {})
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["documents"], 1)
        record = records["aa" * 32]
        self.assertEqual(record["rights_basis"], "CC BY-NC-SA 4.0")
        self.assertEqual(record["source_url"], "https://example/aaaa")
        self.assertIs(record["public_domain"], False)

    def test_first_occurrence_still_wins_when_it_has_a_value(self):
        records, _stats, _conflicts = self.load([
            manifest_row("bb" * 32, rights_basis="first"),
            manifest_row("bb" * 32, rights_basis="second"),
        ])
        self.assertEqual(records["bb" * 32]["rights_basis"], "first")

    def test_contradictory_rights_class_refuses_the_document(self):
        records, stats, conflicts = self.load([
            manifest_row("cc" * 32, admission="public_domain",
                         prefix="licensed-copies"),
        ])
        self.assertEqual(records, {})
        self.assertEqual(stats["class_conflict"], 1)
        self.assertEqual(len(conflicts), 1)

    def test_rerun_is_idempotent(self):
        rows = [manifest_row("dd" * 32), manifest_row("ee" * 32)]
        first, _s1, _c1 = self.load(rows)
        second, _s2, _c2 = self.load(rows)
        self.assertEqual(first, second)


class FakeDocumentsTable(object):
    """conn.execute() for ws13_backfill_provenance.plan()/apply_updates()."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, params=None):
        if query.strip().startswith("SELECT sha256"):
            return FakeCursor([
                (sha, row["admission_class"], row["source_url"],
                 row["rights_basis"], row["public_domain"])
                for sha, row in sorted(self.rows.items())])
        # apply_updates(): fold the VALUES tuples back into the table the way
        # the COALESCE in UPDATE_SQL would.
        changed = 0
        for index in range(0, len(params), 4):
            sha, source_url, rights_basis, public_domain = \
                params[index:index + 4]
            row = self.rows[sha]
            for field, value in (("source_url", source_url),
                                 ("rights_basis", rights_basis),
                                 ("public_domain", public_domain)):
                if value is not None and row[field] != value:
                    row[field] = value
                    changed += 1
        return FakeCursor([(changed,)], rowcount=changed)


class PlanTests(unittest.TestCase):
    def rows(self):
        return {
            "aa" * 32: {"admission_class": "licensed-copies",
                        "source_url": None, "rights_basis": None,
                        "public_domain": None},
            "bb" * 32: {"admission_class": "originals",
                        "source_url": "https://example/bb",
                        "rights_basis": "public domain",
                        "public_domain": True},
        }

    def records(self):
        return {
            "aa" * 32: {"source_url": "https://example/aa",
                        "rights_basis": "CC BY-NC-SA 4.0",
                        "public_domain": False,
                        "admission_class": "licensed-copies"},
            "bb" * 32: {"source_url": "https://example/bb",
                        "rights_basis": "public domain",
                        "public_domain": True,
                        "admission_class": "originals"},
        }

    def test_plan_then_apply_then_plan_reports_nothing_left(self):
        table = FakeDocumentsTable(self.rows())
        updates, report, mismatched = backfill.plan(table, self.records())
        self.assertEqual(mismatched, [])
        self.assertEqual(len(updates), 1)
        self.assertEqual(report["missing_rights"], 0)
        backfill.apply_updates(table, updates)
        again, report, _mismatched = backfill.plan(table, self.records())
        self.assertEqual(again, [])
        self.assertEqual(report["unchanged"], 2)

    def test_licensed_gap_is_counted_separately_from_originals(self):
        rows = self.rows()
        rows["bb" * 32]["rights_basis"] = None
        table = FakeDocumentsTable(rows)
        records = self.records()
        records["aa" * 32]["rights_basis"] = None
        records["bb" * 32]["rights_basis"] = None
        _updates, report, _mismatched = backfill.plan(table, records)
        self.assertEqual(report["missing_rights"], 2)
        self.assertEqual(report["missing_rights_licensed"], 1)

    def test_a_class_mismatch_is_refused_not_written(self):
        rows = self.rows()
        rows["aa" * 32]["admission_class"] = "originals"
        table = FakeDocumentsTable(rows)
        updates, report, mismatched = backfill.plan(table, self.records())
        self.assertEqual(len(mismatched), 1)
        self.assertEqual(report["class_mismatch"], 1)
        self.assertEqual([u[0] for u in updates], [])


def enqueue_row(sha, s3_key, **overrides):
    values = dict(sha256=sha, s3_key=s3_key, doc_class="born_digital",
                  pages=3, status="error", portal="azgs_admmr", state="AZ",
                  mine_ids=[], mine_names=[], county=None, trs=None,
                  doc_date="1948", doc_type="documents", title="t",
                  source_url=None, rights_basis=None, public_domain=None)
    values.update(overrides)
    return ws13_enqueue.Row(**values)


class EnqueueProvenanceTests(unittest.TestCase):
    def test_rights_class_reads_the_same_segment_as_admission_class(self):
        self.assertEqual(
            ws13_enqueue.rights_class("ws12/licensed-copies/azgs/ab.pdf"),
            "licensed-copies")
        self.assertIsNone(ws13_enqueue.rights_class("ws13/searchable/ab.pdf"))
        self.assertIsNone(ws13_enqueue.rights_class(None))

    def test_manifest_fills_a_failed_document_with_no_documents_row(self):
        row = enqueue_row("aa" * 32, "ws12/licensed-copies/azgs/aa.pdf")
        records = {"aa" * 32: {"source_url": "https://example/aa",
                               "rights_basis": "CC BY-NC-SA 4.0",
                               "public_domain": False,
                               "admission_class": "licensed-copies"}}
        resolved, unresolved = ws13_enqueue.resolve_provenance([row], records)
        self.assertEqual(unresolved, [])
        self.assertEqual(resolved["aa" * 32],
                         ("https://example/aa", "CC BY-NC-SA 4.0", False))
        body = ws13_enqueue.body_for(row, False, resolved)
        self.assertEqual(body["meta"]["rights_basis"], "CC BY-NC-SA 4.0")
        # public_domain=False must survive the empty-value filter.
        self.assertIs(body["meta"]["public_domain"], False)

    def test_indexed_values_win_over_the_manifest(self):
        row = enqueue_row("bb" * 32, "ws12/licensed-copies/azgs/bb.pdf",
                          rights_basis="hand corrected",
                          source_url="https://example/corrected")
        records = {"bb" * 32: {"source_url": "https://example/manifest",
                               "rights_basis": "CC BY-NC-SA 4.0",
                               "public_domain": False,
                               "admission_class": "licensed-copies"}}
        resolved, _unresolved = ws13_enqueue.resolve_provenance([row], records)
        self.assertEqual(resolved["bb" * 32],
                         ("https://example/corrected", "hand corrected", False))

    def test_unresolvable_licensed_copy_is_reported(self):
        row = enqueue_row("cc" * 32, "ws12/research-copies/igs/cc.pdf")
        resolved, unresolved = ws13_enqueue.resolve_provenance([row], {})
        self.assertEqual(unresolved, [("cc" * 32, "research-copies")])
        self.assertEqual(resolved["cc" * 32], (None, None, None))

    def test_public_domain_original_without_rights_is_not_refused(self):
        row = enqueue_row("dd" * 32, "ws12/originals/usgs/dd.pdf")
        _resolved, unresolved = ws13_enqueue.resolve_provenance([row], {})
        self.assertEqual(unresolved, [])


class SeedBundleTests(unittest.TestCase):
    def test_missing_bundle_file_names_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(ws13_seed, "HERE", directory):
                with self.assertRaises(SystemExit) as caught:
                    ws13_seed.bundle_files()
        message = str(caught.exception)
        for name in ws13_seed.BUNDLE_FILES:
            self.assertIn(name, message)
        self.assertIn("bundle.tar.gz", message)

    def test_repository_layout_resolves(self):
        self.assertEqual(ws13_seed.bundle_files(), str(MIGRATIONS_SQL))

    def test_seed_does_not_import_its_siblings_at_module_scope(self):
        # A module-scope import fires before bundle_files() can say which
        # file is missing, which is how the seeding node died with a bare
        # ModuleNotFoundError while still releasing the fleet's seed barrier.
        head = (PIPELINES / "ws13_seed.py").read_text(
            encoding="utf-8").split("def bundle_files")[0]
        self.assertNotIn("\nimport ws13_migrate", head)
        self.assertNotIn("\nimport ws13_backfill_provenance", head)


if __name__ == "__main__":
    unittest.main()
