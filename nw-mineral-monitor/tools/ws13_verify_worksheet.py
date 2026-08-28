#!/usr/bin/env python3
"""Build the worksheet a human verifies the known-item candidates on.

Phase E needs 25 verified known items before tools/ws13_live_known_items.py
will certify a cutover, and tools/ws13_gen_known_items.py can only propose
them: every item it emits is verified=false with a TEMPLATE question, because
a question built from the quote's own words proves the lexical arm works and
nothing else. The human step is to open each page, confirm the quote is on it
verbatim, and rewrite the question as a paraphrase.

Nothing on a generated item carries a viewer key, so without this each of the
24 starts with a lookup: the WS13 corpus is indexed in a Postgres the docs API
cannot read, and viewer.html therefore needs the stored original's s3_key
passed in the fragment. This asks the deployed retrieval Lambda for each
document once and writes the fragments out, so the lookup happens here rather
than 24 times by hand.

WHY THIS IS A REPOSITORY TOOL AND NOT A SCRATCH SCRIPT: the first version of
it lived in a temp directory, which meant `var/ws13/verify-worksheet.html` --
several hours of human verification, once it is filled in -- sat in one
gitignored folder on one machine with nothing able to rebuild it. The output
is still gitignored and still disposable; what matters is that regenerating it
is one command rather than an archaeology exercise.

DO NOT PUBLISH THE OUTPUT. The fragments carry private S3 object keys. That is
the same reason site/index.html keeps its DOC_WS13_ROUTES registry fed only
from tool results and never from model-authored text: a key is a capability
reference, and the only place one belongs is a request the signed-in viewer
makes for itself.

    tools/ws13_verify_worksheet.py
    tools/ws13_verify_worksheet.py --fixture tests/fixtures/ws13_known_items.json.new
    tools/ws13_verify_worksheet.py --site https://example.cloudfront.net
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / 'tests' / 'fixtures' / 'ws13_known_items.json'
DEFAULT_OUT = ROOT / 'var' / 'ws13' / 'verify-worksheet.html'
DEFAULT_FUNCTION = 'nw-mineral-monitor-ws13-query'
DEFAULT_REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')


def lookup(function_name, region, sha256, timeout=60):
    """The citation for one document, or None.

    Read-only: op 'documents' with a sha256 filter. A failure here is not
    fatal -- the item still needs verifying, it just costs the reader a manual
    lookup -- so this returns None rather than raising and the caller says so
    on the row itself.
    """
    payload = json.dumps({'op': 'documents', 'filters': {'sha256': sha256},
                          'limit': 1})
    out = ROOT / 'var' / 'ws13' / '.lookup.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ['aws', 'lambda', 'invoke', '--function-name', function_name,
         '--region', region, '--payload', payload, str(out)],
        capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        return None
    try:
        body = json.loads(out.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    documents = body.get('documents') or []
    return (documents[0].get('citation') or {}) if documents else None


def viewer_url(site, item, citation):
    """viewer.html's WS13 contract, or None when the route is unknown.

    s3_key is required and deliberately not defaulted: a fragment without one
    resolves nothing, and inventing a key shape here would be guessing at a
    private object's name.
    """
    if not citation or not citation.get('s3_key'):
        return None
    query = {'corpus': 'ws13', 'doc': item['sha256'],
             'page': str(item['page']), 'q': item.get('quote', ''),
             's3_key': citation['s3_key']}
    for field in ('rights_basis', 'viewer_key_kind'):
        if citation.get(field):
            query[field] = citation[field]
    if citation.get('document_title'):
        query['title'] = citation['document_title']
    return f"{site.rstrip('/')}/viewer.html#{urllib.parse.urlencode(query)}"


def render(rows, fixture_name):
    esc = html.escape
    parts = [
        '<!doctype html><meta charset="utf-8">',
        '<title>WS13 known-item verification</title>',
        '<style>body{font:15px/1.55 -apple-system,BlinkMacSystemFont,sans-serif;'
        'max-width:60rem;margin:2rem auto;padding:0 1rem;color:#16202A;'
        'background:#F1F4F6}'
        'h1{font-size:1.4rem}.it{background:#fff;border:1px solid #D8E0E5;'
        'border-radius:4px;padding:1rem 1.2rem;margin:1rem 0}'
        '.q{background:#F1F4F6;padding:.6rem .8rem;border-left:3px solid #1D6E8B;'
        'margin:.6rem 0;font-family:ui-monospace,Menlo,monospace;font-size:.9rem}'
        '.tmpl{color:#A8791C}.id{font-family:ui-monospace,Menlo,monospace;'
        'font-size:.82rem;color:#61737E}a{color:#1D6E8B}'
        'code{background:#F1F4F6;padding:.1em .35em;border-radius:2px}</style>',
        f'<h1>WS13 known-item verification &mdash; {len(rows)} items</h1>',
        '<p>For each: open the page, confirm the quote is on <em>that</em> page '
        'verbatim, then <strong>rewrite the question in your own words</strong> and '
        f'set <code>"verified": true</code> in <code>{esc(fixture_name)}</code>.</p>',
        '<p><strong>The paraphrase is the point.</strong> A question built from the '
        "quote's own words only proves the lexical arm works; the vector arm is what "
        'a paraphrase tests, and a silently dead vector arm is the failure this gate '
        'exists to catch. The generator refuses to let an item be marked verified '
        "while its question still repeats more than five of the quote's words in "
        'order.</p>',
    ]
    for item, citation, url in rows:
        link = (f'<p><a href="{esc(url)}" target="_blank" rel="noopener">'
                f'open page {esc(str(item["page"]))} &rarr;</a></p>') if url else (
            '<p><em>no viewer route resolved &mdash; look this one up by hand</em></p>')
        title = citation.get('document_title') or item['sha256'][:12]
        parts.append(
            f'<div class="it"><div class="id">{esc(item["id"])} &middot; '
            f'{esc(str(title))} &middot; {esc(str(item.get("admission_class")))}</div>'
            f'<div class="q">{esc(item.get("quote", ""))}</div>'
            f'<p class="tmpl"><strong>Template question (rewrite this):</strong> '
            f'{esc(item.get("question", ""))}</p>{link}</div>')
    return '\n'.join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--fixture', default=str(DEFAULT_FIXTURE))
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--site', default=os.environ.get(
        'WS13_SITE_URL', 'https://drkzgwu4c93ug.cloudfront.net'))
    parser.add_argument('--function-name', default=DEFAULT_FUNCTION)
    parser.add_argument('--region', default=DEFAULT_REGION)
    parser.add_argument('--no-lookup', action='store_true',
                        help='skip the Lambda entirely; every row renders '
                             'without a link (useful with no AWS credentials)')
    args = parser.parse_args(argv)

    fixture = json.loads(Path(args.fixture).read_text(encoding='utf-8'))
    # Verified items are done; rendering them again invites re-doing them.
    items = [i for i in fixture.get('items', []) if not i.get('verified')]
    if not items:
        print('every item is already verified; nothing to render')
        return 0

    rows = []
    for number, item in enumerate(items, 1):
        citation = None if args.no_lookup else lookup(
            args.function_name, args.region, item['sha256'])
        url = viewer_url(args.site, item, citation or {})
        print(f'  [{number}/{len(items)}] {item["id"]}'
              f'{"" if url else "  (no viewer route)"}', file=sys.stderr)
        rows.append((item, citation or {}, url))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, Path(args.fixture).name), encoding='utf-8')
    resolved = sum(1 for _, _, url in rows if url)
    print(f'wrote {out} ({resolved}/{len(rows)} with viewer links)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
