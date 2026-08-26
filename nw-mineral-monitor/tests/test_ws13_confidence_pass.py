"""Tests for the WS13 confidence pass (pipelines/ws13_confidence_pass.py).

The defect being repaired: ws13_worker.page_confidences() rendered pages with
a pdftoppm that is not on PATH in the ocrmypdf image, so every render exited
rc=127 and all 760,059 ws13_pages rows still carry confidence IS NULL. The
pass under test fills the 323,059 ocr_queue pages, and the risk it carries is
not the measurement -- it is scope: replacing OCR text would re-chunk and
re-embed 852,027 chunks, so this program has to touch ws13_pages and nothing
else, and has to be shardable, resumable and bounded while doing it.

`FakeDB` implements just enough of ws13_pages / ws13_documents /
ws13_conf_skips -- the NULL predicate, the sha256 cursor, the modulo shard
partition, the terminal-only skips anti-join -- that the real
`run_shard` / `process_document` / `pending_documents` run unmodified against
it, and it records every statement so the write-scope test can assert over
what actually got executed rather than over what the source looks like.

`FakeDocker` stands in for `ws13_worker._try_docker`, so the real
`ws13_worker.render_argv()` argv and the real PNG globbing are exercised for
each of pdftoppm / pdftocairo / gs.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

os.environ.setdefault("WS13_DB_DSN", "postgresql://test/test")
os.environ.setdefault("WS13_BUCKET", "test-bucket")
# ws13_worker.py reads this at module scope; the confidence pass defaults it
# for the same reason and never uses it.
os.environ.setdefault("WS13_QUEUE_URL", "unused:test")

# The driver and the SDK are deployment dependencies of the worker fleet, not
# of the test host; every call through them is stubbed below.
for _name in ("psycopg", "boto3"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            _stub = types.ModuleType(_name)
            _stub.connect = mock.MagicMock()
            _stub.client = mock.MagicMock()
            sys.modules[_name] = _stub

import boto3                                                    # noqa: E402

with mock.patch.object(boto3, "client", return_value=mock.MagicMock()):
    import ws13_confidence_pass as cp                           # noqa: E402
    import ws13_worker as worker                                # noqa: E402


TSV_HEADER = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
              "left\ttop\twidth\theight\tconf\ttext")


def tsv(*confidences):
    """A tesseract `stdout tsv` dump whose word rows carry `confidences`."""
    lines = [TSV_HEADER]
    for i, conf in enumerate(confidences):
        lines.append(f"5\t1\t1\t1\t1\t{i + 1}\t0\t0\t10\t10\t{conf}\tword")
    return "\n".join(lines) + "\n"


def sha_for(i):
    """A real sha256 hex digest, so the shard arithmetic sees real entropy."""
    return hashlib.sha256(f"ws13-doc-{i}".encode()).hexdigest()


def sql_shard_of(sha, shards):
    """The shard predicate transcribed BY HAND from cp.SHARD_EXPR.

    Deliberately not a call to cp.shard_of: the point of the disjointness
    test is that the Python partition and the SQL partition are the same
    arithmetic, and a fake that just called shard_of() would be satisfied by
    any change to it.
    """
    if shards <= 1:
        return 0
    return int(sha[:8], 16) % shards


class FakeDB:
    """ws13_pages + ws13_documents + ws13_conf_skips, in memory."""

    def __init__(self, docs):
        # docs: {sha: pages}; every document is an ocr_queue document with a
        # searchable PDF, which is the population the pass selects.
        self.docs = {}
        self.pages = {}
        self.skips = {}
        self.statements = []
        for sha, pages in docs.items():
            self.docs[sha] = {
                "pages": pages, "cls": "ocr_queue",
                "key": f"ws13/searchable/{sha[:2]}/{sha}/searchable.pdf"}
            for page in range(1, pages + 1):
                self.pages[(sha, page)] = {"confidence": None,
                                           "low_confidence": None}

    def terminally_skipped(self, sha, page):
        return self.skips.get((sha, page)) in cp.TERMINAL_REASONS

    def pending(self, sha):
        return sorted(
            page for (s, page) in self.pages
            if s == sha and self.pages[(s, page)]["confidence"] is None
            and not self.terminally_skipped(s, page))

    def measured(self):
        return {k: v["confidence"] for k, v in self.pages.items()
                if v["confidence"] is not None}


class FakeConn:
    """Parses only the statements the confidence pass issues."""

    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._result = []
        self.closed = False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.db.statements.append((s, params))
        self._result = []
        self.rowcount = 0
        if s.startswith("CREATE TABLE IF NOT EXISTS ws13_conf_skips"):
            return self
        if s.startswith("INSERT INTO ws13_conf_skips"):
            self.db.skips[(params[0], params[1])] = params[2]
            self.rowcount = 1
            return self
        if s.startswith("UPDATE ws13_pages SET confidence"):
            conf, _conf2, threshold, sha, page = params
            row = self.db.pages.get((sha, page))
            if row is not None and row["confidence"] is None:
                row["confidence"] = conf
                row["low_confidence"] = conf < threshold
                self.rowcount = 1
            return self
        if s.startswith("SELECT DISTINCT d.sha256, d.searchable_key"):
            return self._documents(s, params)
        if s.startswith("SELECT p.page FROM ws13_pages p"):
            self._result = [(page,) for page in self.db.pending(params[0])]
            return self
        if s.startswith("SELECT COUNT(*), COUNT(DISTINCT p.sha256)"):
            return self._remaining(s, params)
        if s.startswith("SELECT p.sha256, COUNT(*)"):
            self._result = [(sha, len(self.db.pending(sha)))
                            for sha in sorted(self.db.docs)
                            if self.db.pending(sha)]
            return self
        raise AssertionError(f"unhandled SQL: {s[:160]}")

    def _shard_filter(self, sql, shards, shard):
        if "mod(" not in sql:
            return lambda sha: True
        return lambda sha: sql_shard_of(sha, shards) == shard

    def _documents(self, sql, params):
        if "mod(" in sql:
            cursor, shards, shard, limit = params
        else:
            cursor, limit = params
            shards, shard = 1, 0
        keep = self._shard_filter(sql, shards, shard)
        rows = []
        for sha in sorted(self.db.docs):
            if sha <= cursor or not keep(sha):
                continue
            pending = self.db.pending(sha)
            if not pending:
                continue
            doc = self.db.docs[sha]
            rows.append((sha, doc["key"], doc["pages"]))
            if len(rows) >= limit:
                break
        self._result = rows
        return self

    def _remaining(self, sql, params):
        shards, shard = (params if "mod(" in sql else (1, 0))
        keep = self._shard_filter(sql, shards, shard)
        shas = [sha for sha in self.db.docs if keep(sha)]
        pages = sum(len(self.db.pending(sha)) for sha in shas)
        docs = sum(1 for sha in shas if self.db.pending(sha))
        self._result = [(pages, docs)]
        return self

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        self.closed = True


def result(returncode=0, stdout=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                 stderr="")


class FakeDocker:
    """Stands in for ws13_worker._try_docker(args, work, timeout, entrypoint).

    Returns None on a simulated timeout, exactly as the real helper does, and
    writes a PNG into the work directory on a successful render so the real
    globbing in measure_page() is what finds it.
    """

    def __init__(self, confidences=(85,), blank_pages=(), no_image=(),
                 render_timeout=(), tess_timeout=(), tess_rc=(),
                 on_render=None):
        self.confidences = confidences
        self.blank_pages = set(blank_pages)
        self.no_image = set(no_image)
        self.render_timeout = set(render_timeout)
        self.tess_timeout = set(tess_timeout)
        self.tess_rc = set(tess_rc)
        self.on_render = on_render
        self.renders = []
        self.tsv_calls = []
        self.timeouts = []

    @staticmethod
    def _page_of(args):
        """Recover the page from whichever argv the renderer wanted.

        pdftoppm/pdftocairo take a /work/pgNNNNN prefix as the last operand
        and gs takes a full /work/pgNNNNN-1.png output path in the middle, so
        the token is found by shape rather than by position.
        """
        for arg in reversed(args):
            base = os.path.basename(str(arg))
            if base.startswith("pg"):
                return int(base[2:7]), base[:7]
        raise AssertionError(f"no page token in argv: {args}")

    def __call__(self, args, work, timeout, entrypoint):
        self.timeouts.append((entrypoint, timeout))
        page, base = self._page_of(args)
        if entrypoint == "tesseract":
            self.tsv_calls.append(page)
            if page in self.tess_timeout:
                return None
            if page in self.tess_rc:
                return result(returncode=1)
            if page in self.blank_pages:
                return result(stdout=tsv())
            return result(stdout=tsv(*self.confidences))
        self.renders.append(page)
        if self.on_render is not None:
            self.on_render()
        if page in self.render_timeout:
            return None
        if page in self.no_image:
            return result(returncode=1)
        with open(os.path.join(work, f"{base}-1.png"), "wb") as handle:
            handle.write(b"\x89PNG")
        return result()


class FakeClock:
    """A clock only the fake renderer advances.

    Driving it from the render rather than from the call count keeps the
    deadline assertions exact no matter how many other time.time() callers
    (tempfile, the stdlib) happen to read it.
    """

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _write_pdf(Bucket, Key, Filename):          # noqa: N803 - boto3 API
    """s3.download_file streams to a path; the pass then hands that path to
    a container and never reads the bytes itself."""
    with open(Filename, "wb") as handle:
        handle.write(b"%PDF-1.4 fake")


class ConfidenceTestCase(unittest.TestCase):
    """Base for the FakeDocker-driven tests.

    These describe the PER-PAGE path, which is still live code: it is what
    the batched path falls back to when a container fails in a way that says
    something about the node rather than about the pages. FakeDocker models
    one container per page, so the batched path -- one container per document
    -- is covered separately by BatchedMeasurementTest and
    BatchedDocumentTest rather than being bent through this harness.
    """

    batch_pages = False

    def setUp(self):
        self.batch = mock.patch.object(cp, "BATCH_PAGES", self.batch_pages)
        self.batch.start()
        self.addCleanup(self.batch.stop)
        self._scratch = tempfile.TemporaryDirectory()
        self.scratch = mock.patch.object(cp, "SCRATCH", self._scratch.name)
        self.scratch.start()
        self.addCleanup(self.scratch.stop)
        self.addCleanup(self._scratch.cleanup)
        self.s3 = mock.MagicMock()
        self.s3.download_file.side_effect = _write_pdf
        self.s3patch = mock.patch.object(cp, "s3", self.s3)
        self.s3patch.start()
        self.addCleanup(self.s3patch.stop)
        for key in cp.stats:
            cp.stats[key] = 0

    def run_shard(self, db, docker, shard=0, shards=1, limit=None,
                  doc_seconds=cp.DOC_SECONDS, renderer="pdftoppm"):
        conn = FakeConn(db)
        with mock.patch.object(worker, "_try_docker", docker):
            cp.run_shard(conn, shard, shards, renderer, limit=limit,
                         doc_seconds=doc_seconds)
        return conn


class TsvParseTest(unittest.TestCase):
    """The number this pass stores has to be the same quantity
    ws13_worker.CONF_THRESHOLD (60) and ESCALATE_THRESHOLD (45) were
    calibrated against, so the TSV parse is pinned here field by field."""

    def test_mean_of_the_word_confidence_column(self):
        self.assertEqual(cp.mean_word_confidence(tsv(90, 80)), 85.0)

    def test_structural_minus_one_rows_are_ignored(self):
        # tesseract writes -1 on the page/block/para/line rows; counting them
        # would drag every page toward -1.
        self.assertEqual(cp.mean_word_confidence(tsv(-1, 90, -1, 80)), 85.0)

    def test_empty_confidence_cells_are_ignored(self):
        self.assertEqual(cp.mean_word_confidence(tsv(90, "", 80)), 85.0)

    def test_zero_confidence_words_are_kept(self):
        # 0 is a real, terrible score. Dropping it would bias the page up.
        self.assertEqual(cp.mean_word_confidence(tsv(0, 100)), 50.0)

    def test_short_rows_are_ignored(self):
        text = TSV_HEADER + "\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\n"
        self.assertIsNone(cp.mean_word_confidence(text))

    def test_page_with_no_words_is_none_not_zero(self):
        """0.0 would read as 'measured, and as bad as a page can be' and
        would sort a blank scan ahead of pages that are genuinely
        unreadable. 3,091 documents in this corpus have zero characters."""
        self.assertIsNone(cp.mean_word_confidence(tsv()))
        self.assertIsNone(cp.mean_word_confidence(""))
        self.assertIsNone(cp.mean_word_confidence(tsv(-1, "")))

    def test_mean_is_rounded_to_one_decimal(self):
        self.assertEqual(cp.mean_word_confidence(tsv(90, 80, 71)), 80.3)


class ShardArithmeticTest(unittest.TestCase):
    def test_sql_expression_and_python_agree(self):
        self.assertIn("substr(p.sha256, 1, 8)", cp.SHARD_EXPR)
        self.assertIn("bit(32)::bigint", cp.SHARD_EXPR)
        for i in range(200):
            sha = sha_for(i)
            for shards in (1, 12, 16, 128, 640):
                self.assertEqual(cp.shard_of(sha, shards),
                                 sql_shard_of(sha, shards))

    def test_single_shard_owns_everything(self):
        self.assertEqual({cp.shard_of(sha_for(i), 1) for i in range(50)}, {0})


class ShardCoverageTest(ConfidenceTestCase):
    """Shards must be disjoint BY CONSTRUCTION. ws13_embed_backfill claimed
    work with FOR UPDATE SKIP LOCKED under autocommit, which released the
    lock before the work started and had 12 threads duplicating each other at
    a measured 6.15x. A modulo partition cannot do that."""

    def setUp(self):
        super().setUp()
        self.db = FakeDB({sha_for(i): 3 for i in range(400)})

    def _claimed(self, shards):
        conn = FakeConn(self.db)
        seen = {}
        for shard in range(shards):
            cursor = ""
            while True:
                rows = cp.pending_documents(conn, shard, shards, cursor)
                if not rows:
                    break
                for sha, _key, _pages in rows:
                    self.assertNotIn(
                        sha, seen,
                        f"{sha[:12]} claimed by shards {seen.get(sha)} "
                        f"and {shard}")
                    seen[sha] = shard
                cursor = rows[-1][0]
        return seen

    def test_shards_are_disjoint_and_cover_every_document(self):
        seen = self._claimed(12)
        self.assertEqual(len(seen), 400, "sharding must cover every document")
        self.assertEqual(len(set(seen.values())), 12,
                         "every shard should own some work at 400 documents")

    def test_single_shard_configuration_covers_everything(self):
        self.assertEqual(len(self._claimed(1)), 400)

    def test_every_page_of_every_shard_is_measured_exactly_once(self):
        shards = 8
        dockers = []
        for shard in range(shards):
            docker = FakeDocker(confidences=(72,))
            self.run_shard(self.db, docker, shard=shard, shards=shards)
            dockers.append(docker)
        self.assertEqual(len(self.db.measured()), 1200)
        rendered = [page for d in dockers for page in d.renders]
        self.assertEqual(len(rendered), 1200,
                         "a page rendered twice is a page paid for twice")


class ResumabilityTest(ConfidenceTestCase):
    """The work set is 'ocr_queue pages whose confidence IS NULL', so an
    interrupted run resumes for free -- provided nothing re-renders a page it
    already measured."""

    def setUp(self):
        super().setUp()
        self.sha = sha_for(1)
        self.db = FakeDB({self.sha: 10})

    def test_interrupted_run_resumes_at_the_first_unmeasured_page(self):
        clock = FakeClock()
        first = FakeDocker(on_render=lambda: clock.advance(400))
        with mock.patch.object(cp.time, "time", clock):
            # 1000 s of budget at 400 s a page measures three pages.
            self.run_shard(self.db, first, limit=1, doc_seconds=1000)
        self.assertEqual(first.renders, [1, 2, 3])
        self.assertEqual(len(self.db.measured()), 3)
        self.assertEqual(self.db.skips, {},
                         "a deadline is not a skip: those pages must stay in "
                         "the work set")

        second = FakeDocker()
        self.run_shard(self.db, second)
        self.assertEqual(second.renders, [4, 5, 6, 7, 8, 9, 10],
                         "a resumed run must not re-render measured pages")
        self.assertEqual(len(self.db.measured()), 10)

    def test_a_completed_document_is_not_rendered_again(self):
        self.run_shard(self.db, FakeDocker())
        again = FakeDocker()
        self.run_shard(self.db, again)
        self.assertEqual(again.renders, [])

    def test_a_page_under_the_escalate_threshold_is_marked_low_confidence(self):
        self.db = FakeDB({self.sha: 2})
        self.run_shard(self.db, FakeDocker(confidences=(30,)))
        row = self.db.pages[(self.sha, 1)]
        self.assertEqual(row["confidence"], 30.0)
        self.assertTrue(row["low_confidence"])
        # Imported, not restated: a row written here and a row written by
        # ws13_worker have to mean the same thing.
        self.assertEqual(cp.ESCALATE_THRESHOLD, worker.ESCALATE_THRESHOLD)
        self.assertEqual(cp.stats["below_escalate_threshold"], 2)
        self.assertEqual(cp.stats["below_conf_threshold"], 2)

    def test_a_weak_page_above_the_escalate_threshold_is_not_low_confidence(self):
        self.db = FakeDB({self.sha: 2})
        self.run_shard(self.db, FakeDocker(confidences=(55,)))
        row = self.db.pages[(self.sha, 1)]
        self.assertEqual(row["confidence"], 55.0)
        self.assertFalse(row["low_confidence"],
                         "55 is weak enough to re-OCR (CONF_THRESHOLD 60) but "
                         "not low_confidence (ESCALATE_THRESHOLD 45)")
        # The size of phase 2's work set is exactly what this pass exists to
        # discover, so the two thresholds are counted separately.
        self.assertEqual(cp.stats["below_conf_threshold"], 2)
        self.assertEqual(cp.stats["below_escalate_threshold"], 0)


class SkipDistinctionTest(ConfidenceTestCase):
    """A page that cannot be measured must be recorded so it is not retried
    forever -- but a five-second network fault must not abandon it for
    good."""

    def setUp(self):
        super().setUp()
        self.sha = sha_for(2)
        self.db = FakeDB({self.sha: 4})

    def test_no_image_is_terminal_and_leaves_the_work_set(self):
        self.run_shard(self.db, FakeDocker(no_image=[2]))
        self.assertEqual(self.db.skips[(self.sha, 2)], "no_image")
        self.assertIn("no_image", cp.TERMINAL_REASONS)
        self.assertEqual(self.db.pending(self.sha), [])
        again = FakeDocker()
        self.run_shard(self.db, again)
        self.assertEqual(again.renders, [],
                         "a terminal skip must not be retried")

    def test_blank_page_is_terminal_and_stores_no_confidence(self):
        self.run_shard(self.db, FakeDocker(blank_pages=[3]))
        self.assertEqual(self.db.skips[(self.sha, 3)], "no_words")
        self.assertIsNone(self.db.pages[(self.sha, 3)]["confidence"],
                          "a blank page is unmeasured, not 0.0")

    def test_render_timeout_is_transient_and_is_readmitted(self):
        self.run_shard(self.db, FakeDocker(render_timeout=[2, 4]))
        self.assertEqual(self.db.skips[(self.sha, 2)], "render_timeout")
        self.assertNotIn("render_timeout", cp.TERMINAL_REASONS)
        self.assertEqual(self.db.pending(self.sha), [2, 4])
        retry = FakeDocker()
        self.run_shard(self.db, retry)
        self.assertEqual(retry.renders, [2, 4])
        self.assertEqual(len(self.db.measured()), 4)

    def test_tesseract_timeout_is_transient(self):
        self.run_shard(self.db, FakeDocker(tess_timeout=[1]))
        self.assertEqual(self.db.skips[(self.sha, 1)], "tesseract_timeout")
        self.assertEqual(self.db.pending(self.sha), [1])

    def test_a_shard_of_unmeasurable_pages_still_terminates(self):
        """Transient skips make no progress; the sweep has to end anyway or
        the shard spins on rows only an operator can fix."""
        self.run_shard(self.db, FakeDocker(render_timeout=[1, 2, 3, 4]))
        self.assertEqual(len(self.db.skips), 4)
        self.assertEqual(self.db.measured(), {})

    def test_a_docker_level_failure_is_transient_not_a_blank_page(self):
        """rc=127 -- the command was not found in the image -- is the exact
        failure that made 760,059 pages look measured. Recording it against
        the page would write those pages off permanently for a fault that
        belongs to the node."""
        docker = FakeDocker()
        docker.no_image = set()

        def missing_binary(args, work, timeout, entrypoint):
            if entrypoint == "tesseract":
                return docker(args, work, timeout, entrypoint)
            return result(returncode=127)

        cp._logged_docker_env = False
        self.run_shard(self.db, missing_binary)
        self.assertEqual(self.db.skips[(self.sha, 1)], "docker_rc_127")
        self.assertNotIn("docker_rc_127", cp.TERMINAL_REASONS)
        self.assertEqual(self.db.pending(self.sha), [1, 2, 3, 4],
                         "a broken renderer must not write off the corpus")
        for code in (125, 126):
            self.assertIn(code, cp.DOCKER_ENV_CODES)

    def test_a_renderer_that_ran_and_produced_nothing_is_terminal(self):
        # rc=1 from a renderer that started fine is the page, not the node.
        self.run_shard(self.db, FakeDocker(no_image=[1]))
        self.assertEqual(self.db.skips[(self.sha, 1)], "no_image")
        self.assertIn("no_image", cp.TERMINAL_REASONS)

    def test_an_exception_on_one_document_does_not_end_the_shard(self):
        """A shard is one process on one node; losing all of it to one
        unexpected document is how a sweep silently stops short."""
        self.db = FakeDB({self.sha: 4, sha_for(21): 4})
        working = FakeDocker()
        calls = {"n": 0}

        def flaky(args, work, timeout, entrypoint):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("docker daemon went away")
            return working(args, work, timeout, entrypoint)

        self.run_shard(self.db, flaky)
        self.assertEqual(len(self.db.measured()), 8,
                         "the other document, and the retried one, must "
                         "still be measured")
        self.assertEqual(self.db.skips, {})

    def test_missing_searchable_pdf_is_terminal_for_every_page(self):
        self.s3.download_file.side_effect = _client_error("NoSuchKey")
        self.run_shard(self.db, FakeDocker())
        self.assertEqual(
            {page: self.db.skips[(self.sha, page)] for page in range(1, 5)},
            {page: "searchable_missing" for page in range(1, 5)})
        self.assertEqual(self.db.pending(self.sha), [])

    def test_s3_failure_is_transient_for_every_page(self):
        self.s3.download_file.side_effect = TimeoutError("read timed out")
        self.run_shard(self.db, FakeDocker())
        self.assertEqual(self.db.skips[(self.sha, 1)], "s3_error:TimeoutError")
        self.assertEqual(self.db.pending(self.sha), [1, 2, 3, 4])
        self.s3.download_file.side_effect = _write_pdf
        retry = FakeDocker()
        self.run_shard(self.db, retry)
        self.assertEqual(retry.renders, [1, 2, 3, 4])

    def test_page_beyond_the_document_is_terminal_without_rendering(self):
        self.db.docs[self.sha]["pages"] = 2
        docker = FakeDocker()
        self.run_shard(self.db, docker)
        self.assertEqual(docker.renders, [1, 2],
                         "a page the PDF does not have must not cost two "
                         "containers to disprove")
        self.assertEqual(self.db.skips[(self.sha, 3)], "page_absent")
        self.assertEqual(self.db.skips[(self.sha, 4)], "page_absent")


def _client_error(code):
    exc = RuntimeError(f"An error occurred ({code})")
    exc.response = {"Error": {"Code": code}}
    return exc


class DeadlineTest(ConfidenceTestCase):
    """The largest document is 1,407 pages. Without a per-document deadline
    it pins a whole shard for the length of the run."""

    def setUp(self):
        super().setUp()
        self.sha = sha_for(3)
        self.db = FakeDB({self.sha: 20})

    def test_document_is_cut_at_the_deadline_and_left_resumable(self):
        clock = FakeClock(start=1000.0)
        docker = FakeDocker(on_render=lambda: clock.advance(400))
        conn = FakeConn(self.db)
        with mock.patch.object(cp.time, "time", clock), \
             mock.patch.object(worker, "_try_docker", docker):
            measured, outcome = cp.process_document(
                conn, self.sha, self.db.docs[self.sha]["key"], 20,
                list(range(1, 21)), "pdftoppm", deadline=2000.0)
        self.assertEqual(outcome, "deadline")
        self.assertEqual(measured, 3)
        self.assertEqual(docker.renders, [1, 2, 3])
        self.assertEqual(self.db.skips, {},
                         "pages the deadline never reached must not be "
                         "recorded as skipped")
        self.assertEqual(self.db.pending(self.sha), list(range(4, 21)))
        self.assertEqual(cp.stats["deadline_documents"], 1)

    def test_per_page_timeouts_are_clamped_to_the_remaining_budget(self):
        clock = FakeClock(start=1000.0)
        docker = FakeDocker(on_render=lambda: clock.advance(10))
        conn = FakeConn(self.db)
        with mock.patch.object(cp.time, "time", clock), \
             mock.patch.object(worker, "_try_docker", docker):
            cp.process_document(conn, self.sha,
                                self.db.docs[self.sha]["key"], 20,
                                list(range(1, 21)), "pdftoppm",
                                deadline=1040.0)
        # 40 s of budget: no container may be given the full 120 s / 180 s.
        first_render = [t for kind, t in docker.timeouts
                        if kind == "pdftoppm"][0]
        self.assertLessEqual(first_render, 40)
        self.assertGreaterEqual(first_render, cp.MIN_TIMEOUT)
        self.assertLessEqual(max(t for _, t in docker.timeouts),
                             worker.RENDER_SECONDS)

    def test_a_deadline_bound_document_does_not_pin_the_shard(self):
        """run_shard rewinds while it is making progress, so the cut-short
        document is finished over several sweeps rather than abandoned -- and
        the run still ends."""
        clock = FakeClock(start=1000.0)
        docker = FakeDocker(on_render=lambda: clock.advance(400))
        with mock.patch.object(cp.time, "time", clock):
            self.run_shard(self.db, docker, doc_seconds=1000)
        self.assertEqual(len(self.db.measured()), 20)
        self.assertGreater(cp.stats["deadline_documents"], 1)


class WriteScopeTest(ConfidenceTestCase):
    """Phase 1 measures; it does not re-OCR. Changing ws13_chunks.text would
    force a re-chunk, which forces a re-embed through Titan, which
    invalidates ws13_chunks_titan_hnsw for those rows. This test asserts over
    the statements a real run issued, not over the source text."""

    MUTATIONS = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP",
                 "TRUNCATE", "COPY")
    FORBIDDEN = ("ws13_chunks", "ws13_documents", "ws13_manifest",
                 "embedding", "titan_embedding", "qwen_embedding", "tsv",
                 "ws13_chunks_titan_hnsw", "quality_score", "quality_method")

    def setUp(self):
        super().setUp()
        self.db = FakeDB({sha_for(i): 4 for i in range(6)})

    def _mutations(self):
        return [(sql, params) for sql, params in self.db.statements
                if sql.split(" ", 1)[0].upper() in self.MUTATIONS]

    def test_only_ws13_pages_and_the_skips_table_are_written(self):
        conn = FakeConn(self.db)
        cp.ensure_skips_table(conn)
        self.run_shard(self.db, FakeDocker(confidences=(40,), no_image=[2],
                                           render_timeout=[3]))
        mutations = self._mutations()
        self.assertTrue(mutations, "the run must have written something")
        for sql, _params in mutations:
            self.assertRegex(
                sql,
                r"^(UPDATE ws13_pages|INSERT INTO ws13_conf_skips|"
                r"CREATE TABLE IF NOT EXISTS ws13_conf_skips)\b",
                f"unexpected write target: {sql[:120]}")
            for table in self.FORBIDDEN:
                self.assertNotIn(table, sql,
                                 f"a measurement pass must not write {table}")

    def test_the_only_columns_written_are_confidence_and_low_confidence(self):
        self.run_shard(self.db, FakeDocker())
        updates = [sql for sql, _ in self._mutations()
                   if sql.startswith("UPDATE ws13_pages")]
        self.assertTrue(updates)
        for sql in updates:
            columns = re.findall(r"(\w+)\s*=\s*", sql.split("WHERE")[0])
            self.assertEqual(columns, ["confidence", "low_confidence"])
            self.assertIn("confidence IS NULL", sql,
                          "the write must be idempotent: a measured page is "
                          "never re-measured")

    def test_no_chunk_or_embedding_read_or_write_appears_at_all(self):
        self.run_shard(self.db, FakeDocker())
        for sql, _params in self.db.statements:
            self.assertNotIn("ws13_chunks", sql)
            self.assertNotIn("embedding", sql)


class HeartbeatTest(ConfidenceTestCase):
    """An operator has to be able to project a finish time -- but 640 shards
    each re-counting their remaining pages every 300 s is 2.1 filtered
    aggregates a second over 760,059 rows, paid against the same RDS
    instance the shards are reading their work from."""

    def test_remaining_is_counted_once_and_then_derived_locally(self):
        db = FakeDB({sha_for(5): 10})
        stop = threading.Event()
        payloads = []

        def capture(**kwargs):
            payloads.append(json.loads(kwargs["Body"]))
            if len(payloads) == 1:
                cp.stats["pages_measured"] = 7      # three more measured
            if len(payloads) >= 3:
                stop.set()

        self.s3.put_object.side_effect = capture
        cp.stats["pages_measured"] = 4
        with mock.patch.object(cp, "psycopg") as pg, \
             mock.patch.object(cp, "HEARTBEAT_SECONDS", 0), \
             mock.patch.object(cp, "REMAINING_SECONDS", 10_000):
            pg.connect.side_effect = lambda *a, **k: FakeConn(db)
            cp.heartbeat(stop, 0, 1, cp.time.time(), "pdftoppm",
                         ["measuring"])

        counts = [sql for sql, _ in db.statements
                  if sql.startswith("SELECT COUNT(*), COUNT(DISTINCT")]
        self.assertEqual(len(counts), 1,
                         "the remaining count must not run every tick")
        self.assertEqual(len(payloads), 3)
        self.assertTrue(payloads[0]["pages_remaining_counted"])
        self.assertEqual(payloads[0]["pages_remaining"], 10)
        self.assertFalse(payloads[1]["pages_remaining_counted"],
                         "a derived number must not be published as a count")
        self.assertEqual(payloads[1]["pages_remaining"], 7,
                         "derived = last count minus this shard's progress")

    def test_a_heartbeat_failure_never_stops_the_run(self):
        db = FakeDB({sha_for(5): 4})
        stop = threading.Event()
        calls = {"n": 0}

        def failing(**kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                stop.set()
            raise RuntimeError("S3 is having a day")

        self.s3.put_object.side_effect = failing
        with mock.patch.object(cp, "psycopg") as pg, \
             mock.patch.object(cp, "HEARTBEAT_SECONDS", 0):
            pg.connect.side_effect = lambda *a, **k: FakeConn(db)
            cp.heartbeat(stop, 0, 1, cp.time.time(), "gs", ["measuring"])
        self.assertGreaterEqual(calls["n"], 2)


class PlanTest(ConfidenceTestCase):
    """--plan / --dry-run exist so the operator sizes the fleet on the number
    that binds. Sharding is by document, so the mean shard is not that
    number: the shard holding the 1,407-page document carries it whole."""

    def setUp(self):
        super().setUp()
        # 320 small documents plus the corpus's largest one.
        docs = {sha_for(i): 20 for i in range(320)}
        self.big = sha_for(9999)
        docs[self.big] = 1407
        self.db = FakeDB(docs)
        self.rows = [(sha, pages) for sha, pages in
                     sorted({sha: len(self.db.pending(sha))
                             for sha in self.db.docs}.items())]

    def test_plan_reports_pages_documents_and_the_three_fleet_sizes(self):
        report = cp.plan(self.rows, cp.DEFAULT_RATE)
        self.assertEqual(report["pages_remaining"], 320 * 20 + 1407)
        self.assertEqual(report["documents_remaining"], 321)
        self.assertEqual([row["nodes"] for row in report["rows"]], [1, 8, 40])
        self.assertEqual([row["cores"] for row in report["rows"]],
                         [16, 128, 640])
        self.assertEqual(cp.NODE_VCPU * cp.MAX_NODES, 640,
                         "40 c7g.4xlarge is exactly the 640 vCPU quota")

    def test_even_split_hours_are_pages_over_rate_times_cores(self):
        rate = 1.0 / 6.5
        report = cp.plan(self.rows, rate)
        pages = report["pages_remaining"]
        for row in report["rows"]:
            self.assertAlmostEqual(
                row["even_hours"], pages / (rate * row["cores"]) / 3600,
                places=6)
            self.assertAlmostEqual(row["mean_pages"], pages / row["cores"],
                                   places=6)

    def test_worst_shard_carries_the_largest_document_whole(self):
        report = cp.plan(self.rows, cp.DEFAULT_RATE)
        for row in report["rows"]:
            self.assertGreaterEqual(
                row["worst_pages"], 1407,
                "the shard that owns the 1,407-page document cannot finish "
                "before that document does")
            self.assertGreaterEqual(row["worst_hours"], row["even_hours"])
        at_640 = report["rows"][-1]
        self.assertGreater(
            at_640["worst_pages"], at_640["mean_pages"],
            "if the mean were the answer the operator would over-provision")

    def test_shard_totals_match_the_partition_the_run_uses(self):
        totals = cp.shard_totals(self.rows, 640)
        expected = {}
        for sha, pages in self.rows:
            key = sql_shard_of(sha, 640)
            expected[key] = expected.get(key, 0) + pages
        self.assertEqual(totals, expected)
        self.assertEqual(sum(totals.values()), 320 * 20 + 1407)

    def test_dry_run_reports_the_work_set_without_rendering_anything(self):
        def explode(*args, **kwargs):
            raise AssertionError("--dry-run must not render a page")

        conn = FakeConn(self.db)
        with mock.patch.object(worker, "_try_docker", explode):
            report = cp.dry_run(conn, 3, 640, cp.DEFAULT_RATE)
        expected = sum(pages for sha, pages in self.rows
                       if sql_shard_of(sha, 640) == 3)
        self.assertEqual(report["shard_pages"], expected)
        self.assertEqual(report["shards"], 640)
        self.assertAlmostEqual(report["shard_hours"],
                               expected / cp.DEFAULT_RATE / 3600, places=6)
        self.assertEqual(self.db.measured(), {},
                         "--dry-run must write no confidence")
        self.assertEqual(self.db.skips, {})
        self.s3.download_file.assert_not_called()

    def test_each_mode_seeds_the_rate_its_own_container_shape_implies(self):
        """Two paths, two costs, and neither seed may be quietly reused.

        Per page is two `docker run` launches, which is the 5-8 s/page
        ws13_worker measures. Batched is one launch per document amortised
        over the 11.1 pages an average document has, so it approaches the
        ~1.2 s of real work. Seeding the batched path at the per-page rate
        would over-provision the fleet by ~4x; seeding the per-page path at
        the batched rate would under-provision it by the same factor.
        """
        self.assertTrue(5.0 <= cp.PER_PAGE_SECONDS <= 8.0,
                        "the per-page seed must stay inside the 5-8 s/page "
                        "ws13_worker actually measures")
        self.assertTrue(1.0 <= cp.BATCHED_SECONDS <= 3.0,
                        "the batched seed must reflect one container per "
                        "document, not two per page")
        self.assertLess(cp.BATCHED_SECONDS, cp.PER_PAGE_SECONDS)
        self.assertAlmostEqual(cp.DEFAULT_RATE, 1.0 / cp.SECONDS_PER_PAGE)
        self.assertIn(cp.SECONDS_PER_PAGE,
                      (cp.BATCHED_SECONDS, cp.PER_PAGE_SECONDS))


class RendererReuseTest(ConfidenceTestCase):
    """The probe and the argv come from ws13_worker: pdftoppm is not on PATH
    in the ocrmypdf image, which is the defect this pass repairs, and a
    second copy of that knowledge here is how the two would drift apart."""

    def test_measure_page_works_with_every_renderer_the_worker_probes(self):
        self.assertEqual(worker.PAGE_RENDERERS,
                         ("pdftoppm", "pdftocairo", "gs"))
        for renderer in worker.PAGE_RENDERERS:
            docker = FakeDocker(confidences=(77,))
            with tempfile.TemporaryDirectory() as work, \
                 mock.patch.object(worker, "_try_docker", docker):
                conf, reason = cp.measure_page(work, cp.PDF_NAME, 7,
                                               renderer, 120, 180)
                self.assertIsNone(reason, f"{renderer} failed to measure")
                self.assertEqual(conf, 77.0)
                self.assertEqual(docker.renders, [7])
                self.assertEqual(os.listdir(work), [],
                                 "page rasters must not accumulate on disk")

    def test_status_key_is_per_shard_once_sharded(self):
        # 640 shards writing one key would leave only the last writer's
        # numbers, which looks authoritative and is not.
        self.assertEqual(cp.status_key(0, 1), "ws13/confidence/status.json")
        self.assertNotEqual(cp.status_key(0, 640), cp.status_key(1, 640))
        self.assertTrue(
            cp.status_key(7, 640).startswith("ws13/confidence/status-"))

    def test_status_payload_reports_a_projectable_rate(self):
        cp.stats["pages_measured"] = 120
        payload = cp.status_payload(3, 640, started=cp.time.time() - 60,
                                    pages_left=480, docs_left=40,
                                    renderer="gs", phase="measuring")
        self.assertGreater(payload["pages_per_second"], 0)
        self.assertAlmostEqual(payload["eta_hours"],
                               480 / payload["pages_per_second"] / 3600,
                               places=1)
        self.assertEqual(payload["pages_remaining"], 480)
        self.assertEqual(payload["shard"], 3)




class BatchedMeasurementTest(unittest.TestCase):
    """One container per document instead of two per page.

    The measurement is ~1.2 s of real work behind 1.5-3 s of `docker run`
    startup, and the per-page shape paid that startup twice for each of
    323,059 pages -- ~646,000 launches where the container, not the OCR,
    dominates. Batching makes it ~28,988. These tests pin the property that
    makes the trade safe: per-page attribution survives, and a page the
    container never reached is left unmeasured rather than guessed at.
    """

    def script_for(self, pages, renderer="pdftoppm"):
        return cp.batch_script(renderer, "doc.pdf", pages)

    def test_every_page_gets_its_own_render_tess_and_end_markers(self):
        script = self.script_for([1, 2, 7])
        for page in (1, 2, 7):
            self.assertIn(f'{cp.BATCH_MARK} {page} render $?', script)
            self.assertIn(f'{cp.BATCH_MARK} {page} tess $?', script)
            self.assertIn(f'{cp.BATCH_MARK} {page} end 0', script)

    def test_the_script_uses_ws13_worker_s_own_render_argv(self):
        """A second copy of the renderer spelling is how the two would drift."""
        for renderer in ("pdftoppm", "pdftocairo", "gs"):
            argv = cp.worker.render_argv(renderer, "doc.pdf", "pg00003", 3)
            script = self.script_for([3], renderer=renderer)
            for token in argv:
                self.assertIn(token, script,
                              f"{renderer} argv token {token!r} missing")

    def test_confidence_is_attributed_to_the_page_that_produced_it(self):
        out = "\n".join([
            f"{cp.BATCH_MARK} 1 render 0",
            "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t90.0\tALPHA",
            f"{cp.BATCH_MARK} 1 tess 0",
            f"{cp.BATCH_MARK} 1 end 0",
            f"{cp.BATCH_MARK} 2 render 0",
            "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t30.0\tBETA",
            f"{cp.BATCH_MARK} 2 tess 0",
            f"{cp.BATCH_MARK} 2 end 0",
        ])
        got = cp.parse_batch_output(out, [1, 2], "pdftoppm")
        self.assertEqual(got[1][0], 90.0)
        self.assertEqual(got[2][0], 30.0)
        self.assertIsNone(got[1][1])
        self.assertIsNone(got[2][1])

    def test_a_page_the_container_never_reached_is_absent_not_guessed(self):
        """The whole point: unmeasured must stay NULL so a sweep retries it."""
        out = "\n".join([
            f"{cp.BATCH_MARK} 1 render 0",
            "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t80.0\tALPHA",
            f"{cp.BATCH_MARK} 1 tess 0",
            f"{cp.BATCH_MARK} 1 end 0",
            f"{cp.BATCH_MARK} 2 render 0",
        ])
        got = cp.parse_batch_output(out, [1, 2, 3], "pdftoppm")
        self.assertIn(1, got)
        self.assertNotIn(2, got, "a cut-off page must not be reported")
        self.assertNotIn(3, got, "an unreached page must not be reported")

    def test_no_image_is_terminal_but_a_docker_env_code_is_not(self):
        absent = cp.parse_batch_output(
            f"{cp.BATCH_MARK} 4 render 1\n{cp.BATCH_MARK} 4 end 0",
            [4], "pdftoppm")
        self.assertEqual(absent[4], (None, "no_image"))
        self.assertIn("no_image", cp.TERMINAL_REASONS)

        broken = cp.parse_batch_output(
            f"{cp.BATCH_MARK} 4 render 127\n{cp.BATCH_MARK} 4 end 0",
            [4], "pdftoppm")
        self.assertEqual(broken[4], (None, "docker_rc_127"))
        self.assertNotIn("docker_rc_127", cp.TERMINAL_REASONS,
                         "rc=127 describes the image, never the page")

    def test_a_blank_page_is_no_words_not_a_confidence_of_zero(self):
        out = "\n".join([
            f"{cp.BATCH_MARK} 9 render 0",
            "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t-1\t",
            f"{cp.BATCH_MARK} 9 tess 0",
            f"{cp.BATCH_MARK} 9 end 0",
        ])
        got = cp.parse_batch_output(out, [9], "pdftoppm")
        self.assertEqual(got[9], (None, "no_words"))

    def test_a_tesseract_failure_is_reported_with_its_code(self):
        out = "\n".join([
            f"{cp.BATCH_MARK} 5 render 0",
            f"{cp.BATCH_MARK} 5 tess 2",
            f"{cp.BATCH_MARK} 5 end 0",
        ])
        self.assertEqual(
            cp.parse_batch_output(out, [5], "pdftoppm")[5],
            (None, "tesseract_rc_2"))

    def test_ocr_text_that_looks_like_a_marker_does_not_shift_pages(self):
        """OCR output is untrusted input to this parser, not a trusted stream."""
        out = "\n".join([
            f"{cp.BATCH_MARK} 1 render 0",
            "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext",
            f"5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t70.0\t{cp.BATCH_MARK}",
            f"{cp.BATCH_MARK} 1 tess 0",
            f"{cp.BATCH_MARK} 1 end 0",
        ])
        got = cp.parse_batch_output(out, [1], "pdftoppm")
        self.assertEqual(got[1][0], 70.0)

    def test_a_node_level_container_failure_reports_not_ok(self):
        """rc 125/126/127 describe this node and this image, never the page,
        so the caller falls back to per-page rather than skipping pages."""
        dead = types.SimpleNamespace(stdout="", returncode=127, stderr="boom")
        with mock.patch.object(cp.worker, "docker", return_value=dead):
            measured, ok = cp.measure_pages_batched(
                "/tmp", "doc.pdf", [1, 2], "pdftoppm", 30)
        self.assertEqual(measured, {})
        self.assertFalse(ok)

    def test_a_timeout_is_progress_not_a_node_failure(self):
        """A timeout keeps what it measured and leaves the rest NULL; falling
        back to per-page there would re-measure pages already stored."""
        def times_out(args, work, timeout=None, entrypoint=None):
            raise subprocess.TimeoutExpired(
                cmd="docker", timeout=1,
                output=f"{cp.BATCH_MARK} 1 render 0\n{_tsv(77.0)}\n"
                       f"{cp.BATCH_MARK} 1 tess 0\n{cp.BATCH_MARK} 1 end 0")

        with mock.patch.object(cp.worker, "docker", side_effect=times_out):
            measured, ok = cp.measure_pages_batched(
                "/tmp", "doc.pdf", [1, 2], "pdftoppm", 30)
        self.assertEqual(measured[1][0], 77.0)
        self.assertNotIn(2, measured)
        self.assertTrue(ok)


def _tsv(conf):
    header = ("level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\t"
              "height\tconf\ttext")
    return f"{header}\n5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t{conf}\tWORD"


def _batch_stdout(pages, conf=85.0):
    out = []
    for page in pages:
        out += [f"{cp.BATCH_MARK} {page} render 0", _tsv(conf),
                f"{cp.BATCH_MARK} {page} tess 0",
                f"{cp.BATCH_MARK} {page} end 0"]
    return "\n".join(out)


class BatchedDocumentTest(ConfidenceTestCase):
    """process_document over the batched path: one container per document."""

    batch_pages = True

    def setUp(self):
        super().setUp()
        self.sha = sha_for(3)
        self.db = FakeDB({self.sha: 20})

    def _run(self, docker_side_effect, pages=range(1, 6)):
        conn = FakeConn(self.db)
        with mock.patch.object(worker, "docker", side_effect=docker_side_effect):
            return cp.process_document(
                conn, self.sha, self.db.docs[self.sha]["key"], 20,
                list(pages), "pdftoppm", deadline=cp.time.time() + 600), conn

    def test_one_container_measures_the_whole_document(self):
        calls = []

        def one_shot(args, work, timeout=None, entrypoint=None):
            calls.append(entrypoint)
            return types.SimpleNamespace(
                stdout=_batch_stdout(range(1, 6)), returncode=0, stderr="")

        (measured, outcome), _ = self._run(one_shot)
        self.assertEqual(measured, 5)
        self.assertIsNone(outcome)
        self.assertEqual(calls, ["sh"],
                         "five pages must cost one container, not ten")
        self.assertEqual(self.db.pending(self.sha), list(range(6, 21)))

    def test_a_timeout_keeps_the_pages_the_container_already_measured(self):
        """The bug this pins: worker._try_docker returns None on a timeout and
        drops the partial stdout, which per document would discard every page
        already measured and pay to redo them."""
        def times_out(args, work, timeout=None, entrypoint=None):
            raise subprocess.TimeoutExpired(
                cmd="docker", timeout=timeout or 1,
                output=_batch_stdout([1, 2, 3]))

        (measured, outcome), _ = self._run(times_out)
        self.assertEqual(measured, 3, "partial progress must survive a timeout")
        self.assertEqual(outcome, "deadline")
        self.assertEqual(self.db.skips, {},
                         "pages the container never reached must not be "
                         "recorded as skipped")
        self.assertEqual(self.db.pending(self.sha), list(range(4, 21)),
                         "unreached pages stay NULL for the next sweep")

    def test_a_broken_docker_daemon_falls_back_to_the_per_page_path(self):
        seen = []

        def env_failure(args, work, timeout=None, entrypoint=None):
            seen.append(entrypoint)
            return types.SimpleNamespace(stdout="", returncode=127, stderr="")

        conn = FakeConn(self.db)
        per_page = FakeDocker()
        with mock.patch.object(worker, "docker", side_effect=env_failure), \
             mock.patch.object(worker, "_try_docker", per_page):
            measured, _ = cp.process_document(
                conn, self.sha, self.db.docs[self.sha]["key"], 20,
                list(range(1, 4)), "pdftoppm", deadline=cp.time.time() + 600)
        self.assertEqual(seen, ["sh"])
        self.assertEqual(per_page.renders, [1, 2, 3],
                         "a node-level failure must isolate per page")
        self.assertEqual(measured, 3)

    def test_pages_past_the_stored_page_count_never_reach_a_container(self):
        def one_shot(args, work, timeout=None, entrypoint=None):
            self.assertNotIn("pg00021", args[1],
                             "a page past the page count must not be rendered")
            return types.SimpleNamespace(
                stdout=_batch_stdout([1]), returncode=0, stderr="")

        (measured, _), _ = self._run(one_shot, pages=[1, 21])
        self.assertEqual(measured, 1)
        self.assertEqual(self.db.skips.get((self.sha, 21)), "page_absent")


class CollectionGuardTest(unittest.TestCase):
    """A test appended below `unittest.main()` is never even defined when the
    file is run directly, so it reports OK while silently omitting itself.
    That happened here: the batched-path classes landed under the block and
    `python3 tests/test_ws13_confidence_pass.py` ran 44 of 58 tests, dropping
    the only coverage of the DEFAULT path."""

    def test_running_this_file_directly_collects_every_test_case(self):
        # sys.modules[__name__], not an import by path: run directly this
        # file is __main__ and the `tests` package is not bound at all.
        module = sys.modules[__name__]
        defined = {name for name, value in vars(module).items()
                   if isinstance(value, type)
                   and issubclass(value, unittest.TestCase)}
        source = Path(module.__file__).read_text(encoding="utf-8")
        declared = set(re.findall(r"^class (\w+)\((?:[\w.]*TestCase|"
                                  r"ConfidenceTestCase)\)", source, re.M))
        self.assertTrue(declared)
        self.assertEqual(declared - defined, set(),
                         "a TestCase is declared but not defined at import: "
                         "it is almost certainly below `unittest.main()`")
        # rindex, and anchored to the line start: this test's own source
        # contains that string as a literal, and index() would match it.
        main_at = source.rindex('\nif __name__ == "__main__"')
        last_class = max(source.rindex(f"class {name}(") for name in declared)
        self.assertLess(last_class, main_at,
                        "every TestCase must be declared above the "
                        "`unittest.main()` block")


class BatchStageTimeoutTest(unittest.TestCase):
    """Each stage inside the batch carries its own timeout.

    Without one, the container's single deadline is the document's whole
    remaining budget: one hung raster consumes it, every later page goes
    unreached, and since pages are handed out in ascending order the same
    page stalls the same way on every future sweep. That is a document
    permanently stuck behind page N -- worse than the per-page path it
    replaced, which clamped every page individually.
    """

    def test_both_stages_are_wrapped_in_timeout(self):
        script = cp.batch_script("pdftoppm", "doc.pdf", [1])
        self.assertIn(f"timeout {int(worker.RENDER_SECONDS)} pdftoppm", script)
        self.assertIn(f"timeout {int(worker.TESSERACT_SECONDS)} tesseract",
                      script)

    def test_a_stalled_render_is_transient_and_names_that_page(self):
        got = cp.parse_batch_output(
            f"{cp.BATCH_MARK} 7 render {cp.TIMEOUT_CODE}\n"
            f"{cp.BATCH_MARK} 7 end 0", [7], "pdftoppm")
        self.assertEqual(got[7], (None, "render_timeout"))
        self.assertNotIn("render_timeout", cp.TERMINAL_REASONS)

    def test_a_stalled_tesseract_is_transient(self):
        got = cp.parse_batch_output(
            f"{cp.BATCH_MARK} 7 render 0\n"
            f"{cp.BATCH_MARK} 7 tess {cp.TIMEOUT_CODE}\n"
            f"{cp.BATCH_MARK} 7 end 0", [7], "pdftoppm")
        self.assertEqual(got[7], (None, "tesseract_timeout"))
        self.assertNotIn("tesseract_timeout", cp.TERMINAL_REASONS)

    def test_one_stalled_page_does_not_stop_the_pages_after_it(self):
        """The property the whole per-stage timeout exists to buy."""
        out = "\n".join([
            f"{cp.BATCH_MARK} 1 render {cp.TIMEOUT_CODE}",
            f"{cp.BATCH_MARK} 1 end 0",
            f"{cp.BATCH_MARK} 2 render 0", _tsv(88.0),
            f"{cp.BATCH_MARK} 2 tess 0",
            f"{cp.BATCH_MARK} 2 end 0",
        ])
        got = cp.parse_batch_output(out, [1, 2], "pdftoppm")
        self.assertEqual(got[1], (None, "render_timeout"))
        self.assertEqual(got[2][0], 88.0)

    def test_a_docker_env_code_from_tesseract_is_not_a_page_defect(self):
        got = cp.parse_batch_output(
            f"{cp.BATCH_MARK} 3 render 0\n"
            f"{cp.BATCH_MARK} 3 tess 127\n"
            f"{cp.BATCH_MARK} 3 end 0", [3], "pdftoppm")
        self.assertEqual(got[3], (None, "docker_rc_127"))
        self.assertNotIn("docker_rc_127", cp.TERMINAL_REASONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
