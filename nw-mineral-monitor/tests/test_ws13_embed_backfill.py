"""Concurrency regression tests for the WS13 embedding backfill.

The defect these cover: the backfill claimed work with
`SELECT ... FOR UPDATE SKIP LOCKED` on an autocommit connection, so every
statement was its own transaction and the row locks were released before the
Bedrock call. All TITAN_THREADS then worked the same head of the NULL set,
measured in production at 5-6x duplicate invocations against a rate-limited,
billed API.

`FakeDB` implements just enough of the SELECT/UPDATE semantics the backfill
relies on -- ordering, LIMIT, the id cursor, the modulo shard predicate, the
model- and reason-scoped skips anti-join, and NULL filtering -- so the real
`titan_worker` / `titan_mopup` / `cohere_worker` functions run unmodified
against it under real threads.
"""
import os
import re
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

os.environ.setdefault("WS13_DB_DSN", "postgresql://test/test")
os.environ.setdefault("WS13_BUCKET", "test-bucket")
# Effectively unlimited so the token bucket never paces the tests.
os.environ["WS13_TITAN_TPM"] = "100000000"

# The driver and SDK are deployment dependencies of the worker nodes, not of
# the test host; every call through them is stubbed below.
for name in ("psycopg", "boto3"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            stub = types.ModuleType(name)
            stub.connect = mock.MagicMock()
            stub.client = mock.MagicMock()
            sys.modules[name] = stub

import boto3

with mock.patch.object(boto3, "client", return_value=mock.MagicMock()):
    import ws13_embed_backfill as bf


# Captured before any test patches `time.sleep`. `RecordingBedrock` paces
# itself with this, so a `time.sleep` patch that outlives the test that made
# it cannot silently reduce the concurrency tests below to serial ones.
_REAL_SLEEP = time.sleep


class _Parked(Exception):
    """Stands in for a worker's park sleep, so a worker that is designed never
    to return can be run to its parking point and unwound on this thread."""


def chunk_text(i):
    return f"chunk-{i}-" + "x" * 200


def id_of(text):
    return int(text.split("-")[1])


class FakeDB:
    """Minimal, thread-safe stand-in for the ws13_chunks table."""

    def __init__(self, ids):
        self.rows = {
            i: {"id": i, "text": chunk_text(i), "titan_embedding": None,
                "embedding": None, "qwen_embedding": None}
            for i in ids
        }
        # (chunk_id, model) -> reason, mirroring the real
        # PRIMARY KEY (chunk_id, model). A bare id set could not represent a
        # Qwen skip distinctly from a Titan one, which is exactly the bug
        # this dimension exists to catch.
        self.skips = {}
        self.lock = threading.Lock()

    def add(self, ids):
        with self.lock:
            for i in ids:
                self.rows[i] = {"id": i, "text": chunk_text(i),
                                "titan_embedding": None, "embedding": None,
                                "qwen_embedding": None}

    def _skipped(self, rid, model, reasons):
        """model=None means the anti-join is unscoped and matches any model;
        empty reasons means it matches any reason. Modelling those widenings
        is what lets the suite detect an under-scoped anti-join."""
        for (cid, m), reason in list(self.skips.items()):
            if cid != rid:
                continue
            if model is not None and m != model:
                continue
            if reasons and reason not in reasons:
                continue
            return True
        return False

    def select(self, column, cursor=None, shards=None, shard=None, limit=50,
               skip_join=False, skip_model=None, skip_reasons=(),
               require_text=False):
        with self.lock:
            out = []
            for i in sorted(self.rows):
                r = self.rows[i]
                if r[column] is not None:
                    continue
                if cursor is not None and i <= cursor:
                    continue
                if shards is not None and shards > 1 and i % shards != shard:
                    continue
                if skip_join and self._skipped(i, skip_model, skip_reasons):
                    continue
                if require_text and not r["text"]:
                    continue
                out.append((r["id"], r["text"]))
                if len(out) >= limit:
                    break
            return out

    def update(self, column, value, rid):
        with self.lock:
            if rid in self.rows:
                self.rows[rid][column] = value
                return 1
            return 0

    def remaining(self, column, skip_model=None, skip_reasons=(),
                  skip_join=False):
        with self.lock:
            return sum(
                1 for r in self.rows.values()
                if r[column] is None
                and not (skip_join and self._skipped(r["id"], skip_model, skip_reasons))
            )


class FakeConn:
    """Parses only the statements the backfill issues."""

    def __init__(self, db):
        self.db = db
        self.closed = False
        self._result = []

    @staticmethod
    def _skip_scope(sql):
        """Recover the model/reasons the anti-join is scoped to, so the fake
        enforces the same scoping the real query does."""
        m = re.search(r"s\d*\.model\s*=\s*'([^']+)'", sql)
        reasons = tuple(re.findall(r"'([^']+)'",
                                   re.search(r"reason IN \(([^)]*)\)", sql).group(1))
                        ) if "reason IN (" in sql else ()
        return (m.group(1) if m else None), reasons

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE"):
            return self
        if s.startswith("INSERT INTO ws13_embed_skips"):
            with self.db.lock:
                self.db.skips[(params[0], params[1])] = params[2]
            return self
        if s.startswith("UPDATE ws13_chunks SET titan_embedding"):
            self.db.update("titan_embedding", params[0], params[1])
            return self
        if s.startswith("UPDATE ws13_chunks SET embedding"):
            self.db.update("embedding", params[0], params[1])
            return self
        if s.startswith("SELECT titan_embedding IS NOT NULL"):
            with self.db.lock:
                self._result = [(self.db.rows[params[0]]["titan_embedding"]
                                 is not None,)]
            return self
        if s.startswith("SELECT COUNT(*), COUNT(*) FILTER"):        # heartbeat
            self._result = [(len(self.db.rows),
                             self.db.remaining("titan_embedding"),
                             self.db.remaining("embedding"),
                             self.db.remaining("qwen_embedding"))]
            return self
        if s.startswith("SELECT COUNT(*) FILTER"):                  # outstanding
            self._result = [(
                self.db.remaining("titan_embedding", bf.TITAN_TAG,
                                  bf.TERMINAL_REASONS, skip_join=True),
                self.db.remaining("embedding", bf.COHERE_TAG,
                                  bf.TERMINAL_REASONS, skip_join=True))]
            return self
        if "FROM ws13_chunks c" in s and s.startswith("SELECT c.id, c.text"):
            column = ("titan_embedding" if "c.titan_embedding IS NULL" in s
                      else "embedding")
            has_skips = "ws13_embed_skips" in s
            model, reasons = self._skip_scope(s)
            scope = dict(skip_join=has_skips, skip_model=model,
                         skip_reasons=reasons)
            if "c.text <> ''" in s:                                 # preflight
                self._result = self.db.select(column, limit=1, require_text=True)
            elif "%%" in sql:                                       # sharded
                cursor, shards, shard, limit = params
                self._result = self.db.select(column, cursor=cursor, shards=shards,
                                              shard=shard, limit=limit, **scope)
            else:                                                   # cursor scan
                cursor, limit = params
                self._result = self.db.select(column, cursor=cursor, limit=limit,
                                              **scope)
            return self
        raise AssertionError(f"unhandled SQL: {s[:160]}")

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        self.closed = True


class RecordingBedrock:
    """Counts invocations per chunk id so duplicates are visible.

    `latency` models the real thing: a Bedrock embed call takes far longer
    than the SELECT that claimed the row. Without it the fake API returns so
    fast that each thread drains the table before the next is scheduled, the
    threads never interleave, and neither the duplication control nor the
    exactly-once assertions are testing anything. It is spent through
    `_REAL_SLEEP` rather than `time.sleep` precisely because several tests
    here patch `time.sleep` on the real `time` module: a patch that escapes
    its test would otherwise disarm every concurrency test downstream of it
    without any of them failing.

    `rendezvous` makes overlap a property of the harness instead of an
    outcome of the scheduler: the first call from each of `rendezvous`
    threads blocks until all of them have arrived, so a test can assert its
    callers were genuinely in flight together. The wait is bounded, so a
    shortfall is a failed assertion rather than a hung process.
    """

    def __init__(self, latency=0.002, reject_ids=(), rendezvous=0):
        self.calls = {}
        self.latency = latency
        self.reject_ids = set(reject_ids)
        self.lock = threading.Lock()
        self._gate = threading.Barrier(rendezvous) if rendezvous > 1 else None
        self.rendezvous_met = self._gate is None
        self._arrival = threading.local()

    def _rendezvous(self):
        """Called outside self.lock: this blocks, and the counter must not."""
        if self._gate is None or getattr(self._arrival, "done", False):
            return
        self._arrival.done = True
        try:
            self._gate.wait(timeout=60)
            self.rendezvous_met = True
        except threading.BrokenBarrierError:
            pass

    def invoke_model(self, modelId, body):
        import json as _json
        self._rendezvous()
        payload = _json.loads(body)
        with self.lock:
            if "texts" in payload:                       # Cohere batch
                for t in payload["texts"]:
                    self.calls[id_of(t)] = self.calls.get(id_of(t), 0) + 1
                result = {"body": _Body(_json.dumps(
                    {"embeddings": {"float": [[0.1] * 4 for _ in payload["texts"]]}}))}
            else:
                rid = id_of(payload["inputText"])
                self.calls[rid] = self.calls.get(rid, 0) + 1
                if rid in self.reject_ids:
                    raise RuntimeError(f"ValidationException: rejected {rid}")
                result = {"body": _Body(_json.dumps(
                    {"embedding": [0.1] * 4, "inputTextTokenCount": 60}))}
        _REAL_SLEEP(self.latency)                        # outside the lock
        return result


class _Body:
    def __init__(self, s):
        self.s = s

    def read(self):
        return self.s


class BackfillTestCase(unittest.TestCase):
    def reset_module_state(self):
        bf.bucket_tokens = float(bf.BUCKET_CAP)
        bf.token_ratio = 1.0
        for k in bf.stats:
            bf.stats[k] = 0

    def patched(self):
        return (mock.patch.object(bf, "bedrock", self.bedrock),
                mock.patch.object(bf, "psycopg"))


class TitanShardingTest(BackfillTestCase):
    def setUp(self):
        self.db = FakeDB(range(1, 601))
        self.bedrock = RecordingBedrock()
        self.reset_module_state()

    def _run_shards(self, threads):
        # Barrier so every shard is genuinely in flight at once rather than
        # the first one draining the table before the rest are scheduled.
        gate = threading.Barrier(threads)

        def run(shard):
            gate.wait()
            bf.titan_worker(shard, threads)

        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg:
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            workers = [threading.Thread(target=run, args=(i,))
                       for i in range(threads)]
            for t in workers:
                t.start()
            for t in workers:
                t.join(timeout=120)
            self.assertFalse([t for t in workers if t.is_alive()],
                             "shard threads did not finish")

    def test_every_chunk_embedded_exactly_once(self):
        # Hold all twelve shards at their first Bedrock call until every one
        # of them has arrived. Otherwise "exactly once" is also what a run
        # that never overlapped would report, and the assertion below would
        # pass trivially. Ids 1..600 over 12 shards give each thread 50 rows,
        # so every thread is guaranteed to reach the rendezvous.
        self.bedrock = RecordingBedrock(rendezvous=12)
        self._run_shards(12)
        self.assertTrue(self.bedrock.rendezvous_met,
                        "the shard threads never ran concurrently; an "
                        "exactly-once result from a serialised run is vacuous")
        self.assertEqual(self.db.remaining("titan_embedding"), 0,
                         "every chunk must end with a titan vector")
        dupes = {i: n for i, n in self.bedrock.calls.items() if n != 1}
        self.assertEqual(dupes, {}, f"chunks embedded more than once: {dupes}")
        self.assertEqual(len(self.bedrock.calls), 600)
        self.assertEqual(bf.stats["titan"], 600)

    def test_shards_are_disjoint(self):
        """Exercises the real titan_fetch, not the double's own predicate."""
        conn = FakeConn(self.db)
        seen = {}
        for shard in range(12):
            cursor = -1
            while True:
                rows = bf.titan_fetch(conn, shard, 12, cursor)
                if not rows:
                    break
                for rid, _ in rows:
                    self.assertNotIn(
                        rid, seen,
                        f"id {rid} claimed by shards {seen.get(rid)} and {shard}")
                    seen[rid] = shard
                cursor = rows[-1][0]
        self.assertEqual(len(seen), 600, "sharding must cover every row")

    def test_single_thread_configuration_still_covers_everything(self):
        self._run_shards(1)
        self.assertEqual(self.db.remaining("titan_embedding"), 0)
        self.assertEqual({i: n for i, n in self.bedrock.calls.items() if n != 1}, {})

    def test_old_unsharded_claim_duplicates_work(self):
        """Control: the pre-fix claim pattern, so this suite can detect a
        regression back to it. Under autocommit the SKIP LOCKED lock was
        released immediately, which is equivalent to no claim at all -- every
        thread repeatedly reads the same head of the NULL set.

        The interleaving is forced, not hoped for: every thread finishes its
        claim before any thread records a result, which is exactly what the
        released lock permitted. Left to the scheduler this control failed
        intermittently -- and a control that only sometimes reproduces the
        defect certifies nothing on the runs where it does not.
        """
        db, bedrock = self.db, self.bedrock
        threads, batch = 12, 50
        claimed = threading.Barrier(threads)

        def legacy_worker():
            conn = FakeConn(db)
            # The claim, then the barrier: the released lock's whole meaning
            # is that a second thread can claim what a first one has not yet
            # written, so hold every thread here until all of them have.
            rows = db.select("titan_embedding", limit=batch)  # no shard/cursor
            claimed.wait(timeout=60)
            while rows:
                for rid, text in rows:
                    import json as _json
                    bedrock.invoke_model(modelId=bf.TITAN,
                                         body=_json.dumps({"inputText": text}))
                    conn.execute(
                        "UPDATE ws13_chunks SET titan_embedding=%s WHERE id=%s",
                        ("[0.1]", rid))
                rows = db.select("titan_embedding", limit=batch)

        workers = [threading.Thread(target=legacy_worker) for _ in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=120)
        self.assertFalse([t for t in workers if t.is_alive()],
                         "legacy threads did not finish")

        # Every thread claimed the same head batch -- ids 1..batch, the first
        # `batch` NULL rows in id order -- and embeds all of them before it
        # looks for more, so each of those ids is billed once per thread and
        # no thread can revisit them afterwards. That equality is the
        # duplication the shard partition exists to remove, stated exactly.
        head = {i: self.bedrock.calls.get(i, 0) for i in range(1, batch + 1)}
        self.assertEqual(
            head, {i: threads for i in range(1, batch + 1)},
            "a claim that releases its lock must bill every thread for the "
            "same head batch")
        total = sum(self.bedrock.calls.values())
        self.assertGreater(
            total, 600,
            "control must reproduce duplication; if this fails the harness is "
            "not exercising concurrency and the positive tests prove nothing")


class ShardTerminationTest(BackfillTestCase):
    """A shard must never be pinned by a row it cannot fill -- main() joins
    every shard before titan_mopup(), so one stuck thread means the mop-up
    never runs and the process never exits."""

    def setUp(self):
        self.db = FakeDB(range(1, 61))
        self.reset_module_state()

    def _run_one_shard(self, timeout=30):
        done = threading.Event()

        def run():
            bf.titan_worker(0, 1)
            done.set()

        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg, \
             mock.patch.object(bf.time, "sleep"):
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            t = threading.Thread(target=run, daemon=True)
            t.start()
            finished = done.wait(timeout=timeout)
        return finished

    def test_empty_text_row_does_not_pin_the_shard(self):
        self.db.rows[7]["text"] = ""
        self.bedrock = RecordingBedrock()
        self.assertTrue(self._run_one_shard(), "titan_worker hung on an empty-text row")
        self.assertEqual(self.db.skips.get((7, bf.TITAN_TAG)), "empty_text")
        self.assertEqual(self.db.remaining("titan_embedding"), 1)

    def test_permanently_failing_row_does_not_pin_the_shard(self):
        self.bedrock = RecordingBedrock(reject_ids=[9])
        self.assertTrue(self._run_one_shard(),
                        "titan_worker hung on a permanently failing row")
        # Not skipped by the shard pass -- give-up is the mop-up's job -- but
        # the thread must still exit so the mop-up can run.
        self.assertEqual(self.db.remaining("titan_embedding"), 1)
        self.assertIsNone(self.db.rows[9]["titan_embedding"])

    def test_all_rows_failing_still_terminates(self):
        self.bedrock = RecordingBedrock(reject_ids=range(1, 61))
        self.assertTrue(self._run_one_shard(),
                        "titan_worker hung when no row could be filled")
        self.assertEqual(self.db.remaining("titan_embedding"), 60)


class SkipScopingTest(BackfillTestCase):
    """ws13_embed_skips is keyed (chunk_id, model) and shared with the Qwen
    overlay. An unscoped anti-join let a Qwen fp16 overflow suppress a
    perfectly embeddable Titan row."""

    def setUp(self):
        self.db = FakeDB(range(1, 41))
        self.bedrock = RecordingBedrock()
        self.reset_module_state()

    def test_qwen_skip_does_not_suppress_titan_coverage(self):
        self.db.skips[(11, "qwen3-8b-fp16")] = "nan_vector"
        self.db.skips[(12, "qwen3-8b-fp16")] = "nan_vector"
        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg, \
             mock.patch.object(bf.time, "sleep"):
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            bf.titan_mopup()
        self.assertIsNotNone(self.db.rows[11]["titan_embedding"],
                             "a qwen skip must not exclude the row from titan")
        self.assertIsNotNone(self.db.rows[12]["titan_embedding"])
        self.assertEqual(self.db.remaining("titan_embedding"), 0)

    def test_transient_giveup_is_reattempted_on_a_later_run(self):
        """'retries_exhausted' is not terminal: a throttle storm must not
        permanently abandon rows."""
        self.db.skips[(5, bf.TITAN_TAG)] = "retries_exhausted"
        self.db.skips[(6, bf.TITAN_TAG)] = "empty_text"
        conn = FakeConn(self.db)
        ids = {rid for rid, _ in bf.titan_fetch(conn, 0, 1, -1)}
        self.assertIn(5, ids, "a retries_exhausted row must be retried")
        self.assertNotIn(6, ids, "an empty_text row is terminal")


class MopupTest(BackfillTestCase):
    def setUp(self):
        self.db = FakeDB(range(1, 121))
        self.bedrock = RecordingBedrock()
        self.reset_module_state()

    def test_mopup_catches_rows_inserted_after_shards_finished(self):
        """ws13_worker re-extraction inserts new chunks mid-run; the shard
        cursors have already passed those ids."""
        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg:
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            workers = [threading.Thread(target=bf.titan_worker, args=(i, 4))
                       for i in range(4)]
            for t in workers:
                t.start()
            for t in workers:
                t.join(timeout=60)
            self.db.add(range(900, 950))          # late arrivals
            self.assertEqual(self.db.remaining("titan_embedding"), 50)
            filled = bf.titan_mopup()

        self.assertEqual(filled, 50)
        self.assertEqual(self.db.remaining("titan_embedding"), 0)
        self.assertEqual({i: n for i, n in self.bedrock.calls.items() if n != 1}, {})

    def test_mopup_records_empty_text_instead_of_looping(self):
        self.db.rows[5]["text"] = ""
        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg:
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            bf.titan_mopup()
        self.assertEqual(self.db.skips.get((5, bf.TITAN_TAG)), "empty_text")
        self.assertIsNone(self.db.rows[5]["titan_embedding"])
        self.assertEqual(self.db.remaining("titan_embedding"), 1)

    def test_mopup_records_terminal_failure_and_terminates(self):
        self.bedrock = RecordingBedrock(reject_ids=[3])
        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg, \
             mock.patch.object(bf.time, "sleep"):
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            bf.titan_mopup()
        self.assertEqual(self.db.skips.get((3, bf.TITAN_TAG)), "retries_exhausted")


class PreflightTest(BackfillTestCase):
    def setUp(self):
        self.db = FakeDB(range(1, 11))
        self.reset_module_state()

    def _preflight(self):
        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg, \
             mock.patch.object(bf.time, "sleep"):
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            return bf.preflight()

    def test_preflight_passes_when_the_write_lands(self):
        self.bedrock = RecordingBedrock()
        self.assertTrue(self._preflight())
        self.assertIsNotNone(self.db.rows[1]["titan_embedding"])

    def test_preflight_fails_when_the_column_rejects_the_write(self):
        """The unverified titan_embedding column type: if it rejects the
        JSON-string form, every row would fail after 8 billed invocations."""
        self.bedrock = RecordingBedrock(reject_ids=range(1, 11))
        self.assertFalse(self._preflight())


class TokenBucketTest(BackfillTestCase):
    def setUp(self):
        self.reset_module_state()

    def test_oversized_request_cannot_deadlock(self):
        """A row whose estimate exceeds the reservoir must be clamped, not
        block forever against a bucket that can never hold enough."""
        done = threading.Event()
        threading.Thread(
            target=lambda: (bf.take_tokens(bf.BUCKET_CAP * 100), done.set()),
            daemon=True).start()
        self.assertTrue(done.wait(timeout=10), "take_tokens deadlocked")

    def test_estimate_is_clamped_to_bucket_capacity(self):
        self.assertLessEqual(bf.estimate_tokens("x" * 10 ** 8), bf.BUCKET_CAP)

    def test_settle_debits_real_token_shortfall(self):
        before = bf.bucket_tokens
        bf.settle_tokens(estimated=100, real=500)
        self.assertAlmostEqual(bf.bucket_tokens, before - 400, places=3)
        self.assertEqual(bf.stats["titan_tokens_real"], 500)

    def test_token_ratio_never_falls_below_one(self):
        for _ in range(500):
            bf.settle_tokens(estimated=1000, real=10)
        self.assertGreaterEqual(bf.token_ratio, 1.0)


class CohereBudgetTest(BackfillTestCase):
    def setUp(self):
        self.db = FakeDB(range(1, 81))
        self.reset_module_state()
        self.saved = {}
        self.stored = None

    def _run_cohere(self, sleep=None):
        def fake_save(day, spent):
            self.saved[day.isoformat()] = spent

        with mock.patch.object(bf, "bedrock", self.bedrock), \
             mock.patch.object(bf, "psycopg") as pg, \
             mock.patch.object(bf, "save_cohere_spent", fake_save), \
             mock.patch.object(bf, "load_cohere_spent",
                               lambda day: self.stored or 0), \
             mock.patch.object(bf.time, "sleep", side_effect=sleep):
            pg.connect.side_effect = lambda *a, **k: FakeConn(self.db)
            bf.cohere_worker()

    def test_failed_batch_does_not_consume_daily_budget(self):
        """Charging the budget before the call let a throttled request burn
        quota that was never spent at the service."""
        calls = {"n": 0}

        class FailingBedrock(RecordingBedrock):
            def invoke_model(self, modelId, body):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("ThrottlingException: slow down")
                return super().invoke_model(modelId=modelId, body=body)

        self.bedrock = FailingBedrock()
        self._run_cohere()

        self.assertEqual(self.db.remaining("embedding"), 0)
        self.assertEqual(bf.stats["cohere_throttle"], 1)
        self.assertEqual(bf.stats["cohere"], 80,
                         "the throttled batch must be retried, not skipped")
        # 80 chunks of ~210 chars => ~70 tokens each. A pre-charged failure
        # would have inflated this by the size of the failed batch.
        self.assertLess(bf.stats["cohere_tokens_spent"], 80 * 80)

    def test_spend_is_checkpointed_so_a_restart_cannot_double_spend(self):
        """The daily allowance is an account cap. A process restarting at
        midday with spent=0 would re-spend the whole budget."""
        self.bedrock = RecordingBedrock()
        self._run_cohere()
        self.assertTrue(self.saved, "spend was never checkpointed")
        self.assertEqual(max(self.saved.values()), bf.stats["cohere_tokens_spent"])

    def test_restart_resumes_from_the_checkpointed_spend(self):
        self.bedrock = RecordingBedrock()
        self.stored = bf.COHERE_DAILY          # budget already exhausted today
        # With the budget spent the worker parks and never returns, so the
        # park itself is raised and caught here on this thread. Running it in
        # a daemon thread and waiting two seconds instead left it parked
        # *inside* `_run_cohere`'s context managers: they never unwound,
        # `bf.time.sleep` -- which is the real `time.sleep` -- stayed mocked
        # for the rest of the process, and every later concurrency test
        # silently lost the call latency it needs in order to interleave.
        with self.assertRaises(_Parked):
            self._run_cohere(sleep=_Parked)
        self.assertEqual(bf.stats["cohere"], 0,
                         "a resumed process must respect the day's prior spend")


if __name__ == "__main__":
    unittest.main(verbosity=2)
