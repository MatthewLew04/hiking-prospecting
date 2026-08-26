#!/usr/bin/env python3
"""Run the Phase E known-item set against live WS13 retrieval and gate cutover.

What this catches that nothing else does: a silently dead vector arm. With no
ws13_chunks_titan_hnsw index the ANN ORDER BY still returns the right rows --
from a sequential scan over 852,027 chunks -- and with the arm disabled the
lexical arm alone still answers most keyword questions. Both look like success
from the outside. So every item asserts three things, not one:

  1. a top-5 hit whose sha256 AND page are the fixture's (right page, not just
     the right document: pagination is preserved end to end and a citation to
     the wrong page is unverifiable);
  2. the fixture's quote inside the excerpt of ANY top-5 hit on that page --
     a page over 3,000 characters is several chunks, so the hit carrying the
     quote is not always the one that ranked highest -- whitespace-normalised
     on both sides because the excerpt collapses whitespace and the OCR text
     keeps the page's line breaks;
  3. at least one top-5 hit whose sources include "vector". This is the arm
     assertion. It fails loudly when the arm is disabled, when its reason is
     set, and when the index is missing.

A cutover run is refused outright (certify()) when the whole run never saw a
vector-sourced hit or when any response reported arms.vector.enabled false,
whatever the individual items asked for: expect_vector_source is a per-item
flag, and clearing it everywhere would otherwise turn the arm assertion off.

Two ways to reach retrieval, exactly one of which must be chosen:

  --function-name  invoke the deployed Lambda (the production path, including
                   its VPC route, its timeouts and its packaging)
  --dsn            import infra/ws13_query_lambda and call handler() in
                   process against that DSN, from an in-VPC host

The Lambda has no egress and cannot embed the query itself, so this runner
embeds with amazon.titan-embed-text-v2:0 at dimensions=1024, normalize=true
and passes query_vector in -- the same contract the ASK function uses. Without
an embedding the vector arm cannot run, so --no-embed is only accepted under
--shadow, where nothing is being certified.

    # 48-72 h shadow run: record, never fail the caller
    ws13_live_known_items.py --function-name nwmm-ws13-query --shadow \\
        --append var/ws13/known-items-shadow.jsonl

    # cutover gate: refuses an incomplete fixture, exits non-zero on any miss
    ws13_live_known_items.py --function-name nwmm-ws13-query
"""
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))

from test_ws13_known_items import load, normalize, require_complete

DEFAULT_FIXTURE = ROOT / 'tests' / 'fixtures' / 'ws13_known_items.json'
EMBED_MODEL = 'amazon.titan-embed-text-v2:0'
VECTOR_DIMS = 1024
TOP_K = 5
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_READY = 2


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--function-name',
                        help='deployed WS13 retrieval Lambda to invoke')
    target.add_argument('--dsn',
                        help='query directly by importing the handler in process')
    parser.add_argument('--fixture', default=str(DEFAULT_FIXTURE))
    parser.add_argument('--top-k', type=int, default=TOP_K,
                        help='hits examined per item (default 5)')
    parser.add_argument('--max-excerpt-chars', type=int, default=760)
    parser.add_argument('--ef-search', type=int, default=200)
    parser.add_argument('--region', default=os.environ.get(
        'AWS_DEFAULT_REGION', 'us-west-2'))
    parser.add_argument('--embed-model', default=EMBED_MODEL)
    parser.add_argument('--no-embed', action='store_true',
                        help='send no query_vector; only valid under --shadow')
    parser.add_argument('--shadow', action='store_true',
                        help='record results and always exit 0 unless the run '
                             'itself could not be performed')
    parser.add_argument('--allow-incomplete', action='store_true',
                        help='run against a fixture short of its target '
                             '(reports, but certifies nothing)')
    parser.add_argument('--out', help='write this run as a JSON report')
    parser.add_argument('--append',
                        help='append this run as one JSONL line, for a shadow '
                             'run that accumulates over days')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the items and the event shape, call nothing')
    args = parser.parse_args(argv)
    if args.no_embed and not args.shadow:
        parser.error('--no-embed disables the vector-arm assertion, so it is '
                     'only accepted with --shadow')
    if args.top_k < 1 or args.top_k > 25:
        parser.error('--top-k must be between 1 and 25')
    return args


def embedder(args):
    """A callable text -> 1024 floats, or None when embedding is off."""
    if args.no_embed:
        return None
    import boto3

    client = boto3.client('bedrock-runtime', region_name=args.region)

    def embed(text):
        body = json.dumps({'inputText': text, 'dimensions': VECTOR_DIMS,
                           'normalize': True})
        response = client.invoke_model(modelId=args.embed_model, body=body)
        vector = json.loads(response['body'].read())['embedding']
        if len(vector) != VECTOR_DIMS:
            raise RuntimeError(
                f'{args.embed_model} returned {len(vector)} dimensions, not '
                f'{VECTOR_DIMS}; the ANN column is vector(1024)')
        return vector

    return embed


def caller(args):
    """A callable event -> response, over the deployed Lambda or in process."""
    if args.function_name:
        import boto3

        client = boto3.client('lambda', region_name=args.region)

        def invoke(event):
            response = client.invoke(
                FunctionName=args.function_name,
                Payload=json.dumps(event).encode('utf-8'))
            payload = json.loads(response['Payload'].read() or b'null')
            if response.get('FunctionError'):
                raise RuntimeError(
                    f'{args.function_name} returned {response["FunctionError"]}'
                    f': {json.dumps(payload)[:400]}')
            return payload

        return invoke

    os.environ['WS13_DB_DSN'] = args.dsn
    # The vector arm is off unless the deployment says otherwise; a direct run
    # is checking the same code path, so ask for it explicitly.
    os.environ.setdefault('WS13_VECTOR_ARM', 'true')
    sys.path.insert(0, str(ROOT / 'infra'))
    import ws13_query_lambda

    return lambda event: ws13_query_lambda.handler(event, None)


def event_for(item, args, vector):
    return {
        'op': 'search',
        'query': item['question'],
        'query_vector': vector,
        'filters': {},
        'limit': args.top_k,
        'max_excerpt_chars': args.max_excerpt_chars,
        'ef_search': args.ef_search,
        'arms': ['lexical', 'vector'],
    }


def judge(item, response, top_k, expect_vector):
    """Per-item verdict: the reasons it failed, or an empty list.

    EVERY top-k hit on the fixture's (sha256, page) is considered, not the
    first one. ws13_chunks is keyed (sha256, page, ordinal) and
    pipelines/ws13_worker.chunk_pages() emits several ordinals for any page
    over 3,000 characters -- 851,619 chunks over 760,043 pages -- so two
    chunks of the same page routinely both land in the top 5. Anchoring on
    whichever ranked higher scored a correct retrieval as a quote miss and,
    on a non-shadow run, blocked the cutover on it.
    """
    reasons = []
    hits = (response.get('hits') or [])[:top_k]
    quote = normalize(item['quote'])
    matches = [hit for hit in hits
               if hit.get('sha256') == item['sha256']
               and int(hit.get('page') or 0) == int(item['page'])]
    quoted = next((hit for hit in matches
                   if quote in normalize(hit.get('excerpt'))), None)
    # The report's anchor is the hit that carried the quote when there is one,
    # so hit_ids and the recorded evidence name the chunk that actually
    # answered rather than the one that happened to rank first.
    anchor = quoted or (matches[0] if matches else None)
    if not matches:
        found = ', '.join(f'{hit.get("sha256", "")[:8]}:p{hit.get("page")}'
                          for hit in hits) or 'nothing'
        reasons.append(f'no top-{top_k} hit for {item["sha256"][:8]} page '
                       f'{item["page"]} (returned {found})')
    elif quoted is None:
        reasons.append(f'the quote is in none of the {len(matches)} top-'
                       f'{top_k} hit(s) on that page (ordinals '
                       f'{[hit.get("ordinal") for hit in matches]})')

    if expect_vector:
        arm = (response.get('arms') or {}).get('vector') or {}
        if not any('vector' in (hit.get('sources') or []) for hit in hits):
            reasons.append(
                'no top-%d hit came from the vector arm (enabled=%r, '
                'reason=%r, candidates=%r) -- the arm is dead, not merely '
                'outranked' % (top_k, arm.get('enabled'), arm.get('reason'),
                               arm.get('candidates')))
    return {
        'id': item['id'],
        'sha256': item['sha256'],
        'page': item['page'],
        'passed': not reasons,
        'reasons': reasons,
        'anchor_found': anchor is not None,
        'quote_found': quoted is not None,
        'vector_sourced': any('vector' in (hit.get('sources') or [])
                              for hit in hits),
        'retrieval_mode': response.get('retrieval_mode'),
        'vector_arm': (response.get('arms') or {}).get('vector'),
        'hit_ids': [hit.get('chunk_id') for hit in hits],
        'anchor_chunk_id': None if anchor is None else anchor.get('chunk_id'),
    }


def run(args, items, invoke, embed):
    results = []
    for item in items:
        started = time.perf_counter()
        try:
            vector = embed(item['question']) if embed else None
            response = invoke(event_for(item, args, vector))
        except Exception as exc:                       # noqa: BLE001
            # A transport or embedding failure is a failed item, never a
            # skipped one: a gate that quietly drops items measures nothing.
            results.append({'id': item['id'], 'sha256': item['sha256'],
                            'page': item['page'], 'passed': False,
                            'reasons': [f'{type(exc).__name__}: {exc}'],
                            'anchor_found': False, 'quote_found': False,
                            'vector_sourced': False, 'retrieval_mode': None,
                            'vector_arm': None, 'hit_ids': []})
            continue
        verdict = judge(item, response, args.top_k,
                        bool(item.get('expect_vector_source')) and bool(embed))
        verdict['ms'] = round((time.perf_counter() - started) * 1000.0, 1)
        results.append(verdict)
    return results


def arm_disabled_in(results):
    """Items whose response reported arms.vector.enabled false.

    A disabled arm is not a ranking outcome, it is the arm not running: the
    index is missing, WS13_VECTOR_ARM is off, or no query_vector arrived. The
    lexical arm alone still answers most keyword questions, so without this
    the run looks like a pass.
    """
    return sorted(str(result['id']) for result in results
                  if (result.get('vector_arm') or {}).get('enabled') is False)


def certify(summary):
    """Reasons a non-shadow run must not certify a cutover, in order.

    Separate from the per-item verdicts because two of these are properties of
    the RUN: a set that never observed a vector-sourced hit measured only the
    lexical arm, whatever each item's expect_vector_source happened to say.
    """
    blocking = []
    failed = [result['id'] for result in summary['results']
              if not result['passed']]
    if failed:
        blocking.append(f'{len(failed)} item(s) failed: {", ".join(failed)}')
    if summary['vector_arm_disabled']:
        blocking.append(
            'the vector arm was reported disabled for '
            f'{len(summary["vector_arm_disabled"])} item(s): '
            f'{", ".join(summary["vector_arm_disabled"])}')
    if summary['items'] and not summary['vector_arm_rate']:
        blocking.append(
            'no item was answered by the vector arm (vector_arm_rate 0.0): '
            'this run measured the lexical arm only and cannot certify that '
            'ws13_chunks_titan_hnsw is being used')
    return blocking


def summarise(results, args, fixture_problems):
    total = len(results) or 1
    return {
        'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'target': args.function_name or 'dsn',
        'top_k': args.top_k,
        'shadow': bool(args.shadow),
        'embedded': not args.no_embed,
        'items': len(results),
        'passed': sum(1 for item in results if item['passed']),
        'recall_at_k': round(
            sum(1 for item in results if item['anchor_found']) / total, 4),
        'quote_rate': round(
            sum(1 for item in results if item['quote_found']) / total, 4),
        'vector_arm_rate': round(
            sum(1 for item in results if item['vector_sourced']) / total, 4),
        'vector_arm_disabled': arm_disabled_in(results),
        'fixture_problems': fixture_problems,
        'results': results,
    }


def report(summary):
    for item in summary['results']:
        mark = 'PASS' if item['passed'] else 'FAIL'
        ms = item.get('ms')
        print(f'{mark}  {item["id"]}  {item["sha256"][:12]} p{item["page"]}'
              + (f'  {ms} ms' if ms is not None else ''))
        for reason in item['reasons']:
            print(f'        {reason}')
    print(f'\n{summary["passed"]}/{summary["items"]} items passed  '
          f'recall@{summary["top_k"]}={summary["recall_at_k"]}  '
          f'quote={summary["quote_rate"]}  '
          f'vector_arm={summary["vector_arm_rate"]}')


def write_report(summary, args):
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(summary, handle, indent=2)
            handle.write('\n')
        print(f'wrote {path}')
    if args.append:
        path = Path(args.append)
        path.parent.mkdir(parents=True, exist_ok=True)
        # One line per run so a 48-72 h shadow run accumulates instead of
        # overwriting the evidence it is meant to build.
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(summary) + '\n')
        print(f'appended to {path}')


def main(argv=None):
    args = parse_args(argv)
    fixture = load(args.fixture)
    items = fixture.get('items') or []
    problems = require_complete(fixture)
    # --dry-run invokes nothing and certifies nothing, so the readiness
    # gate reports rather than blocks it.
    if problems and not (args.shadow or args.allow_incomplete
                         or args.dry_run):
        print('the known-item set is not ready to gate a cutover:',
              file=sys.stderr)
        for problem in problems:
            print(f'  {problem}', file=sys.stderr)
        print('re-run with --shadow to record results anyway, or with '
              '--allow-incomplete to report without certifying',
              file=sys.stderr)
        return EXIT_NOT_READY
    for problem in problems:
        print(f'note: {problem}')

    if args.dry_run:
        for item in items:
            print(f'  {item["id"]}  {item["sha256"][:12]} p{item["page"]}  '
                  f'{item["question"]}')
        print(json.dumps(event_for(items[0], args, None), indent=2)
              if items else 'no items')
        print('--dry-run: nothing invoked')
        return EXIT_OK

    summary = summarise(run(args, items, caller(args), embedder(args)), args,
                        problems)
    report(summary)
    write_report(summary, args)
    if args.shadow:
        # A shadow run measures; it never blocks. The numbers are the output.
        return EXIT_OK
    if problems:
        return EXIT_NOT_READY
    blocking = certify(summary)
    for reason in blocking:
        print(f'BLOCKED: {reason}', file=sys.stderr)
    return EXIT_FAILED if blocking else EXIT_OK


if __name__ == '__main__':
    raise SystemExit(main())
