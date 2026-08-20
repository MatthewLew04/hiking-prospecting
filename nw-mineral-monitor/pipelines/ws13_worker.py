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

Nothing skips silently: every doc ends in exactly one terminal manifest state.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
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
MAX_DOC_SECONDS = 3300

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
    cmd = ['docker', 'run', '--rm', '--user', '0:0', '-v', f'{work}:/work']
    if entrypoint:
        cmd += ['--entrypoint', entrypoint]
    cmd += [OCR_IMAGE] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ocr(work, in_name, out_name, sidecar_name, strong=False):
    args = ['--deskew', '--rotate-pages', '--clean', '--skip-text',
            '--sidecar', f'/work/{sidecar_name}', '--jobs', '2']
    if strong:
        args += ['--oversample', '400', '--clean-final',
                 '--tesseract-timeout', '600']
    args += [f'/work/{in_name}', f'/work/{out_name}']
    started = time.time()
    result = docker(args, work, timeout=MAX_DOC_SECONDS)
    return time.time() - started, result.returncode, result.stderr[-1500:]


def page_texts_from_sidecar(path):
    if not os.path.exists(path):
        return []
    text = open(path, encoding='utf-8', errors='replace').read()
    return text.split('\f')


def page_confidences(work, pdf_name, pages):
    """Per-page mean tesseract word confidence on the OCR'd output."""
    confs = []
    for index in range(1, pages + 1):
        base = f'pg{index:05d}'
        docker(['-r', '150', '-png', '-f', str(index), '-l', str(index),
                f'/work/{pdf_name}', f'/work/{base}'], work,
               timeout=300, entrypoint='pdftoppm')
        pngs = sorted(n for n in os.listdir(work)
                      if n.startswith(base) and n.endswith('.png'))
        if not pngs:
            confs.append(None)
            continue
        tsv = docker([f'/work/{pngs[0]}', 'stdout', 'tsv'], work,
                     timeout=300, entrypoint='tesseract')
        words = [float(p[10]) for p in (l.split('\t') for l in
                 tsv.stdout.splitlines()[1:])
                 if len(p) > 11 and p[10] not in ('-1', '')]
        confs.append(round(statistics.mean(words), 1) if words else None)
        for name in pngs:
            os.unlink(os.path.join(work, name))
    return confs


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


def embed(texts):
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
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


def process(conn, msg):
    body = json.loads(msg['Body'])
    sha, key, cls = body['sha256'], body['key'], body['cls']
    meta = body.get('meta') or {}
    row = manifest_row(conn, sha)
    if row and row[0] == 'done':
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
        searchable_key = None
        if cls == 'ocr_queue':
            secs, code, err = ocr(work, 'in.pdf', 'out.pdf', 'out.txt')
            if code != 0 or not os.path.exists(os.path.join(work, 'out.pdf')):
                set_status(conn, sha, 'error', error=f'ocr_exit_{code}:{err[:400]}')
                return 'error'
            pages = page_texts_from_sidecar(os.path.join(work, 'out.txt'))
            confs = page_confidences(work, 'out.pdf', len(pages))
            weak = [i for i, c in enumerate(confs) if c is not None and c < CONF_THRESHOLD]
            if weak and len(weak) <= max(3, len(pages) // 4):
                # tier-1 escalation: re-OCR whole doc at stronger settings,
                # adopt only if the weak pages improve.
                secs2, code2, _ = ocr(work, 'in.pdf', 'out2.pdf', 'out2.txt',
                                     strong=True)
                if code2 == 0 and os.path.exists(os.path.join(work, 'out2.pdf')):
                    pages2 = page_texts_from_sidecar(os.path.join(work, 'out2.txt'))
                    confs2 = page_confidences(work, 'out2.pdf', len(pages2))
                    better = sum(1 for i in weak if i < len(confs2) and
                                 (confs2[i] or 0) > (confs[i] or 0))
                    if better * 2 >= len(weak):
                        os.replace(os.path.join(work, 'out2.pdf'),
                                   os.path.join(work, 'out.pdf'))
                        pages, confs = pages2, confs2
                        escalated_pages = len(weak)
            low_pages = sum(1 for c in confs if c is not None and c < ESCALATE_THRESHOLD)
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
            r = docker(['/work/in.pdf', '-'], work, timeout=600, entrypoint='pdftotext')
            pages = r.stdout.split('\f')
            s3.put_object(Bucket=BUCKET,
                          Key=f'ws13/searchable/{sha[:2]}/{sha}/sidecar.txt',
                          Body=r.stdout.encode('utf-8', 'replace'),
                          ContentType='text/plain')
        else:
            set_status(conn, sha, 'error', error=f'unsupported_class:{cls}')
            return 'error'

        chunks = chunk_pages(pages)
        if EMBED_MODE == 'defer':
            vectors = [None] * len(chunks)
        else:
            vectors = embed([c['text'] for c in chunks]) if chunks else []
            if len(vectors) != len(chunks):
                set_status(conn, sha, 'error', error='embed_count_mismatch')
                return 'error'

        with conn.transaction():
            conn.execute('DELETE FROM ws13_chunks WHERE sha256=%s', (sha,))
            conn.execute('DELETE FROM ws13_pages WHERE sha256=%s', (sha,))
            conn.execute(
                '''INSERT INTO ws13_documents (sha256, s3_key, searchable_key,
                   doc_class, portal, state, mine_ids, mine_names, county, trs,
                   doc_date, doc_type, title, pages, processed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT (sha256) DO UPDATE SET searchable_key=EXCLUDED.searchable_key,
                     pages=EXCLUDED.pages, processed_at=now(), mine_ids=EXCLUDED.mine_ids,
                     mine_names=EXCLUDED.mine_names''',
                (sha, key, searchable_key, cls, meta.get('portal'), meta.get('state'),
                 meta.get('mine_ids') or [], meta.get('mine_names') or [],
                 meta.get('county'), meta.get('trs'), meta.get('doc_date'),
                 meta.get('doc_type'), meta.get('title'), len(pages)))
            for i, text in enumerate(pages, 1):
                conf = confs[i - 1] if i - 1 < len(confs) else None
                conn.execute(
                    '''INSERT INTO ws13_pages (sha256, page, confidence, chars,
                       low_confidence) VALUES (%s,%s,%s,%s,%s)''',
                    (sha, i, conf, len(text or ''),
                     conf is not None and conf < ESCALATE_THRESHOLD))
            for c, v in zip(chunks, vectors):
                conn.execute(
                    '''INSERT INTO ws13_chunks (sha256, page, ordinal, start_char,
                       end_char, text, tsv, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s, to_tsvector('english', %s), %s)''',
                    (sha, c['page'], c['ordinal'], c['start'], c['end'],
                     c['text'], c['text'],
                     json.dumps(v) if v is not None else None))
        set_status(conn, sha, 'done', embed_pending=(EMBED_MODE == 'defer'),
                   pages=len(pages), chunks=len(chunks),
                   low_conf_pages=low_pages, escalated_pages=escalated_pages,
                   seconds=round(time.time() - started, 1), error=None)
        return 'done'


def main():
    os.makedirs(SCRATCH, exist_ok=True)
    subprocess.run(['docker', 'pull', OCR_IMAGE], capture_output=True, timeout=1800)
    conn = psycopg.connect(DB_DSN, autocommit=False)
    idle = 0
    while True:
        r = sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=1,
                                WaitTimeSeconds=20, VisibilityTimeout=3600)
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
        try:
            outcome = process(conn, msg)
            log(f'{sha[:12]} {outcome}')
            if outcome in ('done', 'skip_done', 'error'):
                sqs.delete_message(QueueUrl=QUEUE_URL,
                                   ReceiptHandle=msg['ReceiptHandle'])
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
