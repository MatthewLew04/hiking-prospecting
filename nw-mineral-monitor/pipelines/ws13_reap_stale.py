#!/usr/bin/env python3
"""Reap WS13 manifest rows stranded at status='running'.

The defect: ws13_worker.py stamps status='running' before it starts a
document and writes a terminal status only at the very end, so a worker that
dies mid-document -- OOM, spot reclaim, an instance scaled in by hand, a
node self-terminating under a live worker -- leaves the row 'running'
forever. Nothing reaps it. ws13_enqueue.py selects on status and nobody
selects 'running', so the document is neither retried nor counted as a
failure; it is just quietly absent from the index. One document
(5c991bfa4e90) is in exactly that state right now.

Age is meaningful because a LIVE worker now says so: ws13_worker.py's
DocumentLease heartbeats `updated_at` every WS13_HEARTBEAT_SECONDS while it
works a document, so an old updated_at means abandoned rather than slow. See
MIN_SAFE_HOURS for the arithmetic.

This moves aged-out 'running' rows to 'error' with a reason that names the
stale reap and the age -- the state ws13_enqueue.py already knows how to put
back on the work queue:

    ws13_reap_stale.py --dsn "$WS13_DB_DSN"           # report, change nothing
    ws13_reap_stale.py --dsn "$WS13_DB_DSN" --apply
    ws13_enqueue.py --status error                    # then requeue

Dry run is the default; --apply is the only mutating path. Rerun-safe: a
reaped row is no longer 'running', so a second pass selects nothing.

Fail closed in three places, because moving a row a worker still owns would
race a document that is about to be marked 'done':
  * rows whose worker_id has written any other manifest row in the last
    LIVE_WORKER_MINUTES are reported and left alone;
  * rows with a NULL updated_at cannot be aged, so they are counted and left
    alone rather than reaped on a guess;
  * every UPDATE is a compare-and-set on the exact (status, worker_id,
    updated_at) that was read, so a row touched between the SELECT and the
    UPDATE is reported as skipped, never overwritten.
"""
import argparse
import os
import re
import sys

import psycopg

DEFAULT_HOURS = 4.0
# Floor for --older-than-hours, and the arithmetic behind it.
#
# This used to argue that four hours was safe because WS13_MAX_DOC_SECONDS
# caps each ocrmypdf container at 3300 s and SQS redelivers after the 3600 s
# visibility timeout. Neither bound was a bound on the DOCUMENT: the per-page
# confidence pass ran outside MAX_DOC_SECONDS with no aggregate cap, the
# tier-1 escalation doubled both the OCR and the confidence work, and
# updated_at was written only at status transitions -- so a worker
# legitimately busy for hours on one large scan looked exactly like an
# abandoned row, and reaping it started a SECOND worker on the same sha256
# while the first was still writing ws13_chunks for it.
#
# ws13_worker.py now bounds both. A DocumentLease gives each document a hard
# WS13_DOC_BUDGET_SECONDS (7200 s) budget that every phase timeout is clamped
# to, and heartbeats `UPDATE ws13_manifest SET updated_at=now()` every
# WS13_HEARTBEAT_SECONDS (120 s) while it works. The only gap that can exceed
# that is one uninterruptible container run, WS13_MAX_DOC_SECONDS = 3300 s
# (0.92 h). So a row belonging to a live worker is at most ~1 h stale: the
# 2 h floor keeps better than 2x margin and the 4 h default keeps 4x.
MIN_SAFE_HOURS = 2.0
# Same liveness window tools/ws13_status.sh uses to count live workers. Now
# that the worker heartbeats, this is a second line of defence rather than
# the only one -- a worker stuck on a single long document writes no OTHER
# manifest row, which is exactly the case this predicate used to miss.
LIVE_WORKER_MINUTES = 10
# sha256 ids are hex by construction. Anything else in --sha is an operator
# error, and one that would otherwise widen the selector instead of
# narrowing it -- see sha_selector().
SHA_RE = re.compile(r'[0-9a-f]{1,64}')
REAPER_ID = f'reap_stale:{os.uname().nodename}'

# The liveness EXISTS runs once per candidate row, and candidates are counted
# in single digits (one today), so the seq scan it costs is irrelevant next
# to reaping a document out from under a running worker.
SELECT_STALE = """
SELECT m.sha256, m.doc_class, m.worker_id, m.updated_at,
       EXTRACT(EPOCH FROM (now() - m.updated_at)) / 3600.0 AS age_hours,
       d.processed_at,
       EXISTS (SELECT 1 FROM ws13_manifest w
                WHERE w.worker_id = m.worker_id
                  AND w.sha256 <> m.sha256
                  AND w.updated_at > now() - %s * INTERVAL '1 minute')
         AS worker_live
  FROM ws13_manifest m
  LEFT JOIN ws13_documents d USING (sha256)
 WHERE m.status = 'running'
   AND m.updated_at IS NOT NULL
   AND m.updated_at < now() - %s * INTERVAL '1 hour'
"""

# Compare-and-set, not SELECT ... FOR UPDATE: this connection is autocommit,
# and the WS13 backfill already paid for the lesson that row locks taken on
# an autocommit connection are released before the next statement runs. The
# WHERE clause is the whole guard, so it cannot be lost that way.
REAP_ONE = """
UPDATE ws13_manifest
   SET status = 'error', error = %s, worker_id = %s, updated_at = now()
 WHERE sha256 = %s
   AND status = 'running'
   AND updated_at = %s
   AND worker_id IS NOT DISTINCT FROM %s
"""


def sha_selector(value):
    """Validate one --sha value as hex before it becomes a LIKE pattern.

    The selector is `m.sha256 LIKE %s` with the value + '%'. The parameter is
    bound, so there is no injection, but the value is still a PATTERN: an
    empty string (a shell variable that did not expand) produced LIKE '%',
    which matches every stale running row, so `--sha "$SHA" --apply` with SHA
    unset would reap the entire aged-out backlog instead of one named
    document. '_' widened it one character at a time for the same reason.
    """
    v = value.strip().lower()
    if not SHA_RE.fullmatch(v):
        raise argparse.ArgumentTypeError(
            f'{value!r} is not a sha256 or a sha256 prefix (1-64 hex '
            f'characters). Empty or wildcard values would widen the '
            f'selector, not restrict it.')
    return v


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'))
    p.add_argument('--older-than-hours', type=float, default=DEFAULT_HOURS,
                   help=f'age of the last manifest write (default '
                        f'{DEFAULT_HOURS:g}h)')
    p.add_argument('--sha', action='append', default=[], type=sha_selector,
                   help='restrict to these sha256 values (repeatable; full '
                        'or unique hex prefix)')
    p.add_argument('--limit', type=int,
                   help='cap the number of rows reaped (applied after the '
                        'live-worker filter, so it caps reaps, not reads)')
    p.add_argument('--apply', action='store_true',
                   help="move the selected rows to status='error'")
    p.add_argument('--dry-run', action='store_true',
                   help='select and report, change nothing (the default)')
    p.add_argument('--allow-short-window', action='store_true',
                   help=f'permit --older-than-hours below {MIN_SAFE_HOURS:g}h')
    return p.parse_args(argv)


def reap_reason(age_hours, threshold_hours, worker_id):
    """Machine-greppable, and it names the age that justified the reap."""
    return (f'stale_running_reaped: no manifest write for {age_hours:.1f}h '
            f'(threshold {threshold_hours:g}h, last worker '
            f'{worker_id or "unknown"})')


def select_stale(conn, args):
    sql = SELECT_STALE
    params = [LIVE_WORKER_MINUTES, args.older_than_hours]
    if args.sha:
        # Prefixes, so the 12-char ids the status tooling prints paste in.
        sql += ('   AND (m.sha256 = ANY(%s) OR ' +
                ' OR '.join(['m.sha256 LIKE %s'] * len(args.sha)) + ')\n')
        params.append(args.sha)
        params.extend(s + '%' for s in args.sha)
    # Deliberately unlimited: --limit caps what is REAPED, and whether a row
    # is reapable is decided by the worker_live column below, in Python. A
    # SQL LIMIT here counted held rows against the operator's budget, so
    # `--limit 10 --apply` against 40 stale rows whose 10 oldest were all
    # held reaped nothing at all and reported 'reaped 0, skipped 0'.
    sql += ' ORDER BY m.updated_at'
    return conn.execute(sql, params).fetchall()


def describe(row):
    sha, cls, worker, updated, age, processed, live = row
    notes = []
    if processed is not None and updated is not None and processed > updated:
        # The data transaction committed but set_status never ran: the worker
        # died between the two. Requeueing re-does the work, which is
        # idempotent, and is still better than leaving the row invisible.
        notes.append('documents row written after the running stamp')
    if live:
        notes.append(f'worker wrote another row in the last '
                     f'{LIVE_WORKER_MINUTES}m: NOT reaped')
    note = ('  [' + '; '.join(notes) + ']') if notes else ''
    return (f'  {sha[:16]} {(cls or "?"):12} {age:7.1f}h '
            f'worker={worker or "unknown"}{note}')


def main(argv=None):
    args = parse_args(argv)
    if not args.dsn:
        sys.exit('need --dsn (or WS13_DB_DSN)')
    if args.apply and args.dry_run:
        sys.exit('--apply and --dry-run are mutually exclusive')
    if args.older_than_hours < MIN_SAFE_HOURS and not args.allow_short_window:
        sys.exit(f'refusing a {args.older_than_hours:g}h window: a live '
                 f'worker can leave its row untouched for one container run '
                 f'(WS13_MAX_DOC_SECONDS 3300 s = 0.92h) between heartbeats, '
                 f'so anything under {MIN_SAFE_HOURS:g}h can reap a document '
                 f'that is still being written. Pass --allow-short-window if '
                 f'you know the fleet is stopped.')

    conn = psycopg.connect(args.dsn, autocommit=True)
    running, unageable = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE updated_at IS NULL)
             FROM ws13_manifest WHERE status = 'running'""").fetchone()
    print(f"{running} rows at status='running'")
    if unageable:
        # No timestamp means no way to prove the row is stale. Say so.
        print(f'  {unageable} have a NULL updated_at and cannot be aged; '
              f'left alone')

    rows = select_stale(conn, args)
    if not rows:
        print(f'nothing older than {args.older_than_hours:g}h to reap')
        return 0

    candidates = [r for r in rows if not r[6]]
    held = [r for r in rows if r[6]]
    print(f'{len(rows)} older than {args.older_than_hours:g}h '
          f'({len(candidates)} reapable, {len(held)} held by a live worker)')
    for row in rows:
        print(describe(row))
    if args.limit and len(candidates) > args.limit:
        deferred = len(candidates) - args.limit
        candidates = candidates[:args.limit]
        print(f'--limit {args.limit}: reaping the {args.limit} oldest '
              f'reapable row(s), leaving {deferred} for a later pass')

    if not args.apply:
        print(f'dry run: pass --apply to move {len(candidates)} row(s) to '
              f"status='error', then requeue with "
              f'ws13_enqueue.py --status error')
        return 0

    reaped = skipped = 0
    for row in candidates:
        sha, _cls, worker, updated, age, _processed, _live = row
        changed = conn.execute(
            REAP_ONE,
            (reap_reason(age, args.older_than_hours, worker), REAPER_ID,
             sha, updated, worker)).rowcount
        if changed:
            reaped += 1
        else:
            skipped += 1
            print(f'  skipped {sha[:16]}: the row changed between the read '
                  f'and the write, so a worker still owns it')
    print(f'reaped {reaped}, skipped {skipped}')
    print('requeue them with: ws13_enqueue.py --status error')
    # A skip is a race, not a crash, but it must not read as a clean run.
    return 1 if skipped else 0


if __name__ == '__main__':
    raise SystemExit(main())
