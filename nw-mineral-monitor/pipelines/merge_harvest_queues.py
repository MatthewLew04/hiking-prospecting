#!/usr/bin/env python3
"""Merge per-run WS12 harvest queues into one canonical queue.

Each production run (per portal or per cooperating node) produces its own
durable queue.  The canonical local queue must hold the union so one manifest
and one coverage export cover every portal.  Merging is append-only and
identity-safe:

- ``hash_objects``/``url_objects`` union by primary key (byte-verified
  identities never conflict; a byte-count mismatch for the same SHA-256 is a
  hard error, never silently resolved);
- ``documents``/``document_candidates`` union by their composite primary
  keys, with a completed row always winning over a pending/skipped one;
- ``tasks`` union by task_key, keeping the more-final status;
- ``source_records``/``observations``/``portal_runs`` union, with
  completion-blocker observations preserved and portal_runs never upgraded
  to complete by a merge.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from mine_file_harvest import QueueDB

TASK_RANK = {'pending': 0, 'active': 0, 'error': 1, 'skipped': 2, 'done': 3}


class MergeError(RuntimeError):
    pass


def merge_into(target: QueueDB, source_path: str) -> dict:
    source = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    stats = {}
    conn = target.conn
    with conn:
        for row in source.execute('SELECT * FROM hash_objects'):
            existing = conn.execute(
                'SELECT bytes FROM hash_objects WHERE sha256=?',
                (row['sha256'],)).fetchone()
            if existing and existing['bytes'] != row['bytes']:
                raise MergeError(
                    f'sha256 {row["sha256"]} byte-count conflict: '
                    f'{existing["bytes"]} vs {row["bytes"]}')
            conn.execute(
                'INSERT OR IGNORE INTO hash_objects VALUES (?,?,?,?)',
                tuple(row))
        stats['hash_objects'] = conn.total_changes

        for row in source.execute('SELECT * FROM url_objects'):
            conn.execute(
                'INSERT OR IGNORE INTO url_objects VALUES (?,?,?)',
                tuple(row))

        columns = [r[1] for r in source.execute('PRAGMA table_info(documents)')]
        marks = ','.join('?' for _ in columns)
        added = 0
        for row in source.execute('SELECT * FROM documents'):
            cursor = conn.execute(
                f'INSERT OR IGNORE INTO documents ({",".join(columns)}) '
                f'VALUES ({marks})', tuple(row))
            added += cursor.rowcount
        stats['documents_added'] = added

        for row in source.execute('SELECT * FROM document_candidates'):
            existing = conn.execute(
                '''SELECT disposition FROM document_candidates WHERE
                   portal_id=? AND portal_source=? AND source_url=? AND
                   mine_id=?''',
                (row['portal_id'], row['portal_source'], row['source_url'],
                 row['mine_id'])).fetchone()
            if existing is None:
                conn.execute(
                    'INSERT INTO document_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    tuple(row))
            elif (existing['disposition'] != 'downloaded' and
                  row['disposition'] == 'downloaded'):
                conn.execute(
                    '''UPDATE document_candidates SET rights_status=?,
                       rights_basis=?, disposition=?, reason=?, discovered_at=?
                       WHERE portal_id=? AND portal_source=? AND source_url=?
                       AND mine_id=?''',
                    (row['rights_status'], row['rights_basis'],
                     row['disposition'], row['reason'], row['discovered_at'],
                     row['portal_id'], row['portal_source'], row['source_url'],
                     row['mine_id']))

        for row in source.execute('SELECT * FROM tasks'):
            existing = conn.execute(
                'SELECT status FROM tasks WHERE task_key=?',
                (row['task_key'],)).fetchone()
            if existing is None:
                conn.execute(
                    '''INSERT INTO tasks (task_key, portal_id, kind, url,
                       payload, status, attempts, not_before, last_error,
                       created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (row['task_key'], row['portal_id'], row['kind'],
                     row['url'], row['payload'], row['status'],
                     row['attempts'], row['not_before'], row['last_error'],
                     row['created_at'], row['updated_at']))
            elif TASK_RANK.get(row['status'], 0) > TASK_RANK.get(
                    existing['status'], 0):
                conn.execute(
                    '''UPDATE tasks SET status=?, attempts=?, last_error=?,
                       updated_at=? WHERE task_key=?''',
                    (row['status'], row['attempts'], row['last_error'],
                     row['updated_at'], row['task_key']))

        for row in source.execute('SELECT * FROM source_records'):
            conn.execute(
                '''INSERT OR IGNORE INTO source_records
                   (portal_id, portal_source, mine_id, mine_name, metadata,
                    observed_at) VALUES (?,?,?,?,?,?)''',
                (row['portal_id'], row['portal_source'], row['mine_id'],
                 row['mine_name'], row['metadata'], row['observed_at']))

        for row in source.execute('SELECT * FROM observations'):
            conn.execute(
                '''INSERT INTO observations (portal_id, name, value,
                   observed_at) VALUES (?,?,?,?)
                   ON CONFLICT(portal_id, name) DO UPDATE SET
                     value=excluded.value, observed_at=excluded.observed_at''',
                (row['portal_id'], row['name'], row['value'],
                 row['observed_at']))

        for row in source.execute('SELECT * FROM portal_runs'):
            conn.execute(
                '''INSERT OR IGNORE INTO portal_runs (portal_id,
                   registry_sha256, seeded_at, crawl_scope, cursor_exhausted,
                   completed_at) VALUES (?,?,?,?,?,?)''',
                (row['portal_id'], row['registry_sha256'], row['seeded_at'],
                 row['crawl_scope'], row['cursor_exhausted'],
                 row['completed_at']))
    source.close()
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('sources', nargs='+')
    args = parser.parse_args(argv)
    target = QueueDB(args.target)
    try:
        for source_path in args.sources:
            stats = merge_into(target, source_path)
            print(f'merged {source_path}: {stats}')
    finally:
        target.close()
    return 0


if __name__ == '__main__':
    sys.path.insert(0, __file__.rsplit('/', 1)[0])
    raise SystemExit(main())
