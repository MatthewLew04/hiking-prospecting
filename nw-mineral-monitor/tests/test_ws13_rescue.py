"""Contract tests for pipelines/ws13_rescue.py.

The rescue tool decides what to do with a document from one string:
ws13_manifest.error. Everything that matters about that decision is pure --
the exit-code classifier, the stderr-tail patterns, the order of the remedy
ladder, the escalation on failure and the classified terminal reason -- so it
is all testable with no database, no docker and no AWS.

Two of those are regressions waiting to happen. The handoff read the seven
residual failures as oversized map plates and prescribed reclassification,
when five of them are ocr_exit_4 INVALID_OUTPUT_PDF and want --output-type
pdf; and commit e9c662a exists because ws13_manifest.error used to carry the
middle of a truncated traceback, so the terminal reason is asserted to be a
classified token and nothing else.

FakeConn implements only what main() calls; FakeExecutor scripts each rung's
outcome so the ladder runs for real without a container.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

os.environ.setdefault("WS13_DB_DSN", "postgresql://test/test")

# The driver and the SDK are deployment dependencies of the worker nodes, not
# of the test host; every call through them is stubbed. ws13_rescue imports
# ws13_enqueue, which imports both at module scope.
for name in ("psycopg", "boto3"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            stub = types.ModuleType(name)
            stub.connect = mock.MagicMock()
            stub.client = mock.MagicMock()
            sys.modules[name] = stub

import ws13_rescue as rescue  # noqa: E402

SLUG = re.compile(r"[a-z0-9_]+")

# The seven documents that are not 'done', with their recorded exit codes
# (ocr_exit_4 x5, ocr_exit_7 x2) and the stderr fragments recorded across
# that set: 'invalid jpeg data reading stream' (objects 480 and 24),
# 'improbable aspect ratio', 'skipping all processing on this page', '1 page
# is facing UP', '[tesseract] Too few characters', '[tesseract] Image too
# large'. Which fragment sits on which sha is not asserted anywhere below --
# the pairing here spreads the real fragments over the real exit codes so
# every classification path is exercised against text the corpus actually
# contains.
RECORDED = {
    "87473fe10d90": "ocr_exit_4:The generated PDF is INVALID",
    "a0d005469420": ("ocr_exit_4:invalid jpeg data reading stream (object "
                     "480)\nThe generated PDF is INVALID"),
    "cb908a113d34": ("ocr_exit_4:[tesseract] Too few characters. Skipping "
                     "this page\nThe generated PDF is INVALID"),
    "e1f8d25d58b6": ("ocr_exit_4:improbable aspect ratio; skipping all "
                     "processing on this page"),
    "f3f199e9f71c": "ocr_exit_4:The generated PDF is INVALID",
    "5a3fed772044": ("ocr_exit_7:1 page is facing UP, confidence 0.87 -- "
                     "rotating\nsubprocess.CalledProcessError: tesseract"),
    "5c991bfa4e90": "ocr_exit_7:[tesseract] Image too large: (52454, 9707)",
}


def doc(sha="87473fe10d90aa", status="error", error="ocr_exit_4:boom",
        s3_key=None):
    return rescue.Row(sha256=sha, status=status,
                      s3_key=s3_key or f"ws12/originals/{sha}.pdf",
                      doc_class="ocr_queue", pages=None, error=error)


def classify(error, sha="aa11bb22cc33", status="error"):
    return rescue.classify(sha, status, error)


class FakeResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Just enough of psycopg's connection for select() and record()."""

    def __init__(self, rows, rowcounts=None):
        self.rows = rows
        self.rowcounts = list(rowcounts or [])
        self.updates = []           # (status, reason, sha, prior_status)
        self.select_sql = None
        self.select_params = None

    def execute(self, sql, params=None):
        if sql.strip().startswith("UPDATE"):
            status, reason, _worker, sha, prior = params
            self.updates.append((status, reason, sha, prior))
            got = self.rowcounts.pop(0) if self.rowcounts else 1
            return FakeResult(rowcount=got)
        self.select_sql, self.select_params = sql, params
        return FakeResult([tuple(r) for r in self.rows])


class FakeExecutor:
    """Scripts each rung's outcome; records the order they were tried in."""

    runs = True

    def __init__(self, outcomes=None, default=(False, "ocr_exit_4")):
        self.outcomes = outcomes or {}
        self.default = default
        self.calls = []

    @contextlib.contextmanager
    def document(self, document):
        yield "/scratch/work"

    def attempt(self, document, work, remedy):
        self.calls.append(remedy.name)
        ok, detail = self.outcomes.get(remedy.name, self.default)
        return rescue.Attempt(remedy, ok, detail)


def run_main(conn, argv, executor=None, enqueue=None):
    """main() against FakeConn, returning (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    factory = mock.MagicMock(return_value=executor or FakeExecutor())
    enqueue_main = enqueue or mock.MagicMock(return_value=0)
    with mock.patch.object(rescue.psycopg, "connect", return_value=conn), \
            mock.patch.object(rescue, "DockerExecutor", factory), \
            mock.patch.object(rescue.ws13_enqueue, "main", enqueue_main):
        with redirect_stdout(out), redirect_stderr(err):
            code = rescue.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExitCodeClassifierTests(unittest.TestCase):
    def test_every_documented_ocrmypdf_code_is_classified(self):
        for code, exit_name in rescue.OCR_EXIT_NAMES.items():
            with self.subTest(code=code):
                cls_ = classify(f"ocr_exit_{code}:some stderr tail")
                self.assertEqual(cls_.exit_code, code)
                self.assertEqual(cls_.exit_name, exit_name)
                self.assertIn(cls_.cause.name, rescue.CAUSES)
                self.assertTrue(SLUG.fullmatch(cls_.cause.terminal))
                self.assertTrue(cls_.cause.note)

    def test_cause_table_keys_match_their_own_names(self):
        for key, cause in rescue.CAUSES.items():
            self.assertEqual(key, cause.name)

    def test_exit_4_is_five_of_the_seven_and_wants_output_type_pdf(self):
        # The handoff called these oversized map plates. They are not: exit 4
        # is INVALID_OUTPUT_PDF, ocrmypdf failing its own PDF/A validation
        # after the OCR succeeded.
        cls_ = classify("ocr_exit_4:The generated PDF is INVALID")
        self.assertEqual(cls_.cause.name, "invalid_output_pdf")
        self.assertEqual(cls_.ladder[0].name, "output_type_pdf")
        self.assertIn("--output-type", cls_.ladder[0].ocr_args)
        self.assertIn("pdf", cls_.ladder[0].ocr_args)

    def test_exit_10_shares_the_ladder_but_not_the_terminal_token(self):
        four = classify("ocr_exit_4:x")
        ten = classify("ocr_exit_10:x")
        self.assertEqual([r.name for r in four.ladder],
                         [r.name for r in ten.ladder])
        self.assertNotEqual(four.cause.terminal, ten.cause.terminal)

    def test_exit_7_escalates_timeout_then_psm_then_skipping_the_page(self):
        cls_ = classify("ocr_exit_7:tesseract died")
        self.assertEqual(cls_.cause.name, "child_process_error")
        names = [r.name for r in cls_.ladder]
        self.assertEqual(names[0], "tesseract_timeout_1800")
        self.assertIn("--tesseract-timeout", cls_.ladder[0].ocr_args)
        # A page-segmentation change before giving up on the page, and
        # skipping the page last -- losing a page beats losing a document.
        self.assertTrue(any("pagesegmode" in n for n in names[1:-1]))
        self.assertEqual(names[-1], "skip_oversized_pages")
        self.assertIn("--skip-big", cls_.ladder[-1].ocr_args)

    def test_exit_2_and_a_damaged_stream_both_repair_the_input_first(self):
        for error in ("ocr_exit_2:not a pdf",
                      "ocr_exit_15:invalid jpeg data reading stream"):
            with self.subTest(error=error):
                cls_ = classify(error)
                self.assertEqual([r.name for r in cls_.ladder],
                                 ["qpdf_decode_repair", "ghostscript_rewrite"])
                self.assertIsNotNone(cls_.ladder[0].repair)

    def test_exit_8_decrypts_rather_than_retrying_the_ocr_settings(self):
        cls_ = classify("ocr_exit_8:encrypted")
        self.assertEqual([r.name for r in cls_.ladder], ["qpdf_decrypt"])

    def test_exit_0_recorded_as_an_error_means_the_output_went_missing(self):
        # ws13_worker.py records ocr_exit_0 when ocrmypdf reported success
        # and out.pdf was not on disk afterwards.
        cls_ = classify("ocr_exit_0:")
        self.assertEqual(cls_.cause.name, "no_output_pdf")
        self.assertTrue(cls_.ladder)

    def test_node_defects_get_no_ladder_at_all(self):
        # Retrying the DOCUMENT cannot fix a missing binary, an unwritable
        # scratch path, bad fleet arguments or an existing text layer, and a
        # ladder there spends a worker slot per rung to learn nothing.
        for code in (1, 3, 5, 6, 9):
            with self.subTest(code=code):
                cls_ = classify(f"ocr_exit_{code}:whatever")
                self.assertEqual(cls_.ladder, ())
                self.assertTrue(cls_.cause.terminal)

    def test_unknown_codes_are_named_by_number_and_still_get_a_ladder(self):
        for code in (11, 42, 99):
            with self.subTest(code=code):
                cls_ = classify(f"ocr_exit_{code}:mystery")
                self.assertEqual(cls_.cause.name, f"unknown_exit_{code}")
                self.assertEqual(cls_.cause.terminal,
                                 f"unknown_exit_{code}_unrecoverable")
                self.assertEqual(cls_.exit_name, f"undocumented_{code}")
                self.assertTrue(cls_.ladder)

    def test_an_unknown_negative_code_still_yields_a_clean_token(self):
        # subprocess reports a signal-killed child as a negative code, and
        # every terminal token in the manifest is a slug.
        cls_ = classify("ocr_exit_-9:killed")
        self.assertEqual(cls_.cause.name, "unknown_exit_neg9")
        self.assertTrue(SLUG.fullmatch(cls_.cause.terminal))
        self.assertTrue(cls_.ladder)

    def test_every_cause_terminal_token_is_a_slug(self):
        for code in list(rescue.OCR_EXIT_NAMES) + [-9, -1, 42, 137, 139]:
            with self.subTest(code=code):
                cls_ = classify(f"ocr_exit_{code}:tail")
                self.assertTrue(SLUG.fullmatch(cls_.cause.terminal))

    def test_container_codes_are_not_read_as_ocrmypdf_codes(self):
        # 137/139 are docker's 128+signal and -1 is ws13_worker.ocr()'s own
        # budget sentinel; mapping either onto ocrmypdf's table would send
        # the ladder somewhere useless.
        self.assertEqual(classify("ocr_exit_137:").cause.name,
                         "container_oom_killed")
        self.assertEqual(classify("ocr_exit_139:").cause.name,
                         "container_segfault")
        budget = classify("ocr_exit_-1:ocrmypdf killed after 3300s "
                          "(document budget)")
        self.assertEqual(budget.cause.name, "doc_budget_timeout")
        self.assertEqual(budget.exit_name, "doc_budget_timeout")

    def test_errors_that_are_not_ocr_exits_classify_by_text(self):
        cases = {
            "integrity_mismatch": "integrity_mismatch",
            "born_digital_no_extractable_text": "born_digital_no_text",
            "stale_running_reaped: no manifest write for 9.1h":
                "stale_running_reaped",
            "conf_unavailable:no_docker": "unclassified",
            "": "unclassified",
            None: "unclassified",
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                cls_ = classify(error)
                self.assertIsNone(cls_.exit_code)
                self.assertIsNone(cls_.exit_name)
                self.assertEqual(cls_.cause.name, expected)
                self.assertEqual(cls_.ladder, ())

    def test_the_recorded_reasons_all_resolve_to_a_remedy_or_a_dead_end(self):
        got = {sha: classify(err, sha=sha) for sha, err in RECORDED.items()}
        # Every one of the seven gets a ladder: none of them is a node
        # defect, and none falls through to 'unclassified'.
        for sha, cls_ in got.items():
            with self.subTest(sha=sha):
                self.assertNotEqual(cls_.cause.name, "unclassified")
                self.assertTrue(cls_.ladder)
        causes = {sha: cls_.cause.name for sha, cls_ in got.items()}
        # The five exit-4 rows are a PDF/A failure, not the tesseract
        # failure the handoff described -- except where the stderr tail
        # names a damaged input, which outranks the exit code.
        exit_four = [sha for sha, err in RECORDED.items()
                     if err.startswith("ocr_exit_4")]
        self.assertEqual(len(exit_four), 5)
        for sha in exit_four:
            with self.subTest(sha=sha):
                self.assertIn(causes[sha],
                              ("invalid_output_pdf", "damaged_jpeg_stream"))
                self.assertNotEqual(causes[sha], "child_process_error")
        self.assertEqual(causes["5a3fed772044"], "child_process_error")
        self.assertEqual(causes["5c991bfa4e90"], "child_process_error")


class StderrPatternTests(unittest.TestCase):
    def test_damaged_jpeg_outranks_the_exit_code(self):
        for code in (4, 7, 15):
            with self.subTest(code=code):
                cls_ = classify(f"ocr_exit_{code}:invalid jpeg data reading "
                                f"stream (object 24)")
                self.assertIn("damaged_jpeg_stream", cls_.hints)
                self.assertEqual(cls_.cause.name, "damaged_jpeg_stream")
                self.assertEqual(cls_.cause.terminal,
                                 "damaged_jpeg_stream_unrecoverable")
                self.assertIsNotNone(cls_.ladder[0].repair)

    def test_pattern_matching_is_case_insensitive(self):
        cls_ = classify("ocr_exit_4:INVALID JPEG DATA READING STREAM")
        self.assertEqual(cls_.cause.name, "damaged_jpeg_stream")

    def test_a_truncated_image_is_the_same_damaged_input(self):
        # e9c662a's other named cause: OSError: image file is truncated.
        cls_ = classify("ocr_exit_4:OSError: image file is truncated "
                        "(2 bytes not processed)")
        self.assertEqual(cls_.cause.name, "damaged_jpeg_stream")

    def test_each_geometry_warning_is_matched(self):
        for tail in ("improbable aspect ratio",
                     "1 page is facing UP",
                     "[tesseract] Too few characters. Skipping this page",
                     "skipping all processing on this page"):
            with self.subTest(tail=tail):
                self.assertIn("page_geometry",
                              rescue.stderr_hints(tail))

    def test_oversized_image_warnings_are_matched(self):
        for tail in ("[tesseract] Image too large: (52454, 9707)",
                     "Image size (509650176 pixels) exceeds limit of "
                     "500000000 pixels"):
            with self.subTest(tail=tail):
                self.assertIn("oversized_image", rescue.stderr_hints(tail))

    def test_a_clean_tail_matches_nothing(self):
        self.assertEqual(rescue.stderr_hints("The generated PDF is INVALID"),
                         ())
        self.assertEqual(rescue.stderr_hints(""), ())

    def test_geometry_extends_the_ladder_without_reordering_it(self):
        plain = classify("ocr_exit_4:The generated PDF is INVALID")
        hinted = classify("ocr_exit_4:improbable aspect ratio; The "
                          "generated PDF is INVALID")
        plain_names = [r.name for r in plain.ladder]
        hinted_names = [r.name for r in hinted.ladder]
        self.assertEqual(hinted_names[:len(plain_names)], plain_names)
        self.assertIn("rotate_threshold_low", hinted_names)
        self.assertIn("pagesegmode_11_sparse", hinted_names)

    def test_a_hint_never_duplicates_a_rung_the_cause_already_has(self):
        cls_ = classify("ocr_exit_7:[tesseract] Image too large: "
                        "(52454, 9707); Too few characters")
        names = [r.name for r in cls_.ladder]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("skip_oversized_pages", names)

    def test_a_hint_never_invents_a_ladder_for_a_node_defect(self):
        # A page warning printed on the way to a missing binary is still a
        # missing binary; three retries of that document learn nothing.
        cls_ = classify("ocr_exit_3:improbable aspect ratio; "
                        "MissingDependencyError")
        self.assertEqual(cls_.cause.name, "missing_dependency")
        self.assertEqual(cls_.ladder, ())


class ArgumentBuildingTests(unittest.TestCase):
    def test_force_ocr_drops_the_workers_skip_text(self):
        # ocrmypdf exits 1 BAD_ARGS on that pair, so the rung would be
        # recorded as a remedy that failed when it had never run.
        merged = rescue.merge_args(rescue.BASE_ARGS,
                                   ("--output-type", "pdf", "--force-ocr"))
        self.assertNotIn("--skip-text", merged)
        self.assertIn("--force-ocr", merged)
        self.assertIn("--deskew", merged)

    def test_no_ladder_rung_ever_combines_the_conflicting_flags(self):
        for cause in rescue.CAUSES.values():
            for remedy in cause.ladder:
                with self.subTest(remedy=remedy.name):
                    argv = rescue.ocr_argv(remedy)
                    if "--force-ocr" in argv:
                        self.assertNotIn("--skip-text", argv)
                        self.assertNotIn("--redo-ocr", argv)

    def test_an_overridden_flag_appears_once_with_the_rungs_value(self):
        merged = rescue.merge_args(("--output-type", "pdfa", "--deskew"),
                                   ("--output-type", "pdf"))
        self.assertEqual(merged.count("--output-type"), 1)
        self.assertNotIn("pdfa", merged)
        self.assertIn("--deskew", merged)

    def test_ocr_argv_is_shaped_like_the_workers_own_invocation(self):
        argv = rescue.ocr_argv(rescue.R_PDF_OUTPUT)
        self.assertIn("--sidecar", argv)
        self.assertIn("--jobs", argv)
        self.assertEqual(argv[-2:], ["/work/in.pdf", "/work/out.pdf"])

    def test_a_rung_can_override_jobs_and_is_not_overridden_back(self):
        # ocrmypdf's argparse takes the LAST value, so a trailing --jobs 2
        # silently cancelled the one remedy for an OOM-killed container.
        argv = rescue.ocr_argv(rescue.R_LOW_MEMORY)
        self.assertEqual(argv.count("--jobs"), 1)
        self.assertEqual(argv[argv.index("--jobs") + 1], "1")

    def test_no_rung_ever_sets_the_same_flag_twice(self):
        for cause in rescue.CAUSES.values():
            for remedy in cause.ladder:
                argv = rescue.ocr_argv(remedy)
                flags = [a for a in argv if a.startswith("--")]
                with self.subTest(remedy=remedy.name):
                    self.assertEqual(len(flags), len(set(flags)))

    def test_repair_argv_names_its_tool_and_rewrites_the_input(self):
        tool, argv = rescue.repair_argv(rescue.R_QPDF_REPAIR)
        self.assertEqual(tool, "qpdf")
        self.assertIn("/work/in.pdf", argv)
        self.assertIn("/work/repaired.pdf", argv)
        self.assertIsNone(rescue.repair_argv(rescue.R_PDF_OUTPUT))

    def test_a_repair_rung_ocrs_the_repaired_file_not_the_original(self):
        # One rule in one place: a plan that says it will OCR in.pdf while
        # the command it prints OCRs repaired.pdf cannot be checked by the
        # operator it was printed for.
        self.assertEqual(rescue.ocr_input_name(rescue.R_QPDF_REPAIR),
                         "repaired.pdf")
        self.assertEqual(rescue.ocr_input_name(rescue.R_PDF_OUTPUT), "in.pdf")
        argv = rescue.ocr_argv(rescue.R_QPDF_REPAIR,
                               in_name=rescue.ocr_input_name(
                                   rescue.R_QPDF_REPAIR))
        self.assertIn("/work/repaired.pdf", argv)
        self.assertNotIn("/work/in.pdf", argv)


class LadderEscalationTests(unittest.TestCase):
    def test_the_first_rung_that_works_stops_the_ladder(self):
        cls_ = classify("ocr_exit_4:The generated PDF is INVALID")
        executor = FakeExecutor({"output_type_pdf": (True, "text_9100_chars")})
        with redirect_stdout(io.StringIO()):
            attempts, winner = rescue.run_ladder(executor, doc(), cls_.ladder)
        self.assertEqual(executor.calls, ["output_type_pdf"])
        self.assertEqual(winner.name, "output_type_pdf")
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].ok)

    def test_escalation_is_in_ladder_order_and_only_on_failure(self):
        cls_ = classify("ocr_exit_7:tesseract died")
        executor = FakeExecutor({"pagesegmode_1_osd": (True, "text_40_chars")})
        with redirect_stdout(io.StringIO()):
            attempts, winner = rescue.run_ladder(executor, doc(), cls_.ladder)
        self.assertEqual(executor.calls,
                         ["tesseract_timeout_1800", "pagesegmode_1_osd"])
        self.assertEqual(winner.name, "pagesegmode_1_osd")
        self.assertEqual([a.ok for a in attempts], [False, True])

    def test_every_rung_is_tried_before_the_document_is_given_up_on(self):
        cls_ = classify("ocr_exit_7:tesseract died")
        executor = FakeExecutor()
        with redirect_stdout(io.StringIO()):
            attempts, winner = rescue.run_ladder(executor, doc(), cls_.ladder)
        self.assertIsNone(winner)
        self.assertEqual(executor.calls, [r.name for r in cls_.ladder])
        self.assertEqual(len(attempts), len(cls_.ladder))

    def test_each_attempt_records_what_was_tried_and_what_happened(self):
        cls_ = classify("ocr_exit_4:The generated PDF is INVALID")
        executor = FakeExecutor(
            {"output_type_pdf": (False, "ocr_exit_4"),
             "output_type_pdf_no_optimize": (False, "empty_text_0_chars"),
             "force_ocr_rasterised": (False, "ocr_timeout")})
        with redirect_stdout(io.StringIO()):
            attempts, _ = rescue.run_ladder(executor, doc(), cls_.ladder)
        recorded = rescue.trail(attempts)
        self.assertEqual(
            recorded,
            "output_type_pdf=ocr_exit_4,"
            "output_type_pdf_no_optimize=empty_text_0_chars,"
            "force_ocr_rasterised=ocr_timeout")

    def test_an_empty_ladder_runs_nothing(self):
        cls_ = classify("ocr_exit_3:MissingDependencyError")
        executor = FakeExecutor()
        with redirect_stdout(io.StringIO()):
            attempts, winner = rescue.run_ladder(executor, doc(), cls_.ladder)
        self.assertEqual(executor.calls, [])
        self.assertEqual(attempts, [])
        self.assertIsNone(winner)


class TerminalReasonTests(unittest.TestCase):
    def exhaust(self, error):
        cls_ = classify(error)
        attempts = [rescue.Attempt(r, False, "ocr_exit_4") for r in cls_.ladder]
        return cls_, rescue.terminal_reason(cls_.cause, attempts)

    def test_the_damaged_stream_dead_end_is_the_classified_token(self):
        _cls, reason = self.exhaust("ocr_exit_4:invalid jpeg data reading "
                                    "stream (object 480)")
        self.assertTrue(reason.startswith("rescue_exhausted:"))
        self.assertIn("damaged_jpeg_stream_unrecoverable", reason)
        self.assertIn("qpdf_decode_repair", reason)
        self.assertIn("ghostscript_rewrite", reason)

    def test_every_cause_produces_a_greppable_single_line_reason(self):
        for cause in rescue.CAUSES.values():
            with self.subTest(cause=cause.name):
                attempts = [rescue.Attempt(r, False, "ocr_exit_4")
                            for r in cause.ladder]
                reason = rescue.terminal_reason(cause, attempts)
                self.assertNotIn("\n", reason)
                self.assertLessEqual(len(reason), rescue.REASON_MAX)
                self.assertIn(cause.terminal, reason)

    def test_a_traceback_can_never_reach_the_manifest(self):
        # The e9c662a regression in reverse: the detail of an attempt is a
        # token, so even a raw traceback handed to it comes out slugged.
        traceback = ('Traceback (most recent call last):\n  File '
                     '"/usr/lib/python3/ocrmypdf/_sync.py", line 402\n'
                     'PIL.Image.DecompressionBombError: Image size')
        attempts = [rescue.Attempt(rescue.R_PDF_OUTPUT, False, traceback)]
        reason = rescue.terminal_reason(
            rescue.CAUSES["invalid_output_pdf"], attempts)
        self.assertNotIn("\n", reason)
        self.assertNotIn('File "', reason)
        self.assertNotIn("Traceback (most", reason)
        self.assertIn("invalid_output_pdf_unrecoverable", reason)
        self.assertTrue(rescue.UNSAFE_CHARS.search(reason) is None)

    def test_a_cause_with_no_ladder_says_so_instead_of_claiming_retries(self):
        reason = rescue.terminal_reason(rescue.CAUSES["missing_dependency"],
                                        [])
        self.assertTrue(reason.startswith("rescue_none:"))
        self.assertIn("missing_dependency_not_a_document_defect", reason)

    def test_success_records_the_winning_flags_for_the_requeue(self):
        attempts = [rescue.Attempt(rescue.R_PDF_OUTPUT, True, "text_910_chars")]
        reason = rescue.success_reason(rescue.R_PDF_OUTPUT, attempts)
        self.assertTrue(reason.startswith("rescue_ok:output_type_pdf:"))
        self.assertIn("--output-type pdf", reason)

    def test_a_repaired_input_says_the_sha256_changed(self):
        attempts = [rescue.Attempt(rescue.R_QPDF_REPAIR, True, "text_88_chars")]
        reason = rescue.readmit_reason(rescue.R_QPDF_REPAIR, attempts)
        self.assertTrue(reason.startswith("rescue_repaired:"))
        self.assertIn("sha256", reason)

    def test_safe_collapses_anything_that_is_not_a_token(self):
        self.assertEqual(rescue.safe("a\nb\tc"), "a b c")
        self.assertEqual(rescue.token("PIL.Image.DecompressionBombError!"),
                         "pil_image_decompressionbomberror")
        self.assertEqual(rescue.token(""), "unknown")
        self.assertEqual(len(rescue.safe("x" * 900)), rescue.REASON_MAX)


class SelectorTests(unittest.TestCase):
    def test_no_selector_refuses_to_run(self):
        conn = FakeConn([doc()])
        with self.assertRaises(SystemExit) as caught:
            run_main(conn, ["--dsn", "x"])
        self.assertIn("no selector", str(caught.exception))
        self.assertIsNone(conn.select_sql)

    def test_all_failed_excludes_rows_this_tool_already_settled(self):
        conn = FakeConn([doc()])
        run_main(conn, ["--dsn", "x", "--all-failed"])
        self.assertIn("NOT (m.status = ANY(%s))", conn.select_sql)
        # 'done' is now excluded by the status whitelist rather than by
        # a NOT-list, so the remaining NOT-list is the settled statuses
        # this tool itself writes.
        self.assertIn([s for s in rescue.SETTLED_STATUSES if s != 'done'],
                      conn.select_params)
        self.assertIn(list(rescue.RESCUABLE_STATUSES), conn.select_params)

    def test_include_settled_reopens_them(self):
        conn = FakeConn([doc()])
        run_main(conn, ["--dsn", "x", "--all-failed", "--include-settled"])
        # --include-settled drops the NOT-list entirely; the status
        # whitelist already keeps 'done' out.
        self.assertNotIn([s for s in rescue.SETTLED_STATUSES if s != 'done'],
                         conn.select_params)
        self.assertIn(list(rescue.RESCUABLE_STATUSES), conn.select_params)

    def test_sha_selector_binds_a_prefix_and_rejects_an_unset_variable(self):
        conn = FakeConn([doc()])
        run_main(conn, ["--dsn", "x", "--sha", "87473fe10d90"])
        self.assertIn("LIKE", conn.select_sql)
        self.assertIn("87473fe10d90%", conn.select_params)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                rescue.parse_args(["--dsn", "x", "--sha", ""])

    def test_limit_caps_the_documents_not_the_query(self):
        conn = FakeConn([doc(sha="aa" * 8), doc(sha="bb" * 8)])
        code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed",
                                       "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertNotIn("LIMIT", conn.select_sql.upper())
        self.assertIn("1 document(s) to rescue", out)


class DryRunTests(unittest.TestCase):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        executor = FakeExecutor()
        conn = FakeConn([doc(error=RECORDED["87473fe10d90"])])
        code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed"],
                                executor=executor)
        self.assertEqual(code, 0)
        self.assertEqual(conn.updates, [])
        self.assertEqual(executor.calls, [])
        self.assertIn("dry run", out)

    def test_the_plan_names_the_exit_code_the_cause_and_every_rung(self):
        conn = FakeConn([doc(error=RECORDED["5c991bfa4e90"], status="error")])
        _code, out, _err = run_main(conn, ["--dsn", "x", "--all-failed"])
        self.assertIn("exit=7 child_process_error", out)
        self.assertIn("cause=child_process_error", out)
        self.assertIn("oversized_image", out)
        for rung in ("tesseract_timeout_1800", "pagesegmode_1_osd",
                     "pagesegmode_11_sparse", "skip_oversized_pages"):
            self.assertIn(rung, out)
        self.assertIn("child_process_error_unrecoverable", out)

    def test_emit_mode_prints_commands_an_operator_can_paste(self):
        conn = FakeConn([doc(error="ocr_exit_4:The generated PDF is INVALID",
                             s3_key="ws12/originals/ab/abc.pdf")])
        _code, out, _err = run_main(conn, ["--dsn", "x", "--all-failed",
                                           "--exec", "emit"])
        self.assertIn("docker run --rm --user 0:0", out)
        self.assertIn(rescue.OCR_IMAGE, out)
        self.assertIn("aws s3 cp s3://$WS13_BUCKET/ws12/originals/ab/abc.pdf",
                      out)
        # Fetched once for the document, not once per rung.
        self.assertEqual(out.count("aws s3 cp"), 1)

    def test_an_emitted_repair_rung_ocrs_what_the_repair_produced(self):
        conn = FakeConn([doc(error=RECORDED["a0d005469420"])])
        _code, out, _err = run_main(conn, ["--dsn", "x", "--all-failed",
                                           "--exec", "emit"])
        self.assertIn("--entrypoint qpdf", out)
        self.assertIn("/work/repaired.pdf /work/out.pdf", out)
        # No rung may hand ocrmypdf the input the repair was meant to fix.
        for line in out.splitlines():
            if "ocrmypdf:latest --deskew" in line:
                self.assertIn("/work/repaired.pdf", line)

    def test_docker_mode_without_apply_still_runs_nothing(self):
        executor = FakeExecutor()
        conn = FakeConn([doc()])
        code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed",
                                       "--exec", "docker"],
                                executor=executor)
        self.assertEqual(code, 0)
        self.assertEqual(executor.calls, [])
        self.assertEqual(conn.updates, [])
        self.assertIn("dry run", out)

    def test_apply_refuses_in_emit_mode(self):
        conn = FakeConn([doc()])
        with self.assertRaises(SystemExit) as caught:
            run_main(conn, ["--dsn", "x", "--all-failed", "--apply"])
        self.assertIn("--exec docker", str(caught.exception))
        self.assertEqual(conn.updates, [])

    def test_requeue_refuses_without_apply(self):
        conn = FakeConn([doc()])
        with self.assertRaises(SystemExit) as caught:
            run_main(conn, ["--dsn", "x", "--all-failed", "--requeue"])
        self.assertIn("--apply", str(caught.exception))


class StuckRunningTests(unittest.TestCase):
    def test_a_running_row_is_deferred_to_the_reaper_not_reclassified(self):
        # 5c991bfa4e90: a worker was killed mid-document, so the row says
        # 'running' forever. ws13_reap_stale.py owns that; this tool does not
        # reimplement the live-worker check or the age floor.
        conn = FakeConn([doc(sha="5c991bfa4e90ab", status="running",
                             error=RECORDED["5c991bfa4e90"])])
        with mock.patch.object(rescue.ws13_reap_stale, "main") as reaper:
            code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed"])
        self.assertEqual(code, 0)
        reaper.assert_not_called()
        self.assertEqual(conn.updates, [])
        self.assertIn("ws13_reap_stale.py", out)
        self.assertIn("not classifiable", out)

    def test_reap_running_delegates_with_the_sha_and_the_window(self):
        conn = FakeConn([doc(sha="5c991bfa4e90ab", status="running")])
        with mock.patch.object(rescue.ws13_reap_stale, "main",
                               return_value=0) as reaper:
            run_main(conn, ["--dsn", "x", "--all-failed", "--reap-running"])
        argv = reaper.call_args[0][0]
        self.assertIn("--sha", argv)
        self.assertIn("5c991bfa4e90ab", argv)
        self.assertIn("--older-than-hours", argv)
        # Without --apply the delegation stays a dry run there too.
        self.assertNotIn("--apply", argv)


class ApplyTests(unittest.TestCase):
    def test_a_rescued_document_is_recorded_with_a_compare_and_set(self):
        conn = FakeConn([doc(sha="87473fe10d90ab",
                             error=RECORDED["87473fe10d90"])])
        executor = FakeExecutor({"output_type_pdf": (True, "text_9100_chars")})
        code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed",
                                       "--exec", "docker", "--apply"],
                                executor=executor)
        self.assertEqual(code, 0)
        self.assertEqual(len(conn.updates), 1)
        status, reason, sha, prior = conn.updates[0]
        self.assertEqual(status, rescue.RESCUED_STATUS)
        self.assertEqual(sha, "87473fe10d90ab")
        self.assertEqual(prior, "error")     # the status that was read
        self.assertIn("rescue_ok:output_type_pdf", reason)
        self.assertIn("ws13_enqueue.py", out)

    def test_an_exhausted_ladder_ends_terminal_with_a_classified_reason(self):
        conn = FakeConn([doc(sha="a0d005469420ab",
                             error=RECORDED["a0d005469420"])])
        executor = FakeExecutor()
        code, _out, _err = run_main(conn, ["--dsn", "x", "--all-failed",
                                           "--exec", "docker", "--apply"],
                                    executor=executor)
        self.assertEqual(code, 1)           # a dead end is not a clean run
        status, reason, _sha, _prior = conn.updates[0]
        self.assertEqual(status, rescue.TERMINAL_STATUS)
        self.assertIn("damaged_jpeg_stream_unrecoverable", reason)
        self.assertNotIn("\n", reason)

    def test_a_repaired_input_is_never_requeued_under_the_same_sha(self):
        # ws13_worker.py refetches the original by its content-addressed key
        # and verifies sha256, so repaired bytes would come back
        # 'integrity_mismatch'. It needs WS12 re-admission instead.
        conn = FakeConn([doc(sha="a0d005469420ab",
                             error=RECORDED["a0d005469420"])])
        executor = FakeExecutor({"qpdf_decode_repair": (True, "text_512_chars")})
        enqueue = mock.MagicMock(return_value=0)
        code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed",
                                       "--exec", "docker", "--apply",
                                       "--requeue"],
                                executor=executor, enqueue=enqueue)
        self.assertEqual(code, 0)
        status, reason, _sha, _prior = conn.updates[0]
        self.assertEqual(status, rescue.READMIT_STATUS)
        self.assertIn("rescue_repaired:qpdf_decode_repair", reason)
        enqueue.assert_not_called()

    def test_a_row_that_moved_under_us_is_reported_not_overwritten(self):
        conn = FakeConn([doc(sha="87473fe10d90ab")], rowcounts=[0])
        executor = FakeExecutor({"output_type_pdf": (True, "text_10_chars")})
        enqueue = mock.MagicMock(return_value=0)
        code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed",
                                       "--exec", "docker", "--apply",
                                       "--requeue"],
                                executor=executor, enqueue=enqueue)
        self.assertEqual(code, 1)
        self.assertIn("NOT recorded", out)
        # And it must not be requeued on the strength of a write that missed,
        # nor counted as rescued in the closing summary.
        enqueue.assert_not_called()
        self.assertIn("rescued 0,", out)
        self.assertIn("not recorded 1", out)

    def test_requeue_hands_the_document_back_to_ws13_enqueue(self):
        conn = FakeConn([doc(sha="87473fe10d90ab")])
        executor = FakeExecutor({"output_type_pdf": (True, "text_10_chars")})
        enqueue = mock.MagicMock(return_value=0)
        code, out, _ = run_main(conn, ["--dsn", "x", "--all-failed",
                                       "--exec", "docker", "--apply",
                                       "--requeue", "--fleet-args-set",
                                       "--queue-url",
                                       "https://sqs/ws13-ocr"],
                                executor=executor, enqueue=enqueue)
        self.assertEqual(code, 0)
        argv = enqueue.call_args[0][0]
        self.assertIn("--sha", argv)
        self.assertIn("87473fe10d90ab", argv)
        self.assertIn("--force", argv)
        self.assertIn("https://sqs/ws13-ocr", argv)
        # The flags the worker needs are fleet-wide, so say so.
        self.assertIn("WS13_OCR_EXTRA_ARGS", out)

    def test_winners_with_different_flags_are_not_requeued_together(self):
        # WS13_OCR_EXTRA_ARGS is read from the WORKER's environment and can
        # hold one arg set at a time; requeueing two together would run one
        # of them with the wrong flags.
        conn = FakeConn([doc(sha="87473fe10d90ab", error="ocr_exit_4:x"),
                         doc(sha="5a3fed772044ab", error="ocr_exit_7:x")])
        executor = FakeExecutor({"output_type_pdf": (True, "text_10_chars"),
                                 "tesseract_timeout_1800": (True, "text_9")})
        enqueue = mock.MagicMock(return_value=0)
        code, _out, err = run_main(conn, ["--dsn", "x", "--all-failed",
                                          "--exec", "docker", "--apply",
                                          "--requeue"],
                                   executor=executor, enqueue=enqueue)
        self.assertEqual(code, 1)
        enqueue.assert_not_called()
        self.assertIn("fleet-wide", err)

    def test_requeue_mixed_accepts_the_risk_and_calls_enqueue_per_group(self):
        conn = FakeConn([doc(sha="87473fe10d90ab", error="ocr_exit_4:x"),
                         doc(sha="5a3fed772044ab", error="ocr_exit_7:x")])
        executor = FakeExecutor({"output_type_pdf": (True, "text_10_chars"),
                                 "tesseract_timeout_1800": (True, "text_9")})
        enqueue = mock.MagicMock(return_value=0)
        code, _out, _err = run_main(conn, ["--dsn", "x", "--all-failed",
                                           "--exec", "docker", "--apply",
                                           "--requeue", "--requeue-mixed",
                                           "--fleet-args-set"],
                                    executor=executor, enqueue=enqueue)
        self.assertEqual(code, 0)
        self.assertEqual(enqueue.call_count, 2)

    def test_enqueue_arg_is_forwarded_verbatim(self):
        # A forwarded flag needs the '=' form: argparse reads a bare
        # "--harvest-manifest" as an option of this tool and exits 2.
        conn = FakeConn([doc(sha="87473fe10d90ab")])
        executor = FakeExecutor({"output_type_pdf": (True, "text_10_chars")})
        enqueue = mock.MagicMock(return_value=0)
        run_main(conn, ["--dsn", "x", "--all-failed", "--exec", "docker",
                        "--apply", "--requeue", "--fleet-args-set",
                        "--enqueue-arg=--harvest-manifest", "--enqueue-arg",
                        "var/ws12/manifest.jsonl"],
                 executor=executor, enqueue=enqueue)
        argv = enqueue.call_args[0][0]
        self.assertEqual(argv[-2:], ["--harvest-manifest",
                                     "var/ws12/manifest.jsonl"])
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                rescue.parse_args(["--dsn", "x", "--enqueue-arg",
                                   "--harvest-manifest"])

    def test_a_cause_with_no_ladder_is_recorded_without_a_single_run(self):
        conn = FakeConn([doc(error="integrity_mismatch")])
        executor = FakeExecutor()
        code, _out, _err = run_main(conn, ["--dsn", "x", "--all-failed",
                                           "--exec", "docker", "--apply"],
                                    executor=executor)
        self.assertEqual(code, 1)
        self.assertEqual(executor.calls, [])
        status, reason, _sha, _prior = conn.updates[0]
        self.assertEqual(status, rescue.TERMINAL_STATUS)
        self.assertIn("integrity_mismatch_not_an_ocr_failure", reason)


class SelectorBoundsTest(unittest.TestCase):
    """--all-failed must not mean "everything that is not done".

    The manifest holds 12,519 map_queue rows -- map plates parked out of the
    text-OCR path with an inventory reason, never an ocrmypdf exit code --
    plus 1 inventory_error. Selecting on "not settled" alone matched 12,537
    rows against a tool that reasons about 7, and --apply would have rewritten
    every parked map plate to 'unrescuable', a status SETTLED_STATUSES then
    excludes forever.
    """

    def where_for(self, argv):
        captured = {}

        class Conn:
            def execute(self, sql, params):
                captured['sql'] = sql
                captured['params'] = params
                return types.SimpleNamespace(fetchall=lambda: [])

        rescue.select(Conn(), rescue.parse_args(argv))
        return captured

    def test_all_failed_is_bounded_by_status_and_class(self):
        got = self.where_for(['--all-failed'])
        flat = [value for group in got['params'] for value in group]
        self.assertIn('error', flat)
        self.assertIn('running', flat)
        self.assertIn('ocr_queue', flat)
        self.assertNotIn('map_queue', flat)
        self.assertIn('m.doc_class = ANY(%s)', got['sql'])

    def test_map_queue_is_not_selectable_by_default(self):
        self.assertNotIn('map_queue', rescue.RESCUABLE_STATUSES)
        self.assertEqual(rescue.RESCUABLE_CLASSES, ('ocr_queue',))
        args = rescue.parse_args(['--all-failed'])
        self.assertEqual(args.cls, ['ocr_queue'])


class RequeueSafetyTest(unittest.TestCase):
    """WS13_OCR_EXTRA_ARGS is appended to ws13_worker.ocr()'s own BASE_ARGS,
    not merged with them, and it is fleet-wide."""

    def _args(self, **over):
        args = rescue.parse_args(['--all-failed', '--dsn', 'postgresql://t/t'])
        for key, value in over.items():
            setattr(args, key, value)
        return args

    def _winner(self, ocr_args):
        doc = types.SimpleNamespace(sha256='a' * 64)
        return [(doc, types.SimpleNamespace(ocr_args=ocr_args))]

    def test_a_winner_that_contradicts_the_worker_base_is_refused(self):
        """--force-ocr beside the worker's --skip-text is exit 1 BAD_ARGS --
        for every document the fleet touches while the variable is set."""
        sent = []
        with mock.patch.object(rescue.ws13_enqueue, 'main',
                               side_effect=lambda a: sent.append(a) or 0):
            rc = rescue.requeue(
                self._args(fleet_args_set=True),
                self._winner(('--output-type', 'pdf', '--force-ocr')))
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [], 'nothing may be enqueued for that winner')

    def test_an_expressible_winner_is_not_sent_until_the_fleet_carries_it(self):
        sent = []
        with mock.patch.object(rescue.ws13_enqueue, 'main',
                               side_effect=lambda a: sent.append(a) or 0):
            rc = rescue.requeue(self._args(fleet_args_set=False),
                                self._winner(('--output-type', 'pdf')))
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [], 'workers long-poll; sending early '
                                   'reproduces the original failure')

    def test_an_expressible_winner_sends_once_acknowledged(self):
        sent = []
        with mock.patch.object(rescue.ws13_enqueue, 'main',
                               side_effect=lambda a: sent.append(a) or 0):
            rc = rescue.requeue(self._args(fleet_args_set=True),
                                self._winner(('--output-type', 'pdf')))
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn('--force', sent[0])


if __name__ == "__main__":
    unittest.main()
