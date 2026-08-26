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
ws13_documents so a requeued document keeps its portal/state/mine joins and
its source_url / rights_basis / public_domain provenance.

That last part only ever held for documents that were already indexed
successfully -- which is the one population that does not need requeuing.
ws13_worker.py inserts into ws13_documents only on the success path, so the
17 status='error' rows and the 8 dead-lettered documents this tool exists to
requeue have a ws13_manifest row and no ws13_documents row at all; the LEFT
JOIN handed back NULL provenance, body_for() dropped the empty fields, and
the worker's ON CONFLICT COALESCE had no prior row to coalesce against, so
the document came back indexed with no rights. For a cc_by_nc_sa_licensed or
state_archive_research_copy document that is not a cosmetic loss:
infra/ws13_query_lambda.py refuses to emit a citation it cannot state terms
for. Provenance therefore falls back to the WS12 harvest manifest, and a
rights-bearing document whose rights_basis cannot be resolved from either
source is refused rather than silently enqueued.

Fail-closed: a selector is required, anything over --max-messages refuses to
run without --yes, and unresolvable rights refuse without
--allow-missing-rights.
"""
import argparse, json, os, sys
from collections import namedtuple

import boto3, psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ws13_backfill_provenance                            # noqa: E402

BATCH = 10          # SQS send_message_batch hard limit
DEFAULT_MAX = 2000  # refuse larger fan-outs unless --yes
# Same default ws13_seed.py uses, so the two read the same provenance file.
DEFAULT_MANIFEST = 'var/ws12/manifest.jsonl'
# The classes whose citation carries a licence or a retention rationale, and
# which infra/ws13_query_lambda.py refuses to cite without a rights_basis.
RIGHTS_BEARING = ws13_backfill_provenance.RIGHTS_BEARING_CLASSES

# The select() row, named so the provenance merge cannot pick the wrong
# offset out of a 17-column tuple.
Row = namedtuple('Row', 'sha256 s3_key doc_class pages status portal state '
                        'mine_ids mine_names county trs doc_date doc_type '
                        'title source_url rights_basis public_domain')


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
    p.add_argument('--harvest-manifest', default=DEFAULT_MANIFEST,
                   help=f'WS12 harvest manifest to read provenance from for '
                        f'documents that have no ws13_documents row yet '
                        f'(default {DEFAULT_MANIFEST})')
    p.add_argument('--bucket', help='read the harvest manifest from S3 '
                                    'instead of a local path')
    p.add_argument('--manifest-key',
                   default=ws13_backfill_provenance.DEFAULT_MANIFEST_KEY,
                   help='S3 key of the harvest manifest under --bucket')
    p.add_argument('--allow-missing-rights', action='store_true',
                   help='enqueue a licensed or research copy even when no '
                        'rights_basis can be resolved for it')
    return p.parse_args(argv)


def rights_class(s3_key):
    """The WS12 rights prefix in 'ws12/<class>/...', or None.

    The same segment ws13_documents.admission_class is generated from
    (split_part(s3_key, '/', 2)). It is read from ws13_manifest.s3_key here
    because a failed or dead-lettered document has no ws13_documents row, so
    the generated column does not exist to be read.
    """
    parts = (s3_key or '').split('/')
    if len(parts) < 3 or parts[0] != 'ws12':
        return None
    return parts[1] or None


def manifest_records(args):
    """sha -> provenance from the WS12 harvest manifest, or {} if unavailable.

    Reads it through ws13_backfill_provenance.load_provenance() rather than
    reimplementing the collapse: one rule for which of a sha's 106,396-row
    manifest occurrences supplies each field, shared with the backfill and
    with ws13_seed.py.
    """
    if args.bucket:
        source = f's3://{args.bucket}/{args.manifest_key}'
    elif not os.path.exists(args.harvest_manifest):
        # Only an explicitly named path is an error; the default is allowed
        # to be absent, and then the rights gate below is what speaks up.
        if args.harvest_manifest != DEFAULT_MANIFEST:
            sys.exit(f'{args.harvest_manifest}: no such harvest manifest')
        print(f'no harvest manifest at {args.harvest_manifest}: provenance '
              f'comes from ws13_documents only')
        return {}
    else:
        source = args.harvest_manifest
    records, stats, conflicts = ws13_backfill_provenance.load_provenance(
        manifest=None if args.bucket else args.harvest_manifest,
        bucket=args.bucket, manifest_key=args.manifest_key,
        region=os.environ.get('AWS_DEFAULT_REGION', 'us-west-2'))
    print(f'harvest manifest {source}: {stats["rows"]} rows, '
          f'{len(records)} documents'
          + (f', {len(conflicts)} refused for a conflicting rights class'
             if conflicts else ''))
    return records


def resolve_provenance(rows, records):
    """Provenance per sha, from ws13_documents first and the manifest second.

    Returns {sha: (source_url, rights_basis, public_domain)} and the
    [(sha, class)] whose rights_basis could not be resolved from either --
    the population that must not be enqueued, because ws13_worker.py would
    write a licensed or research copy with a NULL rights_basis and the
    retrieval Lambda raises rather than cite one.

    ws13_documents wins where it has a value: an operator may have corrected
    a row by hand, and a requeue must not undo that.
    """
    resolved, unresolved = {}, []
    for row in rows:
        record = records.get(row.sha256) or {}
        source_url = row.source_url or record.get('source_url')
        rights_basis = row.rights_basis or record.get('rights_basis')
        public_domain = row.public_domain
        if public_domain is None:
            public_domain = record.get('public_domain')
        resolved[row.sha256] = (source_url, rights_basis, public_domain)
        if not rights_basis:
            found = rights_class(row.s3_key)
            if found in RIGHTS_BEARING:
                unresolved.append((row.sha256, found))
    return resolved, unresolved


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

    # source_url / rights_basis / public_domain ride along with the rest of
    # the indexed metadata. Leaving them out would mean a requeued document
    # came back from the worker with its provenance blanked -- the citation
    # would stop resolving and, for the licensed and research copies, the
    # attribution the licence requires would silently disappear on rework.
    #
    # The join stays LEFT because ws13_documents has no row for a document
    # that has never been indexed successfully, which is most of what gets
    # requeued. Those three columns come back NULL for exactly those rows;
    # resolve_provenance() fills them from the harvest manifest afterwards.
    sql = ('SELECT m.sha256, m.s3_key, m.doc_class, m.pages, m.status, '
           '       d.portal, d.state, d.mine_ids, d.mine_names, d.county, '
           '       d.trs, d.doc_date, d.doc_type, d.title, '
           '       d.source_url, d.rights_basis, d.public_domain '
           '  FROM ws13_manifest m '
           '  LEFT JOIN ws13_documents d USING (sha256) '
           ' WHERE ' + ' AND '.join(where) +
           ' ORDER BY m.sha256')
    if args.limit:
        sql += ' LIMIT %s'
        params.append(args.limit)
    return [Row._make(row) for row in conn.execute(sql, params).fetchall()]


def body_for(row, force, provenance=None):
    """The SQS body for one selected row.

    `provenance` is resolve_provenance()'s map; it supplies the three fields
    for a document with no ws13_documents row, which is every document that
    failed or was dead-lettered.
    """
    source_url, rights_basis, public_domain = (provenance or {}).get(
        row.sha256, (row.source_url, row.rights_basis, row.public_domain))
    meta = {'portal': row.portal, 'state': row.state,
            'mine_ids': row.mine_ids or [], 'mine_names': row.mine_names or [],
            'county': row.county, 'trs': row.trs, 'doc_date': row.doc_date,
            'doc_type': row.doc_type, 'title': row.title,
            'source_url': source_url, 'rights_basis': rights_basis,
            'public_domain': public_domain}
    # `v not in (None, [])` drops empties but KEEPS public_domain=False:
    # False equals neither None nor [], and a public-domain flag of False is
    # the whole point for the licensed and research copies. Do not "simplify"
    # this to a truthiness test.
    body = {'sha256': row.sha256, 'key': row.s3_key, 'cls': row.doc_class,
            'pages': row.pages,
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
    if args.bucket and args.harvest_manifest != DEFAULT_MANIFEST:
        # Two sources of truth for the rights record is one too many; the
        # same refusal ws13_backfill_provenance.py makes.
        sys.exit('pass --harvest-manifest or --bucket, not both')

    conn = psycopg.connect(args.dsn, autocommit=True)
    rows = select(conn, args)
    if not rows:
        print('nothing matched the selector')
        return 0

    by_status, by_cls = {}, {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_cls[r.doc_class] = by_cls.get(r.doc_class, 0) + 1
    print(f'selected {len(rows)} documents')
    print('  by status:', dict(sorted(by_status.items())))
    print('  by class :', dict(sorted(by_cls.items())))

    skipped = [r for r in rows if r.status == 'done' and not args.force]
    if skipped:
        print(f'  note: {len(skipped)} are already done and will be skipped by '
              f'the worker without --force')

    # Only read the 106,396-row manifest when something actually needs it:
    # a re-extraction of already-indexed documents has its provenance in
    # ws13_documents already.
    gaps = any(r.rights_basis is None or r.source_url is None for r in rows)
    provenance, unresolved = resolve_provenance(
        rows, manifest_records(args) if gaps else {})
    from_manifest = sum(
        1 for r in rows
        if not r.rights_basis and provenance[r.sha256][1])
    print(f'  provenance: {from_manifest} rights_basis values came from the '
          f'harvest manifest (no ws13_documents row)')
    if unresolved:
        classes = {}
        for _sha, found in unresolved:
            classes[found] = classes.get(found, 0) + 1
        print(f'  {len(unresolved)} selected documents have no rights_basis in '
              f'ws13_documents or the manifest: {dict(sorted(classes.items()))}',
              file=sys.stderr)
        for sha, found in unresolved[:10]:
            print(f'    {sha[:16]} {found}', file=sys.stderr)
    refusal = ('refusing to enqueue a licensed or research copy whose '
               'rights_basis cannot be resolved: point --harvest-manifest at '
               'the WS12 manifest (or pass --bucket), or pass '
               '--allow-missing-rights to send them and accept that they '
               'will be indexed uncitable')
    refuse = bool(unresolved) and not args.allow_missing_rights
    if refuse and not args.dry_run:
        sys.exit(refusal)

    if len(rows) > args.max_messages and not args.yes:
        sys.exit(f'refusing to send {len(rows)} messages (--max-messages='
                 f'{args.max_messages}); pass --yes to confirm')

    if args.dry_run:
        for r in rows[:20]:
            print('  would queue', r.sha256[:16], r.doc_class, r.status,
                  'rights=' + ('yes' if provenance[r.sha256][1] else 'NONE'))
        if len(rows) > 20:
            print(f'  ... and {len(rows) - 20} more')
        if refuse:
            # A dry run reports rather than aborts, but it still fails: this
            # is the preflight for the real send.
            print(refusal, file=sys.stderr)
            return 1
        return 0

    sqs = boto3.client('sqs', region_name=os.environ.get(
        'AWS_DEFAULT_REGION', 'us-west-2'))
    sent = failed = 0
    batch = []
    for r in rows:
        batch.append({'Id': str(len(batch)),
                      'MessageBody': json.dumps(
                          body_for(r, args.force, provenance))})
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
