"""Contract tests for the WS13 embedding bake-off (pipelines/ws13_vector_bakeoff.py).

The harness exists to stop four more weeks of Cohere spend on an untested
hypothesis, so the ways it could fail are the ways a bake-off launders a prior
into a measurement. These pin each one down:

  * An unreproducible sample proves nothing. Membership is decided by
    sha256(seed:chunk_id), so the same seed must rebuild the identical sample
    and a different seed must not -- and the mandatory known-item chunks must
    survive every draw, including one whose sample_size is smaller than the
    mandatory set.
  * The degraded half of the corpus is the half a new embedding model is
    supposed to help with, so the stratified draw must actually reach the
    degraded and unknown quality bands rather than filling up on clean text.
  * Writing 20,000 rows into ws13_chunks.embedding would make "we tried it"
    indistinguishable from "we committed to it", and the way back would be an
    UPDATE against the live table. Every mutating statement is checked against
    its target, and a full run is driven end to end over a fake corpus with the
    three production vector columns snapshotted before and after.
  * The scoring must be the shipping ranker. rrf_fuse and RRF_K are imported
    from infra/ws13_query_lambda.py; this suite asserts the function the module
    actually calls was defined in that file, and that the module defines no
    rrf_fuse of its own.
  * A bake-off that always names a winner is worse than none, so the refusal
    paths -- too few labelled queries, an interval spanning zero, a candidate
    with partial coverage -- are tested as first-class outcomes.
  * The token budget is what stops the experiment becoming the runaway spend it
    exists to prevent, so it is tested as a hard stop that yields partial
    coverage and an excluded candidate, not a warning.

Nothing here touches the network, a database or AWS. psycopg and boto3 are
stubbed as module stubs the way tests/test_ws13_embed_backfill.py does it, and
FakeConn parses exactly the statements the harness issues -- so the real
embed_sample(), score_candidate() and run_bakeoff() run unmodified against it.
"""
import hashlib
import json
import math
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
sys.path.insert(0, str(ROOT / "pipelines"))

# Deployment dependencies of the fleet and the Lambda zip, not of this host.
for _name in ("psycopg", "boto3"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            _stub = types.ModuleType(_name)
            _stub.connect = mock.MagicMock()
            _stub.client = mock.MagicMock()
            sys.modules[_name] = _stub

import ws13_query_lambda as ql  # noqa: E402
import ws13_vector_bakeoff as bo  # noqa: E402

MODULE_SOURCE = Path(bo.__file__).read_text(encoding="utf-8")
STATES = ("ID", "MT", "WA")
CLASSES = ("originals", "licensed-copies", "research-copies")
# One score per band, so every band is populated and a draw that skips one is
# visible rather than merely unlikely.
SCORES = (None, 12.0, 47.0, 61.0, 88.0)
KNOWN_SHA = "3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1"


def bag_vector(text, dims):
    """A deterministic, meaning-bearing stand-in embedding.

    Hashing words into a bag makes texts that share words genuinely close, so
    the fused ranking the suite scores is a real ranking rather than an
    arbitrary permutation -- which is what makes the end-to-end run able to
    fail for a real reason.
    """
    values = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        index = int(hashlib.sha256(word.encode()).hexdigest(), 16) % dims
        values[index] += 1.0
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        values[0] = 1.0
        return values
    return [value / norm for value in values]


def cosine(first, second):
    return sum(a * b for a, b in zip(first, second))


class FakeCorpus:
    """ws13_documents / ws13_pages / ws13_chunks plus the bake-off tables."""

    def __init__(self, documents=6, chunks_per_doc=4):
        self.documents = {}
        self.pages = {}
        self.chunks = {}
        self.runs = {}
        self.sample = {}
        self.vectors = {}
        self.mutations = []
        chunk_id = 1
        for index in range(documents):
            sha = hashlib.sha256(f"doc-{index}".encode()).hexdigest()
            if index == 0:
                sha = KNOWN_SHA
            self.documents[sha] = {
                "admission_class": CLASSES[index % len(CLASSES)],
                "state": STATES[index % len(STATES)],
            }
            for page in range(1, chunks_per_doc + 1):
                self.pages[(sha, page)] = SCORES[(index + page) % len(SCORES)]
                text = (f"lava creek district butte county mine {index} "
                        f"page {page} assay gold quartz vein sample")
                self.chunks[chunk_id] = {
                    "id": chunk_id, "sha256": sha, "page": page, "text": text,
                    "titan_embedding": bag_vector(text, 1024),
                    "embedding": None, "qwen_embedding": None,
                }
                chunk_id += 1

    def production_snapshot(self):
        """The three production vector columns, exactly as they stand."""
        return {
            column: {cid: row[column] for cid, row in sorted(self.chunks.items())}
            for column in bo.PRODUCTION_VECTOR_COLUMNS
        }


class FakeConn:
    """Parses only the statements the bake-off issues, and guards every write."""

    def __init__(self, corpus):
        self.corpus = corpus
        self.rowcount = 0
        self._result = []
        self.closed = False

    def close(self):
        self.closed = True

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        if bo.is_mutating(statement):
            # The real guard, not a copy of it: a statement the harness could
            # issue that this refuses is a defect in the harness.
            bo.assert_write_allowed(statement)
            self.corpus.mutations.append((bo.write_target(statement), statement))
        self.rowcount = 0
        self._result = []
        handler = self._route(statement)
        if handler is None:
            raise AssertionError(f"unhandled SQL: {statement[:180]}")
        handler(statement, params)
        return self

    def _route(self, statement):
        table = [
            ("CREATE TABLE", self._noop),
            ("INSERT INTO ws13_bakeoff_runs", self._insert_run),
            ("SELECT sampling_rule", self._load_run),
            ("INSERT INTO ws13_bakeoff_sample", self._insert_sample),
            ("SELECT chunk_id, sha256, page, stratum, mandatory", self._load_sample),
            ("SELECT s.chunk_id, c.text", self._pending),
            ("SELECT COUNT(*), COALESCE(SUM(tokens), 0)", self._coverage),
            ("SELECT b.chunk_id", self._rank_vector),
            ("SELECT c.id FROM ws13_bakeoff_sample", self._rank_lexical),
            ("SELECT d.admission_class", self._stratum_counts),
            ("SELECT c.id, c.sha256, c.page", self._chunk_rows),
        ]
        if statement.startswith("INSERT INTO ws13_bakeoff_vectors"):
            return (self._copy_titan if " SELECT " in statement
                    else self._insert_vector)
        for prefix, handler in table:
            if statement.startswith(prefix):
                return handler
        return None

    def _noop(self, statement, params):
        return None

    def _insert_run(self, statement, params):
        run_id, seed, size, candidates, rule = params
        self.corpus.runs.setdefault(run_id, {
            "seed": seed, "sample_size": size,
            "candidates": list(candidates), "rule": json.loads(rule)})

    def _load_run(self, statement, params):
        run = self.corpus.runs.get(params[0])
        # jsonb comes back decoded from psycopg; mirror that, not a string.
        self._result = [(run["rule"],)] if run else []

    def _insert_sample(self, statement, params):
        run_id, chunk_id, sha, page, stratum, mandatory = params
        self.corpus.sample.setdefault((run_id, int(chunk_id)), {
            "sha256": sha, "page": int(page), "stratum": stratum,
            "mandatory": bool(mandatory)})

    def _load_sample(self, statement, params):
        run_id = params[0]
        self._result = [
            (cid, row["sha256"], row["page"], row["stratum"], row["mandatory"])
            for (rid, cid), row in sorted(self.corpus.sample.items())
            if rid == run_id]

    def _pending(self, statement, params):
        run_id, candidate = params
        self._result = [
            (cid, self.corpus.chunks[cid]["text"])
            for (rid, cid) in sorted(self.corpus.sample)
            if rid == run_id and (run_id, candidate, cid) not in self.corpus.vectors]

    def _coverage(self, statement, params):
        run_id, candidate = params
        rows = [row for (rid, cand, _cid), row in self.corpus.vectors.items()
                if rid == run_id and cand == candidate]
        self._result = [(len(rows), sum(row["tokens"] for row in rows))]

    def _copy_titan(self, statement, params):
        candidate, dims, run_id = params
        copied = 0
        for (rid, cid) in sorted(self.corpus.sample):
            if rid != run_id:
                continue
            vector = self.corpus.chunks[cid]["titan_embedding"]
            if vector is None:
                continue
            key = (run_id, candidate, cid)
            if key in self.corpus.vectors:
                continue
            self.corpus.vectors[key] = {"dims": dims, "vec": list(vector),
                                        "tokens": 0}
            copied += 1
        self.rowcount = copied

    def _insert_vector(self, statement, params):
        run_id, candidate, chunk_id, dims, vec, tokens = params
        self.corpus.vectors.setdefault((run_id, candidate, int(chunk_id)), {
            "dims": int(dims), "vec": list(vec), "tokens": int(tokens)})
        self.rowcount = 1

    def _rank_vector(self, statement, params):
        run_id, candidate, dims, literal, limit = params
        query = json.loads(literal)
        scored = []
        for (rid, cand, cid), row in self.corpus.vectors.items():
            if rid != run_id or cand != candidate or row["dims"] != dims:
                continue
            scored.append((1.0 - cosine(row["vec"], query), cid))
        scored.sort()
        self._result = [(cid,) for _distance, cid in scored[:limit]]

    def _rank_lexical(self, statement, params):
        run_id, query, _query_again, limit = params
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        scored = []
        for (rid, cid) in self.corpus.sample:
            if rid != run_id:
                continue
            words = set(re.findall(r"[a-z0-9]+",
                                   self.corpus.chunks[cid]["text"].lower()))
            overlap = len(terms & words)
            if overlap:
                scored.append((-overlap, cid))
        scored.sort()
        self._result = [(cid,) for _rank, cid in scored[:limit]]

    def _stratum_counts(self, statement, params):
        counts = {}
        for row in self.corpus.chunks.values():
            document = self.corpus.documents[row["sha256"]]
            score = self.corpus.pages[(row["sha256"], row["page"])]
            key = (document["admission_class"], document["state"], score)
            counts[key] = counts.get(key, 0) + 1
        self._result = [(klass, state, score, total)
                        for (klass, state, score), total in sorted(
                            counts.items(), key=lambda item: str(item[0]))]

    def _chunk_rows(self, statement, params):
        if "JOIN ws13_documents" in statement:
            return self._pool(statement, params)
        shas = set(params[0])
        self._result = [(row["id"], row["sha256"], row["page"])
                        for row in self.corpus.chunks.values()
                        if row["sha256"] in shas]

    def _pool(self, statement, params):
        klass, state, seed, limit = params
        band = None
        for name, fragment in bo.BAND_SQL.items():
            if fragment in statement:
                band = name if band is None else max(band, name, key=len)
        assert band is not None, f"no band predicate in: {statement[:180]}"
        rows = []
        for row in self.corpus.chunks.values():
            document = self.corpus.documents[row["sha256"]]
            score = self.corpus.pages[(row["sha256"], row["page"])]
            if document["admission_class"] != klass:
                continue
            if document["state"].upper() != state:
                continue
            if bo.quality_band(score) != band:
                continue
            rows.append(row)
        rows.sort(key=lambda row: bo.draw_key(seed, row["id"]))
        self._result = [(row["id"], row["sha256"], row["page"])
                        for row in rows[:limit]]


class FakeBody:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload)


class FakeBedrock:
    """Enough Bedrock to drive the real candidate classes."""

    def __init__(self, fail_after=None):
        self.calls = []
        self.payloads = []
        self.fail_after = fail_after

    def texts_embedded(self, model):
        """How many TEXTS this model was asked to embed, not how many calls."""
        total = 0
        for called, payload in zip(self.calls, self.payloads):
            if called != model:
                continue
            total += len(payload.get("texts", [])) if "texts" in payload else 1
        return total

    def invoke_model(self, modelId, body):
        payload = json.loads(body)
        self.calls.append(modelId)
        self.payloads.append(payload)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("ThrottlingException: slow down")
        if "texts" in payload:
            vectors = [bag_vector(text, 1536) for text in payload["texts"]]
            return {"body": FakeBody({"embeddings": {"float": vectors}})}
        vector = bag_vector(payload["inputText"], payload["dimensions"])
        return {"body": FakeBody({"embedding": vector,
                                  "inputTextTokenCount": 60})}


class FakeArgs:
    """The argparse namespace run_bakeoff() reads, with test defaults."""

    def __init__(self, **overrides):
        self.dsn = "postgresql://test/test"
        self.candidates = "titan,cohere"
        self.sample_size = 12
        self.seed = "test-seed"
        self.min_per_stratum = 1
        self.token_budget = bo.DEFAULT_TOKEN_BUDGET
        self.limit = 10
        self.queries = None
        self.known_items = str(ROOT / "tests" / "fixtures" /
                               "ws13_known_items.json")
        self.run_id = None
        self.price = []
        self.report = None
        self.execute = True
        self.dry_run = False
        for key, value in overrides.items():
            setattr(self, key, value)


def pool_from(corpus, seed, per_stratum=50):
    """Every stratum's candidate rows, the way run_bakeoff assembles them."""
    pools = {}
    for row in corpus.chunks.values():
        document = corpus.documents[row["sha256"]]
        score = corpus.pages[(row["sha256"], row["page"])]
        key = bo.stratum_key(document["admission_class"], document["state"],
                             score)
        pools.setdefault(key, []).append(
            {"chunk_id": row["id"], "sha256": row["sha256"],
             "page": row["page"]})
    for rows in pools.values():
        rows.sort(key=lambda row: bo.draw_key(seed, row["chunk_id"]))
        del rows[per_stratum:]
    return pools


def mandatory_from(corpus, sha=KNOWN_SHA):
    return [{"chunk_id": row["id"], "sha256": row["sha256"], "page": row["page"]}
            for row in sorted(corpus.chunks.values(), key=lambda r: r["id"])
            if row["sha256"] == sha]


class SamplingDeterminismTest(unittest.TestCase):
    """The same seed rebuilds the same sample, and a different one does not."""

    def setUp(self):
        self.corpus = FakeCorpus(documents=30, chunks_per_doc=6)

    def sample_ids(self, seed, size=60):
        pools = pool_from(self.corpus, seed)
        result = bo.build_sample(mandatory_from(self.corpus), pools, size, seed,
                                 floor=1)
        return [row["chunk_id"] for row in result["chunks"]]

    def test_same_seed_reproduces_the_identical_sample(self):
        first = self.sample_ids("seed-alpha")
        second = self.sample_ids("seed-alpha")
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first), "sample must be id-ordered")

    def test_a_different_seed_draws_a_different_sample(self):
        alpha = set(self.sample_ids("seed-alpha"))
        beta = set(self.sample_ids("seed-beta"))
        self.assertNotEqual(alpha, beta)
        # Not disjoint either: mandatory rows are in both by construction, and
        # a claim of "different" that meant "no overlap" would be wrong.
        self.assertTrue(alpha & beta)

    def test_draw_key_matches_its_documented_formula(self):
        # The SQL twin orders by exactly this digest, so a change to either
        # side without the other silently desynchronises the two samples.
        expected = hashlib.sha256(b"seed-alpha:41").hexdigest()
        self.assertEqual(bo.draw_key("seed-alpha", 41), expected)
        self.assertIn("sha256", bo.DRAW_KEY_SQL)
        self.assertIn("|| ':' ||", bo.DRAW_KEY_SQL)

    def test_draw_is_a_prefix_of_the_deterministic_order(self):
        rows = [{"chunk_id": i} for i in range(1, 200)]
        small = [row["chunk_id"] for row in bo.draw(rows, 10, "s")]
        large = [row["chunk_id"] for row in bo.draw(rows, 25, "s")]
        self.assertEqual(large[:10], small,
                         "growing the quota must extend the sample, not "
                         "reshuffle it")

    def test_rule_fingerprint_pins_the_run_id(self):
        rule = bo.sampling_rule("s", 100, [KNOWN_SHA])
        same = bo.sampling_rule("s", 100, [KNOWN_SHA])
        self.assertEqual(bo.run_id_for(rule), bo.run_id_for(same))
        other = bo.sampling_rule("s", 100, [KNOWN_SHA], floor=99)
        self.assertNotEqual(bo.run_id_for(rule), bo.run_id_for(other))


class KnownItemInclusionTest(unittest.TestCase):
    """Every chunk of every known-item document is in the sample, always."""

    def setUp(self):
        self.corpus = FakeCorpus(documents=30, chunks_per_doc=6)
        self.mandatory = mandatory_from(self.corpus)

    def test_mandatory_chunks_are_always_present(self):
        pools = pool_from(self.corpus, "s")
        result = bo.build_sample(self.mandatory, pools, 40, "s", floor=1)
        chosen = {row["chunk_id"] for row in result["chunks"]}
        for row in self.mandatory:
            self.assertIn(row["chunk_id"], chosen)
        self.assertEqual(result["mandatory"], len(self.mandatory))
        self.assertFalse(result["oversubscribed"])

    def test_mandatory_survives_a_sample_smaller_than_itself(self):
        pools = pool_from(self.corpus, "s")
        result = bo.build_sample(self.mandatory, pools, 2, "s", floor=1)
        chosen = {row["chunk_id"] for row in result["chunks"]}
        self.assertTrue({row["chunk_id"] for row in self.mandatory} <= chosen)
        self.assertTrue(result["oversubscribed"],
                        "dropping ground truth to hit a size target must be "
                        "reported, never silent")

    def test_mandatory_is_not_double_counted_in_the_stratified_draw(self):
        pools = pool_from(self.corpus, "s")
        result = bo.build_sample(self.mandatory, pools, 60, "s", floor=1)
        ids = [row["chunk_id"] for row in result["chunks"]]
        self.assertEqual(len(ids), len(set(ids)))
        flagged = {row["chunk_id"] for row in result["chunks"]
                   if row["mandatory"]}
        self.assertEqual(flagged, {row["chunk_id"] for row in self.mandatory})

    def test_fixture_supplies_the_verified_known_item(self):
        items = bo.load_known_items()
        self.assertTrue(items, "the fixture's verified item is the only "
                               "ground truth this harness has")
        self.assertIn(KNOWN_SHA, bo.mandatory_shas(items))
        self.assertEqual(bo.answer_keys(items[0]),
                         {(items[0]["sha256"], items[0]["page"])})

    def test_missing_fixture_is_reported_not_fatal(self):
        items = bo.load_known_items(ROOT / "tests" / "fixtures" / "nope.json")
        self.assertEqual(items, [])


class StratificationTest(unittest.TestCase):
    """Degraded OCR is represented rather than sampled away."""

    def test_quality_bands_cover_the_proxy_range(self):
        self.assertEqual(bo.quality_band(None), "unknown")
        self.assertEqual(bo.quality_band(0.0), "degraded")
        self.assertEqual(bo.quality_band(bo.DEGRADED_SCORE - 0.1), "degraded")
        self.assertEqual(bo.quality_band(bo.DEGRADED_SCORE), "weak")
        self.assertEqual(bo.quality_band(bo.WEAK_SCORE - 0.1), "weak")
        self.assertEqual(bo.quality_band(bo.WEAK_SCORE), "fair")
        self.assertEqual(bo.quality_band(bo.CLEAN_SCORE), "clean")
        self.assertEqual(bo.quality_band(100.0), "clean")
        self.assertEqual(sorted(bo.BAND_SQL), sorted(bo.QUALITY_BANDS))

    def test_unknown_is_not_folded_into_degraded(self):
        # ws13_quality_proxy.score() returns None for a near-blank page. Filing
        # those as "degraded" would pack that stratum with blank pages and
        # leave real OCR noise under-sampled.
        self.assertNotEqual(bo.quality_band(None), bo.quality_band(1.0))

    def test_every_band_and_class_reaches_the_sample(self):
        corpus = FakeCorpus(documents=30, chunks_per_doc=6)
        pools = pool_from(corpus, "s")
        result = bo.build_sample([], pools, 120, "s", floor=1)
        bands, classes = set(), set()
        for row in result["chunks"]:
            klass, _state, band = row["stratum"].split("|", 2)
            bands.add(band)
            classes.add(klass)
        self.assertEqual(bands, set(bo.QUALITY_BANDS))
        self.assertEqual(classes, set(CLASSES))

    def test_allocation_never_exceeds_population_or_sample(self):
        populations = {"a": 100000, "b": 40, "c": 3}
        quota = bo.allocate(populations, 500, floor=25)
        self.assertLessEqual(sum(quota.values()), 500)
        for key, value in quota.items():
            self.assertLessEqual(value, populations[key])
        self.assertEqual(quota["c"], 3, "a tiny stratum contributes all of it")
        self.assertGreaterEqual(quota["b"], 25, "the floor protects breadth")

    def test_floor_cannot_swallow_the_whole_sample(self):
        # 750 strata x a floor of 25 is 18,750 of 20,000 slots; FLOOR_SHARE
        # caps the floor pass so the draw stays proportional in the remainder.
        populations = {f"s{i}": 1000 for i in range(750)}
        quota = bo.allocate(populations, 20000, floor=25)
        self.assertEqual(sum(quota.values()), 20000)
        self.assertLessEqual(max(quota.values()), 25 + 1 + 20000 // 750)

    def test_allocation_fills_everything_it_can(self):
        populations = {"a": 5, "b": 6}
        self.assertEqual(sum(bo.allocate(populations, 50, floor=2).values()), 11)
        self.assertEqual(sum(bo.allocate(populations, 4, floor=2).values()), 4)

    def test_pool_sql_selects_the_band_it_was_asked_for(self):
        sql = bo.pool_sql("originals|ID|degraded")
        self.assertIn(bo.BAND_SQL["degraded"], sql)
        self.assertIn(bo.DRAW_KEY_SQL, sql)
        with self.assertRaises(bo.BakeoffError):
            bo.pool_sql("originals|ID|pristine")

    def test_reported_population_is_the_corpus_count_not_the_fetched_pool(self):
        # The pool is fetched capped at the stratum's quota, so deriving the
        # population from len(pool) would report every stratum's size as its
        # own quota and a reader could not tell 200,000 chunks from 25.
        pools = {"originals|ID|clean": [{"chunk_id": i, "sha256": "a",
                                         "page": 1} for i in range(1, 6)]}
        result = bo.build_sample([], pools, 5, "s", floor=1,
                                 quota={"originals|ID|clean": 3},
                                 populations={"originals|ID|clean": 200000})
        entry = result["strata"][0]
        self.assertEqual(entry["population"], 200000)
        self.assertEqual(entry["quota"], 3)
        self.assertEqual(entry["drawn"], 3)
        self.assertEqual(result["total"], 3)

    def test_a_caller_supplied_quota_is_honoured_exactly(self):
        pools = {"a|ID|clean": [{"chunk_id": i, "sha256": "a", "page": 1}
                                for i in range(1, 51)],
                 "b|MT|degraded": [{"chunk_id": i, "sha256": "b", "page": 1}
                                   for i in range(51, 101)]}
        result = bo.build_sample([], pools, 30, "s", floor=1,
                                 quota={"a|ID|clean": 10, "b|MT|degraded": 20})
        drawn = {}
        for row in result["chunks"]:
            drawn[row["stratum"]] = drawn.get(row["stratum"], 0) + 1
        self.assertEqual(drawn, {"a|ID|clean": 10, "b|MT|degraded": 20})

    def test_sampling_rule_records_what_it_would_take_to_re_run(self):
        rule = bo.sampling_rule("seed-x", 20000, [KNOWN_SHA])
        self.assertEqual(rule["seed"], "seed-x")
        self.assertEqual(rule["mandatory"]["sha256"], [KNOWN_SHA])
        self.assertEqual(rule["draw"]["sql"], bo.DRAW_KEY_SQL)
        self.assertEqual(sorted(rule["strata"]["quality_bands"]),
                         sorted(bo.QUALITY_BANDS))
        self.assertIn("floor_share", rule)


class ProductionWriteRefusalTest(unittest.TestCase):
    """The production embedding columns are never written. Not once."""

    def test_the_backfill_updates_are_refused_verbatim(self):
        # These are the exact statements pipelines/ws13_embed_backfill.py and
        # pipelines/ws13_qwen_overlay.py issue.
        for column in bo.PRODUCTION_VECTOR_COLUMNS:
            statement = (f"UPDATE ws13_chunks SET {column}=%s WHERE id=%s")
            with self.assertRaises(bo.BakeoffSafetyError):
                bo.assert_write_allowed(statement)

    def test_other_production_mutations_are_refused(self):
        for statement in (
            "INSERT INTO ws13_chunks (sha256, page, text) VALUES (%s, %s, %s)",
            "DELETE FROM ws13_documents WHERE sha256 = %s",
            "TRUNCATE TABLE ws13_pages",
            "ALTER TABLE ws13_chunks ADD COLUMN bakeoff_vec vector(1536)",
            "CREATE INDEX ws13_chunks_bakeoff ON ws13_chunks (id)",
            "DROP TABLE ws13_chunks",
            "INSERT INTO ws13_embed_skips (chunk_id, model) VALUES (%s, %s)",
            "UPDATE ws13_manifest SET status='done'",
        ):
            with self.assertRaises(bo.BakeoffSafetyError, msg=statement):
                bo.assert_write_allowed(statement)

    def test_bakeoff_writes_are_allowed(self):
        for statement in bo.schema_statements():
            bo.assert_write_allowed(statement)
        # The Titan copy reads the production column and writes the bake-off
        # table. A guard keyed on any mention of ws13_chunks would refuse the
        # one candidate that costs nothing.
        self.assertEqual(bo.write_target(bo.TITAN_COPY_SQL),
                         "ws13_bakeoff_vectors")
        bo.assert_write_allowed(bo.TITAN_COPY_SQL)

    def test_reads_are_not_treated_as_writes(self):
        for statement in (bo.PENDING_SQL, bo.SAMPLE_SQL, bo.COVERAGE_SQL,
                          bo.VECTOR_RANK_SQL, bo.STRATUM_COUNT_SQL,
                          bo.MANDATORY_SQL, bo.lexical_rank_sql()):
            self.assertFalse(bo.is_mutating(statement))
            bo.assert_write_allowed(statement)

    def test_an_unparseable_mutation_is_refused_rather_than_waved_through(self):
        with self.assertRaises(bo.BakeoffSafetyError):
            bo.assert_write_allowed("INSERT INTO")

    def test_the_module_contains_no_production_write(self):
        # A source-level check as well as a runtime one: the runtime guard only
        # fires on statements that actually execute, and a write added behind
        # an untaken branch would never reach it.
        for column in bo.PRODUCTION_VECTOR_COLUMNS:
            self.assertNotRegex(
                MODULE_SOURCE, rf"(?i)update\s+ws13_chunks\s+set\s+{column}")
        self.assertNotRegex(MODULE_SOURCE, r"(?i)insert\s+into\s+ws13_chunks")
        self.assertNotRegex(MODULE_SOURCE, r"(?i)delete\s+from\s+ws13_")


class ShippingRankerTest(unittest.TestCase):
    """The fusion is the product's, imported, not a lookalike."""

    def test_rrf_is_defined_in_the_query_lambda(self):
        self.assertEqual(Path(bo.rrf_fuse.__code__.co_filename).resolve(),
                         Path(ql.__file__).resolve())
        self.assertEqual(bo.RRF_K, ql.RRF_K)

    def test_the_module_does_not_define_its_own_fusion(self):
        self.assertNotRegex(MODULE_SOURCE, r"(?m)^def rrf_fuse")
        self.assertNotRegex(MODULE_SOURCE, r"1\.0\s*/\s*\(\s*k\s*\+")

    def test_lexical_contract_holds_against_the_shipping_builder(self):
        bo.assert_lexical_contract(ql)
        self.assertIn(bo.LEXICAL_MATCH_SQL, bo.lexical_rank_sql())
        self.assertIn(bo.LEXICAL_RANK_SQL, bo.lexical_rank_sql())

    def test_lexical_contract_fails_when_the_product_ranker_moves(self):
        drifted = types.SimpleNamespace(
            lexical_sql=lambda filters, query, n: ("SELECT c.id FROM x", []))
        with self.assertRaises(bo.BakeoffError):
            bo.assert_lexical_contract(drifted)

    def test_fuse_prefers_a_row_both_arms_found(self):
        fused = bo.fuse([10, 20, 30], [30, 40, 50], 5)
        self.assertEqual(fused[0], 30)
        # A row missing from an arm contributes nothing rather than a penalty:
        # the defect the 0.75/0.25 blend had, which RRF replaced.
        self.assertIn(40, fused)


class ScoringMathTest(unittest.TestCase):
    """recall@k and MRR on hand-built rankings."""

    def setUp(self):
        # chunk 7 is page 3 of the answer document; 8 is the same page, a
        # different chunk, and must count as a hit too.
        self.page_of = {1: ("aa", 1), 2: ("aa", 2), 7: ("bb", 3), 8: ("bb", 3),
                        9: ("cc", 1)}
        self.answers = {("bb", 3)}

    def test_reciprocal_rank_and_hits_at_each_k(self):
        scored = bo.query_scores([1, 2, 7, 9], self.answers, self.page_of)
        self.assertAlmostEqual(scored["reciprocal_rank"], 1.0 / 3)
        self.assertEqual(scored["hits"], {1: 0, 5: 1, 10: 1})

    def test_a_first_place_hit_scores_one(self):
        scored = bo.query_scores([7, 1, 2], self.answers, self.page_of)
        self.assertEqual(scored["reciprocal_rank"], 1.0)
        self.assertEqual(scored["hits"], {1: 1, 5: 1, 10: 1})

    def test_a_miss_scores_zero_everywhere(self):
        scored = bo.query_scores([1, 2, 9], self.answers, self.page_of)
        self.assertEqual(scored["reciprocal_rank"], 0.0)
        self.assertEqual(scored["hits"], {1: 0, 5: 0, 10: 0})

    def test_any_chunk_of_the_right_page_counts(self):
        scored = bo.query_scores([8], self.answers, self.page_of)
        self.assertEqual(scored["hits"][1], 1)

    def test_a_hit_past_k_does_not_count_at_k(self):
        ranking = [1, 2, 9, 1, 2, 9, 1, 2, 9, 1, 7]
        scored = bo.query_scores(ranking, self.answers, self.page_of)
        self.assertEqual(scored["hits"], {1: 0, 5: 0, 10: 0})
        self.assertAlmostEqual(scored["reciprocal_rank"], 1.0 / 11)

    def test_aggregate_recall_and_mrr(self):
        per_query = [
            bo.query_scores([7, 1, 2], self.answers, self.page_of),
            bo.query_scores([1, 2, 7], self.answers, self.page_of),
            bo.query_scores([1, 2, 9], self.answers, self.page_of),
            bo.query_scores([1, 2, 9, 1, 2, 9, 1, 2, 9, 1, 7], self.answers,
                            self.page_of),
        ]
        aggregate = bo.aggregate_scores(per_query)
        self.assertEqual(aggregate["labelled_queries"], 4)
        self.assertEqual(aggregate["recall@1"], 0.25)
        self.assertEqual(aggregate["recall@5"], 0.5)
        self.assertEqual(aggregate["recall@10"], 0.5)
        expected = (1.0 + 1.0 / 3 + 0.0 + 1.0 / 11) / 4
        self.assertAlmostEqual(aggregate["mrr"], round(expected, 4), places=4)

    def test_no_labelled_queries_yields_nulls_not_zeros(self):
        aggregate = bo.aggregate_scores([])
        self.assertEqual(aggregate["labelled_queries"], 0)
        self.assertIsNone(aggregate["recall@5"])
        self.assertIsNone(aggregate["mrr"])

    def test_rank_agreement_measures_disagreement(self):
        identical = bo.rank_agreement([1, 2, 3, 4], [1, 2, 3, 4], k=4)
        self.assertEqual(identical["overlap_at_k"], 1.0)
        self.assertEqual(identical["mean_displacement"], 0.0)
        disjoint = bo.rank_agreement([1, 2], [3, 4], k=2)
        self.assertEqual(disjoint["overlap_at_k"], 0.0)
        self.assertIsNone(disjoint["mean_displacement"])
        shuffled = bo.rank_agreement([1, 2, 3, 4], [4, 3, 2, 1], k=4)
        self.assertEqual(shuffled["overlap_at_k"], 1.0)
        self.assertEqual(shuffled["mean_displacement"], 2.0)


class VerdictTest(unittest.TestCase):
    """The bake-off refuses to name a winner unless the numbers support one."""

    @staticmethod
    def hits(pattern):
        return [int(value) for value in pattern]

    def test_too_few_labelled_queries_is_insufficient_evidence(self):
        # Today's reality: the fixture holds 1 verified item of a target 25.
        baseline = self.hits("1")
        candidate = self.hits("1")
        delta = bo.paired_delta(baseline, candidate)
        result = bo.verdict({"cohere": delta}, labelled_queries=1)
        self.assertEqual(result["verdict"], "insufficient-evidence")
        self.assertIsNone(result["winner"])
        self.assertIn("ws13_known_items.json", result["reason"])

    def test_a_difference_inside_noise_names_no_winner(self):
        baseline = self.hits("1" * 12 + "0" * 12)
        candidate = self.hits("1" * 13 + "0" * 11)
        delta = bo.paired_delta(baseline, candidate)
        self.assertFalse(delta["significant"])
        self.assertLessEqual(delta["ci95"][0], 0.0)
        self.assertGreaterEqual(delta["ci95"][1], 0.0)
        result = bo.verdict({"cohere": delta}, labelled_queries=24)
        self.assertEqual(result["verdict"], "no-clear-winner")
        self.assertIsNone(result["winner"])
        self.assertIn("spanning zero", result["reason"])
        self.assertIn("costs nothing more", result["reason"])

    def test_an_identical_candidate_is_never_a_winner(self):
        hits = self.hits("101101101101101101101101")
        delta = bo.paired_delta(hits, hits)
        result = bo.verdict({"cohere": delta}, labelled_queries=len(hits))
        self.assertEqual(result["verdict"], "no-clear-winner")
        self.assertEqual(delta["delta"], 0.0)

    def test_a_real_and_consistent_gain_does_name_a_winner(self):
        # Every one of 24 queries improves: unanimous, so the interval cannot
        # span zero and refusing here would make the harness useless.
        baseline = self.hits("0" * 24)
        candidate = self.hits("1" * 24)
        delta = bo.paired_delta(baseline, candidate)
        result = bo.verdict({"cohere": delta}, labelled_queries=24)
        self.assertEqual(result["verdict"], "candidate-wins")
        self.assertEqual(result["winner"], "cohere")
        self.assertIn("full-fill cost", result["reason"])

    def test_a_candidate_that_loses_is_not_promoted_to_winner(self):
        baseline = self.hits("1" * 24)
        candidate = self.hits("0" * 24)
        delta = bo.paired_delta(baseline, candidate)
        result = bo.verdict({"cohere": delta}, labelled_queries=24)
        self.assertEqual(result["verdict"], "no-clear-winner")

    def test_partial_coverage_excludes_a_candidate_from_the_verdict(self):
        baseline = self.hits("0" * 24)
        candidate = self.hits("1" * 24)
        delta = bo.paired_delta(baseline, candidate)
        result = bo.verdict({"cohere": delta}, labelled_queries=24,
                            incomplete=["cohere"])
        self.assertEqual(result["verdict"], "insufficient-evidence")
        self.assertIsNone(result["winner"])
        self.assertEqual(result["excluded_incomplete"], ["cohere"])
        self.assertIn("partial coverage", result["reason"])

    def test_the_baseline_is_never_listed_as_an_excluded_candidate(self):
        # titan is what everything is measured against, not a competitor that
        # can drop out of the comparison.
        result = bo.verdict({}, labelled_queries=0,
                            incomplete=["titan", "cohere"])
        self.assertEqual(result["excluded_incomplete"], ["cohere"])
        self.assertNotIn("titan,", result["reason"])

    def test_the_better_of_two_significant_candidates_wins(self):
        baseline = self.hits("0" * 24)
        small = self.hits("1" * 12 + "0" * 12)
        large = self.hits("1" * 24)
        deltas = {"cohere": bo.paired_delta(baseline, small),
                  "qwen": bo.paired_delta(baseline, large)}
        result = bo.verdict(deltas, labelled_queries=24)
        self.assertEqual(result["winner"], "qwen")

    def test_comparison_pairs_by_query_id_not_by_position(self):
        # The baseline answers four questions; the candidate's embedding failed
        # on q2, so its list is one shorter. Zipping by position would compare
        # the candidate's q3 against the baseline's q2 and report a spurious
        # delta of -0.33 from data that is in fact identical wherever both ran.
        def rows(pairs):
            return [{"query_id": qid, "hits": {5: hit}} for qid, hit in pairs]

        results = {
            "titan": {"per_query": rows([("q1", 1), ("q2", 0), ("q3", 1),
                                         ("q4", 0)])},
            "cohere": {"per_query": rows([("q1", 1), ("q3", 1), ("q4", 0)])},
        }
        deltas, labelled = bo.compare_to_baseline(results)
        self.assertEqual(labelled, 3)
        self.assertEqual(deltas["cohere"]["n"], 3)
        self.assertEqual(deltas["cohere"]["delta"], 0.0,
                         "identical answers must produce a zero delta")

    def test_comparison_counts_the_queries_every_candidate_shares(self):
        def rows(pairs):
            return [{"query_id": qid, "hits": {5: hit}} for qid, hit in pairs]

        results = {
            "titan": {"per_query": rows([("q1", 1), ("q2", 1), ("q3", 1)])},
            "cohere": {"per_query": rows([("q1", 0), ("q2", 0), ("q3", 0)])},
            "qwen": {"per_query": rows([("q1", 0)])},
        }
        deltas, labelled = bo.compare_to_baseline(results)
        self.assertEqual(labelled, 1, "the weakest comparison bounds the claim")
        self.assertEqual(deltas["cohere"]["n"], 3)
        self.assertEqual(deltas["qwen"]["n"], 1)
        self.assertEqual(deltas["cohere"]["delta"], -1.0)

    def test_paired_delta_reports_nothing_when_there_is_nothing(self):
        empty = bo.paired_delta([], [])
        self.assertEqual(empty["n"], 0)
        self.assertFalse(empty["significant"])
        self.assertIsNone(empty["delta"])

    def test_a_single_query_can_never_be_significant(self):
        delta = bo.paired_delta([0], [1])
        self.assertEqual(delta["delta"], 1.0)
        self.assertFalse(delta["significant"],
                         "one query is an anecdote, not a measurement")


class TokenBudgetTest(unittest.TestCase):
    """The experiment cannot become the runaway spend it exists to prevent."""

    def test_charging_past_the_ceiling_raises_and_spends_nothing(self):
        budget = bo.TokenBudget(1000)
        budget.charge(600)
        self.assertEqual(budget.spent, 600)
        with self.assertRaises(bo.BudgetExhausted):
            budget.charge(500)
        self.assertEqual(budget.spent, 600, "a refused charge costs nothing")
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.remaining(), 400)

    def test_a_charge_that_exactly_fills_the_budget_is_allowed(self):
        budget = bo.TokenBudget(100)
        budget.charge(100)
        self.assertEqual(budget.remaining(), 0)
        self.assertFalse(budget.exhausted)
        with self.assertRaises(bo.BudgetExhausted):
            budget.charge(1)

    def test_can_afford_agrees_with_charge(self):
        budget = bo.TokenBudget(50)
        self.assertTrue(budget.can_afford(50))
        self.assertFalse(budget.can_afford(51))

    def test_estimate_matches_the_backfill_estimator(self):
        self.assertEqual(bo.estimate_tokens("x" * 3000), 1000)
        self.assertEqual(bo.estimate_tokens(""), 1)

    def test_cost_separates_per_token_from_per_gpu_hour(self):
        self.assertAlmostEqual(
            bo.cost_usd("cohere", 1_000_000, 0.0), bo.PRICE_PER_MTOK["cohere"])
        # Qwen's tokens are free at the margin; the node is not.
        self.assertAlmostEqual(bo.cost_usd("qwen", 5_000_000, 3600.0),
                               bo.GPU_HOURLY_USD["qwen"], places=4)
        self.assertEqual(bo.cost_usd("titan", 0, 0.0), 0.0)

    def test_full_fill_projection_states_the_decision_in_days_of_cap(self):
        projection = bo.project_full_fill("cohere", tokens=20_000_000,
                                          chunks=20_000, seconds=600.0)
        self.assertEqual(projection["chunks_outstanding"],
                         bo.COHERE_NULL_CHUNKS)
        self.assertGreater(projection["days_of_cohere_daily_cap"], 30)
        self.assertGreater(projection["usd"], 0.0)


class EndToEndBakeoffTest(unittest.TestCase):
    """A whole run over a fake corpus, through the real code paths."""

    def setUp(self):
        self.corpus = FakeCorpus(documents=6, chunks_per_doc=4)
        self.conn = FakeConn(self.corpus)
        self.bedrock = FakeBedrock()
        self.clients = {"titan": bo.TitanCandidate(self.bedrock),
                        "cohere": bo.CohereCandidate(self.bedrock)}
        self.before = self.corpus.production_snapshot()

    def run_once(self, **overrides):
        args = FakeArgs(**overrides)
        return bo.run_bakeoff(self.conn, args, self.clients)

    def test_a_full_run_never_touches_a_production_table(self):
        self.run_once()
        self.assertEqual(self.corpus.production_snapshot(), self.before,
                         "the production embedding columns must be byte-"
                         "identical after a bake-off")
        targets = {target for target, _sql in self.corpus.mutations}
        self.assertTrue(targets)
        self.assertTrue(targets <= set(bo.BAKEOFF_TABLES), targets)

    def test_the_report_is_decision_shaped(self):
        report = self.run_once()
        self.assertEqual(report["fusion"]["rrf_k"], ql.RRF_K)
        self.assertEqual(report["candidates"], ["titan", "cohere"])
        self.assertIn("sampling_rule", report)
        self.assertIn("verdict", report)
        self.assertIn("agreement", report)
        for name in ("titan", "cohere"):
            result = report["results"][name]
            self.assertEqual(result["dims"], bo.CANDIDATE_DIMS[name])
            self.assertGreater(result["sample_covered"], 0)
            self.assertIn("usd", result)
            self.assertIn("full_fill_projection", result)
            self.assertIn("recall@5", result["metrics"])
        # Titan is the baseline precisely because its column already exists.
        self.assertEqual(report["results"]["titan"]["sample_tokens"], 0)
        self.assertGreater(report["results"]["cohere"]["sample_tokens"], 0)
        self.assertTrue(json.dumps(report, default=str))

    def test_one_labelled_query_cannot_produce_a_winner(self):
        report = self.run_once()
        self.assertEqual(report["verdict"]["verdict"], "insufficient-evidence")
        self.assertIsNone(report["verdict"]["winner"])

    def test_titan_is_copied_and_never_re_embedded(self):
        report = self.run_once()
        sample_size = report["sample"]["total"]
        labelled = len(bo.load_known_items())
        # Titan is invoked once per QUERY and never once per sample chunk:
        # titan_embedding is already complete, so re-embedding it would spend
        # quota to reproduce vectors the account has already bought.
        self.assertEqual(self.bedrock.texts_embedded(bo.TITAN_MODEL), labelled)
        self.assertLess(self.bedrock.texts_embedded(bo.TITAN_MODEL),
                        sample_size)
        self.assertEqual(self.bedrock.texts_embedded(bo.COHERE_MODEL),
                         sample_size + labelled)
        titan_rows = [key for key in self.corpus.vectors if key[1] == "titan"]
        self.assertEqual(len(titan_rows), sample_size)
        for key in titan_rows:
            self.assertEqual(self.corpus.vectors[key]["tokens"], 0)
            self.assertEqual(self.corpus.vectors[key]["vec"],
                             self.corpus.chunks[key[2]]["titan_embedding"],
                             "the copied vector must be the production one")

    def test_a_second_run_resumes_and_re_embeds_nothing(self):
        first = self.run_once()
        cohere_calls = sum(1 for call in self.bedrock.calls
                           if call == bo.COHERE_MODEL)
        second = self.run_once()
        self.assertEqual(second["run_id"], first["run_id"])
        after = sum(1 for call in self.bedrock.calls if call == bo.COHERE_MODEL)
        # Only the per-query embeddings repeat; the sample is already filled.
        self.assertEqual(second["results"]["cohere"]["sample_tokens"],
                         first["results"]["cohere"]["sample_tokens"])
        self.assertLess(after - cohere_calls, cohere_calls)

    def test_resuming_a_run_drawn_under_another_rule_is_refused(self):
        first = self.run_once(run_id="pinned", seed="seed-one")
        self.assertEqual(first["run_id"], "pinned")
        # Same id, different seed: the draw would differ, and persist_sample's
        # ON CONFLICT DO NOTHING would ADD the new rows to the old sample
        # rather than replace them.
        with self.assertRaises(bo.BakeoffError):
            self.run_once(run_id="pinned", seed="seed-two")
        # Same id, same rule: an ordinary resume, which must still work.
        self.run_once(run_id="pinned", seed="seed-one")

    def test_a_dry_run_writes_nothing_and_still_prices_the_work(self):
        report = self.run_once(execute=False, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.corpus.mutations, [])
        self.assertEqual(self.corpus.vectors, {})
        self.assertGreater(report["sample"]["total"], 0)
        self.assertIn("projection", report)
        self.assertEqual(report["verdict"]["verdict"], "insufficient-evidence")

    def test_the_token_budget_stops_a_candidate_and_excludes_it(self):
        report = self.run_once(token_budget=1)
        cohere = report["results"]["cohere"]
        self.assertTrue(cohere["budget_exhausted"])
        self.assertEqual(cohere["sample_covered"], 0)
        self.assertFalse(cohere["complete"])
        self.assertIn("cohere", report["verdict"]["excluded_incomplete"])
        self.assertEqual(report["verdict"]["verdict"], "insufficient-evidence")
        # And nothing was written for the candidate that ran out.
        self.assertFalse([key for key in self.corpus.vectors
                          if key[1] == "cohere"])

    def test_the_sample_is_persisted_so_the_run_can_be_re_read(self):
        report = self.run_once()
        run_id = report["run_id"]
        self.assertIn(run_id, self.corpus.runs)
        self.assertEqual(self.corpus.runs[run_id]["rule"],
                         report["sampling_rule"])
        stored = {cid for (rid, cid) in self.corpus.sample if rid == run_id}
        self.assertEqual(len(stored), report["sample"]["total"])

    def test_unlabelled_queries_produce_rank_agreement(self):
        path = ROOT / "tests" / "fixtures" / "ws13_known_items.json"
        self.assertTrue(path.exists())
        with mock.patch.object(bo, "load_queries_file",
                               return_value=["gold quartz vein assay",
                                             "butte county lava creek"]):
            report = self.run_once()
        agreement = report["agreement"]
        self.assertEqual(len(agreement), 1)
        self.assertEqual({agreement[0]["a"], agreement[0]["b"]},
                         {"titan", "cohere"})
        self.assertEqual(agreement[0]["queries"], 2)
        self.assertIsNotNone(agreement[0]["mean_overlap_at_k"])

    def test_a_misaligned_cohere_batch_is_refused(self):
        class Misaligned(FakeBedrock):
            def invoke_model(self, modelId, body):
                result = super().invoke_model(modelId, body)
                payload = json.loads(body)
                if "texts" in payload and len(payload["texts"]) > 1:
                    data = json.loads(result["body"].read())
                    data["embeddings"]["float"].pop()
                    return {"body": FakeBody(data)}
                return result

        self.clients["cohere"] = bo.CohereCandidate(Misaligned())
        report = self.run_once()
        cohere = report["results"]["cohere"]
        self.assertGreater(cohere["failed_batches"], 0)
        self.assertFalse(cohere["complete"])
        self.assertIn("cohere", report["verdict"]["excluded_incomplete"])


class CandidateSelectionTest(unittest.TestCase):
    """Candidate parsing, and the baseline that cannot be dropped."""

    def test_titan_is_always_in_the_run(self):
        self.assertEqual(bo.parse_candidates("cohere"), ["titan", "cohere"])
        self.assertEqual(bo.parse_candidates("qwen,cohere"),
                         ["titan", "cohere", "qwen"])

    def test_order_is_canonical_regardless_of_input(self):
        self.assertEqual(bo.parse_candidates("qwen,titan,cohere"),
                         list(bo.CANDIDATES))

    def test_an_unknown_candidate_is_refused(self):
        with self.assertRaises(bo.BakeoffError):
            bo.parse_candidates("titan,voyager")

    def test_default_excludes_qwen_because_no_gpu_has_ever_run_here(self):
        self.assertEqual(bo.DEFAULT_CANDIDATES, ("titan", "cohere"))
        self.assertIn("qwen", bo.CANDIDATES)

    def test_prices_can_be_overridden_and_are_stamped(self):
        prices = bo.parse_prices(["cohere=0.5"])
        self.assertEqual(prices["cohere"], 0.5)
        self.assertEqual(prices["titan"], bo.PRICE_PER_MTOK["titan"])
        with self.assertRaises(bo.BakeoffError):
            bo.parse_prices(["cohere"])

    def test_titan_refuses_to_re_embed_the_sample(self):
        candidate = bo.TitanCandidate(FakeBedrock())
        self.assertFalse(candidate.embeds_sample)
        with self.assertRaises(bo.BakeoffError):
            candidate.embed_documents(["anything"])

    def test_cohere_uses_search_query_for_questions(self):
        bedrock = FakeBedrock()
        recorded = []
        original = bedrock.invoke_model

        def spy(modelId, body):
            recorded.append(json.loads(body).get("input_type"))
            return original(modelId, body)

        bedrock.invoke_model = spy
        candidate = bo.CohereCandidate(bedrock)
        candidate.embed_documents(["a chunk of page text"])
        candidate.embed_query("a question")
        self.assertEqual(recorded, ["search_document", "search_query"])

    def test_qwen_sanitises_a_poisoned_vector_instead_of_crashing(self):
        poisoned = [[float("nan")] * bo.CANDIDATE_DIMS["qwen"],
                    [1.0] + [0.0] * (bo.CANDIDATE_DIMS["qwen"] - 1)]
        candidate = bo.QwenCandidate(endpoint="http://unused",
                                     opener=lambda payload: poisoned)
        vectors, tokens = candidate.embed_documents(["a", "b"])
        self.assertIsNone(vectors[0], "a fully poisoned vector is unusable")
        self.assertAlmostEqual(sum(vectors[1]), 1.0)
        self.assertEqual(len(tokens), 2)

    def test_vector_literal_is_parametric_not_pinned_to_titan(self):
        # ws13_query_lambda.vector_literal hard-requires 1024 floats because
        # Titan is the production vector; reusing it would reject every Cohere
        # and Qwen vector the bake-off exists to compare.
        with self.assertRaises(ValueError):
            ql.vector_literal([0.0] * 1536)
        literal = bo.vector_literal([0.0] * 1535 + [1.0], 1536)
        self.assertEqual(len(json.loads(literal)), 1536)
        with self.assertRaises(bo.BakeoffError):
            bo.vector_literal([0.0] * 1536, 1536)
        with self.assertRaises(bo.BakeoffError):
            bo.vector_literal([1.0] * 10, 1536)

    def test_a_wrong_width_vector_is_refused_at_write_time(self):
        corpus = FakeCorpus(documents=1, chunks_per_doc=1)
        conn = FakeConn(corpus)
        with self.assertRaises(bo.BakeoffError):
            bo.store_vectors(conn, "run", "cohere", [(1, [0.1, 0.2], 5)])


class OfflineProjectionTest(unittest.TestCase):
    """--dry-run has to be useful with no database, and honest about it."""

    def test_projection_names_the_days_of_cap_the_full_fill_would_cost(self):
        projection = bo.offline_projection(20000, ["titan", "cohere"],
                                           bo.PRICE_PER_MTOK)
        self.assertEqual(projection["candidates"]["titan"]["sample_tokens"], 0)
        cohere = projection["candidates"]["cohere"]
        self.assertEqual(cohere["sample_tokens"],
                         20000 * bo.MEAN_TOKENS_PER_CHUNK)
        self.assertGreater(cohere["full_fill_days_of_cap"], 30)
        self.assertAlmostEqual(projection["sample_fraction_of_corpus"],
                               round(20000 / bo.CORPUS_CHUNKS, 5))

    def test_qwen_is_labelled_as_a_gpu_cost_not_a_free_one(self):
        projection = bo.offline_projection(100, ["titan", "qwen"],
                                           bo.PRICE_PER_MTOK)
        self.assertIn("GPU hour", projection["candidates"]["qwen"]["note"])

    def test_queries_file_accepts_json_and_plain_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            plain = Path(folder) / "q.txt"
            plain.write_text("first query\n\nsecond query\n", encoding="utf-8")
            self.assertEqual(bo.load_queries_file(plain),
                             ["first query", "second query"])
            structured = Path(folder) / "q.json"
            structured.write_text(json.dumps({"queries": [{"text": "a"}]}),
                                  encoding="utf-8")
            self.assertEqual(bo.load_queries_file(structured), ["a"])
        self.assertEqual(bo.load_queries_file(None), [])

    def test_build_queries_labels_only_what_has_an_answer(self):
        items = bo.load_known_items()
        queries = bo.build_queries(items, ["free text probe", "  "])
        labelled = [query for query in queries if query["answers"]]
        unlabelled = [query for query in queries if not query["answers"]]
        self.assertEqual(len(labelled), len(items))
        self.assertEqual(len(unlabelled), 1)


if __name__ == "__main__":
    unittest.main()
