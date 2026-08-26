#!/usr/bin/env python3
"""Backfill source_url / rights_basis / public_domain onto ws13_documents.

The defect: ws13_worker.py has never recorded provenance. It inserts
ws13_documents from the SQS message's `meta` dict, and that dict carries
portal, state, mine ids/names, county, TRS, doc_date, doc_type and title --
and nothing else, because until pipelines/ws13_migrations.sql there were no
columns for a source URL or a licence. So all 56,282 already-indexed
documents have a NULL source_url and a NULL rights_basis, and the citation
contract can resolve none of them: no `[title, p. N](source_url)` markdown,
and no rights_basis to fold into the CC BY-NC-SA and research-copy terms that
make serving the licensed and research corpora defensible at all.

The only alternative is re-OCRing 56,282 PDFs to recover metadata that is
already sitting in the WS12 harvest manifest under the same sha256. This
script reads that manifest instead.

Fail-closed properties:

  * COALESCE(new, old) on every column, so a manifest gap can never blank a
    value that is already populated;
  * the manifest's own admission_class is cross-checked against the storage
    prefix in s3_uri AND against ws13_documents.admission_class, and a
    document whose rights class disagrees between the two is refused rather
    than written -- stamping an AZGS CC BY-NC-SA rights_basis onto a row
    stored under ws12/originals/ is a licence error, not a typo;
  * an IS DISTINCT FROM guard on the UPDATE, so a rerun reports 0 updated
    rather than 56,282 and the row counts mean something;
  * every document is accounted for in the closing report: matched, updated,
    already-current, unmatched, refused.

Nothing used to prove this had run. ws13_migrate.py --check reported only
that the three columns exist, so a green preflight was compatible with a
corpus in which every one of the 45,325 licensed and research copies had a
NULL rights_basis. Run this with --require-complete, and gate the WS13
retrieval flag on ws13_migrate.py --check --require-provenance, which
measures the same population from the other side.

Usage:

    ws13_backfill_provenance.py --bucket nw-mineral-monitor-... --dry-run
    ws13_backfill_provenance.py --manifest var/ws12/manifest.jsonl
    ws13_backfill_provenance.py --manifest ... --require-complete
"""
import argparse
import gzip
import json
import os
import sys

import psycopg

DEFAULT_MANIFEST_KEY = 'ws13/fleet/manifest.jsonl'
# Rows per UPDATE. 56,282 documents is ~113 statements at 500; large enough
# that the per-statement round trip is noise, small enough that one failure
# does not roll back the whole corpus.
BATCH = 500

# The manifest names the rights class; S3 stores it as the second path
# segment; ws13_documents.admission_class is generated from that same
# segment. All three must agree or the document is refused.
MANIFEST_CLASS_TO_PREFIX = {
    'public_domain': 'originals',
    'cc_by_nc_sa_licensed': 'licensed-copies',
    'state_archive_research_copy': 'research-copies',
}
KNOWN_CLASSES = frozenset(MANIFEST_CLASS_TO_PREFIX.values())
# The two classes whose citation carries a licence or a retention rationale.
# 'originals' are public domain: a NULL rights_basis there costs nothing at
# serve time, while for these two infra/ws13_query_lambda.py's rights_for()
# raises rather than emit a citation it cannot state terms for. The same
# split is what ws13_migrate.py --check --require-provenance measures.
RIGHTS_BEARING_CLASSES = frozenset(('licensed-copies', 'research-copies'))

UPDATE_SQL = (
    'UPDATE ws13_documents d'
    '   SET source_url    = COALESCE(v.source_url, d.source_url),'
    '       rights_basis  = COALESCE(v.rights_basis, d.rights_basis),'
    '       public_domain = COALESCE(v.public_domain, d.public_domain)'
    '  FROM (VALUES {rows})'
    '       AS v (sha256, source_url, rights_basis, public_domain)'
    ' WHERE d.sha256 = v.sha256'
    '   AND (d.source_url IS DISTINCT FROM'
    '            COALESCE(v.source_url, d.source_url)'
    '     OR d.rights_basis IS DISTINCT FROM'
    '            COALESCE(v.rights_basis, d.rights_basis)'
    '     OR d.public_domain IS DISTINCT FROM'
    '            COALESCE(v.public_domain, d.public_domain))'
)
VALUE_TUPLE = '(%s::text, %s::text, %s::text, %s::boolean)'


def storage_class(s3_uri):
    """The rights prefix in 's3://bucket/ws12/<class>/...', or None.

    Deliberately not a regex over the whole URI: only the segment that
    ws13_documents.admission_class is generated from counts, and anything
    that is not shaped like a WS12 corpus key must read as unknown rather
    than as a lucky substring match.
    """
    parts = (s3_uri or '').split('/', 4)
    if len(parts) < 5 or parts[0] != 's3:' or parts[3] != 'ws12':
        return None
    return parts[4].split('/', 1)[0] or None


def manifest_lines(args):
    """Yield manifest lines from a local path or from S3."""
    if args.manifest:
        opener = gzip.open if args.manifest.endswith('.gz') else open
        with opener(args.manifest, 'rt', encoding='utf-8') as handle:
            for line in handle:
                yield line
        return
    import boto3
    client = boto3.client('s3', region_name=args.region)
    body = client.get_object(Bucket=args.bucket, Key=args.manifest_key)['Body']
    if args.manifest_key.endswith('.gz'):
        for raw in gzip.GzipFile(fileobj=body):
            yield raw.decode('utf-8', 'replace')
        return
    for raw in body.iter_lines():
        yield raw.decode('utf-8', 'replace')


def load_provenance(manifest=None, bucket=None,
                    manifest_key=DEFAULT_MANIFEST_KEY, region=None):
    """load_manifest() for callers that do not have an argparse namespace.

    pipelines/ws13_seed.py and pipelines/ws13_enqueue.py both need the same
    sha -> provenance map this module builds, and both used to reimplement
    part of it. Two implementations of "which manifest row wins" is how the
    seed path and the backfill path ended up able to write different
    rights_basis values for the same document.
    """
    return load_manifest(argparse.Namespace(
        manifest=manifest, bucket=bucket, manifest_key=manifest_key,
        region=region))


def load_manifest(args):
    """Collapse the manifest to one provenance record per sha256.

    The manifest carries one row per (document, mine, source occurrence) --
    106,396 rows for 68,809 distinct documents -- so a sha is seen several
    times. First occurrence in file order wins for any field it actually
    supplies, and a field it leaves empty is filled from a later occurrence
    rather than left NULL, since a missing rights_basis costs a citation.
    This function is the only implementation of that rule: ws13_seed.py and
    ws13_enqueue.py call it through load_provenance() instead of collapsing
    the manifest themselves.
    """
    records = {}
    stats = {'rows': 0, 'malformed': 0, 'no_sha': 0, 'class_conflict': 0}
    conflicts = {}
    for line in manifest_lines(args):
        line = line.strip()
        if not line:
            continue
        stats['rows'] += 1
        try:
            row = json.loads(line)
        except ValueError:
            stats['malformed'] += 1
            continue
        sha = (row.get('sha256') or '').strip()
        if not sha:
            stats['no_sha'] += 1
            continue
        stored = storage_class(row.get('s3_uri'))
        declared = MANIFEST_CLASS_TO_PREFIX.get(row.get('admission_class'))
        if stored and declared and stored != declared:
            # The manifest contradicts itself about where the bytes live.
            stats['class_conflict'] += 1
            conflicts.setdefault(sha, set()).add(f'{declared}!={stored}')
            continue
        rights_class = stored or declared
        record = records.get(sha)
        if record is None:
            records[sha] = {
                'source_url': _clean(row.get('source_url')),
                'rights_basis': _clean(row.get('rights_basis')),
                'public_domain': _as_bool(row.get('public_domain')),
                'admission_class': rights_class,
            }
            continue
        if rights_class and record['admission_class'] not in (None, rights_class):
            # Same bytes manifested under two different rights prefixes. WS12
            # dedupes on sha globally so this should be impossible; if it ever
            # happens the document is refused, not guessed at.
            conflicts.setdefault(sha, set()).update(
                {record['admission_class'], rights_class})
            continue
        # Fill only what the first occurrence left empty; never overwrite.
        for field in ('source_url', 'rights_basis'):
            if record[field] is None:
                record[field] = _clean(row.get(field))
        if record['public_domain'] is None:
            record['public_domain'] = _as_bool(row.get('public_domain'))
        if record['admission_class'] is None:
            record['admission_class'] = rights_class
    for sha in conflicts:
        records.pop(sha, None)
    stats['documents'] = len(records)
    stats['refused_conflicting'] = len(conflicts)
    return records, stats, conflicts


def _clean(value):
    text = str(value).strip() if value is not None else ''
    return text or None


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value in (None, ''):
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', 't', 'yes', '1'):
            return True
        if lowered in ('false', 'f', 'no', '0'):
            return False
    return None


def plan(conn, records, fill_only=False):
    """Compare the manifest against ws13_documents. Returns (updates, report).

    The whole documents table is 56,282 rows of small metadata, so it is
    cheaper and far more legible to read it once and diff in memory than to
    issue 56,282 comparisons -- and it makes --dry-run report exactly the
    numbers a real run would produce.

    By default the manifest wins over a populated column, because
    var/ws12/manifest.jsonl is the authoritative provenance record and a
    corrected rights_basis has to be able to propagate: stale attribution on
    a CC BY-NC-SA document is a licence problem, not a cosmetic one.
    --fill-only inverts that for an operator who has hand-corrected rows.
    Neither mode can write a NULL over a value: a field that must not change
    is sent as NULL and absorbed by the COALESCE in UPDATE_SQL.
    """
    updates = []
    report = {'documents': 0, 'matched': 0, 'unchanged': 0, 'unmatched': 0,
              'class_mismatch': 0, 'unknown_class': 0, 'missing_rights': 0,
              'missing_rights_licensed': 0}
    mismatched = []
    rows = conn.execute(
        'SELECT sha256, admission_class, source_url, rights_basis, '
        '       public_domain '
        '  FROM ws13_documents').fetchall()
    def count_missing(admission):
        report['missing_rights'] += 1
        if admission in RIGHTS_BEARING_CLASSES:
            report['missing_rights_licensed'] += 1

    for sha, admission, source_url, rights_basis, public_domain in rows:
        report['documents'] += 1
        if admission not in KNOWN_CLASSES:
            # The citation contract raises on an unknown admission_class, so
            # this document could never be cited. Count it, do not hide it.
            report['unknown_class'] += 1
        record = records.get(sha)
        if record is None:
            report['unmatched'] += 1
            if not rights_basis:
                count_missing(admission)
            continue
        if (record['admission_class'] and admission
                and record['admission_class'] != admission):
            report['class_mismatch'] += 1
            mismatched.append((sha, admission, record['admission_class']))
            if not rights_basis:
                count_missing(admission)
            continue
        report['matched'] += 1
        current = (source_url, rights_basis, public_domain)
        incoming = (record['source_url'], record['rights_basis'],
                    record['public_domain'])
        merged = tuple(_pick(have, want, fill_only)
                       for have, want in zip(current, incoming))
        if merged == current:
            report['unchanged'] += 1
        else:
            # NULL for every field that must stay as it is; UPDATE_SQL's
            # COALESCE turns that into "leave this column alone".
            updates.append((sha,) + tuple(
                None if new == old else new
                for new, old in zip(merged, current)))
        if not merged[1]:
            count_missing(admission)
    return updates, report, mismatched


def _pick(have, want, fill_only):
    """The value a column should end up with. Never None over a real value."""
    if want is None:
        return have
    if have is not None and fill_only:
        return have
    return want


def apply_updates(conn, updates, batch=BATCH):
    """Write the merged provenance back, in batches. Returns rows changed."""
    changed = 0
    for start in range(0, len(updates), batch):
        chunk = updates[start:start + batch]
        sql = UPDATE_SQL.format(rows=', '.join([VALUE_TUPLE] * len(chunk)))
        params = [value for row in chunk for value in row]
        changed += conn.execute(sql, params).rowcount
    return changed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--manifest',
                        help='local harvest manifest (.jsonl or .jsonl.gz)')
    parser.add_argument('--bucket', default=os.environ.get('WS13_BUCKET'),
                        help='read the manifest from S3 instead')
    parser.add_argument('--manifest-key', default=DEFAULT_MANIFEST_KEY,
                        help=f'S3 key (default {DEFAULT_MANIFEST_KEY})')
    parser.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'))
    parser.add_argument('--region',
                        default=os.environ.get('AWS_DEFAULT_REGION', 'us-west-2'))
    parser.add_argument('--batch', type=int, default=BATCH)
    parser.add_argument('--fill-only', action='store_true',
                        help='only populate NULL columns; leave any value '
                             'already in ws13_documents untouched')
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would change, write nothing')
    parser.add_argument('--require-complete', action='store_true',
                        help='exit 1 if any licensed or research copy still '
                             'lacks a rights_basis')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.dsn:
        sys.exit('need --dsn (or WS13_DB_DSN)')
    if args.manifest and args.bucket:
        # Two sources of truth for the rights record is one too many; say so
        # rather than silently preferring one.
        sys.exit('pass --manifest or --bucket, not both')
    if not args.manifest and not args.bucket:
        sys.exit('need --manifest PATH or --bucket BUCKET (or WS13_BUCKET)')
    if args.batch < 1:
        sys.exit('--batch must be at least 1')

    records, manifest_stats, conflicts = load_manifest(args)
    print(f'manifest rows read      : {manifest_stats["rows"]}')
    print(f'  malformed JSON        : {manifest_stats["malformed"]}')
    print(f'  rows without a sha256 : {manifest_stats["no_sha"]}')
    print(f'  rows refused (class)  : {manifest_stats["class_conflict"]}')
    print(f'distinct documents      : {manifest_stats["documents"]}')
    if conflicts:
        print(f'  shas refused for conflicting rights class: {len(conflicts)}')
        for sha in sorted(conflicts)[:10]:
            print(f'    {sha[:16]} {sorted(conflicts[sha])}')
    if not records:
        sys.exit('manifest yielded no usable provenance records')

    conn = psycopg.connect(args.dsn, autocommit=True)
    updates, report, mismatched = plan(conn, records, fill_only=args.fill_only)
    print(f'mode                    : '
          f'{"fill-only" if args.fill_only else "manifest-wins"}')
    print(f'ws13_documents rows     : {report["documents"]}')
    print(f'  matched in manifest   : {report["matched"]}')
    print(f'  already current       : {report["unchanged"]}')
    print(f'  to update             : {len(updates)}')
    print(f'  unmatched (no row)    : {report["unmatched"]}')
    print(f'  refused (class clash) : {report["class_mismatch"]}')
    print(f'  unknown admission     : {report["unknown_class"]}')
    for sha, stored, manifest_class in mismatched[:10]:
        print(f'    {sha[:16]} db={stored} manifest={manifest_class}')

    if args.dry_run:
        for sha, url, basis, public in updates[:5]:
            print(f'  would set {sha[:16]} public_domain={public} '
                  f'url={(url or "")[:48]}')
        if len(updates) > 5:
            print(f'  ... and {len(updates) - 5} more')
        changed = 0
    else:
        changed = apply_updates(conn, updates, args.batch)
        print(f'rows updated            : {changed}')

    remaining, remaining_licensed = conn.execute(
        'SELECT count(*), '
        '       count(*) FILTER (WHERE admission_class = ANY(%s)) '
        '  FROM ws13_documents '
        " WHERE rights_basis IS NULL OR rights_basis = ''",
        (sorted(RIGHTS_BEARING_CLASSES),)).fetchone()
    if args.dry_run:
        # The UPDATE has not run, so the live count still includes everything
        # this run would have filled. Report the projection, not the stale row.
        remaining = report['missing_rights']
        remaining_licensed = report['missing_rights_licensed']
    print(f'documents still lacking rights_basis: {remaining} '
          f'({remaining_licensed} of them licensed/research copies)')
    conn.close()

    if mismatched or conflicts:
        print('FAILED: at least one document has a contradictory rights '
              'class and was refused', file=sys.stderr)
        return 1
    if args.require_complete and remaining_licensed:
        # Deliberately the licensed/research subset, not every document: an
        # 'originals' row with no rights_basis is public domain and still
        # citable, while these raise out of rights_for() on every hit. This is
        # the same population ws13_migrate.py --check --require-provenance
        # gates on, so the two cannot disagree about whether the corpus is
        # ready to serve.
        print(f'FAILED: {remaining_licensed} licensed/research copies have '
              f'no rights_basis', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
