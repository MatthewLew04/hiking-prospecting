#!/usr/bin/env python3
"""Emit a one-line JSON status of the WS12 harvest queue for the heartbeat."""
import datetime as dt
import json
import sqlite3

conn = sqlite3.connect('file:var/ws12/queue.sqlite3?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
detail = conn.execute(
    "SELECT SUM(status='done') d, COUNT(*) t FROM tasks "
    "WHERE kind <> 'document'").fetchone()
document = conn.execute(
    "SELECT SUM(status='done') d, SUM(status='pending') p, "
    "SUM(status='skipped') s, SUM(status='error') e, COUNT(*) t "
    "FROM tasks WHERE kind='document'").fetchone()
fetched = conn.execute('SELECT COUNT(*) FROM url_objects').fetchone()[0]
unique_urls = conn.execute(
    "SELECT COUNT(DISTINCT url) FROM tasks WHERE kind='document'").fetchone()[0]
objects = conn.execute(
    'SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM hash_objects').fetchone()
stored = {row['admission_class']: {'rows': row['n'], 'bytes': row['b']}
          for row in conn.execute(
              'SELECT admission_class, COUNT(*) n, SUM(bytes) b '
              'FROM documents GROUP BY admission_class')}
print(json.dumps({
    'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
    'detail_done': detail['d'], 'detail_total': detail['t'],
    'documents_done': document['d'], 'documents_pending': document['p'],
    'documents_skipped': document['s'], 'documents_error': document['e'],
    'unique_urls': unique_urls, 'urls_fetched': fetched,
    'unique_files': objects['n'], 'unique_bytes': objects['b'],
    'stored': stored,
}, sort_keys=True))
