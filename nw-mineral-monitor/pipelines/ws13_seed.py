#!/usr/bin/env python3
"""WS13 schema init + SQS seeding from the Phase 0 inventory.

Creates the hybrid index schema (pgvector + tsvector) and enqueues one SQS
message per born_digital / ocr_queue file, carrying WS12 harvest-manifest
metadata (portal, state, mine ids/names, county, TRS, date, type, title,
source URL, rights basis, public-domain flag). map_plate files are recorded
in ws13_manifest as 'map_queue' (image-index / georef path, never text-OCR'd);
error_queue files carry their inventory reason. Already-'done' shas are not
re-enqueued (rerun-safe).

SCHEMA had drifted badly out of parity with the database it claims to
create, so a rebuild from this file would not have reproduced production:

  * ws13_chunks declared only `embedding VECTOR(1536)` -- the Cohere overlay
    column, which is still 593,649 rows short and is NOT the production
    vector. The column retrieval actually reads, titan_embedding VECTOR(1024)
    (852,027 rows, zero NULL), was missing entirely, as was
    qwen_embedding; ws13_embed_backfill.py's
    `UPDATE ws13_chunks SET titan_embedding=...` would have failed on every
    row of a freshly seeded database.
  * ws13_embed_skips, the (chunk_id, model) table that records why a row can
    never be embedded, was missing -- both the backfill and the Qwen overlay
    create it defensively at runtime, which hid the gap.
  * ws13_manifest had no embed_pending column, which ws13_worker.set_status
    writes on every deferred-embed document.
  * none of the retrieval/citation columns existed. Those now come from
    pipelines/ws13_migrations.sql, which this script executes rather than
    duplicates: two copies of that DDL would drift the same way this one did.
"""
import argparse
import gzip
import json
import os
import sys

import boto3
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
# The sibling modules are imported inside main(), after bundle_files() has
# checked they are actually there: a module-scope import fires before any
# error message can be printed, so a bundle missing ws13_migrate.py died with
# a bare ModuleNotFoundError on the seeding node instead of saying which file
# to add to ws13/fleet/bundle.tar.gz.
sys.path.insert(0, HERE)


# qwen_embedding is VECTOR(1536), not the model's native 4096: the overlay
# stores a Matryoshka truncation, L2-renormalized -- see DIMS in
# pipelines/ws13_qwen_overlay.py, which is the only writer.
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS ws13_manifest (
  sha256 TEXT PRIMARY KEY, status TEXT NOT NULL, s3_key TEXT, doc_class TEXT,
  pages INT, chunks INT, low_conf_pages INT, escalated_pages INT,
  seconds REAL, error TEXT, worker_id TEXT, embed_pending BOOL,
  updated_at TIMESTAMPTZ);
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
  tsv TSVECTOR, embedding VECTOR(1536), titan_embedding VECTOR(1024),
  qwen_embedding VECTOR(1536), UNIQUE (sha256, page, ordinal));
CREATE INDEX IF NOT EXISTS ws13_chunks_tsv ON ws13_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS ws13_chunks_sha ON ws13_chunks (sha256, page);
CREATE TABLE IF NOT EXISTS ws13_embed_skips (
  chunk_id BIGINT, model TEXT, reason TEXT, PRIMARY KEY (chunk_id, model));
"""
# No ANN index here on purpose: ws13_chunks_titan_hnsw over
# titan_embedding::halfvec(1024) is a long, memory-tuned build over 852,027
# rows and is owned by pipelines/ws13_build_ann_index.py.


BUNDLE_FILES = ('ws13_migrations.sql', 'ws13_migrate.py',
                'ws13_backfill_provenance.py')


def bundle_files():
    """Locate the files this script needs beside it. Returns the .sql path.

    Everything lives next to ws13_seed.py in both layouts that have to work:
    a developer runs `python3 pipelines/ws13_seed.py` from the repository
    root, and the worker fleet untars ws13/fleet/bundle.tar.gz FLAT into
    /opt/ws13 and runs `python3 ws13_seed.py`. There is no second directory
    to search -- the old `os.path.join(HERE, 'pipelines', ...)` candidate
    could never exist, since HERE is already this script's own directory.

    A miss means the bundle was built without the file, and that must abort
    loudly and by name: a database seeded with SCHEMA alone has no
    admission_class, no year range, no provenance columns and no reader role
    -- which the retrieval Lambda would read as a corpus with no rights
    rather than as a broken deploy.
    """
    missing = [name for name in BUNDLE_FILES
               if not os.path.exists(os.path.join(HERE, name))]
    if missing:
        sys.exit(f'{", ".join(missing)}: not found beside ws13_seed.py in '
                 f'{HERE}. Add {", ".join(BUNDLE_FILES)} to '
                 f'ws13/fleet/bundle.tar.gz.')
    return os.path.join(HERE, BUNDLE_FILES[0])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bucket', required=True)
    p.add_argument('--queue-url', required=True)
    p.add_argument('--dsn', required=True)
    p.add_argument('--inventory-key', default='ws13/inventory/inventory.jsonl.gz')
    p.add_argument('--harvest-manifest', default='var/ws12/manifest.jsonl')
    p.add_argument('--schema-only', action='store_true')
    args = p.parse_args()

    migrations_file = bundle_files()
    import ws13_backfill_provenance                         # noqa: E402
    import ws13_migrate                                     # noqa: E402

    conn = psycopg.connect(args.dsn, autocommit=True)
    with open(migrations_file, encoding='utf-8') as handle:
        migrations = ws13_migrate.split_statements(handle.read())
    # Base tables and migrations in one transaction: a database that has the
    # chunk tables but not the citation columns looks to the retrieval Lambda
    # like a corpus with no rights, which is worse than no database at all.
    with conn.transaction():
        conn.execute(SCHEMA)
        ws13_migrate.apply_migrations(conn, migrations)
    print(f'schema ready ({len(migrations)} migration statements applied)')
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
        # source_url / rights_basis / public_domain are the citation
        # contract's provenance: without them a hit cannot render
        # `[title, p. N](source_url)` and attribution does not travel with the
        # 13,013 licensed and 32,312 research copies. They come from
        # ws13_backfill_provenance.load_provenance() rather than from the
        # setdefault above, which is the same call the backfill makes for
        # already-indexed documents. The loop above keeps the FIRST occurrence
        # of a field even when it is empty and a later row carries the
        # licence; the backfill fills from the later row. That is a real
        # difference, not a stylistic one: the same document could end up with
        # different rights depending on which script last touched it. One
        # implementation, one rule, one pass over the file each.
        provenance, _stats, conflicts = (
            ws13_backfill_provenance.load_provenance(
                manifest=args.harvest_manifest))
        for sha, record in provenance.items():
            entry = meta.get(sha)
            if entry is None:
                continue
            for field in ('source_url', 'rights_basis', 'public_domain'):
                if record[field] is not None:
                    entry[field] = record[field]
        print('manifest shas with provenance:', len(provenance))
        if conflicts:
            # Refused by load_provenance() because the manifest contradicts
            # itself about where the bytes live. Enqueued without provenance
            # rather than with a guessed licence, and said out loud.
            print('manifest shas refused for a conflicting rights class:',
                  len(conflicts))

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
