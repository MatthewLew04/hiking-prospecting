#!/usr/bin/env python3
"""WS13 Qwen3-Embedding-8B overlay: best-in-class vectors via local TEI.

Runs on a GPU node beside a text-embeddings-inference container serving
Qwen/Qwen3-Embedding-8B. Fills ws13_chunks.qwen_embedding (1536-d Matryoshka
truncation, L2-renormalized) for every chunk, worst... in plain id order,
resumable via NULL scans with SKIP LOCKED so multiple GPU nodes cooperate.
Exits when coverage is complete. Heartbeats to ws13/embed/qwen_status.json.
"""
import datetime as dt, json, math, os, time, urllib.request
import boto3, psycopg

DSN = os.environ['WS13_DB_DSN']
BUCKET = os.environ['WS13_BUCKET']
TEI = os.environ.get('WS13_TEI_URL', 'http://127.0.0.1:8080')
DIMS = 1536
BATCH = 32

s3 = boto3.client('s3', region_name=os.environ.get('AWS_DEFAULT_REGION', 'us-west-2'))


def embed(texts):
    req = urllib.request.Request(
        f'{TEI}/embed', method='POST',
        headers={'Content-Type': 'application/json'},
        data=json.dumps({'inputs': [t[:6000] for t in texts],
                         'truncate': True}).encode())
    with urllib.request.urlopen(req, timeout=300) as resp:
        vectors = json.loads(resp.read())
    out = []
    for v in vectors:
        t = v[:DIMS]
        norm = math.sqrt(sum(x * x for x in t)) or 1.0
        out.append([x / norm for x in t])
    return out


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    done = 0
    last_hb = 0.0
    while True:
        rows = conn.execute(
            "SELECT id, text FROM ws13_chunks WHERE qwen_embedding IS NULL "
            "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED", (BATCH,)).fetchall()
        if not rows:
            break
        for attempt in range(6):
            try:
                vecs = embed([t for _, t in rows])
                break
            except Exception as exc:
                if attempt == 5:
                    raise
                time.sleep(min(60, 3 * 2 ** attempt))
        for (cid, _), v in zip(rows, vecs):
            conn.execute('UPDATE ws13_chunks SET qwen_embedding=%s WHERE id=%s',
                         (json.dumps(v), cid))
        done += len(rows)
        if time.time() - last_hb > 300:
            last_hb = time.time()
            total, remaining = conn.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE qwen_embedding IS NULL) "
                "FROM ws13_chunks").fetchone()
            s3.put_object(Bucket=BUCKET, Key='ws13/embed/qwen_status.json',
                          Body=json.dumps({
                              'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
                              'chunks_total': total, 'qwen_remaining': remaining,
                              'session_done': done}).encode(),
                          ContentType='application/json')
    print(f'qwen overlay complete ({done} this session)')


if __name__ == '__main__':
    raise SystemExit(main())
