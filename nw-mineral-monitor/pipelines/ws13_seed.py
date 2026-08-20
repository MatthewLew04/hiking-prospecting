#!/usr/bin/env python3
"""WS13 schema init + SQS seeding from the Phase 0 inventory.

Creates the hybrid index schema (pgvector + tsvector) and enqueues one SQS
message per born_digital / ocr_queue file, carrying WS12 harvest-manifest
metadata (portal, state, mine ids/names, county, TRS, date, type, title).
map_plate files are recorded in ws13_manifest as 'map_queue' (image-index /
georef path, never text-OCR'd); error_queue files carry their inventory
reason. Already-'done' shas are not re-enqueued (rerun-safe).
"""
import argparse
import gzip
import json
import os
import sys

import boto3
import psycopg

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS ws13_manifest (
  sha256 TEXT PRIMARY KEY, status TEXT NOT NULL, s3_key TEXT, doc_class TEXT,
  pages INT, chunks INT, low_conf_pages INT, escalated_pages INT,
  seconds REAL, error TEXT, worker_id TEXT, updated_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS ws13_manifest_status ON ws13_manifest(status);
CREATE TABLE IF NOT EXISTS ws13_documents (
  sha256 TEXT PRIMARY KEY, s3_key TEXT NOT NULL, searchable_key TEXT,
  doc_class TEXT, portal TEXT, state TEXT, mine_ids TEXT[], mine_names TEXT[],
  county TEXT, trs TEXT, doc_date TEXT, doc_type TEXT, title TEXT,
  pages INT, processed_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS ws13_documents_state ON ws13_documents(state, portal);
CREATE INDEX IF NOT EXISTS ws13_documents_mines ON ws13_documents USING GIN (mine_ids);
CREATE TABLE IF NOT EXISTS ws13_pages (
  sha256 TEXT NOT NULL, page INT NOT NULL, confidence REAL, chars INT,
  low_confidence BOOL, PRIMARY KEY (sha256, page));
CREATE TABLE IF NOT EXISTS ws13_chunks (
  id BIGSERIAL PRIMARY KEY, sha256 TEXT NOT NULL, page INT NOT NULL,
  ordinal INT NOT NULL, start_char INT, end_char INT, text TEXT NOT NULL,
  tsv TSVECTOR, embedding VECTOR(1536), UNIQUE (sha256, page, ordinal));
CREATE INDEX IF NOT EXISTS ws13_chunks_tsv ON ws13_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS ws13_chunks_sha ON ws13_chunks (sha256, page);
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bucket', required=True)
    p.add_argument('--queue-url', required=True)
    p.add_argument('--dsn', required=True)
    p.add_argument('--inventory-key', default='ws13/inventory/inventory.jsonl.gz')
    p.add_argument('--harvest-manifest', default='var/ws12/manifest.jsonl')
    p.add_argument('--schema-only', action='store_true')
    args = p.parse_args()

    conn = psycopg.connect(args.dsn, autocommit=True)
    conn.execute(SCHEMA)
    print('schema ready')
    if args.schema_only:
        return 0

    s3 = boto3.client('s3')
    sqs = boto3.client('sqs')
    rows = [json.loads(l) for l in gzip.decompress(s3.get_object(
        Bucket=args.bucket, Key=args.inventory_key)['Body'].read()
        ).decode().splitlines() if l]
    print('inventory rows:', len(rows))

    meta = {}
    if os.path.exists(args.harvest_manifest):
        for line in open(args.harvest_manifest, encoding='utf-8'):
            r = json.loads(line)
            m = meta.setdefault(r['sha256'], {
                'portal': r['portal_id'], 'state': r['state'],
                'mine_ids': [], 'mine_names': [], 'county': r.get('county'),
                'trs': r.get('trs'), 'doc_date': r.get('doc_date'),
                'doc_type': r.get('doc_type'), 'title': r.get('document_title')})
            if r.get('mine_id') and r['mine_id'] not in m['mine_ids']:
                m['mine_ids'].append(r['mine_id'])
            if r.get('mine_name') and r['mine_name'] not in m['mine_names']:
                m['mine_names'].append(r['mine_name'])
        print('manifest shas with metadata:', len(meta))

    done = {r[0] for r in conn.execute(
        "SELECT sha256 FROM ws13_manifest WHERE status='done'")}
    queued = skipped_done = 0
    batch = []
    for r in rows:
        sha, cls = r['sha256'], r['cls']
        if not sha:
            continue
        if cls in ('map_plate', 'error_queue', 'non_document', 'duplicate'):
            status = {'map_plate': 'map_queue', 'error_queue': 'inventory_error',
                      'non_document': 'gis_intake', 'duplicate': 'duplicate'}[cls]
            conn.execute(
                '''INSERT INTO ws13_manifest (sha256, status, s3_key, doc_class,
                   pages, error, updated_at) VALUES (%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT (sha256) DO NOTHING''',
                (sha, status, r['key'], cls, r.get('pages'), r.get('reason')))
            continue
        if sha in done:
            skipped_done += 1
            continue
        body = {'sha256': sha, 'key': r['key'], 'cls': cls,
                'pages': r.get('pages'), 'meta': meta.get(sha, {})}
        batch.append({'Id': str(len(batch)), 'MessageBody': json.dumps(body)})
        if len(batch) == 10:
            sqs.send_message_batch(QueueUrl=args.queue_url, Entries=batch)
            queued += len(batch)
            batch = []
    if batch:
        sqs.send_message_batch(QueueUrl=args.queue_url, Entries=batch)
        queued += len(batch)
    print(f'queued {queued}, skipped already-done {skipped_done}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
