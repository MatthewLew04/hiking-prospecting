#!/usr/bin/env python3
"""WS13 Qwen3-Embedding-8B overlay: best-in-class vectors via local TEI.

Runs on a GPU node beside a text-embeddings-inference container serving
Qwen/Qwen3-Embedding-8B. Fills ws13_chunks.qwen_embedding (1536-d Matryoshka
truncation, L2-renormalized) for every chunk, in plain id order, resumable
via NULL scans. Exits when coverage is complete. Heartbeats to
ws13/embed/qwen_status.json.

Multiple GPU nodes cooperate by disjoint id sharding: set WS13_SHARD_COUNT to
the number of nodes and WS13_SHARD to this node's index. The previous
`FOR UPDATE SKIP LOCKED` claim did not partition anything -- the connection is
autocommit, so each SELECT committed and dropped its row locks before the
embed call, and every node re-embedded the same head of the NULL set.
"""
import datetime as dt, json, math, os, time, urllib.request
import boto3, psycopg

DSN = os.environ['WS13_DB_DSN']
BUCKET = os.environ['WS13_BUCKET']
TEI = os.environ.get('WS13_TEI_URL', 'http://127.0.0.1:8080')
SHARD = int(os.environ.get('WS13_SHARD', '0'))
SHARD_COUNT = int(os.environ.get('WS13_SHARD_COUNT', '1'))
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
        # fp16 overflow can surface as None/NaN components; sanitize rather
        # than crash, and mark fully-poisoned vectors as unusable (None).
        t = [x if isinstance(x, (int, float)) and x == x else 0.0
             for x in (v or [])[:DIMS]]
        norm = math.sqrt(sum(x * x for x in t))
        out.append([x / norm for x in t] if norm > 0 else None)
    return out


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ws13_embed_skips (
             chunk_id BIGINT, model TEXT, reason TEXT,
             PRIMARY KEY (chunk_id, model))""")
    done = 0
    last_hb = 0.0
    cursor, rewound = -1, True
    while True:
        # id %% SHARD_COUNT partitions the work across nodes with no locking,
        # and the cursor stops every batch re-walking the filled prefix.
        # NOT EXISTS rather than NOT IN: the anti-join stays cheap as the
        # skips table grows, and it is NULL-safe.
        rows = conn.execute(
            "SELECT id, text FROM ws13_chunks c WHERE qwen_embedding IS NULL "
            "AND id > %s AND (id %% %s) = %s "
            "AND NOT EXISTS (SELECT 1 FROM ws13_embed_skips s "
            "                 WHERE s.chunk_id = c.id AND s.model = 'qwen3-8b-fp16') "
            "ORDER BY id LIMIT %s",
            (cursor, SHARD_COUNT, SHARD, BATCH)).fetchall()
        if not rows:
            if rewound:
                break           # a full sweep of this shard found nothing
            cursor, rewound = -1, True
            continue
        rewound = False
        for attempt in range(6):
            try:
                vecs = embed([t for _, t in rows])
                break
            except Exception as exc:
                if attempt == 5:
                    raise
                time.sleep(min(60, 3 * 2 ** attempt))
        poisoned = 0
        for (cid, _), v in zip(rows, vecs):
            if v is None:
                # leave NULL for a float32 retry pass; tag via ws13_pages? No:
                # record in a side table so the gap is accounted, not silent.
                conn.execute(
                    """INSERT INTO ws13_embed_skips (chunk_id, model, reason)
                       VALUES (%s, 'qwen3-8b-fp16', 'nan_vector')
                       ON CONFLICT DO NOTHING""", (cid,))
                poisoned += 1
                continue
            conn.execute('UPDATE ws13_chunks SET qwen_embedding=%s WHERE id=%s',
                         (json.dumps(v), cid))
        done += len(rows) - poisoned
        cursor = rows[-1][0]
        if time.time() - last_hb > 300:
            last_hb = time.time()
            total, remaining = conn.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE qwen_embedding IS NULL) "
                "FROM ws13_chunks").fetchone()
            # Per-shard key: with SHARD_COUNT > 1 every node would otherwise
            # overwrite the same object and the fleet's progress would read as
            # whichever node wrote last.
            key = ('ws13/embed/qwen_status.json' if SHARD_COUNT == 1 else
                   f'ws13/embed/qwen_status.shard{SHARD}.json')
            s3.put_object(Bucket=BUCKET, Key=key,
                          Body=json.dumps({
                              'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
                              'chunks_total': total, 'qwen_remaining': remaining,
                              'shard': SHARD, 'shard_count': SHARD_COUNT,
                              'session_done': done}).encode(),
                          ContentType='application/json')
    print(f'qwen overlay complete for shard {SHARD}/{SHARD_COUNT} '
          f'({done} this session)')


if __name__ == '__main__':
    raise SystemExit(main())
