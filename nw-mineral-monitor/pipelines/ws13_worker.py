#!/usr/bin/env python3
"""WS13 Phase 2/3 bulk worker: OCR + chunk + embed + index, keyed by sha256.

Runs as N parallel processes per instance, each long-polling the SQS work
queue. One message = one document (sha256 == WS12 doc_id). Per document:

  1. manifest lookup in Postgres: already 'done' -> ack and skip (rerun-safe)
  2. fetch original from S3 (content-addressed key) and verify sha256
  3. born_digital: direct per-page text; ocr_queue: containerized ocrmypdf
     (deskew/rotate/clean) producing a searchable PDF OVER the original pages
     (pagination preserved) + sidecar text + per-page tesseract confidences
  4. pages below threshold -> re-OCR at stronger settings (escalation tier 1);
     still-weak pages recorded as low_confidence and queued for the capped
     vision-model tier (never silently dropped, never fabricated)
  5. page-anchored chunks (~750 tokens, overlap) carrying doc_id, page,
     portal, state, mine ids/names, county, TRS, doc date/type from the WS12
     harvest manifest
  6. Cohere Embed v4 (1536-d) via the us. inference profile, batched
  7. upsert document/pages/chunks (tsvector + vector) into Postgres; upload
     searchable PDF + sidecar to s3://bucket/ws13/searchable/{sha}/...
  8. mark 'done' with counts; any failure -> 'error' with reason (message
     goes back to SQS for retry, then DLQ after 3 attempts)

Every document runs under one DocumentLease: a hard wall-clock budget that
every phase is clamped to, a background renewal of the SQS visibility so long
work never becomes concurrent work, and a periodic ws13_manifest.updated_at
heartbeat that ws13_reap_stale.py reads as liveness.

Nothing skips silently: every doc ends in exactly one terminal manifest state.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

import boto3
import psycopg

OCR_IMAGE = 'docker.io/jbarlow83/ocrmypdf:latest'
EMBED_MODEL = 'us.cohere.embed-v4:0'
EMBED_DIMS = 1536
EMBED_BATCH = 48
CHUNK_CHARS = 3000
CHUNK_OVERLAP = 400
CONF_THRESHOLD = 60.0
ESCALATE_THRESHOLD = 45.0
# Page rasterisers tried, in order, inside the OCR image. Ghostscript is what
# ocrmypdf itself rasterises with, so it is guaranteed present and the list
# always ends in a working fallback.
PAGE_RENDERERS = ('pdftoppm', 'pdftocairo', 'gs')
# Probe outcomes that are properties of this node or of the image and cannot
# change inside one process, so they are decided once. Everything else is a
# slow or contended docker daemon and is re-probed -- see page_renderer().
TERMINAL_PROBE_REASONS = ('no_docker', 'no_renderer_in_image')
PROBE_RETRY_SECONDS = 300
_page_renderer = None            # terminal (name, reason), decided once
_probe_transient = 'not_probed'  # last transient failure, retried
_probe_next_try = 0.0
_container_seq = itertools.count()

MAX_DOC_SECONDS = int(os.environ.get('WS13_MAX_DOC_SECONDS', '3300'))
# Wall-clock budget for ONE DOCUMENT, and the arithmetic that makes it safe.
# MAX_DOC_SECONDS bounds one ocrmypdf container, not the document. Per-page
# confidence rendering now actually runs (it used to exit rc=127 in ~0.5 s a
# page), which costs two docker runs per measured page, and the tier-1
# escalation is reachable for the first time, which doubles both the OCR and
# the confidence work: a 200-page scan measured at ~5-8 s/page comes to
# ~5500 s against the queue's VisibilityTimeout of 3600 s
# (infra/ws13_dataplane.yaml). SQS then handed the same sha to a second
# worker while the first was still working it -- manifest_row() reads
# 'running', not 'done', so neither skips -- both ran the DELETE/INSERT over
# ws13_chunks and both paid Bedrock for the same embeddings, and after
# maxReceiveCount 3 redeliveries a document every worker had SUCCEEDED on
# landed in ws13-ocr-dlq, silently absent from the index. Two bounds:
#   * DocumentLease re-extends the visibility to LEASE_SECONDS every
#     LEASE_TICK_SECONDS while this process lives, so long work never
#     becomes concurrent work;
#   * every phase timeout is clamped to the remaining budget, so a document
#     holds its message for at most DOC_BUDGET_SECONDS plus the final index
#     phase -- not "however long OCR happens to take".
# 7200 s also sits far under the 12 h SQS ceiling on total extension, and the
# reaper ages a row from its last heartbeat (at most one container run old),
# so ws13_reap_stale.py's 2 h floor keeps better than 2x margin.
DOC_BUDGET_SECONDS = int(os.environ.get('WS13_DOC_BUDGET_SECONDS', '7200'))
LEASE_SECONDS = int(os.environ.get('WS13_LEASE_SECONDS', '3600'))
LEASE_TICK_SECONDS = int(os.environ.get('WS13_LEASE_TICK_SECONDS', '300'))
# ws13_manifest.updated_at was written only at status transitions, so a
# worker legitimately busy for 50 minutes on one document looked identical to
# an abandoned row: ws13_reap_stale.py would move it to 'error' and
# ws13_enqueue.py would start a second worker on the same sha256.
HEARTBEAT_SECONDS = int(os.environ.get('WS13_HEARTBEAT_SECONDS', '120'))
# Kept back from the budget for chunk/embed/index, which is the product; OCR
# quality metadata is not allowed to eat it.
INDEX_RESERVE_SECONDS = int(os.environ.get('WS13_INDEX_RESERVE_SECONDS', '600'))
CONF_MAX_SECONDS = int(os.environ.get('WS13_CONF_MAX_SECONDS', '900'))
CONF_MIN_SECONDS = 60
CONF_SAMPLE_PAGES = int(os.environ.get('WS13_CONF_SAMPLE_PAGES', '60'))
# A single 150 dpi page raster or tesseract pass that takes five minutes is a
# failure, not slow work; the old 300 s each let one page eat 10 minutes of
# the document's budget.
RENDER_SECONDS = int(os.environ.get('WS13_RENDER_SECONDS', '120'))
TESSERACT_SECONDS = int(os.environ.get('WS13_TESSERACT_SECONDS', '180'))
PROBE_SECONDS = 120
# Touched by the node agent (infra/ws13_fleet.yaml) when the Auto Scaling
# group asks to terminate this node: finish the document in hand, take no
# new one. Without it a scale-in kills a worker mid-document, the message
# goes back to SQS with ReceiveCount incremented, and three of those DLQ it.
DRAIN_FILE = os.environ.get('WS13_DRAIN_FILE', '/opt/ws13/drain')

BUCKET = os.environ['WS13_BUCKET']
QUEUE_URL = os.environ['WS13_QUEUE_URL']
DB_DSN = os.environ['WS13_DB_DSN']
WORKER_ID = os.environ.get('WS13_WORKER_ID', f'{os.uname().nodename}:{os.getpid()}')
SCRATCH = os.environ.get('WS13_SCRATCH', '/opt/ws13/scratch')
REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')
# 'inline' embeds during processing; 'defer' stores chunks with NULL
# embeddings for a later quota-budgeted backfill pass (daily token caps).
EMBED_MODE = os.environ.get('WS13_EMBED_MODE', 'inline')

s3 = boto3.client('s3', region_name=REGION)
sqs = boto3.client('sqs', region_name=REGION)
bedrock = boto3.client('bedrock-runtime', region_name=REGION)


def log(msg):
    print(f'{dt.datetime.now(dt.timezone.utc).isoformat()} [{WORKER_ID}] {msg}',
          flush=True)


def docker(args, work, timeout, entrypoint=None):
    """Run one container, and make sure a timeout actually stops it.

    subprocess's timeout kills the `docker run` CLIENT; the container keeps
    running. Now that every container timeout is clamped to the document's
    remaining budget, timeouts are a normal event rather than a never-seen
    edge case, and an orphaned ocrmypdf would keep burning CPU on the node
    for the rest of the fleet's life. Name the container so it can be
    force-removed on the way out.
    """
    name = f'ws13-{os.getpid()}-{next(_container_seq)}'
    cmd = ['docker', 'run', '--rm', '--name', name,
           '--user', '0:0', '-v', f'{work}:/work']
    if entrypoint:
        cmd += ['--entrypoint', entrypoint]
    cmd += [OCR_IMAGE] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True,
                       timeout=120)
        raise


def ocr(work, in_name, out_name, sidecar_name, timeout, strong=False):
    args = ['--deskew', '--rotate-pages', '--clean', '--skip-text',
            '--sidecar', f'/work/{sidecar_name}',
            '--jobs', os.environ.get('WS13_OCR_JOBS', '2')]
    extra = os.environ.get('WS13_OCR_EXTRA_ARGS', '').split()
    args += extra
    if strong:
        args += ['--oversample', '400', '--clean-final',
                 '--tesseract-timeout', '600']
    args += [f'/work/{in_name}', f'/work/{out_name}']
    started = time.time()
    try:
        result = docker(args, work, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Reported as a normal OCR failure so the document ends 'error' with
        # a reason that names the budget, instead of unwinding through the
        # generic handler as an unexplained TimeoutExpired.
        return (time.time() - started, -1,
                f'ocrmypdf killed after {timeout}s (document budget)')
    return time.time() - started, result.returncode, result.stderr[-1500:]


def clean_text(text):
    """Strip NUL bytes.

    PostgreSQL text columns cannot store 0x00, and pypdf's extract_text() on
    malformed PDFs emits them: eight born_digital documents failed the whole
    transaction with `DataError: PostgreSQL text fields cannot contain NUL
    (0x00) bytes`, taking their chunks down with them. A NUL carries no
    information here, so drop it rather than lose the document."""
    return text.replace('\x00', '') if text else text


def page_texts_from_sidecar(path):
    if not os.path.exists(path):
        return []
    text = open(path, encoding='utf-8', errors='replace').read()
    return [clean_text(p) for p in text.split('\f')]


def probe_page_renderer():
    """Find a page rasteriser inside the OCR image. Runs once per process.

    One `command -v` per candidate rather than one call with three operands:
    POSIX defines a single operand and dash prints nothing for the rest, so
    the three-operand form would report only pdftoppm and miss the fallbacks.
    """
    if not shutil.which('docker'):
        return None, 'no_docker'
    script = ('for c in ' + ' '.join(PAGE_RENDERERS) +
              '; do command -v "$c" || true; done')
    try:
        probe = docker(['-c', script], SCRATCH, timeout=PROBE_SECONDS,
                       entrypoint='sh')
    except Exception as exc:
        return None, f'probe_failed:{type(exc).__name__}'
    if probe.returncode != 0:
        return None, f'probe_exit_{probe.returncode}'
    found = {os.path.basename(line.strip())
             for line in probe.stdout.splitlines() if line.strip()}
    for name in PAGE_RENDERERS:
        if name in found:
            return name, None
    return None, 'no_renderer_in_image'


def page_renderer():
    """Probe result: terminal answers cached, transient ones retried.

    Caching every outcome for the life of the process meant one slow docker
    call at boot disabled confidence measurement AND the tier-1 escalation
    for that worker's entire run: main() probes right after `docker pull`
    while 8 sibling workers on the same node do the same thing seconds after
    `systemctl start docker`, so a contended daemon is routine, and
    probe_page_renderer() reports that as 'probe_failed:TimeoutExpired'. The
    worker that lost that race then wrote confidence=NULL and
    error='conf_unavailable:...' on every ocr_queue document it would ever
    touch, announced by one log line at startup.

    'no_docker' and 'no_renderer_in_image' are the only outcomes that cannot
    change inside one process; those are decided once. A transient failure is
    retried every PROBE_RETRY_SECONDS, and every re-probe logs, so a
    persistently failing node stays visible instead of going quiet.
    """
    global _page_renderer, _probe_transient, _probe_next_try
    if _page_renderer is not None:
        return _page_renderer
    now = time.time()
    if now < _probe_next_try:
        return None, _probe_transient
    name, reason = probe_page_renderer()
    if name or reason in TERMINAL_PROBE_REASONS:
        _page_renderer = (name, reason)
        if name:
            log(f'page renderer for confidences: {name}')
        else:
            # Once per process, not once per document: this is a property of
            # the image, and it must never read downstream as "no weak page".
            log(f'NO page renderer in {OCR_IMAGE} ({reason}); per-page '
                f'confidences will be recorded as unavailable')
        return _page_renderer
    _probe_transient = reason
    _probe_next_try = now + PROBE_RETRY_SECONDS
    log(f'page renderer probe failed ({reason}); retrying in '
        f'{PROBE_RETRY_SECONDS}s. Confidences are unavailable until it '
        f'succeeds, and are recorded as unmeasured, not as clean.')
    return None, reason


def render_argv(renderer, pdf_name, base, index):
    """argv that renders exactly page `index` to a /work/{base}*.png.

    Each tool spells the same request differently: pdftocairo needs an
    explicit -png and -r, and gs takes a full output path rather than a
    filename prefix. The caller globs for {base}*.png either way, so the
    digit suffix pdftoppm picks does not matter.
    """
    page = str(index)
    if renderer == 'pdftoppm':
        return ['-r', '150', '-png', '-f', page, '-l', page,
                f'/work/{pdf_name}', f'/work/{base}']
    if renderer == 'pdftocairo':
        return ['-png', '-r', '150', '-f', page, '-l', page,
                f'/work/{pdf_name}', f'/work/{base}']
    if renderer == 'gs':
        return ['-sDEVICE=png16m', '-r150', '-dNOPAUSE', '-dBATCH',
                f'-dFirstPage={page}', f'-dLastPage={page}',
                '-o', f'/work/{base}-1.png', f'/work/{pdf_name}']
    raise ValueError(f'unsupported page renderer: {renderer}')


def sample_indices(pages, cap=CONF_SAMPLE_PAGES):
    """1-based page numbers to measure: all of them, or `cap` evenly spaced.

    Measuring every page costs two docker runs per page (~5-8 s), so a
    900-page scan spends more wall clock proving its OCR quality than
    producing it -- which is what pushed documents past the SQS visibility
    timeout. The escalation decision needs a representative read, not a
    census, so above `cap` pages this samples and page_confidences() reports
    the denominator ('partial:60/900') instead of implying a full count.
    """
    if pages <= 0:
        return []
    if cap <= 0 or pages <= cap:
        return list(range(1, pages + 1))
    step = pages / cap
    return sorted({min(pages, int(i * step) + 1) for i in range(cap)})


def _try_docker(args, work, timeout, entrypoint):
    """docker() that returns None on timeout.

    A confidence measurement is metadata: it may fail, and it must never take
    the document down with it. Without this a single slow page raster raised
    TimeoutExpired out of process() and failed a document whose OCR had
    already succeeded.
    """
    try:
        return docker(args, work, timeout=timeout, entrypoint=entrypoint)
    except subprocess.TimeoutExpired:
        return None


def page_confidences(work, pdf_name, pages, indices=None, lease=None):
    """Per-page mean tesseract word confidence -> (confidences, reason).

    Loud on failure: a render or TSV error logs once per document instead of
    silently yielding an empty list (the defect that blanked confidence
    metadata for the first bulk sweep).

    Loud was not enough, because the renderer itself was wrong. This shelled
    pdftoppm unconditionally and pdftoppm is not on $PATH in the ocrmypdf
    image: every render exited rc=127, so 0 of 760,043 ws13_pages rows carry
    a confidence today and the tier-1 escalation in process() has never once
    fired. The renderer is now probed (pdftoppm, pdftocairo, then gs), and
    when the measurement cannot be taken at all the caller gets a
    machine-readable reason to store instead of a list of Nones that would
    be recorded as low_conf_pages=0, i.e. as "nothing was weak".

    The reason is now per-page-count, not all-or-nothing. `all(c is None)`
    only fired when EVERY page failed, so a 300-page document where the
    renderer produced page 1 and then failed on pages 2-300 was reported as
    fully measured and written as low_conf_pages=0 -- 'nothing was weak'
    about 299 pages nobody looked at. Any shortfall now returns
    'partial:{measured}/{pages}', which process() turns into
    low_conf_pages=NULL.

    Returns a list of length `pages` with None for every unmeasured page.
    """
    confs = [None] * pages
    renderer, reason = page_renderer()
    if renderer is None:
        return confs, reason
    if indices is None:
        indices = sample_indices(pages)
    stop = time.time() + CONF_MAX_SECONDS
    if lease is not None:
        stop = min(stop, lease.deadline - INDEX_RESERVE_SECONDS)
    logged_failure = False
    cut_short = None
    for index in indices:
        if not 1 <= index <= pages:
            continue
        if time.time() >= stop:
            cut_short = 'budget'
            break
        if lease is not None:
            lease.heartbeat()
        base = f'pg{index:05d}'
        render = _try_docker(render_argv(renderer, pdf_name, base, index),
                             work, RENDER_SECONDS, renderer)
        if (render is None or render.returncode != 0) and not logged_failure:
            logged_failure = True
            rc = 'timeout' if render is None else f'rc={render.returncode}'
            err = '' if render is None else render.stderr[-300:]
            log(f'confidence render failed {rc} ({renderer}): {err}')
        pngs = sorted(n for n in os.listdir(work)
                      if n.startswith(base) and n.endswith('.png'))
        if not pngs:
            continue
        tsv = _try_docker([f'/work/{pngs[0]}', 'stdout', 'tsv'], work,
                          TESSERACT_SECONDS, 'tesseract')
        if tsv is not None:
            words = [float(p[10]) for p in (l.split('\t') for l in
                     tsv.stdout.splitlines()[1:])
                     if len(p) > 11 and p[10] not in ('-1', '')]
            if words:
                confs[index - 1] = round(statistics.mean(words), 1)
        for name in pngs:
            os.unlink(os.path.join(work, name))
    measured = sum(1 for c in confs if c is not None)
    suffix = f':{cut_short}' if cut_short else ''
    if not pages:
        return confs, None
    if measured == 0:
        # A renderer exists but produced nothing for any page. That is still
        # not the same measurement as "no page was weak", so it is reported
        # as unmeasured rather than stored as a clean zero.
        return confs, f'no_page_measured{suffix}'
    if measured < pages:
        return confs, f'partial:{measured}/{pages}{suffix}'
    return confs, None


def chunk_pages(page_texts):
    chunks = []
    for page_no, text in enumerate(page_texts, 1):
        text = re.sub(r'[ \t]+', ' ', text or '').strip()
        if not text:
            continue
        start = 0
        ordinal = 0
        while start < len(text):
            end = min(len(text), start + CHUNK_CHARS)
            if end < len(text):
                cut = max(text.rfind('\n\n', start, end),
                          text.rfind('. ', start, end))
                if cut > start + CHUNK_CHARS // 2:
                    end = cut + 1
            piece = text[start:end].strip()
            if piece:
                chunks.append({'page': page_no, 'ordinal': ordinal,
                               'start': start, 'end': end, 'text': piece})
                ordinal += 1
            if end >= len(text):
                break
            start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def embed(texts, lease=None):
    """Batched Cohere Embed v4. Heartbeats between batches.

    A document with thousands of chunks spends minutes here, and under
    Bedrock throttling the retry sleeps stretch that further. Without a
    heartbeat the manifest row goes untouched for the whole phase and
    ws13_reap_stale.py cannot tell it from an abandoned one.
    """
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        if lease is not None:
            lease.heartbeat()
        batch = [t[:6000] for t in texts[i:i + EMBED_BATCH]]
        for attempt in range(6):
            try:
                r = bedrock.invoke_model(modelId=EMBED_MODEL, body=json.dumps({
                    'texts': batch, 'input_type': 'search_document',
                    'embedding_types': ['float'], 'truncate': 'END'}))
                vectors.extend(json.loads(r['body'].read())['embeddings']['float'])
                break
            except Exception as exc:
                if attempt == 5:
                    raise
                time.sleep(min(30, 2 ** attempt) + (attempt * 0.37))
    return vectors


def manifest_row(conn, sha):
    return conn.execute(
        'SELECT status FROM ws13_manifest WHERE sha256=%s', (sha,)).fetchone()


def set_status(conn, sha, status, **fields):
    cols = ['status', 'worker_id', 'updated_at'] + list(fields)
    vals = [status, WORKER_ID, dt.datetime.now(dt.timezone.utc)] + \
        list(fields.values())
    sets = ', '.join(f'{c}=EXCLUDED.{c}' for c in cols)
    conn.execute(
        f'INSERT INTO ws13_manifest (sha256, {",".join(cols)}) '
        f'VALUES (%s, {",".join(["%s"] * len(cols))}) '
        f'ON CONFLICT (sha256) DO UPDATE SET {sets}',
        [sha] + vals)
    conn.commit()


class LeaseLost(RuntimeError):
    """This process can no longer prove it owns the document's SQS message.

    Raised at a checkpoint rather than pressed on: once the visibility lease
    has lapsed, SQS may already have handed the same sha256 to another
    worker, and two workers running the DELETE/INSERT over ws13_chunks for
    one document is exactly the corruption the lease exists to prevent.
    """


class DocumentLease:
    """One document's wall-clock budget, SQS lease and proof-of-life.

    * budget: `deadline` is fixed at receive time and every phase clamps its
      own timeout to what is left, so the document cannot outrun
      DOC_BUDGET_SECONDS however slow one container is.
    * lease: a daemon thread re-extends the message's visibility to
      LEASE_SECONDS every LEASE_TICK_SECONDS. The lease is only declared
      lost when it can no longer be proven held -- a single failed tick is a
      transient SQS error, not a reason to abandon 40 minutes of OCR.
    * liveness: heartbeat() stamps ws13_manifest.updated_at at most every
      HEARTBEAT_SECONDS, which is what ws13_reap_stale.py ages.

    The thread only touches SQS; the manifest UPDATE stays on the main
    thread's connection, because issuing it from a second thread would land
    inside whatever transaction process() has open.
    """

    def __init__(self, receipt_handle, sha, conn, budget=None):
        self.started = time.time()
        self.deadline = self.started + (budget or DOC_BUDGET_SECONDS)
        self.sha = sha
        self.lost = None
        self._handle = receipt_handle
        self._conn = conn
        self._extended_at = self.started
        self._beat_at = 0.0
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._renew, daemon=True,
                                        name=f'lease-{self.sha[:12]}')
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        return False

    def remaining(self, reserve=0):
        return self.deadline - reserve - time.time()

    def allows(self, seconds, reserve=INDEX_RESERVE_SECONDS):
        """Is there room for an optional phase of roughly `seconds`?"""
        return self.remaining(reserve) >= seconds

    def container_timeout(self, cap, reserve=0):
        """Never let one container outlive the document's budget."""
        return max(60, int(min(cap, max(0.0, self.remaining(reserve)))))

    def check(self):
        if self.lost:
            raise LeaseLost(self.lost)

    def heartbeat(self, force=False):
        """Prove the row is alive, rate-limited to HEARTBEAT_SECONDS."""
        self.check()
        now = time.time()
        if not force and now - self._beat_at < HEARTBEAT_SECONDS:
            return
        self._beat_at = now
        # Outside any transaction block by construction: every call site is
        # in the OCR/confidence/embed phases, before the indexing
        # transaction opens.
        self._conn.execute(
            "UPDATE ws13_manifest SET updated_at=now() "
            "WHERE sha256=%s AND status='running'", (self.sha,))
        self._conn.commit()

    def _renew(self):
        while not self._stop.wait(LEASE_TICK_SECONDS):
            try:
                sqs.change_message_visibility(
                    QueueUrl=QUEUE_URL, ReceiptHandle=self._handle,
                    VisibilityTimeout=LEASE_SECONDS)
                self._extended_at = time.time()
            except Exception as exc:
                stale = time.time() - self._extended_at
                # Only fatal once the message could actually have gone back
                # on the queue: the last good extension bought LEASE_SECONDS,
                # and two ticks of margin are kept before giving up.
                if stale > LEASE_SECONDS - 2 * LEASE_TICK_SECONDS:
                    self.lost = (f'{type(exc).__name__} for {stale:.0f}s '
                                 f'(lease was {LEASE_SECONDS}s)')
                    log(f'{self.sha[:12]} LEASE LOST: {self.lost}')
                    return
                log(f'{self.sha[:12]} visibility extension failed '
                    f'({type(exc).__name__}); {stale:.0f}s since the last '
                    f'good one, retrying')


def process(conn, msg, lease):
    body = json.loads(msg['Body'])
    sha, key, cls = body['sha256'], body['key'], body['cls']
    meta = body.get('meta') or {}
    row = manifest_row(conn, sha)
    if row and row[0] == 'done' and not body.get('force'):
        return 'skip_done'
    set_status(conn, sha, 'running', s3_key=key, doc_class=cls)
    started = time.time()
    with tempfile.TemporaryDirectory(dir=SCRATCH) as work:
        os.chmod(work, 0o777)
        raw = s3.get_object(Bucket=BUCKET, Key=key)['Body'].read()
        if hashlib.sha256(raw).hexdigest() != sha:
            set_status(conn, sha, 'error', error='integrity_mismatch')
            return 'error'
        with open(os.path.join(work, 'in.pdf'), 'wb') as f:
            f.write(raw)
        os.chmod(os.path.join(work, 'in.pdf'), 0o644)

        confs, escalated_pages, low_pages = [], 0, 0
        conf_reason = None
        skipped = []
        searchable_key = None
        if cls == 'ocr_queue':
            secs, code, err = ocr(work, 'in.pdf', 'out.pdf', 'out.txt',
                                  timeout=lease.container_timeout(
                                      MAX_DOC_SECONDS))
            if code != 0 or not os.path.exists(os.path.join(work, 'out.pdf')):
                # err is already stderr[-1500:]; take its END, not its start.
                # Slicing [:400] kept the head of the tail -- the middle of a
                # traceback -- and discarded the exception line that actually
                # names the failure, so every OCR error in the corpus was
                # recorded as an unreadable source-code fragment.
                set_status(conn, sha, 'error',
                           error=f'ocr_exit_{code}:{err[-700:]}')
                return 'error'
            pages = page_texts_from_sidecar(os.path.join(work, 'out.txt'))
            lease.heartbeat(force=True)
            # Both passes measure the SAME page set, so the escalation
            # comparison is like for like and the second pass cannot cost
            # more than the first.
            sample = sample_indices(len(pages))
            if lease.allows(CONF_MIN_SECONDS):
                confs, conf_reason = page_confidences(work, 'out.pdf',
                                                      len(pages), sample,
                                                      lease)
            else:
                confs, conf_reason = [None] * len(pages), 'skipped:doc_budget'
            measured = sum(1 for c in confs if c is not None)
            weak = [i for i, c in enumerate(confs) if c is not None and c < CONF_THRESHOLD]
            # Minority of the MEASURED pages, not of the whole document: with
            # a sampled measurement len(pages)//4 is the wrong denominator.
            if weak and len(weak) <= max(3, measured // 4):
                # tier-1 escalation: re-OCR whole doc at stronger settings,
                # adopt only if the weak pages improve. Strong settings cost
                # at least what the first pass did, so it is not started
                # unless that much budget is left; skipping is recorded, not
                # silent.
                if not lease.allows(max(600.0, secs)):
                    skipped.append('escalation_skipped:doc_budget')
                    log(f'{sha[:12]} tier-1 escalation skipped: '
                        f'{lease.remaining(INDEX_RESERVE_SECONDS):.0f}s of '
                        f'budget left, first pass took {secs:.0f}s')
                else:
                    secs2, code2, _ = ocr(
                        work, 'in.pdf', 'out2.pdf', 'out2.txt',
                        timeout=lease.container_timeout(
                            MAX_DOC_SECONDS, INDEX_RESERVE_SECONDS),
                        strong=True)
                    out2 = os.path.join(work, 'out2.pdf')
                    if code2 == 0 and os.path.exists(out2):
                        pages2 = page_texts_from_sidecar(
                            os.path.join(work, 'out2.txt'))
                        confs2, reason2 = page_confidences(
                            work, 'out2.pdf', len(pages2), sample, lease)
                        better = sum(1 for i in weak if i < len(confs2) and
                                     (confs2[i] or 0) > (confs[i] or 0))
                        if better * 2 >= len(weak):
                            os.replace(os.path.join(work, 'out2.pdf'),
                                       os.path.join(work, 'out.pdf'))
                            pages, confs = pages2, confs2
                            conf_reason = reason2
                            measured = sum(1 for c in confs if c is not None)
                            escalated_pages = len(weak)
            # NULL, not 0, whenever the measurement was not a full census: 0
            # would assert that every page cleared the threshold, which is
            # exactly the false reading the pdftoppm defect has been
            # producing. The weak count that WAS measured is not thrown away,
            # it rides in the note below with its denominator.
            weak_measured = sum(
                1 for c in confs if c is not None and c < ESCALATE_THRESHOLD)
            low_pages = None if conf_reason else weak_measured
            if conf_reason and measured:
                skipped.append(f'weak={weak_measured}/{measured}')
            searchable_key = f'ws13/searchable/{sha[:2]}/{sha}/searchable.pdf'
            with open(os.path.join(work, 'out.pdf'), 'rb') as f:
                s3.put_object(Bucket=BUCKET, Key=searchable_key, Body=f,
                              ContentType='application/pdf',
                              Metadata={'sha256-raw': sha, 'variant': 'searchable'})
            with open(os.path.join(work, 'out.txt'), 'rb') as f:
                s3.put_object(Bucket=BUCKET,
                              Key=f'ws13/searchable/{sha[:2]}/{sha}/sidecar.txt',
                              Body=f, ContentType='text/plain')
        elif cls == 'born_digital':
            # pypdf in-process: the OCR container does not expose pdftotext,
            # and a docker dependency is pointless for text-layer extraction.
            from pypdf import PdfReader
            reader = PdfReader(os.path.join(work, 'in.pdf'), strict=False)
            pages = []
            for pg in reader.pages:
                # Rate-limited internally, so per page costs a comparison.
                # Extraction of a several-hundred-page document is minutes.
                lease.heartbeat()
                try:
                    pages.append(clean_text(pg.extract_text() or ''))
                except Exception:
                    pages.append('')
            if not any(p.strip() for p in pages):
                set_status(conn, sha, 'error',
                           error='born_digital_no_extractable_text')
                return 'error'
            sidecar_text = '\f'.join(pages)
            s3.put_object(Bucket=BUCKET,
                          Key=f'ws13/searchable/{sha[:2]}/{sha}/sidecar.txt',
                          Body=sidecar_text.encode('utf-8', 'replace'),
                          ContentType='text/plain')
        else:
            set_status(conn, sha, 'error', error=f'unsupported_class:{cls}')
            return 'error'

        chunks = chunk_pages(pages)
        lease.heartbeat(force=True)
        if EMBED_MODE == 'defer':
            vectors = [None] * len(chunks)
        else:
            vectors = embed([c['text'] for c in chunks], lease) if chunks else []
            if len(vectors) != len(chunks):
                set_status(conn, sha, 'error', error='embed_count_mismatch')
                return 'error'

        # Last checkpoint before the write. If the visibility lease lapsed
        # while this document was being worked, another worker may already
        # own the sha256; running the DELETE/INSERT anyway is how both end up
        # writing ws13_chunks for it.
        lease.check()
        with conn.transaction():
            # Re-extraction rewrites this document's chunks. Carry any vectors
            # already computed for identical text across the delete, keyed on
            # the text itself: re-chunking can shift page/ordinal, but a chunk
            # whose text is unchanged has an unchanged embedding. Without this
            # a re-extraction silently resets every model's coverage for the
            # document and the backfill pays to recompute it. CREATE TABLE AS
            # inherits the source column types, so no cast is needed and this
            # stays correct whether the columns are vector, jsonb, or text.
            conn.execute(
                '''CREATE TEMP TABLE ws13_prev_vecs ON COMMIT DROP AS
                   SELECT DISTINCT ON (md5(text)) md5(text) AS h,
                          embedding, titan_embedding, qwen_embedding
                     FROM ws13_chunks
                    WHERE sha256=%s AND text IS NOT NULL
                    ORDER BY md5(text), id''', (sha,))
            conn.execute('DELETE FROM ws13_chunks WHERE sha256=%s', (sha,))
            conn.execute('DELETE FROM ws13_pages WHERE sha256=%s', (sha,))
            # Provenance travels with the document: without source_url a
            # citation cannot resolve, and without rights_basis /
            # public_domain the licence attached to the 13,013 CC BY-NC-SA
            # copies and the 32,312 research copies cannot be honoured at
            # serve time. The three are COALESCEd on conflict because a
            # requeue may carry no metadata at all -- ws13_enqueue.py rebuilds
            # meta from this very table, and a row written before these
            # columns existed has none -- and a reprocess must never blank a
            # good value. admission_class is generated from s3_key; it is
            # never written here.
            conn.execute(
                '''INSERT INTO ws13_documents (
                       sha256, s3_key, searchable_key, doc_class, portal,
                       state, mine_ids, mine_names, county, trs, doc_date,
                       doc_type, title, pages, source_url, rights_basis,
                       public_domain, processed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,now())
                   ON CONFLICT (sha256) DO UPDATE SET
                     searchable_key=EXCLUDED.searchable_key,
                     pages=EXCLUDED.pages, processed_at=now(),
                     mine_ids=EXCLUDED.mine_ids,
                     mine_names=EXCLUDED.mine_names,
                     source_url=COALESCE(
                       EXCLUDED.source_url, ws13_documents.source_url),
                     rights_basis=COALESCE(
                       EXCLUDED.rights_basis, ws13_documents.rights_basis),
                     public_domain=COALESCE(
                       EXCLUDED.public_domain, ws13_documents.public_domain)''',
                (sha, key, searchable_key, cls, meta.get('portal'), meta.get('state'),
                 meta.get('mine_ids') or [], meta.get('mine_names') or [],
                 meta.get('county'), meta.get('trs'), meta.get('doc_date'),
                 meta.get('doc_type'), meta.get('title'), len(pages),
                 meta.get('source_url'), meta.get('rights_basis'),
                 meta.get('public_domain')))
            for i, text in enumerate(pages, 1):
                conf = confs[i - 1] if i - 1 < len(confs) else None
                # NULL, not false, for an unmeasured page. `conf is not None
                # and ...` evaluated to false, so a page nobody could measure
                # was stored as "measured, and not weak" -- the same claim
                # low_conf_pages above refuses to make, one level down, and
                # ws13_quality_proxy.py counts `FILTER (WHERE low_confidence)`
                # as the weak total over exactly these rows.
                low = None if conf is None else conf < ESCALATE_THRESHOLD
                conn.execute(
                    '''INSERT INTO ws13_pages (sha256, page, confidence, chars,
                       low_confidence) VALUES (%s,%s,%s,%s,%s)''',
                    (sha, i, conf, len(text or ''), low))
            for c, v in zip(chunks, vectors):
                conn.execute(
                    '''INSERT INTO ws13_chunks (sha256, page, ordinal, start_char,
                       end_char, text, tsv, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s, to_tsvector('english', %s), %s)''',
                    (sha, c['page'], c['ordinal'], c['start'], c['end'],
                     c['text'], c['text'],
                     json.dumps(v) if v is not None else None))
            # Restore the carried-over vectors. COALESCE so a freshly computed
            # embedding always wins over the historical one.
            restored = conn.execute(
                '''UPDATE ws13_chunks c
                      SET embedding       = COALESCE(c.embedding, p.embedding),
                          titan_embedding = COALESCE(c.titan_embedding, p.titan_embedding),
                          qwen_embedding  = COALESCE(c.qwen_embedding, p.qwen_embedding)
                     FROM ws13_prev_vecs p
                    WHERE c.sha256=%s AND md5(c.text) = p.h
                      AND (c.embedding IS NULL OR c.titan_embedding IS NULL
                           OR c.qwen_embedding IS NULL)''', (sha,)).rowcount
            if restored:
                log(f'{sha[:12]} carried vectors across re-extraction for '
                    f'{restored}/{len(chunks)} chunks')
        # ws13_manifest has exactly one free-text column, so the reason rides
        # in `error` on an otherwise successful row. status stays 'done', and
        # ws13_enqueue.py selects on status, so this can never cause a requeue
        # loop -- it only makes the gap greppable
        # (WHERE status='done' AND error LIKE 'conf_unavailable:%'). The
        # conf_unavailable: prefix stays first so that grep keeps working;
        # anything else this document had to skip for budget follows it.
        notes = ([f'conf_unavailable:{conf_reason}'] if conf_reason else [])
        conf_note = ';'.join(notes + skipped) or None
        set_status(conn, sha, 'done', embed_pending=(EMBED_MODE == 'defer'),
                   pages=len(pages), chunks=len(chunks),
                   low_conf_pages=low_pages, escalated_pages=escalated_pages,
                   seconds=round(time.time() - started, 1), error=conf_note)
        return 'done'


def main():
    os.makedirs(SCRATCH, exist_ok=True)
    # born_digital is pure pypdf and needs no container. Only ocr_queue does,
    # so a node without docker is still a useful born_digital worker rather
    # than a startup crash.
    have_docker = shutil.which('docker') is not None
    if have_docker:
        subprocess.run(['docker', 'pull', OCR_IMAGE], capture_output=True,
                       timeout=1800)
        # Probe now rather than on the first ocr_queue document, so a node
        # that cannot measure confidences says so in its first log lines
        # instead of quietly writing 760,043 more NULL confidences.
        page_renderer()
    else:
        log('docker unavailable: this worker handles born_digital only and '
            'will release ocr_queue messages for a capable node')
    conn = psycopg.connect(DB_DSN, autocommit=False)
    idle = 0
    released = 0
    while True:
        if os.path.exists(DRAIN_FILE):
            # The node agent saw the Auto Scaling group ask for this node
            # back. Exit between documents so the in-flight one is never
            # killed half-written: a killed document goes back to SQS with
            # ReceiveCount incremented, and three of those dead-letter it.
            log(f'drain requested ({DRAIN_FILE}); exiting between documents')
            return 0
        r = sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=1,
                                WaitTimeSeconds=20,
                                VisibilityTimeout=LEASE_SECONDS)
        msgs = r.get('Messages', [])
        if not msgs:
            idle += 1
            if idle >= 6:
                log('queue drained; exiting')
                return 0
            continue
        idle = 0
        msg = msgs[0]
        sha = json.loads(msg['Body']).get('sha256', '?')
        if not have_docker and json.loads(msg['Body']).get('cls') != 'born_digital':
            # Not this node's work. Return it immediately rather than failing
            # a perfectly good document for a local capability gap.
            sqs.change_message_visibility(QueueUrl=QUEUE_URL,
                                          ReceiptHandle=msg['ReceiptHandle'],
                                          VisibilityTimeout=0)
            released += 1
            if released >= 25:
                log(f'released {released} messages needing docker; nothing '
                    f'here to do. Exiting.')
                return 0
            continue
        try:
            with DocumentLease(msg['ReceiptHandle'], sha, conn) as lease:
                outcome = process(conn, msg, lease)
            log(f'{sha[:12]} {outcome}')
            if outcome in ('done', 'skip_done', 'error'):
                sqs.delete_message(QueueUrl=QUEUE_URL,
                                   ReceiptHandle=msg['ReceiptHandle'])
        except LeaseLost as exc:
            # Deliberately no set_status: the message is back on the queue
            # and another worker may already have stamped 'running' on this
            # sha256. Writing 'error' here would overwrite a document that is
            # being processed correctly right now.
            conn.rollback()
            log(f'{sha[:12]} ABANDONED after losing the SQS lease ({exc}); '
                f'the manifest row is left to whoever holds the message')
        except Exception as exc:
            conn.rollback()
            reason = f'{type(exc).__name__}: {exc}'[:400]
            log(f'{sha[:12]} EXCEPTION {reason}')
            try:
                set_status(conn, sha, 'error', error=reason)
            except Exception:
                conn.rollback()
            # leave message for SQS retry -> DLQ after maxReceiveCount


if __name__ == '__main__':
    raise SystemExit(main())
