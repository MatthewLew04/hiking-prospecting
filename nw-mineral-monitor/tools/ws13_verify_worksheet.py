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
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

WHITESPACE = re.compile(r'\s+')
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")
# tests/test_ws13_known_items.MAX_SHARED_QUESTION_WORDS. An item may not be
# marked verified while its question still repeats more of the quote's words
# in order than this.
MAX_SHARED_QUESTION_WORDS = 5

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


def locate(function_name, region, item, timeout=60):
    """(quote is on that page, the page's excerpt), read from the corpus.

    WHAT THIS IS AND IS NOT. It asks the deployed retrieval function for the
    item's own page and checks the quote is inside the excerpt it returns,
    whitespace-normalised. That is a MECHANICAL check against the same chunk
    store retrieval reads, and it catches the two failures a reader should
    never have to hunt for by hand: a quote that is not on the page at all,
    and a quote the excerpt window cannot reach, which fails the live gate for
    a reason that has nothing to do with retrieval.

    It is NOT the human read of the source page, and it must never be
    described as one. A quote copied out of the corpus will always be found in
    the corpus; what a person adds is the judgement that the page really says
    this and that a question about it is answerable. Both checks were run on
    the 24 candidates carried into this fixture: four failed, three of them
    because the generated phrase was scanner debris rather than text.
    """
    payload = json.dumps({'op': 'search', 'query': item['quote'],
                          'filters': {'sha256': item['sha256']}, 'limit': 8,
                          'max_excerpt_chars': 1200})
    out = ROOT / 'var' / 'ws13' / '.locate.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ['aws', 'lambda', 'invoke', '--function-name', function_name,
         '--region', region, '--payload', payload, str(out)],
        capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        return None, ''
    try:
        body = json.loads(out.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, ''
    on_page = [hit for hit in (body.get('hits') or [])
               if hit.get('page') == item['page']]
    if not on_page:
        return False, ''
    needle = normalize(item['quote']).lower()
    for hit in on_page:
        excerpt = normalize(hit.get('excerpt'))
        if needle in excerpt.lower():
            return True, excerpt
    return False, normalize(on_page[0].get('excerpt'))


def normalize(text):
    """Whitespace-normalised, the same fold the fixture's quotes carry."""
    return WHITESPACE.sub(' ', str(text or '')).strip()


def content_words(text):
    """The words tools/ws13_gen_known_items.question_for() copies.

    That template keeps every word longer than three characters, in order, so
    this is exactly the sequence a rewritten question has to break. Words of
    three characters or fewer are excluded on purpose: 'the' and 'of' recur in
    any two English sentences and counting them would make every paraphrase
    look like a copy.
    """
    return [word.lower() for word in WORD_RE.findall(text or '')
            if len(word) > 3]


def shared_run(question, quote):
    """The longest run of the quote's words the question repeats, in order.

    The same measure tests/test_ws13_known_items.longest_shared_run() applies
    before it will let an item be marked verified. Duplicated rather than
    imported because that module is a test file and a tool that imported it
    would stop working wherever the suite is not installed; the two are
    asserted equal on the cases that separate them in
    tests/test_ws13_verify_worksheet.SharedRunTests, so the copy cannot drift.
    """
    haystack, needles = content_words(quote), content_words(question)
    best = 0
    for start in range(len(needles)):
        for end in range(start + best + 1, len(needles) + 1):
            run = needles[start:end]
            span = len(run)
            if not any(haystack[index:index + span] == run
                       for index in range(len(haystack) - span + 1)):
                break
            best = span
    return best


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
        '.ok{color:#2E7D5B;font-weight:600}.bad{color:#A93B32;font-weight:600}'
        '.ctx{background:#FBFCFD;border:1px dashed #D8E0E5;border-radius:3px;'
        'padding:.5rem .7rem;margin:.5rem 0;font-size:.82rem;color:#33454F;'
        'max-height:9rem;overflow:auto}'
        'mark{background:#FFF3BF}'
        'code{background:#F1F4F6;padding:.1em .35em;border-radius:2px}</style>',
        f'<h1>WS13 known-item verification &mdash; {len(rows)} items</h1>',
        '<p><strong>What is left for you.</strong> Each item below already '
        'carries a question written as a paraphrase, and its quote has been '
        'located on its page mechanically &mdash; the green line is the '
        'deployed retrieval function returning that page with that phrase in '
        'it. What a machine cannot do is read the page and judge that it '
        'really says this and that the question is answerable from it. Skim '
        'the highlighted quote in its page context; open the page for any '
        'that look wrong. Then run the tool again with '
        f'<code>--accept</code>, which sets <code>"verified": true</code> in '
        f'<code>{esc(fixture_name)}</code> for every item.</p>',
        '<p><strong>Why the question is a paraphrase.</strong> A question '
        "built from the quote's own words only proves the lexical arm works; "
        'the vector arm is what a paraphrase tests, and a silently dead '
        'vector arm is the failure this gate exists to catch. An item cannot '
        "be marked verified while its question repeats more than five of the "
        "quote's words in order, and the run each one actually repeats is "
        'printed below.</p>',
    ]
    for item, citation, url, located, context in rows:
        link = (f'<a href="{esc(url)}" target="_blank" rel="noopener">'
                f'open page {esc(str(item["page"]))} &rarr;</a>') if url else (
            '<em>no viewer route resolved &mdash; look this one up by hand</em>')
        title = citation.get('document_title') or item['sha256'][:12]
        if located is None:
            status = '<span class="tmpl">quote not checked</span>'
        elif located:
            status = ('<span class="ok">quote located on page '
                      f'{esc(str(item["page"]))}</span>')
        else:
            status = ('<span class="bad">QUOTE NOT FOUND on page '
                      f'{esc(str(item["page"]))} &mdash; do not accept this '
                      'item</span>')
        run = shared_run(item.get('question', ''), item.get('quote', ''))
        overlap = (f'<span class="bad">question repeats {run} of the '
                   "quote's words in order</span>"
                   if run > MAX_SHARED_QUESTION_WORDS else
                   f'question repeats {run} of the quote&rsquo;s words')
        body = ''
        if context:
            body = f'<div class="ctx">{highlight(context, item["quote"])}</div>'
        parts.append(
            f'<div class="it"><div class="id">{esc(item["id"])} &middot; '
            f'{esc(str(title))} &middot; {esc(str(item.get("admission_class")))}'
            f'</div>'
            f'<div class="q">{esc(item.get("quote", ""))}</div>'
            f'<p><strong>Question:</strong> {esc(item.get("question", ""))}</p>'
            f'<p>{status} &middot; {overlap} &middot; {link}</p>{body}</div>')
    return '\n'.join(parts)


def highlight(context, quote):
    """The page context with the quote marked, both HTML-escaped first.

    Escaped BEFORE the marker is inserted and the needle is escaped the same
    way, so a quote containing '<' still matches and nothing in the OCR can
    close a tag. The output is written to a local file and never published --
    see the header -- but a worksheet that could be broken by its own content
    would be broken by the first form field it rendered.
    """
    haystack, needle = html.escape(context), html.escape(quote)
    index = haystack.lower().find(needle.lower())
    if index < 0:
        return haystack
    return (haystack[:index] + '<mark>' + haystack[index:index + len(needle)]
            + '</mark>' + haystack[index + len(needle):])


def accept(fixture_path, rows):
    """Set verified on every item whose checks pass; refuse the rest.

    This is the operator asserting they have read the pages. It is deliberately
    a separate invocation from the one that renders the worksheet, and it
    refuses an item the mechanical checks failed rather than trusting the
    assertion to cover it -- a reader who has just skimmed 24 rows should not
    be able to wave through the one the tool could already tell was wrong.
    """
    payload = json.loads(Path(fixture_path).read_text(encoding='utf-8'))
    checked = {item['id']: (located, item) for item, _, _, located, _ in rows}
    accepted, refused = [], []
    for item in payload.get('items', []):
        if item.get('verified'):
            continue
        located = checked.get(item['id'], (None, None))[0]
        run = shared_run(item.get('question', ''), item.get('quote', ''))
        if located is not True:
            refused.append((item['id'], 'quote was not located on its page'))
        elif run > MAX_SHARED_QUESTION_WORDS:
            refused.append((item['id'], f'question repeats {run} of the '
                                        "quote's words in order"))
        else:
            item['verified'] = True
            accepted.append(item['id'])
    if not refused:
        payload['complete'] = True
    Path(fixture_path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8')
    return accepted, refused


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
    parser.add_argument('--check-quotes', action='store_true',
                        help="ask the deployed function for each item's page "
                             'and confirm the quote is in the excerpt it '
                             'returns. Mechanical, and not the human read')
    parser.add_argument('--accept', action='store_true',
                        help='set verified on every item whose quote located '
                             'and whose question is a paraphrase. Implies '
                             '--check-quotes: nothing is accepted on a check '
                             'that was not run. Run this AFTER reading the '
                             'worksheet -- it is your assertion, not the '
                             "tool's")
    args = parser.parse_args(argv)
    if args.accept and args.no_lookup:
        parser.error('--accept needs the deployed function; --no-lookup '
                     'cannot check a quote against anything')
    check = args.check_quotes or args.accept

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
        located, context = (locate(args.function_name, args.region, item)
                            if check else (None, ''))
        note = '' if url else '  (no viewer route)'
        if located is False:
            note += '  QUOTE NOT FOUND'
        print(f'  [{number}/{len(items)}] {item["id"]}{note}', file=sys.stderr)
        rows.append((item, citation or {}, url, located, context))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, Path(args.fixture).name), encoding='utf-8')
    resolved = sum(1 for row in rows if row[2])
    print(f'wrote {out} ({resolved}/{len(rows)} with viewer links)')
    if check:
        found = sum(1 for row in rows if row[3])
        print(f'quote check: {found}/{len(rows)} located on their own page')
    if args.accept:
        accepted, refused = accept(args.fixture, rows)
        print(f'accepted {len(accepted)} item(s)')
        for item_id, why in refused:
            print(f'  REFUSED {item_id}: {why}')
        if refused:
            print('the fixture is not complete while any item is refused')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
