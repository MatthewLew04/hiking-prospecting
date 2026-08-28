"""Contract tests for the WS13 retrieval Lambda (infra/ws13_query_lambda.py).

The defects these pin down, in the order they would bite:

  * infra/document_tools.py USED TO score a hybrid hit as
    ``0.75 * vector_score + 0.25 * lexical_score``, substituting -1.0 for
    ``vector_score`` when a row carried no embedding, so a perfect keyword
    match scored -0.5 and sank below every embedded chunk. That blend is not
    in the working tree any more -- the same change set replaced it with
    Reciprocal Rank Fusion at k = 60, where an arm a row is missing from
    contributes nothing at all rather than a penalty. The historical form is
    at ``git show 02da7e9:nw-mineral-monitor/infra/document_tools.py``, lines
    238-241; ``LegacyBlendControlTest`` reproduces that arithmetic so this
    suite can tell a fix from a coincidence, and
    ``LegacyDocumentToolsFusionTest`` drives the live RRF in that module.
  * The halfvec expression index is used only when the ORDER BY repeats
    ``titan_embedding::halfvec(1024)`` byte-identically and casts BOTH sides.
    Casting one side, or reaching for <-> instead of <=>, raises no error --
    it silently becomes a sequential scan over 852,027 rows, which blows the
    30 s API Gateway deadline.
  * ws13_documents.searchable_key is NULL for all 27,294 born_digital
    documents and populated for all 28,988 OCR ones, so a viewer key read
    straight off searchable_key fails to resolve for half the corpus.
  * county is stored two ways -- 15,581 rows end in ' County', 51,685 are bare
    -- so a county predicate that does not normalise both sides silently drops
    whichever spelling the caller did not happen to use.
  * doc_date is free text (76,681 NULL, 27,882 bare 'YYYY', 1,780 values like
    'VARIOUS', 'CIRCA 1980' or '1930; 1933; 1940'), so a year range has to run
    off doc_year_min/doc_year_max and never off the raw column.
  * Attribution travels with the copy. Serving all 56,282 documents is only
    defensible because a licensed-copies citation carries its licence, so a
    citation whose rights cannot be stated must not be emitted at all.
  * The front end emits mine ids like 'stategeo-igs-dd-1-if0126' and
    ws13_documents.mine_ids holds bare IGS and AZGS 'ADMM-...' codes. The
    namespaces do not intersect, so an unbridged mine filter matched 0 of
    56,282 documents and ASK reported "the indexed documents do not answer
    it" for a mine whose documents are in the corpus.
  * source_url is NULL until pipelines/ws13_backfill_provenance.py fills all
    56,282 rows, and the stored-copy citation that stands in for it must not
    be a private S3 object key: no deployed surface resolves one, so pasting
    it into an answer leaks the bucket layout AND hands the reader a
    reference nothing can open.
  * A plan gate that runs EXPLAIN (ANALYZE) performs the 852,027-row
    sequential scan it exists to detect, against production, inside the
    request it protects. The gate probe is a plain EXPLAIN of the FILTERED
    statement the request will really issue.
  * Killing the embed backfill without its cloud-init parent lets that shell
    walk on to `shutdown -h now`. An instance-initiated shutdown is a STOP,
    user data does not re-run on start and the node is in no ASG.

Nothing here touches the network, a database or AWS. The retrieval Lambda is
VPC-attached with no egress and never calls an AWS API at all, so the only
runtime dependency to stand in for is psycopg, stubbed as a module stub the
way tests/test_ws13_embed_backfill.py does it. The SQL builders, the fusion,
the rights resolver and the excerpt window are exercised as the free functions
they are, and ``FakeConn`` shapes result rows from the SELECT list of whatever
SQL ``search()`` actually issues, so the real handler runs unmodified end to
end. The three neighbours this contract spans are driven the same way: ASK
(infra/ask_lambda.py) over a stub boto3, the SQLite fallback ranker
(infra/document_tools.py) over a temporary index built in setUp, and the
index builder's parent classification (pipelines/ws13_build_ann_index.py)
over the plain dicts it decides on -- no /proc, no signals, nothing killed.
"""
import ast
import contextlib
import importlib
import io
import json
import os
import re
import sqlite3
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
sys.path.insert(0, str(ROOT / "pipelines"))

# The Lambda reads its DSN from the environment; the value never has to be
# reachable because _open_connection is replaced in every test that connects.
os.environ.setdefault("WS13_DB_DSN", "postgresql://ws13_reader@test/nwmm")

# psycopg is a deployment dependency of the Lambda, not of the test host, and
# ws13_query_lambda degrades to psycopg = None when it is absent. Bind a stub
# so the module-level name exists for mock.patch.object to replace, and so an
# unpatched connection attempt fails on the fake rather than on an import.
if "psycopg" not in sys.modules:
    try:
        import psycopg                                  # noqa: F401
    except ImportError:
        _stub = types.ModuleType("psycopg")
        _stub.connect = mock.MagicMock()
        sys.modules["psycopg"] = _stub

import ws13_query_lambda as ql
import ws13_index_contract as index_contract
import document_tools as runtime_docs
import ws13_build_ann_index as ann_index


# --- reference implementations the tests compare the module against --------

def county_key(value):
    """Python mirror of the IMMUTABLE ws13_county_key(text) SQL function:
    lower(regexp_replace(coalesce($1,''), '\\s+county$', '', 'i')).

    Used only to check what the builder bound as a parameter; the SQL is free
    to normalise on either side as long as both sides end up here.
    """
    return re.sub(r"\s+county$", "", (value or ""), flags=re.I).lower()


def legacy_hybrid_score(vector_score, lexical_position):
    """The replaced blend, reproduced rather than paraphrased.

    Its source is the pre-change revision -- ``git show
    02da7e9:nw-mineral-monitor/infra/document_tools.py``, lines 238-241 --
    NOT the working tree, where the same change set that added this file
    replaced it with RRF. ``vector_score`` is -1.0 for a row with no stored
    embedding, which is what sank the perfect keyword match.
    ``lexical_position`` is 0-based, matching the enumerate() in that
    revision.
    """
    lexical_score = 1.0 / (1.0 + lexical_position)
    return 0.75 * vector_score + 0.25 * lexical_score


PAGE_TEXT = (
    "The St. Louis mine lies on the north slope of the range and was worked "
    "intermittently between 1902 and 1948 for silver-lead ore carrying "
    "subordinate copper, zinc and bismuth. " + ("Assay returns from the upper "
    "adit averaged 14.2 ounces of silver per ton across a stope width of 3.1 "
    "feet, with local shoots running above 40 ounces. ") * 12)

UNIT_VECTOR = [1.0 / 32.0] * ql.VECTOR_DIMS      # 1024 * (1/32)^2 == 1.0

# The citation chip the browser already parses, copied from the docChip rule
# in site/index.html so this suite fails if either side drifts. A document the
# browser does not know degrades to plain text there, which is why this form
# is safe to emit for a row whose source_url is not backfilled yet -- and why
# a raw S3 key, which nothing renders and nobody can open, is not.
DOC_CHIP_RE = re.compile(
    r"\[([^\]]+)\]\(doc:([0-9a-f]{16,64})(?:#(\d{1,5}))?"
    r"(?:\?q=([A-Za-z0-9_-]+))?\)")
FRONT_END_CHIP_RULE = (
    r"\[([^\]]+)\]\(doc:([0-9a-f]{16,64})(?:#(\d{1,5}))?")


def row(chunk_id=1, sha256=None, page=3, ordinal=0, text=PAGE_TEXT,
        admission_class="originals", searchable_key="__ocr__",
        source_url="https://pubs.example.gov/report.pdf", rights_basis=None,
        doc_class="ocr_queue", distance=0.21, **overrides):
    """One hydrated chunk row, keyed exactly like ql.HYDRATE_COLUMNS."""
    sha256 = sha256 or f"{chunk_id:064x}"
    if searchable_key == "__ocr__":
        searchable_key = f"ws13/searchable/{sha256[:2]}/{sha256}/searchable.pdf"
    values = {
        "chunk_id": chunk_id, "sha256": sha256, "page": page,
        "ordinal": ordinal, "text": text, "title": f"Report {chunk_id}",
        "doc_class": doc_class,
        "s3_key": f"ws12/{admission_class}/{sha256[:2]}/{sha256}.pdf",
        "searchable_key": searchable_key, "source_url": source_url,
        "admission_class": admission_class, "rights_basis": rights_basis,
        "portal": "igs-mines", "state": "ID", "county": "Cassia County",
        "trs": "T03N R24E S15", "doc_date": "1948", "doc_type": "mine file",
        "mine_ids": ["stategeo-igs-dd-1-if0126"],
        "mine_names": ["St. Louis Mine"], "doc_year_min": 1948,
        "doc_year_max": 1948,
        # The exact fp32 re-rank projects a distance alongside the id.
        "distance": distance,
    }
    values.update(overrides)
    return values


def _harvest_licensed_rights_basis():
    """The literal CC BY-NC-SA rights_basis pipelines/mine_file_harvest.py writes.

    Lifted out of that pipeline's own source rather than retyped here, because
    the whole point of the page-size measurement below is that the fixture is
    what the corpus really carries. What comes back is the FLOOR: every
    interpolation (the licence text, the collection id, the title) is dropped,
    so a real one is longer.
    """
    tree = ast.parse(Path(ROOT, "pipelines", "mine_file_harvest.py")
                     .read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "rights_basis"
                and isinstance(node.value, ast.JoinedStr)):
            continue
        literal = "".join(part.value for part in node.value.values
                          if isinstance(part, ast.Constant))
        if "CC BY-NC-SA" in literal:
            return literal
    raise AssertionError(
        "pipelines/mine_file_harvest.py no longer builds a CC BY-NC-SA "
        "rights_basis; the page-size measurement below has lost its fixture")


LICENSED_BASIS_FLOOR = _harvest_licensed_rights_basis()
# One realistic value: the floor with the licence text, the AZGS collection id
# and a title filled in, which is what the 13,013 licensed copies actually
# carry into a citation.
LICENSED_BASIS_REAL = LICENSED_BASIS_FLOOR.replace(
    " - explicit",
    "CC BY-NC-SA 4.0 https://creativecommons.org/licenses/by-nc-sa/4.0/ - "
    "explicit", 1).replace(
    "for collection  (\"\")",
    "for collection admmr-mine-files (\"St. Louis Mine, Cassia County\")", 1)
# The longest rights_basis any validator in this repo accepts:
# pipelines/build_doc_store.py:260 bounds a registry entry at 500 characters.
# ws13_documents.rights_basis is unbounded TEXT, so this is a ceiling by
# convention rather than by constraint, and the test says so.
LICENSED_BASIS_CEILING = "x" * 500


# --- a fake connection that shapes rows from the SELECT list it is handed --

ALIAS_RE = re.compile(r"\bAS\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*$", re.I)
TRAILING_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _split_top_level(text):
    """Split a SELECT list on commas that are not inside parentheses."""
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _select_list(sql):
    """The outermost SELECT list, ignoring any CTE or subquery SELECT."""
    flat = " ".join(sql.split())
    lowered = flat.lower()
    depth, select_at, from_at, index = 0, None, None, 0
    while index < len(flat):
        char = flat[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            if select_at is None and lowered.startswith("select ", index):
                select_at = index + len("select ")
                index = select_at
                continue
            if select_at is not None and lowered.startswith(" from ", index):
                from_at = index
                break
        index += 1
    if select_at is None:
        return []
    end = from_at if from_at is not None else len(flat)
    return _split_top_level(flat[select_at:end])


def _column_name(expression):
    match = ALIAS_RE.search(expression)
    if match:
        return match.group(1).lower()
    if "<=>" in expression:
        return "distance"
    match = TRAILING_NAME_RE.search(expression)
    return match.group(1).lower() if match else None


class FakeRow(tuple):
    """A result row that answers to both a position and a column name.

    ws13_query_lambda reads arm results positionally (``row[0]``, ``row[1]``)
    and hydrates by zipping HYDRATE_COLUMNS, so positions are what matter; the
    name lookup exists so the fake's own bookkeeping stays readable.
    """

    def __new__(cls, names, values):
        item = super().__new__(cls, values)
        item._names = tuple(names)
        return item

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._names.index(key))
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


class FakeCursor:
    def __init__(self, names, rows):
        self._rows = [FakeRow(names, values) for values in rows]
        self.description = [(name,) + (None,) * 6 for name in names]
        self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeConn:
    """Routes each statement to an arm and shapes rows from its SELECT list.

    The arms are what a test controls: ``lexical`` and ``vector`` are the rows
    in the exact rank order that arm is meant to return, so a test states
    ranks directly instead of trying to make a fake tsvector or a fake cosine
    distance come out in a particular order.
    """

    def __init__(self, lexical=(), vector=(), index_present=True, plan=None,
                 mine_id_map=None, documents=None, document_total=None,
                 mine_map_has_relation=True):
        self.lexical = list(lexical)
        self.vector = list(vector)
        self.index_present = index_present
        # The document listing op reads ws13_documents rather than an arm.
        # ``documents`` is the page the database hands back and
        # ``document_total`` is what count(*) says the filter selects, and
        # they are separate on purpose: a fake that derived the total from the
        # page could not tell a true total from the length of a bounded list,
        # which is the exact confusion the op exists to remove.
        self.documents = None if documents is None else list(documents)
        self.document_total = document_total
        # None models the table pipelines/ws13_migrate.py does not create: a
        # later stage builds ws13_mine_id_map, so retrieval has to work before
        # it exists. A dict is the built table. Its values may be
        #
        #   ["IF0126"]  a mapped row, identity, verified, confidence 1.0
        #   []          an UNMAPPED row: the bridge enumerated this front-end
        #               id off the map and could not translate it, which is a
        #               different fact from having no row at all
        #   {...}       ids / relation / confidence / verified, spelled out
        self.mine_id_map = mine_id_map
        # False models the table as it stands in production before
        # pipelines/ws13_mine_id_map.py has migrated it: no relation column,
        # so the wide projection has to demote rather than take the bridge out.
        self.mine_map_has_relation = mine_map_has_relation
        # The plan text EXPLAIN returns. Default is the shape the contract
        # requires; a test that wants the failure states the Seq Scan.
        self.plan = plan or [
            f"Index Scan using {ql.INDEX_NAME} on ws13_chunks c",
            "  Order By: ((titan_embedding)::halfvec(1024) <=> '[...]')"]
        self.statements = []
        self.closed = False

    def _mine_map_rows(self, lowered):
        """ws13_mine_id_map, in whichever projection was asked for.

        Positional rows, as psycopg returns them, because that is what
        ws13_query_lambda.MineMapShape indexes into.
        """
        if "relation" in lowered and not self.mine_map_has_relation:
            raise RuntimeError(
                'column "relation" does not exist\nLINE 1: SELECT '
                'front_end_id, ws13_mine_id, ws13_mine_id_all, ...')
        wide = "relation" in lowered
        columns = ["front_end_id", "ws13_mine_id", "ws13_mine_id_all",
                   "verified", "confidence"] + (["relation"] if wide else [])
        rows = []
        for front, value in self.mine_id_map.items():
            if isinstance(value, dict):
                ids = list(value.get("ids") or [])
                verified = value.get("verified", True)
                confidence = value.get("confidence", 1.0)
                relation = value.get("relation", "identity" if ids else None)
            else:
                ids = list(value or [])
                verified, confidence = True, 1.0
                relation = "identity" if ids else None
            row = [front, ids[0] if ids else None, ids or None,
                   verified, confidence]
            if wide:
                row.append(relation)
            rows.append(tuple(row))
        return FakeCursor(columns, rows)

    # -- psycopg surface -------------------------------------------------
    def transaction(self):
        return contextlib.nullcontext()

    def close(self):
        self.closed = True

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params or ())))
        return self._route(sql)

    # -- routing ---------------------------------------------------------
    def _route(self, sql):
        flat = " ".join(sql.split())
        lowered = flat.lower()
        names = [name for name in
                 (_column_name(part) for part in _select_list(flat)) if name]
        if lowered.startswith("explain"):
            return FakeCursor(["QUERY PLAN"], [(line,) for line in self.plan])
        if "ws13_mine_id_map" in lowered:
            if self.mine_id_map is None:
                raise RuntimeError(
                    'relation "ws13_mine_id_map" does not exist')
            return self._mine_map_rows(lowered)
        if "count(*) as total" in lowered:
            total = (self.document_total if self.document_total is not None
                     else len(self._documents()))
            return FakeCursor(["total"], [(total,)])
        if "count(distinct c.page)" in lowered:
            return FakeCursor(
                names, [(item["sha256"], item.get("indexed_pages", 1),
                         item.get("unembedded_chunks", 0))
                        for item in self._documents()])
        if "from ws13_documents d" in lowered:
            return self._shape(names, self._documents())
        if "pg_class" in lowered or "pg_indexes" in lowered:
            if not self.index_present:
                return FakeCursor(names or ["exists"], [])
            return FakeCursor(names or ["exists"],
                              [tuple(1 for _ in (names or ["exists"]))])

        vector_arm = "<=>" in flat
        lexical_arm = ("@@" in flat or "ts_rank" in lowered
                       or "tsquery" in lowered)
        if vector_arm and lexical_arm:
            raise AssertionError(
                "the fake expects one statement per arm: the response reports "
                "arms.lexical.ms and arms.vector.ms separately, which a single "
                "fused statement could not measure")
        if vector_arm:
            return self._shape(names, self.vector)
        if lexical_arm:
            return self._shape(names, self.lexical)
        if "ws13_chunks" in lowered or "ws13_documents" in lowered:
            return self._shape(names, self._merged())
        return FakeCursor(names or ["one"],
                          [tuple(1 for _ in (names or ["one"]))])

    def _documents(self):
        """The rows ws13_documents answers with; the arms' rows by default.

        Defaulting to the merged arm rows keeps every pre-existing statement
        that reads ws13_documents -- plan_filtered_probe's sha256 resolution
        above all -- shaped exactly as it was before this branch existed.
        """
        return self.documents if self.documents is not None else self._merged()

    def _merged(self):
        seen, merged = set(), []
        for item in list(self.lexical) + list(self.vector):
            if item["chunk_id"] in seen:
                continue
            seen.add(item["chunk_id"])
            merged.append(item)
        return merged

    @staticmethod
    def _shape(names, source):
        names = names or list(ql.HYDRATE_COLUMNS)
        return FakeCursor(names, [tuple(item.get(name) for name in names)
                                  for item in source])


# --- the tests -------------------------------------------------------------


class QueryLambdaTestCase(unittest.TestCase):
    """Resets the module's warm-container globals before every test.

    ``_CONN``, ``_INDEX_PRESENT`` and ``_PLAN_ASSERTED`` outlive a single
    invocation on purpose; left alone they would leak one test's connection
    and one test's index verdict into the next.
    """

    def setUp(self):
        ql._CONN = None
        ql._INDEX_PRESENT = False
        ql._PLAN_ASSERTED = set()
        ql._ITERATIVE_SCAN = None
        ql._APPLIED_TIMEOUT_MS = None
        # The projection ws13_mine_id_map answered to last. It is a warm
        # container global for the same reason as the others -- a migration
        # that adds the relation column starts new containers -- and left
        # alone a test running against a pre-migration table would demote
        # every later test to the legacy projection.
        ql._MINE_MAP_SHAPE = None
        self.addCleanup(setattr, ql, "_CONN", None)
        self.addCleanup(setattr, ql, "_INDEX_PRESENT", False)
        self.addCleanup(setattr, ql, "_PLAN_ASSERTED", set())
        self.addCleanup(setattr, ql, "_ITERATIVE_SCAN", None)
        self.addCleanup(setattr, ql, "_APPLIED_TIMEOUT_MS", None)
        self.addCleanup(setattr, ql, "_MINE_MAP_SHAPE", None)

    def run_search(self, event, lexical=(), vector=(), index_present=True,
                   vector_arm="true", plan=None, environment=None,
                   mine_id_map=None):
        conn = FakeConn(lexical=lexical, vector=vector,
                        index_present=index_present, plan=plan,
                        mine_id_map=mine_id_map)
        self.conn = conn
        environ = {"WS13_VECTOR_ARM": vector_arm}
        environ.update(environment or {})
        with mock.patch.dict(os.environ, environ), \
                mock.patch.object(ql, "_open_connection", lambda: conn):
            return ql.handler(event, None)

    def search(self, lexical=(), vector=(), index_present=True,
               vector_arm="true", plan=None, environment=None,
               mine_id_map=None, **event):
        payload = {"op": "search", "query": "lava creek silver assay",
                   "query_vector": list(UNIT_VECTOR), "limit": 25}
        payload.update(event)
        return self.run_search(payload, lexical=lexical, vector=vector,
                               index_present=index_present,
                               vector_arm=vector_arm, plan=plan,
                               environment=environment,
                               mine_id_map=mine_id_map)

    def hit_by_id(self, response, chunk_id):
        for hit in response["hits"]:
            if hit["chunk_id"] == chunk_id:
                return hit
        raise AssertionError(
            f"chunk {chunk_id} is missing from "
            f"{[hit['chunk_id'] for hit in response['hits']]}")

    def position_of(self, response, chunk_id):
        ids = [hit["chunk_id"] for hit in response["hits"]]
        self.assertIn(chunk_id, ids)
        return ids.index(chunk_id)


class ReciprocalRankFusionTest(QueryLambdaTestCase):
    """score(d) = sum over arms of 1/(60 + rank_in_that_arm), ranks 1-based."""

    def test_missing_arm_contributes_nothing_not_a_negative_score(self):
        fused = dict((chunk_id, score) for chunk_id, score, _ in
                     ql.rrf_fuse({"lexical": [101], "vector": [202]}))
        # Both sit at rank 1 of their own arm and are absent from the other.
        # If absence cost anything these two could not be equal, and at least
        # one of them would be negative.
        self.assertAlmostEqual(fused[101], 1.0 / 61, places=12)
        self.assertAlmostEqual(fused[202], 1.0 / 61, places=12)
        self.assertGreater(fused[101], 0.0)
        self.assertGreater(fused[202], 0.0)

    def test_scores_are_the_exact_sum_of_one_over_sixty_plus_rank(self):
        fused = ql.rrf_fuse({"lexical": [11, 12, 13], "vector": [13, 11]})
        scores = {chunk_id: score for chunk_id, score, _ in fused}
        ranks = {chunk_id: rank for chunk_id, _, rank in fused}
        self.assertAlmostEqual(scores[11], 1.0 / 61 + 1.0 / 62, places=12)
        self.assertAlmostEqual(scores[12], 1.0 / 62, places=12)
        self.assertAlmostEqual(scores[13], 1.0 / 63 + 1.0 / 61, places=12)
        self.assertEqual([chunk_id for chunk_id, _, _ in fused], [11, 13, 12])
        self.assertEqual(ranks[11], {"lexical": 1, "vector": 2})
        self.assertEqual(ranks[12], {"lexical": 2})

    def test_k_is_sixty_which_puts_the_dual_arm_crossover_at_rank_62(self):
        """Recover k from the module's own arithmetic, then state where
        agreement between the arms stops paying.

        A row in both arms at rank r scores 2/(60+r); a rank-1 row in a single
        arm scores 1/61. Those are exactly equal at r = 62 (2/122 == 1/61), so
        consensus wins for r <= 61 and loses from r = 63 on. over_fetch
        defaults to 200, so both sides of that crossover are reachable.
        """
        (_, score, _), = ql.rrf_fuse({"lexical": [1]})
        self.assertEqual(round(1.0 / score) - 1, 60)
        self.assertEqual(ql.RRF_K, 60)
        self.assertAlmostEqual(2.0 / (ql.RRF_K + 62), 1.0 / (ql.RRF_K + 1),
                               places=12)
        self.assertGreater(2.0 / (ql.RRF_K + 61), 1.0 / (ql.RRF_K + 1))
        self.assertLess(2.0 / (ql.RRF_K + 63), 1.0 / (ql.RRF_K + 1))

    def test_a_deep_dual_arm_row_falls_below_a_rank_one_lexical_row(self):
        """A lexical rank-1 row with no vector at all, against a row both arms
        return but only deep in each.

        900 is lexical rank 1 and absent from the vector arm: 1/61. 299 is
        lexical rank 101 and vector rank 100: 1/161 + 1/160. The old blend
        would have scored 900 at -0.5 and sunk it below every embedded row;
        RRF has it comfortably ahead.
        """
        deep = list(range(200, 300))
        fused = ql.rrf_fuse({"lexical": [900, *deep], "vector": deep})
        scores = {chunk_id: score for chunk_id, score, _ in fused}
        order = [chunk_id for chunk_id, _, _ in fused]
        self.assertAlmostEqual(scores[900], 1.0 / 61, places=12)
        self.assertAlmostEqual(scores[299], 1.0 / 161 + 1.0 / 160, places=12)
        self.assertGreater(scores[900], scores[299])
        self.assertLess(order.index(900), order.index(299))
        # 200 is lexical rank 2 and vector rank 1, so both arms agree on it
        # near the top and it legitimately outranks 900 -- that is the k = 60
        # crossover at work, not a regression.
        self.assertAlmostEqual(scores[200], 1.0 / 62 + 1.0 / 61, places=12)
        self.assertGreater(scores[200], scores[900])

    def test_ties_break_deterministically(self):
        """Two identical scores must not reorder between invocations, or a
        shadow run compares against noise."""
        first = ql.rrf_fuse({"lexical": [7, 3], "vector": [3, 7]})
        second = ql.rrf_fuse({"lexical": [7, 3], "vector": [3, 7]})
        self.assertEqual([chunk_id for chunk_id, _, _ in first],
                         [chunk_id for chunk_id, _, _ in second])

    def test_fusion_over_a_single_arm_is_that_arm(self):
        fused = ql.rrf_fuse({"lexical": [5, 6, 7]})
        self.assertEqual([chunk_id for chunk_id, _, _ in fused], [5, 6, 7])


class LegacyBlendControlTest(QueryLambdaTestCase):
    """The control for the fusion change: if the old formula stops producing
    the inversion, the positive tests above are not guarding anything."""

    def test_old_weighting_sinks_a_perfect_keyword_match(self):
        keyword = legacy_hybrid_score(-1.0, 0)      # rank 1, no embedding
        embedded = legacy_hybrid_score(0.35, 14)    # rank 15, cosine 0.35
        self.assertAlmostEqual(keyword, -0.5, places=12)
        self.assertGreater(
            embedded, keyword,
            "0.75*vector + 0.25*lexical must still put the embedded chunk "
            "first, otherwise this control controls nothing")

    def test_reciprocal_rank_fusion_reverses_that_inversion(self):
        """Same two rows, same arms: the keyword hit is lexical rank 1 and has
        no vector, the embedded chunk is lexical rank 15 and did not make the
        ANN candidate slice."""
        vector_arm = list(range(500, 520))
        fused = ql.rrf_fuse({
            "lexical": [900, *range(910, 923), 999],
            "vector": vector_arm})
        scores = {chunk_id: score for chunk_id, score, _ in fused}
        self.assertAlmostEqual(scores[900], 1.0 / 61, places=12)
        self.assertAlmostEqual(scores[999], 1.0 / 75, places=12)
        self.assertGreater(scores[900], scores[999])
        order = [chunk_id for chunk_id, _, _ in fused]
        self.assertLess(order.index(900), order.index(999))


class CitationResolverTest(QueryLambdaTestCase):
    """viewer_key, viewer_key_kind and resolvable_via, per document."""

    def test_ocr_document_resolves_to_its_searchable_copy(self):
        citation = ql.citation_for(row(
            21, searchable_key="ws13/searchable/ab/abc/searchable.pdf"))
        self.assertEqual(citation["viewer_key"],
                         "ws13/searchable/ab/abc/searchable.pdf")
        self.assertEqual(citation["viewer_key_kind"], "searchable")
        self.assertEqual(citation["page"], 3)

    def test_born_digital_document_falls_back_to_the_stored_original(self):
        """searchable_key is NULL for all 27,294 born_digital documents; the
        original already carries a text layer, so s3_key is the right viewer,
        not a compromise."""
        source = row(22, searchable_key=None, doc_class="born_digital")
        citation = ql.citation_for(source)
        self.assertEqual(citation["viewer_key"], source["s3_key"])
        self.assertEqual(citation["viewer_key_kind"], "born_digital_original")

    def test_missing_source_url_cites_the_document_chip_not_the_s3_key(self):
        """A private S3 object key is not a reference a reader can open, and
        pasting one into an answer publishes internal storage layout. The
        front end already parses `[title, p. N](doc:<sha256>#<page>)` (see the
        docChip rule in site/index.html) and degrades an unknown document to
        plain text rather than a dead link, so that is the citation form for a
        row whose source_url has not been backfilled yet."""
        source = row(23, source_url=None)
        citation = ql.citation_for(source)
        self.assertIsNone(citation["source_url"])
        self.assertEqual(citation["resolvable_via"], "stored_copy")
        self.assertTrue(citation["s3_key"])
        self.assertIn(f"(doc:{source['sha256']}#3)", citation["markdown"])
        self.assertNotIn(citation["viewer_key"], citation["markdown"])
        self.assertNotIn(citation["s3_key"], citation["markdown"])
        self.assertRegex(citation["markdown"], DOC_CHIP_RE)

    def test_the_viewer_key_stays_an_internal_field(self):
        """viewer_key and viewer_key_kind are what the eventual viewer
        integration resolves; they are never the thing the model is told to
        paste. They stay in the payload and stay out of the markdown."""
        for source in (row(231, source_url=None),
                       row(232, source_url="https://pubs.example.gov/x.pdf")):
            with self.subTest(source_url=source["source_url"]):
                citation = ql.citation_for(source)
                self.assertTrue(citation["viewer_key"])
                self.assertIn(citation["viewer_key_kind"],
                              ("searchable", "born_digital_original"))
                self.assertNotIn(citation["viewer_key"], citation["markdown"])

    def test_a_hit_with_no_source_url_is_still_emitted(self):
        """A dead portal must not remove a document from retrieval; the WS12
        browser acceptance already proves the stored copy renders without the
        publisher (tools/test_doc_viewer.js)."""
        response = self.search(lexical=[row(24, source_url=None)],
                               arms=["lexical"])
        self.assertEqual(response["count"], 1)
        self.assertEqual(
            response["hits"][0]["citation"]["resolvable_via"], "stored_copy")

    def test_present_source_url_resolves_through_the_publisher(self):
        citation = ql.citation_for(
            row(25, source_url="https://pubs.example.gov/if0126.pdf"))
        self.assertEqual(citation["resolvable_via"], "source_url")
        self.assertEqual(citation["source_url"],
                         "https://pubs.example.gov/if0126.pdf")
        self.assertIn("https://pubs.example.gov/if0126.pdf",
                      citation["markdown"])

    def test_citation_is_anchored_to_the_page_the_chunk_came_from(self):
        citation = ql.citation_for(row(26, page=17))
        self.assertEqual(citation["page"], 17)
        self.assertIn("17", citation["markdown"])

    def test_a_chunk_without_a_page_anchor_fails_closed(self):
        """An unanchored citation is unverifiable; 760,043 pages exist and
        every chunk carries one."""
        with self.assertRaises(RuntimeError):
            ql.citation_for(row(27, page=0))

    def test_unknown_admission_class_fails_closed(self):
        """admission_class is split_part(s3_key,'/',2). A fourth rights prefix
        appearing in the bucket must stop retrieval, not be served with
        guessed terms."""
        with self.assertRaises(RuntimeError):
            ql.citation_for(row(28, admission_class="embargoed-copies"))
        with self.assertRaises(RuntimeError):
            ql.citation_for(row(29, admission_class=None))

    def test_an_unhydratable_ranked_chunk_stops_the_response(self):
        """Nothing may silently drop a document: a ranked chunk id that does
        not come back from ws13_chunks is a corpus inconsistency."""
        conn = FakeConn(lexical=[row(31)])
        conn._merged = lambda: []          # ranked, then refuses to hydrate
        with mock.patch.dict(os.environ, {"WS13_VECTOR_ARM": "false"}), \
                mock.patch.object(ql, "_open_connection", lambda: conn), \
                self.assertRaises(RuntimeError):
            ql.handler({"op": "search", "query": "silver", "limit": 5}, None)


class RightsPropagationTest(QueryLambdaTestCase):
    """Attribution travels with the copy or the copy is not served."""

    def test_originals_carry_no_obligations(self):
        rights = ql.rights_for("originals", None)
        self.assertFalse(rights["attribution_required"])
        self.assertFalse(rights["non_commercial"])
        self.assertFalse(rights["share_alike"])
        self.assertEqual(
            rights["rights_terms"],
            "public domain (US federal / state survey public record)")

    def test_licensed_copies_carry_the_full_cc_by_nc_sa_triple(self):
        basis = "Arizona Geological Survey, ADMMR mine file collection"
        rights = ql.rights_for("licensed-copies", basis)
        self.assertTrue(rights["attribution_required"])
        self.assertTrue(rights["non_commercial"])
        self.assertTrue(rights["share_alike"])
        self.assertIn("CC BY-NC-SA 4.0", rights["rights_terms"])
        self.assertIn("attribution required", rights["rights_terms"])
        self.assertIn("non-commercial", rights["rights_terms"])
        self.assertIn("share-alike", rights["rights_terms"])
        self.assertIn(basis, rights["rights_terms"])
        self.assertEqual(rights["rights_basis"], basis)

    def test_research_copies_are_attributed_but_not_share_alike(self):
        basis = "Idaho Geological Survey state archive"
        rights = ql.rights_for("research-copies", basis)
        self.assertTrue(rights["attribution_required"])
        self.assertTrue(rights["non_commercial"])
        self.assertFalse(rights["share_alike"])
        self.assertIn("state-archive research copy", rights["rights_terms"])
        self.assertIn("not redistributable", rights["rights_terms"])
        self.assertIn(basis, rights["rights_terms"])

    def test_a_citation_is_never_emitted_with_its_rights_basis_dropped(self):
        """13,013 AZGS ADMMR pages are served under CC BY-NC-SA 4.0 and 32,312
        under state-archive research terms. Both licences name the source, so
        a row with no recorded basis has no attributable form: rendering
        'source: None' or dropping the clause is the licence violation, and
        the only safe answer is to refuse the citation."""
        for admission_class in ("licensed-copies", "research-copies"):
            with self.subTest(admission_class=admission_class):
                with self.assertRaises(RuntimeError):
                    ql.rights_for(admission_class, None)
                with self.assertRaises(RuntimeError):
                    ql.rights_for(admission_class, "   ")
                with self.assertRaises(RuntimeError):
                    ql.citation_for(row(42, admission_class=admission_class,
                                        rights_basis=None))
        # Public-domain originals have nothing to attribute, so they are the
        # one class a missing basis cannot compromise.
        self.assertTrue(ql.rights_for("originals", None)["rights_terms"])

    def test_no_rights_terms_string_can_render_a_null_licensor(self):
        for admission_class in ql.ADMISSION_CLASSES:
            with self.subTest(admission_class=admission_class):
                terms = ql.rights_for(admission_class, "Example survey")[
                    "rights_terms"]
                self.assertNotIn("None", terms)
                self.assertNotRegex(terms, r"source:\s*$")

    def test_rights_survive_the_trip_into_a_citation(self):
        basis = "Arizona Geological Survey, ADMMR mine file collection"
        citation = ql.citation_for(row(41, admission_class="licensed-copies",
                                       rights_basis=basis))
        self.assertEqual(citation["admission_class"], "licensed-copies")
        self.assertEqual(citation["rights_basis"], basis)
        self.assertIn("CC BY-NC-SA 4.0", citation["rights_terms"])
        self.assertTrue(citation["attribution_required"])
        self.assertTrue(citation["non_commercial"])
        self.assertTrue(citation["share_alike"])

    def test_every_admission_class_wsl2_landed_is_covered(self):
        """WS12 landed exactly three rights prefixes: 10,957 originals, 13,013
        licensed-copies, 32,312 research-copies."""
        self.assertEqual(sorted(ql.ADMISSION_CLASSES),
                         ["licensed-copies", "originals", "research-copies"])
        for admission_class in ql.ADMISSION_CLASSES:
            with self.subTest(admission_class=admission_class):
                rights = ql.rights_for(admission_class, "Example collection")
                self.assertTrue(rights["rights_terms"].strip())
                for flag in ("attribution_required", "non_commercial",
                             "share_alike"):
                    self.assertIsInstance(rights[flag], bool)

    def test_the_citation_rule_tells_the_caller_to_reproduce_the_terms(self):
        self.assertIn("rights_terms", ql.CITATION_RULE)
        self.assertIn("attribution", ql.CITATION_RULE.lower())


class FilterPredicateTest(QueryLambdaTestCase):
    """The two filters whose wrong answer is silent rather than loud."""

    def test_bare_and_suffixed_county_reach_the_same_predicate_value(self):
        bare_clauses, bare_params = ql.filter_sql({"county": "Cassia"})
        suffixed_clauses, suffixed_params = ql.filter_sql(
            {"county": "Cassia County"})
        self.assertEqual([county_key(value) for value in bare_params],
                         [county_key(value) for value in suffixed_params])
        self.assertEqual([county_key(value) for value in bare_params],
                         ["cassia"])
        # Normalising only the caller's side still misses every stored
        # 'Cassia County' row. Applying ws13_county_key to the column is also
        # what makes the ws13_documents_county_key expression index usable.
        joined = " ".join(bare_clauses + suffixed_clauses)
        self.assertIn("ws13_county_key(d.county)", joined)
        self.assertEqual(joined.count("ws13_county_key(%s)"), 2)

    def test_county_filter_survives_into_both_arms(self):
        lexical, lexical_params = ql.lexical_sql(
            {"county": "Cassia County"}, "silver", 200)
        vector, vector_params = ql.vector_ann_sql(
            {"county": "Cassia"}, "[0.0]", 200)
        for sql in (lexical, vector):
            self.assertIn("ws13_county_key(d.county) = ws13_county_key(%s)",
                          sql)
        self.assertIn("Cassia County", lexical_params)
        self.assertIn("Cassia", vector_params)

    def test_year_filter_uses_the_parsed_year_columns_not_doc_date(self):
        """doc_date is free text: 76,681 NULL and 1,780 values like 'VARIOUS',
        'CIRCA 1980' or '1930; 1933; 1940'. Comparing it as a date or a number
        is wrong for every one of them."""
        clauses, params = ql.filter_sql({"year_min": 1930, "year_max": 1965})
        joined = " ".join(clauses)
        self.assertIn("doc_year_min", joined)
        self.assertIn("doc_year_max", joined)
        self.assertIsNone(
            re.search(r"doc_date\s*(>=|<=|>|<|=|between)", joined, re.I),
            "a year range must never be compared against doc_date")
        self.assertEqual(params, [1930, 1965])

    def test_year_range_is_an_interval_overlap_not_a_containment(self):
        """A 1930-1965 request must return a document dated 1928-1940."""
        clauses, _ = ql.filter_sql({"year_min": 1930, "year_max": 1965})
        joined = " ".join(clauses)
        self.assertIn("d.doc_year_max >= %s", joined)
        self.assertIn("d.doc_year_min <= %s", joined)

    def test_an_inverted_year_range_is_rejected(self):
        with self.assertRaises(ValueError):
            ql.normalize_filters({"year_min": 1990, "year_max": 1930})

    def test_unknown_admission_class_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            ql.normalize_filters({"admission_class": ["public-domain"]})
        self.assertEqual(
            ql.normalize_filters({"admission_class": "licensed-copies"}),
            {"admission_class": ["licensed-copies"]})

    def test_sha256_filter_must_be_sixty_four_hex_characters(self):
        with self.assertRaises(ValueError):
            ql.normalize_filters({"sha256": "3c3fc7e9"})

    def test_filter_values_are_bound_never_interpolated(self):
        clauses, params = ql.filter_sql(
            {"state": "ID", "portal": "azgs-admmr'; DROP TABLE ws13_chunks--"})
        joined = " ".join(clauses)
        self.assertNotIn("DROP", joined)
        self.assertIn("azgs-admmr'; DROP TABLE ws13_chunks--", params)

    def test_metadata_only_search_refuses_to_scan_the_whole_corpus(self):
        with self.assertRaises(ValueError):
            ql.metadata_sql({}, 8)


class HalfvecContractTest(QueryLambdaTestCase):
    """One source of truth for the strings that decide index vs seq scan."""

    def test_exported_constants_are_the_canonical_strings(self):
        self.assertEqual(ql.INDEX_NAME, "ws13_chunks_titan_hnsw")
        self.assertEqual(ql.HALFVEC_EXPR, "titan_embedding::halfvec(1024)")
        self.assertEqual(
            ql.ORDER_BY_SQL,
            "ORDER BY c.titan_embedding::halfvec(1024) <=> %s::halfvec(1024)")

    def test_both_sides_of_the_distance_operator_are_cast(self):
        """Casting only the column is the failure with no error message: the
        operator resolves against vector, halfvec_cosine_ops cannot serve it,
        and the plan becomes a scan of 852,027 rows."""
        self.assertRegex(ql.ORDER_BY_SQL,
                         r"::halfvec\(1024\)\s*<=>\s*%s::halfvec\(1024\)")
        self.assertEqual(ql.ORDER_BY_SQL.count("::halfvec(1024)"), 2)
        self.assertNotIn("<->", ql.ORDER_BY_SQL)
        self.assertNotIn("<#>", ql.ORDER_BY_SQL)

    def test_create_index_matches_the_opclass_the_order_by_needs(self):
        """Measured norms are 0.9999996-1.0000005, so cosine is the correct
        operator, and halfvec recall@10 measured 100% over 6 probes with a
        maximum distance delta of 1.79e-05."""
        flat = " ".join(ql.CREATE_INDEX_SQL.split())
        for fragment in ("ws13_chunks_titan_hnsw", "ON ws13_chunks",
                         "USING hnsw",
                         "(titan_embedding::halfvec(1024)) halfvec_cosine_ops",
                         "m = 16", "ef_construction = 100"):
            self.assertIn(fragment, flat)

    def test_maintenance_statements_target_the_right_relation(self):
        self.assertIn("ANALYZE", ql.ANALYZE_SQL.upper())
        self.assertIn("ws13_chunks", ql.ANALYZE_SQL)
        self.assertTrue(ql.EXPLAIN_SQL.upper().lstrip().startswith("EXPLAIN"))
        self.assertIn(ql.ORDER_BY_SQL, ql.EXPLAIN_SQL)
        self.assertIn("LIMIT", ql.EXPLAIN_SQL.upper())

    def test_the_ann_probe_repeats_the_order_by_byte_for_byte(self):
        """An expression index is used only when the query repeats the
        expression exactly; a reformatted copy is a silent seq scan."""
        sql, _ = ql.vector_ann_sql({}, "[0.0]", 200)
        self.assertIn(ql.ORDER_BY_SQL, sql)
        filtered, _ = ql.vector_ann_sql({"state": "ID"}, "[0.0]", 200)
        self.assertIn(ql.ORDER_BY_SQL, filtered)

    def test_the_ann_order_by_stays_single_table(self):
        """pgvector drives the HNSW index only when the ordering expression
        references one relation; a join in the ANN stage is a sequential
        scan. Document predicates therefore go inside an EXISTS."""
        sql, _ = ql.vector_ann_sql({"state": "ID", "county": "Cassia"},
                                   "[0.0]", 200)
        head = sql.split("ORDER BY")[0]
        self.assertIn("EXISTS (SELECT 1 FROM ws13_documents d", head)
        self.assertNotIn("JOIN", head.upper())

    def test_index_contract_reports_no_problems(self):
        problems = index_contract.check()
        self.assertEqual(list(problems or []), [],
                         f"ws13_index_contract.check() found: {problems}")

    def test_the_query_vector_is_validated_before_it_reaches_postgres(self):
        self.assertTrue(ql.vector_literal(UNIT_VECTOR).startswith("["))
        for bad in ([0.1] * 1536, [0.0] * ql.VECTOR_DIMS, "not-a-vector",
                    [float("nan")] * ql.VECTOR_DIMS):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ValueError):
                    ql.vector_literal(bad)


class BoundedOutputTest(QueryLambdaTestCase):
    """A retrieval tool returns excerpts. Returning pages would republish the
    corpus, and two of the three rights prefixes forbid that."""

    def test_limit_clamps_to_twenty_five(self):
        self.assertEqual(ql.normalize_request({"limit": 500})["limit"], 25)
        self.assertEqual(ql.normalize_request({"limit": 0})["limit"], 1)
        self.assertEqual(ql.normalize_request({"limit": -8})["limit"], 1)
        self.assertEqual(ql.normalize_request({})["limit"], 8)

    def test_excerpt_budget_clamps_to_its_documented_window(self):
        self.assertEqual(
            ql.normalize_request({"max_excerpt_chars": 5000})
            ["max_excerpt_chars"], 1000)
        self.assertEqual(
            ql.normalize_request({"max_excerpt_chars": 5})
            ["max_excerpt_chars"], 120)
        self.assertEqual(ql.normalize_request({})["max_excerpt_chars"], 760)

    def test_over_fetch_never_drops_below_the_requested_limit(self):
        """Fusing a candidate list shorter than `limit` cannot fill the
        response."""
        request = ql.normalize_request({"limit": 25, "over_fetch": 3})
        self.assertGreaterEqual(request["over_fetch"], request["limit"])

    def test_a_whole_page_is_never_returned(self):
        text = ql.excerpt(PAGE_TEXT, ql.terms_of("silver assay"), 760)
        self.assertLess(len(text), len(PAGE_TEXT))
        self.assertNotIn(PAGE_TEXT, text)
        # +2 for the leading and trailing ellipsis a centred window adds.
        self.assertLessEqual(len(text), 762)

    def test_excerpt_is_centred_on_the_query_terms(self):
        haystack = ("filler " * 400) + "LAVA CREEK DISTRICT " + ("tail " * 400)
        window = ql.excerpt(haystack, ql.terms_of("lava creek district"), 200)
        self.assertIn("LAVA CREEK DISTRICT", window)
        self.assertLessEqual(len(window), 202)

    def test_short_text_is_returned_whole_without_ellipsis(self):
        self.assertEqual(ql.excerpt("  a short  page  ", [], 760),
                         "a short page")

    def test_the_response_honours_the_clamped_limit(self):
        rows = [row(700 + offset) for offset in range(40)]
        response = self.search(lexical=rows, limit=500, arms=["lexical"])
        self.assertEqual(len(response["hits"]), 25)
        self.assertEqual(response["count"], 25)

    def test_the_response_honours_the_clamped_excerpt_budget(self):
        response = self.search(lexical=[row(760)], max_excerpt_chars=5000,
                               arms=["lexical"])
        self.assertLessEqual(len(response["hits"][0]["excerpt"]), 1002)


class SearchIntegrationTest(QueryLambdaTestCase):
    """End-to-end through the real search(), against the fake connection."""

    def test_both_arms_fuse_and_the_response_names_the_mode(self):
        response = self.search(lexical=[row(81), row(82)],
                               vector=[row(82), row(81)])
        self.assertEqual(response["status"], "loaded")
        self.assertEqual(response["retrieval_mode"], "rrf_lexical_vector")
        self.assertTrue(response["arms"]["vector"]["enabled"])
        self.assertIsNone(response["arms"]["vector"]["reason"])
        self.assertEqual(response["arms"]["lexical"]["candidates"], 2)
        self.assertEqual(response["arms"]["vector"]["candidates"], 2)
        self.assertEqual(response["arms"]["vector"]["ef_search"], 200)
        hit = self.hit_by_id(response, 81)
        self.assertEqual(hit["sources"], ["lexical", "vector"])
        self.assertEqual(hit["ranks"], {"lexical": 1, "vector": 2})
        self.assertAlmostEqual(hit["rrf_score"], 1.0 / 61 + 1.0 / 62, places=6)
        self.assertIsNotNone(hit["vector_distance"])

    def test_a_lexical_only_row_keeps_a_null_vector_rank_and_distance(self):
        response = self.search(lexical=[row(83)], vector=[row(84)])
        lexical_hit = self.hit_by_id(response, 83)
        self.assertEqual(lexical_hit["sources"], ["lexical"])
        self.assertIsNone(lexical_hit["ranks"]["vector"])
        self.assertIsNone(lexical_hit["vector_distance"])
        self.assertGreater(lexical_hit["rrf_score"], 0.0)

    def test_a_disabled_vector_arm_always_says_why(self):
        """A silently absent vector arm is exactly how a dead ANN index looks
        from the outside."""
        response = self.search(lexical=[row(85)], arms=["lexical"])
        self.assertEqual(response["retrieval_mode"], "lexical_only")
        self.assertFalse(response["arms"]["vector"]["enabled"])
        self.assertIn("not requested", response["arms"]["vector"]["reason"])

    def test_a_missing_ann_index_disables_the_arm_instead_of_scanning(self):
        """There is no ANN index on any vector column yet. Running the arm
        anyway is a sequential scan over 852,027 rows behind a 30 s
        deadline."""
        response = self.search(lexical=[row(86)], vector=[row(86)],
                               index_present=False)
        self.assertEqual(response["retrieval_mode"], "lexical_only")
        self.assertFalse(response["arms"]["vector"]["enabled"])
        self.assertIn(ql.INDEX_NAME, response["arms"]["vector"]["reason"])
        self.assertEqual(
            [sql for sql, _ in self.conn.statements if "<=>" in sql], [],
            "the ANN probe must not run without its index")

    def test_a_vector_arm_without_a_query_vector_is_reported_not_skipped(self):
        """This function has no egress and cannot embed the query itself."""
        response = self.search(lexical=[row(87)], query_vector=None)
        self.assertEqual(response["retrieval_mode"], "lexical_only")
        self.assertIn("query_vector", response["arms"]["vector"]["reason"])

    def test_vector_only_request_reports_vector_only(self):
        response = self.search(vector=[row(88)], arms=["vector"])
        self.assertEqual(response["retrieval_mode"], "vector_only")
        self.assertTrue(response["arms"]["vector"]["enabled"])
        self.assertEqual(self.hit_by_id(response, 88)["sources"], ["vector"])

    def test_filters_without_a_query_run_as_a_metadata_filter(self):
        response = self.search(lexical=[row(89)], query="", query_vector=None,
                               filters={"state": "ID", "year_min": 1940})
        self.assertEqual(response["retrieval_mode"], "metadata_filter")
        self.assertEqual(response["hits"][0]["sources"], [])

    def test_a_request_with_nothing_to_retrieve_on_is_rejected(self):
        with self.assertRaises(ValueError):
            self.search(query="", query_vector=None, filters={})

    def test_ef_search_is_applied_on_the_session_that_runs_the_probe(self):
        """hnsw.ef_search defaults to 40; the contract default is 200."""
        self.search(lexical=[row(90)], vector=[row(90)], ef_search=250)
        applied = [params for sql, params in self.conn.statements
                   if "hnsw.ef_search" in sql]
        self.assertEqual(applied, [("250",)])

    def test_every_hit_carries_the_full_citation_and_metadata_blocks(self):
        response = self.search(lexical=[row(91)], vector=[row(91)])
        hit = response["hits"][0]
        for key in ("chunk_id", "sha256", "page", "ordinal", "excerpt",
                    "sources", "ranks", "rrf_score", "vector_distance",
                    "citation", "metadata"):
            self.assertIn(key, hit)
        for key in ("document_title", "page", "source_url", "markdown",
                    "sha256", "s3_key", "viewer_key", "viewer_key_kind",
                    "admission_class", "rights_basis", "rights_terms",
                    "attribution_required", "non_commercial", "share_alike",
                    "resolvable_via"):
            self.assertIn(key, hit["citation"])
        for key in ("portal", "state", "county", "trs", "doc_date", "doc_type",
                    "mine_ids", "mine_names", "doc_year_min", "doc_year_max"):
            self.assertIn(key, hit["metadata"])
        self.assertTrue(response["citation_rule"].strip())

    def test_the_response_is_json_serialisable(self):
        """It crosses API Gateway; a Decimal or a date here is a 502."""
        json.dumps(self.search(lexical=[row(92)], vector=[row(92)]))

    def test_ping_reports_loaded_without_touching_the_corpus(self):
        response = self.run_search({"op": "ping"})
        self.assertEqual(response["status"], "loaded")
        self.assertEqual(response["index_name"], ql.INDEX_NAME)
        self.assertEqual(
            [sql for sql, _ in self.conn.statements
             if "ws13_chunks" in sql], [],
            "ping must not read ws13_chunks")

    def test_an_unknown_op_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_search({"op": "delete"})

    def test_the_statement_timeout_sits_below_the_gateway_deadline(self):
        """API Gateway integrations time out at 30 s; a query that outlives
        that returns a 504 with no diagnosis, so the server-side timeout has
        to fire first."""
        self.assertLess(ql.statement_timeout_ms(), 30000)
        conn = FakeConn()
        driver = types.SimpleNamespace(connect=lambda *a, **k: conn)
        with mock.patch.object(ql, "psycopg", driver), \
                mock.patch.dict(os.environ,
                                {"WS13_DB_DSN": "postgresql://test/nwmm"}):
            ql._open_connection()
        timeouts = [params for sql, params in conn.statements
                    if "statement_timeout" in sql]
        self.assertTrue(timeouts, "statement_timeout was never set")
        self.assertLess(int(timeouts[0][0]), 30000)

    def test_a_warm_container_reuses_one_connection(self):
        """The function is VPC-attached with no NAT; reconnecting per
        invocation would add a fresh TCP and TLS handshake to every call."""
        conn = FakeConn(lexical=[row(94)])
        opened = []

        def open_once():
            opened.append(conn)
            return conn

        with mock.patch.dict(os.environ, {"WS13_VECTOR_ARM": "false"}), \
                mock.patch.object(ql, "_open_connection", open_once):
            for _ in range(3):
                ql.handler({"op": "search", "query": "silver", "limit": 5},
                           None)
        self.assertEqual(len(opened), 1)


class MineIdNamespaceTest(QueryLambdaTestCase):
    """The front end's mine id is not the corpus's mine id.

    The browser emits 'stategeo-igs-dd-1-if0126'; ws13_documents.mine_ids
    holds AZGS 'ADMM-...' codes and bare IGS codes. The namespaces do not
    intersect, so a mine-scoped search matched 0 of 56,282 documents and ASK
    reported "the indexed documents do not answer it" for a mine whose
    documents are in the corpus. ws13_mine_id_map bridges them, and it is
    built by a later stage than the one that creates the corpus, so retrieval
    has to work before the table exists.
    """

    FRONT_END_ID = "stategeo-igs-dd-1-if0126"

    def test_a_mapped_id_is_rewritten_before_the_predicate_is_built(self):
        response = self.search(
            lexical=[row(301)], arms=["lexical"],
            filters={"mine_id": self.FRONT_END_ID},
            mine_id_map={self.FRONT_END_ID: ["IF0126", "ADMM-01234"]})
        resolution = response["filter_resolution"]["mine_id"]
        self.assertEqual(resolution["requested"], self.FRONT_END_ID)
        self.assertEqual(sorted(resolution["resolved"]),
                         ["ADMM-01234", "IF0126"])
        self.assertEqual(resolution["via"], "ws13_mine_id_map")
        self.assertNotIn("filter_unresolved", response)
        bound = [param for sql, params in self.conn.statements
                 if "d.mine_ids && %s::text[]" in sql
                 for param in params if isinstance(param, list)]
        self.assertTrue(bound, "the mine predicate never ran")
        self.assertNotIn([self.FRONT_END_ID], bound,
                         "the front-end id reached the corpus predicate")
        self.assertTrue(any(sorted(value) == ["ADMM-01234", "IF0126"]
                            for value in bound), bound)

    def test_a_missing_map_table_is_an_empty_map_not_an_error(self):
        """pipelines/ws13_migrate.py deliberately does not create
        ws13_mine_id_map. A relation-does-not-exist here must degrade to
        "use the id as supplied", not fail the request."""
        response = self.search(lexical=[row(302)], arms=["lexical"],
                               filters={"mine_id": "ADMM-01234"},
                               mine_id_map=None)
        self.assertEqual(response["status"], "loaded")
        resolution = response["filter_resolution"]["mine_id"]
        self.assertEqual(resolution["resolved"], ["ADMM-01234"])
        self.assertEqual(resolution["via"], "as_supplied")

    def test_an_unresolved_filter_with_no_hits_says_it_was_unresolved(self):
        """count 0 from a filter that resolved to nothing is not evidence the
        corpus holds nothing: ASK has to fall back rather than tell a user the
        indexed documents do not answer their question."""
        response = self.search(lexical=[], arms=["lexical"],
                               filters={"mine_id": self.FRONT_END_ID},
                               mine_id_map={})
        self.assertEqual(response["status"], "loaded")
        self.assertEqual(response["count"], 0)
        self.assertEqual(response["filter_unresolved"], ["mine_id"])

    def test_hits_mean_the_filter_worked_however_it_resolved(self):
        """An id already in the corpus namespace has no map row and is still
        correct, so a result set must never carry the unresolved marker."""
        response = self.search(lexical=[row(303)], arms=["lexical"],
                               filters={"mine_id": "ADMM-01234"},
                               mine_id_map={})
        self.assertEqual(response["count"], 1)
        self.assertNotIn("filter_unresolved", response)


class DocumentListingTest(QueryLambdaTestCase):
    """op 'documents': the per-mine document list, at corpus scale.

    The defect it closes is a disagreement between two surfaces: ASK routed
    search_documents at 852,027 chunks while docs_for still answered from the
    3.2 MB SQLite slice, which holds exactly 2 documents. So the same mine had
    documents when you asked a question about it and none when you clicked it.

    The two properties this class exists to pin are that the count is a TRUE
    total rather than a by-product of an over-fetch, and that a document is
    never listed without the rights that make serving it defensible at all.
    """

    FRONT_END_ID = "stategeo-igs-dd-1-if0126"
    MAPPED = {FRONT_END_ID: ["IF0126", "ADMM-01234"]}

    def list_documents(self, documents=None, total=None, mine_id_map="__mapped__",
                       **event):
        if mine_id_map == "__mapped__":
            mine_id_map = dict(self.MAPPED)
        conn = FakeConn(documents=documents if documents is not None else [row(1)],
                        document_total=total, mine_id_map=mine_id_map)
        self.conn = conn
        payload = {"op": "documents",
                   "filters": {"mine_id": self.FRONT_END_ID}}
        payload.update(event)
        with mock.patch.object(ql, "_open_connection", lambda: conn):
            return ql.handler(payload, None)

    # -- the count -------------------------------------------------------
    def test_the_count_is_the_true_total_not_the_length_of_the_page(self):
        """A fused chunk search returns `limit` hits, so the number of
        distinct documents behind them is a property of over_fetch. This count
        is an aggregate over ws13_documents under the page's own predicate."""
        response = self.list_documents(documents=[row(1), row(2), row(3)],
                                       total=130)
        self.assertEqual(response["count"], 130)
        self.assertEqual(response["returned"], 3)
        self.assertEqual(len(response["documents"]), 3)

    def test_a_bounded_page_says_it_is_bounded(self):
        response = self.list_documents(documents=[row(1), row(2), row(3)],
                                       total=130)
        self.assertTrue(response["truncated"])
        self.assertEqual(response["next_offset"], 3)
        self.assertIn("page, not the set", response["note"])
        self.assertIn("130", response["note"])

    def test_a_complete_page_is_not_marked_truncated(self):
        """A note that always warns about truncation trains a reader to
        ignore it."""
        response = self.list_documents(documents=[row(1), row(2)], total=2)
        self.assertFalse(response["truncated"])
        self.assertIsNone(response["next_offset"])
        self.assertNotIn("page, not the set", response["note"])

    def test_the_count_survives_the_page_being_empty(self):
        """count comes from its own aggregate, so an offset past the end
        still reports how many documents there are."""
        response = self.list_documents(documents=[], total=4, offset=40)
        self.assertEqual(response["count"], 4)
        self.assertEqual(response["returned"], 0)
        self.assertIn("past the end", response["note"])

    def test_paging_runs_over_a_total_order(self):
        """Title is not unique in this corpus, and LIMIT/OFFSET over a partial
        order lets page 2 repeat a row page 1 showed and skip one nobody
        sees."""
        sql = ql.documents_sql({"mine_id": ["IF0126"]}, 12, 24)[0]
        self.assertTrue(sql.rstrip().endswith("LIMIT %s OFFSET %s"), sql)
        order = sql.split("ORDER BY")[1]
        self.assertIn("d.sha256", order.split("LIMIT")[0])

    def test_a_listing_with_no_filter_at_all_is_refused(self):
        """Without a filter this would page through all 56,282 documents."""
        for builder in (ql.documents_sql, ql.documents_count_sql):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(ValueError):
                    (builder({}, 12, 0) if builder is ql.documents_sql
                     else builder({}))

    def test_the_sha256_filter_is_spelled_against_the_document_table(self):
        """filter_sql() spells it `c.sha256`, which names no relation in a
        query whose only FROM is ws13_documents."""
        clauses, params = ql.documents_where({"sha256": "a" * 64})
        self.assertIn("d.sha256 = %s", clauses)
        self.assertEqual(params, ["a" * 64])

    # -- rights ----------------------------------------------------------
    def test_every_listed_document_carries_its_rights(self):
        response = self.list_documents(documents=[
            row(7, admission_class="licensed-copies", source_url=None,
                rights_basis="AZGS ADMMR")])
        document = response["documents"][0]
        for field in ("admission_class", "rights_basis", "rights_terms",
                      "attribution_required", "non_commercial", "share_alike"):
            self.assertIn(field, document, field)
        self.assertEqual(document["admission_class"], "licensed-copies")
        self.assertIn("CC BY-NC-SA", document["rights_terms"])
        self.assertIn("AZGS ADMMR", document["rights_terms"])
        self.assertTrue(document["attribution_required"])
        self.assertTrue(document["non_commercial"])
        self.assertTrue(document["share_alike"])

    def test_the_rights_also_travel_inside_the_citation(self):
        """The row is what a list renders and the citation is what a model
        pastes; attribution has to be unmissable in both."""
        response = self.list_documents(documents=[
            row(8, admission_class="research-copies", source_url=None,
                rights_basis="Idaho Geological Survey state archive")])
        citation = response["documents"][0]["citation"]
        self.assertEqual(citation["admission_class"], "research-copies")
        self.assertIn("Idaho Geological Survey", citation["rights_terms"])
        self.assertTrue(citation["attribution_required"])

    def test_a_document_whose_rights_cannot_be_stated_is_withheld(self):
        """13,013 licensed and 32,312 research copies name their licensor in
        their own terms, and rights_basis is NULL on every row until the
        provenance backfill runs. One such row must neither be published
        unattributable nor take the whole mine's list down with it."""
        response = self.list_documents(
            documents=[row(9, admission_class="licensed-copies",
                           rights_basis=None, source_url=None),
                       row(10)],
            total=2)
        self.assertEqual(response["returned"], 1)
        self.assertEqual(response["withheld_count"], 1)
        self.assertEqual([hit["sha256"] for hit in
                          response["withheld_documents"]], [f"{9:064x}"])
        # Counted, not lost: the total still knows about it and the note says
        # a document on this page was not listed.
        self.assertEqual(response["count"], 2)
        self.assertIn("counted but NOT listed", response["note"])

    def test_an_unknown_admission_class_is_withheld_not_listed(self):
        response = self.list_documents(
            documents=[row(11, admission_class="mystery")], total=1)
        self.assertEqual(response["documents"], [])
        self.assertEqual(response["withheld_count"], 1)

    def test_no_page_carries_a_document_whose_rights_are_not_stated(self):
        """All three admission classes on one page, row by row.

        A mine's file is mixed -- the corpus is 10,957 originals, 13,013
        CC BY-NC-SA licensed copies and 32,312 state-archive research copies,
        and one mine can attach documents from all three -- so "the rights
        travelled" has to hold for every row of a page rather than for the
        shape of one. The obligations are checked against the class as well:
        a page that carried the licensed copy's terms with the original's
        flags would read as public domain on the row a reader is most likely
        to reuse.
        """
        response = self.list_documents(
            documents=[
                row(21, admission_class="originals", rights_basis=None,
                    source_url=None),
                row(22, admission_class="licensed-copies",
                    rights_basis="AZGS ADMMR", source_url=None),
                row(23, admission_class="research-copies",
                    rights_basis="Idaho Geological Survey state archive",
                    source_url=None)],
            total=3)
        self.assertEqual(response["returned"], 3)
        self.assertNotIn("withheld_count", response)
        expected = {
            "originals": (False, False, False),
            "licensed-copies": (True, True, True),
            "research-copies": (True, True, False),
        }
        seen = []
        for document in response["documents"]:
            admission_class = document["admission_class"]
            seen.append(admission_class)
            self.assertIn(admission_class, expected, document)
            self.assertTrue(document["rights_terms"], document)
            self.assertEqual(
                (document["attribution_required"], document["non_commercial"],
                 document["share_alike"]), expected[admission_class],
                admission_class)
            # The row and the citation are two different surfaces -- a list
            # and a pasted reference -- and they must not disagree about the
            # licence of the same document.
            citation = document["citation"]
            for field in ("admission_class", "rights_basis", "rights_terms",
                          "attribution_required", "non_commercial",
                          "share_alike"):
                self.assertEqual(document[field], citation[field],
                                 f"{admission_class}.{field}")
            if admission_class != "originals":
                self.assertIn(document["rights_basis"],
                              document["rights_terms"])
        self.assertEqual(sorted(seen), sorted(expected))

    # -- the citation ----------------------------------------------------
    def test_the_listing_citation_claims_no_page(self):
        """Nothing here was matched to a page, and docs_for's own contract is
        that it does not invent one."""
        response = self.list_documents(documents=[row(12, source_url=None)])
        citation = response["documents"][0]["citation"]
        self.assertIsNone(citation["page"])
        self.assertNotIn("p. ", citation["markdown"])

    def test_the_stored_copy_chip_is_the_form_the_browser_parses(self):
        response = self.list_documents(documents=[row(13, source_url=None)])
        citation = response["documents"][0]["citation"]
        self.assertEqual(citation["resolvable_via"], "stored_copy")
        match = DOC_CHIP_RE.fullmatch(citation["markdown"])
        self.assertIsNotNone(match, citation["markdown"])
        self.assertEqual(match.group(2), f"{13:064x}")
        # The page group is optional in the browser rule and docChip opens at
        # page 1, so the reader gets the document without the answer ever
        # naming a page.
        self.assertIsNone(match.group(3))

    def test_a_listing_citation_never_publishes_an_object_key_as_the_link(self):
        response = self.list_documents(documents=[row(14, source_url=None)])
        citation = response["documents"][0]["citation"]
        self.assertNotIn("ws12/", citation["markdown"])
        self.assertNotIn("ws13/", citation["markdown"])
        # They stay in the payload as internal fields for the viewer
        # integration, exactly as they do on a search hit.
        self.assertTrue(citation["s3_key"].startswith("ws12/"))
        self.assertTrue(citation["viewer_key"])

    def test_a_published_source_url_still_resolves_through_the_publisher(self):
        response = self.list_documents(documents=[
            row(15, source_url="https://pubs.example.gov/report.pdf")])
        citation = response["documents"][0]["citation"]
        self.assertEqual(citation["resolvable_via"], "source_url")
        self.assertEqual(citation["markdown"],
                         "[Report 15](https://pubs.example.gov/report.pdf)")

    # -- the mine-id namespace bridge ------------------------------------
    def test_the_mine_predicate_matches_the_resolved_ids_by_overlap(self):
        """Identical to search(): one front-end id can resolve to several
        corpus ids, and && is the operator that matches a document carrying
        any of them while still using ws13_documents_mines."""
        response = self.list_documents()
        resolution = response["filter_resolution"]["mine_id"]
        self.assertEqual(resolution["requested"], self.FRONT_END_ID)
        self.assertEqual(sorted(resolution["resolved"]),
                         ["ADMM-01234", "IF0126"])
        self.assertEqual(resolution["via"], "ws13_mine_id_map")
        bound = [param for sql, params in self.conn.statements
                 if "d.mine_ids && %s::text[]" in sql
                 for param in params if isinstance(param, list)]
        self.assertTrue(bound, "the mine predicate never ran")
        self.assertNotIn([self.FRONT_END_ID], bound,
                         "the front-end id reached the corpus predicate")
        self.assertTrue(any(sorted(value) == ["ADMM-01234", "IF0126"]
                            for value in bound), bound)

    def test_a_missing_map_table_is_an_empty_map_not_an_error(self):
        """pipelines/ws13_migrate.py deliberately does not create
        ws13_mine_id_map, so this op has to work before it exists."""
        response = self.list_documents(mine_id_map=None, total=1)
        self.assertEqual(response["status"], "loaded")
        self.assertEqual(response["filter_resolution"]["mine_id"]["via"],
                         "as_supplied")

    def test_an_unmapped_id_reads_as_never_translated_not_as_no_documents(self):
        response = self.list_documents(documents=[], total=0, mine_id_map={})
        self.assertEqual(response["count"], 0)
        self.assertEqual(response["filter_unresolved"], ["mine_id"])
        self.assertIn("NEVER TRANSLATED", response["note"])
        self.assertIn("not that this mine has no documents", response["note"])

    def test_an_enumerated_unmapped_id_is_matched_against_nothing(self):
        """The Nevada collision, and the reason the two zero-answers are told
        apart.

        MRDS numbers Nevada deposits from 10006806 and NBMG numbered its
        mining-district files from 10000001 -- two unrelated namespaces of the
        same shape, 52 of whose integers collide. Falling back to the id as
        supplied answered a click on one district with another district's
        file, at confidence 1.0, and every check downstream read it as a
        translated hit. A row saying 'unmapped' is the bridge reporting it
        enumerated this id and could not translate it, so the honest predicate
        is one that matches nothing.
        """
        response = self.list_documents(documents=[], total=0,
                                       mine_id_map={self.FRONT_END_ID: []})
        self.assertEqual(response["count"], 0)
        self.assertEqual(response["filter_unresolved"], ["mine_id"])
        self.assertEqual(response["filter_resolution"]["mine_id"]["resolved"],
                         [])
        self.assertEqual(response["filter_resolution"]["mine_id"]["via"],
                         "ws13_mine_id_map_unmapped")
        self.assertIn("UNMAPPED", response["note"])
        self.assertIn("not that this mine has no documents", response["note"])
        bound = [param for sql, params in self.conn.statements
                 if "d.mine_ids && %s::text[]" in sql
                 for param in params if isinstance(param, list)]
        self.assertTrue(bound, "the mine predicate never ran")
        for value in bound:
            self.assertEqual(value, [], "the front-end id was matched anyway")

    def test_an_unmapped_row_does_not_drop_the_mine_predicate(self):
        """The predicate has to be BUILT and empty, not omitted. Read for
        truthiness an empty resolution looks like no mine filter at all, and
        the op would answer a mine with no documents with the whole corpus."""
        clauses, params = ql.document_clauses({"mine_id": []})
        self.assertIn("d.mine_ids && %s::text[]", clauses)
        self.assertIn([], params)

    def test_the_relation_of_a_place_level_match_is_reported(self):
        """A district file is not a document about this mine, it is a document
        about the district the mine sits in. The bridge can say so and the
        response has to carry it, or the distinction dies here."""
        response = self.list_documents(
            total=1, mine_id_map={self.FRONT_END_ID: {
                "ids": ["60000037"], "relation": "district",
                "confidence": 0.8, "verified": False}})
        self.assertEqual(response["filter_resolution"]["mine_id"]["relation"],
                         "district")

    def test_a_result_set_never_carries_the_unresolved_marker(self):
        """An id already in the corpus namespace has no map row and is still
        correct."""
        response = self.list_documents(mine_id_map={}, total=1)
        self.assertNotIn("filter_unresolved", response)

    def test_an_empty_page_of_a_real_result_is_not_an_unresolved_filter(self):
        """The marker sends ASK back to a 2-document index, so it has to mean
        "nothing matched", not "this page is past the end". search()'s test is
        `not hits`, which is the same thing only because search has no
        offset."""
        response = self.list_documents(documents=[], total=41, offset=100,
                                       mine_id_map={})
        self.assertEqual(response["count"], 41)
        self.assertNotIn("filter_unresolved", response)
        self.assertNotIn("NEVER TRANSLATED", response["note"])

    # -- shape -----------------------------------------------------------
    def test_the_shape_is_the_one_the_sqlite_document_tool_returns(self):
        """The browser and the model must need no special case for which
        stack answered."""
        response = self.list_documents(total=1)
        for key in ("status", "mine_id", "count", "documents"):
            self.assertIn(key, response)
        self.assertEqual(response["status"], "loaded")
        self.assertEqual(response["mine_id"], self.FRONT_END_ID)
        document = response["documents"][0]
        for key in ("document_id", "title", "portal", "mine_ids", "mine_names",
                    "state", "county", "doc_date", "doc_type", "page_count",
                    "indexed_pages", "source_url", "indexed", "embedded"):
            self.assertIn(key, document, key)
        self.assertEqual(document["document_id"], document["sha256"])

    def test_the_result_passes_the_sqlite_tools_own_validation(self):
        """_validate_result walks result['hits'] and demands an http(s)
        source_url on every citation there. This listing has no 'hits' key, so
        it passes -- vacuously, because there is nothing there to check, not
        because these citations were validated by it. Publishing the documents
        as hits would fail outright: source_url is NULL for most of the 56,282
        rows until the provenance backfill runs."""
        response = self.list_documents(documents=[row(16, source_url=None)])
        self.assertNotIn("hits", response)
        runtime_docs._validate_result(response)      # must not raise

    def test_indexed_pages_counts_pages_that_produced_chunks(self):
        """ws13_pages carries a row for every page rendered, blank ones
        included; only a page with a chunk can come back from a search."""
        response = self.list_documents(documents=[
            row(17, indexed_pages=42, unembedded_chunks=0)])
        document = response["documents"][0]
        self.assertEqual(document["indexed_pages"], 42)
        self.assertTrue(document["indexed"])
        self.assertTrue(document["embedded"])

    def test_a_document_with_no_indexed_page_is_listed_and_says_so(self):
        """It is still attached to the mine; it just cannot be cited."""
        response = self.list_documents(documents=[
            row(18, indexed_pages=0, unembedded_chunks=0)])
        document = response["documents"][0]
        self.assertEqual(document["indexed_pages"], 0)
        self.assertFalse(document["indexed"])
        self.assertFalse(document["embedded"])

    def test_a_partially_embedded_document_is_not_reported_as_embedded(self):
        response = self.list_documents(documents=[
            row(19, indexed_pages=9, unembedded_chunks=3)])
        self.assertFalse(response["documents"][0]["embedded"])

    def test_the_limit_and_offset_are_clamped(self):
        request = ql.normalize_documents_request(
            {"filters": {"mine_id": "x"}, "limit": 900, "offset": -3})
        self.assertEqual(request["limit"], ql.DOCUMENTS_LIMIT_MAX)
        self.assertEqual(request["offset"], 0)

    def test_a_clamped_offset_is_reported_rather_than_applied_silently(self):
        """`offset` in the response is the number this op used, and that is
        not the number the caller asked for once the cap bites. Without
        offset_clamped_from the two are indistinguishable, and a caller paging
        its way down reads the page at 10,000 as the page at 99,999."""
        response = self.list_documents(documents=[row(1), row(2)], total=50000,
                                       offset=99999)
        self.assertEqual(response["offset"], ql.DOCUMENTS_OFFSET_MAX)
        self.assertEqual(response["offset_clamped_from"], 99999)
        self.assertIn("was clamped to", response["note"])

    def test_no_cursor_is_emitted_past_the_offset_cap(self):
        """next_offset was computed unclamped while offset is clamped, so at
        the cap the response handed back a cursor that clamped straight back
        to the page it came from: identical rows, truncated still true,
        forever. Reachable for any filter selecting more than 10,000
        documents, which a bare `state` over 56,282 does."""
        response = self.list_documents(
            documents=[row(1), row(2), row(3)], total=50000,
            offset=ql.DOCUMENTS_OFFSET_MAX)
        self.assertTrue(response["truncated"])
        self.assertIsNone(response["next_offset"])
        self.assertIn("NOT reachable by paging", response["note"])

    def test_the_last_reachable_cursor_is_still_emitted(self):
        """The cap is a boundary, not a cliff: a next_offset that lands
        exactly on DOCUMENTS_OFFSET_MAX still resolves to the page it names,
        so withholding it would strand documents that are reachable."""
        response = self.list_documents(
            documents=[row(1), row(2), row(3)], total=50000,
            offset=ql.DOCUMENTS_OFFSET_MAX - 3)
        self.assertEqual(response["next_offset"], ql.DOCUMENTS_OFFSET_MAX)
        self.assertIn("next_offset carries the cursor", response["note"])

    def test_a_cursor_that_does_not_advance_is_not_published(self):
        """The count and the page are two statements, so a concurrent delete
        can leave an empty page under a total that still says there is more.
        next_offset would then equal offset, and a caller following it reads
        the same empty page forever."""
        response = self.list_documents(documents=[], total=50000, offset=5)
        self.assertTrue(response["truncated"])
        self.assertIsNone(response["next_offset"])
        self.assertIn("no cursor for the rest", response["note"])

    def test_the_listing_ships_the_page_less_citation_rule(self):
        """CITATION_RULE tells a reader both markdown forms end in ', p. N'.
        document_citation() emits neither -- nothing here was matched to a
        page -- so shipping the search rule beside a page-less markdown told
        the model to paste a page number that is not in the string it was
        told to paste verbatim."""
        response = self.list_documents(documents=[row(24, source_url=None)])
        rule = response["citation_rule"]
        self.assertEqual(rule, ql.DOCUMENT_CITATION_RULE)
        self.assertNotIn("p. N", rule)
        self.assertIn("[title](doc:<sha256>)", rule)
        self.assertIn("rights_terms", rule)
        self.assertIn("search_documents", rule)
        # The markdown beside it really is the form the rule describes.
        self.assertNotIn("p. ", response["documents"][0]["citation"]["markdown"])

    def test_the_response_is_json_serialisable(self):
        response = self.list_documents(documents=[row(20, source_url=None)])
        self.assertIsInstance(json.dumps(response), str)

    def test_the_op_is_reachable_through_the_handler(self):
        response = self.list_documents()
        self.assertEqual(response["op"], "documents")
        self.assertEqual(response["retrieval_mode"], "document_filter")

    # -- the cursor, and the budget the page size was measured against ----
    def test_a_withheld_document_does_not_shift_the_page_cursor(self):
        """next_offset counts rows the DATABASE returned, not rows that
        survived the rights check.

        The two numbers differ exactly when a document is withheld, and that
        is the case where the difference is destructive: LIMIT/OFFSET pages by
        rows, so a cursor advanced by ``len(listed)`` re-reads every withheld
        row on the next page and never reaches the end of the set. Here 3 rows
        come back, one is unattributable, and the next page has to start at 3.
        """
        response = self.list_documents(
            documents=[row(31), row(32, admission_class="licensed-copies",
                                     rights_basis=None, source_url=None),
                       row(33)],
            total=9)
        self.assertEqual(response["returned"], 2)
        self.assertEqual(response["withheld_count"], 1)
        self.assertTrue(response["truncated"])
        self.assertEqual(response["next_offset"], 3)
        self.assertIn("1-3 of 9", response["note"])

    def _fattest_page(self, rights_basis, limit):
        """The fattest realistic page: `limit` licensed copies, whose
        rights_terms names its licensor, with an 80-character title, both
        mine-id namespaces, two mine names, a 64-hex s3_key and viewer_key."""
        documents = [
            row(40 + index,
                sha256=f"{0xc0ffee + index:064x}",
                title="Mines and Prospects of the Cassia Quadrangle, Idaho: "
                      "Mine File IF0126, Volume II",
                admission_class="licensed-copies",
                rights_basis=rights_basis,
                source_url=None,
                mine_ids=["IF0126", "ADMM-01234"],
                mine_names=["St. Louis Mine", "Lava Creek Group"],
                pages=118)
            for index in range(limit)]
        response = self.list_documents(documents=documents, total=130,
                                       limit=limit)
        self.assertEqual(response["returned"], limit)
        return len(json.dumps(response))

    def test_the_default_page_fits_the_browser_json_budget_it_was_measured_against(self):
        """DOCUMENTS_LIMIT_DEFAULT is a measurement, and this is the
        measurement.

        Every tool result reaches the model through trimJson(result, 14000) in
        site/index.html, which POPS entries off `documents` and sets
        truncated=true until the JSON fits. That thinning happens in the
        BROWSER, after `returned`, `truncated` and `next_offset` were computed
        in the Lambda -- so a default page that does not fit is published as a
        response stating a page size it no longer has, over a cursor that
        skips exactly the rows the browser dropped. That is the bounded-
        result-reading-as-a-total failure this repo is emphatic about, with
        the browser rather than the Lambda doing the lying.

        The first version of this test was the defect. It built its rows with
        a 44-character rights_basis, measured a page of 6 at 13,303, and its
        docstring asserted that nothing in the corpus writes a longer one.
        pipelines/mine_file_harvest.py already did: LICENSED_BASIS_FLOOR is
        235 characters before a single interpolation and ~346 filled in, and
        rights_basis is serialised FOUR times per row -- raw and interpolated
        into rights_terms, on the row and again inside the citation -- so each
        character of it costs four. The same page of 6 measures 20,575
        characters with the filled-in basis, and the browser keeps 4 of them
        while `returned` still says 6 and next_offset still says 6.

        So the fixture is now the harvest's own string, read out of that
        pipeline's source rather than retyped here, and the page is measured
        again at 500 characters -- the longest basis any validator in this
        repo accepts (pipelines/build_doc_store.py:260).

        Two limits of this measurement, since neither is obvious from the
        number it asserts. It counts in json.dumps's default separators, which
        run ~2% long against the browser's own JSON.stringify -- the
        conservative side, and not the same units trimJson works in. And
        ws13_documents.rights_basis is unbounded TEXT: 500 is a convention
        borrowed from the WS12 registry validator, not a constraint this
        column has, so a longer basis would overflow the default too. This
        test is where that would surface, and the fix is the default page
        size, not this assertion.
        """
        for label, basis in (("harvest floor", LICENSED_BASIS_FLOOR),
                             ("harvest, filled in", LICENSED_BASIS_REAL),
                             ("the 500-character ceiling",
                              LICENSED_BASIS_CEILING)):
            with self.subTest(basis=label):
                serialised = self._fattest_page(basis,
                                                ql.DOCUMENTS_LIMIT_DEFAULT)
                self.assertLessEqual(
                    serialised, 14000,
                    f"a default page of licensed copies with a "
                    f"{len(basis)}-character rights_basis serialises to "
                    f"{serialised} characters, and the browser thins anything "
                    f"over 14,000 AFTER returned/truncated were computed here")

    def test_the_default_page_is_the_largest_one_that_fits(self):
        """The other half of the measurement, so the constant cannot quietly
        drift downwards either: one more document than the default overflows
        the budget at the 500-character ceiling the page above was sized for.
        Note what this does NOT say -- at the realistic 346-character basis a
        page of 4 comes to 13,810 characters in the browser's own units and
        does fit, by 190 characters. The default is 3 because 190 characters
        of headroom over an unbounded TEXT column is not a margin, not because
        4 is impossible. If this test starts failing the rows got cheaper, and
        the default is costing the model documents it could have had."""
        serialised = self._fattest_page(LICENSED_BASIS_CEILING,
                                        ql.DOCUMENTS_LIMIT_DEFAULT + 1)
        self.assertGreater(serialised, 14000)

    def test_the_browser_still_thins_the_key_this_default_was_sized_for(self):
        """The 14,000 budget above is not a constant this stack owns.

        trimJson pops entries off a fixed list of array keys, and 'documents'
        being on that list is the whole reason a listing can be thinned at
        all. If the browser's cap or its key list moves, DOCUMENTS_LIMIT_
        DEFAULT was measured against a budget that no longer exists.
        """
        source = Path(ROOT, "site", "index.html").read_text(encoding="utf-8")
        trim = source.split("function trimJson(o, max=10000){", 1)
        self.assertEqual(len(trim), 2, "trimJson is not where this was measured")
        body = trim[1].split("\n}", 1)[0]
        self.assertIn("'documents'", body,
                      "trimJson no longer thins the listing's own array")
        self.assertIn("o.truncated=true", body.replace(" ", ""))
        self.assertIn("trimJson(local,14000)", source.replace(" ", ""),
                      "the browser's tool-result budget is no longer 14,000")


class MineIdCaseSpellingTest(QueryLambdaTestCase):
    """The bridge carries spellings; it must never invent or fold them.

    mine_file_harvest.py seeds ws13_documents.mine_ids from a raw survey
    attribute and does not case-fold it, so 'SP0145' and 'sp0145' are two
    different values to ws13_documents_mines -- a GIN index whose only
    operators are exact-match. pipelines/ws13_mine_id_map.py:61-68 records
    every stored spelling in ws13_mine_id_all for exactly that reason and
    states that a reader must match against the whole array.

    Two things follow, and they pull in opposite directions, which is why
    they are pinned together here:

      * Nothing between the map row and the bound parameter may normalise a
        case. A .lower() added anywhere on this path would not raise, would
        not warn, and would match nothing at all in a corpus whose ids are
        mostly upper case.
      * Nothing here may GUESS a spelling either. The map is the authority on
        which spellings exist; deriving 'sp0145' from 'SP0145' in the Lambda
        would put an id in the predicate that no pipeline ever recorded.

    The open half of this -- that the live reader still SELECTs ws13_mine_id
    alone, so the array it overlaps against is the primary spelling only --
    is pinned by the last test in this class, as a tripwire and NOT as an
    endorsement.
    """

    FRONT_END_ID = "stategeo-igs-sp-145"

    def bound_mine_ids(self):
        """Every list parameter bound to the mine overlap predicate."""
        return [param for sql, params in self.conn.statements
                if "d.mine_ids && %s::text[]" in sql
                for param in params if isinstance(param, list)]

    def cold_container(self):
        """Drop the cached connection between two requests in one test.

        ``connection()`` keeps _CONN for the life of the container, which is
        the behaviour test_a_warm_container_reuses_one_connection pins. Left
        alone it also means a second request inside one test keeps talking to
        the FIRST test case's fake, and every assertion about what the second
        one bound reads an empty statement log.
        """
        ql._CONN = None

    def test_a_resolved_spelling_is_bound_verbatim(self):
        """Byte-for-byte what the map row held, in both directions of case."""
        for corpus_id in ("SP0145", "sp0145"):
            with self.subTest(corpus_id=corpus_id):
                self.cold_container()
                self.search(lexical=[row(401)], arms=["lexical"],
                            filters={"mine_id": self.FRONT_END_ID},
                            mine_id_map={self.FRONT_END_ID: [corpus_id]})
                self.assertEqual(self.bound_mine_ids(), [[corpus_id]])

    def test_no_case_variant_is_invented_for_a_spelling_the_map_did_not_hold(self):
        """The map is the authority on which spellings exist.

        Deriving the other case here would be the retrieval layer guessing at
        corpus contents -- the same class of move as resolving an 8-character
        sha256 prefix -- and it would do it in the one place where a wrong
        guess silently serves another mine's documents.
        """
        self.search(lexical=[row(402)], arms=["lexical"],
                    filters={"mine_id": self.FRONT_END_ID},
                    mine_id_map={self.FRONT_END_ID: ["SP0145"]})
        for bound in self.bound_mine_ids():
            self.assertEqual(bound, ["SP0145"])
            self.assertNotIn("sp0145", bound)

    def test_every_spelling_the_map_returns_reaches_one_overlap_predicate(self):
        """Several spellings of one mine are one predicate, not several.

        This is the transport ws13_mine_id_all needs: && matches a document
        carrying ANY member, and it is the operator that can carry a
        multi-spelling resolution at all -- `= ANY(d.mine_ids)` would scan all
        56,282 documents and `@>` would demand the document carry every
        spelling at once.
        """
        for op in ("search", "documents"):
            with self.subTest(op=op):
                self.cold_container()
                mapped = {self.FRONT_END_ID: ["SP0145", "sp0145", "SP-145"]}
                if op == "search":
                    self.search(lexical=[row(403)], arms=["lexical"],
                                filters={"mine_id": self.FRONT_END_ID},
                                mine_id_map=mapped)
                else:
                    conn = FakeConn(documents=[row(403)], document_total=1,
                                    mine_id_map=mapped)
                    self.conn = conn
                    with mock.patch.object(ql, "_open_connection", lambda: conn):
                        ql.handler({"op": "documents",
                                    "filters": {"mine_id": self.FRONT_END_ID}},
                                   None)
                bound = self.bound_mine_ids()
                self.assertTrue(bound, "the mine predicate never ran")
                for value in bound:
                    self.assertEqual(sorted(value),
                                     ["SP-145", "SP0145", "sp0145"])

    def test_the_predicate_never_wraps_mine_ids_in_a_function(self):
        """lower(d.mine_ids::text) would make the case problem disappear and
        take ws13_documents_mines with it: an expression over the array is not
        the indexed expression, so the mine filter would fall back to a
        sequential scan over 56,282 rows inside the 30 s gateway budget."""
        for builder, filters in (
                (ql.documents_where, {"mine_id": ["SP0145"]}),
                (ql.document_clauses, {"mine_id": ["SP0145"]})):
            with self.subTest(builder=builder.__name__):
                clauses, _ = builder(filters)
                predicate = [item for item in clauses if "mine_ids" in item]
                self.assertEqual(predicate, ["d.mine_ids && %s::text[]"])

    def test_the_reader_selects_every_spelling(self):
        """The limit this class was a tripwire on, now closed.

        pipelines/ws13_mine_id_map.py:154-166 states the reader query the
        table was built for -- ws13_mine_id_all, and the admission rule -- and
        mine_id_map() selected ws13_mine_id alone and applied neither guard.
        Documents filed under another spelling were lost, and a difflib guess
        capped at 0.6 could scope a query. Both guards moved across together,
        because the array without the confidence filter turns that guess into
        a silently scoped search over MORE documents than before.
        """
        self.search(lexical=[row(404)], arms=["lexical"],
                    filters={"mine_id": self.FRONT_END_ID},
                    mine_id_map={self.FRONT_END_ID: ["SP0145", "sp0145"]})
        bridge = [sql for sql, _ in self.conn.statements
                  if "ws13_mine_id_map" in sql]
        self.assertEqual(len(bridge), 1, bridge)
        self.assertIn("ws13_mine_id_all", bridge[0])
        for value in self.bound_mine_ids():
            self.assertEqual(sorted(value), ["SP0145", "sp0145"])
        # The contract this implements has to still be stated where the table
        # is defined, or the next reader has nothing to check the code against.
        pipeline = Path(ROOT, "pipelines", "ws13_mine_id_map.py").read_text(
            encoding="utf-8")
        self.assertIn("ws13_mine_id_all", pipeline)
        self.assertIn("verified OR confidence >= 0.8", pipeline)

    def test_a_low_confidence_row_does_not_scope_the_search(self):
        """4,060 fuzzy_name rows are in the table, every one of them a difflib
        guess the builder caps at 0.6 and refuses to mark verified. They were
        all being used. They bought five reachable documents and put four
        thousand front-end ids one tie away from serving the wrong mine."""
        self.search(lexical=[row(405)], arms=["lexical"],
                    filters={"mine_id": self.FRONT_END_ID},
                    mine_id_map={self.FRONT_END_ID: {
                        "ids": ["SP0145"], "confidence": 0.6,
                        "verified": False, "relation": "identity"}})
        for value in self.bound_mine_ids():
            self.assertEqual(value, [])

    def test_a_verified_row_is_admitted_whatever_its_confidence(self):
        """The rule is `verified OR confidence >= 0.8`, not the threshold
        alone: a human-confirmed row is a mapping by fiat."""
        self.search(lexical=[row(406)], arms=["lexical"],
                    filters={"mine_id": self.FRONT_END_ID},
                    mine_id_map={self.FRONT_END_ID: {
                        "ids": ["SP0145"], "confidence": 0.1,
                        "verified": True}})
        for value in self.bound_mine_ids():
            self.assertEqual(value, ["SP0145"])

    def test_a_table_without_the_relation_column_still_bridges(self):
        """The deployed table has no relation column until
        pipelines/ws13_mine_id_map.py migrates it, and this Lambda may ship
        first. A 42703 on the wide projection has to demote to the legacy one,
        not take the bridge out -- that would drop Idaho from 25,820 reachable
        documents to zero."""
        conn = FakeConn(lexical=[row(407)],
                        mine_id_map={self.FRONT_END_ID: ["SP0145"]},
                        mine_map_has_relation=False)
        self.conn = conn
        with mock.patch.dict(os.environ, {"WS13_VECTOR_ARM": "false"}), \
                mock.patch.object(ql, "_open_connection", lambda: conn):
            response = ql.handler(
                {"op": "search", "query": "lava creek", "limit": 25,
                 "filters": {"mine_id": self.FRONT_END_ID}}, None)
        self.assertEqual(
            response["filter_resolution"]["mine_id"]["resolved"], ["SP0145"])
        for value in self.bound_mine_ids():
            self.assertEqual(value, ["SP0145"])

    def test_the_demoted_projection_is_learned_once(self):
        """Re-learning it would cost a failed statement on every mine-filtered
        request against a pre-migration table, and the answer cannot change
        under a running container."""
        attempts = []
        for _ in range(2):
            # A fresh connection each time, as a warm container gets on a
            # reconnect; the projection is what has to survive, not the socket.
            ql._CONN = None
            conn = FakeConn(lexical=[row(408)],
                            mine_id_map={self.FRONT_END_ID: ["SP0145"]},
                            mine_map_has_relation=False)
            self.conn = conn
            with mock.patch.dict(os.environ, {"WS13_VECTOR_ARM": "false"}), \
                    mock.patch.object(ql, "_open_connection", lambda: conn):
                ql.handler({"op": "search", "query": "lava creek", "limit": 25,
                            "filters": {"mine_id": self.FRONT_END_ID}}, None)
            attempts.append([sql for sql, _ in conn.statements
                             if "ws13_mine_id_map" in sql])
        # First invocation: the wide projection, then the demotion. Second:
        # the demoted projection alone.
        self.assertEqual(len(attempts[0]), 2, attempts[0])
        self.assertEqual(len(attempts[1]), 1, attempts[1])
        self.assertNotIn("relation", attempts[1][0])


class PlanGuardTest(QueryLambdaTestCase):
    """The Seq Scan guard, actually executed.

    Until this class existed the EXPLAIN branch of ``FakeConn`` was dead code:
    assert_plan_once() returns immediately unless WS13_ASSERT_PLAN is 'true'
    and no test set it, so neither the Seq Scan detection nor the plan
    contract had any coverage at all. An inverted condition would have shipped
    green, leaving a static string comparison as the only defence against the
    sequential scan over 852,027 rows this suite is named for.
    """

    SEQ_SCAN_PLAN = [
        "Limit  (cost=41893.02..41893.05 rows=200 width=12)",
        "  ->  Sort  (cost=41893.02..44023.09 rows=852027 width=12)",
        "        Sort Key: ((titan_embedding)::halfvec(1024) <=> '[...]')",
        "        ->  Seq Scan on ws13_chunks c  (rows=852027 width=12)",
    ]
    ASSERT_PLAN = {"WS13_ASSERT_PLAN": "true"}

    def test_the_gate_probe_is_a_plain_explain_never_explain_analyze(self):
        """EXPLAIN (ANALYZE) EXECUTES the statement, so a guard against a
        sequential scan would run that scan to discover it was there --
        minutes against production RDS, inside the request it protects."""
        self.assertTrue(ql.EXPLAIN_SQL.upper().startswith("EXPLAIN "))
        self.assertNotIn("ANALYZE", ql.EXPLAIN_SQL.upper())
        self.assertIn(ql.ORDER_BY_SQL, ql.EXPLAIN_SQL)
        self.assertIn("LIMIT", ql.EXPLAIN_SQL.upper())
        # The operator's deliberate --measure probe keeps ANALYZE and BUFFERS
        # and is a separate constant, so a gate cannot reach it by accident.
        self.assertIn("ANALYZE", ql.EXPLAIN_ANALYZE_SQL.upper())
        self.assertIn("BUFFERS", ql.EXPLAIN_ANALYZE_SQL.upper())
        self.assertIn(ql.ORDER_BY_SQL, ql.EXPLAIN_ANALYZE_SQL)

    def test_a_seq_scan_plan_is_reported_to_the_caller_not_only_logged(self):
        with self.assertLogs(ql.LOG, level="ERROR") as logs:
            response = self.search(lexical=[row(310)], vector=[row(310)],
                                   plan=self.SEQ_SCAN_PLAN,
                                   environment=self.ASSERT_PLAN)
        warning = response["arms"]["vector"]["plan_warning"]
        self.assertIsNotNone(warning, "a Seq Scan plan produced no warning")
        self.assertIn(ql.INDEX_NAME, warning)
        self.assertIn(ql.INDEX_NAME, "\n".join(logs.output))

    def test_a_plan_that_names_another_index_is_also_a_failure(self):
        """The arm is only correct when the HNSW index is the one chosen;
        ws13_chunks_sha alone answers the filter and none of the ordering."""
        with self.assertLogs(ql.LOG, level="ERROR"):
            response = self.search(
                lexical=[row(313)], vector=[row(313)],
                plan=["Index Scan using ws13_chunks_sha on ws13_chunks c"],
                environment=self.ASSERT_PLAN)
        self.assertIsNotNone(response["arms"]["vector"]["plan_warning"])

    def test_a_good_plan_leaves_no_warning(self):
        response = self.search(lexical=[row(311)], vector=[row(311)],
                               environment=self.ASSERT_PLAN)
        self.assertIsNone(response["arms"]["vector"]["plan_warning"])

    def test_the_gate_explains_the_statement_this_request_will_run(self):
        """Real requests carry filters and filter_sql() changes the shape, so
        EXPLAINing the bare probe certifies a plan no request issues."""
        response = self.search(lexical=[row(312)], vector=[row(312)],
                               filters={"state": "ID"},
                               environment=self.ASSERT_PLAN)
        explains = [sql for sql, _ in self.conn.statements
                    if sql.upper().startswith("EXPLAIN")]
        self.assertEqual(len(explains), 1, explains)
        self.assertNotIn("ANALYZE", explains[0].upper())
        self.assertIn(ql.ORDER_BY_SQL, explains[0])
        probe = explains[0][len("EXPLAIN "):]
        strategy = response["arms"]["vector"]["filter_strategy"]
        self.assertIn(strategy, ("sha_set", "semi_join"))
        # The EXPLAINed text is the probe itself, not a stand-in for it.
        self.assertTrue(probe.startswith("SELECT c.id AS chunk_id"), probe)
        self.assertIn("WHERE", probe.split("ORDER BY")[0])

    def test_the_plan_is_asserted_once_per_filter_shape_not_once_ever(self):
        """One unfiltered request must not suppress the check for every
        filtered request after it: the filtered shape is the one that loses
        the index."""
        conn = FakeConn(lexical=[row(314)], vector=[row(314)])
        with mock.patch.dict(os.environ, dict(self.ASSERT_PLAN,
                                              WS13_VECTOR_ARM="true")), \
                mock.patch.object(ql, "_open_connection", lambda: conn):
            for filters in ({}, {}, {"state": "ID"}, {"state": "ID"}):
                ql.handler({"op": "search", "query": "silver",
                            "query_vector": list(UNIT_VECTOR),
                            "filters": filters, "limit": 5}, None)
        explains = [sql for sql, _ in conn.statements
                    if sql.upper().startswith("EXPLAIN")]
        self.assertEqual(len(explains), 2, explains)

    def test_the_plan_gate_is_off_unless_the_operator_asks_for_it(self):
        self.search(lexical=[row(315)], vector=[row(315)],
                    environment={"WS13_ASSERT_PLAN": "false"})
        self.assertEqual([sql for sql, _ in self.conn.statements
                          if sql.upper().startswith("EXPLAIN")], [])


class IndexContractPlanTest(unittest.TestCase):
    """pipelines/ws13_index_contract.plan_problems(), which had no test.

    It is the pure half of the deploy gate: ws13_build_ann_index.py --verify
    hands it the live plan text, so an inverted condition or a label pgvector
    renders differently would pass a build that seq-scans.
    """

    GOOD = ("Limit  (cost=88.24..97.90 rows=200 width=12)\n"
            "  ->  Index Scan using ws13_chunks_titan_hnsw on ws13_chunks c\n"
            "        Order By: ((titan_embedding)::halfvec(1024) <=> '[...]')")
    SEQ = ("Limit  (cost=41893.02..41893.05 rows=200 width=12)\n"
           "  ->  Sort  (cost=41893.02..44023.09 rows=852027 width=12)\n"
           "        ->  Seq Scan on ws13_chunks c  (rows=852027 width=12)")

    def test_a_good_plan_has_no_problems(self):
        self.assertEqual(index_contract.plan_problems(self.GOOD), [])

    def test_a_sequential_scan_is_reported_as_both_faults(self):
        problems = index_contract.plan_problems(self.SEQ)
        self.assertTrue(any(ql.INDEX_NAME in text for text in problems))
        self.assertTrue(any("Seq Scan" in text for text in problems))

    def test_a_parallel_sequential_scan_is_still_a_sequential_scan(self):
        problems = index_contract.plan_problems(
            "Gather\n  ->  Parallel Seq Scan on ws13_chunks c")
        self.assertTrue(any("Seq Scan" in text for text in problems))

    def test_an_empty_plan_is_a_problem_not_a_pass(self):
        for plan in ("", "   ", None):
            with self.subTest(plan=plan):
                self.assertEqual(len(index_contract.plan_problems(plan)), 1)

    def test_an_index_on_another_relation_does_not_satisfy_the_contract(self):
        problems = index_contract.plan_problems(
            "Index Scan using ws13_documents_pkey on ws13_documents d")
        self.assertTrue(any(ql.INDEX_NAME in text for text in problems))

    def test_assert_plan_raises_and_quotes_the_plan(self):
        with self.assertRaises(index_contract.ContractError) as caught:
            index_contract.assert_plan(self.SEQ)
        self.assertIn("Seq Scan", str(caught.exception))
        index_contract.assert_plan(self.GOOD)      # must not raise


class FakeBedrock:
    """Titan v2 embed, or whatever failure the test wants instead."""

    def __init__(self):
        self.calls = []
        self.embedding = [0.03125] * 1024
        self.error = None

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        payload = json.dumps({"embedding": list(self.embedding)}).encode()
        return {"body": io.BytesIO(payload)}


class FakeLambdaClient:
    def __init__(self):
        self.calls = []
        self.result = {"status": "loaded", "count": 0, "hits": []}
        self.function_error = None
        self.error = None

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        response = {"Payload": io.BytesIO(json.dumps(self.result).encode())}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response

    def payload(self, index=0):
        return json.loads(self.calls[index]["Payload"].decode("utf-8"))


class FakeAws:
    """Just enough boto3 to import infra/ask_lambda and drive its WS13 path."""

    def __init__(self):
        self.bedrock = FakeBedrock()
        self.lambda_client = FakeLambdaClient()
        self.other = mock.MagicMock()

    def client(self, name, **kwargs):
        if name == "bedrock-runtime":
            return self.bedrock
        if name == "lambda":
            return self.lambda_client
        return self.other


def load_ask_lambda(aws, **environment):
    """A fresh infra/ask_lambda over a stubbed boto3 and a given environment.

    WS13_RETRIEVAL_ENABLED and friends are read at import time -- that is what
    makes the dark rollout byte-for-byte the old behaviour when the flag is
    off -- so proving both sides of the switch means importing twice rather
    than mutating the module afterwards.
    """
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = aws.client
    config_stub = types.ModuleType("botocore.config")
    config_stub.Config = lambda **kwargs: kwargs

    class ClientError(Exception):
        def __init__(self, code="AccessDeniedException"):
            self.response = {"Error": {"Code": code}}
            super().__init__(code)

    exceptions_stub = types.ModuleType("botocore.exceptions")
    exceptions_stub.ClientError = ClientError
    botocore_stub = types.ModuleType("botocore")
    botocore_stub.config = config_stub
    botocore_stub.exceptions = exceptions_stub
    modules = {"boto3": boto3_stub, "botocore": botocore_stub,
               "botocore.config": config_stub,
               "botocore.exceptions": exceptions_stub}
    with mock.patch.dict(os.environ, environment), \
            mock.patch.dict(sys.modules, modules):
        sys.modules.pop("ask_lambda", None)
        module = importlib.import_module("ask_lambda")
        sys.modules.pop("ask_lambda", None)
    return module


def ws13_citation(**changes):
    """A stored-copy citation exactly as ws13_query_lambda emits one."""
    sha256 = "3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1"
    citation = {
        "document_title": "Mines and Prospects of Idaho, DD-1",
        "page": 4,
        "source_url": None,
        "markdown": f"[Mines and Prospects of Idaho, DD-1, p. 4](doc:{sha256}#4)",
        "sha256": sha256,
        "s3_key": f"ws12/research-copies/3c/{sha256}.pdf",
        "viewer_key": f"ws13/searchable/3c/{sha256}/searchable.pdf",
        "viewer_key_kind": "searchable",
        "admission_class": "research-copies",
        "rights_basis": "Idaho Geological Survey state archive",
        "rights_terms": "state-archive research copy, not redistributable",
        "attribution_required": True, "non_commercial": True,
        "share_alike": False, "resolvable_via": "stored_copy",
    }
    citation.update(changes)
    return citation


def ws13_document(**changes):
    """One listed document exactly as ws13_query_lambda.documents() emits one."""
    sha256 = "3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1"
    citation = ws13_citation(page=None, markdown=f"[Mines and Prospects of "
                                                 f"Idaho, DD-1](doc:{sha256})")
    document = {
        "document_id": sha256, "sha256": sha256,
        "title": "Mines and Prospects of Idaho, DD-1",
        "portal": "igs-mines", "state": "ID", "county": "Cassia County",
        "page_count": 118, "indexed_pages": 118, "source_url": None,
        "indexed": True, "embedded": True,
        "admission_class": "research-copies",
        "rights_basis": "Idaho Geological Survey state archive",
        "rights_terms": "state-archive research copy, not redistributable",
        "attribution_required": True, "non_commercial": True,
        "share_alike": False, "citation": citation,
    }
    document.update(changes)
    return document


def ws13_document_list(**changes):
    result = {"status": "loaded", "op": "documents",
              "mine_id": "stategeo-igs-dd-1-if0126", "count": 3, "returned": 1,
              "documents": [ws13_document()], "truncated": True,
              "next_offset": 1}
    result.update(changes)
    return result


class AskWs13RoutingTest(unittest.TestCase):
    """infra/ask_lambda's WS13 path: what it sends, and when it gives up.

    The rule the whole path is built around is that a retrieval upgrade may
    degrade an answer but must never take the document tool offline, so every
    failure here has to end at the bounded SQLite index with a reason
    attached.
    """

    ENVIRONMENT = {"WS13_RETRIEVAL_ENABLED": "true",
                   "WS13_RETRIEVAL_FUNCTION": "nwmm-ws13-query",
                   "WS13_VECTOR_ARM": "true"}

    def setUp(self):
        self.aws = FakeAws()
        self.ask = load_ask_lambda(self.aws, **self.ENVIRONMENT)

    def test_the_query_is_embedded_on_the_corpus_contract(self):
        """The retrieval function is VPC-attached with no egress and cannot
        embed anything itself, so these 1024 floats are the only way its
        vector arm can run at all."""
        vector = self.ask._ws13_query_vector("lava creek silver assay")
        self.assertEqual(len(vector), 1024)
        call = self.aws.bedrock.calls[0]
        self.assertEqual(call["modelId"], "amazon.titan-embed-text-v2:0")
        body = json.loads(call["body"])
        self.assertEqual(body["inputText"], "lava creek silver assay")
        self.assertEqual(body["dimensions"], 1024)
        self.assertIs(body["normalize"], True)

    def test_a_wrong_width_embedding_is_refused_not_shipped(self):
        """titan_embedding is vector(1024); 1536 floats would be rejected by
        Postgres as a type error at the far end of a VPC hop."""
        self.aws.bedrock.embedding = [0.1] * 1536
        with self.assertRaises(ValueError):
            self.ask._ws13_query_vector("silver")

    def test_the_search_ships_the_vector_and_only_the_known_filters(self):
        result, reason = self.ask._ws13_search(
            {"query": "silver assay", "mine_id": "stategeo-igs-dd-1-if0126",
             "state": "ID", "unknown_key": "dropped", "limit": 400})
        self.assertIsNone(reason)
        self.assertIsNotNone(result)
        payload = self.aws.lambda_client.payload()
        self.assertEqual(payload["op"], "search")
        self.assertEqual(len(payload["query_vector"]), 1024)
        self.assertEqual(payload["filters"],
                         {"mine_id": "stategeo-igs-dd-1-if0126", "state": "ID"})
        self.assertEqual(payload["limit"], 25)          # clamped from 400
        self.assertEqual(result["retrieval_source"], "ws13")

    def test_an_embedding_failure_still_searches_on_the_lexical_arm(self):
        """Losing the vector arm is a worse answer; losing the tool is no
        answer."""
        self.aws.bedrock.error = RuntimeError("bedrock throttled")
        result, reason = self.ask._ws13_search({"query": "silver assay"})
        self.assertIsNone(reason)
        self.assertIsNone(self.aws.lambda_client.payload()["query_vector"])
        self.assertIn("bedrock throttled", result["embedding_error"])

    def test_a_function_error_is_a_miss_not_a_result_set(self):
        """An unhandled exception inside WS13 still returns HTTP 200 with a
        FunctionError header, so a bare status check reads a traceback as
        hits."""
        self.aws.lambda_client.function_error = "Unhandled"
        self.aws.lambda_client.result = {"errorMessage": "boom"}
        result, reason = self.ask._ws13_search({"query": "silver"})
        self.assertIsNone(result)
        self.assertIn("Unhandled", reason)

    def test_a_non_loaded_status_is_a_miss(self):
        self.aws.lambda_client.result = {"status": "not_loaded"}
        result, reason = self.ask._ws13_search({"query": "silver"})
        self.assertIsNone(result)
        self.assertIn("status", reason)

    def test_a_transport_failure_is_a_miss(self):
        self.aws.lambda_client.error = RuntimeError("connect timeout")
        result, reason = self.ask._ws13_search({"query": "silver"})
        self.assertIsNone(result)
        self.assertIn("connect timeout", reason)

    def test_an_unresolved_filter_is_a_miss_not_an_authoritative_zero(self):
        """The front end emits 'stategeo-igs-dd-1-if0126' and
        ws13_documents.mine_ids holds bare IGS codes, so an unbridged mine
        filter returns 0 of 56,282 documents. Relaying that as a result would
        have the model say the indexed documents do not answer a question
        whose documents are in the corpus."""
        self.aws.lambda_client.result = {
            "status": "loaded", "count": 0, "hits": [],
            "filter_unresolved": ["mine_id"]}
        result, reason = self.ask._ws13_search(
            {"query": "production", "mine_id": "stategeo-igs-dd-1-if0126"})
        self.assertIsNone(result)
        self.assertIn("mine_id", reason)
        self.assertIn("resolve", reason)

    def test_an_unresolved_filter_that_still_found_hits_is_kept(self):
        """WS13 only reports the marker on a zero-hit response; a result set
        is a result set."""
        self.aws.lambda_client.result = {
            "status": "loaded", "count": 1,
            "hits": [{"citation": ws13_citation()}]}
        result, reason = self.ask._ws13_search({"query": "production"})
        self.assertIsNone(reason)
        self.assertEqual(result["count"], 1)

    def test_every_miss_falls_back_to_sqlite_and_says_why(self):
        self.aws.lambda_client.result = {
            "status": "loaded", "count": 0, "hits": [],
            "filter_unresolved": ["mine_id"]}
        stub = types.ModuleType("document_tools")
        stub.TOOL_NAMES = frozenset({"search_documents", "docs_for"})
        stub.execute = lambda name, arguments: {"status": "loaded",
                                                "retrieval_source": "sqlite",
                                                "hits": []}
        with mock.patch.dict(sys.modules, {"document_tools": stub}):
            result = self.ask.execute_local_tool(
                "search_documents",
                {"query": "production", "mine_id": "stategeo-igs-dd-1-if0126"})
        self.assertEqual(result["retrieval_source"], "sqlite")
        self.assertIn("mine_id", result["ws13_fallback_reason"])

    def test_a_stale_stored_copy_markdown_is_rewritten_to_the_chip(self):
        """Version skew between the two stacks lands here: a retrieval
        deployment still emitting "(stored copy: <s3 key>)" would put a
        private object key in front of the reader."""
        stale = ws13_citation(
            markdown="Mines and Prospects of Idaho, DD-1, p. 4 (stored copy: "
                     "ws12/research-copies/3c/if0131.pdf)")
        self.aws.lambda_client.result = {
            "status": "loaded", "count": 1, "hits": [{"citation": stale}]}
        result, _ = self.ask._ws13_search({"query": "production"})
        markdown = result["hits"][0]["citation"]["markdown"]
        self.assertNotIn("ws12/research-copies", markdown)
        self.assertRegex(markdown, DOC_CHIP_RE)

    # -- docs_for, the per-mine document list -----------------------------
    def test_the_document_list_asks_for_the_documents_op(self):
        """Routed at the corpus behind the same flag as search_documents, so
        "ask a question" and "click a mine" cannot answer from different
        archives."""
        self.aws.lambda_client.result = ws13_document_list()
        result, reason = self.ask._ws13_docs_for(
            {"mine_id": "stategeo-igs-dd-1-if0126"})
        self.assertIsNone(reason)
        payload = self.aws.lambda_client.payload()
        self.assertEqual(payload["op"], "documents")
        self.assertEqual(payload["filters"],
                         {"mine_id": "stategeo-igs-dd-1-if0126"})
        self.assertEqual(result["retrieval_source"], "ws13")

    def test_the_document_list_never_spends_an_embedding(self):
        """The op ranks nothing, so a Titan round trip -- or a Titan throttle
        -- would buy the listing exactly nothing."""
        self.aws.lambda_client.result = ws13_document_list()
        self.ask._ws13_docs_for({"mine_id": "stategeo-igs-dd-1-if0126"})
        self.assertEqual(self.aws.bedrock.calls, [])
        self.assertNotIn("query_vector", self.aws.lambda_client.payload())

    def test_the_count_and_the_page_reach_the_caller_apart(self):
        self.aws.lambda_client.result = ws13_document_list()
        result, _ = self.ask._ws13_docs_for(
            {"mine_id": "stategeo-igs-dd-1-if0126"})
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["documents"]), 1)
        self.assertTrue(result["truncated"])

    def test_docs_for_without_a_mine_id_never_spends_a_ws13_invoke(self):
        result, reason = self.ask._ws13_docs_for({})
        self.assertIsNone(result)
        self.assertIn("mine_id", reason)
        self.assertEqual(self.aws.lambda_client.calls, [])

    def test_a_function_error_is_a_miss_not_a_document_list(self):
        self.aws.lambda_client.function_error = "Unhandled"
        self.aws.lambda_client.result = {"errorMessage": "boom"}
        result, reason = self.ask._ws13_docs_for({"mine_id": "IF0126"})
        self.assertIsNone(result)
        self.assertIn("Unhandled", reason)

    def test_a_transport_failure_on_the_document_list_is_a_miss(self):
        self.aws.lambda_client.error = RuntimeError("connect timeout")
        result, reason = self.ask._ws13_docs_for({"mine_id": "IF0126"})
        self.assertIsNone(result)
        self.assertIn("connect timeout", reason)

    def test_an_unmapped_mine_falls_back_to_sqlite_and_says_why(self):
        """The SQLite index is keyed on the front-end id, so it is the half
        that answers this mine today. Relaying WS13's zero would put "this
        mine has no documents" in front of a user whose documents are in the
        corpus."""
        self.aws.lambda_client.result = ws13_document_list(
            count=0, returned=0, documents=[], truncated=False,
            next_offset=None, filter_unresolved=["mine_id"])
        stub = types.ModuleType("document_tools")
        stub.TOOL_NAMES = frozenset({"search_documents", "docs_for"})
        stub.execute = lambda name, arguments: {
            "status": "loaded", "mine_id": arguments["mine_id"], "count": 2,
            "documents": [], "retrieval_source": "sqlite"}
        with mock.patch.dict(sys.modules, {"document_tools": stub}):
            result = self.ask.execute_local_tool(
                "docs_for", {"mine_id": "stategeo-igs-dd-1-if0126"})
        self.assertEqual(result["retrieval_source"], "sqlite")
        self.assertIn("mine_id", result["ws13_fallback_reason"])

    def test_a_stale_stored_copy_markdown_becomes_the_page_less_chip(self):
        stale = ws13_document(citation=ws13_citation(
            page=None,
            markdown="Mines and Prospects of Idaho, DD-1 (stored copy: "
                     "ws12/research-copies/3c/if0131.pdf)"))
        self.aws.lambda_client.result = ws13_document_list(documents=[stale])
        result, _ = self.ask._ws13_docs_for({"mine_id": "IF0126"})
        markdown = result["documents"][0]["citation"]["markdown"]
        self.assertNotIn("ws12/research-copies", markdown)
        match = DOC_CHIP_RE.fullmatch(markdown)
        self.assertIsNotNone(match, markdown)
        self.assertIsNone(match.group(3), "the listing must claim no page")

    def test_a_citation_too_malformed_for_a_chip_loses_the_key(self):
        """The branch that cannot build a chip is exactly the one where a
        stale "(stored copy: <s3 key>)" string would otherwise survive into an
        answer as decoration."""
        broken = ws13_document(citation=ws13_citation(
            page=None, sha256="not-a-digest",
            markdown="DD-1 (stored copy: ws12/research-copies/3c/if0131.pdf)"))
        self.aws.lambda_client.result = ws13_document_list(documents=[broken])
        result, _ = self.ask._ws13_docs_for({"mine_id": "IF0126"})
        self.assertNotIn(
            "ws12/", str(result["documents"][0]["citation"]["markdown"]))

    def test_the_listing_does_not_arm_the_citation_guard(self):
        """A listing citation has page None by construction, and the guard
        requires an int page -- so collecting these would withhold every
        answer that merely listed a mine's documents."""
        messages = [
            {"role": "user", "content": [{"text": "what is filed on IF0126?"}]},
            {"role": "assistant", "content": [{"toolUse": {
                "toolUseId": "t1", "name": "docs_for",
                "input": {"mine_id": "stategeo-igs-dd-1-if0126"}}}]},
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": "t1",
                "content": [{"json": ws13_document_list()}]}}]},
        ]
        self.assertEqual(self.ask._document_citations(messages), [])

    def test_every_docs_for_miss_ends_at_sqlite_with_the_reason_attached(self):
        """The rule the whole path rests on, for the listing half.

        A retrieval upgrade may degrade an answer and must never take the tool
        offline, so each of the four ways WS13 can fail to answer has to end
        at the bounded index -- with the reason on the result, because a
        deployment that has silently stopped using WS13 is otherwise visible
        only in a retrieval mode nobody reads. The SQLite tool has to receive
        the caller's own arguments too: a fallback that dropped the mine_id
        would answer a different question rather than the same one from a
        smaller index.
        """
        cases = {
            "a function error": dict(function_error="Unhandled",
                                     result={"errorMessage": "boom"}),
            "a transport failure": dict(error=RuntimeError("connect timeout")),
            "a status that is not loaded": dict(
                result={"status": "not_loaded", "count": None,
                        "documents": []}),
            "an unresolved mine id": dict(result=ws13_document_list(
                count=0, returned=0, documents=[], truncated=False,
                next_offset=None, filter_unresolved=["mine_id"])),
        }
        arguments = {"mine_id": "stategeo-igs-dd-1-if0126"}
        for label, failure in cases.items():
            with self.subTest(failure=label):
                self.aws.lambda_client = FakeLambdaClient()
                for key, value in failure.items():
                    setattr(self.aws.lambda_client, key, value)
                seen = []
                stub = types.ModuleType("document_tools")
                stub.TOOL_NAMES = frozenset({"search_documents", "docs_for"})
                stub.execute = lambda name, args: (
                    seen.append((name, args)) or
                    {"status": "loaded", "mine_id": args["mine_id"],
                     "count": 2, "documents": [], "retrieval_source": "sqlite"})
                with mock.patch.dict(sys.modules, {"document_tools": stub}):
                    result = self.ask.execute_local_tool("docs_for", arguments)
                self.assertEqual(result["retrieval_source"], "sqlite")
                self.assertTrue(result["ws13_fallback_reason"], result)
                self.assertEqual(seen, [("docs_for", arguments)])
                # The bounded index answered, so the bounded index's own count
                # is what the caller gets -- not WS13's zero wearing a reason.
                self.assertEqual(result["count"], 2)

    def test_a_fallback_result_is_the_bounded_index_answer_and_nothing_else(self):
        """Exactly one key is added. A merge of the two stacks' documents
        would be a document list whose rows came from two different corpora
        under one count, and only one of those corpora carries rights."""
        self.aws.lambda_client.error = RuntimeError("connect timeout")
        sqlite_result = {"status": "loaded", "mine_id": "IF0126", "count": 2,
                         "documents": [{"document_id": "a" * 64,
                                        "title": "IGS mine file"}]}
        stub = types.ModuleType("document_tools")
        stub.TOOL_NAMES = frozenset({"search_documents", "docs_for"})
        stub.execute = lambda name, args: dict(sqlite_result)
        with mock.patch.dict(sys.modules, {"document_tools": stub}):
            result = self.ask.execute_local_tool("docs_for",
                                                 {"mine_id": "IF0126"})
        self.assertEqual(set(result) - set(sqlite_result),
                         {"ws13_fallback_reason"})
        for key, value in sqlite_result.items():
            self.assertEqual(result[key], value, key)


class AskWs13DarkShipTest(unittest.TestCase):
    """With the flag off, both document tools are the SQLite call they were.

    infra/ask_lambda.py:52-62 states the rule the dark rollout rests on, and
    docs_for now shares the switch with search_documents -- so the flag-off
    dispatch has to be proven for it too, not assumed from the search half.
    """

    def setUp(self):
        self.aws = FakeAws()
        self.ask = load_ask_lambda(self.aws)

    def test_neither_document_tool_reaches_ws13_with_the_flag_off(self):
        self.assertFalse(self.ask.WS13_RETRIEVAL_ENABLED)
        stub = types.ModuleType("document_tools")
        stub.TOOL_NAMES = frozenset({"search_documents", "docs_for"})
        stub.execute = lambda name, arguments: {"status": "loaded",
                                                "tool": name, "count": 0,
                                                "documents": [], "hits": []}
        with mock.patch.dict(sys.modules, {"document_tools": stub}):
            for name, arguments in (("docs_for", {"mine_id": "IF0126"}),
                                    ("search_documents", {"query": "silver"})):
                with self.subTest(tool=name):
                    result = self.ask.execute_local_tool(name, arguments)
                    self.assertEqual(result["tool"], name)
                    self.assertNotIn("ws13_fallback_reason", result)
                    self.assertNotIn("retrieval_source", result)
        self.assertEqual(self.aws.lambda_client.calls, [])
        self.assertEqual(self.aws.bedrock.calls, [])

    def test_the_flag_off_dispatch_never_enters_the_ws13_helper_at_all(self):
        """"Inert" is stronger than "the invoke did not happen": the helper
        catches every exception it can raise and reports it as a miss, so a
        helper that ran and failed would look exactly like one that never ran
        -- except for the ws13_fallback_reason it would leave on the result.
        These raisers cannot be swallowed by that except clause because they
        never get inside it."""
        def never(*args, **kwargs):
            raise AssertionError("the WS13 helper ran with the flag off")

        stub = types.ModuleType("document_tools")
        stub.TOOL_NAMES = frozenset({"search_documents", "docs_for"})
        stub.execute = lambda name, args: {"status": "loaded", "tool": name}
        with mock.patch.dict(sys.modules, {"document_tools": stub}), \
                mock.patch.object(self.ask, "_ws13_docs_for", never), \
                mock.patch.object(self.ask, "_ws13_search", never):
            self.assertEqual(
                self.ask.execute_local_tool("docs_for", {"mine_id": "IF0126"}),
                {"status": "loaded", "tool": "docs_for"})

    def test_the_switch_needs_a_function_name_as_well_as_the_flag(self):
        """WS13_RETRIEVAL_ENABLED is `flag and bool(function name)`.

        The order a rollout actually happens in is flag first, function later,
        and an invoke of "" is a ParamValidationError inside the helper -- a
        miss, so the answer survives, but every docs_for call would spend a
        boto3 round trip and arrive carrying a fallback reason that reads like
        a WS13 outage rather than an unconfigured stack.
        """
        ask = load_ask_lambda(self.aws, WS13_RETRIEVAL_ENABLED="true",
                              WS13_RETRIEVAL_FUNCTION="")
        self.assertFalse(ask.WS13_RETRIEVAL_ENABLED)
        stub = types.ModuleType("document_tools")
        stub.TOOL_NAMES = frozenset({"search_documents", "docs_for"})
        stub.execute = lambda name, args: {"status": "loaded", "tool": name}
        with mock.patch.dict(sys.modules, {"document_tools": stub}):
            result = ask.execute_local_tool("docs_for", {"mine_id": "IF0126"})
        self.assertNotIn("ws13_fallback_reason", result)
        self.assertEqual(self.aws.lambda_client.calls, [])


class AskCitationGuardTest(unittest.TestCase):
    """The guard decides what a reader is allowed to be shown.

    A raw private S3 object key resolves nowhere for a reader -- no deployed
    surface takes one -- so certifying an answer because it pasted a
    viewer_key would both leak internal storage layout and bless a citation
    nobody can open.
    """

    def setUp(self):
        self.ask = load_ask_lambda(FakeAws())

    def answer(self, text):
        return {"role": "assistant", "content": [{"text": text}]}

    def test_the_doc_chip_satisfies_the_guard(self):
        citation = ws13_citation()
        text = (f"{citation['document_title']} records 250 tons "
                f"({citation['markdown']}), CC research terms.")
        self.assertTrue(
            self.ask._answer_has_resolvable_citation(self.answer(text),
                                                     [citation]))

    def test_a_raw_s3_key_does_not_satisfy_the_guard(self):
        """The negative half: pasting the stored object key must NOT count as
        a resolvable citation, however complete the rest of the sentence is."""
        citation = ws13_citation()
        for key in (citation["viewer_key"], citation["s3_key"]):
            with self.subTest(key=key):
                text = (f"{citation['document_title']}, p. 4 records 250 tons "
                        f"(stored copy: {key}).")
                self.assertFalse(
                    self.ask._answer_has_resolvable_citation(
                        self.answer(text), [citation]))

    def test_the_viewer_key_is_not_offered_as_a_reference_at_all(self):
        citation = ws13_citation()
        references = self.ask._citation_references(citation)
        self.assertEqual(references, [f"doc:{citation['sha256']}#4"])
        self.assertNotIn(citation["viewer_key"], references)
        self.assertNotIn(citation["s3_key"], references)

    def test_a_source_url_citation_is_unchanged(self):
        """The guard as it stood is extended, never weakened."""
        citation = ws13_citation(
            source_url="https://pubs.example.gov/dd1.pdf",
            resolvable_via="source_url",
            markdown="[Mines and Prospects of Idaho, DD-1, p. 4]"
                     "(https://pubs.example.gov/dd1.pdf)")
        self.assertEqual(self.ask._citation_references(citation),
                         ["https://pubs.example.gov/dd1.pdf"])
        text = f"It records 250 tons {citation['markdown']}."
        self.assertTrue(
            self.ask._answer_has_resolvable_citation(self.answer(text),
                                                     [citation]))

    def test_a_malformed_stored_copy_citation_resolves_to_nothing(self):
        """Fail closed: a sha256 or page the browser's own rule would not
        match cannot be turned into a chip, so the answer is withheld rather
        than blessed with a citation that renders as dead text."""
        for changes in ({"sha256": "not-a-digest"}, {"page": 0},
                        {"page": "4"}, {"sha256": ""}):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.ask._citation_references(ws13_citation(**changes)),
                    [])

    def test_the_withheld_answer_message_never_carries_an_object_key(self):
        """This message is what the reader actually gets when an answer is
        withheld, so it is the one place a leaked key would be guaranteed to
        reach them."""
        citation = ws13_citation()
        message = self.ask._citation_guard_message([citation])
        text = message["content"][0]["text"]
        self.assertNotIn(citation["viewer_key"], text)
        self.assertNotIn(citation["s3_key"], text)
        self.assertIn(f"doc:{citation['sha256']}#4", text)

    def test_a_stale_markdown_is_rebuilt_before_the_reader_sees_it(self):
        citation = ws13_citation(
            markdown="Mines and Prospects of Idaho, DD-1, p. 4 (stored copy: "
                     "ws13/searchable/3c/searchable.pdf)")
        text = self.ask._citation_guard_message(
            [citation])["content"][0]["text"]
        self.assertNotIn("ws13/searchable", text)
        self.assertIn(f"doc:{citation['sha256']}#4", text)

    def test_the_system_prompt_tells_the_model_which_form_to_paste(self):
        """A guard the model cannot satisfy withholds every document answer,
        so the contract has to be stated where the model reads it."""
        self.assertIn("[document title, p. N](source_url)", self.ask.SYSTEM)
        self.assertIn("doc:", self.ask.SYSTEM)
        self.assertIn("viewer_key", self.ask.SYSTEM)

    def test_the_front_end_still_parses_the_form_the_guard_blesses(self):
        """The chip is only a citation because site/index.html renders it; if
        that rule moves, this form silently becomes plain text."""
        page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn(FRONT_END_CHIP_RULE, page)
        citation = ws13_citation()
        self.assertRegex(citation["markdown"], DOC_CHIP_RE)


def _fts5_available():
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(chunk_id "
                      "UNINDEXED, text)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


HAS_FTS5 = _fts5_available()

DOC_CHUNKS = (
    # (page, ordinal, text). The first is the perfect keyword match and is
    # deliberately left without an embedding: that is the row the replaced
    # blend scored at -0.5 and sank below every embedded chunk.
    (1, 0, "The silver assay of the upper adit returned 14.2 ounces per ton "
           "and the assay of the lower level returned 9.1 ounces."),
    (2, 0, "A second silver assay on the dump sample returned 3.4 ounces per "
           "ton across a width of four feet."),
    (3, 0, "The workings follow a quartz vein carrying galena and sphalerite "
           "with subordinate chalcopyrite."),
    (4, 0, "Timbering in the shaft was renewed during the 1948 season by the "
           "lessee, who reported no production."),
    (5, 0, "The property lies on the north slope of the range, reached by a "
           "four mile road from the county highway."),
    (6, 0, "Ore was hauled to the concentrator at the mouth of the canyon "
           "until the mill burned in 1951."),
)


class LegacyDocumentToolsFusionTest(unittest.TestCase):
    """RRF ordering inside infra/document_tools.py, over a real SQLite index.

    This is the fallback path ASK returns to whenever WS13 misses, so its
    ranking is not a legacy detail: it is what the reader gets on the day the
    retrieval Lambda is unreachable. The arithmetic is asserted exactly --
    score = sum over arms of 1/(60 + rank) -- because the failure it replaced
    was silent, and a partial revert would show up as a plausible-looking
    order rather than an error.
    """

    MODEL = "local-hash-smoke-test-v1"      # _query_embedding embeds locally
    DIMENSIONS = 8

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = sqlite3.connect(
            str(Path(self.temp.name) / "docs.sqlite3"))
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row

    def build(self, fts5=True, embedded=range(1, len(DOC_CHUNKS))):
        """A minimal WS12 index: chunks, an FTS shadow, and embeddings."""
        connection = self.connection
        connection.executescript(
            "CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE chunks(chunk_id INTEGER PRIMARY KEY, "
            " document_id TEXT, page INTEGER, ordinal INTEGER, text TEXT, "
            " metadata_json TEXT);"
            "CREATE TABLE chunk_embeddings(chunk_id INTEGER, model TEXT, "
            " dimensions INTEGER, vector BLOB);"
            "CREATE TABLE document_sources(document_id TEXT, mine_id TEXT, "
            " portal_id TEXT);")
        connection.executemany(
            "INSERT INTO schema_meta(key,value) VALUES(?,?)",
            [("schema_version", "1"), ("fts5", "1" if fts5 else "0")])
        metadata = json.dumps({
            "title": "Mines and Prospects of Idaho, DD-1",
            "source_url": "https://www.idahogeology.org/dd1.pdf",
            "state": "ID", "county": "Custer", "mine_ids": ["IF0126"]})
        for index, (page, ordinal, text) in enumerate(DOC_CHUNKS):
            connection.execute(
                "INSERT INTO chunks(chunk_id,document_id,page,ordinal,text,"
                "metadata_json) VALUES(?,?,?,?,?,?)",
                (index, "doc-1", page, ordinal, text, metadata))
        connection.execute(
            "INSERT INTO document_sources(document_id,mine_id,portal_id) "
            "VALUES('doc-1','IF0126','id_igs')")
        if fts5:
            connection.execute("CREATE VIRTUAL TABLE chunk_fts USING fts5("
                               "chunk_id UNINDEXED, text)")
            connection.executemany(
                "INSERT INTO chunk_fts(chunk_id,text) VALUES(?,?)",
                [(index, text) for index, (_, _, text)
                 in enumerate(DOC_CHUNKS)])
        for index in embedded:
            vector = runtime_docs._hash_embedding(DOC_CHUNKS[index][2],
                                                  self.DIMENSIONS)
            connection.execute(
                "INSERT INTO chunk_embeddings(chunk_id,model,dimensions,"
                "vector) VALUES(?,?,?,?)",
                (index, self.MODEL, self.DIMENSIONS,
                 struct.pack(f"<{self.DIMENSIONS}f", *vector)))
        connection.commit()
        return connection

    def search(self, **arguments):
        payload = {"query": "silver assay", "mine_id": "IF0126", "limit": 12}
        payload.update(arguments)
        return runtime_docs._search(self.connection, payload)

    def expected_score(self, ranks):
        return sum(1.0 / (runtime_docs.RRF_K + rank)
                   for rank in ranks.values() if rank)

    def test_every_score_is_the_exact_sum_of_one_over_sixty_plus_rank(self):
        self.build(fts5=HAS_FTS5)
        result = self.search()
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            with self.subTest(chunk=hit["chunk_id"]):
                self.assertAlmostEqual(hit["rrf_score"],
                                       round(self.expected_score(hit["ranks"]),
                                             6),
                                       places=6)
                self.assertGreater(hit["rrf_score"], 0.0)

    def test_a_row_with_no_embedding_keeps_its_lexical_score_whole(self):
        """Chunk 0 is the perfect keyword match and carries no vector. The
        blend this replaced substituted -1.0 for its missing cosine and scored
        it 0.75*-1.0 + 0.25*1.0 = -0.5, below every embedded chunk; under RRF
        the arm it is absent from contributes nothing at all."""
        self.build(fts5=HAS_FTS5)
        result = self.search()
        hit = next(hit for hit in result["hits"] if hit["chunk_id"] == 0)
        self.assertIsNone(hit["ranks"]["vector"])
        self.assertEqual(hit["rrf_score"],
                         round(1.0 / (runtime_docs.RRF_K
                                      + hit["ranks"]["lexical"]), 6)
                         if hit["ranks"]["lexical"] else hit["rrf_score"])
        self.assertGreater(hit["rrf_score"], 0.0)
        self.assertLess(legacy_hybrid_score(-1.0, 0), 0.0)

    def test_it_outranks_a_vector_only_row_that_is_further_down_its_arm(self):
        """The concrete inversion, at fixture scale: chunk 0 is lexical rank 1
        with no vector (1/61), and the mine-scope backfill contributes rows
        that are in the vector arm only. Any of those at vector rank 2 or
        worse must sit below it -- the old blend put every one of them
        above."""
        if not HAS_FTS5:
            # Not a skip: ci/run_tests.py rejects unreviewed skips, and the
            # no-FTS5 degradation below is the contract this host can prove.
            self.assertFalse(HAS_FTS5)
            return
        self.build()
        result = self.search()
        order = [hit["chunk_id"] for hit in result["hits"]]
        keyword = next(hit for hit in result["hits"] if hit["chunk_id"] == 0)
        self.assertEqual(keyword["ranks"]["lexical"], 1)
        deeper = [hit for hit in result["hits"]
                  if hit["ranks"]["lexical"] is None
                  and (hit["ranks"]["vector"] or 0) > 1]
        self.assertTrue(deeper, "the fixture produced no vector-only rows")
        for hit in deeper:
            with self.subTest(chunk=hit["chunk_id"]):
                self.assertLess(keyword["rrf_score"], 1.0 / 60)
                self.assertGreater(keyword["rrf_score"], hit["rrf_score"])
                self.assertLess(order.index(0), order.index(hit["chunk_id"]))

    def test_the_returned_order_is_the_scored_order(self):
        self.build(fts5=HAS_FTS5)
        result = self.search()
        keyed = {hit["chunk_id"]: hit for hit in result["hits"]}
        expected = sorted(
            keyed.values(),
            key=lambda hit: (-hit["rrf_score"], "doc-1", hit["page"],
                             DOC_CHUNKS[hit["chunk_id"]][1]))
        self.assertEqual([hit["chunk_id"] for hit in result["hits"]],
                         [hit["chunk_id"] for hit in expected])

    def test_ties_keep_the_document_page_ordinal_order(self):
        """With no vector arm at all every score is 1/(60+rank), so the sort
        must not reorder a lexical-only result set."""
        self.build(fts5=HAS_FTS5, embedded=())
        result = self.search()
        self.assertIsNone(result["embedding_model"])
        for hit in result["hits"]:
            self.assertIsNone(hit["ranks"]["vector"])
        pages = [hit["page"] for hit in result["hits"]]
        if not HAS_FTS5:
            self.assertEqual(pages, sorted(pages))

    def test_an_unranked_like_filter_is_not_fed_to_the_fusion_as_an_arm(self):
        """An index built without FTS5 answers keyword queries in page order.
        Feeding that to RRF would hand document order the same 1/61 vote as
        the embedding model's best hit, so it forms no arm and the mode says
        so."""
        self.build(fts5=False)
        result = self.search()
        self.assertEqual(result["retrieval_mode"], "like_filter_vector")
        for hit in result["hits"]:
            self.assertIsNone(hit["ranks"]["lexical"])

    def test_a_failed_vector_arm_leaves_the_lexical_results_intact(self):
        """The embedder is the only part of this that can fail at runtime;
        losing it must cost the vector arm and nothing else."""
        self.build(fts5=HAS_FTS5)
        with mock.patch.object(runtime_docs, "_query_embedding",
                               side_effect=RuntimeError("bedrock down")):
            result = self.search()
        self.assertIn("bedrock down", result["embedding_error"])
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            self.assertIsNone(hit["ranks"]["vector"])

    def test_the_two_stacks_agree_on_k(self):
        """A hit ranked here and a hit ranked by the retrieval Lambda are
        compared by an operator reading one report."""
        self.assertEqual(runtime_docs.RRF_K, ql.RRF_K)


class AnnIndexParentClassificationTest(unittest.TestCase):
    """Who may be signalled, and in what order, before the index is built.

    The cost of getting this wrong is not a failed build. Cloud-init user data
    on the nwmm-ws13 fleet ends in `shutdown -h now`; killing only the python
    lets that shell walk on to it, an instance-initiated shutdown is a STOP,
    user data does not re-run on start and the node is in no ASG -- so nothing
    brings the host back. Every function asserted here is pure over the dict
    find_backfill() builds, which is what makes the one decision on that host
    that can stop an instance testable off it.
    """

    def entry(self, **changes):
        entry = {"pid": 4242,
                 "argv": "/usr/bin/python3 /opt/nwmm/pipelines/"
                         "ws13_embed_backfill.py --workers 4",
                 "ppid": 4200,
                 "parent_argv": "/bin/bash /var/lib/cloud/instance/scripts/"
                                "part-001",
                 "parent_name": "bash"}
        entry.update(changes)
        return entry

    def test_a_cloud_init_user_data_shell_is_recognised(self):
        kind, why = ann_index.classify_parent(self.entry())
        self.assertEqual(kind, "cloud_init")
        self.assertIn("4200", why)

    def test_pid_one_is_safe_to_kill(self):
        """An already-orphaned process has no shell above it to walk on to a
        shutdown line, so the child may be signalled alone."""
        for ppid in (0, 1):
            with self.subTest(ppid=ppid):
                kind, _ = ann_index.classify_parent(self.entry(ppid=ppid))
                self.assertEqual(kind, "orphaned")

    def test_an_unreadable_ppid_fails_closed_and_is_never_called_orphaned(self):
        """ppid None means the PPid line could not be READ -- a status read
        racing an exec, hidepid, a permission this process lacks. Calling that
        'orphaned' is a fail-OPEN branch in the one function whose promise is
        to refuse what it cannot identify: the step would then signal the
        child alone while a live user-data shell was still waiting on it."""
        kind, why = ann_index.classify_parent(self.entry(ppid=None))
        self.assertNotEqual(kind, "orphaned")
        self.assertEqual(kind, "unrecognised")
        self.assertIn("could not be read", why)

    def test_an_unknown_parent_shape_is_refused_not_guessed_at(self):
        kind, _ = ann_index.classify_parent(
            self.entry(parent_argv="tmux: server", parent_name="tmux"))
        self.assertEqual(kind, "unrecognised")
        kind, _ = ann_index.classify_parent(self.entry(parent_argv=""))
        self.assertEqual(kind, "unrecognised")

    def test_the_user_data_shell_is_signalled_before_its_child(self):
        """bash does not act on a deferred SIGTERM until its foreground child
        exits, so the parent goes first and is only reaped once the python
        does. Signalling the child first is precisely what lets the shell
        reach its next line."""
        plan = ann_index.plan_pause([self.entry()])
        self.assertTrue(plan["ok"])
        self.assertEqual([list(target) for target in plan["targets"]],
                         [[4200, "user-data shell"], [4242,
                                                      "ws13_embed_backfill.py"]])
        self.assertIn("4200", plan["commands"][0])
        self.assertIn("4242", plan["commands"][1])

    def test_an_orphan_is_signalled_alone(self):
        plan = ann_index.plan_pause([self.entry(ppid=1)])
        self.assertTrue(plan["ok"])
        self.assertEqual([list(target) for target in plan["targets"]],
                         [[4242, "ws13_embed_backfill.py"]])

    def test_an_unreadable_ppid_signals_nothing_at_all(self):
        plan = ann_index.plan_pause([self.entry(ppid=None)])
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["targets"], [])
        self.assertTrue(plan["commands"], "the operator got no instructions")
        self.assertIn("refusing to kill anything", plan["note"])

    def test_one_unrecognised_parent_stops_the_whole_step(self):
        """A half-executed pause is the state nobody can reason about."""
        plan = ann_index.plan_pause([
            self.entry(),
            self.entry(pid=99, ppid=98, parent_argv="tmux: server")])
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["targets"], [])

    def test_the_manual_commands_put_the_parent_first_too(self):
        commands = ann_index.manual_commands(
            self.entry(parent_argv="tmux: server", parent_name="tmux"))
        kills = [text for text in commands if text.startswith("kill -TERM")]
        self.assertEqual(kills, ["kill -TERM 4200", "kill -TERM 4242"])
        self.assertIn("shutdown -h now", "\n".join(commands))

    def test_nothing_running_is_not_a_failure(self):
        """The step is rerun-safe: --pause-backfill twice must not be an
        error the second time."""
        plan = ann_index.plan_pause([])
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["targets"], [])
        self.assertIn("already paused", plan["note"])

    def test_the_shutdown_line_in_the_user_data_script_is_detected(self):
        """The marker is what makes this dangerous; the script is only read to
        confirm it is really there."""
        script = "/var/lib/cloud/instance/scripts/part-001"
        for body, expected in (
                ("#!/bin/bash\npython3 ws13_embed_backfill.py\n"
                 "shutdown -h now\n", True),
                ("#!/bin/bash\npython3 ws13_embed_backfill.py\n", False)):
            with self.subTest(expected=expected):
                with mock.patch("os.path.isfile", return_value=True), \
                        mock.patch.object(
                            ann_index, "Path",
                            lambda token: types.SimpleNamespace(
                                read_text=lambda **kwargs: body)):
                    path, has_shutdown = ann_index.user_data_script(
                        f"/bin/bash {script}")
                self.assertEqual(path, script)
                self.assertIs(has_shutdown, expected)

    def test_a_recycled_pid_is_not_signalled(self):
        """PIDs are recycled; killing a recycled one on a live host is the
        accident this whole step exists to avoid."""
        self.assertTrue(ann_index.looks_like_target(
            "/usr/bin/python3 /opt/nwmm/pipelines/ws13_embed_backfill.py"))
        self.assertTrue(ann_index.looks_like_target(
            "/bin/bash /var/lib/cloud/instance/scripts/part-001"))
        self.assertFalse(ann_index.looks_like_target("sshd: ec2-user@pts/0"))


class ReservedConcurrencyTemplateTests(unittest.TestCase):
    """MaxConcurrency=0 must withhold the property, not send a literal 0.

    Two different failures meet here, and the second is the nasty one.

    The template asked for ReservedConcurrentExecutions: 20. This account's
    TOTAL concurrent-execution limit is 10 and Lambda refuses to leave fewer
    than 10 unreserved, so the reservation was rejected and the stack could
    not create -- the default was twice the whole account's ceiling.

    The obvious fix, passing 0, is worse than the bug: Lambda reads a literal
    ReservedConcurrentExecutions: 0 as throttled-to-zero. The function deploys
    and then can never run, which fails at request time rather than at deploy
    time. So 0 has to resolve to AWS::NoValue.
    """

    TEMPLATE = (ROOT / "infra" / "ws13_retrieval.yaml").read_text(encoding="utf-8")

    def test_zero_is_withheld_rather_than_sent(self):
        self.assertIn("HasReservedConcurrency", self.TEMPLATE)
        self.assertIn("!Equals [!Ref MaxConcurrency, 0]", self.TEMPLATE)
        # Anchored to the PROPERTY line at resource indentation. Matching the
        # bare string finds the prose in the Conditions comment first, which
        # is how the first version of this test passed against a template
        # that still bound the property unconditionally.
        match = re.search(
            r"^ {6}ReservedConcurrentExecutions:(.*?)(?=^ {6}\w)",
            self.TEMPLATE, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "property not found at resource indent")
        block = match.group(1)
        self.assertIn("!If", block)
        self.assertIn("HasReservedConcurrency", block)
        self.assertIn("AWS::NoValue", block)

    def test_the_property_is_never_bound_unconditionally(self):
        # The shape that could not deploy. A bare `!Ref MaxConcurrency` on
        # this line is the regression.
        self.assertNotRegex(
            self.TEMPLATE,
            r"ReservedConcurrentExecutions:\s*!Ref\s+MaxConcurrency")

    def test_the_default_reserves_nothing(self):
        match = re.search(
            r"MaxConcurrency:\s*\n\s+Type: Number\s*\n\s+Default:\s*(\d+)",
            self.TEMPLATE)
        self.assertIsNotNone(match, "MaxConcurrency default not found")
        self.assertEqual(
            match.group(1), "0",
            "a non-zero default cannot deploy while the account's Lambda "
            "concurrency limit is 10 with a 10-unreserved floor")

    def test_the_description_says_zero_is_not_throttle_to_zero(self):
        # This is the whole trap: a reader who sees Default: 0 and assumes it
        # disables the function would raise it back to a value that cannot
        # deploy. The parameter has to say so itself.
        block = self.TEMPLATE.split("MaxConcurrency:")[1].split("CodeS3Bucket:")[0]
        self.assertIn("reserves NOTHING", block)
        self.assertIn("never run", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
