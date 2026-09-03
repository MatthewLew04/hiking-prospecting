#!/usr/bin/env python3
"""ws13_geomodel — the sharded document -> mine -> 3-D chain over WS13.

``pipelines/geomodel_autopopulate.py`` runs the modeller over the 25-document
WS12 store on this machine.  This driver runs the same chain — carve the
mine's own text, parse it, answer every open question with *omit*, build
through ``services/minevis/tools.run_build``, publish content-addressed — over
the 56,282-document WS13 corpus (in-VPC Postgres ``nwmm-ws13`` + S3 sidecar
text), sharded, ledgered and rerun-safe, sized the way the confidence pass
(``ws13_confidence_pass.py``) is sized.

Nothing here invents anything the modeller would not: every element still
carries the sentence it came from, confidence stays per field, a missing
bearing is still a question and the unattended answer to every question is
still ``null`` = omit.  What this file adds is the bookkeeping that lets 64
processes cover the corpus without guessing, duplicating or forgetting.

The funnel
----------
Every document falls through seven steps, and ``--plan`` prints how many
survive each and why the rest dropped::

    documents                        the shard's rows of ws13_documents
    with_mine_ids_or_names           mine_ids[] or mine_names[] non-empty
    with_workings_vocabulary         >= 1 ws13_chunks hit on the workings
                                     tsquery (adit|tunnel|shaft|...)
    rights_permit_a_derived_model    public domain, or a rights_basis to
                                     carry; unknown admission_class refuses
    resolvable_to_a_located_site     geomodel_corpus.SiteIndex.resolve(doc)
                                     names >= 1 located front-end site and
                                     no name is ambiguous
    parseable_elements               the carved text yields >= 1 element
    publishable                      a model was published (or is unchanged)

The first two steps are re-derived every sweep and never written to the
ledger: they are cheap, deterministic, and a document the corpus never
attached a mine to is not "work remaining".  Everything from the rights step
on writes one ``ws13_geomodel_runs`` row per (document, mine) — or per
document under the ``'-'`` mine sentinel when it was decided before any mine
was named.

The Corpus adapter
------------------
Two implementations of one interface (:class:`Corpus`): :class:`PostgresCorpus`
(psycopg + boto3, imported lazily, so this file imports on a laptop with
neither) and :class:`FixtureCorpus` (``tests/fixtures/ws13_geomodel/``:
``documents.jsonl`` of ws13_documents-shaped rows plus ws13_mine_id_map rows,
``pages/<sha>/<page>.txt``).  Every SQL statement lives in :data:`SQL`, with
its columns named; there is no ``SELECT *``.  Page text comes from the
sidecar ``ws13_worker.py`` uploads (``ws13/searchable/<sha[:2]>/<sha>/
sidecar.txt``, pages split on form feed) and, when the sidecar is missing,
from ``ws13_chunks`` concatenated in (page, ordinal) order and de-overlapped
on ``start_char``; the ledger row says which source was used.

Sharding
--------
``--shard i --shards n`` partitions documents on the first 8 hex digits of
sha256, exactly as the confidence pass does::

    Python:    int(sha256[:8], 16) % shards                (shard_of)
    Postgres:  mod(('x' || substr(d.sha256, 1, 8))::bit(32)::bigint, %s)

Both sides are the same arithmetic on the same digits, so ``--plan`` on a
laptop and a running shard cannot disagree about who owns a document.

Rerun safety
------------
A (sha256, mine_key) whose ledger row is ``published`` with an unchanged
``content_hash`` — sha256 over the carved text's hash, the parser, builder,
publisher and driver versions, the answer policy and the context flag — is
skipped; the row is rewritten with the same status, a new run_id and
``reason = 'unchanged: skipped on rerun'``.  A document is *remaining* while
any of its rows is neither terminal (parked / skipped / published) nor an
error that has used its WS13_GEOMODEL_MAX_ATTEMPTS sweeps.  A new builder
version does not by itself reopen finished rows (that predicate would need
the text); ``--rebuild`` reopens them, and the content hash then decides.

Exit codes (a contract, not a boolean; 1 is left to CPython)
------------------------------------------------------------
    0   this shard is finished: 0 documents remaining
    10  documents remain and this sweep finished some -> sweep again
    11  documents remain and this sweep finished NONE -> back off
    2   --shard/--shards is not a partition, or --rate <= 0
    3   environment: no DSN, no bucket, no models bucket, no nwmm checkout,
        no terrain cache while --offline
    12  --verify-complete found work remaining

Heartbeat: ``s3://$WS13_BUCKET/ws13/geomodel/status.json`` unsharded and
``ws13/geomodel/status-<shard:04d>-of-<shards:04d>.json`` once sharded —
documents done / remaining, models published, parked by reason, rate and the
implied finish time.

Sizing
------
The unit of work is a document, and the cost is dominated by the build (and
by terrain tiles when ``--context`` is on).  ``--limit N`` runs a bounded
sweep and reports documents/second and models/second; pass the measured
documents/second back as ``--rate`` and ``--plan`` projects hours per shard
for 64 / 128 / 640 shards — the same argument the confidence pass makes for
one c7g.16xlarge running 64 processes with no cross-node coordination.

Run it::

    ws13_geomodel.py --plan                         # the funnel, no builds
    ws13_geomodel.py --limit 40 --publish s3        # a sizing sweep
    ws13_geomodel.py --shard 7 --shards 64 --publish s3
    ws13_geomodel.py --doc <sha256> --publish local --site-dir site
    ws13_geomodel.py --verify-complete --shards 64
    ws13_geomodel.py --migrate   /   --check        # the ledger table
    ws13_geomodel.py --fixture tests/fixtures/ws13_geomodel --state-dir /tmp/x
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_root():
    """The nwmm checkout: ``$NWMM_ROOT``, or the directory this file's parent
    sits in when this file is ``<root>/pipelines/ws13_geomodel.py``.

    On a fleet node the bundle untars this file flat into /opt/ws13, where
    ``pipelines/geomodel`` is not a sibling; ``NWMM_ROOT`` then has to name a
    checkout, and main() answers with EXIT_ENVIRONMENT rather than a
    traceback when it does not.
    """
    candidates = []
    if os.environ.get('NWMM_ROOT'):
        candidates.append(os.environ['NWMM_ROOT'])
    candidates.append(os.path.dirname(HERE))
    for cand in candidates:
        if os.path.isdir(os.path.join(cand, 'pipelines', 'geomodel')):
            return os.path.abspath(cand)
    return None


ROOT = _find_root()
IMPORT_ERROR = None
if ROOT is None:
    IMPORT_ERROR = ImportError(
        'no nwmm checkout found (need <root>/pipelines/geomodel); set NWMM_ROOT')
else:
    for _sub in ('pipelines', 'services'):
        _path = os.path.join(ROOT, _sub)
        if _path not in sys.path:
            sys.path.insert(0, _path)
    try:
        from geomodel import agentbuild, assay, narrative, publish, resolve   # noqa: E402
        import geomodel_corpus as corpus                                     # noqa: E402
        import geomodel_autopopulate as autop                                # noqa: E402
        from minevis import tools as minevis_tools                           # noqa: E402
    except ImportError as exc:                                               # pragma: no cover
        IMPORT_ERROR = exc

DRIVER_VERSION = 'nwmm-ws13-geomodel/1'
POLICY = 'omit'

#: the ledger's mine_key for a document decided before any mine was named
NO_MINE = '-'
STATUSES = ('planned', 'parked', 'skipped', 'built', 'published', 'error')
#: statuses that take a (document, mine) out of the work set for good
TERMINAL = ('parked', 'skipped', 'published')
MAX_ATTEMPTS = int(os.environ.get('WS13_GEOMODEL_MAX_ATTEMPTS', '3'))

#: the lexical prefilter: a document that never uses one of these words does
#: not describe workings, and 56,282 documents narrow to the ones that might
WORKINGS_VOCABULARY = ('adit', 'tunnel', 'shaft', 'winze', 'raise', 'stope',
                       'drift', 'crosscut', 'level', 'incline', 'decline',
                       'portal', 'collar')
#: ws13_chunks.tsv is to_tsvector('english', text) (ws13_worker.py's INSERT),
#: so the query uses the same configuration and inherits its stemming
WORKINGS_TSQUERY = ' | '.join(WORKINGS_VOCABULARY)
#: the fixture's stand-in for that stemming; only hits > 0 gates anything
WORKINGS_RE = re.compile(r'\b(?:' + '|'.join(WORKINGS_VOCABULARY)
                         + r')(?:s|es|d|ed|ing)?\b', re.I)


def shard_expr(alias):
    """The Postgres side of the partition, over ``<alias>.sha256``."""
    return "('x' || substr(%s.sha256, 1, 8))::bit(32)::bigint" % alias


#: mirrors ws13_confidence_pass.SHARD_EXPR with the documents alias
SHARD_EXPR = shard_expr('d')


def shard_of(sha, shards):
    """Which shard owns this document.  Mirrors :func:`shard_expr` exactly."""
    if shards <= 1:
        return 0
    return int(sha[:8], 16) % shards


EXIT_SHARD_DONE = 0
EXIT_BAD_SHARD = 2
EXIT_ENVIRONMENT = 3
EXIT_WORK_REMAINS = 10
EXIT_NO_PROGRESS = 11
EXIT_INCOMPLETE = 12

HEARTBEAT_SECONDS = int(os.environ.get('WS13_GEOMODEL_HEARTBEAT', '300'))
DOC_BATCH = 200
#: a SEED, not a measurement: run --limit over a few dozen documents, read
#: documents_per_second from the heartbeat, pass it back as --rate
SECONDS_PER_DOCUMENT = float(os.environ.get('WS13_GEOMODEL_SECONDS_PER_DOC', '4.0'))
DEFAULT_RATE = 1.0 / SECONDS_PER_DOCUMENT
#: the confidence pass's recommendation: one node, one shard per vCPU
NODE_TYPE = 'c7g.16xlarge'
NODE_VCPU = 64
PLAN_SHARDS = (64, 128, 640)
DOCS_TOTAL = 56_282

STATUS_PREFIX = 'ws13/geomodel'
RESULTS_DIR = os.path.join(ROOT or HERE, 'var', 'geomodel')
DEFAULT_SQL = os.path.join(HERE, 'ws13_geomodel_migrations.sql')
DEFAULT_TERRAIN_CACHE = os.path.join(ROOT or HERE, 'pipelines', 'cache', 'terrain')


def sidecar_key(sha):
    """The per-page text ws13_worker.py uploads beside the searchable PDF:
    ``ws13/searchable/<sha[:2]>/<sha>/sidecar.txt``, pages joined on ``\\f``."""
    return 'ws13/searchable/%s/%s/sidecar.txt' % (sha[:2], sha)


# Byte-identical mirror of infra/ws13_query_lambda.RIGHTS_BY_CLASS (and of
# infra/docs_lambda.RIGHTS_BY_CLASS).  Copied rather than imported because
# the Lambda module is not on a fleet node; tests/test_ws13_geomodel.py reads
# the Lambda source and asserts the three tables are equal, so a model
# derived from a licensed copy carries exactly the terms a citation does.
RIGHTS_BY_CLASS = {
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

DOCUMENT_COLUMNS = ('sha256', 's3_key', 'searchable_key', 'doc_class', 'portal',
                    'state', 'mine_ids', 'mine_names', 'county', 'trs',
                    'doc_date', 'doc_type', 'title', 'pages', 'source_url',
                    'rights_basis', 'public_domain', 'admission_class',
                    'doc_year_min', 'doc_year_max')
MAP_COLUMNS = ('front_end_id', 'ws13_mine_id', 'ws13_mine_id_all', 'method',
               'relation', 'confidence', 'verified')
LEDGER_COLUMNS = ('sha256', 'mine_key', 'run_id', 'status', 'reason', 'model_id',
                  'content_hash', 'counts', 'warnings', 'attempts', 'updated_at')

_DOC_SELECT = ', '.join('d.%s' % c for c in DOCUMENT_COLUMNS)
_MAP_OBJECT = ', '.join("'%s', m.%s" % (c, c) for c in MAP_COLUMNS)
_LEDGER_SELECT = ', '.join('r.%s' % c for c in LEDGER_COLUMNS)

#: every statement this driver issues, in one place, columns named.  The
#: shard clause is appended by the corpus when shards > 1.
SQL = {
    # ws13_documents joined with ws13_mine_id_map on ANY of the document's
    # corpus mine ids: mine_ids && ws13_mine_id_all, the overlap the retrieval
    # Lambda uses, so a document filed under a second spelling still joins.
    'documents': (
        'SELECT ' + _DOC_SELECT + ', '
        "COALESCE(json_agg(json_build_object(" + _MAP_OBJECT + ") "
        'ORDER BY m.front_end_id) FILTER (WHERE m.front_end_id IS NOT NULL), '
        "'[]'::json) AS mine_map "
        'FROM ws13_documents d '
        'LEFT JOIN ws13_mine_id_map m ON m.ws13_mine_id_all && d.mine_ids '
        'WHERE d.sha256 > %s '),
    'documents_tail': 'GROUP BY ' + _DOC_SELECT + ' ORDER BY d.sha256 LIMIT %s',
    'document': (
        'SELECT ' + _DOC_SELECT + ', '
        "COALESCE(json_agg(json_build_object(" + _MAP_OBJECT + ") "
        'ORDER BY m.front_end_id) FILTER (WHERE m.front_end_id IS NOT NULL), '
        "'[]'::json) AS mine_map "
        'FROM ws13_documents d '
        'LEFT JOIN ws13_mine_id_map m ON m.ws13_mine_id_all && d.mine_ids '
        'WHERE d.sha256 = %s '
        'GROUP BY ' + _DOC_SELECT),
    # the lexical prefilter: per-document count of chunks matching the
    # workings vocabulary, over the GIN index on ws13_chunks.tsv
    'candidates': (
        'SELECT c.sha256, COUNT(*) AS hits '
        'FROM ws13_chunks c '
        'JOIN ws13_documents d ON d.sha256 = c.sha256 '
        "WHERE c.tsv @@ to_tsquery('english', %s) "),
    'candidates_tail': 'GROUP BY c.sha256',
    # the sidecar fallback: chunks in page/ordinal order with their offsets
    'chunks': (
        'SELECT c.page, c.ordinal, c.start_char, c.end_char, c.text '
        'FROM ws13_chunks c WHERE c.sha256 = %s '
        'ORDER BY c.page, c.ordinal'),
    'ledger_load': (
        'SELECT ' + _LEDGER_SELECT + ' FROM ws13_geomodel_runs r WHERE TRUE '),
    'ledger_put': (
        'INSERT INTO ws13_geomodel_runs (sha256, mine_key, run_id, status, '
        'reason, model_id, content_hash, counts, warnings, attempts, updated_at) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, now()) '
        'ON CONFLICT (sha256, mine_key) DO UPDATE SET '
        'run_id = EXCLUDED.run_id, status = EXCLUDED.status, '
        'reason = EXCLUDED.reason, model_id = EXCLUDED.model_id, '
        'content_hash = EXCLUDED.content_hash, counts = EXCLUDED.counts, '
        'warnings = EXCLUDED.warnings, attempts = EXCLUDED.attempts, '
        'updated_at = now()'),
    # --check: the catalogue, read-only
    'check_columns': (
        'SELECT a.attname FROM pg_attribute a '
        "WHERE a.attrelid = to_regclass('public.ws13_geomodel_runs') "
        'AND a.attnum > 0 AND NOT a.attisdropped'),
    'check_constraints': (
        'SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c '
        "WHERE c.conrelid = to_regclass('public.ws13_geomodel_runs')"),
    'check_indexes': (
        'SELECT i.indexname FROM pg_indexes i '
        "WHERE i.schemaname = 'public' AND i.tablename = 'ws13_geomodel_runs'"),
}

REQUIRED_INDEXES = ('ws13_geomodel_runs_pkey', 'ws13_geomodel_runs_status',
                    'ws13_geomodel_runs_run_id', 'ws13_geomodel_runs_model_id')
REQUIRED_CONSTRAINTS = ('ws13_geomodel_runs_status', 'ws13_geomodel_runs_published')


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log_line(msg, shard=None, shards=None, out=None):
    prefix = '' if shard is None else '[shard %s/%s] ' % (shard, shards)
    print('%s %s%s' % (now_iso(), prefix, msg), file=out or sys.stdout, flush=True)


class CorpusUnavailable(RuntimeError):
    """The environment cannot supply the corpus: exit 3, never a traceback."""


# ------------------------------------------------------------------ text
def clean_text(text):
    """ws13_worker.clean_text: NUL bytes carry nothing and Postgres refuses them."""
    return text.replace('\x00', '') if text else (text or '')


def pages_from_sidecar(text):
    """The sidecar's pages: ``\\f``-joined, exactly as ws13_worker wrote it."""
    return [clean_text(p) for p in (text or '').split('\f')]


def pages_from_chunks(rows, page_count=None):
    """ws13_chunks rows ``(page, ordinal, start_char, end_char, text)`` ->
    per-page text, 1-based pages, empty strings for pages with no chunk.

    ws13_worker.chunk_pages() cuts each page at CHUNK_CHARS with a
    CHUNK_OVERLAP tail carried into the next chunk, so consecutive chunks
    repeat up to 400 characters.  Each chunk's ``start_char``/``end_char``
    are offsets into the page's whitespace-normalised text, so the repeated
    prefix is exactly the part of a chunk that lies before the previous
    chunk's ``end_char`` and can be dropped by offset.  The reconstruction
    is the page's chunked text, not the sidecar's — chunking normalised runs
    of spaces and stripped each piece — which is why the ledger records
    which source a build read.
    """
    pages = {}
    cursor = {}
    for page, ordinal, start, end, text in rows:
        text = text or ''
        start = 0 if start is None else int(start)
        end = start + len(text) if end is None else int(end)
        seen = cursor.get(page, 0)
        if start < seen:
            skip = min(len(text), seen - start)
            text = text[skip:]
        if text:
            parts = pages.setdefault(page, [])
            parts.append(text)
        cursor[page] = max(seen, end)
    top = max([page_count or 0] + list(pages))
    return [''.join(pages.get(p, [])) for p in range(1, top + 1)]


# ---------------------------------------------------------------- rights
def rights_for(doc):
    """Does the document's rights record permit a derived model, and what
    must travel with it?

    ``{'status': 'ok'|'refused', 'reason', 'rights': {...}}`` where rights is
    what publish_stage writes into the manifest: source_url, rights_basis,
    public_domain, admission_class, rights_terms and the three flags derived
    exactly as the citation resolver derives them.  Refused when the
    admission class is unknown, when the class's terms name a source and
    there is none, or when the document is not known to be public domain
    (``public_domain`` false OR null) and carries no rights_basis: a derived
    model whose attribution cannot be stated must not be published.
    """
    cls = doc.get('admission_class')
    template = RIGHTS_BY_CLASS.get(cls)
    basis = str(doc.get('rights_basis') or '').strip() or None
    public_domain = doc.get('public_domain')
    if template is None:
        return {'status': 'refused', 'rights': None,
                'reason': 'rights: unknown admission_class %r' % (cls,)}
    if '{basis}' in template['rights_terms'] and basis is None:
        return {'status': 'refused', 'rights': None,
                'reason': 'rights: no rights_basis on a %s document' % cls}
    if not public_domain and basis is None:
        return {'status': 'refused', 'rights': None,
                'reason': 'rights: no rights_basis on a non-public-domain document'}
    return {'status': 'ok', 'reason': None, 'rights': {
        'source_url': doc.get('source_url'),
        'rights_basis': basis,
        'public_domain': bool(public_domain),
        'admission_class': cls,
        'rights_terms': template['rights_terms'].format(basis=basis),
        'attribution_required': template['attribution_required'],
        'non_commercial': template['non_commercial'],
        'share_alike': template['share_alike'],
    }}


# ---------------------------------------------------------------- ledger
class Ledger(object):
    """One row per (sha256, mine_key); the newest write wins."""

    def load(self, shard=0, shards=1):
        """``{(sha256, mine_key): row}`` for this shard."""
        raise NotImplementedError

    def put(self, row):
        raise NotImplementedError


def ledger_row(sha, mine_key, run_id, status, reason=None, model_id=None,
               content_hash=None, counts=None, warnings=None, attempts=1):
    if status not in STATUSES:
        raise ValueError('ledger status %r is not one of %s' % (status, STATUSES))
    return {'sha256': sha, 'mine_key': mine_key, 'run_id': run_id,
            'status': status, 'reason': reason, 'model_id': model_id,
            'content_hash': content_hash, 'counts': dict(counts or {}),
            'warnings': list(warnings or []), 'attempts': int(attempts),
            'updated_at': now_iso()}


class JsonlLedger(Ledger):
    """The fixture ledger: an append-only JSONL file, replayed on load."""

    def __init__(self, path):
        self.path = path
        self.rows = {}
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    self.rows[(row['sha256'], row['mine_key'])] = row

    def load(self, shard=0, shards=1):
        return {k: dict(v) for k, v in self.rows.items()
                if shard_of(k[0], shards) == shard}

    def put(self, row):
        self.rows[(row['sha256'], row['mine_key'])] = dict(row)
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + '\n')


class PostgresLedger(Ledger):
    def __init__(self, conn):
        self.conn = conn

    def load(self, shard=0, shards=1):
        sql, params = SQL['ledger_load'], ()
        if shards > 1:
            sql += 'AND mod(%s, %%s) = %%s' % shard_expr('r')
            params = (shards, shard)
        out = {}
        for row in self.conn.execute(sql, params).fetchall():
            rec = dict(zip(LEDGER_COLUMNS, row))
            for col in ('counts', 'warnings'):
                if isinstance(rec.get(col), str):
                    rec[col] = json.loads(rec[col])
            out[(rec['sha256'], rec['mine_key'])] = rec
        return out

    def put(self, row):
        self.conn.execute(SQL['ledger_put'], (
            row['sha256'], row['mine_key'], row['run_id'], row['status'],
            row.get('reason'), row.get('model_id'), row.get('content_hash'),
            json.dumps(row.get('counts') or {}, default=str),
            json.dumps(row.get('warnings') or [], default=str),
            int(row.get('attempts') or 0)))


# ---------------------------------------------------------------- corpus
class Corpus(object):
    """Where documents, their text, the prefilter, the ledger and the
    heartbeat come from.  Two implementations, one interface."""

    name = 'corpus'
    ledger = None

    def documents(self, shard=0, shards=1):
        """Stream the shard's ws13_documents rows as dicts keyed by
        DOCUMENT_COLUMNS plus ``mine_map`` (the joined ws13_mine_id_map
        rows), in sha256 order."""
        raise NotImplementedError

    def document(self, sha):
        raise NotImplementedError

    def page_texts(self, sha):
        """``(pages, source)`` — 1-based page texts and where they came from."""
        raise NotImplementedError

    def candidates(self, shard=0, shards=1):
        """``{sha256: hits}`` for documents using the workings vocabulary."""
        raise NotImplementedError

    def put_status(self, key, payload):
        raise NotImplementedError

    def close(self):
        pass


def _doc_from_row(row, columns):
    doc = dict(zip(columns, row))
    mine_map = doc.get('mine_map')
    if isinstance(mine_map, str):
        mine_map = json.loads(mine_map)
    doc['mine_map'] = list(mine_map or [])
    doc['mine_ids'] = list(doc.get('mine_ids') or [])
    doc['mine_names'] = list(doc.get('mine_names') or [])
    return doc


class PostgresCorpus(Corpus):
    """The real thing: ws13_documents / ws13_mine_id_map / ws13_chunks over
    psycopg, the sidecar text and the heartbeat over boto3.  Both imported
    here, lazily, so a laptop without either can still --plan a fixture."""

    name = 'postgres'

    def __init__(self, dsn, bucket, region=None, psycopg_module=None, s3_client=None):
        if not dsn:
            raise CorpusUnavailable('no DSN: pass --dsn or set WS13_DB_DSN')
        if not bucket:
            raise CorpusUnavailable('no bucket: pass --bucket or set WS13_BUCKET')
        self.dsn, self.bucket = dsn, bucket
        if psycopg_module is None:
            try:
                import psycopg as psycopg_module
            except ImportError as exc:
                raise CorpusUnavailable('psycopg is not importable: %s' % exc)
        if s3_client is None:
            try:
                import boto3
            except ImportError as exc:
                raise CorpusUnavailable('boto3 is not importable: %s' % exc)
            s3_client = boto3.client(
                's3', region_name=region or os.environ.get('AWS_DEFAULT_REGION', 'us-west-2'))
        self.s3 = s3_client
        self.conn = psycopg_module.connect(dsn, autocommit=True)
        self.ledger = PostgresLedger(self.conn)

    def _columns(self):
        return DOCUMENT_COLUMNS + ('mine_map',)

    def documents(self, shard=0, shards=1):
        cursor = ''
        while True:
            sql = SQL['documents']
            params = [cursor]
            if shards > 1:
                sql += 'AND mod(%s, %%s) = %%s ' % SHARD_EXPR
                params += [shards, shard]
            sql += SQL['documents_tail']
            params.append(DOC_BATCH)
            rows = self.conn.execute(sql, tuple(params)).fetchall()
            if not rows:
                return
            for row in rows:
                yield _doc_from_row(row, self._columns())
            cursor = rows[-1][0]

    def document(self, sha):
        row = self.conn.execute(SQL['document'], (sha,)).fetchone()
        return _doc_from_row(row, self._columns()) if row else None

    def candidates(self, shard=0, shards=1):
        sql, params = SQL['candidates'], [WORKINGS_TSQUERY]
        if shards > 1:
            sql += 'AND mod(%s, %%s) = %%s ' % SHARD_EXPR
            params += [shards, shard]
        sql += SQL['candidates_tail']
        return {sha: int(hits) for sha, hits in
                self.conn.execute(sql, tuple(params)).fetchall()}

    def page_texts(self, sha):
        key = sidecar_key(sha)
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)['Body'].read()
        except Exception as exc:
            code = ''
            response = getattr(exc, 'response', None)
            if isinstance(response, dict):
                code = str(response.get('Error', {}).get('Code', ''))
            if type(exc).__name__ not in ('NoSuchKey', 'NoSuchBucket') and \
                    code not in ('NoSuchKey', 'NoSuchBucket', '404'):
                raise
            rows = self.conn.execute(SQL['chunks'], (sha,)).fetchall()
            if not rows:
                raise CorpusUnavailable(
                    'no sidecar at %s and no ws13_chunks rows for %s' % (key, sha))
            return pages_from_chunks(rows), 'chunks'
        return pages_from_sidecar(body.decode('utf-8', 'replace')), 'sidecar'

    def put_status(self, key, payload):
        self.s3.put_object(Bucket=self.bucket, Key=key, ContentType='application/json',
                           Body=json.dumps(payload, default=str).encode('utf-8'))

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class FixtureCorpus(Corpus):
    """``<dir>/documents.jsonl`` — one ws13_documents-shaped row per line,
    ws13_mine_id_map rows marked ``"_table": "ws13_mine_id_map"`` — and
    ``<dir>/pages/<sha>/<page>.txt`` (or ``<dir>/chunks/<sha>.jsonl`` rows
    shaped like SQL['chunks'] for the fallback path).  The ledger is
    ``<state_dir>/ledger.jsonl`` and the heartbeat lands under
    ``<state_dir>/<key>``; the fixture directory itself is never written."""

    name = 'fixture'

    def __init__(self, directory, state_dir):
        self.dir = directory
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.docs, maps = {}, []
        with open(os.path.join(directory, 'documents.jsonl'), encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                table = row.pop('_table', 'ws13_documents')
                if table == 'ws13_mine_id_map':
                    maps.append(row)
                elif table == 'ws13_documents':
                    doc = {c: row.get(c) for c in DOCUMENT_COLUMNS}
                    doc['mine_map'] = []
                    doc['mine_ids'] = list(doc.get('mine_ids') or [])
                    doc['mine_names'] = list(doc.get('mine_names') or [])
                    self.docs[doc['sha256']] = doc
                else:
                    raise ValueError('unknown fixture table %r' % table)
        for m in maps:
            spellings = set(m.get('ws13_mine_id_all') or [m.get('ws13_mine_id')])
            for doc in self.docs.values():
                if spellings & set(doc['mine_ids']):
                    doc['mine_map'].append({c: m.get(c) for c in MAP_COLUMNS})
        for doc in self.docs.values():
            doc['mine_map'].sort(key=lambda r: str(r.get('front_end_id')))
        self.ledger = JsonlLedger(os.path.join(state_dir, 'ledger.jsonl'))

    def documents(self, shard=0, shards=1):
        for sha in sorted(self.docs):
            if shard_of(sha, shards) == shard:
                yield dict(self.docs[sha])

    def document(self, sha):
        doc = self.docs.get(sha)
        return dict(doc) if doc else None

    def page_texts(self, sha):
        folder = os.path.join(self.dir, 'pages', sha)
        if os.path.isdir(folder):
            names = [n for n in os.listdir(folder) if n.endswith('.txt')]
            by_page = {int(n[:-4]): n for n in names if n[:-4].isdigit()}
            top = max(by_page) if by_page else 0
            pages = []
            for p in range(1, top + 1):
                if p in by_page:
                    with open(os.path.join(folder, by_page[p]), encoding='utf-8') as fh:
                        pages.append(clean_text(fh.read()))
                else:
                    pages.append('')
            return pages, 'sidecar'
        chunks = os.path.join(self.dir, 'chunks', sha + '.jsonl')
        if os.path.exists(chunks):
            rows = []
            with open(chunks, encoding='utf-8') as fh:
                for line in fh:
                    if line.strip():
                        r = json.loads(line)
                        rows.append((r['page'], r['ordinal'], r.get('start_char'),
                                     r.get('end_char'), r.get('text')))
            rows.sort(key=lambda r: (r[0], r[1]))
            doc = self.docs.get(sha) or {}
            return pages_from_chunks(rows, doc.get('pages')), 'chunks'
        raise CorpusUnavailable('fixture has no pages or chunks for %s' % sha)

    def candidates(self, shard=0, shards=1):
        out = {}
        for doc in self.documents(shard, shards):
            try:
                pages, _ = self.page_texts(doc['sha256'])
            except CorpusUnavailable:
                continue
            hits = sum(len(WORKINGS_RE.findall(p)) for p in pages)
            if hits:
                out[doc['sha256']] = hits
        return out

    def put_status(self, key, payload):
        path = os.path.join(self.state_dir, *key.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=1, sort_keys=True, default=str)
        os.replace(tmp, path)


# ---------------------------------------------------------------- stages
#
# Each stage is a function returning a plain dict with a ``stage`` key and a
# ``status``; process_document() strings them together and writes one ledger
# row per (document, mine) from what they report.  Nothing below invents:
# every drop carries a reason, every build goes through the service's own
# run_build, and the answer policy is the one autopopulate documents.

RESOLVER_CONTRACT = """\
resolve(doc) -> {'status': 'resolved'|'parked',
                 'candidates': [{'mine_key', 'kind', 'name', 'lon', 'lat',
                                 'state', 'county', 'method', 'confidence',
                                 'evidence'}],
                 'reason': str}
A resolved result carries exactly one physical mine; ambiguity is a park
with every candidate listed.  geomodel_corpus.targets_for(doc, pages, index)
turns a resolution — and, for a district/county file, every mine the text
names that resolves on its own — into carve targets with the other names
that must cut each window.  This driver never picks between candidates."""


def load_resolver(root=None, log=None):
    """``geomodel_corpus.SiteIndex.load(root)``, or ``None`` when the module
    has no such attribute — the driver then parks every document with
    ``resolver unavailable`` rather than guessing a mine from a name."""
    cls = getattr(corpus, 'SiteIndex', None)
    if cls is None:
        if log:
            log('geomodel_corpus.SiteIndex is not available; every document parks')
        return None
    return cls.load(root or ROOT)


def _target_from_candidate(cand, doc):
    """A carve target when targets_for is not available: the resolved
    candidate's own name cores against every other name the document lists."""
    cores = {corpus.core_label(a) for a in [cand.get('name')] + list(cand.get('aliases') or ())}
    cores = {c for c in cores if c and len(c) >= 3}
    others = {corpus.core_label(n) for n in (doc.get('mine_names') or ())}
    others = {c for c in others if c and len(c) >= 3 and c.lower() not in {x.lower() for x in cores}}
    return {'mine_key': cand['mine_key'], 'cores': sorted(cores),
            'other_cores': sorted(others), 'candidate': cand, 'via': 'resolved'}


def resolve_stage(doc, pages, resolver):
    """Which located mines this document describes — never a guess.

    Returns ``{'stage': 'resolve', 'status': 'resolved'|'parked', 'reason',
    'targets': [...], 'resolution': <the resolver's own result>}``.  A
    target is what ``targets_for`` returns: ``mine_key``, ``cores``,
    ``other_cores``, ``candidate``, ``via``.  A missing resolver, a resolver
    result of an unexpected shape, or a candidate without a coordinate (for
    a non-grades kind) parks with a reason.
    """
    if resolver is None:
        return {'stage': 'resolve', 'status': 'parked', 'reason': 'resolver unavailable',
                'targets': [], 'resolution': None}
    doc = dict(doc)
    doc.setdefault('front_end_ids', doc.get('mine_map') or [])
    try:
        targets_for = getattr(corpus, 'targets_for', None)
        if targets_for is not None:
            got = targets_for(doc, pages, index=resolver)
            resolution, targets = got['resolution'], list(got['targets'])
        else:
            resolution = resolver.resolve(doc)
            targets = ([_target_from_candidate(resolution['candidates'][0], doc)]
                       if resolution.get('status') == 'resolved' and resolution.get('candidates')
                       else [])
    except Exception as exc:
        return {'stage': 'resolve', 'status': 'parked',
                'reason': 'resolver failed: %s: %s' % (type(exc).__name__, str(exc)[:160]),
                'targets': [], 'resolution': None}
    if not isinstance(resolution, dict) or resolution.get('status') not in ('resolved', 'parked'):
        return {'stage': 'resolve', 'status': 'parked',
                'reason': 'resolver returned an unexpected shape', 'targets': [],
                'resolution': None}
    ok, dropped = [], []
    for t in targets:
        cand = t.get('candidate') or {}
        key = t.get('mine_key') or cand.get('mine_key')
        if not key:
            dropped.append('target without a mine_key')
            continue
        if not str(key).startswith('grades:') and (cand.get('lon') is None or cand.get('lat') is None):
            dropped.append('%s has no coordinate' % key)
            continue
        if not t.get('cores'):
            dropped.append('%s has no name core to anchor a section' % key)
            continue
        ok.append(t)
    if not ok:
        reason = resolution.get('reason') or 'no located site'
        if dropped:
            reason = '%s; %s' % (reason, '; '.join(dropped))
        return {'stage': 'resolve', 'status': 'parked', 'reason': reason,
                'targets': [], 'resolution': resolution}
    return {'stage': 'resolve', 'status': 'resolved', 'reason': resolution.get('reason'),
            'targets': ok, 'resolution': resolution, 'dropped': dropped}


def carve_stage(pages, target):
    """The stretch of the document that is about this one mine —
    ``geomodel_corpus.sections`` exactly as autopopulate uses it, the
    sections of one document joined into one description."""
    got = corpus.sections(pages, set(target['cores']), set(target.get('other_cores') or ()))
    text = '\n\n'.join(s['text'] for s in got)
    return {'stage': 'carve', 'status': 'carved' if got else 'no-section',
            'sections': len(got), 'text': text, 'chars': len(text),
            'pages': sorted({p for s in got for p in s['pages']}),
            'spans': [list(s['span']) for s in got]}


def parse_stage(text, mine_key, site_kind):
    """narrative.parse + assay.attach + narrative.lexicon — the same read
    the service makes, plus the vocabulary census the card shows."""
    spec = assay.attach(narrative.parse(text, mine_id=mine_key if site_kind == 'grades' else None),
                        text)
    required = narrative.unresolved(spec)
    in_question = {g.get('element') for g in required if g.get('element')}
    survivors = [e['id'] for e in spec['elements'] if e['id'] not in in_question]
    return {'stage': 'parse', 'status': 'elements' if spec['elements'] else 'no-elements',
            'spec': spec, 'lexicon': narrative.lexicon(text),
            'elements': len(spec['elements']), 'questions': len(spec['gaps']),
            'required': len(required), 'mentions': len(spec['mentions']),
            'assays': len(spec.get('assays') or []), 'survivors': survivors,
            'coverage': spec.get('coverage') or {}}


def compose_stage(text, spec):
    """``composition.compose`` + ``composition.attach`` when the module exists.

    run_build reads the text itself (``minevis.tools._read``) and attaches
    the same composition before it builds, so this stage exists for the
    ledger — how many minerals and statements the text names — and for a
    checkout whose modeller predates the module, where the ledger says so."""
    try:
        from geomodel import composition
    except ImportError:
        return {'stage': 'compose', 'status': 'unavailable', 'spec': spec,
                'note': 'geomodel.composition is not installed; nothing composed',
                'minerals': 0, 'statements': 0}
    comp = composition.compose(text, spec)
    attached = composition.attach(spec, comp)
    return {'stage': 'compose', 'status': 'attached', 'spec': attached, 'composition': comp,
            'minerals': len(comp.get('minerals') or []),
            'statements': len(comp.get('statements') or []),
            'commodities': [c.get('commodity') for c in (comp.get('commodities') or [])]}


# --------------------------------------------------------------- answers
class Answerer(object):
    """The seam for a future model-backed answerer.  Contract:

    ``answer(question, text, spec)`` returns ``None`` — the question is
    answered ``null`` (omit) — or ``{'value': v, 'because': str, 'quote': str}``
    where ``quote`` is a VERBATIM substring of ``text`` (the carved
    description) that states the value.  The driver checks the quote itself
    and discards, with a warning in the ledger, any answer whose quote is
    absent from the text; a discarded answer is an omit.  A value that
    survives is recorded in the manifest exactly as an agent's answer would
    be — ``assumed``, dotted, with the justification and the quote — so an
    auditor still separates what the document said from what the answerer
    read into it.  It may never return a value without a quote.
    """

    name = 'answerer'

    def answer(self, question, text, spec):
        raise NotImplementedError


class NullAnswerer(Answerer):
    """The unattended policy: every question is omitted."""

    name = 'null'

    def answer(self, question, text, spec):
        return None


def answers_for(questions, text, spec, answerer, warnings):
    """One answer per question, in the shape run_build takes."""
    out = []
    for q in questions:
        got = None
        if answerer is not None and not isinstance(answerer, NullAnswerer):
            try:
                got = answerer.answer(q, text, spec)
            except Exception as exc:
                warnings.append('answerer %s failed on %s: %s' % (
                    answerer.name, q.get('id'), str(exc)[:120]))
                got = None
        if isinstance(got, dict) and got.get('value') is not None:
            quote = str(got.get('quote') or '')
            if quote and quote in (text or ''):
                out.append({'id': q['id'], 'value': got['value'],
                            'because': '%s: %s [quote: %s]' % (
                                answerer.name, got.get('because') or 'no justification given',
                                quote)})
                continue
            warnings.append('answer to %s from %s discarded: its quote is not in the '
                            'carved text; omitted instead' % (q.get('id'), answerer.name))
        out.append({'id': q['id'], 'value': None, 'because': autop.OMIT_BECAUSE})
    return out


# ----------------------------------------------------------------- build
def build_stage(key, site_kind, site_ref, text, ctx, context=False, answerer=None,
                spec=None, log=None):
    """One (mine, description) through ``services/minevis/tools.run_build`` —
    a port of ``geomodel_autopopulate.build_one`` with one seam added: the
    answers come from :func:`answers_for`, which with the default
    :class:`NullAnswerer` gives exactly the omit answers build_one gives
    (tests/test_ws13_geomodel.py asserts the parity).  Returns build_one's
    shape: ``{'state': 'done'|'skipped'|'error', ...}``."""
    log = log or (lambda *a: None)
    warnings = []
    spec = spec or narrative.parse(text, mine_id=key if site_kind == 'grades' else None)
    if not spec['elements']:
        return {'state': 'skipped', 'reason': 'no-elements',
                'coverage': spec['coverage'], 'mentions': len(spec['mentions']),
                'warnings': warnings}

    args = {'text': text, 'context': bool(context)}
    if site_kind == 'grades':
        args['mine_id'] = key
    else:
        args['lon'], args['lat'] = site_ref['lon'], site_ref['lat']
        if site_ref.get('name'):
            args['name'] = site_ref['name']
    element_ids = {e['id'] for e in spec['elements']}
    omitted, answers_given = set(), []
    for _ in range(autop.MAX_ROUNDS):
        state, result = minevis_tools.run_build(dict(args), ctx)
        if state != 'questions':
            break
        questions = result.get('questions') or []
        if not questions:
            return {'state': 'error', 'error': 'questions-without-questions',
                    'detail': result, 'warnings': warnings}
        new = answers_for(questions, text, spec, answerer, warnings)
        answers_given.extend(new)
        omitted.update(q['element'] for q, a in zip(questions, new)
                       if q.get('element') and a['value'] is None)
        if not element_ids - omitted:
            # every parsed element is now slated for omission; building would
            # only publish an empty model, so record the outcome instead
            return {'state': 'skipped', 'reason': 'all-elements-omitted',
                    'answers': answers_given, 'spec_id': result['spec_id'],
                    'warnings': warnings}
        args = {'spec_id': result['spec_id'], 'context': bool(context), 'answers': new}
    else:
        return {'state': 'error', 'error': 'question-loop-did-not-converge',
                'answers': answers_given, 'warnings': warnings}

    if state == 'error':
        result = dict(result)
        result.setdefault('state', 'error')
        return {'state': 'error', 'error': result.get('error'),
                'detail': result.get('detail'), 'answers': answers_given,
                'warnings': warnings}

    # emptiness is judged on built elements (workings AND stopes), not on the
    # workings line count alone — a stope is a mesh, not a line
    built_elements = sum((result.get('confidence') or {}).values())
    if not built_elements:
        if isinstance(ctx.target, publish.LocalTarget) and result.get('key_prefix'):
            import shutil
            shutil.rmtree(os.path.join(ctx.target.root, *result['key_prefix'].split('/')),
                          ignore_errors=True)
        return {'state': 'skipped', 'reason': 'all-elements-omitted',
                'answers': answers_given, 'spec_id': result.get('spec_id'),
                'warnings': warnings}
    out = {'state': 'done', 'answers': answers_given,
           'omitted_elements': sorted(omitted & element_ids), 'warnings': warnings}
    held = ctx.specs.get(result.get('spec_id')) if result.get('spec_id') else None
    if held:
        held_spec = held.get('spec') or {}
        out['assay_commodities'] = sorted({a.get('commodity')
                                           for a in held_spec.get('assays') or ()
                                           if a.get('commodity')})
        out['level_depths_m'] = {k: v for k, v in (held_spec.get('levels') or {}).items()
                                 if isinstance(v, (int, float))}
    out.update(result)
    out['warnings'] = warnings + list(result.get('warnings') or [])
    return out


# --------------------------------------------------------------- publish
def content_hash(text, context):
    """What rerun safety compares: the carved text's hash plus every version
    that shapes the model and the policy that answered its questions."""
    blob = json.dumps({
        'text_sha256': hashlib.sha256((text or '').encode('utf-8')).hexdigest(),
        'parser': narrative.PARSER_VERSION,
        'builder': agentbuild.BUILDER_VERSION,
        'publisher': publish.PUBLISHER_VERSION,
        'driver': DRIVER_VERSION,
        'policy': POLICY,
        'context': bool(context),
    }, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def source_document_block(doc, rights, carve, text_source):
    """What the manifest carries about the document a model was derived from:
    identity, where the text came from, and the rights that travel with it."""
    return {
        'corpus': 'ws13',
        'sha256': doc.get('sha256'),
        'title': doc.get('title'),
        'portal': doc.get('portal'),
        'state': doc.get('state'),
        'county': doc.get('county'),
        'doc_date': doc.get('doc_date'),
        'doc_year_min': doc.get('doc_year_min'),
        'doc_year_max': doc.get('doc_year_max'),
        'pages': carve.get('pages') or [],
        'sections': carve.get('sections') or 0,
        'spans': carve.get('spans') or [],
        'text_source': text_source,
        'source_url': rights.get('source_url'),
        'rights_basis': rights.get('rights_basis'),
        'public_domain': rights.get('public_domain'),
        'admission_class': rights.get('admission_class'),
        'rights_terms': rights.get('rights_terms'),
        'attribution_required': rights.get('attribution_required'),
        'non_commercial': rights.get('non_commercial'),
        'share_alike': rights.get('share_alike'),
        'note': 'this model was derived from the document above and carries its terms; '
                'attribution_required / non_commercial / share_alike are the citation '
                "resolver's own flags for the document's admission_class",
    }


def publish_stage(build, doc, rights, carve, text_source, target):
    """The rights ride on the published manifest.

    run_build has already written ``models/<id>/`` through ``target``; this
    reads the manifest back, adds ``source_document`` (identity, carved
    pages, text source, and the rights block from :func:`rights_for`) and
    writes it again.  Idempotent: an unchanged republish leaves an amended
    manifest alone.  Refusal happens BEFORE the build (rights_for in
    process_document), so a document without stateable rights never reaches
    the target at all; this stage asserts that invariant rather than
    trusting it."""
    if build.get('state') != 'done':
        return {'stage': 'publish', 'status': 'not-built'}
    if not rights or rights.get('status') != 'ok':
        raise RuntimeError('publish_stage reached with refused rights for %s' % doc.get('sha256'))
    key = '%s/manifest.json' % build['key_prefix']
    raw = target.get(key)
    if raw is None:
        return {'stage': 'publish', 'status': 'error',
                'reason': 'manifest missing at %s after publish' % key}
    try:
        man = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        return {'stage': 'publish', 'status': 'error',
                'reason': 'manifest unreadable at %s: %s' % (key, exc)}
    block = source_document_block(doc, rights['rights'], carve, text_source)
    amended = False
    if man.get('source_document') != block:
        man['source_document'] = block
        target.put(key, json.dumps(man, indent=1, sort_keys=True, default=str).encode('utf-8'),
                   'application/json')
        amended = True
    return {'stage': 'publish', 'status': 'published', 'model_id': build['model_id'],
            'manifest_key': key, 'amended': amended,
            'republished': bool(build.get('republished'))}


# ----------------------------------------------------------------- index
def results_path(shard, shards, results_dir=None):
    return os.path.join(results_dir or RESULTS_DIR, 'ws13-results-%d.jsonl' % shard
                        if shards > 1 else 'ws13-results.jsonl')


def index_path_for(site_dir, shard, shards):
    """One index per shard: 64 processes must not race on one file.  Merging
    the per-shard indexes into ``data/models/index.json`` is a separate step."""
    name = ('index-%04d-of-%04d.json' % (shard, shards)) if shards > 1 else 'index.json'
    return os.path.join(site_dir, 'data', 'models', name)


def index_stage(entries, site_dir, shard, shards, results_dir=None, log=None):
    """Hand the per-mine entries to ``geomodel_autopopulate.write_index``
    (compact index + one card.json per model) when it exists; otherwise
    append them to ``var/geomodel/ws13-results-<shard>.jsonl`` and say so."""
    log = log or (lambda *a: None)
    writer = getattr(autop, 'write_index', None)
    if writer is None:
        path = results_path(shard, shards, results_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            for entry in entries:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + '\n')
        log('index writer unavailable (geomodel_autopopulate.write_index); %d result(s) '
            'appended to %s' % (len(entries), path))
        return {'stage': 'index', 'status': 'deferred', 'path': path, 'entries': len(entries)}
    index_path = index_path_for(site_dir, shard, shards)
    previous = {}
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding='utf-8') as fh:
                previous = json.load(fh).get('by_mine') or {}
        except (OSError, ValueError):
            previous = {}
    got = writer(entries, site_dir, previous=previous, index_path=index_path,
                 merge_previous=True, stats={'driver': DRIVER_VERSION, 'shard': shard,
                                             'shards': shards}, log=log)
    return {'stage': 'index', 'status': 'written', 'index_path': got['index_path'],
            'cards': got.get('cards'), 'models': len(got.get('model_ids') or ()),
            'entries': len(entries)}


def _model_record(build, doc, carve):
    """The per-model record autopopulate's index entries carry."""
    confidence = build.get('confidence') or {}
    omitted = build.get('omitted_elements') or []
    model = {
        'model_id': build['model_id'],
        'project_url': build.get('project_url'),
        'model_url': build.get('model_url'),
        'doc_id': doc.get('sha256'), 'doc_title': doc.get('title'),
        'source_url': doc.get('source_url'),
        'publication_year': doc.get('doc_year_min'),
        'pages': carve.get('pages') or [],
        'confidence': confidence,
        'elements': sum(confidence.values()) if confidence else None,
        'omitted': len(omitted),
        'summary': build.get('summary'),
        'levels': build.get('levels'),
        'level_depths_m': build.get('level_depths_m') or {},
        'assay_commodities': build.get('assay_commodities') or [],
        'assays': build.get('assays') or 0,
        'vein': bool(build.get('vein')),
        'republished': build.get('republished'),
    }
    if build.get('composition') is not None:
        model['composition'] = build['composition']
    return model


def _document_record(doc, carve):
    return {'doc_id': doc.get('sha256'), 'title': doc.get('title'),
            'source_url': doc.get('source_url'), 'catalog_url': None,
            'publication_year': doc.get('doc_year_min'),
            'citation': None, 'pages': doc.get('pages'),
            'cited_pages': carve.get('pages') or [], 'sections': carve.get('sections') or 0}


class MineEntries(object):
    """Per-mine index entries accumulated over a sweep, in the shape
    ``geomodel_autopopulate.run`` builds and ``write_index`` takes."""

    def __init__(self, grades=None):
        self.entries = {}
        self._grades = grades

    def grades(self):
        if self._grades is None:
            try:
                self._grades = corpus._load_grades()
            except (OSError, ValueError):
                self._grades = {'n': 0}
        return self._grades

    def add(self, target, doc, carve, lexicon, build=None, errored=False):
        cand = target['candidate']
        key = target['mine_key']
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = {
                'key': key, 'label': cand.get('name') or key,
                'site_kind': 'grades' if key.startswith('grades:') else 'latlon',
                'methods': [], 'store_mine_ids': [], 'grade_rows': list(cand.get('rows') or []),
                'documents': [], 'models': [], 'primary': None,
                'lexicon': {'kinds': {}, 'verbs': {}, 'level_labels': [],
                            'sentences': 0, 'mining_sentences': 0},
                'minerals': [], 'extent': None, 'errored': False,
                '_unit': {'site_kind': 'grades' if key.startswith('grades:') else 'latlon',
                          'grade_rows': list(cand.get('rows') or [])}}
        if cand.get('method') and cand['method'] not in entry['methods']:
            entry['methods'].append(cand['method'])
        for mid in doc.get('mine_ids') or ():
            if mid not in entry['store_mine_ids']:
                entry['store_mine_ids'].append(mid)
        if not any(d['doc_id'] == doc.get('sha256') for d in entry['documents']):
            entry['documents'].append(_document_record(doc, carve))
        if lexicon:
            autop._merge_lexicon(entry['lexicon'], lexicon)
        if build and build.get('state') == 'done':
            entry['models'].append(_model_record(build, doc, carve))
        entry['errored'] = entry['errored'] or bool(errored)

    def finish(self):
        out = []
        for key in sorted(self.entries):
            entry = dict(self.entries[key])
            unit = entry.pop('_unit')
            models = entry['models']

            def strength(m):
                c = m.get('confidence') or {}
                return (c.get('surveyed', 0) + c.get('described', 0),
                        (m.get('summary') or {}).get('total_m') or 0.0,
                        m.get('publication_year') or 0)
            models.sort(key=strength, reverse=True)
            for i, model in enumerate(models):
                model['primary'] = (i == 0)
            entry['primary'] = models[0]['model_id'] if models else None
            entry['minerals'] = autop._minerals(unit, self.grades(), models)
            entry['extent'] = autop._extent(models)
            if not entry['documents']:
                entry['lexicon'] = None
            out.append(entry)
        return out


# ---------------------------------------------------------------- funnel
class Funnel(object):
    """Documents surviving each step, and why the rest dropped."""

    STEPS = ('documents', 'with_mine_ids_or_names', 'with_workings_vocabulary',
             'rights_permit_a_derived_model', 'resolvable_to_a_located_site',
             'parseable_elements', 'publishable')

    def __init__(self):
        self.counts = dict((s, 0) for s in self.STEPS)
        self.drops = dict((s, {}) for s in self.STEPS)

    def reached(self, step):
        self.counts[step] += 1

    def dropped(self, step, reason):
        reason = _reason_key(reason)
        self.drops[step][reason] = self.drops[step].get(reason, 0) + 1

    def as_dict(self):
        return {'steps': [{'step': s, 'count': self.counts[s], 'dropped': dict(self.drops[s])}
                          for s in self.STEPS]}

    def lines(self):
        width = max(len(s) for s in self.STEPS)
        out = ['%-*s  %8s  dropped, by reason' % (width, 'step', 'count')]
        for s in self.STEPS:
            drops = self.drops[s]
            head = '%-*s  %8d' % (width, s, self.counts[s])
            if not drops:
                out.append(head)
                continue
            first = True
            for reason, n in sorted(drops.items(), key=lambda kv: (-kv[1], kv[0])):
                out.append('%s  %6d  %s' % (head if first else ' ' * len(head), n, reason))
                first = False
        return out


def _reason_key(reason):
    """A drop reason as a bucket: the leading clause, without identifiers."""
    text = str(reason or 'unspecified')
    text = text.split(';')[0].strip()
    text = re.sub(r'\b[0-9a-f]{12,}\b', '<sha>', text)
    return text[:96]


def doc_terminal(rows, sha):
    """Is this document out of the work set?  Every row it has is terminal,
    or an error that has used its attempts; a document with no row is work."""
    mine = [r for (s, _), r in rows.items() if s == sha]
    if not mine:
        return False
    for r in mine:
        if r['status'] in TERMINAL:
            continue
        if r['status'] == 'error' and int(r.get('attempts') or 0) >= MAX_ATTEMPTS:
            continue
        return False
    return True


# ---------------------------------------------------------- one document
def process_document(doc, hits, env):
    """Every stage over one document; one ledger row per (document, mine).

    ``env`` carries corp, ctx, resolver, answerer, run_id, opts, funnel,
    stats, entries, ledger_rows and log.  Returns the per-target outcomes
    ``[{'mine_key', 'status', 'reason', 'model_id'}]``.
    """
    sha, opts, log = doc['sha256'], env.opts, env.log
    recording = not opts.plan
    outcomes = []

    def prev_attempts(key):
        prev = env.ledger_rows.get((sha, key))
        return (int(prev.get('attempts') or 0) if prev else 0) + 1

    def record(key, status, reason=None, model_id=None, chash=None, counts=None, warnings=None):
        outcomes.append({'mine_key': key, 'status': status, 'reason': reason,
                         'model_id': model_id})
        if not recording:
            return
        env.put(ledger_row(sha, key, env.run_id, status, reason, model_id, chash,
                           counts, warnings, prev_attempts(key)))

    rights = rights_for(doc)
    if rights['status'] == 'refused':
        env.funnel.dropped('rights_permit_a_derived_model', rights['reason'])
        record(NO_MINE, 'parked', rights['reason'], counts={'vocab_hits': hits})
        log('%s parked: %s' % (sha[:12], rights['reason']))
        return outcomes
    env.funnel.reached('rights_permit_a_derived_model')

    try:
        pages, text_source = env.corp.page_texts(sha)
    except Exception as exc:
        reason = 'text: %s: %s' % (type(exc).__name__, str(exc)[:160])
        env.funnel.dropped('resolvable_to_a_located_site', reason)
        record(NO_MINE, 'error', reason, counts={'vocab_hits': hits})
        log('%s error: %s' % (sha[:12], reason))
        return outcomes

    res = resolve_stage(doc, pages, env.resolver)
    if res['status'] == 'parked':
        env.funnel.dropped('resolvable_to_a_located_site', res['reason'])
        record(NO_MINE, 'parked', res['reason'],
               counts={'vocab_hits': hits, 'text_source': text_source, 'pages': len(pages),
                       'candidates': len((res.get('resolution') or {}).get('candidates') or ())})
        log('%s parked: %s' % (sha[:12], res['reason']))
        return outcomes
    env.funnel.reached('resolvable_to_a_located_site')

    any_elements, any_publishable, blockers = False, False, []
    for target in res['targets']:
        key, cand = target['mine_key'], target['candidate']
        site_kind = 'grades' if key.startswith('grades:') else 'latlon'
        carve = carve_stage(pages, target)
        counts = {'vocab_hits': hits, 'text_source': text_source,
                  'sections': carve['sections'], 'pages': carve['pages'],
                  'chars': carve['chars'], 'method': cand.get('method'),
                  'via': target.get('via'), 'kind': cand.get('kind')}
        if not carve['text']:
            reason = 'no-section: no name-anchored window describes development'
            record(key, 'skipped', reason, counts=counts)
            blockers.append(reason)
            if recording:
                env.entries.add(target, doc, carve, None)
            continue

        parse = parse_stage(carve['text'], key, site_kind)
        comp = compose_stage(carve['text'], parse['spec'])
        counts.update({'elements': parse['elements'], 'questions': parse['questions'],
                       'required': parse['required'], 'mentions': parse['mentions'],
                       'assays': parse['assays'], 'survivors': len(parse['survivors']),
                       'minerals': comp.get('minerals', 0),
                       'composition_statements': comp.get('statements', 0)})
        warnings = [] if comp['status'] == 'attached' else [comp['note']]
        chash = content_hash(carve['text'], opts.context)

        # --rebuild reopens finished DOCUMENTS; it does not defeat this test.
        # The hash carries every version that shapes a model, so a new
        # builder changes it and a rebuild then rebuilds; an unchanged one
        # is the same model at the same address and is not built twice.
        prev = env.ledger_rows.get((sha, key))
        if (recording and not opts.dry_run and prev
                and prev.get('status') == 'published' and prev.get('content_hash') == chash):
            record(key, 'published', 'unchanged: skipped on rerun', prev.get('model_id'),
                   chash, counts, warnings)
            env.stats['models_unchanged'] += 1
            any_elements = any_publishable = True
            log('%s %s unchanged (%s)' % (sha[:12], key, prev.get('model_id')))
            continue

        if parse['elements'] == 0:
            reason = 'no-elements: %d question(s), %d mention(s)' % (
                parse['questions'], parse['mentions'])
            record(key, 'skipped', reason, counts=counts, warnings=warnings)
            blockers.append(reason)
            if recording:
                env.entries.add(target, doc, carve, parse['lexicon'])
            continue
        any_elements = True

        if opts.plan or opts.dry_run:
            if parse['survivors']:
                any_publishable = True
            else:
                blockers.append('all-elements-omitted (projected)')
            if opts.dry_run:
                record(key, 'planned', 'dry-run: %d element(s), %d survive the omit policy' % (
                    parse['elements'], len(parse['survivors'])), None, chash, counts, warnings)
            continue

        build = build_stage(key, site_kind, cand, carve['text'], env.ctx,
                            context=opts.context, answerer=env.answerer,
                            spec=parse['spec'], log=log)
        warnings.extend(build.get('warnings') or [])
        counts['answers'] = len(build.get('answers') or [])
        if build['state'] == 'skipped':
            record(key, 'skipped', build['reason'], counts=counts, warnings=warnings)
            blockers.append(build['reason'])
            env.entries.add(target, doc, carve, parse['lexicon'])
            log('%s %s skipped: %s' % (sha[:12], key, build['reason']))
            continue
        if build['state'] == 'error':
            reason = '%s: %s' % (build.get('error'), str(build.get('detail') or '')[:160])
            record(key, 'error', reason, counts=counts, warnings=warnings)
            blockers.append(reason)
            env.entries.add(target, doc, carve, parse['lexicon'], errored=True)
            env.stats['errors'] += 1
            log('%s %s error: %s' % (sha[:12], key, reason))
            continue

        pub = publish_stage(build, doc, rights, carve, text_source, env.ctx.target)
        confidence = build.get('confidence') or {}
        counts.update({'built': sum(confidence.values()), 'confidence': confidence,
                       'omitted': len(build.get('omitted_elements') or []),
                       'republished': bool(build.get('republished'))})
        if pub['status'] != 'published':
            reason = 'publish: %s' % pub.get('reason')
            record(key, 'built', reason, build.get('model_id'), chash, counts, warnings)
            blockers.append(reason)
            env.entries.add(target, doc, carve, parse['lexicon'], errored=True)
            env.stats['errors'] += 1
            continue
        record(key, 'published', 'published' if build.get('republished') else
               'published: already at this address', build['model_id'], chash, counts, warnings)
        env.entries.add(target, doc, carve, parse['lexicon'], build)
        env.stats['models_published'] += 1
        any_publishable = True
        log('%s %s -> %s (%d described, %d assumed, %d omitted)' % (
            sha[:12], key, build['model_id'], confidence.get('described', 0),
            confidence.get('assumed', 0), len(build.get('omitted_elements') or [])))

    if any_elements:
        env.funnel.reached('parseable_elements')
    else:
        env.funnel.dropped('parseable_elements', blockers[0] if blockers else 'no elements')
        return outcomes
    if any_publishable:
        env.funnel.reached('publishable')
    else:
        env.funnel.dropped('publishable', blockers[0] if blockers else 'nothing published')
    return outcomes


# --------------------------------------------------------------- the sweep
def status_key(shard, shards):
    """One key per shard once sharded; the documented single key otherwise."""
    if shards <= 1:
        return '%s/status.json' % STATUS_PREFIX
    return '%s/status-%04d-of-%04d.json' % (STATUS_PREFIX, shard, shards)


def status_payload(env, phase, work_set, done, remaining):
    elapsed = max(1e-6, time.time() - env.started)
    docs_rate = env.stats['documents_processed'] / elapsed
    models = env.stats['models_published']
    return {
        'generated': now_iso(), 'phase': phase, 'driver': DRIVER_VERSION,
        'run_id': env.run_id, 'shard': env.shard, 'shards': env.shards,
        'corpus': env.corp.name, 'elapsed_seconds': round(elapsed, 1),
        'documents_in_shard': env.stats['documents_seen'],
        'documents_work_set': work_set,
        'documents_done': done,
        'documents_remaining': remaining,
        'documents_processed_this_sweep': env.stats['documents_processed'],
        'documents_finished_this_sweep': env.stats['documents_finished'],
        'models_published': models,
        'models_unchanged': env.stats['models_unchanged'],
        'errors': env.stats['errors'],
        'parked_by_reason': dict(env.stats['parked_by_reason']),
        'skipped_by_reason': dict(env.stats['skipped_by_reason']),
        'documents_per_second': round(docs_rate, 4),
        'seconds_per_document': round(1.0 / docs_rate, 2) if docs_rate else None,
        'models_per_second': round(models / elapsed, 4),
        'eta_hours': (round(remaining / docs_rate / 3600.0, 2)
                      if docs_rate and remaining else None),
        'funnel': env.funnel.as_dict(),
    }


def _new_stats():
    return {'documents_seen': 0, 'documents_processed': 0, 'documents_finished': 0,
            'models_published': 0, 'models_unchanged': 0, 'errors': 0,
            'rows_written': 0, 'parked_by_reason': {}, 'skipped_by_reason': {}}


def make_env(corp, ctx, resolver, answerer, opts, shard, shards, log, grades=None):
    env_ns = {
        'corp': corp, 'ctx': ctx, 'resolver': resolver, 'answerer': answerer or NullAnswerer(),
        'opts': opts, 'shard': shard, 'shards': shards, 'log': log,
        'run_id': 'r-%s-%s' % (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                               uuid.uuid4().hex[:8]),
        'started': time.time(), 'funnel': Funnel(), 'stats': _new_stats(),
        'entries': MineEntries(grades), 'ledger_rows': {},
    }
    import types
    env = types.SimpleNamespace(**env_ns)

    def put(row):
        corp.ledger.put(row)
        env.ledger_rows[(row['sha256'], row['mine_key'])] = row
        env.stats['rows_written'] += 1
        bucket = {'parked': 'parked_by_reason', 'skipped': 'skipped_by_reason'}.get(row['status'])
        if bucket:
            key = _reason_key(row.get('reason'))
            env.stats[bucket][key] = env.stats[bucket].get(key, 0) + 1
    env.put = put
    return env


def run_shard(corp, shard, shards, ctx, resolver, answerer, opts, log, site_dir,
              results_dir=None, grades=None):
    """One sweep over one shard.  Returns the summary the exit code is
    decided from: ``work_set``, ``done_before``, ``finished_now``,
    ``remaining``, ``processed`` and the heartbeat payload."""
    env = make_env(corp, ctx, resolver, answerer, opts, shard, shards, log, grades)
    heartbeat = not (opts.plan or opts.dry_run)
    if not opts.plan:
        env.ledger_rows = corp.ledger.load(shard, shards)
    candidates = corp.candidates(shard, shards)
    work_set = done_before = deferred = 0
    last_beat = time.time()

    def beat(phase):
        remaining = max(0, work_set - done_before - env.stats['documents_finished'])
        payload = status_payload(env, phase, work_set,
                                 done_before + env.stats['documents_finished'], remaining)
        try:
            corp.put_status(status_key(shard, shards), payload)
        except Exception as exc:
            log('heartbeat failed: %s: %s' % (type(exc).__name__, exc))
        return payload

    if opts.doc:
        one = corp.document(opts.doc)
        docs = [one] if one else []
        if not docs:
            log('no document %s in the corpus' % opts.doc)
    else:
        docs = corp.documents(shard, shards)

    for doc in docs:
        sha = doc['sha256']
        env.stats['documents_seen'] += 1
        env.funnel.reached('documents')
        if not (doc.get('mine_ids') or doc.get('mine_names')) and not opts.doc:
            env.funnel.dropped('with_mine_ids_or_names', 'no mine ids or names')
            continue
        env.funnel.reached('with_mine_ids_or_names')
        hits = candidates.get(sha, 0)
        if not hits and not opts.doc:
            env.funnel.dropped('with_workings_vocabulary', 'no workings vocabulary')
            continue
        env.funnel.reached('with_workings_vocabulary')
        work_set += 1
        if not opts.rebuild and not opts.doc and doc_terminal(env.ledger_rows, sha):
            done_before += 1
            continue
        if opts.limit is not None and env.stats['documents_processed'] >= opts.limit:
            deferred += 1
            continue
        process_document(doc, hits, env)
        env.stats['documents_processed'] += 1
        if doc_terminal(env.ledger_rows, sha):
            env.stats['documents_finished'] += 1
        if heartbeat and time.time() - last_beat >= HEARTBEAT_SECONDS:
            beat('running')
            last_beat = time.time()

    index = None
    entries = env.entries.finish() if not (opts.plan or opts.dry_run) else []
    if entries:
        index = index_stage(entries, site_dir, shard, shards, results_dir, log)
    payload = beat('finished') if heartbeat else status_payload(
        env, 'plan', work_set, done_before, max(0, work_set - done_before))
    summary = {
        'run_id': env.run_id, 'shard': shard, 'shards': shards, 'started': env.started,
        'work_set': work_set, 'done_before': done_before,
        'processed': env.stats['documents_processed'],
        'finished_now': env.stats['documents_finished'],
        'deferred': deferred,
        'remaining': max(0, work_set - done_before - env.stats['documents_finished']),
        'stats': env.stats, 'funnel': env.funnel, 'status': payload, 'index': index,
        'elapsed_seconds': round(time.time() - env.started, 1),
    }
    return summary


def exit_code_for(summary, opts):
    if opts.plan or opts.dry_run:
        return EXIT_SHARD_DONE
    if summary['remaining'] == 0:
        return EXIT_SHARD_DONE
    return EXIT_WORK_REMAINS if summary['finished_now'] else EXIT_NO_PROGRESS


# ------------------------------------------------------------------ plan
def capacity(shas, rate, shard_counts=PLAN_SHARDS):
    """Documents per shard for candidate shard counts, and the hours the
    worst shard takes at ``rate`` documents/second/process.  The worst shard
    sets the wall clock — the same argument WS13-RETRIEVAL.md makes for the
    confidence pass — so the table shows it beside the mean."""
    rows = []
    total = len(shas)
    for shards in shard_counts:
        totals = {}
        for sha in shas:
            k = shard_of(sha, shards)
            totals[k] = totals.get(k, 0) + 1
        worst = max(totals.values()) if totals else 0
        rows.append({'shards': shards, 'nodes': shards / float(NODE_VCPU),
                     'mean_documents': total / float(shards) if shards else 0.0,
                     'worst_documents': worst,
                     'even_hours': (total / (rate * shards) / 3600.0) if rate else None,
                     'worst_hours': (worst / rate / 3600.0) if rate else None})
    return {'documents': total, 'rate': rate,
            'seconds_per_document': (1.0 / rate) if rate else None,
            'node_type': NODE_TYPE, 'vcpu_per_node': NODE_VCPU, 'rows': rows}


def print_capacity(report, log):
    log('work set             : %s documents' % format(report['documents'], ','))
    log('rate                 : %.4f documents/s/process (%.2f s/document; a SEED '
        'unless you passed a measured --rate)' % (report['rate'], report['seconds_per_document']))
    log('fleet                : %s @ %d vCPU, one shard per vCPU' % (
        report['node_type'], report['vcpu_per_node']))
    log('shards  nodes   mean docs/shard   worst shard   even-split h   worst-shard h')
    for row in report['rows']:
        log('%6d  %5.1f   %15.1f   %11d   %10.2f h   %11.2f h' % (
            row['shards'], row['nodes'], row['mean_documents'], row['worst_documents'],
            row['even_hours'] or 0.0, row['worst_hours'] or 0.0))
    log('wall clock is the worst-shard column: the run ends when the last shard ends.')


def print_summary(summary, opts, log):
    stats = summary['stats']
    log('funnel (shard %s of %s):' % (summary['shard'], summary['shards']))
    for line in summary['funnel'].lines():
        log('  ' + line)
    if opts.plan or opts.dry_run:
        log('%s: nothing built, nothing published' % ('PLAN' if opts.plan else 'DRY RUN'))
    elapsed = max(1e-3, time.time() - summary['started'])
    log('processed %d document(s) in %.1fs: %.4f documents/s, %.4f models/s '
        '(%d published, %d unchanged, %d error(s))' % (
            summary['processed'], elapsed, summary['processed'] / elapsed,
            stats['models_published'] / elapsed, stats['models_published'],
            stats['models_unchanged'], stats['errors']))
    if stats['parked_by_reason']:
        log('parked by reason:')
        for reason, n in sorted(stats['parked_by_reason'].items(), key=lambda kv: -kv[1]):
            log('  %6d  %s' % (n, reason))
    if stats['skipped_by_reason']:
        log('skipped by reason:')
        for reason, n in sorted(stats['skipped_by_reason'].items(), key=lambda kv: -kv[1]):
            log('  %6d  %s' % (n, reason))
    log('work set %d: %d done before this sweep, %d finished now, %d deferred by --limit, '
        '%d remaining' % (summary['work_set'], summary['done_before'], summary['finished_now'],
                          summary['deferred'], summary['remaining']))
    if summary.get('index'):
        log('index: %s' % json.dumps(summary['index'], sort_keys=True, default=str))


# ---------------------------------------------------------------- verify
def verify_complete(corp, shards):
    """Per shard, documents still owed a decision — from the ledger and the
    corpus, never from fleet bookkeeping.  Exit 12 when any remain."""
    per_shard, exhausted = {}, {}
    for shard in range(shards):
        rows = corp.ledger.load(shard, shards)
        candidates = corp.candidates(shard, shards)
        remaining = gave_up = 0
        for doc in corp.documents(shard, shards):
            sha = doc['sha256']
            if not (doc.get('mine_ids') or doc.get('mine_names')) or not candidates.get(sha):
                continue
            if not doc_terminal(rows, sha):
                remaining += 1
            elif any(r['status'] == 'error' for (s, _), r in rows.items() if s == sha):
                gave_up += 1
        if remaining:
            per_shard[shard] = remaining
        if gave_up:
            exhausted[shard] = gave_up
    return {'shards': shards, 'documents_remaining': sum(per_shard.values()),
            'incomplete_shards': sorted(per_shard), 'shard_documents': per_shard,
            'exhausted_documents': sum(exhausted.values()), 'exhausted_shards': exhausted}


def print_verify(report, log):
    log('VERIFY over %d shards' % report['shards'])
    if report['documents_remaining']:
        log('INCOMPLETE: %d document(s) still owe a decision across %d shard(s): %s' % (
            report['documents_remaining'], len(report['incomplete_shards']),
            ', '.join(str(i) for i in report['incomplete_shards'][:40])))
    else:
        log('every shard is finished: 0 documents remain in the work set')
    if report['exhausted_documents']:
        log('NOT BUILT, NOT RETRIED: %d document(s) used all %d attempts and left the work '
            'set with an error row; read their reasons from the ledger' % (
                report['exhausted_documents'], MAX_ATTEMPTS))


# --------------------------------------------------------- migrate / check
def migrate(conn, sql_path=DEFAULT_SQL, echo=True):
    """Apply ws13_geomodel_migrations.sql in one transaction, the way
    ws13_migrate.py applies its file (its lexer, its per-statement report)."""
    import ws13_migrate
    with open(sql_path, encoding='utf-8') as fh:
        statements = ws13_migrate.split_statements(fh.read())
    with conn.transaction():
        return ws13_migrate.apply_migrations(conn, statements, echo=echo)


def run_checks(conn):
    """Read-only verification of the ledger table: ``[(name, ok, detail)]``."""
    results = []

    def record(name, ok, detail=''):
        results.append((name, bool(ok), detail))

    present = {row[0] for row in conn.execute(SQL['check_columns']).fetchall()}
    record('table ws13_geomodel_runs', bool(present), '' if present else 'missing')
    for column in LEDGER_COLUMNS:
        record('column ws13_geomodel_runs.%s' % column, column in present,
               '' if column in present else 'missing')
    constraints = {row[0]: row[1] for row in conn.execute(SQL['check_constraints']).fetchall()}
    for name in REQUIRED_CONSTRAINTS:
        record('constraint %s' % name, name in constraints,
               '' if name in constraints else 'missing')
    definition = constraints.get('ws13_geomodel_runs_status') or ''
    missing = [s for s in STATUSES if "'%s'" % s not in definition]
    record('status constraint admits every status', not missing,
           '' if not missing else 'does not admit %s' % ', '.join(missing))
    pkey = [n for n, d in constraints.items() if d.upper().startswith('PRIMARY KEY')]
    ok = any('sha256' in d and 'mine_key' in d for n, d in constraints.items()
             if d.upper().startswith('PRIMARY KEY'))
    record('primary key (sha256, mine_key)', ok, '' if ok else 'found %s' % (pkey or 'none'))
    indexes = {row[0] for row in conn.execute(SQL['check_indexes']).fetchall()}
    for name in REQUIRED_INDEXES:
        record('index %s' % name, name in indexes, '' if name in indexes else 'missing')
    return results


def print_checks(results, log):
    for name, ok, detail in results:
        log('%s %s%s' % ('ok  ' if ok else 'MISS', name, (': ' + detail) if detail else ''))
    gaps = [n for n, ok, _ in results if not ok]
    log('%d check(s), %d gap(s)' % (len(results), len(gaps)))
    return not gaps


# ------------------------------------------------------------ merge index
def merge_indexes(site_dir, log):
    """Fold every per-shard ``index-<i>-of-<n>.json`` into
    ``data/models/index.json`` through write_index's carry-forward path."""
    writer = getattr(autop, 'write_index', None)
    if writer is None:
        log('geomodel_autopopulate.write_index is not available; nothing merged')
        return None
    folder = os.path.join(site_dir, 'data', 'models')
    previous = {}
    names = sorted(n for n in (os.listdir(folder) if os.path.isdir(folder) else [])
                   if re.match(r'^index-\d{4}-of-\d{4}\.json$', n))
    for name in names:
        with open(os.path.join(folder, name), encoding='utf-8') as fh:
            previous.update(json.load(fh).get('by_mine') or {})
    got = writer([], site_dir, previous=previous, merge_previous=True,
                 stats={'driver': DRIVER_VERSION, 'merged_from': names}, log=log)
    log('merged %d per-shard index file(s) into %s (%d mines)' % (
        len(names), got['index_path'], len([e for e in got['by_mine'].values() if 'a' not in e])))
    return got


# ------------------------------------------------------------------- CLI
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--shard', type=int, default=int(os.environ.get('WS13_SHARD', '0')))
    parser.add_argument('--shards', type=int,
                        default=int(os.environ.get('WS13_SHARD_COUNT', '1')))
    parser.add_argument('--limit', type=int, default=None,
                        help='process at most this many documents this sweep (a sizing run)')
    parser.add_argument('--doc', default=None, help='one document by sha256')
    parser.add_argument('--plan', action='store_true',
                        help='the funnel and the capacity table; no builds, no ledger writes')
    parser.add_argument('--dry-run', action='store_true',
                        help='the funnel through parse, recorded as planned rows; no builds')
    parser.add_argument('--rebuild', action='store_true',
                        help='reopen finished documents; the content hash still skips '
                             'unchanged published models')
    parser.add_argument('--rate', type=float, default=DEFAULT_RATE,
                        help='documents/second/process for the capacity table; a SEED '
                             'unless measured by a --limit sweep')
    parser.add_argument('--verify-complete', action='store_true')
    parser.add_argument('--migrate', action='store_true', help='apply the ledger DDL')
    parser.add_argument('--check', action='store_true', help='verify the ledger DDL')
    parser.add_argument('--sql', default=DEFAULT_SQL)
    parser.add_argument('--index', action='store_true',
                        help='merge the per-shard indexes into data/models/index.json')
    parser.add_argument('--fixture', default=None,
                        help='use the offline FixtureCorpus in this directory')
    parser.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'))
    parser.add_argument('--bucket', default=os.environ.get('WS13_BUCKET'),
                        help='the corpus bucket (sidecars, heartbeat)')
    parser.add_argument('--publish', choices=('local', 's3'), default='local')
    parser.add_argument('--models-bucket', default=os.environ.get('NWMM_MODELS_BUCKET'),
                        help='the site bucket models/ are published to (--publish s3)')
    parser.add_argument('--site-dir', '--out', dest='site_dir',
                        default=os.path.join(ROOT or HERE, 'site'),
                        help='--publish local writes <site-dir>/models/; the index and '
                             'cards always land here')
    parser.add_argument('--state-dir', default=None,
                        help='spec store; the fixture ledger and heartbeat too')
    parser.add_argument('--results-dir', default=None)
    parser.add_argument('--base-url', default=None)
    parser.add_argument('--zoom', type=int, default=13)
    parser.add_argument('--offline', action='store_true',
                        help='never fetch terrain; a collar with no cached tile is '
                             'unplaceable, never sea level')
    parser.add_argument('--context', action='store_true',
                        help='terrain, draped geology and grade points around each mine '
                             '(slower; fetches tiles)')
    parser.add_argument('--terrain-cache', default=None,
                        help='shared tile cache (default pipelines/cache/terrain)')
    parser.add_argument('--verbose', action='store_true')
    return parser.parse_args(argv)


def _connect(args):
    if not args.dsn:
        raise CorpusUnavailable('no DSN: pass --dsn or set WS13_DB_DSN')
    try:
        import psycopg
    except ImportError as exc:
        raise CorpusUnavailable('psycopg is not importable: %s' % exc)
    return psycopg.connect(args.dsn, autocommit=False)


def run(argv=None, resolver=None, answerer=None, corpus_factory=None, log=None,
        report=None):
    """The program.  ``resolver``/``answerer``/``corpus_factory`` are the
    test seams and ``report`` (a list) receives the sweep summary; main()
    passes none of them.  Returns the exit code."""
    args = parse_args(argv)
    out = log or (lambda msg: log_line(msg, args.shard, args.shards))
    if IMPORT_ERROR is not None:
        out('ABORT: %s' % IMPORT_ERROR)
        return EXIT_ENVIRONMENT
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        out('ABORT: shard %d is not in 0..%d' % (args.shard, args.shards - 1))
        return EXIT_BAD_SHARD
    if args.rate <= 0:
        out('ABORT: --rate must be positive, got %s' % args.rate)
        return EXIT_BAD_SHARD

    if args.migrate or args.check:
        try:
            conn = _connect(args)
        except CorpusUnavailable as exc:
            out('ABORT: %s' % exc)
            return EXIT_ENVIRONMENT
        try:
            if args.migrate:
                report = migrate(conn, args.sql, echo=args.verbose)
                conn.commit()
                out('applied %d statement(s) from %s' % (len(report), args.sql))
            if args.check:
                conn.rollback()
                return EXIT_SHARD_DONE if print_checks(run_checks(conn), out) else 1
            return EXIT_SHARD_DONE
        finally:
            conn.close()

    if args.index:
        merge_indexes(args.site_dir, out)
        return EXIT_SHARD_DONE

    state_dir = args.state_dir or os.path.join(ROOT or HERE, 'var', 'geomodel', 'ws13-state')
    try:
        if corpus_factory is not None:
            corp = corpus_factory(args, state_dir)
        elif args.fixture:
            corp = FixtureCorpus(args.fixture, state_dir)
        else:
            corp = PostgresCorpus(args.dsn, args.bucket)
    except CorpusUnavailable as exc:
        out('ABORT: %s' % exc)
        return EXIT_ENVIRONMENT

    try:
        if args.verify_complete:
            report = verify_complete(corp, args.shards)
            print_verify(report, out)
            return EXIT_INCOMPLETE if report['documents_remaining'] else EXIT_SHARD_DONE

        if not (args.plan or args.dry_run):
            cache = args.terrain_cache or DEFAULT_TERRAIN_CACHE
            if args.offline and not os.path.isdir(cache):
                out('ABORT: --offline with no terrain cache at %s: nothing can be placed' % cache)
                return EXIT_ENVIRONMENT
            try:
                os.makedirs(cache, exist_ok=True)
            except OSError as exc:
                out('ABORT: terrain cache %s is not writable: %s' % (cache, exc))
                return EXIT_ENVIRONMENT
            if args.terrain_cache:
                import leapfrog_export
                leapfrog_export.TERRAIN_CACHE = os.path.abspath(cache)

        if args.publish == 's3':
            try:
                target = publish.target_from_env(args.models_bucket)
            except publish.PublishError as exc:
                out('ABORT: %s' % exc)
                return EXIT_ENVIRONMENT
        else:
            target = publish.LocalTarget(args.site_dir)
        os.makedirs(state_dir, exist_ok=True)
        ctx = autop.make_context(state_dir, target, args.base_url, args.zoom, args.offline,
                                 out if args.verbose else (lambda *a: None))
        if resolver is None:
            resolver = load_resolver(ROOT, out)
        summary = run_shard(corp, args.shard, args.shards, ctx, resolver, answerer, args, out,
                            args.site_dir, args.results_dir)
        if report is not None:
            report.append(summary)
        print_summary(summary, args, out)
        if args.plan:
            shas = [d['sha256'] for d in corp.documents(0, 1)
                    if (d.get('mine_ids') or d.get('mine_names'))]
            hits = corp.candidates(0, 1)
            print_capacity(capacity([s for s in shas if hits.get(s)], args.rate), out)
        return exit_code_for(summary, args)
    finally:
        corp.close()


def main(argv=None):
    return run(argv)


if __name__ == '__main__':
    raise SystemExit(main())
