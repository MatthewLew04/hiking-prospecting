"""Unit tests for the pure parts of pipelines/ws13_worker.py.

Everything here is a defect that shipped, or a guard added because one did:

  * render_argv() -- the confidence renderer shelled `pdftoppm`
    unconditionally and pdftoppm is not on $PATH in the ocrmypdf image, so
    every render exited rc=127 and 0 of 760,043 ws13_pages rows carry a
    confidence. A typo in one of the three argv shapes (gs takes an output
    PATH, not a prefix, and -dFirstPage must precede the input file) would
    reproduce that silently.
  * page_renderer() -- a transient probe failure used to be cached for the
    life of the process, disabling confidence measurement and the tier-1
    escalation for that worker's whole run.
  * page_confidences() -- `all(c is None)` only reported a problem when
    EVERY page failed, so a document measured on 1 of 300 pages was recorded
    as fully measured and written as low_conf_pages=0.
  * sample_indices() / DocumentLease -- the per-document wall-clock bound
    that keeps a document inside its SQS visibility lease.

No database, no docker, no network: docker() is stubbed at the seam.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

os.environ.setdefault("WS13_BUCKET", "test-bucket")
os.environ.setdefault("WS13_QUEUE_URL", "https://sqs.test/queue")
os.environ.setdefault("WS13_DB_DSN", "postgresql://test/test")

for name in ("psycopg", "boto3"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            stub = types.ModuleType(name)
            stub.connect = mock.MagicMock()
            stub.client = mock.MagicMock()
            sys.modules[name] = stub

import boto3  # noqa: E402

with mock.patch.object(boto3, "client", return_value=mock.MagicMock()):
    import ws13_worker as w  # noqa: E402


TSV_HEADER = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
              "left\ttop\twidth\theight\tconf\ttext")


def tsv(*confs):
    lines = [TSV_HEADER]
    for i, conf in enumerate(confs):
        lines.append(f"5\t1\t1\t1\t1\t{i}\t0\t0\t10\t10\t{conf}\tword")
    return "\n".join(lines) + "\n"


def quiet(case):
    """Silence the worker's own logging for the duration of one test."""
    patch = mock.patch.object(w, "log")
    patch.start()
    case.addCleanup(patch.stop)


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class RenderArgvTests(unittest.TestCase):
    def test_pdftoppm_asks_for_one_page_and_a_prefix(self):
        argv = w.render_argv("pdftoppm", "out.pdf", "pg00007", 7)
        self.assertEqual(argv, ["-r", "150", "-png", "-f", "7", "-l", "7",
                                "/work/out.pdf", "/work/pg00007"])

    def test_pdftocairo_needs_an_explicit_png_flag(self):
        argv = w.render_argv("pdftocairo", "out.pdf", "pg00001", 1)
        self.assertIn("-png", argv)
        self.assertEqual(argv[-2:], ["/work/out.pdf", "/work/pg00001"])
        self.assertEqual(argv[argv.index("-f") + 1], "1")
        self.assertEqual(argv[argv.index("-l") + 1], "1")

    def test_gs_takes_an_output_path_and_the_input_last(self):
        argv = w.render_argv("gs", "out.pdf", "pg00042", 42)
        self.assertIn("-dFirstPage=42", argv)
        self.assertIn("-dLastPage=42", argv)
        # -o PATH, and the PDF is the final operand: swapping those makes gs
        # write its output over the input.
        self.assertEqual(argv[-1], "/work/out.pdf")
        self.assertEqual(argv[argv.index("-o") + 1], "/work/pg00042-1.png")
        self.assertLess(argv.index("-dFirstPage=42"), argv.index("/work/out.pdf"))

    def test_every_probed_renderer_has_an_argv(self):
        for name in w.PAGE_RENDERERS:
            self.assertTrue(w.render_argv(name, "a.pdf", "pg1", 1))

    def test_unknown_renderer_is_a_hard_error(self):
        with self.assertRaises(ValueError):
            w.render_argv("mutool", "a.pdf", "pg1", 1)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.reset()
        quiet(self)
        self.addCleanup(self.reset)

    def reset(self):
        w._page_renderer = None
        w._probe_transient = "not_probed"
        w._probe_next_try = 0.0

    def test_first_listed_renderer_present_in_the_image_wins(self):
        out = "/usr/bin/pdftocairo\n/usr/bin/gs\n"
        with mock.patch.object(w.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(w, "docker", return_value=completed(stdout=out)):
            self.assertEqual(w.probe_page_renderer(), ("pdftocairo", None))

    def test_empty_found_set_is_reported_as_no_renderer(self):
        with mock.patch.object(w.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(w, "docker", return_value=completed(stdout="\n")):
            self.assertEqual(w.probe_page_renderer(),
                             (None, "no_renderer_in_image"))

    def test_probe_failures_are_machine_readable(self):
        with mock.patch.object(w.shutil, "which", return_value="/usr/bin/docker"):
            with mock.patch.object(w, "docker", return_value=completed(2)):
                self.assertEqual(w.probe_page_renderer(), (None, "probe_exit_2"))
            boom = subprocess.TimeoutExpired(cmd="docker", timeout=1)
            with mock.patch.object(w, "docker", side_effect=boom):
                self.assertEqual(w.probe_page_renderer(),
                                 (None, "probe_failed:TimeoutExpired"))
        with mock.patch.object(w.shutil, "which", return_value=None):
            self.assertEqual(w.probe_page_renderer(), (None, "no_docker"))

    def test_terminal_reasons_are_probed_once(self):
        with mock.patch.object(w, "probe_page_renderer",
                               return_value=(None, "no_renderer_in_image")) as p:
            self.assertEqual(w.page_renderer(), (None, "no_renderer_in_image"))
            self.assertEqual(w.page_renderer(), (None, "no_renderer_in_image"))
            self.assertEqual(p.call_count, 1)

    def test_a_transient_failure_is_retried_not_cached(self):
        # The defect: one contended docker daemon at boot -- 8 workers pull
        # the same image seconds after `systemctl start docker` -- disabled
        # confidences for the worker's entire run.
        outcomes = [(None, "probe_failed:TimeoutExpired"), ("gs", None)]
        with mock.patch.object(w, "probe_page_renderer",
                               side_effect=outcomes) as probe:
            self.assertEqual(w.page_renderer(),
                             (None, "probe_failed:TimeoutExpired"))
            # Backoff holds inside PROBE_RETRY_SECONDS ...
            self.assertEqual(w.page_renderer(),
                             (None, "probe_failed:TimeoutExpired"))
            self.assertEqual(probe.call_count, 1)
            # ... and re-probes once it expires.
            w._probe_next_try = 0.0
            self.assertEqual(w.page_renderer(), ("gs", None))
            self.assertEqual(probe.call_count, 2)
        self.assertEqual(w.page_renderer(), ("gs", None))


class SampleIndicesTests(unittest.TestCase):
    def test_small_documents_are_measured_in_full(self):
        self.assertEqual(w.sample_indices(0, 60), [])
        self.assertEqual(w.sample_indices(3, 60), [1, 2, 3])
        self.assertEqual(w.sample_indices(60, 60), list(range(1, 61)))

    def test_large_documents_are_sampled_within_range(self):
        picked = w.sample_indices(900, 60)
        self.assertLessEqual(len(picked), 60)
        self.assertEqual(picked, sorted(set(picked)))
        self.assertEqual(picked[0], 1)
        self.assertGreaterEqual(min(picked), 1)
        self.assertLessEqual(max(picked), 900)
        # Evenly spaced, so the sample is not just the front of the document.
        self.assertGreater(max(picked), 800)


class FakeLease:
    """DocumentLease's interface, without threads or a database."""

    def __init__(self, deadline=None):
        self.deadline = deadline if deadline is not None else 1e18
        self.beats = 0

    def heartbeat(self, force=False):
        self.beats += 1


class PageConfidenceTests(unittest.TestCase):
    def setUp(self):
        w._page_renderer = ("gs", None)
        w._probe_next_try = 0.0
        self.work = tempfile.mkdtemp()
        quiet(self)
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(setattr, w, "_page_renderer", None)

    def fake_docker(self, render_pages, conf=88.5):
        """render succeeds only for pages in `render_pages`."""
        def call(args, work, timeout, entrypoint=None):
            if entrypoint == "tesseract":
                return completed(stdout=tsv(conf))
            page = int([a for a in args if a.startswith("-dFirstPage=")][0]
                       .split("=")[1])
            if page not in render_pages:
                return completed(1, stderr="gs: no such page")
            base = [a for a in args if a.startswith("/work/pg")][0]
            with open(os.path.join(work, os.path.basename(base)), "wb") as fh:
                fh.write(b"png")
            return completed(0)
        return call

    def test_full_coverage_has_no_reason(self):
        with mock.patch.object(w, "docker", self.fake_docker({1, 2, 3})):
            confs, reason = w.page_confidences(self.work, "out.pdf", 3)
        self.assertEqual(confs, [88.5, 88.5, 88.5])
        self.assertIsNone(reason)

    def test_one_measured_page_is_not_a_measured_document(self):
        # The regression: confs = [88.5, None, None] used to return reason
        # None, and process() wrote low_conf_pages=0 -- "nothing was weak"
        # about two pages nobody looked at.
        with mock.patch.object(w, "docker", self.fake_docker({1})):
            confs, reason = w.page_confidences(self.work, "out.pdf", 3)
        self.assertEqual(confs, [88.5, None, None])
        self.assertEqual(reason, "partial:1/3")

    def test_nothing_measured_is_reported_as_such(self):
        with mock.patch.object(w, "docker", self.fake_docker(set())):
            confs, reason = w.page_confidences(self.work, "out.pdf", 3)
        self.assertEqual(confs, [None, None, None])
        self.assertEqual(reason, "no_page_measured")

    def test_a_sample_reports_its_denominator(self):
        with mock.patch.object(w, "docker", self.fake_docker({1, 5})):
            confs, reason = w.page_confidences(self.work, "out.pdf", 9,
                                               indices=[1, 5])
        self.assertEqual(reason, "partial:2/9")
        self.assertEqual(confs[0], 88.5)
        self.assertEqual(confs[4], 88.5)
        self.assertIsNone(confs[8])

    def test_missing_renderer_never_reads_as_measured(self):
        w._page_renderer = (None, "no_renderer_in_image")
        confs, reason = w.page_confidences(self.work, "out.pdf", 4)
        self.assertEqual(confs, [None] * 4)
        self.assertEqual(reason, "no_renderer_in_image")

    def test_the_budget_cuts_the_pass_short_and_says_so(self):
        lease = FakeLease(deadline=w.time.time() + w.INDEX_RESERVE_SECONDS - 1)
        with mock.patch.object(w, "docker", self.fake_docker({1, 2, 3})):
            confs, reason = w.page_confidences(self.work, "out.pdf", 3,
                                               lease=lease)
        self.assertEqual(confs, [None, None, None])
        self.assertEqual(reason, "no_page_measured:budget")

    def test_a_render_timeout_does_not_fail_the_document(self):
        boom = subprocess.TimeoutExpired(cmd="docker", timeout=1)
        with mock.patch.object(w, "docker", side_effect=boom):
            confs, reason = w.page_confidences(self.work, "out.pdf", 2)
        self.assertEqual(confs, [None, None])
        self.assertEqual(reason, "no_page_measured")


class DocumentLeaseTests(unittest.TestCase):
    def lease(self, budget=1000):
        return w.DocumentLease("handle", "a" * 64, mock.MagicMock(),
                               budget=budget)

    def test_container_timeout_is_clamped_to_what_is_left(self):
        lease = self.lease(budget=300)
        self.assertLessEqual(lease.container_timeout(w.MAX_DOC_SECONDS), 300)
        # A generous budget does not raise a container's own cap.
        lease = self.lease(budget=99999)
        self.assertEqual(lease.container_timeout(600), 600)

    def test_the_index_reserve_is_held_back_from_optional_work(self):
        lease = self.lease(budget=w.INDEX_RESERVE_SECONDS + 30)
        self.assertFalse(lease.allows(120))
        self.assertTrue(lease.allows(10))

    def test_a_lost_lease_raises_at_the_next_checkpoint(self):
        lease = self.lease()
        lease.heartbeat(force=True)          # fine while the lease is held
        lease.lost = "ReceiptHandleIsInvalid for 3300s"
        with self.assertRaises(w.LeaseLost):
            lease.check()
        with self.assertRaises(w.LeaseLost):
            lease.heartbeat(force=True)

    def test_heartbeats_are_rate_limited_and_touch_only_running_rows(self):
        conn = mock.MagicMock()
        lease = w.DocumentLease("handle", "b" * 64, conn, budget=1000)
        lease.heartbeat(force=True)
        lease.heartbeat()                    # inside HEARTBEAT_SECONDS
        self.assertEqual(conn.execute.call_count, 1)
        sql, params = conn.execute.call_args[0]
        self.assertIn("UPDATE ws13_manifest", sql)
        self.assertIn("status='running'", sql)
        self.assertEqual(params, ("b" * 64,))
        conn.commit.assert_called_once()

    def test_the_budget_default_stays_inside_the_reaper_window(self):
        # ws13_reap_stale.py ages a row from its last heartbeat; the longest
        # gap is one container run. Both must stay under its 2 h floor.
        self.assertLessEqual(w.MAX_DOC_SECONDS, 2 * 3600)
        self.assertLessEqual(w.LEASE_TICK_SECONDS * 2, w.LEASE_SECONDS)
        self.assertLess(w.HEARTBEAT_SECONDS, w.MAX_DOC_SECONDS)


if __name__ == "__main__":
    unittest.main()
