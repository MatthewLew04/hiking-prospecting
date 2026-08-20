#!/usr/bin/env python3
"""WS13 embedding backfill: Titan V2 base fill + Cohere v4 overlay trickle.

Titan threads saturate a client-side token bucket (~280k tokens/min, under
the non-adjustable 300k/min account cap) filling titan_embedding (1024-d)
for every chunk. One Cohere thread trickles the 1536-d overlay into the
`embedding` column within a daily token budget (the non-adjustable
16.2M/day cap), resuming each UTC midnight. Both are resumable by
construction (NULL-column scans); the process exits when Titan coverage is
complete AND Cohere coverage is complete, else sleeps and continues.
"""
import datetime as dt, json, os, threading, time
import boto3, psycopg

REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')
DSN = os.environ['WS13_DB_DSN']
BUCKET = os.environ['WS13_BUCKET']
TITAN = 'amazon.titan-embed-text-v2:0'
COHERE = 'us.cohere.embed-v4:0'
TITAN_TPM = 190_000
COHERE_DAILY = 15_500_000
TITAN_THREADS = 12

bedrock = boto3.client('bedrock-runtime', region_name=REGION)
s3 = boto3.client('s3', region_name=REGION)
lock = threading.Lock()
bucket_tokens = TITAN_TPM
bucket_ts = time.time()
stats = {'titan': 0, 'cohere': 0, 'titan_throttle': 0, 'cohere_throttle': 0}


def take_tokens(n):
    global bucket_tokens, bucket_ts
    while True:
        with lock:
            now = time.time()
            bucket_tokens = min(TITAN_TPM, bucket_tokens + (now - bucket_ts) * TITAN_TPM / 60)
            bucket_ts = now
            if bucket_tokens >= n:
                bucket_tokens -= n
                return
        time.sleep(0.25)


def titan_worker():
    conn = psycopg.connect(DSN, autocommit=True)
    while True:
        rows = conn.execute(
            "SELECT id, text FROM ws13_chunks WHERE titan_embedding IS NULL "
            "ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED").fetchall()
        if not rows:
            return
        for cid, text in rows:
            tokens = max(1, len(text) // 3)
            take_tokens(tokens)
            for attempt in range(8):
                try:
                    r = bedrock.invoke_model(modelId=TITAN, body=json.dumps(
                        {'inputText': text[:8000], 'dimensions': 1024,
                         'normalize': True}))
                    v = json.loads(r['body'].read())['embedding']
                    conn.execute(
                        'UPDATE ws13_chunks SET titan_embedding=%s WHERE id=%s',
                        (json.dumps(v), cid))
                    with lock:
                        stats['titan'] += 1
                    break
                except Exception:
                    with lock:
                        stats['titan_throttle'] += 1
                    time.sleep(min(60, 2 ** attempt))


def cohere_worker():
    conn = psycopg.connect(DSN, autocommit=True)
    day = dt.datetime.now(dt.timezone.utc).date()
    spent = 0
    while True:
        today = dt.datetime.now(dt.timezone.utc).date()
        if today != day:
            day, spent = today, 0
        if spent >= COHERE_DAILY:
            time.sleep(600)
            continue
        rows = conn.execute(
            "SELECT id, text FROM ws13_chunks WHERE embedding IS NULL "
            "ORDER BY id LIMIT 40 FOR UPDATE SKIP LOCKED").fetchall()
        if not rows:
            return
        texts = [t[:6000] for _, t in rows]
        spent += sum(max(1, len(t) // 3) for t in texts)
        try:
            r = bedrock.invoke_model(modelId=COHERE, body=json.dumps(
                {'texts': texts, 'input_type': 'search_document',
                 'embedding_types': ['float'], 'truncate': 'END'}))
            vecs = json.loads(r['body'].read())['embeddings']['float']
            for (cid, _), v in zip(rows, vecs):
                conn.execute('UPDATE ws13_chunks SET embedding=%s WHERE id=%s',
                             (json.dumps(v), cid))
            with lock:
                stats['cohere'] += len(rows)
        except Exception as exc:
            with lock:
                stats['cohere_throttle'] += 1
            if 'Throttling' in type(exc).__name__ or 'per day' in str(exc):
                time.sleep(1800)
            else:
                time.sleep(30)


def heartbeat():
    conn = psycopg.connect(DSN, autocommit=True)
    while True:
        total, titan_left, cohere_left = conn.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE titan_embedding IS NULL),"
            " COUNT(*) FILTER (WHERE embedding IS NULL) FROM ws13_chunks").fetchone()
        payload = {'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'chunks_total': total, 'titan_remaining': titan_left,
                   'cohere_remaining': cohere_left, **stats}
        s3.put_object(Bucket=BUCKET, Key='ws13/embed/status.json',
                      Body=json.dumps(payload).encode(),
                      ContentType='application/json')
        if titan_left == 0 and cohere_left == 0:
            return
        time.sleep(300)


def main():
    threads = [threading.Thread(target=titan_worker, daemon=True)
               for _ in range(TITAN_THREADS)]
    threads.append(threading.Thread(target=cohere_worker, daemon=True))
    for t in threads:
        t.start()
    heartbeat()
    print('backfill complete')


if __name__ == '__main__':
    raise SystemExit(main())
