#!/usr/bin/env python3
"""WS12 throttled, resumable mine-file harvester.

Only registry entries with a reviewed ``harvest_ready`` adapter can execute.
Every request is checked against robots.txt, every candidate is rights-gated,
and only byte-verified PDFs are sent to the S3 original-object sink.  The
SQLite queue and JSONL manifest contain metadata only; PDF bytes are never a
repository artifact.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
from html.parser import HTMLParser
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib import error, parse, request, robotparser

from portal_registry import PORTALS_DIR, PortalRegistryError, load_registry


USER_AGENT = 'nw-mineral-monitor/12.0 (public-document research; contact: repository owner)'
TRANSIENT_HTTP = frozenset((408, 425, 429, 500, 502, 503, 504))
AUTH_HTTP = frozenset((401, 402, 407))
REDIRECT_HTTP = frozenset((301, 302, 303, 307, 308))
PAYWALL_RE = re.compile(
    rb'(?:subscribe|purchase|payment)\s+(?:to|required)|'
    rb'(?:sign\s*in|log\s*in)\s+to\s+download|paywall', re.I)
PDF_MAGIC = b'%PDF-'
DEFAULT_MAX_PDF_BYTES = 750 * 1024 * 1024
MANIFEST_FIELDS = (
    'schema_version', 'source_url', 'portal_id', 'portal_source', 'mine_id',
    'mine_name', 'state', 'county', 'trs', 'document_title', 'doc_date',
    'doc_type', 'sha256', 'bytes', 'retrieval_date', 'content_type', 's3_uri',
    'etag', 'last_modified', 'public_domain', 'rights_basis', 'paywalled',
)


class HarvestError(RuntimeError):
    """Base class for crawl failures."""


class TransientHarvestError(HarvestError):
    """A task may succeed on a later attempt."""


class PermanentSkip(HarvestError):
    """A policy or source condition means this candidate must be skipped."""


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False)


def canonical_sha256(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def utc_date():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _atomic_text(path, writer):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, pending = tempfile.mkstemp(prefix='.ws12-', dir=directory)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as output:
            writer(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


class QueueDB:
    """Durable frontier, candidate log, hash index, and manifest source."""

    def __init__(self, path):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
                task_key TEXT PRIMARY KEY,
                portal_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                url TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','active','done','skipped','error')),
                attempts INTEGER NOT NULL DEFAULT 0,
                not_before REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_ready
                ON tasks(status, not_before, portal_id);
            CREATE TABLE IF NOT EXISTS document_candidates (
                portal_id TEXT NOT NULL,
                portal_source TEXT NOT NULL,
                source_url TEXT NOT NULL,
                mine_id TEXT NOT NULL,
                mine_name TEXT NOT NULL,
                state TEXT NOT NULL,
                rights_status TEXT NOT NULL,
                rights_basis TEXT,
                disposition TEXT NOT NULL,
                reason TEXT,
                discovered_at TEXT NOT NULL,
                PRIMARY KEY(portal_id, portal_source, source_url, mine_id)
            );
            CREATE TABLE IF NOT EXISTS hash_objects (
                sha256 TEXT PRIMARY KEY,
                bytes INTEGER NOT NULL,
                s3_uri TEXT NOT NULL,
                content_type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                portal_id TEXT NOT NULL,
                portal_source TEXT NOT NULL,
                source_url TEXT NOT NULL,
                mine_id TEXT NOT NULL,
                mine_name TEXT NOT NULL,
                state TEXT NOT NULL,
                county TEXT,
                trs TEXT,
                document_title TEXT NOT NULL,
                doc_date TEXT,
                doc_type TEXT,
                sha256 TEXT NOT NULL REFERENCES hash_objects(sha256),
                bytes INTEGER NOT NULL,
                retrieval_date TEXT NOT NULL,
                content_type TEXT NOT NULL,
                s3_uri TEXT NOT NULL,
                etag TEXT,
                last_modified TEXT,
                public_domain INTEGER NOT NULL CHECK(public_domain = 1),
                rights_basis TEXT NOT NULL,
                paywalled INTEGER NOT NULL CHECK(paywalled = 0),
                PRIMARY KEY(portal_id, portal_source, source_url, mine_id)
            );
            CREATE TABLE IF NOT EXISTS source_records (
                portal_id TEXT NOT NULL,
                portal_source TEXT NOT NULL,
                mine_id TEXT NOT NULL,
                mine_name TEXT NOT NULL,
                metadata TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY(portal_id, portal_source, mine_id)
            );
            CREATE TABLE IF NOT EXISTS observations (
                portal_id TEXT NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY(portal_id, name)
            );
            CREATE TABLE IF NOT EXISTS portal_runs (
                portal_id TEXT PRIMARY KEY,
                registry_sha256 TEXT NOT NULL,
                seeded_at TEXT NOT NULL,
                crawl_scope TEXT NOT NULL DEFAULT 'full',
                cursor_exhausted INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT
            );
        ''')
        run_columns = {row['name'] for row in self.conn.execute(
            'PRAGMA table_info(portal_runs)')}
        if 'crawl_scope' not in run_columns:
            self.conn.execute(
                "ALTER TABLE portal_runs ADD COLUMN crawl_scope TEXT "
                "NOT NULL DEFAULT 'full'")
        if 'cursor_exhausted' not in run_columns:
            self.conn.execute(
                'ALTER TABLE portal_runs ADD COLUMN cursor_exhausted INTEGER '
                'NOT NULL DEFAULT 0')
        # An interrupted process never owns an active task after reopening.
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE tasks SET status='pending', updated_at=? WHERE status='active'",
            (now,))
        self.conn.commit()

    def close(self):
        self.conn.close()

    def enqueue(self, portal_id, task_key, kind, url, payload):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        cursor = self.conn.execute(
            '''INSERT OR IGNORE INTO tasks
               (task_key, portal_id, kind, url, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (task_key, portal_id, kind, url, canonical_json(payload), now, now))
        self.conn.commit()
        return cursor.rowcount == 1

    def enqueue_many(self, rows):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        values = [(
            row['task_key'], row['portal_id'], row['kind'], row['url'],
            canonical_json(row['payload']), now, now) for row in rows]
        before = self.conn.total_changes
        self.conn.executemany(
            '''INSERT OR IGNORE INTO tasks
               (task_key, portal_id, kind, url, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''', values)
        self.conn.commit()
        return self.conn.total_changes - before

    def seed_portal(self, portal, crawl_scope='full'):
        portal_id = portal['id']
        digest = canonical_sha256(portal)
        prior = self.conn.execute(
            'SELECT registry_sha256 FROM portal_runs WHERE portal_id=?',
            (portal_id,)).fetchone()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if prior and prior['registry_sha256'] != digest:
            self.conn.execute(
                "UPDATE tasks SET status='pending', not_before=0, last_error=NULL, "
                "updated_at=? WHERE portal_id=? AND kind <> 'document'",
                (now, portal_id))
        self.conn.execute(
            '''INSERT INTO portal_runs(portal_id, registry_sha256, seeded_at,
                                       crawl_scope, cursor_exhausted, completed_at)
               VALUES (?, ?, ?, ?, 0, NULL)
               ON CONFLICT(portal_id) DO UPDATE SET
                 registry_sha256=excluded.registry_sha256,
                 seeded_at=excluded.seeded_at,
                 crawl_scope=excluded.crawl_scope,
                 cursor_exhausted=0,
                 completed_at=NULL''', (portal_id, digest, now, crawl_scope))
        self.conn.execute(
            'UPDATE portal_runs SET crawl_scope=? WHERE portal_id=?',
            (crawl_scope, portal_id))
        self.conn.commit()

    def refresh(self, portal_ids):
        if not portal_ids:
            return
        marks = ','.join('?' for _ in portal_ids)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            f"UPDATE tasks SET status='pending', attempts=0, not_before=0, "
            f"last_error=NULL, updated_at=? WHERE portal_id IN ({marks}) "
            "AND kind <> 'document'", (now, *portal_ids))
        self.conn.execute(
            f"UPDATE portal_runs SET completed_at=NULL, cursor_exhausted=0 "
            f"WHERE portal_id IN ({marks})",
            tuple(portal_ids))
        self.conn.commit()

    def claim(self, portal_ids=None):
        now_epoch = time.time()
        clauses = ["status='pending'", 'not_before <= ?']
        params = [now_epoch]
        if portal_ids:
            clauses.append('portal_id IN (' + ','.join('?' for _ in portal_ids) + ')')
            params.extend(portal_ids)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute(
                'SELECT * FROM tasks WHERE ' + ' AND '.join(clauses) +
                ' ORDER BY rowid LIMIT 1', params).fetchone()
            if row is None:
                self.conn.commit()
                return None
            changed = self.conn.execute(
                "UPDATE tasks SET status='active', attempts=attempts+1, "
                "updated_at=? WHERE task_key=? AND status='pending'",
                (dt.datetime.now(dt.timezone.utc).isoformat(),
                 row['task_key'])).rowcount
            self.conn.commit()
            if changed != 1:
                return None
            value = dict(row)
            value['attempts'] += 1
            value['payload'] = json.loads(value['payload'])
            return value
        except BaseException:
            self.conn.rollback()
            raise

    def finish(self, task_key, status='done', message=None):
        self.conn.execute(
            'UPDATE tasks SET status=?, last_error=?, updated_at=? WHERE task_key=?',
            (status, message, dt.datetime.now(dt.timezone.utc).isoformat(), task_key))
        self.conn.commit()

    def retry(self, task, message, max_attempts=6):
        if task['attempts'] >= max_attempts:
            self.finish(task['task_key'], 'error', message)
            return False
        wait_seconds = min(300, 2 ** max(0, task['attempts'] - 1))
        self.conn.execute(
            "UPDATE tasks SET status='pending', last_error=?, not_before=?, "
            "updated_at=? WHERE task_key=?",
            (message, time.time() + wait_seconds,
             dt.datetime.now(dt.timezone.utc).isoformat(), task['task_key']))
        self.conn.commit()
        return True

    def candidate(self, payload, disposition, reason=None):
        self.conn.execute(
            '''INSERT INTO document_candidates
               (portal_id, portal_source, source_url, mine_id, mine_name,
                state, rights_status, rights_basis, disposition, reason,
                discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(portal_id, portal_source, source_url, mine_id)
               DO UPDATE SET rights_status=excluded.rights_status,
                 rights_basis=excluded.rights_basis,
                 disposition=excluded.disposition, reason=excluded.reason''',
            (payload['portal_id'], payload['portal_source'], payload['source_url'],
             payload.get('mine_id') or '', payload.get('mine_name') or '',
             payload['state'], payload.get('rights_status', 'unverified'),
             payload.get('rights_basis'), disposition, reason, utc_date()))
        self.conn.commit()

    def known_document(self, payload):
        return self.conn.execute(
            '''SELECT 1 FROM documents WHERE portal_id=? AND portal_source=?
               AND source_url=? AND mine_id=?''',
            (payload['portal_id'], payload['portal_source'], payload['source_url'],
             payload.get('mine_id') or '')).fetchone() is not None

    def hash_object(self, sha256):
        return self.conn.execute(
            'SELECT * FROM hash_objects WHERE sha256=?', (sha256,)).fetchone()

    def record_document(self, payload, result):
        if payload.get('rights_status') != 'public_domain':
            raise HarvestError('refusing to manifest a rights-unverified document')
        rights_basis = payload.get('rights_basis')
        if not isinstance(rights_basis, str) or len(rights_basis.strip()) < 12:
            raise HarvestError('public-domain manifest row lacks rights basis')
        with self.conn:
            self.conn.execute(
                '''INSERT OR IGNORE INTO hash_objects
                   (sha256, bytes, s3_uri, content_type) VALUES (?, ?, ?, ?)''',
                (result['sha256'], result['bytes'], result['s3_uri'],
                 result['content_type']))
            self.conn.execute(
                '''INSERT OR REPLACE INTO documents
                   (portal_id, portal_source, source_url, mine_id, mine_name,
                    state, county, trs, document_title, doc_date, doc_type,
                    sha256, bytes, retrieval_date, content_type, s3_uri, etag,
                    last_modified, public_domain, rights_basis, paywalled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, 1, ?, 0)''',
                (payload['portal_id'], payload['portal_source'],
                 payload['source_url'], payload.get('mine_id') or '',
                 payload.get('mine_name') or '', payload['state'],
                 payload.get('county'), payload.get('trs'),
                 payload.get('document_title') or os.path.basename(
                     parse.urlsplit(payload['source_url']).path),
                 payload.get('doc_date'), payload.get('doc_type'),
                 result['sha256'], result['bytes'], utc_date(),
                 result['content_type'], result['s3_uri'], result.get('etag'),
                 result.get('last_modified'), rights_basis))
        self.candidate(payload, 'downloaded')

    def source_record(self, portal_id, portal_source, mine_id, mine_name,
                      metadata=None):
        self.conn.execute(
            '''INSERT INTO source_records
               (portal_id, portal_source, mine_id, mine_name, metadata, observed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(portal_id, portal_source, mine_id) DO UPDATE SET
                 mine_name=excluded.mine_name, metadata=excluded.metadata,
                 observed_at=excluded.observed_at''',
            (portal_id, portal_source, mine_id, mine_name,
             canonical_json(metadata or {}), utc_date()))
        self.conn.commit()

    def observe(self, portal_id, name, value):
        self.conn.execute(
            '''INSERT INTO observations(portal_id, name, value, observed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(portal_id, name) DO UPDATE SET
                 value=excluded.value, observed_at=excluded.observed_at''',
            (portal_id, name, canonical_json(value), utc_date()))
        self.conn.commit()

    def maybe_complete(self, portal_ids, full_scope=True):
        if not full_scope:
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for portal_id in portal_ids:
            remaining = self.conn.execute(
                '''SELECT COUNT(*) AS n FROM tasks WHERE portal_id=?
                   AND (status IN ('pending','active','error') OR
                        (status='skipped' AND kind <> 'document'))''',
                (portal_id,)).fetchone()['n']
            blocker = self.conn.execute(
                '''SELECT value FROM observations WHERE portal_id=?
                   AND name='crawl_completion_blocker' ''',
                (portal_id,)).fetchone()
            if remaining == 0 and blocker is None:
                self.conn.execute(
                    '''UPDATE portal_runs SET completed_at=?, cursor_exhausted=1
                       WHERE portal_id=? AND crawl_scope='full' ''',
                    (now, portal_id))
        self.conn.commit()

    def write_manifest(self, path):
        rows = self.conn.execute(
            '''SELECT * FROM documents ORDER BY portal_id, mine_id,
                      portal_source, source_url''').fetchall()

        def write(output):
            for row in rows:
                item = {field: row[field] if field in row.keys() else None
                        for field in MANIFEST_FIELDS}
                item['schema_version'] = 1
                item['public_domain'] = bool(item['public_domain'])
                item['paywalled'] = bool(item['paywalled'])
                output.write(canonical_json(item) + '\n')
        _atomic_text(path, write)
        return len(rows)

    def coverage(self, registry):
        result = []
        for portal_id, portal in sorted(registry.items()):
            counts = {row['status']: row['n'] for row in self.conn.execute(
                '''SELECT status, COUNT(*) AS n FROM tasks WHERE portal_id=?
                   GROUP BY status''', (portal_id,))}
            candidates = self.conn.execute(
                '''SELECT COUNT(*) AS n FROM document_candidates
                   WHERE portal_id=?''', (portal_id,)).fetchone()['n']
            downloaded = self.conn.execute(
                'SELECT COUNT(*) AS n FROM documents WHERE portal_id=?',
                (portal_id,)).fetchone()['n']
            run = self.conn.execute(
                'SELECT * FROM portal_runs WHERE portal_id=?',
                (portal_id,)).fetchone()
            blocker_row = self.conn.execute(
                '''SELECT value FROM observations WHERE portal_id=?
                   AND name='crawl_completion_blocker' ''',
                (portal_id,)).fetchone()
            blocker = json.loads(blocker_row['value']) if blocker_row else None
            if run is None:
                if portal['status'] in (
                        'index_only_no_public_documents', 'probed_empty'):
                    candidates_value = 0
                    downloaded_value = 0
                    counts_status = 'registry_established_no_attachments'
                else:
                    candidates_value = None
                    downloaded_value = None
                    counts_status = 'not_started'
            else:
                candidates_value = candidates
                downloaded_value = downloaded
                counts_status = (
                    'complete' if run['cursor_exhausted'] else 'partial')
            hash_count = self.conn.execute(
                '''SELECT COUNT(DISTINCT sha256) AS n FROM documents
                   WHERE portal_id=?''', (portal_id,)).fetchone()['n']
            manifest_rows = [dict(row) for row in self.conn.execute(
                '''SELECT portal_source, source_url, mine_id, sha256, bytes
                   FROM documents WHERE portal_id=? ORDER BY portal_source,
                   source_url, mine_id''', (portal_id,))]
            result.append({
                'portal_id': portal_id,
                'jurisdiction': portal['jurisdiction'],
                'registry_status': portal['status'],
                'crawl_started': run is not None,
                'counts_status': counts_status,
                'documents_found': candidates_value,
                'documents_downloaded': downloaded_value,
                'tasks_pending': counts.get('pending', 0),
                'tasks_error': counts.get('error', 0),
                'tasks_skipped': counts.get('skipped', 0),
                'crawl_complete': bool(
                    run and run['completed_at'] and run['cursor_exhausted']),
                'cursor_exhausted': bool(run and run['cursor_exhausted']),
                'crawl_scope': (run['crawl_scope'] if run else
                                portal['harvest_state']['crawl_scope']),
                'completed_at': run['completed_at'] if run else None,
                'completion_blocker': blocker,
                'manifest_rows': len(manifest_rows) if run else (
                    0 if counts_status == 'registry_established_no_attachments'
                    else None),
                'unique_hashes': hash_count if run else (
                    0 if counts_status == 'registry_established_no_attachments'
                    else None),
                'manifest_rows_sha256': (
                    canonical_sha256(manifest_rows) if run else None),
            })
        return {'schema_version': 1, 'generated': utc_date(), 'portals': result}

    def write_coverage(self, path, registry):
        value = self.coverage(registry)
        _atomic_text(path, lambda output: json.dump(
            value, output, indent=2, ensure_ascii=False, allow_nan=False))

    def write_diff(self, path, portal_id, left='legacy', right='current'):
        records = self.conn.execute(
            '''SELECT portal_source, mine_id, mine_name, source_url, sha256
               FROM documents WHERE portal_id=? AND portal_source IN (?, ?)
               ORDER BY mine_id, source_url''', (portal_id, left, right)).fetchall()
        mines = {}
        for row in records:
            mine = mines.setdefault(row['mine_id'], {
                'mine_id': row['mine_id'], 'mine_name': row['mine_name'],
                left: [], right: []})
            mine[row['portal_source']].append({
                'source_url': row['source_url'], 'sha256': row['sha256']})
        for mine in mines.values():
            left_hashes = {row['sha256'] for row in mine[left]}
            right_hashes = {row['sha256'] for row in mine[right]}
            mine['only_' + left] = sorted(left_hashes - right_hashes)
            mine['only_' + right] = sorted(right_hashes - left_hashes)
        observations = {row['name']: json.loads(row['value']) for row in
                        self.conn.execute(
                            'SELECT name, value FROM observations WHERE portal_id=?',
                            (portal_id,))}
        value = {
            'schema_version': 1, 'portal_id': portal_id, 'generated': utc_date(),
            'left': left, 'right': right,
            'source_status': {
                left: {'status': 'harvested_document_links'},
                right: observations.get('arcgis_current_inventory', {
                    'status': 'not_observed',
                }),
            },
            'mines': list(mines.values())}
        _atomic_text(path, lambda output: json.dump(
            value, output, indent=2, ensure_ascii=False, allow_nan=False))


class _NoRedirect(request.HTTPRedirectHandler):
    """Expose redirects so policy can inspect them before a second request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RobotsPolicy:
    def __init__(self, user_agent=USER_AGENT, timeout=60, opener=None):
        self.user_agent = user_agent
        self.timeout = timeout
        self.opener = opener or request.build_opener(_NoRedirect())
        self._cache = {}

    @staticmethod
    def _origin(url):
        parts = parse.urlsplit(url)
        return f'{parts.scheme}://{parts.netloc}'

    def rules(self, url):
        origin = self._origin(url)
        if origin in self._cache:
            return self._cache[origin]
        robots_url = origin + '/robots.txt'
        parser = robotparser.RobotFileParser(robots_url)
        try:
            req = request.Request(robots_url, headers={
                'User-Agent': self.user_agent, 'Accept': 'text/plain'})
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(1 << 20)
            text = raw.decode('utf-8-sig', 'replace')
            parser.parse(text.splitlines())
            value = (parser, 'present')
        except error.HTTPError as exc:
            if exc.code in REDIRECT_HTTP:
                location = exc.headers.get('Location')
                exc.close()
                raise TransientHarvestError(
                    f'robots redirect refused: {robots_url} -> '
                    f'{parse.urljoin(robots_url, location or "<missing>")}') from exc
            if exc.code in (404, 410):
                parser.parse([])
                value = (parser, 'absent')
            elif exc.code in AUTH_HTTP or exc.code == 403:
                parser.disallow_all = True
                value = (parser, f'denied_http_{exc.code}')
            else:
                raise TransientHarvestError(
                    f'cannot verify robots policy at {robots_url}: HTTP {exc.code}')
        except (error.URLError, TimeoutError, OSError) as exc:
            raise TransientHarvestError(
                f'cannot verify robots policy at {robots_url}: {exc}') from exc
        self._cache[origin] = value
        return value

    def allowed(self, url):
        parser, status = self.rules(url)
        return parser.can_fetch(self.user_agent, url), status

    def delay(self, url):
        parser, _ = self.rules(url)
        return parser.crawl_delay(self.user_agent) or parser.crawl_delay('*') or 0


class HttpClient:
    def __init__(self, min_interval_seconds=1.0, user_agent=USER_AGENT,
                 timeout=180, robots=None, allowed_hosts=None, opener=None):
        self.min_interval_seconds = min_interval_seconds
        self.user_agent = user_agent
        self.timeout = timeout
        self.robots = robots or RobotsPolicy(user_agent, timeout=min(timeout, 60))
        self.allowed_hosts = frozenset(
            str(host).lower().rstrip('.') for host in (allowed_hosts or ()))
        self.opener = opener or request.build_opener(_NoRedirect())
        self._last_request = {}

    def _check_url(self, url):
        parts = parse.urlsplit(url)
        host = str(parts.hostname or '').lower().rstrip('.')
        try:
            port = parts.port
        except ValueError as exc:
            raise PermanentSkip(f'unsafe_or_non_https_url:{url}') from exc
        if (parts.scheme != 'https' or not host or parts.username or
                parts.password or port not in (None, 443)):
            raise PermanentSkip(f'unsafe_or_non_https_url:{url}')
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise PermanentSkip(f'redirect_or_source_host_not_allowlisted:{host}')

    def _throttle(self, url):
        origin = RobotsPolicy._origin(url)
        delay = max(self.min_interval_seconds, self.robots.delay(url))
        elapsed = time.monotonic() - self._last_request.get(origin, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request[origin] = time.monotonic()

    def open(self, url, accept='*/*'):
        current = url
        for _ in range(11):
            self._check_url(current)
            allowed, robots_status = self.robots.allowed(current)
            if not allowed:
                raise PermanentSkip(
                    f'robots_disallowed ({robots_status}) for {current}')
            self._throttle(current)
            req = request.Request(current, headers={
                'User-Agent': self.user_agent, 'Accept': accept})
            try:
                return self.opener.open(req, timeout=self.timeout)
            except error.HTTPError as exc:
                if exc.code in REDIRECT_HTTP:
                    location = exc.headers.get('Location')
                    exc.close()
                    if not location:
                        raise PermanentSkip(
                            f'redirect_without_location_http_{exc.code}') from exc
                    current = parse.urljoin(current, location)
                    continue
                if exc.code in AUTH_HTTP:
                    raise PermanentSkip(f'paywall_or_auth_http_{exc.code}') from exc
                if exc.code == 403:
                    raise PermanentSkip('access_denied_http_403') from exc
                if exc.code in (404, 410):
                    raise PermanentSkip(f'not_found_http_{exc.code}') from exc
                if exc.code in TRANSIENT_HTTP:
                    raise TransientHarvestError(f'transient HTTP {exc.code}') from exc
                raise PermanentSkip(f'unsupported_http_{exc.code}') from exc
            except (error.URLError, TimeoutError, OSError) as exc:
                raise TransientHarvestError(str(exc)) from exc
        raise PermanentSkip('too_many_redirects')

    def bytes(self, url, accept, max_bytes):
        with self.open(url, accept=accept) as response:
            length = response.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                raise PermanentSkip(f'response_too_large:{length}')
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise PermanentSkip(f'response_exceeds_{max_bytes}_bytes')
            return body, response.headers, response.url

    def json(self, url, max_bytes=32 * 1024 * 1024):
        raw, headers, final_url = self.bytes(
            url, 'application/json', max_bytes=max_bytes)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransientHarvestError(f'invalid JSON from {url}: {exc}') from exc
        if not isinstance(value, dict):
            raise TransientHarvestError(f'JSON response from {url} is not an object')
        if value.get('error'):
            code = value['error'].get('code') if isinstance(value['error'], dict) else None
            if code in AUTH_HTTP or code in (403, 499):
                raise PermanentSkip(f'ArcGIS access error: {value["error"]}')
            raise TransientHarvestError(f'API error: {value["error"]}')
        return value, headers, final_url


class S3OriginalSink:
    """Content-addressed S3 sink; boto3 is loaded only for a real crawl."""

    def __init__(self, bucket, prefix='originals', client=None):
        if not bucket or '/' in bucket or bucket.startswith('.'):
            raise ValueError('an exact S3 bucket name is required')
        self.bucket = bucket
        self.prefix = prefix.strip('/')
        if client is None:
            try:
                import boto3  # type: ignore
            except ImportError as exc:
                if not shutil.which('aws'):
                    raise RuntimeError(
                        'real harvests require boto3 or the AWS CLI') from exc
                client = None
            else:
                client = boto3.client('s3')
        self.client = client

    @staticmethod
    def _verify_head(head, sha256, byte_count):
        metadata = head.get('Metadata') or {}
        actual_bytes = int(head.get('ContentLength') or -1)
        if (actual_bytes != byte_count or metadata.get('sha256') != sha256 or
                metadata.get('bytes') != str(byte_count) or
                not str(head.get('ContentType') or '').lower().startswith(
                    'application/pdf')):
            raise TransientHarvestError(
                'uploaded S3 original failed bytes/hash/content-type HEAD verification')

    def put(self, stream, sha256, byte_count, content_type, portal_id):
        key = '/'.join(part for part in (
            self.prefix, portal_id, sha256[:2], sha256 + '.pdf') if part)
        stream.seek(0)
        if self.client is not None:
            self.client.upload_fileobj(
                stream, self.bucket, key,
                ExtraArgs={
                    'ContentType': 'application/pdf',
                    'ServerSideEncryption': 'AES256',
                    'Metadata': {
                        'sha256': sha256, 'bytes': str(byte_count),
                        'portal-id': portal_id, 'public-domain': 'true',
                    },
                })
            try:
                head = self.client.head_object(Bucket=self.bucket, Key=key)
            except Exception as exc:
                raise TransientHarvestError(
                    f'could not verify uploaded S3 original: {exc}') from exc
            self._verify_head(head, sha256, byte_count)
        else:
            destination = f's3://{self.bucket}/{key}'
            result = subprocess.run(
                ['aws', 's3', 'cp', '-', destination,
                 '--only-show-errors', '--sse', 'AES256',
                 '--content-type', 'application/pdf',
                 '--metadata', (f'sha256={sha256},bytes={byte_count},'
                                f'portal-id={portal_id},public-domain=true')],
                stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False)
            if result.returncode:
                message = result.stderr.decode('utf-8', 'replace').strip()
                raise TransientHarvestError(
                    f'AWS CLI upload failed: {message[:500]}')
            verified = subprocess.run(
                ['aws', 's3api', 'head-object', '--bucket', self.bucket,
                 '--key', key, '--output', 'json'], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False)
            if verified.returncode:
                message = verified.stderr.decode('utf-8', 'replace').strip()
                raise TransientHarvestError(
                    f'AWS CLI S3 HEAD verification failed: {message[:500]}')
            try:
                head = json.loads(verified.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TransientHarvestError(
                    'AWS CLI S3 HEAD verification returned invalid JSON') from exc
            self._verify_head(head, sha256, byte_count)
        return f's3://{self.bucket}/{key}'


class MemorySink:
    """Test sink with the same content-addressed interface."""

    def __init__(self):
        self.objects = {}

    def put(self, stream, sha256, byte_count, content_type, portal_id):
        stream.seek(0)
        raw = stream.read()
        if len(raw) != byte_count:
            raise AssertionError('sink byte count mismatch')
        self.objects.setdefault(sha256, raw)
        return f'memory://originals/{portal_id}/{sha256}.pdf'


class LinkTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text_parts = []
        self.h1_parts = []
        self._href = None
        self._anchor = []
        self._h1_depth = 0
        self._h1_complete = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.casefold() == 'a' and values.get('href'):
            self._href = values['href']
            self._anchor = []
        if tag.casefold() == 'h1' and not self._h1_complete:
            self._h1_depth += 1

    def handle_endtag(self, tag):
        if tag.casefold() == 'a' and self._href is not None:
            self.links.append((self._href, ' '.join(self._anchor).strip()))
            self._href = None
            self._anchor = []
        if tag.casefold() == 'h1' and self._h1_depth:
            self._h1_depth -= 1
            self._h1_complete = True

    def handle_data(self, data):
        clean = ' '.join(data.split())
        if not clean:
            return
        self.text_parts.append(clean)
        if self._href is not None:
            self._anchor.append(clean)
        if self._h1_depth:
            self.h1_parts.append(clean)

    @property
    def text(self):
        return ' '.join(self.text_parts)

    @property
    def h1(self):
        return ' '.join(self.h1_parts)


def parse_html(raw):
    parser = LinkTextParser()
    parser.feed(raw.decode('utf-8', 'replace'))
    return parser


def _query_url(base, parameters):
    separator = '&' if '?' in base else '?'
    return base + separator + parse.urlencode(parameters)


def _pdf_candidate_payload(portal, portal_source, source_url, mine_id,
                           mine_name, **metadata):
    rights_status = metadata.pop('rights_status', None)
    rights_basis = metadata.pop('rights_basis', None)
    if rights_status is None:
        rules = portal.get('rights_rules') or []
        for rule in rules:
            if re.search(rule['url_pattern'], source_url, re.I):
                rights_status = rule['status']
                rights_basis = rule.get('basis')
                break
    return {
        'portal_id': portal['id'], 'portal_source': portal_source,
        'source_url': source_url, 'mine_id': mine_id or '',
        'mine_name': mine_name or '',
        'state': portal['jurisdiction'], 'rights_status': rights_status or 'unverified',
        'rights_basis': rights_basis, **metadata,
    }


def _portal_https_hosts(portal):
    """Return exact HTTPS hosts reviewed in one executable registry entry."""
    hosts = set()

    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and value.startswith('https://'):
            host = parse.urlsplit(value).hostname
            if host:
                host = host.lower().rstrip('.')
                hosts.add(host)
                hosts.add(host[4:] if host.startswith('www.') else 'www.' + host)

    walk(portal)
    return hosts


class Harvester:
    def __init__(self, registry, db, sink, client_factory=None,
                 max_pdf_bytes=DEFAULT_MAX_PDF_BYTES):
        self.registry = registry
        self.db = db
        self.sink = sink
        self.max_pdf_bytes = max_pdf_bytes
        self.client_factory = client_factory
        self._clients = {}

    def client(self, portal):
        portal_id = portal['id']
        if portal_id not in self._clients:
            interval = portal['crawler']['min_interval_seconds']
            self._clients[portal_id] = (
                self.client_factory(portal) if self.client_factory
                else HttpClient(min_interval_seconds=interval,
                                allowed_hosts=_portal_https_hosts(portal)))
        return self._clients[portal_id]

    def seed(self, portal_ids, mine_ids=None):
        mine_ids = list(mine_ids or [])
        for portal_id in portal_ids:
            portal = self.registry[portal_id]
            if portal['status'] != 'harvest_ready':
                raise ValueError(
                    f'{portal_id} is {portal["status"]}, not harvest_ready')
            if mine_ids and portal_id != 'igs_mines':
                raise ValueError('--mine-id is currently supported only by igs_mines')
            self.db.seed_portal(
                portal, crawl_scope='targeted:' + ','.join(mine_ids)
                if mine_ids else 'full')
            adapter = portal['crawler']['adapter']
            if adapter == 'igs_legacy_and_current':
                self._seed_igs(portal, mine_ids=mine_ids)
            elif adapter == 'azgs_metadata':
                self._seed_azgs(portal)
            elif adapter == 'nbmg_embedded_js':
                self.db.enqueue(
                    portal_id, f'{portal_id}:nbmg-index', 'nbmg_index',
                    portal['crawler']['index_url'], {})
            elif adapter == 'html_catalog':
                self.db.enqueue(
                    portal_id, f'{portal_id}:html-index:{portal["entry_url"]}',
                    'html_index', portal['entry_url'], {'depth': 0})
            elif adapter == 'arcgis_attachments':
                self.db.enqueue(
                    portal_id, f'{portal_id}:arcgis-inventory',
                    'arcgis_inventory', portal['crawler']['layer_url'],
                    {'portal_source': 'current'})
            else:  # validation should make this unreachable
                raise ValueError(f'unsupported adapter {adapter}')

    def _seed_igs(self, portal, mine_ids=None):
        crawler = portal['crawler']
        target_ids = list(mine_ids or [])
        if target_ids:
            for mine_id in target_ids:
                if not re.fullmatch(r'[A-Z]{2}\d{4}', mine_id):
                    raise ValueError(f'invalid IGSID {mine_id!r}')
                url = crawler['detail_url_template'].format(mine_id=mine_id)
                self.db.enqueue(
                    portal['id'], f'{portal["id"]}:legacy-detail:{mine_id}',
                    'html_detail', url,
                    {'portal_source': 'legacy', 'mine_id': mine_id})
            self.db.enqueue(
                portal['id'], f'{portal["id"]}:arcgis-hub-metadata',
                'arcgis_hub_metadata', crawler['current_hub_dataset_url'],
                {'portal_source': 'current', 'target_ids': target_ids})
            return
        # Acceptance seeds lead the frontier on a full run as well.  Their
        # newly discovered document tasks still share the durable queue.
        for mine_id in crawler.get('acceptance_seed_ids', []):
            url = crawler['detail_url_template'].format(mine_id=mine_id)
            self.db.enqueue(
                portal['id'], f'{portal["id"]}:legacy-detail:{mine_id}',
                'html_detail', url,
                {'portal_source': 'legacy', 'mine_id': mine_id})
        self.db.enqueue(
            portal['id'], f'{portal["id"]}:arcgis-hub-metadata',
            'arcgis_hub_metadata', crawler['current_hub_dataset_url'],
            {'portal_source': 'current', 'target_ids': []})
        rows = []
        for item in crawler['legacy_id_ranges']:
            prefix = item['prefix']
            for number in range(item['start'], item['end'] + 1):
                mine_id = f'{prefix}{number:04d}'
                url = crawler['detail_url_template'].format(mine_id=mine_id)
                rows.append({
                    'portal_id': portal['id'],
                    'task_key': f'{portal["id"]}:legacy-detail:{mine_id}',
                    'kind': 'html_detail', 'url': url,
                    'payload': {'portal_source': 'legacy', 'mine_id': mine_id},
                })
        self.db.enqueue_many(rows)

    def _seed_azgs(self, portal):
        crawler = portal['crawler']
        url = _query_url(crawler['metadata_url'], {
            'collection_group': crawler['collection_group'],
            'offset': 0, 'limit': crawler['page_size']})
        self.db.enqueue(
            portal['id'], f'{portal["id"]}:metadata:0', 'azgs_metadata', url,
            {'offset': 0})

    def run(self, portal_ids, max_tasks=None, full_scope=True):
        processed = 0
        while max_tasks is None or processed < max_tasks:
            task = self.db.claim(portal_ids)
            if task is None:
                break
            portal = self.registry[task['portal_id']]
            try:
                self._process(task, portal)
                self.db.finish(task['task_key'])
            except PermanentSkip as exc:
                if task['kind'] == 'document':
                    self.db.candidate(task['payload'], 'skipped', str(exc))
                self.db.finish(task['task_key'], 'skipped', str(exc))
            except TransientHarvestError as exc:
                self.db.retry(task, str(exc))
            except Exception as exc:
                # Parser/schema changes are deterministic and should be visible,
                # not retried forever as if they were network noise.
                self.db.finish(task['task_key'], 'error', str(exc))
            processed += 1
        self.db.maybe_complete(portal_ids, full_scope=full_scope)
        return processed

    def _process(self, task, portal):
        kind = task['kind']
        if kind == 'arcgis_inventory':
            return self._arcgis_inventory(task, portal)
        if kind == 'arcgis_hub_metadata':
            return self._arcgis_hub_metadata(task, portal)
        if kind == 'arcgis_page':
            return self._arcgis_page(task, portal)
        if kind == 'arcgis_verify':
            return self._arcgis_verify(task, portal)
        if kind == 'arcgis_attachment_list':
            return self._arcgis_attachment_list(task, portal)
        if kind == 'html_detail':
            return self._html_detail(task, portal)
        if kind == 'html_index':
            return self._html_index(task, portal)
        if kind == 'azgs_metadata':
            return self._azgs_metadata(task, portal)
        if kind == 'nbmg_index':
            return self._nbmg_index(task, portal)
        if kind == 'document':
            return self._document(task, portal)
        raise HarvestError(f'unknown task kind {kind!r}')

    def _arcgis_inventory(self, task, portal):
        client = self.client(portal)
        layer_url = task['url'].rstrip('/')
        metadata, _, _ = client.json(_query_url(layer_url, {'f': 'json'}))
        capabilities = {part.strip().casefold() for part in
                        str(metadata.get('capabilities') or '').split(',')}
        if 'query' not in capabilities:
            raise HarvestError('ArcGIS layer no longer advertises Query')
        has_attachments = metadata.get('hasAttachments') is True
        self.db.observe(portal['id'], 'arcgis_has_attachments', has_attachments)
        ids_result, _, _ = client.json(_query_url(layer_url + '/query', {
            'where': '1=1', 'returnIdsOnly': 'true', 'f': 'json'}))
        ids = ids_result.get('objectIds')
        if not isinstance(ids, list):
            raise HarvestError('ArcGIS inventory has no objectIds array')
        normalized = sorted(int(value) for value in ids)
        if len(normalized) != len(set(normalized)):
            raise HarvestError('ArcGIS inventory contains duplicate object IDs')
        inventory_hash = canonical_sha256(normalized)
        self.db.observe(portal['id'], 'arcgis_object_ids', {
            'count': len(normalized), 'sha256': inventory_hash})
        page_size = portal['crawler'].get('arcgis_page_size', 100)
        rows = []
        for start in range(0, len(normalized), page_size):
            page_ids = normalized[start:start + page_size]
            url = _query_url(layer_url + '/query', {
                'objectIds': ','.join(map(str, page_ids)),
                'outFields': '*', 'returnGeometry': 'false', 'f': 'json'})
            rows.append({
                'portal_id': portal['id'],
                'task_key': f'{portal["id"]}:arcgis-page:{start}',
                'kind': 'arcgis_page', 'url': url,
                'payload': {
                    'portal_source': task['payload'].get('portal_source', 'current'),
                    'object_ids': page_ids,
                    'has_attachments': has_attachments,
                },
            })
        self.db.enqueue_many(rows)
        verify_url = _query_url(layer_url + '/query', {
            'where': '1=1', 'returnIdsOnly': 'true', 'f': 'json'})
        self.db.enqueue(
            portal['id'], f'{portal["id"]}:arcgis-verify:{inventory_hash}',
            'arcgis_verify', verify_url,
            {'expected_sha256': inventory_hash, 'expected_count': len(normalized)})

    def _arcgis_hub_metadata(self, task, portal):
        """Probe current IGS attachment capability without evading robots.

        The underlying ``services.arcgis.com`` feature endpoint currently
        denies robots verification. ArcGIS Hub's official dataset metadata is
        crawlable and exposes the layer-wide record count and attachment flag,
        but it does not establish that any targeted IGSID is present. Keep that
        distinction explicit in the diff instead of relabeling a legacy fetch.
        """
        result, _, final_url = self.client(portal).json(task['url'])
        data = result.get('data') or {}
        attributes = data.get('attributes') or {}
        layer = attributes.get('layer') or {}
        expected_id = portal['crawler']['current_item_id'] + '_0'
        if data.get('id') != expected_id:
            raise HarvestError(
                f'ArcGIS Hub dataset identity changed: {data.get("id")!r}')
        attachments = layer.get('hasAttachments') is True
        record_count = attributes.get('recordCount')
        if not isinstance(record_count, int) or record_count < 0:
            raise HarvestError('ArcGIS Hub metadata lacks an integer recordCount')
        status = {
            'status': ('native_attachments_present_feature_inventory_blocked'
                       if attachments else 'no_native_attachments'),
            'metadata_url': final_url,
            'feature_service_url': portal['crawler']['current_layer_url'],
            'feature_service_access': 'robots_denied_http_403',
            'record_count': record_count,
            'has_attachments': attachments,
            'target_ids_verified': False,
            'target_ids': task['payload'].get('target_ids') or [],
            'external_detail_links': 'not_enumerated_access_blocked',
        }
        self.db.observe(portal['id'], 'arcgis_current_inventory', status)
        self.db.observe(portal['id'], 'arcgis_has_attachments', attachments)
        self.db.observe(portal['id'], 'crawl_completion_blocker', {
            'source': 'current_arcgis_feature_inventory',
            'reason': 'robots_denied_http_403',
            'effect': 'external detail links and exact IGSID inventory not enumerated',
        })
        if attachments:
            raise PermanentSkip(
                'current_feature_inventory_blocked_by_robots; attachments not crawled')

    def _arcgis_page(self, task, portal):
        result, _, _ = self.client(portal).json(task['url'])
        features = result.get('features')
        if not isinstance(features, list):
            raise HarvestError('ArcGIS page has no features array')
        crawler = portal['crawler']
        oid_field = result.get('objectIdFieldName') or 'OBJECTID'
        actual_ids = []
        for feature in features:
            attributes = feature.get('attributes') or {}
            if attributes.get(oid_field) is not None:
                actual_ids.append(int(attributes[oid_field]))
            mine_id = str(attributes.get(crawler.get('id_field', 'IGSID')) or '')
            if not mine_id:
                continue
            mine_name = str(attributes.get(crawler.get('name_field', 'NameList')) or '')
            metadata = {
                key: attributes.get(field) for key, field in
                (crawler.get('metadata_fields') or {}).items()}
            self.db.source_record(
                portal['id'], task['payload']['portal_source'], mine_id,
                mine_name, metadata)
            detail_template = crawler.get('detail_url_template')
            if detail_template:
                detail_url = detail_template.format(mine_id=mine_id)
                source = task['payload']['portal_source']
                self.db.enqueue(
                    portal['id'], f'{portal["id"]}:{source}-detail:{mine_id}',
                    'html_detail', detail_url,
                    {'portal_source': source, 'mine_id': mine_id,
                     'mine_name': mine_name, **metadata})
                # Current IDs also extend the bounded legacy sweep when a new
                # release appends an identifier above the reviewed range.
                if portal['id'] == 'igs_mines':
                    self.db.enqueue(
                        portal['id'], f'{portal["id"]}:legacy-detail:{mine_id}',
                        'html_detail', detail_url,
                        {'portal_source': 'legacy', 'mine_id': mine_id,
                         'mine_name': mine_name, **metadata})
            if task['payload'].get('has_attachments'):
                object_id = attributes.get(oid_field)
                layer_url = task['url'].split('/query?', 1)[0]
                attach_url = _query_url(layer_url + f'/{object_id}/attachments', {
                    'f': 'json'})
                self.db.enqueue(
                    portal['id'],
                    f'{portal["id"]}:attachments:{object_id}',
                    'arcgis_attachment_list', attach_url,
                    {'portal_source': task['payload']['portal_source'],
                     'mine_id': mine_id, 'mine_name': mine_name,
                     'object_id': object_id, **metadata})
        expected = task['payload'].get('object_ids') or []
        if actual_ids and sorted(actual_ids) != sorted(expected):
            raise HarvestError('ArcGIS exact object-ID page changed')

    def _arcgis_verify(self, task, portal):
        result, _, _ = self.client(portal).json(task['url'])
        ids = sorted(int(value) for value in result.get('objectIds') or [])
        if (len(ids) != task['payload']['expected_count'] or
                canonical_sha256(ids) != task['payload']['expected_sha256']):
            raise TransientHarvestError(
                'ArcGIS inventory mutated during enumeration; retry snapshot')

    def _arcgis_attachment_list(self, task, portal):
        result, _, _ = self.client(portal).json(task['url'])
        infos = result.get('attachmentInfos') or []
        if not isinstance(infos, list):
            raise HarvestError('invalid ArcGIS attachment list')
        base = task['url'].split('?', 1)[0]
        for info in infos:
            name = str(info.get('name') or '')
            if not re.search(portal['pdf_link_pattern'], name, re.I):
                continue
            source_url = base + '/' + str(info['id'])
            payload = _pdf_candidate_payload(
                portal, task['payload']['portal_source'], source_url,
                task['payload'].get('mine_id'), task['payload'].get('mine_name'),
                document_title=name, county=task['payload'].get('county'),
                trs=task['payload'].get('trs'), doc_type='attachment')
            self._enqueue_document(portal, payload)

    def _html_detail(self, task, portal):
        raw, _, final_url = self.client(portal).bytes(
            task['url'], 'text/html', max_bytes=24 * 1024 * 1024)
        page = parse_html(raw)
        mine_id = task['payload'].get('mine_id') or ''
        if mine_id and mine_id.casefold() not in page.text.casefold():
            # Legacy enumeration gaps commonly return an empty/lookup page.
            return
        mine_name = task['payload'].get('mine_name') or ''
        if page.h1:
            heading = re.sub(r'^\s*' + re.escape(mine_id) + r'\s*:\s*', '',
                             page.h1, flags=re.I)
            if heading and heading.casefold() != page.h1.casefold() or mine_id in page.h1:
                mine_name = heading.strip()
        county = task['payload'].get('county')
        trs = task['payload'].get('trs')
        county_match = re.search(
            r'County:\s*([A-Za-z .-]{2,40}?)(?=\s+(?:Idaho\s+PLSS|'
            r'Latitude|Longitude|Lat\b|Lon\b|24K\s+Quad|Property\b))',
            page.text, re.I)
        trs_match = re.search(
            r'(?:(?:Idaho\s+)?PLSS\s*(?:\(TRSQQ\))?|TRSQQ|'
            r'Township.Range.Section)\s*[: ]+('
            r'\d{1,2}[NS]\s+\d{1,3}[EW]\s+\d{1,2}(?:\s+[A-Z]{1,4})?)',
            page.text, re.I)
        county = county or (county_match.group(1).strip() if county_match else None)
        trs = trs or (trs_match.group(1).strip() if trs_match else None)
        self.db.source_record(
            portal['id'], task['payload']['portal_source'], mine_id,
            mine_name, {'county': county, 'trs': trs, 'detail_url': final_url})
        seen = set()
        for href, anchor in page.links:
            source_url = parse.urljoin(final_url, href)
            if source_url in seen or not re.search(
                    portal['pdf_link_pattern'], source_url, re.I):
                continue
            seen.add(source_url)
            filename = parse.unquote(os.path.basename(
                parse.urlsplit(source_url).path))
            title = (anchor if anchor and not re.search(
                r'\bdownload\b', anchor, re.I) else filename)
            doc_type = ('USGS mine-record extract' if '/MILS_MRDS/' in source_url
                        else 'property_file')
            if doc_type == 'USGS mine-record extract':
                record_id = os.path.splitext(filename)[0]
                title = f'{record_id}: {mine_name.upper()}' if mine_name else record_id
            payload = _pdf_candidate_payload(
                portal, task['payload']['portal_source'], source_url,
                mine_id, mine_name, county=county, trs=trs,
                document_title=title, doc_type=doc_type)
            self._enqueue_document(portal, payload)

    def _html_index(self, task, portal):
        raw, _, final_url = self.client(portal).bytes(
            task['url'], 'text/html', max_bytes=32 * 1024 * 1024)
        page = parse_html(raw)
        crawler = portal['crawler']
        depth = task['payload'].get('depth', 0)
        for href, anchor in page.links:
            url = parse.urljoin(final_url, href)
            if re.search(portal['pdf_link_pattern'], url, re.I):
                payload = _pdf_candidate_payload(
                    portal, 'catalog', url, '', anchor or os.path.basename(
                        parse.urlsplit(url).path),
                    document_title=anchor or os.path.basename(
                        parse.urlsplit(url).path), doc_type='publication')
                self._enqueue_document(portal, payload)
            elif crawler.get('detail_link_pattern') and re.search(
                    crawler['detail_link_pattern'], url, re.I):
                self.db.enqueue(
                    portal['id'], f'{portal["id"]}:detail:{url}',
                    'html_detail', url,
                    {'portal_source': 'catalog', 'mine_name': anchor})
            elif (depth < crawler.get('max_index_depth', 0) and
                  crawler.get('pagination_link_pattern') and re.search(
                      crawler['pagination_link_pattern'], url, re.I)):
                self.db.enqueue(
                    portal['id'], f'{portal["id"]}:index:{url}',
                    'html_index', url, {'depth': depth + 1})

    def _azgs_metadata(self, task, portal):
        result, _, _ = self.client(portal).json(task['url'])
        rows = result.get('data')
        if not isinstance(rows, list):
            raise HarvestError('AZGS metadata response has no data array')
        crawler = portal['crawler']
        for row in rows:
            collection_id = str(row.get('collection_id') or '')
            metadata = row.get('metadata') or {}
            if not collection_id or metadata.get('private') is True:
                continue
            title = str(metadata.get('title') or collection_id)
            keywords = metadata.get('keywords') or []
            places = [str(item.get('name')) for item in keywords
                      if isinstance(item, dict) and item.get('type') == 'place']
            county = next((place for place in places if place.endswith(' County')), None)
            trs = next((place for place in places if re.search(
                r'\bT\d+[NS]\s+R\d+[EW]\s+Sec\b', place, re.I)), None)
            self.db.source_record(
                portal['id'], 'current', collection_id, title,
                {'year': metadata.get('year'), 'county': county, 'trs': trs})
            for file_info in metadata.get('files') or []:
                filename = str(file_info.get('name') or '')
                if not re.search(portal['pdf_link_pattern'], filename, re.I):
                    continue
                source_url = (crawler['collection_file_base'].rstrip('/') + '/' +
                              parse.quote(collection_id, safe='') + '/' +
                              parse.quote(filename, safe=''))
                # AZGS metadata licenses vary by collection.  Only an explicit
                # public-domain declaration is executable under this scope.
                license_info = metadata.get('license') or {}
                license_text = ' '.join(str(license_info.get(key) or '')
                                        for key in ('type', 'url'))
                public_domain = bool(re.search(
                    r'public\s+domain|creativecommons\.org/publicdomain/',
                    license_text, re.I))
                payload = _pdf_candidate_payload(
                    portal, 'current', source_url, collection_id, title,
                    county=county, trs=trs, document_title=filename,
                    doc_date=str(metadata.get('year') or '') or None,
                    doc_type=str(file_info.get('type') or 'document'),
                    rights_status='public_domain' if public_domain else 'unverified',
                    rights_basis=(license_text if public_domain else None))
                self._enqueue_document(portal, payload)
        offset = int(task['payload']['offset'])
        total = int(result.get('collectionCount') or 0)
        next_offset = offset + len(rows)
        if rows and next_offset < total:
            url = _query_url(crawler['metadata_url'], {
                'collection_group': crawler['collection_group'],
                'offset': next_offset, 'limit': crawler['page_size']})
            self.db.enqueue(
                portal['id'], f'{portal["id"]}:metadata:{next_offset}',
                'azgs_metadata', url, {'offset': next_offset})
        self.db.observe(portal['id'], 'azgs_collection_count', total)

    def _nbmg_index(self, task, portal):
        raw, _, _ = self.client(portal).bytes(
            task['url'], 'text/html', max_bytes=16 * 1024 * 1024)
        text = raw.decode('utf-8', 'replace')
        match = re.search(r'\bmddata\s*=\s*', text)
        if not match:
            raise HarvestError('NBMG page no longer embeds mddata inventory')
        try:
            rows, _ = json.JSONDecoder().raw_decode(text[match.end():])
        except json.JSONDecodeError as exc:
            raise HarvestError(f'cannot decode NBMG mddata: {exc}') from exc
        if not isinstance(rows, list):
            raise HarvestError('NBMG mddata inventory is not an array')
        queued = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 10:
                continue
            document_id, district, county, title = map(str, row[:4])
            pdf_path = str(row[9] or '')
            if not re.search(portal['pdf_link_pattern'], pdf_path, re.I):
                continue
            source_url = parse.urljoin(portal['crawler']['document_base_url'], pdf_path)
            payload = _pdf_candidate_payload(
                portal, 'legacy_static_inventory', source_url, document_id,
                district, county=county, document_title=title or document_id,
                doc_date=str(row[5] or '') or None, doc_type='mining_district_file')
            self.db.candidate(
                payload, 'queued' if payload['rights_status'] == 'public_domain'
                else 'skipped', None if payload['rights_status'] == 'public_domain'
                else 'rights_unverified')
            if payload['rights_status'] != 'public_domain':
                continue
            queued.append({
                'portal_id': portal['id'],
                'task_key': self._document_key(payload), 'kind': 'document',
                'url': source_url, 'payload': payload})
            self.db.source_record(
                portal['id'], 'legacy_static_inventory', document_id, district,
                {'county': county, 'title': title})
        self.db.enqueue_many(queued)
        self.db.observe(portal['id'], 'nbmg_embedded_rows', len(rows))

    @staticmethod
    def _document_key(payload):
        identity = canonical_sha256([
            payload['portal_id'], payload['portal_source'],
            payload['source_url'], payload.get('mine_id') or ''])
        return f'{payload["portal_id"]}:document:{identity}'

    def _enqueue_document(self, portal, payload):
        if payload['rights_status'] != 'public_domain':
            self.db.candidate(payload, 'skipped', 'rights_unverified')
            return False
        if not payload.get('rights_basis'):
            self.db.candidate(payload, 'skipped', 'rights_basis_missing')
            return False
        self.db.candidate(payload, 'queued')
        if self.db.known_document(payload):
            self.db.candidate(payload, 'already_downloaded')
            return False
        return self.db.enqueue(
            portal['id'], self._document_key(payload), 'document',
            payload['source_url'], payload)

    def _document(self, task, portal):
        payload = task['payload']
        if payload.get('rights_status') != 'public_domain':
            raise PermanentSkip('rights_unverified')
        client = self.client(portal)
        with client.open(task['url'], accept='application/pdf') as response:
            length = response.headers.get('Content-Length')
            if length and int(length) > self.max_pdf_bytes:
                raise PermanentSkip(f'pdf_too_large:{length}')
            content_type = str(response.headers.get_content_type()).casefold()
            with tempfile.SpooledTemporaryFile(
                    max_size=32 * 1024 * 1024) as stream:
                digest = hashlib.sha256()
                count = 0
                prefix = b''
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    if len(prefix) < 8192:
                        prefix += chunk[:8192 - len(prefix)]
                    count += len(chunk)
                    if count > self.max_pdf_bytes:
                        raise PermanentSkip(
                            f'pdf_exceeds_{self.max_pdf_bytes}_bytes')
                    digest.update(chunk)
                    stream.write(chunk)
                if PAYWALL_RE.search(prefix):
                    raise PermanentSkip('paywall_html_detected')
                if not prefix.lstrip().startswith(PDF_MAGIC):
                    raise PermanentSkip(
                        f'not_pdf:{content_type or "unknown_content_type"}')
                sha256 = digest.hexdigest()
                known = self.db.hash_object(sha256)
                if known:
                    if known['bytes'] != count:
                        raise HarvestError('SHA-256 object byte count conflict')
                    s3_uri = known['s3_uri']
                else:
                    s3_uri = self.sink.put(
                        stream, sha256, count, 'application/pdf', portal['id'])
                result = {
                    'sha256': sha256, 'bytes': count, 's3_uri': s3_uri,
                    'content_type': 'application/pdf',
                    'etag': response.headers.get('ETag'),
                    'last_modified': response.headers.get('Last-Modified'),
                }
                self.db.record_document(payload, result)


def _select_portals(registry, values):
    if not values or values == ['all']:
        return sorted(portal_id for portal_id, row in registry.items()
                      if row['status'] == 'harvest_ready')
    missing = sorted(set(values) - set(registry))
    if missing:
        raise ValueError('unknown portal ids: ' + ', '.join(missing))
    return list(dict.fromkeys(values))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portals-dir', default=PORTALS_DIR)
    subparsers = parser.add_subparsers(dest='command', required=True)
    crawl = subparsers.add_parser('crawl')
    crawl.add_argument('--portal', action='append', default=[])
    crawl.add_argument('--mine-id', action='append', default=[],
                       help='targeted IGSID crawl; never marks a full cursor complete')
    crawl.add_argument('--queue', required=True)
    crawl.add_argument('--manifest', required=True)
    crawl.add_argument('--coverage', required=True)
    crawl.add_argument('--diff-dir')
    crawl.add_argument('--s3-bucket', required=True)
    crawl.add_argument('--s3-prefix', default='originals')
    crawl.add_argument('--max-tasks', type=int)
    crawl.add_argument('--refresh', action='store_true')
    export = subparsers.add_parser('export')
    export.add_argument('--queue', required=True)
    export.add_argument('--manifest', required=True)
    export.add_argument('--coverage', required=True)
    export.add_argument('--diff-dir')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        registry = load_registry(args.portals_dir)
        db = QueueDB(args.queue)
        if args.command == 'crawl':
            portal_ids = _select_portals(registry, args.portal)
            if args.refresh:
                db.refresh(portal_ids)
            sink = S3OriginalSink(args.s3_bucket, args.s3_prefix)
            harvester = Harvester(registry, db, sink)
            harvester.seed(portal_ids, mine_ids=args.mine_id)
            processed = harvester.run(
                portal_ids, max_tasks=args.max_tasks,
                full_scope=not bool(args.mine_id))
            print(f'processed {processed} tasks')
        manifest_count = db.write_manifest(args.manifest)
        db.write_coverage(args.coverage, registry)
        if args.diff_dir:
            os.makedirs(args.diff_dir, exist_ok=True)
            if 'igs_mines' in registry:
                db.write_diff(
                    os.path.join(args.diff_dir, 'igs_mines.json'), 'igs_mines')
        print(f'wrote {manifest_count} manifest rows')
        db.close()
        return 0
    except (PortalRegistryError, ValueError, RuntimeError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
