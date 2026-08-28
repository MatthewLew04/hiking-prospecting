"""WS13 in-VPC retrieval: hybrid lexical + vector search over 852,027 chunks.

Why this is its own VPC-attached function instead of new code inside the ASK
Lambda: the account has ZERO NAT gateways and ZERO VPC endpoints. The three DB
subnets look public and the EC2 worker fleet does reach Bedrock from them, but
only because those instances get public IPs — a Lambda VPC ENI never does.
VPC-attaching the existing ASK function would therefore strand
bedrock.converse, Cognito, and its 795 MB spatial SQLite download, and it would
fail as a 30-second hang that reads exactly like a security-group problem. So
this function talks ONLY to Postgres over the local VPC route and needs no
egress at all: $0, against $32.85/mo for a NAT gateway or $65.70/mo for the
interface endpoints that would otherwise be needed.

The direct consequence: it CANNOT call Bedrock, so it never embeds the query
itself. The caller — the non-VPC ASK function, which has egress — embeds the
query with amazon.titan-embed-text-v2:0 at dimensions=1024, normalize=true and
passes the 1024 floats in as `query_vector`. A vector arm asked for without a
query_vector is reported disabled with a reason; it is never silently skipped.

The measurements this is built against:
  - titan_embedding vector(1024) is unit-norm (0.9999996-1.0000005) with 0
    NULLs, so cosine is the correct operator and the arm can cover the corpus.
  - tsv is 100% populated and GIN-indexed, so full-corpus keyword retrieval
    works TODAY with no vector dependency. That is why the lexical arm ships
    first and the vector arm ships behind WS13_VECTOR_ARM, which
    infra/ws13_retrieval.yaml sets to 'false' on every deploy.
  - halfvec(1024) recall@10 measured 100% over 6 probes, maximum distance
    delta 1.79e-05, so the ANN stage runs on halfvec and the distance actually
    reported is recomputed on the exact fp32 column.
  - Fusion is Reciprocal Rank Fusion (k=60), NOT the 0.75*vector +
    0.25*lexical blend in infra/document_tools.py. That blend scores a
    vector-less row at -1.0, which pushes a perfect keyword match below every
    embedded chunk, and a weighted blend is the wrong shape for OCR-noisy text
    where the lexical arm is often the better signal.

This module is the single source of truth for the halfvec index name,
expression, and ORDER BY: HALFVEC_EXPR, INDEX_NAME, CREATE_INDEX_SQL,
ANALYZE_SQL, ORDER_BY_SQL, EXPLAIN_SQL, EXPLAIN_ANALYZE_SQL. Other modules
import them; nobody re-types them. An expression index is only used when the
query repeats the expression byte-identically and the opclass matches the
operator, and halfvec_cosine_ops works with <=> only. Getting that wrong is
not an error — it is a SILENT sequential scan over 852,027 rows that blows the
30 s API Gateway deadline.

The two EXPLAINs are split because they do two different jobs and running the
wrong one against production IS the failure they exist to catch: EXPLAIN_SQL
is a plain EXPLAIN that only costs the statement, so a gate may ask "would
this use the index?" even when the index is missing and the answer is a
852,027-row Seq Scan; EXPLAIN_ANALYZE_SQL adds ANALYZE and BUFFERS, which
EXECUTE the statement, and belongs only to the operator's deliberate --measure
run. explain_ann_sql() builds either one for the FILTERED shape a real request
issues, which is the shape that actually loses the index — see filter_sql().

Besides 'search' and 'ping' there is a third op, 'documents': the DISTINCT
documents a filter set (in practice one mine) attaches, paged, each carrying
the same rights block a search citation does. It is a query against
ws13_documents and not a de-duplicated search, because the number of distinct
documents behind a fused hit list is a property of over_fetch rather than of
the mine — see documents_count_sql(). ASK routes docs_for at it so that
"click a mine, see its documents" and "ask a question" stop disagreeing about
what the corpus holds.

Everything except the connection itself is a free function over plain data, so
the SQL builders, the fusion, the rights/citation resolver, and the excerpt
window are unit-testable with no AWS and no database.

Env:
  WS13_DB_DSN              libpq URI for the SELECT-only ws13_reader role,
                           injected by CloudFormation from the ws13/postgres
                           secret (the secret never touches a laptop).
  WS13_VECTOR_ARM          operator kill switch for the vector arm.
                           infra/ws13_retrieval.yaml ships it 'false'; see
                           vector_arm_enabled() for why the in-code fallback
                           is not what keeps a missing index safe.
  WS13_STATEMENT_TIMEOUT_MS  ceiling for ONE statement (default 20000). It is
                           not the request budget: a hybrid search issues up
                           to five statements, so the sum is bounded instead
                           by the Lambda deadline — see request_deadline().
  WS13_REQUIRE_INDEX       'false' to run the vector arm even when the HNSW
                           index is absent (default 'true' — refuse instead).
  WS13_ASSERT_PLAN         'true' to EXPLAIN the filtered ANN probe once per
                           filter shape and log loudly on a Seq Scan.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from typing import Any, Mapping, Sequence

try:
    import psycopg
except Exception:  # pragma: no cover - the driver ships in the deployment zip
    # Imported at module level so it can be substituted, but never required to
    # import this module: the SQL builders, the fusion, and the citation
    # resolver must stay testable on a host with no driver and no database.
    psycopg = None

LOG = logging.getLogger("ws13.query")
LOG.setLevel(logging.INFO)

# Canonical halfvec strings. Every other module imports these.
# BOTH SIDES of the operator must be halfvec. Casting only the column fails
# the same silent way: no error, just a sequential scan.
HALFVEC_EXPR = "titan_embedding::halfvec(1024)"
INDEX_NAME = "ws13_chunks_titan_hnsw"
CREATE_INDEX_SQL = (
    f"CREATE INDEX {INDEX_NAME} ON ws13_chunks "
    f"USING hnsw (({HALFVEC_EXPR}) halfvec_cosine_ops) "
    "WITH (m = 16, ef_construction = 100)"
)
ANALYZE_SQL = "ANALYZE ws13_chunks"
ORDER_BY_SQL = f"ORDER BY c.{HALFVEC_EXPR} <=> %s::halfvec(1024)"
# The re-rank runs on the exact fp32 column. Over-fetching 200 candidates
# costs about 800 KB and removes the quantization question entirely, so the
# vector_distance we report is never a halfvec approximation.
EXACT_DISTANCE_SQL = "c.titan_embedding <=> %s::vector(1024)"
ANN_PROBE_SQL = ("SELECT c.id AS chunk_id FROM ws13_chunks c " + ORDER_BY_SQL +
                 " LIMIT %s")
# EXPLAIN_SQL is the GATE probe: plain EXPLAIN costs the statement and never
# runs it, so asking "would this use the index?" is a planner call even when
# the answer is a sequential scan over 852,027 rows. EXPLAIN (ANALYZE) would
# EXECUTE that scan to discover it was there — a deploy preflight that takes
# minutes against production RDS and evicts shared_buffers under the live
# function. Both are unfiltered; explain_ann_sql() builds the filtered shape.
EXPLAIN_SQL = "EXPLAIN " + ANN_PROBE_SQL
# The operator's deliberate --measure path ONLY. ANALYZE executes the
# statement and BUFFERS reports the pages it touched, which is the only way to
# prove what really ran — and exactly why this must never be a gate.
EXPLAIN_ANALYZE_SQL = "EXPLAIN (ANALYZE, BUFFERS) " + ANN_PROBE_SQL

VECTOR_DIMS = 1024
RRF_K = 60
EF_SEARCH_MAX = 1000
OVER_FETCH_MAX = 2000
# A filtered ANN probe is resolved to a sha256 set when the document filter
# selects no more than this many documents (of 56,282). Above it the list is
# too large to bind and the probe keeps the semi-join, which is the degraded
# shape — see plan_filtered_probe().
FILTER_SHA_CAP = 2000
# pgvector >= 0.8 only. Bounds how far an iterative scan will chase a filtered
# match before giving up, so a filter matching almost nothing cannot turn one
# probe into a full index walk.
HNSW_MAX_SCAN_TUPLES = 40000
# No statement is ever given less than this, and the response needs room to
# serialise after the last one: 25 s Lambda Timeout minus the margin is what
# the four-or-five statements of one search share.
MIN_STATEMENT_TIMEOUT_MS = 1000
RESPONSE_MARGIN_MS = 1500
ARMS = ("lexical", "vector")
ADMISSION_CLASSES = ("originals", "licensed-copies", "research-copies")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")

CITATION_RULE = (
    "Every document claim must cite a returned citation by pasting its "
    "`markdown` verbatim: it is already either [title, p. N](source_url) or "
    "the stored-copy chip [title, p. N](doc:<sha256>#<page>), and both "
    "resolve for the reader. NEVER put viewer_key or s3_key in an answer: "
    "they are private object keys, not links anyone can open. Reproduce "
    "rights_terms with the excerpt whenever attribution_required is true — "
    "attribution travels with the text, and dropping it is what turns a "
    "considered decision into a licence violation. Never redistribute an "
    "excerpt whose non_commercial or share_alike flag is set."
)

# The LISTING's citation rule. CITATION_RULE above describes the two paged
# forms a search hit carries, and document_citation() deliberately emits
# neither: nothing in a listing was matched to a page, so its markdown is the
# page-less [title](source_url) or [title](doc:<sha256>). Shipping the search
# rule on the documents op told a model to paste a ", p. N" that the markdown
# printed beside it does not contain — an invented page number, in the one
# response whose whole contract is that it invents none.
DOCUMENT_CITATION_RULE = (
    "Every document claim must cite a returned citation by pasting its "
    "`markdown` verbatim: here it is either [title](source_url) or the "
    "stored-copy chip [title](doc:<sha256>), and both resolve for the "
    "reader. NEITHER carries a page number, because nothing in this listing "
    "was matched to one — get a page-level citation from search_documents "
    "and never write a page into one of these. NEVER put viewer_key or "
    "s3_key in an answer: they are private object keys, not links anyone can "
    "open. Reproduce rights_terms with the citation whenever "
    "attribution_required is true — attribution travels with the reference, "
    "and dropping it is what turns a considered decision into a licence "
    "violation. Never redistribute a document whose non_commercial or "
    "share_alike flag is set."
)

# Rights by admission_class. rights_basis is interpolated into the two
# non-public-domain templates because for those classes the licensor IS the
# attribution: a licensed copy whose source we cannot name is unattributable,
# so rights_for() refuses to render one rather than emitting a citation that
# claims an obligation it cannot discharge.
RIGHTS_BY_CLASS: dict[str, dict[str, Any]] = {
    "originals": {
        "rights_terms": "public domain (US federal / state survey public record)",
        "attribution_required": False,
        "non_commercial": False,
        "share_alike": False,
    },
    "licensed-copies": {
        "rights_terms": (
            "CC BY-NC-SA 4.0 - attribution required, non-commercial use only, "
            "share-alike; source: {basis}"
        ),
        "attribution_required": True,
        "non_commercial": True,
        "share_alike": True,
    },
    "research-copies": {
        "rights_terms": (
            "state-archive research copy - internal, attributed, authenticated "
            "access only; not redistributable; source: {basis}"
        ),
        "attribution_required": True,
        "non_commercial": True,
        "share_alike": False,
    },
}

# Hydration columns, declared once so the SELECT list and the row mapping can
# never drift apart.
HYDRATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("c.id", "chunk_id"), ("c.sha256", "sha256"), ("c.page", "page"),
    ("c.ordinal", "ordinal"), ("c.text", "text"),
    ("d.title", "title"), ("d.doc_class", "doc_class"), ("d.s3_key", "s3_key"),
    ("d.searchable_key", "searchable_key"), ("d.source_url", "source_url"),
    ("d.admission_class", "admission_class"),
    ("d.rights_basis", "rights_basis"), ("d.portal", "portal"),
    ("d.state", "state"), ("d.county", "county"), ("d.trs", "trs"),
    ("d.doc_date", "doc_date"), ("d.doc_type", "doc_type"),
    ("d.mine_ids", "mine_ids"), ("d.mine_names", "mine_names"),
    ("d.doc_year_min", "doc_year_min"), ("d.doc_year_max", "doc_year_max"),
)
HYDRATE_COLUMNS = tuple(name for _, name in HYDRATE_FIELDS)
# The per-mine document listing (op "documents") selects ws13_documents
# directly and never touches a chunk, so it needs its own field list. Declared
# the same way and for the same reason as HYDRATE_FIELDS: the SELECT list and
# the row mapping are generated from one tuple and cannot drift apart.
DOCUMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("d.sha256", "sha256"), ("d.title", "title"), ("d.doc_class", "doc_class"),
    ("d.s3_key", "s3_key"), ("d.searchable_key", "searchable_key"),
    ("d.source_url", "source_url"),
    ("d.admission_class", "admission_class"),
    ("d.rights_basis", "rights_basis"), ("d.portal", "portal"),
    ("d.state", "state"), ("d.county", "county"), ("d.trs", "trs"),
    ("d.doc_date", "doc_date"), ("d.doc_type", "doc_type"),
    ("d.mine_ids", "mine_ids"), ("d.mine_names", "mine_names"),
    ("d.doc_year_min", "doc_year_min"), ("d.doc_year_max", "doc_year_max"),
    ("d.pages", "pages"),
)
DOCUMENT_COLUMNS = tuple(name for _, name in DOCUMENT_FIELDS)
HYDRATE_SQL = (
    "SELECT " + ", ".join(f"{expr} AS {name}" for expr, name in HYDRATE_FIELDS) +
    " FROM ws13_chunks c "
    # LEFT JOIN, not JOIN: an inner join would silently drop a chunk whose
    # ws13_documents row is missing. A missing row instead reaches the
    # citation resolver, which fails closed on an unknown admission_class.
    "LEFT JOIN ws13_documents d ON d.sha256 = c.sha256 "
    "WHERE c.id = ANY(%s)"
)

METADATA_COLUMNS = ("portal", "state", "county", "trs", "doc_date", "doc_type",
                    "mine_ids", "mine_names", "doc_year_min", "doc_year_max")

# site/index.html:3226 hands every tool result through trimJson(result, 14000)
# before a model sees it, and trimJson pops entries off `documents` and sets
# truncated — in the BROWSER, after `returned` and `next_offset` were computed
# here. A page that does not fit is therefore published as a response stating
# a page size it no longer has, over a cursor that skips exactly the rows the
# browser dropped.
#
# The size of a listed document is dominated by rights_basis, which is
# serialised FOUR times per row: rights_basis and rights_terms on the row, and
# both again inside the citation, because rights_for() interpolates the basis
# into the terms. The first measurement of this constant used a 44-character
# basis and set the default to 6. That fixture was not what the corpus writes:
# pipelines/mine_file_harvest.py:1722 builds the CC BY-NC-SA basis as 235
# characters of literal text before a single interpolation and ~346 filled in,
# and pipelines/ws13_backfill_provenance.py copies it onto ws13_documents
# verbatim. A page of 6 such rows serialises to 20,044 characters and the
# browser keeps 4 of them while `returned` still says 6 — on the 13,013
# licensed copies, the population whose rights matter most.
#
# Re-measured on the fattest realistic row (licensed copy, 80-character title,
# both mine-id namespaces, two mine names, 64-hex s3_key and viewer_key), in
# the browser's own units — JSON.stringify, which is compact; the assertion in
# tests/test_ws13_retrieval.py uses json.dumps with its default separators and
# so reads ~2% long, which is the conservative side to be wrong on. 1,342
# characters of envelope plus 3,117 per document at a 346-character basis and
# 3,725 at 500, the longest basis any validator in this repo accepts
# (pipelines/build_doc_store.py:260). So 3 measures 10,693 and 12,517 and fits
# both. 4 does not: 16,242 at 500, and 13,810 at 346, which is inside the
# budget by 190 characters and is not a margin to size a default on.
#
# Say the limit of that plainly: ws13_documents.rights_basis is unbounded
# TEXT, 500 is a convention borrowed from the WS12 registry validator rather
# than a constraint this column has, and a longer basis would overflow 3 as
# well. The test named above is where that surfaces, and the fix there is this
# number.
# A caller asking for a bigger page gets one — but it is the caller's job to
# be somewhere that can hold it.
DOCUMENTS_LIMIT_DEFAULT = 3
DOCUMENTS_LIMIT_MAX = 50
# OFFSET makes Postgres walk every row it skips. Past this depth into a
# 56,282-row table the filter is what is wrong, not the page.
DOCUMENTS_OFFSET_MAX = 10000
# How many withheld documents are named individually in the response. The
# COUNT of them is always exact; this only bounds the sample.
DOCUMENTS_WITHHELD_SAMPLE = 5


# Settings are read lazily so a test can flip one without re-importing.

TRUE_WORDS = frozenset({"true", "1", "yes", "on", "enabled"})
FALSE_WORDS = frozenset({"false", "0", "no", "off", "disabled"})


def _flag(name: str, default: bool, safe: bool) -> bool:
    """One environment flag, with the value an UNRECOGNISED string resolves to.

    The old helper was `value.strip().lower() == "true"`, which fails OPEN for
    a flag whose safe state is on: WS13_REQUIRE_INDEX set to '1', 'yes', 'on'
    or the empty string an unrendered CloudFormation condition writes turned
    the only guard against a sequential scan over 852,027 rows OFF, silently.
    So '1'/'yes'/'on' are now read as true, 'false'/'0'/'no'/'off' as false,
    an empty or absent value as `default`, and anything else as `safe` — with
    an error in the log naming the variable, because a typo in a safety flag
    must be visible rather than merely survivable.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    LOG.error("%s is %r, which is neither true nor false; using the safe "
              "value %r. Expected one of %s.", name, raw, safe,
              sorted(TRUE_WORDS | FALSE_WORDS))
    return safe


def vector_arm_enabled() -> bool:
    """The operator kill switch for the vector arm.

    The DEPLOYED default is off: infra/ws13_retrieval.yaml always sets this
    variable from its VectorArmEnabled parameter, which defaults to 'false',
    so the function ships lexical-only exactly as intended — tsv is 100%
    populated and covers the corpus today, and there is no ANN index yet.

    The fallback here is 'true' because this flag is not what makes the arm
    safe. require_index() is: with no VALID ws13_chunks_titan_hnsw the arm
    refuses and says so, instead of falling into a silent sequential scan over
    852,027 rows. A missing environment variable therefore degrades to
    "index ws13_chunks_titan_hnsw is absent", never to a 30-second hang.

    An unrecognised value resolves to OFF: losing the vector arm costs recall
    and says so in arms.vector.reason, while running it by accident is the
    sequential scan.
    """
    return _flag("WS13_VECTOR_ARM", default=True, safe=False)


def require_index() -> bool:
    """Refuse the vector arm when the HNSW index is absent. Fails CLOSED.

    Unrecognised values resolve to True, not False: this is the guard, and a
    typo in it must not be the thing that removes it.
    """
    return _flag("WS13_REQUIRE_INDEX", default=True, safe=True)


def assert_plan_enabled() -> bool:
    return _flag("WS13_ASSERT_PLAN", default=False, safe=False)


def statement_timeout_ms() -> int:
    """Ceiling for ONE statement, floored at 1 s and capped at 25 s.

    Not the request budget: a hybrid search issues a lexical arm, an ANN
    probe, an exact re-rank and a hydrate on the same session, and each would
    otherwise get a fresh full budget while the Lambda's own 25 s deadline
    covers all of them together. request_deadline() bounds the sum.
    """
    raw = os.environ.get("WS13_STATEMENT_TIMEOUT_MS", "20000")
    try:
        value = int(str(raw).strip() or "20000")
    except ValueError:
        LOG.error("WS13_STATEMENT_TIMEOUT_MS is %r, which is not a number; "
                  "using 20000 ms", raw)
        value = 20000
    return max(MIN_STATEMENT_TIMEOUT_MS, min(value, 25000))


def request_deadline(context: Any = None) -> float:
    """perf_counter value by which the whole request must be finished.

    statement_timeout is per STATEMENT, so four 20 s statements sum to 80 s
    behind a Lambda Timeout of 25 s: a slow lexical arm followed by a slow ANN
    probe reaches the function deadline mid-query and the caller gets "Task
    timed out after 25.00 seconds" with no plan, no arm timings and nothing to
    diagnose. The Lambda context knows the real remaining time, so it is the
    budget; the margin leaves room to serialise the response. With no context
    (a laptop, a test) the single-statement ceiling stands in for it.
    """
    remaining_ms = None
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        try:
            remaining_ms = int(context.get_remaining_time_in_millis())
        except Exception as exc:      # a stub context, never fatal
            LOG.warning("could not read the Lambda deadline (%s); falling back "
                        "to WS13_STATEMENT_TIMEOUT_MS", type(exc).__name__)
    if remaining_ms is None:
        budget_ms = statement_timeout_ms()
    else:
        budget_ms = max(MIN_STATEMENT_TIMEOUT_MS,
                        remaining_ms - RESPONSE_MARGIN_MS)
    return time.perf_counter() + budget_ms / 1000.0


# Request validation.

def clamp(value: Any, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    return max(low, min(high, int(value)))


def terms_of(query: str) -> list[str]:
    return [term for term in WORD_RE.findall(query or "") if len(term) > 1][:24]


def normalize_filters(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validated filter bag. Unknown values raise rather than widening a query."""
    raw = raw or {}
    filters: dict[str, Any] = {}
    for key in ("state", "county", "mine_id", "portal", "sha256"):
        value = str(raw.get(key) or "").strip()[:256]
        if value:
            filters[key] = value
    if "sha256" in filters and not re.fullmatch(r"[0-9a-f]{64}", filters["sha256"]):
        raise ValueError("sha256 filter must be 64 lowercase hex characters")
    classes = raw.get("admission_class") or []
    if isinstance(classes, str):
        classes = [classes]
    classes = [str(value).strip() for value in classes if str(value).strip()]
    unknown = sorted({value for value in classes if value not in ADMISSION_CLASSES})
    if unknown:
        raise ValueError(f"unknown admission_class filter: {unknown}")
    if classes:
        filters["admission_class"] = sorted(set(classes))
    for key in ("year_min", "year_max"):
        if raw.get(key) not in (None, ""):
            filters[key] = int(raw[key])
    if (filters.get("year_min") is not None and filters.get("year_max") is not None
            and filters["year_min"] > filters["year_max"]):
        raise ValueError("year_min is greater than year_max")
    return filters


def normalize_request(event: Mapping[str, Any] | None) -> dict[str, Any]:
    event = event or {}
    arms = event.get("arms") or list(ARMS)
    if isinstance(arms, str):
        arms = [arms]
    arms = [str(arm).strip().lower() for arm in arms if str(arm).strip()]
    unknown = sorted({arm for arm in arms if arm not in ARMS})
    if unknown:
        raise ValueError(f"unknown arm: {unknown} (expected {list(ARMS)})")
    limit = clamp(event.get("limit"), 8, 1, 25)
    ef_search = clamp(event.get("ef_search"), 200, 1, EF_SEARCH_MAX)
    # Over-fetch never drops below the requested limit: fusing a candidate
    # list shorter than `limit` cannot fill the response.
    over_fetch = clamp(event.get("over_fetch"), 200, limit, OVER_FETCH_MAX)
    # The two were clamped independently, which let over_fetch exceed
    # ef_search: pgvector's HNSW scan yields at most ef_search tuples, so
    # {"over_fetch": 1000} against the 200 default returned ~200 rows and the
    # response reported candidates: 200 — indistinguishable from a corpus that
    # genuinely had 200. RRF would then fuse two arms over different depths.
    # Reconcile the pair here and say so in the response.
    reconciled = None
    if over_fetch > ef_search:
        raised = min(EF_SEARCH_MAX, over_fetch)
        if raised >= over_fetch:
            reconciled = (f"ef_search raised from {ef_search} to {raised} so "
                          f"the HNSW scan can yield the {over_fetch} "
                          f"candidates over_fetch asks for")
            ef_search = raised
        else:
            clamped = max(limit, raised)
            reconciled = (f"over_fetch clamped from {over_fetch} to {clamped}: "
                          f"ef_search cannot exceed {EF_SEARCH_MAX}, so deeper "
                          f"candidates could not be produced anyway")
            ef_search, over_fetch = raised, clamped
    return {
        "query": str(event.get("query") or "").strip()[:1000],
        "query_vector": event.get("query_vector"),
        "filters": normalize_filters(event.get("filters")),
        "limit": limit,
        "max_excerpt_chars": clamp(event.get("max_excerpt_chars"), 760, 120, 1000),
        "ef_search": ef_search,
        "over_fetch": over_fetch,
        "depth_reconciled": reconciled,
        "arms": arms,
    }


def normalize_documents_request(event: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validated request for the document listing op: filters, limit, offset.

    Deliberately NOT normalize_request(). That one validates arms, an
    ef_search and an over_fetch, none of which this op has: it ranks nothing
    and over-fetches nothing, and its count comes from an aggregate rather
    than from a candidate list. Sharing the normaliser would let a caller pass
    over_fetch to a listing whose total is exact and read the answer as though
    that number had bounded it.

    normalize_filters() is shared, so a filter the search op accepts is
    accepted here with the same validation and the same refusals.

    An offset past DOCUMENTS_OFFSET_MAX is clamped, and the clamp is RECORDED
    rather than applied silently: the response otherwise reports `offset` as
    the number it used, which is not the number the caller asked for, and a
    caller paging its way down cannot tell the difference between the page it
    requested and the page at the cap. documents() publishes this as
    offset_clamped_from and the note says it in words.
    """
    event = event or {}
    offset = clamp(event.get("offset"), 0, 0, DOCUMENTS_OFFSET_MAX)
    request = {
        "filters": normalize_filters(event.get("filters")),
        "limit": clamp(event.get("limit"), DOCUMENTS_LIMIT_DEFAULT, 1,
                       DOCUMENTS_LIMIT_MAX),
        "offset": offset,
    }
    if event.get("offset") not in (None, "") and int(event["offset"]) != offset:
        request["offset_clamped_from"] = int(event["offset"])
    return request


def vector_literal(values: Any) -> str:
    """pgvector text literal for a query vector, validated to 1024 finite floats.

    json.dumps is the same encoding ws13_worker.py already uses to write the
    VECTOR columns, so the input parser here is the one already proven in bulk.
    """
    if not isinstance(values, (list, tuple)):
        raise ValueError(
            f"query_vector must be a list of {VECTOR_DIMS} floats, got "
            f"{type(values).__name__}")
    if len(values) != VECTOR_DIMS:
        raise ValueError(
            f"query_vector must be {VECTOR_DIMS} floats, got {len(values)}")
    floats = [float(value) for value in values]
    if any(math.isnan(value) or math.isinf(value) for value in floats):
        raise ValueError("query_vector contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in floats))
    if norm <= 0.0:
        raise ValueError("query_vector has zero norm; cosine is undefined")
    if abs(norm - 1.0) > 0.01:
        # Not fatal — cosine normalises — but the caller was asked to embed
        # with normalize=true, so a non-unit vector means a wrong request path.
        LOG.warning("query_vector norm %.6f is not unit; caller should embed "
                    "amazon.titan-embed-text-v2:0 with normalize=true", norm)
    return json.dumps(floats)


# SQL builders. Free functions over plain data: no connection, no AWS.

def chunk_clauses(filters: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    """WHERE fragments that ws13_chunks answers on its own, alias ``c``."""
    clauses: list[str] = []
    params: list[Any] = []
    if filters.get("sha256"):
        # Chunk-level, so it uses ws13_chunks_sha directly.
        clauses.append("c.sha256 = %s")
        params.append(filters["sha256"])
    return clauses, params


def document_clauses(filters: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    """WHERE fragments against ws13_documents, alias ``d``.

    Returned unwrapped so the same predicates can either go inside the EXISTS
    of a chunk query or drive a standalone document query — plan_filtered_probe
    resolves them to a sha256 set when they are selective enough to enumerate.
    """
    doc_clauses: list[str] = []
    doc_params: list[Any] = []
    if filters.get("state"):
        doc_clauses.append("d.state = %s")
        doc_params.append(filters["state"])
    if filters.get("portal"):
        doc_clauses.append("d.portal = %s")
        doc_params.append(filters["portal"])
    if filters.get("county"):
        # county is stored two ways — 15,581 rows end in ' County', 51,685 are
        # bare — so BOTH sides normalise. The left side is byte-identical to
        # the ws13_documents_county_key index expression.
        doc_clauses.append("ws13_county_key(d.county) = ws13_county_key(%s)")
        doc_params.append(filters["county"])
    if filters.get("mine_id"):
        # Array overlap, not `%s = ANY(d.mine_ids)`: && and @> both use the
        # ws13_documents_mines GIN index, while = ANY() scans all 56,282
        # documents. && rather than @> because one front-end mine id can
        # resolve through ws13_mine_id_map to more than one corpus id, and a
        # document matching ANY of them is a document about that mine.
        mine_ids = filters["mine_id"]
        if isinstance(mine_ids, str):
            mine_ids = [mine_ids]
        doc_clauses.append("d.mine_ids && %s::text[]")
        doc_params.append([str(value) for value in mine_ids])
    if filters.get("admission_class"):
        doc_clauses.append("d.admission_class = ANY(%s)")
        doc_params.append(list(filters["admission_class"]))
    # Year predicates are an interval overlap against doc_year_min/doc_year_max
    # and use ws13_documents_years. A document with NULL year bounds is
    # EXCLUDED on purpose: doc_date is NULL for 76,681 profiled rows, and a
    # date we cannot parse is not evidence that the document falls in range.
    if filters.get("year_min") is not None:
        doc_clauses.append("d.doc_year_max >= %s")
        doc_params.append(filters["year_min"])
    if filters.get("year_max") is not None:
        doc_clauses.append("d.doc_year_min <= %s")
        doc_params.append(filters["year_max"])
    return doc_clauses, doc_params


def filter_sql(filters: Mapping[str, Any],
               sha_candidates: Sequence[str] | None = None
               ) -> tuple[list[str], list[Any]]:
    """WHERE fragments for a query whose chunk table is aliased ``c``.

    Document-level predicates go inside ONE EXISTS against ws13_documents, so
    the FROM names one relation and the ORDER BY expression stays single-table
    — which the expression index does require. It buys nothing beyond that:
    Postgres flattens EXISTS into a semi-join while planning, so the ANN stage
    IS a join, and a semi-join is exactly what can push the planner off the
    HNSW ordered path onto a Seq Scan over 852,027 chunks plus a Top-N sort.
    Claiming the EXISTS keeps the probe join-free is how that scan stayed
    invisible.

    sha_candidates replaces the whole EXISTS with `c.sha256 = ANY(%s)` when the
    caller has already resolved the document filter to a bounded sha256 set.
    That leaves a genuinely single-relation predicate the planner can serve
    from ws13_chunks_sha — which is EXACT, with no ANN truncation at all — or
    from the HNSW index, whichever it costs lower. Chunk-level predicates are
    always kept: they are cheap and they narrow both shapes.
    """
    clauses, params = chunk_clauses(filters)
    if sha_candidates is not None:
        clauses.append("c.sha256 = ANY(%s)")
        params.append([str(value) for value in sha_candidates])
        return clauses, params
    doc_clauses, doc_params = document_clauses(filters)
    if doc_clauses:
        clauses.append(
            "EXISTS (SELECT 1 FROM ws13_documents d WHERE d.sha256 = c.sha256 "
            "AND " + " AND ".join(doc_clauses) + ")")
        params.extend(doc_params)
    return clauses, params


def lexical_sql(filters: Mapping[str, Any], query: str,
                over_fetch: int) -> tuple[str, list[Any]]:
    """GIN-indexed keyword arm: websearch_to_tsquery + ts_rank_cd."""
    clauses, filter_params = filter_sql(filters)
    where = " AND ".join(["c.tsv @@ websearch_to_tsquery('english', %s)"] + clauses)
    sql = (
        "SELECT c.id AS chunk_id, "
        "ts_rank_cd(c.tsv, websearch_to_tsquery('english', %s)) AS rank "
        "FROM ws13_chunks c WHERE " + where +
        " ORDER BY rank DESC, c.id LIMIT %s"
    )
    return sql, [query, query, *filter_params, over_fetch]


def vector_ann_sql(filters: Mapping[str, Any], literal: str, over_fetch: int,
                   sha_candidates: Sequence[str] | None = None
                   ) -> tuple[str, list[Any]]:
    """halfvec ANN candidate probe. ORDER_BY_SQL is pasted, never retyped.

    This is the exact (sql, params) pair the vector arm executes, so a plan
    verifier can EXPLAIN the statement production really runs instead of a
    filter-less stand-in — see explain_ann_sql().
    """
    clauses, filter_params = filter_sql(filters, sha_candidates)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = ("SELECT c.id AS chunk_id FROM ws13_chunks c" + where + " " +
           ORDER_BY_SQL + " LIMIT %s")
    return sql, [*filter_params, literal, over_fetch]


def explain_ann_sql(filters: Mapping[str, Any], literal: str, over_fetch: int,
                    sha_candidates: Sequence[str] | None = None,
                    analyze: bool = False) -> tuple[str, list[Any]]:
    """EXPLAIN of the ANN probe THIS filter set produces, bound not pasted.

    The unfiltered EXPLAIN_SQL certifies a statement no real request issues:
    every request from ASK carries filters, filter_sql() adds a semi-join, and
    the semi-join is the shape that can lose the index. A gate that EXPLAINs
    the bare probe therefore reports "Index Scan using ws13_chunks_titan_hnsw"
    while the filtered query seq-scans 852,027 rows.

    analyze=False (plain EXPLAIN) is the only form fit for a gate: it costs the
    plan without running it, so the answer is a planner call even when it is
    "Seq Scan". analyze=True EXECUTES the statement and belongs to the
    operator's deliberate --measure run.
    """
    sql, params = vector_ann_sql(filters, literal, over_fetch, sha_candidates)
    prefix = "EXPLAIN (ANALYZE, BUFFERS) " if analyze else "EXPLAIN "
    return prefix + sql, params


def vector_rerank_sql(literal: str,
                      chunk_ids: Sequence[int]) -> tuple[str, list[Any]]:
    """Exact fp32 re-rank of the ANN candidates."""
    sql = ("SELECT c.id AS chunk_id, " + EXACT_DISTANCE_SQL + " AS distance "
           "FROM ws13_chunks c WHERE c.id = ANY(%s) ORDER BY distance, c.id")
    return sql, [literal, list(chunk_ids)]


def metadata_sql(filters: Mapping[str, Any], limit: int) -> tuple[str, list[Any]]:
    """Filter-only retrieval: no query text and no query vector."""
    clauses, filter_params = filter_sql(filters)
    if not clauses:
        raise ValueError("a metadata-only search needs at least one filter")
    sql = ("SELECT c.id AS chunk_id FROM ws13_chunks c WHERE " +
           " AND ".join(clauses) +
           " ORDER BY c.sha256, c.page, c.ordinal LIMIT %s")
    return sql, [*filter_params, limit]


def documents_where(filters: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    """WHERE fragments for a query whose ONLY relation is ws13_documents.

    filter_sql() cannot be reused here even though the predicates are the
    same ones: it spells sha256 against the chunk table (`c.sha256 = %s`) and
    wraps every document predicate in an EXISTS over ws13_documents, and both
    are meaningless when ws13_documents IS the FROM. document_clauses() is the
    half that is genuinely shared, so a filter added there — including the
    `d.mine_ids && %s::text[]` overlap, which is the only spelling that
    survives the mine-id case problem below — reaches this op and the search
    op together rather than one of them.
    """
    clauses, params = document_clauses(filters)
    if filters.get("sha256"):
        clauses.append("d.sha256 = %s")
        params.append(filters["sha256"])
    return clauses, params


def documents_count_sql(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    """COUNT of the DISTINCT documents a filter set selects. A TRUE total.

    This is the reason the op exists at all instead of de-duplicating a
    search. A chunk search over-fetches candidates, fuses two arms and returns
    `limit` hits, so the number of distinct documents behind those hits is a
    property of over_fetch and of the fusion — a mine with 130 documents and a
    mine with 13 can both produce 8 hits. ws13_documents holds exactly one row
    per sha256 (56,282 of them), so counting it under the SAME predicate the
    page uses is exact, is one indexed aggregate, and is a number that means
    what a reader will assume it means.
    """
    clauses, params = documents_where(filters)
    if not clauses:
        raise ValueError(
            "a document listing needs at least one filter: without one this "
            "would page through all 56,282 documents")
    return ("SELECT count(*) AS total FROM ws13_documents d WHERE " +
            " AND ".join(clauses)), params


def documents_sql(filters: Mapping[str, Any], limit: int,
                  offset: int) -> tuple[str, list[Any]]:
    """One bounded page of documents, in a TOTAL order.

    The ORDER BY ends in d.sha256 — the primary key — on purpose. Title is far
    from unique in this corpus (a portal that names every scan after the mine
    gives dozens of rows the same title), and LIMIT/OFFSET over a partial
    order lets equal-titled rows come back in a different arrangement for each
    statement: page 2 then repeats a document page 1 already showed and skips
    one nobody ever sees. Paging is only coherent over a total order.
    """
    clauses, params = documents_where(filters)
    if not clauses:
        raise ValueError(
            "a document listing needs at least one filter: without one this "
            "would page through all 56,282 documents")
    sql = ("SELECT " + ", ".join(f"{expr} AS {name}"
                                 for expr, name in DOCUMENT_FIELDS) +
           " FROM ws13_documents d WHERE " + " AND ".join(clauses) +
           " ORDER BY d.title NULLS LAST, d.sha256 LIMIT %s OFFSET %s")
    return sql, [*params, limit, offset]


def document_chunk_stats_sql(sha256s: Sequence[str]) -> tuple[str, list[Any]]:
    """Citable pages and embedding coverage for one bounded set of documents.

    Counted over ws13_chunks and NOT over ws13_pages: ws13_pages carries a row
    for every page the worker rendered, blank ones included (760,043 rows),
    while only a page that produced a chunk can ever come back from
    search_documents. Reporting the render count as indexed_pages would tell a
    reader a page is searchable when nothing on it was indexed at all — the
    same class of claim as citing a page_count as a matched page.

    One grouped statement over the page's own sha256 list, not a correlated
    subquery per row, so the cost is bounded by the page size rather than by
    how many documents the filter selected.
    """
    sql = ("SELECT c.sha256 AS sha256, "
           "count(DISTINCT c.page) AS indexed_pages, "
           "count(*) FILTER (WHERE c.titan_embedding IS NULL) "
           "AS unembedded_chunks "
           "FROM ws13_chunks c WHERE c.sha256 = ANY(%s) GROUP BY c.sha256")
    return sql, [[str(value) for value in sha256s]]


# Fusion.

def rrf_fuse(ranked: Mapping[str, Sequence[int]],
             k: int = RRF_K) -> list[tuple[int, float, dict[str, int]]]:
    """Reciprocal Rank Fusion over {arm: [chunk_id, ...]} in rank order.

    score(d) = sum over arms of 1/(k + rank_in_that_arm), ranks 1-based. A row
    missing from an arm contributes nothing rather than a penalty — the defect
    in the 0.75/0.25 blend was that a vector-less row scored -1.0 and sank
    below every embedded chunk regardless of how well it matched the keywords.
    """
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for arm, chunk_ids in ranked.items():
        for position, chunk_id in enumerate(chunk_ids, 1):
            ranks.setdefault(chunk_id, {})[arm] = position
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position)
    order = sorted(scores, key=lambda chunk_id: (
        -scores[chunk_id], min(ranks[chunk_id].values()), chunk_id))
    return [(chunk_id, scores[chunk_id], ranks[chunk_id]) for chunk_id in order]


# Excerpts, rights, citations.

def excerpt(value: str, terms: Sequence[str], maximum: int) -> str:
    """Bounded, term-centred window. Never a whole page.

    Same idiom as infra/document_tools._excerpt so an excerpt reads the same
    whether it came from the SQLite index or from Postgres.
    """
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= maximum:
        return text
    lowered = text.lower()
    offsets = [lowered.find(term.lower()) for term in terms]
    offsets = [offset for offset in offsets if offset >= 0]
    center = min(offsets) if offsets else 0
    start = max(0, center - maximum // 3)
    end = min(len(text), start + maximum)
    if end - start < maximum:
        start = max(0, end - maximum)
    head = "…" if start else ""
    tail = "…" if end < len(text) else ""
    return head + text[start:end].strip() + tail


def rights_for(admission_class: Any, rights_basis: Any) -> dict[str, Any]:
    """Rights terms and obligation flags for one admission class.

    An unknown class raises, and so does a licensed or research copy with no
    rights_basis. Serving all 56,282 documents is defensible only because
    attribution travels with every excerpt, so a citation whose rights we
    cannot fully state must not be emitted at all.
    """
    key = str(admission_class or "").strip()
    template = RIGHTS_BY_CLASS.get(key)
    if template is None:
        raise RuntimeError(
            f"unknown admission_class {key!r}: refusing to emit a citation "
            f"with unknown rights (expected one of {list(ADMISSION_CLASSES)})")
    basis = str(rights_basis or "").strip() or None
    if "{basis}" in template["rights_terms"] and basis is None:
        raise RuntimeError(
            f"admission_class {key!r} has no rights_basis: its licence names "
            f"the source, so an excerpt cannot be attributed and the citation "
            f"must not be emitted")
    return {
        "rights_basis": basis,
        "rights_terms": template["rights_terms"].format(basis=basis),
        "attribution_required": template["attribution_required"],
        "non_commercial": template["non_commercial"],
        "share_alike": template["share_alike"],
    }


def citation_for(row: Mapping[str, Any]) -> dict[str, Any]:
    """Resolvable citation for one hydrated chunk row.

    viewer_key is searchable_key when present, else s3_key. searchable_key is
    NULL for all 27,294 born_digital documents and populated for all 28,988
    OCR ones, and a born-digital original already carries its own text layer —
    so the fallback is the correct viewer, not a compromise.

    viewer_key and viewer_key_kind are INTERNAL: they are private S3 object
    keys for the viewer integration to sign, never text for an answer. The
    user-visible string is `markdown`, and it is either the publisher link or
    the doc: chip site/index.html already resolves — a raw bucket key in an
    answer both leaks the layout and hands the reader something no browser can
    open.
    """
    sha256 = str(row.get("sha256") or "").strip()
    page = int(row.get("page") or 0)
    if not sha256 or page <= 0:
        raise RuntimeError(f"chunk row lacks a sha256/page anchor: {sha256!r}/{page}")
    rights = rights_for(row.get("admission_class"), row.get("rights_basis"))
    s3_key = str(row.get("s3_key") or "").strip()
    if not s3_key:
        raise RuntimeError(f"document {sha256[:12]} has no stored s3_key")
    searchable_key = str(row.get("searchable_key") or "").strip()
    doc_class = str(row.get("doc_class") or "").strip()
    if not searchable_key and doc_class == "ocr_queue":
        # Every OCR document had a searchable_key at the last measurement.
        # Serving the un-OCR'd original still resolves the page, so this is
        # loud rather than fatal.
        LOG.warning("ocr document %s has no searchable_key; serving the "
                    "original at %s", sha256[:12], s3_key)
    viewer_key = searchable_key or s3_key
    if searchable_key:
        viewer_key_kind = "searchable"
    elif doc_class == "ocr_queue":
        # The branch above has already proved this document is a scan, so
        # calling it born_digital_original would tell the viewer a raster PDF
        # carries a text layer; it would then highlight or text-search the
        # cited page and silently show the reader nothing.
        viewer_key_kind = "scanned_original_no_text_layer"
    else:
        viewer_key_kind = "born_digital_original"
    title = str(row.get("title") or "").strip() or f"untitled document {sha256[:12]}"
    source_url = str(row.get("source_url") or "").strip()
    if source_url and not source_url.lower().startswith(("http://", "https://")):
        raise RuntimeError(
            f"document {sha256[:12]} has a non-resolvable source_url; refusing "
            f"to emit it as a link")
    if source_url:
        markdown = f"[{title}, p. {page}]({source_url})"
        resolvable_via = "source_url"
    else:
        # source_url is NULL on every row until pipelines/
        # ws13_backfill_provenance.py fills all 56,282 from the WS12 manifest,
        # which the deploy gate requires before this path is enabled. Until
        # then the citation resolves against OUR stored copy — never drop the
        # hit, never invent a URL, and never print viewer_key: the previous
        # form pasted a private S3 key into the answer text. This is the chip
        # site/index.html parses today
        # (\[([^\]]+)\]\(doc:([0-9a-f]{16,64})(?:#(\d{1,5}))?), and a document
        # the browser does not know degrades to plain text there rather than
        # rendering a dead link.
        markdown = f"[{title}, p. {page}](doc:{sha256}#{page})"
        resolvable_via = "stored_copy"
    return {
        "document_title": title,
        "page": page,
        "source_url": source_url or None,
        "markdown": markdown,
        "sha256": sha256,
        "s3_key": s3_key,
        "viewer_key": viewer_key,
        "viewer_key_kind": viewer_key_kind,
        "admission_class": str(row.get("admission_class") or "").strip(),
        "rights_basis": rights["rights_basis"],
        "rights_terms": rights["rights_terms"],
        "attribution_required": rights["attribution_required"],
        "non_commercial": rights["non_commercial"],
        "share_alike": rights["share_alike"],
        "resolvable_via": resolvable_via,
    }


def document_citation(row: Mapping[str, Any]) -> dict[str, Any]:
    """Whole-document citation: the same resolver, with no page claim.

    citation_for() is the single owner of viewer_key, of the rights block and
    of resolvable_via, and it anchors on a page because a search hit always
    has one. A per-mine document listing has none — nothing here was matched
    to a page — so the row is anchored at page 1 to satisfy the resolver and
    the page is then dropped rather than published: `page` comes back None and
    the markdown is the PAGE-LESS chip form. site/index.html:3592 already
    accepts it (the `#<page>` group is optional and docChip falls back to page
    1), so the reader gets a button that opens our stored copy at the
    document's own first page instead of a page number we invented and a model
    could then cite as evidence. docs_for's own contract is that it does not
    invent matched pages; emitting "p. 1" here would break exactly that.

    admission_class is read off the ws13_documents column, which
    ws13_migrations.sql generates ALWAYS from split_part(s3_key, '/', 2) — the
    storage prefix — so the rights class on this citation still comes from
    where the bytes live and never from anything a caller supplied.

    Raises for the same reasons citation_for does (unknown admission class, a
    licensed or research copy with no rights_basis, no stored s3_key, a
    non-resolvable source_url). The caller withholds that one document and
    says so; it never emits a document whose rights it cannot state.
    """
    citation = citation_for(dict(row, page=1))
    title = citation["document_title"]
    citation["page"] = None
    if citation["resolvable_via"] == "source_url":
        citation["markdown"] = f"[{title}]({citation['source_url']})"
    else:
        citation["markdown"] = f"[{title}](doc:{citation['sha256']})"
    return citation


def build_hit(row: Mapping[str, Any], rrf_score: float,
              ranks: Mapping[str, int], distance: float | None,
              terms: Sequence[str], max_excerpt_chars: int) -> dict[str, Any]:
    metadata = {key: row.get(key) for key in METADATA_COLUMNS}
    metadata["mine_ids"] = list(row.get("mine_ids") or [])
    metadata["mine_names"] = list(row.get("mine_names") or [])
    return {
        "chunk_id": int(row["chunk_id"]),
        "sha256": str(row.get("sha256") or ""),
        "page": int(row.get("page") or 0),
        "ordinal": int(row.get("ordinal") or 0),
        "excerpt": excerpt(str(row.get("text") or ""), terms, max_excerpt_chars),
        "sources": [arm for arm in ARMS if arm in ranks],
        "ranks": {"lexical": ranks.get("lexical"), "vector": ranks.get("vector")},
        "rrf_score": float(rrf_score),
        "vector_distance": None if distance is None else float(distance),
        "citation": citation_for(row),
        "metadata": metadata,
    }


# Connection handling: the only part of this module that needs psycopg.

_CONN: Any = None
_INDEX_PRESENT = False
# Keyed by filter SHAPE, not one boolean for the container: the old marker let
# the first (unfiltered) request suppress the plan check for every filtered
# request after it, and the filtered shape is the one that loses the index.
_PLAN_ASSERTED: set[str] = set()
# None until the first filtered probe finds out whether this pgvector has
# hnsw.iterative_scan (0.8+). False means it does not and we stop asking.
_ITERATIVE_SCAN: bool | None = None
# (id(connection), statement_timeout) currently in force, so run() re-sends the
# value only when the remaining budget has actually moved. The connection is
# part of the key because the value lives on a SESSION: a second connection
# (the verifier passes its own) has never been told anything.
_APPLIED_TIMEOUT: tuple[int, int] | None = None


def _open_connection() -> Any:
    dsn = os.environ.get("WS13_DB_DSN", "").strip()
    if not dsn:
        raise RuntimeError("WS13_DB_DSN is not set")
    if psycopg is None:
        raise RuntimeError(
            "psycopg 3 is not importable; build the deployment package with "
            "tools/build_ws13_lambda_package.sh, which pins the manylinux "
            "x86_64 wheel and verifies it")
    return _prepare_session(psycopg.connect(dsn, autocommit=True))


def _prepare_session(conn: Any) -> Any:
    """Per-session setup, applied once to every connection this module opens.

    set_config is the parameterised form of SET; is_local=false makes the
    value hold for the whole session, which a warm container reuses. The
    timeout sits below the 30 s API Gateway integration deadline so a
    pathological query fails fast instead of hanging until the gateway gives
    up on it with a 504 that diagnoses nothing.
    """
    global _APPLIED_TIMEOUT
    timeout_ms = statement_timeout_ms()
    conn.execute("SELECT set_config('statement_timeout', %s, false)",
                 (str(timeout_ms),))
    _APPLIED_TIMEOUT = (id(conn), timeout_ms)
    return conn


def connection() -> Any:
    """Module-level lazy singleton, reused across warm invocations.

    One liveness check and exactly one reconnect: an RDS failover or an idle
    reap otherwise surfaces as a hard invocation error on the first request
    after the socket died.
    """
    global _CONN
    if _CONN is not None:
        try:
            _CONN.execute("SELECT 1")
            return _CONN
        except Exception as exc:
            LOG.warning("reconnecting to Postgres after %s: %s",
                        type(exc).__name__, str(exc)[:200])
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None
    # A new server session knows nothing about the last one, so the caches
    # that describe the session are cleared with it rather than living for the
    # whole cold start.
    global _INDEX_PRESENT, _PLAN_ASSERTED, _ITERATIVE_SCAN, _APPLIED_TIMEOUT
    _INDEX_PRESENT = False
    _PLAN_ASSERTED = set()
    _ITERATIVE_SCAN = None
    # Cleared HERE as well as in _prepare_session, because a caller that
    # substitutes _open_connection (every test does) never reaches
    # _prepare_session, and a statement_timeout cache carried across sessions
    # would skip the set_config the next session has not been told about.
    _APPLIED_TIMEOUT = None
    _CONN = _open_connection()
    return _CONN


def run(conn: Any, sql: str, params: Sequence[Any] = (),
        deadline: float | None = None) -> Any:
    """Execute one statement under what is LEFT of the request budget.

    statement_timeout is per statement, so five statements each got a fresh
    20 s while the function itself has 25 s for all of them together: a 15 s
    lexical arm followed by a 12 s ANN probe reached the Lambda deadline
    mid-query and the caller got "Task timed out after 25.00 seconds" with no
    plan, no arm timings and nothing to diagnose. Re-issuing the REMAINING
    budget is what makes the design's claim true — the database gives up
    first, and it says which statement it gave up on.

    The value is only sent when it actually changes, so a request that stays
    inside its budget pays for one set_config, not one per statement.
    """
    global _APPLIED_TIMEOUT
    if deadline is not None:
        remaining_ms = int((deadline - time.perf_counter()) * 1000.0)
        if remaining_ms <= MIN_STATEMENT_TIMEOUT_MS:
            raise RuntimeError(
                f"WS13 request budget exhausted: {remaining_ms} ms left, "
                f"below the {MIN_STATEMENT_TIMEOUT_MS} ms floor, before "
                f"{' '.join(sql.split())[:120]}")
        budget = min(remaining_ms, statement_timeout_ms())
        if _APPLIED_TIMEOUT != (id(conn), budget):
            conn.execute("SELECT set_config('statement_timeout', %s, false)",
                         (str(budget),))
            _APPLIED_TIMEOUT = (id(conn), budget)
    return conn.execute(sql, params)


def index_state(conn: Any, deadline: float | None = None) -> str:
    """'present', 'absent' or 'invalid' for the HNSW index.

    indisvalid/indisready, not just the pg_class row: an interrupted or failed
    CREATE INDEX leaves an INVALID index that exists, is never used by the
    planner, and would put the arm straight back into the sequential scan over
    852,027 rows that require_index() exists to refuse.

    'present' is cached; anything else is re-checked, because caching a
    negative forever leaves every warm container blind to the index the moment
    it is finally built.
    """
    global _INDEX_PRESENT
    if _INDEX_PRESENT:
        return "present"
    row = run(conn,
              "SELECT i.indisvalid AND i.indisready AS usable "
              "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
              "WHERE c.relkind = 'i' AND c.relname = %s",
              (INDEX_NAME,), deadline).fetchone()
    if row is None:
        return "absent"
    if not row[0]:
        return "invalid"
    _INDEX_PRESENT = True
    return "present"


def index_present(conn: Any, deadline: float | None = None) -> bool:
    return index_state(conn, deadline) == "present"


def mine_id_map(conn: Any, front_end_ids: Sequence[str],
                deadline: float | None = None) -> dict[str, list[str]]:
    """front-end mine ids -> corpus mine ids, through ws13_mine_id_map.

    The front end emits 'stategeo-igs-dd-1-if0126'; ws13_documents.mine_ids
    holds AZGS 'ADMM-...' codes and bare IGS codes. The namespaces do not
    intersect, so an unresolved mine filter matched 0 of 56,282 documents and
    ASK reported "the indexed documents do not answer it" for a mine whose
    documents are in the corpus.

    A missing table is an empty map, not an error: pipelines/ws13_migrate.py
    deliberately does not create ws13_mine_id_map (a later stage builds it and
    guards its grant), so the retrieval path has to work before it exists. The
    connection is autocommit, so a failed statement leaves no aborted
    transaction to recover from.
    """
    ids = [str(value) for value in front_end_ids if str(value or "").strip()]
    if not ids:
        return {}
    try:
        rows = run(conn,
                   "SELECT front_end_id, ws13_mine_id FROM ws13_mine_id_map "
                   "WHERE front_end_id = ANY(%s)", [ids], deadline).fetchall()
    except Exception as exc:
        # 42P01 undefined_table, 42703 undefined_column, 42501 insufficient
        # privilege: the bridge has not been built yet, or ws13_reader has no
        # grant on it. Those are the states this function promises to survive.
        # Anything else is a real database failure and is re-raised, because
        # swallowing it would turn an outage into silently unfiltered results.
        state = getattr(exc, "sqlstate", None)
        message = str(exc).lower()
        if state not in ("42P01", "42703", "42501") and not (
                state is None and any(token in message for token in
                                      ("does not exist", "undefined",
                                       "permission denied"))):
            raise
        LOG.warning("ws13_mine_id_map is not readable (%s %s: %s); treating "
                    "the mine-id namespace bridge as empty",
                    type(exc).__name__, state or "-", str(exc)[:200])
        return {}
    mapped: dict[str, list[str]] = {}
    for row in rows:
        corpus_id = str(row[1] or "").strip()
        if corpus_id:
            mapped.setdefault(str(row[0]), []).append(corpus_id)
    return mapped


def resolve_mine_filter(conn: Any, mine_id: str,
                        deadline: float | None = None
                        ) -> tuple[list[str], bool]:
    """(corpus mine ids to match, whether the id resolved to nothing).

    An id with no mapping row falls back to itself: the caller may already be
    passing a corpus-namespace id ('ADMM-01234' or a bare IGS code), and
    refusing it would break the path that works today. It is still reported as
    unresolved, so a zero-hit response can say WHY it is zero rather than
    asserting the corpus holds nothing about the mine.
    """
    mapped = mine_id_map(conn, [mine_id], deadline).get(mine_id) or []
    if mapped:
        return mapped, False
    return [mine_id], True


def iterative_scan_supported(conn: Any,
                             deadline: float | None = None) -> bool:
    """Whether this pgvector has hnsw.iterative_scan (0.8.0 and later).

    Asked as a VERSION question, not by trying the GUC and catching the error:
    an unrecognised dotted parameter whose prefix the extension has not marked
    reserved is accepted as a placeholder and silently does nothing, so
    set_config succeeding would NOT prove iterative scan is on — and the
    response would then suppress the truncation warning that is the whole
    reason for asking. An unparseable version resolves to False, which costs a
    warning the operator can check rather than a silence they cannot.
    """
    global _ITERATIVE_SCAN
    if _ITERATIVE_SCAN is not None:
        return _ITERATIVE_SCAN
    row = run(conn, "SELECT extversion FROM pg_extension WHERE extname = "
                    "'vector'", (), deadline).fetchone()
    version = str(row[0]) if row and row[0] is not None else ""
    match = re.match(r"(\d+)\.(\d+)", version)
    _ITERATIVE_SCAN = bool(match) and (int(match.group(1)),
                                       int(match.group(2))) >= (0, 8)
    if not _ITERATIVE_SCAN:
        LOG.warning("pgvector %r has no hnsw.iterative_scan; a filtered ANN "
                    "probe can only return what the first ef_search tuples "
                    "happen to contain", version or "unknown")
    return _ITERATIVE_SCAN


def iterative_scan_mode(conn: Any, filtered: bool,
                        deadline: float | None = None) -> str | None:
    """Set hnsw.iterative_scan for this probe; None when pgvector is too old.

    Without it an HNSW scan yields at most hnsw.ef_search tuples and the filter
    is applied AFTER that, so a selective filter returns a handful of the
    over_fetch candidates asked for and nothing tells that apart from a corpus
    with nothing better to offer. 'relaxed_order' is safe here specifically
    because the exact fp32 re-rank re-sorts every candidate afterwards, so
    approximate ordering out of the index costs nothing.

    Set on every vector probe, not once per session: a warm container serves
    filtered and unfiltered requests on the same session and must not inherit
    the previous request's mode.
    """
    global _ITERATIVE_SCAN
    if not iterative_scan_supported(conn, deadline):
        return None
    mode = "relaxed_order" if filtered else "off"
    try:
        run(conn,
            "SELECT set_config('hnsw.iterative_scan', %s, false), "
            "set_config('hnsw.max_scan_tuples', %s, false)",
            (mode, str(HNSW_MAX_SCAN_TUPLES)), deadline)
    except Exception as exc:
        # The version said 0.8+, so this is a surprise worth recording; stop
        # asking rather than failing the request over a tuning knob.
        _ITERATIVE_SCAN = False
        LOG.warning("hnsw.iterative_scan was rejected (%s: %s) despite a "
                    "pgvector version that should have it", type(exc).__name__,
                    str(exc)[:200])
        return None
    return mode


def plan_filtered_probe(conn: Any, filters: Mapping[str, Any],
                        deadline: float | None = None
                        ) -> tuple[str, list[str] | None]:
    """Pick the shape of the filtered ANN probe: (strategy, sha_candidates).

      unfiltered    no document predicate at all; the bare probe is correct.
      sha_set       the filter selects <= FILTER_SHA_CAP of 56,282 documents,
                    so bind their sha256s and drop the semi-join. The planner
                    can then use ws13_chunks_sha, which is exact.
      no_documents  the filter matches no document; the probe is skipped
                    rather than run to prove it.
      semi_join     too many documents to enumerate, so the EXISTS stays. THIS
                    IS THE DEGRADED SHAPE: the planner may cost the semi-join
                    as a Seq Scan over 852,027 chunks, and without
                    hnsw.iterative_scan the index yields at most ef_search
                    tuples before the filter is applied. search() reports it
                    in arms.vector.degraded_reason instead of presenting a
                    truncated candidate list as a complete one.

    The resolution query itself is bounded by LIMIT cap + 1 and uses the
    ws13_documents indexes the filters were written against, so finding out
    which shape applies costs one indexed lookup.
    """
    doc_clauses, doc_params = document_clauses(filters)
    if not doc_clauses:
        return "unfiltered", None
    rows = run(conn,
               "SELECT d.sha256 FROM ws13_documents d WHERE " +
               " AND ".join(doc_clauses) + " LIMIT %s",
               [*doc_params, FILTER_SHA_CAP + 1], deadline).fetchall()
    if len(rows) > FILTER_SHA_CAP:
        return "semi_join", None
    if not rows:
        return "no_documents", []
    return "sha_set", [str(row[0]) for row in rows]


def assert_plan_once(conn: Any, filters: Mapping[str, Any], literal: str,
                     over_fetch: int, sha_candidates: Sequence[str] | None,
                     deadline: float | None = None) -> str | None:
    """EXPLAIN the statement THIS request will run, once per filter shape.

    Plain EXPLAIN, never EXPLAIN (ANALYZE): ANALYZE executes the statement, so
    a guard against a 852,027-row sequential scan would run that scan to find
    out it was there — inside the request it is meant to protect.

    Returns the warning text when the plan is wrong, so the response can carry
    it; logging alone leaves the caller with no signal at all.
    """
    if not assert_plan_enabled():
        return None
    global _PLAN_ASSERTED
    if not isinstance(_PLAN_ASSERTED, set):
        # A caller (or a test) may have reset the marker to the boolean this
        # cache used to be; a stale False must not crash the plan guard.
        _PLAN_ASSERTED = set()
    shape = ",".join(sorted(key for key, value in filters.items()
                            if value not in (None, "", [])))
    key = ("sha_set|" if sha_candidates is not None else "semi_join|") + shape
    if key in _PLAN_ASSERTED:
        return None
    _PLAN_ASSERTED.add(key)
    sql, params = explain_ann_sql(filters, literal, over_fetch, sha_candidates)
    plan = "\n".join(str(row[0]) for row in
                     run(conn, sql, params, deadline).fetchall())
    if "Seq Scan" in plan or INDEX_NAME not in plan:
        LOG.error("WS13 vector arm is NOT using %s for filter shape %r — a "
                  "sequential scan over 852,027 chunks will blow the 30 s "
                  "deadline. Plan:\n%s", INDEX_NAME, key, plan)
        return (f"plan for filter shape {key!r} does not use {INDEX_NAME}: "
                f"{plan.splitlines()[0][:200] if plan else 'empty plan'}")
    LOG.info("WS13 vector arm plan uses %s for filter shape %r",
             INDEX_NAME, key)
    return None


def hydrate(conn: Any, chunk_ids: Sequence[int],
            deadline: float | None = None) -> dict[int, dict[str, Any]]:
    if not chunk_ids:
        return {}
    rows = run(conn, HYDRATE_SQL, [list(chunk_ids)], deadline).fetchall()
    hydrated = {}
    for row in rows:
        mapped = dict(zip(HYDRATE_COLUMNS, row))
        hydrated[int(mapped["chunk_id"])] = mapped
    return hydrated


# Operations.

def ping() -> dict[str, Any]:
    """B2 network probe: prove the DB subnets plus the app SG reach 5432.

    This runs before any retrieval code is exercised. No security-group change
    is needed or permitted — the data-plane SG already allows tcp/5432 from
    exactly the SG this function is attached to.
    """
    started = time.perf_counter()
    reused = _CONN is not None
    conn = connection()
    row = conn.execute("SELECT now()").fetchone()
    state = index_state(conn)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    return {
        "status": "loaded",
        "op": "ping",
        "pg_now": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
        "ms": elapsed_ms,
        "reused_connection": reused,
        "vector_arm_enabled": vector_arm_enabled(),
        "index_present": state == "present",
        # 'invalid' is its own answer: a failed CREATE INDEX leaves a row in
        # pg_class that the planner will never use.
        "index_state": state,
        "index_name": INDEX_NAME,
    }


def _vector_disabled_reason(request: Mapping[str, Any]) -> str | None:
    """Why the vector arm will not run, or None if it will."""
    if "vector" not in request["arms"]:
        return "vector arm not requested"
    if not vector_arm_enabled():
        return ("vector arm disabled: WS13_VECTOR_ARM is "
                f"{os.environ.get('WS13_VECTOR_ARM', 'true')!r}")
    if request.get("query_vector") is None:
        return ("no query_vector supplied: this function has no egress and "
                "cannot embed the query itself")
    return None


def vector_degraded_reason(strategy: str, iterative: str | None,
                           candidates: int, request: Mapping[str, Any],
                           documents: int | None = None) -> str | None:
    """Why this filtered ANN result may be a truncated slice, or None.

    pgvector's HNSW scan yields at most hnsw.ef_search tuples and the filter is
    applied AFTER that, so before 0.8 (no hnsw.iterative_scan) a filtered probe
    that comes back short is indistinguishable from a corpus with nothing
    better to offer. Saying so in the response is the whole point: the failure
    this replaces was `arms.vector.candidates: 3` with `enabled: true` and no
    hint that 197 of the 200 requested candidates were removed by the filter
    rather than never existing.

    The two filtered shapes degrade differently and the text says which. In
    the sha_set shape the planner may instead serve `c.sha256 = ANY(%s)` from
    ws13_chunks_sha, which is exact, so a short list there can equally mean the
    document set really is that small; only the plan settles it, which is what
    WS13_ASSERT_PLAN is for.
    """
    if strategy in ("unfiltered", "no_documents"):
        return None
    if iterative or candidates >= request["over_fetch"]:
        return None
    head = (f"filtered ANN returned {candidates} of the "
            f"{request['over_fetch']} candidates requested using the "
            f"{strategy} shape, and hnsw.iterative_scan is unavailable, so "
            f"the index yields at most ef_search={request['ef_search']} "
            f"tuples before the filter is applied")
    if strategy == "sha_set":
        return (head + f"; over {documents} document(s) this is either the "
                f"whole of that set (an exact ws13_chunks_sha plan) or a "
                f"truncated slice — run with WS13_ASSERT_PLAN=true to tell "
                f"them apart")
    return head + "; these candidates may be a truncated slice rather than " \
                  "the best matches"


def search(event: Mapping[str, Any],
           deadline: float | None = None) -> dict[str, Any]:
    request = normalize_request(event)
    # A copy: filters.mine_id is rewritten in place below, and the request bag
    # keeps what the caller actually asked for.
    filters = dict(request["filters"])
    query = request["query"]
    terms = terms_of(query)
    if deadline is None:
        deadline = request_deadline(None)

    lexical_on = bool(query) and "lexical" in request["arms"]
    vector_reason = _vector_disabled_reason(request)
    literal = None if vector_reason else vector_literal(request["query_vector"])

    if not lexical_on and vector_reason is not None and not filters:
        raise ValueError(
            "nothing to retrieve on: pass query text, a query_vector with the "
            "vector arm enabled, or at least one filter")

    conn = connection()

    # The mine-id namespace bridge, BEFORE any predicate is built: the front
    # end's 'stategeo-igs-dd-1-if0126' does not appear in ws13_documents.
    filter_unresolved: list[str] = []
    filter_resolution: dict[str, Any] = {}
    if filters.get("mine_id"):
        requested_mine_id = str(filters["mine_id"])
        corpus_ids, unresolved = resolve_mine_filter(conn, requested_mine_id,
                                                     deadline)
        filters["mine_id"] = corpus_ids
        filter_resolution["mine_id"] = {
            "requested": requested_mine_id,
            "resolved": corpus_ids,
            "via": "as_supplied" if unresolved else "ws13_mine_id_map",
        }
        if unresolved:
            filter_unresolved.append("mine_id")

    if vector_reason is None and require_index():
        state = index_state(conn, deadline)
        if state != "present":
            vector_reason = (
                f"index {INDEX_NAME} is {state}: refusing a sequential scan "
                f"over 852,027 chunks. Build it with CREATE_INDEX_SQL, then "
                f"{ANALYZE_SQL}.")
            literal = None
    vector_on = vector_reason is None

    lexical_ids: list[int] = []
    lexical_ms = 0.0
    if lexical_on:
        sql, params = lexical_sql(filters, query, request["over_fetch"])
        started = time.perf_counter()
        lexical_ids = [int(row[0]) for row in
                       run(conn, sql, params, deadline).fetchall()]
        lexical_ms = round((time.perf_counter() - started) * 1000.0, 1)

    vector_ids: list[int] = []
    vector_ms = 0.0
    distances: dict[int, float] = {}
    vector_arm: dict[str, Any] = {}
    if vector_on:
        started = time.perf_counter()
        # set_config is the parameterised form of SET; is_local=false makes it
        # session-scoped instead of SET LOCAL. The connection is autocommit and
        # every vector probe sets the value immediately before running, so a
        # warm container can never inherit a stale ef_search — and skipping the
        # BEGIN/COMMIT saves two round trips against the 30 s deadline.
        # pgvector's default is 40, far too tight to re-rank 200 candidates.
        conn.execute("SELECT set_config('hnsw.ef_search', %s, false)",
                     (str(request["ef_search"]),))
        strategy, sha_candidates = plan_filtered_probe(conn, filters, deadline)
        candidates: list[int] = []
        iterative = None
        plan_warning = None
        if strategy == "no_documents":
            # Nothing to probe for: the filter matches 0 of 56,282 documents.
            # Skipping is not an optimisation, it is the difference between
            # "no document matches these filters" and "the ANN found nothing".
            pass
        else:
            iterative = iterative_scan_mode(
                conn, strategy in ("sha_set", "semi_join"), deadline)
            plan_warning = assert_plan_once(conn, filters, literal,
                                            request["over_fetch"],
                                            sha_candidates, deadline)
            ann_sql, ann_params = vector_ann_sql(filters, literal,
                                                 request["over_fetch"],
                                                 sha_candidates)
            candidates = [int(row[0]) for row in
                          run(conn, ann_sql, ann_params, deadline).fetchall()]
        if candidates:
            rerank_sql, rerank_params = vector_rerank_sql(literal, candidates)
            for row in run(conn, rerank_sql, rerank_params, deadline).fetchall():
                chunk_id = int(row[0])
                vector_ids.append(chunk_id)
                # titan_embedding has 0 NULLs, so the operator cannot return
                # NULL today. Report an absent distance as absent anyway: the
                # ranking is already decided, and a crash here would throw
                # away a whole result set over one unpopulated column.
                if row[1] is not None:
                    distances[chunk_id] = float(row[1])
        vector_ms = round((time.perf_counter() - started) * 1000.0, 1)
        vector_arm = {
            "filter_strategy": strategy,
            "filter_documents": (None if sha_candidates is None
                                 else len(sha_candidates)),
            "iterative_scan": iterative,
            "over_fetch": request["over_fetch"],
            "plan_warning": plan_warning,
            "degraded_reason": vector_degraded_reason(
                strategy, iterative, len(candidates), request,
                None if sha_candidates is None else len(sha_candidates)),
        }

    ranked: dict[str, Sequence[int]] = {}
    if lexical_on:
        ranked["lexical"] = lexical_ids
    if vector_on:
        ranked["vector"] = vector_ids

    if ranked:
        fused = rrf_fuse(ranked)[:request["limit"]]
        if lexical_on and vector_on:
            retrieval_mode = "rrf_lexical_vector"
        else:
            retrieval_mode = "lexical_only" if lexical_on else "vector_only"
    else:
        sql, params = metadata_sql(filters, request["limit"])
        fused = [(int(row[0]), 0.0, {})
                 for row in run(conn, sql, params, deadline)]
        retrieval_mode = "metadata_filter"

    rows = hydrate(conn, [chunk_id for chunk_id, _, _ in fused], deadline)
    hits = []
    for chunk_id, score, ranks in fused:
        row = rows.get(chunk_id)
        if row is None:
            # Fail closed: a ranked chunk that will not hydrate is a corpus
            # inconsistency, not a hit to quietly drop.
            raise RuntimeError(
                f"chunk {chunk_id} ranked but did not hydrate from ws13_chunks")
        hits.append(build_hit(row, score, ranks, distances.get(chunk_id), terms,
                              request["max_excerpt_chars"]))

    vector_block: dict[str, Any] = {
        "candidates": len(vector_ids), "ms": vector_ms,
        "ef_search": request["ef_search"], "enabled": vector_on,
        "reason": vector_reason,
    }
    vector_block.update(vector_arm)
    response: dict[str, Any] = {
        "status": "loaded",
        "count": len(hits),
        "retrieval_mode": retrieval_mode,
        "arms": {
            "lexical": {"candidates": len(lexical_ids), "ms": lexical_ms},
            "vector": vector_block,
        },
        "hits": hits,
        "citation_rule": CITATION_RULE,
    }
    if request["depth_reconciled"]:
        response["depth_reconciled"] = request["depth_reconciled"]
    if filter_resolution:
        response["filter_resolution"] = filter_resolution
    if filter_unresolved and not hits:
        # A zero-hit answer whose filter never resolved is NOT evidence that
        # the corpus has nothing: the caller must fall back rather than tell a
        # user the indexed documents do not answer their question.
        response["filter_unresolved"] = filter_unresolved
    return response


def _documents_note(request: Mapping[str, Any], total: int, on_page: int,
                    withheld: int, truncated: bool,
                    next_offset: int | None,
                    unresolved_mine_id: str | None) -> str:
    """What this listing is, in the words a reader would otherwise assume.

    The one sentence that has to be here is that `count` is the whole set and
    `documents` is a page of it. Everything else is conditional, because a
    note that always warns about truncation trains a reader to ignore it.
    """
    offset = request["offset"]
    parts = ["count is the TOTAL number of distinct documents these filters "
             "select in the WS13 corpus; documents[] holds the page of them "
             "listed here."]
    if unresolved_mine_id is not None and total == 0:
        # The failure this exists to prevent, stated as plainly as it can be:
        # the front end emits 'stategeo-igs-dd-1-if0126' and
        # ws13_documents.mine_ids holds AZGS 'ADMM-...' codes and bare IGS
        # codes, so an id with no ws13_mine_id_map row matched nothing because
        # it was never translated. Reported as an empty document set it reads
        # to a user as "this mine has no documents" for a mine whose documents
        # are demonstrably in the corpus.
        parts.append(
            f"mine id {unresolved_mine_id!r} has no ws13_mine_id_map row, so "
            f"it was never translated into the corpus namespace and was "
            f"matched as supplied. count 0 therefore means THIS ID WAS NEVER "
            f"TRANSLATED, not that this mine has no documents — fall back to "
            f"the bounded index rather than reporting an empty document set.")
    elif truncated:
        # No "ask again with offset=N" here, deliberately: the tool contract
        # ASK exposes to a model has no offset argument, and telling it to
        # pass one invites an invented parameter. next_offset carries the
        # cursor for a caller that can actually page.
        span = (f"This is documents {offset + 1}-{offset + on_page} of "
                f"{total} — a page, not the set")
        if next_offset is None and offset + on_page > DOCUMENTS_OFFSET_MAX:
            # There is no cursor, and the sentence that used to stand here
            # promised one. offset is clamped at DOCUMENTS_OFFSET_MAX while
            # next_offset was computed unclamped, so a caller that followed
            # the cursor past the cap got the cap back, read the same page
            # again, and could have looped on it forever while every response
            # said the rest was one more request away. Past the cap the filter
            # is what has to change.
            parts.append(
                f"{span}; the rest is NOT reachable by paging — offset is "
                f"clamped at {DOCUMENTS_OFFSET_MAX} and there is no cursor "
                f"past it, so narrow the filters instead. Never present the "
                f"listed rows as the whole set.")
        elif next_offset is None:
            parts.append(
                f"{span}; there is no cursor for the rest — this page came "
                f"back empty under a total that says there is more, which is "
                f"the count and the page disagreeing rather than the end of "
                f"the set. Never present the listed rows as the whole set.")
        else:
            parts.append(
                f"{span}; next_offset carries the cursor for the rest. Never "
                f"present the listed rows as the whole set.")
    elif total and offset >= total:
        parts.append(
            f"offset {offset} is past the end of the {total} documents these "
            f"filters select; the empty page is the offset, not the corpus.")
    if request.get("offset_clamped_from") is not None:
        parts.append(
            f"offset {request['offset_clamped_from']} was clamped to "
            f"{offset}, the deepest this op pages: these are the documents "
            f"at {offset}, not the ones asked for.")
    if withheld:
        parts.append(
            f"{withheld} document(s) on this page are counted but NOT listed: "
            f"their rights could not be stated, and an excerpt or a link "
            f"served without its licence is a rights failure, not a cosmetic "
            f"gap. See withheld_documents.")
    parts.append("Nothing here has been matched to a page; use "
                 "search_documents for page-level citations.")
    return " ".join(parts)


def documents(event: Mapping[str, Any] | None,
              deadline: float | None = None) -> dict[str, Any]:
    """The DISTINCT documents a filter set attaches, with a true total.

    The browser's per-mine document list has always answered from the 3.2 MB
    SQLite slice — two documents — while search_documents went full-corpus, so
    "ask a question" and "click a mine, see its documents" disagreed about
    what exists. This is the corpus-scale answer to the second one.

    It is a document query, not a de-duplicated search: no arms, no ranking,
    no over-fetch, and the count is an aggregate over ws13_documents rather
    than the number of distinct documents a candidate list happened to touch.
    See documents_count_sql().

    The mine-id namespace bridge runs first and IDENTICALLY to search(): the
    same resolve_mine_filter(), so the same ws13_mine_id_map lookup, the same
    fall back to the id as supplied when there is no row, and the same
    `d.mine_ids && %s::text[]` overlap predicate through document_clauses().
    Two consequences are worth naming rather than assuming:

      * The overlap operator is what makes one front-end id able to resolve to
        several corpus ids. It is also the only spelling that can pick up the
        case variants pipelines/ws13_mine_id_map.py records in
        ws13_mine_id_all — mine_ids is case-sensitive and its GIN index is
        exact-match, so 'SP0145' and 'sp0145' are different values. NOTE the
        limit: mine_id_map() below still SELECTs ws13_mine_id alone, so the
        set this op overlaps against is the primary spelling only. Documents
        filed under another spelling are lost here exactly as they are lost in
        search() — this op does not fix that, and must not be described as
        though it had.
      * An id that resolved to nothing is reported as unresolved and the note
        says the id was never translated. A zero here is never evidence that a
        mine has no documents.
    """
    request = normalize_documents_request(event)
    # A copy: filters.mine_id is rewritten in place below and the request bag
    # keeps what the caller actually asked for.
    filters = dict(request["filters"])
    requested_mine_id = str(filters["mine_id"]) if filters.get("mine_id") else None
    if deadline is None:
        deadline = request_deadline(None)
    conn = connection()

    filter_resolution: dict[str, Any] = {}
    unresolved_mine_id: str | None = None
    if requested_mine_id:
        corpus_ids, unresolved = resolve_mine_filter(conn, requested_mine_id,
                                                     deadline)
        filters["mine_id"] = corpus_ids
        filter_resolution["mine_id"] = {
            "requested": requested_mine_id,
            "resolved": corpus_ids,
            "via": "as_supplied" if unresolved else "ws13_mine_id_map",
        }
        if unresolved:
            unresolved_mine_id = requested_mine_id

    count_sql, count_params = documents_count_sql(filters)
    row = run(conn, count_sql, count_params, deadline).fetchone()
    total = int((row[0] if row else 0) or 0)

    page_sql, page_params = documents_sql(filters, request["limit"],
                                          request["offset"])
    rows = [dict(zip(DOCUMENT_COLUMNS, values))
            for values in run(conn, page_sql, page_params, deadline).fetchall()]

    # Citable pages for exactly the documents on this page, in one statement.
    stats: dict[str, tuple[int, int]] = {}
    if rows:
        stats_sql, stats_params = document_chunk_stats_sql(
            [str(item.get("sha256") or "") for item in rows])
        for values in run(conn, stats_sql, stats_params, deadline).fetchall():
            stats[str(values[0])] = (int(values[1] or 0), int(values[2] or 0))

    listed: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for item in rows:
        sha256 = str(item.get("sha256") or "")
        indexed_pages, unembedded = stats.get(sha256) or (0, 0)
        try:
            citation = document_citation(item)
        except RuntimeError as exc:
            # Withheld, not dropped and not degraded. 13,013 licensed copies
            # and 32,312 research copies name their licensor in their own
            # rights terms, and rights_basis is NULL on every row until
            # pipelines/ws13_backfill_provenance.py has run — so this is a
            # reachable state, and one bad row must neither publish an
            # unattributable document nor take a whole mine's list down with
            # it. The count above still includes it, and the note says so.
            withheld.append({"sha256": sha256 or None,
                             "reason": str(exc)[:200]})
            continue
        rights = {key: citation[key] for key in
                  ("admission_class", "rights_basis", "rights_terms",
                   "attribution_required", "non_commercial", "share_alike")}
        listed.append({
            # document_id is the sha256 because that IS the document id
            # everywhere else: infra/document_tools.py notes that
            # documents.document_id is the SHA-256 of the raw original, and it
            # is what the browser's doc: chip resolves. Keeping the WS12 key
            # name is what lets this result reach the same consumers.
            "document_id": sha256,
            "sha256": sha256,
            "title": item.get("title"),
            "portal": item.get("portal"),
            "portal_id": item.get("portal"),
            # No portal_source: ws13_documents has one portal column, and
            # inventing a second value to match the WS12 row shape would be
            # fabricating provenance.
            "mine_ids": list(item.get("mine_ids") or []),
            "mine_names": list(item.get("mine_names") or []),
            "state": item.get("state"), "county": item.get("county"),
            "trs": item.get("trs"), "doc_date": item.get("doc_date"),
            "doc_type": item.get("doc_type"),
            "doc_year_min": item.get("doc_year_min"),
            "doc_year_max": item.get("doc_year_max"),
            "page_count": item.get("pages"),
            "indexed_pages": indexed_pages,
            "source_url": (str(item.get("source_url") or "").strip() or None),
            # indexed is "search_documents can return a page of this", which
            # is only true of a document that produced chunks. A processed
            # document with zero indexed pages is still attached to the mine
            # and still listed — it just cannot be cited, and saying so is the
            # difference between a gap and an invisible one.
            "indexed": indexed_pages > 0,
            # Partial embedding reads as False: the vector arm can only cover
            # a document whose chunks all carry titan_embedding.
            "embedded": indexed_pages > 0 and unembedded == 0,
            # Rights on the document row as well as inside the citation. The
            # row is what a document LIST renders and the citation is what a
            # model pastes, and attribution has to be unmissable in both — a
            # citation that loses its rights is a licence problem, not a
            # cosmetic gap.
            **rights,
            "citation": citation,
        })

    on_page = len(rows)
    truncated = request["offset"] + on_page < total
    next_offset = request["offset"] + on_page if truncated else None
    # A cursor past DOCUMENTS_OFFSET_MAX is not a cursor. normalize_documents_
    # request() clamps `offset` there and this line did not clamp what it
    # emitted, so at the cap the response handed back offset + page size,
    # which clamped straight back to the cap and returned byte-identical rows
    # with truncated still true — a caller following the cursor read the same
    # page forever. Reachable for any filter selecting more than 10,000
    # documents, which a bare `state` over a 56,282-document corpus does.
    # `truncated` stays true because more documents really do exist; there is
    # simply no cursor that reaches them, and _documents_note() says so
    # instead of promising the rest is one request away.
    if next_offset is not None and next_offset > DOCUMENTS_OFFSET_MAX:
        next_offset = None
    elif next_offset is not None and next_offset <= request["offset"]:
        # The same failure from the other end: the count and the page are two
        # statements, so a concurrent delete can leave on_page 0 under a total
        # that still says there is more. A cursor that does not advance is not
        # a cursor either.
        next_offset = None
    response: dict[str, Any] = {
        "status": "loaded",
        "op": "documents",
        "mine_id": requested_mine_id,
        # The TRUE total, which is the whole point of the op. `returned` is
        # the bounded half, and the two are separate keys so neither can be
        # mistaken for the other.
        "count": total,
        "returned": len(listed),
        "documents": listed,
        "limit": request["limit"],
        "offset": request["offset"],
        "truncated": truncated,
        "next_offset": next_offset,
        "retrieval_mode": "document_filter",
        # DOCUMENT_CITATION_RULE, not CITATION_RULE: this op's markdown
        # carries no page, and the search rule tells a reader both forms end
        # in ", p. N". See DOCUMENT_CITATION_RULE.
        "citation_rule": DOCUMENT_CITATION_RULE,
        "note": _documents_note(request, total, on_page, len(withheld),
                                truncated, next_offset,
                                unresolved_mine_id),
    }
    if request.get("offset_clamped_from") is not None:
        # `offset` above is the one this op used, not the one asked for.
        response["offset_clamped_from"] = request["offset_clamped_from"]
    # Deliberately NOT published under "hits". infra/document_tools.
    # _validate_result() walks result["hits"] and requires an http(s)
    # source_url on every citation there; source_url is NULL for most of the
    # 56,282 documents until pipelines/ws13_backfill_provenance.py has run, so
    # a document list published as hits would fail that validation outright.
    # With no "hits" key it passes — vacuously, because there is nothing for
    # it to check, not because these citations were validated by it.

    if filter_resolution:
        response["filter_resolution"] = filter_resolution
    if withheld:
        response["withheld_count"] = len(withheld)
        response["withheld_documents"] = withheld[:DOCUMENTS_WITHHELD_SAMPLE]
    if unresolved_mine_id is not None and total == 0:
        # Same marker and same rule as search(): a zero answer whose filter
        # never resolved is not evidence of absence, and ASK falls back to the
        # bounded index on it rather than reporting an empty set.
        #
        # Gated on the TOTAL, not on an empty page, and search()'s `not hits`
        # is not the same test once offsets exist: `offset=100` over 41
        # matching documents returns nothing and would have flown the marker,
        # sending ASK back to a 2-document index for a mine WS13 answers
        # perfectly well. An empty page is a page; only a zero total is a
        # miss. It is also the condition _documents_note() branches on, so
        # the marker and the sentence explaining it can never disagree.
        response["filter_unresolved"] = ["mine_id"]
    return response


def handler(event: Mapping[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event or {}
    op = str(event.get("op") or "search").strip().lower()
    if op == "ping":
        return ping()
    if op == "search":
        # The context knows how long this invocation really has left, which is
        # what the statements have to share; with no context the single-
        # statement ceiling stands in for it.
        return search(event, request_deadline(context))
    if op == "documents":
        return documents(event, request_deadline(context))
    raise ValueError(
        f"unknown op {op!r}: expected 'search', 'documents' or 'ping'")


def _dry_run(event: Mapping[str, Any]) -> dict[str, Any]:
    """Render the SQL this event would run, without touching the database.

    This is how the byte-identical ORDER BY is checked off a laptop: the
    expression index is only used when the query repeats it exactly.
    """
    request = normalize_request(event)
    plans: dict[str, Any] = {"request": {k: v for k, v in request.items()
                                         if k != "query_vector"}}
    if request["query"]:
        sql, params = lexical_sql(request["filters"], request["query"],
                                  request["over_fetch"])
        plans["lexical"] = {"sql": sql, "params": params}
    literal = "[" + ",".join(["0.0"] * VECTOR_DIMS) + "]"
    sql, params = vector_ann_sql(request["filters"], literal,
                                 request["over_fetch"])
    plans["vector_ann"] = {"sql": sql, "params_without_vector":
                           [p for p in params if p is not literal]}
    plans["vector_rerank"] = {"sql": vector_rerank_sql(literal, [1])[0]}
    # The gate probe for these filters, so an operator can paste the EXPLAIN
    # of the statement production runs rather than of the bare probe.
    plans["vector_explain"] = explain_ann_sql(
        request["filters"], literal, request["over_fetch"])[0]
    plans["vector_explain_sha_set"] = explain_ann_sql(
        request["filters"], literal, request["over_fetch"],
        sha_candidates=["<sha256>"])[0]
    plans["create_index"] = CREATE_INDEX_SQL
    plans["order_by"] = ORDER_BY_SQL
    # The document listing, when the filters can support one. Rendered from
    # the same builders the op runs, so --dry-run shows the real statements
    # rather than a description of them.
    if documents_where(request["filters"])[0]:
        sql, params = documents_sql(request["filters"],
                                    DOCUMENTS_LIMIT_DEFAULT, 0)
        plans["documents"] = {"sql": sql, "params": params}
        count_sql, count_params = documents_count_sql(request["filters"])
        plans["documents_count"] = {"sql": count_sql, "params": count_params}
    return plans


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--op", required=True,
                        choices=("ping", "search", "documents"),
                        help="operation to invoke")
    parser.add_argument("--query", default="", help="search text")
    parser.add_argument("--filter", action="append", default=[],
                        metavar="KEY=VALUE", help="filter (repeatable)")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--offset", type=int, default=0,
                        help="documents: page offset into the true total")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the SQL that would run and connect to nothing")
    args = parser.parse_args(argv)

    filters: dict[str, Any] = {}
    for item in args.filter:
        if "=" not in item:
            parser.error(f"--filter needs KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        filters[key.strip()] = value.strip()
    event = {"op": args.op, "query": args.query, "filters": filters,
             "limit": args.limit, "offset": args.offset}
    result = _dry_run(event) if args.dry_run else handler(event)
    print(json.dumps(result, indent=2, default=str))
    return 0


__all__ = ["HALFVEC_EXPR", "INDEX_NAME", "CREATE_INDEX_SQL", "ANALYZE_SQL",
           "ORDER_BY_SQL", "ANN_PROBE_SQL", "EXPLAIN_SQL",
           "EXPLAIN_ANALYZE_SQL", "RRF_K", "ADMISSION_CLASSES",
           "CITATION_RULE", "DOCUMENT_CITATION_RULE",
           "handler", "search", "documents", "ping",
           "normalize_request", "normalize_documents_request",
           "normalize_filters", "chunk_clauses", "document_clauses",
           "filter_sql", "lexical_sql", "vector_ann_sql", "explain_ann_sql",
           "vector_rerank_sql", "metadata_sql", "documents_where",
           "documents_sql", "documents_count_sql", "document_chunk_stats_sql",
           "DOCUMENT_COLUMNS", "DOCUMENTS_LIMIT_DEFAULT",
           "DOCUMENTS_LIMIT_MAX", "rrf_fuse", "excerpt",
           "rights_for", "citation_for", "document_citation", "build_hit",
           "vector_literal",
           "terms_of", "clamp", "index_state", "index_present",
           "plan_filtered_probe", "mine_id_map", "resolve_mine_filter",
           "request_deadline", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
