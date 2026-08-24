#!/usr/bin/env python3
"""WS13 embedding backfill: Titan V2 base fill + Cohere v4 overlay trickle.

Titan threads saturate a client-side token bucket (TITAN_TPM, under the
non-adjustable 300k/min account cap) filling titan_embedding (1024-d) for
every chunk. One Cohere thread trickles the 1536-d overlay into the
`embedding` column within a daily token budget (the non-adjustable
16.2M/day cap), resuming each UTC midnight.

Work is partitioned across the Titan threads by `id %% TITAN_THREADS`, so
their work sets are disjoint by construction and no row is ever embedded
twice. This replaces a `FOR UPDATE SKIP LOCKED` claim that was inert: the
connection is autocommit, so each SELECT is its own transaction and the row
locks were released before the Bedrock call, leaving all 12 threads working
the same head of the NULL set (measured in production at 5-6x duplicate
invocations against a rate-limited, billed API).

Each shard walks forward on a monotonic id cursor rather than re-running
`ORDER BY id LIMIT n` from the start of the table every batch, which made
each pass more expensive than the last. A shard stops when a full sweep
fills nothing -- not when the shard is empty -- so a permanently unfillable
row cannot pin a thread forever. Whatever a shard gives up on is left to
`titan_mopup`, a single-threaded final pass that also catches rows
ws13_worker inserted while the run was in flight and covers any shard whose
thread died early.

Rows that can never be filled are recorded in ws13_embed_skips keyed by
(chunk_id, model). Only terminal reasons are excluded from a later scan:
a transient throttle must not permanently abandon a row.

Both models are resumable by construction (NULL-column scans); the process
exits when Titan coverage is complete AND Cohere coverage is complete.
"""
import datetime as dt, json, os, sys, threading, time
import boto3, psycopg

REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')
DSN = os.environ['WS13_DB_DSN']
BUCKET = os.environ['WS13_BUCKET']
TITAN = 'amazon.titan-embed-text-v2:0'
COHERE = 'us.cohere.embed-v4:0'
# ws13_embed_skips.model tags. ws13_qwen_overlay writes 'qwen3-8b-fp16'.
TITAN_TAG = 'titan-embed-text-v2'
COHERE_TAG = 'cohere-embed-v4'
# Reasons that permanently disqualify a row. A throttle or an outage is not
# one of them: 'retries_exhausted' rows are re-admitted on the next run.
TERMINAL_REASONS = ('empty_text',)
TITAN_TPM = int(os.environ.get('WS13_TITAN_TPM', '190000'))
COHERE_DAILY = int(os.environ.get('WS13_COHERE_DAILY', '15500000'))
TITAN_THREADS = int(os.environ.get('WS13_TITAN_THREADS', '12'))
TITAN_BATCH = 50
COHERE_BATCH = 40
STATUS_KEY = 'ws13/embed/status.json'
# The daily Cohere allowance is an account cap, not a per-process one, so it
# has to survive a restart: a process that starts at midday with spent=0 will
# re-spend the whole budget and push the account past the 16.2M/day ceiling.
BUDGET_KEY = 'ws13/embed/cohere_budget.json'
# Cap the reservoir well below the per-minute rate: a full bucket is spendable
# instantly, and a bucket sized at the whole minute's allowance let real usage
# reach 1.42x the intended ceiling over a two-minute window. The long-run rate
# is set by the refill, not the capacity, so this costs no throughput.
BUCKET_CAP = max(1, TITAN_TPM // 4)

bedrock = boto3.client('bedrock-runtime', region_name=REGION)
s3 = boto3.client('s3', region_name=REGION)
lock = threading.Lock()
bucket_tokens = float(BUCKET_CAP)
bucket_ts = time.time()
# Titan bills real tokens, not len(text)//3. Correct the reservation from the
# inputTextTokenCount the service returns. Deliberately one-sided: the client
# bucket may under-spend the account cap, never over-spend it.
token_ratio = 1.0
stats = {'titan': 0, 'cohere': 0, 'titan_throttle': 0, 'cohere_throttle': 0,
         'titan_error': 0, 'cohere_error': 0, 'titan_tokens_real': 0,
         'titan_tokens_est': 0, 'cohere_tokens_spent': 0}


def log(msg):
    print(f'{dt.datetime.now(dt.timezone.utc).isoformat()} {msg}', flush=True)


def estimate_tokens(text):
    """Pre-call reservation, corrected by the observed real/estimated ratio."""
    with lock:
        ratio = token_ratio
    return max(1, min(BUCKET_CAP, int((len(text) // 3) * ratio)))


def take_tokens(n):
    """Block until n tokens are available. n is clamped to BUCKET_CAP so an
    oversized row can never deadlock a thread against a bucket that will
    never hold enough."""
    global bucket_tokens, bucket_ts
    n = min(n, BUCKET_CAP)
    while True:
        with lock:
            now = time.time()
            bucket_tokens = min(BUCKET_CAP,
                                bucket_tokens + (now - bucket_ts) * TITAN_TPM / 60)
            bucket_ts = now
            if bucket_tokens >= n:
                bucket_tokens -= n
                return
            deficit = (n - bucket_tokens) * 60 / TITAN_TPM
        time.sleep(min(1.0, max(0.05, deficit)))


def settle_tokens(estimated, real):
    """Debit the shortfall after the fact and fold the observation into the
    estimator, so persistent under-estimation cannot silently overrun the cap."""
    global bucket_tokens, token_ratio
    if not real:
        return
    with lock:
        bucket_tokens -= max(0, real - estimated)
        stats['titan_tokens_real'] += real
        stats['titan_tokens_est'] += estimated
        seen = stats['titan_tokens_est']
        if seen > 50_000:
            observed = stats['titan_tokens_real'] / seen
            token_ratio = max(1.0, token_ratio * 0.99 + observed * 0.01)


def note_skip(conn, cid, model, reason):
    conn.execute(
        """INSERT INTO ws13_embed_skips (chunk_id, model, reason)
           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""", (cid, model, reason))


def titan_embed_one(conn, cid, text):
    """Embed and store one chunk. Returns True on success.

    The token debit sits inside the retry loop: a retried call spends real
    quota, and metering only the first attempt understates usage."""
    for attempt in range(8):
        estimated = estimate_tokens(text)
        take_tokens(estimated)
        try:
            r = bedrock.invoke_model(modelId=TITAN, body=json.dumps(
                {'inputText': text[:8000], 'dimensions': 1024,
                 'normalize': True}))
            payload = json.loads(r['body'].read())
            settle_tokens(estimated, payload.get('inputTextTokenCount'))
            conn.execute(
                'UPDATE ws13_chunks SET titan_embedding=%s WHERE id=%s',
                (json.dumps(payload['embedding']), cid))
            with lock:
                stats['titan'] += 1
            return True
        except Exception as exc:
            throttled = 'Throttl' in type(exc).__name__ or 'Throttl' in str(exc)
            with lock:
                stats['titan_throttle' if throttled else 'titan_error'] += 1
            if attempt == 7:
                log(f'titan give-up id={cid}: {type(exc).__name__}: {exc}')
                return False
            time.sleep(min(60, 2 ** attempt))
    return False


def skips_clause(alias, model):
    """Exclude only rows this model has terminally given up on.

    Scoped by model because ws13_embed_skips is keyed (chunk_id, model) and
    is shared with the Qwen overlay -- an unscoped anti-join would let a
    Qwen fp16 overflow suppress a perfectly embeddable Titan row."""
    reasons = ', '.join(f"'{r}'" for r in TERMINAL_REASONS)
    return (f"AND NOT EXISTS (SELECT 1 FROM ws13_embed_skips s "
            f" WHERE s.chunk_id = {alias}.id AND s.model = '{model}' "
            f"   AND s.reason IN ({reasons})) ")


def titan_fetch(conn, shard, shards, cursor):
    """Rows this shard owns, strictly after `cursor`.

    `id %% shards = shard` partitions the table into disjoint sets, so no
    two threads can select the same row and no locking is required. The
    cursor keeps each batch O(batch) forward on the id index instead of
    re-walking the filled prefix of the table every time."""
    skips = skips_clause('c', TITAN_TAG)
    if shards <= 1:
        return conn.execute(
            'SELECT c.id, c.text FROM ws13_chunks c '
            'WHERE c.titan_embedding IS NULL AND c.id > %s ' + skips +
            'ORDER BY c.id LIMIT %s', (cursor, TITAN_BATCH)).fetchall()
    return conn.execute(
        'SELECT c.id, c.text FROM ws13_chunks c '
        'WHERE c.titan_embedding IS NULL AND c.id > %s AND (c.id %% %s) = %s '
        + skips + 'ORDER BY c.id LIMIT %s',
        (cursor, shards, shard, TITAN_BATCH)).fetchall()


def titan_worker(shard, shards):
    """Fill this shard, then stop.

    Termination is bounded by *progress*, not by emptiness: a sweep that
    fills nothing ends the thread and leaves the remainder to the mop-up.
    Exiting only on an empty shard would let a single permanently-failing
    row spin this loop -- and block the mop-up behind its join -- forever."""
    conn = psycopg.connect(DSN, autocommit=True)
    try:
        cursor, rewound, progress = -1, True, 0
        while True:
            rows = titan_fetch(conn, shard, shards, cursor)
            if not rows:
                if rewound or progress == 0:
                    return
                time.sleep(1)
                cursor, rewound, progress = -1, True, 0
                continue
            rewound = False
            for cid, text in rows:
                if text:
                    if titan_embed_one(conn, cid, text):
                        progress += 1
                else:
                    note_skip(conn, cid, TITAN_TAG, 'empty_text')
                    progress += 1
            cursor = rows[-1][0]
    finally:
        conn.close()


def titan_mopup():
    """Single-threaded final sweep.

    Catches rows ws13_worker inserted while the shards were running, rows a
    shard gave up on, and any shard whose thread died early. Single-threaded
    so the tail cannot reintroduce the duplication sharding exists to
    prevent. This is the documented give-up point: a row that fails all
    retries here is recorded and left for a later run."""
    conn = psycopg.connect(DSN, autocommit=True)
    filled = 0
    try:
        cursor, rewound, progress = -1, True, 0
        while True:
            rows = conn.execute(
                'SELECT c.id, c.text FROM ws13_chunks c '
                'WHERE c.titan_embedding IS NULL AND c.id > %s '
                + skips_clause('c', TITAN_TAG) +
                'ORDER BY c.id LIMIT %s', (cursor, TITAN_BATCH)).fetchall()
            if not rows:
                if rewound or progress == 0:
                    return filled
                time.sleep(1)
                cursor, rewound, progress = -1, True, 0
                continue
            rewound = False
            for cid, text in rows:
                if not text:
                    note_skip(conn, cid, TITAN_TAG, 'empty_text')
                    progress += 1
                elif titan_embed_one(conn, cid, text):
                    filled += 1
                    progress += 1
                else:
                    note_skip(conn, cid, TITAN_TAG, 'retries_exhausted')
            cursor = rows[-1][0]
    finally:
        conn.close()


def load_cohere_spent(day):
    """Today's Cohere spend so far, carried across process restarts."""
    try:
        rec = json.loads(s3.get_object(Bucket=BUCKET, Key=BUDGET_KEY)['Body'].read())
        if rec.get('date') == day.isoformat():
            return int(rec.get('spent', 0))
    except Exception:
        pass
    return 0


def save_cohere_spent(day, spent):
    try:
        s3.put_object(Bucket=BUCKET, Key=BUDGET_KEY, ContentType='application/json',
                      Body=json.dumps({'date': day.isoformat(),
                                       'spent': spent}).encode())
    except Exception as exc:
        log(f'cohere budget checkpoint failed: {exc}')


def cohere_worker():
    """Single Cohere thread, bounded by the account's daily token budget.

    Budget is charged on success only. Charging before the call meant a
    throttled or failed request burned quota that was never spent at the
    service, permanently shrinking an already-binding daily cap."""
    conn = psycopg.connect(DSN, autocommit=True)
    day = dt.datetime.now(dt.timezone.utc).date()
    spent = load_cohere_spent(day)
    if spent:
        log(f'cohere resuming with {spent} tokens already spent today')
    with lock:
        stats['cohere_tokens_spent'] = spent
    cursor, rewound, progress = -1, True, 0
    try:
        while True:
            today = dt.datetime.now(dt.timezone.utc).date()
            if today != day:
                day, spent = today, 0
                save_cohere_spent(day, 0)
                with lock:
                    stats['cohere_tokens_spent'] = 0
            if spent >= COHERE_DAILY:
                time.sleep(600)
                continue
            rows = conn.execute(
                'SELECT c.id, c.text FROM ws13_chunks c '
                'WHERE c.embedding IS NULL AND c.id > %s '
                + skips_clause('c', COHERE_TAG) +
                'ORDER BY c.id LIMIT %s', (cursor, COHERE_BATCH)).fetchall()
            if not rows:
                if rewound or progress == 0:
                    return
                time.sleep(1)
                cursor, rewound, progress = -1, True, 0
                continue
            rewound = False
            usable = [(cid, t) for cid, t in rows if t]
            for cid, t in rows:
                if not t:
                    note_skip(conn, cid, COHERE_TAG, 'empty_text')
                    progress += 1
            if not usable:
                cursor = rows[-1][0]
                continue
            texts = [t[:6000] for _, t in usable]
            cost = sum(max(1, len(t) // 3) for t in texts)
            try:
                r = bedrock.invoke_model(modelId=COHERE, body=json.dumps(
                    {'texts': texts, 'input_type': 'search_document',
                     'embedding_types': ['float'], 'truncate': 'END'}))
                vecs = json.loads(r['body'].read())['embeddings']['float']
                if len(vecs) != len(usable):
                    raise RuntimeError(
                        f'cohere returned {len(vecs)} vectors for {len(usable)} texts')
                for (cid, _), v in zip(usable, vecs):
                    conn.execute('UPDATE ws13_chunks SET embedding=%s WHERE id=%s',
                                 (json.dumps(v), cid))
                spent += cost
                progress += len(usable)
                save_cohere_spent(day, spent)
                with lock:
                    stats['cohere'] += len(usable)
                    stats['cohere_tokens_spent'] = spent
                cursor = rows[-1][0]
            except Exception as exc:
                throttled = ('Throttl' in type(exc).__name__
                             or 'Throttl' in str(exc) or 'per day' in str(exc))
                with lock:
                    stats['cohere_throttle' if throttled else 'cohere_error'] += 1
                # Cursor is deliberately not advanced: the batch is retried.
                time.sleep(1800 if throttled else 30)
    finally:
        conn.close()


def outstanding(conn):
    """Rows still owed a vector, excluding terminal skips."""
    return conn.execute(
        'SELECT COUNT(*) FILTER (WHERE c.titan_embedding IS NULL AND NOT EXISTS ('
        "  SELECT 1 FROM ws13_embed_skips s WHERE s.chunk_id=c.id"
        f"   AND s.model='{TITAN_TAG}' AND s.reason IN ('empty_text')),"
        '  COUNT(*) FILTER (WHERE c.embedding IS NULL AND NOT EXISTS ('
        "  SELECT 1 FROM ws13_embed_skips s2 WHERE s2.chunk_id=c.id"
        f"   AND s2.model='{COHERE_TAG}' AND s2.reason IN ('empty_text'))) "
        'FROM ws13_chunks c').fetchone()


def preflight():
    """Prove one Titan write lands before spending a fleet's worth of quota.

    No running process has ever written titan_embedding with this JSON-string
    form -- only `embedding` is proven. If the column rejects it, every row
    would fail after 8 billed invocations. One call up front turns that into
    an immediate, legible abort."""
    conn = psycopg.connect(DSN, autocommit=True)
    try:
        row = conn.execute(
            'SELECT c.id, c.text FROM ws13_chunks c '
            "WHERE c.titan_embedding IS NULL AND c.text <> '' "
            'ORDER BY c.id LIMIT 1').fetchone()
        if not row:
            log('preflight: no titan work outstanding')
            return True
        cid, text = row
        if not titan_embed_one(conn, cid, text):
            log(f'PREFLIGHT FAILED: could not embed+store chunk {cid}')
            return False
        stored = conn.execute(
            'SELECT titan_embedding IS NOT NULL FROM ws13_chunks WHERE id=%s',
            (cid,)).fetchone()[0]
        if not stored:
            log(f'PREFLIGHT FAILED: UPDATE reported success but chunk {cid} '
                'still has a NULL titan_embedding')
            return False
        log(f'preflight ok: chunk {cid} embedded and stored')
        return True
    finally:
        conn.close()


def heartbeat(stop, phase):
    conn = psycopg.connect(DSN, autocommit=True)
    try:
        while True:
            total, titan_left, cohere_left, qwen_left = conn.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE titan_embedding IS NULL),"
                " COUNT(*) FILTER (WHERE embedding IS NULL),"
                " COUNT(*) FILTER (WHERE qwen_embedding IS NULL)"
                " FROM ws13_chunks").fetchone()
            with lock:
                snapshot = dict(stats)
                ratio = token_ratio
            payload = {'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
                       'chunks_total': total, 'titan_remaining': titan_left,
                       'cohere_remaining': cohere_left, 'qwen_remaining': qwen_left,
                       'phase': phase[0], 'shards': TITAN_THREADS,
                       'token_ratio': round(ratio, 4), **snapshot}
            s3.put_object(Bucket=BUCKET, Key=STATUS_KEY,
                          Body=json.dumps(payload).encode(),
                          ContentType='application/json')
            if stop.wait(300):
                return
    finally:
        conn.close()


def main():
    # Shared with ws13_qwen_overlay; either process may create it first.
    setup = psycopg.connect(DSN, autocommit=True)
    setup.execute(
        """CREATE TABLE IF NOT EXISTS ws13_embed_skips (
             chunk_id BIGINT, model TEXT, reason TEXT,
             PRIMARY KEY (chunk_id, model))""")
    setup.close()

    if not preflight():
        return 2

    stop = threading.Event()
    phase = ['titan+cohere']
    hb = threading.Thread(target=heartbeat, args=(stop, phase), daemon=True)
    hb.start()

    cohere = threading.Thread(target=cohere_worker, daemon=True)
    cohere.start()
    titans = [threading.Thread(target=titan_worker, args=(i, TITAN_THREADS),
                               daemon=True)
              for i in range(TITAN_THREADS)]
    for t in titans:
        t.start()
    for t in titans:
        t.join()

    phase[0] = 'titan-mopup'
    log(f'shards done ({stats["titan"]} embedded); mopping up')
    titan_mopup()
    phase[0] = 'cohere-only'
    log(f'titan pass complete: {stats["titan"]} embedded, '
        f'{stats["titan_throttle"]} throttles, {stats["titan_error"]} errors')

    # Cohere is bounded by a non-adjustable 16.2M token/day account cap and
    # can run for weeks. ws13_worker keeps inserting chunks with a NULL
    # titan_embedding throughout, so sweep Titan again before claiming
    # completion -- the exit code is gated on real coverage, not on joins.
    cohere.join()
    phase[0] = 'final-mopup'
    late = titan_mopup()
    if late:
        log(f'final mop-up embedded {late} chunks inserted during the cohere pass')

    stop.set()
    hb.join(timeout=10)

    conn = psycopg.connect(DSN, autocommit=True)
    try:
        titan_left, cohere_left = outstanding(conn)
    finally:
        conn.close()
    log(f'backfill finished: titan={stats["titan"]} cohere={stats["cohere"]} '
        f'outstanding titan={titan_left} cohere={cohere_left}')
    return 0 if titan_left == 0 and cohere_left == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
