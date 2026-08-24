#!/usr/bin/env python3
"""Enqueue WS13 documents onto the OCR work queue.

The gap this fills: after the initial seed there was no way to queue work at
all. ws13_seed.py takes an S3 conditional-put lock (ws13/fleet/seed.lock)
that is never released, so no node will ever re-seed; and the ad-hoc
requeue.py sends to the *dead-letter* queue URL, which ws13_worker.py never
polls -- messages sent there sit until an operator drains them by hand. So
the error backlog, the dead-lettered documents, and any re-OCR pass had no
path back into the pipeline.

This is a selector over ws13_manifest rather than over the inventory, so it
can target exactly the rows that need rework:

    # the 17 rows sitting in status='error'
    ws13_enqueue.py --status error

    # named documents, reprocessing even if already done
    ws13_enqueue.py --sha ab19e3fb1093db49 --sha 276edff8c8a00520 --force

    # every born_digital document, re-extracted
    ws13_enqueue.py --status done --cls born_digital --force

Message bodies match ws13_seed.py exactly, with metadata read back from
ws13_documents so a requeued document keeps its portal/state/mine joins.
Fail-closed: a selector is required, and anything over --max-messages
refuses to run without --yes.
"""
import argparse, json, os, sys
import boto3, psycopg

BATCH = 10          # SQS send_message_batch hard limit
DEFAULT_MAX = 2000  # refuse larger fan-outs unless --yes


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--queue-url', default=os.environ.get('WS13_QUEUE_URL'))
    p.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'))
    p.add_argument('--status', action='append', default=[],
                   help='manifest status to select (repeatable)')
    p.add_argument('--sha', action='append', default=[],
                   help='explicit sha256 (repeatable; full or unique prefix)')
    p.add_argument('--sha-file', help='file of sha256 values, one per line')
    p.add_argument('--cls', action='append', default=[],
                   help='restrict to these doc_class values (repeatable)')
    p.add_argument('--force', action='store_true',
                   help="set force=true so the worker reprocesses a 'done' document")
    p.add_argument('--limit', type=int, help='cap the number selected')
    p.add_argument('--max-messages', type=int, default=DEFAULT_MAX)
    p.add_argument('--yes', action='store_true',
                   help='confirm a send larger than --max-messages')
    p.add_argument('--dry-run', action='store_true',
                   help='select and report, send nothing')
    return p.parse_args(argv)


def select(conn, args):
    """Rows to enqueue, joined to their indexed metadata."""
    where, params = [], []
    if args.status:
        where.append('m.status = ANY(%s)')
        params.append(args.status)
    shas = list(args.sha)
    if args.sha_file:
        with open(args.sha_file) as fh:
            shas += [ln.strip() for ln in fh if ln.strip()]
    if shas:
        # Accept prefixes so operator-facing 12-char ids from reconcile.py
        # can be pasted straight in.
        where.append('(m.sha256 = ANY(%s) OR ' +
                     ' OR '.join(['m.sha256 LIKE %s'] * len(shas)) + ')')
        params.append(shas)
        params.extend(s + '%' for s in shas)
    if args.cls:
        where.append('m.doc_class = ANY(%s)')
        params.append(args.cls)
    if not where:
        sys.exit('refusing to run with no selector: pass --status, --sha, '
                 '--sha-file or --cls')

    sql = ('SELECT m.sha256, m.s3_key, m.doc_class, m.pages, m.status, '
           '       d.portal, d.state, d.mine_ids, d.mine_names, d.county, '
           '       d.trs, d.doc_date, d.doc_type, d.title '
           '  FROM ws13_manifest m '
           '  LEFT JOIN ws13_documents d USING (sha256) '
           ' WHERE ' + ' AND '.join(where) +
           ' ORDER BY m.sha256')
    if args.limit:
        sql += ' LIMIT %s'
        params.append(args.limit)
    return conn.execute(sql, params).fetchall()


def body_for(row, force):
    (sha, key, cls, pages, _status, portal, state, mine_ids, mine_names,
     county, trs, doc_date, doc_type, title) = row
    meta = {'portal': portal, 'state': state, 'mine_ids': mine_ids or [],
            'mine_names': mine_names or [], 'county': county, 'trs': trs,
            'doc_date': doc_date, 'doc_type': doc_type, 'title': title}
    body = {'sha256': sha, 'key': key, 'cls': cls, 'pages': pages,
            'meta': {k: v for k, v in meta.items() if v not in (None, [])}}
    if force:
        body['force'] = True
    return body


def main(argv=None):
    args = parse_args(argv)
    if not args.queue_url or not args.dsn:
        sys.exit('need --queue-url and --dsn (or WS13_QUEUE_URL / WS13_DB_DSN)')
    if 'dlq' in args.queue_url.lower() or 'dead' in args.queue_url.lower():
        # The exact mistake requeue.py made. No worker polls the DLQ.
        sys.exit(f'refusing to send to what looks like a dead-letter queue: '
                 f'{args.queue_url}')

    conn = psycopg.connect(args.dsn, autocommit=True)
    rows = select(conn, args)
    if not rows:
        print('nothing matched the selector')
        return 0

    by_status, by_cls = {}, {}
    for r in rows:
        by_status[r[4]] = by_status.get(r[4], 0) + 1
        by_cls[r[2]] = by_cls.get(r[2], 0) + 1
    print(f'selected {len(rows)} documents')
    print('  by status:', dict(sorted(by_status.items())))
    print('  by class :', dict(sorted(by_cls.items())))

    skipped = [r for r in rows if r[4] == 'done' and not args.force]
    if skipped:
        print(f'  note: {len(skipped)} are already done and will be skipped by '
              f'the worker without --force')

    if len(rows) > args.max_messages and not args.yes:
        sys.exit(f'refusing to send {len(rows)} messages (--max-messages='
                 f'{args.max_messages}); pass --yes to confirm')

    if args.dry_run:
        for r in rows[:20]:
            print('  would queue', r[0][:16], r[2], r[4])
        if len(rows) > 20:
            print(f'  ... and {len(rows) - 20} more')
        return 0

    sqs = boto3.client('sqs', region_name=os.environ.get(
        'AWS_DEFAULT_REGION', 'us-west-2'))
    sent = failed = 0
    batch = []
    for r in rows:
        batch.append({'Id': str(len(batch)),
                      'MessageBody': json.dumps(body_for(r, args.force))})
        if len(batch) == BATCH:
            sent, failed = flush(sqs, args.queue_url, batch, sent, failed)
            batch = []
    if batch:
        sent, failed = flush(sqs, args.queue_url, batch, sent, failed)

    print(f'queued {sent}, failed {failed}')
    depth = sqs.get_queue_attributes(
        QueueUrl=args.queue_url,
        AttributeNames=['ApproximateNumberOfMessages'])['Attributes']
    print(f"queue depth now ~{depth['ApproximateNumberOfMessages']}")
    return 1 if failed else 0


def flush(sqs, url, batch, sent, failed):
    resp = sqs.send_message_batch(QueueUrl=url, Entries=batch)
    sent += len(resp.get('Successful', []))
    for f in resp.get('Failed', []):
        failed += 1
        print(f"  FAILED {f.get('Id')}: {f.get('Code')} {f.get('Message')}",
              file=sys.stderr)
    return sent, failed


if __name__ == '__main__':
    raise SystemExit(main())
