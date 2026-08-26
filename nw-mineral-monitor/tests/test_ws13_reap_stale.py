"""Contract tests for pipelines/ws13_reap_stale.py.

The tool writes to the production manifest, and every guard in it is pure:
the candidate/held split, the compare-and-set skip, the MIN_SAFE_HOURS
refusal and the --sha selector never need a database to be wrong. Three of
those guards were wrong at review time -- --limit was applied to the SELECT
instead of to the reapable rows, so `--limit 10` against a backlog whose ten
oldest rows were all held reaped nothing, and --sha values went into a LIKE
pattern unvalidated, so an unset shell variable became LIKE '%' and selected
every stale row.

FakeConn implements only what main() calls: execute() returning something
with fetchone/fetchall/rowcount, dispatched on the SQL it is handed.
"""
from __future__ import annotations

import io
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

os.environ.setdefault("WS13_DB_DSN", "postgresql://test/test")

# The driver is a deployment dependency of the worker nodes, not of the test
# host; every call through it is stubbed below.
if "psycopg" not in sys.modules:
    try:
        __import__("psycopg")
    except ImportError:
        stub = types.ModuleType("psycopg")
        stub.connect = mock.MagicMock()
        sys.modules["psycopg"] = stub

import ws13_reap_stale as reap  # noqa: E402


def row(sha, *, age=9.0, worker="w1", live=False, updated="t0"):
    """One SELECT_STALE row: (sha, cls, worker, updated, age, processed, live)."""
    return (sha, "ocr_queue", worker, updated, age, None, live)


class FakeResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, stale_rows, running=7, unageable=0, reap_rowcounts=None):
        self.stale_rows = stale_rows
        self.running = running
        self.unageable = unageable
        self.reap_rowcounts = list(reap_rowcounts or [])
        self.updates = []          # (sha, updated_at, worker) per REAP_ONE
        self.select_sql = None
        self.select_params = None

    def execute(self, sql, params=None):
        if sql.strip().startswith("UPDATE"):
            reason, reaper_id, sha, updated, worker = params
            self.updates.append((sha, updated, worker, reason))
            got = (self.reap_rowcounts.pop(0)
                   if self.reap_rowcounts else 1)
            return FakeResult(rowcount=got)
        if "count(*)" in sql:
            return FakeResult([(self.running, self.unageable)])
        self.select_sql, self.select_params = sql, params
        return FakeResult(self.stale_rows)


def run_main(conn, argv):
    """main() against FakeConn, returning (exit code, stdout)."""
    out = io.StringIO()
    with mock.patch.object(reap.psycopg, "connect", return_value=conn):
        with redirect_stdout(out):
            code = reap.main(argv)
    return code, out.getvalue()


class ShaSelectorTests(unittest.TestCase):
    def test_hex_is_accepted_and_normalised(self):
        self.assertEqual(reap.sha_selector("5C991BFA4E90"), "5c991bfa4e90")
        self.assertEqual(reap.sha_selector(" abc123 "), "abc123")
        self.assertEqual(reap.sha_selector("f" * 64), "f" * 64)

    def test_empty_and_wildcards_are_refused(self):
        # An unset "$SHA" is the case that matters: it became LIKE '%'.
        for bad in ("", "   ", "%", "_", "5c99%", "5c99_1", "g" * 8,
                    "f" * 65, "5c99 4e90"):
            with self.assertRaises(reap.argparse.ArgumentTypeError, msg=bad):
                reap.sha_selector(bad)

    def test_parse_args_rejects_a_bad_sha_before_any_query(self):
        # argparse prints its usage to stderr; swallow it so a passing suite
        # stays readable.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                reap.parse_args(["--dsn", "x", "--sha", ""])


class WindowGuardTests(unittest.TestCase):
    def test_short_window_is_refused(self):
        conn = FakeConn([row("aa")])
        with self.assertRaises(SystemExit) as caught:
            run_main(conn, ["--dsn", "x", "--older-than-hours", "1",
                            "--apply"])
        self.assertIn("--allow-short-window", str(caught.exception))
        self.assertEqual(conn.updates, [])

    def test_short_window_allowed_explicitly(self):
        conn = FakeConn([row("aa", age=1.5)])
        code, _ = run_main(conn, ["--dsn", "x", "--older-than-hours", "1",
                                  "--allow-short-window", "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(len(conn.updates), 1)

    def test_default_window_is_at_or_above_the_floor(self):
        # The floor exists because a live worker's heartbeat can be one
        # container run (MAX_DOC_SECONDS, 0.92 h) old.
        self.assertGreaterEqual(reap.DEFAULT_HOURS, reap.MIN_SAFE_HOURS)
        self.assertGreaterEqual(reap.MIN_SAFE_HOURS, 2.0)


class CandidateSplitTests(unittest.TestCase):
    def test_rows_held_by_a_live_worker_are_never_updated(self):
        conn = FakeConn([row("aa", live=True), row("bb", live=False)])
        code, out = run_main(conn, ["--dsn", "x", "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual([u[0] for u in conn.updates], ["bb"])
        self.assertIn("1 reapable, 1 held by a live worker", out)

    def test_dry_run_is_the_default_and_writes_nothing(self):
        conn = FakeConn([row("aa")])
        code, out = run_main(conn, ["--dsn", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(conn.updates, [])
        self.assertIn("dry run", out)

    def test_compare_and_set_miss_reports_and_exits_nonzero(self):
        conn = FakeConn([row("aa"), row("bb")], reap_rowcounts=[1, 0])
        code, out = run_main(conn, ["--dsn", "x", "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("reaped 1, skipped 1", out)
        self.assertIn("still owns it", out)

    def test_reap_reason_names_the_age_and_the_threshold(self):
        reason = reap.reap_reason(9.25, 4.0, "host-3")
        self.assertTrue(reason.startswith("stale_running_reaped:"))
        self.assertIn("9.2h", reason)
        self.assertIn("threshold 4h", reason)
        self.assertIn("host-3", reason)


class LimitTests(unittest.TestCase):
    def test_limit_caps_reaped_rows_not_rows_read(self):
        # The two oldest are held: a SQL LIMIT 1 would have reaped nothing.
        conn = FakeConn([row("aa", live=True), row("bb", live=True),
                         row("cc"), row("dd")])
        code, out = run_main(conn, ["--dsn", "x", "--apply", "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertEqual([u[0] for u in conn.updates], ["cc"])
        self.assertIn("leaving 1 for a later pass", out)

    def test_select_never_carries_a_sql_limit(self):
        conn = FakeConn([row("aa")])
        run_main(conn, ["--dsn", "x", "--limit", "5"])
        self.assertNotIn("LIMIT", conn.select_sql.upper())

    def test_sha_selector_binds_a_prefix_pattern(self):
        conn = FakeConn([row("aa")])
        run_main(conn, ["--dsn", "x", "--sha", "5c991bfa"])
        self.assertIn("LIKE", conn.select_sql)
        self.assertIn("5c991bfa%", conn.select_params)


if __name__ == "__main__":
    unittest.main()
