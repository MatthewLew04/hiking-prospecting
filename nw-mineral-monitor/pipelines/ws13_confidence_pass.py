#!/usr/bin/env python3
"""WS13 phase 1: measure OCR confidence on the text the corpus already has.

The concrete defect: ws13_worker.page_confidences() shelled
`docker run --entrypoint pdftoppm <ocrmypdf image>` and pdftoppm is not on
PATH inside that image, so every page render exited rc=127 and every
measurement came back empty. All 760,059 rows of ws13_pages carry
confidence IS NULL today, which means the tier-1 escalation in
ws13_worker.process() -- gated on CONF_THRESHOLD 60 -- has never fired once
over the whole corpus. The 108,260 rows carrying low_confidence=true were
written by pipelines/ws13_quality_proxy.py, a lexical proxy on its own
0-100 scale, not by tesseract. ws13_worker.py has since been fixed to probe
pdftoppm / pdftocairo / gs and to record an unmeasured page as NULL rather
than false; this pass imports that fix rather than carrying a second copy of
it.

Scope: the 28,988 ocr_queue documents / 323,059 pages, every one of which
has a searchable_key. The 27,294 born_digital documents (437,000 pages) have
no rasterised page to measure and no searchable_key, and are excluded by the
work predicate rather than by a comment.

Measuring is not re-OCRing, and this program only measures
----------------------------------------------------------
Writing ws13_pages.confidence / low_confidence touches nothing a query can
read: it is idempotent, resumable, and cannot change retrieval. Replacing
OCR text would change ws13_chunks.text, which forces re-chunking, which
forces re-embedding through Titan, which invalidates
ws13_chunks_titan_hnsw for those rows -- the whole 852,027-chunk pipeline
again. So the two operations are two programs. This one measures all
323,059 pages; the re-OCR pass that follows takes only the pages that
measure weak, and the size of that set is unknowable until this pass has
run, so nothing here assumes anything about it.

Why tesseract, and not a "better" engine
----------------------------------------
The number being produced IS a tesseract word confidence. The thresholds it
feeds already exist and are calibrated against that scale
(ws13_worker.CONF_THRESHOLD 60, ws13_worker.ESCALATE_THRESHOLD 45), and both
are imported here rather than restated. A different engine would produce a
differently-scaled score that those thresholds do not mean anything against,
and -- because a stronger engine also emits its own text -- adopting it
would replace the OCR text and trigger the re-chunk/re-embed cascade this
phase exists to avoid. Matching the measurement engine to the thresholds is
the point of the design, not a limitation of it. No GPU engine is a
candidate for a second reason: no GPU has ever run in this account.

Sharding arithmetic
-------------------
One process owns one shard, selected by WS13_SHARD / WS13_SHARD_COUNT, and
shards are disjoint BY CONSTRUCTION on mod(hash(sha256), shards) -- not by
row locking. That is deliberate: ws13_embed_backfill.py claimed work with
`FOR UPDATE SKIP LOCKED` on an autocommit connection, where each statement
is its own transaction and the locks were released before the work started,
and 12 threads then duplicated each other at a measured 6.15x. A modulo
partition cannot do that whatever the isolation level is.

The unit of parallelism is one DOCUMENT (a shard fetches one searchable PDF
and renders pages out of it), so a shard owns whole documents and the page
split is only approximately even:

    shards   pages/shard (mean)   documents/shard (mean)
        16              20,191                    1,812   (1 c7g.4xlarge)
       128               2,524                      226   (8 nodes)
       640                 505                       45   (40 nodes, the
                                                           640 vCPU quota)

At 640 shards the mean shard holds 505 pages but the shard that owns the
1,407-page document holds at least that document whole, so the wall clock is
set by the worst shard and not by the mean. --plan reports both, computed
from the live remaining counts, so the operator sizes the fleet on the
number that actually binds instead of on the average.

Resumable, bounded, observable
------------------------------
* Resumable for free: the work set is "ocr_queue pages whose confidence IS
  NULL", so an interrupted run resumes exactly where it stopped and a
  measured page is never rendered twice.
* A page that cannot be measured is recorded in ws13_conf_skips
  (sha256, page, reason, attempts) so it is not retried forever.
  TERMINAL_REASONS -- the renderer produced no image, the page is not in the
  PDF, the searchable PDF is gone, the page has no scored word at all -- are
  excluded from the work set permanently. Everything else (an S3 timeout, a
  container timeout) is transient and is re-admitted on the next run, because
  a five-second network fault must not abandon a page for good -- but only
  MAX_TRANSIENT_ATTEMPTS times. A page that fails transiently on every sweep
  is deterministic in fact whatever it is in classification, and without that
  counter the shard never reached zero remaining, main() never returned
  "done", and the fleet wrapper swept the same unmeasurable pages for its
  full 24 h ceiling. An exhausted page is reported as its own number in the
  summary and by --verify-complete; it is never counted as measured. One
  attempt is charged per page per SWEEP, not per recorded row: run_shard()
  re-reaches a page several times within one sweep, and charging each of
  those spent the whole budget before the sweep returned.
* Exit codes are a contract, not a boolean. 0 the shard is measured, 10 pages
  remain and this sweep measured some, 11 pages remain and this sweep
  measured NOTHING (so a caller should back off, not sweep again), 2 bad
  shard arithmetic, 3 no docker or no renderer, 12 --verify-complete found
  the corpus unfinished. 1 is deliberately absent: it is what CPython returns
  for an uncaught exception, and the old "1 means work remains" made a dead
  process and an ordinary first sweep the same observable.
* --verify-complete answers "is the whole pass actually done", per shard,
  from ws13_pages rather than from any fleet bookkeeping -- a shard whose
  node died before claiming a slot leaves nothing behind in S3 but leaves
  every page it never measured in the database.
* Bounded: DOC_SECONDS per document and RENDER_SECONDS / TESSERACT_SECONDS
  per page. A document that exceeds its deadline is left PARTIALLY measured
  and no skip row is written for the pages it did not reach: those pages
  still match `confidence IS NULL`, so the next sweep resumes them. The
  1,407-page document therefore takes several passes and can never pin a
  shard for more than one deadline.
* Observable: a heartbeat to s3://$WS13_BUCKET/ws13/confidence/status.json
  (per-shard key when sharded) carrying pages remaining, pages/second and
  the implied finish time.

Write set -- this is the whole of it
------------------------------------
UPDATE ws13_pages SET confidence, low_confidence   (store_confidence)
INSERT INTO ws13_conf_skips                        (record_skip)
CREATE TABLE IF NOT EXISTS ws13_conf_skips         (ensure_skips_table)
ALTER TABLE ws13_conf_skips ADD COLUMN attempts    (ensure_skips_table)

Nothing here writes ws13_chunks, ws13_documents, ws13_manifest, or any
embedding column, and tests/test_ws13_confidence_pass.py asserts that over
every statement a real run issues.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import statistics
import subprocess
import types
import sys
import shlex
import tempfile
import threading
import time

import boto3
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# ws13_worker.py is an SQS consumer and reads WS13_QUEUE_URL at module scope.
# A confidence node polls no queue, so the variable is defaulted to a value
# that is never used: this module never constructs a DocumentLease and never
# touches sqs. WS13_BUCKET and WS13_DB_DSN are deliberately NOT defaulted --
# this pass genuinely needs both, and a missing one has to fail at import
# rather than halfway through a shard.
os.environ.setdefault('WS13_QUEUE_URL', 'unused:ws13_confidence_pass')
import ws13_worker as worker                                    # noqa: E402

REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')
DSN = os.environ['WS13_DB_DSN']
BUCKET = os.environ['WS13_BUCKET']
SCRATCH = os.environ.get('WS13_CONF_SCRATCH', worker.SCRATCH)

# Imported, not restated: the whole argument for measuring with tesseract is
# that these two numbers already mean something on the scale being produced.
CONF_THRESHOLD = worker.CONF_THRESHOLD            # 60.0, re-OCR candidate
ESCALATE_THRESHOLD = worker.ESCALATE_THRESHOLD    # 45.0, low_confidence=true

SHARD = int(os.environ.get('WS13_SHARD', '0'))
SHARD_COUNT = int(os.environ.get('WS13_SHARD_COUNT', '1'))

# The corpus as measured on 2026-08-25, used only as the denominator --plan
# reports progress against. Every number this program acts on is read live
# from the database; nothing here is an input to the work set.
PAGES_TOTAL = 323_059
DOCS_TOTAL = 28_988
NODE_TYPE = 'c7g.4xlarge'
NODE_VCPU = 16
# 640 vCPU On-Demand Standard quota in us-west-2 / 16 vCPU per node.
MAX_NODES = 40
PLAN_NODES = (1, 8, MAX_NODES)
# Two rates, because the two paths cost very different things. Per page the
# real work -- a 150 dpi raster plus a tesseract TSV -- is around 1.2 s, but
# `docker run` against the ocrmypdf image costs 1.5-3 s of container startup.
# ws13_worker's per-page shape pays that twice a page, which is the 5-8 s it
# measures; the batched path pays it once a DOCUMENT, so startup amortises
# over the 11.1 pages an average document has and the rate approaches the
# work itself. BOTH are SEEDS, and nothing in this program replaces them on
# its own: --plan projects from --rate, which defaults to the seed. To size a
# fleet on a measured number, run one shard against a --limit of a few dozen
# documents, read pages_per_second out of the heartbeat it writes, and pass
# that back in as --rate. A fleet sized off the seed is sized off an
# estimate, and the estimate is wrong by ~4x between the two container
# shapes. The heartbeat key is whatever status_key() returns and there is no
# third form: ws13/confidence/status.json for an unsharded run -- which the
# sizing run above IS, since --shards defaults to 1 -- and
# ws13/confidence/status-<shard:04d>-of-<shards:04d>.json once sharded.
# Written out here because 'status-<shard>.json' is neither of them, and an
# operator following that gets NoSuchKey for the one number this whole
# sizing argument rests on.
PER_PAGE_SECONDS = float(os.environ.get('WS13_CONF_SECONDS_PER_PAGE', '6.5'))
BATCHED_SECONDS = float(os.environ.get('WS13_CONF_BATCH_SECONDS', '1.6'))
SECONDS_PER_PAGE = (BATCHED_SECONDS
                    if os.environ.get('WS13_CONF_BATCH', 'true').lower() == 'true'
                    else PER_PAGE_SECONDS)
DEFAULT_RATE = 1.0 / SECONDS_PER_PAGE

# Per-document wall-clock deadline. At the measured 5-8 s/page this covers
# 450-720 pages, so the 1,407-page document is finished over 2-4 sweeps and
# no single document can hold a shard for longer than an hour. Exceeding it
# is not an error and writes no skip row -- see process_document().
DOC_SECONDS = int(os.environ.get('WS13_CONF_DOC_SECONDS', '3600'))
# Floor under a clamped container timeout: a timeout shorter than this kills
# healthy work rather than stuck work, so a document may overrun its
# deadline by at most one page's two floors (60 s).
MIN_TIMEOUT = 30
DOC_BATCH = 200
HEARTBEAT_SECONDS = int(os.environ.get('WS13_CONF_HEARTBEAT', '300'))
# How often a heartbeat may re-COUNT the shard's remaining pages; see
# heartbeat() for the 640-shard arithmetic that sets it.
REMAINING_SECONDS = int(os.environ.get('WS13_CONF_REMAINING', '1800'))
PDF_NAME = 'doc.pdf'

# Exit codes, and the reason 1 is not among them.
#
# main() used to return 1 for "this shard still has pages", which is the most
# ordinary outcome a first sweep has -- and 1 is also what CPython returns for
# any uncaught exception. The fleet wrapper could not tell the two apart, so a
# process that died on an unhandled error was swept again immediately, with no
# backoff, until the 24 h ceiling. Distinct codes make the ordinary case
# distinguishable from the broken one, and leave 1 to mean what the
# interpreter already makes it mean.
EXIT_SHARD_DONE = 0        # this shard has 0 pages left
EXIT_BAD_SHARD = 2         # --shard/--shards arithmetic is not a partition
EXIT_NO_RENDERER = 3       # no docker, or no renderer in the OCR image
EXIT_WORK_REMAINS = 10     # pages left AND this sweep measured some: sweep on
EXIT_NO_PROGRESS = 11      # pages left and this sweep measured NOTHING
EXIT_INCOMPLETE = 12       # --verify-complete: the corpus is not measured

# Reasons that permanently disqualify a page. A container or network timeout
# is NOT one of them: those rows are re-admitted by the next run, the same
# distinction ws13_embed_backfill.TERMINAL_REASONS draws for chunks.
TERMINAL_REASONS = ('no_image', 'no_words', 'page_absent',
                    'searchable_missing')
# ...but "transient" cannot mean "forever". A transient reason is re-admitted
# on the next run, which is right for a five-second network fault and wrong
# for a page that fails the same way every sweep: a render that always times
# out, or a tesseract that always dies on the same raster, is transient by
# CLASSIFICATION and deterministic in FACT. Those pages kept `confidence IS
# NULL`, so the shard never reached zero remaining, so main() never returned
# 0, so the fleet wrapper swept again -- with no backoff, for the full 24 h
# ceiling -- over pages that could not be measured on any of them.
#
# So a transient skip counts. After MAX_TRANSIENT_ATTEMPTS sweeps have each
# recorded a transient reason for the same page, that page leaves the work
# set exactly the way a terminal one does. What it does NOT do is masquerade
# as measured: the row keeps the last reason and the attempt count, the run
# summary and --verify-complete report exhausted pages as their own number,
# and nothing writes ws13_pages.confidence. Giving up is allowed to be a
# conclusion; it is not allowed to be silent.
#
# SWEEPS, not rows -- and the difference is not pedantry. run_shard() rewinds
# whenever a pass made progress, so one sweep reaches the same unmeasurable
# page repeatedly, and a counter charged per recorded row burned all five
# attempts inside a single sweep. See _counted_this_sweep, which is what
# holds the unit to a sweep.
MAX_TRANSIENT_ATTEMPTS = int(os.environ.get('WS13_CONF_MAX_ATTEMPTS', '5'))
# docker's OWN exit codes: 125 the daemon failed, 126 the command could not
# be invoked, 127 the command was not found. They describe this node and
# this image, never the page, and treating one of them as a property of the
# page is exactly the mistake that let rc=127 read as a clean measurement
# across 760,059 rows. They are transient, and they are logged.
DOCKER_ENV_CODES = (125, 126, 127)
_logged_docker_env = False

# The 8 hex digits of the sha256 that decide the shard. sha256 is uniformly
# distributed by construction, so no re-hashing is needed and the value is
# stable across runs, processes and fleet sizes. shard_of() below is the SAME
# arithmetic in Python -- int(sha[:8], 16) % shards -- so --plan, the tests
# and the running shard cannot disagree about who owns a document. bit(32)
# zero-extends into bigint, so the modulus operand is never negative.
SHARD_EXPR = "('x' || substr(p.sha256, 1, 8))::bit(32)::bigint"
SHARD_SQL = f'AND mod({SHARD_EXPR}, %s) = %s '

s3 = boto3.client('s3', region_name=REGION)
lock = threading.Lock()
stats = {'documents': 0, 'pages_measured': 0, 'terminal_skips': 0,
         'transient_skips': 0, 'exhausted_skips': 0, 'deadline_documents': 0,
         'below_conf_threshold': 0, 'below_escalate_threshold': 0}
# How this sweep has already classified each page it could not measure, and
# the reason MAX_TRANSIENT_ATTEMPTS means what it says.
#
# A sweep is not one pass over the shard. run_shard() rewinds whenever a pass
# made progress, because a document cut short by DOC_SECONDS leaves pages
# behind the cursor -- the 1,407-page document needs several. So one sweep
# reaches the same unmeasurable page several times, and an attempt counter
# that charged every recorded row spent the whole five-attempt budget inside
# a single sweep: two charges in the ordinary two-document case, all five
# where a deadline-cut document keeps supplying the progress that triggers
# the rewind. A page whose S3 fetch was throttled for a few minutes was then
# retired permanently with confidence IS NULL, though nothing about it was
# unmeasurable -- the precise failure the counter was added to prevent,
# inverted.
#
# So a page is charged at most ONE attempt per sweep, and this is what
# remembers that it has been charged. Process-local because a sweep IS a
# process: infra/ws13_fleet.yaml runs `python3 ws13_confidence_pass.py` per
# sweep. Two processes sharing a shard across a reclaim each charge once,
# which is right -- those are two sweeps that both happened.
#
# It holds the stats bucket rather than a bare marker so the summary counts
# PAGES and not rows, and so a page that fails transiently and then
# terminally inside one sweep moves between buckets instead of being counted
# in both. None means claimed-but-not-yet-classified.
_counted_this_sweep = {}


def begin_sweep():
    """Start a sweep: no page carries a charge in from the previous one.

    run_shard() calls this, because run_shard() IS the sweep -- that is the
    scope MAX_TRANSIENT_ATTEMPTS is denominated in, and putting the reset
    anywhere else would let a caller do two sweeps that counted as one. It
    does NOT reset `stats`: those accumulate over whatever the caller chose
    to run, and main() reports them once at the end of its single sweep.
    """
    with lock:
        _counted_this_sweep.clear()


def log(msg):
    print(f'{dt.datetime.now(dt.timezone.utc).isoformat()} '
          f'[shard {SHARD}/{SHARD_COUNT}] {msg}', flush=True)


def shard_of(sha, shards):
    """Which shard owns this document. Mirrors SHARD_EXPR exactly."""
    if shards <= 1:
        return 0
    return int(sha[:8], 16) % shards


def mean_word_confidence(tsv_text):
    """Mean tesseract word confidence from a `tesseract ... stdout tsv` dump.

    The parse is ws13_worker.page_confidences()'s, expression for
    expression, because the two numbers have to be comparable: column 10 is
    `conf`, tesseract writes -1 on the structural rows (page/block/para/
    line), writes empty for a word it declined to score, and a row with
    fewer than 12 fields is a truncated line rather than a word. A conf of 0
    is kept: 0 is a real and terrible score, and dropping it would bias
    every page upward.

    None -- never 0.0 -- for a page with no scored word at all. 0.0 would
    read as "measured, and as bad as a page can be" and would sort a blank
    page ahead of pages that genuinely are unreadable in the phase-2 queue.
    The caller records that page as a terminal skip, which says "there is
    nothing here to measure" instead of inventing a number.
    """
    words = [float(p[10]) for p in
             (line.split('\t') for line in (tsv_text or '').splitlines()[1:])
             if len(p) > 11 and p[10] not in ('-1', '')]
    if not words:
        return None
    return round(statistics.mean(words), 1)


def skips_clause(alias):
    """Exclude the pages this pass has given up on, by reason or by count.

    Two ways out of the work set and no third: a terminal reason, which says
    there is nothing here to measure, or MAX_TRANSIENT_ATTEMPTS sweeps that
    each failed transiently, which says this page does not improve by being
    tried again. Both are inlined constants rather than parameters because
    this fragment is concatenated into four different statements.
    """
    reasons = ', '.join(f"'{r}'" for r in TERMINAL_REASONS)
    return (f'AND NOT EXISTS (SELECT 1 FROM ws13_conf_skips s '
            f'WHERE s.sha256 = {alias}.sha256 AND s.page = {alias}.page '
            f'AND (s.reason IN ({reasons}) '
            f'OR s.attempts >= {MAX_TRANSIENT_ATTEMPTS})) ')


def exhausted_clause(alias):
    """The complement: pages excluded for having run out of attempts.

    Their own count, never folded into "remaining 0". A run that ends with
    pages here has not measured the corpus; it has stopped trying, and the
    difference has to survive into the summary an operator reads.
    """
    reasons = ', '.join(f"'{r}'" for r in TERMINAL_REASONS)
    return (f'AND EXISTS (SELECT 1 FROM ws13_conf_skips s '
            f'WHERE s.sha256 = {alias}.sha256 AND s.page = {alias}.page '
            f'AND s.reason NOT IN ({reasons}) '
            f'AND s.attempts >= {MAX_TRANSIENT_ATTEMPTS}) ')


def ensure_skips_table(conn):
    """Bookkeeping only, and bounded by the corpus: one row per page that
    could not be measured, at most 323,059. Read by this pass and by an
    operator; no query path joins it.

    The ALTER is for a table created by the version of this pass that had no
    attempt counter. It is a no-op on a fresh table and on a second run, and
    it is here rather than in ws13_migrations.sql because this table is this
    program's own bookkeeping and no query path joins it.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ws13_conf_skips (
             sha256 TEXT NOT NULL, page INT NOT NULL, reason TEXT,
             noted_at TIMESTAMPTZ,
             attempts INT NOT NULL DEFAULT 0,
             PRIMARY KEY (sha256, page))""")
    conn.execute('ALTER TABLE ws13_conf_skips '
                 'ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0')


def pending_documents(conn, shard, shards, cursor, limit=DOC_BATCH):
    """Documents in this shard with at least one unmeasured page.

    Walks forward on a sha256 cursor rather than re-running the same
    `ORDER BY ... LIMIT n` from the head of the table every batch, which
    makes each pass more expensive than the last as the measured prefix
    grows. `d.searchable_key IS NOT NULL` is what confines the pass to the
    ocr_queue population that actually has a PDF to render.
    """
    sql = ('SELECT DISTINCT d.sha256, d.searchable_key, d.pages '
           'FROM ws13_pages p '
           'JOIN ws13_documents d ON d.sha256 = p.sha256 '
           "WHERE p.confidence IS NULL AND d.doc_class = 'ocr_queue' "
           'AND d.searchable_key IS NOT NULL AND d.sha256 > %s '
           + skips_clause('p'))
    tail = 'ORDER BY d.sha256 LIMIT %s'
    if shards > 1:
        return conn.execute(sql + SHARD_SQL + tail,
                            (cursor, shards, shard, limit)).fetchall()
    return conn.execute(sql + tail, (cursor, limit)).fetchall()


def pending_pages(conn, sha):
    """The unmeasured page numbers of one document, in page order."""
    return [row[0] for row in conn.execute(
        'SELECT p.page FROM ws13_pages p '
        'WHERE p.sha256 = %s AND p.confidence IS NULL '
        + skips_clause('p') + 'ORDER BY p.page', (sha,)).fetchall()]


def remaining(conn, shard, shards):
    """(pages, documents) still owed a measurement in this shard."""
    sql = ('SELECT COUNT(*), COUNT(DISTINCT p.sha256) FROM ws13_pages p '
           'JOIN ws13_documents d ON d.sha256 = p.sha256 '
           "WHERE p.confidence IS NULL AND d.doc_class = 'ocr_queue' "
           'AND d.searchable_key IS NOT NULL ' + skips_clause('p'))
    if shards > 1:
        return conn.execute(sql + SHARD_SQL, (shards, shard)).fetchone()
    return conn.execute(sql).fetchone()


def exhausted(conn, shard, shards):
    """(pages, documents) this shard has stopped retrying but never measured.

    The number that keeps "0 pages remaining" honest. remaining() counts what
    is still owed a measurement; this counts what was quietly removed from
    that debt, and a run that reports the first as zero while the second is
    not zero has stopped, not finished.
    """
    sql = ('SELECT COUNT(*), COUNT(DISTINCT p.sha256) FROM ws13_pages p '
           'JOIN ws13_documents d ON d.sha256 = p.sha256 '
           "WHERE p.confidence IS NULL AND d.doc_class = 'ocr_queue' "
           'AND d.searchable_key IS NOT NULL ' + exhausted_clause('p'))
    if shards > 1:
        return conn.execute(sql + SHARD_SQL, (shards, shard)).fetchone()
    return conn.execute(sql).fetchone()


def pending_by_document(conn):
    """[(sha256, unmeasured_pages)] over the whole corpus, for planning.

    One GROUP BY returning at most 28,988 rows, instead of one aggregate per
    candidate fleet size. The per-shard totals are then computed in Python
    with shard_of(), which is the same arithmetic SHARD_EXPR performs in the
    database, so --plan and a running shard cannot disagree.
    """
    return conn.execute(
        'SELECT p.sha256, COUNT(*) FROM ws13_pages p '
        'JOIN ws13_documents d ON d.sha256 = p.sha256 '
        "WHERE p.confidence IS NULL AND d.doc_class = 'ocr_queue' "
        'AND d.searchable_key IS NOT NULL ' + skips_clause('p') +
        'GROUP BY p.sha256').fetchall()


def record_skip(conn, sha, page, reason):
    """Record why a page could not be measured, and count the attempt.

    Idempotent per page: a transient reason is overwritten by whatever the
    next attempt finds, so a page that later succeeds leaves a stale row that
    the work predicate no longer reaches (confidence IS NOT NULL) and that
    costs nothing.

    `attempts` is what stops a deterministically-unmeasurable page from being
    re-admitted for ever. It counts SWEEPS, not rows: run_shard() rewinds and
    re-reaches the same page several times inside one sweep, so charging
    every recorded row spent the whole budget in one or two sweeps and
    retired pages that a later sweep would have measured. _counted_this_sweep
    is what makes the unit a sweep; the increment itself is still done in SQL
    rather than read-modify-written here, because 64 processes on one node
    share this table and two of them can hold the same document across a
    reclaimed shard.
    """
    key = (sha, page)
    with lock:
        # Claimed before the statement runs, so two threads that reach one
        # page cannot both charge it. None means "claimed, not yet
        # classified" -- the bucket is only known after the RETURNING.
        charge = 1 if key not in _counted_this_sweep else 0
        if charge:
            _counted_this_sweep[key] = None
    row = conn.execute(
        """INSERT INTO ws13_conf_skips (sha256, page, reason, noted_at,
                                        attempts)
           VALUES (%s, %s, %s, now(), %s)
           ON CONFLICT (sha256, page)
           DO UPDATE SET reason = EXCLUDED.reason,
                         noted_at = EXCLUDED.noted_at,
                         attempts = ws13_conf_skips.attempts + %s
           RETURNING attempts""",
        (sha, page, reason, charge, charge)).fetchone()
    attempts = row[0] if row else charge
    if reason in TERMINAL_REASONS:
        bucket = 'terminal_skips'
    elif attempts >= MAX_TRANSIENT_ATTEMPTS:
        # Still not measured and no longer retried. Counted apart from both,
        # because it is neither "nothing to measure here" nor "we will get it
        # next time".
        bucket = 'exhausted_skips'
    else:
        bucket = 'transient_skips'
    with lock:
        previous = _counted_this_sweep.get(key)
        if previous != bucket:
            # A page that failed transiently and then terminally in the same
            # sweep is one page, in the second bucket -- not one in each.
            if previous is not None:
                stats[previous] -= 1
            stats[bucket] += 1
            _counted_this_sweep[key] = bucket
    return attempts


def store_confidence(conn, sha, page, conf):
    """The only column write this program performs.

    Two columns of one table, and no other table anywhere in this module:
    ws13_chunks.text is untouched, so no chunk changes, so no embedding is
    invalidated and ws13_chunks_titan_hnsw stays valid. `confidence IS NULL`
    in the predicate makes the write idempotent and makes a re-run a no-op
    rather than a second measurement.

    low_confidence uses ESCALATE_THRESHOLD, exactly as ws13_worker does when
    it inserts a page, so a row written here and a row written by the worker
    mean the same thing. It overwrites the lexical proxy's verdict for this
    page, which is intended -- that verdict was a stand-in for this
    measurement -- while quality_score / quality_method are deliberately
    left alone so the proxy's answer stays auditable beside the real one.
    """
    return conn.execute(
        'UPDATE ws13_pages SET confidence = %s::real, '
        'low_confidence = (%s::real < %s) '
        'WHERE sha256 = %s AND page = %s AND confidence IS NULL',
        (conf, conf, ESCALATE_THRESHOLD, sha, page)).rowcount


def clamp(cap, budget):
    """A container timeout that can never outlive the document's deadline."""
    return max(MIN_TIMEOUT, int(min(cap, max(0.0, budget))))


def fetch_searchable(work, key):
    """Download the searchable PDF into `work`. -> (name, None) | (None, why).

    Streamed to disk rather than read into memory: a c7g.4xlarge runs 16 of
    these processes against 32 GiB, and the searchable PDF of a several
    hundred page scan is a few hundred MB, so 16 concurrent
    get_object().read() calls would spend gigabytes on files that only ever
    get handed to a container by path anyway.

    A missing object is terminal (the pages behind it can never be measured
    from this key), anything else -- a timeout, a throttle, a torn
    connection -- is transient and gets another run.
    """
    path = os.path.join(work, PDF_NAME)
    try:
        s3.download_file(Bucket=BUCKET, Key=key, Filename=path)
    except Exception as exc:
        name = type(exc).__name__
        code = ''
        response = getattr(exc, 'response', None)
        if isinstance(response, dict):
            code = str(response.get('Error', {}).get('Code', ''))
        if name in ('NoSuchKey', 'NoSuchBucket') or code in ('NoSuchKey',
                                                             'NoSuchBucket',
                                                             '404'):
            return None, 'searchable_missing'
        # A partial download dies with the document's temp directory, so
        # there is nothing to clean up here.
        return None, f's3_error:{name}'
    # The OCR image runs as 0:0 against a bind-mounted /work; the worker
    # does the same for the same reason.
    os.chmod(path, 0o644)
    return PDF_NAME, None


def docker_env_skip(renderer, render):
    """A docker-level failure: transient, and said out loud exactly once.

    Once per process rather than once per page, because a node whose daemon
    is broken would otherwise write one log line per page for 323,059 pages;
    and never zero times, because the whole reason this pass exists is that
    an rc=127 nobody saw looked like a measurement.
    """
    global _logged_docker_env
    if not _logged_docker_env:
        _logged_docker_env = True
        log(f'DOCKER FAILURE rc={render.returncode} running {renderer} in '
            f'{worker.OCR_IMAGE}: {render.stderr[-300:]}. These pages are '
            f'recorded as unmeasured, not as clean, and are retried on a '
            f'later run.')
    return f'docker_rc_{render.returncode}'


def measure_page(work, pdf_name, page, renderer, render_timeout,
                 tess_timeout):
    """Render one page and score it. -> (confidence, None) | (None, reason).

    render_argv() and the renderer probe come from ws13_worker: the image
    has whichever of pdftoppm / pdftocairo / gs it has, that answer is a
    property of the image rather than of this pass, and a second copy of the
    probe here is exactly how the two would drift. worker._try_docker
    returns None on a timeout instead of raising, so one stuck page fails
    one page.
    """
    base = f'pg{page:05d}'
    render = worker._try_docker(
        worker.render_argv(renderer, pdf_name, base, page), work,
        render_timeout, renderer)
    pngs = sorted(name for name in os.listdir(work)
                  if name.startswith(base) and name.endswith('.png'))
    if not pngs:
        if render is None:
            return None, 'render_timeout'
        if render.returncode in DOCKER_ENV_CODES:
            return None, docker_env_skip(renderer, render)
        # Terminal. The renderer ran to completion and produced no image for
        # this page number: the page is not in the PDF, or the PDF is
        # damaged where it is. Neither improves by trying again.
        return None, 'no_image'
    try:
        tsv = worker._try_docker([f'/work/{pngs[0]}', 'stdout', 'tsv'], work,
                                 tess_timeout, 'tesseract')
        if tsv is None:
            return None, 'tesseract_timeout'
        if tsv.returncode != 0:
            return None, f'tesseract_rc_{tsv.returncode}'
        conf = mean_word_confidence(tsv.stdout)
        if conf is None:
            # No scored word on a page that rendered fine: a blank scan.
            # Terminal, and recorded as such rather than stored as 0.0 --
            # 3,091 documents in this corpus have zero extracted characters
            # and every one of them is ocr_queue.
            return None, 'no_words'
        return conf, None
    finally:
        for name in pngs:
            os.unlink(os.path.join(work, name))


BATCH_MARK = '##WS13-MARK'
# One container per DOCUMENT instead of two per PAGE. The measurement itself
# is cheap -- a 150 dpi raster plus a tesseract TSV is around 1.2 s -- but
# `docker run` against the ocrmypdf image costs 1.5-3 s of container startup,
# and the per-page shape paid that twice for every one of 323,059 pages:
# ~646,000 launches, where startup rather than OCR is the dominant term.
# Batching the same work into one `sh -c` per document makes it ~28,988
# launches, a 22x reduction, without giving up per-page granularity: each
# page still reports its own render rc, its own tesseract rc and its own
# confidence, so every terminal reason this module distinguishes survives.
# A container that dies mid-document simply leaves the pages it never reached
# at confidence IS NULL, which is the same convergence the deadline path uses.
BATCH_PAGES = os.environ.get('WS13_CONF_BATCH', 'true').lower() == 'true'
# coreutils `timeout` exit status for a command it killed.
TIMEOUT_CODE = 124


def batch_script(renderer, pdf_name, pages):
    """Shell that renders and scores every page of one document in order.

    Emitted as a marker-delimited stream rather than one file per page so the
    parser can attribute a failure to the exact page that caused it.
    """
    render_cap = int(max(MIN_TIMEOUT, worker.RENDER_SECONDS))
    tess_cap = int(max(MIN_TIMEOUT, worker.TESSERACT_SECONDS))
    lines = ['set -u']
    for page in pages:
        base = f'pg{page:05d}'
        argv = worker.render_argv(renderer, pdf_name, base, page)
        rendered = ' '.join(shlex.quote(token) for token in argv)
        # Each stage carries its OWN timeout. Without one the container's
        # single deadline is the document's whole remaining budget, so one
        # hung raster consumes it, every later page of that document goes
        # unreached, and because pages are always handed out in ascending
        # order the same page stalls the same way on every future sweep --
        # a document permanently stuck behind page N. coreutils `timeout`
        # reports 124, which TIMEOUT_CODE maps back to a transient per-page
        # reason attributed to the page that actually stalled.
        lines += [
            f'timeout {render_cap} {shlex.quote(renderer)} {rendered} '
            f'>/dev/null 2>&1'
            f'; echo "{BATCH_MARK} {page} render $?"',
            f'img=$(ls /work/{base}*.png 2>/dev/null | head -1)',
            'if [ -n "$img" ]; then',
            f'  timeout {tess_cap} tesseract "$img" stdout tsv 2>/dev/null'
            f'; echo "{BATCH_MARK} {page} tess $?"',
            f'  rm -f /work/{base}*.png',
            'fi',
            f'echo "{BATCH_MARK} {page} end 0"',
        ]
    return '\n'.join(lines)


def parse_batch_output(text, pages, renderer):
    """Marker stream -> {page: (confidence, reason)}.

    A page missing from the output is absent from the result rather than
    recorded as anything: the container was cut off before reaching it, and
    an unmeasured page must stay NULL so a later sweep retries it. Recording
    a guess here is the failure this whole pass exists to end.
    """
    results = {}
    pending = {}
    tsv_lines = []
    for line in (text or '').splitlines():
        if not line.startswith(BATCH_MARK):
            tsv_lines.append(line)
            continue
        parts = line.split()
        if len(parts) != 4 or not parts[1].isdigit():
            tsv_lines.append(line)
            continue
        page, stage, code = int(parts[1]), parts[2], parts[3]
        code = int(code) if code.lstrip('-').isdigit() else 1
        if stage == 'render':
            pending = {'render': code}
            tsv_lines = []
        elif stage == 'tess':
            pending['tess'] = code
            pending['tsv'] = '\n'.join(tsv_lines)
            tsv_lines = []
        elif stage == 'end':
            results[page] = _batch_verdict(pending, renderer)
            pending, tsv_lines = {}, []
    return {page: results[page] for page in pages if page in results}


def _batch_verdict(pending, renderer):
    """One page's (confidence, reason) from its collected markers."""
    if 'tess' not in pending:
        # The renderer ran and produced no image for this page number.
        code = pending.get('render', 1)
        if code == TIMEOUT_CODE:
            # This page stalled, not the document. Transient: the next sweep
            # retries it, and the pages after it were still measured.
            return None, 'render_timeout'
        if code in DOCKER_ENV_CODES:
            # Describes the node and the image, never the page. rc=127 read
            # as a clean measurement across 760,059 rows is why this pass
            # exists, so it stays transient and it is logged.
            return None, f'docker_rc_{code}'
        return None, 'no_image'
    if pending['tess'] == TIMEOUT_CODE:
        return None, 'tesseract_timeout'
    if pending['tess'] in DOCKER_ENV_CODES:
        # 125/126/127 out of the tesseract stage says the same thing it says
        # out of the renderer: this image or this daemon, never this page.
        return None, docker_env_skip(renderer, types.SimpleNamespace(
            returncode=pending['tess'], stderr='tesseract stage'))
    if pending['tess'] != 0:
        return None, f"tesseract_rc_{pending['tess']}"
    conf = mean_word_confidence(pending.get('tsv', ''))
    if conf is None:
        return None, 'no_words'
    return conf, None


def run_batch(work, script, timeout):
    """One batch container. -> (stdout, returncode|None, timed_out).

    NOT worker._try_docker, which returns None on TimeoutExpired and discards
    the partial stdout along with it. Per page that cost one page. Per
    document it would throw away every page the container had already
    measured -- up to 1,406 of them for the largest document in this corpus --
    and then pay to measure them all again on the next sweep.
    """
    try:
        done = worker.docker(['-c', script], work, timeout=timeout,
                             entrypoint='sh')
        return done.stdout, done.returncode, False
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ''
        if isinstance(partial, bytes):
            partial = partial.decode('utf-8', 'replace')
        return partial, None, True


def measure_pages_batched(work, pdf_name, pages, renderer, timeout):
    """Measure many pages in one container. -> ({page: (conf, reason)}, ok).

    ok is False only when the container failed in a way that says something
    about this NODE rather than about the pages, so the caller can fall back
    to the per-page path and isolate one bad page. A timeout is not that: it
    is ordinary progress that ran out of budget, the pages it reached are
    kept, and the pages it did not stay NULL for the next sweep -- the same
    convergence the per-document deadline relies on.
    """
    script = batch_script(renderer, pdf_name, pages)
    stdout, code, timed_out = run_batch(work, script, timeout)
    measured = parse_batch_output(stdout, pages, renderer)
    if timed_out:
        return measured, True
    if not measured and code in DOCKER_ENV_CODES:
        docker_env_skip(renderer, types.SimpleNamespace(
            returncode=code, stderr=stdout[-300:]))
        return {}, False
    return measured, True


def process_document(conn, sha, searchable_key, doc_pages, pages, renderer,
                     deadline):
    """Measure `pages` of one document. -> (measured, outcome).

    Outcome is None when every requested page reached a terminal state, or
    'deadline' when the document's wall-clock budget ran out first. A
    deadline writes NO skip row for the pages it did not reach: they still
    match `confidence IS NULL`, so the next sweep picks them up and the
    document converges over as many sweeps as its page count needs. That is
    what stops the 1,407-page document from pinning a shard.
    """
    measured = 0
    with tempfile.TemporaryDirectory(dir=SCRATCH) as work:
        os.chmod(work, 0o777)
        name, reason = fetch_searchable(work, searchable_key)
        if reason:
            # Every pending page of the document shares the fetch's fate.
            # Recording all of them keeps the reason greppable per page and
            # keeps the table's meaning uniform; the transient case is
            # re-admitted whole on the next run.
            for page in pages:
                record_skip(conn, sha, page, reason)
            log(f'{sha[:12]} {reason} for {len(pages)} pages')
            return 0, reason
        # Pages past the stored page count cannot be rendered, so drop them
        # before the batch rather than paying a container to prove it.
        wanted = [page for page in pages if not (doc_pages and page > doc_pages)]
        for page in pages:
            if doc_pages and page > doc_pages:
                record_skip(conn, sha, page, 'page_absent')
        if BATCH_PAGES and wanted:
            budget = deadline - time.time()
            if budget > 0:
                # One container for the whole document. The timeout is the
                # document's remaining budget, so a batch can never outlive
                # the deadline that keeps the 1,407-page document from
                # pinning a shard.
                batched, ok = measure_pages_batched(
                    work, name, wanted, renderer, budget)
                for page in wanted:
                    if page not in batched:
                        continue
                    conf, reason = batched[page]
                    if conf is None:
                        record_skip(conn, sha, page, reason)
                        continue
                    store_confidence(conn, sha, page, conf)
                    measured += 1
                    with lock:
                        stats['pages_measured'] += 1
                        if conf < CONF_THRESHOLD:
                            stats['below_conf_threshold'] += 1
                        if conf < ESCALATE_THRESHOLD:
                            stats['below_escalate_threshold'] += 1
                if ok:
                    unreached = [page for page in wanted if page not in batched]
                    if unreached:
                        # The container was cut off. These stay NULL and the
                        # next sweep retries them, exactly like a deadline.
                        with lock:
                            stats['deadline_documents'] += 1
                        log(f'{sha[:12]} batch reached '
                            f'{len(batched)}/{len(wanted)} pages; '
                            f'{len(unreached)} left for the next sweep')
                        return measured, 'deadline'
                    return measured, None
                # The container itself failed. Fall through to the per-page
                # path so one bad page can be isolated from the rest.
                log(f'{sha[:12]} batch container failed; measuring per page')
                wanted = [page for page in wanted if page not in batched]
        for page in wanted:
            now = time.time()
            if now >= deadline:
                with lock:
                    stats['deadline_documents'] += 1
                log(f'{sha[:12]} hit its deadline after {measured} pages; '
                    f'{len(pages) - measured} left for the next sweep')
                return measured, 'deadline'
            budget = deadline - now
            conf, reason = measure_page(
                work, name, page, renderer,
                clamp(worker.RENDER_SECONDS, budget),
                clamp(worker.TESSERACT_SECONDS, budget))
            if conf is None:
                record_skip(conn, sha, page, reason)
                continue
            store_confidence(conn, sha, page, conf)
            measured += 1
            with lock:
                stats['pages_measured'] += 1
                if conf < CONF_THRESHOLD:
                    stats['below_conf_threshold'] += 1
                if conf < ESCALATE_THRESHOLD:
                    stats['below_escalate_threshold'] += 1
    return measured, None


def run_shard(conn, shard, shards, renderer, limit=None,
              doc_seconds=DOC_SECONDS):
    """Measure this shard's pages, then stop.

    Termination is bounded by PROGRESS, not by emptiness: a full sweep that
    measures nothing ends the run rather than spinning on pages that
    something outside this process has to fix. A sweep that did make
    progress rewinds and sweeps again, because a document cut short by
    its deadline still has pages behind the cursor.
    """
    # This call is the sweep, and the attempt counter is denominated in
    # sweeps -- so the charge ledger is cleared HERE and not in main(). The
    # rewind below is precisely why that distinction exists: it reaches the
    # same unmeasurable page again, and charging each of those visits spent
    # the entire five-attempt budget before this function returned.
    begin_sweep()
    cursor, rewound, progress, documents = '', True, 0, 0
    while True:
        rows = pending_documents(conn, shard, shards, cursor)
        if not rows:
            if rewound or progress == 0:
                return documents
            cursor, rewound, progress = '', True, 0
            continue
        rewound = False
        for sha, key, doc_pages in rows:
            if limit is not None and documents >= limit:
                return documents
            pages = pending_pages(conn, sha)
            if not pages:
                continue
            try:
                measured, _outcome = process_document(
                    conn, sha, key, doc_pages, pages, renderer,
                    time.time() + doc_seconds)
            except Exception as exc:
                # One unexpected document must not end the shard: the pages
                # stay NULL, so nothing is lost, and a fault that repeats on
                # every document still terminates the run through the
                # progress rule below rather than through a stack trace.
                log(f'{sha[:12]} EXCEPTION {type(exc).__name__}: '
                    f'{str(exc)[:200]}')
                measured = 0
            progress += measured
            documents += 1
            with lock:
                stats['documents'] += 1
        cursor = rows[-1][0]


def status_key(shard, shards):
    """One key per shard once sharded.

    640 shards writing ws13/confidence/status.json would leave only the last
    writer's numbers, which is worse than no heartbeat because it looks
    authoritative. The documented single-shard path keeps that name.
    """
    if shards <= 1:
        return 'ws13/confidence/status.json'
    return f'ws13/confidence/status-{shard:04d}-of-{shards:04d}.json'


def status_payload(shard, shards, started, pages_left, docs_left,
                   renderer, phase, counted=True):
    """Everything an operator needs to project a finish time.

    `pages_remaining_counted` says whether the remaining count came from the
    database this tick or was derived from the last count plus local
    progress -- see heartbeat(). A derived number is still worth publishing;
    silently presenting it as a fresh count is not.
    """
    with lock:
        snapshot = dict(stats)
    elapsed = max(1e-6, time.time() - started)
    rate = snapshot['pages_measured'] / elapsed
    return {'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
            'phase': phase, 'shard': shard, 'shards': shards,
            'renderer': renderer, 'elapsed_seconds': round(elapsed, 1),
            'pages_remaining': pages_left, 'documents_remaining': docs_left,
            'pages_remaining_counted': counted,
            'pages_per_second': round(rate, 4),
            'seconds_per_page': round(1 / rate, 2) if rate else None,
            'eta_hours': (round(pages_left / rate / 3600, 2)
                          if rate and pages_left else None),
            **snapshot}


def heartbeat(stop, shard, shards, started, renderer, phase):
    """Progress to S3 every HEARTBEAT_SECONDS on its own connection.

    Its own connection because the shard's connection is inside
    process_document for minutes at a time, and the count is shard-scoped so
    no heartbeat aggregates the whole table.

    The count is still not run every tick. At 640 shards, one filtered
    aggregate over 760,059 ws13_pages rows every 300 s is 2.1 aggregates a
    second against the same RDS instance the shards read their work from --
    paid to print a number, competing with the work it is reporting on. So
    the database is asked at most every REMAINING_SECONDS and the ticks in
    between subtract this shard's own measured pages from the last count.
    The payload says which of the two it published.
    """
    conn = psycopg.connect(DSN, autocommit=True)
    counted_at, counted_pages, counted_docs, counted_measured = 0.0, None, 0, 0
    try:
        while True:
            try:
                now = time.time()
                with lock:
                    measured = stats['pages_measured']
                fresh = (counted_pages is None
                         or now - counted_at >= REMAINING_SECONDS)
                if fresh:
                    counted_pages, counted_docs = remaining(conn, shard,
                                                            shards)
                    counted_at, counted_measured = now, measured
                    pages_left = counted_pages
                else:
                    pages_left = max(0, counted_pages
                                     - (measured - counted_measured))
                s3.put_object(
                    Bucket=BUCKET, Key=status_key(shard, shards),
                    ContentType='application/json',
                    Body=json.dumps(status_payload(
                        shard, shards, started, pages_left, counted_docs,
                        renderer, phase[0], counted=fresh)).encode())
            except Exception as exc:
                # A heartbeat is metadata about the work, never a reason to
                # stop the work.
                log(f'heartbeat failed: {type(exc).__name__}: {exc}')
            if stop.wait(HEARTBEAT_SECONDS):
                return
    finally:
        conn.close()


def shard_totals(doc_rows, shards):
    """Pages per shard for a candidate shard count. -> {shard: pages}."""
    totals = {}
    for sha, pages in doc_rows:
        key = shard_of(sha, shards)
        totals[key] = totals.get(key, 0) + pages
    return totals


def plan(doc_rows, rate):
    """Capacity arithmetic for 1 / 8 / 40 nodes of c7g.4xlarge.

    Reports the worst shard as well as the mean because the shard split is
    by document: the shard that owns the 1,407-page document carries it
    whole, and the run is finished when the LAST shard is finished. Fleet
    sizes past the point where the largest document dominates buy nothing,
    and this is the table that shows where that point is.
    """
    pages = sum(p for _, p in doc_rows)
    rows = []
    for nodes in PLAN_NODES:
        shards = nodes * NODE_VCPU
        totals = shard_totals(doc_rows, shards)
        worst = max(totals.values()) if totals else 0
        rows.append({
            'nodes': nodes, 'cores': shards,
            'mean_pages': pages / shards if shards else 0,
            'worst_pages': worst,
            'even_hours': (pages / (rate * shards) / 3600) if rate else None,
            'worst_hours': (worst / rate / 3600) if rate else None})
    return {'pages_remaining': pages, 'documents_remaining': len(doc_rows),
            'rate': rate, 'seconds_per_page': (1 / rate) if rate else None,
            'node_type': NODE_TYPE, 'vcpu_per_node': NODE_VCPU, 'rows': rows}


def print_plan(report):
    done = PAGES_TOTAL - report['pages_remaining']
    log(f'pages remaining      : {report["pages_remaining"]:,} of the '
        f'{PAGES_TOTAL:,} OCR pages ({100.0 * done / PAGES_TOTAL:.1f}% '
        f'already measured)')
    log(f'documents remaining  : {report["documents_remaining"]:,} of '
        f'{DOCS_TOTAL:,}')
    log(f'rate                 : {report["rate"]:.4f} pages/s/core '
        f'({report["seconds_per_page"]:.2f} s/page, one core measures one '
        f'page at a time)')
    log(f'fleet                : {report["node_type"]} @ '
        f'{report["vcpu_per_node"]} vCPU, one shard per vCPU, '
        f'{MAX_NODES} nodes = the 640 vCPU quota')
    log('nodes  cores   mean pages/shard   worst shard   even-split h'
        '   worst-shard h')
    for row in report['rows']:
        log(f'{row["nodes"]:5d}  {row["cores"]:5d}   {row["mean_pages"]:16,.0f}'
            f'   {row["worst_pages"]:11,}   {row["even_hours"]:8.2f} h   '
            f'{row["worst_hours"]:8.2f} h')
    log('wall clock is the worst-shard column: the run ends when the last '
        'shard ends.')


def dry_run(conn, shard, shards, rate):
    """Work set size and projected wall clock. Renders nothing."""
    doc_rows = pending_by_document(conn)
    mine = [(sha, pages) for sha, pages in doc_rows
            if shard_of(sha, shards) == shard]
    pages = sum(p for _, p in mine)
    report = plan(doc_rows, rate)
    report['shard'] = shard
    report['shards'] = shards
    report['shard_pages'] = pages
    report['shard_documents'] = len(mine)
    report['shard_hours'] = (pages / rate / 3600) if rate else None
    return report


def print_dry_run(report):
    log(f'DRY RUN: shard {report["shard"]} of {report["shards"]} is not '
        f'run; nothing is rendered and no page is written')
    log(f'this shard  : {report["shard_pages"]:,} pages across '
        f'{report["shard_documents"]:,} documents')
    if report['shard_hours'] is not None:
        log(f'projected   : {report["shard_hours"]:.2f} h at '
            f'{report["rate"]:.4f} pages/s')
    print_plan(report)


def verify_complete(conn, shards):
    """Is the whole pass actually finished? -> report dict.

    The third blocker in FleetMode: confidence was that nothing asserted
    every slot reached 'complete' -- once the group decremented to zero,
    "finished" and "finished with shards nobody ever claimed" were the same
    observable. This answers that question from the database rather than from
    the claim objects, which is the stronger place to ask it: a shard whose
    node died before it ever claimed a slot leaves no trace in S3 at all, and
    leaves exactly the pages it did not measure in ws13_pages.

    Two numbers per shard, because they mean different things. `pages` is
    still owed a measurement. `exhausted` was given up on after
    MAX_TRANSIENT_ATTEMPTS and is no longer in that debt -- which is a
    legitimate end state, and one an operator has to be told about rather
    than shown as a clean zero.
    """
    doc_rows = pending_by_document(conn)
    per_shard = {}
    for sha, pages in doc_rows:
        index = shard_of(sha, shards)
        per_shard[index] = per_shard.get(index, 0) + pages
    gave_up, gave_up_docs = exhausted(conn, 0, 1)
    return {'shards': shards,
            'pages_remaining': sum(p for _, p in doc_rows),
            'documents_remaining': len(doc_rows),
            'exhausted_pages': gave_up, 'exhausted_documents': gave_up_docs,
            'incomplete_shards': sorted(per_shard),
            'shard_pages': per_shard}


def print_verify(report):
    """The assertion, spelled out. Never a bare 'complete'."""
    log(f'VERIFY over {report["shards"]} shards')
    if report['pages_remaining']:
        log(f'INCOMPLETE: {report["pages_remaining"]:,} pages across '
            f'{report["documents_remaining"]:,} documents are still '
            f'unmeasured')
        log(f'{len(report["incomplete_shards"])} shard(s) still owe pages: '
            + ', '.join(str(i) for i in report['incomplete_shards'][:40])
            + (' ...' if len(report['incomplete_shards']) > 40 else ''))
        log('a shard here was never claimed, or its node died holding it. '
            'Re-run the pass for those shard indices; nothing else recovers '
            'them, and no claim object, metric or alarm reports them.')
    else:
        log('every shard is measured: 0 pages remain in the work set')
    if report['exhausted_pages']:
        # Reported whether or not anything remains, because this is the
        # number that "0 remaining" would otherwise absorb.
        log(f'NOT MEASURED, NOT RETRIED: {report["exhausted_pages"]:,} pages '
            f'across {report["exhausted_documents"]:,} documents used all '
            f'{MAX_TRANSIENT_ATTEMPTS} attempts and left the work set. Read '
            f'their reasons: SELECT reason, count(*) FROM ws13_conf_skips '
            f'WHERE attempts >= {MAX_TRANSIENT_ATTEMPTS} GROUP BY 1')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--shard', type=int, default=SHARD,
                        help='this process\'s shard index (WS13_SHARD)')
    parser.add_argument('--shards', type=int, default=SHARD_COUNT,
                        help='total shards (WS13_SHARD_COUNT)')
    parser.add_argument('--limit', type=int,
                        help='stop after this many documents')
    parser.add_argument('--doc-seconds', type=int, default=DOC_SECONDS,
                        help=f'per-document deadline (default {DOC_SECONDS})')
    parser.add_argument('--rate', type=float, default=DEFAULT_RATE,
                        help='pages/second/core used for the projections. '
                             'Defaults to a SEED, not a measurement: run a '
                             'short --limit sweep and pass back the '
                             'pages_per_second its heartbeat reports')
    parser.add_argument('--dry-run', action='store_true',
                        help='report the work set and projected wall clock, '
                             'render nothing')
    parser.add_argument('--plan', action='store_true',
                        help='print the capacity arithmetic at 1/8/40 nodes')
    parser.add_argument('--verify-complete', action='store_true',
                        help='assert every shard of --shards is measured '
                             'and name the ones that are not. Renders no '
                             'page and writes no measurement; exits non-zero '
                             'unless the whole work set is accounted for')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        log(f'ABORT: shard {args.shard} is not in 0..{args.shards - 1}')
        return EXIT_BAD_SHARD
    if args.rate <= 0:
        # Every projection divides by it; a zero here would be reported as an
        # infinite fleet rather than as the bad input it is.
        log(f'ABORT: --rate must be positive, got {args.rate}')
        return EXIT_BAD_SHARD
    conn = psycopg.connect(DSN, autocommit=True)
    try:
        # Created in every mode: the work predicate anti-joins this table, so
        # it has to exist even to COUNT the work set.
        ensure_skips_table(conn)
        if args.verify_complete:
            report = verify_complete(conn, args.shards)
            print_verify(report)
            return (EXIT_INCOMPLETE if report['pages_remaining']
                    else EXIT_SHARD_DONE)
        if args.plan:
            print_plan(plan(pending_by_document(conn), args.rate))
            return EXIT_SHARD_DONE
        if args.dry_run:
            print_dry_run(dry_run(conn, args.shard, args.shards, args.rate))
            return EXIT_SHARD_DONE

        os.makedirs(SCRATCH, exist_ok=True)
        if not shutil.which('docker'):
            log('ABORT: docker is not on PATH; this pass renders pages '
                'inside the ocrmypdf image')
            return EXIT_NO_RENDERER
        try:
            subprocess.run(['docker', 'pull', worker.OCR_IMAGE],
                           capture_output=True, timeout=1800)
        except Exception as exc:
            # A cold pull on a node where 16 siblings just started the daemon
            # is routinely slow. Not fatal by itself: the probe below is the
            # thing that decides whether this node can measure at all.
            log(f'docker pull failed ({type(exc).__name__}); probing anyway')
        renderer, reason = worker.page_renderer()
        if renderer is None:
            # Exit rather than write a skip row per page: a node that cannot
            # render is the pdftoppm defect all over again, and 323,059 rows
            # of 'renderer_unavailable' would bury the real reasons.
            log(f'ABORT: no page renderer in {worker.OCR_IMAGE} ({reason}). '
                f'Nothing measurable here; not writing unmeasured pages.')
            return EXIT_NO_RENDERER
        log(f'renderer {renderer}; measuring shard {args.shard} of '
            f'{args.shards}')

        started = time.time()
        stop = threading.Event()
        phase = ['measuring']
        beat = threading.Thread(target=heartbeat, daemon=True,
                                args=(stop, args.shard, args.shards, started,
                                      renderer, phase))
        beat.start()
        try:
            documents = run_shard(conn, args.shard, args.shards, renderer,
                                  limit=args.limit,
                                  doc_seconds=args.doc_seconds)
        finally:
            phase[0] = 'finished'
            stop.set()
            beat.join(timeout=30)

        elapsed = max(1e-6, time.time() - started)
        rate = stats['pages_measured'] / elapsed
        pages_left, docs_left = remaining(conn, args.shard, args.shards)
        log(f'measured {stats["pages_measured"]} pages over {documents} '
            f'documents in {elapsed:.0f}s ({rate:.3f} pages/s, '
            f'{(1 / rate) if rate else float("inf"):.2f} s/page)')
        # Pages, not rows. A page this sweep reached three times over its
        # rewinds is one page nobody could measure, and counting the rows
        # would inflate every number here by the rewind depth.
        log(f'skip pages: {stats["terminal_skips"]} terminal, '
            f'{stats["transient_skips"]} transient (retried next sweep), '
            f'{stats["exhausted_skips"]} out of attempts (NOT retried '
            f'again); {stats["deadline_documents"]} documents hit the '
            f'deadline')
        # The size of phase 2's work set, which nothing could know before
        # this pass ran.
        log(f'below CONF_THRESHOLD {CONF_THRESHOLD}: '
            f'{stats["below_conf_threshold"]} pages; below '
            f'ESCALATE_THRESHOLD {ESCALATE_THRESHOLD}: '
            f'{stats["below_escalate_threshold"]} pages')
        gave_up, gave_up_docs = exhausted(conn, args.shard, args.shards)
        log(f'shard remaining: {pages_left} pages / {docs_left} documents')
        if gave_up:
            # Said every run, not only the last one: this is the population
            # that "remaining 0" would otherwise absorb without a word.
            log(f'shard gave up on {gave_up} pages / {gave_up_docs} '
                f'documents after {MAX_TRANSIENT_ATTEMPTS} attempts each; '
                f'they are excluded from the work set and were never '
                f'measured')
        if pages_left == 0:
            return EXIT_SHARD_DONE
        # Which of the two "not finished" answers this is decides whether the
        # caller should sweep again at once or back off: a sweep that measured
        # nothing will measure nothing again a second later.
        return (EXIT_WORK_REMAINS if stats['pages_measured']
                else EXIT_NO_PROGRESS)
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
