#!/usr/bin/env python3
"""WS13 embedding bake-off: measure a candidate vector before buying one.

The decision this exists to settle. pipelines/ws13_embed_backfill.py has been
trickling Cohere v4 into ws13_chunks.embedding for weeks against a
non-adjustable 16.2M tokens/day account cap. 584,449 chunks are still NULL, so
at the same estimator the backfill bills against (len(text)//3, about 1,000
tokens for a 3,000-character chunk) finishing that column costs roughly 584M
tokens -- about 36 more days of the entire daily allowance. Nothing reads the
column: infra/ws13_query_lambda.py reads titan_embedding and only
titan_embedding (HALFVEC_EXPR, EXACT_DISTANCE_SQL), which is the complete
column (0 NULL over 852,027 chunks). qwen_embedding is worse -- 527,646 NULL
and never written once, because it needs a GPU node and no GPU instance has
ever run in this account (quota is 8 vCPU of On-Demand G/VT, 0 for P and 0 for
all GPU spot, so exactly one g5.2xlarge is possible).

That spend is committed to a hypothesis -- that Cohere or Qwen beats Titan on
OCR-noisy mining documents -- which has never been measured. This harness
measures it on about 2.3% of the corpus instead: embed a defensible SAMPLE
with each candidate, score every candidate on the same queries with the same
fusion the product ships, and commit to a full fill only if one demonstrably
wins.

Four rules hold the experiment to that shape, each answering a way a bake-off
usually fails:

  1. The sample is reproducible, and it does not avoid the hard half of the
     corpus. It contains every chunk of every document named in
     tests/fixtures/ws13_known_items.json, then a stratified draw across
     admission_class x state x lexical-quality band, with a per-stratum floor
     so degraded OCR is represented rather than sampled away. Membership is
     decided by sha256(seed:chunk_id) -- the same expression in Python and in
     SQL -- so anyone with the seed and the corpus rebuilds the identical
     sample. A sample nobody can reproduce proves nothing.
  2. Vectors are written to ws13_bakeoff_vectors, never to ws13_chunks. The
     point is a cheap reversible experiment; 20,000 rows landed in the
     production embedding column would make "we tried it" indistinguishable
     from "we committed to it", and the way back would be an UPDATE against
     the live table. assert_write_allowed() enforces the target of every
     mutating statement this module issues.
  3. Scoring runs the shipping ranker, not a lookalike. rrf_fuse and RRF_K are
     imported from infra/ws13_query_lambda.py through
     pipelines/ws13_index_contract.query_module(), and the lexical arm's match
     and rank expressions are asserted to appear verbatim in that module's
     lexical_sql() output at import, so a drift in the product's ranker breaks
     this harness loudly instead of quietly measuring something else.
  4. The verdict may refuse. The known-item fixture holds 1 verified item of a
     target 25, and a difference measured over a handful of queries is noise.
     verdict() returns 'insufficient-evidence' below MIN_LABELLED_QUERIES and
     'no-clear-winner' when the paired 95% interval spans zero. A bake-off
     that always names a winner is worse than none.

The budget is the fifth rule, and it is why --dry-run is the default: an
experiment run to prevent a runaway spend must not become one. Each candidate
gets a hard token ceiling, charged on success only (charging before the call
burns quota that was never spent at the service -- the lesson
ws13_embed_backfill.cohere_worker learned against a binding daily cap), and a
candidate that hits its ceiling is reported with partial coverage and excluded
from the verdict rather than compared on half a sample.

Usage:
    # plan only: no database writes, no Bedrock calls, no GPU
    ws13_vector_bakeoff.py --dsn "$WS13_DB_DSN" --report var/bakeoff.json

    # spend, resumably, against the same seed
    ws13_vector_bakeoff.py --dsn "$WS13_DB_DSN" --execute \\
        --candidates titan,cohere --sample-size 20000 --seed 13

Env:
  WS13_DB_DSN        libpq URI (--dsn overrides). Reads ws13_chunks /
                     ws13_documents / ws13_pages; writes only ws13_bakeoff_*.
  WS13_TEI_URL       Qwen TEI endpoint, same contract as
                     pipelines/ws13_qwen_overlay.py.
  AWS_DEFAULT_REGION Bedrock region for the titan and cohere candidates.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.request

try:
    import psycopg
except Exception:  # pragma: no cover - driver lives on the fleet, not on CI
    # Module level so it can be substituted, never required to import: the
    # sampling, the scoring maths and the verdict must stay testable on a host
    # with no driver, no database and no AWS.
    psycopg = None

try:
    import boto3
except Exception:  # pragma: no cover - same reason as psycopg
    boto3 = None

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ws13_index_contract as contract  # noqa: E402  (needs HERE on sys.path)

# The shipping ranker, imported rather than retyped. query_module() executes
# infra/ws13_query_lambda.py from an exact path and works in both layouts that
# exist: the repository checkout and the flat /opt/ws13 fleet bundle.
QUERY_LAMBDA = contract.QUERY_LAMBDA
_QL = contract.query_module(QUERY_LAMBDA)
rrf_fuse = _QL.rrf_fuse
RRF_K = _QL.RRF_K
ARMS = ("lexical", "vector")

# The lexical arm here ranges over the ~20,000-row sample, not over all
# 852,027 chunks, so it cannot be ws13_query_lambda.lexical_sql() verbatim --
# that builder has no chunk-id predicate to bound it with, and a vector arm
# over 20,000 rows fused with a lexical arm over 852,027 would not be a
# comparison of anything. What CAN be held identical is the ranking itself,
# so these two fragments are asserted to appear byte-for-byte in the shipping
# statement at import time.
LEXICAL_MATCH_SQL = "c.tsv @@ websearch_to_tsquery('english', %s)"
LEXICAL_RANK_SQL = "ts_rank_cd(c.tsv, websearch_to_tsquery('english', %s))"


class BakeoffError(RuntimeError):
    """A condition the operator must resolve before the run means anything."""


class BakeoffSafetyError(BakeoffError):
    """A statement that would write outside the bake-off tables."""


class BudgetExhausted(BakeoffError):
    """A candidate reached its hard token ceiling."""


def assert_lexical_contract(module=None) -> None:
    """Fail loudly if the product's lexical ranking has moved.

    A bake-off that scores its own fork of the ranker measures the fork. This
    runs at import, costs one string build, and names the file to re-read.
    """
    module = module or _QL
    sql, _ = module.lexical_sql({}, "probe", 1)
    collapsed = " ".join(sql.split())
    missing = [frag for frag in (LEXICAL_MATCH_SQL, LEXICAL_RANK_SQL)
               if frag not in collapsed]
    if missing:
        raise BakeoffError(
            f"{QUERY_LAMBDA} lexical_sql() no longer contains "
            f"{missing!r}: the bake-off would fuse a lexical arm the product "
            f"does not ship. Re-read that builder and update "
            f"LEXICAL_MATCH_SQL / LEXICAL_RANK_SQL to match it.")


assert_lexical_contract()

# Corpus facts, measured, not re-derived here. They only drive the offline
# projection --dry-run prints when it has no database to count against.
CORPUS_CHUNKS = 852_027
COHERE_NULL_CHUNKS = 584_449
QWEN_NULL_CHUNKS = 527_646
COHERE_DAILY_TOKEN_CAP = 16_200_000

BASELINE = "titan"
CANDIDATES = ("titan", "cohere", "qwen")
DEFAULT_CANDIDATES = ("titan", "cohere")
CANDIDATE_DIMS = {"titan": 1024, "cohere": 1536, "qwen": 1536}
TITAN_MODEL = "amazon.titan-embed-text-v2:0"
COHERE_MODEL = "us.cohere.embed-v4:0"
COHERE_BATCH = 40
QWEN_BATCH = 32
# Same clamps the fill paths use, so a bake-off vector is the vector the fill
# would have produced: ws13_embed_backfill truncates Titan at 8000 characters
# and Cohere at 6000, ws13_qwen_overlay truncates at 6000.
TEXT_LIMIT = {"titan": 8000, "cohere": 6000, "qwen": 6000}

DEFAULT_SAMPLE_SIZE = 20_000
DEFAULT_SEED = "ws13-bakeoff-1"
MIN_PER_STRATUM = 25
# The floor pass may claim at most half the sample. Without that ceiling the
# floor alone would decide the whole draw: three admission classes across the
# ~50 states the corpus spans, times five quality bands, is on the order of 750
# non-empty strata, and 750 * 25 = 18,750 of 20,000 slots. The sample would
# then be uniform over cells rather than representative of the corpus, which is
# a different experiment than the one the report claims to describe.
FLOOR_SHARE = 0.5
# Mean tokens for a page-anchored chunk under the estimator the backfill bills
# against (len(text)//3 over CHUNK_CHARS=3000). Used only for projections; the
# budget is charged against real usage.
MEAN_TOKENS_PER_CHUNK = 1000
# Enough for a 20,000-chunk sample at that estimator with 20% headroom, which
# is about 1.5 days of the Cohere daily cap against the ~36 days the remaining
# NULL rows would cost. Deliberately a hard stop, not a warning.
DEFAULT_TOKEN_BUDGET = 24_000_000

# Lexical quality proxy bands. pipelines/ws13_quality_proxy.py scores each page
# 0-100 and flags low_confidence below WEAK_SCORE; a page under 40 characters
# scores None, which means "unknown", never zero. The bands exist so the draw
# can be forced across them: an unstratified sample of a corpus that is 51%
# born-digital would under-represent exactly the degraded OCR a new embedding
# model is supposed to help with.
WEAK_SCORE = 55.0
DEGRADED_SCORE = 40.0
CLEAN_SCORE = 75.0
QUALITY_BANDS = ("unknown", "degraded", "weak", "fair", "clean")

RECALL_KS = (1, 5, 10)
PRIMARY_METRIC = "recall@5"
AGREEMENT_K = 10
# A paired difference over fewer than this many labelled queries cannot
# separate a real gain from which questions happened to be written down. The
# fixture's target_count is 25 and it currently holds 1 verified item, so the
# honest default verdict today is 'insufficient-evidence'.
MIN_LABELLED_QUERIES = 20
Z95 = 1.96

# On-demand Bedrock list prices in USD per million input tokens. These are
# ASSUMPTIONS, not measurements: the report stamps price_source and
# price_recorded so a stale rate is visible in the artifact instead of silently
# baked into a verdict, and --price NAME=USD overrides any of them.
PRICE_PER_MTOK = {"titan": 0.02, "cohere": 0.12, "qwen": 0.0}
PRICE_SOURCE = "AWS Bedrock on-demand list price, operator-recorded"
PRICE_RECORDED = "2026-08-25"
# Qwen is not billed per token; it is billed by the hour of the single
# g5.2xlarge this account's quota allows. Scoring it on tokens alone would
# report a GPU candidate as free, which is the arithmetic that makes a GPU
# overlay look cheap right up until the invoice.
GPU_HOURLY_USD = {"qwen": 1.212}

PRODUCTION_VECTOR_COLUMNS = ("titan_embedding", "embedding", "qwen_embedding")
BAKEOFF_TABLES = ("ws13_bakeoff_runs", "ws13_bakeoff_sample",
                  "ws13_bakeoff_vectors")
KNOWN_ITEMS_PATH = HERE.parents[0] / "tests" / "fixtures" / "ws13_known_items.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ws13_bakeoff_runs (
  run_id TEXT PRIMARY KEY, seed TEXT NOT NULL, sample_size INT NOT NULL,
  candidates TEXT[] NOT NULL, sampling_rule JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ws13_bakeoff_sample (
  run_id TEXT NOT NULL, chunk_id BIGINT NOT NULL, sha256 TEXT NOT NULL,
  page INT NOT NULL, stratum TEXT NOT NULL, mandatory BOOL NOT NULL,
  PRIMARY KEY (run_id, chunk_id));
CREATE TABLE IF NOT EXISTS ws13_bakeoff_vectors (
  run_id TEXT NOT NULL, candidate TEXT NOT NULL, chunk_id BIGINT NOT NULL,
  dims INT NOT NULL, vec REAL[] NOT NULL, tokens INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, candidate, chunk_id));
"""


def log(message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"{stamp} {message}", flush=True)


# ---------------------------------------------------------------------------
# Write guard.

_MUTATING_VERBS = ("insert", "update", "delete", "truncate", "create", "drop",
                   "alter", "copy", "merge")
_CREATE_INDEX_RE = re.compile(
    r"\Acreate\s+index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?"
    r"[\w.\"]+\s+on\s+([\w.\"]+)", re.I)
_TARGET_RE = re.compile(
    r"\A(?:insert\s+into|update|delete\s+from|merge\s+into|copy|"
    r"truncate(?:\s+table)?|create\s+table(?:\s+if\s+not\s+exists)?|"
    r"drop\s+table(?:\s+if\s+exists)?|alter\s+table(?:\s+if\s+exists)?)"
    r"\s+(?:only\s+)?([\w.\"]+)", re.I)
_SET_COLUMN_RE = "|".join(PRODUCTION_VECTOR_COLUMNS)


def is_mutating(sql: str) -> bool:
    """True when the statement can change data or schema."""
    head = " ".join(sql.split()).lstrip("(").split(" ", 1)[0].lower()
    return head in _MUTATING_VERBS


def write_target(sql: str) -> str | None:
    """The relation a mutating statement writes, lowercased and unquoted.

    The TARGET, not any relation named: the Titan path is
    ``INSERT INTO ws13_bakeoff_vectors ... SELECT ... FROM ws13_chunks``, which
    reads the production column and writes only the bake-off table. A guard
    that refused on any mention of ws13_chunks would refuse the one candidate
    that costs nothing.
    """
    collapsed = " ".join(sql.split()).strip()
    match = _CREATE_INDEX_RE.match(collapsed) or _TARGET_RE.match(collapsed)
    if not match:
        return None
    return match.group(1).strip('"').lower()


def assert_write_allowed(sql: str) -> str:
    """Refuse any mutating statement that leaves the bake-off tables.

    This is the mechanism behind rule 2 in the module docstring. The failure it
    prevents is not hypothetical arithmetic: an UPDATE of ws13_chunks.embedding
    for 20,000 sampled rows is indistinguishable, afterwards, from a decision
    to fill the column -- coverage moves, the backfill's NULL scan skips those
    rows, and the experiment has quietly become the commitment.
    """
    collapsed = " ".join(sql.split()).strip()
    if not is_mutating(collapsed):
        return collapsed
    target = write_target(collapsed)
    if target is None:
        raise BakeoffSafetyError(
            f"cannot identify the write target of {collapsed[:120]!r}; the "
            f"bake-off refuses statements it cannot prove are confined to "
            f"{list(BAKEOFF_TABLES)}")
    if target not in BAKEOFF_TABLES:
        raise BakeoffSafetyError(
            f"refusing to write {target}: the bake-off writes only "
            f"{list(BAKEOFF_TABLES)}. Landing sample vectors in a production "
            f"table turns a reversible experiment into a commitment.")
    if re.search(rf"\bset\b[^;]*\b(?:{_SET_COLUMN_RE})\s*=", collapsed, re.I):
        raise BakeoffSafetyError(
            f"refusing {collapsed[:120]!r}: it assigns a production embedding "
            f"column ({', '.join(PRODUCTION_VECTOR_COLUMNS)})")
    return collapsed


def write(conn, sql: str, params=()):  # pragma: no cover - thin DB wrapper
    """Every mutating statement in this module goes through here."""
    assert_write_allowed(sql)
    return conn.execute(sql, params)


def read(conn, sql: str, params=()):  # pragma: no cover - thin DB wrapper
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Sampling.

def quality_band(score) -> str:
    """Band a page's lexical quality proxy score.

    None is 'unknown', never 'degraded': ws13_quality_proxy.score() returns
    None for a page under 40 characters, and treating unknown as worst would
    stuff the degraded stratum with blank pages and leave real OCR noise
    under-sampled -- the exact failure the stratification exists to avoid.
    """
    if score is None:
        return "unknown"
    value = float(score)
    if value < DEGRADED_SCORE:
        return "degraded"
    if value < WEAK_SCORE:
        return "weak"
    if value < CLEAN_SCORE:
        return "fair"
    return "clean"


def stratum_key(admission_class, state, score) -> str:
    """The stratification cell for one chunk: rights x state x quality band."""
    klass = str(admission_class or "unknown").strip() or "unknown"
    where = str(state or "unknown").strip().upper() or "UNKNOWN"
    return f"{klass}|{where}|{quality_band(score)}"


def draw_key(seed: str, chunk_id) -> str:
    """Deterministic membership key: sha256(seed:chunk_id), hex.

    DRAW_KEY_SQL below is the same expression in Postgres, so the sample a
    laptop reproduces from the seed is the sample the database drew. Not a
    random shuffle: an ORDER BY random() sample is unreproducible even to the
    person who ran it, and an unreproducible sample cannot support a decision
    about four weeks of spend.
    """
    return hashlib.sha256(f"{seed}:{chunk_id}".encode()).hexdigest()


DRAW_KEY_SQL = "encode(sha256(convert_to(%s || ':' || c.id::text, 'UTF8')), 'hex')"


def allocate(populations: dict, sample_size: int,
             floor: int = MIN_PER_STRATUM,
             floor_share: float = FLOOR_SHARE) -> dict:
    """Per-stratum quotas: a bounded floor for breadth, then proportional.

    The floor pass hands out one row at a time in ascending population order,
    so a sample smaller than floor * len(strata) still covers as many strata as
    it can instead of filling the largest ones. Without it, research-copies
    (32,312 documents of 56,282) plus the clean band would swallow the draw and
    the degraded OCR strata -- a few hundred chunks each -- would contribute
    single-digit rows, which is a bake-off that avoids the hard cases. The
    floor is capped at floor_share of the sample so it cannot swing the draw
    the other way and make it uniform over cells; see FLOOR_SHARE.
    """
    keys = sorted(populations)
    quota = {key: 0 for key in keys}
    remaining = max(0, int(sample_size))
    floor_budget = min(remaining, int(remaining * max(0.0, floor_share)))
    ascending = sorted(keys, key=lambda key: (populations[key], key))
    for _ in range(max(0, int(floor))):
        if floor_budget <= 0:
            break
        for key in ascending:
            if floor_budget <= 0:
                break
            if quota[key] < min(floor, populations[key]):
                quota[key] += 1
                remaining -= 1
                floor_budget -= 1
    while remaining > 0:
        room = {key: populations[key] - quota[key] for key in keys}
        total = sum(room.values())
        if total <= 0:
            break
        shares = {key: remaining * room[key] / total for key in keys}
        whole = {key: min(room[key], int(shares[key])) for key in keys}
        assigned = sum(whole.values())
        if assigned == 0:
            # Fewer slots left than strata with room: largest fractional share
            # takes them, one each, which terminates because total > 0.
            ranked = sorted(keys, key=lambda key: (-shares[key], key))
            for key in ranked:
                if remaining <= 0:
                    break
                if room[key] > 0:
                    quota[key] += 1
                    remaining -= 1
            continue
        for key in keys:
            quota[key] += whole[key]
        remaining -= assigned
    return quota


def draw(rows, quota: int, seed: str) -> list:
    """The first `quota` rows of a stratum under the deterministic key."""
    ranked = sorted(rows, key=lambda row: (draw_key(seed, row["chunk_id"]),
                                           int(row["chunk_id"])))
    return ranked[:max(0, int(quota))]


def sampling_rule(seed: str, sample_size: int, mandatory_shas,
                  floor: int = MIN_PER_STRATUM) -> dict:
    """The rule, in the report, in enough detail to re-run it.

    Recorded rather than described: a reader with this dict, the seed and the
    corpus rebuilds the identical sample without reading this file.
    """
    return {
        "seed": seed,
        "sample_size": int(sample_size),
        "min_per_stratum": int(floor),
        "floor_share": FLOOR_SHARE,
        "mandatory": {
            "source": str(KNOWN_ITEMS_PATH.name),
            "rule": ("every chunk of every document named in the known-item "
                     "fixture, included whole and never subject to the draw"),
            "sha256": sorted(mandatory_shas),
        },
        "strata": {
            "dimensions": ["ws13_documents.admission_class",
                           "ws13_documents.state",
                           "ws13_pages.quality_score band"],
            "quality_bands": {
                "unknown": "quality_score IS NULL (page under 40 characters)",
                "degraded": f"< {DEGRADED_SCORE}",
                "weak": f"{DEGRADED_SCORE} <= score < {WEAK_SCORE}",
                "fair": f"{WEAK_SCORE} <= score < {CLEAN_SCORE}",
                "clean": f">= {CLEAN_SCORE}",
            },
            "allocation": ("floor of min_per_stratum handed out one row at a "
                           "time in ascending population order, capped at "
                           "floor_share of the sample, then largest-remainder "
                           "proportional on the residual population"),
        },
        "draw": {
            "python": "sha256(f'{seed}:{chunk_id}').hexdigest(), ascending",
            "sql": DRAW_KEY_SQL,
            "tie_break": "chunk_id ascending",
        },
        "quality_proxy": ("pipelines/ws13_quality_proxy.py lexical-proxy-v1; "
                          "true tesseract confidences are NULL for every page "
                          "and this is a labelled proxy, not a confidence"),
    }


def rule_fingerprint(rule: dict) -> str:
    """Short stable hash of the rule, so a run id names its own sampling."""
    payload = json.dumps(rule, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def run_id_for(rule: dict) -> str:
    return (f"bakeoff-{rule['seed']}-{rule['sample_size']}-"
            f"{rule_fingerprint(rule)}")


def build_sample(mandatory_rows, pool_by_stratum, sample_size: int, seed: str,
                 floor: int = MIN_PER_STRATUM, quota: dict | None = None,
                 populations: dict | None = None) -> dict:
    """Assemble the bake-off sample from mandatory rows plus a stratified draw.

    `pool_by_stratum` maps stratum key -> candidate rows already fetched for
    that stratum, each a mapping with chunk_id / sha256 / page. Mandatory rows
    are never dropped: when the known-item documents alone exceed sample_size
    the sample is reported oversubscribed rather than silently losing the only
    ground truth the run has.

    `quota` and `populations` come from the caller when it has already run
    allocate() over the TRUE corpus counts. Recomputing the allocation here
    from pool lengths would allocate over numbers that were themselves capped
    by the first allocation -- so every stratum's reported population would be
    its own quota, and a reader could not tell a stratum of 200,000 chunks from
    one of 25.
    """
    chosen: dict = {}
    for row in sorted(mandatory_rows, key=lambda r: int(r["chunk_id"])):
        record = dict(row)
        record["mandatory"] = True
        record.setdefault("stratum", "mandatory")
        chosen[int(row["chunk_id"])] = record
    mandatory_count = len(chosen)

    pools = {}
    for stratum, rows in pool_by_stratum.items():
        pools[stratum] = [row for row in rows
                          if int(row["chunk_id"]) not in chosen]
    if populations is None:
        populations = {stratum: len(rows) for stratum, rows in pools.items()
                       if rows}
    else:
        populations = {stratum: count for stratum, count in populations.items()
                       if pools.get(stratum)}
    if quota is None:
        budget = max(0, int(sample_size) - mandatory_count)
        quota = allocate(populations, budget, floor) if populations else {}
    quota = {stratum: value for stratum, value in quota.items()
             if stratum in pools}
    for stratum in sorted(quota):
        for row in draw(pools[stratum], quota[stratum], seed):
            record = dict(row)
            record["mandatory"] = False
            record["stratum"] = stratum
            chosen[int(row["chunk_id"])] = record

    rows = [chosen[key] for key in sorted(chosen)]
    strata_report = []
    for stratum in sorted(populations):
        strata_report.append({
            "stratum": stratum,
            "population": populations[stratum],
            "quota": quota.get(stratum, 0),
            "drawn": sum(1 for row in rows if row.get("stratum") == stratum),
        })
    return {
        "chunks": rows,
        "total": len(rows),
        "mandatory": mandatory_count,
        "drawn": len(rows) - mandatory_count,
        "requested": int(sample_size),
        "oversubscribed": mandatory_count > int(sample_size),
        "strata": strata_report,
    }


# ---------------------------------------------------------------------------
# Budget and cost.

class TokenBudget:
    """A hard per-candidate token ceiling, charged on success only.

    Charging on reservation was the defect ws13_embed_backfill.cohere_worker
    fixed against the 16.2M/day cap: a throttled or failed request burned quota
    that was never spent at the service, permanently shrinking an already
    binding allowance. Here the consequence would be subtler and worse -- a
    candidate cut short by phantom spend gets partial coverage and loses the
    comparison for a reason that has nothing to do with retrieval quality.
    """

    def __init__(self, limit: int, spent: int = 0):
        self.limit = max(0, int(limit))
        self.spent = max(0, int(spent))
        self.exhausted = False

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def can_afford(self, tokens: int) -> bool:
        return self.spent + max(0, int(tokens)) <= self.limit

    def charge(self, tokens: int) -> int:
        tokens = max(0, int(tokens))
        if self.spent + tokens > self.limit:
            self.exhausted = True
            raise BudgetExhausted(
                f"token budget {self.limit} would be exceeded: {self.spent} "
                f"already spent, this batch costs {tokens}. Raise "
                f"--token-budget deliberately or accept partial coverage; the "
                f"candidate is excluded from the verdict either way.")
        self.spent += tokens
        return self.spent


def estimate_tokens(text: str) -> int:
    """len(text)//3, the same estimator ws13_embed_backfill bills against.

    Deliberately the same wrong-but-consistent number: the bake-off's job is a
    comparison, and a projection that used a different estimator than the fill
    would compare against would not translate into the fill's day count.
    """
    return max(1, len(text or "") // 3)


def cost_usd(candidate: str, tokens: int, seconds: float,
             prices: dict | None = None) -> float:
    """Dollars for one candidate's embedding work.

    Per-token for the Bedrock models, per-GPU-hour for Qwen. Mixing the two
    units is how a candidate that needs a dedicated g5.2xlarge gets reported as
    free: its tokens genuinely cost nothing, and the node costs
    GPU_HOURLY_USD/hour whether it is embedding or idle.
    """
    prices = PRICE_PER_MTOK if prices is None else prices
    per_token = max(0, int(tokens)) / 1_000_000 * prices.get(candidate, 0.0)
    per_hour = GPU_HOURLY_USD.get(candidate, 0.0) * max(0.0, seconds) / 3600.0
    return round(per_token + per_hour, 6)


def project_full_fill(candidate: str, tokens: int, chunks: int,
                      seconds: float, prices: dict | None = None) -> dict:
    """What committing this candidate to the whole corpus would cost.

    The number the verdict is actually about. A recall gain of half a point is
    a different decision at $12 than at 36 days of a binding daily cap, so the
    report carries both and never just the metric.
    """
    chunks = max(1, int(chunks))
    per_chunk_tokens = max(0, int(tokens)) / chunks
    per_chunk_seconds = max(0.0, seconds) / chunks
    outstanding = {"cohere": COHERE_NULL_CHUNKS, "qwen": QWEN_NULL_CHUNKS}
    remaining = outstanding.get(candidate, 0)
    total_tokens = int(round(per_chunk_tokens * remaining))
    total_seconds = per_chunk_seconds * remaining
    days = (total_tokens / COHERE_DAILY_TOKEN_CAP
            if candidate == "cohere" and total_tokens else None)
    return {
        "chunks_outstanding": remaining,
        "tokens": total_tokens,
        "usd": cost_usd(candidate, total_tokens, total_seconds, prices),
        "days_of_cohere_daily_cap": round(days, 2) if days else None,
        "wall_clock_hours": round(total_seconds / 3600.0, 2),
    }


# ---------------------------------------------------------------------------
# Scoring.

def fuse(lexical_ids, vector_ids, limit: int) -> list:
    """The product's RRF over this candidate's two arms.

    rrf_fuse is imported, never reimplemented: the whole claim of the bake-off
    is that it measures the shipping ranker.
    """
    ranked = {"lexical": list(lexical_ids), "vector": list(vector_ids)}
    fused = rrf_fuse(ranked, RRF_K)
    return [chunk_id for chunk_id, _score, _ranks in fused[:max(0, limit)]]


def answer_keys(item: dict) -> set:
    """The (sha256, page) pairs that answer one known item."""
    return {(str(item["sha256"]).lower(), int(item["page"]))}


def query_scores(ranking, answers, page_of, ks=RECALL_KS) -> dict:
    """Hit indicators and reciprocal rank for one query's fused ranking.

    `page_of` maps chunk_id -> (sha256, page); a hit is the right page of the
    right document, not the right chunk, because chunking is an implementation
    detail the answer key must not depend on.
    """
    hits = {int(k): 0 for k in ks}
    reciprocal = 0.0
    for position, chunk_id in enumerate(ranking, 1):
        if page_of.get(chunk_id) in answers:
            reciprocal = 1.0 / position
            for k in hits:
                hits[k] = 1 if position <= k else hits[k]
            break
    return {"hits": hits, "reciprocal_rank": reciprocal}


def aggregate_scores(per_query, ks=RECALL_KS) -> dict:
    """recall@k and MRR over the labelled queries."""
    count = len(per_query)
    out = {"labelled_queries": count}
    for k in ks:
        if count:
            hit = sum(row["hits"].get(int(k), 0) for row in per_query)
            out[f"recall@{k}"] = round(hit / count, 4)
        else:
            out[f"recall@{k}"] = None
    if count:
        out["mrr"] = round(
            sum(row["reciprocal_rank"] for row in per_query) / count, 4)
    else:
        out["mrr"] = None
    return out


def rank_agreement(first, second, k: int = AGREEMENT_K) -> dict:
    """How far two candidates' top-k disagree, where no answer key exists.

    Most queries have no ground truth -- the fixture holds 1 verified item of
    25 -- and reporting nothing for them would hide the case that matters most:
    two candidates that rank identically cannot be worth four weeks of spend,
    whatever their recall on a handful of labelled questions says.
    """
    top_first = list(first)[:max(0, k)]
    top_second = list(second)[:max(0, k)]
    common = set(top_first) & set(top_second)
    denominator = max(1, min(k, max(len(top_first), len(top_second))))
    position_first = {cid: i for i, cid in enumerate(top_first, 1)}
    position_second = {cid: i for i, cid in enumerate(top_second, 1)}
    shifts = [abs(position_first[cid] - position_second[cid]) for cid in common]
    return {
        "overlap_at_k": round(len(common) / denominator, 4),
        "common": len(common),
        "mean_displacement": (round(sum(shifts) / len(shifts), 3)
                              if shifts else None),
    }


def paired_delta(baseline_hits, candidate_hits) -> dict:
    """Paired difference of per-query hit indicators, with a 95% interval.

    Paired because both candidates answer the SAME queries: the query-to-query
    variance is enormous compared with the difference between two embedding
    models, and an unpaired comparison would drown a real effect in it.
    """
    pairs = list(zip(baseline_hits, candidate_hits))
    count = len(pairs)
    if not count:
        return {"n": 0, "delta": None, "stderr": None, "ci95": None,
                "significant": False}
    diffs = [float(candidate) - float(base) for base, candidate in pairs]
    mean = sum(diffs) / count
    if count > 1:
        variance = sum((d - mean) ** 2 for d in diffs) / (count - 1)
        stderr = math.sqrt(variance / count)
    else:
        stderr = 0.0
    margin = Z95 * stderr
    significant = count >= MIN_LABELLED_QUERIES and abs(mean) > margin
    return {
        "n": count,
        "delta": round(mean, 4),
        "stderr": round(stderr, 4),
        "ci95": [round(mean - margin, 4), round(mean + margin, 4)],
        "significant": bool(significant),
    }


def compare_to_baseline(results: dict, baseline: str = BASELINE,
                        metric: str = PRIMARY_METRIC) -> tuple:
    """(deltas, labelled_queries), paired BY QUERY ID and never by position.

    A candidate whose query embedding failed once -- a throttle, an exhausted
    budget -- has a shorter per_query list than the baseline. Zipping the two
    lists by position would then compare that candidate's question 5 against
    the baseline's question 4 for every remaining row: a total corruption of
    the delta that still produces a confident-looking interval, because the
    arithmetic downstream has no way to notice the rows do not correspond.
    """
    metric_k = int(str(metric).split("@")[1])

    def hits_by_query(name):
        return {row["query_id"]: row["hits"].get(metric_k, 0)
                for row in results.get(name, {}).get("per_query", [])}

    baseline_hits = hits_by_query(baseline)
    deltas, paired_counts = {}, []
    for name in sorted(results):
        if name == baseline:
            continue
        candidate_hits = hits_by_query(name)
        shared = [query_id for query_id in baseline_hits
                  if query_id in candidate_hits]
        deltas[name] = paired_delta(
            [baseline_hits[query_id] for query_id in shared],
            [candidate_hits[query_id] for query_id in shared])
        paired_counts.append(len(shared))
    # The verdict rests on the queries every comparison actually shares, not on
    # the largest list any one candidate happened to produce.
    labelled = min(paired_counts) if paired_counts else len(baseline_hits)
    return deltas, labelled


def verdict(deltas: dict, labelled_queries: int, incomplete=(),
            baseline: str = BASELINE, metric: str = PRIMARY_METRIC) -> dict:
    """Name a winner only when the measurement supports one.

    Three ways this refuses, in order:
      * a candidate whose sample coverage is partial (budget exhausted, embed
        failures) is excluded outright -- comparing a full candidate against
        half a sample measures the budget, not the model;
      * below MIN_LABELLED_QUERIES the answer is 'insufficient-evidence', which
        is the honest reading of a fixture holding 1 verified item of 25;
      * a paired 95% interval that spans zero is 'no-clear-winner'.

    The default outcome is refusal. A bake-off that always names a winner is
    worse than none: it launders the prior into a measurement.
    """
    # The baseline is what everything is measured AGAINST, so it is never one
    # of the things excluded from the comparison.
    excluded = sorted(set(incomplete) - {baseline})
    considered = {name: value for name, value in deltas.items()
                  if name not in excluded}
    base = {
        "baseline": baseline,
        "metric": metric,
        "labelled_queries": int(labelled_queries),
        "min_labelled_queries": MIN_LABELLED_QUERIES,
        "excluded_incomplete": excluded,
        "winner": None,
    }
    if not considered:
        if not deltas:
            reason = f"nothing was measured against {baseline}"
            if excluded:
                reason += (f"; {', '.join(excluded)} did not complete the "
                           f"sample")
        else:
            reason = (f"every candidate compared against {baseline} has "
                      f"partial coverage: {', '.join(excluded)}. Comparing a "
                      f"complete candidate against half a sample measures the "
                      f"budget, not the model.")
        return dict(base, verdict="insufficient-evidence", reason=reason)
    if int(labelled_queries) < MIN_LABELLED_QUERIES:
        return dict(base, verdict="insufficient-evidence", reason=(
            f"{labelled_queries} labelled queries is below the "
            f"{MIN_LABELLED_QUERIES} needed to separate a real gain from "
            f"which questions happened to be written down; fill "
            f"{KNOWN_ITEMS_PATH.name} before reading any delta as a result"))
    winners = {name: value for name, value in considered.items()
               if value.get("significant") and (value.get("delta") or 0) > 0}
    if not winners:
        spread = ", ".join(
            f"{name} {value.get('delta')} (95% CI {value.get('ci95')})"
            for name, value in sorted(considered.items()))
        return dict(base, verdict="no-clear-winner", reason=(
            f"every {metric} difference against {baseline} has a 95% interval "
            f"spanning zero over {labelled_queries} labelled queries: {spread}."
            f" {baseline} is already complete and costs nothing more, so the "
            f"measurement does not support a fill."))
    best = max(winners, key=lambda name: winners[name]["delta"])
    value = winners[best]
    return dict(base, verdict="candidate-wins", winner=best, reason=(
        f"{best} beats {baseline} on {metric} by {value['delta']} "
        f"(95% CI {value['ci95']}, n={value['n']}). The interval excludes "
        f"zero; weigh it against that candidate's projected full-fill cost "
        f"before committing, and note that {labelled_queries} labelled "
        f"queries bound how finely this can discriminate."))


# ---------------------------------------------------------------------------
# Corpus access.

def load_known_items(path=KNOWN_ITEMS_PATH) -> list:
    """The known-item fixture, or an empty set with a loud line.

    A missing fixture is not fatal: the rank-agreement half of the report still
    means something without ground truth. It IS reported, because a run with no
    labelled queries can only ever return 'insufficient-evidence'.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        log(f"known-item fixture {path} is missing: no labelled queries, so "
            f"the verdict can only be insufficient-evidence")
        return []
    return [item for item in payload.get("items", []) if item.get("verified")]


def mandatory_shas(items) -> list:
    return sorted({str(item["sha256"]).lower() for item in items})


def schema_statements() -> list:
    """SCHEMA_SQL as individual statements. No statement contains a ';'."""
    return [part.strip() for part in SCHEMA_SQL.split(";") if part.strip()]


def ensure_schema(conn) -> None:
    for statement in schema_statements():
        write(conn, statement)


STRATUM_COUNT_SQL = """
SELECT d.admission_class, d.state, p.quality_score, COUNT(*)
  FROM ws13_chunks c
  JOIN ws13_documents d ON d.sha256 = c.sha256
  LEFT JOIN ws13_pages p ON p.sha256 = c.sha256 AND p.page = c.page
 GROUP BY 1, 2, 3
"""


def stratum_populations(conn) -> dict:
    """Chunk counts per stratum, banded in Python.

    Banded here rather than in SQL so quality_band() is the single definition
    of the bands and the test suite can exercise the same function the run
    uses. The GROUP BY is over distinct scores, which is a few thousand rows.
    """
    counts: dict = {}
    for row in read(conn, STRATUM_COUNT_SQL):
        klass, state, score, total = row
        key = stratum_key(klass, state, score)
        counts[key] = counts.get(key, 0) + int(total)
    return counts


POOL_SQL = """
SELECT c.id, c.sha256, c.page
  FROM ws13_chunks c
  JOIN ws13_documents d ON d.sha256 = c.sha256
  LEFT JOIN ws13_pages p ON p.sha256 = c.sha256 AND p.page = c.page
 WHERE COALESCE(NULLIF(TRIM(d.admission_class), ''), 'unknown') = %s
   AND COALESCE(NULLIF(UPPER(TRIM(d.state)), ''), 'UNKNOWN') = %s
   AND {band}
   AND c.text <> ''
 ORDER BY {draw_key}
 LIMIT %s
"""
BAND_SQL = {
    "unknown": "p.quality_score IS NULL",
    "degraded": f"p.quality_score < {DEGRADED_SCORE}",
    "weak": (f"p.quality_score >= {DEGRADED_SCORE} "
             f"AND p.quality_score < {WEAK_SCORE}"),
    "fair": (f"p.quality_score >= {WEAK_SCORE} "
             f"AND p.quality_score < {CLEAN_SCORE}"),
    "clean": f"p.quality_score >= {CLEAN_SCORE}",
}


def pool_sql(stratum: str) -> str:
    """The per-stratum draw, ordered by the SQL twin of draw_key()."""
    _klass, _state, band = stratum.split("|", 2)
    if band not in BAND_SQL:
        raise BakeoffError(f"unknown quality band {band!r} in stratum "
                           f"{stratum!r}")
    return POOL_SQL.format(band=BAND_SQL[band], draw_key=DRAW_KEY_SQL)


def fetch_pool(conn, stratum: str, seed: str, limit: int) -> list:
    klass, state, _band = stratum.split("|", 2)
    rows = read(conn, pool_sql(stratum), (klass, state, seed, int(limit)))
    return [{"chunk_id": int(cid), "sha256": sha, "page": int(page)}
            for cid, sha, page in rows]


MANDATORY_SQL = """
SELECT c.id, c.sha256, c.page
  FROM ws13_chunks c
 WHERE c.sha256 = ANY(%s) AND c.text <> ''
 ORDER BY c.id
"""


def fetch_mandatory(conn, shas) -> list:
    if not shas:
        return []
    rows = read(conn, MANDATORY_SQL, (list(shas),))
    return [{"chunk_id": int(cid), "sha256": sha, "page": int(page)}
            for cid, sha, page in rows]


def persist_run(conn, run_id: str, rule: dict, candidates) -> None:
    write(conn,
          "INSERT INTO ws13_bakeoff_runs "
          "(run_id, seed, sample_size, candidates, sampling_rule) "
          "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
          (run_id, rule["seed"], rule["sample_size"], list(candidates),
           json.dumps(rule, sort_keys=True)))


def persist_sample(conn, run_id: str, rows) -> None:
    for row in rows:
        write(conn,
              "INSERT INTO ws13_bakeoff_sample "
              "(run_id, chunk_id, sha256, page, stratum, mandatory) "
              "VALUES (%s, %s, %s, %s, %s, %s) "
              "ON CONFLICT (run_id, chunk_id) DO NOTHING",
              (run_id, int(row["chunk_id"]), row["sha256"], int(row["page"]),
               row.get("stratum", "mandatory"), bool(row.get("mandatory"))))


RUN_SQL = "SELECT sampling_rule FROM ws13_bakeoff_runs WHERE run_id = %s"


def load_run(conn, run_id: str):
    """The sampling rule a previous session stored for this run, or None."""
    rows = read(conn, RUN_SQL, (run_id,))
    if not rows:
        return None
    value = rows[0][0]
    return json.loads(value) if isinstance(value, str) else value


def assert_resumable(conn, run_id: str, rule: dict) -> None:
    """Refuse to resume a run that was drawn under a different rule.

    The default run id embeds the rule's fingerprint, so this only bites when
    an operator passes --run-id explicitly -- which is exactly when it is
    needed. persist_sample() inserts ON CONFLICT DO NOTHING, so a second draw
    under the same id would not overwrite the first: it would ADD to it, and
    the run would report metrics over a sample that is the union of two
    different sampling rules with no record that it happened.
    """
    existing = load_run(conn, run_id)
    if existing is None or existing == rule:
        return
    raise BakeoffError(
        f"run {run_id!r} was drawn under a different sampling rule "
        f"(stored fingerprint {rule_fingerprint(existing)}, this run "
        f"{rule_fingerprint(rule)}). Resuming would merge two samples into one "
        f"result. Drop --run-id to get a fresh id, or re-run with the seed and "
        f"--sample-size that produced the stored one.")


SAMPLE_SQL = ("SELECT chunk_id, sha256, page, stratum, mandatory "
              "FROM ws13_bakeoff_sample WHERE run_id = %s ORDER BY chunk_id")


def load_sample(conn, run_id: str) -> list:
    rows = read(conn, SAMPLE_SQL, (run_id,))
    return [{"chunk_id": int(cid), "sha256": sha, "page": int(page),
             "stratum": stratum, "mandatory": bool(mandatory)}
            for cid, sha, page, stratum, mandatory in rows]


PENDING_SQL = """
SELECT s.chunk_id, c.text
  FROM ws13_bakeoff_sample s
  JOIN ws13_chunks c ON c.id = s.chunk_id
 WHERE s.run_id = %s
   AND NOT EXISTS (SELECT 1 FROM ws13_bakeoff_vectors v
                    WHERE v.run_id = s.run_id AND v.candidate = %s
                      AND v.chunk_id = s.chunk_id)
 ORDER BY s.chunk_id
"""


def pending_chunks(conn, run_id: str, candidate: str) -> list:
    """Sample rows this candidate still owes a vector.

    Resumability is the anti-join, not a checkpoint file: a run killed halfway
    resumes exactly where it stopped, and re-running a finished candidate costs
    one query and zero tokens.
    """
    return [(int(cid), text)
            for cid, text in read(conn, PENDING_SQL, (run_id, candidate))]


TITAN_COPY_SQL = """
INSERT INTO ws13_bakeoff_vectors
       (run_id, candidate, chunk_id, dims, vec, tokens)
SELECT s.run_id, %s, s.chunk_id, %s, c.titan_embedding::real[], 0
  FROM ws13_bakeoff_sample s
  JOIN ws13_chunks c ON c.id = s.chunk_id
 WHERE s.run_id = %s AND c.titan_embedding IS NOT NULL
ON CONFLICT (run_id, candidate, chunk_id) DO NOTHING
"""


def copy_titan(conn, run_id: str) -> int:
    """Read the production Titan column into the bake-off table. Never embed.

    titan_embedding is complete over all 852,027 chunks, so the baseline's
    marginal cost is zero -- which is half the decision. Re-embedding it to
    make the harness symmetrical would spend real Titan quota to reproduce
    vectors the account has already paid for.
    """
    result = write(conn, TITAN_COPY_SQL,
                   ("titan", CANDIDATE_DIMS["titan"], run_id))
    return int(getattr(result, "rowcount", 0) or 0)


def store_vectors(conn, run_id: str, candidate: str, rows) -> int:
    """Land one candidate's vectors, refusing any of the wrong width.

    rank_vector() filters on `dims = %s`, so a short vector would not error --
    it would silently drop out of that candidate's ranking and cost it recall
    it never actually lost. Refuse at write time, where the cause is visible.
    """
    expected = CANDIDATE_DIMS[candidate]
    stored = 0
    for chunk_id, vector, tokens in rows:
        if len(vector) != expected:
            raise BakeoffError(
                f"{candidate} returned {len(vector)} dims for chunk "
                f"{chunk_id}, expected {expected}")
        write(conn,
              "INSERT INTO ws13_bakeoff_vectors "
              "(run_id, candidate, chunk_id, dims, vec, tokens) "
              "VALUES (%s, %s, %s, %s, %s, %s) "
              "ON CONFLICT (run_id, candidate, chunk_id) DO NOTHING",
              (run_id, candidate, int(chunk_id), len(vector),
               [float(value) for value in vector], int(tokens)))
        stored += 1
    return stored


COVERAGE_SQL = ("SELECT COUNT(*), COALESCE(SUM(tokens), 0) "
                "FROM ws13_bakeoff_vectors WHERE run_id = %s AND candidate = %s")


def coverage(conn, run_id: str, candidate: str) -> tuple:
    row = read(conn, COVERAGE_SQL, (run_id, candidate))
    if not row:
        return 0, 0
    return int(row[0][0]), int(row[0][1])


VECTOR_RANK_SQL = """
SELECT b.chunk_id
  FROM ws13_bakeoff_vectors b
 WHERE b.run_id = %s AND b.candidate = %s AND b.dims = %s
 ORDER BY b.vec::vector <=> %s::vector, b.chunk_id
 LIMIT %s
"""


def vector_literal(values, dims: int) -> str:
    """pgvector text literal, validated at `dims`.

    NOT ws13_query_lambda.vector_literal: that one hard-requires VECTOR_DIMS =
    1024 because Titan is the production vector, and it would raise on every
    1536-d Cohere and Qwen vector this harness exists to compare. The checks it
    makes are the ones worth keeping, so they are kept and made parametric --
    a non-finite or zero-norm vector is refused rather than stored, because a
    zero vector sits at a constant cosine distance from every query and would
    depress a candidate's ranking for a reason that is not the model's.
    """
    values = list(values)
    if len(values) != int(dims):
        raise BakeoffError(f"expected {dims} floats, got {len(values)}")
    floats = [float(value) for value in values]
    if any(math.isnan(value) or math.isinf(value) for value in floats):
        raise BakeoffError("vector contains a non-finite value")
    if math.sqrt(sum(value * value for value in floats)) <= 0.0:
        raise BakeoffError("vector has zero norm; cosine is undefined")
    # json.dumps is the encoding ws13_worker.py already uses to write the
    # VECTOR columns, so the parser on the other side is the proven one.
    return json.dumps(floats)


def rank_vector(conn, run_id: str, candidate: str, vector, limit: int) -> list:
    """Exact cosine over the sample. No ANN index and none wanted.

    20,000 rows is a sequential scan measured in milliseconds, and an ANN index
    would put recall loss from the index between the models being compared --
    which is the one variable a model bake-off must not carry.
    """
    literal = vector_literal(vector, CANDIDATE_DIMS[candidate])
    rows = read(conn, VECTOR_RANK_SQL,
                (run_id, candidate, CANDIDATE_DIMS[candidate], literal,
                 int(limit)))
    return [int(cid) for (cid,) in rows]


LEXICAL_RANK_SQL_TEMPLATE = """
SELECT c.id
  FROM ws13_bakeoff_sample s
  JOIN ws13_chunks c ON c.id = s.chunk_id
 WHERE s.run_id = %s AND {match}
 ORDER BY {rank} DESC, c.id
 LIMIT %s
"""


def lexical_rank_sql() -> str:
    return LEXICAL_RANK_SQL_TEMPLATE.format(match=LEXICAL_MATCH_SQL,
                                            rank=LEXICAL_RANK_SQL)


def rank_lexical(conn, run_id: str, query: str, limit: int) -> list:
    """The shipping keyword arm, bounded to the sample.

    Identical for every candidate by construction, which is the point: the only
    thing that differs between two candidates' fused rankings is the vector arm.
    """
    rows = read(conn, lexical_rank_sql(), (run_id, query, query, int(limit)))
    return [int(cid) for (cid,) in rows]


# ---------------------------------------------------------------------------
# Candidates.

class TitanCandidate:
    """The baseline. Its sample vectors are copied, only queries are embedded."""

    name = "titan"
    dims = CANDIDATE_DIMS["titan"]
    embeds_sample = False

    def __init__(self, client=None):
        self.client = client

    def embed_query(self, text: str) -> tuple:
        body = json.dumps({"inputText": text[:TEXT_LIMIT["titan"]],
                           "dimensions": self.dims, "normalize": True})
        response = self.client.invoke_model(modelId=TITAN_MODEL, body=body)
        payload = json.loads(response["body"].read())
        tokens = int(payload.get("inputTextTokenCount")
                     or estimate_tokens(text))
        return payload["embedding"], tokens

    def embed_documents(self, texts) -> tuple:  # pragma: no cover - unused
        raise BakeoffError(
            "titan sample vectors are copied from ws13_chunks.titan_embedding, "
            "which is complete; re-embedding them would spend quota to "
            "reproduce vectors the account has already bought")


class CohereCandidate:
    """Bedrock us.cohere.embed-v4:0, 1536-d, batched like the backfill.

    input_type is asymmetric on purpose: 'search_document' for corpus text and
    'search_query' for questions is how the model is trained to be used, and
    embedding queries as documents would understate Cohere -- losing the
    bake-off for a reason that is the harness's fault, not the model's.
    """

    name = "cohere"
    dims = CANDIDATE_DIMS["cohere"]
    embeds_sample = True
    batch = COHERE_BATCH

    def __init__(self, client=None):
        self.client = client

    def _invoke(self, texts, input_type: str) -> list:
        body = json.dumps({"texts": list(texts), "input_type": input_type,
                           "embedding_types": ["float"], "truncate": "END"})
        response = self.client.invoke_model(modelId=COHERE_MODEL, body=body)
        vectors = json.loads(response["body"].read())["embeddings"]["float"]
        if len(vectors) != len(texts):
            raise BakeoffError(
                f"cohere returned {len(vectors)} vectors for {len(texts)} "
                f"texts; a misaligned batch would attach one chunk's vector to "
                f"another chunk's id and silently corrupt every metric")
        return vectors

    def embed_documents(self, texts) -> tuple:
        """(vectors, tokens-per-text). Per text, not a batch total, so a stored
        row carries its own spend and the report can divide by real chunks."""
        clipped = [text[:TEXT_LIMIT["cohere"]] for text in texts]
        tokens = [estimate_tokens(text) for text in clipped]
        return self._invoke(clipped, "search_document"), tokens

    def embed_query(self, text: str) -> tuple:
        clipped = text[:TEXT_LIMIT["cohere"]]
        return self._invoke([clipped], "search_query")[0], estimate_tokens(clipped)


class QwenCandidate:
    """Qwen3-Embedding-8B over local TEI, per pipelines/ws13_qwen_overlay.py.

    Same contract as the overlay: POST /embed, Matryoshka truncation to 1536,
    L2 renormalisation, and NaN/None components sanitised rather than crashing
    (fp16 overflow surfaces that way). A fully poisoned vector is dropped here
    instead of stored -- a zero vector would rank at a constant distance from
    every query and quietly depress the candidate's recall.
    """

    name = "qwen"
    dims = CANDIDATE_DIMS["qwen"]
    embeds_sample = True
    batch = QWEN_BATCH

    def __init__(self, endpoint=None, opener=None):
        self.endpoint = endpoint or os.environ.get("WS13_TEI_URL",
                                                   "http://127.0.0.1:8080")
        self.opener = opener or self._post

    def _post(self, payload: dict) -> list:  # pragma: no cover - needs a GPU
        request = urllib.request.Request(
            f"{self.endpoint}/embed", method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode())
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())

    def _embed(self, texts) -> list:
        raw = self.opener({"inputs": list(texts), "truncate": True})
        out = []
        for vector in raw:
            values = [value if isinstance(value, (int, float)) and value == value
                      else 0.0 for value in (vector or [])[:self.dims]]
            norm = math.sqrt(sum(value * value for value in values))
            out.append([value / norm for value in values] if norm > 0 else None)
        return out

    def embed_documents(self, texts) -> tuple:
        clipped = [text[:TEXT_LIMIT["qwen"]] for text in texts]
        tokens = [estimate_tokens(text) for text in clipped]
        return self._embed(clipped), tokens

    def embed_query(self, text: str) -> tuple:
        clipped = text[:TEXT_LIMIT["qwen"]]
        vectors = self._embed([clipped])
        if not vectors or vectors[0] is None:
            raise BakeoffError(
                f"qwen returned an unusable vector for query {text[:60]!r}")
        return vectors[0], estimate_tokens(clipped)


def build_candidate(name: str, clients=None):
    clients = clients or {}
    if name in clients:
        return clients[name]
    if name in ("titan", "cohere"):
        if boto3 is None:
            raise BakeoffError(
                f"candidate {name!r} needs boto3 and it is not importable; "
                f"run with --dry-run or install the SDK")
        client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        return TitanCandidate(client) if name == "titan" else CohereCandidate(client)
    if name == "qwen":
        return QwenCandidate()
    raise BakeoffError(f"unknown candidate {name!r}; known: {list(CANDIDATES)}")


def embed_sample(conn, run_id: str, candidate, budget: TokenBudget) -> dict:
    """Fill one candidate's vectors for the sample, inside its token ceiling.

    Returns coverage and spend whether it finished or not. A candidate stopped
    by its budget is NOT an error: it is a candidate with partial coverage, and
    verdict() excludes it by name rather than comparing it on half a sample.
    """
    started = time.time()
    if not candidate.embeds_sample:
        copied = copy_titan(conn, run_id)
        embedded, tokens = coverage(conn, run_id, candidate.name)
        return {"embedded": embedded, "copied": copied, "tokens": tokens,
                "seconds": round(time.time() - started, 3),
                "budget_exhausted": False, "failed_batches": 0}
    pending = pending_chunks(conn, run_id, candidate.name)
    failed = 0
    exhausted = False
    batch_size = getattr(candidate, "batch", 32)
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        texts = [text or "" for _cid, text in batch]
        try:
            vectors, tokens = candidate.embed_documents(texts)
        except Exception as exc:
            failed += 1
            log(f"{candidate.name}: batch at offset {start} failed: "
                f"{type(exc).__name__}: {exc}")
            continue
        try:
            # Charged after the call returns, never before it: quota reserved
            # for a request that failed is quota nothing ever spent.
            budget.charge(sum(tokens))
        except BudgetExhausted as exc:
            log(f"{candidate.name}: {exc}")
            exhausted = True
            break
        usable = [(cid, vector, cost)
                  for (cid, _text), vector, cost in zip(batch, vectors, tokens)
                  if vector is not None]
        store_vectors(conn, run_id, candidate.name, usable)
    embedded, tokens_stored = coverage(conn, run_id, candidate.name)
    # tokens_stored is the sum over ALL sessions of this run, which is what a
    # resumed run costs in total; budget.spent covers this process only.
    return {"embedded": embedded, "copied": 0, "tokens": tokens_stored,
            "seconds": round(time.time() - started, 3),
            "budget_exhausted": exhausted, "failed_batches": failed}


def score_candidate(conn, run_id: str, candidate, queries, page_of,
                    budget: TokenBudget, limit: int = 10) -> dict:
    """Fuse and score one candidate over every query.

    The lexical arm is computed per query and shared, so two candidates differ
    only in their vector arm -- and the fused list is what the product would
    actually return, not a vector-only ranking no user would ever see.
    """
    rankings: dict = {}
    per_query = []
    over_fetch = max(limit, 200)
    for query in queries:
        lexical = rank_lexical(conn, run_id, query["text"], over_fetch)
        try:
            vector, tokens = candidate.embed_query(query["text"])
            budget.charge(tokens)
            vector_ids = rank_vector(conn, run_id, candidate.name, vector,
                                     over_fetch)
        except BudgetExhausted as exc:
            log(f"{candidate.name}: query embedding stopped: {exc}")
            break
        except Exception as exc:
            log(f"{candidate.name}: query {query['id']!r} failed: "
                f"{type(exc).__name__}: {exc}")
            continue
        fused = fuse(lexical, vector_ids, over_fetch)
        rankings[query["id"]] = fused
        if query.get("answers"):
            per_query.append(dict(query_scores(fused, query["answers"], page_of),
                                  query_id=query["id"]))
    return {"rankings": rankings, "per_query": per_query,
            "metrics": aggregate_scores(per_query)}


# ---------------------------------------------------------------------------
# Report assembly.

def build_queries(items, extra=()) -> list:
    """Labelled queries from the fixture plus unlabelled probes.

    Unlabelled queries are not filler. Ground truth exists for 1 item of a
    target 25, so without them the only measurable difference between two
    candidates would rest on a single question; rank agreement over unlabelled
    probes at least shows whether the candidates differ at all.
    """
    queries = []
    for item in items:
        queries.append({"id": item["id"], "text": item["question"],
                        "answers": answer_keys(item), "labelled": True})
    for index, text in enumerate(extra, 1):
        text = str(text).strip()
        if text:
            queries.append({"id": f"unlabelled-{index:03d}", "text": text,
                            "answers": None, "labelled": False})
    return queries


def agreement_report(rankings_by_candidate, queries,
                     k: int = AGREEMENT_K) -> list:
    """Pairwise top-k agreement on the queries with no answer key."""
    out = []
    names = sorted(rankings_by_candidate)
    unlabelled = [query["id"] for query in queries if not query.get("answers")]
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            overlaps, shifts, counted = [], [], 0
            for query_id in unlabelled:
                left = rankings_by_candidate[first].get(query_id)
                right = rankings_by_candidate[second].get(query_id)
                if left is None or right is None:
                    continue
                measure = rank_agreement(left, right, k)
                overlaps.append(measure["overlap_at_k"])
                if measure["mean_displacement"] is not None:
                    shifts.append(measure["mean_displacement"])
                counted += 1
            out.append({
                "a": first, "b": second, "queries": counted, "k": k,
                "mean_overlap_at_k": (round(sum(overlaps) / len(overlaps), 4)
                                      if overlaps else None),
                "mean_displacement": (round(sum(shifts) / len(shifts), 3)
                                      if shifts else None),
            })
    return out


def offline_projection(sample_size: int, candidates, prices) -> dict:
    """What a run would cost, computed without a database.

    So --dry-run is useful on a laptop. Labelled a projection, not a
    stratification: the real allocation needs the corpus counts, and stating
    otherwise would be exactly the unreproducible confidence this harness is
    supposed to replace.
    """
    tokens = int(sample_size) * MEAN_TOKENS_PER_CHUNK
    rows = {}
    for name in candidates:
        spend = 0 if name == BASELINE else tokens
        entry = {
            "sample_tokens": spend,
            "usd": cost_usd(name, spend, 0.0, prices),
            "note": ("titan_embedding is complete, so the baseline's sample "
                     "costs 0 tokens and $0" if name == BASELINE else None),
        }
        if name == "cohere":
            entry["days_of_cohere_daily_cap"] = round(
                spend / COHERE_DAILY_TOKEN_CAP, 2)
            entry["full_fill_days_of_cap"] = round(
                COHERE_NULL_CHUNKS * MEAN_TOKENS_PER_CHUNK
                / COHERE_DAILY_TOKEN_CAP, 1)
        if name == "qwen":
            entry["note"] = ("billed per GPU hour, not per token; this account "
                             "has never run a GPU instance and its quota "
                             "allows exactly one g5.2xlarge")
        rows[name] = entry
    return {
        "basis": (f"{MEAN_TOKENS_PER_CHUNK} tokens per chunk (len(text)//3 "
                  f"over 3,000-character chunks), the estimator "
                  f"ws13_embed_backfill bills against"),
        "sample_fraction_of_corpus": round(int(sample_size) / CORPUS_CHUNKS, 5),
        "candidates": rows,
    }


def parse_prices(pairs) -> dict:
    prices = dict(PRICE_PER_MTOK)
    for item in pairs or ():
        if "=" not in item:
            raise BakeoffError(f"--price needs NAME=USD_PER_MTOK, got {item!r}")
        name, value = item.split("=", 1)
        prices[name.strip()] = float(value)
    return prices


def parse_candidates(value: str) -> list:
    names = [name.strip() for name in str(value).split(",") if name.strip()]
    unknown = [name for name in names if name not in CANDIDATES]
    if unknown:
        raise BakeoffError(f"unknown candidate(s) {unknown}; known: "
                           f"{list(CANDIDATES)}")
    if BASELINE not in names:
        # A delta against nothing is not a measurement, and titan is the
        # column the product reads. It is always in the run.
        names.insert(0, BASELINE)
    ordered = [name for name in CANDIDATES if name in names]
    return ordered


def load_queries_file(path) -> list:
    if not path:
        return []
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(payload, dict):
        payload = payload.get("queries", [])
    return [item if isinstance(item, str) else item.get("text", "")
            for item in payload]


def run_bakeoff(conn, args, clients=None) -> dict:
    """One bake-off, end to end, against an open connection."""
    items = load_known_items(args.known_items)
    shas = mandatory_shas(items)
    rule = sampling_rule(args.seed, args.sample_size, shas, args.min_per_stratum)
    run_id = args.run_id or run_id_for(rule)
    candidates = parse_candidates(args.candidates)
    prices = parse_prices(args.price)
    started = time.time()
    report = {
        "schema_version": 1,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": run_id,
        "dry_run": bool(args.dry_run),
        "seed": args.seed,
        "candidates": candidates,
        "sampling_rule": rule,
        "query_lambda": str(QUERY_LAMBDA),
        "fusion": {"function": "rrf_fuse", "rrf_k": RRF_K, "arms": list(ARMS),
                   "source": str(QUERY_LAMBDA)},
        "pricing": {"per_million_tokens": prices, "gpu_hourly_usd":
                    dict(GPU_HOURLY_USD), "source": PRICE_SOURCE,
                    "recorded": PRICE_RECORDED},
        "token_budget_per_candidate": int(args.token_budget),
    }

    populations = stratum_populations(conn)
    mandatory = fetch_mandatory(conn, shas)
    quota = allocate(populations, max(0, args.sample_size - len(mandatory)),
                     args.min_per_stratum)
    pools = {}
    for stratum, allowance in sorted(quota.items()):
        if allowance <= 0:
            continue
        # Over-fetch by the mandatory count so removing mandatory rows from a
        # stratum cannot leave it short of its quota.
        pools[stratum] = fetch_pool(conn, stratum, args.seed,
                                    allowance + len(mandatory))
    sample = build_sample(mandatory, pools, args.sample_size, args.seed,
                          args.min_per_stratum, quota=quota,
                          populations=populations)
    report["sample"] = {key: value for key, value in sample.items()
                        if key != "chunks"}

    if args.dry_run:
        report["projection"] = offline_projection(sample["total"], candidates,
                                                  prices)
        report["verdict"] = verdict({}, 0, incomplete=candidates)
        report["note"] = ("dry run: the sample was planned and priced, nothing "
                          "was embedded and nothing was written. Re-run with "
                          "--execute to spend.")
        report["seconds"] = round(time.time() - started, 3)
        return report

    ensure_schema(conn)
    assert_resumable(conn, run_id, rule)
    persist_run(conn, run_id, rule, candidates)
    persist_sample(conn, run_id, sample["chunks"])
    stored = load_sample(conn, run_id)
    page_of = {row["chunk_id"]: (row["sha256"], row["page"]) for row in stored}
    queries = build_queries(items, load_queries_file(args.queries))

    results, rankings, incomplete = {}, {}, []
    for name in candidates:
        candidate = build_candidate(name, clients)
        budget = TokenBudget(args.token_budget)
        spend = embed_sample(conn, run_id, candidate, budget)
        # Split the two spends rather than adding budget.spent to the stored
        # total: on a resumed run they overlap, and reporting their sum would
        # bill this session for tokens an earlier one already paid.
        embed_spent = budget.spent
        scored = score_candidate(conn, run_id, candidate, queries, page_of,
                                 budget, args.limit)
        query_tokens = budget.spent - embed_spent
        rankings[name] = scored["rankings"]
        complete = (spend["embedded"] >= len(stored)
                    and not spend["budget_exhausted"])
        if not complete:
            incomplete.append(name)
        results[name] = {
            "dims": CANDIDATE_DIMS[name],
            "sample_covered": spend["embedded"],
            "sample_total": len(stored),
            "complete": complete,
            "sample_tokens": spend["tokens"],
            "query_tokens": query_tokens,
            "tokens": spend["tokens"] + query_tokens,
            "session_tokens": budget.spent,
            "budget_exhausted": spend["budget_exhausted"],
            "failed_batches": spend["failed_batches"],
            "seconds": spend["seconds"],
            "usd": cost_usd(name, spend["tokens"] + query_tokens,
                            spend["seconds"], prices),
            "metrics": scored["metrics"],
            "per_query": scored["per_query"],
            "full_fill_projection": project_full_fill(
                name, spend["tokens"], max(1, spend["embedded"]),
                spend["seconds"], prices),
        }

    deltas, labelled = compare_to_baseline(results)

    report["results"] = results
    report["deltas"] = deltas
    report["agreement"] = agreement_report(rankings, queries)
    report["verdict"] = verdict(deltas, labelled, incomplete)
    report["seconds"] = round(time.time() - started, 3)
    return report


def connect(dsn):  # pragma: no cover - needs a real database
    if psycopg is None:
        raise BakeoffError(
            "psycopg is not importable, so no database work can run; "
            "--dry-run still plans and prices the sample")
    return psycopg.connect(dsn, autocommit=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", default=os.environ.get("WS13_DB_DSN"),
                        help="libpq URI (default $WS13_DB_DSN)")
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES),
                        help=("comma separated: titan,cohere,qwen. titan is "
                              "always included as the baseline. qwen is off by "
                              "default because it needs the single g5.2xlarge "
                              "this account's quota allows and no GPU instance "
                              "has ever run here"))
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help="target chunks in the bake-off sample")
    parser.add_argument("--seed", default=DEFAULT_SEED,
                        help="sampling seed; the same seed rebuilds the sample")
    parser.add_argument("--min-per-stratum", type=int, default=MIN_PER_STRATUM,
                        help="floor per stratum so degraded OCR is represented")
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET,
                        help=("hard per-candidate token ceiling; a candidate "
                              "that hits it is reported partial and excluded "
                              "from the verdict"))
    parser.add_argument("--limit", type=int, default=10,
                        help="fused hits scored per query")
    parser.add_argument("--queries",
                        help="JSON list or newline file of unlabelled probes")
    parser.add_argument("--known-items", default=str(KNOWN_ITEMS_PATH),
                        help="known-item fixture supplying the ground truth")
    parser.add_argument("--run-id",
                        help="resume a specific run; default derives from seed")
    parser.add_argument("--price", action="append", default=[],
                        metavar="NAME=USD", help="override USD per million tokens")
    parser.add_argument("--report", help="write the JSON report here as well")
    parser.add_argument("--execute", action="store_true",
                        help="actually embed and write; default is a dry run")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:  # pragma: no cover - CLI wiring
    args = build_parser().parse_args(argv)
    args.dry_run = not args.execute
    try:
        if not args.dsn:
            if not args.dry_run:
                raise BakeoffError("--dsn (or $WS13_DB_DSN) is required to run")
            report = {
                "schema_version": 1,
                "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
                "dry_run": True,
                "sampling_rule": sampling_rule(
                    args.seed, args.sample_size,
                    mandatory_shas(load_known_items(args.known_items)),
                    args.min_per_stratum),
                "projection": offline_projection(
                    args.sample_size, parse_candidates(args.candidates),
                    parse_prices(args.price)),
                "verdict": verdict({}, 0,
                                   incomplete=parse_candidates(args.candidates)),
                "note": ("no --dsn: this is the offline projection, not a "
                         "measured stratification"),
            }
        else:
            conn = connect(args.dsn)
            try:
                report = run_bakeoff(conn, args)
            finally:
                conn.close()
    except BakeoffError as exc:
        print(f"bake-off refused: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, default=str)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


__all__ = ["BakeoffError", "BakeoffSafetyError", "BudgetExhausted",
           "TokenBudget", "RRF_K", "rrf_fuse", "fuse", "quality_band",
           "stratum_key", "draw_key", "draw", "allocate", "build_sample",
           "sampling_rule", "rule_fingerprint", "run_id_for",
           "assert_write_allowed", "write_target", "is_mutating",
           "assert_lexical_contract", "estimate_tokens", "cost_usd",
           "vector_literal", "schema_statements",
           "project_full_fill", "query_scores", "aggregate_scores",
           "rank_agreement", "paired_delta", "compare_to_baseline", "verdict",
           "build_queries",
           "agreement_report", "offline_projection", "parse_candidates",
           "parse_prices", "embed_sample", "score_candidate", "run_bakeoff",
           "load_run", "assert_resumable",
           "TitanCandidate", "CohereCandidate", "QwenCandidate", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
